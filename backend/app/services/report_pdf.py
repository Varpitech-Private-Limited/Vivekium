"""Render an immutable PRISM Report as a branded PDF.

PRISM Report is the DOCUMENT. Tatva Assessment is the PROCESS that produces it.
Neither name is ever used for the other.

The module, its route and the stored columns still say "ppi". That is
deliberate: a report link already in someone's inbox quotes the route, a
rolling deploy is still writing traces under the old module name, and every
report written before 2026-08-23 was filed under it. Renaming the symbols would
break a reader's access to an existing report and buy nothing a reader sees.

ReportLab avoids shipping a browser runtime. Radar charts are rasterized with
Pillow and embedded as PNG images so the permanent record is portable.
"""
from __future__ import annotations

import io
import math
from datetime import datetime
from typing import Any, Iterable
from xml.sax.saxutils import escape

from PIL import Image as PILImage
from PIL import ImageDraw
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.services.siddhi.synthesis import NOT_ASSESSED_WORD

# A full stop, not a dash. The no-em-dash rule covers every string a client
# reads, and a PDF footer is read by the client more often than most of the UI.
# The repo-wide sweep never caught this one because the character was assembled
# from chr(8212), which is exactly the construction the sweep cannot see.
FOOTER = "Confidential. Permanent Assessment Record."

#: The report's header, verbatim (spec doc 4, part 3). Split in two because the
#: title and the expansion are set in different styles; a reader must be able to
#: read the two lines together and get the string the spec wrote.
REPORT_TITLE = "PRISM Report"
REPORT_SUBTITLE = "Evidence-Based Role Intelligence & Suitability Mapping"

#: The section order (spec doc 4, part 3), and the ONLY place it is written down
#: on this side. `render_report_pdf` walks this tuple rather than emitting
#: sections in hand-written sequence, so the order cannot be changed here
#: without changing what the PDF does.
#:
#: These keys are the same keys `REPORT_SECTION_ORDER` uses in
#: frontend/components/functional-skills-report.tsx, and tests/test_prism_report
#: reads that literal out of the .tsx and asserts the two are equal. A reorder
#: applied to one renderer therefore fails a test instead of shipping a PDF that
#: disagrees with the screen the recruiter approved it from.
#:
#: Gap Analysis now precedes Validation, reversing the earlier order. Validation
#: is the candidate's own unrated submission; the action plan belongs beside the
#: grades it was derived from rather than after a block of uninterpreted form
#: answers.
#:
#: The Proctoring Report is LAST (proctoring spec section 7: "Appended as the
#: final section"). It is informational, moves no grade, and sits after every
#: section that does, so a reader reaches the assessment before the monitoring.
#: The Evidence vs Claim Summary sits with the rated sections and the
#: Recommended Human Validation Points sit with the plan, which is why they are
#: not adjacent. The first answers "what did they assert and what stands behind
#: it", so it belongs beside the grades it annotates. The second answers "what
#: should a person check", so it belongs beside the Gap Analysis, which is the
#: other thing an interviewer acts on. Gap Analysis still precedes Validation,
#: unchanged.
SECTION_ORDER: tuple[str, ...] = (
    "ai_score",
    "overall",
    "must_have",
    "nice_to_have",
    "behavioural",
    "leadership_alignment",
    "claim_evidence",
    "gap_analysis",
    "validation_points",
    "validation",
    "proctoring",
)

#: Leadership Alignment (0135, spec 22.9), immediately after Behavioural: it
#: reads the grades above it against what the company's leaders asked for.
#: Verbatim in both renderers and in `siddhi.synthesis.SECTION_TITLES`.
LEADERSHIP_ALIGNMENT_TITLE = "Leadership Alignment"

#: The two 0107 headings, verbatim in both renderers and in
#: `siddhi.synthesis.SECTION_TITLES`.
CLAIM_EVIDENCE_TITLE = "Evidence vs Claim Summary"
VALIDATION_POINTS_TITLE = "Recommended Human Validation Points"

#: The proctoring section's heading and its note, verbatim in both renderers.
PROCTORING_TITLE = "Proctoring Report"
PROCTORING_NOTE = (
    "Informational only. This section does not affect this candidate's score "
    "or ranking."
)
PROCTORING_ABSENT = "No proctoring report exists for this assessment."
#: Said when a report exists but the monitoring behind it had gaps and the
#: serializer gave no sentence of its own.
PROCTORING_PARTIAL = (
    "Monitoring was incomplete for part of this assessment, so this report "
    "describes less than the whole session."
)

