"""Resource and provider controls for Phase 28 collaboration.

These helpers answer four questions the collaboration engine must ask *before*
or *around* delegating work:

* may the configured completion provider do what a subtask requires?
* how many tokens has this collaboration actually consumed, and is there room
  left in its budget?
* how long should an idempotent rate-limited subtask wait before retrying?
* how much concurrency does this machine's current resource reading support?

They are deliberately pure / injectable: the engine owns execution and this
module owns only the arithmetic and the honest wording of a refusal. Nothing
here estimates a token count from prose or invents a hardware figure.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

MB = 1024 * 1024

_CAPABILITY_ALIASES = {
    "text": "text_generation",
    "text_generation": "text_generation",
    "json": "structured_output",
    "json_mode": "structured_output",
    "structured": "structured_output",
    "structured_output": "structured_output",
    "tools": "tool_calling",
    "tool": "tool_calling",
    "tool_calling": "tool_calling",
    "function_calling": "tool_calling",
    "vision": "vision",
    "images": "vision",
    "image": "vision",
    "stream": "streaming",
    "streaming": "streaming",
}

_RETRY_AFTER = re.compile(
    r"\bretry[-_ ]after\b\s*[:=]?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_LIMIT_WORDS = re.compile(
    r"\b(?:429|rate[ -]?limit(?:ed|ing)?|too many requests|quota(?: exceeded)?)\b",
    re.IGNORECASE,
)

_TOKEN_KEYS = (
    "prompt_tokens",
    "input_tokens",
    "completion_tokens",
    "output_tokens",
    "total_tokens",
    "tokens",
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None or number < 0 or number != number:  # NaN
        return None
    return int(number)


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """What a completion provider says it can do — never what it might do.

    A missing capability is ``None``/false, not a guess. A caller that requires
    a context window therefore fails honestly when the provider does not
    declare one instead of assuming the largest plausible value.
    """

    name: str = ""
    model: str = ""
    available: bool = False
    text_generation: bool = True
    structured_output: bool = False
    tool_calling: bool = False
    vision: bool = False
    streaming: bool = False
    context_window: int | None = None
    max_output_tokens: int | None = None
    rate_limit_requests_per_minute: int | None = None
    source: str = ""
    reason: str = ""

    def supports(self, capability: str) -> bool:
        key = _CAPABILITY_ALIASES.get(str(capability or "").strip().lower(), "")
        if key == "text_generation":
            return self.text_generation
        if key == "structured_output":
            return self.structured_output
        if key == "tool_calling":
            return self.tool_calling
        if key == "vision":
            return self.vision
        if key == "streaming":
            return self.streaming
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "available": self.available,
            "text_generation": self.text_generation,
            "structured_output": self.structured_output,
            "tool_calling": self.tool_calling,
            "vision": self.vision,
            "streaming": self.streaming,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "rate_limit_requests_per_minute": self.rate_limit_requests_per_minute,
            "source": self.source,
            "reason": self.reason,
        }

    @classmethod
    def from_provider(cls, provider: object | None) -> "ProviderCapabilities":
        if provider is None:
            return cls(
                available=False,
                source="none",
                reason="no completion provider is configured",
            )
        raw: dict[str, Any] = {}
        try:
            declared = getattr(provider, "capabilities", None)
            if callable(declared):
                declared = declared()
            raw = _mapping(declared)
        except Exception:
            raw = {}
        name = str(getattr(provider, "name", "") or "")
        model = str(getattr(provider, "model", "") or "")
        complete = getattr(provider, "complete", None)
        available = bool(callable(complete)) and bool(
            getattr(provider, "available", True)
        )
        if "available" in raw:
            available = bool(raw.get("available")) and available
        reason = str(raw.get("reason", "") or "")
        if not available and not reason:
            reason = "the provider is unavailable or has no complete() method"
        text_generation = bool(
            raw.get("text_generation", raw.get("text", callable(complete)))
        )
        return cls(
            name=name or type(provider).__name__,
            model=model,
            available=available,
            text_generation=text_generation,
            structured_output=bool(
                raw.get(
                    "structured_output",
                    raw.get("structured", raw.get("json_mode", False)),
                )
            ),
            tool_calling=bool(
                raw.get("tool_calling", raw.get("tools", raw.get("function_calling", False)))
            ),
            vision=bool(raw.get("vision", raw.get("images", False))),
            streaming=bool(raw.get("streaming", raw.get("stream", False))),
            context_window=_integer(
                raw.get("context_window", raw.get("max_context_tokens"))
            )
            or _integer(getattr(provider, "context_window", None))
            or _integer(getattr(provider, "max_context_tokens", None)),
            max_output_tokens=_integer(
                raw.get("max_output_tokens", raw.get("max_tokens"))
            )
            or _integer(getattr(provider, "max_output_tokens", None)),
            rate_limit_requests_per_minute=_integer(
                raw.get(
                    "rate_limit_requests_per_minute",
                    raw.get("requests_per_minute"),
                )
            )
            or _integer(getattr(provider, "rate_limit_requests_per_minute", None)),
            source="provider.capabilities" if raw else "provider attributes",
            reason=reason,
        )


@dataclass(frozen=True, slots=True)
class ProviderPreflight:
    """The result of checking one subtask's declared provider requirements."""

    subtask_id: str
    ok: bool
    required: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    provider: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subtask_id": self.subtask_id,
            "ok": self.ok,
            "required": list(self.required),
            "missing": list(self.missing),
            "reasons": list(self.reasons),
            "provider": dict(self.provider),
        }


