"""The prediction interface — and the honest answer when nothing can predict (§16/§17).

A world state store is not a predictive world model, and this module exists to make
that difference legible rather than to blur it. Three positions are encoded here:

**Nothing is predicted by default.** :class:`NoPredictionProvider` is the default
provider and it answers ``MODEL_UNAVAILABLE`` with the reason. A request for a
future state therefore ends with "no predictive model is wired", never with a
plausible-looking guess — because a fabricated prediction is the most damaging
output this phase could produce.

**A rule projection is labelled as one.** :class:`RuleProjectionProvider` does
real, checkable arithmetic — it measures the displacement the state already
recorded between two observations, divides by the time between them, and extends
it over the horizon the caller asked for. It sets ``rule_based=True``, names
itself ``rule-projection``, reports ``confidence=None`` (nothing measured it) and
states in ``limitations`` that no model was involved. It is a projection of
observed motion, not learned intelligence, and the result says so in words a
reader and a test can both check.

**Resource admission is respected before inference.** A provider that needs a
model is asked through the SAME gate the perception layer uses. A refusal is
``RESOURCE_BLOCKED`` — a distinct outcome, not a failed prediction, because
nothing tried and failed: the machine said no.

A provider that raises, returns the wrong type, or overruns its budget ends as
``FAILED`` with the reason. No failure of an optional provider can prevent a
deterministic state query from working: this module is only ever reached from an
explicit prediction request.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from novacontrol.world.models import (
    EntityStatus,
    PredictionRequest,
    PredictionResult,
    PredictionStatus,
    WorldEntity,
    WorldState,
    WorldStateTransition,
    as_optional_int,
)
from novacontrol.world.timeutil import seconds_between

__all__ = [
    "DEFAULT_MAX_HORIZON_SECONDS",
    "NoPredictionProvider",
    "PredictionContext",
    "PredictionProvider",
    "PredictionService",
    "ResourceGateLike",
    "RuleProjectionProvider",
]

#: The furthest a rule projection may reach. Beyond this the linear assumption is
#: so weak that reporting a number would be more misleading than reporting nothing.
DEFAULT_MAX_HORIZON_SECONDS = 300.0

#: The default budget for one provider call. A provider that overruns it is
#: reported as FAILED with the measurement, not silently accepted.
DEFAULT_TIMEOUT_MS = 5000


@dataclass(frozen=True, slots=True)
class PredictionContext:
    """What a provider may read: the state, its transitions and its retained versions."""

    state: WorldState
    transitions: tuple[WorldStateTransition, ...] = ()
    snapshots: tuple[WorldState, ...] = ()


@runtime_checkable
class PredictionProvider(Protocol):
    """A predictive provider. Three attributes, one method."""

    name: str
    available: bool
    needs_model: bool

    def predict(
        self, request: PredictionRequest, context: PredictionContext
    ) -> PredictionResult:
        """One prediction, or an honest statement of why there is none."""
        ...


@runtime_checkable
class ResourceGateLike(Protocol):
    """The admission gate, structurally — the perception gate already satisfies it."""

    def admit(
        self,
        *,
        needs_model: bool,
        required_bytes: int | None = None,
        model: str = "",
        latency_budget_ms: int | None = None,
    ) -> Any:
        ...


class NoPredictionProvider:
    """The default: there is no predictive model, and this says so.

    It exists as a class rather than as ``None`` so that "no provider wired" is a
    provider that ANSWERED — the same reason the vision layer ships a null
    provider. A caller cannot confuse an absent provider with an absent answer.
    """

    name = "none"
    available = False
    needs_model = False

    def predict(
        self, request: PredictionRequest, context: PredictionContext
    ) -> PredictionResult:
        return PredictionResult(
            status=PredictionStatus.MODEL_UNAVAILABLE,
            reason=(
                "no predictive model is wired; this build maintains a state store and "
                "does not ship a learned world model"
            ),
            provider=self.name,
            state_id=context.state.state_id,
            limitations=(
                "a prediction needs a provider this installation does not have",
                "installing one is a separate decision; nothing is downloaded or loaded here",
            ),
            request=request,
        )


class RuleProjectionProvider:
    """A deterministic projection of one measured movement, labelled as such.

    It reads two versions of the SAME entity, measures how far it moved and how
    long that took, and extends the motion over the requested horizon. Everything
    it reports is arithmetic over recorded facts:

    * no motion recorded          -> ``INSUFFICIENT_EVIDENCE``
    * the two times cannot be ordered -> ``INSUFFICIENT_EVIDENCE`` (no invented dt)
    * a horizon beyond the ceiling -> the horizon is clamped and that is a limitation

    ``rule_based`` is always True and ``confidence`` is always ``None``: nothing
    measured the projection's own quality, and inventing a number for it would be
    the fabrication this whole module is arranged to prevent.
    """

    name = "rule-projection"
    available = True
    needs_model = False

    def __init__(self, *, max_horizon_seconds: float = DEFAULT_MAX_HORIZON_SECONDS) -> None:
        self.max_horizon_seconds = max(0.0, float(max_horizon_seconds))

    def predict(
        self, request: PredictionRequest, context: PredictionContext
    ) -> PredictionResult:
        state = context.state
        entity = _resolve(state, request.target)
        if entity is None:
            return PredictionResult(
                status=PredictionStatus.INSUFFICIENT_EVIDENCE,
                reason=(
                    "no entity in the current state matches "
                    f"{request.target!r}, so there is nothing to project"
                ),
                provider=self.name,
                rule_based=True,
                state_id=state.state_id,
                limitations=("a projection is about a known entity",),
                request=request,
            )
        previous_state = _previous_snapshot(state, context.snapshots)
        if previous_state is None:
            return PredictionResult(
                status=PredictionStatus.INSUFFICIENT_EVIDENCE,
                reason=(
                    "only one state version is retained, so no movement can be measured "
                    "to project"
                ),
                provider=self.name,
                rule_based=True,
                state_id=state.state_id,
                evidence=(state.state_id,),
                limitations=("a projection needs two observations of the same entity",),
                request=request,
            )
        before = previous_state.entity(entity.entity_id)
        distance = _displacement(before, entity)
        if before is None or distance is None:
            return PredictionResult(
                status=PredictionStatus.INSUFFICIENT_EVIDENCE,
                reason=(
                    f"{entity.label or entity.entity_id} has no measured position in both "
                    "retained versions, so its movement cannot be measured"
                ),
                provider=self.name,
                rule_based=True,
                state_id=state.state_id,
                evidence=(previous_state.state_id, state.state_id),
                limitations=("a projection needs a position in two observations",),
                request=request,
            )
        elapsed = seconds_between(previous_state.timestamp, state.timestamp)
        if elapsed is None or elapsed <= 0:
            return PredictionResult(
                status=PredictionStatus.INSUFFICIENT_EVIDENCE,
                reason=(
                    "no rate can be measured from these two versions: their times are "
                    "either unreadable or not in order (an out-of-order observation made "
                    "the newer version's timestamp earlier)"
                ),
                provider=self.name,
                rule_based=True,
                state_id=state.state_id,
                evidence=(previous_state.state_id, state.state_id),
                limitations=("a rate needs two comparable timestamps",),
                request=request,
            )
        horizon = request.horizon_seconds
        if horizon is None:
            horizon = elapsed
            horizon_note = (
                "no horizon was given, so one step of the measured interval was used"
            )
        else:
            horizon_note = ""
        limitations = [
            "a deterministic projection of one measured displacement; no predictive "
            "model was involved",
            "linear motion is assumed",
            "the projection is not a fact about the future",
        ]
        if horizon > self.max_horizon_seconds:
            horizon = self.max_horizon_seconds
            limitations.append(
                f"the horizon was clamped to {self.max_horizon_seconds:.0f}s, the furthest "
                "this rule projects"
            )
        if horizon_note:
            limitations.append(horizon_note)
        moved = _centre_delta(before, entity)
        if moved is None:  # pragma: no cover - distance implies the delta exists
            return PredictionResult(
                status=PredictionStatus.INSUFFICIENT_EVIDENCE,
                reason="the movement could not be reduced to a velocity",
                provider=self.name,
                rule_based=True,
                state_id=state.state_id,
                limitations=tuple(limitations),
                request=request,
            )
        dx, dy = moved
        velocity_x = dx / elapsed
        velocity_y = dy / elapsed
        centre = entity.bbox.center if entity.bbox is not None else (0.0, 0.0)
        projected = (
            centre[0] + velocity_x * horizon,
            centre[1] + velocity_y * horizon,
        )
        return PredictionResult(
            status=PredictionStatus.PREDICTED,
            reason=(
                f"projected {entity.label or entity.entity_id} forward {horizon:.1f}s from the "
                f"displacement measured over the last {elapsed:.1f}s"
            ),
            provider=self.name,
            predicted={
                "entity_id": entity.entity_id,
                "label": entity.label,
                "basis": "rule_projection",
                "measured_displacement_px": {"dx": round(dx, 2), "dy": round(dy, 2)},
                "measured_seconds": round(elapsed, 3),
                "velocity_px_per_second": {
                    "dx": round(velocity_x, 3),
                    "dy": round(velocity_y, 3),
                },
                "horizon_seconds": round(horizon, 3),
                "projected_centre": {
                    "x": round(projected[0], 1),
                    "y": round(projected[1], 1),
                },
            },
            confidence=None,
            state_id=state.state_id,
            evidence=(previous_state.state_id, state.state_id),
            limitations=tuple(limitations),
            rule_based=True,
            request=request,
        )


class PredictionService:
    """Asks one provider, respects the gate, and reports every ending honestly.

    Every outcome the phase asks for is reachable from here and each has its own
    status: no provider (``MODEL_UNAVAILABLE``), nothing to project from
    (``INSUFFICIENT_EVIDENCE``), a refused model load (``RESOURCE_BLOCKED``), a
    provider that raised or misbehaved (``FAILED``).
    """

    def __init__(
        self,
        *,
        provider: PredictionProvider | None = None,
        gate: ResourceGateLike | None = None,
        timeout_ms: int | None = None,
        max_horizon_seconds: float = DEFAULT_MAX_HORIZON_SECONDS,
    ) -> None:
        self.provider: PredictionProvider = provider or NoPredictionProvider()
        self.gate = gate
        self.timeout_ms = max(1, int(timeout_ms or DEFAULT_TIMEOUT_MS))
        self.max_horizon_seconds = max(0.0, float(max_horizon_seconds))

    def set_provider(self, provider: PredictionProvider | None) -> None:
        """Install a provider (or remove one). Nothing is loaded by doing this."""
        self.provider = provider or NoPredictionProvider()

    def availability(self) -> dict[str, Any]:
        """What this installation can predict with, and why — for a status surface."""
        provider = self.provider
        available = bool(getattr(provider, "available", False))
        needs_model = bool(getattr(provider, "needs_model", False))
        return {
            "provider": getattr(provider, "name", "none"),
            "available": available,
            "requires_model": needs_model,
            "rule_based": bool(getattr(provider, "rule_based_safe", False))
            or getattr(provider, "name", "") == RuleProjectionProvider.name,
            "timeout_ms": self.timeout_ms,
            "max_horizon_seconds": self.max_horizon_seconds,
            "gate_wired": self.gate is not None,
            "reason": (
                ""
                if available
                else (
                    "no predictive provider is wired; this build ships a state store, not "
                    "a learned world model"
                )
            ),
        }

    def predict(
        self,
        request: PredictionRequest,
        *,
        state: WorldState,
        transitions: Sequence[WorldStateTransition] = (),
        snapshots: Sequence[WorldState] = (),
    ) -> PredictionResult:
        """One prediction attempt, reported as exactly what happened."""
        import time

        context = PredictionContext(
            state=state, transitions=tuple(transitions), snapshots=tuple(snapshots)
        )
        provider = self.provider
        if not bool(getattr(provider, "available", False)):
            return PredictionResult(
                status=PredictionStatus.MODEL_UNAVAILABLE,
                reason=(
                    f"the prediction provider {getattr(provider, 'name', 'none')!r} is not "
                    "available in this installation"
                ),
                provider=getattr(provider, "name", "none"),
                state_id=state.state_id,
                limitations=("an unavailable provider is asked and answers nothing",),
                request=request,
            )
        needs_model = bool(getattr(provider, "needs_model", False))
        if needs_model:
            refusal = self._ask_gate(request)
            if refusal is not None:
                return PredictionResult(
                    status=PredictionStatus.RESOURCE_BLOCKED,
                    reason=refusal,
                    provider=getattr(provider, "name", "none"),
                    state_id=state.state_id,
                    limitations=(
                        "resource admission refused the model, so nothing was loaded or run",
                        "state queries that need no model are unaffected",
                    ),
                    request=request,
                )
        started = time.perf_counter()
        try:
            produced = provider.predict(request, context)
        except Exception as exc:  # noqa: BLE001 - a provider failure is a result, not a crash
            return PredictionResult(
                status=PredictionStatus.FAILED,
                reason=f"the provider raised {type(exc).__name__}: {exc}",
                provider=getattr(provider, "name", "none"),
                state_id=state.state_id,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
                limitations=("an optional provider's failure cannot fail a state query",),
                request=request,
            )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        if not isinstance(produced, PredictionResult):
            return PredictionResult(
                status=PredictionStatus.FAILED,
                reason=(
                    "the provider returned "
                    f"{type(produced).__name__} instead of a PredictionResult; the output "
                    "was discarded rather than interpreted"
                ),
                provider=getattr(provider, "name", "none"),
                state_id=state.state_id,
                elapsed_ms=elapsed_ms,
                limitations=("an unreadable provider answer is not a prediction",),
                request=request,
            )
        if produced.status is PredictionStatus.PREDICTED and not produced.predicted:
            # A "prediction" with no content is a failure to predict that dressed
            # itself as success. Refusing it here is the last line of defence.
            return PredictionResult(
                status=PredictionStatus.FAILED,
                reason="the provider reported a prediction but returned no predicted content",
                provider=produced.provider or getattr(provider, "name", "none"),
                state_id=produced.state_id or state.state_id,
                elapsed_ms=elapsed_ms,
                limitations=("a prediction must carry what was predicted",),
                request=request,
            )
        if elapsed_ms > self.timeout_ms:
            return PredictionResult(
                status=PredictionStatus.FAILED,
                reason=(
                    f"the provider took {elapsed_ms:.0f}ms, over its {self.timeout_ms}ms "
                    "budget"
                ),
                provider=produced.provider or getattr(provider, "name", "none"),
                state_id=produced.state_id or state.state_id,
                elapsed_ms=elapsed_ms,
                limitations=("the budget was exceeded and the answer is reported as a failure",),
                request=request,
            )
        limitations = list(produced.limitations)
        if produced.rule_based and not any("model" in item for item in limitations):
            limitations.append(
                "this was a rule-based projection, not a learned prediction"
            )
        return PredictionResult(
            status=produced.status,
            reason=produced.reason,
            provider=produced.provider or getattr(provider, "name", "none"),
            predicted=dict(produced.predicted),
            confidence=produced.confidence,
            state_id=produced.state_id or state.state_id,
            evidence=produced.evidence,
            limitations=tuple(limitations),
            rule_based=produced.rule_based,
            elapsed_ms=elapsed_ms,
            request=request,
        )

    def _ask_gate(self, request: PredictionRequest) -> str | None:
        """Whether a model-backed prediction may proceed; a refusal, or ``None``.

        No gate wired is a REFUSAL, for the same reason the perception gate refuses:
        a build with no governance has not agreed to spend the memory, and "nothing
        to ask" must not silently become "yes".
        """
        if self.gate is None:
            return (
                "no resource gate is wired, so a model-backed prediction cannot be "
                "authorized"
            )
        budget = as_optional_int(request.resource_budget.get("max_latency_ms"))
        required = as_optional_int(request.resource_budget.get("required_bytes"))
        try:
            decision = self.gate.admit(
                needs_model=True,
                required_bytes=required,
                model=getattr(self.provider, "name", ""),
                latency_budget_ms=budget,
            )
        except Exception as exc:  # noqa: BLE001 - a broken gate is a refusal
            return f"the resource gate could not be asked ({type(exc).__name__}); refused"
        allowed = getattr(decision, "allowed", None)
        reason = str(getattr(decision, "reason", "") or "")
        if allowed is True:
            return None
        return reason or "the resource gate did not allow the model-backed prediction"


def _resolve(state: WorldState, target: str) -> WorldEntity | None:
    """An entity by id, then by label — the same resolution the queries use."""
    text = str(target or "").strip()
    if not text:
        return None
    direct = state.entity(text)
    if direct is not None:
        return direct
    matches = [
        item for item in state.find(text) if item.status is not EntityStatus.EXPIRED
    ]
    return sorted(matches, key=lambda item: item.entity_id)[0] if matches else None


def _previous_snapshot(
    state: WorldState, snapshots: Sequence[WorldState]
) -> WorldState | None:
    """The version immediately before this one, or ``None`` when nothing precedes it."""
    for item in reversed(snapshots):
        if item.state_id == state.previous_state_id and item.state_id != state.state_id:
            return item
    candidates = [
        item
        for item in snapshots
        if item.version < state.version and item.state_id != state.state_id
    ]
    if not candidates and state.previous_state_id:
        return None
    return max(candidates, key=lambda item: item.version) if candidates else None


def _displacement(before: WorldEntity | None, after: WorldEntity) -> float | None:
    """How far an entity moved between two versions, or ``None`` when unmeasurable."""
    if before is None or before.bbox is None or after.bbox is None:
        return None
    if not before.bbox.has_extent or not after.bbox.has_extent:
        return None
    delta = _centre_delta(before, after)
    if delta is None:  # pragma: no cover - bbox presence implies the delta
        return None
    return math.hypot(delta[0], delta[1])


def _centre_delta(before: WorldEntity | None, after: WorldEntity) -> tuple[float, float] | None:
    """The centre-to-centre delta between two versions of an entity."""
    if before is None or before.bbox is None or after.bbox is None:
        return None
    if not before.bbox.has_extent or not after.bbox.has_extent:
        return None
    first = before.bbox.center
    second = after.bbox.center
    return (second[0] - first[0], second[1] - first[1])


@dataclass(frozen=True, slots=True)
class PredictionProviders:
    """The named providers this build knows, for a caller choosing one."""

    names: tuple[str, ...] = field(default=("none", RuleProjectionProvider.name))

    def to_dict(self) -> dict[str, Any]:
        return {
            "providers": list(self.names),
            "default": "none",
            "rule_based": [RuleProjectionProvider.name],
        }
