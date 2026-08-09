"""Health endpoint contract tests (DB stubbed; no live database required)."""

from unittest.mock import AsyncMock

import pytest

from app.db.session import get_session
from app.main import app


@pytest.fixture
def stub_db_up():
    session = AsyncMock()
    session.execute = AsyncMock()

    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def stub_db_down():
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=ConnectionError("db unreachable"))

    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    yield
    app.dependency_overrides.clear()


async def test_health_ok(client, stub_db_up):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "up"


async def test_health_degraded_when_db_down(client, stub_db_down):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] == "down"


# -- Worker liveness -----------------------------------------------------------
#
# The failure this covers is a silent one: with the worker gone the API still
# answers every request correctly, and reports simply never finish.


@pytest.fixture
def arq_pool(monkeypatch):
    """Install a fake pool on app.state, the way the lifespan would."""

    def _install(heartbeat: object = None, *, raises: bool = False):
        pool = AsyncMock()
        pool.get = AsyncMock(
            side_effect=ConnectionError("redis unreachable") if raises else None,
            return_value=heartbeat,
        )
        monkeypatch.setattr(app.state, "arq_pool", pool, raising=False)
        return pool

    return _install


async def test_no_queue_configured_means_no_worker_to_miss(client, stub_db_up):
    """In-process evaluation is the intended design without Redis, not a fault."""
    response = await client.get("/api/v1/health")

    worker = response.json()["worker"]
    assert worker["expected"] is False
    assert worker["alive"] is None
    assert response.json()["status"] == "ok"


async def test_a_live_worker_reports_its_heartbeat(client, stub_db_up, arq_pool):
    arq_pool(b"Aug-09 20:35:00 j_complete=4 j_failed=0 j_retried=0 j_ongoing=0 queued=0")

    body = (await client.get("/api/v1/health")).json()

    assert body["worker"] == {
        "expected": True,
        "alive": True,
        # arq's own summary, passed through rather than parsed.
        "detail": "Aug-09 20:35:00 j_complete=4 j_failed=0 j_retried=0 j_ongoing=0 queued=0",
    }
    assert body["status"] == "ok"


async def test_a_dead_worker_degrades_the_health_status(client, stub_db_up, arq_pool):
    """arq deletes the key on shutdown and lets it expire otherwise, so a
    missing heartbeat is the whole signal."""
    arq_pool(heartbeat=None)

    body = (await client.get("/api/v1/health")).json()

    assert body["worker"]["alive"] is False
    # Unlike an AI fallback, nothing downstream covers for this: reports queue
    # up and stay PENDING until a human notices.
    assert body["status"] == "degraded"


async def test_an_unreadable_heartbeat_is_unknown_not_dead(client, stub_db_up, arq_pool):
    """Redis going away is already reported as a queue fallback. Calling the
    worker dead on top of that would be a second alarm for one fault, and a
    wrong one -- the worker may be running fine."""
    arq_pool(raises=True)

    body = (await client.get("/api/v1/health")).json()

    assert body["worker"]["alive"] is None
    assert body["status"] == "ok"
