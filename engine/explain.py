"""Explanations: at most 4 strengths and 4 gaps per candidate.

* LLM path: the model sees ONLY the feature outputs (name, applicable, value, detail) and their
  verified evidence quotes — never the resume text. Every strength must carry a quote; a strength
  whose quote is not in the evidence list is dropped (post-check), as are strengths about unknown
  or N/A features.
* Template path (offline, or if the LLM call fails after retries): built directly from features.

Gaps always start with the missing required skills and the knockout reasons, computed
deterministically, so they appear whatever the LLM writes. Explanations never affect scores.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from functools import lru_cache
from pathlib import Path

from engine.config import Settings, get_settings
from engine.extraction import normalize_for_match
from engine.llm import LLMProvider, LLMRetriesExhausted, parse_with_retry
from engine.logging_setup import log_event
from engine.prompts import fence_untrusted, get_prompt
from engine.schemas import (
    DroppedStrength,
    Explanation,
    ExplanationDraft,
    FeatureResult,
    Gap,
    KnockoutResult,
    Strength,
)

logger = logging.getLogger("engine.explain")

MAX_ITEMS = 4
MIN_QUOTE_CHARS = 3
STRENGTH_MIN_VALUE = 0.6
GAP_MAX_VALUE = 0.5
# Similarity features have a backend-dependent scale (TF-IDF cosines run far lower than embeddings), so they
# are only called "low" against a threshold calibrated on the golden set for that backend (eval/calibration.py).
SIMILARITY_FEATURES = ("project_relevance", "semantic_relevance")
CALIBRATION_PATH = Path(__file__).with_name("similarity_calibration.json")


@lru_cache(maxsize=1)
def load_similarity_thresholds() -> dict[str, dict[str, float]]:
    """{backend: {feature: threshold}} from the calibration file; empty if not calibrated yet."""
    if not CALIBRATION_PATH.exists():
        return {}
    data = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
    return {backend: entry["thresholds"] for backend, entry in data.get("backends", {}).items()}


FEATURE_LABELS = {
    "required_skill_coverage": "Required skills",
    "relevant_experience": "Relevant experience",
    "project_relevance": "Project relevance",
    "seniority_fit": "Seniority fit",
    "preferred_skill_coverage": "Preferred skills",
    "semantic_relevance": "Overall profile match",
}


def explanation_input(features: Sequence[FeatureResult]) -> str:
    """The only data the LLM sees: feature outputs and their verified evidence quotes."""
    payload = [
        {
            "name": f.name,
            "applicable": f.applicable,
            "value": round(f.value, 2),
            "detail": f.detail,
            "evidence": f.evidence,
        }
        for f in features
    ]
    return json.dumps({"features": payload}, ensure_ascii=False, indent=1)


def explain(
    features: Sequence[FeatureResult],
    knockout: KnockoutResult,
    llm: LLMProvider | None,
    settings: Settings | None = None,
    sleep: Callable[[float], None] = time.sleep,
    warnings: Sequence[str] = (),
    similarity_backends: dict[str, str] | None = None,
) -> Explanation:
    """Explain one candidate. ``llm=None`` selects templates. ``warnings`` are attached verbatim.

    ``similarity_backends`` maps each similarity feature to the backend that produced it, so templates
    can apply that backend's calibrated "low" threshold.
    """
    fixed_gaps = deterministic_gaps(knockout)
    settings = settings or get_settings()
    if llm is None or settings.explanation_mode == "template":
        return template_explanation(features, fixed_gaps, warnings, similarity_backends)

    prompt = get_prompt("explanation")
    try:
        result = parse_with_retry(
            llm,
            system=prompt.system,
            user=prompt.render_user(document=fence_untrusted(explanation_input(features))),
            schema=ExplanationDraft,
            settings=settings,
            sleep=sleep,
        )
    except LLMRetriesExhausted as exc:
        # Explanations never change scores, so a template is an acceptable, clearly-flagged fallback.
        log_event(logger, "explanation_llm_failed_fallback_to_template", level=logging.ERROR, error=str(exc)[:300])
        fallback = template_explanation(features, fixed_gaps, warnings, similarity_backends)
        return fallback.model_copy(update={"fallback_reason": str(exc)[:500]})

    strengths, dropped = verify_strengths(result.parsed, features)
    applicable = {f.name for f in features if f.applicable}
    llm_gaps: list[Gap] = []
    dropped_gaps: list[str] = []
    for g in result.parsed.gaps:
        if not g.text.strip():
            continue
        if g.feature not in applicable or mentions_career_gap(g.text):
            dropped_gaps.append(g.text[:300])
            continue
        llm_gaps.append(Gap(feature=g.feature, text=g.text.strip(), source="feature"))
    explanation = Explanation(
        strengths=strengths,
        gaps=_merge_gaps(fixed_gaps, llm_gaps),
        warnings=list(warnings),
        method="llm",
        prompt_name=prompt.name,
        prompt_version=prompt.version,
        model=llm.model,
        dropped_strengths=dropped,
        dropped_gaps=dropped_gaps,
        usage=result.usage,
    )
    log_event(
        logger,
        "explanation_generated",
        method="llm",
        strengths=len(strengths),
        gaps=len(explanation.gaps),
        dropped_strengths=len(dropped),
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        cost_usd=result.usage.cost_usd,
    )
    return explanation


# ---------------------------------------------------------------------------
# Post-checks
# ---------------------------------------------------------------------------


def verify_strengths(
    draft: ExplanationDraft, features: Sequence[FeatureResult]
) -> tuple[list[Strength], list[DroppedStrength]]:
    """Keep strengths about applicable features whose quote occurs in the verified evidence list."""
    applicable = {f.name for f in features if f.applicable}
    evidence = [normalize_for_match(q) for f in features if f.applicable for q in f.evidence]
    kept: list[Strength] = []
    dropped: list[DroppedStrength] = []
    seen_quotes: set[str] = set()
    for s in draft.strengths:
        quote_norm = normalize_for_match(s.quote)
        reason = None
        if s.feature not in applicable:
            reason = "unknown_or_na_feature"
        elif mentions_career_gap(s.text):
            reason = "mentions_career_gap"
        elif len(quote_norm) < MIN_QUOTE_CHARS or not any(quote_norm in ev for ev in evidence):
            reason = "quote_not_in_evidence"
        elif quote_norm in seen_quotes:
            reason = "duplicate"
        elif len(kept) >= MAX_ITEMS:
            reason = "over_limit"
        if reason:
            dropped.append(DroppedStrength(text=s.text[:300], quote=s.quote[:300], reason=reason))  # type: ignore[arg-type]
            continue
        seen_quotes.add(quote_norm)
        kept.append(Strength(feature=s.feature, text=s.text.strip(), quote=s.quote.strip()))
    return kept, dropped


# Time between jobs is never a signal: it can reflect caregiving, health or other protected circumstances.
_CAREER_GAP = re.compile(
    r"\b(?:career|employment|work|job|resume|cv)\s+(?:gap|break|hiatus)s?\b"
    r"|\bgaps?\s+(?:in|between)\s+(?:(?:his|her|their)\s+|the\s+candidate'?s?\s+)?(?:employment|jobs?|roles?|career|work|experience)\b"
    r"|\b(?:break|time)\s+(?:off|away)\s+from\s+(?:work|employment)\b|\bunemploy|\bout\s+of\s+work\b"
    r"|\b(?:\d+|one|two|three|four|five|several|multi)[- ](?:year|month)s?\s+(?:gap|break)\b",
    re.IGNORECASE,
)


def mentions_career_gap(text: str) -> bool:
    return _CAREER_GAP.search(text) is not None


def deterministic_gaps(knockout: KnockoutResult) -> list[Gap]:
    """Missing required skills (always, even for eligible candidates) and knockout reasons."""
    gaps: list[Gap] = []
    if knockout.missing_required_skills:
        gaps.append(
            Gap(
                feature="required_skill_coverage",
                text=f"Missing required skills: {', '.join(knockout.missing_required_skills)}",
                source="missing_required_skills",
            )
        )
    for reason in knockout.reasons:
        if reason.rule != "required_skills":  # already covered by the missing-skills gap
            gaps.append(Gap(feature=None, text=f"Knockout: {reason.detail}", source="knockout"))
    return gaps


def _merge_gaps(fixed: list[Gap], extra: list[Gap]) -> list[Gap]:
    """Deterministic gaps first, then others up to MAX_ITEMS; at most one extra gap per feature."""
    gaps = list(fixed[:MAX_ITEMS])
    if len(fixed) > MAX_ITEMS:  # keep every knockout visible by folding the overflow into the last slot
        gaps[-1] = Gap(feature=None, text="; ".join(g.text for g in fixed[MAX_ITEMS - 1 :]), source="knockout")
    covered = {g.feature for g in gaps if g.source == "feature"}
    has_missing_skills = any(g.source == "missing_required_skills" for g in gaps)
    for gap in extra:
        if len(gaps) >= MAX_ITEMS:
            break
        if gap.feature in covered or (has_missing_skills and gap.feature == "required_skill_coverage"):
            continue
        covered.add(gap.feature)
        gaps.append(gap)
    return gaps


# ---------------------------------------------------------------------------
# Templates (offline)
# ---------------------------------------------------------------------------


def _summary(detail: str) -> str:
    """First sentence of a feature's detail, for human-readable text."""
    head = detail.split(". ")[0]
    return head if len(head) <= 160 else head[:157] + "..."


