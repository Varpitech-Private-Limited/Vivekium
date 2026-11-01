"""The sentence library (proctoring-spec-doc.md sections 7.3, 7.4 and 8.3).

TWO AUDIENCES, ONE FILE, BECAUSE THE RULES ARE THE SAME
-------------------------------------------------------
The candidate reads a warning; the recruiter reads a report. Both are plain
language, both name what happened and what the system did, and neither ever
carries an internal identifier, a count in digits, a duration in
milliseconds, a model name or a word that implies the candidate cheated. The
specification lists the forbidden vocabulary (section 7.1) and
`tests/test_proctoring_phrasing.py` sweeps every sentence this module can
produce against it, so a sentence added here without the word "flag" is a
sentence a test has read.

"Implement as a lookup, not string concatenation scattered through code."
Every template lives in a table keyed by catalog event type; the functions
below fill the table entries and spell the numbers. A caller that wants a
sentence asks for it by event type and never assembles one.

COUNTS AND DURATIONS ARE WORDS. "twice", "about half a minute". The only digits
allowed anywhere in a report are clock times in the date line and the activity
log, which `report.py` renders itself. Nothing in this file emits a digit, and
the test asserts that over the whole table at every count and duration.
"""
from __future__ import annotations

from app.models.proctoring import (
    OUTCOME_ABANDONED,
    OUTCOME_COMPLETED,
    OUTCOME_TECHNICAL_FAILURE,
    OUTCOME_TERMINATED_INTEGRITY,
    OUTCOME_TERMINATED_WARNINGS,
    POLICY_TERMINATE,
)
from app.services.proctoring import catalog
from app.services.proctoring.config import ProctoringConfig

__all__ = [
    "count_word",
    "ordinal_word",
    "duration_phrase",
    "finding_sentence",
    "activity_description",
    "system_action",
    "outcome_sentence",
    "warning_message",
    "termination_message",
    "pause_message",
    "pause_summary",
    "speech_highlight",
    "candidate_rules",
    "blocked_kind",
    "blocked_item_sentence",
    "BLOCKED_KINDS",
    "NO_ISSUES",
    "NO_EVENTS_AT_ALL",
    "CLOSING",
    "INFORMATIONAL_NOTE",
    "BLOCKED_ACTIONS_NOTE",
    "AUDIO_UNAVAILABLE",
    "MONITORING_REDUCED_RATE",
    "CAMERA_ANALYSIS_FAILING",
    "MONITORING_COMPONENT_STOPPED",
    "FORBIDDEN_WORDS",
]

#: Section 7.1's forbidden vocabulary, plus the words that imply a verdict.
#: Read by the phrasing test, and by nothing else: this list is what the
#: sentences below are checked against, not something the code consults.
FORBIDDEN_WORDS: tuple[str, ...] = (
    "strike",
    "tier",
    "violation",
    "flag",
    "anomaly",
    "anomalous",
    "signal",
    "confidence",
    "threshold",
    "severity",
    "cheat",
    "cheated",
    "cheating",
    "fraud",
    "suspicious",
    "dishonest",
    "misconduct",
    "event_type",
    "path a",
    "path b",
    "path c",
    "%",
    "percent",
    "ms",
    "millisecond",
)

NO_ISSUES = "No issues detected."
NO_EVENTS_AT_ALL = "No issues were detected during this assessment."
CLOSING = (
    "This report reflects only what the system detected during the "
    "assessment. It does not affect this candidate's score or ranking; how "
    "much weight to give it is entirely your decision."
)
INFORMATIONAL_NOTE = (
    "Final section of the PRISM Report. Informational only: it does not "
    "affect this candidate's score or ranking."
)
#: Stated only when attempts occurred (section 7.2). Section 4.4 is explicit
#: that browser-level blocking stops an ordinary candidate and not a determined
#: one, so the sentence says what is blocked and never that blocking is
#: unbreakable.
BLOCKED_ACTIONS_NOTE = (
    "Copy-paste, right-click, and developer tools are blocked during the "
    "assessment. The candidate attempted these {times}."
)
AUDIO_UNAVAILABLE = (
    "Audio monitoring was not available for this assessment, so a second "
    "voice could not have been detected."
)
MONITORING_REDUCED_RATE = (
    "The candidate's device could not keep up with the usual monitoring rate, "
    "so camera monitoring ran at a reduced rate for part of the assessment."
)
CAMERA_ANALYSIS_FAILING = (
    "Camera analysis was failing on the candidate's device for part of the "
    "assessment, so a phone or another person on camera may not have been "
    "detected during that time."
)
MONITORING_COMPONENT_STOPPED = (
    "Part of the monitoring stopped responding for a time and recovered, so "
    "some activity may not have been checked during that period."
)

