"""Runtime configuration, loaded exclusively from environment variables (or a .env file)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Local open-weight models: explicit selection never falls back to a paid provider.
    llm_provider: Literal["azure_openai", "ollama"] = Field(default="azure_openai", validation_alias="LLM_PROVIDER")
    ollama_base_url: str = Field(default="http://127.0.0.1:11434", validation_alias="OLLAMA_BASE_URL")
    ollama_chat_model: str = Field(default="shortlist-local:v1", min_length=1, validation_alias="OLLAMA_CHAT_MODEL")
    ollama_embedding_model: str = Field(
        default="nomic-embed-text:v1.5", min_length=1, validation_alias="OLLAMA_EMBEDDING_MODEL"
    )
    ollama_chat_digest: str | None = Field(default=None, validation_alias="OLLAMA_CHAT_DIGEST")
    ollama_embedding_digest: str | None = Field(default=None, validation_alias="OLLAMA_EMBEDDING_DIGEST")
    ollama_num_ctx: int = Field(default=16384, ge=1024, le=65536, validation_alias="OLLAMA_NUM_CTX")
    ollama_num_predict: int = Field(default=2048, ge=128, validation_alias="OLLAMA_NUM_PREDICT")
    ollama_keep_alive: str = Field(default="2m", validation_alias="OLLAMA_KEEP_ALIVE")
    ollama_embed_chunk_bytes: int = Field(default=1500, ge=128, le=1500, validation_alias="OLLAMA_EMBED_CHUNK_BYTES")
    # Model-server capacity, applied by `scripts/local_ai.py serve`. 1/1 suits an 8 GB laptop; a machine with a
    # GPU can serve several requests (match WORKER_CONCURRENCY) and keep chat + embedding models loaded together.
    ollama_num_parallel: int = Field(default=1, ge=1, le=8, validation_alias="OLLAMA_NUM_PARALLEL")
    ollama_max_loaded_models: int = Field(default=1, ge=1, le=4, validation_alias="OLLAMA_MAX_LOADED_MODELS")

    @field_validator("ollama_base_url")
    @classmethod
    def _local_ollama_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("OLLAMA_BASE_URL must be a loopback HTTP URL without credentials or a path")
        return value.rstrip("/")

    @field_validator("ollama_chat_model", "ollama_embedding_model")
    @classmethod
    def _local_model_name(cls, value: str) -> str:
        value = value.strip()
        if not value or "cloud" in value.casefold() or "://" in value:
            raise ValueError("Use a downloaded local model, not an Ollama cloud model")
        return value

    @model_validator(mode="after")
    def _local_context_limits(self) -> Settings:
        if self.ollama_num_predict >= self.ollama_num_ctx:
            raise ValueError("OLLAMA_NUM_PREDICT must be smaller than OLLAMA_NUM_CTX")
        if self.llm_provider == "ollama" and self.ocr_provider == "azure":
            raise ValueError("Use OCR_PROVIDER=tesseract or none with the free local provider")
        return self

    # --- Azure OpenAI (AI gateway). Absent endpoint/key => offline mode. ---
    azure_openai_endpoint: str | None = Field(default=None, validation_alias="AZURE_OPENAI_ENDPOINT")
    azure_openai_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_KEY")
    )
    azure_openai_api_version: str = Field(default="2024-10-21", validation_alias="AZURE_OPENAI_API_VERSION")
    azure_openai_chat_deployment: str = Field(default="gpt-4o", validation_alias="AZURE_OPENAI_CHAT_DEPLOYMENT")
    azure_openai_embedding_deployment: str = Field(
        default="text-embedding-3-small", validation_alias="AZURE_OPENAI_EMBEDDING_DEPLOYMENT"
    )
    llm_timeout_s: float = Field(default=60.0, gt=0, validation_alias="LLM_TIMEOUT_S")
    # Retries after the first failed attempt, with exponential backoff (base * 2**attempt, plus jitter).
    llm_max_retries: int = Field(default=3, ge=0, validation_alias="LLM_MAX_RETRIES")
    llm_backoff_base_s: float = Field(default=1.0, ge=0, validation_alias="LLM_BACKOFF_BASE_S")
    # USD per 1K tokens, used only for cost logging.
    llm_price_input_per_1k: float = Field(default=0.0025, ge=0, validation_alias="LLM_PRICE_INPUT_PER_1K")
    llm_price_output_per_1k: float = Field(default=0.01, ge=0, validation_alias="LLM_PRICE_OUTPUT_PER_1K")
    llm_price_embedding_per_1k: float = Field(default=0.00002, ge=0, validation_alias="LLM_PRICE_EMBEDDING_PER_1K")

    # --- Run-level modes (never mixed within one batch). Unset => chosen from credential presence. ---
    extraction_mode: Literal["llm", "rules"] | None = Field(default=None, validation_alias="EXTRACTION_MODE")
    semantic_backend: Literal["embeddings", "tfidf"] | None = Field(default=None, validation_alias="SEMANTIC_BACKEND")
    # Optional cross-encoder for semantic_relevance (needs the sentence-transformers package + model weights).
    semantic_reranker: Literal["none", "bge-reranker-v2-m3"] = Field(
        default="none", validation_alias="SEMANTIC_RERANKER"
    )
    reranker_model: str = Field(default="BAAI/bge-reranker-v2-m3", validation_alias="RERANKER_MODEL")
    # "template" writes strengths/gaps from feature results without a second model call (faster on CPU).
    # Explanations never change scores.
    explanation_mode: Literal["llm", "template"] = Field(default="llm", validation_alias="EXPLANATION_MODE")

    # --- Parsing ---
    max_file_bytes: int = Field(default=5 * 1024 * 1024, gt=0, validation_alias="MAX_FILE_BYTES")
    ocr_min_chars: int = Field(default=200, ge=0, validation_alias="OCR_MIN_CHARS")
    hidden_min_font_pt: float = Field(default=4.0, ge=0, validation_alias="HIDDEN_MIN_FONT_PT")
    # A colour whose R, G and B channels are all >= this (0-255) counts as "near-white".
    hidden_near_white_min: int = Field(default=240, ge=0, le=255, validation_alias="HIDDEN_NEAR_WHITE_MIN")
    # Share of a span's bbox that must lie outside the page for it to count as off-page.
    hidden_offpage_ratio: float = Field(default=0.5, gt=0, le=1, validation_alias="HIDDEN_OFFPAGE_RATIO")

    # --- OCR (Azure Document Intelligence, prebuilt-read) for scanned PDFs ---
    azure_docintel_endpoint: str | None = Field(default=None, validation_alias="AZURE_DOCINTEL_ENDPOINT")
    azure_docintel_key: SecretStr | None = Field(default=None, validation_alias="AZURE_DOCINTEL_KEY")
    ocr_timeout_s: float = Field(default=120.0, gt=0, validation_alias="OCR_TIMEOUT_S")
    ocr_price_per_page: float = Field(default=0.0015, ge=0, validation_alias="OCR_PRICE_PER_PAGE")
    ocr_provider: Literal["auto", "none", "tesseract", "azure"] = Field(default="auto", validation_alias="OCR_PROVIDER")
    tesseract_data_dir: Path = Field(default=Path(".cache/local-ai/tessdata"), validation_alias="TESSERACT_DATA_DIR")
    tesseract_language: str = Field(default="eng", validation_alias="TESSERACT_LANGUAGE")
    tesseract_dpi: int = Field(default=200, ge=100, le=400, validation_alias="TESSERACT_DPI")
    ocr_max_pages: int = Field(default=30, ge=1, le=200, validation_alias="OCR_MAX_PAGES")
    ocr_max_page_pixels: int = Field(default=20_000_000, ge=1_000_000, validation_alias="OCR_MAX_PAGE_PIXELS")

    @field_validator("tesseract_language")
    @classmethod
    def _ocr_languages(cls, value: str) -> str:
        if re.fullmatch(r"[a-zA-Z0-9_]+(?:\+[a-zA-Z0-9_]+)*", value) is None:
            raise ValueError("TESSERACT_LANGUAGE must contain language codes such as eng or eng+hin")
        return value

    # --- Extraction ---
    max_resume_chars: int = Field(default=40_000, gt=0, validation_alias="MAX_RESUME_CHARS")
    # Job-independent extraction results are cached (see engine/cache.py).
    extraction_cache_enabled: bool = Field(default=True, validation_alias="EXTRACTION_CACHE_ENABLED")
    extraction_cache_dir: Path = Field(default=Path(".cache/extractions"), validation_alias="EXTRACTION_CACHE_DIR")
    # Resumes processed concurrently per screening run (LLM calls are I/O bound).
    screen_max_workers: int = Field(default=4, ge=1, le=32, validation_alias="SCREEN_MAX_WORKERS")

    # --- Knockouts (job-level required_skills_min_match overrides the ratio) ---
    # Share of required skills a candidate must have when the job doesn't say: ceil(0.8 * n) by default.
    knockout_required_skill_ratio: float = Field(
        default=0.8, gt=0, le=1, validation_alias="KNOCKOUT_REQUIRED_SKILL_RATIO"
    )
    # Grace period: candidates this many months short of experience_min_years still pass.
    knockout_experience_tolerance_months: int = Field(
        default=0, ge=0, validation_alias="KNOCKOUT_EXPERIENCE_TOLERANCE_MONTHS"
    )

    # --- Scoring (Phase 3) ---
    # Feature name -> weight; normalized to sum 1. A job's score_weights override these entirely.
    score_weights: dict[str, float] = Field(
        default={
            "required_skill_coverage": 0.30,
            "relevant_experience": 0.20,
            "semantic_relevance": 0.20,
            "project_relevance": 0.10,
            "seniority_fit": 0.10,
            "preferred_skill_coverage": 0.10,
        },
        validation_alias="SCORE_WEIGHTS",
    )
    # Skill recency: full weight if last used within this many months...
    skill_recency_full_months: int = Field(default=36, ge=0, validation_alias="SKILL_RECENCY_FULL_MONTHS")
    # ...then linear decay, reaching the floor weight at this age (10 years) and staying there.
    skill_recency_floor_months: int = Field(default=120, gt=0, validation_alias="SKILL_RECENCY_FLOOR_MONTHS")
    skill_recency_min_weight: float = Field(default=0.6, ge=0, le=1, validation_alias="SKILL_RECENCY_MIN_WEIGHT")
    # Weight for a verified skill not tied to any dated role (e.g. only listed in a skills section).
    skill_unknown_recency_weight: float = Field(
        default=0.5, ge=0, le=1, validation_alias="SKILL_UNKNOWN_RECENCY_WEIGHT"
    )
    # A job counts toward relevant_experience when its relevance (title/skill overlap) reaches this.
    relevant_job_threshold: float = Field(default=0.5, gt=0, le=1, validation_alias="RELEVANT_JOB_THRESHOLD")

    # --- API ---
    # When set, every API request except /health must send it in the X-API-Key header.
    api_key: SecretStr | None = Field(default=None, validation_alias="API_KEY")
    max_files_per_request: int = Field(default=100, ge=1, validation_alias="MAX_FILES_PER_REQUEST")
    max_jd_chars: int = Field(default=50_000, gt=0, validation_alias="MAX_JD_CHARS")
    # Finished screening jobs kept in memory for GET /screen/{job_id}; the oldest finished ones are evicted.
    max_stored_jobs: int = Field(default=500, ge=1, validation_alias="MAX_STORED_JOBS")

    # --- Evaluation ---
    # Fairness counterfactuals in eval: "ci" = 3 job/resume cases x 6 name variants (cheap enough for every
    # online CI run); "full" = every job x 3 base resumes x all 27 variants.
    fairness_scope: Literal["ci", "full"] = Field(default="ci", validation_alias="FAIRNESS_SCOPE")

    # --- Logging ---
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    log_json: bool = Field(default=True, validation_alias="LOG_JSON")

    @model_validator(mode="after")
    def _used_skills_outweigh_listed_only(self) -> Settings:
        # A skill shown in any dated role, however old, must outweigh one that is merely listed.
        if not self.skill_recency_min_weight > self.skill_unknown_recency_weight:
            raise ValueError("SKILL_RECENCY_MIN_WEIGHT must be greater than SKILL_UNKNOWN_RECENCY_WEIGHT")
        if self.skill_recency_floor_months <= self.skill_recency_full_months:
            raise ValueError("SKILL_RECENCY_FLOOR_MONTHS must be greater than SKILL_RECENCY_FULL_MONTHS")
        return self

    @property
    def has_azure_credentials(self) -> bool:
        return bool(self.azure_openai_endpoint and self.azure_openai_api_key)

    @property
    def resolved_extraction_mode(self) -> Literal["llm", "rules"]:
        """The extraction mode for this run. Fails loudly if "llm" is requested without credentials."""
        if self.extraction_mode is None:
            return "llm" if self.llm_provider == "ollama" or self.has_azure_credentials else "rules"
        if self.extraction_mode == "llm" and self.llm_provider == "azure_openai" and not self.has_azure_credentials:
            raise ConfigError("EXTRACTION_MODE=llm requires AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY")
        return self.extraction_mode

    @property
    def resolved_semantic_backend(self) -> Literal["embeddings", "tfidf"]:
        """Semantic similarity backend; local embeddings need installed weights, Azure needs credentials."""
        if self.semantic_backend is None:
            return "embeddings" if self.llm_provider == "ollama" or self.has_azure_credentials else "tfidf"
        if (
            self.semantic_backend == "embeddings"
            and self.llm_provider == "azure_openai"
            and not self.has_azure_credentials
        ):
            raise ConfigError("SEMANTIC_BACKEND=embeddings requires AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY")
        return self.semantic_backend

    @property
    def chat_model(self) -> str:
        return self.ollama_chat_model if self.llm_provider == "ollama" else self.azure_openai_chat_deployment

    @property
    def embedding_model(self) -> str:
        return self.ollama_embedding_model if self.llm_provider == "ollama" else self.azure_openai_embedding_deployment

    @property
    def resolved_ocr_provider(self) -> Literal["none", "tesseract", "azure"]:
        if self.ocr_provider != "auto":
            return self.ocr_provider
        if self.llm_provider == "ollama":
            return "tesseract"
        return "azure" if self.azure_docintel_endpoint and self.azure_docintel_key else "none"

    @property
    def resolved_ocr_price_per_page(self) -> float:
        return self.ocr_price_per_page if self.resolved_ocr_provider == "azure" else 0.0


class ConfigError(RuntimeError):
    """Invalid or inconsistent configuration."""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
