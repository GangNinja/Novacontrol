"""Transparent reward aggregation, normalization and attribution for PI-14."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .components import (
    COMPONENT_REGISTRY,
    EXPERIMENTAL_COMPONENT_REGISTRY,
    validate_component_registry,
)
from .models import (
    ComponentResult,
    RewardConfig,
    RewardContext,
    RewardResult,
    RewardValidationError,
    TransitionView,
    canonical_hash,
    default_reward_config,
)


def _round(value: float) -> float:
    return round(float(value), 10)


class RewardEngine:
    """Deterministic component aggregation; it never mutates a transition."""

    def __init__(
        self,
        *,
        registry: Mapping[str, Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._registry = dict(registry or COMPONENT_REGISTRY)
        validate_component_registry(self._registry)
        allowed_registries = (
            set(COMPONENT_REGISTRY),
            set(EXPERIMENTAL_COMPONENT_REGISTRY),
        )
        if set(self._registry) not in allowed_registries:
            raise RewardValidationError("a reward registry must contain every supported component")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._calculations = 0

    @property
    def registry(self) -> dict[str, str]:
        return {name: component.category for name, component in self._registry.items()}

    def calculate(
        self,
        transition: Mapping[str, Any],
        *,
        config: RewardConfig | None = None,
        history: Sequence[Mapping[str, Any]] = (),
        environment: Mapping[str, Any] | None = None,
        risk_evidence: Mapping[str, Any] | None = None,
        calculated_at: datetime | None = None,
    ) -> RewardResult:
        config = config or default_reward_config()
        if not isinstance(config, RewardConfig):
            raise RewardValidationError("config must be a RewardConfig")
        unavailable = sorted(set(config.enabled) - set(self._registry))
        if unavailable:
            raise RewardValidationError(
                "enabled component(s) are unavailable in this reward engine: "
                + ", ".join(unavailable)
            )
        view = TransitionView.from_transition(transition, environment=environment)
        history_views = tuple(
            TransitionView.from_transition(row, environment=environment)
            for row in list(history)[-50:]
        )
        context = RewardContext(
            transition=view,
            history=history_views,
            risk_evidence=dict(risk_evidence or {}),
            thresholds=dict(config.thresholds),
        )
        component_payloads: dict[str, Any] = {}
        missing: list[str] = []
        excluded: list[str] = []
        available_weights: dict[str, float] = {}
        contributions: dict[str, float] = {}
        penalties: dict[str, float] = {}
        results: dict[str, ComponentResult] = {}
        for name, component in self._registry.items():
            if name not in config.enabled:
                excluded.append(name)
                component_payloads[name] = {
                    "name": name,
                    "category": component.category,
                    "enabled": False,
                    "available": False,
                    "raw_value": None,
                    "normalized": None,
                    "weight": 0.0,
                    "contribution": 0.0,
                    "reason": "disabled by reward configuration",
                    "metrics": {},
                }
                continue
            try:
                result = component.evaluate(context)
            except Exception as exc:  # noqa: BLE001 - one component must not erase attribution
                result = ComponentResult(
                    name,
                    component.category,
                    None,
                    available=False,
                    reason=f"component failed: {type(exc).__name__}: {exc}",
                )
            results[name] = result
            if result.penalties:
                for key, value in result.penalties.items():
                    if key not in config.penalties:
                        continue
                    penalties[key] = max(penalties.get(key, 0.0), float(value))
            if not result.available or result.raw_value is None:
                missing.append(name)
                component_payloads[name] = {
                    **result.to_dict(),
                    "enabled": True,
                    "normalized": None,
                    "weight": config.components.get(name, 0.0),
                    "contribution": 0.0,
                    "reason": result.reason or "component unavailable",
                }
                continue
            raw = float(result.raw_value)
            scale = config.scales.get(name, 1.0)
            scaled = raw / scale if scale else raw
            low, high = config.bounds
            normalized = (
                max(low, min(high, scaled)) if config.normalization == "bounded" else scaled
            )
            weight = float(config.components.get(name, 0.0))
            available_weights[name] = weight
            contributions[name] = weight * normalized
            component_payloads[name] = {
                **result.to_dict(),
                "enabled": True,
                "scale": scale,
                "normalized": _round(normalized),
                "weight": weight,
                "contribution": _round(weight * normalized),
                "outlier_clipped": bool(normalized != scaled),
            }
        denominator = sum(available_weights.values())
        if config.missing_policy == "renormalize":
            total_components = (
                sum(contributions.values()) / denominator if denominator != 0 else 0.0
            )
        else:
            total_components = sum(contributions.values())
        penalty_payload = {
            key: _round(float(weight) * float(penalties.get(key, 0.0)))
            for key, weight in config.penalties.items()
            if penalties.get(key, 0.0)
        }
        total_raw = total_components - sum(penalty_payload.values())
        total_low, total_high = config.bounds
        total = (
            max(total_low, min(total_high, total_raw))
            if config.normalization == "bounded"
            else total_raw
        )
        calculated = calculated_at or self._clock()
        if calculated.tzinfo is None:
            calculated = calculated.replace(tzinfo=UTC)
        attribution = sorted(
            (
                {
                    "component": name,
                    "contribution": _round(value),
                    "weight": float(config.components.get(name, 0.0)),
                    "category": self._registry[name].category,
                }
                for name, value in contributions.items()
            ),
            key=lambda item: (-abs(float(str(item["contribution"]))), item["component"]),
        )
        source = {
            "transition_id": view.transition_id,
            "episode_id": view.episode_id,
            "environment_id": view.environment_id,
            "sequence": view.sequence,
            "observation_id": view.observation.get("observation_id"),
            "next_observation_id": view.next_observation.get("observation_id"),
            "dataset_version": view.dataset_version,
            "feature_set_version": view.feature_set_version,
            "environment_version": view.environment_version,
            "simulation_config_hash": view.simulation_config_hash,
            "transition_reward_interface": view.observation.get("reward_interface_version"),
        }
        result_hash = canonical_hash(
            {
                "config_hash": config.config_hash,
                "source": source,
                "total": _round(total),
                "components": {
                    key: {
                        "raw": value.get("raw_value"),
                        "normalized": value.get("normalized"),
                        "contribution": value.get("contribution"),
                        "available": value.get("available"),
                    }
                    for key, value in sorted(component_payloads.items())
                },
                "penalties": penalty_payload,
                "missing": sorted(missing),
            }
        )
        reward_id = (
            "rw_"
            + hashlib.sha256(
                (view.transition_id + "|" + config.config_hash + "|" + result_hash).encode()
            ).hexdigest()[:24]
        )
        with self._lock:
            self._calculations += 1
        return RewardResult(
            reward_id=reward_id,
            transition_id=view.transition_id,
            episode_id=view.episode_id,
            config_id=config.config_id,
            config_hash=config.config_hash,
            total=_round(total),
            components=component_payloads,
            attribution=tuple(attribution),
            penalties=penalty_payload,
            missing_components=tuple(sorted(missing)),
            excluded_components=tuple(sorted(excluded)),
            normalization={
                "method": config.normalization,
                "bounds": list(config.bounds),
                "scales": dict(config.scales),
                "missing_policy": config.missing_policy,
                "weights_denominator": _round(denominator),
                "total_raw": _round(total_raw),
                "total_clipped": bool(total != total_raw),
                "learned_parameters": False,
                "fit_data": "none; fixed experimental scales only",
            },
            source=source,
            calculated_at=calculated,
            result_hash=result_hash,
            experimental=config.experimental,
        )

    def recalculate(
        self,
        transition: Mapping[str, Any],
        *,
        config: RewardConfig | None = None,
        history: Sequence[Mapping[str, Any]] = (),
        environment: Mapping[str, Any] | None = None,
        risk_evidence: Mapping[str, Any] | None = None,
    ) -> RewardResult:
        """Pure recalculation from a stored transition; no persisted row is changed."""
        return self.calculate(
            transition,
            config=config,
            history=history,
            environment=environment,
            risk_evidence=risk_evidence,
        )

    def status(self) -> dict[str, Any]:
        with self._lock:
            calculations = self._calculations
        return {
            "phase": "PI-14",
            "engine": "reward-engine",
            "contract_version": "pi14-reward-v1",
            "components": self.registry,
            "component_count": len(self.registry),
            "calculations": calculations,
            "deterministic": True,
            "mutates_environment": False,
            "mutates_production_financial_state": False,
            "normalization": "bounded experimental scales; no learned normalizer",
        }


__all__ = ["RewardEngine"]
