"""Evaluation metrics: ranking quality, extraction quality and evidence drop rates."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence

from engine.normalize import canonical_skill_key
from engine.schemas import NormalizedCandidate
from engine.scorer import ndcg_at_k
from eval.golden import AnnotatedJob

RELEVANT = 2  # labels >= 2 count as relevant for precision/recall


def precision_at_k(labels_in_ranked_order: Sequence[int], k: int = 5) -> float:
    """Share of the top-k with label >= 2 (denominator is k, or fewer if the list is shorter)."""
    top = labels_in_ranked_order[:k]
    return sum(lb >= RELEVANT for lb in top) / len(top) if top else 0.0


def recall_in_top_fraction(labels_in_ranked_order: Sequence[int], fraction: float = 0.2) -> float | None:
    """Share of all label >= 2 candidates found in the top ``fraction`` of the list (at least 1 slot)."""
    relevant = sum(lb >= RELEVANT for lb in labels_in_ranked_order)
    if relevant == 0:
        return None
    k = max(1, math.ceil(fraction * len(labels_in_ranked_order)))
    return sum(lb >= RELEVANT for lb in labels_in_ranked_order[:k]) / relevant


def r_precision(labels_in_ranked_order: Sequence[int]) -> float | None:
    """Precision at rank R, where R = number of label >= 2 candidates. None if there are none."""
    r = sum(lb >= RELEVANT for lb in labels_in_ranked_order)
    if r == 0:
        return None
    return sum(lb >= RELEVANT for lb in labels_in_ranked_order[:r]) / r


def ndcg10(labels_in_ranked_order: Sequence[int]) -> float | None:
    return ndcg_at_k(labels_in_ranked_order, 10)


def skill_prf(predicted: Iterable[str], gold: Iterable[str]) -> tuple[int, int, int]:
    """(true positives, false positives, false negatives) after canonicalizing skill names."""
    p = {canonical_skill_key(s) for s in predicted}
    g = {canonical_skill_key(s) for s in gold}
    return len(p & g), len(p - g), len(g - p)


def f1_from_counts(tp: int, fp: int, fn: int) -> float:
    if tp == 0:
        return 0.0
    precision, recall = tp / (tp + fp), tp / (tp + fn)
    return 2 * precision * recall / (precision + recall)


def date_matches(annotated: Sequence[AnnotatedJob], candidate: NormalizedCandidate) -> list[tuple[AnnotatedJob, bool]]:
    """For each annotated job: was a job with the same title extracted with the same start/end month?"""
    out = []
    for job in annotated:
        found = [j for j in candidate.jobs if j.title.casefold() == job.title.casefold()]
        end = candidate.reference_month if job.end == "present" else job.end
        ok = any(j.start == job.start and j.end == end for j in found)
        out.append((job, ok))
    return out


def aggregate_drop_rates(rows: Iterable[tuple[str, dict[str, tuple[int, int]]]]) -> dict[str, dict[str, float]]:
    """Group per-resume evidence stats by prompt version: {group: {field: drop_rate, ..., "overall": rate}}.

    ``rows``: (group key such as "llm:resume_extraction@1.1.0" or "rules", {field: (total, dropped)}).
    """
    totals: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for group, stats in rows:
        for field, (total, dropped) in stats.items():
            totals[group][field][0] += total
            totals[group][field][1] += dropped
    report: dict[str, dict[str, float]] = {}
    for group, fields in totals.items():
        all_total = sum(t for t, _ in fields.values())
        all_dropped = sum(d for _, d in fields.values())
        report[group] = {f: round(d / t, 4) for f, (t, d) in sorted(fields.items()) if t}
        report[group]["overall"] = round(all_dropped / all_total, 4) if all_total else 0.0
    return report
