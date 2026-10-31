"""Business Development Portal, the fourth portal (`/bd` UI, `/bd` API).

    Provider Portal   the Vivekium owner's console over its customers
    Customer Portal   a client company's own dashboard
    Candidate Portal  the public candidate surface
    BD Portal         this module: the team that FINDS the customers

Four pages: BD Reach, AI Reach, Customers, Settings. BD Reach was two screens
(Personal Reach and Social Reach) until 2026-08-09; they were always the same
funnel over one `bd_leads` table discriminated by `channel` (see models/bd.py
for why one table, not two), so the merge is a UI consolidation and the column
is unchanged. Omitting `channel` on the list has always meant "both", and that
is now what the screen sends.

SESSION AND PERMISSIONS
-----------------------
A BD rep is Vivekium staff, not a customer's staff: they have no `tenant_id`,
and the leads they work belong to no tenant. So this router uses the OWNER
token audience (the same portal family as the Provider console) and the RLS
BYPASS scope, with an `audit_log` row written for every request exactly as
`get_superadmin_db` does. `bd_leads` still has an RLS policy that requires
`app.bypass_rls = 'on'` (migration 0023), so an org or candidate session cannot
read Vivekium's own sales pipeline. RLS is the boundary; the handler filters
are defence in depth (CLAUDE.md rule 1).

Gating is `require_bd_capability(...)`, resolved through the same data-driven
RBAC engine every other portal uses. There is no `if role == "bd"` anywhere in
this file, and the `bd` role holds nothing this file grants it in code: the
grants are rows in `role_permissions` (CLAUDE.md rule 3).

NO EM DASHES in any user-facing string, including the error details below.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user
from app.core.db import get_session_factory, superadmin_scope
from app.core.security import AUDIENCE_OWNER
from app.models.bd import CHANNEL_SOCIAL, SOCIAL_SOURCES, BDLead
from app.models.user import User
from app.schemas.bd import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    AIReachIn,
    AIReachOut,
    BDCustomerListOut,
    BDProfileOut,
    BDProfileUpdateIn,
    JobCardOut,
    LeadAgreementIn,
    LeadCreateIn,
    LeadListOut,
    LeadOut,
    LeadProgressIn,
    LeadUpdateIn,
    SegmentOut,
    progress_steps,
)
from app.services import bd_leads, rbac, web_research
from app.services.audit import audit

router = APIRouter()

# ── Capability slugs ─────────────────────────────────────────────────────────
# The canonical home for these is services/capabilities.py; the exact additions
# are specified in docs/spec/handoff-bd-backend.md. They are repeated here as
# constants (never as inline string literals at the call sites) so this router
# reads the same way api/provider.py does, and so a rename is one edit.
MANAGE_BD_LEADS = "manage_bd_leads"
VIEW_BD_CUSTOMERS = "view_bd_customers"
USE_AI_REACH = "use_ai_reach"

#: Ceiling on one CSV export. The BD pipeline is a sales list, not a data
#: warehouse; a cap keeps one click from pinning a worker.
MAX_EXPORT_ROWS = 10_000


# ── Session ──────────────────────────────────────────────────────────────────

async def get_bd_db(
    request: Request, user: CurrentUser = Depends(get_current_user)
) -> AsyncIterator[AsyncSession]:
    """BD Portal session: owner audience + RLS bypass scope + an audit row.

    The audience check is auth plumbing (which portal this token was minted
    for), not an RBAC shortcut: what a BD rep may DO is decided entirely by
    `require_bd_capability` below, against permission data.
    """
    if user.audience != AUDIENCE_OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Business Development portal session required",
        )
    async with get_session_factory()() as session:
        async with session.begin():
            async with superadmin_scope(session):
                await audit(
                    session,
                    tenant_id=None,
                    actor_user_id=user.user_id,
                    action="bd_portal_access",
                    target_type="endpoint",
                    target_id=None,
                    metadata={
                        "method": request.method,
                        "path": request.url.path,
                        "query": str(request.url.query or ""),
                    },
                )
                yield session


def require_bd_capability(capability: str):
    """Dependency factory, the BD twin of `deps.require_capability`.

    It exists separately only because `require_capability` is wired to
    `get_tenant_db` (org audience, tenant-scoped), and a BD rep has no tenant.
    The RESOLUTION is identical: the same `rbac.has_capability`, the same user
    overlay then global template then deny chain, resolved on every request so
    a revoked grant takes effect immediately.
    """

    async def dependency(
        user: CurrentUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_bd_db),
    ) -> CurrentUser:
        if not await rbac.has_capability(
            session, None, user.role, capability, user.user_id
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing capability: {capability}",
            )
        return user

    return dependency


# ── Serialization ────────────────────────────────────────────────────────────

async def _owner_names(
    session: AsyncSession, leads: list[BDLead]
) -> dict[uuid.UUID, str | None]:
    ids = {lead.owner_user_id for lead in leads if lead.owner_user_id}
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(User.id, User.full_name, User.email).where(User.id.in_(ids))
        )
    ).all()
    return {row.id: (row.full_name or row.email) for row in rows}


def _lead_out(lead: BDLead, owner_name: str | None = None) -> LeadOut:
    return LeadOut(
        id=lead.id,
        channel=lead.channel,  # type: ignore[arg-type]
        company_name=lead.company_name,
        website=lead.website,
        industry=lead.industry,
        location=lead.location,
        contact_name=lead.contact_name,
        contact_email=lead.contact_email,
        contact_phone=lead.contact_phone,
        social_source=lead.social_source,  # type: ignore[arg-type]
        progress=progress_steps(lead),
        agreement=lead.agreement,
        agreement_at=lead.agreement_at,
        tenant_id=lead.tenant_id,
        owner_user_id=lead.owner_user_id,
        owner_name=owner_name,
        notes=lead.notes,
        archived_at=lead.archived_at,
        created_at=lead.created_at,
        updated_at=lead.updated_at,
    )


async def _own_scope(session: AsyncSession, user: CurrentUser) -> uuid.UUID | None:
    """A public-demo BD user reaches only the leads that user owns.

    A demo account is open to anyone, so it must never read the real sales
    pipeline or another visitor's leads. Every other BD user sees all leads.
    """
    is_demo = await session.scalar(
        select(User.public_demo).where(User.id == user.user_id)
    )
    return user.user_id if is_demo else None


async def _load_lead(
    session: AsyncSession, lead_id: uuid.UUID, owner_scope: uuid.UUID | None = None
) -> BDLead:
    lead = await session.get(BDLead, lead_id)
    if lead is None or (owner_scope is not None and lead.owner_user_id != owner_scope):
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


# ── Leads ────────────────────────────────────────────────────────────────────

@router.get("/leads", response_model=LeadListOut)
async def list_leads(
    request: Request,
    channel: str | None = Query(
        default=None, description="personal | social. Omit for both."
    ),
    social_source: str | None = Query(
        default=None,
        description="Narrow to one platform (linkedin, google, facebook, "
                    "instagram, x). Omit for every source.",
    ),
    search: str | None = Query(
        default=None,
        description="Matches company, industry, location, contact name, email, "
                    "phone or website.",
    ),
    # Typed as a STRING, not a bool, on purpose. The contract below is that
    # `agreement=` (present but empty) means "undecided", but FastAPI's bool
    # validator rejects "" during dependency resolution with a 422, so the
    # handler body that distinguishes the two cases was never reached. The BD
    # console's "Undecided" tab sends exactly that (components/bd/reach-page.tsx)
    # and was therefore 422ing every time. Parsed by hand below.
    agreement: str | None = Query(
        default=None,
        description="true signed, false declined. Send an empty value to list "
                    "the leads nobody has decided yet.",
    ),
    include_archived: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_bd_db),
    user: CurrentUser = Depends(require_bd_capability(MANAGE_BD_LEADS)),
) -> LeadListOut:
    """BD Reach, the one lead funnel.

    Omitting `channel` lists both, which is what the merged screen does; the
    parameter survives because `channel` is still a real column and the source
    filter narrows on it.

    Search, the channel, source and agreement filters and pagination all run IN SQL,
    before the page is cut. Filtering a fetched page in the browser would make
    "3 of 108 leads match" depend on which page happened to be loaded, which is
    the same reason the Provider Portal's customer list works this way.
    """
    if channel is not None and channel not in ("personal", "social"):
        raise HTTPException(
            status_code=422, detail="channel must be personal or social"
        )
    if social_source is not None and social_source not in SOCIAL_SOURCES:
        raise HTTPException(
            status_code=422,
            detail="social_source must be one of " + ", ".join(SOCIAL_SOURCES),
        )
    # `agreement=` (present but empty) means "undecided"; an absent key means
    # "do not filter". Only the raw query string can tell them apart.
    agreement_is_set = "agreement" in request.query_params
    if agreement is None or agreement == "":
        agreement_value: bool | None = None
    elif agreement.lower() in ("true", "1", "yes"):
        agreement_value = True
    elif agreement.lower() in ("false", "0", "no"):
        agreement_value = False
    else:
        raise HTTPException(
            status_code=422,
            detail="agreement must be true, false, or empty for undecided",
        )

    predicates = bd_leads.lead_predicates(
        channel=channel,
        social_source=social_source,
        search=search,
        agreement=agreement_value,
        include_archived=include_archived,
        owner_user_id=await _own_scope(session, user),
        agreement_is_set=agreement_is_set,
    )
    total = (
        await session.execute(
            select(func.count(BDLead.id)).where(*predicates)
        )
    ).scalar_one()
    rows = list(
        (
            await session.execute(
                bd_leads.lead_list_query(predicates, page, page_size)
            )
        ).scalars().all()
    )
    names = await _owner_names(session, rows)
    return LeadListOut(
        leads=[_lead_out(lead, names.get(lead.owner_user_id)) for lead in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/leads", response_model=LeadOut, status_code=201)
async def create_lead(
    body: LeadCreateIn,
    user: CurrentUser = Depends(require_bd_capability(MANAGE_BD_LEADS)),
    session: AsyncSession = Depends(get_bd_db),
) -> LeadOut:
    """Add a lead to either reach channel.

    The lead is owned by whoever created it. Ownership is informational here
    (the list is not filtered by it) so a rep going on leave does not hide
    their pipeline from the team.
    """
    lead = BDLead(
        id=uuid.uuid4(),
        channel=body.channel,
        company_name=body.company_name,
        website=body.website,
        industry=body.industry,
        location=body.location,
        contact_name=body.contact_name,
        contact_email=body.contact_email,
        contact_phone=body.contact_phone,
        social_source=body.social_source,
        notes=body.notes,
        owner_user_id=user.user_id,
    )
    session.add(lead)
    try:
        await session.flush()
    except IntegrityError as exc:
        # The database CHECK is the real guarantee that a personal lead cannot
        # carry a social source. Pydantic already rejected this shape, so
        # reaching here means the constraint caught something pydantic could
        # not; report it rather than returning a 500.
        raise HTTPException(
            status_code=422,
            detail="That lead does not satisfy the channel and source rule.",
        ) from exc
    await audit(
        session, tenant_id=None, actor_user_id=user.user_id,
        action="bd_lead_created", target_type="bd_lead", target_id=str(lead.id),
        metadata={"channel": lead.channel, "company": lead.company_name},
    )
    return _lead_out(lead)


@router.patch("/leads/{lead_id}", response_model=LeadOut)
async def update_lead(
    lead_id: uuid.UUID,
    body: LeadUpdateIn,
    user: CurrentUser = Depends(require_bd_capability(MANAGE_BD_LEADS)),
    session: AsyncSession = Depends(get_bd_db),
) -> LeadOut:
    """Partial edit. An absent key leaves the field alone, an explicit blank
    clears it.

    `channel` is not editable: moving a lead between Personal Reach and Social
    Reach would either strip a real source or invent one.
    """
    lead = await _load_lead(session, lead_id, await _own_scope(session, user))
    changes = body.model_dump(exclude_unset=True)

    if "social_source" in changes and lead.channel != CHANNEL_SOCIAL:
        raise HTTPException(
            status_code=422,
            detail="Only a social lead has a source. This lead came from "
                   "personal reach.",
        )
    if (
        "social_source" in changes
        and lead.channel == CHANNEL_SOCIAL
        and changes["social_source"] is None
    ):
        raise HTTPException(
            status_code=422,
            detail="A social lead needs a source. Choose LinkedIn, Google, "
                   "Facebook, Instagram or X.",
        )
    if "company_name" in changes and not changes["company_name"]:
        raise HTTPException(
            status_code=422, detail="A lead needs a company name."
        )

    changed = []
    for field, value in changes.items():
        if getattr(lead, field) != value:
            setattr(lead, field, value)
            changed.append(field)
    await session.flush()
    if changed:
        await audit(
            session, tenant_id=None, actor_user_id=user.user_id,
            action="bd_lead_updated", target_type="bd_lead",
            target_id=str(lead.id), metadata={"fields": sorted(changed)},
        )
    return _lead_out(lead)


@router.delete("/leads/{lead_id}", response_model=LeadOut)
async def archive_lead(
    lead_id: uuid.UUID,
    user: CurrentUser = Depends(require_bd_capability(MANAGE_BD_LEADS)),
    session: AsyncSession = Depends(get_bd_db),
) -> LeadOut:
    """ARCHIVE, not destroy.

    DELETE is the verb the UI's remove button reaches for, but the row stays:
    a lead carries the history of a relationship (who was contacted, when, what
    was decided) and a mis-click must not be able to erase it. Archived leads
    are hidden from the default list and from the Customers page, and reappear
    with `include_archived=true`.
    """
    lead = await _load_lead(session, lead_id, await _own_scope(session, user))
    if lead.archived_at is None:
        lead.archived_at = datetime.now(timezone.utc)
        await session.flush()
        await audit(
            session, tenant_id=None, actor_user_id=user.user_id,
            action="bd_lead_archived", target_type="bd_lead",
            target_id=str(lead.id), metadata={"company": lead.company_name},
        )
    return _lead_out(lead)


@router.patch("/leads/{lead_id}/progress", response_model=LeadOut)
async def update_progress(
    lead_id: uuid.UUID,
    body: LeadProgressIn,
    user: CurrentUser = Depends(require_bd_capability(MANAGE_BD_LEADS)),
    session: AsyncSession = Depends(get_bd_db),
) -> LeadOut:
    """Tick or untick any of the six checkboxes.

    The body is a SPARSE map, so the UI sends only the box that was clicked and
    two reps working the same lead cannot overwrite each other's ticks.
    """
    lead = await _load_lead(session, lead_id, await _own_scope(session, user))
    changed = bd_leads.apply_progress(lead, body.progress)
    await session.flush()
    if changed:
        await audit(
            session, tenant_id=None, actor_user_id=user.user_id,
            action="bd_lead_progress", target_type="bd_lead",
            target_id=str(lead.id),
            metadata={"steps": sorted(changed),
                      "values": {k: bool(body.progress[k]) for k in changed}},
        )
    return _lead_out(lead)


@router.patch("/leads/{lead_id}/agreement", response_model=LeadOut)
async def update_agreement(
    lead_id: uuid.UUID,
    body: LeadAgreementIn,
    user: CurrentUser = Depends(require_bd_capability(MANAGE_BD_LEADS)),
    session: AsyncSession = Depends(get_bd_db),
) -> LeadOut:
    """The final stage: yes, no, or back to undecided.

    Saying YES promotes the lead to a CUSTOMER, and a customer IS a `tenants`
    row (CLAUDE.md hard rule). The new tenant lands in the `prospect` status so
    it does not appear in the Provider Portal's live customer list before
    anyone has onboarded it.

    Taking the yes away NEVER deletes that tenant: the link is cleared and the
    tenant is archived, which is reversible, and re-signing reuses the same
    company rather than creating a second one.
    """
    scope = await _own_scope(session, user)
    if scope is not None and body.agreement is True:
        # Signing creates a real tenant; an open demo account must not.
        raise HTTPException(
            status_code=403,
            detail="Signing an agreement is not available in the demo.",
        )
    lead = await _load_lead(session, lead_id, scope)
    outcome, tenant = await bd_leads.set_agreement(session, lead, body.agreement)
    if outcome != "unchanged":
        await audit(
            session, tenant_id=(tenant.id if tenant else None),
            actor_user_id=user.user_id,
            action=f"bd_lead_agreement_{outcome}",
            target_type="bd_lead", target_id=str(lead.id),
            metadata={
                "agreement": body.agreement,
                "tenant_id": str(tenant.id) if tenant else None,
                "company": lead.company_name,
            },
        )
    return _lead_out(lead)


# ── Customers ────────────────────────────────────────────────────────────────

async def _customer_rows(
    session: AsyncSession, search: str | None, page: int | None,
    page_size: int | None, owner_scope: uuid.UUID | None = None,
) -> list[BDLead]:
    predicates = bd_leads.customer_predicates(search=search, owner_user_id=owner_scope)
    stmt = bd_leads.customer_list_query(predicates, page, page_size)
    return list((await session.execute(stmt)).scalars().all())


@router.get("/customers", response_model=BDCustomerListOut)
async def list_bd_customers(
    search: str | None = Query(
        default=None,
        description="Matches company, industry, location or contact details.",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    session: AsyncSession = Depends(get_bd_db),
    user: CurrentUser = Depends(require_bd_capability(VIEW_BD_CUSTOMERS)),
) -> BDCustomerListOut:
    """The customers database: every lead whose agreement was signed.

    Search and pagination run in SQL for the same reason the lead list does.
    """
    scope = await _own_scope(session, user)
    predicates = bd_leads.customer_predicates(search=search, owner_user_id=scope)
    total = (
        await session.execute(select(func.count(BDLead.id)).where(*predicates))
    ).scalar_one()
    rows = await _customer_rows(session, search, page, page_size, scope)
    return BDCustomerListOut(
        customers=[bd_leads.customer_out(lead) for lead in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/customers/export.csv")
async def export_bd_customers(
    search: str | None = Query(default=None),
    session: AsyncSession = Depends(get_bd_db),
    user: CurrentUser = Depends(require_bd_capability(VIEW_BD_CUSTOMERS)),
) -> StreamingResponse:
    """The same customer data as a CSV download, honouring the same filter.

    The rows are read BEFORE the response is returned rather than lazily inside
    the stream: FastAPI closes a `yield` dependency's session before the
    response body is sent, so a generator that queried mid-stream would be
    reaching into a closed transaction. The export is capped instead, and the
    formatted lines still stream.

    Quoting is `csv.writer`'s, so a customer called "Acme, Inc." stays one
    field instead of silently becoming two columns.
    """
    rows = await _customer_rows(
        session, search, page=1, page_size=MAX_EXPORT_ROWS,
        owner_scope=await _own_scope(session, user),
    )
    customers = [bd_leads.customer_out(lead) for lead in rows]
    filename = bd_leads.csv_filename()
    return StreamingResponse(
        iter(list(bd_leads.iter_csv(customers))),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Settings ─────────────────────────────────────────────────────────────────

@router.get("/me", response_model=BDProfileOut)
async def get_bd_profile(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_bd_db),
) -> BDProfileOut:
    """The BD team member's own details.

    There is no password here and no password endpoint anywhere in this router.
    Firebase owns credentials and recovery (CLAUDE.md rule 2), and the existing
    `frontend/components/change-password.tsx` already implements the change via
    the Firebase client SDK with no Vivekium call at all, so the BD Settings
    page mounts that component unchanged.
    """
    row = await session.get(User, user.user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return BDProfileOut(
        user_id=row.id,
        name=row.full_name,
        email=row.email,
        phone=row.phone,
        role=row.role.value,
        capabilities=await rbac.resolve_role_capabilities(
            session, None, row.role, row.id
        ),
    )


@router.patch("/me", response_model=BDProfileOut)
async def update_bd_profile(
    body: BDProfileUpdateIn,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_bd_db),
) -> BDProfileOut:
    """Name, email and phone. Nothing else, and no password.

    Changing the email here changes the Vivekium record only. The Firebase
    identity is separate and is not rewritten from this endpoint, so a rep who
    changes their email keeps signing in with the identity Firebase knows until
    that is changed in Firebase too.
    """
    row = await session.get(User, user.user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Account not found")

    changes = body.model_dump(exclude_unset=True)
    if row.public_demo and changes.get("email", row.email) != row.email:
        # The email is a demo account's identity, so it cannot move to another address.
        raise HTTPException(status_code=403, detail="The demo email cannot be changed.")
    mapping = {"name": "full_name", "email": "email", "phone": "phone"}
    changed = []
    for field, value in changes.items():
        column = mapping[field]
        if getattr(row, column) != value:
            setattr(row, column, value)
            changed.append(column)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="Another account already uses that email address.",
        ) from exc
    if changed:
        await audit(
            session, tenant_id=None, actor_user_id=user.user_id,
            action="bd_profile_updated", target_type="user",
            target_id=str(row.id), metadata={"fields": sorted(changed)},
        )
    return await get_bd_profile(user, session)


# ── AI Reach ─────────────────────────────────────────────────────────────────

@router.post("/ai-reach/search", response_model=AIReachOut)
async def ai_reach_search(
    body: AIReachIn,
    session: AsyncSession = Depends(get_bd_db),
    _user: CurrentUser = Depends(require_bd_capability(USE_AI_REACH)),
) -> AIReachOut:
    """Two clearly separated segments, always both returned.

    SIMILAR TO CUSTOMERS is computed against Vivekium's OWN customer database
    and makes no external call, so it works on a deployment with no web search
    key and survives any outage of the second segment. It is computed FIRST for
    exactly that reason.

    FROM THE INTERNET is the agentic segment: plan, Tavily advanced search,
    an LLM truthfulness and relevance pass that DROPS what it cannot support,
    then shaping into cards. It is bounded by a hard 30 second budget, so this
    interactive request returns a clean `status: "timeout"` rather than hanging
    (all genuinely slow work in this platform is dispatched; this one is
    user-initiated, interactive, and time-boxed).

    Confidence is a WORD (High, Medium, Low). No score, percentage or rank ever
    reaches the client.
    """
    similar = await bd_leads.similar_to_customers(
        session,
        job_role=body.job_role,
        city=body.city,
        industry=body.industry,
        company=body.company,
    )
    similar_segment = SegmentOut(
        status="ok",
        message=(
            None if similar
            else "No matching roles were found in Vivekium's customer "
                 "database for this search."
        ),
        jobs=similar,
    )

    internet = await web_research.search_jobs(
        job_role=body.job_role,
        city=body.city,
        industry=body.industry,
        company=body.company,
        session=session,
    )
    internet_segment = SegmentOut(
        status=internet["status"],
        message=internet.get("message"),
        jobs=[JobCardOut(**card) for card in internet.get("jobs", [])],
    )
    return AIReachOut(
        query=body,
        similar_to_customers=similar_segment,
        from_internet=internet_segment,
    )


