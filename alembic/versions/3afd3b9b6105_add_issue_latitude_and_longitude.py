"""add issue latitude and longitude

Revision ID: 3afd3b9b6105
Revises: 6d0df8bd3d14
Create Date: 2026-08-22 17:35:43.012894

Milestone 20: geospatial civic intelligence.

Adds `issues.latitude`/`issues.longitude` -- both nullable, no default,
no backfill. No existing citizen submission flow captures coordinates
yet, so every pre-existing (and, for now, every newly-created) issue has
NULL here; hotspot detection (backend/hotspots.py) simply excludes
issues without both values set. Nothing here is geocoded or inferred
from the existing free-text `location` field -- that would risk
inventing coordinates CivicSync was never actually given.

Wrapped in batch_alter_table for SQLite compatibility, matching every
prior Issue-table column addition/removal in this project (e.g.
08c8ddac1c20's jurisdiction_id, 524456ef4a09's resolved_by).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '3afd3b9b6105'
down_revision: Union[str, None] = '6d0df8bd3d14'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("issues") as batch_op:
        batch_op.add_column(sa.Column("latitude", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("longitude", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("issues") as batch_op:
        batch_op.drop_column("longitude")
        batch_op.drop_column("latitude")
