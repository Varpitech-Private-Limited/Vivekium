"""Company self-registration, end to end over the REAL routes (owner spec 34.2).

Real Postgres, real Redis, the real session exchange for the sign-in refusal.
A registration body does not need a proof.
Doubled only at the vendor seams: Razorpay's Orders API (`create_order`), its
key configuration (so a signature can be computed the way Razorpay computes
it), and Firebase's Admin API (`create_password_user`, `verify_id_token`).
The security code is read from the dispatched security email, the one place
the plaintext goes. Every assertion about stored state reads from a SECOND
connection after the response.
"""
from __future__ import annotations

import ast
import hashlib
import hmac
import inspect
import uuid

import httpx
import pytest
from redis.exceptions import RedisError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import auth as auth_api
from app.api import company_onboarding as onboarding_api
from app.api.deps import ACCESS_COOKIE, get_public_db
from app.core import cache
from app.core.config import get_settings
from app.core.db import superadmin_scope, tenant_scope
from app.main import app
from app.models.company_registration import CompanyRegistration
from app.models.enums import Role, UserStatus
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.auth import FirebaseSessionIn
from app.services import company_onboarding, firebase_auth, razorpay, security_codes
from app.services.firebase_auth import FirebaseIdentity
from app.workers import dispatch

BASE = "/api/v1/company-onboarding"
KEY_SECRET = "rzp-onboarding-test-secret"
WEBHOOK_SECRET = "rzp-onboarding-webhook-secret"
PASSWORD = "Circa-Corp-2026"


class World:
    def __init__(self, factory, http: httpx.AsyncClient, marker: str) -> None:
        self.factory = factory
        self.http = http
        self.marker = marker
        self.email = f"founder-{marker}@circa{marker}.com"
        self.company = f"Circa Corp {marker}"
        self.created_identities: list[tuple] = []

    def form(self, **overrides) -> dict:
        body = {
            "first_name": "Saravan",
            "last_name": "Kumar",
            "email": self.email.upper(),
            "phone": "98765 43210",
            "company_name": self.company,
            "industry": "Technology",
        }
        body.update(overrides)
        return body

    async def register(self, **overrides) -> httpx.Response:
        return await self.http.post(f"{BASE}/register", json=self.form(**overrides))

    def last_code(self) -> str:
        sent = [
            d for d in dispatch.recorded()
            if d.name == "pickready.send_security_email"
            and d.args[1] == "security_code_registration"
        ]
        assert sent, "no security code email was dispatched"
        return sent[-1].args[2]["code"]

    async def verified(self) -> None:
        assert (await self.register()).status_code == 202
        response = await self.http.post(
            f"{BASE}/code/verify", json={"email": self.email, "code": self.last_code()}
        )
        assert response.status_code == 200, response.text

    async def order(self, pack: str = "standard_50") -> dict:
        response = await self.http.post(f"{BASE}/purchase", json={"pack_slug": pack})
        assert response.status_code == 200, response.text
        return response.json()

    async def rows(self):
        async with self.factory() as session, session.begin():
            async with superadmin_scope(session):
                registration = (await session.execute(
                    select(CompanyRegistration).where(CompanyRegistration.email == self.email)
                    .order_by(CompanyRegistration.created_at.desc())
                )).scalars().first()
                tenant = user = None
                if registration is not None and registration.tenant_id:
                    tenant = await session.get(Tenant, registration.tenant_id)
                if registration is not None and registration.user_id:
                    user = await session.get(User, registration.user_id)
                return registration, tenant, user


