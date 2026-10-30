"""Company self-registration: `/api/v1/company-onboarding` (owner spec 2.2, 5, 24).

    POST /register             details + CAPTCHA -> a security code (or a notice)
    POST /demo                 public demo company, simulated charge, org session
    POST /code/verify          the code -> onboarding tenant, invited client, cookie
    POST /code/resend          a new code for a registration already started
    GET  /state                where this browser's registration stands (derived)
    GET  /monthly/plans        the paid Starter pilot
    POST /monthly/subscribe    create its Razorpay subscription
    POST /monthly/verify       verify captured charge before activation
    POST /activate             password (or an existing sign-in) -> org session

WHO IS ASKING, AND HOW THAT IS PROVEN
-------------------------------------
Nobody here has a portal session, and until the code is verified there is no
tenant at all. So the proofs are, in order: a single-use CAPTCHA proof
(`company_register`) before any email is sent; the six-digit security code
from the mailbox before any tenant is created; then the ONBOARDING COOKIE
(`services/company_onboarding`, its own audience, sixty minutes) for every
later step, each of which reads the ONE registration the cookie names. The
activation additionally requires a PAID charge, read from `billing_transactions`
(rule 8), so a password can never be set for a company that has not paid.

WHY THE BYPASS SESSION, AND WHY IT IS SAFE
------------------------------------------
`company_registrations` exists before its tenant, so no tenant session could
write it, and the tenant a verification creates is not reachable by an org
session until activation. Every route therefore runs on `get_onboarding_db`,
the explicit RLS bypass scope, and every handler filters by the exact
registration its proof names (the email it was sent to, or the cookie's id),
never by anything else in the body. Writes commit EXPLICITLY before the
response is built, so a cookie or a session is never handed out for a row
that rolled back afterwards.

ONE MONTHLY SETTLEMENT PATH
---------------------------
The onboarding verification and Razorpay webhook both call
`monthly_plans.settle_charge`; its payment id guard grants one month once.

ENUMERATION
-----------
`/register` and `/code/resend` answer the same sentence and start the same
resend window whether or not the address already has an account; an address
that does receives the platform `account_exists_notice` instead of a code.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.admin import _seed_permissions, next_free_domain
from app.core.config import get_settings
from app.core.db import get_session_factory, superadmin_scope
from app.core.security import AUDIENCE_ORG
from app.models.billing import SUBUNITS_PER_CREDIT, BillingTransaction
from app.models.company_registration import (
    OPEN_STATUSES,
    REGISTRATION_ACTIVATED,
    REGISTRATION_EXPIRED,
    REGISTRATION_PENDING,
    REGISTRATION_VERIFIED,
    CompanyRegistration,
)
from app.models.enums import Role, UserStatus
from app.models.tenant import CUSTOMER_ACTIVE, TENANT_ONBOARDING, Tenant
from app.models.user import User
from app.schemas.admin import derive_tenant_domain
from app.schemas.auth import SessionOut
from app.schemas.company_onboarding import (
    ActivateIn,
    CodeResendIn,
    CodeVerifyIn,
    DemoCompanyIn,
    OnboardingStateOut,
    RegistrationAcceptedOut,
    RegistrationIn,
)
from app.services import (
    captcha,
    company_onboarding as onboarding,
    credits,
    demo_access,
    monthly_plans,
    employer_pages,
    firebase_auth,
    password_policy,
    razorpay,
    rbac,
    security_codes,
    security_email,
)
from app.services.audit import AUTH_LOGIN_SUCCEEDED, audit, record_auth_event
from app.services.rate_limit import rate_limit

log = logging.getLogger(__name__)

router = APIRouter()


class MonthlyChoiceIn(BaseModel):
    plan_slug: str


class MonthlyProofIn(BaseModel):
    razorpay_subscription_id: str
    razorpay_payment_id: str
    razorpay_signature: str

CODE_PURPOSE = "company_register"

ACCEPTED_MESSAGE = (
    "If this email address can be used to register a company, a security code "
    "is on its way to it. It expires in ten minutes."
)
CODE_REFUSED_DETAIL = (
    "That security code is not right or has expired. Check the latest email "
    "or ask for a new code."
)
ACCOUNT_EXISTS_DETAIL = (
    "This email address already has a Vivekium account, so it cannot register "
    "a new company. Sign in instead."
)
COMPANY_NAME_TAKEN_DETAIL = (
    "A company with this name is already registered on Vivekium. If it is "
    "yours, sign in instead, or contact us."
)
NO_REGISTRATION_DETAIL = (
    "Your registration session has ended. Enter your email address on the "
    "registration page to get a new security code and continue."
)
NOT_READY_DETAIL = (
    "Verify your email address with the security code before you continue."
)
ALREADY_ACTIVE_DETAIL = (
    "This company registration is already finished. Sign in with your email "
    "and password."
)
ALREADY_PAID_DETAIL = (
    "Your first credit purchase is already complete. Set your password to "
    "finish."
)
PAYMENT_REQUIRED_DETAIL = (
    "Complete your first credit purchase before you set your password."
)
PAYMENT_UNVERIFIED_DETAIL = "Payment could not be verified"
IDENTITY_EXISTS_DETAIL = (
    "This email address already has a Vivekium sign-in. Enter that password "
    "to finish, or choose Forgot password on the company sign-in page."
)
IDENTITY_MISMATCH_DETAIL = (
    "That sign-in belongs to a different email address. Sign in with the "
    "address you registered."
)
IDENTITY_UNAVAILABLE_DETAIL = (
    "We could not set up your sign-in right now. Please try again in a moment."
)


async def get_onboarding_db() -> AsyncIterator[AsyncSession]:
    """The explicit RLS BYPASS session, for this router only.

    A registration has no tenant until its code is verified, and its tenant
    is unreachable by any org session until activation, so no tenant-scoped
    session can serve these routes. Every handler filters by the exact
    registration its proof names. No `session.begin()` wrapper: each handler
    commits explicitly BEFORE building its response (see the module header).
    """
    async with get_session_factory()() as session:
        async with superadmin_scope(session):
            yield session


# ── Helpers ─────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _open_registration(
    session: AsyncSession, email: str, *, lock: bool = False
) -> CompanyRegistration | None:
    """The OPEN registration for `email`, closing a stale pending one first."""
    stmt = select(CompanyRegistration).where(
        CompanyRegistration.email == email,
        CompanyRegistration.status.in_(OPEN_STATUSES),
    )
    if lock:
        stmt = stmt.with_for_update()
    registration = (await session.execute(stmt)).scalars().first()
    if (
        registration is not None
        and registration.status == REGISTRATION_PENDING
        and registration.created_at is not None
        and registration.created_at
        < _now() - timedelta(days=onboarding.REGISTRATION_PENDING_DAYS)
    ):
        registration.status = REGISTRATION_EXPIRED
        await session.flush()
        return None
    return registration


async def _other_accounts(
    session: AsyncSession, email: str, registration: CompanyRegistration | None
) -> list[User]:
    """Every `users` row on this address EXCEPT the registration's own
    invited Super Admin (which exists once the code was verified)."""
    rows = (
        await session.execute(select(User).where(func.lower(User.email) == email))
    ).scalars().all()
    own = registration.user_id if registration is not None else None
    return [row for row in rows if row.id != own]


async def _name_taken(
    session: AsyncSession, name: str, registration: CompanyRegistration | None
) -> bool:
    stmt = select(Tenant.id).where(func.lower(Tenant.name) == name.lower())
    if registration is not None and registration.tenant_id is not None:
        stmt = stmt.where(Tenant.id != registration.tenant_id)
    return (await session.execute(stmt.limit(1))).first() is not None


def require_onboarding_cookie(request: Request) -> uuid.UUID:
    """THE GATE of every step after the security code: the registration id a
    valid onboarding cookie names, or 401. The cookie is minted only by a
    right code (`/code/verify`), is signed with its own audience, and lives
    sixty minutes; a portal session's token does not pass it."""
    registration_id = onboarding.read_cookie(request.cookies.get(onboarding.ONBOARDING_COOKIE))
    if registration_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=NO_REGISTRATION_DETAIL)
    return registration_id