_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
)
_ORDINALS = (
    "zeroth", "first", "second", "third", "fourth", "fifth", "sixth",
    "seventh", "eighth", "ninth", "tenth",
)
_MS_PER_SECOND = 1000
_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60


def number_word(n: int) -> str:
    """A small integer in words; beyond the table it is 'more than twenty'."""
    n = max(0, int(n))
    if n < len(_ONES):
        return _ONES[n]
    return f"more than {_ONES[-1]}"


def count_word(n: int) -> str:
    """'once', 'twice', 'three times', ... 'more than twenty times'."""
    n = max(0, int(n))
    if n == 0:
        return "never"
    if n == 1:
        return "once"
    if n == 2:
        return "twice"
    return f"{number_word(n)} times"


def ordinal_word(n: int) -> str:
    n = max(0, int(n))
    if n < len(_ORDINALS):
        return _ORDINALS[n]
    return f"number {number_word(n)}"


def duration_phrase(total_ms: int | None) -> str:
    """An approximate, human duration. Never a figure in milliseconds."""
    if total_ms is None or total_ms <= 0:
        return "a moment"
    seconds = total_ms / _MS_PER_SECOND
    if seconds < 5:
        return "a few seconds"
    if seconds < 15:
        return "about ten seconds"
    if seconds < 25:
        return "about twenty seconds"
    if seconds < 45:
        return "about half a minute"
    if seconds < 90:
        return "about a minute"
    minutes = seconds / _SECONDS_PER_MINUTE
    if minutes < 20:
        return f"about {number_word(round(minutes))} minutes"
    if minutes < 45:
        return "about half an hour"
    if minutes < 90:
        return "about an hour"
    hours = minutes / _MINUTES_PER_HOUR
    return f"about {number_word(round(hours))} hours"


# ── Recruiter phrasing (section 7.4) ─────────────────────────────────────────
#
# `{times}` is the count in words and `{duration}` the total duration phrase.
# A template with neither describes a fact whose count carries no meaning
# (more than one screen is connected or it is not).

