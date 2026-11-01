"""Client-portal video access + dashboard metadata (2026-09-05 dashboard /
Executive Profile / video spec, sections 3-5 and 15-19).

Four layers:

* the WORDS, pure: every lifecycle status maps to one of the four dashboard
  words, the mode labels, and the report-availability vocabulary;
* the SHAPES, structural: no bucket or object key in any response schema, the
  three routes all gated on `view_review_screen`, the dashboard queries read
  the recording through a lateral join in the one statement (no N+1, no S3);
* the ROUTES, against the migrated test database (skip cleanly without it):
  metadata states for ready / processing / failed / conversational-no-video,
  the preview URL plus its audit row, the download consent gate in both
  directions, the cross-tenant 404, and the HIRING TEAM scope (2026-09-24):
  a Recruiter not assigned to the job is refused while an HR Manager, who
  reaches every job in the tenant, is not;
* the LEAK sweep over a real metadata response body.
"""
from __future__ import annotations

import inspect
import pathlib
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.services import assessment_video_access as video_access
from app.services.video import lifecycle

BACKEND = pathlib.Path(__file__).resolve().parents[1]


# ── The words (pure) ─────────────────────────────────────────────────────────


def test_every_lifecycle_status_maps_to_exactly_one_dashboard_word() -> None:
    """The whole machine, not a sample: a status added to the lifecycle must
    land in a word here or the dashboard renders a blank cell nobody notices."""
    for status in lifecycle.STATUSES:
        word = video_access.video_status_word(status)
        assert word in (
            video_access.VIDEO_READY,
            video_access.VIDEO_PROCESSING,
            video_access.VIDEO_FAILED,
        ), status
        assert word in video_access.VIDEO_STATUS_DETAIL
    assert video_access.video_status_word(lifecycle.READY) == video_access.VIDEO_READY
    for status in lifecycle.FAILURE_STATUSES:
        assert video_access.video_status_word(status) == video_access.VIDEO_FAILED
    assert video_access.video_status_word(None) == video_access.VIDEO_NONE


def test_a_session_with_no_recording_reads_no_recording_not_failed() -> None:
    """A session that never opened its recording (one recorded before the
    2026-09-22 ruling, or one that never started) is a normal state, never an
    error."""
    word = video_access.video_status_word(None)
    assert word == video_access.VIDEO_NONE
    assert "No video was recorded" in video_access.VIDEO_STATUS_DETAIL[word]


def test_mode_labels_are_the_specified_words() -> None:
    assert video_access.mode_label("video_interview") == "Video interview"
    assert video_access.mode_label("conversational") == "Conversational"
    # No session yet is Not started, never a claimed mode.
    assert video_access.mode_label(None) == "Not started"


def test_prism_word_derivation_matches_the_report_presence() -> None:
    assert (
        video_access.prism_status_word(has_report=True, conversation_status=None)
        == "Available"
    )
    # Finished assessment, synthesis not delivered yet: in flight.
    assert (
        video_access.prism_status_word(
            has_report=False, conversation_status="completed"
        )
        == "Processing"
    )
    # An unfinished assessment is not a report in flight.
    assert (
        video_access.prism_status_word(has_report=False, conversation_status="active")
        == "Not available"
    )
    assert (
        video_access.prism_status_word(has_report=False, conversation_status=None)
        == "Not available"
    )


def test_proctoring_word_reuses_the_existing_report_presence() -> None:
    assert (
        video_access.proctoring_status_word(
            has_proctoring_report=True, has_proctoring_session=True
        )
        == "Available"
    )
    assert (
        video_access.proctoring_status_word(
            has_proctoring_report=False, has_proctoring_session=True
        )
        == "Processing"
    )
    assert (
        video_access.proctoring_status_word(
            has_proctoring_report=False, has_proctoring_session=False
        )
        == "Not available"
    )


