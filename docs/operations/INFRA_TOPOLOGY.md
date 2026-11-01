# Infrastructure topology

**Status:** current. **Reviewed:** 2026-10-14 against `infra/environments/pilot` composition names; counts and sizes are as last recorded.

What runs where on AWS, as composed by `infra/environments/pilot/main.tf`
after the Vivekium simplification release (2026-09-25). Pilot IS the live site
(region `ap-south-2`); `infra/environments/staging` and `production` have never
been applied, and `production` is DERIVED from `staging` by
`infra/environments/derive-production.py` (run it, then review
`git diff infra/environments/production`). Deploying is `DEPLOY_AWS.md`;
disaster recovery is `DISASTER_RECOVERY.md`. The code sandbox described
below is decided removal (register S-20), not a live component.

## 1. Compute

| Unit | Kind | Runs | Notes |
|---|---|---|---|
| `api` | ECS Fargate service | the backend image, FastAPI | behind the ALB; holds `lambda:InvokeFunction` on the two drafting functions and SES send |
| `frontend` | ECS Fargate service | the Next.js image | behind the ALB (default target group) |
| `analysis` | ECS Fargate service | the analysis-service image (diarization, the flagged AI-text detector) | one secret, `HUGGINGFACE_TOKEN`; reached through Cloud Map |
| `agent` | on-demand Fargate task definition | the backend image, `command = ["agent"]` | one task per `Route.ECS` dispatch (scoring, question generation, recording processing), started by the trigger, stops on exit |
| `migrate` | on-demand Fargate task definition | the backend image, `command = ["migrate"]` | the only container with `POSTGRES_MIGRATION_ROLE`; `scripts/run-migration.sh` polls it to STOPPED |
| `readypick-task-worker` | Lambda, backend image `<tag>-fn` | every `Route.LAMBDA` task | invokes ITSELF for Lambda-to-Lambda dispatch; holds the object-store, SES and Transcribe grants |
| `readypick-jd-gen`, `readypick-company-profile` | Lambda, backend image | the two synchronous drafting agents | invoked by the API only |
| `readypick-assessment-trigger` | Lambda, zip | `ecs:RunTask` for the `agent` family | the ONLY holder of `iam:PassRole`; thirty lines and boto3 |
| `inbound_email` | Lambda, zip | parses SES inbound mail from S3 via SNS | exists only when `INBOUND_EMAIL_DOMAIN` is set: pilot has none (inert) |
| Judge0 host | EC2 t3.medium, AL2023, optional | the code sandbox. **Decided removal (S-20)**; stage A resources were destroyed on 2026-09-28 and nothing is provisioned | `judge0_enabled` / `judge0_instance_enabled` / `judge0_clients_enabled`, all default false |
| Image builder | CodeBuild, native arm64 | image builds from a commit on main | `infra/modules/image_builder`, `scripts/build-images-remote.sh`; the role can push to ECR and never deploy |

**Retries live in one place**: `runtime.run_task` owns them and every Lambda
event invoke config sets `maximum_retry_attempts = 0`.

**Every task declares its RLS scope** (`@task(..., rls=...)`); a `tenant`
task opens `tenant_worker_session`, scoped by connection startup parameters on
a private engine.

## 2. Data

| Store | Module | Notes |
|---|---|---|
| Postgres + pgvector | `rds` (`db.t4g.medium`, 50 GB floor, 200 GB ceiling, `multi_az = false`) | the app connects as `pickready_app` (NOINHERIT, its own rotated password); `?ssl=require`; no RDS Proxy (pinning, 2026-09-10) |
| Redis | `elasticache`, `noeviction` | rate limiter, caches (tenant-keyed), run-status records, auth sessions, the proctoring warning counter; every client is a `LoopBoundRedis` |
| Object store | `s3`, one bucket, KMS | see section 3 |
| Transcribe working bucket | `aws_s3_bucket.transcribe`, in the Transcribe region, SSE-S3 | spoken answers are copied here for Transcribe and expire |
| Secrets | `secrets` | read map per service, enumerated; `service_secret_writers` for the rotation; `LLM_KEY_ENCRYPTION_SECRET` HELD, granted to no service |

## 3. The object store

