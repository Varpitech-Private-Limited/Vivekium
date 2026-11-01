# Background work

**Status:** current. **Reviewed:** 2026-11-01. How background work is dispatched, routed, retried and scheduled, and what a person needs to know before adding a task, adding a sweep, or debugging one that did not run. Rules in short form: [../engineering/backend.md](../engineering/backend.md).

## 1. Shape

Short work is billed per invocation (Lambda); long work is one on-demand Fargate task per dispatch. There is no standing worker pool and no scheduler process to lose. A task's name, implementation, destination and retry policy are one declaration. The product's own economics have this shape: an assessment either is running or it is not.

## 2. The one way to start background work

```python
from app.workers.dispatch import dispatch

handle = dispatch("pickready.run_matching", args=[str(job.id)])
handle.id  # the polling id, available immediately
```

`handle.id` is generated client-side, so a request handler has the polling id before the invoke completes. The frontend's `task_id` and `task_ids` fields rely on that.

### A task about a row is dispatched after the row is committed

A request that writes the row a task reads uses

```python
from app.workers.dispatch import dispatch_after_commit

handle = dispatch_after_commit(session, "pickready.run_matching", args=[str(job.id)])
```

The handle comes back at once; the invoke runs from SQLAlchemy's `after_commit` through `core/after_commit.on_commit`. A rollback, or a session closed without committing, dispatches nothing. The task name, the backend and the JSON-ness of the arguments are checked at the call; a `DispatchError` from the invoke itself is logged at ERROR after the commit and never raised out of `commit()`. The call also adds a `background_jobs` row to the session (the outbox, section 10), so a lost invoke leaves a `queued` row that `pickready.sweep_background_jobs` re-drives. Optional keywords name the job for readers: `resource_type`, `resource_id`, `required_capability`, `owner_user_id`, and `idempotent=True` when the task is safe to run again after a lost worker. `await dispatch_once_after_commit(session, name, dedupe_key=..., ...)` is the same call with at most one active job per key, for a sweep that would otherwise stack a job per pass. A plain `dispatch()` (no session) writes no ledger row and is untracked. Never call it and then raise: the raise rolls the dispatch back. A callback registered inside a SAVEPOINT that later rolls back still fires on the outer commit, so register after the savepoint. `tests/test_dispatch_after_commit_sweep.py` refuses a new bare `dispatch` in `app/api`, `services/proctoring` and `services/video`; its legacy allowlist is empty. A test inspecting a dispatch inside a transaction it rolls back reads `after_commit.pending_labels(session)`.

### The three backends are three deployments

| `TASK_DISPATCH_BACKEND` | What happens | Where |
|---|---|---|
| `aws` | Invoke Lambda. Short work runs there; long work becomes one on-demand Fargate task. | Pilot, staging, production |
| `local` | Run the task in a daemon thread in this process. | `infra/docker-compose.yml` |
| `record` | Accept the dispatch, run nothing, remember it. | The test suite |

Nothing falls back to anything. The backend is chosen once from configuration, and a failure inside it raises: several call sites degrade visibly on an enqueue failure (a skipped outreach recipient says it could not be queued), and a dispatcher that swallowed the error would turn that into silence. `record` is refused in production by the dispatcher itself, because it accepts work and does not do it.

### Timeouts are explicit

An unreachable endpoint with no timeout does not fail, it hangs, which silently defeats every `try`/`except` around the call. botocore's defaults are a 60-second connect timeout with retries on top. Every client in `app/workers/dispatch.py` and `app/workers/agent_client.py` sets both timeouts short and caps the attempts.

## 3. The route is a cost decision, and it is data

`app/workers/registry.py`:

```python
@task(name="pickready.send_email", route=Route.LAMBDA, rls=...)
@task(name="pickready.run_matching", route=Route.ECS, rls=...)
```

**`Route.LAMBDA`** is work measured in seconds: delivery, resume parsing, the reconciliation sweeps. It runs in `readypick-task-worker`.

**`Route.ECS`** is work measured in minutes with a real ceiling risk: scoring a whole candidate pool, writing a PRISM Report, generating questions, processing a recording. One on-demand Fargate task per dispatch, started by `readypick-assessment-trigger`, which stops when the process exits. Lambda's fifteen-minute ceiling is the hard reason for the split: a task killed at a ceiling leaves half a pool scored with nothing saying so.

