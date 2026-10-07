"""Normalization: skill ontology, dates, experience, seniority and locations.

* Skills map to canonical names via ``skills_ontology.json``; ambiguous short
  aliases ("go", "r", "cv") only count as standalone items in a skills list.
* Dates ("2021-01", "Jan 2021", "01/2021", "2021", "present") parse to months;
  total experience is the union of job intervals, so overlaps count once.
* Job titles map to seniority levels 1 (intern) .. 6 (principal).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

from engine.schemas import (
    CandidateExtraction,
    CandidateSkill,
    JobItem,
    NormalizedCandidate,
    NormalizedJob,
    SeniorityLevel,
)

ONTOLOGY_PATH = Path(__file__).with_name("skills_ontology.json")

# ---------------------------------------------------------------------------
# Skill ontology
# ---------------------------------------------------------------------------

# A skill token may contain + # . / - (C++, C#, Node.js, CI/CD, scikit-learn), so
# ordinary \b word boundaries are not enough.
# "/" separates skills ("PyTorch/TensorFlow"); slash-containing aliases (CI/CD, A/B testing, PL/SQL)
# still match whole because aliases are tried longest-first.
_LEFT = r"(?<![\w+#.-])"
_RIGHT = r"(?![\w+#]|\.\w|-\w)"
# Separators between items in a skills list ("Python, Go | R / SQL • Docker").
_LIST_SPLIT = re.compile(r"\s*(?:[,;|•·▪●\t]|\s/\s|\s-\s|\band\b)\s*")


@dataclass(frozen=True)
class SkillMention:
    canonical: str
    matched: str  # alias text as it appears in the source
    line: str  # the full source line containing the mention (verbatim)


@dataclass(frozen=True, eq=False)
class SkillOntology:
    version: str
    categories: dict[str, str]  # canonical -> category
    aliases: dict[str, str]  # lowercase alias -> canonical (unambiguous, free-text safe)
    ambiguous: dict[str, str]  # lowercase alias -> canonical (skills-list only)
    # Compiled free-text aliases, longest first, each with a literal that every match contains (fast pre-check).
    patterns: tuple[tuple[re.Pattern[str], str, str], ...]

    def canonicalize(self, name: str) -> str | None:
        """Map an explicitly stated skill name to its canonical form (any alias allowed)."""
        key = _norm_key(name)
        return self.aliases.get(key) or self.ambiguous.get(key)

    def find_mentions(self, text: str, skills_section: str | None = None) -> list[SkillMention]:
        """Find skills in ``text``; ambiguous aliases only count as standalone items in ``skills_section``."""
        found: dict[str, SkillMention] = {}
        lines = text.split("\n")
        for line in lines:
            low = line.lower()
            for alias, canonical, needle in self.patterns:
                if canonical in found or needle not in low:
                    continue
                m = alias.search(low)
                if m:
                    found[canonical] = SkillMention(canonical, line[m.start() : m.end()], line.strip())
        if skills_section:
            for line in skills_section.split("\n"):
                for token in _skill_list_tokens(line):
                    canonical = self.ambiguous.get(token.lower())
                    if canonical and canonical not in found:
                        found[canonical] = SkillMention(canonical, token, line.strip())
        return list(found.values())


def _norm_key(name: str) -> str:
    return " ".join(name.lower().split())


def _skill_list_tokens(line: str) -> list[str]:
    # Drop a leading label such as "Languages:" before splitting.
    body = line.split(":", 1)[1] if ":" in line[:40] else line
    return [t.strip(" .()[]") for t in _LIST_SPLIT.split(body) if t.strip(" .()[]")]


@lru_cache(maxsize=1)
def load_ontology(path: Path = ONTOLOGY_PATH) -> SkillOntology:
    raw = json.loads(path.read_text(encoding="utf-8"))
    categories: dict[str, str] = {}
    aliases: dict[str, str] = {}
    ambiguous: dict[str, str] = {}
    for canonical, spec in raw["skills"].items():
        categories[canonical] = spec["category"]
        amb = {_norm_key(a) for a in spec.get("ambiguous_aliases", [])}
        for a in amb:
            _register(ambiguous, a, canonical)
        for a in [canonical, *spec.get("aliases", [])]:
            key = _norm_key(a)
            if key not in amb:
                _register(aliases, key, canonical)
    ordered = sorted(aliases.items(), key=lambda kv: -len(kv[0]))
    patterns = tuple(
        (re.compile(_LEFT + _alias_regex(alias) + _RIGHT), canonical, _needle(alias)) for alias, canonical in ordered
    )
    return SkillOntology(
        version=raw["version"], categories=categories, aliases=aliases, ambiguous=ambiguous, patterns=patterns
    )


# Aliases whose inflected forms are ordinary English ("reacted", "sparked", "forecasts") are matched exactly.
INFLECTION_GUARD = frozenset(
    {"react", "spark", "rust", "flask", "torch", "snowflake", "forecasting", "containerization", "unix"}
    # Profession skills whose stems are ordinary words: "key accounts", "bank", "bill", "purchases", "brand".
    | {"accounting", "banking", "billing", "advertising", "merchandising", "prospecting", "budgeting"}
    | {"branding", "purchasing"}
)
_MIN_INFLECT_LEN = 4  # shorter tokens (sql, aws, dbt, git) are acronyms, not words


def inflected_forms(word: str) -> list[str]:
    """Generic -s/-es/-ed/-ing variants of a word ("fine-tuning" -> fine-tune, fine-tuned, fine-tunes...)."""
    if word.endswith("ing") and len(word) > 5:
        stem = word[:-3]
        return [word, stem + "e", stem + "es", stem + "ed", stem + "s", stem]
    if word.endswith("e"):
        return [word, word + "s", word + "d", word[:-1] + "ing"]
    return [word, word + "s", word + "es", word + "ed", word + "ing"]


def _last_word_forms(alias: str) -> list[str]:
    """Accepted forms of an alias's last word: inflected unless the alias is guarded or the word is too short."""
    last = alias.split()[-1]
    if alias in INFLECTION_GUARD or len(last) < _MIN_INFLECT_LEN or not re.fullmatch(r"[a-z-]+", last):
        return [last]
    return sorted(set(inflected_forms(last)), key=len, reverse=True)


