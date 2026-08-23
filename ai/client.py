"""
Gemini client/service for CivicSync.

Provides a single entry point, `analyze_complaint`, that takes raw citizen
text and returns a validated CivicIssue. The Google GenAI client is
initialized lazily and cached so that importing this module (e.g. for
tests that only touch schemas.py) does not require an API key to be set.
"""

from __future__ import annotations

import os

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from ai.prompts import (
    EXPLANATION_SYSTEM_PROMPT,
    OPERATIONAL_BRIEFING_SYSTEM_PROMPT,
    PRIORITIZATION_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_explanation_prompt,
    build_operational_briefing_prompt,
    build_prioritization_prompt,
    build_user_prompt,
)
from ai.schemas import (
    CivicIssue,
    GeminiCivicIssueSchema,
    InsightPrioritizationOutput,
    IssueExplanationOutput,
    OperationalBriefingOutput,
)

# Current working Gemini model for this project. Do not change without
# team agreement (see README.md "AI assistants must not independently
# change core architecture without agreement").
GEMINI_MODEL = "gemini-3.6-flash"

# HTTP status codes treated as transient/retryable Gemini failures --
# rate limiting (429) and server-side problems (5xx). Anything else
# (400 invalid request, 401/403 auth, 404, safety-refusal-driven empty
# responses, or any non-APIError exception like a schema ValidationError)
# is NOT retried with the backup key, since a different key would not
# fix a malformed request, a validation failure, or a safety refusal --
# only a genuinely different, temporarily-unavailable backend would.
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

_client: genai.Client | None = None
_backup_client: genai.Client | None = None


def _get_client() -> genai.Client:
    """Lazily create and cache the primary Gemini client.

    Raises a clear, actionable error if GEMINI_API_KEY is not set, instead
    of letting the SDK fail with an opaque error later. The key itself is
    never logged or printed.
    """
    global _client
    if _client is not None:
        return _client

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "GEMINI_API_KEY is not set. Add it to your local .env file "
            "(see .env.example) and ensure it is loaded before calling "
            "the CivicSync AI service."
        )

    _client = genai.Client(api_key=api_key)
    return _client


def _get_backup_client() -> genai.Client | None:
    """Lazily create and cache the optional backup Gemini client.

    Returns None (never raises) if GEMINI_API_KEY_BACKUP isn't
    configured -- an absent backup key is not an error, it just means
    no failover is available, and the existing single-key behavior
    applies unchanged. The key itself is never logged or printed.
    """
    global _backup_client
    if _backup_client is not None:
        return _backup_client

    api_key = os.environ.get("GEMINI_API_KEY_BACKUP")
    if not api_key:
        return None

    _backup_client = genai.Client(api_key=api_key)
    return _backup_client


def _is_retryable_gemini_error(exc: Exception) -> bool:
    """Whether a Gemini call failure is a transient, retryable problem
    (rate limiting or a temporary service outage) as opposed to a
    non-retryable one (invalid request, auth failure, safety refusal,
    or a local validation/schema error) that failing over to a
    different API key would not fix.
    """
    code = getattr(exc, "code", None)
    return isinstance(exc, genai_errors.APIError) and code in _RETRYABLE_STATUS_CODES


def _generate_content_with_failover(*, model: str, contents, config):
    """Call Gemini's generate_content through the primary key, and --
    only for a retryable failure (rate limit or temporary service
    outage) with GEMINI_API_KEY_BACKUP configured -- retry exactly once
    more through the backup key. At most 2 attempts total.

    Every one of the 4 AI capabilities in this module (analyze_complaint,
    generate_insight_prioritization, generate_issue_explanation,
    generate_operational_briefing) calls this single centralized
    function rather than the SDK directly, so the failover policy lives
    in exactly one place. It changes nothing about the prompts, models,
    schemas, or response handling of any of those 4 functions -- it only
    decides which client makes the one underlying generate_content call.

    A non-retryable error (bad request, auth failure, or any exception
    that isn't a Gemini APIError with a retryable status code -- e.g. a
    local ValueError from post-processing) is never retried and
    propagates immediately, from whichever key raised it. If no backup
    key is configured, a retryable primary failure also propagates
    immediately -- this is the existing single-key behavior, preserved
    exactly. Never logs, prints, or includes either API key's value.
    """
    primary_client = _get_client()
    try:
        return primary_client.models.generate_content(model=model, contents=contents, config=config)
    except Exception as exc:
        if not _is_retryable_gemini_error(exc):
            raise
        backup_client = _get_backup_client()
        if backup_client is None:
            raise
        return backup_client.models.generate_content(model=model, contents=contents, config=config)


