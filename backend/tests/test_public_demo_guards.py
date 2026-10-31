"""Guards around public demo purchase and invite setup.

The suite database is the one `get_settings().database_url` already names.
Nothing here opens a second database, and the ledger is read from Postgres
on a second connection after the response commits. Razorpay and Firebase are
patched only at the vendor call.
"""
from __future__ import annotations

import sys
import uuid

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import billing as billing_api
from app.api import companies as companies_api
from app.api.deps import CurrentUser, get_current_user, get_tenant_db
from app.core.config import get_settings
from app.core.db import get_identity_session, superadmin_scope, tenant_scope
from app.core.security import AUDIENCE_ORG
from app.main import app
from app.models.billing import (
    PURCHASE_PAID,
    STARTER_PACK_SLUG,
    STARTER_PACK_TOTAL_CREDITS,
    SUBUNITS_PER_CREDIT,
    CreditPurchase,
)
from app.models.enums import Role, UserStatus
from app.models.invite import StaffInvite, hash_invite_token, invite_expiry
from app.models.tenant import Tenant
from app.models.user import User
from app.services import credits, demo_access, firebase_auth, razorpay
from app.services.password_policy import PASSWORD_RULE_DETAIL

# A proof is accepted and not read. A short value must not be a validation error.
DUMMY_PROOF = "x" * 16
SHORT_PASSWORD = "short"
PASSWORD = "Join-The-Team-7"
PACK_SUBUNITS = STARTER_PACK_TOTAL_CREDITS * SUBUNITS_PER_CREDIT
PURCHASE_PATH = "/api/v1/billing/demo/purchase"


def _demo_settings(monkeypatch, *, mode: bool):
    """Replace the settings object `demo_access.get_settings()` returns.

    Route modules that imported `enabled` by value are pointed back at the
    function, so they read this object rather than a copy taken at import.
    """
    current = demo_access.get_settings()
    updates: dict = {"demo_mode": mode}
    if "demo_mode_confirm" in type(current).model_fields:
        updates["demo_mode_confirm"] = "public-demo"
    settings = current.model_copy(update=updates)
    monkeypatch.setattr(demo_access, "get_settings", lambda bound=settings: bound)
    target = demo_access.enabled
    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith("app.api"):
            continue
        for attr in dir(module):
            if attr.startswith("_"):
                continue
            if getattr(module, attr, None) is target:
                monkeypatch.setattr(module, attr, target)
    return settings


def _forbid_razorpay(monkeypatch) -> list:
    calls: list = []

    async def _create_order(**kwargs):
        calls.append(kwargs)
        raise AssertionError("razorpay.create_order must not be called")

    monkeypatch.setattr(razorpay, "create_order", _create_order)
    for module in (billing_api, companies_api):
        if getattr(module, "create_order", None) is not None:
            monkeypatch.setattr(module, "create_order", _create_order)
    return calls