def _signature(order_id: str, payment_id: str) -> str:
    return hmac.new(
        KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
    ).hexdigest()


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

    monkeypatch.setattr(
        razorpay, "config",
        lambda: razorpay.RazorpayConfig(
            key_id="rzp_test_onboarding", key_secret=KEY_SECRET, webhook_secret=WEBHOOK_SECRET
        ),
    )

    async def _order(*, amount_paise, receipt, notes=None):
        return {"id": f"order_{uuid.uuid4().hex[:14]}", "amount": amount_paise}

    monkeypatch.setattr(razorpay, "create_order", _order)

    # The routes' OWN session shapes, on this test's engine: the process-wide
    # engine's pool belongs to whichever event loop opened it first, and each
    # test runs on its own loop.
    async def _onboarding_db():
        async with factory() as session:
            async with superadmin_scope(session):
                yield session

    async def _public_db():
        async with factory() as session:
            async with session.begin():
                async with superadmin_scope(session):
                    yield session

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[onboarding_api.get_onboarding_db] = _onboarding_db
    app.dependency_overrides[get_public_db] = _public_db
    marker = uuid.uuid4().hex[:10]
    # A fresh client address per test, so the anonymous rate limits of one
    # test never spend another's allowance.
    headers = {"X-Forwarded-For": f"10.{int(marker[:2], 16)}.{int(marker[2:4], 16)}.{int(marker[4:6], 16)}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
    ) as http:
        state = World(factory, http, marker)

        async def _create(address, password, display_name):
            state.created_identities.append((address, password, display_name))
            return f"fbuid-{uuid.uuid4().hex}"

        monkeypatch.setattr(firebase_auth, "create_password_user", _create)
        try:
            yield state
        finally:
            async with factory() as session, session.begin():
                async with superadmin_scope(session):
                    tenant_ids = (await session.execute(text(
                        "SELECT tenant_id FROM company_registrations WHERE email = :e "
                        "AND tenant_id IS NOT NULL"
                    ), {"e": state.email})).scalars().all()
                    await session.execute(text(
                        "DELETE FROM company_registrations WHERE email = :e"
                    ), {"e": state.email})
                    await session.execute(text(
                        "DELETE FROM users WHERE lower(email) = :e"
                    ), {"e": state.email})
                    for tenant_id in tenant_ids:
                        await session.execute(text(
                            "DELETE FROM tenants WHERE id = :t"
                        ), {"t": tenant_id})
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)
            await engine.dispose()


def _set_cookie_names(response: httpx.Response) -> list[str]:
    return [raw.split("=", 1)[0] for raw in response.headers.get_list("set-cookie")]


# ── The form and the security code ──────────────────────────────────────────

async def test_register_accepts_a_missing_proof(world) -> None:
    response = await world.register()
    assert response.status_code == 202


async def test_register_accepts_a_dummy_proof(world) -> None:
    response = await world.register(captcha_proof="not-a-proof")
    assert response.status_code == 202


async def test_a_right_code_creates_an_onboarding_company_and_an_invited_super_admin(world) -> None:
    response = await world.register()
    assert response.status_code == 202
    assert response.json()["message"] == onboarding_api.ACCEPTED_MESSAGE
    registration, tenant, _user = await world.rows()
    assert registration.status == "email_verification_pending"
    assert registration.email == world.email  # lowercased
    assert registration.phone == "+919876543210"
    assert tenant is None

    verify = await world.http.post(
        f"{BASE}/code/verify", json={"email": world.email, "code": world.last_code()}
    )
    assert verify.status_code == 200
    assert verify.json()["stage"] == "choose_pack"
    assert company_onboarding.ONBOARDING_COOKIE in _set_cookie_names(verify)
    assert ACCESS_COOKIE not in _set_cookie_names(verify)

    registration, tenant, user = await world.rows()
    assert registration.status == "email_verified"
    assert registration.email_verified_at is not None
    assert tenant.status == "onboarding" and tenant.name == world.company
    assert tenant.industry == "Technology"
    assert user.role == Role.client and user.status == UserStatus.invited
    assert user.firebase_uid is None and user.tenant_id == tenant.id
    # The page resumes from the cookie.
    state = await world.http.get(f"{BASE}/state")
    assert state.json() == {
        "stage": "choose_pack", "email": world.email,
        "first_name": "Saravan", "company_name": world.company,
    }


