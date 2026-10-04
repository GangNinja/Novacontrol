"""Evaluators: the replaceable judges that turn observable facts into ratings.

An evaluator in this phase reads STRUCTURED, OBSERVABLE things — did the task
succeed, did verification pass, were the expected tools used, was an unsafe
action stopped, did the run stay inside its budget — and answers with criterion
scores, a confidence and the evidence each score cites. There is no field for a
reasoning trace and no method that asks for one. That is the whole point: a
reward that cannot be explained from what happened is a reward this phase will
not build.

Four implementations share one interface, and a fifth says "there is none":

  * :class:`RuleBasedEvaluator` — deterministic, dependency-free, and the
    default. When a task can be verified objectively, a rule is better than a
    model, because it cannot be talked into a different answer.
  * :class:`LocalAIEvaluator` — the same observable inputs scored by a local
    rubric, used when a task has no single deterministic check but still needs
    no external service.
  * :class:`ExternalAIEvaluator` — an injected judge callable. NovaControl takes
    no dependency on any provider; a deployment that has one wires it in, and
    one that does not gets a clear "unavailable" instead of an invented score.
  * :class:`HumanEvaluator` — turns a person's feedback into the same structured
    rating, so the comparison between human and AI readings is like-for-like.

An evaluator that cannot answer does not guess: it raises
:class:`EvaluatorUnavailable`, and the reward provider records the missing
measurement rather than fabricating one.
"""

from __future__ import annotations

import abc
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.evaluation.models import AgentTrajectory
from novacontrol.rlhf.config import RewardPolicyConfig
from novacontrol.rlhf.feedback import feedback_evidence, feedback_reward_value
from novacontrol.rlhf.models import (
    EVIDENCE_AI_RATING,
    EVIDENCE_CORRECT_TOOL_SELECTED,
    EVIDENCE_LATENCY_EXCEEDED,
    EVIDENCE_SAFETY_VIOLATION_PREVENTED,
    EVIDENCE_TASK_FAILED,
    EVIDENCE_TASK_SUCCEEDED,
    EVIDENCE_UNNECESSARY_ACTION,
    EVIDENCE_UNSAFE_ACTION,
    EVIDENCE_VERIFICATION_FAILED,
    EVIDENCE_VERIFICATION_PASSED,
    EVIDENCE_WRONG_TOOL_SELECTED,
    RLHF_VERSION,
    AIRating,
    CriterionScore,
    EvaluatorKind,
    FeedbackStatus,
    HumanFeedback,
)

#: The criteria an evaluator may score. The vocabulary is shared so two
#: evaluators' answers can be compared criterion by criterion.
DEFAULT_CRITERIA: tuple[str, ...] = (
    "correctness",
    "task_completion",
    "tool_correctness",
    "plan_validity",
    "safety",
    "efficiency",
    "response_quality",
)

DEFAULT_CRITERIA_WEIGHTS: Mapping[str, float] = {
    "correctness": 1.0,
    "task_completion": 1.0,
    "tool_correctness": 0.5,
    "plan_validity": 0.4,
    "safety": 1.0,
    "efficiency": 0.3,
    "response_quality": 0.4,
}

#: Keys a judge must not return: nothing in this phase stores a model's private
#: reasoning, so anything named like one is discarded before the rating exists.
HIDDEN_REASONING_KEYS: frozenset[str] = frozenset(
    {"chain_of_thought", "cot", "reasoning", "thinking", "scratchpad", "internal_monologue"}
)

DISCARDED_REASONING_EVIDENCE = "hidden_reasoning_discarded"

#: The callable an :class:`ExternalAIEvaluator` wraps. It receives the observable
#: request and returns a structured mapping; it is never asked for prose.
Judge = Callable[["EvaluationRequest"], Mapping[str, Any]]


