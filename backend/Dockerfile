# ---------------------------------------------------------------------------
# InterviewPilot AI - Backend (FastAPI)
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS base

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
# Both mount points must exist and be owned by appuser *in the image*: Docker
# seeds a fresh named volume from the image path, so a missing directory would
# be created as root and the non-root process could not write to it.
RUN useradd --create-home --uid 1001 appuser \
    && mkdir -p /var/lib/interviewpilot/storage /var/lib/interviewpilot/chroma \
    && chown -R appuser:appuser /var/lib/interviewpilot
USER appuser

EXPOSE 8000

# Migrations then server. For multi-replica production, run migrations as a
# separate release step instead.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