def analyze_complaint(citizen_text: str) -> CivicIssue:
    """Convert raw citizen complaint text into a structured CivicIssue.

    Args:
        citizen_text: Raw text of the citizen's complaint (already
            transcribed to text if it originated as speech).

    Returns:
        A validated CivicIssue instance.

    Raises:
        ValueError: if citizen_text is empty/blank.
        EnvironmentError: if GEMINI_API_KEY is not configured.
    """
    if not citizen_text or not citizen_text.strip():
        raise ValueError("citizen_text must be a non-empty string.")

    response = _generate_content_with_failover(
        model=GEMINI_MODEL,
        contents=build_user_prompt(citizen_text),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            # Use the Gemini-compatible schema variant here -- CivicIssue
            # itself would render additionalProperties: false in its JSON
            # Schema (from extra="forbid"), which Gemini's structured
            # output API rejects. See GeminiCivicIssueSchema's docstring.
            response_schema=GeminiCivicIssueSchema,
            temperature=0.1,
        ),
    )

    # The SDK will populate `.parsed` with a validated instance of
    # GeminiCivicIssueSchema when response_schema is a Pydantic model.
    # Regardless of whether `.parsed` is available, we re-validate through
    # the strict CivicIssue model before returning -- GeminiCivicIssueSchema
    # is deliberately more permissive (extra="ignore"), so relying on it
    # directly would silently skip CivicIssue's stricter local guarantees.
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, GeminiCivicIssueSchema):
        civic_issue = CivicIssue.model_validate(parsed.model_dump())
    else:
        if not response.text:
            raise ValueError("Gemini returned an empty response.")
        civic_issue = CivicIssue.model_validate_json(response.text)

    # CRITICAL (Milestone 22): original_text must be preserved EXACTLY as
    # the citizen submitted it -- never Gemini's own echo of it, which
    # the model is asked to reproduce as part of its structured output
    # but is not guaranteed to reproduce byte-for-byte, especially for
    # non-Latin scripts (Hindi/Bengali) where subtle transcription drift
    # is more likely than for plain English. Overwriting it here with the
    # real input the backend actually received guarantees exact
    # preservation regardless of anything Gemini does.
    return civic_issue.model_copy(update={"original_text": citizen_text})


def generate_insight_prioritization(insights: list[dict]) -> InsightPrioritizationOutput:
    """Ask Gemini to recommend a priority order and explanation for a list
    of already-computed, grounded civic insights.

    A separate capability from analyze_complaint() above (this reasons
    about aggregate insight data, not raw citizen text), but deliberately
    reuses the same client plumbing (_get_client(), GEMINI_MODEL) rather
    than standing up a second AI client.

    `insights` must contain only aggregate, non-identifying fields (type,
    priority, title, summary, affected_issue_count, evidence, department
    code/name) -- never per-issue detail like original_text or public_id.
    Gemini's role is strictly advisory: it reorders and explains what
    CivicSync already computed, and never changes any Issue, assignment,
    or status (see PRIORITIZATION_SYSTEM_PROMPT).

    Args:
        insights: A list of plain dicts, one per grounded insight.

    Returns:
        A validated InsightPrioritizationOutput instance.

    Raises:
        ValueError: if `insights` is empty, or Gemini returns an empty
            response.
        EnvironmentError: if GEMINI_API_KEY is not configured.
    """
    if not insights:
        raise ValueError("insights must be a non-empty list.")

    response = _generate_content_with_failover(
        model=GEMINI_MODEL,
        contents=build_prioritization_prompt(insights),
        config=types.GenerateContentConfig(
            system_instruction=PRIORITIZATION_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=InsightPrioritizationOutput,
            temperature=0.1,
        ),
    )

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, InsightPrioritizationOutput):
        return parsed

    if not response.text:
        raise ValueError("Gemini returned an empty response.")

    return InsightPrioritizationOutput.model_validate_json(response.text)


