"""Local provider contracts and isolation, exercised offline through an HTTP mock transport."""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import httpx
import pymupdf
import pytest
from pydantic import BaseModel, ValidationError

from backend.snapshots import freeze_settings, restore_settings
from engine.cache import extraction_cache_key
from engine.config import Settings
from engine.extraction import extract_candidate
from engine.llm import LLMError, get_embedder, get_llm
from engine.local_ai import OllamaProvider, _chunks, pin_local_models
from engine.local_ocr import _read_pdf, tesseract_ocr
from engine.normalize import load_ontology
from engine.parsing import parse_bytes
from engine.semantic import EmbeddingScorer, build_semantic_scorer
from tests.conftest import ROOT


def local(**updates) -> Settings:
    return Settings(
        _env_file=None,
        **{
            "LLM_PROVIDER": "ollama",
            "EXTRACTION_MODE": "llm",
            "SEMANTIC_BACKEND": "embeddings",
            "OCR_PROVIDER": "tesseract",
            **updates,
        },
    )


class Fact(BaseModel):
    skill: str


def chat(content: dict | str, **updates) -> dict:
    return {
        "message": {"content": json.dumps(content) if isinstance(content, dict) else content},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 42,
        "eval_count": 7,
        **updates,
    }


def provider(response: dict, settings: Settings | None = None, status: int = 200):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json=response)

    return OllamaProvider(settings or local(), transport=httpx.MockTransport(handler)), requests


def test_local_factory_ignores_cloud_credentials() -> None:
    settings = local(AZURE_OPENAI_ENDPOINT="https://unused.openai.azure.com", AZURE_OPENAI_API_KEY="unused")
    assert isinstance(get_llm(settings), OllamaProvider)
    assert isinstance(get_embedder(settings), OllamaProvider)
    assert settings.resolved_ocr_provider == "tesseract"
    assert settings.resolved_ocr_price_per_page == 0
    assert get_llm(local(EXTRACTION_MODE="rules")) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "http://127.0.0.1.example.com",
        "https://ollama.com",
        "http://user:secret@localhost:11434",
        "http://localhost:11434/path",
        "http://localhost:11434/?token=secret",
        "file:///tmp/model",
    ],
)
def test_local_endpoint_rejects_external_destinations(url) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        local(OLLAMA_BASE_URL=url)


@pytest.mark.parametrize("name", ["qwen3:cloud", "gpt-oss:120b-cloud", "https://ollama.com/model", "   "])
def test_cloud_models_are_rejected(name) -> None:
    with pytest.raises(ValidationError):
        local(OLLAMA_CHAT_MODEL=name)


def test_paid_ocr_and_unsafe_language_path_are_rejected() -> None:
    with pytest.raises(ValidationError, match="OCR_PROVIDER"):
        local(OCR_PROVIDER="azure")
    with pytest.raises(ValidationError, match="language codes"):
        local(TESSERACT_LANGUAGE="../private")


def test_structured_output_uses_schema_no_thinking_seed_and_zero_cost() -> None:
    llm, requests = provider(chat({"skill": "Python"}))
    result = llm.parse(system="Extract facts", user="Python", schema=Fact)
    body = json.loads(requests[0].content)
    assert requests[0].url.host == "127.0.0.1" and requests[0].url.path == "/api/chat"
    assert "authorization" not in requests[0].headers
    assert body["format"] == Fact.model_json_schema()
    assert body["options"]["temperature"] == 0 and body["options"]["seed"] == 42
    assert body["think"] is False and body["stream"] is False
    assert result.parsed.skill == "Python"
    assert result.usage.provider == "ollama" and result.usage.cost_usd == 0
    assert result.usage.prompt_tokens == 42 and result.usage.completion_tokens == 7


@pytest.mark.parametrize(
    "response",
    [
        chat("private resume: not JSON"),
        chat({"unrelated": "private resume"}),
        chat({"skill": "Python"}, done=False),
        chat({"skill": "Python"}, done_reason="length"),
        chat({"skill": "Python"}, prompt_eval_count=-1),
        {"message": [], "done": True},
    ],
)
def test_invalid_or_incomplete_response_is_reprocessable_without_document_leaks(response) -> None:
    llm, _ = provider(response)
    with pytest.raises(LLMError) as error:
        llm.parse(system="", user="private resume", schema=Fact)
    assert "private resume" not in str(error.value)


@pytest.mark.parametrize("status", [302, 401, 404, 500])
def test_http_errors_do_not_redirect_or_leak_resume_text(status) -> None:
    llm, requests = provider({"error": "private resume"}, status=status)
    with pytest.raises(LLMError, match=f"HTTP {status}"):
        llm.parse(system="", user="private resume", schema=Fact)
    assert len(requests) == 1


def test_context_limit_fails_before_request_instead_of_truncating() -> None:
    llm, requests = provider(chat({"skill": "Python"}), local(OLLAMA_NUM_CTX=4096))
    with pytest.raises(LLMError, match="context budget"):
        llm.parse(system="", user="long document" * 1000, schema=Fact)
    assert not requests


