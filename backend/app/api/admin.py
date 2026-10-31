"""Owner console (contract rev 2, formerly "Super Admin"). Every request runs
through `get_superadmin_db`, which enforces the super_admin audience, uses the
RLS bypass scope, and writes an audit_log row for the cross-tenant access.

What is left here is the Owner's own surface, and nothing that duplicates the
Provider Portal: onboarding a customer (`POST /tenants`), the BD team, the
LLM and cost telemetry, and the three DECLARED OPERATOR ROUTES (the audit log,
the Super Admin transfer and the retyped-name tenant delete), which have no
screen by design and are listed with their reasons in
`tests/test_route_callers.OPERATOR_SURFACE`.

The tenant list, tenant edit, cross-tenant staff list, Owner-side staff
invitation and the permission-template editor were DELETED in the Vivekium
release (PLAN-p7 WP-B6). None had a screen, `/provider/*` serves the reads,
and three of them were Provider WRITES over customer data, which the
read-only-by-absence rule of 2026-07-27 forbids and which bypassed
`test_provider_portal.PROVIDER_WRITES`.

Auth is Firebase (claude.md rule 2): no OTP is ever generated here. Email is
SMTP via the dispatched `pickready.send_email` task (rules 4 and 5).
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentUser,
    get_current_user,
    get_superadmin_db,
)
from app.config import llm_providers
from app.core.config import get_settings
from app.models.enums import Role, UserStatus
from app.models.invite import (
    StaffInvite,
    build_invite_link,
    generate_invite_token,
    hash_invite_token,
    invite_expiry,
)
from app.models.bd import BDLead
from app.models.tenant import AuditLog, RolePermission, Tenant
from app.models.user import User
from app.schemas.admin import (
    SuperAdminTransferIn,
    AdminUserOut,
    AuditLogOut,
    BDUserCreateIn,
    BDUserDeleteOut,
    BDUserOut,
    BDUserUpdateIn,
    TenantCreateIn,
    TenantCreateOut,
    TenantDeleteOut,
    TenantOut,
    derive_tenant_domain,
)
from app.services import cost_telemetry, employer_pages, rbac, tenant_deletion
from app.services.audit import audit, record_action
from app.services.capabilities import DEFAULT_PERMISSION_MATRIX
from app.services.owner import OwnerRoleViolation, ensure_owner_invariant
from app.workers.dispatch import dispatch_after_commit
from app.services import llm_router
from app.services import role_hierarchy

router = APIRouter()

# The customer's staff roles, the set the Provider Portal's team view reads
# (`api/provider.py` imports it from here).
STAFF_ROLES: frozenset[Role] = role_hierarchy.MANAGEABLE_ROLES


def _seed_permissions(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Copy the default template (PRD §6) into tenant rows so the Owner can
    tune this tenant independently later."""
    for role, caps_map in DEFAULT_PERMISSION_MATRIX.items():
        for capability, allowed in caps_map.items():
            session.add(RolePermission(
                tenant_id=tenant_id, role=role, capability=capability, allowed=allowed
            ))


# ── Pure helpers (DB-free, unit-testable) ────────────────────────────────────

def next_free_domain(preferred: str, taken: set[str]) -> str:
    """`tenants.domain` is UNIQUE but is no longer user-supplied, so a
    collision (two companies whose owners share a mail host) must not surface
    as a 409 the Owner cannot resolve. Suffix until free."""
    if preferred not in taken:
        return preferred
    stem, _, tld = preferred.partition(".")
    for n in range(2, 1000):
        nth = f"{stem}-{n}.{tld}" if tld else f"{preferred}-{n}"
        if nth not in taken:
            return nth
    raise HTTPException(status_code=409, detail="Could not allocate a tenant key")


def confirmation_matches(typed: str | None, tenant_name: str) -> bool:
    """Destructive-delete guard: the caller must retype the company name.
    Case- and whitespace-insensitive so the check is about intent, not typing
    precision. A literal "DELETE" is also accepted."""
    value = (typed or "").strip().casefold()
    if not value:
        return False
    return value in {tenant_name.strip().casefold(), "delete"}


