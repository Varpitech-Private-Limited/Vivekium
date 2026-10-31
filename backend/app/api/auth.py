"""Firebase identity exchange, workspace selection and the session lifecycle.

Firebase proves identity; Vivekium remains authoritative for application
roles, tenant isolation, capabilities, and its portal-scoped sessions.
`services/login_context` decides which workspaces a proven identity may enter.

The retired one-time-code login handlers (candidate self-registration, code
request, code verification) sat here without a route for two months after
Firebase took over identity, and were deleted on 2026-09-24 with the schemas
only they used. `tests/test_login_otp_removed.py` keeps them gone.
"""
import re
import uuid
from datetime import datetime, timezone

import jwt as pyjwt
from starlette.concurrency import run_in_threadpool
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, or_, select, text
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.deps import (
    REFRESH_COOKIE,
    CurrentUser,
    clear_auth_cookies,
    get_current_any,
    is_user_activity,
    set_auth_cookies,
)
from app.core.config import get_settings
from app.core.db import get_identity_session, tenant_scope, superadmin_scope
from app.core.security import (
    ALGORITHM,
    AUDIENCE_CANDIDATE,
    AUDIENCE_ORG,
    AUDIENCE_OWNER,
    audience_for_role,
    create_access_token,
    create_refresh_token,
)
from app.models.enums import Role, UserStatus
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.auth import (
    ContextOut,
    DemoSessionIn,
    DemoStatusOut,
    FirebaseSessionIn,
    MeOut,
    PasswordChangeCompleteIn,
    PasswordChangedOut,
    PasswordChangeRequestIn,
    PasswordChangeVerifyIn,
    PasswordChangeVerifyOut,
    PasswordResetCompleteIn,
    PasswordResetRequestIn,
    PasswordResetVerifyIn,
    PasswordResetVerifyOut,
    SecurityCodeSentOut,
    SelectContextIn,
    SessionOut,
    UserOut,
)
from app.services import auth_sessions, candidate_identity, login_context
from app.services import company_onboarding
from app.services import demo_access
from app.services import password_policy, security_codes, security_email
from app.services import firebase_auth
from app.services import rbac
from app.services import staff_invites
from app.services.rate_limit import rate_limit
from app.services.audit import (
    AUTH_CONTEXT_SELECTED,
    AUTH_LOGIN_REFUSED,
    AUTH_LOGIN_SUCCEEDED,
    AUTH_PASSWORD_CHANGE_CODE_SENT,
    AUTH_PASSWORD_CHANGED,
    record_auth_event,
)

router = APIRouter()

#: Which portal a session exchange may land in, by the sign-in surface
#: (`captcha_purpose`). The surface is the portal intent: a filter over the
#: database's own roles, never a grant, so a person
#: with a candidate profile and a company seat gets the candidate workspace on
#: the candidate sign-in page and the company one on /company/login.
EXCHANGE_PURPOSE_PORTAL: dict[str, str] = {
    "candidate_login": "candidate",
    "candidate_register": "candidate",
    "company_login": "org",
    "invite_join": "org",
    "provider_login": "owner",
    "bd_login": "bd",
}

#: How a refusal names the portal a person tried to enter. The words are the
#: surface's, never the role enum's.
_PORTAL_LABEL = {
    "candidate": "candidate",
    "org": "company",
    "owner": "Provider",
    "bd": "business development",
}

PORTAL_ROLES = {
    "candidate": {Role.candidate},
    # Every tenant role. `recruitment_manager` and `interview_manager` were
    # both absent here and in `core.security._ORG_ROLES`, which meant a portal
    # choice silently filtered those accounts out of their own sign-in.
    # Asserted against `Role` in tests/test_rbac_conformance.py so the next
    # role added fails a test rather than a login.
    "org": {
        Role.client,
        Role.recruitment_manager,
        Role.hr_manager,
        Role.recruiter,
        Role.hiring_manager,
        Role.interview_manager,
        Role.ceo,
        Role.md,
        Role.functional_head,
    },
    "bd": {Role.bd},
    "owner": {Role.super_admin},
}


def _filter_portal(users: list[User], portal: str | None) -> list[User]:
    """Apply a login-screen portal choice without ever converting roles.

    Portal intent narrows already-authorised database users. It cannot create a
    BD, owner or customer-team account and therefore cannot be used for
    privilege escalation.
    """
    if not portal:
        return users
    allowed = PORTAL_ROLES[portal]
    return [user for user in users if user.role in allowed]


def _is_owner_email(email: str | None, owner_email: str) -> bool:
    return bool(email) and (email or "").strip().lower() == (owner_email or "").strip().lower()


