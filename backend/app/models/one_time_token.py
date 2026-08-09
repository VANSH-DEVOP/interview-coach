"""Single-use, expiring tokens emailed to a user.

Password reset and email verification want exactly the same lifecycle -- mint,
email, redeem once, expire -- so they share a table with a `purpose`
discriminator rather than duplicating that logic twice. The hazard of sharing
is a verification token being accepted as a reset token; every lookup therefore
takes `purpose` as a required argument, and a test asserts the crossover fails.

Only a SHA-256 hash of the token is stored. The token itself lives in exactly
one place -- the user's inbox -- so a database leak yields nothing usable, the
same reasoning as password hashing. SHA-256 rather than bcrypt on purpose:
bcrypt's cost exists to slow down guessing low-entropy secrets, and these are
32 random bytes. Brute force is not the threat, and a slow hash on every
redemption would just be a slow endpoint.
"""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User


class TokenPurpose(str, enum.Enum):
    PASSWORD_RESET = "password_reset"
    EMAIL_VERIFICATION = "email_verification"


class OneTimeToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "one_time_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    purpose: Mapped[TokenPurpose] = mapped_column(
        Enum(
            TokenPurpose,
            name="token_purpose",
            values_callable=lambda e: [x.value for x in e],
        ),
        nullable=False,
    )
    # Hex SHA-256. Unique so redemption is a single indexed lookup rather than a
    # scan, and so two tokens can never collide into one row.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    # Set on redemption. A replayed link finds a consumed row and is refused.
    consumed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    user: Mapped["User"] = relationship(back_populates="one_time_tokens")