_FINDINGS: dict[str, str] = {
    "FULLSCREEN_EXITED": "Left the assessment screen {times}, for about {duration}.",
    "WINDOW_FOCUS_LOST": "Left the assessment screen {times}, for about {duration}.",
    "DEVICE_DETECTED_PHONE": "A phone was visible on camera {times}, for about {duration}.",
    "DEVICE_DETECTED_LAPTOP": "A laptop was visible on camera {times}, for about {duration}.",
    "DEVICE_DETECTED_SCREEN": (
        "A second screen or television was visible on camera {times}, for "
        "about {duration}."
    ),
    "SECOND_PERSON_DETECTED": "Another person appeared on camera {times}, for about {duration}.",
    "SECOND_VOICE_DETECTED": "Another voice was heard {times} during the assessment.",
    "FACE_ABSENT_BRIEF": "The candidate was out of camera view {times}, for about {duration}.",
    "FACE_ABSENT_MODERATE": "The candidate was out of camera view {times}, for about {duration}.",
    "FACE_ABSENT_EXTENDED": (
        "The candidate was out of camera view for about {duration}, and the "
        "assessment was ended."
    ),
    "MULTIPLE_DISPLAYS_DETECTED": (
        "More than one screen was connected to the candidate's computer."
    ),
    "IDENTITY_MISMATCH": (
        "The system could not confirm that the same person remained present "
        "throughout, and the assessment was ended."
    ),
    "IDENTITY_CHECK_MISMATCH": (
        "A routine identity check did not match the candidate's appearance at "
        "the start {times}. A single check can differ because of lighting or "
        "the angle of the camera."
    ),
    "CAMERA_OBSTRUCTED": (
        "The camera was covered for about {duration}, and the assessment was "
        "ended."
    ),
    # The four losses PAUSE the assessment (catalog Path P, 2026-09-24). The
    # sentence says what stopped and how often; whether each pause was
    # recovered is the pause sentence's job (`pause_summary`), so a loss is
    # never described as having ended the assessment when it did not.
    "CAMERA_PERMISSION_LOST": (
        "Camera access was switched off {times} during the assessment."
    ),
    "MIC_PERMISSION_LOST": (
        "Microphone access was switched off {times} during the assessment."
    ),
    "CAMERA_STREAM_FAILED": (
        "The camera stopped working {times} during the assessment."
    ),
    "MIC_STREAM_FAILED": (
        "The microphone stopped working {times} during the assessment."
    ),
    "CAMERA_STREAM_INTERRUPTED": (
        "The camera stream was interrupted {times}, for about {duration}, and "
        "recovered."
    ),
    "MIC_STREAM_INTERRUPTED": (
        "The microphone stream was interrupted {times}, for about {duration}, "
        "and recovered."
    ),
    "DEVICE_RECOVERED": (
        "The camera and microphone were restored {times} after the assessment "
        "had paused."
    ),
    "DEVICE_PAUSE_LIMIT_EXCEEDED": (
        "The camera or microphone stopped again after every pause allowed had "
        "been used, so the assessment was ended."
    ),
    "DEVICE_RECOVERY_TIMED_OUT": (
        "The camera or microphone was not restored within the time allowed, so "
        "the assessment was ended."
    ),
    "SPEECH_DURING_NON_AUDIO_QUESTION": (
        "The candidate spoke {times} while answering questions that do not "
        "take a spoken answer, for about {duration} in total. Speaking is "
        "noted and never ends an assessment; it can be the candidate thinking "
        "aloud or reading a question to themselves."
    ),
    "SUSTAINED_HIGH_SOUND_LEVEL": (
        "The microphone picked up a sustained high sound level {times}, for "
        "about {duration} in total. This is a rough reading of the microphone "
        "and cannot tell shouting from a loud room, a fan, music or the "
        "microphone's own volume settings, so it is noted for context only. "
        "It never earns a warning and never ends an assessment."
    ),
    "INTEGRITY_CHECK_FAILED": (
        "The monitoring components stopped responding and did not recover, so "
        "the assessment could not continue."
    ),
    "INTEGRITY_CHECK_WARNING": (
        "The monitoring components stopped responding {times}, for about "
        "{duration}, and recovered."
    ),
    "MONITORING_INTERRUPTED": (
        "Monitoring was interrupted for about {duration} during the assessment."
    ),
    "SESSION_QUALITY_DEGRADED": MONITORING_REDUCED_RATE,
    "LOW_LIGHT": (
        "Lighting was poor for part of the assessment, which reduces what the "
        "camera can see. This is context about the room, not about the candidate."
    ),
    "BLOCKED_ACTION_ATTEMPTED": BLOCKED_ACTIONS_NOTE,
    "FAST_TEXT_ENTRY": (
        "{answers} completed far faster than the candidate's own typing "
        "elsewhere in the assessment makes likely. This can also happen when a "
        "candidate has planned their answer in advance."
    ),
    "UNIFORM_TEXT_ENTRY": (
        "{answers} entered in a single uninterrupted stretch with almost no "
        "corrections, which is unusual. This can also happen when a candidate "
        "has planned their answer in advance."
    ),
    "LOW_TYPED_RATIO": (
        "{answers} contained more text than the keystrokes recorded for it "
        "would produce. This can happen with dictation software or an "
        "assistive input method as well as with text brought in from elsewhere."
    ),
    "MOUSE_BEHAVIOR_DEVIATION": (
        "{answers} showed pointer movement very different from the "
        "candidate's own pattern earlier in the assessment."
    ),
    "AI_TEXT_SIGNAL": (
        "Part of {answers_lower} showed some characteristics of AI-generated "
        "text. This particular check is not reliable enough to draw "
        "conclusions from on its own."
    ),
}


def _answers_phrase(n: int) -> str:
    return "One answer was" if n <= 1 else f"{number_word(n).capitalize()} answers were"


def _answers_lower(n: int) -> str:
    return "one written answer" if n <= 1 else f"{number_word(n)} written answers"


