"""Operational health collection over the existing diagnostic roster.

This module does not register a second set of probes.  It runs the application's
``DiagnosticManager`` — whose checks already read live services — measures each
check's duration, and attaches the machine readings and recent failure events
that make a diagnosis possible.  A component with no probe is UNKNOWN, not OK.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from novacontrol.core.diagnostics import HealthState
from novacontrol.diagnostics.manager import DiagnosticManager, DiagnosticResult
from novacontrol.self_management.models import (
    ComponentHealth,
    HealthSnapshot,
    IncidentSeverity,
    new_id,
)

#: Event prefixes whose failed endings describe an operational fault.  The
#: signal is derived from events the system actually published; no probe invents
#: a failure to make the detector look useful.
_FAILURE_EVENT_COMPONENT: Mapping[str, str] = {
    "tool.failed": "Tools",
    "task.failed": "Agent execution",
    "automation.failed": "Scheduler",
    "operations.task.failed": "Background workers",
    "collaboration.failed": "Agent execution",
    "agent.task_failed": "Agent execution",
}


def _component_for_event(event_type: str) -> str:
    if event_type in _FAILURE_EVENT_COMPONENT:
        return _FAILURE_EVENT_COMPONENT[event_type]
    if event_type.startswith("tool."):
        return "Tools"
    if event_type.startswith("operations."):
        return "Background workers"
    if event_type.startswith("automation."):
        return "Scheduler"
    if event_type.startswith("collaboration.") or event_type.startswith("agent."):
        return "Agent execution"
    if event_type.startswith("model."):
        return "Model manager"
    if event_type.startswith("vision."):
        return "Vision"
    return "Event bus"


def _is_failure_event(event_type: str) -> bool:
    return event_type.endswith(".failed") or event_type.endswith("_failed")


class HealthCollector:
    """Run real checks and turn them into one bounded operational snapshot."""

    def __init__(
        self,
        diagnostics: DiagnosticManager,
        *,
        metrics: Callable[[], Mapping[str, Any]] | None = None,
        event_bus: Any | None = None,
        event_window_seconds: float = 900.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.diagnostics = diagnostics
        self._metrics = metrics
        self.event_bus = event_bus
        self.event_window_seconds = max(1.0, float(event_window_seconds))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._last: HealthSnapshot | None = None

    @property
    def last(self) -> HealthSnapshot | None:
        return self._last

    async def collect(self, *, only: tuple[str, ...] | None = None) -> HealthSnapshot:
        started = time.monotonic()
        rows = await self.diagnostics.run(only=only)
        components = tuple(self._component(row) for row in rows)
        # An accelerator the host does not expose is optional: its UNKNOWN row
        # stays visible, but it must not make the whole system look unreadable.
        optional_unknown = {"GPU", "NPU"}
        overall = HealthState.OK
        for row in components:
            if row.component in optional_unknown and row.status is HealthState.UNKNOWN:
                continue
            if row.status.severity_rank > overall.severity_rank:
                overall = row.status
        metrics: dict[str, Any] = {}
        if self._metrics is not None:
            try:
                metrics = dict(self._metrics())
            except Exception as exc:  # a metrics failure must not hide health
                metrics = {
                    "available": False,
                    "error": type(exc).__name__,
                    "message": str(exc),
                }
        snapshot = HealthSnapshot(
            snapshot_id=new_id("health"),
            overall=overall,
            components=components,
            metrics=metrics,
            checked_at=self._clock(),
            duration_ms=round((time.monotonic() - started) * 1000.0, 3),
            event_signals=self.event_signals(),
        )
        self._last = snapshot
        return snapshot

    def _component(self, row: DiagnosticResult) -> ComponentHealth:
        category = ""
        for name, value in self.diagnostics.registered():
            if name == row.component:
                category = value
                break
        return ComponentHealth(
            component=row.component,
            category=category,
            status=row.status,
            message=row.message,
            severity=IncidentSeverity.from_health(row.status),
            duration_ms=row.duration_ms,
            checked_at=row.checked_at,
            metadata=dict(row.metadata),
        )

    def event_signals(self) -> tuple[Mapping[str, Any], ...]:
        """Bounded, evidence-preserving counts of recent failure events."""
        if self.event_bus is None:
            return ()
        try:
            events = self.event_bus.recent(limit=200)
        except Exception:
            return ()
        cutoff = self._clock() - timedelta(seconds=self.event_window_seconds)
        counts: Counter[str] = Counter()
        examples: dict[str, list[str]] = {}
        for event in events:
            try:
                created = event.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                if created < cutoff:
                    continue
                event_type = str(event.type)
                if not _is_failure_event(event_type):
                    continue
                component = _component_for_event(event_type)
                counts[component] += 1
                if len(examples.setdefault(component, [])) < 3:
                    examples[component].append(
                        f"{event_type} ({str(event.payload.get('error', ''))[:120]})"
                    )
            except Exception:
                continue
        return tuple(
            {
                "component": component,
                "failures": count,
                "examples": list(examples.get(component, ())),
            }
            for component, count in sorted(counts.items())
        )

    def component(self, name: str) -> ComponentHealth | None:
        if self._last is None:
            return None
        for row in self._last.components:
            if row.component == name:
                return row
        return None

    async def check_component(self, name: str) -> ComponentHealth:
        """Run one existing diagnostic probe, for recovery verification."""
        result = await self.diagnostics.check(name)
        return self._component(result)
