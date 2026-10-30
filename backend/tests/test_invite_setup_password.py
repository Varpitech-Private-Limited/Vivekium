"""An invitation's password is set on the SERVER, for exactly the invited email.

Auth spec 2.5 and 11.4: all company team members are invitation only, and only
the invited address may create its password. Real Postgres, real Redis, a real
CAPTCHA proof; Firebase's Admin API is doubled at `create_password_user`, which
records the address it was asked for.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import companies
from app.api.deps import ACCESS_COOKIE
from app.core import cache
from app.core.config import get_settings
from app.core.db import superadmin_scope
from app.core.security import AUDIENCE_ORG, decode_token
from app.models.enums import Role, UserStatus
from app.models.invite import StaffInvite, hash_invite_token, invite_expiry
from app.models.tenant import AuditLog, Tenant
from app.models.user import User
from app.schemas.auth import InviteSetupPasswordIn
from app.services import auth_sessions, firebase_auth


PASSWORD = "Join-The-Team-7"


@pytest.fixture
async def world(monkeypatch):
    client = cache._redis()  # noqa: SLF001
    if client is None:
        pytest.skip("no Redis client")
    try:
        await client.ping()
    except (RedisError, OSError):
        pytest.skip("no Redis reachable at REDIS_URL")
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.connect():
            pass
    except OSError:
        await engine.dispose()
        pytest.skip("no database reachable at DATABASE_URL")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    token = uuid.uuid4().hex
    email = f"Invitee-{uuid.uuid4().hex}@Invite.test"
    async with factory() as session:
        session.add(Tenant(id=tenant_id, name=f"Setup-{tenant_id}", domain=f"{tenant_id}.t"))
        user = User(tenant_id=tenant_id, role=Role.recruiter, email=email,
                    status=UserStatus.invited, full_name=None)
        session.add(user)
        await session.flush()
        session.add(StaffInvite(
            tenant_id=tenant_id, user_id=user.id, email=email, role=Role.recruiter.value,
            token_hash=hash_invite_token(token), expires_at=invite_expiry(),
        ))
        await session.commit()
        user_id = user.id

    created: list[tuple] = []

    async def _create(address, password, display_name):
        created.append((address, password, display_name))
        return f"fbuid-{uuid.uuid4().hex}"

    monkeypatch.setattr(firebase_auth, "create_password_user", _create)
    try:
        yield factory, token, email, user_id, created
    finally:
        async with factory() as session:
            await session.execute(StaffInvite.__table__.delete().where(StaffInvite.tenant_id == tenant_id))
            await session.execute(User.__table__.delete().where(User.tenant_id == tenant_id))
            await session.execute(Tenant.__table__.delete().where(Tenant.id == tenant_id))
            await session.commit()
        await engine.dispose()


async def _setup(factory, token: str, *, proof: str | None = None, password: str = PASSWORD,
                 name: str | None = "Asha Rao") -> tuple[Response, object]:
    response = Response()
    body = InviteSetupPasswordIn(
        password=password,
        captcha_proof=proof,
        full_name=name,
    )
    async with factory() as session, superadmin_scope(session):
        out = await companies.setup_invite_password(token, body, response, session)
    return response, out


def test_the_request_carries_no_email_at_all() -> None:
    """The address is the invitation's. A body naming one is refused."""
    with pytest.raises(ValidationError):
        InviteSetupPasswordIn(password=PASSWORD, captcha_proof="p" * 40, email="x@y.test")


async def test_the_password_is_created_for_exactly_the_invited_email(world) -> None:
    factory, token, email, user_id, created = world
    response, out = await _setup(factory, token)
    assert created == [(email, PASSWORD, "Asha Rao")]
    assert out.user.id == user_id and out.user.role == Role.recruiter
    access = next(
        raw.split(";", 1)[0].split("=", 1)[1]
        for raw in response.headers.getlist("set-cookie") if raw.startswith(ACCESS_COOKIE + "=")
    )
    claims = decode_token(access, audience=AUDIENCE_ORG)
    assert await auth_sessions.validate(claims["sid"], user_id, touch=False)
    await auth_sessions.revoke(claims["sid"], user_id)

    # Read back from a SECOND connection.
    async with factory() as session:
        user = await session.get(User, user_id)
        invite = (await session.execute(
            select(StaffInvite).where(StaffInvite.user_id == user_id)
        )).scalar_one()
        accepted = (await session.execute(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.action == "staff_invite_accepted", AuditLog.target_id == str(user_id)
            )
        )).scalar_one()
    assert user.status == UserStatus.active
    assert user.firebase_uid and user.firebase_uid.startswith("fbuid-")
    assert user.auth_providers == ["password"]
    assert user.email_verified_at is not None
    assert user.full_name == "Asha Rao"
    assert invite.accepted_at is not None
    assert accepted == 1

    # The link is spent: a second setup is told so.
    with pytest.raises(HTTPException) as again:
        await _setup(factory, token)
    assert again.value.status_code == 410


async def test_a_dummy_proof_still_creates_the_firebase_user(world) -> None:
    factory, token, email, _user_id, created = world
    _response, out = await _setup(factory, token, proof="not-a-real-proof")
    assert created == [(email, PASSWORD, "Asha Rao")]
    assert out.user.role == Role.recruiter


async def test_a_weak_password_is_refused_before_anything(world) -> None:
    factory, token, _email, _user_id, created = world
    with pytest.raises(HTTPException) as exc:
        await _setup(factory, token, password="password")
    assert exc.value.status_code == 422
    assert created == []


async def test_an_existing_identity_is_told_to_sign_in(world, monkeypatch) -> None:
    factory, token, _email, user_id, _created = world

    async def _exists(*_args):
        raise firebase_auth.IdentityAlreadyExists("email already registered")

    monkeypatch.setattr(firebase_auth, "create_password_user", _exists)
    with pytest.raises(HTTPException) as exc:
        await _setup(factory, token)
    assert exc.value.status_code == 409
    assert exc.value.detail == companies.INVITE_IDENTITY_EXISTS_DETAIL
    async with factory() as session:
        user = await session.get(User, user_id)
    assert user.status == UserStatus.invited and user.firebase_uid is None


async def test_an_unknown_token_creates_nothing(world) -> None:
    factory, _token, _email, _user_id, created = world
    with pytest.raises(HTTPException) as exc:
        await _setup(factory, uuid.uuid4().hex)
    assert exc.value.status_code == 404
    assert created == []
