"""Security response headers on the API.

This process serves JSON, so the policy here is the maximally restrictive one
rather than a tuned allowlist -- its job is to make inert the two things that
*do* render: a substituted error page, and any endpoint that returns HTML by
accident. The page-level CSP that matters is the frontend's, which carries a
nonce; see `frontend/src/middleware.ts`.
"""

import pytest

from app.core.config import get_settings
from app.main import create_app


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_every_response_carries_the_headers(client):
    response = await client.get("/api/v1/health")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["x-permitted-cross-domain-policies"] == "none"
    assert "microphone=()" in response.headers["permissions-policy"]


async def test_the_api_policy_allows_nothing(client):
    """`default-src 'none'` covers scripts, styles, images, frames and
    connections at once, because every fetch directive falls back to it."""
    policy = (await client.get("/api/v1/health")).headers["content-security-policy"]

    assert "default-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "base-uri 'none'" in policy
    assert "form-action 'none'" in policy


async def test_an_error_response_is_covered_too(client):
    """The case the policy exists for: a body this application did not shape.
    A 404 that a proxy or framework renders as HTML must still be inert."""
    response = await client.get("/api/v1/nope")

    assert response.status_code == 404
    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_hsts_is_absent_outside_production(client):
    """It is a year-long promise about a *host*. Sent from a local or staging
    box under a shared parent domain it outlives the box."""
    assert "strict-transport-security" not in (await client.get("/api/v1/health")).headers


def test_hsts_is_sent_in_production(monkeypatch):
    from starlette.testclient import TestClient

    monkeypatch.setattr(get_settings(), "ENVIRONMENT", "production", raising=False)
    with TestClient(create_app()) as production_client:
        header = production_client.get("/api/v1/health").headers[
            "strict-transport-security"
        ]

    assert "max-age=31536000" in header
    assert "includeSubDomains" in header
    # `preload` submits the domain to a list compiled into browsers, and is
    # effectively irreversible. Not something to acquire as a side effect.
    assert "preload" not in header


def test_the_docs_page_gets_a_policy_that_lets_it_load(monkeypatch):
    """Swagger UI is real HTML pulling its bundle from a CDN, so the API's
    `default-src 'none'` would render a blank page. This looser policy only
    exists off production, where /docs is disabled outright."""
    from starlette.testclient import TestClient

    monkeypatch.setattr(get_settings(), "ENVIRONMENT", "local", raising=False)
    with TestClient(create_app()) as local_client:
        response = local_client.get("/docs")

    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    assert "https://cdn.jsdelivr.net" in policy
    # Still not embeddable, and still cannot be re-pointed by an injected <base>.
    assert "frame-ancestors 'none'" in policy


def test_docs_are_gone_in_production_and_so_is_the_loose_policy(monkeypatch):
    from starlette.testclient import TestClient

    monkeypatch.setattr(get_settings(), "ENVIRONMENT", "production", raising=False)
    with TestClient(create_app()) as production_client:
        response = production_client.get("/docs")

    assert response.status_code == 404
    assert "cdn.jsdelivr.net" not in response.headers["content-security-policy"]


async def test_a_cors_preflight_is_still_answered(client):
    """The headers middleware is registered *after* CORSMiddleware so it runs
    outside it. Inside, it would never see a preflight -- CORSMiddleware answers
    those itself without calling through."""
    response = await client.options(
        "/api/v1/health",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert response.headers["x-content-type-options"] == "nosniff"