#: THREE radar charts, not four (spec doc 4, part 3), which lists a chart for
#: Overall Assessment, Must-have and Nice-to-have and lists only a grade and a
#: remark for Behavioural.
#:
#: Filtered at RENDER rather than at the generator on purpose: a report is
#: immutable, so every report written before today still carries a behavioural
#: chart in its stored payload, and a reader opening an old report must see the
#: same three charts as a reader opening a new one.
RENDERED_CHART_KEYS: tuple[str, ...] = ("overall", "must_have", "nice_to_have")

#: Titles as `prism_view.RADAR_CHART_TITLES` writes them, duplicated
#: here ONLY to identify a chart in a payload that predates the `key` field.
#: Copied rather than imported: this is a frozen historical shape, and following
#: a live label would make an old report's charts vanish the day somebody
#: renames a category.
_LEGACY_CHART_TITLES: dict[str, str] = {
    "overall": "Overall",
    "must_have": "Must-have",
    "nice_to_have": "Nice-to-have",
}
PURPLE = colors.HexColor("#5028E0")
INK = colors.HexColor("#172033")
MUTED = colors.HexColor("#64748B")
PAPER = colors.HexColor("#F7F7FB")


def _value(item: Any, key: str, default: Any = None) -> Any:
    # A dict is asked BEFORE getattr, not after. The other way round, a payload
    # arriving as a dict returned `dict.items`, a bound method, for the gap
    # group's "items" key, and the section rendered as a TypeError instead of a
    # list of gaps. Every key that shadows a dict method had the same hole.
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _text(value: Any) -> str:
    return escape(str(value or ""))


def radar_png(chart: Any, *, size: int = 720) -> bytes:
    """Rasterize one qualitative radar chart without displaying coordinates."""
    axes = list(_value(chart, "axes", []))
    image = PILImage.new("RGB", (size, size), "white")
    draw = ImageDraw.Draw(image)
    center = size / 2
    radius = size * 0.31
    count = max(1, len(axes))

    for ring in range(1, 5):
        points = [
            (
                center
                + radius * ring / 4 * math.cos(-math.pi / 2 + 2 * math.pi * i / count),
                center
                + radius * ring / 4 * math.sin(-math.pi / 2 + 2 * math.pi * i / count),
            )
            for i in range(count)
        ]
        if len(points) >= 3:
            draw.polygon(points, outline="#CBD5E1")

    requirement: list[tuple[float, float]] = []
    candidate: list[tuple[float, float]] = []
    # A spoke with no recorded requirement draws no requirement shape at all:
    # a default level would plot a requirement nobody stated. The shape is
    # drawn only when EVERY spoke carries one.
    draws_requirement = all(
        _value(axis, "requirement_index") is not None for axis in axes
    )
    for index, axis in enumerate(axes):
        angle = -math.pi / 2 + 2 * math.pi * index / count
        edge = (
            center + radius * math.cos(angle),
            center + radius * math.sin(angle),
        )
        draw.line((center, center, *edge), fill="#E2E8F0", width=2)
        candidate_index = int(_value(axis, "candidate_index", 1))
        if draws_requirement:
            requirement_index = int(_value(axis, "requirement_index"))
            requirement.append(
                (
                    center + radius * requirement_index / 4 * math.cos(angle),
                    center + radius * requirement_index / 4 * math.sin(angle),
                )
            )
        candidate.append(
            (
                center + radius * candidate_index / 4 * math.cos(angle),
                center + radius * candidate_index / 4 * math.sin(angle),
            )
        )
        label = str(_value(axis, "axis", ""))[:24]
        bounds = draw.textbbox((0, 0), label)
        x = center + (radius + 48) * math.cos(angle) - (bounds[2] - bounds[0]) / 2
        y = center + (radius + 48) * math.sin(angle) - (bounds[3] - bounds[1]) / 2
        draw.text((x, y), label, fill="#172033")

    if len(requirement) >= 3:
        draw.polygon(requirement, outline="#64748B", width=5)
    if len(candidate) >= 3:
        draw.polygon(candidate, outline="#5028E0", width=6)
    if draws_requirement:
        draw.rectangle((32, size - 52, 72, size - 42), fill="#64748B")
        draw.text((82, size - 58), "Job Requirement", fill="#172033")
    draw.rectangle((300, size - 52, 340, size - 42), fill="#5028E0")
    draw.text((350, size - 58), "Candidate Assessment", fill="#172033")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _footer(canvas, document) -> None:
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#DDDCEB"))
    canvas.line(18 * mm, 13 * mm, 192 * mm, 13 * mm)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 8 * mm, FOOTER)
    canvas.drawRightString(192 * mm, 8 * mm, f"Page {document.page}")
    canvas.restoreState()


