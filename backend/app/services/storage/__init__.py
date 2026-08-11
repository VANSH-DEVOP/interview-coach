"""Storage provider factory.

The only place in the codebase that knows which concrete provider is active. No
business logic, route or model changes when a provider is added.

There are two backends, not four. An earlier note here suggested registering
`S3StorageService`, `R2StorageService` and `MinioStorageService` separately --
but AWS S3, Cloudflare R2, MinIO and Backblaze B2 all speak the same API, so
those would have been three copies of one file differing by a hostname. They are
one backend selected by `STORAGE_BACKEND=s3` and distinguished by
`S3_ENDPOINT_URL`.
"""

from functools import lru_cache

from app.core.config import get_settings
from app.core.exceptions import StorageError
from app.services.storage.base import StorageService, StoredFile
from app.services.storage.local import LocalStorageService

__all__ = ["StorageService", "StoredFile", "get_storage_service"]


@lru_cache
def get_storage_service() -> StorageService:
    settings = get_settings()
    match settings.STORAGE_BACKEND:
        case "local":
            return LocalStorageService(root=settings.STORAGE_LOCAL_PATH)
        case "s3":
            # Imported here rather than at module scope: boto3 pulls in botocore
            # and its service definitions, and a deployment on local disk should
            # not pay for that at startup.
            from app.services.storage.s3 import S3StorageService

            if not settings.S3_BUCKET:
                # Loudly, at boot. The alternative is a successful start and a
                # 502 on the first upload, which is the same failure an hour
                # later and much harder to attribute.
                raise StorageError(
                    "STORAGE_BACKEND=s3 requires S3_BUCKET to be set."
                )
            return S3StorageService(
                bucket=settings.S3_BUCKET,
                endpoint_url=settings.S3_ENDPOINT_URL,
                region=settings.S3_REGION,
                access_key_id=settings.S3_ACCESS_KEY_ID,
                secret_access_key=settings.S3_SECRET_ACCESS_KEY,
            )
        case _:  # pragma: no cover - unreachable while the Literal is exhaustive
            raise ValueError(f"Unknown storage backend: {settings.STORAGE_BACKEND}")