def test_servable_requires_ready_plus_the_playable_object() -> None:
    class _Rec:
        status = lifecycle.READY
        s3_compressed_key = "assessment-compressed/x/y/assessment.mp4"
        stored_format = "mp4"
        media_deleted_at = None

    rec = _Rec()
    assert video_access.is_servable(rec)
    rec.status = lifecycle.COMPRESSING
    assert not video_access.is_servable(rec)
    rec.status = lifecycle.READY
    rec.stored_format = "webm"
    assert not video_access.is_servable(rec)
    rec.stored_format = "mp4"
    rec.s3_compressed_key = ""
    assert not video_access.is_servable(rec)
    assert not video_access.is_servable(None)


def test_deleted_media_is_not_servable_however_ready_the_row_looks() -> None:
    """Retention and erasure delete OBJECTS; the row survives as provenance.
    A signed URL onto a key that is gone renders as a broken player, which is
    a worse answer than the sentence the dashboard already has for it."""
    from datetime import datetime, timezone

    class _Rec:
        status = lifecycle.READY
        s3_compressed_key = "assessment-compressed/x/y/assessment.mp4"
        stored_format = "mp4"
        media_deleted_at = None

    rec = _Rec()
    assert video_access.is_servable(rec)
    assert not video_access.media_deleted(rec)
    rec.media_deleted_at = datetime.now(timezone.utc)
    assert not video_access.is_servable(rec)
    assert video_access.media_deleted(rec)
    assert (
        video_access.video_status_word(lifecycle.READY, media_deleted=True)
        == video_access.VIDEO_NONE
    )


def test_can_retry_mirrors_the_lifecycles_own_retryable_set() -> None:
    """The UI must never offer a retry the endpoint would 409. Swept over the
    whole machine so a new failure state cannot silently diverge."""

    class _Rec:
        status = None

    rec = _Rec()
    for status in lifecycle.STATUSES:
        rec.status = status
        assert video_access.can_retry(rec) == (
            status in lifecycle.RETRYABLE_FAILURES
        ), status
    assert not video_access.can_retry(None)


# ── The shapes (structural) ──────────────────────────────────────────────────


def test_no_bucket_or_object_key_field_in_any_video_response_schema() -> None:
    """Spec section 13: never expose the S3 bucket or the object key. Swept
    over field NAMES so a future field cannot slip one in under a new name."""
    from app.schemas.videos import VideoAccessOut, VideoDeliveryOut

    forbidden = ("s3", "bucket", "key", "object")
    for model in (VideoAccessOut, VideoDeliveryOut):
        for name in model.model_fields:
            lowered = name.lower()
            for term in forbidden:
                assert term not in lowered, f"{model.__name__}.{name}"


def test_every_video_route_is_gated_on_view_review_screen() -> None:
    """The capability that opens the transcript and the PRISM Report gates the
    video too: reading the grade's evidence and watching it are one grant."""
    source = (BACKEND / "app" / "api" / "videos.py").read_text(encoding="utf-8")
    routes = source.count("@router.")
    assert routes == 3
    assert source.count("require_capability(caps.VIEW_REVIEW_SCREEN)") == routes


def test_the_metadata_path_touches_no_media_and_no_object_store() -> None:
    """Spec sections 3-5: the dashboard is metadata only. The metadata handler
    and both dashboard queries must never reach for the object store."""
    from app.api import videos as videos_api

    metadata_source = inspect.getsource(videos_api.video_metadata)
    assert "presigned" not in metadata_source
    assert "object_storage" not in metadata_source

    for module in ("services/job_candidates.py", "services/dashboard.py"):
        source = (BACKEND / "app" / module).read_text(encoding="utf-8")
        assert "object_storage" not in source, module
        assert "presigned" not in source, module


def test_the_dashboard_queries_read_the_recording_in_one_statement() -> None:
    """No N+1: the latest recording and the latest session are LATERAL joins
    inside the same statement as everything else, never a per-row query.

    The job page's candidate table reads the session only: its mode and
    video words left it in the stage 3 final sweeps, so a recording join
    there would be a query for a column nothing renders."""
    for module in ("services/job_candidates.py", "services/dashboard.py"):
        source = (BACKEND / "app" / module).read_text(encoding="utf-8")
        assert "LEFT JOIN LATERAL" in source, module
        assert "assessment_conversations ac" in source, module
        # The pipeline package stays untouched by the dashboard read path;
        # the words come from the delivery layer's pure functions.
        assert "from app.services.video import" not in source, module
    dashboard = (BACKEND / "app" / "services" / "dashboard.py").read_text(encoding="utf-8")
    assert "video_recordings vr" in dashboard
    table = (BACKEND / "app" / "services" / "job_candidates.py").read_text(encoding="utf-8")
    assert "video_recordings" not in table