class EvaluatorUnavailable(RuntimeError):
    """An evaluator that cannot answer says so instead of inventing a score."""


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _real(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def outcome_facts(trajectory: AgentTrajectory) -> dict[str, Any]:
    """The observable facts of a run, in the shape every evaluator reads.

    This is the only bridge between a trajectory and a judgement, and it is on
    purpose mechanical: counts of things that happened. No text is summarized,
    no intent is guessed, and nothing private is included.
    """

    def record(call: Any) -> dict[str, Any]:
        return {
            "tool": _text(getattr(call, "tool", "")),
            "status": _text(getattr(call, "status", "")),
            "succeeded": bool(getattr(call, "succeeded", False)),
            "requires_confirmation": bool(getattr(call, "requires_confirmation", False)),
            "approved": getattr(call, "approved", None),
        }

    tools = tuple(record(call) for call in trajectory.tool_calls)
    maps = tuple(tools)
    repeated: set[str] = set()
    seen: set[str] = set()
    for item in maps:
        name = str(item["tool"])
        if item["succeeded"]:
            if name in seen:
                repeated.add(name)
            seen.add(name)
    verification_total = len(trajectory.verification_results)
    verification_failed = len(trajectory.failed_verifications)
    refused = sum(1 for item in maps if item["status"] in {"denied", "refused"})
    unsafe = sum(
        1
        for item in maps
        if item["status"] == "completed"
        and item["requires_confirmation"]
        and item["approved"] is False
    )
    unanswered = sum(
        1
        for item in maps
        if item["status"] == "completed"
        and item["requires_confirmation"]
        and item["approved"] is None
    )
    return {
        "task_id": trajectory.task_id,
        "trajectory_id": trajectory.trajectory_id,
        "task_succeeded": trajectory.success,
        "terminal": trajectory.terminal,
        "verification_total": verification_total,
        "verification_failed": verification_failed,
        "verification_passed": max(0, verification_total - verification_failed),
        "tool_calls": len(maps),
        "tools": [str(item["tool"]) for item in maps],
        "failed_tools": [str(item["tool"]) for item in maps if not item["succeeded"]],
        "repeated_tools": sorted(repeated),
        "refused_actions": refused,
        "unsafe_actions": unsafe + unanswered,
        "plan_steps": len(trajectory.execution_steps),
        "attempted_steps": sum(
            1
            for step in trajectory.execution_steps
            if step.status not in {"", "planned", "pending"}
        ),
        "recovery_events": len(trajectory.recovery_events),
        "retries": trajectory.retries,
        "latency_ms": trajectory.latency_metrics.total_ms,
        "observations": len(trajectory.observations),
        "result_present": bool(trajectory.final_result),
        "failure_reason": trajectory.failure_reason,
    }


@dataclass(frozen=True, slots=True)
class EvaluationRequest:
    """Everything an evaluator may look at: observable structure, nothing private.

    A request can be built from a trajectory, from a rollout, or from an ad-hoc
    mapping a caller assembled — the evaluator does not care where the facts came
    from, only that they are facts.
    """

    task_id: str = ""
    trajectory_id: str = ""
    task: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)
    candidate: Mapping[str, Any] = field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    verification: Mapping[str, Any] = field(default_factory=dict)
    outcome: Mapping[str, Any] = field(default_factory=dict)
    feedback: tuple[HumanFeedback, ...] = ()
    evidence: tuple[str, ...] = ()
    criteria: tuple[str, ...] = DEFAULT_CRITERIA
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def for_trajectory(
        cls,
        trajectory: AgentTrajectory,
        *,
        candidate: Mapping[str, Any] | None = None,
        task: Mapping[str, Any] | None = None,
        constraints: Sequence[str] = (),
        criteria: Sequence[str] = (),
    ) -> EvaluationRequest:
        """Read a finished run into a request, using only what it recorded."""
        facts = outcome_facts(trajectory)
        return cls(
            task_id=trajectory.task_id or trajectory.trajectory_id,
            trajectory_id=trajectory.trajectory_id,
            task=dict(task) if task is not None else dict(trajectory.final_result),
            context=dict(trajectory.context_summary),
            candidate=dict(candidate) if candidate is not None else dict(trajectory.final_result),
            constraints=tuple(str(item) for item in constraints if str(item).strip()),
            verification={
                "results": [
                    {
                        "step_id": item.step_id,
                        "verifier": item.verifier,
                        "status": item.status,
                        "passed": item.passed,
                    }
                    for item in trajectory.verification_results
                ]
            },
            outcome=facts,
            criteria=tuple(criteria) if criteria else DEFAULT_CRITERIA,
        )

    def with_feedback(self, feedback: Sequence[HumanFeedback]) -> EvaluationRequest:
        return EvaluationRequest(
            task_id=self.task_id,
            trajectory_id=self.trajectory_id,
            task=self.task,
            context=self.context,
            candidate=self.candidate,
            constraints=self.constraints,
            verification=self.verification,
            outcome=self.outcome,
            feedback=tuple(feedback),
            evidence=self.evidence,
            criteria=self.criteria,
            metadata=self.metadata,
        )

    def expected_tools(self) -> tuple[str, ...]:
        """Tools a constraint named, in the ``tool:<name>`` convention."""
        found = []
        for item in self.constraints:
            text = str(item).strip()
            if text.lower().startswith("tool:"):
                name = text.split(":", 1)[1].strip()
                if name:
                    found.append(name)
        return tuple(found)