async def _registration(
    session: AsyncSession, registration_id: uuid.UUID | None, *, lock: bool = False
) -> CompanyRegistration | None:
    if registration_id is None:
        return None
    stmt = select(CompanyRegistration).where(CompanyRegistration.id == registration_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalars().first()


async def _verified_registration(
    session: AsyncSession, registration_id: uuid.UUID, *, lock: bool = False
) -> tuple[CompanyRegistration, Tenant]:
    """The cookie's registration, which must be email-verified and still
    onboarding. Every step after the code goes through here."""
    registration = await _registration(session, registration_id, lock=lock)
    if registration is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=NO_REGISTRATION_DETAIL)
    if registration.status == REGISTRATION_ACTIVATED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=ALREADY_ACTIVE_DETAIL)
    if registration.status != REGISTRATION_VERIFIED or registration.tenant_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NOT_READY_DETAIL)
    tenant = await session.get(Tenant, registration.tenant_id)
    if tenant is None or tenant.status != TENANT_ONBOARDING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NO_REGISTRATION_DETAIL)
    return registration, tenant


async def _state(session: AsyncSession, registration: CompanyRegistration | None) -> OnboardingStateOut:
    paid = await onboarding.has_paid(session, registration.tenant_id if registration else None)
    stage = onboarding.stage(registration, paid)
    if registration is None or stage == onboarding.STAGE_DETAILS:
        return OnboardingStateOut(stage=stage)
    return OnboardingStateOut(
        stage=stage,
        email=registration.email,
        first_name=registration.first_name,
        company_name=registration.company_name,
    )


