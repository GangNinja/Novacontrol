"""Focused PI-10 swing/long-term forecasting verification.

The market, model and prediction rows are deterministic fixtures.  They verify
orchestration, target semantics, point-in-time filtering, context validation and
horizon-separated evaluation; they are not a claim of live-market accuracy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from novacontrol.api.models import ApiSurface
from novacontrol.novamarket.marketdata.calendar import calendar_by_mic
from novacontrol.novamarket.features.models import FEATURE_SET_VERSION
from novacontrol.novamarket.marketdata.models import (
    AssetClass,
    DataInterval,
    Freshness,
    Instrument,
    OhlcvBar,
)
from novacontrol.novamarket.outlook import (
    HorizonForecastService,
    OutlookValidationError,
    fundamental_from_input,
    news_from_input,
    outlook_profile,
    validate_outlook_profiles,
)
from novacontrol.novamarket.prediction.models import (
    ConfirmationStage,
    Direction,
    ForecastHorizon,
    ForecastValidity,
    FundamentalSnapshot,
    NewsEventEvidence,
)
from novacontrol.novamarket.scheduling.schedule import default_target

INSTRUMENT = "XNSE:RELIANCE"
SECTOR = "XNSE:NIFTY50"
BENCHMARK = "XNSE:NIFTYBANK"
PEER = "XNSE:TCS"


def _instrument(instrument_id: str, metadata: dict[str, Any] | None = None) -> Instrument:
    mic, symbol = instrument_id.split(":", 1)
    return Instrument(
        instrument_id=instrument_id,
        symbol=symbol,
        name=symbol,
        asset_class=AssetClass.EQUITY,
        exchange="NSE",
        mic=mic,
        currency="INR",
        timezone="Asia/Kolkata",
        provider_symbols={"fixture": symbol},
        metadata=metadata or {},
    )


def _bars(instrument_id: str, *, end: datetime, count: int = 80) -> list[OhlcvBar]:
    rows: list[OhlcvBar] = []
    day = end.date() - timedelta(days=count * 2 + 5)
    close = 100.0
    while len(rows) < count and day <= end.date():
        if day.weekday() < 5:
            observed = datetime(day.year, day.month, day.day, 10, 0, tzinfo=UTC)
            if observed <= end:
                close += 0.2
                rows.append(
                    OhlcvBar(
                        bar_id=f"{instrument_id}:{day.isoformat()}",
                        instrument_id=instrument_id,
                        provider="fixture",
                        interval=DataInterval.DAY_1,
                        open=Decimal(str(close - 0.1)),
                        high=Decimal(str(close + 0.4)),
                        low=Decimal(str(close - 0.4)),
                        close=Decimal(str(close)),
                        currency="INR",
                        observed_at=observed,
                        retrieved_at=observed,
                        source_timezone="UTC",
                        volume=Decimal("1000"),
                    )
                )
        day += timedelta(days=1)
    return rows


def _api_bars(*, end: datetime, count: int = 359) -> list[dict[str, Any]]:
    """Deterministic bars in the API's JSON shape, ending exactly at ``end``."""

    days: list[Any] = []
    cursor = end.date()
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    rows: list[dict[str, Any]] = []
    for index, day in enumerate(days):
        close = 100.0 + index * 0.2
        observed = datetime(day.year, day.month, day.day, 10, 0, tzinfo=UTC)
        rows.append(
            {
                "observed_at": observed.isoformat(),
                "open": close - 0.1,
                "high": close + 0.4,
                "low": close - 0.4,
                "close": close,
                "volume": 1000.0,
                "quality": "valid",
                "freshness": "fresh",
            }
        )
    return rows


class FakeMarket:
    def __init__(self) -> None:
        self.instruments = {
            INSTRUMENT: _instrument(
                INSTRUMENT,
                {
                    "sector_instrument_id": SECTOR,
                    "benchmark_instrument_id": BENCHMARK,
                    "sector_peers": [PEER],
                },
            ),
            SECTOR: _instrument(SECTOR),
            BENCHMARK: _instrument(BENCHMARK),
            PEER: _instrument(PEER),
            "XNSE:INFY": _instrument("XNSE:INFY"),
        }
        self.rows = {
            instrument_id: _bars(
                instrument_id,
                end=datetime(2026, 9, 15, 3, 45, tzinfo=UTC),
            )
            for instrument_id in self.instruments
        }

    def get_instrument(self, instrument_id: str) -> Instrument:
        return self.instruments[instrument_id]

    def bars(self, instrument_id: str, **kwargs: Any) -> Any:
        return SimpleNamespace(items=list(reversed(self.rows[instrument_id])))


