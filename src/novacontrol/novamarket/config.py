"""NovaMarket configuration: validated, versioned and secret-free.

Configuration is a contract in its own right. The rules encoded here are:

* **Unknown fields are refused.** A typo in a persisted payload must not be
  silently ignored, because "I configured it" and "the subsystem never saw it"
  must not look the same.
* **Secrets never live in this file.** A key that looks like a credential is
  rejected with instructions to use a *reference* (``env:NAME`` /
  ``store:NAME``) instead. NovaMarket resolves none of those references in
  NM-01; it records them so the phase that needs a provider key uses the
  operator's existing secret channel rather than inventing a weak one here.
* **The stored shape is versioned.** A payload from a newer schema or contract
  is refused, not best-effort parsed.
* **Only the observe mode exists.** ``paper`` is a recognised future mode and
  is refused with the phase that will introduce it; ``live`` is refused
  outright, because NM-01 enables no real-money trading at all.
* **Bounds are real.** Every numeric limit has a minimum and a maximum, so a
  hand-edited file cannot ask for an unbounded history or zero assets.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar

from novacontrol.novamarket.contracts import (
    ConfigurationError,
    MARKET_CONTRACT_VERSION,
    MARKET_SCHEMA_VERSION,
)
from novacontrol.novamarket.marketdata.freshness import FreshnessSettings
from novacontrol.novamarket.marketdata.quality import QualitySettings
from novacontrol.novamarket.marketdata.settings import (
    CacheSettings,
    IngestionSettings,
)

#: The operational mode this build implements. ``paper`` and ``live`` are
#: deliberately absent from the allowed set; reserved modes are named below.
OPERATIONAL_MODES: tuple[str, ...] = ("observe",)

#: Modes a later phase will introduce, and the phase (or reason) responsible.
RESERVED_MODES: dict[str, str] = {
    "paper": "NM-10 (Realistic Paper Execution)",
    "live": "no NovaMarket phase enables real-money trading; it is out of scope",
}

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

#: Key fragments that mean "a credential value is being stored here".
_SECRET_MARKERS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "passwd",
    "credential_value",
    "private_key",
    "access_key",
    "broker_key",
)

_PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# A secret-store key is a name, not an environment variable: dots and dashes are
# ordinary there, while an environment name may not contain them.
_STORE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_INSTRUMENT_ID = re.compile(r"^[A-Z0-9]{2,12}:[A-Za-z0-9^&=._/-]{1,64}$")
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,63}$")


@dataclass(frozen=True, slots=True)
class SecretReference:
    """A pointer to a secret, never the secret itself.

    ``env:NAME`` means the operator supplies the value through the environment;
    ``store:NAME`` means their existing secret store. NovaMarket records which
    channel and which name, so a later phase can ask for the value at the moment
    a provider is used — and no API response, log line or configuration file ever
    contains the value.
    """

    scheme: str
    name: str

    SCHEMES: ClassVar[tuple[str, ...]] = ("env", "store")

    def __post_init__(self) -> None:
        if self.scheme not in self.SCHEMES:
            raise ConfigurationError(
                f"unsupported secret reference scheme {self.scheme!r}; "
                f"use one of {', '.join(self.SCHEMES)}"
            )
        pattern = _ENVIRONMENT_NAME if self.scheme == "env" else _STORE_NAME
        if not pattern.match(self.name):
            example = (
                "NOVAMARKET_DATA_KEY" if self.scheme == "env" else "market-data"
            )
            raise ConfigurationError(
                f"invalid {self.scheme} secret reference name; expected a "
                f"name such as {example} (the reference is a name, never a value)"
            )

    @classmethod
    def parse(cls, value: object) -> SecretReference:
        """Parse ``scheme:name``; refuse anything that looks like a literal key."""
        text = str(value or "").strip()
        if not text:
            raise ConfigurationError("a secret reference must not be blank")
        if ":" not in text:
            raise ConfigurationError(
                "credentials_ref must be a reference like "
                "'env:NOVAMARKET_DATA_KEY' or 'store:market-data', not a credential "
                "value"
            )
        scheme, _, name = text.partition(":")
        return cls(scheme.strip().lower(), name.strip())

    def to_dict(self) -> dict[str, str]:
        return {"scheme": self.scheme, "name": self.name}

    def __str__(self) -> str:
        return f"{self.scheme}:{self.name}"


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """What NovaMarket may hold or fetch at once.

    The ceilings are advisory for NM-01 (it fetches nothing) and binding for
    NM-02 (the collection phase), which is why they are validated now: a limit
    introduced after data starts arriving is a migration, not a setting.
    """

    max_assets: int = 50
    max_quotes_per_asset: int = 500
    max_concurrent_fetches: int = 2

    MIN_ASSETS = 1
    MAX_ASSETS = 10_000
    MIN_QUOTES = 1
    MAX_QUOTES = 100_000
    MIN_FETCHES = 1
    MAX_FETCHES = 16

    def __post_init__(self) -> None:
        for name, value, low, high in (
            ("max_assets", self.max_assets, self.MIN_ASSETS, self.MAX_ASSETS),
            (
                "max_quotes_per_asset",
                self.max_quotes_per_asset,
                self.MIN_QUOTES,
                self.MAX_QUOTES,
            ),
            (
                "max_concurrent_fetches",
                self.max_concurrent_fetches,
                self.MIN_FETCHES,
                self.MAX_FETCHES,
            ),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigurationError(f"resources.{name} must be an integer")
            if not low <= value <= high:
                raise ConfigurationError(
                    f"resources.{name} must be between {low} and {high}, got {value}"
                )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> ResourceLimits:
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ConfigurationError("resources must be an object")
        allowed = {"max_assets", "max_quotes_per_asset", "max_concurrent_fetches"}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ConfigurationError(
                f"unknown resource limit field(s): {', '.join(unknown)}; "
                f"allowed: {', '.join(sorted(allowed))}"
            )
        # Instance defaults for the same slots reason as NovaMarketConfig: the
        # class attribute of a defaulted slotted field is a descriptor.
        defaults = cls()
        return cls(
            max_assets=data.get("max_assets", defaults.max_assets),
            max_quotes_per_asset=data.get(
                "max_quotes_per_asset", defaults.max_quotes_per_asset
            ),
            max_concurrent_fetches=data.get(
                "max_concurrent_fetches", defaults.max_concurrent_fetches
            ),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_assets": self.max_assets,
            "max_quotes_per_asset": self.max_quotes_per_asset,
            "max_concurrent_fetches": self.max_concurrent_fetches,
        }


def _has_secret_shaped_key(key: str) -> bool:
    lowered = str(key).strip().lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


@dataclass(frozen=True, slots=True)
class NovaMarketConfig:
    """The resolved NovaMarket configuration.

    Construction validates everything; there is no second "validate" step that a
    caller might forget. ``from_mapping`` is the only door from persisted or API
    input, and it rejects unknown fields instead of ignoring them.
    """

    enabled: bool = True
    mode: str = "observe"
    data_providers: tuple[str, ...] = ()
    credentials_ref: str = ""
    provider_credentials: Mapping[str, str] = field(default_factory=dict)
    universe: tuple[str, ...] = ()
    stale_after_seconds: int = 900
    resources: ResourceLimits = field(default_factory=ResourceLimits)
    ingestion: IngestionSettings = field(default_factory=IngestionSettings)
    freshness: FreshnessSettings = field(default_factory=FreshnessSettings)
    quality: QualitySettings = field(default_factory=QualitySettings)
    cache: CacheSettings = field(default_factory=CacheSettings)
    history_page_limit: int = 200
    log_level: str = "INFO"
    emit_events: bool = True
    # Operator notifications. The recipient is not a secret; the SMTP
    # credential is read from the server environment and never stored here.
    notification_email: str = ""
    notifications_enabled: bool = True
    notify_market_open: bool = True
    notify_market_close: bool = True
    profit_alert_pct: float = 5.0
    loss_alert_pct: float = 3.0
    alert_lead_minutes: int = 10
    paused: bool = False
    contract_version: str = MARKET_CONTRACT_VERSION
    schema_version: int = MARKET_SCHEMA_VERSION

    #: Exactly the fields a payload may carry.
    FIELDS: ClassVar[tuple[str, ...]] = (
        "enabled",
        "mode",
        "data_providers",
        "credentials_ref",
        "provider_credentials",
        "universe",
        "stale_after_seconds",
        "resources",
        "ingestion",
        "freshness",
        "quality",
        "cache",
        "history_page_limit",
        "log_level",
        "emit_events",
        "notification_email",
        "notifications_enabled",
        "notify_market_open",
        "notify_market_close",
        "profit_alert_pct",
        "loss_alert_pct",
        "alert_lead_minutes",
        "paused",
        "contract_version",
        "schema_version",
    )

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigurationError("enabled must be a boolean")
        if not isinstance(self.emit_events, bool):
            raise ConfigurationError("emit_events must be a boolean")
        for name in (
            "notifications_enabled",
            "notify_market_open",
            "notify_market_close",
            "paused",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ConfigurationError(f"{name} must be a boolean")
        email = str(self.notification_email or "").strip()
        if email and not _EMAIL.match(email):
            raise ConfigurationError(
                "notification_email must be one plain email address, or blank"
            )
        object.__setattr__(self, "notification_email", email)
        for name in ("profit_alert_pct", "loss_alert_pct"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigurationError(f"{name} must be a number")
            if not 0.1 <= float(value) <= 100.0:
                raise ConfigurationError(f"{name} must be between 0.1 and 100")
            object.__setattr__(self, name, float(value))
        if isinstance(self.alert_lead_minutes, bool) or not isinstance(
            self.alert_lead_minutes, int
        ):
            raise ConfigurationError("alert_lead_minutes must be an integer")
        if not 5 <= self.alert_lead_minutes <= 30:
            raise ConfigurationError("alert_lead_minutes must be between 5 and 30")

        mode = str(self.mode or "").strip().lower()
        if mode in RESERVED_MODES:
            raise ConfigurationError(
                f"mode {mode!r} is reserved: {RESERVED_MODES[mode]}"
            )
        if mode not in OPERATIONAL_MODES:
            raise ConfigurationError(
                f"unknown mode {mode!r}; this build implements: "
                f"{', '.join(OPERATIONAL_MODES)}"
            )
        object.__setattr__(self, "mode", mode)

        providers = self.data_providers
        if isinstance(providers, str) or not isinstance(providers, Sequence):
            raise ConfigurationError(
                "data_providers must be a list of provider names, not "
                f"{type(providers).__name__}"
            )
        cleaned: list[str] = []
        lowered: set[str] = set()
        for provider in providers:
            name = str(provider or "").strip().lower()
            if not _PROVIDER_NAME.match(name):
                raise ConfigurationError(
                    "invalid data provider reference; use a short name such as "
                    "'nse-bhavcopy', not a URL or credentials"
                )
            if name in lowered:
                raise ConfigurationError(f"duplicate data provider: {name}")
            lowered.add(name)
            cleaned.append(name)
        if len(cleaned) > 8:
            raise ConfigurationError("at most 8 data providers may be declared")
        object.__setattr__(self, "data_providers", tuple(cleaned))

        creds = self.provider_credentials
        if not isinstance(creds, Mapping):
            raise ConfigurationError("provider_credentials must be an object")
        parsed_creds: dict[str, str] = {}
        for provider, reference in creds.items():
            key = str(provider or "").strip().lower()
            if not _PROVIDER_NAME.match(key):
                raise ConfigurationError(
                    f"invalid provider name in provider_credentials: {provider!r}"
                )
            if key in parsed_creds:
                raise ConfigurationError(
                    f"duplicate provider credential entry: {key}"
                )
            text = str(reference or "").strip()
            SecretReference.parse(text)
            parsed_creds[key] = text
        if len(parsed_creds) > 8:
            raise ConfigurationError(
                "at most 8 provider credential references may be configured"
            )
        object.__setattr__(self, "provider_credentials", parsed_creds)

        universe = self.universe
        if isinstance(universe, str) or not isinstance(universe, Sequence):
            raise ConfigurationError(
                "universe must be a list of instrument ids, not "
                f"{type(universe).__name__}"
            )
        cleaned_universe: list[str] = []
        seen_instruments: set[str] = set()
        for value in universe:
            instrument_id = str(value or "").strip()
            if not _INSTRUMENT_ID.match(instrument_id):
                raise ConfigurationError(
                    "invalid universe instrument id; expected MIC:SYMBOL such as "
                    "XNSE:RELIANCE or CRYPTO:BTC-USD"
                )
            if instrument_id in seen_instruments:
                raise ConfigurationError(
                    f"duplicate universe instrument: {instrument_id}"
                )
            seen_instruments.add(instrument_id)
            cleaned_universe.append(instrument_id)
        if len(cleaned_universe) > self.resources.max_assets:
            raise ConfigurationError(
                f"universe has {len(cleaned_universe)} instruments but "
                f"resources.max_assets is {self.resources.max_assets}"
            )
        object.__setattr__(self, "universe", tuple(cleaned_universe))

        if self.credentials_ref:
            SecretReference.parse(self.credentials_ref)

        for name, expected in (
            ("ingestion", IngestionSettings),
            ("freshness", FreshnessSettings),
            ("quality", QualitySettings),
            ("cache", CacheSettings),
        ):
            if not isinstance(getattr(self, name), expected):
                raise ConfigurationError(f"{name} must be a {expected.__name__} value")
        if self.ingestion.max_concurrency > self.resources.max_concurrent_fetches:
            raise ConfigurationError(
                "ingestion.max_concurrency may not exceed "
                "resources.max_concurrent_fetches"
            )
        if isinstance(self.history_page_limit, bool) or not isinstance(
            self.history_page_limit, int
        ):
            raise ConfigurationError("history_page_limit must be an integer")
        if not 1 <= self.history_page_limit <= 1_000:
            raise ConfigurationError(
                "history_page_limit must be between 1 and 1000"
            )

        if isinstance(self.stale_after_seconds, bool) or not isinstance(
            self.stale_after_seconds, int
        ):
            raise ConfigurationError("stale_after_seconds must be an integer")
        if not 1 <= self.stale_after_seconds <= 604_800:
            raise ConfigurationError(
                "stale_after_seconds must be between 1 and 604800 (one week)"
            )

        if not isinstance(self.resources, ResourceLimits):
            raise ConfigurationError("resources must be a ResourceLimits value")

        level = str(self.log_level or "").strip().upper()
        if level not in _LOG_LEVELS:
            raise ConfigurationError(
                f"log_level must be one of {', '.join(_LOG_LEVELS)}, got {self.log_level!r}"
            )
        object.__setattr__(self, "log_level", level)

        if not isinstance(self.schema_version, int) or isinstance(
            self.schema_version, bool
        ):
            raise ConfigurationError("schema_version must be an integer")
        if self.schema_version != MARKET_SCHEMA_VERSION:
            raise ConfigurationError(
                f"configuration schema {self.schema_version} is not supported; "
                f"this build reads schema {MARKET_SCHEMA_VERSION}"
            )
        if not isinstance(self.contract_version, str):
            raise ConfigurationError("contract_version must be a string")
        if self.contract_version != MARKET_CONTRACT_VERSION:
            raise ConfigurationError(
                f"configuration contract {self.contract_version!r} is not "
                f"supported; this build speaks {MARKET_CONTRACT_VERSION}"
            )

    # -- serialization --------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The configuration as it may be persisted or served (no secret values)."""
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "data_providers": list(self.data_providers),
            "credentials_ref": self.credentials_ref,
            "provider_credentials": dict(self.provider_credentials),
            "universe": list(self.universe),
            "stale_after_seconds": self.stale_after_seconds,
            "resources": self.resources.to_dict(),
            "ingestion": self.ingestion.to_dict(),
            "freshness": self.freshness.to_dict(),
            "quality": self.quality.to_dict(),
            "cache": self.cache.to_dict(),
            "history_page_limit": self.history_page_limit,
            "log_level": self.log_level,
            "emit_events": self.emit_events,
            "notification_email": self.notification_email,
            "notifications_enabled": self.notifications_enabled,
            "notify_market_open": self.notify_market_open,
            "notify_market_close": self.notify_market_close,
            "profit_alert_pct": self.profit_alert_pct,
            "loss_alert_pct": self.loss_alert_pct,
            "alert_lead_minutes": self.alert_lead_minutes,
            "paused": self.paused,
            "contract_version": self.contract_version,
            "schema_version": self.schema_version,
        }

    def diff(self, other: NovaMarketConfig) -> tuple[str, ...]:
        """The field names whose effective values changed."""
        mine, theirs = self.to_dict(), other.to_dict()
        return tuple(sorted(name for name in mine if mine[name] != theirs[name]))

    def with_changes(self, **changes: Any) -> NovaMarketConfig:
        """A re-validated copy — the supported way to change one field."""
        return replace(self, **changes)

    # -- loading --------------------------------------------------------------

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> NovaMarketConfig:
        """Build from persisted/API input, refusing anything unrecognised."""
        if data is None:
            return cls()
        if isinstance(data, NovaMarketConfig):
            return data
        if not isinstance(data, Mapping):
            raise ConfigurationError(
                f"configuration must be an object, not {type(data).__name__}"
            )

        for key in data:
            if _has_secret_shaped_key(str(key)):
                raise ConfigurationError(
                    f"configuration field {key!r} looks like a credential; store "
                    "secrets outside NovaMarket and reference them with "
                    "credentials_ref='env:NAME' or 'store:NAME'"
                )
        unknown = sorted(set(data) - set(cls.FIELDS))
        if unknown:
            raise ConfigurationError(
                f"unknown configuration field(s): {', '.join(unknown)}; "
                f"allowed: {', '.join(cls.FIELDS)}"
            )

        providers = data.get("data_providers", ())
        if providers is None:
            providers = ()
        if isinstance(providers, str) or not isinstance(providers, Sequence):
            # Without this, tuple("nse") would become three one-letter provider
            # names and pass validation. A string is one name, not a list.
            raise ConfigurationError(
                "data_providers must be a list of provider names, not "
                f"{type(providers).__name__}"
            )

        universe = data.get("universe", ())
        if universe is None:
            universe = ()
        if isinstance(universe, str) or not isinstance(universe, Sequence):
            raise ConfigurationError(
                "universe must be a list of instrument ids, not "
                f"{type(universe).__name__}"
            )

        credentials = data.get("provider_credentials", {})
        if credentials is None:
            credentials = {}
        if not isinstance(credentials, Mapping):
            raise ConfigurationError("provider_credentials must be an object")

        # Defaults come from an INSTANCE, not ``cls.<field>``: with ``slots=True``
        # the class attribute of a defaulted field is a slot descriptor, not the
        # default value, so a partial payload used to fail as "enabled must be a
        # boolean" rather than applying the one field it named.
        defaults = cls()
        ingestion_value = data.get("ingestion")
        freshness_value = data.get("freshness")
        quality_value = data.get("quality")
        cache_value = data.get("cache")
        return cls(
            enabled=data.get("enabled", defaults.enabled),
            mode=data.get("mode", defaults.mode),
            data_providers=tuple(providers),
            credentials_ref=str(data.get("credentials_ref", "") or ""),
            provider_credentials=dict(credentials),
            universe=tuple(universe),
            stale_after_seconds=data.get(
                "stale_after_seconds", defaults.stale_after_seconds
            ),
            resources=ResourceLimits.from_mapping(data.get("resources")),
            ingestion=(
                ingestion_value
                if isinstance(ingestion_value, IngestionSettings)
                else IngestionSettings.from_mapping(ingestion_value)
            ),
            freshness=(
                freshness_value
                if isinstance(freshness_value, FreshnessSettings)
                else FreshnessSettings.from_mapping(freshness_value)
            ),
            quality=(
                quality_value
                if isinstance(quality_value, QualitySettings)
                else QualitySettings.from_mapping(quality_value)
            ),
            cache=(
                cache_value
                if isinstance(cache_value, CacheSettings)
                else CacheSettings.from_mapping(cache_value)
            ),
            history_page_limit=data.get(
                "history_page_limit", defaults.history_page_limit
            ),
            log_level=data.get("log_level", defaults.log_level),
            emit_events=data.get("emit_events", defaults.emit_events),
            notification_email=data.get(
                "notification_email", defaults.notification_email
            ),
            notifications_enabled=data.get(
                "notifications_enabled", defaults.notifications_enabled
            ),
            notify_market_open=data.get(
                "notify_market_open", defaults.notify_market_open
            ),
            notify_market_close=data.get(
                "notify_market_close", defaults.notify_market_close
            ),
            profit_alert_pct=data.get("profit_alert_pct", defaults.profit_alert_pct),
            loss_alert_pct=data.get("loss_alert_pct", defaults.loss_alert_pct),
            alert_lead_minutes=data.get(
                "alert_lead_minutes", defaults.alert_lead_minutes
            ),
            paused=data.get("paused", defaults.paused),
            contract_version=data.get("contract_version", MARKET_CONTRACT_VERSION),
            schema_version=data.get("schema_version", MARKET_SCHEMA_VERSION),
        )

    @staticmethod
    def schema() -> tuple[dict[str, Any], ...]:
        """The configuration contract as data, served by ``/market/contracts``."""
        return (
            {"field": "enabled", "type": "boolean", "default": True, "notes": "operator on/off"},
            {
                "field": "mode",
                "type": "string",
                "default": "observe",
                "notes": f"one of {', '.join(OPERATIONAL_MODES)}; reserved: "
                + ", ".join(f"{mode} ({why})" for mode, why in RESERVED_MODES.items()),
            },
            {
                "field": "data_providers",
                "type": "list[string]",
                "default": [],
                "notes": "provider references only (max 8); no URLs or credentials",
            },
            {
                "field": "credentials_ref",
                "type": "string",
                "default": "",
                "notes": "env:NAME or store:NAME; never a credential value",
            },
            {
                "field": "provider_credentials",
                "type": "object",
                "default": {},
                "notes": "provider name -> secret reference; values are references, "
                "never credentials",
            },
            {
                "field": "universe",
                "type": "list[string]",
                "default": [],
                "notes": "instrument ids (MIC:SYMBOL) the scheduled worker collects; "
                "bounded by resources.max_assets",
            },
            {
                "field": "stale_after_seconds",
                "type": "integer",
                "default": 900,
                "notes": "1..604800; NM-01's coarse window retained for contract "
                "compatibility. NM-02 freshness rules in 'freshness' are authoritative.",
            },
            {
                "field": "resources",
                "type": "object",
                "default": ResourceLimits().to_dict(),
                "notes": "max_assets 1..10000, max_quotes_per_asset 1..100000, "
                "max_concurrent_fetches 1..16",
            },
            {
                "field": "ingestion",
                "type": "object",
                "default": IngestionSettings().to_dict(),
                "notes": "background polling; off by default",
            },
            {
                "field": "freshness",
                "type": "object",
                "default": FreshnessSettings().to_dict(),
                "notes": "asset/interval-aware freshness windows",
            },
            {
                "field": "quality",
                "type": "object",
                "default": QualitySettings().to_dict(),
                "notes": "anomaly and reconciliation thresholds",
            },
            {
                "field": "notification_email",
                "type": "string",
                "default": "",
                "notes": "one operator email address, or blank",
            },
            {
                "field": "notifications_enabled",
                "type": "boolean",
                "default": True,
                "notes": "master switch for email notifications",
            },
            {
                "field": "notify_market_open",
                "type": "boolean",
                "default": True,
                "notes": "send the NSE/BSE pre-open alert",
            },
            {
                "field": "notify_market_close",
                "type": "boolean",
                "default": True,
                "notes": "send the NSE/BSE pre-close alert",
            },
            {
                "field": "profit_alert_pct",
                "type": "number",
                "default": 5.0,
                "notes": "per-stock profit threshold, percent of average cost",
            },
            {
                "field": "loss_alert_pct",
                "type": "number",
                "default": 3.0,
                "notes": "per-stock loss threshold, percent of average cost",
            },
            {
                "field": "alert_lead_minutes",
                "type": "integer",
                "default": 10,
                "notes": "minutes before an NSE/BSE open or close to send the alert (5..30)",
            },
            {
                "field": "paused",
                "type": "boolean",
                "default": False,
                "notes": "pause the continuous monitor without disabling the API or records",
            },
            {
                "field": "cache",
                "type": "object",
                "default": CacheSettings().to_dict(),
                "notes": "quote/instrument/provider cache lifetimes",
            },
            {
                "field": "history_page_limit",
                "type": "integer",
                "default": 200,
                "notes": "default page size for history reads (1..1000)",
            },
            {
                "field": "log_level",
                "type": "string",
                "default": "INFO",
                "notes": f"one of {', '.join(_LOG_LEVELS)}",
            },
            {"field": "emit_events", "type": "boolean", "default": True, "notes": "publish lifecycle events"},
            {
                "field": "contract_version",
                "type": "string",
                "default": MARKET_CONTRACT_VERSION,
                "notes": "must match the running build",
            },
            {
                "field": "schema_version",
                "type": "integer",
                "default": MARKET_SCHEMA_VERSION,
                "notes": "must match the running build",
            },
        )


__all__ = [
    "NovaMarketConfig",
    "OPERATIONAL_MODES",
    "RESERVED_MODES",
    "ResourceLimits",
    "SecretReference",
]
