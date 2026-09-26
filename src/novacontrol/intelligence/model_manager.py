"""Model lifecycle management, behind one replaceable abstraction.

NovaControl runs on a 16 GB machine where two 4-8 GB models cannot both stay
resident. The lifecycle rules already exist and are already correct — they live
in ``integrations/llm.py`` (resident inspection, eviction, warm-up, measured
memory planning, exclusivity). What was missing was a NAMED, replaceable
interface, so callers can ask lifecycle questions without knowing that Ollama is
the backend:

    load_model(model)            unload_model(model)      is_loaded(model)
    get_active_model()           get_available_memory()   get_model_status()

Deliberately NOT a new implementation: :class:`OllamaBackend` delegates to the
existing functions, so the measured-memory policy (exclusive-by-default plus a
headroom-aware fit check) stays the single source of truth and cannot drift into
two versions. A future backend — a different runtime, or an NPU-accelerated
executor — implements :class:`ModelBackend` and nothing above it changes.

The NPU is intentionally NOT assumed: ``integrations/llm.py`` talks to whatever
endpoint the user configured, and this module never claims a hardware
acceleration path it has not measured.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast

from novacontrol.integrations.llm import (
    _OLLAMA_DEFAULT_URL,
    _OLLAMA_MEMORY_HEADROOM_BYTES,
    detect_ollama,
    ollama_available_memory_bytes,
    ollama_model_capabilities,
    ollama_model_size_bytes,
    ollama_models,
    ollama_resident_models,
    unload_ollama_model,
    warm_ollama_model,
)


#: How long the runtime is given to describe one model's capabilities.
_CAPABILITY_PROBE_TIMEOUT = 0.5


class ModelBackend(Protocol):
    """A model runtime this manager can drive."""

    name: str

    def list_models(self) -> tuple[str, ...]:
        """Every model available to load, without loading anything."""
        ...  # pragma: no cover - protocol

    def resident_models(self) -> tuple[tuple[str, int], ...] | None:
        """(name, resident bytes) for what is in memory; ``None`` = unknown."""
        ...  # pragma: no cover - protocol

    def load(self, model: str) -> bool:
        """Bring a model into memory."""
        ...  # pragma: no cover - protocol

    def unload(self, model: str) -> bool:
        """Release a model's memory."""
        ...  # pragma: no cover - protocol

    def available_memory_bytes(self) -> int | None:
        """Free physical memory, or ``None`` when the OS will not say."""
        ...  # pragma: no cover - protocol

    def model_size_bytes(self, model: str) -> int | None:
        """A model's footprint — resident when loaded, else on disk."""
        ...  # pragma: no cover - protocol

    def capabilities(self, model: str) -> tuple[str, ...] | None:
        """The runtime's own capability words for a model, or ``None``."""
        ...  # pragma: no cover - protocol


@dataclass(frozen=True, slots=True)
class OllamaBackend:
    """The local model runtime NovaControl ships against."""

    base_url: str = _OLLAMA_DEFAULT_URL
    name: str = "ollama"

    def list_models(self) -> tuple[str, ...]:
        models = ollama_models(self.base_url)
        return tuple(models or ())

    def resident_models(self) -> tuple[tuple[str, int], ...] | None:
        resident = ollama_resident_models(self.base_url)
        if resident is None:
            return None
        return tuple((entry["name"], int(entry["size"])) for entry in resident)

    def load(self, model: str) -> bool:
        # An empty warm-up request is how Ollama is asked to load a model
        # without generating anything.
        return bool(warm_ollama_model(model, self.base_url))

    def unload(self, model: str) -> bool:
        return bool(unload_ollama_model(model, self.base_url))

    def available_memory_bytes(self) -> int | None:
        return ollama_available_memory_bytes()

    def model_size_bytes(self, model: str) -> int | None:
        return ollama_model_size_bytes(model, self.base_url)

    def capabilities(self, model: str) -> tuple[str, ...] | None:
        """What the runtime says this model can do (``None`` = it did not say).

        ``/api/show`` is the one authoritative capability source available: a
        name is a guess and a config file is a claim, while this is the runtime
        reporting what the weights accept. ``None`` — unreachable, not pulled, or
        an older build with no capability metadata — is deliberately distinct
        from an empty answer, so a caller keeps its own evidence instead of
        being told the model can do nothing.
        """
        # A short timeout on purpose: this is asked once per installed model and
        # the answer is only ever an IMPROVEMENT on what is already declared, so
        # a runtime that is slow to describe its models must not be allowed to
        # hold up anything. (``ollama_models`` keeps the same discipline.)
        reported = ollama_model_capabilities(
            model, self.base_url, timeout=_CAPABILITY_PROBE_TIMEOUT
        )
        return None if reported is None else tuple(sorted(reported))


