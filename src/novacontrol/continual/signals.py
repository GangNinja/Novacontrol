"""Signals derived from executed evidence.

A signal states something the evidence supports and nothing more:

* a tool fails repeatedly  → "avoid <tool>" (with the count that says so),
* a step is repeatedly the failing one → "this plan shape fails at <step>",
* corrections cluster on a task kind → "this kind is being corrected",
* recoveries cluster on a task kind → "recovery works here",
* the same task succeeds repeatedly → "this task kind is repeatable".

Every signal carries its evidence ids and a confidence computed from support.
Signals are re-derived from the corpus rather than accumulated, so deleting an
experience removes its effect and two runs cannot double-count the same failure.
"""

from __future__ import annotations

import hashlib
import statistics
from collections import defaultdict
from typing import Iterable, Sequence

from novacontrol.continual.models import (
    ExecutionEvidence,
    LearningSignal,
    OutcomeClass,
    SignalKind,
    StepStatus,
    clamp01,
)

__all__ = ["SignalExtractor"]

#: A tool needs this many observed step outcomes before its reliability is
#: stated at all. One failure is an anecdote; two failures out of two attempts
#: is already a pattern worth avoiding.
MIN_TOOL_SUPPORT = 2
MIN_PATTERN_SUPPORT = 2
MIN_LATENCY_SUPPORT = 3


