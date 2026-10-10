"""Layout of one resume version's redacted text: lines, sections, headings, pages, noise and protected lines.

Every offset refers to the stored ``resumes.redacted_text`` of that version, so a location identifies the exact
text a recruiter or the screening engine saw.

Pages: for a text PDF the parser joins pages with one blank line and emits no blank lines within a page;
``_tidy`` collapses longer runs and contact redaction never spans a blank line (it cannot match two newline
characters). Splitting the redacted text on blank lines therefore recovers the pages exactly when their count
equals the PDF page count. DOCX and TXT files have no pages, and OCR output joins pages with a single newline,
so those are recorded as ``not_paginated`` / ``unavailable`` rather than guessed.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from engine.extraction import split_sections
from engine.normalize import DATE_RANGE_RE
from retrieval.versions import LAYOUT_VERSION

SECTION_TYPES = ("header", "summary", "experience", "projects", "skills", "education", "certifications", "other")
SourceKind = Literal["pdf_text", "pdf_ocr", "docx", "txt", "unknown"]
PageMapping = Literal["exact", "derived", "not_paginated", "unavailable"]
NoiseKind = Literal["page_marker", "separator", "repeated_header", "ocr_noise"]

BULLET_RE = re.compile(r"^\s*(?:[-*•·▪●◦]|\d+[.)])\s+")
_PERSONAL_HEADING = re.compile(
    r"^\s*(?:#+\s*)?(?:personal\s+(?:details|information|data|particulars|profile\s+details)|bio[\s-]?data)\s*:?\s*$",
    re.IGNORECASE,
)
# Label-based patterns: a resume line that *states* a protected or sensitive personal attribute. Such lines are
# never retrievable, embedded or sent to a model; matching on labels keeps ordinary prose ("aged-care software")
# out of the net.
PROTECTED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("date_of_birth", r"\b(?:date\s+of\s+birth|d\.\s?o\.\s?b\.?|dob\s*[:\-]|birth\s*date|born\s+on)"),
        ("age", r"\bage[d]?\s*[:\-]\s*\d{1,2}\b|\b\d{1,2}\s+years?\s+old\b"),
        ("gender", r"\b(?:gender|sex)\s*[:\-]"),
        ("marital_status", r"\bmarital\s+status\b"),
        ("religion_or_caste", r"\b(?:religion|caste|sub[\s-]?caste)\s*[:\-]"),
        ("nationality", r"\b(?:nationality|citizenship)\s*[:\-]"),
        ("family", r"\b(?:father|mother|spouse|husband|wife|guardian)(?:'s|s)?\s+name\b"),
        ("physical", r"\b(?:blood\s+group|height|weight)\s*[:\-]"),
        (
            "identity_document",
            r"\b(?:passport\s*(?:no|number|#)|aadhaa?r|pan\s*(?:card|no|number)|social\s+security|ssn\b)",
        ),
        ("health", r"\b(?:disabilit(?:y|ies)|medical\s+condition|health\s+(?:condition|status))\s*[:\-]"),
        ("photo", r"\bphoto(?:graph)?\s*[:\-]"),
        ("pronouns", r"\bpronouns?\s*[:\-]"),
    )
)
_PAGE_MARKER = re.compile(
    r"^\s*(?:page\s*)?\d{1,3}\s*(?:(?:of|/)\s*\d{1,3})?\s*$|\bpage\s+\d{1,3}\s*(?:of|/)\s*\d{1,3}\b", re.IGNORECASE
)
_SEPARATOR = re.compile(r"^[\W_]{3,}$")
_REPEAT_RUN = re.compile(r"(.)\1{5,}")


@dataclass(frozen=True)
class Line:
    index: int
    start: int
    end: int
    text: str  # stripped
    section: str
    heading: str | None  # heading text of the section the line belongs to (None before the first heading)
    is_heading: bool
    page: int | None  # 1-based
    bullet: bool
    noise: NoiseKind | None
    protected: str | None  # protected/sensitive attribute category the line states

    @property
    def usable(self) -> bool:
        """Content that may be chunked, embedded and shown as evidence."""
        return bool(self.text) and not self.is_heading and self.noise is None and self.protected is None


@dataclass(frozen=True)
class DocumentLayout:
    text: str
    lines: tuple[Line, ...]
    page_spans: tuple[tuple[int, int], ...] | None
    page_mapping: PageMapping
    version: str = LAYOUT_VERSION

    @property
    def page_count(self) -> int | None:
        return len(self.page_spans) if self.page_spans is not None else None

    def line_at(self, offset: int) -> Line | None:
        starts = [line.start for line in self.lines]
        index = bisect_right(starts, offset) - 1
        return self.lines[index] if 0 <= index < len(self.lines) else None

    def lines_between(self, start: int, end: int) -> list[Line]:
        """Non-empty lines overlapping [start, end)."""
        return [line for line in self.lines if line.text and line.start < end and line.end > start]

    def counts(self) -> dict[str, int]:
        noise: dict[str, int] = defaultdict(int)
        protected: dict[str, int] = defaultdict(int)
        for line in self.lines:
            if line.noise:
                noise[line.noise] += 1
            if line.protected:
                protected[line.protected] += 1
        return {
            "lines": sum(1 for line in self.lines if line.text),
            "usable_lines": sum(1 for line in self.lines if line.usable),
            **{f"noise_{k}": v for k, v in sorted(noise.items())},
            **{f"protected_{k}": v for k, v in sorted(protected.items())},
        }


def source_kind(mime: str | None, ocr_pages: int) -> SourceKind:
    if mime == "application/pdf":
        return "pdf_ocr" if ocr_pages else "pdf_text"
    if mime and "wordprocessingml" in mime:
        return "docx"
    if mime == "text/plain":
        return "txt"
    return "unknown"


def page_spans(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for m in re.finditer(r"\n\n", text):
        spans.append((start, m.start()))
        start = m.end()
    spans.append((start, len(text)))
    return spans


def protected_category(text: str) -> str | None:
    for name, pattern in PROTECTED_PATTERNS:
        if pattern.search(text):
            return name
    return None


def _is_ocr_noise(text: str) -> bool:
    s = text.strip()
    if len(s) < 6 or _SEPARATOR.match(s):
        return False
    if sum(c.isalnum() for c in s) / len(s) < 0.45:
        return True
    tokens = s.split()
    if len(tokens) >= 5 and sum(len(t) == 1 for t in tokens) / len(tokens) >= 0.6:
        return True
    return _REPEAT_RUN.search(s) is not None


def _noise_key(text: str) -> str:
    return " ".join(re.sub(r"\d+", "#", text.casefold()).split())


def analyze(text: str, *, kind: SourceKind = "unknown", expected_pages: int | None = None) -> DocumentLayout:
    """Lines with section, heading, page, noise and protected classification."""
    if kind == "pdf_text":
        spans = page_spans(text)
        if expected_pages is not None and len(spans) != expected_pages:
            pages: list[tuple[int, int]] | None = None
            mapping: PageMapping = "unavailable"
        else:
            pages, mapping = spans, ("exact" if expected_pages is not None else "derived")
    else:
        pages, mapping = None, ("unavailable" if kind in {"pdf_ocr", "unknown"} else "not_paginated")

    raw: list[tuple[int, int, str]] = []
    offset = 0
    for piece in text.split("\n"):
        raw.append((offset, offset + len(piece), piece))
        offset += len(piece) + 1
    starts = [start for start, _, _ in raw]

    # Section of every content line, and inline headings ("Skills: Python, SQL").
    content: dict[int, str] = {}
    inline_heading: dict[int, str] = {}
    for name, section_lines in split_sections(text).lines.items():
        for section_line in section_lines:
            index = bisect_right(starts, section_line.start) - 1
            content[index] = name
            line_start, _, piece = raw[index]
            lead = len(piece) - len(piece.lstrip())
            if section_line.start > line_start + lead:
                inline_heading[index] = text[line_start : section_line.start].strip().rstrip(":").strip()

    page_starts = [s for s, _ in pages] if pages else []
    personal_block = False
    current_heading: str | None = None
    lines: list[Line] = []
    for index, (start, end, piece) in enumerate(raw):
        stripped = piece.strip()
        is_heading = bool(stripped) and index not in content
        if is_heading:
            current_heading = stripped.rstrip(":").strip()
            personal_block = _PERSONAL_HEADING.match(stripped) is not None
            section = next((content[j] for j in range(index + 1, len(raw)) if j in content), "other")
        elif stripped:
            section = content[index]
            if index in inline_heading:
                current_heading = inline_heading[index]
                personal_block = _PERSONAL_HEADING.match(current_heading) is not None
            elif _PERSONAL_HEADING.match(stripped):
                # Headings the screening sectioner does not know ("Personal Information") start a block too.
                current_heading, personal_block, is_heading = stripped.rstrip(":").strip(), True, True
        else:
            section = lines[-1].section if lines else "header"
        page = (bisect_right(page_starts, start) if pages else None) or None
        protected = None
        if stripped and not is_heading:
            protected = "personal_details" if personal_block else protected_category(stripped)
        noise: NoiseKind | None = None
        if stripped and not is_heading:
            if _SEPARATOR.match(stripped):
                noise = "separator"
            elif len(stripped) <= 100 and _PAGE_MARKER.search(stripped):
                noise = "page_marker"
            elif _is_ocr_noise(stripped):
                noise = "ocr_noise"
        lines.append(
            Line(
                index=index,
                start=start,
                end=end,
                text=stripped,
                section=section,
                heading=current_heading if section != "header" or is_heading else None,
                is_heading=is_heading,
                page=page,
                bullet=bool(BULLET_RE.match(piece)),
                noise=noise,
                protected=protected,
            )
        )
    return DocumentLayout(
        text=text, lines=_mark_repeated(lines, pages is not None), page_spans=tuple(pages) if pages else None,
        page_mapping=mapping,
    )  # fmt: skip


def _mark_repeated(lines: list[Line], paginated: bool) -> tuple[Line, ...]:
    """Running headers/footers: the same line at the edge of several pages (or 3+ times in an unpaged file)."""
    candidates: dict[str, set[int]] = defaultdict(set)
    if paginated:
        by_page: dict[int, list[Line]] = defaultdict(list)
        for line in lines:
            if line.text and not line.is_heading and line.page is not None:
                by_page[line.page].append(line)
        if len(by_page) < 2:
            return tuple(lines)
        pages_with: dict[str, set[int]] = defaultdict(set)
        for page, page_lines in by_page.items():
            for line in page_lines[:2] + page_lines[-2:]:
                pages_with[_noise_key(line.text)].add(page)
                candidates[_noise_key(line.text)].add(line.index)
        repeated = {key for key, seen in pages_with.items() if len(seen) >= 2}
    else:
        # Without pages only identical lines count (digits matter: "Co1 | 2019" and "Co2 | 2020" are content),
        # and dated lines are never running headers.
        counts: dict[str, int] = defaultdict(int)
        for line in lines:
            if (
                line.text
                and not line.is_heading
                and not line.bullet
                and len(line.text) >= 8
                and DATE_RANGE_RE.search(line.text) is None
            ):
                key = " ".join(line.text.casefold().split())
                counts[key] += 1
                candidates[key].add(line.index)
        repeated = {key for key, count in counts.items() if count >= 3}
    marked = {index for key in repeated for index in candidates[key]}
    return tuple(
        Line(**{**line.__dict__, "noise": "repeated_header"}) if line.index in marked and line.noise is None else line
        for line in lines
    )
