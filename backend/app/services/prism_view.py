"""The PRISM Report's READ MODEL: one stored report, shaped for a reader.

PLAN-p5 WP5-F, the Vivekium release. The report route, the PDF route and the
citation route all read a report through this module, so the screen and the
PDF are built from ONE serializer and cannot disagree about a grade, a note or
a chart. It imports no grading stage: it reads rows the pipeline wrote and
never writes one, and `tests/test_assessment_pipeline_direction.py` keeps the
pipeline from importing it back.

WHAT CROSSES THE BOUNDARY, AND WHAT DOES NOT
--------------------------------------------
* A grade is a WORD, converted here from the internal score. A row the
  evaluation could not complete (`assessment_status = 'not_assessed'`, score
  NULL since migration 0130) states `NOT_ASSESSED_WORD` instead of a grade,
  with `status_note` saying why; it is never drawn on a radar and never
  projected to the bottom band, which would state Not Matching.
* `remark_note` marks a remark a fixed template wrote (the writing model was
  unavailable), and `support_note` is the citation trail's own marker for a
  remark its evidence did not clearly support. Both are the server's words.
* The AI Score of a report written from the Vivekium release on is Yukti's
  frozen snapshot (`ai_score_snapshot`); an older report still carries its
  four matching rows under the legacy category and renders them.
* `pdf_available` / `pdf_blocked_reason` come from `siddhi.delivery`'s gate in
  its non-raising form, so the screen never offers a download the PDF route
  would refuse.
"""
from __future__ import annotations

import uuid
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assessment import FunctionalSkillsReport, ReportDimension
from app.models.candidate import Candidate, JobCandidateLink
from app.schemas.assessments import (
    AiScoreSnapshotOut,
    AiScoreTagOut,
    ClaimEvidenceOut,
    DimensionOut,
    FunctionalReportOut,
    GapAnalysisOut,
    LeadershipAlignmentOut,
    RadarChartOut,
    ValidationPointsOut,
)
from app.services import (
    evidence_confidence,
    ppi,
    reference_code,
    retention_consent,
)
from app.services.rating import GRADES, band_index_for, grade_for_percent
from app.services.siddhi import ai_score as siddhi_ai_score
from app.services.siddhi import remarks as siddhi_remarks
from app.services.siddhi.synthesis import NOT_ASSESSED_WORD
from app.services.siddhi import trail as siddhi_trail

__all__ = [
    "CATEGORY_MATCHING",
    "CATEGORY_TECHNICAL",
    "OVERALL_AXES",
    "RADAR_BANDS",
    "RADAR_CHART_KEYS",
    "RADAR_CHART_TITLES",
    "RADAR_SERIES",
    "REPORT_CATEGORIES",
    "REPORT_NOT_READY",
    "STATUS_NOTE_NOT_ASSESSED",
    "build_radar_charts",
    "dimension_out",
    "load_report",
    "report_out",
]

#: LEGACY. Reports written before the Vivekium release carry AI Score rows
#: under this category (four matching parameters, 25 to 30 word remarks). The
#: AI Score is now Yukti's frozen snapshot on `ai_score_json`; nothing writes
#: this category any more, and it is read so a historic report still renders.
CATEGORY_MATCHING = "matching"

#: LEGACY. Reports written before Draft v4 carry rows scored against the
#: standalone technical bank that no longer exists; read, never written.
CATEGORY_TECHNICAL = "technical"

#: Report section order. The two legacy categories trail it because only a
#: historic report carries them.
REPORT_CATEGORIES: tuple[str, ...] = (
    CATEGORY_MATCHING,
    ppi.CATEGORY_MUST_HAVE,
    ppi.CATEGORY_NICE_TO_HAVE,
    ppi.CATEGORY_BEHAVIOURAL,
    CATEGORY_TECHNICAL,
)

#: What a reader asking for a report that is not written yet is told.
REPORT_NOT_READY = "The PRISM Report is not ready yet"

#: The sentence beside a skill the evaluation could not complete. Its grade
#: slot carries `siddhi.synthesis.NOT_ASSESSED_WORD`, never a grade.
STATUS_NOTE_NOT_ASSESSED = "Not assessed: the evaluation could not be completed"

_STATUS_GRADED = "graded"
_STATUS_NOT_ASSESSED = "not_assessed"

# ── Radar charts (spec 9.4) ──────────────────────────────────────────────────
# Each chart plots the candidate's assessed level and, where the report
# recorded one, what the job requires, on the same axes. No number appears on
# an axis, a data label or a tooltip: `*_index` is a RENDERING COORDINATE
# (1..4), because a radar has no geometry without a radius and the four grades
# ARE the radial axis.

#: Ordered best-to-worst grade labels, for the radar legend and colour ramp.
RADAR_BANDS: tuple[str, ...] = GRADES

RADAR_CHART_KEYS: tuple[str, ...] = ("overall", *ppi.CATEGORIES)

RADAR_CHART_TITLES: dict[str, str] = {
    "overall": "Overall",
    **{category: ppi.CATEGORY_LABELS[category] for category in ppi.CATEGORIES},
}

