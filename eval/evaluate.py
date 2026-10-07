"""Run the engine over the golden set and compute metrics."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from engine.cache import CacheStats, cached_extract_candidate, get_extraction_cache
from engine.config import Settings, get_settings
from engine.extraction import extract_job_requirements
from engine.features import FEATURE_NAMES
from engine.llm import LLMProvider
from engine.normalize import normalize_candidate
from engine.parsing import parse_file
from engine.pipeline import screen
from engine.schemas import CandidateResult, ScreeningResult
from engine.scorer import GoldenExample
from eval.golden import GoldenSet
from eval.metrics import (
    aggregate_drop_rates,
    date_matches,
    f1_from_counts,
    ndcg10,
    precision_at_k,
    r_precision,
    recall_in_top_fraction,
    skill_prf,
)


@dataclass
class JobEval:
    job_id: str
    order: list[str]  # candidate files in presented order (ranked, ineligible, needs_reprocess)
    labels: list[int]
    ndcg_at_10: float | None
    precision_at_5: float
    recall_top20pct: float | None
    r_precision: float | None
    result: ScreeningResult


@dataclass
class EvalReport:
    mode: str
    jobs: list[JobEval]
    metrics: dict[str, float | None]  # None: not measurable (no annotations)
    skill_counts: tuple[int, int, int]
    date_exact: tuple[int, int]  # (correct, total) for fully dated jobs
    date_approx: tuple[int, int]  # (correct, total) for year-only / start-only jobs
    drop_rates: dict[str, dict[str, float]]
    cache_hits: int = 0
    cache_misses: int = 0
    per_resume: dict[str, dict] = field(default_factory=dict)
    examples: list[GoldenExample] = field(default_factory=list)


def _scored(result: ScreeningResult) -> list[CandidateResult]:
    return [*result.ranked, *result.ineligible]


def presented_order(result: ScreeningResult) -> list[str]:
    return [r.source_name for r in _scored(result)] + [i.source_name for i in result.needs_reprocess]


def _mean(values: Sequence[float | None]) -> float:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else 0.0


def evaluate(golden: GoldenSet, llm: LLMProvider | None, settings: Settings | None = None) -> EvalReport:
    settings = settings or get_settings()
    files = golden.resume_files
    documents = [(f, golden.resume_path(f).read_bytes()) for f in files]

    jobs: list[JobEval] = []
    examples: list[GoldenExample] = []
    cache = get_extraction_cache(settings)
    hits = misses = 0
    for job_id, jd_text in golden.jobs.items():
        requirements = extract_job_requirements(jd_text, llm, settings=settings).requirements.confirm(by="golden-eval")
        result = screen(requirements, documents, settings=settings, llm=llm, today=golden.reference_date, cache=cache)
        hits += result.run.extraction_cache_hits
        misses += result.run.extraction_cache_misses
        labels_by_file = golden.labels_for(job_id)
        order = presented_order(result)
        labels = [labels_by_file[f] for f in order]
        jobs.append(
            JobEval(
                job_id=job_id,
                order=order,
                labels=labels,
                ndcg_at_10=ndcg10(labels),
                precision_at_5=precision_at_k(labels, 5),
                recall_top20pct=recall_in_top_fraction(labels, 0.2),
                r_precision=r_precision(labels),
                result=result,
            )
        )
        for r in _scored(result):
            values = {f.name: (f.value if f.applicable else None) for f in r.features}
            examples.append(
                GoldenExample(
                    job_id=job_id,
                    candidate_id=r.source_name,
                    features={n: values[n] for n in FEATURE_NAMES},
                    label=labels_by_file[r.source_name],
                )
            )

    # Extraction quality: one extraction per resume, compared with the annotations.
    tp = fp = fn = 0
    exact = [0, 0]
    approx = [0, 0]
    drop_rows = []
    per_resume: dict[str, dict] = {}
    stats = CacheStats()
    for f in [f for f in files if f in golden.annotations]:  # extraction metrics need annotations
        ann = golden.annotations[f]
        doc = parse_file(golden.resume_path(f), settings=settings)
        extraction = cached_extract_candidate(doc.text, llm, cache, stats, settings=settings)
        group = (
            f"{extraction.method}:{extraction.prompt_name}@{extraction.prompt_version}"
            if extraction.method == "llm"
            else "rules"
        )
        drop_rows.append((group, {k: (v.total, v.dropped) for k, v in extraction.evidence_stats.items()}))
        if extraction.extraction is None:
            fn += len(ann.skills)
            exact[1] += sum(not j.approximate for j in ann.jobs)
            approx[1] += sum(j.approximate for j in ann.jobs)
            per_resume[f] = {"status": extraction.status}
            continue
        cand = normalize_candidate(
            extraction.extraction, today=golden.reference_date, low_confidence_jobs=extraction.low_confidence_jobs
        )
        t, p, n = skill_prf((s.canonical for s in cand.skills), ann.skills)
        tp, fp, fn = tp + t, fp + p, fn + n
        matches = date_matches(ann.jobs, cand)
        for job, ok in matches:
            bucket = approx if job.approximate else exact
            bucket[0] += ok
            bucket[1] += 1
        per_resume[f] = {
            "skills_missed": sorted({s for s in ann.skills} - {s.canonical for s in cand.skills}),
            "skills_extra": sorted({s.canonical for s in cand.skills} - set(ann.skills)),
            "dates_wrong": [j.title for j, ok in matches if not ok],
        }

    metrics = {
        "ndcg_at_10": _mean([j.ndcg_at_10 for j in jobs]),
        "precision_at_5": _mean([j.precision_at_5 for j in jobs]),
        "recall_top20pct": _mean([j.recall_top20pct for j in jobs]),
        "r_precision": _mean([j.r_precision for j in jobs]),
        "skill_f1": round(f1_from_counts(tp, fp, fn), 4) if golden.annotations else None,
        "date_accuracy": round((exact[0] + approx[0]) / (exact[1] + approx[1]), 4) if exact[1] + approx[1] else None,
    }
    return EvalReport(
        mode=settings.resolved_extraction_mode if llm is not None else "rules",
        jobs=jobs,
        metrics=metrics,
        skill_counts=(tp, fp, fn),
        date_exact=(exact[0], exact[1]),
        date_approx=(approx[0], approx[1]),
        drop_rates=aggregate_drop_rates(drop_rows),
        cache_hits=hits + stats.hits,
        cache_misses=misses + stats.misses,
        per_resume=per_resume,
        examples=examples,
    )
