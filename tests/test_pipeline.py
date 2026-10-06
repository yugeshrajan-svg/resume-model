from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from engine.extraction import extract_job_requirements
from engine.llm import LLMError, LLMResult
from engine.pipeline import RequirementsNotConfirmed, load_documents, main, screen
from engine.prompts import get_prompt
from engine.schemas import CandidateExtraction, ExplanationDraft, JobItem, JobRequirements, LLMUsage, SkillItem
from tests.conftest import SAMPLE_DIR

TODAY = date(2026, 10, 5)


@pytest.fixture
def requirements() -> JobRequirements:
    jd = (SAMPLE_DIR / "jd.txt").read_text(encoding="utf-8")
    return extract_job_requirements(jd, None).requirements.confirm(by="Test Reviewer")


@pytest.fixture
def documents() -> list[tuple[str, bytes]]:
    return load_documents(SAMPLE_DIR / "resumes")


def test_refuses_unconfirmed_requirements(requirements: JobRequirements, documents) -> None:
    unconfirmed = requirements.model_copy(update={"requirements_confirmed": None})
    with pytest.raises(RequirementsNotConfirmed):
        screen(unconfirmed, documents)


def test_sample_batch_offline(requirements: JobRequirements, documents) -> None:
    result = screen(requirements, documents, today=TODAY)

    # Ranked section: eligible candidates only, ranked by score.
    assert [r.rank for r in result.ranked] == list(range(1, len(result.ranked) + 1))
    assert all(r.eligible for r in result.ranked)
    assert [r.source_name for r in result.ranked] == ["01_strong_match.pdf", "02_overlapping_jobs.docx"]
    assert [r.score for r in result.ranked] == sorted((r.score for r in result.ranked), reverse=True)
    # Eligible under the 80% default, but the missing required skill is still reported as a gap.
    assert result.ranked[1].knockout.missing_required_skills == ["Kubernetes"]

    # Ineligible section: kept with reasons, unranked, by score.
    ineligible = {r.source_name: r for r in result.ineligible}
    assert all(r.rank is None and not r.eligible and r.knockout.reasons for r in result.ineligible)
    assert ineligible["07_location_notice.txt"].knockout.reasons[0].rule == "notice_period"
    assert "03_hidden_injection.pdf" in ineligible
    assert [r.score for r in result.ineligible] == sorted((r.score for r in result.ineligible), reverse=True)

    assert [(i.source_name, i.status) for i in result.needs_reprocess] == [("08_scanned_needs_ocr.pdf", "needs_ocr")]
    assert len(result.ranked) + len(result.ineligible) + len(result.needs_reprocess) == len(documents)

    run = result.run
    assert run.extraction_mode == "rules" and run.semantic_backend == "tfidf"
    assert run.prompt_version is None and run.ontology_version
    assert run.requirements_confirmed.by == "Test Reviewer"
    assert sum(run.weights.values()) == pytest.approx(1.0)
    for r in [*result.ranked, *result.ineligible]:
        assert 0 <= r.score <= 100
        assert len(r.features) == 6
        assert r.extraction.method == "rules"
        assert sum(r.score_breakdown.weights.values()) == pytest.approx(1.0, abs=1e-3)


def test_screening_is_deterministic(requirements: JobRequirements, documents) -> None:
    a = screen(requirements, documents, today=TODAY)
    b = screen(requirements, documents, today=TODAY)
    key = [(r.source_name, r.score) for r in [*a.ranked, *a.ineligible]]
    assert key == [(r.source_name, r.score) for r in [*b.ranked, *b.ineligible]]


def test_hidden_injection_does_not_raise_score(requirements: JobRequirements) -> None:
    hidden = (SAMPLE_DIR / "resumes" / "03_hidden_injection.pdf").read_bytes()
    result = screen(requirements, [("03.pdf", hidden)], today=TODAY)
    assert result.ranked == []
    r = result.ineligible[0]
    assert r.hidden_items_removed == 3
    assert r.score < 20


def test_duplicates_and_bad_files_go_to_reprocess(requirements: JobRequirements) -> None:
    good = (SAMPLE_DIR / "resumes" / "05_intern.txt").read_bytes()
    docs = [("a.txt", good), ("copy.txt", good), ("broken.pdf", b"not a pdf"), ("evil.exe", b"MZ")]
    result = screen(requirements, docs, today=TODAY)
    assert [r.source_name for r in [*result.ranked, *result.ineligible]] == ["a.txt"]
    statuses = {i.source_name: i.status for i in result.needs_reprocess}
    assert statuses == {"copy.txt": "duplicate", "broken.pdf": "parse_error", "evil.exe": "parse_error"}