#: The legend below every chart, by word only (spec 9.4).
RADAR_SERIES: tuple[str, ...] = ("Job Requirement", "Candidate Assessment")

#: One Overall spoke per aspect, both shapes derived from the same rows the
#: sections render, so a chart can never disagree with the text beside it.
OVERALL_AXES: tuple[tuple[str, str], ...] = tuple(
    (category, ppi.CATEGORY_LABELS[category]) for category in ppi.CATEGORIES
)


def _mean(values: list[int]) -> int:
    return round(sum(values) / len(values))


def _axis(name: str, candidate_score: int, required: int | None) -> dict[str, Any]:
    """One spoke. `required` None means the report recorded no requirement for
    it, and the spoke carries NO requirement shape: inventing a default level
    would draw a requirement nobody stated."""
    candidate_band = grade_for_percent(candidate_score) or GRADES[-1]
    requirement_band = grade_for_percent(required) if required is not None else None
    return {
        "axis": name,
        "requirement_band": requirement_band,
        "requirement_index": (
            band_index_for(requirement_band) if requirement_band is not None else None
        ),
        "candidate_band": candidate_band,
        "candidate_index": band_index_for(candidate_band),
    }


def build_radar_charts(dimensions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The radar charts, built from the SAME dimension rows the sections render.

    A row with no score (a skill stated "Not assessed", 0130) has NO axis: a
    candidate shape drawn for it would plot a grade nobody made, and drawing
    it at the bottom band would state Not Matching. Pure; unit-tested.
    """
    scored = [row for row in dimensions if row.get("score") is not None]
    by_category: dict[str, list[dict[str, Any]]] = {}
    for row in scored:
        by_category.setdefault(row["category"], []).append(row)

    charts: list[dict[str, Any]] = []
    overall_axes = []
    for category, label in OVERALL_AXES:
        rows = by_category.get(category) or []
        if not rows:
            continue
        required = [row["required_level"] for row in rows if row.get("required_level")]
        overall_axes.append(
            _axis(
                label,
                _mean([row["score"] for row in rows]),
                _mean(required) if required else None,
            )
        )
    charts.append({"key": "overall", "title": RADAR_CHART_TITLES["overall"], "axes": overall_axes})

    for category in ppi.CATEGORIES:
        rows = sorted(by_category.get(category) or [], key=lambda item: item["ordinal"])
        charts.append(
            {
                "key": category,
                "title": RADAR_CHART_TITLES[category],
                "axes": [
                    _axis(row["name"], row["score"], row.get("required_level")) for row in rows
                ],
            }
        )
    return charts


# ── One row ──────────────────────────────────────────────────────────────────


def dimension_out(
    row: ReportDimension, trail: siddhi_trail.CitationTrail | None = None
) -> DimensionOut:
    """One stored row as a reader sees it. Words only; see the module docstring."""
    status = row.assessment_status or _STATUS_GRADED
    not_assessed = status == _STATUS_NOT_ASSESSED or row.score is None
    return DimensionOut(
        name=row.name,
        description=row.description,
        grade=NOT_ASSESSED_WORD if not_assessed else (grade_for_percent(row.score) or GRADES[-1]),
        status=_STATUS_NOT_ASSESSED if not_assessed else status,
        status_note=STATUS_NOTE_NOT_ASSESSED if not_assessed else None,
        required_level=grade_for_percent(row.required_level),
        remark=row.remark,
        remark_note=siddhi_remarks.remark_note(row.remark_provenance),
        support_note=(
            trail.remark_support_note(row.category, row.name)
            if trail is not None and trail.available
            else None
        ),
        # EVIDENCE CONFIDENCE (0107). The stored code becomes a word HERE,
        # server side, for the same reason `score` becomes a grade here: a
        # code that crossed the boundary is a code somebody eventually renders
        # directly. A row written before 0107 carries None.
        evidence_confidence=evidence_confidence.display_word(row.evidence_confidence),
        evidence_sources=list(evidence_confidence.source_labels(row.evidence_sources)),
    )


def _snapshot_out(ai_score_json: Mapping[str, Any] | None) -> AiScoreSnapshotOut | None:
    snapshot = siddhi_ai_score.read_snapshot(ai_score_json)
    if snapshot is None:
        return None
    return AiScoreSnapshotOut(
        status=snapshot.status,
        grade=snapshot.grade,
        header=snapshot.header,
        tags=[AiScoreTagOut(text=tag.text, polarity=tag.polarity) for tag in snapshot.tags],
    )


def _overall(
    report: FunctionalSkillsReport, rows: Iterable[ReportDimension]
) -> tuple[str, str]:
    """(overall grade word, overall status). A withheld overall (a Must-have
    not assessed on the final attempt, 0130) states `NOT_ASSESSED_WORD` and is
    never recomputed from the rows."""
    if report.overall_status == _STATUS_NOT_ASSESSED:
        return NOT_ASSESSED_WORD, _STATUS_NOT_ASSESSED
    overall = report.overall_score
    if overall is None and report.overall_status is None:
        # Written before migration 0030, when the overall was not stored.
        # Recomputed from the rows it was written with rather than shown blank.
        # A report with no assessed row has nothing to recompute from, and it
        # says so: a zero substituted here would state Not Matching about a
        # candidate nobody graded.
        assessed = [
            row.score
            for row in rows
            if row.category != CATEGORY_MATCHING and row.score is not None
        ]
        overall = round(sum(assessed) / len(assessed)) if assessed else None
    if overall is None:
        return NOT_ASSESSED_WORD, _STATUS_NOT_ASSESSED
    return grade_for_percent(overall) or GRADES[-1], _STATUS_GRADED


# ── The whole report ─────────────────────────────────────────────────────────


async def load_report(
    session: AsyncSession, link_id: uuid.UUID
) -> FunctionalSkillsReport | None:
    return (
        await session.execute(
            select(FunctionalSkillsReport).where(
                FunctionalSkillsReport.job_candidate_link_id == link_id
            )
        )
    ).scalars().first()


async def report_out(
    session: AsyncSession, link: JobCandidateLink, report: FunctionalSkillsReport
) -> FunctionalReportOut:
    """The report payload, shared by the report route and the PDF route."""
    from app.services.proctoring import report as proctoring_report  # noqa: PLC0415
    from app.services.siddhi import delivery  # noqa: PLC0415

    rows = (
        await session.execute(
            select(ReportDimension)
            .where(ReportDimension.report_id == report.id)
            .order_by(ReportDimension.ordinal)
        )
    ).scalars().all()
    trail = siddhi_trail.read_trail(report.gap_analysis_json)

    grouped: dict[str, list[DimensionOut]] = {}
    for row in rows:
        grouped.setdefault(row.category, []).append(dimension_out(row, trail))

    charts = build_radar_charts(
        [
            {
                "category": row.category,
                "name": row.name,
                "score": row.score,
                "required_level": row.required_level,
                "ordinal": row.ordinal,
            }
            for row in rows
        ]
    )
    # The legend names only the shapes a chart actually draws.
    draws_requirement = any(
        axis["requirement_index"] is not None for chart in charts for axis in chart["axes"]
    )
    series = list(RADAR_SERIES) if draws_requirement else [RADAR_SERIES[1]]

    overall_grade, overall_status = _overall(report, rows)

    # The Proctoring Report, appended as the final, informational section. It
    # moves nothing above it: no grade in this payload is read from it.
    proctoring = await proctoring_report.load_report_out(session, link.id)
    proctoring_state = await proctoring_report.load_state(session, link.id)
    candidate = await session.get(Candidate, link.candidate_id)
    _, blocked_reason = await delivery.clearance_or_reason(session, report)

    return FunctionalReportOut(
        id=report.id,
        job_candidate_link_id=link.id,
        # The same COMPANY-JOB-CANDIDATE code the candidate table shows under
        # the name. Recomputed rather than joined: a pure function of three ids.
        reference_code=reference_code.reference_code(
            link.tenant_id, link.job_id, link.candidate_id
        ),
        grade=report.grade,
        ai_score=grouped.get(CATEGORY_MATCHING, []),
        ai_score_snapshot=_snapshot_out(report.ai_score_json),
        overall_grade=overall_grade,
        overall_status=overall_status,
        overall_summary=report.overall_summary,
        must_have=grouped.get(ppi.CATEGORY_MUST_HAVE, []),
        nice_to_have=grouped.get(ppi.CATEGORY_NICE_TO_HAVE, []),
        behavioural=grouped.get(ppi.CATEGORY_BEHAVIOURAL, []),
        # Empty on every report written from Draft v4 onward; read-only for a
        # report written before it.
        technical=grouped.get(CATEGORY_TECHNICAL, []),
        validation=report.validation_json,
        proctoring=proctoring,
        proctoring_state=proctoring_state,
        gap_analysis=GapAnalysisOut.model_validate(report.gap_analysis_json or {}),
        # EMPTY models, not None, for a report written before 0107.
        claim_evidence=ClaimEvidenceOut.model_validate(report.claim_evidence_json or {}),
        validation_points=ValidationPointsOut.model_validate(
            report.validation_points_json or {}
        ),
        # The stored section carries its context id, version and digest for
        # provenance; only the words cross the boundary.
        leadership_alignment=(
            LeadershipAlignmentOut.model_validate(
                {
                    "note": report.leadership_alignment_json.get("note") or "",
                    "groups": report.leadership_alignment_json.get("groups") or [],
                }
            )
            if report.leadership_alignment_json
            else None
        ),
        # Populated only where Gap Analysis is not: a pre-Draft-v4 report shows
        # what it was actually written with.
        suggested_interview_questions=(
            list(report.suggested_probes_json or [])
            if not (report.gap_analysis_json or {})
            else []
        ),
        radar_charts=[RadarChartOut(**chart) for chart in charts],
        radar_bands=list(RADAR_BANDS),
        radar_series=series,
        synthesized_at=report.synthesized_at,
        immutable=True,
        report_download_allowed=retention_consent.assessment_download_allowed(candidate),
        pdf_available=blocked_reason is None,
        pdf_blocked_reason=blocked_reason,
    )
