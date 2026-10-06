from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

import backend.worker as module
from backend.api.deps import Services
from backend.config import PlatformSettings
from backend.queue import ClaimedJob
from backend.storage import InMemoryBlobStorage
from backend.worker import Worker


def job(*, attempts: int = 1, maximum: int = 2, kind: str = "extract_resume") -> ClaimedJob:
    return ClaimedJob(uuid.uuid4(), uuid.uuid4(), kind, {}, attempts, maximum)


def services(**settings) -> Services:
    config = PlatformSettings(
        _env_file=None,
        APP_ENV="test",
        QUEUE_LOCK_TIMEOUT_S=1,
        WORKER_POLL_INTERVAL_S=0.01,
        WORKER_CONCURRENCY=1,
        WORKER_SHUTDOWN_TIMEOUT_S=0.1,
        **settings,
    )
    queue = AsyncMock()
    queue.renew.return_value = True
    return Services(config, AsyncMock(), InMemoryBlobStorage(), queue)


@pytest.mark.parametrize("kind", ["extract_resume", "screen_application", "delete_candidate", "retention_sweep"])
def test_default_handlers_are_connected(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    svc = services()
    claimed = job(kind=kind)
    svc.queue.claim.return_value = claimed
    handler = AsyncMock()
    monkeypatch.setattr(module, f"process_{kind}", handler)
    assert asyncio.run(Worker(svc).run_once()) is True
    assert handler.await_count == 1
    assert handler.await_args.args[-1] is claimed
    svc.queue.complete.assert_awaited_once_with(claimed)


def test_empty_queue_returns_without_dispatch() -> None:
    svc = services()
    svc.queue.claim.return_value = None
    assert asyncio.run(Worker(svc).run_once()) is False
    svc.queue.complete.assert_not_awaited()


def test_transient_failure_retries_without_finalization() -> None:
    svc = services()
    claimed = job()
    svc.queue.claim.return_value = claimed
    worker = Worker(svc, handlers={claimed.kind: AsyncMock(side_effect=RuntimeError("sensitive payload"))})
    worker._record_dead = AsyncMock()
    asyncio.run(worker.run_once())
    svc.queue.fail.assert_awaited_once_with(claimed, "RuntimeError")
    worker._record_dead.assert_not_awaited()


def test_exhausted_job_records_terminal_status_before_dead_lettering() -> None:
    svc = services()
    claimed = job(attempts=2)
    svc.queue.claim.return_value = claimed
    calls = []
    worker = Worker(svc, handlers={claimed.kind: AsyncMock(side_effect=OSError())})
    worker._record_dead = AsyncMock(side_effect=lambda *_: calls.append("record"))
    svc.queue.fail.side_effect = lambda *_: calls.append("dead")
    asyncio.run(worker.run_once())
    assert calls == ["record", "dead"]


def test_finalizer_outage_keeps_job_recoverable() -> None:
    svc = services()
    claimed = job(attempts=3)
    svc.queue.claim.return_value = claimed
    handler = AsyncMock()
    worker = Worker(svc, handlers={claimed.kind: handler})
    worker._record_dead = AsyncMock(side_effect=RuntimeError("database down"))
    asyncio.run(worker.run_once())
    handler.assert_not_awaited()  # Recovery after a crash on the last attempt must not repeat engine work.
    svc.queue.release.assert_awaited_once_with(claimed, "Terminal bookkeeping unavailable")
    svc.queue.fail.assert_not_awaited()


@pytest.mark.parametrize("lose_lease", [False, True])
def test_lease_renewal_and_loss(lose_lease: bool) -> None:
    async def scenario():
        svc = services()
        claimed = job()
        svc.queue.claim.return_value = claimed
        svc.queue.renew.return_value = not lose_lease
        cancelled = asyncio.Event()

        async def handler(_job):
            try:
                await asyncio.sleep(0.4 if not lose_lease else 10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        await Worker(svc, handlers={claimed.kind: handler}).run_once()
        svc.queue.renew.assert_awaited_once_with(claimed)
        if lose_lease:
            assert cancelled.is_set()
            svc.queue.complete.assert_not_awaited()
            svc.queue.fail.assert_not_awaited()
        else:
            svc.queue.complete.assert_awaited_once_with(claimed)

    asyncio.run(scenario())


@pytest.mark.parametrize("finish", [True, False])
def test_shutdown_drains_or_leaves_interrupted_lease(monkeypatch: pytest.MonkeyPatch, finish: bool) -> None:
    async def scenario():
        svc = services()
        claimed = job()
        svc.queue.claim.side_effect = [claimed, None]
        started, allow_finish, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
        monkeypatch.setattr(module, "schedule_retention", AsyncMock())

        async def handler(_job):
            started.set()
            await allow_finish.wait()

        worker = Worker(svc, handlers={claimed.kind: handler})
        running = asyncio.create_task(worker.run(stop))
        await asyncio.wait_for(started.wait(), 1)
        stop.set()
        if finish:
            allow_finish.set()
        await asyncio.wait_for(running, 1)
        assert svc.queue.claim.await_count == 1
        if finish:
            svc.queue.complete.assert_awaited_once_with(claimed)
        else:
            svc.queue.complete.assert_not_awaited()
            svc.queue.fail.assert_not_awaited()

    asyncio.run(scenario())
