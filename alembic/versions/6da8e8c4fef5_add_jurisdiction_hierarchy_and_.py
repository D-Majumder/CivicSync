"""add jurisdiction hierarchy and department jurisdiction link

Revision ID: 6da8e8c4fef5
Revises: b196f6ddb0ac
Create Date: 2026-08-21 11:40:08.007795

Adds CivicSync's administrative jurisdiction hierarchy (Country -> State
-> District -> Local Body) as a new, self-referential `jurisdictions`
table, and links the existing `departments` table to it via a nullable
`jurisdiction_id` foreign key -- purely additive, no existing column is
changed or removed.

Also seeds a deterministic demo jurisdiction chain (India -> West Bengal
-> Nadia -> Krishnanagar Municipality, the exact example used in the M13
spec) and backfills the 8 existing seed departments to that municipality,
so the new hierarchy has real, visible data from the moment this
migration runs rather than an empty table. This mirrors the same seed +
backfill pattern already used by ae85602c974c/b196f6ddb0ac.

Backward compatible: `departments.jurisdiction_id` is NULLABLE, so any
department created (by future code, or by a differently-configured
deployment) without an explicit jurisdiction remains perfectly valid.
"""
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '6da8e8c4fef5'
down_revision: Union[str, None] = 'b196f6ddb0ac'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Lightweight, migration-local table references (not the live ORM models)
# -- the same pattern already used by every prior data migration in this
# project, so this migration keeps working correctly even if
# backend/models.py changes shape later.
jurisdictions_table = sa.table(
    "jurisdictions",
    sa.column("id", sa.Integer),
    sa.column("code", sa.String),
    sa.column("name", sa.String),
    sa.column("level", sa.String),
    sa.column("country_code", sa.String),
    sa.column("parent_jurisdiction_id", sa.Integer),
    sa.column("is_active", sa.Boolean),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)
departments_table = sa.table(
    "departments",
    sa.column("id", sa.Integer),
    sa.column("code", sa.String),
    sa.column("jurisdiction_id", sa.Integer),
)

# The demo jurisdiction chain, root to leaf. (code, name, level, parent_code)
# country_code is "IN" for every row (present on every level, not just the
# country row -- see Jurisdiction's docstring in backend/models.py for why).
SEED_JURISDICTIONS = [
    ("IN", "India", "COUNTRY", None),
    ("IN-WB", "West Bengal", "STATE", "IN"),
    ("IN-WB-NADIA", "Nadia", "DISTRICT", "IN-WB"),
    ("IN-WB-NADIA-KRISHNANAGAR", "Krishnanagar Municipality", "LOCAL_BODY", "IN-WB-NADIA"),
]

# Every existing seed department (from b196f6ddb0ac) is backfilled to the
# one demo municipality above -- this is the concrete proof that the
# hierarchy actually connects to real, pre-existing operational data, not
# just empty new tables.
EXISTING_DEPARTMENT_CODES = [
    "STREET_LIGHTING", "ROADS_TRANSPORT", "WATER_SANITATION", "WASTE_MANAGEMENT",
    "PUBLIC_HEALTH", "ELECTRICITY", "PARKS_ENVIRONMENT", "OTHER",
]