async def _finalize_single(
    session: AsyncSession,
    response: Response,
    user: User,
    identity: firebase_auth.FirebaseIdentity,
) -> SessionOut:
    """Link a proven Firebase identity to ONE resolved user and issue that
    user's portal-scoped session cookies. Firebase proves the identity; the
    database role/permissions stay authoritative (claude.md rule 2)."""
    if user.status == UserStatus.disabled:
        raise HTTPException(status_code=403, detail="Account unavailable")

    # A BOUND ACCOUNT IS NEVER SILENTLY REBOUND TO A DIFFERENT IDENTITY.
    #
    # This line used to be an unconditional `user.firebase_uid = identity.uid`,
    # and that was account takeover. The caller resolves a user by EMAIL as well
    # as by uid (see the match filters in `firebase_session`), and Firebase
    # email/password signup does not verify the address, so anyone who knew a
    # staff member's email could register it with Firebase, sign in, present the
    # token here, and have this line hand them that person's role and tenant.
    # The victim's uid was overwritten in the same statement, so they lost the
    # account at the moment the attacker gained it.
    #
    # Binding a NULL uid is the legitimate first-login path and still happens.
    # Rebinding a uid that is already set to a different one is not a login, it
    # is a change of who owns the account, and this product already has a
    # deliberate, audited route for that: the Provider's primary-contact rebind,
    # which CLEARS the uid, returns the row to `invited`, revokes the pending
    # invite and sends a fresh one. A silent rebind here bypassed all four of
    # those steps.
    #
    # So the refusal is not a dead end: an account whose Firebase identity
    # genuinely changed is recovered through that route.
    # THE CONDITION IS "UNVERIFIED", NOT "DIFFERENT", AND THE DISTINCTION IS
    # THE WHOLE FIX. A first draft refused every rebind, which is wrong twice:
    # a Google identity always carries a verified email, and control of the
    # address is exactly the proof the email match rests on, so refusing it
    # buys nothing; and the platform OWNER is resolved from a configured email
    # with no administrator above them, so a blanket refusal would lock them
    # out permanently the first time their Firebase uid changed, with no
    # recovery path at all.
    #
    # An UNVERIFIED password identity proves nothing about the address, and
    # that is the case this exists to refuse.
    if (
        user.firebase_uid
        and user.firebase_uid != identity.uid
        and not identity.email_verified
    ):
        await record_auth_event(
            session, action=AUTH_LOGIN_REFUSED, actor_user_id=user.id,
            tenant_id=user.tenant_id,
            metadata={"reason": "firebase_uid_rebind_refused",
                      "provider": identity.provider},
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "This account is already linked to a different sign-in. "
                "Ask your administrator to re-issue your invitation."
            ),
        )

    if user.public_demo:
        await record_auth_event(
            session, action=AUTH_LOGIN_REFUSED, actor_user_id=user.id,
            tenant_id=user.tenant_id,
            metadata={"reason": "public_demo_firebase_refused",
                      "provider": identity.provider},
        )
        raise HTTPException(
            status_code=403,
            detail="This sign-in cannot be linked to a demo workspace.",
        )

    user.firebase_uid = identity.uid
    user.auth_providers = sorted(set((user.auth_providers or []) + [identity.provider]))
    if identity.email_verified:
        user.email_verified_at = user.email_verified_at or datetime.now(timezone.utc)
    # Invited staff/candidates activate on first proven login, mirroring
    # `login_context.select_context` (proving identifier ownership is what
    # flips invited -> active).
    if user.status == UserStatus.invited:
        user.status = UserStatus.active
    # ...and the INVITATION row learns about it too, or the staff table reports
    # "Pending" for ever beside an account that is signed in and working
    # (services/staff_invites carries the full account of why).
    await staff_invites.accept_pending_invite(session, user.id)
    # EVERY candidate sign-in makes sure the person has exactly one candidate
    # record, through the one resolver. Idempotent: a linked record is returned
    # untouched. It also repairs an account whose record was erased, and it is
    # the only place an email match can link a sourced record, and only on an
    # address Firebase has verified (services/candidate_identity).
    if user.role == Role.candidate:
        await candidate_identity.link_on_sign_in(session, user, identity)
    await record_auth_event(
        session, action=AUTH_LOGIN_SUCCEEDED, actor_user_id=user.id,
        tenant_id=user.tenant_id,
        metadata={"provider": identity.provider, "via": "firebase"},
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        # e.g. this firebase_uid is already bound to a different (filtered-out)
        # row — never surface a 500.
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="This sign-in could not be linked to an account"
        ) from exc
    audience = audience_for_role(user.role)
    await _issue_session(response, user, audience)
    return SessionOut(
        user=await _user_out(session, user),
        capabilities=await _capabilities(session, user),
    )


