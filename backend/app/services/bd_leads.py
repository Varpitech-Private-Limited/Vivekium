"""Business Development Portal business logic.

Everything here is deliberately callable WITHOUT a FastAPI request: the query
builders are pure, the mutators take a row and a clock, and only the two
functions that genuinely need the database take a session. That is what makes
the boundary rules (search runs in SQL, agreement promotes a lead to a tenant,
a company name containing a comma survives the CSV) testable directly.

THE FOUR THINGS THAT MUST NOT DRIFT
-----------------------------------
1. Search, filtering and pagination run in SQL, BEFORE the page is cut. This is
   the same rule the Provider Portal follows: filter a fetched page in the
   browser and "3 of 108 match" starts depending on which page was loaded.
2. Ticking a progress box stamps its timestamp the FIRST time only. Unticking
   clears the flag and keeps the stamp.
3. `agreement = true` promotes the lead to a CUSTOMER, and a customer IS a
   `tenants` row (CLAUDE.md hard rule). Un-setting it never deletes the tenant.
4. The CSV is written by `csv.writer`, never by string concatenation, so a
   company called "Acme, Inc." does not silently become two columns.
"""
from __future__ import annotations

import csv
import io
import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Iterator, Sequence

from sqlalchemy import Select, or_, select, text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bd import (
    CHANNEL_SOCIAL,
    PROGRESS_FLAGS,
    TENANT_PROSPECT,
    BDLead,
)
from app.models.tenant import CUSTOMER_ARCHIVED, Tenant
from app.services import employer_pages
from app.schemas.bd import CSV_COLUMNS, BDCustomerOut, JobCardOut
from app.services.reach_embeddings import (
    ReachEmbeddingError,
    embed_passages,
    embed_query,
)

logger = logging.getLogger(__name__)

#: How many "similar to our customers" cards the segment returns. A dashboard
#: panel, not a search results page.
SIMILAR_LIMIT = 12
SIMILARITY_THRESHOLD = 0.82

_ROLE_ALIASES = {
    "front end": "frontend",
    "back end": "backend",
    "dev ops": "devops",
    "machine-learning": "machine learning",
    "ml engineer": "machine learning engineer",
}
_GENERIC_ROLE_TOKENS = {
    "developer", "engineer", "specialist", "professional", "senior", "junior",
    "lead", "manager", "software",
}


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


# ── Lead listing (all filtering in SQL) ──────────────────────────────────────

def lead_predicates(
    *,
    channel: str | None = None,
    social_source: str | None = None,
    search: str | None = None,
    agreement: bool | None = None,
    include_archived: bool = False,
    owner_user_id: uuid.UUID | None = None,
    agreement_is_set: bool = False,
) -> list:
    """The WHERE clauses for the lead list, as SQLAlchemy expressions.

    `agreement_is_set` distinguishes "the caller asked for undecided leads"
    (`agreement=None` WAS sent) from "the caller did not filter on agreement"
    (`agreement` absent). Without it, `?agreement=` could not express "show me
    the leads nobody has decided yet", which is the BD team's working queue.

    `social_source` narrows to one platform. It exists because Personal Reach
    and Social Reach merged into one BD Reach screen on 2026-08-09: with one
    table in front of the rep, "show me only the LinkedIn ones" has to be a
    SQL filter for the same reason every other filter here is one. Narrowing a
    fetched page in the browser would make the result count depend on which
    page happened to be loaded.
    """
    predicates = []
    if channel:
        predicates.append(BDLead.channel == channel)
    if social_source:
        predicates.append(BDLead.social_source == social_source)
    if not include_archived:
        predicates.append(BDLead.archived_at.is_(None))
    if owner_user_id is not None:
        predicates.append(BDLead.owner_user_id == owner_user_id)
    if agreement_is_set:
        predicates.append(
            BDLead.agreement.is_(None) if agreement is None
            else BDLead.agreement.is_(agreement)
        )

    needle = (search or "").strip()
    if needle:
        pattern = f"%{needle}%"
        predicates.append(
            or_(
                BDLead.company_name.ilike(pattern),
                BDLead.industry.ilike(pattern),
                BDLead.location.ilike(pattern),
                BDLead.contact_name.ilike(pattern),
                BDLead.contact_email.ilike(pattern),
                BDLead.contact_phone.ilike(pattern),
                BDLead.website.ilike(pattern),
            )
        )
    return predicates


