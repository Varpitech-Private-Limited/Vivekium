"""Named world builders: the initial state a scenario stands on.

WHY A NAME AND NOT SQL IN THE SCENARIO
----------------------------------------
HARNESS.md section 2 states the rule this module exists to enforce: "A scenario
names a world builder, it does not inline SQL. Builders live in
`harness/world.py` and are shared. A scenario that builds its own state is a
scenario nobody can compose."

The practical consequence is that a world is a PRODUCT of smaller seeds rather
than a copy of the one above it. `single_tenant_job_with_applicant` is
`single_tenant_published_job` plus a candidate, and that composition is why
adding a column to `jobs` is one edit here rather than nine near-identical
inserts across a scenario directory.

WHY THE SEEDS ARE RAW SQL AND NOT THE ORM
-------------------------------------------
Every insert below runs under `superadmin_scope`, in explicit SQL, naming its
columns. That is the shape `tests/skills_fixtures.py` and
`tests/test_audit_single_insert_api.py` already use, and it is deliberate: the
ORM would apply defaults, validators and event hooks that the PRODUCT applies
on the write path, and a world that got its rows through the same machinery
being tested cannot establish a state the product would refuse to create. A
scenario that wants a job whose skills the team emptied, while the draft state
still says no draft was ever asked for (audit number 8), needs exactly that.

THE SCHEMA PREFLIGHT, AND WHY A MISSING RELATION IS `unavailable`
-------------------------------------------------------------------
Each builder declares the relations its world and its workload will touch.
`preflight` asks the catalog for them BEFORE anything is inserted, and a
missing one aborts the build with `SchemaMissing` rather than with whatever
`UndefinedTableError` the first insert happens to raise. The runner turns that
into `unavailable`, never `fail`, because a database that has not been migrated
far enough has told us nothing about the product: reporting it as a failure
would put a red scenario in front of somebody whose fix is `alembic upgrade
head`, and reporting it as a pass is the green-while-broken outcome the whole
harness exists to prevent.

NOTHING HERE IS CLEANED UP BY TRUNCATION
------------------------------------------
`teardown` deletes the tenant and lets the foreign keys cascade, plus the
handful of rows that are deliberately tenant-free (`candidates`, `profiles`,
which span tenants through the databank). Truncating tables would destroy the
rows every other agent working against this shared stack is relying on.

Provenance: docs/spec/HARNESS.md sections 2 and 3.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.db import superadmin_scope
from app.services.ppi import DEFAULT_REQUIRED_LEVEL

__all__ = [
    "SchemaMissing",
    "World",
    "WorldError",
    "build",
    "registered",
    "session_factory",
    "teardown",
]


class WorldError(RuntimeError):
    """A world cannot be built, with the builder and the reason named."""


class SchemaMissing(WorldError):
    """The database is missing a relation this world needs.

    A named subclass because the runner treats it differently from every other
    world failure: an un-migrated database is a statement about the ENVIRONMENT
    and produces `unavailable`, while a builder that raised for any other
    reason is a defect in the harness and produces a failure.
    """


#: The one instant every world is built at, so that a scenario asserting over a
#: posting window, a profile age or a notice period reads the same value on
#: every run. HARNESS.md section 6 requires a replay to reproduce "same clock",
#: and a world seeded from `now()` would move underneath one. Chosen to sit
#: comfortably inside a 30-day posting window opened by `_publish_window`.
ANCHOR = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)

#: One credit is 60 integer sub-units (claude.md, 2026-07-28). A world that
#: funds a tenant grants ten of them, which is enough for any scenario here to
#: spend without the balance reaching the gate that refuses new work.
CREDIT_SUBUNITS = 60
FUNDED_SUBUNITS = CREDIT_SUBUNITS * 10


@dataclass
class World:
    """Everything a scenario's workload and assertions need to name a row.

    `ids` is an open mapping rather than a fixed set of fields because the
    builders genuinely differ: a cross-tenant world has two of everything and a
    ranked-pool world has fifty links. A frozen shape would force every builder
    to carry every other builder's nulls, and a null in that position is
    indistinguishable from a row a builder forgot to seed.
    """

    name: str
    ids: dict[str, Any] = field(default_factory=dict)
    tenants: list[uuid.UUID] = field(default_factory=list)
    candidates: list[uuid.UUID] = field(default_factory=list)
    candidate_users: list[uuid.UUID] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def id(self, key: str) -> Any:
        """One named id, refusing rather than returning None for an unknown key.

        None would flow straight into a URL and produce a 404 that reads as a
        product refusal, which is the most misleading failure a harness can
        report: the route was never asked the question the scenario wrote down.
        """
        if key not in self.ids:
            known = ", ".join(sorted(self.ids)) or "nothing"
            raise WorldError(
                f"world {self.name!r} has no id named {key!r}; it carries {known}"
            )
        return self.ids[key]


def session_factory() -> async_sessionmaker[AsyncSession]:
    """A NullPool sessionmaker on the configured database.

    NullPool for the reason `tests/dashboard_world.py` already states: an
    asyncpg connection belongs to the event loop that opened it, and the
    harness runs the world builder, the application under test and the state
    reader on three different loops. A pooled connection handed across that
    boundary fails the next caller for something the previous one did.
    """
    return async_sessionmaker(
        create_async_engine(get_settings().database_url, poolclass=NullPool),
        expire_on_commit=False,
    )


async def preflight(session: AsyncSession, relations: Sequence[str]) -> None:
    """Refuse the build when the database lacks a relation this world needs."""
    if not relations:
        return
    found = set(
        (
            await session.execute(
                sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = ANY(:names)"
                ),
                {"names": list(relations)},
            )
        )
        .scalars()
        .all()
    )
    missing = sorted(set(relations) - found)
    if missing:
        raise SchemaMissing(
            f"the database is missing {', '.join(missing)}. This world cannot "
            "be built and nothing about the product has been measured. Migrate "
            "the test database (`alembic upgrade head`) and run it again."
        )


# ── Seeds, composed by the builders below ────────────────────────────────────


async def _seed_tenant(
    session: AsyncSession, world: World, *, key: str, is_demo: bool = False
) -> uuid.UUID:
    tenant = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO tenants (id, name, domain, spf_dkim_status, is_demo) "
            "VALUES (:id, :name, :domain, 'pending', :demo)"
        ),
        {
            "id": str(tenant),
            "name": f"Harness-{tenant.hex[:8]}",
            "domain": f"{tenant.hex[:12]}.harness.test",
            "demo": is_demo,
        },
    )
    world.ids[key] = tenant
    world.tenants.append(tenant)
    return tenant


async def _seed_staff(
    session: AsyncSession,
    world: World,
    *,
    key: str,
    tenant: uuid.UUID,
    role: str = "client",
    department: uuid.UUID | None = None,
) -> uuid.UUID:
    user = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO users (id, tenant_id, email, full_name, role, status, department_id) "
            "VALUES (:id, :tid, :email, :name, :role, 'active', :dept)"
        ),
        {
            "id": str(user),
            "tid": str(tenant),
            "email": f"{user.hex[:12]}@harness.test",
            "name": "Anita Rao",
            "role": role,
            "dept": str(department) if department else None,
        },
    )
    world.ids[key] = user
    return user


async def _seed_company_profile(
    session: AsyncSession, tenant: uuid.UUID, *, about: str | None
) -> None:
    """The Gate 1 row: `companies.about_company`, asked of the TABLE.

    `about` may be None deliberately. A scenario that asserts job creation is
    refused without a profile needs the row to exist and the section to be
    empty, because an absent `companies` row and a blank `about_company` are
    two different states and the gate reads the second.
    """
    await session.execute(
        sa.text(
            "INSERT INTO companies (id, tenant_id, about_company, work_life, "
            " benefits_text) VALUES (:id, :tid, :about, :life, :benefits)"
        ),
        {
            "id": str(uuid.uuid4()),
            "tid": str(tenant),
            "about": about,
            "life": "Four days in the office, one remote." if about else None,
            "benefits": "Health cover for the family." if about else None,
        },
    )


async def _fund(session: AsyncSession, tenant: uuid.UUID, *, subunits: int) -> None:
    await session.execute(
        sa.text(
            "INSERT INTO credit_ledger (id, tenant_id, event_type, "
            " subunits_delta, idempotency_key) "
            "VALUES (:id, :tid, 'grant', :delta, :key)"
        ),
        {
            "id": str(uuid.uuid4()),
            "tid": str(tenant),
            "delta": subunits,
            "key": f"harness-grant-{tenant.hex}",
        },
    )


async def _seed_job(
    session: AsyncSession,
    world: World,
    *,
    key: str,
    tenant: uuid.UUID,
    created_by: uuid.UUID | None,
    title: str = "Platform Engineer",
    published: bool = True,
    lifecycle_state: str = "DRAFT",
    assessment_status: str = "questions_pending_review",
    framework_generated_at: datetime | None = None,
    closed_at: datetime | None = None,
) -> uuid.UUID:
    job = uuid.uuid4()
    # The posting window is checked against the real clock, so a fixed ANCHOR date
    # expires: the published job 404'd for candidates once ANCHOR + 35 days passed.
    start = datetime.now(timezone.utc) - timedelta(days=2) if published else ANCHOR
    await session.execute(
        sa.text(
            "INSERT INTO jobs (id, tenant_id, title, jd_json, jd_markdown, "
            " status, lifecycle_state, assessment_status, assessment_grade, "
            " created_by, posting_start_date, ratified_at, "
            " framework_generated_at, closed_at, experience_min_years, "
            " experience_max_years) "
            "VALUES (:id, :tid, :title, CAST(:jd AS jsonb), :md, :status, "
            " :life, :astatus, 'non_managerial', :by, :start, :ratified, :fga, "
            " :closed, 3, 7)"
        ),
        {
            "id": str(job),
            "tid": str(tenant),
            "title": title,
            "jd": '{"skills": ["Python", "PostgreSQL"]}',
            "md": (
                "## The role\nOwns the payments platform and its two services "
                "end to end.\n\n## Skills\nPython, PostgreSQL."
            ),
            "status": "ratified" if published else "draft",
            "life": lifecycle_state,
            "astatus": assessment_status,
            "by": None if created_by is None else str(created_by),
            "start": start,
            # `ratified_at IS NOT NULL` IS THE PUBLISHED GATE, not the status
            # word beside it. `portal._published_job_or_404` reads this column
            # and nothing else, so a world that set `status = 'ratified'` and
            # left the stamp NULL produced a job the recruiter could see and
            # the candidate could not, and every candidate-side scenario 404'd
            # in a way that read as a product refusal.
            "ratified": start if published else None,
            "fga": framework_generated_at,
            "closed": closed_at,
        },
    )
    world.ids[key] = job
    return job


#: The saved SWOT a skills world stands on, one short sentence per section.
#: SAVED because the Skills step reads nothing else: Sutra drafts from the
#: saved SWOT, the draft sweep selects only jobs whose SWOT is saved, and the
#: publish gate asks for it. A world with skills and no saved SWOT would be a
#: state the product never produces, and the sweep scenario would pass for the
#: wrong reason (the sweep skips an unsaved SWOT before it ever reads a row).
_SWOT_SECTIONS: dict[str, str] = {
    "strengths": "The team ships the settlement platform every week and owns its on-call.",
    "weaknesses": "Nobody on the team has run a PostgreSQL major version upgrade.",
    "opportunities": "Two banks are asking for same-day settlement reporting.",
    "threats": "A regulator audit of how incidents were handled is due next quarter.",
}

#: The SWOT sentence each seeded skill was drafted from, when it was drafted
#: from one. VERBATIM from `_SWOT_SECTIONS`, because Sutra keeps a quote only
#: when it is a verbatim substring of the saved SWOT (`sutra._verbatim`); a
#: world carrying a quote the SWOT does not contain would be a citation the
#: product could never have written.
_SWOT_ORIGIN: dict[str, str] = {
    "PostgreSQL": _SWOT_SECTIONS["weaknesses"],
    "Incident response": _SWOT_SECTIONS["threats"],
}


async def _seed_saved_swot(
    session: AsyncSession,
    world: World,
    *,
    tenant: uuid.UUID,
    job: uuid.UUID,
    author: uuid.UUID,
) -> None:
    """A Job SWOT Analysis the team SAVED (`swot_analysis.is_saved` is True).

    `edited` with `human_edited` set, which is the plain saved case, at version
    one. The draft stamp on the job's skills names the same version, so a
    world never reads as "the SWOT moved on since the skills were drafted" and
    the redraft offer stays off unless a scenario moves it.
    """
    row = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO job_swot_analyses (id, tenant_id, job_id, status, "
            " strengths, weaknesses, opportunities, threats, generated_by, "
            " last_generated_at, human_edited, last_modified_by, "
            " last_modified_at, version) "
            "VALUES (:id, :tid, :job, 'edited', :s, :w, :o, :t, 'ai', :gen, "
            " TRUE, :by, :at, 1)"
        ),
        {
            "id": str(row),
            "tid": str(tenant),
            "job": str(job),
            "s": _SWOT_SECTIONS["strengths"],
            "w": _SWOT_SECTIONS["weaknesses"],
            "o": _SWOT_SECTIONS["opportunities"],
            "t": _SWOT_SECTIONS["threats"],
            "gen": ANCHOR - timedelta(hours=8),
            "by": str(author),
            "at": ANCHOR - timedelta(hours=7),
        },
    )
    world.ids["swot"] = row


async def _seed_skills(
    session: AsyncSession,
    world: World,
    *,
    key: str,
    tenant: uuid.UUID,
    job: uuid.UUID,
    names: Sequence[str],
    bucket: str,
    authored_by: str,
    saved: bool,
) -> None:
    """One bucket of a job's skills, exactly as the Skills step writes them.

    THE COLUMNS ARE THE ONES THE PRODUCT WRITES NOW, and no others. A skill is
    a name in a bucket with an author and an ordinal; Sutra's draft adds its
    provenance and, where it had one, the SWOT sentence it came from. The
    seven-stage columns the retired Tatva compiler filled (`dimension`,
    `weight`, `threshold_json`, `evidence_sources`, `assessment_method`) are
    deliberately NOT seeded: nothing on the Skills path writes them, so a
    world that did would be standing on a state no job created today can
    reach.

    `saved` adds what Save Skills writes: the evidence line (mirrored into
    `description`, the 2026-09-21 rule) and the per-bucket priority in
    `force_rank`. An unsaved draft carries neither, because Sutra's draft
    writes no evidence line; the context call at Save is what does.
    """
    ids: list[uuid.UUID] = []
    for ordinal, name in enumerate(names, start=1):
        row = uuid.uuid4()
        drafted = authored_by == "sutra"
        origin = _SWOT_ORIGIN.get(name) if drafted else None
        evidence = (
            f"Has shipped production work that depended on {name} and explained "
            "the decisions made along the way."
            if saved
            else None
        )
        await session.execute(
            sa.text(
                "INSERT INTO job_competencies (id, tenant_id, job_id, category, "
                " name, description, required_level, ordinal, is_active, "
                " authored_by, swot_origin, observable_evidence, force_rank, "
                " provenance_json) "
                "VALUES (:id, :tid, :job, :cat, :name, :eve, :level, :ord, TRUE, "
                " :author, :origin, :eve, :rank, CAST(:provenance AS jsonb))"
            ),
            {
                "id": str(row),
                "tid": str(tenant),
                "job": str(job),
                "cat": bucket,
                "name": name,
                "eve": evidence,
                # The ORM default, written out because this insert is raw SQL:
                # `required_level` has no server default and the Skills step
                # never sets one explicitly.
                "level": DEFAULT_REQUIRED_LEVEL,
                "ord": ordinal,
                "author": authored_by,
                "origin": origin,
                "rank": ordinal if saved else None,
                # What `skills.draft` records on a row it wrote, less the
                # model id and prompt version: no model ran for a world, and
                # naming one would claim a call that never happened.
                "provenance": (
                    json.dumps(
                        {
                            "generated_by": "sutra",
                            "source": "swot" if origin else "jd",
                            "swot_version": 1,
                        }
                    )
                    if drafted
                    else None
                ),
            },
        )
        ids.append(row)
    world.ids[key] = ids


async def _seed_candidate(
    session: AsyncSession,
    world: World,
    *,
    key: str,
    resume_text: str = "Eight years on Python services. Owned a PostgreSQL migration.",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A candidate, their portal user, and a main profile carrying a resume.

    `candidates` and `profiles` are deliberately TENANT-FREE (claude.md,
    2026-09-12: one employment history, per-tenant verification), so they are
    removed by id in `teardown` rather than by a tenant cascade.
    """
    candidate = uuid.uuid4()
    user = uuid.uuid4()
    profile = uuid.uuid4()
    email = f"{candidate.hex[:12]}@harness.test"
    await session.execute(
        sa.text(
            "INSERT INTO users (id, tenant_id, email, full_name, role, status) "
            "VALUES (:id, NULL, :email, 'Rohit Nair', 'candidate', 'active')"
        ),
        {"id": str(user), "email": email},
    )
    await session.execute(
        sa.text(
            "INSERT INTO candidates (id, tenant_id, user_id, full_name, email, "
            " city, consent_databank) "
            "VALUES (:id, NULL, :uid, 'Rohit Nair', :email, 'Bengaluru', TRUE)"
        ),
        {"id": str(candidate), "uid": str(user), "email": email},
    )
    # THE STORAGE METADATA IS NOT DECORATION, it is what makes the resume
    # REUSABLE. `portal._main_resume_profile` returns the main profile only
    # when `resume_public_id` is set, and `copy_resume_metadata` refuses a
    # source missing the url or the filename. A world that seeded resume TEXT
    # alone produced a candidate whose apply attempt answered 422 "No main
    # resume to reuse", which reads as a product defect and is a seed defect.
    # No bytes are written: nothing on the apply path reads the object, and an
    # upload here would make every scenario depend on MinIO being reachable.
    await session.execute(
        sa.text(
            "INSERT INTO profiles (id, candidate_id, resume_text, "
            " resume_storage_provider, resume_public_id, resume_url, "
            " resume_original_filename, resume_mime_type, resume_size_bytes, "
            " resume_uploaded_at, resume_sha256) "
            "VALUES (:id, :cid, :text, 's3', :public_id, :url, :filename, "
            " 'application/pdf', 84211, :at, :digest)"
        ),
        {
            "id": str(profile),
            "cid": str(candidate),
            "text": resume_text,
            "public_id": f"resumes/{profile.hex}.pdf",
            "url": f"s3://harness/resumes/{profile.hex}.pdf",
            "filename": "rohit-nair-resume.pdf",
            "at": ANCHOR - timedelta(days=30),
            "digest": profile.hex + candidate.hex,
        },
    )
    await session.execute(
        sa.text("UPDATE candidates SET main_profile_id = :pid WHERE id = :id"),
        {"pid": str(profile), "id": str(candidate)},
    )
    world.ids[key] = candidate
    world.ids[f"{key}_user"] = user
    world.ids[f"{key}_profile"] = profile
    world.candidates.append(candidate)
    world.candidate_users.append(user)
    return candidate, user, profile


