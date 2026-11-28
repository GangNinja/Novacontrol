"""Phase 33: adaptive model and compute intelligence.

This module is a DECISION layer over the model layer that already exists. It
does not own models, capabilities, hardware readings or lifecycle: those stay in
``models/profiles.py``, ``models/hardware.py`` and ``models/manager.py``. What is
added here is the missing question those layers do not answer:

    Given *this* task, which already-registered model/provider should serve it,
    what are the alternatives, why was this one chosen, and what happens when it
    fails?

The design follows four rules that are easy to state and hard to fake:

**Declared metadata only.** A model's capabilities, context window and size come
from its :class:`ModelProfile` (configured, declared in ``profiles.py``, or
reported by the runtime). Pricing, rate limits and latency are attached only when
they are genuinely known; an unknown figure is ``None`` and never a zero. A cloud
model whose capabilities were not configured is selectable for text only, because
that is the one capability its OpenAI-compatible transport can be trusted with.

**Selection is evidence, not a global leaderboard.** Every eligible endpoint is
scored from the task's requirements plus measured evidence (benchmark records,
observed success and latency), not from a name or a fixed favourite. The score
never overrides eligibility: an endpoint missing a required capability, context
or privacy permission is rejected no matter how good its numbers are.

**Failure is classified before it is retried.** Timeouts, rate limits, auth
failures, context overflow, malformed answers and resource exhaustion do not all
mean the same thing. Only retryable failures move to another compatible
candidate, and only while the original requirements still hold. A fallback that
drops a requirement is refused instead of being reported as a success.

**Learning is bounded and reversible.** Observations are stored per task category
and model, and a learned routing policy is only proposed once a category has
enough independent observations. Activation snapshots the previous policy; a
rollback restores it. This is deliberately not an online gradient step on a
handful of requests.

The runtime-facing pieces are injected, so this module remains usable from a CLI
and honest in tests: the provider registry, the local manager, the hardware
monitor, the benchmark service and the Phase 27 learning engine are collaborators,
not copies.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from novacontrol.models.hardware import HardwareMonitor
from novacontrol.models.manager import ModelManager
from novacontrol.models.profiles import (
    BASELINE,
    ModelCapability,
    ModelProfile,
    as_capabilities,
)

#: A policy change must be supported by this many observations in a category
#: before it may be activated. One good answer is an anecdote.
MIN_POLICY_SUPPORT = 3

#: A proposed policy must improve replayed success by at least this margin.
#: Below it, the difference is indistinguishable from noise on a small sample.
MIN_POLICY_IMPROVEMENT = 0.05

#: Bounded history. Routing is a request-path readout, not a growing journal.
DEFAULT_HISTORY = 200
DEFAULT_OBSERVATIONS = 500


class ProviderKind(StrEnum):
    LOCAL = "local"
    CLOUD = "cloud"


class ModelRole(StrEnum):
    """The job a model is primarily registered for.

    A model can declare several capabilities; this is the catalogue's primary
    classification, used for display and for preferring a specialist when the
    task names one. It is never the only reason a model is eligible.
    """

    GENERAL = "general"
    REASONING = "reasoning"
    CODING = "coding"
    VISION = "vision"
    EMBEDDING = "embedding"
    LIGHTWEIGHT = "lightweight"
    SPECIALIZED = "specialized"


class FailureKind(StrEnum):
    """Why a provider call failed, as a routing decision needs to know it."""

    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    AUTHENTICATION = "authentication"
    CONTEXT_LENGTH = "context_length"
    MODEL_OVERLOAD = "model_overload"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    MALFORMED_RESPONSE = "malformed_response"
    CONNECTION = "connection"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


#: Only these failures are worth another compatible candidate. Authentication
#: and unsupported-capability failures will fail again; a cancellation was the
#: caller's decision, not the provider's fault.
_RETRYABLE_FAILURES = frozenset(
    {
        FailureKind.TIMEOUT,
        FailureKind.RATE_LIMIT,
        FailureKind.MODEL_OVERLOAD,
        FailureKind.CONNECTION,
        FailureKind.MALFORMED_RESPONSE,
        FailureKind.RESOURCE_EXHAUSTION,
    }
)

#: Failure classification is done from the exception's type and its message. A
#: provider library does not share one exception hierarchy with this project, so
#: a status code or a well-known sentence is the only portable signal.
_FAILURE_PATTERNS: tuple[tuple[FailureKind, tuple[str, ...]], ...] = (
    (FailureKind.TIMEOUT, ("timeout", "timed out", "deadline exceeded", "read timed out")),
    (FailureKind.RATE_LIMIT, ("429", "rate limit", "rate_limit", "too many requests", "quota")),
    (FailureKind.AUTHENTICATION, ("401", "403", "unauthorized", "forbidden", "invalid api key", "authentication")),
    (
        FailureKind.CONTEXT_LENGTH,
        (
            "context length",
            "context_length",
            "maximum context",
            "too many tokens",
            "prompt is too long",
            "input is too long",
            "token limit",
        ),
    ),
    (FailureKind.MODEL_OVERLOAD, ("503", "502", "overloaded", "model is busy", "server busy")),
    (
        FailureKind.UNSUPPORTED_CAPABILITY,
        ("unsupported capability", "does not support", "not support vision", "model_not_supported"),
    ),
    (
        FailureKind.RESOURCE_EXHAUSTION,
        ("out of memory", "oom", "no space", "resource exhausted", "disk full"),
    ),
    (FailureKind.CONNECTION, ("connection", "connect", "network", "unreachable", "dns")),
    (FailureKind.CANCELLED, ("cancelled", "canceled")),
    (FailureKind.MALFORMED_RESPONSE, ("malformed", "invalid json", "empty response", "no content")),
)


def classify_failure(error: BaseException | str | None) -> FailureKind:
    """Classify one provider failure without pretending to know more.

    The order matters: an authentication response can also contain the word
    "connection", and a context overflow can arrive as a 400. Specific checks
    therefore come before the generic transport checks.
    """
    if error is None:
        return FailureKind.UNKNOWN
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return FailureKind.TIMEOUT
    if isinstance(error, asyncio.CancelledError):
        return FailureKind.CANCELLED
    text = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
    lowered = text.lower()
    for kind, needles in _FAILURE_PATTERNS:
        if any(needle in lowered for needle in needles):
            return kind
    return FailureKind.UNKNOWN


def failure_is_retryable(kind: FailureKind) -> bool:
    return kind in _RETRYABLE_FAILURES


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _round(value: float | None, places: int = 1) -> float | None:
    return round(float(value), places) if value is not None else None


# ── Providers ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ProviderRecord:
    """One provider NovaControl can actually reach or has configured.

    ``configured`` and ``available`` are separate facts. A cloud provider with a
    stored key is configured but may be unreachable; a local runtime can be
    running while no model is installed. The registry reports both so the UI can
    say which one is missing.
    """

    provider_id: str
    label: str = ""
    kind: ProviderKind = ProviderKind.LOCAL
    enabled: bool = True
    configured: bool = True
    models: tuple[str, ...] = ()
    base_url: str = ""
    auth_required: bool = False
    rate_limit_per_minute: int | None = None
    max_concurrency: int = 1
    pricing_per_1m_input: float | None = None
    pricing_per_1m_output: float | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        self.provider_id = str(self.provider_id or "").strip().lower()
        if not self.provider_id:
            raise ValueError("a provider record needs an id")
        self.label = str(self.label or self.provider_id)
        self.models = tuple(str(model).strip() for model in self.models if str(model).strip())
        self.rate_limit_per_minute = (
            max(1, int(self.rate_limit_per_minute))
            if self.rate_limit_per_minute is not None
            else None
        )
        self.max_concurrency = max(1, int(self.max_concurrency))

    @property
    def local(self) -> bool:
        return self.kind is ProviderKind.LOCAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "label": self.label,
            "kind": self.kind.value,
            "enabled": self.enabled,
            "configured": self.configured,
            "models": list(self.models),
            "base_url": self.base_url,
            "auth_required": self.auth_required,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "max_concurrency": self.max_concurrency,
            "pricing_per_1m_input": self.pricing_per_1m_input,
            "pricing_per_1m_output": self.pricing_per_1m_output,
            "notes": self.notes,
        }


@dataclass(slots=True)
class ProviderHealth:
    """Measured provider health, with a bounded cooldown after repeated failure."""

    provider_id: str
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_error_kind: FailureKind | None = None
    last_error: str = ""
    last_latency_ms: float | None = None
    total_latency_ms: float = 0.0
    latency_samples: int = 0
    cooldown_until: float = 0.0
    last_success_at: str = ""
    last_failure_at: str = ""

    @property
    def mean_latency_ms(self) -> float | None:
        if not self.latency_samples:
            return None
        return self.total_latency_ms / self.latency_samples

    def to_dict(self, *, now: float | None = None) -> dict[str, Any]:
        current = time.monotonic() if now is None else now
        return {
            "provider_id": self.provider_id,
            "successes": self.successes,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "last_error_kind": self.last_error_kind.value if self.last_error_kind else "",
            "last_error": self.last_error,
            "last_latency_ms": _round(self.last_latency_ms),
            "mean_latency_ms": _round(self.mean_latency_ms),
            "cooldown_seconds_remaining": round(max(0.0, self.cooldown_until - current), 1),
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
        }


class ProviderRegistry:
    """The authoritative set of providers, their switches and measured health.

    This is not a second model registry. It owns provider identity, enablement,
    rate limits and failure state; model capabilities remain in
    :class:`~novacontrol.models.profiles.ModelRegistry`, and model lifecycle
    remains in :class:`~novacontrol.models.manager.ModelManager`.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        failure_threshold: int = 2,
        cooldown_seconds: float = 45.0,
    ) -> None:
        self._providers: dict[str, ProviderRecord] = {}
        self._health: dict[str, ProviderHealth] = {}
        self._clock = clock
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))

    def register(self, record: ProviderRecord) -> ProviderRecord:
        # Re-registration refreshes what the provider IS (label, models, base
        # URL) but never flips a user switch: the application re-syncs the
        # catalogue on every status read, and without this a provider the user
        # disabled would be silently re-enabled on the next refresh.
        existing = self._providers.get(record.provider_id)
        if existing is not None:
            record = replace(record, enabled=existing.enabled)
        self._providers[record.provider_id] = record
        self._health.setdefault(record.provider_id, ProviderHealth(record.provider_id))
        return record

    def unregister(self, provider_id: str) -> bool:
        key = str(provider_id or "").strip().lower()
        self._health.pop(key, None)
        return self._providers.pop(key, None) is not None

    def get(self, provider_id: str) -> ProviderRecord | None:
        return self._providers.get(str(provider_id or "").strip().lower())

    def all(self) -> tuple[ProviderRecord, ...]:
        return tuple(sorted(self._providers.values(), key=lambda row: row.provider_id))

    def enabled(self) -> tuple[ProviderRecord, ...]:
        return tuple(row for row in self.all() if row.enabled and row.configured)

    def set_enabled(self, provider_id: str, enabled: bool) -> ProviderRecord:
        record = self.get(provider_id)
        if record is None:
            raise ValueError(f"Unknown provider: {provider_id!r}")
        updated = replace(record, enabled=bool(enabled))
        self._providers[updated.provider_id] = updated
        return updated

    def health(self, provider_id: str) -> ProviderHealth:
        key = str(provider_id or "").strip().lower()
        return self._health.setdefault(key, ProviderHealth(key))

    def available(self, provider_id: str) -> bool:
        """Configured, enabled, and not cooling down from recent failures."""
        record = self.get(provider_id)
        if record is None or not record.enabled or not record.configured:
            return False
        health = self.health(record.provider_id)
        return health.cooldown_until <= self._clock()

    def record_success(self, provider_id: str, *, latency_ms: float | None = None) -> None:
        health = self.health(provider_id)
        health.successes += 1
        health.consecutive_failures = 0
        health.cooldown_until = 0.0
        health.last_error_kind = None
        health.last_error = ""
        health.last_success_at = _now_iso()
        if latency_ms is not None:
            measured = max(0.0, float(latency_ms))
            health.last_latency_ms = measured
            health.total_latency_ms += measured
            health.latency_samples += 1

    def record_failure(
        self,
        provider_id: str,
        kind: FailureKind,
        *,
        error: str = "",
        retry_after_seconds: float | None = None,
    ) -> None:
        health = self.health(provider_id)
        health.failures += 1
        health.consecutive_failures += 1
        health.last_error_kind = kind
        health.last_error = str(error)[:300]
        health.last_failure_at = _now_iso()
        cooldown = retry_after_seconds
        if cooldown is None and health.consecutive_failures >= self.failure_threshold:
            cooldown = self.cooldown_seconds
        if cooldown is not None and cooldown > 0:
            health.cooldown_until = self._clock() + float(cooldown)

    def mark_recovered(self, provider_id: str) -> None:
        health = self.health(provider_id)
        health.consecutive_failures = 0
        health.cooldown_until = 0.0

    def snapshot(self) -> tuple[dict[str, Any], ...]:
        now = self._clock()
        rows = []
        for record in self.all():
            health = self.health(record.provider_id)
            rows.append(
                {
                    **record.to_dict(),
                    "available": self.available(record.provider_id),
                    "health": health.to_dict(now=now),
                }
            )
        return tuple(rows)


