"""Cross-tenant reads, writes and deletes, through the REAL product routes.

WHY THIS FILE EXISTS WHEN `test_cross_tenant_isolation.py` ALREADY PASSES
--------------------------------------------------------------------------
That file proves the DATABASE refuses a cross-tenant row: it seeds two graphs,
enters `tenant_scope`, and checks that a SELECT returns only its own. It is the
right test for the RLS policies and it says nothing at all about the product.

A route can defeat every one of those policies without touching them. It can
read through `get_superadmin_db`. It can look a row up by a column the policy
does not key on. It can take the tenant from the request instead of the
session. It can answer 403 where it should answer 404, which discloses that
the row exists to somebody who may not read it. None of that is visible from
a SQL-level test, and the ones that ARE tested at route level today
(`test_conversations_api`, `test_support_flow`, `test_bgv_api`,
`test_dashboard_rbac_matrix`) cover four surfaces out of the product.

The gap this closes is the rest: jobs, candidate profiles, project evidence,
reports, resume downloads and application archiving, over READ, UPDATE and
DELETE, through `app.main.app` with real routers and the real RLS session.

404, NEVER 403, AND THAT IS THE ASSERTION
------------------------------------------
Every case asserts the exact status. A 403 on a resource in another customer's
tenant CONFIRMS THE ID IS REAL to a caller who has no business knowing that,
and the difference between 403 and 404 is the whole of what an enumeration
attack needs. `assert response.status_code == 404` rather than
`assert response.status_code != 200` for the same reason: a 500 is also not a
200, and a stack trace is not an access control.

THE CALLER IS DELIBERATELY OVER-PRIVILEGED
--------------------------------------------
Each request is made as a Super Admin of tenant A, holding every capability
their role grants. A test that used an under-privileged caller would collect
403s from the capability gate and prove nothing about the tenant boundary: the
refusal has to come from the boundary itself, so the caller is given every
reason to succeed except the one that matters. The paired "tenant A reaches
its OWN copy of the same resource" assertion in each test is what proves the
404 is isolation rather than a fixture that never worked.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Iterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.deps import CurrentUser, get_current_user, get_tenant_db
from app.core.config import get_settings
from app.core.db import superadmin_scope, tenant_scope
from app.core.security import AUDIENCE_ORG
from app.main import app
from app.models.enums import Role

V1 = "/api/v1"
V2 = "/api/v2"


def _run(coro):
    """One fresh loop per call.

    TestClient drives the application on its own loop, so an engine bound to a
    loop that has since closed surfaces later as an unrelated timeout in a
    different test.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _sessions():
    return async_sessionmaker(
        create_async_engine(get_settings().database_url, poolclass=NullPool),
        expire_on_commit=False,
    )


async def _reachable() -> bool:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1 FROM job_candidate_links LIMIT 0"))
        return True
    except Exception:  # noqa: BLE001 -- no database here
        return False
    finally:
        await engine.dispose()


