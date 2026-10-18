# Pilot solo-test mode

## Why this exists

For the temporary owner-only testing period, ReadyPick runs application compute
only during an explicit test session. The cost-optimised pilot architecture
keeps encrypted state and stable routing intact, but stops compute and periodic
work between sessions. It is intentionally not a wholesale serverless rewrite.

Sleeping means API, frontend and analysis ECS desired/running counts are zero,
the PostgreSQL RDS instance is stopped, and application schedules are disabled.
Redis, ALB, NAT, S3, ECR, Lambda, Secrets Manager, backups and core networking
remain intact. Paid interface endpoints are absent while Judge0 is disabled;
private tasks use the existing managed NAT path when awake.

The controller accepts only AWS account `016617990245`, cluster
`readypick-pilot` and region `ap-south-2`. It automatically selects a configured
profile whose STS identity matches that account. Use `--profile NAME` only to
override discovery.

## Commands

Run these from the repository root:

```text
python scripts/pilot_power.py status
python scripts/pilot_power.py wake
python scripts/pilot_power.py sleep
python scripts/pilot_power.py analysis-start
python scripts/pilot_power.py analysis-stop
python scripts/pilot_power.py launch-mode
```

`status` is read-only. It reports RDS, each ECS service, target/container
health, ALB, Redis and a final overall state.

`wake` starts RDS and waits for it, then starts and health-checks the API and
frontend in dependency order. It checks both `https://readypick.ai/health/live`
and the website. Analysis remains off.

`sleep` refuses to continue while an ECS deployment, migration/one-shot task,
recent analysis request, RDS modification, or unsafe RDS state is visible. It
then disables application schedules, stops analysis, frontend and API in that
order, waits for all tasks to exit, and only then stops RDS. Finally it enables
the hourly RDS sleep guard. The command is idempotent.

`analysis-start` requires awake RDS and API. It starts one analysis task and
waits for the container's `/health` check and healthy Cloud Map registration.
The container check runs only after model initialization, so success means the
service is ready, not merely that ECS says RUNNING.

`analysis-stop` conservatively refuses if analysis traffic appeared in the
last two minutes. This avoids interrupting a synchronous inference request.

`launch-mode` disables the RDS sleep guard and restores RDS, schedules, and all
three standing services, with autoscaling
API 1..4, frontend 1..4 and analysis 1..2. Analysis is restored to one task
because the current proctoring path is synchronous; this is temporary until a
separately designed on-demand architecture exists.

Windows `.cmd` wrappers alongside the Python script provide the same commands.

## Autoscaling and Terraform drift

Sleep suspends all three Application Auto Scaling directions before setting an
ECS desired count to zero. Wake resumes API/frontend after they are healthy;
analysis stays suspended until explicitly started. Launch mode restores the
normal min/max values and resumes every target.

Terraform already ignores operational ECS `desired_count`. It now also ignores
only the scalable target's `suspended_state`, so an ordinary deploy cannot wake
a sleeping pilot. Terraform continues to own min/max limits, policies and all
resource shape. The controller never changes task definitions, policies or
persistent resources.

## RDS automatic restart

AWS automatically restarts an RDS instance after it has been stopped for seven
consecutive days. The enabled sleep guard reissues `StopDBInstance` hourly while
the pilot is asleep, limiting an automatic restart to at most one hour of DB
compute. `wake` disables the guard before it starts RDS. `status` reports both
the application schedules and guard state.

## What still costs money while sleeping

The sleeping floor includes the ALB, NAT Gateway and its data processing, Redis
`cache.t4g.micro`, RDS/EBS storage and backups beyond the free automated backup
allowance, S3/ECR/log storage, Route 53, and any Lambda invocations. RDS compute
and ECS Fargate compute stop billing while stopped; RDS storage does not.

## What not to delete

Never manually delete RDS, Redis, buckets, ECR, ALB, NAT, VPC/subnets, security
groups, Secrets Manager entries, Lambda infrastructure, backups or Terraform
state. Terraform intentionally removes the four paid interface endpoints while
Judge0 is disabled and retains the no-hourly-charge S3 gateway endpoint. Do not
disable deletion protection or automated backups.

## If a command fails

1. Read the failed step and resource in the error.
2. Run `python scripts/pilot_power.py status`.
3. For ECS failures, inspect the named service's latest events and CloudWatch
   log group under `/ecs/readypick-pilot/`.
4. For RDS, wait until its current operation finishes; do not force-stop it.
5. Retry the same command. Operations are idempotent.
6. If recovery is needed, run `wake`; for launch availability run
   `launch-mode`.

Rollback of this feature is operational: run `launch-mode`, then revert the
pilot frontend CPU/memory and the autoscaling suspension lifecycle change in
Terraform. Never roll back by deleting infrastructure.
