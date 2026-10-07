"""Persistent platform API: uvicorn backend.main:app --port 8000."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from azure.core.exceptions import AzureError
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.api.deps import Services
from backend.api.routes_auth import router as auth_router
from backend.api.routes_candidates import router as candidates_router
from backend.api.routes_drive import router as drive_router
from backend.api.routes_duplicates import router as duplicates_router
from backend.api.routes_hr import router as hr_router
from backend.api.routes_jobs import router as jobs_router
from backend.api.routes_portal import router as portal_router
from backend.api.routes_recruiting import router as recruiting_router
from backend.api.routes_talent_pool import router as talent_pool_router
from backend.config import PlatformSettings, get_platform_settings
from backend.db import NotFound
from backend.runtime import build_services, close_services, readiness, require_ready
from backend.services.common import ServiceError

logger = logging.getLogger("backend.api")


def create_app(settings: PlatformSettings | None = None, *, svc: Services | None = None) -> FastAPI:
    """Own production resources; injected services remain owned by their caller."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active = svc or await build_services(settings or get_platform_settings())
        try:
            await require_ready(active)
            app.state.services = active
            app.state.started = True
            yield
        finally:
            app.state.started = False
            if svc is None:
                await close_services(active)

    app = FastAPI(title="Resume screening platform", version="0.1.0", lifespan=lifespan)
    app.state.started = False
    app.include_router(auth_router)
    app.include_router(jobs_router)
    app.include_router(candidates_router)
    app.include_router(talent_pool_router)
    app.include_router(drive_router)
    app.include_router(recruiting_router)
    app.include_router(hr_router)
    app.include_router(duplicates_router)
    app.include_router(portal_router)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", tags=["health"])
    async def ready() -> JSONResponse:
        if not app.state.started:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        checks = await readiness(app.state.services)
        ok = all(value == "ok" for value in checks.values())
        return JSONResponse({"status": "ok" if ok else "unavailable", "checks": checks}, status_code=200 if ok else 503)

    @app.exception_handler(NotFound)
    async def not_found(_request: Request, _exc: NotFound) -> JSONResponse:
        return JSONResponse({"detail": "Resource not found"}, status_code=404)

    @app.exception_handler(ServiceError)
    async def service_error(_request: Request, exc: ServiceError) -> JSONResponse:
        content = {"detail": str(exc)}
        if exc.details is not None:
            content["details"] = exc.details
        return JSONResponse(jsonable_encoder(content), status_code=exc.status)

    @app.exception_handler(IntegrityError)
    async def conflict(_request: Request, _exc: IntegrityError) -> JSONResponse:
        return JSONResponse({"detail": "Request conflicts with existing data"}, status_code=409)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(_request: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.warning("database_request_failed error_type=%s", type(exc).__name__)
        return JSONResponse({"detail": "Database temporarily unavailable"}, status_code=503)

    @app.exception_handler(AzureError)
    async def storage_error(_request: Request, exc: AzureError) -> JSONResponse:
        logger.warning("storage_request_failed error_type=%s", type(exc).__name__)
        return JSONResponse({"detail": "Storage temporarily unavailable"}, status_code=503)

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        logger.error("request_failed error_type=%s", type(exc).__name__)
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    return app


app = create_app()
