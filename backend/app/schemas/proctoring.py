"""Proctoring API shapes (proctoring-spec-doc.md sections 5, 7, 8, 9).

THE CANDIDATE SIDE IS EVENTS ONLY. `EventIn` carries an identifier, a time, a
duration, a confidence and a small metadata object. There is no field for a
frame, an image, a descriptor of anything but a face at session creation, or
any text a candidate typed: keystroke capture arrives as timing offsets on the
answer itself (`schemas/assessments.AnswerBehaviourIn`), never as characters.

THE RECRUITER SIDE IS WORDS ONLY. `ProctoringReportOut` has no numeric field
at any path, because it travels inside the delivered PRISM payload under
`schemas/reports.NumberFreeDelivery`, and because the specification's own
language rule (section 7.1) forbids counts, timings and confidences in the
recruiter's view. Counts are spelled out; durations are approximate.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.proctoring import FACE_DESCRIPTOR_WIDTH
from app.services.proctoring import catalog

__all__ = [
    "DeviceContextIn",
    "SystemCheckIn",
    "SessionCreateIn",
    "SessionOut",
    "ProctoringConfigOut",
    "PauseOut",
    "WarningAckOut",
    "EventIn",
    "EventBatchIn",
    "WarningOut",
    "TerminationOut",
    "IngestOut",
    "MonitoringStateIn",
    "HeartbeatIn",
    "HeartbeatOut",
    "AudioChunkOut",
    "ProctoringFindingsOut",
    "ActivityLogRowOut",
    "ProctoringReportOut",
]

#: The most metadata one event may carry. Aggregates and labels, never media.
MAX_METADATA_BYTES = 2_000


class DeviceContextIn(BaseModel):
    """Browser, OS, screen count, camera resolution: context for interpreting
    session quality, never identity."""

    model_config = ConfigDict(extra="forbid")

    user_agent: str = Field(default="", max_length=400)
    platform: str = Field(default="", max_length=100)
    screen_count: int | None = Field(default=None, ge=1, le=16)
    screen_width: int | None = Field(default=None, ge=1, le=20_000)
    screen_height: int | None = Field(default=None, ge=1, le=20_000)
    camera_width: int | None = Field(default=None, ge=1, le=10_000)
    camera_height: int | None = Field(default=None, ge=1, le=10_000)
    hardware_concurrency: int | None = Field(default=None, ge=1, le=256)
    webgl: bool | None = None


class SystemCheckIn(BaseModel):
    """The six pre-start checks (section 8.2), each pass or fail. The server
    refuses to create a session unless every one passed; a failed check is the
    client's screen to resolve, with fix instructions, before it retries."""

    model_config = ConfigDict(extra="forbid")

    camera: bool
    microphone: bool
    browser_supported: bool
    fullscreen_supported: bool
    face_detected: bool
    inference_adequate: bool
    #: The measured inference rate, so a slow device is recorded as degraded
    #: rather than refused (section 3.6).
    measured_fps: float | None = Field(default=None, ge=0, le=120)

    @property
    def all_passed(self) -> bool:
        return all(
            (
                self.camera,
                self.microphone,
                self.browser_supported,
                self.fullscreen_supported,
                self.face_detected,
                self.inference_adequate,
            )
        )


