"""Fact extraction from resumes and job descriptions.

Two paths produce the same schema:

* LLM (Azure OpenAI structured outputs, temperature 0) — resume text is
  fenced as untrusted data in the prompt.
* Rule-based (regex + skill ontology) — offline mode.

The mode is chosen once per run (EXTRACTION_MODE) and never mixed in a batch;
an LLM failure after retries marks the resume ``extraction_failed``.

Both outputs go through verification: an item survives only if its
``evidence`` quote occurs in the source text (whitespace/case/dash/quote
normalized) AND the quote supports the claim (a skill's quote mentions that
skill; a job's quote contains its title and its dates are the date range
nearest its title line, within DATE_WINDOW_LINES lines, each range claimed
by only one job). The LLM never scores; it only extracts.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal

from engine.config import Settings, get_settings
from engine.llm import LLMProvider, LLMRetriesExhausted, parse_with_retry
from engine.logging_setup import log_event
from engine.normalize import (
    DATE_RANGE_RE,
    DATE_RE,
    SENIORITY_NAMES,
    SkillOntology,
    is_present,
    is_year_only,
    load_ontology,
    parse_date,
    title_to_seniority,
)
from engine.prompts import fence_untrusted, get_prompt
from engine.schemas import (
    CandidateExtraction,
    CertificationItem,
    DroppedItem,
    DropReason,
    EducationItem,
    ExtractedJobRequirements,
    ExtractionResult,
    FieldEvidenceStats,
    JobAnalysisResult,
    JobItem,
    JobRequirements,
    LocationItem,
    NoticePeriodItem,
    ProjectItem,
    RequirementSkill,
    SkillItem,
    evidence_stats_summary,
)

logger = logging.getLogger("engine.extraction")
# Part of the extraction cache key: bump when verification changes what a cached result contains,
# so results verified by older rules are recomputed. 2: notice days follow the quote.
VERIFIER_VERSION = 2

MIN_EVIDENCE_CHARS = 3

# ---------------------------------------------------------------------------
# Evidence verification
# ---------------------------------------------------------------------------

_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"), "-")
_QUOTES = {
    **dict.fromkeys(map(ord, "\u2018\u2019\u201a\u201b\u2032"), "'"),
    **dict.fromkeys(map(ord, "\u201c\u201d\u201e\u201f\u2033"), '"'),
}
# Job dates may sit up to this many (non-empty) lines above or below the title line.
DATE_WINDOW_LINES = 3


def normalize_for_match(text: str) -> str:
    """Canonical form used for evidence matching: NFKC, casefolded, unified dashes/quotes, collapsed whitespace."""
    text = unicodedata.normalize("NFKC", text).translate(_DASHES).translate(_QUOTES).casefold()
    return " ".join(text.split())


def _contains(haystack_norm: str, needle: str | None) -> bool:
    """Token-bounded match, so a claim like the skill "r" is not satisfied by any word containing "r"."""
    if not needle:
        return False
    term = normalize_for_match(needle)
    return bool(term) and re.search(r"(?<![\w+#])" + re.escape(term) + r"(?![\w+#])", haystack_norm) is not None


@dataclass(frozen=True)
class _DateRange:
    line: int  # index into the verifier's non-empty source lines
    start: str
    end: str

    def matches(self, start: str | None, end: str | None) -> bool:
        """True if a job's claimed dates are this range (compared as parsed months)."""
        if start is not None and (parse_date(start) is None or parse_date(start) != parse_date(self.start)):
            return False
        if end is None:
            return True
        if is_present(end) or is_present(self.end):
            return is_present(end) and is_present(self.end)
        claimed = parse_date(end, is_end=True)
        return claimed is not None and claimed == parse_date(self.end, is_end=True)


