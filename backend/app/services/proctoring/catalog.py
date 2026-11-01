"""The proctoring event vocabulary and the consequence path of each entry.

FOUR PATHS (proctoring-spec-doc.md section 4.0, and the master prompt's
Phase 3 for the fourth), decided HERE and only here:

    A   immediate termination. Proctoring itself has been defeated, so the
        assessment can no longer be considered valid. No warning, no
        recruiter preference consulted.
    B   the shared three-warning counter. Shared across every Path B type:
        a tab switch, a phone and a second person together use all three,
        deliberately, because separate counters would allow twelve incidents
        before any consequence.
    C   logged only. Never a warning, never a termination.
    P   a PAUSE (2026-09-24). The camera or the microphone stopped. The
        assessment and its clock stop, the candidate is told what to fix and
        has a grace period to fix it (`device_pause.py`). At most
        `device_max_pauses` per session; the next loss, or a pause not
        recovered inside the grace, ends the session as a technical failure
        through one of the two server-derived Path A reasons below. This
        SUPERSEDES the rule that a lost camera or microphone permission, or
        a camera that did not come back, terminated at once.

WHO EMITS WHAT
--------------
The browser emits what only the browser can see: detections, focus, permission
changes, its own integrity. The server derives what only the server can know:
whether an identity mismatch is the second consecutive one, whether a second
voice was heard in two consecutive chunks, whether a heartbeat gap opened,
whether an answer was typed unusually against the candidate's own baseline.
`client_emittable` says which; an event a client sends under a server-derived
type is refused at ingestion rather than trusted.

THREE IDENTIFIERS ARE ADDITIONS TO THE SPECIFICATION'S CATALOG, each for a
rule the specification states in prose without naming an event:

    MONITORING_INTERRUPTED   section 9's heartbeat gap, which "appears in the
                             report"
    INTEGRITY_CHECK_FAILED   section 9's integrity self-check, "failure =
                             event, and after 60 seconds = Path A"
    IDENTITY_CHECK_MISMATCH  one of section 3.3's two consecutive mismatches,
                             recorded so the report can say how the
                             termination was reached
    CAMERA_STREAM_FAILED     section 4.1's stream failure; a pause since
                             2026-09-24

Added 2026-09-24 for the pause path and the audio rules:

    MIC_STREAM_FAILED            the microphone stream stopped (a pause)
    MIC_STREAM_INTERRUPTED       it stopped for less than the glitch window
    DEVICE_RECOVERED             every monitored device is live again; closes
                                 the open pause
    DEVICE_PAUSE_LIMIT_EXCEEDED  a loss arrived with every pause used
    DEVICE_RECOVERY_TIMED_OUT    a pause was not recovered inside the grace
    SPEECH_DURING_NON_AUDIO_QUESTION
                                 speech heard while no spoken answer was being
                                 captured; logged every time, never a warning

These identifiers are INTERNAL. None of them reaches a recruiter; the report
speaks in `phrasing.py`'s sentences.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "PATH_A",
    "PATH_B",
    "PATH_C",
    "PATH_P",
    "EventSpec",
    "CATALOG",
    "spec_for",
    "is_known",
    "CLIENT_EMITTABLE",
    "TERMINATING",
    "WARNING_EVENTS",
    "LOGGED_ONLY",
    "PAUSING",
    "DEVICE_RECOVERED",
    "SOUND_LEVEL",
    "DEVICE_REASONS",
    "INTERRUPTED_FORM",
    "GROUP_SCREEN",
    "GROUP_CAMERA",
    "GROUP_AUDIO",
    "GROUP_ANSWERS",
    "GROUP_SYSTEM",
    "CLIENT_RATE_LIMIT_PER_MINUTE",
]

PATH_A = "A"
PATH_B = "B"
PATH_C = "C"
PATH_P = "P"

#: Report findings groups (section 7.2). Every event belongs to exactly one.
GROUP_SCREEN = "screen_browser"
GROUP_CAMERA = "camera"
GROUP_AUDIO = "audio"
GROUP_ANSWERS = "answer_patterns"
#: Monitoring interruptions and quality notes. Rendered in the summary and
#: the activity log rather than under a findings heading of their own.
GROUP_SYSTEM = "system"


@dataclass(frozen=True)
class EventSpec:
    """One catalog entry.

    `cooldown` names the `ProctoringConfig` field holding the seconds during
    which a repeat of this type (or of its `cooldown_key` family) is logged
    but does not warn again. None means no cooldown applies.
    `once_per_session` means the first occurrence takes the path and every
    later one is recorded on Path C; that is how "more than one display" is
    reported without burning three warnings in three minutes.
    """

    event_type: str
    path: str
    group: str
    client_emittable: bool
    cooldown: str | None = None
    cooldown_key: str | None = None
    once_per_session: bool = False

    @property
    def terminates(self) -> bool:
        return self.path == PATH_A

    @property
    def warns(self) -> bool:
        return self.path == PATH_B

    @property
    def pauses(self) -> bool:
        return self.path == PATH_P


_ENTRIES: tuple[EventSpec, ...] = (
    # ── Path A: immediate termination (section 4.1) ──────────────────────────
    EventSpec("IDENTITY_MISMATCH", PATH_A, GROUP_CAMERA, client_emittable=False),
    EventSpec("CAMERA_OBSTRUCTED", PATH_A, GROUP_CAMERA, client_emittable=True),
    EventSpec("FACE_ABSENT_EXTENDED", PATH_A, GROUP_CAMERA, client_emittable=True),
    EventSpec("INTEGRITY_CHECK_FAILED", PATH_A, GROUP_SYSTEM, client_emittable=True),
    # Server-derived ends of the pause path. Always a technical failure
    # (`ingestion._ALWAYS_TECHNICAL`); the report says which device stopped.
    EventSpec("DEVICE_PAUSE_LIMIT_EXCEEDED", PATH_A, GROUP_SYSTEM, client_emittable=False),
    EventSpec("DEVICE_RECOVERY_TIMED_OUT", PATH_A, GROUP_SYSTEM, client_emittable=False),
    # ── Path P: the device pause (master prompt, Phase 3) ────────────────────
    EventSpec("CAMERA_PERMISSION_LOST", PATH_P, GROUP_CAMERA, client_emittable=True),
    EventSpec("MIC_PERMISSION_LOST", PATH_P, GROUP_AUDIO, client_emittable=True),
    EventSpec("CAMERA_STREAM_FAILED", PATH_P, GROUP_CAMERA, client_emittable=True),
    EventSpec("MIC_STREAM_FAILED", PATH_P, GROUP_AUDIO, client_emittable=True),
    # ── Path B: the shared warning counter (section 4.2) ─────────────────────
    EventSpec("FULLSCREEN_EXITED", PATH_B, GROUP_SCREEN, client_emittable=True),
    EventSpec("WINDOW_FOCUS_LOST", PATH_B, GROUP_SCREEN, client_emittable=True),
    EventSpec(
        "DEVICE_DETECTED_PHONE", PATH_B, GROUP_CAMERA, client_emittable=True,
        cooldown="object_cooldown_seconds", cooldown_key="DEVICE_DETECTED_PHONE",
    ),
    EventSpec(
        "DEVICE_DETECTED_LAPTOP", PATH_B, GROUP_CAMERA, client_emittable=True,
        cooldown="object_cooldown_seconds", cooldown_key="DEVICE_DETECTED_LAPTOP",
    ),
    EventSpec(
        "DEVICE_DETECTED_SCREEN", PATH_B, GROUP_CAMERA, client_emittable=True,
        cooldown="object_cooldown_seconds", cooldown_key="DEVICE_DETECTED_SCREEN",
    ),
    EventSpec(
        "SECOND_PERSON_DETECTED", PATH_B, GROUP_CAMERA, client_emittable=True,
        cooldown="second_person_cooldown_seconds", cooldown_key="SECOND_PERSON_DETECTED",
    ),
    EventSpec("SECOND_VOICE_DETECTED", PATH_B, GROUP_AUDIO, client_emittable=False),
    EventSpec(
        "FACE_ABSENT_MODERATE", PATH_B, GROUP_CAMERA, client_emittable=True,
        cooldown="face_absent_moderate_cooldown_seconds", cooldown_key="FACE_ABSENT_MODERATE",
    ),
    EventSpec(
        "MULTIPLE_DISPLAYS_DETECTED", PATH_B, GROUP_SCREEN, client_emittable=True,
        once_per_session=True,
    ),
    # ── Path C: logged only (section 4.3, 4.4, 9) ────────────────────────────
    EventSpec("FACE_ABSENT_BRIEF", PATH_C, GROUP_CAMERA, client_emittable=True),
    EventSpec("IDENTITY_CHECK_MISMATCH", PATH_C, GROUP_CAMERA, client_emittable=True),
    EventSpec(
        "LOW_LIGHT", PATH_C, GROUP_CAMERA, client_emittable=True,
        cooldown="low_light_cooldown_seconds", cooldown_key="LOW_LIGHT",
    ),
    EventSpec("BLOCKED_ACTION_ATTEMPTED", PATH_C, GROUP_SCREEN, client_emittable=True),
    EventSpec("FAST_TEXT_ENTRY", PATH_C, GROUP_ANSWERS, client_emittable=False),
    EventSpec("UNIFORM_TEXT_ENTRY", PATH_C, GROUP_ANSWERS, client_emittable=False),
    EventSpec("LOW_TYPED_RATIO", PATH_C, GROUP_ANSWERS, client_emittable=False),
    EventSpec("MOUSE_BEHAVIOR_DEVIATION", PATH_C, GROUP_ANSWERS, client_emittable=False),
    EventSpec("AI_TEXT_SIGNAL", PATH_C, GROUP_ANSWERS, client_emittable=False),
    EventSpec("SESSION_QUALITY_DEGRADED", PATH_C, GROUP_SYSTEM, client_emittable=True),
    EventSpec("MONITORING_INTERRUPTED", PATH_C, GROUP_SYSTEM, client_emittable=False),
    EventSpec("INTEGRITY_CHECK_WARNING", PATH_C, GROUP_SYSTEM, client_emittable=True),
    EventSpec("CAMERA_STREAM_INTERRUPTED", PATH_C, GROUP_CAMERA, client_emittable=True),
    EventSpec("MIC_STREAM_INTERRUPTED", PATH_C, GROUP_AUDIO, client_emittable=True),
    # Closes the open device pause (`device_pause.recover`). Path C because a
    # recovery is never a consequence; the SERVER decides whether it came
    # inside the grace.
    EventSpec("DEVICE_RECOVERED", PATH_C, GROUP_SYSTEM, client_emittable=True),
    EventSpec(
        "SPEECH_DURING_NON_AUDIO_QUESTION", PATH_C, GROUP_AUDIO, client_emittable=False
    ),
    # A sustained, uncalibrated microphone level above the room's own floor.
    # An ADVISORY, never a warning: it is not Path B on purpose, so it can
    # never reach the three-warning counter whatever the policy (owner mission
    # 2026-11-01, item B). The browser measures it; `ingestion._loud` refuses
    # a report that is too short, inside the cooldown or during a spoken answer.
    EventSpec(
        "SUSTAINED_HIGH_SOUND_LEVEL", PATH_C, GROUP_AUDIO, client_emittable=True,
        cooldown="loud_cooldown_seconds", cooldown_key="SUSTAINED_HIGH_SOUND_LEVEL",
    ),
)

CATALOG: dict[str, EventSpec] = {entry.event_type: entry for entry in _ENTRIES}

CLIENT_EMITTABLE: frozenset[str] = frozenset(
    entry.event_type for entry in _ENTRIES if entry.client_emittable
)
TERMINATING: frozenset[str] = frozenset(e.event_type for e in _ENTRIES if e.terminates)
WARNING_EVENTS: frozenset[str] = frozenset(e.event_type for e in _ENTRIES if e.warns)
LOGGED_ONLY: frozenset[str] = frozenset(e.event_type for e in _ENTRIES if e.path == PATH_C)
PAUSING: frozenset[str] = frozenset(e.event_type for e in _ENTRIES if e.pauses)

DEVICE_RECOVERED = "DEVICE_RECOVERED"
SOUND_LEVEL = "SUSTAINED_HIGH_SOUND_LEVEL"
#: The two ways the pause path ends a session.
DEVICE_REASONS: frozenset[str] = frozenset(
    {"DEVICE_PAUSE_LIMIT_EXCEEDED", "DEVICE_RECOVERY_TIMED_OUT"}
)
#: What a loss shorter than the glitch window is recorded as. The camera and
#: the microphone each have their own interruption, so the report names the
#: device that flickered.
INTERRUPTED_FORM: dict[str, str] = {
    "CAMERA_PERMISSION_LOST": "CAMERA_STREAM_INTERRUPTED",
    "CAMERA_STREAM_FAILED": "CAMERA_STREAM_INTERRUPTED",
    "MIC_PERMISSION_LOST": "MIC_STREAM_INTERRUPTED",
    "MIC_STREAM_FAILED": "MIC_STREAM_INTERRUPTED",
}

#: Abuse ceiling on client-emitted events per session per minute. A browser
#: emitting more than this is broken or hostile; the surplus is refused with
#: a 429 rather than stored. Not a threshold about candidate behaviour, so
#: it lives with the vocabulary rather than in Settings.
CLIENT_RATE_LIMIT_PER_MINUTE = 600


def spec_for(event_type: str) -> EventSpec:
    """The catalog entry, or a KeyError naming the unknown identifier."""
    return CATALOG[event_type]


def is_known(event_type: str) -> bool:
    return event_type in CATALOG


if len(CATALOG) != len(_ENTRIES):  # pragma: no cover - an import-time contract
    raise ImportError("proctoring catalog has a duplicated event type")
if set(INTERRUPTED_FORM) != PAUSING or not set(INTERRUPTED_FORM.values()) <= LOGGED_ONLY:
    raise ImportError(  # pragma: no cover - an import-time contract
        "every pausing event needs a logged interruption form"
    )