async def test_a_wrong_or_expired_code_creates_nothing(world) -> None:
    assert (await world.register()).status_code == 202
    code = world.last_code()
    wrong = "000000" if code != "000000" else "111111"
    response = await world.http.post(f"{BASE}/code/verify", json={"email": world.email, "code": wrong})
    assert response.status_code == 400
    assert response.json()["detail"] == onboarding_api.CODE_REFUSED_DETAIL

    # Expire the live code in the store, as ten minutes would.
    client = cache._redis()  # noqa: SLF001
    await client.hset(security_codes._key("company_register", world.email), "expires_at", "1")  # noqa: SLF001
    response = await world.http.post(f"{BASE}/code/verify", json={"email": world.email, "code": code})
    assert response.status_code == 400
    registration, tenant, _user = await world.rows()
    assert registration.status == "email_verification_pending" and tenant is None
    assert company_onboarding.ONBOARDING_COOKIE not in _set_cookie_names(response)


async def test_a_resent_code_replaces_the_old_one(world) -> None:
    assert (await world.register()).status_code == 202
    first = world.last_code()
    client = cache._redis()  # noqa: SLF001
    # The resend window is the service's own rule; step past it.
    await client.delete(security_codes._cooldown_key("company_register", world.email))  # noqa: SLF001
    resend = await world.http.post(f"{BASE}/code/resend", json={"email": world.email})
    assert resend.status_code == 202
    second = world.last_code()
    if first != second:
        old = await world.http.post(f"{BASE}/code/verify", json={"email": world.email, "code": first})
        assert old.status_code == 400
    new = await world.http.post(f"{BASE}/code/verify", json={"email": world.email, "code": second})
    assert new.status_code == 200


async def test_an_address_with_an_account_gets_a_notice_and_the_same_answer(world) -> None:
    async with world.factory() as session, session.begin():
        async with superadmin_scope(session):
            session.add(User(role=Role.candidate, email=world.email, status=UserStatus.active))
    response = await world.register()
    assert response.status_code == 202
    assert response.json()["message"] == onboarding_api.ACCEPTED_MESSAGE
    sent = [d for d in dispatch.recorded() if d.name == "pickready.send_security_email"]
    assert [d.args[1] for d in sent] == ["account_exists_notice"]
    assert "code" not in sent[0].args[2]
    registration, _tenant, _user = await world.rows()
    assert registration is None


# ── Payment ─────────────────────────────────────────────────────────────────

async def test_paid_starter_charge_activates_pilot_once(world, monkeypatch) -> None:
    await world.verified()
    plans = await world.http.get(f"{BASE}/monthly/plans")
    assert plans.status_code == 200
    assert [plan["slug"] for plan in plans.json()["plans"]] == ["starter"]
    refused = await world.http.post(f"{BASE}/monthly/subscribe", json={"plan_slug": "growth"})
    assert refused.status_code == 422

    async def create_plan(**_kwargs):
        return f"plan_{uuid.uuid4().hex[:12]}"

    async def create_subscription(**_kwargs):
        return {"id": f"sub_{uuid.uuid4().hex[:12]}"}

    async def fetch_payment(_payment_id):
        return {"status": "captured", "currency": "INR", "amount": 2_832_000}

    monkeypatch.setattr(razorpay, "create_monthly_plan", create_plan)
    monkeypatch.setattr(razorpay, "create_subscription", create_subscription)
    monkeypatch.setattr(razorpay, "fetch_payment", fetch_payment)

    checkout = await world.http.post(f"{BASE}/monthly/subscribe", json={"plan_slug": "starter"})
    assert checkout.status_code == 200, checkout.text
    subscription_id = checkout.json()["subscription_id"]
    payment_id = f"pay_{uuid.uuid4().hex[:12]}"
    signature = hmac.new(
        KEY_SECRET.encode(), f"{payment_id}|{subscription_id}".encode(), hashlib.sha256,
    ).hexdigest()
    proof = {"razorpay_subscription_id": subscription_id,
             "razorpay_payment_id": payment_id, "razorpay_signature": signature}
    confirmed = await world.http.post(f"{BASE}/monthly/verify", json=proof)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["stage"] == "set_password"
    duplicate = await world.http.post(f"{BASE}/monthly/verify", json=proof)
    assert duplicate.status_code == 200
    _, tenant, _ = await world.rows()
    async with world.factory() as session, session.begin():
        async with superadmin_scope(session):
            assert (await session.execute(text(
                "SELECT sum(subunits_delta) FROM credit_ledger WHERE tenant_id = :tid"
            ), {"tid": str(tenant.id)})).scalar_one() == 75 * 60