def _alias_regex(alias: str) -> str:
    """Regex for an alias; its last word may be inflected (see ``_last_word_forms``)."""
    words = alias.split()
    last_re = "(?:" + "|".join(re.escape(f) for f in _last_word_forms(alias)) + ")"
    return r"\s+".join([*map(re.escape, words[:-1]), last_re])


def _needle(alias: str) -> str:
    """A literal contained in every match of the alias: its first word, or the shared prefix of its word forms."""
    words = alias.split()
    return words[0] if len(words) > 1 else os.path.commonprefix(_last_word_forms(alias))


def _register(table: dict[str, str], alias: str, canonical: str) -> None:
    if table.get(alias, canonical) != canonical:
        raise ValueError(f"Ontology alias {alias!r} maps to both {table[alias]!r} and {canonical!r}")
    table[alias] = canonical


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}
_MONTH_NAME = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_PRESENT = r"(?:present|current(?:ly)?|now|ongoing|till\s+date|to\s+date|today)"
_DATE = (
    rf"(?:{_MONTH_NAME}\.?,?\s*'?\d{{2,4}}"  # Jan 2021, January, 2021, Jan '21
    r"|\d{1,2}[/.]\d{4}"  # 01/2021
    r"|\d{4}[-/.]\d{1,2}(?!\d)"  # 2021-01
    r"|(?:19|20)\d{2})"  # 2021
)
# A single date anywhere in a line (used to verify partial job dates).
DATE_RE = re.compile(_DATE, re.IGNORECASE)
DATE_RANGE_RE = re.compile(
    rf"(?P<start>{_DATE})\s*(?:-|–|—|to|until|till)\s*(?P<end>{_DATE}|{_PRESENT})",
    re.IGNORECASE,
)
_PRESENT_RE = re.compile(rf"^{_PRESENT}$", re.IGNORECASE)


@dataclass(frozen=True, order=True)
class YearMonth:
    year: int
    month: int

    @property
    def index(self) -> int:
        """Months since year 0; differences give month counts."""
        return self.year * 12 + (self.month - 1)

    @classmethod
    def from_date(cls, d: date) -> YearMonth:
        return cls(d.year, d.month)


