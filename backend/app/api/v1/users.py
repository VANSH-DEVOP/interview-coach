from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import (
    CurrentUser,
    get_account_deletion_service,
    get_auth_service,
    get_user_service,
    limit_by_user,
)
from app.schemas.auth import TokenPair
from app.schemas.user import AccountDelete, PasswordChange, UserRead, UserUpdate
from app.services.auth_service import AuthService
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["users"])

UserSvc = Annotated[UserService, Depends(get_user_service)]
AuthSvc = Annotated[AuthService, Depends(get_auth_service)]


@router.get("/me", response_model=UserRead)
async def read_me(current_user: CurrentUser) -> UserRead:
    return UserRead.model_validate(current_user)


@router.patch("/me", response_model=UserRead)
async def update_me(payload: UserUpdate, current_user: CurrentUser, users: UserSvc) -> UserRead:
    user = await users.update_profile(current_user, payload)
    return UserRead.model_validate(user)


@router.post(
    "/me/password",
    response_model=TokenPair,
    # Guessing the current password here would otherwise be unlimited, and
    # unlike login this endpoint is reached with a token rather than an IP we
    # want to punish. Keyed by user, on the auth budget.
    dependencies=[Depends(limit_by_user("auth"))],
)
async def change_password(
    payload: PasswordChange,
    current_user: CurrentUser,
    users: UserSvc,
    auth: AuthSvc,
) -> TokenPair:
    """Change the password and sign every other session out.

    Returns a **new token pair**. Changing a password has to invalidate sessions
    -- that is most of the point, and the usual reason someone does it is that
    they think another session exists that shouldn't. But revoking everything
    would also sign out the device making the request, so a fresh pair is issued
    afterwards and the caller stays where they are. The old tokens, including
    the caller's previous refresh token, are dead either way.
    """
    await users.change_password(
        current_user, payload.current_password, payload.new_password
    )
    await auth.revoke_all(current_user.id)
    return await auth.issue_for(current_user.id)


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(limit_by_user("auth"))],
)
async def delete_me(
    payload: AccountDelete,
    current_user: CurrentUser,
    users: Annotated[UserService, Depends(get_account_deletion_service)],
) -> Response:
    """Delete the account and everything in it. Irreversible.

    DELETE with a body, which is unusual but correct here: the password is a
    credential and has no business in a query string, where it would land in
    access logs and browser history. Clients that cannot send a body on DELETE
    are not a constraint this API has.

    Requires the password because an access token borrowed from an unattended
    machine should not be able to destroy someone's account.

    Sessions are not revoked explicitly -- the refresh tokens cascade away with
    the user row, which is the same outcome by a shorter route.
    """
    await users.delete_account(current_user, payload.password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