# ── Serialization ────────────────────────────────────────────────────────────

def _tenant_out(tenant: Tenant, client: User | None, staff_count: int = 0) -> TenantOut:
    return TenantOut(
        id=tenant.id,
        name=tenant.name,
        domain=tenant.domain,
        spf_dkim_status=tenant.spf_dkim_status,
        created_at=tenant.created_at,
        industry=tenant.industry,
        culture=tenant.culture,
        details=tenant.details,
        client_email=client.email if client else None,
        client_name=client.full_name if client else None,
        client_phone=client.phone if client else None,
        client_status=client.status.value if client else None,
        staff_count=staff_count,
    )


# ── Tenants ──────────────────────────────────────────────────────────────────

@router.post("/tenants", response_model=TenantCreateOut, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    body: TenantCreateIn,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_superadmin_db),
) -> TenantCreateOut:
    """Onboard a client company and its Client Company Admin account.

    The admin signs in with email/password or Google via Firebase — Vivekium
    stores no password and issues no OTP (claude.md rule 2). No sending domain
    is collected: outbound mail is SMTP (rule 5).
    """
    # Defensive owner invariant: no user-creating path may ever mint a
    # super_admin identity other than settings.owner_email (contract rev 2).
    try:
        ensure_owner_invariant(Role.client, str(body.client_email))
    except OwnerRoleViolation as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    name = body.name.strip()
    duplicate_name = (
        await session.execute(
            select(Tenant).where(func.lower(Tenant.name) == name.lower())
        )
    ).scalars().first()
    if duplicate_name is not None:
        raise HTTPException(
            status_code=409, detail=f"A company named “{name}” already exists"
        )

    preferred = (body.domain or derive_tenant_domain(name, str(body.client_email))).lower()
    taken = set(
        (await session.execute(select(Tenant.domain))).scalars().all()
    )
    domain = next_free_domain(preferred, taken)

    tenant = Tenant(
        name=name,
        domain=domain,
        industry=body.industry,
        culture=body.culture,
        details=body.details,
    )
    session.add(tenant)
    await session.flush()
    # Every customer gets a public employer-page slug at creation (2026-09-05
    # add-features spec); visibility is governed separately by `is_public`.
    await employer_pages.assign_slug(session, tenant)

    client_user = User(
        tenant_id=tenant.id,
        role=Role.client,
        email=str(body.client_email),
        phone=body.client_phone,
        full_name=name,
        status=UserStatus.invited,
    )
    session.add(client_user)
    _seed_permissions(session, tenant.id)
    await session.flush()
    # The new tenant's permission rows were just written; drop any cached
    # resolution for it. This line used to read `body.tenant_id` and
    # `body.entries`, fields `TenantCreateIn` does not have (a copy of the
    # deleted permission editor's invalidation), so onboarding a customer
    # raised AttributeError after the flush and rolled the whole thing back.
    await rbac.invalidate_role_permissions(tenant.id)

    await audit(
        session, tenant_id=tenant.id, actor_user_id=user.user_id,
        action="tenant_created", target_type="tenant", target_id=tenant.id,
        metadata={"name": name, "domain": domain, "industry": body.industry},
    )
    # Firebase owns credentials: the invite is an onboarding pointer, never a
    # code. Sending is a dispatched task (rule 4) over SMTP (rule 5).
    #
    # The email used to carry only the tenant's name and NO acceptance link,
    # because no StaffInvite row was ever minted for the client owner — so the
    # person the workspace was created for had no way into it. Mint the same
    # single-use invite the staff path mints (see create_staff_invite below).
    client_token = generate_invite_token()
    session.add(
        StaffInvite(
            tenant_id=tenant.id,
            user_id=client_user.id,
            email=str(body.client_email),
            role=Role.client.value,
            token_hash=hash_invite_token(client_token),
            invited_by=user.user_id,
            expires_at=invite_expiry(),
        )
    )
    await session.flush()
    # After the commit, never before it: an invitation about a tenant whose
    # creation then rolled back would point its recipient at nothing. A lost
    # invoke is logged by the dispatcher, and the Provider Portal's primary
    # contact route (`PUT /provider/customers/{id}/primary-contact`) re-sends
    # the invitation, which is the repair.
    dispatch_after_commit(
        session,
        "pickready.send_email",
        args=[str(tenant.id), str(body.client_email), "client_invite",
              {"tenant_name": name,
               "invite_link": build_invite_link(
                   get_settings().frontend_url, client_token
               )}],
    )
    return TenantCreateOut(
        tenant=_tenant_out(tenant, client_user),
        client_user=AdminUserOut.model_validate(client_user),
    )


