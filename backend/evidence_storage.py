"""
Resolution evidence file storage (Milestone 18, Phase 3).

STORAGE DECISION -- read this before changing anything here.

CivicSync has no cloud object storage configured (no S3/GCS/Cloudinary
credentials, no existing upload code anywhere in the project). Introducing
a full cloud storage SDK for one hackathon-phase feature was judged out of
scope for this milestone. Instead, this module is a minimal, narrow
STORAGE BOUNDARY -- two functions, `save` and `load` -- so the rest of the
application (the API, the ResolutionEvidence model, every test) never
touches a filesystem path directly and is completely unaware of *how*
bytes are actually stored.

The one implementation provided here writes to local disk, under a
directory configured by CIVICSYNC_EVIDENCE_STORAGE_DIR (default:
"evidence_storage", created next to wherever the app runs from -- NOT
inside frontend/static, which serves versioned app assets, not user
uploads).

IMPORTANT DEPLOYMENT CAVEAT: Railway's default filesystem is EPHEMERAL.
Anything written to local disk is LOST on every redeploy and on most
restarts, unless a Railway Volume is explicitly attached and
CIVICSYNC_EVIDENCE_STORAGE_DIR is pointed at that volume's mount path
(e.g. "/data/evidence"). Without a volume, this module works correctly
within a single running process but is NOT durable storage in production.
This is documented here rather than silently assumed -- see this
milestone's final report for the exact Railway configuration needed.

To move to real persistent/cloud storage later (S3, GCS, etc.), only this
one file needs to change -- swap save()/load()'s implementation, keep the
same signatures. Nothing else in the codebase depends on local disk.
"""

from __future__ import annotations

import os
from pathlib import Path


def _storage_root() -> Path:
    root = Path(os.environ.get("CIVICSYNC_EVIDENCE_STORAGE_DIR", "evidence_storage"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def save(storage_key: str, content: bytes) -> None:
    """Persist evidence bytes under storage_key.

    storage_key MUST be server-generated (see
    backend/service.py's upload_issue_evidence) -- never derived from a
    client-supplied filename. This function does not sanitize its input;
    the caller is responsible for ensuring storage_key can never contain
    path separators or traversal sequences (".."), which is guaranteed by
    generating it from secrets.token_hex() rather than any user input.
    """
    path = _storage_root() / storage_key
    path.write_bytes(content)


def load(storage_key: str) -> bytes:
    """Read back evidence bytes previously saved under storage_key.

    Raises FileNotFoundError if storage_key doesn't exist -- callers
    (backend/service.py) translate that into a sanitized 404, never a
    raw filesystem error.
    """
    path = _storage_root() / storage_key
    return path.read_bytes()
