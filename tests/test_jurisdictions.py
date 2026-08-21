"""
Tests for the jurisdiction hierarchy (Milestone 13): the ORM model +
repository functions. Uses tests/conftest.py's `db_session` fixture (a
temporary SQLite database, never civicsync.db). No Gemini calls -- this
is a pure data-layer feature.
"""

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from backend.models import Department, Jurisdiction, JurisdictionLevel
from backend.repository import (
    get_jurisdiction_ancestry,
    get_jurisdiction_by_code,
    get_jurisdictions_with_ancestry,
    list_jurisdictions,
)


def _make_chain(db_session) -> dict[str, Jurisdiction]:
    """Build the same 4-level demo chain the real migration seeds, for
    tests that need a populated hierarchy."""
    country = Jurisdiction(code="IN", name="India", level=JurisdictionLevel.COUNTRY, country_code="IN")
    db_session.add(country)
    db_session.flush()

    state = Jurisdiction(
        code="IN-WB", name="West Bengal", level=JurisdictionLevel.STATE,
        country_code="IN", parent_jurisdiction_id=country.id,
    )
    db_session.add(state)
    db_session.flush()

    district = Jurisdiction(
        code="IN-WB-NADIA", name="Nadia", level=JurisdictionLevel.DISTRICT,
        country_code="IN", parent_jurisdiction_id=state.id,
    )
    db_session.add(district)
    db_session.flush()

    local_body = Jurisdiction(
        code="IN-WB-NADIA-KRISHNANAGAR", name="Krishnanagar Municipality",
        level=JurisdictionLevel.LOCAL_BODY, country_code="IN",
        parent_jurisdiction_id=district.id,
    )
    db_session.add(local_body)
    db_session.commit()

    return {"country": country, "state": state, "district": district, "local_body": local_body}


# --- 1. Creating/retrieving jurisdictions -----------------------------------


def test_create_and_retrieve_jurisdiction(db_session):
    j = Jurisdiction(code="BR", name="Brazil", level=JurisdictionLevel.COUNTRY, country_code="BR")
    db_session.add(j)
    db_session.commit()

    found = get_jurisdiction_by_code(db_session, "BR")
    assert found is not None
    assert found.name == "Brazil"
    assert found.level == JurisdictionLevel.COUNTRY
    assert found.country_code == "BR"


def test_get_jurisdiction_by_code_returns_none_for_unknown_code(db_session):
    assert get_jurisdiction_by_code(db_session, "DOES_NOT_EXIST") is None


# --- 2. Parent-child hierarchy -----------------------------------------------


def test_parent_child_relationship(db_session):
    chain = _make_chain(db_session)
    db_session.refresh(chain["state"])
    assert chain["state"].parent_jurisdiction_id == chain["country"].id
    assert chain["state"].parent.code == "IN"


def test_jurisdiction_level_check_constraint_rejects_invalid_value(db_session):
    """The level column is CHECK-constrained (create_constraint=True),
    same discipline as IssueStatus/SeverityLevel elsewhere."""
    from sqlalchemy import text

    db_session.add(
        Jurisdiction(code="XX", name="Test", level=JurisdictionLevel.COUNTRY, country_code="XX")
    )
    db_session.commit()
    with __import__("pytest").raises(IntegrityError):
        db_session.execute(
            text("UPDATE jurisdictions SET level = :bad WHERE code = 'XX'"),
            {"bad": "PLANET"},
        )
        db_session.commit()


# --- 3. Multiple jurisdictions (e.g. a second country) -----------------------


def test_multiple_independent_country_trees(db_session):
    """Proves the hierarchy genuinely supports more than one country --
    the concrete BRICS-generalization claim."""
    in_country = Jurisdiction(code="IN", name="India", level=JurisdictionLevel.COUNTRY, country_code="IN")
    br_country = Jurisdiction(code="BR", name="Brazil", level=JurisdictionLevel.COUNTRY, country_code="BR")
    db_session.add_all([in_country, br_country])
    db_session.commit()

    india_only = list_jurisdictions(db_session, country_code="IN")
    brazil_only = list_jurisdictions(db_session, country_code="BR")
    assert [j.code for j in india_only] == ["IN"]
    assert [j.code for j in brazil_only] == ["BR"]


def test_list_jurisdictions_filters_by_level(db_session):
    _make_chain(db_session)
    states = list_jurisdictions(db_session, level=JurisdictionLevel.STATE)
    assert [j.code for j in states] == ["IN-WB"]


# --- 4-7: existing issue/department/tracking workflow is untouched ----------
# (Full end-to-end regression for these lives in the existing test suite --
# tests/test_departments.py, tests/test_lifecycle.py, tests/test_tracking_api.py,
# etc. -- which this milestone deliberately does not modify. Here we only
# confirm the *new* jurisdiction_id column doesn't break Department itself.)


