from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from engine.config import Settings, get_settings
from engine.llm import LLMError, LLMResult
from engine.schemas import LLMUsage

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = ROOT / "data" / "sample"


@pytest.fixture(scope="session", autouse=True)
def _offline_defaults(extraction_cache_dir: Path):
    """Offline defaults also apply before module-scoped fixtures; live tests explicitly select their AI mode."""
    patch = pytest.MonkeyPatch()
    patch.setenv("EXTRACTION_CACHE_DIR", str(extraction_cache_dir))
    for key, value in {
        "LLM_PROVIDER": "azure_openai",
        "EXTRACTION_MODE": "rules",
        "SEMANTIC_BACKEND": "tfidf",
        "SEMANTIC_RERANKER": "none",
        "OCR_PROVIDER": "none",
    }.items():
        patch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    patch.undo()


@pytest.fixture(scope="session")
def extraction_cache_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One extraction cache per test session (never the developer's real cache)."""
    return tmp_path_factory.mktemp("extraction-cache")


@pytest.fixture(autouse=True)
def _offline(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, extraction_cache_dir: Path) -> None:
    """Tests run offline (rules mode, no credentials) unless marked ``live``.

    Env vars override any .env file, so blanking them guarantees no network access.
    """
    monkeypatch.setenv("EXTRACTION_CACHE_DIR", str(extraction_cache_dir))
    if request.node.get_closest_marker("live") is None:
        # A developer's selected profile must not affect tests that deliberately unset a mode to test auto-selection.
        monkeypatch.setitem(Settings.model_config, "env_file", None)
        monkeypatch.setenv("LLM_PROVIDER", "azure_openai")
        monkeypatch.setenv("OCR_PROVIDER", "none")
        monkeypatch.setenv("EXTRACTION_MODE", "rules")
        monkeypatch.setenv("SEMANTIC_BACKEND", "tfidf")
        monkeypatch.setenv("SEMANTIC_RERANKER", "none")
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "")
        monkeypatch.setenv("AZURE_OPENAI_KEY", "")
        monkeypatch.setenv("AZURE_DOCINTEL_ENDPOINT", "")
        monkeypatch.setenv("AZURE_DOCINTEL_KEY", "")
        monkeypatch.setenv("LLM_BACKOFF_BASE_S", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeLLM:
    """In-memory LLMProvider: returns a canned response and records the prompts it saw.

    ``errors`` are raised (in order) on the first calls, simulating transient failures.
    """

    def __init__(self, response: BaseModel | None = None, errors: list[Exception] | None = None) -> None:
        self.response = response
        self.errors = list(errors or [])
        self.calls: list[dict[str, Any]] = []

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model-1"

    def parse(self, *, system: str, user: str, schema: type[BaseModel], temperature: float = 0.0) -> LLMResult[Any]:
        self.calls.append({"system": system, "user": user, "schema": schema, "temperature": temperature})
        if self.errors:
            raise self.errors.pop(0)
        assert isinstance(self.response, schema)
        usage = LLMUsage(provider="fake", model=self.model, prompt_tokens=100, completion_tokens=50, cost_usd=0.001)
        return LLMResult(parsed=self.response, usage=usage)


def outage(n: int) -> list[Exception]:
    return [LLMError(f"simulated outage {i + 1}") for i in range(n)]