Delivery cannot wait behind an LLM chain because the two kinds of work do not share a pool. `backend/tests/test_email_delivery.py` asserts it against the registry rather than a hand-kept list.

**Every task declares its RLS scope** (`rls="tenant"` or `"bypass"`, no default; a bypass carries `rls_reason`), checked by `tests/test_worker_tenant_session.py`.

## 4. Retries have exactly one owner

`runtime.run_task` owns the loop. The platform's own asynchronous retry is set to zero in `aws_lambda_function_event_invoke_config`: three in-process attempts under two platform attempts would be nine sends of one email. A permanently failed invocation is published once to the alarm topic.

- **The loop predicts.** Before attempt N+1 it asks whether that attempt could finish inside the remaining budget, using the longest attempt so far as the estimate and `context.get_remaining_time_in_millis()` as the budget. `elapsed >= deadline` sounds right and is not.
- **The ceiling belongs to the platform** (the Lambda timeout, the Fargate `stopTimeout`); the retry loop lives inside the invocation that gets killed, so a task that outran its budget is not retried.
- **A final failure is re-raised**, so the error metric moves, the alarm fires and the destination publishes.
- Delivery keeps its operator knob: `max_attempts_setting="delivery_max_retries"` is resolved at run time.

## 5. Where a task actually runs

| Entry point | Serves | Packaging |
|---|---|---|
| `app.workers.entrypoints.lambda_worker.lambda_handler` | `readypick-task-worker` | Backend image |
| `app.workers.entrypoints.agents.jd_generation_handler` | `readypick-jd-gen` | Backend image |
| `app.workers.entrypoints.agents.company_profile_handler` | `readypick-company-profile` | Backend image |
| `app.workers.entrypoints.ecs_task` (`docker-entrypoint.sh agent`) | the on-demand Fargate task | Backend image |
| `lambda/assessment_trigger/handler.py` | `readypick-assessment-trigger` | Zip |

All converge on `runtime.run_task`: one implementation of "resolve a name, open a session, run it, retry it, record what happened". The three image functions run the backend image because they import the application (model router, prompt registry, database session); a second artifact with the same code would let an agent and the API disagree about what a prompt says or a grade means. **The trigger is the only zip, and it must stay thirty lines and boto3.** It is the only holder of `iam:PassRole`, a privilege-escalation primitive; the internet-facing API holds none. Its grant names the task definition families (with a revision wildcard), the cluster and the two roles it may pass.

## 6. The two synchronous agents

`readypick-jd-gen` and `readypick-company-profile` produce a draft a recruiter is waiting for, so they are invoked with `InvocationType="RequestResponse"`. JD generation has a 25-second per-attempt and 50-second total model budget; the function moves that wait off the API task. `agent_client` calls the same service function directly under `local` and `record`, so there is one implementation of each agent and it lives in `app/services`. There is no single-candidate resume match function: matching is `pickready.run_matching`, a batch over every candidate linked to a job with stage-by-stage progress, which runs as an on-demand Fargate task.

## 7. The schedule

`app/workers/schedule.py` is the source of truth. Each environment mirrors it as EventBridge Scheduler rules, and `backend/tests/test_schedule_parity.py` reads both and fails on drift. A schedule invokes `readypick-task-worker` with the same payload a dispatch sends; giving the scheduler its own entry point would let it fire something the registry does not have.

The two halves fail differently:

- A rule in Terraform with no entry in Python costs one CloudWatch error every interval, for ever, with nobody reading it.
- **An entry in Python with no rule in Terraform is a sweep that never runs**, and that one is silent by construction: a reconciliation sweep does nothing when there is nothing to repair, so "not running" and "nothing to do" produce the same empty log.

**The sweeps do not run locally.** A laptop has no EventBridge Scheduler: `reconcile_job_setup`, the credit reconciler and the proctoring sweeps do not fire under `infra/docker-compose.yml`. Run one by hand:

```bash
docker compose -f infra/docker-compose.yml exec backend \
  python -c "from app.workers.runtime import run_task; \
             run_task({'task': 'pickready.reconcile_job_setup', \
                       'args': [], 'kwargs': {}, 'run_id': ''})"
```

## 8. Run status

`app/workers/status.py`. **The database decides, Redis only describes** (changed 2026-11-01). A run's state comes from its `background_jobs` row; Redis (six-hour TTL) carries the stage payload and the task's return value that a row should not hold (a leadership draft is text).

