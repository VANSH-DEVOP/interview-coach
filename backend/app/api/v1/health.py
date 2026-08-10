from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rate_limit
from app.core.config import get_settings
from app.db.session import get_session
from app.services import job_queue
from app.services.ai import degradation, retrieval_metrics

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


class WorkerHealthResponse(BaseModel):
    """Whether anything is *consuming* the queue.

    `queue.connected` only says the API can reach Redis. A worker that has died
    leaves the API accepting interviews normally while every report sits on
    PENDING -- nothing is lost, the queue drains when it comes back, and without
    this block nothing anywhere says so.

    `expected` is false when no queue is configured, where in-process evaluation
    is the intended design rather than a fault. `alive` is null when the
    question could not be asked at all.
    """

    expected: bool
    alive: bool | None = None
    detail: str | None = None


class RagHealth(BaseModel):
    """Whether retrieval is on, and whether it is finding anything.

    Retrieval degrades more quietly than anything else here: when it is off,
    empty, or broken, question generation falls back to the first 4000
    characters of the resume and produces plausible questions anyway. The
    interview works; it is just no longer personalised.

    `enabled` is null until something has tried to build the service.
    `disabled_reason` is usually `no_api_key`, or an `init_failed` when
    CHROMA_PATH is not writable -- the normal case outside Docker.

    `full_text_fallbacks` climbing while `retrievals` stays flat means
    retrieval is not being reached at all. `hits` climbing with a
    `last_best_distance` near 1.0 or above means it is being reached and
    returning junk, which counts as a hit and is not one.
    """

    enabled: bool | None = None
    disabled_reason: str | None = None
    retrievals: int
    hits: int
    empty: int
    failed: int
    full_text_fallbacks: int
    avg_ms: float | None = None
    max_ms: float | None = None
    last_best_distance: float | None = None
    resumes_indexed: int
    chunks_produced: int
    chunks_embedded: int
    # Hybrid retrieval. `agreed` at zero across many fusions means the dense
    # and keyword halves never corroborate each other, which is what a broken
    # embedding model or an empty keyword index looks like from outside -- the
    # fused list still comes back full, from one source.
    fusions: int
    dense_only: int
    sparse_only: int
    agreed: int
    last_at: str | None = None


class RateLimitHealth(BaseModel):
    """Where the counters live, and whether they stayed there.

    `store` is `in_process` when there is no Redis pool, which means the limits
    apply per replica: N replicas allow N x the intended ceiling against a quota
    that is shared by all of them. `fallbacks` counts requests that had a Redis
    and could not use it, and so were counted per-process anyway.
    """

    enabled: bool
    store: Literal["redis", "in_process"]
    fallbacks: int
    last_error: str | None = None
    last_at: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    environment: str
    database: Literal["up", "down"]
    ai: AiHealth
    queue: QueueHealth
    worker: WorkerHealthResponse
    rate_limit: RateLimitHealth
    rag: RagHealth


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

    pool = getattr(request.app.state, "arq_pool", None)
    worker = await job_queue.worker_health(pool)
    # Expected only when the queue is actually in use. Without a pool the
    # evaluations run in this process by design, and there is no worker to miss.
    worker_expected = pool is not None

    # AI degradation does not flip `status`: the app is still serving correct
    # responses, and a single historical 429 should not fail a liveness probe.
    # It is reported for monitoring, not for orchestration.
    #
    # A missing worker does flip it. Unlike a fallback, nothing downstream
    # covers for it: reports queue up and stay PENDING until someone notices,
    # and "someone notices" is exactly what this field is for.
    degraded = database == "down" or (worker_expected and worker.alive is False)
    return HealthResponse(
        status="degraded" if degraded else "ok",
        environment=settings.ENVIRONMENT,
        database=database,
        worker=WorkerHealthResponse(
            expected=worker_expected, alive=worker.alive, detail=worker.detail
        ),
        # Reported, not acted on, for the same reason as the AI block: the app
        # is still returning correct answers, they are merely less tailored.
        rag=RagHealth.model_validate(retrieval_metrics.snapshot()),
        # Reported, not acted on: a per-process counter still bounds abuse, it
        # just bounds it once per replica.
        rate_limit=RateLimitHealth.model_validate(
            {
                "enabled": settings.RATE_LIMIT_ENABLED,
                "store": "redis" if pool is not None else "in_process",
                **rate_limit.snapshot(),
            }
        ),
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