class Side:
    """One customer's complete, self-consistent resource graph.

    Both sides are identical in shape, so every assertion below can be made
    twice with the roles reversed. A one-sided fixture hides the case where
    isolation happens to hold in one direction because of row ordering.
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self.tenant = uuid.uuid4()
        self.user = uuid.uuid4()
        self.job = uuid.uuid4()
        self.candidate = uuid.uuid4()
        self.profile = uuid.uuid4()
        self.link = uuid.uuid4()
        self.report = uuid.uuid4()


class World:
    def __init__(self) -> None:
        self.a = Side("Isolation-A")
        self.b = Side("Isolation-B")


async def _seed_side(session, side: Side) -> None:
    ids = {
        "tenant": str(side.tenant),
        "user": str(side.user),
        "job": str(side.job),
        "candidate": str(side.candidate),
        "profile": str(side.profile),
        "link": str(side.link),
        "report": str(side.report),
        "label": side.label,
    }
    await session.execute(
        sa.text(
            "INSERT INTO tenants (id, name, domain, spf_dkim_status) "
            "VALUES (:tenant, :name, :domain, 'pending')"
        ),
        {**ids, "name": f"{side.label}-{side.tenant.hex[:6]}",
         "domain": f"{side.tenant.hex[:10]}.iso.test"},
    )
    # `client` is the RBAC Super Admin: tenant-scoped, top of the customer
    # hierarchy, holder of every capability the tenant has. See the module
    # docstring for why the caller is deliberately the most privileged one.
    await session.execute(
        sa.text(
            "INSERT INTO users (id, tenant_id, email, full_name, role, status) "
            "VALUES (:user, :tenant, :email, 'Priya Raman', 'client', 'active')"
        ),
        {**ids, "email": f"{side.user.hex[:10]}@iso.test"},
    )
    # `ratified_at` is set because `_get_visible_job` hides a pre-ratified job
    # from callers who cannot see one. An unratified job would answer 404 for
    # its OWN tenant too, and the test would pass while proving nothing.
    await session.execute(
        sa.text(
            "INSERT INTO jobs (id, tenant_id, title, jd_json, status, "
            " ratified_at) "
            "VALUES (:job, :tenant, :label, CAST('{}' AS jsonb), 'ratified', "
            " now())"
        ),
        ids,
    )
    await session.execute(
        sa.text(
            "INSERT INTO candidates (id, full_name, email, consent_databank) "
            "VALUES (:candidate, :label, :email, false)"
        ),
        {**ids, "email": f"{side.candidate.hex[:10]}@iso.test"},
    )
    await session.execute(
        sa.text(
            "INSERT INTO profiles (id, candidate_id, source_tenant_id, "
            " resume_text, resume_url, resume_public_id) "
            "VALUES (:profile, :candidate, :tenant, :label, :url, :pid)"
        ),
        {**ids, "url": f"s3://iso/{side.profile}", "pid": f"resumes/{side.profile}"},
    )
    await session.execute(
        sa.text(
            "INSERT INTO job_candidate_links "
            "(id, tenant_id, job_id, candidate_id, profile_id, source, status) "
            "VALUES (:link, :tenant, :job, :candidate, :profile, 'fresh', "
            " 'applied')"
        ),
        ids,
    )
    await session.execute(
        sa.text(
            "INSERT INTO functional_skills_reports "
            "(id, tenant_id, job_id, job_candidate_link_id, grade, status, "
            " overall_summary, validation_json, suggested_probes_json, "
            " synthesized_at) "
            "VALUES (:report, :tenant, :job, :link, 'non_managerial', 'ready', "
            " :label, CAST('{}' AS jsonb), CAST('[]' AS jsonb), now())"
        ),
        ids,
    )


@pytest.fixture
def world() -> Iterator[World]:
    if not _run(_reachable()):
        pytest.skip("no database reachable -- skipping HTTP tenant isolation test")

    w = World()
    sessions = _sessions()

    async def _seed() -> None:
        async with sessions() as session:
            async with session.begin():
                async with superadmin_scope(session):
                    await _seed_side(session, w.a)
                    await _seed_side(session, w.b)

    async def _teardown() -> None:
        async with sessions() as session:
            async with session.begin():
                async with superadmin_scope(session):
                    # Tenant deletion cascades every tenant-owned row. The
                    # candidate and profile carry no tenant FK by design
                    # (a candidate spans tenants), so they are removed by id.
                    await session.execute(
                        sa.text("DELETE FROM profiles WHERE id = ANY(:ids)"),
                        {"ids": [str(w.a.profile), str(w.b.profile)]},
                    )
                    await session.execute(
                        sa.text("DELETE FROM tenants WHERE id = ANY(:ids)"),
                        {"ids": [str(w.a.tenant), str(w.b.tenant)]},
                    )
                    await session.execute(
                        sa.text("DELETE FROM candidates WHERE id = ANY(:ids)"),
                        {"ids": [str(w.a.candidate), str(w.b.candidate)]},
                    )

    _run(_seed())
    try:
        yield w
    finally:
        _run(_teardown())


class Caller:
    def __init__(self) -> None:
        self.principal: CurrentUser | None = None
        self.http: TestClient | None = None

    def acting_for(self, side: Side) -> None:
        self.principal = CurrentUser(
            user_id=side.user,
            tenant_id=side.tenant,
            role=Role.client,
            audience=AUDIENCE_ORG,
        )


@pytest.fixture
def client(world: World) -> Iterator[Caller]:
    sessions = _sessions()
    caller = Caller()

    async def _current_user() -> CurrentUser:
        assert caller.principal is not None
        return caller.principal

    async def _tenant_db():
        principal = caller.principal
        assert principal is not None
        async with sessions() as session:
            async with session.begin():
                # Entered exactly as the production dependency enters it, so a
                # refusal comes from Postgres and not only from the
                # application's own comparison.
                async with tenant_scope(session, principal.tenant_id):
                    yield session

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_current_user] = _current_user
    app.dependency_overrides[get_tenant_db] = _tenant_db
    try:
        with TestClient(app) as http:
            caller.http = http
            yield caller
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def _both_directions(world: World):
    """Every case runs A-against-B and B-against-A.

    Isolation that holds one way and not the other is the shape a row-ordering
    accident takes, and a one-directional test cannot see it.
    """
    return ((world.a, world.b), (world.b, world.a))


# ── READ ────────────────────────────────────────────────────────────────────


def test_a_job_in_another_tenant_reads_as_missing(
    client: Caller, world: World
) -> None:
    for mine, theirs in _both_directions(world):
        client.acting_for(mine)
        # The control: my own job is genuinely reachable, so a 404 below is
        # isolation and not a fixture that never worked.
        own = client.http.get(f"{V1}/jobs/{mine.job}")
        assert own.status_code == 200, own.text
        assert own.json()["title"] == mine.label

        refused = client.http.get(f"{V1}/jobs/{theirs.job}")
        assert refused.status_code == 404, refused.text
        assert theirs.label not in refused.text


def test_the_v2_mount_of_the_same_router_is_isolated_too(
    client: Caller, world: World
) -> None:
    """One router object, two prefixes. A test that only exercised /api/v1
    would leave the second URL for the same handler unproven, and a future
    mount-specific dependency is exactly the thing that would differ."""
    for mine, theirs in _both_directions(world):
        client.acting_for(mine)
        assert client.http.get(f"{V2}/jobs/{mine.job}").status_code == 200
        assert client.http.get(f"{V2}/jobs/{theirs.job}").status_code == 404


def test_another_tenants_report_reads_as_missing(
    client: Caller, world: World
) -> None:
    """The report is the most sensitive artifact in the product: it names a
    person and states how they were graded."""
    for mine, theirs in _both_directions(world):
        client.acting_for(mine)
        own = client.http.get(f"{V2}/assessments/reports/links/{mine.link}")
        assert own.status_code == 200, own.text

        refused = client.http.get(f"{V2}/assessments/reports/links/{theirs.link}")
        assert refused.status_code == 404, refused.text
        # And the refusal must not leak the grade or the summary it withheld.
        assert theirs.label not in refused.text


def test_another_tenants_project_evidence_reads_as_missing(
    client: Caller, world: World
) -> None:
    """The route's own docstring promises 404 rather than 403 for a candidate
    who is not linked in this tenant. Nothing asserted it."""
    for mine, theirs in _both_directions(world):
        client.acting_for(mine)
        own = client.http.get(f"{V1}/candidates/{mine.candidate}/project-evidence")
        assert own.status_code == 200, own.text

        refused = client.http.get(
            f"{V1}/candidates/{theirs.candidate}/project-evidence"
        )
        assert refused.status_code == 404, refused.text


def _seed_projects(candidate: uuid.UUID, statuses: list[str]) -> None:
    async def _go() -> None:
        sessions = _sessions()
        async with sessions() as session:
            async with session.begin():
                async with superadmin_scope(session):
                    for index, status in enumerate(statuses):
                        await session.execute(
                            sa.text(
                                "INSERT INTO candidate_projects (id, candidate_id, "
                                " name, description, submission_kind, status, "
                                " status_detail, evidence_json) VALUES (:id, :cid, "
                                " :name, 'mine', 'repository', :status, :detail, "
                                " CAST(:evidence AS jsonb))"
                            ),
                            {
                                "id": str(uuid.uuid4()),
                                "cid": str(candidate),
                                "name": f"Project {index}",
                                "status": status,
                                "detail": "A note for the reviewer." if "fail" in status else None,
                                "evidence": "{}",
                            },
                        )

    _run(_go())


def test_project_submissions_are_reported_apart_from_ready_evidence(
    client: Caller, world: World
) -> None:
    """Pending, partial, success and failed all show; only ready ones carry evidence.

    The false "none" came from returning only evidence-ready rows. Another
    tenant's reviewer still gets 404 for these candidates, and their own
    candidate's counts do not include these rows.
    """
    _seed_projects(
        world.a.candidate,
        ["submitted", "processing", "partially_processed", "processed", "failed_extraction"],
    )
    client.acting_for(world.a)
    body = client.http.get(f"{V1}/candidates/{world.a.candidate}/project-evidence").json()
    assert body["counts"] == {
        "submitted": 5,
        "queued": 1,
        "analysing": 1,
        "ready": 1,
        "summary_only": 1,
        "failed": 1,
    }
    assert len(body["submissions"]) == 5
    assert len(body["projects"]) == 2  # processed and partially_processed only
    failed = [row for row in body["submissions"] if row["state"] == "failed"]
    assert failed[0]["status_detail"] == "A note for the reviewer."
    assert "failure_code" not in failed[0]

    # Isolation, both ways: B cannot read A's candidate, and A's rows do not
    # appear in B's own candidate.
    assert (
        client.http.get(f"{V1}/candidates/{world.b.candidate}/project-evidence").status_code
        == 404
    )
    client.acting_for(world.b)
    assert (
        client.http.get(f"{V1}/candidates/{world.a.candidate}/project-evidence").status_code
        == 404
    )
    own = client.http.get(f"{V1}/candidates/{world.b.candidate}/project-evidence").json()
    assert own["counts"]["submitted"] == 0 and own["submissions"] == []


def test_another_tenants_candidate_profile_reads_as_missing(
    client: Caller, world: World
) -> None:
    """THE ONE MOST LIKELY TO FAIL, AND THE REASON IT IS HERE.

    `candidates` and `profiles` are deliberately NOT tenant-owned: a candidate
    spans tenants through the databank, and `profiles.source_tenant_id` is a
    plain nullable UUID with no foreign key. So this handler cannot rely on
    the tenant equality that protects `jobs` and `job_candidate_links`, and
    when the caller holds `send_outreach` the handler's own link check is
    skipped entirely. What is left standing between tenant A and tenant B's
    resume is the RLS policy on those two tables and nothing else.

    Asserted with no `profile_id` query parameter on purpose: supplying one
    takes a second branch that DOES check the link, so a test that always sent
    it would exercise the guarded path and never the unguarded one.
    """
    for mine, theirs in _both_directions(world):
        client.acting_for(mine)
        own = client.http.get(f"{V1}/candidates/{mine.candidate}/profile")
        assert own.status_code == 200, own.text

        refused = client.http.get(f"{V1}/candidates/{theirs.candidate}/profile")
        assert refused.status_code == 404, refused.text
        assert theirs.label not in refused.text


def test_another_tenants_resume_file_is_not_served(
    client: Caller, world: World
) -> None:
    """A download route is where an isolation bug becomes a file on a laptop.

    `follow_redirects=False` is load bearing: the authorized path answers 307
    to itself carrying a freshly issued access token, and following it would
    make the assertion about the second request rather than the first.
    """
    for mine, theirs in _both_directions(world):
        client.acting_for(mine)
        own = client.http.get(
            f"{V1}/candidates/profiles/{mine.profile}/resume-file",
            follow_redirects=False,
        )
        assert own.status_code == 307, own.text

        refused = client.http.get(
            f"{V1}/candidates/profiles/{theirs.profile}/resume-file",
            follow_redirects=False,
        )
        assert refused.status_code == 404, refused.text
        # Never a storage URL or an object key, on either answer.
        assert "s3://" not in refused.text


# ── UPDATE ──────────────────────────────────────────────────────────────────


def test_another_tenants_job_cannot_be_edited(client: Caller, world: World) -> None:
    """A write is worse than a read and must be refused identically.

    The assertion is not only the status: the row is read back afterwards
    through the owning tenant to prove nothing was written. A handler that
    404'd AFTER flushing the change would pass a status-only test.
    """
    for mine, theirs in _both_directions(world):
        client.acting_for(mine)
        refused = client.http.patch(
            f"{V1}/jobs/{theirs.job}", json={"title": "Seized by another tenant"}
        )
        assert refused.status_code == 404, refused.text

        client.acting_for(theirs)
        after = client.http.get(f"{V1}/jobs/{theirs.job}")
        assert after.status_code == 200
        assert after.json()["title"] == theirs.label, "the refused edit was written"


def test_another_tenants_job_cannot_be_closed(client: Caller, world: World) -> None:
    """Gate 8 is terminal for candidates and there is no reopen, so a
    cross-tenant close is an irreversible denial of service on a competitor's
    live posting."""
    for mine, theirs in _both_directions(world):
        client.acting_for(mine)
        refused = client.http.post(
            f"{V1}/jobs/{theirs.job}/close", json={"reason": "filled"}
        )
        assert refused.status_code == 404, refused.text

    # Read back through each owner: both postings are still open.
    for side in (world.a, world.b):
        client.acting_for(side)
        detail = client.http.get(f"{V1}/jobs/{side.job}")
        assert detail.status_code == 200
        assert detail.json().get("closed_at") is None, "the job was closed"


# ── DELETE ──────────────────────────────────────────────────────────────────


def test_another_tenants_application_cannot_be_archived(
    client: Caller, world: World
) -> None:
    """The product's only destructive candidate operation from the portal.

    Archiving is reversible, which is exactly why it is easy to under-guard:
    the consequence of getting it wrong reads as a display bug rather than as
    data loss, while the candidate has silently left the other customer's
    ranked list.
    """
    for mine, theirs in _both_directions(world):
        client.acting_for(mine)
        refused = client.http.delete(f"{V1}/candidates/links/{theirs.link}")
        assert refused.status_code == 404, refused.text

    # The rows are read directly rather than through a route, because the
    # question is whether a WRITE happened and no route reports `archived_at`
    # unconditionally.
    sessions = _sessions()

    async def _archived_flags() -> list[object]:
        async with sessions() as session:
            async with session.begin():
                async with superadmin_scope(session):
                    return (
                        await session.execute(
                            sa.text(
                                "SELECT archived_at FROM job_candidate_links "
                                "WHERE id = ANY(:ids) ORDER BY id"
                            ),
                            {"ids": [str(world.a.link), str(world.b.link)]},
                        )
                    ).scalars().all()

    assert all(value is None for value in _run(_archived_flags())), (
        "a cross-tenant delete archived the application anyway"
    )


def test_the_owning_tenant_can_still_archive_its_own_application(
    client: Caller, world: World
) -> None:
    """THE DIRECTION THAT MATTERS MOST HERE.

    Every assertion above is a refusal, and a route that refused EVERYTHING
    would satisfy all of them while the product did not work. This is the one
    that fails if isolation is tightened into an outage.
    """
    client.acting_for(world.a)
    archived = client.http.delete(f"{V1}/candidates/links/{world.a.link}")
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] is True


# ── The shape of the refusal ────────────────────────────────────────────────


def test_a_cross_tenant_refusal_is_never_a_403_on_any_of_these_routes(
    client: Caller, world: World
) -> None:
    """One assertion over every route at once, because the 403/404 choice is
    the kind of detail a new route gets wrong individually.

    A 403 says "this exists and you may not have it". Across a customer
    boundary that is a disclosure: it turns an id guess into a confirmed
    account, and confirming that a named person was assessed by a named
    company is the disclosure this product most has to avoid.
    """
    client.acting_for(world.a)
    theirs = world.b
    probes = (
        ("GET", f"{V1}/jobs/{theirs.job}", None),
        ("GET", f"{V2}/jobs/{theirs.job}", None),
        ("GET", f"{V2}/assessments/reports/links/{theirs.link}", None),
        ("GET", f"{V1}/candidates/{theirs.candidate}/profile", None),
        ("GET", f"{V1}/candidates/{theirs.candidate}/project-evidence", None),
        ("GET", f"{V1}/candidates/profiles/{theirs.profile}/resume-file", None),
        ("PATCH", f"{V1}/jobs/{theirs.job}", {"title": "x"}),
        ("POST", f"{V1}/jobs/{theirs.job}/close", {"reason": "filled"}),
        ("DELETE", f"{V1}/candidates/links/{theirs.link}", None),
    )
    statuses = {}
    for method, path, body in probes:
        response = client.http.request(
            method, path, json=body, follow_redirects=False
        )
        statuses[f"{method} {path}"] = response.status_code
    wrong = {key: value for key, value in statuses.items() if value != 404}
    assert not wrong, f"these did not answer 404 across the tenant boundary: {wrong}"


def test_an_id_that_exists_nowhere_answers_exactly_like_another_tenants_id(
    client: Caller, world: World
) -> None:
    """The property 404-not-403 is FOR.

    Identical status and identical body, so a caller cannot tell a real id in
    somebody else's tenant from an id nobody has ever issued. Comparing the
    bodies is the half a status-only test misses: two 404s whose details read
    "Job not found" and "Not authorized for this job" leak exactly the same
    bit the status code was chosen to hide.
    """
    client.acting_for(world.a)
    nowhere = uuid.uuid4()
    for path_template in (
        f"{V1}/jobs/{{}}",
        f"{V1}/candidates/{{}}/project-evidence",
    ):
        theirs_id = (
            world.b.job if "jobs" in path_template else world.b.candidate
        )
        other_tenant = client.http.get(path_template.format(theirs_id))
        imaginary = client.http.get(path_template.format(nowhere))
        assert other_tenant.status_code == imaginary.status_code == 404
        assert other_tenant.json() == imaginary.json(), (
            f"{path_template} distinguishes a real foreign id from a fake one"
        )
