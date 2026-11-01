"""The durable record of one dispatched background task (migration 0138).

One row per dispatch. It is two things at once, deliberately: the OUTBOX (the
row is written in the same transaction as the work that asked for the task, so
a lost invoke leaves a `queued` row a sweep re-drives) and the JOB STATUS (the
one place a screen asks "is this running, blocked, failed or done"). Redis is a
cache of progress detail and is never consulted for terminal truth.

Domain tables keep their own durable state (`candidate_projects.status`, the
recording row). This table does not replace them; it records the EXECUTION of
the task that moves them, which none of them can: whether a worker was ever
started, when it last proved it was alive, and why it failed.

STATES (`workers/jobs` owns the transitions)
    queued           written, the invoke has not been confirmed accepted
    accepted         the platform accepted the invoke (Lambda 202). Acceptance
                     only: it says nothing about a worker having started
    capacity_blocked RunTask was refused for lack of vCPU quota or memory;
                     retried with backoff, never reported as running
    running          a worker claimed it and is heartbeating
    waiting_external a worker is waiting on a provider (for example Transcribe)
    retrying         a lost worker is being re-driven
    succeeded / failed   terminal
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

STATE_QUEUED = "queued"
STATE_ACCEPTED = "accepted"
STATE_CAPACITY_BLOCKED = "capacity_blocked"
STATE_RUNNING = "running"
STATE_WAITING_EXTERNAL = "waiting_external"
STATE_RETRYING = "retrying"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"

STATES: tuple[str, ...] = (
    STATE_QUEUED,
    STATE_ACCEPTED,
    STATE_CAPACITY_BLOCKED,
    STATE_RUNNING,
    STATE_WAITING_EXTERNAL,
    STATE_RETRYING,
    STATE_SUCCEEDED,
    STATE_FAILED,
)
TERMINAL_STATES = frozenset({STATE_SUCCEEDED, STATE_FAILED})
#: Not yet claimed by a worker: the sweep may re-drive these.
WAITING_TO_START = frozenset(
    {STATE_QUEUED, STATE_ACCEPTED, STATE_CAPACITY_BLOCKED, STATE_RETRYING}
)
#: A worker owns it and proves liveness with a heartbeat.
IN_FLIGHT = frozenset({STATE_RUNNING, STATE_WAITING_EXTERNAL})

#: Mirrors the partial indexes: "active" is every non-terminal state.
_ACTIVE_SQL = "state NOT IN ('succeeded', 'failed')"


class BackgroundJob(Base):
    __tablename__ = "background_jobs"
    __table_args__ = (
        CheckConstraint(
            "state IN (" + ", ".join(repr(s) for s in STATES) + ")",
            name="ck_background_jobs_state",
        ),
        CheckConstraint("route IN ('lambda', 'ecs')", name="ck_background_jobs_route"),
        Index(
            "ix_background_jobs_active",
            "state",
            "queued_at",
            postgresql_where=text(_ACTIVE_SQL),
        ),
        Index(
            "uq_background_jobs_dedupe_active",
            "dedupe_key",
            unique=True,
            postgresql_where=text(f"dedupe_key IS NOT NULL AND {_ACTIVE_SQL}"),
        ),
        Index("ix_background_jobs_resource", "resource_type", "resource_id", "queued_at"),
        Index("ix_background_jobs_kind_done", "kind", "completed_at"),
    )
    # The server default on `tenant_id` is read back by nothing, and a RETURNING
    # of it would run the SELECT policy over a NULL-tenant row.
    __mapper_args__ = {"eager_defaults": False}

    #: The run id `dispatch` hands the caller, so a polling client's id and the
    #: row's key are one value.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    #: Defaults to the tenant of the session that wrote the row (NULL under a
    #: bypass session: a platform or candidate-owned job).
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        server_default=text("nullif(current_setting('app.tenant_id', true), '')::uuid"),
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(120), nullable=False)
    route: Mapped[str] = mapped_column(String(8), nullable=False)
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=STATE_QUEUED
    )
    stage: Mapped[str | None] = mapped_column(String(120))
    progress_done: Mapped[int | None] = mapped_column(Integer)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    #: The capability a reader needs; checked per request through RBAC.
    required_capability: Mapped[str | None] = mapped_column(String(100))
    #: The task is safe to run again after a worker was lost mid-run. Only then
    #: may the sweep re-drive a RUNNING job or a person retry a failed one.
    idempotent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    #: {"args": [...], "kwargs": {...}}. Cleared on success.
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: Executions a worker started.
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    #: Times the sweep re-drove the invoke.
    dispatch_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    failure_class: Mapped[str | None] = mapped_column(String(40))
    #: A safe code or exception CLASS NAME, never a message that can quote a row.
    failure_detail: Mapped[str | None] = mapped_column(String(200))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
