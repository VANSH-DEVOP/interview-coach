import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.question import Question


class Answer(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "answers"

    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("questions.id", ondelete="CASCADE"),
        unique=True,  # 1:1 with question
        index=True,
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    duration_seconds: Mapped[int | None] = mapped_column(nullable=True)
    # How this answer was produced: typed, or dictated and transcribed.
    #
    # Recorded because the two are not comparable text. Speech arrives as
    # run-on, largely unpunctuated prose, and the heuristic evaluator scores
    # partly on word depth -- so a spoken answer and a typed one of equal
    # quality do not necessarily score alike, and without this column there is
    # no way to notice that, let alone correct for it.
    #
    # A plain string rather than a database enum: the set will grow (a
    # server-side transcriber is a different provenance from the browser's) and
    # widening a Postgres enum needs a migration where widening this does not.
    # Defaulted to "typed" so every existing row states what it is instead of
    # being null and ambiguous.
    transcript_source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="typed"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    question: Mapped["Question"] = relationship(back_populates="answer")
