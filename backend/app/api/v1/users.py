from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser, get_user_service
from app.schemas.user import UserRead, UserUpdate
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["users"])

UserSvc = Annotated[UserService, Depends(get_user_service)]


@router.get("/me", response_model=UserRead)
async def read_me(current_user: CurrentUser) -> UserRead:
    return UserRead.model_validate(current_user)


@router.patch("/me", response_model=UserRead)
async def update_me(payload: UserUpdate, current_user: CurrentUser, users: UserSvc) -> UserRead:
    user = await users.update_profile(current_user, payload)
    return UserRead.model_validate(user)