class _Verifier:
    def __init__(self, source_text: str, ontology: SkillOntology) -> None:
        self.source = normalize_for_match(source_text)
        # Wrapped lines already join (whitespace is collapsed); also accept end-of-line hyphenation
        # removed ("serv-\ning" -> "serving") or kept as a real hyphen ("self-\nserve" -> "self-serve").
        self.sources = {
            self.source,
            normalize_for_match(re.sub(r"(\w)-[ \t]*\n[ \t]*(\w)", r"\1\2", source_text)),
            normalize_for_match(re.sub(r"-[ \t]*\n[ \t]*", "-", source_text)),
        }
        self.lines = [normalize_for_match(ln) for ln in source_text.split("\n") if ln.strip()]
        # Ranges are read from the original (not normalized) lines, so dates copied from them stay verbatim.
        raw_lines = [ln.strip() for ln in source_text.split("\n") if ln.strip()]
        self.ranges = [
            _DateRange(line=i, start=m.group("start"), end=m.group("end"))
            for i, line in enumerate(raw_lines)
            for m in DATE_RANGE_RE.finditer(line)
        ]
        self.ontology = ontology
        self.dropped: list[DroppedItem] = []
        self.stats: dict[str, FieldEvidenceStats] = {}

    def record(self, field: str, reason: DropReason | None, evidence: str | None) -> bool:
        """Count one item for ``field``; a non-None ``reason`` means it is dropped. Returns "kept"."""
        stats = self.stats.setdefault(field, FieldEvidenceStats(total=0, dropped=0))
        stats.total += 1
        if reason is None:
            return True
        stats.dropped += 1
        self.dropped.append(DroppedItem(field=field, reason=reason, evidence=(evidence or "")[:200]))
        return False

    def check(
        self, evidence: str | None, claims: Iterable[str | None] = (), any_of: Iterable[str] = ()
    ) -> DropReason | None:
        """Why the evidence fails, or None if it is in the source and supports every claim.

        ``claims``: strings that must each appear in the evidence.
        ``any_of``: if non-empty, at least one of these must appear in the evidence.
        """
        ev_norm = normalize_for_match(evidence or "")
        alts = [a for a in any_of if a]
        if len(ev_norm) < MIN_EVIDENCE_CHARS:
            return "evidence_too_short"
        if not self.in_source(ev_norm):
            return "evidence_not_found"
        if any(c and not _contains(ev_norm, c) for c in claims):
            return "evidence_mismatch"
        if alts and not any(_contains(ev_norm, a) for a in alts):
            return "evidence_mismatch"
        return None

    def keep(
        self, field: str, evidence: str | None, claims: Iterable[str | None] = (), any_of: Iterable[str] = ()
    ) -> bool:
        return self.record(field, self.check(evidence, claims, any_of), evidence)

    def in_source(self, text_norm: str) -> bool:
        return any(text_norm in variant for variant in self.sources)

    def date_near(self, anchor: int, date_text: str) -> bool:
        """True if a single date equal to ``date_text`` (as a month) appears within the window of ``anchor``."""
        wanted = parse_date(date_text)
        if wanted is None:
            return False
        lo, hi = max(0, anchor - DATE_WINDOW_LINES), anchor + DATE_WINDOW_LINES + 1
        return any(parse_date(m.group(0)) == wanted for line in self.lines[lo:hi] for m in DATE_RE.finditer(line))

    def anchor_line(self, job: JobItem) -> int | None:
        """Index of the source line holding this job's title, preferring lines inside its evidence quote."""
        title_lines = [i for i, line in enumerate(self.lines) if _contains(line, job.title)]
        if not title_lines:
            return None
        ev = normalize_for_match(job.evidence)
        ev_lines = [i for i, line in enumerate(self.lines) if line and (line in ev or ev in line)]
        if not ev_lines:
            return title_lines[0]
        return min(title_lines, key=lambda t: (min(abs(t - e) for e in ev_lines), t))

    def verified_lines(self, field: str, text: str | None) -> str | None:
        """Keep only the lines of ``text`` that occur verbatim (normalized) in the source; count the rest."""
        if not text:
            return text
        kept = []
        for line in text.split("\n"):
            stripped = _BULLET_RE.sub("", line).strip()
            if not stripped:
                continue
            ok = self.in_source(normalize_for_match(stripped))
            self.record(field, None if ok else "evidence_not_found", stripped)
            if ok:
                kept.append(stripped)
        return "\n".join(kept) or None

    def supports_skill(self, evidence: str | None, name: str) -> bool:
        """True if the quote mentions the skill, using the same matcher as extraction (aliases, inflected
        forms like "fine-tuned"; ambiguous aliases such as "R" only as standalone list items)."""
        if not evidence:
            return False
        canonical = self.ontology.canonicalize(name)
        if canonical is None:
            return _contains(normalize_for_match(evidence), name)
        return any(m.canonical == canonical for m in self.ontology.find_mentions(evidence, evidence))

    def keep_skill(self, field: str, evidence: str | None, name: str) -> bool:
        reason = self.check(evidence)
        if reason is None and not self.supports_skill(evidence, name):
            reason = "evidence_mismatch"
        return self.record(field, reason, evidence)


@dataclass
class Verification:
    dropped: list[DroppedItem]
    stats: dict[str, FieldEvidenceStats]
    # Jobs (indices into the verified job list) whose dates came from the minority side of the layout.
    low_confidence_jobs: list[int] = field(default_factory=list)


def verify_candidate(
    extraction: CandidateExtraction, source_text: str, ontology: SkillOntology | None = None
) -> tuple[CandidateExtraction, Verification]:
    """Drop every item whose evidence is not a quote from ``source_text`` or does not support the item."""
    v = _Verifier(source_text, ontology or load_ontology())

    skills = [s for s in extraction.skills if v.keep_skill("skills", s.evidence, s.name)]
    jobs, low_confidence_jobs = _verify_jobs(v, extraction.jobs)
    projects = [p for p in extraction.projects if v.keep("projects", p.evidence)]
    # Descriptions feed relevance features, so only lines that occur in the resume are kept.
    jobs = [
        j.model_copy(update={"description": v.verified_lines("job_description_lines", j.description)}) for j in jobs
    ]
    projects = [
        p.model_copy(update={"description": v.verified_lines("project_description_lines", p.description)})
        for p in projects
    ]
    education = [e for e in extraction.education if v.keep("education", e.evidence, claims=[e.degree])]
    certifications = [c for c in extraction.certifications if v.keep("certifications", c.evidence, claims=[c.name])]
    location = extraction.location
    if location and not v.keep("location", location.evidence, claims=[location.value]):
        location = None
    notice = extraction.notice_period_days
    if notice and not v.keep("notice_period_days", notice.evidence):
        notice = None
    if notice:
        # Models copy the number but drop the unit ("3 months" -> 3). A quote that states a duration is authoritative.
        stated = _rule_notice(notice.evidence)
        if stated is not None and stated.days != notice.days:
            notice = notice.model_copy(update={"days": stated.days})

    verified = CandidateExtraction(
        skills=_dedupe_skills(skills, v.ontology),
        jobs=jobs,
        projects=projects,
        education=education,
        certifications=certifications,
        location=location,
        notice_period_days=notice,
        summary=_safe_summary(extraction.summary),
    )
    return verified, Verification(dropped=v.dropped, stats=v.stats, low_confidence_jobs=low_confidence_jobs)