async def _seed_link(
    session: AsyncSession,
    world: World,
    *,
    key: str,
    tenant: uuid.UUID,
    job: uuid.UUID,
    candidate: uuid.UUID,
    profile: uuid.UUID,
    status: str = "applied",
    source_type: str = "applied",
    pre_score: float | None = 74.0,
) -> uuid.UUID:
    """One link as a Yukti run leaves it: a pre-assessment score and the
    `scored` status when a score is given, `pending` otherwise. The ranked
    table orders on these (`yukti.ranking`); the retired `match_score` and
    `tier` are history columns the Vivekium release stopped writing, so a
    world that seeded them would rank on nothing."""
    link = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO job_candidate_links (id, tenant_id, job_id, "
            " candidate_id, profile_id, source, status, source_type, "
            " yukti_pre_score, yukti_status, yukti_profile_id, created_at) "
            # `source` is `LinkSource`, whose only values are `fresh` and
            # `databank`; `source_type` is the separate applied/sourced/databank
            # provenance of 2026-07-28. Seeding 'applied' into the first of them
            # raised `LookupError: 'applied' is not among the defined enum
            # values` inside SQLAlchemy's row processor, which is the same shape
            # as the defect `PipelineStatus`'s own docstring records.
            "VALUES (:id, :tid, :job, :cid, :pid, 'fresh', :status, :stype, "
            " :score, :ystatus, :ypid, :at)"
        ),
        {
            "id": str(link),
            "tid": str(tenant),
            "job": str(job),
            "cid": str(candidate),
            "pid": str(profile),
            "status": status,
            "stype": source_type,
            "score": pre_score,
            "ystatus": "pending" if pre_score is None else "scored",
            "ypid": None if pre_score is None else str(profile),
            "at": ANCHOR - timedelta(days=1),
        },
    )
    world.ids[key] = link
    return link


