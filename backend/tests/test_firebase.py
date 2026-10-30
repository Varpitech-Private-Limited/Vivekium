"""Firebase-session endpoint hardening (claude.md rule 2: Firebase is
identity-only, DB roles/permissions stay authoritative).

Two layers:
- DB-free unit tests on the provider gate + owner-email predicate.
- Live integration against the app DB (skips cleanly when unreachable). We can't
  mint a real Firebase token in tests, so `firebase_auth.verify_id_token` is
  monkeypatched to return a chosen FirebaseIdentity; the endpoint logic (owner
  invariant, staff linking, multi-context chooser, provider gate, edge cases)
  is what is exercised.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.auth import (
    available_workspaces,
    firebase_session,
    select_context,
)
from app.api.deps import ACCESS_COOKIE, CurrentUser
from app.core.config import get_settings
from app.models.candidate import Candidate
from app.models.enums import Role, UserStatus
from app.models.tenant import AuditLog, Tenant
from app.models.user import User
from app.schemas.auth import FirebaseSessionIn, SelectContextIn
from app.services import firebase_auth
from app.services.login_context import decode_context_token
from app.services.firebase_auth import FirebaseIdentity, assert_provider_allowed
from fastapi import HTTPException, Response



# ── DB-free: provider gate (Google = candidates only) ────────────────────────

def _identity(provider: str = "password", email: str | None = "x@y.test",
              uid: str | None = None,
              name: str | None = "Test User", email_verified: bool = True) -> FirebaseIdentity:
    return FirebaseIdentity(
        uid=uid or f"fbuid-{uuid.uuid4().hex}", email=email,
        name=name, provider=provider, email_verified=email_verified,
    )


def test_google_allowed_for_candidate() -> None:
    assert_provider_allowed(_identity(provider="google.com"), "candidate")  # no raise


@pytest.mark.parametrize("role", ["super_admin", "bd"])
def test_google_allowed_for_the_platform_roles(role: str) -> None:
    """The Provider owner and BD keep the policy they had (auth spec 2.6)."""
    assert_provider_allowed(_identity(provider="google.com"), role)


def _org_roles() -> list[str]:
    from app.core.security import _ORG_ROLES

    return sorted(_ORG_ROLES)


@pytest.mark.parametrize("role", _org_roles())
def test_google_is_refused_for_every_company_role(role: str) -> None:
    """Every company role signs in with a password only (auth spec 2.6, 8).
    Parametrised over `core.security._ORG_ROLES` itself, so a role added to
    the org portal is swept the day it exists."""
    with pytest.raises(HTTPException) as exc:
        assert_provider_allowed(_identity(provider="google.com"), role)
    assert exc.value.status_code == 403
    assert exc.value.detail == "Google sign-in is available to candidates only"


@pytest.mark.parametrize("role", _org_roles())
def test_password_is_allowed_for_every_company_role(role: str) -> None:
    assert_provider_allowed(_identity(provider="password"), role)


@pytest.mark.parametrize("role", ["candidate", "super_admin", "hr_manager", "recruiter", "client"])
def test_password_allowed_for_all_roles(role: str) -> None:
    assert_provider_allowed(_identity(provider="password"), role)


def test_phone_provider_rejected() -> None:
    with pytest.raises(HTTPException) as exc:
        assert_provider_allowed(_identity(provider="phone"), "candidate")
    assert exc.value.status_code == 403


def test_unknown_provider_rejected() -> None:
    with pytest.raises(HTTPException) as exc:
        assert_provider_allowed(_identity(provider="apple.com"), "candidate")
    assert exc.value.status_code == 403


def test_the_identity_carries_no_phone_claim() -> None:
    """Phone sign-in was removed, so nothing downstream may match on a number.
    A field that is not there cannot be read by the next person to add a
    lookup."""
    assert "phone" not in {f.name for f in FirebaseIdentity.__dataclass_fields__.values()}


# ── Live integration (skips if the database is unreachable) ──────────────────

async def _db_or_skip():
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect():
            pass
    except Exception:  # noqa: BLE001 — any connect failure means "no DB here"
        await engine.dispose()
        pytest.skip("no database reachable — skipping firebase integration test")
    return engine


def _set_verify(monkeypatch, identity: FirebaseIdentity) -> None:
    """Patch the module attribute the endpoint looks up at call time."""
    monkeypatch.setattr(firebase_auth, "verify_id_token", lambda _tok: identity)


def _cookie_names(response: Response) -> list[str]:
    return [v.decode().split("=", 1)[0] for (k, v) in response.raw_headers
            if k == b"set-cookie"]


async def _call(
    session, identity, monkeypatch, purpose: str = "candidate_login"
) -> tuple[Response, object]:
    """The route as a browser reaches it: the portal surface, then the exchange.
    A proof is not required."""
    _set_verify(monkeypatch, identity)
    response = Response()
    body = FirebaseSessionIn(
        id_token="t" * 40,
        captcha_purpose=purpose,
    )
    out = await firebase_session(body, response, session)
    return response, out


async def _cleanup_users(factory, user_ids: list[uuid.UUID], tenant_ids: list[uuid.UUID] | None = None) -> None:
    async with factory() as session:
        for uid in user_ids:
            await session.execute(
                Candidate.__table__.delete().where(Candidate.user_id == uid)
            )
            await session.execute(User.__table__.delete().where(User.id == uid))
        for tid in (tenant_ids or []):
            await session.execute(Tenant.__table__.delete().where(Tenant.id == tid))
        await session.commit()


async def _get_or_create_owner(factory) -> uuid.UUID:
    owner_email = get_settings().owner_email
    async with factory() as session:
        owner = (await session.execute(
            select(User).where(User.role == Role.super_admin)
        )).scalars().first()
        if owner is None:
            owner = User(role=Role.super_admin, email=owner_email, tenant_id=None,
                         status=UserStatus.active, full_name="Owner")
            session.add(owner)
            await session.commit()
        return owner.id


# ── 1. OWNER INVARIANT ───────────────────────────────────────────────────────

async def test_owner_google_login_succeeds_for_configured_email() -> None:
    """The configured Owner email may use Google without changing its role."""
    engine = await _db_or_skip()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _get_or_create_owner(factory)
    monkeypatch = pytest.MonkeyPatch()
    try:
        ident = _identity(provider="google.com", email=get_settings().owner_email.upper())
        async with factory() as session:
            response, out = await _call(session, ident, monkeypatch, "provider_login")
        assert out.user.role == Role.super_admin
        assert "pr_access" in _cookie_names(response)
    finally:
        monkeypatch.undo()
        await engine.dispose()


async def test_random_email_never_becomes_super_admin() -> None:
    """An unknown identity is created as a candidate, never elevated to owner."""
    engine = await _db_or_skip()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch = pytest.MonkeyPatch()
    email = f"random-{uuid.uuid4().hex}@pickready.test"
    created: list[uuid.UUID] = []
    try:
        ident = _identity(provider="google.com", email=email)
        async with factory() as session:
            _, out = await _call(session, ident, monkeypatch)
        assert out.user is not None
        assert out.user.role == Role.candidate
        created.append(out.user.id)
        assert out.capabilities == []  # candidates carry no org capabilities
    finally:
        monkeypatch.undo()
        await _cleanup_users(factory, created)
        await engine.dispose()


# ── 2. STAFF LINKING BY EMAIL ────────────────────────────────────────────────

async def test_staff_first_login_links_uid_and_preserves_role() -> None:
    """A pre-seeded staff user (no firebase_uid) is found by email and LINKED,
    not duplicated; the role is preserved and org cookies are issued."""
    engine = await _db_or_skip()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch = pytest.MonkeyPatch()
    email = f"staff-{uuid.uuid4().hex}@pickready.test"
    tid = uuid.uuid4()
    created: list[uuid.UUID] = []
    try:
        async with factory() as session:
            session.add(Tenant(id=tid, name="FB-Staff-Test", domain=f"{tid}.fb.test"))
            staff = User(role=Role.hr_manager, email=email, tenant_id=tid,
                         status=UserStatus.invited)
            session.add(staff)
            await session.commit()
            created.append(staff.id)

        ident = _identity(provider="password", email=email)
        async with factory() as session:
            response, out = await _call(session, ident, monkeypatch, "company_login")
        assert out.user is not None
        assert out.user.role == Role.hr_manager
        assert out.user.id == created[0]
        assert ACCESS_COOKIE in _cookie_names(response)

        async with factory() as session:
            rows = (await session.execute(
                select(User).where(User.email == email)
            )).scalars().all()
            assert len(rows) == 1  # linked in place, no duplicate candidate
            assert rows[0].firebase_uid == ident.uid
            assert rows[0].status == UserStatus.active  # invited -> active
            assert ident.provider in rows[0].auth_providers
    finally:
        monkeypatch.undo()
        await _cleanup_users(factory, created, [tid])
        await engine.dispose()


# ── 3. MULTI-CONTEXT CHOOSER ─────────────────────────────────────────────────

async def test_multi_context_returns_chooser_then_select_issues_cookies() -> None:
    """One identity matching two users returns contexts + context_token and NO
    cookies; /auth/select-context then exchanges the token for cookies."""
    engine = await _db_or_skip()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch = pytest.MonkeyPatch()
    email = f"multi-{uuid.uuid4().hex}@pickready.test"
    ta, tb = uuid.uuid4(), uuid.uuid4()
    created: list[uuid.UUID] = []
    try:
        async with factory() as session:
            session.add(Tenant(id=ta, name="FB-Multi-A", domain=f"{ta}.fb.test"))
            session.add(Tenant(id=tb, name="FB-Multi-B", domain=f"{tb}.fb.test"))
            u1 = User(role=Role.recruiter, email=email, tenant_id=ta, status=UserStatus.active)
            u2 = User(role=Role.hr_manager, email=email, tenant_id=tb, status=UserStatus.active)
            session.add_all([u1, u2])
            await session.commit()
            created.extend([u1.id, u2.id])
            pick = u1.id

        ident = _identity(provider="password", email=email)
        async with factory() as session:
            response, out = await _call(session, ident, monkeypatch, "company_login")
        assert out.user is None
        assert out.context_token is not None
        assert len(out.contexts) == 2
        assert ACCESS_COOKIE not in _cookie_names(response)  # no cookies yet

        # Exchange the proof token for a real session as one of the two users.
        async with factory() as session:
            sel_response = Response()
            sel = await select_context(
                SelectContextIn(context_token=out.context_token, user_id=pick),
                sel_response, session,
            )
        assert sel.user is not None
        assert sel.user.id == pick
        assert sel.user.workspace_name == "FB-Multi-A"
        assert ACCESS_COOKIE in _cookie_names(sel_response)

        # A live session can reopen the chooser without a full sign-out. The
        # token records the previous context for the selection audit trail.
        async with factory() as session:
            choices = await available_workspaces(
                CurrentUser(
                    user_id=pick,
                    tenant_id=ta,
                    role=Role.recruiter,
                    audience="pickready:org",
                ),
                session,
            )
        assert {context.tenant_name for context in choices.contexts or []} == {
            "FB-Multi-A",
            "FB-Multi-B",
        }
        assert decode_context_token(choices.context_token)["source_user_id"] == str(pick)

        async with factory() as session:
            audit_row = (
                await session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.action == "context_selected",
                        AuditLog.actor_user_id == pick,
                    )
                    .order_by(AuditLog.at.desc())
                )
            ).scalars().first()
        assert audit_row is not None
        assert audit_row.metadata_json["workspace_name"] == "FB-Multi-A"
        assert audit_row.at is not None
    finally:
        monkeypatch.undo()
        await _cleanup_users(factory, created, [ta, tb])
        await engine.dispose()


# ── 4. PROVIDER GATE + EDGE CASES ────────────────────────────────────────────

async def test_no_email_is_422() -> None:
    engine = await _db_or_skip()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch = pytest.MonkeyPatch()
    try:
        ident = _identity(provider="password", email=None, name=None)
        async with factory() as session:
            with pytest.raises(HTTPException) as exc:
                await _call(session, ident, monkeypatch)
        assert exc.value.status_code == 422
        assert exc.value.detail == (
            "An email address is required to create a candidate profile"
        )
    finally:
        monkeypatch.undo()
        await engine.dispose()


async def test_disabled_user_gets_403() -> None:
    engine = await _db_or_skip()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch = pytest.MonkeyPatch()
    email = f"disabled-{uuid.uuid4().hex}@pickready.test"
    created: list[uuid.UUID] = []
    try:
        async with factory() as session:
            u = User(role=Role.candidate, email=email, tenant_id=None,
                     status=UserStatus.disabled)
            session.add(u)
            await session.commit()
            created.append(u.id)

        ident = _identity(provider="password", email=email)
        async with factory() as session:
            with pytest.raises(HTTPException) as exc:
                await _call(session, ident, monkeypatch)
        assert exc.value.status_code == 403
        # The disabled row must NOT have been duplicated by a fresh candidate.
        async with factory() as session:
            rows = (await session.execute(
                select(User).where(User.email == email)
            )).scalars().all()
            assert len(rows) == 1
    finally:
        monkeypatch.undo()
        await _cleanup_users(factory, created)
        await engine.dispose()


async def test_google_staff_login_is_refused_by_the_backend() -> None:
    """A company account may NOT sign in with Google, even when a caller
    skips the screen that hides the button (auth spec 7.2, 8). The refusal
    comes before anything is bound: the account keeps no Google uid."""
    engine = await _db_or_skip()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch = pytest.MonkeyPatch()
    email = f"gstaff-{uuid.uuid4().hex}@pickready.test"
    tid = uuid.uuid4()
    created: list[uuid.UUID] = []
    try:
        async with factory() as session:
            session.add(Tenant(id=tid, name="FB-GStaff", domain=f"{tid}.fb.test"))
            u = User(role=Role.recruiter, email=email, tenant_id=tid, status=UserStatus.active)
            session.add(u)
            await session.commit()
            created.append(u.id)

        ident = _identity(provider="google.com", email=email)
        async with factory() as session:
            with pytest.raises(HTTPException) as exc:
                await _call(session, ident, monkeypatch, "company_login")
        assert exc.value.status_code == 403
        assert exc.value.detail == "Google sign-in is available to candidates only"
        async with factory() as session:
            row = await session.get(User, created[0])
            assert row.firebase_uid is None
    finally:
        monkeypatch.undo()
        await _cleanup_users(factory, created, [tid])
        await engine.dispose()


async def test_email_verified_is_stamped() -> None:
    engine = await _db_or_skip()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch = pytest.MonkeyPatch()
    email = f"verif-{uuid.uuid4().hex}@pickready.test"
    created: list[uuid.UUID] = []
    try:
        ident = _identity(provider="password", email=email, email_verified=True)
        async with factory() as session:
            _, out = await _call(session, ident, monkeypatch)
        created.append(out.user.id)
        assert out.user.email_verified is True
    finally:
        monkeypatch.undo()
        await _cleanup_users(factory, created)
        await engine.dispose()


# ── Clock skew: the bug that broke every sign-in (killer-spec) ───────────────
#
# A Docker Desktop VM whose clock sits one second behind Google's signing
# servers makes EVERY freshly minted ID token look like it was used before it
# was issued, and firebase-admin rejects it outright:
#
#   InvalidIdTokenError: Token used too early, 1785249345 < 1785249346
#
# Email/password and Google sign-in both 401 on a perfectly valid token, and
# nothing about the request looks wrong. These tests pin the fix, because it is
# a single keyword argument that a refactor could silently drop.

class _RecordingClient:
    """Stand-in for firebase_admin.auth, capturing how it was called."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._raises = raises

    def verify_id_token(self, token, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return {
            "uid": "fbuid-clock",
            "email": "clock@test.local",
            "email_verified": True,
            "firebase": {"sign_in_provider": "password"},
        }


def test_verification_allows_a_minute_of_clock_skew(monkeypatch) -> None:
    client = _RecordingClient()
    monkeypatch.setattr(firebase_auth, "firebase_client", lambda: client)

    identity = firebase_auth.verify_id_token("any-token")

    assert identity.uid == "fbuid-clock"
    assert client.calls, "verify_id_token was never called"
    kwargs = client.calls[0]
    # 60s is the maximum firebase-admin accepts, and what the official docs
    # recommend for exactly this failure.
    assert kwargs.get("clock_skew_seconds") == 60
    # Revocation checking must NOT be traded away for the skew tolerance.
    assert kwargs.get("check_revoked") is True


def test_a_clock_skew_rejection_says_so(monkeypatch) -> None:
    """Every failure returning the same "Invalid Firebase session" is what
    turned a one-line bug into an afternoon of guessing. The message names the
    CATEGORY of failure, and never the token or the claims."""
    error = ValueError("Token used too early, 1785249345 < 1785249346.")
    monkeypatch.setattr(
        firebase_auth, "firebase_client", lambda: _RecordingClient(raises=error)
    )
    with pytest.raises(HTTPException) as caught:
        firebase_auth.verify_id_token("any-token")
    assert caught.value.status_code == 401
    assert "clock" in caught.value.detail.lower()
    assert "1785249345" not in caught.value.detail


def test_an_unconfigured_server_is_503_not_401(monkeypatch) -> None:
    """A missing service account is a SERVER fault. Answering 401 sends an
    operator round the login screen chasing a credential problem that is not
    theirs."""
    def _unavailable():
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON is not configured")

    monkeypatch.setattr(firebase_auth, "firebase_client", _unavailable)
    with pytest.raises(HTTPException) as caught:
        firebase_auth.verify_id_token("any-token")
    assert caught.value.status_code == 503


def test_an_expired_token_is_distinguishable_from_a_revoked_one(monkeypatch) -> None:
    for error, expected in (
        (ValueError("The Firebase ID token has expired."), "expired"),
        (ValueError("The Firebase ID token has been revoked."), "revoked"),
    ):
        monkeypatch.setattr(
            firebase_auth, "firebase_client", lambda e=error: _RecordingClient(raises=e)
        )
        with pytest.raises(HTTPException) as caught:
            firebase_auth.verify_id_token("any-token")
        assert expected in caught.value.detail.lower(), caught.value.detail