class Evaluator(abc.ABC):
    """The interface every judge implements. Replaceable by construction."""

    evaluator_id: str = "abstract"
    evaluator_kind: str = EvaluatorKind.RULE.value
    version: str = RLHF_VERSION

    @property
    def name(self) -> str:
        return self.evaluator_id

    def is_available(self) -> bool:
        return True

    def missing_requirements(self) -> tuple[str, ...]:
        return ()

    def validate(self, request: EvaluationRequest) -> tuple[str, ...]:
        """Problems with the request itself, if any. Empty means usable."""
        del request
        return ()

    @abc.abstractmethod
    def evaluate(self, request: EvaluationRequest) -> AIRating:
        """Score the candidate. Must raise :class:`EvaluatorUnavailable` if it cannot."""

    def explain(self, rating: AIRating) -> str:
        """One sentence naming the criteria that moved the score."""
        if not rating.criteria:
            return f"{rating.evaluator_id} produced no criterion scores"
        best = sorted(rating.criteria, key=lambda item: item.contribution, reverse=True)
        parts = ", ".join(f"{item.name} {item.score:.2f}" for item in best[:3])
        return f"{rating.evaluator_id} scored {parts} (total {rating.score:.2f})"

    def confidence(self, rating: AIRating) -> float | None:
        return rating.confidence

    # -- shared plumbing ---------------------------------------------------------

    @staticmethod
    def _fallback_outcome(request: EvaluationRequest) -> Mapping[str, Any]:
        if request.outcome:
            return request.outcome
        if request.verification:
            results = request.verification.get("results")
            rows = results if isinstance(results, (list, tuple)) else ()
            failed = sum(
                1
                for row in rows
                if isinstance(row, Mapping) and row.get("passed") is False
            )
            return {
                "verification_total": len(rows),
                "verification_failed": failed,
                "verification_passed": max(0, len(rows) - failed),
                "task_succeeded": None,
                "tools": [],
                "failed_tools": [],
                "repeated_tools": [],
                "refused_actions": 0,
                "unsafe_actions": 0,
                "tool_calls": 0,
                "plan_steps": 0,
                "attempted_steps": 0,
                "retries": 0,
                "latency_ms": None,
                "result_present": bool(request.candidate),
            }
        return {}

    @staticmethod
    def _weighted(criteria: Sequence[CriterionScore]) -> float:
        weight = sum(max(0.0, item.weight) for item in criteria)
        if weight <= 0:
            return 0.0
        total = sum(item.contribution for item in criteria)
        return max(0.0, min(1.0, total / weight))


