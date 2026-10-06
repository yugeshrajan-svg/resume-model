"""Platform settings (environment variables). The engine keeps its own settings in engine/config.py."""

from __future__ import annotations

import logging
import secrets
from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qs, urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("backend.config")

_TLS_VALUES = {"require", "verify-ca", "verify-full", "true"}


def url_requires_tls(url: str) -> bool:
    """True if a SQLAlchemy/asyncpg URL demands TLS (``ssl=require`` or ``sslmode=verify-full`` etc.)."""
    query = parse_qs(urlparse(url).query)
    values = [v.lower() for key in ("ssl", "sslmode") for v in query.get(key, [])]
    return any(v in _TLS_VALUES for v in values)


class PlatformSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["dev", "test", "prod"] = Field(default="dev", validation_alias="APP_ENV")

    # Two roles: the app role is always subject to row-level security; the system role (BYPASSRLS) is used only
    # for cross-tenant work: login lookup, company registration, queue claims and retention sweeps.
    database_url: str = Field(
        default="postgresql+asyncpg://screening_app:app@127.0.0.1:55432/screening", validation_alias="DATABASE_URL"
    )
    database_system_url: str = Field(
        default="postgresql+asyncpg://screening_system:system@127.0.0.1:55432/screening",
        validation_alias="DATABASE_SYSTEM_URL",
    )
    db_pool_size: int = Field(default=10, ge=0, validation_alias="DB_POOL_SIZE")  # 0 => no pooling (tests)

    # Auth
    jwt_secret: SecretStr | None = Field(default=None, validation_alias="JWT_SECRET")
    jwt_issuer: str = Field(default="resume-screening", validation_alias="JWT_ISSUER")
    jwt_audience: str = Field(default="resume-screening-api", validation_alias="JWT_AUDIENCE")
    access_token_ttl_s: int = Field(default=900, gt=0, validation_alias="ACCESS_TOKEN_TTL_S")
    refresh_token_ttl_s: int = Field(default=14 * 86400, gt=0, validation_alias="REFRESH_TOKEN_TTL_S")
    # OIDC hook (e.g. Entra ID): accepted issuer/audience/JWKS for external tokens once implemented.
    oidc_issuer: str | None = Field(default=None, validation_alias="OIDC_ISSUER")
    oidc_audience: str | None = Field(default=None, validation_alias="OIDC_AUDIENCE")
    oidc_jwks_url: str | None = Field(default=None, validation_alias="OIDC_JWKS_URL")

    # Keyed hash for contact deduplication (normalized email / phone are never stored unhashed for matching).
    pii_hash_key: SecretStr | None = Field(default=None, validation_alias="PII_HASH_KEY")

    # Blob storage (Azurite locally)
    blob_connection_string: SecretStr | None = Field(default=None, validation_alias="BLOB_CONNECTION_STRING")
    blob_container: str = Field(default="resumes", validation_alias="BLOB_CONTAINER")
    sas_ttl_s: int = Field(default=600, gt=0, le=3600, validation_alias="SAS_TTL_S")

    # Queue / worker
    queue_max_attempts: int = Field(default=5, ge=1, validation_alias="QUEUE_MAX_ATTEMPTS")
    queue_backoff_base_s: float = Field(default=5.0, ge=0, validation_alias="QUEUE_BACKOFF_BASE_S")
    queue_lock_timeout_s: int = Field(default=900, gt=0, validation_alias="QUEUE_LOCK_TIMEOUT_S")
    worker_poll_interval_s: float = Field(default=1.0, gt=0, validation_alias="WORKER_POLL_INTERVAL_S")
    worker_concurrency: int = Field(default=4, ge=1, le=64, validation_alias="WORKER_CONCURRENCY")
    worker_shutdown_timeout_s: float = Field(default=30.0, gt=0, le=300, validation_alias="WORKER_SHUTDOWN_TIMEOUT_S")
    retention_sweep_hour_utc: int = Field(default=2, ge=0, le=23, validation_alias="RETENTION_SWEEP_HOUR_UTC")
    # How long erasure tombstones (keyed contact hashes) block ingestion of uploads registered before an erasure.
    # Must exceed the longest time a queued ingestion job can wait (retries + backoff).
    erasure_tombstone_days: int = Field(default=30, ge=1, le=365, validation_alias="ERASURE_TOMBSTONE_DAYS")

    # Limits
    max_bulk_register: int = Field(default=500, ge=1, validation_alias="MAX_BULK_REGISTER")
    max_page_size: int = Field(default=200, ge=1, validation_alias="MAX_PAGE_SIZE")

    @model_validator(mode="after")
    def _prod_requirements(self) -> PlatformSettings:
        if self.app_env == "prod":
            problems = []
            if self.jwt_secret is None or len(self.jwt_secret.get_secret_value()) < 32:
                problems.append("JWT_SECRET (>= 32 chars)")
            if self.pii_hash_key is None or len(self.pii_hash_key.get_secret_value()) < 32:
                problems.append("PII_HASH_KEY (>= 32 chars)")
            for name, url in (("DATABASE_URL", self.database_url), ("DATABASE_SYSTEM_URL", self.database_system_url)):
                if not url_requires_tls(url):
                    problems.append(f"{name} with TLS (ssl=require or sslmode=verify-full)")
            if problems:
                raise ValueError("APP_ENV=prod refuses to start without: " + ", ".join(problems))
        return self

    def jwt_key(self) -> str:
        return self.jwt_secret.get_secret_value() if self.jwt_secret else _dev_secret("jwt")

    def pii_key(self) -> bytes:
        return (self.pii_hash_key.get_secret_value() if self.pii_hash_key else _dev_secret("pii")).encode()


@lru_cache(maxsize=4)
def _dev_secret(purpose: str) -> str:
    # Non-prod only (prod refuses to start without real secrets): random per process, so dev tokens and
    # dedup hashes do not survive a restart. Set JWT_SECRET / PII_HASH_KEY to make them stable.
    logger.warning("using an ephemeral %s secret; set it explicitly outside tests", purpose)
    return secrets.token_urlsafe(48)


@lru_cache(maxsize=1)
def get_platform_settings() -> PlatformSettings:
    return PlatformSettings()
