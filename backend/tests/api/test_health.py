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


# -- Retrieval -----------------------------------------------------------------


async def test_rag_reports_why_retrieval_is_off(client, stub_db_up):
    """The quietest degradation in the system: with RAG off, questions are still
    generated from truncated resume text and look fine."""
    from app.services.ai import retrieval_metrics

    retrieval_metrics.reset()
    retrieval_metrics.record_availability(enabled=False, reason="init_failed: OSError")
    try:
        body = (await client.get("/api/v1/health")).json()
    finally:
        retrieval_metrics.reset()

    assert body["rag"]["enabled"] is False
    # The usual cause is an unwritable CHROMA_PATH, and the only other symptom
    # is that the questions feel generic.
    assert body["rag"]["disabled_reason"] == "init_failed: OSError"
    # Reported, not acted on -- the app is still answering correctly.
    assert body["status"] == "ok"


async def test_rag_counters_start_at_zero_and_are_reported(client, stub_db_up):
    from app.services.ai import retrieval_metrics

    retrieval_metrics.reset()
    body = (await client.get("/api/v1/health")).json()

    rag = body["rag"]
    assert rag["enabled"] is None  # nothing has tried to build the service yet
    assert (rag["retrievals"], rag["hits"], rag["full_text_fallbacks"]) == (0, 0, 0)
    assert rag["avg_ms"] is None


async def test_an_unreadable_heartbeat_is_unknown_not_dead(client, stub_db_up, arq_pool):
    """Redis going away is already reported as a queue fallback. Calling the
    worker dead on top of that would be a second alarm for one fault, and a
    wrong one -- the worker may be running fine."""
    arq_pool(raises=True)

    body = (await client.get("/api/v1/health")).json()

    assert body["worker"]["alive"] is None
    assert body["status"] == "ok"


async def test_ai_telemetry_is_reported(client, stub_db_up):
    """Latency, token spend and both rates, in the block an operator reads."""
    from app.services.ai import call_metrics, degradation

    call_metrics.reset()
    degradation.reset()
    degradation.record_attempt("initial_questions")
    degradation.record_attempt("initial_questions")
    degradation.record_fallback("initial_questions", RuntimeError("HTTP 429"))
    call_metrics.record_call(
        operation="generate",
        model="gemini-flash-latest",
        outcome="ok",
        duration_ms=250.0,
        input_tokens=800,
        output_tokens=200,
    )
    call_metrics.record_call(
        operation="embed", model="models/gemini-embedding-001", outcome="ok", duration_ms=50.0
    )

    ai = (await client.get("/api/v1/health")).json()["ai"]

    # The number to alert on: a count alone cannot distinguish one bad hour
    # from a dead provider.
    assert ai["attempts"] == 2
    assert ai["fallback_rate"] == 0.5

    calls = ai["calls"]
    assert calls["calls"] == 2
    assert calls["failure_rate"] == 0.0
    assert calls["avg_ms"] == 150.0
    # Only the chat call reports usage; the embedding contributes none rather
    # than a zero that would understate spend per call.
    assert (calls["input_tokens"], calls["output_tokens"]) == (800, 200)
    assert calls["by_operation"]["embed"]["input_tokens"] is None
    # Integers stay integers rather than being coerced to floats.
    assert calls["by_operation"]["generate"]["calls"] == 1

    call_metrics.reset()
    degradation.reset()


async def test_ai_rates_are_null_before_anything_is_attempted(client, stub_db_up):
    """Zero would read as a healthy provider on a deployment that has never
    called one."""
    from app.services.ai import call_metrics, degradation

    call_metrics.reset()
    degradation.reset()

    ai = (await client.get("/api/v1/health")).json()["ai"]

    assert ai["fallback_rate"] is None
    assert ai["calls"]["failure_rate"] is None
    assert ai["calls"]["avg_ms"] is None