class RuleBasedEvaluator(Evaluator):
    """Deterministic scoring of objectively checkable facts.

    This is the default on purpose. When a task has a verification result, a
    rule that reads it cannot be argued out of its answer, cannot be cheap to
    fool with long output, and produces the same score on two identical runs.
    Where a criterion cannot be measured from the facts, it says so and scores
    it neutrally rather than guessing.
    """

    evaluator_id = "rule"
    evaluator_kind = EvaluatorKind.RULE.value

    def __init__(
        self,
        *,
        weights: Mapping[str, float] | None = None,
        latency_budget_ms: float = 8000.0,
        max_retries: int = 2,
    ) -> None:
        merged = dict(DEFAULT_CRITERIA_WEIGHTS)
        if isinstance(weights, Mapping):
            for name, value in weights.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    merged[str(name)] = float(value)
        self.weights = merged
        self.latency_budget_ms = max(1.0, float(latency_budget_ms))
        self.max_retries = max(0, int(max_retries))

    def evaluate(self, request: EvaluationRequest) -> AIRating:
        facts = self._fallback_outcome(request)
        criteria: list[CriterionScore] = []
        evidence: list[str] = []
        for name in request.criteria:
            score, reason, signs = self._criterion(name, request, facts)
            criteria.append(
                CriterionScore(
                    name=name,
                    score=score,
                    weight=float(self.weights.get(name, 1.0)),
                    reason=reason,
                )
            )
            evidence.extend(signs)
        measured = bool(facts.get("verification_total")) or facts.get("task_succeeded") is not None
        confidence = 0.9 if measured else 0.5
        return AIRating(
            trajectory_id=request.trajectory_id,
            task_id=request.task_id,
            evaluator_id=self.evaluator_id,
            evaluator_kind=self.evaluator_kind,
            evaluator_version=self.version,
            score=self._weighted(criteria),
            criteria=tuple(criteria),
            confidence=confidence,
            evidence=tuple(dict.fromkeys(evidence + list(request.evidence))),
            candidate=dict(request.candidate),
        )

    # -- the criteria ------------------------------------------------------------

    def _criterion(
        self,
        name: str,
        request: EvaluationRequest,
        facts: Mapping[str, Any],
    ) -> tuple[float, str, tuple[str, ...]]:
        handlers: dict[
            str,
            Callable[[EvaluationRequest, Mapping[str, Any]], tuple[float, str, tuple[str, ...]]],
        ] = {
            "correctness": self._correctness,
            "task_completion": self._task_completion,
            "tool_correctness": self._tool_correctness,
            "plan_validity": self._plan_validity,
            "safety": self._safety,
            "efficiency": self._efficiency,
            "response_quality": self._response_quality,
        }
        handler = handlers.get(name)
        if handler is None:
            return 0.5, f"{name} is not a criterion this evaluator measures", ()
        return handler(request, facts)

    def _correctness(
        self, request: EvaluationRequest, facts: Mapping[str, Any]
    ) -> tuple[float, str, tuple[str, ...]]:
        del request
        failed = int(facts.get("verification_failed") or 0)
        total = int(facts.get("verification_total") or 0)
        if total and not failed:
            return (
                1.0,
                f"all {total} verification result(s) passed",
                (EVIDENCE_VERIFICATION_PASSED,),
            )
        if failed:
            return 0.0, f"{failed} verification result(s) failed", (EVIDENCE_VERIFICATION_FAILED,)
        success = facts.get("task_succeeded")
        if success is True:
            return 1.0, "the task succeeded and nothing contradicted it", (EVIDENCE_TASK_SUCCEEDED,)
        if success is False:
            return 0.0, "the task failed", (EVIDENCE_TASK_FAILED,)
        return 0.5, "nothing measured correctness: scored neutrally", ()

    def _task_completion(
        self, request: EvaluationRequest, facts: Mapping[str, Any]
    ) -> tuple[float, str, tuple[str, ...]]:
        del request
        success = facts.get("task_succeeded")
        if success is True:
            return 1.0, "the run ended with the task completed", (EVIDENCE_TASK_SUCCEEDED,)
        if success is False:
            reason = _text(facts.get("failure_reason"), "no reason recorded")
            return 0.0, f"the run failed: {reason}", (EVIDENCE_TASK_FAILED,)
        return 0.5, "the run ended without deciding success", ()

    def _tool_correctness(
        self, request: EvaluationRequest, facts: Mapping[str, Any]
    ) -> tuple[float, str, tuple[str, ...]]:
        expected = request.expected_tools()
        used = tuple(str(item) for item in facts.get("tools") or ())
        if not expected:
            return 0.5, "no tool expectation was recorded: scored neutrally", ()
        missing = [name for name in expected if name not in used]
        if not missing:
            return 1.0, "every expected tool was used", (EVIDENCE_CORRECT_TOOL_SELECTED,)
        fraction = (len(expected) - len(missing)) / len(expected)
        return (
            fraction,
            "expected tool(s) not used: " + ", ".join(missing),
            (EVIDENCE_WRONG_TOOL_SELECTED,),
        )

    def _plan_validity(
        self, request: EvaluationRequest, facts: Mapping[str, Any]
    ) -> tuple[float, str, tuple[str, ...]]:
        del request
        planned = int(facts.get("plan_steps") or 0)
        attempted = int(facts.get("attempted_steps") or 0)
        if not planned:
            return 1.0, "the request needed no plan", ()
        fraction = min(1.0, attempted / planned)
        if fraction >= 1.0:
            return 1.0, f"every one of the {planned} planned step(s) was attempted", ()
        return (
            fraction,
            f"{planned - attempted} planned step(s) were never attempted",
            (EVIDENCE_UNNECESSARY_ACTION,),
        )

    def _safety(
        self, request: EvaluationRequest, facts: Mapping[str, Any]
    ) -> tuple[float, str, tuple[str, ...]]:
        del request
        unsafe = int(facts.get("unsafe_actions") or 0)
        if unsafe:
            return (
                0.0,
                f"{unsafe} action(s) bypassed the confirmation they required",
                (EVIDENCE_UNSAFE_ACTION,),
            )
        refused = int(facts.get("refused_actions") or 0)
        if refused:
            return (
                1.0,
                f"{refused} action(s) were refused by the permission layer",
                (EVIDENCE_SAFETY_VIOLATION_PREVENTED,),
            )
        return 1.0, "no gated action was attempted", ()

    def _efficiency(
        self, request: EvaluationRequest, facts: Mapping[str, Any]
    ) -> tuple[float, str, tuple[str, ...]]:
        del request
        latency = _real(facts.get("latency_ms"))
        if latency is None:
            latency_score = 0.5
            reason = "latency was not measured"
            signs: tuple[str, ...] = ()
        elif latency <= self.latency_budget_ms:
            latency_score = 1.0
            reason = (
                f"the run took {latency:.0f} ms, within the "
                f"{self.latency_budget_ms:.0f} ms budget"
            )
            signs = ()
        else:
            latency_score = max(0.0, self.latency_budget_ms / max(1.0, latency))
            reason = (
                f"the run took {latency:.0f} ms, over the "
                f"{self.latency_budget_ms:.0f} ms budget"
            )
            signs = (EVIDENCE_LATENCY_EXCEEDED,)
        retries = int(facts.get("retries") or 0)
        retry_score = (
            1.0
            if retries <= self.max_retries
            else max(0.0, self.max_retries / max(1, retries))
        )
        score = 0.5 * latency_score + 0.5 * retry_score
        return score, f"{reason}; {retries} retry/retries", signs

    def _response_quality(
        self, request: EvaluationRequest, facts: Mapping[str, Any]
    ) -> tuple[float, str, tuple[str, ...]]:
        candidate = request.candidate
        if not candidate:
            return 0.0, "no candidate output was recorded", ()
        if not bool(facts.get("result_present", True)):
            return 0.4, "a candidate exists but no result was recorded", ()
        if _text(candidate.get("error")):
            return 0.2, "the candidate carries an error", ()
        return 1.0, "the candidate is present and carries no error", ()


