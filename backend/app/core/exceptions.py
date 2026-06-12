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


class StorageError(AppException):
    """Raised by storage providers; provider-agnostic by design."""

    status_code = 502
    code = "storage_error"
