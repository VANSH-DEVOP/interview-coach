"""Centralised, environment-driven application settings.

All configuration enters the application through this module. No other module
reads environment variables directly.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    # -- Application ---------------------------------------------------------
    APP_NAME: str = "InterviewPilot AI"
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    # -- Database ------------------------------------------------------------
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "interviewpilot"
    POSTGRES_PASSWORD: str = "interviewpilot"
    POSTGRES_DB: str = "interviewpilot"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def sync_database_url(self) -> str:
        """Synchronous-style URL used by Alembic (asyncpg driver, run via async runner)."""
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    # -- Job queue -------------------------------------------------------------
    # When set, evaluations are handed to the arq worker over Redis and survive
    # a restart of the web process. When unset the app falls back to in-process
    # background tasks, which is fine for local development and tests but loses
    # work on restart -- see app/services/job_queue.py.
    REDIS_URL: str | None = None
    # A single evaluation is one provider round-trip; well under a minute in
    # practice, but the free tier can be slow.
    EVALUATION_JOB_TIMEOUT_SECONDS: int = 300
    # Attempts, not retries: 3 means the original run plus two retries. arq
    # backs off between them, which is what makes this worth having over a
    # single in-process attempt.
    EVALUATION_MAX_TRIES: int = 3

    # -- AI provider ----------------------------------------------------------
    # When set, the Gemini-backed generator/evaluator activate; otherwise the
    # app falls back to deterministic local implementations (no key required).
    GEMINI_API_KEY: str | None = None
    GEMINI_MODEL: str = "gemini-flash-latest"
    # Google retires model IDs, and a retired ID is a 404 that the fallback
    # layer hides. Keep these overridable so a rotation is an env change, and
    # check `GET /v1beta/models` for the key in use before changing a default.
    GEMINI_EMBEDDING_MODEL: str = "models/gemini-embedding-001"

    # -- Vector store (RAG) -----------------------------------------------------
    # Must point at durable storage. On a throwaway path (/tmp, a container
    # layer) the resume index is lost on restart and RAG silently degrades to
    # truncated raw resume text with no error anywhere.
    CHROMA_PATH: Path = Path("/var/lib/interviewpilot/chroma")

    # -- Security ------------------------------------------------------------
    JWT_SECRET_KEY: str = "insecure-local-dev-key-override-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # -- Rate limiting ---------------------------------------------------------
    # Counters are per-process (see app/core/rate_limit.py), so with N workers
    # the effective ceiling is N x these values.
    RATE_LIMIT_ENABLED: bool = True
    # Credential stuffing defence. Per client IP.
    RATE_LIMIT_AUTH_ATTEMPTS: int = 10
    RATE_LIMIT_AUTH_WINDOW_SECONDS: int = 300
    # Quota defence. Per user. Every one of these requests costs a Gemini call,
    # and the free tier allows only 20/day across the whole deployment.
    RATE_LIMIT_AI_REQUESTS: int = 20
    RATE_LIMIT_AI_WINDOW_SECONDS: int = 3600
    # Upload abuse / storage growth. Per user.
    RATE_LIMIT_UPLOAD_REQUESTS: int = 10
    RATE_LIMIT_UPLOAD_WINDOW_SECONDS: int = 3600

    # -- Email -----------------------------------------------------------------
    # "log" writes messages to the log instead of sending them: no credentials,
    # nothing leaves the machine, and reset links are copy-pasteable out of the
    # console. It is refused in production, where printing those links is a
    # credential leak (see app/services/email/__init__.py).
    # "smtp" is the real transport and works with SES, SendGrid, Mailgun,
    # Postmark or Gmail -- changing provider is host/port/credentials here.
    EMAIL_BACKEND: Literal["log", "smtp"] = "log"
    EMAIL_FROM: str = "InterviewPilot AI <no-reply@interviewpilot.local>"
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: str | None = None
    # Implicit TLS (usually port 465) vs STARTTLS (usually 587). Mutually
    # exclusive; setting both is rejected at startup.
    SMTP_USE_TLS: bool = False
    SMTP_START_TLS: bool = True
    SMTP_TIMEOUT_SECONDS: int = 10

    # Absolute base URL for links in emails. Emails are read outside the
    # browser session, so a relative path is meaningless -- and this cannot be
    # derived from the request, because a Host header is attacker-controlled and
    # would let someone point a password-reset link at their own domain.
    FRONTEND_BASE_URL: str = "http://localhost:3000"

    # -- Storage ---------------------------------------------------------------
    # "local" today; "s3" | "r2" | "minio" are future providers. The value is
    # consumed only by the storage factory (app.services.storage).
    STORAGE_BACKEND: Literal["local"] = "local"
    # Lives OUTSIDE the application code directory by design.
    STORAGE_LOCAL_PATH: Path = Path("/var/lib/interviewpilot/storage")
    MAX_UPLOAD_SIZE_BYTES: int = 5 * 1024 * 1024  # 5 MiB
    ALLOWED_RESUME_CONTENT_TYPES: tuple[str, ...] = (
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
