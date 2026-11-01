"""Consumption interfaces: how the rest of Vivekium reads project evidence.

Two consumers, two shapes, one source:

  * `candidate_project_context` -- a compact text block for the AI context
    that already includes the JD, framework and resume. Wired into
    per-candidate PPI question generation so the interviewer can probe what
    the projects actually show; it changes no grade and no report section.
  * `recruiter_view` -- the client-facing shape. WORDS ONLY for anything
    rating-like (`evidence_strength` is Strong/Moderate/Limited/Insufficient);
    candidate claims and system-observed evidence stay visibly separate.

No project is required: both interfaces return an honest empty for a
candidate with none, and NOTHING here penalises absence. What the wider
intelligence does with less evidence is the wider intelligence's decision
(brief section 3).
"""
from __future__ import annotations

import uuid
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import (
    EVIDENCE_READY_STATUSES,
    STATUS_FAILED_EVIDENCE,
    STATUS_FAILED_EXTRACTION,
    STATUS_FAILED_SECURITY,
    STATUS_PARTIALLY_PROCESSED,
    STATUS_PERSISTED,
    STATUS_PROCESSED,
    STATUS_PROCESSING,
    STATUS_SUBMITTED,
    CandidateProject,
)

#: Ceiling for the question-generation context block. Small on purpose: this
#: rides inside a prompt that already carries a JD, a framework and a resume
#: excerpt, and evidence reduction is the feature, not a constraint.
MAX_CONTEXT_CHARS = 3000


async def ready_projects(
    session: AsyncSession, candidate_id: uuid.UUID
) -> list[CandidateProject]:
    rows = (
        await session.execute(
            select(CandidateProject)
            .where(
                CandidateProject.candidate_id == candidate_id,
                CandidateProject.status.in_(sorted(EVIDENCE_READY_STATUSES)),
            )
            .order_by(CandidateProject.created_at)
        )
    ).scalars().all()
    return list(rows)


#: A function applied to each stored JSON document before a block is built from
#: it. The tool layer passes its compensation stripper here so the guarantee is
#: applied to the DATA the block is read from rather than to the prose it
#: becomes (`tools.implementations._extract_project_evidence`).
Redactor = Callable[[Any], Any]


def _identity(value: Any) -> Any:
    return value


def _project_block(project: CandidateProject, redact: Redactor = _identity) -> str:
    record = redact(project.evidence_json or {})
    stack = record.get("technology_stack") or {}
    lines = [f"Project: {project.name}"]
    technologies = list(stack.get("technologies") or [])
    languages = list((stack.get("languages") or {}).keys())
    observed = sorted(set(languages) | set(technologies))
    if observed:
        lines.append("Observed stack: " + ", ".join(observed[:12]))
    interpretation = redact(project.ai_interpretation_json or {})
    if interpretation.get("synthesis"):
        lines.append("Assessment: " + str(interpretation["synthesis"]))
    for area in (interpretation.get("validation_areas") or [])[:3]:
        lines.append("Worth probing: " + str(area))
    for gap in (record.get("potential_gaps") or [])[:3]:
        lines.append("Evidence gap: " + str(gap))
    return "\n".join(lines)


async def candidate_project_context(
    session: AsyncSession,
    candidate_id: uuid.UUID,
    *,
    redact: Redactor = _identity,
) -> str:
    """The AI-context block, empty string when there is nothing ready.

    `redact` is applied to each stored JSON document before a block is built,
    so a caller with a stricter boundary (the tool layer's compensation strip)
    narrows what is READ rather than scrubbing what was written.
    """
    projects = await ready_projects(session, candidate_id)
    if not projects:
        return ""
    blocks = [_project_block(project, redact) for project in projects]
    text = (
        "Project evidence (derived from the candidate's submitted projects; "
        "claims and observations are labelled):\n\n" + "\n\n".join(blocks)
    )
    return text[:MAX_CONTEXT_CHARS]


