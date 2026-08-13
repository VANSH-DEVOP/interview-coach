"""Application factory and wiring."""

import logging
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.metrics import router as metrics_router
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.error_reporting import configure_error_reporting
from app.core.logging import configure_logging
from app.core.startup_checks import verify_production_config
from app.db.session import engine
from app.middleware.error_handler import register_exception_handlers
from app.middleware.metrics import MetricsMiddleware
from app.middleware.request_logging import RequestLoggingMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.services.evaluation_worker import recover_stale_reports

logger = logging.getLogger(__name__)


async def _open_queue(app: FastAPI) -> None:
    """Connect the arq pool, or leave the app on the in-process fallback.

    A Redis that is configured but unreachable must not stop the API booting:
    evaluations degrade to in-process (loudly, see job_queue.py) while every
    other endpoint keeps working. Failing startup here would take the whole
    product down over one background feature.
    """
    app.state.arq_pool = None
    settings = get_settings()
    if not settings.REDIS_URL:
        logger.warning(
            "REDIS_URL is not set; evaluations will run in-process and will not "
            "survive a restart."
        )
        return

    try:
        app.state.arq_pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
        logger.info("Evaluation queue connected.")
    except Exception:
        logger.exception("Could not connect to Redis; evaluations will run in-process.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    # Before anything is served, and before the queue or the database is
    # touched: every check is a value that is correct in development and a hole
    # in production, and each one leaves the application apparently working.
    verify_production_config(get_settings())
    # Before the queue: connecting to Redis is the first thing that can fail,
    # and a reporter initialised after it would miss the failure it most wants.
    configure_error_reporting()
    await _open_queue(app)

    # Only meaningful without a queue. With one, a PENDING or GENERATING report
    # has a real job waiting in Redis, and failing those rows on boot would
    # destroy live work -- every deploy would kill the evaluations in flight.
    # The queued case is handled instead by the worker's reconciliation cron
    # (app/worker.py), which waits out an age threshold and re-queues; that is
    # what catches the reports a *Redis* restart orphans, which this cannot.
    if app.state.arq_pool is None:
        await recover_stale_reports()

    yield

    if app.state.arq_pool is not None:
        await app.state.arq_pool.aclose()
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.APP_NAME,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
        redoc_url=None,
    )

    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Registered LAST, which makes it the OUTERMOST layer: Starlette inserts
    # each new middleware at the front of the stack, so the last one added is
    # the first one entered and the last one to touch a response.
    #
    # That ordering is load-bearing, not tidiness. CORSMiddleware answers a
    # preflight itself and never calls through, so a security-headers
    # middleware registered *before* it -- and therefore wrapped by it -- never
    # sees those responses at all. Verified by the preflight test in
    # tests/api/test_security_headers.py, which failed when this was the other
    # way round.
    app.add_middleware(
        SecurityHeadersMiddleware,
        # Only where the site is actually served over HTTPS. See the module
        # docstring: this is a year-long promise about a host, not a response.
        hsts=settings.ENVIRONMENT == "production",
        # From the app's own docs_url, not a copy of it. In production that is
        # None, so the CDN-permitting policy Swagger needs is not merely unused
        # there -- it does not exist. Matching on the path instead handed that
        # policy to the 404 that replaces the page.
        docs_paths=tuple(path for path in (app.docs_url, app.redoc_url) if path),
    )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)
    # Outside the versioned prefix on purpose: /metrics is where
    # Prometheus looks, and it carries no API versioning promise.
    app.include_router(metrics_router)
    return app


app = create_app()
