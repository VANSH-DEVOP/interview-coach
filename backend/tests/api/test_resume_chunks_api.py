"""Resume chunks as rows.

Before this table the chunks existed only inside Chroma, so re-indexing meant
re-embedding, nothing could see what had been stored, and "30 chunks produced,
4 embedded" was a process-local counter that died with the process. These tests
are about the properties the table exists to provide.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models.resume import Resume
from app.models.resume_chunk import ResumeChunk
from app.repositories.resume_chunk_repository import ResumeChunkRepository
from app.services.ai.rag import ResumeChunker

RESUME_TEXT = """\
Rae Sandoval
Data Engineer

SUMMARY
Six years building batch and streaming pipelines.

EXPERIENCE
Senior Data Engineer, Cartwheel (2021-2026)
Owned the warehouse ingestion path end to end.

EDUCATION
BSc Mathematics, University of Leeds

SKILLS
Python, Spark, Airflow, dbt
"""


@pytest.fixture
async def resume(db_session, registered_user) -> Resume:
    """A parsed resume row, with no chunks yet."""
    resume = Resume(
        user_id=uuid.UUID(registered_user["user"]["id"]),
        file_name="cv.pdf",
        storage_key="resumes/u/cv.pdf",
        content_type="application/pdf",
        size_bytes=1234,
        parsed_text=RESUME_TEXT,
    )
    db_session.add(resume)
    await db_session.flush()
    return resume


@pytest.fixture
def chunks(db_session) -> ResumeChunkRepository:
    return ResumeChunkRepository(db_session)


def _rows(text: str) -> list[ResumeChunk]:
    return [
        ResumeChunk(ordinal=chunk.ordinal, section=chunk.section, content=chunk.content)
        for chunk in ResumeChunker().chunk(text)
    ]


async def test_chunks_are_stored_with_their_section_and_order(resume, chunks, db_session):
    await chunks.replace_for_resume(resume.id, resume.user_id, _rows(RESUME_TEXT))

    stored = await chunks.list_for_resume(resume.id)
    assert [chunk.ordinal for chunk in stored] == list(range(len(stored)))
    assert "EDUCATION" in [chunk.section for chunk in stored]
    # Retrieval can now be explained from SQL rather than by querying Chroma.
    assert any("University of Leeds" in chunk.content for chunk in stored)


async def test_reindexing_replaces_rather_than_accumulates(resume, chunks):
    """Re-chunking can produce *fewer* pieces than last time, and an
    update-in-place would leave the tail of the previous run behind as rows
    matching no part of the document."""
    await chunks.replace_for_resume(resume.id, resume.user_id, _rows(RESUME_TEXT))
    first = await chunks.list_for_resume(resume.id)

    await chunks.replace_for_resume(resume.id, resume.user_id, _rows("SKILLS\nGo\n"))

    remaining = await chunks.list_for_resume(resume.id)
    assert len(first) > len(remaining)
    assert [chunk.ordinal for chunk in remaining] == list(range(len(remaining)))


async def test_unembedded_chunks_are_visible_as_such(resume, chunks):
    """The durable version of the produced-versus-embedded gap: a row with
    content and no embedded_at is a piece of the resume retrieval cannot see."""
    rows = _rows(RESUME_TEXT)
    await chunks.replace_for_resume(resume.id, resume.user_id, rows)

    # As if the provider failed partway through the batch.
    await chunks.mark_embedded(resume.id, [0, 1])

    assert await chunks.count_unembedded(resume.id) == len(rows) - 2
    stored = await chunks.list_for_resume(resume.id)
    assert stored[0].embedded_at is not None
    assert stored[-1].embedded_at is None


async def test_marking_nothing_embedded_is_not_an_error(resume, chunks):
    """The batch failed entirely; every row stays unembedded."""
    await chunks.replace_for_resume(resume.id, resume.user_id, _rows(RESUME_TEXT))

    assert await chunks.mark_embedded(resume.id, []) == 0


async def test_deleting_a_resume_takes_its_chunks(resume, chunks, db_session):
    """A cascade, not application code: the chunks hold resume text, and an
    orphaned copy of a deleted resume is a data-retention problem."""
    await chunks.replace_for_resume(resume.id, resume.user_id, _rows(RESUME_TEXT))

    await db_session.delete(resume)
    await db_session.flush()

    remaining = (
        await db_session.execute(
            select(ResumeChunk).where(ResumeChunk.resume_id == resume.id)
        )
    ).scalars().all()
    assert remaining == []


DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx_bytes(text: str) -> bytes:
    """A real DOCX carrying real text.

    DOCX rather than PDF because the suite's PDF helper writes a blank page,
    and this test needs a document that parses into something chunkable.
    """
    import io

    from docx import Document

    document = Document()
    for line in text.split("\n"):
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


async def test_uploading_a_resume_through_the_api_stores_its_chunks(
    api, registered_user, storage_root, db_session
):
    """The wiring, end to end: upload -> parse -> chunk -> rows.

    With no Gemini key the API fixture leaves RAG off, so nothing is embedded --
    which is exactly the case worth pinning. The chunks are still saved, so
    switching retrieval on later is a re-index rather than a re-upload, which
    is the whole reason the text lives in Postgres now.
    """
    response = await api.post(
        "/api/v1/resumes",
        files={"file": ("cv.docx", _docx_bytes(RESUME_TEXT), DOCX_TYPE)},
        headers=registered_user["headers"],
    )
    assert response.status_code == 201, response.text

    resume_id = uuid.UUID(response.json()["id"])
    stored = await ResumeChunkRepository(db_session).list_for_resume(resume_id)
    assert stored, "upload stored no chunks"
    assert "EDUCATION" in [chunk.section for chunk in stored]
    assert all(chunk.embedded_at is None for chunk in stored)