async def _load_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


@router.delete("/tenants/{tenant_id}", response_model=TenantDeleteOut)
async def delete_tenant(
    tenant_id: uuid.UUID,
    confirm: str = Query(
        ...,
        description="Retype the company name (or 'DELETE') to confirm this "
                    "irreversible action.",
    ),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_superadmin_db),
) -> TenantDeleteOut:
    """Permanently remove a tenant and everything scoped to it.

    Runs inside the request transaction (`get_superadmin_db` opens
    `session.begin()`), so a failure anywhere rolls the whole thing back.

    Rows with a FK to `tenants` cascade at the database level: users,
    staff_invites, companies, hiring_managers, email_templates, jobs,
    job_approvals, job_candidate_links, verification_requests, interviews,
    pipeline_status, role_permissions.

    Two tables carry a tenant reference WITHOUT a FK, on purpose:
      * `candidates.tenant_id` / `profiles.source_tenant_id` — Databank rows
        may be shared across tenants, so they are RELEASED (set NULL) rather
        than deleted. Deleting them would destroy other tenants' matches.
      * `audit_log.tenant_id` — append-only; history survives the tenant.
    """
    tenant = await _load_tenant(session, tenant_id)
    if not confirmation_matches(confirm, tenant.name):
        raise HTTPException(
            status_code=400,
            detail="Confirmation does not match the company name",
        )

    name = tenant.name
    # The one implementation, shared with the operator's cleanup script
    # (services/tenant_deletion carries the cascade and release rules).
    removed = await tenant_deletion.delete_tenant(
        session, tenant, actor_user_id=user.user_id
    )
    return TenantDeleteOut(id=tenant_id, name=name, removed=removed)


# ── Owner-side team management (invite staff INTO a tenant) ──────────────────

# ── Business Development team (platform staff, tenant_id NULL) ───────────────
#
# WHY THIS IS NOT THE STAFF-INVITE FLOW ABOVE. Every invite path in the product
# is tenant-scoped: `staff_invites` has a NOT NULL tenant, the invite email is
# rendered from a tenant's template, and `/join` accepts into a tenant. A BD
# user has no tenant by design, so reusing that machinery would mean inventing
# a fake one. The identity model is nonetheless IDENTICAL to the invite flow:
# the row is reserved here with status `invited` and no firebase_uid, and the
# first proven Firebase sign-in on this email binds the uid and flips the row
# to active (api/auth._finalize_single). No password is ever created or stored.
#
# Gating is `get_superadmin_db`, the same dependency as every other route in
# this module: it demands the owner audience AND the super_admin role, and
# super_admin short-circuits capability resolution, so no new capability is
# introduced. What a BD user may DO once signed in is decided by the three
# `bd` capabilities seeded in migration 0023, never by a role branch.

def _bd_user_out(user: User) -> BDUserOut:
    return BDUserOut(
        id=user.id,
        email=user.email or "",
        full_name=user.full_name,
        phone=user.phone,
        status=user.status.value,
        created_at=user.created_at,
        signed_in=bool(user.firebase_uid),
    )