class ProviderPreflightChecker:
    """Check declared requirements against the live provider's declarations."""

    def __init__(
        self,
        provider: object | None = None,
        *,
        capabilities: ProviderCapabilities | None = None,
        provider_getter: Callable[[], object | None] | None = None,
    ) -> None:
        self._provider = provider
        self._provider_getter = provider_getter
        self._capabilities = capabilities

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Read the provider again so a hot-swap cannot leave stale promises."""
        if self._capabilities is not None:
            return self._capabilities
        provider = (
            self._provider_getter()
            if self._provider_getter is not None
            else self._provider
        )
        return ProviderCapabilities.from_provider(provider)

    def describe(self) -> dict[str, Any]:
        return self.capabilities.to_dict()

    def check(
        self,
        requirements: Mapping[str, Any] | None,
        *,
        subtask_id: str = "",
    ) -> ProviderPreflight:
        requirements = _mapping(requirements)
        required_raw = requirements.get(
            "required_capabilities", requirements.get("capabilities", ())
        )
        required: tuple[str, ...]
        if isinstance(required_raw, str):
            required = (required_raw,)
        elif isinstance(required_raw, Sequence) and not isinstance(
            required_raw, (bytes, bytearray)
        ):
            required = tuple(str(item) for item in required_raw if str(item).strip())
        else:
            required = ()
        require_provider = bool(
            requirements.get("require_provider", bool(required))
        )
        minimum_context = _integer(
            requirements.get(
                "min_context_tokens", requirements.get("context_tokens")
            )
        )
        maximum_output = _integer(requirements.get("max_output_tokens"))
        missing: list[str] = []
        reasons: list[str] = []
        capabilities = self.capabilities

        if require_provider and not capabilities.available:
            missing.append("provider")
            reasons.append(capabilities.reason or "no available completion provider")
        for capability in required:
            if not capabilities.supports(capability):
                missing.append(capability)
                reasons.append(
                    f"the provider does not declare the {capability!r} capability"
                )
        if minimum_context:
            if capabilities.context_window is None:
                missing.append("context_window")
                reasons.append(
                    "the provider does not declare a context window, so the "
                    f"required {minimum_context} tokens cannot be verified"
                )
            elif capabilities.context_window < minimum_context:
                missing.append("context_window")
                reasons.append(
                    f"the provider's context window is {capabilities.context_window} "
                    f"tokens, below the required {minimum_context}"
                )
        if maximum_output:
            if capabilities.max_output_tokens is None:
                missing.append("max_output_tokens")
                reasons.append(
                    "the provider does not declare an output-token ceiling"
                )
            elif capabilities.max_output_tokens < maximum_output:
                missing.append("max_output_tokens")
                reasons.append(
                    f"the provider's output ceiling is "
                    f"{capabilities.max_output_tokens} tokens, below the required "
                    f"{maximum_output}"
                )
        # Keep one entry per missing requirement while preserving declaration order.
        unique_missing = tuple(dict.fromkeys(missing))
        return ProviderPreflight(
            subtask_id=subtask_id,
            ok=not unique_missing,
            required=required,
            missing=unique_missing,
            reasons=tuple(reasons),
            provider=capabilities.to_dict(),
        )

    def check_plan(self, plan: Any) -> tuple[ProviderPreflight, ...]:
        """Check every subtask that declared requirements, in plan order."""
        reports: list[ProviderPreflight] = []
        for spec in getattr(plan, "subtasks", ()):
            metadata = _mapping(getattr(spec, "metadata", {}))
            requirements = metadata.get(
                "provider_requirements", metadata.get("provider_capabilities")
            )
            if requirements:
                reports.append(
                    self.check(requirements, subtask_id=str(getattr(spec, "id", "")))
                )
        return tuple(reports)


def normalize_token_usage(usage: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Normalize the token fields providers actually report.

    ``total_tokens`` wins when present; otherwise prompt + completion is used.
    A ``tokens`` shorthand is accepted for agents that report only a total. A
    mapping with no token field returns ``None`` — zero would claim a free call.
    """
    if not isinstance(usage, Mapping):
        return None
    lower = {str(key).lower(): value for key, value in usage.items()}
    prompt = _integer(lower.get("prompt_tokens", lower.get("input_tokens")))
    completion = _integer(
        lower.get("completion_tokens", lower.get("output_tokens"))
    )
    total = _integer(lower.get("total_tokens"))
    shorthand = _integer(lower.get("tokens"))
    if total is None:
        if prompt is not None or completion is not None:
            total = (prompt or 0) + (completion or 0)
        elif shorthand is not None:
            total = shorthand
    if total is None and prompt is None and completion is None:
        return None
    # A reported total below its own parts undercounts the call. Keep the
    # larger figure and say why rather than silently trusting bad arithmetic.
    computed = (prompt or 0) + (completion or 0)
    consistent = total is None or total >= computed
    if total is None or total < computed:
        total = computed
    if shorthand is not None and total is not None and shorthand > total:
        total = shorthand
    return {
        "prompt_tokens": int(prompt or 0),
        "completion_tokens": int(completion or 0),
        "total_tokens": int(total or 0),
        "consistent": consistent,
        "reported": True,
    }


