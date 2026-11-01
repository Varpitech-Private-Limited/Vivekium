"""Owner decision D4: a session recording is purged at whichever comes first,
90 days after the session or the job-closure purge.

"Both deadlines are stored or derived, never a moving constant", and the
promise is kept by three things this file pins together:

  * the recording's own date, `media_purge_due_at`, STAMPED once when the
    recording is finalized, so a settings change never moves a deadline a
    candidate was already given;
  * the closure date, `jobs.assessment_purge_due_at`, read at SWEEP time, so a
    job closed after the recording was stamped still wins;
  * the hourly sweep that deletes HEAD-confirmed, leaves the row as the record
    that a recording existed, and counts a failure instead of reporting a
    deletion it did not see;

plus the retry pass for raw segments whose deletion did not confirm, and the
S3 half of the same promise in `infra/modules/s3`, which is only the backstop.
"""
from __future__ import annotations

import asyncio
import pathlib
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from tests import fake_s3
from tests.recording_world import (
    World,
    candidate,
    cleanup,
    factory_or_skip,
    resolve_ownership,
    seed,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
S3_MODULE = REPO / "infra" / "modules" / "s3"
ENVIRONMENTS = ("pilot", "staging", "production")
FULL = b"v" * (5 * 1024 * 1024)


# ── The rule, pure ───────────────────────────────────────────────────────────


def test_the_earlier_of_the_two_deadlines_is_the_purge_date() -> None:
    from app.services.video.recordings import purge_due_at

    ended = datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)
    assert purge_due_at(
        session_ended_at=ended, retention_days=90, job_purge_due_at=None
    ) == ended + timedelta(days=90)
    closure = ended + timedelta(days=12)
    assert purge_due_at(
        session_ended_at=ended, retention_days=90, job_purge_due_at=closure
    ) == closure
    later_closure = ended + timedelta(days=200)
    assert purge_due_at(
        session_ended_at=ended, retention_days=90, job_purge_due_at=later_closure
    ) == ended + timedelta(days=90)


def test_the_retention_is_ninety_days_and_can_never_be_zero() -> None:
    """Zero used to mean "no time-based purge"; D4 replaced that policy, and a
    zero now would stamp every recording due the moment it was stored."""
    from pydantic import ValidationError

    from app.core.config import Settings

    assert Settings.model_fields["assessment_media_retention_days"].default == 90
    for bad in (0, -1):
        with pytest.raises(ValidationError, match="ASSESSMENT_MEDIA_RETENTION_DAYS"):
            Settings(assessment_media_retention_days=bad)
    with pytest.raises(ValidationError, match="VIDEO_PART_MAX_BYTES"):
        Settings(video_part_max_bytes=fake_s3.MIN_PART - 1)


# ── Against the real schema ──────────────────────────────────────────────────


async def _stored_recording(factory, world: World, monkeypatch) -> uuid.UUID:
    """A recording with one completed segment, finalized: `uploaded`, with
    its purge date stamped."""
    from app.api import assessment_recording as routes
    from app.core.db import superadmin_scope
    from app.models.dual_mode import VideoRecordingSegment
    from app.schemas.videos import SegmentOpenIn
    from app.services.video import recordings

    resolve_ownership(monkeypatch, world)
    async with factory() as s:
        async with s.begin():
            async with superadmin_scope(s):
                started = await routes.start_session_media(
                    world.link_id, user=candidate(world), session=s
                )
                opened = await routes.open_recording_segment(
                    world.conv_id, SegmentOpenIn(source_format="video/webm"),
                    user=candidate(world), session=s,
                )
                recording = await recordings.active_recording(s, world.conv_id)
                segment = await s.get(VideoRecordingSegment, opened.segment_id)
                await recordings.record_part(
                    s, recording, segment, part_number=1, data=FULL, final=True
                )
                await routes.finalize_recording(
                    world.conv_id, user=candidate(world), session=s
                )
    return started.recording_id