def detect_date_layout(anchors: list[int], ranges: list[_DateRange]) -> Literal["above", "below"]:
    """Whether this resume puts date ranges above or below job titles (majority vote).

    Each title votes for the side of its nearest range within the window; a range on the title's own line,
    or equidistant ranges on both sides, count as neutral. Ties (including no votes) default to "below".
    """
    votes = {"above": 0, "below": 0}
    for anchor in set(anchors):
        offsets = [r.line - anchor for r in ranges if abs(r.line - anchor) <= DATE_WINDOW_LINES]
        if not offsets or 0 in offsets:
            continue
        nearest = min(abs(o) for o in offsets)
        sides = {"below" if o > 0 else "above" for o in offsets if abs(o) == nearest}
        if len(sides) == 1:
            votes[sides.pop()] += 1
    return "above" if votes["above"] > votes["below"] else "below"


def _is_partial(job: JobItem) -> bool:
    """Start only ("Jan 2022 -"), or a single year ("2022")."""
    if job.start is None:
        return False
    return job.end is None or (is_year_only(job.start) and job.end.strip() == job.start.strip())


def _verify_jobs(v: _Verifier, jobs: list[JobItem]) -> tuple[list[JobItem], list[int]]:
    """Keep jobs whose quote contains the title and whose dates are verified near the title.

    Date ranges may sit up to DATE_WINDOW_LINES lines from the title. The resume's layout (dates above
    or below titles) is detected by majority vote and applied to every job. Titles are processed in
    document order; each takes the earliest unclaimed range on its own line or on the layout side
    (this also handles two-column layouts where all dates come out as one block). Each range can be
    claimed by only one job, and a job is kept only if its dates equal the range it was given.

    Partial dates (a start with no end, or a single year) are kept when that date appears near the
    title; normalization then infers the interval with low confidence.
    """
    reasons: list[DropReason | None] = [v.check(job.evidence, claims=[job.title]) for job in jobs]
    anchors: dict[int, int] = {}
    for k, job in enumerate(jobs):
        if reasons[k] is None and (job.start is not None or job.end is not None):
            anchor = v.anchor_line(job)
            if anchor is not None:
                anchors[k] = anchor
    layout = detect_date_layout(list(anchors.values()), v.ranges)

    assigned: dict[int, _DateRange] = {}
    claimed: set[int] = set()
    in_order = sorted(anchors, key=lambda k: (anchors[k], k))
    minority_side: set[int] = set()
    # Pass 1: a range on the title's own line belongs to that title.
    for k in in_order:
        for r, dr in enumerate(v.ranges):
            if dr.line == anchors[k] and r not in claimed:
                assigned[k] = dr
                claimed.add(r)
                break
    # Pass 2: titles without one take the earliest unclaimed range on the layout side. A job with partial
    # dates only takes a range that agrees with them, so it cannot take a neighbour's range.
    for k in in_order:
        if k in assigned:
            continue
        anchor = anchors[k]
        side = sorted(
            (
                r
                for r, dr in enumerate(v.ranges)
                if (
                    0 < dr.line - anchor <= DATE_WINDOW_LINES
                    if layout == "below"
                    else 0 < anchor - dr.line <= DATE_WINDOW_LINES
                )
            ),
            key=lambda r: v.ranges[r].line,
        )
        for r in side:
            if r in claimed or (_is_partial(jobs[k]) and not v.ranges[r].matches(jobs[k].start, None)):
                continue
            assigned[k] = v.ranges[r]
            claimed.add(r)
            break

    # Pass 3 (mixed layouts): a title still without matching dates may take an unclaimed range on the
    # other side, nearest first, but only one equal to its own dates. Such jobs are low confidence.
    for k in in_order:
        if k in assigned and assigned[k].matches(jobs[k].start, jobs[k].end):
            continue
        if k in assigned:  # release the non-matching claim from pass 2
            claimed.discard(v.ranges.index(assigned.pop(k)))
        anchor = anchors[k]
        minority = sorted(
            (
                r
                for r, dr in enumerate(v.ranges)
                if (anchor - dr.line > 0 if layout == "below" else dr.line - anchor > 0)
                and abs(dr.line - anchor) <= DATE_WINDOW_LINES
            ),
            key=lambda r: abs(v.ranges[r].line - anchor),
        )
        for r in minority:
            if r not in claimed and v.ranges[r].matches(jobs[k].start, jobs[k].end):
                assigned[k] = v.ranges[r]
                claimed.add(r)
                minority_side.add(k)
                break

    kept: list[JobItem] = []
    low_confidence: list[int] = []  # indices into ``kept``
    for k, job in enumerate(jobs):
        reason = reasons[k]
        if reason is None and (job.start is not None or job.end is not None):
            date_range = assigned.get(k)
            if date_range is not None and date_range.matches(job.start, job.end):
                if job.end is None:  # the source states the end; use it instead of inferring one
                    job = job.model_copy(update={"end": date_range.end})
            elif not (k in anchors and _is_partial(job) and v.date_near(anchors[k], job.start or "")):
                reason = "dates_not_verified"
        if v.record("jobs", reason, job.evidence):
            if k in minority_side:
                low_confidence.append(len(kept))
            kept.append(job)
    return kept, low_confidence


def verify_job_requirements(
    req: ExtractedJobRequirements, source_text: str, ontology: SkillOntology | None = None
) -> tuple[JobRequirements, Verification]:
    """Evidence-check JD skills (scalars such as role/locations are reviewed by HR before screening)."""
    v = _Verifier(source_text, ontology or load_ontology())

    def verified(field: str, skills: list[RequirementSkill]) -> list[RequirementSkill]:
        out = []
        for s in skills:
            if not v.keep_skill(field, s.evidence, s.name):
                continue
            # Keep only alternatives the quote actually offers ("Azure or AWS").
            alts = [a for a in s.alternatives if v.supports_skill(s.evidence, a)]
            out.extend(_atomic_skills(s.model_copy(update={"alternatives": alts}), v.ontology))
        return out

    required = _unique_skills(verified("required_skills", req.required_skills))
    required_names = {s.name.casefold() for s in required}
    preferred = [
        s
        for s in _unique_skills(verified("preferred_skills", req.preferred_skills))
        if s.name.casefold() not in required_names
    ]
    data = req.model_dump(include=set(ExtractedJobRequirements.model_fields))
    data.update(required_skills=required, preferred_skills=preferred)
    return JobRequirements(**data), Verification(dropped=v.dropped, stats=v.stats)