def test_the_audit_actions_are_the_specifications() -> None:
    assert video_access.VIDEO_PREVIEW_REQUESTED == "video_preview_requested"
    assert video_access.VIDEO_DOWNLOAD_REQUESTED == "video_download_requested"


# ── The routes, against the migrated test database ───────────────────────────


async def _factory_or_skip():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings

    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT status FROM video_recordings LIMIT 0"))
    except Exception:  # noqa: BLE001 -- no migrated database reachable
        await engine.dispose()
        pytest.skip("no migrated database reachable for the video access routes")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class _Fx:
    def __init__(self) -> None:
        self.hr_user_id = uuid.uuid4()
        self.tenant_id = uuid.uuid4()
        self.other_tenant_id = uuid.uuid4()
        self.job_id = uuid.uuid4()
        self.cand_id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.link_id = uuid.uuid4()
        self.conv_id = uuid.uuid4()
        self.recording_id = uuid.uuid4()


async def _seed(
    factory,
    fx: _Fx,
    *,
    mode: str = "video_interview",
    recording_status: str | None = None,
    download_consent: bool | None = None,
    assigned: bool = True,
) -> None:
    from app.core.db import superadmin_scope
    from app.models import Candidate, Job, JobStatus, LinkSource, Tenant
    from app.models.assessment import AssessmentConversation
    from app.models.candidate import JobCandidateLink
    from app.models.dual_mode import VideoRecording
    from app.services.video import keys

    now = datetime.now(timezone.utc)
    async with factory() as s:
        async with s.begin():
            async with superadmin_scope(s):
                for tenant_id in (fx.tenant_id, fx.other_tenant_id):
                    s.add(Tenant(id=tenant_id, name=f"Vx {tenant_id.hex[:6]}",
                                 domain=f"{tenant_id}.vx.test"))
                await s.flush()
                # The two staff members the scope tests act as. The recruiter
                # is ASSIGNED to the job unless the test says otherwise: a
                # recording belongs to the job's hiring team (2026-09-24).
                for user_id, role in (
                    (fx.user_id, "recruiter"), (fx.hr_user_id, "hr_manager"),
                ):
                    await s.execute(
                        text(
                            "INSERT INTO users (id, tenant_id, email, full_name, "
                            "role, status) VALUES (:u, :t, :e, 'Staff', :r, 'active')"
                        ),
                        {"u": user_id, "t": fx.tenant_id,
                         "e": f"{user_id.hex[:12]}@vx.test", "r": role},
                    )
                s.add(Job(id=fx.job_id, tenant_id=fx.tenant_id, title="Engineer",
                          jd_json={}, status=JobStatus.ratified, ratified_at=now,
                          assessment_grade="non_managerial"))
                await s.flush()
                if assigned:
                    await s.execute(
                        text(
                            "INSERT INTO job_assignments (tenant_id, job_id, "
                            "user_id, assignment_role, active) "
                            "VALUES (:t, :j, :u, 'recruiter', true)"
                        ),
                        {"t": fx.tenant_id, "j": fx.job_id, "u": fx.user_id},
                    )
                s.add(Candidate(id=fx.cand_id, email=f"c{fx.cand_id.hex[:8]}@t.test",
                                full_name="Video Access Candidate",
                                consent_databank=False,
                                retain_video_consent=download_consent))
                await s.flush()
                s.add(JobCandidateLink(id=fx.link_id, tenant_id=fx.tenant_id,
                                       job_id=fx.job_id, candidate_id=fx.cand_id,
                                       source=LinkSource.fresh, status="applied"))
                # Flush the link BEFORE adding the conversation: there is no
                # ORM relationship between the two models, so the unit of work
                # has nothing to order their inserts by and the conversation
                # can reach the database first, violating its link FK.
                await s.flush()
                s.add(AssessmentConversation(
                    id=fx.conv_id, tenant_id=fx.tenant_id, job_id=fx.job_id,
                    job_candidate_link_id=fx.link_id, grade="non_managerial",
                    status="active", next_question_index=0, mode=mode,
                    started_at=now,
                ))
                await s.flush()
                if recording_status is not None:
                    ready = recording_status == lifecycle.READY
                    s.add(VideoRecording(
                        id=fx.recording_id, tenant_id=fx.tenant_id,
                        conversation_id=fx.conv_id, candidate_id=fx.cand_id,
                        job_candidate_link_id=fx.link_id,
                        status=recording_status, source_format="video/webm",
                        stored_format="mp4" if ready else None,
                        duration_seconds=1840.0 if ready else None,
                        compressed_size_bytes=48_324_721 if ready else None,
                        # The single raw object a pre-0126 row names; no
                        # builder for it survives, the stored key is the record.
                        s3_raw_key=(f"assessment-raw/{fx.conv_id}/"
                                    f"{fx.recording_id}/raw.webm"),
                        s3_compressed_key=keys.compressed_key(fx.conv_id,
                                                              fx.recording_id),
                        raw_deleted=ready,
                        started_at=now,
                    ))


