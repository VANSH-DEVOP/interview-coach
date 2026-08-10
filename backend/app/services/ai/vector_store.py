"""Vector store abstraction for resume embeddings using ChromaDB."""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)


class VectorStoreError(RuntimeError):
    """Raised when vector store operations fail."""


class RetrievalResult:
    """Result from vector store retrieval."""

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


class ChromaVectorStore(VectorStore):
    """ChromaDB-backed vector store for resume embeddings."""

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
        except ImportError as e:  # pragma: no cover - depends on install extras
            raise VectorStoreError(
                "chromadb is not installed; install it to enable RAG "
                '(pip install -e ".[dev]")'
            ) from e

        try:
            if persist_directory:
                # Use new Chroma client with persistent storage
                self._client = chromadb.PersistentClient(path=str(persist_directory))
            else:
                # Use ephemeral in-memory client
                self._client = chromadb.EphemeralClient()

            # Get or create collection for resumes
            self._collection = self._client.get_or_create_collection(
                name=collection_name,
                metadata={
                    "hnsw:space": "cosine",
                    # How many candidates HNSW keeps while searching. The
                    # default (10) makes search approximate, which for a
                    # collection of this shape is cost without benefit: chunks
                    # are filtered to a single resume, so a query ranks perhaps
                    # ten vectors, and scanning ten vectors exhaustively is
                    # free. Widening it buys exactness for nothing.
                    "hnsw:search_ef": 200,
                },
            )
            logger.info("ChromaDB vector store initialized")
        except Exception as e:
            raise VectorStoreError(f"Failed to initialize ChromaDB: {e}") from e

    async def add_resume(
        self,
        resume_id: uuid.UUID,
        user_id: uuid.UUID,
        chunks: list[str],
        embeddings: list[list[float]],
    ) -> None:
        """Store resume chunks with embeddings in ChromaDB."""
        if len(chunks) != len(embeddings):
            raise VectorStoreError("Chunks and embeddings must have same length")

        if not chunks:
            logger.warning(f"No chunks to store for resume {resume_id}")
            return

        try:
            # Create unique IDs for each chunk
            ids = [f"{resume_id}:chunk:{i}" for i in range(len(chunks))]
            metadatas = [
                {
                    "resume_id": str(resume_id),
                    "user_id": str(user_id),
                    "chunk_index": i,
                }
                for i in range(len(chunks))
            ]

            # chromadb's stubs declare numpy-flavoured types for embeddings and
            # a narrower Mapping for metadatas than the runtime accepts; plain
            # lists and dicts are the documented input. Cast rather than
            # contort the call site around the stubs.
            self._collection.add(
                ids=ids,
                embeddings=cast(Any, embeddings),
                documents=chunks,
                metadatas=cast(Any, metadatas),
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
        """Retrieve top-k relevant chunks for a query."""
        try:
            results = self._collection.query(
                query_embeddings=cast(Any, [query_embedding]),
                n_results=top_k,
                where={"resume_id": str(resume_id)},
            )

            # Every key in the result is optional and each value is a list of
            # per-query lists. Unpack defensively: an absent key and an empty
            # result both mean "nothing retrieved".
            raw_documents = results.get("documents") or []
            raw_distances = results.get("distances") or []
            raw_metadatas = results.get("metadatas") or []

            if not raw_documents or not raw_documents[0]:
                logger.warning(f"No results found for resume {resume_id}")
                return RetrievalResult([], [], [])

            documents = [str(doc) for doc in raw_documents[0]]
            distances = [float(d) for d in raw_distances[0]] if raw_distances else []
            metadatas = (
                [dict(meta) for meta in raw_metadatas[0]] if raw_metadatas else []
            )

            return RetrievalResult(documents, distances, metadatas)
        except Exception as e:
            raise VectorStoreError(
                f"Failed to retrieve from vector store: {e}"
            ) from e

    async def delete_resume(self, resume_id: uuid.UUID) -> None:
        """Delete all chunks for a resume from vector store."""
        try:
            # ChromaDB delete requires passing IDs or a where filter
            self._collection.delete(
                where={"resume_id": str(resume_id)}
            )
            logger.info(f"Deleted chunks for resume {resume_id} from vector store")
        except Exception as e:
            logger.warning(
                f"Failed to delete resume {resume_id} from vector store: {e}"
            )


# Global vector store instance (initialized lazily)
_vector_store: VectorStore | None = None


def get_vector_store(persist_directory: Path | str | None = None) -> VectorStore:
    """Get or create the global vector store instance.

    Args:
        persist_directory: Path for ChromaDB persistence.

    Returns:
        Singleton VectorStore instance.
    """
    global _vector_store
    if _vector_store is None:
        _vector_store = ChromaVectorStore(persist_directory)
    return _vector_store
