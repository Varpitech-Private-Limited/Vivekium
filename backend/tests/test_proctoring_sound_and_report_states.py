"""Mission 2026-11-01, items A, B and F on the server side.

B. SUSTAINED HIGH SOUND LEVEL is an advisory. The server stores it on Path C
   only when the duration clears the sustained window, it is not inside the
   cooldown and no spoken answer was being captured; it never reaches the
   warning counter; the report words it with its hedge.
A. A camera analysis that kept failing, or monitoring that stopped responding,
   makes the Proctoring Report say so (`monitoring_was_incomplete`) rather than
   read as an all-clear.
F. The PRISM Report states WHY a Proctoring Report is absent (`state_of`,
   `load_state`), the hourly reconciler re-dispatches an ended session that
   has no report, and a late or duplicated report writes exactly one row.

Database and Redis tests run against the real thing, like the pipeline tests
they borrow their fixtures from.
"""
from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.models.proctoring import (
    OUTCOME_ABANDONED,
    OUTCOME_ACTIVE,
    OUTCOME_COMPLETED,
    ProctoringReport,
    ProctoringSession,
    POLICY_CONTINUE_AND_NOTE,
    QUALITY_GOOD,
)
from app.schemas.proctoring import EventBatchIn, EventIn
from app.services.proctoring import audio as proctoring_audio
from app.services.proctoring import catalog, ingestion, phrasing, state
from app.services.proctoring import report as proctoring_report
from app.services.proctoring.config import get_config
from app.workers import tasks_proctoring
from tests.test_proctoring_pipeline import (
    _batch,
    _cleanup,
    _events,
    _factory_or_skip,
    _Fx,
    _load,
    _seed,
)

CONFIG = get_config()
MS = 1000
LOUD = catalog.SOUND_LEVEL
NOW = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
OVERDUE = timedelta(minutes=CONFIG.report_overdue_minutes)


def _audio_service_configured(monkeypatch) -> None:
    """The test deployment has no analysis service, so every report would
    (correctly) say audio monitoring was unavailable. A test about a report
    with COMPLETE monitoring needs one configured."""
    configured = dataclasses.replace(CONFIG, analysis_service_url="http://analysis.test")
    monkeypatch.setattr(proctoring_report, "get_config", lambda: configured)


async def _no_spoken_answer(*_args, **_kwargs) -> bool:
    return False


async def _spoken_answer_open(*_args, **_kwargs) -> bool:
    return True


def _loud_batch(duration_s: float, **metadata) -> EventBatchIn:
    return EventBatchIn(
        events=[
            EventIn(
                event_type=LOUD,
                occurred_at=datetime.now(timezone.utc),
                duration_ms=int(duration_s * MS),
                metadata=metadata,
            )
        ]
    )


async def _ingest(factory, fx, batch, *, now=None):
    from app.core.db import superadmin_scope

    async with factory() as s:
        async with s.begin():
            async with superadmin_scope(s):
                ps = await _load(s, fx)
                return await ingestion.ingest(
                    s, ps, POLICY_CONTINUE_AND_NOTE, batch,
                    now=now or datetime.now(timezone.utc), enqueue=fx.enqueue,
                )


async def _stored(factory, fx) -> list:
    from app.core.db import superadmin_scope

    async with factory() as s:
        async with superadmin_scope(s):
            return await _events(s, fx)


# ══════════════════════════════════════════════════════════════════════════
# B. The sound-level advisory
# ══════════════════════════════════════════════════════════════════════════


def test_the_advisory_is_path_c_and_can_never_reach_the_warning_counter() -> None:
    spec = catalog.spec_for(LOUD)
    assert spec.path == catalog.PATH_C
    assert not spec.warns and not spec.terminates and not spec.pauses
    assert LOUD not in catalog.WARNING_EVENTS
    assert LOUD in catalog.CLIENT_EMITTABLE


