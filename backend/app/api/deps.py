"""Dependency injection wiring.

Routes declare dependencies; FastAPI builds the object graph per request:
session → repositories → services. Routes contain zero construction logic.
"""

import uuid
from typing import Annotated

import jwt as pyjwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import UnauthorizedError
from app.core.security import decode_token
from app.core.config import get_settings
from app.db.session import get_session
from app.models.user import User
from app.repositories.interview_repository import InterviewRepository
from app.repositories.report_repository import ReportRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.user_repository import UserRepository
from app.services.ai.base import get_question_generator
from app.services.ai.evaluator import get_evaluator
from app.services.ai.embedding import EmbeddingService
from app.services.ai.vector_store import get_vector_store
from app.services.ai.rag import RAGService
from app.services.auth_service import AuthService
from app.services.interview_service import InterviewService
from app.services.resume_service import ResumeService
from app.services.user_service import UserService
from app.services.storage import get_storage_service

DbSession = Annotated[AsyncSession, Depends(get_session)]

_bearer = HTTPBearer(auto_error=False)


# -- Repositories -------------------------------------------------------------
def get_user_repository(session: DbSession) -> UserRepository:
    return UserRepository(session)


def get_resume_repository(session: DbSession) -> ResumeRepository:
    return ResumeRepository(session)


def get_interview_repository(session: DbSession) -> InterviewRepository:
    return InterviewRepository(session)


def get_report_repository(session: DbSession) -> ReportRepository:
    return ReportRepository(session)


# -- AI Services ------------------------------------------------------------------
def get_rag_service() -> "RAGService | None":
    """Get RAG service if Gemini API key is configured.
    
    Returns None if not configured, allowing graceful fallback.
    """
    settings = get_settings()
    if not settings.GEMINI_API_KEY:
        return None
    
    try:
        embedding_service = EmbeddingService(settings.GEMINI_API_KEY)
        vector_store = get_vector_store(persist_directory=settings.CHROMA_PATH)
        return RAGService(embedding_service, vector_store)
    except Exception:
        # Return None if RAG initialization fails; gracefully disable RAG
        return None


# -- Services ------------------------------------------------------------------
def get_auth_service(
    users: Annotated[UserRepository, Depends(get_user_repository)],
) -> AuthService:
    return AuthService(users)


def get_user_service(
    users: Annotated[UserRepository, Depends(get_user_repository)],
) -> UserService:
    return UserService(users)


def get_resume_service(
    resumes: Annotated[ResumeRepository, Depends(get_resume_repository)],
) -> ResumeService:
    rag_service = get_rag_service()
    return ResumeService(resumes, get_storage_service(), rag_service)


def get_interview_service(
    interviews: Annotated[InterviewRepository, Depends(get_interview_repository)],
    resumes: Annotated[ResumeRepository, Depends(get_resume_repository)],
    reports: Annotated[ReportRepository, Depends(get_report_repository)],
) -> InterviewService:
    return InterviewService(
        interviews, resumes, get_question_generator(rag_service=get_rag_service()), get_evaluator(), reports
    )


# -- Authentication -------------------------------------------------------------
async def get_current_user(
    users: Annotated[UserRepository, Depends(get_user_repository)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> User:
    if credentials is None:
        raise UnauthorizedError("Missing authentication credentials.")
    try:
        payload = decode_token(credentials.credentials, expected_type="access")
        user_id = uuid.UUID(payload["sub"])
    except (pyjwt.InvalidTokenError, KeyError, ValueError) as exc:
        raise UnauthorizedError("Invalid or expired access token.") from exc

    user = await users.get(user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("Account is no longer active.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
