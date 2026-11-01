"""The one place a background task is actually executed.

Every entry point converges here: the generic worker Lambda, the on-demand
Fargate task, and the in-process thread the docker-compose stack uses. There is
one implementation of "resolve a name, open a session, run it, retry it, record
what happened", so a task cannot behave differently depending on which door it
came through.

THE RETRY LOOP PREDICTS, IT DOES NOT MERELY OBSERVE
---------------------------------------------------
Before starting attempt N+1 the loop asks whether that attempt could FINISH
inside the remaining budget, using the longest attempt seen so far as the
estimate. `elapsed >= deadline` is the check that sounds right and is not: a
task whose attempts take eighty seconds under a hundred-second budget starts a
second attempt at eighty seconds and is killed by the platform at a hundred,
having thrown away the work of both. This is the same rule `agent_loop` and
`llm_router` already state, applied to the outer loop.

THERE IS NO SOFT TIME LIMIT ANY MORE, AND THAT IS A SIMPLIFICATION
------------------------------------------------------------------
Celery had two ceilings: a soft one that raised inside the task and a hard one
that killed it. The soft limit existed to turn a hang into a retryable
exception, and the lesson this codebase then recorded was that retrying a
timeout is exactly wrong, so `SoftTimeLimitExceeded` had to be added to a
no-retry exclusion list to undo the thing the soft limit was for.

Now the ceiling belongs to the platform, and the two platforms differ:

  Lambda  a hard function timeout. A task that outruns it is killed, and no
          retry is attempted, because the retry loop lives inside the
          invocation that was killed. The exclusion list is gone because
          nothing needs excluding: a task that could not finish in its budget
          will not finish in another one.

  Fargate NO ceiling. An on-demand task runs until its process exits, and
          `stopTimeout` is a SIGTERM grace period rather than a limit on the
          work. `DEFAULT_BUDGET_SECONDS` below is a self-imposed bound on the
          RETRY LOOP for that case, and it never cuts an attempt short.

WHAT A FAILURE COSTS
--------------------
The final failure is recorded in the run status and logged, and then it is
RE-RAISED. The Lambda invocation fails, which is what makes the function's
error metric move, the CloudWatch alarm fire and the on-failure destination
publish. Swallowing it here would leave the platform believing every
invocation succeeded, which is the reporting failure this product has already
been bitten by from the other direction.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.workers import jobs, status
from app.workers.registry import TaskSpec, resolve

logger = logging.getLogger(__name__)

#: Used when the caller cannot say how much time is left, which is the Fargate
#: entry point: an on-demand task has NO platform-imposed ceiling. It runs until
#: its process exits, and `stopTimeout` is a SIGTERM grace period rather than a
#: limit on the work.
#:
#: So this is not a mirror of a platform bound, it is a self-imposed one, and it
#: exists for a narrow purpose: to stop the RETRY LOOP running for ever. A
#: single attempt is never cut short by it, because the check is made before an
#: attempt starts and never during one. Roughly an hour, which is far longer
#: than the slowest legitimate chain and short enough that a task retrying a
#: permanent failure cannot bill indefinitely.
DEFAULT_BUDGET_SECONDS = 3300.0


class PermanentTaskFailure(Exception):
    """This will fail the same way every time. Do not spend a retry on it.

    Raised by a task that has determined the input is wrong rather than the
    world being briefly unavailable: a missing SWOT artifact, a row that
    no longer exists, an argument that does not parse. The distinction matters
    because the alternative is five backoff attempts against a condition no
    amount of waiting changes, producing five log lines that read like a bug in
    the task rather than a fact about its input.
    """


@dataclass
class TaskContext:
    """Handed to a task declared with `bind=True`, mirroring Celery's `self`.

    Carries the two things a task legitimately needs to know about its own
    execution: which run it is (so it can publish progress a screen is polling)
    and which attempt this is (so a delivery task can audit the terminal
    failure rather than the intermediate ones).
    """

    run_id: str
    name: str
    attempt: int
    max_attempts: int

    @property
    def is_final_attempt(self) -> bool:
        return self.attempt >= self.max_attempts

    def publish(self, payload: dict[str, Any]) -> None:
        """Record in-flight progress for the polling endpoint.

        Never raises into the work. A progress display that can fail the task
        it is describing is a strictly worse trade than one that goes blank,
        which is the rule `matching_progress.Progress` already states.

        CALLED FROM BOTH SIDES OF A LOOP. A sync task body calls it with no
        loop running, and `asyncio.run` is right. `run_matching` calls it from
        INSIDE its own `asyncio.run`, where a second `asyncio.run` raises; that
        raise was swallowed here, the coroutine was never awaited, and no
        progress payload ever reached the job page while a run was in flight.
        Inside a running loop the write is scheduled on THAT loop (the status
        client is loop-bound), and a strong reference is held until it is done,
        because the loop keeps only a weak one.
        """
        write = status.write(self.run_id, status.STATE_PROGRESS, payload)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        try:
            if loop is None:
                _run(write)
            else:
                pending = loop.create_task(write)
                _PUBLISHES_IN_FLIGHT.add(pending)
                pending.add_done_callback(_PUBLISHES_IN_FLIGHT.discard)
        except Exception:  # noqa: BLE001 -- see above
            write.close()
            logger.warning("taskrun.publish_failed run_id=%s", self.run_id, exc_info=True)

    def stage(
        self,
        stage: str,
        *,
        done: int | None = None,
        total: int | None = None,
        waiting_external: bool = False,
    ) -> None:
        """Record the CURRENT STAGE in the job ledger (the durable status).

        `done`/`total` only when the step is measurable; leave them out and the
        screen shows no percentage rather than an invented one. Pass
        `waiting_external=True` while waiting on a provider. Never raises into
        the work, and is a no-op for an untracked run.
        """
        if not self.run_id:
            return

        async def _go() -> None:
            try:
                async with worker_session() as session:
                    await jobs.set_stage(
                        session, self.run_id, stage,
                        done=done, total=total, waiting_external=waiting_external,
                    )
            except Exception as exc:  # noqa: BLE001 -- never fails the work
                logger.warning(
                    "job.stage_failed run_id=%s error=%s", self.run_id, type(exc).__name__
                )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _run(_go())
            return
        pending = loop.create_task(_go())
        _PUBLISHES_IN_FLIGHT.add(pending)
        pending.add_done_callback(_PUBLISHES_IN_FLIGHT.discard)


#: Progress writes scheduled on a running loop, held until they finish: the
#: event loop keeps only a weak reference to a task, so an unreferenced write
#: can be collected before it runs.
_PUBLISHES_IN_FLIGHT: set[asyncio.Task] = set()


# -- Session helper ----------------------------------------------------------


@asynccontextmanager
async def worker_session():
    """Fresh engine + session for one task run, disposed on exit.

    A fresh engine per run rather than a module-level one: a Lambda execution
    environment is frozen between invocations, and a pooled asyncpg connection
    does not survive the freeze. It comes back looking healthy and fails on
    first use, which is the worst version of a broken connection because it
    fails inside the task rather than at connect time.

    THE BYPASS PATH. The session runs with `app.bypass_rls = 'on'`, the same
    escape hatch the RLS policies define for the audit-logged super-admin path.
    Only a task registered `rls="bypass"` opens it, and its registration says
    why in words (`registry.task`): a sweep over every tenant, a read of a
    tenant-free table such as `candidates` or `profiles`, or a body not yet
    proven under `tenant_worker_session`. A task that belongs to one tenant
    and has been proven opens `tenant_worker_session` instead.
    """
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            # false => session-level (not transaction-local): survives commits.
            await session.execute(
                text("SELECT set_config('app.bypass_rls', 'on', false)")
            )
            yield session
    finally:
        await engine.dispose()


@asynccontextmanager
async def tenant_worker_session(tenant_id: uuid.UUID):
    """Fresh engine + session for one task run, confined to ONE tenant by RLS.

    The tenant twin of `worker_session`, for tasks registered `rls="tenant"`.
    The Postgres policy, not the task's WHERE clauses, is what keeps the run
    inside the customer it was dispatched for (claude.md rule 3).

    - A fresh engine per run, for the Lambda-freeze reason `worker_session`
      gives, and disposed on exit.
    - The scope is a CONNECTION STARTUP PARAMETER (asyncpg `server_settings`),
      not a statement run after connecting and not the transaction-local
      `SET LOCAL` that `core.db.tenant_scope` uses. Every connection this
      engine ever opens carries it from its first byte, INCLUDING the one
      `pool_pre_ping` opens to replace a connection that died between two of
      the task's commits. A `SET ROLE` / `set_config` issued once on the first
      connection would be missing on that replacement, and under a login role
      that owns the tables (dev, the test database) the replacement would run
      with no policy at all. A startup parameter is also the session DEFAULT,
      so a rollback in the body, a commit, or even `RESET ALL` returns to it
      rather than shedding it. Correct HERE and nowhere else: the engine is
      private to this run and disposed on exit, so nothing set on its
      connections can reach another tenant's work through a shared pool.
    - `role` drops to `postgres_rls_app_role` when one is configured, so the
      policy binds even when the login role owns the tables or bypasses RLS.
      The name is validated as a plain SQL identifier by the same rule the API
      path uses (`core.db._app_role`).
    - `app.bypass_rls` is set to 'off' EXPLICITLY. "off" is the value the
      policies compare against, and saying it costs nothing.
    """
    from app.core.db import _app_role

    tid = uuid.UUID(str(tenant_id))
    scope = {"app.tenant_id": str(tid), "app.bypass_rls": "off"}
    role = _app_role()
    if role is not None:
        scope["role"] = role
    engine = create_async_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        connect_args={"server_settings": scope},
    )
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


#: The ONLY tables `resolve_tenant_id` may read, each by its primary key, each
#: with a NOT NULL `tenant_id` (asserted against `information_schema` by
#: `tests/test_worker_tenant_session.py`). An allowlist rather than a table-name
#: argument, because the name is interpolated into SQL and a map keyed by a
#: Literal cannot be steered by a payload.
TENANT_OWNER_TABLES: dict[str, str] = {
    "link": "job_candidate_links",
    "job": "jobs",
    "email_log": "email_log",
    "purchase": "credit_purchases",
    "support_thread": "support_threads",
}

TenantOwnerKind = Literal["link", "job", "email_log", "purchase", "support_thread"]


async def resolve_tenant_id(kind: TenantOwnerKind, entity_id: Any) -> uuid.UUID | None:
    """The tenant that owns one row, read BEFORE the tenant session opens.

    A task dispatched with an entity id rather than a tenant id has to learn
    the tenant before it can open `tenant_worker_session`, and under RLS it
    cannot read the row to find out. So this is a one-query BYPASS read of
    exactly one column, `tenant_id`, from one allowlisted table by primary key,
    and nothing else of the row ever crosses into the task through it.

    None means the row does not exist, which the caller answers exactly as its
    body already answered a missing row (a skip, logged). It is not a
    fallback: there is no tenant to confine a run over nothing to.
    """
    table = TENANT_OWNER_TABLES[kind]
    row_id = uuid.UUID(str(entity_id))
    async with worker_session() as session:
        value = (
            await session.execute(
                text(f"SELECT tenant_id FROM {table} WHERE id = :id"),
                {"id": row_id},
            )
        ).scalar_one_or_none()
    return None if value is None else uuid.UUID(str(value))


def _run(coro):
    """Run a coroutine to completion from sync task code.

    `asyncio.run` rather than a reused loop: task bodies are synchronous
    functions by design, so the API process never has to import what a worker
    imports, and a loop that outlived one run would carry its engine with it.
    """
    return asyncio.run(coro)


# -- Execution ---------------------------------------------------------------


def _max_attempts(spec: TaskSpec) -> int:
    """The attempt cap, resolved at RUN time rather than import time.

    Delivery keeps its operator knob: `settings.delivery_max_retries` is what
    the Celery task consulted, and reading it here rather than freezing it into
    the registry means changing it is still an environment change rather than a
    deploy.
    """
    if spec.max_attempts_setting:
        configured = getattr(get_settings(), spec.max_attempts_setting, None)
        if isinstance(configured, int) and configured >= 0:
            return configured + 1
    return spec.max_attempts


def _backoff(spec: TaskSpec, attempt: int) -> float:
    return min(spec.backoff_seconds * (2 ** (attempt - 1)), spec.backoff_max_seconds)


def _ledger(fn, *args, **kwargs):
    """Run one `workers/jobs` operation in its own session.

    A ledger outage is logged and reported as None, never raised: the ledger
    describes the work and must not be able to fail it. The cost of that choice
    is stated where it bites: a claim that could not be made lets the task run
    UNTRACKED, so duplicate-delivery protection is absent for that run only.
    """

    async def _go():
        async with worker_session() as session:
            return await fn(session, *args, **kwargs)

    try:
        return _run(_go())
    except Exception as exc:  # noqa: BLE001 -- see docstring
        logger.warning("job.ledger_unavailable op=%s error=%s", fn.__name__, type(exc).__name__)
        return None


class _Heartbeat:
    """Proves a long run is alive, so the sweep can tell slow from lost.

    A daemon thread writing `heartbeat_at` every `jobs.HEARTBEAT_INTERVAL`
    seconds. A task that publishes no progress for ten minutes (an ffmpeg
    transcode) would otherwise look identical to a killed worker.
    """

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name=f"heartbeat-{run_id[:8]}", daemon=True
        )

    def _loop(self) -> None:
        while not self._stop.wait(jobs.HEARTBEAT_INTERVAL):
            _ledger(jobs.heartbeat, self._run_id)

    def __enter__(self) -> "_Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()


def run_task(
    body: dict[str, Any],
    *,
    remaining_seconds: Callable[[], float] | None = None,
) -> Any:
    """Execute one dispatched task. The only path to a task body.

    `body` is the wire format `dispatch.payload_for` produces.
    `remaining_seconds` is how much of the platform's budget is left, which the
    Lambda entry point wires to `context.get_remaining_time_in_millis`.
    """
    name = body.get("task")
    if not isinstance(name, str) or not name:
        raise PermanentTaskFailure("dispatched payload has no task name")
    run_id = str(body.get("run_id") or "")
    args = list(body.get("args") or [])
    kwargs = dict(body.get("kwargs") or {})

    spec = resolve(name)
    cap = _max_attempts(spec)

    # THE DUPLICATE-EXECUTION GUARD. Lambda async delivery is at-least-once and
    # the sweep may re-drive a slow start, so one run id can arrive twice. Only
    # the delivery that wins the ledger claim executes; a run the ledger has
    # already finished (or that another worker is heartbeating) is skipped.
    # Scheduled sweeps carry no run id and are never tracked.
    verdict = _ledger(jobs.claim, run_id) if run_id else None
    if verdict == jobs.DUPLICATE:
        logger.warning("taskrun.duplicate_skipped task=%s run_id=%s", name, run_id)
        return None
    tracked = verdict == jobs.CLAIMED
    heartbeat = _Heartbeat(run_id) if tracked else None
    if heartbeat is not None:
        heartbeat.__enter__()
    try:
        return _execute(spec, name, run_id, args, kwargs, cap, remaining_seconds, tracked)
    finally:
        if heartbeat is not None:
            heartbeat.__exit__(None, None, None)


def _execute(spec, name, run_id, args, kwargs, cap, remaining_seconds, tracked):
    started = time.monotonic()
    longest = 0.0
    last_error: BaseException | None = None

    for attempt in range(1, cap + 1):
        ctx = TaskContext(run_id=run_id, name=name, attempt=attempt, max_attempts=cap)
        call_args = (ctx, *args) if spec.bind else tuple(args)
        attempt_started = time.monotonic()
        try:
            result = spec.fn(*call_args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 -- classified immediately below
            longest = max(longest, time.monotonic() - attempt_started)
            last_error = exc
            retryable = isinstance(exc, spec.retry_on) and not isinstance(
                exc, PermanentTaskFailure
            )
            if not retryable or attempt >= cap:
                break
            delay = _backoff(spec, attempt)
            budget = (
                remaining_seconds()
                if remaining_seconds is not None
                else DEFAULT_BUDGET_SECONDS - (time.monotonic() - started)
            )
            # Predictive, not observational. See the module docstring.
            if delay + longest >= budget:
                logger.warning(
                    "taskrun.retry_abandoned task=%s run_id=%s attempt=%d "
                    "delay=%.1f longest=%.1f budget=%.1f",
                    name, run_id, attempt, delay, longest, budget,
                )
                break
            logger.warning(
                "taskrun.retrying task=%s run_id=%s attempt=%d/%d error=%s delay=%.1f",
                name, run_id, attempt, cap, type(exc).__name__, delay,
            )
            time.sleep(delay)
            continue

        elapsed = time.monotonic() - attempt_started
        payload = result if isinstance(result, dict) else {"result": result}
        if tracked:
            # The ledger first: it is the terminal truth, Redis only a cache.
            _ledger(jobs.complete, run_id)
        if run_id:
            _run(status.write(run_id, status.STATE_SUCCESS, payload))
        logger.info(
            "taskrun.succeeded task=%s run_id=%s attempt=%d elapsed=%.1fs",
            name, run_id, attempt, elapsed,
        )
        return result

    assert last_error is not None  # the loop only breaks after an exception
    if tracked:
        _ledger(
            jobs.fail,
            run_id,
            failure_class=(
                jobs.FAIL_PERMANENT
                if isinstance(last_error, PermanentTaskFailure)
                else jobs.FAIL_WORKER_ERROR
            ),
            detail=type(last_error).__name__,
        )
    if run_id:
        # The exception CLASS NAME, never its message. A message can quote a
        # row, and this payload is read by a recruiter's browser.
        _run(
            status.write(
                run_id,
                status.STATE_FAILURE,
                {},
                error=type(last_error).__name__,
            )
        )
    logger.error(
        "taskrun.failed task=%s run_id=%s error=%s",
        name, run_id, type(last_error).__name__,
        exc_info=last_error,
    )
    raise last_error
