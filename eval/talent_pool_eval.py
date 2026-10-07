"""Talent-pool retrieval quality on a resume set labelled by group (for example the Kaggle resume categories).

For each group, search the workspace's talent pool with the group's name and measure how many of the top-k results
belong to that group (precision@k; a random ranking scores about 1/number-of-groups). Groups are a coarse,
profession-level label, so this measures whether search finds the right kind of person, not fit for a specific job.

It also reports, per group, how many resumes the rule-based extractor finds skills in (skill-dictionary coverage),
read directly from the source files.

    python -m eval.talent_pool_eval --map .cache/datasets/kaggle-import.csv \\
        --credentials .cache/datasets/kaggle-workspace.json --source .cache/datasets/resume-dataset.zip --wait
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import httpx

from engine.extraction import rule_based_candidate
from engine.parsing import ParseError, parse_bytes


def query_for(group: str) -> str:
    words = group.replace("-", " ").replace("_", " ").split()
    query = " ".join(w if len(w) <= 3 else w.capitalize() if i == 0 else w.lower() for i, w in enumerate(words))
    return query if len(query) >= 3 else f"{query} professional"  # search needs at least 3 characters ("HR")


class Platform:
    def __init__(self, api: str, email: str, password: str) -> None:
        self.email, self.password = email, password
        self.http = httpx.Client(base_url=api.rstrip("/"), timeout=180, trust_env=False)
        self._login()

    def _login(self) -> None:
        r = self.http.post("/v1/auth/login", json={"email": self.email, "password": self.password})
        r.raise_for_status()
        self.http.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    def search(self, query: str, limit: int) -> dict:
        body = {"query": query, "limit": limit}
        r = self.http.post("/v1/talent-pool/search", json=body)
        if r.status_code == 401:
            self._login()
            r = self.http.post("/v1/talent-pool/search", json=body)
        r.raise_for_status()
        return r.json()


def wait_for_index(platform: Platform, expected: int, timeout_s: int) -> dict:
    """Wait until every processed resume is indexed. Unreadable files (e.g. scans needing OCR) never are, so the
    wait also ends once nothing is pending and the count has stopped changing."""
    started, previous = time.monotonic(), None
    while True:
        status = platform.search("resume", 1)
        done, pending = status["indexed_candidates"], status["resumes_pending_index"]
        print(f"  indexed {done} candidates, {pending} resumes waiting", flush=True)
        if pending == 0 and (done >= expected or done == previous):
            return status
        previous = done if pending == 0 else None
        if time.monotonic() - started > timeout_s:
            print("  timed out waiting; evaluating what is indexed so far", flush=True)
            return status
        time.sleep(30)


def skill_coverage(rows: list[dict], source: Path) -> dict[str, dict[str, float]]:
    found: dict[str, list[int]] = defaultdict(list)
    with zipfile.ZipFile(source) if source.suffix.lower() == ".zip" else _Folder() as archive:
        for row in rows:
            data = archive.read(row["file"])
            try:
                text = parse_bytes(data, row["file"].split("/")[-1]).text
            except ParseError:
                continue
            found[row["group"]].append(len(rule_based_candidate(text).skills))
    return {
        group: {"with_skills": sum(1 for n in counts if n) / len(counts), "median_skills": statistics.median(counts)}
        for group, counts in found.items()
    }


class _Folder:
    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self, path: str) -> bytes:
        return Path(path).read_bytes()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--map", type=Path, required=True, help="resume_id,group,file CSV from import_resumes.py")
    parser.add_argument("--credentials", type=Path, help='JSON {"email", "password"} of the workspace')
    parser.add_argument("--email")
    parser.add_argument("--password")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--source", type=Path, help="the imported folder/.zip, for skill-dictionary coverage")
    parser.add_argument("--k", type=int, nargs="+", default=[10, 20])
    parser.add_argument("--wait", action="store_true", help="wait until every imported resume is indexed")
    parser.add_argument("--wait-timeout", type=int, default=4 * 3600)
    parser.add_argument("--out", type=Path, help="markdown report (default: next to the map file)")
    args = parser.parse_args()
    # Reports contain characters such as "→"; Windows consoles and redirected output default to a legacy code page.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.credentials:
        creds = json.loads(args.credentials.read_text(encoding="utf-8"))
        email, password = creds["email"], creds["password"]
    elif args.email and args.password:
        email, password = args.email, args.password
    else:
        parser.error("give --credentials or --email and --password")

    rows = list(csv.DictReader(args.map.open(encoding="utf-8")))
    group_of = {row["resume_id"]: row["group"] for row in rows}
    groups = sorted(set(group_of.values()))
    platform = Platform(args.api, email, password)
    if args.wait:
        status = wait_for_index(platform, len(set(group_of)), args.wait_timeout)
    else:
        status = platform.search("resume", 1)
    top = max(args.k)

    table, confusions = [], {}
    for group in groups:
        query = query_for(group)
        started = time.perf_counter()
        results = platform.search(query, top)["results"]
        latency = time.perf_counter() - started
        labels = [group_of.get(r["matched_resume"]["resume_id"], "?") for r in results]
        precision = {k: sum(label == group for label in labels[:k]) / k for k in args.k}
        confusions[group] = Counter(label for label in labels[: min(args.k)] if label != group).most_common(3)
        table.append(
            {
                "group": group,
                "query": query,
                "size": sum(1 for g in group_of.values() if g == group),
                "latency_s": latency,
                **{f"p@{k}": precision[k] for k in args.k},
            }
        )

    coverage = skill_coverage(rows, args.source) if args.source else {}
    lines = [
        "# Talent-pool retrieval on labelled resumes",
        "",
        f"{len(group_of)} resumes in {len(groups)} groups; {status['indexed_candidates']} candidates indexed with "
        f"`{status['model']}`. Query = the group name. Random ranking ≈ {1 / len(groups):.0%} precision.",
        "",
        "| Group | Query | Resumes | " + " | ".join(f"P@{k}" for k in args.k) + " | Most confused with (top "
        f"{min(args.k)}) |" + (" Resumes with recognised skills |" if coverage else ""),
        "|---|---|---|" + "---|" * len(args.k) + "---|" + ("---|" if coverage else ""),
    ]
    for row in table:
        confused = ", ".join(f"{g} ({n})" for g, n in confusions[row["group"]]) or "—"
        cells = [row["group"], row["query"], str(row["size"]), *(f"{row[f'p@{k}']:.0%}" for k in args.k), confused]
        if coverage:
            cells.append(f"{coverage.get(row['group'], {}).get('with_skills', 0):.0%}")
        lines.append("| " + " | ".join(cells) + " |")
    macro = {k: statistics.mean(row[f"p@{k}"] for row in table) for k in args.k}
    lines += [
        "",
        "Macro average: "
        + ", ".join(f"P@{k} {macro[k]:.0%}" for k in args.k)
        + f". Median search latency {statistics.median(r['latency_s'] for r in table):.2f}s.",
    ]
    if coverage:
        overall = statistics.mean(c["with_skills"] for c in coverage.values())
        lines.append(
            f"Rule-based extraction recognises at least one skill in {overall:.0%} of resumes (group average); "
            "the skill dictionary determines which skills the fast mode can match against requirements."
        )
    report = "\n".join(lines) + "\n"
    out = args.out or args.map.with_suffix(".eval.md")
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"Report written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