def _razorpay_or_503(exc: Exception) -> HTTPException:
    if isinstance(exc, razorpay.RazorpayNotConfigured):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payments are not configured on this server yet.",
        )
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


async def _send_code(session: AsyncSession, registration: CompanyRegistration, code: str) -> None:
    security_email.dispatch_security_email(
        registration.email, "security_code_registration", {"code": code}, session=session
    )
    await audit(
        session, tenant_id=registration.tenant_id, actor_user_id=registration.user_id,
        action=onboarding.AUDIT_CODE_SENT, target_type="company_registration",
        target_id=registration.id,
    )


# ── Routes ──────────────────────────────────────────────────────────────────

@router.post(
    "/register",
    response_model=RegistrationAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("company_register", limit=10, window=600))],
)
async def register(
    body: RegistrationIn, session: AsyncSession = Depends(get_onboarding_db)
) -> RegistrationAcceptedOut:
    """Accept the form and send a security code, CAPTCHA first (spec 5.2).

    Only the FIRST Company Super Admin registers publicly: the account this
    creates is always `client`, and nothing in the body can name a role.
    """
    await captcha.consume_proof(body.captcha_proof, CODE_PURPOSE)
    email = body.email
    registration = await _open_registration(session, email, lock=True)
    if registration is None or registration.status == REGISTRATION_PENDING:
        if await _name_taken(session, body.company_name, registration):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=COMPANY_NAME_TAKEN_DETAIL)

    # Issued for EVERY address, so the resend window cannot tell an existing
    # account from a new one. The code is only ever sent to a new one.
    code = await security_codes.issue_code(CODE_PURPOSE, email)

    if await _other_accounts(session, email, registration):
        security_email.dispatch_security_email(
            email, "account_exists_notice", {"surface": "company"}, session=session
        )
        await audit(
            session, tenant_id=None, actor_user_id=None,
            action=onboarding.AUDIT_STARTED, target_type="company_registration",
            metadata={"outcome": "existing_account"},
        )
        await session.commit()
        return RegistrationAcceptedOut(message=ACCEPTED_MESSAGE)

    if registration is None:
        registration = CompanyRegistration(
            id=uuid.uuid4(),
            email=email,
            first_name=body.first_name,
            last_name=body.last_name,
            phone=body.phone,
            company_name=body.company_name,
            industry=body.industry,
            industry_other=body.industry_other,
            status=REGISTRATION_PENDING,
        )
        session.add(registration)
        await session.flush()
        await audit(
            session, tenant_id=None, actor_user_id=None,
            action=onboarding.AUDIT_STARTED, target_type="company_registration",
            target_id=registration.id, metadata={"outcome": "new"},
        )
    elif registration.status == REGISTRATION_PENDING:
        # A second submit before the code: the latest details win.
        registration.first_name = body.first_name
        registration.last_name = body.last_name
        registration.phone = body.phone
        registration.company_name = body.company_name
        registration.industry = body.industry
        registration.industry_other = body.industry_other
    # A VERIFIED registration keeps the details its tenant was created from;
    # a new code only lets the same person back in to continue.
    await _send_code(session, registration, code)
    try:
        await session.commit()
    except IntegrityError:
        # A concurrent submit for the same address opened the registration
        # first; its code is the one in the mailbox.
        await session.rollback()
    return RegistrationAcceptedOut(message=ACCEPTED_MESSAGE)