async def _seed_conversation(
    session: AsyncSession,
    world: World,
    *,
    key: str,
    tenant: uuid.UUID,
    job: uuid.UUID,
    link: uuid.UUID,
    invited: bool = True,
) -> uuid.UUID:
    conversation = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO assessment_conversations (id, tenant_id, job_id, "
            " job_candidate_link_id, grade, status, next_question_index, "
            " invitation_sent_at) "
            "VALUES (:id, :tid, :job, :link, 'non_managerial', 'active', 0, :inv)"
        ),
        {
            "id": str(conversation),
            "tid": str(tenant),
            "job": str(job),
            "link": str(link),
            "inv": ANCHOR if invited else None,
        },
    )
    world.ids[key] = conversation
    return conversation


# ── The builders ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Builder:
    """One named world, with the relations it needs stated up front.

    `relations` covers the world's own inserts AND what its workload will reach:
    a funded tenant means the billing path is walked, so `credit_lots` belongs
    here even though nothing below inserts into it. Declaring it on the WORLD
    rather than on the scenario is what keeps `scenario.py` parse only.
    """

    name: str
    relations: tuple[str, ...]
    seed: Callable[[AsyncSession, World, Mapping[str, Any]], Any]
    describes: str


def _override(overrides: Mapping[str, Any], key: str, default: Any) -> Any:
    return overrides.get(key, default)


