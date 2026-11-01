"""The candidate's BGV routes, called the way a browser calls them.

THE DEFECT THIS PINS
--------------------
Every `/bgv/me*` route declared `get_current_user` (staff audiences only)
beside `get_candidate_db` (candidate audience only). No token satisfies both,
so every real candidate got a 401 and Employment History was unreachable for
the whole life of the feature. Every test passed regardless, because every
test overrode BOTH dependencies and so never asked the question a browser
asks.

WHAT IS REAL HERE
-----------------
Nothing is overridden. The candidate is signed in by `tests/candidate_session`,
which writes the rows through `candidate_identity.link_on_sign_in` and the
session record through `auth._issue_session`, so the cookie carries a real
`sid` backed by a real Redis hash. The staff token is minted by the same
function under the ORG audience. Writes are read back on a SECOND connection
after the response, because a write that answered 200 and rolled back at commit
is invisible to an assertion on the response body.

Mutation check: restoring `get_current_user` on any `/bgv/me*` route turns its
status here into 401.
"""
from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from fastapi import Response
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import auth
from app.api.deps import ACCESS_COOKIE
from app.core.config import get_settings
from app.core.db import superadmin_scope
from app.core.security import AUDIENCE_ORG, decode_token
from app.main import app
from app.models.enums import Role, UserStatus
from app.models.user import User
from app.services import auth_sessions
from tests.candidate_session import close_candidate_session, create_candidate_session
from tests.test_bgv_documents import (
    _png,
)

BGV = "/api/v1/bgv"
PREVIOUS_HR = "people@northwind-systems.example.com"
CORRECTED_HR = "verification@northwind-systems.example.com"


def _employer() -> dict:
    return {
        "employer_name": "Northwind Systems",
        "designation": "Senior Engineer",
        "started_on": "2020-02-01",
        "ended_on": "2023-03-31",
        "hr_name": "Anita Rao",
        "hr_email": PREVIOUS_HR,
    }


@pytest.fixture
async def factory():
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def candidate(factory):
    signed_in = await create_candidate_session(factory, full_name="Kavya Menon")
    try:
        yield signed_in
    finally:
        # Deleting the candidate cascades to the history, its corrections, its
        # documents and its verifications; the email_log rows a resend wrote
        # are the only children that do not cascade.
        async with factory() as session:
            async with session.begin():
                async with superadmin_scope(session):
                    await session.execute(
                        sa.text("DELETE FROM email_log WHERE candidate_id = :cid"),
                        {"cid": str(signed_in.candidate_id)},
                    )
        await close_candidate_session(factory, signed_in)


@pytest.fixture
async def staff(factory):
    """A real ORG-audience session for a client user in a real tenant."""
    tenant_id = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            async with superadmin_scope(session):
                await session.execute(
                    sa.text(
                        "INSERT INTO tenants (id, name, domain, spf_dkim_status) "
                        "VALUES (:id, :name, :domain, 'pending')"
                    ),
                    {
                        "id": str(tenant_id),
                        "name": f"BGV-TOKEN-{tenant_id.hex[:6]}",
                        "domain": f"{tenant_id.hex[:10]}.bgvtoken.test",
                    },
                )
                user = User(
                    role=Role.client,
                    email=f"staff-{tenant_id.hex[:10]}@bgvtoken.test",
                    full_name="Staff Member",
                    tenant_id=tenant_id,
                    status=UserStatus.active,
                )
                session.add(user)
                await session.flush()
    response = Response()
    await auth._issue_session(response, user, AUDIENCE_ORG)  # noqa: SLF001
    access = next(
        raw.split(";", 1)[0].split("=", 1)[1]
        for raw in response.headers.getlist("set-cookie")
        if raw.startswith(ACCESS_COOKIE + "=")
    )
    sid = decode_token(access, audience=AUDIENCE_ORG)["sid"]
    try:
        yield {"tenant_id": tenant_id, "user_id": user.id, "access": access}
    finally:
        await auth_sessions.revoke(sid, user.id)
        async with factory() as session:
            async with session.begin():
                async with superadmin_scope(session):
                    await session.execute(
                        sa.text("DELETE FROM users WHERE id = :id"), {"id": str(user.id)}
                    )
                    await session.execute(
                        sa.text("DELETE FROM tenants WHERE id = :id"),
                        {"id": str(tenant_id)},
                    )


def _http(cookies: dict[str, str]) -> TestClient:
    return TestClient(app, cookies=cookies)


async def _employment_rows(factory, candidate_id: uuid.UUID) -> list[tuple[str, str]]:
    async with factory() as session:
        async with session.begin():
            async with superadmin_scope(session):
                rows = await session.execute(
                    sa.text(
                        "SELECT employer_name, hr_email FROM candidate_employments "
                        "WHERE candidate_id = :cid ORDER BY employer_name"
                    ),
                    {"cid": str(candidate_id)},
                )
                return [(row[0], row[1]) for row in rows]


# ── The candidate's own routes, with the candidate's own cookie ──────────────


