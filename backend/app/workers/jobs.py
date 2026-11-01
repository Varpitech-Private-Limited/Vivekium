"""The job ledger: the DB-authoritative truth about every dispatched task.

`models.background_job.BackgroundJob` is written by `dispatch_after_commit` in
the same transaction as the work that asked for the task (the outbox). This
module is everything that then moves the row, so there is exactly one
definition of each transition:

    worker side   claim -> heartbeat / set_stage -> complete | fail
    sweep side    sweep() re-drives rows a worker never claimed, and settles
                  rows whose worker stopped proving it was alive
    reader side   public_view() turns a row into the sentences a screen shows

WHAT THIS DELIBERATELY DOES NOT BELIEVE
---------------------------------------
- A Lambda `202` is acceptance of the invoke and nothing more. The row only
  becomes `running` when the worker claims it.
- A task that exits is not a task that finished. Only `complete` writes
  `succeeded`.
- Redis is a cache of progress detail. This table decides terminal truth.

DUPLICATE EXECUTION
-------------------
Lambda async delivery is at-least-once, the sweep may re-drive a slow start,
and a failed invocation is retried by the platform. `claim` is a single
conditional UPDATE, so of any number of deliveries of one run id exactly one
becomes the worker and the rest are told `duplicate` and do nothing. A job
that is already terminal is a duplicate too. A run id with no row (a plain
`dispatch()` outside a request) is `untracked` and runs as before.

BOUNDS
------
Every wait has a ceiling and a deterministic backoff: a never-started job is
re-driven at most `MAX_DISPATCH_ATTEMPTS` times, a capacity-blocked one for at
most `CAPACITY_MAX_AGE`, and a job whose worker went silent is re-driven only
when it was dispatched `idempotent`, at most `MAX_EXECUTIONS` times. Past a
bound the row is `failed` with a class saying which bound, never left pending.
"""
from __future__ import annotations

import logging
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Sequence

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.background_job import (
    IN_FLIGHT,
    STATE_ACCEPTED,
    STATE_CAPACITY_BLOCKED,
    STATE_FAILED,
    STATE_QUEUED,
    STATE_RETRYING,
    STATE_RUNNING,
    STATE_SUCCEEDED,
    STATE_WAITING_EXTERNAL,
    TERMINAL_STATES,
    WAITING_TO_START,
    BackgroundJob,
)

logger = logging.getLogger(__name__)

# -- Failure classes ---------------------------------------------------------
FAIL_CAPACITY = "capacity"
FAIL_CAPACITY_EXHAUSTED = "capacity_exhausted"
FAIL_DISPATCH = "dispatch"
FAIL_DISPATCH_EXHAUSTED = "dispatch_exhausted"
FAIL_WORKER_ERROR = "worker_error"
FAIL_PERMANENT = "permanent"
FAIL_WORKER_LOST = "worker_lost"
FAIL_UNKNOWN_TASK = "unknown_task"

# -- Bounds (seconds unless named) ------------------------------------------
#: A `queued` row this old was probably never invoked. The fast path normally
#: starts a worker within seconds; this is the margin before the sweep steps in.
QUEUE_GRACE = 120
#: An `accepted` row not claimed by now lost its start. Fargate needs longer.
START_GRACE = {"lambda": 300, "ecs": 900}
#: A worker beats every `HEARTBEAT_INTERVAL`; this many seconds of silence and
#: it is presumed lost. Twenty missed beats, longer than any single DB stall.
HEARTBEAT_INTERVAL = 30
STALE_HEARTBEAT = 600
#: While the sweep re-drives a row it holds a lease so a second sweep skips it.
LEASE = 120
MAX_DISPATCH_ATTEMPTS = 8
CAPACITY_MAX_AGE = timedelta(hours=24)
MAX_EXECUTIONS = 3
BACKOFF_BASE = 120
BACKOFF_MAX = 3600
SWEEP_BATCH = 25
RETENTION = timedelta(days=30)
#: Fewest finished runs before a duration is called "typical".
MIN_ETA_SAMPLES = 5
ETA_WINDOW = 20


def backoff_seconds(dispatch_attempts: int) -> int:
    """Deterministic: 2, 4, 8 ... minutes, capped at an hour."""
    return min(BACKOFF_BASE * (2 ** max(dispatch_attempts - 1, 0)), BACKOFF_MAX)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# -- Writing the row (the outbox half) --------------------------------------


