"""A fixed retrieval benchmark, so "better retrieval" can be shown rather than asserted.

Parts 2-5 of the RAG work (structure-aware chunking, hybrid search, caching,
query rewriting) all claim to improve retrieval. Without a fixed resume, a
fixed set of queries and a number, every one of those claims is untestable and
the only feedback available is whether the questions *feel* better.

**What this does and does not prove.** The embeddings here are deterministic
and lexical -- a hashed bag of words -- so cosine similarity is term overlap
and nothing more. That is enough to pin the machinery end to end (chunk ->
embed -> store -> filter by resume -> rank -> assemble context) and to compare
ranking changes reproducibly in CI with no provider and no quota. It says
nothing about *semantic* quality: a real embedding model matches "distributed
systems" to "Kafka" and this cannot. Judging that needs
`test_rag_pipeline.py` and a live key.

The gap is the point of Part 3. A lexical scorer is exactly what BM25 is, so
the numbers below are close to the sparse half of the hybrid retriever -- and
the queries that score badly here are the ones dense retrieval is supposed to
rescue.
"""

import hashlib
import math
import re
import uuid

import pytest

from app.services.ai import retrieval_metrics
from app.services.ai.rag import RAGService, TextChunker
from app.services.ai.vector_store import ChromaVectorStore

_DIMENSIONS = 512


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


class LexicalEmbeddings:
    """Deterministic hashed bag-of-words vectors.

    The hashing trick: each token lands in a fixed dimension, counts are
    L2-normalised, so the cosine between two texts is their weighted term
    overlap. No network, no key, identical on every machine.
    """

    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def _bucket(token: str) -> int:
        # blake2b, not the builtin hash(): PYTHONHASHSEED randomises str hashing
        # per process, so builtin hashing would give a different set of
        # collisions on every run and make the scores below drift for no reason.
        digest = hashlib.blake2b(token.encode(), digest_size=4).digest()
        return int.from_bytes(digest, "big") % _DIMENSIONS

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * _DIMENSIONS
        for token in _tokens(text):
            vector[self._bucket(token)] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            # Chroma rejects an all-zero vector under cosine space, and a chunk
            # of pure punctuation is not retrievable anyway.
            vector[0] = 1.0
            norm = 1.0
        return [value / norm for value in vector]

    async def embed_text(self, text: str, *, redactor=None) -> list[float]:
        self.calls += 1
        return self._vector(text)

    async def embed_batch(self, texts: list[str], *, redactor=None) -> list[list[float]]:
        return [await self.embed_text(text) for text in texts]


# Long enough to chunk into something retrieval can actually discriminate
# between. The first draft of this fixture was a short resume that collapsed
# into two chunks, where every query scored a perfect hit by having nowhere
# else to go -- a benchmark that cannot fail is not one.
RESUME = """\
Priya Raman
Senior Backend Engineer, Bengaluru

SUMMARY
Backend engineer with eight years building payment and messaging systems at
scale. Comfortable owning a service from schema design through on-call.

EXPERIENCE

Staff Engineer, Meridian Payments (2021-2026)
Rebuilt the settlement ledger on PostgreSQL with idempotent writes keyed on a
provider reference, which removed a class of double-payment incidents that had
recurred for two years.
Cut nightly reconciliation from six hours to eleven minutes by replacing a
row-by-row comparison with a windowed aggregate.
Owned the on-call rotation for the payments platform, including the runbook
rewrite after a nine-hour outage caused by connection-pool exhaustion.

Senior Engineer, Northwind Logistics (2018-2021)
Built an event pipeline on Kafka moving four million shipment updates a day,
with exactly-once delivery into the warehouse via transactional outbox.
Introduced gRPC between the routing and dispatch services, cutting p99 latency
from 340ms to 45ms and replacing a hand-rolled JSON contract.
Ran the migration off a monolithic Rails application, service by service, with
no scheduled downtime across fourteen months.

Engineer, Calico Systems (2016-2018)
Maintained an internal billing service in Django and wrote its first test
suite, taking coverage from nothing to roughly seventy percent.

EDUCATION
BSc Computer Science, University of Pune, 2012-2016
Thesis on compiler optimisation for embedded targets, focused on loop unrolling
under tight instruction-cache budgets. Graduated with distinction.

SKILLS
Languages: Python, Go, Java, SQL
Data: PostgreSQL, Kafka, Redis, ClickHouse
Infrastructure: Kubernetes, Terraform, AWS, Docker
Practices: trunk-based development, property-based testing, incident review

PROJECTS
Ledgerkit, an open-source double-entry bookkeeping library in Go with about
1200 stars, used by three payment startups.
A static analyser for Django migrations that flags operations taking an ACCESS
EXCLUSIVE lock on large tables.

CERTIFICATIONS
Certified Kubernetes Administrator, 2023
AWS Solutions Architect Associate, 2021

LANGUAGES
English (fluent), Tamil (native), German (conversational)
"""