async def _no_state(
    session: AsyncSession, world: World, overrides: Mapping[str, Any]
) -> None:
    """Nothing at all, for a scenario whose subject is not a row.

    Real and named rather than achieved by leaving `given.world` out, because
    the loader requires a world and a scenario that quietly declared none would
    be indistinguishable from one whose builder failed to run.
    """
    world.notes.append("no database state was seeded")


async def _funded_tenant(
    session: AsyncSession, world: World, overrides: Mapping[str, Any]
) -> None:
    tenant = await _seed_tenant(
        session, world, key="tenant", is_demo=bool(_override(overrides, "tenant.is_demo", False))
    )
    await _seed_staff(session, world, key="staff", tenant=tenant)
    await _seed_company_profile(
        session,
        tenant,
        about=_override(
            overrides,
            "company.about_company",
            "We run settlement infrastructure for twelve Indian banks.",
        ),
    )
    await _fund(
        session, tenant, subunits=int(_override(overrides, "credits.subunits", FUNDED_SUBUNITS))
    )


async def _published_job(
    session: AsyncSession, world: World, overrides: Mapping[str, Any]
) -> None:
    await _funded_tenant(session, world, overrides)
    await _seed_job(
        session,
        world,
        key="job",
        tenant=world.id("tenant"),
        created_by=world.id("staff"),
        lifecycle_state=str(_override(overrides, "job.lifecycle_state", "PUBLISHED")),
        assessment_status=str(
            _override(overrides, "job.assessment_status", "ready_for_candidates")
        ),
        framework_generated_at=ANCHOR - timedelta(hours=6),
    )


#: The default skills, per bucket. Every bucket is seeded because Save Skills
#: refuses a set with no Must-have or no Behavioural skill
#: (`skills.validate_for_save`), and a world seeding one bucket alone would
#: make every save scenario answer 422 about a question it was not asking.
#: Four, two and two sit inside the five-per-bucket limit with room to paste.
_DEFAULT_SKILLS: dict[str, tuple[str, ...]] = {
    "must_have": ("Python", "PostgreSQL", "Distributed systems", "Incident response"),
    "nice_to_have": ("Terraform", "Kafka"),
    "behavioural": ("Ownership under ambiguity", "Written communication"),
}


async def _seed_skill_set(
    session: AsyncSession,
    world: World,
    overrides: Mapping[str, Any],
    *,
    authored_by: str,
    saved: bool,
) -> None:
    """Every bucket of the world's job, keyed `<bucket>_skills` and `skills`.

    `skills` is EVERY row, because the step that empties the set has to empty
    all of it: the defect it pins lives in the state where not one active row
    is left in any bucket, and leaving one bucket behind would not reach it.
    """
    combined: list[uuid.UUID] = []
    for bucket, default in _DEFAULT_SKILLS.items():
        names = _override(overrides, f"skills.{bucket}", list(default))
        await _seed_skills(
            session,
            world,
            key=f"{bucket}_skills",
            tenant=world.id("tenant"),
            job=world.id("job"),
            names=[str(name) for name in names],
            bucket=bucket,
            authored_by=authored_by,
            saved=saved,
        )
        combined.extend(world.id(f"{bucket}_skills"))
    world.ids["skills"] = combined


async def _set_draft_state(
    session: AsyncSession, job: uuid.UUID, *, status: str
) -> None:
    """The job's skills draft state, as `skills.draft` leaves it.

    `drafted` carries the drafted stamp and the SWOT version it was drafted
    from; `not_started` carries neither, which is what a job whose skills the
    team typed before any draft was asked for looks like.
    """
    drafted = status == "drafted"
    await session.execute(
        sa.text(
            "UPDATE jobs SET skills_draft_status = :status, "
            " skills_drafted_at = :at, skills_drafted_swot_version = :version "
            "WHERE id = :id"
        ),
        {
            "status": status,
            "at": ANCHOR - timedelta(hours=6) if drafted else None,
            "version": 1 if drafted else None,
            "id": str(job),
        },
    )


async def _unpublished_job_with_saved_swot(
    session: AsyncSession, world: World, overrides: Mapping[str, Any]
) -> None:
    """A funded customer's job in DRAFT, with the SWOT the team saved.

    NOT published, because under the publish gate (the JD and saved skills,
    CONTRACT v10) a job cannot go live before its skills are saved, and the
    skills worlds built on this are the state before that save. The SWOT is
    saved because it is a state the product reaches; since v10 it is optional
    context for the draft and never a publication step.
    """
    await _funded_tenant(session, world, overrides)
    await _seed_job(
        session,
        world,
        key="job",
        tenant=world.id("tenant"),
        created_by=world.id("staff"),
        published=False,
        lifecycle_state=str(_override(overrides, "job.lifecycle_state", "DRAFT")),
        assessment_status=str(
            _override(overrides, "job.assessment_status", "questions_pending_review")
        ),
    )
    await _seed_saved_swot(
        session,
        world,
        tenant=world.id("tenant"),
        job=world.id("job"),
        author=world.id("staff"),
    )


