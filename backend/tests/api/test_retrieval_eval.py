"""A fixed retrieval benchmark, so "better retrieval" can be shown rather than asserted.

Parts 2-5 of the RAG work (structure-aware chunking, hybrid search, caching,
query rewriting) all claim to improve retrieval. Without a fixed resume, a
fixed set of queries and a number, every one of those claims is untestable and
the only feedback available is whether the questions *feel* better.

Runs the real pipeline: real chunker, real (in-memory) Chroma, real Postgres
full-text search, real rank fusion. Only the embedding model is a stand-in.

**What this does and does not prove.** The embeddings here are deterministic
and lexical -- a hashed bag of words -- so cosine similarity is term overlap
and nothing more. That is enough to pin the machinery end to end and to
compare ranking changes reproducibly in CI with no provider and no quota.

It is *not* enough to judge semantic quality, and the limit bites hardest
exactly here in part 3: with a lexical stand-in, **both halves of the hybrid
retriever are lexical**, so fusion cannot show the gain it exists for -- a real
embedding model matching "distributed streaming" to a paragraph about Kafka.
What the numbers below do show is that keyword search rescues queries the
(lossy, hashed) dense half ranks badly, that fusion does not regress anything,
and that a query about nothing now retrieves nothing. The semantic gain needs
`test_rag_pipeline.py` and a live key.
"""

import hashlib
import math
import re
import uuid

import pytest

from app.core.config import get_settings
from app.models.resume import Resume
from app.models.resume_chunk import ResumeChunk
from app.repositories.resume_chunk_repository import ResumeChunkRepository
from app.services.ai import retrieval_metrics
from app.services.ai.rag import RAGService, ResumeChunker
from app.services.ai.retrieval import HybridRetriever
from app.services.ai.vector_store import ChromaVectorStore

