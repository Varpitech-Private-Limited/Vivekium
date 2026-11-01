"""Client-side video access + candidate dashboard metadata layer.

Provenance: the 2026-09-05 Client Candidate Dashboard, Executive Profile &
Proctored Video Specification, sections 3-5 (dashboard columns), 15-19
(preview, secure access, download, audit) and 30-36 (integration).

WHAT THIS MODULE IS AND IS NOT
------------------------------
It is the DELIVERY and PRESENTATION layer over the recording pipeline that
`services/video/` owns: the honest status words the client dashboard renders,
the availability decisions (preview, download) and the short-lived presigned
URLs the routes hand a browser. It runs nothing: no transcode, no S3 read on
the dashboard path, no media byte ever passes through here. The stored
compressed object is H.264/AAC mp4, directly playable in a browser, so the
"prepare playable representation" step of spec section 11 is a presigned GET
(S3 serves range requests natively) and deliberately not a transcode.

It is NOT a scorer and imports none. Video access moves no grade, no weight
and no ranking; everything here is metadata words plus a bearer URL.

NAMING NOTE: this module is deliberately NOT named `video_access` --
`tests/test_dual_mode_assessment.py` allowlists importers of the
`app.services.video` package by name PREFIX, and a sibling module whose dotted
path started with `app.services.video` would flag every one of its own
importers. This module IS on that allowlist (it reads the lifecycle constants
and the per-link accessor); its importers are not, which is the point: the
dashboard reads words from here without touching the pipeline.

NEVER EXPOSED (spec section 13): the bucket name and the raw object keys.
`tests/test_video_access.py` sweeps the response schemas for both.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.dual_mode import (
    MODE_CONVERSATIONAL,
    MODE_VIDEO_INTERVIEW,
    VideoRecording,
)
from app.services import object_storage
from app.services.video import lifecycle
from app.services.video import recordings as video_recordings

# ── Audit actions (spec section 19) ──────────────────────────────────────────
# Written through `services/audit.audit` inside the request transaction, so an
# access grant and its audit row are atomic. Metadata carries ids only: user,
# tenant, candidate, application, conversation, recording. Never the video
# contents, never a transcript excerpt.
VIDEO_PREVIEW_REQUESTED = "video_preview_requested"
VIDEO_DOWNLOAD_REQUESTED = "video_download_requested"

# ── Assessment mode words (spec section 4.1) ─────────────────────────────────
#: No conversation row means no assessment has begun in either mode. Saying
#: "Conversational" for it would claim a session that never existed.
MODE_NOT_STARTED_LABEL = "Not started"

MODE_LABELS: dict[str, str] = {
    MODE_CONVERSATIONAL: "Conversational",
    MODE_VIDEO_INTERVIEW: "Video interview",
}


def mode_label(mode: str | None) -> str:
    """Display word for an assessment session's mode. None (no session yet)
    reads as Not started rather than defaulting to either real mode."""
    if mode is None:
        return MODE_NOT_STARTED_LABEL
    return MODE_LABELS.get(mode, MODE_LABELS[MODE_CONVERSATIONAL])


# ── Report availability words (spec sections 4.2 / 4.3) ──────────────────────
REPORT_AVAILABLE = "Available"
REPORT_PROCESSING = "Processing"
REPORT_NOT_AVAILABLE = "Not available"


def prism_status_word(*, has_report: bool, conversation_status: str | None) -> str:
    """PRISM Report availability, derived from the same presence the candidate
    table already reads (`has_report` is `report_id IS NOT NULL`).

    Processing means the assessment finished and synthesis has not delivered
    yet; anything earlier is honestly Not available -- an unfinished
    assessment is not a report in flight.
    """
    if has_report:
        return REPORT_AVAILABLE
    if conversation_status == "completed":
        return REPORT_PROCESSING
    return REPORT_NOT_AVAILABLE


def proctoring_status_word(
    *, has_proctoring_report: bool, has_proctoring_session: bool
) -> str:
    """Proctoring Report availability. Reuses the existing report presence
    (spec 4.3: do not recreate the proctoring intelligence); a session with no
    generated report yet is Processing, no session is Not available."""
    if has_proctoring_report:
        return REPORT_AVAILABLE
    if has_proctoring_session:
        return REPORT_PROCESSING
    return REPORT_NOT_AVAILABLE


# ── Video status words (spec sections 4.4 / 14) ──────────────────────────────
VIDEO_READY = "Ready"
VIDEO_PROCESSING = "Processing"
VIDEO_FAILED = "Failed"
VIDEO_NONE = "No recording"

#: What each word means, in one client-facing sentence. Served with the word so
#: the dashboard never invents copy.
#:
#: SUPERSEDED 2026-09-22: this comment used to read "a conversational session
#: with no recording is a normal state (plan resolution 5: proctoring stores no
#: media, so conversational assessments have no video today)". The owner
#: reversed that, so a proctored conversational session HAS a stored recording
#: and reaches this layer through the same rows. `VIDEO_NONE` survives and is
#: still honest for the cases that really have no media: a session that never
#: started, one recorded before the ruling, and one whose media has since been
#: deleted by erasure, job closure or the retention sweep.
VIDEO_STATUS_DETAIL: dict[str, str] = {
    VIDEO_READY: "The assessment video is ready to preview.",
    VIDEO_PROCESSING: "The recording is still being processed. Check back shortly.",
    VIDEO_FAILED: (
        "Processing this recording did not complete. It can be retried "
        "without involving the candidate."
    ),
    VIDEO_NONE: "No video was recorded for this assessment.",
}

#: Shown when the recording exists and its media has been deleted. Distinct
#: from "no video was recorded", because those are different facts and the
#: hiring team asking the question is asking which one it is.
VIDEO_DELETED_DETAIL = (
    "The recording for this assessment has been deleted under the platform's "
    "retention and deletion policy."
)


def video_status_word(status: str | None, *, media_deleted: bool = False) -> str:
    """Collapse the lifecycle machine into the four dashboard words.

    None (no recording row) is No recording; `ready` is Ready; any failure
    state is Failed; everything in between -- including a recording still in
    the candidate's browser -- is Processing, because from the client's chair
    that is what it is.

    DELETED MEDIA IS CHECKED FIRST and reads as No recording, whatever the
    lifecycle status says. The status is a record of what the PIPELINE did and
    it stays true; the question this word answers is "can I watch it", and
    once the objects are gone the honest answer to that is no. Every caller
    passes the flag from the row it already read, so a list surface and the
    access route cannot disagree about whether a video exists.
    """
    if media_deleted:
        return VIDEO_NONE
    if status is None:
        return VIDEO_NONE
    if status == lifecycle.READY:
        return VIDEO_READY
    if status in lifecycle.FAILURE_STATUSES:
        return VIDEO_FAILED
    return VIDEO_PROCESSING


def is_servable(recording: VideoRecording | None) -> bool:
    """Whether a preview URL may be minted: the pipeline finished, the
    long-term object exists, it is the directly playable mp4, and the media
    has not since been DELETED.

    The deletion check is the half added with the retention lifecycle
    (2026-09-22). Without it a recording whose objects were erased would still
    read as Ready and mint a presigned URL onto a key that is gone, which the
    browser would render as a broken player rather than as the honest sentence
    the dashboard has for it.
    """
    return (
        recording is not None
        and recording.status == lifecycle.READY
        and bool(recording.s3_compressed_key)
        and recording.stored_format == "mp4"
        and recording.media_deleted_at is None
    )


def media_deleted(recording: VideoRecording | None) -> bool:
    """Whether this recording's stored media has been verifiably deleted."""
    return recording is not None and recording.media_deleted_at is not None


