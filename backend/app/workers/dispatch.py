"""Hand a background task to whatever is going to run it.

This is the one replacement for `celery_app.send_task`. Every call site that
used to publish a message to a Redis broker now calls `dispatch(...)` and gets
back a run id it can hand to a client for polling.

THE THREE BACKENDS ARE THREE REAL DEPLOYMENTS, NOT A FALLBACK CHAIN
-------------------------------------------------------------------
`aws` invokes Lambda. `local` runs the task in a thread in this process, which
is what the docker-compose stack does because there is no Lambda on a laptop.
`record` accepts the dispatch, runs nothing, and remembers it, which is what
the test suite does because executing a task would make every route test depend
on a model provider.

Nothing falls back to anything. The backend is chosen once from configuration
and a failure inside it RAISES, so a caller that wants best-effort behaviour
wraps the call itself. That is deliberate and it is the behaviour the existing
call sites were already written against: several of them catch the enqueue
failure and degrade visibly (a skipped outreach recipient says it could not be
queued), and a dispatcher that swallowed the error would turn those into
silence.

WHY LONG WORK IS STILL ADDRESSED THROUGH A LAMBDA
-------------------------------------------------
`Route.ECS` does not call `ecs:RunTask` from here. It invokes
`readypick-assessment-trigger`, whose entire job is that one call. The reason is
least privilege: running a Fargate task means holding `iam:PassRole` over the
assessment agent's task role, and that is a privilege-escalation primitive,
since anything that can pass a role can run code as it. The API service has no
business holding it. A 128MB function that does nothing else can.

TIMEOUTS ARE EXPLICIT, BECAUSE THIS EXACT MISTAKE HAS BEEN MADE HERE BEFORE
--------------------------------------------------------------------------
Publishing to Redis had no timeout by default, so an unreachable broker did not
fail, it HUNG, which silently defeated every `try/except` around an enqueue,
because nothing was ever raised for the handler to catch. It was observed as a
management job that found thirty files, blocked forever on its first enqueue,
and was killed at the 900-second ceiling having written nothing.

botocore has the same shape of default (a 60-second connect timeout, and
retries on top). The client below sets both timeouts short and caps the
attempts, so a Lambda control-plane problem costs a request a few seconds and
an exception the caller can handle, never a hung worker.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

from app.core.config import get_settings
from app.workers.registry import Route, TaskSpec, resolve

logger = logging.getLogger(__name__)

BACKEND_AWS = "aws"
BACKEND_LOCAL = "local"
BACKEND_RECORD = "record"

#: The generic worker Lambda. One function runs every `Route.LAMBDA` task,
#: because they share an image, a role and a set of secrets, and one function
#: per task would multiply the deployment surface by thirty for no isolation
#: anybody asked for. The three agent functions are separate for the opposite
#: reason: they are request/response and sized differently.
WORKER_FUNCTION = "readypick-task-worker"

#: Invoked for `Route.ECS`. Calls `ecs:RunTask` and returns. See the docstring.
TRIGGER_FUNCTION = "readypick-assessment-trigger"


class DispatchError(RuntimeError):
    """The task could not be handed off. Raised, never swallowed."""


@dataclass(frozen=True)
class TaskHandle:
    """What the caller gets back.

    `id` is generated HERE rather than returned by the platform, so a request
    handler has the polling id before the invoke completes and an asynchronous
    invoke does not have to be waited on to learn it. Celery generated its task
    ids client-side for the same reason, so the contract the frontend already
    polls against is unchanged.
    """

    id: str
    name: str
    route: Route


@dataclass(frozen=True)
class RecordedDispatch:
    id: str
    name: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


_recorded: list[RecordedDispatch] = []
_recorded_lock = threading.Lock()
_client: Any = None


def backend() -> str:
    """The configured backend, validated once per call.

    `record` is refused in production outright. It is the mode that accepts
    work and does not do it, which is exactly the failure this product has been
    bitten by, so it may only ever be reached deliberately and never by a
    misread environment variable on a live service.
    """
    settings = get_settings()
    chosen = (settings.task_dispatch_backend or BACKEND_AWS).strip().lower()
    if chosen not in {BACKEND_AWS, BACKEND_LOCAL, BACKEND_RECORD}:
        raise DispatchError(f"unknown task dispatch backend: {chosen!r}")
    if chosen == BACKEND_RECORD and settings.is_production:
        raise DispatchError(
            "task_dispatch_backend=record accepts work without running it and "
            "is refused in production"
        )
    return chosen


def _lambda_client():
    """A boto3 Lambda client with bounded timeouts. See the module docstring."""
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config

        settings = get_settings()
        _client = boto3.client(
            "lambda",
            region_name=settings.aws_region,
            config=Config(
                connect_timeout=5,
                read_timeout=10,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
    return _client


def reset_client() -> None:
    """Drop the cached client. Used by tests and after a credential change."""
    global _client
    _client = None


def payload_for(
    run_id: str,
    spec: TaskSpec,
    args: Sequence[Any],
    kwargs: dict,
    dispatch_attempt: int = 0,
) -> dict:
    """The one wire format. Both entry points parse exactly this shape.

    Arguments are JSON, which is what Celery's `task_serializer="json"` already
    required, so nothing that was dispatchable before has become undispatchable.
    `dispatch_attempt` counts the sweep's re-drives of this run. The trigger
    Lambda folds it into the `RunTask` client token, so a platform retry of one
    invoke cannot start two tasks while a deliberate re-drive can start one.
    """
    return {
        "run_id": run_id,
        "task": spec.name,
        "args": list(args),
        "kwargs": dict(kwargs),
        "dispatch_attempt": dispatch_attempt,
    }


def dispatch(
    name: str,
    args: Sequence[Any] | None = None,
    kwargs: dict[str, Any] | None = None,
) -> TaskHandle:
    """Hand `name` off to run, and return its handle.

    The signature mirrors `celery_app.send_task(name, args=[...])` so the call
    sites read the same and a reviewer can see that only the transport changed.
    """
    spec = resolve(name)
    return _send(spec, str(uuid.uuid4()), tuple(args or ()), dict(kwargs or {}))


def _prepare(
    name: str, args: Sequence[Any] | None, kwargs: dict[str, Any] | None
) -> tuple[TaskSpec, str, tuple[Any, ...], dict[str, Any]]:
    """Everything that can fail for a PROGRAMMING reason, checked in-request:
    the task name resolves, the backend is valid, the arguments are JSON."""
    spec = resolve(name)
    backend()
    args = tuple(args or ())
    kwargs = dict(kwargs or {})
    run_id = str(uuid.uuid4())
    json.dumps(payload_for(run_id, spec, args, kwargs))
    return spec, run_id, args, kwargs


def _ledger_row(spec: TaskSpec, run_id: str, args, kwargs, **meta):
    from app.workers import jobs

    return jobs.new_row(
        run_id, kind=spec.name, route=spec.route.value, args=args, kwargs=kwargs, **meta
    )


def _send_when_committed(session: Any, spec: TaskSpec, run_id: str, args, kwargs) -> None:
    from app.core.after_commit import on_commit

    on_commit(
        session,
        lambda: _send(spec, run_id, args, kwargs),
        label=f"dispatch task={spec.name} run_id={run_id}",
    )


def dispatch_after_commit(
    session: Any,
    name: str,
    *,
    args: Sequence[Any] | None = None,
    kwargs: dict[str, Any] | None = None,
    resource_type: str | None = None,
    resource_id: Any = None,
    required_capability: str | None = None,
    owner_user_id: uuid.UUID | None = None,
    idempotent: bool = False,
) -> TaskHandle:
    """Hand `name` off to run once `session`'s transaction COMMITS.

    THE OUTBOX. A `background_jobs` row is added to `session` here, so it
    commits or rolls back WITH the caller's own writes. The invoke after the
    commit is only the fast path: if it is lost (Lambda control plane down, a
    crash between commit and invoke, an ECS refusal an async invoke cannot
    report) the row is still `queued` and `pickready.sweep_background_jobs`
    re-drives it. A dispatched job is therefore never silent.

    The handle, with its client-side id, is returned NOW, so a route can put
    the polling id in its response exactly as it does with `dispatch`. The
    invoke itself happens from SQLAlchemy's `after_commit`
    (`core/after_commit.on_commit`):

    - A rolled-back transaction dispatches NOTHING and records NOTHING.
    - The task can never start before the rows it reads are visible.
    - Everything that can fail for a PROGRAMMING reason is checked here,
      inside the request, where raising is correct (`_prepare`).
    - A `DispatchError` from the invoke is LOGGED at ERROR, never raised out of
      `commit()`: the write is durable and the outbox row will be re-driven.

    `resource_type`/`resource_id` name what the job is about, so a screen can
    ask for it by resource. `required_capability` is what a reader must hold
    (checked through RBAC per request). `idempotent=True` declares the task
    safe to run again after a lost worker; only then does the sweep re-drive a
    RUNNING job or may a person retry a failed one.

    Never call this and then raise: the raise rolls the transaction back and
    the dispatch goes with it. Return a state the client can act on instead.
    """
    spec, run_id, args, kwargs = _prepare(name, args, kwargs)
    session.add(
        _ledger_row(
            spec, run_id, args, kwargs,
            resource_type=resource_type, resource_id=resource_id,
            required_capability=required_capability,
            owner_user_id=owner_user_id, idempotent=idempotent,
        )
    )
    _send_when_committed(session, spec, run_id, args, kwargs)
    return TaskHandle(id=run_id, name=spec.name, route=spec.route)


async def dispatch_once_after_commit(
    session: Any,
    name: str,
    *,
    dedupe_key: str,
    args: Sequence[Any] | None = None,
    kwargs: dict[str, Any] | None = None,
    resource_type: str | None = None,
    resource_id: Any = None,
    required_capability: str | None = None,
    owner_user_id: uuid.UUID | None = None,
    idempotent: bool = False,
) -> TaskHandle:
    """`dispatch_after_commit`, except at most ONE active job per `dedupe_key`.

    A sweep that re-enqueues a stale resource every hour would otherwise stack
    a new queued job per pass while the first still waits for capacity. The
    unique partial index over active `dedupe_key` rows makes the second call a
    no-op that returns the existing job's handle. Name the work in the key, for
    example `process_candidate_project:<project id>`.
    """
    from sqlalchemy import select, text
    from sqlalchemy.dialects.postgresql import insert

    from app.models.background_job import BackgroundJob

    spec, run_id, args, kwargs = _prepare(name, args, kwargs)
    row = _ledger_row(
        spec, run_id, args, kwargs,
        resource_type=resource_type, resource_id=resource_id,
        required_capability=required_capability,
        owner_user_id=owner_user_id, idempotent=idempotent, dedupe_key=dedupe_key,
    )
    values = {
        column.name: getattr(row, column.name)
        for column in BackgroundJob.__table__.columns
        if getattr(row, column.name) is not None
    }
    result = await session.execute(
        insert(BackgroundJob)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=["dedupe_key"],
            index_where=text(
                "dedupe_key IS NOT NULL AND state NOT IN ('succeeded', 'failed')"
            ),
        )
    )
    if result.rowcount:
        _send_when_committed(session, spec, run_id, args, kwargs)
        return TaskHandle(id=run_id, name=spec.name, route=spec.route)
    existing = (
        await session.execute(
            select(BackgroundJob.id).where(
                BackgroundJob.dedupe_key == dedupe_key,
                BackgroundJob.state.notin_(("succeeded", "failed")),
            )
        )
    ).scalar_one_or_none()
    logger.info("dispatch.deduplicated task=%s dedupe_key=%s", spec.name, dedupe_key)
    return TaskHandle(id=str(existing or run_id), name=spec.name, route=spec.route)


async def redrive(job: Any, attempt: int):
    """Start (again) the run a `background_jobs` row describes. For the sweep.

    Unlike the fast path this WAITS for the answer, because the sweep is the
    thing that must learn why a start failed:

    - `Route.ECS` invokes the trigger synchronously. A `RunTask` refusal comes
      back as a function error whose message carries a `[capacity]` or
      `[refused]` tag (`lambda/assessment_trigger`), so quota exhaustion is
      recorded as `capacity_blocked` instead of being inferred from silence.
    - `Route.LAMBDA` is an async invoke; `202` is acceptance, and the worker's
      own claim is what makes the job `running`.

    Returns a `jobs.Outcome`; a refusal is an outcome, not an exception.
    """
    import asyncio

    from app.workers import jobs

    spec = resolve(job.kind)
    payload = job.payload or {}
    body = payload_for(
        str(job.id), spec, payload.get("args") or [], payload.get("kwargs") or {}, attempt
    )
    mode = backend()
    if mode == BACKEND_RECORD:
        with _recorded_lock:
            _recorded.append(
                RecordedDispatch(
                    id=str(job.id), name=spec.name,
                    args=tuple(body["args"]), kwargs=body["kwargs"],
                )
            )
        return jobs.Outcome(True)
    if mode == BACKEND_LOCAL:
        _run_locally(body)
        return jobs.Outcome(True)

    ecs = spec.route is Route.ECS
    function = TRIGGER_FUNCTION if ecs else WORKER_FUNCTION

    def _invoke():
        return _lambda_client().invoke(
            FunctionName=function,
            InvocationType="RequestResponse" if ecs else "Event",
            Payload=json.dumps(body).encode("utf-8"),
        )

    try:
        response = await asyncio.to_thread(_invoke)
    except Exception as exc:  # noqa: BLE001 -- reported as an outcome, not raised
        logger.warning(
            "dispatch.redrive_invoke_failed task=%s function=%s error=%s",
            spec.name, function, type(exc).__name__,
        )
        return jobs.Outcome(False, jobs.FAIL_DISPATCH, type(exc).__name__)
    if response.get("FunctionError"):
        message = ""
        try:
            message = str(json.loads(response["Payload"].read()).get("errorMessage", ""))
        except (ValueError, AttributeError):
            pass
        capacity = "[capacity]" in message
        logger.warning(
            "dispatch.redrive_refused task=%s run_id=%s class=%s",
            spec.name, job.id, "capacity" if capacity else "refused",
        )
        return jobs.Outcome(
            False,
            jobs.FAIL_CAPACITY if capacity else jobs.FAIL_DISPATCH,
            "run_task_capacity" if capacity else "run_task_refused",
        )
    status_code = int(response.get("StatusCode") or 0)
    if status_code not in (200, 202):
        return jobs.Outcome(False, jobs.FAIL_DISPATCH, f"status_{status_code}")
    return jobs.Outcome(True)


def requeue_after_commit(session: Any, job: Any) -> None:
    """Send an existing, just-requeued job once `session` commits."""
    spec = resolve(job.kind)
    payload = job.payload or {}
    _send_when_committed(
        session, spec, str(job.id), tuple(payload.get("args") or ()),
        dict(payload.get("kwargs") or {}),
    )


def _send(
    spec: TaskSpec,
    run_id: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> TaskHandle:
    """The transport. Shared by `dispatch` and `dispatch_after_commit` so the
    two can never disagree about what a dispatch IS."""
    body = payload_for(run_id, spec, args, kwargs)

    mode = backend()
    if mode == BACKEND_RECORD:
        with _recorded_lock:
            _recorded.append(
                RecordedDispatch(id=run_id, name=spec.name, args=args, kwargs=kwargs)
            )
        logger.info("dispatch.recorded task=%s run_id=%s", spec.name, run_id)
        return TaskHandle(id=run_id, name=spec.name, route=spec.route)

    if mode == BACKEND_LOCAL:
        _run_locally(body)
        return TaskHandle(id=run_id, name=spec.name, route=spec.route)

    function = TRIGGER_FUNCTION if spec.route is Route.ECS else WORKER_FUNCTION
    try:
        response = _lambda_client().invoke(
            FunctionName=function,
            InvocationType="Event",
            Payload=json.dumps(body).encode("utf-8"),
        )
    except Exception as exc:  # noqa: BLE001 -- re-raised as DispatchError below
        logger.exception(
            "dispatch.invoke_failed task=%s function=%s", spec.name, function
        )
        raise DispatchError(
            f"could not dispatch {spec.name}: {type(exc).__name__}"
        ) from exc

    accepted = int(response.get("StatusCode") or 0)
    if accepted != 202:
        # An asynchronous invoke answers 202 and nothing else. Anything here is
        # a refusal wearing a success shape, and treating it as accepted is how
        # work disappears behind a log line that says it was queued.
        raise DispatchError(
            f"{spec.name}: lambda {function} answered {accepted}, expected 202"
        )
    logger.info(
        "dispatch.invoked task=%s run_id=%s function=%s route=%s",
        spec.name,
        run_id,
        function,
        spec.route.value,
    )
    return TaskHandle(id=run_id, name=spec.name, route=spec.route)


def _run_locally(body: dict) -> None:
    """Run the task in a daemon thread, for the docker-compose stack.

    A thread rather than an inline call because the call sites are request
    handlers: running a matching pass inline would hold the request open for
    minutes and change the behaviour being developed against. Errors surface
    where they would in a deployed worker, in the log, because `run_task`
    records the failure itself and never re-raises past this boundary.
    """
    from app.workers.runtime import run_task

    def _target() -> None:
        try:
            run_task(body)
        except Exception:  # noqa: BLE001 -- a thread's exception has nowhere to go
            logger.exception("dispatch.local_run_failed task=%s", body.get("task"))

    threading.Thread(
        target=_target, name=f"task-{body.get('task')}", daemon=True
    ).start()


# -- Test-mode inspection ----------------------------------------------------
# Only meaningful under `record`. Exposed so a test can assert WHAT was
# dispatched, which is a stronger check than the monkeypatched broker it
# replaces: patching `send_task` with a lambda proved only that the call site
# ran, never that it named a task that exists.


def recorded() -> list[RecordedDispatch]:
    with _recorded_lock:
        return list(_recorded)


def recorded_names() -> list[str]:
    return [item.name for item in recorded()]


def clear_recorded() -> None:
    with _recorded_lock:
        _recorded.clear()
