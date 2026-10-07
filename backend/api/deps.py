"""Request-scoped dependencies: services, authentication, permissions, tenant transactions."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.config import PlatformSettings
from backend.db import Database, TenantRepo
from backend.queue import Queue
from backend.security import AuthError, Principal, decode_access_token
from backend.storage import BlobStorage

_bearer = HTTPBearer(auto_error=False)


@dataclass
class Services:
    settings: PlatformSettings
    db: Database
    storage: BlobStorage
    queue: Queue

    @asynccontextmanager
    async def tenant(self, principal: Principal) -> AsyncIterator[TenantRepo]:
        """One transaction scoped to the caller's company (repository filter + RLS)."""
        async with self.db.tenant_session(principal.company_id) as session:
            yield TenantRepo(session, principal.company_id)


def services(request: Request) -> Services:
    return request.app.state.services


async def current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Missing bearer token", headers={"WWW-Authenticate": "Bearer"}
        )
    try:
        return decode_access_token(credentials.credentials, services(request).settings)
    except AuthError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"}
        ) from exc


def require(permission: str) -> Callable[..., Principal]:
    """Dependency: the authenticated principal, if their role grants ``permission``; otherwise 403."""

    async def dependency(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if not principal.can(permission):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Role {principal.role!r} lacks {permission!r}")
        return principal

    dependency.__name__ = f"require_{permission.replace(':', '_')}"
    return dependency
