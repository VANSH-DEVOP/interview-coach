import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.resume_chunk import ResumeChunk
    from app.models.user import User


class ResumeStatus(str, enum.Enum):
    UPLOADED = "uploaded"
    PARSED = "parsed"
    FAILED = "failed"


class Resume(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "resumes"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Provider-agnostic storage key (local path segment today, object key on
    # S3/R2/MinIO tomorrow). Never interpreted outside the storage layer.
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    # Populated by the future parsing/embedding pipeline (ChromaDB integration).
    parsed_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ResumeStatus] = mapped_column(
        Enum(ResumeStatus, name="resume_status", values_callable=lambda e: [x.value for x in e]),
        default=ResumeStatus.UPLOADED,
        nullable=False,
    )

    user: Mapped["User"] = relationship(back_populates="resumes")
    # Deleting a resume takes its chunks with it, in the database as well as
    # via the FK, so the ORM and the schema agree.
    chunks: Mapped[list["ResumeChunk"]] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
        order_by="ResumeChunk.ordinal",
    )