def _gap_analysis(report, styles) -> list:
    """Gap Analysis & Action Plan (spec 9.6).

    The retired suggested-question payload is retained in storage for schema
    compatibility but is never rendered in a client-facing PDF.
    """
    story: list = []
    gaps = _value(report, "gap_analysis", None) or {}
    groups = _value(gaps, "groups", []) or []
    if not groups:
        return story

    story.append(Paragraph("Gap Analysis & Action Plan", styles["Section"]))
    focus = _value(gaps, "focus_summary", "")
    if focus:
        story.append(Paragraph(f"<b>{_text(focus)}</b>", styles["Body"]))
        story.append(Spacer(1, 3 * mm))

    for group in groups:
        story.append(
            Paragraph(f"<b>{_text(_value(group, 'label', ''))}</b>", styles["Body"])
        )
        cap = _value(group, "cap_statement", None)
        if cap:
            story.append(Paragraph(_text(cap), styles["Body"]))
        items = _value(group, "items", []) or []
        if not items:
            story.append(
                Paragraph(
                    _text(_value(group, "no_gaps_statement", "No gaps identified.")),
                    styles["Body"],
                )
            )
        for item in items:
            story.append(
                Paragraph(
                    f"<b>{_text(_value(item, 'name', ''))}</b>: "
                    f"{_text(_value(item, 'grade', ''))}",
                    styles["Body"],
                )
            )
            remark = _value(item, "remark", None)
            if remark:
                story.append(Paragraph(_text(remark), styles["Body"]))
            for probe in _value(item, "probes", []) or []:
                story.append(Paragraph(f"&bull; {_text(probe)}", styles["Body"]))
            story.append(Spacer(1, 2 * mm))
        story.append(Spacer(1, 3 * mm))
    return story


def _chart(report: Any, key: str, styles: dict[str, ParagraphStyle]) -> list[Any]:
    """The one chart belonging to `key`, or nothing.

    Returning an empty list for an unlisted key is what makes the three-chart
    rule hold for reports written when four were generated: the stored payload
    still has the fourth, and nothing here goes looking for it.
    """
    if key not in RENDERED_CHART_KEYS:
        return []
    for chart in list(_value(report, "radar_charts", []) or []):
        stored = _value(chart, "key")
        # A chart with no key at all is a payload written before charts carried
        # one. Matching its title is the only identification left, and the
        # alternative is silently dropping every chart from a permanent record
        # nobody can regenerate.
        if stored != key and not (
            stored is None and _value(chart, "title") == _LEGACY_CHART_TITLES[key]
        ):
            continue
        return [
            KeepTogether(
                [
                    Spacer(1, 3 * mm),
                    Paragraph(
                        _text(_value(chart, "title", "Assessment profile")),
                        styles["Body"],
                    ),
                    Image(
                        io.BytesIO(radar_png(chart)),
                        width=118 * mm,
                        height=118 * mm,
                    ),
                ]
            )
        ]
    return []


def _overall(report: Any, styles: dict[str, ParagraphStyle]) -> list[Any]:
    """The Overall Grade, its remark, and the first of the three charts.

    The heading is the spec's section name, not the framework's name: the
    framework is the Tatva Assessment and this document is the PRISM Report,
    and printing either word in place of the other is how a reader comes away
    believing they are one thing.
    """
    return [
        Paragraph("Overall Assessment", styles["Section"]),
        Spacer(1, 2 * mm),
        Paragraph(
            f"<b>{_text(_value(report, 'overall_grade') or NOT_ASSESSED_WORD)}</b><br/>"
            f"{_text(_value(report, 'overall_summary'))}",
            styles["Body"],
        ),
        *_chart(report, "overall", styles),
    ]


