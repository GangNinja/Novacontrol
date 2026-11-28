"""The continual learning engine — capture, evaluate, retrieve, improve.

This is the phase's one new component. It does not plan, execute, verify or
remember conversations; it observes work those existing layers already did,
turns it into assessed experience, and answers one question later: *what does
previous evidence suggest for this task?*

Boundaries that are structural rather than advisory:

* **Nothing is considered learned because it was stored.** A record is evidence,
  not knowledge; a strategy becomes ACTIVE only after validation over judged
  trials, and every recommendation carries the ids of the evidence behind it.
* **Unverified work never supports a promotion.** ``OutcomeEvaluator`` classifies
  a run with no independent confirmation as ``UNVERIFIED``, and the strategy book
  refuses to validate on those.
* **A failed synthesis never breaks the work that produced it.** Every capture,
  record, feedback and recommendation path catches its own exceptions and
  reports them in ``status()``; the application calls this engine AFTER shaping a
  response, so a learning defect can cost learning, never the request.
* **Learning is optional.** ``recommend`` returns a source of ``none`` when it
  has nothing comparable, and callers must treat that as "no suggestion".

The engine is synchronous and bounded: at most ``max_experiences`` records are
kept, saves are debounced, and no network or model is touched on any path.
"""

from __future__ import annotations

import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from novacontrol.continual.evaluate import OutcomeEvaluator
from novacontrol.continual.models import (
    ExecutionEvidence,
    LearningProposal,
    LearningSignal,
    OutcomeClass,
    ProposalStatus,
    Recommendation,
    StepEvidence,
    StrategyRecord,
    StrategyStatus,
    clamp01,
    fingerprint_for,
    new_id,
    normalize_goal,
    now_iso,
)
from novacontrol.continual.retrieval import ExperienceRetriever
from novacontrol.continual.signals import SignalExtractor
from novacontrol.continual.store import (
    ContinualLearningStore,
    InMemoryContinualLearningStore,
    LearningState,
)
from novacontrol.continual.strategies import PROMOTION_FLOOR, StrategyBook, StrategyStats

__all__ = ["ContinualLearningEngine", "DEFAULT_MAX_EXPERIENCES"]

DEFAULT_MAX_EXPERIENCES = 500

#: Feedback verdicts. ``negative`` is the user saying the work was wrong; the
#: record becomes USER_CORRECTED and its strategy loses confidence. ``positive``
#: is the user CONFIRMING the work, which is legitimate verification evidence.
_NEGATIVE_VERDICTS = frozenset({"negative", "wrong", "rejected", "incorrect", "no"})
_POSITIVE_VERDICTS = frozenset({"positive", "ok", "accepted", "correct", "yes", "worked"})


