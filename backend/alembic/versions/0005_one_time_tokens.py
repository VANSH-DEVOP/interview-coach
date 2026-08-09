"""Emailed single-use tokens, and email verification state.

One table for both password reset and email verification: identical lifecycle,
distinguished by `purpose`. Only a SHA-256 hash of the token is stored.

`users.email_verified_at` is nullable with no backfill -- existing accounts are
genuinely unverified, and marking them otherwise would assert something nobody
ever checked. Nothing gates on it, so this changes no behaviour for them.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# create_type=False so `create_table` below does not try to CREATE TYPE a
# second time -- SQLAlchemy emits one automatically for any enum column it does
# not know already exists, which collides with the explicit create. The type is
# managed by hand here precisely so `downgrade` can drop it: Postgres keeps an
# enum type after its last column disappears, and a leftover type makes the
# upgrade fail on the way back up.
TOKEN_PURPOSE = postgresql.ENUM(
    "password_reset",
    "email_verification",
    name="token_purpose",
    create_type=False,
)


def upgrade() -> None:
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(), nullable=True))

    TOKEN_PURPOSE.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "one_time_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", TOKEN_PURPOSE, nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_one_time_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_one_time_tokens")),
    )
    op.create_index(op.f("ix_one_time_tokens_user_id"), "one_time_tokens", ["user_id"])
    op.create_index(
        op.f("ix_one_time_tokens_token_hash"), "one_time_tokens", ["token_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_one_time_tokens_token_hash"), table_name="one_time_tokens")
    op.drop_index(op.f("ix_one_time_tokens_user_id"), table_name="one_time_tokens")
    op.drop_table("one_time_tokens")
    TOKEN_PURPOSE.drop(op.get_bind(), checkfirst=True)
    op.drop_column("users", "email_verified_at")