def finding_sentence(event_type: str, *, times: int, duration_ms: int | None) -> str:
    """The recruiter-facing sentence for `times` occurrences of one event type
    totalling `duration_ms`. Raises on an event type with no phrasing rather
    than printing the identifier, because the identifier is the one thing
    section 7.1 says a recruiter must never see."""
    if event_type not in catalog.CATALOG:
        raise KeyError(f"{event_type!r} is not a proctoring event type")
    template = _FINDINGS[event_type]
    return template.format(
        times=count_word(times),
        duration=duration_phrase(duration_ms),
        answers=_answers_phrase(times),
        answers_lower=_answers_lower(times),
    )


# ── Activity log (section 7.2) ───────────────────────────────────────────────

_ACTIVITY: dict[str, str] = {
    "FULLSCREEN_EXITED": "Left fullscreen",
    "WINDOW_FOCUS_LOST": "Left the assessment window",
    "DEVICE_DETECTED_PHONE": "A phone was visible on camera",
    "DEVICE_DETECTED_LAPTOP": "A laptop was visible on camera",
    "DEVICE_DETECTED_SCREEN": "A second screen or television was visible on camera",
    "SECOND_PERSON_DETECTED": "Another person appeared on camera",
    "SECOND_VOICE_DETECTED": "Another voice was heard",
    "FACE_ABSENT_BRIEF": "Briefly out of camera view",
    "FACE_ABSENT_MODERATE": "Out of camera view",
    "FACE_ABSENT_EXTENDED": "Out of camera view for an extended period",
    "MULTIPLE_DISPLAYS_DETECTED": "More than one screen connected",
    "IDENTITY_MISMATCH": "The same person could not be confirmed present",
    "IDENTITY_CHECK_MISMATCH": "An identity check did not match",
    "CAMERA_OBSTRUCTED": "The camera was covered",
    "CAMERA_PERMISSION_LOST": "Camera access was switched off",
    "MIC_PERMISSION_LOST": "Microphone access was switched off",
    "CAMERA_STREAM_FAILED": "The camera stopped working",
    "MIC_STREAM_FAILED": "The microphone stopped working",
    "CAMERA_STREAM_INTERRUPTED": "The camera stream was interrupted and recovered",
    "MIC_STREAM_INTERRUPTED": "The microphone stream was interrupted and recovered",
    "DEVICE_RECOVERED": "The camera and microphone were restored",
    "DEVICE_PAUSE_LIMIT_EXCEEDED": "A device stopped again after every pause allowed was used",
    "DEVICE_RECOVERY_TIMED_OUT": "A device was not restored within the time allowed",
    "SPEECH_DURING_NON_AUDIO_QUESTION": (
        "Spoke during a question that does not take a spoken answer"
    ),
    "SUSTAINED_HIGH_SOUND_LEVEL": "The microphone picked up a sustained high sound level",
    "INTEGRITY_CHECK_FAILED": "Monitoring stopped responding and did not recover",
    "INTEGRITY_CHECK_WARNING": "Monitoring stopped responding and recovered",
    "MONITORING_INTERRUPTED": "Monitoring was interrupted",
    "SESSION_QUALITY_DEGRADED": "Monitoring ran at a reduced rate",
    "LOW_LIGHT": "Lighting was poor",
    "BLOCKED_ACTION_ATTEMPTED": "A blocked action was attempted",
    "FAST_TEXT_ENTRY": "An answer was typed much faster than the candidate's usual pace",
    "UNIFORM_TEXT_ENTRY": "An answer was typed in one uninterrupted stretch",
    "LOW_TYPED_RATIO": "An answer contained more text than was typed",
    "MOUSE_BEHAVIOR_DEVIATION": "Pointer movement differed from the candidate's usual pattern",
    "AI_TEXT_SIGNAL": "An answer showed some characteristics of AI-generated text",
}

#: Once per session the audio note is the one degraded-quality event that is
#: about the platform rather than the device.
AUDIO_UNAVAILABLE_NOTE = "audio_analysis_unavailable"
AUDIO_FAILED_NOTE = "audio_analysis_failed"
AUDIO_ACTIVITY = "Audio monitoring was not available"
#: The browser's camera analysis failed on frame after frame (`camera.ts`). A
#: phone or a second person could have gone unnoticed for that stretch, so the
#: report says so rather than reading as an all-clear.
CAMERA_ANALYSIS_FAILING_NOTE = "camera_analysis_failing"
CAMERA_ANALYSIS_FAILING_ACTIVITY = "Camera analysis was failing"


