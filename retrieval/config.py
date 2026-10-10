"""Retrieval configuration (environment variables prefixed ``RAG_``), separate from screening settings.

Provider credentials and model names come from the engine settings (``engine/config.py``): retrieval uses the
configured chat model for answers and the configured embedding model for vectors, never a different provider
as a silent fallback. Everything below is a retrieval policy or budget, grouped by capability.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RagSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Off by default: enabling it starts indexing and exposes the authenticated /v1/rag endpoints. Nothing is
    # ever exposed through the public portal.
    enabled: bool = Field(default=False, validation_alias="RAG_ENABLED")

    # --- Document preparation / indexing (background, priority 2 queue work) ---
    chunk_max_chars: int = Field(default=900, ge=200, le=4000, validation_alias="RAG_CHUNK_MAX_CHARS")
    chunk_overlap_lines: int = Field(default=1, ge=0, le=5, validation_alias="RAG_CHUNK_OVERLAP_LINES")
    index_max_chunks: int = Field(default=200, ge=1, le=2000, validation_alias="RAG_INDEX_MAX_CHUNKS")
    index_embed_batch: int = Field(default=16, ge=1, le=256, validation_alias="RAG_INDEX_EMBED_BATCH")
    # "auto": embed chunks when the engine's semantic backend resolves to embeddings; "off": keyword only.
    embeddings: Literal["auto", "off"] = Field(default="auto", validation_alias="RAG_EMBEDDINGS")
    # Pin the embedding space used for search (blue/green model migration). Unset: the configured model's space.
    search_space_id: str | None = Field(default=None, validation_alias="RAG_SEARCH_SPACE_ID")

    # --- Vector search ---
    # auto: pgvector when the extension column exists, else exact search over REAL[] vectors.
    vector_backend: Literal["auto", "exact", "pgvector"] = Field(default="auto", validation_alias="RAG_VECTOR_BACKEND")
    # Exact search loads the eligible vectors of one company; above this many the query fails loudly
    # (install pgvector) instead of silently sampling.
    exact_max_vectors: int = Field(default=50_000, ge=100, validation_alias="RAG_EXACT_MAX_VECTORS")
    hnsw_ef_search: int = Field(default=100, ge=10, le=1000, validation_alias="RAG_HNSW_EF_SEARCH")

    # --- Hybrid retrieval (capability: semantic candidate retrieval) ---
    search_max_query_chars: int = Field(default=500, ge=20, le=2000, validation_alias="RAG_SEARCH_MAX_QUERY_CHARS")
    search_max_results: int = Field(default=50, ge=1, le=200, validation_alias="RAG_SEARCH_MAX_RESULTS")
    # Chunks taken from each ranked list (keyword, vector) before fusion.
    search_pool: int = Field(default=200, ge=10, le=2000, validation_alias="RAG_SEARCH_POOL")
    search_passages_per_candidate: int = Field(default=3, ge=1, le=10, validation_alias="RAG_SEARCH_PASSAGES")
    # Reciprocal rank fusion constant and per-signal weights (tuned on labelled queries; see docs).
    fusion_k: int = Field(default=60, ge=1, le=1000, validation_alias="RAG_FUSION_K")
    fusion_keyword_weight: float = Field(default=1.0, ge=0, le=10, validation_alias="RAG_FUSION_KEYWORD_WEIGHT")
    fusion_vector_weight: float = Field(default=1.0, ge=0, le=10, validation_alias="RAG_FUSION_VECTOR_WEIGHT")
    # Optional reranking of the fused passages: "none" or "cross-encoder" (needs sentence-transformers).
    reranker: Literal["none", "cross-encoder"] = Field(default="none", validation_alias="RAG_RERANKER")
    reranker_model: str = Field(default="cross-encoder/ms-marco-MiniLM-L-6-v2", validation_alias="RAG_RERANKER_MODEL")
    rerank_top_n: int = Field(default=30, ge=1, le=200, validation_alias="RAG_RERANK_TOP_N")

    # --- Evidence-backed answers / comparison (interactive) ---
    # auto: generate narrative with the configured chat model when extraction mode resolves to llm; off:
    # extractive answers only (evidence passages and deterministic coverage, no generated text).
    generation: Literal["auto", "off"] = Field(default="auto", validation_alias="RAG_GENERATION")
    ask_max_question_chars: int = Field(default=500, ge=10, le=2000, validation_alias="RAG_ASK_MAX_QUESTION_CHARS")
    ask_max_passages: int = Field(default=8, ge=1, le=30, validation_alias="RAG_ASK_MAX_PASSAGES")
    ask_context_max_chars: int = Field(default=6000, ge=500, le=60000, validation_alias="RAG_ASK_CONTEXT_MAX_CHARS")
    ask_max_output_tokens: int = Field(default=700, ge=64, le=4096, validation_alias="RAG_ASK_MAX_OUTPUT_TOKENS")
    ask_timeout_s: float = Field(default=120.0, gt=0, le=900, validation_alias="RAG_ASK_TIMEOUT_S")
    ask_max_retries: int = Field(default=1, ge=0, le=3, validation_alias="RAG_ASK_MAX_RETRIES")
    ask_max_statements: int = Field(default=8, ge=1, le=20, validation_alias="RAG_ASK_MAX_STATEMENTS")
    compare_max_candidates: int = Field(default=5, ge=2, le=10, validation_alias="RAG_COMPARE_MAX_CANDIDATES")
    compare_passages_per_requirement: int = Field(default=2, ge=1, le=5, validation_alias="RAG_COMPARE_PASSAGES")

    # --- Resource control ---
    # Concurrent interactive model calls (answers, query embeddings) per API process; excess requests get 429.
    interactive_concurrency: int = Field(default=2, ge=1, le=64, validation_alias="RAG_INTERACTIVE_CONCURRENCY")
    interactive_wait_s: float = Field(default=2.0, ge=0, le=60, validation_alias="RAG_INTERACTIVE_WAIT_S")
    company_daily_questions: int = Field(default=500, ge=0, validation_alias="RAG_COMPANY_DAILY_QUESTIONS")
    company_daily_searches: int = Field(default=5000, ge=0, validation_alias="RAG_COMPANY_DAILY_SEARCHES")

    # --- Caches (process memory only; never persisted) ---
    cache_ttl_s: float = Field(default=300.0, ge=0, le=3600, validation_alias="RAG_CACHE_TTL_S")
    cache_max_entries: int = Field(default=256, ge=0, le=100_000, validation_alias="RAG_CACHE_MAX_ENTRIES")

    # --- Retention of interaction metadata (no prompts, answers or retrieved text are stored) ---
    session_retention_days: int = Field(default=90, ge=1, le=3650, validation_alias="RAG_SESSION_RETENTION_DAYS")

    @model_validator(mode="after")
    def _consistent(self) -> RagSettings:
        if self.search_passages_per_candidate > self.ask_max_passages:
            raise ValueError("RAG_SEARCH_PASSAGES must not exceed RAG_ASK_MAX_PASSAGES")
        if self.fusion_keyword_weight == 0 and self.fusion_vector_weight == 0:
            raise ValueError("At least one fusion weight must be positive")
        return self


@lru_cache(maxsize=1)
def get_rag_settings() -> RagSettings:
    return RagSettings()
