"""Dependency injection wiring.

Routes declare dependencies; FastAPI builds the object graph per request:
session → repositories → services. Routes contain zero construction logic.
"""

import logging
import uuid
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated

import jwt as pyjwt
from fastapi import BackgroundTasks, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rate_limit
from app.core.config import get_settings
from app.core.exceptions import UnauthorizedError
from app.core.security import decode_token
from app.db.session import get_session
from app.models.user import User
from app.repositories.interview_repository import InterviewRepository
from app.repositories.one_time_token_repository import OneTimeTokenRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.report_repository import ReportRepository
from app.repositories.resume_chunk_repository import ResumeChunkRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.user_repository import UserRepository
from app.services.account_service import AccountService
from app.services.ai import retrieval_metrics
from app.services.ai.base import get_question_generator
from app.services.ai.embedding import EmbeddingService
from app.services.ai.masking import redactor_for
from app.services.ai.rag import RAGService
from app.services.ai.retrieval import HybridRetriever
from app.services.ai.vector_store import get_vector_store
from app.services.auth_service import AuthService
from app.services.email import get_email_sender
from app.services.interview_service import InterviewService
from app.services.job_queue import EvaluationQueue
from app.services.one_time_tokens import OneTimeTokenService
from app.services.report_service import ReportService
from app.services.resume_service import ResumeService
from app.services.storage import get_storage_service
from app.services.user_service import UserService

if TYPE_CHECKING:
    from app.services.ai.embedding_cache import EmbeddingCache

logger = logging.getLogger(__name__)

DbSession = Annotated[AsyncSession, Depends(get_session)]

_bearer = HTTPBearer(auto_error=False)


# -- Repositories -------------------------------------------------------------
def get_user_repository(session: DbSession) -> UserRepository:
    return UserRepository(session)


def get_resume_repository(session: DbSession) -> ResumeRepository:
    return ResumeRepository(session)


def get_resume_chunk_repository(session: DbSession) -> ResumeChunkRepository:
    return ResumeChunkRepository(session)


def get_interview_repository(session: DbSession) -> InterviewRepository:
    return InterviewRepository(session)


def get_report_repository(session: DbSession) -> ReportRepository:
    return ReportRepository(session)


def get_refresh_token_repository(session: DbSession) -> RefreshTokenRepository:
    return RefreshTokenRepository(session)


def get_one_time_token_repository(session: DbSession) -> OneTimeTokenRepository:
    return OneTimeTokenRepository(session)


# -- AI Services ------------------------------------------------------------------
@lru_cache(maxsize=1)
def _embedding_cache() -> "EmbeddingCache | None":
    """The embedding cache, or None when there is no Redis to put it in.

    Its own client rather than the arq pool: this is process-wide and lives as
    long as the cached RAG service, while the arq pool is opened by the
    lifespan for queue work. `from_url` connects lazily, so building it here
    costs nothing until the first lookup, and a Redis that is configured but
    down degrades to full-price embedding rather than failing the request.
    """
    settings = get_settings()
    if not settings.REDIS_URL:
        logger.info("REDIS_URL is not set; embeddings will not be cached.")
        return None

    from redis.asyncio import Redis

    from app.services.ai.embedding_cache import EmbeddingCache

    return EmbeddingCache(
        Redis.from_url(settings.REDIS_URL),
        model=settings.GEMINI_EMBEDDING_MODEL,
        ttl_seconds=settings.EMBEDDING_CACHE_TTL_SECONDS,
    )


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
        retrieval_metrics.record_availability(enabled=False, reason="no_api_key")
        return None

    try:
        embedding_service = EmbeddingService(
            settings.GEMINI_API_KEY,
            model=settings.GEMINI_EMBEDDING_MODEL,
            cache=_embedding_cache(),
        )
        vector_store = get_vector_store(persist_directory=settings.CHROMA_PATH)
    except Exception as exc:
        # Disable RAG rather than fail the request -- but say so. Silently
        # returning None here is how a broken index looks identical to a
        # working one from the outside. The result is cached, so this warning
        # is logged once per process; /health keeps it readable afterwards,
        # which matters because the usual cause is an unwritable CHROMA_PATH
        # outside Docker and the symptom is merely "the questions feel generic".
        logger.warning(
            "RAG initialisation failed (path=%s); continuing without retrieval.",
            settings.CHROMA_PATH,
            exc_info=True,
        )
        retrieval_metrics.record_availability(
            enabled=False, reason=f"init_failed: {type(exc).__name__}"
        )
        return None

    logger.info("RAG enabled (chroma_path=%s).", settings.CHROMA_PATH)
    retrieval_metrics.record_availability(enabled=True)
    return RAGService(embedding_service, vector_store)


# -- Authentication -------------------------------------------------------------
# Above the services on purpose: several of them take CurrentUser so the PII
# redactor knows whose name is identifying, and this module deliberately has no
# `from __future__ import annotations` (see the rate-limiting note below), so a
# name must be defined before the signature that mentions it.
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


# -- Services ------------------------------------------------------------------
def get_auth_service(
    users: Annotated[UserRepository, Depends(get_user_repository)],
    tokens: Annotated[RefreshTokenRepository, Depends(get_refresh_token_repository)],
) -> AuthService:
    return AuthService(users, tokens)


