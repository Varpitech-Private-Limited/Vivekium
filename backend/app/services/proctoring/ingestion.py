"""Event ingestion: the browser reports, the server decides (sections 4 and 9).

THREE THINGS THE BROWSER IS NOT TRUSTED WITH
--------------------------------------------
1. THE PATH. The browser sends an identifier; `catalog.spec_for` says which
   consequence path it takes, and `classify` below moves an event DOWN a path
   when its own duration does not clear the rule the identifier claims. A
   focus loss under the ignore window is logged, not warned. A camera or
   microphone loss shorter than the glitch window is an interruption, not a
   pause. A browser cannot escalate by naming a graver identifier.
2. THE COUNT. The warning number comes from `state.increment_warning`, an
   atomic Redis INCR, and is mirrored onto the row afterwards. The browser
   never sends a count and would not be believed if it did. The same holds for
   the pause count, which is the number of pause rows (`device_pause.py`).
3. THE VERDICT. Whether the third warning ends the assessment is the
   recruiter's job-level setting, read from `jobs.proctoring_warning_policy`
   at the moment it matters and never cached in the browser.

ONE WARNING PER BATCH, AND WHY
------------------------------
A batch is a few seconds of one browser's life. A phone put down on the desk
produces one detection every few frames until it is moved, and a candidate
who has just been told to move it needs the chance to do so before the next
warning lands. The first warning-worthy event in a batch takes the warning;
the rest are recorded with `warning_issued = false` so the report can still
say how many times the thing happened.

A WARNING STOPS THE CLOCK WHILE IT IS ON THE SCREEN (PLAN-p3 3.7). A warning
that is not the end of the session opens a `warning` pause, closed by the
candidate's acknowledgement (`acknowledge_warning`) and capped at
`assessment_warning_pause_max_seconds` whether or not the acknowledgement ever
arrives. The time a candidate spends reading what they must fix is not taken
from their answer.

THE DEVICE PAUSE (the fourth path, 2026-09-24)
---------------------------------------------
A camera or microphone loss that clears the glitch window pauses the
assessment (`device_pause.py` decides; this module records and acts). Every
batch and every heartbeat first asks whether an open pause has run past its
grace (`enforce_pause`), because a candidate who never comes back sends no
recovery for anything to react to, and the answer must not wait for the
hourly sweep when a request is already here.

WHAT A TERMINATION DOES, IN ORDER
---------------------------------
Records the event, closes the proctoring session with its outcome and reason,
closes any pause still open, marks the conversation terminated so
`gate.require_active` refuses the next turn, hands
`pickready.run_functional_assessment` to the dispatcher to run AFTER the
transaction commits (so the scorer can never read a session the request then
rolled back), and returns a plain-language message for the candidate's screen.
It never deletes an answer.
"""
from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.assessment import AssessmentConversation
from app.models.proctoring import (
    ENDED_OUTCOMES,
    OUTCOME_TECHNICAL_FAILURE,
    OUTCOME_TERMINATED_INTEGRITY,
    OUTCOME_TERMINATED_WARNINGS,
    POLICY_TERMINATE,
    QUALITY_DEGRADED,
    QUALITY_GOOD,
    QUALITY_POOR,
    ProctoringEvent,
    ProctoringSession,
)
from app.schemas.proctoring import (
    EventBatchIn,
    EventIn,
    HeartbeatOut,
    IngestOut,
    PauseOut,
    TerminationOut,
    WarningOut,
)
from app.services.assessment_conversation import pauses
from app.services.proctoring import catalog, device_pause, identity, phrasing, state
from app.services.proctoring.config import ProctoringConfig, get_config

logger = logging.getLogger(__name__)

__all__ = [
    "CONVERSATION_TERMINATED",
    "NOTE_KEY",
    "NOTE_WITHIN_COOLDOWN",
    "NOTE_ALREADY_REPORTED",
    "NOTE_UNDER_IGNORE_WINDOW",
    "NOTE_DOWNGRADED_FROM",
    "NOTE_WITHIN_DISTANCE",
    "NOTE_NO_WARNING_LEFT",
    "NOTE_BATCH_ALREADY_WARNED",
    "NOTE_PAUSE_OPENED",
    "NOTE_ALREADY_PAUSED",
    "NOTE_PAUSE_LIMIT",
    "NOTE_RESUMED",
    "NOTE_NOT_PAUSED",
    "NOTE_RECOVERED_TOO_LATE",
    "RateLimited",
    "SessionEnded",
    "Classified",
    "WarningDecision",
    "classify",
    "decide_warning",
    "outcome_for_termination",
    "session_quality_for",
    "enqueue_after_commit",
    "ingest",
    "apply_server_event",
    "heartbeat",
    "terminate",
    "end_session",
    "enforce_pause",
    "pause_out",
    "acknowledge_warning",
    "termination_out",
]

#: `assessment_conversations.status` once proctoring has ended it. The
#: conversation is neither active (the gate refuses it) nor completed
#: (`completed_at` stays NULL, so the credit reconciler settles it as
#: incomplete, which is what it is).
CONVERSATION_TERMINATED = "terminated"

