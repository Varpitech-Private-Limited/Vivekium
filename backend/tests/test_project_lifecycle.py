"""A candidate project's execution is claimed, visible and bounded (2026-11-01).

Real Postgres. Each outcome is read back from a second connection.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.core import db as app_db
from app.core.db import superadmin_scope
from app.models.background_job import BackgroundJob
from app.models.project import CandidateProject
from app.services.projects import lifecycle
from app.services.projects import pipeline as project_pipeline
from app.services.projects.repository import RepositoryUnavailable
from app.workers import dispatch, jobs
from app.workers.registry import resolve
from tests import rag_world


@pytest.fixture
async def world():
    engine, factory = await rag_world.factory_or_skip()
    fx = rag_world.World()
    await rag_world.seed(factory, fx)
    try:
        yield factory, fx
    finally:
        async with factory() as s:
            async with s.begin():
                async with superadmin_scope(s):
                    await s.execute(
                        text("DELETE FROM background_jobs WHERE resource_type = 'candidate_project'")
                    )
        await rag_world.cleanup(factory, fx)
        if app_db._engine is not None:
            await app_db._engine.dispose()
        app_db._engine = None
        app_db._session_factory = None
        await engine.dispose()


async def add_project(factory, fx, **over) -> uuid.UUID:
    values = dict(
        id=uuid.uuid4(),
        candidate_id=fx.candidate_id,
        name="Lifecycle probe",
        description="A project used to prove claim and bounds.",
        submission_kind="repository",
        repository_url="https://github.com/example/probe",
        status="submitted",
    )
    values.update(over)
    async with factory() as s:
        async with s.begin():
            async with superadmin_scope(s):
                s.add(CandidateProject(**values))
    return values["id"]


async def read(factory, project_id) -> CandidateProject:
    async with factory() as s:
        async with s.begin():
            async with superadmin_scope(s):
                return (
                    await s.execute(select(CandidateProject).where(CandidateProject.id == project_id))
                ).scalar_one()


async def test_only_one_delivery_claims_a_submitted_project(world) -> None:
    factory, fx = world
    pid = await add_project(factory, fx)
    async with factory() as s:
        async with superadmin_scope(s):
            assert await lifecycle.claim(s, pid) == lifecycle.CLAIMED
            assert await lifecycle.claim(s, pid) == lifecycle.BUSY
    assert (await read(factory, pid)).status == "processing"


async def test_a_processing_project_with_a_silent_heartbeat_can_be_claimed_again(world) -> None:
    factory, fx = world
    quiet = await add_project(
        factory, fx, status="processing",
        updated_at=datetime.now(timezone.utc) - lifecycle.LEASE - timedelta(minutes=1),
    )
    live = await add_project(
        factory, fx, status="processing", updated_at=datetime.now(timezone.utc)
    )
    async with factory() as s:
        async with superadmin_scope(s):
            assert await lifecycle.claim(s, quiet) == lifecycle.CLAIMED
            assert await lifecycle.claim(s, live) == lifecycle.BUSY
            assert await lifecycle.claim(s, uuid.uuid4()) == lifecycle.MISSING


async def test_a_finished_project_passes_through_to_the_pipeline_rules(world) -> None:
    factory, fx = world
    done = await add_project(factory, fx, status="processed")
    async with factory() as s:
        async with superadmin_scope(s):
            assert await lifecycle.claim(s, done) == lifecycle.PASS_THROUGH


async def test_an_unreadable_repository_is_counted_then_ends_with_a_candidate_sentence(
    world, monkeypatch
) -> None:
    factory, fx = world
    pid = await add_project(factory, fx)

    async def unavailable(session, project_id):
        raise RepositoryUnavailable("github answered 502")

    monkeypatch.setattr(project_pipeline, "process_project", unavailable)
    run = resolve("pickready.process_candidate_project").fn

    for expected in range(1, lifecycle.MAX_FETCH_FAILURES):
        with pytest.raises(RepositoryUnavailable):
            await asyncio.to_thread(run, str(pid))
        row = await read(factory, pid)
        assert row.status == "submitted"
        assert row.telemetry_json["fetch_failures"] == expected
        assert row.status_detail == lifecycle.DETAIL_RETRYING

    await asyncio.to_thread(run, str(pid))  # the bound: recorded, not raised
    ended = await read(factory, pid)
    assert ended.status == "failed_extraction"
    assert ended.failure_code == "repository_unavailable"
    assert ended.status_detail == lifecycle.DETAIL_REPOSITORY_GAVE_UP
    assert "—" not in ended.status_detail


async def test_a_second_delivery_does_nothing_while_a_worker_holds_the_project(
    world, monkeypatch
) -> None:
    factory, fx = world
    pid = await add_project(
        factory, fx, status="processing", updated_at=datetime.now(timezone.utc)
    )
    calls: list[uuid.UUID] = []

    async def spy(session, project_id):  # pragma: no cover - must not run
        calls.append(project_id)

    monkeypatch.setattr(project_pipeline, "process_project", spy)
    await asyncio.to_thread(resolve("pickready.process_candidate_project").fn, str(pid))
    assert calls == []


# -- The intake sweep ---------------------------------------------------------


async def run_sweep() -> None:
    await asyncio.to_thread(resolve("pickready.reconcile_project_intake").fn)


async def test_the_sweep_redispatches_a_lost_project_durably_and_once(world) -> None:
    """Positive control for the floor test below."""
    factory, fx = world
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    pid = await add_project(factory, fx, created_at=old)
    await run_sweep()
    await run_sweep()  # the second pass must not stack another job

    async with factory() as s:
        async with superadmin_scope(s):
            rows = (
                await s.execute(
                    select(BackgroundJob).where(BackgroundJob.resource_id == str(pid))
                )
            ).scalars().all()
    assert len(rows) == 1
    assert rows[0].dedupe_key == f"process_candidate_project:{pid}"
    assert rows[0].idempotent and rows[0].state == "queued"
    assert (await read(factory, pid)).telemetry_json["sweep_redispatches"] == 1


async def test_the_sweep_leaves_a_recent_project_and_a_project_with_a_live_job_alone(world) -> None:
    factory, fx = world
    recent = await add_project(factory, fx)  # created now
    owned = await add_project(
        factory, fx, created_at=datetime.now(timezone.utc) - timedelta(hours=2)
    )
    async with factory() as s:
        async with superadmin_scope(s):
            dispatch.dispatch_after_commit(
                s, "pickready.process_candidate_project", args=[str(owned)],
                resource_type="candidate_project", resource_id=owned, idempotent=True,
            )
            await s.commit()
    await run_sweep()
    assert "sweep_redispatches" not in ((await read(factory, recent)).telemetry_json or {})
    assert "sweep_redispatches" not in ((await read(factory, owned)).telemetry_json or {})
    async with factory() as s:
        async with superadmin_scope(s):
            assert len(
                (await s.execute(select(BackgroundJob).where(BackgroundJob.resource_id == str(owned)))).all()
            ) == 1
            assert await jobs.has_active(s, "candidate_project", owned)


async def test_a_project_the_sweep_could_not_move_ends_as_failed(world) -> None:
    factory, fx = world
    pid = await add_project(
        factory, fx,
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
        telemetry_json={"sweep_redispatches": lifecycle.MAX_STUCK_REDISPATCHES},
    )
    await run_sweep()
    row = await read(factory, pid)
    assert (row.status, row.failure_code) == ("failed_extraction", "analysis_timeout")
    assert row.status_detail == lifecycle.DETAIL_STUCK