@pytest.mark.asyncio
async def test_a_sustained_level_is_stored_as_an_advisory_and_never_warns(monkeypatch) -> None:
    monkeypatch.setattr(proctoring_audio, "voice_capture_open", _no_spoken_answer)
    engine, factory = await _factory_or_skip()
    fx = _Fx()
    try:
        await _seed(factory, fx)
        result = await _ingest(
            factory, fx,
            _loud_batch(
                CONFIG.loud_sustained_seconds + 2,
                peak_dbfs=-9.87, floor_dbfs=-61.24, character="voice_like",
                transcript="must not be kept", peak_db_spl=110,
            ),
        )
        assert result.warning is None and result.termination is None
        assert result.warnings_used == 0
        rows = await _stored(factory, fx)
        assert [r.event_type for r in rows] == [LOUD]
        assert rows[0].path == catalog.PATH_C
        assert rows[0].warning_issued is False
        # Only the whitelisted fields, rounded, and the record's own caveat.
        assert rows[0].metadata_json == {
            "measure": ingestion.LOUD_MEASURE,
            "peak_dbfs": -9.9,
            "floor_dbfs": -61.2,
            "character": "voice_like",
        }
        assert await state.warnings_used(fx.ps_id) == 0
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_level_shorter_than_the_sustained_window_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(proctoring_audio, "voice_capture_open", _no_spoken_answer)
    engine, factory = await _factory_or_skip()
    fx = _Fx()
    try:
        await _seed(factory, fx)
        await _ingest(factory, fx, _loud_batch(CONFIG.loud_sustained_seconds - 1))
        # A report with no duration at all is refused too: nothing is trusted
        # to be "sustained" on the browser's word alone.
        await _ingest(
            factory, fx,
            EventBatchIn(events=[EventIn(event_type=LOUD, occurred_at=datetime.now(timezone.utc))]),
        )
        assert await _stored(factory, fx) == []
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_second_advisory_inside_the_cooldown_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(proctoring_audio, "voice_capture_open", _no_spoken_answer)
    engine, factory = await _factory_or_skip()
    fx = _Fx()
    try:
        await _seed(factory, fx)
        for _ in range(3):
            await _ingest(factory, fx, _loud_batch(CONFIG.loud_sustained_seconds + 1))
        assert len(await _stored(factory, fx)) == 1
        # Three advisories later the session still has every warning left.
        assert await state.warnings_used(fx.ps_id) == 0
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_level_during_a_spoken_answer_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(proctoring_audio, "voice_capture_open", _spoken_answer_open)
    engine, factory = await _factory_or_skip()
    fx = _Fx()
    try:
        await _seed(factory, fx)
        await _ingest(factory, fx, _loud_batch(CONFIG.loud_sustained_seconds + 5))
        assert await _stored(factory, fx) == []
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_report_words_the_advisory_with_its_hedge_and_no_accusation(monkeypatch) -> None:
    monkeypatch.setattr(proctoring_audio, "voice_capture_open", _no_spoken_answer)
    _audio_service_configured(monkeypatch)
    engine, factory = await _factory_or_skip()
    fx = _Fx()
    try:
        await _seed(factory, fx)
        await _ingest(factory, fx, _loud_batch(CONFIG.loud_sustained_seconds + 4))
        from app.core.db import superadmin_scope

        async with factory() as s:
            async with s.begin():
                async with superadmin_scope(s):
                    ps = await _load(s, fx)
                    ps.outcome = OUTCOME_COMPLETED
                    ps.ended_at = datetime.now(timezone.utc)
                    await s.flush()
                    content = (await proctoring_report.generate(s, ps)).report_content
        audio = " ".join(content["findings"]["audio"])
        assert "sustained high sound level" in audio
        assert "never earns a warning" in audio
        assert "cannot tell shouting from a loud room" in audio
        assert "The candidate was not warned at any point." in content["summary"]
        assert content["monitoring_was_incomplete"] is False
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


