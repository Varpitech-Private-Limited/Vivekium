"""The PRISM PDF's Proctoring Report section tells the truth about its state.

The PDF is built from the one serializer output (`prism_view.report_out`), so
it prints the serializer's own words for where the proctoring report stands:
no session, still generating, failed, partly unavailable. A report with no
proctoring and no state still prints the absent sentence, so a reader can tell
the section was looked for. The section stays last, informational, and free of
numbers.
"""
from __future__ import annotations

import pytest

from app.services import report_pdf
from app.services.siddhi import numbers
from tests.test_report_sections_0107 import _legacy_report, _pdf_text

PROCTORING = {
    "candidate": "Fixture Candidate",
    "assessment": "Platform Engineer",
    "date_line": "1 November 2026",
    "outcome": "Completed with two notices",
    "summary": "The session was monitored and two notices were recorded.",
    "findings": {
        "screen_browser": ["The window lost focus once."],
        "camera": ["A second device was seen briefly."],
        "audio": [],
        "answer_patterns": [],
    },
    "activity_log": [
        {
            "time": "10:04",
            "what_happened": "The window lost focus",
            "how_long": "A few seconds",
            "what_the_system_did": "Showed the candidate a notice",
        }
    ],
    "closing": "This report is informational and moves no grade.",
    "monitoring_was_incomplete": False,
}


def _with(**fields) -> dict:
    return {**_legacy_report(), **fields}


def test_no_proctoring_and_no_state_prints_the_absent_sentence() -> None:
    text = _pdf_text(_with())
    assert report_pdf.PROCTORING_TITLE in text
    assert report_pdf.PROCTORING_ABSENT in text


@pytest.mark.parametrize(
    "message",
    [
        "The Proctoring Report is still being prepared.",
        "The Proctoring Report could not be generated.",
        "No proctoring session was recorded for this assessment.",
    ],
)
def test_a_missing_report_prints_the_serializers_state_sentence(message: str) -> None:
    text = " ".join(
        _pdf_text(_with(proctoring_state={"state": "generating", "message": message})).split()
    )
    assert message in text
    assert report_pdf.PROCTORING_ABSENT not in text


def test_a_present_report_prints_its_findings_and_the_informational_note() -> None:
    text = _pdf_text(_with(proctoring=PROCTORING))
    flat = " ".join(text.split())
    assert "Informational only" in flat
    assert "The window lost focus once." in flat
    assert "A second device was seen briefly." in flat
    assert "Showed the candidate a notice" in flat


def test_a_partial_report_says_the_monitoring_was_incomplete() -> None:
    partial = {**PROCTORING, "monitoring_was_incomplete": True}
    flat = " ".join(_pdf_text(_with(proctoring=partial)).split())
    assert report_pdf.PROCTORING_PARTIAL in flat
    sentence = "Audio analysis was unavailable for part of the session."
    flat = " ".join(
        _pdf_text(
            _with(
                proctoring=partial,
                proctoring_state={"state": "partially_unavailable", "message": sentence},
            )
        ).split()
    )
    assert sentence in flat
    assert report_pdf.PROCTORING_PARTIAL not in flat


def test_the_proctoring_section_is_last_in_the_order_and_in_the_document() -> None:
    assert report_pdf.SECTION_ORDER[-1] == "proctoring"
    text = _pdf_text(_with(proctoring=PROCTORING))
    assert text.rindex(report_pdf.PROCTORING_TITLE) > text.index("PRISM Report")


def test_a_number_in_the_proctoring_block_refuses_the_pdf() -> None:
    dirty = {**PROCTORING, "risk_score": 0.82}
    with pytest.raises(numbers.NumberInDeliveredReport):
        _pdf_text(_with(proctoring=dirty))
