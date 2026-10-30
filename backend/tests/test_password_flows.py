"""Password reset and password change run on the server (auth spec 9, 34.4).

Real Postgres, real Redis, real CAPTCHA proofs and real security codes; only
Firebase's Admin API is doubled, at the four `firebase_auth` functions the
routes call. The code a person would read in their inbox is read from the
dispatched `pickready.send_security_email`, which is exactly where it goes.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException, Response
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import auth
from app.api.deps import ACCESS_COOKIE, CurrentUser
from app.core import cache
from app.core.config import get_settings
from app.core.db import superadmin_scope
from app.core.security import AUDIENCE_CANDIDATE, decode_token
from app.models.enums import Role
from app.models.tenant import AuditLog
from app.models.user import User
from app.schemas.auth import (
    PasswordChangeCompleteIn,
    PasswordChangeRequestIn,
    PasswordChangeVerifyIn,
    PasswordResetCompleteIn,
    PasswordResetRequestIn,
    PasswordResetVerifyIn,
)
from app.services import auth_sessions, firebase_auth, security_codes
from app.workers import dispatch
from tests.candidate_session import close_candidate_session, create_candidate_session, open_session


NEW_PASSWORD = "Fresh-Pass-2026"


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
    person = await create_candidate_session(factory)
    async with factory() as session:
        async with superadmin_scope(session):
            uid = (await session.get(User, person.user_id)).firebase_uid

    firebase: dict[str, list] = {"set": [], "revoked": []}

    async def _exists(email):
        return uid if email.lower() == person.email.lower() else None

    async def _set(target, password):
        firebase["set"].append((target, password))

    async def _revoke(target):
        firebase["revoked"].append(target)

    monkeypatch.setattr(firebase_auth, "identity_exists", _exists)
    monkeypatch.setattr(firebase_auth, "set_password", _set)
    monkeypatch.setattr(firebase_auth, "revoke_refresh_tokens", _revoke)
    try:
        yield factory, person, uid, firebase
    finally:
        await close_candidate_session(factory, person)
        await engine.dispose()


def _sent_code(to: str) -> str:
    records = [
        r for r in dispatch.recorded()
        if r.name == "pickready.send_security_email" and r.args[0] == to
    ]
    assert records, f"no security email was dispatched to {to}"
    to_address, template, context = records[-1].args
    assert template == "security_code_password"
    return context["code"]


async def _audits(factory, action: str, user_id) -> list[AuditLog]:
    async with factory() as session:
        return (await session.execute(
            select(AuditLog).where(AuditLog.action == action, AuditLog.actor_user_id == user_id)
        )).scalars().all()


# -- reset ----------------------------------------------------------------------


async def test_reset_sends_the_code_without_a_proof(world) -> None:
    factory, person, _uid, _fb = world
    async with factory() as session, superadmin_scope(session):
        sent = await auth.password_reset_request(
            PasswordResetRequestIn(email=person.email), session
        )
    assert sent.message == auth.RESET_SENT_MESSAGE
    assert _sent_code(person.email.lower())


async def test_reset_answers_the_same_for_an_unknown_address(world) -> None:
    factory, person, _uid, _fb = world
    stranger = f"nobody-{uuid.uuid4().hex}@reset.test"
    async with factory() as session, superadmin_scope(session):
        unknown = await auth.password_reset_request(
            PasswordResetRequestIn(email=stranger),
            session,
        )
    async with factory() as session, superadmin_scope(session):
        known = await auth.password_reset_request(
            PasswordResetRequestIn(email=person.email),
            session,
        )
    assert unknown == known
    assert unknown.message == auth.RESET_SENT_MESSAGE
    sent_to = [r.args[0] for r in dispatch.recorded() if r.name == "pickready.send_security_email"]
    assert sent_to == [person.email.lower()]
    # A second request for the stranger is held by the same cooldown a real
    # address gets, so the cooldown is no oracle either.
    async with factory() as session, superadmin_scope(session):
        with pytest.raises(security_codes.CodeCooldown):
            await auth.password_reset_request(
                PasswordResetRequestIn(
                    email=stranger
                ),
                session,
            )


async def test_reset_end_to_end_revokes_every_session(world) -> None:
    factory, person, uid, firebase = world
    async with factory() as session, superadmin_scope(session):
        await auth.password_reset_request(
            PasswordResetRequestIn(email=person.email),
            session,
        )
    code = _sent_code(person.email.lower())
    assert (await _audits(factory, "password_change_code_sent", person.user_id))[-1].metadata_json == {
        "flow": "reset"
    }

    with pytest.raises(HTTPException) as wrong:
        await auth.password_reset_verify(
            PasswordResetVerifyIn(email=person.email, code=f"{(int(code) + 1) % 10**6:06d}")
        )
    assert wrong.value.status_code == 400
    ticket = (await auth.password_reset_verify(
        PasswordResetVerifyIn(email=person.email, code=code)
    )).reset_token

    # A weak password is refused BEFORE the ticket is spent.
    async with factory() as session, superadmin_scope(session):
        with pytest.raises(HTTPException) as weak:
            await auth.password_reset_complete(
                PasswordResetCompleteIn(reset_token=ticket, password="short"), session
            )
    assert weak.value.status_code == 422

    assert await auth_sessions.validate(person.sid, person.user_id, touch=False)
    async with factory() as session, superadmin_scope(session):
        done = await auth.password_reset_complete(
            PasswordResetCompleteIn(reset_token=ticket, password=NEW_PASSWORD), session
        )
    assert done.password_changed is True
    assert firebase["set"] == [(uid, NEW_PASSWORD)]
    assert firebase["revoked"] == [uid]
    assert not await auth_sessions.validate(person.sid, person.user_id, touch=False)
    rows = await _audits(factory, "password_changed", person.user_id)
    assert rows and rows[-1].metadata_json == {"flow": "reset"}
    assert NEW_PASSWORD not in repr([r.metadata_json for r in rows])

    # The ticket was single use.
    async with factory() as session, superadmin_scope(session):
        with pytest.raises(HTTPException) as again:
            await auth.password_reset_complete(
                PasswordResetCompleteIn(reset_token=ticket, password=NEW_PASSWORD), session
            )
    assert again.value.status_code == 400


async def test_an_expired_reset_code_is_refused(world) -> None:
    factory, person, _uid, _fb = world
    async with factory() as session, superadmin_scope(session):
        await auth.password_reset_request(
            PasswordResetRequestIn(email=person.email),
            session,
        )
    code = _sent_code(person.email.lower())
    await cache._redis().delete(  # noqa: SLF001  expiry is the Redis TTL
        security_codes._key("password_reset", person.email)  # noqa: SLF001
    )
    with pytest.raises(HTTPException) as exc:
        await auth.password_reset_verify(PasswordResetVerifyIn(email=person.email, code=code))
    assert exc.value.status_code == 400


# -- change ---------------------------------------------------------------------


def _current(person) -> CurrentUser:
    return CurrentUser(
        user_id=person.user_id, tenant_id=None, role=Role.candidate, audience=AUDIENCE_CANDIDATE
    )


def _cookie(response: Response, name: str) -> str | None:
    for raw in response.headers.getlist("set-cookie"):
        if raw.startswith(name + "="):
            return raw.split(";", 1)[0].split("=", 1)[1]
    return None


async def test_change_sends_the_code_without_a_proof(world) -> None:
    factory, person, _uid, _fb = world
    async with factory() as session, superadmin_scope(session):
        sent = await auth.password_change_request(
            PasswordChangeRequestIn(),
            _current(person), session,
        )
    assert "security code" in sent.message and person.email not in sent.message
    code = _sent_code(person.email)
    with pytest.raises(HTTPException) as wrong:
        await auth.password_change_verify(
            PasswordChangeVerifyIn(code=f"{(int(code) + 1) % 10**6:06d}"), _current(person)
        )
    assert wrong.value.status_code == 400
    # A ticket minted for somebody else is not this person's.
    other = await security_codes.mint_ticket("password_change", str(uuid.uuid4()))
    async with factory() as session, superadmin_scope(session):
        with pytest.raises(HTTPException) as foreign:
            await auth.password_change_complete(
                PasswordChangeCompleteIn(change_token=other, password=NEW_PASSWORD),
                Response(), _current(person), session,
            )
    assert foreign.value.status_code == 400


async def test_change_revokes_other_sessions_and_reissues_this_one(world) -> None:
    factory, person, uid, firebase = world
    async with factory() as session, superadmin_scope(session):
        user = await session.get(User, person.user_id)
    other_sid, _access, _refresh = await open_session(user)

    async with factory() as session, superadmin_scope(session):
        await auth.password_change_request(
            PasswordChangeRequestIn(),
            _current(person), session,
        )
    code = _sent_code(person.email)
    ticket = (await auth.password_change_verify(
        PasswordChangeVerifyIn(code=code), _current(person)
    )).change_token
    response = Response()
    async with factory() as session, superadmin_scope(session):
        done = await auth.password_change_complete(
            PasswordChangeCompleteIn(change_token=ticket, password=NEW_PASSWORD),
            response, _current(person), session,
        )
    assert done.password_changed is True
    assert firebase["set"] == [(uid, NEW_PASSWORD)] and firebase["revoked"] == [uid]
    assert not await auth_sessions.validate(person.sid, person.user_id, touch=False)
    assert not await auth_sessions.validate(other_sid, person.user_id, touch=False)
    fresh = _cookie(response, ACCESS_COOKIE)
    assert fresh, "the browser that changed the password gets a fresh session"
    fresh_sid = decode_token(fresh, audience=AUDIENCE_CANDIDATE)["sid"]
    assert await auth_sessions.validate(fresh_sid, person.user_id, touch=False)
    rows = await _audits(factory, "password_changed", person.user_id)
    assert rows[-1].metadata_json == {"flow": "change"}
    await auth_sessions.revoke(fresh_sid, person.user_id)