async def _mark_ready(factory, recording_id, *, due_at, compressed_key) -> None:
    from app.core.db import superadmin_scope

    async with factory() as s:
        async with s.begin():
            async with superadmin_scope(s):
                await s.execute(
                    text("UPDATE video_recordings SET status = 'ready', "
                         "stored_at = now(), s3_compressed_key = :k, "
                         "media_purge_due_at = :d WHERE id = :r"),
                    {"k": compressed_key, "d": due_at, "r": str(recording_id)},
                )


async def _read(factory, sql: str, **params):
    from app.core.db import superadmin_scope

    async with factory() as s:
        async with superadmin_scope(s):
            return (await s.execute(text(sql), params)).mappings().one()


@pytest.mark.asyncio
async def test_a_settings_change_never_moves_a_stamped_date(monkeypatch) -> None:
    """Mutation-checked: letting `stamp_purge_due` overwrite a stamped date
    moves this recording's deadline to thirty days."""
    from app.core.config import get_settings
    from app.core.db import superadmin_scope
    from app.models.dual_mode import VideoRecording
    from app.services.video import recordings

    engine, factory = await factory_or_skip()
    world = World()
    fake_s3.install(monkeypatch)
    try:
        await seed(factory, world)
        recording_id = await _stored_recording(factory, world, monkeypatch)
        before = await _read(
            factory,
            "SELECT ended_at, media_purge_due_at FROM video_recordings WHERE id = :r",
            r=str(recording_id),
        )
        assert before["media_purge_due_at"] == before["ended_at"] + timedelta(days=90)

        monkeypatch.setattr(get_settings(), "assessment_media_retention_days", 30)
        async with factory() as s:
            async with s.begin():
                async with superadmin_scope(s):
                    recording = await s.get(VideoRecording, recording_id)
                    await recordings.stamp_purge_due(s, recording)
        after = await _read(
            factory,
            "SELECT media_purge_due_at FROM video_recordings WHERE id = :r",
            r=str(recording_id),
        )
        assert after["media_purge_due_at"] == before["media_purge_due_at"]
    finally:
        await cleanup(factory, world)
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_closed_job_stamps_its_earlier_purge_date(monkeypatch) -> None:
    engine, factory = await factory_or_skip()
    world = World()
    closure = datetime.now(timezone.utc) + timedelta(days=10)
    fake_s3.install(monkeypatch)
    try:
        await seed(factory, world, job_purge_due_at=closure)
        recording_id = await _stored_recording(factory, world, monkeypatch)
        row = await _read(
            factory,
            "SELECT media_purge_due_at FROM video_recordings WHERE id = :r",
            r=str(recording_id),
        )
        assert row["media_purge_due_at"] == closure
    finally:
        await cleanup(factory, world)
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_closure_after_the_stamp_still_wins_at_sweep_time(monkeypatch) -> None:
    """The recording was stamped for ninety days while the job was open; the
    job then closed and its purge date passed. The sweep reads the LEAST of
    the two, so the recording is due now rather than in ninety days."""
    from app.core.db import superadmin_scope
    from app.services import assessment_media_retention as media_retention

    engine, factory = await factory_or_skip()
    world = World()
    fake_s3.install(monkeypatch)
    try:
        await seed(factory, world)
        recording_id = await _stored_recording(factory, world, monkeypatch)
        async with factory() as s:
            async with superadmin_scope(s):
                due = await media_retention.due_recordings(s)
        assert recording_id not in {row.id for row in due}
        async with factory() as s:
            async with s.begin():
                async with superadmin_scope(s):
                    await s.execute(
                        text("UPDATE jobs SET closed_at = now() - interval '31 days', "
                             "assessment_purge_due_at = now() - interval '1 minute' "
                             "WHERE id = :j"),
                        {"j": str(world.job_id)},
                    )
        async with factory() as s:
            async with superadmin_scope(s):
                due = await media_retention.due_recordings(s)
        assert recording_id in {row.id for row in due}
    finally:
        await cleanup(factory, world)
        await engine.dispose()


