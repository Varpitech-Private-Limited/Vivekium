"""The execution lifecycle of a candidate project: claim, heartbeat, bounded failure.

`pipeline.process_project` turns a submission into evidence. This module owns
the part around it that a lost or repeated dispatch exposes (2026-11-01):

- CLAIM. Two deliveries of one project (a Lambda redelivery, a sweep re-drive
  beside a slow start) must not both run the pipeline. `claim` moves
  `submitted` to `processing` with one conditional UPDATE, and a `processing`
  row with a fresh `updated_at` belongs to a live worker, so the second
  delivery stands down. `updated_at` IS the heartbeat: the task body touches it
  while it works, so a `processing` row whose `updated_at` has gone quiet is a
  lost worker and may be claimed again.
- A TRANSIENT FETCH FAILURE IS RECORDED, NOT LOST. `RepositoryUnavailable`
  escapes the pipeline by design, and the escape rolled back the pipeline's
  own writes, so a project whose repository could not be read stayed
  `submitted` with nothing on the row for ever (live, 2026-10-31: three
  projects, twelve failed runs, no change on any row). `record_fetch_failure`
  counts the failure on the row and, at the bound, ends the project as
  `failed_extraction` with a sentence the candidate can act on.
- A STUCK PROJECT HAS AN END. `fail_stuck` is what the intake sweep does after
  its re-dispatches have not moved a row.

Every status_detail here is read by a candidate: plain words, no internals.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import (
    STATUS_FAILED_EXTRACTION,
    STATUS_PROCESSING,
    STATUS_SUBMITTED,
    CandidateProject,
)

#: A `processing` row untouched for this long has lost its worker.
LEASE = timedelta(minutes=10)
#: How often the task body touches `updated_at` while it works.
HEARTBEAT_SECONDS = 60
#: Failed attempts to read a repository before the project is ended. Two task
#: runs of two in-process attempts each.
MAX_FETCH_FAILURES = 4
#: Intake-sweep re-dispatches of a project that never reached a result.
MAX_STUCK_REDISPATCHES = 5
#: A `submitted` row older than this has had its dispatch lost.
STUCK_AFTER = timedelta(minutes=30)

DETAIL_RETRYING = (
    "We could not reach your repository just now. We will try again shortly."
)
DETAIL_REPOSITORY_GAVE_UP = (
    "We could not read your repository after several tries. Check that the "
    "link is correct and the repository is public, then submit the project again."
)
DETAIL_STUCK = (
    "The analysis did not finish. Please submit the project again."
)

CLAIMED = "claimed"
BUSY = "busy"
PASS_THROUGH = "pass"
MISSING = "missing"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def claim(session: AsyncSession, project_id: uuid.UUID) -> str:
    """Take the project for this worker, or learn that another worker has it.

    Returns CLAIMED, BUSY (a live worker holds it), MISSING, or PASS_THROUGH for
    every other status, which the pipeline already handles by its own rules (a
    processed project returns at once, a partial one completes its AI stage).
    Reads columns, not the entity, so the identity map holds no stale status.
    """
    current = (
        await session.execute(
            select(CandidateProject.status).where(CandidateProject.id == project_id)
        )
    ).scalar_one_or_none()
    if current is None:
        return MISSING
    if current not in (STATUS_SUBMITTED, STATUS_PROCESSING):
        return PASS_THROUGH
    now = _now()
    claimed = (
        await session.execute(
            update(CandidateProject)
            .where(
                CandidateProject.id == project_id,
                or_(
                    CandidateProject.status == STATUS_SUBMITTED,
                    and_(
                        CandidateProject.status == STATUS_PROCESSING,
                        or_(
                            CandidateProject.updated_at.is_(None),
                            CandidateProject.updated_at < now - LEASE,
                        ),
                    ),
                ),
            )
            .values(
                status=STATUS_PROCESSING,
                status_detail="Your project is being analysed.",
                updated_at=now,
            )
            .returning(CandidateProject.id)
        )
    ).first()
    await session.commit()
    return CLAIMED if claimed is not None else BUSY


async def touch(session: AsyncSession, project_id: uuid.UUID) -> None:
    """The heartbeat: only a row still `processing` is touched."""
    await session.execute(
        update(CandidateProject)
        .where(
            CandidateProject.id == project_id,
            CandidateProject.status == STATUS_PROCESSING,
        )
        .values(updated_at=_now())
    )
    await session.commit()


async def record_fetch_failure(session: AsyncSession, project_id: uuid.UUID) -> bool:
    """Count a repository fetch failure on the row. True when it ended the project.

    The caller has rolled the pipeline's session back; this re-reads the row.
    Below the bound the project returns to `submitted` with a sentence saying it
    will be tried again; at the bound it is `failed_extraction`, which the sweep
    does not retry and the candidate can resubmit from.
    """
    project = (
        await session.execute(
            select(CandidateProject)
            .where(CandidateProject.id == project_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if project is None:
        return False
    telemetry = dict(project.telemetry_json or {})
    failures = int(telemetry.get("fetch_failures", 0)) + 1
    telemetry["fetch_failures"] = failures
    project.telemetry_json = telemetry
    project.updated_at = _now()
    ended = failures >= MAX_FETCH_FAILURES
    if ended:
        project.status = STATUS_FAILED_EXTRACTION
        project.failure_code = "repository_unavailable"
        project.status_detail = DETAIL_REPOSITORY_GAVE_UP
    else:
        project.status = STATUS_SUBMITTED
        project.status_detail = DETAIL_RETRYING
    await session.commit()
    return ended


def is_stuck(project: CandidateProject, now: datetime) -> bool:
    """A `submitted` row past `STUCK_AFTER`, or a `processing` row that lost its heartbeat."""
    if project.status == STATUS_SUBMITTED:
        return project.created_at < now - STUCK_AFTER
    if project.status == STATUS_PROCESSING:
        beat = project.updated_at or project.created_at
        return beat < now - LEASE
    return False


def fail_stuck(project: CandidateProject) -> None:
    """End a project the sweep has re-dispatched to no effect."""
    project.status = STATUS_FAILED_EXTRACTION
    project.failure_code = "analysis_timeout"
    project.status_detail = DETAIL_STUCK
    project.updated_at = _now()
