"""
Tests for ai/client.py.

Unit tests below mock the Gemini SDK entirely, so they run with no network
access and no API key. A single integration test at the bottom makes a
real call to Gemini and is skipped by default -- it only runs when
RUN_GEMINI_INTEGRATION_TESTS=1 is set in the environment (in addition to a
real GEMINI_API_KEY), so it never runs accidentally in CI.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from dotenv import load_dotenv
from google.genai import errors as genai_errors

import ai.client as client_module
from ai.schemas import CivicIssue, GeminiCivicIssueSchema, IssueCategory, IssueExplanationOutput

# Test-only: the real skipif decorator below reads os.environ at *collection*
# time (when this module is imported), which is earlier than any app code
# (e.g. backend/main.py) would normally call load_dotenv(). Load .env here so
# RUN_GEMINI_INTEGRATION_TESTS and GEMINI_API_KEY are already in os.environ
# by the time that decorator is evaluated. This does not change app behavior;
# it only affects how this test module resolves its own skip condition.
load_dotenv()


@pytest.fixture(autouse=True)
def reset_cached_client():
    """Ensure the module-level client caches don't leak between tests."""
    client_module._client = None
    client_module._backup_client = None
    yield
    client_module._client = None
    client_module._backup_client = None


def test_missing_api_key_raises_clear_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(EnvironmentError, match="GEMINI_API_KEY"):
        client_module._get_client()


def test_analyze_complaint_rejects_blank_text(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-testing")
    with pytest.raises(ValueError):
        client_module.analyze_complaint("   ")


def test_analyze_complaint_uses_parsed_response(monkeypatch):
    """When the SDK successfully parses a GeminiCivicIssueSchema instance,
    re-validate it through the strict CivicIssue model and return that."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-testing")

    expected_issue = CivicIssue(
        original_text="No street light near our school for two weeks.",
        category=IssueCategory.STREET_LIGHTING,
        problem="Non-functional street light near a school.",
        confidence=0.75,
    )

    # The SDK parses into the schema class we actually sent as
    # response_schema -- GeminiCivicIssueSchema, not CivicIssue itself.
    mock_response = MagicMock()
    mock_response.parsed = GeminiCivicIssueSchema(**expected_issue.model_dump())
    mock_response.text = expected_issue.model_dump_json()

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    with patch("ai.client.genai.Client", return_value=mock_client):
        result = client_module.analyze_complaint(
            "No street light near our school for two weeks."
        )

    assert result == expected_issue
    assert type(result) is CivicIssue
    mock_client.models.generate_content.assert_called_once()
    _, kwargs = mock_client.models.generate_content.call_args
    assert kwargs["model"] == client_module.GEMINI_MODEL
    assert kwargs["config"].response_schema is GeminiCivicIssueSchema


def test_analyze_complaint_always_preserves_real_original_text(monkeypatch):
    """Milestone 22 (critical safety fix): original_text in the returned
    CivicIssue must always be exactly the real citizen_text argument,
    NEVER whatever Gemini itself echoed back in its structured response
    -- even if Gemini's echo subtly differs (a real risk for non-Latin
    scripts, where an LLM asked to reproduce text is more likely to
    introduce drift than for plain English). This test deliberately
    mocks Gemini returning a DIFFERENT original_text than the real
    input, and confirms the backend overwrites it."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-testing")

    real_citizen_text = "सड़क पर गड्ढा है, बहुत खतरनाक।"
    gemini_echoed_text = "सड़क पर गड्ढे है, बहुत खतरनाक"  # subtly different from the real input

    gemini_response_issue = CivicIssue(
        original_text=gemini_echoed_text,
        category=IssueCategory.ROADS_AND_POTHOLES,
        problem="Dangerous pothole on the road.",
        confidence=0.8,
    )
    mock_response = MagicMock()
    mock_response.parsed = GeminiCivicIssueSchema(**gemini_response_issue.model_dump())
    mock_response.text = gemini_response_issue.model_dump_json()

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    with patch("ai.client.genai.Client", return_value=mock_client):
        result = client_module.analyze_complaint(real_citizen_text)

    assert result.original_text == real_citizen_text
    assert result.original_text != gemini_echoed_text


