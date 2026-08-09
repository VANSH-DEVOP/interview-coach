"""Deleting token rows that can no longer authenticate or be redeemed.

Both token tables grow without bound in normal use: `refresh_tokens` gains a
row per login and per rotation, `one_time_tokens` one per password reset or
verification email. Nothing in the request path can clean them up -- a login
cannot be asked to pay for a table scan, and the rows it would delete belong to
other users -- so this runs on the worker's cron.

What is deliberately *not* deleted is anything unexpired. A revoked refresh
token is still consulted (that is what makes logout work), and a consumed
one-time token is what makes a replayed reset link fail rather than mint a
second password change. Past `expires_at` both are refused on the timestamp
alone, so the row stops carrying information and can go.

Like the evaluation worker, this runs outside any request and opens its own
session; the repositories flush, so the commit belongs here.
"""

import logging
from dataclasses import dataclass

from app.db.session import AsyncSessionFactory
from app.repositories.one_time_token_repository import OneTimeTokenRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Pruned:
    """Rows deleted by one pass, per table."""

    refresh_tokens: int = 0
    one_time_tokens: int = 0

    def __bool__(self) -> bool:
        return bool(self.refresh_tokens or self.one_time_tokens)


async def prune_expired_tokens() -> Pruned:
    """Delete expired rows from both token tables. Never raises.

    Returning zeros on failure rather than propagating: this runs on a cron, and
    an exception would take the sweep down until the worker restarts. A table
    that keeps its expired rows for another hour costs nothing.
    """
    try:
        async with AsyncSessionFactory() as db:
            refresh = await RefreshTokenRepository(db).delete_expired()
            one_time = await OneTimeTokenRepository(db).delete_expired()
            await db.commit()
    except Exception:
        logger.exception("Could not prune expired tokens.")
        return Pruned()

    pruned = Pruned(refresh_tokens=refresh, one_time_tokens=one_time)
    if pruned:
        logger.info(
            "Pruned expired tokens: %d refresh, %d one-time.",
            pruned.refresh_tokens,
            pruned.one_time_tokens,
        )
    return pruned
