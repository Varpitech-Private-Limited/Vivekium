"""Six-digit security codes for company registration and password security.

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
A security code proves that a person reads a mailbox, at the two moments the
auth spec asks for that proof (2.7): the first Company Super Admin verifying
the company email at registration (`company_register`), and anybody changing
or resetting a password (`password_change`, `password_reset`). It is NOT a
login mechanism. Sign-in is Firebase (claude.md rule 2) and the retired
login-code flow stays retired (`tests/test_login_otp_removed.py`); this module
shares none of its names, and user-facing copy calls this a "security code".

WHAT IS STORED AND WHAT NEVER IS
--------------------------------
Redis holds `security_code:{purpose}:{subject digest}` with the HMAC of the
code (keyed with the application secret and bound to the purpose and subject),
the attempt count and the expiry. The plaintext exists in two places for its
whole life: the return value of `issue_code`, which the caller hands straight
to the platform security email, and the mailbox it lands in. It is never
persisted, logged or audited. The subject (an email address or a user id) is
hashed into the key, so a Redis dump does not list who asked for a code.

THE RULES, ALL FROM SETTINGS
----------------------------
A code lives `SECURITY_CODE_TTL_SECONDS` (600), survives
`SECURITY_CODE_MAX_ATTEMPTS` (5) wrong entries, and a second one may be asked
for only after `SECURITY_CODE_RESEND_SECONDS` (60). A new code REPLACES the
previous one, so "which of the two codes counts" never has two answers. Every
attempt is COUNTED BEFORE the comparison. A right code is consumed.

FAILS CLOSED: a Redis that cannot answer raises
`SecurityCodesUnavailable` (503), never a verdict.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time

from fastapi import HTTPException, status

from app.core import cache
from app.core.config import get_settings

log = logging.getLogger(__name__)

__all__ = [
    "CODE_LENGTH",
    "PURPOSES",
    "CodeCooldown",
    "SecurityCodesUnavailable",
    "TICKET_TTL_SECONDS",
    "check_code",
    "issue_code",
    "mint_ticket",
    "redeem_ticket",
]

CODE_LENGTH = 6

#: The only three things a security code is for (auth spec 2.7).
PURPOSES = frozenset({"company_register", "password_change", "password_reset"})

UNAVAILABLE_DETAIL = (
    "Security codes are briefly unavailable. Please try again in a moment."
)


class CodeCooldown(HTTPException):
    """A new code was asked for inside the resend window."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, int(retry_after))
        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "A security code was just sent. You can ask for another in "
                f"{self.retry_after} seconds."
            ),
            headers={"Retry-After": str(self.retry_after)},
        )


class SecurityCodesUnavailable(HTTPException):
    """Redis could not answer; no code was issued and no code was judged."""

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE_DETAIL
        )


def _client():
    client = cache._redis()  # noqa: SLF001 - the shared loop-aware client and test seam
    if client is None:
        log.error("security_codes.store_unavailable op=client")
        raise SecurityCodesUnavailable()
    return client


async def _call(operation: str, factory):
    try:
        return await factory()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - any store failure fails CLOSED
        log.error(
            "security_codes.store_unavailable op=%s err=%s", operation, type(exc).__name__
        )
        raise SecurityCodesUnavailable() from exc


def _check_purpose(purpose: str) -> None:
    if purpose not in PURPOSES:
        raise ValueError(f"unknown security code purpose {purpose!r}")


def _subject(subject: str) -> str:
    """An email is compared case-insensitively; a user id is unchanged by it."""
    return (subject or "").strip().lower()


def _digest(purpose: str, subject: str) -> str:
    return hashlib.sha256(f"{purpose}:{_subject(subject)}".encode("utf-8")).hexdigest()


def _key(purpose: str, subject: str) -> str:
    return f"security_code:{purpose}:{_digest(purpose, subject)}"


def _cooldown_key(purpose: str, subject: str) -> str:
    return f"security_code:cooldown:{purpose}:{_digest(purpose, subject)}"


