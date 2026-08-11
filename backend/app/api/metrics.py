"""The `/metrics` endpoint.

Deliberately outside `/api/v1`: it is not part of the product's API, it has no
versioning promise, and Prometheus looks for `/metrics` by convention.

**Off by default, and behind a token when on.** The counters here describe the
deployment rather than any one user, but together they are an operational
picture worth withholding from the public internet -- how many people signed up,
how often the provider is failing, how much quota is left. A default-open
endpoint on a public API would publish that to anyone who guessed the path.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request, Response

from app.core.config import get_settings
from app.core.exceptions import NotFoundError, UnauthorizedError
from app.core.metrics import REGISTRY

router = APIRouter(tags=["system"])


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    settings = get_settings()
    if not settings.METRICS_ENABLED:
        # 404 rather than 403: an endpoint that is switched off should be
        # indistinguishable from one that does not exist, so scanning for it
        # tells an attacker nothing about the deployment.
        raise NotFoundError("Not found.")

    if settings.METRICS_TOKEN:
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {settings.METRICS_TOKEN}"
        # Constant-time: a token checked with `==` leaks its prefix to anyone
        # patient enough to measure, and this one guards the deployment's
        # operational picture.
        if not hmac.compare_digest(supplied, expected):
            raise UnauthorizedError("Invalid metrics credentials.")

    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
