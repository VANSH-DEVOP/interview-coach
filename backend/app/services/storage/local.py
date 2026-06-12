"""Local-disk implementation of StorageService.

MVP provider. Stores objects under a root directory that lives OUTSIDE the
application code tree (default: /var/lib/interviewpilot/storage, mounted as a
Docker volume). File I/O is offloaded to a thread to keep the event loop free.
"""

import asyncio
import logging
from pathlib import Path, PurePosixPath

from app.core.exceptions import StorageError
from app.services.storage.base import StorageService, StoredFile

logger = logging.getLogger(__name__)


class LocalStorageService(StorageService):
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        """Map an opaque key to a filesystem path, rejecting path traversal."""
        pure = PurePosixPath(key)
        if pure.is_absolute() or ".." in pure.parts:
            raise StorageError("Invalid storage key.")
        path = (self._root / pure).resolve()
        if not path.is_relative_to(self._root):
            raise StorageError("Invalid storage key.")
        return path

    async def save(self, key: str, content: bytes, content_type: str) -> StoredFile:
        path = self._resolve(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename for atomicity: readers never see partial files.
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(content)
            tmp.replace(path)

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            logger.error("Local storage write failed", extra={"path": str(path)})
            raise StorageError("Failed to store file.") from exc
        return StoredFile(key=key, size_bytes=len(content), content_type=content_type)

    async def read(self, key: str) -> bytes:
        path = self._resolve(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise StorageError("File not found in storage.") from exc
        except OSError as exc:
            raise StorageError("Failed to read file.") from exc

    async def delete(self, key: str) -> None:
        path = self._resolve(key)
        try:
            await asyncio.to_thread(path.unlink, True)  # missing_ok=True
        except OSError as exc:
            raise StorageError("Failed to delete file.") from exc

    async def exists(self, key: str) -> bool:
        path = self._resolve(key)
        return await asyncio.to_thread(path.exists)
