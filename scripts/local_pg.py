"""Throwaway local PostgreSQL cluster for tests and development (no Docker needed).

Creates a private cluster in .cache/pgdata (port 55432) using an installed PostgreSQL's binaries, with the two
roles the backend expects:
  screening_system  owns the schema, runs migrations, BYPASSRLS (login lookup, queue claims, retention)
  screening_app     ordinary role used for all tenant requests; row-level security always applies

    python scripts/local_pg.py start   # init (first time) + start, prints the .env values
    python scripts/local_pg.py stop
    python scripts/local_pg.py reset   # stop and delete the cluster

Binaries are found via PG_BIN, then PATH, then the default Windows install location. Local-only (trust auth on
127.0.0.1); never use this for real data.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".cache" / "pgdata"
LOG = ROOT / ".cache" / "pgdata.log"
PORT = int(os.environ.get("LOCAL_PG_PORT", "55432"))
DB = "screening"
INIT_SQL = ROOT / "infra" / "postgres" / "init.sql"


def _bin(name: str) -> str:
    candidates = []
    if os.environ.get("PG_BIN"):
        candidates.append(Path(os.environ["PG_BIN"]) / name)
    if found := shutil.which(name):
        candidates.append(Path(found))
    candidates += sorted(Path("C:/Program Files/PostgreSQL").glob(f"*/bin/{name}.exe"), reverse=True)
    for c in candidates:
        if c.exists() or c.with_suffix(".exe").exists():
            return str(c)
    raise SystemExit(f"PostgreSQL binary {name!r} not found; set PG_BIN")


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, capture_output=True, text=True)


def _psql(sql: str, db: str = "postgres") -> None:
    _run(_bin("psql"), "-h", "127.0.0.1", "-p", str(PORT), "-U", "postgres", "-d", db, "-v", "ON_ERROR_STOP=1",
         "-c", sql)  # fmt: skip


def start() -> None:
    if not (DATA / "PG_VERSION").exists():
        DATA.parent.mkdir(parents=True, exist_ok=True)
        _run(_bin("initdb"), "-D", str(DATA), "-U", "postgres", "--auth=trust", "-E", "UTF8", "--locale=C")
    status = _run(_bin("pg_ctl"), "-D", str(DATA), "status", check=False)
    if status.returncode != 0:
        # No output capture: the server inherits pg_ctl's handles, so capturing would block until it exits.
        subprocess.run(
            [_bin("pg_ctl"), "-D", str(DATA), "-l", str(LOG), "-o", f"-p {PORT} -c listen_addresses=127.0.0.1",
             "-w", "start"],
            check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
    exists = _run(_bin("psql"), "-h", "127.0.0.1", "-p", str(PORT), "-U", "postgres", "-tAc",
                  f"SELECT 1 FROM pg_database WHERE datname='{DB}'")  # fmt: skip
    if exists.stdout.strip() != "1":
        _psql(f"CREATE DATABASE {DB}")
    _psql(INIT_SQL.read_text(encoding="utf-8"), db=DB)
    print("Add these values to .env:")
    print(f"DATABASE_URL=postgresql+asyncpg://screening_app:app@127.0.0.1:{PORT}/{DB}")
    print(f"DATABASE_SYSTEM_URL=postgresql+asyncpg://screening_system:system@127.0.0.1:{PORT}/{DB}")


def stop() -> None:
    if (DATA / "PG_VERSION").exists():
        _run(_bin("pg_ctl"), "-D", str(DATA), "-m", "fast", "stop", check=False)


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "start"
    if command == "start":
        start()
    elif command == "stop":
        stop()
    elif command == "reset":
        stop()
        shutil.rmtree(DATA, ignore_errors=True)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
