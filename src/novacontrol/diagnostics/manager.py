"""Self-diagnostics: what this installation can actually do right now.

Phase 14.7 asks for one roster of components, each checked and reported as a
structured result rather than a sentence in a log:

    component, status, severity, message, remediation, metadata

That shape is the whole design. ``status`` is the shared ``HealthState`` (so a
runtime health report and a Phase 14 diagnostic row speak the same five words),
``severity`` says how much the status matters, ``message`` states what was
observed, ``remediation`` says what to DO about it — and is empty when there is
nothing to do — and ``metadata`` carries the readings behind the conclusion, so
a report can be audited instead of trusted.

Two rules, both about honesty:

* A check that cannot run is not a failure and not a pass. ``SKIPPED`` (this
  component is deliberately off) and ``UNKNOWN`` (the platform would not tell)
  exist so the roster never has to lie in either direction.
* A check that throws, times out, or returns something that is not a
  ``DiagnosticResult`` becomes a FAILING row with the exception in it. One
  broken probe must never take the roster down, and a diagnostic that crashes
  silently is worse than one that reports a fault.

Checks may be synchronous or awaitable. Synchronous ones run in a worker thread
with a timeout, so a probe that reaches for a runtime or a driver cannot stall
the event loop the rest of the application is using — which is the "do not block
the UI or main event loop" requirement applied to the layer whose job is to
notice exactly that kind of problem.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from inspect import isawaitable
from typing import Any

from novacontrol.core.diagnostics import HealthState

#: Symbols for the display form, which is the example in the specification.
_SYMBOLS: Mapping[HealthState, str] = {
    HealthState.OK: "✓",
    HealthState.DEGRADED: "⚠",
    HealthState.FAILING: "✗",
    HealthState.SKIPPED: "–",
    HealthState.UNKNOWN: "?",
}


class Severity(StrEnum):
    """How much a status matters, kept separate from the status itself.

    A component can be OK at WARNING severity (working, but on a deprecated
    path) or SKIPPED at INFO (deliberately off). Collapsing the two into one
    field would force every report to choose which fact to lose.
    """

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        """How loudly this severity reads, worst last."""
        return list(Severity).index(self)


#: The severity a status carries unless the check states its own.
SEVERITY_FOR_STATE: Mapping[HealthState, Severity] = {
    HealthState.OK: Severity.INFO,
    HealthState.DEGRADED: Severity.WARNING,
    HealthState.FAILING: Severity.ERROR,
    HealthState.SKIPPED: Severity.INFO,
    HealthState.UNKNOWN: Severity.WARNING,
}


@dataclass(frozen=True, slots=True)
class DiagnosticResult:
    """One component, one check, one structured answer."""

    component: str
    status: HealthState
    message: str
    severity: Severity | None = None
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    duration_ms: float | None = None

    def __post_init__(self) -> None:
        if self.severity is None:
            object.__setattr__(self, "severity", SEVERITY_FOR_STATE[self.status])

    @property
    def needs_attention(self) -> bool:
        return not self.status.healthy

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "status": self.status.value,
            "severity": (self.severity or SEVERITY_FOR_STATE[self.status]).value,
            "message": self.message,
            "remediation": self.remediation,
            "metadata": dict(self.metadata),
            "checked_at": self.checked_at.isoformat(),
            "duration_ms": (
                round(float(self.duration_ms), 3) if self.duration_ms is not None else None
            ),
        }


#: ``() -> result``, synchronous or awaitable. Registered per component.
DiagnosticCheck = Callable[[], DiagnosticResult | Awaitable[DiagnosticResult]]


@dataclass(frozen=True, slots=True)
class DiagnosticSummary:
    """The roster at a glance: the overall state, and who is responsible."""

    overall: HealthState
    counts: Mapping[str, int]
    needs_attention: tuple[str, ...]
    skipped: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.overall is HealthState.OK

    @property
    def degraded(self) -> bool:
        """True when the installation is running below its full capability."""
        return self.overall in (HealthState.DEGRADED, HealthState.FAILING, HealthState.UNKNOWN)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": self.overall.value,
            "ok": self.ok,
            "degraded": self.degraded,
            "counts": dict(self.counts),
            "needs_attention": list(self.needs_attention),
            "skipped": list(self.skipped),
        }


class DiagnosticManager:
    """The component roster and the one place a diagnostic check is run.

    Registration order is report order, because the order is the story: core
    first, the machine next, the optional systems last. Duplicates raise rather
    than overwrite — a second check under one name is a wiring bug, and the
    existing runtime registry makes the same call.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 5.0,
        stamp: Callable[[], datetime] | None = None,
    ) -> None:
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self._stamp = stamp or (lambda: datetime.now(UTC))
        self._checks: dict[str, DiagnosticCheck] = {}
        self._categories: dict[str, str] = {}
        self._last: tuple[DiagnosticResult, ...] = ()

    # -- registration --------------------------------------------------------
    def register(
        self, component: str, check: DiagnosticCheck, *, category: str = ""
    ) -> None:
        """Add a component to the roster. Names are stable and unique."""
        name = str(component or "").strip()
        if not name:
            raise ValueError("a diagnostic component needs a name")
        if name in self._checks:
            raise ValueError(f"diagnostic component already registered: {name}")
        self._checks[name] = check
        self._categories[name] = str(category or "")

    @property
    def components(self) -> tuple[str, ...]:
        """The roster, in registration order."""
        return tuple(self._checks)

    def registered(self) -> tuple[tuple[str, str], ...]:
        """``(component, category)`` pairs, in registration order."""
        return tuple((name, self._categories[name]) for name in self._checks)

    # -- running --------------------------------------------------------------
    async def check(self, component: str) -> DiagnosticResult:
        """Run one component's check. An unknown name raises ``KeyError``."""
        name = str(component or "").strip()
        if name not in self._checks:
            raise KeyError(f"no diagnostic component named {name!r}")
        return await self._invoke(name, self._checks[name])

    async def run(self, *, only: Sequence[str] | None = None) -> tuple[DiagnosticResult, ...]:
        """Run the roster (or the named subset) and return one row per component.

        A subset keeps the roster's order, so two runs of the same components
        produce comparable reports. Every failure mode — an exception, a
        timeout, a check that returns the wrong type — becomes a FAILING row
        rather than an interruption.
        """
        names = self._selected(only)
        results = tuple(
            await asyncio.gather(*(self._invoke(name, self._checks[name]) for name in names))
        )
        self._last = results
        return results

    @property
    def last_results(self) -> tuple[DiagnosticResult, ...]:
        """The most recent run, or ``()`` before the first one."""
        return self._last

    def _selected(self, only: Sequence[str] | None) -> tuple[str, ...]:
        if only is None:
            return tuple(self._checks)
        wanted = {str(name).strip() for name in only}
        unknown = sorted(wanted - set(self._checks))
        if unknown:
            raise KeyError(f"no diagnostic component named {unknown[0]!r}")
        return tuple(name for name in self._checks if name in wanted)

    async def _invoke(self, name: str, check: DiagnosticCheck) -> DiagnosticResult:
        started = _now()
        try:
            outcome = await asyncio.wait_for(self._call(check), timeout=self.timeout_seconds)
        except TimeoutError:
            return self._failure(
                name,
                started,
                f"the check did not answer within {self.timeout_seconds:g}s",
                "the component is not responding; check it directly, or raise the timeout",
                {"timeout_seconds": self.timeout_seconds},
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken probe must not take the roster down
            return self._failure(
                name,
                started,
                f"the check raised {type(exc).__name__}: {exc}",
                "",
                {"error": type(exc).__name__, "message": str(exc)},
            )
        if not isinstance(outcome, DiagnosticResult):
            return self._failure(
                name,
                started,
                f"the check returned {type(outcome).__name__}, not a DiagnosticResult",
                "fix the component's check registration",
                {},
            )
        return DiagnosticResult(
            component=name,
            status=outcome.status,
            message=outcome.message,
            severity=outcome.severity,
            remediation=outcome.remediation,
            metadata=dict(outcome.metadata),
            checked_at=outcome.checked_at if outcome.checked_at else self._stamp(),
            duration_ms=outcome.duration_ms
            if outcome.duration_ms is not None
            else _elapsed_ms(started),
        )

    async def _call(self, check: DiagnosticCheck) -> DiagnosticResult:
        """Run a check, off the event loop when it is synchronous.

        The thread hop is deliberate: the manager's job includes noticing a
        stuck runtime, so its own probes must not be the thing that stalls the
        loop while they wait for one. An awaitable check is CALLED in the
        thread (which does not run it) and then awaited on the loop.
        """
        outcome = await asyncio.to_thread(check)
        if isawaitable(outcome):
            return await outcome
        return outcome

    def _failure(
        self,
        name: str,
        started: float,
        message: str,
        remediation: str,
        metadata: dict[str, Any],
    ) -> DiagnosticResult:
        return DiagnosticResult(
            component=name,
            status=HealthState.FAILING,
            message=message,
            remediation=remediation,
            metadata=metadata,
            checked_at=self._stamp(),
            duration_ms=_elapsed_ms(started),
        )

    # -- reporting ------------------------------------------------------------
    def summarize(self, results: Sequence[DiagnosticResult] | None = None) -> DiagnosticSummary:
        """Count the rows and name the worst state among them."""
        rows = tuple(results) if results is not None else self._last
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.status.value] = counts.get(row.status.value, 0) + 1
        overall = HealthState.OK
        for row in rows:
            if row.status.severity_rank > overall.severity_rank:
                overall = row.status
        return DiagnosticSummary(
            overall=overall,
            counts=counts,
            needs_attention=tuple(
                row.component for row in rows if row.needs_attention
            ),
            skipped=tuple(
                row.component for row in rows if row.status is HealthState.SKIPPED
            ),
        )

    def report_lines(
        self, results: Sequence[DiagnosticResult] | None = None
    ) -> tuple[str, ...]:
        """The checklist a person reads, one line per component."""
        rows = tuple(results) if results is not None else self._last
        width = max((len(row.component) for row in rows), default=0)
        lines = ["NovaControl Health"]
        for row in rows:
            symbol = _SYMBOLS[row.status]
            lines.append(f"{row.component:<{width}}  {symbol}")
        return tuple(lines)

    def to_dict(self, results: Sequence[DiagnosticResult] | None = None) -> dict[str, Any]:
        rows = tuple(results) if results is not None else self._last
        return {
            "summary": self.summarize(rows).to_dict(),
            "components": [row.to_dict() for row in rows],
            "lines": list(self.report_lines(rows)),
        }


def _now() -> float:
    return time.monotonic()


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000.0, 3)


__all__ = [
    "SEVERITY_FOR_STATE",
    "DiagnosticCheck",
    "DiagnosticManager",
    "DiagnosticResult",
    "DiagnosticSummary",
    "Severity",
]
