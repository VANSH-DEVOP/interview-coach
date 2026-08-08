import pytest
from httpx import ASGITransport, AsyncClient

from app.db.session import engine
from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
async def _dispose_engine():
    """Drop pooled connections between tests.

    The engine is created at module scope in app.db.session, but pytest-asyncio
    gives each test its own event loop. asyncpg binds pooled connections to the
    loop that opened them, so the second test to touch the database inherits a
    connection from a closed loop and fails with "attached to a different loop".

    Only one HTTP test touched the database before, which is why this never
    surfaced. Disposing after each test keeps the pool loop-local.
    """
    yield
    await engine.dispose()
