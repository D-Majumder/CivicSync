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

import ai.client as client_module
from ai.schemas import CivicIssue, GeminiCivicIssueSchema, IssueCategory

# Test-only: the real skipif decorator below reads os.environ at *collection*
# time (when this module is imported), which is earlier than any app code
# (e.g. backend/main.py) would normally call load_dotenv(). Load .env here so
# RUN_GEMINI_INTEGRATION_TESTS and GEMINI_API_KEY are already in os.environ
# by the time that decorator is evaluated. This does not change app behavior;
# it only affects how this test module resolves its own skip condition.
load_dotenv()


@pytest.fixture(autouse=True)
def reset_cached_client():
    """Ensure the module-level client cache doesn't leak between tests."""
    client_module._client = None
    yield
    client_module._client = None


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
