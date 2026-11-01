"""The job ledger's decisions, with no database.

Everything here is a pure function of a row and the clock: when a row is due,
what the sweep does with it, what a screen is told, and how the trigger Lambda
classifies a refusal. The DB-backed behaviour (claim, outbox, RLS) is in
`test_job_ledger_db.py`.
"""
from __future__ import annotations

import importlib.util
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.background_job import BackgroundJob
from app.workers import jobs, status
from app.workers.registry import resolve

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def row(**over) -> BackgroundJob:
    values = dict(
        id=uuid.uuid4(),
        kind="pickready.process_candidate_project",
        route="ecs",
        state="queued",
        idempotent=False,
        attempts=0,
        dispatch_attempts=0,
        queued_at=NOW - timedelta(minutes=10),
        updated_at=NOW - timedelta(minutes=10),
        payload={"args": ["x"], "kwargs": {}},
    )
    values.update(over)
    return BackgroundJob(**values)


def test_backoff_is_deterministic_and_capped() -> None:
    assert [jobs.backoff_seconds(n) for n in (1, 2, 3, 4)] == [120, 240, 480, 960]
    assert jobs.backoff_seconds(40) == jobs.BACKOFF_MAX


def test_a_queued_row_is_not_due_inside_the_grace_and_is_after_it() -> None:
    assert not jobs.due(row(queued_at=NOW - timedelta(seconds=30)), NOW)
    assert jobs.due(row(queued_at=NOW - timedelta(seconds=jobs.QUEUE_GRACE + 1)), NOW)


def test_an_accepted_row_is_not_running_and_is_redriven_after_the_start_grace() -> None:
    """A Lambda 202 is acceptance only: an accepted row nobody claimed is due again."""
    fresh = row(state="accepted", dispatched_at=NOW - timedelta(minutes=2))
    stuck = row(state="accepted", dispatched_at=NOW - timedelta(minutes=16))
    assert not jobs.due(fresh, NOW)
    assert jobs.due(stuck, NOW)
    assert jobs.decide(stuck, NOW).action == "redrive"


def test_a_lease_or_backoff_in_the_future_blocks_the_sweep() -> None:
    assert not jobs.due(row(next_retry_at=NOW + timedelta(minutes=1)), NOW)
    assert jobs.due(row(next_retry_at=NOW - timedelta(seconds=1)), NOW)


def test_a_silent_worker_is_due_only_after_the_stale_heartbeat() -> None:
    alive = row(state="running", heartbeat_at=NOW - timedelta(seconds=90))
    silent = row(state="running", heartbeat_at=NOW - timedelta(seconds=jobs.STALE_HEARTBEAT + 5))
    assert not jobs.due(alive, NOW)
    assert jobs.due(silent, NOW)


def test_a_lost_worker_is_redriven_only_when_the_task_is_idempotent() -> None:
    silent = dict(state="running", heartbeat_at=NOW - timedelta(hours=1))
    assert jobs.decide(row(idempotent=True, attempts=1, **silent), NOW).action == "redrive"
    unsafe = jobs.decide(row(idempotent=False, attempts=1, **silent), NOW)
    assert (unsafe.action, unsafe.failure_class) == ("fail", jobs.FAIL_WORKER_LOST)
    spent = jobs.decide(row(idempotent=True, attempts=jobs.MAX_EXECUTIONS, **silent), NOW)
    assert (spent.action, spent.failure_class) == ("fail", jobs.FAIL_WORKER_LOST)


def test_dispatch_and_capacity_retries_are_bounded() -> None:
    exhausted = jobs.decide(
        row(dispatch_attempts=jobs.MAX_DISPATCH_ATTEMPTS, failure_class=jobs.FAIL_DISPATCH), NOW
    )
    assert (exhausted.action, exhausted.failure_class) == ("fail", jobs.FAIL_DISPATCH_EXHAUSTED)
    blocked = jobs.decide(
        row(
            state="capacity_blocked",
            failure_class=jobs.FAIL_CAPACITY,
            queued_at=NOW - jobs.CAPACITY_MAX_AGE - timedelta(minutes=1),
        ),
        NOW,
    )
    assert (blocked.action, blocked.failure_class) == ("fail", jobs.FAIL_CAPACITY_EXHAUSTED)


def test_public_view_has_no_percent_unless_the_task_reported_a_measurable_step() -> None:
    assert jobs.public_view(row(state="running"), now=NOW)["percent"] is None
    measured = row(state="running", progress_done=3, progress_total=4)
    assert jobs.public_view(measured, now=NOW)["percent"] == 75