def seed_jurisdictions(bind) -> int:
    """Insert any SEED_JURISDICTIONS rows not already present, by code.
    Idempotent: safe to call more than once. Returns rows inserted."""
    existing_codes = {row[0] for row in bind.execute(sa.select(jurisdictions_table.c.code))}
    now = datetime.now(timezone.utc)

    # Insert level-by-level (parents before children) so parent_jurisdiction_id
    # can be resolved by code as we go.
    code_to_id: dict[str, int] = {
        row[0]: row[1]
        for row in bind.execute(sa.select(jurisdictions_table.c.code, jurisdictions_table.c.id))
    }

    inserted = 0
    for code, name, level, parent_code in SEED_JURISDICTIONS:
        if code in existing_codes:
            continue
        parent_id = code_to_id.get(parent_code) if parent_code else None
        bind.execute(
            sa.insert(jurisdictions_table).values(
                code=code,
                name=name,
                level=level,
                country_code="IN",
                parent_jurisdiction_id=parent_id,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
        )
        # sa.table() is a lightweight construct without full Table
        # metadata, so INSERT's inserted_primary_key isn't reliable here
        # -- re-query the row we just inserted by its unique code instead.
        new_id = bind.execute(
            sa.select(jurisdictions_table.c.id).where(jurisdictions_table.c.code == code)
        ).scalar_one()
        code_to_id[code] = new_id
        existing_codes.add(code)
        inserted += 1
    return inserted


def backfill_department_jurisdiction(bind, jurisdiction_code: str) -> int:
    """Set jurisdiction_id on every existing seed department that doesn't
    already have one. Idempotent -- only touches rows where
    jurisdiction_id IS NULL, so re-running never overwrites an
    intentional reassignment made after this migration first ran."""
    jurisdiction_id = bind.execute(
        sa.select(jurisdictions_table.c.id).where(jurisdictions_table.c.code == jurisdiction_code)
    ).scalar_one()

    result = bind.execute(
        sa.update(departments_table)
        .where(
            departments_table.c.code.in_(EXISTING_DEPARTMENT_CODES),
            departments_table.c.jurisdiction_id.is_(None),
        )
        .values(jurisdiction_id=jurisdiction_id)
    )
    return result.rowcount


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Jurisdiction hierarchy table. A brand-new table, so the FK here
    # (self-referential) can be created directly -- SQLite's ALTER TABLE
    # restrictions only bite when adding a constraint to an EXISTING
    # table (see the batch_alter_table step below for departments).
    op.create_table(
        "jurisdictions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "level",
            sa.Enum(
                "COUNTRY", "STATE", "DISTRICT", "LOCAL_BODY",
                name="jurisdiction_level",
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("parent_jurisdiction_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["parent_jurisdiction_id"], ["jurisdictions.id"]),
    )
    op.create_index(op.f("ix_jurisdictions_code"), "jurisdictions", ["code"], unique=True)
    op.create_index(
        op.f("ix_jurisdictions_country_code"), "jurisdictions", ["country_code"], unique=False
    )
    op.create_index(
        op.f("ix_jurisdictions_parent_jurisdiction_id"),
        "jurisdictions",
        ["parent_jurisdiction_id"],
        unique=False,
    )

    seed_jurisdictions(bind)

    # 2. Link departments -> jurisdictions. departments already exists, so
    # adding a new FK-backed column needs batch mode on SQLite (the same
    # pattern b196f6ddb0ac used for issues.assigned_department_id).
    with op.batch_alter_table("departments") as batch_op:
        batch_op.add_column(sa.Column("jurisdiction_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_departments_jurisdiction_id_jurisdictions",
            "jurisdictions",
            ["jurisdiction_id"],
            ["id"],
        )
    op.create_index(
        op.f("ix_departments_jurisdiction_id"), "departments", ["jurisdiction_id"], unique=False
    )

    backfill_department_jurisdiction(bind, "IN-WB-NADIA-KRISHNANAGAR")


def downgrade() -> None:
    op.drop_index(op.f("ix_departments_jurisdiction_id"), table_name="departments")
    with op.batch_alter_table("departments") as batch_op:
        batch_op.drop_constraint(
            "fk_departments_jurisdiction_id_jurisdictions", type_="foreignkey"
        )
        batch_op.drop_column("jurisdiction_id")

    op.drop_index(op.f("ix_jurisdictions_parent_jurisdiction_id"), table_name="jurisdictions")
    op.drop_index(op.f("ix_jurisdictions_country_code"), table_name="jurisdictions")
    op.drop_index(op.f("ix_jurisdictions_code"), table_name="jurisdictions")
    op.drop_table("jurisdictions")
