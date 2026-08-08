import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.resume import ResumeStatus


class ResumeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    file_name: str
    content_type: str
    size_bytes: int
    status: ResumeStatus
    created_at: datetime


class ResumePreview(BaseModel):
    """The extracted text, on its own endpoint.

    Kept out of ResumeRead on purpose: it can run to several thousand
    characters and would be dead weight on every list response.

    This is what the AI actually reads. A resume whose text came out empty or
    garbled produces generic questions, and until now there was no way to see
    that -- the file downloaded fine, so nothing looked wrong.
    """

    id: uuid.UUID
    file_name: str
    status: ResumeStatus
    parsed_text: str | None
    character_count: int
    word_count: int
