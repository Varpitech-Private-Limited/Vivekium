# Production defects: traceability matrix, 2026-11-01

**Status:** measurement (a dated record, not a specification). Branch `fix/production-defects` from `main` at `b93dec77`. Nothing in this file is deployed. Every cell marked **RUNTIME** was read from the live AWS account on 2026-11-01 with the read-only `admin` profile in `ap-south-2` (cluster `readypick-pilot`); every cell marked **CODE** is a finding from the working tree. A **hypothesis** is labelled as one. Identities, account ids, secrets and tokens are omitted.

## 1. Live state recorded before any change (RUNTIME)

| Item | Observed |
|---|---|
| API service | task definition `readypick-pilot-api:57`, 0.5 vCPU / 1 GB, image `backend:sha-b93dec77c274` (`sha256:d3a38d75...c5853f`), desired 1, running 1 |
| Frontend service | `readypick-pilot-frontend:47`, 0.25 vCPU / 0.5 GB, image `frontend:sha-b93dec77c274` (`sha256:8831d96f...11166c`), desired 1, running 1 |
| Analysis service | `readypick-pilot-analysis:16`, 2 vCPU / 8 GB, image `analysis:93ebfcb` (`sha256:e0c6d488...037ec1`), **desired 0, running 0** |
| Agent task definition (on-demand, no service) | family `readypick-pilot-agent`, newest revision **53**, 1 vCPU / 2 GB, image `backend:sha-d51149b0b0bf` (`sha256:b48d4f0a...b660e7`), registered 2026-10-30. **Eight commits behind the API** (`d51149b0` is an ancestor of `b93dec77`). The migrate task definition is also `:53` but on `sha-b93dec77c274`, so the last deploy re-registered migrate and did not move the agent |
| Lambda functions | `readypick-task-worker`, `readypick-jd-gen`, `readypick-company-profile`: image `backend:sha-b93dec77c274-fn`, resolved `sha256:d3a38d7566ee871c2268c7cb8fc785c21f91777f1779ebe2f7af75c528c5853f`, last modified 2026-10-31 16:25 UTC. `readypick-assessment-trigger`: zip, code sha256 `UxmgpM1Q...QM3hA=`, last modified 2026-09-04, `ECS_TASK_DEFINITION=readypick-pilot-agent` (family, so it always runs the newest ACTIVE revision) |
| Database | RDS PostgreSQL 16.13 `available`, not publicly accessible. Alembic head applied: **`0137_public_demo`** (from the migrate task log of 2026-10-31). This branch adds `0138_background_jobs`, not applied |
| Redis | ElastiCache `readypick-pilot-001`, redis 7.1.0, `cache.t4g.micro`, `available`; the evictions and memory alarms are OK |
| Fargate On-Demand vCPU quota | `L-3032A538` = **2.0**. Usage metric peaked at 4.0 vCPU in the last two days (the quota is checked at launch against a lagging usage count, so a burst of three 1 vCPU launches was admitted at 2026-10-31 20:00 UTC while earlier bursts were refused) |
| EventBridge schedules | 21 schedules, all `ENABLED` except `readypick-pilot-rds-sleep-guard` (`DISABLED`, target `stopDBInstance`). 20 target `readypick-task-worker`, with `MaximumRetryAttempts: 0` and no dead-letter queue. `reconcile-project-intake`, `reconcile-proctoring-sessions` and `reconcile-assessment-recordings` are all `rate(60 minutes)`, and all ran (the `reconcile_project_intake` run at 15:00 UTC failed with `DispatchError ClientError` while the function was being redeployed; every later hourly run succeeded) |
| Alarms in ALARM | `readypick-pilot-agent-task-failures`, `readypick-pilot-assessment-trigger-error-rate` |
| Feature flags in the live agent task | `DEMO_MODE=true`, `DEMO_MODE_CONFIRM=public-demo`, `TASK_DISPATCH_BACKEND=aws`, `FRONTEND_URL=https://readypick.ai`, `PROCTORING_ANALYSIS_SERVICE_URL=http://analysis.readypick.local:8100` (the analysis service has zero tasks, so that URL has no listener) |
| Task worker Lambda | `TRANSCRIBE_ENABLED=true`, `TRANSCRIBE_REGION=ap-south-1`, a dedicated transcribe bucket; the API task definition lists no `TRANSCRIBE_*` variable by name (not resolved further; see issue 3) |