#: `metadata_json` key under which the server records WHY an event took a
#: different path from the one its identifier names, or what it did about it.
#: Internal; the report reads it to decide what counts as an occurrence.
NOTE_KEY = "server_note"
NOTE_WITHIN_COOLDOWN = "within_cooldown"
NOTE_ALREADY_REPORTED = "already_reported_this_session"
NOTE_UNDER_IGNORE_WINDOW = "under_ignore_window"
NOTE_DOWNGRADED_FROM = "downgraded_from"
NOTE_WITHIN_DISTANCE = "within_distance_threshold"
NOTE_NO_WARNING_LEFT = "no_warning_left"
NOTE_BATCH_ALREADY_WARNED = "batch_already_warned"
#: The device pause notes. A loss either opened a pause, arrived while one
#: was already open (the same interruption, not a new occurrence), or arrived
#: with every pause used. A recovery either resumed the assessment, found
#: nothing paused, or came after the grace.
NOTE_PAUSE_OPENED = "pause_opened"
NOTE_ALREADY_PAUSED = "already_paused"
NOTE_PAUSE_LIMIT = "pause_limit_reached"
NOTE_RESUMED = "resumed"
NOTE_NOT_PAUSED = "not_paused"
NOTE_RECOVERED_TOO_LATE = "recovered_after_grace"

#: What the sound-level record keeps of the browser's metadata (`_loud`). The
#: levels are dBFS of an uncalibrated microphone; `character` is the browser's
#: single-moment guess at whether the energy sat in the speech band.
LOUD_MEASURE = "uncalibrated_microphone_level"
LOUD_LEVEL_KEYS = ("peak_dbfs", "floor_dbfs")
LOUD_CHARACTERS = frozenset({"voice_like", "broadband"})

#: Consecutive-evidence counter names in `state`.
_IDENTITY_RUN = "identity_mismatch"

#: Terminations that are a technical failure rather than candidate behaviour
#: (section 7.3: "A candidate whose laptop camera died must never be presented
#: as suspicious"). Both ends of the device pause always are: the report
#: names the device and what happened to it, and the outcome does not guess
#: why. `INTEGRITY_CHECK_FAILED` is ambiguous (a dead tab or a candidate
#: poking at the page look the same from the server), so it is treated as
#: technical ONLY when the session had issued no warning at all: nothing else
#: in the session suggested the candidate was doing anything, and the honest
#: reading of an unexplained failure on a clean session is that something
#: broke.
_ALWAYS_TECHNICAL = catalog.DEVICE_REASONS
_TECHNICAL_WHEN_CLEAN = frozenset({"INTEGRITY_CHECK_FAILED"})

_MS_PER_SECOND = 1000

Enqueue = Callable[[str], None]


class RateLimited(RuntimeError):
    """The browser exceeded `catalog.CLIENT_RATE_LIMIT_PER_MINUTE`."""


class SessionEnded(RuntimeError):
    """An event arrived for a session that is over."""


@dataclass(frozen=True)
class Classified:
    """One event after the server has decided what it is."""

    event_type: str
    path: str
    note: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WarningDecision:
    number: int
    final: bool
    terminate: bool


def _ms(seconds: float) -> int:
    return int(seconds * _MS_PER_SECOND)


def _below(duration_ms: int | None, seconds: float) -> bool:
    """True when a MEASURED duration is under the rule. An unmeasured duration
    (None) is trusted as sent: the browser is the only party that timed it."""
    return duration_ms is not None and duration_ms < _ms(seconds)


def classify(event: EventIn, config: ProctoringConfig) -> Classified:
    """The server's reading of a client event (sections 4.1, 4.2, 9).

    Every downgrade here is a duration that did not clear the rule the
    identifier claims. Nothing is ever upgraded: a client cannot earn a
    graver path by sending a longer number, only lose one by sending a shorter.
    """
    kind = event.event_type
    duration = event.duration_ms
    if kind == "WINDOW_FOCUS_LOST" and _below(duration, config.focus_loss_ignore_under_seconds):
        return Classified(kind, catalog.PATH_C, {NOTE_KEY: NOTE_UNDER_IGNORE_WINDOW})
    if kind == "INTEGRITY_CHECK_FAILED" and _below(duration, config.integrity_failure_termination_seconds):
        return Classified("INTEGRITY_CHECK_WARNING", catalog.PATH_C, {NOTE_DOWNGRADED_FROM: kind})
    if kind in catalog.PAUSING and _below(duration, config.device_glitch_seconds):
        return Classified(
            catalog.INTERRUPTED_FORM[kind], catalog.PATH_C, {NOTE_DOWNGRADED_FROM: kind}
        )
    if kind == "CAMERA_OBSTRUCTED" and _below(duration, config.obstruction_seconds):
        return Classified("FACE_ABSENT_BRIEF", catalog.PATH_C, {NOTE_DOWNGRADED_FROM: kind})
    if kind == "FACE_ABSENT_EXTENDED" and _below(duration, config.face_absent_extended_seconds):
        if _below(duration, config.face_absent_moderate_seconds):
            return Classified("FACE_ABSENT_BRIEF", catalog.PATH_C, {NOTE_DOWNGRADED_FROM: kind})
        return Classified("FACE_ABSENT_MODERATE", catalog.PATH_B, {NOTE_DOWNGRADED_FROM: kind})
    if kind == "FACE_ABSENT_MODERATE" and _below(duration, config.face_absent_moderate_seconds):
        return Classified("FACE_ABSENT_BRIEF", catalog.PATH_C, {NOTE_DOWNGRADED_FROM: kind})
    return Classified(kind, catalog.spec_for(kind).path)


