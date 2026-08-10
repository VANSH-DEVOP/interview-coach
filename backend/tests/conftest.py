"""Shared test fixtures.

Two kinds of test live here. Most use fakes and need nothing external. The
database-backed ones use the `api` fixture, which runs against a real Postgres
and *skips* rather than fails when one is not reachable -- so `pytest` stays
useful on a laptop with nothing running, while CI gets the real coverage.
"""

import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core import rate_limit
from app.core.config import get_settings
from app.db.session import engine, get_session
from app.main import app
from app.services import evaluation_worker, job_queue, token_pruning

BACKEND_DIR = Path(__file__).resolve().parent.parent

# A dedicated database, never the development one: the fixtures below create
# and migrate it, and a mistake there should not touch real data.
TEST_DB_NAME = "interviewpilot_test"


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


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Rate-limit counters are process-global; leaking them across tests would
    make results depend on execution order."""
    rate_limit.reset()
    yield
    rate_limit.reset()


@pytest.fixture(autouse=True)
def _reset_queue_state():
    """Same reasoning as the rate-limit counters, for the queue fallback count."""
    job_queue.reset()
    yield
    job_queue.reset()


# -- Database-backed fixtures --------------------------------------------------


def _test_database_url() -> str:
    explicit = os.getenv("TEST_DATABASE_URL")
    if explicit:
        return explicit
    settings = get_settings()
    return (
        f"postgresql+asyncpg://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD}"
        f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{TEST_DB_NAME}"
    )


def _with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{name}"))


async def _create_database_if_missing(url: str) -> None:
    """Connect to the maintenance database and create the test one."""
    import asyncpg

    target = urlsplit(url).path.lstrip("/")
    admin_url = _with_database(url, "postgres").replace("postgresql+asyncpg://", "postgresql://")

    connection = await asyncpg.connect(admin_url, timeout=5)
    try:
        exists = await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", target
        )
        if not exists:
            # No parameter binding: CREATE DATABASE forbids it. The name is a
            # module constant, not user input.
            await connection.execute(f'CREATE DATABASE "{target}"')
    finally:
        await connection.close()


def _run_migrations(url: str) -> None:
    from alembic.config import Config

    from alembic import command

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    # Leave the test session's logging alone. Alembic's env.py otherwise calls
    # fileConfig, which replaces the root handlers -- including pytest's capture
    # handler -- so any later test asserting on log output silently sees
    # nothing. See the note in alembic/env.py.
    config.attributes["configure_logging"] = False
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def database_url() -> str:
    """A migrated test database, or skip the tests that need one.

    The schema is built by running the real migrations rather than
    Base.metadata.create_all, so these tests exercise the schema that actually
    ships. A migration that does not apply fails the suite here.
    """
    url = _test_database_url()
    try:
        asyncio.run(_create_database_if_missing(url))
    except Exception as exc:  # noqa: BLE001 - any connection failure means skip
        message = (
            f"No test database reachable ({type(exc).__name__}: {exc}). "
            "Start Postgres and set POSTGRES_HOST/PORT or TEST_DATABASE_URL."
        )
        # CI sets REQUIRE_TEST_DATABASE. Skipping there would turn a broken
        # database service into a green build that ran none of the coverage it
        # was supposed to -- the worst possible outcome for a pipeline.
        if os.getenv("REQUIRE_TEST_DATABASE"):
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    _run_migrations(url)
    return url


@pytest.fixture
async def db_connection(database_url: str):
    """A connection inside an outer transaction that is always rolled back.

    Everything in a test rides on this one connection, which is what makes the
    tests order-independent without truncating tables between them.
    """
    test_engine = create_async_engine(database_url, poolclass=NullPool)
    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield connection
        finally:
            await transaction.rollback()
    await test_engine.dispose()


def _bind_session(connection) -> AsyncSession:
    """A session joined to the test transaction via savepoints.

    `create_savepoint` is what lets application code call commit() normally
    without making anything durable.
    """
    return AsyncSession(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


@pytest.fixture
async def db_session(db_connection):
    """A session whose writes are rolled back at the end of the test."""
    session = _bind_session(db_connection)
    try:
        yield session
    finally:
        await session.close()


@pytest.fixture
async def api(db_session, db_connection, monkeypatch):
    """HTTP client wired to the transactional test session.

    AI is forced off and rate limiting disabled for the duration: these tests
    assert on API behaviour, and neither a live provider (non-deterministic,
    20 requests/day) nor a shared counter belongs in that.

    The evaluation job deliberately opens its *own* session, since by the time
    it runs the request's session is closed. That would step outside the test
    transaction and see none of the test's data, so its session factory is
    pointed at the same connection here. Without this the job silently finds no
    session and every report stays PENDING.

    There is no Redis here and the lifespan does not run under ASGITransport,
    so `app.state.arq_pool` is absent and the queue takes its in-process branch.
    That is what makes a completed interview observably reach COMPLETED inside
    the request: BackgroundTasks run before ASGITransport returns. Tests that
    care about the Redis branch inject a fake pool -- see
    tests/test_job_queue.py.

    Email goes to a recording sender, reachable as the `mailbox` fixture. Tests
    that do not care never notice; the ones that do read the reset link straight
    out of it, which is the same thing a developer does with the log backend.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    # Every module that opens its own session -- the ones that run outside a
    # request -- has to be pointed at the test connection, or it works against
    # the *real* database: invisible to the test, and durable.
    for module in (evaluation_worker, token_pruning):
        monkeypatch.setattr(
            module, "AsyncSessionFactory", lambda: _bind_session(db_connection)
        )

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        app.dependency_overrides.pop(get_session, None)


