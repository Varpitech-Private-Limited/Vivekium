"""What a dispatched task run is doing, for the screens that poll it.

THE DATABASE DECIDES, REDIS ONLY DESCRIBES (rewritten 2026-11-01)
------------------------------------------------------------------
A run's state comes from its `background_jobs` row (`workers/jobs`), written in
the dispatching transaction and moved by the worker and the sweep. Redis holds
the thing a database row should not: the in-flight stage payload and the
task's return value, which can be a recruiter's draft text. It expires after
`TTL_SECONDS`, and an expired or unreachable Redis record therefore changes
nothing about the STATE a screen is told; it only means the payload is gone.

The previous rule, that an id missing from Redis reads PENDING, made a run
whose record expired, whose Redis was down, or whose worker never started look
"queued" for ever. That was a lie by construction, so:

  ledger row present   state is the row's, mapped to the vocabulary below
  no row, Redis record Redis's (a plain `dispatch()` with no ledger row)
  neither              UNKNOWN. Not PENDING: nothing in the system says this
                       run is waiting, so nothing here claims it is

UNKNOWN is not terminal (`done` is False), so pollers behave as they did, but
a screen can now say "status unavailable" instead of "waiting".

Celery's names are kept so the response schemas, the frontend union and the
existing tests keep their vocabulary. The permanent record of what a task
DID is elsewhere and unaffected: `email_log` for delivery, `audit_logs` for
the trigger, and the rows the task itself wrote.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.core.redis_loop import LoopBoundRedis

logger = logging.getLogger(__name__)

#: Terminal and non-terminal states. The names match Celery's so the response
#: schemas, the frontend union and the existing tests keep their vocabulary.
STATE_PENDING = "PENDING"
STATE_PROGRESS = "PROGRESS"
STATE_SUCCESS = "SUCCESS"
STATE_FAILURE = "FAILURE"
#: No ledger row and no Redis record. See the module docstring.
STATE_UNKNOWN = "UNKNOWN"

TERMINAL_STATES = frozenset({STATE_SUCCESS, STATE_FAILURE})

#: Six hours: longer than any run this product performs, plus the time a
#: recruiter might leave the page open afterwards. Generous on purpose, because
#: an expired record is indistinguishable from one that was never written and
#: therefore reads as PENDING for ever (see the docstring).
TTL_SECONDS = 6 * 3600

_PREFIX = "pickready:taskrun:v1"

#: LOOP-BOUND, since 2026-09-24. This module used to cache ONE client for the
#: life of the process, and every task body runs under its own `asyncio.run`
#: (`workers/runtime._run`), so on a warm Lambda the second invocation's reads
#: and writes met a client built on the first invocation's dead loop, failed
#: inside redis-py, and were swallowed at DEBUG. The job page's stage list and
#: the outreach modal's delivery state then read PENDING for ever, with nothing
#: in any log at the default level. `LoopBoundRedis` is the one implementation
#: of the rule `core/cache` already followed.
_CLIENT = LoopBoundRedis(name="taskrun_status", socket_timeout=5, connect_timeout=5)


def _key(run_id: str) -> str:
    return f"{_PREFIX}:{run_id}"


def _redis():
    """The client for the running loop, or None when one cannot be built."""
    return _CLIENT.client()


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    state: str
    #: The task's own payload: progress stages while running, the return value
    #: on success. Never an exception object -- see `record_failure`.
    payload: dict[str, Any]
    #: Present only on FAILURE. The exception CLASS NAME, never its message:
    #: a message can quote a row, and this is read by a recruiter's browser.
    error: str | None = None
    #: Where the state came from: "ledger", "redis" or "none".
    source: str = "redis"
    #: The ledger says the run succeeded but its Redis result has expired.
    result_expired: bool = False

    @property
    def done(self) -> bool:
        return self.state in TERMINAL_STATES


def unknown(run_id: str) -> RunStatus:
    return RunStatus(run_id=run_id, state=STATE_UNKNOWN, payload={}, source="none")


#: The ledger's states in the status vocabulary above.
_LEDGER_TO_STATE = {
    "queued": STATE_PENDING,
    "accepted": STATE_PENDING,
    "capacity_blocked": STATE_PENDING,
    "retrying": STATE_PENDING,
    "running": STATE_PROGRESS,
    "waiting_external": STATE_PROGRESS,
    "succeeded": STATE_SUCCESS,
    "failed": STATE_FAILURE,
}


async def _ledger_state(run_id: str, session: Any) -> tuple[str, str | None] | None:
    """(ledger state, failure detail) for one run, or None when no row.

    Without a `session` this is a one-column bypass read by an unguessable id,
    for the pollers that authorise the run themselves (`matching`'s audit row,
    `leadership`'s owner match). Pass the caller's tenant session to confine
    the read by RLS instead. Raises on a database error; `read` handles it.
    """
    import uuid

    from sqlalchemy import select

    from app.core.db import get_session_factory, superadmin_scope
    from app.models.background_job import BackgroundJob

    try:
        row_id = uuid.UUID(str(run_id))
    except ValueError:
        return None
    query = select(BackgroundJob.state, BackgroundJob.failure_detail).where(
        BackgroundJob.id == row_id
    )
    if session is not None:
        row = (await session.execute(query)).first()
    else:
        async with get_session_factory()() as own:
            async with own.begin():
                async with superadmin_scope(own):
                    row = (await own.execute(query)).first()
    return None if row is None else (row[0], row[1])


async def _read_redis(run_id: str) -> RunStatus | None:
    client = _redis()
    if client is None:
        return None
    try:
        raw = await client.get(_key(run_id))
    except Exception as exc:  # noqa: BLE001 -- an unreadable cache is absent, loudly
        # WARNING, never DEBUG: at DEBUG this was the invisible half of the
        # warm-worker defect above. The run id and the exception CLASS only;
        # never the payload, which a recruiter's browser reads.
        logger.warning(
            "taskrun.read_failed run_id=%s error=%s", run_id, type(exc).__name__
        )
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("taskrun.read_corrupt run_id=%s", run_id)
        return None
    return RunStatus(
        run_id=run_id,
        state=str(data.get("state") or STATE_PENDING),
        payload=data.get("payload") if isinstance(data.get("payload"), dict) else {},
        error=data.get("error"),
    )


async def read(run_id: str, session: Any = None) -> RunStatus:
    """The run's state: the ledger's if it has a row, else Redis's, else UNKNOWN."""
    try:
        ledger = await _ledger_state(run_id, session)
    except Exception as exc:  # noqa: BLE001 -- falls back to the cache, loudly
        logger.warning(
            "taskrun.ledger_read_failed run_id=%s error=%s", run_id, type(exc).__name__
        )
        ledger = None
    cached = await _read_redis(run_id)
    if ledger is None:
        return cached if cached is not None else unknown(run_id)
    state = _LEDGER_TO_STATE[ledger[0]]
    # Redis supplies detail only when it agrees with the ledger about the phase;
    # a stale PROGRESS record never decorates a finished run.
    payload = cached.payload if cached is not None and cached.state == state else {}
    return RunStatus(
        run_id=run_id,
        state=state,
        payload=payload,
        error=ledger[1] if state == STATE_FAILURE else None,
        source="ledger",
        result_expired=state == STATE_SUCCESS and cached is None,
    )


async def write(
    run_id: str,
    state: str,
    payload: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Record a run's state. Never raises into the work it describes.

    A status display that can fail the task it is reporting on is a strictly
    worse trade than a display that goes blank, which is the same rule
    `matching_progress.Progress` already states for its own publish callback.
    """
    client = _redis()
    if client is None:
        return
    body = json.dumps(
        {"state": state, "payload": payload or {}, "error": error},
        default=str,
    )
    try:
        await client.set(_key(run_id), body, ex=TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 -- never fails the work it describes
        logger.warning(
            "taskrun.write_failed run_id=%s state=%s error=%s",
            run_id, state, type(exc).__name__,
        )


async def close() -> None:
    await _CLIENT.aclose()
