import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPKMixin
from app.models.enums import Role, UserStatus


class User(Base, UUIDPKMixin, CreatedAtMixin):
    """tenant_id is NULL for super_admin (platform-wide) and candidates
    (external actors; candidate data lives in `candidates`).

    Tenant users are RLS-isolated. Login and workspace selection are the
    structural exception because they must resolve memberships before a tenant
    exists; those handlers use ``get_identity_session`` explicitly.
    """
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", "role", name="uq_users_tenant_email_role"),
        Index("ix_users_email", "email"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True
    )
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False, length=30), nullable=False)
    # Nullable: a phone-only candidate (Firebase phone provider) has no email
    # (migration 0004). RLS/owner invariant unaffected.
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20))
    # Landline WITH extension, as one free-text field (migration 0020) —
    # "+91-22-1234-5678 ext. 101" is a single thing a human types and reads,
    # and splitting it would only invite two half-filled columns. Shown on the
    # Provider Portal's primary-contact panel; edited by its owner, never by
    # the Provider.
    landline: Mapped[str | None] = mapped_column(String(50))
    full_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, native_enum=False, length=20), nullable=False, default=UserStatus.invited
    )
    # First client login must dual-verify email AND mobile (FR-1.2/1.3)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    firebase_uid: Mapped[str | None] = mapped_column(String(128), unique=True)
    auth_providers: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    # ── Per-user permission overlay (migration 0016) ─────────────────────────
    # The HR Head's per-person grants (spec §7.1). NULL means "inherit the role
    # default" resolved by services/rbac from `role_permissions`. A dict is a
    # SPARSE overlay of {capability: bool} that wins over the role default for
    # this one user — capabilities absent from the dict keep tracking the role,
    # so a later change to the role matrix still reaches everyone it should.
    #
    # This stays DATA, not code: gating is still require_capability(...), never
    # `if role == ...` (claude.md rule 3).
    permissions_json: Mapped[dict | None] = mapped_column(JSONB)

    # ── Who this person reports to (migration 0050, spec §29) ────────────────
    # The customer portal is a four-level hierarchy and each level decides the
    # permissions of the one beneath it. This records the reporting line inside
    # a tenant; the RULE about who may manage whom is rank-based and lives in
    # `services/role_hierarchy`, so a missing manager never grants access it
    # would not otherwise have.
    #
    # ON DELETE SET NULL: a manager leaving must not cascade away their whole
    # team, and a user whose manager is gone falls back to the rank rule alone.
    manager_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # ── The one department a Functional Head belongs to (migration 0133) ─────
    # Required for `functional_head` and NULL for every other role, by a
    # database CHECK (`ck_users_department_iff_functional_head`), and it can
    # only name a department of the user's OWN tenant (a composite foreign key
    # onto company_departments(id, tenant_id)). `services/department_access`
    # reads this column, never the role, to decide what a person may see.
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # public demo workspace. Not the permanent is_demo billing exemption.
    public_demo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

