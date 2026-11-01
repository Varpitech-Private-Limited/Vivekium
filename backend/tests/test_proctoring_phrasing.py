"""Every sentence a recruiter or a candidate can read (spec 7.1, 7.3, 7.4, 8.3).

    "Never appears in recruiter-facing output: strike, tier, violation, flag,
     anomaly, signal, confidence, threshold, severity, event type codes, model
     names, confidence percentages, character counts, millisecond timings, or
     any phrasing implying the candidate cheated."

THE SWEEP IS THE POINT. Reading three example sentences proves nothing about
the fourth, so this file GENERATES every sentence the library can produce, at
every count and every duration bucket, and checks all of them. A template
added without the word "flag" in it is a template a test has already read.

The second half is that the sentences say something. A library that returned
the empty string would pass every prohibition, so each generator is also
asserted to name the thing that happened and, where the specification says so,
what the system did about it.
"""
from __future__ import annotations

import re

import pytest

from app.models.proctoring import (
    OUTCOME_ABANDONED,
    OUTCOME_COMPLETED,
    OUTCOME_TECHNICAL_FAILURE,
    OUTCOME_TERMINATED_INTEGRITY,
    OUTCOME_TERMINATED_WARNINGS,
    POLICY_CONTINUE_AND_NOTE,
    POLICY_TERMINATE,
    SESSION_OUTCOMES,
    WARNING_POLICIES,
)
from app.services.proctoring import catalog, phrasing

EM_DASH = chr(8212)

#: The counts and durations the report actually renders at. The duration list
#: crosses every bucket boundary in `duration_phrase`.
COUNTS = (0, 1, 2, 3, 7, 20, 41)
DURATIONS = (None, 0, 1_200, 9_000, 20_000, 30_000, 60_000, 300_000, 1_500_000, 7_200_000)

#: Every event type the report can phrase, which is every catalog entry.
EVENT_TYPES = tuple(sorted(catalog.CATALOG))
#: Every event type that can produce a live candidate warning: the Path B set
#: minus the two the server derives without a candidate-facing message.
WARNABLE = tuple(sorted(catalog.WARNING_EVENTS - {"SECOND_VOICE_DETECTED"}))


def _every_recruiter_sentence() -> list[tuple[str, str]]:
    """(where it came from, the sentence) for the whole library."""
    produced: list[tuple[str, str]] = []
    for event_type in EVENT_TYPES:
        for times in COUNTS:
            for duration in DURATIONS:
                produced.append(
                    (
                        f"finding:{event_type}:{times}:{duration}",
                        phrasing.finding_sentence(
                            event_type, times=times, duration_ms=duration
                        ),
                    )
                )
        produced.append((f"activity:{event_type}", phrasing.activity_description(event_type)))
    for outcome in SESSION_OUTCOMES:
        for warnings in COUNTS:
            for reason in (None, "IDENTITY_MISMATCH", "CAMERA_OBSTRUCTED", "CAMERA_STREAM_FAILED"):
                produced.append(
                    (
                        f"outcome:{outcome}:{warnings}:{reason}",
                        phrasing.outcome_sentence(
                            outcome, warnings=warnings, termination_reason=reason
                        ),
                    )
                )
    for path in (catalog.PATH_A, catalog.PATH_B, catalog.PATH_C):
        for issued in (True, False):
            for number in (None, 1, 2, 3):
                for terminated in (True, False):
                    produced.append(
                        (
                            f"action:{path}:{issued}:{number}:{terminated}",
                            phrasing.system_action(
                                path=path,
                                warning_issued=issued,
                                warning_number=number,
                                terminated=terminated,
                            ),
                        )
                    )
    for count in COUNTS:
        produced.append((f"count:{count}", phrasing.count_word(count)))
        produced.append((f"ordinal:{count}", phrasing.ordinal_word(count)))
    for duration in DURATIONS:
        produced.append((f"duration:{duration}", phrasing.duration_phrase(duration)))
    for name in (
        "NO_ISSUES", "NO_EVENTS_AT_ALL", "CLOSING", "INFORMATIONAL_NOTE",
        "AUDIO_UNAVAILABLE", "MONITORING_REDUCED_RATE",
        "CAMERA_ANALYSIS_FAILING", "MONITORING_COMPONENT_STOPPED",
    ):
        produced.append((name, getattr(phrasing, name)))
    for state_name, message in sorted(phrasing.REPORT_STATE_MESSAGES.items()):
        produced.append((f"report_state:{state_name}", message))
    produced.append(("activity:camera_analysis_failing", phrasing.CAMERA_ANALYSIS_FAILING_ACTIVITY))
    return produced


