"""The arq worker process.

Run with `arq app.worker.WorkerSettings`. This is a *separate long-running
process* from the API -- it shares the image and the code, and nothing else. It
holds no HTTP server and serves no requests; the API talks to it only by putting
jobs in Redis.

Retry policy is the whole point of the move off BackgroundTasks. A failed
evaluation is raised, not swallowed, so arq re-queues it with backoff. Only the
final attempt writes FAILED -- marking it earlier would show the user a dead
report while a retry was still pending.
"""

import logging
import uuid
from typing import Any

from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import engine
from app.services.evaluation_worker import evaluate, mark_failed

logger = logging.getLogger(__name__)


async def evaluate_session(ctx: dict[str, Any], session_id: str, user_id: str) -> None:
    """Evaluate one interview. Registered with arq under this function name."""
    sid = uuid.UUID(session_id)
    try:
        await evaluate(sid, uuid.UUID(user_id))
    except Exception:
        attempt, limit = ctx["job_try"], get_settings().EVALUATION_MAX_TRIES
        if attempt < limit:
            logger.warning(
                "Evaluation of session %s failed on attempt %d/%d; arq will retry.",
                sid,
                attempt,
                limit,
                exc_info=True,
            )
            raise
        logger.exception(
            "Evaluation of session %s failed on the final attempt (%d/%d).",
            sid,
            attempt,
            limit,
        )
        # Swallowed on purpose: the report is now FAILED and visible in the UI
        # with a retry button. Re-raising would only add a traceback arq has
        # already given up on.
        await mark_failed(sid)


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging()
    logger.info("Evaluation worker started.")


async def shutdown(ctx: dict[str, Any]) -> None:
    # The worker opens its own database sessions per job from the shared engine;
    # dispose so the process exits without leaving connections behind.
    await engine.dispose()
    logger.info("Evaluation worker stopped.")


def _redis_settings() -> RedisSettings:
    """Where to consume from.

    arq reads `WorkerSettings.redis_settings` as a value, not a callable, so
    this is evaluated at import. It must not raise: the tests import this module
    to exercise `evaluate_session`, and they have no Redis. An unset REDIS_URL
    falls through to arq's localhost default and the worker fails on connect
    instead -- with a warning here saying why.
    """
    url = get_settings().REDIS_URL
    if not url:
        logger.warning(
            "REDIS_URL is not set; the worker will try arq's default "
            "(localhost:6379). Set REDIS_URL to point it at the right Redis -- "
            "and note the API falls back to in-process evaluation without it, "
            "so this worker would have nothing to consume."
        )
        return RedisSettings()
    return RedisSettings.from_dsn(url)


class WorkerSettings:
    """arq entry point. These attributes are read by arq as Worker kwargs."""

    functions = [evaluate_session]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = _redis_settings()
    max_tries = get_settings().EVALUATION_MAX_TRIES
    job_timeout = get_settings().EVALUATION_JOB_TIMEOUT_SECONDS
    # Keep finished jobs briefly so a failure is inspectable in Redis; the
    # report row is the durable record, so there is no reason to keep them long.
    keep_result = 3600
