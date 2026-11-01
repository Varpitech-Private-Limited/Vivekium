"""The proctoring tasks: the per-application report and the hourly reconciler.

Carved out of `workers/tasks.py` on 2026-09-24 (PLAN-p3 WP0) as a PURE MOVE:
task names, routes and retry policies are unchanged, and `workers/tasks.py`
imports this module so importing the registry still registers every task.
`pickready.purge_proctoring_events` stays in `workers/tasks.py`.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.workers.dispatch import dispatch, dispatch_after_commit
from app.workers.registry import Route, task
from app.workers.runtime import (
    worker_session as _worker_session,
    _run,
)

logger = logging.getLogger(__name__)

#: How far back and how many ended sessions without a report the hourly sweep
#: re-dispatches in one run. Bounded so a session whose report can never be
#: written (a deleted candidate, say) is not retried for ever, and so one sweep
#: cannot flood the queue after a long outage.
_REPORT_RETRY_WINDOW_DAYS = 7
_REPORT_RETRY_BATCH = 200


@task(
    name="pickready.generate_proctoring_report",
    route=Route.ECS,
    rls="bypass",
    rls_reason=(
        "the report names the candidate: reads the candidate row, which a tenant "
        "session cannot see when a databank candidate owned by another tenant is linked"
        " to this job"
    ),
    max_attempts=2,
    backoff_seconds=5.0,
)
def generate_proctoring_report(link_id: str):
    """Write the Proctoring Report for one application (proctoring spec 7).

    Idempotent: `report.generate` returns the existing row. A session still
    active whose conversation has completed is closed as completed here, on
    the conversation's own timestamp, because completion is decided by the
    assessment and the proctoring row merely records it. A session still
    active whose conversation has NOT completed is left alone and logged:
    the reconciler settles it later as abandoned, on the credit reconciler's
    clock, and reporting it now would date the report before the session
    ended.

    The AI-text observation runs here, post-submission, never in a request.
    """
    from app.models.assessment import AssessmentConversation
    from app.models.proctoring import (
        OUTCOME_ACTIVE,
        OUTCOME_COMPLETED,
        ProctoringSession,
    )
    from app.services.proctoring import ai_text
    from app.services.proctoring import report as proctoring_report

    async def _task():
        async with _worker_session() as session:
            ps = (
                await session.execute(
                    select(ProctoringSession).where(
                        ProctoringSession.job_candidate_link_id == uuid.UUID(str(link_id))
                    )
                )
            ).scalars().first()
            if ps is None:
                logger.info("proctoring.report_skipped no_session link_id=%s", link_id)
                return
            conversation = await session.get(AssessmentConversation, ps.conversation_id)
            if conversation is None:
                raise ValueError(f"proctoring session {ps.id} has no conversation")
            now = datetime.now(timezone.utc)
            if ps.outcome == OUTCOME_ACTIVE:
                if conversation.completed_at is None:
                    logger.warning(
                        "proctoring.report_deferred session_still_active session_id=%s",
                        ps.id,
                    )
                    return
                ps.outcome = OUTCOME_COMPLETED
                ps.ended_at = conversation.completed_at
                ps.updated_at = now
                await session.flush()
            await ai_text.scan_conversation(session, ps, conversation, now=now)
            await proctoring_report.generate(session, ps)
            await session.commit()
    _run(_task())


@task(
    name="pickready.reconcile_proctoring_sessions",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason="a sweep: one run reads every tenant's rows",
)
def reconcile_proctoring_sessions():
    """Hourly: close the sessions nothing else will close, and report them.

    Four states, and only four:

    1. The conversation completed but the session is still active. The report
       task was enqueued and lost (broker hiccup, worker restart). Re-enqueue
       it; it closes the session itself.
    2. A camera or microphone pause ran past its grace and no request came
       back to settle it: the candidate closed the tab while paused. The
       session ends exactly as a live request would have ended it
       (`ingestion.enforce_pause`, `DEVICE_RECOVERY_TIMED_OUT`, a technical
       failure), and that path orders the PRISM Report after this commit.
       Checked BEFORE abandonment: the rules the candidate was shown ended it
       two minutes into the pause, not hours later.
    3. The conversation never completed and the session has heard nothing for
       `credit_reconciliation.SETTLE_AFTER_HOURS`. The candidate closed the
       tab and did not return. The session is ended as `abandoned` and its
       report is enqueued. The clock is deliberately the credit reconciler's,
       so an assessment is never "abandoned" for proctoring while still
       "open" for billing, or the reverse.

    4. The session ENDED (completed, terminated, abandoned or a technical
       failure) more than `proctoring_report_overdue_minutes` ago and has no
       report: the report task was lost or failed after its retries. It is
       dispatched again. `report.generate` is idempotent and its insert is
       ON CONFLICT DO NOTHING, so a late report, or two racing, writes one row.
       Bounded to a recent window so an unreportable session is not retried for
       ever. Until it succeeds the PRISM Report states that the report could
       not be produced yet (`report.load_state`), never that nothing happened.

    A technical failure is NOT decided here. It is decided at termination
    time by `ingestion.outcome_for_termination`, which has the reason in
    hand; re-reading it from a row later would be a second implementation.
    """
    from datetime import timedelta

    from app.models.assessment import AssessmentConversation
    from app.models.proctoring import (
        OUTCOME_ABANDONED,
        OUTCOME_ACTIVE,
        ProctoringReport,
        ProctoringSession,
    )
    from app.services import credit_reconciliation
    from app.services.proctoring import ingestion as proctoring_ingestion
    from app.services.proctoring.config import get_config

    async def _task():
        async with _worker_session() as session:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(hours=credit_reconciliation.SETTLE_AFTER_HOURS)
            rows = (
                await session.execute(
                    select(ProctoringSession).where(
                        ProctoringSession.outcome == OUTCOME_ACTIVE
                    )
                )
            ).scalars().all()
            completed = 0
            timed_out = 0
            abandoned = 0
            for ps in rows:
                conversation = await session.get(AssessmentConversation, ps.conversation_id)
                if conversation is None:
                    continue
                if conversation.completed_at is not None:
                    dispatch(
                        "pickready.generate_proctoring_report",
                        args=[str(ps.job_candidate_link_id)],
                    )
                    completed += 1
                    continue
                if await proctoring_ingestion.enforce_pause(session, ps, now=now) is not None:
                    timed_out += 1
                    continue
                last_heard = ps.last_heartbeat_at or ps.started_at or ps.consented_at
                if last_heard >= cutoff:
                    continue
                await proctoring_ingestion.end_session(
                    session, ps, outcome=OUTCOME_ABANDONED, reason_code=None, now=now
                )
                # After the commit below: a task started before it would read
                # the session as still active and defer.
                dispatch_after_commit(
                    session,
                    "pickready.generate_proctoring_report",
                    args=[str(ps.job_candidate_link_id)],
                )
                abandoned += 1
            overdue = now - timedelta(minutes=get_config().report_overdue_minutes)
            window = now - timedelta(days=_REPORT_RETRY_WINDOW_DAYS)
            unreported = (
                await session.execute(
                    select(ProctoringSession.job_candidate_link_id)
                    .outerjoin(
                        ProctoringReport,
                        ProctoringReport.proctoring_session_id == ProctoringSession.id,
                    )
                    .where(
                        ProctoringSession.outcome != OUTCOME_ACTIVE,
                        ProctoringSession.ended_at <= overdue,
                        ProctoringSession.ended_at >= window,
                        ProctoringReport.id.is_(None),
                    )
                    .order_by(ProctoringSession.ended_at.desc())
                    .limit(_REPORT_RETRY_BATCH)
                )
            ).scalars().all()
            for link_id in unreported:
                dispatch_after_commit(
                    session, "pickready.generate_proctoring_report", args=[str(link_id)]
                )
            await session.commit()
            if completed or timed_out or abandoned or unreported:
                logger.info(
                    "proctoring.reconciled completed_requeued=%d pause_timed_out=%d "
                    "abandoned=%d report_redispatched=%d",
                    completed, timed_out, abandoned, len(unreported),
                )
    _run(_task())
