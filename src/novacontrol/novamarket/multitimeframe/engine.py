"""Deterministic 20-minute / 5-minute / 1-minute trading intelligence.

The engine reads *completed* observations from the existing market-data store,
aggregates 5-minute bars into 20-minute context buckets where needed, and
uses the accepted NM-09 indicator functions.  It does not write market data,
create signals, call the risk engine or execute anything.

Failure policy is fail-closed: a missing, stale, partial or gapped layer can
only produce WAIT/HOLD/REJECT, never a candidate.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable

from novacontrol.novamarket.marketdata.models import DataInterval
from novacontrol.novamarket.strategies.indicators import (
    atr,
    average_volume,
    ema,
    highest,
    lowest,
    rsi,
    sma,
)

from .models import (
    ConflictStatus,
    MarketCondition,
    MultiTimeframeConfig,
    MultiTimeframeDecision,
    SetupState,
    TimeframeDirection,
    TimeframeLabel,
    TimeframeLayer,
    TimeframeOutcome,
    TimingState,
    Uncertainty,
    decision_id,
)

_ENGINE_VERSION = "mtf-engine-1.0"
_SESSION_BREAK_SECONDS = 4 * 3600


@dataclass(frozen=True, slots=True)
class _Bar:
    observed_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    bar_id: str


@dataclass(frozen=True, slots=True)
class _Series:
    bars: tuple[_Bar, ...]
    excluded_partial: int
    duplicates: int
    invalid: int

    @property
    def closes(self) -> list[float]:
        return [bar.close for bar in self.bars]

    @property
    def highs(self) -> list[float]:
        return [bar.high for bar in self.bars]

    @property
    def lows(self) -> list[float]:
        return [bar.low for bar in self.bars]

    @property
    def volumes(self) -> list[float]:
        return [bar.volume or 0.0 for bar in self.bars]

    @property
    def last_at(self) -> datetime | None:
        return self.bars[-1].observed_at if self.bars else None


class MultiTimeframeAnalyzer:
    """Compose the three timeframe assessments into one decision."""

    def __init__(
        self,
        market_data: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        config: MultiTimeframeConfig | None = None,
    ) -> None:
        self._market_data = market_data
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._config = config or MultiTimeframeConfig()
        self._cache: dict[tuple[str, str, str], tuple[float, _Series]] = {}

    @property
    def config(self) -> MultiTimeframeConfig:
        return self._config

    def set_config(self, values: Mapping[str, Any] | MultiTimeframeConfig) -> None:
        self._config = (
            values
            if isinstance(values, MultiTimeframeConfig)
            else self._config.with_overrides(values)
        )
        self.clear_cache()

    def clear_cache(self) -> None:
        self._cache.clear()

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "phase": "post-PI-24 enhancement",
            "engine_version": _ENGINE_VERSION,
            "contract_version": "mtf-hierarchy-1.0",
            "timeframes": {
                "context": TimeframeLabel.CONTEXT.value,
                "setup": TimeframeLabel.SETUP.value,
                "timing": TimeframeLabel.TIMING.value,
            },
            "config": self._config.to_dict(),
            "authority": {
                "risk": "NM-08",
                "execution": "NM-10",
                "executes": False,
                "approves": False,
            },
            "applies_to": ["short_term / day_trading"],
            "preserves": ["medium_term / swing_trading", "long_term / long_term"],
        }

    def analyse(
        self, instrument_id: str, *, now: datetime | None = None
    ) -> MultiTimeframeDecision:
        moment = self._now(now)
        context_bars = self._series(
            instrument_id, self._config.context_interval, moment
        )
        setup_bars = self._series(instrument_id, "5m", moment)
        timing_bars = self._series(instrument_id, "1m", moment)

        context = self._context(context_bars, moment)
        setup = self._setup(setup_bars, moment, context.direction)
        timing = self._timing(timing_bars, moment)
        outcome, conflict, direction, uncertainty, reasons = self._resolve(
            context, setup, timing
        )
        confidence = self._confidence(context, setup, timing, outcome)
        return MultiTimeframeDecision(
            decision_id=decision_id(instrument_id, moment, context, setup, timing),
            instrument_id=instrument_id,
            generated_at=moment,
            context=context,
            setup=setup,
            timing=timing,
            outcome=outcome,
            conflict_status=conflict,
            direction=direction,
            confidence=confidence,
            uncertainty=uncertainty,
            missing=tuple(
                layer.label.value
                for layer in (context, setup, timing)
                if not layer.available
            ),
            reasons=reasons,
        )

    # -- data access ----------------------------------------------------------
    def _now(self, value: datetime | None) -> datetime:
        moment = value or self._clock()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("the multi-timeframe clock must be timezone-aware")
        return moment.astimezone(UTC)

    def _series(
        self, instrument_id: str, interval: str, moment: datetime
    ) -> _Series:
        canonical = DataInterval(interval)
        key = (instrument_id, canonical.value, moment.replace(microsecond=0).isoformat())
        cached = self._cache.get(key)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        page = self._market_data.bars(
            instrument_id,
            interval=canonical,
            limit=400,
            ascending=False,
        )
        raw = list(getattr(page, "items", ()) or ())[::-1]
        # A decision may be evaluated at a historical/replay instant.  Rows
        # observed after that instant are unknown at decision time and must
        # never enter the hierarchy, even though they are already stored.
        raw = [
            bar
            for bar in raw
            if isinstance(getattr(bar, "observed_at", None), datetime)
            and bar.observed_at.tzinfo is not None
            and bar.observed_at.astimezone(UTC) <= moment
        ]
        series = self._normalise(raw)
        self._cache[key] = (
            time.monotonic() + self._config.cache_ttl_seconds,
            series,
        )
        return series

    @staticmethod
    def _normalise(raw: Sequence[Any]) -> _Series:
        excluded = duplicates = invalid = 0
        rows: list[tuple[datetime, Any]] = []
        for bar in raw:
            observed = getattr(bar, "observed_at", None)
            if not isinstance(observed, datetime) or observed.tzinfo is None:
                invalid += 1
                continue
            if bool(getattr(bar, "partial", False)):
                excluded += 1
                continue
            # A stored bar marked "stale" is still a completed historical
            # observation; freshness is enforced against the supplied decision
            # time below.  Only structurally invalid rows are dropped here.
            quality = str(getattr(bar, "quality", "") or "").lower()
            if quality in {"invalid", "suspect"}:
                excluded += 1
                continue
            try:
                close = float(getattr(bar, "close"))
                high = float(getattr(bar, "high"))
                low = float(getattr(bar, "low"))
            except (TypeError, ValueError):
                invalid += 1
                continue
            if close <= 0 or high < low:
                invalid += 1
                continue
            rows.append((observed.astimezone(UTC), bar))

        rows.sort(key=lambda item: item[0])
        deduped: list[tuple[datetime, Any]] = []
        for observed, bar in rows:
            if deduped and deduped[-1][0] == observed:
                duplicates += 1
                deduped[-1] = (observed, bar)
                continue
            deduped.append((observed, bar))
        bars: list[_Bar] = []
        for observed, bar in deduped:
            try:
                close = float(getattr(bar, "close"))
                high = float(getattr(bar, "high"))
                low = float(getattr(bar, "low"))
                open_value = float(getattr(bar, "open", close))
            except (TypeError, ValueError):
                invalid += 1
                continue
            volume_raw = getattr(bar, "volume", None)
            try:
                volume = None if volume_raw is None else float(volume_raw)
            except (TypeError, ValueError):
                volume = None
            bars.append(
                _Bar(
                    observed_at=observed,
                    open=open_value,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    bar_id=str(getattr(bar, "bar_id", "") or ""),
                )
            )
        return _Series(tuple(bars), excluded, duplicates, invalid)

    # -- 20-minute context ----------------------------------------------------
    def _context(self, series: _Series, now: datetime) -> TimeframeLayer:
        config = self._config
        width = config.context_bucket_minutes * 60
        step = 60 if config.context_interval == "1m" else 300
        buckets, incomplete = _aggregate(series.bars, width, step, now)
        reasons: list[str] = []
        if series.excluded_partial:
            reasons.append(f"{series.excluded_partial} partial/invalid bar(s) excluded")
        if series.duplicates:
            reasons.append(f"{series.duplicates} duplicate timestamp(s) collapsed")
        if series.invalid:
            reasons.append(f"{series.invalid} unusable bar(s) ignored")
        if incomplete:
            reasons.append(f"{incomplete} incomplete {config.context_bucket_minutes}m bucket(s) excluded")
        if len(buckets) < config.context_min_buckets:
            reasons.append(
                f"needs {config.context_min_buckets} completed "
                f"{config.context_bucket_minutes}m buckets, has {len(buckets)}"
            )
            return TimeframeLayer(
                TimeframeLabel.CONTEXT,
                config.context_interval,
                TimeframeDirection.NEUTRAL,
                False,
                False,
                len(buckets),
                Decimal("0"),
                buckets[-1].observed_at if buckets else None,
                tuple(reasons),
                condition=MarketCondition.SIDEWAYS,
                metrics={"incomplete_buckets": incomplete},
            )
        missing_gap = _intraday_gap(buckets, config.context_max_gap_seconds)
        if missing_gap:
            reasons.append("missing 20m bucket(s) inside the current session")
            return TimeframeLayer(
                TimeframeLabel.CONTEXT,
                config.context_interval,
                TimeframeDirection.NEUTRAL,
                False,
                False,
                len(buckets),
                Decimal("0"),
                buckets[-1].observed_at,
                tuple(reasons),
                metrics={"max_gap_seconds": missing_gap},
            )
        closes = [bar.close for bar in buckets]
        highs = [bar.high for bar in buckets]
        lows = [bar.low for bar in buckets]
        fast = sma(closes, 5)
        slow = sma(closes, 10)
        slow_f = float(slow) if slow is not None else 0.0
        atr_value = atr(highs, lows, closes, 5)
        ret = closes[-1] / closes[-4] - 1.0 if len(closes) >= 4 and closes[-4] else 0.0
        direction = TimeframeDirection.NEUTRAL
        if fast is not None and slow is not None:
            last, fast_f, slow_f = closes[-1], float(fast), float(slow)
            if last > fast_f > slow_f and ret > config.trend_band_pct:
                direction = TimeframeDirection.BULLISH
            elif last < fast_f < slow_f and ret < -config.trend_band_pct:
                direction = TimeframeDirection.BEARISH
        atr_pct = float(atr_value) / closes[-1] if atr_value is not None and closes[-1] else 0.0
        if atr_pct >= config.high_volatility_atr_pct:
            condition = MarketCondition.HIGH_VOLATILITY
        elif direction is not TimeframeDirection.NEUTRAL:
            condition = MarketCondition.TRENDING
        else:
            condition = MarketCondition.SIDEWAYS
        last_at = buckets[-1].observed_at
        stale = (now - last_at).total_seconds() > config.max_context_age_seconds
        if stale:
            reasons.append(
                f"latest 20m bucket is older than {config.max_context_age_seconds}s"
            )
        confidence = Decimal("0.50")
        if fast is not None and slow is not None and float(slow):
            separation = abs(float(fast) - float(slow)) / float(slow)
            confidence += Decimal(str(min(0.30, separation * 8)))
        confidence += Decimal(str(min(0.15, len(buckets) * 0.012)))
        if direction is TimeframeDirection.NEUTRAL:
            confidence = min(confidence, Decimal("0.55"))
        if stale:
            confidence = min(confidence, Decimal("0.35"))
        return TimeframeLayer(
            TimeframeLabel.CONTEXT,
            config.context_interval,
            direction,
            True,
            stale,
            len(buckets),
            confidence,
            last_at,
            tuple(reasons),
            condition=condition,
            metrics={
                "close": closes[-1],
                "sma_5": str(fast) if fast is not None else None,
                "sma_10": str(slow) if slow is not None else None,
                "return_3_buckets": round(ret, 6),
                "atr_pct": round(atr_pct, 6),
                "incomplete_buckets": incomplete,
            },
        )

    # -- 5-minute setup -------------------------------------------------------
    def _setup(
        self,
        series: _Series,
        now: datetime,
        context_direction: TimeframeDirection,
    ) -> TimeframeLayer:
        config = self._config
        reasons: list[str] = []
        bars = series.bars
        if series.excluded_partial:
            reasons.append(f"{series.excluded_partial} partial/invalid 5m bar(s) excluded")
        if series.duplicates:
            reasons.append(f"{series.duplicates} duplicate 5m timestamp(s) collapsed")
        if len(bars) < config.setup_min_bars:
            reasons.append(
                f"needs {config.setup_min_bars} five-minute bars, has {len(bars)}"
            )
            return TimeframeLayer(
                TimeframeLabel.SETUP,
                "5m",
                TimeframeDirection.NEUTRAL,
                False,
                False,
                len(bars),
                Decimal("0"),
                series.last_at,
                tuple(reasons),
                setup_state=SetupState.ABSENT,
            )
        gap = _intraday_gap(bars, config.setup_max_gap_seconds)
        if gap:
            reasons.append("missing five-minute candle(s) inside the current session")
            return TimeframeLayer(
                TimeframeLabel.SETUP,
                "5m",
                TimeframeDirection.NEUTRAL,
                False,
                False,
                len(bars),
                Decimal("0"),
                series.last_at,
                tuple(reasons),
                setup_state=SetupState.ABSENT,
                metrics={"max_gap_seconds": gap},
            )
        closes, highs, lows = series.closes, series.highs, series.lows
        fast = ema(closes, 9)
        slow = ema(closes, 21)
        rsi_value = rsi(closes, 14)
        prior_high = highest(highs[:-1], 20)
        prior_low = lowest(lows[:-1], 20)
        momentum = TimeframeDirection.NEUTRAL
        if fast is not None and slow is not None and closes[-1]:
            if closes[-1] > float(fast) > float(slow):
                momentum = TimeframeDirection.BULLISH
            elif closes[-1] < float(fast) < float(slow):
                momentum = TimeframeDirection.BEARISH
        breakout = TimeframeDirection.NEUTRAL
        if prior_high is not None and closes[-1] > float(prior_high):
            breakout = TimeframeDirection.BULLISH
        elif prior_low is not None and closes[-1] < float(prior_low):
            breakout = TimeframeDirection.BEARISH
        reversal = TimeframeDirection.NEUTRAL
        if rsi_value is not None:
            if float(rsi_value) <= config.setup_rsi_low:
                reversal = TimeframeDirection.BULLISH
            elif float(rsi_value) >= config.setup_rsi_high:
                reversal = TimeframeDirection.BEARISH
        candidate = (
            momentum
            if momentum is not TimeframeDirection.NEUTRAL
            else breakout
            if breakout is not TimeframeDirection.NEUTRAL
            else reversal
        )
        if candidate is TimeframeDirection.NEUTRAL:
            state = SetupState.ABSENT
            reasons.append("no momentum, breakout or reversal setup")
        elif context_direction is TimeframeDirection.NEUTRAL:
            state = SetupState.WEAK
            reasons.append("market context is neutral; the setup lacks a directional backdrop")
        elif candidate is not context_direction:
            state = SetupState.CONFLICTING
            reasons.append(
                f"{candidate.value} setup conflicts with the {context_direction.value} 20m context"
            )
        elif momentum is candidate or breakout is candidate:
            state = SetupState.CONFIRMED
        else:
            state = SetupState.WEAK
            reasons.append("only a mean-reversion reading supports the setup")
        last_at = bars[-1].observed_at
        stale = (now - last_at).total_seconds() > config.max_setup_age_seconds
        if stale:
            reasons.append(f"latest 5m bar is older than {config.max_setup_age_seconds}s")
        confidence = Decimal("0.45")
        if state is SetupState.CONFIRMED:
            confidence += Decimal("0.20")
            if breakout is candidate:
                confidence += Decimal("0.10")
        if momentum is candidate and breakout is candidate:
            confidence += Decimal("0.05")
        if stale:
            confidence = min(confidence, Decimal("0.35"))
        return TimeframeLayer(
            TimeframeLabel.SETUP,
            "5m",
            candidate,
            True,
            stale,
            len(bars),
            confidence,
            last_at,
            tuple(reasons),
            setup_state=state,
            metrics={
                "close": closes[-1],
                "ema_9": str(fast) if fast is not None else None,
                "ema_21": str(slow) if slow is not None else None,
                "rsi_14": str(rsi_value) if rsi_value is not None else None,
                "momentum": momentum.value,
                "breakout": breakout.value,
                "reversal": reversal.value,
                "average_volume": str(average_volume(series.volumes, 20) or ""),
            },
        )

    # -- 1-minute timing ------------------------------------------------------
    def _timing(self, series: _Series, now: datetime) -> TimeframeLayer:
        config = self._config
        reasons: list[str] = []
        bars = series.bars
        if series.excluded_partial:
            reasons.append(f"{series.excluded_partial} partial/invalid 1m bar(s) excluded")
        if series.duplicates:
            reasons.append(f"{series.duplicates} duplicate 1m timestamp(s) collapsed")
        if len(bars) < config.timing_min_bars:
            reasons.append(
                f"needs {config.timing_min_bars} one-minute bars, has {len(bars)}"
            )
            return TimeframeLayer(
                TimeframeLabel.TIMING,
                "1m",
                TimeframeDirection.NEUTRAL,
                False,
                False,
                len(bars),
                Decimal("0"),
                series.last_at,
                tuple(reasons),
                timing_state=TimingState.INVALID,
            )
        gap = _intraday_gap(bars, config.timing_max_gap_seconds)
        if gap:
            reasons.append("missing one-minute candle(s) inside the current session")
            return TimeframeLayer(
                TimeframeLabel.TIMING,
                "1m",
                TimeframeDirection.NEUTRAL,
                False,
                False,
                len(bars),
                Decimal("0"),
                series.last_at,
                tuple(reasons),
                timing_state=TimingState.INVALID,
                metrics={"max_gap_seconds": gap},
            )
        closes = series.closes
        fast = ema(closes, 5)
        slow = ema(closes, 13)
        fast_previous = ema(closes[:-1], 5)
        slow_previous = ema(closes[:-1], 13)
        last_return = closes[-1] / closes[-4] - 1.0 if len(closes) >= 4 and closes[-4] else 0.0
        last_at = bars[-1].observed_at
        stale = (now - last_at).total_seconds() > config.max_timing_age_seconds
        if stale:
            reasons.append(f"latest 1m bar is older than {config.max_timing_age_seconds}s")
        direction = TimeframeDirection.NEUTRAL
        if fast is not None and slow is not None:
            if float(fast) > float(slow):
                direction = TimeframeDirection.BULLISH
            elif float(fast) < float(slow):
                direction = TimeframeDirection.BEARISH
        cross_down = (
            fast is not None
            and slow is not None
            and fast_previous is not None
            and slow_previous is not None
            and float(fast_previous) >= float(slow_previous)
            and float(fast) < float(slow)
        )
        cross_up = (
            fast is not None
            and slow is not None
            and fast_previous is not None
            and slow_previous is not None
            and float(fast_previous) <= float(slow_previous)
            and float(fast) > float(slow)
        )
        if stale:
            state = TimingState.WAIT
        elif abs(last_return) > config.max_timing_return_pct:
            state = TimingState.WAIT
            reasons.append("sudden one-minute move; timing refuses to chase it")
        elif cross_down:
            state = TimingState.EXIT_LONG
        elif cross_up:
            state = TimingState.EXIT_SHORT
        elif direction is TimeframeDirection.BULLISH and last_return > 0:
            state = TimingState.ENTRY_LONG
        elif direction is TimeframeDirection.BEARISH and last_return < 0:
            state = TimingState.ENTRY_SHORT
        else:
            state = TimingState.WAIT
            reasons.append("one-minute momentum and structure are not aligned")
        confidence = Decimal("0.45")
        if state in {TimingState.ENTRY_LONG, TimingState.ENTRY_SHORT}:
            confidence += Decimal("0.25")
            if fast is not None and slow is not None and float(slow):
                separation = abs(float(fast) - float(slow)) / float(slow)
                confidence += Decimal(str(min(0.20, separation * 30)))
        if state in {TimingState.EXIT_LONG, TimingState.EXIT_SHORT}:
            confidence += Decimal("0.15")
        if stale:
            confidence = min(confidence, Decimal("0.30"))
        return TimeframeLayer(
            TimeframeLabel.TIMING,
            "1m",
            direction,
            True,
            stale,
            len(bars),
            confidence,
            last_at,
            tuple(reasons),
            timing_state=state,
            metrics={
                "close": closes[-1],
                "ema_5": str(fast) if fast is not None else None,
                "ema_13": str(slow) if slow is not None else None,
                "return_3_bars": round(last_return, 6),
                "cross_up": cross_up,
                "cross_down": cross_down,
            },
        )

    # -- conflict resolution --------------------------------------------------
    def _resolve(
        self,
        context: TimeframeLayer,
        setup: TimeframeLayer,
        timing: TimeframeLayer,
    ) -> tuple[
        TimeframeOutcome,
        ConflictStatus,
        TimeframeDirection,
        Uncertainty,
        tuple[str, ...],
    ]:
        config = self._config
        reasons = list(context.reasons) + list(setup.reasons) + list(timing.reasons)
        missing = [
            layer.label.value
            for layer in (context, setup, timing)
            if not layer.available
        ]
        if missing:
            reasons.append(
                "missing or unusable timeframe(s): " + ", ".join(missing)
            )
            return (
                TimeframeOutcome.WAIT_FOR_CONFIRMATION,
                ConflictStatus.INSUFFICIENT_DATA,
                TimeframeDirection.NEUTRAL,
                Uncertainty.HIGH,
                tuple(_dedupe(reasons)),
            )
        if context.stale or setup.stale or timing.stale:
            reasons.append("a timeframe is stale; no new entry is timed")
            return (
                TimeframeOutcome.WAIT_FOR_CONFIRMATION,
                ConflictStatus.INSUFFICIENT_DATA,
                TimeframeDirection.NEUTRAL,
                Uncertainty.HIGH,
                tuple(_dedupe(reasons)),
            )
        if (
            config.block_on_high_volatility
            and context.condition is MarketCondition.HIGH_VOLATILITY
        ):
            reasons.append("20m volatility is too high for a timed entry")
            return (
                TimeframeOutcome.WAIT_FOR_CONFIRMATION,
                ConflictStatus.PARTIAL,
                context.direction,
                Uncertainty.MEDIUM,
                tuple(_dedupe(reasons)),
            )
        context_direction = context.direction
        timing_state = timing.timing_state
        if context_direction is TimeframeDirection.NEUTRAL:
            if (
                config.allow_neutral_context
                and setup.setup_state is SetupState.CONFIRMED
                and timing_state is not None
                and timing_state.entry_side
            ):
                candidate = (
                    TimeframeOutcome.BUY_CANDIDATE
                    if timing_state is TimingState.ENTRY_LONG
                    else TimeframeOutcome.SELL_CANDIDATE
                )
                return (
                    candidate,
                    ConflictStatus.PARTIAL,
                    setup.direction,
                    Uncertainty.MEDIUM,
                    tuple(_dedupe(reasons + ["entry allowed without a directional 20m context"])),
                )
            reasons.append("20m context is neutral; there is no directional trade to time")
            return (
                TimeframeOutcome.HOLD,
                ConflictStatus.PARTIAL,
                TimeframeDirection.NEUTRAL,
                Uncertainty.MEDIUM,
                tuple(_dedupe(reasons)),
            )
        if (
            setup.setup_state is SetupState.CONFLICTING
            or setup.direction is not context_direction
        ):
            reasons.append("the 5m setup opposes the 20m context")
            return (
                TimeframeOutcome.REJECT_SIGNAL,
                ConflictStatus.CONFLICT,
                context_direction,
                Uncertainty.MEDIUM,
                tuple(_dedupe(reasons)),
            )
        if setup.setup_state is not SetupState.CONFIRMED:
            reasons.append("the 5m setup is not confirmed by the 20m context")
            return (
                TimeframeOutcome.WAIT_FOR_CONFIRMATION,
                ConflictStatus.PARTIAL,
                context_direction,
                Uncertainty.MEDIUM,
                tuple(_dedupe(reasons)),
            )
        if timing.timing_state in {TimingState.EXIT_LONG, TimingState.EXIT_SHORT}:
            reasons.append("one-minute timing is signalling an exit, not an entry")
            return (
                TimeframeOutcome.WAIT_FOR_CONFIRMATION,
                ConflictStatus.PARTIAL,
                context_direction,
                Uncertainty.MEDIUM,
                tuple(_dedupe(reasons)),
            )
        if timing.direction is not context_direction:
            reasons.append("one-minute timing has not confirmed the setup direction")
            return (
                TimeframeOutcome.WAIT_FOR_CONFIRMATION,
                ConflictStatus.PARTIAL,
                context_direction,
                Uncertainty.MEDIUM,
                tuple(_dedupe(reasons)),
            )
        wanted = (
            TimingState.ENTRY_LONG
            if context_direction is TimeframeDirection.BULLISH
            else TimingState.ENTRY_SHORT
        )
        if timing.timing_state is not wanted:
            reasons.append("one-minute timing is waiting for a clean entry trigger")
            return (
                TimeframeOutcome.WAIT_FOR_CONFIRMATION,
                ConflictStatus.PARTIAL,
                context_direction,
                Uncertainty.MEDIUM,
                tuple(_dedupe(reasons)),
            )
        outcome = (
            TimeframeOutcome.BUY_CANDIDATE
            if context_direction is TimeframeDirection.BULLISH
            else TimeframeOutcome.SELL_CANDIDATE
        )
        confidence = self._confidence(context, setup, timing, outcome)
        if float(confidence) < config.minimum_confidence:
            reasons.append(
                f"aligned confidence {float(confidence):.3f} is below the "
                f"{config.minimum_confidence:.2f} floor"
            )
            return (
                TimeframeOutcome.WAIT_FOR_CONFIRMATION,
                ConflictStatus.PARTIAL,
                context_direction,
                Uncertainty.MEDIUM,
                tuple(_dedupe(reasons)),
            )
        return (
            outcome,
            ConflictStatus.ALIGNED,
            context_direction,
            Uncertainty.LOW,
            tuple(_dedupe(reasons + ["all three timeframes agree"])),
        )

    def _confidence(
        self,
        context: TimeframeLayer,
        setup: TimeframeLayer,
        timing: TimeframeLayer,
        outcome: TimeframeOutcome,
    ) -> Decimal:
        if not (context.available and setup.available and timing.available):
            return Decimal("0")
        confidence = min(context.confidence, setup.confidence, timing.confidence)
        if outcome in {TimeframeOutcome.BUY_CANDIDATE, TimeframeOutcome.SELL_CANDIDATE}:
            confidence = min(Decimal("0.95"), confidence + Decimal("0.05"))
        elif outcome in {
            TimeframeOutcome.WAIT_FOR_CONFIRMATION,
            TimeframeOutcome.REJECT_SIGNAL,
        }:
            confidence = min(confidence, Decimal("0.50"))
        return max(Decimal("0"), min(Decimal("1"), confidence))


def _aggregate(
    bars: Sequence[_Bar], width: int, step: int, now: datetime
) -> tuple[list[_Bar], int]:
    """Aggregate completed finer bars into fixed-width buckets.

    A bucket is used only when it contains exactly the expected number of
    contiguous bars and has fully closed.  The current forming bucket is
    dropped rather than treated as a completed observation.
    """

    expected = width // step
    groups: dict[int, list[_Bar]] = {}
    for bar in bars:
        key = int(bar.observed_at.timestamp()) // width
        groups.setdefault(key, []).append(bar)
    aggregated: list[_Bar] = []
    incomplete = 0
    for key in sorted(groups):
        rows = sorted(groups[key], key=lambda item: item.observed_at)
        start = datetime.fromtimestamp(key * width, tz=UTC)
        end = start + timedelta(seconds=width)
        contiguous = all(
            int((rows[index].observed_at - rows[index - 1].observed_at).total_seconds())
            == step
            for index in range(1, len(rows))
        )
        if len(rows) != expected or not contiguous or end > now:
            incomplete += 1
            continue
        volumes = [row.volume for row in rows]
        aggregated.append(
            _Bar(
                # The bucket's observation time is its CLOSE, not its start:
                # freshness and gap checks must not age a completed 20m window
                # by another 20 minutes.
                observed_at=end,
                open=rows[0].open,
                high=max(row.high for row in rows),
                low=min(row.low for row in rows),
                close=rows[-1].close,
                volume=(
                    sum(item for item in volumes if item is not None)
                    if all(item is not None for item in volumes)
                    else None
                ),
                bar_id=f"agg_{key * width}",
            )
        )
    return aggregated, incomplete


def _intraday_gap(bars: Sequence[_Bar], maximum: int) -> int:
    """The largest *intraday* gap beyond the limit, or 0.

    Overnight/session breaks are ignored: this probe looks for missing candles
    inside a trading session, not for the legitimate close-to-open gap.
    """

    largest = 0
    for previous, current in zip(bars, bars[1:]):
        gap = int((current.observed_at - previous.observed_at).total_seconds())
        if gap <= maximum or gap >= _SESSION_BREAK_SECONDS:
            continue
        largest = max(largest, gap)
    return largest


def _dedupe(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))



__all__ = ["MultiTimeframeAnalyzer"]