def activity_description(event_type: str, metadata: dict | None = None) -> str:
    if event_type not in catalog.CATALOG:
        raise KeyError(f"{event_type!r} is not a proctoring event type")
    note = (metadata or {}).get("note")
    if event_type == "SESSION_QUALITY_DEGRADED" and note in (AUDIO_UNAVAILABLE_NOTE, AUDIO_FAILED_NOTE):
        return AUDIO_ACTIVITY
    if event_type == "SESSION_QUALITY_DEGRADED" and note == CAMERA_ANALYSIS_FAILING_NOTE:
        return CAMERA_ANALYSIS_FAILING_ACTIVITY
    if event_type == "BLOCKED_ACTION_ATTEMPTED":
        return _BLOCKED_ACTIVITY[blocked_kind(metadata)]
    return _ACTIVITY[event_type]


# ── Blocked actions, itemised (master prompt, Phase 3: every paste attempt is
# blocked, logged as a proctoring event, and shown in the report) ─────────────
#
# The browser names the action it refused (`frontend/lib/proctoring/lockdown`
# `BlockedAction`). The report counts the actions the specification names on
# their own lines and folds every other refusal into one, so a recruiter reads
# "tried to paste three times" rather than a total that hides it. An action the
# browser did not name, or named with a word this table does not know, is
# counted under "another blocked action": still counted, never dropped.

BLOCKED_PASTE = "paste"
BLOCKED_COPY = "copy_or_cut"
BLOCKED_DROP = "drop"
BLOCKED_OTHER = "other"
#: The order the itemised sentences appear in the report.
BLOCKED_KINDS: tuple[str, ...] = (BLOCKED_PASTE, BLOCKED_COPY, BLOCKED_DROP, BLOCKED_OTHER)

_BLOCKED_KIND_FOR_ACTION: dict[str, str] = {
    "paste": BLOCKED_PASTE,
    "copy": BLOCKED_COPY,
    "cut": BLOCKED_COPY,
    "drop": BLOCKED_DROP,
}

_BLOCKED_ITEMS: dict[str, str] = {
    BLOCKED_PASTE: "Tried to paste into an answer {times}.",
    BLOCKED_COPY: "Tried to copy or cut text {times}.",
    BLOCKED_DROP: "Tried to drag and drop text or a file into the page {times}.",
    BLOCKED_OTHER: (
        "Tried another blocked action, such as right-click, printing or "
        "developer tools, {times}."
    ),
}

_BLOCKED_ACTIVITY: dict[str, str] = {
    BLOCKED_PASTE: "Tried to paste into an answer",
    BLOCKED_COPY: "Tried to copy or cut text",
    BLOCKED_DROP: "Tried to drag and drop into the page",
    BLOCKED_OTHER: "A blocked action was attempted",
}


def blocked_kind(metadata: dict | None) -> str:
    """Which itemised line a blocked-action event is counted on."""
    action = (metadata or {}).get("action")
    if not isinstance(action, str):
        return BLOCKED_OTHER
    return _BLOCKED_KIND_FOR_ACTION.get(action.strip().lower(), BLOCKED_OTHER)


def blocked_item_sentence(kind: str, *, times: int) -> str:
    if kind not in _BLOCKED_ITEMS:
        raise KeyError(f"{kind!r} is not a blocked-action kind")
    return _BLOCKED_ITEMS[kind].format(times=count_word(times))


def system_action(
    *,
    path: str,
    warning_issued: bool,
    warning_number: int | None,
    terminated: bool,
    paused: bool = False,
    resumed: bool = False,
) -> str:
    """The "What the system did" cell."""
    if terminated:
        return "Ended the assessment"
    if warning_issued and warning_number is not None:
        return f"Issued the {ordinal_word(warning_number)} warning"
    if paused:
        return "Paused the assessment"
    if resumed:
        return "Resumed the assessment"
    if path == catalog.PATH_B:
        return "Noted it, no further warning"
    return "Noted it"


# ── The device pause, for the recruiter ──────────────────────────────────────


