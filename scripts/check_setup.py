"""Check local database roles, migrations, tenant isolation, and a SAS blob round trip.

Run from the project root after starting the local services and applying migrations:
    python -m scripts.check_setup
    python -m scripts.check_setup --env-file .env.example
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from backend.config import PlatformSettings
from backend.db import Database
from backend.models import Base
from backend.storage import AzureBlobStorage, blob_name_for

ROOT = Path(__file__).resolve().parents[1]


async def check_database(settings: PlatformSettings) -> None:
    db = Database.from_settings(settings)
    try:
        async with db.system_session() as session:
            role = (
                await session.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))
            ).one()
            if role.rolsuper or not role.rolbypassrls:
                raise RuntimeError("The system role must be a non-superuser with BYPASSRLS")
            revision = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            expected = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
            if revision != expected:
                raise RuntimeError("Migrations are not current; run python -m alembic upgrade head")
            tables = (
                await session.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'"
                    )
                )
            ).all()
            secured = {row.relname for row in tables if row.relrowsecurity and row.relforcerowsecurity}
            if not set(Base.metadata.tables) <= secured:
                raise RuntimeError("Some platform tables are missing enforced row-level security")

        async with db.tenant_session(uuid.uuid4()) as session:
            role = (
                await session.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))
            ).one()
            if role.rolsuper or role.rolbypassrls:
                raise RuntimeError("The app role must be a non-superuser without BYPASSRLS")
            for table in Base.metadata.sorted_tables:
                count = (await session.execute(text(f'SELECT count(*) FROM "{table.name}"'))).scalar_one()
                if count:
                    raise RuntimeError("An unknown company can see another company's rows")
        print(f"PostgreSQL: migrations at {revision}; both roles and {len(secured)} RLS tables verified")
    finally:
        await db.dispose()


async def check_storage(settings: PlatformSettings) -> None:
    if settings.blob_connection_string is None:
        raise RuntimeError("Set BLOB_CONNECTION_STRING to the local Azurite connection string")
    storage = AzureBlobStorage(settings)
    name = blob_name_for(uuid.uuid4(), "setup-check.txt")
    data = b"Local resume screening storage check\n"
    request = Request(
        storage.upload_url(name, datetime.now(UTC) + timedelta(minutes=5)),
        data=data,
        headers={"x-ms-blob-type": "BlockBlob"},
        method="PUT",
    )

    def upload() -> None:
        with urlopen(request, timeout=10) as response:
            if response.status != 201:
                raise RuntimeError("The SAS upload did not create a blob")

    try:
        await asyncio.to_thread(upload)
        if await storage.read(name) != data or await storage.size(name) != len(data):
            raise RuntimeError("Blob content or size did not match the upload")
    finally:
        await storage.delete(name)
    if await storage.exists(name):
        raise RuntimeError("The temporary setup-check blob was not deleted")
    print("Azurite: SAS upload, read, size, and delete verified")


async def check(settings: PlatformSettings) -> None:
    await check_database(settings)
    await check_storage(settings)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env", help="Settings file (default: .env)")
    args = parser.parse_args()
    try:
        asyncio.run(check(PlatformSettings(_env_file=args.env_file)))
    except Exception as exc:
        print(f"Setup check failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