_DEMO_EMAIL_REFUSED = (
    "This email address cannot be used for the demo. Use a different email."
)


async def _issue_org_session(response: Response, session: AsyncSession, user: User) -> SessionOut:
    from app.api import auth as auth_api

    await auth_api._issue_session(response, user, AUDIENCE_ORG)  # noqa: SLF001
    return SessionOut(
        user=await auth_api._user_out(session, user),  # noqa: SLF001
        capabilities=await auth_api._capabilities(session, user),  # noqa: SLF001
    )


@router.post(
    "/demo",
    response_model=SessionOut,
    dependencies=[Depends(rate_limit("company_register", limit=10, window=600))],
)
async def register_demo_company(
    body: DemoCompanyIn,
    response: Response,
    session: AsyncSession = Depends(get_onboarding_db),
) -> SessionOut:
    """Create an active public demo company and open its session.

    No CAPTCHA and no Razorpay call. One zero-amount subscription charge
    marks the company paid, and one credit grant funds the ledger. The same
    public demo email returns the existing company session.
    """
    demo_access.require_enabled()
    from app.api import auth as auth_api

    email = body.email
    settings = get_settings()
    if auth_api._is_owner_email(email, settings.owner_email):  # noqa: SLF001
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_DEMO_EMAIL_REFUSED)
    rows = list(
        (
            await session.execute(select(User).where(func.lower(User.email) == email))
        ).scalars().all()
    )
    if any(not row.public_demo for row in rows):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_DEMO_EMAIL_REFUSED)
    org_roles = auth_api.PORTAL_ROLES["org"]
    existing = [
        row for row in rows
        if row.public_demo and row.role in org_roles and row.status != UserStatus.disabled
    ]
    if existing:
        chosen = next((row for row in existing if row.role == Role.client), existing[0])
        return await _issue_org_session(response, session, chosen)
    if await _name_taken(session, body.company_name, None):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=COMPANY_NAME_TAKEN_DETAIL)

    now = _now()
    taken = set((await session.execute(select(Tenant.domain))).scalars().all())
    tenant = Tenant(
        id=uuid.uuid4(),
        name=body.company_name,
        domain=next_free_domain(derive_tenant_domain(body.company_name, email), taken),
        industry=body.industry,
        status=CUSTOMER_ACTIVE,
        is_demo=False,
        public_demo=True,
        subscription_status="active",
        current_plan_slug="starter",
        subscription_started_at=now,
        notes="Public demo company. Created by demo mode. No payment was collected.",
    )
    session.add(tenant)
    await session.flush()
    tenant.razorpay_subscription_id = f"demo-sub-{tenant.id}"
    await employer_pages.assign_slug(session, tenant)
    user = User(
        tenant_id=tenant.id,
        role=Role.client,
        email=email,
        phone=body.phone,
        full_name=f"{body.first_name} {body.last_name}",
        status=UserStatus.active,
        firebase_uid=None,
        auth_providers=["demo"],
        public_demo=True,
    )
    session.add(user)
    _seed_permissions(session, tenant.id)
    await session.flush()
    registration = CompanyRegistration(
        id=uuid.uuid4(),
        email=email,
        first_name=body.first_name,
        last_name=body.last_name,
        phone=body.phone,
        company_name=body.company_name,
        industry=body.industry,
        industry_other=body.industry_other,
        status=REGISTRATION_ACTIVATED,
        email_verified_at=now,
        tenant_id=tenant.id,
        user_id=user.id,
        activated_at=now,
    )
    session.add(registration)
    session.add(
        BillingTransaction(
            tenant_id=tenant.id,
            status="success",
            transaction_type="subscription_charge",
            amount_inr=0,
            subtotal_inr=0,
            gst_inr=0,
            razorpay_payment_id=f"demo-reg-{registration.id}",
            razorpay_subscription_id=tenant.razorpay_subscription_id,
            plan_slug="starter",
            assessments_granted=settings.demo_initial_credits,
            notes="Public demo registration. No payment was collected.",
        )
    )
    await credits.grant(
        session,
        tenant_id=tenant.id,
        subunits=settings.demo_initial_credits * SUBUNITS_PER_CREDIT,
        idempotency_key=f"demo-allocation:{tenant.id}",
        metadata={"source": "public_demo_registration"},
    )
    await audit(
        session, tenant_id=tenant.id, actor_user_id=user.id,
        action=onboarding.AUDIT_ACTIVATED, target_type="company_registration",
        target_id=registration.id,
        metadata={"payment": "simulated", "credits": settings.demo_initial_credits},
    )
    await record_auth_event(
        session, action=AUTH_LOGIN_SUCCEEDED, actor_user_id=user.id,
        tenant_id=tenant.id, metadata={"provider": "demo", "via": "demo_company"},
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=COMPANY_NAME_TAKEN_DETAIL,
        ) from exc
    await rbac.invalidate_role_permissions(tenant.id)
    return await _issue_org_session(response, session, user)


