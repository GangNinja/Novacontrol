"""The Interactive Learning Manager — one bounded cycle, end to end (§4.1, §11).

The cycle this class runs is the one the phase names:

    OBSERVE → IDENTIFY UNCERTAINTY → FORM HYPOTHESIS → SELECT EXPERIMENT
    → CHECK SAFETY AND PERMISSIONS → EXECUTE BOUNDED ACTION → OBSERVE RESULT
    → COMPARE PREDICTION WITH ACTUAL RESULT → UPDATE KNOWLEDGE
    → EVALUATE LEARNING → SELECT NEXT EXPERIMENT

and every arrow is a recorded ``LearningCycleStep``, so a report shows what
happened rather than what was intended.

Five properties are structural rather than aspirational:

1. **Nothing acts without a prior verdict.** The candidate is validated by
   ``validate_experiment``, judged by the ``SafetyGate`` and charged against the
   ``BudgetTracker`` before ``execute`` is ever awaited (§11).
2. **Nothing is invented.** Hypotheses come from the environment's DECLARED
   candidate space; predictions come from a declared rule or a learned rule, and
   otherwise the run is recorded as prediction-unavailable. There is no branch that
   supplies a plausible expectation (§6/§18).
3. **A blocked, rejected, timed-out or unexecuted experiment is never reported as
   a success.** Its status is set from what happened, and only a run that actually
   executed can move a hypothesis (§4.3/§18).
4. **Reuse beats repetition.** A rule that already covers this environment and
   action ends the cycle with no experiment at all; an experiment whose evidence is
   still current is refused as a duplicate. Both are recorded with the reason (§8).
5. **The loop always terminates.** A budget ceiling, a cancellation, an answered
   question, or ``max_uninformative_in_a_row`` consecutive runs that taught nothing;
   the last one is ``unjustified`` rather than a quiet infinite loop (§11).

This manager never trains anything, never touches a model weight, and never acts
outside an environment the caller registered.
"""

from __future__ import annotations

import asyncio
import zlib
from collections.abc import Mapping, Sequence
from typing import Any

from novacontrol.interactive.capabilities import capability_rows
from novacontrol.interactive.comparison import (
    compare,
    evidence_for,
    match_hypotheses,
)
from novacontrol.interactive.environments import Environment, EnvironmentRegistry
from novacontrol.interactive.experiments import (
    DUPLICATE_INTERVENTION_REASON,
    DispositionPolicy,
    ExperimentLedger,
    candidate_experiments,
    intervention_key,
)
from novacontrol.interactive.governance import BudgetTracker, LearningPolicy, SafetyGate, cost_of
from novacontrol.interactive.hypotheses import (
    ConfidencePolicy,
    HypothesisBook,
    candidate_hypotheses,
    preview_hypotheses,
)
from novacontrol.interactive.knowledge import (
    DEFAULT_CONTRADICTION_THRESHOLD,
    DEFAULT_SUPPORT_THRESHOLD,
    RuleBook,
)
from novacontrol.interactive.metrics import LearningEvaluator, LearningMetrics, transfer_result
from novacontrol.interactive.models import (
    BudgetUsage,
    EnvironmentKind,
    EnvironmentObservation,
    EvidenceKind,
    EvidenceReference,
    ExperimentRun,
    ExperimentSpec,
    ExperimentStatus,
    Hypothesis,
    HypothesisStatus,
    KnowledgeChange,
    KnowledgeUpdate,
    LearnedRule,
    LearningBudget,
    LearningCycleReport,
    LearningCycleStep,
    LearningObjective,
    MatchingPolicy,
    ObjectiveStatus,
    OutcomeKind,
    StepKind,
    as_text,
    elapsed_seconds,
    new_id,
    now_iso,
    with_updates,
)
from novacontrol.interactive.prediction import (
    EnvironmentRuleProvider,
    LearnedRuleProvider,
    NoPredictionProvider,
    PredictionService,
    RuleLookup,
    WorldModelProvider,
)
from novacontrol.interactive.selection import (
    ExperimentSelector,
    SelectionContext,
    SelectionOutcome,
    strategy_by_name,
)
from novacontrol.interactive.store import LearningRepository
from novacontrol.perception.models import CapabilityState

__all__ = ["InteractiveLearningManager", "TERMINAL_OBJECTIVE_STATUSES"]

#: Objective statuses no further cycle may run from. A terminal objective is not
#: re-run by accident; a caller that wants more must open a new objective.
TERMINAL_OBJECTIVE_STATUSES = frozenset(
    {
        ObjectiveStatus.ACHIEVED,
        ObjectiveStatus.EXHAUSTED,
        ObjectiveStatus.STOPPED,
        ObjectiveStatus.UNJUSTIFIED,
        ObjectiveStatus.ABANDONED,
    }
)

#: The parameter name a replicate adds when the action declares it, so a second
#: run of one experiment is a distinct experiment rather than a duplicate (§7).
REPLICATE_PARAMETER = "replicate"