class World:
    """One HTTP client, one engine on the suite database, many throwaway tenants."""

    def __init__(
        self,
        factory,
        http: httpx.AsyncClient,
        box: dict,
        created: list,
        firebase_calls: list,
    ) -> None:
        self.factory = factory
        self.http = http
        self.box = box
        self.created = created
        self.firebase_calls = firebase_calls
        self.tenant_id: uuid.UUID | None = None
        self.user_id: uuid.UUID | None = None
        self.email: str | None = None
        self.token: str | None = None

    async def add_billing_tenant(self, *, public_demo: bool, is_demo: bool = False) -> None:
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        self.created.append(tenant_id)
        async with self.factory() as session, session.begin():
            async with superadmin_scope(session):
                session.add(Tenant(
                    id=tenant_id,
                    name=f"Guard {tenant_id.hex[:8]}",
                    domain=f"{tenant_id.hex}.guard.test",
                    public_demo=public_demo,
                    is_demo=is_demo,
                ))
                session.add(User(
                    id=user_id,
                    tenant_id=tenant_id,
                    role=Role.client,
                    email=f"buyer-{tenant_id.hex[:12]}@guard.test",
                    status=UserStatus.active,
                    full_name="Guard Buyer",
                    public_demo=False,
                ))
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.box["tenant_id"] = tenant_id
        self.box["user"] = CurrentUser(
            user_id=user_id,
            tenant_id=tenant_id,
            role=Role.client,
            audience=AUDIENCE_ORG,
        )

    async def add_invite(self, *, public_demo: bool) -> None:
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()
        token = uuid.uuid4().hex
        email = f"invitee-{uuid.uuid4().hex}@invite.test"
        self.created.append(tenant_id)
        async with self.factory() as session, session.begin():
            async with superadmin_scope(session):
                session.add(Tenant(
                    id=tenant_id,
                    name=f"Invite {tenant_id.hex[:8]}",
                    domain=f"{tenant_id.hex}.invite.test",
                    public_demo=public_demo,
                    is_demo=False,
                ))
                session.add(User(
                    id=user_id,
                    tenant_id=tenant_id,
                    role=Role.recruiter,
                    email=email,
                    status=UserStatus.invited,
                    full_name=None,
                    public_demo=False,
                ))
                await session.flush()
                session.add(StaffInvite(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    email=email,
                    role=Role.recruiter.value,
                    token_hash=hash_invite_token(token),
                    expires_at=invite_expiry(),
                ))
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.email = email
        self.token = token

    async def purchase(self) -> httpx.Response:
        return await self.http.post(PURCHASE_PATH, json={"pack_slug": STARTER_PACK_SLUG})

    async def setup(self, *, password: str, proof: str, name: str = "Asha Rao") -> httpx.Response:
        return await self.http.post(
            f"/api/v1/companies/invites/{self.token}/setup-password",
            json={"password": password, "captcha_proof": proof, "full_name": name},
        )

    async def balance(self) -> int:
        async with self.factory() as session, session.begin():
            async with superadmin_scope(session):
                return await credits.balance_subunits(session, self.tenant_id)

    async def purchase_rows(self) -> list[dict]:
        async with self.factory() as session, session.begin():
            async with superadmin_scope(session):
                rows = (await session.execute(
                    select(CreditPurchase).where(CreditPurchase.tenant_id == self.tenant_id)
                )).scalars().all()
                return [
                    {
                        "status": row.status,
                        "order_id": row.razorpay_order_id,
                        "pack_slug": row.pack_slug,
                    }
                    for row in rows
                ]

    async def user_state(self) -> dict:
        async with self.factory() as session, session.begin():
            async with superadmin_scope(session):
                user = await session.get(User, self.user_id)
                assert user is not None
                return {
                    "public_demo": user.public_demo,
                    "firebase_uid": user.firebase_uid,
                    "auth_providers": list(user.auth_providers or []),
                    "status": user.status,
                }


def _client_headers() -> dict[str, str]:
    marker = uuid.uuid4().hex[:10]
    return {
        "X-Forwarded-For": (
            f"10.{int(marker[:2], 16)}.{int(marker[2:4], 16)}.{int(marker[4:6], 16)}"
        )
    }


async def _wipe(factory, tenant_ids: list) -> None:
    if not tenant_ids:
        return
    async with factory() as session, session.begin():
        async with superadmin_scope(session):
            for tenant_id in tenant_ids:
                await session.execute(
                    text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id}
                )


@pytest.fixture
async def world(monkeypatch):
    # This test's loop cannot borrow the process-wide pool.
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    box: dict = {"tenant_id": None, "user": None}
    created: list = []
    firebase_calls: list = []
    original_create = firebase_auth.create_password_user

    async def _create(address, password, display_name):
        firebase_calls.append((address, password, display_name))
        return f"fbuid-{uuid.uuid4().hex}"

    monkeypatch.setattr(firebase_auth, "create_password_user", _create)
    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith("app.api"):
            continue
        if getattr(module, "create_password_user", None) is original_create:
            monkeypatch.setattr(module, "create_password_user", _create)

    async def _current_user() -> CurrentUser:
        return box["user"]

    async def _tenant_db():
        async with factory() as session, session.begin():
            async with tenant_scope(session, box["tenant_id"]):
                yield session

    async def _identity():
        async with factory() as session:
            async with superadmin_scope(session):
                yield session

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_current_user] = _current_user
    app.dependency_overrides[get_tenant_db] = _tenant_db
    app.dependency_overrides[get_identity_session] = _identity
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=_client_headers(),
        ) as http:
            state = World(factory, http, box, created, firebase_calls)
            try:
                yield state
            finally:
                await _wipe(factory, created)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        await engine.dispose()


