"""Vector store abstraction for resume embeddings, over LangChain's Chroma integration.

The transport is `langchain_chroma.Chroma`; the seam above it -- `add_resume`,
`retrieve_relevant` returning a `RetrievalResult` of documents and **cosine
distances**, `delete_resume` -- is unchanged, for the same reason `ModelClient`
kept its seam when the provider call moved: everything above this line is
written against it, including the distance cutoff in `RAGService`, rank fusion
in `retrieval.py`, and the retrieval benchmark.

**Embeddings are computed upstream and handed in, not produced here.** That is
the load-bearing detail of this file. `EmbeddingService` redacts per user
(`redactor_for(user.full_name)`) and caches on the redacted text, while this
store is a process-wide singleton -- so binding an embedding function to the
store would either freeze one user's redactor into everybody's indexing, or
need mutable per-request state on a shared object. Neither is acceptable, and
the first is a silent security regression rather than a visible failure.

`Chroma.add_texts` has no parameter for precomputed vectors: it obtains them
only through `self._embedding_function.embed_documents(texts)`. So the vectors
travel through `_PrecomputedEmbeddings`, a courier that is handed the answers
before the question is asked and never computes, caches or redacts anything. It
is constructed per call and thrown away, so no request can see another's
vectors, and redaction stays where `masking.py` put it.
"""

from __future__ import annotations

import logging
import threading
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Set when the collection is first created; ignored on later opens, which is
# why it is a module constant rather than something a caller can vary.
_COLLECTION_METADATA = {
    "hnsw:space": "cosine",
    # How many candidates HNSW keeps while searching. The default (10) makes
    # search approximate, which for a collection of this shape is cost without
    # benefit: chunks are filtered to a single resume, so a query ranks perhaps
    # ten vectors, and scanning ten vectors exhaustively is free. Widening it
    # buys exactness for nothing.
    "hnsw:search_ef": 200,
}


class VectorStoreError(RuntimeError):
    """Raised when vector store operations fail."""


class RetrievalResult:
    """Result from vector store retrieval.

    `distances` are cosine distances -- 0 identical, 1 orthogonal, 2 opposite --
    and lower is better. Callers act on that direction (`RAG_MAX_DISTANCE` is a
    strict `<`), so it must not quietly become a similarity anywhere below.
    """

    def __init__(self, documents: list[str], distances: list[float], metadatas: list[dict[str, Any]] | None = None):
        self.documents = documents
        self.distances = distances
        self.metadatas = metadatas or [{} for _ in documents]


class VectorStore(ABC):
    """Abstract vector store interface."""

    @abstractmethod
    async def add_resume(
        self,
        resume_id: uuid.UUID,
        user_id: uuid.UUID,
        chunks: list[str],
        embeddings: list[list[float]],
    ) -> None:
        """Store resume chunks with embeddings."""
        ...

    @abstractmethod
    async def retrieve_relevant(
        self, query_embedding: list[float], resume_id: uuid.UUID, top_k: int = 5
    ) -> RetrievalResult:
        """Retrieve top-k relevant chunks for a query."""
        ...

    @abstractmethod
    async def delete_resume(self, resume_id: uuid.UUID) -> None:
        """Delete all chunks for a resume."""
        ...


class _PrecomputedEmbeddings:
    """Carries already-computed vectors into `Chroma.add_texts`.

    Duck-typed against `langchain_core.embeddings.Embeddings` rather than
    subclassing it, so this module still imports with no LangChain installed --
    `app.api.deps` imports it transitively, and a missing RAG extra must
    disable RAG, not make the application unimportable. `Chroma` only ever
    calls these two methods on it; it does no isinstance check.

    Keyed by text, not by position: `add_texts` splits its input into
    with-metadata and without-metadata groups before embedding, so the list it
    passes here is not necessarily the list it was given.
    """

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        try:
            return [self._vectors[text] for text in texts]
        except KeyError as exc:  # pragma: no cover - a bug here, not an input error
            raise VectorStoreError(
                "Chroma asked for an embedding this store was not given. "
                "Vectors are computed by EmbeddingService and passed in; this "
                "courier never generates them."
            ) from exc

    def embed_query(self, text: str) -> list[float]:
        """Never called: queries arrive already embedded.

        Raising rather than embedding is deliberate. A silent fallback here
        would be a provider call outside `EmbeddingService`, which means
        outside redaction and outside the embedding cache -- against a quota of
        twenty requests a day.
        """
        raise VectorStoreError(
            "This store does not embed queries; pass a query vector to "
            "retrieve_relevant()."
        )