async def _job_with_drafted_skills(
    session: AsyncSession, world: World, overrides: Mapping[str, Any]
) -> None:
    """Sutra drafted the skills from the JD, with the saved SWOT as context;
    nobody has saved them.

    The state every Skills-step scenario starts from: rows authored by Sutra,
    two of them carrying the SWOT sentence they came from, the draft stamped
    `drafted` against SWOT version one, no evidence lines and no saved stamp.
    """
    await _unpublished_job_with_saved_swot(session, world, overrides)
    await _seed_skill_set(session, world, overrides, authored_by="sutra", saved=False)
    await _set_draft_state(session, world.id("job"), status="drafted")


async def _job_with_team_written_skills(
    session: AsyncSession, world: World, overrides: Mapping[str, Any]
) -> None:
    """The team typed its own skills before any draft was asked for.

    Reachable, and the precise state audit number 8 lives in: skills added by
    hand before any draft was asked for, then the JD saved
    (`skills.after_jd_saved` finds rows and asks for no draft; a SWOT save
    never asks, CONTRACT v10), so the draft state is still `not_started` while
    rows exist.
    Once the team removes every one of them, the only thing that tells "the
    team emptied this" from "a draft never landed" is that soft-deleted rows
    are still in the table. The old sweep asked for ACTIVE rows and could not
    tell them apart.
    """
    await _unpublished_job_with_saved_swot(session, world, overrides)
    await _seed_skill_set(session, world, overrides, authored_by="human", saved=False)
    await _set_draft_state(session, world.id("job"), status="not_started")


async def _job_with_saved_skills(
    session: AsyncSession, world: World, overrides: Mapping[str, Any]
) -> None:
    """A published job whose skills were drafted, reviewed and SAVED.

    The ground the applicant, invitation and assessment worlds stand on. Saved
    means what `assessment_contract.skills_saved` asks of the table: the saved
    stamp AND the hidden context. The context names this world as its writer
    and carries an empty role summary, the honest shape migration 0118 wrote
    for a saved matrix: no model ran for a world, and a summary claiming one
    had would be template output presented as generation.
    """
    await _published_job(session, world, overrides)
    await _seed_saved_swot(
        session,
        world,
        tenant=world.id("tenant"),
        job=world.id("job"),
        author=world.id("staff"),
    )
    await _seed_skill_set(session, world, overrides, authored_by="sutra", saved=True)
    await _set_draft_state(session, world.id("job"), status="drafted")
    await session.execute(
        sa.text(
            "UPDATE jobs SET framework_approved_at = :at, finalized_at = :at, "
            " finalized_by = :by, "
            " assessment_context_json = CAST(:context AS jsonb) WHERE id = :id"
        ),
        {
            "at": ANCHOR - timedelta(hours=4),
            "by": str(world.id("staff")),
            "context": json.dumps({"role_summary": "", "generated_by": "harness_world"}),
            "id": str(world.id("job")),
        },
    )


async def _job_with_applicant(
    session: AsyncSession, world: World, overrides: Mapping[str, Any]
) -> None:
    await _job_with_saved_skills(session, world, overrides)
    candidate, _user, profile = await _seed_candidate(
        session,
        world,
        key="candidate",
        resume_text=str(
            _override(
                overrides,
                "candidate.resume_text",
                "Eight years on Python services. Owned a PostgreSQL migration.",
            )
        ),
    )
    await _seed_link(
        session,
        world,
        key="link",
        tenant=world.id("tenant"),
        job=world.id("job"),
        candidate=candidate,
        profile=profile,
        status=str(_override(overrides, "link.status", "applied")),
    )


async def _job_with_invited_candidate(
    session: AsyncSession, world: World, overrides: Mapping[str, Any]
) -> None:
    await _job_with_applicant(session, world, overrides)
    await _seed_conversation(
        session,
        world,
        key="conversation",
        tenant=world.id("tenant"),
        job=world.id("job"),
        link=world.id("link"),
    )


async def _job_with_unapplied_candidate(
    session: AsyncSession, world: World, overrides: Mapping[str, Any]
) -> None:
    """A published job and a candidate who has NOT applied to it yet.

    The apply path is the thing under test, so seeding the link would leave the
    scenario asserting over a row the harness wrote rather than one the product
    did.
    """
    await _job_with_saved_skills(session, world, overrides)
    await _seed_candidate(
        session,
        world,
        key="candidate",
        resume_text=str(
            _override(
                overrides,
                "candidate.resume_text",
                "Eight years on Python services. Owned a PostgreSQL migration.",
            )
        ),
    )


async def _golden_journey_ready(
    session: AsyncSession, world: World, overrides: Mapping[str, Any]
) -> None:
    """A funded customer with a Company Profile and two registered candidates
    holding a main resume (the one assessed, and a rival who only applies),
    and NOTHING else.

    The golden journey (`harness.golden_journey`) creates the job, the SWOT,
    the skills, the application and the assessment through the routes, so a
    row seeded here for any of them would be a row the product never wrote.
    What is seeded is what onboarding (a Provider act) and a candidate's own
    earlier resume upload leave behind.
    """
    await _funded_tenant(session, world, overrides)
    # LEADERSHIP INTELLIGENCE (2026-09-29): the department the job is created
    # in, and the two leaders who write for it, as the Super Admin's staff
    # invitations leave them. What they SAVE is written by the journey through
    # the routes, never here.
    department = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO company_departments (id, tenant_id, name, normalized_name) "
            "VALUES (:id, :tid, :name, :key)"
        ),
        {"id": str(department), "tid": str(world.id("tenant")), "name": "Settlements",
         "key": "settlements"},
    )
    world.ids["department"] = department
    await _seed_staff(session, world, key="ceo", tenant=world.id("tenant"), role="ceo")
    await _seed_staff(
        session, world, key="functional_head", tenant=world.id("tenant"),
        role="functional_head", department=department,
    )
    await _seed_candidate(
        session,
        world,
        key="candidate",
        resume_text=str(
            _override(
                overrides,
                "candidate.resume_text",
                "Settlement engineer, six years. Built the Python reconciliation "
                "service for a multi-bank payments switch and tuned its "
                "PostgreSQL ledger; owned the rollback when a settlement file "
                "broke in production.",
            )
        ),
    )
    # A SECOND APPLICANT, so the re-rank after the assessment is a change of
    # ORDER rather than a change of header over a table of one. They apply and
    # are never invited.
    await _seed_candidate(
        session,
        world,
        key="rival",
        resume_text=str(
            _override(
                overrides,
                "rival.resume_text",
                "Payments operations analyst. Ran the daily reconciliation "
                "reports for a card issuer.",
            )
        ),
    )


