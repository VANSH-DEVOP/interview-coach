"""Server-side record of every issued refresh token.

A JWT is self-contained, which is what makes it fast and what makes it
impossible to take back. Logout, password change, password reset and account
deletion all need to invalidate credentials *before* they expire, so the server
has to keep a list of what is currently valid.

Only the `jti` is stored, not the token. The claim is a random 32-hex value
inside a signature the attacker cannot forge without `JWT_SECRET_KEY`, so a
database leak yields identifiers, not usable credentials -- there is nothing
here to hash.

Postgres rather than Redis, deliberately: the Redis in this stack is configured
with no persistence (see docker-compose.yml), and a revocation list that
forgets on restart silently un-revokes every token that was cancelled.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User


class RefreshToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # The JWT's `jti` claim. Unique so a replayed identifier cannot be inserted
    # twice, and indexed because every refresh looks a token up by it.
    jti: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    # Set on rotation, logout, password change/reset, or when reuse of an
    # already-rotated token implicates the whole family.
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")

    @property
    def is_active(self) -> bool:
        from app.core.time import utcnow

        return self.revoked_at is None and self.expires_at > utcnow()
