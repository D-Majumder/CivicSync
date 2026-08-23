"""
CivicSync Milestone 24: AI evaluation harness.

Runs evaluation/dataset.json through the EXISTING, UNMODIFIED Gemini
complaint-understanding pipeline (ai.client.analyze_complaint) and
computes deterministic metrics -- most importantly, an unsupported-fact
count, which is the single most important safety metric this milestone
exists to measure.

This is a standalone, isolated evaluation tool:

- It does NOT touch civicsync.db or any application database at all --
  analyze_complaint() only calls Gemini and returns a CivicIssue in
  memory; nothing here calls create_issue_from_civic_issue or any other
  persistence function.
- It does NOT duplicate or modify the Gemini prompt/schema in any way --
  it imports and calls the real ai.client.analyze_complaint exactly as
  the production submission flow does.
- It does NOT use an LLM to judge the LLM's own output. Every metric
  below is computed with plain, deterministic Python (string/keyword
  matching, exact equality checks) -- see _text_contains_any() and
  evaluate_case() for the actual rules.
- It requires a real, working GEMINI_API_KEY to produce real results.
  If one isn't configured, this script fails loudly and explains why,
  rather than fabricating or silently skipping results -- consistent
  with the "fail loud, never invent" pattern used throughout this
  project (see e.g. backend.repository.get_default_jurisdiction_id).

Usage (from the repository root):

    python3 evaluation/run_evaluation.py
    python3 evaluation/run_evaluation.py --dataset evaluation/dataset.json --output evaluation/results.json
    python3 evaluation/run_evaluation.py --delay-seconds 20
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dotenv  # noqa: E402

# Guarded so this only ever actually reads .env ONCE per real process,
# using the `dotenv` package object itself as the guard -- it's a
# genuine singleton (sys.modules['dotenv']), so this survives this
# file's own top-level code being re-executed (which
# tests/test_m24_ai_evaluation.py deliberately does, to exercise this
# CLI fresh, e.g. via importlib). Without this guard, a second
# execution of this module would call load_dotenv() again, which reads
# the real .env FILE FROM DISK regardless of what a test has already
# done to os.environ (e.g. monkeypatch.delenv("GEMINI_API_KEY") to
# simulate a machine with no key configured) -- silently restoring a
# real key from .env and making the "no key" code path below
# untestable/non-deterministic on any machine whose real .env actually
# has a key. This has no effect on real (single-process) usage: a
# normal `python3 evaluation/run_evaluation.py` invocation still loads
# .env exactly once, exactly as before.
if not getattr(dotenv, "_civicsync_dotenv_loaded", False):
    dotenv.load_dotenv()
    dotenv._civicsync_dotenv_loaded = True

from ai.client import analyze_complaint  # noqa: E402
from ai.schemas import CivicIssue  # noqa: E402

DEFAULT_DATASET_PATH = Path(__file__).resolve().parent / "dataset.json"
DEFAULT_RESULTS_PATH = Path(__file__).resolve().parent / "results.json"

# The Gemini free tier observed during evaluation enforces
# GenerateRequestsPerMinutePerProject-FreeTier with quotaValue: 5 (i.e.
# roughly one request every 12 seconds to stay under it). 15 seconds is
# a deliberately conservative default -- comfortably under that rate
# rather than exactly at its edge -- and is fully overridable via
# --delay-seconds for a paid tier or a different quota.
DEFAULT_DELAY_SECONDS = 15.0


@dataclass
class CaseResult:
    id: str
    language: str
    input_type: str
    input: str
    status: str  # "ok" | "error"
    error_message: str | None
    category: str | None
    severity: str | None
    problem: str | None
    location: str | None
    duration: str | None
    affected_population: str | None
    confidence: float | None
    expected_category: str | None
    category_correct: bool | None  # None when expected_category is unset -- not measured
    expected_severity: str | None
    severity_correct: bool | None
    unsupported_facts_found: list[str]
    unsupported_fact_count: int


def _text_contains_any(haystack_fields: list[str | None], needles: list[str]) -> list[str]:
    """Deterministic, case-insensitive substring check: does any of the
    AI's own output fields contain any forbidden fact/keyword from the
    case's must_not_invent list?

    This is intentionally simple and stated as a limitation in the
    generated report -- it is a conservative, defensible check (a
    genuinely different paraphrase of a forbidden fact could be missed),
    not an exhaustive semantic hallucination detector. No LLM is
    involved in this check at all, per the milestone's explicit
    requirement.
    """
    haystack = " ".join(f for f in haystack_fields if f).lower()
    return [needle for needle in needles if needle.lower() in haystack]


def evaluate_case(case: dict) -> CaseResult:
    try:
        result: CivicIssue = analyze_complaint(case["input"])
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: record ANY failure honestly
        return CaseResult(
            id=case["id"],
            language=case["language"],
            input_type=case["input_type"],
            input=case["input"],
            status="error",
            error_message=f"{type(exc).__name__}: {exc}",
            category=None,
            severity=None,
            problem=None,
            location=None,
            duration=None,
            affected_population=None,
            confidence=None,
            expected_category=case.get("expected_category"),
            category_correct=None,
            expected_severity=case.get("expected_severity"),
            severity_correct=None,
            unsupported_facts_found=[],
            unsupported_fact_count=0,
        )

    category_value = result.category.value
    severity_value = result.severity.value

    expected_category = case.get("expected_category")
    category_correct = (category_value == expected_category) if expected_category else None

    expected_severity = case.get("expected_severity")
    severity_correct = (severity_value == expected_severity) if expected_severity else None

    must_not_invent = case.get("must_not_invent", [])
    unsupported_found = _text_contains_any(
        [result.problem, result.location, result.duration, result.affected_population, result.suggested_department],
        must_not_invent,
    )

    return CaseResult(
        id=case["id"],
        language=case["language"],
        input_type=case["input_type"],
        input=case["input"],
        status="ok",
        error_message=None,
        category=category_value,
        severity=severity_value,
        problem=result.problem,
        location=result.location,
        duration=result.duration,
        affected_population=result.affected_population,
        confidence=result.confidence,
        expected_category=expected_category,
        category_correct=category_correct,
        expected_severity=expected_severity,
        severity_correct=severity_correct,
        unsupported_facts_found=unsupported_found,
        unsupported_fact_count=len(unsupported_found),
    )


def compute_summary(results: list[CaseResult]) -> dict:
    """Every metric here is computed only over the subset of cases where
    it's actually measurable -- e.g. category accuracy only counts cases
    with an expected_category set, never averaged against the full
    dataset (which would misleadingly dilute or inflate the number).
    """
    total = len(results)
    ok_results = [r for r in results if r.status == "ok"]
    error_results = [r for r in results if r.status == "error"]

    def _rate(subset: list[CaseResult]) -> dict:
        n_ok = sum(1 for r in subset if r.status == "ok")
        n = len(subset)
        return {"succeeded": n_ok, "total": n, "rate": (n_ok / n) if n else None}

    category_judged = [r for r in ok_results if r.category_correct is not None]
    severity_judged = [r for r in ok_results if r.severity_correct is not None]

    multilingual_cases = [r for r in results if r.language in ("hi", "bn")]
    imperfect_types = {"spelling_error", "incomplete_word", "transliteration", "colloquial", "mixed_language"}
    imperfect_cases = [r for r in results if r.input_type in imperfect_types]
    ambiguous_types = {"ambiguous", "insufficient_info"}
    ambiguous_cases = [r for r in results if r.input_type in ambiguous_types]

    ambiguous_ok = [r for r in ambiguous_cases if r.status == "ok"]
    ambiguous_safe = [r for r in ambiguous_ok if r.unsupported_fact_count == 0]

    total_unsupported_facts = sum(r.unsupported_fact_count for r in ok_results)
    cases_with_unsupported_facts = sum(1 for r in ok_results if r.unsupported_fact_count > 0)

    return {
        "dataset_size": total,
        "cases_by_language": _count_by(results, lambda r: r.language),
        "cases_by_input_type": _count_by(results, lambda r: r.input_type),
        "analysis_completed": len(ok_results),
        "analysis_errored": len(error_results),
        "error_examples": [
            {"id": r.id, "error": r.error_message} for r in error_results[:5]
        ],
        "category_accuracy": {
            "measured_cases": len(category_judged),
            "correct": sum(1 for r in category_judged if r.category_correct),
            "accuracy": (
                sum(1 for r in category_judged if r.category_correct) / len(category_judged)
                if category_judged
                else None
            ),
        },
        "severity_accuracy": {
            "measured_cases": len(severity_judged),
            "correct": sum(1 for r in severity_judged if r.severity_correct),
            "accuracy": (
                sum(1 for r in severity_judged if r.severity_correct) / len(severity_judged)
                if severity_judged
                else None
            ),
        },
        "multilingual_successful_analysis_rate": _rate(multilingual_cases),
        "imperfect_input_successful_analysis_rate": _rate(imperfect_cases),
        "ambiguous_input_safety_rate": {
            "safe": len(ambiguous_safe),
            "analyzed": len(ambiguous_ok),
            "total_ambiguous_cases": len(ambiguous_cases),
            "rate": (len(ambiguous_safe) / len(ambiguous_ok)) if ambiguous_ok else None,
        },
        "unsupported_fact_rate": {
            "total_unsupported_facts_found": total_unsupported_facts,
            "cases_with_at_least_one": cases_with_unsupported_facts,
            "cases_analyzed": len(ok_results),
            "rate": (cases_with_unsupported_facts / len(ok_results)) if ok_results else None,
        },
    }


def _count_by(results: list[CaseResult], key) -> dict:
    counts: dict[str, int] = {}
    for r in results:
        k = key(r)
        counts[k] = counts.get(k, 0) + 1
    return counts


def run(dataset_path: Path, output_path: Path, delay_seconds: float = DEFAULT_DELAY_SECONDS) -> dict:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    cases = dataset["cases"]

    print(f"Running {len(cases)} evaluation cases through the real analyze_complaint() pipeline...")
    if delay_seconds > 0:
        print(f"Rate-limit delay: {delay_seconds}s between cases (not before the first case).")
    results = []
    for i, case in enumerate(cases, start=1):
        if i > 1 and delay_seconds > 0:
            time.sleep(delay_seconds)
        result = evaluate_case(case)
        results.append(result)
        status_symbol = "OK" if result.status == "ok" else "ERROR"
        print(f"  [{i}/{len(cases)}] {case['id']} ({case['language']}, {case['input_type']}): {status_symbol}")

    summary = compute_summary(results)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_path": str(dataset_path),
        "dataset_version": dataset.get("dataset_version"),
        "summary": summary,
        "results": [asdict(r) for r in results],
    }
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nResults written to {output_path}")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_RESULTS_PATH)
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=(
            f"Delay in seconds between Gemini evaluation cases, to stay under the "
            f"free-tier per-minute request quota (default: {DEFAULT_DELAY_SECONDS}). "
            f"Never applied before the first case."
        ),
    )
    args = parser.parse_args()

    import os

    if not os.environ.get("GEMINI_API_KEY"):
        print(
            "GEMINI_API_KEY is not set -- this evaluation requires a real, working "
            "Gemini API key to produce genuine results. Refusing to run rather than "
            "fabricate or silently skip results. Add GEMINI_API_KEY to your .env "
            "file and try again.",
            file=sys.stderr,
        )
        sys.exit(1)

    output = run(args.dataset, args.output, delay_seconds=args.delay_seconds)
    summary = output["summary"]
    print("\n--- Summary ---")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