# ── Recruiter-facing shape ───────────────────────────────────────────────────


def recruiter_view(project: CandidateProject) -> dict[str, Any]:
    """One project as the review screen renders it. No number that could read
    as a rating crosses this boundary; strength is a word backed by the
    database CHECK."""
    record = project.evidence_json or {}
    interpretation = project.ai_interpretation_json or {}
    stack = record.get("technology_stack") or {}
    identity = record.get("project_identity") or {}
    return {
        "id": str(project.id),
        "name": project.name,
        "status": project.status,
        "submission_kind": project.submission_kind,
        "repository_url": project.repository_url,
        "domains": list(identity.get("domains") or []),
        # The candidate's own words, labelled as such.
        "candidate_description": project.description,
        "technologies": sorted(
            set(stack.get("technologies") or [])
            | set((stack.get("languages") or {}).keys())
        ),
        "observed_evidence": [
            str(unit.get("statement"))
            for unit in (project.evidence_units_json or [])
            if unit.get("unit_type")
            in {"architecture", "implementation", "testing", "infrastructure"}
        ][:12],
        "documentation_evidence": [
            str(unit.get("statement"))
            for unit in (project.evidence_units_json or [])
            if unit.get("unit_type") == "documentation"
        ][:6],
        "claim_assessments": interpretation.get("claim_assessments") or [],
        "synthesis": interpretation.get("synthesis"),
        "evidence_strength": project.evidence_strength,
        "potential_gaps": list(record.get("potential_gaps") or []),
        "validation_areas": interpretation.get("validation_areas") or [],
        "uncertainties": list(record.get("uncertainties") or [])[:8],
        "processed_at": (
            project.processed_at.isoformat() if project.processed_at else None
        ),
    }


#: What a reviewer is told about each submission, by stored status. A word, so
#: the screen never has to interpret a database status.
_SUBMISSION_STATE: dict[str, str] = {
    STATUS_SUBMITTED: "queued",
    STATUS_PROCESSING: "analysing",
    STATUS_PERSISTED: "analysing",
    STATUS_PROCESSED: "ready",
    STATUS_PARTIALLY_PROCESSED: "summary_only",
    STATUS_FAILED_SECURITY: "failed",
    STATUS_FAILED_EXTRACTION: "failed",
    STATUS_FAILED_EVIDENCE: "failed",
}


def submission_state(status: str) -> str:
    return _SUBMISSION_STATE[status]


def _submission(project: CandidateProject) -> dict[str, Any]:
    """Submission status only: no evidence, no candidate text, no failure code."""
    return {
        "id": str(project.id),
        "name": project.name,
        "state": submission_state(project.status),
        "status_detail": project.status_detail,
        "submitted_at": project.created_at.isoformat() if project.created_at else None,
        "updated_at": project.updated_at.isoformat() if project.updated_at else None,
        "processed_at": project.processed_at.isoformat() if project.processed_at else None,
    }


async def recruiter_overview(
    session: AsyncSession, candidate_id: uuid.UUID
) -> dict[str, Any]:
    """Every submission's status, kept apart from the evidence that is ready.

    `projects` is derived evidence and holds only evidence-ready rows.
    `submissions` holds EVERY row, so a project still being analysed is never
    reported as absent. `counts` lets the screen say "3 projects submitted;
    analysis queued" and reserve "none" for a candidate with no rows at all.
    """
    rows = list(
        (
            await session.execute(
                select(CandidateProject)
                .where(CandidateProject.candidate_id == candidate_id)
                .order_by(CandidateProject.created_at)
            )
        ).scalars()
    )
    counts = {"submitted": len(rows)}
    for state in sorted(set(_SUBMISSION_STATE.values())):
        counts[state] = sum(1 for row in rows if submission_state(row.status) == state)
    return {
        "projects": [
            recruiter_view(row) for row in rows if row.status in EVIDENCE_READY_STATUSES
        ],
        "submissions": [_submission(row) for row in rows],
        "counts": counts,
    }
