"""PI-01 tests: contracts, lifecycle, data semantics and existing integration."""

from __future__ import annotations

import sqlite3
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from novacontrol.novamarket.architecture import (
    PREDICTION_DOMAIN_BOUNDARIES,
    PREDICTION_ROADMAP,
    validate_architecture,
    validate_prediction_architecture,
)
from novacontrol.novamarket.events import MarketEventPublisher
from novacontrol.novamarket.marketdata.models import (
    DataInterval,
    DataQuality,
    Freshness,
    Instrument,
    PriceObservation,
    ReconciliationResult,
    ReconciliationStatus,
)
from novacontrol.novamarket.prediction import (
    CalibrationStatus,
    ConfirmationStage,
    Direction,
    FeatureDefinition,
    FeatureVector,
    ForecastFamily,
    ForecastHorizon,
    ForecastOutput,
    ForecastValidity,
    FundamentalSnapshot,
    HistoricalMarketDataset,
    MarketDataSnapshot,
    NewsEventEvidence,
    PredictionEvidenceBundle,
    ResourcePolicy,
    ModelFamily,
    ModelMetadata,
    ModelPrediction,
    ModelState,
    PredictionDataError,
    PredictionPolicy,
    PredictionRequest,
    PredictionStateError,
    PredictionStore,
    PredictionValidationError,
    ProbabilityStatus,
    horizon_spec,
)
from novacontrol.novamarket.prediction.ensemble import EnsembleAssembler
from novacontrol.novamarket.prediction.service import PredictionIntelligenceService
from novacontrol.novamarket.service import CAPABILITIES, NovaMarketService
from novacontrol.novamarket.events import IMPLEMENTED_EVENTS

_TARGET = datetime(2026, 11, 13, 10, 30, tzinfo=UTC)
_INITIAL = _TARGET - timedelta(minutes=20)
_FIRST = _TARGET - timedelta(minutes=5)
_FINAL = _TARGET - timedelta(minutes=1)


def _instrument() -> Instrument:
    return Instrument.create(
        symbol="RELIANCE",
        exchange="NSE",
        currency="INR",
        timezone="Asia/Kolkata",
        provider_symbols={"fake": "RELIANCE.NS"},
    )


def _observation(
    price: str, observed_at: datetime, *, provider: str = "fake"
) -> PriceObservation:
    return PriceObservation.create(
        instrument_id=_instrument().instrument_id,
        provider=provider,
        price=price,
        currency="INR",
        observed_at=observed_at,
        retrieved_at=observed_at + timedelta(seconds=10),
        source_timezone="Asia/Kolkata",
        quality=DataQuality.VALID,
        freshness=Freshness.FRESH,
    )


class FakeMarketData:
    """A deterministic read-only stand-in for the NM-02 engine."""

    def __init__(self) -> None:
        self.instrument = _instrument()
        self.observation: PriceObservation | None = _observation(
            "2450.50", _TARGET - timedelta(minutes=21)
        )
        self.conflict = False
        self.bars_items: tuple[object, ...] = ()

    def get_instrument(self, instrument_id: str) -> Instrument:
        if instrument_id != self.instrument.instrument_id:
            raise KeyError(instrument_id)
        return self.instrument

    def latest(self, instrument_id: str) -> ReconciliationResult:
        if self.conflict:
            return ReconciliationResult(
                instrument_id=instrument_id,
                status=ReconciliationStatus.CONFLICT,
                selected=None,
                observations=(),
                reasons=("providers disagree",),
            )
        return ReconciliationResult(
            instrument_id=instrument_id,
            status=ReconciliationStatus.OK,
            selected=self.observation,
            observations=(self.observation,) if self.observation else (),
        )

    def bars(self, instrument_id: str, **_: object) -> SimpleNamespace:
        return SimpleNamespace(items=self.bars_items, total=len(self.bars_items))

    def provider_health(self, provider: str) -> SimpleNamespace:
        return SimpleNamespace(
            provider=provider,
            status="ok",
            circuit_state="closed",
            latency_ms=12.0,
            last_error="",
        )