def decide_warning(number: int, max_warnings: int, policy: str) -> WarningDecision:
    """What the `number`th warning means under the job's policy (section 4.0).

    Below the limit: a warning. At the limit: the recruiter's setting decides,
    and the default setting is to continue, because the product never
    terminates without an explicit choice.
    """
    final = number >= max_warnings
    return WarningDecision(
        number=number,
        final=final,
        terminate=final and policy == POLICY_TERMINATE,
    )


def outcome_for_termination(reason_code: str, warnings_used: int) -> str:
    """Which ended outcome a Path A reason produces. See `_ALWAYS_TECHNICAL`."""
    if reason_code in _ALWAYS_TECHNICAL:
        return OUTCOME_TECHNICAL_FAILURE
    if reason_code in _TECHNICAL_WHEN_CLEAN and warnings_used <= 0:
        return OUTCOME_TECHNICAL_FAILURE
    return OUTCOME_TERMINATED_INTEGRITY


def session_quality_for(measured_fps: float | None, config: ProctoringConfig) -> str:
    """Section 3.6: a slow device is recorded, never refused."""
    if measured_fps is None:
        return QUALITY_GOOD
    if measured_fps < config.sampling_fps_degraded:
        return QUALITY_POOR
    if measured_fps < config.sampling_fps_normal:
        return QUALITY_DEGRADED
    return QUALITY_GOOD


def enqueue_after_commit(session: AsyncSession) -> Enqueue:
    """The default way a termination orders the PRISM Report: score what was
    answered, AFTER this transaction commits.

    Dispatching before the commit (what this replaced) let the scorer start
    against a session the request could still roll back, and a request that
    raised afterwards left a task running about a termination that never
    happened. `dispatch_after_commit` resolves the task and serialises the
    arguments now, inside the request, and invokes from SQLAlchemy's
    `after_commit`; a lost invoke is repaired by `release_held_assessments`,
    hourly. The proctoring report is generated by that task afterwards, so
    the two never race.
    """

    def _enqueue(link_id: str) -> None:
        from app.workers.dispatch import dispatch_after_commit

        dispatch_after_commit(session, "pickready.run_functional_assessment", args=[link_id])

    return _enqueue


def termination_out(ps: ProctoringSession) -> TerminationOut:
    reason = ps.termination_reason or ""
    return TerminationOut(
        reason_code=reason,
        message=phrasing.termination_message(reason, outcome=ps.outcome),
    )


# ── The per-batch context ────────────────────────────────────────────────────


@dataclass
class _Run:
    session: AsyncSession
    ps: ProctoringSession
    policy: str
    config: ProctoringConfig
    now: datetime
    enqueue: Enqueue
    warnings_used: int
    warned_this_batch: bool = False
    accepted: int = 0
    warning: WarningOut | None = None
    termination: TerminationOut | None = None

    @property
    def ended(self) -> bool:
        return self.termination is not None


def _store(
    run: _Run,
    *,
    event_type: str,
    path: str,
    occurred_at: datetime,
    duration_ms: int | None,
    confidence: float | None,
    question_id: uuid.UUID | None,
    metadata: dict[str, Any],
    warning_issued: bool = False,
    warning_number: int | None = None,
    event_id: uuid.UUID | None = None,
) -> ProctoringEvent:
    # The id is set HERE, not left to the column default: a SQLAlchemy default
    # lands at INSERT, and a pause opened for this event names it before any
    # flush has happened.
    row = ProctoringEvent(
        id=event_id or uuid.uuid4(),
        tenant_id=run.ps.tenant_id,
        proctoring_session_id=run.ps.id,
        event_type=event_type,
        occurred_at=occurred_at,
        duration_ms=duration_ms,
        path=path,
        warning_issued=warning_issued,
        warning_number=warning_number,
        confidence=confidence,
        question_id=question_id,
        metadata_json=metadata,
    )
    run.session.add(row)
    run.accepted += 1
    return row


async def end_session(
    session: AsyncSession,
    ps: ProctoringSession,
    *,
    outcome: str,
    reason_code: str | None,
    now: datetime,
) -> None:
    """Close the proctoring session and the conversation under it.

    Shared by every way a session can end early: a Path A event, the third
    warning under a terminate policy, the end of the device pause, and the
    reconciler's abandonment sweep. Any pause still open is closed at `now` (or
    at its own cap, if that came first): an ended assessment has no clock left
    to stop.
    """
    ps.outcome = outcome
    ps.ended_at = now
    ps.termination_reason = reason_code
    ps.updated_at = now
    conversation = await session.get(AssessmentConversation, ps.conversation_id)
    if conversation is not None and conversation.completed_at is None:
        conversation.status = CONVERSATION_TERMINATED
    await pauses.close_all_open(session, ps.conversation_id, at=now)
    await session.flush()
    await state.clear_session(ps.id)


