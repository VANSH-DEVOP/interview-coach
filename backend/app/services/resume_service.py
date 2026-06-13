"""Resume business logic.

Depends on the StorageService ABSTRACTION only: swapping local disk for
S3/R2/MinIO requires zero changes here.
"""

import logging
import uuid
from pathlib import PurePosixPath

from app.core.config import get_settings
from app.core.exceptions import NotFoundError, PayloadTooLargeError, ValidationError
from app.models.resume import Resume, ResumeStatus
from app.repositories.resume_repository import ResumeRepository
from app.services.storage.base import StorageService
from app.services.resume_parser import ResumeParser

logger = logging.getLogger(__name__)

_EXTENSION_BY_CONTENT_TYPE = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


class ResumeService:
    def __init__(self, resumes: ResumeRepository, storage: StorageService) -> None:
        self.resumes = resumes
        self.storage = storage

    async def upload(
        self, *, user_id: uuid.UUID, file_name: str, content: bytes, content_type: str
    ) -> Resume:
        settings = get_settings()
        if content_type not in settings.ALLOWED_RESUME_CONTENT_TYPES:
            raise ValidationError("Only PDF and DOCX resumes are accepted.")
        if len(content) == 0:
            raise ValidationError("Uploaded file is empty.")
        if len(content) > settings.MAX_UPLOAD_SIZE_BYTES:
            raise PayloadTooLargeError(
                f"File exceeds the {settings.MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)} MiB limit."
            )

        # Opaque, collision-free key; safe original name kept as metadata only.
        extension = _EXTENSION_BY_CONTENT_TYPE[content_type]
        key = f"resumes/{user_id}/{uuid.uuid4().hex}{extension}"
        stored = await self.storage.save(key, content, content_type)

        # Parse resume text
        parsed_text = None
        status = ResumeStatus.UPLOADED
        try:
            parsed_text = ResumeParser.parse(content, content_type)
            status = ResumeStatus.PARSED
            logger.info(f"Successfully parsed resume {file_name} for user {user_id}")
        except ValueError as e:
            logger.warning(f"Failed to parse resume {file_name}: {e}")
            status = ResumeStatus.FAILED

        resume = Resume(
            user_id=user_id,
            file_name=PurePosixPath(file_name).name[:255],  # strip any client path
            storage_key=stored.key,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            parsed_text=parsed_text,
            status=status,
        )
        return await self.resumes.add(resume)

    async def list(self, user_id: uuid.UUID, *, offset: int, limit: int):
        return await self.resumes.list_for_user(user_id, offset=offset, limit=limit)

    async def get(self, resume_id: uuid.UUID, user_id: uuid.UUID) -> Resume:
        resume = await self.resumes.get_owned(resume_id, user_id)
        if resume is None:
            raise NotFoundError("Resume not found.")
        return resume

    async def download(self, resume_id: uuid.UUID, user_id: uuid.UUID) -> tuple[Resume, bytes]:
        resume = await self.get(resume_id, user_id)
        content = await self.storage.read(resume.storage_key)
        return resume, content

    async def delete(self, resume_id: uuid.UUID, user_id: uuid.UUID) -> None:
        resume = await self.get(resume_id, user_id)
        # DB row first (authoritative), then blob; orphan blobs are reclaimable.
        await self.resumes.delete(resume)
        await self.storage.delete(resume.storage_key)
