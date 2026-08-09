"""Email backend factory.

The only place that knows which transport is active. A REST-based provider, if
one is ever wanted for delivery events or per-message tracking, registers here:

    case "resend": return ResendEmailSender(...)

No business logic, routes, or models change when a backend is added.
"""

import logging
from functools import lru_cache

from app.core.config import get_settings
from app.services.email.base import EmailError, EmailMessage, EmailSender
from app.services.email.console import LoggingEmailSender
from app.services.email.memory import RecordingEmailSender
from app.services.email.smtp import SmtpEmailSender

__all__ = [
    "EmailError",
    "EmailMessage",
    "EmailSender",
    "LoggingEmailSender",
    "RecordingEmailSender",
    "SmtpEmailSender",
    "get_email_sender",
]

logger = logging.getLogger(__name__)


@lru_cache
def get_email_sender() -> EmailSender:
    """The configured backend. Cached: backends are stateless and per-process.

    Call `get_email_sender.cache_clear()` in tests that vary the settings.
    """
    settings = get_settings()

    match settings.EMAIL_BACKEND:
        case "log":
            if settings.ENVIRONMENT == "production":
                # Not a warning. The logging backend prints password-reset and
                # verification links in full, so running it in production
                # publishes account-takeover links to whoever can read the logs
                # -- while every flow still reports success. Refusing at startup
                # is the only outcome that cannot be missed.
                raise ValueError(
                    "EMAIL_BACKEND='log' is not allowed in production: it writes "
                    "password-reset and verification links to the log in "
                    "cleartext. Set EMAIL_BACKEND=smtp and configure SMTP_HOST."
                )
            logger.warning(
                "EMAIL_BACKEND=log: emails are written to the log, not sent. "
                "Reset and verification links appear in the console."
            )
            return LoggingEmailSender()

        case "smtp":
            if settings.SMTP_USE_TLS and settings.SMTP_START_TLS:
                raise ValueError(
                    "SMTP_USE_TLS and SMTP_START_TLS are mutually exclusive. Use "
                    "SMTP_USE_TLS for implicit TLS (usually port 465) or "
                    "SMTP_START_TLS for STARTTLS (usually port 587)."
                )
            return SmtpEmailSender(
                host=settings.SMTP_HOST,
                port=settings.SMTP_PORT,
                username=settings.SMTP_USERNAME,
                password=settings.SMTP_PASSWORD,
                sender=settings.EMAIL_FROM,
                use_tls=settings.SMTP_USE_TLS,
                start_tls=settings.SMTP_START_TLS,
                timeout=settings.SMTP_TIMEOUT_SECONDS,
            )

        case _:  # pragma: no cover - unreachable while the Literal is exhaustive
            raise ValueError(f"Unknown email backend: {settings.EMAIL_BACKEND}")
