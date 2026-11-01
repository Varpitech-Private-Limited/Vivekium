"""The job ledger against real Postgres: outbox, claim, sweep, RLS, truth.

Each test asserts COMMITTED state from a second connection, because the point
of an outbox is what survives the transaction, not what the session believes.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import db as app_db
from app.core.config import get_settings
from app.core.db import superadmin_scope, tenant_scope
from app.models.background_job import BackgroundJob
from app.workers import dispatch, jobs, status

KIND = "pickready.process_candidate_project"


@pytest.fixture()
async def factory():
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect():
            pass
    except Exception:  # noqa: BLE001 -- any connect failure means "no DB here"
        await engine.dispose()
        pytest.skip("no database reachable")
    # Every dispatch in an earlier test leaves an outbox row. The sweep leases the
    # oldest rows first, so those would crowd out this module's rows in a full run.
    async with async_sessionmaker(engine)() as session:
        async with session.begin():
            async with superadmin_scope(session):
                await session.execute(text("DELETE FROM background_jobs"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with async_sessionmaker(engine)() as session:
        async with session.begin():
            async with superadmin_scope(session):
                await session.execute(
                    text("DELETE FROM background_jobs WHERE kind LIKE 'pickready.%' AND resource_type LIKE 'ledger-test%'")
                )
    # `status.read` uses the process-wide engine; drop it so no pooled connection
    # stays bound to this test's event loop for the next test.
    if app_db._engine is not None:
        await app_db._engine.dispose()
    app_db._engine = None
    app_db._session_factory = None
    await engine.dispose()


async def bypass_read(factory, job_id) -> BackgroundJob | None:
    """Read from a second connection, as the committed state sees it."""
    async with factory() as session:
        async with session.begin():
            async with superadmin_scope(session):
                return (
                    await session.execute(select(BackgroundJob).where(BackgroundJob.id == job_id))
                ).scalar_one_or_none()


async def seed(factory, **over) -> uuid.UUID:
    values = dict(
        id=uuid.uuid4(), kind=KIND, route="ecs", resource_type="ledger-test",
        resource_id=str(uuid.uuid4()), payload={"args": ["p"], "kwargs": {}},
    )
    values.update(over)
    async with factory() as session:
        async with session.begin():
            async with superadmin_scope(session):
                session.add(BackgroundJob(**values))
    return values["id"]


# -- Outbox ------------------------------------------------------------------


async def test_the_outbox_row_commits_with_the_work_and_vanishes_with_a_rollback(factory) -> None:
    async with factory() as session:
        async with superadmin_scope(session):
            handle = dispatch.dispatch_after_commit(
                session, KIND, args=["p"], resource_type="ledger-test", resource_id="r1"
            )
            await session.commit()
    committed = await bypass_read(factory, uuid.UUID(handle.id))
    assert committed is not None and committed.state == "queued"
    assert committed.payload == {"args": ["p"], "kwargs": {}}
    assert handle.id in [item.id for item in dispatch.recorded()]

    dispatch.clear_recorded()
    async with factory() as session:
        async with superadmin_scope(session):
            lost = dispatch.dispatch_after_commit(
                session, KIND, args=["p"], resource_type="ledger-test", resource_id="r2"
            )
            await session.rollback()
    assert await bypass_read(factory, uuid.UUID(lost.id)) is None
    assert dispatch.recorded() == []


async def test_a_lost_invoke_leaves_a_queued_row_the_sweep_redrives(factory) -> None:
    """The fast path never ran (the process died after the commit): positive control."""
    job_id = await seed(factory, queued_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    calls: list[int] = []

    async def accepted(job, attempt):
        calls.append(attempt)
        return jobs.Outcome(True)

    async with factory() as session:
        async with superadmin_scope(session):
            report = await jobs.sweep(session, accepted)
    after = await bypass_read(factory, job_id)
    assert report.redriven >= 1 and calls
    assert (after.state, after.dispatch_attempts) == ("accepted", 1)
    assert after.dispatched_at is not None and after.started_at is None


async def test_a_fresh_queued_row_is_left_alone(factory) -> None:
    """The floor: the sweep must not double-start work the fast path just sent."""
    job_id = await seed(factory)

    async def never(job, attempt):  # pragma: no cover - must not be called for this row
        assert job.id != job_id
        return jobs.Outcome(True)

    async with factory() as session:
        async with superadmin_scope(session):
            await jobs.sweep(session, never)
    assert (await bypass_read(factory, job_id)).state == "queued"


async def test_dedupe_key_allows_one_active_job_and_a_new_one_after_it_ends(factory) -> None:
    key = f"ledger-test:{uuid.uuid4()}"
    async with factory() as session:
        async with superadmin_scope(session):
            first = await dispatch.dispatch_once_after_commit(
                session, KIND, dedupe_key=key, args=["p"], resource_type="ledger-test"
            )
            second = await dispatch.dispatch_once_after_commit(
                session, KIND, dedupe_key=key, args=["p"], resource_type="ledger-test"
            )
            await session.commit()
    assert second.id == first.id
    assert dispatch.recorded_names().count(KIND) == 1

    async with factory() as session:
        async with superadmin_scope(session):
            await jobs.complete(session, first.id)
            third = await dispatch.dispatch_once_after_commit(
                session, KIND, dedupe_key=key, args=["p"], resource_type="ledger-test"
            )
            await session.commit()
    assert third.id != first.id


# -- Capacity, bounds, 202 is not success -------------------------------------


async def test_a_capacity_refusal_is_blocked_with_backoff_and_not_retried_early(factory) -> None:
    job_id = await seed(factory, queued_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    attempts: list[uuid.UUID] = []

    async def refused(job, attempt):
        attempts.append(job.id)
        return jobs.Outcome(False, jobs.FAIL_CAPACITY, "run_task_capacity")

    async with factory() as session:
        async with superadmin_scope(session):
            await jobs.sweep(session, refused)
            await jobs.sweep(session, refused)
    after = await bypass_read(factory, job_id)
    assert attempts.count(job_id) == 1  # the second pass saw the backoff
    assert (after.state, after.failure_class) == ("capacity_blocked", "capacity")
    assert after.next_retry_at > datetime.now(timezone.utc)
    view = jobs.public_view(after)
    assert view["state"] == "capacity_blocked" and "busy" in view["message"]


async def test_dispatch_attempts_are_bounded_and_end_in_a_stated_failure(factory) -> None:
    job_id = await seed(
        factory, state="capacity_blocked", failure_class="capacity",
        dispatch_attempts=jobs.MAX_DISPATCH_ATTEMPTS,
        next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    async def unreachable(job, attempt):  # pragma: no cover
        raise AssertionError("an exhausted job must not be re-driven")

    async with factory() as session:
        async with superadmin_scope(session):
            await jobs.sweep(session, unreachable)
    after = await bypass_read(factory, job_id)
    assert (after.state, after.failure_class) == ("failed", "capacity_exhausted")
    assert after.completed_at is not None


async def test_an_accepted_invoke_is_not_running_until_a_worker_claims_it(factory) -> None:
    """202 evidence: acceptance leaves the job `accepted`; only a claim runs it."""
    job_id = await seed(factory)
    async with factory() as session:
        async with superadmin_scope(session):
            row = await session.get(BackgroundJob, job_id)
            row.state, row.dispatched_at = "accepted", datetime.now(timezone.utc)
            await session.commit()
    stored = await bypass_read(factory, job_id)
    assert stored.state == "accepted" and stored.started_at is None
    assert status.RunStatus  # the vocabulary under test lives beside it
    async with factory() as session:
        async with superadmin_scope(session):
            assert await jobs.claim(session, str(job_id)) == jobs.CLAIMED
    assert (await bypass_read(factory, job_id)).state == "running"


async def test_a_silent_worker_is_failed_unless_the_task_is_idempotent(factory) -> None:
    silent = datetime.now(timezone.utc) - timedelta(hours=1)
    unsafe = await seed(factory, state="running", attempts=1, started_at=silent, heartbeat_at=silent)
    safe = await seed(
        factory, state="running", attempts=1, idempotent=True, started_at=silent, heartbeat_at=silent
    )
    live = await seed(
        factory, state="running", attempts=1, started_at=silent,
        heartbeat_at=datetime.now(timezone.utc),
    )

    async def accepted(job, attempt):
        assert job.id == safe
        return jobs.Outcome(True)

    async with factory() as session:
        async with superadmin_scope(session):
            await jobs.sweep(session, accepted)
    assert (await bypass_read(factory, unsafe)).failure_class == "worker_lost"
    assert (await bypass_read(factory, safe)).state == "retrying"
    assert (await bypass_read(factory, live)).state == "running"


async def test_old_terminal_rows_are_purged_and_recent_ones_kept(factory) -> None:
    old = await seed(factory, state="succeeded", completed_at=datetime.now(timezone.utc) - timedelta(days=45))
    recent = await seed(factory, state="succeeded", completed_at=datetime.now(timezone.utc) - timedelta(days=2))

    async def unused(job, attempt):  # pragma: no cover
        return jobs.Outcome(True)

    async with factory() as session:
        async with superadmin_scope(session):
            report = await jobs.sweep(session, unused)
    assert report.purged >= 1
    assert await bypass_read(factory, old) is None
    assert await bypass_read(factory, recent) is not None


# -- Duplicate execution ------------------------------------------------------


async def test_only_one_delivery_of_a_run_id_becomes_the_worker(factory) -> None:
    job_id = await seed(factory)
    async with factory() as session:
        async with superadmin_scope(session):
            assert await jobs.claim(session, str(job_id)) == jobs.CLAIMED
            assert await jobs.claim(session, str(job_id)) == jobs.DUPLICATE
            await jobs.complete(session, str(job_id))
            # At-least-once redelivery of a finished run does nothing.
            assert await jobs.claim(session, str(job_id)) == jobs.DUPLICATE
            assert await jobs.claim(session, str(uuid.uuid4())) == jobs.UNTRACKED
    done = await bypass_read(factory, job_id)
    assert (done.state, done.attempts, done.payload) == ("succeeded", 1, {})


async def test_run_task_executes_a_twice_delivered_run_once(factory, monkeypatch) -> None:
    from app.workers import runtime
    from app.workers.registry import task

    name = "pickready.test_ledger_probe"
    ran: list[int] = []
    task(name=name, route=dispatch.Route.LAMBDA, rls="bypass", rls_reason="test probe")(
        lambda: ran.append(1)
    )
    job_id = await seed(factory, kind=name, route="lambda")
    body = {"task": name, "run_id": str(job_id), "args": [], "kwargs": {}}

    import asyncio

    await asyncio.to_thread(runtime.run_task, body)
    await asyncio.to_thread(runtime.run_task, body)
    assert ran == [1]
    assert (await bypass_read(factory, job_id)).state == "succeeded"


# -- Redis never decides ------------------------------------------------------


async def test_an_expired_redis_record_does_not_turn_a_finished_run_into_pending(factory, monkeypatch) -> None:
    async def no_cache(run_id):
        return None

    monkeypatch.setattr(status, "_read_redis", no_cache)
    done = await seed(factory, state="succeeded", completed_at=datetime.now(timezone.utc))
    queued = await seed(factory)
    failed = await seed(factory, state="failed", failure_class="worker_error", failure_detail="Boom")

    finished = await status.read(str(done))
    assert (finished.state, finished.result_expired, finished.source) == ("SUCCESS", True, "ledger")
    assert (await status.read(str(queued))).state == "PENDING"  # the ledger says so
    broken = await status.read(str(failed))
    assert (broken.state, broken.error, broken.done) == ("FAILURE", "Boom", True)
    # No ledger row and no cache: unknown, never waiting.
    assert (await status.read(str(uuid.uuid4()))).state == "UNKNOWN"


async def test_a_stale_progress_record_never_decorates_a_finished_run(factory, monkeypatch) -> None:
    async def stale(run_id):
        return status.RunStatus(run_id=run_id, state="PROGRESS", payload={"stages": ["x"]})

    monkeypatch.setattr(status, "_read_redis", stale)
    done = await seed(factory, state="succeeded", completed_at=datetime.now(timezone.utc))
    result = await status.read(str(done))
    assert result.state == "SUCCESS" and result.payload == {}


# -- Tenancy -----------------------------------------------------------------


async def test_a_tenant_reads_only_its_own_jobs_and_platform_jobs_stay_hidden(factory) -> None:
    tenants = [uuid.uuid4(), uuid.uuid4()]
    async with factory() as session:
        async with session.begin():
            async with superadmin_scope(session):
                for tid in tenants:
                    await session.execute(
                        text(
                            "INSERT INTO tenants (id, name, domain, spf_dkim_status) "
                            "VALUES (:id, 'ledger-rls', :d, 'pending')"
                        ),
                        {"id": str(tid), "d": f"{tid}.ledger.test"},
                    )
    try:
        owned = []
        for tid in tenants:
            async with factory() as session:
                async with session.begin():
                    async with tenant_scope(session, tid):
                        # tenant_id defaults to the session's tenant.
                        handle = dispatch.dispatch_after_commit(
                            session, KIND, args=["p"], resource_type="ledger-test", resource_id=str(tid)
                        )
                        owned.append(uuid.UUID(handle.id))
        platform = await seed(factory)  # bypass session: no tenant

        async with factory() as session:
            async with session.begin():
                async with tenant_scope(session, tenants[0]):
                    visible = set(
                        (await session.execute(select(BackgroundJob.id).where(BackgroundJob.resource_type == "ledger-test"))).scalars()
                    )
                    assert await jobs.latest(session, "ledger-test", tenants[1]) is None
        assert visible == {owned[0]}
        assert platform not in visible
        assert (await bypass_read(factory, owned[1])).tenant_id == tenants[1]
    finally:
        async with factory() as session:
            async with session.begin():
                async with superadmin_scope(session):
                    await session.execute(text("DELETE FROM background_jobs WHERE resource_type = 'ledger-test'"))
                    await session.execute(
                        text("DELETE FROM tenants WHERE id = ANY(:ids)"), {"ids": [str(t) for t in tenants]}
                    )