def new_row(
    run_id: uuid.UUID | str,
    *,
    kind: str,
    route: str,
    args: Sequence[Any],
    kwargs: dict[str, Any],
    resource_type: str | None = None,
    resource_id: Any = None,
    required_capability: str | None = None,
    owner_user_id: uuid.UUID | None = None,
    idempotent: bool = False,
    dedupe_key: str | None = None,
) -> BackgroundJob:
    """The row `dispatch_after_commit` adds to the caller's session."""
    return BackgroundJob(
        id=uuid.UUID(str(run_id)),
        kind=kind,
        route=route,
        resource_type=resource_type,
        resource_id=None if resource_id is None else str(resource_id),
        required_capability=required_capability,
        owner_user_id=owner_user_id,
        idempotent=idempotent,
        dedupe_key=dedupe_key,
        payload={"args": list(args), "kwargs": dict(kwargs)},
        state=STATE_QUEUED,
    )


# -- Worker side -------------------------------------------------------------

CLAIMED = "claimed"
DUPLICATE = "duplicate"
UNTRACKED = "untracked"


async def claim(session: AsyncSession, run_id: str, *, now: datetime | None = None) -> str:
    """Become the one worker for `run_id`, or learn that someone else is.

    Returns CLAIMED, DUPLICATE (already running with a live heartbeat, or
    already terminal) or UNTRACKED (no ledger row).
    """
    now = now or _now()
    stale_before = now - timedelta(seconds=STALE_HEARTBEAT)
    row_id = uuid.UUID(str(run_id))
    claimed = (
        await session.execute(
            update(BackgroundJob)
            .where(
                BackgroundJob.id == row_id,
                or_(
                    BackgroundJob.state.in_(WAITING_TO_START),
                    and_(
                        BackgroundJob.state.in_(IN_FLIGHT),
                        func.coalesce(
                            BackgroundJob.heartbeat_at, BackgroundJob.started_at
                        )
                        < stale_before,
                    ),
                ),
            )
            .values(
                state=STATE_RUNNING,
                attempts=BackgroundJob.attempts + 1,
                started_at=func.coalesce(BackgroundJob.started_at, now),
                heartbeat_at=now,
                updated_at=now,
                next_retry_at=None,
                failure_class=None,
                failure_detail=None,
            )
            .returning(BackgroundJob.id)
        )
    ).first()
    await session.commit()
    if claimed is not None:
        return CLAIMED
    exists = (
        await session.execute(select(BackgroundJob.state).where(BackgroundJob.id == row_id))
    ).scalar_one_or_none()
    return UNTRACKED if exists is None else DUPLICATE


async def heartbeat(session: AsyncSession, run_id: str, *, now: datetime | None = None) -> None:
    now = now or _now()
    await session.execute(
        update(BackgroundJob)
        .where(BackgroundJob.id == uuid.UUID(str(run_id)), BackgroundJob.state.in_(IN_FLIGHT))
        .values(heartbeat_at=now, updated_at=now)
    )
    await session.commit()


async def set_stage(
    session: AsyncSession,
    run_id: str,
    stage: str,
    *,
    done: int | None = None,
    total: int | None = None,
    waiting_external: bool = False,
    now: datetime | None = None,
) -> None:
    """Record the current stage. `done`/`total` only for a measurable step."""
    now = now or _now()
    await session.execute(
        update(BackgroundJob)
        .where(BackgroundJob.id == uuid.UUID(str(run_id)), BackgroundJob.state.in_(IN_FLIGHT))
        .values(
            stage=stage[:120],
            progress_done=done,
            progress_total=total,
            state=STATE_WAITING_EXTERNAL if waiting_external else STATE_RUNNING,
            heartbeat_at=now,
            updated_at=now,
        )
    )
    await session.commit()


async def complete(session: AsyncSession, run_id: str, *, now: datetime | None = None) -> None:
    now = now or _now()
    await session.execute(
        update(BackgroundJob)
        .where(
            BackgroundJob.id == uuid.UUID(str(run_id)),
            BackgroundJob.state.notin_(TERMINAL_STATES),
        )
        .values(
            state=STATE_SUCCEEDED,
            completed_at=now,
            updated_at=now,
            heartbeat_at=now,
            payload={},
            failure_class=None,
            failure_detail=None,
            next_retry_at=None,
        )
    )
    await session.commit()