def test_hallucinated_skill_is_removed_by_existing_evidence_verifier() -> None:
    data = {
        "skills": [{"name": "Python", "evidence": "Python"}, {"name": "Rust", "evidence": "Rust"}],
        "jobs": [],
        "projects": [],
        "education": [],
        "certifications": [],
        "location": None,
        "notice_period_days": None,
        "summary": None,
    }
    llm, _ = provider(chat(data))
    result = extract_candidate("Skills: Python", llm, settings=local())
    assert result.status == "ok" and [item.name for item in result.extraction.skills] == ["Python"]
    assert result.dropped_items[0].reason == "evidence_not_found"


@pytest.mark.parametrize("quote,expected", [("Skills: SQL", ["Python"]), ("Skills: Python, SQL", ["Python", "SQL"])])
def test_comma_list_evidence_requires_exact_quote_and_local_prompt_explains_it(quote, expected) -> None:
    data = {
        "skills": [{"name": "Python", "evidence": "Skills: Python, SQL"}, {"name": "SQL", "evidence": quote}],
        "jobs": [],
        "projects": [],
        "education": [],
        "certifications": [],
        "location": None,
        "notice_period_days": None,
        "summary": None,
    }
    llm, requests = provider(chat(data))
    result = extract_candidate("Skills: Python, SQL", llm, settings=local())
    assert [item.name for item in result.extraction.skills] == expected
    system = json.loads(requests[0].content)["messages"][0]["content"]
    assert 'Do NOT write "Skills: SQL"' in system and llm.prompt_version == "1.1.0"
    assert json.loads(requests[0].content)["format"]["$defs"]["SkillItem"]["properties"]["evidence"]["enum"] == [
        "Skills: Python, SQL"
    ]


def test_document_quotes_are_decoder_data_and_never_interpolated_into_system_prompt() -> None:
    data = {
        "skills": [],
        "jobs": [],
        "projects": [],
        "education": [],
        "certifications": [],
        "location": None,
        "notice_period_days": None,
        "summary": None,
    }
    llm, requests = provider(chat(data))
    extract_candidate("private unique document line", llm, settings=local())
    body = json.loads(requests[0].content)
    assert "private unique document line" not in body["messages"][0]["content"]
    assert body["format"]["$defs"]["SkillItem"]["properties"]["evidence"]["enum"] == ["private unique document line"]


def test_embeddings_preserve_unicode_chunks_pool_vectors_in_order_and_cost_zero() -> None:
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "embeddings": [[1, 0] if i == 0 else [0, 1] for i in range(len(body["input"]))],
                "prompt_eval_count": 12,
            },
        )

    text = "abc" * 42 + "\u0915\U0001f600" * 70
    llm = OllamaProvider(local(OLLAMA_EMBED_CHUNK_BYTES=128), transport=httpx.MockTransport(handler))
    result = llm.embed([text, "second"])
    chunks = [value.removeprefix("search_document: ") for value in requests[0]["input"]]
    assert "".join(chunks) == text and all(len(c.encode()) <= 128 for c in chunks)
    assert requests[0]["truncate"] is False
    assert len(result.vectors) == 2 and all(math.hypot(*v) == pytest.approx(1) for v in result.vectors)
    assert result.vectors[1] == [1, 0] and result.usage.prompt_tokens == 24 and result.usage.cost_usd == 0


@pytest.mark.parametrize("vectors", [[], [[0, 0]], [[True, 0]], [[float("nan"), 0]], [[1, "invalid"]], "invalid"])
def test_invalid_embedding_vectors_are_rejected(vectors) -> None:
    # json.dumps rejects NaN over strict HTTP, so MockTransport returns a permissive JSON byte body for that case.
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=json.dumps({"embeddings": vectors})))
    llm = OllamaProvider(local(), transport=transport)
    with pytest.raises(LLMError, match="invalid vectors"):
        llm.embed(["text"])


def test_dimensions_must_match_across_documents() -> None:
    dimensions = iter([2, 3])
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"embeddings": [[1] * next(dimensions)]}))
    with pytest.raises(LLMError, match="invalid vectors"):
        OllamaProvider(local(), transport=transport).embed(["first", "second"])


def test_empty_embeddings_do_not_contact_runtime() -> None:
    llm, requests = provider({})
    assert llm.embed([]).vectors == [] and not requests
    assert _chunks("", 128) == [""]


def test_local_scorer_keeps_long_document_and_avoids_azure_calibration() -> None:
    class Embedder:
        def embed(self, texts):
            assert len(texts[0]) == 30_000
            return OllamaProvider(
                local(),
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, json={"embeddings": [[1, 0]] * len(json.loads(request.content)["input"])}
                    )
                ),
            ).embed(texts)

    scorer = build_semantic_scorer("job", local(), Embedder())
    assert isinstance(scorer, EmbeddingScorer) and scorer.name == "ollama-embeddings"
    assert scorer.similarities("x" * 30_000, ["doc"]) == [1]
    assert scorer.usage_cost_usd == 0


