"""Isolated, reproducible RL vs conventional-strategy evaluation.

Reads data/novamarket.sqlite3 read-only.  Runtime output is written under
tmp/rl_evaluation/; tracked policy artifacts and exact test-split snapshots are
written under evaluation/rl_strategy/artifacts/.  No production portfolio, ledger, risk
engine, execution engine, autonomy state or policy registry is opened or
mutated.  Trained agents are local JSON files,
not registered policies and not activatable.
"""

from __future__ import annotations

import copy
import json
import math
import platform
import random
import sqlite3
import statistics
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from novacontrol.novamarket.agent_training.agent import (
    ACTION_NAMES,
    DQNAgent,
    ObservationEncoder,
    TrainedPolicy,
    action_payload,
)
from novacontrol.novamarket.backtesting.core import (
    BacktestConfig,
    BacktestEngine,
    BacktestStore,
    HistoricalBar,
)
from novacontrol.novamarket.execution.config import ExecutionConfig
from novacontrol.novamarket.rewards.engine import RewardEngine
from novacontrol.novamarket.rewards.metrics import policy_metrics
from novacontrol.novamarket.rewards.models import (
    DEFAULT_COMPONENTS,
    DEFAULT_SCALES,
    DEFAULT_THRESHOLDS,
    RewardConfig,
    TransitionView,
)
from novacontrol.novamarket.rl.environment import RLEnvironment
from novacontrol.novamarket.rl.models import RLEnvironmentConfig, RLInvalidActionError

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "novamarket.sqlite3"
OUT = ROOT / "tmp" / "rl_evaluation"
ARTIFACT_DIR = ROOT / "evaluation" / "rl_strategy" / "artifacts" / "policies"
TEST_BARS_DIR = ROOT / "evaluation" / "rl_strategy" / "artifacts" / "test_bars"
TEST_BARS_SCHEMA = "novacontrol.rl_evaluation.test_bars.v1"
TEST_BARS_SOURCE = "evaluation/rl_strategy/artifacts/test_bars/*.json"
COMPACT_PATH = ROOT / "evaluation" / "rl_strategy" / "results_compact.json"
BACKTEST_DB = OUT / "backtests.sqlite3"

INSTRUMENTS = (
    "XNSE:RELIANCE",
    "XNSE:HDFCBANK",
    "XNSE:TCS",
    "XNSE:ITC",
    "XNSE:TATASTEEL",
)
START = datetime(2025, 11, 4, tzinfo=UTC)
TRAIN_END = datetime(2026, 5, 1, tzinfo=UTC)
VAL_END = datetime(2026, 7, 1, tzinfo=UTC)
TEST_END = datetime(2026, 11, 11, tzinfo=UTC)
INITIAL = Decimal("100000.00")
SEED = 7
SELECTION = (
    "TrainedPolicy.select: masked argmax (zero-Q tie selects first legal action)"
)
TRAIN_EPISODES = 20
VALIDATE_EVERY = 5
BATCH_SIZE = 32

DEFAULT_WEIGHTS = {
    "profitability": 0.20,
    "risk": 0.15,
    "transaction_cost": 0.10,
    "decision_quality": 0.15,
    "forecast_utilization": 0.10,
    "discipline": 0.10,
    "portfolio_management": 0.10,
    "execution_quality": 0.10,
}
TRADING_WEIGHTS = {
    "profitability": 0.60,
    "risk": 0.10,
    "transaction_cost": 0.15,
    "decision_quality": 0.00,
    "forecast_utilization": 0.00,
    "discipline": 0.15,
    "portfolio_management": 0.00,
    "execution_quality": 0.00,
}


def reward_config(name: str, weights: dict[str, float]) -> RewardConfig:
    return RewardConfig(
        name=name,
        version="rl-eval-1",
        components=weights,
        enabled=DEFAULT_COMPONENTS,
        scales=DEFAULT_SCALES,
        thresholds=DEFAULT_THRESHOLDS,
        experimental=True,
        description=(
            "Isolated evaluation configuration. Fixed scales; never promoted. "
            "No test split is used to select it."
        ),
    )


DEFAULT_REWARD = reward_config("evaluation-default", DEFAULT_WEIGHTS)
TRADING_REWARD = reward_config("evaluation-trading", TRADING_WEIGHTS)


def load_bars(instrument: str) -> list[HistoricalBar]:
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = connection.execute(
        "select observed_at,open,high,low,close,volume,provider,provider_metadata_json "
        "from market_bars where instrument_id=? and interval='1d' and partial=0 "
        "and observed_at>=? and observed_at<=? order by observed_at",
        (instrument, START.isoformat(), TEST_END.isoformat()),
    ).fetchall()
    connection.close()
    bars: list[HistoricalBar] = []
    for observed, open_, high, low, close, volume, provider, metadata in rows:
        meta = json.loads(metadata or "{}")
        bars.append(
            HistoricalBar(
                instrument_id=instrument,
                observed_at=datetime.fromisoformat(str(observed).replace("Z", "+00:00")),
                open=Decimal(str(open_)),
                high=Decimal(str(high)),
                low=Decimal(str(low)),
                close=Decimal(str(close)),
                volume=Decimal(str(volume)),
                source=str(provider),
                adjusted=bool(meta.get("adjusted", False)),
            )
        )
    return bars


def split(bars: list[HistoricalBar]) -> tuple[list[HistoricalBar], list[HistoricalBar], list[HistoricalBar]]:
    train = [bar for bar in bars if bar.observed_at < TRAIN_END]
    val = [bar for bar in bars if TRAIN_END <= bar.observed_at <= VAL_END]
    test = [bar for bar in bars if VAL_END < bar.observed_at <= TEST_END]
    return train, val, test