async def fail(
    session: AsyncSession,
    run_id: str,
    *,
    failure_class: str,
    detail: str,
    now: datetime | None = None,
) -> None:
    """Terminal failure. `detail` is a code or exception class name only."""
    now = now or _now()
    await session.execute(
        update(BackgroundJob)
        .where(
            BackgroundJob.id == uuid.UUID(str(run_id)),
            BackgroundJob.state.notin_(TERMINAL_STATES),
        )
        .values(
            state=STATE_FAILED,
            completed_at=now,
            updated_at=now,
            failure_class=failure_class,
            failure_detail=detail[:200],
            next_retry_at=None,
        )
    )
    await session.commit()


# -- Sweep side --------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """What one attempt to start a job produced. Built by `dispatch.redrive`."""

    accepted: bool
    failure_class: str | None = None
    detail: str = ""


Redrive = Callable[[BackgroundJob, int], Awaitable[Outcome]]


@dataclass
class SweepReport:
    redriven: int = 0
    accepted: int = 0
    blocked: int = 0
    failed: int = 0
    lost: int = 0
    purged: int = 0
    skipped: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Decision:
    action: str  # "redrive" | "fail"
    failure_class: str | None = None


def due(row: BackgroundJob, now: datetime) -> bool:
    """Is this row past the point where waiting longer is a guess?"""
    if row.state in TERMINAL_STATES:
        return False
    # `next_retry_at` is both the backoff and the sweep's lease.
    if row.next_retry_at is not None and row.next_retry_at > now:
        return False
    if row.state in IN_FLIGHT:
        beat = row.heartbeat_at or row.started_at or row.queued_at
        return beat < now - timedelta(seconds=STALE_HEARTBEAT)
    if row.next_retry_at is not None:
        return True
    if row.state in (STATE_ACCEPTED, STATE_RETRYING):
        base = row.dispatched_at or row.queued_at
        return base <= now - timedelta(seconds=START_GRACE[row.route])
    return row.queued_at <= now - timedelta(seconds=QUEUE_GRACE)


def decide(row: BackgroundJob, now: datetime) -> Decision:
    """Pure: what the sweep does with a due row."""
    if row.state in IN_FLIGHT:
        if row.idempotent and row.attempts < MAX_EXECUTIONS:
            return Decision("redrive")
        return Decision("fail", FAIL_WORKER_LOST)
    if row.failure_class == FAIL_CAPACITY and now - row.queued_at > CAPACITY_MAX_AGE:
        return Decision("fail", FAIL_CAPACITY_EXHAUSTED)
    if row.dispatch_attempts >= MAX_DISPATCH_ATTEMPTS:
        return Decision(
            "fail",
            FAIL_CAPACITY_EXHAUSTED
            if row.failure_class == FAIL_CAPACITY
            else FAIL_DISPATCH_EXHAUSTED,
        )
    return Decision("redrive")


