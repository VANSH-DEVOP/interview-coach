"""The arq worker's retry policy.

`run_evaluation` swallows failures and writes FAILED, which is right when there
is nothing to retry. The worker must do the opposite on every attempt but the
last: a swallowed exception is a job arq considers successful, so retries would
silently never happen and the queue would buy nothing over BackgroundTasks.
"""

import uuid

import pytest

from app import worker
from app.core.config import get_settings
from app.services.evaluation_worker import Reconciliation
from app.services.token_pruning import Pruned


@pytest.fixture
def session_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def spy(monkeypatch):
    """Records what the worker did without touching a database or a provider."""
    calls: dict[str, list] = {"evaluated": [], "failed": []}

    async def fake_evaluate(sid, uid):
        calls["evaluated"].append((sid, uid))
        if calls.get("error"):
            raise calls["error"][0]

    async def fake_mark_failed(sid):
        calls["failed"].append(sid)

    monkeypatch.setattr(worker, "evaluate", fake_evaluate)
    monkeypatch.setattr(worker, "mark_failed", fake_mark_failed)
    return calls


async def test_a_successful_job_evaluates_and_marks_nothing_failed(spy, session_id):
    user_id = uuid.uuid4()

    await worker.evaluate_session({"job_try": 1}, str(session_id), str(user_id))

    assert spy["evaluated"] == [(session_id, user_id)]
    assert spy["failed"] == []


async def test_ids_are_parsed_back_into_uuids(spy, session_id):
    """They cross the queue as strings; the evaluator's signature wants UUIDs."""
    await worker.evaluate_session({"job_try": 1}, str(session_id), str(uuid.uuid4()))

    sid, uid = spy["evaluated"][0]
    assert isinstance(sid, uuid.UUID) and isinstance(uid, uuid.UUID)


async def test_a_non_final_failure_is_raised_so_arq_retries(spy, session_id):
    spy["error"] = [RuntimeError("provider timed out")]

    with pytest.raises(RuntimeError):
        await worker.evaluate_session({"job_try": 1}, str(session_id), str(uuid.uuid4()))

    # Crucially not marked FAILED: a retry is still coming, and the user would
    # have been shown a dead report with a retry button for work still in flight.
    assert spy["failed"] == []


async def test_the_final_failure_marks_the_report_and_stops(spy, session_id):
    spy["error"] = [RuntimeError("provider timed out")]
    last = get_settings().EVALUATION_MAX_TRIES

    # No raise: arq has exhausted its tries, so re-raising only adds noise.
    await worker.evaluate_session({"job_try": last}, str(session_id), str(uuid.uuid4()))

    assert spy["failed"] == [session_id]


async def test_a_try_past_the_limit_still_marks_failed(spy, session_id):
    """Defensive: max_tries is configurable and arq owns the counter."""
    spy["error"] = [RuntimeError("provider timed out")]
    beyond = get_settings().EVALUATION_MAX_TRIES + 5

    await worker.evaluate_session({"job_try": beyond}, str(session_id), str(uuid.uuid4()))

    assert spy["failed"] == [session_id]


def test_worker_settings_expose_what_arq_reads():
    """arq pulls these off the class as Worker kwargs; a callable or a missing
    name is a worker that starts and never runs the right function."""
    settings = get_settings()

    assert worker.evaluate_session in worker.WorkerSettings.functions
    assert worker.WorkerSettings.max_tries == settings.EVALUATION_MAX_TRIES
    assert worker.WorkerSettings.job_timeout == settings.EVALUATION_JOB_TIMEOUT_SECONDS
    # A RedisSettings instance, not a factory -- arq does not call it.
    assert not callable(worker.WorkerSettings.redis_settings)


def test_the_registered_name_matches_what_the_api_enqueues():
    """arq resolves jobs by string. A rename on one side alone gives jobs that
    enqueue cleanly and are never executed -- reports stuck PENDING forever."""
    from app.services.job_queue import EVALUATE_SESSION

    assert worker.evaluate_session.__name__ == EVALUATE_SESSION


