import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Enum, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.answer import Answer
    from app.models.interview_session import InterviewSession


class QuestionType(str, enum.Enum):
    BEHAVIORAL = "behavioral"
    TECHNICAL = "technical"
    FOLLOW_UP = "follow_up"


class Question(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "questions"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("interview_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # Self-reference models adaptive follow-up chains.
    parent_question_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("questions.id", ondelete="SET NULL"), nullable=True
    )
    sequence_number: Mapped[int] = mapped_column(nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    question_type: Mapped[QuestionType] = mapped_column(
        Enum(QuestionType, name="question_type", values_callable=lambda e: [x.value for x in e]),
        nullable=False,
    )
    # Explicitly passed over. Distinct from simply having no answer yet: this
    # records that the candidate decided not to answer, which is what lets the
    # UI show a "skipped" state instead of an unfinished one.
    skipped: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    # AI provenance: model name, prompt version, etc. Set by the future
    # Gemini/LangGraph generation pipeline.
    generation_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    session: Mapped["InterviewSession"] = relationship(back_populates="questions")
    answer: Mapped["Answer | None"] = relationship(
        back_populates="question", cascade="all, delete-orphan", uselist=False
    )
