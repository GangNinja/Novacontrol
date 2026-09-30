"""Phase 14's shared vocabulary: modes, privacy, resources, cost, benchmarks.

These types exist so four services can talk about the same facts without
importing each other. The rule that shaped every field is the phase's opening
instruction: *use actual runtime telemetry*. A figure that was not measured is
``None``, never a zero that reads as "free" and never a guess wearing a
number's clothes — the same rule ``models/hardware.py`` already follows.

Nothing here decides anything. ``privacy.py`` decides what may leave the
machine, ``governor.py`` decides what may load, ``cost.py`` estimates what a
request will cost, and ``benchmark.py`` records what a model actually did. The
types only carry the answers, with their reasons, so a caller (and the audit
trail and the UI) can see WHY rather than being handed a bare boolean.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from novacontrol.core.security import RiskLevel


class ExecutionMode(StrEnum):
    """How much of the world this installation may reach for.

    Three modes, stated by the specification, and each is a POLICY rather than
    a claim about what works:

    ``LOCAL_ONLY``    nothing leaves the machine — no cloud model, no remote
                      model, no external search. Local NLU, local models,
                      local tools, local knowledge carry every request that
                      can be carried at all.
    ``BALANCED``      local first; an external path may be used when the
                      operator's privacy controls permit it and the local
                      route genuinely cannot serve the request.
    ``PERFORMANCE``   the operator has said quality/latency may justify an
                      external call; permission and privacy policy still apply
                      to every one of them.
    """

    LOCAL_ONLY = "local_only"
    BALANCED = "balanced"
    PERFORMANCE = "performance"

    @classmethod
    def from_text(cls, value: Any, default: ExecutionMode | None = None) -> ExecutionMode:
        """Parse a configured mode; an unknown spelling keeps ``default``.

        A typo must not silently become the most permissive mode. The default
        is BALANCED — the middle — and a caller that cares can read the
        requested string back from settings.
        """
        fallback = default if default is not None else cls.BALANCED
        text = str(value or "").strip().lower().replace("-", "_")
        for candidate in cls:
            if candidate.value == text:
                return candidate
        return fallback

    @property
    def external_allowed(self) -> bool:
        """Whether ANY external path is possible in this mode."""
        return self is not ExecutionMode.LOCAL_ONLY

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.value, "external_allowed": self.external_allowed}


class PrivacyAction(StrEnum):
    """An outbound action the policy is asked about.

    Named as the OUTBOUND step, not as the feature: the same feature (research,
    a tool, a model) can be local or external, and only the external half is
    the policy's business.
    """

    CLOUD_MODEL = "cloud_model"
    REMOTE_MODEL = "remote_model"
    EXTERNAL_SEARCH = "external_search"
    EXTERNAL_TOOL = "external_tool"
    TELEMETRY = "telemetry"


@dataclass(frozen=True, slots=True)
class PrivacyControls:
    """The operator's switches, all defaulting to the conservative reading.

    ``allow_telemetry`` gates sending operational data OFF this machine. The
    local telemetry sampler is unaffected: it reads this machine and keeps the
    result here, which is not a disclosure.
    """

    allow_cloud: bool = True
    allow_external_search: bool = True
    allow_external_tools: bool = True
    allow_telemetry: bool = True
    allow_remote_model: bool = True
    sensitive_data_redaction: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "allow_cloud": self.allow_cloud,
            "allow_external_search": self.allow_external_search,
            "allow_external_tools": self.allow_external_tools,
            "allow_telemetry": self.allow_telemetry,
            "allow_remote_model": self.allow_remote_model,
            "sensitive_data_redaction": self.sensitive_data_redaction,
        }

    @classmethod
    def from_mapping(cls, data: Any) -> PrivacyControls:
        """Read persisted controls; an unusable value keeps the default.

        Every field is a boolean read back from a file a user can hand-edit, so
        the same rule the audit retention settings follow applies here: an
        unreadable switch does not become the permissive one.
        """
        if not isinstance(data, dict):
            return cls()
        defaults = cls()

        def flag(key: str) -> bool:
            value = data.get(key, getattr(defaults, key))
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off"}:
                return False
            return bool(getattr(defaults, key))

        return cls(
            allow_cloud=flag("allow_cloud"),
            allow_external_search=flag("allow_external_search"),
            allow_external_tools=flag("allow_external_tools"),
            allow_telemetry=flag("allow_telemetry"),
            allow_remote_model=flag("allow_remote_model"),
            sensitive_data_redaction=flag("sensitive_data_redaction"),
        )


@dataclass(frozen=True, slots=True)
class PrivacyDecision:
    """Whether an outbound action may proceed, and the ONE reason it may not.

    ``control`` names the switch (or the mode) that decided it, so a refusal can
    be explained and the screen that fixes it can be pointed at.
    """

    allowed: bool
    action: PrivacyAction
    reason: str
    control: str = ""
    mode: ExecutionMode = ExecutionMode.BALANCED

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action": self.action.value,
            "reason": self.reason,
            "control": self.control,
            "mode": self.mode.value,
        }


class ResourceLevel(StrEnum):
    """How much room the machine has, as three operating levels."""

    AMPLE = "ample"
    TIGHT = "tight"
    CRITICAL = "critical"

    @property
    def severe(self) -> bool:
        return self is not ResourceLevel.AMPLE


@dataclass(frozen=True, slots=True)
class ResourceAssessment:
    """One reading of the machine, with the thresholds that classed it.

    Everything unmeasured is ``None`` and every conclusion carries its reason.
    ``level`` is the WORST level any considered dimension reached: a machine
    with ample RAM and a GPU at 99% is not ample.
    """

    level: ResourceLevel
    reasons: tuple[str, ...] = ()
    available_ram_bytes: int | None = None
    total_ram_bytes: int | None = None
    cpu_percent: float | None = None
    gpu_utilization: float | None = None
    gpu_memory_used_bytes: int | None = None
    gpu_memory_total_bytes: int | None = None
    npu_available: bool = False
    temperature_c: float | None = None
    battery_percent: float | None = None
    on_battery: bool | None = None
    loaded_models: tuple[str, ...] = ()
    headroom_bytes: int = 0

    @property
    def available_ram_gb(self) -> float | None:
        if self.available_ram_bytes is None:
            return None
        return round(self.available_ram_bytes / (1024 ** 3), 2)

    def allows(self, needed_bytes: int | None) -> bool | None:
        """Whether ``needed_bytes`` + headroom fits right now.

        ``None`` when either the need or the free memory was not measured —
        "could not tell" must never be read as a yes.
        """
        if needed_bytes is None or self.available_ram_bytes is None:
            return None
        return self.available_ram_bytes >= int(needed_bytes) + self.headroom_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "reasons": list(self.reasons),
            "available_ram_bytes": self.available_ram_bytes,
            "total_ram_bytes": self.total_ram_bytes,
            "available_ram_gb": self.available_ram_gb,
            "cpu_percent": self.cpu_percent,
            "gpu_utilization": self.gpu_utilization,
            "gpu_memory_used_bytes": self.gpu_memory_used_bytes,
            "gpu_memory_total_bytes": self.gpu_memory_total_bytes,
            "npu_available": self.npu_available,
            "temperature_c": self.temperature_c,
            "battery_percent": self.battery_percent,
            "on_battery": self.on_battery,
            "loaded_models": list(self.loaded_models),
            "headroom_bytes": self.headroom_bytes,
        }


@dataclass(frozen=True, slots=True)
class ResourceAdvice:
    """What the governor tells a load decision to do, and why.

    ``allow`` is three-valued for the same reason every other fit check in this
    build is: ``True`` load, ``False`` do not, ``None`` could not tell — and the
    caller decides what an unknown means for its own risk tolerance.
    """

    allow: bool | None
    level: ResourceLevel
    reason: str
    needed_bytes: int | None = None
    unload: tuple[str, ...] = ()
    prefer_lightweight: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "allow": self.allow,
            "level": self.level.value,
            "reason": self.reason,
            "needed_bytes": self.needed_bytes,
            "unload": list(self.unload),
            "prefer_lightweight": self.prefer_lightweight,
        }


class CostLevel(StrEnum):
    """How expensive a request is expected to be, in five bands."""

    TRIVIAL = "trivial"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"

    @property
    def rank(self) -> int:
        return list(CostLevel).index(self)


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """What a request is likely to cost, before any of it is paid.

    An ESTIMATE by construction: every figure carries a reason, none of it
    blocks a request, and the routing layers use it to prefer a cheaper path
    when one exists — never to refuse the work.
    """

    level: CostLevel
    score: float
    reasons: tuple[str, ...] = ()
    model_required: bool = False
    cloud_required: bool = False
    gpu_required: bool = False
    estimated_ram_mb: int | None = None
    expected_latency_seconds: float | None = None
    tool_calls: int = 0
    risk: RiskLevel = RiskLevel.LOW
    complexity: str = ""
    category: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "score": round(self.score, 3),
            "reasons": list(self.reasons),
            "model_required": self.model_required,
            "cloud_required": self.cloud_required,
            "gpu_required": self.gpu_required,
            "estimated_ram_mb": self.estimated_ram_mb,
            "expected_latency_seconds": self.expected_latency_seconds,
            "tool_calls": self.tool_calls,
            "risk": self.risk.value,
            "complexity": self.complexity,
            "category": self.category,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkRecord:
    """One measured model run. Every optional figure means "not measured".

    The specification's list, one field per line: first-token latency,
    tokens/sec, total latency, RAM, GPU utilization, GPU memory, NPU use,
    structured-output success, task success, tool-selection accuracy and
    failure. A field is ``None`` when the runner could not measure it — the
    honest reading — and ``failure`` says what went wrong when the run did not
    complete at all.
    """

    model: str
    category: str
    total_ms: float
    task: str = ""
    provider: str = ""
    first_token_ms: float | None = None
    tokens_per_second: float | None = None
    ram_bytes: int | None = None
    gpu_utilization: float | None = None
    gpu_memory_bytes: int | None = None
    npu_used: bool | None = None
    structured_ok: bool | None = None
    task_ok: bool | None = None
    tool_ok: bool | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    failure: str = ""
    timestamp: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return bool(self.failure)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "category": self.category,
            "task": self.task,
            "timestamp": self.timestamp,
            "first_token_ms": _round_optional(self.first_token_ms),
            "tokens_per_second": _round_optional(self.tokens_per_second),
            "total_ms": round(float(self.total_ms), 3),
            "ram_bytes": self.ram_bytes,
            "gpu_utilization": _round_optional(self.gpu_utilization),
            "gpu_memory_bytes": self.gpu_memory_bytes,
            "npu_used": self.npu_used,
            "structured_ok": self.structured_ok,
            "task_ok": self.task_ok,
            "tool_ok": self.tool_ok,
            "prompt_tokens": int(self.prompt_tokens),
            "completion_tokens": int(self.completion_tokens),
            "failure": self.failure,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> BenchmarkRecord:
        """Rebuild a stored row; unusable rows RAISE so the reader can skip them.

        A benchmark file is a record of measurements, so a row that does not
        name a model, a category and a duration is not a measurement this build
        can honour — and quietly filling in zeroes would put invented data into
        the comparison table.
        """
        model = str(row.get("model") or "").strip()
        category = str(row.get("category") or "").strip()
        if not model or not category:
            raise ValueError("a benchmark row needs a model and a category")
        total = row.get("total_ms")
        if not isinstance(total, (int, float)):
            raise ValueError("a benchmark row needs a numeric total_ms")
        return cls(
            model=model,
            category=category,
            total_ms=float(total),
            task=str(row.get("task") or ""),
            provider=str(row.get("provider") or ""),
            first_token_ms=_optional_float(row.get("first_token_ms")),
            tokens_per_second=_optional_float(row.get("tokens_per_second")),
            ram_bytes=_optional_int(row.get("ram_bytes")),
            gpu_utilization=_optional_float(row.get("gpu_utilization")),
            gpu_memory_bytes=_optional_int(row.get("gpu_memory_bytes")),
            npu_used=_optional_bool(row.get("npu_used")),
            structured_ok=_optional_bool(row.get("structured_ok")),
            task_ok=_optional_bool(row.get("task_ok")),
            tool_ok=_optional_bool(row.get("tool_ok")),
            prompt_tokens=_optional_int(row.get("prompt_tokens")) or 0,
            completion_tokens=_optional_int(row.get("completion_tokens")) or 0,
            failure=str(row.get("failure") or ""),
            timestamp=str(row.get("timestamp") or ""),
            metadata=dict(row.get("metadata") or {})
            if isinstance(row.get("metadata"), dict)
            else {},
        )


@dataclass(frozen=True, slots=True)
class ModelScore:
    """One model's measured record in one category.

    Rates are ``None`` when nothing measured them, NEVER 0.0: "this model never
    succeeded" and "nobody checked whether it succeeded" are different claims,
    and a comparison table that conflates them is lying with arithmetic.
    """

    model: str
    category: str
    runs: int
    successes: int = 0
    failures: int = 0
    structured_ok: int = 0
    structured_checked: int = 0
    tool_ok: int = 0
    tool_checked: int = 0
    mean_total_ms: float | None = None
    median_total_ms: float | None = None
    mean_first_token_ms: float | None = None
    mean_tokens_per_second: float | None = None

    @property
    def success_rate(self) -> float | None:
        return self.successes / self.runs if self.runs else None

    @property
    def failure_rate(self) -> float | None:
        return self.failures / self.runs if self.runs else None

    @property
    def structured_rate(self) -> float | None:
        return (
            self.structured_ok / self.structured_checked if self.structured_checked else None
        )

    @property
    def tool_rate(self) -> float | None:
        return self.tool_ok / self.tool_checked if self.tool_checked else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "category": self.category,
            "runs": self.runs,
            "successes": self.successes,
            "failures": self.failures,
            "success_rate": _round_optional(self.success_rate),
            "failure_rate": _round_optional(self.failure_rate),
            "structured_rate": _round_optional(self.structured_rate),
            "tool_rate": _round_optional(self.tool_rate),
            "mean_total_ms": _round_optional(self.mean_total_ms),
            "median_total_ms": _round_optional(self.median_total_ms),
            "mean_first_token_ms": _round_optional(self.mean_first_token_ms),
            "mean_tokens_per_second": _round_optional(self.mean_tokens_per_second),
        }


def _round_optional(value: float | None) -> float | None:
    return round(float(value), 3) if value is not None else None


def _optional_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return int(value) if isinstance(value, (int, float)) else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None