def _every_candidate_sentence() -> list[tuple[str, str]]:
    produced: list[tuple[str, str]] = []
    for event_type in WARNABLE:
        for number in (1, 2, 3):
            for policy in WARNING_POLICIES:
                produced.append(
                    (
                        f"warning:{event_type}:{number}:{policy}",
                        phrasing.warning_message(
                            event_type, number=number, max_warnings=3, policy=policy
                        ),
                    )
                )
    for reason in sorted(catalog.TERMINATING):
        produced.append(
            (
                f"termination:{reason}",
                phrasing.termination_message(reason, outcome=OUTCOME_TERMINATED_INTEGRITY),
            )
        )
    produced.append(
        (
            "termination:warnings",
            phrasing.termination_message("FULLSCREEN_EXITED", outcome=OUTCOME_TERMINATED_WARNINGS),
        )
    )
    return produced


ALL_SENTENCES = _every_recruiter_sentence() + _every_candidate_sentence()


def test_the_sweep_actually_generated_the_library() -> None:
    """A sweep over an empty list passes forever. This repository has shipped
    that exact failure: six secret-hygiene assertions reported SKIPPED while
    reading nothing."""
    assert len(ALL_SENTENCES) > 900, len(ALL_SENTENCES)
    assert len({s for _, s in ALL_SENTENCES}) > 60


@pytest.mark.parametrize("word", phrasing.FORBIDDEN_WORDS)
def test_no_forbidden_word_appears_in_any_sentence(word: str) -> None:
    offenders = [
        f"{where}: {sentence!r}"
        for where, sentence in ALL_SENTENCES
        if re.search(rf"(?<![a-z]){re.escape(word)}", sentence, re.IGNORECASE)
    ]
    assert not offenders, (
        f"section 7.1 forbids {word!r} in anything a recruiter or candidate "
        f"reads:\n  " + "\n  ".join(offenders[:8])
    )


def test_no_sentence_contains_a_digit() -> None:
    """Counts and durations are spelled out. The only digits in the whole
    report are the clock times in the date line and the activity log, and
    `report.py` renders those itself rather than through this library."""
    offenders = [
        f"{where}: {sentence!r}"
        for where, sentence in ALL_SENTENCES
        if any(char.isdigit() for char in sentence)
    ]
    assert not offenders, offenders[:8]


def test_no_sentence_contains_an_em_dash() -> None:
    offenders = [where for where, sentence in ALL_SENTENCES if EM_DASH in sentence]
    assert not offenders, offenders


def test_no_internal_identifier_reaches_a_sentence() -> None:
    """The event catalog is explicitly internal ("never shown to recruiters").
    A template that fell through to printing its key would be caught here."""
    offenders = [
        f"{where}: {sentence!r}"
        for where, sentence in ALL_SENTENCES
        for identifier in catalog.CATALOG
        if identifier in sentence
    ]
    assert not offenders, offenders[:8]


def test_every_sentence_is_a_real_sentence() -> None:
    """A library that returned "" would pass every prohibition above."""
    for where, sentence in ALL_SENTENCES:
        assert sentence.strip(), where
        assert "{" not in sentence and "}" not in sentence, f"{where}: unfilled slot"


# ── The sentences say the right thing ────────────────────────────────────────


@pytest.mark.parametrize("event_type", EVENT_TYPES)
def test_every_catalog_entry_has_recruiter_phrasing(event_type: str) -> None:
    """Section 7.4 requires a phrasing per event. A missing entry would raise
    at report time, on a session that already happened."""
    assert phrasing.finding_sentence(event_type, times=1, duration_ms=30_000)
    assert phrasing.activity_description(event_type)


def test_an_unknown_event_type_raises_rather_than_printing_its_key() -> None:
    with pytest.raises(KeyError):
        phrasing.finding_sentence("NOT_AN_EVENT", times=1, duration_ms=None)
    with pytest.raises(KeyError):
        phrasing.activity_description("NOT_AN_EVENT")


@pytest.mark.parametrize(
    "count,expected", [(0, "never"), (1, "once"), (2, "twice"), (3, "three times")]
)
def test_counts_are_spelled_the_way_english_spells_them(count: int, expected: str) -> None:
    assert phrasing.count_word(count) == expected