# -- Redis-backed fixtures -----------------------------------------------------

# A database of its own, and flushed: these tests delete keys, and doing that to
# a developer's default Redis would be unforgivable.
TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
async def redis_pool():
    """A flushed Redis, or skip the tests that need one.

    Same bargain as `database_url`: `pytest` stays useful on a laptop with
    nothing running, and CI sets REQUIRE_TEST_REDIS so a broken service
    container fails the build instead of silently skipping the coverage it was
    supposed to provide.
    """
    from arq import create_pool
    from arq.connections import RedisSettings

    settings = RedisSettings.from_dsn(TEST_REDIS_URL)
    # arq retries five times with a delay by default, which is right at startup
    # and wrong here: with no Redis running it turns an instant skip into a
    # fifteen-second one, on every test that asks for this.
    settings.conn_retries = 1
    settings.conn_retry_delay = 0

    try:
        pool = await create_pool(settings)
    except Exception as exc:  # noqa: BLE001 - any connection failure means skip
        message = (
            f"No test Redis reachable ({type(exc).__name__}: {exc}). "
            f"Start Redis or set TEST_REDIS_URL (currently {TEST_REDIS_URL})."
        )
        if os.getenv("REQUIRE_TEST_REDIS"):
            pytest.fail(message, pytrace=False)
        pytest.skip(message)

    await pool.flushdb()
    try:
        yield pool
    finally:
        await pool.flushdb()
        await pool.aclose()


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    """Point blob storage at a temp directory so tests leave nothing behind.

    get_storage_service is lru_cached and reads STORAGE_LOCAL_PATH at
    construction, so patching the setting alone is not enough -- the cache has
    to be cleared on both sides. Without the second clear, a later test would
    reuse a service rooted at a tmp_path pytest has already deleted.

    Needed by any test whose route constructs storage, which since account
    deletion is no longer only the resume routes: the default root lives under
    /var/lib and is not writable by a developer's user.
    """
    from app.services.storage import get_storage_service

    monkeypatch.setattr(get_settings(), "STORAGE_LOCAL_PATH", tmp_path)
    get_storage_service.cache_clear()
    yield tmp_path
    get_storage_service.cache_clear()


@pytest.fixture
def mailbox(db_session):
    """Captures the emails the app would have sent.

    Overrides the whole AccountService rather than just the sender, because the
    sender is chosen inside a cached factory that the dependency calls directly.
    Must be requested *before* `api` in a test's arguments only if the test
    registers a user through the `registered_user` fixture and wants that
    registration's email -- fixture setup order follows argument order.
    """
    from app.api.deps import get_account_service
    from app.repositories.one_time_token_repository import OneTimeTokenRepository
    from app.repositories.user_repository import UserRepository
    from app.services.account_service import AccountService
    from app.services.email.memory import RecordingEmailSender
    from app.services.one_time_tokens import OneTimeTokenService

    recorder = RecordingEmailSender()

    def _override():
        return AccountService(
            UserRepository(db_session),
            OneTimeTokenService(OneTimeTokenRepository(db_session)),
            recorder,
        )

    app.dependency_overrides[get_account_service] = _override
    try:
        yield recorder
    finally:
        app.dependency_overrides.pop(get_account_service, None)


def token_from(message) -> str:
    """Pull the token out of an emailed link, the way a user clicking it would."""
    from urllib.parse import parse_qs, urlparse

    for word in message.body.split():
        if "token=" in word:
            return parse_qs(urlparse(word).query)["token"][0]
    raise AssertionError(f"No token link in email body:\n{message.body}")


@pytest.fixture
async def registered_user(api):
    """A registered, logged-in user. Returns (client_headers, user_dict)."""
    import uuid

    email = f"user-{uuid.uuid4().hex[:12]}@example.com"
    password = "correct-horse-battery"

    register = await api.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Test User"},
    )
    assert register.status_code == 201, register.text

    login = await api.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert login.status_code == 200, login.text
    tokens = login.json()

    return {
        "headers": {"Authorization": f"Bearer {tokens['access_token']}"},
        "user": register.json(),
        "email": email,
        "password": password,
        "tokens": tokens,
    }
