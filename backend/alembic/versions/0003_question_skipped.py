"""Question.skipped: explicitly passed over, as opposed to not yet answered.

NOT NULL with a server default of false, so existing rows backfill without a
data migration -- nothing was skippable before this.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-08
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "questions",
        sa.Column("skipped", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("questions", "skipped")
