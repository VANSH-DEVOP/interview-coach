"""Queries over the refresh-token revocation list."""

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Executable, delete, select, update
from sqlalchemy.engine import CursorResult

from app.core.time import utcnow
from app.models.refresh_token import RefreshToken
from app.repositories.base import BaseRepository


class RefreshTokenRepository(BaseRepository[RefreshToken]):
    model = RefreshToken

    async def get_by_jti(self, jti: str) -> RefreshToken | None:
        stmt = select(RefreshToken).where(RefreshToken.jti == jti)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def issue(
        self, user_id: uuid.UUID, jti: str, expires_at: datetime
    ) -> RefreshToken:
        return await self.add(
            RefreshToken(user_id=user_id, jti=jti, expires_at=expires_at)
        )

    async def revoke(self, token: RefreshToken) -> None:
        """Idempotent: re-revoking keeps the original timestamp."""
        if token.revoked_at is None:
            token.revoked_at = utcnow()
            await self.session.flush()

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        """Revoke every live token for a user. Returns how many were affected.

        The workhorse behind logout-everywhere, password change, password reset
        and reuse detection. Already-revoked rows are excluded so their original
        timestamps survive.
        """
        return await self._execute_rowcount(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )

    async def delete_expired(self) -> int:
        """Drop rows that can no longer authenticate anything.

        The list only needs to cover tokens that could still be presented; past
        `expires_at` the JWT fails on its own `exp` claim before the lookup
        happens. Without this the table grows forever, one row per login.
        """
        return await self._execute_rowcount(
            delete(RefreshToken).where(RefreshToken.expires_at < utcnow())
        )

    async def _execute_rowcount(self, statement: Executable) -> int:
        """Run a bulk UPDATE/DELETE and report how many rows it touched.

        The cast is because `AsyncSession.execute` is typed as returning the
        generic `Result`, which has no `rowcount`; a DML statement always yields
        a `CursorResult`, which does.
        """
        result = cast(CursorResult[Any], await self.session.execute(statement))
        await self.session.flush()
        return result.rowcount or 0
