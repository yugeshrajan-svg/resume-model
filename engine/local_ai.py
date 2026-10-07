"""Loopback-only Ollama gateway. No API key, cloud fallback, or paid usage accounting."""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any

import httpx
from pydantic import ValidationError

from engine.config import Settings
from engine.llm import EmbeddingResult, LLMError, LLMResult, T
from engine.logging_setup import log_event
from engine.prompts import DOC_CLOSE, DOC_OPEN
from engine.schemas import LLMUsage

logger = logging.getLogger("engine.local_ai")
LOCAL_PROMPT_VERSION = "1.1.0"
_EXACT_QUOTE_HINT = """
EVIDENCE COPYING EXAMPLE:
If the document line is "Skills: Python, SQL", emit separate Python and SQL skills.
For BOTH items, evidence may be "Skills: Python, SQL", copied exactly from that line.
Do NOT write "Skills: SQL": that text does not occur in the document.
Do not reconstruct, shorten by removing middle words, or repeat a heading before each skill.
An evidence quote must be one exact contiguous substring, including any comma-separated list.
Before returning JSON, check that every evidence string actually occurs in the document.
"""


def structured_system_prompt(system: str, schema: type[T]) -> str:
    """Use identical static extraction instructions for inference and fine-tuning."""
    if schema.__name__ in {"CandidateExtraction", "ExtractedJobRequirements"}:
        system += _EXACT_QUOTE_HINT
    return system + "\nReturn only JSON matching this schema:\n" + json.dumps(schema.model_json_schema())