async def terminate(
    session: AsyncSession,
    ps: ProctoringSession,
    reason_code: str,
    now: datetime,
    *,
    occurred_at: datetime | None = None,
    duration_ms: int | None = None,
    confidence: float | None = None,
    metadata: dict[str, Any] | None = None,
    enqueue: Enqueue | None = None,
) -> TerminationOut:
    """Path A. Records the terminating event, ends the session, orders the
    report after commit and returns the candidate's message."""
    if reason_code not in catalog.TERMINATING:
        raise ValueError(f"{reason_code!r} is not a Path A event")
    session.add(
        ProctoringEvent(
            id=uuid.uuid4(),
            tenant_id=ps.tenant_id,
            proctoring_session_id=ps.id,
            event_type=reason_code,
            occurred_at=occurred_at or now,
            duration_ms=duration_ms,
            path=catalog.PATH_A,
            warning_issued=False,
            warning_number=None,
            confidence=confidence,
            question_id=None,
            metadata_json=dict(metadata or {}),
        )
    )
    outcome = outcome_for_termination(reason_code, ps.warnings_used)
    await end_session(session, ps, outcome=outcome, reason_code=reason_code, now=now)
    logger.info(
        "proctoring.terminated session_id=%s reason=%s outcome=%s",
        ps.id, reason_code, outcome,
    )
    (enqueue or enqueue_after_commit(session))(str(ps.job_candidate_link_id))
    return termination_out(ps)


# ── The device pause ─────────────────────────────────────────────────────────


async def _terminate_timed_out(
    session: AsyncSession,
    ps: ProctoringSession,
    config: ProctoringConfig,
    now: datetime,
    pause_row,
    *,
    enqueue: Enqueue | None,
    source: str,
) -> TerminationOut:
    deadline = device_pause.grace_deadline(pause_row, config)
    return await terminate(
        session, ps, "DEVICE_RECOVERY_TIMED_OUT", now,
        occurred_at=deadline,
        duration_ms=int((deadline - pause_row.started_at).total_seconds() * _MS_PER_SECOND),
        metadata={"source": source},
        enqueue=enqueue,
    )


async def enforce_pause(
    session: AsyncSession,
    ps: ProctoringSession,
    *,
    now: datetime,
    enqueue: Enqueue | None = None,
) -> TerminationOut | None:
    """End the session if its device pause has run past the grace.

    Returns the termination rather than raising it, so the caller's
    transaction COMMITS the ended session: a raise would roll the ending back
    and the next request would find the same expired pause again. Called on
    every event batch and heartbeat, by the hourly reconciler for a browser
    that never came back, and by any assessment route that wants the answer
    before it proceeds.
    """
    config = get_config()
    row = await device_pause.expired_pause(session, ps, now=now, config=config)
    if row is None:
        return None
    return await _terminate_timed_out(
        session, ps, config, now, row, enqueue=enqueue, source="grace_exceeded"
    )


async def pause_out(session: AsyncSession, ps: ProctoringSession) -> PauseOut | None:
    """The device pause for the candidate's screen, or None once the session
    has ended (the termination is what the screen shows then)."""
    if ps.outcome in ENDED_OUTCOMES:
        return None
    config = get_config()
    current = await device_pause.state(session, ps, config=config)
    message = None
    if current.paused:
        message = phrasing.pause_message(
            current.opened_by,
            pauses_used=current.pauses_used,
            max_pauses=current.max_pauses,
            grace_seconds=config.device_grace_seconds,
        )
    return PauseOut(
        paused=current.paused,
        message=message,
        grace_deadline_at=current.grace_deadline_at,
        pauses_used=current.pauses_used,
        max_pauses=current.max_pauses,
    )


