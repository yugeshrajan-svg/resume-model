"""Structured candidate facts with located evidence, support levels, warnings, conflicts and review items.

Input: a *verified* ``ExtractionResult`` (LLM or rules; every item's quote was already checked against the
source by ``engine.extraction``) plus the layout of the same text. Output: a strictly validated, versioned
``FactSet``. The fact set is derived data for retrieval and review; screening never reads it, so building or
rebuilding facts cannot change a screening result.

Rules (FACTS_POLICY_VERSION):

* Every fact carries evidence located in the exact resume version (offsets, line, page, section).
* Missing information is ``unknown``, never a default; nothing is inferred about protected characteristics,
  personality, honesty, health or cultural fit, and lines stating protected attributes are never used.
* A skill is ``demonstrated`` when it is named in a work or project entry, ``listed`` when it appears in a
  skills section, otherwise ``mentioned``. These are support levels, not proficiency or confidence.
* Confidence is never taken from a model. Each fact's ``confidence`` is a rule outcome: ``verified`` (quote
  found and supports the claim), ``verified_with_warnings`` (e.g. year-only dates), or ``conflicting``.
* Contradictory statements (two notice periods, two cities, one role with two date ranges) are kept side by
  side as conflicts and produce review items; no value is silently chosen.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from engine.extraction import VERIFIER_VERSION, normalize_for_match
from engine.normalize import (
    DATE_RANGE_RE,
    SENIORITY_NAMES,
    SkillOntology,
    load_ontology,
    location_matches,
    normalize_candidate,
    parse_date,
    ym_from_str,
)
from engine.schemas import ExtractionResult, JobItem, NormalizedJob, SeniorityLevel
from retrieval.chunking import EntryAnchor, entry_assignment
from retrieval.layout import DocumentLayout, Line
from retrieval.text import QuoteLocator
from retrieval.versions import FACTS_POLICY_VERSION, FACTS_SCHEMA_VERSION

Confidence = Literal["verified", "verified_with_warnings", "conflicting"]
Severity = Literal["info", "review"]
SeniorityName = Literal["intern", "junior", "mid", "senior", "lead", "principal"]
_QUOTE_LIMIT = 300


class _Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceRef(_Fact):
    quote: str = Field(max_length=_QUOTE_LIMIT)
    char_start: int | None = Field(ge=0)
    char_end: int | None = Field(ge=0)
    line: int | None = Field(ge=0)
    page: int | None = Field(ge=1)
    section: str | None


class SkillFact(_Fact):
    name: str = Field(min_length=1, max_length=120)
    in_ontology: bool
    listed: bool
    demonstrated: bool
    support: Literal["demonstrated", "listed", "mentioned"]
    entries: list[str] = Field(max_length=50)  # entry keys where the skill is demonstrated
    last_used: str | None  # YYYY-MM end of the latest dated role naming the skill
    confidence: Confidence
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=4)


class EmploymentFact(_Fact):
    key: str
    title: str = Field(min_length=1, max_length=200)
    company: str | None = Field(max_length=200)
    start: str | None  # YYYY-MM
    end: str | None  # YYYY-MM (reference month for current roles)
    current: bool
    months: int = Field(ge=0)
    date_confidence: Literal["high", "low"] | None
    date_note: str | None
    seniority: SeniorityName
    skills_used: list[str] = Field(max_length=100)
    confidence: Confidence
    warnings: list[str]
    evidence: EvidenceRef
    responsibilities: list[EvidenceRef] = Field(max_length=60)


class EmploymentOverlap(_Fact):
    a: str
    b: str
    months: int = Field(ge=1)


class ProjectFact(_Fact):
    key: str
    name: str = Field(min_length=1, max_length=200)
    technologies: list[str] = Field(max_length=60)
    confidence: Confidence
    evidence: EvidenceRef
    details: list[EvidenceRef] = Field(max_length=40)


class AccomplishmentFact(_Fact):
    context: str  # entry key ("employment:0", "project:1") or a section name
    metrics: list[str] = Field(min_length=1, max_length=5)
    evidence: EvidenceRef


class EducationFact(_Fact):
    degree: str = Field(min_length=1, max_length=200)
    field: str | None = Field(max_length=200)
    institution: str | None = Field(max_length=300)
    year: str | None = Field(max_length=20)
    evidence: EvidenceRef


class CertificationFact(_Fact):
    name: str = Field(min_length=1, max_length=300)
    issuer: str | None = Field(max_length=200)
    year: str | None = Field(max_length=20)
    evidence: EvidenceRef


class SeniorityFact(_Fact):
    level: SeniorityName | None
    basis: Literal["most_recent_dated_role", "unknown"]
    evidence: EvidenceRef | None
    leadership: list[EvidenceRef] = Field(max_length=10)


class StatedValue(_Fact):
    value: str
    evidence: EvidenceRef


class ValueFact(_Fact):
    status: Literal["stated", "unknown", "conflicting"]
    value: str | None  # the single stated value; None when unknown or conflicting
    values: list[StatedValue] = Field(max_length=10)


class PreferenceFact(_Fact):
    kind: Literal["relocation", "work_mode", "availability", "employment_type", "work_authorization"]
    statement: str = Field(max_length=_QUOTE_LIMIT)
    evidence: EvidenceRef


class FactWarning(_Fact):
    field: str
    code: str
    severity: Severity
    message: str = Field(max_length=400)
    evidence: list[EvidenceRef] = Field(max_length=6)


class FactConflict(_Fact):
    field: str
    values: list[StatedValue] = Field(min_length=2, max_length=10)


class ReviewItem(_Fact):
    key: str
    field: str
    reason: str
    severity: Severity
    message: str = Field(max_length=400)
    evidence: list[EvidenceRef] = Field(max_length=6)


class ExtractionProvenance(_Fact):
    status: Literal["ok", "extraction_failed"]
    method: Literal["llm", "rules"]
    prompt_version: str | None
    model: str | None
    ontology_version: str | None
    verifier_version: int
    attempts: int
    input_truncated: bool
    unsupported_dropped: dict[str, int]  # evidence drops by field (claims rejected by verification)


class FactSet(_Fact):
    schema_version: str
    policy_version: str
    reference_month: str
    extraction: ExtractionProvenance
    total_experience_months: int = Field(ge=0)
    skills: list[SkillFact]
    employment: list[EmploymentFact]
    overlaps: list[EmploymentOverlap]
    projects: list[ProjectFact]
    accomplishments: list[AccomplishmentFact]
    education: list[EducationFact]
    certifications: list[CertificationFact]
    seniority: SeniorityFact
    notice_period_days: ValueFact
    location: ValueFact
    preferences: list[PreferenceFact]
    warnings: list[FactWarning]
    conflicts: list[FactConflict]
    review_items: list[ReviewItem]
    unknown: list[str]  # fields with no stated information

    @property
    def review_required(self) -> bool:
        return any(item.severity == "review" for item in self.review_items)


# ---------------------------------------------------------------------------
# Patterns (candidate-provided statements only)
# ---------------------------------------------------------------------------

METRIC_RE = re.compile(
    r"[$₹€£]\s?\d[\d,.]*\s?(?:k|m|mn|million|b|bn|billion|cr|crore|lakh)?\b"
    r"|\b\d[\d,.]*\s?%"
    r"|\b\d+(?:\.\d+)?\s?(?:x|times)\b"
    r"|\b\d[\d,.]*\s?(?:k|m|million|billion)?\+?\s+(?:users|customers|clients|queries|requests|transactions|records"
    r"|events|downloads|students|patients|employees|people|members|stores|rps|qps|tps|servers|services)\b"
    r"|\b(?:reduced|increased|improved|grew|saved|cut|boosted|decreased|accelerated|raised|lowered)\b[^.\n]{0,60}?"
    r"\b\d[\d,.]*\s?(?:%|x|ms|s|seconds|minutes|hours|days)?",
    re.IGNORECASE,
)
LEADERSHIP_RE = re.compile(
    r"\b(?:led|lead|managed|mentored|supervised|headed|coached|directed|oversaw)\b[^.\n]{0,60}?"
    r"\b(?:team|engineers|developers|people|staff|group|squad|members|interns|analysts|reports)\b"
    r"|\bteam\s+of\s+\d+",
    re.IGNORECASE,
)
NOTICE_RE = re.compile(
    r"\bnotice(?:\s+period)?\s*(?:of|is|:|-)?\s*(?:(?P<immediate>immediate(?:ly)?)|(?P<num>\d{1,3})\s*"
    r"(?P<unit>days?|weeks?|months?))"
    r"|\b(?:available|able|can)\s+to\s+join\s+(?:(?P<immediate2>immediately)|(?:in|after|within)\s+(?P<num2>\d{1,3})"
    r"\s*(?P<unit2>days?|weeks?|months?))",
    re.IGNORECASE,
)
LOCATION_RE = re.compile(r"(?:^|\|)\s*(?:current\s+)?(?:location|based\s+in|city)\s*[:\-]\s*(?P<value>[^|\n]+)", re.I)
CLAIMED_YEARS_RE = re.compile(
    r"\b(?P<n>\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years?|yrs?)\b(?:\s+of)?(?:\s+[\w/+#.-]+){0,4}?\s+experience\b",
    re.IGNORECASE,
)
PREFERENCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(pattern, re.IGNORECASE))
    for kind, pattern in (
        ("relocation", r"\b(?:(?:open|willing|ready)\s+to\s+relocat\w*|not\s+(?:open|willing)\s+to\s+relocat\w*"
                       r"|relocation\s*[:\-]\s*\w+)"),
        ("work_mode", r"\b(?:prefer(?:s|red)?|open\s+to|looking\s+for|seeking|available\s+for)\s+(?:a\s+)?"
                      r"(?:fully\s+)?(?:remote|hybrid|on-?site|in-?office)\b|\b(?:remote|hybrid|on-?site)\s+only\b"
                      r"|\bwork\s+mode\s*[:\-]"),
        ("availability", r"\bavailable\s+(?:from|starting|immediately|to\s+start)\b|\bcan\s+(?:join|start)\s+"
                         r"(?:immediately|from|in|within)\b|\bavailability\s*[:\-]"),
        ("employment_type", r"\b(?:looking\s+for|seeking|open\s+to|prefer(?:s|red)?)\s+(?:a\s+)?(?:full[\s-]?time|"
                            r"part[\s-]?time|contract|freelance|internship|permanent)\b|\b(?:full[\s-]?time|"
                            r"part[\s-]?time|contract|freelance)\s+(?:only|preferred)\b"),
        ("work_authorization", r"\b(?:work\s+authori[sz]ation|authori[sz]ed\s+to\s+work|visa\s+(?:status|sponsorship)"
                               r"|require[sd]?\s+(?:visa\s+)?sponsorship|work\s+permit)\b"),
    )
)  # fmt: skip
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30}


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


class _Evidence:
    """Builds EvidenceRefs from quotes or lines of one layout."""

    def __init__(self, layout: DocumentLayout) -> None:
        self.layout = layout
        self.locator = QuoteLocator(layout.text)

    def ref(self, quote: str, *, start_at: int = 0) -> EvidenceRef:
        span = self.locator.locate(quote, start_at=start_at)
        line = self.layout.line_at(span[0]) if span else None
        return EvidenceRef(
            quote=" ".join(quote.split())[:_QUOTE_LIMIT],
            char_start=span[0] if span else None,
            char_end=span[1] if span else None,
            line=line.index if line else None,
            page=line.page if line else None,
            section=line.section if line else None,
        )

    def of_line(self, line: Line) -> EvidenceRef:
        raw = self.layout.text[line.start : line.end]
        lead = len(raw) - len(raw.lstrip())
        return EvidenceRef(
            quote=line.text[:_QUOTE_LIMIT],
            char_start=line.start + lead,
            char_end=line.start + lead + len(line.text),
            line=line.index,
            page=line.page,
            section=line.section,
        )

    def span_lines(self, ref: EvidenceRef) -> list[Line]:
        if ref.char_start is None or ref.char_end is None:
            return []
        return self.layout.lines_between(ref.char_start, ref.char_end)


def _contains(text: str, term: str) -> bool:
    needle = normalize_for_match(term)
    return bool(needle) and re.search(
        r"(?<![\w+#])" + re.escape(needle) + r"(?![\w+#])", normalize_for_match(text)
    ) is not None  # fmt: skip


def _title_line(candidates: list[Line], item: JobItem, used: set[int]) -> Line | None:
    """The line holding a job's title. "Machine Learning Engineer" also occurs inside "Lead Machine Learning
    Engineer", so lines naming the employer, then lines starting with the title, win; a line already holding
    another job's title is never reused."""
    title = normalize_for_match(item.title)
    scored = []
    for line in candidates:
        if line.index in used or not _contains(line.text, item.title):
            continue
        text = normalize_for_match(line.text)
        score = (2 if item.company and _contains(line.text, item.company) else 0) + (1 if text.startswith(title) else 0)
        scored.append((-score, line.index, line))
    return min(scored, key=lambda s: (s[0], s[1]))[2] if scored else None