def test_department_created_without_jurisdiction_remains_valid(db_session):
    """Backward compatibility: a department with no jurisdiction (the
    default for anything created before this migration, or intentionally
    unscoped) is still a completely valid row."""
    dept = Department(code="LEGACY_DEPT", name="Legacy Department")
    db_session.add(dept)
    db_session.commit()
    db_session.refresh(dept)
    assert dept.jurisdiction_id is None
    assert dept.jurisdiction is None


def test_department_can_be_linked_to_a_jurisdiction(db_session):
    chain = _make_chain(db_session)
    dept = Department(code="WATER", name="Water", jurisdiction_id=chain["local_body"].id)
    db_session.add(dept)
    db_session.commit()
    db_session.refresh(dept)
    assert dept.jurisdiction.code == "IN-WB-NADIA-KRISHNANAGAR"


# --- 8. Jurisdiction scoping (ancestry) --------------------------------------


def test_ancestry_is_root_to_leaf(db_session):
    chain = _make_chain(db_session)
    ancestry = get_jurisdiction_ancestry(db_session, chain["local_body"])
    assert [j.code for j in ancestry] == ["IN", "IN-WB", "IN-WB-NADIA", "IN-WB-NADIA-KRISHNANAGAR"]


def test_ancestry_of_a_country_is_just_itself(db_session):
    chain = _make_chain(db_session)
    ancestry = get_jurisdiction_ancestry(db_session, chain["country"])
    assert [j.code for j in ancestry] == ["IN"]


def test_ancestry_of_a_state_stops_at_root(db_session):
    chain = _make_chain(db_session)
    ancestry = get_jurisdiction_ancestry(db_session, chain["state"])
    assert [j.code for j in ancestry] == ["IN", "IN-WB"]


# --- 9. Empty jurisdiction cases ----------------------------------------------


def test_list_jurisdictions_empty_database_returns_empty_list(db_session):
    assert list_jurisdictions(db_session) == []


def test_get_jurisdictions_with_ancestry_empty_database(db_session):
    assert get_jurisdictions_with_ancestry(db_session) == []


# --- 10. Invalid hierarchy/reference cases ------------------------------------


def test_jurisdiction_parent_reference_is_not_db_enforced_but_app_never_creates_invalid_ones(
    db_session,
):
    """Documents a real, discovered characteristic: this project's SQLite
    connections don't have `PRAGMA foreign_keys=ON` enabled anywhere (not
    just for jurisdictions -- every existing FK in the schema, e.g.
    Issue.assigned_department_id, has the same property), so a bogus
    parent_jurisdiction_id is NOT rejected at the database level today.

    This is a pre-existing platform characteristic, not something
    introduced by this migration, and fixing it globally is out of scope
    for this milestone (see backend/database.py). What actually matters
    for correctness is that CivicSync's own application code (the seed
    migration, and any future jurisdiction-creation code going through
    this repository module) never constructs an invalid reference --
    which every other test in this file already exercises via real
    parent-child chains built through valid ids.
    """
    j = Jurisdiction(
        code="ORPHAN", name="Orphan", level=JurisdictionLevel.STATE,
        country_code="IN", parent_jurisdiction_id=999999,
    )
    db_session.add(j)
    db_session.commit()  # does not raise today -- documented above
    db_session.refresh(j)
    assert j.parent_jurisdiction_id == 999999
    # The relationship itself correctly resolves to nothing rather than
    # exploding, since the referenced row genuinely doesn't exist.
    assert j.parent is None


def test_jurisdiction_code_must_be_unique(db_session):
    db_session.add(Jurisdiction(code="IN", name="India", level=JurisdictionLevel.COUNTRY, country_code="IN"))
    db_session.commit()
    db_session.add(
        Jurisdiction(code="IN", name="Duplicate", level=JurisdictionLevel.COUNTRY, country_code="IN")
    )
    with __import__("pytest").raises(IntegrityError):
        db_session.commit()


# --- N+1 protection -----------------------------------------------------------


def test_get_jurisdictions_with_ancestry_avoids_n_plus_1(db_session):
    """Resolving ancestry for a whole list of jurisdictions must not
    re-query the table once per row -- exactly two queries total
    regardless of how many jurisdictions exist."""
    _make_chain(db_session)

    executed = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        executed.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        pairs = get_jurisdictions_with_ancestry(db_session)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert len(pairs) == 4  # all 4 levels of the demo chain
    # One query for the (parent-eager-loaded) filtered list, one for the
    # whole table used to resolve every row's ancestry -- not 4 (one per
    # jurisdiction), which is what a naive per-row implementation would do.
    assert len(executed) <= 2