@router.post(
    "/code/resend",
    response_model=RegistrationAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("company_register_resend", limit=10, window=600))],
)
async def resend_code(
    body: CodeResendIn, session: AsyncSession = Depends(get_onboarding_db)
) -> RegistrationAcceptedOut:
    """A new code for a registration already started (and the way back in for
    one whose cookie expired). The newest code replaces the last one; the
    resend window runs for every address alike."""
    code = await security_codes.issue_code(CODE_PURPOSE, body.email)
    registration = await _open_registration(session, body.email)
    if registration is not None and not await _other_accounts(session, body.email, registration):
        await _send_code(session, registration, code)
    await session.commit()
    return RegistrationAcceptedOut(message=ACCEPTED_MESSAGE)


@router.post(
    "/code/verify",
    response_model=OnboardingStateOut,
    dependencies=[Depends(rate_limit("company_register_verify", limit=20, window=600))],
)
async def verify_code(
    body: CodeVerifyIn,
    response: Response,
    session: AsyncSession = Depends(get_onboarding_db),
) -> OnboardingStateOut:
    """A right code creates the pending company (spec 5.4) and the cookie.

    The tenant is `onboarding` and its `client` user is `invited` with no
    sign-in: neither is usable until activation. A second verification of the
    same registration (resuming later) creates nothing and only renews the
    cookie.
    """
    email = body.email
    registration = await _open_registration(session, email, lock=True)
    if registration is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=CODE_REFUSED_DETAIL)
    if not await security_codes.check_code(CODE_PURPOSE, email, body.code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=CODE_REFUSED_DETAIL)
    if await _other_accounts(session, email, registration):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=ACCOUNT_EXISTS_DETAIL)

    if registration.status == REGISTRATION_PENDING:
        if await _name_taken(session, registration.company_name, registration):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=COMPANY_NAME_TAKEN_DETAIL)
        taken = set((await session.execute(select(Tenant.domain))).scalars().all())
        tenant = Tenant(
            id=uuid.uuid4(),
            name=registration.company_name,
            domain=next_free_domain(
                derive_tenant_domain(registration.company_name, email), taken
            ),
            industry=registration.industry,
            status=TENANT_ONBOARDING,
            notes=(
                "Registered itself on the public site."
                + (
                    f" Industry given as: {registration.industry_other}."
                    if registration.industry_other
                    else ""
                )
            ),
        )
        session.add(tenant)
        await session.flush()
        await employer_pages.assign_slug(session, tenant)
        user = User(
            tenant_id=tenant.id,
            role=Role.client,
            email=email,
            phone=registration.phone,
            full_name=f"{registration.first_name} {registration.last_name}",
            status=UserStatus.invited,
        )
        session.add(user)
        _seed_permissions(session, tenant.id)
        await session.flush()
        registration.status = REGISTRATION_VERIFIED
        registration.email_verified_at = _now()
        registration.tenant_id = tenant.id
        registration.user_id = user.id
        await audit(
            session, tenant_id=tenant.id, actor_user_id=user.id,
            action=onboarding.AUDIT_EMAIL_VERIFIED, target_type="company_registration",
            target_id=registration.id,
        )
    out = await _state(session, registration)
    registration_id = registration.id
    tenant_id = registration.tenant_id
    await session.commit()
    if tenant_id is not None:
        await rbac.invalidate_role_permissions(tenant_id)
    onboarding.set_cookie(response, registration_id)
    return out


