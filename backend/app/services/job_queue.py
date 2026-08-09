"""Handing evaluation work off the request path.

Two runners, chosen at request time by whether a Redis pool exists:

- **Redis (arq).** The job is written to Redis before the response is sent, so
  it survives a restart of the web process, and arq retries it with backoff if
  the worker dies mid-flight. This is the production path; it needs the separate
  `worker` service to be running.
- **In-process (BackgroundTasks).** No Redis, no worker, no durability -- a
  restart abandons the evaluation and `recover_stale_reports` flips it to FAILED
  on the way back up. Keeps `uvicorn app.main:app` and the test suite working
  with nothing else running.

The fallback mirrors the AI layer: prefer the real thing, degrade to something
that still works, and record it loudly rather than silently. It is not reusing
`ai.degradation` because the failure is a different one -- the output is
correct, the durability guarantee is what was lost -- and a message about
"generic output" would send anyone reading the logs the wrong way. `/health`
reports both.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from arq.connections import ArqRedis
from arq.constants import default_queue_name, health_check_key_suffix
from fastapi import BackgroundTasks

from app.services.evaluation_worker import run_evaluation

logger = logging.getLogger(__name__)

# The arq function name. Must match the callable registered in app/worker.py --
# arq matches by string, so a rename on one side alone means jobs that enqueue
# fine and are never executed.
EVALUATE_SESSION = "evaluate_session"


@dataclass
class _State:
    fallbacks: int = 0
    last_error: str | None = None
    last_at: str | None = None


_state = _State()


def _record_fallback(error: BaseException) -> None:
    """Log and count one degradation to the in-process runner."""
    _state.fallbacks += 1
    _state.last_error = f"{type(error).__name__}: {error}"
    _state.last_at = datetime.now(timezone.utc).isoformat()
    logger.warning(
        "Could not queue the evaluation; running it in-process instead. The "
        "result is the same but the work will not survive a restart. (%s)",
        _state.last_error,
        exc_info=error,
    )


def snapshot() -> dict[str, object]:
    """Current degradation state, for the health endpoint."""
    return {
        "fallbacks": _state.fallbacks,
        "last_error": _state.last_error,
        "last_at": _state.last_at,
    }


def reset() -> None:
    """Clear the recorded state. For tests."""
    global _state
    _state = _State()


# Where arq's worker writes its heartbeat. Composed from arq's own constants so
# a change to either side does not leave the API reading a key nobody writes.
WORKER_HEALTH_KEY = default_queue_name + health_check_key_suffix


@dataclass(frozen=True)
class WorkerHealth:
    """Whether anything is consuming the queue.

    Distinct from the pool being connected, which only says the *API* can reach
    Redis. With no worker the API keeps accepting interviews and every report
    sits on PENDING: nothing is lost, the queue drains when a worker returns,
    and nothing anywhere says so. That silence is the failure this reports.

    `alive` is None when the question could not be asked -- no pool, or Redis
    unreachable -- which is not the same as a dead worker and must not be shown
    as one.
    """

    alive: bool | None = None
    detail: str | None = None


async def worker_health(pool: ArqRedis | None) -> WorkerHealth:
    """Read the worker's heartbeat out of Redis.

    arq refreshes the key every `health_check_interval` seconds with a TTL just
    past that, and deletes it on a clean shutdown. So its presence means a
    worker was alive within the last interval, and this needs no clock of its
    own -- Redis expiring the key *is* the timeout.
    """
    if pool is None:
        return WorkerHealth()
    try:
        heartbeat = await pool.get(WORKER_HEALTH_KEY)
    except Exception as exc:  # noqa: BLE001 - health must not raise
        logger.warning("Could not read the worker heartbeat: %s", exc)
        return WorkerHealth()

    if heartbeat is None:
        return WorkerHealth(alive=False)
    # arq's own summary string (jobs complete/failed/retried/ongoing, queue
    # depth). Passed through unparsed: it is a human-readable debugging aid
    # whose format belongs to arq, and parsing it would break on an upgrade for
    # no gain.
    return WorkerHealth(alive=True, detail=heartbeat.decode(errors="replace"))


class EvaluationQueue:
    """Request-scoped handle that knows both ways to run an evaluation."""

    def __init__(self, pool: ArqRedis | None, background: BackgroundTasks) -> None:
        self._pool = pool
        self._background = background

    @property
    def is_durable(self) -> bool:
        return self._pool is not None

    async def enqueue(self, session_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """Schedule the evaluation. Returns once the work is handed off.

        Never raises. The caller has already committed the completed session and
        written a PENDING report, so failing the request here would report an
        error for work that did happen -- and leave the report orphaned anyway.
        """
        if self._pool is not None:
            try:
                # UUIDs are passed as strings: they cross a serialisation
                # boundary, and the job payload should not depend on the
                # serialiser happening to be pickle.
                await self._pool.enqueue_job(
                    EVALUATE_SESSION, str(session_id), str(user_id)
                )
                logger.info("Queued evaluation for session %s.", session_id)
                return
            except Exception as exc:  # noqa: BLE001 - any Redis failure degrades
                _record_fallback(exc)

        self._background.add_task(run_evaluation, session_id, user_id)
