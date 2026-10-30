"""Which workspace a proven identity may enter, and the chooser that picks one.

WHAT THIS IS
------------
Firebase proves WHO somebody is (claude.md rule 2); this module answers WHERE
they may go. One identifier (an email) can belong to
several `users` rows across roles and tenants: three portals, one login. So a
proven identity resolves to either exactly one eligible user (sign them in) or
several (show a workspace chooser, carrying a short-lived single-use
`context_token`, finalised by `POST /auth/select-context`).

WHERE IT CAME FROM
------------------
Extracted on 2026-09-24 from `services/otp.py`, which mixed this LIVE logic
with the retired login-OTP flow (challenges, codes, SMS fan-out) that no route
had reached since Firebase took over identity on 2026-07-24. The functions were
moved as they were and then changed in exactly two ways, both deliberate:

* THE DUAL-OTP PENDING GATE IS GONE from `select_context`. It asked whether a
  `client` user had verified BOTH an email and a phone (FR-1.2, superseded by
  Firebase identity) and, if not, answered a pending-channel list with no cookies.
  No screen has ever handled that list, so a multi-workspace client
  with an unverified phone was stranded at the chooser. A selection now
  activates an invited user exactly as `api/auth._finalize_single` does.
* THE SINGLE-USE FLAG FAILS CLOSED. It lived in a limiter that silently fell
  back to per-process memory on any Redis error, which made a context token
  replayable across API tasks during a Redis blip without anybody knowing. It
  is now one atomic `SET NX EX` through `core/cache._redis()`, and a Redis that
  cannot answer raises `ContextStoreUnavailable` (the route answers 503), the
  posture `services/auth_sessions` already takes for the session record.
"""
from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Sequence

import jwt as pyjwt
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import cache
from app.core.config import get_settings
from app.core.security import ALGORITHM
from app.models.enums import Role, UserStatus
from app.models.tenant import Tenant
from app.models.user import User
from app.services import staff_invites

#: A context token is a short-lived proof that an identity was verified, listing
#: the workspaces it may be exchanged for. Five minutes: long enough to read a
#: chooser, short enough that a leaked token is worth little.
CONTEXT_TOKEN_TTL_MINUTES = 5
AUDIENCE_CONTEXT = "pickready:context"

#: Portal identifiers returned in the context list (contract rev 2).
PORTAL_OWNER = "owner"
PORTAL_ORG = "org"
PORTAL_CANDIDATE = "candidate"


# ── Typed errors ─────────────────────────────────────────────────────────────


class LoginContextError(Exception):
    """Base for workspace-resolution failures."""


class UserNotFound(LoginContextError):
    """No eligible account for this identity or selection."""


class ContextTokenInvalid(LoginContextError):
    """Malformed, expired, or wrong-type context token."""


class ContextTokenConsumed(LoginContextError):
    """Context token already exchanged for a session (single-use)."""


class ContextUserMismatch(LoginContextError):
    """The selected user_id is not one of the token's login contexts."""


class ContextStoreUnavailable(LoginContextError):
    """The single-use ledger could not answer, so the token is NOT exchanged.

    Failing open here would make a context token replayable, so the route
    answers 503 and the person tries again when Redis is back.
    """


# ── Pure resolution core (DB-free, unit-testable) ────────────────────────────


def portal_for_role(role: Role | str) -> str:
    """Which portal a login context lands in: owner = super_admin role,
    candidate = candidate role, everything else is the shared org portal."""
    role = Role(role)
    if role == Role.super_admin:
        return PORTAL_OWNER
    if role == Role.candidate:
        return PORTAL_CANDIDATE
    return PORTAL_ORG


def eligible_login_users(users: Sequence[User], *, owner_email: str) -> list[User]:
    """Pure filter for login eligibility:
    - disabled users never log in;
    - a public demo row never logs in through Firebase or select-context;
    - a super_admin row whose email is not the platform owner's is treated as
      nonexistent (owner invariant, defense in depth; the migration deletes
      such rows).
    Invited users ARE eligible: a first proven sign-in is how they activate."""
    out: list[User] = []
    for u in users:
        if u.status == UserStatus.disabled:
            continue
        if u.public_demo:
            continue
        if u.role == Role.super_admin and (
            (u.email or "").strip().lower() != (owner_email or "").strip().lower()
        ):
            continue
        out.append(u)
    return out


@dataclass
class LoginResolution:
    """Exactly one user -> issue a session; several -> a workspace chooser."""

    user: User | None = None
    contexts: list[User] = field(default_factory=list)

    @property
    def is_multi(self) -> bool:
        return len(self.contexts) > 1


def resolve_login(users: Sequence[User], *, owner_email: str) -> LoginResolution:
    """Pure mapper: matched users -> single-session vs multi-context shape.
    Raises UserNotFound when nobody eligible remains."""
    eligible = eligible_login_users(users, owner_email=owner_email)
    if not eligible:
        raise UserNotFound("no account for this identifier")
    if len(eligible) == 1:
        return LoginResolution(user=eligible[0])
    return LoginResolution(contexts=eligible)


