import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.interview_session import SessionStatus
from app.models.question import QuestionType


class InterviewCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    target_role: str | None = Field(default=None, max_length=255)
    resume_id: uuid.UUID | None = None


class AnswerCreate(BaseModel):
    question_id: uuid.UUID
    content: str = Field(min_length=1)
    duration_seconds: int | None = Field(default=None, ge=0)


class AnswerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content: str
    duration_seconds: int | None
    created_at: datetime


class QuestionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    parent_question_id: uuid.UUID | None
    sequence_number: int
    content: str
    question_type: QuestionType
    answer: AnswerRead | None


class InterviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    target_role: str | None
    resume_id: uuid.UUID | None
    status: SessionStatus
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class InterviewDetail(InterviewRead):
    questions: list[QuestionRead]
