"""One place that answers "which model, and is there room for it?".

Phase 7 exists because NovaControl must not assume one model handles everything.
The layers below already know how to *use* a model — the brain completes,
``vision/providers.py`` looks, the planner reasons — and the lifecycle layer
(``intelligence/model_manager.py``) already knows how to *move* one in and out of
memory with measured figures. What was missing was the layer that answers the
question BETWEEN those two: given what this request needs, which model should
serve it, and can this machine hold it right now?

So ``ModelManager`` is a decision layer, not a new runtime:

    need (capabilities, resources, what is already loaded, latency preference)
      -> select   which profile can serve it, or "none, fall back"
      -> load     RAM-aware, in the specification's six steps, verified after
      -> track    switch/latency/eviction counts, so batching is visible

Three rules are load-bearing.

**Selection is by capability, never by name.** See ``models/profiles.py``: a
request asks for ``{VISION}`` and whatever declares it wins. A name appears in
the declaration table and nowhere else.

**A refusal is a result.** When nothing local can serve the request the manager
says so, with the capabilities it wanted and what each candidate lacked — which
is precisely the caller the cloud fallback exists for. It never picks a runner-up
to avoid an empty answer, and it never loads a model it cannot fit.

**Switching is the cost worth avoiding.** Every load is seconds and every
unload discards the warm pages the next request wants back. The manager
therefore prefers a model that already satisfies the requirement over a marginal
upgrade, refuses to evict a model an ACTIVE task is using, and counts the
switches so the batching question can be answered with data.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from novacontrol.models.hardware import HardwareMonitor
from novacontrol.models.profiles import (
    CapabilityMatch,
    ModelCapability,
    ModelProfile,
    ModelRegistry,
    as_capabilities,
    select_profile,
)

#: The headroom a load must leave free, matching the lifecycle layer's figure so
#: one number governs rather than two that can drift apart.
DEFAULT_HEADROOM_BYTES = 512 * 1024 * 1024


@runtime_checkable
class ModelLifecycle(Protocol):
    """The one call the manager delegates to when a lifecycle layer is injected.

    Narrow on purpose: this class needs "release this model", and naming the
    whole lifecycle surface would make a replacement layer responsible for
    methods nobody calls.
    """

    def unload_model(self, model: str) -> bool:
        """Release one model's memory; ``False`` when it was not held."""


@runtime_checkable
class ModelProvider(Protocol):
    """A runtime that can hold models — the replaceable half of this layer.

    Deliberately small, and deliberately the same shape the lifecycle layer
    already speaks (``intelligence/model_manager.py``): a second runtime is a
    new implementation of these six calls, and nothing above changes. An NPU
    executor, a llama.cpp server or a cloud-side model registry all fit here.
    """

    @property
    def name(self) -> str:
        """What this runtime is called (``ollama``, ``llama.cpp``, ``npu``).

        A read-only property rather than a plain attribute so a provider that
        carries its name as a frozen field satisfies the protocol: requiring a
        SETTABLE name would force every implementation to expose one it has no
        reason to change.
        """

    def list_models(self) -> tuple[str, ...]:
        """Every model this provider offers, without loading anything."""

    def resident_models(self) -> tuple[tuple[str, int], ...] | None:
        """(name, resident bytes) for what is in memory; ``None`` = unknown."""

    def load(self, model: str) -> bool:
        """Bring a model into memory."""

    def unload(self, model: str) -> bool:
        """Release a model's memory."""

    def available_memory_bytes(self) -> int | None:
        """Free physical memory, or ``None`` when the OS will not say."""

    def model_size_bytes(self, model: str) -> int | None:
        """A model's footprint — resident when loaded, else on disk."""

    def capabilities(self, model: str) -> tuple[str, ...] | None:
        """The runtime's OWN capability words for a model, or ``None``.

        Optional on purpose: a Protocol with a required method would make every
        existing backend illegal until it grew one, and a runtime that cannot
        describe a model is a supported case, not a broken one. The manager
        probes for this with ``getattr`` and treats silence as silence.
        """