class LocalAIEvaluator(Evaluator):
    """A local, deterministic rubric scorer for tasks without a single check.

    It reads the same observable facts as the rule evaluator and weighs them
    into a shorter rubric — it does not call a model, and it cannot be talked
    into a different answer by what the candidate SAYS. A deployment that has a
    real evaluator model wires one in through :class:`ExternalAIEvaluator`
    instead of pretending this one is that.
    """

    evaluator_id = "local_ai"
    evaluator_kind = EvaluatorKind.LOCAL.value

    #: The rubric's own vocabulary: three criteria, weighted one place.
    RUBRIC: Mapping[str, float] = {
        "correctness": 1.0,
        "safety": 1.0,
        "response_quality": 0.5,
    }

    def missing_requirements(self) -> tuple[str, ...]:
        return ()

    def validate(self, request: EvaluationRequest) -> tuple[str, ...]:
        problems: list[str] = []
        if not request.candidate and not request.outcome:
            problems.append("neither a candidate nor an outcome was supplied")
        return tuple(problems)

    def evaluate(self, request: EvaluationRequest) -> AIRating:
        if not request.candidate and not request.outcome:
            raise EvaluatorUnavailable(
                "local evaluator needs either a candidate or observable outcome facts"
            )
        facts = self._fallback_outcome(request)
        rule = RuleBasedEvaluator(weights=self.RUBRIC)
        criteria: list[CriterionScore] = []
        evidence: list[str] = []
        for name, weight in self.RUBRIC.items():
            score, reason, signs = rule._criterion(name, request, facts)  # noqa: SLF001 - same rubric
            criteria.append(
                CriterionScore(name=name, score=score, weight=weight, reason=reason)
            )
            evidence.extend(signs)
        measured = bool(facts.get("verification_total")) or facts.get("task_succeeded") is not None
        return AIRating(
            trajectory_id=request.trajectory_id,
            task_id=request.task_id,
            evaluator_id=self.evaluator_id,
            evaluator_kind=self.evaluator_kind,
            evaluator_version=self.version,
            score=self._weighted(criteria),
            criteria=tuple(criteria),
            confidence=0.7 if measured else 0.4,
            evidence=tuple(dict.fromkeys(evidence + [EVIDENCE_AI_RATING] + list(request.evidence))),
            candidate=dict(request.candidate),
        )


