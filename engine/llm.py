"""AI gateway: a provider-agnostic interface for structured LLM calls.

Pipeline code depends only on ``LLMProvider``; the Azure OpenAI implementation
is one swappable provider. ``get_llm()`` returns ``None`` in offline mode.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

from engine.config import Settings, get_settings
from engine.logging_setup import log_event
from engine.schemas import LLMUsage

logger = logging.getLogger("engine.llm")

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Raised when the provider fails, refuses, or returns unparseable output."""


class LLMRetriesExhausted(LLMError):
    def __init__(self, attempts: int, last_error: LLMError) -> None:
        super().__init__(f"LLM failed after {attempts} attempts: {last_error}")
        self.attempts = attempts
        self.last_error = last_error


@dataclass
class LLMResult(Generic[T]):
    parsed: T
    usage: LLMUsage
    attempts: int = 1


class LLMProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model(self) -> str: ...

    def parse(self, *, system: str, user: str, schema: type[T], temperature: float = 0.0) -> LLMResult[T]:
        """Return the model output validated against ``schema``."""
        ...


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    usage: LLMUsage


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> EmbeddingResult:
        """Return one embedding vector per input text, in order."""
        ...


class AzureOpenAIProvider:
    """Azure OpenAI chat completions with structured outputs (strict JSON schema)."""

    def __init__(self, settings: Settings) -> None:
        if not (settings.azure_openai_endpoint and settings.azure_openai_api_key):
            raise ValueError("AzureOpenAIProvider requires AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY")
        from openai import AzureOpenAI  # imported lazily so offline mode never needs credentials

        self._settings = settings
        self._client = AzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            api_version=settings.azure_openai_api_version,
            timeout=settings.llm_timeout_s,
            max_retries=0,  # retries are handled (and logged) by parse_with_retry
        )

    @property
    def provider_name(self) -> str:
        return "azure_openai"

    @property
    def model(self) -> str:
        return self._settings.azure_openai_chat_deployment

    def parse(self, *, system: str, user: str, schema: type[T], temperature: float = 0.0) -> LLMResult[T]:
        start = time.perf_counter()
        try:
            completion = self._client.beta.chat.completions.parse(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format=schema,
                temperature=temperature,
            )
        except Exception as exc:  # network, auth, content filter, schema validation
            raise LLMError(f"Azure OpenAI call failed: {type(exc).__name__}: {exc}") from exc
        latency_ms = (time.perf_counter() - start) * 1000

        message = completion.choices[0].message
        if message.refusal:
            raise LLMError(f"Model refused: {message.refusal}")
        if message.parsed is None:
            raise LLMError("Model returned no parseable output")

        prompt_tokens = completion.usage.prompt_tokens if completion.usage else 0
        completion_tokens = completion.usage.completion_tokens if completion.usage else 0
        cost = (
            prompt_tokens / 1000 * self._settings.llm_price_input_per_1k
            + completion_tokens / 1000 * self._settings.llm_price_output_per_1k
        )
        usage = LLMUsage(
            provider=self.provider_name,
            model=completion.model or self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=round(latency_ms, 1),
            cost_usd=round(cost, 6),
        )
        log_event(logger, "llm_call", schema=schema.__name__, **usage.model_dump())
        return LLMResult(parsed=message.parsed, usage=usage)

    def embed(self, texts: list[str]) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=[], usage=LLMUsage(provider=self.provider_name, model=self._embed_model))
        start = time.perf_counter()
        try:
            response = self._client.embeddings.create(model=self._embed_model, input=texts)
        except Exception as exc:
            raise LLMError(f"Azure OpenAI embeddings failed: {type(exc).__name__}: {exc}") from exc
        tokens = response.usage.prompt_tokens if response.usage else 0
        usage = LLMUsage(
            provider=self.provider_name,
            model=response.model or self._embed_model,
            prompt_tokens=tokens,
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
            cost_usd=round(tokens / 1000 * self._settings.llm_price_embedding_per_1k, 6),
        )
        log_event(logger, "embedding_call", texts=len(texts), **usage.model_dump())
        vectors = [d.embedding for d in sorted(response.data, key=lambda d: d.index)]
        return EmbeddingResult(vectors=vectors, usage=usage)

    @property
    def _embed_model(self) -> str:
        return self._settings.azure_openai_embedding_deployment


def parse_with_retry(
    llm: LLMProvider,
    *,
    system: str,
    user: str,
    schema: type[T],
    settings: Settings | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> LLMResult[T]:
    """Call ``llm.parse`` with up to ``llm_max_retries`` retries and exponential backoff (temperature 0)."""
    settings = settings or get_settings()
    attempts = settings.llm_max_retries + 1
    last: LLMError | None = None
    for attempt in range(attempts):
        try:
            result = llm.parse(system=system, user=user, schema=schema, temperature=0.0)
            result.attempts = attempt + 1
            return result
        except LLMError as exc:
            last = exc
            if attempt + 1 < attempts:
                delay = settings.llm_backoff_base_s * (2**attempt) * (1 + random.random() * 0.25)
                log_event(
                    logger,
                    "llm_retry",
                    level=logging.WARNING,
                    attempt=attempt + 1,
                    delay_s=round(delay, 2),
                    error=str(exc)[:300],
                )
                sleep(delay)
    assert last is not None
    raise LLMRetriesExhausted(attempts, last)


def get_llm(settings: Settings | None = None) -> LLMProvider | None:
    """Return the provider for this run, or ``None`` when EXTRACTION_MODE resolves to "rules"."""
    settings = settings or get_settings()
    if settings.resolved_extraction_mode != "llm":
        return None
    if settings.llm_provider == "ollama":
        from engine.local_ai import OllamaProvider

        return OllamaProvider(settings)
    return AzureOpenAIProvider(settings)


def get_embedder(settings: Settings) -> EmbeddingProvider:
    if settings.llm_provider == "ollama":
        from engine.local_ai import OllamaProvider

        return OllamaProvider(settings)
    return AzureOpenAIProvider(settings)