async def _pause(
    run: _Run,
    classified: Classified,
    *,
    occurred_at: datetime,
    duration_ms: int | None,
    confidence: float | None,
    question_id: uuid.UUID | None,
    metadata: dict[str, Any],
) -> None:
    """A camera or microphone loss that cleared the glitch window."""
    kind = classified.event_type
    event_id = uuid.uuid4()
    completed_loss = duration_ms is not None
    started_at = None
    if completed_loss:
        # The browser reports a loss it already saw end (its batch was held
        # while it was offline). The pause is dated back by the measured
        # duration so the clock is credited, and settled at once below.
        started_at = run.now - timedelta(milliseconds=duration_ms)
    decision = await device_pause.begin(
        run.session, run.ps, now=run.now, ref_id=event_id, config=run.config,
        started_at=started_at,
    )
    if decision is device_pause.Decision.SESSION_ENDED:
        run.termination = termination_out(run.ps)
        return
    note = {
        device_pause.Decision.PAUSED: NOTE_PAUSE_OPENED,
        device_pause.Decision.ALREADY_PAUSED: NOTE_ALREADY_PAUSED,
        device_pause.Decision.LIMIT_EXCEEDED: NOTE_PAUSE_LIMIT,
        device_pause.Decision.TIMED_OUT: NOTE_ALREADY_PAUSED,
    }[decision]
    _store(
        run, event_type=kind, path=catalog.PATH_P, occurred_at=occurred_at,
        duration_ms=duration_ms, confidence=confidence, question_id=question_id,
        metadata={**metadata, **classified.note, NOTE_KEY: note}, event_id=event_id,
    )
    if decision is device_pause.Decision.LIMIT_EXCEEDED:
        run.accepted += 1
        run.termination = await terminate(
            run.session, run.ps, "DEVICE_PAUSE_LIMIT_EXCEEDED", run.now,
            occurred_at=occurred_at, metadata={"device_event": kind}, enqueue=run.enqueue,
        )
        return
    if decision is device_pause.Decision.TIMED_OUT:
        await _recover_or_end(run, occurred_at=occurred_at, metadata={}, record=False)
        return
    logger.info(
        "proctoring.device_pause session_id=%s event=%s decision=%s",
        run.ps.id, kind, decision.value,
    )
    if decision is device_pause.Decision.PAUSED and completed_loss:
        if duration_ms > _ms(run.config.device_grace_seconds):
            # The device was gone for longer than the grace before the browser
            # could say so. The rule is the same whether the server heard
            # about it live or afterwards.
            pause_row = await pauses.unclosed_pause(
                run.session, run.ps.conversation_id, pauses.PAUSE_DEVICE_LOSS
            )
            if pause_row is None:  # pragma: no cover - begin() just opened it
                raise RuntimeError(f"device pause vanished under the lock on {run.ps.id}")
            run.accepted += 1
            run.termination = await _terminate_timed_out(
                run.session, run.ps, run.config, run.now, pause_row,
                enqueue=run.enqueue, source="reported_after_grace",
            )
            return
        await _recover_or_end(
            run, occurred_at=occurred_at, metadata={"source": "reported_after_recovery"},
            record=True,
        )


async def _recover_or_end(
    run: _Run, *, occurred_at: datetime, metadata: dict[str, Any], record: bool
) -> None:
    """The recovery half: close the pause inside the grace, or end the session.

    `record` is False when the caller already stored the event that triggered
    this (a loss that found the grace already spent), so the activity log does
    not show a recovery nobody reported.
    """
    decision, elapsed_ms = await device_pause.recover(
        run.session, run.ps, now=run.now, config=run.config
    )
    if decision is device_pause.Decision.SESSION_ENDED:
        run.termination = termination_out(run.ps)
        return
    note = {
        device_pause.Decision.RESUMED: NOTE_RESUMED,
        device_pause.Decision.NOT_PAUSED: NOTE_NOT_PAUSED,
        device_pause.Decision.TIMED_OUT: NOTE_RECOVERED_TOO_LATE,
    }[decision]
    if record:
        _store(
            run, event_type=catalog.DEVICE_RECOVERED, path=catalog.PATH_C,
            occurred_at=occurred_at, duration_ms=elapsed_ms, confidence=None,
            question_id=None, metadata={**metadata, NOTE_KEY: note},
        )
    if decision is device_pause.Decision.TIMED_OUT:
        pause_row = await pauses.unclosed_pause(
            run.session, run.ps.conversation_id, pauses.PAUSE_DEVICE_LOSS
        )
        run.accepted += 1
        if pause_row is None:  # pragma: no cover - recover() just found it open
            raise RuntimeError(f"device pause vanished under the lock on {run.ps.id}")
        run.termination = await _terminate_timed_out(
            run.session, run.ps, run.config, run.now, pause_row,
            enqueue=run.enqueue, source="recovered_after_grace",
        )
        return
    logger.info(
        "proctoring.device_recovery session_id=%s decision=%s",
        run.ps.id, decision.value,
    )



# ── The state machine ────────────────────────────────────────────────────────


async def _open_warning_pause(run: _Run, event_id: uuid.UUID) -> None:
    """Stop the clock while the warning is on the candidate's screen."""
    await pauses.open_pause(
        run.session,
        run.ps.conversation_id,
        pauses.PAUSE_WARNING,
        at=run.now,
        ref_id=event_id,
        max_seconds=get_settings().assessment_warning_pause_max_seconds,
    )