def can_retry(recording: VideoRecording | None) -> bool:
    """Whether the staff retry endpoint would accept this recording. Mirrors
    `lifecycle.RETRYABLE_FAILURES` so the UI never offers a button that 409s."""
    return recording is not None and recording.status in lifecycle.RETRYABLE_FAILURES


async def latest_recording(
    session: AsyncSession, link_id: uuid.UUID
) -> VideoRecording | None:
    """The most recent recording on an application, via the pipeline's own
    accessor so there is one definition of "latest"."""
    return await video_recordings.recording_for_link(session, link_id)


async def latest_conversation_facts(
    session: AsyncSession, link_id: uuid.UUID
) -> tuple[str | None, str | None]:
    """(mode, status) of the newest assessment session on this application, or
    (None, None) when no session exists."""
    from app.models.assessment import AssessmentConversation

    row = (
        await session.execute(
            select(AssessmentConversation.mode, AssessmentConversation.status)
            .where(AssessmentConversation.job_candidate_link_id == link_id)
            .order_by(AssessmentConversation.created_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None, None
    return row[0], row[1]


# ── Who may reach a recording (Vivekium release, 2026-09-24) ─────────────────


async def require_hiring_team(session: AsyncSession, user: Any, job_id: uuid.UUID) -> None:
    """A recording is viewable only by the job's HIRING TEAM.

    `view_review_screen` is SCOPED for the Recruiter, the Hiring Manager and
    the Interview Manager (RBAC 24), and `require_capability` alone answers
    only "may this role do this at all", so before this every Recruiter in a
    tenant could mint a presigned URL for the recording of any job in it. The
    answer here is `rbac.authorize` over the job's resource facts: tenant
    (404 across a boundary), the 24 ceiling, the grant, and the per-job
    assignment for the scoped roles (403 inside the tenant). The Client Super
    Admin and the HR Manager reach every job in their own tenant, as they do
    for the report and the transcript.

    The ONE definition, called by every recruiter-facing recording route
    (`api/videos.py`) and by the staff retry (`api/assessment_recording.py`).
    """
    from app.services import capabilities as caps  # noqa: PLC0415
    from app.services import rbac  # noqa: PLC0415

    resource = await rbac.load_job_resource(session, job_id)
    if resource is None:
        from fastapi import HTTPException  # noqa: PLC0415

        raise HTTPException(status_code=404, detail="Not found")
    principal = rbac.Principal(
        user_id=user.user_id, tenant_id=user.tenant_id, role=user.role
    )
    decision = await rbac.authorize(
        session, principal, caps.VIEW_REVIEW_SCREEN, resource
    )
    rbac.raise_for(decision, caps.VIEW_REVIEW_SCREEN)


# ── Delivery (spec sections 15, 16, 18) ──────────────────────────────────────


def preview_url(recording: VideoRecording) -> tuple[str, int]:
    """A short-lived streaming URL for the browser's own video player.

    Inline, no disposition: the player streams it. The TTL is a setting and
    covers a full watch-through of the longest permitted interview, because a
    seek after expiry is a range request S3 will refuse. Returns
    (url, ttl_seconds).
    """
    ttl = get_settings().video_preview_url_ttl_seconds
    return (
        object_storage.presigned_get_url(recording.s3_compressed_key, ttl_seconds=ttl),
        ttl,
    )


def download_url(recording: VideoRecording) -> tuple[str, int, str]:
    """A short-lived attachment URL. The filename is IDS ONLY, matching the
    object-key rule (spec section 9: no candidate PII in names). Returns
    (url, ttl_seconds, filename). The consent gate is the CALLER's job --
    this function only builds delivery for an already-authorized request."""
    ttl = get_settings().video_download_url_ttl_seconds
    filename = f"assessment-video-{recording.id}.mp4"
    return (
        object_storage.presigned_get_url(
            recording.s3_compressed_key, ttl_seconds=ttl, filename=filename
        ),
        ttl,
        filename,
    )


def audit_metadata(
    recording: VideoRecording, link_id: uuid.UUID
) -> dict[str, Any]:
    """The id-only metadata block every video audit row carries (spec 19)."""
    return {
        "job_candidate_link_id": str(link_id),
        "candidate_id": str(recording.candidate_id),
        "conversation_id": str(recording.conversation_id),
        "recording_id": str(recording.id),
    }


# ── The stage a reviewer reads (G, 2026-11) ───────────────────────────────────
# The four dashboard words hide where a recording is stuck. The stage is the
# finer, still-honest answer: what the pipeline is doing now, how long ago it
# last said so, and what the reviewer can do. It reads rows only (no S3 call).

STAGE_UPLOADED = "uploaded"
STAGE_COMPRESSING = "compressing"
STAGE_STORING = "storing"
STAGE_READY = "ready"
STAGE_MISSING = "missing"
STAGE_FAILED = "failed"
STAGE_STALLED = "stalled"
STAGE_RECORDING = "recording"

STAGE_DETAIL: dict[str, str] = {
    STAGE_RECORDING: (
        "The candidate's browser is still sending the recording, or the session "
        "has not been finalized. Check back after the assessment is submitted."
    ),
    STAGE_UPLOADED: (
        "The recording has arrived and is queued for processing. Nothing needs "
        "doing; it normally starts within minutes."
    ),
    STAGE_COMPRESSING: "The recording is being converted to a playable video.",
    STAGE_STORING: "The playable video is being saved and checked.",
    STAGE_READY: "The assessment video is ready to preview.",
    STAGE_FAILED: (
        "Processing this recording did not complete. Use Retry processing to "
        "run it again; the candidate need not do anything. If it fails again, "
        "contact support with the application reference."
    ),
    STAGE_STALLED: (
        "Processing has not reported progress for longer than it should. An "
        "automatic recovery pass marks it so it can be retried; check back "
        "within the hour. The candidate need not do anything."
    ),
}


def video_stage(
    recording: VideoRecording | None, *, now: datetime | None = None
) -> tuple[str, datetime | None]:
    """(stage, last_reported_at) for one recording.

    `stalled` is a recording that is `uploaded` past `video_orphan_grace_minutes`
    or `compressing`/`storing` past the pipeline's own stall bound
    (`assessment_media_retention.processing_stall_after`): the same thresholds
    the repair sweep uses, so the screen and the sweep cannot disagree.
    """
    from app.services import assessment_media_retention as retention  # noqa: PLC0415

    if recording is None or media_deleted(recording):
        return STAGE_MISSING, None
    moment = now or datetime.now(timezone.utc)
    status = recording.status
    ended = recording.ended_at or recording.created_at
    if status == lifecycle.READY:
        return STAGE_READY, recording.processed_at or recording.stored_at
    if status in lifecycle.FAILURE_STATUSES:
        return STAGE_FAILED, None
    if status == lifecycle.UPLOADED:
        grace = timedelta(minutes=get_settings().video_orphan_grace_minutes)
        return (STAGE_STALLED if ended < moment - grace else STAGE_UPLOADED), ended
    if status in (lifecycle.COMPRESSING, lifecycle.STORING):
        started = recording.processing_started_at or ended
        stalled = started < moment - retention.processing_stall_after()
        return (
            STAGE_STALLED if stalled else status
        ), started
    return STAGE_RECORDING, recording.created_at
