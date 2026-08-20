"""
Tests for backend/main.py startup behavior.

Confirms that importing backend.main calls dotenv.load_dotenv() before the
FastAPI app is constructed, so env-dependent modules (like ai/client.py)
can find GEMINI_API_KEY once the app has started.

We assert the call itself (rather than depending on dotenv's file-discovery
behavior, which searches relative to the importing module's location, not
the test's cwd) and never touch the project's real .env or any key value.
"""

import importlib
import sys
from unittest.mock import patch


def test_importing_main_calls_load_dotenv():
    # Force a fresh import so load_dotenv() actually runs during this test,
    # even if backend.main was already imported earlier in the session.
    sys.modules.pop("backend.main", None)
    sys.modules.pop("backend", None)

    with patch("dotenv.load_dotenv") as mock_load_dotenv:
        importlib.import_module("backend.main")

    mock_load_dotenv.assert_called_once()
