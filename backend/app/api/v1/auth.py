from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import get_auth_service, limit_by_ip
from app.schemas.auth import LoginRequest, LogoutRequest, RefreshRequest, TokenPair
from app.schemas.user import UserCreate, UserRead
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])

AuthSvc = Annotated[AuthService, Depends(get_auth_service)]

# Keyed by IP: there is no authenticated user on these routes, and the point is
# to bound guessing by an anonymous client.
AuthRateLimit = Depends(limit_by_ip("auth"))


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[AuthRateLimit],
)
async def register(payload: UserCreate, auth: AuthSvc) -> UserRead:
    user = await auth.register(payload)
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
