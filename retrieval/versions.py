"""Versions of every retrieval policy that changes stored or returned results.

Bump a version on ANY change to the behaviour it names. Versions are stored with index versions, fact sets,
chunks and retrieval sessions, so results stay traceable and stale derived data is detected, never mixed.
"""

from __future__ import annotations

# Line, section, page, noise and protected-content analysis of a resume version's redacted text.
LAYOUT_VERSION = "layout-1"
# Additional redaction applied to chunk text (URLs, residual contacts, postal addresses).
REDACTION_VERSION = "rag-redaction-1"
# Chunk boundaries, chunk text and exclusion rules. A new version requires reindexing (old chunks are not
# reused). Chunk size/overlap are recorded separately because they are configurable.
CHUNKING_VERSION = "chunk-1"
# How the text sent to an embedding model is built from a chunk (heading prefix etc.).
EMBED_TEXT_VERSION = "embed-text-1"
# Fact-set JSON shape, and the rules that derive facts, warnings, conflicts and review items.
FACTS_SCHEMA_VERSION = "1"
FACTS_POLICY_VERSION = "facts-1"
# Eligibility (consent scope, current-version selection) and hybrid ranking configuration.
RETRIEVAL_POLICY_VERSION = "retrieval-1"
# Answer generation prompt and citation validation rules.
ANSWER_PROMPT_VERSION = "answer-1.0.0"
CITATION_POLICY_VERSION = "citations-1"