class ChromaVectorStore(VectorStore):
    """ChromaDB-backed vector store, driven through `langchain_chroma.Chroma`."""

    def __init__(
        self,
        persist_directory: Path | str | None = None,
        *,
        collection_name: str = "resumes",
    ):
        """Initialize ChromaDB client.

        Args:
            persist_directory: Path to persist ChromaDB data. If None, uses in-memory.
            collection_name: Which collection to read and write. Overridden only
                by tests. An in-memory client is *not* private: chromadb keeps
                one system per settings object for the life of the process, so
                every `EphemeralClient()` shares the same collections. Two tests
                indexing vectors of different dimensions into the default name
                is then a hard error from whichever runs second, and which one
                that is depends on collection order.
        """
        # Imported here, not at module scope: chromadb pulls in a heavy dependency
        # tree, and `app.api.deps` imports this module transitively. A module-level
        # import makes the whole app (and the test suite) unimportable when the RAG
        # extra is missing, instead of just disabling RAG.
        try:
            import chromadb
            from langchain_chroma import Chroma
        except ImportError as e:  # pragma: no cover - depends on install extras
            raise VectorStoreError(
                "chromadb and langchain-chroma are not installed; install them "
                'to enable RAG (pip install -e ".[dev]")'
            ) from e

        self._chroma = Chroma
        try:
            if persist_directory:
                self._client: Any = chromadb.PersistentClient(path=str(persist_directory))
            else:
                self._client = chromadb.EphemeralClient()

            self._collection_name = collection_name
            # Reads and deletes need no embedding function -- a query arrives as
            # a vector -- so this handle is built once and reused. Only indexing
            # needs a courier, and that one is per call.
            self._store = self._open(None)
            logger.info("ChromaDB vector store initialized")
        except Exception as e:
            raise VectorStoreError(f"Failed to initialize ChromaDB: {e}") from e

    def _open(self, embeddings: Any) -> Any:
        """A `Chroma` handle over the shared client.

        Cheap: with a client passed in, construction is a `get_or_create_collection`
        lookup, not a database open. The client is the expensive object and
        there is exactly one.
        """
        return self._chroma(
            client=self._client,
            collection_name=self._collection_name,
            embedding_function=embeddings,
            collection_metadata=_COLLECTION_METADATA,
        )

    async def add_resume(
        self,
        resume_id: uuid.UUID,
        user_id: uuid.UUID,
        chunks: list[str],
        embeddings: list[list[float]],
    ) -> None:
        """Store resume chunks with their already-computed embeddings.

        Synchronous underneath, inside an async signature, as it was before:
        the client is in-process, and `Chroma`'s async methods are executor
        wrappers around these same calls. Hopping to a thread here would only
        add a hop -- and it would drag the courier onto another thread with it.
        """
        if len(chunks) != len(embeddings):
            raise VectorStoreError("Chunks and embeddings must have same length")

        if not chunks:
            logger.warning(f"No chunks to store for resume {resume_id}")
            return

        try:
            ids = [f"{resume_id}:chunk:{i}" for i in range(len(chunks))]
            metadatas = [
                {
                    "resume_id": str(resume_id),
                    "user_id": str(user_id),
                    "chunk_index": i,
                }
                for i in range(len(chunks))
            ]

            # `add_texts` upserts, where the raw `collection.add` it replaces
            # errored on an id it had already seen. Re-indexing a resume whose
            # chunk count is unchanged now overwrites in place instead of
            # failing, which matches `replace_for_resume` on the row side.
            self._open(_PrecomputedEmbeddings(dict(zip(chunks, embeddings)))).add_texts(
                texts=chunks,
                metadatas=metadatas,
                ids=ids,
            )
            logger.info(
                f"Added {len(chunks)} chunks for resume {resume_id} to vector store"
            )
        except Exception as e:
            raise VectorStoreError(
                f"Failed to add resume {resume_id} to vector store: {e}"
            ) from e

    async def retrieve_relevant(
        self, query_embedding: list[float], resume_id: uuid.UUID, top_k: int = 5
    ) -> RetrievalResult:
        """Retrieve top-k relevant chunks for a query.

        **The scores this returns are cosine distances, not similarities**,
        despite `similarity_search_by_vector_with_relevance_scores` reading
        otherwise. The method hands back `results["distances"]` from Chroma
        untouched -- its own docstring says "lower score represents more
        similarity" -- and no `relevance_score_fn` is configured, which is the
        only thing that would invert them.

        This matters more than a naming quibble: `RAG_MAX_DISTANCE` is applied
        as a strict `<` in distance space, so a value flipped to a similarity
        here would keep precisely the chunks the cutoff exists to drop, and
        would do it without an error anywhere. If this call is ever changed,
        check the direction against `tests/api/test_retrieval_eval.py`, which
        fails on it.
        """
        try:
            scored = self._store.similarity_search_by_vector_with_relevance_scores(
                embedding=query_embedding,
                k=top_k,
                filter={"resume_id": str(resume_id)},
            )

            if not scored:
                logger.warning(f"No results found for resume {resume_id}")
                return RetrievalResult([], [], [])

            documents = [document.page_content for document, _ in scored]
            distances = [float(distance) for _, distance in scored]
            metadatas = [dict(document.metadata or {}) for document, _ in scored]

            return RetrievalResult(documents, distances, metadatas)
        except Exception as e:
            raise VectorStoreError(
                f"Failed to retrieve from vector store: {e}"
            ) from e

    async def delete_resume(self, resume_id: uuid.UUID) -> None:
        """Delete all chunks for a resume from vector store.

        By metadata filter rather than by id. The ids are derived from the
        chunk count of whichever indexing run wrote them, and a re-chunk can
        produce fewer pieces -- deleting a range this process computed would
        leave the previous run's tail behind, the same trap `replace_for_resume`
        avoids on the row side.
        """
        try:
            # `ids=None` with a `where` kwarg: Chroma forwards the extras
            # straight to `collection.delete`, which is what supports filtering.
            self._store.delete(ids=None, where={"resume_id": str(resume_id)})
            logger.info(f"Deleted chunks for resume {resume_id} from vector store")
        except Exception as e:
            logger.warning(
                f"Failed to delete resume {resume_id} from vector store: {e}"
            )


