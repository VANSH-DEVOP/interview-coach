"""Resume business logic.

Depends on the StorageService ABSTRACTION only: swapping local disk for
S3/R2/MinIO requires zero changes here.
"""

import logging
import uuid
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from app.core.config import get_settings
from app.core.exceptions import NotFoundError, PayloadTooLargeError, ValidationError
from app.models.resume import Resume, ResumeStatus
from app.models.resume_chunk import ResumeChunk
from app.repositories.resume_chunk_repository import ResumeChunkRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.ai.degradation import record_fallback
from app.services.ai.rag import ResumeChunker
from app.services.resume_parser import ResumeParser
from app.services.storage.base import StorageService

if TYPE_CHECKING:
    from app.services.ai.masking import Redactor
    from app.services.ai.rag import RAGService

logger = logging.getLogger(__name__)

_EXTENSION_BY_CONTENT_TYPE = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


class ResumeService:
    def __init__(
        self,
        resumes: ResumeRepository,
        storage: StorageService,
        rag_service: "RAGService | None" = None,
        redactor: "Redactor | None" = None,
        chunks: ResumeChunkRepository | None = None,
    ) -> None:
        self.resumes = resumes
        self.storage = storage
        self.rag_service = rag_service
        # Indexing sends the resume to Google a chunk at a time. The parsed
        # text stays whole in our own database; only what leaves is redacted.
        self.redactor = redactor
        # Chunking is a pure function and the rows belong to this transaction,
        # so the split and the save happen here rather than inside RAGService,
        # which is cached process-wide and holds no session.
        self.chunks = chunks
        self.chunker = ResumeChunker()

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
        resume = await self.resumes.add(resume)

        await self._index(resume, user_id, parsed_text)
        return resume

    async def _index(
        self, resume: Resume, user_id: uuid.UUID, parsed_text: str | None
    ) -> None:
        """Chunk a resume, store the chunks, and index them. Never raises.

        Non-blocking by design: a failure here must not fail the upload. It is
        recorded as a degradation so it shows up on /health -- an unindexed
        resume means every later question for it is built from truncated raw
        text instead of retrieved context, with no other outward sign.

        The chunks are saved *before* the embedding call and marked embedded
        after it, in that order deliberately. A provider failure then leaves
        rows with `embedded_at` NULL -- a durable record of which parts of this
        resume the retriever cannot see -- instead of leaving nothing at all
        and requiring a re-parse to find out.
        """
        if not parsed_text:
            return

        chunks = self.chunker.chunk(parsed_text)
        if self.chunks is not None:
            await self.chunks.replace_for_resume(
                resume.id,
                user_id,
                [
                    ResumeChunk(
                        ordinal=chunk.ordinal,
                        section=chunk.section,
                        content=chunk.content,
                    )
                    for chunk in chunks
                ],
            )

        if not self.rag_service:
            return
        try:
            embedded = await self.rag_service.index_chunks(
                resume.id, user_id, chunks, redactor=self.redactor
            )
            if self.chunks is not None:
                await self.chunks.mark_embedded(resume.id, embedded)
            logger.info(
                f"Indexed resume {resume.id}: {len(embedded)}/{len(chunks)} chunks embedded"
            )
        except Exception as e:
            logger.error(f"Failed to index resume {resume.id} for RAG: {e}")
            record_fallback("index_resume", e)

    async def reprocess(self, resume_id: uuid.UUID, user_id: uuid.UUID) -> Resume:
        """Re-parse the stored file and rebuild its vector index.

        Two situations need this, and neither had a way out:

          * A parse failure set ResumeStatus.FAILED permanently. The blob was
            still in storage, but nothing would ever look at it again.
          * A resume uploaded while indexing was broken is in the database and
            absent from the vector store, so retrieval silently returns nothing
            for it forever.

        The old index is dropped before rebuilding. Chunk ids are positional
        ({resume_id}:chunk:N), so re-indexing over the top of a longer previous
        run would leave stale trailing chunks behind.
        """
        resume = await self.get(resume_id, user_id)
        content = await self.storage.read(resume.storage_key)

        try:
            resume.parsed_text = ResumeParser.parse(content, resume.content_type)
            resume.status = ResumeStatus.PARSED
            logger.info(f"Re-parsed resume {resume.id} for user {user_id}")
        except ValueError as e:
            resume.parsed_text = None
            resume.status = ResumeStatus.FAILED
            logger.warning(f"Re-parse failed for resume {resume.id}: {e}")

        await self.resumes.session.flush()

        if self.rag_service:
            try:
                await self.rag_service.delete_index(resume_id)
            except Exception as e:
                logger.warning(f"Could not clear old index for resume {resume_id}: {e}")

        await self._index(resume, user_id, resume.parsed_text)
        return resume

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
        
        # Remove from vector store if available
        if self.rag_service:
            try:
                await self.rag_service.delete_index(resume_id)
            except Exception as e:
                logger.error(f"Failed to delete resume {resume_id} from vector store: {e}")