def _model(calibration: CalibrationStatus = CalibrationStatus.UNCALIBRATED) -> ModelMetadata:
    return ModelMetadata(
        model_id="baseline",
        version="1.0.0",
        family=ModelFamily.NAIVE,
        state=ModelState.ELIGIBLE,
        supported_horizons=(ForecastHorizon.MINUTES_20,),
        calibration=calibration,
    )


def _request() -> PredictionRequest:
    return PredictionRequest(
        request_id="req-pi01",
        instrument_id=_instrument().instrument_id,
        exchange="NSE",
        horizon=ForecastHorizon.MINUTES_20,
        target_at=_TARGET,
        created_at=_INITIAL,
    )


def _prediction(
    snapshot: MarketDataSnapshot,
    *,
    predicted_return: float,
    lower: float | None = None,
    upper: float | None = None,
    generated_at: datetime | None = None,
    calibration: CalibrationStatus = CalibrationStatus.UNCALIBRATED,
    p_up: float | None = None,
    p_down: float | None = None,
    p_neutral: float | None = None,
    model_id: str = "baseline",
    family: ModelFamily = ModelFamily.NAIVE,
) -> ModelPrediction:
    return ModelPrediction(
        model_id=model_id,
        model_version="1.0.0",
        family=family,
        instrument_id=snapshot.instrument_id,
        horizon=ForecastHorizon.MINUTES_20,
        generated_at=generated_at or snapshot.received_at + timedelta(seconds=1),
        data_cutoff=snapshot.received_at,
        data_snapshot_id=snapshot.snapshot_id,
        reference_price=snapshot.reference_price,
        predicted_return=predicted_return,
        lower_return=predicted_return - 0.01 if lower is None else lower,
        upper_return=predicted_return + 0.01 if upper is None else upper,
        p_up=p_up,
        p_down=p_down,
        p_neutral=p_neutral,
        calibration=calibration,
    )


class HorizonContractTests(unittest.TestCase):
    def test_intraday_and_daily_resolutions_are_not_interchangeable(self) -> None:
        five = horizon_spec(ForecastHorizon.MINUTES_5)
        twenty = horizon_spec(ForecastHorizon.MINUTES_20)
        day = horizon_spec(ForecastHorizon.DAY_1)
        self.assertTrue(five.accepts_resolution(DataInterval.SPOT))
        self.assertTrue(five.accepts_resolution(DataInterval.MINUTE_1))
        self.assertFalse(five.accepts_resolution(DataInterval.DAY_1))
        self.assertFalse(twenty.accepts_resolution(DataInterval.DAY_1))
        self.assertTrue(day.accepts_resolution(DataInterval.DAY_1))
        self.assertEqual(day.family, ForecastFamily.SWING)
        self.assertEqual(horizon_spec(ForecastHorizon.DAYS_90).family, ForecastFamily.LONG_TERM)

    def test_policy_keeps_general_refresh_floor_and_discovery_cap(self) -> None:
        policy = PredictionPolicy()
        self.assertEqual(policy.initial_lead_minutes, 20)
        self.assertEqual(policy.first_confirmation_lead_minutes, 5)
        self.assertEqual(policy.final_confirmation_lead_minutes, 1)
        with self.assertRaises(PredictionValidationError):
            PredictionPolicy(automatic_promotion_allowed=True)
        with self.assertRaises(PredictionValidationError):
            from novacontrol.novamarket.prediction import AcquisitionPolicy

            AcquisitionPolicy(general_refresh_floor_seconds=60)
        with self.assertRaises(PredictionValidationError):
            from novacontrol.novamarket.prediction import AcquisitionPolicy

            AcquisitionPolicy(discovery_batch_cap=21)
        with self.assertRaises(PredictionValidationError):
            from novacontrol.novamarket.prediction import AcquisitionPolicy

            AcquisitionPolicy(fair_rotation_preserved=False)
        from novacontrol.novamarket.prediction import (
            AcquisitionPolicy,
            low_latency_contract,
        )

        custom = AcquisitionPolicy(intraday_min_interval_seconds=60)
        self.assertEqual(
            low_latency_contract(custom)["intraday_min_interval_seconds"], 60
        )
        self.assertTrue(low_latency_contract(custom)["fair_rotation_preserved"])
        for invalid in (
            {"intraday_min_interval_seconds": 29},
            {"intraday_max_requests_per_minute": 21},
            {"max_concurrent_fetches": 3},
        ):
            with self.assertRaises(PredictionValidationError):
                AcquisitionPolicy(**invalid)


