"""Metrics registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class MetricSample:
    name: str
    value: float
    kind: str
    tags: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "kind": self.kind,
            "tags": self.tags,
            "created_at": self.created_at.isoformat(),
        }


class MetricsRegistry:
    """In-memory metrics registry for counters, gauges, and timings."""

    def __init__(self) -> None:
        self._samples: list[MetricSample] = []
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}

    def increment(self, name: str, value: float = 1.0, *, tags: dict[str, str] | None = None) -> None:
        self._counters[name] = self._counters.get(name, 0.0) + value
        self._samples.append(MetricSample(name, self._counters[name], "counter", tags or {}))

    def gauge(self, name: str, value: float, *, tags: dict[str, str] | None = None) -> None:
        self._gauges[name] = value
        self._samples.append(MetricSample(name, value, "gauge", tags or {}))

    def timing(self, name: str, seconds: float, *, tags: dict[str, str] | None = None) -> None:
        self._samples.append(MetricSample(name, seconds, "timing", tags or {}))

    def snapshot(self) -> dict[str, Any]:
        return {
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "samples": [sample.to_dict() for sample in self._samples],
        }