def test_analyze_complaint_falls_back_to_json_text(monkeypatch):
    """If `.parsed` isn't a CivicIssue, fall back to validating raw JSON text."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-testing")

    expected_issue = CivicIssue(
        original_text="Garbage has not been collected on our street in ten days.",
        category=IssueCategory.SANITATION_AND_WASTE,
        problem="Garbage collection has not occurred.",
        confidence=0.6,
    )

    mock_response = MagicMock()
    mock_response.parsed = None
    mock_response.text = expected_issue.model_dump_json()

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    with patch("ai.client.genai.Client", return_value=mock_client):
        result = client_module.analyze_complaint(
            "Garbage has not been collected on our street in ten days."
        )

    assert result == expected_issue


def test_analyze_complaint_raises_on_empty_response(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-testing")

    mock_response = MagicMock()
    mock_response.parsed = None
    mock_response.text = ""

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    with patch("ai.client.genai.Client", return_value=mock_client):
        with pytest.raises(ValueError, match="empty response"):
            client_module.analyze_complaint("Some complaint text.")


@pytest.mark.skipif(
    os.environ.get("RUN_GEMINI_INTEGRATION_TESTS") != "1"
    or not os.environ.get("GEMINI_API_KEY"),
    reason=(
        "Integration test requires a real GEMINI_API_KEY and "
        "RUN_GEMINI_INTEGRATION_TESTS=1 to be set explicitly."
    ),
)
def test_analyze_complaint_real_api_call():
    """Real call to Gemini. Skipped unless explicitly opted into."""
    issue = client_module.analyze_complaint(
        "There has been no street light working near our school for "
        "the last two weeks."
    )
    assert isinstance(issue, CivicIssue)
    assert 0.0 <= issue.confidence <= 1.0


# ============================================================================
# Milestone 25.7: primary/backup key failover
# ============================================================================
#
# All tests below mock the Gemini SDK entirely -- zero real API calls,
# same as every other test in this file. Each test constructs
# google.genai.errors.ClientError/ServerError instances directly (the
# same exception types the real SDK raises) rather than a generic
# Exception, so the retryable/non-retryable classification is exercised
# against the real error types, not a stand-in.


def _rate_limit_error():
    """A retryable 429 failure, as the real SDK raises it."""
    return genai_errors.ClientError(429, {"message": "rate limited", "status": "RESOURCE_EXHAUSTED"})


def _server_error():
    """A retryable 503 failure, as the real SDK raises it."""
    return genai_errors.ServerError(503, {"message": "temporarily unavailable", "status": "UNAVAILABLE"})


def _invalid_request_error():
    """A non-retryable 400 failure, as the real SDK raises it."""
    return genai_errors.ClientError(400, {"message": "invalid request", "status": "INVALID_ARGUMENT"})


def _mock_response_for(expected_issue: CivicIssue) -> MagicMock:
    mock_response = MagicMock()
    mock_response.parsed = GeminiCivicIssueSchema(**expected_issue.model_dump())
    mock_response.text = expected_issue.model_dump_json()
    return mock_response


def test_primary_success_never_touches_backup(monkeypatch):
    """When the primary key succeeds, the backup client is never even
    constructed -- confirms the happy path makes exactly one call and
    never references GEMINI_API_KEY_BACKUP at all."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-primary-key")
    monkeypatch.setenv("GEMINI_API_KEY_BACKUP", "fake-backup-key")

    expected_issue = CivicIssue(
        original_text="Pothole on the road.",
        category=IssueCategory.ROADS_AND_POTHOLES,
        problem="A pothole.",
        confidence=0.8,
    )
    mock_primary = MagicMock()
    mock_primary.models.generate_content.return_value = _mock_response_for(expected_issue)
    mock_backup = MagicMock()

    with patch("ai.client.genai.Client", side_effect=[mock_primary, mock_backup]):
        result = client_module.analyze_complaint("Pothole on the road.")

    assert result.category == IssueCategory.ROADS_AND_POTHOLES
    mock_primary.models.generate_content.assert_called_once()
    mock_backup.models.generate_content.assert_not_called()
    assert client_module._backup_client is None


