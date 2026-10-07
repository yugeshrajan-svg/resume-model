"""End-to-end screening: parse -> extract -> verify -> normalize -> knockouts -> features -> score -> rank.

Rules enforced here:
* Screening refuses to run unless the requirements were confirmed by a human (``requirements_confirmed``).
* Extraction and semantic modes are resolved once per run; a batch never mixes LLM and rule extraction.
* Resumes that cannot be parsed, need OCR, or failed LLM extraction are excluded from ranking and
  returned in ``needs_reprocess``. Knocked-out candidates stay in the ranking, marked ineligible.

CLI:
    python -m engine.pipeline --jd data/sample/jd.txt --resumes data/sample/resumes/ --confirmed-by "Jane HR"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from engine import __version__
from engine.cache import CacheStats, ExtractionCache, cached_extract_candidate, get_extraction_cache
from engine.config import Settings, get_settings
from engine.explain import explain
from engine.extraction import extract_job_requirements
from engine.features import build_context, compute_features, job_profile_text
from engine.knockout import evaluate_knockouts
from engine.llm import EmbeddingProvider, LLMProvider, get_llm
from engine.logging_setup import configure_logging, log_event
from engine.normalize import SkillOntology, YearMonth, date_warnings, format_ym, load_ontology, normalize_candidate
from engine.parsing import SUPPORTED_EXTENSIONS, OCRHook, ParseError, parse_bytes
from engine.prompts import get_prompt
from engine.schemas import (
    CandidateExtractionMeta,
    CandidateResult,
    JobRequirements,
    LLMUsage,
    ReprocessItem,
    RunMetadata,
    ScreeningResult,
)
from engine.scorer import resolve_weights, score_features
from engine.semantic import (
    CrossEncoderReranker,
    EmbeddingScorer,
    SemanticScorer,
    build_reranker,
    build_semantic_scorer,
)

logger = logging.getLogger("engine.pipeline")


class RequirementsNotConfirmed(RuntimeError):
    """Screening was requested for requirements that no human has reviewed."""


ProgressCallback = Callable[[int, int], None]  # (documents done, documents total)


@dataclass(frozen=True)
class _RunContext:
    requirements: JobRequirements
    settings: Settings
    ontology: SkillOntology
    llm: LLMProvider | None
    weights: dict[str, float]
    semantic: SemanticScorer
    reranker: SemanticScorer | None
    similarity_backends: dict[str, str]
    cache: ExtractionCache
    cache_stats: CacheStats
    today: date
    ocr: OCRHook | None


@dataclass
class _DocOutcome:
    index: int
    result: CandidateResult | None = None
    reprocess: ReprocessItem | None = None
    usages: list[LLMUsage] = field(default_factory=list)


def screen(
    requirements: JobRequirements,
    documents: Sequence[tuple[str, bytes]],
    *,
    settings: Settings | None = None,
    llm: LLMProvider | Literal["auto"] | None = "auto",
    embedder: EmbeddingProvider | None = None,
    today: date | None = None,
    ocr: OCRHook | None = None,
    cache: ExtractionCache | None = None,
    max_workers: int | None = None,
    progress: ProgressCallback | None = None,
) -> ScreeningResult:
    """Rank ``documents`` (filename, bytes) against confirmed ``requirements``.

    Documents are processed concurrently (``max_workers``, default SCREEN_MAX_WORKERS); results do not
    depend on completion order. Extraction results are cached across runs and jobs.
    """
    if requirements.requirements_confirmed is None:
        raise RequirementsNotConfirmed(
            "Job requirements must be reviewed and confirmed (requirements_confirmed.by/at) before screening"
        )
    settings = settings or get_settings()
    today = today or date.today()
    if settings.llm_provider == "ollama" and (
        llm == "auto" or (embedder is None and settings.resolved_semantic_backend == "embeddings")
    ):
        from engine.local_ai import pin_local_models

        settings = pin_local_models(settings)
    started = time.perf_counter()
    ontology = load_ontology()

    # Run-level decisions, made once.
    llm_provider = get_llm(settings) if llm == "auto" else llm
    semantic = build_semantic_scorer(job_profile_text(requirements), settings, embedder, ontology)
    reranker = build_reranker(settings)
    run_ctx = _RunContext(
        requirements=requirements,
        settings=settings,
        ontology=ontology,
        llm=llm_provider,
        weights=resolve_weights(requirements, settings),
        semantic=semantic,
        reranker=reranker,
        similarity_backends={
            "project_relevance": semantic.name,
            "semantic_relevance": reranker.name if reranker else semantic.name,
        },
        cache=cache if cache is not None else get_extraction_cache(settings),
        cache_stats=CacheStats(),
        today=today,
        ocr=ocr,
    )
    method: Literal["llm", "rules"] = "llm" if llm_provider is not None else "rules"
    workers = max_workers or settings.screen_max_workers
    log_event(
        logger,
        "screening_started",
        documents=len(documents),
        extraction_mode=method,
        semantic_backend=semantic.name,
        max_workers=workers,
    )

    # Duplicates are resolved up front (first occurrence wins) so concurrency cannot change the outcome.
    outcomes: list[_DocOutcome] = []
    todo: list[tuple[int, str, bytes, str | None]] = []
    seen: set[str] = set()
    for index, (filename, data) in enumerate(documents):
        candidate_id = hashlib.sha256(data).hexdigest()[:12] if data else None
        if candidate_id is not None and candidate_id in seen:
            item = _reprocess(candidate_id, filename, "duplicate", "identical file already in this batch")
            outcomes.append(_DocOutcome(index=index, reprocess=item))
            continue
        if candidate_id is not None:
            seen.add(candidate_id)
        todo.append((index, filename, data, candidate_id))

    done = len(outcomes)
    if progress:
        progress(done, len(documents))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="screen") as pool:
        futures = [pool.submit(_screen_document, run_ctx, *job) for job in todo]
        for future in as_completed(futures):
            outcomes.append(future.result())
            done += 1
            if progress:
                progress(done, len(documents))

    outcomes.sort(key=lambda o: o.index)
    results = [o.result for o in outcomes if o.result is not None]
    reprocess = [o.reprocess for o in outcomes if o.reprocess is not None]
    usages = [u for o in outcomes for u in o.usages]

    # Ranked = eligible only, by score; candidate_id makes ties deterministic. Knocked-out candidates are
    # listed separately (by score, unranked) with their reasons.
    results.sort(key=lambda r: (-r.score, r.candidate_id))
    ranked = [r.model_copy(update={"rank": i}) for i, r in enumerate((r for r in results if r.eligible), start=1)]
    ineligible = [r.model_copy(update={"rank": None}) for r in results if not r.eligible]

    prompt = get_prompt("resume_extraction")
    stats = run_ctx.cache_stats
    run = RunMetadata(
        engine_version=__version__,
        started_at=datetime.now(UTC),
        reference_month=format_ym(YearMonth.from_date(today)),
        extraction_mode=method,
        prompt_name=prompt.name if method == "llm" else None,
        prompt_version=prompt.version if method == "llm" else None,
        model=llm_provider.model if llm_provider is not None else None,
        semantic_backend=semantic.name,
        reranker=reranker.model_name if isinstance(reranker, CrossEncoderReranker) else None,
        ontology_version=ontology.version,
        weights=run_ctx.weights,
        explanation_method="llm" if llm_provider is not None else "template",
        explanation_prompt_version=get_prompt("explanation").version if llm_provider is not None else None,
        requirements_confirmed=requirements.requirements_confirmed,
        documents_received=len(documents),
        llm_prompt_tokens=sum(u.prompt_tokens for u in usages),
        llm_completion_tokens=sum(u.completion_tokens for u in usages),
        llm_cost_usd=round(sum(u.cost_usd for u in usages), 6),
        embedding_tokens=semantic.usage_tokens if isinstance(semantic, EmbeddingScorer) else 0,
        embedding_cost_usd=round(semantic.usage_cost_usd, 6) if isinstance(semantic, EmbeddingScorer) else 0.0,
        extraction_cache_hits=stats.hits,
        extraction_cache_misses=stats.misses,
        max_workers=workers,
        wall_time_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    log_event(
        logger,
        "screening_finished",
        ranked=len(ranked),
        ineligible=len(ineligible),
        needs_reprocess=len(reprocess),
        wall_time_ms=run.wall_time_ms,
        llm_cost_usd=run.llm_cost_usd,
        embedding_cost_usd=run.embedding_cost_usd,
        cache_hits=stats.hits,
        cache_misses=stats.misses,
        cache_hit_rate=stats.hit_rate,
    )
    return ScreeningResult(
        job_role=requirements.role, run=run, ranked=ranked, ineligible=ineligible, needs_reprocess=reprocess
    )


def _screen_document(ctx: _RunContext, index: int, filename: str, data: bytes, candidate_id: str | None) -> _DocOutcome:
    """Parse, extract (cached), verify, normalize, knock out, score and explain one resume."""
    doc_start = time.perf_counter()
    out = _DocOutcome(index=index)
    try:
        doc = parse_bytes(data, filename, settings=ctx.settings, ocr=ctx.ocr)
    except ParseError as exc:
        out.reprocess = _reprocess(candidate_id, filename, "parse_error", str(exc))
        return out
    if doc.needs_ocr and not doc.ocr_applied:
        reason = f"only {doc.char_count} characters of text; OCR required"
        out.reprocess = _reprocess(candidate_id, filename, "needs_ocr", reason)
        return out

    extraction = cached_extract_candidate(
        doc.text, ctx.llm, ctx.cache, ctx.cache_stats, settings=ctx.settings, ontology=ctx.ontology
    )
    if extraction.usage and not extraction.from_cache:
        out.usages.append(extraction.usage)
    if extraction.status != "ok" or extraction.extraction is None:
        reason = extraction.failure_reason or "unknown"
        out.reprocess = _reprocess(candidate_id, filename, "extraction_failed", reason)
        return out

    candidate = normalize_candidate(
        extraction.extraction, ctx.ontology, ctx.today, low_confidence_jobs=extraction.low_confidence_jobs
    )
    knockout = evaluate_knockouts(ctx.requirements, candidate, ctx.settings, ctx.ontology)
    features = compute_features(
        build_context(ctx.requirements, candidate, ctx.semantic, ctx.settings, ctx.ontology, ctx.reranker)
    )
    breakdown = score_features(features, ctx.weights)
    # Explanations see only feature outputs + verified quotes, and never change the score.
    explanation = explain(
        features,
        knockout,
        ctx.llm,
        ctx.settings,
        warnings=[*date_warnings(candidate), *knockout.warnings],
        similarity_backends=ctx.similarity_backends,
    )
    if explanation.usage:
        out.usages.append(explanation.usage)

    out.result = CandidateResult(
        candidate_id=candidate_id or "",
        source_name=filename,
        rank=None,  # assigned after sorting
        score=breakdown.score,
        eligible=knockout.eligible,
        knockout=knockout,
        features=features,
        score_breakdown=breakdown,
        total_experience_months=candidate.total_experience_months,
        hidden_items_removed=doc.hidden_items_removed,
        extraction=CandidateExtractionMeta(
            method=extraction.method,
            prompt_name=extraction.prompt_name,
            prompt_version=extraction.prompt_version,
            model=extraction.model,
            ontology_version=extraction.ontology_version,
            attempts=extraction.attempts,
            dropped_items=extraction.dropped_count,
            evidence_drop_rates=extraction.evidence_drop_rates,
            input_truncated=extraction.input_truncated,
            from_cache=extraction.from_cache,
            usage=extraction.usage,
        ),
        explanation=explanation,
        explanation_source=explanation.method,
    )
    log_event(
        logger,
        "resume_screened",
        candidate_id=candidate_id,
        score=breakdown.score,
        eligible=knockout.eligible,
        extraction_from_cache=extraction.from_cache,
        latency_ms=round((time.perf_counter() - doc_start) * 1000, 1),
        prompt_tokens=sum(u.prompt_tokens for u in out.usages),
        completion_tokens=sum(u.completion_tokens for u in out.usages),
        cost_usd=round(sum(u.cost_usd for u in out.usages), 6),
    )
    return out


def _reprocess(candidate_id: str | None, filename: str, status: str, reason: str) -> ReprocessItem:
    log_event(logger, "resume_needs_reprocess", level=logging.WARNING, candidate_id=candidate_id, status=status)
    return ReprocessItem(candidate_id=candidate_id, source_name=filename, status=status, reason=reason[:500])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_documents(folder: Path) -> list[tuple[str, bytes]]:
    if not folder.is_dir():
        raise SystemExit(f"Not a directory: {folder}")
    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS)
    if not files:
        raise SystemExit(f"No .pdf/.docx/.txt files in {folder}")
    return [(p.name, p.read_bytes()) for p in files]


def format_table(result: ScreeningResult) -> str:
    run = result.run
    lines = [
        f"Job: {result.job_role}  |  extraction={run.extraction_mode}  semantic={run.semantic_backend}"
        f"  |  confirmed by {run.requirements_confirmed.by}",
        "",
        f"RANKED ({len(result.ranked)} eligible)",
        f"{'#':>3}  {'score':>5}  {'file':<28}  top contributions / gaps / N/A",
        "-" * 110,
    ]
    for r in result.ranked:
        top = sorted(r.score_breakdown.contributions.items(), key=lambda kv: -kv[1])[:3]
        contributions = ", ".join(f"{n}={p:.1f}" for n, p in top)
        lines.append(f"{r.rank:>3}  {r.score:>5.1f}  {r.source_name[:28]:<28}  {contributions}")
        for st in r.explanation.strengths:
            lines.append(f'{"":>39}+ {st.text}  ["{st.quote[:70]}"]')
        for gap in r.explanation.gaps:
            lines.append(f"{'':>39}- {gap.text}")
        for warning in r.explanation.warnings:
            lines.append(f"{'':>39}! {warning}")
        if r.score_breakdown.not_applicable:
            lines.append(f"{'':>39}N/A: {', '.join(r.score_breakdown.not_applicable)}")
    lines += ["", f"INELIGIBLE ({len(result.ineligible)}, not ranked)", "-" * 110]
    for r in result.ineligible:
        reasons = "; ".join(x.detail for x in r.knockout.reasons)
        lines.append(f"     {r.score:>5.1f}  {r.source_name[:28]:<28}  {reasons}")
    lines += ["", f"NEEDS REPROCESSING ({len(result.needs_reprocess)}, not scored)", "-" * 110]
    lines.extend(f"            {i.source_name[:28]:<28}  {i.status}: {i.reason}" for i in result.needs_reprocess)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m engine.pipeline", description="Screen and rank resumes for a job.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--jd", type=Path, help="Job description text file (requirements are extracted from it)")
    source.add_argument("--requirements", type=Path, help="Reviewed JobRequirements JSON (with requirements_confirmed)")
    parser.add_argument("--resumes", type=Path, required=True, help="Folder of .pdf/.docx/.txt resumes")
    parser.add_argument("--confirmed-by", help="Name of the person who reviewed the extracted requirements")
    parser.add_argument("--json", type=Path, help="Also write the full result as JSON to this path")
    parser.add_argument("--today", type=date.fromisoformat, help="Reference date for 'present' (YYYY-MM-DD)")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    if args.requirements:
        requirements = JobRequirements.model_validate_json(args.requirements.read_text(encoding="utf-8"))
        if args.confirmed_by:
            requirements = requirements.confirm(args.confirmed_by)
    else:
        analysis = extract_job_requirements(args.jd.read_text(encoding="utf-8"), get_llm(settings), settings=settings)
        requirements = analysis.requirements
        if not args.confirmed_by:
            print(requirements.model_dump_json(indent=2))
            print(
                '\nReview the extracted requirements above, then re-run with --confirmed-by "<your name>" '
                "(or save, edit and pass them with --requirements).",
                file=sys.stderr,
            )
            return 2
        requirements = requirements.confirm(args.confirmed_by)

    try:
        result = screen(requirements, load_documents(args.resumes), settings=settings, today=args.today)
    except RequirementsNotConfirmed as exc:
        print(f"Refusing to screen: {exc}", file=sys.stderr)
        return 2
    print(format_table(result))
    if args.json:
        args.json.write_text(json.dumps(result.model_dump(mode="json"), indent=2), encoding="utf-8")
        print(f"\nFull result written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