# ══════════════════════════════════════════════════════════════════════════
# A. A camera that could not see is not a camera that saw nothing
# ══════════════════════════════════════════════════════════════════════════


def _ps(**overrides):
    base = dict(
        started_at=NOW, consented_at=NOW, ended_at=NOW + timedelta(minutes=30),
        outcome=OUTCOME_COMPLETED, warnings_used=0, termination_reason=None,
        session_quality=QUALITY_GOOD,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _view(event_type: str, *, at_minutes: int = 1, **metadata) -> proctoring_report.EventView:
    return proctoring_report.EventView(
        event_type=event_type,
        occurred_at=NOW + timedelta(minutes=at_minutes),
        duration_ms=None,
        path=catalog.spec_for(event_type).path,
        warning_issued=False,
        warning_number=None,
        metadata=metadata,
    )


def _compose(events, *, audio_available=True, **ps):
    return proctoring_report.compose(
        candidate_name="Test Candidate", assessment_name="Tatva Assessment for Tester",
        ps=_ps(**ps), events=events, audio_available=audio_available,
    )


def test_a_clean_session_with_no_events_reads_as_genuinely_clean() -> None:
    content = _compose([])
    assert content["monitoring_was_incomplete"] is False
    assert content["activity_log"] == []
    assert phrasing.NO_EVENTS_AT_ALL in content["summary"]


def test_failing_camera_analysis_is_stated_and_marks_the_report_incomplete() -> None:
    content = _compose([_view("SESSION_QUALITY_DEGRADED", note=phrasing.CAMERA_ANALYSIS_FAILING_NOTE)])
    assert content["monitoring_was_incomplete"] is True
    assert phrasing.CAMERA_ANALYSIS_FAILING in content["summary"]
    # It is not mistaken for the slow-device sentence.
    assert phrasing.MONITORING_REDUCED_RATE not in content["summary"]
    assert content["activity_log"][0]["what_happened"] == phrasing.CAMERA_ANALYSIS_FAILING_ACTIVITY


def test_monitoring_that_stopped_responding_is_stated_and_marks_the_report_incomplete() -> None:
    content = _compose(
        [_view("INTEGRITY_CHECK_WARNING", components=["models"], source="heartbeat")]
    )
    assert content["monitoring_was_incomplete"] is True
    assert phrasing.MONITORING_COMPONENT_STOPPED in content["summary"]


def test_a_slow_device_is_still_stated_as_a_reduced_rate() -> None:
    content = _compose([_view("SESSION_QUALITY_DEGRADED", note="inference_below_normal_rate")])
    assert phrasing.MONITORING_REDUCED_RATE in content["summary"]
    assert content["monitoring_was_incomplete"] is True


def test_no_speaker_analysis_is_surfaced_in_the_audio_findings() -> None:
    content = _compose([], audio_available=False)
    assert phrasing.AUDIO_UNAVAILABLE in content["findings"]["audio"]
    assert content["monitoring_was_incomplete"] is True


def test_the_phone_is_in_the_report_when_the_server_kept_it() -> None:
    event = proctoring_report.EventView(
        event_type="DEVICE_DETECTED_PHONE", occurred_at=NOW + timedelta(minutes=2),
        duration_ms=None, path=catalog.PATH_B, warning_issued=True, warning_number=1, metadata={},
    )
    content = _compose([event], warnings_used=1)
    assert "phone" in " ".join(content["findings"]["camera"]).lower()
    assert "warned once" in content["summary"]


# ══════════════════════════════════════════════════════════════════════════
# F. Why is there no Proctoring Report?
# ══════════════════════════════════════════════════════════════════════════


def _row(**content):
    return SimpleNamespace(report_content=content)


def _state(ps, report):
    return proctoring_report.state_of(ps, report, now=NOW, overdue=OVERDUE)


def test_every_state_is_distinct_and_none_reads_as_an_all_clear() -> None:
    ended = _ps(ended_at=NOW - timedelta(minutes=1))
    cases = {
        proctoring_report.STATE_NO_SESSION: _state(None, None),
        proctoring_report.STATE_IN_PROGRESS: _state(_ps(outcome=OUTCOME_ACTIVE, ended_at=None), None),
        proctoring_report.STATE_GENERATING: _state(ended, None),
        proctoring_report.STATE_FAILED: _state(_ps(ended_at=NOW - OVERDUE - timedelta(minutes=1)), None),
        proctoring_report.STATE_PARTIAL: _state(
            ended, _row(monitoring_was_incomplete=True, activity_log=[{"time": "10:01"}])
        ),
        proctoring_report.STATE_NO_EVENTS: _state(
            ended, _row(monitoring_was_incomplete=False, activity_log=[])
        ),
    }
    for expected, (got, message) in cases.items():
        assert got == expected
        assert message, expected
        assert "no issues" not in message.lower(), expected
    assert len({state_name for state_name, _ in cases.values()}) == len(cases)
    assert _state(ended, _row(monitoring_was_incomplete=False, activity_log=[{"time": "10:01"}])) == (
        proctoring_report.STATE_READY, None
    )


def test_a_report_that_is_just_late_is_generating_and_a_long_overdue_one_is_failed() -> None:
    assert _state(_ps(ended_at=NOW - OVERDUE + timedelta(seconds=5)), None)[0] == "generating"
    assert _state(_ps(ended_at=NOW - OVERDUE - timedelta(seconds=5)), None)[0] == "failed"


def test_the_state_messages_carry_no_digit_and_no_em_dash() -> None:
    for message in phrasing.REPORT_STATE_MESSAGES.values():
        assert not any(ch.isdigit() for ch in message), message
        assert "—" not in message


@pytest.mark.asyncio
async def test_load_state_follows_the_session_through_its_life(monkeypatch) -> None:
    _audio_service_configured(monkeypatch)
    engine, factory = await _factory_or_skip()
    fx = _Fx()
    try:
        from app.core.db import superadmin_scope

        await _seed(factory, fx)
        async with factory() as s:
            async with s.begin():
                async with superadmin_scope(s):
                    assert (await proctoring_report.load_state(s, uuid.uuid4())).state == "no_session"
                    assert (await proctoring_report.load_state(s, fx.link_id)).state == "in_progress"
                    ps = await _load(s, fx)
                    ps.outcome = OUTCOME_COMPLETED
                    ps.ended_at = datetime.now(timezone.utc)
                    await s.flush()
                    assert (await proctoring_report.load_state(s, fx.link_id)).state == "generating"
                    ps.ended_at = datetime.now(timezone.utc) - OVERDUE - timedelta(minutes=5)
                    await s.flush()
                    failed = await proctoring_report.load_state(s, fx.link_id)
                    assert failed.state == "failed" and failed.message
                    await proctoring_report.generate(s, ps)
                    assert (await proctoring_report.load_state(s, fx.link_id)).state == "no_events"
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_late_report_for_a_long_finished_session_is_written_once() -> None:
    engine, factory = await _factory_or_skip()
    fx = _Fx()
    try:
        from app.core.db import superadmin_scope

        await _seed(factory, fx)
        async with factory() as s:
            async with s.begin():
                async with superadmin_scope(s):
                    ps = await _load(s, fx)
                    ps.outcome = OUTCOME_COMPLETED
                    ps.ended_at = datetime.now(timezone.utc) - timedelta(days=3)

        async def _generate() -> uuid.UUID:
            async with factory() as s:
                async with s.begin():
                    async with superadmin_scope(s):
                        ps = await _load(s, fx)
                        return (await proctoring_report.generate(s, ps)).id

        ids = await asyncio.gather(_generate(), _generate(), _generate())
        assert len(set(ids)) == 1
        async with factory() as s:
            async with superadmin_scope(s):
                rows = (
                    await s.execute(
                        select(ProctoringReport).where(
                            ProctoringReport.proctoring_session_id == fx.ps_id
                        )
                    )
                ).scalars().all()
                assert len(rows) == 1
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_report_with_several_warnings_lists_each_with_its_number() -> None:
    engine, factory = await _factory_or_skip()
    fx = _Fx()
    try:
        from app.core.db import superadmin_scope

        await _seed(factory, fx)
        for _ in range(3):
            await _ingest(factory, fx, _batch(("WINDOW_FOCUS_LOST", 30_000)))
        async with factory() as s:
            async with s.begin():
                async with superadmin_scope(s):
                    ps = await _load(s, fx)
                    ps.outcome = OUTCOME_COMPLETED
                    ps.ended_at = datetime.now(timezone.utc)
                    await s.flush()
                    content = (await proctoring_report.generate(s, ps)).report_content
        assert "three times" in content["outcome"] or "three" in content["outcome"]
        warned = [row for row in content["activity_log"] if "warning" in row["what_the_system_did"].lower()]
        assert len(warned) == 3
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()


# ══════════════════════════════════════════════════════════════════════════
# F. The reconciler and the report task
# ══════════════════════════════════════════════════════════════════════════


def _run_with_factory(work):
    async def _go():
        engine, factory = await _factory_or_skip()
        try:
            return await work(factory)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


@pytest.fixture
def dispatched(monkeypatch):
    """Record what the sweep hands to the dispatcher, and how."""
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        tasks_proctoring, "dispatch",
        lambda name, *, args=None, kwargs=None: calls.append(("now", name, args[0])),
    )
    monkeypatch.setattr(
        tasks_proctoring, "dispatch_after_commit",
        lambda session, name, *, args=None, kwargs=None: calls.append(("after_commit", name, args[0])),
    )
    return calls


