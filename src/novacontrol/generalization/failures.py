"""Failure classification: naming what went wrong, so it can be acted on.

A miss is only useful if it says WHICH miss it was. The phase names eleven
categories, and the distinctions that matter are the ones a solver's ending
alone cannot express:

* a rule that was never found (``NO_CANDIDATE_RULE_FOUND``) is not a rule that
  fitted the examples and missed the test (``RULE_FITS_TRAIN_FAILS_TEST``);
* two rules that both fit (``MULTIPLE_PLAUSIBLE_RULES``) is not a failure at
  all — it is the honest ending the phase requires;
* an answer that was produced and rejected by the verifier
  (``VERIFICATION_FAILURE``) is not an infrastructure bug
  (``EVALUATION_INFRASTRUCTURE``), and neither is a broken transformation
  (``TRANSFORMATION_DEFECT``);
* a run that hit its budget (``RESOURCE_BUDGET_EXHAUSTED``) is not a decision;
* and a broken evaluation SETUP (``DATA_LEAKAGE``) must never be reported as a
  model failure, because then the numbers are wrong in a way nobody investigates.

The classifier is pure: it reads a solver's outcome and a verification record and
returns a category plus the reason a report can quote. It writes nothing and
promotes nothing.
"""

from __future__ import annotations

from typing import Any

from novacontrol.generalization.models import (
    FailureCategory,
    RuleSource,
    TaskKind,
    TaskStatus,
    VerificationRecord,
)

__all__ = [
    "FAILURE_ADVICE",
    "classify_failure",
    "group_failures",
]


def classify_failure(
    *,
    status: TaskStatus,
    source: RuleSource = RuleSource.NONE,
    verified: bool | None,
    failure: FailureCategory | None,
    reason: str = "",
    task_kind: TaskKind | None = None,
) -> tuple[FailureCategory | None, str]:
    """The category a miss belongs to, and the reason to quote.

    Called AFTER verification: a verified answer has no failure category at all,
    whatever the solver's own ending was.
    """
    if verified is True:
        return None, ""
    if status is TaskStatus.SOLVED:
        # An answer was produced and the check did not accept it.
        if verified is None:
            return (
                FailureCategory.VERIFICATION_FAILURE,
                reason or "the answer could not be checked against an expected output",
            )
        if task_kind is TaskKind.SMALL_PLANNING:
            return (
                FailureCategory.VERIFICATION_FAILURE,
                reason or "the plan was re-simulated and did not reach the goal",
            )
        return (
            FailureCategory.RULE_FITS_TRAIN_FAILS_TEST,
            reason
            or (
                "the rule reproduced every example and produced the wrong answer for the "
                "unseen input"
            ),
        )
    if status is TaskStatus.AMBIGUOUS:
        return (
            FailureCategory.MULTIPLE_PLAUSIBLE_RULES,
            reason or "more than one rule fits the examples and they disagree",
        )
    if status is TaskStatus.UNSUPPORTED:
        if source is RuleSource.MODEL_PROPOSAL:
            return (
                FailureCategory.PROVIDER_UNAVAILABLE,
                reason or "the only source that could answer is not available here",
            )
        return (
            FailureCategory.NO_CANDIDATE_RULE_FOUND,
            reason or "nothing in this build reproduces the examples",
        )
    if status is TaskStatus.BUDGET_EXHAUSTED:
        return (
            FailureCategory.RESOURCE_BUDGET_EXHAUSTED,
            reason or "the run spent its budget before producing an answer",
        )
    if status is TaskStatus.INVALID:
        return (failure or FailureCategory.INVALID_TASK_DEFINITION, reason)
    if status is TaskStatus.PROVIDER_UNAVAILABLE:
        return (FailureCategory.PROVIDER_UNAVAILABLE, reason)
    if status is TaskStatus.ERROR:
        wanted = failure or FailureCategory.EVALUATION_INFRASTRUCTURE
        if wanted is FailureCategory.TRANSFORMATION_DEFECT:
            return (
                FailureCategory.TRANSFORMATION_DEFECT,
                reason or "an operator raised while being applied",
            )
        return (wanted, reason or "the evaluation raised while evaluating the task")
    if status is TaskStatus.FAILED:
        return (failure or FailureCategory.VERIFICATION_FAILURE, reason)
    return (failure or FailureCategory.EVALUATION_INFRASTRUCTURE, reason)


#: What a person should do about each category. Kept beside the classifier so a
#: recommendation cannot drift from the category it recommends for.
FAILURE_ADVICE: dict[FailureCategory, str] = {
    FailureCategory.INPUT_PARSING_FAILURE: (
        "the task's own input is unreadable: fix the task or the reader before reading "
        "anything into the score"
    ),
    FailureCategory.INVALID_TASK_DEFINITION: (
        "the task states no checkable success condition, a missing input or no answer key"
    ),
    FailureCategory.NO_CANDIDATE_RULE_FOUND: (
        "no operator in the library reproduces the examples: either the transformation is "
        "outside the supported families, or one more operator is genuinely needed"
    ),
    FailureCategory.MULTIPLE_PLAUSIBLE_RULES: (
        "the examples do not decide between rules: more examples, or an explicit "
        "disambiguation rule, are the fix — guessing is not"
    ),
    FailureCategory.RULE_FITS_TRAIN_FAILS_TEST: (
        "the fitted rule reproduces the examples and misses the unseen input: suspect the "
        "examples (too few to pin the rule) before suspecting the operator"
    ),
    FailureCategory.TRANSFORMATION_DEFECT: (
        "an operator raised while running: a defect in this build, not a wrong answer"
    ),
    FailureCategory.VERIFICATION_FAILURE: (
        "the answer was produced and rejected: check the verifier against the success "
        "condition before blaming the solver"
    ),
    FailureCategory.RESOURCE_BUDGET_EXHAUSTED: (
        "the run hit a bound: raise the bound deliberately, or reduce the search"
    ),
    FailureCategory.PROVIDER_UNAVAILABLE: (
        "the answer depends on a model or service that is not installed here; the "
        "deterministic path stays the answer"
    ),
    FailureCategory.TIMEOUT: "the work did not finish inside its time budget",
    FailureCategory.DATA_LEAKAGE: (
        "the evaluation is not clean: a held-out task shares its answer, or a fitting "
        "example was used as a tuning set. FIX THE SETUP — the score means nothing"
    ),
    FailureCategory.EVALUATION_INFRASTRUCTURE: (
        "the evaluation itself raised: fix the harness before reading the metrics"
    ),
}


def group_failures(records: Any) -> dict[str, list[str]]:
    """Failures grouped by category — the shape a report and a recommendation use."""
    grouped: dict[str, list[str]] = {}
    for record in records:
        category = getattr(record, "category", None)
        # ``getattr`` rather than ``hasattr`` + ``.value``: the guard on its own
        # cannot narrow ``Any | None`` for the type checker, and the fallback is
        # the same string the previous expression produced.
        key = str(getattr(category, "value", category))
        grouped.setdefault(key, []).append(str(getattr(record, "task_id", "")))
    return {key: sorted(value) for key, value in sorted(grouped.items())}


def verification_record(
    *,
    method: str,
    description: str,
    verified: bool | None,
    detail: str = "",
    evidence: tuple[str, ...] = (),
) -> VerificationRecord:
    """Build a verification record. Kept here so every verifier looks the same."""
    return VerificationRecord(
        method=method,
        description=description,
        verified=verified,
        detail=detail,
        evidence=evidence,
    )
