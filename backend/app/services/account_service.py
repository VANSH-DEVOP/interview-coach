"""Flows that combine a user, an emailed token and a session revocation.

Password reset and email verification both span three collaborators, which is
more than either the auth service or the user service should reach for. They
live together here because they share the same delicate property: **an
anonymous caller must not be able to learn whether an address has an account.**

That constraint is why several methods below succeed silently on inputs that
did nothing.
"""

import logging
import uuid

from app.core.security import hash_password
from app.core.time import utcnow
from app.models.one_time_token import TokenPurpose
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.services.email import templates
from app.services.email.base import EmailSender
from app.services.one_time_tokens import OneTimeTokenService

logger = logging.getLogger(__name__)

# Human-readable forms of the lifetimes in one_time_tokens.LIFETIMES, for the
# email copy. Telling someone a link expires without saying when is useless.
VALID_FOR = {
    TokenPurpose.PASSWORD_RESET: "30 minutes",
    TokenPurpose.EMAIL_VERIFICATION: "24 hours",
}


class AccountService:
    def __init__(
        self,
        users: UserRepository,
        tokens: OneTimeTokenService,
        email: EmailSender,
    ) -> None:
        self.users = users
        self.tokens = tokens
        self.email = email

    # -- Password reset --------------------------------------------------------

    async def request_password_reset(self, email: str) -> None:
        """Email a reset link, if there is an account to email.

        Returns None either way, and the caller returns the same response either
        way. An endpoint that 404s on unknown addresses is an endpoint that
        confirms which of a leaked address list are registered here.
        """
        user = await self.users.get_by_email(email.lower())
        if user is None or not user.is_active:
            logger.info("Password reset requested for an address with no account.")
            return

        token = await self.tokens.issue(user.id, TokenPurpose.PASSWORD_RESET)
        await self._send(
            templates.password_reset(
                user.email, token, valid_for=VALID_FOR[TokenPurpose.PASSWORD_RESET]
            )
        )

    async def reset_password(self, token: str, new_password: str) -> uuid.UUID | None:
        """Redeem a reset token and set the password. Returns the user id.

        None means the token was unusable. Revoking the user's sessions is the
        caller's job -- it needs the auth service, and this one deliberately
        does not.
        """
        user_id = await self.tokens.redeem(token, TokenPurpose.PASSWORD_RESET)
        if user_id is None:
            return None

        user = await self.users.get(user_id)
        if user is None or not user.is_active:
            return None

        user.hashed_password = hash_password(new_password)
        # Receiving the link proves control of the mailbox, which is the same
        # thing verification asks for. Making the user click a second link to
        # prove what they just proved would be theatre.
        if user.email_verified_at is None:
            user.email_verified_at = utcnow()
        await self.users.add(user)
        return user.id

    # -- Email verification ----------------------------------------------------

    async def send_verification(self, user: User) -> None:
        """Email a verification link. Safe to call for an already-verified user.

        Never raises: this runs during registration, and a mail server having a
        bad afternoon must not fail an account creation that otherwise worked.
        """
        if user.email_verified_at is not None:
            return

        token = await self.tokens.issue(user.id, TokenPurpose.EMAIL_VERIFICATION)
        await self._send(
            templates.email_verification(
                user.email, token, valid_for=VALID_FOR[TokenPurpose.EMAIL_VERIFICATION]
            )
        )

    async def verify_email(self, token: str) -> User | None:
        """Redeem a verification token. Returns the user, or None if unusable."""
        user_id = await self.tokens.redeem(token, TokenPurpose.EMAIL_VERIFICATION)
        if user_id is None:
            return None

        user = await self.users.get(user_id)
        if user is None:
            return None

        if user.email_verified_at is None:
            user.email_verified_at = utcnow()
            await self.users.add(user)
        return user

    # -- internals -------------------------------------------------------------

    async def _send(self, message) -> None:
        """Send, swallowing transport failures.

        Deliberate. Surfacing a delivery error to an anonymous caller turns
        /auth/forgot-password into an oracle: an address that produces a mail
        error has an account, one that returns instantly does not. The failure
        is logged, and the user's recourse is to ask again.
        """
        try:
            await self.email.send(message)
        except Exception:
            logger.exception("Could not send %r; the user will not receive it.", message.subject)
