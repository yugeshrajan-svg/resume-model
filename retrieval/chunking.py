"""Section-aware chunking of one resume version for retrieval.

* Experience and project sections are split into entries. With anchors from verified extraction (title line
  plus the date line the verifier assigned to it, which handles dates above/below titles and two-column
  layouts), every line goes to the entry of the nearest preceding title. Without anchors a line heuristic is
  used. A long entry is split into parts that all repeat the entry's header lines, so employer, title and dates
  never become separate fragments from their responsibilities.
* Other sections become one chunk per section, split by size.
* Noise lines (page markers, separators, running headers, OCR debris) and lines stating protected personal
  attributes are never part of a chunk. Contact-only chunks and in-document duplicates are kept for
  traceability but are not retrieval-eligible.
* Chunk text gets a second contact redaction pass and is tagged with ontology skills (ambiguous aliases such
  as "Go" or "R" count only inside a skills section, as in screening extraction).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from engine.normalize import DATE_RANGE_RE, SkillOntology, load_ontology
from retrieval.layout import DocumentLayout, Line
from retrieval.redaction import only_placeholders, redact_chunk_text
from retrieval.safety import has_injection
from retrieval.text import sha256_text
from retrieval.versions import CHUNKING_VERSION, EMBED_TEXT_VERSION

SECTION_LABELS = {
    "header": "Profile",
    "summary": "Summary",
    "experience": "Experience",
    "projects": "Projects",
    "skills": "Skills",
    "education": "Education",
    "certifications": "Certifications",
    "other": "Additional information",
}
ExclusionReason = Literal["contact_only", "duplicate"]
_TITLE_WORDS = re.compile(
    r"\b(engineer|scientist|analyst|developer|manager|intern|lead|architect|consultant|specialist|director|head|"
    r"researcher|associate|officer|administrator|executive|designer|programmer|trainee|founder|accountant|nurse|"
    r"teacher|coordinator|assistant|technician|supervisor)\b",
    re.IGNORECASE,
)
_HEADER_SEPARATOR = re.compile(r"\s(?:\||–|—|-|@|at)\s|,\s")
_SENTENCE_BREAK = re.compile(r"(?<=[.;!?])\s+")
_SOFT_BREAK = re.compile(r"(?:,\s+|\s+\|\s+|\s+)")


@dataclass(frozen=True)
class EntryAnchor:
    """An employment or project entry found by verified extraction."""

    key: str  # "employment:0", "project:1"
    kind: Literal["employment", "project"]
    title_line: int
    header_lines: tuple[int, ...]  # title line and the date line the verifier assigned (line indices)
    label: str


@dataclass(frozen=True)
class ChunkingConfig:
    max_chars: int = 900
    overlap_lines: int = 1
    max_chunks: int = 200

    def describe(self) -> dict[str, int | str]:
        return {
            "version": CHUNKING_VERSION,
            "max_chars": self.max_chars,
            "overlap_lines": self.overlap_lines,
            "max_chunks": self.max_chunks,
        }


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    section: str
    heading: str | None
    entry_key: str | None
    entry_label: str | None
    part: int
    text: str
    spans: tuple[tuple[int, int], ...]  # source spans in the version's redacted text
    line_start: int
    line_end: int
    page_start: int | None
    page_end: int | None
    skills: tuple[str, ...]
    retrieval_eligible: bool
    exclusion_reason: ExclusionReason | None
    injection_suspected: bool
    redactions: int
    text_sha256: str

    @property
    def char_start(self) -> int:
        return min(start for start, _ in self.spans)

    @property
    def char_end(self) -> int:
        return max(end for _, end in self.spans)


@dataclass(frozen=True)
class ChunkingResult:
    chunks: list[Chunk]
    truncated: bool  # the document produced more than ``max_chunks`` chunks; the rest were not indexed

    @property
    def eligible(self) -> list[Chunk]:
        return [c for c in self.chunks if c.retrieval_eligible]


def embed_input(section: str, heading: str | None, text: str) -> str:
    """Text sent to the embedding model for a chunk. Changing it requires a new EMBED_TEXT_VERSION."""
    label = SECTION_LABELS.get(section, "Additional information")
    if heading and heading.casefold() != label.casefold():
        label = f"{label} - {heading}"
    return f"{label}\n{text}"


EMBED_INPUT_VERSION = EMBED_TEXT_VERSION


@dataclass
class _Segment:
    text: str
    start: int
    end: int
    line: Line


@dataclass
class _Unit:
    section: str
    heading: str | None
    entry_key: str | None = None
    entry_label: str | None = None
    header: list[Line] = field(default_factory=list)
    body: list[Line] = field(default_factory=list)

    @property
    def has_date(self) -> bool:
        return any(DATE_RANGE_RE.search(line.text) for line in self.header)


def _text_span(layout: DocumentLayout, line: Line) -> tuple[int, int]:
    raw = layout.text[line.start : line.end]
    lead = len(raw) - len(raw.lstrip())
    return line.start + lead, line.start + lead + len(line.text)


def _regions(layout: DocumentLayout) -> list[list[Line]]:
    """Runs of lines that share one section occurrence (a new heading or section starts a new region)."""
    regions: list[list[Line]] = []
    current: list[Line] = []
    key: tuple[str, str | None] | None = None
    for line in layout.lines:
        if not line.text:
            continue
        line_key = (line.section, line.heading)
        if line.is_heading or line_key != key:
            if current:
                regions.append(current)
            current, key = [], line_key
        if not line.is_heading:
            current.append(line)
    if current:
        regions.append(current)
    return regions


def _owner(line_index: int, local: list[EntryAnchor], claimed: dict[int, str]) -> str | None:
    """The entry a line belongs to: the one that claimed it as a header line, else the nearest preceding title."""
    if line_index in claimed:
        return claimed[line_index]
    return next((a.key for a in reversed(local) if a.title_line <= line_index), None)


def entry_assignment(layout: DocumentLayout, anchors: list[EntryAnchor]) -> dict[int, str]:
    """Line index -> entry key, for every usable line of the experience and project sections.

    The same assignment builds entry chunks and decides whether a skill is demonstrated in a work or project
    entry, so evidence passages and fact support levels agree.
    """
    out: dict[int, str] = {}
    for region in _regions(layout):
        if region[0].section not in {"experience", "projects"}:
            continue
        indices = {line.index for line in region}
        local = sorted((a for a in anchors if a.title_line in indices), key=lambda a: a.title_line)
        claimed = {i: a.key for a in local for i in a.header_lines if i in indices}
        for line in region:
            if line.usable and (owner := _owner(line.index, local, claimed)):
                out[line.index] = owner
    return out


def _anchored_units(region: list[Line], anchors: list[EntryAnchor]) -> list[_Unit] | None:
    indices = {line.index for line in region}
    local = sorted((a for a in anchors if a.title_line in indices), key=lambda a: a.title_line)
    if not local:
        return None
    by_line = {line.index: line for line in region}
    section, heading = region[0].section, region[0].heading
    units = {
        a.key: _Unit(
            section,
            heading,
            a.key,
            a.label,
            header=[by_line[i] for i in sorted(a.header_lines) if i in by_line and by_line[i].usable],
        )
        for a in local
    }
    claimed = {i: a.key for a in local for i in a.header_lines if i in by_line}
    preamble = _Unit(section, heading)
    for line in region:
        if not line.usable or line.index in claimed:
            continue
        owner = _owner(line.index, local, claimed)
        (units[owner].body if owner else preamble.body).append(line)
    ordered = [units[a.key] for a in local]
    return [preamble, *ordered] if preamble.body else ordered


def _heuristic_units(region: list[Line]) -> list[_Unit]:
    section, heading = region[0].section, region[0].heading
    units: list[_Unit] = []
    current: _Unit | None = None
    for line in (line for line in region if line.usable):
        if line.bullet:
            if current is None:
                current = _Unit(section, heading)
                units.append(current)
            current.body.append(line)
            continue
        dated = DATE_RANGE_RE.search(line.text) is not None
        header_like = (
            section == "projects"
            or dated
            or (_TITLE_WORDS.search(line.text) is not None and _HEADER_SEPARATOR.search(line.text) is not None)
        )
        # Every non-bullet project line starts a project, as in rule-based extraction.
        starts_new = section == "projects" or current is None or current.body or (dated and current.has_date)
        if (header_like and starts_new) or current is None:
            current = _Unit(section, heading)
            units.append(current)
            (current.header if header_like else current.body).append(line)
        elif header_like and not current.body:
            current.header.append(line)  # multi-line entry header: title then dates, or dates then title
        else:
            current.body.append(line)
    for n, unit in enumerate(units):
        if unit.header:
            unit.entry_key = f"{section}:h{n}"
            unit.entry_label = " | ".join(line.text for line in unit.header)[:200]
    return units


def _segments(layout: DocumentLayout, line: Line, limit: int) -> list[_Segment]:
    """A line as one segment, or several (at sentence, then list/space boundaries) if it exceeds ``limit``."""
    start, end = _text_span(layout, line)
    text = layout.text[start:end]
    if len(text) <= limit:
        return [_Segment(text, start, end, line)]
    pieces: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(text):
        if len(text) - cursor <= limit:
            pieces.append((cursor, len(text)))
            break
        window = text[cursor : cursor + limit]
        cut = None
        for pattern in (_SENTENCE_BREAK, _SOFT_BREAK):
            breaks = [m.start() for m in pattern.finditer(window) if m.start() > limit // 3]
            if breaks:
                cut = breaks[-1]
                break
        cut = cut or limit
        pieces.append((cursor, cursor + cut))
        cursor += cut
        while cursor < len(text) and text[cursor] in " ,|;":
            cursor += 1
    return [_Segment(text[a:b].strip(), start + a, start + b, line) for a, b in pieces if text[a:b].strip()]


def _parts(layout: DocumentLayout, unit: _Unit, config: ChunkingConfig) -> list[tuple[list[_Segment], list[_Segment]]]:
    """(header segments, body segments) per part; header segments repeat in every part of an entry."""
    header = [s for line in unit.header for s in _segments(layout, line, config.max_chars)]
    header_chars = sum(len(s.text) + 1 for s in header)
    if header and header_chars > config.max_chars // 2:
        # A header this long would crowd out the body: keep it as ordinary content instead of repeating it.
        body_lines, header = [*unit.header, *unit.body], []
        header_chars = 0
    else:
        body_lines = unit.body
    budget = max(config.max_chars - header_chars, config.max_chars // 3)
    body = [s for line in body_lines for s in _segments(layout, line, budget)]
    if not body:
        return [(header, [])] if header else []
    parts: list[list[_Segment]] = []
    current: list[_Segment] = []
    size = 0
    for segment in body:
        if current and size + len(segment.text) + 1 > budget:
            parts.append(current)
            overlap = current[-config.overlap_lines :] if config.overlap_lines else []
            while overlap and sum(len(s.text) + 1 for s in overlap) + len(segment.text) + 1 > budget:
                overlap = overlap[1:]
            current, size = list(overlap), sum(len(s.text) + 1 for s in overlap)
        current.append(segment)
        size += len(segment.text) + 1
    parts.append(current)
    return [(header, part) for part in parts]


def chunk_document(
    layout: DocumentLayout,
    anchors: list[EntryAnchor] | None = None,
    config: ChunkingConfig | None = None,
    ontology: SkillOntology | None = None,
) -> ChunkingResult:
    config = config or ChunkingConfig()
    ontology = ontology or load_ontology()
    units: list[_Unit] = []
    for region in _regions(layout):
        section = region[0].section
        if section in {"experience", "projects"}:
            units.extend(_anchored_units(region, anchors or []) or _heuristic_units(region))
        else:
            usable = [line for line in region if line.usable]
            if usable:
                units.append(_Unit(section, region[0].heading, body=usable))

    planned = [
        (unit, part, header, body) for unit in units for part, (header, body) in enumerate(_parts(layout, unit, config))
    ]
    chunks: list[Chunk] = []
    seen: set[str] = set()
    for unit, part, header, body in planned[: config.max_chunks]:
        segments = [*header, *body]
        joined = "\n".join(s.text for s in segments)
        text, redactions = redact_chunk_text(joined, header=unit.section == "header")
        digest = sha256_text(text)
        exclusion: ExclusionReason | None = None
        if only_placeholders(text):
            exclusion = "contact_only"
        elif digest in seen:
            exclusion = "duplicate"
        seen.add(digest)
        lines = [s.line for s in segments]
        pages = [line.page for line in lines if line.page is not None]
        skills = {m.canonical for m in ontology.find_mentions(text, text if unit.section == "skills" else None)}
        chunks.append(
            Chunk(
                ordinal=len(chunks),
                section=unit.section,
                heading=unit.heading,
                entry_key=unit.entry_key,
                entry_label=unit.entry_label,
                part=part,
                text=text,
                spans=tuple(dict.fromkeys((s.start, s.end) for s in segments)),
                line_start=min(line.index for line in lines),
                line_end=max(line.index for line in lines),
                page_start=min(pages) if pages else None,
                page_end=max(pages) if pages else None,
                skills=tuple(sorted(skills)),
                retrieval_eligible=exclusion is None,
                exclusion_reason=exclusion,
                injection_suspected=has_injection(text),
                redactions=sum(redactions.values()),
                text_sha256=digest,
            )
        )
    return ChunkingResult(chunks=chunks, truncated=len(planned) > config.max_chunks)