class FakeEnsemble:
    def __init__(self, calendar: Any) -> None:
        self.calendar = calendar
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_horizons: set[str] = set()
        self.wrong_target = False

    async def generate(self, instrument_id: str, horizon: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((horizon, dict(kwargs)))
        if horizon in self.fail_horizons:
            raise RuntimeError(f"deterministic {horizon} failure")
        cutoff = kwargs["as_of"]
        target = default_target(
            horizon=ForecastHorizon(horizon), now=cutoff, calendar=self.calendar
        )
        if self.wrong_target:
            target += timedelta(days=1)
        downward = horizon in {"30d", "90d"}
        return {
            "ok": True,
            "status": "assembled",
            "forecast_id": f"ens_{instrument_id}_{horizon}",
            "instrument_id": instrument_id,
            "forecast_horizon": horizon,
            "reference_price": "120.0",
            "predicted_price": "114.0" if downward else "126.0",
            "predicted_return": -0.05 if downward else 0.05,
            "lower_return": -0.12 if downward else -0.02,
            "upper_return": 0.02 if downward else 0.12,
            "predicted_direction": "down" if downward else "up",
            "forecast_target_timestamp": target.isoformat(),
            "target_at": target.isoformat(),
            "generated_at": cutoff.isoformat(),
            "forecast_generated_at": cutoff.isoformat(),
            "model_id": "ensemble",
            "model_version": "pi08-test",
            "forecast_uncertainty": "high" if downward else "medium",
            "uncertainty_width": 0.14 if downward else 0.14,
            "probability_status": "unavailable",
            "data_freshness_status": "fresh",
            "forecast_validity": "valid",
            "eligibility": [
                {"model_id": "naive", "version": "1", "eligible": True, "reason": ""}
            ],
            "weights": {"naive@1": 0.6},
            "disagreement": 0.03 if downward else 0.01,
            "ensemble": {"calibration": {"status": "uncalibrated"}},
            "reasons": (),
            "metadata": {"pi10": kwargs.get("forecast_metadata")},
        }


class FakePrediction:
    def __init__(self) -> None:
        self.outcomes: dict[str, list[Any]] = {}
        self.forecasts: list[Any] = []
        self.store = self

    def list_forecasts(self, **kwargs: Any) -> list[Any]:
        rows = [
            row
            for row in self.forecasts
            if row.instrument_id == kwargs.get("instrument_id")
            and row.forecast_horizon.value == kwargs.get("horizon")
        ]
        return rows

    def forecast_outcomes(self, **kwargs: Any) -> list[Any]:
        return list(self.outcomes.get(str(kwargs.get("horizon") or ""), []))


def _outcome(
    *,
    predicted: str,
    actual: str,
    predicted_return: float = 0.02,
    actual_return: float = 0.03,
    stage: str = "t_minus_1",
) -> Any:
    return SimpleNamespace(
        predicted_direction=predicted,
        actual_direction=actual,
        direction_correct=predicted == actual,
        price_error=Decimal("1.0"),
        absolute_price_error=Decimal("1.0"),
        percentage_price_error=0.01,
        return_error=predicted_return - actual_return,
        absolute_return_error=abs(predicted_return - actual_return),
        inside_interval=True,
        interval_width=0.1,
        stage=SimpleNamespace(value=stage),
    )


def _fundamental(**overrides: Any) -> dict[str, Any]:
    payload = {
        "instrument_id": INSTRUMENT,
        "fiscal_period": "2026-Q2",
        "source_observed_at": "2026-09-10T12:00:00+00:00",
        "received_at": "2026-09-10T12:05:00+00:00",
        "currency": "INR",
        "source": "exchange-filing",
        "metrics": {"revenue": "250000", "eps": "12.4"},
    }
    payload.update(overrides)
    return payload


def _news(**overrides: Any) -> dict[str, Any]:
    payload = {
        "instrument_ids": [INSTRUMENT],
        "event_type": "earnings",
        "headline": "Quarterly results approved by the board",
        "source": "exchange-filing",
        "published_at": "2026-09-11T12:00:00+00:00",
        "received_at": "2026-09-11T12:01:00+00:00",
        "quality": "valid",
        "checksum": "event-one",
        "verification": "confirmed",
    }
    payload.update(overrides)
    return payload


def _service(*, news_provider: Any = None) -> tuple[HorizonForecastService, FakeMarket, FakeEnsemble, FakePrediction]:
    market = FakeMarket()
    ensemble = FakeEnsemble(calendar_by_mic("XNSE"))
    prediction = FakePrediction()
    service = HorizonForecastService(
        market_data=market,
        ensemble=ensemble,
        prediction=prediction,
        clock=lambda: datetime(2026, 9, 15, 3, 45, tzinfo=UTC),
        news_provider=news_provider,
    )
    return service, market, ensemble, prediction


def test_profiles_are_five_distinct_versioned_horizons() -> None:
    assert validate_outlook_profiles() == ()
    profiles = [outlook_profile(item) for item in ("1d", "3d", "5d", "30d", "90d")]
    assert [item.target_basis.value for item in profiles] == [
        "trading_sessions",
        "trading_sessions",
        "trading_sessions",
        "calendar_days",
        "calendar_days",
    ]
    assert [item.target_count for item in profiles] == [1, 3, 5, 30, 90]
    payload = profiles[0].to_dict()
    assert payload["feature_set_version"] == FEATURE_SET_VERSION
    assert "feature_profile_version" not in payload
    with pytest.raises(Exception, match="supports only swing/long"):
        outlook_profile("20m")


def test_target_semantics_use_sessions_for_swing_and_calendar_days_for_long() -> None:
    calendar = calendar_by_mic("XNSE")
    now = datetime(2026, 9, 15, 3, 45, tzinfo=UTC)  # Tuesday pre-open
    expected = {
        "1d": datetime(2026, 9, 16, 10, 0, tzinfo=UTC),
        "3d": datetime(2026, 9, 18, 10, 0, tzinfo=UTC),
        "5d": datetime(2026, 9, 22, 10, 0, tzinfo=UTC),
        "30d": datetime(2026, 10, 15, 10, 0, tzinfo=UTC),
        "90d": datetime(2026, 12, 14, 10, 0, tzinfo=UTC),
    }
    for horizon, target in expected.items():
        assert default_target(
            horizon=ForecastHorizon(horizon), now=now, calendar=calendar
        ) == target


@pytest.mark.asyncio
async def test_all_five_horizons_generate_with_point_in_time_context() -> None:
    service, _, ensemble, _ = _service()
    radar = {
        "instrument_ids": [INSTRUMENT],
        "event_type": "corporate_action",
        "headline": "Board announces dividend",
        "source": "exchange-filing",
        "published_at": "2026-09-12T12:00:00+00:00",
        "retrieved_at": "2026-09-12T12:01:00+00:00",
        "verification": "confirmed",
        "fingerprint": "radar-one",
    }
    service._news_provider = lambda instrument_id: {"events": [radar]}
    proof = await service.generate(
        INSTRUMENT,
        fundamentals=[
            _fundamental(),
            _fundamental(
                fiscal_period="2026-Q1",
                source_observed_at="2026-06-10T12:00:00+00:00",
                received_at="2026-06-10T12:05:00+00:00",
            ),
            _fundamental(
                fiscal_period="2026-Q3",
                source_observed_at="2026-10-10T12:00:00+00:00",
                received_at="2026-10-10T12:05:00+00:00",
            ),
            _fundamental(instrument_id="XNSE:INFY"),
        ],
        news_events=[
            _news(),
            _news(),  # duplicate checksum
            _news(
                headline="Unverified rumour",
                checksum="rumour",
                verification="rumour",
                quality="warning",
            ),
            _news(
                headline="Future announcement",
                checksum="future",
                published_at="2026-10-01T12:00:00+00:00",
                received_at="2026-10-01T12:01:00+00:00",
            ),
        ],
        as_of=datetime(2026, 9, 15, 3, 45, tzinfo=UTC),
    )
    assert proof["generated"] == 5
    assert proof["failed"] == 0
    assert [row["horizon"] for row in proof["results"]] == ["1d", "3d", "5d", "30d", "90d"]
    context = proof["context"]
    assert context["sector"]["available"] is True
    assert context["benchmark"]["available"] is True
    assert len(context["peers"]) == 1 and context["peers"][0]["available"] is True
    assert context["sector_relative_return_20"] is not None
    assert context["benchmark_relative_return_20"] is not None
    assert context["counts"]["fundamentals_point_in_time"] == 2
    assert context["counts"]["fundamentals_dropped_future"] == 1
    assert context["counts"]["fundamentals_model_eligible"] == 2
    assert context["counts"]["news_deduplicated"] == 3
    assert context["counts"]["news_model_eligible"] == 2
    assert context["counts"]["news_unverified"] == 1
    assert context["counts"]["news_dropped_future"] == 1
    assert len(ensemble.calls) == 5
    for _horizon, kwargs in ensemble.calls:
        assert [item.instrument_id for item in kwargs["fundamentals"]] == [
            INSTRUMENT,
            INSTRUMENT,
        ]
        assert len(kwargs["news_events"]) == 2
        assert {item.checksum for item in kwargs["news_events"]} >= {
            "event-one",
            "radar-one",
        }
        assert all(item.event_type != "rumour" for item in kwargs["news_events"])
        assert kwargs["benchmark_bars"]["1d"] and kwargs["sector_bars"]["1d"]
        assert PEER in kwargs["context_bars"]
        assert kwargs["forecast_metadata"]["pi10"]["phase"] == "PI-10"
        assert kwargs["forecast_metadata"]["pi10"]["authorizes_action"] is False
    cross = proof["cross_horizon"]
    assert cross["alignment"] == "divergent"
    assert cross["short_long_divergence"] is True
    assert cross["direction_counts"] == {"up": 3, "down": 2}
    assert proof["authority"]["risk_authority"] == "NM-08"
    assert proof["authority"]["execution_authority"] == "NM-10"
    assert proof["authority"]["portfolio_mutations"] is False


@pytest.mark.asyncio
async def test_one_horizon_failure_does_not_disable_the_others() -> None:
    service, _, ensemble, _ = _service()
    ensemble.fail_horizons = {"3d"}
    proof = await service.generate(
        INSTRUMENT, as_of=datetime(2026, 9, 15, 3, 45, tzinfo=UTC)
    )
    statuses = {row["horizon"]: row["status"] for row in proof["results"]}
    assert statuses == {
        "1d": "generated",
        "3d": "failed",
        "5d": "generated",
        "30d": "generated",
        "90d": "generated",
    }
    assert proof["partial"] is True
    failed = next(row for row in proof["results"] if row["horizon"] == "3d")
    assert failed["authorizes_action"] is False
    assert failed["risk_authority"] == "NM-08"
    assert failed["execution_authority"] == "NM-10"


@pytest.mark.asyncio
async def test_unverified_swing_target_blocks_only_that_horizon() -> None:
    service, _, ensemble, _ = _service()
    proof = await service.generate(
        INSTRUMENT, as_of=datetime(2026, 11, 16, 3, 45, tzinfo=UTC)
    )
    statuses = {row["horizon"]: row["status"] for row in proof["results"]}
    assert statuses["90d"] == "failed"
    assert statuses["1d"] == "generated"
    assert "calendar coverage" in proof["results"][-1]["reasons"][0]
    assert "90d" not in {horizon for horizon, _ in ensemble.calls}


@pytest.mark.asyncio
async def test_missing_sector_and_benchmark_mapping_is_explicit() -> None:
    service, market, _, _ = _service()
    market.instruments[INSTRUMENT] = _instrument(INSTRUMENT)
    context = (
        await service.assemble_context(
            INSTRUMENT, as_of=datetime(2026, 9, 15, 3, 45, tzinfo=UTC)
        )
    ).context
    assert context.sector is None
    assert context.benchmark is None
    assert context.sector_relative_return_20 is None
    assert any("sector" in note for note in context.notes)
    assert any("benchmark" in note for note in context.notes)
    assert context.to_dict()["fabricated_values"] is False


@pytest.mark.asyncio
async def test_a_mismatched_generated_target_fails_that_horizon() -> None:
    service, _, ensemble, _ = _service()
    ensemble.wrong_target = True
    proof = await service.generate(
        INSTRUMENT, horizons=["1d"], as_of=datetime(2026, 9, 15, 3, 45, tzinfo=UTC)
    )
    assert proof["generated"] == 0
    assert proof["failed"] == 1
    result = proof["results"][0]
    assert result["status"] == "failed"
    assert result["target_aligned"] is False
    assert "does not match the verified target" in result["reasons"][0]
    assert proof["cross_horizon"]["observations"] == []


@pytest.mark.asyncio
async def test_unconfirmed_news_is_context_only_and_never_a_model_input() -> None:
    service, _, ensemble, _ = _service()
    proof = await service.generate(
        INSTRUMENT,
        news_events=[
            _news(
                headline="A valid record with no verification claim",
                checksum="no-verification",
                verification=None,
            )
        ],
        as_of=datetime(2026, 9, 15, 3, 45, tzinfo=UTC),
    )
    assert proof["context"]["counts"]["news_model_eligible"] == 0
    assert proof["context"]["counts"]["news_unverified"] == 1
    assert proof["context"]["events"][0]["model_input"] is False
    for _horizon, kwargs in ensemble.calls:
        assert kwargs["news_events"] == ()


@pytest.mark.asyncio
async def test_nm04_radar_verification_vocabulary_is_honoured() -> None:
    def radar(
        verification: str, checksum: str, *, status: str = "active"
    ) -> dict[str, Any]:
        return {
            "instrument_ids": [INSTRUMENT],
            "event_type": "corporate_action",
            "title": f"Radar event {verification}",
            "source": "radar-fixture",
            "published_at": "2026-09-12T12:00:00+00:00",
            "retrieved_at": "2026-09-12T12:01:00+00:00",
            "verification": verification,
            "status": status,
            "fingerprint": checksum,
        }

    service, _, _, _ = _service(
        news_provider=lambda instrument_id: {
            "events": [
                radar("officially_confirmed", "radar-confirmed"),
                radar("multi_source_corroborated", "radar-corroborated"),
                radar("unverified", "radar-unverified"),
                radar("conflicting_evidence", "radar-conflicting"),
                radar("retracted", "radar-retracted"),
                radar("officially_confirmed", "radar-stale-status", status="retracted"),
            ]
        }
    )
    context = (
        await service.assemble_context(
            INSTRUMENT, as_of=datetime(2026, 9, 15, 3, 45, tzinfo=UTC)
        )
    ).context
    assert context.counts["news_model_eligible"] == 2
    assert context.counts["news_unverified"] == 4
    by_checksum = {row["checksum"]: row for row in context.to_dict()["events"]}
    assert by_checksum["radar-confirmed"]["model_input"] is True
    assert by_checksum["radar-confirmed"]["source_verification"] == "officially_confirmed"
    assert by_checksum["radar-corroborated"]["model_input"] is True
    assert by_checksum["radar-corroborated"]["source_verification"] == "multi_source_corroborated"
    assert by_checksum["radar-unverified"]["model_input"] is False
    assert by_checksum["radar-conflicting"]["model_input"] is False
    assert by_checksum["radar-retracted"]["model_input"] is False
    assert by_checksum["radar-stale-status"]["model_input"] is False
    assert by_checksum["radar-stale-status"]["event_status"] == "retracted"


@pytest.mark.asyncio
async def test_mappings_to_unregistered_instruments_are_explicit() -> None:
    service, market, _, _ = _service()
    market.instruments[INSTRUMENT] = _instrument(
        INSTRUMENT,
        {
            "sector_instrument_id": "XNSE:MISSING",
            "benchmark_instrument_id": BENCHMARK,
            "sector_peers": ["XNSE:MISSING", PEER],
        },
    )
    context = (
        await service.assemble_context(
            INSTRUMENT, as_of=datetime(2026, 9, 15, 3, 45, tzinfo=UTC)
        )
    ).context
    assert context.sector is None
    assert context.benchmark is not None and context.benchmark.available is True
    assert [peer.instrument_id for peer in context.peers] == [PEER]
    assert any("not a registered instrument" in note for note in context.notes)


@pytest.mark.asyncio
async def test_fundamental_context_preserves_full_provenance() -> None:
    service, _, _, _ = _service()
    context = (
        await service.assemble_context(
            INSTRUMENT,
            as_of=datetime(2026, 9, 15, 3, 45, tzinfo=UTC),
            fundamentals=[
                _fundamental(
                    units={"revenue": "INR crore"},
                    provenance={"filing_id": "Q2-2026", "auditor": "fixture"},
                )
            ],
        )
    ).context
    payload = context.fundamentals[0]
    assert payload["provenance"]["units"] == {"revenue": "INR crore"}
    assert payload["provenance"]["filing_id"] == "Q2-2026"


def test_latest_read_respects_the_as_of_cutoff() -> None:
    service, _, _, prediction = _service()
    forecast = SimpleNamespace(
        instrument_id=INSTRUMENT,
        forecast_horizon=ForecastHorizon.DAY_1,
        forecast_id="ens_future",
        model_id="ensemble",
        metadata={},
        generated_at=datetime(2026, 9, 16, 3, 45, tzinfo=UTC),
    )
    prediction.forecasts = [forecast]
    report = service.latest(
        INSTRUMENT, as_of=datetime(2026, 9, 15, 3, 45, tzinfo=UTC)
    )
    first = next(row for row in report["results"] if row["horizon"] == "1d")
    assert first["status"] == "unavailable"
    assert first["forecast"] is None
    assert first["risk_authority"] == "NM-08"
    assert first["execution_authority"] == "NM-10"
    assert report["context"] == {}


def test_advisory_evidence_uses_the_existing_decision_contract() -> None:
    now = datetime(2026, 9, 15, 3, 45, tzinfo=UTC)
    forecast = SimpleNamespace(
        lifecycle_id="pcyc_fixture",
        forecast_id="ens_fixture",
        instrument_id=INSTRUMENT,
        lifecycle_stage=ConfirmationStage.INITIAL,
        direction=Direction.UP,
        predicted_return=0.02,
        uncertainty="medium",
        p_up=None,
        p_down=None,
        p_neutral=None,
        reference_price=Decimal("120.0"),
        reference_at=now,
        target_at=now + timedelta(days=1),
        generated_at=now,
        data_observed_at=now,
        data_freshness=Freshness.FRESH,
        validity=ForecastValidity.VALID,
        forecast_horizon=ForecastHorizon.DAY_1,
    )
    evidence = HorizonForecastService._autonomy_evidence(forecast)
    assert evidence["evidence_id"].startswith("pev_")
    assert evidence["authorizes_action"] is False
    assert evidence["risk_authority"] == "NM-08"
    assert evidence["execution_authority"] == "NM-10"
    assert evidence["final_confirmation"] is False
    assert evidence["eligible"] is False
    assert evidence["eligible_as_advisory_evidence"] is False
    assert any("not final confirmation" in reason for reason in evidence["reasons"])


def test_parsers_require_timestamps_and_preserve_units() -> None:
    snapshot = fundamental_from_input(
        _fundamental(
            metrics=[{"name": "revenue", "value": "250000"}],
            units={"revenue": "INR crore"},
        )
    )
    assert isinstance(snapshot, FundamentalSnapshot)
    assert snapshot.metrics == {"revenue": "250000"}
    assert snapshot.provenance["units"] == {"revenue": "INR crore"}
    with pytest.raises(OutlookValidationError):
        fundamental_from_input(_fundamental(source_observed_at=None))
    with pytest.raises(OutlookValidationError):
        fundamental_from_input(_fundamental(received_at=None))
    with pytest.raises(OutlookValidationError):
        fundamental_from_input(_fundamental(quality="fabricated"))
    with pytest.raises(OutlookValidationError):
        fundamental_from_input(_fundamental(freshness="fabricated"))
    event = news_from_input(_news(verification="confirmed"))
    assert isinstance(event, NewsEventEvidence)
    assert event.instrument_ids == (INSTRUMENT,)
    with pytest.raises(OutlookValidationError):
        news_from_input(_news(published_at=None))
    with pytest.raises(OutlookValidationError):
        news_from_input(_news(received_at=None))
    with pytest.raises(OutlookValidationError):
        news_from_input(_news(quality="fabricated"))
    with pytest.raises(OutlookValidationError):
        news_from_input(_news(freshness="fabricated"))


def test_accuracy_is_separate_per_horizon_and_includes_direction_metrics() -> None:
    service, _, _, prediction = _service()
    prediction.outcomes["1d"] = [
        _outcome(predicted="up", actual="up"),
        _outcome(predicted="up", actual="down", predicted_return=0.02, actual_return=-0.01),
    ]
    prediction.outcomes["3d"] = [_outcome(predicted="down", actual="up")]
    report = service.accuracy(limit=100)
    by_horizon = {row["horizon"]: row for row in report["by_horizon"]}
    assert report["combined_accuracy"] is None
    assert by_horizon["1d"]["metrics"]["sample_size"] == 2
    assert by_horizon["1d"]["metrics"]["direction_accuracy"] == 0.5
    assert by_horizon["1d"]["metrics"]["precision_up"] == 0.5
    assert by_horizon["1d"]["metrics"]["recall_up"] == 1.0
    assert by_horizon["1d"]["metrics"]["price_rmse"] == 1.0
    assert by_horizon["3d"]["metrics"]["sample_size"] == 1
    assert by_horizon["90d"]["metrics"]["sample_size"] == 0
    assert by_horizon["90d"]["metrics"]["reliable"] is False
    service._small_sample_size = 2
    configured = service.accuracy(limit=100)
    configured_by_horizon = {row["horizon"]: row for row in configured["by_horizon"]}
    assert configured_by_horizon["1d"]["metrics"]["reliable"] is True
    assert configured_by_horizon["3d"]["metrics"]["reliable"] is False


def test_outlook_routes_are_in_the_api_surface() -> None:
    routes = {(item.method, item.path) for item in ApiSurface.default().routes}
    assert ("POST", "/market/forecasts/horizons") in routes
    assert ("GET", "/market/forecasts/horizons/status") in routes
    assert ("GET", "/market/forecasts/horizons/performance") in routes
    assert ("GET", "/market/forecasts/horizons/{instrument_id}/context") in routes
    assert ("GET", "/market/forecasts/horizons/{instrument_id}") in routes


def test_pi10_routes_are_exposed_by_the_actual_app() -> None:
    try:
        from fastapi.testclient import TestClient
    except Exception:  # pragma: no cover - optional dev dependency
        pytest.skip("httpx/TestClient is unavailable")
    from novacontrol.api.app import create_app
    from novacontrol.application import NovaControlApplication
    from novacontrol.browser import NoopBrowserRunner
    from novacontrol.desktop import NoopDesktopRunner

    with TemporaryDirectory() as tmp:
        def isolated(**kwargs: Any) -> NovaControlApplication:
            kwargs["data_dir"] = tmp
            return NovaControlApplication(**kwargs)

        with mock.patch(
            "novacontrol.api.app.NovaControlApplication", side_effect=isolated
        ), mock.patch(
            "novacontrol.application.LocalDesktopRunner", NoopDesktopRunner
        ), mock.patch(
            "novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner
        ):
            client = TestClient(create_app())
            with client:
                registered = client.post(
                    "/market/instruments",
                    json={
                        "symbol": "RELIANCE",
                        "name": "Reliance Industries",
                        "exchange": "NSE",
                        "mic": "XNSE",
                        "asset_class": "equity",
                        "currency": "INR",
                        "timezone": "Asia/Kolkata",
                        "provider_symbols": {"yahoo": "RELIANCE.NS"},
                    },
                )
                assert registered.status_code == 200, registered.text
                status = client.get("/market/forecasts/horizons/status")
                assert status.status_code == 200, status.text
                assert status.json()["phase"] == "PI-10"
                assert len(status.json()["profiles"]) == 5
                rejected = client.post(
                    "/market/forecasts/horizons",
                    json={"instrument_id": INSTRUMENT, "horizons": ["20m"]},
                )
                assert rejected.status_code == 422, rejected.text
                assert "1d, 3d, 5d, 30d, 90d" in rejected.text
                generated = client.post(
                    "/market/forecasts/horizons",
                    json={"instrument_id": INSTRUMENT},
                )
                assert generated.status_code == 200, generated.text
                body = generated.json()
                assert body["phase"] == "PI-10"
                assert [row["horizon"] for row in body["results"]] == [
                    "1d",
                    "3d",
                    "5d",
                    "30d",
                    "90d",
                ]
                # In this isolated app no model has validation evidence, so the
                # ensemble may abstain. The target for the 90d horizon also falls
                # outside verified 2026 calendar coverage. Both are explicit and
                # must not disable the other horizons.
                assert body["generated"] == 0
                assert body["authority"]["risk_authority"] == "NM-08"
                assert body["authority"]["execution_authority"] == "NM-10"
                latest = client.get(f"/market/forecasts/horizons/{INSTRUMENT}")
                assert latest.status_code == 200, latest.text
                assert len(latest.json()["results"]) == 5
                context = client.get(
                    f"/market/forecasts/horizons/{INSTRUMENT}/context"
                )
                assert context.status_code == 200, context.text
                assert context.json()["point_in_time"] is True
                assert context.json()["sector"] is None


def test_pi10_actual_app_generates_and_stores_with_seeded_evidence() -> None:
    """Seed model evidence, then generate and read a real forecast over HTTP."""

    try:
        from fastapi.testclient import TestClient
    except Exception:  # pragma: no cover - optional dev dependency
        pytest.skip("httpx/TestClient is unavailable")
    from novacontrol.api.app import create_app
    from novacontrol.application import NovaControlApplication
    from novacontrol.browser import NoopBrowserRunner
    from novacontrol.desktop import NoopDesktopRunner

    with TemporaryDirectory() as tmp:
        def isolated(**kwargs: Any) -> NovaControlApplication:
            kwargs["data_dir"] = tmp
            return NovaControlApplication(**kwargs)

        with mock.patch(
            "novacontrol.api.app.NovaControlApplication", side_effect=isolated
        ), mock.patch(
            "novacontrol.application.LocalDesktopRunner", NoopDesktopRunner
        ), mock.patch(
            "novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner
        ):
            client = TestClient(create_app())
            with client:
                registered = client.post(
                    "/market/instruments",
                    json={
                        "symbol": "PI10VERIFY",
                        "name": "PI-10 Verification",
                        "exchange": "NSE",
                        "mic": "XNSE",
                        "asset_class": "equity",
                        "currency": "INR",
                        "timezone": "Asia/Kolkata",
                        "provider_symbols": {"fixture": "PI10VERIFY"},
                    },
                )
                assert registered.status_code == 200, registered.text
                as_of = "2026-09-15T10:00:00+00:00"
                bars = _api_bars(end=datetime(2026, 9, 15, 10, 0, tzinfo=UTC))

                trained = client.post(
                    "/market/forecasts/train",
                    json={
                        "instrument_id": "XNSE:PI10VERIFY",
                        "horizon": "1d",
                        "engine": "statistical",
                        "as_of": as_of,
                        "resolution": "1d",
                        "bars": bars,
                        "models": ["arima", "historical_return"],
                        "train_ratio": 0.6,
                        "validation_ratio": 0.2,
                    },
                )
                assert trained.status_code == 200, trained.text
                training = trained.json()
                assert training["models"]["arima"]["validation"]["samples"] >= 5

                generated = client.post(
                    "/market/forecasts/horizons",
                    json={
                        "instrument_id": "XNSE:PI10VERIFY",
                        "horizons": ["1d"],
                        "as_of": as_of,
                        "resolution": "1d",
                        "bars": bars,
                    },
                )
                assert generated.status_code == 200, generated.text
                body = generated.json()
                assert body["generated"] == 1, [
                    (row["horizon"], row["status"], row["reasons"], row.get("suitability"))
                    for row in body["results"]
                ]
                assert body["abstained"] == 0, body
                assert body["failed"] == 0, body
                result = body["results"][0]
                assert result["horizon"] == "1d"
                assert result["status"] == "generated"
                assert result["target_aligned"] is True
                forecast = result["forecast"]
                assert forecast["forecast_id"].startswith("ens_")
                assert forecast["forecast_target_timestamp"] == "2026-09-16T10:00:00+00:00"

                latest = client.get(
                    "/market/forecasts/horizons/XNSE:PI10VERIFY",
                    params={"as_of": as_of},
                )
                assert latest.status_code == 200, latest.text
                stored = [
                    row for row in latest.json()["results"] if row["horizon"] == "1d"
                ]
                assert stored and stored[0]["status"] == "stored"
                assert stored[0]["forecast"]["forecast_id"] == forecast["forecast_id"]

                persisted = client.get(
                    f"/market/forecasts/{forecast['forecast_id']}"
                )
                assert persisted.status_code == 200, persisted.text
                detail = persisted.json()
                assert detail["forecast_id"] == forecast["forecast_id"]
                assert detail["forecast_horizon"] == "1d"
                assert detail["data_quality"] == "valid"
                assert detail["risk_authority"] == "NM-08"
                assert detail["execution_authority"] == "NM-10"


def test_pi10_actual_app_generates_long_horizon_with_seeded_evidence() -> None:
    """Seed five 30-day validation origins and generate a real long forecast."""

    try:
        from fastapi.testclient import TestClient
    except Exception:  # pragma: no cover - optional dev dependency
        pytest.skip("httpx/TestClient is unavailable")
    from novacontrol.api.app import create_app
    from novacontrol.application import NovaControlApplication
    from novacontrol.browser import NoopBrowserRunner
    from novacontrol.desktop import NoopDesktopRunner

    with TemporaryDirectory() as tmp:
        def isolated(**kwargs: Any) -> NovaControlApplication:
            kwargs["data_dir"] = tmp
            return NovaControlApplication(**kwargs)

        with mock.patch(
            "novacontrol.api.app.NovaControlApplication", side_effect=isolated
        ), mock.patch(
            "novacontrol.application.LocalDesktopRunner", NoopDesktopRunner
        ), mock.patch(
            "novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner
        ):
            client = TestClient(create_app())
            with client:
                registered = client.post(
                    "/market/instruments",
                    json={
                        "symbol": "PI10LONG",
                        "name": "PI-10 Long-Horizon Verification",
                        "exchange": "NSE",
                        "mic": "XNSE",
                        "asset_class": "equity",
                        "currency": "INR",
                        "timezone": "Asia/Kolkata",
                        "provider_symbols": {"fixture": "PI10LONG"},
                    },
                )
                assert registered.status_code == 200, registered.text
                as_of = "2026-09-15T10:00:00+00:00"
                bars = _api_bars(
                    end=datetime(2026, 9, 15, 10, 0, tzinfo=UTC), count=800
                )

                trained = client.post(
                    "/market/forecasts/train",
                    json={
                        "instrument_id": "XNSE:PI10LONG",
                        "horizon": "30d",
                        "engine": "statistical",
                        "as_of": as_of,
                        "resolution": "1d",
                        "bars": bars,
                        "models": ["arima", "historical_return"],
                        "train_ratio": 0.6,
                        "validation_ratio": 0.2,
                    },
                )
                assert trained.status_code == 200, trained.text
                training = trained.json()
                assert training["validation_origins"] >= 5
                assert training["models"]["arima"]["validation"]["samples"] >= 5

                generated = client.post(
                    "/market/forecasts/horizons",
                    json={
                        "instrument_id": "XNSE:PI10LONG",
                        "horizons": ["30d"],
                        "as_of": as_of,
                        "resolution": "1d",
                        "bars": bars,
                    },
                )
                assert generated.status_code == 200, generated.text
                body = generated.json()
                assert body["generated"] == 1, body
                assert body["abstained"] == 0, body
                result = body["results"][0]
                assert result["horizon"] == "30d"
                assert result["status"] == "generated"
                assert result["target_aligned"] is True
                forecast = result["forecast"]
                assert forecast["forecast_id"].startswith("ens_")
                assert forecast["forecast_target_timestamp"] == "2026-10-15T10:00:00+00:00"

                latest = client.get(
                    "/market/forecasts/horizons/XNSE:PI10LONG",
                    params={"as_of": as_of},
                )
                assert latest.status_code == 200, latest.text
                stored = [
                    row for row in latest.json()["results"] if row["horizon"] == "30d"
                ]
                assert stored and stored[0]["status"] == "stored"
                assert stored[0]["forecast"]["forecast_id"] == forecast["forecast_id"]

                persisted = client.get(f"/market/forecasts/{forecast['forecast_id']}")
                assert persisted.status_code == 200, persisted.text
                detail = persisted.json()
                assert detail["forecast_horizon"] == "30d"
                assert detail["data_quality"] == "valid"
                assert detail["risk_authority"] == "NM-08"
                assert detail["execution_authority"] == "NM-10"


def test_pi10_actual_app_generates_ninety_day_horizon_with_seeded_evidence() -> None:
    """Seed five 90-day validation origins and generate a real quarter forecast.

    A 90-day target steps 90 bars between walk-forward origins, so the default
    60/20 split can only expose three of them.  The wider validation window
    (0.4/0.4 over 1,600 verified sessions) is what makes five origins - the
    ensemble's minimum - available without changing the split defaults.
    """

    try:
        from fastapi.testclient import TestClient
    except Exception:  # pragma: no cover - optional dev dependency
        pytest.skip("httpx/TestClient is unavailable")
    from novacontrol.api.app import create_app
    from novacontrol.application import NovaControlApplication
    from novacontrol.browser import NoopBrowserRunner
    from novacontrol.desktop import NoopDesktopRunner

    with TemporaryDirectory() as tmp:
        def isolated(**kwargs: Any) -> NovaControlApplication:
            kwargs["data_dir"] = tmp
            return NovaControlApplication(**kwargs)

        with mock.patch(
            "novacontrol.api.app.NovaControlApplication", side_effect=isolated
        ), mock.patch(
            "novacontrol.application.LocalDesktopRunner", NoopDesktopRunner
        ), mock.patch(
            "novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner
        ):
            client = TestClient(create_app())
            with client:
                registered = client.post(
                    "/market/instruments",
                    json={
                        "symbol": "PI10QUARTER",
                        "name": "PI-10 Quarter Verification",
                        "exchange": "NSE",
                        "mic": "XNSE",
                        "asset_class": "equity",
                        "currency": "INR",
                        "timezone": "Asia/Kolkata",
                        "provider_symbols": {"fixture": "PI10QUARTER"},
                    },
                )
                assert registered.status_code == 200, registered.text
                as_of = "2026-09-15T10:00:00+00:00"
                bars = _api_bars(
                    end=datetime(2026, 9, 15, 10, 0, tzinfo=UTC), count=1600
                )

                trained = client.post(
                    "/market/forecasts/train",
                    json={
                        "instrument_id": "XNSE:PI10QUARTER",
                        "horizon": "90d",
                        "engine": "statistical",
                        "as_of": as_of,
                        "resolution": "1d",
                        "bars": bars,
                        "models": ["arima", "historical_return"],
                        "train_ratio": 0.4,
                        "validation_ratio": 0.4,
                    },
                )
                assert trained.status_code == 200, trained.text
                training = trained.json()
                assert training["validation_origins"] >= 5
                assert training["models"]["arima"]["validation"]["samples"] >= 5
                assert (
                    training["models"]["historical_return"]["validation"]["samples"]
                    >= 5
                )

                generated = client.post(
                    "/market/forecasts/horizons",
                    json={
                        "instrument_id": "XNSE:PI10QUARTER",
                        "horizons": ["90d"],
                        "as_of": as_of,
                        "resolution": "1d",
                        "bars": bars,
                    },
                )
                assert generated.status_code == 200, generated.text
                body = generated.json()
                assert body["generated"] == 1, body
                assert body["abstained"] == 0, body
                result = body["results"][0]
                assert result["horizon"] == "90d"
                assert result["status"] == "generated"
                assert result["target_aligned"] is True
                forecast = result["forecast"]
                assert forecast["forecast_id"].startswith("ens_")
                assert forecast["forecast_target_timestamp"] == (
                    "2026-12-14T10:00:00+00:00"
                )

                latest = client.get(
                    "/market/forecasts/horizons/XNSE:PI10QUARTER",
                    params={"as_of": as_of},
                )
                assert latest.status_code == 200, latest.text
                stored = [
                    row for row in latest.json()["results"] if row["horizon"] == "90d"
                ]
                assert stored and stored[0]["status"] == "stored"
                assert stored[0]["forecast"]["forecast_id"] == forecast["forecast_id"]

                persisted = client.get(f"/market/forecasts/{forecast['forecast_id']}")
                assert persisted.status_code == 200, persisted.text
                detail = persisted.json()
                assert detail["forecast_horizon"] == "90d"
                assert detail["data_quality"] == "valid"
                assert detail["risk_authority"] == "NM-08"
                assert detail["execution_authority"] == "NM-10"
                assert detail["authorizes_action"] is False


def test_all_five_horizon_routes_are_registered_exactly_once() -> None:
    # The API reference is generated from the same surface; this guards against
    # a PI-10 route being added only to FastAPI and not to the registry.
    paths = {item.path for item in ApiSurface.default().routes}
    assert len([path for path in paths if path.startswith("/market/forecasts/horizons")]) == 5
