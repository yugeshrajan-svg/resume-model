"""Query analysis and structured filters for hybrid candidate retrieval.

A free-text query yields (a) the ontology skills it names, which are *reported* per candidate as matched or
missing evidence and boost keyword ranking, and (b) content terms for full-text search. Hard requirements are
given explicitly as ``SkillFilter``s; semantic similarity never satisfies a filter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from engine.normalize import SkillOntology, load_ontology
from retrieval.text import content_words

SupportFilter = Literal["any", "listed", "demonstrated"]
# Words that describe the search itself rather than what to find in a resume.
QUERY_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "from", "by", "with", "without", "about",
    "into", "over", "under", "than", "then", "that", "this", "these", "those", "is", "are", "was", "were", "be",
    "been", "being", "has", "have", "had", "having", "do", "does", "did", "can", "could", "should", "would",
    "will", "may", "might", "must", "i", "me", "my", "we", "our", "you", "your", "he", "she", "they", "them",
    "their", "it", "its", "who", "whom", "whose", "which", "what", "where", "when", "why", "how", "find", "show",
    "list", "give", "get", "search", "look", "looking", "need", "needs", "want", "wants", "please", "any", "all",
    "some", "each", "every", "candidate", "candidates", "applicant", "applicants", "resume", "resumes", "cv", "cvs",
    "profile", "profiles", "person", "people", "someone", "anyone", "experience", "experienced", "years", "year",
    "skill", "skills", "knowledge", "background", "familiarity", "familiar", "exposure", "strong", "good",
    "worked", "work", "working", "using", "used", "use", "uses", "also", "both", "either", "well", "plus",
    "mention", "mentions", "mentioned", "evidence", "support", "supports", "supported", "mentioning",
})  # fmt: skip


class SkillFilter(BaseModel):
    """A hard requirement: the candidate's current resume version must evidence this skill."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    support: SupportFilter = Field(
        default="any",
        description="demonstrated: named in a work or project entry; listed: named in a skills section; any: either",
    )

    def canonical(self, ontology: SkillOntology | None = None) -> str:
        ontology = ontology or load_ontology()
        return ontology.canonicalize(self.name) or " ".join(self.name.split())


@dataclass(frozen=True)
class QueryAnalysis:
    text: str
    skills: tuple[str, ...]  # canonical skills named in the query
    terms: tuple[str, ...]  # keyword-search terms: canonical skill names, then content words

    @property
    def empty(self) -> bool:
        return not self.terms


def analyze_query(text: str, ontology: SkillOntology | None = None) -> QueryAnalysis:
    ontology = ontology or load_ontology()
    mentions = sorted(ontology.find_mentions(text), key=lambda m: text.casefold().find(m.matched.casefold()))
    skills = tuple(dict.fromkeys(m.canonical for m in mentions))
    rest = text
    for m in sorted(mentions, key=lambda m: -len(m.matched)):
        rest = re.sub(re.escape(m.matched), " ", rest, flags=re.IGNORECASE)
    words = [w for w in content_words(rest, QUERY_STOPWORDS) if not w.isdigit()]
    terms = tuple(dict.fromkeys([*(s.casefold() for s in skills), *words]))
    return QueryAnalysis(text=text, skills=skills, terms=terms[:24])