**Read-only database route: none.** The RDS instance is private. The only routes to it are a bastion or `ecs execute-command`, which the mission forbids, and the RDS log group holds server logs, not rows. Stuck-row counts were therefore NOT read. What the logs below prove about the three stuck projects is stated as such.

## 2. Why long ECS work does not complete (RUNTIME unless marked)

Two independent defects stack, and the second would have kept every project failing even with unlimited capacity.

**A. Capacity: RunTask is refused.** `/aws/lambda/readypick-assessment-trigger` holds 17 `ecs:RunTask refused ...: You've reached the limit on the number of vCPUs you can run concurrently` errors between 2026-10-31 16:00 and 20:00 UTC: `process_candidate_project` 10, `process_assessment_video` 3, `generate_proctoring_report` 3, `generate_candidate_questions` 1. Arithmetic: quota 2 vCPU, standing services 0.75 (api 0.5 + frontend 0.25), headroom 1.25, one agent task is 1 vCPU, the analysis service (2 vCPU) cannot fit at all. The trigger was invoked asynchronously, so each refusal raised in a function nobody awaited. The API had already answered `202` and the row stayed `submitted`/`uploaded`; only the error-rate alarm moved. The API's own request log said "queued".

**B. Execution: the project task itself fails every time it does start.** 12 `process_candidate_project` agent tasks started in the last three days; all 12 ended `taskrun.failed ... error=RepositoryUnavailable` (exit code 1, `EssentialContainerExited`), each after one in-process retry, 35 to 75 seconds in. They come in batches of three, matching the three stuck projects in the screenshots (the hourly `reconcile_project_intake` re-dispatches each stale project). The traceback ends in `httpx/_decoders.py` with `zlib.error: Error -3 ... incorrect header check`, raised while `services/egress.py::request` rebuilds the response. **Confirmed from the traceback and the code:** `_read_bounded` collects `response.aiter_bytes()`, which is already DECODED, and `request` then builds `httpx.Response(headers=response.headers, content=body)` keeping the original `Content-Encoding: gzip`. httpx decodes the already-decoded body a second time and fails. GitHub answers gzip to httpx's default `Accept-Encoding`, confirmed with `curl`, so every GitHub repository fetch in the product fails this way. The pipeline lets `RepositoryUnavailable` escape by design and the worker session rolls back, so the row never changed and the candidate saw "submitted" for ever. **Owner of the fix: the egress/project workstream** (`backend/app/services/egress.py`): rebuild the response without `content-encoding`, `content-length` and `transfer-encoding`, with a regression test that serves a gzip body through `httpx.MockTransport`. This workstream added the bounded failure record that makes such a defect visible and terminal (section 4), but did not edit `egress.py`.

**C. Version skew (RUNTIME).** The agent runs code eight commits older than the API. A fix to any ECS task body reaches production only after the agent task definition is re-registered with the new image tag (Terraform `image_tag`, then no service roll is needed because the trigger resolves the family). The deploy must verify the agent revision, not only the services.

**D. Not the cause (RUNTIME).** Redis is healthy, EventBridge schedules run, the task-worker Lambda has no errors, and the 4 `run_matching`, 1 `run_functional_assessment`, 1 `generate_candidate_questions` agent runs in the same window all succeeded. The analysis service being at zero is a separate loss: proctoring speaker analysis is off, and its URL has no listener.

## 3. Capacity recommendation (NOT APPLIED; owner decision)

No infrastructure was applied and no quota request was filed. Prices are list-price estimates for Fargate Linux x86 (about 0.04 USD per vCPU-hour and 0.0044 USD per GB-hour in the nearest priced region, `ap-south-1`) and are **not verified against the account's bill**.

