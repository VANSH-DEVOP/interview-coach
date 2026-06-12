import enum
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import Enum, ForeignKey, Numeric
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.interview_session import InterviewSession


class ReportStatus(str, enum.Enum):
    PENDING = "pending"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"


class EvaluationReport(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "evaluation_reports"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("interview_sessions.id", ondelete="CASCADE"),
        unique=True,  # 1:1 with session
        index=True,
        nullable=False,
    )
    overall_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 2), nullable=True)
    # Schema-less AI output; populated by the future evaluation pipeline.
    strengths: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    weaknesses: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    detailed_feedback: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[ReportStatus] = mapped_column(
        Enum(ReportStatus, name="report_status", values_callable=lambda e: [x.value for x in e]),
        default=ReportStatus.PENDING,
        nullable=False,
    )

    session: Mapped["InterviewSession"] = relationship(back_populates="evaluation_report")