# (query, a phrase that must appear in the chunk we consider correct)
#
# Queries that share words with their target. A term-overlap scorer should get
# these right every time, so they measure the machinery -- chunking, storage,
# filtering, ranking -- rather than retrieval intelligence.
LEXICAL_QUERIES = [
    ("kafka event pipeline shipment updates", "Kafka moving four million"),
    ("settlement ledger reconciliation idempotent writes", "settlement ledger"),
    ("university degree computer science thesis", "University of Pune"),
    ("kubernetes terraform aws certification", "Certified Kubernetes Administrator"),
    ("open source library go double entry bookkeeping", "Ledgerkit"),
    ("grpc latency between routing and dispatch", "gRPC"),
]

# The same six facts asked the way an interviewer would ask them, sharing few
# or no words with the resume. "Distributed streaming" has to reach the Kafka
# chunk; "container orchestration" has to reach Kubernetes. Term overlap cannot
# do this, and that is the entire case for the rest of the RAG work.
SEMANTIC_QUERIES = [
    ("experience with distributed streaming systems", "Kafka moving four million"),
    ("database schema design and safe migrations", "settlement ledger"),
    ("container orchestration experience", "Certified Kubernetes Administrator"),
    ("has this candidate published any of their own work", "Ledgerkit"),
    ("what did they study at university", "University of Pune"),
    ("experience reducing service latency", "gRPC"),
]

# Measured, not aspirational: what this pipeline scores today. Assertions are
# floors, so a regression fails and an improvement asks to have the number
# raised.
#
# The lexical row is a machinery check and should stay pinned at 1.0. The
# semantic row is the one Part 3 exists to move -- and note it is a *lower*
# bound on the real system, since a real embedding model handles paraphrase
# and this stand-in cannot. It measures roughly what the sparse half of a
# hybrid retriever would contribute.
BASELINE = {
    "lexical": {"recall@3": 1.0, "precision@1": 1.0},
    "semantic": {"recall@3": 0.5, "precision@1": 0.16},
}


@pytest.fixture(autouse=True)
def _clean_state():
    retrieval_metrics.reset()
    yield
    retrieval_metrics.reset()


@pytest.fixture
def store() -> ChromaVectorStore:
    """An in-memory Chroma, so this is the real store rather than a fake of it.

    Its own collection per test: chromadb keeps one in-memory system per
    settings for the life of the process, so every `EphemeralClient()` shares
    the default collection. `test_rag_pipeline.py` puts 768-dimension vectors
    in it, these are 512, and whichever ran second failed with a dimension
    mismatch -- in the full suite only, which is the worst way to find out.
    """
    return ChromaVectorStore(None, collection_name=f"eval-{uuid.uuid4()}")


@pytest.fixture
async def indexed(store):
    """The fixture resume, indexed. Returns (rag, resume_id, embeddings)."""
    embeddings = LexicalEmbeddings()
    rag = RAGService(embeddings, store)
    resume_id, user_id = uuid.uuid4(), uuid.uuid4()
    await rag.index_resume(resume_id, user_id, RESUME)
    return rag, resume_id, embeddings


# -- The benchmark -------------------------------------------------------------


async def _score(rag, resume_id, queries) -> dict[str, float]:
    found_at_3 = correct_at_1 = 0
    for query, expected in queries:
        chunks = await _ranked_chunks(rag, resume_id, query, top_k=3)
        if any(expected in chunk for chunk in chunks):
            found_at_3 += 1
        if chunks and expected in chunks[0]:
            correct_at_1 += 1
    return {
        "recall@3": found_at_3 / len(queries),
        "precision@1": correct_at_1 / len(queries),
    }