class KeepAlivePolicy(StrEnum):
    """When the manager releases a model on its own initiative.

    The specification asks for these as configurable policies and warns against
    inventing values, so each one is a RULE and every duration comes from
    configuration. ``WARM`` with no idle window (the default) is the
    RAM-conscious choice on this machine for a specific measured reason: the
    runtime already bounds its own residency, and the case that actually hurts —
    a second model arriving while a large one is resident — is handled at load
    time by the eviction the measurement demands. ``IMMEDIATE`` exists for a
    deployment tighter than this one, and it is a setting rather than a guess.
    """

    #: Release as soon as the operation that needed the model returns.
    IMMEDIATE = "immediate"
    #: Keep it resident, and release it once it has been idle for the window.
    WARM = "warm"
    #: Release it only when no task that selected it is still running.
    WHILE_ACTIVE = "while_active"
    #: Never unload on the manager's initiative (only to make room).
    NEVER = "never"


@dataclass(frozen=True, slots=True)
class KeepAliveSettings:
    """The configured policy, with no invented durations."""

    policy: KeepAlivePolicy = KeepAlivePolicy.WARM
    #: The policy string as it was configured, kept so a deployment can see
    #: whether what it asked for is what it got. An unrecognised policy keeps
    #: the default — a typo must not fail boot — and this is how that stays
    #: VISIBLE instead of silently becoming "warm".
    requested: str = ""
    #: Idle seconds before a ``WARM`` model is released. 0 = leave the runtime's
    #: own residency alone, which is its documented default rather than a
    #: number this project made up.
    idle_seconds: int = 0
    #: How long a single operation is expected to hold its model, used only to
    #: decide whether a warm model is worth keeping for a queued request.
    active_seconds: int = 0

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> KeepAliveSettings:
        data = data or {}
        raw = str(data.get("policy", "") or "").strip().lower().replace("-", "_")
        policy = next(
            (candidate for candidate in KeepAlivePolicy if candidate.value == raw),
            cls().policy,
        )
        return cls(
            policy=policy,
            requested=raw,
            idle_seconds=_seconds(data.get("idle_seconds")),
            active_seconds=_seconds(data.get("active_seconds")),
        )

    @property
    def honoured(self) -> bool:
        """Whether the configured policy is the one in force (a typo is False)."""
        return not self.requested or self.requested == self.policy.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy.value,
            "requested": self.requested,
            "honoured": self.honoured,
            "idle_seconds": self.idle_seconds,
            "active_seconds": self.active_seconds,
        }


@dataclass(frozen=True, slots=True)
class ModelRuntimeStatus:
    """What the runtime holds right now, with the profile of what is active.

    The specification's ``ModelRuntimeStatus``: the provider, the active model
    AND what it can do, every resident model, the memory situation, and whether
    the provider answered at all. ``reachable=False`` is a first-class outcome —
    a runtime that is not running is not an error, it is the reason the local
    route declined.
    """

    provider: str
    active_model: str = ""
    loaded_models: tuple[str, ...] = ()
    available_memory_bytes: int | None = None
    total_memory_bytes: int | None = None
    reachable: bool = False
    active_capabilities: tuple[str, ...] = ()
    keep_alive: Mapping[str, Any] = field(default_factory=dict)
    verified: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "active_model": self.active_model,
            "loaded_models": list(self.loaded_models),
            "available_memory_bytes": self.available_memory_bytes,
            "total_memory_bytes": self.total_memory_bytes,
            "reachable": self.reachable,
            "active_capabilities": list(self.active_capabilities),
            "keep_alive": dict(self.keep_alive),
            "verified": self.verified,
        }


@dataclass(frozen=True, slots=True)
class ModelSelection:
    """Which model should serve a request, and the evidence for the answer.

    ``route`` is ``local`` when a profile was found, ``cloud`` when nothing local
    could and a cloud provider is configured, and ``none`` when neither applies —
    three outcomes, because "no model" and "the cloud" are different answers and
    only one of them is a hand-off.
    """

    route: str
    model: str = ""
    provider: str = ""
    reason: str = ""
    required: tuple[str, ...] = ()
    match: CapabilityMatch | None = None

    @property
    def found(self) -> bool:
        return bool(self.model)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "model": self.model,
            "provider": self.provider,
            "reason": self.reason,
            "required": list(self.required),
            "match": self.match.to_dict() if self.match is not None else None,
        }


