#!/usr/bin/env python3
"""Wake, sleep and inspect the ReadyPick pilot without deleting resources.

This intentionally uses the AWS CLI already required by the repository.  It
adds no Python dependency and works from Windows, macOS and Linux.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


EXPECTED_ACCOUNT = "016617990245"
REGION = "ap-south-2"
CLUSTER = "readypick-pilot"
RDS_INSTANCE = "readypick-pilot"
REDIS_GROUP = "readypick-pilot"
ALB_NAME = "readypick-pilot"
PUBLIC_URL = "https://readypick.ai"
DISCOVERY_NAMESPACE = "readypick.local"
SCHEDULE_PREFIX = "readypick-"
SLEEP_GUARD_SCHEDULE = "readypick-pilot-rds-sleep-guard"
SERVICES = {
    "api": "readypick-pilot-api",
    "frontend": "readypick-pilot-frontend",
    "analysis": "readypick-pilot-analysis",
}
LAUNCH_LIMITS = {
    "api": (1, 4),
    "frontend": (1, 4),
    "analysis": (1, 2),
}
SCALABLE_DIMENSION = "ecs:service:DesiredCount"


class PilotError(RuntimeError):
    """A concise, operator-actionable failure."""


@dataclass
class ServiceState:
    name: str
    desired: int
    running: int
    pending: int
    healthy: bool
    deployments: list[dict[str, Any]]


class AwsCli:
    def __init__(self, profile: str | None, region: str = REGION):
        self.profile = profile
        self.region = region

    def run(self, service: str, operation: str, *args: str) -> Any:
        command = ["aws", service, operation, *args, "--region", self.region, "--output", "json"]
        if self.profile:
            command.extend(["--profile", self.profile])
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise PilotError(f"AWS {service} {operation} failed: {detail}")
        output = result.stdout.strip()
        return json.loads(output) if output else {}


def _identity(profile: str | None) -> dict[str, Any] | None:
    command = ["aws", "sts", "get-caller-identity", "--output", "json"]
    if profile:
        command.extend(["--profile", profile])
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def resolve_profile(requested: str | None) -> str | None:
    """Select credentials for the known pilot account without exposing keys."""
    candidates: list[str | None] = []
    if requested:
        candidates.append(requested)
    elif os.environ.get("AWS_PROFILE"):
        candidates.append(os.environ["AWS_PROFILE"])
    candidates.append(None)

    result = subprocess.run(
        ["aws", "configure", "list-profiles"], text=True, capture_output=True, check=False
    )
    if result.returncode == 0:
        candidates.extend(line.strip() for line in result.stdout.splitlines() if line.strip())

    seen: set[str | None] = set()
    wrong_accounts: list[str] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        identity = _identity(candidate)
        if not identity:
            continue
        account = str(identity.get("Account", ""))
        if account == EXPECTED_ACCOUNT:
            return candidate
        wrong_accounts.append(account)

    found = ", ".join(sorted(set(filter(None, wrong_accounts)))) or "none"
    raise PilotError(
        f"No AWS credentials for pilot account {EXPECTED_ACCOUNT}. "
        f"Reachable account(s): {found}. Run `aws login` or configure the pilot profile."
    )


class PilotPower:
    def __init__(self, aws: AwsCli, timeout: int = 1800, sleep: Callable[[float], None] = time.sleep):
        self.aws = aws
        self.timeout = timeout
        self._sleep = sleep

    def _rds(self) -> dict[str, Any]:
        data = self.aws.run(
            "rds", "describe-db-instances", "--db-instance-identifier", RDS_INSTANCE
        )
        instances = data.get("DBInstances", [])
        if len(instances) != 1:
            raise PilotError(f"RDS resource {RDS_INSTANCE} was not found uniquely.")
        return instances[0]

    def _raw_services(self) -> dict[str, dict[str, Any]]:
        data = self.aws.run(
            "ecs", "describe-services", "--cluster", CLUSTER, "--services", *SERVICES.values()
        )
        failures = data.get("failures", [])
        if failures:
            raise PilotError(f"ECS service lookup failed: {failures}")
        by_name = {service["serviceName"]: service for service in data.get("services", [])}
        missing = sorted(set(SERVICES.values()) - set(by_name))
        if missing:
            raise PilotError(f"Missing ECS service(s): {', '.join(missing)}")
        return by_name

    def _target_health(self, service: dict[str, Any]) -> bool:
        groups = service.get("loadBalancers", [])
        if not groups:
            return self._container_health(service["serviceName"])
        for group in groups:
            data = self.aws.run(
                "elbv2",
                "describe-target-health",
                "--target-group-arn",
                group["targetGroupArn"],
            )
            targets = data.get("TargetHealthDescriptions", [])
            if not targets or any(t.get("TargetHealth", {}).get("State") != "healthy" for t in targets):
                return False
        return True

    def _container_health(self, service_name: str) -> bool:
        listed = self.aws.run(
            "ecs",
            "list-tasks",
            "--cluster",
            CLUSTER,
            "--service-name",
            service_name,
            "--desired-status",
            "RUNNING",
        ).get("taskArns", [])
        if not listed:
            return False
        tasks = self.aws.run(
            "ecs", "describe-tasks", "--cluster", CLUSTER, "--tasks", *listed
        ).get("tasks", [])
        return bool(tasks) and all(
            task.get("lastStatus") == "RUNNING"
            and task.get("healthStatus") == "HEALTHY"
            and all(
                container.get("healthStatus", "HEALTHY") == "HEALTHY"
                for container in task.get("containers", [])
                if container.get("name") in SERVICES
            )
            for task in tasks
        )

    def services(self) -> dict[str, ServiceState]:
        raw = self._raw_services()
        states: dict[str, ServiceState] = {}
        for short, full in SERVICES.items():
            service = raw[full]
            desired = int(service.get("desiredCount", 0))
            running = int(service.get("runningCount", 0))
            healthy = running == 0 if desired == 0 else running >= desired and self._target_health(service)
            states[short] = ServiceState(
                name=full,
                desired=desired,
                running=running,
                pending=int(service.get("pendingCount", 0)),
                healthy=healthy,
                deployments=list(service.get("deployments", [])),
            )
        return states

    def _alb_active(self) -> bool:
        data = self.aws.run("elbv2", "describe-load-balancers", "--names", ALB_NAME)
        lbs = data.get("LoadBalancers", [])
        return len(lbs) == 1 and lbs[0].get("State", {}).get("Code") == "active"

    def _redis_available(self) -> bool:
        data = self.aws.run(
            "elasticache", "describe-replication-groups", "--replication-group-id", REDIS_GROUP
        )
        groups = data.get("ReplicationGroups", [])
        return len(groups) == 1 and groups[0].get("Status") == "available"

    def _schedule_states(self) -> tuple[dict[str, str], str]:
        data = self.aws.run("scheduler", "list-schedules", "--name-prefix", SCHEDULE_PREFIX)
        rows = data.get("Schedules", [])
        applications = {
            str(row.get("Name")): str(row.get("State", "UNKNOWN"))
            for row in rows
            if row.get("Name") != SLEEP_GUARD_SCHEDULE
        }
        guard = next(
            (
                str(row.get("State", "UNKNOWN"))
                for row in rows
                if row.get("Name") == SLEEP_GUARD_SCHEDULE
            ),
            "MISSING",
        )
        return applications, guard

    def _update_schedule_state(self, name: str, state: str) -> None:
        schedule = self.aws.run("scheduler", "get-schedule", "--name", name)
        args = [
            "--name",
            name,
            "--group-name",
            str(schedule.get("GroupName", "default")),
            "--schedule-expression",
            str(schedule["ScheduleExpression"]),
            "--flexible-time-window",
            json.dumps(schedule["FlexibleTimeWindow"], separators=(",", ":")),
            "--target",
            json.dumps(schedule["Target"], separators=(",", ":")),
            "--state",
            state,
        ]
        optional = {
            "Description": "--description",
            "ScheduleExpressionTimezone": "--schedule-expression-timezone",
            "StartDate": "--start-date",
            "EndDate": "--end-date",
            "KmsKeyArn": "--kms-key-arn",
        }
        for key, flag in optional.items():
            if schedule.get(key) is not None:
                args.extend([flag, str(schedule[key])])
        self.aws.run("scheduler", "update-schedule", *args)

    def _set_application_schedules(self, enabled: bool) -> None:
        wanted = "ENABLED" if enabled else "DISABLED"
        states, _ = self._schedule_states()
        if not states:
            raise PilotError("No ReadyPick application schedules were found.")
        for name, current in states.items():
            if current != wanted:
                self._update_schedule_state(name, wanted)

    def _set_sleep_guard(self, enabled: bool) -> None:
        wanted = "ENABLED" if enabled else "DISABLED"
        _, current = self._schedule_states()
        if current == "MISSING":
            raise PilotError(f"Sleep guard schedule {SLEEP_GUARD_SCHEDULE} was not found.")
        if current != wanted:
            self._update_schedule_state(SLEEP_GUARD_SCHEDULE, wanted)

    def status(self, *, print_report: bool = True) -> str:
        rds = self._rds()
        services = self.services()
        alb = self._alb_active()
        redis = self._redis_available()
        schedules, guard = self._schedule_states()
        rds_status = str(rds.get("DBInstanceStatus", "unknown"))

        all_zero = all(s.desired == 0 and s.running == 0 for s in services.values())
        application_schedules_off = bool(schedules) and all(
            state == "DISABLED" for state in schedules.values()
        )
        application_schedules_on = bool(schedules) and all(
            state == "ENABLED" for state in schedules.values()
        )
        sleep_guard_on = guard == "ENABLED"
        public_ready = all(
            services[name].desired >= 1 and services[name].running >= 1 and services[name].healthy
            for name in ("api", "frontend")
        )
        transitional = rds_status in {"starting", "stopping", "rebooting"} or any(
            s.pending or s.running != s.desired for s in services.values()
        )
        broken = (rds_status == "available" and any(
            s.desired > 0 and not s.healthy for s in services.values()
        )) or not alb or not redis

        if all_zero and rds_status == "stopped" and application_schedules_off and sleep_guard_on:
            conclusion = "PILOT IS ASLEEP"
        elif transitional:
            conclusion = "PILOT IS STARTING"
        elif (
            rds_status == "available"
            and public_ready
            and application_schedules_on
            and guard == "DISABLED"
            and not broken
        ):
            conclusion = "PILOT IS READY FOR TESTING"
        elif broken:
            conclusion = "PILOT HAS A PROBLEM"
        else:
            conclusion = "PILOT IS PARTIALLY RUNNING"

        if print_report:
            print("READY PICK PILOT STATUS\n")
            print(f"RDS:      {rds_status.upper()}")
            for label in ("api", "frontend", "analysis"):
                item = services[label]
                health = "healthy" if item.healthy else "unhealthy"
                print(f"{label.title():9} desired {item.desired}  running {item.running}  {health}")
            print(f"ALB:      {'healthy' if alb else 'unhealthy'}")
            print(f"Redis:    {'available' if redis else 'unavailable'}")
            schedule_word = (
                "disabled"
                if application_schedules_off
                else "enabled"
                if application_schedules_on
                else "mixed"
            )
            print(f"Schedules: {schedule_word} ({len(schedules)} application schedules)")
            print(f"RDS guard: {guard.lower()}")
            if rds_status == "available" and all_zero:
                print("\nNote: RDS is available while compute is off; AWS may have auto-restarted it.")
            print(f"\n{conclusion}")
        return conclusion

    def _wait(self, description: str, check: Callable[[], tuple[bool, str]]) -> None:
        deadline = time.monotonic() + self.timeout
        last = ""
        while True:
            done, current = check()
            if current != last:
                print(f"    {description}: {current}")
                last = current
            if done:
                return
            if time.monotonic() >= deadline:
                raise PilotError(f"Timed out waiting for {description}. Last state: {current}")
            self._sleep(10)

    def _wait_rds(self, expected: str) -> None:
        self._wait(
            f"RDS {expected}",
            lambda: (
                (state := str(self._rds().get("DBInstanceStatus"))) == expected,
                state,
            ),
        )

    def _set_scaling_suspended(self, short: str, suspended: bool) -> None:
        state = "true" if suspended else "false"
        self.aws.run(
            "application-autoscaling",
            "register-scalable-target",
            "--service-namespace",
            "ecs",
            "--resource-id",
            f"service/{CLUSTER}/{SERVICES[short]}",
            "--scalable-dimension",
            SCALABLE_DIMENSION,
            "--suspended-state",
            (
                f"DynamicScalingInSuspended={state},"
                f"DynamicScalingOutSuspended={state},"
                f"ScheduledScalingSuspended={state}"
            ),
        )

    def _restore_launch_limits(self, short: str, *, resume: bool) -> None:
        minimum, maximum = LAUNCH_LIMITS[short]
        state = "false" if resume else "true"
        self.aws.run(
            "application-autoscaling",
            "register-scalable-target",
            "--service-namespace",
            "ecs",
            "--resource-id",
            f"service/{CLUSTER}/{SERVICES[short]}",
            "--scalable-dimension",
            SCALABLE_DIMENSION,
            "--min-capacity",
            str(minimum),
            "--max-capacity",
            str(maximum),
            "--suspended-state",
            (
                f"DynamicScalingInSuspended={state},"
                f"DynamicScalingOutSuspended={state},"
                f"ScheduledScalingSuspended={state}"
            ),
        )

    def _set_desired(self, short: str, count: int) -> None:
        self.aws.run(
            "ecs",
            "update-service",
            "--cluster",
            CLUSTER,
            "--service",
            SERVICES[short],
            "--desired-count",
            str(count),
        )

    def _wait_service(self, short: str, count: int, *, require_health: bool) -> None:
        def check() -> tuple[bool, str]:
            item = self.services()[short]
            count_ready = item.desired == count and item.running == count and item.pending == 0
            health_ready = not require_health or item.healthy
            text = (
                f"desired={item.desired} running={item.running} pending={item.pending} "
                f"health={'healthy' if item.healthy else 'unhealthy'}"
            )
            return count_ready and health_ready, text

        self._wait(f"{short} service", check)

    def _start_rds(self) -> None:
        status = str(self._rds().get("DBInstanceStatus"))
        if status == "stopped":
            self.aws.run("rds", "start-db-instance", "--db-instance-identifier", RDS_INSTANCE)
        elif status not in {"available", "starting"}:
            raise PilotError(f"RDS cannot be started safely from state {status}.")
        self._wait_rds("available")
        if not self._rds().get("Endpoint", {}).get("Address"):
            raise PilotError("RDS is available but has no endpoint.")

    @staticmethod
    def _http_ok(url: str) -> tuple[bool, str]:
        request = urllib.request.Request(url, headers={"User-Agent": "ReadyPickPilotPower/1"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return 200 <= response.status < 400, f"HTTP {response.status}"
        except urllib.error.HTTPError as error:
            return False, f"HTTP {error.code}"
        except OSError as error:
            return False, type(error).__name__

    def _wait_public_health(self) -> None:
        for url in (f"{PUBLIC_URL}/health/live", PUBLIC_URL):
            self._wait(url, lambda url=url: self._http_ok(url))

    def wake(self) -> None:
        print("[1/6] Checking AWS identity and current state...")
        self._set_sleep_guard(False)
        print("[2/6] Starting PostgreSQL...")
        self._start_rds()
        print("[3/6] Starting API...")
        self._set_scaling_suspended("api", True)
        self._set_desired("api", 1)
        self._wait_service("api", 1, require_health=True)
        self._restore_launch_limits("api", resume=True)
        print("[4/6] Starting frontend...")
        self._set_scaling_suspended("frontend", True)
        self._set_desired("frontend", 1)
        self._wait_service("frontend", 1, require_health=True)
        self._restore_launch_limits("frontend", resume=True)
        print("[5/6] Keeping analysis off...")
        analysis = self.services()["analysis"]
        analysis_was_running = bool(analysis.desired or analysis.running)
        if analysis_was_running:
            print("    Analysis was already running; it was left untouched for safety.")
        else:
            self._set_scaling_suspended("analysis", True)
        print("[6/6] Running public health checks...")
        self._wait_public_health()
        self._set_application_schedules(True)
        # RegisterScalableTarget can asynchronously reconcile an existing
        # min-capacity of one after the earlier zero check.  Reassert zero only
        # when analysis was off at entry; never interrupt a task the operator
        # had deliberately left running.
        if not analysis_was_running:
            self._set_scaling_suspended("analysis", True)
            self._set_desired("analysis", 0)
            self._wait_service("analysis", 0, require_health=False)
        print(f"\nREADY PICK PILOT IS AWAKE\n\nApplication:\n{PUBLIC_URL}")
        if analysis.desired or analysis.running:
            print("\nAnalysis service: ON (it was already running)")
        else:
            print(
                "\nAnalysis service: OFF\n\nUse:\n"
                "python scripts/pilot_power.py analysis-start\n\n"
                "before testing proctoring/analysis functionality."
            )

    def _assert_no_deployment(self) -> None:
        for short, state in self.services().items():
            active = [
                deployment
                for deployment in state.deployments
                if deployment.get("rolloutState") not in {None, "COMPLETED"}
            ]
            if len(state.deployments) > 1 or active:
                raise PilotError(
                    f"Refusing to sleep: {short} has an ECS deployment in progress. "
                    "Wait for the deployment to complete and retry."
                )

    def _assert_no_one_shot_tasks(self) -> None:
        arns = self.aws.run(
            "ecs", "list-tasks", "--cluster", CLUSTER, "--desired-status", "RUNNING"
        ).get("taskArns", [])
        if not arns:
            return
        tasks = self.aws.run("ecs", "describe-tasks", "--cluster", CLUSTER, "--tasks", *arns).get(
            "tasks", []
        )
        unexpected = [
            task
            for task in tasks
            if not str(task.get("group", "")).startswith("service:")
        ]
        if unexpected:
            labels = ", ".join(
                f"{task.get('group') or 'one-shot'} ({str(task.get('taskArn', '')).rsplit('/', 1)[-1]})"
                for task in unexpected
            )
            raise PilotError(
                f"Refusing to sleep: active migration/background ECS task(s): {labels}. "
                "Wait for them to stop and retry."
            )

    def _recent_analysis_requests(self) -> list[str]:
        start_ms = int((datetime.now(timezone.utc).timestamp() - 120) * 1000)
        data = self.aws.run(
            "logs",
            "filter-log-events",
            "--log-group-name",
            f"/ecs/{CLUSTER}/analysis",
            "--start-time",
            str(start_ms),
            "--limit",
            "100",
        )
        markers = ("POST /diarize", "POST /ai-text")
        return [
            event.get("message", "")
            for event in data.get("events", [])
            if any(marker in event.get("message", "") for marker in markers)
        ]

    def _assert_safe_rds_state(self) -> None:
        rds = self._rds()
        status = rds.get("DBInstanceStatus")
        if status not in {"available", "stopped"}:
            raise PilotError(
                f"Refusing to sleep: RDS is {status}, which can indicate backup/modification work."
            )
        if rds.get("PendingModifiedValues"):
            raise PilotError("Refusing to sleep: RDS has pending modified values.")

    def _stop_analysis(self, *, check_activity: bool) -> None:
        state = self.services()["analysis"]
        if check_activity and (state.desired or state.running):
            if self._recent_analysis_requests():
                raise PilotError(
                    "Analysis traffic was seen within the last two minutes. It may still be active; "
                    "wait two minutes after the job completes and retry."
                )
        self._set_scaling_suspended("analysis", True)
        self._set_desired("analysis", 0)
        self._wait_service("analysis", 0, require_health=False)

    def analysis_stop(self) -> None:
        print("[1/3] Checking active analysis work...")
        self._assert_no_deployment()
        self._assert_no_one_shot_tasks()
        print("[2/3] Stopping analysis safely...")
        self._stop_analysis(check_activity=True)
        print("[3/3] Confirming no analysis tasks remain...")
        self._wait_service("analysis", 0, require_health=False)
        print("\nANALYSIS SERVICE STOPPED")

    def sleep_mode(self) -> None:
        print("[1/6] Checking deployments, jobs and database operations...")
        self._assert_no_deployment()
        self._assert_no_one_shot_tasks()
        self._assert_safe_rds_state()
        self._set_application_schedules(False)
        print("[2/6] Stopping analysis...")
        self._stop_analysis(check_activity=True)
        print("[3/6] Stopping frontend...")
        self._set_scaling_suspended("frontend", True)
        self._set_desired("frontend", 0)
        self._wait_service("frontend", 0, require_health=False)
        print("[4/6] Stopping API...")
        self._set_scaling_suspended("api", True)
        self._set_desired("api", 0)
        self._wait_service("api", 0, require_health=False)
        print("[5/6] Stopping PostgreSQL...")
        rds_status = str(self._rds().get("DBInstanceStatus"))
        if rds_status == "available":
            self.aws.run("rds", "stop-db-instance", "--db-instance-identifier", RDS_INSTANCE)
        elif rds_status != "stopped":
            raise PilotError(f"RDS cannot be stopped safely from state {rds_status}.")
        self._wait_rds("stopped")
        self._set_sleep_guard(True)
        print("[6/6] Verifying final state...")
        conclusion = self.status(print_report=False)
        if conclusion != "PILOT IS ASLEEP":
            raise PilotError(f"Sleep sequence ended in unexpected state: {conclusion}")
        print(
            "\nREADY PICK PILOT IS ASLEEP\n\nRunning application compute:\n"
            "API       0\nFrontend  0\nAnalysis  0\nRDS       stopped\n\n"
            "Persistent data remains intact."
        )

    def _analysis_discovered(self) -> tuple[bool, str]:
        data = self.aws.run(
            "servicediscovery",
            "discover-instances",
            "--namespace-name",
            DISCOVERY_NAMESPACE,
            "--service-name",
            "analysis",
            "--health-status",
            "HEALTHY",
        )
        count = len(data.get("Instances", []))
        return count >= 1, f"{count} healthy Cloud Map instance(s)"

    def analysis_start(self) -> None:
        print("[1/4] Checking database and API...")
        if self._rds().get("DBInstanceStatus") != "available":
            raise PilotError("Analysis requires an available RDS instance. Run `wake` first.")
        api = self.services()["api"]
        if api.running < 1 or not api.healthy:
            raise PilotError("Analysis requires a healthy API. Run `wake` first.")
        print("[2/4] Starting analysis task...")
        self._set_scaling_suspended("analysis", True)
        self._set_desired("analysis", 1)
        self._wait_service("analysis", 1, require_health=True)
        print("[3/4] Waiting for service discovery...")
        self._wait("analysis Cloud Map registration", self._analysis_discovered)
        print("[4/4] Confirming model/service initialization...")
        self._wait_service("analysis", 1, require_health=True)
        print("\nANALYSIS SERVICE READY\n\nProctoring/analysis testing can now begin.")

    def launch_mode(self) -> None:
        print("[1/6] Starting PostgreSQL...")
        self._set_sleep_guard(False)
        self._start_rds()
        for step, short in enumerate(("api", "frontend", "analysis"), start=2):
            print(f"[{step}/6] Restoring {short}...")
            self._set_scaling_suspended(short, True)
            self._set_desired(short, 1)
            self._wait_service(short, 1, require_health=True)
            self._restore_launch_limits(short, resume=True)
        print("[5/6] Checking analysis discovery...")
        self._wait("analysis Cloud Map registration", self._analysis_discovered)
        print("[6/6] Running public health checks...")
        self._wait_public_health()
        self._set_application_schedules(True)
        print(
            "\nREADY PICK PILOT IS IN LAUNCH MODE\n\n"
            "API autoscaling:      min 1 / max 4\n"
            "Frontend autoscaling: min 1 / max 4\n"
            "Analysis:             1 task, min 1 / max 2 (temporary synchronous architecture)"
        )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "command",
        choices=("status", "wake", "sleep", "analysis-start", "analysis-stop", "launch-mode"),
    )
    result.add_argument("--profile", help="AWS profile; otherwise the pilot account is detected")
    result.add_argument("--region", default=os.environ.get("AWS_REGION", REGION))
    result.add_argument("--timeout", type=int, default=1800, help="wait timeout in seconds")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if not shutil.which("aws"):
            raise PilotError("AWS CLI v2 is required and was not found on PATH.")
        print("Checking AWS identity...")
        profile = resolve_profile(args.profile)
        identity = _identity(profile)
        print(
            f"Using account {identity['Account']} in {args.region}"
            f"{f' (profile {profile})' if profile else ''}."
        )
        pilot = PilotPower(AwsCli(profile, args.region), timeout=args.timeout)
        actions = {
            "status": pilot.status,
            "wake": pilot.wake,
            "sleep": pilot.sleep_mode,
            "analysis-start": pilot.analysis_start,
            "analysis-stop": pilot.analysis_stop,
            "launch-mode": pilot.launch_mode,
        }
        actions[args.command]()
        return 0
    except (PilotError, KeyboardInterrupt) as error:
        print(f"\nFAILED: {error}", file=sys.stderr)
        print(
            "Correct the reported resource/state problem, run `status`, then retry the command. "
            "No delete operation is performed by this tool.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