@pytest.mark.parametrize("kind", ["lexical", "semantic"])
async def test_retrieval_meets_the_recorded_baseline(indexed, capsys, kind):
    """Recall@3 and precision@1 over the fixture queries.

    Printed as well as asserted: when a later part changes retrieval, these two
    numbers are what says whether it helped, and by how much.
    """
    rag, resume_id, _ = indexed
    queries = LEXICAL_QUERIES if kind == "lexical" else SEMANTIC_QUERIES

    scores = await _score(rag, resume_id, queries)

    with capsys.disabled():
        print(
            f"\n  retrieval [{kind:8}] recall@3={scores['recall@3']:.2f} "
            f"precision@1={scores['precision@1']:.2f} over {len(queries)} queries"
        )

    for metric, floor in BASELINE[kind].items():
        assert scores[metric] >= floor, f"{kind} {metric} regressed"


async def _ranked_chunks(rag, resume_id, query, top_k=3) -> list[str]:
    """Retrieved chunks in rank order.

    `retrieve_context` returns one joined string, which is all the generator
    needs and not enough to score ranking, so this goes through the store.
    """
    embedding = await rag._embedding_service.embed_text(query)
    result = await rag._vector_store.retrieve_relevant(embedding, resume_id, top_k=top_k)
    return result.documents


# -- Machinery the later parts must not break ----------------------------------


async def test_indexing_covers_the_whole_resume(indexed):
    rag, resume_id, _ = indexed

    state = retrieval_metrics.snapshot()
    assert state["chunks_produced"] > 1
    # Every chunk embedded, or retrieval is answering from a partial resume.
    assert state["chunks_embedded"] == state["chunks_produced"]


async def test_retrieval_is_scoped_to_one_resume(store):
    """Two candidates in one collection. A leak here is a privacy incident, not
    a quality problem, so it is pinned against the real store rather than a fake."""
    embeddings = LexicalEmbeddings()
    rag = RAGService(embeddings, store)
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    await rag.index_resume(mine, uuid.uuid4(), RESUME)
    await rag.index_resume(
        theirs, uuid.uuid4(), "SKILLS\nHaskell, Erlang, OCaml, Prolog\n"
    )

    context = await rag.retrieve_context(mine, "haskell erlang ocaml prolog")

    assert "Haskell" not in context


async def test_an_unindexed_resume_retrieves_nothing_rather_than_anything(indexed):
    """A resume uploaded before indexing worked, queried against a live index."""
    rag, _, _ = indexed

    context = await rag.retrieve_context(uuid.uuid4(), "kafka")

    assert context == ""
    assert retrieval_metrics.snapshot()["empty"] == 1


# -- Known warts, recorded so Part 2 can show they are gone --------------------


def test_chunk_overlap_duplicates_text_verbatim():
    """The current chunker fakes overlap by prepending the previous chunk's
    last 100 characters behind a "\\n...\\n" marker, which retrieval then strips
    out. So the same sentences are embedded twice and can be retrieved twice,
    spending quota and prompt budget on a copy.

    Recorded as a test rather than a comment: Part 2 replaces this, and the
    replacement should make this test fail and be deleted.
    """
    chunks = TextChunker.chunk_text(RESUME)

    assert len(chunks) > 1
    assert any("\n...\n" in chunk for chunk in chunks)


async def test_top_k_pads_the_prompt_with_whatever_is_left(indexed):
    """Retrieval always returns k chunks, however irrelevant.

    A query matching nothing in the resume still fills the prompt with the k
    least-bad chunks, and the caller cannot tell -- `retrieve_context` returns
    a string with no scores. Part 3's relevance threshold is what fixes this;
    until then the behaviour is at least written down.
    """
    rag, resume_id, _ = indexed

    chunks = await _ranked_chunks(rag, resume_id, "underwater basket weaving", top_k=3)

    # Three chunks about payments and Kafka, returned for a query about
    # basket weaving, with nothing in the return value to say they are junk.
    assert len(chunks) == 3
