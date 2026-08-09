"""Password hashing and JWT token primitives.

Pure functions only: no I/O, no database access. Consumed by the auth service
and the authentication dependency.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import bcrypt
import jwt

from app.core.config import get_settings

TokenType = Literal["access", "refresh"]


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A token plus the parts the caller has to persist.

    Refresh tokens are recorded in a revocation list, which needs the `jti` to
    identify the row and `expires_at` to know when it can be pruned. Digging
    both back out by decoding the token we just encoded would be silly.
    """

    token: str
    jti: str
    # Naive UTC, matching the TIMESTAMP WITHOUT TIME ZONE columns.
    expires_at: datetime


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def _create_token(
    subject: str, token_type: TokenType, expires_delta: timedelta
) -> IssuedToken:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires_at = now + expires_delta
    jti = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "jti": jti,
        "iat": now,
        "exp": expires_at,
    }
    token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return IssuedToken(token=token, jti=jti, expires_at=expires_at.replace(tzinfo=None))


def create_access_token(subject: str) -> str:
    """Access tokens are never revoked individually, so only the string matters.

    They are short-lived by design (ACCESS_TOKEN_EXPIRE_MINUTES); revocation
    happens at the refresh boundary. A stolen access token stays usable until it
    expires, which is the trade the short lifetime pays for.
    """
    settings = get_settings()
    return _create_token(
        subject, "access", timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    ).token


def create_refresh_token(subject: str) -> IssuedToken:
    """Returns the token *and* what the revocation list needs to record it."""
    settings = get_settings()
    return _create_token(subject, "refresh", timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS))


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """Decode and validate a JWT. Raises jwt.InvalidTokenError on any failure."""
    settings = get_settings()
    payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"Expected a {expected_type} token")
    return payload
