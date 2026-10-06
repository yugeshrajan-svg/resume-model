"""Text similarity backends for semantic features.

* ``TfidfScorer`` (offline): TF-IDF fitted on the job's own text (one document
  per line), so a candidate's similarity depends only on the job and that
  candidate — never on who else is in the batch. Skill aliases are expanded to
  canonical tokens ("k8s" and "Kubernetes" match).
* ``EmbeddingScorer`` (online): Azure OpenAI embeddings + cosine similarity.
* ``CrossEncoderReranker`` (optional, ``SEMANTIC_RERANKER=bge-reranker-v2-m3``):
  scores (job, candidate) pairs jointly with a cross-encoder. Requires the
  ``sentence-transformers`` package and model weights; enabling it without
  them fails loudly at startup.

All scorers return similarities in [0, 1].
"""

from __future__ import annotations

import logging
import math
import re
import threading
from typing import Protocol

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from engine.config import ConfigError, Settings, get_settings
from engine.llm import EmbeddingProvider
from engine.logging_setup import log_event
from engine.normalize import SkillOntology, load_ontology
from engine.schemas import LLMUsage

logger = logging.getLogger("engine.semantic")

MAX_EMBED_CHARS = 24_000  # stays well under the 8K-token limit of text-embedding-3 models


class SemanticScorer(Protocol):
    @property
    def name(self) -> str: ...

    def similarities(self, query: str, docs: list[str]) -> list[float]:
        """Similarity in [0, 1] between ``query`` and each doc, in order."""
        ...


def skill_tokens(text: str, ontology: SkillOntology) -> str:
    """Canonical skill tokens for the skills mentioned in ``text`` (e.g. "skill_kubernetes")."""
    return " ".join(
        "skill_" + re.sub(r"\W+", "_", m.canonical.lower()).strip("_") for m in ontology.find_mentions(text)
    )


class TfidfScorer:
    name = "tfidf"

    def __init__(self, job_text: str, ontology: SkillOntology | None = None) -> None:
        self._ontology = ontology or load_ontology()
        lines = [ln for ln in job_text.split("\n") if ln.strip()] or [job_text]
        self._vectorizer = TfidfVectorizer(
            lowercase=True, stop_words="english", ngram_range=(1, 2), sublinear_tf=True, norm="l2"
        )
        self._vectorizer.fit([self._expand(ln) for ln in lines])

    def _expand(self, text: str) -> str:
        return f"{text}\n{skill_tokens(text, self._ontology)}"

    def similarities(self, query: str, docs: list[str]) -> list[float]:
        if not docs:
            return []
        q = self._vectorizer.transform([self._expand(query)])
        d = self._vectorizer.transform([self._expand(doc) for doc in docs])
        sims = (d @ q.T).toarray().ravel()  # rows are L2-normalized => dot product == cosine
        return [float(min(max(s, 0.0), 1.0)) for s in sims]


class EmbeddingScorer:
    name = "embeddings"

    def __init__(self, provider: EmbeddingProvider, *, local: bool = False) -> None:
        self._provider = provider
        self._local = local
        if local:
            self.name = "ollama-embeddings"
        self._cache: dict[str, np.ndarray] = {}
        self._lock = threading.Lock()  # screening runs documents concurrently
        self.usage_tokens = 0
        self.usage_cost_usd = 0.0

    def _vectors(self, texts: list[str]) -> list[np.ndarray]:
        with self._lock:
            return self._vectors_locked(texts)

    def _vectors_locked(self, texts: list[str]) -> list[np.ndarray]:
        missing = list(dict.fromkeys(t for t in texts if t not in self._cache))
        if missing:
            result = self._provider.embed(missing if self._local else [t[:MAX_EMBED_CHARS] for t in missing])
            self._record(result.usage)
            for text, vec in zip(missing, result.vectors, strict=True):
                arr = np.asarray(vec, dtype=float)
                norm = np.linalg.norm(arr)
                self._cache[text] = arr / norm if norm else arr
        return [self._cache[t] for t in texts]

    def _record(self, usage: LLMUsage) -> None:
        self.usage_tokens += usage.prompt_tokens
        self.usage_cost_usd += usage.cost_usd

    def similarities(self, query: str, docs: list[str]) -> list[float]:
        if not docs:
            return []
        q, *ds = self._vectors([query, *docs])
        return [float(min(max(float(q @ d), 0.0), 1.0)) for d in ds]


class CrossEncoderReranker:
    name = "cross-encoder"

    def __init__(self, model_name: str) -> None:
        try:
            import torch
            from sentence_transformers import CrossEncoder
        except ImportError as exc:  # optional heavy dependency
            raise ConfigError(
                "SEMANTIC_RERANKER requires the 'sentence-transformers' package (and torch); "
                "install it or set SEMANTIC_RERANKER=none"
            ) from exc
        self._identity = torch.nn.Identity()
        self._model = CrossEncoder(model_name)
        self.model_name = model_name

    def similarities(self, query: str, docs: list[str]) -> list[float]:
        if not docs:
            return []
        # Request raw logits and apply the sigmoid ourselves: the default activation differs by model.
        logits = self._model.predict([(query, d) for d in docs], activation_fn=self._identity)
        return [1.0 / (1.0 + math.exp(-float(x))) for x in np.asarray(logits).ravel()]


def build_semantic_scorer(
    job_text: str,
    settings: Settings | None = None,
    embedder: EmbeddingProvider | None = None,
    ontology: SkillOntology | None = None,
) -> SemanticScorer:
    """The run-level similarity backend (SEMANTIC_BACKEND, defaulting by credential presence)."""
    settings = settings or get_settings()
    backend = settings.resolved_semantic_backend
    if backend == "embeddings":
        if embedder is None:
            from engine.llm import get_embedder

            embedder = get_embedder(settings)
        scorer: SemanticScorer = EmbeddingScorer(embedder, local=settings.llm_provider == "ollama")
    else:
        scorer = TfidfScorer(job_text, ontology)
    log_event(logger, "semantic_backend_selected", backend=scorer.name)
    return scorer


def build_reranker(settings: Settings | None = None) -> CrossEncoderReranker | None:
    settings = settings or get_settings()
    if settings.semantic_reranker == "none":
        return None
    return CrossEncoderReranker(settings.reranker_model)