| Option | Effect | Cost |
|---|---|---|
| 1. Request Fargate On-Demand vCPU quota 2 to 8 (Service Quotas `L-3032A538`) | Several agent tasks at once; room to run the analysis service when wanted. No charge until used | none until used. A 1 vCPU / 2 GB agent task is about 0.05 USD per hour, so a 10 minute run is under 0.01 USD |
| 2. Agent task 1 vCPU / 2 GB to 0.5 vCPU / 2 GB (`infra/environments/*/main.tf`, the `agent` block) | Two agent tasks fit in the current headroom. ffmpeg transcodes take longer | saves about 0.02 USD per task-hour |
| 3. Keep analysis at zero, serialise agent work | The job ledger already records `capacity_blocked` and retries with backoff for 24 hours, one task at a time in practice | none; slow under load |
| 4. Run the analysis service (2 vCPU / 8 GB) | Speaker analysis returns. Needs option 1 first | about 0.12 USD per hour, roughly 85 USD per month always on |

Recommendation: option 1 (free until used) and keep analysis at zero until the owner wants speaker analysis; option 3 is already what the code now does safely. A Terraform file edit was judged not clearly needed, so none was made.

## 4. Matrix of the ten checks

Columns: observed, root cause, patch, test, deployment verification. "WS" means the workstream that owns the change fills its cell; this workstream filled only the rows it owns (async/infra, rows 9 and 10, and the dispatch parts of 6, 7 and 3).

| # | Observed | Root cause | Patch | Test | Deployment verification |
|---|---|---|---|---|---|
| 1 Phone detection | CODE only: not reproduced live | CODE: hypothesis, see mission item 5 (worker asset/model load, thresholds); not examined here | WS | WS | not deployed |
| 2 Loudness | CODE only | CODE: no loudness detector exists; Path B counts toward the three-warning rule, so it needs an advisory path | WS | WS | not deployed |
| 3 Voice answers | RUNTIME: `TRANSCRIBE_ENABLED=true`, `TRANSCRIBE_REGION=ap-south-1` on the task worker. No transcription task appears in the 3-day agent log. CODE: `transcribe_voice_answer` is `Route.LAMBDA` with `max_attempts=1` | CODE: hypothesis that `voice_input_available` hides the control (API reads its own `TRANSCRIBE_ENABLED`, not resolved in the API task definition); dispatch is `dispatch_after_commit`, so it is now ledgered and re-driven | dispatch durable via the ledger (this workstream); UI and API availability: WS | `test_job_ledger_db.py` (outbox, claim); voice: WS | not deployed |
| 4 Profile upload removal | CODE only | CODE: `BgvDocumentsCard` on the profile page; see `docs/operations/BGV_LEGACY_DOCUMENTS.md` | WS | WS | not deployed |
| 5 Staff invitations | CODE only | CODE: hypothesis, Next 16 `searchParams` is a Promise; `join/page.tsx` read it synchronously | WS | WS | not deployed |
| 6 Proctoring report in PRISM | RUNTIME: 3 `generate_proctoring_report` RunTask refusals (quota) on 2026-10-31 18:00, 19:00, 20:00 UTC; `reconcile_proctoring_sessions` hourly and healthy. The analysis service is at zero | RUNTIME-confirmed: report generation was dropped by the quota refusal with no durable record. CODE: report rendering questions are WS | dispatch durable and re-driven by `sweep_background_jobs` (this workstream); report states: WS | `test_job_ledger_db.py::test_a_capacity_refusal_is_blocked_with_backoff_and_not_retried_early` | not deployed |
| 7 Video preview/download | RUNTIME: 3 `process_assessment_video` refusals (quota) 17:00, 19:00, 20:00 UTC. Raw segments are deleted by lifecycle rules, so a recording left `uploaded` long enough is unrecoverable | RUNTIME-confirmed: processing never started. CODE: `reconcile_assessment_recordings` re-handed `uploaded` rows with plain `dispatch()` (no record, no capacity state) | `reconcile_assessment_recordings` now hands off through `dispatch_once_after_commit` with a ledger row and dedupe key (this workstream); API and UI: WS | `test_job_ledger_db.py`; sweep dedupe covered by `test_dedupe_key_allows_one_active_job_and_a_new_one_after_it_ends` | not deployed |
| 8 PRISM PDF | CODE only | CODE: gating and consent copy; WS | WS | WS | not deployed |
| 9 Projects stuck | RUNTIME: 10 `process_candidate_project` RunTask refusals plus 12 started-and-failed runs (`RepositoryUnavailable`, root cause B above). Three projects (the screenshots) re-dispatched hourly with no row change. Row state NOT read (no read-only DB route) | Confirmed from logs and code: capacity refusal (A), `egress.py` double-decode (B), and the pipeline rolled back the failure so nothing was recorded | `services/projects/lifecycle.py` (claim, heartbeat via `updated_at`, bounded fetch-failure record with candidate sentence), `process_candidate_project` and `reconcile_project_intake` rewritten around it (durable re-dispatch, stuck bound, `failed_extraction` after 5 sweeps); `egress.py` fix: WS | `tests/test_project_lifecycle.py` (8 tests, real Postgres) | not deployed; needs agent task definition re-registered |
| 10 Async truth | RUNTIME: see section 2. CODE: `status.read` reported PENDING for a missing, expired or unreadable Redis record; a Lambda 202 was the only evidence of dispatch; RunTask refusals reached only an alarm | Confirmed | `background_jobs` ledger + outbox, `workers/jobs.py` (claim, heartbeat, sweep, bounds), `dispatch.dispatch_after_commit` / `dispatch_once_after_commit` / `redrive`, `status.read` DB-first with `UNKNOWN`, `sweep_background_jobs` every 5 minutes in `schedule.py` and three environments' Terraform, trigger Lambda `[capacity]`/`[refused]` tags and `RunTask` `clientToken`/`startedBy`, `api/job_status.py`, `frontend/components/job-status.tsx`, `docs/spec/BACKGROUND_WORK.md` section 10 | `test_job_ledger_pure.py` (17), `test_job_ledger_db.py` (13), `job-status.test.tsx` (8); 551 related backend tests pass | not deployed; migration `0138_background_jobs`, Terraform scheduler entry and agent task definition all pending the owner |