# ── Catalogue ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ModelEndpoint:
    """A model attached to a provider, with only metadata we can stand behind.

    ``profile`` is the existing capability declaration. Pricing and rate limits
    are optional because most of the information this build receives does not
    include them; a missing value means "unknown", never "free" or "unlimited".
    """

    provider_id: str
    model: str
    profile: ModelProfile
    role: ModelRole = ModelRole.GENERAL
    family: str = ""
    input_modalities: tuple[str, ...] = ("text",)
    output_modalities: tuple[str, ...] = ("text",)
    context_length: int | None = None
    max_output_tokens: int | None = None
    pricing_per_1m_input: float | None = None
    pricing_per_1m_output: float | None = None
    rate_limit_per_minute: int | None = None
    latency_ms: float | None = None
    reliability: float | None = None
    limitations: tuple[str, ...] = ()
    metadata_source: str = "profile"

    def __post_init__(self) -> None:
        if not str(self.provider_id or "").strip():
            raise ValueError("an endpoint needs a provider id")
        if not str(self.model or self.profile.name or "").strip():
            raise ValueError("an endpoint needs a model")
        context = self.context_length if self.context_length is not None else self.profile.context_length
        object.__setattr__(self, "context_length", context)
        # A caller that did not classify the role gets the profile's own
        # capability-derived role, never a blanket "general": a vision model
        # registered without an explicit role must still be recognised as one.
        if self.role is ModelRole.GENERAL:
            object.__setattr__(self, "role", _role_for(self.profile))

    @property
    def capabilities(self) -> frozenset[ModelCapability]:
        return self.profile.capabilities

    @property
    def parameters_b(self) -> float | None:
        return self.profile.parameters_b

    @property
    def supports_tools(self) -> bool:
        return ModelCapability.TOOL_CALLING in self.capabilities

    @property
    def supports_structured_output(self) -> bool:
        return ModelCapability.STRUCTURED_OUTPUT in self.capabilities

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model": self.model,
            "family": self.family,
            "role": self.role.value,
            "capabilities": sorted(capability.value for capability in self.capabilities),
            "context_length": self.context_length,
            "max_output_tokens": self.max_output_tokens,
            "input_modalities": list(self.input_modalities),
            "output_modalities": list(self.output_modalities),
            "supports_tools": self.supports_tools,
            "supports_structured_output": self.supports_structured_output,
            "pricing_per_1m_input": self.pricing_per_1m_input,
            "pricing_per_1m_output": self.pricing_per_1m_output,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "latency_ms": _round(self.latency_ms),
            "reliability": _round(self.reliability, 3),
            "declared": self.profile.declared,
            "inferred_from": self.profile.inferred_from,
            "runtime_confirmed": self.profile.runtime_confirmed,
            "limitations": list(self.limitations),
            "metadata_source": self.metadata_source,
            "notes": self.profile.notes,
            "memory_bytes": self.profile.memory_bytes,
            "parameters_b": self.profile.parameters_b,
        }


def _policy_key(provider_id: str, model: str) -> str:
    """A learned-policy key that cannot merge two providers' same-named model.

    Provider/model is the identity the evidence is stored under; a cloud and a
    local build called ``llama3:8b`` are different choices and must not share
    one success rate. The old model-only shape is still read as a fallback so a
    policy persisted before this fix keeps working.
    """
    provider = str(provider_id or "").strip()
    name = str(model or "").strip()
    return f"{provider}/{name}" if provider and name else name


def _role_for(profile: ModelProfile) -> ModelRole:
    """Classify a model by what it DECLARES, not by its name."""
    if ModelCapability.EMBEDDINGS in profile.capabilities and len(profile.capabilities) == 1:
        return ModelRole.EMBEDDING
    if ModelCapability.VISION in profile.capabilities:
        return ModelRole.VISION
    if ModelCapability.CODING in profile.capabilities:
        return ModelRole.CODING
    if ModelCapability.REASONING in profile.capabilities:
        return ModelRole.REASONING
    parameters = profile.parameters_b
    if parameters is not None and parameters <= 4:
        return ModelRole.LIGHTWEIGHT
    return ModelRole.GENERAL


