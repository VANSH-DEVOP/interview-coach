from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import get_auth_service, limit_by_ip
from app.schemas.auth import LoginRequest, RefreshRequest, TokenPair
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
    return await auth.refresh(payload.refresh_token)
