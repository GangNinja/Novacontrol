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
                    os.getenv("NOVACONTROL_NLU_VERIFY_CONFIDENCE"), default=base.nlu.verify_confidence
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
                    os.getenv("NOVACONTROL_NLU_SEMANTIC_MATCHING"), default=base.nlu.semantic_matching
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


def _parse_float(value: str | None, *, default: float) -> float:
    """Parse a 0..1 setting, ignoring unusable input rather than failing boot."""
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if 0.0 <= parsed <= 1.0 else default


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