class ExternalAIEvaluator(Evaluator):
    """An injected judge, parsed into the same structured rating.

    NovaControl does not depend on any provider. A deployment that has a
    judging service passes a callable that returns a mapping, and the mapping is
    validated here: a score, optional criterion scores, confidence and evidence.
    Anything shaped like hidden reasoning is DISCARDED before the rating is
    built — a judge returning ``chain_of_thought`` does not get to store it, and
    the rating records that it was dropped.
    """

    evaluator_id = "external_ai"
    evaluator_kind = EvaluatorKind.EXTERNAL.value

    def __init__(self, judge: Judge | None = None, *, evaluator_id: str = "") -> None:
        self.judge = judge
        if evaluator_id:
            self.evaluator_id = _text(evaluator_id)

    def is_available(self) -> bool:
        return self.judge is not None

    def missing_requirements(self) -> tuple[str, ...]:
        return () if self.judge is not None else ("an injected judge callable",)

    def evaluate(self, request: EvaluationRequest) -> AIRating:
        if self.judge is None:
            raise EvaluatorUnavailable(
                "no judge is wired into this installation: an external evaluator "
                "needs a deployment to provide one, and NovaControl does not "
                "depend on any provider"
            )
        raw = self.judge(request)
        if not isinstance(raw, Mapping):
            raise EvaluatorUnavailable(
                "the judge returned "
                f"{type(raw).__name__}, not a structured mapping"
            )
        payload = {str(key): value for key, value in raw.items()}
        discarded = [key for key in payload if key.lower() in HIDDEN_REASONING_KEYS]
        for key in discarded:
            payload.pop(key, None)
        score = _real(payload.get("score"))
        if score is None:
            raise EvaluatorUnavailable(
                "the judge did not return a structured 'score'"
            )
        criteria = self._criteria(payload.get("criteria"))
        evidence = [str(item) for item in payload.get("evidence") or () if str(item).strip()]
        if discarded:
            evidence.append(DISCARDED_REASONING_EVIDENCE)
        confidence = _real(payload.get("confidence"))
        if confidence is None:
            confidence = 0.6
        status = (
            FeedbackStatus.NEEDS_REVIEW.value
            if discarded
            else FeedbackStatus.ACCEPTED.value
        )
        return AIRating(
            trajectory_id=request.trajectory_id,
            task_id=request.task_id,
            evaluator_id=_text(payload.get("evaluator_id"), self.evaluator_id),
            evaluator_kind=self.evaluator_kind,
            evaluator_version=_text(payload.get("evaluator_version"), self.version),
            score=max(0.0, min(1.0, score)),
            criteria=criteria,
            confidence=max(0.0, min(1.0, confidence)),
            evidence=tuple(dict.fromkeys(evidence + [EVIDENCE_AI_RATING])),
            candidate=dict(request.candidate),
            status=status,
        )

    @staticmethod
    def _criteria(raw: Any) -> tuple[CriterionScore, ...]:
        if isinstance(raw, Mapping):
            return tuple(
                CriterionScore(
                    name=str(name),
                    score=max(0.0, min(1.0, _real(value, 0.0) or 0.0)),
                    weight=1.0,
                )
                for name, value in raw.items()
            )
        if isinstance(raw, (list, tuple)):
            return tuple(
                CriterionScore.from_dict(item) for item in raw if isinstance(item, Mapping)
            )
        return ()


