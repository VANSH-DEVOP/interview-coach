from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.interview_session import InterviewSession
    from app.models.one_time_token import OneTimeToken
    from app.models.refresh_token import RefreshToken
    from app.models.resume import Resume


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    # Null until the address is proven. Deliberately does NOT gate login: the
    # log email backend is the default, so gating would lock everyone out of a
    # local or demo deployment where nothing actually sends mail.
    email_verified_at: Mapped[datetime | None] = mapped_column(nullable=True)

    @property
    def email_verified(self) -> bool:
        return self.email_verified_at is not None

    resumes: Mapped[list["Resume"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    interview_sessions: Mapped[list["InterviewSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    one_time_tokens: Mapped[list["OneTimeToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