def parse_date(text: str, *, is_end: bool = False, today: date | None = None) -> YearMonth | None:
    """Parse 'YYYY-MM', 'Jan 2021', 'January 2021', '01/2021', '2021' or 'present'.

    Year-only dates resolve to January for starts and December for ends.
    Returns ``None`` for unparseable input or impossible months.
    """
    s = " ".join(text.strip().lower().replace(",", " ").split())
    if not s:
        return None
    if _PRESENT_RE.match(s):
        return YearMonth.from_date(today or date.today())

    if m := re.fullmatch(rf"({_MONTH_NAME})\.?\s*'?(\d{{2}}|\d{{4}})", s):
        return _ym(_expand_year(m.group(2)), _MONTHS[m.group(1)])
    if m := re.fullmatch(r"(\d{1,2})[/.](\d{4})", s):
        return _ym(int(m.group(2)), int(m.group(1)))
    if m := re.fullmatch(r"(\d{4})[-/.](\d{1,2})", s):
        return _ym(int(m.group(1)), int(m.group(2)))
    if m := re.fullmatch(r"((?:19|20)\d{2})", s):
        return _ym(int(m.group(1)), 12 if is_end else 1)
    return None


def is_present(text: str) -> bool:
    return bool(_PRESENT_RE.match(" ".join(text.strip().lower().split())))


def _expand_year(y: str) -> int:
    if len(y) == 4:
        return int(y)
    yy = int(y)
    return 2000 + yy if yy <= (date.today().year % 100) else 1900 + yy


def _ym(year: int, month: int) -> YearMonth | None:
    if not 1 <= month <= 12 or not 1950 <= year <= 2100:
        return None
    return YearMonth(year, month)


# ---------------------------------------------------------------------------
# Seniority
# ---------------------------------------------------------------------------

# Highest matching level wins ("Senior Manager" -> lead, "Senior Associate" -> senior).
_SENIORITY_PATTERNS: list[tuple[SeniorityLevel, re.Pattern[str]]] = [
    (
        SeniorityLevel.PRINCIPAL,
        re.compile(r"\b(principal|distinguished|director|head of|vp|vice president|chief|cto|fellow)\b"),
    ),
    (SeniorityLevel.LEAD, re.compile(r"\b(lead|staff|manager|architect)\b")),
    (SeniorityLevel.SENIOR, re.compile(r"\b(senior|sr\.?|iii)(?=\W|$)")),
    (SeniorityLevel.JUNIOR, re.compile(r"\b(junior|jr\.?|associate|graduate|entry[- ]level|fresher)(?=\W|$)")),
    (SeniorityLevel.INTERN, re.compile(r"\b(intern|internship|trainee|apprentice)\b")),
]
SENIORITY_NAMES: dict[SeniorityLevel, str] = {
    SeniorityLevel.INTERN: "intern",
    SeniorityLevel.JUNIOR: "junior",
    SeniorityLevel.MID: "mid",
    SeniorityLevel.SENIOR: "senior",
    SeniorityLevel.LEAD: "lead",
    SeniorityLevel.PRINCIPAL: "principal",
}


def title_to_seniority(title: str) -> SeniorityLevel:
    """Map a job title to a 1-6 seniority level; titles without a level marker are mid (3)."""
    t = title.lower()
    matched = [level for level, pattern in _SENIORITY_PATTERNS if pattern.search(t)]
    return max(matched) if matched else SeniorityLevel.MID


# ---------------------------------------------------------------------------
# Experience
# ---------------------------------------------------------------------------

Interval = tuple[YearMonth, YearMonth]


def job_interval(start: str | None, end: str | None, today: date | None = None) -> Interval | None:
    """Parse a job's dates into an inclusive (start, end) month interval.

    "Present"-style ends resolve to ``today``; future ends are clamped to ``today``. Returns None
    when either date is missing/unparseable or the range is backwards.
    """
    ref = YearMonth.from_date(today or date.today())
    if not start or not end:
        return None
    s = parse_date(start, today=today)
    e = parse_date(end, is_end=True, today=today)
    if s is None or e is None:
        return None
    e = min(e, ref)
    if s > e:
        return None
    return s, e


