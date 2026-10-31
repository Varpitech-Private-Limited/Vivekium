"""Public demo sign-in and company registration.

The purchase, invite, and mail guards live in test_public_demo_guards.py.
This module covers the session and registration routes those guards do not
call. Ledger rows are read on a second connection after the response commits.
"""
from __future__ import annotations

import sys
import uuid

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.company_onboarding import get_onboarding_db
from app.core.config import get_settings
from app.core.db import get_identity_session, superadmin_scope
from app.main import app
from app.models.billing import SUBUNITS_PER_CREDIT, BillingTransaction
from app.models.company_registration import CompanyRegistration
from app.models.enums import Role, UserStatus
from app.models.tenant import Tenant
from app.models.user import User
from app.services import credits, demo_access
from app.services.login_context import eligible_login_users

SESSION_PATH = "/api/v1/auth/demo/session"
STATUS_PATH = "/api/v1/auth/demo-status"
COMPANY_PATH = "/api/v1/company-onboarding/demo"
REFUSED = "This email address cannot be used for the demo. Use a different email."
OWNER_REFUSED = "This email address cannot be used for the demo."
COMPANY_MISSING = "No company workspace exists for this email. Register a demo company first."


def _demo_settings(monkeypatch, *, mode: bool):
    current = demo_access.get_settings()
    settings = current.model_copy(update={
        "demo_mode": mode,
        "demo_mode_confirm": "public-demo" if mode else "",
    })
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


def _headers() -> dict[str, str]:
    marker = uuid.uuid4().hex[:6]
    return {"X-Forwarded-For": f"10.{int(marker[:2], 16)}.{int(marker[2:4], 16)}.8"}


def test_production_and_pilot_require_the_confirm_phrase() -> None:
    from app.core.config import Settings

    with pytest.raises(ValidationError, match="DEMO_MODE_CONFIRM"):
        Settings(
            environment="production",
            demo_mode=True,
            demo_mode_confirm="",
            require_jwt_secret=False,
        )
    with pytest.raises(ValidationError, match="DEMO_MODE_CONFIRM"):
        Settings(
            environment="pilot",
            demo_mode=True,
            demo_mode_confirm="yes",
            require_jwt_secret=False,
        )
    allowed = Settings(
        environment="development",
        demo_mode=True,
        demo_mode_confirm="",
        require_jwt_secret=False,
    )
    assert allowed.demo_mode is True


def test_enabled_stays_false_on_production_without_the_phrase(monkeypatch) -> None:
    monkeypatch.setattr(
        demo_access,
        "get_settings",
        lambda: type("S", (), {
            "demo_mode": True,
            "environment": "production",
            "demo_mode_confirm": "",
        })(),
    )
    assert demo_access.enabled() is False