def test_a_count_beyond_the_table_degrades_to_words_not_digits() -> None:
    assert phrasing.count_word(500) == "more than twenty times"
    assert phrasing.ordinal_word(50) == "number more than twenty"


@pytest.mark.parametrize(
    "ms,expected",
    [
        (None, "a moment"),
        (1_200, "a few seconds"),
        (12_000, "about ten seconds"),
        (30_000, "about half a minute"),
        (60_000, "about a minute"),
        (300_000, "about five minutes"),
        (1_800_000, "about half an hour"),
        (7_200_000, "about two hours"),
    ],
)
def test_durations_are_approximate_and_human(ms: int | None, expected: str) -> None:
    """Section 7.1: "about 12 seconds," never "12,412 ms"."""
    assert phrasing.duration_phrase(ms) == expected


# ── Outcomes (section 7.3) ───────────────────────────────────────────────────


def test_a_clean_completion_says_so_plainly_and_positively() -> None:
    sentence = phrasing.outcome_sentence(OUTCOME_COMPLETED, warnings=0, termination_reason=None)
    assert sentence == "Assessment completed with no issues detected."


def test_a_completion_with_warnings_states_how_many_in_words() -> None:
    sentence = phrasing.outcome_sentence(OUTCOME_COMPLETED, warnings=2, termination_reason=None)
    assert "completed" in sentence and "twice" in sentence


def test_a_warning_termination_names_the_recruiters_own_setting() -> None:
    """The recruiter chose this. A sentence that did not say so reads as the
    product having made the decision for them."""
    sentence = phrasing.outcome_sentence(
        OUTCOME_TERMINATED_WARNINGS, warnings=3, termination_reason="FULLSCREEN_EXITED"
    )
    assert "your setting for this role" in sentence


def test_an_identity_termination_says_what_could_not_be_confirmed() -> None:
    sentence = phrasing.outcome_sentence(
        OUTCOME_TERMINATED_INTEGRITY, warnings=0, termination_reason="IDENTITY_MISMATCH"
    )
    assert "could not confirm that the same person" in sentence


def test_a_camera_termination_reads_as_a_camera_problem() -> None:
    for reason in ("CAMERA_OBSTRUCTED", "FACE_ABSENT_EXTENDED", "CAMERA_PERMISSION_LOST"):
        sentence = phrasing.outcome_sentence(
            OUTCOME_TERMINATED_INTEGRITY, warnings=0, termination_reason=reason
        )
        assert "camera was covered or unavailable" in sentence, reason


def test_a_technical_failure_is_never_presented_as_the_candidates_doing() -> None:
    """Section 7.3: "That last row matters. A candidate whose laptop camera
    died must never be presented as suspicious." """
    sentence = phrasing.outcome_sentence(
        OUTCOME_TECHNICAL_FAILURE, warnings=0, termination_reason="CAMERA_STREAM_FAILED"
    )
    assert "technical problem, not candidate behaviour" in sentence


def test_an_abandoned_assessment_says_what_happened_without_blame() -> None:
    sentence = phrasing.outcome_sentence(OUTCOME_ABANDONED, warnings=0, termination_reason=None)
    assert "not completed" in sentence
    assert "did not return" in sentence


@pytest.mark.parametrize("outcome", SESSION_OUTCOMES)
def test_every_outcome_has_a_sentence(outcome: str) -> None:
    assert phrasing.outcome_sentence(outcome, warnings=1, termination_reason=None).strip()


# ── Live warnings (section 8.3) ──────────────────────────────────────────────


@pytest.mark.parametrize("event_type", WARNABLE)
def test_a_first_warning_says_what_happened_and_what_to_do(event_type: str) -> None:
    message = phrasing.warning_message(
        event_type, number=1, max_warnings=3, policy=POLICY_CONTINUE_AND_NOTE
    )
    assert "Please" in message, message
    assert "first of three warnings" in message, message


@pytest.mark.parametrize("event_type", WARNABLE)
def test_a_second_warning_warns_that_one_more_may_end_it(event_type: str) -> None:
    message = phrasing.warning_message(
        event_type, number=2, max_warnings=3, policy=POLICY_CONTINUE_AND_NOTE
    )
    assert "second of three warnings" in message, message
    assert "one more and your assessment may end" in message, message