class SessionCreateIn(BaseModel):
    """POST /proctoring/links/{link_id}/session."""

    model_config = ConfigDict(extra="forbid")

    #: Must be literally true. The consent screen's "I understand and agree".
    consent: bool
    device_context: DeviceContextIn
    system_check: SystemCheckIn
    #: The 128-float face descriptor captured at the system check. A vector,
    #: not an image, and refused at any other width.
    face_descriptor: list[float] = Field(min_length=FACE_DESCRIPTOR_WIDTH, max_length=FACE_DESCRIPTOR_WIDTH)

    @field_validator("consent")
    @classmethod
    def _consent_is_explicit(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("consent must be given explicitly")
        return value


class PauseOut(BaseModel):
    """The device pause, as the candidate's screen shows it.

    While `paused` the assessment waits: answering is refused, the clock is
    stopped, and `message` says which device to fix and by when. The two
    counts are operational (how many of the allowed pauses are used), the same
    kind of figure as the warning counter beside them; no assessment number
    travels here.
    """

    paused: bool
    message: str | None = None
    grace_deadline_at: datetime | None = None
    pauses_used: int
    max_pauses: int


class WarningAckOut(BaseModel):
    """POST /proctoring/sessions/{id}/warnings/ack. `resumed` is true when a
    warning pause was open and this acknowledgement closed it."""

    resumed: bool


class SessionOut(BaseModel):
    session_id: uuid.UUID
    conversation_id: uuid.UUID
    status: str
    warnings_used: int
    max_warnings: int
    #: The job's third-warning policy, so the client can word the final
    #: warning correctly. `terminate` or `continue_and_note`.
    warning_policy: str
    consented_at: datetime
    #: The browser-side thresholds (`services/proctoring/config.CLIENT_FIELDS`).
    config: dict[str, Any]
    #: Whether a second voice can be detected in this deployment. When false
    #: the client does not upload audio, and the report says so.
    audio_analysis_available: bool
    #: The device pause as it stands, so a page reloaded in the middle of a
    #: pause shows the pause screen rather than the question.
    pause: PauseOut


class ProctoringConfigOut(BaseModel):
    """GET /proctoring/config."""

    config: dict[str, Any]
    max_warnings: int
    audio_analysis_available: bool
    #: The monitoring rules in plain language, composed by the server from
    #: the same numbers it enforces (`phrasing.candidate_rules`). The consent
    #: screen renders these; it never words a rule itself.
    candidate_rules: list[str]


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: str
    occurred_at: datetime
    duration_ms: int | None = Field(default=None, ge=0, le=24 * 3600 * 1000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    question_id: uuid.UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def _client_emittable(cls, value: str) -> str:
        if value not in catalog.CLIENT_EMITTABLE:
            raise ValueError(f"{value!r} is not an event a client may emit")
        return value

    @field_validator("metadata")
    @classmethod
    def _small_and_flat(cls, value: dict[str, Any]) -> dict[str, Any]:
        # Labels and aggregates only. A frame is megabytes; a label is bytes.
        import json

        if len(json.dumps(value, separators=(",", ":"))) > MAX_METADATA_BYTES:
            raise ValueError("event metadata is too large to be a label")
        for item in value.values():
            if isinstance(item, (dict, list)) and len(json.dumps(item)) > MAX_METADATA_BYTES // 4:
                raise ValueError("event metadata is too large to be a label")
        return value


class EventBatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[EventIn] = Field(min_length=1)


class WarningOut(BaseModel):
    """A warning the SERVER issued on this batch (section 8.3)."""

    number: int
    max_warnings: int
    event_type: str
    #: The whole message, specific and actionable, composed server-side.
    message: str
    #: True on the third warning: the session either ended or was noted.
    final: bool


class TerminationOut(BaseModel):
    reason_code: str
    #: Plain language, for the candidate's screen.
    message: str


class IngestOut(BaseModel):
    accepted: int
    warnings_used: int
    max_warnings: int
    status: str
    warning: WarningOut | None = None
    termination: TerminationOut | None = None
    #: None only when the session has ended.
    pause: PauseOut | None = None


class MonitoringStateIn(BaseModel):
    """The browser's own self-check (section 9): is each monitoring component
    still live. A False is recorded once per session as a Path C note; the
    browser's own integrity module escalates on its own clock."""

    model_config = ConfigDict(extra="forbid")

    camera: bool = True
    microphone: bool = True
    models: bool = True
    handlers: bool = True


class DetectionStatsIn(BaseModel):
    """Camera-analysis telemetry for one heartbeat interval: counts and two
    summary figures, never a frame, a label list or a candidate's image.
    `phone_best_score` is the highest phone confidence seen even when it stayed
    under the event threshold, so a phone the model half-saw can be measured
    in a staged session instead of guessed at."""

    model_config = ConfigDict(extra="forbid")

    frames_processed: int = Field(ge=0, le=100_000)
    frames_failed: int = Field(ge=0, le=100_000)
    measured_fps: float | None = Field(default=None, ge=0.0, le=1000.0)
    inference_ms_mean: float | None = Field(default=None, ge=0.0, le=600_000.0)
    phone_best_score: float | None = Field(default=None, ge=0.0, le=1.0)


class HeartbeatIn(BaseModel):
    """POST /proctoring/sessions/{id}/heartbeat.

    `identity_matched` true resets the server's consecutive-mismatch run: a
    mismatch is only confirmed by two IN A ROW, and the browser reports its
    matches here rather than as events, because a match is not an event.
    """

    model_config = ConfigDict(extra="forbid")

    identity_matched: bool | None = None
    monitoring: MonitoringStateIn = Field(default_factory=MonitoringStateIn)
    detection: DetectionStatsIn | None = None


class HeartbeatOut(BaseModel):
    status: str
    warnings_used: int
    server_time: datetime
    #: Seconds the client should wait before the next heartbeat.
    interval_seconds: int
    termination: TerminationOut | None = None
    #: None only when the session has ended.
    pause: PauseOut | None = None


class AudioChunkOut(BaseModel):
    """What the audio route answers. The chunk itself is gone by the time this
    is built: analysed in memory and destroyed."""

    analysed: bool
    #: `unavailable` when no analysis service is configured.
    status: str
    warnings_used: int
    warning: WarningOut | None = None
    termination: TerminationOut | None = None


# ── The report (section 7) ───────────────────────────────────────────────────


class ProctoringFindingsOut(BaseModel):
    screen_browser: list[str]
    camera: list[str]
    audio: list[str]
    answer_patterns: list[str]


class ActivityLogRowOut(BaseModel):
    time: str
    what_happened: str
    how_long: str
    what_the_system_did: str


class ProctoringReportOut(BaseModel):
    """Words only. No field here is numeric, by construction: this model is
    embedded in `FunctionalReportOut`, which refuses a number at any path."""

    model_config = ConfigDict(from_attributes=True)

    candidate: str
    assessment: str
    date_line: str
    outcome: str
    summary: str
    findings: ProctoringFindingsOut
    activity_log: list[ActivityLogRowOut]
    closing: str
    #: True when a monitoring gap, a degraded device or an unavailable analysis
    #: service means the report describes less than the whole session.
    monitoring_was_incomplete: bool
    generated_at: datetime


class ProctoringStateOut(BaseModel):
    """Where one application's Proctoring Report stands, in words. `state` is
    one of `services/proctoring/report.STATE_*`; `message` is the sentence to
    show when the state is anything but a finished, complete report."""

    model_config = ConfigDict(from_attributes=True)

    state: str
    message: str | None = None