class World:
    def __init__(self, factory, http: httpx.AsyncClient, created: list[uuid.UUID]) -> None:
        self.factory = factory
        self.http = http
        self.created = created

    async def session(self, *, email: str, portal: str, full_name: str | None = None, password: str = "x"):
        body = {"email": email, "password": password, "portal": portal}
        if full_name is not None:
            body["full_name"] = full_name
        return await self.http.post(SESSION_PATH, json=body)

    async def register(self, *, email: str, company_name: str, phone: str = "9876543210"):
        return await self.http.post(COMPANY_PATH, json={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": email,
            "phone": phone,
            "company_name": company_name,
            "industry": "Technology",
            "password": "x",
        })

    async def add_user(self, *, email: str, role: Role, public_demo: bool, status: UserStatus = UserStatus.active, tenant: bool = False) -> uuid.UUID:
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4() if tenant else None
        async with self.factory() as session, session.begin():
            async with superadmin_scope(session):
                if tenant_id is not None:
                    self.created.append(tenant_id)
                    session.add(Tenant(
                        id=tenant_id,
                        name=f"Real {tenant_id.hex[:8]}",
                        domain=f"{tenant_id.hex}.real.test",
                        public_demo=False,
                        is_demo=False,
                    ))
                session.add(User(
                    id=user_id,
                    tenant_id=tenant_id,
                    role=role,
                    email=email,
                    status=status,
                    full_name="Existing Person",
                    public_demo=public_demo,
                    firebase_uid=None,
                    auth_providers=["password"] if not public_demo else ["demo"],
                ))
        return user_id

    async def user_by_email(self, email: str) -> list[dict]:
        async with self.factory() as session, session.begin():
            async with superadmin_scope(session):
                rows = (await session.execute(
                    select(User).where(func.lower(User.email) == email.lower()).order_by(User.created_at, User.id)
                )).scalars().all()
                return [
                    {
                        "id": row.id,
                        "role": row.role,
                        "public_demo": row.public_demo,
                        "firebase_uid": row.firebase_uid,
                        "auth_providers": list(row.auth_providers or []),
                        "status": row.status,
                        "email_verified_at": row.email_verified_at,
                        "tenant_id": row.tenant_id,
                    }
                    for row in rows
                ]

    async def company_facts(self, email: str) -> dict:
        async with self.factory() as session, session.begin():
            async with superadmin_scope(session):
                user = (await session.execute(
                    select(User).where(func.lower(User.email) == email.lower(), User.role == Role.client)
                )).scalars().one()
                tenant = await session.get(Tenant, user.tenant_id)
                assert tenant is not None
                self.created.append(tenant.id)
                charge = (await session.execute(
                    select(BillingTransaction).where(BillingTransaction.tenant_id == tenant.id)
                )).scalars().one()
                registration = (await session.execute(
                    select(CompanyRegistration).where(CompanyRegistration.email == email.lower())
                )).scalars().one()
                balance = await credits.balance_subunits(session, tenant.id)
                return {
                    "public_demo": tenant.public_demo,
                    "is_demo": tenant.is_demo,
                    "status": tenant.status,
                    "plan": tenant.current_plan_slug,
                    "subscription_status": tenant.subscription_status,
                    "subscription_id": tenant.razorpay_subscription_id,
                    "amount_inr": charge.amount_inr,
                    "charge_status": charge.status,
                    "transaction_type": charge.transaction_type,
                    "granted": charge.assessments_granted,
                    "payment_id": charge.razorpay_payment_id,
                    "balance": balance,
                    "registration_status": registration.status,
                    "registration_verified": registration.email_verified_at is not None,
                    "user_verified": user.email_verified_at is not None,
                    "firebase_uid": user.firebase_uid,
                    "auth_providers": list(user.auth_providers or []),
                    "user_public_demo": user.public_demo,
                    "user_id": user.id,
                }


async def _wipe(factory, tenant_ids: list[uuid.UUID], emails: list[str]) -> None:
    async with factory() as session, session.begin():
        async with superadmin_scope(session):
            for email in emails:
                await session.execute(
                    text("DELETE FROM users WHERE lower(email) = :email"),
                    {"email": email.lower()},
                )
            for tenant_id in tenant_ids:
                await session.execute(
                    text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id}
                )


@pytest.fixture
async def world():
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    created: list[uuid.UUID] = []
    emails: list[str] = []

    async def _identity():
        async with factory() as session:
            async with superadmin_scope(session):
                yield session

    async def _onboarding():
        async with factory() as session:
            async with superadmin_scope(session):
                yield session

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_identity_session] = _identity
    app.dependency_overrides[get_onboarding_db] = _onboarding
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=_headers(),
        ) as http:
            state = World(factory, http, created)
            state.emails = emails  # type: ignore[attr-defined]
            try:
                yield state
            finally:
                await _wipe(factory, created, emails)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        await engine.dispose()


def _email(world: World, label: str) -> str:
    email = f"{label}-{uuid.uuid4().hex[:10]}@example.com"
    world.emails.append(email)  # type: ignore[attr-defined]
    return email


async def test_demo_status_and_session_are_closed_when_the_flag_is_off(world, monkeypatch) -> None:
    _demo_settings(monkeypatch, mode=False)
    status = await world.http.get(STATUS_PATH)
    assert status.status_code == 200
    assert status.json() == {"demo_mode": False}
    email = _email(world, "off")
    response = await world.session(email=email, portal="candidate", full_name="Off Person")
    assert response.status_code == 404, response.text
    assert await world.user_by_email(email) == []


async def test_candidate_demo_session_creates_once_and_retrieves(world, monkeypatch) -> None:
    _demo_settings(monkeypatch, mode=True)
    status = await world.http.get(STATUS_PATH)
    assert status.json()["demo_mode"] is True
    email = _email(world, "cand")
    created = await world.session(email=email, portal="candidate", full_name="Ada Lovelace", password=" any ")
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["contexts"] is None
    assert body["user"]["role"] == "candidate"
    assert body["user"]["email"] == email
    assert body["user"]["tenant_id"] is None
    assert isinstance(body["capabilities"], list)
    rows = await world.user_by_email(email)
    assert len(rows) == 1
    assert rows[0]["public_demo"] is True
    assert rows[0]["firebase_uid"] is None
    assert rows[0]["auth_providers"] == ["demo"]
    assert rows[0]["email_verified_at"] is None
    again = await world.session(email=email, portal="candidate", password="other")
    assert again.status_code == 200, again.text
    assert again.json()["user"]["id"] == body["user"]["id"]
    assert len(await world.user_by_email(email)) == 1
    blank = await world.session(email=email, portal="candidate", password="   ")
    assert blank.status_code == 422