async def _load_bd_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    row = (
        await session.execute(
            select(User).where(User.id == user_id, User.role == Role.bd)
        )
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Business Development user not found")
    return row


@router.get("/bd-users", response_model=list[BDUserOut])
async def list_bd_users(
    session: AsyncSession = Depends(get_superadmin_db),
) -> list[BDUserOut]:
    """Every Business Development account, newest last."""
    rows = (
        await session.execute(
            select(User)
            .where(User.role == Role.bd)
            .order_by(User.created_at, User.email)
        )
    ).scalars().all()
    return [_bd_user_out(row) for row in rows]


@router.post(
    "/bd-users", response_model=BDUserOut, status_code=status.HTTP_201_CREATED
)
async def create_bd_user(
    body: BDUserCreateIn,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_superadmin_db),
) -> BDUserOut:
    """Reserve a Business Development account for an email address.

    The account is usable as soon as its owner signs in at the normal login
    page with this email, through Google or email/password. Vivekium never
    holds the credential.
    """
    email = str(body.email)
    # `uq_users_tenant_email_role` cannot catch this: NULLs do not collide under
    # a Postgres UNIQUE constraint, so two BD rows for one address would both
    # insert happily and the login lookup would then return an ambiguous pair.
    existing = (
        await session.execute(
            select(User).where(
                User.tenant_id.is_(None),
                User.role == Role.bd,
                User.public_demo.is_(False),
                func.lower(User.email) == email.strip().lower(),
            )
        )
    ).scalars().first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{email} already holds a Business Development account",
        )
    try:
        ensure_owner_invariant(Role.bd, email)
    except OwnerRoleViolation as exc:  # pragma: no cover - bd is never super_admin
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    bd_user = User(
        tenant_id=None, role=Role.bd, email=email, phone=body.phone,
        full_name=body.full_name, status=UserStatus.invited,
    )
    session.add(bd_user)
    await session.flush()
    await audit(
        session, tenant_id=None, actor_user_id=user.user_id,
        action="bd_user_created", target_type="user", target_id=bd_user.id,
        metadata={"email": email, "full_name": body.full_name},
    )
    return _bd_user_out(bd_user)


@router.patch("/bd-users/{bd_user_id}", response_model=BDUserOut)
async def update_bd_user(
    bd_user_id: uuid.UUID,
    body: BDUserUpdateIn,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_superadmin_db),
) -> BDUserOut:
    """Edit a BD user's details, or disable / re-enable the account.

    Disabling is a REVERSIBLE hide: it sets `status` and nothing else, so a BD
    user who owns leads keeps owning them and can be switched back on. There is
    no delete here, exactly as there is none on the customer list.
    """
    bd_user = await _load_bd_user(session, bd_user_id)
    changes = body.model_dump(exclude_unset=True)
    rebound = False
    if changes.get("email"):
        email = str(changes["email"]).strip()
        if email.lower() != (bd_user.email or "").strip().lower():
            # The same two guards the create path runs, because an edit that
            # can mint what a create refuses is a second door.
            duplicate = (
                await session.execute(
                    select(User).where(
                        User.tenant_id.is_(None),
                        User.role == Role.bd,
                        func.lower(User.email) == email.lower(),
                        User.id != bd_user.id,
                    )
                )
            ).scalars().first()
            if duplicate is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"{email} already holds a Business Development account",
                )
            try:
                ensure_owner_invariant(Role.bd, email)
            except OwnerRoleViolation as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            bd_user.email = email
            # REBIND, never a quiet field write: the email is the identity
            # Firebase binds to (auth matches by uid OR email), so a bound
            # account keeping its old uid would leave the OLD person signed
            # in under the NEW address. Clearing the binding returns the row
            # to invited; the new address binds on its first sign-in and the
            # old one matches nothing.
            if bd_user.firebase_uid:
                bd_user.firebase_uid = None
                rebound = True
            bd_user.email_verified_at = None
            if bd_user.status != UserStatus.disabled:
                bd_user.status = UserStatus.invited
    if "full_name" in changes:
        bd_user.full_name = changes["full_name"]
    if "phone" in changes:
        bd_user.phone = changes["phone"]
    if "status" in changes and changes["status"] is not None:
        if changes["status"] == "disabled":
            bd_user.status = UserStatus.disabled
        else:
            # Re-enable to the truthful state: an account that has never been
            # bound to a Firebase identity is still only invited.
            bd_user.status = (
                UserStatus.active if bd_user.firebase_uid else UserStatus.invited
            )
    await session.flush()
    await audit(
        session, tenant_id=None, actor_user_id=user.user_id,
        action="bd_user_updated", target_type="user", target_id=bd_user.id,
        metadata={"changed": sorted(changes), "status": bd_user.status.value,
                  "rebound": rebound},
    )
    return _bd_user_out(bd_user)


