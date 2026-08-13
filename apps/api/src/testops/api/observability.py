"""Low-cardinality Prometheus instrumentation for the control plane."""

from __future__ import annotations

from time import perf_counter

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    GCCollector,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HTTP_DURATION_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)


class ApiMetrics:
    """Owns a per-application registry so test/app factories never collide."""

    def __init__(self, version: str) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        GCCollector(registry=self.registry)
        PlatformCollector(registry=self.registry)
        ProcessCollector(registry=self.registry)

        self.build_info = Gauge(
            "testops_build_info",
            "TestOps control-plane build information.",
            ("version",),
            registry=self.registry,
        )
        self.build_info.labels(version=version).set(1)
        self.requests_total = Counter(
            "testops_http_requests_total",
            "Completed control-plane HTTP requests.",
            ("method", "route", "status"),
            registry=self.registry,
        )
        self.request_duration = Histogram(
            "testops_http_request_duration_seconds",
            "Control-plane HTTP request duration in seconds.",
            ("method", "route"),
            buckets=HTTP_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.requests_in_progress = Gauge(
            "testops_http_requests_in_progress",
            "Control-plane HTTP requests currently being served.",
            ("method",),
            registry=self.registry,
        )
        self.database_ready = Gauge(
            "testops_database_ready",
            "Whether the latest control-plane database readiness probe succeeded.",
            registry=self.registry,
        )
        self.database_ready.set(0)

    def render(self) -> bytes:
        return generate_latest(self.registry)


class PrometheusMiddleware:
    """Record route templates rather than raw paths to bound label cardinality."""

    def __init__(self, app: ASGIApp, metrics: ApiMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/metrics":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "UNKNOWN")).upper()
        status_code = 500
        started_at = perf_counter()
        self.metrics.requests_in_progress.labels(method=method).inc()

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            self.metrics.requests_in_progress.labels(method=method).dec()
            route = scope.get("route")
            route_template = str(getattr(route, "path", "unmatched"))
            self.metrics.requests_total.labels(
                method=method,
                route=route_template,
                status=str(status_code),
            ).inc()
            self.metrics.request_duration.labels(
                method=method,
                route=route_template,
            ).observe(perf_counter() - started_at)
