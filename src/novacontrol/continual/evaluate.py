"""Outcome evaluation: what actually happened, from evidence.

The single rule this module enforces: **a response that looks successful is not
evidence of success.** A record with no independent verification is classified
``UNVERIFIED`` — a first-class outcome that no strategy may be promoted on.

Verification is read from a small, explicit mapping the capture layer builds:

    {"status": "passed" | "failed" | "skipped", "passed": bool,
     "source": "tool" | "verifier" | "step" | ..., "reason": str,
     "checked": bool, "detail": str}

Anything else is treated as absent rather than inferred. The evaluator is pure:
it records no events, touches no disk, and cannot change the record it reads.
"""

from __future__ import annotations

from typing import Any, Mapping

from novacontrol.continual.models import (
    ExecutionEvidence,
    OutcomeAssessment,
    OutcomeClass,
    StepStatus,
    clamp01,
)

__all__ = ["OutcomeEvaluator", "verification_passed", "verification_failed"]

_PASSED_STATES = frozenset({"passed", "pass", "verified", "ok", "succeeded", "true"})
_FAILED_STATES = frozenset({"failed", "fail", "error", "rejected", "false"})


def verification_passed(verification: Mapping[str, Any] | None) -> bool:
    """Whether the verification mapping affirmatively confirms the work."""
    if not isinstance(verification, Mapping) or not verification:
        return False
    passed = verification.get("passed")
    if passed is True:
        return True
    status = str(verification.get("status", "") or "").strip().lower()
    return status in _PASSED_STATES


def verification_failed(verification: Mapping[str, Any] | None) -> bool:
    if not isinstance(verification, Mapping) or not verification:
        return False
    passed = verification.get("passed")
    if passed is False:
        return True
    status = str(verification.get("status", "") or "").strip().lower()
    return status in _FAILED_STATES