def test_estimate_needs_measured_runs_and_is_dropped_when_the_run_is_slower() -> None:
    running = row(state="running", started_at=NOW - timedelta(seconds=30))
    assert jobs.public_view(running, now=NOW, estimate=None)["estimate"] is None
    estimate = jobs.Estimate(low_seconds=60, high_seconds=120, samples=9)
    view = jobs.public_view(running, now=NOW, estimate=estimate)["estimate"]
    assert view == {"low_seconds": 30, "high_seconds": 90, "samples": 9}
    slow = row(state="running", started_at=NOW - timedelta(seconds=600))
    assert jobs.public_view(slow, now=NOW, estimate=estimate)["estimate"] is None
    queued = row(state="queued")
    assert jobs.public_view(queued, now=NOW, estimate=estimate)["estimate"] is None


def test_retry_is_offered_only_for_a_failed_retry_safe_job() -> None:
    failed = dict(state="failed", failure_class=jobs.FAIL_WORKER_LOST)
    assert jobs.public_view(row(idempotent=True, **failed), now=NOW)["can_retry"]
    assert not jobs.public_view(row(idempotent=False, **failed), now=NOW)["can_retry"]
    permanent = row(idempotent=True, state="failed", failure_class=jobs.FAIL_PERMANENT)
    assert not jobs.public_view(permanent, now=NOW)["can_retry"]
    assert not jobs.public_view(row(idempotent=True, state="running"), now=NOW)["can_retry"]


def test_every_sentence_a_screen_can_receive_is_free_of_em_dashes_and_internals() -> None:
    sentences = list(jobs._MESSAGES.values()) + list(jobs._FAILURE_MESSAGES.values())
    for text in sentences:
        assert "—" not in text
        assert "ecs" not in text.lower() and "lambda" not in text.lower()


def test_unknown_run_is_unknown_not_pending_and_keeps_pollers_polling() -> None:
    unknown = status.unknown("abc")
    assert unknown.state == status.STATE_UNKNOWN != status.STATE_PENDING
    assert not unknown.done


def test_wire_payload_carries_the_redrive_count() -> None:
    from app.workers.dispatch import payload_for

    spec = resolve("pickready.process_candidate_project")
    assert payload_for("r", spec, ["a"], {})["dispatch_attempt"] == 0
    assert payload_for("r", spec, ["a"], {}, 3)["dispatch_attempt"] == 3


def test_the_sweep_is_registered_scheduled_and_declares_its_rls() -> None:
    spec = resolve("pickready.sweep_background_jobs")
    assert spec.rls == "bypass" and spec.rls_reason
    from app.workers.schedule import SCHEDULE

    assert any(entry.task == spec.name for entry in SCHEDULE)


# -- The trigger Lambda ------------------------------------------------------

HANDLER = pathlib.Path(__file__).resolve().parents[2] / "lambda" / "assessment_trigger" / "handler.py"


@pytest.fixture()
def trigger(monkeypatch):
    for key, value in {
        "ECS_CLUSTER": "c", "ECS_TASK_DEFINITION": "t", "PRIVATE_SUBNET_IDS": "s-1",
        "ECS_SECURITY_GROUP_IDS": "sg-1", "AWS_DEFAULT_REGION": "ap-south-2",
        "AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x",
    }.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("assessment_trigger_under_test", HANDLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeEcs:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def run_task(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


QUOTA = "You've reached the limit on the number of vCPUs you can run concurrently"


def test_trigger_tags_a_vcpu_quota_refusal_as_capacity(trigger) -> None:
    trigger._ecs = FakeEcs({"failures": [{"reason": QUOTA}], "tasks": []})
    with pytest.raises(trigger.RunTaskRefused, match=r"^\[capacity\]"):
        trigger.lambda_handler({"task": "pickready.x", "run_id": str(uuid.uuid4())})


def test_trigger_tags_any_other_refusal_as_refused(trigger) -> None:
    trigger._ecs = FakeEcs({"failures": [{"reason": "MISSING"}], "tasks": []})
    with pytest.raises(trigger.RunTaskRefused, match=r"^\[refused\]"):
        trigger.lambda_handler({"task": "pickready.x", "run_id": "r"})


def test_trigger_is_idempotent_per_run_and_redrive_and_names_the_run(trigger) -> None:
    ecs = FakeEcs({"failures": [], "tasks": [{"taskArn": "arn:task/1"}]})
    trigger._ecs = ecs
    run_id = str(uuid.uuid4())
    result = trigger.lambda_handler({"task": "pickready.x", "run_id": run_id, "dispatch_attempt": 2})
    call = ecs.calls[0]
    assert call["clientToken"] == f"{run_id}:2" and len(call["clientToken"]) <= 64
    assert call["startedBy"] == run_id
    assert result["statusCode"] == 202
