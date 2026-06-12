# ---------------------------------------------------------------------------
# InterviewPilot AI - Backend (FastAPI)
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/backend

# Install dependencies first for layer caching.
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Application code.
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

# Non-root runtime user; storage root is OUTSIDE the code directory.
RUN useradd --create-home --uid 1001 appuser \
    && mkdir -p /var/lib/interviewpilot/storage \
    && chown -R appuser:appuser /var/lib/interviewpilot
USER appuser

EXPOSE 8000

# Migrations then server. For multi-replica production, run migrations as a
# separate release step instead.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