async def test_activation_before_payment_is_refused(world) -> None:
    await world.verified()
    response = await world.http.post(f"{BASE}/activate", json={"password": PASSWORD})
    assert response.status_code == 402
    assert response.json()["detail"] == onboarding_api.PAYMENT_REQUIRED_DETAIL
    assert world.created_identities == []
    _registration, tenant, user = await world.rows()
    assert tenant.status == "onboarding" and user.status == UserStatus.invited


async def _exchange(world, monkeypatch, uid: str):
    identity = FirebaseIdentity(
        uid=uid, email=world.email, name="Saravan Kumar", provider="password",
        email_verified=True,
    )
    monkeypatch.setattr(firebase_auth, "verify_id_token", lambda _tok: identity)
    body = FirebaseSessionIn(
        id_token="t" * 40,
        captcha_purpose="company_login",
    )
    from fastapi import Response

    response = Response()
    async with world.factory() as session, superadmin_scope(session):
        return await auth_api.firebase_session(body, response, session), response


async def test_login_before_activation_is_refused_and_activates_nobody(world, monkeypatch) -> None:
    await world.verified()
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as refused:
        await _exchange(world, monkeypatch, f"fb-signup-{world.marker}")
    assert refused.value.status_code == 403
    assert refused.value.detail == company_onboarding.ONBOARDING_LOGIN_DETAIL
    _registration, tenant, user = await world.rows()
    assert user.status == UserStatus.invited and user.firebase_uid is None
    assert tenant.status == "onboarding"


async def test_the_provider_never_lists_an_onboarding_company(world) -> None:
    from app.api import provider

    await world.verified()
    async with world.factory() as session, session.begin():
        async with superadmin_scope(session):
            for status_filter in ("active", "all"):
                page = await provider.list_customers(
                    search=world.company, status_filter=status_filter, page=1,
                    page_size=25, session=session,
                )
                assert page.total == 0


async def test_a_tenant_session_reads_no_registration(world) -> None:
    await world.verified()
    _registration, tenant, _user = await world.rows()
    async with world.factory() as session, session.begin():
        async with tenant_scope(session, tenant.id):
            seen = (await session.execute(
                select(func.count()).select_from(CompanyRegistration)
            )).scalar_one()
    assert seen == 0


def _code_tokens(module) -> str:
    """The module's code with every docstring and comment removed."""
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            body[0] = ast.Pass()
    return ast.unparse(tree)


def test_onboarding_uses_its_own_monthly_payment_routes() -> None:
    code = _code_tokens(onboarding_api).lower()
    assert "/billing/" not in code
    paths = {route.path for route in onboarding_api.router.routes}
    assert paths == {
        "/register", "/demo", "/code/resend", "/code/verify", "/state",
        "/monthly/plans", "/monthly/subscribe", "/monthly/verify", "/activate",
    }
    assert "monthly_plans.begin_subscription" in code
    assert "monthly_plans.settle_charge" in code


def test_the_phone_is_stored_canonical() -> None:
    assert company_onboarding.canonical_phone("98765 43210") == "+919876543210"
    assert company_onboarding.canonical_phone("+44 20 7946 0958") == "+442079460958"
    assert company_onboarding.canonical_phone("0 98765-43210") == "+919876543210"
    for bad in ("12345", "abc", "+0123456789"):
        with pytest.raises(ValueError):
            company_onboarding.canonical_phone(bad)


def test_the_onboarding_cookie_is_not_a_portal_session() -> None:
    token = company_onboarding.mint_cookie(uuid.uuid4())
    from app.core.security import AUDIENCE_ORG, decode_token
    import jwt

    with pytest.raises(jwt.PyJWTError):
        decode_token(token, audience=AUDIENCE_ORG)
    assert company_onboarding.read_cookie("not-a-token") is None
