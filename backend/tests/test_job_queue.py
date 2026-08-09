"""The handoff between the request and the evaluation.

Nothing here talks to a real Redis. The value in these tests is the *branching*
-- which runner gets the work, and what happens when the preferred one is
broken -- and that is exactly the part a real Redis would hide rather than
prove. The arq round-trip itself is verified by hand against docker-compose.
"""

import uuid

import pytest

from app.services import job_queue
from app.services.evaluation_worker import run_evaluation
from app.services.job_queue import EVALUATE_SESSION, EvaluationQueue


class FakeBackgroundTasks:
    """Stands in for fastapi.BackgroundTasks, which only records calls anyway."""

    def __init__(self) -> None:
        self.tasks: list[tuple] = []

    def add_task(self, func, *args, **kwargs) -> None:
        self.tasks.append((func, args, kwargs))


class FakePool:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple] = []

    async def enqueue_job(self, function: str, *args, **kwargs):
        self.calls.append((function, args))
        if self.error is not None:
            raise self.error
        return object()


@pytest.fixture
def ids() -> tuple[uuid.UUID, uuid.UUID]:
    return uuid.uuid4(), uuid.uuid4()


async def test_a_pool_gets_the_job(ids):
    session_id, user_id = ids
    pool, background = FakePool(), FakeBackgroundTasks()

    await EvaluationQueue(pool, background).enqueue(session_id, user_id)

    assert pool.calls == [(EVALUATE_SESSION, (str(session_id), str(user_id)))]
    # Not both. Two runners on one report race each other.
    assert background.tasks == []


async def test_ids_cross_the_boundary_as_strings(ids):
    """The payload is serialised; it must not depend on pickle handling UUIDs."""
    pool = FakePool()

    await EvaluationQueue(pool, FakeBackgroundTasks()).enqueue(*ids)

    _, args = pool.calls[0]
    assert all(isinstance(arg, str) for arg in args)
    # And they must survive the round trip unchanged.
    assert tuple(uuid.UUID(arg) for arg in args) == ids


async def test_without_a_pool_the_work_runs_in_process(ids):
    background = FakeBackgroundTasks()

    await EvaluationQueue(None, background).enqueue(*ids)

    assert background.tasks == [(run_evaluation, ids, {})]


async def test_a_broken_redis_degrades_rather_than_dropping_the_work(ids):
    """The session is already complete and the report already written. Losing
    the evaluation here would leave it PENDING with nothing behind it."""
    pool = FakePool(error=ConnectionError("connection refused"))
    background = FakeBackgroundTasks()

    await EvaluationQueue(pool, background).enqueue(*ids)

    assert background.tasks == [(run_evaluation, ids, {})]


async def test_a_broken_redis_is_recorded_not_swallowed(ids):
    """A queue that has quietly stopped being a queue is the failure worth
    seeing -- the reports still complete, so nothing else gives it away."""
    pool = FakePool(error=ConnectionError("connection refused"))

    await EvaluationQueue(pool, FakeBackgroundTasks()).enqueue(*ids)

    state = job_queue.snapshot()
    assert state["fallbacks"] == 1
    assert "ConnectionError" in str(state["last_error"])
    assert state["last_at"] is not None


async def test_enqueue_never_raises(ids):
    """Whatever Redis does, the request that got here has already committed."""
    pool = FakePool(error=RuntimeError("something unexpected"))

    await EvaluationQueue(pool, FakeBackgroundTasks()).enqueue(*ids)


def test_is_durable_reports_which_branch_is_live():
    assert EvaluationQueue(FakePool(), FakeBackgroundTasks()).is_durable is True
    assert EvaluationQueue(None, FakeBackgroundTasks()).is_durable is False


# -- Worker liveness -----------------------------------------------------------


class FakeHealthPool:
    """A pool that only answers GETs, which is all the heartbeat read needs."""

    def __init__(self, value: bytes | None = None, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.keys: list[str] = []

    async def get(self, key: str):
        self.keys.append(key)
        if self.error is not None:
            raise self.error
        return self.value


async def test_a_present_heartbeat_means_a_live_worker():
    pool = FakeHealthPool(b"Aug-09 20:35:00 j_complete=1 queued=0")

    health = await job_queue.worker_health(pool)

    assert health.alive is True
    assert health.detail == "Aug-09 20:35:00 j_complete=1 queued=0"
    # The key arq actually writes, built from arq's constants rather than a
    # string of our own that could drift out of agreement with it.
    assert pool.keys == ["arq:queue:health-check"]


async def test_a_missing_heartbeat_means_a_dead_worker():
    """arq deletes the key on a clean shutdown and gives it a TTL otherwise, so
    absence is the signal -- no clock of ours is involved."""
    health = await job_queue.worker_health(FakeHealthPool(value=None))

    assert health.alive is False


async def test_an_unreachable_redis_is_unknown_rather_than_dead():
    health = await job_queue.worker_health(
        FakeHealthPool(error=ConnectionError("connection refused"))
    )

    assert health.alive is None


async def test_no_pool_means_the_question_was_never_asked():
    """Without a queue the evaluations run in-process by design."""
    health = await job_queue.worker_health(None)

    assert health.alive is None
    assert health.detail is None
