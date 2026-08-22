"""add issue citizen language

Revision ID: 42fdb96d2957
Revises: 00b13a8ae6f1
Create Date: 2026-08-23 00:00:00.000000

Milestone 22: multilingual citizen experience.

Adds `issues.citizen_language` -- the citizen's own UI language
selection ("en"/"hi"/"bn") at submission time, for authority-side
audit/analytics only. This is the citizen's OWN selector choice, never
Gemini-detected. Nullable, no default, no backfill: every pre-existing
issue has NULL here. original_text itself is never altered based on
this field.

Wrapped in batch_alter_table for SQLite compatibility, matching every
prior Issue-table column addition in this project.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '42fdb96d2957'
down_revision: Union[str, None] = '00b13a8ae6f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("issues") as batch_op:
        batch_op.add_column(sa.Column("citizen_language", sa.String(length=8), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("issues") as batch_op:
        batch_op.drop_column("citizen_language")
