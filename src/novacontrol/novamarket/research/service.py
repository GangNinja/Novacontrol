"""PI-19 financial-research coordinator and bounded run service."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from .evidence import build_company_profile, enrich_news, verify_evidence
from .financials import analyze_financials, peer_comparison
from .intelligence import (
    build_corporate_events,
    build_macro,
    build_news,
    build_peers,
    build_sector,
    forecast_context,
)
from .models import (
    DEFAULT_RUN_TIMEOUT_SECONDS,
    HORIZONS,
    MAX_CONCURRENT_RUNS,
    MAX_EVIDENCE,
    MAX_MACRO,
    MAX_NEWS,
    MAX_PEERS,
    MAX_STATEMENTS,
    RESEARCH_CONTRACT_VERSION,
    RESEARCH_SCHEMA_VERSION,
    TERMINAL_STATUSES,
    EvidenceKind,
    EvidenceRecord,
    FinancialStatement,
    ResearchNotFoundError,
    ResearchRequest,
    ResearchResourceError,
    ResearchStateError,
    ResearchStatus,
    ResearchValidationError,
    SourceTier,
    ValuationAssumptions,
    VerificationStatus,
    clean,
    optional_float,
    optional_time,
    stable_id,
)
from .store import ResearchStore
from .thesis import build_thesis
from .valuation import dcf_valuation, relative_valuation

ContextLoader = Callable[[ResearchRequest], Mapping[str, Any]]


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _payload_evidence(
    context: Mapping[str, Any],
    *,
    instrument_id: str,
    cutoff: datetime,
    warnings: list[str],
) -> list[EvidenceRecord]:
    """Make caller-supplied structured news/events/sector source-bearing records.

    These records still pass through the regular verification layer; being
    supplied by a caller is not verification.  A missing publication time is
    never replaced with the cutoff and never becomes a claim.
    """
    records: list[EvidenceRecord] = []
    specs = (
        (
            EvidenceKind.NEWS.value,
            _rows(context.get("news"))[:MAX_NEWS],
            ("headline", "title"),
            ("publication_time",),
        ),
        (
            EvidenceKind.CORPORATE_EVENT.value,
            _rows(context.get("corporate_events") or context.get("events")),
            ("description", "event_type"),
            ("announced_at", "announcement_date"),
        ),
    )
    for kind, rows, claim_fields, alternate_times in specs:
        for row in rows:
            published_raw = row.get("published_at")
            for alternate_time in alternate_times:
                if published_raw not in (None, ""):
                    break
                published_raw = row.get(alternate_time)
            if published_raw in (None, ""):
                continue
            claim = next(
                (clean(row.get(field), limit=4_000) for field in claim_fields if row.get(field)),
                "",
            )
            if not claim:
                continue
            try:
                published = optional_time(published_raw, "published_at")
                if published is None:
                    continue
                source_name = clean(
                    row.get("source_name") or row.get("source") or row.get("publisher"),
                    limit=200,
                )
                source_id = clean(
                    row.get("source_id") or row.get("source") or source_name,
                    limit=160,
                ) or f"caller:{kind}"
                if kind == EvidenceKind.NEWS.value:
                    event_id = clean(row.get("article_id"), limit=80) or stable_id(
                        "pna_", instrument_id, source_name, claim, published.isoformat()
                    )
                    metadata = {**dict(row), "article_id": event_id, "headline": claim}
                else:
                    event_id = clean(row.get("event_id"), limit=80) or stable_id(
                        "pev_", instrument_id, row.get("event_type"), published.isoformat()
                    )
                    metadata = {**dict(row), "event_id": event_id, "description": claim}
                records.append(
                    EvidenceRecord(
                        evidence_id=stable_id(
                            "pre_", instrument_id, kind, source_id, claim, published.isoformat()
                        ),
                        instrument_id=instrument_id,
                        kind=kind,
                        source_id=source_id,
                        source_name=source_name,
                        claim=claim,
                        published_at=published,
                        retrieved_at=optional_time(
                            row.get("retrieved_at") or row.get("retrieval_at"), "retrieved_at"
                        )
                        or cutoff,
                        source_url=row.get("source_url") or row.get("url") or "",
                        source_tier=row.get("source_tier") or SourceTier.UNKNOWN.value,
                        verification=row.get("verification")
                        or VerificationStatus.UNVERIFIED.value,
                        summary=clean(row.get("summary"), limit=12_000),
                        metadata=metadata,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - malformed source is explicit
                warnings.append(f"{kind} evidence rejected: {type(exc).__name__}: {exc}")
    for row in _rows(context.get("macro"))[:MAX_MACRO]:
        if not row.get("published_at"):
            continue
        try:
            published = optional_time(row.get("published_at"), "published_at")
            if published is None:
                continue
            indicator = clean(row.get("indicator"), limit=120)
            if not indicator:
                continue
            value = optional_float(row.get("value"))
            unit = clean(row.get("unit"), limit=60)
            period = clean(row.get("period"), limit=60)
            records.append(
                EvidenceRecord(
                    evidence_id=stable_id(
                        "pre_", instrument_id, "macro", indicator, period, published.isoformat()
                    ),
                    instrument_id=instrument_id,
                    kind=EvidenceKind.MACRO.value,
                    source_id=clean(row.get("source_id") or row.get("source_name"), limit=160)
                    or "caller:macro",
                    source_name=clean(row.get("source_name") or row.get("source"), limit=200),
                    claim=f"{indicator}: {value} {unit} ({period})".strip(),
                    published_at=published,
                    retrieved_at=optional_time(row.get("retrieved_at"), "retrieved_at")
                    or cutoff,
                    source_tier=row.get("source_tier") or SourceTier.UNKNOWN.value,
                    verification=row.get("verification")
                    or VerificationStatus.UNVERIFIED.value,
                    value=value,
                    unit=unit,
                    currency="INR",
                    metadata=dict(row),
                )
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"macro evidence rejected: {type(exc).__name__}: {exc}")
    sector = _mapping(context.get("sector"))
    published_raw = sector.get("published_at") or sector.get("as_of")
    if sector and published_raw:
        try:
            published = optional_time(published_raw, "published_at")
            if published is not None:
                sector_name = clean(sector.get("sector"), limit=120)
                records.append(
                    EvidenceRecord(
                        evidence_id=stable_id(
                            "pre_", instrument_id, "sector", sector_name, published.isoformat()
                        ),
                        instrument_id=instrument_id,
                        kind=EvidenceKind.SECTOR.value,
                        source_id=clean(
                            sector.get("source_id") or sector.get("source_name"), limit=160
                        )
                        or "caller:sector",
                        source_name=clean(
                            sector.get("source_name") or sector.get("source"), limit=200
                        ),
                        claim=f"Sector snapshot for {sector_name or 'the target sector'}",
                        published_at=published,
                        retrieved_at=optional_time(
                            sector.get("retrieval_at") or sector.get("as_of"), "retrieval_at"
                        )
                        or cutoff,
                        source_tier=sector.get("source_tier") or SourceTier.UNKNOWN.value,
                        verification=sector.get("verification")
                        or VerificationStatus.UNVERIFIED.value,
                        metadata=dict(sector),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"sector evidence rejected: {type(exc).__name__}: {exc}")
    return records


class ResearchService:
    """Own one isolated research store and one bounded execution surface.

    The service is intentionally synchronous so it can be called from a worker
    thread.  It has no order, risk, portfolio or ledger dependency.
    """

    def __init__(
        self,
        store: ResearchStore | str,
        *,
        context_loader: ContextLoader | None = None,
        clock: Callable[[], datetime] | None = None,
        max_concurrent: int = MAX_CONCURRENT_RUNS,
        run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS,
        allowed_sources: Sequence[str] = (),
    ) -> None:
        self.store = (
            store if isinstance(store, ResearchStore) else ResearchStore(store, clock=clock)
        )
        self.context_loader = context_loader
        self.clock = clock or (lambda: datetime.now(tz=UTC))
        self.allowed_sources = tuple(allowed_sources)
        self.max_concurrent = max(1, min(int(max_concurrent), MAX_CONCURRENT_RUNS))
        self.run_timeout_seconds = max(1.0, min(float(run_timeout_seconds), 120.0))
        self._slots = threading.BoundedSemaphore(self.max_concurrent)
        self._cancelled: set[str] = set()
        self._running: set[str] = set()
        self._lock = threading.RLock()

    def create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = ResearchRequest.from_payload(payload, now=self.clock())
        supplied_context = payload.get("context") or payload.get("input_context") or {}
        if supplied_context and not isinstance(supplied_context, Mapping):
            raise ResearchValidationError("research context must be an object")
        if len(str(supplied_context)) > 2_000_000:
            raise ResearchResourceError("research context exceeds the bounded request size")
        if request.idempotency_key:
            existing = self.store.find_by_idempotency(request.idempotency_key)
            if existing:
                return existing
        run = {
            "run_id": request.research_id,
            "instrument_id": request.instrument_id,
            "company_name": request.company_name,
            "exchange": request.exchange,
            "horizon": request.horizon,
            "horizon_group": HORIZONS[request.horizon]["group"],
            "valid_until": (
                request.as_of
                + timedelta(
                    days={"intraday": 1, "swing": 7, "long_term": 30}[
                        HORIZONS[request.horizon]["group"]
                    ]
                )
            ).isoformat(),
            "objective": request.objective,
            "analysis_types": list(request.analysis_types),
            "as_of": request.as_of.isoformat(),
            "publication_cutoff": _iso(request.publication_cutoff),
            "portfolio_id": request.portfolio_id,
            "parent_run_id": request.parent_run_id,
            "idempotency_key": request.idempotency_key,
            "version": request.version,
            "status": ResearchStatus.PENDING.value,
            "created_at": request.created_at.isoformat(),
            "finished_at": None,
            "started_at": None,
            "config": {
                "contract": RESEARCH_CONTRACT_VERSION,
                "schema_version": RESEARCH_SCHEMA_VERSION,
                "max_evidence_age_hours": request.max_evidence_age_hours,
                "requested_by": request.requested_by,
                "run_timeout_seconds": self.run_timeout_seconds,
                "advisory_only": True,
                "can_mutate_portfolio": False,
                "can_submit_orders": False,
                "nm08_risk_authority": True,
                "nm10_execution_authority": True,
            },
            "evidence": [],
            "claims": [],
            "warnings": [],
            "limitations": [],
            "sections": [],
            "thesis": None,
            "features": {},
            "handoff": {},
            "input_context": (
                dict(supplied_context) if isinstance(supplied_context, Mapping) else {}
            ),
        }
        return self.store.save_run(run)

    def run(self, run_id: str) -> dict[str, Any]:
        current = self.store.run(run_id)
        if current.get("status") in {item.value for item in TERMINAL_STATUSES}:
            return current
        with self._lock:
            if run_id in self._running:
                raise ResearchStateError("this research run is already executing")
            self._running.add(run_id)
        if not self._slots.acquire(blocking=False):
            with self._lock:
                self._running.discard(run_id)
            raise ResearchResourceError("the research concurrency limit is reached")
        started = time.monotonic()
        try:
            if run_id in self._cancelled:
                return self._finish_cancelled(run_id)
            request = self._request_from_run(current)
            current["status"] = ResearchStatus.RUNNING.value
            current["started_at"] = self.clock().isoformat()
            self.store.update_run(run_id, current)
            self.store.event(run_id, "research.started", {"instrument_id": request.instrument_id})
            stored_context = current.get("input_context")
            context: Mapping[str, Any] = (
                dict(stored_context) if isinstance(stored_context, Mapping) else {}
            )
            if self.context_loader is not None:
                loaded = self.context_loader(request)
                if not isinstance(loaded, Mapping):
                    raise ResearchValidationError(
                        "the research context loader must return an object"
                    )
                context = {**context, **loaded}
            self._check_cancel(run_id, started)
            report = self._assemble(request, context, deadline=started + self.run_timeout_seconds)
            self._check_cancel(run_id, started)
            report = {**current, **report}
            report["run_id"] = run_id
            report["started_at"] = current.get("started_at")
            report["finished_at"] = self.clock().isoformat()
            self._persist_report(run_id, report)
            self.store.update_run(run_id, report)
            self.store.event(
                run_id,
                "research.completed",
                {
                    "status": report["status"],
                    "thesis_stance": (report.get("thesis") or {}).get("stance"),
                },
            )
            return report
        except ResearchStateError:
            latest = self.store.run(run_id)
            if latest.get("status") in {item.value for item in TERMINAL_STATUSES}:
                return latest
            return self._finish_failed(run_id, "research run was cancelled or timed out")
        except Exception as exc:  # noqa: BLE001 - a research failure is a status, not a crash
            self.store.event(
                run_id,
                "research.failed",
                {"error": f"{type(exc).__name__}: {clean(exc, limit=500)}"},
            )
            return self._finish_failed(run_id, f"{type(exc).__name__}: {clean(exc, limit=500)}")
        finally:
            with self._lock:
                self._running.discard(run_id)
            self._slots.release()

    def run_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        created = self.create(payload)
        return self.run(str(created["run_id"]))

    def cancel(self, run_id: str) -> dict[str, Any]:
        run = self.store.run(run_id)
        if run.get("status") in {item.value for item in TERMINAL_STATUSES}:
            if run.get("status") == ResearchStatus.CANCELLED.value:
                return run
            raise ResearchStateError("a terminal research run cannot be cancelled")
        self._cancelled.add(run_id)
        run["status"] = ResearchStatus.CANCELLED.value
        run["finished_at"] = self.clock().isoformat()
        run["cancel_reason"] = "cancelled by an authenticated research request"
        self.store.update_run(run_id, run)
        self.store.event(run_id, "research.cancelled", {"run_id": run_id})
        return run

    def refresh(self, run_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        previous = self.store.run(run_id)
        if previous.get("status") not in {
            ResearchStatus.COMPLETED.value,
            ResearchStatus.COMPLETED_WITH_WARNINGS.value,
            ResearchStatus.PARTIAL_EVIDENCE.value,
            ResearchStatus.INSUFFICIENT_EVIDENCE.value,
            ResearchStatus.STALE_DATA.value,
        }:
            raise ResearchStateError("only a completed research run can be refreshed")
        supplied = dict(payload or {})
        as_of = optional_time(supplied.get("as_of"), "as_of") or self.clock()
        new_payload = {
            "research_id": supplied.get("research_id")
            or stable_id("prr_", previous["instrument_id"], as_of.isoformat(), "refresh", run_id),
            "instrument_id": previous["instrument_id"],
            "company_name": previous.get("company_name", ""),
            "exchange": previous.get("exchange", ""),
            "horizon": previous.get("horizon", "5d"),
            "objective": previous.get("objective", "full_research"),
            "analysis_types": previous.get("analysis_types", []),
            "as_of": as_of.isoformat(),
            "publication_cutoff": supplied.get("publication_cutoff"),
            "portfolio_id": previous.get("portfolio_id", ""),
            "parent_run_id": run_id,
            "version": int(previous.get("version") or 1) + 1,
            "max_evidence_age_hours": (previous.get("config") or {}).get(
                "max_evidence_age_hours", 720.0
            ),
            "requested_by": supplied.get("requested_by", ""),
            "idempotency_key": supplied.get("idempotency_key", ""),
            "context": supplied.get("context") or previous.get("input_context") or {},
        }
        return self.create(new_payload)

    def monitor(
        self,
        instrument_id: str,
        *,
        horizon: str = "",
        min_interval_hours: float = 6.0,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        now = as_of or self.clock()
        latest = self.store.latest_completed(instrument_id, horizon)
        if not latest:
            return {"action": "no_research", "refreshed": False, "run": None}
        created = optional_time(
            latest.get("finished_at") or latest.get("created_at"), "finished_at"
        )
        due = created is None or (now - created) >= timedelta(hours=max(0.1, min_interval_hours))
        if not due:
            return {
                "action": "current",
                "refreshed": False,
                "run": _summary(latest),
                "next_earliest": (
                    created + timedelta(hours=max(0.1, min_interval_hours))
                ).isoformat()
                if created
                else None,
            }
        refreshed = self.refresh(str(latest["run_id"]), {"as_of": now.isoformat()})
        return {
            "action": "refreshed",
            "refreshed": True,
            "run": _summary(self.run(str(refreshed["run_id"]))),
        }

    def detail(self, run_id: str) -> dict[str, Any]:
        run = self.store.run(run_id)
        valid_until = run.get("valid_until")
        if valid_until:
            try:
                parsed = optional_time(valid_until, "valid_until")
                run["expired"] = None if parsed is None else self.clock() > parsed
            except Exception:  # noqa: BLE001 - old rows remain readable
                run["expired"] = None
        return run

    def section(self, run_id: str, section: str) -> dict[str, Any]:
        return self.store.section(run_id, section)

    def evidence(self, run_id: str) -> list[dict[str, Any]]:
        return self.store.evidence(run_id)

    def claims(self, run_id: str) -> list[dict[str, Any]]:
        return self.store.claims(run_id)

    def history(
        self,
        *,
        instrument_id: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return self.store.runs(
            instrument_id=instrument_id,
            status=status,
            limit=limit,
            offset=offset,
        )

    def features(self, run_id: str) -> dict[str, Any]:
        run = self.store.run(run_id)
        features = run.get("features")
        return features if isinstance(features, dict) else {}

    def handoff(self, run_id: str) -> dict[str, Any]:
        run = self.store.run(run_id)
        handoff = run.get("handoff")
        return handoff if isinstance(handoff, dict) else {}

    def status(self) -> dict[str, Any]:
        return {
            "phase": "PI-19",
            "implemented": True,
            "initialised": True,
            "contract": RESEARCH_CONTRACT_VERSION,
            "schema_version": RESEARCH_SCHEMA_VERSION,
            **self.store.health(),
            "max_concurrent": self.max_concurrent,
            "run_timeout_seconds": self.run_timeout_seconds,
            "advisory_only": True,
            "can_mutate_portfolio": False,
            "can_submit_orders": False,
            "can_change_autonomy": False,
            "risk_authority": "NM-08",
            "execution_authority": "NM-10",
        }

    def contract(self) -> dict[str, Any]:
        return {
            "phase": "PI-19",
            "contract": RESEARCH_CONTRACT_VERSION,
            "schema_version": RESEARCH_SCHEMA_VERSION,
            "horizons": {
                key: value["group"] for key, value in HORIZONS.items()
            },
            "analysis_types": [
                "company",
                "statements",
                "ratios",
                "events",
                "news",
                "sector",
                "peers",
                "valuation",
                "macro",
                "thesis",
            ],
            "statuses": [
                "pending",
                "running",
                "completed",
                "completed_with_warnings",
                "partial_evidence",
                "insufficient_evidence",
                "source_unavailable",
                "stale_data",
                "failed",
                "cancelled",
            ],
            "verification_statuses": [
                "verified",
                "partially_verified",
                "unverified",
                "conflicting_evidence",
                "source_unavailable",
                "stale",
            ],
            "valuation_methods": ["relative", "dcf"],
            "point_in_time_rules": [
                "evidence and statements published after the cutoff are excluded",
                "statement periods ending after the cutoff are excluded",
                "a restatement replaces an earlier record only for the same period, "
                "basis and statement type",
                "currency conversion requires a timestamped FX rate at or after period end",
                "TTM requires four contiguous quarters",
            ],
            "authority": {
                "risk": "NM-08",
                "execution": "NM-10",
                "research_can_mutate_portfolio": False,
                "research_can_write_ledger": False,
                "research_can_submit_orders": False,
                "research_can_change_autonomy": False,
                "experimental_rl": "advisory_only",
            },
            "limits": {
                "max_runs": self.store.max_runs,
                "max_evidence": self.store.max_evidence,
                "max_concurrent": self.max_concurrent,
                "run_timeout_seconds": self.run_timeout_seconds,
                "max_statements": MAX_STATEMENTS,
                "max_news": MAX_NEWS,
                "max_peers": MAX_PEERS,
                "max_macro": MAX_MACRO,
            },
            "no_paid_data_subscription": True,
            "no_broker_integration": True,
        }

    def latest(self, instrument_id: str, *, horizon: str = "") -> dict[str, Any] | None:
        run = self.store.latest_completed(instrument_id, horizon)
        return _summary(run) if run else None

    def health(self) -> dict[str, Any]:
        return {
            **self.store.health(),
            "max_concurrent": self.max_concurrent,
            "run_timeout_seconds": self.run_timeout_seconds,
            "advisory_only": True,
            "can_mutate_portfolio": False,
            "can_submit_orders": False,
            "can_change_autonomy": False,
            "nm08_risk_authority": True,
            "nm10_execution_authority": True,
        }

    def close(self) -> None:
        self.store.close()

    def _request_from_run(self, run: Mapping[str, Any]) -> ResearchRequest:
        return ResearchRequest(
            research_id=str(run["run_id"]),
            instrument_id=str(run["instrument_id"]),
            as_of=datetime.fromisoformat(str(run["as_of"])),
            company_name=str(run.get("company_name") or ""),
            exchange=str(run.get("exchange") or ""),
            horizon=str(run.get("horizon") or "5d"),
            objective=str(run.get("objective") or "full_research"),
            analysis_types=tuple(run.get("analysis_types") or ()),
            publication_cutoff=optional_time(run.get("publication_cutoff"), "publication_cutoff"),
            portfolio_id=str(run.get("portfolio_id") or ""),
            parent_run_id=str(run.get("parent_run_id") or ""),
            max_evidence_age_hours=(run.get("config") or {}).get("max_evidence_age_hours", 720.0),
            requested_by=(run.get("config") or {}).get("requested_by", ""),
            idempotency_key=str(run.get("idempotency_key") or ""),
            version=int(run.get("version") or 1),
            created_at=datetime.fromisoformat(
                str(run.get("created_at") or self.clock().isoformat())
            ),
        )

    def _check_cancel(self, run_id: str, started: float) -> None:
        if run_id in self._cancelled:
            raise ResearchStateError("research run was cancelled")
        if time.monotonic() - started > self.run_timeout_seconds:
            raise ResearchStateError("research run exceeded the configured timeout")

    def _finish_cancelled(self, run_id: str) -> dict[str, Any]:
        run = self.store.run(run_id)
        if run.get("status") in {item.value for item in TERMINAL_STATUSES}:
            return run
        run["status"] = ResearchStatus.CANCELLED.value
        run["finished_at"] = self.clock().isoformat()
        self.store.update_run(run_id, run)
        return run

    def _finish_failed(self, run_id: str, reason: str) -> dict[str, Any]:
        run = self.store.run(run_id)
        if run.get("status") in {item.value for item in TERMINAL_STATUSES}:
            return run
        run["status"] = ResearchStatus.FAILED.value
        run["finished_at"] = self.clock().isoformat()
        run["error"] = reason
        self.store.update_run(run_id, run)
        return run

    def _persist_report(self, run_id: str, report: Mapping[str, Any]) -> None:
        evidence = report.get("evidence") or []
        self.store.save_evidence(run_id, evidence)
        self.store.save_claims(run_id, report.get("claims") or [])
        thesis = report.get("thesis")
        if isinstance(thesis, Mapping):
            self.store.save_thesis(run_id, thesis)
        for section in (
            "company",
            "financials",
            "events",
            "news",
            "sector",
            "peers",
            "macro",
            "valuation",
            "features",
            "handoff",
            "evidence_excluded",
            "evidence_summary",
            "market_context",
            "forecast_context",
            "portfolio_context",
        ):
            value = report.get(section)
            if isinstance(value, Mapping):
                self.store.save_section(run_id, section, value)

    def _assemble(
        self,
        request: ResearchRequest,
        context: Mapping[str, Any],
        *,
        deadline: float,
    ) -> dict[str, Any]:
        cutoff = request.publication_cutoff or request.as_of
        warnings: list[str] = [
            clean(item, limit=500)
            for item in (context.get("source_notes") or ())
            if clean(item, limit=500)
        ]
        limitations: list[str] = []
        sections: list[str] = []
        analysis_types = set(request.analysis_types)
        identity = dict(_mapping(context.get("instrument")))
        for key in ("name", "company_name", "symbol", "exchange", "sector", "industry"):
            if context.get(key) and not identity.get(key):
                identity[key] = context[key]
        if request.company_name and not identity.get("name"):
            identity["name"] = request.company_name
        if request.exchange and not identity.get("exchange"):
            identity["exchange"] = request.exchange
        symbol = str(identity.get("symbol") or request.instrument_id.split(":", 1)[-1])
        identity.setdefault("symbol", symbol)
        identity.setdefault("exchange", request.exchange)
        identity.setdefault("name", request.company_name or symbol)

        price, price_warnings, price_source = self._price(
            context,
            cutoff,
            as_of=request.as_of,
            max_age_hours=request.max_evidence_age_hours,
        )
        warnings.extend(price_warnings)
        price_context = _mapping(context.get("price"))
        price_currency = str(
            context.get("price_currency") or price_context.get("currency") or "INR"
        ).upper()
        price_as_of = (
            price_context.get("as_of")
            or price_context.get("published_at")
            or price_context.get("timestamp")
            or request.as_of.isoformat()
        )
        statements: list[FinancialStatement] = []
        for row in _rows(context.get("statements"))[:MAX_STATEMENTS]:
            try:
                statements.append(
                    FinancialStatement.from_payload(
                        row, instrument_id=request.instrument_id, default_time=cutoff
                    )
                )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"financial statement rejected: {type(exc).__name__}: {exc}")
        raw_evidence: list[EvidenceRecord] = []
        supplied_evidence = _rows(context.get("evidence"))
        if len(supplied_evidence) > MAX_EVIDENCE:
            warnings.append(f"evidence was truncated to the configured {MAX_EVIDENCE} record limit")
        for row in supplied_evidence[:MAX_EVIDENCE]:
            try:
                raw_evidence.append(
                    EvidenceRecord.from_payload(
                        row, instrument_id=request.instrument_id, default_time=cutoff
                    )
                )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"evidence rejected: {type(exc).__name__}: {exc}")
        supplied_keys = {
            (item.kind, item.source_id, item.claim, item.published_at)
            for item in raw_evidence
        }
        raw_evidence.extend(
            item
            for item in _payload_evidence(
                context,
                instrument_id=request.instrument_id,
                cutoff=cutoff,
                warnings=warnings,
            )
            if (item.kind, item.source_id, item.claim, item.published_at) not in supplied_keys
        )
        # A statement is itself a source-bearing record.  Its values are never
        # copied into an evidence claim; the record only points at the filing.
        known = {item.evidence_id for item in raw_evidence}
        for statement in statements:
            if statement.evidence_id and statement.evidence_id in known:
                continue
            source_id = (
                clean(context.get("statement_source_id"), limit=160)
                or "caller:financial_statements"
            )
            source_name = (
                clean(context.get("statement_source_name"), limit=200)
                or "Caller-supplied financial statement"
            )
            # Normalized statements fall back to statement_id as their evidence
            # reference, so the generated evidence record must use that exact id.
            # A separate synthetic id would leave thesis factors pointing at an
            # evidence record that does not exist.
            evidence_id = statement.evidence_id or statement.statement_id
            raw_evidence.append(
                EvidenceRecord(
                    evidence_id=evidence_id,
                    instrument_id=request.instrument_id,
                    kind=EvidenceKind.FINANCIAL_STATEMENT.value,
                    source_id=source_id,
                    source_name=source_name,
                    claim=(
                        f"{statement.statement_type} statement for "
                        f"{statement.period_end.date()} "
                        f"({statement.basis}, {statement.period_type})"
                    ),
                    published_at=statement.published_at,
                    retrieved_at=statement.retrieved_at,
                    reporting_period=statement.period_end.date().isoformat(),
                    currency=statement.currency,
                    verification=statement.verification,
                    source_tier=(
                        SourceTier.OFFICIAL_SECONDARY.value
                        if statement.verification == VerificationStatus.VERIFIED.value
                        else SourceTier.UNKNOWN.value
                    ),
                    metadata={
                        "statement_id": statement.statement_id,
                        "kind": "statement_reference",
                    },
                )
            )
            known.add(evidence_id)
        retained, claims, excluded_evidence, verification_warnings = verify_evidence(
            raw_evidence,
            as_of=request.as_of,
            cutoff=cutoff,
            max_age_hours=request.max_evidence_age_hours,
            allowed_sources=self.allowed_sources,
        )
        warnings.extend(verification_warnings)
        for excluded_item in excluded_evidence:
            warnings.append(
                "evidence excluded: "
                f"{excluded_item.get('evidence_id') or excluded_item.get('source')}: "
                f"{excluded_item.get('reason')}"
            )
        profile = build_company_profile(
            instrument_id=request.instrument_id,
            identity=identity,
            evidence=retained,
            as_of=request.as_of,
            cutoff=cutoff,
        )
        warnings.extend(profile.warnings)
        if profile.missing_fields:
            warnings.append(
                "company profile fields unavailable: " + ", ".join(profile.missing_fields)
            )
        company_section = {
            "requested": "company" in analysis_types,
            **profile.to_dict(),
            "identity_point_in_time_note": (
                "Instrument name, symbol, exchange, sector and industry are current "
                "NM-02 registry metadata; sourced historical profile fields are "
                "filtered by the research cutoff."
            ),
        }
        if "company" in analysis_types:
            sections.append("company")

        statements_for_analysis = statements[:MAX_STATEMENTS]
        analysis = analyze_financials(
            statements_for_analysis,
            as_of=request.as_of,
            cutoff=cutoff,
            max_age_hours=request.max_evidence_age_hours,
            price=price,
            price_currency=price_currency,
            shares_outstanding=optional_float(context.get("shares_outstanding")),
        )
        warnings.extend(analysis.warnings)
        if "statements" in analysis_types or "ratios" in analysis_types:
            sections.append("financials")
        financial_section = {
            "requested": bool({"statements", "ratios"} & analysis_types),
            **analysis.to_dict(),
            "price": {
                "value": price,
                "currency": price_currency,
                "source": price_source,
                "as_of": price_as_of,
            },
        }

        events, event_excluded, event_warnings = build_corporate_events(
            instrument_id=request.instrument_id,
            records=retained,
            payload_events=_rows(context.get("corporate_events") or context.get("events")),
            as_of=request.as_of,
            cutoff=cutoff,
        )
        warnings.extend(event_warnings)
        if "events" in analysis_types:
            sections.append("events")
        events_section = {
            "requested": "events" in analysis_types,
            "events": [item.to_dict() for item in events],
            "excluded": event_excluded,
            "announcement_effective_rule": (
                "announcement does not imply occurrence; effective events are dated separately"
            ),
        }

        news, news_excluded, news_warnings = build_news(
            instrument_id=request.instrument_id,
            records=retained,
            payload_news=_rows(context.get("news")),
            as_of=request.as_of,
            cutoff=cutoff,
        )
        enriched_news, enrich_warnings, enrich_excluded = enrich_news(
            news,
            as_of=request.as_of,
            cutoff=cutoff,
            max_age_hours=request.max_evidence_age_hours,
        )
        warnings.extend(news_warnings + enrich_warnings)
        for excluded_article in news_excluded:
            article_key = (
                excluded_article.get("article_id")
                or excluded_article.get("evidence_id")
                or excluded_article.get("source")
            )
            warnings.append(f"news excluded: {article_key}: {excluded_article.get('reason')}")
        for excluded_reason in enrich_excluded:
            warnings.append(f"news excluded: {excluded_reason}")
        if "news" in analysis_types:
            sections.append("news")
        news_section = {
            "requested": "news" in analysis_types,
            "articles": [item.to_dict() for item in enriched_news[:MAX_NEWS]],
            "excluded": news_excluded + [{"reason": item} for item in enrich_excluded],
            "sentiment_is_price_direction": False,
            "ai_summary_is_independent_source": False,
        }

        sector, sector_warnings = build_sector(
            target_sector=profile.sector,
            records=retained,
            payload=_mapping(context.get("sector")),
            as_of=request.as_of,
            cutoff=cutoff,
        )
        warnings.extend(sector_warnings)
        if "sector" in analysis_types:
            sections.append("sector")
        sector_section = {
            "requested": "sector" in analysis_types,
            "target_sector": profile.sector,
            "target_industry": profile.industry,
            "snapshot": sector.to_dict() if sector else None,
            "source_and_period_required": True,
        }

        peers, peer_excluded, peer_warnings = build_peers(
            payload=_rows(context.get("peers"))[:MAX_PEERS],
            as_of=request.as_of,
            cutoff=cutoff,
        )
        warnings.extend(peer_warnings)
        statement_currency = (
            analysis.statements[-1].get("normalized_currency") if analysis.statements else "INR"
        )
        target_metrics = dict(analysis.ratios.values)
        target_metrics["market_cap"] = analysis.ratios.inputs.get("market_cap")
        target_metrics["revenue_growth"] = analysis.ratios.values.get("revenue_growth")
        comparison = peer_comparison(
            target=target_metrics,
            industry=profile.industry,
            currency=str(statement_currency or "INR"),
            peers=peers,
            as_of=request.as_of,
            cutoff=cutoff,
            market_cap=optional_float(target_metrics.get("market_cap")),
        )
        if "peers" in analysis_types:
            sections.append("peers")
        peer_section = {
            "requested": "peers" in analysis_types,
            "industry": profile.industry,
            "target_currency": statement_currency,
            **comparison,
            "payload_excluded": peer_excluded,
        }

        macro_rows: list[Mapping[str, Any]] = []
        for row in _rows(context.get("macro"))[:MAX_MACRO]:
            macro_rows.append({**dict(row), "relevant": bool(row.get("relevant", False))})
        macro, macro_excluded, macro_warnings = build_macro(
            payload=macro_rows,
            as_of=request.as_of,
            cutoff=cutoff,
        )
        warnings.extend(macro_warnings)
        if "macro" in analysis_types:
            sections.append("macro")
        macro_section = {
            "requested": "macro" in analysis_types,
            "observations": [item.to_dict() for item in macro],
            "excluded": macro_excluded,
            "relevance_rule": (
                "an indicator is relevant only when the caller supplies a relevance note; "
                "macro is not assumed to affect every instrument"
            ),
        }

        medians_value = comparison.get("medians")
        peer_medians: Mapping[str, Any] = (
            medians_value if isinstance(medians_value, Mapping) else {}
        )
        ranges_value = comparison.get("ranges")
        peer_ranges: Mapping[str, Any] = (
            ranges_value if isinstance(ranges_value, Mapping) else {}
        )
        valuation = self._valuation(
            context=context,
            analysis=analysis,
            peer_medians=peer_medians,
            peer_ranges=peer_ranges,
            price=price,
            warnings=warnings,
        )
        if "valuation" in analysis_types:
            sections.append("valuation")
        valuation_section = {
            "requested": "valuation" in analysis_types,
            "result": valuation.to_dict() if valuation else None,
            "methodology_note": (
                "derived estimate only; not an objectively verified intrinsic value"
            ),
        }

        forecast = forecast_context(
            _rows(context.get("forecasts") or context.get("forecast")),
            horizon=request.horizon,
            cutoff=cutoff,
        )
        portfolio_context = self._portfolio_context(
            request=request, context=context, analysis=analysis, profile=profile, warnings=warnings
        )
        prior_thesis: Mapping[str, Any] | None = None
        if isinstance(context.get("prior_thesis"), Mapping):
            prior_thesis = context["prior_thesis"]
        elif request.parent_run_id:
            try:
                prior_thesis = self.store.thesis(request.parent_run_id)
            except ResearchNotFoundError:
                warnings.append(
                    "parent research thesis was not found; version comparison is unavailable"
                )
        thesis = build_thesis(
            request=request,
            profile=profile,
            analysis=analysis,
            events=events,
            news=enriched_news,
            sector=sector,
            peers=comparison,
            valuation=valuation,
            forecast_context=forecast,
            portfolio_context=portfolio_context,
            evidence=[item.to_dict() for item in retained],
            claims=claims,
            current_price=price,
            prior_thesis=prior_thesis,
        )
        if "thesis" in analysis_types:
            sections.append("thesis")
        features = self._features(
            analysis=analysis,
            events=events,
            cutoff=cutoff,
            request=request,
        )
        handoff = self._handoff(
            request=request,
            analysis=analysis,
            profile=profile,
            events=events,
            news=enriched_news,
            sector=sector,
            peers=comparison,
            macro=macro,
            valuation=valuation,
            thesis=thesis,
            forecast=forecast,
            portfolio_context=portfolio_context,
            claims=claims,
        )
        if not retained and not statements:
            warnings.append("no point-in-time evidence or financial statement was supplied")
        evidence_summary = self._evidence_summary(retained, excluded_evidence)
        status = self._status(
            retained=retained,
            statements=statements,
            claims=claims,
            warnings=warnings,
            source_unavailable=bool(context.get("source_unavailable")),
        )
        limitations.extend(
            [
                "PI-19 does not provide a paid market-data subscription or a live filing feed.",
                (
                    "Narrative conclusions are research interpretations, not verified "
                    "facts or execution instructions."
                ),
                (
                    "NM-08 remains the sole risk authority and NM-10 remains the sole "
                    "paper-execution authority."
                ),
            ]
        )
        if not retained:
            limitations.append("No eligible external evidence was available for this run.")
        report = {
            "run_id": request.research_id,
            "instrument_id": request.instrument_id,
            "company_name": profile.name,
            "exchange": profile.exchange,
            "horizon": request.horizon,
            "horizon_group": HORIZONS[request.horizon]["group"],
            "valid_until": (
                request.as_of
                + timedelta(
                    days={"intraday": 1, "swing": 7, "long_term": 30}[
                        HORIZONS[request.horizon]["group"]
                    ]
                )
            ).isoformat(),
            "objective": request.objective,
            "analysis_types": list(request.analysis_types),
            "as_of": request.as_of.isoformat(),
            "publication_cutoff": _iso(request.publication_cutoff),
            "portfolio_id": request.portfolio_id,
            "parent_run_id": request.parent_run_id,
            "status": status,
            "version": request.version,
            "config": {
                "contract": RESEARCH_CONTRACT_VERSION,
                "schema_version": RESEARCH_SCHEMA_VERSION,
                "max_evidence_age_hours": request.max_evidence_age_hours,
                "run_timeout_seconds": self.run_timeout_seconds,
                "allowed_sources": list(self.allowed_sources),
                "advisory_only": True,
                "can_mutate_portfolio": False,
                "can_submit_orders": False,
            },
            "sections": sections,
            "company": company_section,
            "financials": financial_section,
            "events": events_section,
            "news": news_section,
            "sector": sector_section,
            "peers": peer_section,
            "macro": macro_section,
            "valuation": valuation_section,
            "forecast_context": forecast,
            "portfolio_context": portfolio_context,
            "thesis": thesis.to_dict(),
            "features": features,
            "market_context": {
                "pi04_features": dict(_mapping(context.get("features"))),
                "regime": dict(_mapping(context.get("regime"))),
                "source_notes": list(context.get("source_notes") or ()),
                "feature_injection_rule": (
                    "PI-04 context is read-only metadata; unverified narrative is never injected "
                    "as a numerical prediction feature."
                ),
            },
            "handoff": handoff,
            "evidence": [item.to_dict() for item in retained],
            "evidence_excluded": excluded_evidence,
            "evidence_summary": evidence_summary,
            "claims": claims,
            "warnings": list(dict.fromkeys(warnings)),
            "limitations": list(dict.fromkeys(limitations)),
            "advisory_only": True,
            "is_execution_authorization": False,
            "can_mutate_portfolio": False,
            "can_submit_orders": False,
            "nm08_risk_authority": True,
            "nm10_execution_authority": True,
        }
        return report

    def _price(
        self,
        context: Mapping[str, Any],
        cutoff: datetime,
        *,
        as_of: datetime,
        max_age_hours: float,
    ) -> tuple[float | None, list[str], str]:
        raw = context.get("price")
        warnings: list[str] = []
        if isinstance(raw, Mapping):
            value = optional_float(raw.get("value") or raw.get("price"))
            source = clean(raw.get("source") or raw.get("source_name"), limit=200)
            try:
                published = optional_time(
                    raw.get("as_of") or raw.get("published_at") or raw.get("timestamp"),
                    "price timestamp",
                )
            except Exception as exc:  # noqa: BLE001 - invalid quote is not a run failure
                return (
                    None,
                    [f"price snapshot timestamp rejected: {type(exc).__name__}: {exc}"],
                    source,
                )
            if value is None:
                return None, ["price snapshot has no numeric value"], source
            if published is None:
                return None, ["price snapshot has no timestamp and was excluded"], source
            if published > cutoff:
                return (
                    None,
                    ["price snapshot is after the research cutoff and was excluded"],
                    source,
                )
            age_hours = max(0.0, (as_of - published).total_seconds() / 3600.0)
            if age_hours > max_age_hours:
                return (
                    None,
                    [
                        "price snapshot is stale by the configured evidence limit "
                        f"({age_hours:.1f}h > {max_age_hours:.1f}h) and was excluded"
                    ],
                    source,
                )
            return value, warnings, source
        value = optional_float(raw)
        if value is not None:
            warnings.append(
                "price was supplied without a source timestamp; it is treated as a "
                "caller snapshot at as_of"
            )
        return value, warnings, "caller snapshot"

    def _valuation(
        self,
        *,
        context: Mapping[str, Any],
        analysis: Any,
        peer_medians: Mapping[str, Any],
        peer_ranges: Mapping[str, Any],
        price: float | None,
        warnings: list[str],
    ) -> Any:
        raw = context.get("valuation_assumptions") or context.get("valuation")
        assumptions: ValuationAssumptions | None = None
        if isinstance(raw, Mapping):
            try:
                assumptions = ValuationAssumptions.from_payload(raw)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"valuation assumptions rejected: {type(exc).__name__}: {exc}")
        if assumptions is None and context.get("valuation_method") in {"relative", "dcf"}:
            warnings.append(
                "no explicit valuation assumptions were supplied; no valuation was estimated"
            )
            return None
        if assumptions is None:
            if not analysis.statements:
                return None
            assumptions = self._derive_relative_assumptions(analysis, peer_medians)
            if assumptions is None:
                return None
        if assumptions.method == "dcf":
            result = dcf_valuation(assumptions)
        else:
            result = relative_valuation(
                assumptions,
                peer_medians={key: optional_float(value) for key, value in peer_medians.items()},
                peer_min={
                    key: optional_float(value.get("min"))
                    for key, value in peer_ranges.items()
                    if isinstance(value, Mapping)
                },
                peer_max={
                    key: optional_float(value.get("max"))
                    for key, value in peer_ranges.items()
                    if isinstance(value, Mapping)
                },
            )
        if result.status != "completed":
            reason = result.warnings[0] if result.warnings else result.status
            warnings.append(f"valuation {assumptions.method}: {reason}")
        return result

    def _derive_relative_assumptions(
        self, analysis: Any, peer_medians: Mapping[str, Any]
    ) -> ValuationAssumptions | None:
        rows = analysis.statements
        if not rows:
            return None
        latest = rows[-1]
        ttm = analysis.ttm or {}
        income = ttm.get("income") or {}
        annual = latest.get("values") or {}
        revenue = (
            income.get("revenue") if income.get("revenue") is not None else annual.get("revenue")
        )
        net = (
            income.get("net_income")
            if income.get("net_income") is not None
            else annual.get("net_income")
        )
        balance = ttm.get("balance") or {}
        equity = (
            balance.get("equity") if balance.get("equity") is not None else annual.get("equity")
        )
        shares = latest.get("shares_outstanding") or analysis.ratios.inputs.get(
            "shares_outstanding"
        )
        eps = income.get("eps") if income.get("eps") is not None else annual.get("eps")
        if eps is None and net is not None and shares:
            eps = net / float(shares)
        book_per_share = equity / float(shares) if equity is not None and shares else None
        revenue_per_share = revenue / float(shares) if revenue is not None and shares else None
        ebitda = income.get("ebitda") if income.get("ebitda") is not None else annual.get("ebitda")
        fcf = analysis.ratios.values.get("free_cash_flow")
        peer_metric = "pe"
        for candidate in ("pe", "pb", "ps", "ev_ebitda"):
            if peer_medians.get(candidate) is not None:
                peer_metric = candidate
                break
        base = {
            "pe": eps,
            "pb": book_per_share,
            "ps": revenue_per_share,
            "ev_ebitda": ebitda,
            "ev_sales": revenue,
            "fcf_yield": fcf,
        }.get(peer_metric)
        if base is None or peer_medians.get(peer_metric) is None:
            return None
        return ValuationAssumptions.from_payload(
            {
                "method": "relative",
                "currency": str(latest.get("normalized_currency") or "INR"),
                "peer_metric": peer_metric,
                "peer_multiple": peer_medians.get(peer_metric),
                "base_metric": base,
                "shares_outstanding": shares,
                "net_debt": analysis.ratios.values.get("net_debt") or 0.0,
                "eps": eps,
                "book_value": book_per_share,
                "revenue": revenue,
                "ebitda": ebitda,
                "free_cash_flow": fcf,
                "notes": (
                    "derived from point-in-time statement metrics and documented peer medians",
                ),
            }
        )

    def _portfolio_context(
        self,
        *,
        request: ResearchRequest,
        context: Mapping[str, Any],
        analysis: Any,
        profile: Any,
        warnings: list[str],
    ) -> dict[str, Any]:
        raw = _mapping(context.get("portfolio") or context.get("portfolio_context"))
        allowed = (
            "portfolio_id",
            "exposure",
            "weight",
            "sector_exposure",
            "cash",
            "liquidity",
            "concentration",
            "as_of",
            "source",
        )
        sanitized = {key: raw[key] for key in allowed if key in raw}
        sanitized.update(
            {
                "portfolio_id": request.portfolio_id or sanitized.get("portfolio_id", ""),
                "read_only": True,
                "can_mutate_portfolio": False,
                "can_change_allocation": False,
                "requires_pi18_optimizer": True,
                "requires_nm08_risk_approval": True,
                "notes": [
                    "Portfolio context is read-only. A research thesis cannot change "
                    "an allocation.",
                ],
                "financial_health": {
                    "return_on_equity": analysis.ratios.values.get("return_on_equity"),
                    "return_on_invested_capital": analysis.ratios.values.get(
                        "return_on_invested_capital"
                    ),
                    "debt_to_equity": analysis.ratios.values.get("debt_to_equity"),
                    "current_ratio": analysis.ratios.values.get("current_ratio"),
                    "free_cash_flow": analysis.ratios.values.get("free_cash_flow"),
                },
                "business_concentration": {
                    "segments": profile.value("segments"),
                    "geographies": profile.value("geographies"),
                    "note": "concentration is descriptive, not a portfolio instruction",
                },
            }
        )
        if raw and not sanitized.get("source"):
            warnings.append(
                "portfolio context has no source; it is treated as caller-supplied "
                "read-only context"
            )
        return sanitized

    def _features(
        self,
        *,
        analysis: Any,
        events: Sequence[Any],
        cutoff: datetime,
        request: ResearchRequest,
    ) -> dict[str, Any]:
        features: dict[str, dict[str, Any]] = {}
        statement_evidence = tuple(
            str(item.get("evidence_id")) for item in analysis.statements if item.get("evidence_id")
        )
        for name, value in sorted(analysis.ratios.values.items()):
            if value is None or not isinstance(value, (int, float)):
                continue
            features[f"pi19.fundamental.{name}"] = {
                "value": float(value),
                "available_at": cutoff.isoformat(),
                "source": "pi19:point_in_time_ratios",
                "evidence_ids": list(statement_evidence),
                "feature_version": "pi19-features-v1",
                "research_only": True,
                "approved_for_production_forecasting": False,
                "requires_pi04_evaluation": True,
            }
        for event in events:
            if event.verification != VerificationStatus.VERIFIED.value:
                continue
            if event.event_type != "earnings":
                continue
            features["pi19.event.earnings_announcement"] = {
                "value": 1.0 if event.occurrence != "cancelled" else 0.0,
                "available_at": event.announced_at.isoformat(),
                "source": "pi19:verified_corporate_event",
                "evidence_ids": list(event.evidence_ids),
                "feature_version": "pi19-features-v1",
                "research_only": True,
                "approved_for_production_forecasting": False,
                "requires_pi04_evaluation": True,
                "event_effective_at": (
                    event.effective_at.isoformat() if event.effective_at else None
                ),
            }
        return {
            "feature_set_version": "pi19-features-v1",
            "contract": "pi04-research-features-v1",
            "point_in_time": cutoff.isoformat(),
            "horizon": request.horizon,
            "features": features,
            "automatic_retraining": False,
            "production_forecasting_use": False,
            "note": (
                "Research features are candidate inputs. PI-04 must version and "
                "evaluate them before production use."
            ),
        }

    def _handoff(
        self,
        *,
        request: ResearchRequest,
        analysis: Any,
        profile: Any,
        events: Sequence[Any],
        news: Sequence[Any],
        sector: Any,
        peers: Mapping[str, Any],
        macro: Sequence[Any],
        valuation: Any,
        thesis: Any,
        forecast: Mapping[str, Any],
        portfolio_context: Mapping[str, Any],
        claims: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        by_kind: dict[str, list[str]] = {}
        for claim in claims:
            kind = str(claim.get("kind") or "other")
            by_kind.setdefault(kind, []).append(str(claim.get("evidence_id")))
        verified_claims = [
            claim
            for claim in claims
            if claim.get("status")
            in {VerificationStatus.VERIFIED.value, VerificationStatus.PARTIALLY_VERIFIED.value}
        ]
        packages = {
            "market_research": {
                "evidence_ids": sorted(
                    {
                        *by_kind.get("news", []),
                        *by_kind.get("corporate_event", []),
                        *by_kind.get("sector", []),
                        *by_kind.get("macro", []),
                    }
                ),
                "facts": [
                    item.headline
                    for item in news
                    if item.verification != VerificationStatus.UNVERIFIED.value
                ],
                "interpretations": [item.to_dict() for item in news if item.sentiment != "neutral"],
                "cannot_assert": ["That news sentiment predicts a stock-price direction."],
            },
            "fundamental_analysis": {
                "evidence_ids": sorted(
                    {
                        *by_kind.get("financial_statement", []),
                        *by_kind.get("company_profile", []),
                    }
                ),
                "metrics": dict(analysis.ratios.values),
                "profile_missing": list(profile.missing_fields),
                "cannot_assert": ["A valuation estimate is a verified market price."],
            },
            "technical_analysis": {
                "evidence_ids": sorted(by_kind.get("market_data", [])),
                "price_context_available": bool(analysis.ratios.inputs.get("price") is not None),
                "cannot_assert": [
                    "Fundamental evidence alone establishes intraday technical direction."
                ],
            },
            "prediction_intelligence": {
                "forecast_context": dict(forecast),
                "cannot_assert": [
                    "A forecast is certain or calibrated without validation evidence."
                ],
            },
            "market_regime": {
                "sector": sector.to_dict() if sector else None,
                "macro": [item.to_dict() for item in macro],
                "cannot_assert": ["Macro indicators are relevant to every instrument."],
            },
            "portfolio_intelligence": {
                "portfolio_context": dict(portfolio_context),
                "sector": sector.to_dict() if sector else None,
                "peer_comparison": dict(peers),
                "cannot_assert": ["Research may override PI-18 constraints or NM-08 risk limits."],
            },
            "trading_decision": {
                "thesis": thesis.to_dict(),
                "evidence_ids": [str(claim.get("evidence_id")) for claim in verified_claims],
                "decision_input_only": True,
                "requires_nm08": True,
                "requires_nm10": True,
                "cannot_submit_orders": True,
            },
        }
        return {
            "contract": "pi19-research-handoff-v1",
            "instrument_id": request.instrument_id,
            "horizon": request.horizon,
            "as_of": request.as_of.isoformat(),
            "publication_cutoff": _iso(request.publication_cutoff),
            "packages": packages,
            "evidence_references_preserved": True,
            "circular_evidence_rule": (
                "An agent interpretation is not independent verification; only source "
                "records may corroborate a claim."
            ),
            "pi18_inputs": {
                "financial_health": dict(portfolio_context.get("financial_health") or {}),
                "sector": sector.to_dict() if sector else None,
                "valuation": valuation.to_dict() if valuation else None,
                "event_risks": [
                    item.to_dict()
                    for item in events
                    if item.event_type
                    in {"litigation", "regulatory", "trading_suspension", "merger", "acquisition"}
                ],
                "research_uncertainty": thesis.confidence,
                "business_concentration": portfolio_context.get("business_concentration"),
                "advisory_only": True,
                "cannot_override_constraints": True,
            },
            "pi12_context": {
                "thesis": thesis.to_dict(),
                "forecast_context": dict(forecast),
                "research_uncertainty": thesis.confidence,
                "advisory_only": True,
                "cannot_submit_orders": True,
                "can_change_autonomy_mode": False,
                "allowed_modes": ["disabled", "monitoring", "autonomous", "paused"],
                "operator_authorization_required": True,
                "nm08_risk_authority": True,
                "nm10_execution_authority": True,
            },
            "authority": {
                "nm08": "sole risk authority",
                "nm10": "sole paper-execution authority",
                "research_can_mutate_portfolio": False,
                "research_can_write_ledger": False,
                "research_can_submit_orders": False,
                "research_can_change_autonomy": False,
                "experimental_rl": "advisory only",
            },
        }

    def _evidence_summary(
        self, retained: Sequence[EvidenceRecord], excluded: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for item in retained:
            counts[item.verification] = counts.get(item.verification, 0) + 1
        return {
            "eligible_count": len(retained),
            "excluded_count": len(excluded),
            "by_verification": dict(sorted(counts.items())),
            "verified_claim_count": sum(
                count
                for status, count in counts.items()
                if status
                in {VerificationStatus.VERIFIED.value, VerificationStatus.PARTIALLY_VERIFIED.value}
            ),
            "conflicting_count": counts.get(VerificationStatus.CONFLICTING_EVIDENCE.value, 0),
            "stale_count": counts.get(VerificationStatus.STALE.value, 0),
        }

    def _status(
        self,
        *,
        retained: Sequence[EvidenceRecord],
        statements: Sequence[FinancialStatement],
        claims: Sequence[Mapping[str, Any]],
        warnings: Sequence[str],
        source_unavailable: bool,
    ) -> str:
        if source_unavailable and not retained:
            return ResearchStatus.SOURCE_UNAVAILABLE.value
        if not retained and not statements:
            return ResearchStatus.INSUFFICIENT_EVIDENCE.value
        if retained and all(
            item.verification == VerificationStatus.STALE.value for item in retained
        ):
            return ResearchStatus.STALE_DATA.value
        if any(
            claim.get("status") == VerificationStatus.CONFLICTING_EVIDENCE.value for claim in claims
        ):
            return ResearchStatus.PARTIAL_EVIDENCE.value
        if warnings:
            return ResearchStatus.COMPLETED_WITH_WARNINGS.value
        return ResearchStatus.COMPLETED.value


def _summary(run: Mapping[str, Any]) -> dict[str, Any]:
    raw_thesis = run.get("thesis")
    thesis: Mapping[str, Any] = raw_thesis if isinstance(raw_thesis, Mapping) else {}
    return {
        "run_id": run.get("run_id"),
        "instrument_id": run.get("instrument_id"),
        "company_name": run.get("company_name"),
        "exchange": run.get("exchange"),
        "horizon": run.get("horizon"),
        "objective": run.get("objective"),
        "as_of": run.get("as_of"),
        "status": run.get("status"),
        "version": run.get("version"),
        "parent_run_id": run.get("parent_run_id"),
        "created_at": run.get("created_at"),
        "finished_at": run.get("finished_at"),
        "thesis_stance": thesis.get("stance"),
        "evidence_count": len(run.get("evidence") or []),
        "warning_count": len(run.get("warnings") or []),
        "advisory_only": True,
        "financial_mutation": False,
    }
