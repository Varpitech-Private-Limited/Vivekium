"""One person, one candidate record, resolved one way.

WHAT THIS PINS
--------------
`services/candidate_identity` replaced four resolvers that disagreed. The
dangerous one matched `candidates.email = users.email` at REQUEST time on an
address nobody had verified, so a password sign-up carrying somebody else's
address read that person's recruiter-sourced record. The rules now:

* a request resolves by `user_id` only;
* an email match links a record at SIGN-IN, only when Firebase says the
  address is verified, and it links the OLDEST unlinked record;
* an unverified identity gets its own record and links nothing;
* one user owns at most one record, by a UNIQUE index (migration 0120).

Every database assertion reads committed state on a SECOND session after the
call under test has committed, because a write that answered and then rolled
back is invisible to anything that shares its transaction.

The AST sweep at the bottom keeps new request-time resolvers from appearing,
with a ratchet over the legacy ones still owned by other work packages.
"""
from __future__ import annotations

import ast
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.auth import firebase_session
from app.core.config import get_settings
from app.core.db import superadmin_scope
from app.models.candidate import Candidate, JobCandidateLink
from app.models.enums import JobStatus, LinkSource, Role, UserStatus
from app.models.job import Job
from app.models.tenant import AuditLog, Tenant
from app.models.user import User
from app.schemas.auth import FirebaseSessionIn
from app.services import candidate_identity, firebase_auth
from app.services.firebase_auth import FirebaseIdentity

BACKEND = pathlib.Path(__file__).resolve().parents[1]


# ── Pure ─────────────────────────────────────────────────────────────────────

def test_normalise_email_trims_lowers_and_empties_to_none() -> None:
    assert candidate_identity.normalise_email("  Asha@Example.COM ") == "asha@example.com"
    assert candidate_identity.normalise_email("   ") is None
    assert candidate_identity.normalise_email(None) is None


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
async def factory():
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _address() -> str:
    return f"identity-{uuid.uuid4().hex}@vivekium.test"


def _identity(email: str | None, *, verified: bool) -> FirebaseIdentity:
    return FirebaseIdentity(
        uid=f"fbuid-{uuid.uuid4().hex}", email=email, name="Identity Test",
        provider="password", email_verified=verified,
    )


async def _sign_in(factory, identity: FirebaseIdentity, monkeypatch):
    """`/auth/firebase/session` as the route runs it, with the one call that
    needs Google replaced by the identity it would have returned."""
    monkeypatch.setattr(firebase_auth, "verify_id_token", lambda _token: identity)
    async with factory() as session:
        async with superadmin_scope(session):
            return await firebase_session(
                FirebaseSessionIn(
                    id_token="t" * 40,
                    captcha_purpose="candidate_login",
                ),
                Response(),
                session,
            )


async def _sourced(factory, email: str, *, created_at: datetime | None = None,
                   user_id: uuid.UUID | None = None) -> uuid.UUID:
    """A recruiter-uploaded record: no user, carrying `email`."""
    candidate_id = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            async with superadmin_scope(session):
                row = Candidate(id=candidate_id, email=email, full_name="Sourced",
                                user_id=user_id)
                if created_at is not None:
                    row.created_at = created_at
                session.add(row)
    return candidate_id


async def _rows_for(factory, email: str) -> list[tuple[uuid.UUID, uuid.UUID | None]]:
    """(id, user_id) of every record carrying `email`, case-insensitively."""
    async with factory() as session:
        async with superadmin_scope(session):
            return [
                (row.id, row.user_id)
                for row in (
                    await session.execute(
                        select(Candidate.id, Candidate.user_id)
                        .where(func.lower(Candidate.email) == email.lower())
                        .order_by(Candidate.created_at)
                    )
                ).all()
            ]


async def _cleanup(factory, email: str) -> None:
    async with factory() as session:
        async with session.begin():
            async with superadmin_scope(session):
                await session.execute(
                    delete(Candidate).where(func.lower(Candidate.email) == email.lower())
                )
                await session.execute(
                    delete(User).where(func.lower(User.email) == email.lower())
                )


# ── Sign-in linking ──────────────────────────────────────────────────────────

