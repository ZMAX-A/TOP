"""Celery application with deterministic task names and JSON-only payloads."""

from __future__ import annotations

import logging
import os
import time

from celery import Celery
from celery.signals import heartbeat_sent, worker_ready

from .config import WorkerSettings
from .control_plane import ControlPlaneClient

LOGGER = logging.getLogger(__name__)
_last_runner_heartbeat = 0.0

celery_app = Celery(
    "testops-worker",
    broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    include=["testops.worker.tasks"],
)
celery_app.conf.update(
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    timezone="UTC",
    enable_utc=True,
)


def _report_runner_heartbeat(*, force: bool = False) -> None:
    global _last_runner_heartbeat
    settings = WorkerSettings.from_environment()
    if settings.runner_worker_key is None or settings.runner_pool_key is None:
        return
    moment = time.monotonic()
    if not force and moment - _last_runner_heartbeat < settings.runner_heartbeat_interval_seconds:
        return
    _last_runner_heartbeat = moment
    try:
        ControlPlaneClient(
            settings.control_plane_url,
            settings.runner_callback_token,
        ).report_worker_heartbeat(
            settings.runner_worker_key,
            pool_key=settings.runner_pool_key,
            display_name=settings.runner_display_name,
            runner_version=settings.runner_version,
            max_slots=settings.runner_max_slots,
            capabilities=settings.runner_capabilities,
        )
    except Exception as exc:
        LOGGER.warning("Runner heartbeat failed: %s", exc)


@worker_ready.connect(weak=False)
def _worker_ready_heartbeat(**_kwargs: object) -> None:
    _report_runner_heartbeat(force=True)


@heartbeat_sent.connect(weak=False)
def _periodic_runner_heartbeat(**_kwargs: object) -> None:
    _report_runner_heartbeat()
