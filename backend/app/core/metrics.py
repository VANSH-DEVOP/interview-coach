"""Prometheus exposition for counters this application already keeps.

**Nothing here replaces the existing counters.** `degradation.py`,
`call_metrics.py`, `retrieval_metrics.py`, `rate_limit.py` and `job_queue.py`
stay exactly as they are, and this reads their snapshots at scrape time. Two
reasons: `/health` keeps working from the same numbers rather than a second set
that can disagree with it, and a rewrite of five modules to emit Prometheus
types directly would risk the one property they all have -- that recording a
metric can never break the request that produced it.

**Process-local state is correct here, unlike at `/health`.** Every one of those
modules documents that its counters reset with the process and are not shared
between replicas, which is a caveat when a human reads one instance's `/health`.
Prometheus scrapes each instance separately and sums across them, so
per-process is precisely what it wants.

## Two conventions that are not decoration

**Raw counters, never pre-computed rates.** `/health` reports
`ai.fallback_rate`, and that number is an average over the life of the process:
after a day of healthy traffic, an hour of total provider failure barely moves
it. Alerting needs `rate(fallbacks[5m]) / rate(attempts[5m])`, which needs the
two counters and not their quotient. So the ratios are deliberately absent from
this endpoint even though the source module computes them.

**Route templates, never raw paths.** An HTTP metric labelled with
`/api/v1/interviews/9f3c...` creates a new time series per interview, and the
label is attacker-controlled on a 404 -- a few thousand requests to random URLs
would exhaust the scrape's memory rather than ours. The label is the matched
route's *template*, and anything that matched no route is `unmatched`.
"""

from __future__ import annotations

from typing import Any, Iterator

from prometheus_client import CollectorRegistry, Histogram
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

NAMESPACE = "interviewpilot"

# Its own registry rather than the global default, which prometheus_client
# pre-populates with process and garbage-collector metrics and which any
# imported library can write to. An explicit registry keeps this endpoint's
# contents something this file decides.
REGISTRY = CollectorRegistry()

# Buckets in seconds, chosen for what this application actually does: most
# requests are database reads in single-digit milliseconds, and the interesting
# tail is the AI routes, where a provider round-trip is seconds and the timeout
# is 30. The default buckets top out at 10 and would put every slow generation
# in the same bucket as every timeout.
_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

REQUEST_DURATION = Histogram(
    f"{NAMESPACE}_http_request_duration_seconds",
    "HTTP request latency by route template.",
    labelnames=("method", "route", "status"),
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)


def observe_request(*, method: str, route: str, status: int, seconds: float) -> None:
    """Record one HTTP request.

    A Histogram already carries `_count`, so there is no separate request
    counter to keep in step with it.
    """
    REQUEST_DURATION.labels(method=method, route=route, status=str(status)).observe(seconds)


def _number(value: Any) -> float:
    """A snapshot field as a float, treating an absent measurement as zero.

    The snapshots deliberately use None for "no such measurement" -- a rate
    before anything was attempted, tokens for a provider that reports none.
    A gauge can carry that distinction and a counter cannot, so counters take
    zero and the two gauges below are guarded at their call sites instead.
    """
    return float(value) if isinstance(value, (int, float)) else 0.0


def _counter(name: str, doc: str, value: float, **labels: str) -> CounterMetricFamily:
    # `_total` is appended by the client for counters, so the name given here
    # must not already end in it.
    family = CounterMetricFamily(f"{NAMESPACE}_{name}", doc, labels=list(labels))
    family.add_metric(list(labels.values()), value)
    return family


def _labelled_counter(
    name: str, doc: str, label: str, values: dict[str, float]
) -> CounterMetricFamily:
    family = CounterMetricFamily(f"{NAMESPACE}_{name}", doc, labels=[label])
    for key, value in values.items():
        family.add_metric([key], value)
    return family


def _gauge(name: str, doc: str, value: float) -> GaugeMetricFamily:
    return GaugeMetricFamily(f"{NAMESPACE}_{name}", doc, value=value)


