"""Measure the skill dictionary on a resume set labelled by group (sub-folders), before and after a change.

Resumes are split 50/50 by a hash of the file name. On the TRAIN half, each group's most distinctive recognised
skills become the required skills of a job description for that group. On the held-out TEST half it reports:

* coverage: the share of resumes in which the rule-based extractor recognises at least one skill;
* group eligibility: running the real JD analysis and knockout rules, the share of eligible resumes that belong to
  the group (precision) and the share of the group's resumes that are eligible (recall).

Groups are a coarse proxy for fit (an accountant resume is not necessarily fit for every accountant job), so the
numbers compare dictionary versions; they are not hiring accuracy.

    python -m eval.skill_dictionary_eval .cache/datasets/resume-dataset.zip \\
        --baseline-ontology .cache/datasets/skills_ontology_v1.json
"""

from __future__ import annotations

import argparse
import hashlib
import statistics
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from engine.config import get_settings
from engine.extraction import extract_job_requirements, rule_based_candidate
from engine.knockout import evaluate_knockouts
from engine.normalize import ONTOLOGY_PATH, SkillOntology, load_ontology, normalize_candidate
from engine.parsing import ParseError, parse_bytes

REQUIRED_PER_GROUP = 4
MIN_MATCH = 2
MIN_LIFT = 2.0  # a skill is distinctive for a group if it is at least this much more common there than overall


def split_of(name: str) -> str:
    return "train" if int(hashlib.sha256(name.encode()).hexdigest(), 16) % 2 == 0 else "test"


def load_resumes(source: Path) -> list[dict]:
    resumes = []
    archive = zipfile.ZipFile(source) if source.suffix.lower() == ".zip" else None
    paths = (
        [n for n in archive.namelist() if n.lower().endswith((".pdf", ".docx", ".txt"))]
        if archive
        else [str(p) for p in sorted(source.rglob("*")) if p.suffix.lower() in (".pdf", ".docx", ".txt")]
    )
    for path in paths:
        parts = path.replace("\\", "/").split("/")
        data = archive.read(path) if archive else Path(path).read_bytes()
        try:
            text = parse_bytes(data, parts[-1]).text
        except ParseError:
            continue
        resumes.append({"group": parts[-2], "split": split_of(Path(parts[-1]).stem), "text": text})
    return resumes


def profile(resume: dict, ontology: SkillOntology):
    """Rule-based extraction and normalisation of a resume, computed once per dictionary version."""
    key = f"profile:{ontology.version}"
    if key not in resume:
        extraction = rule_based_candidate(resume["text"], ontology)
        resume[key] = (extraction, normalize_candidate(extraction, ontology, date.today()))
    return resume[key]


def skills(resume: dict, ontology: SkillOntology) -> list[str]:
    return [s.name for s in profile(resume, ontology)[0].skills]


def group_requirements(train: list[dict], ontology: SkillOntology) -> dict[str, list[str]]:
    """Each group's most common skills among those distinctive for it (train split only)."""
    per_group: dict[str, Counter] = defaultdict(Counter)
    overall, sizes = Counter(), Counter()
    for r in train:
        sizes[r["group"]] += 1
        for name in set(skills(r, ontology)):
            per_group[r["group"]][name] += 1
            overall[name] += 1
    out = {}
    for group, counts in per_group.items():
        share = {s: n / sizes[group] for s, n in counts.items()}
        lift = {s: share[s] / (overall[s] / len(train)) for s in counts}
        ranked = sorted((s for s in counts if lift[s] >= MIN_LIFT and counts[s] >= 3), key=lambda s: -share[s])
        out[group] = ranked[:REQUIRED_PER_GROUP]
    return out


def eligibility(test: list[dict], group: str, required: list[str], ontology: SkillOntology) -> dict[str, float]:
    """Analyse a JD listing the group's skills, then apply the knockout rules to every held-out resume."""
    settings = get_settings()
    jd = f"{group.title()} position\n\nRequirements\n" + "\n".join(f"- {name}" for name in required) + "\n"
    req = extract_job_requirements(jd, None, settings, ontology).requirements
    if req.required_skills:
        req = req.model_copy(update={"required_skills_min_match": min(MIN_MATCH, len(req.required_skills))})
    eligible = [r for r in test if evaluate_knockouts(req, profile(r, ontology)[1], settings, ontology).eligible]
    in_group = sum(1 for r in test if r["group"] == group)
    hits = sum(1 for r in eligible if r["group"] == group)
    return {
        "recognised": len(req.required_skills),
        "eligible": len(eligible),
        "precision": hits / len(eligible) if eligible else 0.0,
        "recall": hits / in_group if in_group else 0.0,
    }