- **Ledger row present:** the state is the row's, mapped to `PENDING` (queued, accepted, capacity_blocked, retrying), `PROGRESS` (running, waiting_external), `SUCCESS` or `FAILURE`. Redis supplies the payload only when it agrees about the phase, so a stale `PROGRESS` record never decorates a finished run. A `SUCCESS` whose Redis result expired is `SUCCESS` with `result_expired=True` and an empty payload.
- **No row, Redis record present:** Redis's (an untracked `dispatch()`).
- **Neither:** `UNKNOWN`, not `PENDING`. Nothing in the system says that run is waiting, so nothing claims it is. `UNKNOWN` is not terminal, so pollers keep polling as they did, and a screen can say "status unavailable".
- **A `FAILURE` records the exception class name and an empty payload.** A message can quote a row, and this payload is read by a recruiter's browser.
- **Reading the outreach state needs both halves.** `send_email` returns `{"status": "failed"}` for a permanent failure it deliberately did not retry: a run that succeeded and an email that did not arrive.

The permanent record of what happened is elsewhere: `email_log` for delivery, `audit_log` for the trigger, and the rows the task itself wrote.

## 9. Redis and the health probe

| What Redis carries | What losing it costs |
|---|---|
| The proctoring warning counter | `proctoring/gate` answers 503 rather than silently not warning, so every assessment turn is refused |
| Auth sessions | 503, never fail-open |
| The rate limiter | Nothing: it fails open by design |
| The run-status record | The stage payload and a task's return value; the state itself is read from `background_jobs` |
| The caches | A slower read |

`/health` probes Redis as well as the database, so a task that has lost it leaves the target group instead of serving assessments it cannot monitor. `maxmemory-policy` stays `noeviction`: LRU would evict a live assessment's warning counter and silently reset a candidate's warnings to zero, and evict the run-status record a recruiter is watching.

## 10. The job ledger and the outbox

**Status:** current for the ledger, the sweep, the status route and the component. **Decided, pending:** wiring each long task's own endpoint to it (project evidence, PRISM, proctoring report, video, transcription), and recording ECS task STOPPED reasons (section 10.6). Neither is described as working below.

`background_jobs` (migration `0138_background_jobs`, `models/background_job.py`, every transition in `workers/jobs.py`) is one row per tracked dispatch. It is the outbox and the job status in one table. Domain tables keep their own durable state (`candidate_projects.status`, the recording row); the ledger records the EXECUTION of the task that moves them.

### 10.1 States

| State | Meaning | Who sets it |
|---|---|---|
| `queued` | Written in the dispatching transaction; no worker has claimed it | `dispatch_after_commit` |
| `accepted` | The sweep re-drove it and the platform accepted the invoke (Lambda 202, or `RunTask` placed a task). Acceptance only | sweep |
| `capacity_blocked` | `RunTask` refused for vCPU quota or memory. Retried with backoff for at most 24 hours | sweep |
| `running`, `waiting_external` | A worker claimed it and heartbeats every 30 s | `run_task` |
| `retrying` | A worker went silent and an idempotent job was re-driven | sweep |
| `succeeded`, `failed` | Terminal | worker, sweep |

**A Lambda `202` is acceptance, never success.** A job is `running` only after the worker's own atomic claim, and `succeeded` only after `jobs.complete`.

### 10.2 Failure classes (`failure_class`)

`capacity` (retrying), `capacity_exhausted` (24 hours of refusals), `dispatch` (retrying), `dispatch_exhausted` (8 re-drives), `worker_error` (the task raised after its in-process retries), `permanent` (`PermanentTaskFailure`), `worker_lost` (no heartbeat for 10 minutes and the job was not idempotent, or had already run 3 times), `unknown_task`. `failure_detail` is a code or an exception class name, never a message.

### 10.3 Bounds, all deterministic

A `queued` row is due 120 seconds after it was written (the fast path normally starts it in seconds). An `accepted` or `retrying` row not claimed is due after 300 seconds (Lambda) or 900 seconds (Fargate). A refused or failed re-drive backs off 2, 4, 8 ... minutes, capped at one hour. At most 8 re-drives and 24 hours of capacity refusals; then `failed` with a stated class. A RUNNING job is re-driven only when dispatched `idempotent=True`; otherwise it is failed as `worker_lost` rather than risk running twice. Terminal rows are purged after 30 days; a successful row's arguments are cleared at once.