async def _cleanup(factory, fx: _Fx) -> None:
    from app.core.db import superadmin_scope

    async with factory() as s:
        async with s.begin():
            async with superadmin_scope(s):
                await s.execute(
                    text("DELETE FROM tenants WHERE id IN (:a, :b)"),
                    {"a": str(fx.tenant_id), "b": str(fx.other_tenant_id)},
                )
                await s.execute(text("DELETE FROM candidates WHERE id = :c"),
                                {"c": str(fx.cand_id)})


def _user(fx: _Fx, *, tenant_id: uuid.UUID | None = None, hr: bool = False):
    from app.api.deps import CurrentUser
    from app.core.security import AUDIENCE_ORG
    from app.models import Role

    return CurrentUser(user_id=fx.hr_user_id if hr else fx.user_id,
                       tenant_id=tenant_id or fx.tenant_id,
                       role=Role.hr_manager if hr else Role.recruiter,
                       audience=AUDIENCE_ORG)


async def _metadata(fx: _Fx, factory, **user_kwargs):
    from app.api import videos as mod
    from app.core.db import superadmin_scope

    async with factory() as s:
        async with superadmin_scope(s):
            return await mod.video_metadata(
                fx.link_id, user=_user(fx, **user_kwargs), session=s
            )


def _fake_presigner(calls: list):
    def _presign(key: str, *, ttl_seconds: int, filename: str | None = None) -> str:
        calls.append({"key": key, "ttl": ttl_seconds, "filename": filename})
        return f"https://media.invalid/signed?expires={ttl_seconds}"

    return _presign


@pytest.mark.asyncio
async def test_metadata_states_across_the_lifecycle() -> None:
    engine, factory = await _factory_or_skip()
    fx = _Fx()
    try:
        # Conversational session, no recording row: honest No recording.
        await _seed(factory, fx, mode="conversational", recording_status=None)
        out = await _metadata(fx, factory)
        assert out.assessment_mode_label == "Conversational"
        assert out.video_status == "No recording"
        assert out.recording_id is None
        assert not out.preview_available and not out.download_available
        await _cleanup(factory, fx)

        # A row written by the deleted video-interview mode, mid-pipeline:
        # it still reads, labelled for what it was. Processing, nothing
        # offered yet.
        fx = _Fx()
        await _seed(factory, fx, recording_status=lifecycle.COMPRESSING)
        out = await _metadata(fx, factory)
        assert out.assessment_mode_label == "Video interview"
        assert out.video_status == "Processing"
        assert not out.preview_available and not out.download_available
        assert not out.can_retry
        await _cleanup(factory, fx)

        # Failed: Failed, retryable, still nothing to play.
        fx = _Fx()
        await _seed(factory, fx, recording_status=lifecycle.COMPRESSION_FAILED)
        out = await _metadata(fx, factory)
        assert out.video_status == "Failed"
        assert out.can_retry
        assert not out.preview_available
        await _cleanup(factory, fx)

        # Ready with consent: everything available, media facts present.
        fx = _Fx()
        await _seed(factory, fx, recording_status=lifecycle.READY,
                    download_consent=True)
        out = await _metadata(fx, factory)
        assert out.video_status == "Ready"
        assert out.preview_available and out.download_available
        assert out.duration_seconds == 1840.0
        assert out.compressed_size_bytes == 48_324_721
        assert out.download_blocked_reason is None
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