async def _seed_candidate_questions(
    session: AsyncSession,
    world: World,
    *,
    tenant: uuid.UUID,
    job: uuid.UUID,
    link: uuid.UUID,
    competencies: Sequence[uuid.UUID],
    count: int,
) -> None:
    """The one question stream, seeded rather than generated.

    Seeded directly for the reason `tests/test_conversation_flow.py` already
    records: calling the real generator would couple every completion and
    billing assertion to the matrix allocation, which is measured elsewhere,
    and with no model credential it would run the deterministic fallback whose
    question COUNT is a separate question from whether the charge fires once.

    `rubric_json` is an empty object rather than absent: a question whose
    rubric was written with it is the whole reason a generated question is
    sound (claude.md, 2026-08-06), and a NULL here would put the scorer on the
    unanswered path for a reason the scenario did not ask about.
    """
    ids: list[uuid.UUID] = []
    for ordinal in range(count):
        question = uuid.uuid4()
        await session.execute(
            sa.text(
                "INSERT INTO candidate_questions (id, tenant_id, job_id, "
                " job_candidate_link_id, competency_id, ordinal, prompt, "
                " rubric_json, generated_at, question_type) "
                "VALUES (:id, :tid, :job, :link, :comp, :ord, :prompt, "
                " '{}'::jsonb, :at, 'short_answer')"
            ),
            {
                "id": str(question),
                "tid": str(tenant),
                "job": str(job),
                "link": str(link),
                "comp": str(competencies[ordinal % len(competencies)]),
                "ord": ordinal,
                "prompt": (
                    f"Walk through a time you owned the {ordinal + 1}th hardest "
                    "part of a platform migration."
                ),
                "at": ANCHOR - timedelta(hours=1),
            },
        )
        ids.append(question)
    world.ids["questions"] = ids


async def _start_under_proctoring(
    session: AsyncSession,
    world: World,
    *,
    conversation: uuid.UUID,
    link: uuid.UUID,
    candidate: uuid.UUID,
) -> None:
    """Start the conversation and write the live proctoring session its gate reads.

    THE PROCTORING ROW IS NOT OPTIONAL AND IS NOT A CONVENIENCE. Monitoring is
    mandatory, and `proctoring.gate.require_active` runs FIRST in `respond` and
    in the coding Run route, so a world without it would make every turn and
    every Run refused and the scenario would be measuring the gate. Seeding the
    row the consent screen would have written is what keeps the path under
    test the path a real candidate takes; stubbing the gate would make the
    scenario pass on a route nobody can reach.
    """
    await session.execute(
        sa.text(
            "UPDATE assessment_conversations SET started_at = :at WHERE id = :id"
        ),
        {"at": ANCHOR, "id": str(conversation)},
    )
    await session.execute(
        sa.text(
            "INSERT INTO proctoring_sessions (id, tenant_id, conversation_id, "
            " job_candidate_link_id, candidate_id, job_id, consented_at, "
            " started_at, outcome) "
            "VALUES (:id, :tid, :conv, :link, :cid, :job, :at, :at, 'active')"
        ),
        {
            "id": str(uuid.uuid4()),
            "tid": str(world.id("tenant")),
            "conv": str(conversation),
            "link": str(link),
            "cid": str(candidate),
            "job": str(world.id("job")),
            "at": ANCHOR,
        },
    )


async def _assessment_in_progress(
    session: AsyncSession, world: World, overrides: Mapping[str, Any]
) -> None:
    """An invited candidate, their questions, and a live proctoring session.

    The proctoring row is seeded by `_start_under_proctoring`, whose docstring
    says why it is not optional: without it every turn would answer 403 and
    the scenario would be measuring the gate rather than the billing.
    """
    await _job_with_saved_skills(session, world, overrides)
    candidate, _user, profile = await _seed_candidate(session, world, key="candidate")
    link = await _seed_link(
        session,
        world,
        key="link",
        tenant=world.id("tenant"),
        job=world.id("job"),
        candidate=candidate,
        profile=profile,
        status="assessment_invited",
    )
    count = int(_override(overrides, "questions.count", 2))
    if count < 1:
        raise WorldError(
            f"world 'assessment_in_progress' was asked for {count} questions. "
            "A conversation with none completes on its first turn, which would "
            "make the completion assertion true for the wrong reason."
        )
    await _seed_candidate_questions(
        session,
        world,
        tenant=world.id("tenant"),
        job=world.id("job"),
        link=link,
        competencies=world.id("must_have_skills"),
        count=count,
    )
    conversation = await _seed_conversation(
        session,
        world,
        key="conversation",
        tenant=world.id("tenant"),
        job=world.id("job"),
        link=link,
    )
    await _start_under_proctoring(
        session, world, conversation=conversation, link=link, candidate=candidate
    )


#: The answer key of the world's coding question, as SENTINELS: strings that
#: occur nowhere else in the product, so finding one anywhere a client, a log
#: line, a prompt or a stored row can reach is proof the key travelled there.
#: Lower case and free of digits and punctuation, so neither a normaliser nor
#: the number guard can rewrite one out of existence and make the sweep pass.
CODING_HIDDEN_STDIN = "zqharnesshiddenstdin"
CODING_HIDDEN_OUT = "zqharnesshiddenexpected"
CODING_REFERENCE = "zqharnessreferencesolution"
CODING_APPROACH = "zqharnessapproachnotes"

#: How many hidden tests the key carries. More than one, so an outcome
#: sentence has to spell a count out, which the scenarios also read.
CODING_HIDDEN_TESTS = 3


def _coding_draft() -> Any:
    """The v2 coding question and its answer key, built with the product's types.

    Built through `coding_generation.CodingQuestionDraft`, `keys.HiddenTest`,
    `keys.hidden_digest` and `payload.LanguageLimits.of`, never as a literal
    payload: the database refuses a coding payload that carries a key, and a
    key whose digest does not match its tests is refused when it is loaded, so
    a hand-written shape would be one of those two refusals waiting to happen.
    """
    from app.services.assessment_formats import coding_generation  # noqa: PLC0415
    from app.services.code_execution import limits as execution_limits  # noqa: PLC0415
    from app.services.coding_assessment import keys as coding_keys  # noqa: PLC0415
    from app.services.coding_assessment import payload as coding_payload  # noqa: PLC0415

    hidden = tuple(
        coding_keys.HiddenTest(
            key=f"h{index}",
            stdin=f"{CODING_HIDDEN_STDIN}{'x' * index}\n",
            expected_stdout=f"{CODING_HIDDEN_OUT}{'x' * index}\n",
        )
        for index in range(1, CODING_HIDDEN_TESTS + 1)
    )
    title = "Busiest failing endpoint"
    return coding_generation.CodingQuestionDraft(
        title=title,
        statement=(
            "Read a list of request log lines and print the path with the most "
            "server errors, or NONE when there are none."
        ),
        payload={
            "payload_version": coding_payload.PAYLOAD_VERSION,
            "title": title,
            "io": "stdin_stdout",
            "input_format": "A count, then that many lines of a path and a status.",
            "output_format": "One line: the path, or NONE.",
            "constraints": "At most ten thousand lines.",
            "languages": ["python"],
            "starter_code": {"python": "import sys\nprint()\n"},
            "visible_tests": [
                {"id": "v1", "stdin": "0\n", "expected_stdout": "NONE\n", "explanation": ""},
                {"id": "v2", "stdin": "1\n/a 500\n", "expected_stdout": "/a\n", "explanation": ""},
            ],
            "limits": {
                "python": coding_payload.LanguageLimits.of(
                    execution_limits.for_language("python")
                ).model_dump()
            },
        },
        rubric={
            "criteria": list(coding_payload.CODING_REVIEW_CRITERIA),
            "payload_version": coding_payload.PAYLOAD_VERSION,
        },
        reference_language="python",
        validation={
            "provider": "harness_world",
            "hidden_digest": coding_keys.hidden_digest(hidden),
        },
        generated_at=ANCHOR - timedelta(hours=1),
        hidden_tests=hidden,
        reference_source=f"# {CODING_REFERENCE}\nimport sys\nprint()\n",
        expected_approach=f"Count server errors per path. {CODING_APPROACH}",
    )