# Abuse control, not authorization (services/rate_limit). This verifies a
# Firebase token and mints a session: anonymous, and the verification is a
# network call to Google. Cheapest endpoint in the product to hit, one of the
# most expensive to serve.
@router.post("/firebase/session", response_model=SessionOut,
    dependencies=[Depends(rate_limit("auth_exchange", limit=20, window=60))],
)
async def firebase_session(
    body: FirebaseSessionIn,
    response: Response,
    session: AsyncSession = Depends(get_identity_session),
) -> SessionOut:
    """Exchange a verified Firebase ID token for a portal-scoped app session.

    Firebase is identity-only; DB roles/permissions remain authoritative
    (claude.md rule 2). Behaviour matrix:

    - **owner email** (settings.owner_email) -> ALWAYS the seeded super_admin,
      owner-portal cookies, never a new candidate;
    - **single match** -> link firebase_uid, issue that user's portal cookies;
    - **staff pre-seed match by email** -> linked in place, role preserved (no
      duplicate candidate row);
    - **multiple matches** -> workspace chooser: contexts + context_token, NO
      cookies, finalized by /auth/select-context;
    - **no match** -> create a candidate user, then resolve its candidate
      record through `candidate_identity.link_on_sign_in` (a verified address
      may claim the oldest unlinked sourced record; an unverified one never
      does), candidate cookies.

    The surface (`captcha_purpose`) narrows the workspaces this identity may
    enter (`EXCHANGE_PURPOSE_PORTAL`). It is not a challenge. A-24 removed
    the server-drawn check; a proof on the body is not read.

    The provider is ROLE AWARE (`firebase_auth.assert_provider_allowed`): a
    candidate may use Google or a password, every company role a password
    only, and the Provider and BD keep their policy. Phone sign-in is removed.
    Every failure is a clean 400/401/403/409/422/503, never a 500.
    """
    portal = EXCHANGE_PURPOSE_PORTAL[body.captcha_purpose]
    settings = get_settings()
    # `firebase_admin`'s client is SYNCHRONOUS and `check_revoked=True`
    # forces a network round trip to the Firebase Admin API on every
    # verification, so calling it directly blocked the event loop for the
    # whole trip, on the busiest path in the product. Every other blocking
    # vendor call in this tree is already offloaded (document_storage,
    # ses_service, email_senders/eligibility); this was the one that was
    # missed. `HTTPException` propagates out of the threadpool unchanged.
    identity = await run_in_threadpool(firebase_auth.verify_id_token, body.id_token)

    # ── Owner invariant (claude.md rule 2 + services/owner.py) ──────────────
    # The platform-owner email resolves ONLY to the seeded super_admin. It is
    # never created as a candidate here, and no NON-owner can reach super_admin
    # via this endpoint (the general lookup below only ever creates candidates,
    # and eligible_login_users / the ORM guard reject any impostor super_admin).
    if _is_owner_email(identity.email, settings.owner_email):
        if portal != "owner":
            raise HTTPException(
                status_code=403,
                detail=f"No {_PORTAL_LABEL[portal]} workspace is linked to this account",
            )
        # Owner is an internal role resolved only by the configured owner email.
        firebase_auth.assert_provider_allowed(identity, Role.super_admin.value)
        owner = (await session.execute(
            select(User).where(
                User.role == Role.super_admin,
                func.lower(User.email) == identity.email.strip().lower(),
            )
        )).scalars().first()
        if owner is None:
            # Never fabricate the owner from a Firebase login — the account is
            # provisioned by the seed, not by self-service.
            raise HTTPException(status_code=403, detail="Owner account is not provisioned")
        return await _finalize_single(session, response, owner, identity)

    # ── Resolve the identity to existing users (unified login, rev 2) ───────
    match_filters = [User.firebase_uid == identity.uid]
    if identity.email:
        match_filters.append(func.lower(User.email) == identity.email.strip().lower())
    matched = (await session.execute(
        select(User).where(or_(*match_filters)).order_by(User.created_at, User.id)
    )).scalars().all()
    # A public demo row shares an email with a real account only because
    # Postgres treats NULL tenant ids as distinct. Never bind Firebase onto
    # it, and never create a second candidate on top of that email.
    if matched and all(user.public_demo for user in matched):
        raise HTTPException(
            status_code=403,
            detail=f"No {_PORTAL_LABEL[portal]} workspace is linked to this account",
        )
    matched = [user for user in matched if not user.public_demo]

    if matched:
        eligible = login_context.eligible_login_users(
            matched, owner_email=settings.owner_email
        )
        eligible = _filter_portal(eligible, portal)
        if not eligible:
            # Every match is disabled or an impostor super_admin, or belongs to
            # another portal -- do NOT mint a duplicate candidate over the top
            # of an existing account. A disabled account in THIS portal is told
            # so; an account that lives on another surface is told which one
            # this is not.
            if _filter_portal(list(matched), portal):
                raise HTTPException(status_code=403, detail="Account unavailable")
            raise HTTPException(
                status_code=403,
                detail=f"No {_PORTAL_LABEL[portal]} workspace is linked to this account",
            )
    else:
        # First-ever sign-in for this identity -> a fresh candidate user.
        if portal != "candidate":
            raise HTTPException(
                status_code=403,
                detail=f"No {_PORTAL_LABEL[portal]} workspace is linked to this account",
            )
        firebase_auth.assert_provider_allowed(identity, Role.candidate.value)
        if not identity.email:
            raise HTTPException(
                status_code=422,
                detail="An email address is required to create a candidate profile",
            )
        user = User(
            role=Role.candidate, email=identity.email,
            full_name=identity.name, tenant_id=None, status=UserStatus.active,
            firebase_uid=identity.uid, auth_providers=[identity.provider],
        )
        session.add(user)
        await session.flush()
        # The candidate RECORD is resolved in `_finalize_single`, through the
        # one resolver, like every other candidate sign-in. NO CONSENT IS
        # WRITTEN on this path: the candidate has seen no consent wording, and
        # Stage A is stamped where it is shown (PUT /portal/me/profile-form).
        eligible = [user]

    # ── A company still ONBOARDING opens no session ─────────────────────────
    # Before the provider gate and before `_finalize_single`, which flips an
    # invited account to active: a Firebase sign-up with a registered address
    # must not open a workspace nobody has paid for. Refuses with the fixed
    # sentence when no other workspace is left (services/company_onboarding).
    eligible = await company_onboarding.without_onboarding(session, eligible)

    # ── Provider gate on every resolved context ─────────────────────────────
    for user in eligible:
        firebase_auth.assert_provider_allowed(identity, user.role.value)

    if len(eligible) == 1:
        return await _finalize_single(session, response, eligible[0], identity)

    # ── Multiple workspaces -> chooser, NO cookies ──────────────────────────
    # firebase_uid is unique, so it can bind to only one user; it is NOT linked
    # here — the chosen workspace is finalized via /auth/select-context, which
    # (after this proven verification) mints cookies for the picked user_id.
    contexts = await login_context.build_contexts(session, eligible)
    token = login_context.make_context_token(
        identity.email, [c.user_id for c in contexts]
    )
    await session.commit()
    return SessionOut(
        contexts=[_context_out(c) for c in contexts],
        context_token=token,
    )