@pytest.mark.asyncio
async def test_metadata_response_leaks_no_bucket_or_object_key() -> None:
    """The sweep over a REAL response body: the object key prefixes must not
    appear anywhere in what the metadata route serializes."""
    engine, factory = await _factory_or_skip()
    fx = _Fx()
    try:
        await _seed(factory, fx, recording_status=lifecycle.READY,
                    download_consent=True)
        out = await _metadata(fx, factory)
        body = out.model_dump_json()
        for leaked in ("assessment-raw", "assessment-compressed", "s3://", ".mp4"):
            assert leaked not in body, leaked
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


@pytest.mark.asyncio
async def test_cross_tenant_metadata_is_404_never_403() -> None:
    from fastapi import HTTPException

    engine, factory = await _factory_or_skip()
    fx = _Fx()
    try:
        await _seed(factory, fx, recording_status=lifecycle.READY)
        with pytest.raises(HTTPException) as excinfo:
            await _metadata(fx, factory, tenant_id=fx.other_tenant_id)
        assert excinfo.value.status_code == 404
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


@pytest.mark.asyncio
async def test_preview_returns_a_time_limited_url_and_writes_the_audit_row(
    monkeypatch,
) -> None:
    from app.api import videos as mod
    from app.core.config import get_settings
    from app.core.db import superadmin_scope
    from app.services import object_storage

    engine, factory = await _factory_or_skip()
    fx = _Fx()
    calls: list = []
    monkeypatch.setattr(object_storage, "presigned_get_url", _fake_presigner(calls))
    try:
        await _seed(factory, fx, recording_status=lifecycle.READY)
        async with factory() as s:
            async with superadmin_scope(s):
                out = await mod.request_video_preview(
                    fx.link_id, user=_user(fx), session=s
                )
                assert out.disposition == "inline"
                assert out.filename is None
                assert out.url.startswith("https://media.invalid/")
                ttl = get_settings().video_preview_url_ttl_seconds
                assert out.expires_in_seconds == ttl
                assert calls and calls[0]["ttl"] == ttl
                # The URL was signed for the COMPRESSED playable object; the
                # raw key must never be read on the client path.
                assert calls[0]["key"].endswith("assessment.mp4")
                assert "assessment-raw" not in calls[0]["key"]
                # The audit row is in the same transaction as the grant.
                actions = (
                    await s.execute(
                        text("SELECT action, metadata_json FROM audit_log "
                             "WHERE target_id = :r"),
                        {"r": str(fx.recording_id)},
                    )
                ).all()
                assert [a[0] for a in actions] == [
                    video_access.VIDEO_PREVIEW_REQUESTED
                ]
                assert actions[0][1]["candidate_id"] == str(fx.cand_id)
                assert actions[0][1]["job_candidate_link_id"] == str(fx.link_id)
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


@pytest.mark.asyncio
async def test_preview_refuses_while_processing() -> None:
    from fastapi import HTTPException

    from app.api import videos as mod
    from app.core.db import superadmin_scope

    engine, factory = await _factory_or_skip()
    fx = _Fx()
    try:
        await _seed(factory, fx, recording_status=lifecycle.UPLOADED)
        async with factory() as s:
            async with superadmin_scope(s):
                with pytest.raises(HTTPException) as excinfo:
                    await mod.request_video_preview(
                        fx.link_id, user=_user(fx), session=s
                    )
        assert excinfo.value.status_code == 409
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


