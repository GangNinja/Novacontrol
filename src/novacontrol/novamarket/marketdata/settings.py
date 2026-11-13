"""Settings for background ingestion and the market-data cache.

The defaults are deliberately quiet: ingestion is OFF, and when it is switched on
it polls every 15 minutes rather than in a tight loop. A market-data worker that
starts itself and polls continuously is the kind of background load nobody asked
for; this phase makes enabling it an explicit operator decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from novacontrol.novamarket.contracts import ConfigurationError
from novacontrol.novamarket.marketdata.models import DataInterval

#: Intervals a scheduled ingestion cycle may request. SPOT is a quote, not a bar,
#: and is controlled by ``include_quotes``.
_ALLOWED_INTERVALS = (
    DataInterval.MINUTE_5,
    DataInterval.MINUTE_15,
    DataInterval.MINUTE_30,
    DataInterval.HOUR_1,
    DataInterval.DAY_1,
    DataInterval.WEEK_1,
    DataInterval.MONTH_1,
)


@dataclass(frozen=True, slots=True)
class IngestionSettings:
    """When and how the background market-data worker runs."""

    enabled: bool = False
    poll_seconds: int = 900
    intervals: tuple[str, ...] = (DataInterval.DAY_1.value,)
    include_quotes: bool = True
    include_bars: bool = True
    run_when_closed: bool = False
    allow_partial_bars: bool = False
    max_concurrency: int = 2
    history_limit: int = 250
    jitter_seconds: int = 30

    # Class-level table, never an instance field: constructing a default
    # instance inside ``__post_init__`` to read it was infinite recursion.
    LIMITS: ClassVar[Mapping[str, tuple[int, int]]] = {
        "poll_seconds": (15, 86_400),
        "max_concurrency": (1, 16),
        "history_limit": (1, 5_000),
        "jitter_seconds": (0, 300),
    }

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigurationError("ingestion.enabled must be a boolean")
        for name in (
            "include_quotes",
            "include_bars",
            "run_when_closed",
            "allow_partial_bars",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ConfigurationError(f"ingestion.{name} must be a boolean")
        if not self.include_quotes and not self.include_bars:
            raise ConfigurationError(
                "ingestion must include quotes, bars, or both; a cycle that "
                "collects nothing is not a schedule"
            )
        for name, (low, high) in self.LIMITS.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigurationError(f"ingestion.{name} must be an integer")
            if not low <= value <= high:
                raise ConfigurationError(
                    f"ingestion.{name} must be between {low} and {high}, got {value}"
                )
        intervals = self.intervals
        if isinstance(intervals, str) or not isinstance(intervals, (tuple, list)):
            raise ConfigurationError("ingestion.intervals must be a list")
        if not intervals:
            raise ConfigurationError("ingestion.intervals must not be empty")
        cleaned: list[str] = []
        for value in intervals:
            try:
                interval = DataInterval(value)
            except ValueError as exc:
                allowed = ", ".join(item.value for item in _ALLOWED_INTERVALS)
                raise ConfigurationError(
                    f"ingestion interval {value!r} is not supported; allowed: "
                    f"{allowed}"
                ) from exc
            if interval not in _ALLOWED_INTERVALS:
                allowed = ", ".join(item.value for item in _ALLOWED_INTERVALS)
                raise ConfigurationError(
                    f"ingestion interval {interval.value!r} cannot be scheduled; "
                    f"allowed: {allowed}"
                )
            if interval.value in cleaned:
                raise ConfigurationError(
                    f"duplicate ingestion interval: {interval.value}"
                )
            cleaned.append(interval.value)
        if len(cleaned) > 4:
            raise ConfigurationError("at most 4 ingestion intervals may be configured")
        object.__setattr__(self, "intervals", tuple(cleaned))

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> IngestionSettings:
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ConfigurationError("ingestion must be an object")
        defaults = cls()
        allowed = {
            "enabled",
            "poll_seconds",
            "intervals",
            "include_quotes",
            "include_bars",
            "run_when_closed",
            "allow_partial_bars",
            "max_concurrency",
            "history_limit",
            "jitter_seconds",
        }
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ConfigurationError(
                f"unknown ingestion field(s): {', '.join(unknown)}; "
                f"allowed: {', '.join(sorted(allowed))}"
            )
        return cls(
            enabled=data.get("enabled", defaults.enabled),
            poll_seconds=data.get("poll_seconds", defaults.poll_seconds),
            intervals=tuple(data.get("intervals", defaults.intervals)),
            include_quotes=data.get("include_quotes", defaults.include_quotes),
            include_bars=data.get("include_bars", defaults.include_bars),
            run_when_closed=data.get("run_when_closed", defaults.run_when_closed),
            allow_partial_bars=data.get(
                "allow_partial_bars", defaults.allow_partial_bars
            ),
            max_concurrency=data.get("max_concurrency", defaults.max_concurrency),
            history_limit=data.get("history_limit", defaults.history_limit),
            jitter_seconds=data.get("jitter_seconds", defaults.jitter_seconds),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "poll_seconds": self.poll_seconds,
            "intervals": list(self.intervals),
            "include_quotes": self.include_quotes,
            "include_bars": self.include_bars,
            "run_when_closed": self.run_when_closed,
            "allow_partial_bars": self.allow_partial_bars,
            "max_concurrency": self.max_concurrency,
            "history_limit": self.history_limit,
            "jitter_seconds": self.jitter_seconds,
        }

    @staticmethod
    def schema() -> dict[str, Any]:
        defaults = IngestionSettings()
        return {
            "fields": {
                "enabled": {"type": "boolean", "default": defaults.enabled},
                "poll_seconds": {
                    "type": "integer",
                    "default": defaults.poll_seconds,
                    "min": 15,
                    "max": 86400,
                },
                "intervals": {
                    "type": "list[string]",
                    "default": list(defaults.intervals),
                    "allowed": [item.value for item in _ALLOWED_INTERVALS],
                },
                "include_quotes": {"type": "boolean", "default": defaults.include_quotes},
                "include_bars": {"type": "boolean", "default": defaults.include_bars},
                "run_when_closed": {
                    "type": "boolean",
                    "default": defaults.run_when_closed,
                },
                "allow_partial_bars": {
                    "type": "boolean",
                    "default": defaults.allow_partial_bars,
                },
                "max_concurrency": {
                    "type": "integer",
                    "default": defaults.max_concurrency,
                    "min": 1,
                    "max": 16,
                },
                "history_limit": {
                    "type": "integer",
                    "default": defaults.history_limit,
                    "min": 1,
                    "max": 5000,
                },
                "jitter_seconds": {
                    "type": "integer",
                    "default": defaults.jitter_seconds,
                    "min": 0,
                    "max": 300,
                },
            },
            "rules": (
                "Ingestion is off by default. A cycle is skipped for a closed "
                "exchange unless run_when_closed is true; crypto is continuous. "
                "Partial bars are not stored unless allow_partial_bars is true. "
                "The worker never overlaps two cycles."
            ),
        }


@dataclass(frozen=True, slots=True)
class CacheSettings:
    """Cache lifetimes, separated by what is cached."""

    quote_ttl_seconds: int = 15
    instrument_ttl_seconds: int = 300
    provider_ttl_seconds: int = 60

    LIMITS: ClassVar[Mapping[str, tuple[int, int]]] = {
        "quote_ttl_seconds": (0, 3_600),
        "instrument_ttl_seconds": (1, 86_400),
        "provider_ttl_seconds": (1, 86_400),
    }

    def __post_init__(self) -> None:
        for name, (low, high) in self.LIMITS.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigurationError(f"cache.{name} must be an integer")
            if not low <= value <= high:
                raise ConfigurationError(
                    f"cache.{name} must be between {low} and {high}, got {value}"
                )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> CacheSettings:
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ConfigurationError("cache must be an object")
        defaults = cls()
        allowed = set(cls.LIMITS)
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ConfigurationError(
                f"unknown cache field(s): {', '.join(unknown)}; "
                f"allowed: {', '.join(sorted(allowed))}"
            )
        return cls(
            **{
                name: data.get(name, getattr(defaults, name))
                for name in allowed
            }
        )

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.LIMITS}

    @staticmethod
    def schema() -> dict[str, Any]:
        return {
            "fields": {
                name: {"min": low, "max": high}
                for name, (low, high) in CacheSettings.LIMITS.items()
            },
            "rules": (
                "Latest-quote results are cached for quote_ttl_seconds but "
                "freshness is recomputed on every read, so a cached record is "
                "never presented as fresher than it is. Instrument metadata and "
                "provider capability snapshots have their own longer lifetimes. "
                "Historical observations are read from the indexed store, not a "
                "second cache."
            ),
        }


__all__ = ["CacheSettings", "IngestionSettings"]