One bucket per environment, default encryption `aws:kms` under the
environment's key (`aws_kms_key.this`). **The bucket policy refuses any
encryption other than `aws:kms`, any KMS key other than this environment's,
and `aws:kms` without a key id**, so every writer sends `aws:kms` and
`S3_KMS_KEY_ID` (the key's ARN) through `object_storage.sse_arguments`; an
empty key id refuses before any request. Versioning is on
(`noncurrent_retain_days`, thirty in pilot).

| Prefix | Written by | Lifecycle (the BACKSTOP, not the clock) |
|---|---|---|
| resumes, compliance, attachments, legacy BGV documents (no new uploads) | `object_storage` | none |
| `project-intake/` | project evidence intake | deleted by the pipeline after derivation, HEAD-confirmed |
| `assessment-raw/` | session recording segments (multipart) | 7 days; noncurrent versions 1 day |
| `assessment-compressed/` | the processed recording | `assessment_media_retention_days` (90); noncurrent 1 day |
| `voice-answers/` | spoken answers | 1 day; noncurrent 1 day |

The clock for a recording is `video_recordings.media_purge_due_at`, stamped
once at finalize; `pickready.reconcile_assessment_recordings` purges at the
earlier of that and the job-closure purge. A HEAD-confirmed delete on a
versioned bucket leaves the bytes readable at their version, which is why each
media prefix expires noncurrent versions after one day. The media prefixes are
kept out of the Infrequent Access transition.

## 4. Network

VPC with public subnets (ALB, NAT), private subnets (ECS, in-VPC Lambdas) and
data subnets with NO route to the internet in either direction (RDS,
ElastiCache). The Judge0 host sits in its own subnet with no route out of the
VPC except a dedicated S3 gateway endpoint limited to ECR layers and the AL2023
repository; its security group admits port 2358 from the `judge0_client`
group only, never from the shared `ecs` group. WAF in front of the ALB; ACM and
DNS for `readypick.ai` (the domain stays; the product name is Vivekium).

## 5. Schedules

`app/workers/schedule.py` is the source of truth; every environment mirrors it
as EventBridge Scheduler rules in `module "scheduler"`, and
`tests/test_schedule_parity.py` compares them. Sweeps added or rewritten by
this release: `reconcile_job_setup` (rewritten), `remind_unsaved_skills`,
`reconcile_queued_emails`, `reconcile_assessment_recordings`,
`reconcile_coding_submissions`, `probe_code_execution`,
`repair_semantic_index` (`purge_closed_job_assessments` is the 2026-09-22 closure sweep). `reconcile_coding_submissions` and `probe_code_execution` belong to the sandbox and go with it.
`verify_code_execution_sandbox` is registered and deliberately NOT scheduled.

## 6. Alarms added by this release

`infra/modules/observability`, each a log metric filter over a token the code
logs, each pinned to its constant by a test:

| Alarm | Token | Log group | Meaning |
|---|---|---|---|
| `*-prism-statement-withheld` | `prism.statement_withheld_for_review` | agent | a report withheld an uncited statement (a prompt regression or wiring defect) |
| `*-miti-not-assessed-final` | `miti.not_assessed_final_report` | agent | a report was written with skills "Not assessed" on its final attempt |
| `*-rag-repair-degraded` | `rag.repair.degraded` | task worker | the semantic repair sweep could not embed for three consecutive hours |
| `*-judge0-*` | host metrics and log lines | Judge0 host | defined with the sandbox module; moot while nothing is provisioned |

## 7. Switches that are OFF on pilot, and why

| Switch | State | Why |
|---|---|---|
| `CODE_EXECUTION_BACKEND` | `disabled` | the sandbox is decided removal (S-20); the setting is removed with the code |
| `judge0_enabled`, `judge0_instance_enabled`, `judge0_clients_enabled` | false | nothing is provisioned; removed with the sandbox module |
| `transcribe_enabled` | true on the task-worker Lambda; the API task definition must carry `TRANSCRIBE_ENABLED=true` too (decided, pending apply: it does not on `readypick-pilot-api:57`) | the API decides whether the microphone is offered; the Lambda transcribes |
| `INBOUND_EMAIL_DOMAIN` | empty | the region does not receive SES mail; reply threading is inert |

`DEMO_MODE` is not one of the switches above. As of 2026-10-30 the pilot API task definition `readypick-pilot-api:56` sets `DEMO_MODE=true` and `DEMO_MODE_CONFIRM=public-demo`. The frontend image `readypick-pilot-frontend:46` (tag `sha-cdf9b73d358a`) was built with `NEXT_PUBLIC_DEMO_MODE=true`. The code default is off. tfvars were not changed and must not say `demo_mode=true`. The CAPTCHA removal is deployed on `readypick-pilot-api:56` and `readypick-pilot-frontend:46`. [DEMO_MODE.md](DEMO_MODE.md)