@router.delete("/bd-users/{bd_user_id}", response_model=BDUserDeleteOut)
async def delete_bd_user(
    bd_user_id: uuid.UUID,
    confirm: str = Query(
        ...,
        description="Retype the account's email address (or 'DELETE') to "
                    "confirm this irreversible action.",
    ),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_superadmin_db),
) -> BDUserDeleteOut:
    """Permanently remove a Business Development account.

    Added 2026-08-09 at the client's request. It reverses the standing "there is
    no delete route" position, and the reason that position existed is exactly
    what this handler has to protect: a BD rep OWNS LEADS
    (`bd_leads.owner_user_id`), and a lead is the pipeline, not the rep.

    So the cascade is deliberately asymmetric:

      * Leads are RELEASED, never deleted. `owner_user_id` is set NULL and the
        rows stay on the BD board as unassigned, ready to be reassigned. A rep
        leaving must not take the company's pipeline with them, and the
        alternative failure is silent: nobody notices the leads are gone until
        someone asks after a deal that no longer exists.
      * A lead that was promoted to a TENANT is untouched by construction, since
        `bd_leads.promoted_tenant_id` hangs off the lead and not off the rep.
        Deleting a salesperson can never archive a paying customer.
      * The `audit_log` rows naming this user survive. They are append-only
        history and answer "who created this lead", which outlives the account.

    Disable remains, and remains the right answer for someone who may come back:
    it is reversible and it keeps their ownership intact. This is for an account
    created in error or an address that must genuinely leave the system.
    """
    bd_user = await _load_bd_user(session, bd_user_id)
    email = bd_user.email or ""
    # Same guard as the tenant delete: retype the thing being destroyed. The
    # check is about intent, not typing precision.
    if not confirmation_matches(confirm, email):
        raise HTTPException(
            status_code=400,
            detail="Confirmation does not match the account's email address",
        )

    released = await session.execute(
        update(BDLead)
        .where(BDLead.owner_user_id == bd_user_id)
        .values(owner_user_id=None)
    )
    leads_released = released.rowcount or 0

    await session.delete(bd_user)
    await session.flush()
    await audit(
        session, tenant_id=None, actor_user_id=user.user_id,
        action="bd_user_deleted", target_type="user", target_id=bd_user_id,
        metadata={"email": email, "leads_released": leads_released},
    )
    return BDUserDeleteOut(id=bd_user_id, email=email, leads_released=leads_released)


# ── Org-portal: the caller's own company profile ─────────────────────────────

# ── Permissions & audit log ──────────────────────────────────────────────────