def test_retryable_primary_failure_falls_over_to_backup_success(monkeypatch):
    """Primary raises a retryable 429; backup is configured and succeeds.
    Exactly 2 attempts total, and the backup's result is returned."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-primary-key")
    monkeypatch.setenv("GEMINI_API_KEY_BACKUP", "fake-backup-key")

    expected_issue = CivicIssue(
        original_text="Water main burst.",
        category=IssueCategory.WATER_SUPPLY,
        problem="Burst water main.",
        confidence=0.85,
    )
    mock_primary = MagicMock()
    mock_primary.models.generate_content.side_effect = _rate_limit_error()
    mock_backup = MagicMock()
    mock_backup.models.generate_content.return_value = _mock_response_for(expected_issue)

    with patch("ai.client.genai.Client", side_effect=[mock_primary, mock_backup]):
        result = client_module.analyze_complaint("Water main burst.")

    assert result.category == IssueCategory.WATER_SUPPLY
    mock_primary.models.generate_content.assert_called_once()
    mock_backup.models.generate_content.assert_called_once()


def test_server_error_also_triggers_failover(monkeypatch):
    """A 503 ServerError (not just 429) is also treated as retryable."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-primary-key")
    monkeypatch.setenv("GEMINI_API_KEY_BACKUP", "fake-backup-key")

    expected_issue = CivicIssue(
        original_text="Streetlight out.", category=IssueCategory.STREET_LIGHTING,
        problem="Streetlight not working.", confidence=0.7,
    )
    mock_primary = MagicMock()
    mock_primary.models.generate_content.side_effect = _server_error()
    mock_backup = MagicMock()
    mock_backup.models.generate_content.return_value = _mock_response_for(expected_issue)

    with patch("ai.client.genai.Client", side_effect=[mock_primary, mock_backup]):
        result = client_module.analyze_complaint("Streetlight out.")

    assert result.category == IssueCategory.STREET_LIGHTING
    mock_backup.models.generate_content.assert_called_once()


def test_both_keys_failing_raises_the_backup_error(monkeypatch):
    """Primary fails retryably, backup is configured but also fails --
    the backup's own exception propagates (at most 2 attempts, no
    infinite/further retry)."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-primary-key")
    monkeypatch.setenv("GEMINI_API_KEY_BACKUP", "fake-backup-key")

    mock_primary = MagicMock()
    mock_primary.models.generate_content.side_effect = _rate_limit_error()
    mock_backup = MagicMock()
    backup_error = _server_error()
    mock_backup.models.generate_content.side_effect = backup_error

    with patch("ai.client.genai.Client", side_effect=[mock_primary, mock_backup]):
        with pytest.raises(genai_errors.ServerError):
            client_module.analyze_complaint("Some complaint.")

    mock_primary.models.generate_content.assert_called_once()
    mock_backup.models.generate_content.assert_called_once()


def test_no_backup_configured_preserves_single_key_behavior(monkeypatch):
    """GEMINI_API_KEY_BACKUP is not set at all -- a retryable primary
    failure must propagate exactly as it did before this feature
    existed, with no attempt to construct a second client."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-primary-key")
    monkeypatch.delenv("GEMINI_API_KEY_BACKUP", raising=False)

    mock_primary = MagicMock()
    mock_primary.models.generate_content.side_effect = _rate_limit_error()

    with patch("ai.client.genai.Client", return_value=mock_primary) as mock_ctor:
        with pytest.raises(genai_errors.ClientError):
            client_module.analyze_complaint("Some complaint.")

    # Only the primary client was ever constructed.
    mock_ctor.assert_called_once()
    assert client_module._backup_client is None


