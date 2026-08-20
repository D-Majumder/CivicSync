from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from google.genai.errors import APIError

from ai.client import analyze_complaint
from ai.schemas import CivicIssue
from backend.schemas import AnalyzeRequest

# Load environment variables from .env before anything else (e.g. ai/client.py)
# reads them. Must run before any AI service call, since GEMINI_API_KEY is
# read from the environment at call time.
load_dotenv()

app = FastAPI(
    title="CivicSync API",
    description="AI-powered civic intelligence platform API",
    version="0.1.0",
)


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "CivicSync API",
        "version": "0.1.0",
    }


@app.post(
    "/api/analyze",
    response_model=CivicIssue,
    status_code=status.HTTP_200_OK,
    summary="Analyze a citizen complaint into a structured civic issue",
)
def analyze(request: AnalyzeRequest) -> CivicIssue:
    """Convert raw citizen complaint text into a structured CivicIssue.

    All Gemini/AI logic lives in ai/client.py; this route only validates
    the request, delegates to the existing AI service, and translates
    known failure modes into clean HTTP responses.
    """
    try:
        return analyze_complaint(request.text)
    except ValueError as exc:
        # e.g. blank text (belt-and-suspenders alongside AnalyzeRequest's own
        # validation) or an empty/unparsable Gemini response.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None
    except EnvironmentError:
        # Misconfiguration (e.g. missing GEMINI_API_KEY). Never echo the
        # underlying message back to the client -- it could reference env
        # var contents or local setup details.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI service is not configured correctly.",
        ) from None
    except APIError:
        # Gemini-side failure (network, auth, rate limit, server error, etc).
        # google-genai's APIError messages can include request/response
        # internals, so respond with a generic message instead of str(exc).
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI service is temporarily unavailable. Please try again shortly.",
        ) from None