def generate_issue_explanation(issue_context: dict) -> IssueExplanationOutput:
    """Ask Gemini for a short, grounded, authority-facing explanation of
    a single already-extracted, already-persisted issue's existing
    classification.

    A separate capability from analyze_complaint() (new-complaint
    extraction) and generate_insight_prioritization() (aggregate-insight
    reasoning) above, but deliberately reuses the same client plumbing
    (_get_client(), GEMINI_MODEL) rather than standing up a second AI
    client.

    `issue_context` must contain only the issue's already-extracted
    structured fields (category, problem, location, duration,
    affected_population, severity, confidence, suggested_department,
    assigned_department, status_label) -- never the raw original_text or
    public_id. Gemini's role is strictly advisory and explanatory: it
    never changes any Issue, assignment, or status (see
    EXPLANATION_SYSTEM_PROMPT).

    Args:
        issue_context: A plain dict of the issue's structured fields.

    Returns:
        A validated IssueExplanationOutput instance.

    Raises:
        ValueError: if issue_context is empty, or Gemini returns an
            empty response.
        EnvironmentError: if GEMINI_API_KEY is not configured.
    """
    if not issue_context:
        raise ValueError("issue_context must not be empty.")

    response = _generate_content_with_failover(
        model=GEMINI_MODEL,
        contents=build_explanation_prompt(issue_context),
        config=types.GenerateContentConfig(
            system_instruction=EXPLANATION_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=IssueExplanationOutput,
            temperature=0.1,
        ),
    )

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, IssueExplanationOutput):
        return parsed

    if not response.text:
        raise ValueError("Gemini returned an empty response.")

    return IssueExplanationOutput.model_validate_json(response.text)


def generate_operational_briefing(payload: dict) -> OperationalBriefingOutput:
    """Ask Gemini to synthesize a short operational briefing from a
    jurisdiction-scoped (and optionally department-scoped) set of
    currently active issues.

    A separate capability from analyze_complaint() (new-complaint
    extraction), generate_insight_prioritization() (aggregate-insight
    reasoning), and generate_issue_explanation() (single-issue
    explanation) above, but deliberately reuses the same client plumbing
    (_get_client(), GEMINI_MODEL) rather than standing up a second AI
    client.

    `payload` must contain only structured/aggregate fields
    (jurisdiction_code, department_code, total_active_issues, and a list
    of issues each with only category/severity/status/department) --
    never original_text, public_id, or citizen-identifying data. Gemini's
    role is strictly advisory: it synthesizes and observes patterns in
    what it's given, and never changes any Issue, assignment, or status
    (see OPERATIONAL_BRIEFING_SYSTEM_PROMPT).

    Args:
        payload: A plain dict of jurisdiction/department scope plus the
            structured active-issue list.

    Returns:
        A validated OperationalBriefingOutput instance.

    Raises:
        ValueError: if payload is empty, or Gemini returns an empty
            response.
        EnvironmentError: if GEMINI_API_KEY is not configured.
    """
    if not payload:
        raise ValueError("payload must not be empty.")

    response = _generate_content_with_failover(
        model=GEMINI_MODEL,
        contents=build_operational_briefing_prompt(payload),
        config=types.GenerateContentConfig(
            system_instruction=OPERATIONAL_BRIEFING_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=OperationalBriefingOutput,
            temperature=0.1,
        ),
    )

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, OperationalBriefingOutput):
        return parsed

    if not response.text:
        raise ValueError("Gemini returned an empty response.")

    return OperationalBriefingOutput.model_validate_json(response.text)
