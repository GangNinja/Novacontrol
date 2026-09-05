"""Runtime orchestration for NovaControl."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from novacontrol.core.diagnostics import DiagnosticsRegistry, HealthReport, HealthState
from novacontrol.core.errors import ModuleLifecycleError
from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import RuntimeModule
from novacontrol.core.retry import RetryPolicy, with_retries
from novacontrol.core.services import ServiceContainer


@dataclass(slots=True)
class RuntimeState:
    started: bool = False
    module_names: list[str] = field(default_factory=list)
    started_at: datetime | None = None
    last_error: str | None = None


class EventDrivenRuntime:
    """Coordinates module lifecycle while preserving event-based decoupling."""

    def __init__(
        self,
        event_bus: EventBus | None = None,
        *,
        services: ServiceContainer | None = None,
        diagnostics: DiagnosticsRegistry | None = None,
        start_retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.event_bus = event_bus or EventBus()
        self.services = services or ServiceContainer()
        self.diagnostics = diagnostics or DiagnosticsRegistry()
        self.start_retry_policy = start_retry_policy or RetryPolicy()
        self._modules: dict[str, RuntimeModule] = {}
        self.state = RuntimeState()
        self.services.register("event_bus", self.event_bus, replace=True)
        self.services.register("diagnostics", self.diagnostics, replace=True)
        self.diagnostics.register("core.runtime", self._runtime_health)

    def register_module(self, module: RuntimeModule) -> None:
        """Register a module before the runtime starts."""
        if self.state.started:
            raise RuntimeError("Cannot register modules after runtime start.")
        if module.name in self._modules:
            raise ValueError(f"Module already registered: {module.name}")
        self._modules[module.name] = module
        self.state.module_names = sorted(self._modules)

    async def start(self) -> None:
        """Start all modules and publish a runtime started event."""
        if self.state.started:
            return
        started_modules: list[RuntimeModule] = []
        for module in self._modules.values():
            async def _start(m: RuntimeModule = module) -> None:
                await m.start(self.event_bus)

            async def _on_retry(attempt: int, exc: BaseException, m: RuntimeModule = module) -> None:
                await self._publish_retry(m.name, attempt, exc)

            try:
                await with_retries(_start, self.start_retry_policy, on_retry=_on_retry)
                started_modules.append(module)
                await self.event_bus.publish(
                    Event(
                        type="runtime.module_started",
                        payload={"module": module.name},
                        source="core.runtime",
                    )
                )
            except Exception as exc:
                self.state.last_error = str(exc)
                await self.event_bus.publish(
                    Event(
                        type="runtime.module_start_failed",
                        payload={
                            "module": module.name,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                        source="core.runtime",
                    )
                )
                await self._stop_modules(started_modules)
                raise ModuleLifecycleError(module.name, "start", exc) from exc
        self.state.started = True
        self.state.started_at = datetime.now(UTC)
        self.state.last_error = None
        await self.event_bus.publish(
            Event(
                type="runtime.started",
                payload={"modules": tuple(self.state.module_names)},
                source="core.runtime",
            )
        )

    async def stop(self) -> None:
        """Stop modules in reverse registration order."""
        if not self.state.started:
            return
        await self._stop_modules(tuple(self._modules.values()))
        self.state.started = False
        self.state.started_at = None
        await self.event_bus.publish(Event(type="runtime.stopped", source="core.runtime"))
        await self.services.dispose()

    def module_names(self) -> tuple[str, ...]:
        """Return registered module names."""
        return tuple(self.state.module_names)

    async def health(self) -> tuple[HealthReport, ...]:
        """Run registered health checks."""
        return await self.diagnostics.run()

    async def _publish_retry(self, module_name: str, attempt: int, exc: BaseException) -> None:
        await self.event_bus.publish(
            Event(
                type="runtime.module_start_retrying",
                payload={
                    "module": module_name,
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                source="core.runtime",
            )
        )

    async def _stop_modules(self, modules: tuple[RuntimeModule, ...] | list[RuntimeModule]) -> None:
        for module in reversed(tuple(modules)):
            try:
                await module.stop()
            except Exception as exc:
                self.state.last_error = str(exc)
                await self.event_bus.publish(
                    Event(
                        type="runtime.module_stop_failed",
                        payload={
                            "module": module.name,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                        source="core.runtime",
                    )
                )

    def _runtime_health(self) -> HealthReport:
        return HealthReport(
            name="core.runtime",
            state=HealthState.OK if self.state.last_error is None else HealthState.DEGRADED,
            details={
                "started": self.state.started,
                "modules": tuple(self.state.module_names),
                "last_error": self.state.last_error,
            },
        )