def months_inclusive(interval: Interval) -> int:
    s, e = interval
    return e.index - s.index + 1


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """Union of month intervals: overlapping or back-to-back jobs become one span."""
    merged: list[Interval] = []
    for s, e in sorted(intervals):
        if merged and s.index <= merged[-1][1].index + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def total_experience_months(intervals: list[Interval]) -> int:
    """Total months of experience with overlapping jobs counted once."""
    return sum(months_inclusive(iv) for iv in merge_intervals(intervals))


def format_ym(ym: YearMonth) -> str:
    return f"{ym.year:04d}-{ym.month:02d}"


def ym_from_str(s: str) -> YearMonth:
    year, month = s.split("-")
    return YearMonth(int(year), int(month))


# ---------------------------------------------------------------------------
# Candidate normalization
# ---------------------------------------------------------------------------


_YEAR_ONLY = re.compile(r"\s*(?:19|20)\d{2}\s*")


def is_year_only(text: str | None) -> bool:
    return bool(text) and _YEAR_ONLY.fullmatch(text) is not None  # type: ignore[arg-type]


def _infer_intervals(jobs: list[JobItem], today: date) -> list[tuple[Interval | None, str | None, str | None]]:
    """(interval, confidence, note) per job, inferring incomplete dates.

    * Full month range: high confidence.
    * Year only ("2022", or 2022-2022): Jan..Dec of that year, or Jan..this month for the current year.
    * Start only ("Jan 2022 -"): ends at present for the most recent job, else at the next job's start.
    Any year-only date makes the interval low confidence.
    """
    ref = YearMonth.from_date(today)
    out: list[tuple[Interval | None, str | None, str | None]] = []
    starts = [parse_date(j.start, today=today) if j.start else None for j in jobs]
    for k, job in enumerate(jobs):
        start = starts[k]
        same_year_only = is_year_only(job.start) and (job.end is None or job.end.strip() == job.start.strip())
        interval = None if same_year_only else job_interval(job.start, job.end, today)
        if interval is not None:
            low = is_year_only(job.start) or is_year_only(job.end)
            note = "year-only dates; months approximated" if low else None
            out.append((interval, "low" if low else "high", note))
        elif start is None or start > ref:
            out.append((None, None, None))
        elif same_year_only:
            end = YearMonth(start.year, 12) if start.year < ref.year else ref
            out.append(
                ((start, end), "low", f"year only ({start.year}); counted as {start.year} through {format_ym(end)}")
            )
        elif job.end is None:
            later = [s for s in starts if s is not None and s > start]
            if not later:
                out.append(((start, ref), "low", "no end date; most recent role, treated as ongoing"))
            else:
                nxt = min(later)
                out.append(
                    ((start, nxt), "low", f"no end date; assumed to end when the next role began ({format_ym(nxt)})")
                )
        else:
            out.append((None, None, None))  # unparseable or backwards range
    return out


MINORITY_SIDE_NOTE = "dates were found on the opposite side of the title from the rest of this resume"


def normalize_candidate(
    extraction: CandidateExtraction,
    ontology: SkillOntology | None = None,
    today: date | None = None,
    low_confidence_jobs: Sequence[int] = (),
) -> NormalizedCandidate:
    """Canonicalize skills, parse/infer job dates, merge overlapping experience and derive seniority."""
    ontology = ontology or load_ontology()
    today = today or date.today()

    jobs: list[tuple[NormalizedJob, Interval | None]] = []
    inferred = _infer_intervals(extraction.jobs, today)
    for k, (job, (interval, confidence, note)) in enumerate(zip(extraction.jobs, inferred, strict=True)):
        if k in low_confidence_jobs and interval is not None:
            confidence, note = "low", note or MINORITY_SIDE_NOTE
        normalized = NormalizedJob(
            title=job.title,
            company=job.company,
            start=format_ym(interval[0]) if interval else None,
            end=format_ym(interval[1]) if interval else None,
            is_current=bool(job.end and is_present(job.end)) or (note is not None and "ongoing" in note),
            months=months_inclusive(interval) if interval else 0,
            date_confidence=confidence,  # type: ignore[arg-type]
            date_note=note,
            seniority=int(title_to_seniority(job.title)),
            description=job.description,
            evidence=job.evidence,
        )
        jobs.append((normalized, interval))
    # Most recent first (by end, then start); undated jobs last.
    jobs.sort(key=lambda ji: (ji[1] is not None, (ji[1][1], ji[1][0]) if ji[1] else ()), reverse=True)
    intervals = [iv for _, iv in jobs if iv is not None]
    dated = [j for j, iv in jobs if iv is not None]

    skills = [_normalize_skill(s.name, s.evidence, jobs, ontology) for s in extraction.skills]
    # No jobs at all => entry level (1).
    seniority = dated[0].seniority if dated else (jobs[0][0].seniority if jobs else int(SeniorityLevel.INTERN))

    return NormalizedCandidate(
        skills=skills,
        jobs=[j for j, _ in jobs],
        projects=extraction.projects,
        total_experience_months=total_experience_months(intervals),
        undated_jobs=len(jobs) - len(intervals),
        seniority=seniority,
        location=extraction.location.value if extraction.location else None,
        notice_period_days=extraction.notice_period_days.days if extraction.notice_period_days else None,
        reference_month=format_ym(YearMonth.from_date(today)),
    )


