"""Relevance retrieval for previous executions.

This is deliberately lexical and deterministic — no embeddings, no model. A
repeated task is recognised by its words, its kind, its route and its context,
and the score is explainable ("shared 4 tokens", "same normalized goal"). An
embedding index would be a second retrieval system beside Phase 11's knowledge
index; the phase forbids exactly that kind of duplication.

Compatibility is a hard gate, not a weight. A phone execution cannot inform a
desktop task even when the words are identical, because the executor differs;
scoring that similarity would be the "unchecked replay" the specification warns
about. Evidence that shares nothing is excluded instead of padded.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from novacontrol.continual.models import (
    ExecutionEvidence,
    RetrievalHit,
    OutcomeClass,
    normalize_goal,
)

__all__ = ["ExperienceRetriever", "task_tokens"]

_DECIDED = frozenset(
    {
        OutcomeClass.SUCCESS.value,
        OutcomeClass.RECOVERED.value,
        OutcomeClass.PARTIAL.value,
        OutcomeClass.FAILURE.value,
        OutcomeClass.USER_CORRECTED.value,
    }
)


def task_tokens(text: str) -> frozenset[str]:
    return frozenset(normalize_goal(text).split())


class ExperienceRetriever:
    """Rank previous executions against a new task, or exclude them."""

    def __init__(
        self,
        *,
        min_score: float = 0.45,
        max_results: int = 8,
        allow_partial_matches: bool = True,
    ) -> None:
        self.min_score = max(0.0, min(1.0, float(min_score)))
        self.max_results = max(1, int(max_results))
        self.allow_partial_matches = bool(allow_partial_matches)

    def retrieve(
        self,
        goal: str,
        *,
        task_kind: str = "",
        context: Mapping[str, Any] | None = None,
        experiences: Iterable[ExecutionEvidence],
        exclude_ids: Sequence[str] = (),
    ) -> list[RetrievalHit]:
        context = context or {}
        excluded = set(exclude_ids)
        hits: list[RetrievalHit] = []
        for evidence in experiences:
            if evidence.experience_id in excluded or not evidence.assessed:
                continue
            if evidence.outcome == OutcomeClass.UNVERIFIED.value and not self.allow_partial_matches:
                continue
            compatible, incompatibility = self.compatible(evidence, task_kind=task_kind, context=context)
            if not compatible:
                continue
            score, reason = self.score(goal, evidence, task_kind=task_kind)
            if score < self.min_score:
                continue
            hits.append(
                RetrievalHit(
                    experience_id=evidence.experience_id,
                    score=score,
                    outcome=evidence.outcome,
                    goal=evidence.goal,
                    fingerprint=evidence.fingerprint,
                    compatible=True,
                    reason=reason,
                )
            )
        hits.sort(key=lambda hit: (-hit.score, hit.experience_id))
        return hits[: self.max_results]

    def compatible(
        self,
        evidence: ExecutionEvidence,
        *,
        task_kind: str = "",
        context: Mapping[str, Any] | None = None,
    ) -> tuple[bool, str]:
        context = context or {}
        wanted_kind = str(task_kind or "").strip().lower()
        if wanted_kind and evidence.task_kind and wanted_kind != evidence.task_kind.lower():
            return False, f"another task kind ({evidence.task_kind})"
        for key in ("device", "app"):
            wanted = str(context.get(key, "") or "").strip().lower()
            recorded = str(evidence.context.get(key, "") or "").strip().lower()
            if wanted and recorded and wanted != recorded:
                return False, f"another {key} ({recorded})"
        return True, ""

    def score(
        self,
        goal: str,
        evidence: ExecutionEvidence,
        *,
        task_kind: str = "",
    ) -> tuple[float, str]:
        wanted = normalize_goal(goal)
        recorded = evidence.normalized_goal or normalize_goal(evidence.goal)
        if not wanted or not recorded:
            return 0.0, "no comparable tokens"
        if wanted == recorded:
            base = 0.85
            reason = "same normalized goal"
        else:
            left = set(wanted.split())
            right = set(recorded.split())
            union = left | right
            jaccard = len(left & right) / len(union) if union else 0.0
            base = 0.65 * jaccard
            reason = f"shared {len(left & right)} of {len(union)} tokens"
        kind_bonus = 0.10 if str(task_kind or "").strip().lower() == evidence.task_kind.lower() and task_kind else 0.0
        if kind_bonus:
            reason += "; same task kind"
        return min(1.0, base + kind_bonus), reason

    def evidence_for(
        self,
        goal: str,
        *,
        task_kind: str = "",
        context: Mapping[str, Any] | None = None,
        experiences: Iterable[ExecutionEvidence],
        min_score: float | None = None,
    ) -> list[ExecutionEvidence]:
        """The records behind the hits, in retrieval order (for provenance)."""
        by_id = {record.experience_id: record for record in experiences}
        floor = self.min_score if min_score is None else float(min_score)
        return [
            by_id[hit.experience_id]
            for hit in self.retrieve(
                goal,
                task_kind=task_kind,
                context=context,
                experiences=by_id.values(),
            )
            if hit.score >= floor and hit.experience_id in by_id
        ]