@dataclass(frozen=True, slots=True)
class ModelPlan:
    """What must happen before a model loads, and the evidence behind it.

    The measured figures travel with the decision so a load can be explained
    with real numbers, and so "could not measure" (``None``) is never quietly
    read as "nothing to worry about".
    """

    model: str
    available_bytes: int | None
    incoming_bytes: int | None
    evict: tuple[str, ...]
    fits_after_evict: bool | None

    def describe(self) -> str:
        """One line for the log: what was measured, and what was decided."""
        incoming = _gigabytes(self.incoming_bytes)
        available = _gigabytes(self.available_bytes)
        if not self.evict:
            return f"{self.model} ({incoming}): nothing else resident, {available} free"
        if self.fits_after_evict is None:
            verdict = "fit afterwards unknown"
        elif self.fits_after_evict:
            verdict = "room afterwards"
        else:
            verdict = "NOT enough room afterwards — expect paging"
        return (
            f"{self.model} ({incoming}): unloading {', '.join(self.evict)} "
            f"({available} free) — {verdict}"
        )


@dataclass(frozen=True, slots=True)
class ModelStatus:
    """A readable snapshot of the runtime's model state."""

    backend: str
    active_model: str
    loaded_models: tuple[str, ...]
    available_memory_bytes: int | None
    reachable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "active_model": self.active_model,
            "loaded_models": list(self.loaded_models),
            "available_memory_bytes": self.available_memory_bytes,
            "reachable": self.reachable,
        }


@dataclass(frozen=True, slots=True)
class ModelLoadResult:
    """The outcome of a load attempt, with the evidence behind it."""

    model: str
    loaded: bool
    evicted: tuple[str, ...] = ()
    reason: str = ""
    available_memory_bytes: int | None = None
    fits_after_evict: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "loaded": self.loaded,
            "evicted": list(self.evicted),
            "reason": self.reason,
            "available_memory_bytes": self.available_memory_bytes,
            "fits_after_evict": self.fits_after_evict,
        }