def _atomic_skills(skill: RequirementSkill, ontology: SkillOntology) -> list[RequirementSkill]:
    """A requirement line returned as one "skill" ("Strong Python and SQL") becomes its ontology skills.

    Small models often copy whole JD lines. A name the ontology knows, or one with no known skill in it,
    is kept as is. A years-of-experience line is the experience requirement (scored by
    relevant_experience), not a skill, as in the rule-based extractor.
    """
    if ontology.canonicalize(skill.name):
        return [skill]
    if _YEARS_RE.search(skill.name) and re.search(r"experience", skill.name, re.IGNORECASE):
        return []
    groups = _requirement_groups(skill.name, ontology)
    if not groups:
        return [skill]
    return [
        RequirementSkill(name=name, alternatives=alternatives, evidence=skill.evidence)
        for name, *alternatives in groups
    ]


def _unique_skills(skills: list[RequirementSkill]) -> list[RequirementSkill]:
    seen: set[str] = set()
    out = []
    for s in skills:
        if s.name.casefold() not in seen:
            seen.add(s.name.casefold())
            out.append(s)
    return out


def _dedupe_skills(skills: list[SkillItem], ontology: SkillOntology) -> list[SkillItem]:
    seen: set[str] = set()
    out: list[SkillItem] = []
    for s in skills:
        key = (ontology.canonicalize(s.name) or s.name).casefold()
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


_PII_HINT = re.compile(r"@|https?://|www\.|\+?\d[\d\s().-]{7,}\d")


def _safe_summary(summary: str | None) -> str | None:
    """The summary is unverifiable free text: discard it if it looks like it carries contact details."""
    if not summary or _PII_HINT.search(summary):
        return None
    return summary.strip()[:500] or None


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


class ExtractionFailed(RuntimeError):
    """The LLM could not produce an extraction after all retries."""