async def test_a_signed_in_candidate_reads_and_saves_their_history(
    factory, candidate
) -> None:
    with _http(candidate.cookies()) as http:
        empty = http.get(f"{BGV}/me")
        assert empty.status_code == 200, empty.text
        assert empty.json()["employments"] == []

        saved = http.put(
            f"{BGV}/me",
            json={
                "background": "experienced",
                "employments": [_employer()],
                "finalize": False,
            },
        )
        assert saved.status_code == 200, saved.text
        assert [
            (e["employer_name"], e["correction_needed"])
            for e in saved.json()["employments"]
        ] == [("Northwind Systems", False)]

    # Committed, not merely answered: read on a second connection.
    assert await _employment_rows(factory, candidate.candidate_id) == [
        ("Northwind Systems", PREVIOUS_HR)
    ]


async def test_appending_an_employer_reaches_the_handler(factory, candidate) -> None:
    """Before submission the append route refuses with its own sentence.

    A 409 carrying the handler's words is proof the request got past both
    dependencies; a 401 would be the defect.
    """
    with _http(candidate.cookies()) as http:
        refused = http.post(f"{BGV}/me/employers", json=_employer())
    assert refused.status_code == 409, refused.text
    assert "not been submitted" in refused.json()["detail"]


async def test_documents_list_and_delete_but_not_upload_with_a_real_session(
    factory, candidate
) -> None:
    with _http(candidate.cookies()) as http:
        listed = http.get(f"{BGV}/me/documents")
        assert listed.status_code == 200, listed.text
        assert listed.json()["documents"] == []

        # Upload is retired: the method is not on the path.
        uploaded = http.post(
            f"{BGV}/me/documents",
            data={"document_type": "address_proof"},
            files={"file": ("proof.png", io.BytesIO(_png(b"real-token")), "image/png")},
        )
        assert uploaded.status_code == 405, uploaded.text

        # Somebody else's id, or one that never existed, is NOT FOUND.
        missing = http.delete(f"{BGV}/me/documents/{uuid.uuid4()}")
        assert missing.status_code == 404


async def test_correcting_a_bounced_hr_address_with_a_real_session(
    factory, candidate, staff
) -> None:
    with _http(candidate.cookies()) as http:
        submitted = http.put(
            f"{BGV}/me",
            json={
                "background": "experienced",
                "employments": [_employer()],
                "finalize": True,
            },
        )
        assert submitted.status_code == 200, submitted.text
        employment_id = submitted.json()["employments"][0]["id"]

        # Somebody else's employment id is NOT FOUND, never forbidden.
        foreign = http.put(
            f"{BGV}/me/employers/{uuid.uuid4()}/hr-email",
            json={"hr_email": CORRECTED_HR},
        )
        assert foreign.status_code == 404

        # Nothing bounced yet: refused by the handler, and not offered.
        early = http.put(
            f"{BGV}/me/employers/{employment_id}/hr-email",
            json={"hr_email": CORRECTED_HR},
        )
        assert early.status_code == 409, early.text

    sent_at = datetime.now(timezone.utc) - timedelta(hours=3)
    async with factory() as session:
        async with session.begin():
            async with superadmin_scope(session):
                await session.execute(
                    sa.text(
                        "INSERT INTO bgv_verifications (id, tenant_id, candidate_id, "
                        " candidate_employment_id, status, first_sent_at, "
                        " delivery_status, bounced_at, created_at) "
                        "VALUES (gen_random_uuid(), :tid, :cid, :eid, 'pending', "
                        " :at, 'bounced', :at, now())"
                    ),
                    {
                        "tid": str(staff["tenant_id"]),
                        "cid": str(candidate.candidate_id),
                        "eid": employment_id,
                        "at": sent_at,
                    },
                )

    with _http(candidate.cookies()) as http:
        offered = http.get(f"{BGV}/me").json()["employments"]
        assert [e["correction_needed"] for e in offered] == [True]

        corrected = http.put(
            f"{BGV}/me/employers/{employment_id}/hr-email",
            json={"hr_email": CORRECTED_HR},
        )
        assert corrected.status_code == 200, corrected.text
        assert [e["correction_needed"] for e in corrected.json()["employments"]] == [
            False
        ]

    async with factory() as session:
        async with session.begin():
            async with superadmin_scope(session):
                stored = (
                    await session.execute(
                        sa.text(
                            "SELECT hr_email FROM bgv_contact_corrections "
                            "WHERE candidate_employment_id = :eid"
                        ),
                        {"eid": employment_id},
                    )
                ).scalars().all()
    assert stored == [CORRECTED_HR]


# ── The wrong audience is refused, in both directions ────────────────────────


async def test_a_staff_session_is_refused_on_the_candidates_routes(staff) -> None:
    with _http({ACCESS_COOKIE: staff["access"]}) as http:
        # The token is real and live: the any-audience route accepts it.
        assert http.get("/api/v1/auth/me").status_code == 200
        for method, path in (
            ("GET", f"{BGV}/me"),
            ("PUT", f"{BGV}/me"),
            ("POST", f"{BGV}/me/employers"),
            ("PUT", f"{BGV}/me/employers/{uuid.uuid4()}/hr-email"),
            ("GET", f"{BGV}/me/documents"),
            ("DELETE", f"{BGV}/me/documents/{uuid.uuid4()}"),
        ):
            assert http.request(method, path).status_code == 401, (method, path)


async def test_a_candidate_session_is_refused_on_the_recruiters_routes(
    candidate,
) -> None:
    with _http(candidate.cookies()) as http:
        response = http.get(f"{BGV}/candidates/{candidate.candidate_id}")
    assert response.status_code == 401