def _mac(purpose: str, subject: str, code: str) -> str:
    return hmac.new(
        get_settings().jwt_secret.encode("utf-8"),
        f"security-code:{purpose}:{_subject(subject)}:{code}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


async def issue_code(purpose: str, subject: str) -> str:
    """Mint a code for (purpose, subject) and return the PLAINTEXT, once.

    Raises `CodeCooldown` (429) inside the resend window, which is asked
    FIRST and atomically (SET NX EX), so two racing requests cannot both
    mint. The caller sends the code with `pickready.send_security_email`
    and never logs it.
    """
    _check_purpose(purpose)
    if not _subject(subject):
        raise ValueError("a security code needs a subject")
    settings = get_settings()
    client = _client()
    cooldown = _cooldown_key(purpose, subject)
    accepted = await _call(
        "issue.cooldown",
        lambda: client.set(cooldown, "1", nx=True, ex=settings.security_code_resend_seconds),
    )
    if not accepted:
        ttl = await _call("issue.cooldown_ttl", lambda: client.ttl(cooldown))
        raise CodeCooldown(ttl if ttl and ttl > 0 else settings.security_code_resend_seconds)

    # Uniform over the whole six-digit space, leading zeros included.
    code = f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"
    key = _key(purpose, subject)

    async def _write():
        pipe = client.pipeline(transaction=True)
        pipe.delete(key)
        pipe.hset(
            key,
            mapping={
                "mac": _mac(purpose, subject, code),
                "attempts": "0",
                "expires_at": str(int(time.time()) + settings.security_code_ttl_seconds),
            },
        )
        pipe.expire(key, settings.security_code_ttl_seconds)
        return await pipe.execute()

    await _call("issue.write", _write)
    return code


async def check_code(purpose: str, subject: str, code: str) -> bool:
    """True exactly once for the right code; the code is consumed on success.

    False for a wrong, expired, never-issued or exhausted code, with no
    distinction the caller could turn into an oracle. The attempt is counted
    before the comparison, and the code is deleted on the attempt that
    reaches the limit.
    """
    _check_purpose(purpose)
    entered = "".join((code or "").split())
    if not entered.isdigit() or len(entered) != CODE_LENGTH or not _subject(subject):
        return False
    settings = get_settings()
    client = _client()
    key = _key(purpose, subject)
    stored = await _call("check.read", lambda: client.hgetall(key))
    if not stored:
        return False
    if int(stored.get("expires_at", "0")) < int(time.time()):
        await _call("check.expired_delete", lambda: client.delete(key))
        return False
    attempts = int(await _call("check.count", lambda: client.hincrby(key, "attempts", 1)))
    if attempts > settings.security_code_max_attempts:
        await _call("check.exhausted_delete", lambda: client.delete(key))
        return False
    if not hmac.compare_digest(stored.get("mac", ""), _mac(purpose, subject, entered)):
        if attempts >= settings.security_code_max_attempts:
            await _call("check.last_delete", lambda: client.delete(key))
        return False
    # Consumed: only the request whose delete removed the row wins.
    return bool(await _call("check.consume", lambda: client.delete(key)))


# ── Tickets: what a verified code is exchanged for ──────────────────────────
#
# A right code is consumed, so the step AFTER it (choosing the new password)
# needs its own proof that the code step happened. A ticket is that proof: a
# random token stored as its SHA-256 digest with the subject it was minted for,
# single use (the one read deletes it), for `TICKET_TTL_SECONDS`. The password
# reset calls it a reset token and the password change a change token.

TICKET_TTL_SECONDS = 600


def _ticket_key(purpose: str, token: str) -> str:
    return (
        f"security_code:ticket:{purpose}:"
        + hashlib.sha256(token.encode("utf-8")).hexdigest()
    )


async def mint_ticket(purpose: str, subject: str) -> str:
    """A single-use token proving `subject` passed the code step of `purpose`."""
    _check_purpose(purpose)
    token = secrets.token_urlsafe(32)
    client = _client()
    await _call(
        "ticket.mint",
        lambda: client.set(
            _ticket_key(purpose, token), _subject(subject), ex=TICKET_TTL_SECONDS
        ),
    )
    return token


async def redeem_ticket(purpose: str, token: str) -> str | None:
    """The subject a ticket was minted for, once; None when it is unknown,
    expired or already used."""
    _check_purpose(purpose)
    if not token or len(token) > 128:
        return None
    client = _client()
    return await _call("ticket.redeem", lambda: client.getdel(_ticket_key(purpose, token)))
