"""PI-09 durable forecast lifecycle scheduler.

The scheduler owns scheduling, checkpoint claims and lifecycle bookkeeping.  It
does not own market data, models, risk or execution: the PI-03 gateway supplies
observations, the PI-08 ensemble supplies forecasts, PI-01 stores the immutable
versions, and NM-08/NM-10 are never called from this module.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Mapping, Sequence

from novacontrol.core.events import EventType
from novacontrol.novamarket.contracts import ForecastNotFoundError
from novacontrol.novamarket.marketdata.calendar import calendar_by_mic
from novacontrol.novamarket.marketdata.models import DataInterval, Freshness
from novacontrol.novamarket.prediction.lifecycle import changed_fields
from novacontrol.novamarket.prediction.models import (
    HORIZON_SPECS,
    ConfirmationStage,
    Direction,
    ForecastHorizon,
    ForecastLifecycle,
    ForecastOutput,
    ForecastValidity,
    LifecycleStageRecord,
    LifecycleStatus,
    PredictionRequest,
    PredictionValidationError,
    StageStatus,
    direction_for_return,
    prediction_id,
)
from novacontrol.novamarket.prediction.service import PredictionIntelligenceService
from novacontrol.novamarket.prediction.store import PredictionStore
from novacontrol.novamarket.scheduling.models import (
    CheckpointKind,
    CheckpointState,
    CycleState,
    ForecastCheckpoint,
    ForecastCycle,
    ForecastOutcome,
    ForecastRevision,
    PI09_PHASE,
    RevisionClass,
    SCHEDULE_CONTRACT_VERSION,
    SchedulerConfig,
)
from novacontrol.novamarket.scheduling.schedule import (
    ScheduleValidationError,
    build_checkpoints,
    build_cycle,
    session_snapshot,
)


def _dt(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ScheduleValidationError(f"{name} is missing or malformed")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ScheduleValidationError(f"{name} must be timezone-aware")
    return moment.astimezone(UTC)


def _decimal(value: object, name: str) -> Decimal:
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ScheduleValidationError(f"{name} is not decimal") from exc
    if not number.is_finite() or number <= 0:
        raise ScheduleValidationError(f"{name} must be finite and positive")
    return number


class ForecastScheduler:
    """One durable lifecycle coordinator over the existing forecast stack."""

    def __init__(
        self,
        *,
        prediction: PredictionIntelligenceService,
        ensemble: Any,
        gateway: Any,
        clock: Callable[[], datetime] | None = None,
        publisher: Any | None = None,
        config: SchedulerConfig | None = None,
        owner: str = "",
        currency_resolver: Callable[[str], str] | None = None,
        regime_resolver: Callable[[str], str] | None = None,
        planner: Callable[["ForecastScheduler"], Mapping[str, Any]] | None = None,
        context_provider: Callable[
            [str, ForecastHorizon, datetime], Any
        ] | None = None,
    ) -> None:
        self.prediction = prediction
        self.store: PredictionStore = prediction.store
        self.ensemble = ensemble
        self.gateway = gateway
        self._clock = clock or (lambda: datetime.now(UTC))
        self.publisher = publisher
        self.config = config or SchedulerConfig()
        self.owner = owner or f"pi09-{uuid.uuid4().hex[:12]}"
        self._currency = currency_resolver or (lambda instrument: "")
        self._regime = regime_resolver or (lambda instrument: "unclassified")
        self._planner = planner
        self._context_provider = context_provider
        self._task: asyncio.Task[Any] | None = None
        self._tick_lock = asyncio.Lock()
        self._last_tick: datetime | None = None
        self._last_error = ""
        self._ticks = 0
        self._runs = 0
        self._failures = 0

    # -- scheduling ---------------------------------------------------------

    def create_cycle(
        self,
        *,
        instrument_id: str,
        exchange: str,
        horizon: ForecastHorizon | str,
        target_at: datetime | None = None,
        resolution: str | None = None,
        now: datetime | None = None,
    ) -> ForecastCycle:
        """Create (or return) one stable cycle and its durable checkpoints."""

        current = self._now(now)
        # Check the stable identity before target validation.  Repeating a
        # create request after T must return the existing cycle rather than
        # turning an idempotent replay into a "target in the past" error.
        if target_at is not None and target_at.tzinfo is not None and target_at.utcoffset() is not None:
            try:
                requested_horizon = ForecastHorizon(horizon)
            except ValueError:
                requested_horizon = None
            if requested_horizon is not None:
                candidate_id = prediction_id(
                    "pcyc_",
                    str(instrument_id).upper(),
                    str(exchange).upper(),
                    requested_horizon.value,
                    target_at.astimezone(UTC).isoformat(),
                )
                existing = self.store.get_forecast_cycle(candidate_id)
                if existing is not None:
                    self._save_cycle_children(existing)
                    return existing
        cycle = build_cycle(
            instrument_id=instrument_id,
            exchange=exchange,
            horizon=horizon,
            now=current,
            target_at=target_at,
            resolution=resolution,
            config=self.config,
        )
        existing = self.store.get_forecast_cycle(cycle.cycle_id)
        if existing is not None:
            # A crash between cycle insertion and checkpoint insertion must not
            # leave a permanently unscheduled cycle.
            self._save_cycle_children(existing)
            return existing
        request = PredictionRequest(
            request_id=cycle.request_id,
            instrument_id=cycle.instrument_id,
            exchange=cycle.exchange,
            horizon=cycle.horizon,
            target_at=cycle.target_at,
            created_at=cycle.created_at,
            actor="pi09-scheduler",
            notes="Scheduled forecast lifecycle; evidence only, never an order.",
            metadata={
                "phase": PI09_PHASE,
                "cycle_id": cycle.cycle_id,
                "target_rule": cycle.target_rule,
                "policy_version": cycle.policy_version,
                "schedule_contract_version": SCHEDULE_CONTRACT_VERSION,
            },
        )
        lifecycle = ForecastLifecycle(
            lifecycle_id=cycle.cycle_id,
            request_id=cycle.request_id,
            instrument_id=cycle.instrument_id,
            exchange=cycle.exchange,
            horizon=cycle.horizon,
            target_at=cycle.target_at,
            initial_at=cycle.initial_at,
            first_confirmation_at=cycle.first_confirmation_at,
            final_confirmation_at=cycle.final_confirmation_at,
            status=LifecycleStatus.AWAITING_FIRST_CONFIRMATION,
            created_at=cycle.created_at,
            updated_at=cycle.updated_at,
            metadata={
                "phase": PI09_PHASE,
                "cycle_id": cycle.cycle_id,
                "scheduler_owned": True,
                "target_rule": cycle.target_rule,
            },
        )
        created = self.store.save_forecast_cycle(cycle)
        self.store.save_request(request)
        self.store.save_lifecycle(lifecycle)
        for checkpoint in build_checkpoints(cycle, config=self.config):
            self.store.save_checkpoint(checkpoint)
        if not created:
            existing = self.store.get_forecast_cycle(cycle.cycle_id)
            if existing is not None:
                return existing
        return cycle

    def _save_cycle_children(self, cycle: ForecastCycle) -> None:
        """Make a persisted cycle self-healing after a partial crash."""

        self.store.save_request(
            PredictionRequest(
                request_id=cycle.request_id,
                instrument_id=cycle.instrument_id,
                exchange=cycle.exchange,
                horizon=cycle.horizon,
                target_at=cycle.target_at,
                created_at=cycle.created_at,
                actor="pi09-scheduler",
                notes="Scheduled forecast lifecycle; evidence only, never an order.",
                metadata={
                    "phase": PI09_PHASE,
                    "cycle_id": cycle.cycle_id,
                    "target_rule": cycle.target_rule,
                    "policy_version": cycle.policy_version,
                    "schedule_contract_version": SCHEDULE_CONTRACT_VERSION,
                },
            )
        )
        self.store.save_lifecycle(
            ForecastLifecycle(
                lifecycle_id=cycle.cycle_id,
                request_id=cycle.request_id,
                instrument_id=cycle.instrument_id,
                exchange=cycle.exchange,
                horizon=cycle.horizon,
                target_at=cycle.target_at,
                initial_at=cycle.initial_at,
                first_confirmation_at=cycle.first_confirmation_at,
                final_confirmation_at=cycle.final_confirmation_at,
                status=LifecycleStatus.AWAITING_FIRST_CONFIRMATION,
                created_at=cycle.created_at,
                updated_at=cycle.updated_at,
                metadata={
                    "phase": PI09_PHASE,
                    "cycle_id": cycle.cycle_id,
                    "scheduler_owned": True,
                    "target_rule": cycle.target_rule,
                },
            )
        )
        for checkpoint in build_checkpoints(cycle, config=self.config):
            self.store.save_checkpoint(checkpoint)

    @staticmethod
    def _in_verified_session(exchange: str, at: datetime) -> bool:
        calendar = calendar_by_mic(exchange)
        if calendar is None or not calendar.coverage(at):
            return False
        local = calendar.local(at)
        bounds = calendar.session_bounds(local.date())
        if bounds is None:
            return False
        opens, closes = bounds
        return opens <= local <= closes

    def _now(self, value: datetime | None = None) -> datetime:
        current = value or self._clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ScheduleValidationError("the scheduler clock must be timezone-aware")
        return current.astimezone(UTC)

    # -- execution ----------------------------------------------------------

    async def tick(self, *, now: datetime | None = None, limit: int | None = None) -> dict[str, Any]:
        """Claim and execute a bounded batch of due checkpoints."""

        async with self._tick_lock:
            current = self._now(now)
            if not self.config.enabled:
                return {
                    "now": current.isoformat(),
                    "disabled": True,
                    "recovered_leases": 0,
                    "due": 0,
                    "executed": [],
                }
            recovered = self.store.recover_expired_checkpoint_leases(current)
            due = self.store.due_checkpoints(
                current, limit=self.config.max_tasks_per_tick if limit is None else max(1, min(int(limit), 20))
            )
            executed: list[dict[str, Any]] = []
            for item in due:
                claimed = self.store.claim_checkpoint(
                    item.checkpoint_id,
                    owner=self.owner,
                    now=current,
                    lease_seconds=self.config.lease_seconds,
                )
                if claimed is None:
                    continue
                outcome = await self._execute(claimed, current)
                executed.append(outcome)
            self._last_tick = current
            self._ticks += 1
            return {
                "now": current.isoformat(),
                "recovered_leases": recovered,
                "due": len(due),
                "executed": executed,
            }

    async def _execute(self, checkpoint: ForecastCheckpoint, now: datetime) -> dict[str, Any]:
        self._runs += 1
        cycle = self.store.get_forecast_cycle(checkpoint.cycle_id)
        if cycle is None:
            return await self._finish(
                checkpoint, CheckpointState.DEAD_LETTER, now, ("forecast cycle is missing",)
            )
        try:
            if checkpoint.kind is CheckpointKind.OUTCOME:
                result = await self._run_outcome(checkpoint, cycle, now)
            else:
                result = await self._run_forecast(checkpoint, cycle, now)
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed checkpoint is durable evidence
            self._failures += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            return await self._retry(checkpoint, now, f"{type(exc).__name__}: {exc}")

    async def _run_forecast(
        self, checkpoint: ForecastCheckpoint, cycle: ForecastCycle, now: datetime
    ) -> dict[str, Any]:
        existing_forecast = next(
            (
                item
                for item in self.store.forecasts(cycle.cycle_id)
                if item.lifecycle_stage is checkpoint.kind.stage
            ),
            None,
        )
        if existing_forecast is not None:
            # Repair the cycle and stage evidence if a previous attempt
            # persisted the immutable version and then crashed.
            stage = checkpoint.kind.stage
            if stage is not None and self.store.stage_for(cycle.cycle_id, stage) is None:
                await self._record_stage(
                    cycle,
                    checkpoint,
                    stage,
                    StageStatus.RECORDED,
                    now,
                    forecast=existing_forecast,
                    reasons=existing_forecast.reasons,
                )
            self._update_lifecycle(cycle, checkpoint.kind, existing_forecast, now)
            cycle = self._attach_cycle_forecast(cycle, checkpoint.kind, existing_forecast, now)
            self._set_cycle_state(cycle, checkpoint.kind, now, confirmed=True)
            return await self._finish(
                checkpoint,
                CheckpointState.COMPLETED,
                now,
                existing_forecast.reasons,
                forecast_id=existing_forecast.forecast_id,
                payload={"idempotent_replay": True},
            )
        if now >= cycle.target_at:
            await self._record_stage_for_skip(
                cycle, checkpoint, now, "target time passed before this checkpoint executed"
            )
            if checkpoint.kind is CheckpointKind.FINAL:
                self._mark_unconfirmed(cycle, now)
            return await self._finish(
                checkpoint,
                CheckpointState.SKIPPED,
                now,
                ("target time passed before this checkpoint executed; it is not an on-time check",),
            )
        stage = checkpoint.kind.stage
        if stage is None:
            raise PredictionValidationError("a forecast checkpoint has no confirmation stage")
        calendar = calendar_by_mic(cycle.exchange)
        if calendar is None:
            await self._record_stage_for_skip(
                cycle, checkpoint, now, f"no verified calendar for {cycle.exchange}; failing closed"
            )
            if checkpoint.kind is CheckpointKind.FINAL:
                self._mark_unconfirmed(cycle, now)
            return await self._finish(
                checkpoint,
                CheckpointState.FAILED,
                now,
                (f"no verified calendar for {cycle.exchange}; failing closed",),
            )
        if not calendar.coverage(now):
            await self._record_stage_for_skip(
                cycle, checkpoint, now, "calendar coverage is missing for the execution year"
            )
            if checkpoint.kind is CheckpointKind.FINAL:
                self._mark_unconfirmed(cycle, now)
            return await self._finish(
                checkpoint,
                CheckpointState.FAILED,
                now,
                ("calendar coverage is missing for the execution year",),
            )
        if checkpoint.kind is not CheckpointKind.OUTCOME and not calendar.is_open(now):
            await self._record_stage_for_skip(
                cycle, checkpoint, now, "the exchange is not in a verified trading session"
            )
            if checkpoint.kind is CheckpointKind.FINAL:
                self._mark_unconfirmed(cycle, now)
            return await self._finish(
                checkpoint,
                CheckpointState.SKIPPED,
                now,
                ("the exchange is not in a verified trading session; no forecast was fabricated",),
            )
        data = await self._observation(checkpoint, cycle, now)
        if data.get("available") is not True:
            reasons = tuple(str(item) for item in data.get("reasons") or ("no fresh observation",))
            if checkpoint.kind is CheckpointKind.FINAL:
                return await self._defer(
                    checkpoint, now, "; ".join(reasons)
                )
            await self._record_stage_for_skip(cycle, checkpoint, now, "; ".join(reasons))
            return await self._finish(
                checkpoint,
                CheckpointState.SKIPPED,
                now,
                reasons,
                payload={"observation": data},
            )
        try:
            output = await self._generate(cycle, now, data)
        except Exception as exc:  # noqa: BLE001 - model failure is a failed checkpoint
            self._last_error = f"ensemble: {type(exc).__name__}: {exc}"
            return await self._retry(checkpoint, now, f"ensemble: {type(exc).__name__}: {exc}")
        if output.get("status") == "abstained":
            reasons = tuple(str(item) for item in output.get("reasons") or ("ensemble abstained",))
            await self._record_stage(
                cycle, checkpoint, stage, StageStatus.SKIPPED,
                now, reasons=reasons
            )
            return await self._finish(
                checkpoint,
                CheckpointState.ABSTAINED,
                now,
                reasons,
                payload={"ensemble": output, "observation": data},
            )
        forecast = self._forecast_from_body(output, cycle, checkpoint, now)
        previous = self._latest_before_stage(cycle.cycle_id, stage)
        revision = self._revision(previous, forecast) if previous else None
        self.store.save_forecast(forecast)
        cycle = self._attach_cycle_forecast(cycle, checkpoint.kind, forecast, now)
        changes = changed_fields(previous, forecast) if previous is not None else ()
        await self._record_stage(
            cycle,
            checkpoint,
            forecast.lifecycle_stage,
            StageStatus.RECORDED,
            now,
            forecast=forecast,
            previous=previous,
            reasons=forecast.reasons,
        )
        self._update_lifecycle(cycle, checkpoint.kind, forecast, now)
        self._set_cycle_state(cycle, checkpoint.kind, now, confirmed=True)
        await self._publish_forecast(forecast, checkpoint, now)
        return await self._finish(
            checkpoint,
            CheckpointState.COMPLETED,
            now,
            forecast.reasons,
            forecast_id=forecast.forecast_id,
            previous_forecast_id=previous.forecast_id if previous else "",
            revision=revision.to_dict() if revision else None,
            payload={
                "observation": data,
                "ensemble": output,
                "native_target_at": output.get("target_at"),
                "changed_fields": list(changes),
            },
        )

    async def _observation(
        self, checkpoint: ForecastCheckpoint, cycle: ForecastCycle, now: datetime
    ) -> dict[str, Any]:
        if self.gateway is None:
            return {"available": False, "reasons": ("the PI-03 gateway is unavailable",)}
        previous_at = None
        if checkpoint.kind is not CheckpointKind.INITIAL and checkpoint.kind.stage is not None:
            previous = self._latest_before_stage(cycle.cycle_id, checkpoint.kind.stage)
            if previous is not None:
                previous_at = previous.data_observed_at
            order = {
                ConfirmationStage.INITIAL: 0,
                ConfirmationStage.FIRST: 1,
                ConfirmationStage.FINAL: 2,
            }
            for prior in self.store.checkpoints(cycle.cycle_id):
                if prior.kind.stage is None or order[prior.kind.stage] >= order[checkpoint.kind.stage]:
                    continue
                observed = dict(prior.payload).get("observation")
                if not isinstance(observed, Mapping):
                    continue
                try:
                    candidate = _dt(observed.get("observed_at"), "prior observed_at")
                except ScheduleValidationError:
                    continue
                if previous_at is None or candidate > previous_at:
                    previous_at = candidate
        try:
            started = time.perf_counter()
            data = await self.gateway.checkpoint(
                cycle.instrument_id,
                previous_observed_at=previous_at,
                refresh=True,
                accepted_freshness=(Freshness.FRESH,),
            )
        except Exception as exc:  # noqa: BLE001 - a provider outage is a checkpoint answer
            return {
                "available": False,
                "reasons": (f"gateway checkpoint failed: {type(exc).__name__}: {exc}",),
            }
        observation = data.get("observation")
        if data.get("available") is False:
            return {
                **data,
                "available": False,
                "reasons": tuple(
                    data.get("reasons")
                    or ("the gateway refused this checkpoint observation",)
                ),
            }
        if not isinstance(observation, Mapping):
            return {**data, "available": False, "reasons": tuple(data.get("reasons") or ("no observation is available",))}
        try:
            price = _decimal(observation.get("price"), "observation.price")
            # PI-03's checkpoint envelope carries the source timestamp inside
            # ``timestamps``; accept a flat timestamp as well for older callers.
            observed_value = observation.get("observed_at")
            if not observed_value:
                timestamps = observation.get("timestamps")
                if isinstance(timestamps, Mapping):
                    observed_value = timestamps.get("source_observed_at")
            if not observed_value:
                observed_value = data.get("observed_at")
            observed_at = _dt(observed_value, "observation.observed_at")
        except ScheduleValidationError as exc:
            return {"available": False, "reasons": (str(exc),), "raw": data}
        if observed_at > now:
            return {"available": False, "reasons": ("the observation timestamp is in the future",), "raw": data}
        if str(observation.get("instrument_id") or cycle.instrument_id) != cycle.instrument_id:
            return {"available": False, "reasons": ("the observation belongs to another instrument",), "raw": data}
        freshness = str(observation.get("freshness") or data.get("freshness") or "")
        if freshness != "fresh":
            return {
                "available": False,
                "reasons": (f"observation freshness {freshness or 'missing'} is not accepted at this checkpoint",),
                "raw": data,
            }
        if (
            data.get("quality") in {"invalid", "unavailable"}
            or observation.get("quality") in {"invalid", "unavailable"}
            or data.get("validation", {}).get("usable") is False
        ):
            return {"available": False, "reasons": tuple(data.get("reasons") or ("observation quality is not usable",)), "raw": data}
        if previous_at is not None and observed_at <= previous_at:
            return {
                "available": False,
                "reasons": ("the observation is not newer than the previous checkpoint",),
                "raw": data,
            }
        return {
            "available": True,
            "price": price,
            "observed_at": observed_at.isoformat(),
            "source": observation.get("provider", ""),
            "freshness": observation.get("freshness") or data.get("freshness"),
            "quality": observation.get("quality") or data.get("quality"),
            "resolution": observation.get("resolution") or data.get("resolution"),
            "raw": data,
            "gateway_latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }

    async def _generate(
        self, cycle: ForecastCycle, now: datetime, data: Mapping[str, Any]
    ) -> dict[str, Any]:
        if self.ensemble is None:
            raise RuntimeError("PI-08 ensemble is unavailable")
        supplied: dict[str, Any] = {}
        if self._context_provider is not None:
            try:
                context_inputs = self._context_provider(
                    cycle.instrument_id, cycle.horizon, now
                )
                if inspect.isawaitable(context_inputs):
                    context_inputs = await context_inputs
                if isinstance(context_inputs, Mapping):
                    supplied = {
                        key: context_inputs[key]
                        for key in (
                            "fundamentals",
                            "news_events",
                            "benchmark_bars",
                            "sector_bars",
                            "context_bars",
                            "forecast_metadata",
                        )
                        if key in context_inputs
                    }
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - optional context is explicit
                self._last_error = f"outlook context: {type(exc).__name__}: {exc}"
        started = time.perf_counter()
        body = await self.ensemble.generate(
            cycle.instrument_id,
            cycle.horizon,
            as_of=now,
            resolution=cycle.resolution,
            exchange=cycle.exchange,
            currency=self._currency(cycle.instrument_id),
            regime=self._regime(cycle.instrument_id),
            **supplied,
        )
        if not isinstance(body, Mapping):
            raise PredictionValidationError("the ensemble returned a non-object response")
        result = dict(body)
        result["observation"] = dict(data)
        result["inference_latency_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        return result

    def _forecast_from_body(
        self, body: Mapping[str, Any], cycle: ForecastCycle, checkpoint: ForecastCheckpoint, now: datetime
    ) -> ForecastOutput:
        raw = body.get("forecast")
        if not isinstance(raw, Mapping):
            raw = body
        fields = ForecastOutput.__dataclass_fields__
        aliases = {
            "reference_at": "reference_price_timestamp",
            "direction": "predicted_direction",
            "uncertainty": "forecast_uncertainty",
            "target_at": "forecast_target_timestamp",
            "generated_at": "forecast_generation_timestamp",
            "data_observed_at": "underlying_data_timestamp",
            "data_received_at": "data_received_timestamp",
            "data_freshness": "data_freshness_status",
            "validity": "forecast_validity",
            "validity_conditions": "forecast_validity_conditions",
            "disagreement": "ensemble_disagreement",
        }
        payload: dict[str, Any] = {}
        for key in fields:
            if key in raw:
                payload[key] = raw[key]
            elif aliases.get(key) in raw:
                payload[key] = raw[aliases[key]]
        output = ForecastOutput(**payload)
        if output.instrument_id != cycle.instrument_id or output.forecast_horizon != cycle.horizon:
            raise PredictionValidationError("the ensemble forecast identity does not match the cycle")
        chosen_stage = checkpoint.kind.stage
        if chosen_stage is None:
            raise PredictionValidationError("an outcome checkpoint cannot create a forecast version")
        stage = ConfirmationStage(chosen_stage)
        native_target = output.target_at
        target = cycle.target_at
        if native_target != target:
            # The cycle target is the lifecycle contract.  Preserve the native
            # model target in metadata so a mismatch can never be hidden.
            output = replace(
                output,
                metadata={
                    **dict(output.metadata),
                    "native_target_at": native_target.isoformat(),
                    "target_normalized_to_cycle": True,
                },
            )
        version_id = prediction_id("pfc_", cycle.cycle_id, stage.value, checkpoint.checkpoint_id)
        validity = (
            ForecastValidity.CONFIRMED
            if stage is ConfirmationStage.FINAL
            else ForecastValidity.UNCONFIRMED
        )
        return replace(
            output,
            forecast_id=version_id,
            lifecycle_id=cycle.cycle_id,
            lifecycle_stage=stage,
            target_at=target,
            validity=validity,
            metadata={
                **dict(output.metadata),
                "pi09": {
                    "cycle_id": cycle.cycle_id,
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "scheduled_at": checkpoint.scheduled_at.isoformat(),
                    "executed_at": now.isoformat(),
                    "late": now > checkpoint.scheduled_at,
                    "native_target_at": native_target.isoformat(),
                    "observation": dict(body.get("observation") or {}),
                },
            },
        )

    async def _run_outcome(
        self, checkpoint: ForecastCheckpoint, cycle: ForecastCycle, now: datetime
    ) -> dict[str, Any]:
        if now < cycle.target_at:
            return await self._defer(checkpoint, now, "the target time has not been reached")
        data = await self._observation(checkpoint, cycle, now)
        if data.get("available") is not True:
            return await self._defer(
                checkpoint, now, "; ".join(str(item) for item in data.get("reasons") or ("actual observation is unavailable",))
            )
        actual_at = _dt(data["observed_at"], "actual observed_at")
        if not self._in_verified_session(cycle.exchange, actual_at):
            return await self._defer(
                checkpoint,
                now,
                "the actual observation is outside the verified target exchange session",
            )
        delay = (actual_at - cycle.target_at).total_seconds()
        if delay < 0:
            return await self._defer(checkpoint, now, "the observation precedes the target timestamp")
        if delay > self.config.max_outcome_delay_seconds:
            return await self._defer(
                checkpoint,
                now,
                f"actual observation is {delay:.0f}s after target, above the reconciliation window",
            )
        forecasts = self.store.forecasts(cycle.cycle_id)
        if not forecasts:
            return await self._finish(
                checkpoint,
                CheckpointState.SKIPPED,
                now,
                ("no forecast version exists to evaluate",),
                payload={"observation": data},
            )
        actual_price = _decimal(data["price"], "actual price")
        try:
            await self.prediction.evaluate(cycle.cycle_id, actual_price, at=now)
        except Exception as exc:  # noqa: BLE001 - evaluation is retryable
            self._last_error = f"evaluation: {type(exc).__name__}: {exc}"
            return await self._retry(checkpoint, now, f"evaluation: {type(exc).__name__}: {exc}")
        evaluations = {item.forecast_id: item for item in self.store.evaluations(cycle.cycle_id)}
        missing_evaluations = [
            forecast.forecast_id for forecast in forecasts if forecast.forecast_id not in evaluations
        ]
        if missing_evaluations:
            return await self._retry(
                checkpoint,
                now,
                "the evaluation path did not produce a result for every preserved version: "
                + ", ".join(missing_evaluations),
            )
        outcomes: list[dict[str, Any]] = []
        for forecast in forecasts:
            evaluation = evaluations[forecast.forecast_id]
            outcome = self._outcome(cycle, forecast, evaluation, actual_at, now)
            if self.store.save_outcome(outcome):
                outcomes.append(outcome.to_dict())
        stored_outcomes = self.store.outcomes(cycle.cycle_id)
        if stored_outcomes:
            # Recovery after an outcome write can reach this point with the
            # rows already persisted but no local list; report the stored
            # evidence rather than an empty, misleading payload/event.
            outcomes = [item.to_dict() for item in stored_outcomes]
            cycle = replace(
                cycle,
                outcome_id=stored_outcomes[0].outcome_id,
                updated_at=now,
            )
        self._set_cycle_state(cycle, CheckpointKind.OUTCOME, now, confirmed=True)
        await self._publish(
            EventType.NOVAMARKET_PREDICTION_FORECAST_EVALUATED,
            lifecycle_id=cycle.cycle_id,
            forecast_id=forecasts[0].forecast_id,
            stage=forecasts[0].lifecycle_stage.value,
            direction_correct=bool(outcomes and outcomes[0].get("direction_correct")),
            status="evaluated",
        )
        return await self._finish(
            checkpoint,
            CheckpointState.EVALUATED,
            now,
            (),
            payload={
                "observation": data,
                "actual_observed_at": actual_at.isoformat(),
                "observation_delay_seconds": delay,
                "outcomes": outcomes,
            },
        )

    def _outcome(
        self,
        cycle: ForecastCycle,
        forecast: ForecastOutput,
        evaluation: Any,
        actual_at: datetime,
        now: datetime,
    ) -> ForecastOutcome:
        actual_price = _decimal(evaluation.actual_price, "actual price")
        actual_return = float((actual_price / forecast.reference_price) - Decimal("1"))
        actual_direction = direction_for_return(
            actual_return, HORIZON_SPECS[forecast.forecast_horizon].neutral_band_pct
        )
        price_error = forecast.predicted_price - actual_price
        abs_price_error = abs(price_error)
        pct_error = float(abs_price_error / actual_price)
        inside = forecast.lower_return <= actual_return <= forecast.upper_return
        return ForecastOutcome(
            outcome_id=prediction_id("pout_", cycle.cycle_id, forecast.forecast_id, actual_at.isoformat()),
            cycle_id=cycle.cycle_id,
            forecast_id=forecast.forecast_id,
            stage=forecast.lifecycle_stage,
            target_at=forecast.target_at,
            actual_observed_at=actual_at,
            evaluated_at=now,
            observation_delay_seconds=max((actual_at - forecast.target_at).total_seconds(), 0.0),
            actual_price=actual_price,
            actual_return=actual_return,
            predicted_price=forecast.predicted_price,
            predicted_return=forecast.predicted_return,
            predicted_direction=forecast.direction.value,
            actual_direction=actual_direction.value,
            direction_correct=forecast.direction is actual_direction,
            price_error=price_error,
            absolute_price_error=abs_price_error,
            percentage_price_error=pct_error,
            return_error=forecast.predicted_return - actual_return,
            absolute_return_error=abs(forecast.predicted_return - actual_return),
            inside_interval=inside,
            interval_lower=forecast.lower_return,
            interval_upper=forecast.upper_return,
            interval_width=forecast.upper_return - forecast.lower_return,
            metadata={
                "model_id": forecast.model_id,
                "model_version": forecast.model_version,
                "ensemble_id": forecast.ensemble_id,
                "probability_status": forecast.probability_status.value,
                "regime": str(forecast.metadata.get("regime") or forecast.metadata.get("pi08_regime") or "unclassified"),
                "brier": evaluation.brier,
                "log_loss": evaluation.log_loss,
                "evaluation_id": evaluation.evaluation_id,
            },
        )

    def _revision(self, previous: ForecastOutput, current: ForecastOutput) -> ForecastRevision:
        price_revision = current.predicted_price - previous.predicted_price
        return_revision = current.predicted_return - previous.predicted_return
        probabilities = {
            key: (
                None
                if getattr(current, key) is None or getattr(previous, key) is None
                else float(getattr(current, key)) - float(getattr(previous, key))
            )
            for key in ("p_up", "p_down", "p_neutral")
        }
        uncertainty_changed = (
            current.uncertainty is not previous.uncertainty
            or abs(current.uncertainty_width - previous.uncertainty_width) > self.config.revision_return_threshold
        )
        reasons: list[str] = []
        if current.direction is Direction.INVALID or current.validity is ForecastValidity.INVALID:
            classification = RevisionClass.INVALIDATED
            reasons.append("the new forecast is invalid")
        elif current.direction is not previous.direction:
            classification = RevisionClass.DIRECTION_CHANGED
            reasons.append("the predicted direction changed")
        elif (
            abs(return_revision) <= self.config.revision_return_threshold
            and abs(price_revision) <= self.config.revision_price_threshold
        ):
            classification = RevisionClass.STABLE
            reasons.append("the revision is inside the configured stability threshold")
        elif current.direction is Direction.UNCERTAIN or uncertainty_changed:
            classification = RevisionClass.UNCERTAIN
            reasons.append("direction is uncertain or uncertainty changed")
        elif abs(current.predicted_return) > abs(previous.predicted_return):
            classification = RevisionClass.STRENGTHENED
            reasons.append("the same-direction return estimate strengthened")
        else:
            classification = RevisionClass.WEAKENED
            reasons.append("the same-direction return estimate weakened")
        return ForecastRevision(
            previous_stage=previous.lifecycle_stage,
            current_stage=current.lifecycle_stage,
            previous_forecast_id=previous.forecast_id,
            current_forecast_id=current.forecast_id,
            price_revision=price_revision,
            return_revision=return_revision,
            direction_changed=current.direction is not previous.direction,
            probability_changes=probabilities,
            interval_lower_change=current.lower_return - previous.lower_return,
            interval_upper_change=current.upper_return - previous.upper_return,
            uncertainty_changed=uncertainty_changed,
            classification=classification,
            previous_uncertainty=previous.uncertainty.value,
            current_uncertainty=current.uncertainty.value,
            uncertainty_width_change=current.uncertainty_width - previous.uncertainty_width,
            previous_members=tuple(previous.member_versions),
            current_members=tuple(current.member_versions),
            previous_data_freshness=previous.data_freshness.value,
            current_data_freshness=current.data_freshness.value,
            previous_ensemble_id=previous.ensemble_id,
            current_ensemble_id=current.ensemble_id,
            reasons=tuple(reasons),
        )

    def _latest_before_stage(self, cycle_id: str, stage: ConfirmationStage) -> ForecastOutput | None:
        order = {
            ConfirmationStage.INITIAL: 0,
            ConfirmationStage.FIRST: 1,
            ConfirmationStage.FINAL: 2,
        }
        forecasts = [item for item in self.store.forecasts(cycle_id) if order[item.lifecycle_stage] < order[stage]]
        return forecasts[-1] if forecasts else None

    async def _record_stage(
        self,
        cycle: ForecastCycle,
        checkpoint: ForecastCheckpoint,
        stage: ConfirmationStage,
        status: StageStatus,
        now: datetime,
        *,
        forecast: ForecastOutput | None = None,
        previous: ForecastOutput | None = None,
        reasons: Sequence[str] = (),
    ) -> None:
        record = LifecycleStageRecord(
            stage_id=prediction_id("pst_", cycle.cycle_id, stage.value),
            lifecycle_id=cycle.cycle_id,
            stage=stage,
            scheduled_at=checkpoint.scheduled_at,
            recorded_at=now,
            status=status,
            forecast_id=forecast.forecast_id if forecast else "",
            previous_forecast_id=previous.forecast_id if previous else "",
            data_observed_at=forecast.data_observed_at if forecast else None,
            data_freshness=forecast.data_freshness if forecast else None,
            data_resolution=forecast.data_resolution if forecast else None,
            changed_fields=changed_fields(previous, forecast) if previous and forecast else (),
            reasons=tuple(reasons),
        )
        self.store.save_stage(record)

    async def _record_stage_for_skip(
        self, cycle: ForecastCycle, checkpoint: ForecastCheckpoint, now: datetime, reason: str
    ) -> None:
        stage = checkpoint.kind.stage
        if stage is None:
            return
        await self._record_stage(
            cycle,
            checkpoint,
            stage,
            StageStatus.SKIPPED
            if stage in (ConfirmationStage.INITIAL, ConfirmationStage.FIRST)
            else StageStatus.INVALID,
            now,
            reasons=(reason,),
        )

    def _update_lifecycle(
        self,
        cycle: ForecastCycle,
        kind: CheckpointKind,
        forecast: ForecastOutput,
        now: datetime,
    ) -> None:
        lifecycle = self.store.get_lifecycle(cycle.cycle_id)
        if lifecycle is None:
            return
        if lifecycle.status is LifecycleStatus.EVALUATED:
            return
        if kind is CheckpointKind.INITIAL:
            lifecycle = replace(
                lifecycle,
                initial_forecast_id=forecast.forecast_id,
                status=LifecycleStatus.AWAITING_FIRST_CONFIRMATION,
                updated_at=now,
            )
        elif kind is CheckpointKind.FIRST:
            lifecycle = replace(
                lifecycle,
                first_forecast_id=forecast.forecast_id,
                status=LifecycleStatus.AWAITING_FINAL_CONFIRMATION,
                updated_at=now,
            )
        else:
            lifecycle = replace(
                lifecycle,
                final_forecast_id=forecast.forecast_id,
                status=LifecycleStatus.CONFIRMED,
                updated_at=now,
            )
        self.store.update_lifecycle(lifecycle)

    def _attach_cycle_forecast(
        self,
        cycle: ForecastCycle,
        kind: CheckpointKind,
        forecast: ForecastOutput,
        now: datetime,
    ) -> ForecastCycle:
        latest = self.store.get_forecast_cycle(cycle.cycle_id)
        if latest is not None and latest.state is CycleState.EVALUATED:
            cycle = replace(cycle, state=CycleState.EVALUATED)
        fields: dict[str, Any] = {}
        if kind is CheckpointKind.INITIAL:
            fields["initial_forecast_id"] = forecast.forecast_id
        elif kind is CheckpointKind.FIRST:
            fields["first_forecast_id"] = forecast.forecast_id
        elif kind is CheckpointKind.FINAL:
            fields["final_forecast_id"] = forecast.forecast_id
        return replace(cycle, updated_at=now, **fields) if fields else cycle

    def _set_cycle_state(
        self, cycle: ForecastCycle, kind: CheckpointKind, now: datetime, *, confirmed: bool
    ) -> None:
        state = cycle.state
        latest = self.store.get_forecast_cycle(cycle.cycle_id)
        if kind is CheckpointKind.OUTCOME:
            state = CycleState.EVALUATED
        elif latest is not None and latest.state is CycleState.EVALUATED:
            # Do not let a late confirmation retry downgrade a completed
            # evaluation; still persist any newly attached version ids.
            state = CycleState.EVALUATED
        elif kind is CheckpointKind.FINAL:
            state = CycleState.CONFIRMED if confirmed else CycleState.UNCONFIRMED
        elif cycle.state is CycleState.SCHEDULED:
            state = CycleState.ACTIVE
        # Persist even when the state label is unchanged: the same row carries
        # the attached version/outcome ids, which must not be silently dropped
        # on a first or final confirmation.
        self.store.update_forecast_cycle(replace(cycle, state=state, updated_at=now))

    def _mark_unconfirmed(self, cycle: ForecastCycle, now: datetime) -> None:
        lifecycle = self.store.get_lifecycle(cycle.cycle_id)
        latest = self.store.get_forecast_cycle(cycle.cycle_id)
        if (
            lifecycle is not None and lifecycle.status is LifecycleStatus.EVALUATED
        ) or (latest is not None and latest.state is CycleState.EVALUATED):
            # A late final failure cannot erase a completed outcome.
            return
        if lifecycle is not None:
            self.store.update_lifecycle(
                replace(
                    lifecycle,
                    status=LifecycleStatus.UNCONFIRMED,
                    updated_at=now,
                    invalidation_reason="the final confirmation could not be completed",
                )
            )
        self.store.update_forecast_cycle(
            replace(cycle, state=CycleState.UNCONFIRMED, updated_at=now)
        )

    async def _finish(
        self,
        checkpoint: ForecastCheckpoint,
        status: CheckpointState,
        now: datetime,
        reasons: Sequence[str],
        *,
        forecast_id: str = "",
        previous_forecast_id: str = "",
        revision: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        updated = replace(
            checkpoint,
            status=status,
            executed_at=now,
            forecast_id=forecast_id or checkpoint.forecast_id,
            previous_forecast_id=previous_forecast_id or checkpoint.previous_forecast_id,
            revision=dict(revision) if revision else checkpoint.revision,
            reasons=tuple(reasons),
            lease_owner="",
            lease_expires_at=None,
            next_attempt_at=None,
            payload={
                **dict(checkpoint.payload),
                **(dict(payload) if payload else {}),
                "last_attempt_at": now.isoformat(),
                "late": now > checkpoint.scheduled_at,
            },
        )
        self.store.update_checkpoint(updated)
        if status in {
            CheckpointState.SKIPPED,
            CheckpointState.ABSTAINED,
            CheckpointState.FAILED,
            CheckpointState.DEAD_LETTER,
        } and checkpoint.kind is CheckpointKind.FINAL:
            cycle = self.store.get_forecast_cycle(checkpoint.cycle_id)
            if cycle is not None:
                self._mark_unconfirmed(cycle, now)
        return updated.to_dict()

    async def _defer(self, checkpoint: ForecastCheckpoint, now: datetime, reason: str) -> dict[str, Any]:
        attempts = checkpoint.attempts + 1
        backoff = self.config.retry_backoff_seconds * max(1, attempts)
        terminal = attempts >= checkpoint.max_attempts
        updated = replace(
            checkpoint,
            status=(
                CheckpointState.DEAD_LETTER
                if terminal
                else CheckpointState.PENDING
            ),
            attempts=attempts,
            next_attempt_at=(None if terminal else now + timedelta(seconds=backoff)),
            lease_owner="",
            lease_expires_at=None,
            reasons=(*checkpoint.reasons, reason),
            executed_at=(now if terminal else checkpoint.executed_at),
            payload={**dict(checkpoint.payload), "last_attempt_at": now.isoformat()},
        )
        self.store.update_checkpoint(updated)
        if terminal and checkpoint.kind is CheckpointKind.FINAL:
            cycle = self.store.get_forecast_cycle(checkpoint.cycle_id)
            if cycle is not None:
                self._mark_unconfirmed(cycle, now)
        await self._publish(
            EventType.NOVAMARKET_PREDICTION_CONFIRMATION_SKIPPED,
            lifecycle_id=checkpoint.cycle_id,
            stage=checkpoint.kind.value,
            status=updated.status.value,
            reason=reason,
        )
        return updated.to_dict()

    async def _retry(self, checkpoint: ForecastCheckpoint, now: datetime, reason: str) -> dict[str, Any]:
        attempts = checkpoint.attempts + 1
        if attempts >= checkpoint.max_attempts:
            updated = replace(
                checkpoint,
                status=CheckpointState.DEAD_LETTER,
                attempts=attempts,
                next_attempt_at=None,
                lease_owner="",
                lease_expires_at=None,
                executed_at=now,
                reasons=(*checkpoint.reasons, reason),
                payload={**dict(checkpoint.payload), "last_attempt_at": now.isoformat()},
            )
            if checkpoint.kind is CheckpointKind.FINAL:
                cycle = self.store.get_forecast_cycle(checkpoint.cycle_id)
                if cycle is not None:
                    self._mark_unconfirmed(cycle, now)
        else:
            updated = replace(
                checkpoint,
                status=(
                    CheckpointState.PENDING
                    if checkpoint.kind is CheckpointKind.OUTCOME
                    else CheckpointState.SCHEDULED
                ),
                attempts=attempts,
                next_attempt_at=now + timedelta(seconds=self.config.retry_backoff_seconds * attempts),
                lease_owner="",
                lease_expires_at=None,
                executed_at=now,
                reasons=(*checkpoint.reasons, reason),
                payload={**dict(checkpoint.payload), "last_attempt_at": now.isoformat()},
            )
        self.store.update_checkpoint(updated)
        return updated.to_dict()

    async def _publish(self, event_type: EventType | str, **payload: Any) -> None:
        if self.publisher is None:
            return
        try:
            await self.publisher.publish(event_type, **payload)
        except Exception as exc:  # noqa: BLE001 - observability must never stop scheduling
            self._last_error = f"event publish: {type(exc).__name__}: {exc}"

    async def _publish_forecast(
        self, forecast: ForecastOutput, checkpoint: ForecastCheckpoint, now: datetime
    ) -> None:
        del checkpoint
        await self._publish(
            EventType.NOVAMARKET_PREDICTION_FORECAST_GENERATED,
            forecast_id=forecast.forecast_id,
            lifecycle_id=forecast.lifecycle_id,
            instrument_id=forecast.instrument_id,
            horizon=forecast.forecast_horizon.value,
            direction=forecast.direction.value,
            stage=forecast.lifecycle_stage.value,
            target_at=forecast.target_at.isoformat(),
        )
        if forecast.lifecycle_stage is not ConfirmationStage.INITIAL:
            await self._publish(
                EventType.NOVAMARKET_PREDICTION_FORECAST_CONFIRMED,
                lifecycle_id=forecast.lifecycle_id,
                stage=forecast.lifecycle_stage.value,
                forecast_id=forecast.forecast_id,
                direction=forecast.direction.value,
                changed_fields="",
            )

    # -- background loop ----------------------------------------------------

    def start(self) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            return self.status()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return self.status()
        self._task = loop.create_task(self._loop(), name="novamarket-forecast-scheduler")
        return self.status()

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - stop must finish
                pass

    async def _loop(self) -> None:
        try:
            while True:
                if self.config.enabled:
                    # Plan before executing so a newly created initial
                    # checkpoint can run in the same tick rather than waiting
                    # an extra scheduler interval.
                    if self.config.auto_schedule and self._planner is not None:
                        try:
                            self._planner(self)
                        except Exception as exc:  # noqa: BLE001 - planning is best effort
                            self._last_error = f"plan: {type(exc).__name__}: {exc}"
                    try:
                        await self.tick()
                    except Exception as exc:  # noqa: BLE001 - a tick failure must not stop future ticks
                        self._last_error = f"tick: {type(exc).__name__}: {exc}"
                await asyncio.sleep(self.config.tick_seconds)
        except asyncio.CancelledError:
            return

    # -- read surfaces ------------------------------------------------------

    def status(self) -> dict[str, Any]:
        now = self._now()
        cycles = self.store.forecast_cycles(limit=200)
        checkpoints = [item for cycle in cycles for item in self.store.checkpoints(cycle.cycle_id)]
        counts = Counter(item.status.value for item in checkpoints)
        calendar = calendar_by_mic(cycles[0].exchange if cycles else "XNSE")
        return {
            "phase": PI09_PHASE,
            "implemented": True,
            "contract_version": SCHEDULE_CONTRACT_VERSION,
            "running": bool(self._task is not None and not self._task.done()),
            "owner": self.owner,
            "config": self.config.to_dict(),
            "last_tick": self._last_tick.isoformat() if self._last_tick else None,
            "last_error": self._last_error,
            "outlook_context": self._context_provider is not None,
            "ticks": self._ticks,
            "runs": self._runs,
            "failures": self._failures,
            "cycles": len(cycles),
            "checkpoint_states": dict(counts),
            "missed_checkpoints": self.missed(now=now, limit=20),
            "pending_evaluations": [
                item.to_dict()
                for item in checkpoints
                if item.kind is CheckpointKind.OUTCOME
                and item.status in {CheckpointState.PENDING, CheckpointState.DEAD_LETTER}
            ][:20],
            "session": session_snapshot(calendar, now) if calendar is not None else None,
            "gateway": self._gateway_status(),
            "authority": {
                "forecast_only": True,
                "risk_authority": "NM-08",
                "execution_authority": "NM-10",
                "orders_created": False,
                "portfolio_mutations": False,
                "ledger_mutations": False,
                "fills_created": False,
            },
        }

    def _gateway_status(self) -> dict[str, Any]:
        try:
            status = self.gateway.status()
            return dict(status) if isinstance(status, Mapping) else {}
        except Exception as exc:  # noqa: BLE001 - a read must not crash scheduler health
            return {"error": f"{type(exc).__name__}: {exc}"}

    def missed(self, *, now: datetime | None = None, limit: int = 100) -> list[dict[str, Any]]:
        current = self._now(now)
        cycles = self.store.forecast_cycles(limit=200)
        items: list[dict[str, Any]] = []
        for cycle in cycles:
            for checkpoint in self.store.checkpoints(cycle.cycle_id):
                pending_due = (
                    checkpoint.due_at() <= current
                    and checkpoint.executed_at is None
                    and checkpoint.status in {
                        CheckpointState.SCHEDULED,
                        CheckpointState.PENDING,
                    }
                )
                terminal_miss = checkpoint.status in {
                    CheckpointState.SKIPPED,
                    CheckpointState.FAILED,
                    CheckpointState.DEAD_LETTER,
                }
                if pending_due or terminal_miss:
                    items.append({
                        **checkpoint.to_dict(),
                        "cycle": cycle.to_dict(),
                        "missed_by_seconds": (
                            (current - checkpoint.due_at()).total_seconds()
                            if pending_due
                            else max(0.0, (checkpoint.executed_at - checkpoint.scheduled_at).total_seconds())
                            if checkpoint.executed_at
                            else 0.0
                        ),
                        "missed_kind": "not_executed" if pending_due else checkpoint.status.value,
                    })
        return items[: max(1, min(int(limit), 200))]

    def cycle_view(self, cycle_id: str) -> dict[str, Any]:
        cycle = self.store.get_forecast_cycle(cycle_id)
        if cycle is None:
            raise ForecastNotFoundError(f"forecast cycle {cycle_id!r} does not exist")
        forecasts = self.store.forecasts(cycle_id)
        evaluations = self.store.evaluations(cycle_id)
        outcomes = self.store.outcomes(cycle_id)
        calendar = calendar_by_mic(cycle.exchange)
        return {
            "cycle": cycle.to_dict(),
            "checkpoints": [item.to_dict() for item in self.store.checkpoints(cycle_id)],
            "forecasts": [item.to_dict() for item in forecasts],
            "stages": [item.to_dict() for item in self.store.stages(cycle_id)],
            "evaluations": [item.to_dict() for item in evaluations],
            "outcomes": [item.to_dict() for item in outcomes],
            "revisions": [item.revision for item in self.store.checkpoints(cycle_id) if item.revision],
            "session": session_snapshot(calendar, self._now()) if calendar is not None else None,
            "authority": {
                "forecast_only": True,
                "risk_authority": "NM-08",
                "execution_authority": "NM-10",
            },
        }

    def accuracy(
        self,
        *,
        instrument_id: str | None = None,
        horizon: str | None = None,
        stage: str | None = None,
        cycle_id: str | None = None,
        model_id: str | None = None,
        model_version: str | None = None,
        ensemble_id: str | None = None,
        target_date: str | None = None,
        regime: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        outcomes = self.store.forecast_outcomes(
            instrument_id=instrument_id,
            horizon=horizon,
            stage=stage,
            cycle_id=cycle_id,
            limit=limit,
        )
        if target_date:
            outcomes = [item for item in outcomes if item.target_at.date().isoformat() == str(target_date)[:10]]
        if model_id:
            outcomes = [item for item in outcomes if item.metadata.get("model_id") == model_id]
        if model_version:
            outcomes = [item for item in outcomes if item.metadata.get("model_version") == model_version]
        if ensemble_id:
            outcomes = [item for item in outcomes if item.metadata.get("ensemble_id") == ensemble_id]
        if regime:
            outcomes = [item for item in outcomes if item.metadata.get("regime") == regime]
        cycles = self.store.forecast_cycles(
            instrument_id=instrument_id,
            horizon=horizon,
            cycle_id=cycle_id,
            limit=200,
        )
        by_stage: dict[str, dict[str, Any]] = {}
        for confirmation_stage in ConfirmationStage:
            rows = [item for item in outcomes if item.stage is confirmation_stage]
            by_stage[confirmation_stage.value] = self._summarize_outcomes(rows)
        checkpoints = [item for cycle in cycles for item in self.store.checkpoints(cycle.cycle_id)]
        confirmation_total = sum(1 for item in checkpoints if item.kind is CheckpointKind.FINAL)
        confirmation_ok = sum(
            1
            for item in checkpoints
            if item.kind is CheckpointKind.FINAL
            and item.status in {CheckpointState.COMPLETED, CheckpointState.EVALUATED}
        )
        forecast_checkpoints = [item for item in checkpoints if item.kind is not CheckpointKind.OUTCOME]
        abstained = sum(1 for item in forecast_checkpoints if item.status is CheckpointState.ABSTAINED)
        forecast_rows = [
            forecast
            for cycle in cycles
            for forecast in self.store.forecasts(cycle.cycle_id)
            if not stage or forecast.lifecycle_stage.value == stage
        ]
        if model_id:
            forecast_rows = [item for item in forecast_rows if item.model_id == model_id]
        if model_version:
            forecast_rows = [item for item in forecast_rows if item.model_version == model_version]
        if ensemble_id:
            forecast_rows = [item for item in forecast_rows if item.ensemble_id == ensemble_id]
        if regime:
            forecast_rows = [
                item
                for item in forecast_rows
                if str(item.metadata.get("regime") or "unclassified") == regime
            ]
        outcome_ids = {outcome.forecast_id for outcome in outcomes}
        missing = sum(1 for item in forecast_rows if item.forecast_id not in outcome_ids)
        summaries = list(by_stage.values())
        sample = sum(int(item["sample_size"]) for item in summaries)
        return {
            "phase": PI09_PHASE,
            "instrument_id": instrument_id or "",
            "horizon": horizon or "",
            "stage": stage or "",
            "cycle_id": cycle_id or "",
            "model_id": model_id or "",
            "model_version": model_version or "",
            "ensemble_id": ensemble_id or "",
            "target_date": target_date or "",
            "regime": regime or "",
            "sample_size": sample,
            "reliable": sample >= self.config.small_sample_size,
            "reliability_note": (
                "sample is below the configured small-sample threshold; do not treat it as reliable model performance"
                if sample < self.config.small_sample_size
                else "sample meets the configured minimum; it remains historical evidence, not a guarantee"
            ),
            "by_stage": by_stage,
            "confirmation_success_rate": (confirmation_ok / confirmation_total) if confirmation_total else None,
            "abstention_frequency": (abstained / len(forecast_checkpoints)) if forecast_checkpoints else None,
            "missing_evaluation_frequency": (missing / len(forecast_rows)) if forecast_rows else 0.0,
            "counts": {
                "cycles": len(cycles),
                "checkpoints": len(checkpoints),
                "forecasts": len(forecast_rows),
                "outcomes": len(outcomes),
            },
            "financial_authority": "none",
            "paper_trading_pnl": "not_combined_with_forecast_accuracy",
        }

    def _summarize_outcomes(self, rows: Sequence[ForecastOutcome]) -> dict[str, Any]:
        if not rows:
            return {
                "sample_size": 0,
                "direction_accuracy": None,
                "price_mae": None,
                "return_mae": None,
                "interval_coverage": None,
                "mean_interval_width": None,
                "direction_counts": {},
                "actual_direction_counts": {},
                "directional_sample_size": 0,
                "directional_accuracy": None,
                "abstention_count": 0,
                "reliable": False,
            }
        correct = sum(1 for item in rows if item.direction_correct)
        covered = sum(1 for item in rows if item.inside_interval)
        directional = [item for item in rows if item.predicted_direction in {Direction.UP.value, Direction.DOWN.value, Direction.NEUTRAL.value}]
        return {
            "sample_size": len(rows),
            "direction_accuracy": correct / len(rows),
            "directional_sample_size": len(directional),
            "directional_accuracy": (sum(1 for item in directional if item.direction_correct) / len(directional)) if directional else None,
            "price_mae": sum(float(item.absolute_price_error) for item in rows) / len(rows),
            "return_mae": sum(item.absolute_return_error for item in rows) / len(rows),
            "interval_coverage": covered / len(rows),
            "mean_interval_width": sum(item.interval_width for item in rows) / len(rows),
            "direction_counts": dict(Counter(item.predicted_direction for item in rows)),
            "actual_direction_counts": dict(Counter(item.actual_direction for item in rows)),
            "abstention_count": sum(1 for item in rows if item.predicted_direction in {Direction.UNCERTAIN.value, Direction.INVALID.value}),
            "reliable": len(rows) >= self.config.small_sample_size,
        }

    def pending_evaluations(self) -> list[dict[str, Any]]:
        cycles = self.store.forecast_cycles(limit=200)
        return [
            item.to_dict()
            for cycle in cycles
            for item in self.store.checkpoints(cycle.cycle_id)
            if item.kind is CheckpointKind.OUTCOME
            and item.status in {CheckpointState.PENDING, CheckpointState.DEAD_LETTER}
        ]


__all__ = ["ForecastScheduler"]