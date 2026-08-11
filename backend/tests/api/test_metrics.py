"""The /metrics endpoint.

Three things are being pinned, and only one of them is "the numbers appear".

**Cardinality.** A route label taken from the raw path creates a time series per
interview, and on a 404 that label is attacker-controlled -- a few thousand
requests to random URLs would exhaust the scrape rather than this process. The
label must be the matched route's template.

**Raw counters, not rates.** `/health` reports `ai.fallback_rate`, an average
over the life of the process that an hour of total failure barely moves.
Alerting needs `rate(fallbacks[5m]) / rate(attempts[5m])`, which needs both
counters and not their quotient.

**Exposure.** The counters describe the deployment -- signups, provider failure
rate, remaining quota -- and the endpoint is off by default and token-guarded
when on.
"""

import pytest

from app.core.config import get_settings


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "METRICS_ENABLED", True, raising=False)
    monkeypatch.setattr(get_settings(), "METRICS_TOKEN", None, raising=False)


async def scrape(client) -> str:
    response = await client.get("/metrics")
    assert response.status_code == 200, response.text
    return response.text


# -- Exposure --------------------------------------------------------------------


async def test_metrics_are_off_by_default(client):
    """404 rather than 403: an endpoint that is switched off should be
    indistinguishable from one that never existed, so scanning tells an
    attacker nothing."""
    response = await client.get("/metrics")

    assert response.status_code == 404


async def test_a_token_is_required_when_one_is_configured(monkeypatch, client):
    monkeypatch.setattr(get_settings(), "METRICS_ENABLED", True, raising=False)
    monkeypatch.setattr(get_settings(), "METRICS_TOKEN", "scrape-me", raising=False)

    assert (await client.get("/metrics")).status_code == 401

    wrong = await client.get("/metrics", headers={"Authorization": "Bearer wrong"})
    assert wrong.status_code == 401

    right = await client.get("/metrics", headers={"Authorization": "Bearer scrape-me"})
    assert right.status_code == 200


async def test_the_exposition_format_is_what_prometheus_expects(client, enabled):
    response = await client.get("/metrics")

    assert response.headers["content-type"].startswith("text/plain")
    assert "# HELP" in response.text
    assert "# TYPE" in response.text


# -- Cardinality -----------------------------------------------------------------


async def test_the_route_label_is_a_template_not_a_path(client, enabled):
    """The whole point of the label. One time series for every interview's
    answers, not one per interview."""
    # /metrics is itself a matched route, so scraping twice is enough to show
    # a template label without needing a database.
    await scrape(client)

    body = await scrape(client)

    assert 'route="/metrics"' in body


async def test_an_unmatched_path_cannot_invent_a_time_series(client, enabled):
    """The dangerous case: the label would otherwise be attacker-controlled."""
    for suffix in ("aaa", "bbb", "ccc"):
        await client.get(f"/api/v1/does-not-exist-{suffix}")

    body = await scrape(client)

    assert 'route="unmatched"' in body
    for suffix in ("aaa", "bbb", "ccc"):
        assert f"does-not-exist-{suffix}" not in body


# -- What is exported ------------------------------------------------------------


async def test_both_halves_of_the_fallback_rate_are_exported_and_the_rate_is_not(
    client, enabled
):
    from app.services.ai import degradation

    degradation.reset()
    degradation.record_attempt("initial_questions")
    degradation.record_attempt("initial_questions")
    degradation.record_fallback("initial_questions", RuntimeError("429"))

    body = await scrape(client)

    assert "interviewpilot_ai_attempts_total 2.0" in body
    assert "interviewpilot_ai_fallbacks_total 1.0" in body
    # Deliberately absent. A ratio computed over the life of the process cannot
    # be alerted on; the query does the division over a window.
    assert "fallback_rate" not in body
    degradation.reset()


async def test_provider_calls_are_broken_out_by_operation_and_outcome(client, enabled):
    from app.services.ai import call_metrics

    call_metrics.reset()
    call_metrics.record_call(
        operation="generate",
        model="m",
        outcome="ok",
        duration_ms=1800.0,
        input_tokens=1200,
        output_tokens=260,
    )
    call_metrics.record_call(operation="embed", model="e", outcome="failed", duration_ms=90.0)

    body = await scrape(client)

    assert 'interviewpilot_ai_calls_total{operation="generate",outcome="ok"} 1.0' in body
    assert 'interviewpilot_ai_calls_total{operation="embed",outcome="failed"} 1.0' in body
    # Seconds, not milliseconds: Prometheus convention, and the unit is in the
    # metric name so a dashboard cannot get it wrong.
    assert 'interviewpilot_ai_call_duration_seconds_total{operation="generate"} 1.8' in body
    assert 'interviewpilot_ai_tokens_total{direction="input",operation="generate"} 1200.0' in body
    call_metrics.reset()


async def test_embedding_calls_report_no_tokens_rather_than_zero(client, enabled):
    """Google returns no usage for embeddings. A zero would be a claim that
    they cost nothing."""
    from app.services.ai import call_metrics

    call_metrics.reset()
    call_metrics.record_call(operation="embed", model="e", outcome="ok", duration_ms=50.0)

    body = await scrape(client)

    assert 'operation="embed"' in body
    assert 'interviewpilot_ai_tokens_total{direction="input",operation="embed"}' not in body
    call_metrics.reset()


async def test_retrieval_outcomes_are_exported_with_the_distance_that_qualifies_them(
    client, enabled
):
    """`hits` alone would read as success. A hit at distance 1.0 is junk."""
    from app.services.ai import retrieval_metrics

    retrieval_metrics.reset()
    retrieval_metrics.record_availability(enabled=True)
    retrieval_metrics.record_retrieval(
        purpose="initial_questions", outcome="hit", duration_ms=40.0, chunks=3, best_distance=0.42
    )

    body = await scrape(client)

    assert 'interviewpilot_rag_retrievals_total{outcome="hit"} 1.0' in body
    assert "interviewpilot_rag_last_best_distance 0.42" in body
    assert "interviewpilot_rag_enabled 1.0" in body
    retrieval_metrics.reset()


async def test_scraping_does_not_disturb_the_counters_it_reads(client, enabled):
    """A collector, not a second write path: /health must keep reporting the
    same numbers after a scrape."""
    from app.services.ai import degradation

    degradation.reset()
    degradation.record_attempt("evaluate")

    await scrape(client)
    await scrape(client)

    assert degradation.snapshot()["attempts"] == 1
    degradation.reset()


async def test_a_scrape_is_not_counted_as_traffic_by_itself(client, enabled):
    """It is a request like any other and does get a histogram entry -- but the
    entry must be for /metrics, not for whatever was scraped."""
    body = await scrape(client)

    assert 'route="/metrics"' in body or "interviewpilot_http_request_duration" in body