def test_bars_snapshot_path(instrument: str) -> Path:
    return TEST_BARS_DIR / f"{instrument.replace(':', '_')}.json"


def write_test_bars_snapshot(
    instrument: str,
    train_bars: list[HistoricalBar],
    validation_bars: list[HistoricalBar],
    test_bars: list[HistoricalBar],
) -> Path:
    """Write the exact out-of-sample bars used by every adapter evaluation."""
    if not test_bars:
        raise ValueError(f"no test bars to snapshot for {instrument}")
    path = test_bars_snapshot_path(instrument)
    payload = {
        "schema": TEST_BARS_SCHEMA,
        "instrument": instrument,
        "source_database": "data/novamarket.sqlite3",
        "source_mode": "read-only",
        "split": {
            "train_start": START.isoformat(),
            "train_end_exclusive": TRAIN_END.isoformat(),
            "validation_start": TRAIN_END.isoformat(),
            "validation_end_inclusive": VAL_END.isoformat(),
            "test_start_exclusive": VAL_END.isoformat(),
            "test_end_inclusive": TEST_END.isoformat(),
            "chronological": True,
        },
        "counts": {
            "bars": len(train_bars) + len(validation_bars) + len(test_bars),
            "train_bars": len(train_bars),
            "validation_bars": len(validation_bars),
            "test_bars": len(test_bars),
        },
        "test_first": test_bars[0].observed_at.isoformat(),
        "test_last": test_bars[-1].observed_at.isoformat(),
        "bars": [bar.to_dict() for bar in test_bars],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def load_test_bars_snapshot(instrument: str) -> tuple[list[HistoricalBar], dict[str, Any]]:
    """Load a tracked test split without opening the source database."""
    path = test_bars_snapshot_path(instrument)
    if not path.is_file():
        raise FileNotFoundError(
            f"missing tracked test-bar snapshot {path}; run "
            "evaluation/rl_strategy/run_rl_evaluation.py"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != TEST_BARS_SCHEMA:
        raise ValueError(f"unsupported test-bar snapshot schema in {path}")
    if payload.get("instrument") != instrument:
        raise ValueError(
            f"test-bar snapshot instrument mismatch: {path} says "
            f"{payload.get('instrument')!r}"
        )
    counts = payload.get("counts") or {}
    for key in ("bars", "train_bars", "validation_bars", "test_bars"):
        if not isinstance(counts.get(key), int):
            raise ValueError(f"test-bar snapshot {path} has no integer {key}")
    if counts["bars"] != (
        counts["train_bars"] + counts["validation_bars"] + counts["test_bars"]
    ):
        raise ValueError(f"test-bar snapshot {path} has inconsistent counts")
    bars = [
        HistoricalBar.from_mapping(row)
        for row in payload.get("bars") or []
        if isinstance(row, dict)
    ]
    if len(bars) != counts["test_bars"] or not bars:
        raise ValueError(f"test-bar snapshot {path} has an incomplete test split")
    if any(bar.instrument_id != instrument for bar in bars):
        raise ValueError(f"test-bar snapshot {path} contains another instrument")
    if any(
        prev.observed_at >= current.observed_at
        for prev, current in zip(bars, bars[1:], strict=False)
    ):
        raise ValueError(f"test-bar snapshot {path} is not strictly ordered")
    if any(bar.observed_at <= VAL_END or bar.observed_at > TEST_END for bar in bars):
        raise ValueError(f"test-bar snapshot {path} contains bars outside the test split")
    first = bars[0].observed_at.isoformat()
    last = bars[-1].observed_at.isoformat()
    if payload.get("test_first") != first or payload.get("test_last") != last:
        raise ValueError(f"test-bar snapshot {path} has inconsistent boundaries")
    return bars, {
        "path": path.relative_to(ROOT).as_posix(),
        **counts,
        "test_first": first,
        "test_last": last,
    }


def config_for(instrument: str, bars: list[HistoricalBar], label: str) -> RLEnvironmentConfig:
    return RLEnvironmentConfig(
        name=f"rl-eval-{instrument}-{label}",
        dataset_id="rl-eval-real-daily",
        dataset_version="rl-eval-real-daily-v1",
        instrument_id=instrument,
        start=bars[0].observed_at,
        end=bars[-1].observed_at,
        initial_capital=INITIAL,
        include_features=False,
        include_forecasts=False,
        max_steps=500,
        max_position_weight=Decimal("0.25"),
        max_order_notional=Decimal("25000.00"),
        max_gross_exposure=Decimal("1.00"),
        execution_config={},
        random_seed=SEED,
        notes="Read-only evaluation environment; no production authority.",
    )


def allowed_actions(agent: DQNAgent, state: list[float], env: RLEnvironment) -> list[int]:
    position = env.portfolio.position
    held = position is not None and position.quantity > 0
    values = agent.q_values(state)
    allowed: list[int] = []
    for index, name in enumerate(ACTION_NAMES):
        if name == "sell" and not held:
            continue
        if name == "buy" and (held or env.portfolio.exposure >= Decimal("0.249")):
            continue
        allowed.append(index)
    return sorted(allowed, key=lambda index: values[index], reverse=True)


def choose_action(
    agent: DQNAgent,
    state: list[float],
    env: RLEnvironment,
    *,
    epsilon: float,
    rng: random.Random,
) -> int:
    ranked = allowed_actions(agent, state, env)
    if epsilon > 0 and rng.random() < epsilon:
        return rng.choice(ranked)
    values = agent.q_values(state)
    hold_index = ACTION_NAMES.index("hold")
    # An untrained or indifferent Q-function must abstain, not churn because
    # action index 0 happens to sort first.  A trade is chosen only when its
    # estimated value is strictly better than holding.
    if values[ranked[0]] <= values[hold_index] + 1e-12:
        return hold_index
    return ranked[0]


def run_episode(
    env: RLEnvironment,
    agent: DQNAgent,
    reward_engine: RewardEngine,
    rcfg: RewardConfig,
    *,
    episode_id: str,
    train: bool,
    epsilon: float,
    rng: random.Random,
    policy: TrainedPolicy | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[Decimal], list[str]]:
    observation = env.reset(episode_id)
    observation_dict = observation.to_dict()
    state = agent.encoder.encode(observation_dict)
    history: list[dict[str, Any]] = []
    rewards: list[dict[str, Any]] = []
    views: list[TransitionView] = []
    losses: list[float] = []
    actions: list[str] = []
    q_values_seen: list[list[float]] = []
    q_spreads: list[float] = []
    while env.status.value == "running":
        q_values_seen.append(agent.q_values(state))
        q_spreads.append(max(q_values_seen[-1]) - min(q_values_seen[-1]))
        if policy is not None and not train:
            # Out-of-sample evaluation uses the user-facing masked-argmax path.
            # Training and validation keep choose_action so checkpoint
            # selection stays reproducible.
            decision = policy.select(
                observation=observation_dict,
                portfolio=env.portfolio.to_dict(),
                environment=env.config.to_dict(),
                history=history,
            )
            action = decision.action
            name = str(action.get("action_type") or "wait")
            index = (
                ACTION_NAMES.index(name)
                if name in ACTION_NAMES
                else ACTION_NAMES.index("wait")
            )
            actions.append(name)
        else:
            index = choose_action(agent, state, env, epsilon=epsilon, rng=rng)
            action = action_payload(index, observation_dict)
            actions.append(ACTION_NAMES[index])
        try:
            observation, _raw_reward, done, truncated, transition = env.step(action)
        except RLInvalidActionError:
            # A masked action should be impossible, but a refused order must
            # never be silently treated as a real trade.
            observation, _raw_reward, done, truncated, transition = env.step(
                {"action_type": "wait"}
            )
            actions[-1] = "wait"
        payload = transition.to_dict()
        reward = reward_engine.calculate(
            payload,
            config=rcfg,
            history=history,
            environment=env.config.to_dict(),
        )
        history.append(payload)
        rewards.append(reward.to_dict())
        views.append(TransitionView.from_transition(payload, environment=env.config.to_dict()))
        observation_dict = observation.to_dict()
        next_state = agent.encoder.encode(observation_dict)
        if train:
            agent.remember(state, index, float(reward.total), next_state, done or truncated)
            loss = agent.train_step(BATCH_SIZE)
            if loss is not None:
                losses.append(loss)
        state = next_state
        if done or truncated:
            break
    metrics, _trades = policy_metrics(
        initial_capital=INITIAL,
        transitions=views,
        rewards=rewards,
    )
    metrics["actions"] = actions
    metrics["inputs"] = {
        "encoder_dimension": agent.encoder.dimension,
        "encoder_version": agent.encoder.version,
        "feature_names": list(agent.encoder.feature_names),
    }
    metrics["loss_mean"] = round(statistics.fmean(losses), 10) if losses else None
    all_q_values = [value for values in q_values_seen for value in values]
    metrics["q_value_diagnostics"] = {
        "observations": len(q_values_seen),
        "min_q": min(all_q_values) if all_q_values else None,
        "max_q": max(all_q_values) if all_q_values else None,
        "max_spread": max(q_spreads) if q_spreads else None,
        "mean_spread": round(statistics.fmean(q_spreads), 12) if q_spreads else None,
        "all_equal_observations": sum(1 for spread in q_spreads if spread <= 1e-12),
        "zero_q_observations": sum(
            1
            for values in q_values_seen
            if all(abs(value) <= 1e-12 for value in values)
        ),
    }
    return metrics, rewards, [Decimal(str(row["total"])) for row in rewards if row.get("total")], actions


def _slippage_from_costs(total_costs: Any, fees_paid: Any) -> float | None:
    if total_costs is None or fees_paid is None:
        return None
    return float(Decimal(str(total_costs)) - Decimal(str(fees_paid)))


def compact_rl(metrics: dict[str, Any]) -> dict[str, Any]:
    financial = metrics.get("financial") or {}
    trading = metrics.get("trading") or {}
    risk = metrics.get("risk") or {}
    decision = metrics.get("decision") or {}
    learning = metrics.get("learning") or {}
    regimes = metrics.get("regimes") or {}
    return {
        "total_return": financial.get("total_return"),
        "max_drawdown": financial.get("max_drawdown"),
        "sharpe": financial.get("sharpe"),
        "sortino": financial.get("sortino"),
        "calmar": financial.get("calmar"),
        "final_value": financial.get("final_value"),
        "realized_pnl": financial.get("realized_pnl"),
        "unrealized_pnl": financial.get("unrealized_pnl"),
        "fees_paid": financial.get("fees_paid"),
        "total_slippage": _slippage_from_costs(
            trading.get("total_costs"), financial.get("fees_paid")
        ),
        "trades": trading.get("total_trades"),
        "win_rate": trading.get("win_rate"),
        "profit_factor": trading.get("profit_factor"),
        "average_holding_days": trading.get("average_holding_days"),
        "turnover": trading.get("turnover"),
        "total_costs": trading.get("total_costs"),
        "action_distribution": decision.get("action_distribution"),
        "invalid_actions": decision.get("invalid_actions"),
        "constraint_violations": decision.get("constraint_violations"),
        "average_reward": learning.get("average_reward"),
        "total_reward": learning.get("total_reward"),
        "reward_stability": learning.get("reward_stability"),
        "loss_mean": metrics.get("loss_mean"),
        "q_value_diagnostics": metrics.get("q_value_diagnostics"),
        "regimes": regimes,
        "actions": metrics.get("actions"),
        "inputs": metrics.get("inputs"),
        "risk": {
            "annualized_volatility": (risk.get("annualized_volatility") or {}).get("value"),
            "max_drawdown": (risk.get("max_drawdown") or {}).get("value"),
            "sharpe": (risk.get("sharpe") or {}).get("value"),
            "sortino": (risk.get("sortino") or {}).get("value"),
        },
    }


def validation_score(metrics: dict[str, Any]) -> float:
    financial = metrics.get("financial") or {}
    trading = metrics.get("trading") or {}
    total_return = financial.get("total_return")
    drawdown = financial.get("max_drawdown")
    trades = trading.get("total_trades")
    if total_return is None:
        return -999.0
    return float(total_return) - 0.5 * abs(float(drawdown or 0.0)) + 0.0005 * min(int(trades or 0), 10)


def train_policy(
    instrument: str,
    bars: list[HistoricalBar],
    train_bars: list[HistoricalBar],
    val_bars: list[HistoricalBar],
    rcfg: RewardConfig,
    *,
    reward_engine: RewardEngine | None = None,
) -> tuple[DQNAgent, dict[str, Any], dict[str, Any]]:
    cfg = config_for(instrument, train_bars, rcfg.name)
    env = RLEnvironment(cfg, train_bars)
    env.reset("fit")
    encoder = ObservationEncoder.fit(env.observation.to_dict())
    agent = DQNAgent(
        input_dim=encoder.dimension,
        hidden_layers=(24,),
        activation="relu",
        optimizer="adam",
        learning_rate=0.003,
        gamma=0.95,
        epsilon_start=1.0,
        epsilon_end=0.10,
        epsilon_decay=0.995,
        target_update_interval=20,
        replay_capacity=2000,
        seed=SEED,
        encoder=encoder,
    )
    engine = reward_engine or RewardEngine()
    rng = random.Random(SEED)
    best: dict[str, Any] | None = None
    best_score = -999.0
    history: list[dict[str, Any]] = []
    training_action_counts = {name: 0 for name in ACTION_NAMES}
    q_observations = 0
    q_min: float | None = None
    q_max: float | None = None
    q_spread_max: float | None = None
    q_spread_total = 0.0
    q_all_equal = 0
    q_zero = 0
    started = time.perf_counter()
    for episode_index in range(TRAIN_EPISODES):
        metrics, _rewards, _totals, _actions = run_episode(
            env,
            agent,
            engine,
            rcfg,
            episode_id=f"train-{instrument}-{episode_index}",
            train=True,
            epsilon=agent.epsilon_for_step(agent.steps),
            rng=rng,
        )
        for name in ACTION_NAMES:
            training_action_counts[name] += _actions.count(name)
        q_diagnostics = metrics.get("q_value_diagnostics") or {}
        q_observations += int(q_diagnostics.get("observations") or 0)
        if q_diagnostics.get("min_q") is not None:
            q_min = float(q_diagnostics["min_q"]) if q_min is None else min(q_min, float(q_diagnostics["min_q"]))
        if q_diagnostics.get("max_q") is not None:
            q_max = float(q_diagnostics["max_q"]) if q_max is None else max(q_max, float(q_diagnostics["max_q"]))
        if q_diagnostics.get("max_spread") is not None:
            q_spread_max = (
                float(q_diagnostics["max_spread"])
                if q_spread_max is None
                else max(q_spread_max, float(q_diagnostics["max_spread"]))
            )
        q_spread_total += float(q_diagnostics.get("mean_spread") or 0.0) * int(
            q_diagnostics.get("observations") or 0
        )
        q_all_equal += int(q_diagnostics.get("all_equal_observations") or 0)
        q_zero += int(q_diagnostics.get("zero_q_observations") or 0)
        history.append(
            {
                "episode": episode_index + 1,
                "train_return": (metrics.get("financial") or {}).get("total_return"),
                "train_trades": (metrics.get("trading") or {}).get("total_trades"),
                "average_reward": (metrics.get("learning") or {}).get("average_reward"),
                "loss": metrics.get("loss_mean"),
                "epsilon": round(agent.epsilon_for_step(agent.steps), 8),
                "action_distribution": {
                    name: _actions.count(name) for name in ACTION_NAMES
                },
                "q_value_diagnostics": q_diagnostics,
            }
        )
        if (episode_index + 1) % VALIDATE_EVERY == 0 or episode_index + 1 == TRAIN_EPISODES:
            vcfg = config_for(instrument, val_bars, f"{rcfg.name}-validation")
            validation_env = RLEnvironment(vcfg, val_bars)
            vmetrics, _vr, _vt, _va = run_episode(
                validation_env,
                agent,
                engine,
                rcfg,
                episode_id=f"validation-{instrument}-{episode_index}",
                train=False,
                epsilon=0.0,
                rng=random.Random(SEED + episode_index),
            )
            score = validation_score(vmetrics)
            history[-1]["validation_return"] = (vmetrics.get("financial") or {}).get("total_return")
            history[-1]["validation_score"] = round(score, 8)
            if score > best_score:
                best_score = score
                best = copy.deepcopy(agent.save_state())
    elapsed = time.perf_counter() - started
    if best is None:
        raise RuntimeError("no validation checkpoint was produced")
    best_agent = DQNAgent.from_state(best)
    meta = {
        "instrument": instrument,
        "reward_config": rcfg.name,
        "reward_config_hash": rcfg.config_hash,
        "train_bars": len(train_bars),
        "validation_bars": len(val_bars),
        "episodes": TRAIN_EPISODES,
        "best_validation_score": round(best_score, 8),
        "elapsed_seconds": round(elapsed, 4),
        "peak_traced_bytes": None,
        "history": history,
        "training_action_distribution": training_action_counts,
        "training_q_value_diagnostics": {
            "observations": q_observations,
            "min_q": q_min,
            "max_q": q_max,
            "max_spread": q_spread_max,
            "mean_spread": (
                round(q_spread_total / q_observations, 12) if q_observations else None
            ),
            "all_equal_observations": q_all_equal,
            "zero_q_observations": q_zero,
        },
        "seed": SEED,
        "algorithm": "dqn",
        "algorithm_version": "pi15-dqn-pure-python-1",
    }
    return best_agent, meta, encoder.to_dict()


def compact_backtest(result: Any) -> dict[str, Any]:
    metrics = result.metrics
    benchmark = result.benchmark
    robustness = result.robustness or {}
    return {
        "total_return": metrics.get("total_return"),
        "max_drawdown": metrics.get("max_drawdown"),
        "sharpe": metrics.get("sharpe"),
        "sortino": metrics.get("sortino"),
        "calmar": metrics.get("calmar"),
        "final_value": metrics.get("final_value"),
        "trades": metrics.get("trades"),
        "win_rate": metrics.get("win_rate"),
        "profit_factor": metrics.get("profit_factor"),
        "average_holding_days": metrics.get("average_holding_days"),
        "turnover": metrics.get("turnover"),
        "total_fees": metrics.get("total_fees"),
        "total_slippage": metrics.get("total_slippage"),
        "wins": metrics.get("wins"),
        "losses": metrics.get("losses"),
        "regimes": robustness.get("regimes"),
        "benchmark": benchmark,
        "warnings": list(result.warnings),
    }


def baseline_run(
    instrument: str,
    strategy: str,
    strategy_config: dict[str, Any],
    test_bars: list[HistoricalBar],
    store: BacktestStore,
) -> dict[str, Any]:
    cfg = BacktestConfig(
        name=f"rl-eval-{instrument}-{strategy}",
        strategy_catalog_id=strategy,
        strategy_version="nm09-evaluation-v1",
        strategy_config=strategy_config,
        instruments=(instrument,),
        start=test_bars[0].observed_at,
        end=test_bars[-1].observed_at,
        initial_capital=INITIAL,
        benchmark=instrument,
        execution_config=ExecutionConfig(),
        order_notional=Decimal("20000.00"),
        max_position_weight=Decimal("0.25"),
        max_gross_exposure=Decimal("1.00"),
        order_type="market",
        horizon=strategy_config.get("horizon_days", 5) and "short_term",
        random_seed=SEED,
    )
    engine = BacktestEngine(store)
    result = engine.run(cfg, test_bars, force=True)
    return compact_backtest(result)


def bootstrap_mean(values: list[float], *, seed: int = 11, draws: int = 4000) -> dict[str, Any]:
    usable = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not usable:
        return {"n": 0, "mean": None, "ci95": None}
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        sample = [rng.choice(usable) for _ in usable]
        means.append(statistics.fmean(sample))
    means.sort()
    return {
        "n": len(usable),
        "mean": round(statistics.fmean(usable), 8),
        "median": round(statistics.median(usable), 8),
        "ci95": [round(means[int(draws * 0.025)], 8), round(means[int(draws * 0.975) - 1], 8)],
        "min": round(min(usable), 8),
        "max": round(max(usable), 8),
    }


def aggregate(rows: list[dict[str, Any]], key: str = "total_return") -> dict[str, Any]:
    values = [row.get(key) for row in rows]
    usable = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not usable:
        return {"n": 0}
    return {
        "n": len(usable),
        "mean": round(statistics.fmean(usable), 8),
        "median": round(statistics.median(usable), 8),
        "stdev": round(statistics.pstdev(usable), 8) if len(usable) > 1 else 0.0,
        "min": round(min(usable), 8),
        "max": round(max(usable), 8),
        "bootstrap": bootstrap_mean(usable),
    }


def policy_parameter_count(agent: DQNAgent) -> int:
    return sum(len(row) for layer in agent.online.weights for row in layer) + sum(
        len(layer) for layer in agent.online.biases
    )


def _compact_policy(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("error"):
        return {"error": row["error"]}
    return {
        key: row.get(key)
        for key in (
            "trades",
            "total_return",
            "total_reward",
            "average_reward",
            "final_value",
            "fees_paid",
            "parameters",
            "artifact_bytes",
            "action_distribution",
            "invalid_actions",
            "constraint_violations",
            "q_value_diagnostics",
        )
    }


def _compact_baseline(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("error"):
        return {"error": row["error"]}
    benchmark = row.get("benchmark") or {}
    compact = {
        key: row.get(key)
        for key in (
            "trades",
            "total_return",
            "final_value",
            "max_drawdown",
            "sharpe",
            "total_fees",
            "total_slippage",
        )
    }
    compact["benchmark_total_return"] = benchmark.get("total_return")
    compact["benchmark_excess_return"] = benchmark.get("excess_return")
    return compact


def _policy_total_slippage(row: dict[str, Any]) -> float | None:
    value = row.get("total_slippage")
    if value is not None:
        return float(value)
    return _slippage_from_costs(row.get("total_costs"), row.get("fees_paid"))


def _compact_aggregate(report: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    instruments = (report.get("instruments") or {}).values()
    for label, row in (report.get("aggregate") or {}).items():
        if not isinstance(row, dict):
            continue
        returns = row.get("returns") or {}
        sharpes = row.get("sharpes") or {}
        drawdowns = row.get("drawdowns") or {}
        if label in ("rl_default", "rl_trading"):
            slippages = [
                _policy_total_slippage(instrument_row[label])
                for instrument_row in instruments
                if isinstance(instrument_row, dict)
                and isinstance(instrument_row.get(label), dict)
            ]
            total_slippage = round(
                sum(value for value in slippages if value is not None), 6
            )
        else:
            total_slippage = row.get("total_slippage")
        compact[label] = {
            "return_mean": returns.get("mean"),
            "return_min": returns.get("min"),
            "return_max": returns.get("max"),
            "sharpe_mean": sharpes.get("mean"),
            "drawdown_mean": drawdowns.get("mean"),
            "trades": row.get("trades"),
            "total_fees": row.get("total_fees"),
            "total_slippage": total_slippage,
        }
    return compact


def _compact_registry(registry: Any) -> Any:
    if not isinstance(registry, dict):
        return registry
    policies = [
        {
            key: policy.get(key)
            for key in (
                "policy_id",
                "status",
                "algorithm",
                "production_authority",
                "activation_available",
            )
        }
        for policy in registry.get("policies") or []
        if isinstance(policy, dict)
    ]
    evaluations = [
        {
            key: evaluation.get(key)
            for key in (
                "split",
                "policy_name",
                "action_distribution",
                "transitions",
                "total_return",
                "baselines",
            )
        }
        for evaluation in registry.get("evaluations") or []
        if isinstance(evaluation, dict)
    ]
    test_evaluations = [
        evaluation for evaluation in evaluations if evaluation.get("split") == "test"
    ]
    zero_trade = sum(
        1
        for evaluation in test_evaluations
        if int((evaluation.get("action_distribution") or {}).get("buy") or 0)
        + int((evaluation.get("action_distribution") or {}).get("sell") or 0)
        == 0
    )
    return {
        "counts": registry.get("counts"),
        "policies": policies,
        "tested_policies": sum(
            1 for policy in policies if policy.get("status") == "tested"
        ),
        "test_evaluations": len(test_evaluations),
        "test_evaluations_with_zero_trades": zero_trade,
        "evaluations": evaluations,
    }


def _compact_verdict(report: dict[str, Any]) -> dict[str, Any]:
    instruments = (report.get("instruments") or {}).values()
    policy_rows: list[dict[str, Any]] = []
    for instrument_row in instruments:
        if not isinstance(instrument_row, dict):
            continue
        for label in ("rl_default", "rl_trading"):
            payload = instrument_row.get(label)
            if isinstance(payload, dict):
                policy_rows.append(payload)
    with_trades = sum(
        1 for row in policy_rows if int(row.get("trades") or 0) > 0
    )

    def label_trades(label: str) -> int:
        aggregate_row = (report.get("aggregate") or {}).get(label) or {}
        return int(aggregate_row.get("trades") or 0)

    def label_reward(label: str) -> float:
        total = 0.0
        for instrument_row in (report.get("instruments") or {}).values():
            if not isinstance(instrument_row, dict):
                continue
            payload = instrument_row.get(label)
            if isinstance(payload, dict):
                total += float(payload.get("total_reward") or 0.0)
        return round(total, 4)

    return {
        "summary": (
            "Through the actual TrainedPolicy masked-argmax selector, "
            f"{with_trades} of the {len(policy_rows)} fresh default/trading "
            "policies make an out-of-sample trade. Simulation evidence only; "
            "no policy is promoted."
        ),
        "fresh_policies": len(policy_rows),
        "fresh_policies_with_zero_trades": len(policy_rows) - with_trades,
        "fresh_policies_with_nonzero_trades": with_trades,
        "rl_default_aggregate_trades": label_trades("rl_default"),
        "rl_trading_aggregate_trades": label_trades("rl_trading"),
        "rl_default_total_reward": label_reward("rl_default"),
        "rl_trading_total_reward": label_reward("rl_trading"),
        "selection": SELECTION,
    }


def compact_report(report: dict[str, Any]) -> dict[str, Any]:
    """Build the tracked compact extract from this harness's full report.

    The full report carries training histories and per-step actions that are
    too large to track.  This writer keeps the evidence needed to reproduce
    the out-of-sample result: the split, execution assumptions, per-policy
    test metrics, baselines, aggregates and the policy-selection verdict.
    It is deliberately written by the harness itself so the compact extract
    does not depend on a separate post-processing script.
    """

    instruments: dict[str, Any] = {}
    for instrument, row in (report.get("instruments") or {}).items():
        if not isinstance(row, dict):
            continue
        compact = {
            key: row.get(key)
            for key in (
                "bars",
                "train_bars",
                "validation_bars",
                "test_bars",
                "test_first",
                "test_last",
            )
        }
        for label in ("rl_default", "rl_trading"):
            payload = row.get(label)
            if isinstance(payload, dict):
                compact[label] = _compact_policy(payload)
        compact["baselines"] = {
            label: _compact_baseline(base)
            for label, base in (row.get("baselines") or {}).items()
            if isinstance(base, dict)
        }
        instruments[instrument] = compact

    return {
        "artifact": "compact tracked extract",
        "source_harness": "evaluation/rl_strategy/run_rl_evaluation.py",
        "reproduction_command": (
            "PYTHONPATH=src ../.venv/Scripts/python.exe "
            "evaluation/rl_strategy/run_rl_evaluation.py"
        ),
        "reproduction_exit_code": 0,
        "verified_at": report.get("generated_at"),
        "generated_at": report.get("generated_at"),
        "full_runtime_output": "tmp/rl_evaluation/results.json",
        "full_runtime_output_tracked": False,
        "simulation_only": bool(report.get("simulation_only", True)),
        "production_authority": bool(report.get("production_authority", False)),
        "automatic_policy_promotion": bool(
            report.get("automatic_policy_promotion", False)
        ),
        "source_mode": report.get("source_mode", "read-only"),
        "source_database": "data/novamarket.sqlite3",
        "initial_capital": report.get("initial_capital"),
        "split": report.get("split"),
        "execution_assumptions": report.get("execution_assumptions"),
        "verdict": _compact_verdict(report),
        "instruments": instruments,
        "aggregate": _compact_aggregate(report),
        "paired_differences": report.get("paired_differences"),
        "existing_registry": _compact_registry(report.get("existing_registry")),
        "errors": report.get("errors") or [],
        "hardware": report.get("hardware"),
        "resources": report.get("resources"),
        "reward_configs": report.get("reward_configs"),
        "isolation": {
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
        "out_of_sample_reevaluation": {
            "evaluated_at": report.get("generated_at"),
            "script": "evaluation/rl_strategy/run_rl_evaluation.py",
            "runner": "evaluation/rl_strategy/run_rl_evaluation.py",
            "source_artifacts": [
                "evaluation/rl_strategy/artifacts/policies/*.json",
                TEST_BARS_SOURCE,
            ],
            "selection": SELECTION,
            "note": (
                "Each policy is evaluated on the chronological test split "
                "through TrainedPolicy.select inside this harness run. This "
                "compact is written by the same run; no separate patcher or "
                "post-processing step is required. "
                "evaluate_trained_adapters.py is the independent "
                "artifact-level check and reads the tracked test-bar snapshot."
            ),
        },
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    for path in (BACKTEST_DB, BACKTEST_DB.with_suffix(".sqlite3-shm"), BACKTEST_DB.with_suffix(".sqlite3-wal")):
        if path.exists():
            path.unlink()
    store = BacktestStore(BACKTEST_DB)
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "simulation_only": True,
        "production_authority": False,
        "automatic_policy_promotion": False,
        "source_database": str(DB),
        "source_mode": "read-only",
        "initial_capital": str(INITIAL),
        "split": {
            "train_start": START.isoformat(),
            "train_end_exclusive": TRAIN_END.isoformat(),
            "validation_start": TRAIN_END.isoformat(),
            "validation_end_inclusive": VAL_END.isoformat(),
            "test_start_exclusive": VAL_END.isoformat(),
            "test_end_inclusive": TEST_END.isoformat(),
            "chronological": True,
            "test_used_for_training_or_selection": False,
        },
        "execution_assumptions": {
            "execution_config": ExecutionConfig().to_dict(),
            "order_notional": "20000.00",
            "max_position_weight": "0.25",
            "max_gross_exposure": "1.00",
            "fills": "next completed daily bar open + NM-10 fee/slippage functions",
        },
        "reward_configs": {
            "default": DEFAULT_REWARD.to_dict(),
            "trading": TRADING_REWARD.to_dict(),
        },
        "instruments": {},
        "baselines": {},
        "errors": [],
        "hardware": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "processor": platform.processor(),
            "logical_cpus": __import__("os").cpu_count(),
        },
    }
    all_rows: dict[str, list[dict[str, Any]]] = {}
    for instrument in INSTRUMENTS:
        try:
            bars = load_bars(instrument)
            train_bars, val_bars, test_bars = split(bars)
            if min(len(train_bars), len(val_bars), len(test_bars)) < 25:
                raise RuntimeError(
                    f"insufficient bars: train={len(train_bars)} val={len(val_bars)} test={len(test_bars)}"
                )
            print(instrument, "train/val/test", len(train_bars), len(val_bars), len(test_bars), flush=True)
            instrument_row: dict[str, Any] = {
                "bars": len(bars),
                "train_bars": len(train_bars),
                "validation_bars": len(val_bars),
                "test_bars": len(test_bars),
                "train_first": train_bars[0].observed_at.isoformat(),
                "train_last": train_bars[-1].observed_at.isoformat(),
                "validation_first": val_bars[0].observed_at.isoformat(),
                "validation_last": val_bars[-1].observed_at.isoformat(),
                "test_first": test_bars[0].observed_at.isoformat(),
                "test_last": test_bars[-1].observed_at.isoformat(),
            }
            for reward_name, rcfg in (("default", DEFAULT_REWARD), ("trading", TRADING_REWARD)):
                agent, meta, encoder = train_policy(instrument, bars, train_bars, val_bars, rcfg)
                test_cfg = config_for(instrument, test_bars, f"{reward_name}-test")
                test_env = RLEnvironment(test_cfg, test_bars)
                test_metrics, _tr, _tt, _ta = run_episode(
                    test_env,
                    agent,
                    RewardEngine(),
                    rcfg,
                    episode_id=f"test-{instrument}-{reward_name}",
                    train=False,
                    epsilon=0.0,
                    rng=random.Random(SEED),
                    policy=TrainedPolicy(
                        agent, name=f"evaluation-{instrument}-{reward_name}"
                    ),
                )
                compact = compact_rl(test_metrics)
                compact["selection"] = SELECTION
                compact["training"] = meta
                compact["parameters"] = policy_parameter_count(agent)
                compact["encoder"] = encoder
                artifact = {
                    "policy_version": "rl-eval-v1",
                    "algorithm": "dqn",
                    "instrument": instrument,
                    "reward_config": reward_name,
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
                artifact_path = ARTIFACT_DIR / f"{instrument.replace(':','_')}_{reward_name}.json"
                artifact_path.write_text(json.dumps(artifact, sort_keys=True), encoding="utf-8")
                compact["artifact_path"] = artifact_path.relative_to(ROOT).as_posix()
                compact["artifact_bytes"] = artifact_path.stat().st_size
                instrument_row[f"rl_{reward_name}"] = compact
                all_rows.setdefault(f"rl_{reward_name}", []).append(compact)
            snapshot_path = write_test_bars_snapshot(
                instrument, train_bars, val_bars, test_bars
            )
            print(
                "test-bars",
                snapshot_path.relative_to(ROOT).as_posix(),
                flush=True,
            )
            strategies = {
                "day_momentum": (
                    "momentum_breakout",
                    {"lookback": 5, "volume_factor": 1.1, "exit_period": 3, "horizon_days": 1, "max_data_age_hours": 72},
                ),
                "swing_trend": (
                    "trend_sma",
                    {"fast_period": 10, "slow_period": 30, "horizon_days": 5, "max_data_age_hours": 72},
                ),
                "long_horizon_proxy": (
                    "trend_sma",
                    {"fast_period": 20, "slow_period": 60, "horizon_days": 20, "max_data_age_hours": 72},
                ),
            }
            instrument_row["baselines"] = {}
            for label, (strategy, config) in strategies.items():
                try:
                    compact = baseline_run(instrument, strategy, config, test_bars, store)
                    compact["strategy"] = strategy
                    compact["strategy_config"] = config
                    instrument_row["baselines"][label] = compact
                    all_rows.setdefault(label, []).append(compact)
                except Exception as exc:  # noqa: BLE001 - report the gap, do not hide it
                    instrument_row["baselines"][label] = {
                        "error": f"{type(exc).__name__}: {exc}"
                    }
                    report["errors"].append(f"{instrument} {label}: {type(exc).__name__}: {exc}")
            report["instruments"][instrument] = instrument_row
        except Exception as exc:  # noqa: BLE001 - one instrument must not erase evidence
            report["errors"].append(f"{instrument}: {type(exc).__name__}: {exc}")
            print("ERROR", instrument, exc, flush=True)
    store.close()
    report["aggregate"] = {}
    for label, rows in all_rows.items():
        report["aggregate"][label] = {
            "returns": aggregate(rows, "total_return"),
            "drawdowns": aggregate(rows, "max_drawdown"),
            "sharpes": aggregate(rows, "sharpe"),
            "trades": sum(int(row.get("trades") or 0) for row in rows),
            "turnover": aggregate(rows, "turnover"),
            "total_fees": sum(float(row.get("total_fees") or row.get("fees_paid") or 0) for row in rows),
            "total_slippage": sum(float(row.get("total_slippage") or 0) for row in rows),
        }
    # Paired RL-minus-baseline differences on the same five instruments.
    report["paired_differences"] = {}
    for label in ("rl_default", "rl_trading"):
        for baseline in ("day_momentum", "swing_trend", "long_horizon_proxy"):
            diffs = []
            for instrument in INSTRUMENTS:
                row = report["instruments"].get(instrument, {})
                rl = row.get(label) or {}
                base = (row.get("baselines") or {}).get(baseline) or {}
                if rl.get("total_return") is not None and base.get("total_return") is not None:
                    diffs.append(float(rl["total_return"]) - float(base["total_return"]))
            report["paired_differences"][f"{label}_minus_{baseline}"] = bootstrap_mean(diffs)
    report["existing_registry"] = existing_registry_summary()
    report["resources"] = {
        "training": {
            instrument: {
                label: report["instruments"][instrument][label]["training"]["elapsed_seconds"]
                for label in ("rl_default", "rl_trading")
                if label in report["instruments"][instrument]
            }
            for instrument in report["instruments"]
        },
        "note": "CPU-only pure-Python DQN; no GPU or NPU path was used.",
    }
    (OUT / "results.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    policy_rows = [
        row[label]
        for row in (report.get("instruments") or {}).values()
        if isinstance(row, dict)
        for label in ("rl_default", "rl_trading")
        if isinstance(row.get(label), dict)
    ]
    if not policy_rows or not all(
        "TrainedPolicy" in str(row.get("selection")) for row in policy_rows
    ):
        raise RuntimeError(
            "refusing to overwrite compact evidence: no TrainedPolicy-rescored "
            "out-of-sample rows were produced"
        )
    COMPACT_PATH.write_text(
        json.dumps(compact_report(report), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["aggregate"], indent=2, sort_keys=True), flush=True)
    print("wrote", OUT / "results.json", flush=True)
    print("wrote", COMPACT_PATH, flush=True)


def existing_registry_summary() -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{ROOT / 'data' / 'novamarket_rl.sqlite3'}?mode=ro", uri=True)
    try:
        counts = {}
        for table in ("rl_train_runs", "rl_train_policies", "rl_train_evaluations", "rl_train_configs"):
            counts[table] = connection.execute(f'select count(*) from "{table}"').fetchone()[0]
        policies = []
        for payload, status in connection.execute("select payload,status from rl_train_policies order by created_at"):
            row = json.loads(payload)
            policies.append(
                {
                    "policy_id": row.get("policy_id"),
                    "status": status,
                    "algorithm": row.get("algorithm"),
                    "run_id": row.get("run_id"),
                    "test_metrics": row.get("test_metrics"),
                    "validation_metrics": row.get("validation_metrics"),
                    "production_authority": row.get("production_authority"),
                    "activation_available": row.get("activation_available"),
                }
            )
        evaluations = []
        for split, payload in connection.execute("select split,payload from rl_train_evaluations order by created_at"):
            row = json.loads(payload)
            policy = row.get("policy") or {}
            evaluations.append(
                {
                    "split": split,
                    "policy_name": policy.get("policy_name"),
                    "action_distribution": policy.get("action_distribution"),
                    "transitions": policy.get("transitions"),
                    "total_return": (row.get("financial") or {}).get("total_return"),
                    "baselines": [
                        {
                            "name": base.get("policy_name"),
                            "action_distribution": base.get("action_distribution"),
                            "total_return": (base.get("metrics") or {}).get("financial", {}).get("total_return"),
                        }
                        for base in row.get("baselines") or []
                    ],
                }
            )
        return {"counts": counts, "policies": policies, "evaluations": evaluations}
    finally:
        connection.close()


if __name__ == "__main__":
    main()
