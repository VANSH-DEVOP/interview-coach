"""The enqueue path against a real Redis.

tests/test_job_queue.py proves the branching with a fake pool, which is the part
worth unit-testing. What a fake cannot prove is that we are calling arq
correctly -- that the job lands on the queue arq's worker reads, under the name
the worker registered, with arguments that survive serialisation. That contract
lives in arq, so only arq can check it.

The `redis_pool` fixture (tests/conftest.py) skips when no Redis is reachable,
mirroring the Postgres fixtures: `pytest` stays useful on a laptop with nothing
running, and CI sets REQUIRE_TEST_REDIS so a broken service container fails the
build instead of silently skipping.
"""

import uuid

from app.services.job_queue import EVALUATE_SESSION, EvaluationQueue


class _FakeBackgroundTasks:
    def __init__(self) -> None:
        self.tasks: list[tuple] = []

    def add_task(self, func, *args, **kwargs) -> None:
        self.tasks.append((func, args, kwargs))


async def test_the_job_lands_on_the_queue_arq_reads(redis_pool):
    session_id, user_id = uuid.uuid4(), uuid.uuid4()
    background = _FakeBackgroundTasks()

    await EvaluationQueue(redis_pool, background).enqueue(session_id, user_id)

    queued = await redis_pool.queued_jobs()
    assert len(queued) == 1
    # The name the worker registered, and arguments that survived the round
    # trip. A mismatch on either is a job that enqueues cleanly and never runs.
    assert queued[0].function == EVALUATE_SESSION
    assert queued[0].args == (str(session_id), str(user_id))
    assert background.tasks == []


async def test_the_worker_would_accept_what_we_enqueue(redis_pool):
    """The two halves are wired by a string; nothing else checks they agree."""
    from app.worker import WorkerSettings

    await EvaluationQueue(redis_pool, _FakeBackgroundTasks()).enqueue(
        uuid.uuid4(), uuid.uuid4()
    )

    queued = await redis_pool.queued_jobs()
    registered = {f.__name__ for f in WorkerSettings.functions}
    assert queued[0].function in registered


async def test_two_completions_queue_two_jobs(redis_pool):
    """Re-evaluating after completing must not coalesce onto one job id."""
    queue = EvaluationQueue(redis_pool, _FakeBackgroundTasks())
    session_id, user_id = uuid.uuid4(), uuid.uuid4()

    await queue.enqueue(session_id, user_id)
    await queue.enqueue(session_id, user_id)

    assert len(await redis_pool.queued_jobs()) == 2
