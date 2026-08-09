from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str
    # "Sign out everywhere" -- revokes every session for the account, not just
    # the one making the request. The obvious response to a device being lost.
    everywhere: bool = False


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
