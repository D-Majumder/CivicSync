"""add issue location accuracy

Revision ID: 00b13a8ae6f1
Revises: 3afd3b9b6105
Create Date: 2026-08-22 20:10:00.000000

Milestone 21: citizen geolocation capture.

Adds `issues.location_accuracy` -- the browser-reported GPS accuracy (in
meters) for a citizen-captured coordinate, when the device supplies one.
Nullable, no default, no backfill: every pre-existing issue (and every
issue submitted without location capture, which remains fully
supported) has NULL here. Independent of latitude/longitude
(Milestone 20) -- always NULL if those are NULL, but not assumed
otherwise.

Wrapped in batch_alter_table for SQLite compatibility, matching every
prior Issue-table column addition in this project.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '00b13a8ae6f1'
down_revision: Union[str, None] = '3afd3b9b6105'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("issues") as batch_op:
        batch_op.add_column(sa.Column("location_accuracy", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("issues") as batch_op:
        batch_op.drop_column("location_accuracy")
