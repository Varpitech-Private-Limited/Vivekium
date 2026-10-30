"""The server-drawn CAPTCHA is gone (A-24, 2026-10-30).

`captcha_purpose` stays. It is the portal filter on the session exchange.
`captcha_proof` is accepted and not read. Rate limits on the session and
password routes stay. Nothing in live code draws a challenge or spends a proof.
"""
from __future__ import annotations

import importlib
import re
import uuid

import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import auth
from app.core.config import get_settings
from app.main import app
from app.models.enums import Role, UserStatus
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.auth import FirebaseSessionIn
from app.services import firebase_auth
from app.services.firebase_auth import FirebaseIdentity
from tests.removal_sweep import BACKEND, REPO, sweep

THIS_FILE = BACKEND / "tests" / "test_captcha_removed.py"

GONE = re.compile(
    r"consume_proof|/captcha/challenge|/captcha/verify|services\.captcha|services/captcha"
    r"|components/captcha|captcha_support|CaptchaRequired|create_challenge"
    r"|AUTH_CAPTCHA|captcha_challenge_failed"
)

_ABSENT = (
    BACKEND / "app" / "services" / "captcha.py",
    BACKEND / "tests" / "captcha_support.py",
    REPO / "frontend" / "components" / "captcha.tsx",
    REPO / "frontend" / "components" / "captcha.test.tsx",
)


def test_the_sweep_pattern_matches_a_sample() -> None:
    assert GONE.search("consume_proof")


def test_the_service_and_the_widget_are_gone() -> None:
    missing = [str(path) for path in _ABSENT if path.exists()]
    assert not missing, missing
    with pytest.raises((ModuleNotFoundError, ImportError)):
        importlib.import_module("app.services.captcha")


def test_nothing_names_the_retired_check() -> None:
    hits = sweep(GONE, exempt=(THIS_FILE,))
    assert not hits, hits


def test_the_challenge_routes_are_not_mounted() -> None:
    paths = set(app.openapi()["paths"])
    assert "/api/v1/auth/captcha/challenge" not in paths
    assert "/api/v1/auth/captcha/verify" not in paths
    for path in (
        "/api/v1/auth/firebase/session",
        "/api/v1/auth/password-reset/request",
        "/api/v1/auth/password-change/request",
    ):
        assert path in paths, path


def _limited(path: str) -> bool:
    for route in auth.router.routes:
        if getattr(route, "path", "") != path:
            continue
        for dependency in getattr(route, "dependencies", []):
            call = getattr(dependency, "dependency", None)
            if "rate_limit" in getattr(call, "__qualname__", ""):
                return True
    return False


def test_session_and_password_routes_stay_rate_limited() -> None:
    for path in (
        "/firebase/session",
        "/password-reset/request",
        "/password-reset/verify",
        "/password-reset/complete",
        "/password-change/request",
        "/password-change/verify",
        "/password-change/complete",
    ):
        assert _limited(path), path


def test_a_missing_or_short_proof_is_not_a_validation_error() -> None:
    bare = FirebaseSessionIn(id_token="t" * 40, captcha_purpose="candidate_login")
    assert bare.captcha_proof is None
    short = FirebaseSessionIn(
        id_token="t" * 40, captcha_proof="x", captcha_purpose="candidate_login"
    )
    assert short.captcha_proof == "x"
    with pytest.raises(ValidationError):
        FirebaseSessionIn(id_token="t" * 40)
    with pytest.raises(ValidationError):
        FirebaseSessionIn(id_token="t" * 40, captcha_purpose="password_reset")
    with pytest.raises(ValidationError):
        FirebaseSessionIn(
            id_token="t" * 40, captcha_purpose="candidate_login", portal="org"
        )


async def test_the_exchange_reads_the_token_without_a_proof(monkeypatch) -> None:
    asked: list[str] = []

    def _reject(token: str):
        asked.append(token)
        raise HTTPException(status_code=401, detail="Invalid Firebase session")

    monkeypatch.setattr(firebase_auth, "verify_id_token", _reject)
    body = FirebaseSessionIn(id_token="t" * 40, captcha_purpose="candidate_login")
    with pytest.raises(HTTPException) as exc:
        await auth.firebase_session(body, Response(), session=None)
    assert asked == ["t" * 40]
    assert exc.value.status_code == 401


async def _factory_or_skip():
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.connect():
            pass
    except OSError:
        await engine.dispose()
        pytest.skip("no database reachable at DATABASE_URL")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def test_a_candidate_surface_cannot_open_an_org_workspace(monkeypatch) -> None:
    """The purpose is the portal filter. No proof is minted."""
    engine, factory = await _factory_or_skip()
    tenant_id = uuid.uuid4()
    email = f"surface-{uuid.uuid4().hex}@portal.test"
    try:
        async with factory() as session:
            session.add(Tenant(
                id=tenant_id, name=f"Surface-{tenant_id}", domain=f"{tenant_id}.t"
            ))
            session.add(User(
                role=Role.recruiter, email=email, tenant_id=tenant_id,
                status=UserStatus.active,
            ))
            await session.commit()
        identity = FirebaseIdentity(
            uid=f"fbuid-{uuid.uuid4().hex}", email=email, name="R",
            provider="password", email_verified=True,
        )
        monkeypatch.setattr(firebase_auth, "verify_id_token", lambda _t: identity)
        async with factory() as session:
            with pytest.raises(HTTPException) as exc:
                await auth.firebase_session(
                    FirebaseSessionIn(
                        id_token="t" * 40, captcha_purpose="candidate_login"
                    ),
                    Response(), session,
                )
        assert exc.value.status_code == 403
        assert re.match(r"No candidate workspace is linked", exc.value.detail)
        async with factory() as session:
            out = await auth.firebase_session(
                FirebaseSessionIn(
                    id_token="t" * 40, captcha_purpose="company_login"
                ),
                Response(), session,
            )
        assert out.user is not None and out.user.role == Role.recruiter
    finally:
        async with factory() as session:
            await session.execute(User.__table__.delete().where(User.tenant_id == tenant_id))
            await session.execute(Tenant.__table__.delete().where(Tenant.id == tenant_id))
            await session.commit()
        await engine.dispose()
