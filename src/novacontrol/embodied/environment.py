"""The environment adapter contract, its registry, and the allowlist.

An adapter is the ONLY thing in this package that touches an environment. The
interface is deliberately small — identity, connect, observe, act, release,
shutdown — because every additional method is another way for a subsystem to
grow its own private path into a game.

Three rules the interface enforces rather than documents:

1. **Everything returns a report.** No method raises to say "the environment
   said no"; refusals are statuses, so a caller cannot mistake an expected
   refusal for a crash.
2. **Nothing is assumed to exist.** ``status()`` answers with
   :class:`EnvironmentStatus`, and an adapter that cannot reach its
   environment says UNAVAILABLE with the reason — it does not pretend.
3. **The allowlist is the door.** Only environments an operator allowed may be
   connected; :class:`EnvironmentRegistry` refuses the rest by name, which is
   the mechanism that keeps this phase inside authorized environments.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from novacontrol.embodied.models import (
    ActionResult,
    EnvironmentAction,
    EnvironmentInfo,
    EnvironmentStatus,
    Observation,
)


class EnvironmentRefused(PermissionError):
    """An environment this build will not drive — not configured, not allowed."""


@dataclass(frozen=True, slots=True)
class ConnectReport:
    """What connecting did, and what the environment says about itself."""

    environment_id: str = ""
    connected: bool = False
    status: EnvironmentStatus = EnvironmentStatus.UNAVAILABLE
    reason: str = ""
    actions: tuple[str, ...] = ()
    observations: tuple[str, ...] = ()
    mock: bool = False
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.connected and not self.mock

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment_id": self.environment_id,
            "connected": self.connected,
            "status": self.status.value,
            "reason": self.reason,
            "actions": list(self.actions),
            "observations": list(self.observations),
            "mock": self.mock,
            "detail": {str(key): value for key, value in dict(self.detail).items()},
        }


@dataclass(frozen=True, slots=True)
class ReleaseReport:
    """Whether every held input is now released — the safety contract."""

    released: bool = True
    held: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "released": self.released,
            "held": list(self.held),
            "reason": self.reason,
        }


@runtime_checkable
class EnvironmentAdapter(Protocol):
    """One authorized environment, driven through a bounded interface.

    A concrete adapter never decides policy: it reports what it can do, does
    what it is told within the bounds it was given, and says honestly what
    happened. Safety, budgets and approvals live above it.
    """

    @property
    def info(self) -> EnvironmentInfo: ...

    def connect(self) -> ConnectReport: ...

    def status(self) -> EnvironmentStatus: ...

    def observe(self, *, session_id: str = "") -> Observation: ...

    def available_actions(self) -> tuple[str, ...]: ...

    def send_input(self, action: EnvironmentAction) -> ActionResult: ...

    def release_inputs(self) -> ReleaseReport: ...

    def shutdown(self) -> None: ...


class EnvironmentRegistry:
    """The environments this build may drive, and the allowlist that gates them.

    Constructed with an explicit allowlist. An empty allowlist means NOTHING is
    allowed, which is the safe default: an operator opts an environment in by
    name, and this class is the only place that decision is read.
    """

    #: The character that separates an environment FAMILY from the configuration
    #: it was started with (``gridworld:drifting``). An allowlist entry names the
    #: family, so permitting ``gridworld`` permits every scenario it can be
    #: started in — while a different environment still needs its own entry. The
    #: distinction is deliberate: a scenario is configuration, an environment is
    #: a grant, and only the operator may widen the second.
    CONFIGURATION_SEPARATOR = ":"

    def __init__(
        self,
        *,
        allowed: Sequence[str] = (),
        adapters: Sequence[EnvironmentAdapter] = (),
    ) -> None:
        self._allowed: set[str] = {str(name).strip() for name in allowed if str(name).strip()}
        self._adapters: dict[str, EnvironmentAdapter] = {}
        self._order: list[str] = []
        for adapter in adapters:
            self.register(adapter)

    # -- registration ----------------------------------------------------------

    def register(self, adapter: EnvironmentAdapter) -> None:
        """Add an adapter. Re-registering the same id replaces it in place."""
        info = adapter.info
        environment_id = info.environment_id.strip()
        if not environment_id:
            raise ValueError("An environment adapter needs an environment_id.")
        if environment_id not in self._adapters:
            self._order.append(environment_id)
        self._adapters[environment_id] = adapter

    def unregister(self, environment_id: str) -> bool:
        name = str(environment_id).strip()
        if name not in self._adapters:
            return False
        del self._adapters[name]
        self._order = [item for item in self._order if item != name]
        return True

    # -- reading ---------------------------------------------------------------

    def adapters(self) -> tuple[EnvironmentAdapter, ...]:
        return tuple(self._adapters[name] for name in self._order)

    def environments(self) -> tuple[EnvironmentInfo, ...]:
        rows: list[EnvironmentInfo] = []
        for name in self._order:
            adapter = self._adapters[name]
            info = adapter.info
            permitted = self.is_allowed(name)
            rows.append(info if info.allowed == permitted else _with_allowed(info, permitted))
        return tuple(rows)

    def allowed(self) -> tuple[str, ...]:
        return tuple(sorted(self._allowed))

    def family_of(self, environment_id: str) -> str:
        """The environment a configured id belongs to (``gridworld`` for
        ``gridworld:drifting``). An id with no configuration part is its own
        family."""
        name = str(environment_id).strip()
        return name.split(self.CONFIGURATION_SEPARATOR, 1)[0]

    def is_allowed(self, environment_id: str) -> bool:
        """Whether an operator allowed this environment OR its family."""
        name = str(environment_id).strip()
        return name in self._allowed or self.family_of(name) in self._allowed

    def get(self, environment_id: str) -> EnvironmentAdapter | None:
        return self._adapters.get(str(environment_id).strip())

    def require(self, environment_id: str) -> EnvironmentAdapter:
        """The adapter, or a refusal naming exactly what is wrong.

        Two distinct refusals are possible and they are kept distinct: an
        unknown environment ("this build has no such adapter") and a
        disallowed one ("this build has it, but an operator has not allowed
        it"). Folding them together would hide the second behind the first.
        """
        name = str(environment_id).strip()
        adapter = self._adapters.get(name)
        if adapter is None:
            known = ", ".join(self._order) or "none"
            raise EnvironmentRefused(
                f"no environment adapter is registered as {name!r}; this build has: {known}"
            )
        if not self.is_allowed(name):
            raise EnvironmentRefused(
                f"environment {name!r} is not in the allowlist; add it (or its "
                f"family {self.family_of(name)!r}) to 'embodied.environments' (or the "
                "equivalent config) to permit it"
            )
        return adapter


def _with_allowed(info: EnvironmentInfo, allowed: bool) -> EnvironmentInfo:
    return EnvironmentInfo(
        environment_id=info.environment_id,
        name=info.name,
        kind=info.kind,
        status=info.status,
        reason=info.reason,
        allowed=allowed,
        actions=info.actions,
        observations=info.observations,
        capabilities=info.capabilities,
    )


__all__ = [
    "ConnectReport",
    "EnvironmentAdapter",
    "EnvironmentRefused",
    "EnvironmentRegistry",
    "ReleaseReport",
]