def test_the_sweep_purges_what_is_due_headconfirmed_and_keeps_the_row(
    monkeypatch,
) -> None:
    """Two stored recordings: one past its date, one not. The due one's
    objects are gone from the store, its row says so, and its segments are
    stamped; the other is untouched. Mutation-checked: dropping the
    `effective_due <= moment` condition purges the recording that is not due.
    """
    from app.workers import tasks_media

    store = fake_s3.install(monkeypatch)
    due_world, kept_world = World(), World()
    now = datetime.now(timezone.utc)

    async def _prepare():
        engine, factory = await factory_or_skip()
        ids = []
        for world, due_at in ((due_world, now - timedelta(minutes=1)),
                              (kept_world, now + timedelta(days=30))):
            await seed(factory, world)
            recording_id = await _stored_recording(factory, world, monkeypatch)
            key = f"assessment-compressed/{world.conv_id}/{recording_id}/assessment.mp4"
            store.objects[key] = b"mp4"
            await _mark_ready(factory, recording_id, due_at=due_at, compressed_key=key)
            ids.append((recording_id, key))
        await engine.dispose()
        return ids

    async def _after(ids):
        engine, factory = await factory_or_skip()
        rows = []
        for recording_id, _key in ids:
            rows.append(await _read(
                factory,
                "SELECT r.media_deleted_at, r.raw_deleted, r.media_delete_failures, "
                "bool_and(s.raw_deleted_at IS NOT NULL) AS segments_stamped "
                "FROM video_recordings r JOIN video_recording_segments s "
                "ON s.recording_id = r.id WHERE r.id = :r "
                "GROUP BY r.media_deleted_at, r.raw_deleted, r.media_delete_failures",
                r=str(recording_id),
            ))
        for world in (due_world, kept_world):
            await cleanup(factory, world)
        await engine.dispose()
        return rows

    ids = asyncio.run(_prepare())
    try:
        tasks_media.purge_assessment_media()
    finally:
        due_row, kept_row = asyncio.run(_after(ids))
    (_due_id, due_key), (_kept_id, kept_key) = ids
    assert due_key not in store.objects
    assert due_row["media_deleted_at"] is not None
    assert due_row["raw_deleted"] is True and due_row["segments_stamped"] is True
    assert kept_key in store.objects
    assert kept_row["media_deleted_at"] is None


def test_a_deletion_that_does_not_confirm_is_counted_not_reported(monkeypatch) -> None:
    from app.workers import tasks_media

    store = fake_s3.install(monkeypatch)
    world = World()

    async def _prepare():
        engine, factory = await factory_or_skip()
        await seed(factory, world)
        recording_id = await _stored_recording(factory, world, monkeypatch)
        key = f"assessment-compressed/{world.conv_id}/{recording_id}/assessment.mp4"
        store.objects[key] = b"mp4"
        store.stubborn.add(key)
        await _mark_ready(factory, recording_id,
                          due_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                          compressed_key=key)
        await engine.dispose()
        return recording_id

    async def _after(recording_id):
        engine, factory = await factory_or_skip()
        row = await _read(
            factory,
            "SELECT media_deleted_at, media_delete_failures FROM video_recordings "
            "WHERE id = :r",
            r=str(recording_id),
        )
        await cleanup(factory, world)
        await engine.dispose()
        return row

    recording_id = asyncio.run(_prepare())
    try:
        tasks_media.purge_assessment_media()
    finally:
        row = asyncio.run(_after(recording_id))
    assert row["media_deleted_at"] is None
    assert row["media_delete_failures"] == 1