# -- The reconciliation cron ---------------------------------------------------


class _FakePool:
    """Stands in for ctx["redis"], recording what the sweep queued."""

    def __init__(self) -> None:
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, *args))


async def test_the_cron_enqueues_under_the_name_the_worker_consumes(monkeypatch):
    """The sweep is pointless if it queues jobs nothing will pick up."""
    pool = _FakePool()
    session_id, user_id = uuid.uuid4(), uuid.uuid4()

    async def fake_reconcile(enqueue):
        await enqueue(session_id, user_id)
        return Reconciliation(requeued=1)

    monkeypatch.setattr(worker, "reconcile_stale_reports", fake_reconcile)

    await worker.reconcile_reports({"redis": pool})

    from app.services.job_queue import EVALUATE_SESSION

    # Strings, not UUIDs: the payload must not depend on the serialiser.
    assert pool.jobs == [(EVALUATE_SESSION, str(session_id), str(user_id))]


def _cron(name: str):
    return next(j for j in worker.WorkerSettings.cron_jobs if j.name == f"cron:{name}")


def test_both_crons_are_registered():
    """A cron that is written but never listed does nothing at all, silently."""
    assert {job.name for job in worker.WorkerSettings.cron_jobs} == {
        "cron:reconcile_reports",
        "cron:prune_tokens",
    }
    # One replica per tick for both; several running at once would queue each
    # orphan once per replica, and have two transactions deleting one set of rows.
    assert all(job.unique for job in worker.WorkerSettings.cron_jobs)


def test_the_reconciliation_cron_runs_often_and_at_startup():
    sweep = _cron("reconcile_reports")

    assert sweep.run_at_startup is True
    assert sweep.minute == set(range(0, 60, worker.RECONCILE_EVERY_MINUTES))


def test_the_prune_cron_runs_hourly_off_the_reconciliation_tick():
    prune = _cron("prune_tokens")

    # Hourly: no hour constraint, one minute.
    assert prune.hour is None
    assert prune.minute == worker.PRUNE_TOKENS_AT_MINUTE
    # Nothing is broken while expired rows sit there, so a restart need not scan.
    assert prune.run_at_startup is False
    # Off the reconciliation schedule on purpose: both open database sessions.
    assert worker.PRUNE_TOKENS_AT_MINUTE % worker.RECONCILE_EVERY_MINUTES != 0


async def test_the_prune_cron_calls_the_pruner(monkeypatch):
    called: list[bool] = []

    async def fake_prune():
        called.append(True)
        return Pruned(refresh_tokens=2, one_time_tokens=1)

    monkeypatch.setattr(worker, "prune_expired_tokens", fake_prune)

    await worker.prune_tokens({})

    assert called == [True]


def test_the_worker_heartbeats_often_enough_to_notice_a_death():
    """arq's default is an hour, which cannot answer "is anything draining the
    queue right now". /health and `arq --check` both read this key."""
    assert worker.WorkerSettings.health_check_interval == worker.HEALTH_CHECK_INTERVAL_SECONDS
    assert worker.HEALTH_CHECK_INTERVAL_SECONDS <= 60


def test_the_sweep_interval_leaves_room_for_a_full_retry_cycle():
    """The staleness window has to outlast the work it is looking for.

    Below max_tries * job_timeout, the sweep re-queues evaluations that are
    still running and two workers score the same session at once.
    """
    settings = get_settings()

    worst_case = settings.EVALUATION_MAX_TRIES * settings.EVALUATION_JOB_TIMEOUT_SECONDS
    assert settings.EVALUATION_STALE_AFTER_SECONDS > worst_case
    assert settings.EVALUATION_STALE_GIVE_UP_SECONDS > settings.EVALUATION_STALE_AFTER_SECONDS