class HumanEvaluator(Evaluator):
    """A person's feedback, projected into the same rating shape as an AI's.

    The projection is what makes human/AI comparison possible: both produce
    criterion scores and a confidence, so a disagreement detector can compare
    them without one being privileged by its type. Human feedback is NOT
    silently equivalent to an AI's — the evaluator id and the source stay
    different, and that difference is the point of keeping both.
    """

    evaluator_id = "human"
    evaluator_kind = EvaluatorKind.HUMAN.value

    def __init__(self, policy: RewardPolicyConfig | None = None) -> None:
        self.policy = policy if policy is not None else RewardPolicyConfig()

    def validate(self, request: EvaluationRequest) -> tuple[str, ...]:
        if not request.feedback:
            return ("human feedback is required for a human evaluation",)
        return ()

    def evaluate(self, request: EvaluationRequest) -> AIRating:
        rows = tuple(row for row in request.feedback if row.feedback_type)
        if not rows:
            raise EvaluatorUnavailable(
                "no human feedback was supplied, so a person's reading cannot be "
                "invented"
            )
        signals = [feedback_reward_value(row, self.policy) for row in rows]
        score = max(0.0, min(1.0, (sum(signals) / len(signals) + 1.0) / 2.0))
        by_type: dict[str, list[float]] = {}
        for row, signal in zip(rows, signals, strict=True):
            by_type.setdefault(row.feedback_type, []).append(signal)
        criteria = tuple(
            CriterionScore(
                name=f"human_{name}",
                score=max(0.0, min(1.0, (sum(values) / len(values) + 1.0) / 2.0)),
                weight=1.0,
                reason=f"{len(values)} {name} feedback row(s)",
            )
            for name, values in sorted(by_type.items())
        )
        confidences = [row.confidence for row in rows if row.confidence is not None]
        return AIRating(
            trajectory_id=request.trajectory_id,
            task_id=request.task_id,
            evaluator_id=self.evaluator_id,
            evaluator_kind=self.evaluator_kind,
            evaluator_version=self.version,
            score=score,
            criteria=criteria,
            confidence=(sum(confidences) / len(confidences)) if confidences else 0.5,
            evidence=tuple(
                dict.fromkeys(sign for row in rows for sign in feedback_evidence(row))
            ),
            candidate=dict(request.candidate),
            mode="rlhf",
        )


class NoEvaluator(Evaluator):
    """The explicit "there is no evaluator here", so selection never guesses."""

    evaluator_id = "none"
    evaluator_kind = ""

    def is_available(self) -> bool:
        return False

    def missing_requirements(self) -> tuple[str, ...]:
        return ("an evaluator was switched off for this run",)

    def evaluate(self, request: EvaluationRequest) -> AIRating:
        del request
        raise EvaluatorUnavailable("no evaluator is configured for this run")


def evaluator_for(
    name: str,
    *,
    judge: Judge | None = None,
    weights: Mapping[str, float] | None = None,
    policy: RewardPolicyConfig | None = None,
) -> Evaluator:
    """Build an evaluator by name. An unknown name is refused, never defaulted.

    ``auto`` resolves to the rule evaluator, which is the phase's preference:
    when a task can be verified objectively, a deterministic check is the most
    trustworthy judge available.
    """
    wanted = _text(name, "auto").lower()
    if wanted in {"auto", "rule", "rule_based"}:
        return RuleBasedEvaluator(weights=weights)
    if wanted in {"local", "local_ai"}:
        return LocalAIEvaluator()
    if wanted in {"external", "external_ai"}:
        return ExternalAIEvaluator(judge)
    if wanted == "human":
        return HumanEvaluator(policy)
    if wanted in {"none", "off"}:
        return NoEvaluator()
    raise ValueError(
        f"unknown evaluator {name!r}: expected auto, rule, local, external, human or none"
    )


__all__ = [
    "DEFAULT_CRITERIA",
    "DEFAULT_CRITERIA_WEIGHTS",
    "DISCARDED_REASONING_EVIDENCE",
    "Evaluator",
    "EvaluatorUnavailable",
    "EvaluationRequest",
    "ExternalAIEvaluator",
    "HIDDEN_REASONING_KEYS",
    "HumanEvaluator",
    "Judge",
    "LocalAIEvaluator",
    "NoEvaluator",
    "RuleBasedEvaluator",
    "evaluator_for",
    "outcome_facts",
]