class ContinualLearningEngine:
    """Bounded, auditable learning over executed evidence."""

    def __init__(
        self,
        *,
        store: ContinualLearningStore | None = None,
        evaluator: OutcomeEvaluator | None = None,
        retriever: ExperienceRetriever | None = None,
        extractor: SignalExtractor | None = None,
        book: StrategyBook | None = None,
        enabled: bool = True,
        persist: bool = True,
        max_experiences: int = DEFAULT_MAX_EXPERIENCES,
        min_support: int = 2,
        min_confidence: float = 0.6,
        auto_learn: bool = True,
        auto_promote: bool = True,
        auto_rollback: bool = True,
        retention_days: int = 180,
        auto_save_seconds: float = 2.0,
        observer: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.persist = bool(persist)
        self.store = store if store is not None else InMemoryContinualLearningStore()
        self.evaluator = evaluator or OutcomeEvaluator()
        self.retriever = retriever or ExperienceRetriever()
        self.extractor = extractor or SignalExtractor()
        self.book = book or StrategyBook(min_support=min_support)
        self.max_experiences = max(16, int(max_experiences))
        self.min_support = max(1, int(min_support))
        self.min_confidence = max(0.0, min(1.0, float(min_confidence)))
        self.auto_learn = bool(auto_learn)
        self.auto_promote = bool(auto_promote)
        self.auto_rollback = bool(auto_rollback)
        self.retention_days = max(0, int(retention_days))
        self._auto_save_seconds = max(0.0, float(auto_save_seconds))
        self._observer = observer
        self._clock = clock or now_iso
        self._lock = threading.RLock()
        self._state = LearningState()
        self._dirty = False
        self._last_saved_monotonic = 0.0
        self._last_saved_at = ""
        self._restore_report: dict[str, Any] = {}
        self._last_recommendation: dict[str, Any] = {}
        self._errors: list[str] = []

    # -- lifecycle --------------------------------------------------------

    def restore(self) -> dict[str, Any]:
        """Load the corpus from the store. Never raises; reports what it read."""
        with self._lock:
            try:
                payload = self.store.load()
            except Exception as exc:  # pragma: no cover - a store like that is broken
                payload = {}
                self._note_error(f"restore: {type(exc).__name__}")
            state = LearningState.from_payload(payload)
            self._state = state
            self._recompute_stats()
            self._refresh_signals()
            self._restore_report = {
                **state.report(),
                "restored": bool(payload),
                "restored_at": now_iso(),
                # Nothing here can resume an in-flight operation: learning is
                # synchronous, so a half-written strategy would be a proposal
                # without its validation — exactly what a restart leaves.
                "interrupted_proposals": sum(
                    1
                    for proposal in state.proposals.values()
                    if proposal.status in {ProposalStatus.PROPOSED.value, ProposalStatus.VALIDATED.value}
                ),
            }
            return dict(self._restore_report)

    def flush(self) -> dict[str, Any]:
        """Persist now. Called on application shutdown and after explicit changes."""
        with self._lock:
            return self._save(force=True)

    def _save(self, *, force: bool = False) -> dict[str, Any]:
        if not self.persist:
            return {"saved": False, "reason": "persistence disabled"}
        if not force and not self._dirty:
            return {"saved": False, "reason": "no changes"}
        if not force and self._auto_save_seconds > 0:
            elapsed = time.monotonic() - self._last_saved_monotonic
            if elapsed < self._auto_save_seconds:
                return {"saved": False, "reason": "debounced", "pending": True}
        try:
            self.store.save(self._state.to_payload())
        except Exception as exc:  # pragma: no cover - the store is already forgiving
            self._note_error(f"save: {type(exc).__name__}")
            return {"saved": False, "reason": f"store refused the snapshot: {type(exc).__name__}"}
        self._dirty = False
        self._last_saved_monotonic = time.monotonic()
        self._last_saved_at = now_iso()
        return {"saved": True, "at": self._last_saved_at}

    # -- capture ----------------------------------------------------------

    def capture(
        self,
        goal: str,
        *,
        task_kind: str = "general",
        source: str = "request",
        route: str = "",
        intent: str = "",
        context: Mapping[str, Any] | None = None,
        steps: Sequence[StepEvidence | Mapping[str, Any]] = (),
        tools: Sequence[str] = (),
        verification: Mapping[str, Any] | None = None,
        errors: Sequence[str] = (),
        recoveries: Sequence[str] = (),
        latency_ms: float = 0.0,
        retries: int = 0,
        attempts: int = 1,
        feedback: str = "",
        verdict: str = "",
        correlation_id: str = "",
        strategy_id: str = "",
        strategy_version: int = 0,
        started_at: str = "",
        finished_at: str = "",
    ) -> ExecutionEvidence:
        """Build a record from evidence a caller already observed."""
        normalized_steps: list[StepEvidence] = []
        for index, step in enumerate(steps):
            if isinstance(step, StepEvidence):
                normalized_steps.append(step)
            elif isinstance(step, Mapping):
                parsed = StepEvidence.from_dict({**step, "step_id": step.get("step_id") or f"step-{index + 1}"})
                if parsed is not None:
                    normalized_steps.append(parsed)
        return ExecutionEvidence(
            experience_id=new_id("exp"),
            goal=str(goal or "").strip(),
            task_kind=str(task_kind or "general").strip().lower() or "general",
            source=str(source or "request"),
            route=str(route or ""),
            intent=str(intent or ""),
            strategy_id=str(strategy_id or ""),
            strategy_version=max(0, int(strategy_version)),
            context={str(key): value for key, value in (context or {}).items()},
            steps=normalized_steps,
            tools=[str(tool) for tool in tools],
            errors=[str(item) for item in errors],
            recoveries=[str(item) for item in recoveries],
            verification=dict(verification or {}),
            user_feedback=str(feedback or ""),
            feedback_verdict=str(verdict or ""),
            latency_ms=max(0.0, float(latency_ms)),
            retries=max(0, int(retries)),
            attempts=max(1, int(attempts)),
            correlation_id=str(correlation_id or ""),
            started_at=str(started_at or ""),
            finished_at=str(finished_at or ""),
        )

    def record(self, evidence: ExecutionEvidence, *, assess: bool = True) -> dict[str, Any]:
        """Assess and store one execution. Idempotent by experience id."""
        if not self.enabled:
            return {"recorded": False, "reason": "continual learning is disabled"}
        with self._lock:
            existing = self._state.experiences.get(evidence.experience_id)
            if existing is not None:
                return {
                    "recorded": False,
                    "duplicate": True,
                    "experience_id": existing.experience_id,
                    "outcome": existing.outcome,
                    "reason": "this experience id is already recorded",
                }
            try:
                if assess:
                    self.evaluator.apply(evidence)
                self._state.experiences[evidence.experience_id] = evidence
                self._prune()
                self._recompute_stats()
                self._refresh_signals()
                self._dirty = True
                # Enrichment is guarded on its own: the evidence is already
                # recorded, so a proposal or promotion defect must not turn a
                # successfully captured execution into a failed capture.
                proposal = promoted = rolled_back = None
                try:
                    if self.auto_learn:
                        proposal = self._auto_propose(evidence.task_kind)
                    if proposal is not None:
                        promoted = self._auto_promote(proposal)
                    if self.auto_rollback:
                        rolled_back = self._maybe_rollback(evidence.task_kind)
                except Exception as exc:  # pragma: no cover - enrichment is optional
                    self._note_error(f"learn: {type(exc).__name__}")
                self._emit(
                    "learning.experience_recorded",
                    {
                        "experience_id": evidence.experience_id,
                        "task_kind": evidence.task_kind,
                        "outcome": evidence.outcome,
                        "verified": evidence.verified,
                    },
                )
                if proposal is not None:
                    self._emit(
                        "learning.strategy_proposed",
                        {
                            "proposal_id": proposal.proposal_id,
                            "task_kind": proposal.task_kind,
                            "support": proposal.support,
                        },
                    )
                if promoted is not None:
                    self._emit(
                        "learning.strategy_promoted",
                        {
                            "strategy_id": promoted.strategy_id,
                            "version": promoted.version,
                            "task_kind": promoted.task_kind,
                        },
                    )
                if rolled_back is not None:
                    self._emit(
                        "learning.strategy_rolled_back",
                        {
                            "strategy_id": rolled_back.strategy_id,
                            "version": rolled_back.version,
                            "task_kind": rolled_back.task_kind,
                        },
                    )
                self._save()
                return {
                    "recorded": True,
                    "experience_id": evidence.experience_id,
                    "outcome": evidence.outcome,
                    "confidence": evidence.outcome_confidence,
                    "verified": evidence.verified,
                    "reasons": list(evidence.outcome_reasons),
                    "proposal": proposal.to_dict() if proposal is not None else None,
                    "promoted": promoted.to_dict() if promoted is not None else None,
                    "rolled_back": rolled_back.to_dict() if rolled_back is not None else None,
                }
            except Exception as exc:  # pragma: no cover - learning must not break work
                self._note_error(f"record: {type(exc).__name__}")
                return {"recorded": False, "reason": f"the record could not be filed: {type(exc).__name__}"}

    # -- feedback ---------------------------------------------------------

    def feedback(
        self,
        experience_id: str,
        verdict: str,
        *,
        note: str = "",
    ) -> dict[str, Any]:
        """A person's verdict on one recorded execution.

        Negative feedback is the strongest correction signal this system has: it
        reclassifies the run as ``USER_CORRECTED`` regardless of what any verifier
        thought, re-derives signals, and can trigger a regression rollback.
        Positive feedback counts as verification by the person who saw the result;
        it never upgrades a failure's steps, only the outcome's confirmation.
        """
        normalized = str(verdict or "").strip().lower()
        if normalized in _NEGATIVE_VERDICTS:
            kind = "negative"
        elif normalized in _POSITIVE_VERDICTS:
            kind = "positive"
        else:
            raise ValueError(
                "verdict must be positive or negative (accepted: "
                f"{', '.join(sorted(_POSITIVE_VERDICTS | _NEGATIVE_VERDICTS))})"
            )
        if not str(experience_id or "").strip():
            raise ValueError("feedback needs an experience_id")
        with self._lock:
            evidence = self._state.experiences.get(experience_id)
            if evidence is None:
                raise KeyError(f"no recorded experience {experience_id!r}")
            evidence.user_feedback = str(note or "").strip()
            evidence.feedback_verdict = kind
            if kind == "positive" and not evidence.verified:
                evidence.verification = {
                    **dict(evidence.verification),
                    "passed": True,
                    "status": "passed",
                    "source": "user",
                    "reason": "the user confirmed this execution",
                }
            self.evaluator.apply(evidence)
            self._recompute_stats()
            self._refresh_signals()
            self._dirty = True
            proposal = self._auto_propose(evidence.task_kind) if self.auto_learn and kind == "negative" else None
            rolled_back = self._maybe_rollback(evidence.task_kind) if self.auto_rollback else None
            self._emit(
                "learning.feedback_recorded",
                {
                    "experience_id": evidence.experience_id,
                    "task_kind": evidence.task_kind,
                    "verdict": kind,
                    "outcome": evidence.outcome,
                },
            )
            if proposal is not None:
                self._emit(
                    "learning.strategy_proposed",
                    {
                        "proposal_id": proposal.proposal_id,
                        "task_kind": proposal.task_kind,
                        "support": proposal.support,
                    },
                )
            self._save(force=True)
            return {
                "experience_id": evidence.experience_id,
                "verdict": kind,
                "outcome": evidence.outcome,
                "outcome_reasons": list(evidence.outcome_reasons),
                "proposal": proposal.to_dict() if proposal is not None else None,
                "rolled_back": rolled_back.to_dict() if rolled_back is not None else None,
            }

    # -- recommendation ---------------------------------------------------

    def recommend(
        self,
        goal: str,
        *,
        task_kind: str = "",
        context: Mapping[str, Any] | None = None,
        limit: int = 8,
    ) -> Recommendation:
        """Suggest a strategy or tool policy for this task, with provenance.

        This is the only method the request path calls, and it is advisory: an
        empty source means no comparable evidence, and a caller that ignores the
        result loses nothing.
        """
        if not self.enabled:
            return Recommendation(compatible=False, reason="continual learning is disabled")
        with self._lock:
            try:
                return self._recommend(goal, task_kind=task_kind, context=context or {}, limit=limit)
            except Exception as exc:  # pragma: no cover - a suggestion is optional
                self._note_error(f"recommend: {type(exc).__name__}")
                return Recommendation(
                    compatible=False,
                    reason=f"no suggestion: {type(exc).__name__}",
                    source="none",
                )

    def _recommend(
        self,
        goal: str,
        *,
        task_kind: str,
        context: Mapping[str, Any],
        limit: int,
    ) -> Recommendation:
        experiences = list(self._state.experiences.values())
        hits = self.retriever.retrieve(
            goal,
            task_kind=task_kind,
            context=context,
            experiences=experiences,
        )[: max(1, int(limit))]
        if not hits:
            return Recommendation(
                task_kind=task_kind,
                compatible=False,
                reason="no comparable previous execution",
                source="none",
            )
        by_id = {record.experience_id: record for record in experiences}
        resolved_kind = str(task_kind or "").strip().lower()
        if not resolved_kind:
            resolved_kind = by_id[hits[0].experience_id].task_kind
        active = self.book.active_for(resolved_kind, self._state.strategies)
        avoid = [
            signal.subject
            for signal in self._state.signals.values()
            if signal.kind == "tool_reliability"
            and signal.direction == "avoid"
            and signal.confidence >= self.min_confidence
            and (not signal.task_kind or signal.task_kind == resolved_kind or not task_kind)
        ]
        if active is not None and active.status == StrategyStatus.ACTIVE.value:
            stats = active.stats
            support_factor = min(1.0, stats.verified_trials / max(1, self.min_support))
            confidence = clamp01(
                0.75 * stats.success_rate + 0.2 * support_factor - 0.1 * min(stats.corrections, 3)
            )
            if stats.verified_trials >= self.min_support and confidence >= self.min_confidence:
                self._last_recommendation = {
                    "at": now_iso(),
                    "goal": str(goal)[:200],
                    "task_kind": resolved_kind,
                    "source": "strategy",
                    "strategy_id": active.strategy_id,
                    "version": active.version,
                    "confidence": round(confidence, 3),
                }
                return Recommendation(
                    task_kind=resolved_kind,
                    strategy_id=active.strategy_id,
                    strategy_version=active.version,
                    steps=[dict(step) for step in active.steps],
                    avoid_tools=sorted(set(active.avoid_tools) | set(avoid)),
                    prefer_tools=[tool for tool in active.tool_order if tool not in active.avoid_tools],
                    retry_budget=active.retry_budget,
                    verification_required=active.verification_required,
                    confidence=confidence,
                    evidence_ids=list(active.provenance)[:32],
                    compatible=True,
                    adapt_targets=True,
                    reason=(
                        f"strategy version {active.version} has {stats.verified_trials} verified "
                        f"success(es) in {stats.decided_trials} decided trial(s)"
                    ),
                    source="strategy",
                )
        # No active strategy clears the bar: recommend the closest VERIFIED
        # execution's steps as a weaker, evidence-only suggestion.
        for hit in hits:
            record = by_id[hit.experience_id]
            if not record.verified_outcome or not record.steps:
                continue
            if hit.score < 0.85:
                continue
            steps = [
                {"kind": step.kind, "target": step.target, "tool": step.tool}
                for step in record.steps
                if step.counts_as_done
            ]
            if not steps:
                continue
            confidence = clamp01(hit.score * 0.6)
            self._last_recommendation = {
                "at": now_iso(),
                "goal": str(goal)[:200],
                "task_kind": resolved_kind,
                "source": "evidence",
                "experience_id": record.experience_id,
                "confidence": round(confidence, 3),
            }
            return Recommendation(
                task_kind=resolved_kind,
                steps=steps,
                avoid_tools=sorted(set(avoid)),
                prefer_tools=[step.tool for step in record.steps if step.tool and step.tool not in avoid],
                retry_budget=max(0, min(3, record.retries + (1 if record.outcome == OutcomeClass.RECOVERED.value else 0))),
                verification_required=True,
                confidence=confidence,
                evidence_ids=[record.experience_id],
                compatible=True,
                adapt_targets=True,
                reason=(
                    f"{record.outcome} execution with score {hit.score:.2f} "
                    f"({hit.reason}); no validated strategy is active yet"
                ),
                source="evidence",
            )
        return Recommendation(
            task_kind=resolved_kind,
            avoid_tools=sorted(set(avoid)),
            compatible=False,
            reason="comparable executions exist but none is verified",
            evidence_ids=[hit.experience_id for hit in hits],
            source="none",
        )

    # -- proposals --------------------------------------------------------

    def propose(self, task_kind: str = "") -> dict[str, Any] | None:
        """Propose a strategy for a kind, or for the busiest uncovered kind."""
        with self._lock:
            kind = str(task_kind or "").strip().lower() or self._busiest_kind()
            if not kind:
                return None
            proposal = self._auto_propose(kind, force=True)
            self._save(force=True)
            return proposal.to_dict() if proposal is not None else None

    def validate(self, proposal_id: str) -> dict[str, Any]:
        with self._lock:
            proposal = self._state.proposals.get(proposal_id)
            if proposal is None:
                raise KeyError(f"no proposal {proposal_id!r}")
            if proposal.status == ProposalStatus.PROMOTED.value:
                raise ValueError("this proposal was already promoted; promote the next version instead")
            trial_strategy = proposal.strategy_id or ""
            trials = [
                evidence
                for evidence in self._state.experiences.values()
                if evidence.strategy_id and evidence.strategy_id == trial_strategy
            ]
            fallback = [
                self._state.experiences[evidence_id]
                for evidence_id in proposal.evidence_ids
                if evidence_id in self._state.experiences
            ]
            validated, summary = self.book.validate(
                proposal,
                trials=trials,
                fallback_evidence=fallback,
            )
            proposal.status = (
                ProposalStatus.VALIDATED.value if validated else ProposalStatus.REJECTED.value
            )
            proposal.validation = summary
            proposal.decided_at = now_iso()
            reasons = summary.get("reasons", [])
            if not isinstance(reasons, list):
                reasons = []
            proposal.decision_reason = "; ".join(str(item) for item in reasons)
            self._dirty = True
            self._emit(
                "learning.strategy_validated",
                {
                    "proposal_id": proposal.proposal_id,
                    "task_kind": proposal.task_kind,
                    "validated": validated,
                },
            )
            self._save(force=True)
            return {
                "proposal_id": proposal.proposal_id,
                "validated": validated,
                "status": proposal.status,
                "validation": summary,
            }

    def promote(self, proposal_id: str, *, reason: str = "") -> dict[str, Any]:
        with self._lock:
            proposal = self._state.proposals.get(proposal_id)
            if proposal is None:
                raise KeyError(f"no proposal {proposal_id!r}")
            if proposal.status != ProposalStatus.VALIDATED.value:
                raise ValueError(
                    "only a VALIDATED proposal can be promoted; validate it against executed evidence first"
                )
            record = self.book.promote(
                proposal,
                records=self._state.strategies,
                evidence=list(self._state.experiences.values()),
                reason=reason,
            )
            record.stats = self._stats_for(record)
            self._dirty = True
            self._save(force=True)
            return record.to_dict()

    def _auto_propose(
        self, task_kind: str, *, force: bool = False
    ) -> LearningProposal | None:
        kind = str(task_kind or "").strip().lower()
        if not kind:
            return None
        active = self.book.active_for(kind, self._state.strategies)
        experiences = [
            evidence
            for evidence in self._state.experiences.values()
            if evidence.task_kind == kind
        ]
        verified = [evidence for evidence in experiences if evidence.verified_outcome]
        if len({item.experience_id for item in verified}) < self.min_support:
            return None
        if active is not None and not force:
            # An active strategy with a clean record does not need another one;
            # only a correction or a regression justifies revisiting it.
            if active.stats.success_rate >= self.book.promotion_floor and active.stats.corrections < 2:
                return None
        avoid = [
            signal.subject
            for signal in self._state.signals.values()
            if signal.kind == "tool_reliability"
            and signal.direction == "avoid"
            and signal.confidence >= self.min_confidence
            and (not signal.task_kind or signal.task_kind == kind)
        ]
        proposal = self.book.build_proposal(
            task_kind=kind,
            experiences=experiences,
            records=self._state.strategies,
            avoid_tools=avoid,
            reasons=[
                f"{len(verified)} verified execution(s) of {kind!r} are recorded",
                f"minimum support for a template is {self.min_support}",
            ],
        )
        if proposal is None:
            return None
        for existing in self._state.proposals.values():
            if (
                existing.task_kind == proposal.task_kind
                and existing.status in {ProposalStatus.PROPOSED.value, ProposalStatus.VALIDATED.value}
                and existing.payload.get("shape") == proposal.payload.get("shape")
            ):
                return None  # duplicate proposal: the open one already covers it
        self._state.proposals[proposal.proposal_id] = proposal
        self._dirty = True
        return proposal

    def _auto_promote(self, proposal: LearningProposal | None) -> StrategyRecord | None:
        if proposal is None or not self.auto_promote:
            return None
        try:
            outcome = self.validate(proposal.proposal_id)
            if not outcome.get("validated"):
                return None
            promoted = self.promote(proposal.proposal_id, reason="validated automatically against executed evidence")
            return self.book.active_for(proposal.task_kind, self._state.strategies)
        except (KeyError, ValueError):
            return None

    def _maybe_rollback(self, task_kind: str) -> StrategyRecord | None:
        if not self.auto_rollback:
            return None
        active = self.book.active_for(task_kind, self._state.strategies)
        if active is None or active.parent_version <= 0:
            return None
        stats = active.stats
        reasons: list[str] = []
        if stats.corrections >= 2:
            reasons.append(f"{stats.corrections} user correction(s) against this version")
        elif stats.decided_trials >= 4 and stats.success_rate < PROMOTION_FLOOR:
            reasons.append(
                f"verified success rate {stats.success_rate:.2f} fell below {PROMOTION_FLOOR:.2f} "
                f"over {stats.decided_trials} decided trial(s)"
            )
        if not reasons:
            return None
        restored = self.book.rollback(
            task_kind,
            records=self._state.strategies,
            reason="; ".join(reasons),
            evidence=list(self._state.experiences.values()),
        )
        if restored is None:
            return None
        restored.stats = self._stats_for(restored)
        self._dirty = True
        self._save(force=True)
        return restored

    def rollback(self, task_kind: str, *, reason: str = "") -> dict[str, Any] | None:
        with self._lock:
            restored = self.book.rollback(
                task_kind,
                records=self._state.strategies,
                reason=reason or "rolled back by request",
                evidence=list(self._state.experiences.values()),
            )
            if restored is None:
                return None
            restored.stats = self._stats_for(restored)
            self._dirty = True
            self._save(force=True)
            return restored.to_dict()

    # -- reads ------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            outcomes = Counter(evidence.outcome for evidence in self._state.experiences.values() if evidence.outcome)
            kinds = Counter(evidence.task_kind for evidence in self._state.experiences.values())
            active = [
                record
                for record in self._state.strategies.values()
                if record.status == StrategyStatus.ACTIVE.value
            ]
            return {
                "enabled": self.enabled,
                "persist": self.persist,
                "experiences": len(self._state.experiences),
                "by_outcome": dict(sorted(outcomes.items())),
                "by_task_kind": dict(sorted(kinds.items())),
                "signals": len(self._state.signals),
                "strategies": len(self._state.strategies),
                "active_strategies": [record.to_dict() for record in active],
                "proposals": sum(
                    1
                    for proposal in self._state.proposals.values()
                    if proposal.status in {ProposalStatus.PROPOSED.value, ProposalStatus.VALIDATED.value}
                ),
                "policy": {
                    "min_support": self.min_support,
                    "min_confidence": self.min_confidence,
                    "promotion_floor": self.book.promotion_floor,
                    "auto_learn": self.auto_learn,
                    "auto_promote": self.auto_promote,
                    "auto_rollback": self.auto_rollback,
                    "max_experiences": self.max_experiences,
                    "retention_days": self.retention_days,
                },
                "storage": {
                    "schema_version": self._state.schema_version,
                    "skipped": dict(self._state.skipped),
                    "last_saved_at": self._last_saved_at,
                    "dirty": self._dirty,
                },
                "restored": dict(self._restore_report),
                "last_recommendation": dict(self._last_recommendation),
                "errors": list(self._errors[-10:]),
                # The boundaries are part of the surface: a caller must be able
                # to read what this subsystem will never do.
                "boundaries": {
                    "edits_source_code": False,
                    "changes_permissions": False,
                    "weakens_safety_controls": False,
                    "promotes_unverified_evidence": False,
                    "requires_execution_evidence": True,
                    "advisory_only": True,
                },
            }

    def overview(self) -> dict[str, Any]:
        """What the phase ships, with honest limits stated in the payload."""
        status = self.status()
        return {
            **status,
            "capabilities": [
                {
                    "name": "capture_experience",
                    "state": "available" if self.enabled else "disabled",
                    "detail": "structured evidence per execution, assessed from verification",
                },
                {
                    "name": "evaluate_outcome",
                    "state": "available" if self.enabled else "disabled",
                    "detail": "success/partial/failure/recovered/corrected/unverified",
                },
                {
                    "name": "retrieve_relevant_experience",
                    "state": "available" if self.enabled else "disabled",
                    "detail": "deterministic lexical relevance with context compatibility",
                },
                {
                    "name": "validated_strategy_versions",
                    "state": "available" if self.enabled else "disabled",
                    "detail": "proposal → validation → promotion, with rollback",
                },
                {
                    "name": "model_based_learning",
                    "state": "unavailable",
                    "detail": "Phase 27 uses no model; evidence is lexical and deterministic",
                },
                {
                    "name": "source_code_self_modification",
                    "state": "refused",
                    "detail": "learning changes strategy data only, never the build",
                },
            ],
            "limits": [
                "evidence is only as good as the verification the executing layer supplies",
                "a task with no comparable tokens is not learned from",
                "strategy templates are plan data; the existing planner still decides how to run them",
                "no delivery or real-world side effect can be verified beyond the recorded verifier",
            ],
        }

    def experiences(
        self,
        *,
        task_kind: str = "",
        outcome: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        with self._lock:
            records = [
                evidence
                for evidence in self._state.experiences.values()
                if (not task_kind or evidence.task_kind == task_kind)
                and (not outcome or evidence.outcome == outcome)
            ]
            records.sort(key=lambda evidence: (evidence.finished_at or evidence.started_at, evidence.experience_id), reverse=True)
            window = records[offset : offset + max(1, min(200, int(limit)))]
            return {
                "total": len(records),
                "experiences": [evidence.to_dict() for evidence in window],
            }

    def experience(self, experience_id: str) -> dict[str, Any]:
        with self._lock:
            evidence = self._state.experiences.get(experience_id)
            if evidence is None:
                raise KeyError(f"no recorded experience {experience_id!r}")
            return evidence.to_dict()

    def delete(self, experience_id: str) -> dict[str, Any]:
        with self._lock:
            if experience_id not in self._state.experiences:
                raise KeyError(f"no recorded experience {experience_id!r}")
            self._state.experiences.pop(experience_id, None)
            self._recompute_stats()
            self._refresh_signals()
            self._dirty = True
            self._save(force=True)
            return {"deleted": experience_id, "remaining": len(self._state.experiences)}

    def signals(self, *, task_kind: str = "", direction: str = "") -> dict[str, Any]:
        with self._lock:
            rows = [
                signal
                for signal in self._state.signals.values()
                if (not task_kind or signal.task_kind == task_kind)
                and (not direction or signal.direction == direction)
            ]
            rows.sort(key=lambda signal: (-signal.confidence, signal.signal_id))
            return {"total": len(rows), "signals": [signal.to_dict() for signal in rows]}

    def strategies(self) -> dict[str, Any]:
        with self._lock:
            rows = sorted(self._state.strategies.values(), key=lambda record: (-record.version, record.task_kind))
            return {"total": len(rows), "strategies": [record.to_dict() for record in rows]}

    def proposals(self, *, status: str = "") -> dict[str, Any]:
        with self._lock:
            rows = [
                proposal
                for proposal in self._state.proposals.values()
                if not status or proposal.status == status
            ]
            rows.sort(key=lambda proposal: (proposal.created_at, proposal.proposal_id), reverse=True)
            return {"total": len(rows), "proposals": [proposal.to_dict() for proposal in rows]}

    def proposal(self, proposal_id: str) -> dict[str, Any]:
        with self._lock:
            proposal = self._state.proposals.get(proposal_id)
            if proposal is None:
                raise KeyError(f"no proposal {proposal_id!r}")
            return proposal.to_dict()

    # -- internals --------------------------------------------------------

    def _stats_for(self, record: StrategyRecord) -> StrategyStats:
        stats = StrategyStats()
        seen: set[str] = set()
        provenance = set(record.provenance)
        for evidence in self._state.experiences.values():
            if evidence.experience_id in seen or not evidence.assessed:
                continue
            cites = evidence.strategy_id == record.strategy_id and (
                not evidence.strategy_version or evidence.strategy_version == record.version
            )
            if evidence.experience_id in provenance or cites:
                seen.add(evidence.experience_id)
                stats.record(evidence)
        return stats

    def _recompute_stats(self) -> None:
        for record in self._state.strategies.values():
            record.stats = self._stats_for(record)

    def _refresh_signals(self) -> None:
        derived = self.extractor.derive(list(self._state.experiences.values()))
        self._state.signals = {signal.signal_id: signal for signal in derived}

    def _busiest_kind(self) -> str:
        counts = Counter(
            evidence.task_kind
            for evidence in self._state.experiences.values()
            if evidence.verified_outcome
        )
        uncovered = [
            kind
            for kind in counts
            if self.book.active_for(kind, self._state.strategies) is None
        ]
        return sorted(uncovered, key=lambda kind: (-counts[kind], kind))[0] if uncovered else ""

    def _prune(self) -> None:
        """Cap the corpus, keeping what later decisions actually need."""
        if self.retention_days > 0:
            cutoff = time.time() - self.retention_days * 86400
            for experience_id, evidence in list(self._state.experiences.items()):
                if _timestamp(evidence.finished_at or evidence.started_at) < cutoff:
                    self._state.experiences.pop(experience_id, None)
        if len(self._state.experiences) <= self.max_experiences:
            return
        provenance: set[str] = set()
        for record in self._state.strategies.values():
            provenance.update(record.provenance)
        kept: dict[str, ExecutionEvidence] = {}
        for key in provenance:
            found = self._state.experiences.get(key)
            if found is not None:
                kept[key] = found
        ordered = sorted(
            self._state.experiences.values(),
            key=lambda evidence: (evidence.finished_at or evidence.started_at, evidence.experience_id),
            reverse=True,
        )
        for evidence in ordered:
            if len(kept) >= self.max_experiences:
                break
            kept.setdefault(evidence.experience_id, evidence)
        self._state.experiences = kept

    def _note_error(self, detail: str) -> None:
        self._errors.append(f"{now_iso()} {detail}")
        del self._errors[:-20]

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self._observer is None:
            return
        try:
            self._observer(event_type, dict(payload))
        except Exception as exc:  # pragma: no cover - an observer must not fail learning
            self._note_error(f"observer: {type(exc).__name__}")


def _timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return time.time()
