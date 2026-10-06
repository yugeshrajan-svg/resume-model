"""Evaluate the engine on the golden set; exit non-zero on regression (CI gate).

    python -m eval.run_eval                     # report + compare with eval/baseline.json
    python -m eval.run_eval --update-baseline   # accept current metrics as the new baseline
    python -m eval.run_eval --calibrate         # write similarity "low" thresholds for this backend
    python -m eval.run_eval --ltr               # also compare LambdaMART vs fitted weights (needs .[ltr])

Exit codes: 0 ok, 1 regression or fairness failure, 2 usage/config error.
The baseline is kept per extraction mode ("rules" offline, "llm" online).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from engine.config import ConfigError, get_settings
from engine.extraction import extract_job_requirements
from engine.llm import get_llm
from engine.logging_setup import configure_logging
from eval.calibration import calibrate, calibrate_leave_one_job_out, write_calibration
from eval.error_analysis import REPORT_PATH, analyze, write_report
from eval.evaluate import EvalReport, evaluate
from eval.fairness import FairnessCase, run_counterfactuals
from eval.golden import GOLDEN_DIR, REAL_DIR, load_golden

BASELINE_PATH = Path(__file__).with_name("baseline.json")
FAIRNESS_MAX_DELTA = 1.0
FAIRNESS_BASES = ("g01_ml_below.txt", "g05_de_below.txt", "g07_da_startonly.txt")
GATED_METRICS = ("ndcg_at_10", "precision_at_5", "recall_top20pct", "r_precision", "skill_f1", "date_accuracy")


def check_regressions(metrics: dict[str, float], baseline: dict) -> list[str]:
    """Metrics that fell more than the tolerance below the baseline."""
    tolerance = baseline.get("tolerance", 0.02)
    out = []
    for name in GATED_METRICS:
        base = baseline["metrics"].get(name)
        value = metrics.get(name)
        if base is not None and value is not None and value < base - tolerance:
            out.append(f"{name}: {value:.4f} < baseline {base:.4f} - tolerance {tolerance}")
    return out


def fairness_failures(cases: Sequence[FairnessCase]) -> list[str]:
    out = []
    for c in cases:
        if c.delta >= FAIRNESS_MAX_DELTA:
            out.append(f"{c.job_id}/{c.base_file}: score delta {c.delta:.2f} >= {FAIRNESS_MAX_DELTA} ({c.worst_pair})")
        if not c.eligibility_consistent:
            out.append(f"{c.job_id}/{c.base_file}: eligibility differs across name/gender variants")
        if not c.explanation_consistent:
            out.append(f"{c.job_id}/{c.base_file}: explanation differs in substance across variants")
    return out


def format_report(
    report: EvalReport,
    cases: Sequence[FairnessCase],
    calibration: dict,
    backend: str,
    ltr: dict | None,
    scope: str = "full",
) -> str:
    m = {k: ("n/a" if v is None else f"{v:.4f}") for k, v in report.metrics.items()}
    lines = [
        f"GOLDEN SET EVALUATION  (extraction={report.mode}, semantic={backend})",
        "",
        "Ranking (mean over jobs; order = ranked, then ineligible, then needs-reprocess)",
        f"  NDCG@10                 {m['ndcg_at_10']}",
        f"  precision@5 (label>=2)  {m['precision_at_5']}",
        f"  recall@top20% (>=2)     {m['recall_top20pct']}",
        f"  R-precision (>=2)       {m['r_precision']}",
    ]
    for j in report.jobs:
        lines.append(
            f"    {j.job_id:<14} ndcg={j.ndcg_at_10 or 0:.3f} p@5={j.precision_at_5:.2f} "
            f"r@20%={j.recall_top20pct or 0:.2f} rprec={j.r_precision or 0:.2f}  top3={j.order[:3]}"
        )
    tp, fp, fn = report.skill_counts
    lines += [
        "",
        "Extraction",
        f"  skill F1                {m['skill_f1']}   (tp={tp} fp={fp} fn={fn})",
        f"  date accuracy           {m['date_accuracy']}   "
        f"(exact {report.date_exact[0]}/{report.date_exact[1]}, "
        f"year-only/start-only {report.date_approx[0]}/{report.date_approx[1]})",
    ]
    misses = {f: v for f, v in report.per_resume.items() if any(v.get(k) for k in v)}
    for f, v in misses.items():
        lines.append(f"    {f}: {v}")
    lookups = report.cache_hits + report.cache_misses
    hit_rate = report.cache_hits / lookups if lookups else 0.0
    lines.append(f"  extraction cache        {report.cache_hits}/{lookups} hits ({hit_rate:.0%})")
    lines += ["", "Evidence drop rates (per field, by extraction prompt version)"]
    for group, rates in report.drop_rates.items():
        lines.append(f"  {group}: overall {rates['overall']:.4f}")
        lines.append("    " + ", ".join(f"{k}={v:.3f}" for k, v in rates.items() if k != "overall"))
    lines += [
        "",
        f"Fairness counterfactuals (scope={scope}: {len(cases)} job x resume cases, name/gender/pronoun swaps)",
    ]
    for c in cases:
        status = (
            "ok" if c.delta < FAIRNESS_MAX_DELTA and c.eligibility_consistent and c.explanation_consistent else "FAIL"
        )
        lines.append(
            f"  {status:<4} {c.job_id:<14} {c.base_file:<24} delta={c.delta:.3f} "
            f"eligibility_same={c.eligibility_consistent} explanation_same={c.explanation_consistent}"
        )
    lines += ["", f"Similarity 'low' thresholds calibrated on golden labels (backend={backend})"]
    for feature, r in calibration.items():
        heldout = r.get("heldout_balanced_accuracy")
        lines.append(
            f"  {feature:<20} threshold={r['threshold']:.4f} balanced_acc in-sample={r['balanced_accuracy']:.3f} "
            f"held-out={heldout if heldout is not None else 'n/a'} (leave-one-job-out, {r.get('folds', 0)} folds) "
            f"n={r['n']}"
        )
    if ltr is not None:
        lines += ["", "Learning-to-rank comparison (leave-one-job-out mean NDCG@10)"]
        lines += [f"  {k:<16} {v}" for k, v in ltr.items()]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.run_eval", description="Golden-set evaluation / CI gate.")
    parser.add_argument("--golden", type=Path, default=None, help="labelled set (default: eval/golden)")
    parser.add_argument("--real", action="store_true", help="evaluate the real labelled set in eval/real (git-ignored)")
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--calibrate", action="store_true", help="write similarity thresholds for this backend")
    parser.add_argument("--ltr", action="store_true", help="compare LambdaMART (LightGBM) vs fitted weights")
    parser.add_argument("--json", type=Path, help="write metrics as JSON")
    parser.add_argument("--error-analysis", type=Path, default=None, help="where to write the error analysis")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging("ERROR", settings.log_json)
    try:
        root = args.golden or (REAL_DIR if args.real else GOLDEN_DIR)
        golden = load_golden(root, synthetic=not args.real)
        llm = get_llm(settings)
        backend = settings.resolved_semantic_backend  # scorer names match the backend names
    except (ValueError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = evaluate(golden, llm, settings)
    requirements = {
        job_id: extract_job_requirements(text, llm, settings=settings).requirements.confirm(by="fairness-eval")
        for job_id, text in golden.jobs.items()
    }
    cases: list[FairnessCase] = []
    if not args.real:  # counterfactual bases are synthetic golden resumes with a known header layout
        bases = {f: golden.resume_path(f).read_text(encoding="utf-8") for f in FAIRNESS_BASES}
        cases = run_counterfactuals(
            requirements, bases, golden.reference_date, settings, llm, scope=settings.fairness_scope
        )
    calibration = calibrate(report.examples)
    for feature, loo in calibrate_leave_one_job_out(report.examples).items():
        if feature in calibration:
            calibration[feature].update(loo)
    ltr = None
    if args.ltr:
        from eval.ltr import compare_ltr

        try:
            ltr = compare_ltr(report.examples)
        except ImportError as exc:
            ltr = {"skipped": str(exc)}  # type: ignore[dict-item]

    labels = {job_id: golden.labels_for(job_id) for job_id in golden.jobs}
    analysis_md, analysis_cases = analyze(report, labels)
    # Real-set reports name real candidates' files: keep them inside the git-ignored eval/real folder.
    report_path = args.error_analysis or (REAL_DIR / "reports" / "error_analysis.md" if args.real else REPORT_PATH)
    analysis_path = write_report(analysis_md, report_path)
    print(format_report(report, cases, calibration, backend, ltr, scope=settings.fairness_scope))

    engine_issues = sum(c["engine_issue"] for c in analysis_cases)
    print(f"\nError analysis: {len(analysis_cases)} cases ({engine_issues} engine issues) -> {analysis_path}")
    if args.calibrate:
        write_calibration(backend, calibration)
        print(f"\nWrote similarity thresholds for backend '{backend}'.")
    if args.json:
        payload = {
            "mode": report.mode,
            "metrics": report.metrics,
            "drop_rates": report.drop_rates,
            "fairness_max_delta": max((c.delta for c in cases), default=None),
            "calibration": calibration,
            "ltr": ltr,
        }
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    baselines = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline.exists() else {}
    mode_key = f"real:{report.mode}" if args.real else report.mode
    if args.update_baseline:
        baselines[mode_key] = {
            "metrics": report.metrics,
            "tolerance": baselines.get(mode_key, {}).get("tolerance", 0.02),
        }
        args.baseline.write_text(json.dumps(baselines, indent=2) + "\n", encoding="utf-8")
        print(f"\nBaseline '{mode_key}' updated.")

    failures = fairness_failures(cases)
    if mode_key in baselines and not args.update_baseline:
        failures += check_regressions(report.metrics, baselines[mode_key])
    elif mode_key not in baselines:
        print(f"\nNo baseline '{mode_key}'; run with --update-baseline to create one.")
    if failures:
        print("\nFAILED:\n  " + "\n  ".join(failures))
        return 1
    print("\nPASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
