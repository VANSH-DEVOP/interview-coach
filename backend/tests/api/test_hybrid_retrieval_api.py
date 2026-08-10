"""Keyword search against a real Postgres, and the two halves fused.

`ts_rank`, the generated tsvector column and the OR-of-terms query are all
Postgres behaviour, so a fake proves nothing about them. The properties here
are the ones that would fail silently: a query returning everything, a query
returning nothing when it should match, and the two halves formatting chunk
text differently so fusion never recognises agreement.
"""

import uuid

import pytest

from app.models.resume import Resume
from app.models.resume_chunk import ResumeChunk
from app.repositories.resume_chunk_repository import ResumeChunkRepository, _to_tsquery
from app.services.ai import retrieval_metrics
from app.services.ai.rag import ResumeChunker
from app.services.ai.retrieval import HybridRetriever

RESUME_TEXT = """\
Priya Raman
Senior Backend Engineer

EXPERIENCE
Staff Engineer, Meridian Payments (2021-2026)
Rebuilt the settlement ledger on PostgreSQL with idempotent writes.

Senior Engineer, Northwind Logistics (2018-2021)
Built an event pipeline on Kafka moving four million shipment updates a day.
Introduced gRPC between the routing and dispatch services.

EDUCATION
BSc Computer Science, University of Pune

SKILLS
Python, Go, PostgreSQL, Kafka, Kubernetes, Terraform
"""


@pytest.fixture(autouse=True)
def _clean_metrics():
    retrieval_metrics.reset()
    yield
    retrieval_metrics.reset()


@pytest.fixture
async def indexed(db_session, registered_user):
    """A resume whose chunks are rows. Returns (repository, resume_id)."""
    resume = Resume(
        user_id=uuid.UUID(registered_user["user"]["id"]),
        file_name="cv.pdf",
        storage_key="resumes/u/cv.pdf",
        content_type="application/pdf",
        size_bytes=1,
        parsed_text=RESUME_TEXT,
    )
    db_session.add(resume)
    await db_session.flush()

    chunks = ResumeChunkRepository(db_session)
    await chunks.replace_for_resume(
        resume.id,
        resume.user_id,
        [
            ResumeChunk(ordinal=c.ordinal, section=c.section, content=c.content)
            for c in ResumeChunker().chunk(RESUME_TEXT)
        ],
    )
    return chunks, resume.id


# -- The tsquery ---------------------------------------------------------------


def test_terms_are_ored_not_anded():
    """`plainto_tsquery` ANDs every term, so "skills and experience relevant to
    Senior Backend Engineer" would match only a chunk containing all of them --
    which is no chunk. Retrieval wants ranking by how much matched."""
    assert _to_tsquery("kafka event pipeline") == "kafka | event | pipeline"


def test_punctuation_that_would_break_to_tsquery_is_stripped():
    """A candidate's answer reaches this. `to_tsquery` has a syntax and raises
    on a stray operator, which would turn a typo into a 500."""
    assert _to_tsquery("what about (a & b) | c?") == "what | about"


def test_a_query_with_no_usable_terms_is_none():
    assert _to_tsquery("   ?  !") is None
    assert _to_tsquery("") is None


def test_technical_tokens_survive():
    """C++, C#, .NET and version numbers are exactly the terms dense retrieval
    is worst at, so the keyword half must not discard them."""
    assert _to_tsquery("C++ and .NET 8.0") == "C++ | and | .NET | 8.0"


# -- Keyword search ------------------------------------------------------------


async def test_search_finds_the_chunk_holding_the_exact_term(indexed):
    chunks, resume_id = indexed

    results = await chunks.search(resume_id, "gRPC routing dispatch")

    assert results
    assert "gRPC" in results[0][0].content


async def test_a_query_matching_nothing_returns_nothing(indexed):
    """The property dense retrieval cannot provide: chunks matching no term are
    absent, rather than returned with a low score. This is what lets a query
    about nothing retrieve nothing instead of padding the prompt."""
    chunks, resume_id = indexed

    assert await chunks.search(resume_id, "underwater basketweaving macrame") == []