def get_user_service(
    users: Annotated[UserRepository, Depends(get_user_repository)],
) -> UserService:
    return UserService(users)


def get_account_deletion_service(
    users: Annotated[UserRepository, Depends(get_user_repository)],
    resumes: Annotated[ResumeRepository, Depends(get_resume_repository)],
) -> UserService:
    """The same service, wired for deletion.

    Separate from `get_user_service` on purpose. Deletion is the only operation
    that touches the blob store and the vector index, and folding them into the
    common dependency would make every /users route construct a storage backend
    -- so reading your own profile would fail if object storage were
    misconfigured or its credentials had expired.
    """
    return UserService(users, resumes, get_storage_service(), get_rag_service())


def get_one_time_token_service(
    tokens: Annotated[OneTimeTokenRepository, Depends(get_one_time_token_repository)],
) -> OneTimeTokenService:
    return OneTimeTokenService(tokens)


def get_account_service(
    users: Annotated[UserRepository, Depends(get_user_repository)],
    tokens: Annotated[OneTimeTokenService, Depends(get_one_time_token_service)],
) -> AccountService:
    # The email backend is a process-wide singleton chosen by configuration,
    # not a per-request object; tests override this dependency to inject a
    # recording sender.
    return AccountService(users, tokens, get_email_sender())


def get_report_service(
    reports: Annotated[ReportRepository, Depends(get_report_repository)],
) -> ReportService:
    return ReportService(reports)


def get_resume_service(
    resumes: Annotated[ResumeRepository, Depends(get_resume_repository)],
    chunks: Annotated[ResumeChunkRepository, Depends(get_resume_chunk_repository)],
    current_user: CurrentUser,
) -> ResumeService:
    # The current user is a dependency purely so the redactor knows whose name
    # to treat as identifying; every route using this service already requires
    # authentication, so it costs no extra work.
    rag_service = get_rag_service()
    return ResumeService(
        resumes,
        get_storage_service(),
        rag_service,
        redactor_for(current_user.full_name),
        chunks=chunks,
    )


def get_interview_service(
    interviews: Annotated[InterviewRepository, Depends(get_interview_repository)],
    resumes: Annotated[ResumeRepository, Depends(get_resume_repository)],
    reports: Annotated[ReportRepository, Depends(get_report_repository)],
    chunks: Annotated[ResumeChunkRepository, Depends(get_resume_chunk_repository)],
    current_user: CurrentUser,
) -> InterviewService:
    redactor = redactor_for(current_user.full_name)
    # Built per request: the keyword half is a repository on this request's
    # session, while the dense half is the process-wide client. Composing them
    # here keeps question generation unaware that there are two.
    retriever = HybridRetriever(get_rag_service(), chunks)
    return InterviewService(
        interviews,
        resumes,
        get_question_generator(retriever=retriever, redactor=redactor),
        reports,
    )


# -- Background work -------------------------------------------------------------
def get_evaluation_queue(request: Request, background: BackgroundTasks) -> EvaluationQueue:
    """Handle for scheduling an evaluation off the request path.

    The pool is opened once in the lifespan and lives on app.state; `getattr`
    rather than attribute access because the lifespan does not run under
    httpx's ASGITransport, and a missing pool is a supported state (it means
    the in-process fallback), not an error.
    """
    return EvaluationQueue(getattr(request.app.state, "arq_pool", None), background)


EvaluationQueueDep = Annotated[EvaluationQueue, Depends(get_evaluation_queue)]


# -- Rate limiting ---------------------------------------------------------------
# The mechanism lives in app/core/rate_limit.py; the wiring lives here, like all
# other DI. Note the absence of `from __future__ import annotations` in this
# module: these signatures must stay resolvable at runtime, or FastAPI cannot
# see the Depends marker and silently reinterprets the parameter as a query
# field (which is exactly what happened when this lived in app/core).
def rate_limit_store(request: Request):
    """The Redis holding the shared counters, or None for per-process ones.

    The arq pool doubles as the store rather than opening a second one: it is a
    connection pool to the same Redis, already built at startup, and the keys
    live under their own prefix (arq owns `arq:*`). The consequence to know is
    that no REDIS_URL means no shared counters -- correct today, since a
    deployment with several API replicas needs the queue anyway.
    """
    return getattr(request.app.state, "arq_pool", None)


def limit_by_ip(scope: str):
    """Rate limit by client IP. For endpoints with no authenticated user."""

    async def dependency(request: Request) -> None:
        await rate_limit.enforce(
            rate_limit.client_ip(request), scope=scope, redis=rate_limit_store(request)
        )

    return dependency


def limit_by_user(scope: str):
    """Rate limit by authenticated user id.

    Depending on get_current_user costs nothing extra -- FastAPI caches
    dependencies per request, so the route's own CurrentUser resolves the same
    object. It also means an unauthenticated caller is rejected with 401 before
    consuming anyone's budget.
    """

    async def dependency(request: Request, user: CurrentUser) -> None:
        await rate_limit.enforce(
            str(user.id), scope=scope, redis=rate_limit_store(request)
        )

    return dependency
