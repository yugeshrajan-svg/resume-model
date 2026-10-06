"""Durable Postgres worker: python -m backend.worker [--once]."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from backend.api.deps import Services
from backend.config import get_platform_settings
from backend.db import NotFound
from backend.models import Company
from backend.queue import ClaimedJob
from backend.runtime import build_services, close_services, require_ready
from backend.services.pool_matching import process_match_pool, process_pool_check
from backend.services.privacy import process_delete_candidate, process_retention_sweep
from backend.services.resumes import process_extract_resume, record_dead_extract_job
from backend.services.screening import process_screen_application, record_dead_screen_job
from backend.services.talent_pool import process_embed_resume

logger = logging.getLogger("backend.worker")
Handler = Callable[[ClaimedJob], Awaitable[None]]


async def schedule_retention(svc: Services, *, now: datetime | None = None) -> None:
    """Enqueue today's due sweep (or yesterday's before the UTC hour), once per company/date."""
    now = now or datetime.now(UTC)
    due = now.date() if now.hour >= svc.settings.retention_sweep_hour_utc else (now - timedelta(days=1)).date()
    async with svc.db.system_session() as session:
        company_ids = (await session.execute(select(Company.id))).scalars().all()
        for company_id in company_ids:
            await svc.queue.enqueue(
                session,
                company_id=company_id,
                kind="retention_sweep",
                payload={},
                key=f"retention_sweep:{company_id}:{due.isoformat()}",
            )


class Worker:
    def __init__(self, svc: Services, *, handlers: dict[str, Handler] | None = None) -> None:
        self.svc = svc
        self.worker_id = f"worker-{uuid.uuid4().hex}"
        self.handlers = handlers or {
            "extract_resume": self._extract,
            "screen_application": self._screen,
            "delete_candidate": self._delete,
            "retention_sweep": self._retention,
            "embed_resume": self._embed,
            "match_pool": self._match_pool,
            "pool_check": self._pool_check,
        }

    async def _extract(self, job: ClaimedJob) -> None:
        await process_extract_resume(self.svc.db, self.svc.storage, self.svc.settings, job, queue=self.svc.queue)

    async def _embed(self, job: ClaimedJob) -> None:
        await process_embed_resume(self.svc.db, job)

    async def _match_pool(self, job: ClaimedJob) -> None:
        await process_match_pool(self.svc.db, self.svc.queue, job)

    async def _pool_check(self, job: ClaimedJob) -> None:
        await process_pool_check(self.svc.db, job)

    async def _screen(self, job: ClaimedJob) -> None:
        await process_screen_application(self.svc.db, job)

    async def _delete(self, job: ClaimedJob) -> None:
        await process_delete_candidate(
            self.svc.db, self.svc.storage, job, tombstone_days=self.svc.settings.erasure_tombstone_days
        )

    async def _retention(self, job: ClaimedJob) -> None:
        await process_retention_sweep(self.svc.db, self.svc.queue, job)

    async def _record_dead(self, job: ClaimedJob, error: str) -> None:
        if job.kind == "screen_application":
            await record_dead_screen_job(self.svc.db, job, error)
        elif job.kind == "extract_resume":
            await record_dead_extract_job(self.svc.db, job, error)

    async def _execute(self, job: ClaimedJob) -> None:
        try:
            if job.max_attempts < 1:
                raise ValueError("This runner requires a queue with bounded attempts")
            if job.attempts > job.max_attempts:
                # A worker crashed on its last attempt. Recover terminal bookkeeping without more engine work.
                raise RuntimeError("Retry limit exhausted after worker interruption")
            await self.handlers[job.kind](job)
        except NotFound:
            # Privacy deletion can remove an object's pending jobs while they are being processed.
            await self.svc.queue.complete(job)
        except Exception as exc:
            # Persist/log only the exception type: SDK exception messages may contain URLs, secrets or resume text.
            error = type(exc).__name__
            logger.warning("job_failed id=%s kind=%s attempt=%s error_type=%s", job.id, job.kind, job.attempts, error)
            if job.attempts >= job.max_attempts:
                try:
                    await self._record_dead(job, error)
                except Exception as finalizer_error:
                    logger.warning("job_finalizer_failed id=%s error_type=%s", job.id, type(finalizer_error).__name__)
                    await self.svc.queue.release(job, "Terminal bookkeeping unavailable")
                    return
            await self.svc.queue.fail(job, error)
        else:
            if await self.svc.queue.complete(job):
                logger.info("job_processed id=%s kind=%s", job.id, job.kind)
            else:
                logger.warning("job_acknowledgement_rejected id=%s", job.id)

    async def _renew(self, job: ClaimedJob) -> None:
        while True:
            await asyncio.sleep(self.svc.settings.queue_lock_timeout_s / 3)
            if not await self.svc.queue.renew(job):
                return

    async def _process(self, job: ClaimedJob) -> None:
        work = asyncio.create_task(self._execute(job))
        lease = asyncio.create_task(self._renew(job))
        try:
            await asyncio.wait((work, lease), return_when=asyncio.FIRST_COMPLETED)
            if work.done():
                await work
            else:
                logger.warning("job_lease_lost id=%s", job.id)
        finally:
            for task in (work, lease):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, lease, return_exceptions=True)

    async def run_once(self, slot: int = 0) -> bool:
        job = await self.svc.queue.claim(f"{self.worker_id}:{slot}")
        if job is None:
            return False
        await self._process(job)
        return True

    @staticmethod
    async def _pause(stop: asyncio.Event, seconds: float) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)

    async def _loop(self, stop: asyncio.Event, slot: int) -> None:
        while not stop.is_set():
            try:
                processed = await self.run_once(slot)
            except Exception as exc:
                logger.warning("worker_dependency_failed error_type=%s", type(exc).__name__)
                processed = False
            if not processed:
                await self._pause(stop, self.svc.settings.worker_poll_interval_s)

    async def _scheduler(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await schedule_retention(self.svc)
            except Exception as exc:
                logger.warning("retention_schedule_failed error_type=%s", type(exc).__name__)
            await self._pause(stop, 60)

    async def run(self, stop: asyncio.Event) -> None:
        tasks = [asyncio.create_task(self._loop(stop, slot)) for slot in range(self.svc.settings.worker_concurrency)]
        tasks.append(asyncio.create_task(self._scheduler(stop)))
        try:
            await stop.wait()
        finally:
            stop.set()  # Stop claims; allow jobs and their heartbeats to finish before closing resources.
            _, pending = await asyncio.wait(tasks, timeout=self.svc.settings.worker_shutdown_timeout_s)
            for task in pending:
                task.cancel()  # Leave leases running so another process can reclaim interrupted work.
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info("worker_stopped interrupted_tasks=%s", len(pending))


async def _main(once: bool) -> None:
    svc = await build_services(get_platform_settings())
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    previous = {}
    try:
        await require_ready(svc)
        worker = Worker(svc)
        if once:
            await worker.run_once()
            return
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            # signal.signal also works on Windows, where loop.add_signal_handler is unavailable.
            signal.signal(signum, lambda *_args: loop.call_soon_threadsafe(stop.set))
        await worker.run(stop)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        await close_services(svc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process at most one job and exit (no retention scheduler)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with suppress(KeyboardInterrupt):
        asyncio.run(_main(args.once))


if __name__ == "__main__":
    main()
