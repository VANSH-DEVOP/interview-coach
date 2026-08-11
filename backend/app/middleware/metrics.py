"""Per-request timing for the Prometheus endpoint.

Separate from `RequestLoggingMiddleware` because they answer different
questions: that one produces a line per request for reading after the fact,
this one produces numbers for aggregation. Sharing a class would couple the log
format to the metric labels.
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.metrics import observe_request


def route_template(request: Request) -> str:
    """The matched route's pattern, or `unmatched`.

    **Never the raw path.** `/api/v1/interviews/9f3c...` would create a new
    time series per interview, and on a 404 the label would be
    attacker-controlled -- a few thousand requests to random URLs would blow up
    the scrape's memory rather than this process's. The template collapses all
    of them onto `/api/v1/interviews/{session_id}`.

    Starlette records the matched route in the scope during routing, so this is
    only readable *after* the request has been handled.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # An unhandled exception still took time and still says something
            # about the route. Recorded as a 500 -- which is what the error
            # handler will turn it into -- and then re-raised untouched, since
            # measuring must never swallow.
            observe_request(
                method=request.method,
                route=route_template(request),
                status=500,
                seconds=time.perf_counter() - started,
            )
            raise

        observe_request(
            method=request.method,
            route=route_template(request),
            status=response.status_code,
            seconds=time.perf_counter() - started,
        )
        return response
