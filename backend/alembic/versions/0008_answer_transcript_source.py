"""answer transcript source

Records whether an answer was typed or dictated. Backfilled to 'typed' by the
server default rather than left nullable: every existing answer *was* typed, so
NULL would mean "unknown" about rows we know the answer for.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-11 11:40:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = '0008'
down_revision: Union[str, None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'answers',
        sa.Column(
            'transcript_source',
            sa.String(length=16),
            nullable=False,
            server_default='typed',
        ),
    )


def downgrade() -> None:
    op.drop_column('answers', 'transcript_source')
