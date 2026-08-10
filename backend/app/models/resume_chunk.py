"""The retrievable pieces of a resume.

Chunks used to exist only inside Chroma, which had three consequences worth
fixing before anything else in the retrieval pipeline changes:

- **Re-indexing meant re-embedding.** The text was only in the vector store, so
  rebuilding an index cost one provider call per chunk against a quota of
  twenty per day.
- **Nothing could see what was stored.** "Why did retrieval return that?" had no
  answer short of querying Chroma by hand, and `chunks_produced` versus
  `chunks_embedded` was a process-local counter that died with the process.
- **There was nowhere to put a keyword index.** Hybrid retrieval needs the
  chunks in something that can do full-text search, which Postgres already can.

`embedded_at` is the durable version of that produced-versus-embedded gap: a
row with text and no `embedded_at` is a piece of the resume that retrieval
cannot see, which is exactly the state that used to be invisible.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Computed,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.resume import Resume


def retrieval_text(section: str | None, content: str) -> str:
    """The exact string a chunk is embedded as and retrieved as.

    One function, used by both the chunker and this model, because hybrid
    retrieval fuses the two halves *by chunk text*: the dense side returns what
    Chroma stored at index time, the keyword side rebuilds it from these
    columns, and if the two formattings differ by so much as a newline then no
    chunk is ever recognised as found by both. Rank fusion would still return a
    list, the `agreed` counter would sit at zero, and the fused results would
    quietly be the union of two half-length lists instead of a consensus.

    The heading is prepended rather than stored in `content` so that every
    chunk says which part of the resume it came from, including the third chunk
    of a long EXPERIENCE section.
    """
    return f"{section}\n{content}" if section else content


class ResumeChunk(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "resume_chunks"

    __table_args__ = (
        # Position within the resume is the chunk's identity, and re-indexing
        # replaces rows by it. Doubles as the lookup index for "this resume's
        # chunks in order", which is every read there is -- so `resume_id`
        # carries no index of its own, since a leading-column prefix of this
        # one serves the same queries.
        UniqueConstraint("resume_id", "ordinal", name="resume_ordinal"),
        # The keyword half of hybrid retrieval. GIN over the generated tsvector
        # below; without it every search sequentially scans the table.
        Index("ix_resume_chunks_search", "search_vector", postgresql_using="gin"),
    )

    resume_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("resumes.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Denormalised from the resume so a retrieval query can filter on ownership
    # without a join, the same reason the vector store keeps it in metadata.
    # Deleting the user cascades through the resume.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # Position in the resume, from zero. Stable across a re-index of unchanged
    # text, which is what makes chunk ids in the vector store meaningful.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    # The resume heading this text sat under -- "EXPERIENCE", "EDUCATION" --
    # or NULL for anything before the first heading (name, contact details).
    # Kept as its own column rather than folded into the text so it can be
    # filtered and inspected; `Chunk.retrieval_text` is what gets embedded.
    section: Mapped[str | None] = mapped_column(String(120), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # When this chunk's embedding reached the vector store. NULL means the text
    # is here and retrieval cannot see it.
    embedded_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Postgres keeps this in step with `section` and `content`; nothing in the
    # application writes it. Generated rather than maintained by a trigger or
    # by the repository, so a chunk written by any path -- a migration, a
    # backfill, psql -- is searchable, and the column cannot drift out of date.
    #
    # The section heading is indexed alongside the body so that "education" as
    # a query term reaches the EDUCATION block even when the word appears
    # nowhere in the text under it.
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            text("to_tsvector('english', coalesce(section, '') || ' ' || content)"),
            persisted=True,
        ),
        nullable=False,
    )

    resume: Mapped["Resume"] = relationship(back_populates="chunks")

    @property
    def retrieval_text(self) -> str:
        """What this chunk was embedded as. See the module-level function."""
        return retrieval_text(self.section, self.content)