def test_the_retry_pass_finishes_a_raw_deletion_that_did_not_confirm(
    monkeypatch,
) -> None:
    """Before the sweep a failed raw deletion incremented a counter and
    nothing ever looked at the row again. Now the hourly pass retries it,
    per segment, and stops once the store confirms."""
    from app.workers import tasks_media

    store = fake_s3.install(monkeypatch)
    world = World()

    async def _prepare():
        engine, factory = await factory_or_skip()
        await seed(factory, world)
        recording_id = await _stored_recording(factory, world, monkeypatch)
        await _mark_ready(factory, recording_id,
                          due_at=datetime.now(timezone.utc) + timedelta(days=90),
                          compressed_key="assessment-compressed/x/y/assessment.mp4")
        from app.core.db import superadmin_scope

        async with factory() as s:
            async with s.begin():
                async with superadmin_scope(s):
                    await s.execute(
                        text("UPDATE video_recordings SET raw_deleted = false, "
                             "raw_delete_failures = 1 WHERE id = :r"),
                        {"r": str(recording_id)},
                    )
        segment_key = (await _read(
            factory,
            "SELECT s3_key FROM video_recording_segments WHERE recording_id = :r",
            r=str(recording_id),
        ))["s3_key"]
        await engine.dispose()
        return recording_id, segment_key

    async def _after(recording_id, *, clean: bool):
        engine, factory = await factory_or_skip()
        row = await _read(
            factory,
            "SELECT r.raw_deleted, s.raw_deleted_at FROM video_recordings r "
            "JOIN video_recording_segments s ON s.recording_id = r.id "
            "WHERE r.id = :r",
            r=str(recording_id),
        )
        if clean:
            await cleanup(factory, world)
        await engine.dispose()
        return row

    recording_id, segment_key = asyncio.run(_prepare())
    assert segment_key in store.objects
    store.stubborn.add(segment_key)
    try:
        tasks_media.reconcile_assessment_recordings()
        first = asyncio.run(_after(recording_id, clean=False))
        store.stubborn.clear()
        tasks_media.reconcile_assessment_recordings()
    finally:
        second = asyncio.run(_after(recording_id, clean=True))
    # The first pass could not confirm: counted, not reported, retried.
    assert first["raw_deleted"] is False and first["raw_deleted_at"] is None
    # The second pass could: gone from the store, stamped per segment.
    assert segment_key not in store.objects
    assert second["raw_deleted"] is True and second["raw_deleted_at"] is not None


# ── The S3 half: the backstop, the grant, the encryption ─────────────────────


def _module() -> str:
    return (S3_MODULE / "main.tf").read_text(encoding="utf-8")


def _variable_default(name: str) -> str:
    source = (S3_MODULE / "variables.tf").read_text(encoding="utf-8")
    block = source[source.index(f'variable "{name}"'):]
    block = block[: block.index("\n}\n")]
    return re.search(r"\n  default\s*=\s*(.+)", block).group(1).strip()


def _rule(source: str, rule_id: str) -> str:
    start = source.index(f'id     = "{rule_id}"')
    end = source.index("\n  rule {", start) if "\n  rule {" in source[start:] else len(source)
    return source[start:end]


def test_the_lifecycle_backstop_matches_the_database_policy() -> None:
    """The DATABASE purges on time; the lifecycle rule catches a sweep that
    did not run. The compressed object is written after the session ended,
    so a rule of the same length can only fire after the stored date."""
    from app.core.config import Settings

    source = _module()
    assert int(_variable_default("assessment_media_retention_days")) == (
        Settings.model_fields["assessment_media_retention_days"].default
    )
    assert int(_variable_default("assessment_raw_backstop_days")) == 7
    assert int(_variable_default("voice_answer_backstop_days")) == 1
    for rule_id, variable in (
        ("expire-assessment-compressed", "assessment_media_retention_days"),
        ("expire-assessment-raw", "assessment_raw_backstop_days"),
        ("expire-voice-answers", "voice_answer_backstop_days"),
    ):
        rule = _rule(source, rule_id)
        assert f"days = var.{variable}" in rule, rule_id
        # A HEAD-confirmed delete on a versioned bucket leaves the bytes at a
        # noncurrent version; one day is what makes the delete a purge.
        assert "noncurrent_days = 1" in rule, rule_id