def extract_candidate(
    text: str,
    llm: LLMProvider | None,
    settings: Settings | None = None,
    ontology: SkillOntology | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ExtractionResult:
    """Extract a verified CandidateExtraction.

    ``llm=None`` selects rule-based extraction; otherwise the LLM is used exclusively (the mode is a
    run-level choice, never mixed in one batch). Once retries are exhausted the result has
    ``status="extraction_failed"`` and must be reprocessed; it is never silently re-extracted by rules.
    """
    settings = settings or get_settings()
    ontology = ontology or load_ontology()
    if not text.strip():
        raise ValueError("Cannot extract from empty resume text")

    truncated = len(text) > settings.max_resume_chars
    if truncated:
        log_event(logger, "resume_truncated", level=logging.WARNING, chars=len(text), limit=settings.max_resume_chars)
        text = text[: settings.max_resume_chars]

    start = time.perf_counter()
    prompt = get_prompt("resume_extraction")
    method: Literal["llm", "rules"] = "llm" if llm is not None else "rules"
    llm_meta = (
        {"prompt_name": prompt.name, "prompt_version": prompt.version, "model": llm.model} if llm is not None else {}
    )

    if llm is not None:
        try:
            result = parse_with_retry(
                llm,
                system=prompt.system,
                user=prompt.render_user(document=fence_untrusted(text)),
                schema=CandidateExtraction,
                settings=settings,
                sleep=sleep,
            )
        except LLMRetriesExhausted as exc:
            reason = str(exc)[:500]
            log_event(
                logger,
                "candidate_extraction_failed",
                level=logging.ERROR,
                attempts=exc.attempts,
                error=reason,
                latency_ms=round((time.perf_counter() - start) * 1000, 1),
            )
            return ExtractionResult(
                status="extraction_failed",
                extraction=None,
                failure_reason=reason,
                method="llm",
                ontology_version=ontology.version,
                input_truncated=truncated,
                attempts=exc.attempts,
                **llm_meta,
            )
        raw, usage, attempts = result.parsed, result.usage, result.attempts
    else:
        raw, usage, attempts = rule_based_candidate(text, ontology), None, 0

    verified, verification = verify_candidate(raw, text, ontology)
    out = ExtractionResult(
        status="ok",
        extraction=verified,
        method=method,
        ontology_version=ontology.version,
        dropped_items=verification.dropped,
        evidence_stats=verification.stats,
        low_confidence_jobs=verification.low_confidence_jobs,
        input_truncated=truncated,
        attempts=attempts,
        usage=usage,
        **llm_meta,
    )
    log_event(
        logger,
        "candidate_extracted",
        method=method,
        skills=len(verified.skills),
        jobs=len(verified.jobs),
        projects=len(verified.projects),
        dropped_items=len(verification.dropped),
        dropped_reasons=_count_reasons(verification.dropped),
        evidence_drop_rates=out.evidence_drop_rates,
        attempts=attempts,
        latency_ms=round((time.perf_counter() - start) * 1000, 1),
        llm_latency_ms=usage.latency_ms if usage else 0.0,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
        cost_usd=usage.cost_usd if usage else 0.0,
    )
    return out


def extract_job_requirements(
    jd_text: str,
    llm: LLMProvider | None,
    settings: Settings | None = None,
    ontology: SkillOntology | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> JobAnalysisResult:
    """Turn a JD into (unconfirmed) JobRequirements for HR review. ``llm=None`` selects rules.

    Raises ``ExtractionFailed`` if the LLM fails after all retries.
    """
    settings = settings or get_settings()
    ontology = ontology or load_ontology()
    if not jd_text.strip():
        raise ValueError("Cannot extract from empty job description")

    prompt = get_prompt("jd_extraction")
    method: Literal["llm", "rules"] = "llm" if llm is not None else "rules"
    if llm is not None:
        try:
            result = parse_with_retry(
                llm,
                system=prompt.system,
                user=prompt.render_user(document=fence_untrusted(jd_text)),
                schema=ExtractedJobRequirements,
                settings=settings,
                sleep=sleep,
            )
        except LLMRetriesExhausted as exc:
            log_event(logger, "jd_extraction_failed", level=logging.ERROR, attempts=exc.attempts, error=str(exc)[:500])
            raise ExtractionFailed(str(exc)) from exc
        raw, usage, attempts = result.parsed, result.usage, result.attempts
    else:
        raw, usage, attempts = rule_based_job_requirements(jd_text, ontology), None, 0

    verified, verification = verify_job_requirements(raw, jd_text, ontology)
    log_event(
        logger,
        "job_requirements_extracted",
        method=method,
        required=len(verified.required_skills),
        preferred=len(verified.preferred_skills),
        dropped_items=len(verification.dropped),
        evidence_drop_rates=evidence_stats_summary(verification.stats),
        attempts=attempts,
        llm_latency_ms=usage.latency_ms if usage else 0.0,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
        cost_usd=usage.cost_usd if usage else 0.0,
    )
    return JobAnalysisResult(
        requirements=verified,
        method=method,
        prompt_name=prompt.name if llm is not None else None,
        prompt_version=prompt.version if llm is not None else None,
        model=llm.model if llm is not None else None,
        ontology_version=ontology.version,
        dropped_items=verification.dropped,
        evidence_stats=verification.stats,
        attempts=attempts,
        usage=usage,
    )


def _count_reasons(dropped: list[DroppedItem]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in dropped:
        counts[d.reason] = counts.get(d.reason, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Rule-based resume extraction
# ---------------------------------------------------------------------------

_SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "summary": (
        "summary",
        "professional summary",
        "profile",
        "professional profile",
        "about me",
        "objective",
        "career objective",
    ),
    "experience": (
        "experience",
        "work experience",
        "professional experience",
        "employment history",
        "employment",
        "work history",
        "career history",
        "relevant experience",
    ),
    "skills": (
        "skills",
        "technical skills",
        "core skills",
        "key skills",
        "skills & tools",
        "skills and tools",
        "tools",
        "technologies",
        "tech stack",
        "core competencies",
        "competencies",
        # Common in template-based resumes (e.g. livecareer.com), where they hold the skills list.
        "highlights",
        "skill highlights",
        "core qualifications",
        "core strengths",
    ),
    "projects": ("projects", "personal projects", "key projects", "selected projects", "academic projects"),
    "education": ("education", "academic background", "qualifications", "education & training", "academics"),
    "certifications": (
        "certifications",
        "certification",
        "certificates",
        "licenses & certifications",
        "licenses and certifications",
        "certifications & training",
        "courses",
    ),
    "other": (
        "languages",
        "achievements",
        "awards",
        "publications",
        "interests",
        "hobbies",
        "references",
        "contact",
        "personal details",
        "volunteering",
        "additional information",
    ),
}
_HEADER_TO_SECTION = {alias: sec for sec, aliases in _SECTION_ALIASES.items() for alias in aliases}
_HEADER_RE = re.compile(
    r"^\s*(?:#+\s*)?(?P<header>"
    + "|".join(re.escape(a) for a in sorted(_HEADER_TO_SECTION, key=len, reverse=True))
    + r")\s*(?::\s*(?P<rest>.*))?$",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^\s*(?:[-*•·▪●◦]|\d+[.)])\s+")
_TITLE_WORDS = re.compile(
    r"\b(engineer|scientist|analyst|developer|manager|intern|lead|architect|consultant|specialist|director|"
    r"head|researcher|associate|officer|administrator|executive|designer|programmer|trainee|founder)\b",
    re.IGNORECASE,
)
_DEGREE_RE = re.compile(
    r"(?<![A-Za-z])("
    r"ph\.?\s?d\.?|doctorate|m\.?\s?tech|m\.?\s?sc|m\.?\s?s\.?|m\.?\s?e\.?|mba|master(?:'s)?(?: of [a-z]+)?|"
    r"b\.?\s?tech|b\.?\s?sc|b\.?\s?s\.?|b\.?\s?e\.?|b\.?\s?a\.?|bca|mca|bachelor(?:'s)?(?: of [a-z]+)?|"
    r"diploma|associate degree"
    r")(?![A-Za-z])",
    re.IGNORECASE,
)
_INSTITUTION_RE = re.compile(
    r"[^,|\u2013\u2014()]*\b(university|institute|college|school|academy|iit|nit)\b[^,|\u2013\u2014()]*", re.IGNORECASE
)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_LOCATION_RE = re.compile(r"^\s*(?:current\s+)?(?:location|based in)\s*[:\-]\s*(?P<value>[^|\n]+)", re.IGNORECASE)
_NOTICE_RE = re.compile(
    r"notice(?:\s+period)?\s*(?:of|is|:|-)?\s*(?:(?P<immediate>immediate(?:ly)?(?:\s+available)?)|(?P<num>\d+)\s*(?P<unit>days?|weeks?|months?))",
    re.IGNORECASE,
)
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30}


@dataclass
class _Line:
    text: str
    start: int  # char offset in source
    end: int


@dataclass
class _Sections:
    source: str
    lines: dict[str, list[_Line]]

    def get(self, name: str) -> list[_Line]:
        return self.lines.get(name, [])

    def text(self, name: str) -> str:
        return "\n".join(line.text for line in self.get(name))

    def span(self, first: _Line, last: _Line) -> str:
        """Exact source slice covering ``first``..``last`` (keeps evidence verbatim)."""
        return self.source[first.start : last.end]


def split_sections(text: str) -> _Sections:
    sections: dict[str, list[_Line]] = {}
    current = "header"
    offset = 0
    for raw in text.split("\n"):
        start, end = offset, offset + len(raw)
        offset = end + 1
        if not raw.strip():
            continue
        m = _HEADER_RE.match(raw) if len(raw.strip()) <= 60 else None
        if m:
            current = _HEADER_TO_SECTION[m.group("header").lower()]
            rest = m.group("rest")
            if rest and rest.strip():  # inline header, e.g. "Skills: Python, SQL"
                rest_start = start + raw.index(rest)
                sections.setdefault(current, []).append(
                    _Line(rest.strip(), rest_start, rest_start + len(rest.rstrip()))
                )
            continue
        lead = len(raw) - len(raw.lstrip())
        sections.setdefault(current, []).append(_Line(raw.strip(), start + lead, start + lead + len(raw.strip())))
    return _Sections(source=text, lines=sections)


def rule_based_candidate(text: str, ontology: SkillOntology | None = None) -> CandidateExtraction:
    ontology = ontology or load_ontology()
    sec = split_sections(text)

    skills = [
        SkillItem(name=m.canonical, evidence=m.line) for m in ontology.find_mentions(text, sec.text("skills") or None)
    ]
    return CandidateExtraction(
        skills=skills,
        jobs=_rule_jobs(sec),
        projects=_rule_projects(sec, ontology),
        education=_rule_education(sec),
        certifications=_rule_certifications(sec),
        location=_rule_location(text),
        notice_period_days=_rule_notice(text),
        summary=_rule_summary(sec),
    )


@dataclass
class _Slot:
    """A date range (or partial date) on a line of the experience section."""

    idx: int
    start: str
    end: str | None
    text: str  # matched text, removed when reading a title from the same line


# A date at the end of a line, optionally followed by a dash: "Jan 2022 -", "2022".
_PARTIAL_DATE_RE = re.compile(rf"(?P<start>{DATE_RE.pattern})\s*(?:-|\u2013|\u2014|to)?\s*$", re.IGNORECASE)


def _date_slot(i: int, text: str) -> _Slot | None:
    if m := DATE_RANGE_RE.search(text):
        return _Slot(i, m.group("start"), m.group("end"), m.group(0))
    if m := _PARTIAL_DATE_RE.search(text):
        return _Slot(i, m.group("start"), None, m.group(0))
    return None


def _rule_jobs(sec: _Sections) -> list[JobItem]:
    """Pair job titles with date slots: same line first, then by the resume's detected layout.

    Mirrors verification: titles in document order take the earliest unclaimed date slot on the
    layout side (above/below) within DATE_WINDOW_LINES lines; each slot is used once.
    """
    lines = sec.get("experience")
    slots: dict[int, _Slot] = {}
    titles: dict[int, str] = {}
    for i, line in enumerate(lines):
        if _BULLET_RE.match(line.text):
            continue
        slot = _date_slot(i, line.text)
        if slot:
            slots[i] = slot
        head = _strip_seps(line.text.replace(slot.text, " ")) if slot else _strip_seps(line.text)
        if head and _TITLE_WORDS.search(head):
            titles[i] = head

    pairs: dict[int, int] = {t: t for t in titles if t in slots}  # title line -> slot line
    free_titles = [t for t in titles if t not in pairs]
    free_slots = [i for i in slots if i not in titles]
    votes = {"above": 0, "below": 0}
    for t in free_titles:
        offsets = [i - t for i in free_slots if abs(i - t) <= DATE_WINDOW_LINES]
        if offsets:
            nearest = min(abs(o) for o in offsets)
            sides = {"below" if o > 0 else "above" for o in offsets if abs(o) == nearest}
            if len(sides) == 1:
                votes[sides.pop()] += 1
    layout = "above" if votes["above"] > votes["below"] else "below"
    claimed: set[int] = set()
    for t in free_titles:
        side = [
            i
            for i in free_slots
            if i not in claimed
            and (0 < i - t <= DATE_WINDOW_LINES if layout == "below" else 0 < t - i <= DATE_WINDOW_LINES)
        ]
        if side:
            pairs[t] = min(side)
            claimed.add(pairs[t])
    # Mixed layouts: a title still without dates takes the nearest unclaimed slot on the other side
    # (verification then marks the job low confidence).
    for t in free_titles:
        if t in pairs:
            continue
        other = [
            i
            for i in free_slots
            if i not in claimed
            and (0 < t - i <= DATE_WINDOW_LINES if layout == "below" else 0 < i - t <= DATE_WINDOW_LINES)
        ]
        if other:
            pairs[t] = min(other, key=lambda i: abs(i - t))
            claimed.add(pairs[t])

    blocks = sorted((min(t, d), max(t, d), t, d) for t, d in pairs.items())
    jobs: list[JobItem] = []
    for n, (first, last, t, d) in enumerate(blocks):
        title, company = _split_title_company(titles[t])
        if not title:
            continue
        next_first = blocks[n + 1][0] if n + 1 < len(blocks) else len(lines)
        desc_lines = [
            _BULLET_RE.sub("", ln.text)
            for k, ln in enumerate(lines[last + 1 : next_first], start=last + 1)
            if k not in slots and k not in titles
        ]
        jobs.append(
            JobItem(
                title=title,
                company=company,
                start=slots[d].start,
                end=slots[d].end,
                description="\n".join(desc_lines) or None,
                evidence=sec.span(lines[first], lines[last]),
            )
        )
    return jobs


def _strip_seps(s: str) -> str:
    s = re.sub(r"\(\s*\)", " ", s)
    return " ".join(s.split()).strip(" |,–—-·:()\t")


def _split_title_company(text: str) -> tuple[str, str | None]:
    parts = [p.strip() for p in re.split(r"\s+(?:\||–|—|-|@|at)\s+|,\s+", text, maxsplit=1) if p.strip()]
    if not parts:
        return "", None
    if len(parts) == 1:
        return parts[0], None
    first, second = parts
    if _TITLE_WORDS.search(second) and not _TITLE_WORDS.search(first):
        first, second = second, first
    return first, second


def _rule_projects(sec: _Sections, ontology: SkillOntology) -> list[ProjectItem]:
    entries: list[list[_Line]] = []
    started_by_heading = False
    for line in sec.get("projects"):
        is_bullet = bool(_BULLET_RE.match(line.text))
        if is_bullet and entries and started_by_heading:
            entries[-1].append(line)
        else:
            entries.append([line])
            started_by_heading = not is_bullet
    projects: list[ProjectItem] = []
    for entry in entries:
        first = _BULLET_RE.sub("", entry[0].text)
        m = re.match(r"(?P<name>.+?)\s*(?::|–|—|\s-\s)\s*(?P<rest>.*)$", first)
        name, rest = (m.group("name"), m.group("rest")) if m else (first, "")
        body = " ".join([rest] + [_BULLET_RE.sub("", ln.text) for ln in entry[1:]]).strip()
        full = first + " " + body
        projects.append(
            ProjectItem(
                name=name.strip()[:120],
                description=body or None,
                technologies=[m.canonical for m in ontology.find_mentions(full)],
                evidence=entry[0].text,
            )
        )
    return projects


def _rule_education(sec: _Sections) -> list[EducationItem]:
    out: list[EducationItem] = []
    for line in sec.get("education"):
        m = _DEGREE_RE.search(line.text)
        if not m:
            continue
        after = line.text[m.end() :]
        field_m = re.match(r"\s*(?:in|of|-|,)?\s*([A-Za-z &/]+?)(?=\s*(?:,|\||–|—|\(|\bfrom\b|\bat\b|$))", after)
        inst_m = _INSTITUTION_RE.search(line.text)
        years = [y.group(0) for y in _YEAR_RE.finditer(line.text)]
        out.append(
            EducationItem(
                degree=m.group(0).strip(),
                field=(field_m.group(1).strip() or None) if field_m else None,
                institution=inst_m.group(0).strip() if inst_m else None,
                year=years[-1] if years else None,
                evidence=line.text,
            )
        )
    return out


def _rule_certifications(sec: _Sections) -> list[CertificationItem]:
    out: list[CertificationItem] = []
    for line in sec.get("certifications"):
        text = _BULLET_RE.sub("", line.text)
        name = re.split(r"\s*(?:\(|\||–|—|\s-\s|,)\s*", text, maxsplit=1)[0].strip()
        if len(name) < 3:
            continue
        years = [y.group(0) for y in _YEAR_RE.finditer(text)]
        out.append(CertificationItem(name=name, issuer=None, year=years[-1] if years else None, evidence=line.text))
    return out


def _rule_location(text: str) -> LocationItem | None:
    for raw in text.split("\n"):
        for part in raw.split("|"):
            m = _LOCATION_RE.match(part)
            if m:
                value = m.group("value").strip(" .;")
                # Digits suggest a street address / postcode: never extract those.
                if value and not re.search(r"\d", value):
                    return LocationItem(value=value, evidence=part.strip())
    return None


def _rule_notice(text: str) -> NoticePeriodItem | None:
    m = _NOTICE_RE.search(text)
    if not m:
        return None
    days = 0 if m.group("immediate") else int(m.group("num")) * _UNIT_DAYS[m.group("unit").lower().rstrip("s")]
    return NoticePeriodItem(days=days, evidence=m.group(0))


def _rule_summary(sec: _Sections) -> str | None:
    lines = sec.get("summary")
    if not lines:
        return None
    return " ".join(line.text for line in lines[:3])[:300]


# ---------------------------------------------------------------------------
# Rule-based JD extraction
# ---------------------------------------------------------------------------

_REQUIRED_HEADER = re.compile(
    r"\b(requirements?|required|must[- ]haves?|mandatory|qualifications|"
    r"what you(?:'ll)? need|you have|who you are|essential)\b",
    re.IGNORECASE,
)
_PREFERRED_MARK = re.compile(
    r"\b(nice[- ]to[- ]haves?|preferred|bonus|good[- ]to[- ]have|desirable|a plus|optional)\b", re.IGNORECASE
)
_NEUTRAL_HEADER = re.compile(
    r"\b(responsibilities|what you(?:'ll)? do|about (?:us|the role|the team)|benefits|perks|we offer|why join)\b",
    re.IGNORECASE,
)
_ROLE_LABEL = re.compile(r"^\s*(?:job\s+title|title|role|position)\s*[:\-]\s*(?P<v>.+)$", re.IGNORECASE)
_JD_LOCATION = re.compile(r"^\s*(?:location|locations|work location)\s*[:\-]\s*(?P<v>.+)$", re.IGNORECASE)
_YEARS_RE = re.compile(r"(?P<n>\d+(?:\.\d+)?)\s*\+?\s*(?:(?:-|\u2013|to)\s*\d+\s*)?(?:years?|yrs?)", re.IGNORECASE)
_JD_NOTICE = re.compile(
    r"(?:notice\s+period|join)[^.\n]*?(?:of|within|up to|upto|maximum|max|less than|<=?|under)?"
    r"\s*(?P<num>\d+)\s*(?P<unit>days?|weeks?|months?)",
    re.IGNORECASE,
)


_LIST_GAP = re.compile(r"\s*(?:,|/|&|\bor\b|\band\b|,\s*(?:or|and)\b)?\s*", re.IGNORECASE)
_AND_GAP = re.compile(r"\band\b|&", re.IGNORECASE)
_ALT_GAP = re.compile(r"/|\bor\b", re.IGNORECASE)


def _requirement_groups(line: str, ontology: SkillOntology) -> list[list[str]]:
    """Skills on a requirement line; "X or Y" / "X/Y" / "X, Y or Z" become one requirement with alternatives.

    Skills separated by "and" or by commas alone ("Python, SQL", "Python and SQL") stay separate.
    """
    # Ambiguous aliases ("Go", "R") count only as standalone list items in requirement lines.
    low = line.lower()
    located: list[tuple[int, int, str]] = []
    for m in ontology.find_mentions(line, line):
        hit = re.search(r"(?<![\w+#])" + re.escape(m.matched.lower()) + r"(?![\w+#])", low)
        if hit:
            located.append((hit.start(), hit.end(), m.canonical))
    located.sort()

    # Pieces: consecutive mentions joined only by list punctuation, split at "and".
    pieces: list[tuple[list[str], bool]] = []  # (skills, has_alternative_separator)
    for i, (start, _end, canonical) in enumerate(located):
        gap = line[located[i - 1][1] : start] if i else ""
        if i and _LIST_GAP.fullmatch(gap) and not _AND_GAP.search(gap):
            skills, is_alt = pieces[-1]
            pieces[-1] = ([*skills, canonical], is_alt or bool(_ALT_GAP.search(gap)))
        else:
            pieces.append(([canonical], False))

    groups: list[list[str]] = []
    for skills, is_alt in pieces:
        groups.extend([skills] if is_alt else [[s] for s in skills])
    return groups


_EXPERIENCE_DOMAIN = re.compile(
    r"\bexperience\s+(?:in|with|as|working\s+(?:in|on|with))\s+(?:a\s+|an\s+)?(?P<domain>[a-z][a-z /&,+-]*?)"
    r"(?:\s+(?:roles?|positions?|environments?|teams?|fields?))?\s*(?:[.;(]|$)",
    re.IGNORECASE,
)


def rule_based_job_requirements(jd_text: str, ontology: SkillOntology | None = None) -> ExtractedJobRequirements:
    ontology = ontology or load_ontology()
    lines = [ln for ln in jd_text.split("\n") if ln.strip()]

    role = None
    for ln in lines:
        if m := _ROLE_LABEL.match(ln):
            role = m.group("v").strip()
            break
    role = role or lines[0].strip().lstrip("# ").strip()

    required: dict[str, RequirementSkill] = {}
    preferred: dict[str, RequirementSkill] = {}
    mode = "neutral"  # neutral | required | preferred
    for ln in lines:
        stripped = ln.strip().rstrip(":")
        is_heading = len(stripped) <= 60 and not _BULLET_RE.match(ln) and not ontology.find_mentions(stripped)
        if is_heading and _NEUTRAL_HEADER.search(stripped):
            mode = "neutral"
            continue
        if is_heading and _PREFERRED_MARK.search(stripped):
            mode = "preferred"
            continue
        if is_heading and _REQUIRED_HEADER.search(stripped):
            mode = "required"
            continue
        line_mode = "preferred" if _PREFERRED_MARK.search(ln) else mode
        if line_mode == "neutral":
            line_mode = "required" if re.search(r"\b(must|required|need)\b", ln, re.IGNORECASE) else "neutral"
        if line_mode == "neutral":
            continue
        # "5+ years of experience in machine learning" states the experience domain (scored by
        # relevant_experience), not a skill to check off.
        if _YEARS_RE.search(ln) and re.search(r"experience", ln, re.IGNORECASE):
            continue
        target = required if line_mode == "required" else preferred
        for group in _requirement_groups(ln, ontology):
            name, *alternatives = group
            target.setdefault(name, RequirementSkill(name=name, evidence=ln.strip(), alternatives=alternatives))
    for name in required:
        preferred.pop(name, None)

    min_years = None
    domain = None
    for ln in lines:
        if re.search(r"experience|years", ln, re.IGNORECASE) and (m := _YEARS_RE.search(ln)):
            min_years = float(m.group("n"))
            if d := _EXPERIENCE_DOMAIN.search(ln[m.end() :]):
                domain = d.group("domain").strip(" .,;") or None
            break

    locations: list[str] = []
    for ln in lines:
        if m := _JD_LOCATION.match(ln):
            value = m.group("v")
            for part in re.split(r"\s*(?:/|;|\||\bor\b)\s*", value):
                part = re.sub(r"\((?:hybrid|onsite|on-site|remote)\)", "", part, flags=re.IGNORECASE).strip(" .,")
                if part and part.lower() != "remote":
                    locations.append(part)
            if re.search(r"\bremote\b", value, re.IGNORECASE):
                locations.append("Remote")
            break

    max_notice = None
    if m := _JD_NOTICE.search(jd_text):
        max_notice = int(m.group("num")) * _UNIT_DAYS[m.group("unit").lower().rstrip("s")]

    return ExtractedJobRequirements(
        role=role,
        seniority=SENIORITY_NAMES[title_to_seniority(role)],  # type: ignore[arg-type]
        experience_min_years=min_years,
        experience_domain=domain,
        required_skills=list(required.values()),
        preferred_skills=list(preferred.values()),
        locations=locations,
        max_notice_period_days=max_notice,
    )