class ModelManager:
    """One place to ask lifecycle questions, whatever the backend is."""

    def __init__(self, backend: ModelBackend | None = None, *, exclusive: bool = True) -> None:
        # Declared as the protocol (not the concrete class) so a replacement
        # backend needs no change here or anywhere above it.
        self.backend: ModelBackend = backend if backend is not None else cast(ModelBackend, OllamaBackend())
        # Exclusive by default: on a machine this small, holding a chat model
        # and a vision model at once means one of them gets paged out mid-answer.
        # The measured memory check still applies independently — see
        # ollama_memory_plan for why the two reasons are reported separately.
        self.exclusive = exclusive

    # -- questions ------------------------------------------------------------

    def is_loaded(self, model: str) -> bool:
        resident = self.backend.resident_models()
        if resident is None:
            return False
        return any(_same_model(name, model) for name, _size in resident)

    def get_active_model(self) -> str:
        """The model currently in memory, or ``''`` when none (or unknown)."""
        resident = self.backend.resident_models()
        if not resident:
            return ""
        return resident[0][0]

    def get_available_memory(self) -> int | None:
        return self.backend.available_memory_bytes()

    def get_models(self) -> tuple[str, ...]:
        return self.backend.list_models()

    def get_model_status(self) -> ModelStatus:
        resident = self.backend.resident_models()
        return ModelStatus(
            backend=self.backend.name,
            active_model=resident[0][0] if resident else "",
            loaded_models=tuple(name for name, _size in resident) if resident else (),
            available_memory_bytes=self.get_available_memory(),
            reachable=resident is not None,
        )

    # -- actions --------------------------------------------------------------

    def plan_for(self, model: str) -> ModelPlan:
        """Decide what must be unloaded before ``model`` can load.

        Measured through the BACKEND, never through a hard-coded runtime: the
        same two independent reasons apply (exclusivity by policy, and the
        measured memory situation), and an unreachable backend yields a plan
        that evicts nothing rather than a guess.
        """
        resident = self.backend.resident_models()
        if resident is None:
            return ModelPlan(model=model, available_bytes=None, incoming_bytes=None, evict=(), fits_after_evict=None)
        others = tuple(name for name, _size in resident if not _same_model(name, model))
        available = self.backend.available_memory_bytes()
        incoming = next(
            (int(size) for name, size in resident if _same_model(name, model) and size),
            None,
        )
        if incoming is None:
            incoming = self.backend.model_size_bytes(model)
        evict = others if self.exclusive else ()
        fits: bool | None = None
        if available is not None and incoming is not None:
            required = incoming + _OLLAMA_MEMORY_HEADROOM_BYTES
            if available < required:
                # Memory overrides the policy: even with exclusivity off, a model
                # that cannot fit alongside what is resident must wait its turn.
                evict = others
            freed = sum(int(size) for name, size in resident if name in evict)
            fits = available + freed >= required
        return ModelPlan(
            model=model,
            available_bytes=available,
            incoming_bytes=incoming,
            evict=evict,
            fits_after_evict=fits,
        )

    def load_model(self, model: str) -> ModelLoadResult:
        """Load ``model``, freeing room first when the measurement requires it.

        Never raises: a lifecycle probe that cannot reach the runtime must not
        fail the request that asked for it.
        """
        try:
            plan = self.plan_for(model)
        except Exception:  # pragma: no cover - defensive: a probe must never throw
            plan = ModelPlan(model=model, available_bytes=None, incoming_bytes=None, evict=(), fits_after_evict=None)
        evicted: list[str] = []
        for resident in plan.evict:
            if self.unload_model(resident):
                evicted.append(resident)
        try:
            loaded = self.backend.load(model)
        except Exception:
            loaded = False
        return ModelLoadResult(
            model=model,
            loaded=loaded,
            evicted=tuple(evicted),
            reason=plan.describe(),
            available_memory_bytes=plan.available_bytes,
            fits_after_evict=plan.fits_after_evict,
        )

    def unload_model(self, model: str) -> bool:
        try:
            return bool(self.backend.unload(model))
        except Exception:  # pragma: no cover - defensive
            return False

    def unload_all(self) -> tuple[str, ...]:
        """Release every resident model — the honest \"free the RAM\" action."""
        resident = self.backend.resident_models() or ()
        evicted: list[str] = []
        for name, _size in resident:
            if self.unload_model(name):
                evicted.append(name)
        return tuple(evicted)

    def ensure_exclusive(self, model: str) -> tuple[str, ...]:
        """Unload whatever is resident except ``model``; return what was freed."""
        resident = self.backend.resident_models() or ()
        evicted: list[str] = []
        for name, _size in resident:
            if _same_model(name, model):
                continue
            if self.unload_model(name):
                evicted.append(name)
        return tuple(evicted)


def detect_backend(url: str = _OLLAMA_DEFAULT_URL) -> OllamaBackend | None:
    """Return a backend when the runtime answers, else ``None`` (never raises)."""
    try:
        detected = detect_ollama(url)
    except Exception:  # pragma: no cover - defensive
        return None
    if not detected:
        return None
    return OllamaBackend(base_url=str(detected.get("url") or url))


def _gigabytes(value: int | None) -> str:
    """Human GB for a measurement, or 'unknown' — never a confident 0."""
    return "unknown" if value is None else f"{value / 1024 ** 3:.1f} GB"


def _same_model(left: str, right: str) -> bool:
    """Compare model names tolerantly (``qwen3:8b`` == ``qwen3:8b``, tags aside)."""
    return left.strip().lower() == right.strip().lower()


def loaded_names(backend: ModelBackend | None = None) -> tuple[str, ...]:
    """Names held in memory, for callers that only need the list."""
    resident = (backend or OllamaBackend()).resident_models()
    return tuple(name for name, _size in resident) if resident else ()
