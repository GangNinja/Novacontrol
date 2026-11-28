"""PI-20 Prediction Intelligence Dashboard contracts.

The dashboard is a read-only composition layer.  These models describe the
filters and status vocabulary it may expose; no model in this module contains an
order, fill, position, ledger, risk-approval or autonomy field.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

DASHBOARD_PHASE = "PI-20"
DASHBOARD_CONTRACT_VERSION = "pi20.dashboard.1.0"
DASHBOARD_SCHEMA_VERSION = 1
DASHBOARD_CONFIG_LIMIT = 64_000
DASHBOARD_NAME_LIMIT = 80

RISK_AUTHORITY = "NM-08"
EXECUTION_AUTHORITY = "NM-10"

_INSTRUMENT = re.compile(r"^[A-Z0-9]{2,12}:[A-Za-z0-9^&=._/-]{1,64}$")
_NAME = re.compile(r"[^a-zA-Z0-9._:-]+")


class DashboardError(ValueError):
    """Base class for a dashboard refusal."""


class DashboardValidationError(DashboardError):
    """The query, filter or configuration is invalid."""


class DashboardNotFoundError(LookupError):
    """A requested dashboard source record does not exist."""


class DashboardStoreError(RuntimeError):
    """The isolated dashboard preference store failed."""


class DataStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    PARTIAL_DATA = "PARTIAL_DATA"
    STALE = "STALE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


HORIZONS = ("1m", "5m", "15m", "20m", "1h", "1d", "3d", "5d", "30d", "90d")
# Older notes called the one-hour contract 60m; the forecast engine emits 1h.
HORIZON_ALIASES = {"60m": "1h"}
HORIZON_FAMILIES = {
    "5m": "intraday",
    "15m": "intraday",
    "20m": "intraday",
    "1h": "intraday",
    "1d": "swing",
    "3d": "swing",
    "5d": "swing",
    "30d": "long_term",
    "90d": "long_term",
}
DIRECTIONS = ("up", "down", "neutral", "uncertain", "invalid")
DECISION_ACTIONS = ("buy", "sell", "hold", "wait", "no_action")
FORECAST_ENGINES = ("statistical", "gradient", "lstm", "ensemble")


def _text(value: object, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]


def _instrument(value: object) -> str:
    text = _text(value, 80).upper()
    if not text:
        return ""
    if not _INSTRUMENT.fullmatch(text):
        raise DashboardValidationError("instrument_id must be a MIC-qualified symbol")
    return text


def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(str(value)))
    except (TypeError, ValueError) as exc:
        raise DashboardValidationError(f"{name} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise DashboardValidationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return int(parsed)


def _choice(value: object, name: str, allowed: tuple[str, ...]) -> str:
    text = _text(value, 40).lower()
    if not text:
        return ""
    if text not in allowed:
        raise DashboardValidationError(f"unknown {name} {text!r}")
    return text


def _day(value: object, name: str) -> str:
    text = _text(value, 40)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DashboardValidationError(f"{name} must be an ISO date") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.date().isoformat()


@dataclass(frozen=True, slots=True)
class DashboardFilters:
    """Bounded dashboard filters shared by the API and saved workspace."""

    instrument_id: str = ""
    exchange: str = ""
    sector: str = ""
    regime: str = ""
    horizon: str = ""
    model_id: str = ""
    direction: str = ""
    validity: str = ""
    evaluation_start: str = ""
    evaluation_end: str = ""
    decision_status: str = ""
    agent_id: str = ""
    portfolio_id: str = ""
    research_status: str = ""
    limit: int = 50
    offset: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", _instrument(self.instrument_id))
        object.__setattr__(self, "exchange", _text(self.exchange, 16).upper())
        object.__setattr__(self, "sector", _text(self.sector, 80))
        object.__setattr__(self, "regime", _text(self.regime, 40).lower())
        horizon = _choice(
            self.horizon, "horizon", HORIZONS + tuple(HORIZON_ALIASES)
        )
        object.__setattr__(self, "horizon", HORIZON_ALIASES.get(horizon, horizon))
        object.__setattr__(self, "model_id", _text(self.model_id, 120))
        object.__setattr__(self, "direction", _choice(self.direction, "direction", DIRECTIONS))
        object.__setattr__(self, "validity", _text(self.validity, 40).lower())
        object.__setattr__(
            self, "evaluation_start", _day(self.evaluation_start, "evaluation_start")
        )
        object.__setattr__(
            self, "evaluation_end", _day(self.evaluation_end, "evaluation_end")
        )
        object.__setattr__(
            self, "decision_status", _text(self.decision_status, 40).lower()
        )
        object.__setattr__(self, "agent_id", _text(self.agent_id, 80))
        object.__setattr__(self, "portfolio_id", _text(self.portfolio_id, 80))
        object.__setattr__(
            self, "research_status", _text(self.research_status, 40).lower()
        )
        object.__setattr__(
            self, "limit", _bounded_int(self.limit, "limit", 1, 200)
        )
        object.__setattr__(
            self, "offset", _bounded_int(self.offset, "offset", 0, 10_000)
        )
        if (
            self.evaluation_start
            and self.evaluation_end
            and self.evaluation_start > self.evaluation_end
        ):
            raise DashboardValidationError(
                "evaluation_start cannot be after evaluation_end"
            )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> DashboardFilters:
        source = payload or {}
        return cls(
            instrument_id=source.get("instrument_id") or "",
            exchange=source.get("exchange") or "",
            sector=source.get("sector") or "",
            regime=source.get("regime") or "",
            horizon=source.get("horizon") or "",
            model_id=source.get("model_id") or source.get("model") or "",
            direction=source.get("direction") or "",
            validity=source.get("validity") or "",
            evaluation_start=source.get("evaluation_start") or source.get("start") or "",
            evaluation_end=source.get("evaluation_end") or source.get("end") or "",
            decision_status=source.get("decision_status") or source.get("status") or "",
            agent_id=source.get("agent_id") or source.get("agent") or "",
            portfolio_id=source.get("portfolio_id") or source.get("portfolio") or "",
            research_status=source.get("research_status") or "",
            limit=source.get("limit", 50),
            offset=source.get("offset", 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "exchange": self.exchange,
            "sector": self.sector,
            "regime": self.regime,
            "horizon": self.horizon,
            "model_id": self.model_id,
            "direction": self.direction,
            "validity": self.validity,
            "evaluation_start": self.evaluation_start,
            "evaluation_end": self.evaluation_end,
            "decision_status": self.decision_status,
            "agent_id": self.agent_id,
            "portfolio_id": self.portfolio_id,
            "research_status": self.research_status,
            "limit": self.limit,
            "offset": self.offset,
        }


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    """A saved dashboard filter preset; it is presentation state only."""

    name: str = "default"
    filters: DashboardFilters = field(default_factory=DashboardFilters)
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def __post_init__(self) -> None:
        name = _NAME.sub("-", self.name.strip()).strip("-") or "default"
        object.__setattr__(self, "name", name[:DASHBOARD_NAME_LIMIT])

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "filters": self.filters.to_dict(),
            "updated_at": self.updated_at.isoformat(),
            "presentation_only": True,
            "financial_mutation": False,
        }


def authority_contract() -> dict[str, Any]:
    return {
        "risk_authority": RISK_AUTHORITY,
        "execution_authority": EXECUTION_AUTHORITY,
        "autonomy_authority": "PI-12",
        "dashboard_authority": "read-only composition",
        "read_only": True,
        "authorizes_action": False,
        "can_submit_orders": False,
        "can_mutate_portfolio": False,
        "can_write_ledger": False,
        "can_change_autonomy": False,
        "can_activate_policies": False,
        "broker_connected": False,
        "live_trading": False,
        "paper_only": True,
    }