## 5. Verification commands run (2026-11-01)

- `./scripts/test.sh unit -- tests/test_job_ledger_db.py tests/test_job_ledger_pure.py tests/test_project_lifecycle.py` against the fresh test stack (Docker Postgres, Redis, MinIO): all 38 pass.
- The same runner over 37 related modules (task registry, schedule parity, after-commit, dispatch sweep, Redis loop binding, matching, outreach, leadership, email delivery, project evidence, worker tenant session, cross-tenant isolation, CTC prompt registry, others) plus the three new modules: 551 pass, none skipped, none failed.
- `npx vitest run components/job-status.test.tsx`: 8 pass. `npx tsc --noEmit` shows no error in the new files. `npx eslint` on the two new files: clean.
- The full backend suite and the frontend build were NOT run by this workstream.

## 6. Left for the owner

1. Apply `0138_background_jobs` (migration) before the API image that contains this branch.
2. Terraform: the `readypick-sweep-background-jobs` scheduler entry (edited in `infra/environments/{pilot,staging,production}/main.tf`, **not applied**) and a new `image_tag` so the agent task definition moves off `sha-d51149b0b0bf`.
3. Decide the capacity option in section 3.
4. Decide whether to record ECS task STOPPED reasons on the ledger row (EventBridge rule on the cluster; described in `docs/spec/BACKGROUND_WORK.md` section 10.6, decided, pending).
5. After deploy, re-read the three stuck projects. Their original uploads may already be erased; a repository-only project can simply be re-run, and a project whose retries ended as `failed_extraction` shows the candidate a resubmit sentence rather than fabricated evidence.