def lead_list_query(predicates: Sequence, page: int, page_size: int) -> Select:
    """One page of leads.

    The order must be TOTAL. `created_at` alone is not: two leads entered in
    the same second could swap between page 1 and page 2, so one duplicates and
    another is never seen. The trailing `id` is what closes that.
    """
    return (
        select(BDLead)
        .where(*predicates)
        .order_by(BDLead.created_at.desc(), BDLead.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )


# ── Progress checkboxes ──────────────────────────────────────────────────────

def apply_progress(
    lead: object, changes: dict[str, bool], now: datetime | None = None
) -> list[str]:
    """Tick or untick boxes. Returns the flags that actually changed.

    Ticking stamps `<flag>_at` the FIRST time only. Unticking clears the flag
    and KEEPS the stamp: a rep correcting a mis-click should not erase the fact
    that the company was contacted, and re-ticking should not pretend the
    contact happened today.
    """
    at = _now(now)
    changed: list[str] = []
    for flag in PROGRESS_FLAGS:
        if flag not in changes:
            continue
        value = bool(changes[flag])
        if bool(getattr(lead, flag, False)) == value:
            continue
        setattr(lead, flag, value)
        if value and getattr(lead, f"{flag}_at", None) is None:
            setattr(lead, f"{flag}_at", at)
        changed.append(flag)
    return changed


# ── Promotion: a signed lead becomes a customer ──────────────────────────────

_DOMAIN_SAFE = re.compile(r"[^a-z0-9.-]+")

#: Reserved suffix for a tenant that exists only because a lead signed. It
#: keeps a synthesized key obviously synthetic, so nobody mistakes it for a
#: real mail domain (the Owner console replaces it at onboarding).
PROSPECT_DOMAIN_SUFFIX = "prospect.pickready.local"


def derive_tenant_domain(
    *, website: str | None, contact_email: str | None, lead_id: uuid.UUID
) -> str:
    """`tenants.domain` is NOT NULL and UNIQUE, so a promoted lead needs one.

    Preference order: the company website host, then the contact email host,
    then a synthetic key derived from the lead id. Pure, so the fallback chain
    is unit-testable without a database.
    """
    candidate = (website or "").strip().lower()
    for prefix in ("https://", "http://"):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix):]
    candidate = candidate.split("/")[0].strip()
    if candidate.startswith("www."):
        candidate = candidate[4:]

    if not candidate and contact_email and "@" in contact_email:
        candidate = contact_email.rsplit("@", 1)[1].strip().lower()

    candidate = _DOMAIN_SAFE.sub("", candidate).strip(".-")
    if not candidate or "." not in candidate:
        return f"lead-{str(lead_id)[:8]}.{PROSPECT_DOMAIN_SUFFIX}"
    return candidate[:255]


async def _unique_domain(session: AsyncSession, base: str, lead_id: uuid.UUID) -> str:
    """Two leads for the same company must not collide on the UNIQUE key.

    The suffix is the lead id rather than a counter: a counter needs a read to
    decide the next value and two concurrent promotions would pick the same one.
    """
    taken = (
        await session.execute(select(Tenant.id).where(Tenant.domain == base))
    ).first()
    if taken is None:
        return base
    short = str(lead_id)[:8]
    host, _, tld = base.partition(".")
    return f"{host}-{short}.{tld}" if tld else f"{host}-{short}"


async def set_agreement(
    session: AsyncSession,
    lead: BDLead,
    agreement: bool | None,
    *,
    now: datetime | None = None,
) -> tuple[str, Tenant | None]:
    """Apply the yes / no / undecided decision and run the promotion.

    Returns `(outcome, tenant)` where outcome is one of `unchanged`,
    `promoted`, `repromoted`, `demoted`, `set`.

    PROMOTION (`true`): creates the `tenants` row this lead becomes, in the
    `prospect` status so it does not appear in the Provider Portal's live
    customer list, and links it. A lead that was promoted before reuses and
    unarchives its original tenant rather than minting a duplicate company.

    DEMOTION (`false` or `null`): unlinks and ARCHIVES. It never deletes the
    tenant, because by then the customer may already have users, jobs and
    applications hanging off it, and because archiving is the reversible
    counterpart the rest of the platform already uses.
    """
    at = _now(now)
    if lead.agreement is agreement:
        return "unchanged", None

    previous = lead.agreement
    lead.agreement = agreement
    lead.agreement_at = at if agreement is not None else None

    if agreement is True:
        if lead.promoted_tenant_id is not None:
            tenant = await session.get(Tenant, lead.promoted_tenant_id)
            if tenant is not None:
                tenant.status = TENANT_PROSPECT
                tenant.archived_at = None
                lead.tenant_id = tenant.id
                await session.flush()
                return "repromoted", tenant
        tenant = await _create_tenant_from_lead(session, lead)
        lead.tenant_id = tenant.id
        lead.promoted_tenant_id = tenant.id
        await session.flush()
        return "promoted", tenant

    # false or None: unlink, and archive the customer the lead had created.
    tenant: Tenant | None = None
    if lead.tenant_id is not None:
        tenant = await session.get(Tenant, lead.tenant_id)
        if tenant is not None and tenant.status != CUSTOMER_ARCHIVED:
            tenant.status = CUSTOMER_ARCHIVED
            tenant.archived_at = at
        lead.tenant_id = None
    await session.flush()
    return ("demoted" if previous is True else "set"), tenant