# ── Task profile ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TaskProfile:
    """What a task needs from a model, derived from existing task analysis.

    ``source`` names the estimator that produced the cost/complexity part, so a
    caller can tell a measured classification from an explicit override.
    """

    text: str
    category: str = "chat"
    complexity: str = "medium"
    score: float = 0.3
    capabilities: tuple[ModelCapability, ...] = (ModelCapability.TEXT,)
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    min_context_tokens: int = 0
    latency_sensitivity: str = "balanced"
    cost_sensitivity: str = "balanced"
    local_only: bool = False
    privacy_required: bool = False
    requires_vision: bool = False
    requires_tools: bool = False
    requires_structured_output: bool = False
    requires_coding: bool = False
    reasoning_depth: str = "none"
    deterministic: bool = False
    expected_latency_seconds: float | None = None
    source: str = "task_cost_estimator"

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text[:300],
            "category": self.category,
            "complexity": self.complexity,
            "score": round(self.score, 3),
            "capabilities": sorted(capability.value for capability in self.capabilities),
            "estimated_input_tokens": self.estimated_input_tokens,
            "estimated_output_tokens": self.estimated_output_tokens,
            "min_context_tokens": self.min_context_tokens,
            "latency_sensitivity": self.latency_sensitivity,
            "cost_sensitivity": self.cost_sensitivity,
            "local_only": self.local_only,
            "privacy_required": self.privacy_required,
            "requires_vision": self.requires_vision,
            "requires_tools": self.requires_tools,
            "requires_structured_output": self.requires_structured_output,
            "requires_coding": self.requires_coding,
            "reasoning_depth": self.reasoning_depth,
            "deterministic": self.deterministic,
            "expected_latency_seconds": self.expected_latency_seconds,
            "source": self.source,
        }


class TaskClassifier(Protocol):
    def estimate(
        self,
        text: str,
        *,
        requires_vision: bool = False,
    ) -> Any: ...

    def route_hint(self, estimate: Any) -> dict[str, Any]: ...


class AdaptiveTaskClassifier:
    """Adapter over the existing cost/complexity estimator.

    It does not add a second NLU. The category, complexity and cost band come
    from :class:`~novacontrol.decision.cost.TaskCostEstimator`; this class only
    translates them into the capability requirements a model selector needs and
    accepts advisory flags from the existing ``StructuredIntent``.
    """

    def __init__(self, estimator: TaskClassifier | None = None) -> None:
        if estimator is None:
            from novacontrol.decision.cost import TaskCostEstimator

            estimator = TaskCostEstimator()
        self._estimator = estimator

    def classify(
        self,
        text: str,
        *,
        intent: Any | None = None,
        has_image: bool = False,
        local_only: bool = False,
        requires_tools: bool = False,
        requires_structured_output: bool = False,
        latency_sensitivity: str = "",
        min_context_tokens: int = 0,
    ) -> TaskProfile:
        request = str(text or "").strip()
        estimate = self._estimator.estimate(request, requires_vision=has_image)
        hint = self._estimator.route_hint(estimate)
        category = str(getattr(estimate, "category", "") or "chat")
        required: set[ModelCapability] = set(BASELINE)
        requires_vision = bool(has_image or getattr(intent, "requires_vision", False))
        intent_reasoning = str(getattr(intent, "reasoning_level", "none") or "none")
        requires_coding = category in {"code", "build"}
        requires_reasoning = category not in {
            "vision",
            "status",
            "simple_action",
            "automation",
        } and (
            hint.get("requires_capable_model", False)
            or intent_reasoning == "high"
            or category in {"build"}
        )
        tools = bool(requires_tools or getattr(intent, "requires_tools", False))
        if requires_vision:
            required.add(ModelCapability.VISION)
        if requires_coding:
            required.add(ModelCapability.CODING)
        if requires_reasoning:
            required.add(ModelCapability.REASONING)
        if tools:
            required.add(ModelCapability.TOOL_CALLING)
        if requires_structured_output:
            required.add(ModelCapability.STRUCTURED_OUTPUT)

        # Token estimates are deliberately simple and labelled as estimates. A
        # document/PDF task gets a larger context floor because its own extractor
        # will hand the model several thousand tokens; a chat sentence does not.
        input_tokens = max(1, len(request) // 4)
        output_tokens = 256
        if category in {"document", "research"}:
            input_tokens = max(input_tokens, 4096)
            output_tokens = 768
        elif category == "build":
            input_tokens = max(input_tokens, 2048)
            output_tokens = 1200
        elif category == "code":
            output_tokens = 768
        elif category == "vision":
            output_tokens = 512
        context_floor = max(int(min_context_tokens), input_tokens + output_tokens)
        if category in {"document", "research"}:
            context_floor = max(context_floor, 8192)
        # A request that the estimator says a deterministic tool can carry still
        # has a model-capable fallback, but it is marked so the router can say
        # "no model needed" instead of spending a call on a counter read.
        deterministic = bool(hint.get("deterministic", False)) and not requires_vision
        if not latency_sensitivity:
            latency_sensitivity = (
                "high"
                if estimate.level.value in {"trivial", "low"}
                else "quality"
                if hint.get("requires_capable_model", False)
                else "balanced"
            )
        cost_sensitivity = "high" if local_only or estimate.level.value in {"trivial", "low"} else "balanced"
        if estimate.level.value in {"high", "very_high"}:
            cost_sensitivity = "low"
        reasoning_depth = "high" if requires_reasoning and not requires_coding else (
            "medium" if requires_reasoning or requires_coding else "none"
        )
        return TaskProfile(
            text=request,
            category=category,
            complexity=str(getattr(estimate, "complexity", "") or "medium"),
            score=float(getattr(estimate, "score", 0.0) or 0.0),
            capabilities=tuple(sorted(required, key=lambda item: item.value)),
            estimated_input_tokens=input_tokens,
            estimated_output_tokens=output_tokens,
            min_context_tokens=context_floor,
            latency_sensitivity=latency_sensitivity,
            cost_sensitivity=cost_sensitivity,
            local_only=bool(local_only),
            privacy_required=bool(local_only),
            requires_vision=requires_vision,
            requires_tools=tools,
            requires_structured_output=requires_structured_output,
            requires_coding=requires_coding,
            reasoning_depth=reasoning_depth,
            deterministic=deterministic,
            expected_latency_seconds=float(getattr(estimate, "expected_latency_seconds", 0.0) or 0.0)
            or None,
            source="task_cost_estimator",
        )


# ── Selection and routing ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CandidateScore:
    endpoint: ModelEndpoint
    score: float
    eligible: bool
    reasons: tuple[str, ...] = ()
    rejected_because: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint.to_dict(),
            "score": round(self.score, 4),
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "rejected_because": list(self.rejected_because),
        }


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """A selection with its alternatives and the reasoning that produced it."""

    task: TaskProfile
    route: str
    model: str = ""
    provider_id: str = ""
    provider_kind: str = ""
    role: str = ""
    rationale: tuple[str, ...] = ()
    candidates: tuple[CandidateScore, ...] = ()
    fallback: tuple[ModelEndpoint, ...] = ()
    policy: str = "evidence"
    deterministic: bool = False
    source: str = "adaptive_model_router"
    created_at: str = field(default_factory=_now_iso)

    @property
    def found(self) -> bool:
        return bool(self.model)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "model": self.model,
            "provider_id": self.provider_id,
            "provider_kind": self.provider_kind,
            "role": self.role,
            "rationale": list(self.rationale),
            "fallback": [endpoint.to_dict() for endpoint in self.fallback],
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "policy": self.policy,
            "deterministic": self.deterministic,
            "source": self.source,
            "created_at": self.created_at,
            "task": self.task.to_dict(),
        }


@dataclass(slots=True)
class RoutingPreferences:
    """User controls over automatic selection; every field changes real state."""

    allow_cloud: bool = True
    prefer_local: bool = True
    preferred_model: str = ""
    max_cost_per_request: float | None = None
    max_context_tokens: int | None = None
    max_concurrency: int = 2
    latency_budget_ms: float | None = None
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "allow_cloud": self.allow_cloud,
            "prefer_local": self.prefer_local,
            "preferred_model": self.preferred_model,
            "max_cost_per_request": self.max_cost_per_request,
            "max_context_tokens": self.max_context_tokens,
            "max_concurrency": self.max_concurrency,
            "latency_budget_ms": self.latency_budget_ms,
            "enabled": self.enabled,
        }


@dataclass(frozen=True, slots=True)
class RoutingObservation:
    """One measured outcome for one endpoint on one task category."""

    provider_id: str
    model: str
    category: str
    success: bool
    latency_ms: float
    failure_kind: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    fallback: bool = False
    retries: int = 0
    at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model": self.model,
            "category": self.category,
            "success": self.success,
            "latency_ms": _round(self.latency_ms),
            "failure_kind": self.failure_kind,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "fallback": self.fallback,
            "retries": self.retries,
            "at": self.at,
        }


@dataclass(frozen=True, slots=True)
class RouteAttempt:
    provider_id: str
    model: str
    success: bool
    failure_kind: str = ""
    error: str = ""
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model": self.model,
            "success": self.success,
            "failure_kind": self.failure_kind,
            "error": self.error[:300],
            "latency_ms": _round(self.latency_ms),
        }


@dataclass(frozen=True, slots=True)
class RouteOutcome:
    """What actually answered, or the accurate reason nothing compatible did."""

    decision: RouteDecision
    answered: bool
    text: str = ""
    provider_id: str = ""
    model: str = ""
    attempts: tuple[RouteAttempt, ...] = ()
    fallback_used: bool = False
    latency_ms: float = 0.0
    usage: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "answered": self.answered,
            "text": self.text,
            "provider_id": self.provider_id,
            "model": self.model,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "fallback_used": self.fallback_used,
            "latency_ms": _round(self.latency_ms),
            "usage": dict(self.usage),
            "error": self.error,
            "decision": self.decision.to_dict(),
        }


