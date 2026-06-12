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
