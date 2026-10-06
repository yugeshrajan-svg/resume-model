"""Job queue: Postgres-backed (SELECT ... FOR UPDATE SKIP LOCKED) behind a small interface.

* Enqueue happens inside the caller's transaction (outbox pattern): a job exists iff the request committed.
* Every job has an idempotency key (kind + content hash/ids + version); enqueueing the same key twice is a no-op,
  and handlers are written to be safe to re-run.
* Failures retry with exponential backoff; after ``max_attempts`` the job moves to the ``dead`` state.
* A job whose worker died (lock older than QUEUE_LOCK_TIMEOUT_S) is reclaimed.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import PlatformSettings
from backend.db import Database
from backend.models import QueueJob

# Lower runs first. Uploads, screening and privacy deletions are interactive; talent-pool indexing and matching
# can create thousands of jobs and must never delay them.
PRIORITY = {
    "extract_resume": 0,
    "screen_application": 0,
    "delete_candidate": 0,
    "retention_sweep": 1,
    "embed_resume": 2,
    "match_pool": 2,
    "pool_check": 2,
}
JOB_KINDS = (
    "extract_resume",
    "screen_application",
    "delete_candidate",
    "retention_sweep",
    "embed_resume",
    "match_pool",
    "pool_check",
)


@dataclass(frozen=True)
class ClaimedJob:
    id: uuid.UUID
    company_id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


class Queue(Protocol):
    async def enqueue(
        self, session: AsyncSession, *, company_id: uuid.UUID, kind: str, payload: dict[str, Any], key: str
    ) -> None: ...

    async def claim(self, worker_id: str) -> ClaimedJob | None: ...

    async def complete(self, job: ClaimedJob) -> bool: ...

    async def fail(self, job: ClaimedJob, error: str) -> bool: ...

    async def renew(self, job: ClaimedJob) -> bool: ...

    async def release(self, job: ClaimedJob, error: str) -> bool: ...


class PostgresQueue:
    def __init__(self, db: Database, settings: PlatformSettings) -> None:
        self.db = db
        self.settings = settings

    async def enqueue(
        self, session: AsyncSession, *, company_id: uuid.UUID, kind: str, payload: dict[str, Any], key: str
    ) -> None:
        if kind not in JOB_KINDS:
            raise ValueError(f"unknown job kind {kind!r}")
        stmt = (
            insert(QueueJob)
            .values(
                id=uuid.uuid4(),
                company_id=company_id,
                kind=kind,
                priority=PRIORITY[kind],
                idempotency_key=key,
                payload=payload,
                max_attempts=self.settings.queue_max_attempts,
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
        )
        await session.execute(stmt)

    async def claim(self, worker_id: str) -> ClaimedJob | None:
        async with self.db.system_session() as s:
            row = (
                await s.execute(
                    text(
                        """
                        UPDATE queue_jobs SET status = 'running', locked_at = now(), locked_by = :worker,
                               attempts = attempts + 1, updated_at = now()
                        WHERE id = (
                            SELECT id FROM queue_jobs
                            WHERE (status = 'queued' AND run_after <= now())
                               OR (status = 'running' AND locked_at < now() - make_interval(secs => :lock_timeout))
                            ORDER BY priority, run_after, created_at
                            FOR UPDATE SKIP LOCKED
                            LIMIT 1)
                        RETURNING id, company_id, kind, payload, attempts, max_attempts
                        """
                    ),
                    {"worker": worker_id, "lock_timeout": self.settings.queue_lock_timeout_s},
                )
            ).first()
        if row is None:
            return None
        payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
        return ClaimedJob(row.id, row.company_id, row.kind, payload, row.attempts, row.max_attempts)

    async def complete(self, job: ClaimedJob) -> bool:
        async with self.db.system_session() as s:
            return await self._settle(s, job, "done", None, 0)

    async def fail(self, job: ClaimedJob, error: str) -> bool:
        dead = job.attempts >= job.max_attempts
        delay = self.settings.queue_backoff_base_s * (2 ** min(job.attempts - 1, 16))
        async with self.db.system_session() as s:
            return await self._settle(s, job, "dead" if dead else "queued", error, delay)

    async def release(self, job: ClaimedJob, error: str) -> bool:
        """Retry terminal bookkeeping when dependencies fail, without rerunning an exhausted handler."""
        async with self.db.system_session() as s:
            return await self._settle(s, job, "queued", error, self.settings.queue_backoff_base_s)

    async def renew(self, job: ClaimedJob) -> bool:
        async with self.db.system_session() as s:
            row = (
                await s.execute(
                    text("""UPDATE queue_jobs SET locked_at = now(), updated_at = now()
                        WHERE id = :id AND status = 'running' AND attempts = :attempts RETURNING id"""),
                    {"id": job.id, "attempts": job.attempts},
                )
            ).first()
        return row is not None

    @staticmethod
    async def _settle(session: AsyncSession, job: ClaimedJob, status: str, error: str | None, delay: float) -> bool:
        # The attempt counter is the lease generation. A reclaimed job cannot be acknowledged by its old worker.
        row = (
            await session.execute(
                text(
                    """
                    UPDATE queue_jobs SET status = :status, last_error = :error,
                           locked_at = NULL, locked_by = NULL, updated_at = now(),
                           run_after = now() + make_interval(secs => :delay)
                    WHERE id = :id AND status = 'running' AND attempts = :attempts RETURNING id
                    """
                ),
                {
                    "id": job.id,
                    "attempts": job.attempts,
                    "status": status,
                    "error": error[:2000] if error else None,
                    "delay": delay,
                },
            )
        ).first()
        return row is not None


class ServiceBusQueue:
    """Azure Service Bus adapter (optional; not used by default and not exercised by the test suite).

    Uses the message id as the idempotency key, so the queue must have duplicate detection enabled. Unlike the
    Postgres queue, enqueue is not part of the caller's database transaction, and retries/dead-lettering follow
    the queue's MaxDeliveryCount instead of QUEUE_MAX_ATTEMPTS.
    """

    def __init__(self, connection_string: str, queue_name: str, max_wait_s: float = 5.0) -> None:
        from azure.servicebus import ServiceBusClient

        self._client = ServiceBusClient.from_connection_string(connection_string)
        self._queue_name = queue_name
        self._max_wait_s = max_wait_s
        self._receiver = self._client.get_queue_receiver(queue_name)
        self._messages: dict[uuid.UUID, Any] = {}

    async def enqueue(
        self, session: AsyncSession, *, company_id: uuid.UUID, kind: str, payload: dict[str, Any], key: str
    ) -> None:
        import asyncio

        from azure.servicebus import ServiceBusMessage

        body = json.dumps({"company_id": str(company_id), "kind": kind, "payload": payload})
        message = ServiceBusMessage(body, message_id=key[:128])

        def send() -> None:
            with self._client.get_queue_sender(self._queue_name) as sender:
                sender.send_messages(message)

        await asyncio.to_thread(send)

    async def claim(self, worker_id: str) -> ClaimedJob | None:
        import asyncio

        messages = await asyncio.to_thread(
            self._receiver.receive_messages, max_message_count=1, max_wait_time=self._max_wait_s
        )
        if not messages:
            return None
        message = messages[0]
        data = json.loads(str(message))
        job = ClaimedJob(
            id=uuid.uuid4(),
            company_id=uuid.UUID(data["company_id"]),
            kind=data["kind"],
            payload=data["payload"],
            attempts=message.delivery_count or 1,
            max_attempts=0,  # governed by the queue's MaxDeliveryCount
        )
        self._messages[job.id] = message
        return job

    async def complete(self, job: ClaimedJob) -> bool:
        import asyncio

        await asyncio.to_thread(self._receiver.complete_message, self._messages.pop(job.id))
        return True

    async def fail(self, job: ClaimedJob, error: str) -> bool:
        import asyncio

        await asyncio.to_thread(self._receiver.abandon_message, self._messages.pop(job.id))
        return True

    async def release(self, job: ClaimedJob, error: str) -> bool:
        return await self.fail(job, error)

    async def renew(self, job: ClaimedJob) -> bool:
        import asyncio

        await asyncio.to_thread(self._receiver.renew_message_lock, self._messages[job.id])
        return True