@router.get(
    "/state",
    response_model=OnboardingStateOut,
    dependencies=[Depends(rate_limit("company_register_state", limit=60, window=60))],
)
async def registration_state(
    request: Request, session: AsyncSession = Depends(get_onboarding_db)
) -> OnboardingStateOut:
    """Where this browser's registration stands, so a reload resumes at the
    right step. No cookie (or an expired one) is the first step, not an
    error."""
    registration_id = onboarding.read_cookie(request.cookies.get(onboarding.ONBOARDING_COOKIE))
    return await _state(session, await _registration(session, registration_id))


@router.get("/monthly/plans")
async def onboarding_monthly_plans(
    registration_id: uuid.UUID = Depends(require_onboarding_cookie),
    session: AsyncSession = Depends(get_onboarding_db),
) -> dict:
    await _verified_registration(session, registration_id)
    return {"plans": [
        {**plan.__dict__, "gst_inr": plan.gst_inr, "total_inr": plan.total_inr,
         "rollover_months": plan.rollover_months}
        for plan in monthly_plans.PLANS if plan.slug == "starter"
    ]}


@router.post("/monthly/subscribe")
async def onboarding_monthly_subscribe(
    body: MonthlyChoiceIn,
    registration_id: uuid.UUID = Depends(require_onboarding_cookie),
    session: AsyncSession = Depends(get_onboarding_db),
) -> dict:
    registration, tenant = await _verified_registration(session, registration_id, lock=True)
    if tenant.public_demo:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Demo workspaces do not use a payment gateway.",
        )
    if body.plan_slug != "starter":
        raise HTTPException(status_code=422, detail="The 30-day pilot starts on Starter")
    if await onboarding.has_paid(session, tenant.id):
        raise HTTPException(status_code=409, detail=ALREADY_PAID_DETAIL)
    try:
        checkout = await monthly_plans.begin_subscription(session, tenant, body.plan_slug)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (razorpay.RazorpayError, razorpay.RazorpayNotConfigured) as exc:
        raise _razorpay_or_503(exc) from exc
    await audit(
        session, tenant_id=tenant.id, actor_user_id=registration.user_id,
        action=onboarding.AUDIT_PAYMENT_CREATED, target_type="tenant",
        target_id=tenant.id, metadata={"plan_slug": body.plan_slug},
    )
    await session.commit()
    return checkout


@router.post("/monthly/verify", response_model=OnboardingStateOut)
async def onboarding_monthly_verify(
    body: MonthlyProofIn,
    registration_id: uuid.UUID = Depends(require_onboarding_cookie),
    session: AsyncSession = Depends(get_onboarding_db),
) -> OnboardingStateOut:
    registration, tenant = await _verified_registration(session, registration_id)
    if tenant.public_demo:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Demo workspaces do not use a payment gateway.",
        )
    if not razorpay.verify_subscription_signature(
        subscription_id=body.razorpay_subscription_id,
        payment_id=body.razorpay_payment_id,
        signature=body.razorpay_signature,
    ):
        raise HTTPException(status_code=400, detail=PAYMENT_UNVERIFIED_DETAIL)
    if tenant.razorpay_subscription_id != body.razorpay_subscription_id:
        raise HTTPException(status_code=403, detail="Payment belongs to another company")
    payment = await razorpay.fetch_payment(body.razorpay_payment_id)
    if payment.get("status") != "captured" or payment.get("currency") != "INR":
        raise HTTPException(status_code=409, detail="Payment has not been captured")
    plan = monthly_plans.BY_SLUG[tenant.current_plan_slug]
    amount_paise = int(payment["amount"])
    settled = False
    if amount_paise == plan.total_inr * razorpay.PAISE_PER_RUPEE:
        settled = await monthly_plans.settle_charge(
            session, tenant=tenant, subscription_id=body.razorpay_subscription_id,
            payment_id=body.razorpay_payment_id, amount_inr=plan.total_inr,
        )
    else:
        # Checkout may return a nominal mandate authorization before the
        # first subscription charge. It is never a paid Starter month.
        if tenant.subscription_status != "active":
            tenant.subscription_status = "pending"
    await audit(
        session, tenant_id=tenant.id, actor_user_id=registration.user_id,
        action=onboarding.AUDIT_PAYMENT_VERIFIED, target_type="tenant",
        target_id=tenant.id, metadata={"granted": settled},
    )
    out = await _state(session, registration)
    await session.commit()
    return out