def _is_low(f: FeatureResult, similarity_backends: dict[str, str] | None) -> tuple[bool, str]:
    """Whether a feature counts as a gap, and how to describe its threshold."""
    if f.name not in SIMILARITY_FEATURES:
        return f.value < GAP_MAX_VALUE, ""
    backend = (similarity_backends or {}).get(f.name)
    threshold = load_similarity_thresholds().get(backend or "", {}).get(f.name)
    if threshold is None:  # uncalibrated backend: never call it low
        return False, ""
    return f.value < threshold, f" (below the calibrated {threshold:.2f} for {backend})"


def template_explanation(
    features: Sequence[FeatureResult],
    fixed_gaps: list[Gap],
    warnings: Sequence[str] = (),
    similarity_backends: dict[str, str] | None = None,
) -> Explanation:
    applicable = [f for f in features if f.applicable]
    strengths = [
        Strength(
            feature=f.name,
            text=f"{FEATURE_LABELS.get(f.name, f.name)}: {_summary(f.detail)}",
            quote=f.evidence[0],
        )
        for f in sorted(applicable, key=lambda f: -f.value)
        if f.value >= STRENGTH_MIN_VALUE and f.evidence
    ][:MAX_ITEMS]
    feature_gaps = []
    for f in sorted(applicable, key=lambda f: f.value):
        low, note = _is_low(f, similarity_backends)
        if low:
            label = FEATURE_LABELS.get(f.name, f.name)
            feature_gaps.append(
                Gap(feature=f.name, text=f"{label} is low{note}: {_summary(f.detail)}", source="feature")
            )
    explanation = Explanation(
        strengths=strengths, gaps=_merge_gaps(fixed_gaps, feature_gaps), warnings=list(warnings), method="template"
    )
    log_event(logger, "explanation_generated", method="template", strengths=len(strengths), gaps=len(explanation.gaps))
    return explanation