@dataclass(slots=True)
class RoutingPolicy:
    """A learned per-category endpoint bonus, versioned and reversible."""

    version: int = 0
    active: bool = False
    bonuses: dict[str, dict[str, float]] = field(default_factory=dict)
    support: dict[str, int] = field(default_factory=dict)
    baseline_success: dict[str, float] = field(default_factory=dict)
    expected_success: dict[str, float] = field(default_factory=dict)
    created_at: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "active": self.active,
            "bonuses": {category: dict(rows) for category, rows in self.bonuses.items()},
            "support": dict(self.support),
            "baseline_success": {key: round(value, 3) for key, value in self.baseline_success.items()},
            "expected_success": {key: round(value, 3) for key, value in self.expected_success.items()},
            "created_at": self.created_at,
            "reason": self.reason,
        }


@dataclass(slots=True)
class _Performance:
    successes: int = 0
    failures: int = 0
    total_latency_ms: float = 0.0

    @property
    def samples(self) -> int:
        return self.successes + self.failures

    @property
    def success_rate(self) -> float | None:
        return self.successes / self.samples if self.samples else None

    @property
    def mean_latency_ms(self) -> float | None:
        return self.total_latency_ms / self.samples if self.samples else None


class AdaptiveModelRouter:
    """Select a model by task evidence, execute it, and record what happened.

    The router is synchronous on the selection path (it performs no I/O beyond
    the already-cached monitor readings) and asynchronous only when it calls a
    provider. Selection cost is therefore a few dictionary lookups and sorts,
    which is what keeps routing from becoming the bottleneck it is meant to
    avoid.
    """

    def __init__(
        self,
        model_manager: ModelManager,
        *,
        providers: ProviderRegistry | None = None,
        classifier: AdaptiveTaskClassifier | None = None,
        benchmark: Any | None = None,
        monitor: HardwareMonitor | None = None,
        learning: Any | None = None,
        preferences: RoutingPreferences | None = None,
        history: int = DEFAULT_HISTORY,
        observation_limit: int = DEFAULT_OBSERVATIONS,
        clock: Callable[[], float] = time.monotonic,
        store: Any | None = None,
        adapter_resolver: Callable[[ModelEndpoint], Any | None] | None = None,
        include_declared: bool = True,
    ) -> None:
        self.model_manager = model_manager
        self.providers = providers or ProviderRegistry(clock=clock)
        self.classifier = classifier or AdaptiveTaskClassifier()
        self.benchmark = benchmark
        self.monitor = monitor or getattr(model_manager, "monitor", None)
        self.learning = learning
        self.preferences = preferences or RoutingPreferences()
        self._clock = clock
        self._store = store
        self._adapter_resolver = adapter_resolver
        self._endpoints: dict[tuple[str, str], ModelEndpoint] = {}
        self._adapters: dict[str, Any] = {}
        self._history: deque[RouteDecision] = deque(maxlen=max(1, int(history)))
        self._observations: deque[RoutingObservation] = deque(
            maxlen=max(1, int(observation_limit))
        )
        self._performance: dict[tuple[str, str, str], _Performance] = {}
        self._policy = RoutingPolicy()
        self._policy_history: list[RoutingPolicy] = []
        self._inflight = 0
        self._queued = 0
        self._semaphore = asyncio.Semaphore(max(1, int(self.preferences.max_concurrency)))
        self._load_policy()
        if include_declared:
            self._register_known_local_models()

    # -- registration -----------------------------------------------------

    def register_provider(
        self,
        record: ProviderRecord,
        *,
        adapter: Any | None = None,
    ) -> ProviderRecord:
        """Register a provider and its execution adapter (never its secret)."""
        stored = self.providers.register(record)
        if adapter is not None:
            self._adapters[stored.provider_id] = adapter
        return stored

    def register_endpoint(self, endpoint: ModelEndpoint) -> ModelEndpoint:
        if self.providers.get(endpoint.provider_id) is None:
            raise ValueError(
                f"provider {endpoint.provider_id!r} must be registered before its models"
            )
        self._endpoints[(endpoint.provider_id, endpoint.model)] = endpoint
        record = self.providers.get(endpoint.provider_id)
        if record is not None and endpoint.model not in record.models:
            record.models = (*record.models, endpoint.model)
        return endpoint

    def remove_endpoint(self, provider_id: str, model: str) -> bool:
        """Remove one model without discarding its provider's health/switch."""
        key = (str(provider_id or "").strip().lower(), str(model or "").strip())
        removed = self._endpoints.pop(key, None) is not None
        record = self.providers.get(key[0])
        if record is not None:
            record.models = tuple(item for item in record.models if item != key[1])
        return removed

    def remove_provider(self, provider_id: str) -> bool:
        key = str(provider_id or "").strip().lower()
        self._adapters.pop(key, None)
        for endpoint_key in [item for item in self._endpoints if item[0] == key]:
            self._endpoints.pop(endpoint_key, None)
        return self.providers.unregister(key)

    def _register_known_local_models(self) -> None:
        provider_id = str(self.model_manager.provider_name() or "local").lower()
        try:
            discovered = list(self.model_manager.registry.all())
        except Exception:  # pragma: no cover - a registry read must not block boot
            discovered = []
        if provider_id and self.providers.get(provider_id) is None:
            self.providers.register(
                ProviderRecord(
                    provider_id=provider_id,
                    label=f"{provider_id} (local)",
                    kind=ProviderKind.LOCAL,
                    models=tuple(profile.name for profile in discovered),
                    max_concurrency=1,
                    notes="local runtime; no per-token provider charge",
                )
            )
        for profile in discovered:
            if not profile.provider:
                profile = replace(profile, provider=provider_id)
            self.register_endpoint(self._endpoint_for_local(profile, provider_id=provider_id))

    def _endpoint_for_local(self, profile: ModelProfile, *, provider_id: str) -> ModelEndpoint:
        role = _role_for(profile)
        modalities = ["text"]
        if ModelCapability.VISION in profile.capabilities:
            modalities.append("image")
        return ModelEndpoint(
            provider_id=provider_id,
            model=profile.name,
            profile=profile,
            role=role,
            family=_family_from_profile(profile),
            input_modalities=tuple(modalities),
            context_length=profile.context_length,
            pricing_per_1m_input=0.0,
            pricing_per_1m_output=0.0,
            limitations=(
                ()
                if profile.runtime_confirmed or profile.declared
                else ("capabilities inferred; runtime did not report them",)
            ),
            metadata_source="runtime-reported"
            if profile.runtime_confirmed
            else "declared"
            if profile.declared
            else "inferred",
        )

    def sync_local_models(self, profiles: Sequence[ModelProfile] | None = None) -> int:
        """Refresh the local endpoints from the existing model registry.

        No new discovery mechanism is introduced: callers hand in profiles the
        manager already discovered/reconciled, or it reads the manager's own
        registry. Existing endpoints keep their identity, so measured evidence
        survives a refresh.
        """
        provider_id = str(self.model_manager.provider_name() or "local").lower()
        if profiles is None:
            profiles = list(self.model_manager.registry.all())
        count = 0
        for profile in profiles:
            profile = replace(profile, provider=profile.provider or provider_id)
            self.register_endpoint(self._endpoint_for_local(profile, provider_id=provider_id))
            count += 1
        return count

    def prune_local_models(self, provider_id: str, keep: Iterable[str]) -> int:
        """Drop local endpoints the runtime no longer lists.

        Discovery adds models but must also be able to subtract them: a model
        uninstalled after boot would otherwise stay selectable until restart.
        Only call this after a discovery pass that actually answered; an empty
        or failed probe is not evidence that nothing is installed.
        """
        wanted = {str(name).strip() for name in keep if str(name).strip()}
        removed = 0
        for endpoint in self.endpoints():
            if endpoint.provider_id == provider_id and endpoint.model not in wanted:
                if self.remove_endpoint(provider_id, endpoint.model):
                    removed += 1
        return removed

    def register_cloud_model(
        self,
        *,
        provider_id: str,
        model: str,
        label: str = "",
        adapter: Any | None = None,
        profile: ModelProfile | None = None,
        context_length: int | None = None,
        pricing_per_1m_input: float | None = None,
        pricing_per_1m_output: float | None = None,
        rate_limit_per_minute: int | None = None,
        base_url: str = "",
        limitations: Sequence[str] = (),
    ) -> ModelEndpoint:
        """Register the single cloud model the user configured.

        The default profile declares TEXT only. That is not pessimism: the
        OpenAI-compatible transport is known to carry text, and no provider
        metadata this build receives documents vision, tools or structured
        output. An operator who knows their model's capabilities can pass a
        profile; the catalogue then records where that declaration came from.
        """
        name = str(model or "").strip()
        if not name:
            raise ValueError("a cloud model needs a configured model name")
        profile = profile or ModelProfile(
            name=name,
            capabilities=BASELINE,
            provider=provider_id,
            context_length=context_length,
            declared=False,
            inferred_from="configured provider model name; capabilities not reported",
            notes="text transport only until capabilities are configured",
        )
        self.register_provider(
            ProviderRecord(
                provider_id=provider_id,
                label=label or provider_id,
                kind=ProviderKind.CLOUD,
                models=(name,),
                base_url=base_url,
                auth_required=True,
                rate_limit_per_minute=rate_limit_per_minute,
                pricing_per_1m_input=pricing_per_1m_input,
                pricing_per_1m_output=pricing_per_1m_output,
                notes="cloud provider; sends the request off this machine",
            ),
            adapter=adapter,
        )
        modalities = ["text"]
        if ModelCapability.VISION in profile.capabilities:
            modalities.append("image")
        endpoint = ModelEndpoint(
            provider_id=provider_id,
            model=name,
            profile=profile,
            role=_role_for(profile),
            family=_family_from_profile(profile),
            input_modalities=tuple(modalities),
            context_length=context_length,
            pricing_per_1m_input=pricing_per_1m_input,
            pricing_per_1m_output=pricing_per_1m_output,
            rate_limit_per_minute=rate_limit_per_minute,
            limitations=tuple(limitations)
            or (() if profile.declared else ("capabilities not reported by provider",)),
            metadata_source="declared" if profile.declared else "configured-name-only",
        )
        return self.register_endpoint(endpoint)

    def endpoints(self) -> tuple[ModelEndpoint, ...]:
        return tuple(sorted(self._endpoints.values(), key=lambda row: (row.provider_id, row.model)))

    def executable(self) -> bool:
        """Whether at least one registered provider has a real execution adapter."""
        return any(
            endpoint.provider_id in self._adapters
            or self._adapter_resolver is not None
            for endpoint in self.endpoints()
        )

    # -- status -----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Everything the management surface needs, without probing a runtime."""
        providers = []
        for row in self.providers.snapshot():
            endpoints = [
                endpoint.to_dict()
                for endpoint in self.endpoints()
                if endpoint.provider_id == row["provider_id"]
            ]
            providers.append({**row, "endpoints": endpoints})
        return {
            "enabled": self.preferences.enabled,
            "providers": providers,
            "models": [endpoint.to_dict() for endpoint in self.endpoints()],
            "preferences": self.preferences.to_dict(),
            "policy": self._policy.to_dict(),
            "policy_history": len(self._policy_history),
            "history": len(self._history),
            "observations": len(self._observations),
            "inflight": self._inflight,
            "queued": self._queued,
            "concurrency_limit": self.preferences.max_concurrency,
            "last_route": self._history[-1].to_dict() if self._history else None,
        }

    def routing_history(self, limit: int = 20) -> dict[str, Any]:
        rows = list(self._history)[-max(1, min(int(limit), 100)) :]
        return {"routes": [row.to_dict() for row in reversed(rows)], "count": len(rows)}

    def observations(self, limit: int = 50) -> dict[str, Any]:
        rows = list(self._observations)[-max(1, min(int(limit), 200)) :]
        return {"observations": [row.to_dict() for row in reversed(rows)], "count": len(rows)}

    # -- preferences/control ---------------------------------------------

    def configure_preferences(self, **changes: Any) -> dict[str, Any]:
        """Apply real routing controls, rejecting values that are not usable."""
        allowed = set(RoutingPreferences.__dataclass_fields__)
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Unknown routing preference(s): {', '.join(sorted(unknown))}")
        current = self.preferences
        values: dict[str, Any] = {}
        for key, value in changes.items():
            if key in {"allow_cloud", "prefer_local", "enabled"}:
                if not isinstance(value, bool):
                    raise ValueError(f"{key} must be true or false")
                values[key] = value
            elif key == "preferred_model":
                values[key] = str(value or "").strip()
            elif key == "max_cost_per_request":
                values[key] = _optional_non_negative_float(value, key=key)
            elif key == "max_context_tokens":
                values[key] = _optional_positive_int(value, key=key)
            elif key == "max_concurrency":
                number = _optional_positive_int(value, key=key)
                values[key] = number or 1
            elif key == "latency_budget_ms":
                values[key] = _optional_non_negative_float(value, key=key)
        updated = replace(current, **values)
        if updated.max_concurrency != current.max_concurrency:
            self._semaphore = asyncio.Semaphore(updated.max_concurrency)
        self.preferences = updated
        return updated.to_dict()

    def set_provider_enabled(self, provider_id: str, enabled: bool) -> dict[str, Any]:
        record = self.providers.set_enabled(provider_id, enabled)
        return record.to_dict()

    def controls(self) -> dict[str, Any]:
        return {
            "preferences": self.preferences.to_dict(),
            "providers": [
                {
                    "provider_id": row.provider_id,
                    "enabled": row.enabled,
                    "configured": row.configured,
                    "kind": row.kind.value,
                }
                for row in self.providers.all()
            ],
        }

    # -- selection --------------------------------------------------------

    def route(
        self,
        text: str,
        *,
        intent: Any | None = None,
        has_image: bool = False,
        local_only: bool | None = None,
        requires_tools: bool = False,
        requires_structured_output: bool = False,
        latency_sensitivity: str = "",
        min_context_tokens: int = 0,
        task: TaskProfile | None = None,
        record: bool = True,
    ) -> RouteDecision:
        """Select the best eligible endpoint, or explain why none is eligible."""
        if task is not None:
            resolved_task = task
            # A caller-supplied task must not be able to override the privacy
            # constraint of THIS call: complete(..., local_only=True, task=…) is
            # still a local-only request.
            if local_only:
                resolved_task = replace(
                    task, local_only=True, privacy_required=True
                )
        else:
            resolved_task = self.classifier.classify(
                text,
                intent=intent,
                has_image=has_image,
                local_only=bool(
                    self._local_only_default() if local_only is None else local_only
                ),
                requires_tools=requires_tools,
                requires_structured_output=requires_structured_output,
                latency_sensitivity=latency_sensitivity,
                min_context_tokens=min_context_tokens,
            )
        if not self.preferences.enabled:
            return self._remember(
                RouteDecision(
                    task=resolved_task,
                    route="disabled",
                    rationale=("adaptive routing is disabled in the routing controls",),
                    source="adaptive_model_router",
                ),
                record=record,
            )
        if resolved_task.deterministic:
            return self._remember(
                RouteDecision(
                    task=resolved_task,
                    route="deterministic",
                    deterministic=True,
                    rationale=(
                        "the existing task estimator says a deterministic tool can carry this request",
                        "no model call is needed, so no provider tokens are spent",
                    ),
                ),
                record=record,
            )

        evaluated = tuple(self._evaluate(endpoint, resolved_task) for endpoint in self.endpoints())
        eligible = sorted(
            (candidate for candidate in evaluated if candidate.eligible),
            key=lambda candidate: (-candidate.score, candidate.endpoint.model, candidate.endpoint.provider_id),
        )
        if not eligible:
            reasons = tuple(
                reason
                for candidate in evaluated
                for reason in candidate.rejected_because
            )
            return self._remember(
                RouteDecision(
                    task=resolved_task,
                    route="none",
                    rationale=tuple(dict.fromkeys(reasons)) or ("no model endpoint is registered",),
                    candidates=evaluated,
                ),
                record=record,
            )
        winner = eligible[0]
        fallback = self._fallback_chain(winner, eligible[1:])
        rationale = list(winner.reasons)
        if resolved_task.local_only:
            rationale.append("privacy policy requires local execution; cloud candidates were rejected")
        if self._policy.active:
            bonus = self._policy.bonuses.get(resolved_task.category, {}).get(
                _policy_key(winner.endpoint.provider_id, winner.endpoint.model),
                self._policy.bonuses.get(resolved_task.category, {}).get(
                    winner.endpoint.model, 0.0
                ),
            )
            if bonus:
                rationale.append(f"learned policy v{self._policy.version} added {bonus:+.3f}")
        return self._remember(
            RouteDecision(
                task=resolved_task,
                route="local" if winner.endpoint.provider_id in self._local_provider_ids() else "cloud",
                model=winner.endpoint.model,
                provider_id=winner.endpoint.provider_id,
                provider_kind=self._provider_kind(winner.endpoint.provider_id),
                role=winner.endpoint.role.value,
                rationale=tuple(rationale),
                candidates=evaluated,
                fallback=fallback,
                policy=f"v{self._policy.version}" if self._policy.active else "evidence",
            ),
            record=record,
        )

    def _evaluate(self, endpoint: ModelEndpoint, task: TaskProfile) -> CandidateScore:
        record = self.providers.get(endpoint.provider_id)
        if record is None:
            return CandidateScore(endpoint, 0.0, False, rejected_because=("provider is not registered",))
        if not record.enabled:
            return CandidateScore(endpoint, 0.0, False, rejected_because=("provider is disabled",))
        if not record.configured:
            return CandidateScore(endpoint, 0.0, False, rejected_because=("provider is not configured",))
        if not self.providers.available(endpoint.provider_id):
            health = self.providers.health(endpoint.provider_id)
            remaining = max(0.0, health.cooldown_until - self._clock())
            return CandidateScore(
                endpoint,
                0.0,
                False,
                rejected_because=(
                    f"provider cooling down for {remaining:.1f}s after {health.last_error_kind or 'a failure'}",
                ),
            )
        if task.local_only and record.kind is not ProviderKind.LOCAL:
            return CandidateScore(endpoint, 0.0, False, rejected_because=("privacy policy requires local execution",))
        if not self.preferences.allow_cloud and record.kind is ProviderKind.CLOUD:
            return CandidateScore(endpoint, 0.0, False, rejected_because=("cloud providers are disabled",))
        missing = endpoint.profile.missing(task.capabilities)
        if missing:
            return CandidateScore(
                endpoint,
                0.0,
                False,
                rejected_because=(
                    "missing required capability: " + ", ".join(item.value for item in missing),
                ),
            )
        if (
            task.min_context_tokens
            and endpoint.context_length is not None
            and endpoint.context_length < task.min_context_tokens
        ):
            return CandidateScore(
                endpoint,
                0.0,
                False,
                rejected_because=(
                    f"context {endpoint.context_length} < required {task.min_context_tokens}",
                ),
            )
        if (
            self.preferences.max_context_tokens is not None
            and task.min_context_tokens > self.preferences.max_context_tokens
        ):
            return CandidateScore(
                endpoint,
                0.0,
                False,
                rejected_because=("request exceeds the configured context limit",),
            )
        cost = self.estimate_cost(endpoint, task)
        if (
            record.kind is ProviderKind.CLOUD
            and self.preferences.max_cost_per_request is not None
            and cost is None
        ):
            return CandidateScore(
                endpoint,
                0.0,
                False,
                rejected_because=("cost cap is set but this provider's price is unknown",),
            )
        if (
            cost is not None
            and self.preferences.max_cost_per_request is not None
            and cost > self.preferences.max_cost_per_request
        ):
            return CandidateScore(
                endpoint,
                0.0,
                False,
                rejected_because=(
                    f"estimated cost {cost:.6f} exceeds the cap {self.preferences.max_cost_per_request:.6f}",
                ),
            )
        hardware_reason = self._hardware_rejection(endpoint, record)
        if hardware_reason:
            return CandidateScore(endpoint, 0.0, False, rejected_because=(hardware_reason,))

        # A configured latency budget is a real control, not a display value:
        # when a measured latency is known and exceeds it, this candidate cannot
        # satisfy the request. An unmeasured latency is not treated as failing.
        if self.preferences.latency_budget_ms is not None:
            measured_latency = self._latency_for(endpoint)
            if (
                measured_latency is not None
                and measured_latency > self.preferences.latency_budget_ms
            ):
                return CandidateScore(
                    endpoint,
                    0.0,
                    False,
                    rejected_because=(
                        f"measured latency {measured_latency:.0f} ms exceeds the "
                        f"budget {self.preferences.latency_budget_ms:.0f} ms",
                    ),
                )

        score = 0.5
        reasons: list[str] = []
        if task.requires_coding and ModelCapability.CODING in endpoint.capabilities:
            score += 0.12
            reasons.append("declares the coding capability the task requires")
        if task.capabilities == tuple(sorted(BASELINE, key=lambda item: item.value)):
            if endpoint.role is ModelRole.LIGHTWEIGHT:
                score += 0.08
                reasons.append("lightweight role matches a simple task")
            if endpoint.parameters_b is not None and endpoint.parameters_b <= 4:
                score += 0.04
        if task.reasoning_depth == "high":
            if ModelCapability.REASONING in endpoint.capabilities:
                score += 0.1
                reasons.append("declares reasoning for a high-complexity task")
            if endpoint.parameters_b is not None:
                score += min(0.12, endpoint.parameters_b / 100.0)
        elif endpoint.parameters_b is not None and endpoint.parameters_b > 20:
            # A very large model is not a penalty, but for a simple task its
            # latency/cost is real; the measured figures below weigh it.
            score -= 0.02
        if task.category in {"code", "build"} and endpoint.role is ModelRole.CODING:
            score += 0.08
            reasons.append("role matches the coding task")
        if task.requires_vision and ModelCapability.VISION in endpoint.capabilities:
            score += 0.12
            reasons.append("declares vision and the request carries an image")
        elif not task.requires_vision and endpoint.role is ModelRole.VISION:
            # A multimodal model is not the automatic choice for text: several
            # local VLMs spend their budget on visual/reasoning tokens and can
            # return an empty text answer, measured on qwen3-vl:4b. It stays
            # eligible, just not preferred without an image.
            score -= 0.06
            reasons.append("vision-specialist role is not preferred for a text-only task")
        if task.requires_structured_output and endpoint.supports_structured_output:
            score += 0.08
            reasons.append("declares structured output")

        if endpoint.context_length is not None:
            if task.min_context_tokens and endpoint.context_length >= task.min_context_tokens * 4:
                score += 0.02
                reasons.append("context window comfortably exceeds the estimate")
        else:
            reasons.append(
                "context window unknown; accepted because the context need is an estimate, not a reported hard limit"
            )

        if task.latency_sensitivity == "high":
            latency = self._latency_for(endpoint)
            if latency is not None:
                score += 0.16 if latency <= 1000 else 0.08 if latency <= 5000 else -0.06
                reasons.append(f"measured provider latency {latency:.0f} ms")
            if record.kind is ProviderKind.LOCAL:
                score += 0.02
        elif task.latency_sensitivity == "quality":
            if endpoint.role in {ModelRole.REASONING, ModelRole.CODING}:
                score += 0.06
            if record.kind is ProviderKind.CLOUD:
                score += 0.01
        if cost is None:
            reasons.append("price unknown; treated as neutral, not free")
        if task.cost_sensitivity == "high":
            if record.kind is ProviderKind.LOCAL:
                score += 0.06
                reasons.append("local execution avoids a per-token provider charge")
            elif cost is not None:
                score += max(-0.08, 0.06 - min(0.08, cost * 100))
        elif cost is not None and cost == 0.0:
            score += 0.02
        if record.kind is ProviderKind.LOCAL and self.preferences.prefer_local:
            score += 0.03
        if self.preferences.preferred_model and endpoint.model == self.preferences.preferred_model:
            score += 0.1
            reasons.append("matches the configured preferred model")
        if self._policy.active:
            policy_scope = self._policy.bonuses.get(task.category, {})
            bonus = policy_scope.get(
                _policy_key(endpoint.provider_id, endpoint.model),
                policy_scope.get(endpoint.model, 0.0),
            )
            if bonus:
                score += bonus
                reasons.append(f"learned policy bonus {bonus:+.3f}")

        evidence = self._performance.get((task.category, endpoint.provider_id, endpoint.model))
        if evidence is not None and evidence.samples:
            success = evidence.success_rate or 0.0
            # Shrink toward 0.5 so one success does not outrank a whole fleet.
            shrink = evidence.samples / (evidence.samples + 3)
            score += (success - 0.5) * 0.2 * shrink
            if evidence.mean_latency_ms is not None:
                score -= min(0.06, evidence.mean_latency_ms / 60_000.0)
            reasons.append(
                f"{evidence.samples} observed run(s), success {success:.0%}, "
                f"mean {evidence.mean_latency_ms or 0:.0f} ms"
            )
        measured = self._benchmark_score(task.category, endpoint.model)
        if measured is not None:
            benchmark_success = measured.get("success_rate")
            if benchmark_success is not None:
                score += (float(benchmark_success) - 0.5) * 0.15
                reasons.append(
                    f"benchmark success {float(benchmark_success):.0%} "
                    f"over {measured.get('runs', 0)} run(s)"
                )
            median = measured.get("median_total_ms")
            if median is not None and task.latency_sensitivity == "high":
                score -= min(0.08, float(median) / 60_000.0)

        if not reasons:
            reasons.append("meets every declared requirement; ranking used available measurements")
        return CandidateScore(endpoint, score, True, tuple(reasons))

    def _hardware_rejection(self, endpoint: ModelEndpoint, record: ProviderRecord) -> str:
        if record.kind is not ProviderKind.LOCAL or self.monitor is None:
            return ""
        if endpoint.profile.memory_bytes is None:
            return ""
        try:
            available = self.monitor.available_ram_bytes()
            if available is None:
                return ""
            if endpoint.profile.memory_bytes > available:
                return (
                    f"needs {endpoint.profile.memory_bytes} bytes but only {available} are available"
                )
        except Exception:  # pragma: no cover - a readout must not block selection
            return ""
        return ""

    def _latency_for(self, endpoint: ModelEndpoint) -> float | None:
        if endpoint.latency_ms is not None:
            return endpoint.latency_ms
        health = self.providers.health(endpoint.provider_id)
        if health.mean_latency_ms is not None:
            return health.mean_latency_ms
        evidence = self._performance.get(("", endpoint.provider_id, endpoint.model))
        return evidence.mean_latency_ms if evidence is not None else None

    def _benchmark_score(self, category: str, model: str) -> dict[str, Any] | None:
        if self.benchmark is None:
            return None
        try:
            for score in self.benchmark.compare(category):
                if str(getattr(score, "model", "")) == model:
                    return dict(score.to_dict())
        except Exception:  # pragma: no cover - benchmark absence is not a routing error
            return None
        return None

    def estimate_cost(self, endpoint: ModelEndpoint, task: TaskProfile) -> float | None:
        """Estimated provider charge for one call, or ``None`` when unknown.

        Only per-million-token prices are known; input/output token counts are
        estimates from the existing task classifier. Local endpoints have a
        known provider charge of zero (the machine is already paid for). No
        figure is invented for a cloud model whose price this build does not have.
        """
        if endpoint.pricing_per_1m_input is None or endpoint.pricing_per_1m_output is None:
            return None
        return (
            (task.estimated_input_tokens / 1_000_000.0) * endpoint.pricing_per_1m_input
            + (task.estimated_output_tokens / 1_000_000.0) * endpoint.pricing_per_1m_output
        )

    @staticmethod
    def _fallback_chain(
        winner: CandidateScore, remaining: Sequence[CandidateScore]
    ) -> tuple[ModelEndpoint, ...]:
        """Up to three alternatives, preferring PROVIDER diversity.

        A fallback that stays inside the provider which just failed is not much
        of a fallback: a local runtime that is down takes all its models with
        it, and no local runtime takes all its models with it. Different-provider
        candidates therefore come first, and same-provider ones fill the rest
        only when the list is short.
        """
        winner_provider = winner.endpoint.provider_id
        other_providers = [
            candidate
            for candidate in remaining
            if candidate.endpoint.provider_id != winner_provider
        ]
        same_provider = [
            candidate
            for candidate in remaining
            if candidate.endpoint.provider_id == winner_provider
        ]
        ordered = [*other_providers, *same_provider]
        return tuple(candidate.endpoint for candidate in ordered[:3])

    def _local_provider_ids(self) -> set[str]:
        return {
            row.provider_id
            for row in self.providers.all()
            if row.kind is ProviderKind.LOCAL
        }

    def _provider_kind(self, provider_id: str) -> str:
        record = self.providers.get(provider_id)
        return record.kind.value if record is not None else ""

    def _local_only_default(self) -> bool:
        return False

    def _remember(self, decision: RouteDecision, *, record: bool) -> RouteDecision:
        if record:
            self._history.append(decision)
        return decision

    # -- execution and fallback ------------------------------------------

    async def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        text: str = "",
        intent: Any | None = None,
        task: TaskProfile | None = None,
        local_only: bool | None = None,
        timeout_seconds: float | None = None,
        **provider_kwargs: Any,
    ) -> RouteOutcome:
        """Route this request, call the chosen provider, and fall back safely.

        The decision and every attempt are returned. A caller can therefore
        tell a first-choice success from a compatible fallback, which is what
        makes the report honest instead of merely successful-looking.
        """
        decision = self.route(
            text or _messages_text(messages),
            intent=intent,
            task=task,
            local_only=local_only,
        )
        if decision.deterministic:
            # A deterministic answer belongs to the existing tool/handler path,
            # not to a model. Returning answered=True with empty text would tell
            # callers a model answered when no answer was produced at all.
            return RouteOutcome(
                decision=decision,
                answered=False,
                text="",
                error="deterministic request is handled outside the model router",
            )
        if not decision.found:
            return RouteOutcome(
                decision=decision,
                answered=False,
                error="no compatible model is available",
            )
        ordered: list[ModelEndpoint] = []
        selected = self._endpoints.get((decision.provider_id, decision.model))
        if selected is not None:
            ordered.append(selected)
        ordered.extend(decision.fallback)
        attempts: list[RouteAttempt] = []
        total_start = self._clock()
        allow_cloud = self.preferences.allow_cloud
        for index, endpoint in enumerate(ordered[:3]):
            record = self.providers.get(endpoint.provider_id)
            # Re-check the requirement at the moment of use, not only at
            # selection time: a provider can be disabled, cool down or lose its
            # hardware fit between the two.
            if record is None or not record.enabled:
                continue
            if record.kind is ProviderKind.CLOUD and not allow_cloud:
                continue
            if decision.task.local_only and record.kind is not ProviderKind.LOCAL:
                continue
            if not self.providers.available(endpoint.provider_id):
                continue
            adapter = self._adapter_for(endpoint)
            if adapter is None:
                attempts.append(
                    RouteAttempt(
                        provider_id=endpoint.provider_id,
                        model=endpoint.model,
                        success=False,
                        failure_kind=FailureKind.UNSUPPORTED_CAPABILITY.value,
                        error="no execution adapter is wired for this provider",
                    )
                )
                continue
            started = self._clock()
            queued = False
            acquired = False
            try:
                if self._inflight >= self.preferences.max_concurrency:
                    queued = True
                    self._queued += 1
                await asyncio.wait_for(
                    self._semaphore.acquire(),
                    timeout=(timeout_seconds or 30.0),
                )
                acquired = True
                self._inflight += 1
                if record.kind is ProviderKind.LOCAL:
                    try:
                        await asyncio.to_thread(self.model_manager.acquire, endpoint.model)
                    except Exception:
                        # The load/eviction layer already reports its refusals;
                        # a failure here means the adapter may still reach a
                        # model that is already resident, so it is not fatal.
                        pass
                    try:
                        self.model_manager.begin_activity(endpoint.model)
                    except Exception:
                        pass
                call = adapter.complete(list(messages), **provider_kwargs)
                if timeout_seconds is not None:
                    answer = await asyncio.wait_for(call, timeout=timeout_seconds)
                else:
                    answer = await call
                answer_text = str(answer or "")
                if not answer_text.strip():
                    raise ValueError("empty response from provider")
                latency = (self._clock() - started) * 1000.0
                self.providers.record_success(endpoint.provider_id, latency_ms=latency)
                usage = getattr(adapter, "usage", None)
                usage_dict = dict(usage) if isinstance(usage, Mapping) else {}
                self.record_observation(
                    endpoint,
                    decision.task,
                    success=True,
                    latency_ms=latency,
                    usage=usage_dict,
                    fallback=index > 0,
                    retries=len(attempts),
                )
                attempts.append(
                    RouteAttempt(
                        provider_id=endpoint.provider_id,
                        model=endpoint.model,
                        success=True,
                        latency_ms=latency,
                    )
                )
                return RouteOutcome(
                    decision=decision,
                    answered=True,
                    text=answer_text,
                    provider_id=endpoint.provider_id,
                    model=endpoint.model,
                    attempts=tuple(attempts),
                    fallback_used=index > 0,
                    latency_ms=(self._clock() - total_start) * 1000.0,
                    usage=usage_dict,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                kind = classify_failure(exc)
                latency = (self._clock() - started) * 1000.0
                self.providers.record_failure(
                    endpoint.provider_id,
                    kind,
                    error=f"{type(exc).__name__}: {exc}",
                )
                self.record_observation(
                    endpoint,
                    decision.task,
                    success=False,
                    latency_ms=latency,
                    failure_kind=kind.value,
                    fallback=index > 0,
                    retries=len(attempts),
                )
                attempts.append(
                    RouteAttempt(
                        provider_id=endpoint.provider_id,
                        model=endpoint.model,
                        success=False,
                        failure_kind=kind.value,
                        error=f"{type(exc).__name__}: {exc}",
                        latency_ms=latency,
                    )
                )
                if not failure_is_retryable(kind):
                    return RouteOutcome(
                        decision=decision,
                        answered=False,
                        attempts=tuple(attempts),
                        fallback_used=index > 0,
                        latency_ms=(self._clock() - total_start) * 1000.0,
                        error=(
                            f"{endpoint.provider_id}/{endpoint.model} failed with "
                            f"{kind.value}, which is not retryable: {exc}"
                        ),
                    )
            finally:
                if acquired:
                    self._inflight -= 1
                    self._semaphore.release()
                if queued:
                    self._queued = max(0, self._queued - 1)
                if record is not None and record.kind is ProviderKind.LOCAL:
                    try:
                        self.model_manager.release_after_use(endpoint.model)
                    except Exception:
                        pass
        return RouteOutcome(
            decision=decision,
            answered=False,
            attempts=tuple(attempts),
            fallback_used=len(attempts) > 1,
            latency_ms=(self._clock() - total_start) * 1000.0,
            error="every compatible model failed; no unsuitable fallback was attempted",
        )

    def _adapter_for(self, endpoint: ModelEndpoint) -> Any | None:
        adapter = self._adapters.get(endpoint.provider_id)
        if adapter is not None:
            return adapter
        if self._adapter_resolver is None:
            return None
        try:
            resolved = self._adapter_resolver(endpoint)
        except Exception:  # pragma: no cover - a factory failure is a routing failure
            return None
        return resolved

    # -- observations and learning ---------------------------------------

    def record_observation(
        self,
        endpoint: ModelEndpoint | str,
        task: TaskProfile,
        *,
        success: bool,
        latency_ms: float,
        failure_kind: str = "",
        usage: Mapping[str, Any] | None = None,
        fallback: bool = False,
        retries: int = 0,
    ) -> dict[str, Any]:
        if isinstance(endpoint, str):
            matches = [row for row in self._endpoints.values() if row.model == endpoint]
            if not matches:  # pragma: no cover - defensive call path
                raise ValueError(f"unknown model endpoint: {endpoint!r}")
            resolved = matches[0]
        else:
            resolved = endpoint
        category = task.category or "chat"
        key = (category, resolved.provider_id, resolved.model)
        stats = self._performance.setdefault(key, _Performance())
        if success:
            stats.successes += 1
        else:
            stats.failures += 1
        stats.total_latency_ms += max(0.0, float(latency_ms))
        usage_map = dict(usage or {})
        observation = RoutingObservation(
            provider_id=resolved.provider_id,
            model=resolved.model,
            category=category,
            success=bool(success),
            latency_ms=max(0.0, float(latency_ms)),
            failure_kind=str(failure_kind or ""),
            prompt_tokens=int(usage_map.get("prompt_tokens") or 0),
            completion_tokens=int(usage_map.get("completion_tokens") or 0),
            fallback=bool(fallback),
            retries=int(retries),
            at=_now_iso(),
        )
        self._observations.append(observation)
        self._emit_learning(observation)
        return observation.to_dict()

    def _emit_learning(self, observation: RoutingObservation) -> None:
        """Hand the observation to Phase 27 as evidence, without a second engine.

        A provider's answer is evidence of provider reliability, not proof the
        user's task succeeded, so the verification dict says exactly that. An
        unavailable/disabled learning engine simply does not record it.
        """
        learning = self.learning
        if learning is None or not getattr(learning, "enabled", False):
            return
        try:
            evidence = learning.capture(
                observation.category,
                task_kind=observation.category,
                source="model_router",
                route="model",
                context={
                    "model": observation.model,
                    "provider": observation.provider_id,
                    "failure_kind": observation.failure_kind,
                },
                tools=(observation.model,),
                verification={
                    "provider_responded": observation.success,
                    "verified": False,
                    "source": "adaptive_model_router",
                },
                errors=() if observation.success else (observation.failure_kind,),
                latency_ms=observation.latency_ms,
                retries=observation.retries,
            )
            learning.record(evidence)
        except Exception:
            return

    def performance(self) -> dict[str, Any]:
        rows = []
        for (category, provider_id, model), stats in sorted(self._performance.items()):
            rows.append(
                {
                    "category": category,
                    "provider_id": provider_id,
                    "model": model,
                    "runs": stats.samples,
                    "successes": stats.successes,
                    "failures": stats.failures,
                    "success_rate": _round(stats.success_rate, 3),
                    "mean_latency_ms": _round(stats.mean_latency_ms),
                }
            )
        return {"rows": rows, "observations": len(self._observations)}

    def propose_policy(self) -> dict[str, Any]:
        """Propose bonuses only where the evidence has enough independent runs."""
        by_category: dict[str, dict[str, tuple[int, int, float]]] = {}
        for (category, provider_id, model), stats in self._performance.items():
            if stats.samples < MIN_POLICY_SUPPORT:
                continue
            # Provider/model, not model alone: the same name on two providers
            # is two candidates with two measured histories.
            by_category.setdefault(category, {})[_policy_key(provider_id, model)] = (
                stats.successes,
                stats.samples,
                stats.total_latency_ms / max(1, stats.samples),
            )
        bonuses: dict[str, dict[str, float]] = {}
        baseline: dict[str, float] = {}
        expected: dict[str, float] = {}
        support: dict[str, int] = {}
        for category, rows in by_category.items():
            if len(rows) < 2:
                # A policy that only says "use the one measured model" is not a
                # learned routing policy; it is a default with a citation.
                continue
            rates = {
                model: (successes + 1) / (samples + 2)
                for model, (successes, samples, _latency) in rows.items()
            }
            unweighted = sum(rates.values()) / len(rates)
            best_prior = max(rates.values())
            category_support = sum(samples for _successes, samples, _latency in rows.values())
            support[category] = category_support
            baseline[category] = unweighted
            expected[category] = max(rates.values())
            bonuses[category] = {
                model: round((rate - unweighted) * 0.35, 4)
                for model, rate in rates.items()
                if abs(rate - unweighted) >= 0.01
            }
        eligible = bool(bonuses) and any(
            expected[category] - baseline[category] >= MIN_POLICY_IMPROVEMENT
            for category in bonuses
        )
        return {
            "eligible": eligible,
            "reason": (
                "enough observations in at least one category for a bounded policy"
                if eligible
                else "not enough independent multi-model evidence to improve routing"
            ),
            "min_support": MIN_POLICY_SUPPORT,
            "min_improvement": MIN_POLICY_IMPROVEMENT,
            "bonuses": bonuses,
            "support": support,
            "baseline_success": {key: _round(value, 3) for key, value in baseline.items()},
            "expected_success": {key: _round(value, 3) for key, value in expected.items()},
        }

    def activate_policy(self, *, expected: Mapping[str, Any] | None = None) -> dict[str, Any]:
        proposal = dict(expected or self.propose_policy())
        if not proposal.get("eligible"):
            raise ValueError(f"routing policy is not validated: {proposal.get('reason', '')}")
        previous = self._policy
        # Snapshot even v0 (inactive): rollback of the FIRST learned policy must
        # restore the evidence-only baseline, not fail for lack of history.
        self._policy_history.append(
            RoutingPolicy(
                version=previous.version,
                active=previous.active,
                bonuses={key: dict(value) for key, value in previous.bonuses.items()},
                support=dict(previous.support),
                baseline_success=dict(previous.baseline_success),
                expected_success=dict(previous.expected_success),
                created_at=previous.created_at,
                reason=previous.reason,
            )
        )
        policy = RoutingPolicy(
            version=previous.version + 1,
            active=True,
            bonuses={key: dict(value) for key, value in dict(proposal.get("bonuses") or {}).items()},
            support={key: int(value) for key, value in dict(proposal.get("support") or {}).items()},
            baseline_success={
                key: float(value) for key, value in dict(proposal.get("baseline_success") or {}).items()
            },
            expected_success={
                key: float(value) for key, value in dict(proposal.get("expected_success") or {}).items()
            },
            created_at=_now_iso(),
            reason=str(proposal.get("reason") or ""),
        )
        self._policy = policy
        self._persist_policy()
        return policy.to_dict()

    def rollback_policy(self) -> dict[str, Any]:
        if not self._policy_history:
            raise ValueError("no previous routing policy is available to roll back to")
        restored = self._policy_history.pop()
        self._policy = restored
        self._persist_policy()
        return {"rolled_back": True, "policy": restored.to_dict(), "remaining": len(self._policy_history)}

    def _persist_policy(self) -> None:
        if self._store is None:
            return
        try:
            self._store.write("model_routing_policy", self._policy.to_dict())
        except Exception:  # pragma: no cover - persistence must not fail routing
            return

    def _load_policy(self) -> None:
        if self._store is None:
            return
        try:
            data = self._store.read("model_routing_policy")
        except Exception:  # pragma: no cover - an unreadable store keeps defaults
            return
        if not isinstance(data, Mapping) or not data:
            return
        try:
            bonuses = {
                str(category): {str(model): float(value) for model, value in dict(rows).items()}
                for category, rows in dict(data.get("bonuses") or {}).items()
                if isinstance(rows, Mapping)
            }
            self._policy = RoutingPolicy(
                version=max(0, int(data.get("version", 0) or 0)),
                active=bool(data.get("active", False)),
                bonuses=bonuses,
                support={str(k): int(v) for k, v in dict(data.get("support") or {}).items()},
                baseline_success={
                    str(k): float(v) for k, v in dict(data.get("baseline_success") or {}).items()
                },
                expected_success={
                    str(k): float(v) for k, v in dict(data.get("expected_success") or {}).items()
                },
                created_at=str(data.get("created_at") or ""),
                reason=str(data.get("reason") or ""),
            )
        except (TypeError, ValueError):
            return


def _messages_text(messages: Sequence[Mapping[str, str]]) -> str:
    """The last user message's text, for classification when no text is supplied."""
    for message in reversed(list(messages)):
        if str(message.get("role", "")).lower() == "user":
            return str(message.get("content", ""))
    return ""


def _family_from_profile(profile: ModelProfile) -> str:
    text = profile.name.lower()
    for family in (
        "llama",
        "qwen",
        "mistral",
        "gemma",
        "phi",
        "deepseek",
        "claude",
        "gpt",
        "gemini",
        "mixtral",
        "granite",
        "command-r",
        "nomic",
    ):
        if family in text:
            return family
    return ""


def _optional_non_negative_float(value: Any, *, key: str) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number") from exc
    if number < 0:
        raise ValueError(f"{key} must not be negative")
    return number


def _optional_positive_int(value: Any, *, key: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if number <= 0:
        raise ValueError(f"{key} must be positive")
    return number


__all__ = [
    "DEFAULT_HISTORY",
    "DEFAULT_OBSERVATIONS",
    "MIN_POLICY_IMPROVEMENT",
    "MIN_POLICY_SUPPORT",
    "AdaptiveModelRouter",
    "AdaptiveTaskClassifier",
    "CandidateScore",
    "FailureKind",
    "ModelEndpoint",
    "ModelRole",
    "ProviderHealth",
    "ProviderKind",
    "ProviderRecord",
    "ProviderRegistry",
    "RouteAttempt",
    "RouteDecision",
    "RouteOutcome",
    "RoutingObservation",
    "RoutingPolicy",
    "RoutingPreferences",
    "TaskProfile",
    "classify_failure",
    "failure_is_retryable",
]
