"""Pruning expired token rows.

Both token tables grow with use and nothing in the request path can clean them
up, so a cron does it. The interesting cases are not the deletions -- they are
the rows that must survive, because each one is what makes a security control
work. A prune that is slightly too eager silently un-revokes a logout or lets a
password-reset link be replayed.
"""

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.core.time import utcnow
from app.models.one_time_token import OneTimeToken
from app.models.refresh_token import RefreshToken
from app.services import token_pruning
from app.services.token_pruning import prune_expired_tokens


async def _count(db_session, model) -> int:
    return (await db_session.execute(select(func.count()).select_from(model))).scalar_one()


async def _expire(db_session, model) -> None:
    """Move every row's expiry into the past."""
    await db_session.execute(update(model).values(expires_at=utcnow() - timedelta(days=1)))
    await db_session.commit()


@pytest.fixture
async def tokens(api, registered_user, db_session):
    """Registration mints a verification token; logging in mints a refresh one."""
    assert await _count(db_session, RefreshToken) == 1
    assert await _count(db_session, OneTimeToken) == 1
    return registered_user


async def test_expired_rows_are_deleted(tokens, db_session):
    await _expire(db_session, RefreshToken)
    await _expire(db_session, OneTimeToken)

    pruned = await prune_expired_tokens()

    assert (pruned.refresh_tokens, pruned.one_time_tokens) == (1, 1)
    assert await _count(db_session, RefreshToken) == 0
    assert await _count(db_session, OneTimeToken) == 0


async def test_live_rows_are_untouched(tokens, db_session):
    pruned = await prune_expired_tokens()

    assert not pruned
    assert await _count(db_session, RefreshToken) == 1
    assert await _count(db_session, OneTimeToken) == 1


async def test_a_revoked_refresh_token_survives_until_it_expires(tokens, api, db_session):
    """Revocation lives in that row. Deleting it early un-revokes the token,
    and the logout the user performed stops meaning anything."""
    await api.post("/api/v1/auth/logout", json={"refresh_token": tokens["tokens"]["refresh_token"]})

    pruned = await prune_expired_tokens()

    assert pruned.refresh_tokens == 0
    assert await _count(db_session, RefreshToken) == 1
    revoked = (await db_session.execute(select(RefreshToken))).scalar_one()
    await db_session.refresh(revoked)
    assert revoked.revoked_at is not None


async def test_a_consumed_one_time_token_survives_until_it_expires(
    mailbox, api, registered_user, db_session
):
    """A replayed reset link must find a consumed row and be refused. With the
    row gone, the token is simply unknown -- which is the same answer, but only
    by luck; the row is the record that it was used."""
    from tests.conftest import token_from

    await api.post("/api/v1/auth/forgot-password", json={"email": registered_user["email"]})
    message = mailbox.last_to(registered_user["email"])
    assert message is not None, "no reset email was sent"
    token = token_from(message)
    reset = await api.post(
        "/api/v1/auth/reset-password",
        json={"token": token, "new_password": "a-brand-new-passphrase"},
    )
    assert reset.status_code in (200, 204), reset.text

    pruned = await prune_expired_tokens()

    assert pruned.one_time_tokens == 0
    consumed = (
        await db_session.execute(
            select(func.count()).select_from(OneTimeToken).where(OneTimeToken.consumed_at.isnot(None))
        )
    ).scalar_one()
    assert consumed == 1


async def test_pruning_survives_an_unreachable_database(monkeypatch):
    """It runs on a cron; raising would stop every later prune until a restart."""

    def _boom():
        raise ConnectionError("database is not up yet")

    monkeypatch.setattr(token_pruning, "AsyncSessionFactory", _boom)

    assert not await prune_expired_tokens()