async def test_the_section_heading_is_searchable(indexed):
    """"education" as a query term has to reach the EDUCATION block even though
    the word appears nowhere in the text under it."""
    chunks, resume_id = indexed

    results = await chunks.search(resume_id, "education")

    assert results
    assert results[0][0].section == "EDUCATION"


async def test_search_is_scoped_to_one_resume(db_session, indexed, registered_user):
    """A leak here is a privacy incident, not a ranking problem."""
    chunks, mine = indexed
    other = Resume(
        user_id=uuid.UUID(registered_user["user"]["id"]),
        file_name="other.pdf",
        storage_key="resumes/u/other.pdf",
        content_type="application/pdf",
        size_bytes=1,
        parsed_text="SKILLS\nHaskell, Erlang\n",
    )
    db_session.add(other)
    await db_session.flush()
    await chunks.replace_for_resume(
        other.id, other.user_id, [ResumeChunk(ordinal=0, section="SKILLS", content="Haskell, Erlang")]
    )

    results = await chunks.search(mine, "Haskell Erlang")

    assert results == []


async def test_results_are_ranked_not_merely_filtered(indexed):
    """Every chunk mentions something; the one mentioning more of the query has
    to come first, or the keyword half contributes noise to fusion."""
    chunks, resume_id = indexed

    results = await chunks.search(resume_id, "Kafka shipment updates pipeline")

    assert "Kafka" in results[0][0].content
    assert results[0][1] > 0


async def test_the_generated_column_tracks_edits(indexed, db_session):
    """Postgres maintains the tsvector; nothing in the application writes it.
    A chunk written by a migration or by psql is searchable too."""
    chunks, resume_id = indexed
    stored = await chunks.list_for_resume(resume_id)
    stored[0].content = "Rewrote everything in Haskell"
    await db_session.flush()

    results = await chunks.search(resume_id, "Haskell")

    assert results
    assert "Haskell" in results[0][0].content


# -- The two halves together ---------------------------------------------------


class _DenseStub:
    """Stands in for the vector half, which needs a provider."""

    def __init__(self, documents):
        self.documents = documents

    async def retrieve_ranked(self, resume_id, query, top_k=5, *, redactor=None, max_distance=None):
        return self.documents


async def test_the_two_halves_agree_on_chunk_text(indexed):
    """The quietest possible bug in this design.

    Fusion matches candidates *by text*: the dense half returns what Chroma
    stored at index time, the keyword half rebuilds it from the section and
    content columns. If those two formattings differ by a single newline, no
    chunk is ever recognised as found by both -- rank fusion still returns a
    list, so the only visible symptom is `agreed` sitting at zero forever.
    """
    chunks, resume_id = indexed
    stored = await chunks.list_for_resume(resume_id)
    # Exactly what indexing embedded, taken from the chunker's own output.
    embedded = {chunk.retrieval_text for chunk in ResumeChunker().chunk(RESUME_TEXT)}

    assert {chunk.retrieval_text for chunk in stored} == embedded

    # Hand the dense half the same chunk the keyword half will find, so the
    # only thing that can stop them agreeing is a formatting difference.
    grpc_chunk = next(chunk for chunk in stored if "gRPC" in chunk.content)
    retriever = HybridRetriever(_DenseStub([grpc_chunk.retrieval_text]), chunks)
    await retriever.retrieve_context(resume_id, "gRPC routing dispatch")

    assert retrieval_metrics.snapshot()["agreed"] >= 1


async def test_keyword_results_reach_the_prompt_when_dense_finds_nothing(indexed):
    """The case hybrid retrieval exists for: an exact term the embedding missed."""
    chunks, resume_id = indexed
    retriever = HybridRetriever(_DenseStub([]), chunks)

    context = await retriever.retrieve_context(resume_id, "Terraform Kubernetes")

    assert "Terraform" in context
    assert retrieval_metrics.snapshot()["sparse_only"] == 1
