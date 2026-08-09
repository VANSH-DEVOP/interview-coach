"""Queries over emailed single-use tokens."""

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Executable, delete, select, update
from sqlalchemy.engine import CursorResult

from app.core.time import utcnow
from app.models.one_time_token import OneTimeToken, TokenPurpose
from app.repositories.base import BaseRepository


class OneTimeTokenRepository(BaseRepository[OneTimeToken]):
    model = OneTimeToken

    async def get_by_hash(
        self, token_hash: str, purpose: TokenPurpose
    ) -> OneTimeToken | None:
        """Look a token up. `purpose` is required, never defaulted.

        One table serves password reset and email verification, so a lookup
        that forgot to filter would let a verification link set a password.
        """
        stmt = select(OneTimeToken).where(
            OneTimeToken.token_hash == token_hash, OneTimeToken.purpose == purpose
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def issue(
        self,
        user_id: uuid.UUID,
        purpose: TokenPurpose,
        token_hash: str,
        expires_at: datetime,
    ) -> OneTimeToken:
        return await self.add(
            OneTimeToken(
                user_id=user_id,
                purpose=purpose,
                token_hash=token_hash,
                expires_at=expires_at,
            )
        )

    async def consume(self, token: OneTimeToken) -> None:
        token.consumed_at = utcnow()
        await self.session.flush()

    async def invalidate_outstanding(
        self, user_id: uuid.UUID, purpose: TokenPurpose
    ) -> int:
        """Consume any live tokens of this purpose before issuing another.

        Otherwise every "resend" leaves another working link in another inbox
        message, and the oldest one stays valid for its full lifetime. Only the
        most recent link should work.
        """
        return await self._execute_rowcount(
            update(OneTimeToken)
            .where(
                OneTimeToken.user_id == user_id,
                OneTimeToken.purpose == purpose,
                OneTimeToken.consumed_at.is_(None),
            )
            .values(consumed_at=utcnow())
        )

    async def delete_expired(self) -> int:
        """Drop rows that can no longer be redeemed."""
        return await self._execute_rowcount(
            delete(OneTimeToken).where(OneTimeToken.expires_at < utcnow())
        )

    async def _execute_rowcount(self, statement: Executable) -> int:
        result = cast(CursorResult[Any], await self.session.execute(statement))
        await self.session.flush()
        return result.rowcount or 0