async def _create_tenant_from_lead(session: AsyncSession, lead: BDLead) -> Tenant:
    base = derive_tenant_domain(
        website=lead.website, contact_email=lead.contact_email, lead_id=lead.id
    )
    tenant = Tenant(
        id=uuid.uuid4(),
        name=lead.company_name,
        domain=await _unique_domain(session, base, lead.id),
        industry=lead.industry,
        website_domain=lead.website,
        status=TENANT_PROSPECT,
        notes=(
            "Created from a Business Development lead. Not onboarded yet."
            if not lead.notes
            else f"Created from a Business Development lead. {lead.notes}"[:5000]
        ),
    )
    session.add(tenant)
    await session.flush()
    # A prospect gets its employer-page slug at creation too: the page itself
    # stays hidden until the customer status is `active` (the public routes
    # filter on it), so assigning early just means the URL is ready the day
    # they are onboarded (2026-09-05 add-features spec).
    await employer_pages.assign_slug(session, tenant)
    return tenant


# ── The BD Customers page ────────────────────────────────────────────────────

def customer_predicates(
    *, search: str | None = None, owner_user_id: uuid.UUID | None = None
) -> list:
    """A customer is a lead whose agreement was signed. Archived leads are
    excluded: archiving a lead hides the whole relationship, and a hidden lead
    reappearing on the Customers page would be a surprising leak."""
    predicates = [BDLead.agreement.is_(True), BDLead.archived_at.is_(None)]
    if owner_user_id is not None:
        predicates.append(BDLead.owner_user_id == owner_user_id)
    needle = (search or "").strip()
    if needle:
        pattern = f"%{needle}%"
        predicates.append(
            or_(
                BDLead.company_name.ilike(pattern),
                BDLead.industry.ilike(pattern),
                BDLead.location.ilike(pattern),
                BDLead.contact_name.ilike(pattern),
                BDLead.contact_email.ilike(pattern),
                BDLead.contact_phone.ilike(pattern),
            )
        )
    return predicates


def customer_list_query(
    predicates: Sequence, page: int | None = None, page_size: int | None = None
) -> Select:
    """Customers, newest agreement first, with a TOTAL order.

    `page` is optional so the CSV export can reuse this builder unpaginated and
    therefore honour exactly the same filters as the table above it.
    """
    stmt = select(BDLead).where(*predicates).order_by(
        BDLead.agreement_at.desc().nullslast(), BDLead.company_name, BDLead.id
    )
    if page is not None and page_size is not None:
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    return stmt


def customer_out(lead: BDLead) -> BDCustomerOut:
    return BDCustomerOut(
        lead_id=lead.id,
        tenant_id=lead.tenant_id,
        company_name=lead.company_name,
        location=lead.location,
        industry=lead.industry,
        contact_name=lead.contact_name,
        contact_email=lead.contact_email,
        contact_phone=lead.contact_phone,
        website=lead.website,
        channel=lead.channel,  # type: ignore[arg-type]
        social_source=lead.social_source,  # type: ignore[arg-type]
        agreement_at=lead.agreement_at,
    )


# ── CSV export ───────────────────────────────────────────────────────────────

