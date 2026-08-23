"""
Generates a human-readable markdown report from evaluation/results.json
(Milestone 24). Purely a formatting step over already-computed,
deterministic metrics -- no new evaluation logic lives here, and no LLM
is involved in producing this report.

Usage (from the repository root, after running run_evaluation.py):

    python3 evaluation/generate_report.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_RESULTS_PATH = Path(__file__).resolve().parent / "results.json"
DEFAULT_REPORT_PATH = Path(__file__).resolve().parent / "report.md"


def _pct(rate: float | None) -> str:
    if rate is None:
        return "N/A (not measurable for this dataset)"
    return f"{rate * 100:.1f}%"


def render_report(data: dict) -> str:
    summary = data["summary"]
    results = data["results"]
    lines = []

    lines.append("# CivicSync AI Evaluation Report (Milestone 24)")
    lines.append("")
    lines.append(f"Generated: {data['generated_at']}")
    lines.append(f"Dataset: `{data['dataset_path']}` (version {data.get('dataset_version')})")
    lines.append("")
    lines.append(
        "**This is a small, hand-curated 52-case prototype dataset, not a "
        "statistically representative sample.** See `evaluation/README.md` "
        "for full limitations before citing any number below."
    )
    lines.append("")

    lines.append("## Headline safety metric: unsupported facts")
    lines.append("")
    uf = summary["unsupported_fact_rate"]
    lines.append(
        f"- **{uf['cases_with_at_least_one']} of {uf['cases_analyzed']}** successfully-analyzed "
        f"cases contained at least one detected unsupported (invented) fact."
    )
    lines.append(f"- Total unsupported facts detected across all cases: **{uf['total_unsupported_facts_found']}**")
    lines.append(f"- Rate: **{_pct(uf['rate'])}**")
    lines.append(
        "- Detection method: deterministic case-insensitive substring matching against "
        "each case's `must_not_invent` list -- no LLM judged this. See README for the "
        "known limitation of this approach (paraphrases may be missed)."
    )
    lines.append("")

    lines.append("## Pipeline robustness")
    lines.append("")
    lines.append(f"- Analysis completed without error: **{summary['analysis_completed']} / {summary['dataset_size']}**")
    lines.append(f"- Analysis errored: **{summary['analysis_errored']} / {summary['dataset_size']}**")
    if summary["error_examples"]:
        lines.append("")
        lines.append("  Example errors (up to 5 shown):")
        for ex in summary["error_examples"]:
            lines.append(f"  - `{ex['id']}`: {ex['error']}")
    lines.append("")

    ml = summary["multilingual_successful_analysis_rate"]
    lines.append(
        f"- Multilingual (Hindi/Bengali) successful-analysis rate: "
        f"**{ml['succeeded']}/{ml['total']}** ({_pct(ml['rate'])})"
    )
    imp = summary["imperfect_input_successful_analysis_rate"]
    lines.append(
        f"- Imperfect-input (spelling/incomplete/transliteration/colloquial/mixed) "
        f"successful-analysis rate: **{imp['succeeded']}/{imp['total']}** ({_pct(imp['rate'])})"
    )
    lines.append("")

    lines.append("## Ambiguous / insufficient-information safety")
    lines.append("")
    amb = summary["ambiguous_input_safety_rate"]
    lines.append(
        f"- Of {amb['analyzed']} successfully-analyzed ambiguous/insufficient-information "
        f"cases (out of {amb['total_ambiguous_cases']} total in this category), "
        f"**{amb['safe']}** introduced zero unsupported facts."
    )
    lines.append(f"- Rate: **{_pct(amb['rate'])}**")
    lines.append("")

    lines.append("## Category / severity accuracy (only where a specific expectation was defined)")
    lines.append("")
    cat = summary["category_accuracy"]
    sev = summary["severity_accuracy"]
    lines.append(
        f"- Category: **{cat['correct']}/{cat['measured_cases']}** correct "
        f"({_pct(cat['accuracy'])}) -- measured only over cases with an `expected_category` set."
    )
    lines.append(
        f"- Severity: **{sev['correct']}/{sev['measured_cases']}** correct "
        f"({_pct(sev['accuracy'])}) -- measured only over cases with an `expected_severity` set. "
        f"This dataset deliberately sets `expected_severity` for very few cases (severity is "
        f"inherently more subjective than category), so this number is low-confidence and "
        f"should not be over-interpreted."
    )
    lines.append("")

    lines.append("## Dataset composition")
    lines.append("")
    lines.append("| Language | Count |")
    lines.append("|---|---|")
    for lang, count in sorted(summary["cases_by_language"].items()):
        lines.append(f"| {lang} | {count} |")
    lines.append("")
    lines.append("| Input type | Count |")
    lines.append("|---|---|")
    for input_type, count in sorted(summary["cases_by_input_type"].items()):
        lines.append(f"| {input_type} | {count} |")
    lines.append("")

    flagged = [r for r in results if r["status"] == "ok" and r["unsupported_fact_count"] > 0]
    if flagged:
        lines.append("## Cases with detected unsupported facts")
        lines.append("")
        lines.append("| ID | Language | Input type | Unsupported facts found |")
        lines.append("|---|---|---|---|")
        for r in flagged:
            facts = ", ".join(r["unsupported_facts_found"])
            lines.append(f"| {r['id']} | {r['language']} | {r['input_type']} | {facts} |")
        lines.append("")

    errored = [r for r in results if r["status"] == "error"]
    if errored:
        lines.append("## Cases that errored")
        lines.append("")
        lines.append("| ID | Language | Input type | Error |")
        lines.append("|---|---|---|---|")
        for r in errored:
            lines.append(f"| {r['id']} | {r['language']} | {r['input_type']} | {r['error_message']} |")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT_PATH)
    args = parser.parse_args()

    if not args.results.exists():
        print(
            f"No results file found at {args.results} -- run run_evaluation.py first.",
            file=sys.stderr,
        )
        sys.exit(1)

    data = json.loads(args.results.read_text(encoding="utf-8"))
    report = render_report(data)
    args.output.write_text(report, encoding="utf-8")
    print(f"Report written to {args.output}")


if __name__ == "__main__":
    main()
