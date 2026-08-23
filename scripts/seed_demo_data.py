"""
Demo/development data seeding for CivicSync (Milestone 23).

FOR DEMO/DEVELOPMENT USE ONLY -- never run automatically by the
application itself, and never imported by any application code path.
This is a standalone CLI tool an operator runs manually before a demo.

Creates a small, realistic dataset covering everything a walkthrough of
CivicSync needs to show in one pass: multiple departments, every
severity level, a spread of lifecycle states, at least one resolved
issue with evidence, one reopened issue, a geographic cluster for the
Milestone 20 hotspot detector, and multilingual (English/Hindi/Bengali)
citizen complaints.

Every created row's public_id is recorded in a manifest file
(seed_demo_data.manifest.json, next to this script) so the exact same
data can be cleanly removed later with `--clear` -- this is what keeps
the mechanism from permanently contaminating whatever database it's
pointed at. Nothing here bypasses jurisdiction/lifecycle validation --
it calls the same repository/service functions the real application
uses (create_issue_from_civic_issue, transition_issue_status,
resolve_issue, create_evidence, create_reopen_request,
decide_reopen_request), the same way a real citizen/authority session
would, just without going through Gemini or HTTP.

Usage (from the repository root, with the same environment the app
itself uses -- CIVICSYNC_DEFAULT_JURISDICTION_CODE, etc.):

    python3 scripts/seed_demo_data.py            # create demo data
    python3 scripts/seed_demo_data.py --clear     # remove it again
    python3 scripts/seed_demo_data.py --db sqlite:///path/to/other.db
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.schemas import CivicIssue, IssueCategory, SeverityLevel  # noqa: E402
from backend.database import build_engine  # noqa: E402
from backend.models import (  # noqa: E402
    Issue,
    IssueStatusHistory,
    ReopenRequest,
    ResolutionEvidence,
)
from backend.repository import (  # noqa: E402
    create_evidence,
    create_issue_from_civic_issue,
    create_reopen_request,
    decide_reopen_request,
    get_default_jurisdiction_id,
    get_issue_by_public_id,
    resolve_issue,
    transition_issue_status,
)
from backend.transitions import IssueStatus  # noqa: E402

MANIFEST_PATH = Path(__file__).resolve().parent / "seed_demo_data.manifest.json"

# A real Krishnanagar-area coordinate, with a tight cluster of 3 nearby
# points for the Milestone 20 hotspot detector (within its default
# HOTSPOT_RADIUS_KM) plus one far-away point that must NOT join the
# cluster -- demonstrating the detector's distance boundary honestly.
_CLUSTER_LAT, _CLUSTER_LNG = 23.4058, 88.4894
_FAR_LAT, _FAR_LNG = 12.9716, 77.5946  # Bengaluru -- clearly not a hotspot member


def _tiny_png_bytes() -> bytes:
    """A minimal, genuinely valid 1x1 red PNG -- built by hand with zlib
    (no Pillow dependency needed for this standalone script)."""
    import struct
    import zlib

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + chunk_type
            + data
            + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
        )

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw_scanline = b"\x00" + b"\xff\x00\x00"  # filter byte + one red pixel
    idat = _chunk(b"IDAT", zlib.compress(raw_scanline))
    iend = _chunk(b"IEND", b"")
    return signature + ihdr + idat + iend


def _demo_issues():
    """Each entry: (text, category, severity, language, target lifecycle
    path, coordinates or None). The lifecycle path is a list of statuses
    to walk through in order, starting from SUBMITTED."""
    return [
        dict(
            text="There has been no street light near our school for two weeks.",
            category=IssueCategory.STREET_LIGHTING,
            severity=SeverityLevel.MEDIUM,
            language="en",
            path=[],  # stays SUBMITTED
            coords=None,
        ),
        dict(
            text="सड़क पर बहुत बड़ा गड्ढा है, बहुत खतरनाक है।",
            category=IssueCategory.ROADS_AND_POTHOLES,
            severity=SeverityLevel.HIGH,
            language="hi",
            path=["CLASSIFIED"],
            coords=None,
        ),
        dict(
            text="রাস্তার পাশে আবর্জনা জমে আছে অনেকদিন ধরে।",
            category=IssueCategory.SANITATION_AND_WASTE,
            severity=SeverityLevel.LOW,
            language="bn",
            path=["CLASSIFIED", "ROUTED"],
            coords=None,
        ),
        dict(
            text="Water main burst, flooding the street near the market, urgent.",
            category=IssueCategory.WATER_SUPPLY,
            severity=SeverityLevel.CRITICAL,
            language="en",
            path=["CLASSIFIED", "ROUTED", "ACKNOWLEDGED", "IN_PROGRESS"],
            coords=None,
        ),
        dict(
            text="Open drain overflowing near the bus stand, health hazard.",
            category=IssueCategory.PUBLIC_SAFETY,
            severity=SeverityLevel.HIGH,
            language="en",
            path="REJECTED",  # duplicate/spam-style rejection example
            coords=None,
        ),
        # --- Resolved with evidence ---
        dict(
            text="Broken park bench and litter near the children's play area.",
            category=IssueCategory.PARKS_AND_PUBLIC_SPACES,
            severity=SeverityLevel.LOW,
            language="en",
            path="RESOLVED_WITH_EVIDENCE",
            coords=None,
        ),
        # --- Resolved, then reopened (approved) ---
        dict(
            text="Streetlight flickering badly outside the temple entrance.",
            category=IssueCategory.STREET_LIGHTING,
            severity=SeverityLevel.MEDIUM,
            language="hi",
            path="REOPENED",
            coords=None,
        ),
        # --- Geographic cluster (3 close together) for M20 hotspot ---
        dict(
            text="Large pothole outside the market entrance.",
            category=IssueCategory.ROADS_AND_POTHOLES,
            severity=SeverityLevel.HIGH,
            language="en",
            path=[],
            coords=(_CLUSTER_LAT, _CLUSTER_LNG),
        ),
        dict(
            text="Road surface breaking apart just past the market.",
            category=IssueCategory.ROADS_AND_POTHOLES,
            severity=SeverityLevel.MEDIUM,
            language="bn",
            path=["CLASSIFIED"],
            coords=(_CLUSTER_LAT + 0.0003, _CLUSTER_LNG + 0.0003),
        ),
        dict(
            text="Another pothole near the market, same stretch of road.",
            category=IssueCategory.ROADS_AND_POTHOLES,
            severity=SeverityLevel.HIGH,
            language="en",
            path=[],
            coords=(_CLUSTER_LAT + 0.0002, _CLUSTER_LNG + 0.0001),
        ),
        # Deliberately far away -- proves the hotspot detector's distance
        # boundary honestly (must NOT join the cluster above).
        dict(
            text="Streetlight out on a residential lane, unrelated location.",
            category=IssueCategory.STREET_LIGHTING,
            severity=SeverityLevel.LOW,
            language="en",
            path=[],
            coords=(_FAR_LAT, _FAR_LNG),
        ),
    ]


def seed(db_url: str) -> None:
    if MANIFEST_PATH.exists():
        print(
            "A demo-data manifest already exists -- run with --clear first "
            "before seeding again, to avoid creating duplicate demo data.",
            file=sys.stderr,
        )
        sys.exit(1)

    engine = build_engine(db_url)
    Session = sessionmaker(bind=engine)
    db = Session()

    jurisdiction_id = get_default_jurisdiction_id(db)
    created_public_ids: list[str] = []

    for spec in _demo_issues():
        civic_issue = CivicIssue(
            original_text=spec["text"],
            category=spec["category"],
            problem=spec["text"],
            severity=spec["severity"],
            confidence=0.85,
        )
        lat, lng = spec["coords"] if spec["coords"] else (None, None)
        issue = create_issue_from_civic_issue(
            db,
            civic_issue,
            jurisdiction_id,
            latitude=lat,
            longitude=lng,
            citizen_language=spec["language"],
        )
        db.commit()
        created_public_ids.append(issue.public_id)

        path = spec["path"]
        if path == "REJECTED":
            transition_issue_status(db, issue, IssueStatus.REJECTED, reason="Demo: duplicate report.")
        elif path == "RESOLVED_WITH_EVIDENCE":
            for status in [IssueStatus.CLASSIFIED, IssueStatus.ROUTED, IssueStatus.ACKNOWLEDGED, IssueStatus.IN_PROGRESS]:
                transition_issue_status(db, issue, status)
            create_evidence(
                db, issue,
                storage_key=f"demo-{issue.public_id}.png",
                original_filename="repair_photo.png",
                content_type="image/png",
                size_bytes=len(_tiny_png_bytes()),
                uploaded_by="demo-authority",
            )
            resolve_issue(db, issue, "Bench repaired and area cleaned by parks crew.", "demo-authority")
        elif path == "REOPENED":
            for status in [IssueStatus.CLASSIFIED, IssueStatus.ROUTED, IssueStatus.ACKNOWLEDGED, IssueStatus.IN_PROGRESS]:
                transition_issue_status(db, issue, status)
            resolve_issue(db, issue, "Fixture replaced.", "demo-authority")
            request = create_reopen_request(db, issue, "The light went out again after only four days.")
            decide_reopen_request(
                db, request, issue,
                approve=True,
                decision_reason="Confirmed recurrence on-site; reopening for a proper repair.",
                decided_by="demo-authority",
            )
        else:
            for status_name in path:
                transition_issue_status(db, issue, IssueStatus[status_name])

    MANIFEST_PATH.write_text(json.dumps({"public_ids": created_public_ids}, indent=2))
    print(f"Seeded {len(created_public_ids)} demo issues.")
    print(f"Manifest written to {MANIFEST_PATH}")
    print("Run with --clear to remove this demo data cleanly.")


def clear(db_url: str) -> None:
    if not MANIFEST_PATH.exists():
        print("No demo-data manifest found -- nothing to clear.", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(MANIFEST_PATH.read_text())
    public_ids = manifest.get("public_ids", [])

    engine = build_engine(db_url)
    Session = sessionmaker(bind=engine)
    db = Session()

    removed = 0
    for public_id in public_ids:
        issue = get_issue_by_public_id(db, public_id)
        if issue is None:
            continue
        db.query(ReopenRequest).filter(ReopenRequest.issue_id == issue.id).delete()
        db.query(ResolutionEvidence).filter(ResolutionEvidence.issue_id == issue.id).delete()
        db.query(IssueStatusHistory).filter(IssueStatusHistory.issue_id == issue.id).delete()
        db.delete(issue)
        removed += 1
    db.commit()

    MANIFEST_PATH.unlink()
    print(f"Removed {removed} demo issue(s). Manifest deleted.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clear", action="store_true", help="Remove previously-seeded demo data.")
    parser.add_argument(
        "--db",
        default="sqlite:///civicsync.db",
        help="Database URL to seed/clear (default: sqlite:///civicsync.db, run from the repo root).",
    )
    args = parser.parse_args()

    if args.clear:
        clear(args.db)
    else:
        seed(args.db)


if __name__ == "__main__":
    main()
