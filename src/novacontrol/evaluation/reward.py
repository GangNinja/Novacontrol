"""The reward engine: a number with its reasons attached, never a bare number.

Reward here means one thing: a weighted reading of OBSERVABLE structured
outcomes. Task success, verification, tool correctness, plan efficiency, safety,
latency and resource use go up; unnecessary actions, failed execution, failed
verification, unsafe actions and excessive retries go down. There is no term
anywhere in this module that depends on a model's hidden reasoning, because none
is collected — the reward is a function of the trajectory and the evaluation,
and both are records of what happened.

Weights are configuration, not constants: :class:`RewardConfig` is the only
place a number that shapes the total is written down, and the engine reads it
from there. An operator can therefore change what "good" means for this
installation without editing code, and a stored
:class:`RewardResult` records the version and the weights that produced it, so
an old reward can still be explained after the weights move.

A refusals is not a penalty. ``unsafe_action_penalty`` is for an action that
BYPASSED a gate (ran although its confirmation was refused, or ran with no
recorded answer), not for one the permission layer correctly stopped: stopping
that is what ``safety_reward`` is FOR. Penalising a refusal would teach a future
phase to avoid the safety layer, which is the opposite of the intent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from novacontrol.evaluation.evaluator import EvaluationResult
from novacontrol.evaluation.models import (
    REWARD_SCHEMA_VERSION,
    AgentTrajectory,
    EvaluationDimension,
    now_iso,
)

#: The additive components, in the order a reader wants them.
COMPONENT_NAMES: tuple[str, ...] = (
    "task_success",
    "verification_success",
    "tool_correctness",
    "plan_efficiency",
    "safety",
    "latency",
    "resource_efficiency",
)

#: The subtractive terms. Values are magnitudes, subtracted from the total.
PENALTY_NAMES: tuple[str, ...] = (
    "unnecessary_action",
    "failed_execution",
    "failed_verification",
    "unsafe_action",
    "excessive_retry",
)

#: Default weights. Chosen so task success dominates, verification matters more
#: than speed, and no single penalty can outweigh a genuinely good run.
DEFAULT_COMPONENT_WEIGHTS: Mapping[str, float] = {
    "task_success": 1.0,
    "verification_success": 0.5,
    "tool_correctness": 0.3,
    "plan_efficiency": 0.2,
    "safety": 0.3,
    "latency": 0.1,
    "resource_efficiency": 0.1,
}

DEFAULT_PENALTY_WEIGHTS: Mapping[str, float] = {
    "unnecessary_action": 0.2,
    "failed_execution": 0.5,
    "failed_verification": 0.3,
    "unsafe_action": 1.0,
    "excessive_retry": 0.2,
}

#: How many occurrences a counted penalty (one that is not a yes/no fact) can
#: charge for. Three is enough to make a pattern hurt and small enough that a
#: long-but-correct run is not buried by one figure repeated many times.
MAX_COUNTED_PENALTIES = 3

REWARD_VERSION = "phase15.1"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


@dataclass(frozen=True, slots=True)
class RewardConfig:
    """Every weight the total is built from, in one place."""

    version: str = REWARD_VERSION
    enabled: bool = True
    component_weights: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_COMPONENT_WEIGHTS)
    )
    penalty_weights: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_PENALTY_WEIGHTS)
    )
    min_reward: float = -3.0
    max_reward: float = 3.0
    normalize: bool = True
    #: The latency a run is expected to stay within. Shared with the evaluator's
    #: own budget by configuration, not by a second constant.
    latency_budget_ms: float = 8000.0
    max_retries: int = 2
    memory_budget_bytes: int = 2 * 1024 * 1024 * 1024

    def weight(self, name: str) -> float:
        return float(self.component_weights.get(name, DEFAULT_COMPONENT_WEIGHTS.get(name, 0.0)))

    def penalty(self, name: str) -> float:
        return float(self.penalty_weights.get(name, DEFAULT_PENALTY_WEIGHTS.get(name, 0.0)))

    @property
    def positive_pool(self) -> float:
        """The largest total the components can add up to (normalisation base)."""
        return sum(max(0.0, self.weight(name)) for name in COMPONENT_NAMES)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "enabled": self.enabled,
            "component_weights": {name: self.weight(name) for name in COMPONENT_NAMES},
            "penalty_weights": {name: self.penalty(name) for name in PENALTY_NAMES},
            "min_reward": self.min_reward,
            "max_reward": self.max_reward,
            "normalize": self.normalize,
            "latency_budget_ms": self.latency_budget_ms,
            "max_retries": self.max_retries,
            "memory_budget_bytes": self.memory_budget_bytes,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> RewardConfig:
        """Read a configured reward; an unusable weight keeps the default.

        A weight may be any finite number (zero is meaningful: "this factor does
        not count here"), but it may not be ``nan``/``inf`` — a reward nobody can
        compare is worse than a reward with the wrong weight.
        """
        defaults = cls()

        def weights(
            source: Any, names: Sequence[str], current: Mapping[str, float]
        ) -> dict[str, float]:
            merged = {name: float(current.get(name, 0.0)) for name in names}
            if not isinstance(source, Mapping):
                return merged
            for name in names:
                if name not in source:
                    continue
                value = source.get(name)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                number = float(value)
                if number != number or number in {float("inf"), float("-inf")}:
                    continue
                merged[name] = number
            return merged

        def number(key: str, current: float, *, low: float, high: float) -> float:
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return current
            return _clamp(float(value), low, high)

        def count(key: str, current: int, *, high: int) -> int:
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return current
            return min(value, high)

        enabled = data.get("enabled")
        version = data.get("version")
        normalize_flag = data.get("normalize")
        return cls(
            version=str(version).strip()
            if isinstance(version, str) and version.strip()
            else defaults.version,
            enabled=enabled if isinstance(enabled, bool) else defaults.enabled,
            component_weights=weights(
                data.get("component_weights"), COMPONENT_NAMES, defaults.component_weights
            ),
            penalty_weights=weights(
                data.get("penalty_weights"), PENALTY_NAMES, defaults.penalty_weights
            ),
            min_reward=number("min_reward", defaults.min_reward, low=-1000.0, high=0.0),
            max_reward=number("max_reward", defaults.max_reward, low=0.0, high=1000.0),
            normalize=normalize_flag
            if isinstance(normalize_flag, bool)
            else defaults.normalize,
            latency_budget_ms=number(
                "latency_budget_ms", defaults.latency_budget_ms, low=1.0, high=3_600_000.0
            ),
            max_retries=count("max_retries", defaults.max_retries, high=100),
            memory_budget_bytes=count(
                "memory_budget_bytes", defaults.memory_budget_bytes, high=1 << 50
            ),
        )


@dataclass(frozen=True, slots=True)
class RewardComponent:
    """One positive term: what it saw, what it is worth, and why."""

    name: str
    value: float
    weight: float
    contribution: float
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": round(self.value, 6),
            "weight": self.weight,
            "contribution": round(self.contribution, 6),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RewardComponent:
        return cls(
            name=str(data.get("name", "")),
            value=float(data.get("value", 0.0) or 0.0),
            weight=float(data.get("weight", 0.0) or 0.0),
            contribution=float(data.get("contribution", 0.0) or 0.0),
            reason=str(data.get("reason", "")),
        )


@dataclass(frozen=True, slots=True)
class RewardPenalty:
    """One negative term: how many times it applied, and what that costs."""

    name: str
    count: float
    weight: float
    contribution: float
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "count": round(self.count, 6),
            "weight": self.weight,
            "contribution": round(self.contribution, 6),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RewardPenalty:
        return cls(
            name=str(data.get("name", "")),
            count=float(data.get("count", 0.0) or 0.0),
            weight=float(data.get("weight", 0.0) or 0.0),
            contribution=float(data.get("contribution", 0.0) or 0.0),
            reason=str(data.get("reason", "")),
        )


def reward_id_for(trajectory_id: str) -> str:
    """A deterministic id, so re-scoring a row replaces its reward."""
    return f"rw-{trajectory_id}" if trajectory_id else "rw-unknown"


@dataclass(frozen=True, slots=True)
class RewardResult:
    """A reward with its full breakdown — the only form this phase stores.

    ``component_rewards`` and ``penalties`` are the weighted contributions the
    total is actually made of, while ``components`` and ``penalty_breakdown``
    carry the raw values, the weights and a sentence of reason for each. A reader
    can therefore ask "why is this 1.35?" without re-running anything.
    """

    reward_id: str = ""
    trajectory_id: str = ""
    evaluation_id: str = ""
    #: Which kind of signal produced this reading (``human``, ``ai``, ``rule``,
    #: ``verifier``, ``composite``). Empty on a Phase 15 row, which has one source.
    reward_source: str = ""
    #: How sure the source was, when the source can say. Never invented here.
    confidence: float | None = None
    #: The evaluator/provider that produced the signal, when there is one.
    evaluator_id: str = ""
    total_reward: float = 0.0
    normalized_reward: float | None = None
    component_rewards: Mapping[str, float] = field(default_factory=dict)
    penalties: Mapping[str, float] = field(default_factory=dict)
    components: tuple[RewardComponent, ...] = ()
    penalty_breakdown: tuple[RewardPenalty, ...] = ()
    reward_version: str = REWARD_VERSION
    weights: Mapping[str, Any] = field(default_factory=dict)
    explanation_summary: str = ""
    evidence: tuple[str, ...] = ()
    schema_version: int = REWARD_SCHEMA_VERSION
    timestamp: str = field(default_factory=now_iso)

    @property
    def applied_penalties(self) -> tuple[RewardPenalty, ...]:
        return tuple(item for item in self.penalty_breakdown if item.contribution)

    def with_provenance(
        self,
        *,
        source: str = "",
        evaluator_id: str = "",
        confidence: float | None = None,
    ) -> RewardResult:
        """The same reward, stamped with where it came from (Phase 18's need).

        A Phase 15 reward has one source; an RL-facing one may be a human's
        judgement, an AI rating or a rule, and a reader has to be able to tell
        which without asking the code that stored it. Anything not stated keeps
        what the reward already carried.
        """
        return replace(
            self,
            reward_source=source or self.reward_source,
            evaluator_id=evaluator_id or self.evaluator_id,
            confidence=self.confidence if confidence is None else float(confidence),
        )

    def component(self, name: str) -> RewardComponent | None:
        for item in self.components:
            if item.name == name:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reward_id": self.reward_id,
            "trajectory_id": self.trajectory_id,
            "evaluation_id": self.evaluation_id,
            "reward_source": self.reward_source,
            "confidence": self.confidence,
            "evaluator_id": self.evaluator_id,
            "total_reward": round(self.total_reward, 6),
            "normalized_reward": None
            if self.normalized_reward is None
            else round(self.normalized_reward, 6),
            "component_rewards": {
                name: round(value, 6) for name, value in self.component_rewards.items()
            },
            "penalties": {name: round(value, 6) for name, value in self.penalties.items()},
            "components": [item.to_dict() for item in self.components],
            "penalty_breakdown": [item.to_dict() for item in self.penalty_breakdown],
            "reward_version": self.reward_version,
            "weights": dict(self.weights),
            "explanation_summary": self.explanation_summary,
            "evidence": list(self.evidence),
            "schema_version": self.schema_version,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RewardResult:
        rows = data.get("components")
        components: list[RewardComponent] = []
        if isinstance(rows, (list, tuple)):
            for row in rows:
                if isinstance(row, Mapping):
                    components.append(RewardComponent.from_dict(row))
        penalty_rows = data.get("penalty_breakdown")
        penalties: list[RewardPenalty] = []
        if isinstance(penalty_rows, (list, tuple)):
            for row in penalty_rows:
                if isinstance(row, Mapping):
                    penalties.append(RewardPenalty.from_dict(row))
        normalized = data.get("normalized_reward")
        evidence = data.get("evidence")
        return cls(
            reward_id=str(data.get("reward_id", "")),
            trajectory_id=str(data.get("trajectory_id", "")),
            evaluation_id=str(data.get("evaluation_id", "")),
            reward_source=str(data.get("reward_source", "")),
            confidence=float(data["confidence"])
            if isinstance(data.get("confidence"), (int, float))
            else None,
            evaluator_id=str(data.get("evaluator_id", "")),
            total_reward=float(data.get("total_reward", 0.0) or 0.0),
            normalized_reward=float(normalized) if isinstance(normalized, (int, float)) else None,
            component_rewards={
                str(key): float(value)
                for key, value in (data.get("component_rewards") or {}).items()
                if isinstance(value, (int, float))
            }
            if isinstance(data.get("component_rewards"), Mapping)
            else {},
            penalties={
                str(key): float(value)
                for key, value in (data.get("penalties") or {}).items()
                if isinstance(value, (int, float))
            }
            if isinstance(data.get("penalties"), Mapping)
            else {},
            components=tuple(components),
            penalty_breakdown=tuple(penalties),
            reward_version=str(data.get("reward_version", REWARD_VERSION)),
            weights=dict(data.get("weights", {}))
            if isinstance(data.get("weights"), Mapping)
            else {},
            explanation_summary=str(data.get("explanation_summary", "")),
            evidence=tuple(str(item) for item in evidence)
            if isinstance(evidence, (list, tuple))
            else (),
            schema_version=int(data.get("schema_version", REWARD_SCHEMA_VERSION) or 1),
            timestamp=str(data.get("timestamp", "")) or now_iso(),
        )


class RewardEngine:
    """Turns a trajectory (and its evaluation) into a weighted, explained total."""

    def __init__(self, config: RewardConfig | None = None) -> None:
        self.config = config if config is not None else RewardConfig()

    # -- entry point -----------------------------------------------------------

    def score(
        self,
        trajectory: AgentTrajectory,
        evaluation: EvaluationResult | None = None,
    ) -> RewardResult:
        """Score one run. Deterministic: the same inputs produce the same total."""
        components = self._components(trajectory, evaluation)
        penalties = self._penalties(trajectory, evaluation)
        raw_total = sum(item.contribution for item in components) - sum(
            item.contribution for item in penalties
        )
        total = _clamp(raw_total, self.config.min_reward, self.config.max_reward)
        normalized: float | None = None
        if self.config.normalize:
            pool = max(1.0, self.config.positive_pool)
            normalized = _clamp(total / pool, -1.0, 1.0)
        return RewardResult(
            reward_id=reward_id_for(trajectory.trajectory_id),
            trajectory_id=trajectory.trajectory_id,
            evaluation_id=evaluation.evaluation_id if evaluation is not None else "",
            total_reward=total,
            normalized_reward=normalized,
            component_rewards={item.name: item.contribution for item in components},
            penalties={item.name: item.contribution for item in penalties},
            components=tuple(components),
            penalty_breakdown=tuple(penalties),
            reward_version=self.config.version,
            weights=self.config.to_mapping(),
            explanation_summary=self._explain(components, penalties, total),
            evidence=tuple(
                (list(evaluation.evidence) if evaluation is not None else [])
                + [f"trajectory:{trajectory.trajectory_id}"]
            ),
        )

    # -- components ------------------------------------------------------------

    def _components(
        self, trajectory: AgentTrajectory, evaluation: EvaluationResult | None
    ) -> list[RewardComponent]:
        config = self.config
        values: list[tuple[str, float, str]] = [
            ("task_success", *self._success_value(trajectory)),
            ("verification_success", *self._verification_value(trajectory, evaluation)),
            ("tool_correctness", *self._tool_value(trajectory, evaluation)),
            ("plan_efficiency", *self._plan_value(trajectory, evaluation)),
            ("safety", *self._safety_value(trajectory, evaluation)),
            ("latency", *self._latency_value(trajectory)),
            ("resource_efficiency", *self._resource_value(trajectory)),
        ]
        return [
            RewardComponent(
                name=name,
                value=_clamp(value, 0.0, 1.0),
                weight=config.weight(name),
                contribution=_clamp(value, 0.0, 1.0) * config.weight(name),
                reason=reason,
            )
            for name, value, reason in values
        ]

    def _success_value(self, trajectory: AgentTrajectory) -> tuple[float, str]:
        if trajectory.success is True:
            return 1.0, "the task completed"
        if trajectory.success is False:
            return 0.0, f"the task failed: {trajectory.failure_reason or 'no reason recorded'}"
        return 0.5, "the run ended without deciding success: scored neutrally"

    def _verification_value(
        self, trajectory: AgentTrajectory, evaluation: EvaluationResult | None
    ) -> tuple[float, str]:
        measured = evaluation.score(EvaluationDimension.VERIFICATION) if evaluation else None
        if measured is not None:
            return measured, "verification scored from the recorded verification results"
        if trajectory.success is True:
            return 1.0, "no verification was recorded and nothing contradicted the result"
        if trajectory.success is False:
            return 0.0, "the run failed with no verification to argue otherwise"
        return 0.5, "no verification evidence was captured"

    def _tool_value(
        self, trajectory: AgentTrajectory, evaluation: EvaluationResult | None
    ) -> tuple[float, str]:
        measured = evaluation.score(EvaluationDimension.TOOL_SELECTION) if evaluation else None
        if measured is not None:
            return measured, f"tool selection scored over {len(trajectory.tool_calls)} call(s)"
        if not trajectory.tool_calls:
            return 1.0, "no tool was needed for this request"
        return 0.5, "tool calls were made but not scored"

    def _plan_value(
        self, trajectory: AgentTrajectory, evaluation: EvaluationResult | None
    ) -> tuple[float, str]:
        measured = evaluation.score(EvaluationDimension.PLANNING) if evaluation else None
        if measured is not None:
            return measured, "planning scored from the captured plan and its steps"
        if not trajectory.plan and not trajectory.execution_steps:
            return 1.0, "the request needed no plan"
        return 0.5, "a plan exists but planning was not scored"

    def _safety_value(
        self, trajectory: AgentTrajectory, evaluation: EvaluationResult | None
    ) -> tuple[float, str]:
        measured = evaluation.score(EvaluationDimension.SAFETY) if evaluation else None
        if measured is not None:
            refused = sum(
                1 for call in trajectory.tool_calls if call.status in {"denied", "refused"}
            )
            detail = (
                f"; {refused} action(s) were refused by the permission layer"
                if refused
                else ""
            )
            return measured, "safety scored from the gated actions and their answers" + detail
        return 1.0, "no gated action was attempted"

    def _latency_value(self, trajectory: AgentTrajectory) -> tuple[float, str]:
        total = trajectory.latency_metrics.total_ms
        if total is None:
            return 0.5, "latency was not measured: scored neutrally"
        budget = max(1.0, self.config.latency_budget_ms)
        if total <= budget:
            return 1.0, f"the run took {total:.0f} ms, within the {budget:.0f} ms budget"
        return _clamp(budget / max(1.0, total), 0.0, 1.0), (
            f"the run took {total:.0f} ms, over the {budget:.0f} ms budget"
        )

    def _resource_value(self, trajectory: AgentTrajectory) -> tuple[float, str]:
        delta = trajectory.resource_usage.ram_delta_bytes
        if delta is None:
            return 0.5, "memory use was not measured: scored neutrally"
        budget = max(1, self.config.memory_budget_bytes)
        if delta <= budget:
            return 1.0, f"the run added {delta} bytes, within the {budget} byte budget"
        return _clamp(budget / max(1, delta), 0.0, 1.0), (
            f"the run added {delta} bytes, over the {budget} byte budget"
        )

    # -- penalties -------------------------------------------------------------

    def _penalties(
        self, trajectory: AgentTrajectory, evaluation: EvaluationResult | None
    ) -> list[RewardPenalty]:
        config = self.config
        counted: list[tuple[str, float, str]] = [
            ("unnecessary_action", *self._unnecessary(trajectory)),
            ("failed_execution", 0.0 if trajectory.success is not False else 1.0, ""),
            ("failed_verification", *self._failed_verification(trajectory)),
            ("unsafe_action", *self._unsafe(trajectory)),
            ("excessive_retry", *self._retries(trajectory)),
        ]
        produced: list[RewardPenalty] = []
        for name, count, reason in counted:
            magnitude = _clamp(count, 0.0, float(MAX_COUNTED_PENALTIES))
            weight = config.penalty(name)
            produced.append(
                RewardPenalty(
                    name=name,
                    count=magnitude,
                    weight=weight,
                    contribution=magnitude * weight,
                    reason=reason
                    or (
                        "the run failed"
                        if name == "failed_execution"
                        else "the penalty applied"
                    ),
                )
            )
        return produced

    def _unnecessary(self, trajectory: AgentTrajectory) -> tuple[float, str]:
        repeated: set[str] = set()
        seen_ok: set[str] = set()
        for call in trajectory.tool_calls:
            if call.succeeded:
                if call.tool in seen_ok:
                    repeated.add(call.tool)
                seen_ok.add(call.tool)
        never_attempted = sum(
            1 for step in trajectory.execution_steps if step.status in {"", "planned", "pending"}
        )
        count = len(repeated) + never_attempted
        if not count:
            return 0.0, "every action was taken once and every planned step was attempted"
        parts: list[str] = []
        if repeated:
            parts.append("repeated tool call(s): " + ", ".join(sorted(repeated)))
        if never_attempted:
            parts.append(f"{never_attempted} planned step(s) were never attempted")
        return float(count), "; ".join(parts)

    def _failed_verification(self, trajectory: AgentTrajectory) -> tuple[float, str]:
        failed = len(trajectory.failed_verifications)
        if not failed:
            return 0.0, "every recorded verification passed"
        return float(failed), f"{failed} verification result(s) failed"

    def _unsafe(self, trajectory: AgentTrajectory) -> tuple[float, str]:
        breached = [
            call
            for call in trajectory.tool_calls
            if call.status == "completed" and call.requires_confirmation and call.approved is False
        ]
        unanswered = [
            call
            for call in trajectory.tool_calls
            if call.status == "completed" and call.requires_confirmation and call.approved is None
        ]
        count = len(breached) + len(unanswered)
        if not count:
            return 0.0, "no action bypassed the confirmation it required"
        return float(count), (
            f"{len(breached)} action(s) ran after their confirmation was refused and "
            f"{len(unanswered)} ran with no recorded answer"
        )

    def _retries(self, trajectory: AgentTrajectory) -> tuple[float, str]:
        excess = trajectory.retries - self.config.max_retries
        if excess <= 0:
            return 0.0, (
                f"{trajectory.retries} retry/retries, within the tolerance of "
                f"{self.config.max_retries}"
            )
        return float(excess), (
            f"{excess} retry/retries above the tolerance of {self.config.max_retries}"
        )

    # -- explanation -----------------------------------------------------------

    def _explain(
        self,
        components: Sequence[RewardComponent],
        penalties: Sequence[RewardPenalty],
        total: float,
    ) -> str:
        """One sentence about observable factors. Never a reasoning trace."""
        positives = [item for item in components if item.contribution > 0]
        applied = [item for item in penalties if item.contribution > 0]
        parts: list[str] = []
        if positives:
            best = sorted(positives, key=lambda item: item.contribution, reverse=True)[:3]
            parts.append(
                "reward "
                + ", ".join(f"{item.name} (+{item.contribution:.2f})" for item in best)
            )
        else:
            parts.append("no positive component contributed")
        if applied:
            parts.append(
                "penalties "
                + ", ".join(f"{item.name} (-{item.contribution:.2f})" for item in applied)
            )
        else:
            parts.append("no penalties applied")
        return f"total {total:+.2f}: " + "; ".join(parts)


__all__ = [
    "COMPONENT_NAMES",
    "DEFAULT_COMPONENT_WEIGHTS",
    "DEFAULT_PENALTY_WEIGHTS",
    "MAX_COUNTED_PENALTIES",
    "PENALTY_NAMES",
    "REWARD_VERSION",
    "RewardComponent",
    "RewardConfig",
    "RewardEngine",
    "RewardPenalty",
    "RewardResult",
    "reward_id_for",
]
