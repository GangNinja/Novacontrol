"""Configuration loading for NovaControl."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
from typing import Any, Mapping


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
    def from_mapping(cls, data: Mapping[str, Any]) -> "NluSettings":
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
    vision: VisionSettings = field(default_factory=VisionSettings)
    models: ModelSettings = field(default_factory=ModelSettings)
    modules: Mapping[str, ModuleSettings] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "NovaControlConfig":
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
            vision=VisionSettings.from_mapping(_mapping(data.get("vision", {}))),
            models=ModelSettings.from_mapping(_mapping(data.get("models", {}))),
            modules=modules,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "NovaControlConfig":
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
    def from_environment(cls) -> "NovaControlConfig":
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


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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


def _bool_setting(data: Mapping[str, Any], key: str, default: bool) -> bool:
    value = data.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
