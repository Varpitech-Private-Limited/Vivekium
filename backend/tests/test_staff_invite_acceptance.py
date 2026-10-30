"""Signing in as the invited user accepts the invitation (PR #5 item 5, ported).

THE REPORTED SYMPTOM
--------------------
A Recruiter had been signed in and working for weeks while the staff table
still reported their invitation as "Pending". `users.status` is flipped
invited -> active by ANY proven sign-in; `staff_invites.accepted_at` was
written only by `POST /companies/invites/{token}/accept`, reached by clicking
the emailed link. An invitee who signed in directly showed Active and Pending
at once, for ever.

WHAT IS PINNED HERE
-------------------
* `services/staff_invites.accept_pending_invite` touches only a genuinely
  pending row, and audits the acceptance in one insert;
* both session-issuing paths call it (the Firebase exchange and the
  workspace chooser), read back from a SECOND connection;
* the /join page's own second step, which posts the acceptance AFTER the
  sign-in that already accepted it, is answered as accepted, not as a spent
  link, and moves nothing.

Every assertion reads committed state from a second session, because a write
that answered and rolled back is invisible to an assertion on a return value.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException, Response
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import companies as companies_api
from app.api.auth import firebase_session
from app.api.deps import CurrentUser
from app.core import cache
from app.core.config import get_settings
from app.models.enums import Role, UserStatus
from app.models.invite import StaffInvite, hash_invite_token, invite_expiry
from app.models.tenant import AuditLog, Tenant
from app.models.user import User
from app.schemas.auth import FirebaseSessionIn
from app.services import firebase_auth, login_context, staff_invites
from app.services.firebase_auth import FirebaseIdentity

pytestmark = pytest.mark.asyncio


async def _factory_or_skip():
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.connect():
            pass
    except OSError:
        await engine.dispose()
        pytest.skip("no database reachable at DATABASE_URL")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _redis_or_skip() -> None:
    client = cache._redis()  # noqa: SLF001
    if client is None:
        pytest.skip("no Redis client could be built for REDIS_URL")
    try:
        await client.ping()
    except (RedisError, OSError):
        pytest.skip("no Redis reachable at REDIS_URL")


def _invite(tenant_id: uuid.UUID, user: User, token: str) -> StaffInvite:
    return StaffInvite(
        tenant_id=tenant_id, user_id=user.id, email=user.email,
        role=Role.recruiter.value, token_hash=hash_invite_token(token),
        expires_at=invite_expiry(),
    )


@pytest.fixture
async def world():
    """One tenant, one INVITED recruiter, one pending invitation. `shape`
    lets a test bend the invitation (accepted, revoked, expired) before the
    commit; everything is removed afterwards."""
    engine, factory = await _factory_or_skip()
    tenant_id = uuid.uuid4()
    token = uuid.uuid4().hex

    async def build(shape=lambda invite: None) -> tuple[uuid.UUID, uuid.UUID, str, str]:
        async with factory() as session:
            session.add(Tenant(id=tenant_id, name=f"Invite-{tenant_id}",
                               domain=f"{tenant_id}.invite.test"))
            user = User(
                tenant_id=tenant_id, role=Role.recruiter,
                email=f"invitee-{uuid.uuid4().hex}@invite.test",
                status=UserStatus.invited, full_name="Invited Recruiter",
            )
            session.add(user)
            await session.flush()
            invite = _invite(tenant_id, user, token)
            shape(invite)
            session.add(invite)
            await session.commit()
            return tenant_id, user.id, user.email, token

    try:
        yield factory, build
    finally:
        async with factory() as session:
            await session.execute(
                StaffInvite.__table__.delete().where(StaffInvite.tenant_id == tenant_id)
            )
            await session.execute(User.__table__.delete().where(User.tenant_id == tenant_id))
            await session.execute(Tenant.__table__.delete().where(Tenant.id == tenant_id))
            await session.commit()
        await engine.dispose()


async def _stored(factory, user_id: uuid.UUID) -> tuple[User, StaffInvite, int]:
    """The user, their invitation and the count of acceptance audit rows, from
    a fresh session."""
    async with factory() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        invite = (
            await session.execute(select(StaffInvite).where(StaffInvite.user_id == user_id))
        ).scalar_one()
        audits = (
            await session.execute(
                select(func.count()).select_from(AuditLog).where(
                    AuditLog.action == staff_invites.STAFF_INVITE_ACCEPTED,
                    AuditLog.target_id == str(user_id),
                )
            )
        ).scalar_one()
    return user, invite, audits


# -- the helper ---------------------------------------------------------------


async def test_a_pending_invitation_is_accepted_and_audited_once(world) -> None:
    factory, build = world
    _tid, uid, _email, _token = await build()
    async with factory() as session:
        accepted = await staff_invites.accept_pending_invite(session, uid)
        await session.commit()
    assert accepted is not None
    _user, invite, audits = await _stored(factory, uid)
    assert invite.accepted_at is not None
    assert audits == 1


async def test_an_accepted_invitation_is_not_restamped(world) -> None:
    """`accepted_at` records the FIRST acceptance. Re-stamping it on every later
    sign-in would turn an audit fact into a last-seen clock."""
    factory, build = world
    first = datetime.now(timezone.utc) - timedelta(days=3)

    def accepted(invite: StaffInvite) -> None:
        invite.accepted_at = first

    _tid, uid, _email, _token = await build(accepted)
    async with factory() as session:
        assert await staff_invites.accept_pending_invite(session, uid) is None
        await session.commit()
    _user, invite, audits = await _stored(factory, uid)
    assert abs((invite.accepted_at - first).total_seconds()) < 1
    assert audits == 0


async def test_a_revoked_invitation_is_not_resurrected(world) -> None:
    """Withdrawing an invitation must not be undone by the invitee signing in."""
    factory, build = world

    def revoked(invite: StaffInvite) -> None:
        invite.revoked_at = datetime.now(timezone.utc)

    _tid, uid, _email, _token = await build(revoked)
    async with factory() as session:
        assert await staff_invites.accept_pending_invite(session, uid) is None
        await session.commit()
    _user, invite, audits = await _stored(factory, uid)
    assert invite.accepted_at is None
    assert audits == 0


async def test_an_expired_invitation_still_has_to_be_resent(world) -> None:
    """Matches `_resolve_invite`'s 410: expiry is not waived by a later login."""
    factory, build = world

    def expired(invite: StaffInvite) -> None:
        invite.expires_at = datetime.now(timezone.utc) - timedelta(days=1)

    _tid, uid, _email, _token = await build(expired)
    async with factory() as session:
        assert await staff_invites.accept_pending_invite(session, uid) is None
        await session.commit()
    _user, invite, _audits = await _stored(factory, uid)
    assert invite.accepted_at is None