# Global vector store instance (initialized lazily)
_vector_store: VectorStore | None = None

# Serialises construction, and it is not optional.
#
# `chromadb.PersistentClient` is not safe to build concurrently: two cold calls
# for the same path race on chromadb's own `SharedSystemClient` registry, and
# the losers see a half-started system. Reproduced with four threads and a
# barrier, which produced exactly the three failures seen in production --
# `'RustBindingsAPI' object has no attribute 'bindings'`, `Could not connect to
# tenant default_tenant`, and a `KeyError` on the path.
#
# Worse than a one-off failure: the attempt that fails calls chromadb's
# `_release_system`, which stops and unregisters the system the *winner* is
# using. So one unlucky moment leaves the registry poisoned and every later
# attempt in the process fails too -- which is why RAG stayed off until a
# restart rather than recovering.
#
# FastAPI is what makes this reachable: `get_rag_service` is a sync dependency,
# so it runs in the threadpool, and two requests arriving together at cold
# start land in two threads. `functools.lru_cache` does not help -- it prevents
# repeat work after a call returns, not two threads entering the body at once.
_vector_store_lock = threading.Lock()


def get_vector_store(persist_directory: Path | str | None = None) -> VectorStore:
    """Get or create the global vector store instance.

    Args:
        persist_directory: Path for ChromaDB persistence.

    Returns:
        Singleton VectorStore instance.
    """
    global _vector_store
    # Read first, without the lock: after construction this is the hot path and
    # every retrieval would otherwise queue behind a mutex it never needs.
    if _vector_store is not None:
        return _vector_store

    with _vector_store_lock:
        # Checked again inside the lock: the thread that waited here may have
        # been waiting for the one that built it.
        if _vector_store is None:
            _vector_store = ChromaVectorStore(persist_directory)
    return _vector_store