async def test_demo_purchase_is_not_found_when_demo_mode_is_off(world, monkeypatch) -> None:
    _demo_settings(monkeypatch, mode=False)
    assert demo_access.enabled() is False
    calls = _forbid_razorpay(monkeypatch)
    await world.add_billing_tenant(public_demo=True, is_demo=False)
    before = await world.balance()

    response = await world.purchase()

    assert response.status_code == 404, response.text
    assert calls == []
    assert await world.purchase_rows() == []
    assert await world.balance() == before


async def test_demo_purchase_conflicts_when_the_tenant_is_not_public_demo(world, monkeypatch) -> None:
    """`is_demo` is the billing exemption. It is not `public_demo`."""
    _demo_settings(monkeypatch, mode=True)
    assert demo_access.enabled() is True
    calls = _forbid_razorpay(monkeypatch)
    await world.add_billing_tenant(public_demo=False, is_demo=True)
    before = await world.balance()

    response = await world.purchase()

    assert response.status_code == 409, response.text
    assert calls == []
    assert await world.purchase_rows() == []
    assert await world.balance() == before


async def test_public_demo_purchase_settles_the_starter_pack_without_razorpay(world, monkeypatch) -> None:
    _demo_settings(monkeypatch, mode=True)
    assert demo_access.enabled() is True
    calls = _forbid_razorpay(monkeypatch)
    await world.add_billing_tenant(public_demo=True, is_demo=False)
    before = await world.balance()

    response = await world.purchase()

    assert response.status_code == 200, response.text
    assert calls == []
    rows = await world.purchase_rows()
    assert len(rows) == 1
    assert rows[0]["status"] == PURCHASE_PAID
    assert rows[0]["pack_slug"] == STARTER_PACK_SLUG
    assert rows[0]["order_id"] is not None
    assert rows[0]["order_id"].startswith("demo_")
    assert await world.balance() - before == PACK_SUBUNITS


async def test_public_demo_invite_accepts_a_dummy_proof_and_a_short_password(world, monkeypatch) -> None:
    _demo_settings(monkeypatch, mode=True)
    assert demo_access.enabled() is True
    await world.add_invite(public_demo=True)

    response = await world.setup(password=SHORT_PASSWORD, proof=DUMMY_PROOF)

    assert response.status_code == 200, response.text
    assert world.firebase_calls == []
    state = await world.user_state()
    assert state["public_demo"] is True
    assert state["firebase_uid"] is None
    assert "demo" in state["auth_providers"]
    assert state["status"] == UserStatus.active


async def test_a_real_invite_accepts_a_dummy_proof_while_demo_mode_is_on(world, monkeypatch) -> None:
    """Demo mode does not skip the password rule on a real company."""
    _demo_settings(monkeypatch, mode=True)
    assert demo_access.enabled() is True
    await world.add_invite(public_demo=False)

    response = await world.setup(password=PASSWORD, proof=DUMMY_PROOF)

    assert response.status_code == 200, response.text
    assert world.firebase_calls == [(world.email, PASSWORD, "Asha Rao")]
    state = await world.user_state()
    assert state["public_demo"] is False
    assert state["status"] == UserStatus.active
    assert state["firebase_uid"] is not None
    assert state["firebase_uid"].startswith("fbuid-")


async def test_demo_mode_off_still_requires_the_password_rule(world, monkeypatch) -> None:
    """A public_demo tenant still walks the password rule while the flag is off."""
    _demo_settings(monkeypatch, mode=False)
    assert demo_access.enabled() is False
    await world.add_invite(public_demo=True)

    weak = await world.setup(password=SHORT_PASSWORD, proof=DUMMY_PROOF)
    assert weak.status_code == 422, weak.text
    assert weak.json()["detail"] == PASSWORD_RULE_DETAIL
    assert world.firebase_calls == []

    ready = await world.setup(password=PASSWORD, proof=DUMMY_PROOF)
    assert ready.status_code == 200, ready.text
    assert world.firebase_calls == [(world.email, PASSWORD, "Asha Rao")]
    state = await world.user_state()
    assert state["status"] == UserStatus.active
    assert state["public_demo"] is False
    assert state["firebase_uid"] is not None
    assert state["firebase_uid"].startswith("fbuid-")
    assert state["auth_providers"] == ["password"]
