"""Audit-service guarantees (PRD §8 / ESD §16).

Two layers:
- DB-free assertions: UUID coercion (tenant_id=None tolerated), the auth
  action constant set, and the promise that `record_auth_event` NEVER raises
  into its caller even when the session blows up.
- Live integration (skips cleanly when no database is reachable): both entry
  points actually persist a row, including the tenant_id=None / actor=None
  case that a past bug crashed on.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models.tenant import AuditLog
from app.services import audit as audit_mod
from app.services.audit import (
    AUTH_ACTIONS,
    AUTH_LOGIN_SUCCEEDED,
    AUTH_OTP_FAILED,
    AUTH_OTP_RATE_LIMITED,
    AUTH_OTP_REQUESTED,
    AUTH_OTP_VERIFIED,
    AUTH_OWNER_INVARIANT_VIOLATION,
    _coerce_uuid,
    audit,
    record_auth_event,
)


# ── DB-free: UUID coercion tolerates None / empty ────────────────────────────

def test_coerce_uuid_none_is_none() -> None:
    assert _coerce_uuid(None) is None


def test_coerce_uuid_empty_string_is_none() -> None:
    # The historical crash: a "" tenant_id must degrade to NULL, not raise.
    assert _coerce_uuid("") is None
    assert _coerce_uuid("   ") is None


def test_coerce_uuid_roundtrips_str_and_uuid() -> None:
    u = uuid.uuid4()
    assert _coerce_uuid(str(u)) == u
    assert _coerce_uuid(u) == u


# ── DB-free: the auth action constant set is complete ────────────────────────

def test_all_auth_actions_registered() -> None:
    expected = {
        "otp_requested",
        "otp_verified",
        "otp_failed",
        "otp_rate_limited",
        "login_succeeded",
        # Recorded when a proven Firebase identity is refused a session. Today
        # the only reason is an attempt to rebind an account already linked to
        # a different uid by an identity whose email is unverified, which is
        # the shape of an account takeover and is worth a row rather than
        # silence.
        "login_refused",
        "context_selected",
        "logout",
        "email_send_failed",
        "owner_invariant_violation",
        # Auth hardening, 2026-09-29 (auth spec section 29): a security code
        # sent for a password change or reset, the password changed, and an
        # invitation accepted by setting a password.
        "password_change_code_sent",
        "password_changed",
        "staff_invite_accepted",
    }
    assert AUTH_ACTIONS == expected


def test_auth_action_constants_have_expected_values() -> None:
    assert AUTH_OTP_REQUESTED == "otp_requested"
    assert AUTH_OTP_VERIFIED == "otp_verified"
    assert AUTH_OTP_FAILED == "otp_failed"
    assert AUTH_OTP_RATE_LIMITED == "otp_rate_limited"
    assert AUTH_LOGIN_SUCCEEDED == "login_succeeded"
    assert AUTH_OWNER_INVARIANT_VIOLATION == "owner_invariant_violation"


# ── DB-free: record_auth_event never raises into the caller ──────────────────

class _ExplodingSession:
    """Stand-in AsyncSession whose transaction machinery blows up — proves
    record_auth_event swallows any failure and returns False."""

    def begin_nested(self):  # noqa: D401 — mimics AsyncSession.begin_nested
        raise RuntimeError("simulated DB failure")

    def add(self, _row) -> None:  # pragma: no cover - never reached
        raise AssertionError("add should not run once begin_nested failed")


async def test_record_auth_event_swallows_session_failure() -> None:
    session = _ExplodingSession()
    # Must NOT raise, and must report the write as failed.
    ok = await record_auth_event(
        session,  # type: ignore[arg-type]
        action=AUTH_LOGIN_SUCCEEDED,
        tenant_id=None,
        actor_user_id=None,
    )
    assert ok is False


async def test_record_auth_event_tolerates_unregistered_action() -> None:
    # An unknown action is logged, not raised — still swallowed on failure.
    ok = await record_auth_event(
        _ExplodingSession(),  # type: ignore[arg-type]
        action="some_brand_new_action",
    )
    assert ok is False


# ── Live integration (skips if the database is unreachable) ──────────────────

async def _db_or_skip():
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect():
            pass
    except Exception:  # noqa: BLE001 — any connect failure means "no DB here"
        await engine.dispose()
        pytest.skip("no database reachable — skipping audit integration test")
    return engine


async def test_audit_persists_row_with_null_tenant_and_actor() -> None:
    """The exact shape a failed Owner/candidate login produces: tenant_id=None
    AND actor_user_id=None. Must persist, not crash."""
    engine = await _db_or_skip()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    marker = f"test-audit-{uuid.uuid4()}"
    try:
        async with factory() as session:
            row = await audit(
                session,
                tenant_id=None,
                actor_user_id=None,
                action=AUTH_OTP_FAILED,
                target_type="otp_challenge",
                target_id=None,
                metadata={"marker": marker},
            )
            assert row.tenant_id is None
            assert row.actor_user_id is None
            await session.commit()

        async with factory() as session:
            found = (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == AUTH_OTP_FAILED)
                )
            ).scalars().all()
            assert any((r.metadata_json or {}).get("marker") == marker for r in found)
    finally:
        await engine.dispose()


async def test_record_auth_event_persists_inside_savepoint() -> None:
    """record_auth_event writes a real row without a tenant scope set, and
    leaves the caller's transaction usable afterwards."""
    engine = await _db_or_skip()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    marker = f"test-authevt-{uuid.uuid4()}"
    try:
        async with factory() as session:
            ok = await record_auth_event(
                session,
                action=AUTH_OTP_RATE_LIMITED,
                tenant_id=None,
                actor_user_id=None,
                metadata={"marker": marker},
            )
            assert ok is True
            # The caller's session must still be usable after the audit write.
            await session.execute(select(AuditLog).limit(1))
            await session.commit()

        async with factory() as session:
            found = (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == AUTH_OTP_RATE_LIMITED)
                )
            ).scalars().all()
            assert any((r.metadata_json or {}).get("marker") == marker for r in found)
    finally:
        await engine.dispose()