def _match_jobs(extracted: list[JobItem], normalized: list[NormalizedJob]) -> list[JobItem]:
    """The extracted JobItem behind each normalized job (normalization reorders jobs, most recent first)."""
    pool = list(extracted)
    out = []
    for job in normalized:
        found = next(j for j in pool if j.evidence == job.evidence and j.title == job.title)
        pool.remove(found)
        out.append(found)
    return out


def _date_line(ev: _Evidence, title_line: Line, job: JobItem) -> Line | None:
    """The line holding the job's verified date range: the title line itself, or one within three lines."""
    if job.start is None:
        return None
    wanted = parse_date(job.start)
    candidates = sorted(
        (line for line in ev.layout.lines if line.text and abs(line.index - title_line.index) <= 3),
        key=lambda line: (abs(line.index - title_line.index), line.index),
    )
    for line in candidates:
        for m in DATE_RANGE_RE.finditer(line.text):
            if wanted is not None and parse_date(m.group("start")) == wanted:
                return line
    return None


def _notice_days(m: re.Match[str]) -> int:
    if m.group("immediate") or m.group("immediate2"):
        return 0
    num, unit = (m.group("num"), m.group("unit")) if m.group("num") else (m.group("num2"), m.group("unit2"))
    return int(num) * _UNIT_DAYS[unit.lower().rstrip("s")]


