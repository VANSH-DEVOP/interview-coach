import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=255)


class PasswordChange(BaseModel):
    # The current password is required even though the caller is already
    # authenticated: an access token left behind on a shared machine should not
    # be enough to take the account over permanently.
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class AccountDelete(BaseModel):
    """Deletion is irreversible, so it is re-authenticated rather than trusted
    to a token that may have been left lying around."""

    password: str


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    is_active: bool
    created_at: datetime