async def sweep(
    session: AsyncSession,
    redrive: Redrive,
    *,
    now: datetime | None = None,
    limit: int = SWEEP_BATCH,
) -> SweepReport:
    """Re-drive, settle and purge. Safe to run concurrently with itself.

    Rows are leased (`next_retry_at` pushed out) in one short transaction, then
    worked outside any lock, because `redrive` is a network call.
    """
    now = now or _now()
    report = SweepReport()
    candidates = (
        await session.execute(
            select(BackgroundJob)
            .where(BackgroundJob.state.notin_(TERMINAL_STATES))
            .order_by(BackgroundJob.queued_at)
            .limit(limit * 4)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    leased = [row for row in candidates if due(row, now)][:limit]
    for row in leased:
        row.next_retry_at = now + timedelta(seconds=LEASE)
        row.updated_at = now
    await session.commit()

    for row in leased:
        decision = decide(row, now)
        if decision.action == "fail":
            await fail(
                session,
                str(row.id),
                failure_class=decision.failure_class or FAIL_DISPATCH_EXHAUSTED,
                detail=row.failure_detail or row.state,
                now=now,
            )
            report.failed += 1
            if decision.failure_class == FAIL_WORKER_LOST:
                report.lost += 1
            logger.warning(
                "job.sweep_failed job=%s kind=%s class=%s state=%s attempts=%d dispatches=%d",
                row.id, row.kind, decision.failure_class, row.state,
                row.attempts, row.dispatch_attempts,
            )
            continue
        attempt = row.dispatch_attempts + 1
        was_in_flight = row.state in IN_FLIGHT
        try:
            outcome = await redrive(row, attempt)
        except Exception as exc:  # noqa: BLE001 -- recorded on the row, never lost
            logger.exception("job.redrive_crashed job=%s kind=%s", row.id, row.kind)
            outcome = Outcome(False, FAIL_DISPATCH, type(exc).__name__)
        report.redriven += 1
        await _record_redrive(session, row, attempt, outcome, was_in_flight, now, report)
    purged = await session.execute(
        delete(BackgroundJob).where(
            BackgroundJob.state.in_(TERMINAL_STATES),
            BackgroundJob.completed_at < now - RETENTION,
        )
    )
    report.purged = purged.rowcount or 0
    await session.commit()
    return report


async def _record_redrive(
    session: AsyncSession,
    row: BackgroundJob,
    attempt: int,
    outcome: Outcome,
    was_in_flight: bool,
    now: datetime,
    report: SweepReport,
) -> None:
    values: dict[str, Any] = {
        "dispatch_attempts": attempt,
        "updated_at": now,
        "dispatched_at": now,
    }
    if outcome.accepted:
        values.update(
            state=STATE_RETRYING if was_in_flight else STATE_ACCEPTED,
            next_retry_at=None,
            failure_class=None,
            failure_detail=None,
        )
        report.accepted += 1
    else:
        capacity = outcome.failure_class == FAIL_CAPACITY
        values.update(
            state=STATE_CAPACITY_BLOCKED if capacity else (
                STATE_RETRYING if was_in_flight else STATE_QUEUED
            ),
            failure_class=outcome.failure_class or FAIL_DISPATCH,
            failure_detail=(outcome.detail or "")[:200],
            next_retry_at=now + timedelta(seconds=backoff_seconds(attempt)),
        )
        if capacity:
            report.blocked += 1
    await session.execute(
        update(BackgroundJob)
        .where(BackgroundJob.id == row.id, BackgroundJob.state.notin_(TERMINAL_STATES))
        .values(**values)
    )
    await session.commit()
    logger.info(
        "job.redriven job=%s kind=%s attempt=%d accepted=%s class=%s",
        row.id, row.kind, attempt, outcome.accepted, outcome.failure_class,
    )


# -- Reader side -------------------------------------------------------------


async def latest(
    session: AsyncSession, resource_type: str, resource_id: Any
) -> BackgroundJob | None:
    """The newest job for one resource. The caller has authorised the resource;
    RLS has already confined the rows to the caller's tenant."""
    return (
        await session.execute(
            select(BackgroundJob)
            .where(
                BackgroundJob.resource_type == resource_type,
                BackgroundJob.resource_id == str(resource_id),
            )
            .order_by(BackgroundJob.queued_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@dataclass(frozen=True)
class Estimate:
    low_seconds: float
    high_seconds: float
    samples: int


async def typical_duration(session: AsyncSession, kind: str) -> Estimate | None:
    """The middle half of the last finished runs of this kind, or None.

    Nothing is invented: fewer than `MIN_ETA_SAMPLES` measured runs gives no
    estimate at all, and the screen says so.
    """
    rows = (
        await session.execute(
            select(
                func.extract("epoch", BackgroundJob.completed_at - BackgroundJob.started_at)
            )
            .where(
                BackgroundJob.kind == kind,
                BackgroundJob.state == STATE_SUCCEEDED,
                BackgroundJob.started_at.isnot(None),
            )
            .order_by(BackgroundJob.completed_at.desc())
            .limit(ETA_WINDOW)
        )
    ).scalars().all()
    seconds = sorted(float(value) for value in rows if value is not None and value >= 0)
    if len(seconds) < MIN_ETA_SAMPLES:
        return None
    quartiles = statistics.quantiles(seconds, n=4, method="inclusive")
    return Estimate(quartiles[0], quartiles[2], len(seconds))


_MESSAGES = {
    STATE_QUEUED: "Waiting for a worker to pick this up.",
    STATE_ACCEPTED: "Accepted. Waiting for a worker to start.",
    STATE_CAPACITY_BLOCKED: (
        "All workers are busy. This starts on its own as soon as there is room."
    ),
    STATE_RETRYING: "A worker stopped unexpectedly. Trying again.",
    STATE_WAITING_EXTERNAL: "Waiting for an outside service to respond.",
    STATE_RUNNING: "Working on it.",
    STATE_SUCCEEDED: "Finished.",
}
_FAILURE_MESSAGES = {
    FAIL_CAPACITY_EXHAUSTED: "This could not start because workers stayed busy.",
    FAIL_DISPATCH_EXHAUSTED: "This could not be started.",
    FAIL_WORKER_LOST: "The worker stopped before it finished.",
    FAIL_PERMANENT: "This cannot be completed with the information given.",
    FAIL_UNKNOWN_TASK: "This could not be started.",
}


def public_view(
    row: BackgroundJob, *, now: datetime | None = None, estimate: Estimate | None = None
) -> dict[str, Any]:
    """The response body for one job. Server-written words, no internals.

    `percent` exists only when the task reported a measurable done/total.
    The estimate exists only while running, only from measured runs, and is
    dropped once the run is already slower than the typical middle half.
    """
    now = now or _now()
    percent = None
    if row.progress_total and row.progress_done is not None and row.progress_total > 0:
        percent = max(0, min(100, int(100 * row.progress_done / row.progress_total)))
    eta = None
    if estimate is not None and row.state in IN_FLIGHT and row.started_at is not None:
        elapsed = (now - row.started_at).total_seconds()
        if elapsed <= estimate.high_seconds:
            eta = {
                "low_seconds": max(0, int(estimate.low_seconds - elapsed)),
                "high_seconds": max(0, int(estimate.high_seconds - elapsed)),
                "samples": estimate.samples,
            }
    if row.state == STATE_FAILED:
        message = _FAILURE_MESSAGES.get(
            row.failure_class or "", "This did not complete."
        )
    else:
        message = _MESSAGES[row.state]
    return {
        "job_id": str(row.id),
        "kind": row.kind,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "state": row.state,
        "terminal": row.state in TERMINAL_STATES,
        "stage": row.stage,
        "message": message,
        "queued_at": row.queued_at,
        "started_at": row.started_at,
        "heartbeat_at": row.heartbeat_at,
        "updated_at": row.updated_at,
        "completed_at": row.completed_at,
        "attempts": row.attempts,
        "failure_class": row.failure_class if row.state == STATE_FAILED else None,
        "next_retry_at": row.next_retry_at if row.state in WAITING_TO_START else None,
        "percent": percent,
        "estimate": eta,
        "can_retry": row.state == STATE_FAILED
        and row.idempotent
        and row.failure_class != FAIL_PERMANENT,
    }


async def has_active(session: AsyncSession, resource_type: str, resource_id: Any) -> bool:
    """Is a non-terminal job already tracking this resource?

    A domain sweep asks before re-dispatching: while a job is queued, blocked or
    running, the ledger sweep owns getting it started, and a second dispatch
    would only stack a duplicate behind it.
    """
    return (
        await session.execute(
            select(BackgroundJob.id)
            .where(
                BackgroundJob.resource_type == resource_type,
                BackgroundJob.resource_id == str(resource_id),
                BackgroundJob.state.notin_(TERMINAL_STATES),
            )
            .limit(1)
        )
    ).first() is not None


async def requeue(session: AsyncSession, row: BackgroundJob, *, now: datetime | None = None) -> bool:
    """Put a failed, retry-safe job back to `queued`. False if it is not one."""
    now = now or _now()
    if row.state != STATE_FAILED or not row.idempotent or row.failure_class == FAIL_PERMANENT:
        return False
    result = await session.execute(
        update(BackgroundJob)
        .where(BackgroundJob.id == row.id, BackgroundJob.state == STATE_FAILED)
        .values(
            state=STATE_QUEUED,
            queued_at=now,
            updated_at=now,
            completed_at=None,
            next_retry_at=None,
            failure_class=None,
            failure_detail=None,
            dispatch_attempts=0,
            attempts=0,
            started_at=None,
            heartbeat_at=None,
            stage=None,
            progress_done=None,
            progress_total=None,
        )
    )
    return bool(result.rowcount)