def build_fact_set(
    extraction: ExtractionResult,
    layout: DocumentLayout,
    reference_date: date,
    ontology: SkillOntology | None = None,
) -> tuple[FactSet, list[EntryAnchor]]:
    """Facts for one resume version, and the entry anchors that keep chunks aligned with them."""
    ontology = ontology or load_ontology()
    ev = _Evidence(layout)
    dropped: dict[str, int] = defaultdict(int)
    for item in extraction.dropped_items:
        dropped[item.field] += 1
    provenance = ExtractionProvenance(
        status=extraction.status,
        method=extraction.method,
        prompt_version=extraction.prompt_version,
        model=extraction.model,
        ontology_version=extraction.ontology_version,
        verifier_version=VERIFIER_VERSION,
        attempts=extraction.attempts,
        input_truncated=extraction.input_truncated,
        unsupported_dropped=dict(dropped),
    )
    warnings: list[FactWarning] = []
    conflicts: list[FactConflict] = []
    reference_month = f"{reference_date.year:04d}-{reference_date.month:02d}"
    usable = [line for line in layout.lines if line.usable]
    if extraction.status != "ok" or extraction.extraction is None:
        warnings.append(
            FactWarning(
                field="extraction",
                code="extraction_failed",
                severity="review",
                message="Facts could not be extracted from this resume version; reprocess it.",
                evidence=[],
            )
        )
        return _finish(provenance, reference_month, warnings=warnings, conflicts=conflicts), []

    data = extraction.extraction
    candidate = normalize_candidate(data, ontology, reference_date, low_confidence_jobs=extraction.low_confidence_jobs)
    if extraction.input_truncated:
        warnings.append(
            FactWarning(
                field="extraction",
                code="input_truncated",
                severity="review",
                message="Only the first part of this resume was read; later facts may be missing.",
                evidence=[],
            )
        )
    if dropped:
        warnings.append(
            FactWarning(
                field="extraction",
                code="unsupported_claims_rejected",
                severity="info",
                message="Extracted items whose quotes did not support them were rejected: "
                + ", ".join(f"{field} {count}" for field, count in sorted(dropped.items())),
                evidence=[],
            )
        )

    # --- Employment -------------------------------------------------------------------------------------
    anchors: list[EntryAnchor] = []
    employment: list[EmploymentFact] = []
    intervals: dict[str, tuple[str, str]] = {}
    used_title_lines: set[int] = set()
    for n, (norm, item) in enumerate(zip(candidate.jobs, _match_jobs(data.jobs, candidate.jobs), strict=True)):
        key = f"employment:{n}"
        evidence = ev.ref(item.evidence)
        title_line = _title_line(ev.span_lines(evidence), item, used_title_lines) or _title_line(
            [ln for ln in usable if ln.section == "experience"], item, used_title_lines
        )
        if title_line is not None and title_line.protected:
            continue  # never build facts on a line that states a protected attribute
        job_warnings = []
        if title_line is None:
            job_warnings.append("evidence_location_unavailable")
        else:
            used_title_lines.add(title_line.index)
            date_line = _date_line(ev, title_line, item)
            header = tuple(sorted({title_line.index, *([date_line.index] if date_line else [])}))
            dates = f"{norm.start or '?'} to {'present' if norm.is_current else norm.end or '?'}"
            label = " | ".join(filter(None, [item.title, item.company, dates if norm.start else None]))
            anchors.append(EntryAnchor(key, "employment", title_line.index, header, label[:200]))
        if norm.date_confidence == "low":
            job_warnings.append("date_low_confidence")
            warnings.append(
                FactWarning(
                    field=key,
                    code="date_low_confidence",
                    severity="review",
                    message=f"Dates for '{item.title}' are approximate: {norm.date_note}",
                    evidence=[evidence],
                )
            )
        elif norm.date_confidence is None:
            job_warnings.append("dates_unknown")
            warnings.append(
                FactWarning(
                    field=key,
                    code="dates_unknown",
                    severity="review",
                    message=f"No parseable dates for '{item.title}'; it does not count toward experience.",
                    evidence=[evidence],
                )
            )
        responsibilities = []
        for text in (item.description or "").split("\n"):
            if text.strip():
                ref = ev.ref(text, start_at=title_line.start if title_line else 0)
                if ref.line is None or not layout.lines[ref.line].protected:
                    responsibilities.append(ref)
        employment.append(
            EmploymentFact(
                key=key,
                title=item.title,
                company=item.company,
                start=norm.start,
                end=norm.end,
                current=norm.is_current,
                months=norm.months,
                date_confidence=norm.date_confidence,
                date_note=norm.date_note,
                seniority=SENIORITY_NAMES[SeniorityLevel(norm.seniority)],  # type: ignore[arg-type]
                skills_used=[],  # filled below from entry assignment
                confidence="verified_with_warnings" if job_warnings else "verified",
                warnings=job_warnings,
                evidence=evidence,
                responsibilities=responsibilities[:60],
            )
        )
        if norm.start and norm.end:
            intervals[key] = (norm.start, norm.end)

    overlaps = []
    keys = sorted(intervals)
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            (sa, ea), (sb, eb) = intervals[a], intervals[b]
            start, end = max(ym_from_str(sa), ym_from_str(sb)), min(ym_from_str(ea), ym_from_str(eb))
            if start <= end:
                overlaps.append(EmploymentOverlap(a=a, b=b, months=end.index - start.index + 1))
    if overlaps:
        warnings.append(
            FactWarning(
                field="employment",
                code="employment_overlap",
                severity="info",
                message=f"{len(overlaps)} pair(s) of roles overlap in time; overlapping months are counted once.",
                evidence=[],
            )
        )
    by_title: dict[tuple[str, str], list[EmploymentFact]] = defaultdict(list)
    for job in employment:
        by_title[(job.title.casefold(), (job.company or "").casefold())].append(job)
    for (_title, _company), jobs in by_title.items():
        ranges = {(j.start, j.end) for j in jobs if j.start}
        if len(ranges) > 1:
            conflicts.append(
                FactConflict(
                    field="employment_dates",
                    values=[StatedValue(value=f"{j.start} to {j.end}", evidence=j.evidence) for j in jobs],
                )
            )

    # --- Projects ----------------------------------------------------------------------------------------
    projects: list[ProjectFact] = []
    for n, item in enumerate(data.projects):
        key = f"project:{n}"
        evidence = ev.ref(item.evidence)
        title_line = layout.lines[evidence.line] if evidence.line is not None else None
        if title_line is None or not title_line.usable:
            continue
        anchors.append(EntryAnchor(key, "project", title_line.index, (title_line.index,), item.name[:200]))
        details = [ev.ref(t, start_at=title_line.start) for t in (item.description or "").split("\n") if t.strip()]
        projects.append(
            ProjectFact(
                key=key,
                name=item.name,
                technologies=item.technologies[:60],
                confidence="verified",
                evidence=evidence,
                details=details[:40],
            )
        )

    owners = entry_assignment(layout, anchors)

    # --- Skills ------------------------------------------------------------------------------------------
    # Ambiguous aliases ("Go", "R") count only on skills-section lines, exactly as in screening extraction.
    line_skills = {
        line.index: {
            m.canonical for m in ontology.find_mentions(line.text, line.text if line.section == "skills" else None)
        }
        for line in usable
    }
    skills: list[SkillFact] = []
    seen: set[str] = set()
    entry_skills: dict[str, set[str]] = defaultdict(set)
    for skill in data.skills:
        canonical = ontology.canonicalize(skill.name)
        name = canonical or " ".join(skill.name.split())
        if name.casefold() in seen:
            continue
        seen.add(name.casefold())
        in_ontology = canonical is not None
        hits = [
            line for line in usable if (name in line_skills[line.index] if in_ontology else _contains(line.text, name))
        ]
        listed = any(line.section == "skills" for line in hits)
        demo_lines = [line for line in hits if line.index in owners]
        entries = sorted({owners[line.index] for line in demo_lines})
        for entry in entries:
            entry_skills[entry].add(name)
        normalized = next((s for s in candidate.skills if s.canonical == name), None)
        primary = ev.ref(skill.evidence)
        if primary.line is not None and not layout.lines[primary.line].usable:
            if not hits:
                continue  # the only evidence is a protected or noise line
            primary = ev.of_line(hits[0])
        refs = [primary]
        for line in demo_lines + [line for line in hits if line.section == "skills"]:
            ref = ev.of_line(line)
            if all(ref.line != r.line for r in refs) and len(refs) < 4:
                refs.append(ref)
        support = "demonstrated" if entries else "listed" if listed or primary.section == "skills" else "mentioned"
        skills.append(
            SkillFact(
                name=name,
                in_ontology=in_ontology,
                listed=listed or primary.section == "skills",
                demonstrated=bool(entries),
                support=support,
                entries=entries[:50],
                last_used=normalized.last_used if normalized else None,
                confidence="verified",
                evidence=refs,
            )
        )
    employment = [
        job.model_copy(update={"skills_used": sorted(entry_skills.get(job.key, set()))[:100]}) for job in employment
    ]

    # --- Accomplishments and leadership (candidate-stated, from entry and summary lines) --------------------
    accomplishments: list[AccomplishmentFact] = []
    leadership: list[EvidenceRef] = []
    for line in usable:
        if line.section not in {"experience", "projects", "summary", "other"}:
            continue
        metrics = [" ".join(m.group(0).split()) for m in METRIC_RE.finditer(line.text)]
        metrics = [m for m in dict.fromkeys(metrics) if not DATE_RANGE_RE.fullmatch(m)]
        if metrics and not DATE_RANGE_RE.fullmatch(line.text.strip()):
            accomplishments.append(
                AccomplishmentFact(
                    context=owners.get(line.index, line.section), metrics=metrics[:5], evidence=ev.of_line(line)
                )
            )
        if line.section == "experience" and LEADERSHIP_RE.search(line.text) and len(leadership) < 10:
            leadership.append(ev.of_line(line))

    # --- Education and certifications (explicitly stated) -------------------------------------------------
    education = [
        EducationFact(
            degree=e.degree, field=e.field, institution=e.institution, year=e.year, evidence=ev.ref(e.evidence)
        )
        for e in data.education
    ]
    certifications = [
        CertificationFact(name=c.name, issuer=c.issuer, year=c.year, evidence=ev.ref(c.evidence))
        for c in data.certifications
    ]
    by_degree: dict[tuple[str, str], set[str]] = defaultdict(set)
    for e in education:
        if e.year:
            by_degree[(e.degree.casefold(), (e.institution or "").casefold())].add(e.year)
    for (degree, institution), years in by_degree.items():
        if len(years) > 1:
            conflicts.append(
                FactConflict(
                    field="education_year",
                    values=[
                        StatedValue(value=e.year or "", evidence=e.evidence)
                        for e in education
                        if (e.degree.casefold(), (e.institution or "").casefold()) == (degree, institution)
                    ],
                )
            )

    # --- Seniority -----------------------------------------------------------------------------------------
    dated = [job for job in employment if job.start]
    seniority = SeniorityFact(
        level=dated[0].seniority if dated else None,
        basis="most_recent_dated_role" if dated else "unknown",
        evidence=dated[0].evidence if dated else None,
        leadership=leadership,
    )

    # --- Availability and preferences (candidate-provided) -------------------------------------------------
    notice_values: dict[int, list[StatedValue]] = defaultdict(list)
    location_values: list[StatedValue] = []
    preferences: list[PreferenceFact] = []
    claimed_years: list[tuple[float, Line]] = []
    for line in usable:
        for m in NOTICE_RE.finditer(line.text):
            days = _notice_days(m)
            notice_values[days].append(StatedValue(value=str(days), evidence=ev.of_line(line)))
        for m in LOCATION_RE.finditer(line.text):
            value = m.group("value").strip(" .;")
            if value and not re.search(r"\d", value):
                location_values.append(StatedValue(value=value, evidence=ev.of_line(line)))
        for kind, pattern in PREFERENCE_PATTERNS:
            if pattern.search(line.text):
                preferences.append(
                    PreferenceFact(kind=kind, statement=line.text[:_QUOTE_LIMIT], evidence=ev.of_line(line))  # type: ignore[arg-type]
                )
        if line.section in {"header", "summary"}:
            for m in CLAIMED_YEARS_RE.finditer(line.text):
                claimed_years.append((float(m.group("n")), line))
    if data.notice_period_days is not None:
        extracted = data.notice_period_days.days
        if extracted not in notice_values:
            ref = ev.ref(data.notice_period_days.evidence)
            notice_values[extracted].append(StatedValue(value=str(extracted), evidence=ref))
    notice = _value_fact({str(days): values for days, values in notice_values.items()})
    if notice.status == "conflicting":
        conflicts.append(FactConflict(field="notice_period_days", values=notice.values))

    if data.location is not None and not any(
        normalize_for_match(v.value) == normalize_for_match(data.location.value) for v in location_values
    ):
        location_values.append(StatedValue(value=data.location.value, evidence=ev.ref(data.location.evidence)))
    location_groups: dict[str, list[StatedValue]] = {}
    for value in location_values:
        key = next((k for k in location_groups if _same_place(k, value.value)), value.value)
        location_groups.setdefault(key, []).append(value)
    location = _value_fact(location_groups)
    if location.status == "conflicting":
        conflicts.append(FactConflict(field="location", values=location.values))

    if claimed_years and dated:
        claimed, line = max(claimed_years, key=lambda c: c[0])
        actual = candidate.total_experience_months / 12
        if claimed - actual > 1.5:
            warnings.append(
                FactWarning(
                    field="total_experience",
                    code="claimed_experience_exceeds_dated_roles",
                    severity="review",
                    message=f"The resume states {claimed:g} years of experience; dated roles cover {actual:.1f} years. "
                    "Undated or omitted roles may explain the difference.",
                    evidence=[ev.of_line(line)],
                )
            )

    return (
        _finish(
            provenance,
            reference_month,
            total=candidate.total_experience_months,
            skills=skills,
            employment=employment,
            overlaps=overlaps,
            projects=projects,
            accomplishments=accomplishments[:50],
            education=education,
            certifications=certifications,
            seniority=seniority,
            notice=notice,
            location=location,
            preferences=preferences[:20],
            warnings=warnings,
            conflicts=conflicts,
        ),
        anchors,
    )


