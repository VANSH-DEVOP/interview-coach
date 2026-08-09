from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_session
from app.services import job_queue
from app.services.ai import degradation

router = APIRouter(tags=["system"])


class AiHealth(BaseModel):
    """Whether AI output is real or coming from the deterministic fallbacks.

    `fallbacks` counts degradations since process start. A non-zero value means
    some users received generic questions or feedback; a steadily climbing one
    means the provider is effectively down even though every request succeeded.
    """

    configured: bool
    fallbacks: int
    last_operation: str | None = None
    last_error: str | None = None
    last_at: str | None = None


class QueueHealth(BaseModel):
    """Whether evaluations are durable.

    `connected` false means the Redis pool is absent -- either unconfigured or
    unreachable at startup -- and evaluations are running in the web process,
    where a restart loses them. `fallbacks` counts requests that had a pool and
    still could not enqueue, which is a Redis that has degraded since boot.
    """

    configured: bool
    connected: bool
    fallbacks: int
    last_error: str | None = None
    last_at: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    environment: str
    database: Literal["up", "down"]
    ai: AiHealth
    queue: QueueHealth


@router.get("/health", response_model=HealthResponse)
async def health(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> HealthResponse:
    settings = get_settings()
    try:
        await session.execute(text("SELECT 1"))
        database: Literal["up", "down"] = "up"
    except Exception:
        database = "down"
    # AI degradation does not flip `status`: the app is still serving correct
    # responses, and a single historical 429 should not fail a liveness probe.
    # It is reported for monitoring, not for orchestration.
    return HealthResponse(
        status="ok" if database == "up" else "degraded",
        environment=settings.ENVIRONMENT,
        database=database,
        ai=AiHealth.model_validate(
            {"configured": bool(settings.GEMINI_API_KEY), **degradation.snapshot()}
        ),
        queue=QueueHealth.model_validate(
            {
                "configured": bool(settings.REDIS_URL),
                "connected": getattr(request.app.state, "arq_pool", None) is not None,
                **job_queue.snapshot(),
            }
        ),
    )
