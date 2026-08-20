"""Low-cardinality Prometheus instrumentation for the control plane."""

from __future__ import annotations

from time import perf_counter
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from .quality_operations_services import QualityOperationsSnapshot
    from .reliability_services import ReliabilitySnapshot

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
        self.runs_in_flight = Gauge(
            "testops_runs_in_flight",
            "Current non-terminal Runs by status.",
            ("status",),
            registry=self.registry,
        )
        for status in ("QUEUED", "PREPARING", "RUNNING"):
            self.runs_in_flight.labels(status=status).set(0)
        self.dispatch_waiting_runs = Gauge(
            "testops_dispatch_waiting_runs",
            "Runs currently waiting for dispatch capacity.",
            registry=self.registry,
        )
        self.dispatch_backlog_oldest_age_seconds = Gauge(
            "testops_dispatch_backlog_oldest_age_seconds",
            "Age of the oldest Run waiting for dispatch capacity.",
            registry=self.registry,
        )
        self.schedule_due_backlog = Gauge(
            "testops_schedule_due_backlog",
            "Active regression schedules whose next fire time is due.",
            registry=self.registry,
        )
        self.schedule_lag_seconds = Gauge(
            "testops_schedule_lag_seconds",
            "Lag of the oldest due regression schedule.",
            registry=self.registry,
        )
        self.stale_runner_leases = Gauge(
            "testops_stale_runner_leases",
            "Active leases bound to Runner workers with expired heartbeats.",
            registry=self.registry,
        )
        self.reliability_snapshot_success = Gauge(
            "testops_reliability_snapshot_success",
            "Whether the latest reliability snapshot query succeeded.",
            registry=self.registry,
        )
        self.reliability_snapshot_success.set(0)
        self.quality_alert_evaluation_due_configs = Gauge(
            "testops_quality_alert_evaluation_due_configs",
            "Enabled quality alert configurations currently due for evaluation.",
            registry=self.registry,
        )
        self.quality_alert_evaluation_lag_seconds = Gauge(
            "testops_quality_alert_evaluation_lag_seconds",
            "Lag of the oldest due quality alert evaluation.",
            registry=self.registry,
        )
        self.quality_alert_active_silences = Gauge(
            "testops_quality_alert_active_silences",
            "Enabled quality alert configurations with an active silence window.",
            registry=self.registry,
        )
        self.quality_webhook_deliveries = Gauge(
            "testops_quality_webhook_deliveries",
            "Current quality Webhook delivery records by operational status.",
            ("status",),
            registry=self.registry,
        )
        for status in ("PENDING", "FAILED"):
            self.quality_webhook_deliveries.labels(status=status).set(0)
        self.quality_webhook_oldest_pending_age_seconds = Gauge(
            "testops_quality_webhook_oldest_pending_age_seconds",
            "Age of the oldest pending quality Webhook delivery.",
            registry=self.registry,
        )
        self.quality_webhook_replay_deliveries = Gauge(
            "testops_quality_webhook_replay_deliveries",
            "Persisted quality Webhook deliveries created by manual replay.",
            registry=self.registry,
        )
        self.quality_operations_snapshot_success = Gauge(
            "testops_quality_operations_snapshot_success",
            "Whether the latest quality operations snapshot query succeeded.",
            registry=self.registry,
        )
        self.quality_operations_snapshot_success.set(0)

    def update_reliability(self, snapshot: ReliabilitySnapshot) -> None:
        self.runs_in_flight.labels(status="QUEUED").set(snapshot.queued_runs)
        self.runs_in_flight.labels(status="PREPARING").set(snapshot.preparing_runs)
        self.runs_in_flight.labels(status="RUNNING").set(snapshot.running_runs)
        self.dispatch_waiting_runs.set(snapshot.dispatch_waiting_runs)
        self.dispatch_backlog_oldest_age_seconds.set(snapshot.dispatch_backlog_oldest_age_seconds)
        self.schedule_due_backlog.set(snapshot.due_schedules)
        self.schedule_lag_seconds.set(snapshot.schedule_lag_seconds)
        self.stale_runner_leases.set(snapshot.stale_runner_leases)
        self.reliability_snapshot_success.set(1)

    def update_quality_operations(self, snapshot: QualityOperationsSnapshot) -> None:
        self.quality_alert_evaluation_due_configs.set(snapshot.due_evaluations)
        self.quality_alert_evaluation_lag_seconds.set(snapshot.evaluation_lag_seconds)
        self.quality_alert_active_silences.set(snapshot.active_silences)
        self.quality_webhook_deliveries.labels(status="PENDING").set(snapshot.pending_deliveries)
        self.quality_webhook_deliveries.labels(status="FAILED").set(snapshot.failed_deliveries)
        self.quality_webhook_oldest_pending_age_seconds.set(snapshot.oldest_pending_age_seconds)
        self.quality_webhook_replay_deliveries.set(snapshot.replay_deliveries)
        self.quality_operations_snapshot_success.set(1)

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
