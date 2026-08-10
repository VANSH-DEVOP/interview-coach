"""Rate-limit counters against a real Redis.

tests/test_rate_limit.py covers the window arithmetic and the fallback, which a
fake proves as well as anything. What it cannot prove is the reason for moving
the counters at all: that two processes share one budget, and that the Lua does
what its comment claims -- counting and expiring in the same atomic step, so no
key can outlive its window and lock its subject out for good.

The `redis_pool` fixture (tests/conftest.py) skips when no Redis is reachable;
CI sets REQUIRE_TEST_REDIS to make that a failure instead.
"""

import pytest

from app.core import rate_limit
from app.core.config import get_settings
from app.core.exceptions import RateLimitedError


@pytest.fixture(autouse=True)
def _limits(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "RATE_LIMIT_AUTH_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(settings, "RATE_LIMIT_AUTH_WINDOW_SECONDS", 300, raising=False)
    rate_limit.reset()
    yield
    rate_limit.reset()


async def test_the_limit_survives_a_restart_of_the_counting_process(redis_pool):
    """The whole point of the move.

    `rate_limit.reset()` is a fresh process: a new replica, or the same one
    after a deploy. With per-process counters the budget resets with it, so a
    client gets a full allowance from every replica it happens to reach.
    """
    for _ in range(3):
        await rate_limit.enforce("1.2.3.4", scope="auth", redis=redis_pool)

    rate_limit.reset()

    with pytest.raises(RateLimitedError):
        await rate_limit.enforce("1.2.3.4", scope="auth", redis=redis_pool)


async def test_two_processes_share_one_budget(redis_pool):
    """Spend the budget across two 'replicas' rather than in one."""
    await rate_limit.enforce("1.2.3.4", scope="auth", redis=redis_pool)
    rate_limit.reset()
    await rate_limit.enforce("1.2.3.4", scope="auth", redis=redis_pool)
    rate_limit.reset()
    await rate_limit.enforce("1.2.3.4", scope="auth", redis=redis_pool)

    with pytest.raises(RateLimitedError):
        await rate_limit.enforce("1.2.3.4", scope="auth", redis=redis_pool)


async def test_the_counter_always_carries_an_expiry(redis_pool):
    """A counter with no TTL never resets, and locks its subject out forever.

    That is the failure INCR-then-EXPIRE from the client can leave behind, and
    the reason the two live in one script.
    """
    await rate_limit.enforce("1.2.3.4", scope="auth", redis=redis_pool)

    ttl = await redis_pool.ttl("ratelimit:auth:1.2.3.4")
    assert 0 < ttl <= 300


async def test_the_window_is_fixed_not_extended_by_later_hits(redis_pool):
    """Each hit must not push the reset further out, or a client hammering the
    endpoint is locked out for as long as it keeps trying."""
    await rate_limit.enforce("1.2.3.4", scope="auth", redis=redis_pool)
    first = await redis_pool.pttl("ratelimit:auth:1.2.3.4")

    for _ in range(5):
        try:
            await rate_limit.enforce("1.2.3.4", scope="auth", redis=redis_pool)
        except RateLimitedError:
            pass

    assert await redis_pool.pttl("ratelimit:auth:1.2.3.4") <= first


async def test_retry_after_comes_from_the_shared_expiry(redis_pool):
    for _ in range(3):
        await rate_limit.enforce("1.2.3.4", scope="auth", redis=redis_pool)

    with pytest.raises(RateLimitedError) as excinfo:
        await rate_limit.enforce("1.2.3.4", scope="auth", redis=redis_pool)

    retry_after = int(excinfo.value.headers["Retry-After"])
    assert 0 < retry_after <= 301


async def test_keys_and_scopes_do_not_share_a_budget(redis_pool):
    for _ in range(3):
        await rate_limit.enforce("1.2.3.4", scope="auth", redis=redis_pool)

    # A different client, and the same client in a different scope, are both
    # untouched. Sharing either would let auth traffic exhaust the AI budget.
    await rate_limit.enforce("5.6.7.8", scope="auth", redis=redis_pool)
    await rate_limit.enforce("1.2.3.4", scope="ai", redis=redis_pool)


async def test_the_counters_stay_out_of_arqs_namespace(redis_pool):
    """One Redis holds the queue and these counters; a collision would corrupt
    whichever lost."""
    await rate_limit.enforce("1.2.3.4", scope="auth", redis=redis_pool)

    keys = [key.decode() for key in await redis_pool.keys("*")]
    assert keys == ["ratelimit:auth:1.2.3.4"]
    assert not any(key.startswith("arq:") for key in keys)