# ---------------------------------------------------------------------------
# LLM mode: failures are excluded, never re-extracted by rules
# ---------------------------------------------------------------------------

RESUME_OK = b"""Experience
Senior Machine Learning Engineer | Nimbus | Jan 2020 - Present
- PyTorch, Python, SQL, Docker, Kubernetes and Azure
Location: Bengaluru, India
Notice period: 30 days
"""
RESUME_FAIL = b"Experience\nData Engineer | Acme | 2019 - 2021\nFAIL_ME\n" + b"x" * 300


class SelectiveLLM:
    """Succeeds with a fixed extraction, except for documents containing FAIL_ME."""

    provider_name = "fake"
    model = "fake-model-1"

    def __init__(self) -> None:
        self.calls = 0

    def parse(self, *, system: str, user: str, schema, temperature: float = 0.0) -> LLMResult:
        self.calls += 1
        if "FAIL_ME" in user:
            raise LLMError("upstream 503")
        if schema is ExplanationDraft:
            return LLMResult(
                parsed=ExplanationDraft(strengths=[], gaps=[]), usage=LLMUsage(provider="fake", model=self.model)
            )
        extraction = CandidateExtraction(
            skills=[SkillItem(name=s, evidence=s) for s in ("PyTorch", "Python", "SQL", "Docker", "Kubernetes")],
            jobs=[
                JobItem(
                    title="Senior Machine Learning Engineer", company="Nimbus", start="Jan 2020", end="Present",
                    description="PyTorch, Python, SQL, Docker, Kubernetes and Azure",
                    evidence="Senior Machine Learning Engineer | Nimbus | Jan 2020 - Present",
                )
            ],
            projects=[], education=[], certifications=[], location=None, notice_period_days=None, summary=None,
        )  # fmt: skip
        return LLMResult(parsed=extraction, usage=LLMUsage(provider="fake", model=self.model, prompt_tokens=10))


def test_llm_failures_are_excluded_and_listed_for_reprocessing(requirements: JobRequirements) -> None:
    llm = SelectiveLLM()
    result = screen(requirements, [("ok.txt", RESUME_OK), ("fail.txt", RESUME_FAIL)], llm=llm, today=TODAY)

    scored = [*result.ranked, *result.ineligible]
    assert [r.source_name for r in scored] == ["ok.txt"]
    assert scored[0].extraction.method == "llm"
    assert scored[0].extraction.prompt_version == get_prompt("resume_extraction").version
    [failed] = result.needs_reprocess
    assert failed.source_name == "fail.txt" and failed.status == "extraction_failed"
    assert "upstream 503" in failed.reason
    assert result.run.extraction_mode == "llm" and result.run.model == "fake-model-1"
    assert llm.calls == 1 + 4 + 1  # one extraction; the failing resume tried 1 + 3 retries; one explanation
    assert result.run.llm_prompt_tokens == 10


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_requires_confirmation(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--jd", str(SAMPLE_DIR / "jd.txt"), "--resumes", str(SAMPLE_DIR / "resumes")])
    out, err = capsys.readouterr()
    assert code == 2
    assert json.loads(out)["role"] == "Senior Machine Learning Engineer"  # extracted requirements for review
    assert "--confirmed-by" in err


def test_cli_prints_ranked_table_and_writes_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_json = tmp_path / "result.json"
    code = main(
        [
            "--jd", str(SAMPLE_DIR / "jd.txt"), "--resumes", str(SAMPLE_DIR / "resumes"),
            "--confirmed-by", "CLI Reviewer", "--today", "2026-10-05", "--json", str(out_json),
        ]
    )  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0
    assert "01_strong_match.pdf" in out
    assert out.index("RANKED") < out.index("INELIGIBLE") < out.index("NEEDS REPROCESSING")
    assert "- Missing required skills: Kubernetes" in out
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["run"]["requirements_confirmed"]["by"] == "CLI Reviewer"


def test_cli_rejects_requirements_file_without_confirmation(tmp_path: Path, requirements: JobRequirements) -> None:
    path = tmp_path / "req.json"
    path.write_text(requirements.model_copy(update={"requirements_confirmed": None}).model_dump_json(), "utf-8")
    assert main(["--requirements", str(path), "--resumes", str(SAMPLE_DIR / "resumes")]) == 2
