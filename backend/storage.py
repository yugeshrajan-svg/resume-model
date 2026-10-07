"""Resume file storage: Azure Blob Storage (Azurite locally) with short-lived SAS upload URLs; private container.

Blob names are ``<company_id>/<uuid>/<safe filename>``; the API only accepts blob names under the caller's own
company prefix, so a tenant cannot register (or read) another tenant's upload.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol

from backend.config import PlatformSettings

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def blob_name_for(company_id: uuid.UUID, filename: str) -> str:
    safe = _SAFE.sub("_", filename).strip("._")[:120] or "resume"
    return f"{company_id}/{uuid.uuid4().hex}/{safe}"


def belongs_to(company_id: uuid.UUID, blob_name: str) -> bool:
    return blob_name.startswith(f"{company_id}/") and ".." not in blob_name


class BlobStorage(Protocol):
    def upload_url(self, blob_name: str, expires_at: datetime) -> str:
        """Write-only (create) SAS URL for a single blob."""
        ...

    def uri(self, blob_name: str) -> str: ...

    async def size(self, blob_name: str) -> int | None:
        """Blob size in bytes, or None if it does not exist."""
        ...

    async def read(self, blob_name: str) -> bytes: ...

    async def put(self, blob_name: str, data: bytes) -> None: ...

    async def delete(self, blob_name: str) -> None: ...

    async def exists(self, blob_name: str) -> bool: ...

    async def health(self) -> None: ...

    async def close(self) -> None: ...


class AzureBlobStorage:
    """Azure Blob Storage via the sync SDK run in threads (no extra async HTTP dependency)."""

    def __init__(self, settings: PlatformSettings) -> None:
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import BlobServiceClient

        if settings.blob_connection_string is None:
            raise ValueError("BLOB_CONNECTION_STRING is not set")
        conn = settings.blob_connection_string.get_secret_value()
        self._service = BlobServiceClient.from_connection_string(conn)
        self._container = self._service.get_container_client(settings.blob_container)
        parts = dict(p.split("=", 1) for p in conn.split(";") if "=" in p)
        self._account_name = parts["AccountName"]
        self._account_key = parts["AccountKey"]
        with contextlib.suppress(ResourceExistsError):
            self._container.create_container()  # private: no public access level

    def upload_url(self, blob_name: str, expires_at: datetime) -> str:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        sas = generate_blob_sas(
            account_name=self._account_name,
            container_name=self._container.container_name,
            blob_name=blob_name,
            account_key=self._account_key,
            permission=BlobSasPermissions(create=True),
            expiry=expires_at,
            start=datetime.now(UTC) - timedelta(minutes=1),  # clock skew
        )
        return f"{self.uri(blob_name)}?{sas}"

    def uri(self, blob_name: str) -> str:
        return self._container.get_blob_client(blob_name).url

    async def size(self, blob_name: str) -> int | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            props = await asyncio.to_thread(self._container.get_blob_client(blob_name).get_blob_properties)
        except ResourceNotFoundError:
            return None
        return int(props.size)

    async def read(self, blob_name: str) -> bytes:
        blob = self._container.get_blob_client(blob_name)
        return await asyncio.to_thread(lambda: blob.download_blob().readall())

    async def put(self, blob_name: str, data: bytes) -> None:
        blob = self._container.get_blob_client(blob_name)
        await asyncio.to_thread(blob.upload_blob, data, overwrite=True)

    async def delete(self, blob_name: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        blob = self._container.get_blob_client(blob_name)
        with contextlib.suppress(ResourceNotFoundError):
            await asyncio.to_thread(blob.delete_blob, delete_snapshots="include")

    async def exists(self, blob_name: str) -> bool:
        return await self.size(blob_name) is not None

    async def health(self) -> None:
        await asyncio.to_thread(
            self._container.get_container_properties, retry_total=0, connection_timeout=5, read_timeout=5
        )

    async def close(self) -> None:
        await asyncio.to_thread(self._service.close)


class InMemoryBlobStorage:
    """Test double with the same contract (no SAS: tests ``put`` the bytes directly)."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def upload_url(self, blob_name: str, expires_at: datetime) -> str:
        return f"memory://{blob_name}?se={expires_at.isoformat()}"

    def uri(self, blob_name: str) -> str:
        return f"memory://{blob_name}"

    async def size(self, blob_name: str) -> int | None:
        data = self.blobs.get(blob_name)
        return None if data is None else len(data)

    async def read(self, blob_name: str) -> bytes:
        return self.blobs[blob_name]

    async def put(self, blob_name: str, data: bytes) -> None:
        self.blobs[blob_name] = data

    async def delete(self, blob_name: str) -> None:
        self.blobs.pop(blob_name, None)

    async def exists(self, blob_name: str) -> bool:
        return blob_name in self.blobs

    async def health(self) -> None:
        return None

    async def close(self) -> None:
        return None


def build_storage(settings: PlatformSettings) -> BlobStorage:
    if settings.blob_connection_string is None:
        if settings.app_env == "prod":
            raise ValueError("BLOB_CONNECTION_STRING is required in prod")
        return InMemoryBlobStorage()
    return AzureBlobStorage(settings)
