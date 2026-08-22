"""add resolution evidence table

Revision ID: 318ba5e831e7
Revises: 524456ef4a09
Create Date: 2026-08-22 11:34:38.463480

Milestone 18, Phase 3: resolution evidence attachments.

Adds a new, independent `resolution_evidence` table (metadata only --
actual file bytes live outside the database, in whatever
backend/evidence_storage.py is configured to use; see that module's
docstring for the storage decision). Purely additive: no existing table
is altered, so this needs no batch_alter_table and cannot affect any
existing Issue row.

Generated via `alembic revision --autogenerate` against the real ORM
model (backend/models.py's ResolutionEvidence) to guarantee the schema
here matches Base.metadata exactly -- verified with `alembic check`
after applying.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '318ba5e831e7'
down_revision: Union[str, None] = '524456ef4a09'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'resolution_evidence',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('public_id', sa.String(length=20), nullable=False),
        sa.Column('issue_id', sa.Integer(), nullable=False),
        sa.Column('storage_key', sa.String(length=255), nullable=False),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('content_type', sa.String(length=100), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('uploaded_by', sa.String(length=255), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['issue_id'], ['issues.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('storage_key'),
    )
    op.create_index(
        op.f('ix_resolution_evidence_issue_id'), 'resolution_evidence', ['issue_id'], unique=False
    )
    op.create_index(
        op.f('ix_resolution_evidence_public_id'), 'resolution_evidence', ['public_id'], unique=True
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_resolution_evidence_public_id'), table_name='resolution_evidence')
    op.drop_index(op.f('ix_resolution_evidence_issue_id'), table_name='resolution_evidence')
    op.drop_table('resolution_evidence')
