"""Interview configuration: type, difficulty, and question count.

Previously the shape of an interview was decided entirely by the generation
prompt. These columns record what the user actually asked for.

All three are NOT NULL with server defaults matching the old implicit
behaviour (mixed / mid / 5), so existing rows backfill without a data
migration and older clients that omit the fields are unaffected.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

interview_type = postgresql.ENUM(
    "behavioral",
    "technical",
    "system_design",
    "mixed",
    name="interview_type",
    create_type=False,
)
difficulty_level = postgresql.ENUM(
    "junior", "mid", "senior", name="difficulty_level", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    interview_type.create(bind, checkfirst=True)
    difficulty_level.create(bind, checkfirst=True)

    op.add_column(
        "interview_sessions",
        sa.Column(
            "interview_type", interview_type, nullable=False, server_default="mixed"
        ),
    )
    op.add_column(
        "interview_sessions",
        sa.Column("difficulty", difficulty_level, nullable=False, server_default="mid"),
    )
    op.add_column(
        "interview_sessions",
        sa.Column(
            "question_count", sa.Integer(), nullable=False, server_default="5"
        ),
    )
    # Bare name only: the metadata naming convention ("ck_%(table_name)s_%(constraint_name)s")
    # is applied on top of it. Passing the full name yields a double-prefixed,
    # hash-truncated constraint that no longer matches the model.
    op.create_check_constraint(
        "question_count_range",
        "interview_sessions",
        "question_count BETWEEN 3 AND 10",
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_constraint(
        "question_count_range",
        "interview_sessions",
        type_="check",
    )
    op.drop_column("interview_sessions", "question_count")
    op.drop_column("interview_sessions", "difficulty")
    op.drop_column("interview_sessions", "interview_type")
    difficulty_level.drop(bind, checkfirst=True)
    interview_type.drop(bind, checkfirst=True)
