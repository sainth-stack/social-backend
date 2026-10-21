"""Per-request MongoDB command counters (feeds access logs and Server-Timing)."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from pymongo import monitoring


@dataclass
class DbMetrics:
    count: int = 0
    micros: int = 0


_current: ContextVar[DbMetrics | None] = ContextVar("db_metrics", default=None)


def start_request_metrics() -> DbMetrics:
    metrics = DbMetrics()
    _current.set(metrics)
    return metrics


class CommandMetricsListener(monitoring.CommandListener):
    def started(self, event: monitoring.CommandStartedEvent) -> None:
        return None

    def succeeded(self, event: monitoring.CommandSucceededEvent) -> None:
        self._record(event.duration_micros)

    def failed(self, event: monitoring.CommandFailedEvent) -> None:
        self._record(event.duration_micros)

    @staticmethod
    def _record(duration_micros: int) -> None:
        metrics = _current.get()
        if metrics is not None:
            metrics.count += 1
            metrics.micros += duration_micros
