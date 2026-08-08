"""Translates domain exceptions into the consistent HTTP error envelope:

    {"error": {"code": "...", "message": "...", "details": ...}}
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.exceptions import AppException

logger = logging.getLogger(__name__)


def _envelope(code: str, message: str, details=None) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def handle_app_exception(request: Request, exc: AppException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, exc.details),
            headers=exc.headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_envelope("validation_error", "Request validation failed.", exc.errors()),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Never leak internals to clients; full traceback goes to logs only.
        logger.exception("Unhandled exception", extra={"path": request.url.path})
        return JSONResponse(
            status_code=500,
            content=_envelope("internal_error", "An unexpected error occurred."),
        )