def _validation(report: Any, styles: dict[str, ParagraphStyle]) -> list[Any]:
    """The candidate's own submission, reproduced and never rated.

    Last on the document by spec doc 4: it is the only unrated section, so
    ending on it keeps every rated statement and the plan drawn from them
    together rather than split around a block of form answers.
    """
    story: list[Any] = [Paragraph("Validation", styles["Section"])]
    validation = _value(report, "validation", {}) or {}
    for field in validation.get("fields", []):
        story.append(
            Paragraph(
                f"<b>{_text(field.get('label', ''))}</b><br/>"
                f"{_text(field.get('value') or 'Not stated')}",
                styles["Body"],
            )
        )
        story.append(Spacer(1, 1.5 * mm))
    return story


def _proctoring(report: Any, styles: dict[str, ParagraphStyle]) -> list[Any]:
    """The Proctoring Report (proctoring spec section 7.2), words only.

    Rendered with its heading even when no report exists, so a reader knows
    the section was looked for rather than wondering whether the page was
    cut short. No icon, no colour code and no severity column: the order of
    the findings is what carries their weight.
    """
    story: list[Any] = [
        Paragraph(PROCTORING_TITLE, styles["Section"]),
        Paragraph(_text(PROCTORING_NOTE), styles["Body"]),
        Spacer(1, 2 * mm),
    ]
    proctoring = _value(report, "proctoring", None)
    # The serializer's own words for where the report stands (not started,
    # still generating, failed, partly unavailable): printed as given, so the
    # PDF and the screen cannot tell different stories.
    state_message = _text(_value(_value(report, "proctoring_state", None), "message", ""))
    if not proctoring:
        story.append(Paragraph(state_message or PROCTORING_ABSENT, styles["Body"]))
        return story
    if state_message:
        story.append(Paragraph(state_message, styles["Body"]))
    elif _value(proctoring, "monitoring_was_incomplete", False):
        story.append(Paragraph(PROCTORING_PARTIAL, styles["Body"]))
    story.append(Spacer(1, 2 * mm))
    for label, key in (
        ("Candidate", "candidate"),
        ("Assessment", "assessment"),
        ("Date", "date_line"),
        ("Outcome", "outcome"),
    ):
        story.append(
            Paragraph(f"<b>{label}:</b> {_text(_value(proctoring, key, ''))}", styles["Body"])
        )
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph("<b>Summary</b>", styles["Body"]))
    story.append(Paragraph(_text(_value(proctoring, "summary", "")), styles["Body"]))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph("<b>Findings</b>", styles["Body"]))
    findings = _value(proctoring, "findings", {}) or {}
    for label, key in (
        ("Screen and Browser Activity", "screen_browser"),
        ("Camera Monitoring", "camera"),
        ("Audio Monitoring", "audio"),
        ("Answer Pattern Analysis", "answer_patterns"),
    ):
        story.append(Paragraph(f"<i>{label}</i>", styles["Body"]))
        for sentence in _value(findings, key, []) or []:
            story.append(Paragraph(f"&bull; {_text(sentence)}", styles["Body"]))
    rows = _value(proctoring, "activity_log", []) or []
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph("<b>Activity log</b>", styles["Body"]))
    if rows:
        table = [
            [
                Paragraph("Time", styles["Body"]),
                Paragraph("What happened", styles["Body"]),
                Paragraph("How long", styles["Body"]),
                Paragraph("What the system did", styles["Body"]),
            ]
        ]
        for row in rows:
            table.append(
                [
                    Paragraph(_text(_value(row, "time", "")), styles["Body"]),
                    Paragraph(_text(_value(row, "what_happened", "")), styles["Body"]),
                    Paragraph(_text(_value(row, "how_long", "")), styles["Body"]),
                    Paragraph(_text(_value(row, "what_the_system_did", "")), styles["Body"]),
                ]
            )
        story.append(
            Table(
                table,
                colWidths=[18 * mm, 66 * mm, 34 * mm, 50 * mm],
                style=TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), PAPER),
                        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#DDDCEB")),
                        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#DDDCEB")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                ),
            )
        )
    else:
        story.append(Paragraph("Nothing was recorded during this assessment.", styles["Body"]))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(_text(_value(proctoring, "closing", "")), styles["Body"]))
    return story


