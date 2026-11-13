"""NM-11 reproducible backtesting exports."""

from novacontrol.novamarket.backtesting.core import (
    SOFTWARE_VERSION,
    BacktestConfig,
    BacktestEngine,
    BacktestError,
    BacktestResult,
    BacktestStore,
    HistoricalBar,
    RiskDecision,
    SignalRecord,
    SimulatedOrder,
    Trade,
    benchmark_metrics,
    calculate_metrics,
    compare,
    normalise_bars,
    regime_analysis,
)
from novacontrol.novamarket.backtesting.service import BacktestingService
from novacontrol.novamarket.risk import RiskPolicy

__all__ = [
    "SOFTWARE_VERSION",
    "BacktestConfig",
    "BacktestEngine",
    "BacktestError",
    "BacktestResult",
    "BacktestStore",
    "BacktestingService",
    "HistoricalBar",
    "RiskDecision",
    "RiskPolicy",
    "SignalRecord",
    "SimulatedOrder",
    "Trade",
    "benchmark_metrics",
    "calculate_metrics",
    "compare",
    "normalise_bars",
    "regime_analysis",
]