@dataclass(frozen=True, slots=True)
class ModelLoadOutcome:
    """The specification's six steps, with what each one found.

    ``verified`` is separate from ``loaded`` on purpose: the runtime saying it
    accepted a load and the runtime actually listing the model are two different
    facts, and only the second one means the next request will work.
    """

    model: str
    loaded: bool
    steps: tuple[Mapping[str, Any], ...] = ()
    evicted: tuple[str, ...] = ()
    refused: bool = False
    reason: str = ""
    verified: bool | None = None
    load_seconds: float = 0.0
    available_memory_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "loaded": self.loaded,
            "evicted": list(self.evicted),
            "refused": self.refused,
            "reason": self.reason,
            "verified": self.verified,
            "load_seconds": round(self.load_seconds, 3),
            "available_memory_bytes": self.available_memory_bytes,
            "steps": [dict(step) for step in self.steps],
        }


@dataclass(slots=True)
class ModelTelemetry:
    """Switching, loading and eviction counts — the numbers batching is judged by.

    Kept here rather than in ``intelligence/telemetry.py`` because these are the
    manager's own operations; the intelligence telemetry aggregates the request
    view and reads these figures in, so there is one source per fact.
    """

    loads: int = 0
    load_skips: int = 0
    refusals: int = 0
    unloads: int = 0
    evictions: int = 0
    verified_failures: int = 0
    load_seconds: list[float] = field(default_factory=list)
    unload_seconds: list[float] = field(default_factory=list)
    selections: dict[str, int] = field(default_factory=dict)

    def record_load(self, seconds: float, *, skipped: bool = False) -> None:
        if skipped:
            self.load_skips += 1
            return
        self.loads += 1
        self.load_seconds.append(max(0.0, float(seconds)))

    def record_unload(self, seconds: float) -> None:
        self.unloads += 1
        self.unload_seconds.append(max(0.0, float(seconds)))

    def record_selection(self, route: str) -> None:
        key = str(route or "none")
        self.selections[key] = self.selections.get(key, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "loads": self.loads,
            "load_skips": self.load_skips,
            "refusals": self.refusals,
            "unloads": self.unloads,
            "evictions": self.evictions,
            "verified_failures": self.verified_failures,
            "load_seconds": _latency(self.load_seconds),
            "unload_seconds": _latency(self.unload_seconds),
            "selections": dict(self.selections),
        }