class InteractiveLearningManager:
    """The bounded learning cycle over the environments a caller registered."""

    def __init__(
        self,
        *,
        environments: EnvironmentRegistry | None = None,
        selector: ExperimentSelector | None = None,
        gate: SafetyGate | None = None,
        policy: LearningPolicy | None = None,
        repository: LearningRepository | None = None,
        book: RuleBook | None = None,
        hypothesis_book: HypothesisBook | None = None,
        ledger: ExperimentLedger | None = None,
        observer: Any | None = None,
        matching: MatchingPolicy | None = None,
        disposition: DispositionPolicy | None = None,
        confidence: ConfidencePolicy | None = None,
        world_bridge: Any | None = None,
        correlation_id: str = "",
        support_threshold: int = DEFAULT_SUPPORT_THRESHOLD,
        contradiction_threshold: int = DEFAULT_CONTRADICTION_THRESHOLD,
    ) -> None:
        self.environments = environments or EnvironmentRegistry()
        self.policy = policy or LearningPolicy()
        self.matching = matching or MatchingPolicy()
        self.disposition = disposition or DispositionPolicy(
            allow_destructive=self.policy.allow_destructive,
            max_cost_multiple_of_budget=1.0,
            require_distinguishing=True,
        )
        self.gate = gate or SafetyGate(policy=self.policy)
        self.selector = selector or ExperimentSelector(gate=self.gate)
        self.repository = repository
        self.book = book or RuleBook(
            support_threshold=support_threshold,
            contradiction_threshold=contradiction_threshold,
        )
        self.hypotheses = hypothesis_book or HypothesisBook(policy=confidence)
        self.ledger = ledger or ExperimentLedger()
        self.observer = observer
        self.world_bridge = world_bridge
        self.correlation_id = as_text(correlation_id)
        self.evaluator = LearningEvaluator(support_threshold=self.book.support_threshold)
        self._objectives: dict[str, LearningObjective] = {}
        self._runs: dict[str, list[ExperimentRun]] = {}
        self._evidence: dict[str, EvidenceReference] = {}
        self._steps: dict[str, list[LearningCycleStep]] = {}
        #: Interventions per objective whose verdict SETTLED a hypothesis, so a
        #: repeat of one is a duplicate and a repeat of an inconclusive one is not.
        self._answered: dict[str, set[str]] = {}
        self._approvals: dict[str, tuple[str, str]] = {}
        self._rejections: dict[str, str] = {}
        self._interrupted: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.duplicates_avoided = 0
        self.safety_refusals = 0
        self.reuse_count = 0

    # ── objectives ───────────────────────────────────────────────────────

    def create_objective(
        self,
        question: str,
        *,
        environment_id: str = "",
        scope: str = "",
        budget: LearningBudget | Mapping[str, Any] | None = None,
        correlation_id: str = "",
        limitations: Sequence[str] = (),
    ) -> LearningObjective:
        """A bounded question. The environment must be one that is registered."""
        text = as_text(question)
        if not text:
            raise ValueError("a learning objective needs a question")
        environment = self._resolve_environment(environment_id)
        if environment is None:
            registered = ", ".join(self.environments.identifiers()) or "none"
            raise ValueError(
                f"no environment {environment_id!r} is registered; this build has: {registered}"
            )
        resolved_budget = (
            budget
            if isinstance(budget, LearningBudget)
            else LearningBudget.from_mapping(budget or {})
        )
        stamp = now_iso()
        objective = LearningObjective(
            objective_id=new_id("obj"),
            question=text,
            environment_id=environment.environment_id,
            scope=as_text(scope) or environment.environment_id,
            budget=resolved_budget,
            correlation_id=as_text(correlation_id) or self.correlation_id,
            created_at=stamp,
            updated_at=stamp,
            limitations=tuple(limitations),
        )
        self._objectives[objective.objective_id] = objective
        self._steps[objective.objective_id] = []
        self._runs[objective.objective_id] = []
        self._announce(
            "learning.objective_created",
            {"objective_id": objective.objective_id, "environment_id": objective.environment_id},
            correlation_id=objective.correlation_id,
        )
        self._persist(objective.objective_id)
        return objective

    def objective(self, objective_id: str) -> LearningObjective | None:
        return self._objectives.get(as_text(objective_id))

    def objectives(self) -> tuple[LearningObjective, ...]:
        return tuple(self._objectives.values())

    def open_objectives(self) -> tuple[LearningObjective, ...]:
        return tuple(
            item
            for item in self._objectives.values()
            if item.status not in TERMINAL_OBJECTIVE_STATUSES
        )

    def runs(self, objective_id: str) -> tuple[ExperimentRun, ...]:
        return tuple(self._runs.get(as_text(objective_id), ()))

    def all_runs(self) -> tuple[ExperimentRun, ...]:
        return tuple(run for rows in self._runs.values() for run in rows)

    def evidence(self, evidence_id: str) -> EvidenceReference | None:
        return self._evidence.get(as_text(evidence_id))

    def evidence_for_objective(self, objective_id: str) -> tuple[EvidenceReference, ...]:
        """Every evidence reference this objective's experiments produced."""
        ids = {item.experiment_id for item in self.ledger.for_objective(objective_id)}
        return tuple(item for item in self._evidence.values() if item.experiment_id in ids)

    def objective_view(self, objective_id: str) -> dict[str, Any]:
        """One objective with everything recorded about it, for a caller to read.

        One shape for the API, a CLI and a test. It carries REFERENCES and statuses,
        never a reasoning trace: the steps say what the cycle did, and the evidence
        list is how a conclusion is traced back to the observations behind it.
        """
        objective = self._objectives.get(as_text(objective_id))
        if objective is None:
            raise ValueError(f"no learning objective {objective_id!r} in this manager")
        run_ids = {item.experiment_id for item in self.runs(objective.objective_id)}
        return {
            "objective": objective.to_dict(),
            "hypotheses": [
                item.to_dict() for item in self.hypotheses.for_objective(objective.objective_id)
            ],
            "experiments": [
                item.to_dict() for item in self.ledger.for_objective(objective.objective_id)
            ],
            "runs": [item.to_dict() for item in self.runs(objective.objective_id)],
            "steps": [item.to_dict() for item in self.steps(objective.objective_id)],
            "evidence": [
                item.to_dict() for item in self.evidence_for_objective(objective.objective_id)
            ],
            "rules": [item.to_dict() for item in self.book.for_objective(objective.objective_id)],
            "budget_usage": objective.usage.to_dict(),
            "interrupted": [
                item for item in self.interrupted() if item["experiment_id"] in run_ids
            ],
            "simulated": objective.environment_id in set(self.environments.simulators()),
            "limitations": list(objective.limitations),
        }

    def transfer_check(
        self,
        *,
        rule_id: str,
        held_out: Sequence[Mapping[str, Any]],
        experiments_before: int = 0,
        experiments_after: int = 0,
    ) -> dict[str, Any]:
        """Check a learned rule against cases the CALLER held out.

        Each case names an action and the outcome actually observed for it; the rule
        predicts, and the agreements are counted. What this measures is a rule applied
        to cases the caller says were not used to build it — this build cannot verify
        that claim, so the limitation says so rather than presenting the number as
        generalization (§10).
        """
        rule = next((item for item in self.book.rules if item.rule_id == as_text(rule_id)), None)
        if rule is None:
            raise ValueError(f"no learned rule {rule_id!r} in this session")
        agreed = 0
        cases = 0
        skipped = 0
        mismatches: list[str] = []
        for case in held_out:
            if not isinstance(case, Mapping):
                skipped += 1
                continue
            action = as_text(case.get("action"))
            actual = case.get("expected")
            predicted = rule.predicts(action)
            if not action or not isinstance(actual, Mapping) or not predicted:
                skipped += 1
                continue
            cases += 1
            verdict = _matches(predicted, _observation_from(actual), self.matching)
            if verdict is OutcomeKind.CONFIRMED:
                agreed += 1
            else:
                mismatches.append(
                    f"{action}: the rule expected {dict(predicted)}, "
                    f"the case reports {dict(actual)}"
                )
        result = transfer_result(
            before_seconds=None,
            after_seconds=None,
            experiments_before=max(0, experiments_before),
            experiments_after=max(0, experiments_after),
            held_out=True,
            cases=cases,
        ).to_dict()
        result.update(
            {
                "rule_id": rule.rule_id,
                "rule_status": rule.status.value,
                "scope": rule.scope,
                "agreed": agreed,
                "mismatches": mismatches,
                "skipped": skipped,
                "agreement_rate": (agreed / cases) if cases else None,
                "limitations": [
                    *result.get("limitations", []),
                    "this build cannot verify that the cases were held out; it takes the "
                    "caller's word and reports what the rule did with them",
                    "a rule that agrees here has not been shown to generalize beyond this "
                    "caller's cases",
                ],
            }
        )
        return result

    def interrupt_all(self, reason: str) -> int:
        """Mark every in-flight experiment INTERRUPTED — for a caller stopping the build."""
        marked = 0
        for rows in self._runs.values():
            for run in rows:
                if run.status is not ExperimentStatus.RUNNING:
                    continue
                self._mark_interrupted(run, reason)
                marked += 1
        return marked

    def steps(self, objective_id: str) -> tuple[LearningCycleStep, ...]:
        return tuple(self._steps.get(as_text(objective_id), ()))

    def approve(self, objective_id: str, *, reason: str = "") -> dict[str, Any]:
        """Approve an objective for a real (non-simulator) experiment, once."""
        objective = self._require(objective_id)
        self._approvals[objective.objective_id] = (now_iso(), as_text(reason))
        return {
            "objective_id": objective.objective_id,
            "approved": True,
            "reason": as_text(reason) or "approved by the caller",
            "single_use": True,
        }

    def reject(self, objective_id: str, *, reason: str = "") -> LearningObjective:
        """Refuse an objective: it stops, with the refusal recorded as its reason."""
        objective = self._require(objective_id)
        text = as_text(reason) or "the objective was rejected by the caller"
        self._rejections[objective.objective_id] = text
        updated = with_updates(
            objective,
            status=ObjectiveStatus.STOPPED,
            stop_reason=text,
            updated_at=now_iso(),
        )
        self._objectives[objective.objective_id] = updated
        self._persist(objective.objective_id)
        return updated

    def cancel(self, objective_id: str, *, reason: str = "") -> LearningObjective:
        """Ask a cycle to stop at its next checkpoint — recorded, never a silent abort."""
        objective = self._require(objective_id)
        text = as_text(reason) or "cancelled by the caller"
        self._rejections[f"cancel:{objective.objective_id}"] = text
        self._announce(
            "learning.experiment_cancelled",
            {"objective_id": objective.objective_id, "reason": text},
            correlation_id=objective.correlation_id,
        )
        return objective

    # ── preview and selection ────────────────────────────────────────────

    def preview(self, objective_id: str, *, strategy: str = "") -> dict[str, Any]:
        """The candidates for the next experiment, ranked — nothing is executed.

        When the objective has no hypotheses yet, the ones the cycle WOULD propose are
        built as values and shown labelled ``dry_run`` — a preview that showed nothing
        until a run had happened would be useless, and writing them into the book
        would make "show me what you would do" change what the record says was done.
        """
        objective = self._require(objective_id)
        environment = self._resolve_environment(objective.environment_id)
        chosen_strategy = self._strategy(strategy)
        selector = ExperimentSelector(strategy=chosen_strategy, gate=self.gate)
        context = self._context(objective, environment)
        existing = self.hypotheses.testable_for_objective(objective.objective_id)
        dry_run = not existing
        hypotheses = existing or preview_hypotheses(
            environment,
            objective_id=objective.objective_id,
            scope=objective.scope,
            exclude=tuple(
                item.statement for item in self.hypotheses.for_objective(objective.objective_id)
            ),
        )
        candidates = self._candidate_specs(objective, environment, hypotheses=hypotheses)
        outcome = selector.select(candidates, context=context)
        return {
            "objective_id": objective.objective_id,
            "environment_id": objective.environment_id,
            "strategy": outcome.strategy,
            "considered": outcome.considered,
            "candidates": [item.to_dict() for item in outcome.candidates],
            "chosen": None if outcome.chosen is None else outcome.chosen.to_dict(),
            "rejected": [
                {"experiment_id": experiment_id, "reason": reason}
                for experiment_id, reason in outcome.rejected
            ],
            "hypotheses": [item.to_dict() for item in hypotheses],
            "dry_run": dry_run,
            "limitations": list(outcome.limitations)
            + (
                [
                    "the hypotheses shown here are the ones the cycle WOULD propose; "
                    "nothing was recorded and nothing was executed",
                ]
                if dry_run
                else []
            ),
            "executed": False,
        }

    def compare_strategies(self, objective_id: str) -> dict[str, Any]:
        """What each shipped strategy would choose — for a reproducible comparison."""
        objective = self._require(objective_id)
        environment = self._resolve_environment(objective.environment_id)
        selector = ExperimentSelector(
            strategies=tuple(
                item
                for item in (
                    strategy_by_name(name) for name in ("information_value", "bounded_random")
                )
                if item is not None
            ),
            gate=self.gate,
        )
        context = self._context(objective, environment)
        rows = selector.compare(self._candidate_specs(objective, environment), context=context)
        return {
            "objective_id": objective.objective_id,
            "environment_id": objective.environment_id,
            "rows": rows,
            "limitations": [
                "this compares what each strategy WOULD choose for the same candidates",
                "a difference in choice is not by itself a difference in learning",
            ],
        }

    # ── the cycle ────────────────────────────────────────────────────────

    async def run_cycle(
        self,
        objective_id: str,
        *,
        approval: bool = False,
        max_experiments: int | None = None,
        strategy: str = "",
    ) -> LearningCycleReport:
        """Run the bounded cycle for one objective and report what it did."""
        objective = self._require(objective_id)
        if objective.status in TERMINAL_OBJECTIVE_STATUSES:
            return self._report(
                objective,
                stop_reason=(
                    f"the objective is already {objective.status.value}: "
                    f"{objective.stop_reason or 'no further cycle is run'}"
                ),
                steps=(),
            )
        lock = self._locks.setdefault(objective.objective_id, asyncio.Lock())
        if lock.locked():
            # A second submission of the same objective is one submission (§13).
            self.duplicates_avoided += 1
            return self._report(
                objective,
                stop_reason=(
                    "a cycle for this objective is already running, so this submission did "
                    "not start a second one"
                ),
                steps=(),
                extra={"duplicate_submission": True},
            )
        async with lock:
            return await self._run_locked(
                objective,
                approval=approval,
                max_experiments=max_experiments,
                strategy=strategy,
            )

    async def _run_locked(
        self,
        objective: LearningObjective,
        *,
        approval: bool,
        max_experiments: int | None,
        strategy: str,
    ) -> LearningCycleReport:
        environment = self._resolve_environment(objective.environment_id)
        tracker = BudgetTracker(objective.budget)
        tracker.restore(objective.usage)
        steps = self._steps.setdefault(objective.objective_id, [])
        steps.clear()
        chosen_strategy = self._strategy(strategy)
        selector = ExperimentSelector(strategy=chosen_strategy, gate=self.gate)
        approved = approval or self._consume_approval(objective.objective_id)
        rejection = self._rejection(objective.objective_id)
        if rejection:
            self._announce(
                "learning.experiment_rejected",
                {"objective_id": objective.objective_id, "experiment_id": "", "reason": rejection},
                correlation_id=objective.correlation_id,
            )
            return self._finish(
                objective,
                tracker,
                steps,
                ObjectiveStatus.STOPPED,
                rejection,
                environment=environment,
                extra={"rejected": True},
            )
        budget = objective.budget
        limit = (
            max(1, min(max_experiments, budget.max_experiments))
            if max_experiments is not None
            else budget.max_experiments
        )
        uninformative = 0
        stop_reason = ""
        status = ObjectiveStatus.OPEN
        last_run: ExperimentRun | None = None
        last_comparison = None
        updates: list[KnowledgeUpdate] = []
        while True:
            for kind, detail, data in self._observe(environment):
                self._append(objective, kind, detail, **data)
            # IDENTIFY UNCERTAINTY (a reused rule answers it before any action).
            reuse = self._covering_rule(objective, environment)
            if reuse is not None:
                self.reuse_count += 1
                self._append(
                    objective,
                    StepKind.IDENTIFY_UNCERTAINTY,
                    f"a learned rule already covers {reuse.condition.get('action')!r} in this "
                    "scope, so the question is answered without a new experiment",
                    rule_id=reuse.rule_id,
                    status=reuse.status.value,
                )
                status, stop_reason = (
                    ObjectiveStatus.ACHIEVED,
                    (
                        f"answered from the learned rule {reuse.rule_id} ({reuse.status.value}); "
                        "no experiment was run"
                    ),
                )
                break
            testable = self.hypotheses.testable_for_objective(objective.objective_id)
            if not testable:
                proposed = self._propose(objective, environment)
                testable = self.hypotheses.testable_for_objective(objective.objective_id)
                if not testable:
                    status = ObjectiveStatus.UNJUSTIFIED
                    stop_reason = (
                        "no testable hypothesis could be formed: the environment declares no "
                        "candidate explanation and no rule to test"
                        if not proposed
                        else "every hypothesis for this objective is already resolved"
                    )
                    break
            candidates = self._candidate_specs(objective, environment)
            context = self._context(objective, environment, approval=approved)
            outcome: SelectionOutcome = selector.select(candidates, context=context)
            if outcome.chosen is None:
                self._record_refusals(outcome)
                status, stop_reason = self._no_candidate_status(objective, outcome)
                break
            spec = self.ledger.add(outcome.chosen.experiment)
            information = outcome.chosen.information_value
            self._append(
                objective,
                StepKind.SELECT_EXPERIMENT,
                f"selected {spec.action!r} in {spec.environment_id!r} "
                f"(strategy {outcome.strategy}, information value "
                f"{information if information is not None else 'unknown'})",
                experiment_id=spec.experiment_id,
                action=spec.action,
                strategy=outcome.strategy,
                considered=outcome.considered,
                rejected=[reason for _, reason in outcome.rejected][:4],
            )
            self._announce(
                "learning.experiment_proposed",
                {
                    "objective_id": objective.objective_id,
                    "experiment_id": spec.experiment_id,
                    "action": spec.action,
                },
                correlation_id=objective.correlation_id,
            )
            verdict = self._budget_verdict(tracker, spec)
            if not verdict.allowed:
                self.ledger.set_status(
                    spec.experiment_id, ExperimentStatus.REJECTED, reason=verdict.reason
                )
                self._append(
                    objective,
                    StepKind.CHECK_SAFETY,
                    f"refused before execution: {verdict.reason}",
                    experiment_id=spec.experiment_id,
                    reason=verdict.reason,
                )
                self._announce(
                    "learning.budget_exhausted",
                    {"objective_id": objective.objective_id, "reason": verdict.reason},
                    correlation_id=objective.correlation_id,
                )
                status, stop_reason = ObjectiveStatus.EXHAUSTED, verdict.reason
                break
            safety = self.gate.evaluate(spec, environment=environment, approval=approved)
            self._append(
                objective,
                StepKind.CHECK_SAFETY,
                f"{'allowed' if safety.allowed else 'refused'} ({safety.source}): {safety.reason}",
                experiment_id=spec.experiment_id,
                allowed=safety.allowed,
                source=safety.source,
                risk=safety.risk,
            )
            if not safety.allowed:
                self.safety_refusals += 1
                self.ledger.set_status(
                    spec.experiment_id, ExperimentStatus.BLOCKED, reason=safety.reason
                )
                self._announce(
                    "learning.experiment_rejected",
                    {
                        "objective_id": objective.objective_id,
                        "experiment_id": spec.experiment_id,
                        "reason": safety.reason,
                    },
                    correlation_id=objective.correlation_id,
                )
                if safety.source == "approval":
                    # A missing approval is a pending decision, not an untestable
                    # question: leaving the objective open is what lets the approval
                    # that follows actually be spent by the next cycle.
                    status, stop_reason = (
                        ObjectiveStatus.OPEN,
                        (
                            f"nothing was attempted: {safety.reason} — approve this objective "
                            "and run it again; one approval authorizes one cycle"
                        ),
                    )
                else:
                    status, stop_reason = (
                        ObjectiveStatus.STOPPED,
                        (
                            f"the action was refused before execution ({safety.source}): "
                            f"{safety.reason}"
                        ),
                    )
                break
            self.ledger.set_status(spec.experiment_id, ExperimentStatus.APPROVED)
            self._announce(
                "learning.experiment_approved",
                {"objective_id": objective.objective_id, "experiment_id": spec.experiment_id},
                correlation_id=objective.correlation_id,
            )
            if environment is None:  # pragma: no cover - the gate refuses this first
                status, stop_reason = ObjectiveStatus.STOPPED, "the environment disappeared"
                break
            environment = self._environment_for(environment)
            before_book = self._book_signature(objective.objective_id)
            run, updates_run = await self._execute(objective, environment, spec, tracker)
            last_run = run
            last_comparison = run.comparison
            updates.extend(updates_run)
            # What made this run worth its budget: a prediction that was scored, or a
            # hypothesis that MOVED. A run with no prediction is not automatically an
            # uninformative one — in a simulator whose rule is hidden there is never a
            # prediction, and counting those as uninformative would stop a cycle that
            # is learning perfectly well.
            scored = run.comparison is not None and run.comparison.kind in {
                OutcomeKind.CONFIRMED,
                OutcomeKind.PARTIALLY_CONFIRMED,
                OutcomeKind.CONTRADICTED,
            }
            moved = self._book_signature(objective.objective_id) != before_book
            uninformative = 0 if (scored or moved) else uninformative + 1
            self._append(
                objective,
                StepKind.EVALUATE,
                self._evaluation_detail(objective, run),
                experiment_id=spec.experiment_id,
                status=run.status.value,
                actions=run.actions,
            )
            status, stop_reason, stop = self._terminal(
                objective, tracker, run, uninformative, limit
            )
            if stop or status is not ObjectiveStatus.OPEN:
                break
        return self._finish(
            objective,
            tracker,
            steps,
            status,
            stop_reason,
            environment=environment,
            run=last_run,
            comparison=last_comparison,
            updates=tuple(updates),
            extra={"strategy": chosen_strategy.name},
        )

    # ── one experiment ───────────────────────────────────────────────────

    async def _execute(
        self,
        objective: LearningObjective,
        environment: Environment,
        spec: ExperimentSpec,
        tracker: BudgetTracker,
    ) -> tuple[ExperimentRun, list[KnowledgeUpdate]]:
        """Execute one bounded experiment, observe, compare, update. Always records."""
        started = now_iso()
        prediction = self._prediction_service(environment).predict(
            spec,
            self.hypotheses.for_objective(objective.objective_id),
            scope=objective.scope or environment.environment_id,
        )
        before = await environment.observe()
        self._append(
            objective,
            StepKind.OBSERVE,
            f"initial state read from {environment.environment_id!r} (complete={before.complete})",
            experiment_id=spec.experiment_id,
            observation_ref=before.observation_ref,
        )
        self._append(
            objective,
            StepKind.COMPARE,
            f"prediction basis: {prediction.basis.value}"
            + (f" — {prediction.reason}" if prediction.reason else ""),
            experiment_id=spec.experiment_id,
            basis=prediction.basis.value,
        )
        self.ledger.set_status(spec.experiment_id, ExperimentStatus.RUNNING)
        self._announce(
            "learning.experiment_started",
            {"objective_id": objective.objective_id, "experiment_id": spec.experiment_id},
            correlation_id=objective.correlation_id,
        )
        attempts = 0
        allowed_retries = max(0, min(spec.max_retries, objective.budget.max_retries))
        result = None
        timed_out = False
        while True:
            # The FINAL attempt decides what happened: an earlier timeout must not
            # label a later ordinary failure as a timeout.
            timed_out = False
            try:
                result = await asyncio.wait_for(
                    environment.execute(spec.action, spec.parameters),
                    timeout=max(0.001, spec.max_seconds),
                )
            except TimeoutError:
                timed_out = True
                result = None
            if result is not None and result.ok:
                attempts += max(1, result.actions)
                break
            blocking = result is not None and result.status == "blocked"
            if timed_out or blocking or attempts >= allowed_retries:
                break
            attempts += 1
            tracker.record(retries=1)
        duration_ms = elapsed_seconds(started, now_iso()) * 1000.0
        actions = result.actions if result is not None and result.ok else 0
        run_status = ExperimentStatus.COMPLETED
        error = ""
        if result is None or not result.ok:
            error = (
                "the environment did not answer within the experiment's own time limit"
                if result is None
                else result.error or result.reason or "the action did not complete"
            )
            if result is not None and result.status == "blocked":
                run_status = ExperimentStatus.BLOCKED
            elif timed_out:
                run_status = ExperimentStatus.TIMED_OUT
            else:
                run_status = ExperimentStatus.FAILED
        observation: EnvironmentObservation | None = None
        verdicts: dict[str, OutcomeKind] = {}
        comparison = None
        if run_status is ExperimentStatus.COMPLETED:
            # An action that reports its own reading is believed, and the environment
            # is asked only when it did not: a bridged executor's returned values ARE
            # the post-action observation, and a second read could describe a state
            # that moved on in between.
            observation = (
                result.observation
                if result is not None and result.observation is not None
                else await environment.observe()
            )
            self._append(
                objective,
                StepKind.OBSERVE_RESULT,
                f"post-action observation {observation.observation_ref} "
                f"(complete={observation.complete}, quality={observation.quality:.2f})",
                experiment_id=spec.experiment_id,
                observation_ref=observation.observation_ref,
                complete=observation.complete,
            )
            self._announce(
                "learning.observation_collected",
                {
                    "objective_id": objective.objective_id,
                    "experiment_id": spec.experiment_id,
                    "complete": observation.complete,
                },
                correlation_id=objective.correlation_id,
            )
            comparison = compare(
                prediction,
                observation,
                matching=self.matching,
                before=before,
                expected_before=None,
                started_at=started,
            )
            verdicts = match_hypotheses(spec, observation, matching=self.matching)
            self._announce(
                "learning.prediction_evaluated",
                {
                    "objective_id": objective.objective_id,
                    "experiment_id": spec.experiment_id,
                    "outcome": comparison.kind.value,
                    "basis": prediction.basis.value,
                },
                correlation_id=objective.correlation_id,
            )
        cost = cost_of(duration_ms / 1000.0, actions)
        tracker.record(
            actions=actions,
            seconds=duration_ms / 1000.0,
            cost=cost,
            experiments=1,
        )
        self.ledger.set_status(spec.experiment_id, run_status, reason=error)
        run = ExperimentRun(
            run_id=new_id("run"),
            experiment_id=spec.experiment_id,
            objective_id=objective.objective_id,
            status=run_status,
            environment_id=environment.environment_id,
            environment_kind=environment.kind,
            prediction=prediction,
            observation=observation,
            comparison=comparison,
            before=before,
            actions=actions,
            retries=tracker.usage.retries,
            duration_ms=round(duration_ms, 3),
            cost_units=round(cost, 4),
            error=error,
            reason=as_text(result.reason) if result is not None else "",
            started_at=started,
            finished_at=now_iso(),
            simulated=environment.kind is EnvironmentKind.SIMULATOR,
        )
        self._runs.setdefault(objective.objective_id, []).append(run)
        event = "learning.experiment_completed" if run.executed else "learning.experiment_failed"
        self._announce(
            event,
            {
                "objective_id": objective.objective_id,
                "experiment_id": spec.experiment_id,
                "status": run_status.value,
                **({"error": error} if error else {}),
            },
            correlation_id=objective.correlation_id,
        )
        updates = self._update_knowledge(objective, environment, spec, run, verdicts)
        self._persist(objective.objective_id)
        return run, updates

    # ── knowledge ────────────────────────────────────────────────────────

    def _update_knowledge(
        self,
        objective: LearningObjective,
        environment: Environment,
        spec: ExperimentSpec,
        run: ExperimentRun,
        verdicts: Mapping[str, OutcomeKind],
    ) -> list[KnowledgeUpdate]:
        """Evidence → hypothesis status → (maybe) a rule. Never the other way round."""
        updates: list[KnowledgeUpdate] = []
        if not run.executed or run.status is not ExperimentStatus.COMPLETED:
            return updates
        references = evidence_for(
            spec,
            observation=run.observation,
            verdicts=verdicts,
            comparison=run.comparison,
        )
        for reference in references:
            self._evidence[reference.evidence_id] = reference
        for hypothesis_id in spec.hypothesis_ids:
            mine = tuple(item for item in references if item.hypothesis_id == hypothesis_id)
            if not mine:
                continue
            verdict = verdicts.get(hypothesis_id, OutcomeKind.INCONCLUSIVE)
            applied = self.hypotheses.apply_evidence(
                hypothesis_id,
                mine,
                reason=f"{verdict.value} from {spec.action!r}",
                experiment_id=spec.experiment_id,
                observation_refs=(run.observation.observation_ref,) if run.observation else (),
            )
            if applied.status in {HypothesisStatus.SUPPORTED, HypothesisStatus.CONTRADICTED}:
                # This intervention settled something, so repeating it would be a
                # duplicate rather than a second observation (§7).
                self._answered.setdefault(objective.objective_id, set()).add(
                    intervention_key(
                        environment_id=spec.environment_id,
                        action=spec.action,
                        parameters=spec.parameters,
                    )
                )
            self._announce(
                "learning.hypothesis_updated",
                {
                    "objective_id": objective.objective_id,
                    "hypothesis_id": applied.hypothesis_id,
                    "status": applied.status.value,
                },
                correlation_id=objective.correlation_id,
            )
            self._append(
                objective,
                StepKind.UPDATE_KNOWLEDGE,
                f"{applied.hypothesis_id} is now {applied.status.value} "
                f"(independent support {applied.independent_support}, "
                f"contradiction {applied.independent_contradiction})",
                hypothesis_id=applied.hypothesis_id,
                status=applied.status.value,
            )
            if applied.status is HypothesisStatus.CONTRADICTED:
                for rule in self.book.for_objective(objective.objective_id):
                    if rule.status.value == "retired":
                        continue
                    if rule.predicts(spec.action):
                        updates.append(
                            self.book.contradict(
                                rule.rule_id,
                                evidence_id=mine[0].evidence_id,
                                experiment_id=spec.experiment_id,
                                reason=(
                                    f"an observation in {environment.environment_id!r} disagreed "
                                    f"with {rule.rule_id}"
                                ),
                            )
                        )
            promotion = self._promote(objective, environment, spec, applied, mine)
            if promotion is not None:
                updates.append(promotion)
                if promotion.change is not KnowledgeChange.NO_CHANGE:
                    # A refusal to promote is RECORDED (its reason names the counts
                    # that stopped it) but it is not a knowledge change, so the
                    # "knowledge updated" announcement is only for real changes.
                    self._announce(
                        "learning.knowledge_updated",
                        {
                            "objective_id": objective.objective_id,
                            "change": promotion.change.value,
                        },
                        correlation_id=objective.correlation_id,
                    )
            updates.extend(self._generalize(objective, environment, spec, run, applied))
        return updates

    def _promote(
        self,
        objective: LearningObjective,
        environment: Environment,
        spec: ExperimentSpec,
        hypothesis: Hypothesis,
        references: Sequence[EvidenceReference],
    ) -> KnowledgeUpdate | None:
        """Promote a hypothesis to a scoped rule, or let the book record the refusal.

        The threshold decisions belong to the BOOK, not here: it is what records a
        ``NO_CHANGE`` whose reason names the counts that stopped the promotion, so a
        caller can read why a rule has not appeared yet (§6/§7). Returning early
        instead would leave that answer in no record at all.
        """
        if hypothesis.status not in {HypothesisStatus.SUPPORTED, HypothesisStatus.TESTABLE}:
            return None
        if not hypothesis.predicted:
            return None
        existing = self.book.rule_id_for(hypothesis.hypothesis_id, objective.scope)
        if existing in {rule.rule_id for rule in self.book.rules}:
            return None
        first = references[0].evidence_id if references else ""
        return self.book.promote(
            hypothesis,
            environment_id=environment.environment_id,
            action=spec.action,
            expected=hypothesis.predicted,
            statement=f"in {environment.environment_id!r}: {hypothesis.statement}",
            experiment_id=spec.experiment_id,
            evidence_id=first,
            source_key=first,
        )

    def _generalize(
        self,
        objective: LearningObjective,
        environment: Environment,
        spec: ExperimentSpec,
        run: ExperimentRun,
        hypothesis: Hypothesis,
    ) -> list[KnowledgeUpdate]:
        """A rule from ANOTHER scope confirmed by this environment's own observation.

        This is the only path to ``RuleStatus.CONFIRMED``, and it needs a real
        observation in the new scope: a rule is never promoted to "general" from the
        same environment that produced it (§10).
        """
        del hypothesis
        observation = run.observation
        if observation is None or not run.executed:
            return []
        out: list[KnowledgeUpdate] = []
        for rule in self.book.rules:
            if rule.objective_id == objective.objective_id and rule.scope == objective.scope:
                continue
            if rule.scope == environment.environment_id:
                continue
            expected = rule.predicts(spec.action)
            if not expected:
                continue
            kind = _matches(expected, observation, self.matching)
            if kind is OutcomeKind.CONFIRMED:
                out.append(
                    self.book.generalize(
                        rule.rule_id,
                        scope=environment.environment_id,
                        note=f"{spec.action!r} produced the expected outcome here too",
                    )
                )
            elif kind is OutcomeKind.CONTRADICTED:
                out.append(
                    self.book.contradict(
                        rule.rule_id,
                        experiment_id=spec.experiment_id,
                        reason=(
                            f"{spec.action!r} behaved differently in "
                            f"{environment.environment_id!r}, so the rule does not apply here"
                        ),
                    )
                )
        return out

    # ── restore, resume, persistence ─────────────────────────────────────

    def restore(self) -> dict[str, Any]:
        """Rebuild from the repository; an interrupted experiment is never completed.

        ONE bad record costs its own objective and nothing else. The store is
        hand-editable, and this runs at boot — before the application can answer
        anything — so a payload whose shape cannot be read is skipped and its reason
        is REPORTED (`unreadable`), never raised. A record whose `objective` is not a
        mapping used to take the whole restore (and therefore application startup)
        down with an ``AttributeError``.
        """
        report: dict[str, Any] = {
            "objectives": 0,
            "skipped": 0,
            "interrupted": 0,
            "unreadable": [],
        }
        if self.repository is None:
            return report
        for objective_id in self.repository.index():
            payload = self.repository.load(objective_id)
            if not payload:
                report["skipped"] += 1
                continue
            raw_objective = payload.get("objective")
            if not isinstance(raw_objective, Mapping):
                self._record_unreadable(
                    report, objective_id, "the stored record has no objective mapping"
                )
                continue
            try:
                objective = self._restore_objective(payload, raw_objective, report)
            except Exception as exc:  # noqa: BLE001 - an unreadable record is skipped
                self._record_unreadable(report, objective_id, f"{type(exc).__name__}: {exc}")
                continue
            if objective is None:
                self._record_unreadable(report, objective_id, "the record names no objective")
                continue
            self._objectives[objective.objective_id] = objective
            report["objectives"] += 1
        return report

    @staticmethod
    def _record_unreadable(report: dict[str, Any], objective_id: str, reason: str) -> None:
        """Count a skipped record and keep WHY it was skipped — silence is not a report."""
        report["skipped"] += 1
        rows = report.setdefault("unreadable", [])
        if isinstance(rows, list):
            rows.append({"objective_id": as_text(objective_id), "reason": reason})

    def _restore_objective(
        self,
        payload: Mapping[str, Any],
        raw_objective: Mapping[str, Any],
        report: dict[str, Any],
    ) -> LearningObjective | None:
        """Rebuild ONE objective from its payload — ``None`` when the record is unnamed."""
        objective = LearningObjective.from_mapping(raw_objective)
        if not objective.objective_id:
            return None
        restored_runs = [
            ExperimentRun.from_mapping(item)
            for item in rows_of(payload, "runs")
            if as_text(item.get("run_id"))
        ]
        unfinished = [item for item in restored_runs if item.status is ExperimentStatus.RUNNING]
        self._runs[objective.objective_id] = restored_runs
        self._steps[objective.objective_id] = []
        self._evidence.update(
            {
                item.evidence_id: item
                for item in (
                    EvidenceReference.from_mapping(raw) for raw in rows_of(payload, "evidence")
                )
                if item.evidence_id
            }
        )
        self.hypotheses.restore(
            tuple(
                Hypothesis.from_mapping(item)
                for item in rows_of(payload, "hypotheses")
                if as_text(item.get("hypothesis_id"))
            )
        )
        for spec in (
            ExperimentSpec.from_mapping(item)
            for item in rows_of(payload, "experiments")
            if as_text(item.get("experiment_id"))
        ):
            try:
                self.ledger.add(spec)
            except ValueError:  # a full ledger is not a restore failure
                report["skipped"] += 1
        # Rules are DURABLE knowledge, so they MERGE across payloads: a payload
        # written earlier may be read after one written later, and assigning the
        # last snapshot read would drop a rule another objective learned. A
        # terminal objective's record is restored in full for the same reason —
        # an achieved objective's evidence is exactly what a later question cites.
        stored_rules = payload.get("rules")
        if isinstance(stored_rules, Mapping) and stored_rules:
            self.book.absorb(RuleBook.from_mapping(stored_rules))
        if unfinished:
            for item in unfinished:
                self._mark_interrupted(item, "the process restarted while it was running")
            report["interrupted"] += 1
            objective = with_updates(
                objective,
                status=ObjectiveStatus.STOPPED,
                stop_reason=(
                    f"interrupted: {len(unfinished)} experiment(s) were running when the "
                    "process stopped; they are recorded as interrupted and were NOT resumed"
                ),
                updated_at=now_iso(),
            )
            for item in unfinished:
                self._announce(
                    "learning.experiment_interrupted",
                    {
                        "objective_id": objective.objective_id,
                        "experiment_id": item.experiment_id,
                        "reason": "the process restarted while it was running",
                    },
                    correlation_id=objective.correlation_id,
                )
        return objective

    def interrupted(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {"experiment_id": key, "reason": value}
            for key, value in sorted(self._interrupted.items())
        )

    def can_resume(self, objective_id: str) -> tuple[bool, str]:
        """Whether an interrupted objective may be re-evaluated safely (§13).

        Resuming is only allowed when the environment is a SIMULATOR whose recorded
        state still matches the state the interrupted run started from — that is the
        "the environment state and safety preconditions can be verified" condition.
        Anything else stays interrupted and needs a person, which is the honest answer
        for an action taken on the real world.
        """
        objective = self._require(objective_id)
        environment = self._resolve_environment(objective.environment_id)
        if environment is None:
            return False, f"environment {objective.environment_id!r} is no longer registered"
        if environment.kind is not EnvironmentKind.SIMULATOR:
            return False, (
                "the environment is not a simulator, so a restart cannot verify what its "
                "state now is; re-open the objective instead of resuming it"
            )
        rows = self._runs.get(objective.objective_id, [])
        interrupted_runs = [item for item in rows if item.status is ExperimentStatus.INTERRUPTED]
        if not interrupted_runs:
            return False, "no interrupted experiment was recorded for this objective"
        current = environment.state()
        for item in interrupted_runs:
            if item.before is None:
                return False, (
                    "the interrupted run did not record the state it started from, so it "
                    "cannot be verified as safe to re-run"
                )
            drifted = [
                key
                for key, value in item.before.values.items()
                if key in current and current[key] != value
            ]
            if drifted:
                return False, (
                    f"the environment's state has moved on (keys: {sorted(drifted)}), so "
                    "resuming would test a different situation"
                )
        return True, "the simulator's state matches the interrupted run's starting state"

    async def resume(
        self, objective_id: str, *, approval: bool = False, strategy: str = ""
    ) -> LearningCycleReport:
        """Re-run an interrupted objective when, and only when, that is verifiable."""
        objective = self._require(objective_id)
        ok, reason = self.can_resume(objective.objective_id)
        if not ok:
            return self._report(
                objective,
                stop_reason=f"not resumed: {reason}",
                steps=(),
                extra={"resumed": False, "reason": reason},
            )
        reopened = with_updates(
            objective,
            status=ObjectiveStatus.OPEN,
            stop_reason="",
            updated_at=now_iso(),
        )
        self._objectives[objective.objective_id] = reopened
        self._rejections.pop(f"cancel:{objective.objective_id}", None)
        return await self.run_cycle(objective.objective_id, approval=approval, strategy=strategy)

    def save(self) -> bool:
        """Persist every objective — the reference-only form (§13)."""
        if self.repository is None:
            return False
        ok = True
        for objective in self._objectives.values():
            ok = self.repository.remember(objective.objective_id) and ok
            ok = self.repository.save(objective.objective_id, self._payload(objective)) and ok
        return ok

    # ── metrics and status ───────────────────────────────────────────────

    def metrics(self) -> LearningMetrics:
        """Evaluate what happened — including the inconclusive results."""
        return self.evaluator.evaluate(
            objectives=self._objectives.values(),
            hypotheses=self.hypotheses.all(),
            experiments=self.ledger.all(),
            runs=self.all_runs(),
            rules=self.book.rules,
            duplicate=self.duplicates_avoided,
            rejections=self.safety_refusals,
            notes=tuple(
                f"{objective.objective_id}: {objective.status.value}"
                + (f" ({objective.stop_reason})" if objective.stop_reason else "")
                for objective in self._objectives.values()
            ),
        )

    def capability_report(self) -> dict[str, Any]:
        """The capability table with LIVE availability, plus the phase's own posture.

        The table itself lives in :mod:`novacontrol.interactive.capabilities` and is
        the SAME one registered on the application's capability registry: a second
        list here would be a second vocabulary for the same question, which is how a
        capability table starts to disagree with itself.
        """
        rows = capability_rows(self)
        implemented = [
            row for row in rows if row.state is CapabilityState.IMPLEMENTED and row.available
        ]
        return {
            "availability": "working" if implemented else "unavailable",
            "capabilities": [row.to_dict() for row in rows],
            "counts": {
                "capabilities": len(rows),
                "available": sum(1 for row in rows if row.available),
                "unavailable": sum(1 for row in rows if row.available is False),
                "unknown": sum(1 for row in rows if row.available is None),
            },
            "limitations": [
                "a simulator's result is evidence about the simulator, not about the real world",
                "no capability here loads, downloads or trains a model",
                "a capability listed as provider-dependent works only when its provider is wired",
            ],
        }

    def status(self) -> dict[str, Any]:
        """The operator view: what is registered, what is in force, what was done."""
        metrics = self.metrics()
        return {
            "enabled": True,
            "availability": self.capability_report()["availability"],
            "environments": self.environments.status(),
            "strategies": [
                "uncertainty_first",
                "information_value",
                "bounded_random",
            ],
            "policy": self.policy.to_dict(),
            "safety": self.gate.to_dict(),
            "thresholds": {
                "support": self.book.support_threshold,
                "contradiction": self.book.contradiction_threshold,
            },
            "ledger": self.ledger.counts(),
            "objectives": [item.to_dict() for item in self.objectives()],
            "rules": [item.to_dict() for item in self.book.rules],
            "knowledge_updates": len(self.book.updates),
            "evidence_references": len(self._evidence),
            "interrupted": list(self.interrupted()),
            "duplicates_avoided": self.duplicates_avoided,
            "reuse_count": self.reuse_count,
            "metrics": metrics.to_dict(),
            "provider_report": self._provider_report(),
            "capabilities": self.capability_report(),
            "limitations": [
                "expectations are heuristic rankings, not Bayesian information gain",
                "nothing here acts outside an environment a caller registered",
                "a rule learned in a simulator is provisional until a held-out case confirms it",
            ],
        }

    # ── internals ────────────────────────────────────────────────────────

    def _record_refusals(self, outcome: SelectionOutcome) -> None:
        """Keep the refused candidates in the ledger, with the reason each got.

        A refusal is a result: without this, an experiment the safety gate stopped
        before it ran would leave no trace of having been considered, and "what
        happened" would be answerable only from the cycle's stop reason (§4.3).
        """
        reasons = dict(outcome.rejected)
        for candidate in outcome.candidates:
            if not candidate.rejected:
                continue
            experiment_id = candidate.experiment.experiment_id
            if self.ledger.get(experiment_id) is not None:
                continue  # a completed run is never overwritten by a refusal
            stored = self.ledger.add(candidate.experiment)
            self.ledger.set_status(
                stored.experiment_id,
                ExperimentStatus.REJECTED,
                reason=reasons.get(experiment_id, candidate.reason),
            )

    def _book_signature(self, objective_id: str) -> tuple[tuple[str, str, int, int], ...]:
        """A comparable snapshot of the hypothesis book — what a run changed, if anything."""
        return tuple(
            (
                item.hypothesis_id,
                item.status.value,
                item.independent_support,
                item.independent_contradiction,
            )
            for item in self.hypotheses.for_objective(objective_id)
        )

    def _answered_for(self, objective_id: str) -> tuple[str, ...]:
        """Interventions that settled a hypothesis for this objective — the duplicates."""
        return tuple(sorted(self._answered.get(as_text(objective_id), set())))

    def _environment_for(self, environment: Environment) -> Environment:
        """Re-resolve a registered environment by id, or keep the one given."""
        return self._resolve_environment(environment.environment_id) or environment

    def _resolve_environment(self, environment_id: str) -> Environment | None:
        key = as_text(environment_id)
        if key:
            return self.environments.get(key)
        found = self.environments.all()
        return found[0] if found else None

    def _require(self, objective_id: str) -> LearningObjective:
        objective = self._objectives.get(as_text(objective_id))
        if objective is None:
            raise ValueError(f"no learning objective {objective_id!r} in this manager")
        return objective

    def _strategy(self, name: str) -> Any:
        from novacontrol.interactive.selection import UncertaintyFirstStrategy

        if not as_text(name):
            return self.selector.strategy
        found = strategy_by_name(name, seed=self._seed_for(name))
        if found is None:
            raise ValueError(f"unknown selection strategy {name!r}")
        return found or UncertaintyFirstStrategy()

    def _seed_for(self, name: str) -> int:
        """A reproducible seed for a strategy name — same name, same order, ANY run.

        ``hash()`` is salted per process, so a seed derived from it reorders a random
        strategy between runs and a "reproducible" comparison stops being one. CRC32 of
        the name is stable across processes and platforms.
        """
        return zlib.crc32(as_text(name).encode("utf-8")) & 0x7FFFFFFF

    def _observe(
        self, environment: Environment | None
    ) -> list[tuple[StepKind, str, dict[str, Any]]]:
        """The OBSERVE step's content, without awaiting anything."""
        if environment is None:
            return [
                (
                    StepKind.OBSERVE,
                    "no environment is registered, so there is nothing to observe",
                    {},
                )
            ]
        actions = [item.name for item in environment.actions()]
        return [
            (
                StepKind.OBSERVE,
                f"environment {environment.environment_id!r} ({environment.kind.value}) exposes "
                f"{len(actions)} action(s): {', '.join(actions) or 'none'}",
                {"actions": actions, "environment_kind": environment.kind.value},
            )
        ]

    def _propose(
        self, objective: LearningObjective, environment: Environment | None
    ) -> tuple[Hypothesis, ...]:
        """FORM HYPOTHESIS — from the environment's declared candidate space only."""
        if environment is None:
            return ()
        proposed = candidate_hypotheses(
            environment,
            objective_id=objective.objective_id,
            book=self.hypotheses,
            scope=objective.scope,
            exclude=tuple(
                item.statement for item in self.hypotheses.for_objective(objective.objective_id)
            ),
        )
        if proposed:
            self._append(
                objective,
                StepKind.FORM_HYPOTHESIS,
                f"{len(proposed)} candidate explanation(s) proposed from the environment's "
                "declared space",
                hypothesis_ids=[item.hypothesis_id for item in proposed],
            )
            self._announce(
                "learning.hypothesis_proposed",
                {
                    "objective_id": objective.objective_id,
                    "hypothesis_id": proposed[0].hypothesis_id,
                    "hypotheses": len(proposed),
                },
                correlation_id=objective.correlation_id,
            )
            for item in proposed:
                self.hypotheses.mark_testable(
                    item.hypothesis_id, reason="a distinguishable experiment exists for it"
                )
        else:
            self._append(
                objective,
                StepKind.FORM_HYPOTHESIS,
                "no candidate explanation was declared, so none was invented",
            )
        return proposed

    def _candidate_specs(
        self,
        objective: LearningObjective,
        environment: Environment | None,
        *,
        hypotheses: Sequence[Hypothesis] | None = None,
    ) -> tuple[ExperimentSpec, ...]:
        if environment is None:
            return ()
        attempt = len(self.ledger.for_objective(objective.objective_id)) + 1
        return candidate_experiments(
            objective,
            hypotheses
            if hypotheses is not None
            else self.hypotheses.testable_for_objective(objective.objective_id),
            environment,
            budget=objective.budget,
            matching=self.matching,
            policy=self.disposition,
            parameters_by_action=self._parameters(environment, attempt),
        )

    def _parameters(self, environment: Environment, attempt: int) -> dict[str, dict[str, Any]]:
        """The parameters each action is tried with, so a repeat is a DISTINCT experiment.

        Only an action that DECLARES the replicate parameter gets one: the phase must
        never silently re-run an identical intervention and count it twice, and it must
        not fabricate a parameter an environment did not offer.
        """
        out: dict[str, dict[str, Any]] = {}
        for spec in environment.actions():
            if REPLICATE_PARAMETER in spec.parameters:
                out[spec.name] = {REPLICATE_PARAMETER: attempt}
        return out

    def _context(
        self,
        objective: LearningObjective,
        environment: Environment | None,
        *,
        approval: bool = False,
    ) -> SelectionContext:
        tracker = BudgetTracker(objective.budget)
        tracker.restore(objective.usage)
        return SelectionContext(
            objective=objective,
            hypotheses=self.hypotheses.testable_for_objective(objective.objective_id),
            environment=environment,
            usage=tracker.usage,
            budget=objective.budget,
            matching=self.matching,
            policy=self.disposition,
            answered_interventions=self._answered_for(objective.objective_id),
            known_actions=tuple(
                item.experiment_id
                for item in self.ledger.for_objective(objective.objective_id)
                if item.status is ExperimentStatus.COMPLETED
            ),
            approval=approval,
            random_seed=self._seed_for("bounded_random"),
        )

    def _budget_verdict(self, tracker: BudgetTracker, spec: ExperimentSpec) -> Any:
        if spec.estimated_seconds > tracker.budget.max_seconds_per_experiment:
            from novacontrol.interactive.models import BudgetVerdict

            return BudgetVerdict(
                allowed=False,
                reason=(
                    f"the experiment estimates {spec.estimated_seconds:.2f}s, more than the "
                    f"per-experiment ceiling ({tracker.budget.max_seconds_per_experiment:.2f}s)"
                ),
                remaining={},
            )
        return tracker.verdict(spec)

    def _no_candidate_status(
        self, objective: LearningObjective, outcome: SelectionOutcome
    ) -> tuple[ObjectiveStatus, str]:
        reasons = [reason for _, reason in outcome.rejected]
        duplicate = [
            reason for reason in reasons if reason.startswith(DUPLICATE_INTERVENTION_REASON)
        ]
        if duplicate:
            self.duplicates_avoided += len(duplicate)
            supported = [
                item
                for item in self.hypotheses.for_objective(objective.objective_id)
                if item.status is HypothesisStatus.SUPPORTED
            ]
            if supported:
                return ObjectiveStatus.ACHIEVED, (
                    f"the question is already answered by evidence that is still current "
                    f"({len(duplicate)} duplicate intervention(s) avoided): "
                    f"{supported[0].statement}; the action declares no replicate, so no "
                    "further independent observation could be made here"
                )
            return ObjectiveStatus.UNJUSTIFIED, (
                f"every candidate for this objective has already been answered by evidence "
                f"that is still current ({len(duplicate)} duplicate(s) avoided), so nothing "
                "was re-run"
            )
        budget_like = [text for text in reasons if "ceiling" in text or "budget" in text]
        if budget_like:
            return ObjectiveStatus.EXHAUSTED, budget_like[0]
        # Which refusals came from the SAFETY GATE, and which layer decided — read
        # from the gate's own decisions, never from a list of phrases. Matching the
        # prose here used to send an un-approved real action to UNJUSTIFIED (the
        # gate's wording had drifted away from the phrases this function looked
        # for), which also hid the refusal from the safety-refusal count and from
        # the `learning.experiment_rejected` announcement.
        gate_refusals = tuple(
            decision
            for item in outcome.candidates
            if (decision := item.safety) is not None and not decision.allowed
        )
        # A refusal a person CANNOT approve away (destructive by policy, or no
        # permission layer wired) stops the objective; a missing approval does not,
        # because the next thing that happens is somebody approving it.
        unevictable = tuple(
            item.reason for item in gate_refusals if item.source in {"policy", "permissions"}
        )
        pending = tuple(item.reason for item in gate_refusals if item.source == "approval")
        if unevictable or pending:
            self.safety_refusals += 1
            self._announce(
                "learning.experiment_rejected",
                {
                    "objective_id": objective.objective_id,
                    "experiment_id": "",
                    "reason": (unevictable or pending)[0],
                },
                correlation_id=objective.correlation_id,
            )
        if unevictable:
            return ObjectiveStatus.STOPPED, (
                f"no experiment could be proposed that the safety policy allows: {unevictable[0]}"
            )
        if pending:
            return ObjectiveStatus.OPEN, (
                "nothing was attempted: "
                f"{pending[0]} — approve this objective and run it again; one approval "
                "authorizes one cycle"
            )
        if not outcome.considered:
            return ObjectiveStatus.UNJUSTIFIED, (
                "no candidate experiment could be built: no testable hypothesis has a "
                "prediction, or the environment exposes no action"
            )
        return ObjectiveStatus.UNJUSTIFIED, (
            "no candidate experiment was eligible: "
            + "; ".join(reasons[:3] or ["the selector refused every candidate"])
        )

    def _covering_rule(
        self, objective: LearningObjective, environment: Environment | None
    ) -> LearnedRule | None:
        """A rule that already answers this objective's action in this scope."""
        if environment is None:
            return None
        action = self._action_of(objective, environment)
        if not action:
            return None
        scope = objective.scope or environment.environment_id
        for rule in self.book.rules:
            if rule.status.value == "retired":
                continue
            if not rule.applies_to(scope):
                continue
            if rule.predicts(action):
                return rule
        return None

    def _action_of(self, objective: LearningObjective, environment: Environment) -> str:
        """The single action this objective is about, when the environment has one."""
        actions = [item.name for item in environment.actions() if not item.destructive]
        if len(actions) == 1:
            return actions[0]
        for rule in self.book.rules:
            for action in actions:
                if rule.predicts(action):
                    return action
        return ""

    def _prediction_service(self, environment: Environment) -> PredictionService:
        lookup: RuleLookup = self._lookup
        return PredictionService(
            providers=(
                EnvironmentRuleProvider(environment),
                LearnedRuleProvider(lookup),
                WorldModelProvider(self.world_bridge),
                NoPredictionProvider(),
            )
        )

    def _lookup(self, action: str, scope: str) -> Mapping[str, Any] | None:
        """Rules that cover an action in a scope — where a learned rule is READ."""
        for rule in self.book.rules:
            if rule.status.value == "retired":
                continue
            if scope and not rule.applies_to(scope):
                continue
            outcome = rule.predicts(action)
            if outcome:
                return outcome
        return None

    def _provider_report(self) -> list[dict[str, Any]]:
        service = PredictionService(
            providers=(
                LearnedRuleProvider(self._lookup),
                WorldModelProvider(self.world_bridge),
                NoPredictionProvider(),
            )
        )
        return [dict(row) for row in service.provider_report()]

    def _world_reader(self) -> Any:
        for item in self.environments.all():
            if item.kind is EnvironmentKind.WORLD and bool(item.status().get("reader_wired")):
                return item
        return None

    def _terminal(
        self,
        objective: LearningObjective,
        tracker: BudgetTracker,
        run: ExperimentRun,
        uninformative: int,
        limit: int,
    ) -> tuple[ObjectiveStatus, str, bool]:
        """The stop conditions, in the order that matters (§11/§1).

        Returns ``(status, reason, stop)``. The third element is the cycle's own
        verdict about whether to keep going, and it has to be separate from the
        status: the bound branch below reports ``OPEN`` on purpose (the objective is
        NOT settled), and a loop that read ``OPEN`` as "keep going" — as this one
        did — silently ran past ``max_experiments`` and spent a budget it had been
        told not to. One owner for every stop decision, and the loop obeys it.
        """
        if self._rejection(objective.objective_id):
            return ObjectiveStatus.STOPPED, self._rejection(objective.objective_id), True
        if run.status is ExperimentStatus.BLOCKED:
            return (
                ObjectiveStatus.STOPPED,
                f"the environment refused the action: {run.reason or run.error}",
                True,
            )
        if run.status is ExperimentStatus.TIMED_OUT:
            return (
                ObjectiveStatus.STOPPED,
                "the experiment exceeded its own time limit and was stopped; a timeout is not "
                "a result",
                True,
            )
        exhausted, why = tracker.exhausted()
        if exhausted:
            return ObjectiveStatus.EXHAUSTED, why, True
        supporting = [
            item
            for item in self.hypotheses.for_objective(objective.objective_id)
            if item.status is HypothesisStatus.SUPPORTED
        ]
        if (
            supporting
            and self._covering_rule(objective, self._resolve_environment(objective.environment_id))
            is not None
        ):
            rule = self._covering_rule(
                objective, self._resolve_environment(objective.environment_id)
            )
            assert rule is not None
            return (
                ObjectiveStatus.ACHIEVED,
                (
                    f"the question was answered and a scoped rule ({rule.rule_id}, "
                    f"{rule.status.value}) was learned from {rule.independent_support} "
                    "independent observation(s); that rule is what a later objective in this "
                    "scope reuses"
                ),
                True,
            )
        if not self.hypotheses.testable_for_objective(objective.objective_id):
            if supporting:
                return (
                    ObjectiveStatus.ACHIEVED,
                    (
                        f"the question was answered: {supporting[0].statement} (independent "
                        f"support {supporting[0].independent_support})"
                    ),
                    True,
                )
            return (
                ObjectiveStatus.UNJUSTIFIED,
                "every hypothesis was resolved without a supported one, so the question was "
                "answered in the negative rather than left open",
                True,
            )
        if uninformative >= max(1, self.policy.max_uninformative_in_a_row):
            return (
                ObjectiveStatus.UNJUSTIFIED,
                (
                    f"{uninformative} consecutive experiment(s) produced no verdict that "
                    "changed anything, so further experimentation is not justified"
                ),
                True,
            )
        if tracker.usage.experiments >= limit:
            # OPEN, but STOP: the objective stays runnable and is not reported as
            # settled, while the cycle that was given this bound spends no more.
            return (
                ObjectiveStatus.OPEN,
                (
                    f"the cycle's own bound was reached ({limit} experiment(s)); the objective "
                    "remains open rather than being reported as settled"
                ),
                True,
            )
        return ObjectiveStatus.OPEN, "", False

    def _evaluation_detail(self, objective: LearningObjective, run: ExperimentRun) -> str:
        if run.comparison is None:
            return (
                f"{run.status.value} after {run.actions} action(s) "
                f"({run.error or 'no comparison was possible'})"
            )
        return (
            f"{run.status.value}: prediction {run.comparison.kind.value} "
            f"({run.comparison.detail or 'no detail'})" + (" [simulator]" if run.simulated else "")
        )

    def _finish(
        self,
        objective: LearningObjective,
        tracker: BudgetTracker,
        steps: list[LearningCycleStep],
        status: ObjectiveStatus,
        stop_reason: str,
        *,
        environment: Environment | None,
        run: ExperimentRun | None = None,
        comparison: Any | None = None,
        updates: tuple[KnowledgeUpdate, ...] = (),
        extra: Mapping[str, Any] | None = None,
    ) -> LearningCycleReport:
        rules = self.book.for_objective(objective.objective_id)
        updated = with_updates(
            objective,
            status=status,
            usage=tracker.usage,
            hypotheses=tuple(
                item.hypothesis_id for item in self.hypotheses.for_objective(objective.objective_id)
            ),
            experiments=tuple(
                item.experiment_id for item in self.ledger.for_objective(objective.objective_id)
            ),
            rules=tuple(item.rule_id for item in rules),
            stop_reason=stop_reason,
            updated_at=now_iso(),
        )
        self._objectives[objective.objective_id] = updated
        self._persist(objective.objective_id)
        report = self._report(
            updated,
            stop_reason=stop_reason,
            steps=tuple(steps),
            run=run,
            comparison=comparison,
            updates=updates,
            environment=environment,
            extra={
                "usage": tracker.usage.to_dict(),
                "rules": [item.rule_id for item in rules],
                **(dict(extra) if extra else {}),
            },
        )
        return report

    def _report(
        self,
        objective: LearningObjective,
        *,
        stop_reason: str,
        steps: tuple[LearningCycleStep, ...],
        run: ExperimentRun | None = None,
        comparison: Any | None = None,
        updates: tuple[KnowledgeUpdate, ...] = (),
        environment: Environment | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> LearningCycleReport:
        resolved_environment = environment or self._resolve_environment(objective.environment_id)
        kind = (
            resolved_environment.kind
            if resolved_environment is not None
            else EnvironmentKind.SIMULATOR
        )
        metrics = self.metrics().to_dict()
        return LearningCycleReport(
            objective_id=objective.objective_id,
            status=objective.status,
            environment_id=objective.environment_id,
            environment_kind=kind,
            simulated=kind is EnvironmentKind.SIMULATOR,
            steps=steps,
            experiment_id=run.experiment_id if run is not None else "",
            run=run,
            comparison=comparison,
            updates=updates,
            rules=tuple(item.rule_id for item in self.book.for_objective(objective.objective_id)),
            stop_reason=stop_reason,
            metrics={**metrics, **(dict(extra) if extra else {})},
            hypotheses=self.hypotheses.for_objective(objective.objective_id),
            limitations=tuple(self.policy.limitations)
            + (
                "this build's environments are simulators: the result is evidence about the "
                "simulator, not about the real world",
            ),
            correlation_id=objective.correlation_id,
        )

    def _append(
        self, objective: LearningObjective, kind: StepKind, detail: str, **data: Any
    ) -> None:
        rows = self._steps.setdefault(objective.objective_id, [])
        rows.append(
            LearningCycleStep(
                kind=kind,
                detail=detail,
                index=len(rows) + 1,
                timestamp=now_iso(),
                data={key: value for key, value in data.items() if value not in (None, "", [])},
            )
        )

    def _announce(
        self, event_type: str, payload: Mapping[str, Any], *, correlation_id: str = ""
    ) -> None:
        """Announce through the injected seam; a seam that raises never breaks the cycle."""
        if self.observer is None:
            return
        try:
            self.observer(event_type, {**dict(payload), "correlation_id": correlation_id})
        except Exception:  # noqa: BLE001 - an observer is not allowed to fail the work
            return

    def _payload(self, objective: LearningObjective) -> dict[str, Any]:
        return {
            "schema": "phase23.1",
            "objective": objective.to_dict(),
            "hypotheses": [
                item.to_dict() for item in self.hypotheses.for_objective(objective.objective_id)
            ],
            "experiments": [
                item.to_dict() for item in self.ledger.for_objective(objective.objective_id)
            ],
            "runs": [item.to_dict() for item in self._runs.get(objective.objective_id, [])][-32:],
            "evidence": [
                item.to_dict()
                for item in self._evidence.values()
                if item.experiment_id
                in {row.experiment_id for row in self.ledger.for_objective(objective.objective_id)}
            ][-128:],
            "rules": self.book.to_dict(),
        }

    def _persist(self, objective_id: str) -> bool:
        if self.repository is None:
            return False
        objective = self._objectives.get(as_text(objective_id))
        if objective is None:
            return False
        self.repository.remember(objective.objective_id)
        return self.repository.save(objective.objective_id, self._payload(objective))

    def _mark_interrupted(self, run: ExperimentRun, reason: str) -> None:
        updated = with_updates(run, status=ExperimentStatus.INTERRUPTED, error=reason)
        rows = self._runs.get(run.objective_id, [])
        self._runs[run.objective_id] = [
            updated if item.run_id == run.run_id else item for item in rows
        ]
        self._interrupted[run.experiment_id] = reason
        try:
            self.ledger.set_status(run.experiment_id, ExperimentStatus.INTERRUPTED, reason=reason)
        except KeyError:
            return

    def _consume_approval(self, objective_id: str) -> bool:
        """Single use: an approval is spent by the cycle it authorized."""
        return self._approvals.pop(as_text(objective_id), None) is not None

    def _rejection(self, objective_id: str) -> str:
        return self._rejections.get(as_text(objective_id), "") or self._rejections.get(
            f"cancel:{as_text(objective_id)}", ""
        )


def _matches(
    expected: Mapping[str, Any], observation: EnvironmentObservation, matching: MatchingPolicy
) -> OutcomeKind:
    """Whether an observation agrees with an expected mapping — for generalization."""
    if not expected or not observation.values:
        return OutcomeKind.INCONCLUSIVE
    if observation.conflicting:
        return OutcomeKind.INCONCLUSIVE
    missing = [key for key in expected if key not in observation.values]
    if missing and not observation.complete:
        return OutcomeKind.INCONCLUSIVE
    agreed = 0
    for key, wanted in expected.items():
        if key not in observation.values:
            return OutcomeKind.INCONCLUSIVE
        actual = observation.values[key]
        tolerance = matching.tolerance_for(key)
        if isinstance(wanted, (int, float)) and isinstance(actual, (int, float)):
            ok = abs(float(wanted) - float(actual)) <= tolerance
        else:
            ok = wanted == actual
        if ok:
            agreed += 1
    if agreed == len(expected):
        return OutcomeKind.CONFIRMED
    return OutcomeKind.CONTRADICTED


def rows_of(payload: Mapping[str, Any], key: str) -> tuple[Mapping[str, Any], ...]:
    """The mapping rows under a stored key, or none — a corrupt list is skipped, not fatal."""
    raw = payload.get(key)
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def _observation_from(values: Mapping[str, Any]) -> EnvironmentObservation:
    """A caller-reported outcome read as an observation, so ONE comparer judges both."""
    return EnvironmentObservation(
        observation_ref="",
        values=dict(values),
        complete=True,
        timestamp=now_iso(),
        source="held_out_case",
        note="reported by the caller as the outcome of a held-out case",
    )


def usage_of(objective: LearningObjective) -> BudgetUsage:
    """The objective's recorded spend — a read-only convenience."""
    return objective.usage


def knowledge_changes(updates: Sequence[KnowledgeUpdate]) -> dict[str, int]:
    """How many updates of each kind a cycle produced."""
    table: dict[str, int] = {}
    for update in updates:
        key = update.change.value
        table[key] = table.get(key, 0) + 1
    return table


def _unused() -> tuple[type[Any], type[Any]]:  # pragma: no cover - keeps imports honest
    return KnowledgeChange, EvidenceKind
