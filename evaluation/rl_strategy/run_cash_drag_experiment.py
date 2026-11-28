"""Train and test the experimental cash-drag reward in isolation.

Reuses the reproducible split, environment and DQN setup from
``run_rl_evaluation.py``.  The default reward configuration is never passed to
this run and is never modified.  Writes the full report under
``tmp/rl_evaluation/cash_drag``, tracked policy artifacts under
``evaluation/rl_strategy/artifacts/cash_drag`` and a compact tracked extract
under ``evaluation/rl_strategy``.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evaluate_trained_adapters import evaluate
from run_rl_evaluation import (
    INSTRUMENTS,
    OUT,
    SELECTION,
    TEST_BARS_SOURCE,
    aggregate,
    load_bars,
    split,
    train_policy,
    write_test_bars_snapshot,
)

from novacontrol.novamarket.rewards.components import EXPERIMENTAL_COMPONENT_REGISTRY
from novacontrol.novamarket.rewards.engine import RewardEngine
from novacontrol.novamarket.rewards.models import (
    default_reward_config,
    experimental_cash_drag_reward_config,
)

CASH_DRAG_OUT = OUT / "cash_drag"
EVIDENCE_DIR = Path(__file__).resolve().parent
ROOT = EVIDENCE_DIR.parents[1]
POLICY_DIR = EVIDENCE_DIR / "artifacts" / "cash_drag" / "policies"
COMPACT_PATH = EVIDENCE_DIR / "cash_drag_results_compact.json"


def _learning_diagnostic(report: dict[str, Any]) -> dict[str, Any]:
    rows = list(report["instruments"].values())
    training = [row.get("training") or {} for row in rows]
    test_q = [row.get("q_value_diagnostics") or {} for row in rows]
    training_actions: dict[str, int] = {
        name: 0 for name in ("buy", "sell", "hold", "wait", "no_action")
    }
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
        instrument: (row.get("training") or {}).get("best_validation_score")
        for instrument, row in report["instruments"].items()
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


def _compact_training(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        key: meta.get(key)
        for key in (
            "instrument",
            "reward_config",
            "reward_config_hash",
            "train_bars",
            "validation_bars",
            "episodes",
            "best_validation_score",
            "elapsed_seconds",
            "seed",
            "algorithm",
            "algorithm_version",
            "training_action_distribution",
            "training_q_value_diagnostics",
        )
    }


def main() -> None:
    CASH_DRAG_OUT.mkdir(parents=True, exist_ok=True)
    POLICY_DIR.mkdir(parents=True, exist_ok=True)
    config = experimental_cash_drag_reward_config()
    default = default_reward_config()
    engine = RewardEngine(registry=EXPERIMENTAL_COMPONENT_REGISTRY)
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "experiment": "experimental cash-drag reward",
        "simulation_only": True,
        "production_authority": False,
        "automatic_policy_promotion": False,
        "source_database": "data/novamarket.sqlite3",
        "source_mode": "read-only",
        "reward_config": config.to_dict(),
        "default_reward": {
            "config_hash": default.config_hash,
            "cash_drag_enabled": "cash_drag" in default.enabled,
            "cash_drag_weighted": "cash_drag" in default.components,
        },
        "component_registry": sorted(EXPERIMENTAL_COMPONENT_REGISTRY),
        "split": "train/validation/test from run_rl_evaluation",
        "instruments": {},
        "aggregate": {},
        "errors": [],
    }
    rows: list[dict[str, Any]] = []
    for instrument in INSTRUMENTS:
        try:
            bars = load_bars(instrument)
            train_bars, val_bars, test_bars = split(bars)
            if min(len(train_bars), len(val_bars), len(test_bars)) < 25:
                raise RuntimeError(
                    "insufficient bars: "
                    f"train={len(train_bars)} val={len(val_bars)} test={len(test_bars)}"
                )
            print(
                instrument,
                "train/val/test",
                len(train_bars),
                len(val_bars),
                len(test_bars),
                flush=True,
            )
            started = time.perf_counter()
            agent, meta, encoder = train_policy(
                instrument,
                bars,
                train_bars,
                val_bars,
                config,
                reward_engine=engine,
            )
            artifact = {
                "policy_version": "cash-drag-eval-v1",
                "algorithm": "dqn",
                "instrument": instrument,
                "reward_config": config.name,
                "reward_config_hash": config.config_hash,
                "training": _compact_training(meta),
                "state": {
                    key: value
                    for key, value in agent.save_state().items()
                    if key != "replay"
                },
                "simulated": True,
                "advisory_only": True,
                "production_authority": False,
                "activation_available": False,
            }
            artifact_path = POLICY_DIR / f"{instrument.replace(':', '_')}_cash_drag.json"
            artifact_path.write_text(json.dumps(artifact, sort_keys=True), encoding="utf-8")
            snapshot_path = write_test_bars_snapshot(
                instrument, train_bars, val_bars, test_bars
            )
            print(
                "test-bars",
                snapshot_path.relative_to(ROOT).as_posix(),
                flush=True,
            )
            # The compact evidence is the adapter rescore of the saved artifact,
            # not the harness episode.  A regression in the local selector must
            # never be able to overwrite the tracked evidence with zero trades.
            rescored = evaluate(artifact_path, reward_config=config, engine=engine)
            if "TrainedPolicy" not in str(rescored.get("selection")):
                raise RuntimeError(
                    "adapter rescore did not use TrainedPolicy for "
                    f"{instrument}; refusing to write compact evidence"
                )
            rescored["training"] = _compact_training(meta)
            rescored["parameters"] = sum(
                len(row) for layer in agent.online.weights for row in layer
            ) + sum(len(layer) for layer in agent.online.biases)
            rescored["encoder"] = encoder
            rescored["artifact_path"] = artifact_path.relative_to(ROOT).as_posix()
            rescored["artifact_bytes"] = artifact_path.stat().st_size
            rescored["elapsed_seconds"] = round(time.perf_counter() - started, 4)
            report["instruments"][instrument] = rescored
            rows.append(rescored)
        except Exception as exc:  # noqa: BLE001 - retain one instrument's failure
            report["errors"].append(f"{instrument}: {type(exc).__name__}: {exc}")
            print("ERROR", instrument, exc, flush=True)

    report["aggregate"] = {
        "returns": aggregate(rows, "total_return"),
        "drawdowns": aggregate(rows, "max_drawdown"),
        "sharpes": aggregate(rows, "sharpe"),
        "trades": sum(int(row.get("trades") or 0) for row in rows),
        "turnover": aggregate(rows, "turnover"),
        "total_fees": sum(float(row.get("fees_paid") or 0) for row in rows),
    }
    trade_total = int(report["aggregate"]["trades"])
    report["out_of_sample"] = {
        "split": "test",
        "trades": trade_total,
        "instruments_with_trades": sum(
            1 for row in rows if int(row.get("trades") or 0) > 0
        ),
        "policy_produced_non_zero_trades": trade_total > 0,
        "selection": SELECTION,
    }
    report["learning_diagnostic"] = _learning_diagnostic(report)
    report["out_of_sample_reevaluation"] = {
        "evaluated_at": report["generated_at"],
        "script": "evaluation/rl_strategy/evaluate_trained_adapters.py",
        "runner": "evaluation/rl_strategy/run_cash_drag_experiment.py",
        "source_artifacts": [
            "evaluation/rl_strategy/artifacts/cash_drag/policies/*.json",
            TEST_BARS_SOURCE,
        ],
        "selection": SELECTION,
        "note": (
            "run_cash_drag_experiment.py trains and saves each policy artifact, "
            "then rescores it through the shared adapter evaluator "
            "(TrainedPolicy.select masked argmax) before writing this compact. "
            "Training and validation selection were unchanged."
        ),
    }
    if not rows or not all(
        "TrainedPolicy" in str(row.get("selection")) for row in rows
    ):
        raise RuntimeError(
            "refusing to overwrite compact evidence: the run has no "
            "TrainedPolicy-rescored out-of-sample rows"
        )
    (CASH_DRAG_OUT / "results.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    COMPACT_PATH.write_text(
        json.dumps(
            {
                "artifact": "tracked compact extract",
                "generated_at": report["generated_at"],
                "experiment": report["experiment"],
                "reward_config": report["reward_config"],
                "default_reward": report["default_reward"],
                "split": report["split"],
                "out_of_sample": report["out_of_sample"],
                "out_of_sample_reevaluation": report[
                    "out_of_sample_reevaluation"
                ],
                "learning_diagnostic": report["learning_diagnostic"],
                "aggregate": report["aggregate"],
                "instruments": {
                    instrument: {
                        "trades": row.get("trades"),
                        "total_return": row.get("total_return"),
                        "total_reward": row.get("total_reward"),
                        "average_reward": row.get("average_reward"),
                        "final_value": row.get("final_value"),
                        "action_distribution": row.get("action_distribution"),
                        "fees_paid": row.get("fees_paid"),
                        "invalid_actions": row.get("invalid_actions"),
                        "constraint_violations": row.get("constraint_violations"),
                        "q_value_diagnostics": row.get("q_value_diagnostics"),
                        "selection": row.get("selection"),
                        "artifact": row.get("artifact"),
                        "training": row.get("training"),
                    }
                    for instrument, row in report["instruments"].items()
                },
                "errors": report["errors"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["out_of_sample"], indent=2), flush=True)
    print("wrote", CASH_DRAG_OUT / "results.json", flush=True)
    print("wrote", COMPACT_PATH, flush=True)


if __name__ == "__main__":
    main()
