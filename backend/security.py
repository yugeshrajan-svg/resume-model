"""Passwords (argon2), JWT access tokens, opaque rotating refresh tokens, and role permissions."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from backend.config import PlatformSettings

Role = Literal["admin", "recruiter", "hiring_manager"]
_hasher = PasswordHasher()

# Permission -> roles allowed. Every endpoint declares exactly one permission.
PERMISSIONS: dict[str, frozenset[str]] = {
    "hr:access": frozenset({"admin", "recruiter", "hiring_manager"}),
    "hr:manage": frozenset({"admin"}),
    "users:manage": frozenset({"admin"}),
    "company:manage": frozenset({"admin"}),
    "usage:read": frozenset({"admin"}),
    "jobs:read": frozenset({"admin", "recruiter", "hiring_manager"}),
    "jobs:write": frozenset({"admin", "recruiter"}),
    "recruiting:manage": frozenset({"admin", "recruiter"}),
    "resumes:write": frozenset({"admin", "recruiter"}),
    "screening:run": frozenset({"admin", "recruiter"}),
    "candidates:read": frozenset({"admin", "recruiter", "hiring_manager"}),
    "contacts:read": frozenset({"admin", "recruiter"}),
    "consents:write": frozenset({"admin", "recruiter"}),
    "decisions:write": frozenset({"admin", "recruiter", "hiring_manager"}),
    "candidates:delete": frozenset({"admin"}),
    "candidates:export": frozenset({"admin"}),
}


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    company_id: uuid.UUID
    role: Role

    def can(self, permission: str) -> bool:
        return self.role in PERMISSIONS[permission]


class AuthError(Exception):
    """Invalid, expired or wrong-type token."""


def issue_access_token(principal: Principal, settings: PlatformSettings) -> tuple[str, int]:
    now = datetime.now(UTC)
    claims = {
        "sub": str(principal.user_id),
        "cid": str(principal.company_id),
        "role": principal.role,
        "typ": "access",
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + timedelta(seconds=settings.access_token_ttl_s),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, settings.jwt_key(), algorithm="HS256"), settings.access_token_ttl_s


def decode_access_token(token: str, settings: PlatformSettings) -> Principal:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_key(),
            algorithms=["HS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "cid", "role", "typ"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthError(str(exc)) from exc
    try:
        if claims["typ"] != "access" or claims["role"] not in PERMISSIONS["hr:access"]:
            raise AuthError("not an access token")
        return Principal(uuid.UUID(claims["sub"]), uuid.UUID(claims["cid"]), claims["role"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise AuthError("invalid access token claims") from exc


def new_refresh_token() -> tuple[str, str]:
    """(opaque token for the client, sha256 hex stored in the database)."""
    token = secrets.token_urlsafe(48)
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ExternalIdentityProvider(Protocol):
    """OIDC hook (e.g. Microsoft Entra ID): validate an external ID token and map it to a Principal.

    Not implemented yet. An implementation should verify the token signature against OIDC_JWKS_URL, check
    OIDC_ISSUER / OIDC_AUDIENCE, then look up the user by email or object id (system session) and return the
    Principal; /auth/login would then accept that token in place of a password.
    """

    async def principal_from_id_token(self, id_token: str) -> Principal: ...
