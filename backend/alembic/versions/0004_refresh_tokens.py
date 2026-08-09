"""Refresh-token revocation list.

Makes a refresh token cancellable before it expires, which logout, password
change, password reset and account deletion all depend on.

Existing refresh tokens have no row here and stop being refreshable the moment
this deploys -- their next refresh fails and the client logs in again. That is
the one-off cost of introducing the list; it is not a data migration we can
write, because the tokens live in clients' cookies and their `jti` values are
not recorded anywhere.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("jti", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_refresh_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
    )
    op.create_index(op.f("ix_refresh_tokens_user_id"), "refresh_tokens", ["user_id"])
    # Unique: every refresh looks a token up by jti, and a duplicate identifier
    # would make revocation ambiguous.
    op.create_index(op.f("ix_refresh_tokens_jti"), "refresh_tokens", ["jti"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_refresh_tokens_jti"), table_name="refresh_tokens")
    op.drop_index(op.f("ix_refresh_tokens_user_id"), table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
