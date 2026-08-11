"""Domain exceptions.

Services raise these; the error-handling middleware translates them into a
consistent HTTP error envelope. Routes never raise HTTPException for domain
errors.
"""

from typing import Any


class AppException(Exception):
    """Base class for all expected application errors."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str = "An unexpected error occurred.", details: Any = None):
        self.message = message
        self.details = details
        # Response headers the handler should copy onto the error response.
        # Empty for most errors; 429 uses it for Retry-After.
        self.headers: dict[str, str] = {}
        super().__init__(message)


class NotFoundError(AppException):
    status_code = 404
    code = "not_found"


class ConflictError(AppException):
    status_code = 409
    code = "conflict"


class UnauthorizedError(AppException):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppException):
    status_code = 403
    code = "forbidden"


class ValidationError(AppException):
    status_code = 422
    code = "validation_error"


class PayloadTooLargeError(AppException):
    status_code = 413
    code = "payload_too_large"


class RateLimitedError(AppException):
    status_code = 429
    code = "rate_limited"

    def __init__(
        self,
        message: str = "Too many requests.",
        details: Any = None,
        *,
        retry_after: int | None = None,
    ):
        super().__init__(message, details)
        if retry_after is not None:
            self.headers["Retry-After"] = str(retry_after)


class QuotaExceededError(AppException):
    """The account already holds as much of something as it is allowed to.

    429, not 403, and the status is a deliberate choice rather than the obvious
    one. The frontend classifies 401 *and* 403 as `kind === "auth"` (see
    `api-client.ts`), which is the one kind of failure allowed to implicate the
    session -- so answering "you have ten resumes" with a 403 would tell the UI
    the user's credentials were the problem.

    The `code` is what distinguishes this from `rate_limited`, and the
    difference is real: a rate limit clears by waiting, and a quota clears by
    deleting something. `Retry-After` is therefore set only when waiting is
    actually the answer.
    """

    status_code = 429
    code = "quota_exceeded"

    def __init__(
        self,
        message: str = "Quota exceeded.",
        details: Any = None,
        *,
        retry_after: int | None = None,
    ):
        super().__init__(message, details)
        if retry_after is not None:
            self.headers["Retry-After"] = str(retry_after)


class StorageError(AppException):
    """Raised by storage providers; provider-agnostic by design."""

    status_code = 502
    code = "storage_error"
