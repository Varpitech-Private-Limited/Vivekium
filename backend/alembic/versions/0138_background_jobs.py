"""Durable job ledger and dispatch outbox: background_jobs.

Revision ID: 0138_background_jobs
Revises: 0137_public_demo

One row per dispatched background task. Written in the transaction of the work
that asked for it (the outbox), then driven through queued, accepted,
capacity_blocked, running and a terminal state by the dispatcher, the worker
and the sweep (`workers/jobs`). Redis stays a progress cache only.

RLS: a tenant session reads and changes only its own tenant's rows. A row with
no tenant (a platform or candidate-owned job) can be INSERTED by any session,
so no dispatch call site can start failing on its session kind, but it can be
read only under the bypass scope. The new table is the only change.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0138_background_jobs"
down_revision = "0137_public_demo"
branch_labels = None
depends_on = None

BYPASS = "current_setting('app.bypass_rls', true) = 'on'"
TENANT = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
ACTIVE = "state NOT IN ('succeeded', 'failed')"
STATES = (
    "queued", "accepted", "capacity_blocked", "running",
    "waiting_external", "retrying", "succeeded", "failed",
)


def _now() -> sa.sql.elements.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "background_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            nullable=True,
            server_default=sa.text(
                "nullif(current_setting('app.tenant_id', true), '')::uuid"
            ),
        ),
        sa.Column("owner_user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("resource_type", sa.String(64), nullable=True),
        sa.Column("resource_id", sa.String(64), nullable=True),
        sa.Column("kind", sa.String(120), nullable=False),
        sa.Column("route", sa.String(8), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("stage", sa.String(120), nullable=True),
        sa.Column("progress_done", sa.Integer(), nullable=True),
        sa.Column("progress_total", sa.Integer(), nullable=True),
        sa.Column("dedupe_key", sa.String(200), nullable=True),
        sa.Column("required_capability", sa.String(100), nullable=True),
        sa.Column("idempotent", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("payload", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failure_class", sa.String(40), nullable=True),
        sa.Column("failure_detail", sa.String(200), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN (" + ", ".join(repr(s) for s in STATES) + ")",
            name="ck_background_jobs_state",
        ),
        sa.CheckConstraint("route IN ('lambda', 'ecs')", name="ck_background_jobs_route"),
    )
    op.create_index(
        "ix_background_jobs_active", "background_jobs", ["state", "queued_at"],
        postgresql_where=sa.text(ACTIVE),
    )
    op.create_index(
        "uq_background_jobs_dedupe_active", "background_jobs", ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text(f"dedupe_key IS NOT NULL AND {ACTIVE}"),
    )
    op.create_index(
        "ix_background_jobs_resource", "background_jobs",
        ["resource_type", "resource_id", "queued_at"],
    )
    op.create_index("ix_background_jobs_kind_done", "background_jobs", ["kind", "completed_at"])

    op.execute("ALTER TABLE background_jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE background_jobs FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY background_jobs_tenant_isolation ON background_jobs
        USING ({TENANT} OR {BYPASS})
        WITH CHECK (tenant_id IS NULL OR {TENANT} OR {BYPASS})
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON background_jobs TO pickready_app")


def downgrade() -> None:
    # A guarded drop: the table is the only record of what was dispatched.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM background_jobs WHERE state NOT IN ('succeeded', 'failed')) THEN
                RAISE EXCEPTION 'background_jobs holds active jobs; drain them before downgrading';
            END IF;
        END $$
        """
    )
    op.drop_table("background_jobs")
