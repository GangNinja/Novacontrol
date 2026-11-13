"""NM-11 service boundary: configuration, historical loading and run lifecycle."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from novacontrol.novamarket.backtesting.core import (
    SOFTWARE_VERSION,
    BacktestConfig,
    BacktestEngine,
    BacktestError,
    BacktestResult,
    BacktestStore,
    HistoricalBar,
    compare,
)
from novacontrol.novamarket.execution.config import ExecutionConfig

Publisher = Callable[[str, dict[str, Any]], Awaitable[None]]
BarLoader = Callable[[str, datetime, datetime], Sequence[Mapping[str, Any]]]


class BacktestingService:
    """Bounded, persistent backtesting without touching live or paper books."""

    def __init__(
        self,
        path: str | Path,
        *,
        market_data: Any | None = None,
        strategy_loader: Callable[[str], Mapping[str, Any]] | None = None,
        risk_policy_loader: Callable[[str], Mapping[str, Any]] | None = None,
        event_loader: Callable[[str, datetime], Sequence[Mapping[str, Any]]] | None = None,
        forecast_loader: Callable[[str, datetime], Sequence[Mapping[str, Any]]] | None = None,
        publisher: Publisher | None = None,
        clock: Callable[[], datetime] | None = None,
        max_concurrency: int = 2,
    ) -> None:
        self._market_data = market_data
        self._strategy_loader = strategy_loader
        self._risk_policy_loader = risk_policy_loader
        self._event_loader = event_loader
        self._forecast_loader = forecast_loader
        self._publisher = publisher
        self._clock = clock or (lambda: datetime.now(UTC))
        self._store = BacktestStore(path)
        self._engine = BacktestEngine(
            self._store,
            clock=self._clock,
            event_loader=event_loader,
            forecast_loader=forecast_loader,
            max_concurrency=max_concurrency,
        )
        self._lock = threading.RLock()
        self._running: set[str] = set()
        self._last_error = ""

    @property
    def store(self) -> BacktestStore:
        return self._store

    @property
    def engine(self) -> BacktestEngine:
        return self._engine

    async def _publish(self, name: str, payload: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        try:
            await self._publisher(name, payload)
        except Exception as exc:  # noqa: BLE001 - committed state stands
            self._last_error = f"{name}: {type(exc).__name__}: {exc}"

    def _resolved(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(payload)
        data.pop("load_data", None)
        strategy_id = str(data.get("strategy_id") or "")
        if strategy_id and not data.get("strategy_catalog_id"):
            if self._strategy_loader is None:
                raise BacktestError("strategy resolution is unavailable")
            record = dict(self._strategy_loader(strategy_id))
            strategy = dict(record.get("strategy") or record)
            data.setdefault("strategy_catalog_id", strategy.get("catalog_id"))
            data.setdefault("strategy_version", strategy.get("version"))
            data.setdefault(
                "strategy_config", dict(strategy.get("config") or {})
            )
            if not data.get("horizon"):
                horizons = list(strategy.get("horizons") or ())
                data["horizon"] = horizons[0] if horizons else "short_term"
            data.setdefault("name", strategy.get("name") or "backtest")
        data.setdefault("name", "backtest")
        data.setdefault("evaluation_frequency", "1d")
        data.setdefault("instruments", ())
        if not data.get("risk_policy") and self._risk_policy_loader is not None:
            try:
                policy = dict(
                    self._risk_policy_loader(str(data.get("risk_policy_version") or ""))
                )
                data["risk_policy"] = {
                    key: policy[key]
                    for key in (
                        "max_position_weight",
                        "max_gross_exposure",
                        "min_cash_fraction",
                        "max_drawdown",
                        "restricted_instruments",
                    )
                    if key in policy
                }
                if policy.get("version"):
                    data["risk_policy_version"] = str(policy["version"])
            except Exception as exc:  # noqa: BLE001 - defaults are explicit
                self._last_error = f"risk policy snapshot unavailable: {exc}"
        if isinstance(data.get("execution_config"), Mapping):
            data["execution_config"] = ExecutionConfig.from_mapping(
                data["execution_config"]
            )
        return data

    def config(self, payload: Mapping[str, Any]) -> BacktestConfig:
        return BacktestConfig.from_mapping(self._resolved(payload))

    def validate(
        self, payload: Mapping[str, Any], *, load_data: bool = False
    ) -> dict[str, Any]:
        config = self.config(payload)
        body = {
            "ok": True,
            "config": config.to_dict(),
            "config_hash": config.config_hash,
            "validated": True,
            "data_checked": False,
        }
        if load_data:
            bars = self.load_bars(config)
            clean, warnings = self._engine.validate(config, bars)
            body.update(
                {
                    "data_checked": True,
                    "bars": len(
                        [bar for bar in clean if bar.instrument_id in config.instruments]
                    ),
                    "instruments": list(config.instruments),
                    "warnings": warnings,
                }
            )
        return body

    async def create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        config = self.config(payload)
        existing = self._store.find_by_config(config.config_hash)
        if existing is not None:
            return {
                "ok": True,
                "backtest_id": existing,
                "status": self._store.status(existing) or "created",
                "config": config.to_dict(),
                "config_hash": config.config_hash,
                "simulated": True,
                "replayed": True,
            }
        backtest_id = self._store.create(config, self._clock())
        await self._publish(
            "novamarket.backtest.created",
            {
                "backtest_id": backtest_id,
                "strategy_id": config.strategy_id,
                "dataset_id": config.dataset_id,
            },
        )
        return {
            "ok": True,
            "backtest_id": backtest_id,
            "status": "created",
            "config": config.to_dict(),
            "config_hash": config.config_hash,
            "simulated": True,
        }

    def load_bars(self, config: BacktestConfig) -> list[HistoricalBar]:
        if self._market_data is None:
            raise BacktestError("NM-02 market data is unavailable")
        rows: list[HistoricalBar] = []
        targets = list(config.instruments)
        if config.benchmark and config.benchmark not in targets:
            targets.append(config.benchmark)
        for instrument in targets:
            rows.extend(self._load_instrument(instrument, config))
        return rows

    def _load_instrument(
        self, instrument: str, config: BacktestConfig
    ) -> list[HistoricalBar]:
        market_data = self._market_data
        if market_data is None:
            raise BacktestError("NM-02 market data is unavailable")
        page = market_data.bars(
            instrument,
            interval=config.evaluation_frequency,
            start=config.start,
            end=config.end,
            limit=1000,
            offset=0,
            ascending=True,
        )
        items = list(getattr(page, "items", ()))
        total = int(getattr(page, "total", len(items)) or len(items))
        if not items:
            return []
        if total > 100_000:
            raise BacktestError(
                f"historical dataset for {instrument} exceeds the 100,000-bar limit"
            )
        offset = len(items)
        while offset < total:
            next_page = market_data.bars(
                instrument,
                interval=config.evaluation_frequency,
                start=config.start,
                end=config.end,
                limit=1000,
                offset=offset,
                ascending=True,
            )
            next_items = list(getattr(next_page, "items", ()))
            if not next_items:
                break
            items.extend(next_items)
            offset += len(next_items)
        result: list[HistoricalBar] = []
        for bar in items:
            quality = str(getattr(getattr(bar, "quality", None), "value", "")).lower()
            if bool(getattr(bar, "partial", False)) or quality in {"invalid", "suspect"}:
                continue
            observed = getattr(bar, "observed_at", None)
            if observed is None:
                continue
            volume = getattr(bar, "volume", None)
            result.append(
                HistoricalBar(
                    instrument_id=instrument,
                    observed_at=observed,
                    available_at=observed,
                    retrieved_at=getattr(bar, "retrieved_at", None),
                    open=getattr(bar, "open"),
                    high=getattr(bar, "high"),
                    low=getattr(bar, "low"),
                    close=getattr(bar, "close"),
                    volume=Decimal(str(volume)) if volume is not None else Decimal("0"),
                    source=str(getattr(bar, "provider", "nm02") or "nm02"),
                    adjusted=bool(
                        getattr(bar, "provider_metadata", {}).get("adjusted", False)
                    ),
                )
            )
        return result

    async def start(self, backtest_id: str) -> dict[str, Any]:
        status = self._store.status(backtest_id)
        if status is None:
            raise BacktestError(f"unknown backtest {backtest_id!r}")
        if status == "completed":
            existing = self._store.result(backtest_id)
            if existing is not None:
                return {"ok": True, "replayed": True, **existing}
        if status == "cancelled":
            raise BacktestError(f"backtest {backtest_id} was cancelled")
        config = self._store.config(backtest_id)
        if config is None:
            raise BacktestError(f"unknown backtest {backtest_id!r}")
        with self._lock:
            if backtest_id in self._running:
                raise BacktestError(f"backtest {backtest_id} is already running")
            self._running.add(backtest_id)
        try:
            await self._publish(
                "novamarket.backtest.started",
                {
                    "backtest_id": backtest_id,
                    "strategy_id": config.strategy_id,
                    "start": config.start.isoformat(),
                    "end": config.end.isoformat(),
                },
            )
            bars = await asyncio.to_thread(self.load_bars, config)
            await self._publish(
                "novamarket.backtest.progress.updated",
                {
                    "backtest_id": backtest_id,
                    "phase": "replay",
                    "bars_loaded": len(bars),
                },
            )
            result = await asyncio.to_thread(self._engine.execute, backtest_id, bars)
            await self._publish(
                "novamarket.backtest.replay.checkpoint.created",
                {
                    "backtest_id": backtest_id,
                    "at": (
                        result.equity[-1]["at"]
                        if result.equity
                        else result.created_at.isoformat()
                    ),
                    "observations": len(result.equity),
                },
            )
            await self._publish(
                "novamarket.backtest.performance.calculated",
                {
                    "backtest_id": backtest_id,
                    "observations": result.metrics.get("observations", 0),
                    "total_return": result.metrics.get("total_return"),
                },
            )
            if result.warnings:
                await self._publish(
                    "novamarket.backtest.data_quality.warning",
                    {
                        "backtest_id": backtest_id,
                        "warnings": len(result.warnings),
                    },
                )
            payload = result.to_dict()
            await self._publish(
                "novamarket.backtest.completed",
                {
                    "backtest_id": backtest_id,
                    "status": result.status,
                    "trades": len(result.trades),
                    "repro_hash": result.repro_hash,
                },
            )
            return {"ok": True, **payload}
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            await self._publish(
                "novamarket.backtest.failed",
                {
                    "backtest_id": backtest_id,
                    "error": self._last_error,
                },
            )
            raise
        finally:
            with self._lock:
                self._running.discard(backtest_id)

    async def cancel(self, backtest_id: str) -> dict[str, Any]:
        body = self._engine.cancel(backtest_id)
        await self._publish(
            "novamarket.backtest.cancelled",
            {"backtest_id": backtest_id},
        )
        return {"ok": True, "backtest": body}

    def _completed(self, backtest_id: str) -> BacktestResult:
        result = self._store.result_model(backtest_id)
        if result is not None:
            return result
        status = self._store.status(backtest_id)
        if status is None:
            raise BacktestError(f"unknown backtest {backtest_id!r}")
        raise BacktestError(
            f"backtest {backtest_id!r} has not completed (status={status})"
        )

    def get(self, backtest_id: str, *, include_bars: bool = False) -> dict[str, Any]:
        result = self._store.result_model(backtest_id)
        if result is not None:
            return result.to_dict(include_bars=include_bars)
        record = self._store.record(backtest_id)
        config = self._store.config(backtest_id)
        if record is None or config is None:
            raise BacktestError(f"unknown backtest {backtest_id!r}")
        return {
            "backtest_id": backtest_id,
            "status": record["status"],
            "config": config.to_dict(),
            "config_hash": config.config_hash,
            "created_at": record["created_at"],
            "error": record.get("error", ""),
            "metrics": {},
            "trades": [],
            "orders": [],
            "risk_decisions": [],
            "signals": [],
            "warnings": [],
            "data_quality": {},
            "benchmark": {},
            "robustness": {},
            "repro_hash": "",
            "completed": False,
            "simulated": True,
        }

    def results(self, backtest_id: str) -> dict[str, Any]:
        result = self._completed(backtest_id)
        return {
            "ok": True,
            "backtest_id": backtest_id,
            "config": result.config.to_dict(),
            "metrics": result.metrics,
            "benchmark": result.benchmark,
            "robustness": result.robustness,
            "warnings": result.warnings,
            "data_quality": result.data_quality,
            "repro_hash": result.repro_hash,
            "simulated": True,
        }

    def metrics(self, backtest_id: str) -> dict[str, Any]:
        result = self._completed(backtest_id)
        return {"ok": True, "backtest_id": backtest_id, **result.metrics}

    def trades(self, backtest_id: str) -> dict[str, Any]:
        result = self._completed(backtest_id)
        return {
            "ok": True,
            "backtest_id": backtest_id,
            "trades": [trade.to_dict() for trade in result.trades],
        }

    def orders(self, backtest_id: str) -> dict[str, Any]:
        result = self._completed(backtest_id)
        return {
            "ok": True,
            "backtest_id": backtest_id,
            "orders": [order.to_dict() for order in result.orders],
        }

    def decisions(self, backtest_id: str) -> dict[str, Any]:
        result = self._completed(backtest_id)
        return {
            "ok": True,
            "backtest_id": backtest_id,
            "risk_decisions": [row.to_dict() for row in result.risk_decisions],
        }

    def signals(self, backtest_id: str) -> dict[str, Any]:
        result = self._completed(backtest_id)
        return {
            "ok": True,
            "backtest_id": backtest_id,
            "signals": [row.to_dict() for row in result.signals],
        }

    def data_quality(self, backtest_id: str) -> dict[str, Any]:
        result = self._completed(backtest_id)
        return {"ok": True, "backtest_id": backtest_id, **result.data_quality}

    def list(self, limit: int = 50) -> dict[str, Any]:
        rows = self._store.list(limit)
        return {
            "ok": True,
            "backtests": [
                {
                    "backtest_id": row["backtest_id"],
                    "status": row["status"],
                    "name": row.get("config", {}).get("name"),
                    "strategy_catalog_id": row.get("config", {}).get("strategy_catalog_id"),
                    "created_at": row.get("created_at"),
                    "total_return": row.get("metrics", {}).get("total_return"),
                    "max_drawdown": row.get("metrics", {}).get("max_drawdown"),
                    "repro_hash": row.get("repro_hash"),
                }
                for row in rows
            ],
            "total": len(rows),
        }

    def compare(self, backtest_ids: Sequence[str]) -> dict[str, Any]:
        results: list[BacktestResult] = [
            self._completed(backtest_id) for backtest_id in backtest_ids
        ]
        body = compare(results)
        comparison_id = self._store.save_comparison("comparison", body, self._clock())
        return {**body, "comparison_id": comparison_id, "persisted": True}

    def comparisons(self, limit: int = 50) -> dict[str, Any]:
        rows = self._store.comparisons(limit)
        return {"ok": True, "comparisons": rows, "total": len(rows)}

    async def walk_forward(
        self, base_id: str, train_bars: object, test_bars: object
    ) -> dict[str, Any]:
        base = self._store.config(base_id)
        if base is None:
            raise BacktestError(f"unknown backtest {base_id!r}")
        try:
            train = int(str(train_bars))
            test = int(str(test_bars))
        except (TypeError, ValueError) as exc:
            raise BacktestError("train_bars and test_bars must be integers") from exc
        bars = await asyncio.to_thread(self.load_bars, base)
        body = await asyncio.to_thread(
            self._engine.walk_forward,
            base,
            bars,
            train_bars=train,
            test_bars=test,
        )
        comparison_id = self._store.save_comparison(
            "walk_forward", body, self._clock()
        )
        await self._publish(
            "novamarket.backtest.comparison.completed",
            {"base_backtest_id": base_id, "variants": body.get("count", 0)},
        )
        return {**body, "comparison_id": comparison_id, "persisted": True}

    async def verify(self, backtest_id: str) -> dict[str, Any]:
        body = await asyncio.to_thread(self._engine.verify, backtest_id)
        await self._publish(
            "novamarket.backtest.reproducibility.verified",
            {
                "backtest_id": backtest_id,
                "ok": bool(body.get("ok")),
            },
        )
        return body

    async def sensitivity(
        self, base_id: str, variants: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        base = self._store.config(base_id)
        if base is None:
            raise BacktestError(f"unknown backtest {base_id!r}")
        configs = [self.config(row) for row in variants]
        bars = await asyncio.to_thread(self.load_bars, base)
        body = await asyncio.to_thread(self._engine.sensitivity, base, configs, bars)
        comparison_id = self._store.save_comparison(
            "sensitivity", body, self._clock()
        )
        await self._publish(
            "novamarket.backtest.comparison.completed",
            {"base_backtest_id": base_id, "variants": len(configs)},
        )
        return {**body, "comparison_id": comparison_id, "persisted": True}

    def status(self) -> dict[str, Any]:
        rows = self._store.list(500)
        return {
            "ok": True,
            "phase": "NM-11",
            "initialised": True,
            "total": len(rows),
            "completed": sum(1 for row in rows if row.get("status") == "completed"),
            "failed": sum(1 for row in rows if row.get("status") == "failed"),
            "running": len(self._running),
            "history_persisted": True,
            "isolated": True,
            "real_money": False,
            "last_error": self._last_error,
        }

    def contract(self) -> dict[str, Any]:
        return {
            "ok": True,
            "implemented": True,
            "phase": "NM-11",
            "version": SOFTWARE_VERSION,
            "frequencies": ["1d"],
            "order_types": [str(item) for item in ("market", "limit", "stop", "stop_limit")],
            "guarantees": [
                "decisions use bars available at the decision timestamp",
                "orders execute from the next bar onward, never the signal close",
                "fees, slippage and participation are applied through NM-10 pricing",
                "production wallets and paper-execution books are never mutated",
                "completed runs are immutable and persisted with their bars",
                "full replay verification recomputes the reproducibility hash",
            ],
            "limitations": [
                "historical universe membership and delistings are not reconstructed",
                "corporate actions require caller-adjusted bars",
                "intraday bars, exchange calendars and halts require explicit tradable flags",
                "regime labels are descriptive, not predictive",
                "parameter sensitivity is not an out-of-sample optimisation",
            ],
            "simulated": True,
            "real_money": False,
        }

    def close(self) -> None:
        self._store.close()