@pytest.mark.asyncio
async def test_download_is_refused_without_the_candidates_consent(
    monkeypatch,
) -> None:
    """`retain_video_consent` NULL (never asked) and False both keep the
    record view-only; the metadata says so instead of offering a dead button."""
    from fastapi import HTTPException

    from app.api import videos as mod
    from app.core.db import superadmin_scope
    from app.services import object_storage

    engine, factory = await _factory_or_skip()
    fx = _Fx()
    monkeypatch.setattr(object_storage, "presigned_get_url", _fake_presigner([]))
    try:
        await _seed(factory, fx, recording_status=lifecycle.READY,
                    download_consent=None)
        out = await _metadata(fx, factory)
        assert out.preview_available
        assert not out.download_available
        assert out.download_blocked_reason
        async with factory() as s:
            async with superadmin_scope(s):
                with pytest.raises(HTTPException) as excinfo:
                    await mod.request_video_download(
                        fx.link_id, user=_user(fx), session=s
                    )
        assert excinfo.value.status_code == 403
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


@pytest.mark.asyncio
async def test_download_succeeds_with_consent_and_audits(monkeypatch) -> None:
    from app.api import videos as mod
    from app.core.config import get_settings
    from app.core.db import superadmin_scope
    from app.services import object_storage

    engine, factory = await _factory_or_skip()
    fx = _Fx()
    calls: list = []
    monkeypatch.setattr(object_storage, "presigned_get_url", _fake_presigner(calls))
    try:
        await _seed(factory, fx, recording_status=lifecycle.READY,
                    download_consent=True)
        async with factory() as s:
            async with superadmin_scope(s):
                out = await mod.request_video_download(
                    fx.link_id, user=_user(fx), session=s
                )
                assert out.disposition == "attachment"
                assert out.expires_in_seconds == (
                    get_settings().video_download_url_ttl_seconds
                )
                # Ids only in the filename, no candidate name or email.
                assert out.filename == f"assessment-video-{fx.recording_id}.mp4"
                assert calls[0]["filename"] == out.filename
                actions = (
                    await s.execute(
                        text("SELECT action FROM audit_log WHERE target_id = :r"),
                        {"r": str(fx.recording_id)},
                    )
                ).scalars().all()
                assert actions == [video_access.VIDEO_DOWNLOAD_REQUESTED]
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_job_table_row_carries_the_new_metadata_words() -> None:
    """End to end through `ranked_candidates`: one query, and the row carries
    the report words. The mode and video words left the table in the stage 3
    final sweeps: the recording is reached from the report's video section."""
    from app.core.db import superadmin_scope
    from app.services import job_candidates

    engine, factory = await _factory_or_skip()
    fx = _Fx()
    try:
        await _seed(factory, fx, recording_status=lifecycle.READY)
        async with factory() as s:
            async with superadmin_scope(s):
                page = await job_candidates.ranked_candidates(s, fx.job_id)
        assert page.total == 1
        row = page.rows[0]
        assert "assessment_mode_label" not in row
        assert "video_status" not in row
        assert row["prism_report_status"] == "Not available"
        assert row["proctoring_report_status"] == "Not available"
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


# ── The hiring team, not the tenant (Vivekium release, 2026-09-24) ───────────


def test_every_video_route_asks_for_the_hiring_team() -> None:
    """The route dependency answers "may this role review at all"; the scope
    answer is `require_hiring_team`, and every route reaches it through the
    one gate function they already share."""
    source = (BACKEND / "app" / "api" / "videos.py").read_text(encoding="utf-8")
    gate = source[source.index("async def _link_or_404"):]
    gate = gate[: gate.index("\nasync def ", 1)]
    assert "video_access.require_hiring_team(" in gate
    # Scope BEFORE the closure gate and before anything is loaded.
    assert gate.index("require_hiring_team") < gate.index("require_readable")


