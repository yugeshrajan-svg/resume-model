"""End-to-end smoke test against the real Azure OpenAI deployment.

Parses and extracts one JD and one resume, then prints tokens, latency and
estimated cost per document.

Usage:
    python scripts/smoke_azure.py [--jd data/sample/jd.txt] [--resume data/sample/resumes/01_strong_match.pdf]

Exit codes: 0 ok, 1 extraction failed, 2 credentials missing.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.config import Settings  # noqa: E402
from engine.extraction import ExtractionFailed, extract_candidate, extract_job_requirements  # noqa: E402
from engine.llm import AzureOpenAIProvider  # noqa: E402
from engine.logging_setup import configure_logging  # noqa: E402
from engine.parsing import parse_file  # noqa: E402
from engine.schemas import LLMUsage, evidence_stats_summary  # noqa: E402


def _row(label: str, usage: LLMUsage | None, attempts: int, wall_ms: float, extra: str) -> str:
    if usage is None:
        return f"{label:<8} {'-':>7} {'-':>7} {'-':>9} {wall_ms:>9.0f} {'-':>10} {attempts:>3}  {extra}"
    return (
        f"{label:<8} {usage.prompt_tokens:>7} {usage.completion_tokens:>7} {usage.latency_ms:>9.0f} "
        f"{wall_ms:>9.0f} {usage.cost_usd:>10.5f} {attempts:>3}  {extra}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jd", type=Path, default=ROOT / "data/sample/jd.txt")
    parser.add_argument("--resume", type=Path, default=ROOT / "data/sample/resumes/01_strong_match.pdf")
    args = parser.parse_args()

    settings = Settings()
    if not settings.has_azure_credentials:
        print("AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY must be set.", file=sys.stderr)
        return 2
    configure_logging(settings.log_level, settings.log_json)
    llm = AzureOpenAIProvider(settings)
    print(f"deployment={llm.model} api_version={settings.azure_openai_api_version}")
    print(f"{'doc':<8} {'in_tok':>7} {'out_tok':>7} {'llm_ms':>9} {'wall_ms':>9} {'cost_usd':>10} {'try':>3}  detail")

    total_cost = 0.0
    t0 = time.perf_counter()
    try:
        jd = extract_job_requirements(args.jd.read_text(encoding="utf-8"), llm, settings=settings)
    except ExtractionFailed as exc:
        print(f"JD extraction failed: {exc}", file=sys.stderr)
        return 1
    req = jd.requirements
    detail = f"required={[s.name for s in req.required_skills]} drop_rates={evidence_stats_summary(jd.evidence_stats)}"
    print(_row("jd", jd.usage, jd.attempts, (time.perf_counter() - t0) * 1000, detail))
    total_cost += jd.usage.cost_usd if jd.usage else 0.0

    t0 = time.perf_counter()
    doc = parse_file(args.resume, settings=settings)
    result = extract_candidate(doc.text, llm, settings=settings)
    wall = (time.perf_counter() - t0) * 1000
    if result.status != "ok" or result.extraction is None:
        print(_row("resume", None, result.attempts, wall, f"FAILED: {result.failure_reason}"))
        return 1
    e = result.extraction
    detail = (
        f"skills={len(e.skills)} jobs={len(e.jobs)} hidden_removed={doc.hidden_items_removed} "
        f"drop_rates={result.evidence_drop_rates}"
    )
    print(_row("resume", result.usage, result.attempts, wall, detail))
    total_cost += result.usage.cost_usd if result.usage else 0.0
    print(f"total estimated cost: ${total_cost:.5f} (prices from LLM_PRICE_INPUT_PER_1K / LLM_PRICE_OUTPUT_PER_1K)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
