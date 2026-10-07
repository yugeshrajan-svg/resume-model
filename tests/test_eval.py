from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from engine.explain import load_similarity_thresholds, template_explanation
from engine.features import FEATURE_NAMES
from engine.normalize import normalize_candidate
from engine.schemas import CandidateExtraction, FeatureResult, JobItem
from engine.scorer import GoldenExample
from eval.calibration import best_threshold, calibrate
from eval.evaluate import evaluate
from eval.golden import GOLDEN_DIR, REQUIRED_LAYOUTS, AnnotatedJob, load_golden
from eval.metrics import (
    aggregate_drop_rates,
    date_matches,
    f1_from_counts,
    precision_at_k,
    recall_in_top_fraction,
    skill_prf,
)
from eval.run_eval import BASELINE_PATH, check_regressions, main

# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_precision_at_k() -> None:
    assert precision_at_k([3, 2, 0, 1, 2, 3], 5) == 0.6
    assert precision_at_k([3, 1], 5) == 0.5
    assert precision_at_k([], 5) == 0.0


def test_recall_in_top_fraction() -> None:
    labels = [3, 0, 2, 0, 0, 0, 0, 0, 0, 2]  # top 20% = 2 slots; relevant = 3
    assert recall_in_top_fraction(labels, 0.2) == pytest.approx(1 / 3)
    assert recall_in_top_fraction([0, 1, 0]) is None


def test_skill_prf_canonicalizes() -> None:
    assert skill_prf(["k8s", "Python", "Rust"], ["Kubernetes", "Python", "SQL"]) == (2, 1, 1)
    assert f1_from_counts(2, 1, 1) == pytest.approx(2 / 3)
    assert f1_from_counts(0, 3, 3) == 0.0


def test_date_matches_present_and_titles() -> None:
    extraction = CandidateExtraction(
        skills=[],
        jobs=[
            JobItem(title="ML Engineer", company=None, start="Jan 2022", end="Present", description=None, evidence="x"),
            JobItem(title="Analyst", company=None, start="Mar 2019", end="Dec 2021", description=None, evidence="x"),
        ],
        projects=[], education=[], certifications=[], location=None, notice_period_days=None, summary=None,
    )  # fmt: skip
    from datetime import date

    cand = normalize_candidate(extraction, today=date(2026, 10, 5))
    annotated = [
        AnnotatedJob(title="ml engineer", start="2022-01", end="present"),
        AnnotatedJob(title="Analyst", start="2019-03", end="2021-11"),  # wrong end
        AnnotatedJob(title="Intern", start="2018-01", end="2018-06"),  # not extracted
    ]
    assert [ok for _, ok in date_matches(annotated, cand)] == [True, False, False]


def test_aggregate_drop_rates_by_prompt_version() -> None:
    rows = [
        ("llm:resume_extraction@1.1.0", {"skills": (10, 1), "jobs": (2, 0)}),
        ("llm:resume_extraction@1.1.0", {"skills": (10, 3)}),
        ("rules", {"skills": (5, 0)}),
    ]
    report = aggregate_drop_rates(rows)
    assert report["llm:resume_extraction@1.1.0"]["skills"] == 0.2
    assert report["llm:resume_extraction@1.1.0"]["overall"] == pytest.approx(4 / 22, abs=1e-4)
    assert report["rules"] == {"skills": 0.0, "overall": 0.0}


# ---------------------------------------------------------------------------
# Golden set
# ---------------------------------------------------------------------------


def test_golden_set_is_valid_and_covers_required_layouts() -> None:
    golden = load_golden()
    layouts = {a.layout for a in golden.annotations.values()}
    assert layouts >= REQUIRED_LAYOUTS and "start_only" in layouts
    assert any(not a.has_projects for a in golden.annotations.values())
    assert len(golden.labels) == len(golden.jobs) * len(golden.annotations)
    for job_id in golden.jobs:
        labels = golden.labels_for(job_id).values()
        assert any(lb >= 2 for lb in labels) and any(lb <= 1 for lb in labels)