class SignalExtractor:
    """Turn a bounded corpus of executions into statements with provenance."""

    def __init__(
        self,
        *,
        min_tool_support: int = MIN_TOOL_SUPPORT,
        min_pattern_support: int = MIN_PATTERN_SUPPORT,
        min_latency_support: int = MIN_LATENCY_SUPPORT,
        correction_threshold: int = 1,
    ) -> None:
        self.min_tool_support = max(1, int(min_tool_support))
        self.min_pattern_support = max(1, int(min_pattern_support))
        self.min_latency_support = max(1, int(min_latency_support))
        self.correction_threshold = max(1, int(correction_threshold))

    def derive(self, experiences: Sequence[ExecutionEvidence]) -> list[LearningSignal]:
        signals: list[LearningSignal] = []
        signals.extend(self._tool_reliability(experiences))
        signals.extend(self._plan_mistakes(experiences))
        signals.extend(self._corrections(experiences))
        signals.extend(self._recoveries(experiences))
        signals.extend(self._latency(experiences))
        signals.extend(self._repeat_patterns(experiences))
        return signals

    # -- per-tool ---------------------------------------------------------

    def _tool_reliability(self, experiences: Sequence[ExecutionEvidence]) -> list[LearningSignal]:
        completed: dict[str, int] = defaultdict(int)
        failed: dict[str, int] = defaultdict(int)
        evidence_ids: dict[str, set[str]] = defaultdict(set)
        task_kinds: dict[str, set[str]] = defaultdict(set)
        for evidence in experiences:
            for step in evidence.steps:
                tool = str(step.tool or "").strip().lower()
                if not tool:
                    continue
                if step.status == StepStatus.COMPLETED.value:
                    completed[tool] += 1
                elif step.status in {StepStatus.FAILED.value, StepStatus.DENIED.value}:
                    failed[tool] += 1
                else:
                    continue
                evidence_ids[tool].add(evidence.experience_id)
                task_kinds[tool].add(evidence.task_kind)
        signals: list[LearningSignal] = []
        for tool in sorted(set(completed) | set(failed)):
            total = completed[tool] + failed[tool]
            if total < self.min_tool_support:
                continue
            failure_rate = failed[tool] / total
            support = len(evidence_ids[tool])
            if failed[tool] >= 2 and failure_rate >= 0.5:
                confidence = clamp01(failure_rate * (0.7 + 0.1 * min(support, 3)))
                signals.append(
                    _signal(
                        kind=SignalKind.TOOL_RELIABILITY,
                        subject=tool,
                        task_kind=_dominant(task_kinds[tool]),
                        statement=(
                            f"tool {tool!r} failed {failed[tool]} of {total} observed step(s); "
                            "prefer a verified alternative"
                        ),
                        direction="avoid",
                        support=support,
                        evidence_ids=sorted(evidence_ids[tool]),
                        confidence=confidence,
                        qualifier=f"avoid:{tool}",
                    )
                )
            elif completed[tool] >= 3 and failure_rate <= 0.2:
                confidence = clamp01(0.6 + 0.1 * min(completed[tool], 3))
                signals.append(
                    _signal(
                        kind=SignalKind.TOOL_RELIABILITY,
                        subject=tool,
                        task_kind=_dominant(task_kinds[tool]),
                        statement=(
                            f"tool {tool!r} completed {completed[tool]} of {total} observed step(s)"
                        ),
                        direction="prefer",
                        support=support,
                        evidence_ids=sorted(evidence_ids[tool]),
                        confidence=confidence,
                        qualifier=f"prefer:{tool}",
                    )
                )
        return signals

    # -- plan shapes ------------------------------------------------------

    def _plan_mistakes(self, experiences: Sequence[ExecutionEvidence]) -> list[LearningSignal]:
        failures: dict[tuple[str, str], list[str]] = defaultdict(list)
        task_kinds: dict[tuple[str, str], set[str]] = defaultdict(set)
        for evidence in experiences:
            for step in evidence.steps:
                if step.status not in {StepStatus.FAILED.value, StepStatus.DENIED.value}:
                    continue
                key = (step.kind or "step", step.target or step.tool or "unknown")
                failures[key].append(evidence.experience_id)
                task_kinds[key].add(evidence.task_kind)
        signals: list[LearningSignal] = []
        for (kind, target), ids in sorted(failures.items()):
            distinct = sorted(set(ids))
            if len(distinct) < self.min_pattern_support:
                continue
            subject = f"{kind}:{target}"
            signals.append(
                _signal(
                    kind=SignalKind.PLAN_MISTAKE,
                    subject=subject,
                    task_kind=_dominant(task_kinds[(kind, target)]),
                    statement=(
                        f"{len(distinct)} execution(s) failed at {kind} {target!r}; "
                        "that step needs a different approach"
                    ),
                    direction="avoid",
                    support=len(distinct),
                    evidence_ids=distinct,
                    confidence=clamp01(0.55 + 0.1 * min(len(distinct), 4)),
                    qualifier=f"avoid-step:{subject}",
                )
            )
        return signals

    # -- people -----------------------------------------------------------

    def _corrections(self, experiences: Sequence[ExecutionEvidence]) -> list[LearningSignal]:
        by_kind: dict[str, list[str]] = defaultdict(list)
        for evidence in experiences:
            if evidence.outcome == OutcomeClass.USER_CORRECTED.value:
                by_kind[evidence.task_kind].append(evidence.experience_id)
        signals: list[LearningSignal] = []
        for task_kind, ids in sorted(by_kind.items()):
            if len(ids) < self.correction_threshold:
                continue
            signals.append(
                _signal(
                    kind=SignalKind.USER_CORRECTION,
                    subject=task_kind,
                    task_kind=task_kind,
                    statement=(
                        f"{len(ids)} execution(s) of {task_kind!r} were corrected by the user; "
                        "the current approach is not what was wanted"
                    ),
                    direction="avoid",
                    support=len(ids),
                    evidence_ids=sorted(ids),
                    confidence=clamp01(0.6 + 0.1 * min(len(ids), 3)),
                    qualifier=f"corrected:{task_kind}",
                )
            )
        return signals

    # -- recovery ---------------------------------------------------------

    def _recoveries(self, experiences: Sequence[ExecutionEvidence]) -> list[LearningSignal]:
        by_kind: dict[str, list[str]] = defaultdict(list)
        attempts: dict[str, int] = defaultdict(int)
        for evidence in experiences:
            if evidence.outcome == OutcomeClass.RECOVERED.value:
                by_kind[evidence.task_kind].append(evidence.experience_id)
                attempts[evidence.task_kind] += max(1, evidence.retries)
        signals: list[LearningSignal] = []
        for task_kind, ids in sorted(by_kind.items()):
            if len(ids) < self.min_pattern_support:
                continue
            average = attempts[task_kind] / len(ids)
            signals.append(
                _signal(
                    kind=SignalKind.RECOVERY_EFFECTIVENESS,
                    subject=task_kind,
                    task_kind=task_kind,
                    statement=(
                        f"{len(ids)} recovered execution(s) of {task_kind!r} finished verified "
                        f"after about {average:.1f} retries; recovery is worth its cost here"
                    ),
                    direction="prefer",
                    support=len(ids),
                    evidence_ids=sorted(ids),
                    confidence=clamp01(0.55 + 0.1 * min(len(ids), 3)),
                    qualifier=f"recovery:{task_kind}",
                )
            )
        return signals

    # -- latency ----------------------------------------------------------

    def _latency(self, experiences: Sequence[ExecutionEvidence]) -> list[LearningSignal]:
        by_kind: dict[str, list[float]] = defaultdict(list)
        ids: dict[str, list[str]] = defaultdict(list)
        for evidence in experiences:
            if evidence.latency_ms <= 0:
                continue
            by_kind[evidence.task_kind].append(evidence.latency_ms)
            ids[evidence.task_kind].append(evidence.experience_id)
        signals: list[LearningSignal] = []
        for task_kind, values in sorted(by_kind.items()):
            if len(values) < self.min_latency_support:
                continue
            median = statistics.median(values)
            signals.append(
                _signal(
                    kind=SignalKind.LATENCY,
                    subject=task_kind,
                    task_kind=task_kind,
                    statement=(
                        f"{len(values)} execution(s) of {task_kind!r} have a median latency of "
                        f"{median:.0f} ms"
                    ),
                    direction="note",
                    support=len(values),
                    evidence_ids=sorted(ids[task_kind]),
                    confidence=0.6,
                    qualifier=f"latency:{task_kind}",
                )
            )
        return signals

    # -- repeats ----------------------------------------------------------

    def _repeat_patterns(self, experiences: Sequence[ExecutionEvidence]) -> list[LearningSignal]:
        by_fingerprint: dict[str, list[ExecutionEvidence]] = defaultdict(list)
        for evidence in experiences:
            if evidence.assessed:
                by_fingerprint[evidence.fingerprint].append(evidence)
        signals: list[LearningSignal] = []
        for fingerprint, group in sorted(by_fingerprint.items()):
            if len(group) < self.min_pattern_support:
                continue
            decided = [
                item
                for item in group
                if item.outcome
                in {
                    OutcomeClass.SUCCESS.value,
                    OutcomeClass.RECOVERED.value,
                    OutcomeClass.PARTIAL.value,
                    OutcomeClass.FAILURE.value,
                }
            ]
            if len(decided) < self.min_pattern_support:
                continue
            successes = [item for item in decided if item.verified_outcome]
            rate = len(successes) / len(decided)
            task_kind = _dominant({item.task_kind for item in group})
            if rate >= 0.6:
                direction, word = "prefer", "succeeds"
            elif rate <= 0.4:
                direction, word = "avoid", "fails"
            else:
                direction, word = "note", "is inconsistent"
            signals.append(
                _signal(
                    kind=SignalKind.REPEAT_PATTERN,
                    subject=group[0].normalized_goal or group[0].goal,
                    task_kind=task_kind,
                    statement=(
                        f"a repeated task ({group[0].goal!r}) {word} in {len(successes)} of "
                        f"{len(decided)} decided execution(s)"
                    ),
                    direction=direction,
                    support=len(decided),
                    evidence_ids=sorted(item.experience_id for item in group),
                    confidence=clamp01(0.55 + 0.1 * min(len(decided), 4)),
                    qualifier=f"repeat:{fingerprint}",
                )
            )
        return signals


def _signal(
    *,
    kind: SignalKind,
    subject: str,
    task_kind: str,
    statement: str,
    direction: str,
    support: int,
    evidence_ids: Iterable[str],
    confidence: float,
    qualifier: str,
) -> LearningSignal:
    digest = hashlib.sha1(f"{qualifier}\x1f{task_kind}".encode("utf-8")).hexdigest()[:16]
    return LearningSignal(
        signal_id=f"sig:{digest}",
        kind=kind.value,
        subject=subject,
        task_kind=task_kind,
        statement=statement,
        direction=direction,
        support=support,
        evidence_ids=list(evidence_ids),
        confidence=clamp01(confidence),
    )


def _dominant(values: Iterable[str]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        if value:
            counts[value] += 1
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
