"""Out-of-sample evaluation through the actual TrainedPolicy adapter.

The original harness's ``choose_action`` intentionally turns a zero-Q tie into
HOLD.  ``TrainedPolicy.select`` instead takes a masked argmax, so a zero-Q tie
selects the first legal action (BUY).  This script evaluates the tracked policy
artifacts under ``evaluation/rl_strategy/artifacts`` through the adapter users
actually receive.  It reads the tracked test-split snapshots under
``evaluation/rl_strategy/artifacts/test_bars`` and never opens the untracked
``data/novamarket.sqlite3``.  It does not train, promote or register a policy.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from run_rl_evaluation import (
    INITIAL,
    INSTRUMENTS,
    SELECTION,
    TEST_BARS_SOURCE,
    TRADING_REWARD,
    _compact_policy,
    _compact_verdict,
    aggregate,
    bootstrap_mean,
    compact_rl,
    config_for,
    load_test_bars_snapshot,
    policy_parameter_count,
)

from novacontrol.novamarket.agent_training.agent import DQNAgent, TrainedPolicy
from novacontrol.novamarket.rewards.components import EXPERIMENTAL_COMPONENT_REGISTRY
from novacontrol.novamarket.rewards.engine import RewardEngine
from novacontrol.novamarket.rewards.metrics import policy_metrics
from novacontrol.novamarket.rewards.models import (
    TransitionView,
    default_reward_config,
    experimental_cash_drag_reward_config,
)
from novacontrol.novamarket.rl.environment import RLEnvironment

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "evaluation" / "rl_strategy" / "artifacts"
ARTIFACT_ROOT = ARTIFACTS.resolve()
OUT = ROOT / "tmp" / "rl_evaluation" / "adapter_evaluation.json"
MAIN_COMPACT = ROOT / "evaluation" / "rl_strategy" / "results_compact.json"
CASH_DRAG_COMPACT = (
    ROOT / "evaluation" / "rl_strategy" / "cash_drag_results_compact.json"
)
MAIN_SOURCE_ARTIFACTS = (
    "evaluation/rl_strategy/artifacts/policies/*.json",
    TEST_BARS_SOURCE,
)
CASH_DRAG_SOURCE_ARTIFACTS = (
    "evaluation/rl_strategy/artifacts/cash_drag/policies/*.json",
    TEST_BARS_SOURCE,
)
REPRODUCTION_COMMAND = (
    "PYTHONPATH=src ../.venv/Scripts/python.exe "
    "evaluation/rl_strategy/evaluate_trained_adapters.py"
)


def evaluate(path: Path, *, reward_config: Any, engine: RewardEngine) -> dict[str, Any]:
    artifact = json.loads(path.read_text(encoding="utf-8"))
    agent = DQNAgent.from_state(artifact["state"])
    policy = TrainedPolicy(agent, name=f"adapter-{path.stem}")
    instrument = str(artifact["instrument"])
    test_bars, snapshot = load_test_bars_snapshot(instrument)
    config = config_for(instrument, test_bars, "adapter-test")
    env = RLEnvironment(config, test_bars)
    observation = env.reset(f"adapter-test-{instrument}")
    history: list[dict[str, Any]] = []
    views: list[TransitionView] = []
    rewards: list[dict[str, Any]] = []
    actions: list[str] = []
    q_values_seen: list[list[float]] = []
    q_spreads: list[float] = []
    while env.status.value == "running":
        q_values_seen.append(agent.q_values(agent.encoder.encode(observation.to_dict())))
        q_spreads.append(max(q_values_seen[-1]) - min(q_values_seen[-1]))
        decision = policy.select(
            observation=observation.to_dict(),
            portfolio=env.portfolio.to_dict(),
            environment=env.config.to_dict(),
            history=history,
        )
        action = decision.action
        actions.append(str(action.get("action_type")))
        observation, _raw, done, truncated, transition = env.step(action)
        payload = transition.to_dict()
        reward = engine.calculate(
            payload,
            config=reward_config,
            history=history,
            environment=env.config.to_dict(),
        )
        history.append(payload)
        rewards.append(reward.to_dict())
        views.append(
            TransitionView.from_transition(
                payload, environment=env.config.to_dict()
            )
        )
        if done or truncated:
            break
    metrics, _ = policy_metrics(
        initial_capital=INITIAL,
        transitions=views,
        rewards=rewards,
    )
    compact = compact_rl(metrics)
    compact.update(
        {
            "instrument": instrument,
            "artifact": path.relative_to(ROOT).as_posix(),
            "artifact_bytes": path.stat().st_size,
            "parameters": policy_parameter_count(agent),
            "model": artifact.get("reward_config"),
            "bars": snapshot["bars"],
            "train_bars": snapshot["train_bars"],
            "validation_bars": snapshot["validation_bars"],
            "test_bars": snapshot["test_bars"],
            "test_first": snapshot["test_first"],
            "test_last": snapshot["test_last"],
            "test_bars_snapshot": snapshot["path"],
            "training": artifact.get("training") or {},
            "actions_seen": sorted(set(actions)),
            "action_count": len(actions),
            "q_value_diagnostics": {
                "observations": len(q_values_seen),
                "min_q": min((value for values in q_values_seen for value in values), default=None),
                "max_q": max((value for values in q_values_seen for value in values), default=None),
                "max_spread": max(q_spreads) if q_spreads else None,
                "mean_spread": (
                    round(sum(q_spreads) / len(q_spreads), 12) if q_spreads else None
                ),
                "all_equal_observations": sum(
                    1 for spread in q_spreads if spread <= 1e-12
                ),
                "zero_q_observations": sum(
                    1
                    for values in q_values_seen
                    if all(abs(value) <= 1e-12 for value in values)
                ),
            },
            "selection": "TrainedPolicy.select masked argmax",
        }
    )
    return compact


def _is_under_artifacts(path: Path) -> bool:
    try:
        path.relative_to(ARTIFACT_ROOT)
    except ValueError:
        return False
    return True


def validate_source_artifacts(source_artifacts: Any) -> list[Path]:
    if isinstance(source_artifacts, (str, Path)):
        patterns = [str(source_artifacts)]
    elif isinstance(source_artifacts, (list, tuple)):
        patterns = [str(item) for item in source_artifacts]
    else:
        raise ValueError("source_artifacts must be a path or list of paths")
    if not patterns:
        raise ValueError("source_artifacts is empty")
    matches: list[Path] = []
    for pattern in patterns:
        candidate = Path(pattern)
        if candidate.is_absolute():
            raise ValueError(f"source_artifacts must be relative: {pattern}")
        if any(char in str(candidate.parent) for char in "*?["):
            raise ValueError(
                f"source_artifacts parent must not be a glob: {pattern}"
            )
        parent = (ROOT / candidate.parent).resolve()
        if not _is_under_artifacts(parent):
            raise ValueError(
                "source_artifacts resolves outside "
                f"evaluation/rl_strategy/artifacts/: {pattern}"
            )
        found = sorted(ROOT.glob(pattern))
        if not found:
            raise ValueError(f"source_artifacts matched no files: {pattern}")
        for path in found:
            resolved = path.resolve()
            if not _is_under_artifacts(resolved):
                raise ValueError(
                    "source_artifacts resolves outside "
                    f"evaluation/rl_strategy/artifacts/: {pattern}"
                )
            matches.append(resolved)
    return matches


def _load_template(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _main_label(row: dict[str, Any]) -> str:
    return "rl_trading" if row.get("model") == "trading" else "rl_default"


def _policy_aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    returns = aggregate(rows, "total_return")
    sharpes = aggregate(rows, "sharpe")
    drawdowns = aggregate(rows, "max_drawdown")
    return {
        "return_mean": returns.get("mean"),
        "return_min": returns.get("min"),
        "return_max": returns.get("max"),
        "sharpe_mean": sharpes.get("mean"),
        "drawdown_mean": drawdowns.get("mean"),
        "trades": sum(int(row.get("trades") or 0) for row in rows),
        "total_fees": sum(float(row.get("fees_paid") or 0.0) for row in rows),
        "total_slippage": round(
            sum(float(row.get("total_slippage") or 0.0) for row in rows), 6
        ),
    }


def _paired_differences(
    instruments: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for label in ("rl_default", "rl_trading"):
        for baseline in ("day_momentum", "swing_trend", "long_horizon_proxy"):
            diffs: list[float] = []
            for instrument in INSTRUMENTS:
                instrument_row = instruments.get(instrument) or {}
                rl = instrument_row.get(label) or {}
                base = (instrument_row.get("baselines") or {}).get(baseline) or {}
                if (
                    rl.get("total_return") is not None
                    and base.get("total_return") is not None
                ):
                    diffs.append(
                        float(rl["total_return"]) - float(base["total_return"])
                    )
            result[f"{label}_minus_{baseline}"] = bootstrap_mean(diffs)
    return result


def _main_reevaluation(now: str) -> dict[str, Any]:
    return {
        "evaluated_at": now,
        "script": "evaluation/rl_strategy/evaluate_trained_adapters.py",
        "runner": "evaluation/rl_strategy/evaluate_trained_adapters.py",
        "source_artifacts": MAIN_SOURCE_ARTIFACTS,
        "selection": SELECTION,
        "note": (
            "evaluate_trained_adapters.py rescores the tracked policy "
            "artifacts through TrainedPolicy.select and writes this compact "
            "directly; training and validation selection were unchanged."
        ),
    }


def _build_main_compact(
    rows: list[dict[str, Any]], template: dict[str, Any], now: str
) -> dict[str, Any]:
    compact = copy.deepcopy(template)
    compact.setdefault("artifact", "compact tracked extract")
    compact.setdefault(
        "source_harness", "evaluation/rl_strategy/run_rl_evaluation.py"
    )
    compact.setdefault("full_runtime_output", "tmp/rl_evaluation/results.json")
    compact.setdefault("full_runtime_output_tracked", False)
    compact.setdefault("simulation_only", True)
    compact.setdefault("production_authority", False)
    compact.setdefault("automatic_policy_promotion", False)
    compact.setdefault("source_mode", "read-only")
    compact["source_database"] = "data/novamarket.sqlite3"
    compact.setdefault("initial_capital", str(INITIAL))
    compact.setdefault("split", {})
    compact.setdefault("execution_assumptions", {})
    compact.setdefault("existing_registry", {})
    compact.setdefault("hardware", {})
    compact.setdefault("resources", {})
    compact.setdefault("reward_configs", {})
    compact.setdefault("generated_at", now)
    compact.setdefault(
        "isolation",
        {
            "source_opened_read_only": True,
            "runtime_outputs_under_tmp_rl_evaluation": True,
            "policy_artifacts_under_evaluation_rl_strategy_artifacts": True,
            "production_portfolio_opened": False,
            "production_ledger_opened": False,
            "risk_engine_opened": False,
            "order_gateway_opened": False,
            "autonomy_state_opened": False,
            "policy_registry_mutated": False,
            "policy_activated_or_promoted": False,
        },
    )

    instruments = copy.deepcopy(template.get("instruments") or {})
    for row in rows:
        instrument = str(row["instrument"])
        slot = instruments.setdefault(instrument, {})
        for key in (
            "bars",
            "train_bars",
            "validation_bars",
            "test_bars",
            "test_first",
            "test_last",
            "test_bars_snapshot",
        ):
            slot[key] = row.get(key)
        slot[_main_label(row)] = _compact_policy(row)
    compact["instruments"] = instruments
    compact["test_bars_snapshot"] = TEST_BARS_SOURCE
    compact["test_bars_snapshots"] = {
        str(row["instrument"]): row["test_bars_snapshot"] for row in rows
    }

    aggregate_rows = copy.deepcopy(template.get("aggregate") or {})
    for label in ("rl_default", "rl_trading"):
        label_rows = [row for row in rows if _main_label(row) == label]
        aggregate_rows[label] = _policy_aggregate(label_rows)
    compact["aggregate"] = aggregate_rows
    compact["verdict"] = _compact_verdict(
        {"instruments": instruments, "aggregate": aggregate_rows}
    )
    compact["paired_differences"] = _paired_differences(instruments)
    compact["out_of_sample_reevaluation"] = _main_reevaluation(now)
    compact["reproduction_command"] = REPRODUCTION_COMMAND
    compact["reproduction_exit_code"] = 0
    compact["verified_at"] = now
    compact["errors"] = []
    return compact


def _cash_aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "returns": aggregate(rows, "total_return"),
        "drawdowns": aggregate(rows, "max_drawdown"),
        "sharpes": aggregate(rows, "sharpe"),
        "trades": sum(int(row.get("trades") or 0) for row in rows),
        "turnover": aggregate(rows, "turnover"),
        "total_fees": sum(float(row.get("fees_paid") or 0.0) for row in rows),
    }


def _cash_out_of_sample(rows: list[dict[str, Any]]) -> dict[str, Any]:
    trades = sum(int(row.get("trades") or 0) for row in rows)
    return {
        "split": "test",
        "trades": trades,
        "instruments_with_trades": sum(
            1 for row in rows if int(row.get("trades") or 0) > 0
        ),
        "policy_produced_non_zero_trades": trades > 0,
        "selection": SELECTION,
    }


def _cash_learning_diagnostic(rows: list[dict[str, Any]]) -> dict[str, Any]:
    training = [row.get("training") or {} for row in rows]
    test_q = [row.get("q_value_diagnostics") or {} for row in rows]
    training_actions: dict[str, int] = dict.fromkeys(
        ("buy", "sell", "hold", "wait", "no_action"), 0
    )
    for item in training:
        for name, count in (item.get("training_action_distribution") or {}).items():
            training_actions[name] = training_actions.get(name, 0) + int(count or 0)
    observations = sum(int(q.get("observations") or 0) for q in test_q)
    zero = sum(int(q.get("zero_q_observations") or 0) for q in test_q)
    all_equal = sum(int(q.get("all_equal_observations") or 0) for q in test_q)
    max_spread = max(
        (float(q.get("max_spread") or 0.0) for q in test_q), default=None
    )
    best_scores = {
        str(row["instrument"]): (row.get("training") or {}).get(
            "best_validation_score"
        )
        for row in rows
    }
    if observations and zero == observations and (max_spread or 0.0) <= 1e-12:
        verdict = "did_not_learn"
        positive_validation = any(
            score is not None and float(score) != 0.0
            for score in best_scores.values()
        )
        reason = (
            "Every saved checkpoint has zero Q-value spread on every test "
            "observation; "
            + (
                "training remains HOLD-dominated despite non-zero validation scores."
                if positive_validation
                else "the best validation score is 0.0 and training remains "
                "HOLD-dominated."
            )
        )
    elif max_spread is not None and max_spread > 1e-9:
        verdict = "learned_signal"
        reason = (
            "Saved checkpoints produce non-zero Q-value spread on the test split; "
            "review the executed trades before any promotion."
        )
    else:
        verdict = "inconclusive"
        reason = (
            "The test split does not show a stable learned Q-value preference."
        )
    return {
        "verdict": verdict,
        "reason": reason,
        "test_q_observations": observations,
        "test_zero_q_observations": zero,
        "test_all_equal_observations": all_equal,
        "test_max_spread": max_spread,
        "best_validation_scores": best_scores,
        "training_action_distribution": training_actions,
        "training_q_observations": sum(
            int((item.get("training_q_value_diagnostics") or {}).get("observations") or 0)
            for item in training
        ),
        "training_zero_q_observations": sum(
            int(
                (item.get("training_q_value_diagnostics") or {}).get(
                    "zero_q_observations"
                )
                or 0
            )
            for item in training
        ),
    }


def _cash_instruments(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for row in rows:
        result[str(row["instrument"])] = {
            key: row.get(key)
            for key in (
                "trades",
                "total_return",
                "total_reward",
                "average_reward",
                "final_value",
                "action_distribution",
                "fees_paid",
                "invalid_actions",
                "constraint_violations",
                "q_value_diagnostics",
                "selection",
                "artifact",
                "training",
                "test_bars_snapshot",
            )
        }
    return result


def _build_cash_compact(
    rows: list[dict[str, Any]], template: dict[str, Any], now: str
) -> dict[str, Any]:
    config = experimental_cash_drag_reward_config()
    default = default_reward_config()
    compact = copy.deepcopy(template)
    compact["artifact"] = "tracked compact extract"
    compact.setdefault("generated_at", now)
    compact.setdefault("experiment", "experimental cash-drag reward")
    compact["reward_config"] = config.to_dict()
    compact["default_reward"] = {
        "config_hash": default.config_hash,
        "cash_drag_enabled": "cash_drag" in default.enabled,
        "cash_drag_weighted": "cash_drag" in default.components,
    }
    compact.setdefault("split", "train/validation/test from run_rl_evaluation")
    compact["test_bars_snapshot"] = TEST_BARS_SOURCE
    compact["test_bars_snapshots"] = {
        str(row["instrument"]): row["test_bars_snapshot"] for row in rows
    }
    compact["instruments"] = _cash_instruments(rows)
    compact["aggregate"] = _cash_aggregate(rows)
    compact["out_of_sample"] = _cash_out_of_sample(rows)
    compact["learning_diagnostic"] = _cash_learning_diagnostic(rows)
    compact["errors"] = []
    compact["out_of_sample_reevaluation"] = {
        "evaluated_at": now,
        "script": "evaluation/rl_strategy/evaluate_trained_adapters.py",
        "runner": "evaluation/rl_strategy/evaluate_trained_adapters.py",
        "source_artifacts": CASH_DRAG_SOURCE_ARTIFACTS,
        "selection": SELECTION,
        "note": (
            "evaluate_trained_adapters.py rescores the tracked cash-drag "
            "artifacts through TrainedPolicy.select and writes this compact "
            "directly; training and validation selection were unchanged."
        ),
    }
    compact["reproduction_command"] = REPRODUCTION_COMMAND
    compact["reproduction_exit_code"] = 0
    compact["verified_at"] = now
    return compact


def _collect_source_artifacts(value: Any) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "source_artifacts":
                found.append(child)
            else:
                found.extend(_collect_source_artifacts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_collect_source_artifacts(child))
    return found


def validate_compact_source_artifacts(compact: dict[str, Any]) -> list[Path]:
    values = _collect_source_artifacts(compact)
    if not values:
        raise ValueError("compact has no source_artifacts")
    matches: list[Path] = []
    for value in values:
        matches.extend(validate_source_artifacts(value))
    return matches


def _write_compact(path: Path, compact: dict[str, Any]) -> None:
    validate_compact_source_artifacts(compact)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(compact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    now = datetime.now(UTC).isoformat()
    validate_source_artifacts(MAIN_SOURCE_ARTIFACTS)
    validate_source_artifacts(CASH_DRAG_SOURCE_ARTIFACTS)
    cash_drag = experimental_cash_drag_reward_config()
    default = default_reward_config()
    cash_engine = RewardEngine(registry=EXPERIMENTAL_COMPONENT_REGISTRY)
    default_engine = RewardEngine()
    policy_dir = ARTIFACTS / "policies"
    cash_dir = ARTIFACTS / "cash_drag" / "policies"
    main_rows: list[dict[str, Any]] = []
    cash_rows: list[dict[str, Any]] = []
    for directory in (policy_dir, cash_dir):
        if not directory.is_dir():
            raise SystemExit(
                f"missing tracked policy artifacts under {directory}; "
                "run the matching training harness first"
            )
    for path in sorted(policy_dir.glob("*.json")):
        config = (
            TRADING_REWARD if "_trading" in path.name else default
        )
        main_rows.append(
            evaluate(path, reward_config=config, engine=default_engine)
        )
    for path in sorted(cash_dir.glob("*.json")):
        cash_rows.append(
            evaluate(path, reward_config=cash_drag, engine=cash_engine)
        )
    if not main_rows:
        raise SystemExit("no main policy artifacts were evaluated")
    if not cash_rows:
        raise SystemExit("no cash-drag policy artifacts were evaluated")
    rows = main_rows + cash_rows
    report = {
        "generated_at": now,
        "classification": "SIMULATION — out-of-sample evaluation through TrainedPolicy",
        "selection": "TrainedPolicy.select: masked argmax (zero-Q tie selects first legal action)",
        "automatic_policy_promotion": False,
        "production_authority": False,
        "rows": rows,
        "totals": {
            "policies": len(rows),
            "trades": sum(int(row.get("trades") or 0) for row in rows),
            "policies_with_trades": sum(
                1 for row in rows if int(row.get("trades") or 0) > 0
            ),
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    main_compact = _build_main_compact(
        main_rows, _load_template(MAIN_COMPACT), now
    )
    cash_compact = _build_cash_compact(
        cash_rows, _load_template(CASH_DRAG_COMPACT), now
    )
    _write_compact(MAIN_COMPACT, main_compact)
    _write_compact(CASH_DRAG_COMPACT, cash_compact)
    print(json.dumps(report["totals"], indent=2))
    for row in rows:
        print(
            row["instrument"],
            row["model"],
            "trades=" + str(row["trades"]),
            "return=" + str(row["total_return"]),
            "actions=" + json.dumps(row["action_distribution"], sort_keys=True),
        )
    print("wrote", OUT, flush=True)
    print("wrote", MAIN_COMPACT, flush=True)
    print("wrote", CASH_DRAG_COMPACT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