def test_non_retryable_failure_never_invokes_backup(monkeypatch):
    """A 400 invalid-request failure must propagate immediately without
    ever attempting the backup key, even when one is configured."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-primary-key")
    monkeypatch.setenv("GEMINI_API_KEY_BACKUP", "fake-backup-key")

    mock_primary = MagicMock()
    mock_primary.models.generate_content.side_effect = _invalid_request_error()
    mock_backup = MagicMock()

    with patch("ai.client.genai.Client", side_effect=[mock_primary, mock_backup]):
        with pytest.raises(genai_errors.ClientError):
            client_module.analyze_complaint("Some complaint.")

    mock_primary.models.generate_content.assert_called_once()
    mock_backup.models.generate_content.assert_not_called()
    # The backup client must never even have been constructed.
    assert client_module._backup_client is None


def test_local_validation_error_never_invokes_backup(monkeypatch):
    """A non-APIError failure (e.g. this module's own ValueError when
    Gemini returns an empty response) is not a Gemini API failure at
    all, and must never trigger a backup retry."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-primary-key")
    monkeypatch.setenv("GEMINI_API_KEY_BACKUP", "fake-backup-key")

    mock_response = MagicMock()
    mock_response.parsed = None
    mock_response.text = ""
    mock_primary = MagicMock()
    mock_primary.models.generate_content.return_value = mock_response
    mock_backup = MagicMock()

    with patch("ai.client.genai.Client", side_effect=[mock_primary, mock_backup]):
        with pytest.raises(ValueError, match="empty response"):
            client_module.analyze_complaint("Some complaint.")

    mock_backup.models.generate_content.assert_not_called()


def test_is_retryable_gemini_error_classification():
    """Direct unit coverage of the classifier itself, across the
    concrete error types/codes it's meant to distinguish."""
    assert client_module._is_retryable_gemini_error(_rate_limit_error()) is True
    assert client_module._is_retryable_gemini_error(_server_error()) is True
    assert client_module._is_retryable_gemini_error(_invalid_request_error()) is False
    assert client_module._is_retryable_gemini_error(ValueError("not a Gemini error")) is False


def test_get_backup_client_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY_BACKUP", raising=False)
    assert client_module._get_backup_client() is None


def test_get_backup_client_is_cached(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY_BACKUP", "fake-backup-key")
    mock_backup = MagicMock()
    with patch("ai.client.genai.Client", return_value=mock_backup) as mock_ctor:
        first = client_module._get_backup_client()
        second = client_module._get_backup_client()
    assert first is second
    mock_ctor.assert_called_once()


def test_failover_never_includes_api_key_value_in_raised_error(monkeypatch):
    """The exception raised after both keys fail must not contain either
    key's literal value anywhere in its string representation."""
    monkeypatch.setenv("GEMINI_API_KEY", "primary-secret-value-12345")
    monkeypatch.setenv("GEMINI_API_KEY_BACKUP", "backup-secret-value-67890")

    mock_primary = MagicMock()
    mock_primary.models.generate_content.side_effect = _rate_limit_error()
    mock_backup = MagicMock()
    mock_backup.models.generate_content.side_effect = _server_error()

    with patch("ai.client.genai.Client", side_effect=[mock_primary, mock_backup]):
        with pytest.raises(genai_errors.ServerError) as exc_info:
            client_module.analyze_complaint("Some complaint.")

    error_text = str(exc_info.value)
    assert "primary-secret-value-12345" not in error_text
    assert "backup-secret-value-67890" not in error_text


def test_generate_issue_explanation_also_uses_failover(monkeypatch):
    """Confirms the centralized wrapper is genuinely used by a second
    AI capability, not just analyze_complaint -- failover behavior is
    shared, not duplicated per function."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-primary-key")
    monkeypatch.setenv("GEMINI_API_KEY_BACKUP", "fake-backup-key")

    expected = IssueExplanationOutput(
        explanation="This looks correctly classified given the details provided.",
    )
    mock_primary = MagicMock()
    mock_primary.models.generate_content.side_effect = _rate_limit_error()
    mock_backup = MagicMock()
    mock_backup_response = MagicMock()
    mock_backup_response.parsed = expected
    mock_backup.models.generate_content.return_value = mock_backup_response

    with patch("ai.client.genai.Client", side_effect=[mock_primary, mock_backup]):
        result = client_module.generate_issue_explanation({"category": "Roads and Potholes"})

    assert result == expected
    mock_backup.models.generate_content.assert_called_once()
