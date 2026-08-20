"""
API request/response models for the FastAPI layer.

Kept separate from ai/schemas.py: these describe the HTTP contract for
backend/main.py, while ai/schemas.py describes the AI service's own data
model (CivicIssue). CivicIssue is reused directly as the response model.
"""

from pydantic import BaseModel, Field


class AnalyzeRequest(BaseModel):
    """Request body for POST /api/analyze."""

    text: str = Field(
        ...,
        min_length=1,
        description="Raw citizen complaint text (voice-transcribed or typed) to analyze.",
        examples=["There has been no street light near our school for two weeks."],
    )
