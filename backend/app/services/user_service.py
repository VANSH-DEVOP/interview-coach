"""User profile business logic."""

from app.core.exceptions import UnauthorizedError, ValidationError
from app.core.security import hash_password, verify_password
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.user import UserUpdate


class UserService:
    def __init__(self, users: UserRepository) -> None:
        self.users = users

    async def update_profile(self, user: User, payload: UserUpdate) -> User:
        if payload.full_name is not None:
            user.full_name = payload.full_name
        return await self.users.add(user)

    async def change_password(self, user: User, current: str, new: str) -> User:
        """Re-authenticate, then replace the hash.

        Revoking the user's sessions is the caller's job, not this method's --
        it needs the token repository, and the route has to issue a replacement
        pair afterwards so the device doing the change is not signed out by it.
        """
        if not verify_password(current, user.hashed_password):
            # 401 rather than 400: this is a failed authentication, and it is
            # rate-limited on the same footing as a login attempt.
            raise UnauthorizedError("Your current password is incorrect.")
        if verify_password(new, user.hashed_password):
            raise ValidationError("The new password must be different from the current one.")

        user.hashed_password = hash_password(new)
        return await self.users.add(user)

    async def verify_password_or_raise(self, user: User, password: str) -> None:
        """Re-authentication gate for irreversible actions."""
        if not verify_password(password, user.hashed_password):
            raise UnauthorizedError("Incorrect password.")
