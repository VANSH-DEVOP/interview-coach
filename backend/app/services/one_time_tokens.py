"""Minting and redeeming the tokens that go out in emails.

The raw token exists in memory for exactly as long as it takes to put it in an
email. Only its hash is persisted, so this service is the single place that
ever sees both halves -- which is why hashing lives here rather than in the
repository, where a caller could accidentally pass a raw value.
"""

import hashlib
import secrets
import uuid
from datetime import timedelta

from app.core.time import utcnow, utcnow_plus
from app.models.one_time_token import OneTimeToken, TokenPurpose
from app.repositories.one_time_token_repository import OneTimeTokenRepository

# 32 bytes of urandom, urlsafe-encoded. Long enough that guessing is not a
# threat model, short enough to survive an email client wrapping the line.
TOKEN_BYTES = 32

# Reset links are the more dangerous of the two -- anyone holding one can take
# the account -- so they live for minutes. Verification proves an address and
# is harmless to hold, so it gets long enough to survive a spam folder and a
# night's sleep.
LIFETIMES = {
    TokenPurpose.PASSWORD_RESET: timedelta(minutes=30),
    TokenPurpose.EMAIL_VERIFICATION: timedelta(hours=24),
}


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class OneTimeTokenService:
    def __init__(self, tokens: OneTimeTokenRepository) -> None:
        self.tokens = tokens

    async def issue(self, user_id: uuid.UUID, purpose: TokenPurpose) -> str:
        """Mint a token, store its hash, and return the raw value.

        The return value is the only copy. It goes straight into an email and is
        never logged, stored, or returned from an endpoint.
        """
        # Previous links for the same purpose stop working. A resend should
        # replace the old link, not add a second live one.
        await self.tokens.invalidate_outstanding(user_id, purpose)

        raw = secrets.token_urlsafe(TOKEN_BYTES)
        await self.tokens.issue(
            user_id, purpose, hash_token(raw), utcnow_plus(LIFETIMES[purpose])
        )
        return raw

    async def redeem(self, raw: str, purpose: TokenPurpose) -> uuid.UUID | None:
        """Consume a token and return whose it was, or None if unusable.

        One return value for every failure -- unknown, wrong purpose, expired,
        already used. The caller has nothing useful to do with the distinction,
        and telling an anonymous caller which one it was leaks whether a token
        ever existed.
        """
        token = await self.tokens.get_by_hash(hash_token(raw), purpose)
        if token is None or not self._is_usable(token):
            return None

        # Consumed before the caller acts on it, so a concurrent second request
        # with the same link cannot also succeed.
        await self.tokens.consume(token)
        return token.user_id

    @staticmethod
    def _is_usable(token: OneTimeToken) -> bool:
        return token.consumed_at is None and token.expires_at > utcnow()
