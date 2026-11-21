"""Deterministic baseline policies for PI-14 policy evaluation.

All policies are read-only consumers of a PI-13 observation and simulated
portfolio.  They cannot call an execution engine, a risk authority, an order
store or an autonomy runtime.  The "PI-12 adapter" reads the same forecast
evidence shape documented by PI-12; it is an evaluation adapter, not the
production decision pipeline.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .models import RewardValidationError


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    action: Mapping[str, Any]
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {"action": dict(self.action), "rationale": self.rationale}


class Policy(Protocol):
    name: str
    version: str
    description: str

    def select(
        self,
        *,
        observation: Mapping[str, Any],
        portfolio: Mapping[str, Any],
        environment: Mapping[str, Any],
        history: Sequence[Mapping[str, Any]],
    ) -> PolicyDecision: ...


def _d(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _forecast(observation: Mapping[str, Any]) -> dict[str, Any]:
    value = observation.get("forecasts")
    return dict(value) if isinstance(value, Mapping) else {}


def _features(observation: Mapping[str, Any]) -> dict[str, Any]:
    features = observation.get("features")
    if not isinstance(features, Mapping):
        return {}
    values = features.get("values")
    return dict(values) if isinstance(values, Mapping) else {}


def _portfolio(portfolio: Mapping[str, Any]) -> tuple[float, float, float]:
    positions = portfolio.get("positions")
    quantity = 0.0
    value = 0.0
    if isinstance(positions, list) and positions:
        row = positions[0] if isinstance(positions[0], Mapping) else {}
        quantity = _d(row.get("quantity"))
        value = _d(row.get("market_value"))
    return quantity, value, _d(portfolio.get("cash"))


def _valid(forecast: Mapping[str, Any]) -> bool:
    return bool(forecast.get("available")) and str(forecast.get("validity") or "").lower() not in {
        "invalid",
        "stale",
        "expired",
        "unavailable",
    }


def _uncertainty_ok(forecast: Mapping[str, Any], maximum: str = "medium") -> bool:
    order = {"low": 0, "medium": 1, "high": 2}
    current = str(forecast.get("uncertainty") or "unknown").lower()
    return current in order and order[current] <= order[maximum]


class NoActionPolicy:
    name = "no_action"
    version = "pi14-baseline-no-action-1"
    description = "Always abstains; the do-nothing control."

    def select(
        self,
        *,
        observation: Mapping[str, Any],
        portfolio: Mapping[str, Any],
        environment: Mapping[str, Any],
        history: Sequence[Mapping[str, Any]],
    ) -> PolicyDecision:
        return PolicyDecision(
            {"action_type": "no_action"},
            "No-action baseline deliberately takes no position.",
        )


class RuleBasedPolicy:
    name = "rule_based"
    version = "pi14-baseline-rule-1"
    description = "A simple, documented forecast and drawdown rule; no learning."

    def select(
        self,
        *,
        observation: Mapping[str, Any],
        portfolio: Mapping[str, Any],
        environment: Mapping[str, Any],
        history: Sequence[Mapping[str, Any]],
    ) -> PolicyDecision:
        forecast = _forecast(observation)
        quantity, _value, cash = _portfolio(portfolio)
        instrument = str(observation.get("instrument_id") or "")
        expected = _d(forecast.get("expected_return"))
        direction = str(forecast.get("direction") or "").lower()
        drawdown = _d(portfolio.get("drawdown"))
        if quantity <= 0:
            if (
                _valid(forecast)
                and direction == "up"
                and expected > 0
                and _uncertainty_ok(forecast)
                and cash > 0
            ):
                return PolicyDecision(
                    {
                        "action_type": "buy",
                        "instrument_id": instrument,
                        "size_fraction": "0.20",
                    },
                    "Flat book, valid upward evidence and acceptable uncertainty.",
                )
            return PolicyDecision(
                {"action_type": "wait"},
                "No valid entry evidence; abstaining preserves the simulated book.",
            )
        if not _valid(forecast) or direction == "down" or drawdown <= -0.05:
            return PolicyDecision(
                {"action_type": "sell", "instrument_id": instrument},
                "Exit because the forecast deteriorated, became invalid, "
                "or drawdown reached the rule.",
            )
        return PolicyDecision(
            {"action_type": "hold"},
            "Retain the long position while the forecast remains valid and non-bearish.",
        )


class PI12AdapterPolicy:
    name = "pi12_adapter"
    version = "pi14-pi12-adapter-1"
    description = (
        "Read-only adapter over PI-12 forecast evidence: expected return, direction, "
        "calibrated probability, uncertainty and age. It is not the production pipeline."
    )

    def select(
        self,
        *,
        observation: Mapping[str, Any],
        portfolio: Mapping[str, Any],
        environment: Mapping[str, Any],
        history: Sequence[Mapping[str, Any]],
    ) -> PolicyDecision:
        forecast = _forecast(observation)
        quantity, _value, cash = _portfolio(portfolio)
        instrument = str(observation.get("instrument_id") or "")
        expected = _d(forecast.get("expected_return"))
        p_up = forecast.get("p_up")
        p_up_value = 0.5 if p_up is None else _d(p_up)
        age = _d(forecast.get("age_seconds"))
        max_age = 72 * 3600
        if quantity <= 0:
            if (
                _valid(forecast)
                and expected > 0.005
                and str(forecast.get("direction") or "").lower() == "up"
                and p_up_value >= 0.55
                and _uncertainty_ok(forecast, "medium")
                and age <= max_age
                and cash > 0
            ):
                return PolicyDecision(
                    {
                        "action_type": "buy",
                        "instrument_id": instrument,
                        "size_fraction": "0.15",
                    },
                    "PI-12 evidence adapter: positive expected return, "
                    "calibrated confidence and fresh forecast.",
                )
            return PolicyDecision(
                {"action_type": "wait"},
                "PI-12 evidence adapter: insufficient edge or confidence; abstain.",
            )
        if (
            not _valid(forecast)
            or expected < 0
            or str(forecast.get("direction") or "").lower() == "down"
            or age > max_age
        ):
            return PolicyDecision(
                {"action_type": "sell", "instrument_id": instrument},
                "PI-12 evidence adapter: forecast edge expired or turned negative.",
            )
        return PolicyDecision(
            {"action_type": "hold"},
            "PI-12 evidence adapter: retain while the stored forecast evidence remains supportive.",
        )


class ExperimentalRLPolicy:
    name = "experimental_rl"
    version = "pi14-experimental-policy-1"
    description = (
        "A seeded, non-trained experimental policy using forecast and RSI evidence. "
        "It is a comparison baseline, not a trained or promoted RL model."
    )

    def __init__(self, *, seed: int = 42) -> None:
        self._random = random.Random(int(seed))
        self.seed = int(seed)

    def select(
        self,
        *,
        observation: Mapping[str, Any],
        portfolio: Mapping[str, Any],
        environment: Mapping[str, Any],
        history: Sequence[Mapping[str, Any]],
    ) -> PolicyDecision:
        forecast = _forecast(observation)
        features = _features(observation)
        quantity, _value, cash = _portfolio(portfolio)
        instrument = str(observation.get("instrument_id") or "")
        expected = _d(forecast.get("expected_return"))
        rsi = _d(features.get("rsi_14"), 50.0)
        # Exploration is deliberately small and seeded. A hold decision is a
        # genuine action here, not a disguised claim of a trained policy.
        exploration = self._random.random() < 0.15
        if quantity <= 0:
            if (
                not exploration
                and _valid(forecast)
                and expected > 0.004
                and 35 <= rsi <= 75
                and _uncertainty_ok(forecast)
                and cash > 0
            ):
                return PolicyDecision(
                    {
                        "action_type": "buy",
                        "instrument_id": instrument,
                        "size_fraction": "0.18",
                    },
                    "Experimental baseline: positive forecast with non-extreme RSI.",
                )
            return PolicyDecision(
                {"action_type": "hold" if exploration else "wait"},
                "Experimental baseline: exploration/abstention branch.",
            )
        if not exploration and (
            not _valid(forecast)
            or expected < 0
            or rsi > 78
            or _d(portfolio.get("drawdown")) < -0.04
        ):
            return PolicyDecision(
                {"action_type": "sell", "instrument_id": instrument},
                "Experimental baseline: exit on weak evidence, overbought RSI or drawdown.",
            )
        return PolicyDecision(
            {"action_type": "hold"},
            "Experimental baseline: retain under non-exit conditions.",
        )


BASELINE_POLICIES: dict[str, Policy] = {
    "no_action": NoActionPolicy(),
    "rule_based": RuleBasedPolicy(),
    "pi12_adapter": PI12AdapterPolicy(),
    "experimental_rl": ExperimentalRLPolicy(),
}
BASELINE_TYPES = {
    "no_action": "no_action",
    "rule_based": "rule_based",
    "pi12_adapter": "pi12_adapter",
    "experimental_rl": "experimental_rl",
}
BASELINE_LIMITATIONS = {
    "no_action": "A control only; it cannot discover returns.",
    "rule_based": "A fixed heuristic, not an optimized or validated strategy.",
    "pi12_adapter": (
        "Reads forecast evidence only; it does not call PI-12 execution, NM-08 or NM-10 "
        "and cannot represent the full autonomous decision pipeline."
    ),
    "experimental_rl": (
        "Seeded, untrained comparison policy. It is not a trained RL model and must not "
        "be interpreted as evidence of learned-policy quality."
    ),
}


def policy_for(name: str, *, seed: int = 42) -> Policy:
    key = str(name or "").strip().lower()
    if key == "experimental_rl":
        return ExperimentalRLPolicy(seed=seed)
    policy = BASELINE_POLICIES.get(key)
    if policy is None:
        raise RewardValidationError(f"unknown policy {name!r}")
    return policy


def policy_catalogue() -> list[dict[str, Any]]:
    return [
        {
            "name": policy.name,
            "version": policy.version,
            "type": BASELINE_TYPES.get(policy.name, "baseline"),
            "description": policy.description,
            "limitations": BASELINE_LIMITATIONS.get(policy.name, ""),
            "advisory_only": True,
            "production_authority": False,
            "trained": False,
        }
        for policy in BASELINE_POLICIES.values()
    ]


__all__ = [
    "BASELINE_LIMITATIONS",
    "BASELINE_POLICIES",
    "BASELINE_TYPES",
    "ExperimentalRLPolicy",
    "NoActionPolicy",
    "PI12AdapterPolicy",
    "Policy",
    "PolicyDecision",
    "RuleBasedPolicy",
    "policy_catalogue",
    "policy_for",
]