async def test_bd_demo_session_creates_once_retrieves_and_has_no_owner_reach(world, monkeypatch) -> None:
    _demo_settings(monkeypatch, mode=True)
    email = _email(world, "bduser")
    created = await world.session(email=email, portal="bd", password="first")
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["user"]["role"] == "bd"
    assert body["user"]["tenant_id"] is None
    rows = await world.user_by_email(email)
    assert [row["role"] for row in rows] == [Role.bd]
    assert rows[0]["public_demo"] is True and rows[0]["firebase_uid"] is None
    # The session cookie is a BD session: the owner-only console stays shut.
    owner_only = await world.http.get("/api/v1/admin/bd-users")
    assert owner_only.status_code == 403
    again = await world.session(email=email, portal="bd", password="second")
    assert again.status_code == 200, again.text
    assert again.json()["user"]["id"] == body["user"]["id"]
    assert len(await world.user_by_email(email)) == 1


async def test_one_email_is_one_user_per_portal_even_in_parallel(world, monkeypatch) -> None:
    import asyncio

    _demo_settings(monkeypatch, mode=True)
    email = _email(world, "parallel")
    results = await asyncio.gather(*[
        world.session(email=email, portal="bd", password=f"p{n}") for n in range(5)
    ])
    assert [r.status_code for r in results] == [200] * 5, [r.text for r in results]
    assert len({r.json()["user"]["id"] for r in results}) == 1
    candidate = await world.session(email=email, portal="candidate", password="other")
    assert candidate.status_code == 200, candidate.text
    roles = sorted(row["role"].value for row in await world.user_by_email(email))
    assert roles == ["bd", "candidate"]


async def test_demo_session_refuses_owner_real_accounts_and_other_portals(world, monkeypatch) -> None:
    _demo_settings(monkeypatch, mode=True)
    owner = get_settings().owner_email
    refused = await world.session(email=owner, portal="candidate")
    assert refused.status_code == 403
    assert refused.json()["detail"] == OWNER_REFUSED
    company = await world.session(email=owner, portal="company")
    assert company.status_code == 403
    provider = await world.session(email=_email(world, "prov"), portal="provider")
    assert provider.status_code == 403
    assert "workspace is linked" in provider.json()["detail"]
    real = _email(world, "real")
    await world.add_user(email=real, role=Role.client, public_demo=False, tenant=True)
    clash = await world.session(email=real, portal="candidate")
    assert clash.status_code == 409
    assert clash.json()["detail"] == REFUSED
    assert len(await world.user_by_email(real)) == 1

    missing = _email(world, "nocompany")
    no_company = await world.session(email=missing, portal="company")
    assert no_company.status_code == 403
    assert no_company.json()["detail"] == COMPANY_MISSING
    assert await world.user_by_email(missing) == []

    other = _email(world, "orgonly")
    await world.session(email=other, portal="candidate", full_name="Only Candidate")
    blocked = await world.session(email=other, portal="company")
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == COMPANY_MISSING
    assert [row["role"] for row in await world.user_by_email(other)] == [Role.candidate]

    disabled = _email(world, "disabled")
    await world.add_user(
        email=disabled, role=Role.candidate, public_demo=True, status=UserStatus.disabled
    )
    unavailable = await world.session(email=disabled, portal="candidate")
    assert unavailable.status_code == 403
    assert unavailable.json()["detail"] == "Account unavailable"
    assert len(await world.user_by_email(disabled)) == 1


def test_public_demo_users_are_not_firebase_eligible() -> None:
    user = User(
        role=Role.candidate,
        email="skip@demo.test",
        status=UserStatus.active,
        public_demo=True,
    )
    assert eligible_login_users([user], owner_email="owner@example.com") == []


async def test_company_registration_is_closed_when_the_flag_is_off(world, monkeypatch) -> None:
    _demo_settings(monkeypatch, mode=False)
    email = _email(world, "closed")
    response = await world.register(email=email, company_name=f"Closed {uuid.uuid4().hex[:6]}")
    assert response.status_code == 404, response.text
    assert await world.user_by_email(email) == []


