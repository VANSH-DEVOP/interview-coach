"""StorageService abstraction.

Business logic, API routes, and database models depend ONLY on this interface.
Providers (local disk today; AWS S3, Cloudflare R2, MinIO tomorrow) implement
it. Swapping providers is a configuration change (`STORAGE_BACKEND`), not a
code change.

Contract notes:
- `key` is an opaque, provider-agnostic identifier (e.g.
  "resumes/<user_id>/<uuid>.pdf"). It is persisted in PostgreSQL as
  `Resume.storage_key` and is never interpreted outside the storage layer.
- All methods raise `StorageError` on provider failure so callers never need
  provider-specific error handling.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StoredFile:
    """Result of a successful save: everything callers need to persist metadata."""

    key: str
    size_bytes: int
    content_type: str


class StorageService(ABC):
    """Abstract object storage. One instance per process; stateless."""

    @abstractmethod
    async def save(self, key: str, content: bytes, content_type: str) -> StoredFile:
        """Persist `content` under `key`, overwriting any existing object."""

    @abstractmethod
    async def read(self, key: str) -> bytes:
        """Return the object's bytes. Raises StorageError if missing."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove the object. Idempotent: missing objects are not an error."""

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Return True if an object exists under `key`."""
