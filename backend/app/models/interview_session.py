import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.evaluation_report import EvaluationReport
    from app.models.question import Question
    from app.models.user import User


class SessionStatus(str, enum.Enum):
    CREATED = "created"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class InterviewType(str, enum.Enum):
    """What kind of interview to simulate. Shapes the generation prompt."""

    BEHAVIORAL = "behavioral"
    TECHNICAL = "technical"
    SYSTEM_DESIGN = "system_design"
    MIXED = "mixed"


class DifficultyLevel(str, enum.Enum):
    """Seniority the questions should target."""

    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"


# Bounds shared by the model, the schema, and the generator prompt.
MIN_QUESTION_COUNT = 3
MAX_QUESTION_COUNT = 10
DEFAULT_QUESTION_COUNT = 5


class InterviewSession(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "interview_sessions"

    # Schemas validate this too; the constraint is the backstop that survives a
    # direct write. The name matches the "ck" naming convention so autogenerate
    # sees it as already applied by migration 0002.
    __table_args__ = (
        CheckConstraint(
            f"question_count BETWEEN {MIN_QUESTION_COUNT} AND {MAX_QUESTION_COUNT}",
            name="question_count_range",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    resume_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    target_role: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, name="session_status", values_callable=lambda e: [x.value for x in e]),
        default=SessionStatus.CREATED,
        nullable=False,
    )
    interview_type: Mapped[InterviewType] = mapped_column(
        Enum(
            InterviewType,
            name="interview_type",
            values_callable=lambda e: [x.value for x in e],
        ),
        default=InterviewType.MIXED,
        server_default=InterviewType.MIXED.value,
        nullable=False,
    )
    difficulty: Mapped[DifficultyLevel] = mapped_column(
        Enum(
            DifficultyLevel,
            name="difficulty_level",
            values_callable=lambda e: [x.value for x in e],
        ),
        default=DifficultyLevel.MID,
        server_default=DifficultyLevel.MID.value,
        nullable=False,
    )
    question_count: Mapped[int] = mapped_column(
        Integer,
        default=DEFAULT_QUESTION_COUNT,
        server_default=str(DEFAULT_QUESTION_COUNT),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    user: Mapped["User"] = relationship(back_populates="interview_sessions")
    questions: Mapped[list["Question"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="Question.sequence_number",
    )
    evaluation_report: Mapped["EvaluationReport | None"] = relationship(
        back_populates="session", cascade="all, delete-orphan", uselist=False
    )