async def test_a_user_with_no_invitation_is_left_alone() -> None:
    """Every candidate sign-in reaches this call; it must be a quiet no-op."""
    engine, factory = await _factory_or_skip()
    try:
        async with factory() as session:
            assert await staff_invites.accept_pending_invite(session, uuid.uuid4()) is None
    finally:
        await engine.dispose()


# -- the wiring: both session-issuing paths -----------------------------------


async def test_the_firebase_sign_in_accepts_the_invitation(world, monkeypatch) -> None:
    """The actual reported bug: the invitee never clicks the link, signs in
    with the invited address, and is matched by email."""
    await _redis_or_skip()
    factory, build = world
    _tid, uid, email, _token = await build()
    identity = FirebaseIdentity(
        uid=f"fbuid-{uuid.uuid4().hex}", email=email,
        name="Invited Recruiter", provider="password", email_verified=True,
    )
    monkeypatch.setattr(firebase_auth, "verify_id_token", lambda _token: identity)
    async with factory() as session:
        await firebase_session(
            FirebaseSessionIn(
                id_token="t" * 40,
                captcha_purpose="company_login",
            ),
            Response(),
            session,
        )

    user, invite, audits = await _stored(factory, uid)
    assert user.status == UserStatus.active, "signing in activates the account"
    assert invite.accepted_at is not None, (
        "...and the invitation stops reporting Pending, which is the bug"
    )
    assert audits == 1


async def test_the_workspace_chooser_accepts_the_invitation(world) -> None:
    """The second door: an address with more than one workspace signs in
    through the chooser, which issues the session for the picked user."""
    await _redis_or_skip()
    factory, build = world
    _tid, uid, email, _token = await build()
    token = login_context.make_context_token(email, [uid])
    async with factory() as session:
        await login_context.select_context(session, context_token=token, user_id=uid)
        await session.commit()

    user, invite, audits = await _stored(factory, uid)
    assert user.status == UserStatus.active
    assert invite.accepted_at is not None
    assert audits == 1


# -- the /join page's own second step -----------------------------------------


def _as(user_id: uuid.UUID, tenant_id: uuid.UUID) -> CurrentUser:
    return CurrentUser(user_id=user_id, tenant_id=tenant_id, role=Role.recruiter,
                       audience="pickready:org")


async def test_join_after_sign_in_is_accepted_not_a_spent_link(world) -> None:
    """/join signs the invitee in (which now accepts) and THEN posts the
    acceptance. That second call must answer accepted, not 410, and must not
    move the first acceptance or write a second audit row."""
    factory, build = world
    tid, uid, _email, token = await build()
    async with factory() as session:
        await staff_invites.accept_pending_invite(session, uid)
        await session.commit()
    _user, before, _audits = await _stored(factory, uid)

    async with factory() as session:
        out = await companies_api.accept_invite(token, user=_as(uid, tid), session=session)
        await session.commit()
    assert out.accepted is True

    _user, after, audits = await _stored(factory, uid)
    assert after.accepted_at == before.accepted_at
    assert audits == 1


async def test_join_by_someone_else_is_still_refused(world) -> None:
    """The accepted-by-this-user admission is exactly that wide: a different
    signed-in member still reads the spent-link refusal."""
    factory, build = world
    tid, uid, _email, token = await build()
    async with factory() as session:
        await staff_invites.accept_pending_invite(session, uid)
        await session.commit()
    async with factory() as session:
        with pytest.raises(HTTPException) as refused:
            await companies_api.accept_invite(
                token, user=_as(uuid.uuid4(), tid), session=session
            )
    assert refused.value.status_code == 410


async def test_join_with_a_pending_link_still_accepts_through_the_token(world) -> None:
    """The emailed link keeps working on its own: a pending invitation is
    accepted by the /join handler and audited there, once."""
    factory, build = world
    tid, uid, _email, token = await build()
    async with factory() as session:
        out = await companies_api.accept_invite(token, user=_as(uid, tid), session=session)
        await session.commit()
    assert out.accepted is True
    _user, invite, audits = await _stored(factory, uid)
    assert invite.accepted_at is not None
    assert audits == 1