def _claim_evidence(report: Any, styles: dict[str, ParagraphStyle]) -> list[Any]:
    """Evidence vs Claim Summary (0107).

    Rendered with its heading even when the section is empty, for the same
    reason the Proctoring Report is: a reader has to be able to tell a section
    with nothing in it from a page that was cut short. A report written before
    0107 carries neither entries nor an empty-state sentence, and prints
    nothing at all, which is the honest rendering of a section that did not
    exist when the document was written.
    """
    payload = _value(report, "claim_evidence", None) or {}
    entries = list(_value(payload, "entries", []) or [])
    statement = _value(payload, "no_claims_statement", None)
    if not entries and not statement:
        return []
    story: list[Any] = [Paragraph(CLAIM_EVIDENCE_TITLE, styles["Section"])]
    note = _value(payload, "note", "")
    if note:
        story.append(Paragraph(_text(note), styles["Body"]))
        story.append(Spacer(1, 2 * mm))
    if not entries:
        story.append(Paragraph(_text(statement), styles["Body"]))
        return story
    for entry in entries:
        area = _value(entry, "area", "")
        story.append(
            Paragraph(
                f"<b>{_text(_value(entry, 'claim', ''))}</b>"
                + (f"<br/><font color='#64748B'>{_text(area)}</font>" if area else ""),
                styles["Body"],
            )
        )
        story.append(Paragraph(_text(_value(entry, "evidence", "")), styles["Body"]))
        confidence = _value(entry, "confidence", None)
        if confidence:
            story.append(
                Paragraph(
                    "<font color='#64748B'>Evidence confidence: "
                    f"{_text(confidence)}</font>",
                    styles["Body"],
                )
            )
        story.append(Spacer(1, 2.5 * mm))
    return story


def _leadership_alignment(report: Any, styles: dict[str, ParagraphStyle]) -> list[Any]:
    """Leadership Alignment (0135). A report with no such section prints
    nothing at all: a job with no leadership input, or any report written
    before the section existed, reads exactly as it did."""
    payload = _value(report, "leadership_alignment", None)
    if not payload:
        return []
    groups = list(_value(payload, "groups", []) or [])
    story: list[Any] = [Paragraph(LEADERSHIP_ALIGNMENT_TITLE, styles["Section"])]
    note = _value(payload, "note", "")
    if note:
        story.append(Paragraph(_text(note), styles["Body"]))
        story.append(Spacer(1, 2 * mm))
    for group in groups:
        story.append(Paragraph(f"<b>{_text(_value(group, 'title', ''))}</b>", styles["Body"]))
        for line in _value(group, "lines", []) or []:
            story.append(Paragraph(f"&bull; {_text(_value(line, 'text', ''))}", styles["Body"]))
        story.append(Spacer(1, 2.5 * mm))
    return story


def _validation_points(report: Any, styles: dict[str, ParagraphStyle]) -> list[Any]:
    """Recommended Human Validation Points (0107).

    NOT the Gap Analysis above it, and the two sit apart on the page so a
    reader meets them as different lists. That one is grade driven; this one is
    driven by how thin the evidence is, which is why a strongly graded item can
    appear here and never there.
    """
    payload = _value(report, "validation_points", None) or {}
    points = list(_value(payload, "points", []) or [])
    statement = _value(payload, "no_points_statement", None)
    if not points and not statement:
        return []
    story: list[Any] = [Paragraph(VALIDATION_POINTS_TITLE, styles["Section"])]
    note = _value(payload, "note", "")
    if note:
        story.append(Paragraph(_text(note), styles["Body"]))
        story.append(Spacer(1, 2 * mm))
    if not points:
        story.append(Paragraph(_text(statement), styles["Body"]))
        return story
    for point in points:
        confidence = _value(point, "confidence", None)
        story.append(
            Paragraph(
                f"<b>{_text(_value(point, 'area', ''))}</b>"
                + (
                    f" <font color='#64748B'>Evidence confidence: "
                    f"{_text(confidence)}</font>"
                    if confidence
                    else ""
                ),
                styles["Body"],
            )
        )
        story.append(Paragraph(_text(_value(point, "reason", "")), styles["Body"]))
        story.append(
            Paragraph(f"&bull; {_text(_value(point, 'probe', ''))}", styles["Body"])
        )
        story.append(Spacer(1, 2.5 * mm))
    return story


