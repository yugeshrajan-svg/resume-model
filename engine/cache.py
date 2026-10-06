"""Extraction cache.

Candidate extraction depends only on the resume text and how it was extracted — not on the job — so
results are cached by (content hash, extraction mode, prompt version, model, ontology version,
max_resume_chars). Screening several jobs, or re-running evaluation, never re-extracts a resume.

``ExtractionCache`` is the interface; ``FileExtractionCache`` stores one JSON file per key and can be
swapped for a database-backed implementation (e.g. Postgres) without touching callers.
Failed extractions are never cached, so they are retried next time.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from engine.config import Settings, get_settings
from engine.extraction import VERIFIER_VERSION, extract_candidate
from engine.llm import LLMProvider
from engine.logging_setup import log_event
from engine.normalize import SkillOntology, load_ontology
from engine.prompts import get_prompt
from engine.schemas import ExtractionResult

logger = logging.getLogger("engine.cache")


class ExtractionCache(Protocol):
    def get(self, key: str) -> ExtractionResult | None: ...

    def put(self, key: str, result: ExtractionResult) -> None: ...


class NullExtractionCache:
    """Caching disabled."""

    def get(self, key: str) -> ExtractionResult | None:
        return None

    def put(self, key: str, result: ExtractionResult) -> None:
        return None


class FileExtractionCache:
    """One JSON file per key under ``directory`` (sharded by key prefix). Writes are atomic."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _path(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"

    def get(self, key: str) -> ExtractionResult | None:
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            return ExtractionResult.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError) as exc:  # corrupt or schema-changed entry: treat as a miss
            log_event(logger, "extraction_cache_entry_invalid", level=logging.WARNING, key=key, error=str(exc)[:200])
            return None

    def put(self, key: str, result: ExtractionResult) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(result.model_dump_json())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def get_extraction_cache(settings: Settings | None = None) -> ExtractionCache:
    settings = settings or get_settings()
    if not settings.extraction_cache_enabled:
        return NullExtractionCache()
    return FileExtractionCache(settings.extraction_cache_dir)


@dataclass
class CacheStats:
    """Hit/miss counters for one run (thread-safe)."""

    hits: int = 0
    misses: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, hit: bool) -> None:
        with self._lock:
            if hit:
                self.hits += 1
            else:
                self.misses += 1

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total else 0.0


def extraction_cache_key(text: str, llm: LLMProvider | None, settings: Settings, ontology: SkillOntology) -> str:
    prompt = get_prompt("resume_extraction")
    parts = {
        "content": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "mode": "llm" if llm is not None else "rules",
        "prompt_version": prompt.version if llm is not None else None,
        "model": llm.model if llm is not None else None,
        "provider": llm.provider_name if llm is not None else None,
        "provider_prompt_version": getattr(llm, "prompt_version", None),
        "local_model_digest": settings.ollama_chat_digest
        if llm is not None and settings.llm_provider == "ollama"
        else None,
        "local_context": [settings.ollama_num_ctx, settings.ollama_num_predict]
        if llm is not None and settings.llm_provider == "ollama"
        else None,
        "ontology_version": ontology.version,
        "verifier_version": VERIFIER_VERSION,
        "max_resume_chars": settings.max_resume_chars,
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()


def cached_extract_candidate(
    text: str,
    llm: LLMProvider | None,
    cache: ExtractionCache,
    stats: CacheStats | None = None,
    settings: Settings | None = None,
    ontology: SkillOntology | None = None,
) -> ExtractionResult:
    """``extract_candidate`` with a cache in front. Cache hits are flagged ``from_cache`` and cost nothing."""
    settings = settings or get_settings()
    ontology = ontology or load_ontology()
    key = extraction_cache_key(text, llm, settings, ontology)
    cached = cache.get(key)
    if stats is not None:
        stats.record(cached is not None)
    if cached is not None:
        log_event(logger, "extraction_cache_hit", level=logging.DEBUG, key=key[:12])
        return cached.model_copy(update={"from_cache": True})
    result = extract_candidate(text, llm, settings=settings, ontology=ontology)
    if result.status == "ok":
        cache.put(key, result)
    return result
