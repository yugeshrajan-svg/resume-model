"""Shared service helpers: audit logging and usage metering."""

from __future__ import annotations

import uuid
from typing import Any

from backend.db import TenantRepo
from backend.models import AuditLog, UsageEvent
from backend.security import Principal


class ServiceError(Exception):
    """A request that cannot be fulfilled as asked (mapped to 409/422 by the API)."""

    def __init__(self, message: str, *, status: int = 409, details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.details = details


def audit(
    repo: TenantRepo,
    actor: Principal | str,
    action: str,
    object_type: str,
    object_id: uuid.UUID | str | None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """Append an audit entry. Callers must never pass contact details (name/email/phone) in before/after."""
    repo.add(
        AuditLog(
            actor_id=actor.user_id if isinstance(actor, Principal) else None,
            actor=actor.role if isinstance(actor, Principal) else actor,
            action=action,
            object_type=object_type,
            object_id=str(object_id) if object_id is not None else None,
            before=before,
            after=after,
        )
    )


def record_usage(
    repo: TenantRepo,
    kind: str,
    *,
    run_id: uuid.UUID | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    ocr_pages: int = 0,
    cost_usd: float = 0.0,
) -> None:
    if not (prompt_tokens or completion_tokens or ocr_pages or cost_usd):
        return
    repo.add(
        UsageEvent(
            run_id=run_id,
            kind=kind,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            ocr_pages=ocr_pages,
            cost_usd=cost_usd,
        )
    )
