"""Client-portal access to recorded assessment videos.

Provenance: the 2026-09-05 Client Candidate Dashboard, Executive Profile &
Proctored Video Specification, sections 15-19 and 30-36. The recording
pipeline (segmented upload, compression, storage) lives behind
`api/assessment_recording.py` and `services/video/`; these routes are the CLIENT side:
what a recruiter's dashboard and the Executive Profile ask about a finished
recording, and the two delivery URLs.

EVERY route here follows the same contract:

  * `require_capability(view_review_screen)` -- the capability that opens this
    candidate's transcript and PRISM Report. Someone who may read the
    assessment evidence may watch it; someone who may not certainly may not.
  * THE JOB'S HIRING TEAM ONLY (2026-09-24): the same capability resolved by
    `rbac.authorize` over the job (`video_access.require_hiring_team`), so
    the per-job assignment scope RBAC 24 gives the Recruiter, the Hiring
    Manager and the Interview Manager binds here too.
  * The RLS-aware tenant session (`get_tenant_db`).
  * The link-in-tenant gate answers 404, never 403: a cross-tenant id must be
    indistinguishable from a nonexistent one (spec section 17).
  * An audit row inside the request transaction for every minted URL
    (spec section 19): ids only, never the video contents.

The metadata route touches NO media and NO S3 -- it reads rows. Only the two
POST routes mint presigned URLs, and neither transcodes anything: the stored
compressed object is directly playable mp4, so delivery is a signed GET
(spec section 11: do not decompress when the stored format already serves).

DOWNLOAD is additionally gated on the candidate's own retention consent
(`retention_consent.video_download_allowed`): an explicit False or a
never-asked NULL keeps the record preview-only in the client portal.

ONE RECORDING KIND IS WRITTEN NOW, the proctored session's, and a row of the
deleted video-interview kind is served by exactly the same rules. A recruiter
reaches both through the same capability, the same scope, the same tenant
gate, the same audit row, the same consent rule and the same presigned URL.

A recording whose media has been DELETED (erasure, job closure, or the
retention sweep) is not servable and says so in its own sentence. It is not a
404: the application exists and the question was reasonable, and "no video was
ever recorded" is a different fact from "the video has been deleted".
"""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_tenant_db, require_capability
from app.models.candidate import Candidate, JobCandidateLink
from app.models.dual_mode import VideoRecording
from app.models.job import Job
from app.schemas.videos import VideoAccessOut, VideoDeliveryOut
from app.services import assessment_video_access as video_access
from app.services import capabilities as caps
from app.services import job_assessment_retention
from app.services import object_storage, retention_consent
from app.services.audit import audit

logger = logging.getLogger(__name__)

router = APIRouter()

#: Shown beside a working Preview button when the candidate withheld download
#: consent. Mirrors the PRISM PDF's view-only copy: say why, never a dead
#: button and never a silent absence.
DOWNLOAD_WITHHELD_REASON = (
    "The candidate chose view-only retention for their video record, so it "
    "can be previewed here but not downloaded."
)


