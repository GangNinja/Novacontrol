"""The evaluation contracts for the game-playing milestone that follows.

This is the phase's §13: *prepare useful measurement infrastructure*, using the
Phase 25 environment adapter — NOT a new game implementation. Nine contracts are
defined, each naming what it measures, the unit it is measured in, and where the
number comes from in a real embodied session record:

    objective_completion      did the session's conditions get OBSERVED holding?
    action_efficiency         inputs sent per verified completion
    observation_freshness     stale readings and how often they were taken
    recovery_success          recoveries attempted versus succeeded
    constraint_violations     refusals, failures and safety violations
    time_to_completion        the session's own elapsed seconds
    initial_condition_spread  the same objective across scenarios/fault budgets
    reproducibility           the seed, the scenario and the frozen settings
    resource_use              trajectory size, record bytes, persist cost

Two rules are the reason this module exists rather than the numbers being read
ad hoc by whoever writes the game benchmark later:

* **A figure that was not measured is ``None``, never ``0.0``.** An embodied
  session that never recorded an elapsed time reports no time, and the contract's
  row says so.
* **Nothing here claims gameplay.** The contracts are evaluated over sessions
  this build actually ran in its OWN simulation. A real game is the next
  milestone, and every row this module produces carries
  ``environment`` = the environment id of the session it summarised.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "GAME_EVAL_CONTRACTS",
    "GameEvalContract",
    "contracts_payload",
    "evaluate_embodied_sessions",
    "summarise_embodied_session",
]


@dataclass(frozen=True, slots=True)
class GameEvalContract:
    """One measurement contract for the coming game-playing evaluation."""

    contract_id: str
    description: str
    unit: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "description": self.description,
            "unit": self.unit,
            "source": self.source,
        }


GAME_EVAL_CONTRACTS: tuple[GameEvalContract, ...] = (
    GameEvalContract(
        contract_id="objective_completion",
        description=(
            "Whether the session's success conditions were observed holding, and the "
            "completion rate over the inputs it spent."
        ),
        unit="bool and ratio",
        source="session verdict.objective_met + verdict.verified + objective_completion_rate",
    ),
    GameEvalContract(
        contract_id="action_efficiency",
        description=(
            "How many inputs produced one verified completion — the figure that separates "
            "winning from flailing."
        ),
        unit="inputs per completion",
        source="evaluation.actions_per_success, or actions_attempted / completed",
    ),
    GameEvalContract(
        contract_id="observation_freshness",
        description=(
            "How many readings were taken and how many of them were stale — a session that "
            "acted on stale state shows up here, not in its score."
        ),
        unit="count",
        source="evaluation.observations + evaluation.stale_observations",
    ),
    GameEvalContract(
        contract_id="recovery_success",
        description=(
            'Recoveries attempted versus succeeded, so "it retried" and "the retry worked" '
            "are different numbers."
        ),
        unit="count and ratio",
        source="evaluation.recoveries + recoveries_succeeded + recovery_success_rate",
    ),
    GameEvalContract(
        contract_id="constraint_violations",
        description=(
            "Refused inputs, failed inputs and safety violations — the safety measure the "
            "score must never hide."
        ),
        unit="count",
        source=(
            "evaluation.actions_rejected + actions_failed + safety_violations + invalid_actions"
        ),
    ),
    GameEvalContract(
        contract_id="time_to_completion",
        description="Elapsed session seconds, reported only when the record measured them.",
        unit="seconds",
        source="evaluation.seconds",
    ),
    GameEvalContract(
        contract_id="initial_condition_spread",
        description=(
            "The same objective measured across different initial conditions (scenarios and "
            "fault budgets), reported per configuration and never pooled into one number."
        ),
        unit="per-scenario outcome",
        source="session scenario + environment_id across sessions",
    ),
    GameEvalContract(
        contract_id="reproducibility",
        description=(
            "The environment id, scenario, seed and frozen budgets a session ran with, so a "
            "result can be reproduced or declared unreproducible."
        ),
        unit="configuration record",
        source="session environment_id + scenario + objective + settings",
    ),
    GameEvalContract(
        contract_id="resource_use",
        description=(
            "What the session cost: trajectory entries, record bytes when the caller "
            "supplies them, and the persist time — never a fabricated figure."
        ),
        unit="count and milliseconds",
        source="benchmark-recorded record bytes and save time; session trajectory length",
    ),
)


def summarise_embodied_session(report: Mapping[str, Any]) -> dict[str, Any]:
    """One embodied session summarised against the contracts.

    Reads the session record the application already produces. Every figure that
    the record does not carry stays ``None``: a session with no elapsed time is
    not a zero-second session.
    """
    evaluation = dict(report.get("evaluation") or {})
    verdict = dict(report.get("verdict") or {})
    objective = dict(report.get("objective") or {})
    trajectory = report.get("trajectory") or ()
    attempted = _count(evaluation.get("actions_attempted"))
    completed = _count(evaluation.get("actions_completed"))
    success_rate = evaluation.get("objective_completion_rate")
    per_success = evaluation.get("actions_per_success")
    if per_success is None and attempted is not None and completed:
        per_success = attempted / completed
    observations = _count(evaluation.get("observations"))
    stale = _count(evaluation.get("stale_observations"))
    freshness = None
    if observations is not None:
        freshness = 1.0 - (stale or 0) / observations if observations else None
    return {
        "session_id": str(report.get("session_id") or ""),
        "environment": str(report.get("environment_id") or ""),
        "scenario": str(report.get("scenario") or ""),
        "status": str(report.get("status") or ""),
        "objective_met": bool(verdict.get("met")) if verdict else None,
        "verified_completion": (
            bool(evaluation.get("verified_completion")) if evaluation else None
        ),
        "objective_completion_rate": success_rate,
        "action_efficiency_inputs_per_completion": per_success,
        "inputs_sent": completed,
        "inputs_attempted": attempted,
        "observations": observations,
        "stale_observations": stale,
        "observation_freshness_ratio": freshness,
        "recoveries": _count(evaluation.get("recoveries")),
        "recoveries_succeeded": _count(evaluation.get("recoveries_succeeded")),
        "recovery_success_rate": evaluation.get("recovery_success_rate"),
        "constraint_violations": _violation_count(evaluation),
        "seconds": evaluation.get("seconds"),
        "trajectory_entries": len(trajectory) if isinstance(trajectory, Sequence) else None,
        "reproducibility": {
            "environment_id": str(report.get("environment_id") or ""),
            "scenario": str(report.get("scenario") or ""),
            "objective": objective,
            "seed": objective.get("seed"),
        },
    }


def evaluate_embodied_sessions(reports: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Every session summarised, plus the spread across initial conditions.

    The spread is reported PER CONFIGURATION. Pooling it into one average is the
    thing the phase forbids: a scenario that is easy and a scenario that is
    impossible are two facts, not one mean.
    """
    rows = [summarise_embodied_session(report) for report in reports]
    configurations: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["environment"] or "unknown"
        bucket = configurations.setdefault(
            key,
            {
                "environment": key,
                "sessions": 0,
                "completed": 0,
                "scenarios": [],
                "inputs_sent": 0,
                "recoveries": 0,
                "constraint_violations": 0,
                "seconds": [],
            },
        )
        bucket["sessions"] += 1
        bucket["completed"] += 1 if row["objective_met"] else 0
        if row["scenario"] and row["scenario"] not in bucket["scenarios"]:
            bucket["scenarios"].append(row["scenario"])
        if row["inputs_sent"] is not None:
            bucket["inputs_sent"] += row["inputs_sent"]
        if row["recoveries"] is not None:
            bucket["recoveries"] += row["recoveries"]
        if row["constraint_violations"] is not None:
            bucket["constraint_violations"] += row["constraint_violations"]
        if row["seconds"] is not None:
            bucket["seconds"].append(row["seconds"])
    for bucket in configurations.values():
        bucket["scenarios"].sort()
        seconds = sorted(float(value) for value in bucket["seconds"])
        bucket["seconds"] = {
            "count": len(seconds),
            "min": seconds[0] if seconds else None,
            "max": seconds[-1] if seconds else None,
        }
    return {
        "contracts": [contract.to_dict() for contract in GAME_EVAL_CONTRACTS],
        "sessions": rows,
        "configurations": [configurations[key] for key in sorted(configurations)],
        "note": (
            "these are measurements of sessions this build ran in its OWN simulation; no "
            "real game was driven and no gameplay capability is claimed"
        ),
    }


def contracts_payload() -> list[dict[str, Any]]:
    return [contract.to_dict() for contract in GAME_EVAL_CONTRACTS]


def _count(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _violation_count(evaluation: Mapping[str, Any]) -> int | None:
    parts = [
        _count(evaluation.get("actions_rejected")),
        _count(evaluation.get("actions_failed")),
        _count(evaluation.get("safety_violations")),
        _count(evaluation.get("invalid_actions")),
    ]
    counted = [part for part in parts if part is not None]
    if not counted:
        return None
    return sum(counted)