def test_the_media_prefixes_are_granted_and_kept_out_of_infrequent_access() -> None:
    source = _module()
    default = (S3_MODULE / "variables.tf").read_text(encoding="utf-8")
    for prefix in ("assessment-raw", "assessment-compressed", "voice-answers"):
        assert f'"{prefix}"' in default[default.index('variable "application_prefixes"'):]
        short_lived = source[source.index("short_lived_prefixes = ["):]
        assert f'"{prefix}"' in short_lived[: short_lived.index("]")]
    assert "!contains(local.short_lived_prefixes, prefix)" in source
    grant = source[source.index('data "aws_iam_policy_document" "application"'):]
    for action in ("s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"):
        assert f'"{action}"' in grant


def test_the_bucket_policy_admits_a_multipart_upload_under_this_key() -> None:
    """UploadPart authorizes as s3:PutObject and carries no encryption header,
    so a deny on `StringNotEquals` (which matches an ABSENT header) refused
    every part of every recording. Each deny must be `IfExists`, or be the
    one that names aws:kms without this key.
    Mutation-checked against the previous single `StringNotEquals` statement.
    """
    source = _module()
    policy = source[source.index('data "aws_iam_policy_document" "private"'):]
    policy = policy[: policy.index('resource "aws_s3_bucket_policy" "private"')]
    header_tests = re.findall(
        r'test\s*=\s*"([^"]+)"\s*\n\s*variable\s*=\s*"s3:x-amz-server-side-encryption[^"]*"',
        policy,
    )
    assert header_tests, "no encryption condition found"
    assert "StringNotEquals" not in header_tests, header_tests
    assert header_tests.count("StringNotEqualsIfExists") == 2
    assert 'values   = [var.kms_key_arn]' in policy
    kms_without_key = policy[policy.index('sid    = "DenyKmsWithoutThisKey"'):]
    assert 'test     = "Null"' in kms_without_key[: kms_without_key.index("\n  }\n")]


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_every_environment_gives_the_task_worker_the_grant_and_the_key(
    environment: str,
) -> None:
    """The sweeps that delete media run on the task worker. With no object
    grant each deletion would answer AccessDenied, forever; with no key id
    every media write refuses."""
    source = (REPO / "infra" / "environments" / environment / "main.tf").read_text(
        encoding="utf-8"
    )
    attachment = source[source.index('resource "aws_iam_role_policy_attachment" "task_worker_s3"'):]
    attachment = attachment[: attachment.index("\n}\n")]
    assert 'execution_role_arns["task-worker"]' in attachment
    assert "module.s3.access_policy_arn" in attachment
    ecs = source[source.index('module "ecs"'):]
    lambda_ = source[source.index('module "lambda"'):]
    for block in (ecs, lambda_):
        assert "S3_KMS_KEY_ID" in block[: block.index("\n}\n")]


def test_speech_to_text_is_granted_to_the_worker_that_calls_it() -> None:
    """The video-interview transcription on the ECS agent is deleted; spoken
    answers are transcribed by a Route.LAMBDA task, so the grant and the
    three settings moved to the task worker."""
    source = (REPO / "infra" / "environments" / "pilot" / "main.tf").read_text(
        encoding="utf-8"
    )
    assert 'resource "aws_iam_role_policy" "agent_transcribe"' not in source
    grant = source[source.index('resource "aws_iam_role_policy" "task_worker_transcribe"'):]
    assert 'execution_role_arns["task-worker"]' in grant[: grant.index("\n}\n")]
    lambda_ = source[source.index('module "lambda"'):]
    lambda_ = lambda_[: lambda_.index("\n}\n")]
    ecs = source[source.index('module "ecs"'):]
    ecs = ecs[: ecs.index("\n}\n")]
    for name in ("TRANSCRIBE_ENABLED", "TRANSCRIBE_REGION", "TRANSCRIBE_BUCKET"):
        assert name in lambda_, name
    # The API only decides whether the microphone is offered, so it carries the
    # switch and nothing else; the region and bucket belong to the worker.
    assert "TRANSCRIBE_ENABLED" in ecs
    for name in ("TRANSCRIBE_REGION", "TRANSCRIBE_BUCKET"):
        assert name not in ecs, name