_DEMO_EMAIL_REFUSED = "This email address cannot be used for the demo. Use a different email."
_DEMO_OWNER_REFUSED = "This email address cannot be used for the demo."
_DEMO_COMPANY_MISSING = (
    "No company workspace exists for this email. Register a demo company first."
)


@router.get("/demo-status", response_model=DemoStatusOut)
async def demo_status() -> DemoStatusOut:
    """Whether public demo sign-in is on. Sets no cookie and opens no session."""
    return DemoStatusOut(demo_mode=demo_access.enabled())


async def _users_for_email(session: AsyncSession, email: str) -> list[User]:
    return list(
        (
            await session.execute(
                select(User)
                .where(func.lower(User.email) == email)
                .order_by(User.created_at, User.id)
            )
        ).scalars().all()
    )


async def _open_demo_session(
    session: AsyncSession, response: Response, user: User
) -> SessionOut:
    """Issue one public-demo session. Never writes firebase_uid."""
    if user.public_demo is not True:
        raise HTTPException(status_code=403, detail=_DEMO_OWNER_REFUSED)
    if user.status == UserStatus.disabled:
        raise HTTPException(status_code=403, detail="Account unavailable")
    if user.status == UserStatus.invited:
        user.status = UserStatus.active
    await staff_invites.accept_pending_invite(session, user.id)
    if user.role == Role.candidate:
        await candidate_identity.link_on_sign_in(
            session,
            user,
            firebase_auth.FirebaseIdentity(
                uid="demo",
                email=user.email,
                name=user.full_name,
                provider="demo",
                email_verified=False,
            ),
        )
    await record_auth_event(
        session, action=AUTH_LOGIN_SUCCEEDED, actor_user_id=user.id,
        tenant_id=user.tenant_id,
        metadata={"provider": "demo", "via": "demo_session"},
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="This sign-in could not be linked to an account"
        ) from exc
    await _issue_session(response, user, audience_for_role(user.role))
    return SessionOut(
        user=await _user_out(session, user),
        capabilities=await _capabilities(session, user),
    )


def _prefer_client(users: list[User]) -> User:
    for user in users:
        if user.role == Role.client:
            return user
    return users[0]


@router.post(
    "/demo/session",
    response_model=SessionOut,
    dependencies=[Depends(rate_limit("demo_session", limit=20, window=60))],
)
async def demo_session(
    body: DemoSessionIn,
    response: Response,
    session: AsyncSession = Depends(get_identity_session),
) -> SessionOut:
    """Open a public demo session for any nonempty password.

    A real account, the owner, and a permanent demo tenant are refused. The
    provider portal is never reachable here. Candidate and BD sign-in create
    or retrieve the one public-demo user for that email and portal. Company
    sign-in retrieves a public-demo company and does not create one: a
    company user cannot exist without a tenant, so a new email goes to
    company registration.
    """
    demo_access.require_enabled()
    email = _normal_email(body.email)
    settings = get_settings()
    if _is_owner_email(email, settings.owner_email):
        raise HTTPException(status_code=403, detail=_DEMO_OWNER_REFUSED)
    portal = demo_access.PORTAL_TO_FILTER[body.portal]
    if portal == "owner":
        raise HTTPException(
            status_code=403,
            detail=f"No {_PORTAL_LABEL[portal]} workspace is linked to this account",
        )
    # Tenantless users have no unique key, so two parallel first logins would
    # otherwise create two accounts. One first login at a time per email and portal.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"demo-session:{portal}:{email}"},
    )
    rows = await _users_for_email(session, email)
    if any(not row.public_demo for row in rows):
        raise HTTPException(status_code=409, detail=_DEMO_EMAIL_REFUSED)
    allowed = PORTAL_ROLES[portal]
    in_portal = [row for row in rows if row.public_demo and row.role in allowed]
    eligible = [row for row in in_portal if row.status != UserStatus.disabled]
    if portal == "org":
        if not eligible:
            raise HTTPException(status_code=403, detail=_DEMO_COMPANY_MISSING)
        return await _open_demo_session(session, response, _prefer_client(eligible))
    if eligible:
        return await _open_demo_session(session, response, eligible[0])
    if in_portal:
        raise HTTPException(status_code=403, detail="Account unavailable")
    full_name = " ".join((body.full_name or "").split()) or None
    user = User(
        role=Role.bd if portal == "bd" else Role.candidate,
        email=email,
        full_name=full_name,
        tenant_id=None,
        status=UserStatus.active,
        firebase_uid=None,
        auth_providers=["demo"],
        public_demo=True,
        # A demo BD user may not send AI outreach mail from the platform.
        permissions_json={"use_ai_reach": False} if portal == "bd" else None,
    )
    session.add(user)
    await session.flush()
    return await _open_demo_session(session, response, user)