@router.get("/audit-log", response_model=list[AuditLogOut])
async def list_audit_log(
    tenant_id: uuid.UUID | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    session: AsyncSession = Depends(get_superadmin_db),
) -> list[AuditLogOut]:
    # `id` in the ORDER BY makes it total: audit rows written in the same
    # millisecond would otherwise be free to swap between pages.
    stmt = (
        select(AuditLog)
        .order_by(AuditLog.at.desc(), AuditLog.id)
        .offset(skip)
        .limit(limit)
    )
    if tenant_id is not None:
        stmt = stmt.where(AuditLog.tenant_id == tenant_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [AuditLogOut.model_validate(r) for r in rows]


# ── LLM roster observability (2026-08-11) ────────────────────────────────────

@router.get("/llm/stats")
async def llm_stats(
    session: AsyncSession = Depends(get_superadmin_db),
) -> dict:
    """Per-model and per-credential health, latency, tokens and estimated cost.

    `llm_router.provider_stats()` has existed for a long time and was exposed
    NOWHERE, which makes it a monitoring surface nobody can read. It is exposed
    here alongside two finer breakdowns.

    WHAT CHANGED WITH THE SINGLE-VENDOR CONSOLIDATION (spec-doc5 Part B).
    Per-KEY counters were added on 2026-08-11 to answer "which of the seven keys
    on this provider is failing", and with one credential that question no
    longer exists. `models` replaces it as the breakdown that still varies:
    gpt-5.6-terra and gpt-5.6-luna have different latency profiles and a price
    difference, so "which model is spending the budget" is now the operator
    question the per-provider table used to answer. `keys` is retained because
    it is where the fingerprint and the breaker state are visible.

    Owner-only, through `get_superadmin_db`, which also writes the audit row.

    No key material crosses this boundary. Counters are filed under a
    FINGERPRINT -- twelve hex characters of SHA-256 -- which is the same
    non-secret handle the breaker and every log line already use, and that is
    asserted in `tests/test_llm_key_telemetry.py` rather than left to review.

    The cost figure is named `estimated_cost_usd` and is list price from
    `config/llm_providers.TOKEN_PRICES_USD_PER_MILLION`, not an invoice: prompt
    caching, batch discounts and the vendor's own rounding all move the real
    number. What it is good for is the ordering -- which model, which task --
    which stays right even when the absolute figure is not.

    Counters are per PROCESS and reset on restart. That is a real limitation and
    is stated in the payload rather than left for a reader to discover: a zero
    here means "nothing since this worker started", never "nothing ever".
    """
    return {
        "providers": llm_router.provider_stats(),
        "models": llm_router.model_stats(),
        "keys": llm_router.key_stats(),
        "configured_keys": llm_providers.configured_key_count(),
        "scope": "this process, since it started",
        "cost_basis": "list price estimate, not an invoice",
    }


# ── Per-assessment cost telemetry (change 28D) ───────────────────────────────

@router.get("/cost/assessments")
async def assessment_cost_summary(
    month: str | None = Query(
        None,
        description="Calendar month as YYYY-MM in UTC. Defaults to the current one.",
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    session: AsyncSession = Depends(get_superadmin_db),
) -> dict:
    """What assessments cost the platform this month: total, average, per client.

    INTERNAL ONLY, AND THE BOUNDARY IS THE AUDIENCE RATHER THAN A COMMENT.
    Behind `get_superadmin_db`, exactly like `/admin/llm/stats`: the super_admin
    audience, the RLS bypass scope, and an audit row for the cross-tenant read.
    `VIEW_INTELLIGENCE_DASHBOARDS` is deliberately not what gates this, and the
    difference matters -- that capability is held by TENANT users, so gating on
    it would hand one customer a per-client cost breakdown of every other
    customer. `tests/test_assessment_cost_dashboard.py` asserts that an org
    audience and a candidate audience are both refused.
    That is also why the per-assessment record exists on its own table rather
    than joining the intelligence dashboards: those are read by the tenant.

    WHAT THE NUMBERS ARE. Estimates, and every one of them says so in place.
    The per-token rates come from `config/llm_providers`, which is the one
    source and which carries an honest caveat of its own: they are unverified
    for the two model ids in use, so what they encode reliably is the RATIO
    between tiers rather than an absolute. Prompt-cache hits are counted and
    are NOT discounted, because no cached-input rate is on file to discount
    them by. The rupee figures ride on a fixed FX rate from settings, so they
    are approximate by construction; `fx.basis` on the payload says that rather
    than leaving a reader to assume a live quote.

    WHAT IS ABSENT IS ABSENT. An average over zero assessments reports
    `status: "unavailable"` with a reason and carries no number at all, and the
    Rs threshold flag then answers `exceeded: null` rather than `false`: a
    month nobody measured has not been shown to be under the line. Media and
    proctoring cost report unavailable permanently, because the analysis
    service is billed per account and proctoring stores no media, so there is
    no per-assessment figure to read.
    """
    settings = get_settings()
    start, end = _month_window(month)
    return {
        "month": f"{start.year:04d}-{start.month:02d}",
        **await cost_telemetry.owner_cost_summary(
            session,
            window_start=start,
            window_end=end,
            usd_to_inr=settings.usd_to_inr_rate,
            alert_threshold_inr=settings.assessment_cost_alert_inr,
        ),
    }


def _month_window(month: str | None) -> tuple[datetime, datetime]:
    """The half-open UTC window `[first of the month, first of the next)`.

    UTC rather than a local zone, because the rows are stamped in UTC and a
    window in one zone over timestamps in another silently moves a few hours of
    spend between two months every month. Half-open so twelve of these tile a
    year without the instant at midnight being counted twice.
    """
    now = datetime.now(timezone.utc)
    year, month_number = (now.year, now.month)
    if month is not None:
        year, month_number = (int(month[:4]), int(month[5:7]))
    start = datetime(year, month_number, 1, tzinfo=timezone.utc)
    end = (
        datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        if month_number == 12
        else datetime(year, month_number + 1, 1, tzinfo=timezone.utc)
    )
    return start, end


# ── RBAC 7.1: transferring a customer's Super Admin seat ─────────────────────
#
# WHY THIS IS ON THE PROVIDER CONSOLE
# -----------------------------------
# The customer's Super Admin seat is minted here at onboarding, so moving it
# belongs here too. It is also the only place it CAN live for the case that
# matters: a client whose Super Admin has left the company has, by definition,
# nobody inside the tenant with the authority to appoint a replacement.
#
# 7.1 requires "a controlled mechanism for changing/transferring the Super
# Admin role when necessary", and migration 0061's partial unique index is what
# makes the absence of one unrecoverable rather than merely awkward. The two
# ship together.
#
# Controlled means three things here: exactly one seat moves, the outgoing
# holder is demoted rather than deleted or disabled, and the whole thing is one
# audited transaction.

@router.post("/tenants/{tenant_id}/super-admin")
async def transfer_super_admin(
    tenant_id: uuid.UUID,
    body: SuperAdminTransferIn,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_superadmin_db),
) -> dict:
    """Move the Client Super Admin seat to another member of the same tenant.

    Refuses with a readable message rather than letting the unique index raise
    an IntegrityError: "duplicate key value violates unique constraint
    uq_users_one_active_super_admin_per_tenant" is not something an operator
    can act on.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    try:
        result = await rbac.transfer_super_admin(
            session, tenant_id=tenant_id, to_user_id=body.user_id
        )
    except rbac.SuperAdminTransferError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await record_action(
        session,
        action="super_admin_transferred",
        actor_user_id=user.user_id,
        actor_role=user.role.value,
        tenant_id=tenant_id,
        resource_type="tenant",
        resource_id=tenant_id,
        previous_state=result["previous_state"],
        new_state=result["new_state"],
        # 7.5: an administrative override on somebody else's organization is
        # exactly the kind of act that has to read as exceptional in the trail.
        exceptional=True,
    )
    return {
        "tenant_id": str(tenant_id),
        "super_admin_user_id": result["new_state"]["super_admin_user_id"],
        "previous_super_admin_user_id": result["previous_state"]["super_admin_user_id"],
        "demoted_to": result["new_state"]["demoted_to"],
    }