async def test_verified_first_sign_in_links_the_oldest_unlinked_record(
    factory, monkeypatch
) -> None:
    email = _address()
    now = datetime.now(timezone.utc)
    oldest = await _sourced(factory, email.upper(), created_at=now - timedelta(days=9))
    newer = await _sourced(factory, email, created_at=now - timedelta(days=1))
    try:
        out = await _sign_in(factory, _identity(email, verified=True), monkeypatch)
        rows = dict(await _rows_for(factory, email))
        assert set(rows) == {oldest, newer}, "a verified sign-in created a record"
        assert rows[oldest] == out.user.id, "the OLDEST record is the one linked"
        assert rows[newer] is None, "only one record may be linked"
        async with factory() as session:
            async with superadmin_scope(session):
                audited = (
                    await session.execute(
                        select(func.count()).select_from(AuditLog).where(
                            AuditLog.action == candidate_identity.ACTION_CANDIDATE_LINKED,
                            AuditLog.target_id == str(oldest),
                        )
                    )
                ).scalar_one()
        assert audited == 1
    finally:
        await _cleanup(factory, email)


async def test_unverified_sign_in_creates_its_own_record_and_links_nothing(
    factory, monkeypatch
) -> None:
    """The takeover guard. An unverified password identity proves nothing about
    the address, so the sourced record carrying it stays unlinked and unread."""
    email = _address()
    sourced = await _sourced(factory, email)
    try:
        out = await _sign_in(factory, _identity(email, verified=False), monkeypatch)
        rows = dict(await _rows_for(factory, email))
        assert rows[sourced] is None
        own = [cid for cid, uid in rows.items() if uid == out.user.id]
        assert len(own) == 1 and own[0] != sourced
    finally:
        await _cleanup(factory, email)


async def test_sign_in_is_idempotent_and_repairs_an_erased_record(
    factory, monkeypatch
) -> None:
    email = _address()
    identity = _identity(email, verified=True)
    try:
        first = await _sign_in(factory, identity, monkeypatch)
        await _sign_in(factory, identity, monkeypatch)
        assert [uid for _, uid in await _rows_for(factory, email)] == [first.user.id]

        async with factory() as session:
            async with session.begin():
                async with superadmin_scope(session):
                    await session.execute(
                        delete(Candidate).where(Candidate.user_id == first.user.id)
                    )
        await _sign_in(factory, identity, monkeypatch)
        assert [uid for _, uid in await _rows_for(factory, email)] == [first.user.id]
    finally:
        await _cleanup(factory, email)


# ── Request-time resolution ──────────────────────────────────────────────────

