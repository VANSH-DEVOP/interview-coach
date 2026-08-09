"""Authentication business logic: registration, login, refresh, logout.

Refresh tokens **rotate**: every successful refresh revokes the token that was
presented and issues a new one, so a stolen token is useful only until the
legitimate client refreshes next. Rotation is also what makes theft detectable
-- see `refresh` below.
"""

import logging
import uuid

import jwt as pyjwt

from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import TokenPair
from app.schemas.user import UserCreate

logger = logging.getLogger(__name__)


class AuthService:
    def __init__(self, users: UserRepository, tokens: RefreshTokenRepository) -> None:
        self.users = users
        self.tokens = tokens

    async def register(self, payload: UserCreate) -> User:
        email = payload.email.lower()
        if await self.users.get_by_email(email) is not None:
            raise ConflictError("An account with this email already exists.")
        user = User(
            email=email,
            hashed_password=hash_password(payload.password),
            full_name=payload.full_name,
        )
        return await self.users.add(user)

    async def login(self, email: str, password: str) -> TokenPair:
        user = await self.users.get_by_email(email)
        # Constant-shape flow: same error for unknown email and bad password.
        if user is None or not verify_password(password, user.hashed_password):
            raise UnauthorizedError("Invalid email or password.")
        if not user.is_active:
            raise UnauthorizedError("This account is deactivated.")
        return await self._issue_tokens(user.id)

    async def refresh(self, refresh_token: str) -> TokenPair:
        """Rotate a refresh token, or reject it.

        A token that decodes but has already been revoked is the interesting
        case. The legitimate client rotated it away, so whoever is presenting it
        now either replayed a captured token or is the real client racing
        itself. Both are indistinguishable from here, and the safe reading is
        theft: every token for the account is revoked, forcing a fresh login
        that an attacker without the password cannot complete.
        """
        record, user = await self._resolve(refresh_token)

        if record.revoked_at is not None:
            revoked = await self.tokens.revoke_all_for_user(user.id)
            logger.warning(
                "Refresh token reuse for user %s; revoked %d token(s). The token "
                "had already been rotated, so it was either replayed or captured.",
                user.id,
                revoked,
            )
            raise UnauthorizedError("This session has expired. Please sign in again.")

        await self.tokens.revoke(record)
        return await self._issue_tokens(user.id)

    async def logout(self, refresh_token: str, *, everywhere: bool = False) -> None:
        """Revoke the presented token, or all of the user's.

        Deliberately quiet about a token that is already invalid: logging out
        twice, or with a token the server has forgotten, should leave the client
        signed out either way. Turning that into an error only teaches clients
        to ignore the response.
        """
        try:
            record, user = await self._resolve(refresh_token)
        except UnauthorizedError:
            return

        if everywhere:
            await self.tokens.revoke_all_for_user(user.id)
        else:
            await self.tokens.revoke(record)

    async def revoke_all(self, user_id: uuid.UUID) -> int:
        """Sign a user out of every device. For password change and reset."""
        return await self.tokens.revoke_all_for_user(user_id)

    async def issue_for(self, user_id: uuid.UUID) -> TokenPair:
        """A fresh pair for an already-authenticated user.

        Lets a password change revoke everything and still leave the caller
        signed in, rather than logging them out of the device they just used.
        """
        return await self._issue_tokens(user_id)

    # -- internals -------------------------------------------------------------

    async def _resolve(self, refresh_token: str) -> tuple[RefreshToken, User]:
        """Decode a refresh token and load its record and user.

        Raises UnauthorizedError with one message for every failure mode --
        malformed, expired, wrong type, unknown, deactivated. The distinctions
        matter to us and not to the caller, and spelling them out tells an
        attacker which half of a guess was right.
        """
        invalid = UnauthorizedError("Invalid or expired refresh token.")
        try:
            payload = decode_token(refresh_token, expected_type="refresh")
        except pyjwt.InvalidTokenError as exc:
            raise invalid from exc

        record = await self.tokens.get_by_jti(payload.get("jti", ""))
        if record is None:
            # Signature is valid but the server has no record. Either the row
            # was pruned after expiry, or the token predates the revocation list
            # existing at all. Both mean "log in again".
            raise invalid

        user = await self.users.get(uuid.UUID(payload["sub"]))
        if user is None or not user.is_active:
            raise UnauthorizedError("Account is no longer active.")
        return record, user

    async def _issue_tokens(self, user_id: uuid.UUID) -> TokenPair:
        subject = str(user_id)
        refresh = create_refresh_token(subject)
        # Recorded before the token reaches the client: a token the client holds
        # and the server has no row for is one that can never be refreshed.
        await self.tokens.issue(user_id, refresh.jti, refresh.expires_at)
        return TokenPair(
            access_token=create_access_token(subject),
            refresh_token=refresh.token,
        )