@pytest.mark.parametrize("event_type", WARNABLE)
def test_the_third_warning_states_the_consequence_the_policy_chose(event_type: str) -> None:
    """The two branches say different things, and the difference is the whole
    reason the setting exists."""
    stopped = phrasing.warning_message(
        event_type, number=3, max_warnings=3, policy=POLICY_TERMINATE
    )
    noted = phrasing.warning_message(
        event_type, number=3, max_warnings=3, policy=POLICY_CONTINUE_AND_NOTE
    )
    assert "Your assessment has ended." in stopped
    assert "You may continue" in noted and "noted in your report" in noted
    assert stopped != noted


def test_a_warning_is_specific_rather_than_vague() -> None:
    """Section 8.3: "A phone was detected on camera. Please move it out of
    view." Never vague, never accusatory."""
    message = phrasing.warning_message(
        "DEVICE_DETECTED_PHONE", number=1, max_warnings=3, policy=POLICY_CONTINUE_AND_NOTE
    )
    assert "phone" in message.lower()
    assert "move it out of view" in message


def test_an_event_with_no_candidate_message_raises_rather_than_inventing_one() -> None:
    with pytest.raises(KeyError):
        phrasing.warning_message(
            "LOW_LIGHT", number=1, max_warnings=3, policy=POLICY_CONTINUE_AND_NOTE
        )


# ── Terminations the candidate reads (section 4.1) ───────────────────────────


@pytest.mark.parametrize("reason", sorted(catalog.TERMINATING))
def test_every_path_a_reason_has_a_plain_language_screen(reason: str) -> None:
    """Section 4.1: "show the candidate a clear plain-language screen
    explaining the assessment has ended and why"."""
    message = phrasing.termination_message(reason, outcome=OUTCOME_TERMINATED_INTEGRITY)
    assert "Your assessment has ended because" in message
    assert "answers up to this point have been saved" in message


def test_a_warning_termination_reads_as_the_limit_rather_than_a_fault() -> None:
    message = phrasing.termination_message(
        "FULLSCREEN_EXITED", outcome=OUTCOME_TERMINATED_WARNINGS
    )
    assert "warning limit for this role was reached" in message


def test_an_unknown_termination_reason_raises() -> None:
    with pytest.raises(KeyError):
        phrasing.termination_message("LOW_LIGHT", outcome=OUTCOME_TERMINATED_INTEGRITY)


def test_the_blocked_action_sentence_does_not_claim_blocking_is_unbreakable() -> None:
    """Section 4.4: "Do not represent it as unbreakable anywhere in the code
    comments, docs, or UI." """
    sentence = phrasing.finding_sentence(
        "BLOCKED_ACTION_ATTEMPTED", times=2, duration_ms=None
    )
    for overclaim in ("cannot", "impossible", "prevented entirely", "unbreakable"):
        assert overclaim not in sentence.lower(), sentence


def test_the_ai_text_sentence_carries_its_hedge() -> None:
    """Section 3.5: informational only, and the unreliability must be stated
    in the sentence itself rather than assumed known by the reader."""
    sentence = phrasing.finding_sentence("AI_TEXT_SIGNAL", times=1, duration_ms=None)
    assert "not reliable enough to draw conclusions from on its own" in sentence


def test_the_fast_entry_sentence_offers_the_benign_reading() -> None:
    """Section 7.4: "Never let the report push toward a conclusion." """
    sentence = phrasing.finding_sentence("FAST_TEXT_ENTRY", times=1, duration_ms=None)
    assert "planned their answer in advance" in sentence


def test_the_low_typed_ratio_sentence_names_assistive_input_first() -> None:
    """The same rule, on the finding most likely to be read as an accusation:
    dictation and assistive input produce it, and a report that did not say so
    would be pushing toward a conclusion."""
    sentence = phrasing.finding_sentence("LOW_TYPED_RATIO", times=1, duration_ms=None)
    assert "dictation" in sentence and "assistive" in sentence


def test_the_closing_line_disclaims_any_effect_on_the_score() -> None:
    assert "does not affect this candidate's score or ranking" in phrasing.CLOSING
    assert "entirely your decision" in phrasing.CLOSING


def test_the_system_action_names_which_warning_it_was() -> None:
    assert phrasing.system_action(
        path=catalog.PATH_B, warning_issued=True, warning_number=2, terminated=False
    ) == "Issued the second warning"
    assert phrasing.system_action(
        path=catalog.PATH_A, warning_issued=False, warning_number=None, terminated=True
    ) == "Ended the assessment"
    assert phrasing.system_action(
        path=catalog.PATH_C, warning_issued=False, warning_number=None, terminated=False
    ) == "Noted it"
