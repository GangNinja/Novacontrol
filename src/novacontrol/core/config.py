"""Configuration loading for NovaControl."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Self


@dataclass(frozen=True, slots=True)
class AppSettings:
    name: str = "NovaControl"
    environment: str = "development"
    log_level: str = "INFO"


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    url: str = "sqlite:///data/novacontrol.sqlite3"
    echo: bool = False


@dataclass(frozen=True, slots=True)
class RedisSettings:
    url: str = "redis://localhost:6379/0"


@dataclass(frozen=True, slots=True)
class SecuritySettings:
    require_approval_for_sensitive_actions: bool = True
    audit_log_path: str = "logs/audit.log"


@dataclass(frozen=True, slots=True)
class NluSettings:
    """Request-understanding thresholds (see intelligence/thresholds.py).

    These live in the central configuration because the correct values are
    machine-specific: a CPU-only box pays tens of seconds for a language-model
    round trip while a GPU box pays one, so a deployment needs to be able to
    tighten or loosen the bands without editing code. The same fields are also
    overridable per-process with ``NOVACONTROL_NLU_*`` environment variables.
    """

    fast_confidence: float = 0.90
    verify_confidence: float = 0.70
    multi_step_confidence: float = 0.88
    lexical_confidence: float = 0.62
    reference_confidence: float = 0.74
    # Calibrated confidence below which an embedding match is not acted on.
    # Defaulted from a measured precision/coverage curve (see
    # intelligence/thresholds.py); the right line depends on the embedding
    # backend, hence a setting.
    semantic_confidence: float = 0.60
    allow_llm: bool = True
    lexical_matching: bool = True
    semantic_matching: bool = True

    def to_mapping(self) -> dict[str, Any]:
        return {
            "fast_confidence": self.fast_confidence,
            "verify_confidence": self.verify_confidence,
            "multi_step_confidence": self.multi_step_confidence,
            "lexical_confidence": self.lexical_confidence,
            "reference_confidence": self.reference_confidence,
            "semantic_confidence": self.semantic_confidence,
            "allow_llm": self.allow_llm,
            "lexical_matching": self.lexical_matching,
            "semantic_matching": self.semantic_matching,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> NluSettings:
        defaults = cls()
        return cls(
            fast_confidence=_float_setting(data, "fast_confidence", defaults.fast_confidence),
            verify_confidence=_float_setting(data, "verify_confidence", defaults.verify_confidence),
            multi_step_confidence=_float_setting(
                data, "multi_step_confidence", defaults.multi_step_confidence
            ),
            lexical_confidence=_float_setting(
                data, "lexical_confidence", defaults.lexical_confidence
            ),
            reference_confidence=_float_setting(
                data, "reference_confidence", defaults.reference_confidence
            ),
            semantic_confidence=_float_setting(
                data, "semantic_confidence", defaults.semantic_confidence
            ),
            allow_llm=_bool_setting(data, "allow_llm", defaults.allow_llm),
            lexical_matching=_bool_setting(data, "lexical_matching", defaults.lexical_matching),
            semantic_matching=_bool_setting(data, "semantic_matching", defaults.semantic_matching),
        )


@dataclass(frozen=True, slots=True)
class DecisionSettings:
    """Which provider decides what to DO with an understood request.

    ``local`` is the default and the only provider a default install ever uses:
    the decision layer is deterministic, offline and complete on its own. An
    external provider can be named here, but it is an ADVISOR — it may suggest a
    route, never an executor — and it is consulted only when ``allow_remote``
    opts in, so nothing leaves the machine by accident.
    """

    provider: str = "local"
    jev_endpoint: str = ""
    jev_timeout_s: float = 2.0
    allow_remote: bool = False

    def to_mapping(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "jev_endpoint": self.jev_endpoint,
            "jev_timeout_s": self.jev_timeout_s,
            "allow_remote": self.allow_remote,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> DecisionSettings:
        defaults = cls()
        provider = str(data.get("provider", defaults.provider) or defaults.provider).strip().lower()
        return cls(
            provider=provider or defaults.provider,
            jev_endpoint=str(data.get("jev_endpoint", defaults.jev_endpoint) or "").strip(),
            jev_timeout_s=_float_value(data.get("jev_timeout_s"), defaults.jev_timeout_s),
            allow_remote=_bool_setting(data, "allow_remote", defaults.allow_remote),
        )


#: Ceilings for the plan layer's loops. A loop bound that can be configured to
#: "very large" is a loop that cannot be depended on to stop, so a configured
#: value above these is ignored rather than honoured. They mirror the plan
#: layer's own limits (``planning.models.MAX_STEP_ATTEMPTS`` and the agent
#: loop's cycle ceiling), and a test pins them together so they cannot drift.
MAX_PLAN_CYCLES = 10
MAX_PLAN_ATTEMPTS = 10


@dataclass(frozen=True, slots=True)
class PlanningSettings:
    """How far execution may go before it stops and says what happened.

    ``max_step_attempts`` counts TOTAL attempts at one step, so 2 is "try, then
    try once more" — enough for a flaky network, not enough to grind on a
    permanent failure. ``max_cycles`` bounds the agent loop's whole
    understand-decide-plan-execute-verify cycle. Both are small on purpose:
    repetition is what an unrecoverable failure looks like when nobody is
    counting.
    """

    max_cycles: int = 2
    max_step_attempts: int = 2

    def to_mapping(self) -> dict[str, Any]:
        return {
            "max_cycles": self.max_cycles,
            "max_step_attempts": self.max_step_attempts,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> PlanningSettings:
        defaults = cls()
        return cls(
            max_cycles=_count_setting(
                data, "max_cycles", defaults.max_cycles, maximum=MAX_PLAN_CYCLES
            ),
            max_step_attempts=_count_setting(
                data,
                "max_step_attempts",
                defaults.max_step_attempts,
                maximum=MAX_PLAN_ATTEMPTS,
            ),
        )


@dataclass(frozen=True, slots=True)
class VisionSettings:
    """Which vision model looks at pictures, and when it is consulted at all.

    The point of these settings is that the vision model is a PLUG, not a
    dependency: NovaControl must never be built around one VLM. ``provider``
    says whether a vision model may be used — ``auto`` (the default) uses
    whatever is wired, ``none`` refuses one outright and answers from OCR alone,
    which is the honest setting for a deployment that must not run one.

    ``model`` pins a LOCAL model by name (an Ollama vision build such as
    ``qwen2.5vl:3b``). Empty means "whatever is configured elsewhere" — the
    dedicated vision model chosen in the Vision panel, or the brain's own
    provider. The order of preference is deliberate: an operator's explicit
    choice wins over this file, and this file wins over the chat brain, because
    the chat brain is usually a text model that cannot see.

    ``prefer_ocr`` keeps the cheap path first (see ``vision/manager.py``): read
    the image, answer from the text when the text is enough, and only then pay
    for a model call.
    """

    provider: str = "auto"
    model: str = ""
    prefer_ocr: bool = True

    def to_mapping(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "prefer_ocr": self.prefer_ocr,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> VisionSettings:
        defaults = cls()
        provider = str(data.get("provider", defaults.provider) or defaults.provider).strip().lower()
        if provider not in _VISION_PROVIDERS:
            # An unknown provider keeps the default rather than failing boot:
            # a typo must not be the reason the vision layer is unreachable.
            provider = defaults.provider
        return cls(
            provider=provider,
            model=str(data.get("model", defaults.model) or "").strip(),
            prefer_ocr=_bool_setting(data, "prefer_ocr", defaults.prefer_ocr),
        )


#: The provider vocabulary. Small on purpose: the vision model itself is
#: configured where it lives (the Vision panel / the persisted model store), and
#: these only say whether one may be used.
_VISION_PROVIDERS = frozenset({"auto", "none"})

#: Headroom ceiling for a model load, in megabytes. Half of this machine's RAM
#: is the largest reserve that still leaves the desktop room to run; above that
#: the setting is not a reserve, it is a refusal to load anything.
MAX_MODEL_HEADROOM_MB = 8192

#: The longest a model may be kept warm before idle release, in seconds. One
#: hour: past that the model has been idle long enough that the reload is the
#: smaller cost, on a machine whose whole problem is memory pressure.
MAX_MODEL_KEEP_WARM_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """How models are kept resident, and what the operator declares about them.

    Three knobs, each answering a question the model manager cannot answer for
    itself, and each defaulting to the RAM-conscious end of its range:

    ``keep_alive`` is the lifecycle POLICY — ``immediate`` (release as soon as
    the operation returns), ``warm`` (hold it, release after
    ``keep_warm_seconds`` idle), ``while_active`` (hold while a task that
    selected it runs), ``never``. The vocabulary and its meaning live in
    ``models/manager.py``; the value is carried as a string and validated there,
    so there is one list of policy names rather than two that can drift. Left
    unrecognised it keeps the default and reports the miss (see
    ``KeepAliveSettings.honoured``) instead of quietly doing something else.

    ``keep_warm_seconds`` defaults to 0, which is NOT "release immediately" —
    it means "do not override the runtime's own residency", because that number
    is the runtime's to choose and inventing one here would be a guess wearing a
    configuration's clothes.

    ``headroom_mb`` is how much memory a load must leave free. It is in
    megabytes rather than bytes because this is a human decision, and clamped to
    :data:`MAX_MODEL_HEADROOM_MB` because an unbounded reserve is an unbounded
    refusal to load.

    ``declarations`` lets configuration describe a model this build has never
    heard of — its capabilities, its context window, its size — so a local build
    is selectable by what it can do without editing Python. Each entry is a
    mapping in :class:`~novacontrol.models.profiles.ModelProfile` shape.
    """

    keep_alive: str = "warm"
    keep_warm_seconds: int = 0
    headroom_mb: int = 512
    declarations: tuple[Mapping[str, Any], ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "keep_alive": self.keep_alive,
            "keep_warm_seconds": self.keep_warm_seconds,
            "headroom_mb": self.headroom_mb,
            "declarations": [dict(declaration) for declaration in self.declarations],
        }

    @property
    def headroom_bytes(self) -> int:
        return max(0, int(self.headroom_mb)) * 1024 * 1024

    def keep_alive_settings(self) -> dict[str, Any]:
        """The mapping ``models.manager.KeepAliveSettings`` expects.

        ``idle_seconds`` is only meaningful for the ``warm`` policy, so it is
        passed only there: handing a duration to ``immediate`` would suggest a
        delay that policy does not have.
        """
        settings: dict[str, Any] = {"policy": self.keep_alive}
        if self.keep_alive.strip().lower().replace("-", "_") == "warm":
            settings["idle_seconds"] = self.keep_warm_seconds
        return settings

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ModelSettings:
        defaults = cls()
        raw = data.get("declarations", ())
        declarations: tuple[Mapping[str, Any], ...] = ()
        if isinstance(raw, Mapping):
            declarations = (raw,)
        elif isinstance(raw, (list, tuple)):
            declarations = tuple(item for item in raw if isinstance(item, Mapping))
        return cls(
            keep_alive=str(data.get("keep_alive", defaults.keep_alive) or defaults.keep_alive)
            .strip()
            .lower(),
            keep_warm_seconds=_count_setting(
                data,
                "keep_warm_seconds",
                defaults.keep_warm_seconds,
                maximum=MAX_MODEL_KEEP_WARM_SECONDS,
            ),
            headroom_mb=_count_setting(
                data, "headroom_mb", defaults.headroom_mb, maximum=MAX_MODEL_HEADROOM_MB
            ),
            declarations=declarations,
        )


#: How often the automation tick is allowed to run, in seconds. The floor is
#: what stops a configured "tick every 0" from becoming a busy loop.
MIN_AUTOMATION_TICK_SECONDS = 0.5
MAX_AUTOMATION_TICK_SECONDS = 3600.0

#: Failed runs before a broken automation disables itself. Bounded so a typo
#: cannot set "keep failing forever".
MAX_AUTOMATION_FAILURES = 10


@dataclass(frozen=True, slots=True)
class AutomationSettings:
    """How scheduled work is policed.

    ``tick_seconds`` is the resolution of the whole mechanism, not of any one
    task: a schedule is stored as an instant and the engine asks "what is due?"
    on this cadence. ``max_failures`` is the point at which a task that keeps
    failing disables itself — bounded on purpose, because an automation that
    fails every hour forever is a task nobody is watching.
    """

    enabled: bool = True
    tick_seconds: float = 15.0
    max_failures: int = 3

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "tick_seconds": self.tick_seconds,
            "max_failures": self.max_failures,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> AutomationSettings:
        defaults = cls()
        return cls(
            enabled=_bool_setting(data, "enabled", defaults.enabled),
            tick_seconds=_tick_value(data.get("tick_seconds"), defaults.tick_seconds),
            max_failures=_count_setting(
                data, "max_failures", defaults.max_failures, maximum=MAX_AUTOMATION_FAILURES
            ),
        )


@dataclass(frozen=True, slots=True)
class ResourceSettings:
    """Phase 14: the lines a load decision is made against, as configuration.

    Defaults describe the machine this phase was written for — 16 GB of RAM,
    an 8 GB Arc and an NPU that may or may not be visible — so a 4 GB free
    comfort line and a 1.5 GB critical line. They are read from the config
    file's ``resources`` section because the second machine this runs on will
    not be this one. ``max_resident_models`` is the concurrent-resident limit
    (0 = no policy limit, the memory check still applies) and ``benchmark_cap``
    bounds the stored measurements.
    """

    tight_free_ram_mb: int = 4096
    critical_free_ram_mb: int = 1536
    max_cpu_percent: float = 90.0
    max_gpu_utilization: float = 95.0
    max_temperature_c: float = 85.0
    min_battery_percent: float = 20.0
    max_resident_models: int = 2
    benchmark_cap: int = 500
    # How long one diagnostic check may take before it is reported as failing.
    # Short by default: a check that reaches for a runtime or a driver should
    # answer from what is already wired, not by waiting on one.
    diagnostics_timeout_seconds: float = 5.0

    def to_mapping(self) -> dict[str, Any]:
        return {
            "tight_free_ram_mb": self.tight_free_ram_mb,
            "critical_free_ram_mb": self.critical_free_ram_mb,
            "max_cpu_percent": self.max_cpu_percent,
            "max_gpu_utilization": self.max_gpu_utilization,
            "max_temperature_c": self.max_temperature_c,
            "min_battery_percent": self.min_battery_percent,
            "max_resident_models": self.max_resident_models,
            "benchmark_cap": self.benchmark_cap,
            "diagnostics_timeout_seconds": self.diagnostics_timeout_seconds,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ResourceSettings:
        """Read the section; an unusable value keeps its default.

        The critical line is clamped to the comfort line: a critical threshold
        ABOVE it would make "tight" unreachable, which is a configuration bug
        the governor would otherwise have to discover at run time.
        """
        defaults = cls()

        def count(key: str, current: int, *, low: int = 0, high: int) -> int:
            value = data.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return current
            return max(low, min(high, int(value)))

        def number(key: str, current: float, *, low: float, high: float) -> float:
            value = data.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return current
            return max(low, min(high, float(value)))

        tight = count("tight_free_ram_mb", defaults.tight_free_ram_mb, low=1, high=1024 * 1024)
        critical = count(
            "critical_free_ram_mb", defaults.critical_free_ram_mb, low=0, high=1024 * 1024
        )
        return cls(
            tight_free_ram_mb=tight,
            critical_free_ram_mb=min(critical, tight),
            max_cpu_percent=number(
                "max_cpu_percent", defaults.max_cpu_percent, low=1.0, high=100.0
            ),
            max_gpu_utilization=number(
                "max_gpu_utilization", defaults.max_gpu_utilization, low=1.0, high=100.0
            ),
            max_temperature_c=number(
                "max_temperature_c", defaults.max_temperature_c, low=1.0, high=150.0
            ),
            min_battery_percent=number(
                "min_battery_percent", defaults.min_battery_percent, low=0.0, high=100.0
            ),
            max_resident_models=count(
                "max_resident_models", defaults.max_resident_models, low=0, high=8
            ),
            benchmark_cap=count("benchmark_cap", defaults.benchmark_cap, low=0, high=100_000),
            diagnostics_timeout_seconds=number(
                "diagnostics_timeout_seconds",
                defaults.diagnostics_timeout_seconds,
                low=0.1,
                high=120.0,
            ),
        )


#: The longest a trajectory may be kept, in days. Ten years: past that the
#: setting is not a retention policy, it is a decision not to have one.
MAX_EVALUATION_RETENTION_DAYS = 3650

#: The largest number of rows one evaluation store may hold.
MAX_EVALUATION_RECORDS = 1_000_000


@dataclass(frozen=True, slots=True)
class EvaluationSettings:
    """Phase 15: what is recorded, how it is scored, and how long it is kept.

    ``enabled`` is the master switch: off means no trajectory is built at all.
    ``record_trajectories`` is the operator's finer switch — evaluation and
    reward can still be computed for a trajectory handed over directly (a
    specialist run, an imported row) while live capture stays off.

    The scoring lines (``latency_budget_ms``, ``max_tool_calls``, ``max_retries``)
    are here rather than in code because "slow" and "too many steps" are facts
    about a machine and a workload, not about this project. The reward weights
    are read here too: an operator changing what "good" means does it in the
    config file, and the defaults live in ``evaluation/reward.py`` so there is
    exactly one copy of them.
    """

    enabled: bool = True
    record_trajectories: bool = True
    evaluate: bool = True
    compute_rewards: bool = True
    quality_filter: bool = True
    redact_sensitive: bool = True
    retention_days: int = 30
    max_records: int = 2000
    latency_budget_ms: float = 8000.0
    max_tool_calls: int = 12
    max_retries: int = 2
    reward_component_weights: Mapping[str, float] = field(default_factory=dict)
    reward_penalty_weights: Mapping[str, float] = field(default_factory=dict)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "record_trajectories": self.record_trajectories,
            "evaluate": self.evaluate,
            "compute_rewards": self.compute_rewards,
            "quality_filter": self.quality_filter,
            "redact_sensitive": self.redact_sensitive,
            "retention_days": self.retention_days,
            "max_records": self.max_records,
            "latency_budget_ms": self.latency_budget_ms,
            "max_tool_calls": self.max_tool_calls,
            "max_retries": self.max_retries,
            "reward_component_weights": dict(self.reward_component_weights),
            "reward_penalty_weights": dict(self.reward_penalty_weights),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> EvaluationSettings:
        """Read the section; an unusable value keeps its default."""
        defaults = cls()
        days = data.get("retention_days")
        if isinstance(days, bool) or not isinstance(days, (int, float)) or days < 0:
            retention = defaults.retention_days
        else:
            retention = min(int(days), MAX_EVALUATION_RETENTION_DAYS)
        return cls(
            enabled=_bool_setting(data, "enabled", defaults.enabled),
            record_trajectories=_bool_setting(
                data, "record_trajectories", defaults.record_trajectories
            ),
            evaluate=_bool_setting(data, "evaluate", defaults.evaluate),
            compute_rewards=_bool_setting(data, "compute_rewards", defaults.compute_rewards),
            quality_filter=_bool_setting(data, "quality_filter", defaults.quality_filter),
            redact_sensitive=_bool_setting(data, "redact_sensitive", defaults.redact_sensitive),
            retention_days=retention,
            max_records=_count_setting(
                data, "max_records", defaults.max_records, maximum=MAX_EVALUATION_RECORDS
            ),
            latency_budget_ms=_float_value(
                data.get("latency_budget_ms"), defaults.latency_budget_ms
            ),
            max_tool_calls=_count_setting(
                data, "max_tool_calls", defaults.max_tool_calls, maximum=10_000
            ),
            max_retries=_count_setting(data, "max_retries", defaults.max_retries, maximum=100),
            reward_component_weights=_weight_setting(
                data.get("reward_component_weights"), defaults.reward_component_weights
            ),
            reward_penalty_weights=_weight_setting(
                data.get("reward_penalty_weights"), defaults.reward_penalty_weights
            ),
        )


#: The largest number of rows one training store may hold.
MAX_TRAINING_RECORDS = 1_000_000

#: The ceiling on a checkpoint retention policy. Kept here (rather than
#: imported from the training package) so the settings layer never imports the
#: subsystem it configures; ``tests/test_training.py`` pins the two together.
MAX_TRAINING_CHECKPOINTS = 100


@dataclass(frozen=True, slots=True)
class TrainingSettings:
    """Phase 16: whether supervised fine-tuning is available, and how cautious.

    Training is the one subsystem that can occupy this machine for hours and
    fill a disk with checkpoints, so its defaults are the cautious ones:
    ``enabled`` is True (the surface exists) while ``dry_run`` is True (nothing
    is actually trained until an operator asks) and ``allow_unsafe`` is False
    (an estimate that says UNSAFE is a refusal, not a warning to click past).

    ``defaults`` is a partial ``TrainingConfig`` mapping: the machine's usual
    epochs, LoRA rank or sequence length are written down once here and every
    new run inherits them. The mapping is not validated here — an unusable
    value is reported by the run itself, so there is one validator, not two.
    """

    enabled: bool = True
    dry_run: bool = True
    allow_unsafe: bool = False
    hardware_policy: str = "auto"
    max_checkpoints: int = 3
    max_records: int = 2000
    retention_days: int = 30
    defaults: Mapping[str, Any] = field(default_factory=dict)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "allow_unsafe": self.allow_unsafe,
            "hardware_policy": self.hardware_policy,
            "max_checkpoints": self.max_checkpoints,
            "max_records": self.max_records,
            "retention_days": self.retention_days,
            "defaults": dict(self.defaults),
        }

    def base_config(self) -> dict[str, Any]:
        """What a new run starts from: this section's policy, plus overrides.

        The section's own fields are the policy (they say how this deployment
        behaves); ``defaults`` is the operator's opinion about the trainer.
        A key in both is the operator's, deliberately.
        """
        settings: dict[str, Any] = {
            "hardware_policy": self.hardware_policy,
            "max_checkpoints": self.max_checkpoints,
            "dry_run": self.dry_run,
        }
        settings.update(self.defaults)
        return settings

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Self:
        """Read the section; an unusable value keeps its default.

        ``Self`` rather than the class name because the parser builds ``cls``:
        Phase 17's preference section is the same shape and inherits this reader,
        and an annotation naming the base would make that inherit-then-narrow
        pattern untypeable for the caller.
        """
        defaults = cls()
        days = data.get("retention_days")
        if isinstance(days, bool) or not isinstance(days, (int, float)) or days < 0:
            retention = defaults.retention_days
        else:
            retention = min(int(days), MAX_EVALUATION_RETENTION_DAYS)
        raw_defaults = data.get("defaults")
        overrides = (
            {str(key): value for key, value in raw_defaults.items()}
            if isinstance(raw_defaults, Mapping)
            else {}
        )
        policy = str(data.get("hardware_policy", defaults.hardware_policy)).strip().lower()
        return cls(
            enabled=_bool_setting(data, "enabled", defaults.enabled),
            dry_run=_bool_setting(data, "dry_run", defaults.dry_run),
            allow_unsafe=_bool_setting(data, "allow_unsafe", defaults.allow_unsafe),
            hardware_policy=policy or defaults.hardware_policy,
            max_checkpoints=_count_setting(
                data,
                "max_checkpoints",
                defaults.max_checkpoints,
                maximum=MAX_TRAINING_CHECKPOINTS,
            ),
            max_records=_count_setting(
                data, "max_records", defaults.max_records, maximum=MAX_TRAINING_RECORDS
            ),
            retention_days=retention,
            defaults=overrides,
        )


@dataclass(frozen=True, slots=True)
class PreferenceSettings(TrainingSettings):
    """Phase 17: whether preference optimization is offered, and how cautious.

    Deliberately the SAME fields as the training section: DPO/ORPO is the same
    kind of subsystem with the same hazards — a run can occupy this machine for
    hours and fill a disk with checkpoints — so these are the same switches, and
    an operator who understands one section understands both. The section is
    separate because the answer may differ: an installation may fine-tune a model
    on its own verified data and still refuse preference optimization, or the
    other way round, and one switch for both would decide that for them.

    ``defaults`` is a partial ``PreferenceTrainingConfig`` mapping, so this is
    where a deployment writes down its usual ``algorithm``, ``beta`` or LoRA rank
    once and every new run inherits it. The mapping is not validated here — an
    unusable value is reported by the run itself, so there is one validator, not
    two.
    """


@dataclass(frozen=True, slots=True)
class RLHFSettings(TrainingSettings):
    """Phase 18: whether RLHF/RLAIF is offered, and how cautious.

    The same fields as the other training sections, for the same reasons: an RL
    run can occupy this machine for hours and fill a disk with checkpoints, so an
    operator who understands one section understands this one. It is a separate
    section because the answer may differ — and this is the section where the
    careful answer is most likely, because only a mock policy optimizer ships
    with the phase. Its ``dry_run`` default means the pipeline can be planned,
    priced and simulated everywhere while a real run stays behind three doors:
    this switch, the walled dependencies, and a wired policy-optimizer runner.
    """


@dataclass(frozen=True, slots=True)
class RLVRSettings(TrainingSettings):
    """Phase 19: whether RLVR and critique-based learning are offered at all.

    The same cautious fields as the other training sections — ``dry_run`` on by
    default, ``allow_unsafe`` off, one place to write down a usual base model or
    LoRA rank — plus the two switches that are RLVR's own:

      * ``deterministic_only`` keeps an AI evaluator out of the loop entirely,
        so a reward can only ever come from a check a machine can repeat;
      * ``require_evidence`` refuses a reward whose verifications cite no
        observable fact.

    The section is optional by construction: nothing in normal NovaControl
    operation imports it, and an installation that never turns it on still
    records, evaluates, fine-tunes and preference-optimizes exactly as before.
    """

    deterministic_only: bool = True
    require_evidence: bool = True
    critique_enabled: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Self:
        # Explicit two-argument ``super`` on purpose: ``@dataclass(slots=True)``
        # rebuilds the class, so the zero-argument form's ``__class__`` cell
        # still points at the pre-slots class and raises ``TypeError``.
        base = super(RLVRSettings, cls).from_mapping(data)
        defaults = cls()
        return replace(
            base,
            deterministic_only=_bool_setting(
                data, "deterministic_only", defaults.deterministic_only
            ),
            require_evidence=_bool_setting(
                data, "require_evidence", defaults.require_evidence
            ),
            critique_enabled=_bool_setting(
                data, "critique_enabled", defaults.critique_enabled
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            # Explicit two-argument ``super`` for the same reason as
            # ``from_mapping`` above (``slots=True`` rebuilds the class).
            **super(RLVRSettings, self).to_mapping(),
            "deterministic_only": self.deterministic_only,
            "require_evidence": self.require_evidence,
            "critique_enabled": self.critique_enabled,
        }


@dataclass(frozen=True, slots=True)
class ModuleSettings:
    enabled: bool = True
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NovaControlConfig:
    app: AppSettings = field(default_factory=AppSettings)
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    redis: RedisSettings = field(default_factory=RedisSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    nlu: NluSettings = field(default_factory=NluSettings)
    decision: DecisionSettings = field(default_factory=DecisionSettings)
    planning: PlanningSettings = field(default_factory=PlanningSettings)
    automation: AutomationSettings = field(default_factory=AutomationSettings)
    # Phase 14: the resource governor's thresholds and the model-layer limits
    # read from the same section, so one file describes this machine's comfort
    # lines rather than each service carrying its own constants.
    resources: ResourceSettings = field(default_factory=ResourceSettings)
    # Phase 15: what is recorded, what it is scored against, and how long the
    # evidence is kept — one section, because these are one decision.
    evaluation: EvaluationSettings = field(default_factory=EvaluationSettings)
    # Phase 16: whether supervised fine-tuning is offered, how cautious it is,
    # and the defaults a new training run inherits. Separate from evaluation
    # because an installation may record and score without ever training.
    training: TrainingSettings = field(default_factory=TrainingSettings)
    # Phase 17: whether DPO/ORPO is offered at all, how cautious it is, and the
    # defaults a new preference run inherits. Its own section for the same reason
    # the training section is its own: the two subsystems can be answered
    # differently, and a deployment that wants one and not the other should not
    # have to accept both.
    preference: PreferenceSettings = field(default_factory=PreferenceSettings)
    # Phase 18: whether RLHF/RLAIF is offered at all, how cautious it is, and
    # the defaults an RL run inherits. Its own section for the third time, and
    # for the same reason: recording, fine-tuning, preference optimization and
    # reinforcement learning are four decisions a deployment may answer
    # differently — and an installation that records and evaluates should be
    # able to refuse RL without giving up anything else.
    rlhf: RLHFSettings = field(default_factory=RLHFSettings)
    # Phase 19: verifiable rewards and critique-based learning. A separate
    # section for the fourth time, and for the same reason: an installation may
    # run RLHF and still refuse RLVR (or refuse to let a model score itself).
    rlvr: RLVRSettings = field(default_factory=RLVRSettings)
    vision: VisionSettings = field(default_factory=VisionSettings)
    models: ModelSettings = field(default_factory=ModelSettings)
    modules: Mapping[str, ModuleSettings] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> NovaControlConfig:
        """Build config from a dictionary-like object."""
        modules = {
            name: ModuleSettings(
                enabled=bool(settings.get("enabled", True)),
                options={
                    key: value
                    for key, value in settings.items()
                    if key != "enabled"
                },
            )
            for name, settings in _mapping(data.get("modules", {})).items()
            if isinstance(settings, Mapping)
        }

        return cls(
            app=AppSettings(**_mapping(data.get("app", {}))),
            database=DatabaseSettings(**_mapping(data.get("database", {}))),
            redis=RedisSettings(**_mapping(data.get("redis", {}))),
            security=SecuritySettings(**_mapping(data.get("security", {}))),
            nlu=NluSettings.from_mapping(_mapping(data.get("nlu", {}))),
            decision=DecisionSettings.from_mapping(_mapping(data.get("decision", {}))),
            planning=PlanningSettings.from_mapping(_mapping(data.get("planning", {}))),
            automation=AutomationSettings.from_mapping(_mapping(data.get("automation", {}))),
            resources=ResourceSettings.from_mapping(_mapping(data.get("resources", {}))),
            evaluation=EvaluationSettings.from_mapping(_mapping(data.get("evaluation", {}))),
            training=TrainingSettings.from_mapping(_mapping(data.get("training", {}))),
            preference=PreferenceSettings.from_mapping(
                _mapping(data.get("preference", {}))
            ),
            rlhf=RLHFSettings.from_mapping(_mapping(data.get("rlhf", {}))),
            rlvr=RLVRSettings.from_mapping(_mapping(data.get("rlvr", {}))),
            vision=VisionSettings.from_mapping(_mapping(data.get("vision", {}))),
            models=ModelSettings.from_mapping(_mapping(data.get("models", {}))),
            modules=modules,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> NovaControlConfig:
        """Load config from JSON, or YAML when PyYAML is installed."""
        config_path = Path(path)
        raw = config_path.read_text(encoding="utf-8")
        if config_path.suffix.lower() == ".json":
            return cls.from_mapping(json.loads(raw))
        if config_path.suffix.lower() in {".yaml", ".yml"}:
            try:
                import yaml
            except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError("PyYAML is required to load YAML configuration.") from exc
            return cls.from_mapping(yaml.safe_load(raw) or {})
        raise ValueError(f"Unsupported config file type: {config_path.suffix}")

    @classmethod
    def from_environment(cls) -> NovaControlConfig:
        """Load config from environment variables."""
        base = cls()
        require_approval = os.getenv("NOVACONTROL_REQUIRE_APPROVAL")
        return replace(
            base,
            app=replace(
                base.app,
                environment=os.getenv("NOVACONTROL_ENV", base.app.environment),
                log_level=os.getenv("NOVACONTROL_LOG_LEVEL", base.app.log_level),
            ),
            database=replace(
                base.database,
                url=os.getenv("NOVACONTROL_DATABASE_URL", base.database.url),
            ),
            redis=replace(
                base.redis,
                url=os.getenv("NOVACONTROL_REDIS_URL", base.redis.url),
            ),
            security=replace(
                base.security,
                require_approval_for_sensitive_actions=_parse_bool(
                    require_approval,
                    default=base.security.require_approval_for_sensitive_actions,
                ),
            ),
            nlu=replace(
                base.nlu,
                # The centralized NLU band overrides; see
                # intelligence/thresholds.py for the same fields' meaning.
                fast_confidence=_parse_float(
                    os.getenv("NOVACONTROL_NLU_FAST_CONFIDENCE"), default=base.nlu.fast_confidence
                ),
                verify_confidence=_parse_float(
                    os.getenv("NOVACONTROL_NLU_VERIFY_CONFIDENCE"),
                    default=base.nlu.verify_confidence,
                ),
                semantic_confidence=_parse_float(
                    os.getenv("NOVACONTROL_NLU_SEMANTIC_CONFIDENCE"),
                    default=base.nlu.semantic_confidence,
                ),
                allow_llm=_parse_bool(
                    os.getenv("NOVACONTROL_NLU_ALLOW_LLM"), default=base.nlu.allow_llm
                ),
                lexical_matching=_parse_bool(
                    os.getenv("NOVACONTROL_NLU_LEXICAL_MATCHING"), default=base.nlu.lexical_matching
                ),
                semantic_matching=_parse_bool(
                    os.getenv("NOVACONTROL_NLU_SEMANTIC_MATCHING"),
                    default=base.nlu.semantic_matching,
                ),
            ),
            decision=replace(
                base.decision,
                # "local" unless an operator names something else: the decision
                # layer must never require an external service to work.
                provider=(
                    os.getenv(
                        "NOVACONTROL_DECISION_PROVIDER", base.decision.provider
                    ).strip().lower()
                    or base.decision.provider
                ),
                jev_endpoint=os.getenv(
                    "NOVACONTROL_DECISION_JEV_ENDPOINT", base.decision.jev_endpoint
                ).strip(),
                jev_timeout_s=_seconds_value(
                    os.getenv("NOVACONTROL_DECISION_JEV_TIMEOUT"), base.decision.jev_timeout_s
                ),
                allow_remote=_parse_bool(
                    os.getenv("NOVACONTROL_DECISION_ALLOW_REMOTE"),
                    default=base.decision.allow_remote,
                ),
            ),
            planning=replace(
                base.planning,
                # Bounds on loops: an unusable value keeps the default, and a
                # value above the ceiling is clamped to it rather than obeyed.
                max_cycles=_count_value(
                    os.getenv("NOVACONTROL_PLANNING_MAX_CYCLES"),
                    base.planning.max_cycles,
                    maximum=MAX_PLAN_CYCLES,
                ),
                max_step_attempts=_count_value(
                    os.getenv("NOVACONTROL_PLANNING_MAX_STEP_ATTEMPTS"),
                    base.planning.max_step_attempts,
                    maximum=MAX_PLAN_ATTEMPTS,
                ),
            ),
            automation=replace(
                base.automation,
                enabled=_parse_bool(
                    os.getenv("NOVACONTROL_AUTOMATION_ENABLED"),
                    default=base.automation.enabled,
                ),
                # Clamped, not obeyed: a tick below the floor would busy-loop and
                # one above the ceiling would make a "15 minute" schedule fire
                # hours late, so an unusable value keeps the default.
                tick_seconds=_tick_value(
                    os.getenv("NOVACONTROL_AUTOMATION_TICK_SECONDS"),
                    base.automation.tick_seconds,
                ),
                max_failures=_count_value(
                    os.getenv("NOVACONTROL_AUTOMATION_MAX_FAILURES"),
                    base.automation.max_failures,
                    maximum=MAX_AUTOMATION_FAILURES,
                ),
            ),
            models=replace(
                base.models,
                # The policy NAME is not validated here: the vocabulary lives in
                # models/manager.py, and an unrecognised value keeps the default
                # while being reported as unhonoured rather than silently
                # becoming "warm".
                keep_alive=(
                    os.getenv(
                        "NOVACONTROL_MODEL_KEEP_ALIVE", base.models.keep_alive
                    ).strip().lower()
                    or base.models.keep_alive
                ),
                keep_warm_seconds=_count_value(
                    os.getenv("NOVACONTROL_MODEL_KEEP_WARM_SECONDS"),
                    base.models.keep_warm_seconds,
                    maximum=MAX_MODEL_KEEP_WARM_SECONDS,
                ),
                headroom_mb=_count_value(
                    os.getenv("NOVACONTROL_MODEL_HEADROOM_MB"),
                    base.models.headroom_mb,
                    maximum=MAX_MODEL_HEADROOM_MB,
                ),
            ),
            evaluation=replace(
                base.evaluation,
                enabled=_parse_bool(
                    os.getenv("NOVACONTROL_EVALUATION_ENABLED"),
                    default=base.evaluation.enabled,
                ),
                record_trajectories=_parse_bool(
                    os.getenv("NOVACONTROL_EVALUATION_RECORDING"),
                    default=base.evaluation.record_trajectories,
                ),
                redact_sensitive=_parse_bool(
                    os.getenv("NOVACONTROL_EVALUATION_REDACT"),
                    default=base.evaluation.redact_sensitive,
                ),
                retention_days=_days_value(
                    os.getenv("NOVACONTROL_EVALUATION_RETENTION_DAYS"),
                    base.evaluation.retention_days,
                ),
                max_records=_count_value(
                    os.getenv("NOVACONTROL_EVALUATION_MAX_RECORDS"),
                    base.evaluation.max_records,
                    maximum=MAX_EVALUATION_RECORDS,
                ),
                latency_budget_ms=_seconds_value(
                    os.getenv("NOVACONTROL_EVALUATION_LATENCY_BUDGET_MS"),
                    base.evaluation.latency_budget_ms,
                ),
            ),
            training=replace(
                base.training,
                enabled=_parse_bool(
                    os.getenv("NOVACONTROL_TRAINING_ENABLED"),
                    default=base.training.enabled,
                ),
                dry_run=_parse_bool(
                    os.getenv("NOVACONTROL_TRAINING_DRY_RUN"),
                    default=base.training.dry_run,
                ),
                allow_unsafe=_parse_bool(
                    os.getenv("NOVACONTROL_TRAINING_ALLOW_UNSAFE"),
                    default=base.training.allow_unsafe,
                ),
                hardware_policy=(
                    os.getenv(
                        "NOVACONTROL_TRAINING_HARDWARE_POLICY", base.training.hardware_policy
                    ).strip().lower()
                    or base.training.hardware_policy
                ),
                max_checkpoints=_count_value(
                    os.getenv("NOVACONTROL_TRAINING_MAX_CHECKPOINTS"),
                    base.training.max_checkpoints,
                    maximum=MAX_TRAINING_CHECKPOINTS,
                ),
                max_records=_count_value(
                    os.getenv("NOVACONTROL_TRAINING_MAX_RECORDS"),
                    base.training.max_records,
                    maximum=MAX_TRAINING_RECORDS,
                ),
                retention_days=_days_value(
                    os.getenv("NOVACONTROL_TRAINING_RETENTION_DAYS"),
                    base.training.retention_days,
                ),
            ),
            # Phase 17: the preference switches, read on exactly the same rules.
            # The ceilings are the training section's, because they are about the
            # same two things — how many checkpoints a run may keep and how many
            # rows the store may hold.
            preference=replace(
                base.preference,
                enabled=_parse_bool(
                    os.getenv("NOVACONTROL_PREFERENCE_ENABLED"),
                    default=base.preference.enabled,
                ),
                dry_run=_parse_bool(
                    os.getenv("NOVACONTROL_PREFERENCE_DRY_RUN"),
                    default=base.preference.dry_run,
                ),
                allow_unsafe=_parse_bool(
                    os.getenv("NOVACONTROL_PREFERENCE_ALLOW_UNSAFE"),
                    default=base.preference.allow_unsafe,
                ),
                hardware_policy=(
                    os.getenv(
                        "NOVACONTROL_PREFERENCE_HARDWARE_POLICY",
                        base.preference.hardware_policy,
                    )
                    .strip()
                    .lower()
                    or base.preference.hardware_policy
                ),
                max_checkpoints=_count_value(
                    os.getenv("NOVACONTROL_PREFERENCE_MAX_CHECKPOINTS"),
                    base.preference.max_checkpoints,
                    maximum=MAX_TRAINING_CHECKPOINTS,
                ),
                max_records=_count_value(
                    os.getenv("NOVACONTROL_PREFERENCE_MAX_RECORDS"),
                    base.preference.max_records,
                    maximum=MAX_TRAINING_RECORDS,
                ),
                retention_days=_days_value(
                    os.getenv("NOVACONTROL_PREFERENCE_RETENTION_DAYS"),
                    base.preference.retention_days,
                ),
            ),
            # Phase 18: the RLHF switches, read on exactly the same rules, with
            # the training section's ceilings because they are about the same two
            # things (how many checkpoints a run may keep, how many rows the
            # store may hold).
            rlhf=replace(
                base.rlhf,
                enabled=_parse_bool(
                    os.getenv("NOVACONTROL_RLHF_ENABLED"),
                    default=base.rlhf.enabled,
                ),
                dry_run=_parse_bool(
                    os.getenv("NOVACONTROL_RLHF_DRY_RUN"),
                    default=base.rlhf.dry_run,
                ),
                allow_unsafe=_parse_bool(
                    os.getenv("NOVACONTROL_RLHF_ALLOW_UNSAFE"),
                    default=base.rlhf.allow_unsafe,
                ),
                hardware_policy=(
                    os.getenv(
                        "NOVACONTROL_RLHF_HARDWARE_POLICY",
                        base.rlhf.hardware_policy,
                    )
                    .strip()
                    .lower()
                    or base.rlhf.hardware_policy
                ),
                max_checkpoints=_count_value(
                    os.getenv("NOVACONTROL_RLHF_MAX_CHECKPOINTS"),
                    base.rlhf.max_checkpoints,
                    maximum=MAX_TRAINING_CHECKPOINTS,
                ),
                max_records=_count_value(
                    os.getenv("NOVACONTROL_RLHF_MAX_RECORDS"),
                    base.rlhf.max_records,
                    maximum=MAX_TRAINING_RECORDS,
                ),
                retention_days=_days_value(
                    os.getenv("NOVACONTROL_RLHF_RETENTION_DAYS"),
                    base.rlhf.retention_days,
                ),
            ),
            # Phase 19: the RLVR switches, read on exactly the same rules. The
            # two phase-specific flags default in the cautious direction too:
            # deterministic-only verification and mandatory evidence.
            rlvr=replace(
                base.rlvr,
                enabled=_parse_bool(
                    os.getenv("NOVACONTROL_RLVR_ENABLED"),
                    default=base.rlvr.enabled,
                ),
                dry_run=_parse_bool(
                    os.getenv("NOVACONTROL_RLVR_DRY_RUN"),
                    default=base.rlvr.dry_run,
                ),
                allow_unsafe=_parse_bool(
                    os.getenv("NOVACONTROL_RLVR_ALLOW_UNSAFE"),
                    default=base.rlvr.allow_unsafe,
                ),
                hardware_policy=(
                    os.getenv(
                        "NOVACONTROL_RLVR_HARDWARE_POLICY",
                        base.rlvr.hardware_policy,
                    )
                    .strip()
                    .lower()
                    or base.rlvr.hardware_policy
                ),
                max_checkpoints=_count_value(
                    os.getenv("NOVACONTROL_RLVR_MAX_CHECKPOINTS"),
                    base.rlvr.max_checkpoints,
                    maximum=MAX_TRAINING_CHECKPOINTS,
                ),
                max_records=_count_value(
                    os.getenv("NOVACONTROL_RLVR_MAX_RECORDS"),
                    base.rlvr.max_records,
                    maximum=MAX_TRAINING_RECORDS,
                ),
                retention_days=_days_value(
                    os.getenv("NOVACONTROL_RLVR_RETENTION_DAYS"),
                    base.rlvr.retention_days,
                ),
                deterministic_only=_parse_bool(
                    os.getenv("NOVACONTROL_RLVR_DETERMINISTIC_ONLY"),
                    default=base.rlvr.deterministic_only,
                ),
                require_evidence=_parse_bool(
                    os.getenv("NOVACONTROL_RLVR_REQUIRE_EVIDENCE"),
                    default=base.rlvr.require_evidence,
                ),
                critique_enabled=_parse_bool(
                    os.getenv("NOVACONTROL_RLVR_CRITIQUE_ENABLED"),
                    default=base.rlvr.critique_enabled,
                ),
            ),
            vision=replace(
                base.vision,
                # "none" is a real answer here, so an unknown value must not
                # silently become "auto": a deployment that switched the model
                # off would otherwise get it back by typo.
                provider=_vision_provider_setting(
                    os.getenv("NOVACONTROL_VISION_PROVIDER"), base.vision.provider
                ),
                model=(
                    os.getenv("NOVACONTROL_VISION_MODEL", base.vision.model).strip()
                    or base.vision.model
                ),
                prefer_ocr=_parse_bool(
                    os.getenv("NOVACONTROL_VISION_PREFER_OCR"),
                    default=base.vision.prefer_ocr,
                ),
            ),
        )


def _mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"Expected mapping, got {type(value).__name__}")
    return dict(value)


#: The spellings an operator may use for a boolean environment variable. Both
#: directions are listed so an explicitly written "off" is obeyed while a typo
#: is not mistaken for one.
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def _parse_bool(value: str | None, *, default: bool) -> bool:
    """Read a boolean switch, keeping the current value on unusable input.

    An explicitly written value — 1/true/yes/on, or 0/false/no/off — is obeyed.
    Anything else (a typo, a stray quote, an empty string) keeps the default
    rather than reading as "off": for a switch like ``training_dry_run`` or
    ``evaluation_redact_sensitive`` the default is the cautious answer, and a
    misspelling must not be the thing that turns it into the permissive one.
    This is the same rule ``_count_value`` and ``_days_value`` already apply to
    numbers, and the one the settings sections document.
    """
    if value is None:
        return default
    text = value.strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return default


def _count_value(value: str | None, default: int, *, maximum: int) -> int:
    """Parse a positive whole-number setting, clamped to its ceiling.

    Unusable input (not a number, zero, negative) keeps the default; a number
    above the ceiling is clamped, because "stop after 10 000 cycles" is not a
    configuration, it is an unbounded loop written down.
    """
    if value is None or not str(value).strip():
        return default
    try:
        parsed = int(str(value).strip())
    except ValueError:
        return default
    if parsed < 1:
        return default
    return min(parsed, maximum)


def _count_setting(data: Mapping[str, Any], key: str, default: int, *, maximum: int) -> int:
    """The mapping-side twin of :func:`_count_value`."""
    value = data.get(key)
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < 1:
        return default
    return min(parsed, maximum)


def _tick_value(value: Any, default: float) -> float:
    """An automation tick cadence, clamped to the range that still works.

    Below the floor the scheduler is a busy loop; above the ceiling a schedule
    fires so late that it has stopped being the schedule that was asked for. Both
    are clamped rather than obeyed, and unusable input keeps the default.
    """
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return min(
        max(parsed, MIN_AUTOMATION_TICK_SECONDS), MAX_AUTOMATION_TICK_SECONDS
    )


def _vision_provider_setting(value: str | None, default: str) -> str:
    """The configured vision provider, ignoring anything outside the vocabulary."""
    if value is None:
        return default
    candidate = value.strip().lower()
    return candidate if candidate in _VISION_PROVIDERS else default


def _parse_float(value: str | None, *, default: float) -> float:
    """Parse a 0..1 setting, ignoring unusable input rather than failing boot."""
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if 0.0 <= parsed <= 1.0 else default


def _seconds_value(value: str | None, default: float) -> float:
    """Parse a timeout in seconds; unusable input keeps the default."""
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _float_value(value: Any, default: float) -> float:
    """Parse an unconstrained float setting (timeouts, sizes) from config."""
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _float_setting(data: Mapping[str, Any], key: str, default: float) -> float:
    value = data.get(key)
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if 0.0 <= parsed <= 1.0 else default


def _days_value(value: str | None, default: int) -> int:
    """A retention period in days, where 0 legitimately means "no age limit"."""
    if value is None or not str(value).strip():
        return default
    try:
        parsed = int(str(value).strip())
    except ValueError:
        return default
    if parsed < 0:
        return default
    return min(parsed, MAX_EVALUATION_RETENTION_DAYS)


def _weight_setting(value: Any, default: Mapping[str, float]) -> Mapping[str, float]:
    """Reward weights from configuration: finite numbers only, others ignored.

    A weight may be zero ("this factor does not count here") and may be large;
    what it may not be is ``nan`` or an infinity, because a reward nobody can
    compare is worse than a reward with the wrong weight. An unusable entry is
    dropped rather than defaulted, so the engine's own default applies to it.
    """
    if not isinstance(value, Mapping):
        return {}
    cleaned: dict[str, float] = {}
    for key, item in value.items():
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            continue
        number = float(item)
        if number != number or number in {float("inf"), float("-inf")}:
            continue
        cleaned[str(key)] = number
    return cleaned or dict(default)


def _bool_setting(data: Mapping[str, Any], key: str, default: bool) -> bool:
    value = data.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
