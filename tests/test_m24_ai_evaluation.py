"""
Milestone 24: AI evaluation harness tests.

Exercises evaluation/run_evaluation.py's and evaluation/generate_report.py's
logic with Gemini MOCKED (matching this project's established test
pattern, e.g. tests/test_client.py) -- these tests verify the harness's
own deterministic evaluation logic (unsupported-fact detection, rate
computation, error handling), never a real Gemini call, and never touch
civicsync.db.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel

RUN_EVAL_PATH = Path(__file__).resolve().parent.parent / "evaluation" / "run_evaluation.py"
GENERATE_REPORT_PATH = Path(__file__).resolve().parent.parent / "evaluation" / "generate_report.py"
DATASET_PATH = Path(__file__).resolve().parent.parent / "evaluation" / "dataset.json"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def run_eval(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-testing")
    return _load_module(RUN_EVAL_PATH, "run_evaluation")


@pytest.fixture
def generate_report():
    return _load_module(GENERATE_REPORT_PATH, "generate_report")


def _civic_issue(**overrides) -> CivicIssue:
    base = dict(
        original_text="test",
        category=IssueCategory.ROADS_AND_POTHOLES,
        problem="A pothole on the road.",
        location=None,
        duration=None,
        affected_population=None,
        severity=SeverityLevel.HIGH,
        confidence=0.8,
    )
    base.update(overrides)
    return CivicIssue(**base)


# ============================================================================
# Dataset validity
# ============================================================================


def test_dataset_is_valid_json_with_required_fields():
    data = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    cases = data["cases"]
    assert len(cases) >= 40  # "around 50" per the milestone requirement
    for case in cases:
        assert case["id"]
        assert case["language"] in ("en", "hi", "bn")
        assert case["input_type"]
        assert case["input"]
        assert "must_not_invent" in case
        assert "supported_facts" in case


def test_dataset_ids_are_unique():
    data = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    ids = [c["id"] for c in data["cases"]]
    assert len(ids) == len(set(ids))


def test_dataset_covers_all_three_languages():
    data = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    languages = {c["language"] for c in data["cases"]}
    assert languages == {"en", "hi", "bn"}


def test_dataset_covers_ambiguous_and_insufficient_info_cases():
    data = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    types = {c["input_type"] for c in data["cases"]}
    assert "ambiguous" in types
    assert "insufficient_info" in types


def test_ambiguous_cases_have_no_forced_expected_category():
    """Forcing a category on a genuinely ambiguous case would itself be
    a form of fabrication -- the dataset must not do this."""
    data = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    for case in data["cases"]:
        if case["input_type"] in ("ambiguous", "insufficient_info"):
            assert case["expected_category"] is None
            assert case["expected_severity"] is None


# ============================================================================
# Deterministic unsupported-fact detection
# ============================================================================


def test_text_contains_any_detects_forbidden_fact(run_eval):
    found = run_eval._text_contains_any(["A large pothole near the water main."], ["water main"])
    assert found == ["water main"]


def test_text_contains_any_case_insensitive(run_eval):
    found = run_eval._text_contains_any(["A LARGE POTHOLE"], ["large pothole"])
    assert found == ["large pothole"]


def test_text_contains_any_returns_empty_when_nothing_forbidden_present(run_eval):
    found = run_eval._text_contains_any(["A small pothole on the road."], ["water main", "streetlight"])
    assert found == []


def test_text_contains_any_ignores_none_fields(run_eval):
    found = run_eval._text_contains_any([None, "pothole", None], ["pothole"])
    assert found == ["pothole"]


# ============================================================================
# evaluate_case
# ============================================================================


def test_evaluate_case_detects_no_unsupported_facts_when_output_is_clean(run_eval):
    case = {
        "id": "T1", "language": "en", "input_type": "normal", "input": "text",
        "must_not_invent": ["streetlight", "flooding"],
    }
    with patch("run_evaluation.analyze_complaint", return_value=_civic_issue(problem="A pothole on the road.")):
        result = run_eval.evaluate_case(case)
    assert result.status == "ok"
    assert result.unsupported_fact_count == 0
    assert result.unsupported_facts_found == []


def test_evaluate_case_detects_unsupported_fact_when_present(run_eval):
    case = {
        "id": "T2", "language": "en", "input_type": "ambiguous", "input": "text",
        "must_not_invent": ["water main", "flooding"],
    }
    with patch(
        "run_evaluation.analyze_complaint",
        return_value=_civic_issue(problem="A burst water main causing flooding."),
    ):
        result = run_eval.evaluate_case(case)
    assert result.status == "ok"
    assert result.unsupported_fact_count == 2
    assert set(result.unsupported_facts_found) == {"water main", "flooding"}


def test_evaluate_case_records_category_correct_when_expected_set(run_eval):
    case = {
        "id": "T3", "language": "en", "input_type": "normal", "input": "text",
        "must_not_invent": [], "expected_category": "Roads and Potholes",
    }
    with patch("run_evaluation.analyze_complaint", return_value=_civic_issue(category=IssueCategory.ROADS_AND_POTHOLES)):
        result = run_eval.evaluate_case(case)
    assert result.category_correct is True


def test_evaluate_case_records_category_incorrect(run_eval):
    case = {
        "id": "T4", "language": "en", "input_type": "normal", "input": "text",
        "must_not_invent": [], "expected_category": "Roads and Potholes",
    }
    with patch("run_evaluation.analyze_complaint", return_value=_civic_issue(category=IssueCategory.WATER_SUPPLY)):
        result = run_eval.evaluate_case(case)
    assert result.category_correct is False


def test_evaluate_case_category_correct_is_none_when_not_expected(run_eval):
    """No fabricated pass/fail judgment when the dataset itself made no claim."""
    case = {"id": "T5", "language": "en", "input_type": "ambiguous", "input": "text", "must_not_invent": []}
    with patch("run_evaluation.analyze_complaint", return_value=_civic_issue()):
        result = run_eval.evaluate_case(case)
    assert result.category_correct is None
    assert result.severity_correct is None


def test_evaluate_case_handles_gemini_error_gracefully(run_eval):
    """A real Gemini/API failure must be recorded honestly, never
    silently skipped or fabricated as a successful result."""
    case = {"id": "T6", "language": "en", "input_type": "normal", "input": "text", "must_not_invent": []}
    with patch("run_evaluation.analyze_complaint", side_effect=ValueError("Gemini returned an empty response.")):
        result = run_eval.evaluate_case(case)
    assert result.status == "error"
    assert "ValueError" in result.error_message
    assert result.category is None
    assert result.unsupported_fact_count == 0


def test_evaluate_case_handles_environment_error(run_eval):
    case = {"id": "T7", "language": "en", "input_type": "normal", "input": "text", "must_not_invent": []}
    with patch("run_evaluation.analyze_complaint", side_effect=EnvironmentError("GEMINI_API_KEY is not set.")):
        result = run_eval.evaluate_case(case)
    assert result.status == "error"
    # EnvironmentError is a builtin alias for OSError in Python -- both
    # names refer to the exact same class, so type(exc).__name__ always
    # reports "OSError".
    assert "OSError" in result.error_message
    assert "GEMINI_API_KEY" in result.error_message


# ============================================================================
# compute_summary
# ============================================================================


def test_compute_summary_unmeasurable_rate_is_none_not_zero(run_eval):
    """An empty denominator must report None (not measurable), never a
    fabricated 0% or 100%."""
    results = []
    summary = run_eval.compute_summary(results)
    assert summary["category_accuracy"]["accuracy"] is None
    assert summary["unsupported_fact_rate"]["rate"] is None


def test_compute_summary_unsupported_fact_rate(run_eval):
    case_clean = {"id": "A", "language": "en", "input_type": "normal", "input": "x", "must_not_invent": []}
    case_bad = {"id": "B", "language": "en", "input_type": "normal", "input": "x", "must_not_invent": ["flood"]}
    with patch("run_evaluation.analyze_complaint", return_value=_civic_issue(problem="clean text")):
        r1 = run_eval.evaluate_case(case_clean)
    with patch("run_evaluation.analyze_complaint", return_value=_civic_issue(problem="a flood occurred")):
        r2 = run_eval.evaluate_case(case_bad)

    summary = run_eval.compute_summary([r1, r2])
    assert summary["unsupported_fact_rate"]["cases_with_at_least_one"] == 1
    assert summary["unsupported_fact_rate"]["cases_analyzed"] == 2
    assert summary["unsupported_fact_rate"]["rate"] == 0.5


def test_compute_summary_category_accuracy_only_counts_measured_cases(run_eval):
    measured = {
        "id": "A", "language": "en", "input_type": "normal", "input": "x",
        "must_not_invent": [], "expected_category": "Roads and Potholes",
    }
    unmeasured = {"id": "B", "language": "en", "input_type": "ambiguous", "input": "x", "must_not_invent": []}
    with patch("run_evaluation.analyze_complaint", return_value=_civic_issue(category=IssueCategory.ROADS_AND_POTHOLES)):
        r1 = run_eval.evaluate_case(measured)
    with patch("run_evaluation.analyze_complaint", return_value=_civic_issue()):
        r2 = run_eval.evaluate_case(unmeasured)

    summary = run_eval.compute_summary([r1, r2])
    assert summary["category_accuracy"]["measured_cases"] == 1
    assert summary["category_accuracy"]["accuracy"] == 1.0


def test_compute_summary_ambiguous_safety_rate(run_eval):
    safe_case = {"id": "A", "language": "en", "input_type": "ambiguous", "input": "x", "must_not_invent": ["pothole"]}
    unsafe_case = {"id": "B", "language": "en", "input_type": "ambiguous", "input": "x", "must_not_invent": ["pothole"]}
    with patch("run_evaluation.analyze_complaint", return_value=_civic_issue(problem="unclear issue")):
        r1 = run_eval.evaluate_case(safe_case)
    with patch("run_evaluation.analyze_complaint", return_value=_civic_issue(problem="a pothole was found")):
        r2 = run_eval.evaluate_case(unsafe_case)

    summary = run_eval.compute_summary([r1, r2])
    assert summary["ambiguous_input_safety_rate"]["safe"] == 1
    assert summary["ambiguous_input_safety_rate"]["analyzed"] == 2
    assert summary["ambiguous_input_safety_rate"]["rate"] == 0.5


# ============================================================================
# Full run() pipeline (mocked) -- never touches civicsync.db
# ============================================================================


def test_run_pipeline_end_to_end_with_mocked_gemini(run_eval, tmp_path):
    tmp_dataset = tmp_path / "tiny_dataset.json"
    tmp_dataset.write_text(json.dumps({
        "dataset_version": "test",
        "cases": [
            {"id": "X1", "language": "en", "input_type": "normal", "input": "pothole on road",
             "must_not_invent": [], "expected_category": "Roads and Potholes"},
        ],
    }))
    tmp_output = tmp_path / "results.json"

    with patch("run_evaluation.analyze_complaint", return_value=_civic_issue(category=IssueCategory.ROADS_AND_POTHOLES)):
        output = run_eval.run(tmp_dataset, tmp_output)

    assert tmp_output.exists()
    assert output["summary"]["analysis_completed"] == 1
    saved = json.loads(tmp_output.read_text(encoding="utf-8"))
    assert saved["summary"]["dataset_size"] == 1


def _tiny_three_case_dataset(tmp_path):
    tmp_dataset = tmp_path / "three_case_dataset.json"
    tmp_dataset.write_text(json.dumps({
        "dataset_version": "test",
        "cases": [
            {"id": "D1", "language": "en", "input_type": "normal", "input": "a", "must_not_invent": []},
            {"id": "D2", "language": "en", "input_type": "normal", "input": "b", "must_not_invent": []},
            {"id": "D3", "language": "en", "input_type": "normal", "input": "c", "must_not_invent": []},
        ],
    }), encoding="utf-8")
    return tmp_dataset


def test_default_delay_seconds_constant(run_eval):
    assert run_eval.DEFAULT_DELAY_SECONDS == 15.0


def test_run_defaults_to_15_second_delay_between_cases(run_eval, tmp_path):
    """No delay_seconds argument passed -- run() must fall back to
    DEFAULT_DELAY_SECONDS (15s), not 0 or some other value."""
    tmp_dataset = _tiny_three_case_dataset(tmp_path)
    tmp_output = tmp_path / "results.json"

    with patch("run_evaluation.analyze_complaint", return_value=_civic_issue()), \
         patch("run_evaluation.time.sleep") as mock_sleep:
        run_eval.run(tmp_dataset, tmp_output)

    # 3 cases -> exactly 2 delays (never before the first case).
    assert mock_sleep.call_count == 2
    for call in mock_sleep.call_args_list:
        assert call.args[0] == 15.0


def test_no_delay_before_the_first_case(run_eval, tmp_path):
    tmp_dataset = _tiny_three_case_dataset(tmp_path)
    tmp_output = tmp_path / "results.json"

    call_order = []

    def _record_sleep(seconds):
        call_order.append("sleep")

    original_evaluate_case = run_eval.evaluate_case

    def _record_evaluate(case):
        call_order.append("evaluate")
        return original_evaluate_case(case)

    with patch("run_evaluation.analyze_complaint", return_value=_civic_issue()), \
         patch("run_evaluation.time.sleep", side_effect=_record_sleep), \
         patch("run_evaluation.evaluate_case", side_effect=_record_evaluate):
        run_eval.run(tmp_dataset, tmp_output, delay_seconds=5.0)

    # First action must be "evaluate" (case 1), never "sleep" first.
    assert call_order[0] == "evaluate"
    assert call_order == ["evaluate", "sleep", "evaluate", "sleep", "evaluate"]


def test_run_respects_custom_delay_seconds(run_eval, tmp_path):
    tmp_dataset = _tiny_three_case_dataset(tmp_path)
    tmp_output = tmp_path / "results.json"

    with patch("run_evaluation.analyze_complaint", return_value=_civic_issue()), \
         patch("run_evaluation.time.sleep") as mock_sleep:
        run_eval.run(tmp_dataset, tmp_output, delay_seconds=3.5)

    assert mock_sleep.call_count == 2
    for call in mock_sleep.call_args_list:
        assert call.args[0] == 3.5


def test_zero_delay_seconds_skips_sleep_entirely(run_eval, tmp_path):
    tmp_dataset = _tiny_three_case_dataset(tmp_path)
    tmp_output = tmp_path / "results.json"

    with patch("run_evaluation.analyze_complaint", return_value=_civic_issue()), \
         patch("run_evaluation.time.sleep") as mock_sleep:
        run_eval.run(tmp_dataset, tmp_output, delay_seconds=0)

    mock_sleep.assert_not_called()


def test_single_case_dataset_never_sleeps(run_eval, tmp_path):
    """Only one case -- there is no "between cases" gap at all, so
    time.sleep must never be called regardless of delay_seconds."""
    tmp_dataset = tmp_path / "one_case_dataset.json"
    tmp_dataset.write_text(json.dumps({
        "dataset_version": "test",
        "cases": [{"id": "S1", "language": "en", "input_type": "normal", "input": "x", "must_not_invent": []}],
    }), encoding="utf-8")
    tmp_output = tmp_path / "results.json"

    with patch("run_evaluation.analyze_complaint", return_value=_civic_issue()), \
         patch("run_evaluation.time.sleep") as mock_sleep:
        run_eval.run(tmp_dataset, tmp_output, delay_seconds=15.0)

    mock_sleep.assert_not_called()


def test_cli_delay_seconds_flag_is_parsed_and_passed_through(run_eval, tmp_path, monkeypatch):
    """--delay-seconds must actually reach run(), not just exist as an
    unused argparse option."""
    tmp_dataset = _tiny_three_case_dataset(tmp_path)
    tmp_output = tmp_path / "results.json"

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-testing")
    monkeypatch.setattr(
        sys, "argv",
        ["run_evaluation.py", "--dataset", str(tmp_dataset), "--output", str(tmp_output), "--delay-seconds", "7"],
    )

    with patch("run_evaluation.analyze_complaint", return_value=_civic_issue()), \
         patch("run_evaluation.time.sleep") as mock_sleep:
        run_eval.main()

    assert mock_sleep.call_count == 2
    for call in mock_sleep.call_args_list:
        assert call.args[0] == 7.0


def test_cli_delay_seconds_defaults_when_flag_omitted(run_eval, tmp_path, monkeypatch):
    tmp_dataset = _tiny_three_case_dataset(tmp_path)
    tmp_output = tmp_path / "results.json"

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-testing")
    monkeypatch.setattr(
        sys, "argv",
        ["run_evaluation.py", "--dataset", str(tmp_dataset), "--output", str(tmp_output)],
    )

    with patch("run_evaluation.analyze_complaint", return_value=_civic_issue()), \
         patch("run_evaluation.time.sleep") as mock_sleep:
        run_eval.main()

    for call in mock_sleep.call_args_list:
        assert call.args[0] == 15.0


def test_run_evaluation_never_imports_persistence_layer():
    """The harness must never touch civicsync.db -- confirmed by static
    inspection that no actual `import`/`from ... import` statement pulls
    in backend.repository or any persistence function. (The module's own
    docstring mentions backend.repository.get_default_jurisdiction_id as
    a prose cross-reference for context -- that's documentation, not an
    import, so this check inspects import lines specifically rather than
    the whole file's text.)"""
    import_lines = [
        line for line in RUN_EVAL_PATH.read_text().splitlines()
        if line.strip().startswith("import ") or line.strip().startswith("from ")
    ]
    joined_imports = "\n".join(import_lines)
    assert "backend.repository" not in joined_imports
    assert "backend.database" not in joined_imports
    assert "create_issue_from_civic_issue" not in joined_imports


def test_main_fails_loudly_without_api_key(monkeypatch, capsys):
    # Load the module FIRST, then remove the key -- this is what makes
    # the precondition deterministic regardless of whether an earlier
    # test in this session already triggered run_evaluation.py's
    # (guarded, load-once) load_dotenv() call: either way, nothing after
    # this point calls load_dotenv() again, so the deletion below is the
    # final word on GEMINI_API_KEY's presence by the time main() runs.
    module = _load_module(RUN_EVAL_PATH, "run_evaluation_no_key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_evaluation.py"])
    with pytest.raises(SystemExit) as exc_info:
        module.main()
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "GEMINI_API_KEY is not set" in captured.err


# ============================================================================
# generate_report.py
# ============================================================================


def test_generate_report_renders_without_crashing(run_eval, generate_report, tmp_path):
    tmp_dataset = tmp_path / "tiny_dataset.json"
    tmp_dataset.write_text(json.dumps({
        "dataset_version": "test",
        "cases": [
            {"id": "X1", "language": "en", "input_type": "ambiguous", "input": "vague",
             "must_not_invent": ["pothole"]},
        ],
    }))
    tmp_output = tmp_path / "results.json"
    with patch("run_evaluation.analyze_complaint", return_value=_civic_issue(problem="a pothole was seen")):
        output = run_eval.run(tmp_dataset, tmp_output)

    report_text = generate_report.render_report(output)
    assert "CivicSync AI Evaluation Report" in report_text
    assert "unsupported" in report_text.lower()
    assert "X1" in report_text  # flagged in the unsupported-facts table


def test_generate_report_handles_missing_results_file(generate_report, tmp_path, monkeypatch, capsys):
    missing = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(sys, "argv", ["generate_report.py", "--results", str(missing)])
    with pytest.raises(SystemExit) as exc_info:
        generate_report.main()
    assert exc_info.value.code == 1