async def _coding_question_open(
    session: AsyncSession, world: World, overrides: Mapping[str, Any]
) -> None:
    """An invited candidate whose OPEN question is an executed (v2) coding question.

    One question, at ordinal zero, so it is the conversation's current base
    question and the Run route's open-question fence lets a Run through. The
    question and its answer key are written by the PRODUCT's
    `coding_generation.persist_coding_question`, in this world's transaction,
    rather than by raw SQL like the other seeds. The raw-SQL rule exists so a
    world can establish a state the product would refuse to create; this one
    must be exactly the state the product creates, because the key is sealed
    by a digest the read path recomputes, and a key written any other way is
    a key the product refuses to grade with.

    `world.ids` carries the question id, the visible and hidden inputs (a
    workload step scripts the echoing program with them) and the sentinels
    the exfiltration probes sweep for.
    """
    from app.models.assessment import CandidateQuestion  # noqa: PLC0415
    from app.services.assessment_formats import coding_generation  # noqa: PLC0415

    await _job_with_saved_skills(session, world, overrides)
    candidate, _user, profile = await _seed_candidate(session, world, key="candidate")
    link = await _seed_link(
        session,
        world,
        key="link",
        tenant=world.id("tenant"),
        job=world.id("job"),
        candidate=candidate,
        profile=profile,
        status="assessment_invited",
    )
    draft = _coding_draft()
    question = CandidateQuestion(
        tenant_id=world.id("tenant"),
        job_id=world.id("job"),
        job_candidate_link_id=link,
        competency_id=world.id("must_have_skills")[0],
        ordinal=0,
        prompt=draft.statement,
    )
    await coding_generation.persist_coding_question(session, question, draft)
    conversation = await _seed_conversation(
        session,
        world,
        key="conversation",
        tenant=world.id("tenant"),
        job=world.id("job"),
        link=link,
    )
    await _start_under_proctoring(
        session, world, conversation=conversation, link=link, candidate=candidate
    )
    # The coding question is ON SCREEN: the server's turn clock has its turn
    # open, with the coding allocation, as `respond` leaves it after opening a
    # turn. The Run route reads that clock (p4-4c hunk 2, stage 2 integration)
    # and refuses a Run on a turn with no clock, so a world without it would
    # measure the clock refusal instead of the path under test. `now()` rather
    # than the world's fixed anchor, because the clock is read against the
    # real time of the request.
    from app.core.config import get_settings  # noqa: PLC0415

    await session.execute(
        sa.text(
            "UPDATE assessment_conversations SET turn_seq = 1, prompt_shown_at = now(), "
            "turn_allocation_seconds = :allocation WHERE id = :id"
        ),
        {
            "allocation": get_settings().assessment_time_coding_seconds,
            "id": str(conversation),
        },
    )
    world.ids["coding_question"] = question.id
    world.ids["coding_visible_stdins"] = [
        test["stdin"] for test in draft.payload["visible_tests"]
    ]
    world.ids["coding_hidden_stdins"] = [test.stdin for test in draft.hidden_tests]
    world.ids["coding_key_sentinels"] = [
        CODING_HIDDEN_STDIN,
        CODING_HIDDEN_OUT,
        CODING_REFERENCE,
    ]
    world.ids["coding_approach_sentinel"] = CODING_APPROACH


async def _two_tenants(
    session: AsyncSession, world: World, overrides: Mapping[str, Any]
) -> None:
    """Two customers, one job each, and a staff principal in the first only.

    The whole point of the pair is that the second tenant's job id is a real,
    live, readable row for somebody else. A cross-tenant assertion against an
    id that does not exist anywhere would pass against a product with no
    isolation at all, because both answers are 404.
    """
    for suffix in ("a", "b"):
        tenant = await _seed_tenant(session, world, key=f"tenant_{suffix}")
        staff = await _seed_staff(session, world, key=f"staff_{suffix}", tenant=tenant)
        await _seed_company_profile(
            session, tenant, about="We build logistics software for two ports."
        )
        await _fund(session, tenant, subunits=FUNDED_SUBUNITS)
        await _seed_job(
            session,
            world,
            key=f"job_{suffix}",
            tenant=tenant,
            created_by=staff,
            title=f"Platform Engineer ({suffix.upper()})",
            lifecycle_state="PUBLISHED",
            assessment_status="ready_for_candidates",
        )
    # The names the scenarios read. `tenant` is the one the caller acts as, so
    # every generic probe resolves to tenant A without each scenario spelling
    # out which half of the pair it means.
    world.ids["tenant"] = world.id("tenant_a")
    world.ids["staff"] = world.id("staff_a")
    world.ids["job"] = world.id("job_a")


async def _ranked_pool(
    session: AsyncSession, world: World, overrides: Mapping[str, Any]
) -> None:
    """One job with many applicants, for the ordering and latency scenarios."""
    await _job_with_saved_skills(session, world, overrides)
    size = int(_override(overrides, "pool.size", 40))
    if size < 1:
        raise WorldError(
            f"world 'ranked_pool' was asked for {size} candidates. A pool of "
            "none would make every ordering assertion vacuously true."
        )
    links: list[uuid.UUID] = []
    for index in range(size):
        candidate, _user, profile = await _seed_candidate(
            session,
            world,
            key=f"pool_candidate_{index}",
            resume_text=(
                f"{4 + index % 11} years on Python services and data pipelines."
            ),
        )
        link = await _seed_link(
            session,
            world,
            key=f"pool_link_{index}",
            tenant=world.id("tenant"),
            job=world.id("job"),
            candidate=candidate,
            profile=profile,
            # Spread deterministically across the four bands so the ordering
            # assertion has something to order. `index % 37` rather than a
            # random draw: `seed_mock_data.py` makes the same choice, and for
            # the same reason.
            pre_score=float(40 + (index * 37) % 55),
        )
        links.append(link)
    world.ids["pool_links"] = links
    world.ids["link"] = links[0]
    world.ids["candidate"] = world.id("pool_candidate_0")
    world.ids["candidate_user"] = world.id("pool_candidate_0_user")


_CORE = (
    "tenants",
    "users",
    "companies",
    "credit_ledger",
    "role_permissions",
)
_JOB = _CORE + ("jobs", "job_competencies")
#: The Skills step reads the saved SWOT (drafting, the sweep, the publish gate)
#: and asks the snapshot table whether the skills are locked on every read and
#: every write, so a skills world declares both.
_SKILLS = _JOB + ("job_swot_analyses", "job_skill_snapshots")
_APPLIED = _SKILLS + ("candidates", "profiles", "job_candidate_links")

