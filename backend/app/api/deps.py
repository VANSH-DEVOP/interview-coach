"""Dependency injection wiring.

Routes declare dependencies; FastAPI builds the object graph per request:
session → repositories → services. Routes contain zero construction logic.
"""

import logging
import uuid
from functools import lru_cache
from typing import Annotated

import jwt as pyjwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rate_limit
from app.core.config import get_settings
from app.core.exceptions import UnauthorizedError
from app.core.security import decode_token
from app.db.session import get_session
from app.models.user import User
from app.repositories.interview_repository import InterviewRepository
from app.repositories.report_repository import ReportRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.user_repository import UserRepository
from app.services.ai.base import get_question_generator
from app.services.ai.embedding import EmbeddingService
from app.services.ai.evaluator import get_evaluator
from app.services.ai.rag import RAGService
from app.services.ai.vector_store import get_vector_store
from app.services.auth_service import AuthService
from app.services.interview_service import InterviewService
from app.services.report_service import ReportService
from app.services.resume_service import ResumeService
from app.services.storage import get_storage_service
from app.services.user_service import UserService

logger = logging.getLogger(__name__)

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
@lru_cache(maxsize=1)
def get_rag_service() -> "RAGService | None":
    """Get the RAG service if a Gemini API key is configured.

    Returns None if RAG is unavailable, so callers fall back gracefully.

    Cached: the embedding client and the Chroma client are process-wide
    singletons. Building them per request re-opened the Chroma database on
    every call. The None result is cached too -- a missing key or a broken
    vector store is a startup-shaped problem, not something to retry on the
    hot path. Call get_rag_service.cache_clear() in tests that vary settings.
    """
    settings = get_settings()
    if not settings.GEMINI_API_KEY:
        logger.info("GEMINI_API_KEY is not set; RAG disabled.")
        return None

    try:
        embedding_service = EmbeddingService(
            settings.GEMINI_API_KEY, model=settings.GEMINI_EMBEDDING_MODEL
        )
        vector_store = get_vector_store(persist_directory=settings.CHROMA_PATH)
    except Exception:
        # Disable RAG rather than fail the request -- but say so. Silently
        # returning None here is how a broken index looks identical to a
        # working one from the outside.
        logger.warning(
            "RAG initialisation failed (path=%s); continuing without retrieval.",
            settings.CHROMA_PATH,
            exc_info=True,
        )
        return None

    logger.info("RAG enabled (chroma_path=%s).", settings.CHROMA_PATH)
    return RAGService(embedding_service, vector_store)


# -- Services ------------------------------------------------------------------
def get_auth_service(
    users: Annotated[UserRepository, Depends(get_user_repository)],
) -> AuthService:
    return AuthService(users)


def get_user_service(
    users: Annotated[UserRepository, Depends(get_user_repository)],
) -> UserService:
    return UserService(users)


def get_report_service(
    reports: Annotated[ReportRepository, Depends(get_report_repository)],
) -> ReportService:
    return ReportService(reports)


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


# -- Rate limiting ---------------------------------------------------------------
# The mechanism lives in app/core/rate_limit.py; the wiring lives here, like all
# other DI. Note the absence of `from __future__ import annotations` in this
# module: these signatures must stay resolvable at runtime, or FastAPI cannot
# see the Depends marker and silently reinterprets the parameter as a query
# field (which is exactly what happened when this lived in app/core).
def limit_by_ip(scope: str):
    """Rate limit by client IP. For endpoints with no authenticated user."""

    async def dependency(request: Request) -> None:
        rate_limit.enforce(rate_limit.client_ip(request), scope=scope)

    return dependency


def limit_by_user(scope: str):
    """Rate limit by authenticated user id.

    Depending on get_current_user costs nothing extra -- FastAPI caches
    dependencies per request, so the route's own CurrentUser resolves the same
    object. It also means an unauthenticated caller is rejected with 401 before
    consuming anyone's budget.
    """

    async def dependency(user: CurrentUser) -> None:
        rate_limit.enforce(str(user.id), scope=scope)

    return dependency