async def _apply(
    run: _Run,
    classified: Classified,
    *,
    occurred_at: datetime,
    duration_ms: int | None,
    confidence: float | None,
    question_id: uuid.UUID | None,
    metadata: dict[str, Any],
    event_id: uuid.UUID | None = None,
) -> None:
    """Apply one classified event to the session. The whole state machine.

    `event_id` names the row a logged-only event is stored under, for a
    caller that needs to find it again (the speech rule extends one event's
    duration across a run of chunks)."""
    kind, path = classified.event_type, classified.path
    spec = catalog.spec_for(kind)
    meta = {**metadata, **classified.note}

    if path == catalog.PATH_A:
        run.accepted += 1
        run.termination = await terminate(
            run.session, run.ps, kind, run.now,
            occurred_at=occurred_at, duration_ms=duration_ms,
            confidence=confidence, metadata=meta, enqueue=run.enqueue,
        )
        return

    if path == catalog.PATH_P:
        await _pause(
            run, classified, occurred_at=occurred_at, duration_ms=duration_ms,
            confidence=confidence, question_id=question_id, metadata=metadata,
        )
        return

    if kind == catalog.DEVICE_RECOVERED:
        await _recover_or_end(run, occurred_at=occurred_at, metadata=meta, record=True)
        return

    if kind == catalog.SOUND_LEVEL:
        await _loud(
            run, occurred_at=occurred_at, duration_ms=duration_ms,
            question_id=question_id, metadata=metadata,
        )
        return

    if path == catalog.PATH_C:
        _store(
            run, event_type=kind, path=path, occurred_at=occurred_at,
            duration_ms=duration_ms, confidence=confidence,
            question_id=question_id, metadata=meta, event_id=event_id,
        )
        if kind == "IDENTITY_CHECK_MISMATCH":
            await _identity_check(run, meta)
        return

    # Path B, the shared counter.
    if spec.once_per_session and not await state.claim_once(run.ps.id, kind):
        _store(
            run, event_type=kind, path=catalog.PATH_C, occurred_at=occurred_at,
            duration_ms=duration_ms, confidence=confidence, question_id=question_id,
            metadata={**meta, NOTE_KEY: NOTE_ALREADY_REPORTED},
        )
        return
    if spec.cooldown_key and await state.in_cooldown(run.ps.id, spec.cooldown_key):
        _store(
            run, event_type=kind, path=path, occurred_at=occurred_at,
            duration_ms=duration_ms, confidence=confidence, question_id=question_id,
            metadata={**meta, NOTE_KEY: NOTE_WITHIN_COOLDOWN},
        )
        return
    if run.warned_this_batch:
        _store(
            run, event_type=kind, path=path, occurred_at=occurred_at,
            duration_ms=duration_ms, confidence=confidence, question_id=question_id,
            metadata={**meta, NOTE_KEY: NOTE_BATCH_ALREADY_WARNED},
        )
        return
    if run.warnings_used >= run.config.max_warnings:
        # The limit was reached under a continue-and-note policy. Recorded so
        # the report says it kept happening; no further warning exists to give.
        _store(
            run, event_type=kind, path=path, occurred_at=occurred_at,
            duration_ms=duration_ms, confidence=confidence, question_id=question_id,
            metadata={**meta, NOTE_KEY: NOTE_NO_WARNING_LEFT},
        )
        return

    number = await state.increment_warning(run.ps.id)
    if number > run.config.max_warnings:
        # Two batches raced past the limit. The row's CHECK forbids a count
        # above the maximum, and there is no fourth warning to issue.
        _store(
            run, event_type=kind, path=path, occurred_at=occurred_at,
            duration_ms=duration_ms, confidence=confidence, question_id=question_id,
            metadata={**meta, NOTE_KEY: NOTE_NO_WARNING_LEFT},
        )
        return
    run.warnings_used = number
    run.ps.warnings_used = number
    run.ps.updated_at = run.now
    run.warned_this_batch = True
    if spec.cooldown and spec.cooldown_key:
        await state.start_cooldown(
            run.ps.id, spec.cooldown_key, getattr(run.config, spec.cooldown)
        )
    decision = decide_warning(number, run.config.max_warnings, run.policy)
    stored = _store(
        run, event_type=kind, path=path, occurred_at=occurred_at,
        duration_ms=duration_ms, confidence=confidence, question_id=question_id,
        metadata=meta, warning_issued=True, warning_number=number,
    )
    logger.info(
        "proctoring.warning session_id=%s event=%s number=%d final=%s terminate=%s",
        run.ps.id, kind, number, decision.final, decision.terminate,
    )
    if decision.terminate:
        await end_session(
            run.session, run.ps,
            outcome=OUTCOME_TERMINATED_WARNINGS, reason_code=kind, now=run.now,
        )
        run.enqueue(str(run.ps.job_candidate_link_id))
        run.termination = termination_out(run.ps)
        return
    await _open_warning_pause(run, stored.id)
    run.warning = WarningOut(
        number=number,
        max_warnings=run.config.max_warnings,
        event_type=kind,
        message=phrasing.warning_message(
            kind, number=number, max_warnings=run.config.max_warnings, policy=run.policy
        ),
        final=decision.final,
    )


async def _loud(
    run: _Run,
    *,
    occurred_at: datetime,
    duration_ms: int | None,
    question_id: uuid.UUID | None,
    metadata: dict[str, Any],
) -> None:
    """A sustained high microphone level: an ADVISORY, stored on Path C only.

    The browser measured an uncalibrated level, so the server stores it only
    when the duration the browser claims clears the sustained window, the
    advisory is not inside the cooldown, and no spoken answer was being
    captured (the candidate dictating is not an environment problem). A
    refused report is logged with its reason and not stored: storing it would
    put an occurrence in the Proctoring Report that the rules do not allow.
    Only the whitelisted fields are kept, and the record says in its own
    metadata that the measure is an uncalibrated microphone level.
    """
    from app.services.proctoring import audio as proctoring_audio  # noqa: PLC0415

    kind = catalog.SOUND_LEVEL
    reason: str | None = None
    if duration_ms is None or _below(duration_ms, run.config.loud_sustained_seconds):
        reason = "under_sustained_window"
    elif await proctoring_audio.voice_capture_open(
        run.session, run.ps.conversation_id, run.now, run.config
    ):
        reason = "spoken_answer_open"
    elif await state.in_cooldown(run.ps.id, kind):
        reason = "within_cooldown"
    if reason is not None:
        logger.info("proctoring.sound_level_refused session_id=%s reason=%s", run.ps.id, reason)
        return
    await state.start_cooldown(run.ps.id, kind, run.config.loud_cooldown_seconds)
    kept: dict[str, Any] = {"measure": LOUD_MEASURE}
    for key in LOUD_LEVEL_KEYS:
        value = metadata.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            kept[key] = round(float(value), 1)
    if metadata.get("character") in LOUD_CHARACTERS:
        kept["character"] = metadata["character"]
    _store(
        run, event_type=kind, path=catalog.PATH_C, occurred_at=occurred_at,
        duration_ms=duration_ms, confidence=None, question_id=question_id, metadata=kept,
    )


