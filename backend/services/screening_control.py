"""Stop a screening run while retaining its completed results and immutable inputs."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import update

from backend.db import TenantRepo
from backend.models import QueueJob, ScreeningRun
from backend.security import Principal
from backend.services.common import audit


async def stop_run(repo: TenantRepo, principal: Principal, run_id: uuid.UUID) -> ScreeningRun:
    run = await repo.get(ScreeningRun, run_id, for_update=True)
    if run.status not in {"queued", "running"}:
        return run  # Repeated clicks and a completion racing Stop are harmless.
    previous = run.status
    run.status = "stopped"
    run.finished_at = datetime.now(UTC)
    await repo.session.execute(
        update(QueueJob)
        .where(
            QueueJob.company_id == repo.company_id,
            QueueJob.kind == "screen_application",
            QueueJob.payload["run_id"].as_string() == str(run_id),
            QueueJob.status == "queued",
        )
        .values(status="cancelled", updated_at=run.finished_at)
    )
    audit(
        repo,
        principal,
        "screening_run.stopped",
        "screening_run",
        run_id,
        before={"status": previous},
        after={"status": "stopped", "done": run.done, "total": run.total},
    )
    return run
