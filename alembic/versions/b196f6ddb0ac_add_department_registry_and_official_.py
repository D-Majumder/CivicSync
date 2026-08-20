"""add department registry and official assignment

Revision ID: b196f6ddb0ac
Revises: 11c4670a4773
Create Date: 2026-08-20 11:04:42.932541

Creates the controlled department registry (departments), seeds it with
the initial 8 departments (idempotent), creates the assignment audit
trail (issue_assignment_history), and replaces issues.assigned_department
(free text) with issues.assigned_department_id (a foreign key into
departments) -- assigned_department was never a controlled vocabulary
before this; going forward the official assignment is always a real
Department row, never an arbitrary string.

For existing Issue rows: assigned_department was never actually set by
any prior feature (no assignment endpoint existed before this migration),
so every existing row's value is NULL, and the new assigned_department_id
column is correspondingly NULL for all of them -- no guessing or mapping
was needed. suggested_department (the AI's free-text recommendation) is a
separate column entirely and is completely untouched by this migration.
"""
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b196f6ddb0ac'
down_revision: Union[str, None] = '11c4670a4773'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Lightweight, migration-local table reference for the seed step (not the
# live ORM model) -- standard Alembic pattern for data migrations, so this
# migration keeps working correctly even if backend/models.py changes
# shape later.
departments_table = sa.table(
    "departments",
    sa.column("id", sa.Integer),
    sa.column("code", sa.String),
    sa.column("name", sa.String),
    sa.column("description", sa.Text),
    sa.column("is_active", sa.Boolean),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)

# The controlled initial department set. (code, name, description)
SEED_DEPARTMENTS = [
    (
        "STREET_LIGHTING",
        "Street Lighting",
        "Installation, repair, and maintenance of public street lighting.",
    ),
    (
        "ROADS_TRANSPORT",
        "Roads & Transport",
        "Road surfaces, potholes, signage, and public transport infrastructure.",
    ),
    (
        "WATER_SANITATION",
        "Water & Sanitation",
        "Water supply, sewage, and drainage infrastructure.",
    ),
    (
        "WASTE_MANAGEMENT",
        "Waste Management",
        "Garbage collection, recycling, and public sanitation.",
    ),
    (
        "PUBLIC_HEALTH",
        "Public Health",
        "Public health hazards and community health concerns.",
    ),
    (
        "ELECTRICITY",
        "Electricity",
        "Electrical supply infrastructure outside of street lighting.",
    ),
    (
        "PARKS_ENVIRONMENT",
        "Parks & Environment",
        "Public parks, green spaces, and environmental concerns.",
    ),
    (
        "OTHER",
        "Other",
        "Issues that do not fit an existing department category.",
    ),
]


def seed_departments(bind) -> int:
    """Insert any SEED_DEPARTMENTS rows not already present, by code.
    Idempotent: safe to call more than once. Returns rows inserted."""
    existing_codes = {row[0] for row in bind.execute(sa.select(departments_table.c.code))}
    now = datetime.now(timezone.utc)

    rows_to_insert = [
        {
            "code": code,
            "name": name,
            "description": description,
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        for code, name, description in SEED_DEPARTMENTS
        if code not in existing_codes
    ]

    if rows_to_insert:
        bind.execute(sa.insert(departments_table), rows_to_insert)

    return len(rows_to_insert)


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Department registry.
    op.create_table(
        "departments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(op.f("ix_departments_code"), "departments", ["code"], unique=True)

    seed_departments(bind)

    # 2. Assignment audit trail.
    op.create_table(
        "issue_assignment_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("issue_id", sa.Integer(), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("unassigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.id"]),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"]),
    )
    op.create_index(
        op.f("ix_issue_assignment_history_issue_id"),
        "issue_assignment_history",
        ["issue_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_issue_assignment_history_department_id"),
        "issue_assignment_history",
        ["department_id"],
        unique=False,
    )

    # 3. Replace issues.assigned_department (free text) with
    # issues.assigned_department_id (FK into departments). Every existing
    # row's assigned_department is NULL (see module docstring), so the new
    # column is correspondingly NULL for all of them -- nothing to map.
    with op.batch_alter_table("issues") as batch_op:
        batch_op.drop_column("assigned_department")
        batch_op.add_column(sa.Column("assigned_department_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_issues_assigned_department_id_departments",
            "departments",
            ["assigned_department_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("issues") as batch_op:
        batch_op.drop_constraint(
            "fk_issues_assigned_department_id_departments", type_="foreignkey"
        )
        batch_op.drop_column("assigned_department_id")
        batch_op.add_column(sa.Column("assigned_department", sa.String(length=255), nullable=True))

    op.drop_index(
        op.f("ix_issue_assignment_history_department_id"),
        table_name="issue_assignment_history",
    )
    op.drop_index(
        op.f("ix_issue_assignment_history_issue_id"),
        table_name="issue_assignment_history",
    )
    op.drop_table("issue_assignment_history")

    op.drop_index(op.f("ix_departments_code"), table_name="departments")
    op.drop_table("departments")