def date_warnings(candidate: NormalizedCandidate) -> list[str]:
    """Human-readable warnings for jobs whose dates were incomplete and had to be inferred."""
    return [
        f"Low-confidence dates for '{job.title}': {job.date_note}"
        for job in candidate.jobs
        if job.date_confidence == "low" and job.date_note
    ]


def _normalize_skill(
    raw: str, evidence: str, jobs: list[tuple[NormalizedJob, Interval | None]], ontology: SkillOntology
) -> CandidateSkill:
    canonical = ontology.canonicalize(raw)
    name = canonical or " ".join(raw.split())
    pattern = re.compile(r"(?<![\w+#])" + re.escape(name.lower()) + r"(?![\w+#])")
    last_used: YearMonth | None = None
    for job, interval in jobs:
        if interval is None:
            continue
        text = "\n".join(filter(None, [job.title, job.description, job.evidence]))
        if canonical:
            mentioned = any(m.canonical == canonical for m in ontology.find_mentions(text))
        else:
            mentioned = pattern.search(text.lower()) is not None
        if mentioned and (last_used is None or interval[1] > last_used):
            last_used = interval[1]
    return CandidateSkill(
        canonical=name,
        raw=raw,
        evidence=evidence,
        in_ontology=canonical is not None,
        last_used=format_ym(last_used) if last_used else None,
    )


def canonical_skill_key(name: str, ontology: SkillOntology | None = None) -> str:
    """Comparison key for a skill name: its canonical form if known, casefolded."""
    ontology = ontology or load_ontology()
    return (ontology.canonicalize(name) or " ".join(name.split())).casefold()


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

_CITY_ALIASES = {
    "bangalore": "bengaluru",
    "bombay": "mumbai",
    "madras": "chennai",
    "calcutta": "kolkata",
    "gurgaon": "gurugram",
    "new delhi": "delhi",
    "nyc": "new york",
    "new york city": "new york",
    "sf": "san francisco",
    "bay area": "san francisco",
}
_WORK_MODE = re.compile(r"\((?:hybrid|on-?site|in-?office|remote)\)", re.IGNORECASE)


def _location_parts(location: str) -> list[str]:
    cleaned = _WORK_MODE.sub("", location)
    parts = [" ".join(p.split()).casefold() for p in cleaned.split(",")]
    return [_CITY_ALIASES.get(p, p) for p in parts if p]


def is_remote(location: str) -> bool:
    return re.fullmatch(r"\s*(fully\s+)?remote\s*", _WORK_MODE.sub("", location), re.IGNORECASE) is not None


def location_matches(candidate: str, allowed: list[str]) -> bool:
    """True if the candidate's location satisfies any allowed location.

    An allowed "Remote" accepts every location. Otherwise the allowed entry's most specific part
    (its city, or a bare country) must appear among the candidate's location parts.
    """
    if not allowed:
        return True
    cand_parts = set(_location_parts(candidate))
    for entry in allowed:
        if is_remote(entry):
            return True
        parts = _location_parts(entry)
        if parts and parts[0] in cand_parts:
            return True
    return False