async def _end(factory, fx, *, outcome=OUTCOME_COMPLETED, ended_ago: timedelta, report=False) -> None:
    from app.core.db import superadmin_scope

    async with factory() as s:
        async with s.begin():
            async with superadmin_scope(s):
                ps = await _load(s, fx)
                ps.outcome = outcome
                ps.ended_at = datetime.now(timezone.utc) - ended_ago
                await s.flush()
                if report:
                    await proctoring_report.generate(s, ps)


def _seed_and_end(fx, **kwargs):
    async def _work(factory):
        await _seed(factory, fx, completed=True)
        await _end(factory, fx, **kwargs)

    _run_with_factory(_work)


def _clean(fx):
    async def _work(factory):
        await _cleanup(factory, fx)

    _run_with_factory(_work)


REPORT_TASK = "pickready.generate_proctoring_report"


def test_the_sweep_redispatches_an_ended_session_that_never_got_its_report(dispatched) -> None:
    fx = _Fx()
    try:
        _seed_and_end(fx, ended_ago=OVERDUE + timedelta(hours=1))
        tasks_proctoring.reconcile_proctoring_sessions()
        assert ("after_commit", REPORT_TASK, str(fx.link_id)) in dispatched
    finally:
        _clean(fx)


def test_the_sweep_leaves_a_recent_an_old_and_an_already_reported_session_alone(dispatched) -> None:
    recent, old, reported = _Fx(), _Fx(), _Fx()
    try:
        _seed_and_end(recent, ended_ago=timedelta(minutes=1))
        _seed_and_end(old, ended_ago=timedelta(days=tasks_proctoring._REPORT_RETRY_WINDOW_DAYS + 5))
        _seed_and_end(reported, ended_ago=OVERDUE + timedelta(hours=1), report=True)
        tasks_proctoring.reconcile_proctoring_sessions()
        mine = {str(fx.link_id) for fx in (recent, old, reported)}
        assert [call for call in dispatched if call[2] in mine] == []
    finally:
        for fx in (recent, old, reported):
            _clean(fx)