class OllamaProvider:
    provider_name = "ollama"
    prompt_version = LOCAL_PROMPT_VERSION

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport

    @property
    def model(self) -> str:
        return self._settings.ollama_chat_model

    def _request(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        # Ignore proxy environment variables; credentials and resume text must remain on loopback.
        try:
            with httpx.Client(
                base_url=self._settings.ollama_base_url,
                timeout=min(10, self._settings.llm_timeout_s) if body is None else self._settings.llm_timeout_s,
                trust_env=False,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = client.get(path) if body is None else client.post(path, json=body)
                if response.status_code != 200:
                    raise LLMError(
                        f"Local Ollama returned HTTP {response.status_code}; "
                        "start the server and install the configured model"
                    )
                result = response.json()
                if not isinstance(result, dict) or result.get("error"):
                    raise ValueError("invalid provider response")
                return result
        except (httpx.HTTPError, ValueError) as exc:
            # Provider error bodies/schema errors can contain the document. Log only their type.
            raise LLMError(
                f"Local Ollama request failed ({type(exc).__name__}); check the local server/model"
            ) from None

    def model_digest(self, model: str, expected: str | None = None) -> str:
        models = self._request("/api/tags").get("models", [])
        if not isinstance(models, list):
            raise LLMError("Local Ollama returned invalid model metadata")
        for item in models:
            if isinstance(item, dict) and (item.get("name") == model or item.get("model") == model):
                if item.get("remote_host") or item.get("remote_model"):
                    raise LLMError("Cloud-backed models are disabled for the local provider")
                digest = item.get("digest")
                if not isinstance(digest, str) or not digest:
                    break
                if expected is not None and expected != digest:
                    raise LLMError(
                        "Local model changed since this run was created; "
                        "restore its original weights or create a new run"
                    )
                return digest
        raise LLMError(f"Local model {model!r} is not installed; run python scripts/local_ai.py setup")

    def _verify(self, model: str, digest: str | None) -> None:
        if digest is not None:
            self.model_digest(model, digest)

    def parse(self, *, system: str, user: str, schema: type[T], temperature: float = 0.0) -> LLMResult[T]:
        self._verify(self.model, self._settings.ollama_chat_digest)
        start = time.perf_counter()
        shape = schema.model_json_schema()
        # Keep the static schema in the system prompt. Document-derived values stay in the decoder constraint,
        # rather than interpolating untrusted resume text into a higher-priority message.
        if schema.__name__ in {"CandidateExtraction", "ExtractedJobRequirements"}:
            definition = "SkillItem" if schema.__name__ == "CandidateExtraction" else "RequirementSkill"
            source = user
            if DOC_OPEN in user and DOC_CLOSE in user:
                source = user.split(DOC_OPEN, 1)[1].rsplit(DOC_CLOSE, 1)[0]
            quotes = list(dict.fromkeys(line.strip() for line in source.splitlines() if len(line.strip()) >= 3))
            if quotes:
                shape["$defs"][definition]["properties"]["evidence"]["enum"] = quotes
        body = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": structured_system_prompt(system, schema),
                },
                {"role": "user", "content": user},
            ],
            "format": shape,
            "stream": False,
            "think": False,
            "keep_alive": self._settings.ollama_keep_alive,
            "options": {
                "temperature": temperature,
                "seed": 42,
                "num_ctx": self._settings.ollama_num_ctx,
                "num_predict": self._settings.ollama_num_predict,
            },
        }
        # UTF-8 bytes upper-bound byte-tokenizer input tokens. Leave room for template and output.
        # This conservative guard rejects oversized documents instead of Ollama silently dropping text.
        prompt_bytes = sum(len(item["content"].encode("utf-8")) for item in body["messages"])
        if prompt_bytes + self._settings.ollama_num_predict + 256 > self._settings.ollama_num_ctx:
            raise LLMError("Input exceeds the safe local context budget; shorten it or increase OLLAMA_NUM_CTX")
        response = self._request("/api/chat", body)
        if response.get("done") is not True or response.get("done_reason") == "length":
            raise LLMError("Local model output was incomplete; increase OLLAMA_NUM_PREDICT or shorten the input")
        try:
            parsed = schema.model_validate_json(response["message"]["content"])
            usage = LLMUsage(
                provider=self.provider_name,
                model=self.model,
                prompt_tokens=_token_count(response.get("prompt_eval_count", 0)),
                completion_tokens=_token_count(response.get("eval_count", 0)),
                latency_ms=round((time.perf_counter() - start) * 1000, 1),
                cost_usd=0.0,
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            raise LLMError("Local model returned invalid structured data; document content omitted") from None
        self._verify(self.model, self._settings.ollama_chat_digest)
        log_event(logger, "llm_call", schema=schema.__name__, **usage.model_dump())
        return LLMResult(parsed=parsed, usage=usage)

    def embed(self, texts: list[str]) -> EmbeddingResult:
        return self._embed(texts, "search_document: ")

    def embed_query(self, texts: list[str]) -> EmbeddingResult:
        """Embed search queries; Nomic models expect a different task prefix for queries than for documents."""
        return self._embed(texts, "search_query: ")

    def _embed(self, texts: list[str], prefix: str) -> EmbeddingResult:
        model = self._settings.ollama_embedding_model
        if not texts:
            return EmbeddingResult([], LLMUsage(provider=self.provider_name, model=model))
        self._verify(model, self._settings.ollama_embedding_digest)
        start = time.perf_counter()
        vectors, tokens = [], 0
        dimension = None
        for text in texts:
            # Process all chunks; never silently truncate a long resume to the model's context window.
            chunks = _chunks(text, self._settings.ollama_embed_chunk_bytes)
            response = self._request(
                "/api/embed",
                {
                    "model": model,
                    "input": [prefix + chunk for chunk in chunks],
                    "truncate": False,
                    "keep_alive": self._settings.ollama_keep_alive,
                },
            )
            try:
                embeddings = response["embeddings"]
                if not isinstance(embeddings, list) or len(embeddings) != len(chunks):
                    raise ValueError("missing vectors")
                pooled = None
                for chunk, vector in zip(chunks, embeddings, strict=True):
                    if (
                        not isinstance(vector, list)
                        or not vector
                        or any(
                            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                            for v in vector
                        )
                    ):
                        raise ValueError("invalid vector")
                    dimension = dimension or len(vector)
                    if len(vector) != dimension or not any(vector):
                        raise ValueError("inconsistent vector")
                    weight = max(1, len(chunk))
                    if pooled is None:
                        pooled = [0.0] * dimension
                    for index, value in enumerate(vector):
                        pooled[index] += value * weight
                norm = math.hypot(*pooled)
                if not norm or not math.isfinite(norm):
                    raise ValueError("zero or overflowing vector")
                vectors.append([value / norm for value in pooled])
                tokens += _token_count(response.get("prompt_eval_count", 0))
            except (KeyError, TypeError, ValueError):
                raise LLMError("Local embedding model returned invalid vectors") from None
        self._verify(model, self._settings.ollama_embedding_digest)
        usage = LLMUsage(
            provider=self.provider_name,
            model=model,
            prompt_tokens=tokens,
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
            cost_usd=0.0,
        )
        log_event(logger, "embedding_call", texts=len(texts), **usage.model_dump())
        return EmbeddingResult(vectors, usage)


def _chunks(text: str, maximum_bytes: int) -> list[str]:
    if not text:
        return [""]
    chunks, current, length = [], [], 0
    for character in text:
        size = len(character.encode("utf-8"))
        if length + size > maximum_bytes:
            chunks.append("".join(current))
            current, length = [], 0
        current.append(character)
        length += size
    if current:
        chunks.append("".join(current))
    return chunks


def _token_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Invalid token count")
    return value


def pin_local_models(settings: Settings) -> Settings:
    """Freeze actual installed digests for durable runs and caches, rather than a mutable tag alone."""
    if settings.llm_provider != "ollama":
        return settings
    provider = OllamaProvider(settings)
    updates = {}
    if settings.resolved_extraction_mode == "llm":
        updates["ollama_chat_digest"] = provider.model_digest(settings.chat_model, settings.ollama_chat_digest)
    if settings.resolved_semantic_backend == "embeddings":
        updates["ollama_embedding_digest"] = provider.model_digest(
            settings.embedding_model, settings.ollama_embedding_digest
        )
    return settings.model_copy(update=updates)