def test_model_digest_and_configuration_survive_restart_and_invalidate_cache(monkeypatch) -> None:
    monkeypatch.setattr(
        OllamaProvider, "model_digest", lambda self, model, expected=None: "sha-chat" if model == CHAT else "sha-embed"
    )
    settings = pin_local_models(local())
    saved = freeze_settings(settings)
    restored = restore_settings(saved, local(OLLAMA_CHAT_MODEL="other:v2"))
    assert restored.chat_model == CHAT and restored.ollama_chat_digest == "sha-chat"
    assert restored.ollama_embedding_digest == "sha-embed"
    original = extraction_cache_key("Python", get_llm(restored), restored, load_ontology())
    changed = restored.model_copy(update={"ollama_chat_digest": "changed"})
    assert original != extraction_cache_key("Python", get_llm(changed), changed, load_ontology())


CHAT = "shortlist-local:v1"


def test_changed_model_during_inference_rejects_result() -> None:
    digests = iter(["original", "changed"])

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": CHAT, "digest": next(digests)}]})
        return httpx.Response(200, json=chat({"skill": "Python"}))

    llm = OllamaProvider(local(OLLAMA_CHAT_DIGEST="original"), transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError, match="changed since this run"):
        llm.parse(system="", user="Python", schema=Fact)


@pytest.mark.parametrize(
    "models", [None, [], [{"name": CHAT}], [{"name": CHAT, "digest": "sha", "remote_host": "ollama.com"}]]
)
def test_missing_corrupt_or_cloud_metadata_is_rejected(models) -> None:
    llm, _ = provider({"models": models})
    with pytest.raises(LLMError):
        llm.model_digest(CHAT)


def pdf_bytes(*, width=595, height=842, pages=1) -> bytes:
    with pymupdf.open() as document:
        for _ in range(pages):
            document.new_page(width=width, height=height)
        return document.tobytes()


def test_ocr_page_and_pixel_limits_are_checked_before_ocr() -> None:
    options = {"tessdata": "unused", "language": "eng", "dpi": 200, "maximum_pages": 1, "maximum_pixels": 20_000_000}
    with pytest.raises(ValueError, match="page limit"):
        _read_pdf(pdf_bytes(pages=2), **options)
    with pytest.raises(ValueError, match="pixel limit"):
        _read_pdf(pdf_bytes(width=10000), **options)


def test_ocr_missing_weights_and_timeout_route_to_reprocessing(tmp_path: Path, monkeypatch, caplog) -> None:
    settings = local(TESSERACT_DATA_DIR=tmp_path)
    result = parse_bytes(pdf_bytes(), "scan.pdf", settings=settings)
    assert result.needs_ocr and not result.ocr_applied
    (tmp_path / "eng.traineddata").write_bytes(b"placeholder")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("private resume", 1, stderr=b"private resume")

    monkeypatch.setattr("engine.local_ocr.subprocess.run", timeout)
    assert tesseract_ocr(pdf_bytes(), "safe-doc-id", settings) is None
    assert "private resume" not in caplog.text


def test_ocr_subprocess_receives_bytes_and_returns_page_metering(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "eng.traineddata").write_bytes(b"placeholder")
    data = pdf_bytes()

    def run(command, **kwargs):
        assert kwargs["input"] == data and kwargs["timeout"] == 7
        assert "engine.local_ocr" in command
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps({"text": "Python developer", "pages": 1}).encode()
        )

    monkeypatch.setattr("engine.local_ocr.subprocess.run", run)
    result = tesseract_ocr(data, "safe-doc-id", local(TESSERACT_DATA_DIR=tmp_path, OCR_TIMEOUT_S=7))
    assert result.text == "Python developer" and result.pages == 1


def _launcher():
    import importlib.util

    spec = importlib.util.spec_from_file_location("local_ai_launcher", ROOT / "scripts" / "local_ai.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("sizes", "expected"),
    [
        ({"size": 100, "size_vram": 100}, "GPU"),
        ({"size": 100, "size_vram": 0}, "CPU"),
        ({"size": 100}, "CPU"),
        ({"size": 100, "size_vram": 25}, "25% GPU / 75% CPU"),
    ],
)
def test_status_reports_where_the_model_runs(sizes, expected) -> None:
    assert _launcher().processor(sizes) == expected


def test_server_capacity_comes_from_settings(monkeypatch) -> None:
    launcher = _launcher()
    assert launcher.runtime_environment()["OLLAMA_NUM_PARALLEL"] == "1"  # 8 GB laptop default
    monkeypatch.setenv("OLLAMA_NUM_PARALLEL", "4")
    monkeypatch.setenv("OLLAMA_MAX_LOADED_MODELS", "2")
    env = launcher.runtime_environment()
    assert (env["OLLAMA_NUM_PARALLEL"], env["OLLAMA_MAX_LOADED_MODELS"]) == ("4", "2")
    monkeypatch.setenv("OLLAMA_NUM_PARALLEL", "64")
    with pytest.raises(ValidationError):
        launcher.runtime_environment()
