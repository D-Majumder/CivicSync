"""
Milestone 23: demo/development data seeding script
(scripts/seed_demo_data.py).

Exercises the script's seed()/clear() functions directly against an
isolated temporary SQLite database -- never civicsync.db. Confirms the
seeded dataset satisfies every M23 demo-data requirement and that
--clear removes exactly what was seeded, leaving nothing behind.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from backend.database import Base, build_engine
from backend.models import Issue, IssueStatus, Jurisdiction, JurisdictionLevel, ReopenRequest, ResolutionEvidence

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "seed_demo_data.py"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_demo_data", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seed_module():
    return _load_seed_module()


@pytest.fixture
def demo_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CIVICSYNC_DEFAULT_JURISDICTION_CODE", "IN-WB-NADIA-KRISHNANAGAR")
    db_path = tmp_path / "test_demo_seed.db"
    db_url = f"sqlite:///{db_path}"
    engine = build_engine(db_url)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    seed_db = Session()
    seed_db.add(
        Jurisdiction(
            code="IN-WB-NADIA-KRISHNANAGAR", name="Krishnanagar Municipality",
            level=JurisdictionLevel.LOCAL_BODY, country_code="IN",
        )
    )
    seed_db.commit()
    seed_db.close()
    yield db_url, engine, Session
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_manifest():
    """The manifest file lives next to the script, not per-test-isolated
    -- ensure no leftover manifest from a prior run/failure interferes
    with these tests, and clean up afterward."""
    manifest = SCRIPT_PATH.parent / "seed_demo_data.manifest.json"
    if manifest.exists():
        manifest.unlink()
    yield
    if manifest.exists():
        manifest.unlink()


def test_seed_creates_expected_number_of_issues(seed_module, demo_db):
    db_url, engine, Session = demo_db
    seed_module.seed(db_url)

    db = Session()
    assert db.query(Issue).count() == len(seed_module._demo_issues())
    db.close()


def test_seed_covers_multiple_severities(seed_module, demo_db):
    db_url, engine, Session = demo_db
    seed_module.seed(db_url)

    db = Session()
    severities = {i.severity.value for i in db.query(Issue).all()}
    assert {"Low", "Medium", "High", "Critical"}.issubset(severities)
    db.close()


def test_seed_covers_multiple_lifecycle_states(seed_module, demo_db):
    db_url, engine, Session = demo_db
    seed_module.seed(db_url)

    db = Session()
    statuses = {i.status for i in db.query(Issue).all()}
    assert IssueStatus.RESOLVED in statuses
    assert IssueStatus.REOPENED in statuses
    assert IssueStatus.REJECTED in statuses
    assert len(statuses) >= 4
    db.close()


def test_seed_creates_at_least_one_resolved_with_evidence(seed_module, demo_db):
    db_url, engine, Session = demo_db
    seed_module.seed(db_url)

    db = Session()
    assert db.query(ResolutionEvidence).count() >= 1
    resolved_issue_ids = {e.issue_id for e in db.query(ResolutionEvidence).all()}
    for issue_id in resolved_issue_ids:
        issue = db.query(Issue).filter(Issue.id == issue_id).one()
        assert issue.resolution_summary is not None
    db.close()


def test_seed_creates_at_least_one_reopened_issue(seed_module, demo_db):
    db_url, engine, Session = demo_db
    seed_module.seed(db_url)

    db = Session()
    reopened = db.query(Issue).filter(Issue.status == IssueStatus.REOPENED).all()
    assert len(reopened) >= 1
    # The reopen must have gone through a real, approved ReopenRequest.
    request = db.query(ReopenRequest).filter(ReopenRequest.issue_id == reopened[0].id).one()
    assert request.state.value == "APPROVED"
    db.close()


def test_seed_creates_geographic_cluster_detectable_by_m20(seed_module, demo_db):
    db_url, engine, Session = demo_db
    seed_module.seed(db_url)

    db = Session()
    from backend.service import get_civic_hotspots

    result = get_civic_hotspots(db, jurisdiction_code="IN-WB-NADIA-KRISHNANAGAR")
    assert len(result.hotspots) >= 1
    assert result.hotspots[0].complaint_count >= 3
    db.close()


def test_seed_far_away_point_does_not_join_cluster(seed_module, demo_db):
    db_url, engine, Session = demo_db
    seed_module.seed(db_url)

    db = Session()
    from backend.service import get_civic_hotspots

    result = get_civic_hotspots(db, jurisdiction_code="IN-WB-NADIA-KRISHNANAGAR")
    far_issue = db.query(Issue).filter(Issue.latitude == seed_module._FAR_LAT).one()
    for hotspot in result.hotspots:
        assert far_issue.public_id not in hotspot.member_public_ids
    db.close()


def test_seed_covers_multiple_languages(seed_module, demo_db):
    db_url, engine, Session = demo_db
    seed_module.seed(db_url)

    db = Session()
    languages = {i.citizen_language for i in db.query(Issue).all() if i.citizen_language}
    assert {"en", "hi", "bn"}.issubset(languages)
    db.close()


def test_seed_original_text_preserved_for_all_languages(seed_module, demo_db):
    db_url, engine, Session = demo_db
    seed_module.seed(db_url)

    db = Session()
    for spec in seed_module._demo_issues():
        issue = db.query(Issue).filter(Issue.original_text == spec["text"]).one_or_none()
        assert issue is not None
    db.close()


def test_seed_refuses_to_run_twice_without_clear(seed_module, demo_db):
    db_url, engine, Session = demo_db
    seed_module.seed(db_url)
    with pytest.raises(SystemExit):
        seed_module.seed(db_url)


def test_clear_removes_exactly_the_seeded_issues(seed_module, demo_db):
    db_url, engine, Session = demo_db
    seed_module.seed(db_url)
    seed_module.clear(db_url)

    db = Session()
    assert db.query(Issue).count() == 0
    assert db.query(ResolutionEvidence).count() == 0
    assert db.query(ReopenRequest).count() == 0
    db.close()


def test_clear_deletes_manifest_file(seed_module, demo_db):
    db_url, engine, Session = demo_db
    seed_module.seed(db_url)
    assert seed_module.MANIFEST_PATH.exists()
    seed_module.clear(db_url)
    assert not seed_module.MANIFEST_PATH.exists()


def test_clear_without_prior_seed_exits_cleanly(seed_module, demo_db):
    db_url, engine, Session = demo_db
    with pytest.raises(SystemExit):
        seed_module.clear(db_url)


def test_clear_never_touches_issues_outside_the_manifest(seed_module, demo_db):
    """A pre-existing, non-demo issue in the same database must survive
    --clear untouched -- this is the actual "does not permanently
    contaminate the real database" guarantee."""
    db_url, engine, Session = demo_db
    from ai.schemas import CivicIssue, IssueCategory, SeverityLevel
    from backend.repository import create_issue_from_civic_issue, get_default_jurisdiction_id

    db = Session()
    jid = get_default_jurisdiction_id(db)
    pre_existing = create_issue_from_civic_issue(
        db,
        CivicIssue(
            original_text="A real, pre-existing issue.", category=IssueCategory.OTHER,
            problem="Real issue.", severity=SeverityLevel.MEDIUM, confidence=0.7,
        ),
        jid,
    )
    db.commit()
    pre_existing_id = pre_existing.public_id
    db.close()

    seed_module.seed(db_url)
    seed_module.clear(db_url)

    db = Session()
    assert db.query(Issue).filter(Issue.public_id == pre_existing_id).one_or_none() is not None
    assert db.query(Issue).count() == 1
    db.close()