def _remark_cell(row: Any) -> str:
    """The remark, then the server's own markers beside it: a remark a fixed
    template wrote, and a remark its cited evidence did not clearly support."""
    cell = _text(_value(row, "remark"))
    for key in ("remark_note", "support_note"):
        note = _value(row, key)
        if note:
            cell += f"<br/><font color='#64748B'>{_text(note)}</font>"
    return cell


#: The section's PRINTED heading. The key stays `ai_score` (stored payloads
#: carry it); the words a recruiter reads are the product's vocabulary, the
#: same "AI Match" the candidate table uses for the same pre-assessment check
#: (CONTRACT v4 item 5). The screen prints the same words.
AI_MATCH_HEADING = "AI Match"


def _ai_score(report: Any, styles: dict[str, ParagraphStyle]) -> list[Any]:
    """The AI Match section: Yukti's frozen snapshot on a report written from
    the Vivekium release on (a grade word, a header, evidence tags), or the
    four legacy matching rows an older report was written with."""
    snapshot = _value(report, "ai_score_snapshot")
    if not snapshot:
        return _dimension_cards(AI_MATCH_HEADING, _value(report, "ai_score", []), styles)
    grade = _value(snapshot, "grade") or NOT_ASSESSED_WORD
    story: list[Any] = [
        Paragraph(AI_MATCH_HEADING, styles["Section"]),
        Spacer(1, 2 * mm),
        Paragraph(f"<b>{_text(grade)}</b>", styles["Body"]),
    ]
    header = _value(snapshot, "header")
    if header:
        story.append(Paragraph(_text(header), styles["Body"]))
    for tag in _value(snapshot, "tags", []) or []:
        mark = "Supports" if _value(tag, "polarity") == "positive" else "Against"
        story.append(
            Paragraph(f"&bull; {mark}: {_text(_value(tag, 'text'))}", styles["Body"])
        )
    return story


def _dimension_cards(
    title: str,
    rows: Iterable[Any],
    styles: dict[str, ParagraphStyle],
) -> list[Any]:
    items = list(rows)
    if not items:
        return []
    cards: list[list[Any]] = []
    for row in items:
        required = _value(row, "required_level")
        # A skill the evaluation could not complete carries NO grade and says
        # so in the server's own sentence; it is never printed as a grade.
        grade = _value(row, "grade") or NOT_ASSESSED_WORD
        label = f"<b>{_text(_value(row, 'name'))}</b> - {_text(grade)}"
        status_note = _value(row, "status_note")
        if status_note:
            label += f"<br/><font color='#64748B'>{_text(status_note)}</font>"
        if required:
            label += (
                "<br/><font color='#64748B'>Role requires: "
                f"{_text(required)}</font>"
            )
        # EVIDENCE CONFIDENCE (0107), under the grade rather than beside it, so
        # a reader cannot mistake the two words for one compound verdict. The
        # sources are named on the next line because "Low" with nothing behind
        # it is a judgement a reader cannot check.
        #
        # ABSENT ON EVERY REPORT WRITTEN BEFORE 0107, and nothing stands in for
        # it. The report is immutable and the evidence set it was written from
        # is not reconstructable, so the card simply carries no such line, the
        # same way `_chart` simply does not go looking for a fourth chart.
        confidence = _value(row, "evidence_confidence")
        if confidence:
            label += (
                "<br/><font color='#64748B'>Evidence confidence: "
                f"{_text(confidence)}</font>"
            )
        sources = list(_value(row, "evidence_sources", []) or [])
        if sources:
            label += (
                "<br/><font color='#64748B'>Sources: "
                f"{_text(', '.join(str(item) for item in sources))}</font>"
            )
        cards.append(
            [
                Table(
                    [[
                        Paragraph(label, styles["Body"]),
                        Paragraph(_remark_cell(row), styles["Body"]),
                    ]],
                    colWidths=[52 * mm, 116 * mm],
                    style=TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, -1), PAPER),
                            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#DDDCEB")),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 7),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                            ("TOPPADDING", (0, 0), (-1, -1), 7),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                        ]
                    ),
                ),
                Spacer(1, 2.5 * mm),
            ]
        )
    story: list[Any] = [
        KeepTogether(
            [
                Paragraph(title, styles["Section"]),
                Spacer(1, 2 * mm),
                *cards[0],
            ]
        )
    ]
    story.extend(KeepTogether(card) for card in cards[1:])
    return story


