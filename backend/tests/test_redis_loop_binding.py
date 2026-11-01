"""One redis-py client per event loop, and the warm-worker defect it fixed.

WHY THESE TESTS RUN `asyncio.run` THEMSELVES
--------------------------------------------
The defect only exists across TWO event loops: `workers/runtime._run` gives every
task body its own `asyncio.run`, so on a warm Lambda the second invocation used a
client built on the first invocation's loop. A pytest-asyncio test runs inside
one loop and structurally cannot see it, so every test here is synchronous and
opens its loops explicitly, exactly as the worker does.

Mutation check: put `workers/status.py` back on a module-global client and
`test_a_status_written_in_one_loop_reads_back_in_the_next` fails, because the
second loop's read raises inside redis-py and reads PENDING.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

import pytest
from redis.exceptions import RedisError

from app.core import cache
from app.core.redis_loop import LoopBoundRedis
from app.services import web_research
from app.workers import status


def _redis_reachable() -> bool:
    async def _ping() -> bool:
        probe = LoopBoundRedis(name="probe", socket_timeout=1, connect_timeout=1)
        client = probe.client()
        if client is None:
            return False
        try:
            return bool(await client.ping())
        except (RedisError, OSError):
            return False
        finally:
            await probe.aclose()

    return asyncio.run(_ping())


requires_redis = pytest.mark.skipif(
    not _redis_reachable(),
    reason="no Redis at REDIS_URL; the compose test stack provides one on 6381",
)


@pytest.fixture(autouse=True)
def _fresh_clients():
    for bound in (cache._CLIENT, status._CLIENT, web_research._BREAKER_CLIENT):
        bound.reset_for_tests()
    yield
    for bound in (cache._CLIENT, status._CLIENT, web_research._BREAKER_CLIENT):
        bound.reset_for_tests()


def test_a_new_loop_gets_a_new_client() -> None:
    bound = LoopBoundRedis(name="t", socket_timeout=1, connect_timeout=1)

    async def _get():
        return bound.client()

    first = asyncio.run(_get())
    second = asyncio.run(_get())
    assert first is not None and second is not None
    assert first is not second, "a client carried into a second loop fails every call"


def test_one_loop_reuses_its_client() -> None:
    bound = LoopBoundRedis(name="t", socket_timeout=1, connect_timeout=1)

    async def _twice():
        return bound.client(), bound.client()

    a, b = asyncio.run(_twice())
    assert a is b


def test_the_four_callers_share_the_one_implementation() -> None:
    """Several copies of "a client for this loop" is how two of them were
    wrong. The proctoring state joined in the stage 3 final sweeps."""
    from app.services.proctoring import state as proctoring_state

    assert isinstance(cache._CLIENT, LoopBoundRedis)
    assert isinstance(status._CLIENT, LoopBoundRedis)
    assert isinstance(web_research._BREAKER_CLIENT, LoopBoundRedis)
    assert isinstance(proctoring_state._CLIENT, LoopBoundRedis)


def test_the_proctoring_state_fails_closed_when_no_client_can_be_built(
    monkeypatch,
) -> None:
    """The cache reads None as a miss; the proctoring gate must not. A
    decision that could not be made answers 503, never "nothing happened"."""
    import pytest

    from app.services.proctoring import state as proctoring_state

    monkeypatch.setattr(proctoring_state._CLIENT, "client", lambda: None)
    with pytest.raises(proctoring_state.StateUnavailable):
        proctoring_state._redis()


def test_a_build_failure_is_logged_and_latched_for_that_loop_only(
    monkeypatch, caplog
) -> None:
    import redis.asyncio as redis_asyncio

    bound = LoopBoundRedis(name="latched", socket_timeout=1, connect_timeout=1)
    real_from_url = redis_asyncio.from_url
    calls: list[int] = []

    def refuse(*_args, **_kwargs):
        calls.append(1)
        raise ConnectionError("refused")

    async def _twice():
        return bound.client(), bound.client()

    monkeypatch.setattr(redis_asyncio, "from_url", refuse)
    with caplog.at_level(logging.WARNING, logger="app.core.redis_loop"):
        first, again = asyncio.run(_twice())
    assert first is None and again is None
    assert len(calls) == 1, "one failed build per loop, not one per call"
    assert any("redis_loop.unavailable" in r.getMessage() for r in caplog.records)

    monkeypatch.setattr(redis_asyncio, "from_url", real_from_url)

    async def _once():
        return bound.client()

    assert asyncio.run(_once()) is not None, "the next loop must try again"


@requires_redis
def test_a_status_written_in_one_loop_reads_back_in_the_next() -> None:
    run_id = f"loop-binding-{uuid.uuid4().hex}"

    asyncio.run(status.write(run_id, status.STATE_PROGRESS, {"stage": "one"}))
    read_back = asyncio.run(status.read(run_id))
    assert read_back.state == status.STATE_PROGRESS
    assert read_back.payload == {"stage": "one"}

    asyncio.run(status.write(run_id, status.STATE_SUCCESS, {"stage": "two"}))
    final = asyncio.run(status.read(run_id))
    assert final.state == status.STATE_SUCCESS
    assert final.payload == {"stage": "two"}


def test_a_status_write_failure_is_a_warning_naming_the_run_and_never_the_payload(
    monkeypatch, caplog
) -> None:
    class _Failing:
        async def set(self, *_args, **_kwargs):
            raise ConnectionError("down")

        async def get(self, *_args, **_kwargs):
            raise ConnectionError("down")

    monkeypatch.setattr(status, "_redis", lambda: _Failing())
    with caplog.at_level(logging.WARNING, logger="app.workers.status"):
        asyncio.run(status.write("run-1", status.STATE_PROGRESS, {"secret": "PAYLOAD"}))
        result = asyncio.run(status.read("run-1"))

    assert result.state == status.STATE_UNKNOWN  # unreadable cache, no ledger row: never PENDING
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("taskrun.write_failed run_id=run-1" in m for m in messages), messages
    assert any("taskrun.read_failed run_id=run-1" in m for m in messages), messages
    assert all("PAYLOAD" not in m for m in messages)


@requires_redis
def test_the_web_search_breaker_counts_across_loops() -> None:
    """Three failures recorded in three separate loops open the breaker, which
    a fourth loop can read. On the old process-global client the second loop's
    INCR raised and was reduced to a warning, so the breaker never opened."""
    asyncio.run(web_research._clear_breaker())
    try:
        for _ in range(web_research._FAILURE_THRESHOLD):
            asyncio.run(web_research._record_failure())
        assert asyncio.run(web_research._breaker_retry_after()) > 0
    finally:
        assert asyncio.run(web_research._clear_breaker()) is True


def test_close_never_raises_and_forgets_the_client() -> None:
    bound = LoopBoundRedis(name="closing", socket_timeout=1, connect_timeout=1)

    async def _build_and_close():
        assert bound.client() is not None
        await bound.aclose()
        return bound._client  # noqa: SLF001

    assert asyncio.run(_build_and_close()) is None
