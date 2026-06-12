"""Storage provider factory.

The only place in the codebase that knows which concrete provider is active.
Future providers register here:

    case "s3":    return S3StorageService(...)       # AWS S3
    case "r2":    return R2StorageService(...)       # Cloudflare R2
    case "minio": return MinioStorageService(...)    # MinIO

No business logic, routes, or models change when a provider is added.
"""

from functools import lru_cache

from app.core.config import get_settings
from app.services.storage.base import StorageService, StoredFile
from app.services.storage.local import LocalStorageService

__all__ = ["StorageService", "StoredFile", "get_storage_service"]


@lru_cache
def get_storage_service() -> StorageService:
    settings = get_settings()
    match settings.STORAGE_BACKEND:
        case "local":
            return LocalStorageService(root=settings.STORAGE_LOCAL_PATH)
        case _:  # pragma: no cover - unreachable while Literal["local"]
            raise ValueError(f"Unknown storage backend: {settings.STORAGE_BACKEND}")