def pause_summary(*, pauses: int, resumed: int, ended: bool) -> str | None:
    """One sentence about the pauses a session had, or None when it had none.

    Monitoring is incomplete while an assessment is paused, so this sentence is
    stated rather than left to be inferred from the device findings.
    """
    if pauses <= 0:
        return None
    base = (
        f"The assessment was paused {count_word(pauses)} while the camera or "
        "microphone was restored"
    )
    if ended:
        return base + ", and it did not continue after the last pause."
    if resumed >= pauses:
        if pauses == 1:
            return base + ", and it resumed."
        return base + ", and it resumed each time."
    return base + "."


def speech_highlight(times: int) -> str:
    """The summary line for speaking during questions that do not take a
    spoken answer, once it has happened often enough to be highlighted."""
    return (
        f"The candidate spoke {count_word(times)} during questions that do not "
        "take a spoken answer, which is highlighted because it happened "
        "repeatedly. It did not affect whether the assessment continued."
    )


# ── Outcome (section 7.3) ────────────────────────────────────────────────────

_CAMERA_REASONS = frozenset(
    {"CAMERA_OBSTRUCTED", "FACE_ABSENT_EXTENDED", "CAMERA_PERMISSION_LOST", "MIC_PERMISSION_LOST"}
)


def outcome_sentence(outcome: str, *, warnings: int, termination_reason: str | None) -> str:
    if outcome == OUTCOME_COMPLETED:
        if warnings <= 0:
            return "Assessment completed with no issues detected."
        return (
            "Assessment completed. The candidate was warned "
            f"{count_word(warnings)} during the session."
        )
    if outcome == OUTCOME_TERMINATED_WARNINGS:
        return (
            "Assessment ended early. The candidate crossed the warning limit "
            "and, per your setting for this role, the assessment was stopped."
        )
    if outcome == OUTCOME_TERMINATED_INTEGRITY:
        if termination_reason == "IDENTITY_MISMATCH":
            return (
                "Assessment ended early. The system could not confirm that the "
                "same person remained present throughout."
            )
        if termination_reason in _CAMERA_REASONS:
            return (
                "Assessment ended early. The camera was covered or unavailable "
                "for an extended period."
            )
        return (
            "Assessment ended early. Monitoring could not continue, and the "
            "assessment was stopped."
        )
    if outcome == OUTCOME_TECHNICAL_FAILURE:
        if termination_reason in catalog.DEVICE_REASONS:
            # Not "a technical problem, not candidate behaviour": a camera
            # that died and a camera switched off end the same way, and this
            # sentence must not vouch for which it was. The findings name the
            # device and what happened to it.
            return (
                "Assessment ended early because the camera or microphone "
                "stopped and was not restored within the rules the candidate "
                "was shown before starting."
            )
        return (
            "Assessment could not be completed due to a technical problem, not "
            "candidate behaviour."
        )
    if outcome == OUTCOME_ABANDONED:
        return (
            "Assessment was not completed. The candidate stopped answering and "
            "did not return."
        )
    return "Assessment is still in progress."


# ── Candidate-facing warnings (section 8.3) ──────────────────────────────────
#
# (what happened, what to do). Specific and actionable, never accusatory.

_WARNINGS: dict[str, tuple[str, str]] = {
    "FULLSCREEN_EXITED": (
        "The assessment left fullscreen",
        "return to fullscreen and stay there",
    ),
    "WINDOW_FOCUS_LOST": (
        "You left the assessment window",
        "keep this window in front until you finish",
    ),
    "DEVICE_DETECTED_PHONE": (
        "A phone was detected on camera",
        "move it out of view",
    ),
    "DEVICE_DETECTED_LAPTOP": (
        "A laptop was detected on camera",
        "move it out of view",
    ),
    "DEVICE_DETECTED_SCREEN": (
        "A second screen was detected on camera",
        "move it out of view or switch it off",
    ),
    "SECOND_PERSON_DETECTED": (
        "Another person was detected on camera",
        "make sure you are alone in the room",
    ),
    "SECOND_VOICE_DETECTED": (
        "Another voice was heard",
        "make sure nobody else is speaking near you",
    ),
    "FACE_ABSENT_MODERATE": (
        "Your face was out of camera view",
        "stay in front of the camera",
    ),
    "MULTIPLE_DISPLAYS_DETECTED": (
        "More than one screen is connected to your computer",
        "disconnect the extra screen",
    ),
}


