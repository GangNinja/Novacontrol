"""AT-02 session-aware monitoring and discovery.

The runner composes two accepted components and adds no financial logic:

* the AT-01 :class:`AutonomyRuntime` owns the mode, the durable lease and the
  one-active-cycle invariant;
* the NM-05 :class:`AssetDiscoveryEngine` performs discovery.

Everything here is a read or a monitoring record.  There is no strategy,
proposal, risk, order, fill, portfolio, wallet or ledger call, and the module
deliberately imports none of them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from novacontrol.novamarket.autonomy.models import (
    MonitoringConfig,
    MonitoringCycleStatus,
    MonitoringErrorCode,
    MonitoringTrigger,
)
from novacontrol.novamarket.autonomy.runtime import AutonomyRuntime
from novacontrol.novamarket.autonomy.session import (
    SessionDecision,
    canonical_mic,
    evaluate_session,
)
from novacontrol.novamarket.discovery.models import TriggerKind, UniverseFilter

BATCH_CAP = 20
REFRESH_FLOOR_SECONDS = 900

_CODE_FOR_VERDICT: dict[str, str] = {
    "exchange_holiday": MonitoringErrorCode.EXCHANGE_HOLIDAY.value,
    "calendar_unavailable": MonitoringErrorCode.CALENDAR_UNAVAILABLE.value,
    "calendar_unverified": MonitoringErrorCode.CALENDAR_UNVERIFIED.value,
    "weekend": MonitoringErrorCode.OUTSIDE_TRADING_SESSION.value,
    "market_closed": MonitoringErrorCode.OUTSIDE_TRADING_SESSION.value,
    "market_not_open": MonitoringErrorCode.OUTSIDE_TRADING_SESSION.value,
    "unknown": MonitoringErrorCode.CALENDAR_UNAVAILABLE.value,
}


class MonitoringRefused(RuntimeError):
    """A monitoring cycle refused with a stable reason code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _canonical_instrument_id(value: object) -> str:
    """Return ``MIC:SYMBOL`` for a complete identity, else an empty string."""

    text = str(value or "").strip()
    if ":" not in text:
        return ""
    exchange, symbol = text.split(":", 1)
    mic = canonical_mic(exchange)
    if not mic or not symbol.strip():
        return ""
    return f"{mic}:{symbol.strip()}"