@pytest.mark.asyncio
async def test_a_recruiter_not_assigned_to_the_job_cannot_watch_it(
    monkeypatch,
) -> None:
    """`view_review_screen` is SCOPED for the Recruiter (RBAC 24). Holding it
    on SOME job is not holding it on this one, so an unassigned Recruiter in
    the SAME tenant is refused with a 403, before any URL is minted."""
    from fastapi import HTTPException

    from app.api import videos as mod
    from app.core.db import superadmin_scope
    from app.services import object_storage

    engine, factory = await _factory_or_skip()
    fx = _Fx()
    calls: list = []
    monkeypatch.setattr(object_storage, "presigned_get_url", _fake_presigner(calls))
    try:
        await _seed(factory, fx, recording_status=lifecycle.READY,
                    download_consent=True, assigned=False)
        with pytest.raises(HTTPException) as metadata:
            await _metadata(fx, factory)
        assert metadata.value.status_code == 403
        async with factory() as s:
            async with superadmin_scope(s):
                with pytest.raises(HTTPException) as preview:
                    await mod.request_video_preview(
                        fx.link_id, user=_user(fx), session=s
                    )
        assert preview.value.status_code == 403
        assert calls == [], "a URL was minted for a refused request"
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_hr_manager_reaches_every_job_in_their_tenant(monkeypatch) -> None:
    """The HR Manager's cell is unscoped, exactly as for the report and the
    transcript: no assignment is needed, and nothing about the new scope may
    narrow a role the matrix does not narrow."""
    from app.api import videos as mod
    from app.core.db import superadmin_scope
    from app.services import object_storage

    engine, factory = await _factory_or_skip()
    fx = _Fx()
    monkeypatch.setattr(object_storage, "presigned_get_url", _fake_presigner([]))
    try:
        await _seed(factory, fx, recording_status=lifecycle.READY, assigned=False)
        async with factory() as s:
            async with superadmin_scope(s):
                out = await mod.request_video_preview(
                    fx.link_id, user=_user(fx, hr=True), session=s
                )
        assert out.disposition == "inline"
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


# ── G (2026-11): the stage a reviewer reads, deleted media, revoked consent ──


def _row(status: str, *, started_ago_minutes: int = 0, **fields):
    from datetime import timedelta
    from types import SimpleNamespace

    moment = datetime.now(timezone.utc) - timedelta(minutes=started_ago_minutes)
    base = dict(
        status=status, ended_at=moment, created_at=moment,
        processing_started_at=moment, processed_at=None, stored_at=None,
        media_deleted_at=None,
    )
    base.update(fields)
    return SimpleNamespace(**base)


def test_every_lifecycle_status_has_a_stage_and_a_sentence() -> None:
    for status in lifecycle.STATUSES:
        stage, _ = video_access.video_stage(_row(status))
        assert stage in (
            video_access.STAGE_RECORDING, video_access.STAGE_UPLOADED,
            video_access.STAGE_COMPRESSING, video_access.STAGE_STORING,
            video_access.STAGE_READY, video_access.STAGE_FAILED,
        ), status
        assert video_access.STAGE_DETAIL[stage]


def test_stages_name_what_the_pipeline_is_doing() -> None:
    assert video_access.video_stage(None)[0] == video_access.STAGE_MISSING
    deleted = _row(lifecycle.READY, media_deleted_at=datetime.now(timezone.utc))
    assert video_access.video_stage(deleted)[0] == video_access.STAGE_MISSING
    assert video_access.video_stage(_row(lifecycle.UPLOADED))[0] == video_access.STAGE_UPLOADED
    assert video_access.video_stage(_row(lifecycle.COMPRESSING))[0] == video_access.STAGE_COMPRESSING
    assert video_access.video_stage(_row(lifecycle.STORING))[0] == video_access.STAGE_STORING
    assert video_access.video_stage(_row(lifecycle.READY))[0] == video_access.STAGE_READY
    for failure in lifecycle.FAILURE_STATUSES:
        assert video_access.video_stage(_row(failure))[0] == video_access.STAGE_FAILED


