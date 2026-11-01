"""Staff invitations (tenant-scoped, single-use, expiring).

An "invite" is NOT a credential. Firebase owns credentials (claude.md rule 2):
inviting a team member creates their `users` row (status=invited) and mails
them a link. The link only *identifies* the invitation — the invitee still has
to prove ownership of the email via Firebase (Google / email+password /
phone), after which `/auth/firebase/session` links the Firebase uid to the
pre-seeded staff row BY EMAIL and flips `invited -> active`.

Only the SHA-256 of the token is stored, so a database read never yields a
usable invite link (same posture as the OTP code hash, ESD §16).
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, UUIDPKMixin

# ASSUMPTION: the PRD sets no invite TTL. 7 days matches the sprint brief and
# is long enough to survive a weekend without leaving links live indefinitely.
INVITE_TTL_DAYS = 7

# Lifecycle states surfaced to the UI and to /join.
INVITE_PENDING = "pending"
INVITE_ACCEPTED = "accepted"
INVITE_REVOKED = "revoked"
INVITE_EXPIRED = "expired"


# ── Pure helpers (DB-free, unit-testable) ────────────────────────────────────

def generate_invite_token() -> str:
    """A fresh, high-entropy, URL-safe invite token (the raw secret — shown to
    the admin / emailed once, never persisted)."""
    return secrets.token_urlsafe(32)


def hash_invite_token(token: str) -> str:
    """SHA-256 hex of the raw token. Deterministic so lookup is a single
    indexed equality match; the raw token is never stored."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def invite_expiry(now: datetime | None = None, ttl_days: int = INVITE_TTL_DAYS) -> datetime:
    return (now or datetime.now(timezone.utc)) + timedelta(days=ttl_days)


def invite_state(
    *,
    accepted_at: datetime | None,
    revoked_at: datetime | None,
    expires_at: datetime,
    now: datetime | None = None,
) -> str:
    """Resolve an invite's lifecycle state. Precedence is deliberate:
    accepted > revoked > expired > pending — an already-accepted invite is
    reported as accepted even after its expiry timestamp passes."""
    if accepted_at is not None:
        return INVITE_ACCEPTED
    if revoked_at is not None:
        return INVITE_REVOKED
    moment = now or datetime.now(timezone.utc)
    if expires_at.tzinfo is None:  # naive rows from older drivers
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= moment:
        return INVITE_EXPIRED
    return INVITE_PENDING


def build_invite_link(frontend_url: str, token: str) -> str:
    """The acceptance URL handled by frontend `app/(auth)/join/page.tsx`."""
    base = (frontend_url or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        # A relative link in an email is a dead link; fail loudly instead.
        raise ValueError("FRONTEND_URL must be an absolute http(s) URL to build an invite link")
    return f"{base}/join?invite={token}"


class StaffInvite(Base, UUIDPKMixin, CreatedAtMixin):
    """One row per issued invitation. Resending revokes the previous row and
    issues a new one, so at most ONE pending invite exists per staff user."""

    __tablename__ = "staff_invites"
    __table_args__ = (
        Index("ix_staff_invites_user", "tenant_id", "user_id"),
        Index("ix_staff_invites_token_hash", "token_hash", unique=True),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