def test_the_sweep_abandons_a_silent_session_and_orders_its_report_after_the_commit(dispatched) -> None:
    fx = _Fx()

    async def _work(factory):
        await _seed(factory, fx, heartbeat_at=datetime.now(timezone.utc) - timedelta(days=30))

    try:
        _run_with_factory(_work)
        tasks_proctoring.reconcile_proctoring_sessions()

        async def _check(factory):
            from app.core.db import superadmin_scope

            async with factory() as s:
                async with superadmin_scope(s):
                    return (await s.get(ProctoringSession, fx.ps_id)).outcome

        assert _run_with_factory(_check) == OUTCOME_ABANDONED
        # After the commit, never before it: a report task that started first
        # would read the session as still active and defer.
        assert ("after_commit", REPORT_TASK, str(fx.link_id)) in dispatched
        assert ("now", REPORT_TASK, str(fx.link_id)) not in dispatched
    finally:
        _clean(fx)


def test_the_report_task_defers_while_the_assessment_is_still_open() -> None:
    fx = _Fx()

    async def _work(factory):
        await _seed(factory, fx, completed=False)

    async def _check(factory):
        from app.core.db import superadmin_scope

        async with factory() as s:
            async with superadmin_scope(s):
                ps = await s.get(ProctoringSession, fx.ps_id)
                rows = (
                    await s.execute(
                        text("SELECT count(*) FROM proctoring_reports WHERE proctoring_session_id = :p"),
                        {"p": str(fx.ps_id)},
                    )
                ).scalar_one()
                return ps.outcome, rows

    try:
        _run_with_factory(_work)
        tasks_proctoring.generate_proctoring_report(str(fx.link_id))
        assert _run_with_factory(_check) == (OUTCOME_ACTIVE, 0)
    finally:
        _clean(fx)