@dataclass(slots=True)
class TokenLedger:
    """One collaboration's central token accounting."""

    limit: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    reported_calls: int = 0
    inconsistent_reports: int = 0
    by_agent: dict[str, dict[str, int]] = field(default_factory=dict)
    by_subtask: dict[str, dict[str, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.limit = max(0, int(self.limit))
        self.prompt_tokens = max(0, int(self.prompt_tokens))
        self.completion_tokens = max(0, int(self.completion_tokens))
        self.total_tokens = max(0, int(self.total_tokens))
        self.calls = max(0, int(self.calls))
        self.reported_calls = max(0, int(self.reported_calls))
        self.inconsistent_reports = max(0, int(self.inconsistent_reports))

    @property
    def exhausted(self) -> bool:
        return self.limit > 0 and self.total_tokens >= self.limit

    @property
    def remaining(self) -> int | None:
        return None if self.limit <= 0 else max(0, self.limit - self.total_tokens)

    @property
    def over_budget(self) -> bool:
        return self.limit > 0 and self.total_tokens > self.limit

    def _bucket(self, ledger: dict[str, dict[str, int]], key: str) -> dict[str, int]:
        return ledger.setdefault(
            key,
            {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        )

    def account(
        self,
        usage: Mapping[str, Any] | None,
        *,
        agent: str = "",
        subtask_id: str = "",
    ) -> dict[str, Any]:
        normalized = normalize_token_usage(usage)
        self.calls += 1
        if normalized is None:
            return {"accounted": False, "reason": "the agent reported no token usage"}
        self.reported_calls += 1
        if not normalized["consistent"]:
            self.inconsistent_reports += 1
        self.prompt_tokens += normalized["prompt_tokens"]
        self.completion_tokens += normalized["completion_tokens"]
        self.total_tokens += normalized["total_tokens"]
        for ledger, key in ((self.by_agent, agent), (self.by_subtask, subtask_id)):
            if not key:
                continue
            bucket = self._bucket(ledger, key)
            bucket["calls"] += 1
            bucket["prompt_tokens"] += normalized["prompt_tokens"]
            bucket["completion_tokens"] += normalized["completion_tokens"]
            bucket["total_tokens"] += normalized["total_tokens"]
        return {
            "accounted": True,
            "prompt_tokens": normalized["prompt_tokens"],
            "completion_tokens": normalized["completion_tokens"],
            "total_tokens": normalized["total_tokens"],
            "exhausted": self.exhausted,
            "remaining": self.remaining,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget": self.limit,
            "limit": self.limit,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "reported_calls": self.reported_calls,
            "inconsistent_reports": self.inconsistent_reports,
            "remaining": self.remaining,
            "exhausted": self.exhausted,
            "over_budget": self.over_budget,
            "by_agent": {
                key: dict(value) for key, value in sorted(self.by_agent.items())
            },
            "by_subtask": {
                key: dict(value) for key, value in sorted(self.by_subtask.items())
            },
        }

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any] | None, *, limit: int | None = None
    ) -> "TokenLedger":
        data = _mapping(data)
        # An explicit live-policy limit wins (including 0 = unbounded); only a
        # caller that passes none inherits the stored ledger's limit.
        stored_limit = _integer(data.get("limit", data.get("budget")))
        effective_limit = int(limit) if limit is not None else (stored_limit or 0)
        return cls(
            limit=effective_limit,
            prompt_tokens=_integer(data.get("prompt_tokens")) or 0,
            completion_tokens=_integer(data.get("completion_tokens")) or 0,
            total_tokens=_integer(data.get("total_tokens")) or 0,
            calls=_integer(data.get("calls")) or 0,
            reported_calls=_integer(data.get("reported_calls")) or 0,
            inconsistent_reports=_integer(data.get("inconsistent_reports")) or 0,
            by_agent={
                str(key): {
                    "calls": _integer(value.get("calls")) or 0,
                    "prompt_tokens": _integer(value.get("prompt_tokens")) or 0,
                    "completion_tokens": _integer(value.get("completion_tokens")) or 0,
                    "total_tokens": _integer(value.get("total_tokens")) or 0,
                }
                for key, value in _mapping(data.get("by_agent")).items()
                if isinstance(value, Mapping)
            },
            by_subtask={
                str(key): {
                    "calls": _integer(value.get("calls")) or 0,
                    "prompt_tokens": _integer(value.get("prompt_tokens")) or 0,
                    "completion_tokens": _integer(value.get("completion_tokens")) or 0,
                    "total_tokens": _integer(value.get("total_tokens")) or 0,
                }
                for key, value in _mapping(data.get("by_subtask")).items()
                if isinstance(value, Mapping)
            },
        )


def looks_rate_limited(text: str) -> bool:
    return bool(_LIMIT_WORDS.search(str(text or "")))


def retry_after_seconds(text: str) -> float | None:
    match = _RETRY_AFTER.search(str(text or ""))
    if not match:
        return None
    return _number(match.group(1))


@dataclass(frozen=True, slots=True)
class BackoffDecision:
    delay_seconds: float
    attempt: int
    rate_limited: bool
    source: str
    retry_after: float | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "delay_seconds": self.delay_seconds,
            "attempt": self.attempt,
            "rate_limited": self.rate_limited,
            "source": self.source,
            "retry_after": self.retry_after,
            "reason": self.reason,
        }