def _same_place(a: str, b: str) -> bool:
    return location_matches(a, [b]) or location_matches(b, [a])


def _value_fact(groups: dict[str, list[StatedValue]]) -> ValueFact:
    flat = [values[0] for values in groups.values()]
    if not groups:
        return ValueFact(status="unknown", value=None, values=[])
    if len(groups) == 1:
        return ValueFact(status="stated", value=next(iter(groups)), values=flat[:10])
    return ValueFact(status="conflicting", value=None, values=flat[:10])


def _finish(
    provenance: ExtractionProvenance,
    reference_month: str,
    *,
    total: int = 0,
    skills: list[SkillFact] | None = None,
    employment: list[EmploymentFact] | None = None,
    overlaps: list[EmploymentOverlap] | None = None,
    projects: list[ProjectFact] | None = None,
    accomplishments: list[AccomplishmentFact] | None = None,
    education: list[EducationFact] | None = None,
    certifications: list[CertificationFact] | None = None,
    seniority: SeniorityFact | None = None,
    notice: ValueFact | None = None,
    location: ValueFact | None = None,
    preferences: list[PreferenceFact] | None = None,
    warnings: list[FactWarning],
    conflicts: list[FactConflict],
) -> FactSet:
    unknown_value = ValueFact(status="unknown", value=None, values=[])
    seniority = seniority or SeniorityFact(level=None, basis="unknown", evidence=None, leadership=[])
    notice = notice or unknown_value
    location = location or unknown_value
    review: list[ReviewItem] = []
    for n, warning in enumerate(warnings):
        if warning.severity == "review":
            review.append(
                ReviewItem(
                    key=f"{warning.field}:{warning.code}:{n}",
                    field=warning.field,
                    reason=warning.code,
                    severity="review",
                    message=warning.message,
                    evidence=warning.evidence,
                )
            )
    for n, conflict in enumerate(conflicts):
        review.append(
            ReviewItem(
                key=f"{conflict.field}:conflict:{n}",
                field=conflict.field,
                reason="conflicting_statements",
                severity="review",
                message=f"The resume states {len(conflict.values)} different values for {conflict.field}: "
                + "; ".join(v.value for v in conflict.values)[:300],
                evidence=[v.evidence for v in conflict.values][:6],
            )
        )
    unknown = [
        name
        for name, missing in (
            ("skills", not skills),
            ("employment", not employment),
            ("education", not education),
            ("certifications", not certifications),
            ("seniority", seniority.level is None),
            ("notice_period_days", notice.status == "unknown"),
            ("location", location.status == "unknown"),
            ("preferences", not preferences),
        )
        if missing
    ]
    return FactSet.model_validate(
        {
            "schema_version": FACTS_SCHEMA_VERSION,
            "policy_version": FACTS_POLICY_VERSION,
            "reference_month": reference_month,
            "extraction": provenance,
            "total_experience_months": total,
            "skills": skills or [],
            "employment": employment or [],
            "overlaps": overlaps or [],
            "projects": projects or [],
            "accomplishments": accomplishments or [],
            "education": education or [],
            "certifications": certifications or [],
            "seniority": seniority,
            "notice_period_days": notice,
            "location": location,
            "preferences": preferences or [],
            "warnings": warnings,
            "conflicts": conflicts,
            "review_items": review,
            "unknown": unknown,
        }
    )
