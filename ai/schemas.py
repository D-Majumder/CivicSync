"""
Structured data schemas for CivicSync's AI layer.

These models define the machine-processable shape that a raw citizen
complaint is converted into. They are intentionally strict about types
and value ranges so that downstream services (aggregation, mapping,
dashboards) can rely on consistent, predictable data rather than free-form
text.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class IssueCategory(str, Enum):
    """Fixed set of civic issue categories.

    A fixed vocabulary keeps classification consistent across thousands of
    complaints. OTHER is the deliberate escape hatch for anything that does
    not fit -- the model should never invent a new category string.
    """

    STREET_LIGHTING = "Street Lighting"
    ROADS_AND_POTHOLES = "Roads and Potholes"
    WATER_SUPPLY = "Water Supply"
    SEWAGE_AND_DRAINAGE = "Sewage and Drainage"
    SANITATION_AND_WASTE = "Sanitation and Waste"
    ELECTRICITY = "Electricity"
    PUBLIC_SAFETY = "Public Safety"
    PUBLIC_TRANSPORT = "Public Transport"
    PARKS_AND_PUBLIC_SPACES = "Parks and Public Spaces"
    NOISE_POLLUTION = "Noise Pollution"
    ILLEGAL_CONSTRUCTION = "Illegal Construction"
    STRAY_ANIMALS = "Stray Animals"
    OTHER = "Other"


class SeverityLevel(str, Enum):
    """Estimated severity of the reported issue.

    UNKNOWN is a first-class value (not a null/omission) so that "the model
    could not tell" is explicitly distinguishable from "the model judged it
    low severity".
    """

    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    CRITICAL = "Critical"
    UNKNOWN = "Unknown"


class CivicIssue(BaseModel):
    """Structured representation of a single citizen complaint.

    Fields that describe information the citizen may or may not have
    provided (location, duration, affected_population, suggested_department)
    are Optional and default to None. The AI is instructed (see prompts.py)
    to use None rather than guessing when information is not present in the
    source text -- this preserves the distinction between "unknown" and
    "known but empty".
    """

    original_text: str = Field(
        ...,
        min_length=1,
        description="The original, unmodified citizen complaint text this issue was extracted from.",
    )
    category: IssueCategory = Field(
        ...,
        description="Best-fit civic category for the complaint. Use OTHER if no category clearly applies.",
    )
    problem: str = Field(
        ...,
        min_length=1,
        description="Concise, specific description of what is wrong, in the AI's own words.",
    )
    location: str | None = Field(
        default=None,
        description="Geographic location or area mentioned in the complaint. None if not stated.",
    )
    duration: str | None = Field(
        default=None,
        description="How long the issue has reportedly been occurring. None if not stated or implied.",
    )
    severity: SeverityLevel = Field(
        default=SeverityLevel.UNKNOWN,
        description="Estimated severity of the issue based only on information present in the text.",
    )
    affected_population: str | None = Field(
        default=None,
        description="Who is affected by the issue, if stated or reasonably evident from context. None if not evident.",
    )
    suggested_department: str | None = Field(
        default=None,
        description="Local civic department or authority likely responsible for handling this issue. None if unclear.",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="AI's self-assessed confidence in the overall extraction, from 0.0 (very unsure) to 1.0 (very sure).",
    )

    model_config = {
        "use_enum_values": False,
        "extra": "forbid",
    }


class GeminiCivicIssueSchema(CivicIssue):
    """Gemini-facing variant of CivicIssue, used only to build the request's
    response_schema.

    Gemini's structured-output schema format is a restricted subset of
    OpenAPI/JSON Schema and does not support the `additionalProperties`
    keyword. Pydantic emits `additionalProperties: false` whenever
    `extra="forbid"` is set, which makes CivicIssue's own JSON Schema
    incompatible with the API ("Unknown name 'additional_properties'").

    This subclass inherits every field and validator from CivicIssue and
    only relaxes `extra` back to Pydantic's default, so its generated JSON
    Schema omits `additionalProperties` entirely. It is never used to
    validate or store data on its own -- ai/client.py re-validates every
    Gemini response through the strict CivicIssue model before returning
    it, so local validation guarantees are unchanged.
    """

    model_config = {
        "extra": "ignore",
    }


class InsightPrioritizationOutput(BaseModel):
    """Gemini-facing structured output for civic-insight prioritization.

    This is a NEW, separate capability from CivicIssue/GeminiCivicIssueSchema
    above (which extract structured data from citizen complaints) -- this
    schema is for reasoning ABOUT already-computed, grounded insights
    (see backend/insights.py), not for extracting new facts. It reuses the
    same Gemini client/model plumbing (ai/client.py's _get_client(),
    GEMINI_MODEL) rather than standing up a second AI client.

    extra="ignore" (not "forbid") for the same reason as
    GeminiCivicIssueSchema: Gemini's structured-output API rejects the
    additionalProperties JSON Schema keyword that extra="forbid" would
    otherwise render.

    Gemini is asked only to REORDER and EXPLAIN insights CivicSync already
    computed deterministically -- it is never asked to invent new counts,
    statistics, or insights, and its output never changes any Issue,
    assignment, or status. See ai/prompts.py's PRIORITIZATION_SYSTEM_PROMPT
    for the enforced boundary, and backend/service.py's
    get_prioritized_insights() for how the output is sanitized against the
    insights actually sent before being returned to the caller.
    """

    model_config = {"extra": "ignore"}

    recommended_priority_order: list[str] = Field(
        ...,
        description=(
            "The insight_type values from the provided insights, reordered "
            "most urgent/actionable first. Must only reuse insight_type "
            "values that were provided -- never invent a new one."
        ),
    )
    summary: str = Field(
        ..., description="One or two sentence summary of the overall prioritization."
    )
    explanation: str = Field(
        ...,
        description=(
            "Brief explanation of why this order was chosen, referencing the "
            "provided evidence (counts, departments, categories) directly -- "
            "not any information beyond what was provided."
        ),
    )
