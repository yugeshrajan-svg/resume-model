"""Optional reranking of fused passages (RAG_RERANKER). Off by default: see docs/rag-evaluation.md.

A reranker only reorders the top ``RAG_RERANK_TOP_N`` passages that retrieval already found; it cannot add a
passage, satisfy a filter, validate a qualification or influence screening. If it fails, the fused order is
returned with ``status="failed"`` so callers can report the fallback; nothing is generated from passages the
reranker did not see.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from retrieval.query import QUERY_STOPWORDS
from retrieval.text import content_words, light_stem


class Reranker(Protocol):
    name: str

    def scores(self, query: str, passages: Sequence[str]) -> list[float]: ...


@dataclass(frozen=True)
class RerankOutcome:
    order: list[int]  # indices into the input passages, best first
    status: Literal["applied", "skipped", "failed"]
    reranker: str | None
    scores: list[float | None]  # per input passage; None where not reranked


def rerank(reranker: Reranker | None, query: str, passages: Sequence[str], *, top_n: int) -> RerankOutcome:
    """Reorder the first ``top_n`` passages; the remainder keep their fused order after them."""
    identity = list(range(len(passages)))
    if reranker is None or not passages:
        return RerankOutcome(identity, "skipped", None, [None] * len(passages))
    head = list(passages[:top_n])
    try:
        values = reranker.scores(query, head)
        if len(values) != len(head) or not all(math.isfinite(v) for v in values):
            raise ValueError("reranker returned invalid scores")
    except Exception:  # optional component: any failure falls back to the fused order, reported explicitly
        return RerankOutcome(identity, "failed", reranker.name, [None] * len(passages))
    order = sorted(range(len(head)), key=lambda i: (-values[i], i)) + identity[len(head) :]
    scores: list[float | None] = [*values, *([None] * (len(passages) - len(head)))]
    return RerankOutcome(order, "applied", reranker.name, scores)


class TermCoverageReranker:
    """Deterministic baseline: share of query content terms (stemmed) a passage contains."""

    name = "term-coverage"

    def scores(self, query: str, passages: Sequence[str]) -> list[float]:
        wanted = {light_stem(w) for w in content_words(query, QUERY_STOPWORDS)}
        if not wanted:
            return [0.0] * len(passages)
        return [
            len(wanted & {light_stem(w) for w in content_words(p, QUERY_STOPWORDS)}) / len(wanted) for p in passages
        ]


class CrossEncoderReranker:
    """sentence-transformers cross-encoder (``pip install .[rerank]``); raw logits passed through a sigmoid."""

    def __init__(self, model_name: str) -> None:
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:  # optional heavy dependency
            raise RuntimeError("RAG_RERANKER=cross-encoder requires the sentence-transformers package") from exc
        self.name = f"cross-encoder:{model_name}"
        self._model = CrossEncoder(model_name)

    def scores(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        logits = self._model.predict([(query, p) for p in passages])
        return [1.0 / (1.0 + math.exp(-float(x))) for x in logits]