# ── Context tokens (signed, single-use) ──────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_context_token(
    identifier: str,
    user_ids: Sequence[uuid.UUID | str],
    *,
    now: datetime | None = None,
    source_user_id: uuid.UUID | str | None = None,
) -> str:
    """Short-TTL JWT proving a verified identity for `identifier`, listing the
    user contexts it may be exchanged for. Single-use via the jti ledger."""
    settings = get_settings()
    now = now or _utcnow()
    payload = {
        "type": "context",
        "aud": AUDIENCE_CONTEXT,
        "identifier": identifier,
        "user_ids": [str(u) for u in user_ids],
        "jti": secrets.token_urlsafe(16),
        "iat": now,
        "exp": now + timedelta(minutes=CONTEXT_TOKEN_TTL_MINUTES),
    }
    if source_user_id is not None:
        # Present only for an in-session workspace switch. It gives the audit
        # trail an unambiguous previous context without granting authority.
        payload["source_user_id"] = str(source_user_id)
    return pyjwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_context_token(token: str) -> dict:
    """Raises ContextTokenInvalid on any signature/expiry/shape problem."""
    try:
        payload = pyjwt.decode(
            token, get_settings().jwt_secret, algorithms=[ALGORITHM],
            audience=AUDIENCE_CONTEXT,
        )
    except pyjwt.PyJWTError as exc:
        raise ContextTokenInvalid("invalid or expired context token") from exc
    if payload.get("type") != "context" or not payload.get("jti"):
        raise ContextTokenInvalid("invalid or expired context token")
    return payload


def _consumed_key(jti: str) -> str:
    # The key spelling is inherited unchanged from the retired module, so a
    # token consumed by the previous release during a rolling deploy is still
    # consumed for this one. It is not a cache key and carries no tenant data.
    return f"otp:ctx:{jti}"


async def _consume_once(jti: str) -> None:
    """Mark a context token used, atomically. Raises if it already was.

    One `SET NX EX` rather than a read followed by a write: two concurrent
    selections of one token cannot both see "unused". Fails CLOSED.
    """
    client = cache._redis()  # noqa: SLF001 - the shared, loop-aware client
    if client is None:
        raise ContextStoreUnavailable("the session store is unavailable")
    try:
        won = await client.set(
            _consumed_key(jti), "1", nx=True, ex=CONTEXT_TOKEN_TTL_MINUTES * 60
        )
    except (RedisError, OSError) as exc:
        raise ContextStoreUnavailable("the session store is unavailable") from exc
    if not won:
        raise ContextTokenConsumed("context token already used")


# ── DB-backed resolution ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class LoginContext:
    """One selectable workspace in the chooser."""

    user_id: uuid.UUID
    role: Role
    tenant_id: uuid.UUID | None
    tenant_name: str | None
    portal: str


@dataclass(frozen=True)
class SelectionResult:
    """The user a context token was exchanged for. The route issues the
    session; this module never mints a cookie."""

    user: User


async def find_users(session: AsyncSession, identifier: str) -> list[User]:
    """ALL users whose email equals the identifier, case-insensitively, across
    roles and tenants (unified login, contract rev 2). Ordering keeps the
    context list stable between requests.

    EMAIL ONLY. This used to match `users.phone` too, and a phone number is
    not an identity this product proves any more (phone sign-in was removed):
    two people sharing a number in imported data would each have been offered
    the other's workspace, which is the cross-person chooser the deleted
    phone-reuse refusal in `api/auth` existed to stop. Case-insensitive because
    `firebase_session` matches the same way, and the two answers to "which
    accounts share this address" must agree.
    """
    address = identifier.strip().lower()
    stmt = (
        select(User)
        .where(func.lower(User.email) == address)
        .order_by(User.created_at, User.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def build_contexts(
    session: AsyncSession, users: Sequence[User]
) -> list[LoginContext]:
    tenant_ids = {u.tenant_id for u in users if u.tenant_id is not None}
    names: dict[uuid.UUID, str] = {}
    if tenant_ids:
        rows = (
            await session.execute(select(Tenant).where(Tenant.id.in_(tenant_ids)))
        ).scalars().all()
        names = {t.id: t.name for t in rows}
    return [
        LoginContext(
            user_id=u.id,
            role=u.role,
            tenant_id=u.tenant_id,
            tenant_name=names.get(u.tenant_id) if u.tenant_id else None,
            portal=portal_for_role(u.role),
        )
        for u in users
    ]


async def select_context(
    session: AsyncSession,
    *,
    context_token: str,
    user_id: uuid.UUID | str,
) -> SelectionResult:
    """Exchange a context token for ONE of the identifier's users. Single-use.

    Everything that could refuse the selection runs BEFORE the token is
    consumed, so a mismatched pick or an ineligible account does not burn a
    token the person could still use for a valid choice.

    Raises ContextTokenInvalid / ContextTokenConsumed / ContextUserMismatch /
    UserNotFound / ContextStoreUnavailable.
    """
    settings = get_settings()
    payload = decode_context_token(context_token)

    if str(user_id) not in payload.get("user_ids", []):
        raise ContextUserMismatch("user is not part of this login")

    user = await session.get(User, uuid.UUID(str(user_id)))
    if user is None or not eligible_login_users([user], owner_email=settings.owner_email):
        raise UserNotFound("account unavailable")

    await _consume_once(payload["jti"])

    # Invited -> active on the first proven sign-in, exactly as
    # `api/auth._finalize_single` does for a single-workspace identity.
    if user.status == UserStatus.invited:
        user.status = UserStatus.active
    # Same session issuance, same acceptance (services/staff_invites).
    await staff_invites.accept_pending_invite(session, user.id)
    await session.flush()
    return SelectionResult(user=user)
