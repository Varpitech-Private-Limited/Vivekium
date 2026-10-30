"""Public demo gate, outbound recipient check, and retention purge.

Session login and company registration live with their routes. This module
only answers whether the demo is on, which addresses may be mailed, and
which expired public-demo rows to delete.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.enums import Role
from app.models.tenant import Tenant
from app.models.user import User

logger = logging.getLogger(__name__)

PORTAL_TO_FILTER = {
    "candidate": "candidate",
    "company": "org",
    "provider": "owner",
    "bd": "bd",
}


class DemoEmailWithheld(Exception):
    """The public demo refused this recipient. The address is not the message."""


def enabled() -> bool:
    settings = get_settings()
    if not settings.demo_mode:
        return False
    environment = (settings.environment or "").strip().lower()
    if environment in {"production", "pilot"}:
        return settings.demo_mode_confirm == "public-demo"
    return True


def require_enabled() -> None:
    if not enabled():
        raise HTTPException(status_code=404, detail="Not found.")


def _platform_addresses() -> set[str]:
    settings = get_settings()
    found = {
        (settings.platform_security_sender_email or "").strip().lower(),
        (settings.smtp_from_email or "").strip().lower(),
    }
    found.discard("")
    return found


def _domain(email: str) -> str:
    normalized = (email or "").strip().lower()
    if "@" not in normalized:
        return ""
    return normalized.rsplit("@", 1)[1]


def _allowlist_matches(normalized: str, domain: str) -> bool:
    raw = get_settings().demo_email_allowlist or ""
    entries = [part.strip().lower() for part in raw.split(",") if part.strip()]
    for entry in entries:
        if "@" in entry:
            if normalized == entry:
                return True
            continue
        if domain == entry or domain.endswith("." + entry):
            return True
    return False


def recipient_allowed(email: str) -> bool:
    """Demo off: every recipient. Demo on: platform senders and the allowlist.

    An allowlist entry with @ is one exact address. Any other entry is a
    domain suffix, so example.com matches a@example.com and a@sub.example.com.
    An empty allowlist leaves only the platform addresses.
    """
    if not enabled():
        return True
    normalized = (email or "").strip().lower()
    if normalized in _platform_addresses():
        return True
    if "@" not in normalized:
        return False
    return _allowlist_matches(normalized, _domain(email))


def ensure_withheld(email: str) -> None:
    if recipient_allowed(email):
        return
    logger.info("demo.email_withheld domain=%s", _domain(email))
    raise DemoEmailWithheld()


async def _delete_rows(session: AsyncSession, rows: list, *, kind: str) -> tuple[int, int]:
    deleted = 0
    failed = 0
    for row in rows:
        if row.public_demo is not True:
            continue
        try:
            async with session.begin_nested():
                if row.public_demo is not True:
                    continue
                await session.delete(row)
        except SQLAlchemyError as exc:
            logger.warning(
                "demo.purge_failed kind=%s id=%s error=%s",
                kind,
                row.id,
                type(exc).__name__,
            )
            failed += 1
        else:
            deleted += 1
    return deleted, failed


async def purge_expired(session: AsyncSession) -> dict:
    """Delete expired public-demo tenants and tenantless candidate users.

    A savepoint around each delete keeps one failure from undoing the rest.
    A row with public_demo false is never deleted. The caller commits.
    """
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.demo_retention_days)
    tenants = list(
        (
            await session.execute(
                select(Tenant).where(
                    Tenant.public_demo.is_(True),
                    Tenant.created_at < cutoff,
                )
            )
        ).scalars().all()
    )
    tenants_deleted, tenants_failed = await _delete_rows(session, tenants, kind="tenant")
    candidates = list(
        (
            await session.execute(
                select(User).where(
                    User.public_demo.is_(True),
                    User.tenant_id.is_(None),
                    User.role == Role.candidate,
                    User.created_at < cutoff,
                )
            )
        ).scalars().all()
    )
    candidates_deleted, candidates_failed = await _delete_rows(
        session, candidates, kind="candidate"
    )
    return {
        "tenants_deleted": tenants_deleted,
        "tenants_failed": tenants_failed,
        "candidates_deleted": candidates_deleted,
        "candidates_failed": candidates_failed,
    }
