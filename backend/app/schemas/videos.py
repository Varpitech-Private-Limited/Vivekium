"""Response schemas for the client-portal video access routes (2026-09-05
Client Candidate Dashboard, Executive Profile & Proctored Video Specification,
sections 13, 15-18).

TWO THINGS THESE SCHEMAS MUST NEVER CARRY (spec section 13): the S3 bucket
name and the raw object keys. Neither type has a field for either, and
`tests/test_video_access.py` sweeps the field sets to keep it that way. The
presigned URL a delivery response carries is the ONE sanctioned way an object
location leaves the backend: time-limited, single-purpose and useless once
expired.

No candidate score, grade or rank appears here. `duration_seconds` and
`compressed_size_bytes` are operational media facts, not assessment numbers.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class SessionMediaStartOut(BaseModel):
    """The recording opened for a proctored session.

    Carries every number the browser needs BEFORE it opens a MediaRecorder,
    because a client that guessed would discover a limit only when a part was
    refused mid-session: the part ceiling and floor, the recording's total
    ceiling, the longest session, and the recorder's own caps. The server
    refuses anything outside them regardless, so these are served for the
    client's benefit and are not a boundary. No bucket name and no object key,
    like every other schema in this file.
    """

    conversation_id: uuid.UUID
    recording_id: uuid.UUID
    #: A `services/video/lifecycle` status; `recording` on a fresh open.
    status: str
    #: Total bytes across every segment of this recording.
    max_upload_bytes: int
    max_duration_seconds: int
    #: One part's ceiling, and S3's floor for every part but a segment's last.
    part_max_bytes: int
    part_min_bytes: int
    #: The MediaRecorder caps (`videoBitsPerSecond`, `audioBitsPerSecond`,
    #: and the capture resolution).
    video_bits_per_second: int
    audio_bits_per_second: int
    max_width: int
    max_height: int


class SegmentOpenIn(BaseModel):
    """The MediaRecorder's MIME type for the segment about to start. Only its
    subtype is used, through the fixed extension table in `video/keys.py`."""

    source_format: str | None = None


class SegmentOut(BaseModel):
    """One segment as the server holds it. The candidate's browser uses the
    id to address parts; it never learns where the bytes land."""

    segment_id: uuid.UUID
    recording_id: uuid.UUID
    ordinal: int
    #: `open`, `completed` or `aborted`.
    status: str
    #: Part numbers received so far, so a reloaded page can resume without
    #: re-sending a part the store already holds.
    parts_received: list[int]
    #: Operational byte count of what arrived, never an assessment figure.
    bytes_received: int
    final_part_number: int | None = None


class RecordingStatusOut(BaseModel):
    """The candidate's honest view of their recording.

    `status` is the lifecycle state and `message` the plain-language account
    of it. The message never pretends a failed step ran, and no internal
    identifier crosses beyond the recording's own id.
    """

    recording_id: uuid.UUID
    status: str
    message: str


class VideoAccessOut(BaseModel):
    """The Executive Profile / dashboard view of one application's video."""

    job_candidate_link_id: uuid.UUID
    #: Null when no recording exists (every conversational session today).
    recording_id: uuid.UUID | None = None

    #: 'conversational', or null before any session opens. A row written by
    #: the deleted video-interview mode still reads 'video_interview'.
    assessment_mode: str | None = None
    #: "Conversational" / "Not started", or "Video interview" for such a row.
    assessment_mode_label: str

    #: "Ready" / "Processing" / "Failed" / "No recording" -- the four dashboard
    #: words, never a lifecycle-machine identifier.
    video_status: str
    #: One client-facing sentence explaining the word above.
    video_status_detail: str

    #: The finer stage behind the word: recording, uploaded, compressing,
    #: storing, ready, missing, failed or stalled, with a sentence saying what
    #: it means and what the reviewer can do, and when the pipeline last said so.
    video_stage: str = "missing"
    video_stage_detail: str = ""
    last_reported_at: datetime | None = None

    #: Set only once the recording is ready; null is "not known yet", never 0.
    duration_seconds: float | None = None
    compressed_size_bytes: int | None = None

    #: Whether POST .../preview will mint a URL right now.
    preview_available: bool = False
    #: Preview availability AND the candidate's download consent
    #: (`retain_video_consent`). False keeps the record view-only.
    download_available: bool = False
    #: Why download is withheld while preview works, so the UI explains
    #: instead of showing a dead button. Null when download is available or
    #: nothing is previewable anyway.
    download_blocked_reason: str | None = None

    #: Whether the staff retry endpoint would accept this recording, so the
    #: Failed state can offer "Retry processing" without ever offering a 409.
    can_retry: bool = False


class VideoDeliveryOut(BaseModel):
    """One minted delivery URL (preview or download).

    The URL is a bearer token: it is returned once, expires on the server's
    clock, and is never stored. `expires_in_seconds` is served so the player
    can refresh before a long viewing session outlives its URL.
    """

    url: str
    expires_in_seconds: int
    #: "inline" for the player, "attachment" for a browser download.
    disposition: str
    #: The attachment filename (ids only, no candidate PII); null for preview.
    filename: str | None = None
