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

    # -- AI provider ----------------------------------------------------------
    # When set, the Gemini-backed generator/evaluator activate; otherwise the
    # app falls back to deterministic local implementations (no key required).
    GEMINI_API_KEY: str | None = None
    GEMINI_MODEL: str = "gemini-1.5-flash"

    # -- Security ------------------------------------------------------------
    JWT_SECRET_KEY: str = "insecure-local-dev-key-override-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

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