class OutcomeEvaluator:
    """Classify one execution from its verification, steps, retries and feedback."""

    def __init__(self, *, correction_threshold: int = 1) -> None:
        #: A single explicit correction is enough: a person saying "no, that was
        #: wrong" is stronger evidence than any heuristic this layer could apply.
        self.correction_threshold = max(1, int(correction_threshold))

    def assess(self, evidence: ExecutionEvidence) -> OutcomeAssessment:
        steps = list(evidence.steps)
        completed = [step for step in steps if step.status == StepStatus.COMPLETED.value]
        failed = [step for step in steps if step.status in {StepStatus.FAILED.value, StepStatus.DENIED.value}]
        skipped = [step for step in steps if step.status == StepStatus.SKIPPED.value]
        applicable = len(steps) - len(skipped)
        completion = (len(completed) / applicable) if applicable > 0 else 0.0
        verification = evidence.verification if isinstance(evidence.verification, Mapping) else {}
        source = str(verification.get("source", "") or "").strip()
        verified = verification_passed(verification)
        failed_verification = verification_failed(verification)
        # A step that reports its own completion is NOT independent verification:
        # a write confirming it wrote proves only that it returned. Only a step
        # whose job is checking something else (kind ``verify``/``check``) can
        # move a run into SUCCESS without a separate verification mapping.
        independent = [
            step
            for step in steps
            if step.kind.strip().lower() in {"verify", "check", "validation"}
            and step.status == StepStatus.COMPLETED.value
            and step.verified is True
        ]
        retried = bool(evidence.recoveries) or evidence.retries > 0 or any(step.attempts > 1 for step in steps)

        # A NOTE alone is not a correction: positive feedback with an
        # explanation must not reclassify verified work as user-corrected.
        if evidence.feedback_verdict == "negative":
            return OutcomeAssessment(
                outcome=OutcomeClass.USER_CORRECTED.value,
                confidence=0.95,
                reasons=[
                    "the user corrected or rejected this execution",
                    *( [f"feedback: {evidence.user_feedback}"] if evidence.user_feedback else [] ),
                ],
                completion_ratio=completion if steps else 0.0,
                verified=False,
                verification_source=source,
            )

        if failed_verification:
            return OutcomeAssessment(
                outcome=OutcomeClass.FAILURE.value,
                confidence=0.9,
                reasons=[
                    str(verification.get("reason", "") or "verification failed"),
                    "the recorded verification says the work did not complete",
                ],
                completion_ratio=completion if steps else 0.0,
                verified=False,
                verification_source=source,
            )

        if verified:
            if failed and retried:
                return OutcomeAssessment(
                    outcome=OutcomeClass.RECOVERED.value,
                    confidence=0.9,
                    reasons=[
                        f"{len(failed)} step(s) failed or were denied and the run still ended verified",
                        f"{len(evidence.recoveries)} recovery action(s) recorded"
                        if evidence.recoveries
                        else f"retries recorded: {evidence.retries}",
                    ],
                    completion_ratio=completion if steps else 1.0,
                    verified=True,
                    verification_source=source or "verifier",
                )
            if failed:
                # Verified overall with unrecovered failed steps is contradictory
                # evidence: partial, not success. A strategy must not learn that
                # this shape works.
                return OutcomeAssessment(
                    outcome=OutcomeClass.PARTIAL.value,
                    confidence=0.7,
                    reasons=[
                        "verification passed but some steps never completed",
                        f"{len(failed)} failed/denied step(s) remain",
                    ],
                    completion_ratio=completion if steps else 0.0,
                    verified=True,
                    verification_source=source or "verifier",
                )
            return OutcomeAssessment(
                outcome=OutcomeClass.SUCCESS.value,
                confidence=0.95 if source else 0.8,
                reasons=[
                    str(verification.get("reason", "") or "verification passed"),
                    f"all {len(completed)} executed step(s) completed" if steps else "the recorded verification passed",
                ],
                completion_ratio=1.0 if not steps else completion,
                verified=True,
                verification_source=source or "verifier",
            )

        # No affirmative verification. Step-level confirmations are the only
        # fallback, and they raise confidence — they do not turn a run into a
        # verified success, because a step confirming itself is not independent.
        if independent and not failed and completion >= 1.0:
            return OutcomeAssessment(
                outcome=OutcomeClass.SUCCESS.value,
                confidence=0.8,
                reasons=[
                    f"{len(independent)} verification step(s) confirmed the result",
                    "confidence is lower than an external verifier's verdict",
                ],
                completion_ratio=1.0,
                verified=True,
                verification_source=source or "step",
            )

        if completed or failed:
            if failed and not completed:
                return OutcomeAssessment(
                    outcome=OutcomeClass.FAILURE.value,
                    confidence=0.85,
                    reasons=[
                        "no step completed",
                        str(evidence.errors[-1]) if evidence.errors else "the run recorded failed steps",
                    ],
                    completion_ratio=0.0,
                    verified=False,
                    verification_source=source,
                )
            return OutcomeAssessment(
                outcome=OutcomeClass.PARTIAL.value,
                confidence=0.6,
                reasons=[
                    f"{len(completed)} of {len(steps)} step(s) completed",
                    "nothing independently verified the outcome",
                ] + ([str(evidence.errors[-1])] if evidence.errors else []),
                completion_ratio=completion,
                verified=False,
                verification_source=source,
            )

        return OutcomeAssessment(
            outcome=OutcomeClass.UNVERIFIED.value,
            confidence=0.25,
            reasons=[
                "the execution produced no verification evidence",
                "no step reported a confirmed result",
            ],
            completion_ratio=0.0,
            verified=False,
            verification_source=source,
        )

    def apply(self, evidence: ExecutionEvidence) -> OutcomeAssessment:
        """Assess and write the verdict back onto the record (one direction)."""
        assessment = self.assess(evidence)
        evidence.outcome = assessment.outcome
        evidence.outcome_confidence = clamp01(assessment.confidence)
        evidence.outcome_reasons = list(assessment.reasons)
        evidence.completion_ratio = clamp01(assessment.completion_ratio)
        evidence.verified = bool(assessment.verified)
        return assessment
