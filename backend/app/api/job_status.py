"""Per-resource background job status: one contract for every long operation.

    GET  /api/v1/job-status/resource/{resource_type}/{resource_id}
    GET  /api/v1/job-status/{job_id}
    POST /api/v1/job-status/{job_id}/retry

The body is `workers/jobs.public_view` and carries the CURRENT STAGE, the times
the ledger measured, a server-written sentence, a percentage only when the
task reported a measurable step, and an estimate only when enough recent runs
of the same kind support one. Nothing is read from Redis, so an expired cache
cannot change the answer.

VISIBILITY, ENFORCED HERE AND BELOW HERE
- Rows are tenant rows under RLS (`get_tenant_db`), so another tenant's job is
  a 404 by the database, not by a WHERE clause.
- Each row names the capability a reader needs (`required_capability`, set by
  the code that dispatched it) and it is checked through RBAC on every request,
  like `require_capability`. A row naming none is visible only to the user who
  started it. Every refusal is a 404: a 403 would say the job exists.
- Candidate-owned jobs have no tenant and are not readable here at all. A
  domain endpoint that has authorised its own resource (a recruiter looking at a
  candidate's project, the candidate looking at their own) reads them with
  `jobs.latest(...)` and `jobs.public_view(...)` from its own session.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user, get_tenant_db
from app.models.background_job import BackgroundJob
from app.services import rbac
from app.services.audit import audit
from app.workers import jobs
from app.workers.dispatch import requeue_after_commit

router = APIRouter()

ACTION_JOB_RETRIED = "background_job_retried"
NOT_FOUND = "No such job."


class JobEstimateOut(BaseModel):
    low_seconds: int
    high_seconds: int
    samples: int


class JobStatusOut(BaseModel):
    job_id: str
    kind: str
    resource_type: str | None
    resource_id: str | None
    state: str
    terminal: bool
    stage: str | None
    message: str
    queued_at: datetime
    started_at: datetime | None
    heartbeat_at: datetime | None
    updated_at: datetime
    completed_at: datetime | None
    attempts: int
    failure_class: str | None
    next_retry_at: datetime | None
    percent: int | None
    estimate: JobEstimateOut | None
    can_retry: bool


async def _visible(session: AsyncSession, user: CurrentUser, row: BackgroundJob | None) -> BackgroundJob:
    if row is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    if row.required_capability:
        allowed = await rbac.has_capability(
            session, user.tenant_id, user.role, row.required_capability, user.user_id
        )
    else:
        allowed = row.owner_user_id is not None and row.owner_user_id == user.user_id
    if not allowed:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return row


async def _present(session: AsyncSession, row: BackgroundJob) -> JobStatusOut:
    estimate = None if row.state in jobs.TERMINAL_STATES else await jobs.typical_duration(session, row.kind)
    return JobStatusOut(**jobs.public_view(row, estimate=estimate))


@router.get("/resource/{resource_type}/{resource_id}", response_model=JobStatusOut)
async def job_for_resource(
    resource_type: str,
    resource_id: str,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_db),
) -> JobStatusOut:
    row = await jobs.latest(session, resource_type, resource_id)
    return await _present(session, await _visible(session, user, row))


@router.get("/{job_id}", response_model=JobStatusOut)
async def job_by_id(
    job_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_db),
) -> JobStatusOut:
    row = (
        await session.execute(select(BackgroundJob).where(BackgroundJob.id == job_id))
    ).scalar_one_or_none()
    return await _present(session, await _visible(session, user, row))


@router.post("/{job_id}/retry", response_model=JobStatusOut)
async def retry_job(
    job_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_db),
) -> JobStatusOut:
    """Put a failed, retry-safe job back in the queue. 409 if it is not one."""
    row = (
        await session.execute(
            select(BackgroundJob).where(BackgroundJob.id == job_id).with_for_update()
        )
    ).scalar_one_or_none()
    row = await _visible(session, user, row)
    if not await jobs.requeue(session, row):
        raise HTTPException(status_code=409, detail="This job cannot be retried.")
    await session.refresh(row)
    await audit(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.user_id,
        action=ACTION_JOB_RETRIED,
        target_type="background_job",
        target_id=row.id,
        metadata={"kind": row.kind},
    )
    requeue_after_commit(session, row)
    return await _present(session, row)
