# Production defects: traceability matrix, 2026-11-01

**Status:** measurement (a dated record, not a specification). Branch `fix/production-defects` from `main` at `b93dec77`. Deployed to the pilot on 2026-11-02 (section 7); the sections above are the state recorded before the change. Every cell marked **RUNTIME** was read from the live AWS account on 2026-11-01 with the read-only `admin` profile in `ap-south-2` (cluster `readypick-pilot`); every cell marked **CODE** is a finding from the working tree. A **hypothesis** is labelled as one. Identities, account ids, secrets and tokens are omitted.

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
| 1 Phone detection | CODE only: not reproduced live | CODE: hypothesis, see mission item 5 (worker asset/model load, thresholds); not examined here | WS | WS | deployed 2026-11-02, see section 7 |
| 2 Loudness | CODE only | CODE: no loudness detector exists; Path B counts toward the three-warning rule, so it needs an advisory path | WS | WS | deployed 2026-11-02, see section 7 |
| 3 Voice answers | RUNTIME: `TRANSCRIBE_ENABLED=true`, `TRANSCRIBE_REGION=ap-south-1` on the task worker. No transcription task appears in the 3-day agent log. CODE: `transcribe_voice_answer` is `Route.LAMBDA` with `max_attempts=1` | CODE: hypothesis that `voice_input_available` hides the control (API reads its own `TRANSCRIBE_ENABLED`, not resolved in the API task definition); dispatch is `dispatch_after_commit`, so it is now ledgered and re-driven | dispatch durable via the ledger (this workstream); UI and API availability: WS | `test_job_ledger_db.py` (outbox, claim); voice: WS | deployed 2026-11-02, see section 7 |
| 4 Profile upload removal | CODE only | CODE: `BgvDocumentsCard` on the profile page; see `docs/operations/BGV_LEGACY_DOCUMENTS.md` | WS | WS | deployed 2026-11-02, see section 7 |
| 5 Staff invitations | CODE only | CODE: hypothesis, Next 16 `searchParams` is a Promise; `join/page.tsx` read it synchronously | WS | WS | deployed 2026-11-02, see section 7 |
| 6 Proctoring report in PRISM | RUNTIME: 3 `generate_proctoring_report` RunTask refusals (quota) on 2026-10-31 18:00, 19:00, 20:00 UTC; `reconcile_proctoring_sessions` hourly and healthy. The analysis service is at zero | RUNTIME-confirmed: report generation was dropped by the quota refusal with no durable record. CODE: report rendering questions are WS | dispatch durable and re-driven by `sweep_background_jobs` (this workstream); report states: WS | `test_job_ledger_db.py::test_a_capacity_refusal_is_blocked_with_backoff_and_not_retried_early` | deployed 2026-11-02, see section 7 |
| 7 Video preview/download | RUNTIME: 3 `process_assessment_video` refusals (quota) 17:00, 19:00, 20:00 UTC. Raw segments are deleted by lifecycle rules, so a recording left `uploaded` long enough is unrecoverable | RUNTIME-confirmed: processing never started. CODE: `reconcile_assessment_recordings` re-handed `uploaded` rows with plain `dispatch()` (no record, no capacity state) | `reconcile_assessment_recordings` now hands off through `dispatch_once_after_commit` with a ledger row and dedupe key (this workstream); API and UI: WS | `test_job_ledger_db.py`; sweep dedupe covered by `test_dedupe_key_allows_one_active_job_and_a_new_one_after_it_ends` | deployed 2026-11-02, see section 7 |
| 8 PRISM PDF | CODE only | CODE: gating and consent copy; WS | WS | WS | deployed 2026-11-02, see section 7 |
| 9 Projects stuck | RUNTIME: 10 `process_candidate_project` RunTask refusals plus 12 started-and-failed runs (`RepositoryUnavailable`, root cause B above). Three projects (the screenshots) re-dispatched hourly with no row change. Row state NOT read (no read-only DB route) | Confirmed from logs and code: capacity refusal (A), `egress.py` double-decode (B), and the pipeline rolled back the failure so nothing was recorded | `services/projects/lifecycle.py` (claim, heartbeat via `updated_at`, bounded fetch-failure record with candidate sentence), `process_candidate_project` and `reconcile_project_intake` rewritten around it (durable re-dispatch, stuck bound, `failed_extraction` after 5 sweeps); `egress.py` fix: WS | `tests/test_project_lifecycle.py` (8 tests, real Postgres) | deployed 2026-11-02, see section 7 |
| 10 Async truth | RUNTIME: see section 2. CODE: `status.read` reported PENDING for a missing, expired or unreadable Redis record; a Lambda 202 was the only evidence of dispatch; RunTask refusals reached only an alarm | Confirmed | `background_jobs` ledger + outbox, `workers/jobs.py` (claim, heartbeat, sweep, bounds), `dispatch.dispatch_after_commit` / `dispatch_once_after_commit` / `redrive`, `status.read` DB-first with `UNKNOWN`, `sweep_background_jobs` every 5 minutes in `schedule.py` and three environments' Terraform, trigger Lambda `[capacity]`/`[refused]` tags and `RunTask` `clientToken`/`startedBy`, `api/job_status.py`, `frontend/components/job-status.tsx`, `docs/spec/BACKGROUND_WORK.md` section 10 | `test_job_ledger_pure.py` (17), `test_job_ledger_db.py` (13), `job-status.test.tsx` (8); 551 related backend tests pass | deployed 2026-11-02, see section 7 |