@router.post(
    "/activate",
    response_model=SessionOut,
    dependencies=[Depends(rate_limit("company_register_activate", limit=10, window=600))],
)
async def activate(
    body: ActivateIn,
    response: Response,
    registration_id: uuid.UUID = Depends(require_onboarding_cookie),
    session: AsyncSession = Depends(get_onboarding_db),
) -> SessionOut:
    """Set the password and open the workspace (spec 5.7).

    The proofs are the onboarding cookie and a PAID purchase for the tenant,
    both checked under a lock on the registration so two tabs cannot activate
    twice. A new password creates a Firebase password identity with a
    verified email (the code proved the mailbox); an address that already has
    an identity sends that identity's ID token instead, obtained by signing in
    with its password. Google is never accepted for a company account.
    """
    from app.api import auth as auth_api

    if body.password is not None:
        password_policy.require_valid(body.password)
    registration, tenant = await _verified_registration(session, registration_id, lock=True)
    if not await onboarding.has_paid(session, tenant.id):
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=PAYMENT_REQUIRED_DETAIL)
    user = await session.get(User, registration.user_id) if registration.user_id else None
    if user is None or user.status == UserStatus.disabled or user.tenant_id != tenant.id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NO_REGISTRATION_DETAIL)

    created_uid: str | None = None
    if body.password is not None:
        name = f"{registration.first_name} {registration.last_name}"
        try:
            created_uid = await firebase_auth.create_password_user(
                registration.email, body.password, name
            )
        except firebase_auth.IdentityAlreadyExists as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=IDENTITY_EXISTS_DETAIL) from exc
        except firebase_auth.IdentityOperationFailed as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=IDENTITY_UNAVAILABLE_DETAIL
            ) from exc
        uid = created_uid
    else:
        identity = await run_in_threadpool(firebase_auth.verify_id_token, body.id_token)
        if onboarding.normal_email(identity.email or "") != registration.email:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=IDENTITY_MISMATCH_DETAIL)
        firebase_auth.assert_provider_allowed(identity, Role.client.value)
        uid = identity.uid

    now = _now()
    user.firebase_uid = uid
    user.auth_providers = sorted(set((user.auth_providers or []) + ["password"]))
    user.email_verified_at = user.email_verified_at or now
    user.status = UserStatus.active
    tenant.status = CUSTOMER_ACTIVE
    registration.status = REGISTRATION_ACTIVATED
    registration.activated_at = now
    await audit(
        session, tenant_id=tenant.id, actor_user_id=user.id,
        action=onboarding.AUDIT_ACTIVATED, target_type="company_registration",
        target_id=registration.id,
        metadata={"password": "created" if created_uid else "existing_identity"},
    )
    await record_auth_event(
        session, action=AUTH_LOGIN_SUCCEEDED, actor_user_id=user.id,
        tenant_id=tenant.id, metadata={"provider": "password", "via": "company_activation"},
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if created_uid is not None:
            # The identity exists and no account owns it: take it back out.
            try:
                await run_in_threadpool(firebase_auth.delete_identity, created_uid)
            except firebase_auth.IdentityDeletionFailed:
                log.error("company_activation.orphan_identity_left")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This sign-in could not be linked to an account",
        ) from exc

    onboarding.clear_cookie(response)
    await auth_api._issue_session(response, user, AUDIENCE_ORG)  # noqa: SLF001
    return SessionOut(
        user=await auth_api._user_out(session, user),  # noqa: SLF001
        capabilities=await auth_api._capabilities(session, user),  # noqa: SLF001
    )