#: Walked by any workload that reaches the credit gate or charges a credit.
#: Declared on the world rather than on the scenario: `scenario.py` is parse
#: only and must stay runnable with no database behind it.
_BILLING = ("credit_lots", "credit_lot_draws")

_BUILDERS: dict[str, Builder] = {
    "no_state": Builder(
        "no_state",
        (),
        _no_state,
        "nothing seeded; for a scenario whose subject is not a row",
    ),
    "funded_tenant": Builder(
        "funded_tenant",
        _CORE + _BILLING,
        _funded_tenant,
        "one customer with a company profile and a funded credit pool",
    ),
    "published_job": Builder(
        "published_job",
        _JOB,
        _published_job,
        "a funded customer with one published job and no skills rows",
    ),
    "job_with_drafted_skills": Builder(
        "job_with_drafted_skills",
        _SKILLS + ("audit_log",),
        _job_with_drafted_skills,
        "a draft job with a saved SWOT and the skills Sutra drafted from it, "
        "not yet saved: four Must-have, two Nice-to-have, two Behavioural",
    ),
    "job_with_team_written_skills": Builder(
        "job_with_team_written_skills",
        _SKILLS,
        _job_with_team_written_skills,
        "a draft job with a saved SWOT and skills the team typed before any "
        "draft was asked for, so the draft state is still not started",
    ),
    "job_with_saved_skills": Builder(
        "job_with_saved_skills",
        _SKILLS,
        _job_with_saved_skills,
        "a published job whose drafted skills were reviewed and saved, ready "
        "for candidates",
    ),
    "job_with_applicant": Builder(
        "job_with_applicant",
        _APPLIED + ("pipeline_status", "candidate_updates", "audit_log"),
        _job_with_applicant,
        "a published job with one applicant sitting at `applied`",
    ),
    "job_with_unapplied_candidate": Builder(
        "job_with_unapplied_candidate",
        _APPLIED,
        _job_with_unapplied_candidate,
        "a published job and a registered candidate who has not applied",
    ),
    "golden_journey_ready": Builder(
        "golden_journey_ready",
        # The world inserts little; the JOURNEY reaches every one of these, and
        # a stack missing one is `unavailable` before anything runs.
        _APPLIED
        + (
            "job_swot_analyses",
            "job_skill_snapshots",
            "assessment_conversations",
            "assessment_consents",
            "candidate_questions",
            "assessment_answers",
            "proctoring_sessions",
            "proctoring_reports",
            "voice_answers",
            "coding_runs",
            "coding_submissions",
            "evaluations",
            "functional_skills_reports",
            "report_dimensions",
        )
        + _BILLING,
        _golden_journey_ready,
        "a funded customer with a Company Profile and two registered candidates "
        "with a main resume; the golden journey creates everything else",
    ),
    "job_with_invited_candidate": Builder(
        "job_with_invited_candidate",
        _APPLIED + ("assessment_conversations",) + _BILLING,
        _job_with_invited_candidate,
        "an applicant the hiring team has invited to the assessment",
    ),
    "assessment_in_progress": Builder(
        "assessment_in_progress",
        _APPLIED
        + (
            "assessment_conversations",
            "assessment_messages",
            "candidate_questions",
            "proctoring_sessions",
            "telemetry_events",
        )
        + _BILLING,
        _assessment_in_progress,
        "an invited candidate mid-assessment, with questions and a live "
        "proctoring session",
    ),
    "coding_question_open": Builder(
        "coding_question_open",
        _APPLIED
        + (
            "assessment_conversations",
            "assessment_messages",
            "assessment_answers",
            "candidate_questions",
            "proctoring_sessions",
            "coding_question_keys",
            "coding_runs",
            "coding_submissions",
        ),
        _coding_question_open,
        "an invited candidate whose open question is an executed coding "
        "question, with its answer key sealed and a live proctoring session",
    ),
    "two_tenants_one_job_each": Builder(
        "two_tenants_one_job_each",
        _JOB,
        _two_tenants,
        "two customers with a live job each, for a cross-tenant read",
    ),
    "ranked_pool": Builder(
        "ranked_pool",
        _APPLIED,
        _ranked_pool,
        "one job with a pool of applicants spread across the four bands",
    ),
}


def registered() -> tuple[str, ...]:
    """Every world name, for a scenario author and for the error above."""
    return tuple(sorted(_BUILDERS))


def describe(name: str) -> str:
    builder = _BUILDERS.get(name)
    if builder is None:
        raise WorldError(
            f"unknown world {name!r}; the registry holds {', '.join(registered())}"
        )
    return builder.describes


async def build(
    name: str,
    overrides: Mapping[str, Any],
    *,
    sessions: async_sessionmaker[AsyncSession],
) -> World:
    """Resolve a world name to real database state.

    One transaction, committed, because the workload reads it back over HTTP
    through a DIFFERENT connection and an uncommitted seed would be invisible
    to it. That is the same reason the state reader is a second connection: the
    harness never lets one session's uncommitted view stand in for what the
    database actually holds.
    """
    builder = _BUILDERS.get(name)
    if builder is None:
        raise WorldError(
            f"unknown world {name!r}; the registry holds {', '.join(registered())}"
        )
    unknown = sorted(
        key for key in overrides if not isinstance(key, str) or not key.strip()
    )
    if unknown:
        raise WorldError(f"world {name!r}: overrides carry an unusable key {unknown}")
    world = World(name=name)
    async with sessions() as session:
        async with session.begin():
            async with superadmin_scope(session):
                await preflight(session, builder.relations)
                await builder.seed(session, world, overrides)
    return world


async def teardown(
    world: World, *, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Remove everything this world created, and nothing else.

    Deleting by id rather than truncating, because this stack is shared: a
    truncation would take out the rows every other suite running against it is
    standing on. The tenant cascade covers jobs, links, competencies and audit
    rows; `candidates` and `profiles` are tenant-free by design and are removed
    explicitly.

    The ORDER is load bearing. `profiles.candidate_id` cascades from
    `candidates`, so the candidate goes first and takes its profiles with it;
    the candidate's portal `users` row carries no tenant and so survives the
    tenant cascade, and it is removed LAST because the candidate row references
    it.
    """
    async with sessions() as session:
        async with session.begin():
            async with superadmin_scope(session):
                for tenant in world.tenants:
                    await session.execute(
                        sa.text("DELETE FROM tenants WHERE id = :id"),
                        {"id": str(tenant)},
                    )
                if world.candidates:
                    await session.execute(
                        sa.text("DELETE FROM candidates WHERE id = ANY(:ids)"),
                        {"ids": [str(item) for item in world.candidates]},
                    )
                if world.candidate_users:
                    await session.execute(
                        sa.text("DELETE FROM users WHERE id = ANY(:ids)"),
                        {"ids": [str(item) for item in world.candidate_users]},
                    )
