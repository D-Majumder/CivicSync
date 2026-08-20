"""Unit tests for ai/schemas.py. No network/API key required."""

import pytest
from pydantic import ValidationError

from ai.schemas import CivicIssue, GeminiCivicIssueSchema, IssueCategory, SeverityLevel


def _minimal_kwargs(**overrides):
    base = dict(
        original_text="There has been no street light working near our school for two weeks.",
        category=IssueCategory.STREET_LIGHTING,
        problem="Non-functional street light near a school.",
        confidence=0.8,
    )
    base.update(overrides)
    return base


def test_minimal_valid_issue_uses_defaults():
    issue = CivicIssue(**_minimal_kwargs())
    assert issue.category == IssueCategory.STREET_LIGHTING
    assert issue.severity == SeverityLevel.UNKNOWN
    assert issue.location is None
    assert issue.duration is None
    assert issue.affected_population is None
    assert issue.suggested_department is None


def test_full_valid_issue_round_trips():
    issue = CivicIssue(
        **_minimal_kwargs(
            location="Near Lincoln Elementary School",
            duration="Approximately two weeks",
            severity=SeverityLevel.MEDIUM,
            affected_population="Students and pedestrians",
            suggested_department="Municipal Electrical Department",
        )
    )
    data = issue.model_dump()
    assert data["severity"] == SeverityLevel.MEDIUM
    assert data["location"] == "Near Lincoln Elementary School"

    # Round-trip through JSON to confirm serialization is stable.
    restored = CivicIssue.model_validate_json(issue.model_dump_json())
    assert restored == issue


@pytest.mark.parametrize("bad_confidence", [-0.1, 1.1, 5])
def test_confidence_out_of_range_rejected(bad_confidence):
    with pytest.raises(ValidationError):
        CivicIssue(**_minimal_kwargs(confidence=bad_confidence))


def test_invalid_category_rejected():
    with pytest.raises(ValidationError):
        CivicIssue(**_minimal_kwargs(category="Not A Real Category"))


def test_empty_problem_rejected():
    with pytest.raises(ValidationError):
        CivicIssue(**_minimal_kwargs(problem=""))


def test_empty_original_text_rejected():
    with pytest.raises(ValidationError):
        CivicIssue(**_minimal_kwargs(original_text=""))


def test_extra_fields_rejected():
    """Schema forbids unexpected fields to keep downstream data strict."""
    with pytest.raises(ValidationError):
        CivicIssue(**_minimal_kwargs(unexpected_field="oops"))


def test_missing_required_field_rejected():
    kwargs = _minimal_kwargs()
    del kwargs["problem"]
    with pytest.raises(ValidationError):
        CivicIssue(**kwargs)


# --- GeminiCivicIssueSchema: the Gemini-facing schema variant -------------


def test_civic_issue_json_schema_forbids_additional_properties():
    """Regression guard: CivicIssue itself must stay strict locally."""
    schema = CivicIssue.model_json_schema()
    assert schema.get("additionalProperties") is False


def test_gemini_schema_json_has_no_additional_properties_keyword():
    """This is the actual fix: Gemini rejects the additionalProperties
    keyword in response_schema, so the schema variant sent to Gemini must
    not emit it anywhere in the payload."""
    schema = GeminiCivicIssueSchema.model_json_schema()

    def assert_no_additional_properties(node):
        if isinstance(node, dict):
            assert "additionalProperties" not in node
            for value in node.values():
                assert_no_additional_properties(value)
        elif isinstance(node, list):
            for item in node:
                assert_no_additional_properties(item)

    assert_no_additional_properties(schema)


def test_gemini_schema_still_enforces_field_validation():
    """Relaxing `extra` must not weaken the other validation rules."""
    with pytest.raises(ValidationError):
        GeminiCivicIssueSchema(**_minimal_kwargs(confidence=2.0))

    with pytest.raises(ValidationError):
        GeminiCivicIssueSchema(**_minimal_kwargs(category="Not A Real Category"))


def test_gemini_schema_instance_revalidates_cleanly_into_civic_issue():
    """Simulates ai/client.py's re-validation step: a GeminiCivicIssueSchema
    instance (what the SDK's `.parsed` would return) must convert cleanly
    into a strict CivicIssue with identical data."""
    gemini_instance = GeminiCivicIssueSchema(**_minimal_kwargs(location="Downtown"))
    strict_instance = CivicIssue.model_validate(gemini_instance.model_dump())

    assert type(strict_instance) is CivicIssue
    assert strict_instance.model_dump() == gemini_instance.model_dump()


def test_gemini_schema_ignores_unexpected_fields_instead_of_rejecting():
    """Unlike CivicIssue (extra='forbid'), the Gemini-facing variant must
    not error out on an unexpected field, since its only job is shaping
    the outbound request/response -- CivicIssue re-validation is what
    enforces strictness afterward."""
    instance = GeminiCivicIssueSchema(**_minimal_kwargs(), unexpected_field="oops")
    assert "unexpected_field" not in instance.model_dump()