def _csv_line(values: Iterable[object]) -> str:
    """One correctly quoted CSV line.

    `csv.writer` rather than ",".join: a company called "Acme, Inc." must stay
    one field, and a name containing a quote must be escaped rather than
    breaking the row. `\\r\\n` is the RFC 4180 terminator Excel expects.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(["" if value is None else str(value) for value in values])
    return buffer.getvalue()


def csv_header() -> str:
    return _csv_line(label for _field, label in CSV_COLUMNS)


def csv_row(customer: BDCustomerOut) -> str:
    values = []
    for field, _label in CSV_COLUMNS:
        value = getattr(customer, field, None)
        if isinstance(value, datetime):
            # Date only: the BD team reads these in a spreadsheet, and a
            # timezone-suffixed timestamp is noise in a sales export.
            value = value.date().isoformat()
        values.append(value)
    return _csv_line(values)


def iter_csv(customers: Iterable[BDCustomerOut]) -> Iterator[str]:
    """Header first, then one line per customer. A generator so the response
    streams instead of building the whole file in memory."""
    yield csv_header()
    for customer in customers:
        yield csv_row(customer)


def csv_filename(now: datetime | None = None) -> str:
    return f"vivekium-bd-customers-{_now(now).date().isoformat()}.csv"


# ── AI Reach, segment 1: our OWN customer database ───────────────────────────

@dataclass(frozen=True)
class ReachCandidate:
    job_id: uuid.UUID
    title: str
    skills: tuple[str, ...]
    vector: tuple[float, ...] | None
    tenant_name: str
    tenant_industry: str | None
    website_domain: str | None
    tenant_domain: str | None
    jd_json: dict


@dataclass(frozen=True)
class RankedReachCandidate:
    candidate: ReachCandidate
    similarity: float


def normalize_role(value: str) -> str:
    """Canonical role text used by the query and catalogue documents."""
    normalized = re.sub(r"[/_.()+-]+", " ", (value or "").casefold())
    normalized = " ".join(normalized.split())
    for source, target in _ROLE_ALIASES.items():
        normalized = normalized.replace(source, target)
    return " ".join(normalized.split())


def role_embedding_text(title: str, skills: Sequence[str]) -> str:
    clean_skills = sorted(
        {normalize_role(skill) for skill in skills if normalize_role(skill)}
    )
    return (
        f"Job role: {normalize_role(title)}\n"
        f"Primary skills: {', '.join(clean_skills)}"
    )


def _vector(value: object) -> tuple[float, ...] | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return tuple(float(item) for item in value)
    raw = str(value).strip().removeprefix("[").removesuffix("]")
    if not raw:
        return None
    try:
        return tuple(float(item) for item in raw.split(","))
    except ValueError:
        return None


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def _distinctive_tokens(value: str) -> set[str]:
    tokens = set(normalize_role(value).split())
    distinctive = tokens - _GENERIC_ROLE_TOKENS
    return distinctive or tokens


def _lexical_role_similarity(query: str, title: str) -> float:
    wanted = _distinctive_tokens(query)
    available = set(normalize_role(title).split())
    return len(wanted & available) / len(wanted) if wanted else 0.0


def rank_role_candidates(
    query: str,
    query_vector: Sequence[float] | None,
    candidates: Sequence[ReachCandidate],
    *,
    threshold: float = SIMILARITY_THRESHOLD,
) -> list[RankedReachCandidate]:
    """Return only semantically relevant roles, best match first."""
    ranked: list[RankedReachCandidate] = []
    for candidate in candidates:
        semantic = (
            _cosine(query_vector, candidate.vector)
            if query_vector is not None and candidate.vector is not None
            else 0.0
        )
        # Short role queries contain generic nouns. This exact-vocabulary
        # guard makes "Java Developer" robust without letting "Developer"
        # admit every unrelated developer role.
        lexical = _lexical_role_similarity(query, candidate.title)
        # BGE role vectors are intentionally broad: "Java Developer" and
        # "Full Stack Developer" are semantically related, but the latter is
        # not an honest search result. Distinctive query vocabulary supplies a
        # deterministic precision term while the embedding supplies synonym
        # and phrasing recall.
        role_similarity = (semantic * 0.80) + (lexical * 0.20)
        if role_similarity >= threshold:
            ranked.append(RankedReachCandidate(candidate, role_similarity))
    return sorted(
        ranked,
        key=lambda item: (
            -item.similarity,
            normalize_role(item.candidate.title),
            item.candidate.tenant_name.casefold(),
            str(item.candidate.job_id),
        ),
    )


def _confidence(similarity: float, possible: int | None = None) -> str:
    """Project the internal similarity onto the approved word-only scale.

    ``possible`` remains accepted while older callers transition from the
    historical substring hit-count implementation.
    """
    if possible is not None:
        similarity = similarity / possible if possible else 0.0
    if similarity >= 0.90:
        return "Highly Matching"
    if similarity >= 0.86:
        return "Matching"
    if similarity >= SIMILARITY_THRESHOLD:
        return "Moderately Matching"
    return "Not Matching"


def _candidate_from_row(row) -> ReachCandidate:
    mapping = row._mapping
    skills = tuple(mapping["primary_skills"] or ())
    if not skills:
        skills = tuple(
            skill
            for skill in (mapping["jd_json"] or {}).get("skills", [])
            if isinstance(skill, str)
        )
    return ReachCandidate(
        job_id=mapping["job_id"],
        title=mapping["title"],
        skills=skills,
        vector=_vector(mapping["reach_embedding"]),
        tenant_name=mapping["tenant_name"],
        tenant_industry=mapping["tenant_industry"],
        website_domain=mapping["website_domain"],
        tenant_domain=mapping["tenant_domain"],
        jd_json=mapping["jd_json"] or {},
    )


async def similar_to_customers(
    session: AsyncSession, *, job_role: str, city: str, industry: str,
    company: str | None = None,
) -> list[JobCardOut]:
    """Semantic customer-catalogue search with a hard role threshold."""
    rows = (
        await session.execute(
            sql_text(
                """
                SELECT j.id AS job_id, j.title, j.jd_json,
                       j.reach_embedding::text AS reach_embedding,
                       t.name AS tenant_name, t.industry AS tenant_industry,
                       t.website_domain, t.domain AS tenant_domain,
                       COALESCE(
                         array_agg(c.name ORDER BY c.ordinal)
                           FILTER (WHERE c.category = 'primary_skill'
                                        AND c.is_active),
                         ARRAY[]::varchar[]
                       ) AS primary_skills
                  FROM jobs j
                  JOIN tenants t ON t.id = j.tenant_id
             LEFT JOIN job_competencies c ON c.job_id = j.id
                 WHERE j.archived_at IS NULL
                   AND t.status IN ('active', 'prospect')
                   AND (:company = '' OR t.name ILIKE :company_pattern)
              GROUP BY j.id, j.title, j.jd_json, j.reach_embedding,
                       t.name, t.industry, t.website_domain, t.domain
                """
            ),
            {
                "company": (company or "").strip(),
                "company_pattern": f"%{(company or '').strip()}%",
            },
        )
    ).all()
    candidates = [_candidate_from_row(row) for row in rows]

    query_vector: tuple[float, ...] | None = None
    missing = [candidate for candidate in candidates if candidate.vector is None]
    try:
        query_vector = tuple(
            await embed_query(role_embedding_text(job_role, ()))
        )
        vectors = await embed_passages(
            [
            role_embedding_text(candidate.title, candidate.skills)
            for candidate in missing
            ]
        )
        replacements = {
            candidate.job_id: tuple(vector)
            for candidate, vector in zip(missing, vectors)
        }
        for candidate, vector in zip(missing, vectors):
            await session.execute(
                sql_text(
                    "UPDATE jobs SET reach_embedding = CAST(:vector AS vector) "
                    "WHERE id = :job_id"
                ),
                {
                    "job_id": str(candidate.job_id),
                    "vector": "[" + ",".join(str(value) for value in vector) + "]",
                },
            )
        candidates = [
            ReachCandidate(
                **{
                    **candidate.__dict__,
                    "vector": replacements.get(candidate.job_id, candidate.vector),
                }
            )
            for candidate in candidates
        ]
    except ReachEmbeddingError:
        # Exact distinctive-role matching is the honest degraded path. It
        # remains thresholded and never pads with unrelated titles.
        logger.warning("bd.ai_reach_embedding_unavailable")

    ranked = rank_role_candidates(job_role, query_vector, candidates)
    cards: list[JobCardOut] = []
    for item in ranked[:SIMILAR_LIMIT]:
        candidate = item.candidate
        host = (candidate.website_domain or candidate.tenant_domain or "").strip()
        if not host:
            continue
        company_url = (
            host if host.startswith(("http://", "https://")) else f"https://{host}"
        )
        location = candidate.jd_json.get("location")
        cards.append(
            JobCardOut(
                job_title=candidate.title,
                company=candidate.tenant_name,
                city=location if isinstance(location, str) else None,
                industry=candidate.tenant_industry,
                company_url=company_url,
                job_url=None,
                source_domain=(candidate.website_domain or candidate.tenant_domain),
                confidence_label=_confidence(item.similarity),
            )
        )
    return cards
