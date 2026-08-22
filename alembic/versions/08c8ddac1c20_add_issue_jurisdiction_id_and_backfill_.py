"""add issue jurisdiction_id and backfill existing issues

Revision ID: 08c8ddac1c20
Revises: 6da8e8c4fef5
Create Date: 2026-08-22 06:00:00.000000

Fixes a real architectural bug: jurisdiction scoping previously worked
transitively via Issue -> assigned_department -> Department ->
Jurisdiction. Since assigned_department_id is intentionally NULL for
every SUBMITTED/CLASSIFIED issue (an issue isn't officially routed yet),
every newly-submitted issue silently disappeared from every
jurisdiction-scoped authority view (dashboard, queue, all issues, aging,
etc.) the moment it was created -- even though public tracking, status
history, Civic Intelligence, and Recent Activity all correctly saw it,
since those paths don't derive jurisdiction from department at all.

This migration adds a DIRECT, required `issues.jurisdiction_id` column,
independent of `assigned_department_id`:

    jurisdiction_id        "Where does this issue belong?" (required)
    assigned_department_id "Which department is responsible?" (nullable,
                            unchanged -- still set later, by an authority
                            action, never derived from jurisdiction)

Backward compatible with existing data: every pre-existing Issue row is
backfilled to IN-WB-NADIA-KRISHNANAGAR (the current production demo
jurisdiction, seeded by 6da8e8c4fef5) BEFORE the NOT NULL constraint is
applied. If that jurisdiction doesn't exist, this migration raises
immediately and makes NO schema changes at all, rather than silently
leaving jurisdiction_id NULL for existing rows -- see
_resolve_default_jurisdiction_id below.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '08c8ddac1c20'
down_revision: Union[str, None] = '6da8e8c4fef5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The current production demo jurisdiction (seeded by 6da8e8c4fef5).
# Existing issues predating this migration are backfilled to it -- this
# is a one-time historical backfill value, NOT the ongoing default used
# for newly-created issues going forward (that's
# CIVICSYNC_DEFAULT_JURISDICTION_CODE, read at request time by
# backend.repository.get_default_jurisdiction_id -- see .env.example).
BACKFILL_JURISDICTION_CODE = "IN-WB-NADIA-KRISHNANAGAR"

jurisdictions_table = sa.table(
    "jurisdictions",
    sa.column("id", sa.Integer),
    sa.column("code", sa.String),
)
issues_table = sa.table(
    "issues",
    sa.column("id", sa.Integer),
    sa.column("jurisdiction_id", sa.Integer),
)


def _resolve_backfill_jurisdiction_id(bind) -> int:
    """Look up BACKFILL_JURISDICTION_CODE's id. Raises immediately (before
    any schema change is made) if it doesn't exist -- existing issues
    must never silently end up with a NULL jurisdiction_id."""
    row = bind.execute(
        sa.select(jurisdictions_table.c.id).where(
            jurisdictions_table.c.code == BACKFILL_JURISDICTION_CODE
        )
    ).first()
    if row is None:
        raise RuntimeError(
            f"Migration {revision} requires the jurisdiction "
            f"{BACKFILL_JURISDICTION_CODE!r} to already exist (it should have "
            f"been seeded by migration 6da8e8c4fef5). Cannot safely backfill "
            f"existing issues.jurisdiction_id -- refusing to proceed rather "
            f"than leave it NULL."
        )
    return row[0]


def upgrade() -> None:
    bind = op.get_bind()

    # Resolve the backfill target FIRST, before touching the schema at
    # all -- if this raises, the migration makes zero changes.
    default_jurisdiction_id = _resolve_backfill_jurisdiction_id(bind)

    # 1. Add the column nullable first (required for backfilling existing
    # rows), with its FK, via batch mode (needed for SQLite -- same
    # pattern as 6da8e8c4fef5's departments.jurisdiction_id).
    with op.batch_alter_table("issues") as batch_op:
        batch_op.add_column(sa.Column("jurisdiction_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_issues_jurisdiction_id_jurisdictions",
            "jurisdictions",
            ["jurisdiction_id"],
            ["id"],
        )

    # 2. Backfill every existing issue (idempotent: only rows that are
    # still NULL are touched, so re-running this migration, or applying
    # it to a database where some rows were already somehow populated,
    # never overwrites an intentional value).
    bind.execute(
        sa.update(issues_table)
        .where(issues_table.c.jurisdiction_id.is_(None))
        .values(jurisdiction_id=default_jurisdiction_id)
    )

    # 3. Now that every row has a value, enforce NOT NULL at the DB level.
    with op.batch_alter_table("issues") as batch_op:
        batch_op.alter_column("jurisdiction_id", existing_type=sa.Integer(), nullable=False)

    op.create_index(op.f("ix_issues_jurisdiction_id"), "issues", ["jurisdiction_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_issues_jurisdiction_id"), table_name="issues")
    with op.batch_alter_table("issues") as batch_op:
        batch_op.drop_constraint("fk_issues_jurisdiction_id_jurisdictions", type_="foreignkey")
        batch_op.drop_column("jurisdiction_id")