def backoff_decision(
    error: str,
    *,
    attempt: int,
    base_seconds: float,
    maximum_seconds: float,
) -> BackoffDecision:
    """Honor Retry-After when present, otherwise use bounded exponential delay."""
    attempt = max(1, int(attempt))
    base = max(0.0, float(base_seconds))
    maximum = max(0.0, float(maximum_seconds))
    retry_after = retry_after_seconds(error)
    if retry_after is not None:
        delay = min(max(0.0, retry_after), maximum)
        return BackoffDecision(
            delay_seconds=round(delay, 3),
            attempt=attempt,
            rate_limited=True,
            source="retry_after",
            retry_after=retry_after,
            reason="the provider supplied a Retry-After delay",
        )
    delay = min(base * (2 ** (attempt - 1)), maximum)
    return BackoffDecision(
        delay_seconds=round(delay, 3),
        attempt=attempt,
        rate_limited=looks_rate_limited(error),
        source="exponential",
        reason=(
            "rate-limited; exponential backoff"
            if looks_rate_limited(error)
            else "no Retry-After was supplied; exponential backoff"
        ),
    )


@dataclass(frozen=True, slots=True)
class HardwareSchedule:
    """The concurrency this machine can support right now."""

    requested_concurrency: int
    effective_concurrency: int
    level: str
    source: str
    available_ram_bytes: int | None = None
    total_ram_bytes: int | None = None
    cpu_percent: float | None = None
    gpu_utilization: float | None = None
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_concurrency": self.requested_concurrency,
            "effective_concurrency": self.effective_concurrency,
            "level": self.level,
            "source": self.source,
            "available_ram_bytes": self.available_ram_bytes,
            "total_ram_bytes": self.total_ram_bytes,
            "cpu_percent": self.cpu_percent,
            "gpu_utilization": self.gpu_utilization,
            "reasons": list(self.reasons),
        }