def warning_message(
    event_type: str, *, number: int, max_warnings: int, policy: str
) -> str:
    """The whole warning, composed server-side so the client never words it.

    First and second warnings tell the candidate where they stand; the last
    one says what the recruiter's setting decided.
    """
    if event_type not in _WARNINGS:
        raise KeyError(f"{event_type!r} is not a warning event")
    what, do = _WARNINGS[event_type]
    total = number_word(max_warnings)
    if number >= max_warnings:
        if policy == POLICY_TERMINATE:
            return f"{what}. Your assessment has ended."
        return f"{what}. You may continue, but this has been noted in your report."
    if number == 1:
        return f"{what}. Please {do}. This is your first of {total} warnings."
    remaining = max_warnings - number
    if remaining == 1:
        tail = "one more and your assessment may end"
    else:
        tail = f"{number_word(remaining)} more and your assessment may end"
    return (
        f"{what} again. Please {do}. This is your {ordinal_word(number)} of "
        f"{total} warnings, {tail}."
    )


# ── Candidate-facing termination (section 4.1) ───────────────────────────────

_TERMINATIONS: dict[str, str] = {
    "IDENTITY_MISMATCH": (
        "Your assessment has ended because the system could not confirm that "
        "the same person stayed in front of the camera."
    ),
    "CAMERA_OBSTRUCTED": (
        "Your assessment has ended because the camera was covered for too long."
    ),
    "FACE_ABSENT_EXTENDED": (
        "Your assessment has ended because you were out of camera view for too "
        "long."
    ),
    "CAMERA_PERMISSION_LOST": (
        "Your assessment has ended because camera access was switched off."
    ),
    "MIC_PERMISSION_LOST": (
        "Your assessment has ended because microphone access was switched off."
    ),
    "CAMERA_STREAM_FAILED": (
        "Your assessment has ended because the camera stopped working and could "
        "not be restarted."
    ),
    "INTEGRITY_CHECK_FAILED": (
        "Your assessment has ended because monitoring stopped working and could "
        "not be restored."
    ),
    "DEVICE_PAUSE_LIMIT_EXCEEDED": (
        "Your assessment has ended because your camera or microphone stopped "
        "again after every pause allowed had been used."
    ),
    "DEVICE_RECOVERY_TIMED_OUT": (
        "Your assessment has ended because your camera or microphone was not "
        "working again within the time allowed."
    ),
}
_TERMINATION_WARNINGS = (
    "Your assessment has ended because the warning limit for this role was "
    "reached."
)
_TERMINATION_TAIL = (
    " Your answers up to this point have been saved, and the hiring team has "
    "been told why the assessment ended."
)


def termination_message(reason_code: str, *, outcome: str) -> str:
    if outcome == OUTCOME_TERMINATED_WARNINGS:
        return _TERMINATION_WARNINGS + _TERMINATION_TAIL
    if reason_code not in _TERMINATIONS:
        raise KeyError(f"{reason_code!r} is not a termination reason")
    return _TERMINATIONS[reason_code] + _TERMINATION_TAIL


# ── Candidate-facing: the device pause (master prompt, Phase 3) ──────────────

_DEVICE_NAME: dict[str, str] = {
    "CAMERA_PERMISSION_LOST": "camera",
    "CAMERA_STREAM_FAILED": "camera",
    "MIC_PERMISSION_LOST": "microphone",
    "MIC_STREAM_FAILED": "microphone",
}
_DEVICE_FIX: dict[str, str] = {
    "CAMERA_PERMISSION_LOST": "allow camera access for this page again in your browser",
    "CAMERA_STREAM_FAILED": (
        "check that your camera is connected and that no other application is using it"
    ),
    "MIC_PERMISSION_LOST": "allow microphone access for this page again in your browser",
    "MIC_STREAM_FAILED": (
        "check that your microphone is connected and that no other application is using it"
    ),
}
_ANY_DEVICE_FIX = (
    "check that your camera and microphone are connected and allowed for this "
    "page in your browser"
)