# The fixture resume has ~229 distinct tokens. At the 512 this started as,
# collisions in the hashing trick were frequent enough to *decide* rankings:
# a 46-character chunk that shared one colliding bucket with the query
# outscored the paragraph that actually answered it, and the semantic score
# swung between 0.33 and 0.67 purely with the dimension count. 4096 buckets
# for 229 tokens makes collisions rare, so the numbers measure retrieval
# rather than the stand-in embedder's arithmetic.
_DIMENSIONS = 4096


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
#
# Asserted against the *hybrid* row, which is what a request actually uses.
# Reproducible across processes; the dense and sparse rows are printed for
# attribution when a number moves.
#
# History, because two of these figures were wrong when recorded:
#
#   part 1  semantic r@3 0.50 / p@1 0.17   measured at 512 hash dimensions,
#                                          where collisions decided rankings
#   part 2  semantic r@3 0.67 / p@1 0.17   a lucky run: without a distance
#                                          cutoff, several chunks tie at
#                                          distance 1.0 and which one lands in
#                                          the top 3 varies per process, so the
#                                          same code scored 0.50 or 0.67
#   part 3  semantic r@3 0.50 / p@1 0.33   stable, and precision doubled
#
# Recall@3 did not move. Precision@1 did, which is the half hybrid retrieval
# was expected to help with here -- and a query matching nothing now retrieves
# nothing rather than three near-random paragraphs.
BASELINE = {
    "lexical": {"recall@3": 1.0, "precision@1": 1.0},
    "semantic": {"recall@3": 0.5, "precision@1": 0.33},
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
    in it, these are 4096, and whichever ran second failed with a dimension
    mismatch -- in the full suite only, which is the worst way to find out.
    """
    return ChromaVectorStore(None, collection_name=f"eval-{uuid.uuid4()}")


@pytest.fixture
async def indexed(store, db_session, registered_user):
    """The fixture resume through the whole pipeline.

    Chunks in Postgres for keyword search, vectors in Chroma for dense search,
    both from the same chunker call -- which is also what makes the two halves
    agree on chunk text and lets fusion recognise a chunk found by both.

    Returns (dense, sparse, hybrid, resume_id).
    """
    chunks = ResumeChunker().chunk(RESUME)

    resume = Resume(
        user_id=uuid.UUID(registered_user["user"]["id"]),
        file_name="cv.pdf",
        storage_key="resumes/u/cv.pdf",
        content_type="application/pdf",
        size_bytes=1,
        parsed_text=RESUME,
    )
    db_session.add(resume)
    await db_session.flush()

    sparse = ResumeChunkRepository(db_session)
    await sparse.replace_for_resume(
        resume.id,
        resume.user_id,
        [
            ResumeChunk(ordinal=c.ordinal, section=c.section, content=c.content)
            for c in chunks
        ],
    )

    dense = RAGService(LexicalEmbeddings(), store)
    await dense.index_chunks(resume.id, resume.user_id, chunks)

    return dense, sparse, HybridRetriever(dense, sparse), resume.id


# -- The benchmark -------------------------------------------------------------


async def _ranked(retriever, kind, resume_id, query, top_k=3) -> list[str]:
    """The top chunks one retriever returns, in rank order.

    The dense probe applies the same distance cutoff the pipeline does. Without
    it this measurement is not reproducible: a paraphrased query leaves several
    chunks at cosine distance exactly 1.0 -- sharing nothing with it -- and
    which of those ties lands in the top 3 varies between processes. That is
    what made the semantic score flip between 0.50 and 0.67 on identical code.
    """
    if kind == "dense":
        return await retriever.retrieve_ranked(
            resume_id, query, top_k=top_k, max_distance=get_settings().RAG_MAX_DISTANCE
        )
    if kind == "sparse":
        matches = await retriever.search(resume_id, query, limit=top_k)
        return [chunk.retrieval_text for chunk, _ in matches]
    scored = await retriever.retrieve_scored(resume_id, query, top_k=top_k)
    return [candidate.text for candidate in scored]


async def _score(retriever, kind, resume_id, queries) -> dict[str, float]:
    found_at_3 = correct_at_1 = 0
    for query, expected in queries:
        chunks = await _ranked(retriever, kind, resume_id, query)
        if any(expected in chunk for chunk in chunks):
            found_at_3 += 1
        if chunks and expected in chunks[0]:
            correct_at_1 += 1
    return {
        "recall@3": found_at_3 / len(queries),
        "precision@1": correct_at_1 / len(queries),
    }


@pytest.mark.parametrize("tier", ["lexical", "semantic"])
async def test_retrieval_meets_the_recorded_baseline(indexed, capsys, tier):
    """Recall@3 and precision@1 for each retriever, over the fixture queries.

    All three are printed, not just the one being asserted: when a later part
    changes retrieval, the interesting question is usually which half moved.
    """
    dense, sparse, hybrid, resume_id = indexed
    queries = LEXICAL_QUERIES if tier == "lexical" else SEMANTIC_QUERIES

    scores = {}
    for kind, retriever in (("dense", dense), ("sparse", sparse), ("hybrid", hybrid)):
        scores[kind] = await _score(retriever, kind, resume_id, queries)

    with capsys.disabled():
        print(f"\n  [{tier}]")
        for kind, result in scores.items():
            print(
                f"    {kind:7} recall@3={result['recall@3']:.2f} "
                f"precision@1={result['precision@1']:.2f}"
            )

    for metric, floor in BASELINE[tier].items():
        assert scores["hybrid"][metric] >= floor, f"{tier} {metric} regressed"


# -- Machinery the later parts must not break ----------------------------------


async def test_indexing_covers_the_whole_resume(indexed):
    state = retrieval_metrics.snapshot()

    assert state["chunks_produced"] > 1
    # Every chunk embedded, or retrieval is answering from a partial resume.
    assert state["chunks_embedded"] == state["chunks_produced"]


async def test_retrieval_is_scoped_to_one_resume(store):
    """Two candidates in one collection. A leak here is a privacy incident, not
    a quality problem, so it is pinned against the real store rather than a fake."""
    rag = RAGService(LexicalEmbeddings(), store)
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    chunker = ResumeChunker()
    await rag.index_chunks(mine, uuid.uuid4(), chunker.chunk(RESUME))
    await rag.index_chunks(
        theirs, uuid.uuid4(), chunker.chunk("SKILLS\nHaskell, Erlang, OCaml, Prolog\n")
    )

    context = await rag.retrieve_context(mine, "haskell erlang ocaml prolog")

    assert "Haskell" not in context


async def test_an_unindexed_resume_retrieves_nothing_rather_than_anything(indexed):
    """A resume uploaded before indexing worked, queried against a live index."""
    _, _, hybrid, _ = indexed

    assert await hybrid.retrieve_context(uuid.uuid4(), "kafka") == ""


# -- What part 3 changed -------------------------------------------------------


async def test_a_query_sharing_nothing_with_the_resume_retrieves_nothing(indexed):
    """Part 1 recorded the opposite as a wart: `top_k` always returned k chunks,
    so a query about basket weaving filled the prompt with the k least-bad
    paragraphs of a payments resume. Keyword search contributes nothing for a
    query matching no term, and the distance cutoff drops dense results that
    are no better than orthogonal.
    """
    _, _, hybrid, resume_id = indexed

    scored = await hybrid.retrieve_scored(resume_id, "zzzq xqjv wkbp", top_k=3)

    assert [candidate.text for candidate in scored] == []


async def test_a_weakly_related_query_is_no_longer_padded_to_k(indexed):
    """The cutoff trims the tail; it does not promise an empty result.

    "underwater basket weaving" shares no *meaning* with this resume, but the
    stand-in embedder still puts one chunk at distance 0.91 through a hash
    collision, and a real embedding model likewise returns non-zero similarity
    for unrelated text. So the honest claim is the weaker one: the prompt stops
    being padded to k with chunks nothing wanted.
    """
    _, _, hybrid, resume_id = indexed

    scored = await hybrid.retrieve_scored(resume_id, "underwater basket weaving", top_k=3)

    assert len(scored) < 3


async def test_the_caller_can_see_which_half_found_what(indexed):
    """The other half of that wart: the return value now carries ranks, so a
    weak result is distinguishable from a strong one without guessing."""
    _, _, hybrid, resume_id = indexed

    scored = await hybrid.retrieve_scored(resume_id, "Kafka shipment updates", top_k=3)

    assert scored
    assert all(candidate.sources in {"dense", "sparse", "both"} for candidate in scored)
    assert scored[0].score > 0
