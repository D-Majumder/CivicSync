"""add issue resolved_by field

Revision ID: 524456ef4a09
Revises: 08c8ddac1c20
Create Date: 2026-08-22 11:15:00.000000

Milestone 18, Phase 2: authority resolution workflow.

Adds ONLY `issues.resolved_by` -- the one genuinely new field this
milestone requires. The "resolution note" concept reuses the existing
`issues.resolution_summary` column (present since an earlier milestone
but never actually written to by any code path until now); no new
column was needed for that, so none is added here.

Purely additive: a nullable String column with no default and no
backfill. There is no historical value to backfill it with -- unlike
Milestone 17's jurisdiction_id (where a real default jurisdiction
existed to backfill to), there is no way to know, after the fact, which
authority resolved an issue that predates this column. Existing
RESOLVED/CLOSED issues simply keep resolved_by = NULL, which is the
honest state (matching resolved_at/closed_at's own existing nullable,
"unset until known" convention).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '524456ef4a09'
down_revision: Union[str, None] = '08c8ddac1c20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable column addition, via batch mode for SQLite compatibility --
    # same pattern already used for every prior Issue-table column
    # addition in this project (e.g. b196f6ddb0ac's
    # issues.assigned_department_id, 08c8ddac1c20's issues.jurisdiction_id).
    with op.batch_alter_table("issues") as batch_op:
        batch_op.add_column(sa.Column("resolved_by", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("issues") as batch_op:
        batch_op.drop_column("resolved_by")
