"""Error analysis over the golden ranking: why relevant candidates were missed and who displaced them.

Covers (per job):
  * every label >= 2 candidate ranked outside the top 20%, and
  * every top-5 false positive (label < 2 inside the top 5).

Each case gets a one-line root cause, derived mechanically (no hand-tuning to labels):
  1. metric ceiling — there are more relevant candidates than slots (or fewer than 5 relevant ones),
     and every candidate on the other side of the cut is correctly ordered relative to this one;
  2. knockout — the candidate was ineligible (with the knockout reasons);
  3. otherwise, the feature with the largest weighted gap to the candidates it should have outranked
     (or been outranked by), with that feature's detail.
"""

from __future__ import annotations

import math
from pathlib import Path

from engine.features import FEATURE_NAMES
from engine.schemas import CandidateResult
from eval.evaluate import EvalReport, JobEval
from eval.metrics import RELEVANT

REPORT_PATH = Path(__file__).parent / "reports" / "error_analysis.md"


def _scored(job: JobEval) -> dict[str, CandidateResult]:
    return {r.source_name: r for r in [*job.result.ranked, *job.result.ineligible]}


def _values(r: CandidateResult) -> dict[str, float | None]:
    return {f.name: (f.value if f.applicable else None) for f in r.features}


def _feature_line(r: CandidateResult) -> str:
    vals = _values(r)
    return ", ".join(f"{n}={'N/A' if vals[n] is None else f'{vals[n]:.2f}'}" for n in FEATURE_NAMES)


def _weighted_gap_cause(r: CandidateResult, others: list[CandidateResult], higher: bool) -> str:
    """The feature that most explains r being ranked below (higher=False) / above (higher=True) ``others``."""
    if not others:
        return "no comparison candidates"
    weights = r.score_breakdown.configured_weights
    mine = _values(r)
    best_name, best_gap = None, 0.0
    for name in FEATURE_NAMES:
        theirs = [v for o in others if (v := _values(o)[name]) is not None]
        if mine[name] is None or not theirs:
            continue
        gap = weights[name] * ((mine[name] - sum(theirs) / len(theirs)) * (1 if higher else -1))
        if gap > best_gap:
            best_name, best_gap = name, gap
    if best_name is None:
        return "scores within noise of the comparison candidates"
    detail = next(f.detail for f in r.features if f.name == best_name)
    direction = "higher" if higher else "lower"
    return (
        f"{best_name} {direction} than the comparison candidates ({best_gap * 100:.1f} weighted points): {detail[:140]}"
    )


def analyze(report: EvalReport, golden_labels: dict[str, dict[str, int]]) -> tuple[str, list[dict]]:
    """Markdown report and the structured cases."""
    cases: list[dict] = []
    for job in report.jobs:
        labels = golden_labels[job.job_id]
        scored = _scored(job)
        n = len(job.order)
        k = max(1, math.ceil(0.2 * n))
        relevant_total = sum(lb >= RELEVANT for lb in job.labels)
        position = {f: i + 1 for i, f in enumerate(job.order)}

        for f in job.order[k:]:
            if labels[f] < RELEVANT:
                continue
            r = scored.get(f)
            above = [scored[g] for g in job.order[: position[f] - 1] if g in scored]
            if r is None:
                cause = "needs reprocessing (not scored)"
            elif not r.eligible:
                cause = "knocked out: " + "; ".join(x.detail for x in r.knockout.reasons)
            elif relevant_total > k and all(labels[o.source_name] >= RELEVANT for o in above):
                cause = (
                    f"metric ceiling: {relevant_total} relevant candidates for {k} top-20% slots; "
                    "all ranked above are relevant"
                )
            else:
                weaker_above = [o for o in above if labels[o.source_name] < labels[f]]
                cause = _weighted_gap_cause(r, weaker_above or above, higher=False)
            cases.append(_case("missed_relevant", job, f, labels[f], position[f], r, cause))

        for f in job.order[:5]:
            if labels[f] >= RELEVANT:
                continue
            r = scored.get(f)
            below_relevant = [scored[g] for g in job.order[position[f] :] if g in scored and labels[g] >= RELEVANT]
            if relevant_total < 5 and not below_relevant:
                cause = (
                    f"metric ceiling: only {relevant_total} candidates have label >= 2, so the top 5 must hold others"
                )
            elif r is None:
                cause = "needs reprocessing (not scored)"
            else:
                cause = _weighted_gap_cause(r, below_relevant, higher=True)
            cases.append(_case("top5_false_positive", job, f, labels[f], position[f], r, cause))

    return _markdown(report, cases), cases


def _case(kind: str, job: JobEval, f: str, label: int, pos: int, r: CandidateResult | None, cause: str) -> dict:
    return {
        "kind": kind,
        "job": job.job_id,
        "file": f,
        "label": label,
        "position": pos,
        "score": r.score if r else None,
        "eligible": r.eligible if r else None,
        "features": _feature_line(r) if r else "",
        "root_cause": cause,
        "engine_issue": not cause.startswith(("metric ceiling", "knocked out", "needs reprocessing")),
    }


def _markdown(report: EvalReport, cases: list[dict]) -> str:
    m = report.metrics
    lines = [
        "# Golden-set error analysis",
        "",
        f"Mode: extraction={report.mode}. NDCG@10={m['ndcg_at_10']:.4f}, precision@5={m['precision_at_5']:.4f}, "
        f"recall@top20%={m['recall_top20pct']:.4f}.",
        "",
        "Generated by `python -m eval.run_eval`. Root causes are derived mechanically from features and labels;",
        "`engine issue = yes` marks cases not explained by a metric ceiling, a knockout or reprocessing.",
        "",
    ]
    if not cases:
        lines.append("No missed relevant candidates and no top-5 false positives.")
    for kind, title in (
        ("missed_relevant", "Label >= 2 outside the top 20%"),
        ("top5_false_positive", "Top-5 false positives"),
    ):
        group = [c for c in cases if c["kind"] == kind]
        lines += [f"## {title} ({len(group)})", ""]
        for c in group:
            status = "eligible" if c["eligible"] else ("ineligible" if c["eligible"] is False else "not scored")
            lines += [
                f"### {c['job']} / {c['file']}",
                f"- label {c['label']}, position {c['position']}, score {c['score']}, {status}",
                f"- features: {c['features']}",
                f"- root cause: {c['root_cause']}",
                f"- engine issue: {'yes' if c['engine_issue'] else 'no'}",
                "",
            ]
    return "\n".join(lines)


def write_report(markdown: str, path: Path = REPORT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown + "\n", encoding="utf-8")
    return path
