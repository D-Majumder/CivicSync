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
from google.genai import types

from ai.prompts import SYSTEM_PROMPT, build_user_prompt
from ai.schemas import CivicIssue, GeminiCivicIssueSchema

# Current working Gemini model for this project. Do not change without
# team agreement (see README.md "AI assistants must not independently
# change core architecture without agreement").
GEMINI_MODEL = "gemini-3.6-flash"

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    """Lazily create and cache the Gemini client.

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

    client = _get_client()

    response = client.models.generate_content(
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
        return CivicIssue.model_validate(parsed.model_dump())

    if not response.text:
        raise ValueError("Gemini returned an empty response.")

    return CivicIssue.model_validate_json(response.text)
