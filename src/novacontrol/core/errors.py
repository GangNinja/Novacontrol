"""Error boundaries for runtime and event delivery."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HandlerFailure:
    event: object
    handler: str
    error: BaseException

    def to_dict(self) -> dict[str, object]:
        """A reportable form: which event, which handler, what it raised."""
        event = self.event
        type_ = getattr(event, "type", "")
        return {
            "event_type": str(type_),
            "handler": self.handler,
            "error": f"{type(self.error).__name__}: {self.error}",
        }


class EventDeliveryError(RuntimeError):
    """Raised when an event handler fails and the bus is not in continue mode."""

    def __init__(self, event: object, failures: list[HandlerFailure]) -> None:
        self.event = event
        self.failures = tuple(failures)
        super().__init__(f"Failed to deliver event to {len(failures)} handler(s).")


class ModuleLifecycleError(RuntimeError):
    """Raised when a runtime module cannot start or stop cleanly."""

    def __init__(self, module_name: str, phase: str, error: BaseException) -> None:
        self.module_name = module_name
        self.phase = phase
        self.error = error
        super().__init__(f"Module {module_name!r} failed during {phase}: {error}")