def test_the_report_task_closes_a_completed_assessment_and_writes_its_report_once() -> None:
    fx = _Fx()

    async def _work(factory):
        await _seed(factory, fx, completed=True)

    async def _check(factory):
        from app.core.db import superadmin_scope

        async with factory() as s:
            async with superadmin_scope(s):
                ps = await s.get(ProctoringSession, fx.ps_id)
                rows = (
                    await s.execute(
                        text("SELECT count(*) FROM proctoring_reports WHERE proctoring_session_id = :p"),
                        {"p": str(fx.ps_id)},
                    )
                ).scalar_one()
                return ps.outcome, rows

    try:
        _run_with_factory(_work)
        tasks_proctoring.generate_proctoring_report(str(fx.link_id))
        # A retried or duplicated delivery is harmless.
        tasks_proctoring.generate_proctoring_report(str(fx.link_id))
        assert _run_with_factory(_check) == (OUTCOME_COMPLETED, 1)
    finally:
        _clean(fx)


@pytest.mark.asyncio
async def test_without_an_analysis_service_the_stored_report_says_so(monkeypatch) -> None:
    """The deployed state of an unconfigured or scaled-down analysis service:
    the report is `partially_unavailable`, never a clean-looking `no_events`."""
    engine, factory = await _factory_or_skip()
    fx = _Fx()
    try:
        from app.core.db import superadmin_scope

        await _seed(factory, fx)
        async with factory() as s:
            async with s.begin():
                async with superadmin_scope(s):
                    ps = await _load(s, fx)
                    ps.outcome = OUTCOME_COMPLETED
                    ps.ended_at = datetime.now(timezone.utc)
                    await s.flush()
                    content = (await proctoring_report.generate(s, ps)).report_content
                    out = await proctoring_report.load_state(s, fx.link_id)
        assert phrasing.AUDIO_UNAVAILABLE in content["findings"]["audio"]
        assert out.state == proctoring_report.STATE_PARTIAL
    finally:
        await _cleanup(factory, fx)
        await engine.dispose()