async def _user_out(session: AsyncSession, user: User) -> UserOut:
    if user.tenant_id is not None:
        async with tenant_scope(session, user.tenant_id):
            workspace_name = (
                await session.execute(
                    select(Tenant.name).where(Tenant.id == user.tenant_id)
                )
            ).scalar_one_or_none() or "Unknown workspace"
    elif user.role == Role.candidate:
        workspace_name = "Candidate workspace"
    else:
        workspace_name = "Vivekium"
    return UserOut(
        id=user.id,
        role=user.role,
        tenant_id=user.tenant_id,
        full_name=user.full_name,
        email=user.email,
        email_verified=user.email_verified_at is not None,
        phone_verified=user.phone_verified_at is not None,
        password_enabled="password" in (user.auth_providers or []),
        workspace_name=workspace_name,
    )


async def _capabilities(session: AsyncSession, user: User) -> list[str]:
    """Capability list for auth responses. Tenant-scoped so the RLS policy on
    role_permissions exposes this tenant's override rows (claude.md rule 1).

    `user_id` is passed so the response is the EFFECTIVE set for this person —
    role defaults with their HR Head overlay applied (spec §7.1) — rather than
    the generic set for their role. The frontend hides actions from this list,
    so a stale answer here would show buttons that then 403.
    """
    if user.tenant_id is None:
        async with superadmin_scope(session):
            return await rbac.capabilities_for_user(
                session, role=user.role, tenant_id=None, user_id=user.id
            )
    async with tenant_scope(session, user.tenant_id):
        return await rbac.capabilities_for_user(
            session, role=user.role, tenant_id=user.tenant_id, user_id=user.id
        )


# Cookie writing is centralized in deps.set_auth_cookies / clear_auth_cookies
# (SameSite=Strict, httponly, secure-in-prod, refresh path-scoped) — a single
# source of truth next to the cookie-name constants.
_set_auth_cookies = set_auth_cookies


async def _issue_session(response: Response, user: User, audience: str) -> None:
    """Create the server record before exposing its signed browser cookies."""
    sid = auth_sessions.new_id()
    access = create_access_token(
        user.id, user.role.value, user.tenant_id, audience=audience,
        session_id=sid,
    )
    refresh_token = create_refresh_token(user.id, audience=audience, session_id=sid)
    jti = pyjwt.decode(
        refresh_token, get_settings().jwt_secret,
        algorithms=[ALGORITHM], audience=audience,
    )["jti"]
    await auth_sessions.create(sid, user.id, jti, refresh_token)
    _set_auth_cookies(response, access, refresh_token)


def _context_out(ctx: login_context.LoginContext) -> ContextOut:
    return ContextOut(
        user_id=ctx.user_id, role=ctx.role, tenant_id=ctx.tenant_id,
        tenant_name=ctx.tenant_name, portal=ctx.portal,
    )


@router.post("/workspaces", response_model=SessionOut)
async def available_workspaces(
    current: CurrentUser = Depends(get_current_any),
    session: AsyncSession = Depends(get_identity_session),
) -> SessionOut:
    """Return the workspaces belonging to the current proven identity.

    This is the in-session counterpart to the login chooser. The access token
    proves the current identity; the returned short-lived, single-use context
    token is still required to finalize a selection. No tenant data is returned
    here, only identity rows and their human-readable workspace labels.
    """
    source = await session.get(User, current.user_id)
    if source is None or source.status == UserStatus.disabled:
        raise HTTPException(status_code=401, detail="Account unavailable")
    # Email only: a phone number is not an identity this product proves.
    identifier = source.email
    if not identifier:
        raise HTTPException(
            status_code=409,
            detail="This account has no email address for workspace switching",
        )

    eligible = login_context.eligible_login_users(
        await login_context.find_users(session, identifier),
        owner_email=get_settings().owner_email,
    )
    # A company still onboarding is never offered (services/company_onboarding).
    eligible = await company_onboarding.without_onboarding(session, eligible)
    contexts = await login_context.build_contexts(session, eligible)
    token = login_context.make_context_token(
        identifier,
        [context.user_id for context in contexts],
        source_user_id=source.id,
    )
    return SessionOut(
        contexts=[_context_out(context) for context in contexts],
        context_token=token,
    )