def _effective_concurrency(level: str, requested: int, sequential_fallback: bool) -> int:
    requested = max(1, int(requested))
    if level == "critical":
        return 1 if sequential_fallback else max(1, requested // 2)
    if level == "tight":
        return min(requested, 2)
    return requested


def schedule_hardware(
    monitor: object | None,
    *,
    requested_concurrency: int,
    hardware_aware: bool = True,
    sequential_fallback: bool = True,
) -> HardwareSchedule:
    """Read the machine, then cap concurrency without inventing a measurement.

    A governor with ``assess()`` is preferred because it already owns the
    configured thresholds. A bare ``HardwareMonitor`` is read through its
    documented cheap probes. No reading means *no reduction* and an explicit
    ``unmeasured`` level, never a confident zero.
    """
    requested = max(1, int(requested_concurrency))
    if not hardware_aware:
        return HardwareSchedule(
            requested_concurrency=requested,
            effective_concurrency=requested,
            level="disabled",
            source="configuration",
            reasons=("hardware-aware scheduling is disabled",),
        )
    if monitor is None:
        return HardwareSchedule(
            requested_concurrency=requested,
            effective_concurrency=requested,
            level="unmeasured",
            source="none",
            reasons=("no hardware monitor is configured",),
        )
    assessment: Any = None
    assess = getattr(monitor, "assess", None)
    if callable(assess):
        try:
            assessment = assess()
        except Exception as exc:
            return HardwareSchedule(
                requested_concurrency=requested,
                effective_concurrency=requested,
                level="unmeasured",
                source="governor",
                reasons=(f"hardware assessment failed: {type(exc).__name__}: {exc}",),
            )
    if assessment is not None:
        level = str(getattr(assessment, "level", "") or "").lower()
        if level not in {"ample", "tight", "critical"}:
            level = "unmeasured"
        available = getattr(assessment, "available_ram_bytes", None)
        total = getattr(assessment, "total_ram_bytes", None)
        cpu = getattr(assessment, "cpu_percent", None)
        gpu = getattr(assessment, "gpu_utilization", None)
        assessment_reasons = tuple(
            str(item) for item in getattr(assessment, "reasons", ()) or ()
        )
        return HardwareSchedule(
            requested_concurrency=requested,
            effective_concurrency=_effective_concurrency(
                level, requested, sequential_fallback
            ),
            level=level,
            source="resource_governor",
            available_ram_bytes=available if isinstance(available, int) else None,
            total_ram_bytes=total if isinstance(total, int) else None,
            cpu_percent=float(cpu) if isinstance(cpu, (int, float)) else None,
            gpu_utilization=float(gpu) if isinstance(gpu, (int, float)) else None,
            reasons=assessment_reasons,
        )

    def read(name: str) -> Any:
        getter = getattr(monitor, name, None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:
            return None

    available = read("available_ram_bytes")
    total = read("total_ram_bytes")
    cpu = read("cpu_percent")
    gpu_reading = read("gpu")
    gpu = (
        gpu_reading.get("percent")
        if isinstance(gpu_reading, Mapping) and gpu_reading.get("available")
        else None
    )
    level = "ample"
    measured = False
    reasons: list[str] = []
    if isinstance(available, int):
        measured = True
        if available < 1536 * MB:
            level = "critical"
            reasons.append(
                f"free RAM is {available / MB:.0f} MB, below the critical line"
            )
        elif available < 4096 * MB:
            level = "tight"
            reasons.append(
                f"free RAM is {available / MB:.0f} MB, below the comfort line"
            )
    else:
        reasons.append("free memory could not be measured")
    if isinstance(cpu, (int, float)):
        measured = True
        if cpu >= 90.0:
            if level == "ample":
                level = "tight"
            reasons.append(f"CPU utilization is {cpu:.0f}%")
    if isinstance(gpu, (int, float)):
        measured = True
        if gpu >= 95.0 and level == "ample":
            level = "tight"
            reasons.append(f"GPU utilization is {gpu:.0f}%")
    if not measured:
        level = "unmeasured"
        reasons = ["no hardware reading was available"]
    elif not reasons:
        reasons.append("hardware readings are within the comfort lines")
    return HardwareSchedule(
        requested_concurrency=requested,
        effective_concurrency=_effective_concurrency(
            level, requested, sequential_fallback
        ),
        level=level,
        source="hardware_monitor",
        available_ram_bytes=available if isinstance(available, int) else None,
        total_ram_bytes=total if isinstance(total, int) else None,
        cpu_percent=float(cpu) if isinstance(cpu, (int, float)) else None,
        gpu_utilization=float(gpu) if isinstance(gpu, (int, float)) else None,
        reasons=tuple(reasons),
    )


__all__ = [
    "BackoffDecision",
    "HardwareSchedule",
    "ProviderCapabilities",
    "ProviderPreflight",
    "ProviderPreflightChecker",
    "TokenLedger",
    "backoff_decision",
    "looks_rate_limited",
    "normalize_token_usage",
    "retry_after_seconds",
    "schedule_hardware",
]