def render_report_pdf(
    report: Any,
    *,
    candidate_name: str,
    job_title: str,
    tenant_name: str,
    generated_at: datetime,
) -> bytes:
    """Return a complete report PDF; no underlying numeric score is rendered.

    THE NUMBER BAN RUNS BEFORE ANY BYTE IS WRITTEN (spec-doc6 D8). A PDF is the
    copy that gets forwarded, so a number that reaches one outlives every access
    control on the document it came from, and there is no version of "strip it
    quietly" that leaves the reader able to tell a redaction from an omission.
    The check is at the top of the renderer rather than in a wrapper because the
    download route calls this function directly.
    """
    from app.services.siddhi import numbers

    numbers.assert_clean(report, where="prism.pdf")
    output = io.BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=19 * mm,
        title=f"{REPORT_TITLE} - {candidate_name}",
        author="Vivekium",
    )
    base = getSampleStyleSheet()
    styles = {
        "Title": ParagraphStyle(
            "ReportTitle",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=23,
            leading=28,
            textColor=INK,
            alignment=TA_CENTER,
        ),
        "Subtitle": ParagraphStyle(
            "Subtitle",
            parent=base["BodyText"],
            fontSize=10,
            leading=15,
            textColor=MUTED,
            alignment=TA_CENTER,
        ),
        "Section": ParagraphStyle(
            "Section",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=18,
            textColor=PURPLE,
            spaceBefore=7 * mm,
        ),
        "Body": ParagraphStyle(
            "Body",
            parent=base["BodyText"],
            fontSize=9.5,
            leading=14,
            textColor=INK,
        ),
    }
    # The reference code is printed under the identity block rather than in a
    # corner: a printed report is compared against a row on a screen by eye, and
    # a code the reader has to hunt for gets copied wrong. It identifies a row
    # and authorises nothing.
    reference = _text(_value(report, "reference_code", "") or "")
    story: list[Any] = [
        Paragraph("Vivekium", styles["Subtitle"]),
        Paragraph(REPORT_TITLE, styles["Title"]),
        Paragraph(_text(REPORT_SUBTITLE), styles["Subtitle"]),
        Spacer(1, 3 * mm),
        Paragraph(
            f"<b>{_text(candidate_name)}</b><br/>{_text(job_title)}<br/>"
            f"{_text(tenant_name)}<br/>"
            f"{generated_at.strftime('%d %B %Y')}"
            + (f"<br/><font face='Courier'>{reference}</font>" if reference else ""),
            styles["Subtitle"],
        ),
        Spacer(1, 8 * mm),
    ]

    # Walked, never hand-sequenced: SECTION_ORDER is the order, and a key with
    # no builder here would raise at render rather than silently vanish from a
    # permanent record.
    builders = {
        "ai_score": lambda: _ai_score(report, styles),
        "overall": lambda: _overall(report, styles),
        "must_have": lambda: _dimension_cards(
            "Must-have", _value(report, "must_have", []), styles
        )
        + _chart(report, "must_have", styles),
        "nice_to_have": lambda: _dimension_cards(
            "Nice-to-have", _value(report, "nice_to_have", []), styles
        )
        + _chart(report, "nice_to_have", styles),
        # No chart: the spec gives Behavioural a grade and a remark only.
        "behavioural": lambda: _dimension_cards(
            "Behavioural Competencies", _value(report, "behavioural", []), styles
        ),
        "leadership_alignment": lambda: _leadership_alignment(report, styles),
        "claim_evidence": lambda: _claim_evidence(report, styles),
        "gap_analysis": lambda: _gap_analysis(report, styles),
        "validation_points": lambda: _validation_points(report, styles),
        "validation": lambda: _validation(report, styles),
        "proctoring": lambda: _proctoring(report, styles),
    }
    for key in SECTION_ORDER:
        story.extend(builders[key]())

    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return output.getvalue()
