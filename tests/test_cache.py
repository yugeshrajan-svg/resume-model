from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest

from engine.cache import (
    CacheStats,
    FileExtractionCache,
    NullExtractionCache,
    cached_extract_candidate,
    extraction_cache_key,
    get_extraction_cache,
)
from engine.config import get_settings
from engine.extraction import extract_job_requirements
from engine.normalize import load_ontology
from engine.pipeline import load_documents, screen
from tests.conftest import SAMPLE_DIR, FakeLLM, outage

RESUME = "Experience\nML Engineer | Acme | Jan 2021 - Present\n- PyTorch on Kubernetes\nSkills\nPython, SQL\n"


def test_cache_hit_returns_same_result_without_re_extracting(tmp_path: Path) -> None:
    cache, stats = FileExtractionCache(tmp_path), CacheStats()
    first = cached_extract_candidate(RESUME, None, cache, stats)
    second = cached_extract_candidate(RESUME, None, cache, stats)
    assert not first.from_cache and second.from_cache
    assert second.extraction == first.extraction
    assert (stats.hits, stats.misses, stats.hit_rate) == (1, 1, 0.5)


def test_llm_extraction_is_cached_by_model_and_prompt(tmp_path: Path) -> None:
    from tests.test_extraction import _extraction

    cache = FileExtractionCache(tmp_path)
    llm = FakeLLM(response=_extraction())
    cached_extract_candidate(RESUME, llm, cache)
    cached_extract_candidate(RESUME, llm, cache)
    assert len(llm.calls) == 1  # second call served from the cache


def test_failed_extractions_are_not_cached(tmp_path: Path) -> None:
    from tests.test_extraction import _extraction

    cache = FileExtractionCache(tmp_path)
    failing = FakeLLM(response=_extraction(), errors=outage(4))
    assert cached_extract_candidate(RESUME, failing, cache).status == "extraction_failed"
    retry = cached_extract_candidate(RESUME, failing, cache)  # errors exhausted: now succeeds
    assert retry.status == "ok" and not retry.from_cache


def test_key_depends_on_content_mode_prompt_and_model() -> None:
    settings, ontology = get_settings(), load_ontology()
    llm_a, llm_b = FakeLLM(), FakeLLM()
    llm_b.__class__ = type("OtherModel", (FakeLLM,), {"model": property(lambda self: "other-model")})
    base = extraction_cache_key(RESUME, None, settings, ontology)
    assert base == extraction_cache_key(RESUME, None, settings, ontology)
    assert base != extraction_cache_key(RESUME + " ", None, settings, ontology)
    assert base != extraction_cache_key(RESUME, llm_a, settings, ontology)
    assert extraction_cache_key(RESUME, llm_a, settings, ontology) != extraction_cache_key(
        RESUME, llm_b, settings, ontology
    )


def test_key_changes_when_verification_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    settings, ontology = get_settings(), load_ontology()
    base = extraction_cache_key(RESUME, FakeLLM(), settings, ontology)
    monkeypatch.setattr("engine.cache.VERIFIER_VERSION", -1)
    assert extraction_cache_key(RESUME, FakeLLM(), settings, ontology) != base


def test_corrupt_cache_entry_is_a_miss(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cache = FileExtractionCache(tmp_path)
    key = extraction_cache_key(RESUME, None, get_settings(), load_ontology())
    path = tmp_path / key[:2] / f"{key}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="engine.cache"):
        assert cache.get(key) is None
    assert any(r.getMessage() == "extraction_cache_entry_invalid" for r in caplog.records)


def test_cache_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXTRACTION_CACHE_ENABLED", "false")
    get_settings.cache_clear()
    assert isinstance(get_extraction_cache(), NullExtractionCache)


def test_screening_several_jobs_extracts_each_resume_once(tmp_path: Path) -> None:
    cache = FileExtractionCache(tmp_path)
    docs = load_documents(SAMPLE_DIR / "resumes")
    jd = (SAMPLE_DIR / "jd.txt").read_text(encoding="utf-8")
    req = extract_job_requirements(jd, None).requirements.confirm(by="cache-test")
    other = req.model_copy(update={"role": "Machine Learning Engineer", "experience_min_years": 2})
    first = screen(req, docs, today=date(2026, 10, 5), cache=cache)
    second = screen(other, docs, today=date(2026, 10, 5), cache=cache)
    scored = len(docs) - 1  # the scanned PDF never reaches extraction
    assert (first.run.extraction_cache_hits, first.run.extraction_cache_misses) == (0, scored)
    assert (second.run.extraction_cache_hits, second.run.extraction_cache_misses) == (scored, 0)
    assert all(r.extraction.from_cache for r in [*second.ranked, *second.ineligible])


def test_concurrent_screening_matches_sequential(tmp_path: Path) -> None:
    docs = load_documents(SAMPLE_DIR / "resumes")
    jd = (SAMPLE_DIR / "jd.txt").read_text(encoding="utf-8")
    req = extract_job_requirements(jd, None).requirements.confirm(by="cache-test")
    progress: list[tuple[int, int]] = []
    seq = screen(req, docs, today=date(2026, 10, 5), cache=NullExtractionCache(), max_workers=1)
    par = screen(
        req, docs, today=date(2026, 10, 5), cache=NullExtractionCache(), max_workers=8,
        progress=lambda done, total: progress.append((done, total)),
    )  # fmt: skip

    def key(result):
        return (
            [(r.source_name, r.score, r.rank) for r in result.ranked],
            [(r.source_name, r.score) for r in result.ineligible],
            [(i.source_name, i.status) for i in result.needs_reprocess],
        )

    assert key(seq) == key(par)
    assert par.run.max_workers == 8
    assert progress[-1] == (len(docs), len(docs))
    assert [d for d, _ in progress] == sorted(d for d, _ in progress)