def test_a_run_past_its_bounds_reads_stalled_not_in_progress() -> None:
    from app.services import assessment_media_retention as retention

    long_ago = int(retention.processing_stall_after().total_seconds() // 60) + 5
    assert video_access.video_stage(
        _row(lifecycle.COMPRESSING, started_ago_minutes=long_ago)
    )[0] == video_access.STAGE_STALLED
    assert video_access.video_stage(
        _row(lifecycle.UPLOADED, started_ago_minutes=24 * 60)
    )[0] == video_access.STAGE_STALLED
    assert video_access.video_stage(
        _row(lifecycle.UPLOADED, started_ago_minutes=1)
    )[0] == video_access.STAGE_UPLOADED


async def _expect_409(coro, expected_detail: str) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await coro
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == expected_detail


@pytest.mark.asyncio
async def test_deleted_media_is_refused_for_preview_and_download_and_never_signed(
    monkeypatch,
) -> None:
    from app.api import videos as mod
    from app.core.db import superadmin_scope
    from app.services import object_storage

    engine, factory = await _factory_or_skip()
    fx = _Fx()
    calls: list = []
    monkeypatch.setattr(object_storage, "presigned_get_url", _fake_presigner(calls))
    try:
        await _seed(factory, fx, recording_status=lifecycle.READY, download_consent=True)
        async with factory() as s:
            async with s.begin():
                async with superadmin_scope(s):
                    await s.execute(
                        text("UPDATE video_recordings SET media_deleted_at = now() WHERE id = :r"),
                        {"r": str(fx.recording_id)},
                    )
        out = await _metadata(fx, factory)
        assert out.video_stage == video_access.STAGE_MISSING
        assert out.video_stage_detail == video_access.VIDEO_DELETED_DETAIL
        assert not out.preview_available and not out.download_available
        async with factory() as s:
            async with superadmin_scope(s):
                await _expect_409(
                    mod.request_video_preview(fx.link_id, user=_user(fx), session=s),
                    video_access.VIDEO_DELETED_DETAIL,
                )
                await _expect_409(
                    mod.request_video_download(fx.link_id, user=_user(fx), session=s),
                    video_access.VIDEO_DELETED_DETAIL,
                )
        assert calls == [], "a URL was signed for deleted media"
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


@pytest.mark.asyncio
async def test_consent_revoked_after_a_grant_closes_download_on_the_next_request(
    monkeypatch,
) -> None:
    """Preview needs no download consent; download re-reads the candidate on
    EVERY request, so a revocation takes effect at once and each allowed
    request writes its own audit row."""
    from fastapi import HTTPException

    from app.api import videos as mod
    from app.core.db import superadmin_scope
    from app.services import object_storage

    engine, factory = await _factory_or_skip()
    fx = _Fx()
    monkeypatch.setattr(object_storage, "presigned_get_url", _fake_presigner([]))
    try:
        await _seed(factory, fx, recording_status=lifecycle.READY, download_consent=True)
        async with factory() as s:
            async with superadmin_scope(s):
                await mod.request_video_download(fx.link_id, user=_user(fx), session=s)
                await mod.request_video_preview(fx.link_id, user=_user(fx), session=s)
                await s.execute(
                    text("UPDATE candidates SET retain_video_consent = false WHERE id = :c"),
                    {"c": str(fx.cand_id)},
                )
                with pytest.raises(HTTPException) as excinfo:
                    await mod.request_video_download(fx.link_id, user=_user(fx), session=s)
                assert excinfo.value.status_code == 403
                still_previewable = await mod.request_video_preview(
                    fx.link_id, user=_user(fx), session=s
                )
                assert still_previewable.disposition == "inline"
                actions = (
                    await s.execute(
                        text("SELECT action FROM audit_log WHERE target_id = :r"),
                        {"r": str(fx.recording_id)},
                    )
                ).scalars().all()
                assert sorted(actions) == sorted([
                    video_access.VIDEO_DOWNLOAD_REQUESTED,
                    video_access.VIDEO_PREVIEW_REQUESTED,
                    video_access.VIDEO_PREVIEW_REQUESTED,
                ])
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


def test_a_presigned_url_expires_by_setting_and_the_response_says_when() -> None:
    from app.core.config import get_settings

    settings = get_settings()
    assert settings.video_preview_url_ttl_seconds > 0
    assert settings.video_download_url_ttl_seconds > 0
    source = (BACKEND / "app" / "api" / "videos.py").read_text(encoding="utf-8")
    assert "expires_in_seconds=ttl" in source
