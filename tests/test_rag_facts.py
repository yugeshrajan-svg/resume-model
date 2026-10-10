"""Structured candidate facts: located evidence, support levels, warnings, conflicts, unknowns and review items."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from engine.extraction import extract_candidate, normalize_for_match
from engine.schemas import CandidateExtraction, DroppedItem, ExtractionResult, JobItem, SkillItem
from retrieval.facts import FactSet, build_fact_set
from retrieval.layout import analyze
from tests.test_rag_document_prep import RESUME

TODAY = date(2026, 10, 5)


def facts_for(text: str) -> FactSet:
    facts, _ = build_fact_set(extract_candidate(text, None), analyze(text, kind="txt"), TODAY)
    return facts


def test_every_located_evidence_quote_is_the_exact_source_text() -> None:
    facts = facts_for(RESUME)
    refs = [r for s in facts.skills for r in s.evidence] + [j.evidence for j in facts.employment]
    refs += [r for j in facts.employment for r in j.responsibilities] + [e.evidence for e in facts.education]
    assert refs
    for ref in refs:
        assert ref.char_start is not None and ref.line is not None
        assert normalize_for_match(RESUME[ref.char_start : ref.char_end]) == normalize_for_match(ref.quote)


def test_skill_support_distinguishes_listed_from_demonstrated() -> None:
    facts = facts_for(RESUME)
    skills = {s.name: s for s in facts.skills}
    assert skills["Python"].support == "demonstrated" and skills["Python"].listed
    assert skills["Python"].entries == ["employment:0"]
    assert skills["PostgreSQL"].entries == ["employment:0", "project:0"]
    assert skills["Docker"].support == "listed" and not skills["Docker"].demonstrated
    assert skills["Go"].support == "listed"  # ambiguous alias: only as a skills-list item
    assert all(s.confidence == "verified" for s in facts.skills)
    acme = facts.employment[0]
    assert acme.title == "Senior Backend Engineer" and acme.current and {"FastAPI", "Python"} <= set(acme.skills_used)


def test_protected_lines_never_become_facts_or_evidence() -> None:
    facts = facts_for(RESUME)
    quotes = " ".join(
        r.quote for r in [*(e for s in facts.skills for e in s.evidence)] + [j.evidence for j in facts.employment]
    )
    assert "Birth" not in facts.model_dump_json() and "Married" not in quotes


def test_accomplishments_leadership_and_claimed_experience() -> None:
    facts = facts_for(RESUME)
    metrics = {m for a in facts.accomplishments for m in a.metrics}
    assert any("12k requests" in m for m in metrics) and any("40%" in m for m in metrics)
    assert all(a.context == "employment:0" for a in facts.accomplishments)
    assert facts.seniority.level == "senior" and facts.seniority.basis == "most_recent_dated_role"
    assert [r.quote for r in facts.seniority.leadership] == ["- Mentored a team of 4 engineers"]
    claimed = (
        "Summary\nEngineer with 15 years of experience.\nExperience\nEngineer | Acme | Jan 2023 - Present\n- Built APIs"
    )
    warning = next(w for w in facts_for(claimed).warnings if w.code == "claimed_experience_exceeds_dated_roles")
    assert warning.severity == "review" and "15 years" in warning.message


def test_conflicting_statements_are_kept_side_by_side_for_review() -> None:
    text = RESUME + "\nAdditional Information\nAvailable to join after 90 days\nBased in: Chennai\n"
    facts = facts_for(text)
    assert facts.notice_period_days.status == "conflicting" and facts.notice_period_days.value is None
    assert sorted(v.value for v in facts.notice_period_days.values) == ["30", "90"]
    assert facts.location.status == "conflicting"
    reasons = {(item.field, item.reason) for item in facts.review_items}
    assert ("notice_period_days", "conflicting_statements") in reasons
    assert ("location", "conflicting_statements") in reasons
    assert facts.review_required


def test_dates_overlaps_unknowns_and_preferences() -> None:
    text = (
        "Location: Remote\nOpen to relocation within India. Prefer hybrid work.\n"
        "Experience\nData Engineer | Harbor | 2024\n- Maintained Airflow DAGs\n"
        "Analyst | Maple | Jan 2023 - Present\n- Wrote SQL reports\n"
    )
    facts = facts_for(text)
    codes = {w.code for w in facts.warnings}
    assert "date_low_confidence" in codes and "employment_overlap" in codes
    assert facts.overlaps and facts.overlaps[0].months >= 1
    assert facts.notice_period_days.status == "unknown" and "notice_period_days" in facts.unknown
    assert {"education", "certifications"} <= set(facts.unknown)
    assert {p.kind for p in facts.preferences} >= {"relocation", "work_mode"}
    assert facts.location.status == "stated" and facts.location.value == "Remote"


def test_extraction_failures_and_rejected_claims_are_review_items() -> None:
    failed = ExtractionResult(status="extraction_failed", extraction=None, failure_reason="timeout", method="llm")
    facts, anchors = build_fact_set(failed, analyze(RESUME), TODAY)
    assert anchors == [] and facts.review_items[0].reason == "extraction_failed"
    assert set(facts.unknown) >= {"skills", "employment", "notice_period_days"}

    llm = ExtractionResult(
        status="ok",
        extraction=CandidateExtraction(
            skills=[SkillItem(name="Python", evidence="Python, Go, PostgreSQL, Docker, Kubernetes, FastAPI, Django")],
            jobs=[
                JobItem(
                    title="Senior Backend Engineer",
                    company="Acme Payments",
                    start="Jan 2021",
                    end="Present",
                    description=None,
                    evidence="Senior Backend Engineer | Acme Payments | Jan 2021 - Present",
                )
            ],
            projects=[],
            education=[],
            certifications=[],
            location=None,
            notice_period_days=None,
            summary=None,
        ),
        method="llm",
        dropped_items=[DroppedItem(field="skills", reason="evidence_not_found", evidence="Rust expert")],
    )
    facts, _ = build_fact_set(llm, analyze(RESUME), TODAY)
    assert facts.extraction.unsupported_dropped == {"skills": 1}
    assert any(w.code == "unsupported_claims_rejected" for w in facts.warnings)
    assert "Rust" not in facts.model_dump_json()  # rejected claims are counted, never shown


def test_fact_sets_are_strict_and_versioned() -> None:
    facts = facts_for(RESUME)
    assert facts.schema_version == "1" and facts.policy_version == "facts-1"
    data = facts.model_dump(mode="json")
    assert FactSet.model_validate(data) == facts
    with pytest.raises(ValidationError):
        FactSet.model_validate({**data, "personality": "x"})
    with pytest.raises(ValidationError):
        FactSet.model_validate({**data, "skills": [{**data["skills"][0], "support": "expert"}]})