@router.post("/select-context", response_model=SessionOut)
async def select_context(
    body: SelectContextIn,
    response: Response,
    session: AsyncSession = Depends(get_identity_session),
) -> SessionOut:
    """Exchange a context_token (proof of a verified identity) for cookies as
    one of the identifier's users (contract rev 2). Single-use."""
    # A company still ONBOARDING is refused before the token is spent and
    # before `select_context` flips an invited account to active
    # (services/company_onboarding).
    chosen = await session.get(User, body.user_id)
    if chosen is not None:
        await company_onboarding.without_onboarding(session, [chosen])
    try:
        result = await login_context.select_context(
            session, context_token=body.context_token, user_id=body.user_id
        )
    except login_context.ContextTokenConsumed as exc:
        raise HTTPException(status_code=410, detail="Context token already used") from exc
    except login_context.ContextTokenInvalid as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except login_context.ContextUserMismatch as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except login_context.UserNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except login_context.ContextStoreUnavailable as exc:
        # FAILS CLOSED, the `auth_sessions` posture: a token that cannot be
        # marked used must not be exchanged, or it becomes replayable.
        raise HTTPException(
            status_code=503,
            detail="Sign-in is briefly unavailable. Please try again in a moment.",
        ) from exc

    user = result.user
    token_payload = login_context.decode_context_token(body.context_token)
    selected_workspace = await _user_out(session, user)
    await record_auth_event(
        session, action=AUTH_CONTEXT_SELECTED,
        actor_user_id=user.id, tenant_id=user.tenant_id,
        metadata={
            "role": user.role.value,
            "workspace_name": selected_workspace.workspace_name,
            "selected_tenant_id": str(user.tenant_id) if user.tenant_id else None,
            "source_user_id": token_payload.get("source_user_id"),
        },
    )
    await session.commit()
    await _issue_session(response, user, audience_for_role(user.role))
    return SessionOut(
        user=selected_workspace,
        capabilities=await _capabilities(session, user),
    )


def _dead_session(detail: str) -> JSONResponse:
    """A definite "this refresh token cannot be used again" answer.

    Returned, not raised. Raising an HTTPException discards the injected
    Response and with it the Set-Cookie headers, so the clearing silently never
    happened; a plain JSONResponse carries them.

    Every auth cookie is cleared on the way out, including the `pr_session`
    presence hint. Leaving the hint behind would let the Next.js middleware keep
    admitting the browser to portal routes that can only fail, so the user would
    bounce between a blank page and a broken session instead of landing on the
    login screen once.
    """
    dead = JSONResponse(status_code=401, content={"detail": detail})
    clear_auth_cookies(dead)
    return dead


# Abuse control, not authorization (services/rate_limit). A refresh token is a
# long random JWT and is not brute forceable in practice, so this is not about
# guessing one: it is that an unthrottled endpoint doing a database round trip
# per call is a resource-exhaustion vector, and this was the one auth route the
# sweep that added `auth_exchange` missed.
#
# The window is deliberately generous. A legitimate browser refreshes roughly
# once per access-token lifetime (fifteen minutes), and several tabs of one
# session can refresh at once after a laptop wakes, so a tight limit here would
# sign real people out. Thirty per minute is far above that and far below a
# useful flood. Now that `client_identifier` resolves a verified subject, an
# authenticated caller gets their own bucket rather than sharing their office
# address with every colleague.
@router.post(
    "/refresh",
    dependencies=[Depends(rate_limit("auth_refresh", limit=30, window=60))],
)
async def refresh(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_identity_session),
):
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        return _dead_session("Missing refresh token")
    payload = None
    # A refresh token is minted for exactly one portal audience (owner / org /
    # candidate); try each so any valid session refreshes, but the reissued
    # tokens keep the SAME audience — no cross-portal escalation.
    for audience in (AUDIENCE_OWNER, AUDIENCE_ORG, AUDIENCE_CANDIDATE):
        try:
            payload = pyjwt.decode(
                token, get_settings().jwt_secret, algorithms=[ALGORITHM], audience=audience
            )
            break
        except pyjwt.InvalidAudienceError:
            continue
        except pyjwt.PyJWTError:
            return _dead_session("Invalid refresh token")
    if payload is None or payload.get("type") != "refresh" or not payload.get("sid") or not payload.get("jti"):
        return _dead_session("Invalid refresh token")

    user = await session.get(User, uuid.UUID(payload["sub"]))
    if user is None or user.status.value == "disabled":
        return _dead_session("Account unavailable")

    # Rotate BOTH tokens on every refresh (refresh-token rotation), preserving
    # the token's audience.
    access = create_access_token(
        user.id, user.role.value, user.tenant_id, audience=payload["aud"],
        session_id=payload["sid"],
    )
    refresh_candidate = create_refresh_token(
        user.id, audience=payload["aud"], session_id=payload["sid"]
    )
    new_jti = pyjwt.decode(
        refresh_candidate, get_settings().jwt_secret,
        algorithms=[ALGORITHM], audience=payload["aud"],
    )["jti"]
    # A refresh renews the idle deadline only when a person caused it. The
    # common refresh is a poll's 401 being repaired, and renewing on that is
    # how a forgotten tab used to stay signed in for ever (services/auth_sessions).
    refresh_token = await auth_sessions.rotate(
        payload["sid"], user.id, payload["jti"], new_jti, refresh_candidate,
        touch=is_user_activity(request),
    )
    if refresh_token is None:
        return _dead_session("Session expired or revoked")
    set_auth_cookies(response, access, refresh_token)
    return {"refreshed": True}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    for name in (REFRESH_COOKIE, "pr_access"):
        token = request.cookies.get(name)
        if not token:
            continue
        try:
            payload = pyjwt.decode(
                token, get_settings().jwt_secret, algorithms=[ALGORITHM],
                audience=[AUDIENCE_OWNER, AUDIENCE_ORG, AUDIENCE_CANDIDATE],
                options={"verify_exp": False},
            )
        except pyjwt.PyJWTError:
            continue
        if payload.get("sid") and payload.get("sub"):
            await auth_sessions.revoke(payload["sid"], payload["sub"])
            break
    clear_auth_cookies(response)
    return {"logged_out": True}


