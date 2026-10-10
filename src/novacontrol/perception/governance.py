"""Resource governance: how expensive perception is allowed to be, decided first.

The perception layer has one genuinely expensive move — asking a vision model —
and several cheap ones. This module is the gate in front of the expensive one,
built on the machinery Phase 14 already established rather than beside it:

    the SAME ``ResourceGovernor``          an admission advice for a model load
    the SAME ``HardwareMonitor``           the figures behind that advice
    the SAME ``ModelManager``              whether a model is already resident

Three behaviours are deliberate.

**Nothing here loads a model.** ``admit`` answers a question; loading is the
model manager's job, on the model manager's terms. Perception asks, and degrades
when the answer is no.

**A "cannot tell" answer means no.** The governor's advice is three-valued, and
the honest reading for a MODEL LOAD is the conservative one: an unmeasured
machine does not get a model it might not fit. The fast path still runs, so the
request degrades rather than fails.

**No governor wired is not permission.** With nothing to ask, the gate does not
default to yes — it refuses the model-backed path with that reason. A build with
no governance is a build that has not agreed to spend the RAM.

The profiles are the other half: a machine under pressure lowers the sampling
rate and the preview size, which reduces the work every later stage does.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from novacontrol.perception.preprocessing import PreprocessSpec

__all__ = [
    "AdmissionDecision",
    "PerceptionProfile",
    "PerceptionResourceGate",
    "preprocess_for",
    "sampling_for",
]

#: The coarse performance targets. Named after what they cost, not after what
#: they are for, because the choice is always about the machine.
_PROFILE_TARGETS: Mapping[str, dict[str, float | int]] = {
    "low_resource": {
        "target_fps": 0.5,
        "max_fps": 1.0,
        "min_interval_ms": 400,
        "preview_max_side": 128,
        "max_objects": 24,
    },
    "balanced": {
        "target_fps": 2.0,
        "max_fps": 5.0,
        "min_interval_ms": 120,
        "preview_max_side": 192,
        "max_objects": 48,
    },
    "performance": {
        "target_fps": 6.0,
        "max_fps": 12.0,
        "min_interval_ms": 60,
        "preview_max_side": 320,
        "max_objects": 96,
    },
}


class PerceptionProfile:
    """The profile names as a small value type — strings, validated on read."""

    LOW_RESOURCE = "low_resource"
    BALANCED = "balanced"
    PERFORMANCE = "performance"
    ALL = (LOW_RESOURCE, BALANCED, PERFORMANCE)

    @staticmethod
    def resolve(value: Any, default: str = BALANCED) -> str:
        """A profile name that is one of the three, or the default."""
        text = str(value or "").strip().lower()
        return text if text in PerceptionProfile.ALL else default


def sampling_for(profile: str) -> Any:
    """A sampling policy for a profile — imported lazily to avoid a cycle.

    ``sampling`` imports ``preprocessing``, which this module also imports, so
    the policy is built on demand rather than at module import time.
    """
    from novacontrol.perception.sampling import SamplingPolicy

    resolved = PerceptionProfile.resolve(profile)
    targets = _PROFILE_TARGETS[resolved]
    return SamplingPolicy(
        target_fps=float(targets["target_fps"]),
        max_fps=float(targets["max_fps"]),
        min_interval_ms=int(targets["min_interval_ms"]),
    )


def preprocess_for(profile: str) -> PreprocessSpec:
    """The preprocessing budget for a profile."""
    resolved = PerceptionProfile.resolve(profile)
    return PreprocessSpec(max_side=int(_PROFILE_TARGETS[resolved]["preview_max_side"]))


def max_objects_for(profile: str) -> int:
    """How many objects a scene may carry under this profile."""
    resolved = PerceptionProfile.resolve(profile)
    return int(_PROFILE_TARGETS[resolved]["max_objects"])


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """Whether the expensive path may run, and what to do instead when it may not."""

    allowed: bool | None = None
    reason: str = ""
    profile: str = PerceptionProfile.BALANCED
    degrade_to: str = ""
    advice: Mapping[str, Any] = field(default_factory=dict)

    @property
    def denied(self) -> bool:
        return self.allowed is False

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "profile": self.profile,
            "degrade_to": self.degrade_to,
            "advice": dict(self.advice),
        }


class PerceptionResourceGate:
    """Asks the existing governance whether a model-backed path may run.

    Every collaborator is optional and each absence has a stated consequence,
    because "nothing to ask" must not silently become "yes":

      governor absent      the model-backed path is refused for this request
      monitor absent       the report says the figure is unmeasured (``None``)
      model manager absent resident-model checks report "unknown"
    """

    def __init__(
        self,
        *,
        governor: Any | None = None,
        monitor: Any | None = None,
        model_manager: Any | None = None,
        profile: str = PerceptionProfile.BALANCED,
    ) -> None:
        self.governor = governor
        self.monitor = monitor
        self.model_manager = model_manager
        self.profile = PerceptionProfile.resolve(profile)

    def with_profile(self, profile: str) -> PerceptionResourceGate:
        """The same gate under a different budget — a degraded run, not a new one."""
        return PerceptionResourceGate(
            governor=self.governor,
            monitor=self.monitor,
            model_manager=self.model_manager,
            profile=PerceptionProfile.resolve(profile),
        )

    # ── admission ────────────────────────────────────────────────────────
    def admit(
        self,
        *,
        needs_model: bool,
        required_bytes: int | None = None,
        model: str = "",
        latency_budget_ms: int | None = None,
    ) -> AdmissionDecision:
        """Whether the expensive path may run right now.

        ``required_bytes`` is what the caller believes the model needs (from the
        model manager's own catalogue); ``None`` means nobody could measure it,
        which is passed through to the governor rather than guessed at here.
        """
        if not needs_model:
            return AdmissionDecision(
                allowed=True,
                reason="the fast path does not need a model",
                profile=self.profile,
            )
        resident = self._residency(model)
        if resident is True:
            return AdmissionDecision(
                allowed=True,
                reason=f"the model {model!r} is already resident, so nothing is loaded",
                profile=self.profile,
            )
        budget = self._latency_advice(latency_budget_ms)
        if self.governor is None:
            return AdmissionDecision(
                allowed=False,
                reason=(
                    "no resource governor is wired, so the model-backed path is not "
                    "taken: absence of a gate is not permission to spend the RAM"
                ),
                profile=self.profile,
                degrade_to=PerceptionProfile.LOW_RESOURCE,
                advice=budget,
            )
        advice = self.governor.advise_load(
            required_bytes,
            model=model,
            loaded=self._resident_models(),
            active=self._active_models(),
        )
        rows = dict(getattr(advice, "to_dict", dict)()) if hasattr(advice, "to_dict") else {}
        if advice.allow is True:
            return AdmissionDecision(
                allowed=True,
                reason=str(advice.reason or "the resource governor allows this load"),
                profile=self.profile,
                advice={**rows, **budget},
            )
        if advice.allow is False:
            return AdmissionDecision(
                allowed=False,
                reason=str(advice.reason or "the resource governor refused this load"),
                profile=self.profile,
                degrade_to=PerceptionProfile.LOW_RESOURCE,
                advice={**rows, **budget, "unload": list(getattr(advice, "unload", ()) or ())},
            )
        return AdmissionDecision(
            allowed=False,
            reason=(
                "the resource governor could not measure this machine"
                + (f": {advice.reason}" if getattr(advice, "reason", "") else "")
                + ", so no model-backed path is taken"
            ),
            profile=self.profile,
            degrade_to=PerceptionProfile.LOW_RESOURCE,
            advice={**rows, **budget},
        )

    def _latency_advice(self, latency_budget_ms: int | None) -> dict[str, Any]:
        """What the REQUEST's own deadline says — reported, and never enforced by a kill.

        A latency budget is a caller's expectation, not a resource fact: the gate
        records it so the engine can skip the expensive path when the deadline is
        already too tight to be met, and it does not abort an inference mid-flight.
        """
        if latency_budget_ms is None:
            return {}
        return {"latency_budget_ms": int(latency_budget_ms)}

    def _residency(self, model: str) -> bool | None:
        """Whether the model is already loaded — ``None`` when nothing can say."""
        manager = self.model_manager
        if manager is None or not str(model).strip():
            return None
        try:
            active = [str(name).casefold() for name in manager.active_models()]
        except Exception:  # noqa: BLE001 - a probe that fails is an unknown, not a crash
            return None
        return str(model).casefold() in active

    def _resident_models(self) -> tuple[str, ...] | None:
        """What the RUNTIME says is in memory — a decision-path question only.

        This asks the model manager's runtime, which is one HTTP round trip
        when the local runtime is up and a bounded timeout when it is not, so
        it is called from ``admit`` — the rare, model-backed path — and NEVER
        from ``status``, which travels in every ``app.status()`` poll and must
        stay probe-free (the same rule ``models_status`` follows).
        """
        manager = self.model_manager
        if manager is None:
            return None
        try:
            status = manager.runtime_status()
            # ModelRuntimeStatus calls the field ``loaded_models``; reading a
            # nonexistent ``resident`` attribute silently yielded () forever.
            names = getattr(status, "loaded_models", ()) or ()
            return tuple(str(name) for name in names)
        except Exception:  # noqa: BLE001 - residency is advisory here
            return None

    def _active_models(self) -> tuple[str, ...]:
        manager = self.model_manager
        if manager is None:
            return ()
        try:
            return tuple(str(name) for name in manager.active_models())
        except Exception:  # noqa: BLE001 - an unreadable set must not block a load
            return ()

    # ── reporting ────────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        """What this gate would decide, and the figures behind it.

        Unmeasured figures stay ``None``: a status board that shows a zero for
        "could not read free memory" is lying about the machine.

        Probe-free by contract: nothing here asks the model runtime, so this
        can ride in every ``app.status()`` poll without a network round trip.
        """
        available_ram: int | None = None
        monitor_row: dict[str, Any] = {}
        if self.monitor is not None:
            try:
                available = self.monitor.available_ram_bytes()
                total = self.monitor.total_ram_bytes()
                available_ram = int(available) if available is not None else None
                monitor_row = {
                    "available": available is not None or total is not None,
                    "available_bytes": available_ram,
                    "total_bytes": int(total) if total is not None else None,
                }
                if not monitor_row["available"]:
                    monitor_row["reason"] = "the memory probe returned no figure"
            except Exception:  # noqa: BLE001 - an unreadable probe is an unknown
                monitor_row = {"available": False, "reason": "the memory probe failed"}
        governor_row: dict[str, Any] = {}
        if self.governor is not None:
            try:
                governor_row = dict(self.governor.report())
            except Exception:  # noqa: BLE001 - reporting must never break a request
                governor_row = {"available": False}
        assessment = governor_row.get("assessment")
        loaded = assessment.get("loaded_models") if isinstance(assessment, Mapping) else None
        return {
            "profile": self.profile,
            "governor_wired": self.governor is not None,
            "model_manager_wired": self.model_manager is not None,
            "available_ram_bytes": available_ram,
            "memory": monitor_row,
            "governor": governor_row,
            # Residency exactly as the governor's own assessment measured it
            # above — never re-asked from the runtime — and None when nothing
            # measured it (no governor, or a failed report).
            "resident_models": list(loaded) if isinstance(loaded, (list, tuple)) else None,
        }

