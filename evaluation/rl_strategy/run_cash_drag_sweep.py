"""Focused experimental cash-drag weight/scale sweep.

The default reward is never modified.  Each variant is an independent
``RewardConfig`` cloned from ``experimental_cash_drag_reward_config()`` with
only ``components["cash_drag"]`` and/or ``scales["cash_drag"]`` changed.

A variant is called *genuine* only when its production TrainedPolicy
evaluation has non-zero Q-value spread and at least one executed out-of-sample
trade.  Zero-Q tie BUYs are explicitly not counted as learned trading.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from novacontrol.novamarket.rewards.components import EXPERIMENTAL_COMPONENT_REGISTRY
from novacontrol.novamarket.rewards.engine import RewardEngine
from novacontrol.novamarket.rewards.models import experimental_cash_drag_reward_config

from evaluate_trained_adapters import evaluate
from run_rl_evaluation import (
    INSTRUMENTS,
    SEED,
    config_for,
    load_bars,
    split,
    train_policy,
)

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tmp" / "rl_evaluation" / "cash_drag_sweep"
EVIDENCE = ROOT / "evaluation" / "rl_strategy" / "cash_drag_sweep_results.json"

VARIANTS: dict[str, dict[str, float]] = {
    "w0.10_s0.01": {"weight": 0.10, "scale": 0.01},
    "w0.60_s0.02": {"weight": 0.60, "scale": 0.02},
    "w0.30_s0.10": {"weight": 0.30, "scale": 0.10},
}


def variant_config(label: str) -> Any:
    base = experimental_cash_drag_reward_config()
    components = dict(base.components)
    scales = dict(base.scales)
    components["cash_drag"] = VARIANTS[label]["weight"]
    scales["cash_drag"] = VARIANTS[label]["scale"]
    return replace(
        base,
        name=f"PI-14 cash-drag sweep {label}",
        version=f"cash-drag-sweep-{label}",
        components=components,
        scales=scales,
        description=(
            "Isolated cash-drag weight/scale sweep; default reward unchanged, "
            "experimental and never promoted."
        ),
    )


def _training_summary(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "training_action_distribution": meta.get("training_action_distribution"),
        "training_q_value_diagnostics": meta.get("training_q_value_diagnostics"),
        "best_validation_score": meta.get("best_validation_score"),
        "episodes": meta.get("episodes"),
    }


def run_variant(label: str) -> dict[str, Any]:
    config = variant_config(label)
    engine = RewardEngine(registry=EXPERIMENTAL_COMPONENT_REGISTRY)
    variant_dir = OUT / label
    policy_dir = variant_dir / "policies"
    policy_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    started = time.perf_counter()
    for instrument in INSTRUMENTS:
        bars = load_bars(instrument)
        train_bars, val_bars, test_bars = split(bars)
        agent, meta, _encoder = train_policy(
            instrument,
            bars,
            train_bars,
            val_bars,
            config,
            reward_engine=engine,
        )
        artifact = {
            "policy_version": f"cash-drag-sweep-{label}",
            "algorithm": "dqn",
            "instrument": instrument,
            "reward_config": config.name,
            "reward_config_hash": config.config_hash,
            "training": meta,
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
        path = policy_dir / f"{instrument.replace(':', '_')}.json"
        path.write_text(json.dumps(artifact, sort_keys=True), encoding="utf-8")
        evaluated = evaluate(path, reward_config=config, engine=engine)
        evaluated["training"] = _training_summary(meta)
        rows.append(evaluated)
        print(
            label,
            instrument,
            "trades=",
            evaluated.get("trades"),
            "q_spread=",
            (evaluated.get("q_value_diagnostics") or {}).get("max_spread"),
            flush=True,
        )
    training_actions: dict[str, int] = {
        name: 0 for name in ("buy", "sell", "hold", "wait", "no_action")
    }
    training_q_zero = 0
    training_q_observations = 0
    for row in rows:
        training = row.get("training") or {}
        for name, count in (training.get("training_action_distribution") or {}).items():
            training_actions[name] = training_actions.get(name, 0) + int(count or 0)
        q = training.get("training_q_value_diagnostics") or {}
        training_q_zero += int(q.get("zero_q_observations") or 0)
        training_q_observations += int(q.get("observations") or 0)
    genuine = [
        row
        for row in rows
        if int(row.get("trades") or 0) > 0
        and float((row.get("q_value_diagnostics") or {}).get("max_spread") or 0.0) > 1e-9
    ]
    report = {
        "variant": label,
        "config": config.to_dict(),
        "weight": VARIANTS[label]["weight"],
        "scale": VARIANTS[label]["scale"],
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "policies": len(rows),
        "trades": sum(int(row.get("trades") or 0) for row in rows),
        "policies_with_trades": sum(1 for row in rows if int(row.get("trades") or 0) > 0),
        "constraint_violations": sum(
            int(row.get("constraint_violations") or 0) for row in rows
        ),
        "test_q_observations": sum(
            int((row.get("q_value_diagnostics") or {}).get("observations") or 0)
            for row in rows
        ),
        "test_zero_q_observations": sum(
            int((row.get("q_value_diagnostics") or {}).get("zero_q_observations") or 0)
            for row in rows
        ),
        "test_max_spread": max(
            (
                float((row.get("q_value_diagnostics") or {}).get("max_spread") or 0.0)
                for row in rows
            ),
            default=0.0,
        ),
        "genuine_policies": len(genuine),
        "genuine_variant": bool(genuine),
        "training_action_distribution": training_actions,
        "training_q_observations": training_q_observations,
        "training_zero_q_observations": training_q_zero,
        "instruments": {row["instrument"]: row for row in rows},
    }
    variant_dir.mkdir(parents=True, exist_ok=True)
    (variant_dir / "results.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def combine(reports: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "classification": "SIMULATION — experimental cash-drag weight/scale sweep",
        "simulation_only": True,
        "default_reward_modified": False,
        "automatic_policy_promotion": False,
        "production_authority": False,
        "selection": "TrainedPolicy.select masked argmax on the test split",
        "genuine_definition": (
            "at least one executed out-of-sample trade and non-zero Q-value "
            "spread (max Q spread > 1e-9); zero-Q tie BUYs do not count"
        ),
        "baseline": {
            "weight": 0.30,
            "scale": 0.02,
            "source": "evaluation/rl_strategy/cash_drag_results_compact.json",
        },
        "variants": {report["variant"]: report for report in reports},
        "any_genuine_variant": any(report["genuine_variant"] for report in reports),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=sorted(VARIANTS))
    args = parser.parse_args()
    labels = [args.variant] if args.variant else list(VARIANTS)
    reports: list[dict[str, Any]] = []
    for label in labels:
        existing = OUT / label / "results.json"
        if existing.exists():
            reports.append(json.loads(existing.read_text(encoding="utf-8")))
        else:
            reports.append(run_variant(label))
        print("completed", label, flush=True)
    evidence = combine(reports)
    EVIDENCE.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2, sort_keys=True)[:6000], flush=True)
    print("wrote", EVIDENCE, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
