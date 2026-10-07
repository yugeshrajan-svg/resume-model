"""Golden set loading and validation.

Layout of ``eval/golden``:
  golden.json         {"reference_date": "YYYY-MM-DD", "jobs": [...], "resumes": N}
  jobs/<job_id>.txt   job descriptions
  resumes/<file>      resumes (PDF/DOCX/TXT)
  labels.jsonl        {"job_id", "candidate_file", "label": 0-3}
  annotations.jsonl   {"candidate_file", "layout", "has_projects", "skills": [...],
                       "jobs": [{title, start, end, approximate}]}
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

GOLDEN_DIR = Path(__file__).parent / "golden"
REAL_DIR = Path(__file__).parent / "real"  # git-ignored; see README "Evaluating on real data"
REQUIRED_LAYOUTS = {"above", "below", "same_line", "two_column", "year_only"}


class Label(BaseModel):
    job_id: str
    candidate_file: str
    label: int = Field(ge=0, le=3)


class AnnotatedJob(BaseModel):
    title: str
    start: str  # YYYY-MM
    end: str | None  # YYYY-MM or "present"
    approximate: bool = False


class Annotation(BaseModel):
    candidate_file: str
    layout: str
    has_projects: bool
    skills: list[str]
    jobs: list[AnnotatedJob]


class GoldenSet(BaseModel):
    root: Path
    reference_date: date
    jobs: dict[str, str]  # job_id -> JD text
    labels: list[Label]
    annotations: dict[str, Annotation]  # candidate_file -> annotation (may be partial for real sets)
    resume_files: list[str]

    def resume_path(self, candidate_file: str) -> Path:
        return self.root / "resumes" / candidate_file

    def labels_for(self, job_id: str) -> dict[str, int]:
        return {lb.candidate_file: lb.label for lb in self.labels if lb.job_id == job_id}


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name}:{n}: invalid JSON: {exc}") from exc
    return rows


def load_golden(root: Path = GOLDEN_DIR, synthetic: bool = True) -> GoldenSet:
    """Load and validate a labelled set; raises ValueError describing every problem found.

    ``synthetic=True`` (the built-in golden set) also requires annotations for every resume and coverage
    of every date layout. Real sets (``synthetic=False``) only need labels; annotations.jsonl is optional
    and may cover a subset of resumes (extraction metrics are computed on that subset).
    """
    meta = json.loads((root / "golden.json").read_text(encoding="utf-8"))
    jobs = {job_id: (root / "jobs" / f"{job_id}.txt").read_text(encoding="utf-8") for job_id in meta["jobs"]}
    labels = [Label(**row) for row in _read_jsonl(root / "labels.jsonl")]
    ann_path = root / "annotations.jsonl"
    rows = _read_jsonl(ann_path) if (synthetic or ann_path.exists()) else []
    annotations = {a.candidate_file: a for a in (Annotation(**row) for row in rows)}

    problems: list[str] = []
    files = {p.name for p in (root / "resumes").iterdir() if p.is_file()}
    for lb in labels:
        if lb.job_id not in jobs:
            problems.append(f"label for unknown job {lb.job_id!r}")
        if lb.candidate_file not in files:
            problems.append(f"label for missing resume {lb.candidate_file!r}")
    pairs = {(lb.job_id, lb.candidate_file) for lb in labels}
    if len(pairs) != len(labels):
        problems.append("duplicate (job_id, candidate_file) labels")
    for job_id in jobs:
        for f in files:
            if (job_id, f) not in pairs:
                problems.append(f"no label for ({job_id}, {f})")
    if set(annotations) - files:
        problems.append(f"annotations for missing resumes: {sorted(set(annotations) - files)}")
    if synthetic:
        if set(annotations) != files:
            problems.append(f"annotations/resumes mismatch: {sorted(set(annotations) ^ files)}")
        missing_layouts = REQUIRED_LAYOUTS - {a.layout for a in annotations.values()}
        if missing_layouts:
            problems.append(f"golden set lacks date layouts: {sorted(missing_layouts)}")
        if all(a.has_projects for a in annotations.values()):
            problems.append("golden set needs resumes without a projects section")
    if problems:
        raise ValueError("Invalid golden set:\n  " + "\n  ".join(problems))

    return GoldenSet(
        root=root,
        reference_date=date.fromisoformat(meta["reference_date"]),
        jobs=jobs,
        labels=labels,
        annotations=annotations,
        resume_files=sorted(files),
    )