async def _identity_check(run: _Run, meta: dict[str, Any]) -> None:
    """Section 3.3: two consecutive mismatches confirm an identity mismatch.

    The run is counted in Redis so it survives the browser's batching, and it
    is reset by a heartbeat that reports a match (`heartbeat`) or by a check
    whose reported distance is inside the threshold.
    """
    distance = meta.get(identity.MISMATCH_DISTANCE_KEY)
    if not identity.is_mismatch(distance, run.config):
        meta[NOTE_KEY] = NOTE_WITHIN_DISTANCE
        await state.reset_consecutive(run.ps.id, _IDENTITY_RUN)
        return
    consecutive = await state.bump_consecutive(run.ps.id, _IDENTITY_RUN)
    if consecutive < run.config.identity_consecutive_mismatches:
        return
    await state.reset_consecutive(run.ps.id, _IDENTITY_RUN)
    run.accepted += 1
    run.termination = await terminate(
        run.session, run.ps, "IDENTITY_MISMATCH", run.now,
        metadata={"consecutive_mismatches": consecutive}, enqueue=run.enqueue,
    )


async def _start(
    session: AsyncSession,
    ps: ProctoringSession,
    policy: str,
    now: datetime,
    enqueue: Enqueue | None,
) -> _Run:
    if ps.outcome in ENDED_OUTCOMES:
        raise SessionEnded(ps.outcome)
    config = get_config()
    used = await state.seed_warnings(ps.id, ps.warnings_used)
    run = _Run(
        session=session, ps=ps, policy=policy, config=config, now=now,
        enqueue=enqueue or enqueue_after_commit(session), warnings_used=used,
    )
    # A pause that ran past its grace is settled before anything in this
    # batch is applied: whatever the batch carries happened after the moment
    # the rules had already ended the session.
    termination = await enforce_pause(session, ps, now=now, enqueue=run.enqueue)
    if termination is not None:
        run.accepted += 1
        run.termination = termination
    return run


async def _out(run: _Run) -> IngestOut:
    return IngestOut(
        accepted=run.accepted,
        warnings_used=run.warnings_used,
        max_warnings=run.config.max_warnings,
        status=run.ps.outcome,
        warning=run.warning,
        termination=run.termination,
        pause=await pause_out(run.session, run.ps),
    )


async def ingest(
    session: AsyncSession,
    ps: ProctoringSession,
    policy: str,
    batch: EventBatchIn,
    *,
    now: datetime,
    enqueue: Enqueue | None = None,
) -> IngestOut:
    """One client batch. Raises `SessionEnded`, `RateLimited` or
    `state.StateUnavailable`; the API maps those to 409, 429 and 503."""
    run = await _start(session, ps, policy, now, enqueue)
    total = await state.count_in_minute(ps.id, now, len(batch.events))
    if total > catalog.CLIENT_RATE_LIMIT_PER_MINUTE:
        logger.warning(
            "proctoring.rate_limited session_id=%s minute_total=%d", ps.id, total
        )
        raise RateLimited(str(total))
    for event in batch.events:
        if run.ended:
            # Nothing after a termination is applied: the session is over and
            # the counters are cleared. The events are dropped, not stored,
            # because storing them would date them after the session ended.
            break
        await _apply(
            run, classify(event, run.config),
            occurred_at=event.occurred_at, duration_ms=event.duration_ms,
            confidence=event.confidence, question_id=event.question_id,
            metadata=dict(event.metadata),
        )
    await session.flush()
    # Counts only: which events were stored against which were refused is how
    # a phone the browser saw is told apart from one the server never kept.
    logger.info(
        "proctoring.ingest session_id=%s received=%d stored=%d warned=%s ended=%s",
        ps.id, len(batch.events), run.accepted, run.warning is not None, run.ended,
    )
    return await _out(run)


