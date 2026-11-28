"""Phase 35 service: evidence-first business opportunity lifecycle.

The service is deliberately a consumer of NovaControl's existing seams:

* research is an injected async callable that wraps ``ExploreService``;
* state is a namespace in the existing ``JsonStateStore``;
* events go through the application's EventBus publisher and audit through the
  existing AuditLogger;
* no agent is given a direct "mark validated" capability, and no external action
  (contact, advertisement, purchase, campaign) is executed here at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import math
import re
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from novacontrol.business.analysis import (
    build_competition,
    build_customer_validation,
    build_market_research,
    build_report,
    decide_verdict,
    evidence_gaps,
    next_steps_for,
    plan_experiments,
)
from novacontrol.business.feasibility import assess_feasibility
from novacontrol.business.financial import analyse_financials
from novacontrol.business.models import (
    EvidenceKind,
    EvidenceQuality,
    EvidenceRecord,
    Experiment,
    FinancialAnalysis,
    Opportunity,
    OpportunityStage,
    SignalType,
    SourceRef,
    new_id,
    transition,
    utcnow,
)
from novacontrol.business.scoring import DEFAULT_WEIGHTS, score_opportunity

NAMESPACE = "business_opportunities"
SCHEMA_VERSION = 1


class BusinessError(ValueError):
    """A refused business operation with a safe explanation."""


class OpportunityNotFound(BusinessError):
    def __init__(self, opportunity_id: str) -> None:
        super().__init__(f"Opportunity {opportunity_id!r} was not found.")
        self.opportunity_id = opportunity_id


class DuplicateOpportunity(BusinessError):
    def __init__(self, existing_id: str, reason: str) -> None:
        super().__init__(reason)
        self.existing_id = existing_id


class BusinessConflict(BusinessError):
    """A duplicate concurrent run, an invalid lifecycle move or a budget."""


class ResearchUnavailable(BusinessError):
    pass


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _valid_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _token_set(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]{3,}", text.lower()) if token}


def _jaccard(left: str, right: str) -> float:
    a, b = _token_set(left), _token_set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _normalise(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _confidence(evidence: Sequence[EvidenceRecord]) -> float:
    if not evidence:
        return 0.0
    weights = {
        EvidenceQuality.PRIMARY: 1.0,
        EvidenceQuality.SECONDARY: 0.7,
        EvidenceQuality.ESTIMATE: 0.45,
        EvidenceQuality.SYNTHESIS: 0.25,
        EvidenceQuality.ASSUMPTION: 0.1,
    }
    total = sum(weights[row.quality] * max(0.0, min(1.0, row.confidence)) for row in evidence)
    freshness = 1.0 - (sum(1 for row in evidence if row.stale()) / len(evidence))
    return max(0.0, min(1.0, (total / len(evidence)) * (0.8 + 0.2 * freshness)))


def _merge_experiments(
    existing: Sequence[Experiment], planned: Sequence[Experiment]
) -> tuple[Experiment, ...]:
    """Keep approval/outcome history for experiments whose title is unchanged."""
    previous = {row.title: row for row in existing}
    merged: list[Experiment] = []
    for row in planned:
        old = previous.pop(row.title, None)
        if old is None:
            merged.append(row)
        else:
            merged.append(
                replace(
                    row,
                    id=old.id,
                    status=old.status,
                    approved_by=old.approved_by,
                    outcome=old.outcome,
                    result=old.result,
                    created_at=old.created_at,
                )
            )
    merged.extend(previous.values())
    return tuple(merged[:20])


class BusinessOpportunityService:
    """Discovery, research, evidence, assessment, scoring and verdicts."""

    def __init__(
        self,
        *,
        store: Any | None = None,
        research: Callable[[str, int], Awaitable[Sequence[Any]]] | None = None,
        audit: Callable[..., Any] | None = None,
        publish: Callable[[str, Mapping[str, Any]], Any] | None = None,
        knowledge: Callable[[str, str], Awaitable[Any] | Any] | None = None,
        resource_snapshot: Callable[[], Mapping[str, Any]] | None = None,
        actor: str = "operator",
        weights: Mapping[str, float] | None = None,
        threshold: float = 0.6,
        max_opportunities: int = 500,
        max_evidence_per_opportunity: int = 200,
        max_research_sources: int = 6,
        max_research_runs: int = 4,
        max_scan_runs: int = 20,
        research_timeout_seconds: float = 45.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 0.3 <= threshold <= 0.95:
            raise ValueError("threshold must be between 0.3 and 0.95.")
        self._store = store
        self._research_callable = research
        self._audit = audit
        self._publish_callback = publish
        self._knowledge = knowledge
        self._resource_snapshot = resource_snapshot
        self._actor = actor
        self._weights = dict(weights or DEFAULT_WEIGHTS)
        self.threshold = threshold
        self.max_opportunities = max(1, int(max_opportunities))
        self.max_evidence_per_opportunity = max(1, int(max_evidence_per_opportunity))
        self.max_research_sources = max(1, min(20, int(max_research_sources)))
        self.max_research_runs = max(0, int(max_research_runs))
        self.max_scan_runs = max(1, int(max_scan_runs))
        self.research_timeout_seconds = max(1.0, float(research_timeout_seconds))
        self._clock = clock or utcnow
        self._opportunities: dict[str, Opportunity] = {}
        self._loaded = False
        self._load_errors: list[str] = []
        self._write_lock = threading.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._active: set[str] = set()
        self._intent_guard = asyncio.Lock()
        self._scan_lock = asyncio.Lock()
        self._scan_runs = 0

    # ── persistence and retrieval ───────────────────────────────────────────

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._store is None:
            return
        payload = self._store.read(NAMESPACE)
        rows = payload.get("opportunities") if isinstance(payload, Mapping) else None
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            return
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            try:
                opportunity = Opportunity.from_dict(row)
            except Exception as exc:  # noqa: BLE001 - one corrupt row must not hide the rest
                self._load_errors.append(str(exc))
                continue
            self._opportunities[opportunity.id] = opportunity

    def _persist(self) -> None:
        if self._store is None:
            return
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": self._clock().isoformat(),
            "opportunities": [row.to_dict() for row in self._opportunities.values()],
        }
        with self._write_lock:
            self._store.write(NAMESPACE, payload)

    def list_opportunities(
        self,
        *,
        query: str = "",
        stage: str = "",
        industry: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._load()
        rows = sorted(
            self._opportunities.values(), key=lambda row: row.updated_at, reverse=True
        )
        if stage:
            rows = [row for row in rows if row.stage.value == stage]
        if industry:
            needle = industry.strip().lower()
            rows = [row for row in rows if needle in row.industry.lower()]
        if query:
            needle = query.strip().lower()
            rows = [
                row
                for row in rows
                if needle
                in " ".join(
                    (
                        row.title,
                        row.description,
                        row.customer_problem,
                        row.proposed_solution,
                        row.target_audience,
                        row.industry,
                        row.category,
                    )
                ).lower()
            ]
        total = len(rows)
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        page = rows[offset : offset + limit]
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "opportunities": [row.to_summary() for row in page],
        }

    def get_opportunity(self, opportunity_id: str) -> Opportunity:
        self._load()
        opportunity = self._opportunities.get(opportunity_id)
        if opportunity is None:
            raise OpportunityNotFound(opportunity_id)
        return opportunity

    def detail(self, opportunity_id: str) -> dict[str, Any]:
        opportunity = self.get_opportunity(opportunity_id)
        return {
            **opportunity.to_dict(),
            "available_transitions": sorted(
                row.value for row in _transitions_from(opportunity)
            ),
        }

    def compare(self, opportunity_ids: Sequence[str]) -> dict[str, Any]:
        if not 2 <= len(opportunity_ids) <= 5:
            raise BusinessError("Comparison needs between 2 and 5 opportunity ids.")
        rows = [self.get_opportunity(item) for item in dict.fromkeys(opportunity_ids)]
        return {
            "opportunities": [
                {
                    **row.to_summary(),
                    "weighted_score": None if row.score is None else row.score.weighted_score,
                    "evidence_coverage": None
                    if row.score is None
                    else row.score.evidence_coverage,
                    "verdict_rationale": row.verdict_rationale,
                    "feasibility": None
                    if row.feasibility is None
                    else row.feasibility.overall_score,
                    "financial_base": _base_value(row.financials),
                }
                for row in rows
            ],
            "note": "Comparison reports each opportunity's recorded evidence; it does not rank away missing evidence.",
        }

    def status(self) -> dict[str, Any]:
        self._load()
        stages: dict[str, int] = {}
        for row in self._opportunities.values():
            stages[row.stage.value] = stages.get(row.stage.value, 0) + 1
        return {
            "service": "business_opportunities",
            "namespace": NAMESPACE,
            "schema_version": SCHEMA_VERSION,
            "opportunities": len(self._opportunities),
            "by_stage": stages,
            "research_provider_configured": self._research_callable is not None,
            "knowledge_configured": self._knowledge is not None,
            "resource_snapshot": self._read_resources(),
            "limits": {
                "max_opportunities": self.max_opportunities,
                "max_evidence_per_opportunity": self.max_evidence_per_opportunity,
                "max_research_sources": self.max_research_sources,
                "max_research_runs_per_opportunity": self.max_research_runs,
                "max_scan_runs_per_session": self.max_scan_runs,
                "research_timeout_seconds": self.research_timeout_seconds,
                "score_threshold": self.threshold,
            },
            "responsibilities": (
                "opportunity_discovery",
                "market_research",
                "customer_problem_analysis",
                "competitive_intelligence",
                "feasibility_analysis",
                "business_economics",
                "validation_planning",
                "business_evaluation",
            ),
            "boundaries": {
                "external_actions_executed": False,
                "requires_operator_approval": "every experiment that contacts customers, publishes content or spends money",
                "validation_rule": "a verdict is derived from recorded evidence; an agent recommendation cannot set VALIDATED",
                "stale_evidence_days": 90,
            },
            "load_errors": list(self._load_errors),
        }

    def _transitions_from(self, opportunity: Opportunity) -> set[OpportunityStage]:
        return _transitions_from(opportunity)

    # ── discovery ───────────────────────────────────────────────────────────

    def discover(
        self,
        *,
        title: str,
        customer_problem: str,
        proposed_solution: str,
        target_audience: str = "",
        industry: str = "",
        category: str = "",
        description: str = "",
        signal_type: str = SignalType.USER_SUPPLIED.value,
        source_title: str = "",
        source_url: str = "",
        source_reference: str = "",
        source_publisher: str = "",
        source_excerpt: str = "",
        tags: Sequence[str] = (),
        actor: str = "",
    ) -> dict[str, Any]:
        self._load()
        actor = (actor or self._actor).strip() or "operator"
        if len(self._opportunities) >= self.max_opportunities:
            raise BusinessConflict(
                f"Opportunity cap ({self.max_opportunities}) reached; archive records before adding more."
            )
        try:
            kind = SignalType(str(signal_type))
        except ValueError as exc:
            raise BusinessError(f"Unknown signal_type {signal_type!r}.") from exc
        clean_title = " ".join(title.split())
        clean_problem = " ".join(customer_problem.split())
        clean_solution = " ".join(proposed_solution.split())
        if not clean_title or not clean_problem or not clean_solution:
            raise BusinessError("title, customer_problem and proposed_solution are required.")
        source_url = source_url.strip()
        if source_url and not _valid_http_url(source_url):
            raise BusinessError("source_url must be an http(s) URL.")
        reference = (
            source_reference.strip()
            or source_title.strip()
            or source_url
            or f"operator brief supplied by {actor}"
        )
        if not source_url and not source_title.strip() and not source_reference.strip():
            raise BusinessError(
                "Discovery needs a source reference (a URL or a brief/reference title); "
                "an unsourced idea cannot carry evidence."
            )
        duplicate_key = hashlib.sha256(
            "|".join(
                _normalise(value)
                for value in (clean_title, industry, category, clean_problem, target_audience)
            ).encode("utf-8")
        ).hexdigest()
        for existing in self._opportunities.values():
            if existing.duplicate_key and existing.duplicate_key == duplicate_key:
                raise DuplicateOpportunity(
                    existing.id, f"Duplicate opportunity: {existing.id} has the same discovery key."
                )
            if (
                _normalise(existing.title) == _normalise(clean_title)
                and _normalise(existing.industry) == _normalise(industry)
            ):
                raise DuplicateOpportunity(
                    existing.id, f"A matching opportunity already exists: {existing.id}."
                )
            if _jaccard(existing.title, clean_title) >= 0.82 and _normalise(
                existing.customer_problem
            ) == _normalise(clean_problem):
                raise DuplicateOpportunity(
                    existing.id,
                    f"A near-duplicate opportunity already exists: {existing.id}.",
                )
        now = self._clock()
        source = SourceRef(
            id=new_id("src"),
            title=source_title.strip() or reference,
            url=source_url,
            source_type="web" if source_url else "operator_reference",
            publisher=source_publisher.strip(),
            retrieved_at=now,
            excerpt=(source_excerpt or reference)[:1000],
            supplied_by=actor,
        )
        quality = (
            EvidenceQuality.SECONDARY
            if source_url
            else EvidenceQuality.ASSUMPTION
        )
        evidence = EvidenceRecord(
            id=new_id("ev"),
            kind=EvidenceKind.OTHER,
            claim=(
                f"Discovery source for {clean_title}: {source_excerpt or reference}"
            )[:1000],
            quality=quality,
            source=source,
            confidence=0.45 if source_url else 0.15,
            recorded_at=now,
            supplied_by=actor,
            value={"signal_type": kind.value, "discovery": True},
            notes=(
                "External source recorded during discovery; classification and claims "
                "require the operator or a later assessment."
                if source_url
                else "Operator-supplied brief; this is an assumption, not external market evidence."
            ),
        )
        opportunity = Opportunity(
            id=new_id("opp"),
            title=clean_title,
            description=" ".join(description.split()) or clean_solution,
            industry=" ".join(industry.split()),
            category=" ".join(category.split()),
            customer_problem=clean_problem,
            proposed_solution=clean_solution,
            target_audience=" ".join(target_audience.split()),
            signal_type=kind,
            discovered_at=now,
            updated_at=now,
            source_refs=(source,),
            evidence=(evidence,),
            confidence=_confidence((evidence,)),
            uncertainty=(
                "No market or competitor evidence has been collected yet.",
                "No primary customer evidence has been supplied.",
            ),
            tags=tuple(" ".join(str(tag).split()) for tag in tags if str(tag).strip()),
            metadata={
                "discovery_reference": reference,
                "evidence_classification": "unclassified discovery source",
            },
            duplicate_key=duplicate_key,
        )
        self._opportunities[opportunity.id] = opportunity
        self._persist()
        self._emit(
            "business.opportunity.discovered",
            {
                "opportunity_id": opportunity.id,
                "stage": opportunity.stage.value,
                "signal_type": kind.value,
                "evidence_count": len(opportunity.evidence),
            },
        )
        self._record_audit(
            action="business.opportunity.discovered",
            opportunity=opportunity,
            actor=actor,
            detail=f"Discovered {opportunity.title}",
        )
        return self.detail(opportunity.id)

    # ── evidence ────────────────────────────────────────────────────────────

    def add_evidence(
        self,
        opportunity_id: str,
        *,
        kind: str,
        claim: str,
        quality: str,
        source_title: str = "",
        source_url: str = "",
        source_reference: str = "",
        source_type: str = "",
        source_excerpt: str = "",
        source_publisher: str = "",
        retrieved_at: str = "",
        supplied_by: str = "",
        confidence: float = 0.5,
        value: Mapping[str, Any] | None = None,
        supports: Sequence[str] = (),
        notes: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        opportunity = self.get_opportunity(opportunity_id)
        actor = (actor or self._actor).strip() or "operator"
        self._assert_mutable(opportunity, "add evidence")
        if len(opportunity.evidence) >= self.max_evidence_per_opportunity:
            raise BusinessConflict(
                f"Evidence cap ({self.max_evidence_per_opportunity}) reached for this opportunity."
            )
        try:
            evidence_kind = EvidenceKind(str(kind))
            evidence_quality = EvidenceQuality(str(quality))
        except ValueError as exc:
            raise BusinessError(f"Unknown evidence kind or quality: {exc}") from exc
        clean_claim = " ".join(str(claim).split())
        if not clean_claim:
            raise BusinessError("Evidence needs a claim.")
        clean_supplied_by = supplied_by.strip() or actor
        if evidence_quality is EvidenceQuality.PRIMARY and not supplied_by.strip():
            raise BusinessError(
                "Primary evidence must name the person who supplied it (supplied_by)."
            )
        clean_url = source_url.strip()
        if clean_url and not _valid_http_url(clean_url):
            raise BusinessError("source_url must be an http(s) URL.")
        reference = source_reference.strip() or source_title.strip() or clean_url
        source: SourceRef | None = None
        if clean_url or reference:
            source = SourceRef(
                id=new_id("src"),
                title=source_title.strip() or reference or clean_url,
                url=clean_url,
                source_type=source_type.strip()
                or ("web" if clean_url else "operator_reference"),
                publisher=source_publisher.strip(),
                retrieved_at=_parse_dt(retrieved_at) or self._clock(),
                excerpt=(source_excerpt or clean_claim)[:1000],
                supplied_by=clean_supplied_by,
            )
        if evidence_quality.measured and source is None:
            raise BusinessError(
                "Measured evidence needs a source URL or a named source reference."
            )
        confidence_value = float(confidence)
        if not math.isfinite(confidence_value):
            raise BusinessError("Evidence confidence must be a finite number.")
        record = EvidenceRecord(
            id=new_id("ev"),
            kind=evidence_kind,
            claim=clean_claim,
            quality=evidence_quality,
            source=source,
            confidence=max(0.0, min(1.0, confidence_value)),
            supports=tuple(supports),
            recorded_at=self._clock(),
            supplied_by=clean_supplied_by,
            value=dict(value or {}),
            notes=notes,
        )
        updated = replace(
            opportunity,
            evidence=(*opportunity.evidence, record),
            updated_at=self._clock(),
            confidence=_confidence((*opportunity.evidence, record)),
        )
        if source is not None and source.id not in {row.id for row in updated.source_refs}:
            updated = replace(updated, source_refs=(*updated.source_refs, source))
        # A new record invalidates the old market/customer/feasibility/score and
        # report. Leaving them on the record while the evidence changed is how a
        # stale verdict gets presented as current.
        updated = self._invalidate_analysis(updated)
        self._opportunities[opportunity.id] = updated
        self._persist()
        self._emit(
            "business.evidence.added",
            {
                "opportunity_id": opportunity.id,
                "stage": updated.stage.value,
                "evidence_count": len(updated.evidence),
                "quality": evidence_quality.value,
                "kind": evidence_kind.value,
            },
        )
        self._record_audit(
            action="business.evidence.added",
            opportunity=updated,
            actor=actor,
            detail=f"Added {evidence_quality.value} {evidence_kind.value} evidence",
        )
        return self.detail(opportunity.id)

    # ── discovery scan ──────────────────────────────────────────────────────

    async def scan_signals(
        self,
        *,
        query: str,
        signal_type: str = SignalType.MARKET_TREND.value,
        limit: int = 5,
        actor: str = "",
    ) -> dict[str, Any]:
        """Use Explore to find source-backed candidate signals.

        A candidate is NOT an opportunity: no customer problem, solution or
        market claim is invented. The operator must record the brief and its
        source before an opportunity exists. This keeps autonomous discovery
        from turning a search result into a fabricated business idea.
        """
        self._load()
        actor = (actor or self._actor).strip() or "operator"
        clean_query = " ".join(query.split())
        if not clean_query:
            raise BusinessError("A discovery query is required.")
        try:
            kind = SignalType(str(signal_type))
        except ValueError as exc:
            raise BusinessError(f"Unknown signal_type {signal_type!r}.") from exc
        if self._research_callable is None:
            raise ResearchUnavailable("No Explore research provider is configured.")
        if self._scan_lock.locked():
            raise BusinessConflict("A discovery scan is already running; duplicate scans were refused.")
        self._scan_runs += 1
        if self._scan_runs > self.max_scan_runs:
            raise BusinessConflict(
                f"Discovery scan budget reached ({self.max_scan_runs} runs for this process)."
            )
        count = max(1, min(self.max_research_sources, int(limit)))
        async with self._scan_lock:
            try:
                result = await asyncio.wait_for(
                    self._research_callable(clean_query, count),
                    timeout=self.research_timeout_seconds,
                )
            except (TimeoutError, asyncio.TimeoutError) as exc:
                raise ResearchUnavailable(
                    f"Discovery scan exceeded {self.research_timeout_seconds:.0f}s."
                ) from exc
            except Exception as exc:  # noqa: BLE001 - provider failure is reported
                raise ResearchUnavailable(f"Discovery provider unavailable: {exc}") from exc
            items = list(result) if isinstance(result, Sequence) and not isinstance(result, (str, bytes)) else []
            candidates: list[dict[str, Any]] = []
            seen: set[str] = set()
            warnings: list[str] = []
            for item in items[:count]:
                payload = item if isinstance(item, Mapping) else _item_mapping(item)
                url = str(payload.get("url", "")).strip()
                title = str(payload.get("title", "")).strip()
                if not _valid_http_url(url):
                    warnings.append(f"Skipped an un-sourced result: {title or 'untitled'}")
                    continue
                if url in seen:
                    continue
                seen.add(url)
                candidates.append(
                    {
                        "id": new_id("signal"),
                        "title": title or url,
                        "source_url": url,
                        "excerpt": str(payload.get("snippet", payload.get("excerpt", "")))[:1000],
                        "signal_type": kind.value,
                        "retrieved_at": (
                            _parse_dt(payload.get("retrieved_at")) or self._clock()
                        ).isoformat(),
                        "customer_problem": "",
                        "proposed_solution": "",
                        "note": (
                            "Source-backed candidate signal only. No customer problem or solution "
                            "was inferred; record a brief with this source to create an opportunity."
                        ),
                    }
                )
            if not candidates and not warnings:
                warnings.append("The scan returned no usable sourced candidate.")
        self._emit(
            "business.discovery.scan.completed",
            {
                "stage": "discovery",
                "candidate_count": len(candidates),
                "signal_type": kind.value,
            },
        )
        if self._audit is not None:
            try:
                self._audit(
                    task_id="business:discovery-scan",
                    user_request=f"Discovery scan: {clean_query}",
                    outcome="success",
                    source="business",
                    intent="business.discovery.scan.completed",
                    decision={
                        "candidate_count": len(candidates),
                        "signal_type": kind.value,
                        "external_action_executed": False,
                    },
                )
            except Exception:  # noqa: BLE001
                pass
        return {
            "query": clean_query,
            "signal_type": kind.value,
            "candidates": candidates,
            "warnings": warnings,
            "note": (
                "Candidates are source references, not opportunities. No problem, solution, "
                "market size or customer was generated."
            ),
        }

    # ── research ────────────────────────────────────────────────────────────

    async def research(self, opportunity_id: str, *, actor: str = "") -> dict[str, Any]:
        opportunity = self.get_opportunity(opportunity_id)
        actor = (actor or self._actor).strip() or "operator"
        if opportunity.stage in {
            OpportunityStage.VALIDATED,
            OpportunityStage.NOT_VIABLE,
            OpportunityStage.PAUSED,
            OpportunityStage.REJECTED,
            OpportunityStage.ARCHIVED,
        }:
            raise BusinessConflict(f"Cannot research an opportunity in {opportunity.stage.value}.")
        runs = int(opportunity.metadata.get("research_runs", 0) or 0)
        if runs >= self.max_research_runs:
            raise BusinessConflict(
                f"Research budget reached ({self.max_research_runs} runs); review the evidence before retrying."
            )
        if self._research_callable is None:
            raise ResearchUnavailable("No Explore research provider is configured.")
        async with self._intent(opportunity.id):
            current = self.get_opportunity(opportunity.id)
            # Re-check after acquiring the lock: another operation may have
            # completed while this caller was queued and made the record
            # terminal. The pre-lock check is only a fast rejection.
            if current.stage in {
                OpportunityStage.VALIDATED,
                OpportunityStage.NOT_VIABLE,
                OpportunityStage.PAUSED,
                OpportunityStage.REJECTED,
                OpportunityStage.ARCHIVED,
            }:
                raise BusinessConflict(
                    f"Cannot research an opportunity in {current.stage.value}."
                )
            runs = int(current.metadata.get("research_runs", 0) or 0)
            if runs >= self.max_research_runs:
                raise BusinessConflict(
                    f"Research budget reached ({self.max_research_runs} runs); "
                    "review the evidence before retrying."
                )
            query = self._research_query(current)
            if current.stage is OpportunityStage.DISCOVERED:
                current = transition(
                    current,
                    OpportunityStage.RESEARCHING,
                    reason="research started",
                    actor=actor,
                )
            elif current.stage is OpportunityStage.REQUIRES_FURTHER_RESEARCH:
                current = transition(
                    current,
                    OpportunityStage.EVIDENCE_COLLECTION,
                    reason="research refresh requested after further-research verdict",
                    actor=actor,
                )
            elif current.stage is OpportunityStage.INSUFFICIENT_EVIDENCE:
                current = transition(
                    current,
                    OpportunityStage.EVIDENCE_COLLECTION,
                    reason="additional research requested after insufficient evidence",
                    actor=actor,
                )
            elif current.stage is OpportunityStage.CONDITIONALLY_VALIDATED:
                current = transition(
                    current,
                    OpportunityStage.EVIDENCE_COLLECTION,
                    reason="research requested to close a validation condition",
                    actor=actor,
                )
            current = replace(
                current,
                updated_at=self._clock(),
                metadata={
                    **dict(current.metadata),
                    "research_runs": runs + 1,
                    "last_research_query": query,
                    "last_research_started_at": self._clock().isoformat(),
                },
            )
            self._opportunities[current.id] = current
            self._persist()
            self._emit(
                "business.research.started",
                {
                    "opportunity_id": current.id,
                    "stage": current.stage.value,
                    "research_run": runs + 1,
                },
            )
            self._record_audit(
                action="business.research.started",
                opportunity=current,
                actor=actor,
                detail=f"Explore query: {query}",
            )
            try:
                result = await asyncio.wait_for(
                    self._research_callable(query, self.max_research_sources),
                    timeout=self.research_timeout_seconds,
                )
            except (TimeoutError, asyncio.TimeoutError) as exc:
                return self._research_failure(
                    opportunity.id, actor, f"research exceeded {self.research_timeout_seconds:.0f}s"
                )
            except Exception as exc:  # noqa: BLE001 - an unavailable provider is not a crash
                return self._research_failure(opportunity.id, actor, str(exc))
            items = list(result) if isinstance(result, Sequence) and not isinstance(result, (str, bytes)) else []
            added: list[EvidenceRecord] = []
            warnings: list[str] = []
            existing_urls = {
                row.source.url for row in current.evidence if row.source is not None and row.source.url
            }
            for item in items[: self.max_research_sources]:
                payload = item if isinstance(item, Mapping) else _item_mapping(item)
                url = str(payload.get("url", "")).strip()
                title = str(payload.get("title", "")).strip()
                snippet = str(payload.get("snippet", payload.get("excerpt", ""))).strip()
                if not _valid_http_url(url):
                    warnings.append(f"Skipped a research result without a usable URL: {title or 'untitled'}")
                    continue
                if url in existing_urls:
                    continue
                existing_urls.add(url)
                retrieved = _parse_dt(payload.get("retrieved_at")) or self._clock()
                source = SourceRef(
                    id=new_id("src"),
                    title=title or url,
                    url=url,
                    source_type=str(payload.get("source_type", "web")),
                    publisher=str(payload.get("publisher", "")),
                    retrieved_at=retrieved,
                    excerpt=snippet[:1000],
                    supplied_by="Explore",
                )
                evidence = EvidenceRecord(
                    id=new_id("ev"),
                    kind=EvidenceKind.OTHER,
                    claim=(
                        f"Research reference: {title or url}. {snippet}"
                        if snippet
                        else f"Research reference: {title or url}."
                    )[:1200],
                    quality=EvidenceQuality.SECONDARY,
                    source=source,
                    confidence=0.5,
                    recorded_at=self._clock(),
                    supplied_by="Explore",
                    value={"research_query": query, "unclassified": True},
                    notes=(
                        "Explore supplied this source. It is not yet a market-size, customer "
                        "or competitor claim; classify it explicitly before it can support a verdict."
                    ),
                )
                added.append(evidence)
            if not added and not warnings:
                warnings.append("Research returned no usable sourced results.")
            current = replace(
                current,
                evidence=(*current.evidence, *added),
                source_refs=tuple(
                    {
                        **{row.id: row for row in current.source_refs},
                        **{
                            row.source.id: row.source
                            for row in added
                            if row.source is not None
                        },
                    }.values()
                ),
                confidence=_confidence((*current.evidence, *added)),
                updated_at=self._clock(),
                stage=(
                    OpportunityStage.RESEARCHING
                    if current.stage is OpportunityStage.RESEARCHING
                    else current.stage
                ),
                metadata={
                    **dict(current.metadata),
                    "last_research_at": self._clock().isoformat(),
                    "last_research_error": "",
                    "last_research_added": len(added),
                    "last_research_warnings": warnings,
                },
            )
            if current.stage is OpportunityStage.RESEARCHING:
                current = transition(
                    current,
                    OpportunityStage.EVIDENCE_COLLECTION,
                    reason=(
                        f"research added {len(added)} sourced reference(s)"
                        if added
                        else "research completed with no usable sources"
                    ),
                    actor=actor,
                )
            current = self._invalidate_analysis(current)
            self._opportunities[current.id] = current
            self._persist()
            self._emit(
                "business.research.completed",
                {
                    "opportunity_id": current.id,
                    "stage": current.stage.value,
                    "sources_added": len(added),
                    "evidence_count": len(current.evidence),
                },
            )
            self._record_audit(
                action="business.research.completed",
                opportunity=current,
                actor=actor,
                detail=f"Added {len(added)} sourced research reference(s); {len(warnings)} warning(s)",
            )
            return {
                "opportunity": self.detail(opportunity.id),
                "provider": "explore",
                "query": query,
                "sources_added": len(added),
                "warnings": warnings,
                "note": (
                    "Research references are unclassified secondary evidence until an operator "
                    "records a specific market, customer or competitor claim."
                ),
            }

    def _research_failure(self, opportunity_id: str, actor: str, detail: str) -> dict[str, Any]:
        current = self.get_opportunity(opportunity_id)
        current = replace(
            current,
            updated_at=self._clock(),
            metadata={
                **dict(current.metadata),
                "last_research_at": self._clock().isoformat(),
                "last_research_error": detail[:500],
            },
        )
        self._opportunities[current.id] = current
        self._persist()
        self._emit(
            "business.research.failed",
            {
                "opportunity_id": current.id,
                "stage": current.stage.value,
                "status": "unavailable",
            },
        )
        self._record_audit(
            action="business.research.failed",
            opportunity=current,
            actor=actor,
            detail=detail[:500],
        )
        return {
            "opportunity": self.detail(opportunity_id),
            "provider": "explore",
            "sources_added": 0,
            "warnings": [f"Research provider unavailable: {detail[:300]}"],
            "note": "No evidence was created from the failed run. The opportunity can be retried.",
        }

    def _research_query(self, opportunity: Opportunity) -> str:
        return (
            f"{opportunity.title}: market demand, customer problem, target audience, "
            f"competitors and pricing for {opportunity.target_audience or 'the target customer'} "
            f"in {opportunity.industry or opportunity.category or 'the relevant market'}. "
            f"Problem: {opportunity.customer_problem}. Proposed solution: {opportunity.proposed_solution}."
        )[:1200]

    # ── assessment and scoring ──────────────────────────────────────────────

    async def assess(
        self,
        opportunity_id: str,
        *,
        financial_inputs: Mapping[str, Any] | None = None,
        feasibility_inputs: Mapping[str, Any] | None = None,
        assessment_inputs: Mapping[str, Any] | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        opportunity = self.get_opportunity(opportunity_id)
        actor = (actor or self._actor).strip() or "operator"
        self._assert_assessable(opportunity)
        async with self._intent(opportunity.id):
            current = self.get_opportunity(opportunity.id)
            # A queued assessment must not overwrite a record that became
            # terminal while it waited for the lock.
            self._assert_assessable(current)
            current = self._remember_report_version(current)
            current = self._advance_for_assessment(current, actor)
            current, financials, feasibility, score, market, customer, competition = self._analyse(
                current,
                financial_inputs=financial_inputs,
                feasibility_inputs=feasibility_inputs,
                assessment_inputs=assessment_inputs,
            )
            gaps = evidence_gaps(
                opportunity=current,
                score=score,
                feasibility=feasibility,
                financials=financials,
            )
            current = replace(
                current,
                market=market,
                customer=customer,
                competition=competition,
                financials=financials,
                feasibility=feasibility,
                score=score,
                confidence=_confidence(current.evidence),
                uncertainty=tuple(gaps[:10]),
                updated_at=self._clock(),
                verdict="",
                verdict_rationale="",
                report=None,
            )
            self._opportunities[current.id] = current
            self._persist()
            self._emit(
                "business.assessment.completed",
                {
                    "opportunity_id": current.id,
                    "stage": current.stage.value,
                    "score": round(score.weighted_score, 4),
                    "coverage": round(score.evidence_coverage, 4),
                    "evidence_count": len(current.evidence),
                },
            )
            self._emit(
                "business.score.calculated",
                {
                    "opportunity_id": current.id,
                    "stage": current.stage.value,
                    "score": round(score.weighted_score, 4),
                    "eligible": score.eligible,
                },
            )
            self._record_audit(
                action="business.assessment.completed",
                opportunity=current,
                actor=actor,
                detail=(
                    f"Score {score.weighted_score:.2f}, coverage {score.evidence_coverage:.0%}, "
                    f"eligible={score.eligible}"
                ),
            )
            return self.detail(current.id)

    def _analyse(
        self,
        opportunity: Opportunity,
        *,
        financial_inputs: Mapping[str, Any] | None = None,
        feasibility_inputs: Mapping[str, Any] | None = None,
        assessment_inputs: Mapping[str, Any] | None = None,
    ) -> tuple[Opportunity, FinancialAnalysis, Any, Any, Any, Any, Any]:
        metadata = dict(opportunity.metadata)
        financial_merged: dict[str, Any] = {}
        if isinstance(metadata.get("financial_inputs"), Mapping):
            financial_merged.update(metadata["financial_inputs"])
        if financial_inputs is not None:
            financial_merged.update(
                {
                    key: value
                    for key, value in financial_inputs.items()
                    if value is not None and value != ""
                }
            )
        financial_payload: Mapping[str, Any] | None = financial_merged or None
        feasibility_payload: dict[str, Any] = {}
        if isinstance(metadata.get("feasibility_inputs"), Mapping):
            feasibility_payload.update(metadata["feasibility_inputs"])
        if feasibility_inputs is not None:
            feasibility_payload.update(feasibility_inputs)
        score_payload: dict[str, Any] = {}
        if isinstance(metadata.get("assessment_inputs"), Mapping):
            score_payload.update(metadata["assessment_inputs"])
        if assessment_inputs is not None:
            score_payload.update(assessment_inputs)
        financials = (
            analyse_financials(financial_payload)
            if financial_payload is not None
            else opportunity.financials or analyse_financials(None)
        )
        market = build_market_research(opportunity.evidence)
        customer = build_customer_validation(opportunity.evidence, opportunity.target_audience)
        competition = build_competition(opportunity.evidence, opportunity.title)
        snapshot = self._read_resources()
        feasibility = assess_feasibility(
            evidence=opportunity.evidence,
            market=market,
            customer=customer,
            financials=financials,
            resource_snapshot=snapshot,
            inputs=feasibility_payload,
        )
        score = score_opportunity(
            evidence=opportunity.evidence,
            market=market,
            customer=customer,
            competition=competition,
            feasibility=feasibility,
            financials=financials,
            target_audience=opportunity.target_audience,
            inputs=score_payload,
            weights=self._weights,
            threshold=self.threshold,
        )
        updated = replace(
            opportunity,
            metadata={
                **metadata,
                **({"financial_inputs": dict(financial_payload)} if financial_payload is not None else {}),
                "feasibility_inputs": feasibility_payload,
                "assessment_inputs": score_payload,
            },
        )
        return updated, financials, feasibility, score, market, customer, competition

    def _advance_for_assessment(self, opportunity: Opportunity, actor: str) -> Opportunity:
        paths = {
            OpportunityStage.DISCOVERED: (
                OpportunityStage.EVIDENCE_COLLECTION,
                OpportunityStage.FEASIBILITY_ASSESSMENT,
            ),
            OpportunityStage.RESEARCHING: (
                OpportunityStage.EVIDENCE_COLLECTION,
                OpportunityStage.FEASIBILITY_ASSESSMENT,
            ),
            OpportunityStage.EVIDENCE_COLLECTION: (OpportunityStage.FEASIBILITY_ASSESSMENT,),
            OpportunityStage.REQUIRES_FURTHER_RESEARCH: (
                OpportunityStage.EVIDENCE_COLLECTION,
                OpportunityStage.FEASIBILITY_ASSESSMENT,
            ),
            OpportunityStage.INSUFFICIENT_EVIDENCE: (
                OpportunityStage.EVIDENCE_COLLECTION,
                OpportunityStage.FEASIBILITY_ASSESSMENT,
            ),
            OpportunityStage.CONDITIONALLY_VALIDATED: (
                OpportunityStage.EVIDENCE_COLLECTION,
                OpportunityStage.FEASIBILITY_ASSESSMENT,
            ),
        }
        return self._walk(opportunity, paths.get(opportunity.stage, ()), actor, "assessment")

    # ── experiment planning and evaluation ──────────────────────────────────

    async def plan_experiments(self, opportunity_id: str, *, actor: str = "") -> dict[str, Any]:
        opportunity = self.get_opportunity(opportunity_id)
        actor = (actor or self._actor).strip() or "operator"
        self._assert_assessable(opportunity)
        async with self._intent(opportunity.id):
            current = self.get_opportunity(opportunity.id)
            self._assert_assessable(current)
            current = self._advance_for_assessment(current, actor)
            current, financials, feasibility, score, market, customer, competition = self._analyse(current)
            gaps = evidence_gaps(
                opportunity=current, score=score, feasibility=feasibility, financials=financials
            )
            current = replace(
                current,
                market=market,
                customer=customer,
                competition=competition,
                financials=financials,
                feasibility=feasibility,
                score=score,
                experiments=_merge_experiments(
                    current.experiments,
                    plan_experiments(
                        opportunity=current,
                        gaps=gaps,
                        currency=financials.currency,
                    ),
                ),
                confidence=_confidence(current.evidence),
                uncertainty=tuple(gaps[:10]),
                updated_at=self._clock(),
            )
            if current.stage in {
                OpportunityStage.EVIDENCE_COLLECTION,
                OpportunityStage.FEASIBILITY_ASSESSMENT,
                OpportunityStage.REQUIRES_FURTHER_RESEARCH,
                OpportunityStage.INSUFFICIENT_EVIDENCE,
                OpportunityStage.CONDITIONALLY_VALIDATED,
            }:
                current = self._walk(
                    current,
                    {
                        OpportunityStage.EVIDENCE_COLLECTION: (
                            OpportunityStage.FEASIBILITY_ASSESSMENT,
                            OpportunityStage.EXPERIMENT_PLANNING,
                        ),
                        OpportunityStage.FEASIBILITY_ASSESSMENT: (
                            OpportunityStage.EXPERIMENT_PLANNING,
                        ),
                        OpportunityStage.REQUIRES_FURTHER_RESEARCH: (
                            OpportunityStage.EXPERIMENT_PLANNING,
                        ),
                        OpportunityStage.INSUFFICIENT_EVIDENCE: (
                            OpportunityStage.EVIDENCE_COLLECTION,
                            OpportunityStage.FEASIBILITY_ASSESSMENT,
                            OpportunityStage.EXPERIMENT_PLANNING,
                        ),
                        OpportunityStage.CONDITIONALLY_VALIDATED: (
                            OpportunityStage.EVIDENCE_COLLECTION,
                            OpportunityStage.FEASIBILITY_ASSESSMENT,
                            OpportunityStage.EXPERIMENT_PLANNING,
                        ),
                    }.get(current.stage, ()),
                    actor,
                    "experiment planning",
                )
            self._opportunities[current.id] = current
            self._persist()
            self._emit(
                "business.experiment.proposed",
                {
                    "opportunity_id": current.id,
                    "stage": current.stage.value,
                    "experiment_count": len(current.experiments),
                },
            )
            self._record_audit(
                action="business.experiment.proposed",
                opportunity=current,
                actor=actor,
                detail=f"Planned/refreshed {len(current.experiments)} validation experiment(s)",
            )
            return self.detail(current.id)

    async def evaluate(self, opportunity_id: str, *, actor: str = "") -> dict[str, Any]:
        opportunity = self.get_opportunity(opportunity_id)
        actor = (actor or self._actor).strip() or "operator"
        if opportunity.stage in {
            OpportunityStage.VALIDATED,
            OpportunityStage.NOT_VIABLE,
            OpportunityStage.PAUSED,
            OpportunityStage.REJECTED,
            OpportunityStage.ARCHIVED,
        }:
            raise BusinessConflict(
                f"Opportunity in {opportunity.stage.value} cannot be re-evaluated; "
                "a terminal record is immutable. Archive it or create a new record."
            )
        # VALIDATION is the final pre-verdict stage and may exist after an
        # interrupted run; evaluate must be able to finish it. The mutable guard
        # still refuses paused and decided records without reopening them.
        self._assert_mutable(opportunity, "evaluate")
        async with self._intent(opportunity.id):
            current = self.get_opportunity(opportunity.id)
            # Re-check after acquiring the lock: a queued evaluator can arrive
            # after another evaluation has already made the record terminal.
            self._assert_mutable(current, "evaluate")
            current = self._remember_report_version(current)
            current = self._advance_for_assessment(current, actor)
            current, financials, feasibility, score, market, customer, competition = self._analyse(current)
            gaps = evidence_gaps(
                opportunity=current, score=score, feasibility=feasibility, financials=financials
            )
            experiments = _merge_experiments(
                current.experiments,
                plan_experiments(
                    opportunity=replace(
                        current,
                        market=market,
                        customer=customer,
                        competition=competition,
                        financials=financials,
                        feasibility=feasibility,
                    ),
                    gaps=gaps,
                    currency=financials.currency,
                ),
            )
            draft = replace(
                current,
                market=market,
                customer=customer,
                competition=competition,
                financials=financials,
                feasibility=feasibility,
                score=score,
                experiments=experiments,
                confidence=_confidence(current.evidence),
                uncertainty=tuple(gaps[:10]),
                updated_at=self._clock(),
            )
            verdict, rationale, gaps = decide_verdict(
                opportunity=draft,
                score=score,
                feasibility=feasibility,
                financials=financials,
            )
            next_steps = next_steps_for(
                opportunity=draft, gaps=gaps, financials=financials
            )
            version = max(
                int(current.metadata.get("report_version", 0) or 0),
                current.report.version if current.report else 0,
            ) + 1
            report = build_report(
                opportunity=draft,
                market=market,
                customer=customer,
                competition=competition,
                feasibility=feasibility,
                financials=financials,
                score=score,
                experiments=experiments,
                verdict=verdict,
                rationale=rationale,
                gaps=gaps,
                next_steps=next_steps,
                version=version,
            )
            draft = replace(
                draft,
                verdict=verdict,
                verdict_rationale=rationale,
                report=report,
                metadata={
                    **dict(draft.metadata),
                    "report_version": version,
                },
                updated_at=self._clock(),
            )
            draft = self._walk_to_validation(draft, actor)
            target = OpportunityStage(verdict.lower())
            draft = transition(
                draft,
                target,
                reason=f"validation verdict derived from recorded evidence: {verdict}",
                actor=actor,
            )
            self._opportunities[draft.id] = draft
            self._persist()
            self._emit(
                "business.report.generated",
                {
                    "opportunity_id": draft.id,
                    "stage": draft.stage.value,
                    "verdict": verdict,
                    "version": version,
                },
            )
            self._emit(
                "business.validation.completed",
                {
                    "opportunity_id": draft.id,
                    "stage": draft.stage.value,
                    "verdict": verdict,
                    "score": round(score.weighted_score, 4),
                },
            )
            self._record_audit(
                action="business.validation.completed",
                opportunity=draft,
                actor=actor,
                detail=f"Verdict {verdict}: {rationale}",
            )
            warning = await self._ingest_report(draft)
            if warning:
                draft = replace(
                    draft,
                    metadata={
                        **dict(draft.metadata),
                        "knowledge_ingest_error": warning,
                    },
                )
                self._opportunities[draft.id] = draft
                self._persist()
            return self.detail(draft.id)

    async def _ingest_report(self, opportunity: Opportunity) -> str:
        if self._knowledge is None or opportunity.report is None:
            return ""
        report = opportunity.report
        body = "\n\n".join(
            f"{section.title}\n{section.body}" for section in report.sections
        )
        text = (
            f"Business validation report: {opportunity.title}\n"
            f"Opportunity ID: {opportunity.id}\nVerdict: {report.verdict}\n"
            f"Evidence sources: {report.source_count}; primary records: {report.primary_count}\n\n"
            f"{body}\n\nEvidence gaps:\n- "
            + "\n- ".join(report.evidence_gaps)
        )
        try:
            result = self._knowledge(text, f"Business validation: {opportunity.title}")
            if inspect.isawaitable(result):
                await result
            return ""
        except Exception as exc:  # noqa: BLE001 - knowledge ingestion is auxiliary
            return str(exc)[:500]

    def _walk_to_validation(self, opportunity: Opportunity, actor: str) -> Opportunity:
        paths = {
            OpportunityStage.DISCOVERED: (
                OpportunityStage.EVIDENCE_COLLECTION,
                OpportunityStage.FEASIBILITY_ASSESSMENT,
                OpportunityStage.EXPERIMENT_PLANNING,
                OpportunityStage.VALIDATION,
            ),
            OpportunityStage.RESEARCHING: (
                OpportunityStage.EVIDENCE_COLLECTION,
                OpportunityStage.FEASIBILITY_ASSESSMENT,
                OpportunityStage.EXPERIMENT_PLANNING,
                OpportunityStage.VALIDATION,
            ),
            OpportunityStage.EVIDENCE_COLLECTION: (
                OpportunityStage.FEASIBILITY_ASSESSMENT,
                OpportunityStage.EXPERIMENT_PLANNING,
                OpportunityStage.VALIDATION,
            ),
            OpportunityStage.FEASIBILITY_ASSESSMENT: (
                OpportunityStage.EXPERIMENT_PLANNING,
                OpportunityStage.VALIDATION,
            ),
            OpportunityStage.EXPERIMENT_PLANNING: (OpportunityStage.VALIDATION,),
            OpportunityStage.REQUIRES_FURTHER_RESEARCH: (OpportunityStage.VALIDATION,),
            OpportunityStage.INSUFFICIENT_EVIDENCE: (
                OpportunityStage.EVIDENCE_COLLECTION,
                OpportunityStage.FEASIBILITY_ASSESSMENT,
                OpportunityStage.EXPERIMENT_PLANNING,
                OpportunityStage.VALIDATION,
            ),
            OpportunityStage.CONDITIONALLY_VALIDATED: (OpportunityStage.VALIDATION,),
        }
        return self._walk(
            opportunity,
            paths.get(opportunity.stage, ()),
            actor,
            "evidence-backed validation",
        )

    # ── experiment approval and outcomes ────────────────────────────────────

    def approve_experiment(
        self,
        opportunity_id: str,
        experiment_id: str,
        *,
        approved: bool,
        note: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        opportunity = self.get_opportunity(opportunity_id)
        actor = (actor or self._actor).strip() or "operator"
        self._assert_mutable(opportunity, "change an experiment approval")
        index = next(
            (i for i, row in enumerate(opportunity.experiments) if row.id == experiment_id),
            None,
        )
        if index is None:
            raise BusinessError(f"Experiment {experiment_id!r} was not found.")
        experiment = opportunity.experiments[index]
        updated_experiment = replace(
            experiment,
            status="approved" if approved else "rejected",
            approved_by=actor,
        )
        rows = list(opportunity.experiments)
        rows[index] = updated_experiment
        updated = replace(
            opportunity, experiments=tuple(rows), updated_at=self._clock()
        )
        self._opportunities[updated.id] = updated
        self._persist()
        event = (
            "business.experiment.approval.granted"
            if approved
            else "business.experiment.approval.denied"
        )
        self._emit(
            event,
            {
                "opportunity_id": updated.id,
                "stage": updated.stage.value,
                "experiment_id": experiment.id,
                "approval_status": updated_experiment.status,
            },
        )
        self._record_audit(
            action=event,
            opportunity=updated,
            actor=actor,
            detail=f"{updated_experiment.status.title()} experiment {experiment.title}. {note}".strip(),
        )
        return self.detail(updated.id)

    def record_experiment_outcome(
        self,
        opportunity_id: str,
        experiment_id: str,
        *,
        outcome: str,
        result: Mapping[str, Any] | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        opportunity = self.get_opportunity(opportunity_id)
        actor = (actor or self._actor).strip() or "operator"
        self._assert_mutable(opportunity, "record an experiment outcome")
        index = next(
            (i for i, row in enumerate(opportunity.experiments) if row.id == experiment_id),
            None,
        )
        if index is None:
            raise BusinessError(f"Experiment {experiment_id!r} was not found.")
        experiment = opportunity.experiments[index]
        if experiment.requires_approval and experiment.status != "approved":
            raise BusinessConflict(
                "This experiment requires explicit operator approval before its outcome can be recorded."
            )
        if not outcome.strip():
            raise BusinessError("An outcome is required.")
        updated_experiment = replace(
            experiment,
            status="completed" if outcome.lower() in {"success", "succeeded", "completed", "passed"} else "failed",
            outcome=outcome.strip(),
            result=dict(result or {}),
        )
        rows = list(opportunity.experiments)
        rows[index] = updated_experiment
        updated = replace(opportunity, experiments=tuple(rows), updated_at=self._clock())
        self._opportunities[updated.id] = updated
        self._persist()
        self._emit(
            "business.experiment.outcome.recorded",
            {
                "opportunity_id": updated.id,
                "stage": updated.stage.value,
                "experiment_id": experiment.id,
                "outcome": updated_experiment.status,
            },
        )
        self._record_audit(
            action="business.experiment.outcome.recorded",
            opportunity=updated,
            actor=actor,
            detail=f"Experiment {experiment.title}: {outcome.strip()}",
        )
        return self.detail(updated.id)

    # ── lifecycle controls ──────────────────────────────────────────────────

    def pause(self, opportunity_id: str, *, reason: str, actor: str = "") -> dict[str, Any]:
        opportunity = self.get_opportunity(opportunity_id)
        self._assert_mutable(opportunity, "pause")
        return self._lifecycle(opportunity_id, OpportunityStage.PAUSED, reason, actor)

    def resume(self, opportunity_id: str, *, reason: str = "", actor: str = "") -> dict[str, Any]:
        opportunity = self.get_opportunity(opportunity_id)
        if opportunity.stage is not OpportunityStage.PAUSED:
            raise BusinessConflict("Only a paused opportunity can be resumed.")
        previous = str(opportunity.metadata.get("paused_from", ""))
        if not previous:
            raise BusinessConflict("Paused opportunity has no remembered stage to resume.")
        try:
            target = OpportunityStage(previous)
        except ValueError as exc:
            raise BusinessConflict("Paused opportunity has an invalid remembered stage.") from exc
        return self._lifecycle(
            opportunity_id, target, reason or "resumed by operator", actor
        )

    def reject(self, opportunity_id: str, *, reason: str, actor: str = "") -> dict[str, Any]:
        if not reason.strip():
            raise BusinessError("Rejecting an opportunity requires a reason.")
        return self._lifecycle(opportunity_id, OpportunityStage.REJECTED, reason, actor)

    def archive(self, opportunity_id: str, *, reason: str = "", actor: str = "") -> dict[str, Any]:
        return self._lifecycle(
            opportunity_id, OpportunityStage.ARCHIVED, reason or "archived by operator", actor
        )

    def _lifecycle(
        self, opportunity_id: str, target: OpportunityStage, reason: str, actor: str
    ) -> dict[str, Any]:
        opportunity = self.get_opportunity(opportunity_id)
        actor = (actor or self._actor).strip() or "operator"
        try:
            updated = transition(opportunity, target, reason=reason, actor=actor)
        except ValueError as exc:
            raise BusinessConflict(str(exc)) from exc
        self._opportunities[updated.id] = updated
        self._persist()
        self._emit(
            "business.lifecycle.changed",
            {
                "opportunity_id": updated.id,
                "stage": updated.stage.value,
                "previous_stage": opportunity.stage.value,
            },
        )
        self._record_audit(
            action="business.lifecycle.changed",
            opportunity=updated,
            actor=actor,
            detail=f"{opportunity.stage.value} -> {updated.stage.value}: {reason}",
        )
        return self.detail(updated.id)

    # ── helpers ─────────────────────────────────────────────────────────────

    def _read_resources(self) -> dict[str, Any]:
        """A failed/unavailable hardware probe must not fail an assessment."""
        if self._resource_snapshot is None:
            return {}
        try:
            value = self._resource_snapshot()
        except Exception:  # noqa: BLE001 - resource telemetry is advisory
            return {}
        return dict(value) if isinstance(value, Mapping) else {}

    def _remember_report_version(self, opportunity: Opportunity) -> Opportunity:
        """Keep the report version across an invalidation/assessment."""
        metadata = dict(opportunity.metadata)
        previous = max(
            int(metadata.get("report_version", 0) or 0),
            opportunity.report.version if opportunity.report else 0,
        )
        if previous:
            metadata["report_version"] = previous
        return replace(opportunity, metadata=metadata)

    def _invalidate_analysis(self, opportunity: Opportunity) -> Opportunity:
        """Clear snapshots derived from evidence that has just changed."""
        current = self._remember_report_version(opportunity)
        return replace(
            current,
            market=None,
            customer=None,
            competition=None,
            feasibility=None,
            financials=None,
            score=None,
            experiments=current.experiments,
            report=None,
            verdict="",
            verdict_rationale="",
            uncertainty=(),
            updated_at=self._clock(),
        )

    def _assert_mutable(self, opportunity: Opportunity, action: str) -> None:
        if opportunity.stage in {
            OpportunityStage.PAUSED,
            OpportunityStage.REJECTED,
            OpportunityStage.ARCHIVED,
            OpportunityStage.NOT_VIABLE,
        }:
            raise BusinessConflict(f"Cannot {action} for an opportunity in {opportunity.stage.value}.")
        if opportunity.stage is OpportunityStage.VALIDATED:
            raise BusinessConflict(
                f"Cannot {action} for a VALIDATED opportunity; its verdict is a recorded decision."
            )

    def _assert_assessable(self, opportunity: Opportunity) -> None:
        if opportunity.stage in {
            OpportunityStage.PAUSED,
            OpportunityStage.REJECTED,
            OpportunityStage.ARCHIVED,
            OpportunityStage.VALIDATED,
            OpportunityStage.NOT_VIABLE,
            OpportunityStage.VALIDATION,
        }:
            raise BusinessConflict(
                f"Cannot assess an opportunity in {opportunity.stage.value}."
            )

    def _advance(self, opportunity: Opportunity, target: OpportunityStage, actor: str, reason: str) -> Opportunity:
        try:
            updated = transition(opportunity, target, reason=reason, actor=actor)
        except ValueError as exc:
            raise BusinessConflict(str(exc)) from exc
        self._emit(
            "business.lifecycle.changed",
            {
                "opportunity_id": updated.id,
                "stage": updated.stage.value,
                "previous_stage": opportunity.stage.value,
            },
        )
        self._record_audit(
            action="business.lifecycle.changed",
            opportunity=updated,
            actor=actor,
            detail=f"{opportunity.stage.value} -> {updated.stage.value}: {reason}",
        )
        return updated

    def _walk(
        self,
        opportunity: Opportunity,
        path: Sequence[OpportunityStage],
        actor: str,
        reason: str,
    ) -> Opportunity:
        current = opportunity
        for target in path:
            if current.stage is target:
                continue
            current = self._advance(current, target, actor, reason)
        return current

    def _intent(self, opportunity_id: str) -> "_Intent":
        return _Intent(self, opportunity_id)

    def _item_mapping(self, item: Any) -> Mapping[str, Any]:
        return _item_mapping(item)

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self._publish_callback is None:
            return
        try:
            self._publish_callback(event_type, dict(payload))
        except Exception:  # noqa: BLE001 - event delivery is not the business transaction
            pass

    def _record_audit(
        self,
        *,
        action: str,
        opportunity: Opportunity,
        actor: str,
        detail: str,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit(
                task_id=f"business:{opportunity.id}",
                user_request=detail,
                outcome="success",
                source="business",
                intent=action,
                decision={
                    "opportunity_id": opportunity.id,
                    "stage": opportunity.stage.value,
                    "verdict": opportunity.verdict,
                    "evidence_count": len(opportunity.evidence),
                },
                actions=(action,),
            )
        except Exception:  # noqa: BLE001 - the audit trail is auxiliary to the operation
            pass


class _Intent:
    """Reject duplicate concurrent runs and serialise one opportunity's work."""

    def __init__(self, service: BusinessOpportunityService, opportunity_id: str) -> None:
        self.service = service
        self.opportunity_id = opportunity_id
        self.lock: asyncio.Lock | None = None

    async def __aenter__(self) -> None:
        service = self.service
        async with service._intent_guard:
            if self.opportunity_id in service._active:
                raise BusinessConflict(
                    f"A {self.opportunity_id!r} operation is already running; duplicate concurrent work was refused."
                )
            service._active.add(self.opportunity_id)
            self.lock = service._locks.setdefault(self.opportunity_id, asyncio.Lock())
        try:
            await self.lock.acquire()
        except BaseException:
            async with service._intent_guard:
                service._active.discard(self.opportunity_id)
            raise

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        assert self.lock is not None
        self.lock.release()
        async with self.service._intent_guard:
            self.service._active.discard(self.opportunity_id)


