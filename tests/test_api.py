from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from engine.cache import FileExtractionCache
from engine.config import get_settings
from engine.extraction import extract_job_requirements
from tests.conftest import SAMPLE_DIR

JD = (SAMPLE_DIR / "jd.txt").read_text(encoding="utf-8")


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(get_settings(), cache=FileExtractionCache(tmp_path)))


def _confirmed_requirements() -> str:
    return extract_job_requirements(JD, None).requirements.confirm(by="API Tester").model_dump_json()


def _files(*names: str) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("files", (n, (SAMPLE_DIR / "resumes" / n).read_bytes(), "application/octet-stream")) for n in names]


def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["extraction_mode"] == "rules" and body["semantic_backend"] == "tfidf"


def test_analyze_returns_unconfirmed_requirements(client: TestClient) -> None:
    r = client.post("/jobs/analyze", json={"jd_text": JD})
    assert r.status_code == 200
    req = r.json()["requirements"]
    assert req["role"] == "Senior Machine Learning Engineer"
    assert req["requirements_confirmed"] is None


def test_analyze_validates_input(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.post("/jobs/analyze", json={"jd_text": "too short"}).status_code == 422
    monkeypatch.setenv("MAX_JD_CHARS", "100")
    get_settings.cache_clear()
    small = TestClient(create_app(get_settings()))
    assert small.post("/jobs/analyze", json={"jd_text": "x" * 101}).status_code == 413


def test_screen_runs_as_a_background_job(client: TestClient) -> None:
    files = _files("01_strong_match.pdf", "02_overlapping_jobs.docx", "05_intern.txt", "08_scanned_needs_ocr.pdf")
    r = client.post("/screen", data={"requirements": _confirmed_requirements()}, files=files)
    assert r.status_code == 202
    accepted = r.json()
    assert accepted["status"] == "queued" and accepted["documents"] == 4

    job = client.get(accepted["status_url"]).json()  # TestClient runs background tasks before returning
    assert job["status"] == "completed"
    assert job["progress"] == {"done": 4, "total": 4}
    result = job["result"]
    assert [c["source_name"] for c in result["ranked"]][:1] == ["01_strong_match.pdf"]
    assert [c["source_name"] for c in result["needs_reprocess"]] == ["08_scanned_needs_ocr.pdf"]
    assert result["run"]["requirements_confirmed"]["by"] == "API Tester"
    assert all(c["explanation_source"] == "template" for c in result["ranked"])


def test_screen_uses_the_extraction_cache_across_jobs(client: TestClient) -> None:
    files = _files("01_strong_match.pdf", "05_intern.txt")
    req = _confirmed_requirements()
    first = client.post("/screen", data={"requirements": req}, files=files).json()
    second = client.post("/screen", data={"requirements": req}, files=files).json()
    run1 = client.get(first["status_url"]).json()["result"]["run"]
    run2 = client.get(second["status_url"]).json()["result"]["run"]
    assert (run1["extraction_cache_hits"], run1["extraction_cache_misses"]) == (0, 2)
    assert (run2["extraction_cache_hits"], run2["extraction_cache_misses"]) == (2, 0)


def test_screen_rejects_unconfirmed_requirements(client: TestClient) -> None:
    unconfirmed = extract_job_requirements(JD, None).requirements.model_dump_json()
    r = client.post("/screen", data={"requirements": unconfirmed}, files=_files("05_intern.txt"))
    assert r.status_code == 422 and "requirements_confirmed" in r.text


def test_screen_rejects_invalid_requirements_json(client: TestClient) -> None:
    r = client.post("/screen", data={"requirements": json.dumps({"role": 1})}, files=_files("05_intern.txt"))
    assert r.status_code == 422


@pytest.mark.parametrize(
    ("name", "content", "code"),
    [
        ("cv.exe", b"MZ binary", 415),
        ("cv.pdf", b"not really a pdf", 415),
        ("cv.txt", b"", 400),
    ],
)
def test_screen_rejects_bad_files(client: TestClient, name: str, content: bytes, code: int) -> None:
    files = [("files", (name, content, "application/octet-stream"))]
    r = client.post("/screen", data={"requirements": _confirmed_requirements()}, files=files)
    assert r.status_code == code


def test_screen_enforces_size_and_count_limits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MAX_FILE_BYTES", "1000")
    monkeypatch.setenv("MAX_FILES_PER_REQUEST", "2")
    get_settings.cache_clear()
    client = TestClient(create_app(get_settings(), cache=FileExtractionCache(tmp_path)))
    req = _confirmed_requirements()
    big = [("files", ("big.txt", b"x" * 1001, "text/plain"))]
    assert client.post("/screen", data={"requirements": req}, files=big).status_code == 413
    many = [("files", (f"{i}.txt", b"Skills\nPython\n", "text/plain")) for i in range(3)]
    assert client.post("/screen", data={"requirements": req}, files=many).status_code == 413


def test_unknown_job_is_404(client: TestClient) -> None:
    assert client.get("/screen/does-not-exist").status_code == 404


def test_failed_background_job_is_reported(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import api.main

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(api.main, "screen", boom)
    r = client.post("/screen", data={"requirements": _confirmed_requirements()}, files=_files("05_intern.txt"))
    job = client.get(r.json()["status_url"]).json()
    assert job["status"] == "failed" and "disk full" in job["error"] and job["result"] is None


def test_api_key_is_enforced_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", "s3cret")
    get_settings.cache_clear()
    client = TestClient(create_app(get_settings()))
    assert client.get("/health").status_code == 200  # health stays open for probes
    assert client.post("/jobs/analyze", json={"jd_text": JD}).status_code == 401
    assert client.post("/jobs/analyze", json={"jd_text": JD}, headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.post("/jobs/analyze", json={"jd_text": JD}, headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.get("/screen/x", headers={"X-API-Key": "s3cret"}).status_code == 404


def test_job_store_evicts_oldest_finished_jobs() -> None:
    from api.main import JobStore

    store = JobStore(capacity=2)
    a = store.create(1)
    store.update(a.job_id, status="completed")
    b = store.create(1)
    c = store.create(1)  # over capacity: the finished job "a" is evicted, running ones are kept
    assert store.get(a.job_id) is None
    assert store.get(b.job_id) is not None and store.get(c.job_id) is not None
