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
class ModuleSettings:
    enabled: bool = True
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NovaControlConfig:
    app: AppSettings = field(default_factory=AppSettings)
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    redis: RedisSettings = field(default_factory=RedisSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
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
