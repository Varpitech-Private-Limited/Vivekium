import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPKMixin
from app.models.enums import Role


class Tenant(Base, UUIDPKMixin, CreatedAtMixin):
    """One row per client company engagement, protected by RLS.

    The company *profile* (industry / culture / details) lives here rather than
    on `companies` because it is captured at Owner-console onboarding time,
    before the client has ever signed in and authored their company profile. The
    `companies` row remains the client-authored, candidate-facing page.

    RLS uses ``id`` as the tenant discriminator. Provider/BD administration and
    pre-tenant identity lookup use explicit bypass scopes; ordinary org
    sessions see exactly their own row.
    """
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Legacy: the tenant's email domain. Kept (harmless, and still the stable
    # UNIQUE key) but no longer collected in the onboarding UI — it is derived
    # from the owner's email address. Outbound mail is SMTP, so there is no
    # per-tenant sending domain to verify (claude.md rule 5).
    domain: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # Legacy sending-domain verification state. Retained for schema stability;
    # the SMTP sender does not consult it.
    spf_dkim_status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)

    # ── Company profile (migration 0009) ─────────────────────────────────────
    # One of INDUSTRY_CHOICES (schemas/admin.py). Stored as free text so the
    # option list can grow without a migration.
    industry: Mapped[str | None] = mapped_column(String(100))
    culture: Mapped[str | None] = mapped_column(Text)
    # ASSUMPTION: free-form prose (size, HQ, founding year, mission) rather
    # than JSONB — the requirement is narrative context for candidates and for
    # JD generation, not queryable structured fields.
    details: Mapped[str | None] = mapped_column(Text)

    # ── Provider Portal customer lifecycle (migration 0020) ──────────────────
    # `active` | `archived`. Archiving is the REVERSIBLE counterpart to the
    # hard `DELETE /admin/tenants/{id}`: it only hides the customer from the
    # default Provider list — no job, application, report or user is touched,
    # and unarchiving restores the row exactly as it was.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Provider-editable customer metadata. Distinct from `domain` above, which
    # is the immutable internal tenant key derived at onboarding.
    website_domain: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)

    # ── Billing (migrations 0026, 0072, 0132) ─────────────────────────────────
    # A customer IS a tenant, so billing state lives here rather than on
    # `companies` (which does not exist until the client first signs in; see
    # models/billing.py). The monthly subscription columns that used to sit
    # here were dropped by migration 0132 when the product became per-credit
    # only; credits are bought as one-time Razorpay Orders and the balance is
    # the ledger.
    # The stored deficit flag (a copy of "the ledger sums below zero") is
    # DROPPED by migration 0128: nothing read it, and the comment that said it
    # drove a dunning email and a deficit banner described neither. The
    # ledger is the balance; ask `credits.balance_subunits`.
    #
    # A permanent demonstration company (Sarkar Corp, ACRM Corp, Specter & Co.).
    # Billing still RECORDS everything for these tenants -- a demo of a billing
    # page with no usage on it proves nothing -- but it never refuses anything:
    # every billing gate (`credits.has_positive_balance`,
    # `credits.can_start_assessment`) answers yes for it. Kept as a column rather than a UUID list in Python so the
    # exemption is visible in the table and a future demo tenant is an UPDATE
    # rather than a release (migration 0037).
    is_demo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # public demo workspace. Not the permanent is_demo billing exemption.
    public_demo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    current_plan_slug: Mapped[str | None] = mapped_column(String(20))
    pending_plan_slug: Mapped[str | None] = mapped_column(String(20))
    razorpay_subscription_id: Mapped[str | None] = mapped_column(String(100))
    subscription_status: Mapped[str | None] = mapped_column(String(20))
    subscription_current_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    subscription_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: How much an assessed candidate's rank rests on their Tatva Assessment
    #: (the rest is Yukti's resume reading), 0..100 by CHECK, 70 by default
    #: (migration 0122, CONTRACT v2). INTERNAL: read by the ranking SQL and by
    #: nothing that serializes. No route and no capability writes it; a change
    #: is operator SQL until the owner rules on who may set it (PLAN-p2 Q4).
    yukti_assessment_weight_pct: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=70, server_default="70"
    )
    # ── Two-tier credit warnings (Master Directive Part 5 §4) ────────────────
    # "Sent" flags for the LOW (<= 20 credits) and CRITICAL (<= 10 credits)
    # warning emails. Rule 5: BOTH reset to false on every new purchase, so the
    # warning system starts fresh after each top-up. Stored because each is a
    # LATCH (was this warning sent?), which the ledger cannot answer.
    credit_warning_1_sent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    credit_warning_2_sent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # ── Credit-pack purchase state (Master Directive Part 5) ─────────────────
    #: Historical first-purchase flag retained for existing accounts.
    trial_used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    #: Historical setup fee flags retained for invoice compatibility. New
    #: subscriptions and Starter top-ups never charge a setup fee.
    setup_fee_paid: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    #: §5.1: within the first 15 client accounts, the fee was waived.
    setup_fee_waived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    #: Client GSTIN, collected for B2B input-credit purposes (§5.2). Optional;
    #: printed on the invoice when present.
    gstin: Mapped[str | None] = mapped_column(String(20))

    # ── Public employer page (migration 0084) ────────────────────────────────
    # The URL identity of the customer's PUBLIC employer page
    # (/employers/{slug}), from the 2026-09-05 add-features spec ("Employer
    # Page & Content"). It lives on `tenants` and not `companies` for the same
    # reason billing does: a customer IS a tenant, the name the slug derives
    # from lives here, and a `companies` row does not exist until the client
    # first signs in -- a page keyed there would be unreachable for exactly
    # the customer who was just onboarded. Nullable until generated: the
    # migration backfills every existing tenant and the two creation paths
    # (Provider onboarding, BD promotion) assign one at creation via
    # `services/employer_pages.assign_slug`. Lowercase, hyphenated, unique.
    public_slug: Mapped[str | None] = mapped_column(String(140), unique=True)
    # Whether the employer page is publicly visible. A page is served only
    # when this is true AND the customer status is `active`; flipping it off
    # hides the page without touching the slug, so turning it back on
    # restores the same URL.
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )


#: `tenants.status` values. Mirrors the CHECK constraint in migration 0020.
CUSTOMER_ACTIVE = "active"
CUSTOMER_ARCHIVED = "archived"
CUSTOMER_STATUSES: tuple[str, ...] = (CUSTOMER_ACTIVE, CUSTOMER_ARCHIVED)
#: A FOURTH `tenants.status` (migration 0134), beside `prospect`
#: (`models/bd.TENANT_PROSPECT`). A company registered itself and verified its
#: email but has not finished: no paid first purchase, or no password yet.
#: Like a prospect it is not a live customer, so the Provider Portal's lists
#: never show it, and unlike one it has a `client` user who must NOT be able to
#: sign in until `POST /company-onboarding/activate` flips it to `active`
#: (`services/company_onboarding.login_refusal`).
TENANT_ONBOARDING = "onboarding"


class RolePermission(Base, UUIDPKMixin):
    """The RBAC engine's data (ESD §6): permissions are data, not code.
    tenant_id NULL rows form the global default template (Super Admin-editable)."""
    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "role", "capability", name="uq_role_permissions"),
        Index("ix_role_permissions_lookup", "tenant_id", "role", "capability"),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True
    )
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False, length=30), nullable=False)
    capability: Mapped[str] = mapped_column(String(100), nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class AuditLog(Base, UUIDPKMixin):
    """Append-only (no UPDATE/DELETE grants for the app role — enforced in the
    migration). tenant_id NULL for platform-level events.

    Deliberately carries NO foreign key to `tenants`: the trail must survive a
    tenant deletion (api/admin.py delete_tenant relies on this)."""
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_tenant_at", "tenant_id", "at"),)

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(50))
    target_id: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict | None] = mapped_column(JSONB)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", nullable=False
    )

    # ── RBAC_SPECIFICATION.md 30 (migration 0061) ────────────────────────────
    # Nine of these are things 30 names and the blob above was carrying by
    # convention, which meant the Super Admin activity view (31) could not
    # filter on any of them without parsing every row. All nullable, so a
    # rolling deploy has an old writer and a new reader coexisting.
    #
    # `actor_role` is the role AT THE TIME OF THE ACTION, copied rather than
    # joined: a person's role changes, and what authority a past action was
    # taken under does not.
    actor_role: Mapped[str | None] = mapped_column(String(30))
    previous_state: Mapped[dict | None] = mapped_column(JSONB)
    new_state: Mapped[dict | None] = mapped_column(JSONB)
    # 30's "relevant job/application/candidate context". Columns rather than
    # blob keys because every question 31 asks is scoped by one of them.
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    application_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # 30's "source/request metadata".
    request_method: Mapped[str | None] = mapped_column(String(10))
    request_path: Mapped[str | None] = mapped_column(String(512))
    request_ip: Mapped[str | None] = mapped_column(String(64))
    # spec-doc6 4.1: one correlation id, issued at job creation, traceable
    # through every stage and appearing in every audit row for that flow.
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    # 34: an AI-initiated mutation is attributable to BOTH the human
    # principal (actor_user_id, which stays the human, always) and the agent
    # that executed it. Two columns because one cannot hold both, and
    # overloading the actor would make "which human authorised this"
    # unanswerable exactly where it matters most.
    agent_name: Mapped[str | None] = mapped_column(String(50))
    # A 24-asterisked cell was used: allowed, and recorded as a deviation
    # from the canonical flow (7.5 requires the Super Admin override to be
    # recorded; spec-doc6 C13 requires the same of an HR Manager publish).
    exceptional: Mapped[bool | None] = mapped_column(Boolean)