class ApplicationCollector:
    """Reads the application's own counters when Prometheus asks.

    A collector rather than a set of `Counter` objects updated at the call
    sites: the call sites already record everything, and a second write path
    would be a second thing to forget. The cost is that a scrape walks a few
    dictionaries, which is cheaper than the HTTP request carrying it.
    """

    def collect(self) -> Iterator[Any]:
        # Imported here rather than at module scope: this module is imported by
        # the app factory, and importing the AI package at that point would
        # pull in its dependency tree before the settings that decide whether
        # any of it is used.
        from app.core import rate_limit
        from app.services import job_queue
        from app.services.ai import call_metrics, degradation, retrieval_metrics

        yield from self._ai(degradation.snapshot())
        yield from self._calls(call_metrics.snapshot())
        yield from self._rag(retrieval_metrics.snapshot())
        yield _counter(
            "rate_limit_fallbacks",
            "Times a rate-limit check fell back to per-process counting.",
            _number(rate_limit.snapshot()["fallbacks"]),
        )
        yield _counter(
            "queue_fallbacks",
            "Times an evaluation ran in-process because the queue was unusable.",
            _number(job_queue.snapshot()["fallbacks"]),
        )

    def _ai(self, ai: dict[str, Any]) -> Iterator[Any]:
        # Both halves of the fallback rate, and not the rate itself: see the
        # module docstring. `attempts` is the denominator that makes three
        # fallbacks readable as a catastrophe or as noise.
        yield _counter(
            "ai_attempts",
            "AI operations attempted against a real provider.",
            _number(ai["attempts"]),
        )
        yield _counter(
            "ai_fallbacks",
            "AI operations that degraded to the deterministic implementation.",
            _number(ai["fallbacks"]),
        )

    def _calls(self, calls: dict[str, Any]) -> Iterator[Any]:
        by_operation: dict[str, Any] = calls.get("by_operation") or {}

        totals = CounterMetricFamily(
            f"{NAMESPACE}_ai_calls",
            "Provider round-trips.",
            labels=["operation", "outcome"],
        )
        duration = CounterMetricFamily(
            f"{NAMESPACE}_ai_call_duration_seconds",
            "Total provider round-trip time. Divide by the call count for a mean.",
            labels=["operation"],
        )
        tokens = CounterMetricFamily(
            f"{NAMESPACE}_ai_tokens",
            "Tokens reported by the provider. Absent for embeddings, which report none.",
            labels=["operation", "direction"],
        )
        for operation, stats in by_operation.items():
            totals.add_metric([operation, "ok"], _number(stats["ok"]))
            totals.add_metric([operation, "failed"], _number(stats["failed"]))
            # A mean and a count are what these modules keep; a sum is
            # reconstructed rather than invented, and a real histogram would
            # mean changing how call_metrics records.
            mean_ms = _number(stats.get("avg_ms"))
            duration.add_metric([operation], (mean_ms * _number(stats["calls"])) / 1000.0)
            for direction in ("input", "output"):
                value = stats.get(f"{direction}_tokens")
                if value is not None:
                    tokens.add_metric([operation, direction], _number(value))
        yield totals
        yield duration
        yield tokens

    def _rag(self, rag: dict[str, Any]) -> Iterator[Any]:
        yield _labelled_counter(
            "rag_retrievals",
            "Retrieval attempts by outcome. A hit is not a success -- see last_best_distance.",
            "outcome",
            {
                "hit": _number(rag["hits"]),
                "empty": _number(rag["empty"]),
                "failed": _number(rag["failed"]),
            },
        )
        yield _counter(
            "rag_full_text_fallbacks",
            "Generations that used truncated resume text instead of retrieved context.",
            _number(rag["full_text_fallbacks"]),
        )
        yield _labelled_counter(
            "rag_fusions",
            "Hybrid retrievals by which half contributed.",
            "source",
            {
                "dense_only": _number(rag["dense_only"]),
                "sparse_only": _number(rag["sparse_only"]),
                "total": _number(rag["fusions"]),
            },
        )
        yield _labelled_counter(
            "rag_cache",
            "Embedding-cache lookups. Errors are counted apart from misses on purpose.",
            "outcome",
            {
                "hit": _number(rag["cache_hits"]),
                "miss": _number(rag["cache_misses"]),
                "error": _number(rag["cache_errors"]),
            },
        )
        yield _labelled_counter(
            "rag_chunks",
            "Chunks produced by chunking versus chunks that reached the index.",
            "stage",
            {
                "produced": _number(rag["chunks_produced"]),
                "embedded": _number(rag["chunks_embedded"]),
            },
        )
        # A gauge, not a counter: it describes the present, and `enabled` is
        # null until something has tried to build the service.
        if rag["enabled"] is not None:
            yield _gauge(
                "rag_enabled", "Whether retrieval is switched on.", 1.0 if rag["enabled"] else 0.0
            )
        if rag["last_best_distance"] is not None:
            yield _gauge(
                "rag_last_best_distance",
                "Cosine distance of the best chunk on the last hit. Near 1.0 means junk.",
                _number(rag["last_best_distance"]),
            )


REGISTRY.register(ApplicationCollector())  # type: ignore[arg-type]