### 10.4 Duplicate execution

Lambda async delivery is at-least-once, the sweep may re-drive a slow start, and the platform retries a failed invoke. `jobs.claim` is one conditional `UPDATE`: of any number of deliveries of one run id, exactly one becomes the worker. The rest are logged `taskrun.duplicate_skipped` and do nothing, and a finished run delivered again does nothing. The trigger Lambda also passes a `RunTask` `clientToken` of `<run id>:<re-drive number>`, so a platform retry of one invoke cannot start two tasks. A ledger outage at claim time lets the task run untracked (logged `job.ledger_unavailable`), which removes the guard for that run only.

### 10.5 The sweep

`pickready.sweep_background_jobs`, every five minutes, in `schedule.py` and all three environments' Terraform. It leases a row (pushes `next_retry_at` out two minutes), re-drives outside any lock with `dispatch.redrive`, and records the outcome. For `Route.ECS` it invokes the trigger SYNCHRONOUSLY: the trigger tags a refusal `[capacity]` or `[refused]` in its error message, which is how a quota refusal becomes `capacity_blocked` instead of an unread asynchronous failure.

### 10.6 Reading it

- `GET /api/v1/job-status/resource/{resource_type}/{resource_id}`, `GET /api/v1/job-status/{job_id}`, `POST /api/v1/job-status/{job_id}/retry` (`api/job_status.py`). Rows are tenant rows under RLS; each names a `required_capability` checked through RBAC per request (none named means only the user who started it); every refusal is a 404. A candidate-owned job has no tenant and is not readable here: the domain endpoint that has authorised its own resource calls `jobs.latest` and `jobs.public_view`.
- `percent` exists only when a task reported a measurable `done` and `total` through `ctx.stage(...)`. `estimate` exists only while running and only when at least 5 of the last 20 finished runs of the same kind support a middle-half range. Otherwise the screen says "Time estimate unavailable".
- `frontend/components/job-status.tsx` (`JobStatus`, `useJobStatus`) renders it: stage, "Last updated", backoff polling from 3 s to 30 s, paused while the tab is hidden, stopped at a terminal state, and a Try again button only when the server says the job can be retried.
- A `bind=True` task reports its stage with `ctx.stage("Reading the repository", done=2, total=5)`. It does nothing for an untracked run.

**Decided, pending:** the ECS `Task State Change` (STOPPED) event is not yet recorded on the row. Today a task that dies without reporting (out of memory, killed) is detected by its silent heartbeat and recorded as `worker_lost`; the stopped reason and exit code are only in the cluster. Recording them needs an EventBridge rule on the cluster's task-state events invoking a registered task, which is an infrastructure change not made here. The trigger already sets `startedBy` to the run id, so the event names the row it belongs to.

### 10.7 Debugging a job that did not finish

1. `GET /job-status/{job_id}` (or the row in `background_jobs`): the state says where it stopped.
2. `queued` for more than seven minutes: the sweep is not running. Check the `readypick-sweep-background-jobs` schedule and the task-worker Lambda logs.
3. `capacity_blocked`: the Fargate vCPU quota. See the runbook in section 10.8.
4. `accepted` or `retrying` that never becomes `running`: the agent container exited before claiming. Read the `/ecs/readypick-<env>/agent` log group for the run id; the task's `startedBy` is the run id.
5. `failed` with `worker_error`: the log line `taskrun.failed task=... run_id=...` has the class and traceback.

### 10.8 Runbook: workers will not start

Symptom: jobs sit `capacity_blocked`, the `assessment-trigger-error-rate` and `agent-task-failures` alarms are in ALARM, and `trigger.refused class=capacity` appears in `/aws/lambda/readypick-assessment-trigger`. Cause: the Fargate On-Demand vCPU quota (L-3032A538) is lower than the standing services plus one agent task (1 vCPU). Read-only checks: `aws service-quotas get-service-quota --service-code fargate --quota-code L-3032A538`, `aws ecs describe-services`, `aws ecs list-tasks --desired-status STOPPED`. Options, cheapest first: request a quota increase (no charge until used); reduce the agent task size; keep the analysis service at zero. See `docs/verification/PRODUCTION_DEFECTS_2026-11-01.md` for the 2026-11-01 numbers. Nothing here applies infrastructure; the owner approves any change.
