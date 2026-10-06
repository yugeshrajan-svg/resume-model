"""AzureOpenAIProvider exercised offline through a mocked HTTP transport (no network)."""

from __future__ import annotations

import json

import httpx
import pytest
from openai import AzureOpenAI

from engine.config import Settings
from engine.llm import AzureOpenAIProvider, LLMError
from engine.schemas import CandidateExtraction

EMPTY_EXTRACTION = {
    "skills": [{"name": "Python", "evidence": "Python"}],
    "jobs": [],
    "projects": [],
    "education": [],
    "certifications": [],
    "location": None,
    "notice_period_days": None,
    "summary": None,
}


def _provider(message: dict, usage: dict | None = None) -> tuple[AzureOpenAIProvider, list[dict]]:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append({"url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-4o-2024-08-06",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", **message}}],
                "usage": usage or {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
            },
        )

    settings = Settings(
        AZURE_OPENAI_ENDPOINT="https://unit-test.openai.azure.com",
        AZURE_OPENAI_API_KEY="test-key",
        AZURE_OPENAI_CHAT_DEPLOYMENT="extract-dep",
        LLM_PRICE_INPUT_PER_1K=0.0025,
        LLM_PRICE_OUTPUT_PER_1K=0.01,
    )
    provider = AzureOpenAIProvider(settings)
    provider._client = AzureOpenAI(  # swap in a client whose transport never leaves the process
        azure_endpoint=settings.azure_openai_endpoint,
        api_key="test-key",
        api_version=settings.azure_openai_api_version,
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return provider, requests


def test_structured_output_request_and_usage_accounting() -> None:
    provider, requests = _provider({"content": json.dumps(EMPTY_EXTRACTION)})
    result = provider.parse(system="sys", user="usr", schema=CandidateExtraction)

    assert isinstance(result.parsed, CandidateExtraction)
    assert result.parsed.skills[0].name == "Python"
    body = requests[0]["body"]
    assert "/deployments/extract-dep/chat/completions" in requests[0]["url"]
    assert body["temperature"] == 0.0
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert result.usage.prompt_tokens == 1000 and result.usage.completion_tokens == 200
    assert result.usage.cost_usd == pytest.approx(1000 / 1000 * 0.0025 + 200 / 1000 * 0.01)
    assert result.usage.model == "gpt-4o-2024-08-06"


def test_refusal_raises_llm_error() -> None:
    provider, _ = _provider({"content": None, "refusal": "I can't help with that."})
    with pytest.raises(LLMError, match="refused"):
        provider.parse(system="sys", user="usr", schema=CandidateExtraction)


def test_schema_violation_raises_llm_error() -> None:
    provider, _ = _provider({"content": json.dumps({"skills": "not-a-list"})})
    with pytest.raises(LLMError):
        provider.parse(system="sys", user="usr", schema=CandidateExtraction)