async def test_company_registration_simulates_payment_and_retrieves(world, monkeypatch) -> None:
    _demo_settings(monkeypatch, mode=True)
    email = _email(world, "co")
    name = f"Demo Co {uuid.uuid4().hex[:8]}"
    created = await world.register(email=email, company_name=name)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["user"]["role"] == "client"
    assert body["user"]["email"] == email
    assert "manage_billing" in body["capabilities"]
    facts = await world.company_facts(email)
    settings = get_settings()
    assert facts["public_demo"] is True
    assert facts["is_demo"] is False
    assert facts["status"] == "active"
    assert facts["plan"] == "starter"
    assert facts["subscription_status"] == "active"
    assert facts["subscription_id"].startswith("demo-sub-")
    assert facts["amount_inr"] == 0
    assert facts["charge_status"] == "success"
    assert facts["transaction_type"] == "subscription_charge"
    assert facts["granted"] == settings.demo_initial_credits
    assert facts["payment_id"].startswith("demo-reg-")
    assert facts["balance"] == settings.demo_initial_credits * SUBUNITS_PER_CREDIT
    assert facts["registration_status"] == "activated"
    assert facts["registration_verified"] is True
    assert facts["user_verified"] is False
    assert facts["firebase_uid"] is None
    assert facts["auth_providers"] == ["demo"]
    assert facts["user_public_demo"] is True
    assert str(facts["user_id"]) == body["user"]["id"]

    again = await world.register(email=email, company_name=name)
    assert again.status_code == 200, again.text
    assert again.json()["user"]["id"] == body["user"]["id"]
    signed = await world.session(email=email, portal="company", password="different")
    assert signed.status_code == 200, signed.text
    assert signed.json()["user"]["id"] == body["user"]["id"]

    taken = await world.register(
        email=_email(world, "taken"),
        company_name=name,
        phone="9876543211",
    )
    assert taken.status_code == 409, taken.text

    owner = await world.register(
        email=get_settings().owner_email,
        company_name=f"Owner Co {uuid.uuid4().hex[:6]}",
        phone="9876543212",
    )
    assert owner.status_code == 409
    assert owner.json()["detail"] == REFUSED

    real = _email(world, "realco")
    await world.add_user(email=real, role=Role.candidate, public_demo=False)
    clash = await world.register(
        email=real,
        company_name=f"Clash Co {uuid.uuid4().hex[:6]}",
        phone="9876543213",
    )
    assert clash.status_code == 409
    assert clash.json()["detail"] == REFUSED


async def test_demo_bd_user_sees_only_own_leads_and_cannot_use_ai_reach(world, monkeypatch) -> None:
    from app.models.bd import BDLead

    _demo_settings(monkeypatch, mode=True)
    real_id = uuid.uuid4()
    async with world.factory() as session, session.begin():
        async with superadmin_scope(session):
            session.add(BDLead(
                id=real_id, channel="personal", company_name="Real Pipeline Co",
                owner_user_id=None,
            ))
    try:
        first = _email(world, "bda")
        assert (await world.session(email=first, portal="bd")).status_code == 200
        created = await world.http.post(
            "/api/v1/bd/leads", json={"channel": "personal", "company_name": "Mine Ltd"}
        )
        assert created.status_code == 201, created.text
        mine = created.json()["id"]
        listed = await world.http.get("/api/v1/bd/leads")
        assert [lead["company_name"] for lead in listed.json()["leads"]] == ["Mine Ltd"]
        assert (await world.http.patch(
            f"/api/v1/bd/leads/{real_id}", json={"notes": "x"}
        )).status_code == 404
        reach = await world.http.post(
            "/api/v1/bd/ai-reach/search",
            json={"job_role": "Engineer", "city": "Pune", "industry": "Software"},
        )
        assert reach.status_code == 403, reach.text

        signed = await world.http.patch(
            f"/api/v1/bd/leads/{mine}/agreement", json={"agreement": True}
        )
        assert signed.status_code == 403, signed.text
        moved = await world.http.patch("/api/v1/bd/me", json={"email": _email(world, "moved")})
        assert moved.status_code == 403, moved.text
        second = _email(world, "bdb")
        assert (await world.session(email=second, portal="bd")).status_code == 200
        other = await world.http.get("/api/v1/bd/leads")
        assert other.json()["total"] == 0
        assert (await world.http.patch(
            f"/api/v1/bd/leads/{mine}", json={"notes": "x"}
        )).status_code == 404
        # The first user, signing in again with another password, still has the lead.
        assert (await world.session(email=first, portal="bd", password="new")).status_code == 200
        again = await world.http.get("/api/v1/bd/leads")
        assert [lead["company_name"] for lead in again.json()["leads"]] == ["Mine Ltd"]
    finally:
        async with world.factory() as session, session.begin():
            async with superadmin_scope(session):
                await session.execute(
                    text("DELETE FROM bd_leads WHERE id = :i OR company_name = 'Mine Ltd'"),
                    {"i": real_id},
                )
