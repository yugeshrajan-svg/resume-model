from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import jwt
import pytest
from azure.core.exceptions import AzureError
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError

import backend.main as main
from backend.api.deps import Services
from backend.config import PlatformSettings
from backend.db import NotFound
from backend.runtime import close_services, readiness
from backend.services.common import ServiceError
from backend.storage import InMemoryBlobStorage


@pytest.fixture
def svc() -> Services:
    return Services(
        PlatformSettings(_env_file=None, APP_ENV="test", JWT_SECRET="test-jwt-key-" * 4),
        AsyncMock(),
        InMemoryBlobStorage(),
        AsyncMock(),
    )


def test_app_lifecycle_and_readiness(monkeypatch: pytest.MonkeyPatch, svc: Services) -> None:
    build = AsyncMock(return_value=svc)
    close = AsyncMock()
    checks = AsyncMock(return_value={"database": "ok", "storage": "ok"})
    monkeypatch.setattr(main, "build_services", build)
    monkeypatch.setattr(main, "close_services", close)
    monkeypatch.setattr(main, "require_ready", AsyncMock())
    monkeypatch.setattr(main, "readiness", checks)
    app = main.create_app(svc.settings)
    assert TestClient(app).get("/ready").status_code == 503
    with TestClient(app) as client:
        assert app.state.services is svc
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/ready").status_code == 200
        checks.return_value = {"database": "unavailable", "storage": "ok"}
        assert client.get("/ready").status_code == 503
        assert client.get("/health").status_code == 200
        assert client.get("/v1/jobs").status_code == 401
        schema = client.get("/openapi.json").json()
        assert "/v1/auth/login" in schema["paths"]
        assert "/v1/jobs/{job_id}/screening-runs" in schema["paths"]
        assert "/v1/candidates/{candidate_id}" in schema["paths"]
    assert app.state.started is False
    build.assert_awaited_once_with(svc.settings)
    close.assert_awaited_once_with(svc)


def test_failed_startup_closes_owned_resources(monkeypatch: pytest.MonkeyPatch, svc: Services) -> None:
    close = AsyncMock()
    monkeypatch.setattr(main, "build_services", AsyncMock(return_value=svc))
    monkeypatch.setattr(main, "close_services", close)
    monkeypatch.setattr(main, "require_ready", AsyncMock(side_effect=RuntimeError("not ready")))
    with pytest.raises(RuntimeError, match="not ready"), TestClient(main.create_app()):
        pass
    close.assert_awaited_once_with(svc)


@pytest.mark.parametrize(
    ("error", "code", "body"),
    [
        (NotFound("private identifier"), 404, {"detail": "Resource not found"}),
        (
            ServiceError("No applications", details={"blocked": []}),
            409,
            {"detail": "No applications", "details": {"blocked": []}},
        ),
        (
            IntegrityError("secret query", {}, Exception("private")),
            409,
            {"detail": "Request conflicts with existing data"},
        ),
        (
            OperationalError("secret query", {}, Exception("private")),
            503,
            {"detail": "Database temporarily unavailable"},
        ),
        (AzureError("private credential"), 503, {"detail": "Storage temporarily unavailable"}),
        (RuntimeError("private payload"), 500, {"detail": "Internal server error"}),
    ],
)
def test_api_error_mapping(
    monkeypatch: pytest.MonkeyPatch, svc: Services, error: Exception, code: int, body: dict
) -> None:
    monkeypatch.setattr(main, "require_ready", AsyncMock())
    app = main.create_app(svc=svc)

    @app.get("/test-error")
    async def raise_error():
        raise error

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test-error")
        assert response.status_code == code
        assert response.json() == body
    svc.db.dispose.assert_not_awaited()  # Injected resources belong to the caller.


@pytest.mark.parametrize("bad_claims", [{"sub": "invalid-uuid"}, {"cid": None}, {"role": []}])
def test_malformed_signed_claims_return_401(monkeypatch: pytest.MonkeyPatch, svc: Services, bad_claims: dict) -> None:
    monkeypatch.setattr(main, "require_ready", AsyncMock())
    claims = {
        "sub": str(uuid.uuid4()),
        "cid": str(uuid.uuid4()),
        "role": "admin",
        "typ": "access",
        "iss": svc.settings.jwt_issuer,
        "aud": svc.settings.jwt_audience,
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        **bad_claims,
    }
    token = jwt.encode(claims, svc.settings.jwt_key(), algorithm="HS256")
    with TestClient(main.create_app(svc=svc)) as client:
        response = client.get("/v1/jobs", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


def test_readiness_catches_storage_failure_without_exposing_secrets(monkeypatch: pytest.MonkeyPatch, svc: Services):
    import backend.runtime as runtime

    monkeypatch.setattr(runtime, "_database_health", AsyncMock())
    svc.storage.health = AsyncMock(side_effect=RuntimeError("secret storage credential"))
    assert asyncio.run(readiness(svc)) == {"database": "ok", "storage": "unavailable"}


def test_close_disposes_database_even_if_storage_close_fails(svc: Services) -> None:
    svc.storage.close = AsyncMock(side_effect=RuntimeError("storage close"))
    with pytest.raises(RuntimeError, match="storage close"):
        asyncio.run(close_services(svc))
    svc.db.dispose.assert_awaited_once()
