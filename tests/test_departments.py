"""
Tests for the Department registry: the ORM model + repository functions
(tests/conftest.py's `db_session` fixture, a temp SQLite DB), and the
seed-migration data (loaded directly from the migration file, same
pattern as tests/test_migrations.py). No Gemini calls anywhere here.
"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.database import Base, build_engine
from backend.models import Department
from backend.repository import get_active_departments, get_department_by_code

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "b196f6ddb0ac_add_department_registry_and_official_.py"
)

EXPECTED_SEED_CODES = {
    "STREET_LIGHTING",
    "ROADS_TRANSPORT",
    "WATER_SANITATION",
    "WASTE_MANAGEMENT",
    "PUBLIC_HEALTH",
    "ELECTRICITY",
    "PARKS_ENVIRONMENT",
    "OTHER",
}


@pytest.fixture
def migration_module():
    spec = importlib.util.spec_from_file_location("department_seed_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- 1. Department table exists ----------------------------------------------


def test_department_table_exists_with_expected_columns(db_session):
    inspector = inspect(db_session.get_bind())
    assert "departments" in inspector.get_table_names()
    columns = {col["name"] for col in inspector.get_columns("departments")}
    assert columns == {
        "id",
        "code",
        "name",
        "description",
        "is_active",
        "created_at",
        "updated_at",
    }


# --- 2. Seeded departments exist (via the seed migration) -------------------


def test_seed_departments_creates_all_eight(migration_module):
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        inserted = migration_module.seed_departments(conn)
        conn.commit()
    assert inserted == 8

    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        departments = db.query(Department).all()
        assert {d.code for d in departments} == EXPECTED_SEED_CODES
        assert all(d.is_active for d in departments)
        assert all(d.name for d in departments)
    finally:
        db.close()
        engine.dispose()


def test_seed_departments_is_idempotent(migration_module):
    """No duplicate departments if the seed runs more than once."""
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        first_run = migration_module.seed_departments(conn)
        conn.commit()
    assert first_run == 8

    with engine.connect() as conn:
        second_run = migration_module.seed_departments(conn)
        conn.commit()
    assert second_run == 0

    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        assert db.query(Department).count() == 8
    finally:
        db.close()
        engine.dispose()


# --- 3. Department codes are unique -----------------------------------------


def test_department_code_must_be_unique(db_session):
    db_session.add(Department(code="STREET_LIGHTING", name="Street Lighting"))
    db_session.commit()

    db_session.add(Department(code="STREET_LIGHTING", name="A Different Name"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_department_name_must_be_unique(db_session):
    db_session.add(Department(code="STREET_LIGHTING", name="Street Lighting"))
    db_session.commit()

    db_session.add(Department(code="A_DIFFERENT_CODE", name="Street Lighting"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_department_is_active_defaults_true(db_session):
    department = Department(code="ELECTRICITY", name="Electricity")
    db_session.add(department)
    db_session.commit()
    db_session.refresh(department)
    assert department.is_active is True


# --- 4. Repository helpers ----------------------------------------------------


def test_get_active_departments_excludes_inactive(db_session):
    db_session.add_all(
        [
            Department(code="STREET_LIGHTING", name="Street Lighting", is_active=True),
            Department(code="OTHER", name="Other", is_active=False),
        ]
    )
    db_session.commit()

    active = get_active_departments(db_session)
    assert [d.code for d in active] == ["STREET_LIGHTING"]


def test_get_department_by_code_returns_match_or_none(db_session):
    db_session.add(Department(code="ELECTRICITY", name="Electricity"))
    db_session.commit()

    found = get_department_by_code(db_session, "ELECTRICITY")
    assert found is not None
    assert found.name == "Electricity"

    assert get_department_by_code(db_session, "DOES_NOT_EXIST") is None