class ContractTests(unittest.TestCase):
    def test_probabilities_must_be_complete_and_sum_to_one(self) -> None:
        snapshot = self._snapshot()
        with self.assertRaises(PredictionValidationError):
            _prediction(snapshot, predicted_return=0.01, p_up=0.5)
        with self.assertRaises(PredictionValidationError):
            _prediction(
                snapshot,
                predicted_return=0.01,
                p_up=0.5,
                p_down=0.5,
                p_neutral=0.5,
            )

    def test_uncalibrated_probabilities_are_withheld_not_relabelled(self) -> None:
        snapshot = self._snapshot()
        model = _model(CalibrationStatus.UNCALIBRATED)
        prediction = _prediction(
            snapshot,
            predicted_return=0.01,
            p_up=0.6,
            p_down=0.3,
            p_neutral=0.1,
        )
        result = EnsembleAssembler().assemble(
            _request(), snapshot, (prediction,), (model,), now=_INITIAL
        )
        assert result.output is not None
        self.assertEqual(result.output.probability_status, ProbabilityStatus.UNAVAILABLE)
        self.assertIsNone(result.output.p_up)
        self.assertIsNone(result.output.p_down)
        self.assertEqual(result.output.calibration_status, CalibrationStatus.UNCALIBRATED)

    def test_calibrated_probabilities_are_carried_only_with_calibrated_models(self) -> None:
        snapshot = self._snapshot()
        model = _model(CalibrationStatus.CALIBRATED)
        prediction = _prediction(
            snapshot,
            predicted_return=0.01,
            p_up=0.6,
            p_down=0.3,
            p_neutral=0.1,
            calibration=CalibrationStatus.CALIBRATED,
        )
        result = EnsembleAssembler().assemble(
            _request(), snapshot, (prediction,), (model,), now=_INITIAL
        )
        assert result.output is not None
        self.assertEqual(result.output.probability_status, ProbabilityStatus.CALIBRATED)
        self.assertAlmostEqual(result.output.p_up or 0.0, 0.6)
        self.assertEqual(result.output.direction, Direction.UP)

    def test_forecast_output_is_marked_as_not_a_market_observation_or_action(self) -> None:
        result = EnsembleAssembler().assemble(
            _request(),
            self._snapshot(),
            (_prediction(self._snapshot(), predicted_return=0.01),),
            (_model(),),
            now=_INITIAL,
        )
        assert result.output is not None
        payload = result.output.to_dict()
        self.assertTrue(payload["is_forecast"])
        self.assertFalse(payload["is_market_observation"])
        self.assertFalse(payload["is_price_prediction"])
        self.assertFalse(payload["authorizes_action"])
        self.assertTrue(payload["paper_only"])
        self.assertFalse(payload["live_trading"])
        self.assertFalse(payload["broker_connected"])
        self.assertEqual(payload["risk_authority"], "NM-08")
        self.assertEqual(payload["execution_authority"], "NM-10")

    def test_ensemble_abstains_when_models_disagree_too_much(self) -> None:
        snapshot = self._snapshot()
        first = _prediction(snapshot, predicted_return=0.01, model_id="baseline")
        second = _prediction(
            snapshot,
            predicted_return=0.30,
            model_id="boost",
            family=ModelFamily.XGBOOST,
        )
        second_model = ModelMetadata(
            model_id="boost",
            version="1.0.0",
            family=ModelFamily.XGBOOST,
            state=ModelState.ELIGIBLE,
            supported_horizons=(ForecastHorizon.MINUTES_20,),
            calibration=CalibrationStatus.UNCALIBRATED,
        )
        result = EnsembleAssembler().assemble(
            _request(),
            snapshot,
            (first, second),
            (_model(), second_model),
            now=_INITIAL,
        )
        self.assertEqual(result.status, "abstained")
        self.assertIsNone(result.output)
        self.assertTrue(result.reasons)

    def test_ensemble_excludes_an_unregistered_or_suspended_model(self) -> None:
        snapshot = self._snapshot()
        prediction = _prediction(snapshot, predicted_return=0.01)
        result = EnsembleAssembler().assemble(
            _request(), snapshot, (prediction,), (), now=_INITIAL
        )
        self.assertEqual(result.status, "abstained")
        self.assertIsNone(result.output)
        self.assertIn("model is not registered", str(result.excluded_models))

    def test_ensemble_abstains_instead_of_constructing_a_nonpositive_price(self) -> None:
        snapshot = self._snapshot()
        prediction = _prediction(
            snapshot,
            predicted_return=-1.10,
            lower=-1.20,
            upper=-1.00,
        )
        result = EnsembleAssembler().assemble(
            _request(), snapshot, (prediction,), (_model(),), now=_INITIAL
        )
        self.assertEqual(result.status, "abstained")
        self.assertIsNone(result.output)
        self.assertIn("non-positive", str(result.reasons))

    def test_model_registry_drops_horizons_no_longer_supported(self) -> None:
        store = PredictionStore(":memory:")
        store.register_model(_model())
        self.assertEqual(len(store.models(ForecastHorizon.MINUTES_20)), 1)
        store.register_model(
            replace(_model(), supported_horizons=(ForecastHorizon.DAY_1,))
        )
        self.assertEqual(store.models(ForecastHorizon.MINUTES_20), [])
        self.assertEqual(len(store.models(ForecastHorizon.DAY_1)), 1)

    def test_data_contracts_preserve_source_and_receipt_timestamps(self) -> None:
        snapshot = self._snapshot()
        definition = FeatureDefinition(
            name="return_5m",
            version="1.0.0",
            dtype="float",
            description="Five-minute simple return.",
            source=snapshot.snapshot_id,
            lookback="5m",
        )
        self.assertEqual(definition.to_dict()["contract_version"], "prediction.feature-definition.1.0")
        self.assertEqual(definition.name, "return_5m")
        features = FeatureVector(
            feature_set_id="price_features",
            version="1.0.0",
            instrument_id=snapshot.instrument_id,
            as_of=_INITIAL - timedelta(seconds=30),
            values={"return_5m": 0.001, "rsi_14": 55.0},
            data_snapshot_ids=(snapshot.snapshot_id,),
            lineage={"return_5m": snapshot.snapshot_id},
        )
        fundamentals = FundamentalSnapshot(
            snapshot_id="fund_1",
            instrument_id=snapshot.instrument_id,
            fiscal_period="2026-Q2",
            source_observed_at=_INITIAL - timedelta(days=1),
            received_at=_INITIAL - timedelta(hours=23),
            currency="INR",
            metrics={"revenue": "1000.00", "net_margin": "0.12"},
            source="company-filing",
            quality=DataQuality.VALID,
            freshness=Freshness.FRESH,
        )
        news = NewsEventEvidence(
            evidence_id="news_1",
            instrument_ids=(snapshot.instrument_id,),
            event_type="earnings",
            headline="Quarterly result announced",
            source="exchange-filing",
            published_at=_INITIAL - timedelta(hours=2),
            received_at=_INITIAL - timedelta(hours=1),
            quality=DataQuality.VALID,
            freshness=Freshness.FRESH,
        )
        dataset = HistoricalMarketDataset(
            dataset_id="daily-reliance",
            version="1.0.0",
            instrument_id=snapshot.instrument_id,
            resolution=DataInterval.DAY_1,
            start_at=_INITIAL - timedelta(days=180),
            end_at=_INITIAL,
            bar_count=120,
            source_snapshot_ids=(snapshot.snapshot_id,),
        )
        bundle = PredictionEvidenceBundle(
            bundle_id="bundle_1",
            instrument_id=snapshot.instrument_id,
            market_snapshot=snapshot,
            feature_vector=features,
            fundamentals=(fundamentals,),
            news_events=(news,),
            datasets=(dataset,),
            assembled_at=_INITIAL,
        )
        self.assertEqual(features.to_dict()["as_of"], (_INITIAL - timedelta(seconds=30)).isoformat())
        self.assertEqual(fundamentals.source_observed_at, _INITIAL - timedelta(days=1))
        self.assertNotEqual(fundamentals.source_observed_at, fundamentals.received_at)
        self.assertEqual(news.to_dict()["contract_version"], "prediction.news-events.1.0")
        self.assertEqual(dataset.to_dict()["resolution"], "1d")
        self.assertEqual(bundle.to_dict()["contract_version"], "prediction.evidence-bundle.1.0")
        self.assertFalse(bundle.to_dict()["second_data_acquisition_authority"])
        schedule = ResourcePolicy().to_dict()
        self.assertEqual(schedule["model_load_strategy"], "lazy")
        self.assertFalse(schedule["background_scheduler"])
        with self.assertRaises(PredictionValidationError):
            ResourcePolicy(distributed_workers=1)
        with self.assertRaises(PredictionValidationError):
            ResourcePolicy(background_scheduler=True)
        with self.assertRaises(PredictionValidationError):
            ResourcePolicy(failure_isolation=False)

    def _snapshot(self) -> MarketDataSnapshot:
        observed = _INITIAL - timedelta(seconds=30)
        return MarketDataSnapshot(
            snapshot_id="pds_test",
            instrument_id=_instrument().instrument_id,
            exchange="NSE",
            provider="fake",
            source_observed_at=observed,
            received_at=observed + timedelta(seconds=10),
            resolution=DataInterval.SPOT,
            reference_price=Decimal("2450.50"),
            currency="INR",
            quality=DataQuality.VALID,
            freshness=Freshness.FRESH,
            usable=True,
            provider_conflict=False,
            missing_observation=False,
            rate_limited=False,
            provider_health="ok",
            history_bars=100,
            history_sufficient=True,
        )


class DataSemanticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_cached_observation_is_not_fresh(self) -> None:
        fake = FakeMarketData()
        fake.observation = _observation("2450.50", _FIRST - timedelta(minutes=20))
        service = PredictionIntelligenceService(
            store=PredictionStore(":memory:"), market_data=fake, clock=lambda: _FIRST
        )
        snapshot = service.data_snapshot(_request(), stage=ConfirmationStage.FIRST, at=_FIRST)
        self.assertEqual(snapshot.source_observed_at, _FIRST - timedelta(minutes=20))
        self.assertFalse(snapshot.usable)
        self.assertEqual(snapshot.freshness, Freshness.STALE)
        self.assertTrue(any("allows" in reason for reason in snapshot.reasons))

    async def test_provider_unavailable_observation_is_not_relabelled_fresh(self) -> None:
        fake = FakeMarketData()
        fake.observation = None
        fake.bars_items = (
            SimpleNamespace(
                observed_at=_INITIAL - timedelta(seconds=30),
                retrieved_at=_INITIAL - timedelta(seconds=20),
                provider="fake",
                interval=DataInterval.MINUTE_5,
                close=Decimal("2450.50"),
                open=Decimal("2440"),
                high=Decimal("2460"),
                low=Decimal("2430"),
                currency="INR",
                quality=DataQuality.VALID,
                freshness=Freshness.PROVIDER_UNAVAILABLE,
                partial=False,
                bar_id="bar-unavailable",
                reasons=(),
                provider_metadata={},
                to_dict=lambda: {"bar_id": "bar-unavailable", "interval": "5m"},
            ),
        )
        service = PredictionIntelligenceService(
            store=PredictionStore(":memory:"), market_data=fake, clock=lambda: _INITIAL
        )
        snapshot = service.data_snapshot(_request(), at=_INITIAL)
        self.assertEqual(snapshot.freshness, Freshness.PROVIDER_UNAVAILABLE)
        self.assertFalse(snapshot.usable)

    async def test_conflict_is_visible_and_never_fabricated(self) -> None:
        fake = FakeMarketData()
        fake.conflict = True
        service = PredictionIntelligenceService(
            store=PredictionStore(":memory:"), market_data=fake, clock=lambda: _INITIAL
        )
        snapshot = service.data_snapshot(_request(), at=_INITIAL)
        self.assertFalse(snapshot.usable)
        self.assertTrue(snapshot.provider_conflict)
        self.assertIsNone(snapshot.provenance.get("observation_id"))

    async def test_daily_bar_cannot_satisfy_a_five_minute_horizon(self) -> None:
        fake = FakeMarketData()
        fake.observation = None
        fake.bars_items = (
            SimpleNamespace(
                observed_at=_INITIAL,
                retrieved_at=_INITIAL,
                provider="fake",
                interval=DataInterval.DAY_1,
                close=Decimal("2450.50"),
                open=Decimal("2440"),
                high=Decimal("2460"),
                low=Decimal("2430"),
                currency="INR",
                quality=DataQuality.VALID,
                freshness=Freshness.FRESH,
                partial=False,
                bar_id="bar1",
                reasons=(),
                provider_metadata={},
                to_dict=lambda: {"bar_id": "bar1", "interval": "1d"},
            ),
        )
        request = PredictionRequest(
            request_id="req-5m",
            instrument_id=_instrument().instrument_id,
            exchange="NSE",
            horizon=ForecastHorizon.MINUTES_5,
            target_at=_INITIAL + timedelta(minutes=20),
            created_at=_INITIAL,
        )
        service = PredictionIntelligenceService(
            store=PredictionStore(":memory:"), market_data=fake, clock=lambda: _INITIAL
        )
        snapshot = service.data_snapshot(request, at=_INITIAL)
        self.assertFalse(snapshot.usable)
        self.assertEqual(snapshot.resolution, DataInterval.DAY_1)
        self.assertTrue(any("cannot support" in reason for reason in snapshot.reasons))


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fake = FakeMarketData()
        self.store = PredictionStore(":memory:")
        self.service = PredictionIntelligenceService(
            store=self.store,
            market_data=self.fake,
            clock=lambda: _INITIAL,
            publisher=MarketEventPublisher(),
        )
        await self.service.register_model(_model())

    async def _snapshot(
        self, at: datetime, stage: ConfirmationStage, price: str = "2450.50"
    ) -> MarketDataSnapshot:
        self.fake.observation = _observation(price, at - timedelta(seconds=30))
        return self.service.data_snapshot(_request(), stage=stage, at=at)

    async def test_initial_lifecycle_cannot_start_after_t5(self) -> None:
        with self.assertRaises(PredictionStateError):
            await self.service.create_lifecycle(
                _request(),
                (),
                models=(_model(),),
                at=_FIRST,
            )

    async def test_initial_first_and_final_are_preserved_as_three_versions(self) -> None:
        initial = await self._snapshot(_INITIAL, ConfirmationStage.INITIAL)
        created = await self.service.create_lifecycle(
            _request(),
            (_prediction(initial, predicted_return=0.01),),
            models=(_model(),),
            snapshot=initial,
            at=_INITIAL,
        )
        lifecycle_id = created["lifecycle"]["lifecycle_id"]
        self.assertEqual(created["lifecycle"]["status"], "awaiting_first_confirmation")
        self.assertEqual(len(created["forecasts"]), 1)
        replay = await self.service.create_lifecycle(
            _request(), (), models=(_model(),), at=_TARGET
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(replay["forecasts"]), 1)
        initial_id = created["forecasts"][0]["forecast_id"]

        first_snapshot = await self._snapshot(_FIRST, ConfirmationStage.FIRST)
        first = await self.service.confirm(
            lifecycle_id,
            ConfirmationStage.FIRST,
            (_prediction(first_snapshot, predicted_return=0.02),),
            models=(_model(),),
            snapshot=first_snapshot,
            at=_FIRST,
        )
        self.assertEqual(first["lifecycle"]["status"], "awaiting_final_confirmation")
        self.assertEqual(len(first["forecasts"]), 2)
        self.assertEqual(first["forecasts"][0]["forecast_id"], initial_id)

        final_snapshot = await self._snapshot(_FINAL, ConfirmationStage.FINAL)
        final = await self.service.confirm(
            lifecycle_id,
            ConfirmationStage.FINAL,
            (_prediction(final_snapshot, predicted_return=0.03),),
            models=(_model(),),
            snapshot=final_snapshot,
            at=_FINAL,
        )
        self.assertEqual(final["lifecycle"]["status"], "confirmed")
        self.assertEqual(len(final["forecasts"]), 3)
        self.assertEqual(final["forecasts"][0]["forecast_id"], initial_id)
        self.assertEqual(final["forecasts"][-1]["forecast_validity"], "confirmed")
        self.assertIn(
            "expected_return",
            next(stage for stage in final["stages"] if stage["stage"] == "t_minus_1")[
                "changed_fields"
            ],
        )

        evidence = await self.service.decision_evidence(lifecycle_id)
        self.assertTrue(evidence["evidence"]["eligible"])
        self.assertFalse(evidence["evidence"]["authorizes_action"])
        self.assertFalse(evidence["evidence"]["is_price_prediction"])
        self.assertFalse(evidence["evidence"]["is_investment_advice"])
        self.assertFalse(evidence["evidence"]["is_trading_recommendation"])
        self.assertEqual(evidence["evidence"]["risk_authority"], "NM-08")
        self.assertEqual(evidence["evidence"]["execution_authority"], "NM-10")

    async def test_stale_supplied_snapshot_cannot_bypass_stage_freshness(self) -> None:
        initial = await self._snapshot(_INITIAL, ConfirmationStage.INITIAL)
        created = await self.service.create_lifecycle(
            _request(),
            (_prediction(initial, predicted_return=0.01),),
            models=(_model(),),
            snapshot=initial,
            at=_INITIAL,
        )
        lifecycle_id = created["lifecycle"]["lifecycle_id"]
        with self.assertRaises(PredictionDataError):
            await self.service.confirm(
                lifecycle_id,
                ConfirmationStage.FIRST,
                (),
                models=(_model(),),
                snapshot=initial,
                at=_FIRST,
            )
        fresh_timestamp_but_stale = replace(
            initial,
            source_observed_at=_FIRST - timedelta(seconds=30),
            received_at=_FIRST - timedelta(seconds=20),
            freshness=Freshness.STALE,
        )
        with self.assertRaises(PredictionDataError):
            await self.service.confirm(
                lifecycle_id,
                ConfirmationStage.FIRST,
                (),
                models=(_model(),),
                snapshot=fresh_timestamp_but_stale,
                at=_FIRST,
            )
        self.assertIsNone(
            self.store.stage_for(lifecycle_id, ConfirmationStage.FIRST)
        )

    async def test_missing_data_skips_t5_and_invalidates_t1_without_rewriting(self) -> None:
        initial = await self._snapshot(_INITIAL, ConfirmationStage.INITIAL)
        created = await self.service.create_lifecycle(
            _request(),
            (_prediction(initial, predicted_return=0.01),),
            models=(_model(),),
            snapshot=initial,
            at=_INITIAL,
        )
        lifecycle_id = created["lifecycle"]["lifecycle_id"]
        initial_id = created["forecasts"][0]["forecast_id"]

        self.fake.observation = None
        first = await self.service.confirm(
            lifecycle_id,
            ConfirmationStage.FIRST,
            (_prediction(initial, predicted_return=0.02, generated_at=_FIRST),),
            models=(_model(),),
            at=_FIRST,
        )
        self.assertEqual(first["lifecycle"]["status"], "awaiting_final_confirmation")
        self.assertEqual(first["stages"][-1]["status"], "skipped")
        self.assertEqual(first["forecasts"][0]["forecast_id"], initial_id)

        final = await self.service.confirm(
            lifecycle_id,
            ConfirmationStage.FINAL,
            (_prediction(initial, predicted_return=0.02, generated_at=_FINAL),),
            models=(_model(),),
            at=_FINAL,
        )
        self.assertEqual(final["lifecycle"]["status"], "unconfirmed")
        self.assertEqual(final["stages"][-1]["status"], "invalid")
        self.assertEqual(len(final["forecasts"]), 1)
        with self.assertRaises(Exception):
            await self.service.decision_evidence(lifecycle_id)

    async def test_evaluation_compares_every_preserved_forecast(self) -> None:
        initial = await self._snapshot(_INITIAL, ConfirmationStage.INITIAL)
        created = await self.service.create_lifecycle(
            _request(),
            (_prediction(initial, predicted_return=0.01),),
            models=(_model(),),
            snapshot=initial,
            at=_INITIAL,
        )
        lifecycle_id = created["lifecycle"]["lifecycle_id"]
        first_snapshot = await self._snapshot(_FIRST, ConfirmationStage.FIRST)
        await self.service.confirm(
            lifecycle_id,
            ConfirmationStage.FIRST,
            (_prediction(first_snapshot, predicted_return=0.02),),
            models=(_model(),),
            snapshot=first_snapshot,
            at=_FIRST,
        )
        final_snapshot = await self._snapshot(_FINAL, ConfirmationStage.FINAL)
        await self.service.confirm(
            lifecycle_id,
            ConfirmationStage.FINAL,
            (_prediction(final_snapshot, predicted_return=0.03),),
            models=(_model(),),
            snapshot=final_snapshot,
            at=_FINAL,
        )
        evaluated = await self.service.evaluate(
            lifecycle_id, "2470.00", at=_TARGET
        )
        self.assertEqual(len(evaluated["evaluations"]), 3)
        self.assertEqual(evaluated["lifecycle"]["status"], "evaluated")
        self.assertTrue(all(item["actual_price"] == "2470.00" for item in evaluated["evaluations"]))
        self.assertTrue(
            all(item["actual_direction"] == "up" for item in evaluated["evaluations"])
        )
        again = await self.service.evaluate(
            lifecycle_id, "2470.00", at=_TARGET + timedelta(minutes=1)
        )
        self.assertEqual(len(again["evaluations"]), 3)
        with self.assertRaises(PredictionStateError):
            await self.service.evaluate(
                lifecycle_id, "2471.00", at=_TARGET + timedelta(minutes=2)
            )
        self.assertTrue(
            all(item["actual_price"] == "2470.00" for item in again["evaluations"])
        )

    async def test_store_rejects_rewriting_a_forecast(self) -> None:
        initial = await self._snapshot(_INITIAL, ConfirmationStage.INITIAL)
        created = await self.service.create_lifecycle(
            _request(),
            (_prediction(initial, predicted_return=0.01),),
            models=(_model(),),
            snapshot=initial,
            at=_INITIAL,
        )
        forecast_id = created["forecasts"][0]["forecast_id"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE prediction_forecasts SET payload = '{}' WHERE forecast_id = ?",
                (forecast_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "DELETE FROM prediction_forecasts WHERE forecast_id = ?",
                (forecast_id,),
            )


class ArchitectureAndServiceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_seven_domains_sixteen_phases_and_authority_boundaries(self) -> None:
        self.assertEqual(len(PREDICTION_ROADMAP), 16)
        self.assertEqual(len(PREDICTION_DOMAIN_BOUNDARIES), 7)
        self.assertEqual(validate_prediction_architecture(), ())
        self.assertEqual(validate_architecture(), ())
        self.assertGreaterEqual(len(CAPABILITIES), 51)
        self.assertGreaterEqual(len(IMPLEMENTED_EVENTS), 176)

    async def test_existing_service_exposes_prediction_contracts_and_real_market_data(self) -> None:
        with TemporaryDirectory() as directory:
            service = NovaMarketService(data_dir=directory)
            try:
                await service.initialize()
                status = service.status()
                contracts = service.contracts()
            finally:
                await service.shutdown()
        self.assertEqual(status["prediction"]["phase"], "PI-01")
        self.assertTrue(status["prediction"]["initialised"])
        self.assertEqual(len(contracts["prediction_roadmap"]), 16)
        self.assertEqual(len(contracts["prediction_domains"]), 7)
        self.assertEqual(contracts["prediction"]["authorities"]["risk"], "NM-08")
        self.assertEqual(contracts["prediction"]["authorities"]["execution"], "NM-10")

    async def test_real_engine_output_can_be_consumed_by_the_gateway(self) -> None:
        with TemporaryDirectory() as directory:
            service = NovaMarketService(data_dir=directory)
            try:
                await service.initialize()
                engine = service._market_engine
                store = service._market_store
                assert engine is not None and store is not None
                instrument = _instrument()
                store.upsert_instrument(instrument)
                now = datetime.now(UTC)
                observation = _observation("2450.50", now - timedelta(seconds=10))
                store.append_observation(observation)
                request = PredictionRequest(
                    request_id="req-real",
                    instrument_id=instrument.instrument_id,
                    exchange=instrument.exchange,
                    horizon=ForecastHorizon.MINUTES_20,
                    target_at=now + timedelta(minutes=20),
                    created_at=now,
                )
                snapshot = service.prediction_service().data_snapshot(
                    request, at=now
                )
                self.assertTrue(snapshot.usable, snapshot.reasons)
                self.assertEqual(snapshot.reference_price, Decimal("2450.50"))
                self.assertEqual(snapshot.source_observed_at, observation.observed_at)
            finally:
                await service.shutdown()