def _item_mapping(item: Any) -> Mapping[str, Any]:
    if isinstance(item, Mapping):
        return item
    data: dict[str, Any] = {}
    for key in ("title", "url", "snippet", "excerpt", "source_type", "publisher", "retrieved_at"):
        value = getattr(item, key, None)
        if value:
            data[key] = value
    return data


def _base_value(financials: FinancialAnalysis | None) -> float | None:
    if financials is None:
        return None
    base = next((row for row in financials.scenarios if row.name == "base"), None)
    return None if base is None or not base.calculated else base.monthly_net_profit


def _transitions_from(opportunity: Opportunity) -> set[OpportunityStage]:
    from novacontrol.business.models import ALLOWED_TRANSITIONS

    if opportunity.stage is OpportunityStage.PAUSED:
        previous = str(opportunity.metadata.get("paused_from", ""))
        try:
            remembered = OpportunityStage(previous)
        except ValueError:
            remembered = None
        return {
            row
            for row in {remembered, OpportunityStage.REJECTED, OpportunityStage.ARCHIVED}
            if row is not None
        }
    return set(ALLOWED_TRANSITIONS.get(opportunity.stage, frozenset()))


__all__ = [
    "BusinessConflict",
    "BusinessError",
    "BusinessOpportunityService",
    "DuplicateOpportunity",
    "NAMESPACE",
    "OpportunityNotFound",
    "ResearchUnavailable",
]
