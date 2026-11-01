"""The session recording tasks: processing, repair, and the retention purge.

Carved out of `workers/tasks.py` on 2026-09-24 (PLAN-p3 WP0) and rebuilt in
the same release (PLAN-p3 WP5). `workers/tasks.py` imports this module so
importing the registry still registers every task.

    pickready.process_assessment_video        Route.ECS, one recording
    pickready.reconcile_assessment_recordings Route.LAMBDA, hourly repair
    pickready.purge_assessment_media          Route.LAMBDA, hourly retention

The processing task keeps its name for queued-message compatibility although
the video-interview half it used to carry is deleted.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from app.workers.registry import Route, task
from app.workers.runtime import (
    worker_session as _worker_session,
    _run,
)

logger = logging.getLogger(__name__)


@task(
    name="pickready.purge_assessment_media",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason="a sweep: one run reads every tenant's rows",
)
def purge_assessment_media():
    """Hourly retention sweep: owner decision D4.

    A recording is purged at whichever comes first of its stored
    `media_purge_due_at` (the session end plus the retention setting) and its
    job's stored closure purge date. It asks the TABLE for both
    (`media_retention.due_recordings`), never a constant, and every deletion
    is HEAD-confirmed. The ROW survives with `media_deleted_at` stamped, so
    the record still says a recording existed and that its media is gone.

    One recording at a time, committing as it goes: a single transaction over
    the whole backlog would roll back deletions the object store has already
    performed and leave the counters disagreeing with the store. A deletion
    that does not confirm is counted and comes back next hour; there is no
    terminal failure state.
    """
    from app.services import assessment_media_retention as media_retention
    from app.services import deletion_requests

    async def _task():
        async with _worker_session() as session:
            due = await media_retention.due_recordings(session)
            purged = 0
            incomplete = 0
            for recording in due:
                outcome = await media_retention.delete_media_for_recording(
                    session, recording
                )
                await session.commit()
                if outcome.finished and outcome.failure is None:
                    purged += 1
                    continue
                incomplete += 1
                if recording.media_delete_failures >= deletion_requests.ATTEMPTS_BEFORE_ALARM:
                    logger.error(
                        "assessment_media.purge_stuck recording_id=%s failures=%d "
                        "failure=%s",
                        recording.id,
                        recording.media_delete_failures,
                        outcome.failure or "ObjectStillPresent",
                    )
            logger.info(
                "assessment_media.purged recordings=%d incomplete=%d",
                purged, incomplete,
            )
    _run(_task())


@task(
    name="pickready.reconcile_assessment_recordings",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason="a sweep: one run reads every tenant's rows",
)
def reconcile_assessment_recordings():
    """Hourly repair of the recording pipeline. Five passes, each asking the
    TABLE rather than trusting that an earlier step happened:

    1. RAW DELETIONS THAT DID NOT CONFIRM. A stored (`ready`) recording whose
       raw segment objects were not all HEAD-confirmed gone. Before this
       sweep a failed deletion incremented `raw_delete_failures` and nothing
       ever looked at the row again, so the raw bytes of a candidate's session
       sat in the bucket until the lifecycle rule found them. Past
       `ATTEMPTS_BEFORE_ALARM` the log line becomes an error.
    2. ORPHANED RECORDINGS. A tab closed mid-session never calls finalize.
       Once the session is over and nothing has arrived for the grace, the
       recording is finalized from the STORE's own list of parts and handed
       to processing, so a session is stored even when its browser vanished.
    3. FINALIZED RECORDINGS NO RUN PICKED UP. A finalize dispatches after its
       commit and a lost invoke is logged rather than raised; this re-hands
       a recording still `uploaded` after the grace.
    4. RUNS THAT DIED WITHOUT SAYING SO. A processing task killed from
       outside leaves its row in `compressing` or `storing` for ever. Past
       every bound the pipeline runs under (`processing_stall_after`), the
       row is given the failure state of its step, logged at ERROR, and
       becomes retryable by the hiring team, instead of waiting silently
       while the bucket's raw rule deletes the only copy.
    5. SPOKEN-ANSWER AUDIO. A voice answer whose first deletion could not be
       confirmed, or whose transcription never reported back (failed first
       through the same stale rule the routes apply), is deleted and
       HEAD-confirmed by `voice_audio.repair_pending_audio`; an unconfirmed
       deletion is counted on the row and tried again next hour (p3-w5
       hunk 2, applied at the stage 2 integration).

    Dispatches happen AFTER the commit that makes the rows they read durable.

    ONE RECORDING AT A TIME, AND ONE FAILURE STAYS ONE FAILURE. Completing an
    orphan calls the object store, and a store error for one recording rolls
    back that recording's work, is logged at ERROR with its class, and the
    pass moves on: the row is still `recording`, so the next hour tries it
    again. Rows are re-read by id after every commit or rollback, because a
    rollback expires every loaded row and an expired row read in an async
    session is an implicit query the driver refuses.
    """
    from sqlalchemy import select

    from app.models.dual_mode import VideoRecording
    from app.services import assessment_media_retention as media_retention
    from app.services import deletion_requests, object_storage
    from app.services.assessment_conversation import voice_audio
    from app.services.video import processing
    from app.services.video import recordings as video_recordings
    from app.workers import jobs as job_ledger
    from app.workers.dispatch import dispatch_once_after_commit

    async def _row(session, recording_id):
        return (
            await session.execute(
                select(VideoRecording)
                .where(VideoRecording.id == recording_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def _task():
        to_process: list[str] = []
        retried = cleared = finalized = failed = stalled = 0
        async with _worker_session() as session:
            raw_ids = [
                row.id for row in await media_retention.raw_retry_recordings(session)
            ]
            for recording_id in raw_ids:
                recording = await _row(session, recording_id)
                if recording is None:
                    continue
                retried += 1
                if await processing.delete_raw_objects(session, recording):
                    cleared += 1
                elif recording.raw_delete_failures >= deletion_requests.ATTEMPTS_BEFORE_ALARM:
                    logger.error(
                        "assessment_media.raw_delete_stuck recording_id=%s failures=%d",
                        recording.id, recording.raw_delete_failures,
                    )
                await session.commit()

            orphan_ids = [
                row.id for row in await video_recordings.orphaned_recordings(session)
            ]
            for recording_id in orphan_ids:
                recording = await _row(session, recording_id)
                if recording is None:
                    continue
                try:
                    outcome = await video_recordings.finalize_recording(
                        session, recording, from_store=True
                    )
                    await session.commit()
                except object_storage.ObjectStorageError as exc:
                    await session.rollback()
                    failed += 1
                    logger.error(
                        "assessment_media.orphan_finalize_failed recording_id=%s "
                        "failure=%s",
                        recording_id, type(exc).__name__,
                    )
                    continue
                finalized += 1
                if outcome.dispatch:
                    to_process.append(str(recording_id))

            for recording in await media_retention.stuck_uploaded_recordings(session):
                to_process.append(str(recording.id))
            await session.commit()

            stalled_ids = [
                row.id for row in await media_retention.stalled_recordings(session)
            ]
            for recording_id in stalled_ids:
                recording = await _row(session, recording_id)
                if recording is None:
                    continue
                was = recording.status
                now_status = processing.mark_stalled(recording)
                await session.commit()
                stalled += 1
                logger.error(
                    "assessment_media.processing_stalled recording_id=%s "
                    "status_was=%s status=%s",
                    recording_id, was, now_status,
                )

            voice = await voice_audio.repair_pending_audio(
                session, now=datetime.now(timezone.utc)
            )
            await session.commit()
            logger.info(
                "assessment_media.voice_audio_repaired examined=%d deleted=%d "
                "unconfirmed=%d failed_stale=%d",
                voice.examined, voice.deleted, voice.unconfirmed, voice.failed_stale,
            )
        # DURABLE, 2026-11-01: each hand-off is a ledger row written in a
        # transaction of its own, so a refused Fargate start (vCPU quota) is
        # recorded as capacity_blocked and re-driven by the job sweep instead of
        # being a lost invoke nobody reads. A recording that already has a live
        # job is left to that job.
        dispatched = 0
        if to_process:
            async with _worker_session() as session:
                for recording_id in dict.fromkeys(to_process):
                    if await job_ledger.has_active(
                        session, "assessment_recording", recording_id
                    ):
                        continue
                    await dispatch_once_after_commit(
                        session,
                        "pickready.process_assessment_video",
                        dedupe_key=f"process_assessment_video:{recording_id}",
                        args=[recording_id],
                        resource_type="assessment_recording",
                        resource_id=recording_id,
                        idempotent=True,
                    )
                    dispatched += 1
                await session.commit()
        logger.info(
            "assessment_media.reconciled raw_retried=%d raw_cleared=%d "
            "orphans_finalized=%d orphans_failed=%d stalled=%d dispatched=%d",
            retried, cleared, finalized, failed, stalled, dispatched,
        )
    _run(_task())


@task(
    name="pickready.process_assessment_video",
    route=Route.ECS,
    rls="bypass",
    rls_reason=(
        "one tenant's work, not yet proven under tenant_worker_session (PLAN-p7 3.3 "
        "converts only with a real-Postgres read-back test)"
    ),
    max_attempts=2,
    backoff_seconds=10.0,
)
def process_assessment_video(recording_id: str):
    """The session recording pipeline: assemble, compress, store, delete raw.

    Route.ECS because this is work measured in minutes: downloading a two-hour
    recording's segments and one ffmpeg transcode legitimately outrun Lambda's
    ceiling.

    The whole body lives in `services/video/processing.process_recording`,
    which owns the recording's state machine: every failure lands the row in
    the failure state naming the step (committed before the re-raise, so a
    retry or the staff retry endpoint knows what to retry), and the row is
    locked while it leaves `uploaded`, so this task redelivered is safe.
    """
    from app.services.video import processing

    async def _task():
        async with _worker_session() as session:
            await processing.process_recording(session, uuid.UUID(str(recording_id)))

    _run(_task())
