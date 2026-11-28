"""Focused PI-09 forecast-scheduler verification.

These tests use a controlled clock and deterministic observations.  They do
not claim live-market execution.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

import pytest

from novacontrol.novamarket.marketdata.calendar import calendar_by_mic
from novacontrol.novamarket.prediction.models import ForecastOutput
from novacontrol.novamarket.prediction.service import PredictionIntelligenceService
from novacontrol.novamarket.prediction.store import PredictionStore
from novacontrol.novamarket.scheduling.models import (
    CheckpointKind,
    CheckpointState,
    CycleState,
    SchedulerConfig,
)
from novacontrol.novamarket.scheduling.schedule import ScheduleValidationError, build_cycle
from novacontrol.novamarket.scheduling.service import ForecastScheduler
from novacontrol.novamarket.service import NovaMarketService

INSTRUMENT = "XNSE:RELIANCE"
EXCHANGE = "XNSE"
T0 = datetime(2026, 11, 16, 3, 45, tzinfo=UTC)
TARGET = datetime(2026, 11, 16, 4, 5, tzinfo=UTC)


class FakeGateway:
    def __init__(self) -> None:
        self.now = T0
        self.price = Decimal("100")
        self.available = True
        self.reason = ""
        self.freshness = "fresh"
        self.seen: list[datetime | None] = []

    async def checkpoint(self, instrument_id: str, **kwargs: Any) -> dict[str, Any]:
        self.seen.append(kwargs.get("previous_observed_at"))
        if not self.available:
            return {
                "available": False,
                "reasons": (self.reason or "no fresh observation",),
                "observation": None,
                "quality": "unavailable",
                "freshness": "missing",
            }
        return {
            "available": True,
            "freshness": "fresh",
            "quality": "good",
            "resolution": "1m",
            "validation": {"usable": True},
            "reasons": (),
            "observation": {
                "instrument_id": instrument_id,
                "instrument": instrument_id,
                "price": format(self.price, "f"),
                "observed_at": self.now.isoformat(),
                "provider": "deterministic-test",
                "freshness": self.freshness,
                "quality": "good",
                "resolution": "1m",
            },
        }


class FakeEnsemble:
    def __init__(self, gateway: FakeGateway, target: datetime) -> None:
        self.gateway = gateway
        self.target = target
        self.returns = [0.01, 0.02, 0.015]
        self.calls = 0
        self.abstain = False
        self.failures = 0
        self.kwargs: list[dict[str, Any]] = []
        self.event_metadata: dict[str, Any] = {}

    async def generate(self, instrument_id: str, horizon: str, *, as_of: datetime, **kwargs: Any) -> dict[str, Any]:
        self.kwargs.append(dict(kwargs))
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("deterministic ensemble outage")
        if self.abstain:
            return {
                "ok": True,
                "status": "abstained",
                "engine": "ensemble",
                "forecast": None,
                "reasons": ["not enough eligible models"],
                "ensemble": {"phase": "PI-08"},
            }
        pred_return = self.returns[min(self.calls, len(self.returns) - 1)]
        self.calls += 1
        ref = self.gateway.price
        output = ForecastOutput(
            forecast_id=f"pfc_native_{self.calls}",
            lifecycle_id="native-ensemble",
            lifecycle_stage="t_minus_20",
            instrument_id=instrument_id,
            exchange=EXCHANGE,
            currency="INR",
            reference_price=ref,
            reference_at=as_of,
            predicted_price=ref * (Decimal("1") + Decimal(str(pred_return))),
            predicted_return=pred_return,
            direction="up",
            lower_return=-0.02,
            upper_return=0.04,
            lower_price=ref * Decimal("0.98"),
            upper_price=ref * Decimal("1.04"),
            uncertainty="medium",
            uncertainty_width=0.06,
            p_up=None,
            p_down=None,
            p_neutral=None,
            probability_status="unavailable",
            calibration_status="uncalibrated",
            confidence=None,
            ensemble_id="ens_test",
            model_id="ensemble",
            model_version="pi08-test",
            member_versions=("m1@v1", "m2@v1"),
            forecast_horizon=horizon,
            target_at=self.target,
            generated_at=as_of,
            data_observed_at=as_of,
            data_received_at=as_of,
            data_resolution="1m",
            data_freshness="fresh",
            data_quality="valid",
            data_snapshot_id="snap-test",
            validity="valid",
            validity_conditions=(),
            disagreement=0.0,
            digest="a" * 64,
            metadata={"phase": "PI-08", **self.event_metadata},
            feature_set_version="features-test",
            dataset_version="dataset-test",
        )
        body = output.to_dict()
        body.update(
            {
                "ok": True,
                "status": "assembled",
                "engine": "ensemble",
                "ensemble_id": "ens_test",
                "ensemble": {"phase": "PI-08", "calibration": {"status": "uncalibrated"}},
            }
        )
        return body


def make_scheduler(
    *, now: datetime = T0, target: datetime = TARGET, config: SchedulerConfig | None = None
) -> tuple[ForecastScheduler, PredictionStore, FakeGateway, FakeEnsemble]:
    store = PredictionStore(":memory:")
    prediction = PredictionIntelligenceService(store=store)
    gateway = FakeGateway()
    gateway.now = now
    ensemble = FakeEnsemble(gateway, target)
    scheduler = ForecastScheduler(
        prediction=prediction,
        ensemble=ensemble,
        gateway=gateway,
        clock=lambda: gateway.now,
        config=config or SchedulerConfig(),
    )
    scheduler.create_cycle(
        instrument_id=INSTRUMENT,
        exchange=EXCHANGE,
        horizon="20m",
        target_at=target,
        now=now,
    )
    return scheduler, store, gateway, ensemble


@pytest.mark.asyncio
async def test_pi10_context_provider_is_forwarded_to_the_ensemble() -> None:
    scheduler, store, _, ensemble = make_scheduler()
    seen: list[tuple[str, Any, datetime]] = []

    def provider(instrument_id: str, horizon: Any, now: datetime) -> dict[str, Any]:
        seen.append((instrument_id, horizon, now))
        return {
            "fundamentals": ("snapshot-fixture",),
            "news_events": ("verified-event-fixture",),
            "forecast_metadata": {"pi10": {"phase": "PI-10"}},
            # A provider cannot replace the scheduler's identity/clock fields.
            "as_of": datetime(2000, 1, 1, tzinfo=UTC),
        }

    scheduler._context_provider = provider
    await scheduler.tick(now=T0, limit=1)
    assert seen and seen[0][0] == INSTRUMENT and seen[0][2] == T0
    call = ensemble.kwargs[-1]
    assert call["fundamentals"] == ("snapshot-fixture",)
    assert call["news_events"] == ("verified-event-fixture",)
    assert call["forecast_metadata"] == {"pi10": {"phase": "PI-10"}}
    assert "as_of" not in call
    assert store.checkpoints(store.forecast_cycles()[0].cycle_id)[0].status is CheckpointState.COMPLETED


@pytest.mark.asyncio
async def test_pi10_context_failure_does_not_disable_the_lifecycle() -> None:
    scheduler, store, _, ensemble = make_scheduler()

    def provider(instrument_id: str, horizon: Any, now: datetime) -> dict[str, Any]:
        raise RuntimeError("optional context unavailable")

    scheduler._context_provider = provider
    await scheduler.tick(now=T0, limit=1)
    assert store.checkpoints(store.forecast_cycles()[0].cycle_id)[0].status is CheckpointState.COMPLETED
    assert ensemble.calls == 1
    assert "optional context unavailable" in scheduler.status()["last_error"]


@pytest.mark.asyncio
async def test_t20_t5_t1_and_outcome_are_one_stable_cycle() -> None:
    scheduler, store, gateway, _ = make_scheduler()
    cycle = store.forecast_cycles()[0]
    assert cycle.target_at == TARGET
    assert cycle.initial_at == T0
    assert cycle.first_confirmation_at == TARGET - timedelta(minutes=5)
    assert cycle.final_confirmation_at == TARGET - timedelta(minutes=1)

    await scheduler.tick(now=T0)
    gateway.now = TARGET - timedelta(minutes=5)
    gateway.price = Decimal("101")
    await scheduler.tick(now=gateway.now)
    gateway.now = TARGET - timedelta(minutes=1)
    gateway.price = Decimal("102")
    await scheduler.tick(now=gateway.now)
    gateway.now = TARGET
    gateway.price = Decimal("103")
    await scheduler.tick(now=gateway.now)

    forecasts = store.forecasts(cycle.cycle_id)
    assert [item.lifecycle_stage.value for item in forecasts] == ["t_minus_20", "t_minus_5", "t_minus_1"]
    assert len({item.lifecycle_id for item in forecasts}) == 1
    assert len(store.outcomes(cycle.cycle_id)) == 3
    assert store.get_forecast_cycle(cycle.cycle_id).state is CycleState.EVALUATED
    assert len(store.evaluations(cycle.cycle_id)) == 3
    # Every version stays independently identifiable and immutable.
    assert len({item.forecast_id for item in forecasts}) == 3
    assert store.get_lifecycle(cycle.cycle_id).status.value == "evaluated"
    stored_cycle = store.get_forecast_cycle(cycle.cycle_id)
    assert stored_cycle.initial_forecast_id == forecasts[0].forecast_id
    assert stored_cycle.first_forecast_id == forecasts[1].forecast_id
    assert stored_cycle.final_forecast_id == forecasts[2].forecast_id
    assert stored_cycle.outcome_id
    accuracy = scheduler.accuracy()
    assert accuracy["sample_size"] == 3
    assert set(accuracy["by_stage"]) == {"t_minus_20", "t_minus_5", "t_minus_1"}
    assert accuracy["by_stage"]["t_minus_20"]["sample_size"] == 1
    assert accuracy["reliable"] is False


@pytest.mark.asyncio
async def test_checkpoint_accepts_the_gateway_nested_source_timestamp() -> None:
    scheduler, store, gateway, _ = make_scheduler()
    flat = gateway.checkpoint

    async def checkpoint(instrument_id: str, **kwargs: Any) -> dict[str, Any]:
        data = await flat(instrument_id, **kwargs)
        observation = data["observation"]
        observation["timestamps"] = {
            "source_observed_at": observation.pop("observed_at")
        }
        return data

    gateway.checkpoint = checkpoint
    await scheduler.tick(now=T0, limit=1)
    cycle = store.forecast_cycles()[0]
    assert store.checkpoints(cycle.cycle_id)[0].status is CheckpointState.COMPLETED
    assert store.forecasts(cycle.cycle_id)[0].data_observed_at == T0


@pytest.mark.asyncio
async def test_revision_comparison_is_not_accuracy() -> None:
    scheduler, store, gateway, _ = make_scheduler()
    await scheduler.tick(now=T0)
    gateway.now = TARGET - timedelta(minutes=5)
    gateway.price = Decimal("101")
    await scheduler.tick(now=gateway.now)
    first = store.checkpoints(store.forecast_cycles()[0].cycle_id)[1]
    assert first.revision["classification"] in {"strengthened", "weakened", "stable", "uncertain"}
    assert first.revision["is_forecast_revision"] is True
    assert "accuracy" not in first.revision


@pytest.mark.asyncio
async def test_missing_observation_is_skipped_and_never_fabricated() -> None:
    scheduler, store, gateway, ensemble = make_scheduler()
    gateway.available = False
    gateway.reason = "provider returned no new observation"
    await scheduler.tick(now=T0)
    cycle = store.forecast_cycles()[0]
    checkpoints = store.checkpoints(cycle.cycle_id)
    assert checkpoints[0].status is CheckpointState.SKIPPED
    assert checkpoints[0].forecast_id == ""
    assert store.forecasts(cycle.cycle_id) == []
    assert "provider returned no new observation" in " ".join(checkpoints[0].reasons)
    assert ensemble.calls == 0


@pytest.mark.asyncio
async def test_final_observation_failure_is_dead_letter_and_unconfirmed() -> None:
    scheduler, store, gateway, _ = make_scheduler(config=SchedulerConfig(max_attempts=1))
    gateway.available = False
    gateway.reason = "provider returned no new observation"
    await scheduler.tick(now=T0)
    gateway.now = TARGET - timedelta(minutes=5)
    await scheduler.tick(now=gateway.now)
    gateway.now = TARGET - timedelta(minutes=1)
    await scheduler.tick(now=gateway.now)
    cycle = store.forecast_cycles()[0]
    final = store.checkpoints(cycle.cycle_id)[2]
    assert final.status is CheckpointState.DEAD_LETTER
    assert store.get_forecast_cycle(cycle.cycle_id).state is CycleState.UNCONFIRMED
    assert store.get_lifecycle(cycle.cycle_id).status.value == "unconfirmed"


@pytest.mark.asyncio
async def test_abstention_is_preserved_without_a_direction() -> None:
    scheduler, store, gateway, ensemble = make_scheduler()
    ensemble.abstain = True
    await scheduler.tick(now=T0)
    cycle = store.forecast_cycles()[0]
    checkpoint = store.checkpoints(cycle.cycle_id)[0]
    assert checkpoint.status is CheckpointState.ABSTAINED
    assert store.forecasts(cycle.cycle_id) == []
    assert "not enough eligible models" in " ".join(checkpoint.reasons)


@pytest.mark.asyncio
async def test_late_initial_keeps_real_execution_timestamp() -> None:
    scheduler, store, gateway, _ = make_scheduler()
    gateway.now = T0 + timedelta(minutes=5)
    gateway.price = Decimal("100.5")
    await scheduler.tick(now=gateway.now, limit=1)
    checkpoint = store.checkpoints(store.forecast_cycles()[0].cycle_id)[0]
    assert checkpoint.status is CheckpointState.COMPLETED
    assert checkpoint.executed_at == gateway.now
    assert checkpoint.executed_at > checkpoint.scheduled_at
    assert checkpoint.to_dict()["late"] is True
    cycle = store.get_forecast_cycle(checkpoint.cycle_id)
    assert cycle.initial_at < checkpoint.executed_at


@pytest.mark.asyncio
async def test_target_passed_checkpoints_are_not_executed_as_on_time() -> None:
    scheduler, store, gateway, ensemble = make_scheduler()
    gateway.now = TARGET + timedelta(minutes=1)
    gateway.price = Decimal("104")
    await scheduler.tick(now=gateway.now, limit=4)
    cycle = store.get_forecast_cycle(store.forecast_cycles()[0].cycle_id)
    checkpoints = store.checkpoints(cycle.cycle_id)
    assert [item.status for item in checkpoints[:3]] == [CheckpointState.SKIPPED] * 3
    assert store.forecasts(cycle.cycle_id) == []
    assert ensemble.calls == 0
    assert cycle.state is CycleState.UNCONFIRMED


def test_cycle_identity_normalizes_case_and_replay_after_target() -> None:
    scheduler, store, _, _ = make_scheduler()
    existing = store.forecast_cycles()[0]
    replay = scheduler.create_cycle(
        instrument_id="xnse:reliance",
        exchange="xnse",
        horizon="20m",
        target_at=TARGET,
        now=TARGET + timedelta(minutes=1),
    )
    assert replay.cycle_id == existing.cycle_id
    assert replay.instrument_id == "XNSE:RELIANCE"


def test_calendar_and_session_validation_fail_closed() -> None:
    # The same instant expressed in exchange-local time maps to one UTC target.
    local_target = datetime(2026, 11, 16, 9, 35, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    local_cycle = build_cycle(
        instrument_id=INSTRUMENT,
        exchange=EXCHANGE,
        horizon="20m",
        now=T0,
        target_at=local_target,
    )
    assert local_cycle.target_at == TARGET
    with pytest.raises(ScheduleValidationError):
        build_cycle(
            instrument_id=INSTRUMENT,
            exchange="UNKNOWN",
            horizon="20m",
            now=T0,
            target_at=TARGET,
        )
    with pytest.raises(ScheduleValidationError):
        build_cycle(
            instrument_id=INSTRUMENT,
            exchange=EXCHANGE,
            horizon="20m",
            now=T0,
            target_at=datetime(2026, 11, 14, 4, 5, tzinfo=UTC),  # Saturday
        )
    with pytest.raises(ScheduleValidationError):
        build_cycle(
            instrument_id=INSTRUMENT,
            exchange=EXCHANGE,
            horizon="20m",
            now=T0,
            target_at=datetime(2026, 11, 16, 10, 30, tzinfo=UTC),  # after NSE close
        )


@pytest.mark.asyncio
async def test_forecast_version_is_immutable_and_file_backed_restart_keeps_it() -> None:
    with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = Path(tmp) / "prediction.db"
        first_store = PredictionStore(str(path))
        first_scheduler = ForecastScheduler(
            prediction=PredictionIntelligenceService(store=first_store),
            ensemble=FakeEnsemble(FakeGateway(), TARGET),
            gateway=FakeGateway(),
            clock=lambda: T0,
        )
        first_scheduler.create_cycle(
            instrument_id=INSTRUMENT,
            exchange=EXCHANGE,
            horizon="20m",
            target_at=TARGET,
            now=T0,
        )
        await first_scheduler.tick(now=T0, limit=1)
        cycle = first_store.forecast_cycles()[0]
        forecast = first_store.forecasts(cycle.cycle_id)[0]
        original = forecast.to_dict()
        duplicate = replace(
            forecast,
            predicted_price=forecast.lower_price + Decimal("0.01"),
        )
        assert first_store.save_forecast(duplicate) is False
        assert first_store.get_forecast(forecast.forecast_id).predicted_price == forecast.predicted_price

        first_store.close()
        reopened = PredictionStore(str(path))
        assert reopened.get_forecast_cycle(cycle.cycle_id) is not None
        assert reopened.get_forecast(forecast.forecast_id).to_dict() == original
        assert reopened.checkpoints(cycle.cycle_id)[0].status is CheckpointState.COMPLETED
        reopened.close()


def test_checkpoint_claim_is_atomic_and_leases_recover() -> None:
    scheduler, store, _, _ = make_scheduler()
    cycle = store.forecast_cycles()[0]
    checkpoint = store.checkpoints(cycle.cycle_id)[0]
    claimed = store.claim_checkpoint(
        checkpoint.checkpoint_id, owner="worker-a", now=T0, lease_seconds=30
    )
    assert claimed is not None
    assert store.claim_checkpoint(
        checkpoint.checkpoint_id, owner="worker-b", now=T0, lease_seconds=30
    ) is None
    assert store.recover_expired_checkpoint_leases(T0 + timedelta(seconds=31)) == 1
    recovered = store.get_checkpoint(checkpoint.checkpoint_id)
    assert recovered.status is CheckpointState.SCHEDULED
    assert recovered.lease_owner == ""


@pytest.mark.asyncio
async def test_restart_recovery_reuses_durable_checkpoints() -> None:
    scheduler, store, gateway, ensemble = make_scheduler()
    scheduler2 = ForecastScheduler(
        prediction=PredictionIntelligenceService(store=store),
        ensemble=ensemble,
        gateway=gateway,
        clock=lambda: gateway.now,
    )
    assert scheduler2.tick is not None
    await scheduler2.tick(now=T0, limit=1)
    cycle = store.forecast_cycles()[0]
    assert store.checkpoints(cycle.cycle_id)[0].status is CheckpointState.COMPLETED
    assert len(store.forecasts(cycle.cycle_id)) == 1


@pytest.mark.asyncio
async def test_duplicate_tick_does_not_create_duplicate_versions() -> None:
    scheduler, store, _, _ = make_scheduler()
    await scheduler.tick(now=T0, limit=1)
    await scheduler.tick(now=T0, limit=1)
    assert len(store.forecasts(store.forecast_cycles()[0].cycle_id)) == 1


@pytest.mark.asyncio
async def test_outcome_pending_when_actual_observation_is_unavailable() -> None:
    scheduler, store, gateway, _ = make_scheduler()
    gateway.available = False
    await scheduler.tick(now=TARGET, limit=4)
    cycle = store.forecast_cycles()[0]
    outcome_checkpoint = [item for item in store.checkpoints(cycle.cycle_id) if item.kind is CheckpointKind.OUTCOME][0]
    assert outcome_checkpoint.status in {CheckpointState.PENDING, CheckpointState.DEAD_LETTER}
    assert outcome_checkpoint.payload.get("last_attempt_at")


def test_accuracy_reports_sample_size_and_small_sample_caveat() -> None:
    scheduler, _, _, _ = make_scheduler()
    body = scheduler.accuracy()
    assert body["sample_size"] == 0
    assert body["reliable"] is False
    assert "small-sample" in body["reliability_note"]
    assert body["financial_authority"] == "none"
    filtered = scheduler.accuracy(stage="t_minus_20")
    assert filtered["stage"] == "t_minus_20"
    assert filtered["by_stage"]["t_minus_1"]["sample_size"] == 0


def test_horizon_policies_are_explicit_for_intraday_and_swing() -> None:
    from novacontrol.novamarket.prediction.models import ForecastHorizon
    from novacontrol.novamarket.scheduling.models import HORIZON_SCHEDULE_POLICIES

    assert HORIZON_SCHEDULE_POLICIES[next(iter(HORIZON_SCHEDULE_POLICIES))].to_dict()
    assert HORIZON_SCHEDULE_POLICIES[next(iter(HORIZON_SCHEDULE_POLICIES))].target_rule
    assert len(HORIZON_SCHEDULE_POLICIES) == 10
    one_minute = HORIZON_SCHEDULE_POLICIES[ForecastHorizon.MINUTES_1]
    assert one_minute.initial_lead_seconds == 60
    assert one_minute.first_lead_seconds == 30
    assert one_minute.final_lead_seconds == 5


@pytest.mark.asyncio
async def test_stale_source_timestamp_is_rejected() -> None:
    scheduler, store, gateway, ensemble = make_scheduler()
    gateway.freshness = "stale"
    await scheduler.tick(now=T0, limit=1)
    checkpoint = store.checkpoints(store.forecast_cycles()[0].cycle_id)[0]
    assert checkpoint.status is CheckpointState.SKIPPED
    assert "freshness stale" in " ".join(checkpoint.reasons)
    assert ensemble.calls == 0


@pytest.mark.asyncio
async def test_retry_preserves_cycle_and_checkpoint_identity() -> None:
    scheduler, store, gateway, ensemble = make_scheduler()
    ensemble.failures = 1
    first = await scheduler.tick(now=T0, limit=1)
    cycle = store.forecast_cycles()[0]
    checkpoint = store.checkpoints(cycle.cycle_id)[0]
    assert checkpoint.attempts == 1
    assert checkpoint.status is CheckpointState.SCHEDULED
    assert checkpoint.checkpoint_id in str(first["executed"])
    gateway.now = T0 + timedelta(seconds=31)
    await scheduler.tick(now=gateway.now, limit=1)
    recovered = store.checkpoints(cycle.cycle_id)[0]
    assert recovered.status is CheckpointState.COMPLETED
    assert recovered.checkpoint_id == checkpoint.checkpoint_id
    assert len(store.forecasts(cycle.cycle_id)) == 1


@pytest.mark.asyncio
async def test_outcome_rejects_an_observation_outside_the_target_session() -> None:
    scheduler, store, gateway, _ = make_scheduler()
    await scheduler.tick(now=T0)
    gateway.now = TARGET - timedelta(minutes=5)
    await scheduler.tick(now=gateway.now)
    gateway.now = TARGET - timedelta(minutes=1)
    await scheduler.tick(now=gateway.now)
    gateway.now = TARGET + timedelta(hours=7)
    await scheduler.tick(now=gateway.now)
    cycle = store.forecast_cycles()[0]
    outcome = store.checkpoints(cycle.cycle_id)[3]
    assert outcome.status is CheckpointState.PENDING
    assert "outside" in " ".join(outcome.reasons)


@pytest.mark.asyncio
async def test_outcome_pending_reconciles_on_a_later_tick() -> None:
    scheduler, store, gateway, _ = make_scheduler()
    gateway.available = False
    await scheduler.tick(now=TARGET, limit=4)
    cycle = store.forecasts(store.forecast_cycles()[0].cycle_id)
    assert cycle == []
    outcome = [item for item in store.checkpoints(store.forecast_cycles()[0].cycle_id) if item.kind is CheckpointKind.OUTCOME][0]
    assert outcome.status is CheckpointState.PENDING
    gateway.available = True
    gateway.now = TARGET + timedelta(seconds=31)
    gateway.price = Decimal("104")
    await scheduler.tick(now=gateway.now, limit=1)
    reconciled = [item for item in store.checkpoints(store.forecast_cycles()[0].cycle_id) if item.kind is CheckpointKind.OUTCOME][0]
    assert reconciled.status is CheckpointState.SKIPPED  # no version exists to evaluate
    assert reconciled.executed_at is not None


def test_one_day_policy_creates_the_initial_forecast_with_the_cycle() -> None:
    cycle = build_cycle(
        instrument_id=INSTRUMENT,
        exchange=EXCHANGE,
        horizon="1d",
        now=T0,
        target_at=datetime(2026, 11, 17, 10, 0, tzinfo=UTC),
    )
    assert cycle.target_rule == "swing_sessions"
    assert cycle.initial_at == T0
    assert cycle.first_confirmation_at == datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
    assert cycle.final_confirmation_at == datetime(2026, 11, 17, 9, 59, tzinfo=UTC)


def test_automatic_cycle_planner_prioritizes_caps_throttles_and_isolates_errors() -> None:
    store = PredictionStore(":memory:")
    gateway = FakeGateway()
    scheduler = ForecastScheduler(
        prediction=PredictionIntelligenceService(store=store),
        ensemble=FakeEnsemble(gateway, TARGET),
        gateway=gateway,
        clock=lambda: T0,
        config=SchedulerConfig(auto_schedule_limit=5),
    )
    scheduler.create_cycle(
        instrument_id="XNSE:WATCH2",
        exchange="XNSE",
        horizon="20m",
        now=T0,
    )

    clock = [T0]
    service = NovaMarketService(clock=lambda: clock[0])
    service._held_instruments = lambda: ["XNSE:HELD1", "XNSE:HELD2"]  # type: ignore[method-assign]
    service.watchlists = lambda: {  # type: ignore[method-assign]
        "watchlists": [{"watchlist_id": "wl_api09"}]
    }
    service.watchlist = lambda watchlist_id, *, limit=100: {  # type: ignore[method-assign]
        "members": [
            {"instrument_id": "XNSE:WATCH1"},
            {"instrument_id": "XNSE:WATCH2"},
            {"instrument_id": "XNSE:HELD1"},
        ]
    }
    service.market_instruments = lambda **kwargs: {  # type: ignore[method-assign]
        "items": [
            {"instrument_id": "XNSE:MARKET1"},
            {"instrument_id": "XNSE:MARKET2"},
        ]
    }

    original_create = scheduler.create_cycle
    create_calls: list[str] = []

    def create_cycle(**kwargs: Any) -> Any:
        instrument = str(kwargs["instrument_id"])
        create_calls.append(instrument)
        if instrument == "XNSE:WATCH1":
            raise RuntimeError("deterministic creation failure")
        return original_create(**kwargs)

    with mock.patch.object(scheduler, "create_cycle", create_cycle):
        first = service._auto_plan_from_scheduler(scheduler)

        assert first["considered"] == 6
        assert first["limit"] == 5
        assert first["selected"] == 5
        assert first["sources"] == {
            "held": ["XNSE:HELD1", "XNSE:HELD2"],
            "watchlists": ["XNSE:WATCH1", "XNSE:WATCH2"],
            "market": ["XNSE:MARKET1", "XNSE:MARKET2"],
        }
        assert [row["instrument_id"] for row in first["created"]] == [
            "XNSE:HELD1",
            "XNSE:HELD2",
            "XNSE:MARKET1",
        ]
        assert first["skipped"] == [
            {"instrument_id": "XNSE:WATCH2", "reason": "active cycle exists"}
        ]
        assert first["errors"] == [
            {
                "instrument_id": "XNSE:WATCH1",
                "error": "RuntimeError: deterministic creation failure",
            }
        ]
        assert create_calls == [
            "XNSE:HELD1",
            "XNSE:HELD2",
            "XNSE:WATCH1",
            "XNSE:MARKET1",
        ]
        assert store.forecast_cycles(instrument_id="XNSE:MARKET2", horizon="20m") == []
        assert len(store.forecast_cycles(instrument_id="XNSE:WATCH2", horizon="20m")) == 1

        clock[0] = T0 + timedelta(seconds=59)
        before_throttle = len(create_calls)
        second = service._auto_plan_from_scheduler(scheduler)
        assert second is first
        assert len(create_calls) == before_throttle

    store.close()


def test_the_cycle_list_puts_the_newest_lifecycle_first() -> None:
    """A cycle scheduled now must lead its group, not sort behind older cycles.

    The store's default order stays chronological for the scheduler's own
    scans; the operator-facing list asks for ``outstanding_first`` so a
    lifecycle targeting tomorrow is never pushed off the page by its limit.
    Both cycles here are still scheduled, so creation order decides.
    """
    store = PredictionStore(":memory:")
    try:
        older = build_cycle(
            instrument_id=INSTRUMENT,
            exchange=EXCHANGE,
            horizon="20m",
            now=T0,
            target_at=TARGET,
        )
        newer = build_cycle(
            instrument_id="XNSE:TCS",
            exchange=EXCHANGE,
            horizon="20m",
            now=T0 + timedelta(minutes=1),
            target_at=TARGET,
        )
        assert store.save_forecast_cycle(older) is True
        assert store.save_forecast_cycle(newer) is True
        # Both target the same time, so only creation order separates them.
        assert [item.cycle_id for item in store.forecast_cycles(limit=1)] == [
            older.cycle_id
        ]
        assert [
            item.cycle_id
            for item in store.forecast_cycles(limit=1, outstanding_first=True)
        ] == [newer.cycle_id]
        assert [
            item.cycle_id for item in store.forecast_cycles(outstanding_first=True)
        ] == [newer.cycle_id, older.cycle_id]
    finally:
        store.close()


def test_the_cycle_list_surfaces_outstanding_cycles_before_finished_history() -> None:
    """An older cycle that still has work to do leads newer finished cycles.

    Ordering only by creation buried a still-scheduled lifecycle under today's
    evaluated history once the limit was reached.
    """
    store = PredictionStore(":memory:")
    try:
        outstanding = build_cycle(
            instrument_id=INSTRUMENT,
            exchange=EXCHANGE,
            horizon="20m",
            now=T0,
            target_at=TARGET,
        )
        historical = replace(
            build_cycle(
                instrument_id="XNSE:TCS",
                exchange=EXCHANGE,
                horizon="20m",
                now=T0 + timedelta(minutes=1),
                target_at=TARGET,
            ),
            state=CycleState.EVALUATED,
        )
        assert store.save_forecast_cycle(outstanding) is True
        assert store.save_forecast_cycle(historical) is True
        assert [
            item.cycle_id for item in store.forecast_cycles(outstanding_first=True)
        ] == [outstanding.cycle_id, historical.cycle_id]
        # The limit keeps the outstanding cycle on the page instead of spending
        # the only slot on the newer, finished one.
        assert [
            item.cycle_id
            for item in store.forecast_cycles(limit=1, outstanding_first=True)
        ] == [outstanding.cycle_id]
        # Two outstanding cycles are still newest-created first.
        newer_outstanding = build_cycle(
            instrument_id="XNSE:INFY",
            exchange=EXCHANGE,
            horizon="20m",
            now=T0 + timedelta(minutes=2),
            target_at=TARGET,
        )
        assert store.save_forecast_cycle(newer_outstanding) is True
        ordered = store.forecast_cycles(outstanding_first=True)
        assert [item.cycle_id for item in ordered] == [
            newer_outstanding.cycle_id,
            outstanding.cycle_id,
            historical.cycle_id,
        ]
        # The scheduler's own scans keep the chronological default: same target,
        # so creation order decides there and history is never silently dropped.
        assert [item.cycle_id for item in store.forecast_cycles()] == [
            outstanding.cycle_id,
            historical.cycle_id,
            newer_outstanding.cycle_id,
        ]
    finally:
        store.close()


def test_api_exposes_cycle_scheduler_and_missed_surfaces() -> None:
    try:
        from fastapi.testclient import TestClient
    except Exception:  # pragma: no cover - optional dev dependency
        pytest.skip("httpx/TestClient is unavailable")
    from tempfile import TemporaryDirectory

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
                # The API uses the real scheduler clock, so the fixture target
                # must be the next weekday rather than a time that has already
                # passed when the suite runs.
                api_target = (datetime.now(UTC) + timedelta(days=1)).replace(
                    hour=4, minute=5, second=0, microsecond=0
                )
                exchange_calendar = calendar_by_mic(EXCHANGE)
                assert exchange_calendar is not None
                while not exchange_calendar.is_trading_day(api_target.date()):
                    api_target += timedelta(days=1)
                target_text = api_target.isoformat()
                created = client.post(
                    "/market/forecasts/cycles",
                    json={
                        "instrument_id": INSTRUMENT,
                        "horizon": "20m",
                        "target_at": target_text,
                    },
                )
                assert created.status_code == 200, created.text
                body = created.json()
                cycle_id = body["cycle"]["cycle_id"]
                assert len(body["checkpoints"]) == 4
                repeated = client.post(
                    "/market/forecasts/cycles",
                    json={
                        "instrument_id": INSTRUMENT,
                        "horizon": "20m",
                        "target_at": target_text,
                    },
                )
                assert repeated.status_code == 200
                assert repeated.json()["cycle"]["cycle_id"] == cycle_id
                invalid = client.post(
                    "/market/forecasts/cycles",
                    json={
                        "instrument_id": INSTRUMENT,
                        "horizon": "20m",
                        "target_at": (api_target + timedelta(hours=7)).isoformat(),
                    },
                )
                assert invalid.status_code == 422
                # A second lifecycle scheduled now shares the first one's target,
                # so only creation order can decide which one the list shows
                # first: the operator-facing list must lead with the newest.
                second = client.post(
                    "/market/forecasts/cycles",
                    json={
                        "instrument_id": "XNSE:TCS",
                        "horizon": "20m",
                        "target_at": target_text,
                    },
                )
                assert second.status_code == 200, second.text
                second_id = second.json()["cycle"]["cycle_id"]
                listed = client.get("/market/forecasts/cycles")
                assert listed.status_code == 200
                listed_ids = [item["cycle_id"] for item in listed.json()]
                assert cycle_id in listed_ids, listed_ids
                assert second_id in listed_ids, listed_ids
                assert listed_ids.index(second_id) < listed_ids.index(cycle_id)
                detail = client.get(f"/market/forecasts/cycles/{cycle_id}")
                assert detail.status_code == 200
                assert detail.json()["cycle"]["cycle_id"] == cycle_id
                scheduler = client.get("/market/forecasts/scheduler")
                assert scheduler.status_code == 200
                assert scheduler.json()["authority"]["forecast_only"] is True
                assert "auto_plan" in scheduler.json()
                missed = client.get("/market/forecasts/missed")
                assert missed.status_code == 200
                pending = client.get("/market/forecasts/evaluations/pending")
                assert pending.status_code == 200
                accuracy = client.get("/market/forecasts/accuracy")
                assert accuracy.status_code == 200


@pytest.mark.asyncio
async def test_pi09_persists_the_declared_event_feature_provenance() -> None:
    """A PI-09 forecast keeps the ensemble's event declaration, not a guess.

    The PI-24 comparison labels a forecast event-aware from its own metadata;
    this is the hand-off that put a real label on a lifecycle forecast.
    """

    scheduler, store, _, ensemble = make_scheduler()
    ensemble.event_metadata = {
        "event_aware": True,
        "event_ids": ["evt_earnings"],
        "event_feature_set_version": "pi04-feature-set-1.0",
        "event_feature_names": ["news_count_5d"],
        "event_aware_constituents": ["gradient"],
    }
    await scheduler.tick(now=T0, limit=1)
    cycle = store.forecast_cycles()[0]
    forecast = store.forecasts(cycle.cycle_id)[0]
    metadata = dict(forecast.metadata)
    assert metadata["event_aware"] is True
    assert metadata["event_ids"] == ["evt_earnings"]
    assert metadata["event_feature_set_version"] == "pi04-feature-set-1.0"
    assert metadata["pi09"]["cycle_id"] == cycle.cycle_id
    assert forecast.feature_set_version == "features-test"