# ── Password reset and change (auth spec section 9) ─────────────────────────
#
# THE SERVER CHANGES THE PASSWORD NOW. Firebase's own reset email and the
# browser-side password update are retired: the reset went around the security
# code entirely, and the change needed a separate call, on a fresh Firebase
# token, to revoke sessions, which a browser could simply not make.
# `tests/test_auth_hardening_removed.py` keeps both gone. Both flows are two
# steps after A-24: a security code sent to the account's email, exchanged
# for a single-use ticket, and the ticket exchanged for the new password.
# Firebase still stores the credential. Rate limits stay.

RESET_SENT_MESSAGE = (
    "If an account uses this address, we have sent a security code to it. "
    "Check your inbox and your spam folder."
)
CODE_REFUSED_DETAIL = (
    "That security code is not valid or has expired. Check the latest email, "
    "or ask for a new code."
)
TICKET_REFUSED_DETAIL = (
    "This password change has expired. Start again and ask for a new "
    "security code."
)
PASSWORD_UPDATE_FAILED_DETAIL = (
    "We could not change your password right now. Please start again in a moment."
)
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _normal_email(value: str) -> str:
    email = (value or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    return email


async def _accounts_for_email(session: AsyncSession, email: str) -> list[User]:
    """Every NON-DISABLED account on this address, across portals.

    One Firebase identity stands behind all of them, so a password reset
    reaches all of them and revokes all of their sessions.
    """
    rows = (await session.execute(
        select(User).where(func.lower(User.email) == email).order_by(User.created_at, User.id)
    )).scalars().all()
    return [row for row in rows if row.status != UserStatus.disabled]


def _masked(email: str) -> str:
    local, _, domain = email.partition("@")
    head = local[:1] if local else ""
    return f"{head}***@{domain}" if domain else "your email address"


async def _replace_password(uid: str, password: str) -> None:
    """Set the Firebase password and revoke its refresh tokens, or 503."""
    try:
        await firebase_auth.set_password(uid, password)
        await firebase_auth.revoke_refresh_tokens(uid)
    except firebase_auth.IdentityOperationFailed as exc:
        raise HTTPException(status_code=503, detail=PASSWORD_UPDATE_FAILED_DETAIL) from exc


async def _identity_uid(email: str) -> str | None:
    try:
        return await firebase_auth.identity_exists(email)
    except firebase_auth.IdentityOperationFailed as exc:
        raise HTTPException(
            status_code=503,
            detail="Sign-in is briefly unavailable. Please try again in a moment.",
        ) from exc


@router.post(
    "/password-reset/request",
    response_model=SecurityCodeSentOut,
    dependencies=[Depends(rate_limit("password_reset", limit=10, window=600))],
)
async def password_reset_request(
    body: PasswordResetRequestIn,
    session: AsyncSession = Depends(get_identity_session),
) -> SecurityCodeSentOut:
    """Send a security code for a forgotten password. ENUMERATION SAFE.

    The answer is the same sentence whether or not an account uses the
    address, and a code is ISSUED (and its resend window started) either way,
    so neither the answer, a second request's cooldown nor a later verify can
    tell a registered address from an unregistered one. Only an address with a
    live account AND a Firebase identity is actually written to.
    """
    email = _normal_email(body.email)
    code = await security_codes.issue_code("password_reset", email)
    accounts = await _accounts_for_email(session, email)
    if accounts and await _identity_uid(email):
        security_email.dispatch_security_email(
            email, "security_code_password", {"code": code, "action": "reset"},
            session=session,
        )
        await record_auth_event(
            session,
            action=AUTH_PASSWORD_CHANGE_CODE_SENT,
            actor_user_id=accounts[0].id,
            tenant_id=accounts[0].tenant_id,
            metadata={"flow": "reset"},
        )
        await session.commit()
    return SecurityCodeSentOut(message=RESET_SENT_MESSAGE)


@router.post(
    "/password-reset/verify",
    response_model=PasswordResetVerifyOut,
    dependencies=[Depends(rate_limit("password_reset_verify", limit=20, window=600))],
)
async def password_reset_verify(body: PasswordResetVerifyIn) -> PasswordResetVerifyOut:
    """Exchange a right code for a single-use reset token (ten minutes)."""
    email = _normal_email(body.email)
    if not await security_codes.check_code("password_reset", email, body.code):
        raise HTTPException(status_code=400, detail=CODE_REFUSED_DETAIL)
    return PasswordResetVerifyOut(
        reset_token=await security_codes.mint_ticket("password_reset", email)
    )


@router.post(
    "/password-reset/complete",
    response_model=PasswordChangedOut,
    dependencies=[Depends(rate_limit("password_reset_complete", limit=20, window=600))],
)
async def password_reset_complete(
    body: PasswordResetCompleteIn,
    session: AsyncSession = Depends(get_identity_session),
) -> PasswordChangedOut:
    """Set the new password, then revoke every session of every account on
    the address, in Firebase and here. The rule is checked BEFORE the token is
    spent, so a weak password costs a retry, not the whole flow."""
    password_policy.require_valid(body.password)
    email = await security_codes.redeem_ticket("password_reset", body.reset_token)
    if not email:
        raise HTTPException(status_code=400, detail=TICKET_REFUSED_DETAIL)
    uid = await _identity_uid(email)
    accounts = await _accounts_for_email(session, email)
    if uid is None or not accounts:
        raise HTTPException(status_code=400, detail=TICKET_REFUSED_DETAIL)
    await _replace_password(uid, body.password)
    for account in accounts:
        await auth_sessions.revoke_all(account.id)
        await record_auth_event(
            session,
            action=AUTH_PASSWORD_CHANGED,
            actor_user_id=account.id,
            tenant_id=account.tenant_id,
            metadata={"flow": "reset"},
        )
    await session.commit()
    return PasswordChangedOut(
        message="Your password has been changed. Sign in with your new password."
    )


@router.post(
    "/password-change/request",
    response_model=SecurityCodeSentOut,
    dependencies=[Depends(rate_limit("password_change", limit=10, window=600))],
)
async def password_change_request(
    body: PasswordChangeRequestIn,
    current: CurrentUser = Depends(get_current_any),
    session: AsyncSession = Depends(get_identity_session),
) -> SecurityCodeSentOut:
    """Send a security code to the signed-in account's own email address."""
    user = await session.get(User, current.user_id)
    if user is None or user.status == UserStatus.disabled:
        raise HTTPException(status_code=401, detail="Account unavailable")
    if not user.email:
        raise HTTPException(
            status_code=409,
            detail="This account has no email address to send a security code to.",
        )
    code = await security_codes.issue_code("password_change", str(user.id))
    security_email.dispatch_security_email(
        user.email, "security_code_password", {"code": code, "action": "change"},
        session=session,
    )
    await record_auth_event(
        session,
        action=AUTH_PASSWORD_CHANGE_CODE_SENT,
        actor_user_id=user.id,
        tenant_id=user.tenant_id,
        metadata={"flow": "change"},
    )
    await session.commit()
    return SecurityCodeSentOut(
        message=f"We sent a security code to {_masked(user.email)}."
    )


@router.post(
    "/password-change/verify",
    response_model=PasswordChangeVerifyOut,
    dependencies=[Depends(rate_limit("password_change_verify", limit=20, window=600))],
)
async def password_change_verify(
    body: PasswordChangeVerifyIn,
    current: CurrentUser = Depends(get_current_any),
) -> PasswordChangeVerifyOut:
    """Exchange a right code for a single-use change token (ten minutes)."""
    subject = str(current.user_id)
    if not await security_codes.check_code("password_change", subject, body.code):
        raise HTTPException(status_code=400, detail=CODE_REFUSED_DETAIL)
    return PasswordChangeVerifyOut(
        change_token=await security_codes.mint_ticket("password_change", subject)
    )


@router.post(
    "/password-change/complete",
    response_model=PasswordChangedOut,
    dependencies=[Depends(rate_limit("password_change_complete", limit=20, window=600))],
)
async def password_change_complete(
    body: PasswordChangeCompleteIn,
    response: Response,
    current: CurrentUser = Depends(get_current_any),
    session: AsyncSession = Depends(get_identity_session),
) -> PasswordChangedOut:
    """Set the new password, revoke every OTHER session, re-issue this one.

    Every app session of every account on this email is revoked (one Firebase
    identity stands behind all of them), Firebase's refresh tokens are
    revoked, and the browser that made the change gets a FRESH session, so
    the person who changed their password stays signed in here and nowhere
    else.
    """
    password_policy.require_valid(body.password)
    subject = await security_codes.redeem_ticket("password_change", body.change_token)
    if subject != str(current.user_id):
        raise HTTPException(status_code=400, detail=TICKET_REFUSED_DETAIL)
    user = await session.get(User, current.user_id)
    if user is None or user.status == UserStatus.disabled or not user.email:
        raise HTTPException(status_code=401, detail="Account unavailable")
    uid = user.firebase_uid or await _identity_uid(user.email)
    if uid is None:
        raise HTTPException(
            status_code=409,
            detail="This account has no password sign-in to change.",
        )
    await _replace_password(uid, body.password)
    accounts = await _accounts_for_email(session, user.email.strip().lower())
    for account_id in {user.id, *(account.id for account in accounts)}:
        await auth_sessions.revoke_all(account_id)
    await record_auth_event(
        session,
        action=AUTH_PASSWORD_CHANGED,
        actor_user_id=user.id,
        tenant_id=user.tenant_id,
        metadata={"flow": "change"},
    )
    if "password" not in (user.auth_providers or []):
        user.auth_providers = sorted(set((user.auth_providers or []) + ["password"]))
    await session.commit()
    await _issue_session(response, user, audience_for_role(user.role))
    return PasswordChangedOut(
        message="Your password has been changed. Other devices have been signed out."
    )


@router.get("/me", response_model=MeOut)
async def me(
    current: CurrentUser = Depends(get_current_any),
    session: AsyncSession = Depends(get_identity_session),
) -> MeOut:
    user = await session.get(User, current.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown user")
    return MeOut(
        user=await _user_out(session, user),
        capabilities=await _capabilities(session, user),
    )
