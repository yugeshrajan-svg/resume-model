"""Shared API/worker resource lifecycle and dependency readiness checks."""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import text

from backend.api.deps import Services
from backend.config import PlatformSettings
from backend.db import Database
from backend.queue import PostgresQueue
from backend.storage import build_storage

logger = logging.getLogger("backend.runtime")


async def build_services(settings: PlatformSettings) -> Services:
    db = Database.from_settings(settings)
    try:
        storage = await asyncio.to_thread(build_storage, settings)
    except BaseException:
        await db.dispose()
        raise
    return Services(settings, db, storage, PostgresQueue(db, settings))


async def close_services(svc: Services) -> None:
    try:
        await svc.storage.close()
    finally:
        await svc.db.dispose()


async def _database_health(db: Database) -> None:
    # Probe both credentials and the migrated queue table, not just the server's TCP port.
    for context, bypass in ((db.system_session(), True), (db.tenant_session(uuid.uuid4()), False)):
        async with context as session:
            superuser, bypassrls = (
                await session.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))
            ).one()
            if superuser or bypassrls != bypass:
                raise RuntimeError("Database credentials do not match the system/app role separation")
            await session.execute(text("SELECT id FROM queue_jobs LIMIT 0"))
            await session.execute(text("SELECT id FROM recruiting_activities LIMIT 0"))
            await session.execute(text("SELECT id FROM hiring_plans LIMIT 0"))
            await session.execute(text("SELECT id FROM hr_records LIMIT 0"))
            await session.execute(text("SELECT id FROM candidate_aliases LIMIT 0"))
            await session.execute(
                text(
                    "SELECT r.processing_generation, r.processed_generation, s.snapshot, "
                    "s.reference_date, s.retry_of_run_id, i.extraction, g.blob_names, d.previous_stage, "
                    "d.new_stage, t.contact_hash, e.vector, c.scope, m.eligible "
                    "FROM resumes r, screening_runs s, screening_inputs i, resume_registrations g, decisions d, "
                    "erasure_tombstones t, candidate_embeddings e, consents c, pool_matches m LIMIT 0"
                )
            )


async def readiness(svc: Services) -> dict[str, str]:
    async def check(name: str, probe) -> tuple[str, str]:
        try:
            async with asyncio.timeout(6):
                await probe
        except Exception as exc:
            logger.warning("dependency_unavailable dependency=%s error_type=%s", name, type(exc).__name__)
            return name, "unavailable"
        return name, "ok"

    results = await asyncio.gather(check("database", _database_health(svc.db)), check("storage", svc.storage.health()))
    return dict(results)


async def require_ready(svc: Services) -> None:
    checks = await readiness(svc)
    if any(value != "ok" for value in checks.values()):
        raise RuntimeError("Backend dependencies unavailable; start PostgreSQL/storage and run alembic upgrade head")
