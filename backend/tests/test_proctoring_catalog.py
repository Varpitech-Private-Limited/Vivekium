"""Path A / B / C / P classification for every catalog entry (spec section 4).

The consequence path is the most important single fact about an event, and
the specification writes it down in three tables; the master prompt's Phase 3
adds the fourth, the device pause. This file restates those tables
INDEPENDENTLY of `catalog.py` so a change to one has to be made in both, and
checks the schema refuses an identifier the browser may not emit.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.proctoring import EventBatchIn, EventIn
from app.services.proctoring import catalog

#: Section 4.1, plus the Path A addition section 9 states in prose (a failed
#: integrity check), MINUS the three device losses the master prompt moved to
#: the pause path, PLUS the two server-derived ways that path ends a session.
SPEC_PATH_A = {
    "IDENTITY_MISMATCH",
    "CAMERA_OBSTRUCTED",
    "FACE_ABSENT_EXTENDED",
    "INTEGRITY_CHECK_FAILED",
    "DEVICE_PAUSE_LIMIT_EXCEEDED",
    "DEVICE_RECOVERY_TIMED_OUT",
}
#: Master prompt, Phase 3: "Camera or microphone loss triggers a pause, an
#: explicit warning and a 2-minute grace."
SPEC_PATH_P = {
    "CAMERA_PERMISSION_LOST",
    "MIC_PERMISSION_LOST",
    "CAMERA_STREAM_FAILED",
    "MIC_STREAM_FAILED",
}
#: Section 4.2.
SPEC_PATH_B = {
    "FULLSCREEN_EXITED",
    "WINDOW_FOCUS_LOST",
    "DEVICE_DETECTED_PHONE",
    "DEVICE_DETECTED_LAPTOP",
    "DEVICE_DETECTED_SCREEN",
    "SECOND_PERSON_DETECTED",
    "SECOND_VOICE_DETECTED",
    "FACE_ABSENT_MODERATE",
    "MULTIPLE_DISPLAYS_DETECTED",
}
#: Sections 4.3, 4.4 and 9.
SPEC_PATH_C = {
    "FACE_ABSENT_BRIEF",
    "IDENTITY_CHECK_MISMATCH",
    "LOW_LIGHT",
    "BLOCKED_ACTION_ATTEMPTED",
    "FAST_TEXT_ENTRY",
    "UNIFORM_TEXT_ENTRY",
    "LOW_TYPED_RATIO",
    "MOUSE_BEHAVIOR_DEVIATION",
    "AI_TEXT_SIGNAL",
    "SESSION_QUALITY_DEGRADED",
    "MONITORING_INTERRUPTED",
    "INTEGRITY_CHECK_WARNING",
    "CAMERA_STREAM_INTERRUPTED",
    "MIC_STREAM_INTERRUPTED",
    "DEVICE_RECOVERED",
    "SPEECH_DURING_NON_AUDIO_QUESTION",
    # Advisory, owner mission 2026-11-01 item B: deliberately NOT Path B.
    "SUSTAINED_HIGH_SOUND_LEVEL",
}

#: Derived by the SERVER (section 9: "The client requests a warning; the
#: server decides"). A client that sends one of these is refused at the
#: schema, before any rule runs.
SERVER_ONLY = {
    "IDENTITY_MISMATCH",
    "SECOND_VOICE_DETECTED",
    "FAST_TEXT_ENTRY",
    "UNIFORM_TEXT_ENTRY",
    "LOW_TYPED_RATIO",
    "MOUSE_BEHAVIOR_DEVIATION",
    "AI_TEXT_SIGNAL",
    "MONITORING_INTERRUPTED",
    "DEVICE_PAUSE_LIMIT_EXCEEDED",
    "DEVICE_RECOVERY_TIMED_OUT",
    "SPEECH_DURING_NON_AUDIO_QUESTION",
}


def test_the_catalog_is_exactly_the_four_tables() -> None:
    tables = (SPEC_PATH_A, SPEC_PATH_B, SPEC_PATH_C, SPEC_PATH_P)
    assert set(catalog.CATALOG) == set().union(*tables)
    for index, first in enumerate(tables):
        for second in tables[index + 1:]:
            assert not (first & second), f"an event has two paths: {first & second}"


@pytest.mark.parametrize("event_type", sorted(SPEC_PATH_A))
def test_path_a_terminates(event_type: str) -> None:
    spec = catalog.spec_for(event_type)
    assert spec.path == catalog.PATH_A
    assert spec.terminates and not spec.warns
    assert event_type in catalog.TERMINATING


@pytest.mark.parametrize("event_type", sorted(SPEC_PATH_B))
def test_path_b_warns(event_type: str) -> None:
    spec = catalog.spec_for(event_type)
    assert spec.path == catalog.PATH_B
    assert spec.warns and not spec.terminates
    assert event_type in catalog.WARNING_EVENTS


@pytest.mark.parametrize("event_type", sorted(SPEC_PATH_C))
def test_path_c_is_logged_only(event_type: str) -> None:
    spec = catalog.spec_for(event_type)
    assert spec.path == catalog.PATH_C
    assert not spec.warns and not spec.terminates and not spec.pauses
    assert event_type in catalog.LOGGED_ONLY


@pytest.mark.parametrize("event_type", sorted(SPEC_PATH_P))
def test_path_p_pauses_and_never_terminates_or_warns(event_type: str) -> None:
    spec = catalog.spec_for(event_type)
    assert spec.path == catalog.PATH_P
    assert spec.pauses and not spec.warns and not spec.terminates
    assert event_type in catalog.PAUSING
    assert event_type not in catalog.TERMINATING
    assert spec.client_emittable, "only the browser can see its own devices stop"


def test_every_pausing_loss_has_a_logged_interruption_of_the_same_device() -> None:
    """A flicker under the glitch window is recorded against the device that
    flickered, never against the other one."""
    assert set(catalog.INTERRUPTED_FORM) == SPEC_PATH_P
    for loss, interruption in catalog.INTERRUPTED_FORM.items():
        assert interruption in SPEC_PATH_C
        assert ("MIC" in loss) == ("MIC" in interruption), (loss, interruption)


def test_the_speech_rule_can_never_warn_or_end_an_assessment() -> None:
    """Master prompt: speaking during a non-audio question "never counts
    toward termination". Pinned on the catalog entry, because the path is the
    whole of what an event can do."""
    spec = catalog.spec_for("SPEECH_DURING_NON_AUDIO_QUESTION")
    assert spec.path == catalog.PATH_C
    assert not spec.client_emittable


def test_every_entry_belongs_to_exactly_one_findings_group() -> None:
    groups = {
        catalog.GROUP_SCREEN, catalog.GROUP_CAMERA, catalog.GROUP_AUDIO,
        catalog.GROUP_ANSWERS, catalog.GROUP_SYSTEM,
    }
    for spec in catalog.CATALOG.values():
        assert spec.group in groups, spec.event_type


def test_the_cooldowns_the_spec_names_exist_and_point_at_config_fields() -> None:
    """Section 4.2: object and second-person detections cool down for thirty
    seconds, moderate absence for sixty, and the multiple-display check is
    reported once. Without them a phone on the desk burns three warnings in
    six seconds."""
    from app.services.proctoring.config import ProctoringConfig

    for event_type in ("DEVICE_DETECTED_PHONE", "DEVICE_DETECTED_LAPTOP", "DEVICE_DETECTED_SCREEN"):
        assert catalog.spec_for(event_type).cooldown == "object_cooldown_seconds"
    assert catalog.spec_for("SECOND_PERSON_DETECTED").cooldown == "second_person_cooldown_seconds"
    assert catalog.spec_for("FACE_ABSENT_MODERATE").cooldown == "face_absent_moderate_cooldown_seconds"
    assert catalog.spec_for("MULTIPLE_DISPLAYS_DETECTED").once_per_session
    field_names = {f for f in ProctoringConfig.__dataclass_fields__}
    for spec in catalog.CATALOG.values():
        if spec.cooldown:
            assert spec.cooldown in field_names, spec.event_type
            assert spec.cooldown_key, spec.event_type


@pytest.mark.parametrize("event_type", sorted(SERVER_ONLY))
def test_a_client_cannot_emit_a_server_derived_event(event_type: str) -> None:
    assert not catalog.spec_for(event_type).client_emittable
    with pytest.raises(ValidationError):
        EventIn(event_type=event_type, occurred_at=datetime.now(timezone.utc))


def test_an_unknown_identifier_is_refused_at_the_schema() -> None:
    with pytest.raises(ValidationError):
        EventIn(event_type="SOMETHING_ELSE", occurred_at=datetime.now(timezone.utc))
    assert not catalog.is_known("SOMETHING_ELSE")


def test_a_batch_must_carry_at_least_one_event() -> None:
    with pytest.raises(ValidationError):
        EventBatchIn(events=[])


def test_event_metadata_is_bounded_to_a_label() -> None:
    """A frame is megabytes; a label is bytes. The metadata field is the one
    place a client could smuggle an image through, and it is capped."""
    with pytest.raises(ValidationError):
        EventIn(
            event_type="LOW_LIGHT",
            occurred_at=datetime.now(timezone.utc),
            metadata={"frame": "x" * 5000},
        )
