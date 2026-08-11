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
    # How long a report may sit PENDING or GENERATING, measured from its last
    # state change, before the worker's cron treats it as orphaned and queues it
    # again. A Redis restart drops the queued jobs and nothing else ever flips
    # those rows, so without this the report spins forever.
    #
    # This must stay comfortably above EVALUATION_MAX_TRIES *
    # EVALUATION_JOB_TIMEOUT_SECONDS plus arq's backoff between attempts.
    # Below that, the sweep re-queues work that is still running and two workers
    # evaluate the same session at once.
    EVALUATION_STALE_AFTER_SECONDS: int = 1800
    # Past this age, stop re-queueing and mark the report FAILED. Re-queueing is
    # right for work that lost its job; it is wrong for a session that cannot be
    # evaluated at all, which would otherwise be retried every sweep forever.
    # FAILED is at least visible, and the UI offers a retry.
    EVALUATION_STALE_GIVE_UP_SECONDS: int = 86400

    # -- AI provider ----------------------------------------------------------
    # When set, the Gemini-backed generator/evaluator activate; otherwise the
    # app falls back to deterministic local implementations (no key required).
    GEMINI_API_KEY: str | None = None
    GEMINI_MODEL: str = "gemini-flash-latest"
    # Cosine distance beyond which a retrieved chunk is treated as unrelated
    # and dropped, rather than padding the prompt. Chroma's cosine distance
    # runs 0 (identical) to 2 (opposite), so 1.0 is "no better than orthogonal"
    # -- a guard against obvious junk, not a relevance judgement.
    #
    # Deliberately loose and configurable: the band a model puts "related" text
    # in varies between embedding models, so a tight value tuned against one
    # silently discards good chunks under another.
    RAG_MAX_DISTANCE: float = 1.0
    # How long a cached embedding vector lives, when REDIS_URL is set. An
    # embedding is a pure function of (model, text), so entries never go stale
    # -- the TTL exists to reclaim space, not to guard correctness, and a
    # model change is already a different key. Thirty days.
    #
    # Why this matters: the free tier allows 20 requests per day for the whole
    # account and indexing one resume costs one call per chunk, so two uploads
    # exhausted the day. Re-indexing unchanged text is now free.
    EMBEDDING_CACHE_TTL_SECONDS: int = 60 * 60 * 24 * 30
    # Google retires model IDs, and a retired ID is a 404 that the fallback
    # layer hides. Keep these overridable so a rotation is an env change, and
    # check `GET /v1beta/models` for the key in use before changing a default.
    GEMINI_EMBEDDING_MODEL: str = "models/gemini-embedding-001"

    # -- Tracing (LangSmith) ---------------------------------------------------
    # Off by default. `retrieval_metrics` says how often retrieval comes back
    # empty; tracing says why *this* interview's questions were poor, by tying
    # one operation's rewrite, retrieval, fusion, prompt and parse into one span
    # tree with timings and errors attached to the step that produced them.
    LANGSMITH_TRACING: bool = False
    LANGSMITH_API_KEY: str | None = None
    LANGSMITH_PROJECT: str = "interviewpilot"
    # Self-hosted instances only; unset uses LangSmith's cloud.
    LANGSMITH_ENDPOINT: str | None = None
    # Whether traces carry the actual prompts and retrieved chunks.
    #
    # Off by default because a trace's payload is resume text: the prompt
    # contains it, and the retrieved chunks come from Chroma, which holds the
    # resume *unredacted* on purpose. Turning this on ships to the tracing
    # service exactly what app/services/ai/masking.py exists to withhold, and
    # does it silently, since nobody reviews a trace the way they review a
    # request. Reasonable for local debugging or a self-hosted endpoint.
    LANGSMITH_TRACE_CONTENT: bool = False

    # -- Error reporting (Sentry) ----------------------------------------------
    # Off until a DSN is set. The counters in degradation.py and call_metrics.py
    # say how often something failed; this says what the traceback was.
    #
    # Wired at the *swallow points*, not just at the ASGI layer: this
    # application has 42 `except Exception` blocks by design, so a reporter that
    # only sees unhandled exceptions would be quietest exactly when things are
    # worst. See app/core/error_reporting.py.
    SENTRY_DSN: str | None = None
    # Which build this is. Worth setting in any real deployment -- "since when"
    # is the first question about a new error, and it is unanswerable without.
    SENTRY_RELEASE: str | None = None
    # Performance tracing. Off: the AI pipeline is already traced by LangSmith,
    # which knows what a retrieval is, and a second tracer would double the
    # vendors seeing this traffic for a worse picture of it.
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0
    # Whether crash reports carry request bodies and local variables.
    #
    # Off by default, for the same reason as LANGSMITH_TRACE_CONTENT and with
    # more force: the locals at the point of a crash here are `prompt`,
    # `resume_text`, `transcript` and `answer`. Turning this on ships a third
    # party exactly what app/services/ai/masking.py exists to withhold, and does
    # it silently, since nobody reviews a crash report the way they review a
    # request. Reasonable only against a self-hosted Sentry.
    SENTRY_SEND_CONTENT: bool = False

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
    # Interviews one user may start per day. A *consumption* cap, so it is a
    # counter rather than a row count: starting an interview spends a provider
    # call, and deleting the session afterwards cannot un-spend it. Counting
    # `interview_sessions` rows instead would make delete-and-retry a way round
    # this. See MAX_RESUMES_PER_USER for the case that works the other way.
    #
    # The hourly `ai` limit above bounds bursts; this bounds the day, which the
    # hourly one does not (20/hour is 480/day).
    RATE_LIMIT_INTERVIEW_CREATES: int = 5
    RATE_LIMIT_INTERVIEW_WINDOW_SECONDS: int = 60 * 60 * 24

    # -- Per-user quotas -------------------------------------------------------
    # How many resumes one account may keep. An *occupancy* quota -- a ceiling
    # on what is held, not on what is spent -- so it is enforced by counting
    # rows rather than by a counter, for three reasons:
    #
    #   * The number already exists in Postgres. A counter would be a second
    #     copy of a fact, and the two would drift.
    #   * It is durable. A Redis restart must not hand out unlimited uploads.
    #   * Deleting a resume frees the quota immediately, which is *correct*
    #     here: the resource being bounded is storage, and deleting returns it.
    #
    # The rate limit above bounds uploads per hour; nothing bounded the total,
    # so 10/hour was 7,300 files a month with no ceiling at all.
    #
    # Zero or less means unlimited. Deliberately not `int | None`: an optional
    # int cannot express "unlimited" through the environment at all -- `null`
    # fails validation, and a blank value falls back to this default because of
    # `env_ignore_empty`. That left an operator no way to switch the quota off,
    # while `MAX_RESUMES_PER_USER=0` -- the obvious guess for it -- rejected
    # every upload instead.
    MAX_RESUMES_PER_USER: int = 10

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