def coverage(test: list[dict], ontology: SkillOntology) -> dict[str, tuple[float, float]]:
    per: dict[str, list[int]] = defaultdict(list)
    for r in test:
        per[r["group"]].append(len(skills(r, ontology)))
    return {g: (sum(1 for n in v if n) / len(v), statistics.median(v)) for g, v in per.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="folder or .zip with one sub-folder per group")
    parser.add_argument("--ontology", type=Path, default=ONTOLOGY_PATH, help="dictionary to evaluate")
    parser.add_argument("--baseline-ontology", type=Path, help="earlier dictionary to compare against")
    parser.add_argument("--out", type=Path, help="markdown report path")
    args = parser.parse_args()
    # Reports contain characters such as "→"; Windows consoles and redirected output default to a legacy code page.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    resumes = load_resumes(args.source)
    train = [r for r in resumes if r["split"] == "train"]
    test = [r for r in resumes if r["split"] == "test"]
    current = load_ontology(args.ontology)
    baseline = load_ontology(args.baseline_ontology) if args.baseline_ontology else None
    required = group_requirements(train, current)
    groups = sorted({r["group"] for r in resumes})
    cov_new = coverage(test, current)
    cov_old = coverage(test, baseline) if baseline else {}

    rows, results = [], {}
    for group in groups:
        new = eligibility(test, group, required[group], current) if required.get(group) else None
        old = eligibility(test, group, required[group], baseline) if baseline and required.get(group) else None
        results[group] = (new, old)
        cells = [group, ", ".join(required.get(group, [])) or "—"]
        if baseline:
            cells.append(f"{cov_old[group][0]:.0%} → {cov_new[group][0]:.0%}")
            cells.append(f"{old['precision']:.0%} → {new['precision']:.0%}" if new and old else "—")
            cells.append(f"{old['recall']:.0%} → {new['recall']:.0%}" if new and old else "—")
        else:
            cells += [
                f"{cov_new[group][0]:.0%}",
                f"{new['precision']:.0%}" if new else "—",
                f"{new['recall']:.0%}" if new else "—",
            ]
        rows.append("| " + " | ".join(cells) + " |")

    scored = [g for g in groups if results[g][0]]
    mean = lambda key, i: statistics.mean(results[g][i][key] for g in scored)  # noqa: E731
    arrow = " (before → after)" if baseline else ""
    lines = [
        f"# Skill dictionary on held-out resumes ({current.version})",
        "",
        f"{len(test)} held-out resumes in {len(groups)} groups ({len(train)} used to pick each group's required "
        f"skills). Required skills = the {REQUIRED_PER_GROUP} most common skills that are distinctive for the group "
        f"(lift ≥ {MIN_LIFT}); eligible = at least {MIN_MATCH} of them, all other knockout rules unchanged.",
        "",
        f"| Group | Required skills (from train split) | Resumes with skills{arrow} | Eligibility precision"
        f"{arrow} | Eligibility recall{arrow} |",
        "|---|---|---|---|---|",
        *rows,
        "",
        f"Macro average over {len(scored)} groups: coverage "
        + (f"{statistics.mean(c[0] for c in cov_old.values()):.0%} → " if baseline else "")
        + f"{statistics.mean(c[0] for c in cov_new.values()):.0%}; eligibility precision "
        + (f"{mean('precision', 1):.0%} → " if baseline else "")
        + f"{mean('precision', 0):.0%}; recall "
        + (f"{mean('recall', 1):.0%} → " if baseline else "")
        + f"{mean('recall', 0):.0%}. A random choice would give ≈{1 / len(groups):.0%} precision.",
    ]
    report = "\n".join(lines) + "\n"
    if args.out:
        args.out.write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
