from dotenv import load_dotenv
from fastapi import FastAPI

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