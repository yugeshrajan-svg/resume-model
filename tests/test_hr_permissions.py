"""Offline checks of the HR permission model: every platform route declares exactly one permission."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from backend.main import create_app
from backend.security import PERMISSIONS

# Routes that authenticate by other means (registration, login, refresh) or are public probes.
PUBLIC = {"/v1/auth/register-company", "/v1/auth/login", "/v1/auth/refresh", "/health", "/ready"}


def _permission(route: APIRoute) -> list[str]:
    found = []
    for dependency in route.dependant.dependencies:
        name = getattr(dependency.call, "__name__", "")
        if name.startswith("require_"):
            found.append(name.removeprefix("require_").replace("_", ":", 1))
    return found


def _expand(routes: list) -> list[APIRoute]:
    out: list[APIRoute] = []
    for r in routes:
        if isinstance(r, APIRoute):
            out.append(r)
        elif hasattr(r, "original_router"):  # FastAPI >= 0.13x keeps included routers wrapped
            out.extend(_expand(r.original_router.routes))
    return out


def _routes() -> list[APIRoute]:
    return _expand(create_app().routes)


def test_every_platform_route_declares_exactly_one_permission() -> None:
    routes = [r for r in _routes() if r.path.startswith("/v1") and r.path not in PUBLIC]
    assert len(routes) >= 30, "route discovery failed; the check would pass vacuously"
    for route in routes:
        permissions = _permission(route)
        assert len(permissions) == 1, (sorted(route.methods), route.path, permissions)
        assert permissions[0] in PERMISSIONS, (route.path, permissions)


@pytest.mark.parametrize(
    ("method", "path", "permission"),
    [
        ("POST", "/v1/applications/{application_id}/decision", "decisions:write"),
        ("GET", "/v1/applications/{application_id}/decisions", "candidates:read"),
        ("POST", "/v1/candidates/compare", "candidates:read"),
        ("GET", "/v1/candidates/{candidate_id}/consents", "candidates:read"),
        ("POST", "/v1/candidates/{candidate_id}/consents", "consents:write"),
        ("POST", "/v1/candidates/{candidate_id}/consents/revoke", "consents:write"),
        ("GET", "/v1/candidates/{candidate_id}/export", "candidates:export"),
        ("DELETE", "/v1/candidates/{candidate_id}", "candidates:delete"),
        ("GET", "/v1/candidates", "contacts:read"),
        ("GET", "/v1/company", "company:manage"),
        ("PATCH", "/v1/company", "company:manage"),
        ("GET", "/v1/usage", "usage:read"),
    ],
)
def test_hr_endpoint_permissions(method: str, path: str, permission: str) -> None:
    [route] = [r for r in _routes() if r.path == path and method in r.methods]
    assert _permission(route) == [permission]


def test_role_matrix() -> None:
    """Contact details, privacy operations and company settings are never available to hiring managers."""
    assert PERMISSIONS["contacts:read"] == {"admin", "recruiter"}
    assert PERMISSIONS["consents:write"] == {"admin", "recruiter"}
    assert PERMISSIONS["decisions:write"] == {"admin", "recruiter", "hiring_manager"}
    assert PERMISSIONS["candidates:read"] == {"admin", "recruiter", "hiring_manager"}
    for admin_only in ("candidates:delete", "candidates:export", "company:manage", "usage:read", "users:manage"):
        assert PERMISSIONS[admin_only] == {"admin"}, admin_only
