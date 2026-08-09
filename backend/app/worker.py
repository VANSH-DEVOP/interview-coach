"""The arq worker process.

Run with `arq app.worker.WorkerSettings`. This is a *separate long-running
process* from the API -- it shares the image and the code, and nothing else. It
holds no HTTP server and serves no requests; the API talks to it only by putting
jobs in Redis.

Retry policy is the whole point of the move off BackgroundTasks. A failed
evaluation is raised, not swallowed, so arq re-queues it with backoff. Only the
final attempt writes FAILED -- marking it earlier would show the user a dead
report while a retry was still pending.

The worker also owns the cron that reconciles orphaned reports. arq's retries
cover a job that fails; nothing covers a job that ceases to exist, which is what
a Redis restart does to the whole queue.
"""

import logging
import uuid
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import engine
from app.services.evaluation_worker import evaluate, mark_failed, reconcile_stale_reports
from app.services.job_queue import EVALUATE_SESSION

logger = logging.getLogger(__name__)

# How often to sweep for orphaned reports. The recovery window a user actually
# experiences is this plus EVALUATION_STALE_AFTER_SECONDS, so there is no point
# sweeping much more often than the staleness threshold changes anything.
RECONCILE_EVERY_MINUTES = 10


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


async def reconcile_reports(ctx: dict[str, Any]) -> None:
    """Put orphaned evaluations back on the queue. Registered as a cron job.

    The pool to enqueue on is arq's own, handed to every job as `ctx["redis"]`;
    opening a second one here would be a connection the worker already has.
    """

    async def _enqueue(session_id: uuid.UUID, user_id: uuid.UUID) -> None:
        # Same argument shape as app/services/job_queue.py: ids cross the queue
        # as strings so the payload does not depend on the serialiser.
        await ctx["redis"].enqueue_job(EVALUATE_SESSION, str(session_id), str(user_id))

    outcome = await reconcile_stale_reports(_enqueue)
    if not outcome:
        logger.debug("Reconciliation swept: nothing orphaned.")


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
    cron_jobs = [
        cron(
            reconcile_reports,
            minute=set(range(0, 60, RECONCILE_EVERY_MINUTES)),
            second=0,
            # A worker restart is one of the moments reports get orphaned, and
            # waiting up to ten minutes to look would be for nothing. The age
            # threshold is what protects live work, not the schedule.
            run_at_startup=True,
            # arq's default. Stated because it is load-bearing: with several
            # worker replicas, only one runs each tick. Without it every replica
            # would sweep, and the same session would be queued N times.
            unique=True,
            # One tick, no retries. A failure inside the sweep is already
            # swallowed and logged, and the next tick is the retry.
            max_tries=1,
        )
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = _redis_settings()
    max_tries = get_settings().EVALUATION_MAX_TRIES
    job_timeout = get_settings().EVALUATION_JOB_TIMEOUT_SECONDS
    # Keep finished jobs briefly so a failure is inspectable in Redis; the
    # report row is the durable record, so there is no reason to keep them long.
    keep_result = 3600