async def apply_server_event(
    session: AsyncSession,
    ps: ProctoringSession,
    policy: str,
    event_type: str,
    *,
    now: datetime,
    duration_ms: int | None = None,
    question_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
    enqueue: Enqueue | None = None,
    event_id: uuid.UUID | None = None,
) -> IngestOut:
    """A SERVER-derived event (second voice, speech during a question that
    takes no spoken answer) through the same machinery a client event goes
    through, so a derived warning counts on the same counter and obeys the
    same policy."""
    if catalog.spec_for(event_type).client_emittable:
        raise ValueError(f"{event_type!r} is a client event; send it through ingest")
    run = await _start(session, ps, policy, now, enqueue)
    if not run.ended:
        await _apply(
            run, Classified(event_type, catalog.spec_for(event_type).path),
            occurred_at=now, duration_ms=duration_ms, confidence=None,
            question_id=question_id, metadata=dict(metadata or {}), event_id=event_id,
        )
    await session.flush()
    return await _out(run)


async def acknowledge_warning(
    session: AsyncSession, ps: ProctoringSession, *, now: datetime
) -> bool:
    """The candidate dismissed the warning on their screen: the clock runs
    again. True when a warning pause was open and this closed it; a repeat
    acknowledgement, or one for a warning whose cap already passed, is a
    no-op rather than an error, because the browser retries on a lost reply."""
    if ps.outcome in ENDED_OUTCOMES:
        return False
    open_pause = await pauses.current_pause(
        session, ps.conversation_id, pauses.PAUSE_WARNING, at=now
    )
    if open_pause is None:
        return False
    await pauses.close_pause(session, ps.conversation_id, pauses.PAUSE_WARNING, at=now)
    return True


async def heartbeat(
    session: AsyncSession,
    ps: ProctoringSession,
    *,
    identity_matched: bool | None,
    monitoring: dict[str, bool],
    now: datetime,
    enqueue: Enqueue | None = None,
    detection: dict[str, Any] | None = None,
) -> HeartbeatOut:
    """Section 9. A gap over `heartbeat_gap_seconds` is a monitoring
    interruption and appears in the report; a heartbeat that reports a
    matching identity check resets the consecutive-mismatch run; a device
    pause past its grace ends the session here, on the next heartbeat.

    An ended session answers with its termination rather than a 409, because
    the browser that missed the terminating response is exactly the one still
    sending heartbeats.

    A GAP IS RECORDED, NEVER A TERMINATION. PLAN-p3 3.0 item 7 proposed ending
    a session whose heartbeats stopped for longer than the grace. Not done:
    from here a candidate's dead network and this platform's own outage or a
    deploy look identical, and ending every in-flight assessment because the
    API was unreachable for three minutes is the unfair termination the plan's
    own risk list forbids. The gap is stated in the report with its length,
    and a camera or microphone the browser saw stop is paused and timed out
    on its own evidence.
    """
    config = get_config()
    if ps.outcome not in ENDED_OUTCOMES:
        await enforce_pause(session, ps, now=now, enqueue=enqueue)
    if ps.outcome in ENDED_OUTCOMES:
        await session.flush()
        return HeartbeatOut(
            status=ps.outcome,
            warnings_used=ps.warnings_used,
            server_time=now,
            interval_seconds=config.heartbeat_interval_seconds,
            termination=termination_out(ps),
            pause=None,
        )
    previous = ps.last_heartbeat_at or ps.started_at or ps.consented_at
    gap_ms = int((now - previous).total_seconds() * _MS_PER_SECOND)
    if gap_ms > _ms(config.heartbeat_gap_seconds):
        session.add(
            ProctoringEvent(
                id=uuid.uuid4(),
                tenant_id=ps.tenant_id,
                proctoring_session_id=ps.id,
                event_type="MONITORING_INTERRUPTED",
                occurred_at=previous,
                duration_ms=gap_ms,
                path=catalog.PATH_C,
                metadata_json={},
            )
        )
        logger.info("proctoring.heartbeat_gap session_id=%s gap_ms=%d", ps.id, gap_ms)
    ps.last_heartbeat_at = now
    ps.updated_at = now
    if detection is not None:
        logger.info(
            "proctoring.detection_stats session_id=%s frames=%s failed=%s fps=%s "
            "inference_ms=%s phone_best=%s",
            ps.id, detection.get("frames_processed"), detection.get("frames_failed"),
            detection.get("measured_fps"), detection.get("inference_ms_mean"),
            detection.get("phone_best_score"),
        )
    if identity_matched is True:
        await state.reset_consecutive(ps.id, _IDENTITY_RUN)
    down = sorted(name for name, alive in monitoring.items() if alive is False)
    if down and await state.claim_once(ps.id, "heartbeat_integrity_warning"):
        # The browser's own self-check escalates to INTEGRITY_CHECK_FAILED on
        # its own clock; this is the server's record that a heartbeat said a
        # component was down, once, so a browser whose events never arrive
        # still leaves a trace in the report.
        session.add(
            ProctoringEvent(
                id=uuid.uuid4(),
                tenant_id=ps.tenant_id,
                proctoring_session_id=ps.id,
                event_type="INTEGRITY_CHECK_WARNING",
                occurred_at=now,
                duration_ms=None,
                path=catalog.PATH_C,
                metadata_json={"components": down, "source": "heartbeat"},
            )
        )
    await session.flush()
    return HeartbeatOut(
        status=ps.outcome,
        warnings_used=ps.warnings_used,
        server_time=now,
        interval_seconds=config.heartbeat_interval_seconds,
        termination=None,
        pause=await pause_out(session, ps),
    )