## 5. Verification commands run (2026-11-01)

- `./scripts/test.sh unit -- tests/test_job_ledger_db.py tests/test_job_ledger_pure.py tests/test_project_lifecycle.py` against the fresh test stack (Docker Postgres, Redis, MinIO): all 38 pass.
- The same runner over 37 related modules (task registry, schedule parity, after-commit, dispatch sweep, Redis loop binding, matching, outreach, leadership, email delivery, project evidence, worker tenant session, cross-tenant isolation, CTC prompt registry, others) plus the three new modules: 551 pass, none skipped, none failed.
- `npx vitest run components/job-status.test.tsx`: 8 pass. `npx tsc --noEmit` shows no error in the new files. `npx eslint` on the two new files: clean.
- The full backend suite and the frontend build were NOT run by this workstream.

## 6. Left for the owner

1. Decide the Fargate quota (support case open since 2026-10-30). Until it is raised, agent tasks that find the 2 vCPU quota full are refused and now re-driven by the ledger (section 7).
2. Decide the ECS STOPPED-reason ledger (decided, pending; `docs/spec/BACKGROUND_WORK.md` section 10.6).
3. Decide whether the analysis service runs (desired 0). The deploy verification reports it as having no running tasks for that reason.
4. Reconcile the live drift with Terraform (a full apply of `infra/environments/pilot` would destroy the RDS sleep guard, recreate four interface endpoints, strip `DEMO_MODE` from the Lambdas and raise the frontend to 0.5 vCPU) and the stale state images (`e25fdf15494d`). No apply was run.

## 7. Deployment to the pilot (RUNTIME, 2026-11-02)

Rollback: backend and frontend `sha-b93dec77c274`, services `readypick-pilot-api:57` and `readypick-pilot-frontend:47`, agent `readypick-pilot-agent:53`. Migration 0138 is additive and needs no downgrade.

| Step | Result |
|---|---|
| PR #25 merged | CI all green, including the backend suite and agent evaluation; merge commit `21e54384`. The pipeline's own deploy jobs are skipped (no `AWS_DEPLOY_ENABLED`), so the release was applied by the repository scripts |
| `./scripts/run-migration.sh pilot` | exit 0; log: `Running upgrade 0137_public_demo -> 0138_background_jobs` |
| `./scripts/update-lambda-code.sh pilot` | `readypick-task-worker`, `readypick-jd-gen`, `readypick-company-profile` read back at `backend@sha256:b477e8f4...a1e45d` |
| `readypick-assessment-trigger` (zip) | `aws lambda update-function-code` from `lambda/assessment_trigger/handler.py`; `Successful`, `Active` (Terraform ignores its code hash, so it is deployed by hand) |
| `./scripts/deploy-services.sh pilot` | api `:57 -> :58`, frontend `:47 -> :48`, analysis already on `:16`; services stable, circuit breaker did not fire |
| Schedule `readypick-sweep-background-jobs` | created `rate(5 minutes)`, `Asia/Kolkata`, `ENABLED`, target `readypick-task-worker`, `MaximumRetryAttempts: 0`; imported into pilot Terraform state (Terraform 1.16.2) |
| `./scripts/verify-deployment.sh pilot` | api and frontend: every running task is on the expected digest (`backend sha256:b477e8f4...a1e45d`, `frontend sha256:716cd633...8dbba3`). analysis: NO RUNNING TASKS because the service is deliberately at desired 0, so the script exits non-zero for that reason alone (owner decision, section 6) |

Smoke checks:

- `https://readypick.ai/` 200; `/health/live` 200 `{"status":"ok"}`; the sign-in route redirects to `/login` (200); API `/docs` 200. No error lines in the API log group for the 15 minutes after the rollout.
- `sweep_background_jobs` on the task worker: `taskrun.succeeded`, run on the schedule.
- Agent `RunTask` path: invoking `readypick-assessment-trigger` with `sweep_background_jobs` started task `91cf5812...` on `readypick-pilot-agent:54`; it ended exit 0 on digest `sha256:b477e8f4...a1e45d` (`ecs_task.done`).
- Stuck projects (issue 9): `reconcile_project_intake` was run once by hand (the same payload as the hourly schedule). It requeued 2 projects. One RunTask was refused for capacity (`[capacity]` class) and the ledger re-drove it; both `process_candidate_project` runs then ended `taskrun.succeeded` (26.3 s and 13.3 s). Before the fix all 12 earlier runs ended `RepositoryUnavailable`. This confirms the egress gzip fix against the live GitHub fetch. The third screenshot project was not requeued by this sweep; its row state was not read (no read-only database route) and it is not claimed fixed.
- Not exercised live: the proctoring sound-level advisory, the invitation link, the voice transcription round trip and the PRISM PDF download need a signed-in candidate or staff session, which this deploy did not use. They are covered by the tests in section 5 and the CI run.