async def test_every_resolver_answers_the_same_record_and_never_by_email(
    factory, monkeypatch
) -> None:
    email = _address()
    try:
        out = await _sign_in(factory, _identity(email, verified=True), monkeypatch)
        # A second record with the SAME address, never linked: a request must
        # not be able to reach it, whatever its age.
        await _sourced(factory, email, created_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
        async with factory() as session:
            async with superadmin_scope(session):
                resolved = await candidate_identity.resolve_candidate_id(
                    session, out.user.id
                )
                required = await candidate_identity.require_candidate(
                    session, out.user.id
                )
        linked = [cid for cid, uid in await _rows_for(factory, email) if uid == out.user.id]
        assert resolved == required.id == linked[0]
    finally:
        await _cleanup(factory, email)


async def test_require_candidate_is_404_without_a_linked_record_and_stamps_engagement(
    factory,
) -> None:
    async with factory() as session:
        async with superadmin_scope(session):
            with pytest.raises(HTTPException) as refused:
                await candidate_identity.require_candidate(session, uuid.uuid4())
    assert refused.value.status_code == 404
    assert refused.value.detail == candidate_identity.NO_CANDIDATE_DETAIL

    email = _address()
    try:
        async with factory() as session:
            async with session.begin():
                async with superadmin_scope(session):
                    user = User(role=Role.candidate, email=email, tenant_id=None,
                                status=UserStatus.active)
                    session.add(user)
                    await session.flush()
                    session.add(Candidate(email=email, user_id=user.id))
        async with factory() as session:
            async with session.begin():
                async with superadmin_scope(session):
                    candidate = await candidate_identity.require_candidate(
                        session, user.id
                    )
                    assert candidate.last_engagement_at is not None
    finally:
        await _cleanup(factory, email)


async def test_find_canonical_by_email_prefers_the_signed_in_record_then_the_oldest(
    factory, monkeypatch
) -> None:
    email = _address()
    now = datetime.now(timezone.utc)
    try:
        older_unlinked = await _sourced(factory, email, created_at=now - timedelta(days=30))
        await _sourced(factory, email, created_at=now - timedelta(days=2))
        async with factory() as session:
            async with superadmin_scope(session):
                first = await candidate_identity.find_canonical_by_email(
                    session, f"  {email.upper()} "
                )
        assert first.id == older_unlinked

        out = await _sign_in(factory, _identity(email, verified=False), monkeypatch)
        async with factory() as session:
            async with superadmin_scope(session):
                chosen = await candidate_identity.find_canonical_by_email(session, email)
                none = await candidate_identity.find_canonical_by_email(session, "  ")
        assert chosen.user_id == out.user.id
        assert none is None
    finally:
        await _cleanup(factory, email)


async def test_one_user_cannot_own_two_records(factory) -> None:
    """Migration 0120's UNIQUE index, read as the database enforces it."""
    email = _address()
    try:
        async with factory() as session:
            async with session.begin():
                async with superadmin_scope(session):
                    user = User(role=Role.candidate, email=email, tenant_id=None,
                                status=UserStatus.active)
                    session.add(user)
                    await session.flush()
                    session.add(Candidate(email=email, user_id=user.id))
        with pytest.raises(IntegrityError):
            async with factory() as session:
                async with session.begin():
                    async with superadmin_scope(session):
                        session.add(Candidate(email=email, user_id=user.id))
    finally:
        await _cleanup(factory, email)


# ── The sourced-link rehome ──────────────────────────────────────────────────

async def _job_with_sourced_link(factory, sourced_candidate: uuid.UUID):
    tenant_id, job_id, link_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            async with superadmin_scope(session):
                session.add(Tenant(id=tenant_id, name=f"Rehome {tenant_id.hex[:6]}",
                                   domain=f"{tenant_id}.rehome.test"))
                await session.flush()
                session.add(Job(id=job_id, tenant_id=tenant_id, title="Engineer",
                                jd_json={}, status=JobStatus.ratified,
                                ratified_at=datetime.now(timezone.utc),
                                assessment_grade="non_managerial"))
                await session.flush()
                session.add(JobCandidateLink(
                    id=link_id, tenant_id=tenant_id, job_id=job_id,
                    candidate_id=sourced_candidate, source=LinkSource.fresh,
                    status="sourced",
                ))
    return tenant_id, job_id, link_id


async def _drop_job(factory, tenant_id, job_id) -> None:
    async with factory() as session:
        async with session.begin():
            async with superadmin_scope(session):
                await session.execute(
                    delete(JobCandidateLink).where(JobCandidateLink.job_id == job_id)
                )
                await session.execute(delete(Job).where(Job.id == job_id))
                await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


async def _link_owner(factory, link_id) -> uuid.UUID:
    async with factory() as session:
        async with superadmin_scope(session):
            return (
                await session.execute(
                    select(JobCandidateLink.candidate_id).where(
                        JobCandidateLink.id == link_id
                    )
                )
            ).scalar_one()


async def _rehome(factory, applicant_user: uuid.UUID, job_id, *, verified: bool):
    async with factory() as session:
        async with session.begin():
            async with superadmin_scope(session):
                applicant = await candidate_identity.require_candidate(
                    session, applicant_user, record_engagement=False
                )
                return await candidate_identity.rehome_sourced_link(
                    session, applicant=applicant, job_id=job_id,
                    verified_email=verified,
                )


async def test_a_verified_applicant_takes_over_their_sourced_link(
    factory, monkeypatch
) -> None:
    email = _address()
    sourced = await _sourced(factory, email)
    tenant_id, job_id, link_id = await _job_with_sourced_link(factory, sourced)
    try:
        # Signed up UNVERIFIED first, so sign-in created a separate record.
        out = await _sign_in(factory, _identity(email, verified=False), monkeypatch)

        assert await _rehome(factory, out.user.id, job_id, verified=False) is None
        assert await _link_owner(factory, link_id) == sourced

        moved = await _rehome(factory, out.user.id, job_id, verified=True)
        assert moved is not None and moved.id == link_id
        own = [cid for cid, uid in await _rows_for(factory, email) if uid == out.user.id]
        assert await _link_owner(factory, link_id) == own[0]
        # A second call finds the applicant already on the job: a no-op.
        assert await _rehome(factory, out.user.id, job_id, verified=True) is None
        async with factory() as session:
            async with superadmin_scope(session):
                audited = (
                    await session.execute(
                        select(func.count()).select_from(AuditLog).where(
                            AuditLog.action == candidate_identity.ACTION_LINK_REHOMED,
                            AuditLog.target_id == str(link_id),
                        )
                    )
                ).scalar_one()
        assert audited == 1
    finally:
        await _drop_job(factory, tenant_id, job_id)
        await _cleanup(factory, email)


async def test_a_link_owned_by_a_signed_in_record_is_never_taken(
    factory, monkeypatch
) -> None:
    email = _address()
    try:
        owner = await _sign_in(factory, _identity(email, verified=True), monkeypatch)
        owned = [cid for cid, uid in await _rows_for(factory, email) if uid == owner.user.id][0]
        tenant_id, job_id, link_id = await _job_with_sourced_link(factory, owned)
        try:
            # Same address, a different verified account (an unverified record
            # created for it). The link belongs to a record somebody signed in
            # to, so it is not an orphan waiting for its person.
            async with factory() as session:
                async with session.begin():
                    async with superadmin_scope(session):
                        other = User(role=Role.candidate, email=email.upper(),
                                     tenant_id=None, status=UserStatus.active)
                        session.add(other)
                        await session.flush()
                        session.add(Candidate(email=email.upper(), user_id=other.id))
            assert await _rehome(factory, other.id, job_id, verified=True) is None
            assert await _link_owner(factory, link_id) == owned
        finally:
            await _drop_job(factory, tenant_id, job_id)
    finally:
        await _cleanup(factory, email)


# ── No request-time resolver outside the module ──────────────────────────────

#: Files that still resolve a candidate by `user_id` or match candidates by
#: `email` themselves, each with the work package that converts it. The list can
#: only SHRINK: a file that gains a hit fails, and so does an entry whose file no
#: longer needs it (remove the entry in the same change that converts the file).
LEGACY_RESOLVERS: dict[str, str] = {}

_SQL_RESOLVER_FRAGMENTS = ("c.user_id = :uid", "c.email = u.email")


def _hits(path: pathlib.Path) -> list[int]:
    """Lines where `path` resolves a candidate itself.

    An ORM comparison `Candidate.user_id == ...` / `Candidate.email == ...` is
    found by AST, so a comment or a docstring naming the pattern is not a hit;
    the raw SQL resolvers are found in string constants only, for the same
    reason.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(
            isinstance(op, ast.Eq) for op in node.ops
        ):
            for side in (node.left, *node.comparators):
                if (
                    isinstance(side, ast.Attribute)
                    and side.attr in {"user_id", "email"}
                    and isinstance(side.value, ast.Name)
                    and side.value.id == "Candidate"
                ):
                    lines.append(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(fragment in node.value for fragment in _SQL_RESOLVER_FRAGMENTS):
                lines.append(node.lineno)
    return sorted(set(lines))


def test_no_request_time_candidate_resolver_outside_candidate_identity() -> None:
    found: dict[str, list[int]] = {}
    for root in ("app/api", "app/services"):
        for path in sorted((BACKEND / root).rglob("*.py")):
            relative = path.relative_to(BACKEND).as_posix()
            if relative == "app/services/candidate_identity.py":
                continue
            lines = _hits(path)
            if lines:
                found[relative] = lines

    unexpected = {path: lines for path, lines in found.items()
                  if path not in LEGACY_RESOLVERS}
    assert not unexpected, (
        "resolve a candidate through services/candidate_identity, never by "
        f"comparing Candidate.user_id or Candidate.email in place: {unexpected}"
    )
    stale = sorted(set(LEGACY_RESOLVERS) - set(found))
    assert not stale, (
        f"these files no longer resolve candidates themselves; remove them from "
        f"LEGACY_RESOLVERS so the ratchet stays tight: {stale}"
    )


def test_the_sweep_sees_the_patterns_it_forbids(tmp_path) -> None:
    """A sweep that matches nothing passes for ever. Feed it each shape."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "# Candidate.user_id == x in a comment is not a hit\n"
        "q = select(Candidate).where(Candidate.user_id == uid)\n"
        "r = Candidate.email == address\n"
        "s = 'SELECT c.id FROM candidates c WHERE c.user_id = :uid'\n",
        encoding="utf-8",
    )
    assert _hits(sample) == [2, 3, 4]
