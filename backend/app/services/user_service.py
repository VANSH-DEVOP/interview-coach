"""User profile business logic."""

import logging

from app.core.exceptions import UnauthorizedError, ValidationError
from app.core.security import hash_password, verify_password
from app.models.user import User
from app.repositories.resume_repository import ResumeRepository
from app.repositories.user_repository import UserRepository
from app.schemas.user import UserUpdate
from app.services.storage.base import StorageService

logger = logging.getLogger(__name__)


class UserService:
    def __init__(
        self,
        users: UserRepository,
        resumes: ResumeRepository | None = None,
        storage: StorageService | None = None,
        rag_service=None,
    ) -> None:
        self.users = users
        # Only deletion needs these. Optional so the profile-editing paths, and
        # the tests that exercise them, do not have to construct a storage
        # backend they never touch.
        self.resumes = resumes
        self.storage = storage
        self.rag_service = rag_service

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

    async def delete_account(self, user: User, password: str) -> None:
        """Erase the account and everything belonging to it. Irreversible.

        The database cascade reaches resumes, sessions, questions, answers,
        reports and tokens. It does NOT reach the two stores that live outside
        Postgres -- the resume blobs and the vector index -- and those are the
        ones holding the actual content of someone's CV. Deleting only the rows
        would leave the sensitive part behind while reporting success.

        External stores go first, deliberately. They are not transactional: if
        the row deletion fails after them, the user still exists with some blobs
        missing, which is recoverable by re-uploading. The reverse order risks
        the account disappearing while its resume text stays in the vector
        store, unreferenced and unreachable -- exactly the thing they asked to
        remove, now impossible to find again.
        """
        await self.verify_password_or_raise(user, password)

        if self.resumes is not None:
            for resume in await self.resumes.all_for_user(user.id):
                await self._purge_resume_artifacts(resume)

        await self.users.delete(user)
        logger.info("Deleted account %s and all associated data.", user.id)

    async def _purge_resume_artifacts(self, resume) -> None:
        """Best-effort removal from the stores outside the database.

        Failures are logged and swallowed rather than aborting: a blob the
        storage backend cannot delete must not strand the user in an account
        they have asked to be rid of. What is left behind is an orphan with no
        row pointing at it, which is reclaimable; refusing the deletion is not.
        """
        if self.storage is not None:
            try:
                await self.storage.delete(resume.storage_key)
            except Exception:
                logger.exception(
                    "Could not delete stored file %s during account deletion.",
                    resume.storage_key,
                )

        if self.rag_service is not None:
            try:
                await self.rag_service.delete_index(resume.id)
            except Exception:
                logger.exception(
                    "Could not delete the vector index for resume %s during "
                    "account deletion.",
                    resume.id,
                )
