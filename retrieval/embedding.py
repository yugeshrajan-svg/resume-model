"""Embedding-space identity and embedding calls for retrieval.

An *embedding space* is everything that makes two vectors comparable: provider, model, the model's weights
digest (when the provider exposes it), document and query prefixes, how long inputs are pooled, the
normalization, and how chunk text is turned into model input. Vectors are stored per space id and search only
compares vectors of one space, so a model upgrade, a changed digest (``scripts/local_ai.py setup --rebuild``)
or a changed preprocessing step creates a new space that must be reindexed instead of mixing silently.

Limitation: Azure OpenAI does not expose weights digests; a deployment name can be repointed to another model
by an operator. The space records the deployment and the model name the API reports.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass

from engine.config import ConfigError, Settings
from engine.llm import EmbeddingProvider, LLMError, get_embedder
from retrieval.versions import EMBED_TEXT_VERSION


@dataclass(frozen=True)
class EmbeddingSpace:
    provider: str
    model: str
    model_digest: str | None
    document_prefix: str
    query_prefix: str
    pooling: str
    normalization: str = "l2"
    embed_text_version: str = EMBED_TEXT_VERSION

    def descriptor(self) -> dict[str, str | None]:
        return asdict(self)

    @property
    def id(self) -> str:
        canonical = json.dumps(self.descriptor(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:40]


def embeddings_configured(settings: Settings) -> bool:
    try:
        return settings.resolved_semantic_backend == "embeddings"
    except ConfigError:
        return False


def space_for_settings(settings: Settings) -> EmbeddingSpace | None:
    """The space the configured embedding model writes into, or None when embeddings are not configured.

    For the local provider, ``settings`` must carry the pinned digest (``engine.local_ai.pin_local_models``).
    """
    if not embeddings_configured(settings):
        return None
    if settings.llm_provider == "ollama":
        return EmbeddingSpace(
            provider="ollama",
            model=settings.ollama_embedding_model,
            model_digest=settings.ollama_embedding_digest,
            document_prefix="search_document: ",
            query_prefix="search_query: ",
            pooling=f"length-weighted-mean:{settings.ollama_embed_chunk_bytes}b",
        )
    return EmbeddingSpace(
        provider="azure_openai",
        model=settings.azure_openai_embedding_deployment,
        model_digest=None,
        document_prefix="",
        query_prefix="",
        pooling="none",
    )


def unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm or not math.isfinite(norm):
        raise LLMError("Embedding model returned a zero or invalid vector")
    return [v / norm for v in vector]


def embed_documents(settings: Settings, texts: list[str], embedder: EmbeddingProvider | None = None):
    """(unit vectors, usage) for chunk inputs, using the configured provider exactly as screening does."""
    embedder = embedder or get_embedder(settings)
    result = embedder.embed(texts)
    if len(result.vectors) != len(texts):
        raise LLMError("Embedding model returned the wrong number of vectors")
    return [unit(list(v)) for v in result.vectors], result.usage


def embed_query(settings: Settings, text: str, embedder: EmbeddingProvider | None = None):
    """(unit vector, usage) for a search query; models with task prefixes use their query prefix."""
    embedder = embedder or get_embedder(settings)
    method = getattr(embedder, "embed_query", embedder.embed)
    result = method([text])
    if len(result.vectors) != 1:
        raise LLMError("Embedding model returned the wrong number of vectors")
    return unit(list(result.vectors[0])), result.usage
