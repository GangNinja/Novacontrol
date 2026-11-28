"""Autonomous asset discovery, evaluation and watchlist maintenance (NM-05).

The engine composes what earlier phases already provide: NM-02's canonical
instruments, quotes and bars, and NM-04's evidenced market events. It owns only
the discovery decision — universe filtering, scoring, membership and lifecycle.

It never places an order, sizes a position or allocates capital; there is no
code path from a watchlist entry to an execution surface.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Sequence

from novacontrol.novamarket.contracts import (
    DiscoveryNotFoundError,
    DiscoveryValidationError,
)
from novacontrol.novamarket.discovery import scoring as scoring_module
from novacontrol.novamarket.discovery.models import (
    METHODOLOGY_VERSION,
    CandidateEvaluation,
    CycleReport,
    DataQuality,
    DiscoveryPolicy,
    DiscoveryReason,
    EntryStatus,
    ScoreFactor,
    TriggerKind,
    UniverseFilter,
    WatchlistEntry,
    WatchlistKind,
    ensure_entry_transition,
    entry_id_for,
    utcnow,
)
from novacontrol.novamarket.discovery.sessions import (
    SessionState,
    session_state,
    sessions_for,
)
from novacontrol.novamarket.discovery.store import DiscoveryStore

_LOGGER = logging.getLogger("novacontrol.novamarket.discovery")

Publisher = Callable[[str, dict[str, Any]], Awaitable[None]]

#: Watchlists that exist because their selection rule differs, not for decoration.
DEFAULT_WATCHLISTS: dict[WatchlistKind, tuple[str, str]] = {
    WatchlistKind.DISCOVERY: (
        "Discovery candidates",
        "Every instrument that cleared the discovery threshold, ranked by score.",
    ),
    WatchlistKind.ACTIVITY: (
        "High market activity",
        "Candidates whose recent price movement is unusually large.",
    ),
    WatchlistKind.EVENT_DRIVEN: (
        "Event-driven candidates",
        "Candidates affected by an officially confirmed or corroborated NM-04 event.",
    ),
    WatchlistKind.LIQUIDITY: (
        "High liquidity",
        "Candidates whose recent traded value is high relative to the universe.",
    ),
}

CONFIRMED_STATES = frozenset(
    {"officially_confirmed", "multi_source_corroborated"}
)
SUSPENDED_TRADING = frozenset({"halted", "delisted"})


class AssetDiscoveryEngine:
    """Discovery cycles, candidate scoring and persistent watchlists."""

    def __init__(
        self,
        store: DiscoveryStore,
        *,
        market_data: Any | None = None,
        radar: Any | None = None,
        universe: UniverseFilter | None = None,
        policy: DiscoveryPolicy | None = None,
        publisher: Publisher | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._market_data = market_data
        self._radar = radar
        self._last_universe_source = "catalogue"
        self.universe = universe or UniverseFilter()
        self.policy = policy or DiscoveryPolicy()
        self._publisher = publisher
        self._clock = clock or utcnow
        self._last_error = ""
        self._last_cycle_at: datetime | None = None
        self._cycles = 0
        self._cycle_lock = asyncio.Lock()
        self._scheduler_task: asyncio.Task[Any] | None = None
        self._scheduler_stop: asyncio.Event | None = None
        self._scheduler_interval = 0
        self._scheduler_cycles = 0
        self._scheduler_errors = 0
        self._sessions: dict[str, str] = {}
        self._last_exchanges: tuple[str, ...] = ()
        self._ensure_default_watchlists()

    # -- construction helpers -------------------------------------------------

    def _now(self) -> datetime:
        return self._clock()

    def _ensure_default_watchlists(self) -> None:
        for kind, (name, description) in DEFAULT_WATCHLISTS.items():
            self._store.ensure_watchlist(
                kind, name, description=description, at=self._now()
            )

    def set_market_data(self, market_data: Any | None) -> None:
        self._market_data = market_data

    def set_radar(self, radar: Any | None) -> None:
        self._radar = radar

    def set_publisher(self, publisher: Publisher | None) -> None:
        self._publisher = publisher

    async def _publish(self, event_type: str, **payload: Any) -> None:
        if self._publisher is None:
            return
        try:
            await self._publisher(event_type, payload)
        except Exception as exc:  # noqa: BLE001 - a stored decision stands
            self._last_error = f"{event_type}: {type(exc).__name__}: {exc}"
            _LOGGER.warning(
                "novamarket.discovery.event_not_published name=%s error=%s",
                event_type,
                type(exc).__name__,
            )

    # -- data access ----------------------------------------------------------

    def _instrument(self, instrument_id: str) -> dict[str, Any]:
        if self._market_data is None:
            raise DiscoveryNotFoundError(
                "market data is not initialised; discovery cannot read instruments"
            )
        try:
            payload: dict[str, Any] = self._market_data.get_instrument(
                instrument_id
            ).to_dict()
            return payload
        except Exception as exc:  # noqa: BLE001 - one typed error for the caller
            raise DiscoveryNotFoundError(
                f"unknown instrument {instrument_id!r}"
            ) from exc

    def _bars(self, instrument_id: str) -> list[scoring_module.BarPoint]:
        if self._market_data is None:
            return []
        try:
            page = self._market_data.bars(
                instrument_id,
                limit=self.policy.min_history_bars * 4,
                ascending=False,
            )
        except Exception as exc:  # noqa: BLE001 - absent history is not a failure
            _LOGGER.info(
                "novamarket.discovery.no_bars instrument=%s error=%s",
                instrument_id,
                type(exc).__name__,
            )
            return []
        return [
            scoring_module.BarPoint(
                close=float(bar.close),
                volume=float(bar.volume) if bar.volume is not None else None,
                observed_at=bar.observed_at,
            )
            for bar in reversed(page.items)
        ]

    def _quote(self, instrument_id: str) -> tuple[float | None, datetime | None]:
        if self._market_data is None:
            return None, None
        try:
            result = self._market_data.latest(instrument_id)
        except Exception as exc:  # noqa: BLE001 - a quote miss is data, not a crash
            _LOGGER.info(
                "novamarket.discovery.no_quote instrument=%s error=%s",
                instrument_id,
                type(exc).__name__,
            )
            return None, None
        selected = getattr(result, "selected", None)
        if selected is None:
            return None, None
        return float(selected.price), selected.observed_at

    def _events(
        self, instrument_id: str, sectors: Sequence[str]
    ) -> tuple[
        tuple[scoring_module.EventSignal, ...], tuple[scoring_module.EventSignal, ...]
    ]:
        if self._radar is None:
            return (), ()
        direct: list[scoring_module.EventSignal] = []
        seen: set[str] = set()
        try:
            body = self._radar.instrument_events(instrument_id, limit=20)
        except Exception as exc:  # noqa: BLE001 - events are context, not a blocker
            _LOGGER.info(
                "novamarket.discovery.no_events instrument=%s error=%s",
                instrument_id,
                type(exc).__name__,
            )
            body = {"events": []}
        for event in body.get("events", []):
            relationship = "direct"
            for mapping in event.get("mappings", ()):
                if mapping.get("instrument_id") == instrument_id:
                    relationship = str(mapping.get("relationship") or "direct")
                    break
            signal = self._signal(event, relationship)
            if signal is not None:
                direct.append(signal)
                seen.add(signal.event_id)
        sector_signals: list[scoring_module.EventSignal] = []
        for sector in sectors:
            try:
                rows = self._radar.search(
                    _event_query(sector=sector, limit=10)
                )["events"]
            except Exception:  # noqa: BLE001
                continue
            for event in rows:
                event_id = str(event.get("event_id") or "")
                if not event_id or event_id in seen:
                    continue
                signal = self._signal(event, "indirect")
                if signal is not None:
                    sector_signals.append(signal)
                    seen.add(event_id)
        return tuple(direct), tuple(sector_signals)

    @staticmethod
    def _signal(event: dict[str, Any], relationship: str) -> scoring_module.EventSignal | None:
        event_id = str(event.get("event_id") or "")
        if not event_id:
            return None
        published = event.get("published_at")
        try:
            published_at = datetime.fromisoformat(published) if published else None
        except (TypeError, ValueError):
            published_at = None
        return scoring_module.EventSignal(
            event_id=event_id,
            importance=str(event.get("importance") or "low"),
            verification=str(event.get("verification") or "unverified"),
            relationship=relationship,
            published_at=published_at,
        )

    # -- evaluation -----------------------------------------------------------

    def _evaluate(
        self,
        instrument: dict[str, Any],
        *,
        reason: DiscoveryReason,
        at: datetime,
    ) -> tuple[CandidateEvaluation, tuple[scoring_module.EventSignal, ...]]:
        instrument_id = str(instrument["instrument_id"])
        metadata = instrument.get("metadata") or {}
        sectors = [
            str(item).strip()
            for item in (
                metadata.get("sectors")
                or ([metadata.get("sector")] if metadata.get("sector") else [])
            )
            if str(item).strip()
        ]
        events, sector_events = self._events(instrument_id, sectors)
        price, observed = self._quote(instrument_id)
        evaluation = scoring_module.score_candidate(
            instrument_id=instrument_id,
            asset_class=str(instrument.get("asset_class") or "unknown"),
            trading_status=str(instrument.get("trading_status") or "unknown"),
            bars=self._bars(instrument_id),
            quote_price=price,
            quote_observed_at=observed,
            events=events,
            sector_events=sector_events,
            discovery_reason=reason,
            now=at,
            policy=self.policy,
        )
        return evaluation, events

    def evaluate(
        self,
        instrument_id: str,
        *,
        cycle_id: str = "",
        reason: DiscoveryReason = DiscoveryReason.CATALOGUE,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """Evaluate one instrument and persist the immutable record."""
        moment = at or self._now()
        instrument = self._instrument(instrument_id)
        evaluation, _ = self._evaluate(instrument, reason=reason, at=moment)
        self._store.record_evaluation(evaluation, cycle_id=cycle_id)
        created = self._store.upsert_candidate(
            instrument_id,
            at=moment,
            reason=reason.value,
            score=evaluation.score,
            quality=evaluation.data_quality.value,
        )
        return {
            "ok": True,
            "created": created,
            "instrument": instrument,
            **evaluation.to_dict(),
        }

    # -- discovery cycles -----------------------------------------------------

    def _universe_items(
        self,
        limit: int | None = None,
        *,
        universe: UniverseFilter | None = None,
        instrument_ids: Sequence[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """The instruments this cycle may scan, plus the catalogue failures.

        ``universe`` overrides the engine-wide filter for one cycle so a caller
        (notably AT-02 monitoring) never has to mutate shared discovery state.
        ``instrument_ids`` narrows the scan to the configured allow-list.

        A failing catalogue is returned, never swallowed: a cycle that read
        nothing must not be reported as a successful scan of zero instruments.
        """
        if self._market_data is None:
            return [], ["market data is not initialised"]
        scope = universe or self.universe
        allowed = {
            str(item or "").strip().upper()
            for item in (instrument_ids or ())
            if str(item or "").strip()
        }
        cap = min(limit or scope.max_instruments, scope.max_instruments)
        rows: dict[str, dict[str, Any]] = {}
        failures: list[str] = []
        self._last_universe_source = "catalogue"
        if allowed:
            # A configured universe is authoritative.  Reading those rows
            # directly prevents the catalogue page or bar inventory from
            # silently omitting a configured instrument.
            self._last_universe_source = "configured_universe"
            for instrument_id in sorted(allowed):
                try:
                    body = self._instrument(instrument_id)
                except Exception as exc:  # noqa: BLE001 - one bad row is not a cycle failure
                    failures.append(
                        f"instrument catalogue ({instrument_id}): "
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue
                if scope.allows(
                    str(body.get("asset_class") or ""),
                    str(body.get("exchange") or ""),
                    str(body.get("mic") or ""),
                ):
                    rows[instrument_id] = body
            return list(rows.values())[:cap], failures
        for asset_class in scope.asset_classes:
            try:
                page = self._market_data.list_instruments(
                    asset_class=asset_class, limit=cap
                )
            except Exception as exc:  # noqa: BLE001 - another class may still list
                message = (
                    f"instrument catalogue ({asset_class}): "
                    f"{type(exc).__name__}: {exc}"
                )
                self._last_error = message
                failures.append(message)
                self._store.record_source(
                    "instrument_catalogue",
                    status="error",
                    at=self._now(),
                    error=str(exc),
                )
                continue
            for instrument in page.items:
                body = instrument.to_dict()
                if scope.allows(
                    str(body.get("asset_class") or ""),
                    str(body.get("exchange") or ""),
                    str(body.get("mic") or ""),
                ):
                    rows[str(body["instrument_id"])] = body
        # A catalogue that failed on every class is a source failure, not an
        # empty market.  Report it before trying the stored-bar inventory.
        if failures and not rows:
            return [], failures
        # Prefer instruments that actually have a stored series.  The catalogue
        # is alphabetical, so a plain page can be filled with data-less rows and
        # every candidate then scores zero for a data gap rather than a market
        # signal.  The inventory is a read-only index over the same NM-02 bars;
        # if this facade does not expose it, the validated catalogue rows stand.
        inventory = getattr(self._market_data, "bar_inventory", None)
        if callable(inventory):
            try:
                body = inventory(
                    interval="1d",
                    min_bars=self.policy.min_history_bars,
                    limit=cap,
                )
            except Exception as exc:  # noqa: BLE001 - catalogue is the fallback
                _LOGGER.info(
                    "novamarket.discovery.inventory_unavailable error=%s",
                    type(exc).__name__,
                )
            else:
                stored: dict[str, dict[str, Any]] = {}
                for item in list(body.get("items") or ())[:cap]:
                    instrument_id = str(item.get("instrument_id") or "").strip()
                    if not instrument_id:
                        continue
                    try:
                        instrument = self._instrument(instrument_id)
                    except Exception as exc:  # noqa: BLE001 - one bad row is not a cycle failure
                        failures.append(
                            f"instrument catalogue ({instrument_id}): "
                            f"{type(exc).__name__}: {exc}"
                        )
                        continue
                    if scope.allows(
                        str(instrument.get("asset_class") or ""),
                        str(instrument.get("exchange") or ""),
                        str(instrument.get("mic") or ""),
                    ):
                        stored[instrument_id] = instrument
                if stored:
                    self._last_universe_source = "stored_bars"
                    return list(stored.values())[:cap], failures
        return list(rows.values())[:cap], failures

    async def run_cycle(
        self,
        *,
        force: bool = False,
        trigger: TriggerKind = TriggerKind.MANUAL,
        limit: int | None = None,
        universe: UniverseFilter | None = None,
        instrument_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Run one discovery cycle; overlapping or too-frequent runs are refused."""
        moment = self._now()
        scope = universe or self.universe
        if trigger is TriggerKind.SCHEDULED:
            sessions = (
                sessions_for(scope.exchanges, moment)
                if scope.exchanges
                else self.sessions_now()
            )
            if sessions and all(
                state == SessionState.CLOSED.value for state in sessions.values()
            ):
                return {
                    "ok": True,
                    "status": "market_closed",
                    "reason": (
                        "every declared exchange is outside its trading session"
                    ),
                    "sessions": sessions,
                }
        if self._cycle_lock.locked():
            return {"ok": True, "status": "busy", "reason": "a cycle is already running"}
        if not force and self._last_cycle_at is not None:
            elapsed = (moment - self._last_cycle_at).total_seconds()
            if elapsed < self.policy.min_interval_seconds:
                return {
                    "ok": True,
                    "status": "throttled",
                    "retry_after_seconds": round(
                        self.policy.min_interval_seconds - elapsed, 1
                    ),
                }
        async with self._cycle_lock:
            return await self._run_cycle_locked(
                trigger=trigger,
                limit=limit,
                started=moment,
                scope=scope,
                instrument_ids=instrument_ids,
            )

    async def _run_cycle_locked(
        self,
        *,
        trigger: TriggerKind,
        limit: int | None,
        started: datetime,
        scope: UniverseFilter | None = None,
        instrument_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        self._cycles += 1
        cycle_id = "cyc_" + hashlib.sha256(
            f"{started.isoformat()}|{self._cycles}".encode("utf-8")
        ).hexdigest()[:16]
        errors: list[str] = []
        failed_sources = 0
        if self._market_data is None:
            failed_sources = 1
            errors.append("market data is not initialised")
            self._store.record_source(
                "market_data", status="error", at=started, error=errors[-1]
            )
        scope = scope or self.universe
        items, catalogue_errors = self._universe_items(
            limit, universe=scope, instrument_ids=instrument_ids
        )
        if catalogue_errors and self._market_data is not None:
            failed_sources += 1
            errors.extend(catalogue_errors[:5])
        scanned = len(items)
        self._last_exchanges = tuple(
            sorted(
                {
                    str(row.get("exchange") or "")
                    for row in items
                    if row.get("exchange")
                }
            )
        )
        self._sessions = sessions_for(self._last_exchanges, started)
        contexts: list[
            tuple[dict[str, Any], CandidateEvaluation, tuple[scoring_module.EventSignal, ...]]
        ] = []
        created = 0
        created_ids: list[str] = []
        skipped = 0
        for instrument in items:
            status = str(instrument.get("trading_status") or "unknown")
            if scope.exclude_unknown_trading_status and status == "unknown":
                skipped += 1
                continue
            try:
                evaluation, events = self._evaluate(
                    instrument, reason=DiscoveryReason.CATALOGUE, at=started
                )
            except Exception as exc:  # noqa: BLE001 - one instrument never stops a run
                skipped += 1
                if len(errors) < 10:
                    errors.append(
                        f"{instrument.get('instrument_id')}: {type(exc).__name__}: {exc}"
                    )
                continue
            self._store.record_evaluation(evaluation, cycle_id=cycle_id)
            if self._store.upsert_candidate(
                evaluation.instrument_id,
                at=started,
                reason=DiscoveryReason.CATALOGUE.value,
                score=evaluation.score,
                quality=evaluation.data_quality.value,
            ):
                created += 1
                created_ids.append(evaluation.instrument_id)
            contexts.append((instrument, evaluation, events))

        pruned = 0
        if self._last_universe_source == "stored_bars" and contexts:
            pruned = self._store.prune_candidates_except(
                [evaluation.instrument_id for _, evaluation, _ in contexts]
            )

        added = 0
        counts: dict[str, int] = {
            "evaluated": 0,
            "upgraded": 0,
            "downgraded": 0,
            "removed": 0,
            "skipped": 0,
        }
        contexts.sort(key=lambda item: item[1].score, reverse=True)
        top = contexts[: self.policy.top_n]
        handled: set[str] = set()
        for rank, (instrument, evaluation, events) in enumerate(top, start=1):
            added += await self._auto_memberships(
                instrument, evaluation, events, at=started, rank=rank
            )
            tally = await self._reconcile_instrument(
                instrument, evaluation, at=started, skip_fresh=True
            )
            handled.add(evaluation.instrument_id)
            for key in counts:
                counts[key] += tally[key]
        maintenance = await self._maintain(started, skip_ids=handled)
        for key in counts:
            counts[key] += maintenance[key]

        finished = self._now()
        report = CycleReport(
            cycle_id=cycle_id,
            trigger=trigger,
            status="degraded" if errors else "ok",
            started_at=started,
            finished_at=finished,
            scanned=scanned,
            eligible=len(contexts),
            evaluated=len(contexts),
            created=created,
            pruned=pruned,
            added=added,
            upgraded=counts["upgraded"],
            downgraded=counts["downgraded"],
            removed=counts["removed"],
            skipped=skipped + counts["skipped"],
            failed_sources=failed_sources,
            duration_ms=int((finished - started).total_seconds() * 1000),
            errors=tuple(errors),
        )
        self._last_cycle_at = finished
        self._store.record_cycle(report)
        for instrument_id in created_ids[: self.policy.top_n]:
            created_eval = next(
                (item[1] for item in contexts if item[1].instrument_id == instrument_id),
                None,
            )
            if created_eval is None:
                continue
            await self._publish(
                "novamarket.asset.discovered",
                instrument_id=instrument_id,
                score=created_eval.score,
                reason=created_eval.discovery_reason.value,
            )
        for _instrument, evaluation, _events in top:
            await self._publish(
                "novamarket.asset.candidate.evaluated",
                instrument_id=evaluation.instrument_id,
                score=evaluation.score,
                data_quality=evaluation.data_quality.value,
            )
        for rank, (_instrument, evaluation, _events) in enumerate(top, start=1):
            await self._publish(
                "novamarket.asset.candidate.ranked",
                instrument_id=evaluation.instrument_id,
                score=evaluation.score,
                tier=evaluation.tier,
                rank=rank,
            )
        if failed_sources:
            reason = "; ".join(errors[:5]) or "a discovery source failed"
            source_id = "instrument_catalogue" if catalogue_errors else "market_data"
            self._store.record_source(
                source_id, status="error", at=finished, error=reason[:500]
            )
            await self._publish(
                "novamarket.asset.discovery.failed",
                reason=reason[:500],
                source_id=source_id,
            )
        else:
            self._store.record_source("instrument_catalogue", status="ok", at=finished)
        await self._publish(
            "novamarket.watchlist.evaluation.completed",
            evaluated=report.evaluated,
            upgraded=report.upgraded,
            downgraded=report.downgraded,
            removed=report.removed,
        )
        _LOGGER.info(
            "novamarket.discovery.cycle id=%s scanned=%d evaluated=%d added=%d",
            cycle_id,
            scanned,
            report.evaluated,
            added,
        )
        return {
            "ok": True,
            **report.to_dict(),
            "sessions": self._sessions,
        }

    @staticmethod
    def _factor(evaluation: CandidateEvaluation, name: str) -> ScoreFactor | None:
        for factor in evaluation.factors:
            if factor.name == name:
                return factor
        return None

    def _confirmed_events(
        self, events: Sequence[scoring_module.EventSignal]
    ) -> tuple[scoring_module.EventSignal, ...]:
        return tuple(
            signal for signal in events if signal.verification in CONFIRMED_STATES
        )

    async def _auto_memberships(
        self,
        instrument: dict[str, Any],
        evaluation: CandidateEvaluation,
        events: Sequence[scoring_module.EventSignal],
        *,
        at: datetime,
        rank: int,
    ) -> int:
        """Add a candidate to the watchlists whose rule it actually satisfies."""
        if evaluation.score < self.policy.add_threshold:
            return 0
        if evaluation.data_quality in (DataQuality.INSUFFICIENT, DataQuality.STALE):
            return 0
        instrument_id = evaluation.instrument_id
        targets: list[tuple[WatchlistKind, str]] = [
            (WatchlistKind.DISCOVERY, DEFAULT_WATCHLISTS[WatchlistKind.DISCOVERY][0])
        ]
        activity = self._factor(evaluation, "market_activity")
        if activity is not None and activity.value is not None and activity.value >= 0.6:
            targets.append(
                (WatchlistKind.ACTIVITY, DEFAULT_WATCHLISTS[WatchlistKind.ACTIVITY][0])
            )
        liquidity = self._factor(evaluation, "liquidity")
        if liquidity is not None and liquidity.value is not None and liquidity.value >= 0.6:
            targets.append(
                (WatchlistKind.LIQUIDITY, DEFAULT_WATCHLISTS[WatchlistKind.LIQUIDITY][0])
            )
        confirmed = self._confirmed_events(events)
        if confirmed:
            targets.append(
                (
                    WatchlistKind.EVENT_DRIVEN,
                    DEFAULT_WATCHLISTS[WatchlistKind.EVENT_DRIVEN][0],
                )
            )
        metadata = instrument.get("metadata") or {}
        sectors = [
            str(item)
            for item in (
                metadata.get("sectors")
                or ([metadata.get("sector")] if metadata.get("sector") else [])
            )
            if str(item).strip()
        ]
        for sector in sectors:
            targets.append((WatchlistKind.SECTOR, f"Sector: {sector}"))

        added = 0
        for kind, name in targets:
            entry = await self._add_member(
                kind,
                name,
                instrument_id,
                reason=f"{evaluation.discovery_reason.value} score {evaluation.score:.1f} (rank {rank})",
                score=evaluation.score,
                at=at,
                event_ids=tuple(signal.event_id for signal in confirmed),
            )
            if entry is not None:
                added += 1
        return added

    async def _add_member(
        self,
        kind: WatchlistKind,
        name: str,
        instrument_id: str,
        *,
        reason: str,
        score: float,
        at: datetime,
        event_ids: Sequence[str] = (),
    ) -> str | None:
        record = self._store.ensure_watchlist(
            kind, name, description=DEFAULT_WATCHLISTS.get(kind, ("", ""))[1], at=at
        )
        existing = self._store.entry(record.watchlist_id, instrument_id)
        if existing is not None and existing.status is not EntryStatus.REMOVED:
            return None
        if existing is None and record.entries >= self.policy.max_entries_per_watchlist:
            return None
        entry = WatchlistEntry(
            entry_id=entry_id_for(record.watchlist_id, instrument_id),
            watchlist_id=record.watchlist_id,
            instrument_id=instrument_id,
            status=EntryStatus.ACTIVE,
            added_at=existing.added_at if existing is not None else at,
            updated_at=at,
            reason=reason,
            score=score,
            last_evaluated_at=at,
            status_reason="",
            event_ids=tuple(event_ids),
            below_threshold_count=0,
        )
        reinstated = existing is not None
        self._store.save_entry(
            entry,
            history_reason=(
                "reinstated after removal" if reinstated else f"added: {reason}"
            ),
        )
        await self._publish(
            "novamarket.watchlist.instrument.added",
            watchlist_id=record.watchlist_id,
            instrument_id=instrument_id,
            score=score,
            reason=reason,
        )
        return "reinstated" if reinstated else "added"

    async def _apply_decision(
        self,
        entry: WatchlistEntry,
        evaluation: CandidateEvaluation,
        instrument: dict[str, Any],
        *,
        at: datetime,
        history_reason: str = "",
        event_ids: Sequence[str] | None = None,
    ) -> EntryStatus | None:
        """Apply the lifecycle decision for one membership; None means refused."""
        target, reason, weak = self._next_status(entry, evaluation, instrument)
        if target is not entry.status:
            try:
                ensure_entry_transition(entry.status, target)
            except ValueError as exc:
                self._last_error = str(exc)
                return None
        refreshed = WatchlistEntry(
            entry_id=entry.entry_id,
            watchlist_id=entry.watchlist_id,
            instrument_id=entry.instrument_id,
            status=target,
            added_at=entry.added_at,
            updated_at=at,
            reason=entry.reason,
            score=evaluation.score,
            last_evaluated_at=at,
            status_reason=reason,
            event_ids=tuple(
                event_ids if event_ids is not None else evaluation.event_ids
            ),
            below_threshold_count=weak,
        )
        self._store.save_entry(refreshed, history_reason=history_reason or reason)
        if target is EntryStatus.REMOVED:
            await self._publish(
                "novamarket.watchlist.instrument.removed",
                watchlist_id=entry.watchlist_id,
                instrument_id=entry.instrument_id,
                reason=reason,
            )
        elif target is not entry.status:
            await self._publish(
                "novamarket.watchlist.instrument.downgraded"
                if target is not EntryStatus.ACTIVE
                else "novamarket.watchlist.instrument.updated",
                watchlist_id=entry.watchlist_id,
                instrument_id=entry.instrument_id,
                from_status=entry.status.value,
                to_status=target.value,
                status=target.value,
                reason=reason,
            )
        else:
            await self._publish(
                "novamarket.watchlist.instrument.updated",
                watchlist_id=entry.watchlist_id,
                instrument_id=entry.instrument_id,
                status=target.value,
                score=evaluation.score,
            )
        return target

    async def _reconcile_instrument(
        self,
        instrument: dict[str, Any],
        evaluation: CandidateEvaluation,
        *,
        at: datetime,
        skip_fresh: bool = False,
        only_event_id: str = "",
    ) -> dict[str, int]:
        """Apply one fresh evaluation to every membership of that instrument."""
        counts = {
            "evaluated": 0,
            "upgraded": 0,
            "downgraded": 0,
            "removed": 0,
            "skipped": 0,
        }
        for entry in self._store.entries_for_instrument(evaluation.instrument_id):
            if skip_fresh and entry.last_evaluated_at == at:
                continue
            remaining: tuple[str, ...] | None = None
            if only_event_id:
                if only_event_id not in entry.event_ids:
                    continue
                remaining = tuple(
                    item for item in entry.event_ids if item != only_event_id
                )
            applied = await self._apply_decision(
                entry,
                evaluation,
                instrument,
                at=at,
                history_reason=(
                    f"event {only_event_id} was retracted" if only_event_id else ""
                ),
                event_ids=remaining,
            )
            if applied is None:
                counts["skipped"] += 1
                continue
            counts["evaluated"] += 1
            if applied is EntryStatus.REMOVED:
                counts["removed"] += 1
            elif applied is not entry.status:
                if applied is EntryStatus.ACTIVE:
                    counts["upgraded"] += 1
                else:
                    counts["downgraded"] += 1
        return counts

    # -- maintenance ----------------------------------------------------------

    def _next_status(
        self,
        entry: WatchlistEntry,
        evaluation: CandidateEvaluation,
        instrument: dict[str, Any],
    ) -> tuple[EntryStatus, str, int]:
        trading = str(instrument.get("trading_status") or "unknown")
        if trading in SUSPENDED_TRADING:
            return (
                EntryStatus.SUSPENDED,
                f"instrument trading status is {trading}",
                entry.below_threshold_count,
            )
        if evaluation.data_quality is DataQuality.INSUFFICIENT:
            return (
                EntryStatus.STALE,
                "insufficient market data to confirm relevance",
                entry.below_threshold_count,
            )
        if evaluation.data_quality is DataQuality.STALE:
            return (
                EntryStatus.STALE,
                "market data is stale",
                entry.below_threshold_count,
            )
        if evaluation.score < self.policy.remove_threshold:
            weak = entry.below_threshold_count + 1
            if weak >= self.policy.remove_after_cycles:
                return (
                    EntryStatus.REMOVED,
                    f"score {evaluation.score:.1f} stayed below "
                    f"{self.policy.remove_threshold:.1f} for {weak} evaluations",
                    weak,
                )
            return (
                EntryStatus.UNDER_REVIEW,
                f"score {evaluation.score:.1f} is below the removal threshold",
                weak,
            )
        if entry.score - evaluation.score >= self.policy.downgrade_delta:
            return (
                EntryStatus.DOWNGRADED,
                f"score fell from {entry.score:.1f} to {evaluation.score:.1f}",
                0,
            )
        if (
            entry.status
            in {
                EntryStatus.DOWNGRADED,
                EntryStatus.UNDER_REVIEW,
                EntryStatus.STALE,
                EntryStatus.SUSPENDED,
            }
            and evaluation.score >= self.policy.add_threshold
        ):
            return (
                EntryStatus.ACTIVE,
                f"score recovered to {evaluation.score:.1f}",
                0,
            )
        if evaluation.score - entry.score >= self.policy.upgrade_delta:
            return (
                EntryStatus.ACTIVE,
                f"score rose from {entry.score:.1f} to {evaluation.score:.1f}",
                0,
            )
        return entry.status, entry.status_reason, 0

    async def reevaluate(self, *, watchlist_id: str | None = None) -> dict[str, Any]:
        """Re-evaluate memberships and apply evidence-based lifecycle changes."""
        context = await self._maintain(self._now(), watchlist_id=watchlist_id)
        await self._publish(
            "novamarket.watchlist.evaluation.completed",
            evaluated=context["evaluated"],
            upgraded=context["upgraded"],
            downgraded=context["downgraded"],
            removed=context["removed"],
        )
        return {"ok": True, **context}

    async def _maintain(
        self,
        at: datetime,
        *,
        watchlist_id: str | None = None,
        skip_ids: frozenset[str] | set[str] = frozenset(),
    ) -> dict[str, Any]:
        """Re-evaluate members once per instrument and apply lifecycle decisions."""
        entries = self._store.active_entries(watchlist_id=watchlist_id, limit=1000)
        by_instrument: dict[str, list[WatchlistEntry]] = {}
        for entry in entries:
            if entry.status is EntryStatus.REMOVED or entry.instrument_id in skip_ids:
                continue
            by_instrument.setdefault(entry.instrument_id, []).append(entry)
        evaluated = upgraded = downgraded = removed = skipped = 0
        for instrument_id, group in by_instrument.items():
            try:
                instrument = self._instrument(instrument_id)
                evaluation, _ = self._evaluate(
                    instrument, reason=DiscoveryReason.CATALOGUE, at=at
                )
            except Exception as exc:  # noqa: BLE001 - a failure is not a removal reason
                skipped += len(group)
                self._last_error = f"{instrument_id}: {type(exc).__name__}: {exc}"
                continue
            self._store.record_evaluation(evaluation)
            tally = await self._reconcile_instrument(instrument, evaluation, at=at)
            evaluated += tally["evaluated"]
            upgraded += tally["upgraded"]
            downgraded += tally["downgraded"]
            removed += tally["removed"]
            skipped += tally["skipped"]
        return {
            "evaluated": evaluated,
            "upgraded": upgraded,
            "downgraded": downgraded,
            "removed": removed,
            "skipped": skipped,
            "at": at.isoformat(),
        }

    # -- event-driven discovery ----------------------------------------------

    async def consume_event(self, event_id: str) -> dict[str, Any]:
        """React to one NM-04 event; unverified events never trigger inclusion."""
        if self._radar is None:
            raise DiscoveryNotFoundError("the event radar is not initialised")
        event = self._radar.event(event_id)
        verification = str(event.get("verification") or "unverified")
        importance = str(event.get("importance") or "low")
        at = self._now()
        if verification in {"retracted", "expired"}:
            return await self._retract_event(event, event_id, at=at)
        if verification not in CONFIRMED_STATES:
            return {
                "ok": True,
                "status": "ignored",
                "reason": (
                    f"event {event_id} is {verification}; only officially confirmed or "
                    "corroborated events may drive watchlist inclusion"
                ),
            }
        if importance not in {"high", "critical"}:
            return {
                "ok": True,
                "status": "ignored",
                "reason": f"event {event_id} is {importance} importance",
            }
        added = updated = 0
        evaluated: list[str] = []
        for instrument_id in event.get("instruments", ()):
            try:
                instrument = self._instrument(instrument_id)
                evaluation, _ = self._evaluate(
                    instrument, reason=DiscoveryReason.EVENT, at=at
                )
            except Exception as exc:  # noqa: BLE001 - one instrument never stops this
                self._last_error = f"{instrument_id}: {type(exc).__name__}: {exc}"
                continue
            self._store.record_evaluation(evaluation)
            self._store.upsert_candidate(
                instrument_id,
                at=at,
                reason=DiscoveryReason.EVENT.value,
                score=evaluation.score,
                quality=evaluation.data_quality.value,
            )
            evaluated.append(instrument_id)
            updated += 1
            await self._reconcile_instrument(instrument, evaluation, at=at)
            if (
                evaluation.score >= self.policy.add_threshold
                and evaluation.data_quality in (DataQuality.GOOD, DataQuality.PARTIAL)
            ):
                result = await self._add_member(
                    WatchlistKind.EVENT_DRIVEN,
                    DEFAULT_WATCHLISTS[WatchlistKind.EVENT_DRIVEN][0],
                    instrument_id,
                    reason=f"event {event_id} ({importance}, {verification})",
                    score=evaluation.score,
                    at=at,
                    event_ids=(event_id,),
                )
                if result:
                    added += 1
        return {
            "ok": True,
            "status": "evaluated",
            "event_id": event_id,
            "instruments": evaluated,
            "added": added,
            "evaluated": updated,
        }

    def _live_event_ids(self, entry: WatchlistEntry) -> tuple[str, ...]:
        """Drop evidence ids the radar now reports as retracted or expired."""
        radar = self._radar
        if radar is None:
            return entry.event_ids
        kept: list[str] = []
        for event_id in entry.event_ids:
            try:
                verification = str(radar.event(event_id).get("verification") or "")
            except Exception:  # noqa: BLE001 - an unreadable id is kept, not invented
                kept.append(event_id)
                continue
            if verification in {"retracted", "expired"}:
                continue
            kept.append(event_id)
        return tuple(kept)

    async def _retract_event(
        self, event: dict[str, Any], event_id: str, *, at: datetime
    ) -> dict[str, Any]:
        """A retraction withdraws evidence; it never silently keeps a membership."""
        evaluated = affected = 0
        instruments: list[str] = []
        for instrument_id in event.get("instruments", ()):
            try:
                instrument = self._instrument(instrument_id)
                evaluation, _ = self._evaluate(
                    instrument, reason=DiscoveryReason.EVENT, at=at
                )
            except Exception as exc:  # noqa: BLE001 - one instrument never stops this
                self._last_error = f"{instrument_id}: {type(exc).__name__}: {exc}"
                continue
            self._store.record_evaluation(evaluation)
            self._store.upsert_candidate(
                instrument_id,
                at=at,
                reason=DiscoveryReason.EVENT.value,
                score=evaluation.score,
                quality=evaluation.data_quality.value,
            )
            evaluated += 1
            instruments.append(instrument_id)
            for entry in self._store.entries_for_instrument(instrument_id):
                remaining = self._live_event_ids(entry)
                if remaining == entry.event_ids and event_id not in entry.event_ids:
                    continue
                applied = await self._apply_decision(
                    entry,
                    evaluation,
                    instrument,
                    at=at,
                    history_reason=f"event {event_id} was retracted",
                    event_ids=remaining,
                )
                if applied is not None:
                    affected += 1
        return {
            "ok": True,
            "status": "retracted",
            "event_id": event_id,
            "instruments": instruments,
            "evaluated": evaluated,
            "affected": affected,
        }

    async def forward_market_event(
        self, name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Feed one published NM-04 event into discovery; other events pass."""
        label = str(name or "")
        if not label.startswith("novamarket.market_event."):
            return {
                "forwarded": False,
                "reason": f"{label or 'event'} is not a market event",
            }
        event_id = str(payload.get("event_id") or "")
        if not event_id:
            return {
                "forwarded": False,
                "reason": "the event payload carries no event_id",
            }
        result = await self.consume_event(event_id)
        return {"forwarded": True, **result}

    def sessions_now(self) -> dict[str, str]:
        """Declared session state for the exchanges this run covers."""
        exchanges = self._last_exchanges or self.universe.exchanges
        return sessions_for(exchanges, self._now())

    # -- watchlist operations -------------------------------------------------

    async def create_watchlist(
        self, name: str, kind: WatchlistKind = WatchlistKind.CUSTOM, *, description: str = ""
    ) -> dict[str, Any]:
        label = str(name or "").strip()
        if not label:
            raise DiscoveryValidationError("a watchlist needs a name")
        at = self._now()
        record = self._store.ensure_watchlist(
            WatchlistKind(kind), label, description=description, custom=True, at=at
        )
        await self._publish(
            "novamarket.watchlist.created",
            watchlist_id=record.watchlist_id,
            kind=record.kind.value,
            name=record.name,
        )
        return {"ok": True, **record.to_dict()}

    def watchlists(self) -> dict[str, Any]:
        rows = [record.to_dict() for record in self._store.watchlists()]
        return {"ok": True, "watchlists": rows, "total": len(rows)}

    async def rename_watchlist(self, watchlist_id: str, name: str) -> dict[str, Any]:
        """Rename an existing list; the id stays stable so history survives."""
        label = str(name or "").strip()
        if not label:
            raise DiscoveryValidationError("a watchlist needs a name")
        record = self._store.watchlist(watchlist_id)
        if record is None:
            raise DiscoveryNotFoundError(f"unknown watchlist {watchlist_id!r}")
        folded = label.casefold()
        if any(
            other.watchlist_id != watchlist_id and other.name.casefold() == folded
            for other in self._store.watchlists()
        ):
            raise DiscoveryValidationError("a watchlist with that name already exists")
        updated = self._store.rename_watchlist(watchlist_id, label, at=self._now())
        if updated is None:
            raise DiscoveryNotFoundError(f"unknown watchlist {watchlist_id!r}")
        await self._publish(
            "novamarket.watchlist.renamed",
            watchlist_id=updated.watchlist_id,
            name=updated.name,
            previous_name=record.name,
        )
        return {"ok": True, **updated.to_dict()}

    def watchlist(self, watchlist_id: str, *, limit: int = 100) -> dict[str, Any]:
        record = self._store.watchlist(watchlist_id)
        if record is None:
            raise DiscoveryNotFoundError(f"unknown watchlist {watchlist_id!r}")
        entries = []
        for entry in self._store.entries(watchlist_id, limit=limit):
            body = entry.to_dict()
            try:
                instrument = self._instrument(entry.instrument_id)
                body["instrument"] = {
                    "name": instrument.get("name"),
                    "symbol": instrument.get("symbol"),
                    "asset_class": instrument.get("asset_class"),
                    "exchange": instrument.get("exchange"),
                    "currency": instrument.get("currency"),
                }
            except Exception:  # noqa: BLE001 - a membership outlives catalogue churn
                body["instrument"] = None
            entries.append(body)
        return {"ok": True, **record.to_dict(), "members": entries, "total": len(entries)}

    async def add_instrument(
        self, watchlist_id: str, instrument_id: str, *, reason: str = ""
    ) -> dict[str, Any]:
        record = self._store.watchlist(watchlist_id)
        if record is None:
            raise DiscoveryNotFoundError(f"unknown watchlist {watchlist_id!r}")
        instrument = self._instrument(instrument_id)
        at = self._now()
        evaluation, _ = self._evaluate(
            instrument, reason=DiscoveryReason.CONFIGURED, at=at
        )
        self._store.record_evaluation(evaluation)
        self._store.upsert_candidate(
            instrument_id,
            at=at,
            reason=DiscoveryReason.CONFIGURED.value,
            score=evaluation.score,
            quality=evaluation.data_quality.value,
        )
        existing = self._store.entry(watchlist_id, instrument_id)
        if existing is not None and existing.status is not EntryStatus.REMOVED:
            raise DiscoveryValidationError(
                f"{instrument_id} is already an active member of this watchlist"
            )
        if existing is None and record.entries >= self.policy.max_entries_per_watchlist:
            raise DiscoveryValidationError(
                f"watchlist {watchlist_id!r} is at its {self.policy.max_entries_per_watchlist}-member limit"
            )
        entry = WatchlistEntry(
            entry_id=entry_id_for(watchlist_id, instrument_id),
            watchlist_id=watchlist_id,
            instrument_id=instrument_id,
            status=EntryStatus.ACTIVE,
            added_at=existing.added_at if existing is not None else at,
            updated_at=at,
            reason=reason or "added manually",
            score=evaluation.score,
            last_evaluated_at=at,
            event_ids=tuple(evaluation.event_ids),
        )
        self._store.save_entry(entry, history_reason=reason or "added manually")
        await self._publish(
            "novamarket.watchlist.instrument.added",
            watchlist_id=watchlist_id,
            instrument_id=instrument_id,
            score=evaluation.score,
            reason=entry.reason,
        )
        return {"ok": True, **entry.to_dict()}

    async def remove_instrument(
        self, watchlist_id: str, instrument_id: str, *, reason: str = ""
    ) -> dict[str, Any]:
        entry = self._store.entry(watchlist_id, instrument_id)
        if entry is None or entry.status is EntryStatus.REMOVED:
            raise DiscoveryNotFoundError(
                f"{instrument_id} is not an active member of {watchlist_id!r}"
            )
        at = self._now()
        removed = WatchlistEntry(
            entry_id=entry.entry_id,
            watchlist_id=entry.watchlist_id,
            instrument_id=entry.instrument_id,
            status=EntryStatus.REMOVED,
            added_at=entry.added_at,
            updated_at=at,
            reason=entry.reason,
            score=entry.score,
            last_evaluated_at=entry.last_evaluated_at,
            status_reason=reason or "removed by request",
            event_ids=entry.event_ids,
            below_threshold_count=entry.below_threshold_count,
        )
        self._store.save_entry(removed, history_reason=reason or "removed by request")
        await self._publish(
            "novamarket.watchlist.instrument.removed",
            watchlist_id=watchlist_id,
            instrument_id=instrument_id,
            reason=removed.status_reason,
        )
        return {"ok": True, **removed.to_dict()}

    # -- reads ----------------------------------------------------------------

    def candidates(
        self, *, limit: int = 50, min_score: float = 0.0
    ) -> dict[str, Any]:
        rows = self._store.candidates(limit=limit, min_score=min_score)
        candidates = []
        for row in rows:
            body = dict(row)
            instrument_id = str(row["instrument_id"])
            evaluation = self._store.latest_evaluation(instrument_id)
            if evaluation is not None:
                body["evaluation"] = evaluation
            memberships = self._store.entries_for_instrument(instrument_id)
            body["watchlist_ids"] = [entry.watchlist_id for entry in memberships]
            body["on_watchlist"] = bool(memberships)
            candidates.append(body)
        return {"ok": True, "candidates": candidates, "total": len(candidates)}

    def candidate(self, instrument_id: str) -> dict[str, Any]:
        row = self._store.candidate(instrument_id)
        if row is None:
            raise DiscoveryNotFoundError(f"unknown candidate {instrument_id!r}")
        latest = self._store.latest_evaluation(instrument_id)
        memberships = self._store.entries_for_instrument(instrument_id)
        return {
            "ok": True,
            **row,
            "watchlist_ids": [entry.watchlist_id for entry in memberships],
            "on_watchlist": bool(memberships),
            "evaluation": latest,
            "evaluations": self._store.evaluations(instrument_id, limit=50),
        }

    def history(
        self,
        *,
        watchlist_id: str | None = None,
        instrument_id: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        rows = self._store.history(
            watchlist_id=watchlist_id, instrument_id=instrument_id, limit=limit
        )
        body: dict[str, Any] = {"ok": True, "history": rows, "total": len(rows)}
        if instrument_id:
            evaluations = self._store.evaluations(instrument_id, limit=limit)
            body["evaluations"] = evaluations
            body["evaluation_total"] = len(evaluations)
        return body

    def cycles(self, *, limit: int = 20) -> dict[str, Any]:
        rows = self._store.cycles(limit=limit)
        return {"ok": True, "cycles": rows, "total": len(rows)}

    def sources(self) -> dict[str, Any]:
        rows = self._store.sources()
        return {"ok": True, "sources": rows, "total": len(rows)}

    def scheduler_status(self) -> dict[str, Any]:
        return {
            "running": self._scheduler_task is not None
            and not self._scheduler_task.done(),
            "interval_seconds": self._scheduler_interval,
            "cycles": self._scheduler_cycles,
            "errors": self._scheduler_errors,
            "last_cycle_at": (
                self._last_cycle_at.isoformat() if self._last_cycle_at else None
            ),
        }

    # -- scheduling -----------------------------------------------------------

    async def start_scheduler(self, interval_seconds: int | None = None) -> dict[str, Any]:
        """Start the bounded periodic cycle runner; nothing runs uncontrolled."""
        interval = int(
            interval_seconds
            or self._scheduler_interval
            or max(self.policy.min_interval_seconds, 300)
        )
        if interval < self.policy.min_interval_seconds:
            raise DiscoveryValidationError(
                f"interval must be at least {self.policy.min_interval_seconds} seconds"
            )
        await self.stop_scheduler()
        self._scheduler_interval = interval
        self._scheduler_stop = asyncio.Event()
        self._scheduler_task = asyncio.create_task(
            self._scheduler_loop(interval, self._scheduler_stop)
        )
        return {"ok": True, **self.scheduler_status()}

    async def _scheduler_loop(self, interval: int, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.run_cycle(force=True, trigger=TriggerKind.SCHEDULED)
                self._scheduler_cycles += 1
            except Exception as exc:  # noqa: BLE001 - the loop survives one bad cycle
                self._scheduler_errors += 1
                self._last_error = f"scheduled cycle: {type(exc).__name__}: {exc}"
                _LOGGER.warning(
                    "novamarket.discovery.scheduled_cycle_failed error=%s",
                    type(exc).__name__,
                )

    async def stop_scheduler(self) -> dict[str, Any]:
        stop = self._scheduler_stop
        task = self._scheduler_task
        if stop is not None:
            stop.set()
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._scheduler_task = None
        self._scheduler_stop = None
        return {"ok": True, **self.scheduler_status()}

    async def close(self) -> None:
        await self.stop_scheduler()
        self._store.close()

    # -- status and contract --------------------------------------------------

    def status(self) -> dict[str, Any]:
        cycle = self._store.latest_cycle()
        return {
            "initialised": True,
            "schema_version": self._store.schema_version,
            "phase": "NM-05",
            "universe": self.universe.to_dict(),
            "policy": self.policy.to_dict(),
            "methodology_version": METHODOLOGY_VERSION,
            "last_cycle_at": (
                self._last_cycle_at.isoformat() if self._last_cycle_at else None
            ),
            "last_cycle": cycle,
            "last_error": self._last_error,
            "scheduler": self.scheduler_status(),
            "sessions": self.sessions_now(),
            "recent_cycles": self._store.cycles(limit=10),
            "sources": self._store.sources(),
            "metrics": self._store.metrics(),
        }

    @staticmethod
    def _declared_exchanges() -> tuple[str, ...]:
        from novacontrol.novamarket.discovery.sessions import SESSION_TABLE

        return tuple(SESSION_TABLE)

    def metrics(self) -> dict[str, Any]:
        body = self._store.metrics()
        body.update(
            {
                "cycles_run": self._cycles,
                "last_error": self._last_error,
                "scheduler": self.scheduler_status(),
            }
        )
        return body

    def contract(self) -> dict[str, Any]:
        return {
            "implemented": True,
            "phase": "NM-05",
            "universe": self.universe.to_dict(),
            "watchlists": [
                {
                    "kind": kind.value,
                    "name": name,
                    "description": description,
                }
                for kind, (name, description) in DEFAULT_WATCHLISTS.items()
            ],
            "entry_states": [status.value for status in EntryStatus],
            "scoring": scoring_module.describe_model(),
            "policy": self.policy.to_dict(),
            "sessions": {
                "states": [state.value for state in SessionState],
                "declared": sorted(
                    exchange for exchange in self._declared_exchanges()
                ),
                "unknown_never_blocks": True,
            },
            "events": [
                "novamarket.asset.discovered",
                "novamarket.asset.candidate.evaluated",
                "novamarket.asset.candidate.ranked",
                "novamarket.watchlist.created",
                "novamarket.watchlist.instrument.added",
                "novamarket.watchlist.instrument.updated",
                "novamarket.watchlist.instrument.downgraded",
                "novamarket.watchlist.instrument.removed",
                "novamarket.watchlist.evaluation.completed",
                "novamarket.asset.discovery.failed",
            ],
            "guarantees": [
                "a watchlist entry is never an order, a position or a recommendation",
                "an unverified event cannot add an instrument to a watchlist",
                "a price decline alone never removes an instrument",
                "a data-source failure is reported, never converted into a removal",
            ],
        }


def _event_query(**kwargs: Any) -> Any:
    from novacontrol.novamarket.radar import EventQuery

    return EventQuery(**kwargs)


__all__ = [
    "AssetDiscoveryEngine",
    "CONFIRMED_STATES",
    "DEFAULT_WATCHLISTS",
]