def test_golden_loader_rejects_problems(tmp_path: Path) -> None:
    shutil.copytree(GOLDEN_DIR, tmp_path / "golden", ignore=shutil.ignore_patterns("*.py"))
    root = tmp_path / "golden"
    with (root / "labels.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"job_id": "ml_engineer", "candidate_file": "missing.txt", "label": 2}) + "\n")
    with pytest.raises(ValueError, match="missing resume"):
        load_golden(root)


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def test_best_threshold_separates_classes() -> None:
    pairs = [(0.1, 0), (0.15, 1), (0.2, 1), (0.4, 2), (0.5, 3), (0.6, 2)]
    result = best_threshold(pairs)
    assert result and result["threshold"] == pytest.approx(0.3) and result["balanced_accuracy"] == 1.0
    assert best_threshold([(0.1, 0), (0.2, 1)]) is None  # one class only


def test_calibrate_ignores_na_values() -> None:
    examples = [
        GoldenExample(
            job_id="j",
            candidate_id=f"c{i}",
            features={**dict.fromkeys(FEATURE_NAMES, 0.5), "project_relevance": None, "semantic_relevance": v},
            label=lb,
        )
        for i, (v, lb) in enumerate([(0.1, 0), (0.2, 1), (0.6, 2), (0.7, 3)])
    ]
    assert set(calibrate(examples)) == {"semantic_relevance"}


def _features(**values: float) -> list[FeatureResult]:
    return [
        FeatureResult(name=n, value=values.get(n, 0.9), detail=f"{n} detail", evidence=["q"]) for n in FEATURE_NAMES
    ]


def test_template_uses_calibrated_similarity_threshold() -> None:
    thresholds = load_similarity_thresholds()
    assert "tfidf" in thresholds, "run: python -m eval.run_eval --calibrate"
    t = thresholds["tfidf"]["semantic_relevance"]
    low = template_explanation(
        _features(semantic_relevance=t - 0.05), [], similarity_backends={"semantic_relevance": "tfidf"}
    )
    assert [g.feature for g in low.gaps] == ["semantic_relevance"] and "calibrated" in low.gaps[0].text
    high = template_explanation(
        _features(semantic_relevance=t + 0.05), [], similarity_backends={"semantic_relevance": "tfidf"}
    )
    assert high.gaps == []
    uncalibrated = template_explanation(
        _features(semantic_relevance=0.01), [], similarity_backends={"semantic_relevance": "cross-encoder"}
    )
    assert uncalibrated.gaps == []


# ---------------------------------------------------------------------------
# End to end + CI gate
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def golden_report():
    return evaluate(load_golden(), None)


def test_golden_metrics_meet_the_baseline(golden_report) -> None:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["rules"]
    assert check_regressions(golden_report.metrics, baseline) == []
    assert golden_report.drop_rates["rules"]["overall"] == 0.0


def test_every_layout_gets_correct_dates(golden_report) -> None:
    golden = load_golden()
    for f, ann in golden.annotations.items():
        assert golden_report.per_resume[f]["dates_wrong"] == [], (ann.layout, f)


def test_check_regressions_tolerance() -> None:
    baseline = {"metrics": {"ndcg_at_10": 0.9, "skill_f1": 0.9}, "tolerance": 0.02}
    metrics = {"ndcg_at_10": 0.885, "precision_at_5": 1, "recall_top20pct": 1, "skill_f1": 0.87, "date_accuracy": 1}
    failures = check_regressions(metrics, baseline)
    assert len(failures) == 1 and failures[0].startswith("skill_f1")


def test_run_eval_exits_non_zero_on_regression(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    inflated = {"rules": {"metrics": {"ndcg_at_10": 1.0, "skill_f1": 1.0, "date_accuracy": 1.0}, "tolerance": 0.0}}
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(inflated), encoding="utf-8")
    assert main(["--baseline", str(path)]) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and "ndcg_at_10" in out and "Fairness counterfactuals" in out


def test_ltr_comparison(golden_report) -> None:
    pytest.importorskip("lightgbm")
    from eval.ltr import compare_ltr

    result = compare_ltr(golden_report.examples)
    assert result["folds"] == 3
    for key in ("default_weights", "fit_weights", "lambdamart"):
        assert 0.0 <= result[key] <= 1.0


def test_error_analysis_covers_misses_and_false_positives(golden_report) -> None:
    from eval.error_analysis import analyze

    golden = load_golden()
    markdown, cases = analyze(golden_report, {j: golden.labels_for(j) for j in golden.jobs})
    assert "## Label >= 2 outside the top 20%" in markdown and "## Top-5 false positives" in markdown
    for c in cases:
        assert c["kind"] in {"missed_relevant", "top5_false_positive"}
        assert c["root_cause"] and c["features"]
        if c["kind"] == "missed_relevant":
            assert c["label"] >= 2 and c["position"] > 4  # 16 resumes: top 20% = 4 slots
        else:
            assert c["label"] < 2 and c["position"] <= 5


def test_real_set_needs_only_labels(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    real = tmp_path / "real"
    shutil.copytree(GOLDEN_DIR, real, ignore=shutil.ignore_patterns("*.py", "annotations.jsonl"))
    golden = load_golden(real, synthetic=False)
    assert golden.annotations == {} and len(golden.resume_files) == 16
    with pytest.raises(FileNotFoundError):
        load_golden(real)  # the synthetic golden set requires annotations

    code = main(["--real", "--golden", str(real), "--baseline", str(tmp_path / "b.json"),
                 "--error-analysis", str(real / "reports" / "error_analysis.md")])  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0
    assert "skill F1                n/a" in out and "No baseline 'real:rules'" in out
    assert (real / "reports" / "error_analysis.md").exists()


def test_r_precision() -> None:
    from eval.metrics import r_precision

    assert r_precision([3, 2, 0, 1, 2]) == pytest.approx(2 / 3)  # R = 3; top 3 holds 2 relevant
    assert r_precision([3, 2, 2, 0]) == 1.0
    assert r_precision([0, 1, 0]) is None