def _parse(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _freshness_state(counts: dict[str, int]) -> str:
    """Map NM-05 data quality to one honest freshness label."""

    if counts.get("good"):
        return "fresh"
    if counts.get("partial"):
        return "delayed"
    if counts.get("stale"):
        return "stale"
    return "unavailable"


def _lease_valid(snapshot: dict[str, Any]) -> bool:
    lease = snapshot.get("lease")
    return bool(lease.get("valid")) if isinstance(lease, dict) else False


def closed_monitoring_status() -> dict[str, Any]:
    """The honest AT-02 view when the subsystem is not running."""

    session = evaluate_session("XNSE", datetime.now(UTC))
    return {
        "ok": True,
        "initialised": False,
        "milestone": "AT-02",
        "phase": "AT-02",
        "implemented": True,
        "trading_implemented": False,
        "orders_possible": False,
        "paper_only": True,
        "live_trading": False,
        "broker_connected": False,
        "mode": "disabled",
        "runtime_state": "stopped",
        "lease_valid": False,
        "config": MonitoringConfig().to_dict(),
        "session": session.to_dict(),
        "scheduler": {
            "running": False,
            "hosted_by": "novamarket_runtime",
            "interval_seconds": MonitoringConfig().interval_seconds,
            "last_attempt_at": None,
            "next_due_at": None,
        },
        "last_cycle": None,
        "recent_cycles": [],
        "data_freshness": {
            "state": "unavailable",
            "counts": {},
            "source": "NM-05 evaluation.data_quality",
        },
        "rotation": {"cursor": 0, "batch_size": BATCH_CAP, "cap": BATCH_CAP},
        "health": {
            "state": "stopped",
            "reasons": ["the NovaMarket subsystem is not running"],
            "consecutive_failures": 0,
        },
        "last_error": "",
        "last_error_code": "",
    }


class AutonomyMonitoring:
    """One monitoring runner per AT-01 runtime; never a second scheduler."""

    def __init__(
        self,
        runtime: AutonomyRuntime,
        *,
        discovery: Callable[[], Any | None],
        clock: Callable[[], datetime] | None = None,
        instrument_lookup: Callable[[str], Any] | None = None,
        universe: Callable[[], Sequence[str]] | None = None,
    ) -> None:
        self._runtime = runtime
        self._store = runtime.store
        self._discovery = discovery
        self._clock = clock or (lambda: datetime.now(UTC))
        self._instrument_lookup = instrument_lookup
        self._universe = universe or (lambda: ())
        self._lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return self._runtime.closed

    def _now(self) -> datetime:
        moment = self._clock()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("the autonomy clock must return a timezone-aware datetime")
        return moment.astimezone(UTC)

    def _config(self) -> MonitoringConfig:
        return self._store.monitoring_config()

    def config(self) -> MonitoringConfig:
        """Public read used by the service's session endpoint."""

        return self._config()

    def save_config(self, config: MonitoringConfig) -> MonitoringConfig:
        """Persist a validated monitoring configuration."""

        return self._store.save_monitoring_config(config, now=self._now())

    # -- reads ---------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Server-authoritative monitoring status; no order claim is possible."""

        if self.closed:
            return closed_monitoring_status()
        config = self._config()
        state = self._store.monitoring_state()
        runtime = self._runtime.status()
        now = self._now()
        decision = evaluate_session(config.exchange, now)
        last = self._store.latest_monitoring_cycle()
        recent = self._store.monitoring_cycles(limit=10)
        quality_counts: dict[str, int] = {}
        if last is not None:
            for candidate in self._store.monitoring_candidates(
                str(last["cycle_id"]), limit=2000
            ):
                quality = str(candidate.get("data_quality") or "").strip().lower()
                quality = quality or "unavailable"
                quality_counts[quality] = quality_counts.get(quality, 0) + 1
        mode = str(runtime.get("mode") or "disabled")
        lease_valid = _lease_valid(runtime)
        scheduler_running = bool(
            config.enabled and lease_valid and mode in ("monitoring", "autonomous")
        )
        last_attempt = _parse(state["last_attempt_at"])
        next_due = (
            last_attempt.timestamp() + config.interval_seconds
            if last_attempt is not None
            else None
        )
        reasons: list[str] = []
        if not config.enabled:
            reasons.append("monitoring is disabled by configuration")
        if mode not in ("monitoring", "autonomous"):
            reasons.append(f"runtime mode is {mode}")
        if not lease_valid:
            reasons.append("the runtime lease is not valid")
        if int(state["consecutive_failures"]) > 0:
            reasons.append(
                f"{state['consecutive_failures']} consecutive monitoring failure(s)"
            )
        if last is not None and str(last.get("status")) == "failed":
            reasons.append("the last monitoring cycle failed")
        health_state = "degraded" if reasons else "ok"
        return {
            "ok": health_state == "ok",
            "initialised": True,
            "milestone": "AT-02",
            "phase": "AT-02",
            "implemented": True,
            "trading_implemented": False,
            "orders_possible": False,
            "paper_only": True,
            "live_trading": False,
            "broker_connected": False,
            "mode": mode,
            "runtime_state": runtime.get("runtime_state"),
            "lease_valid": lease_valid,
            "config": config.to_dict(),
            "session": decision.to_dict(),
            "scheduler": {
                "running": scheduler_running,
                "hosted_by": "novamarket_runtime",
                "interval_seconds": config.interval_seconds,
                "last_attempt_at": state["last_attempt_at"],
                "next_due_at": (
                    datetime.fromtimestamp(next_due, tz=UTC).isoformat()
                    if next_due is not None
                    else None
                ),
                "refresh_floor_seconds": REFRESH_FLOOR_SECONDS,
            },
            "last_cycle": self._cycle_summary(last) if last else None,
            "recent_cycles": [self._cycle_summary(row) for row in recent],
            "data_freshness": {
                "state": _freshness_state(quality_counts),
                "counts": quality_counts,
                "source": "NM-05 evaluation.data_quality",
            },
            "rotation": {
                "cursor": int(state["rotation_cursor"]),
                "batch_size": config.max_instruments_per_cycle,
                "cap": BATCH_CAP,
            },
            "health": {
                "state": health_state,
                "reasons": reasons,
                "consecutive_failures": int(state["consecutive_failures"]),
                "last_error_code": state["last_error_code"],
            },
            "last_error": state["last_error"],
            "last_error_code": state["last_error_code"],
        }

    def cycles(self, *, limit: int = 20) -> dict[str, Any]:
        rows = [self._cycle_summary(row) for row in self._store.monitoring_cycles(limit=limit)]
        return {"ok": True, "cycles": rows, "total": len(rows)}

    def cycle(self, cycle_id: str) -> dict[str, Any]:
        row = self._store.monitoring_cycle(cycle_id)
        if row is None:
            raise MonitoringRefused(
                "CYCLE_NOT_FOUND", f"unknown monitoring cycle {cycle_id!r}"
            )
        return {
            "ok": True,
            **self._cycle_summary(row),
            "candidates": self._store.monitoring_candidates(cycle_id, limit=2000),
        }

    def contract(self) -> dict[str, Any]:
        return {
            "milestone": "AT-02",
            "status": "session-aware monitoring and discovery; no trading",
            "implemented": True,
            "trading_implemented": False,
            "orders_possible": False,
            "paper_only": True,
            "live_trading": False,
            "broker_connected": False,
            "session_evaluator": "verified exchange calendar only, fail closed",
            "calendar_years": [2026],
            "batch_cap": BATCH_CAP,
            "refresh_floor_seconds": REFRESH_FLOOR_SECONDS,
            "one_cycle_at_a_time": True,
            "scheduler": "hosted by the single NovaMarket runtime tick",
            "data_quality_labels": ["good", "partial", "stale", "insufficient"],
            "cycle_statuses": [item.value for item in MonitoringCycleStatus],
            "error_codes": [item.value for item in MonitoringErrorCode],
            "triggers": [item.value for item in MonitoringTrigger],
            "financial_mutation": "none",
        }

    @staticmethod
    def _cycle_summary(row: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "cycle_id",
            "autonomy_cycle_id",
            "owner_id",
            "mode",
            "exchange",
            "trading_date",
            "calendar_verified",
            "calendar_source",
            "session_state",
            "session_eligible",
            "phase",
            "status",
            "trigger",
            "started_at",
            "finished_at",
            "discovered_count",
            "eligible_count",
            "processed_count",
            "deferred_count",
            "rejected_count",
            "rotation_start",
            "rotation_end",
            "error_code",
            "error",
        )
        body = {key: row.get(key) for key in fields}
        body["calendar_verified"] = bool(body["calendar_verified"])
        body["session_eligible"] = bool(body["session_eligible"])
        return body

    # -- scheduling ----------------------------------------------------------

    async def maybe_run(self) -> dict[str, Any]:
        """Run one cycle if the mode, lease, config and cadence all allow it."""

        if self.closed:
            return self._refusal("RUNTIME_DISABLED", "the runtime is closed")
        config = self._config()
        if not config.enabled:
            return self._refusal(
                MonitoringErrorCode.MONITORING_DISABLED.value,
                "monitoring is disabled by configuration",
            )
        runtime = self._runtime.status()
        mode = str(runtime.get("mode") or "disabled")
        if mode == "disabled":
            return self._refusal(
                MonitoringErrorCode.RUNTIME_DISABLED.value,
                "the runtime is disabled; no monitoring cycle may run",
            )
        if mode == "paused":
            return self._refusal(
                MonitoringErrorCode.RUNTIME_PAUSED.value,
                "paused mode runs no new discovery cycles",
            )
        if not _lease_valid(runtime):
            return self._refusal(
                MonitoringErrorCode.LEASE_INVALID.value,
                "the runtime lease is not valid",
            )
        state = self._store.monitoring_state()
        last = _parse(state["last_attempt_at"])
        if last is not None:
            elapsed = (self._now() - last).total_seconds()
            if elapsed < config.interval_seconds:
                return {
                    "ok": True,
                    "status": "not_due",
                    "retry_after_seconds": round(config.interval_seconds - elapsed, 1),
                    "trading_implemented": False,
                }
        return await self.run_cycle(trigger=MonitoringTrigger.SCHEDULED.value)

    async def run_cycle(self, *, trigger: str = "manual") -> dict[str, Any]:
        """Run one monitoring cycle; concurrent calls are refused, not queued."""

        if self._lock.locked():
            return self._refusal(
                MonitoringErrorCode.ALREADY_RUNNING.value,
                "a monitoring cycle is already running",
            )
        async with self._lock:
            return await self._run_locked(trigger)

    async def _run_locked(self, trigger: str) -> dict[str, Any]:
        now = self._now()
        if self.closed:
            return self._refusal("RUNTIME_DISABLED", "the runtime is closed")
        config = self._config()
        if not config.enabled:
            return self._refusal(
                MonitoringErrorCode.MONITORING_DISABLED.value,
                "monitoring is disabled by configuration",
            )
        runtime = self._runtime.status()
        mode = str(runtime.get("mode") or "disabled")
        if mode == "disabled":
            return self._refusal(
                MonitoringErrorCode.RUNTIME_DISABLED.value,
                "the runtime is disabled; no monitoring cycle may run",
            )
        if mode == "paused":
            return self._refusal(
                MonitoringErrorCode.RUNTIME_PAUSED.value,
                "paused mode runs no new discovery cycles",
            )
        if not _lease_valid(runtime):
            return self._refusal(
                MonitoringErrorCode.LEASE_INVALID.value,
                "the runtime lease is not valid",
            )

        decision = evaluate_session(config.exchange, now)
        self._store.set_monitoring_attempt(now=now)
        if not decision.eligible:
            code = _CODE_FOR_VERDICT.get(
                decision.verdict.value,
                MonitoringErrorCode.OUTSIDE_TRADING_SESSION.value,
            )
            return self._defer(decision, mode, trigger, code, now)
        return await self._execute(config, decision, mode, trigger, now)

    # -- cycle execution -----------------------------------------------------

    async def _execute(
        self,
        config: MonitoringConfig,
        decision: SessionDecision,
        mode: str,
        trigger: str,
        now: datetime,
    ) -> dict[str, Any]:
        cycle_id = "mon_" + uuid4().hex
        cycle_key = (
            f"monitoring:{config.exchange}:{decision.trading_date}:{cycle_id}"
        )
        state = self._store.monitoring_state()
        cursor = int(state["rotation_cursor"])
        self._store.create_monitoring_cycle(
            {
                "cycle_id": cycle_id,
                "autonomy_cycle_id": "",
                "cycle_key": cycle_key,
                "owner_id": self._runtime.owner_id,
                "mode": mode,
                "exchange": config.exchange,
                "trading_date": decision.trading_date,
                "calendar_verified": decision.calendar_verified,
                "calendar_source": decision.calendar_source,
                "session_state": decision.session_state,
                "session_eligible": True,
                "phase": MonitoringCycleStatus.CALENDAR_VALIDATION.value,
                "status": "running",
                "trigger": trigger,
                "started_at": now.isoformat(),
                "rotation_start": cursor,
            },
            now=now,
        )
        autonomy_cycle_id = ""
        try:
            claimed = self._runtime.claim_cycle(cycle_id, cycle_key)
            autonomy_cycle_id = str(claimed.get("cycle_id") or cycle_id)
            self._store.update_monitoring_cycle(
                cycle_id,
                now=now,
                autonomy_cycle_id=autonomy_cycle_id,
                phase=MonitoringCycleStatus.DISCOVERY.value,
                status="running",
            )
            report, candidates = await self._run_discovery(config)
            self._store.update_monitoring_cycle(
                cycle_id,
                now=now,
                phase=MonitoringCycleStatus.ELIGIBILITY_FILTERING.value,
            )
            configured = tuple(
                str(item or "").strip().upper()
                for item in self._universe()
                if str(item or "").strip()
            )
            accepted, rejected = self._normalise(
                candidates, config.exchange, configured
            )
            self._store.update_monitoring_cycle(
                cycle_id,
                now=now,
                phase=MonitoringCycleStatus.BATCH_PREPARATION.value,
            )
            window, rotation_start, next_cursor = self._rotate(
                accepted, cursor, config.max_instruments_per_cycle
            )
            rotation_end = rotation_start + len(window)
            processed_ids = {row["instrument_id"] for row in window}
            candidate_rows: list[dict[str, Any]] = []
            for rank, row in enumerate(window, start=1):
                candidate_rows.append(
                    {
                        **row,
                        "disposition": "processed",
                        "reason": "",
                        "rank": rank,
                    }
                )
            deferred = [
                row for row in accepted if row["instrument_id"] not in processed_ids
            ]
            for rank, row in enumerate(deferred, start=len(window) + 1):
                candidate_rows.append(
                    {
                        **row,
                        "disposition": "deferred",
                        "reason": "beyond the current rotation batch",
                        "rank": rank,
                    }
                )
            candidate_rows.extend(rejected)
            self._store.record_monitoring_candidates(cycle_id, candidate_rows)

            discovered = len(accepted) + len(rejected)
            failed_sources = int(report.get("failed_sources") or 0)
            error_code = ""
            error = ""
            if failed_sources and not accepted:
                raise MonitoringRefused(
                    MonitoringErrorCode.DISCOVERY_PROVIDER_FAILURE.value,
                    "discovery reported provider failures and produced no candidates",
                )
            if failed_sources:
                error_code = MonitoringErrorCode.DISCOVERY_PROVIDER_FAILURE.value
                error = str(report.get("errors") or "a discovery source failed")[:500]
            elif not accepted:
                error_code = MonitoringErrorCode.NO_CANDIDATES.value
                error = "discovery produced no eligible candidates"
            self._store.update_monitoring_cycle(
                cycle_id,
                now=self._now(),
                phase=MonitoringCycleStatus.MONITORING.value,
                status="running",
                discovered_count=discovered,
                eligible_count=len(accepted),
                processed_count=len(window),
                deferred_count=len(deferred),
                rejected_count=len(rejected),
                rotation_start=rotation_start,
                rotation_end=rotation_end,
                error_code=error_code,
                error=error,
            )
            self._store.set_rotation(next_cursor, now=self._now())
            finished = self._now()
            self._store.update_monitoring_cycle(
                cycle_id,
                now=finished,
                phase=MonitoringCycleStatus.COMPLETED.value,
                status="completed",
                finished=True,
            )
            self._store.note_monitoring_success(cycle_id, now=finished)
            self._runtime.finish_cycle(autonomy_cycle_id, status="completed")
            return {
                "ok": True,
                "status": "completed",
                "cycle_id": cycle_id,
                "autonomy_cycle_id": autonomy_cycle_id,
                "session": decision.to_dict(),
                "discovered": discovered,
                "eligible": len(accepted),
                "processed": len(window),
                "deferred": len(deferred),
                "rejected": len(rejected),
                "rotation_cursor": next_cursor,
                "error_code": error_code,
                "error": error,
                "trading_implemented": False,
                "orders_possible": False,
            }
        except Exception as exc:  # noqa: BLE001 - a cycle must report, not crash the runtime
            code = str(getattr(exc, "code", MonitoringErrorCode.DISCOVERY_PROVIDER_FAILURE.value))
            message = str(exc) or type(exc).__name__
            failed_at = self._now()
            try:
                self._store.update_monitoring_cycle(
                    cycle_id,
                    now=failed_at,
                    phase=MonitoringCycleStatus.CANCELLED.value,
                    status="failed",
                    error_code=code,
                    error=message,
                    finished=True,
                )
            except Exception:  # noqa: BLE001 - preserving the original failure matters more
                pass
            if autonomy_cycle_id:
                try:
                    self._runtime.finish_cycle(
                        autonomy_cycle_id, status="failed", error=message
                    )
                except Exception:  # noqa: BLE001
                    pass
            try:
                self._store.note_monitoring_failure(code, message, now=failed_at)
            except Exception:  # noqa: BLE001
                pass
            return {
                "ok": False,
                "status": "failed",
                "cycle_id": cycle_id,
                "session": decision.to_dict(),
                "error_code": code,
                "error": message,
                "trading_implemented": False,
                "orders_possible": False,
            }

    def _defer(
        self,
        decision: SessionDecision,
        mode: str,
        trigger: str,
        code: str,
        now: datetime,
    ) -> dict[str, Any]:
        cycle_id = "mon_" + uuid4().hex
        state = self._store.monitoring_state()
        cursor = int(state["rotation_cursor"])
        self._store.create_monitoring_cycle(
            {
                "cycle_id": cycle_id,
                "cycle_key": f"deferred:{decision.exchange}:{now.isoformat()}:{cycle_id}",
                "owner_id": self._runtime.owner_id,
                "mode": mode,
                "exchange": decision.exchange,
                "trading_date": decision.trading_date,
                "calendar_verified": decision.calendar_verified,
                "calendar_source": decision.calendar_source,
                "session_state": decision.session_state,
                "session_eligible": False,
                "phase": MonitoringCycleStatus.SESSION_CHECK.value,
                "status": MonitoringCycleStatus.DEFERRED.value,
                "trigger": trigger,
                "started_at": now.isoformat(),
                "rotation_start": cursor,
                "rotation_end": cursor,
                "error_code": code,
                "error": decision.reason,
            },
            now=now,
        )
        self._store.update_monitoring_cycle(
            cycle_id,
            now=now,
            status=MonitoringCycleStatus.DEFERRED.value,
            finished=True,
        )
        return {
            "ok": True,
            "status": "deferred",
            "cycle_id": cycle_id,
            "session": decision.to_dict(),
            "reason_code": code,
            "reason": decision.reason,
            "trading_implemented": False,
            "orders_possible": False,
        }

    async def _run_discovery(
        self, config: MonitoringConfig
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        engine = self._discovery()
        if engine is None:
            raise MonitoringRefused(
                MonitoringErrorCode.MARKET_DATA_UNAVAILABLE.value,
                "the discovery engine or market data is not initialised",
            )
        # Discovery is scoped per cycle: the shared engine is also the manual
        # discovery surface, so mutating its UniverseFilter here would leak the
        # monitoring exchange into unrelated runs.
        base = getattr(engine, "universe", None)
        configured = tuple(
            str(item or "").strip().upper()
            for item in self._universe()
            if str(item or "").strip()
        )
        scope = UniverseFilter(
            asset_classes=tuple(getattr(base, "asset_classes", ("equity", "etf", "index"))),
            exchanges=(config.exchange,),
            max_instruments=config.max_instruments_per_cycle,
            exclude_unknown_trading_status=bool(
                getattr(base, "exclude_unknown_trading_status", False)
            ),
        )
        last_error: Exception | None = None
        for _attempt in range(config.retry_max + 1):
            try:
                result = await asyncio.wait_for(
                    engine.run_cycle(
                        force=True,
                        trigger=TriggerKind.MANUAL,
                        universe=scope,
                        instrument_ids=configured or None,
                    ),
                    timeout=float(config.cycle_timeout_seconds),
                )
                if isinstance(result, dict):
                    return result, self._candidate_rows(engine, result)
                raise MonitoringRefused(
                    MonitoringErrorCode.DISCOVERY_PROVIDER_FAILURE.value,
                    "the discovery engine returned an unexpected result",
                )
            except asyncio.TimeoutError:
                last_error = MonitoringRefused(
                    MonitoringErrorCode.CYCLE_TIMEOUT.value,
                    "the discovery cycle exceeded the configured timeout",
                )
            except Exception as exc:  # noqa: BLE001 - retried, then reported
                last_error = exc
        if isinstance(last_error, MonitoringRefused):
            raise last_error
        raise MonitoringRefused(
            MonitoringErrorCode.DISCOVERY_PROVIDER_FAILURE.value,
            f"{type(last_error).__name__}: {last_error}",
        )

    @staticmethod
    def _candidate_rows(
        engine: Any, report: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Read the existing NM-05 candidate store, never a second discovery."""

        rows = report.get("candidates")
        if isinstance(rows, list) and rows:
            return list(rows)
        finder = getattr(engine, "candidates", None)
        if not callable(finder):
            return []
        try:
            body = finder(limit=500)
        except Exception:  # noqa: BLE001 - a read failure is an empty candidate set
            return []
        rows = body.get("candidates") if isinstance(body, dict) else None
        return list(rows) if isinstance(rows, list) else []

    def _normalise(
        self,
        rows: list[dict[str, Any]],
        exchange: str,
        universe: Sequence[str] = (),
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        seen: set[str] = set()
        allowed = {str(item or "").strip().upper() for item in universe}
        for row in rows:
            if not isinstance(row, dict):
                rejected.append(
                    {
                        "instrument_id": "",
                        "symbol": "",
                        "exchange": exchange,
                        "disposition": "rejected",
                        "reason": "INVALID_INSTRUMENT: not an object",
                        "rank": 0,
                    }
                )
                continue
            instrument_id = str(row.get("instrument_id") or "").strip()
            symbol = str(row.get("symbol") or "").strip()
            row_exchange = canonical_mic(str(row.get("exchange") or exchange))
            if not instrument_id:
                rejected.append(
                    {
                        "instrument_id": "",
                        "symbol": symbol,
                        "exchange": row_exchange,
                        "disposition": "rejected",
                        "reason": "INVALID_INSTRUMENT: blank instrument_id",
                        "rank": 0,
                    }
                )
                continue
            if instrument_id in seen:
                rejected.append(
                    {
                        "instrument_id": instrument_id,
                        "symbol": symbol,
                        "exchange": row_exchange,
                        "disposition": "rejected",
                        "reason": "duplicate instrument_id",
                        "rank": 0,
                    }
                )
                continue
            seen.add(instrument_id)
            if allowed and instrument_id.upper() not in allowed:
                rejected.append(
                    {
                        "instrument_id": instrument_id,
                        "symbol": symbol,
                        "exchange": row_exchange,
                        "disposition": "rejected",
                        "reason": (
                            "OUTSIDE_CONFIGURED_UNIVERSE: candidate is not in the "
                            "configured instrument allow-list"
                        ),
                        "rank": 0,
                    }
                )
                continue
            if self._instrument_lookup is not None:
                try:
                    instrument = self._instrument_lookup(instrument_id)
                except Exception as exc:  # noqa: BLE001 - one bad row is not a cycle failure
                    rejected.append(
                        {
                            "instrument_id": instrument_id,
                            "symbol": symbol,
                            "exchange": row_exchange,
                            "disposition": "rejected",
                            "reason": (
                                f"INVALID_INSTRUMENT: {type(exc).__name__}: {exc}"
                            )[:300],
                            "rank": 0,
                        }
                    )
                    continue
                if isinstance(instrument, dict):
                    looked_up_identity = str(
                        instrument.get("instrument_id") or ""
                    ).strip()
                    if looked_up_identity and (
                        _canonical_instrument_id(looked_up_identity)
                        != _canonical_instrument_id(instrument_id)
                    ):
                        rejected.append(
                            {
                                "instrument_id": instrument_id,
                                "symbol": symbol,
                                "exchange": row_exchange,
                                "disposition": "rejected",
                                "reason": (
                                    "INVALID_INSTRUMENT: lookup returned "
                                    f"{looked_up_identity!r} for {instrument_id!r}"
                                ),
                                "rank": 0,
                            }
                        )
                        continue
                    symbol = str(instrument.get("symbol") or symbol)
                    row_exchange = canonical_mic(
                        str(
                            instrument.get("mic")
                            or instrument.get("exchange")
                            or row_exchange
                        )
                    )
            if row_exchange != exchange:
                rejected.append(
                    {
                        "instrument_id": instrument_id,
                        "symbol": symbol,
                        "exchange": row_exchange,
                        "disposition": "rejected",
                        "reason": (
                            f"EXCHANGE_MISMATCH: candidate {row_exchange} "
                            f"does not belong to configured {exchange}"
                        ),
                        "rank": 0,
                    }
                )
                continue
            quality = row.get("data_quality")
            evaluation = row.get("evaluation")
            if not quality and isinstance(evaluation, dict):
                quality = evaluation.get("data_quality")
            accepted.append(
                {
                    "instrument_id": instrument_id,
                    "symbol": symbol,
                    "exchange": row_exchange,
                    "data_quality": str(quality or "").strip().lower(),
                }
            )
        accepted.sort(key=lambda item: item["instrument_id"])
        rejected.sort(key=lambda item: item["instrument_id"])
        return accepted, rejected

    @staticmethod
    def _rotate(
        accepted: list[dict[str, Any]], cursor: int, batch: int
    ) -> tuple[list[dict[str, Any]], int, int]:
        total = len(accepted)
        if total == 0:
            return [], 0, 0
        start = cursor if 0 <= cursor < total else 0
        window = accepted[start : start + max(1, min(batch, BATCH_CAP))]
        end = start + len(window)
        next_cursor = 0 if end >= total else end
        return window, start, next_cursor

    @staticmethod
    def _refusal(code: str, message: str) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "refused",
            "reason_code": code,
            "reason": message,
            "trading_implemented": False,
            "orders_possible": False,
        }


__all__ = ["AutonomyMonitoring", "MonitoringRefused", "closed_monitoring_status"]
