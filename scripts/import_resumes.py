"""Bulk-import a folder or .zip of resumes into a workspace through the platform API.

Files go through the same path as dashboard uploads (short-lived upload URL, then batched registration), so the
worker parses, indexes and matches them as usual. Sub-folder names are treated as groups, which allows a
stratified sample and is written to the mapping file (useful for labelled sets such as the Kaggle resume dataset).

    python scripts/import_resumes.py .cache/datasets/resume-dataset.zip --create-workspace "Kaggle test data" \\
        --per-group 20 --consent-source "Kaggle CC0 example resumes (test data)" --talent-pool \\
        --map .cache/datasets/kaggle-import.csv

Use a separate workspace for test data, so it never mixes with real candidates. Consent records are created only
from the options given here: record only consent that was actually obtained (or, for public test data, label it
clearly as such).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import secrets
import sys
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import httpx

EXTENSIONS = (".pdf", ".docx", ".txt")
MAX_BATCH = 500


class Platform:
    """Minimal API client that signs in again when the access token expires."""

    def __init__(self, api: str, email: str, password: str) -> None:
        self.email, self.password = email, password
        self.http = httpx.Client(base_url=api.rstrip("/"), timeout=120, trust_env=False)
        self._login()

    def _login(self) -> None:
        r = self.http.post("/v1/auth/login", json={"email": self.email, "password": self.password})
        r.raise_for_status()
        self.http.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        r = self.http.request(method, path, **kwargs)
        if r.status_code == 401:
            self._login()
            r = self.http.request(method, path, **kwargs)
        return r


def create_workspace(api: str, name: str) -> tuple[str, str]:
    email = f"admin-{secrets.token_hex(4)}@test-workspace.example"
    password = secrets.token_urlsafe(18)
    r = httpx.post(
        f"{api.rstrip('/')}/v1/auth/register-company",
        json={"company_name": name, "admin_email": email, "admin_password": password},
        timeout=60,
        trust_env=False,
    )
    r.raise_for_status()
    return email, password


def collect(source: Path) -> list[tuple[str, str, bytes | None]]:
    """(group, path, data-or-None) for every resume file; data is read lazily for folders."""
    files = []
    if source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            for name in archive.namelist():
                if name.lower().endswith(EXTENSIONS):
                    parts = name.split("/")
                    files.append((parts[-2] if len(parts) > 1 else "", name, None))
    else:
        for path in sorted(source.rglob("*")):
            if path.is_file() and path.suffix.lower() in EXTENSIONS:
                files.append((path.parent.name if path.parent != source else "", str(path), None))
    return files


def select(files: list, per_group: int | None, limit: int | None, seed: int) -> list:
    rng = random.Random(seed)
    if per_group:
        groups: dict[str, list] = defaultdict(list)
        for item in files:
            groups[item[0]].append(item)
        files = [x for g in sorted(groups) for x in rng.sample(groups[g], min(per_group, len(groups[g])))]
    if limit and len(files) > limit:
        files = rng.sample(files, limit)
    return files


def read(source: Path, path: str, archive: zipfile.ZipFile | None) -> bytes:
    return archive.read(path) if archive else Path(path).read_bytes()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="folder or .zip of PDF/DOCX/TXT resumes")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--email", help="admin or recruiter of the target workspace")
    parser.add_argument("--password")
    parser.add_argument("--create-workspace", metavar="NAME", help="register a new workspace and import into it")
    parser.add_argument("--credentials-out", type=Path, help="with --create-workspace: save the new admin login")
    parser.add_argument("--job-id", help="also apply every resume to this job")
    parser.add_argument("--per-group", type=int, help="stratified sample: at most N files per sub-folder")
    parser.add_argument("--limit", type=int, help="at most N files in total")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--batch", type=int, default=100, help=f"files per registration request (max {MAX_BATCH})")
    parser.add_argument("--consent-source", help="record consent with this source (only consent actually obtained)")
    parser.add_argument("--consent-purpose", default="Resume screening and consideration for suitable roles")
    parser.add_argument("--talent-pool", action="store_true", help="consent includes consideration for future roles")
    parser.add_argument("--map", type=Path, help="write resume_id,group,file CSV for evaluation")
    args = parser.parse_args()
    if not 1 <= args.batch <= MAX_BATCH:
        parser.error(f"--batch must be between 1 and {MAX_BATCH}")
    if args.talent_pool and not args.consent_source:
        parser.error("--talent-pool requires --consent-source")

    if args.create_workspace:
        email, password = create_workspace(args.api, args.create_workspace)
        print(f"Created workspace {args.create_workspace!r}. Sign in as {email}")
        if args.credentials_out:
            args.credentials_out.parent.mkdir(parents=True, exist_ok=True)
            args.credentials_out.write_text(json.dumps({"email": email, "password": password}), encoding="utf-8")
            print(f"Login saved to {args.credentials_out} (local test workspace; delete it when done)")
        else:
            print(f"Password: {password}")
    elif args.email and args.password:
        email, password = args.email, args.password
    else:
        parser.error("give --email and --password, or --create-workspace")

    platform = Platform(args.api, email, password)
    files = select(collect(args.source), args.per_group, args.limit, args.seed)
    if not files:
        print("No PDF, DOCX or TXT files found.", file=sys.stderr)
        return 1
    consent = None
    if args.consent_source:
        consent = {
            "source": args.consent_source,
            "purpose": args.consent_purpose,
            "scope": "talent_pool" if args.talent_pool else "job",
        }
    else:
        print("No consent given: imported candidates cannot be screened or matched until consent is recorded.")

    archive = zipfile.ZipFile(args.source) if args.source.suffix.lower() == ".zip" else None
    started = time.perf_counter()
    registered, duplicates, failed, rows = 0, 0, [], []
    for offset in range(0, len(files), args.batch):
        chunk, items = files[offset : offset + args.batch], []
        for group, path, _ in chunk:
            data = read(args.source, path, archive)
            filename = path.replace("\\", "/").split("/")[-1]
            sas = platform.request("POST", "/v1/uploads/sas", json={"filename": filename, "size": len(data)})
            if sas.status_code != 200:
                failed.append((path, sas.text[:200]))
                continue
            target = sas.json()
            put = httpx.put(target["upload_url"], content=data, headers=target["headers"], timeout=120, trust_env=False)
            if put.status_code not in (200, 201):
                failed.append((path, f"upload failed ({put.status_code})"))
                continue
            item = {"blob_name": target["blob_name"], "filename": filename, "consent": consent}
            if args.job_id:
                item["job_id"] = args.job_id
            items.append((group, path, item))
        if not items:
            continue
        result = platform.request("POST", "/v1/resumes/register-bulk", json={"items": [i for _, _, i in items]})
        result.raise_for_status()
        by_blob = {r["blob_name"]: r for r in result.json()}
        for group, path, item in items:
            outcome = by_blob.get(item["blob_name"], {})
            if outcome.get("resume"):
                registered += 1
                duplicates += not outcome.get("created", True)
                rows.append({"resume_id": outcome["resume"]["id"], "group": group, "file": path})
            else:
                failed.append((path, outcome.get("error", "not registered")))
        print(f"  {min(offset + args.batch, len(files))}/{len(files)} files sent", flush=True)

    if args.map:
        args.map.parent.mkdir(parents=True, exist_ok=True)
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=["resume_id", "group", "file"])
        writer.writeheader()
        writer.writerows(rows)
        args.map.write_text(buffer.getvalue(), encoding="utf-8")
    elapsed = time.perf_counter() - started
    print(f"Registered {registered} of {len(files)} files in {elapsed:.0f}s ({duplicates} already present).")
    for path, error in failed[:20]:
        print(f"  failed: {path}: {error}")
    if len(failed) > 20:
        print(f"  … and {len(failed) - 20} more failures")
    print("The worker now parses and indexes them; a talent-pool search shows indexing progress.")
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