def pause_message(
    opened_by: str | None, *, pauses_used: int, max_pauses: int, grace_seconds: int
) -> str:
    """What the candidate reads while the assessment is paused.

    Names the device and what to do, gives the time allowed in words (the
    screen shows the countdown beside it), and says plainly what the next stop
    will mean, because the candidate was told the rule before starting and
    must not learn it for the first time at the moment it ends their session.
    """
    device = _DEVICE_NAME.get(opened_by or "", "camera or microphone")
    fix = _DEVICE_FIX.get(opened_by or "", _ANY_DEVICE_FIX)
    window = duration_phrase(grace_seconds * _MS_PER_SECOND)
    remaining = max_pauses - pauses_used
    if remaining <= 0:
        tail = (
            "This is the last pause allowed: if your camera or microphone stops "
            "again, the assessment will end."
        )
    elif remaining == 1:
        tail = (
            "One more pause is allowed after this one. After that, the "
            "assessment will end if your camera or microphone stops again."
        )
    else:
        tail = (
            f"{number_word(remaining).capitalize()} more pauses are allowed after "
            "this one."
        )
    return (
        f"Your {device} has stopped, so the assessment is paused and its clock "
        f"has stopped. You have {window} to fix it: {fix}. {tail} Your answers "
        "so far are saved."
    )


def candidate_rules(config: ProctoringConfig) -> list[str]:
    """The monitoring rules, read to the candidate before they start.

    Composed from the SAME configuration the server enforces, so the screen
    can never describe a rule the pipeline is not applying: change the grace
    or the pause count and this text changes with it. Served by
    `GET /proctoring/config`; the consent screen renders it and words nothing
    itself.
    """
    grace = duration_phrase(config.device_grace_seconds * _MS_PER_SECOND)
    warnings = number_word(config.max_warnings)
    if config.device_max_pauses > 0:
        pause_rule = (
            "If your camera or microphone stops, the assessment pauses, its "
            f"clock stops, and you have {grace} to fix it. This can happen at "
            f"most {count_word(config.device_max_pauses)}. A further stop, or "
            f"not fixing it within {grace}, ends the assessment."
        )
    else:
        pause_rule = (
            "If your camera or microphone stops, the assessment ends, so check "
            "both before you start."
        )
    sources = "Leaving the window, and a phone or another person on camera,"
    if config.audio_analysis_available:
        sources = (
            "Leaving the window, a phone or another person on camera, and "
            "another voice nearby"
        )
    rules = [
        "Your camera and microphone must stay on for the whole assessment.",
        (
            "Stay in fullscreen and keep this window in front until you finish. "
            f"{sources} each earn a warning. You have {warnings} warnings, and "
            "the hiring team has decided what happens at the last one."
        ),
        pause_rule,
    ]
    if config.audio_analysis_available:
        rules.append(
            "Make sure nobody else is speaking near you. Another voice heard "
            "clearly earns a warning; your own voice never does."
        )
        rules.append(
            "Speak only when a question asks for a spoken answer. Speaking at "
            "other times is noted in the report, but it never ends your "
            "assessment."
        )
    rules.extend(
        [
            (
                "If the sound level around you stays high for a while, a short "
                "reminder appears on screen and the report notes it. It is a "
                "note only and never earns a warning. Your spoken answers are "
                "not counted."
            ),
            (
                "Copying, cutting, pasting and dragging text are blocked, and "
                "every attempt is noted in the report."
            ),
            (
                "Questions come one at a time in a fixed order. You cannot skip a "
                "question or change an answer once you submit it."
            ),
            (
                "Each question has its own time limit, measured by our server. "
                "When the time runs out, whatever you have written is submitted."
            ),
        ]
    )
    return rules


# ── The state of the Proctoring Report in the PRISM Report ───────────────────

#: Keyed by `report.STATE_*`. A finished, complete report has no entry: the
#: report speaks for itself. Every other state says what is true, and none says
#: "no issues", because absence of a report is not absence of events.
REPORT_STATE_MESSAGES: dict[str, str] = {
    "no_session": (
        "No proctoring session was recorded for this assessment, so there is no "
        "Proctoring Report. Nothing can be said either way about how it was taken."
    ),
    "in_progress": (
        "The monitored session has not ended yet. The Proctoring Report is "
        "written once it does."
    ),
    "generating": (
        "The Proctoring Report is being prepared and will appear here shortly."
    ),
    "failed": (
        "The Proctoring Report could not be produced yet. It is retried "
        "automatically, and nothing should be read into its absence."
    ),
    "partially_unavailable": (
        "Monitoring was incomplete for part of this assessment, so the "
        "Proctoring Report below describes less than the whole session."
    ),
    "no_events": (
        "Monitoring ran for the whole assessment and nothing was recorded."
    ),
}