class ModelManager:
    """Capability matching, resource awareness and lifecycle, in one place.

    The provider is injected (so a fake drives every branch in tests) and the
    monitor is injected (so a machine without a GPU is a supported case, not an
    untested one). ``lifecycle`` is the existing lifecycle manager when the
    application supplies it; without one this class speaks to the provider
    directly, which is what keeps it usable from a test or a CLI without an
    application around it.
    """

    def __init__(
        self,
        provider: ModelProvider | None = None,
        *,
        registry: ModelRegistry | None = None,
        monitor: HardwareMonitor | None = None,
        lifecycle: ModelLifecycle | None = None,
        keep_alive: KeepAliveSettings | None = None,
        headroom_bytes: int = DEFAULT_HEADROOM_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self.registry = registry if registry is not None else ModelRegistry()
        self.keep_alive = keep_alive if keep_alive is not None else KeepAliveSettings()
        self.headroom_bytes = max(0, int(headroom_bytes))
        self.telemetry = ModelTelemetry()
        self._clock = clock
        # The lifecycle manager is duck-typed on purpose: this class needs
        # "load/unload/status", and requiring the concrete type would make the
        # runtime impossible to fake in a test.
        self._lifecycle = lifecycle
        self.monitor = monitor if monitor is not None else HardwareMonitor(
            resident_models=self._resident_names,
            headroom_bytes=self.headroom_bytes,
        )
        #: Models a task is actively using. A model in here is NEVER evicted to
        #: make room — the task would fail mid-flight, which is worse than the
        #: request waiting for the memory.
        self._active: dict[str, int] = {}
        self._idle_since: dict[str, float] = {}
        #: Capability words the RUNTIME reported, per model, and the set already
        #: probed — a probe is an HTTP round trip, so each model is asked once
        #: per process rather than once per selection.
        self._reported: dict[str, frozenset[str]] = {}
        self._probed: set[str] = set()

    # ── discovery and health ────────────────────────────────────────────
    def provider_name(self) -> str:
        return str(getattr(self._provider, "name", "") or "none")

    def _resident(self) -> tuple[tuple[str, int], ...] | None:
        if self._provider is None:
            return None
        try:
            return self._provider.resident_models()
        except Exception:  # pragma: no cover - a probe must never throw
            return None

    def _resident_names(self) -> tuple[str, ...]:
        resident = self._resident()
        return tuple(name for name, _size in resident) if resident else ()

    def discover(self) -> tuple[ModelProfile, ...]:
        """Every model the provider offers, merged into the registry.

        The runtime is the authority on what EXISTS; the registry is the
        authority on what it CAN do. A model the runtime lists but nobody
        declared gets an inferred profile, marked as such. Capability words the
        runtime has already reported (see :meth:`refresh_capabilities`) are
        applied on the way through, so a later discovery does not lose what an
        earlier measurement established.
        """
        if self._provider is None:
            return ()
        try:
            names = self._provider.list_models()
        except Exception:  # pragma: no cover - defensive
            return ()
        return self.registry.discover(
            names, provider=self.provider_name(), reported=self._reported
        )

    def refresh_capabilities(self, *, budget_seconds: float = 1.5) -> Mapping[str, frozenset[str]]:
        """Ask the runtime what each available model can do, ONCE per model.

        This is what keeps the capability table from being a hard-coded guess: a
        runtime that reports ``vision`` for a build nobody declared makes that
        build selectable for a screenshot, and one that omits it for a build
        whose NAME suggests vision corrects the guess. A runtime that cannot
        answer (or a model it says nothing about) leaves the declaration in
        place, still marked as a declaration.

        Repeated calls are cheap and a model is never asked twice: the first
        answer is kept, including "no answer", so a status read on the request
        path cannot become one HTTP round trip per selection.

        Bounded by ``budget_seconds`` because this runs on the boot path and a
        runtime that ACCEPTS a connection without answering would otherwise turn
        "start NovaControl" into one timeout per installed model. Whatever the
        budget did not reach keeps its declaration and is asked again next time,
        which is why a model is only marked probed once it has answered.
        """
        probe = getattr(self._provider, "capabilities", None)
        if self._provider is None or probe is None:
            return dict(self._reported)
        try:
            installed = self._provider.list_models()
        except Exception:  # pragma: no cover - defensive
            installed = ()
        # The models the runtime actually offers, when it says; otherwise the
        # declared table. Probing a model that is not installed wastes a round
        # trip on a certain miss.
        names = [str(name) for name in installed] or [
            profile.name for profile in self.registry.all()
        ]
        deadline = self._clock() + max(0.0, float(budget_seconds))
        for name in names:
            key = _key(name)
            if not key or key in self._probed:
                continue
            if self._clock() > deadline:
                break
            try:
                reported = probe(name)
            except Exception:  # pragma: no cover - a probe must never throw
                reported = None
            self._probed.add(key)
            words = tuple(str(word) for word in reported or ())
            if words:
                self._reported[key] = frozenset(words)
        # Re-merge so the registry reflects what was just learned.
        self.discover()
        return dict(self._reported)

    def runtime_status(self) -> ModelRuntimeStatus:
        """The provider's state right now, with the active model's profile."""
        resident = self._resident()
        available = None
        if self._provider is not None:
            try:
                available = self._provider.available_memory_bytes()
            except Exception:  # pragma: no cover - defensive
                available = None
        active = resident[0][0] if resident else ""
        profile = self.registry.get(active) if active else None
        return ModelRuntimeStatus(
            provider=self.provider_name(),
            active_model=active,
            loaded_models=tuple(name for name, _size in resident) if resident else (),
            available_memory_bytes=available,
            total_memory_bytes=self.monitor.total_ram_bytes(),
            reachable=resident is not None,
            active_capabilities=(
                tuple(sorted(capability.value for capability in profile.capabilities))
                if profile is not None
                else ()
            ),
            keep_alive=self.keep_alive.to_dict(),
        )

    def health(self) -> dict[str, Any]:
        """One block for a status surface: runtime, hardware, policy, telemetry."""
        status = self.runtime_status()
        return {
            "runtime": status.to_dict(),
            "hardware": self.monitor.headroom_report(),
            "keep_alive": self.keep_alive.to_dict(),
            "telemetry": self.telemetry.to_dict(),
            "registry": self.registry_report(),
            "active_tasks": dict(self._active),
        }

    def registry_report(self) -> dict[str, Any]:
        """The capability table as a status surface can carry it.

        Bounded by construction (one row per known model), and every row says
        where its capabilities came from: ``declared``, an ``inferred_from``
        guess, and — when there is one — the runtime's own words. A row that is
        only a guess looks different from one a runtime confirmed, which is the
        entire reason for carrying the provenance this far.
        """
        known = self.registry.all()
        return {
            "known": len(known),
            "declared": len(self.registry.declared()),
            "runtime_confirmed": sum(1 for profile in known if profile.runtime_confirmed),
            "models": [profile.to_dict() for profile in known],
        }

    # ── selection ───────────────────────────────────────────────────────
    def select(
        self,
        required: Iterable[ModelCapability | str],
        *,
        cloud_available: bool = False,
        cloud_model: str = "",
        latency_preference: str = "balanced",
        candidates: Sequence[str] | None = None,
        preferred: str = "",
    ) -> ModelSelection:
        """The model that should serve a request needing ``required``.

        The inputs the specification names are all here or in the caller's hands:
        the required capabilities, the resources (measured by the monitor), the
        currently loaded model (preferred when it satisfies the requirement), and
        the latency preference. Intent, decision and complexity reach this as
        *capabilities* — which is the point: those layers decide what work the
        request is, and capabilities are how that work is asked for.
        """
        wanted = as_capabilities(required)
        available_names = candidates if candidates is not None else None
        pool = self._pool(available_names)
        resident = self.runtime_status().active_model
        match = select_profile(
            wanted,
            pool,
            prefer_loaded=resident,
            preferred=preferred,
            available_memory_bytes=self.monitor.available_ram_bytes(),
            headroom_bytes=self.headroom_bytes,
            latency_preference=latency_preference,
        )
        if match.found and match.profile is not None:
            self.telemetry.record_selection("local")
            return ModelSelection(
                route="local",
                model=match.profile.name,
                provider=match.profile.provider or self.provider_name(),
                reason=match.reason,
                required=tuple(sorted(capability.value for capability in match.required)),
                match=match,
            )
        if cloud_available:
            self.telemetry.record_selection("cloud")
            return ModelSelection(
                route="cloud",
                model=cloud_model,
                provider="cloud",
                reason=f"{match.reason}; a cloud provider is configured",
                required=tuple(sorted(capability.value for capability in match.required)),
                match=match,
            )
        self.telemetry.record_selection("none")
        return ModelSelection(
            route="none",
            reason=match.reason,
            required=tuple(sorted(capability.value for capability in match.required)),
            match=match,
        )

    def select_for_request(
        self,
        *,
        requires_vision: bool = False,
        # The flags are booleans rather than capability names on purpose: a
        # caller that can spell a capability can call ``select`` directly.
        needs_reasoning: bool = False,
        needs_coding: bool = False,
        needs_tools: bool = False,
        cloud_available: bool = False,
        cloud_model: str = "",
        latency_preference: str = "balanced",
        preferred: str = "",
    ) -> ModelSelection:
        """``select`` for the flags the request layers actually produce.

        A thin, named translation rather than a second policy: the understanding
        and decision layers report *what the request needs* (vision, reasoning,
        coding, tools), and the capability set those map to lives here so the
        mapping cannot drift into two versions.
        """
        required: set[ModelCapability] = set()
        if requires_vision:
            required.add(ModelCapability.VISION)
        if needs_reasoning:
            required.add(ModelCapability.REASONING)
        if needs_coding:
            required.add(ModelCapability.CODING)
        if needs_tools:
            required.add(ModelCapability.TOOL_CALLING)
        return self.select(
            required,
            cloud_available=cloud_available,
            cloud_model=cloud_model,
            latency_preference=latency_preference,
            preferred=preferred,
        )

    def _pool(self, names: Sequence[str] | None) -> tuple[ModelProfile, ...]:
        """The profiles a selection may choose from.

        Discovery first when a provider can answer, so a model that is installed
        but undeclared is still selectable. Without a provider (or with one that
        says nothing) the registry's own declarations are the pool — which is how
        a test selects against a declared capability rather than an install.
        """
        if names is not None:
            return self.registry.discover(names, provider=self.provider_name())
        discovered = self.discover()
        return discovered or self.registry.all()

    # ── activities: what must NOT be evicted ─────────────────────────────
    def begin_activity(self, model: str) -> None:
        """Declare that a task is using ``model`` and must not lose it."""
        key = _key(model)
        if not key:
            return
        self._active[key] = self._active.get(key, 0) + 1
        self._idle_since.pop(key, None)

    def end_activity(self, model: str) -> None:
        """Done with ``model`` — it becomes eligible for collection."""
        key = _key(model)
        if key in self._active:
            self._active[key] = self._active[key] - 1
            if self._active[key] <= 0:
                del self._active[key]
        self._idle_since[key] = self._clock()

    def active_models(self) -> tuple[str, ...]:
        return tuple(sorted(self._active))

    # ── loading: the specification's six steps ───────────────────────────
    def load(self, model: str, *, force: bool = False) -> ModelLoadOutcome:
        """Load ``model``, in order: measure, inspect, estimate, free, load, verify.

        Every step records what it saw, so a refusal can be explained rather than
        apologised for. ``force`` skips the fit check — and only the fit check:
        a model that does not fit still gets the room made for it, because the
        alternative is paging, not magic.
        """
        steps: list[Mapping[str, Any]] = []
        name = str(model or "").strip()
        if not name:
            return ModelLoadOutcome(
                model="", loaded=False, refused=True, reason="no model was named"
            )
        profile = self.registry.profile_for(name, provider=self.provider_name())

        # 0. Apply the configured keep-alive policy. A model whose active task
        # has ended and whose idle window has passed is released HERE, before the
        # estimate, so the room it frees is counted rather than re-derived — and
        # so "keep warm for N minutes" is a policy that actually runs rather than
        # one that is only reported. A no-op under the default configuration
        # (:meth:`enforce_keep_alive` returns immediately when no window is set),
        # so an unconfigured install pays nothing for it.
        self.enforce_keep_alive()

        # 1. Available memory.
        available = self.monitor.available_ram_bytes()
        total = self.monitor.total_ram_bytes()
        steps.append({"step": "check_memory", "available_bytes": available, "total_bytes": total})

        # 2. Currently loaded models.
        resident = self._resident()
        loaded_now = tuple(n for n, _s in resident) if resident else ()
        steps.append({"step": "check_resident", "models": list(loaded_now)})

        # 3. Estimate what the model needs, and decide what must go to make room.
        #
        # The fit is judged TWICE, and the difference is the whole of step 4.
        # ``fits`` asks whether the model can live alongside what is resident —
        # true means nothing has to be unloaded at all, which is the cheapest
        # answer and the one that keeps a warm model warm. ``fits_after_evict``
        # asks whether it can live there once the models that MAY be unloaded are
        # gone, which is the case the specification's own example describes
        # (Qwen3 resident, a vision model needed, room made for it). Refusing on
        # the FIRST figure alone would be a bug with a tidy explanation: the
        # manager would decline to switch on the very machine it exists for.
        measured = None
        if self._provider is not None:
            try:
                measured = self._provider.model_size_bytes(name)
            except Exception:  # pragma: no cover - defensive
                measured = None
        needed = self.monitor.estimate_bytes(
            memory_bytes=profile.memory_bytes or measured,
            parameters_b=profile.parameters_b,
        )
        fits = self.monitor.fits(needed)
        sizes = {_key(resident_name): size for resident_name, size in resident or ()}
        # A model an active task is using is never a candidate, so it is not
        # counted as room either: a refusal has to be a refusal even in the
        # arithmetic.
        protected = set(self._active)
        candidates = [
            other
            for other in loaded_now
            if _key(other) != _key(name) and _key(other) not in protected
        ]
        blocked = [
            other
            for other in loaded_now
            if _key(other) != _key(name) and _key(other) in protected
        ]
        # Unknown sizes free nothing. A report of 0 means the runtime did not say
        # how big the resident model is, and counting it as reclaimed memory is
        # how a load ends up paging.
        freed = sum(sizes.get(_key(other), 0) for other in candidates)
        room_after = None if available is None else available + freed
        fits_after = (
            None
            if needed is None or room_after is None
            else room_after >= needed + self.headroom_bytes
        )
        steps.append(
            {
                "step": "estimate",
                "needed_bytes": needed,
                "fits": fits,
                "fits_after_evict": fits_after,
                "freed_by_eviction_bytes": freed,
                # Where the figure came from: a measured footprint, or the
                # parameter count scaled. An estimate that hides that it is one
                # is how a guess starts looking like a measurement.
                "source": "measured" if measured else "parameters",
            }
        )

        if any(_key(n) == _key(name) for n in loaded_now):
            # Already resident: the cheapest possible answer, and the reason the
            # selector prefers a loaded model in the first place.
            self.telemetry.record_load(0.0, skipped=True)
            steps.append({"step": "already_loaded", "model": name})
            return ModelLoadOutcome(
                model=name,
                loaded=True,
                steps=tuple(steps),
                verified=True,
                reason="the model was already resident",
                available_memory_bytes=available,
            )

        if fits is False and fits_after is False and blocked and not force:
            # A model is in use, and without it there is not enough room. Naming
            # it is the actionable version of this refusal.
            self.telemetry.refusals += 1
            steps.append(
                {"step": "refuse", "reason": "resident model is in use", "models": blocked}
            )
            return ModelLoadOutcome(
                model=name,
                loaded=False,
                refused=True,
                steps=tuple(steps),
                reason=(
                    "a task is using "
                    + ", ".join(blocked)
                    + " and unloading it would break that task"
                ),
                available_memory_bytes=available,
            )
        if fits_after is False and not force:
            # Not a fallback: a model loaded into memory it does not have pages
            # for the whole time it answers.
            self.telemetry.refusals += 1
            steps.append(
                {
                    "step": "refuse",
                    "reason": "would not fit even after eviction",
                    "freed_by_eviction_bytes": freed,
                }
            )
            return ModelLoadOutcome(
                model=name,
                loaded=False,
                refused=True,
                steps=tuple(steps),
                reason=(
                    "the model needs more memory than is free"
                    + (
                        " even after unloading " + ", ".join(candidates)
                        if candidates
                        else ""
                    )
                ),
                available_memory_bytes=available,
            )

        # 4. Free what must go — and ONLY what must go. Evicting is not a
        # condition of loading: a model the incoming one can live beside stays
        # resident, because a reload costs seconds and discards the warm pages
        # the next request wants back. This is where "avoid unnecessary
        # switching" is enforced, rather than asked of every caller.
        evicted: list[str] = []
        if fits is False:
            for other in candidates:
                if self._release(other):
                    evicted.append(other)
        steps.append({"step": "evict", "models": list(evicted)})

        # 5. Load.
        started = self._clock()
        loaded = False
        try:
            if self._provider is not None:
                loaded = bool(self._provider.load(name))
        except Exception:  # pragma: no cover - a runtime must not break the caller
            loaded = False
        load_seconds = max(0.0, self._clock() - started)
        self.telemetry.record_load(load_seconds)
        self.telemetry.evictions += len(evicted)
        steps.append({"step": "load", "accepted": loaded, "seconds": round(load_seconds, 3)})

        # 6. Verify: the runtime listing the model is the only proof it is there.
        verified: bool | None = None
        if loaded:
            after = self._resident()
            # ``None`` when the runtime would not answer the question at all,
            # which is deliberately not the same as ``False``.
            verified = (
                None if after is None else any(_key(n) == _key(name) for n, _s in after)
            )
            if verified is False:
                self.telemetry.verified_failures += 1
        steps.append({"step": "verify", "loaded": loaded, "verified": verified})

        reason = ""
        if not loaded:
            reason = "the runtime did not accept the load"
        elif verified is False:
            reason = "the runtime accepted the load but does not list the model"
        elif verified is None:
            # Step 6 could not be carried out. The load is reported as what it
            # is — accepted and UNCONFIRMED — rather than as a verified success,
            # because the next request is the thing that will find out.
            reason = "the runtime accepted the load but could not confirm it"
        self._idle_since.pop(_key(name), None)
        return ModelLoadOutcome(
            model=name,
            loaded=bool(loaded and verified is not False),
            steps=tuple(steps),
            evicted=tuple(evicted),
            reason=reason,
            verified=verified,
            load_seconds=load_seconds,
            available_memory_bytes=available,
        )

    def acquire(self, model: str, *, force: bool = False) -> ModelLoadOutcome:
        """Mark ``model`` as IN USE and make sure it is resident — one call.

        The pair of :meth:`release_after_use`, and the only thing a request path
        has to remember before handing work to a model. It exists because a
        model a request needs must be made room for BEFORE the runtime is asked
        to run it: an automatic vision request used to reach the runtime with the
        chat model still resident — the situation the specification's own example
        describes (Qwen3 resident, a vision model needed, room made for it), and
        precisely what this layer was built to prevent.

        The activity is taken BEFORE the load on purpose: an eviction triggered
        by this very load must never be able to choose the model being acquired.
        It is left open for the caller, which is why ``acquire`` without a
        matching :meth:`release_after_use` is a leak — both call sites in the
        application are a single ``try``/``finally``.

        Idempotent and cheap when the model is already resident: :meth:`load`
        reports ``already_loaded`` without asking the runtime to do anything.
        """
        name = str(model or "").strip()
        if not name:
            return ModelLoadOutcome(
                model="", loaded=False, refused=True, reason="no model was named"
            )
        self.begin_activity(name)
        try:
            return self.load(name, force=force)
        except Exception:  # pragma: no cover - load is already defensive
            self.end_activity(name)
            return ModelLoadOutcome(
                model=name,
                loaded=False,
                reason="the runtime could not be asked to load it",
            )

    def unload(self, model: str = "") -> tuple[str, ...]:
        """Release one model, or every resident one. Returns what was released."""
        target = str(model or "").strip()
        if target:
            if _key(target) in self._active:
                return ()
            return (target,) if self._release(target) else ()
        released: list[str] = []
        for name in self._resident_names():
            if _key(name) in self._active:
                continue
            if self._release(name):
                released.append(name)
        return tuple(released)

    def _release(self, model: str) -> bool:
        """Unload through the provider (or the lifecycle manager when present)."""
        started = self._clock()
        released = False
        try:
            if self._lifecycle is not None:
                released = bool(self._lifecycle.unload_model(model))
            elif self._provider is not None:
                released = bool(self._provider.unload(model))
        except Exception:  # pragma: no cover - defensive
            released = False
        if released:
            self.telemetry.record_unload(max(0.0, self._clock() - started))
            self._idle_since.pop(_key(model), None)
        return released

    # ── keep-alive ──────────────────────────────────────────────────────
    def enforce_keep_alive(self, *, now: float | None = None) -> tuple[str, ...]:
        """Apply the policy: release what it says to release, and nothing else.

        Only ``WARM`` with a positive idle window unloads anything here, and only
        models whose activity has ended. ``IMMEDIATE`` is handled at the call
        site that knows the operation finished (see ``release_after_use``),
        ``WHILE_ACTIVE`` deliberately does nothing, and ``NEVER`` is the absence
        of initiative.
        """
        if self.keep_alive.policy is not KeepAlivePolicy.WARM or self.keep_alive.idle_seconds <= 0:
            return ()
        moment = self._clock() if now is None else now
        released: list[str] = []
        for name in self._resident_names():
            key = _key(name)
            if key in self._active:
                continue
            idle_since = self._idle_since.get(key)
            if idle_since is None:
                # First sighting: start its clock rather than guessing it has
                # been idle since boot.
                self._idle_since[key] = moment
                continue
            if (
                moment - idle_since >= self.keep_alive.idle_seconds
                and self._release(name)
            ):
                released.append(name)
        return tuple(released)

    def release_after_use(self, model: str) -> bool:
        """Called when the operation that used ``model`` has finished.

        The pair of :meth:`acquire`, and where each policy keeps its own promise:

        * ``IMMEDIATE`` releases the model as soon as the last operation using it
          has returned;
        * ``WHILE_ACTIVE`` releases it once no task that selected it is still
          running — the same moment, reached from the other direction, because
          "keep it warm while a task is running" is a statement about when it MAY
          go, not a reason to hold it for the rest of the process's life (which is
          what this policy used to do: it was indistinguishable from ``NEVER``);
        * ``WARM`` only marks it idle — its own window decides, applied by
          :meth:`enforce_keep_alive`;
        * ``NEVER`` never unloads on this layer's initiative.

        Having the caller say "I am done" is what keeps all four honest — the
        manager cannot see which operations are in flight, and pretending to
        would be a guess.
        """
        self.end_activity(model)
        if _key(model) in self._active:
            # Another task is still using it. Releasing now would pull the model
            # out from under that task, which is the one thing this layer must
            # never do.
            return False
        if self.keep_alive.policy in (
            KeepAlivePolicy.IMMEDIATE,
            KeepAlivePolicy.WHILE_ACTIVE,
        ):
            return self._release(model)
        return False


def _key(name: str) -> str:
    return str(name or "").strip().casefold()


def _seconds(value: Any) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _latency(samples: Sequence[float]) -> dict[str, Any]:
    """Count and percentiles for a latency list, or an honest empty block."""
    if not samples:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(float(sample) for sample in samples)
    return {
        "count": len(ordered),
        "p50_ms": round(_percentile(ordered, 0.5) * 1000.0, 2),
        "p95_ms": round(_percentile(ordered, 0.95) * 1000.0, 2),
        "max_ms": round(ordered[-1] * 1000.0, 2),
    }


def _percentile(ordered: Sequence[float], fraction: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]
