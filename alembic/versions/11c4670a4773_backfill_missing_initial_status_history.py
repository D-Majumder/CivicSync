"""backfill missing initial status history

Revision ID: 11c4670a4773
Revises: ae85602c974c
Create Date: 2026-08-20 10:48:34.518651

Data-only migration -- adds no columns, tables, or constraints.

Root cause: a small number of pre-existing Issues (created before this
initial-history logic existed in backend/repository.py's
create_issue_from_civic_issue(), i.e. before the same commit that added
the issue_status_history table via ae85602c974c) never received the
"null -> SUBMITTED" history row, because that row is written by
application code at creation time, not by the schema migration itself.
Every Issue created by the current code already gets this row atomically;
this migration is a one-time repair for rows that predate that code.

Uses each affected Issue's own created_at as changed_at, so the backfilled
record reflects when the issue was actually submitted, not when this
migration happens to run.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '11c4670a4773'
down_revision: Union[str, None] = 'ae85602c974c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Lightweight, migration-local table references (not the live ORM models)
# -- the standard Alembic pattern for data migrations, so this migration
# keeps working correctly even if backend/models.py changes shape later.
issues_table = sa.table(
    "issues",
    sa.column("id", sa.Integer),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
history_table = sa.table(
    "issue_status_history",
    sa.column("id", sa.Integer),
    sa.column("issue_id", sa.Integer),
    sa.column("from_status", sa.String),
    sa.column("to_status", sa.String),
    sa.column("changed_at", sa.DateTime(timezone=True)),
    sa.column("reason", sa.Text),
)


def backfill_missing_initial_history(bind) -> int:
    """Insert a null -> SUBMITTED history row for every Issue that
    doesn't already have one. Returns the number of rows inserted.

    Idempotent -- safe to run more than once (e.g. re-running `alembic
    upgrade head` after a partial failure) since it only inserts for
    issue_ids that don't already have a from_status IS NULL row.
    """
    issue_ids_with_initial_row = {
        row[0]
        for row in bind.execute(
            sa.select(history_table.c.issue_id).where(history_table.c.from_status.is_(None))
        )
    }

    all_issues = bind.execute(sa.select(issues_table.c.id, issues_table.c.created_at)).fetchall()

    rows_to_insert = [
        {
            "issue_id": issue_id,
            "from_status": None,
            "to_status": "SUBMITTED",
            "changed_at": created_at,
            "reason": "Issue submitted.",
        }
        for issue_id, created_at in all_issues
        if issue_id not in issue_ids_with_initial_row
    ]

    if rows_to_insert:
        bind.execute(sa.insert(history_table), rows_to_insert)

    return len(rows_to_insert)


def upgrade() -> None:
    bind = op.get_bind()
    backfill_missing_initial_history(bind)


def downgrade() -> None:
    # Data-only migration. We intentionally do NOT delete the backfilled
    # rows on downgrade -- they are real historical facts (the issue really
    # was SUBMITTED at issue.created_at), not something to roll back.
    pass
