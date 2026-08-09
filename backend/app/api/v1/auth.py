from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import (
    CurrentUser,
    get_account_service,
    get_auth_service,
    limit_by_ip,
    limit_by_user,
)
from app.core.exceptions import ValidationError
from app.schemas.auth import (
    ForgotPasswordRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    ResetPasswordRequest,
    TokenPair,
    VerifyEmailRequest,
)
from app.schemas.user import UserCreate, UserRead
from app.services.account_service import AccountService
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])

AuthSvc = Annotated[AuthService, Depends(get_auth_service)]
AccountSvc = Annotated[AccountService, Depends(get_account_service)]

# Keyed by IP: there is no authenticated user on these routes, and the point is
# to bound guessing by an anonymous client.
AuthRateLimit = Depends(limit_by_ip("auth"))


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[AuthRateLimit],
)
async def register(payload: UserCreate, auth: AuthSvc, accounts: AccountSvc) -> UserRead:
    user = await auth.register(payload)
    # Best-effort. `send_verification` swallows transport failures: a mail
    # server having a bad afternoon must not fail an account creation that
    # otherwise succeeded, leaving the user with a registered address they
    # cannot register again.
    await accounts.send_verification(user)
    return UserRead.model_validate(user)


@router.post("/login", response_model=TokenPair, dependencies=[AuthRateLimit])
async def login(payload: LoginRequest, auth: AuthSvc) -> TokenPair:
    return await auth.login(payload.email, payload.password)


@router.post("/refresh", response_model=TokenPair, dependencies=[AuthRateLimit])
async def refresh(payload: RefreshRequest, auth: AuthSvc) -> TokenPair:
    """Rotate the refresh token. The old one stops working immediately."""
    return await auth.refresh(payload.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: LogoutRequest, auth: AuthSvc) -> Response:
    """Revoke the refresh token server-side, so it cannot outlive the session.

    Takes no access token: logging out is exactly what a client does when its
    access token has expired, and requiring a valid one would make the endpoint
    useless precisely when it is needed. The refresh token proves enough on its
    own -- and it is the credential being surrendered.

    Always 204, even for a token that was already invalid. See
    `AuthService.logout`.
    """
    await auth.logout(payload.refresh_token, everywhere=payload.everywhere)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/forgot-password",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[AuthRateLimit],
)
async def forgot_password(payload: ForgotPasswordRequest, accounts: AccountSvc) -> Response:
    """Email a reset link if the address has an account.

    **Always 202**, whether or not it does. Returning 404 for unknown addresses
    would turn this into a membership oracle: paste in a leaked address list and
    read off which ones have accounts here. Delivery failures are swallowed for
    the same reason -- see AccountService._send.
    """
    await accounts.request_password_reset(payload.email)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/reset-password", response_model=TokenPair, dependencies=[AuthRateLimit])
async def reset_password(
    payload: ResetPasswordRequest, accounts: AccountSvc, auth: AuthSvc
) -> TokenPair:
    """Set a new password from an emailed token, and sign in.

    Every existing session is revoked first. Reset is the button someone presses
    when they think an attacker is in the account, so leaving that attacker's
    sessions alive would defeat the entire flow. The returned pair is issued
    afterwards, so the person completing the reset ends up signed in on this
    device and nowhere else.
    """
    user_id = await accounts.reset_password(payload.token, payload.new_password)
    if user_id is None:
        # One message for expired, already-used, unknown and wrong-purpose. The
        # user's next step is identical in every case: request a new link.
        raise ValidationError("This reset link is invalid or has expired. Request a new one.")

    await auth.revoke_all(user_id)
    return await auth.issue_for(user_id)


@router.post("/verify-email", response_model=UserRead, dependencies=[AuthRateLimit])
async def verify_email(payload: VerifyEmailRequest, accounts: AccountSvc) -> UserRead:
    """Confirm an address from an emailed token.

    Unauthenticated: the link is opened from an inbox, which may well be on a
    different device from the one that registered.
    """
    user = await accounts.verify_email(payload.token)
    if user is None:
        raise ValidationError(
            "This confirmation link is invalid or has expired. Request a new one."
        )
    return UserRead.model_validate(user)


@router.post(
    "/resend-verification",
    status_code=status.HTTP_202_ACCEPTED,
    # Bounded per user, or the button becomes a way to have this server mail
    # someone repeatedly on demand.
    dependencies=[Depends(limit_by_user("auth"))],
)
async def resend_verification(current_user: CurrentUser, accounts: AccountSvc) -> Response:
    """Send another confirmation email. Authenticated, so no oracle to worry
    about -- the caller already knows the account exists.

    Issuing a new token invalidates the previous link, so only the most recent
    email works.
    """
    await accounts.send_verification(current_user)
    return Response(status_code=status.HTTP_202_ACCEPTED)