async def _link_or_404(
    session: AsyncSession, user: CurrentUser, link_id: uuid.UUID
) -> JobCandidateLink:
    """The link-in-tenant gate. 404 for absent AND for cross-tenant, so the
    two are indistinguishable from outside (spec section 17).

    IT ALSO CARRIES THE JOB-CLOSURE GATE (change request 22, 2026-09-22), and
    it carries it here because all three routes in this module already come
    through this function. A recording is assessment data exactly as the
    transcript is, so closing the job withholds it on the same instant and the
    thirty day sweep deletes the objects on the same day. Putting the check on
    each route instead would be three copies of one rule, and the copy that
    drifted would be the one minting a presigned URL.
    """
    link = await session.get(JobCandidateLink, link_id)
    if link is None or link.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="Application not found")
    job = await session.get(Job, link.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Application not found")
    # THE HIRING TEAM, NOT THE TENANT (Vivekium release, 2026-09-24). The
    # route dependency answers "may this role review at all"; this answers
    # "on THIS job", which is where a scoped Recruiter, Hiring Manager or
    # Interview Manager stops at the jobs they are assigned to.
    await video_access.require_hiring_team(session, user, job.id)
    await job_assessment_retention.require_readable(session, user, job)
    return link


async def _download_consented(session: AsyncSession, link: JobCandidateLink) -> bool:
    candidate = await session.get(Candidate, link.candidate_id)
    return candidate is not None and retention_consent.video_download_allowed(candidate)


@router.get("/links/{link_id}", response_model=VideoAccessOut)
async def video_metadata(
    link_id: uuid.UUID,
    user: CurrentUser = Depends(require_capability(caps.VIEW_REVIEW_SCREEN)),
    session: AsyncSession = Depends(get_tenant_db),
) -> VideoAccessOut:
    """The video facts for one application: mode, status word, duration,
    availability booleans. Metadata only -- no S3 call, no media work, and a
    conversational session with no recording answers "No recording" honestly
    rather than 404ing a reasonable question about a real application."""
    link = await _link_or_404(session, user, link_id)
    recording = await video_access.latest_recording(session, link.id)
    mode, _conversation_status = await video_access.latest_conversation_facts(
        session, link.id
    )
    deleted = video_access.media_deleted(recording)
    status_word = video_access.video_status_word(
        recording.status if recording is not None else None,
        media_deleted=deleted,
    )
    preview_available = video_access.is_servable(recording)
    stage, reported_at = video_access.video_stage(recording)
    download_consented = (
        await _download_consented(session, link) if preview_available else False
    )
    return VideoAccessOut(
        job_candidate_link_id=link.id,
        recording_id=recording.id if recording is not None else None,
        assessment_mode=mode,
        assessment_mode_label=video_access.mode_label(mode),
        video_status=status_word,
        video_status_detail=(
            video_access.VIDEO_DELETED_DETAIL
            if deleted
            else video_access.VIDEO_STATUS_DETAIL[status_word]
        ),
        video_stage=stage,
        # Missing keeps the status route's own sentence: deleted and never
        # recorded are different facts.
        video_stage_detail=(
            video_access.VIDEO_DELETED_DETAIL
            if deleted
            else video_access.VIDEO_STATUS_DETAIL[status_word]
            if stage == video_access.STAGE_MISSING
            else video_access.STAGE_DETAIL[stage]
        ),
        last_reported_at=reported_at,
        duration_seconds=(
            recording.duration_seconds if preview_available else None
        ),
        compressed_size_bytes=(
            recording.compressed_size_bytes if preview_available else None
        ),
        preview_available=preview_available,
        download_available=preview_available and download_consented,
        download_blocked_reason=(
            DOWNLOAD_WITHHELD_REASON
            if preview_available and not download_consented
            else None
        ),
        can_retry=video_access.can_retry(recording),
    )


async def _servable_recording_or_409(
    session: AsyncSession, user: CurrentUser, link_id: uuid.UUID
) -> tuple[JobCandidateLink, VideoRecording]:
    link = await _link_or_404(session, user, link_id)
    recording = await video_access.latest_recording(session, link.id)
    if not video_access.is_servable(recording):
        if video_access.media_deleted(recording):
            raise HTTPException(
                status_code=409, detail=video_access.VIDEO_DELETED_DETAIL
            )
        status_word = video_access.video_status_word(
            recording.status if recording is not None else None
        )
        raise HTTPException(
            status_code=409,
            detail=video_access.VIDEO_STATUS_DETAIL[status_word],
        )
    return link, recording


@router.post("/links/{link_id}/preview", response_model=VideoDeliveryOut)
async def request_video_preview(
    link_id: uuid.UUID,
    user: CurrentUser = Depends(require_capability(caps.VIEW_REVIEW_SCREEN)),
    session: AsyncSession = Depends(get_tenant_db),
) -> VideoDeliveryOut:
    """Authorize, audit, and mint a short-lived streaming URL for the in-modal
    player. The stored mp4 streams as-is; S3 serves the player's range
    requests natively, so nothing is transcoded on this path."""
    link, recording = await _servable_recording_or_409(session, user, link_id)
    # Sign BEFORE the audit write: if the object store is unconfigured or
    # unreachable, the request fails without recording an access that never
    # happened.
    try:
        url, ttl = await run_in_threadpool(video_access.preview_url, recording)
    except object_storage.ObjectStorageError as exc:
        logger.error(
            "video_preview.storage_unavailable recording_id=%s error=%s",
            recording.id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="The video store is not reachable right now. Please retry.",
        ) from exc
    await audit(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.user_id,
        action=video_access.VIDEO_PREVIEW_REQUESTED,
        target_type="video_recording",
        target_id=recording.id,
        metadata=video_access.audit_metadata(recording, link.id),
    )
    return VideoDeliveryOut(url=url, expires_in_seconds=ttl, disposition="inline")


@router.post("/links/{link_id}/download", response_model=VideoDeliveryOut)
async def request_video_download(
    link_id: uuid.UUID,
    user: CurrentUser = Depends(require_capability(caps.VIEW_REVIEW_SCREEN)),
    session: AsyncSession = Depends(get_tenant_db),
) -> VideoDeliveryOut:
    """Authorize, check the candidate's retention consent, audit, and mint a
    short-lived attachment URL (spec section 18). The consent gate is the
    server's, not the UI's: a hand-crafted request without consent is 403."""
    link, recording = await _servable_recording_or_409(session, user, link_id)
    if not await _download_consented(session, link):
        raise HTTPException(status_code=403, detail=DOWNLOAD_WITHHELD_REASON)
    try:
        url, ttl, filename = await run_in_threadpool(
            video_access.download_url, recording
        )
    except object_storage.ObjectStorageError as exc:
        logger.error(
            "video_download.storage_unavailable recording_id=%s error=%s",
            recording.id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="The video store is not reachable right now. Please retry.",
        ) from exc
    await audit(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.user_id,
        action=video_access.VIDEO_DOWNLOAD_REQUESTED,
        target_type="video_recording",
        target_id=recording.id,
        metadata=video_access.audit_metadata(recording, link.id),
    )
    return VideoDeliveryOut(
        url=url, expires_in_seconds=ttl, disposition="attachment", filename=filename
    )
