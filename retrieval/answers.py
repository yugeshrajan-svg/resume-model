"""Evidence-backed answers: bounded context, structured drafts and deterministic citation validation.

A model drafts statements that cite evidence ids. Nothing it writes is returned until every statement passes
deterministic checks (CITATION_POLICY_VERSION):

1. It cites at least one id, and every cited id is one of the passages it was given.
2. Every skill, number and capitalized name in the statement appears in the cited passages (alias-aware for
   ontology skills), and most of its content words do (light stemming, a few verb synonyms).
3. It makes no hiring recommendation or character judgement, uses no gendered pronoun, and mentions no
   protected characteristic that its evidence does not itself contain.

Statements that fail are rejected and counted. If none survive, the answer is the abstention
``NOT_FOUND`` — the model's own knowledge never fills a gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from engine.normalize import SkillOntology, load_ontology
from engine.prompts import fence_untrusted
from retrieval.safety import evaluative, gendered, neutralize, ungrounded_protected_terms
from retrieval.text import content_words, light_stem

NOT_FOUND = "Not found in the available resume evidence."
RejectReason = Literal[
    "no_citation",
    "unknown_citation",
    "unsupported_terms",
    "low_overlap",
    "evaluative_language",
    "protected_attribute",
    "gendered_language",
    "too_long",
    "over_limit",
    "duplicate",
    "empty",
]
MIN_CONTENT_OVERLAP = 0.6
MAX_STATEMENT_CHARS = 400
_GENERIC = frozenset({
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for", "from", "by", "with", "as", "is", "are",
    "was", "were", "be", "been", "has", "have", "had", "this", "that", "these", "those", "it", "its", "their",
    "they", "them", "candidate", "candidates", "resume", "resumes", "cv", "experience", "experienced", "role",
    "roles", "position", "positions", "job", "jobs", "shows", "show", "showed", "shown", "demonstrates",
    "demonstrated", "demonstrate", "lists", "listed", "list", "mentions", "mentioned", "mention", "states",
    "stated", "state", "includes", "including", "include", "also", "various", "multiple", "several", "skill",
    "skills", "section", "evidence", "passage", "passages", "worked", "work", "working", "using", "used", "use",
    "uses", "utilized", "leveraged", "applied", "years", "year", "professional", "professionally", "responsible",
    "responsibilities",
})  # fmt: skip
# Resume verbs a faithful paraphrase may swap; each maps to one class for overlap checks.
_VERB_CLASSES = {
    word: verb_class
    for verb_class, words in {
        "~build": ("built", "build", "builds", "developed", "develop", "develops", "created", "create", "creates",
                   "implemented", "implement", "implements", "designed", "design", "designs", "wrote", "write",
                   "writes", "engineered", "engineer", "delivered", "deliver", "shipped", "ship"),
        "~lead": ("led", "lead", "leads", "managed", "manage", "manages", "headed", "head", "supervised",
                  "supervise", "directed", "direct", "oversaw", "oversee"),
        "~deploy": ("deployed", "deploy", "deploys", "released", "release", "launched", "launch"),
    }.items()
    for word in words
}  # fmt: skip
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?\s?(?:%|[kKmMbB]\b)?")
_CAPITALIZED = re.compile(r"\b[A-Z][A-Za-z0-9+#.&-]*[A-Za-z0-9+#]\b|\b[A-Z]{2,}\b")
_CAPITAL_ALLOW = frozenset({
    "The", "Candidate", "Their", "They", "This", "These", "It", "In", "At", "As", "A", "An", "Experience", "Skills",
    "Projects", "Education", "Summary", "Certifications", "Profile",
})  # fmt: skip


class EvidencePassage(BaseModel):
    """A retrievable chunk as shown to a model and returned to the client (resolved by ``evidence_id``)."""

    evidence_id: str
    candidate_id: str
    resume_id: str
    resume_version: int | None
    section: str
    heading: str | None
    entry_label: str | None
    page_start: int | None
    text: str


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftStatement(_Strict):
    text: str = Field(description="One factual sentence about the candidate")
    evidence_ids: list[str] = Field(description="Ids of the passages that state every fact in the sentence")


class AnswerDraft(_Strict):
    """Model response format. Limits and citations are enforced after the call."""

    statements: list[DraftStatement]
    not_found: list[str] = Field(description="Parts of the question the passages do not answer")


class ValidatedStatement(BaseModel):
    text: str
    evidence_ids: list[str]


class RejectedStatement(BaseModel):
    reason: RejectReason
    detail: str | None = None  # the unsupported terms, never the statement text (it may be unfounded)


class AnswerValidation(BaseModel):
    statements: list[ValidatedStatement]
    rejected: list[RejectedStatement]
    not_found: list[str]
    abstained: bool

    @property
    def answer(self) -> str:
        if self.abstained:
            return NOT_FOUND
        return " ".join(f"{s.text} [{', '.join(s.evidence_ids)}]" for s in self.statements)


@dataclass(frozen=True)
class Context:
    text: str  # passages block for the prompt (neutralized, fenced)
    passages: list[EvidencePassage]  # passages included, in prompt order
    labels: dict[str, str]  # prompt label ("E1") -> evidence_id
    truncated: int  # passages left out by the budget


def build_context(passages: list[EvidencePassage], *, max_chars: int, max_passages: int) -> Context:
    """Number passages E1..En in relevance order within the character and passage budgets."""
    blocks, included, labels = [], [], {}
    used = 0
    for passage in passages:
        if len(included) >= max_passages:
            break
        label = f"E{len(included) + 1}"
        where = " | ".join(filter(None, [passage.section.title(), passage.entry_label or passage.heading]))
        body = fence_untrusted(neutralize(passage.text))
        block = f"[{label}] ({where})\n{body}"
        if used + len(block) > max_chars:
            if not included:  # one oversized passage: include a truncated copy rather than nothing
                block = block[:max_chars]
            else:
                break
        blocks.append(block)
        included.append(passage)
        labels[label] = passage.evidence_id
        used += len(block) + 2
    return Context("\n\n".join(blocks), included, labels, len(passages) - len(included))


def _stems(words: list[str]) -> set[str]:
    return {_VERB_CLASSES.get(w, light_stem(w)) for w in words}


def unsupported_terms(statement: str, evidence: str, ontology: SkillOntology) -> list[str]:
    """Skills, numbers and names in ``statement`` that ``evidence`` does not contain."""
    missing = []
    evidence_skills = {m.canonical for m in ontology.find_mentions(evidence)}
    for m in ontology.find_mentions(statement):
        if m.canonical not in evidence_skills:
            missing.append(m.canonical)
    flat_evidence = evidence.replace(",", "")
    for m in _NUMBER.finditer(statement):
        number = m.group(0).replace(",", "").replace(" ", "")
        if number.rstrip("%kKmMbB") and number not in flat_evidence.replace(" ", ""):
            missing.append(m.group(0).strip())
    lowered = evidence.casefold()
    skill_words = {w.casefold() for m in ontology.find_mentions(statement) for w in m.matched.split()}
    for m in _CAPITALIZED.finditer(statement):
        word = m.group(0)
        sentence_start = not statement[: m.start()].strip() or statement[: m.start()].rstrip().endswith((".", ":", ";"))
        if word in _CAPITAL_ALLOW or word.casefold() in skill_words or (sentence_start and not word.isupper()):
            continue
        if word.casefold() not in lowered:
            missing.append(word)
    return list(dict.fromkeys(missing))


def content_overlap(statement: str, evidence: str) -> float:
    words = content_words(statement, _GENERIC)
    if not words:
        return 1.0
    have = _stems(content_words(evidence, _GENERIC))
    stems = _stems(words)
    return len(stems & have) / len(stems)


def validate_answer(
    draft: AnswerDraft,
    context: Context,
    *,
    max_statements: int,
    ontology: SkillOntology | None = None,
) -> AnswerValidation:
    ontology = ontology or load_ontology()
    by_label = {label: p for label, p in zip(context.labels, context.passages, strict=True)}
    by_id = {p.evidence_id: p for p in context.passages}
    kept: list[ValidatedStatement] = []
    rejected: list[RejectedStatement] = []
    seen: set[str] = set()
    for statement in draft.statements:
        text = " ".join(statement.text.split())
        cited: list[EvidencePassage] = []
        unknown = False
        for ref in statement.evidence_ids:
            ref = ref.strip().strip("[]")
            passage = by_label.get(ref) or by_id.get(ref)
            if passage is None:
                unknown = True
            elif passage not in cited:
                cited.append(passage)
        reason: RejectReason | None = None
        detail = None
        evidence = "\n".join(p.text for p in cited)
        if not text:
            reason = "empty"
        elif len(text) > MAX_STATEMENT_CHARS:
            reason = "too_long"
        elif not statement.evidence_ids:
            reason = "no_citation"
        elif unknown or not cited:
            reason = "unknown_citation"
        elif evaluative(text):
            reason = "evaluative_language"
        elif gendered(text):
            reason = "gendered_language"
        elif terms := ungrounded_protected_terms(text, evidence):
            reason, detail = "protected_attribute", ", ".join(sorted(terms))
        elif missing := unsupported_terms(text, evidence, ontology):
            reason, detail = "unsupported_terms", ", ".join(missing[:8])
        elif content_overlap(text, evidence) < MIN_CONTENT_OVERLAP:
            reason = "low_overlap"
        elif text.casefold() in seen:
            reason = "duplicate"
        elif len(kept) >= max_statements:
            reason = "over_limit"
        if reason is not None:
            rejected.append(RejectedStatement(reason=reason, detail=detail))
            continue
        seen.add(text.casefold())
        kept.append(ValidatedStatement(text=text, evidence_ids=[p.evidence_id for p in cited]))
    not_found = [" ".join(item.split())[:200] for item in draft.not_found if item.strip()][:10]
    return AnswerValidation(statements=kept, rejected=rejected, not_found=not_found, abstained=not kept)
