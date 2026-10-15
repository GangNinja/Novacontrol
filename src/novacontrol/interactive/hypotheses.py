"""Hypotheses: what is believed, on what evidence, and how sure of it this thing is.

The book exists for three rules that are easy to state and easy to break (§4.2/§7):

**Evidence is counted as independent, not as repeated.** Two readings that share a
``source_key`` are one fact. A value read ten times from one sensor is one
observation, and ``independent_support`` says so while ``supporting`` still lists
every reading for a report.

**A prediction is never its own support.** Only observations and inferences move a
status; a prediction is stored so the report can show what was expected.

**Confidence is heuristic, and says so.** ``confidence_kind`` is
``insufficient_evidence`` until something has been observed, and ``heuristic``
afterwards — the number is a smoothed ratio of independent evidence, not a
probability, and no code here calls it one. When the support is only inferred, the
status is INCONCLUSIVE rather than SUPPORTED, because "we think this follows" is not
"we saw this".

Nothing here decides what to try, acts, or promotes a hypothesis into a fact: the
knowledge module owns learned rules and their thresholds, and the manager owns the
cycle.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from novacontrol.interactive.models import (
    MAX_EVIDENCE_PER_HYPOTHESIS,
    MAX_HYPOTHESES_PER_OBJECTIVE,
    ConfidenceKind,
    EvidenceKind,
    EvidenceReference,
    EvidenceRole,
    Hypothesis,
    HypothesisStatus,
    HypothesisVersion,
    as_float,
    as_text,
    clamp,
    new_id,
    now_iso,
    with_updates,
)

__all__ = [
    "ConfidencePolicy",
    "HypothesisBook",
    "candidate_hypotheses",
    "declared_candidates",
    "predictions_differ",
    "preview_hypotheses",
]


@dataclass(frozen=True, slots=True)
class ConfidencePolicy:
    """How a heuristic confidence is computed, and where the status lines are.

    Every threshold is declared so a report can state why a hypothesis is
    "supported" rather than leaving a reader to infer it from a number.
    """

    support_weight: float = 1.0
    contradiction_weight: float = 1.0
    prior_weight: float = 0.0
    promoted_at: float = 0.7
    contradicted_below: float = 0.3
    min_independent_support: int = 1
    limitations: tuple[str, ...] = (
        "confidence is a heuristic score from independent evidence counts, not a probability",
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "support_weight": self.support_weight,
            "contradiction_weight": self.contradiction_weight,
            "prior_weight": self.prior_weight,
            "promoted_at": self.promoted_at,
            "contradicted_below": self.contradicted_below,
            "min_independent_support": self.min_independent_support,
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ConfidencePolicy:
        defaults = cls()
        return cls(
            support_weight=as_float(data.get("support_weight"), defaults.support_weight),
            contradiction_weight=as_float(
                data.get("contradiction_weight"), defaults.contradiction_weight
            ),
            prior_weight=as_float(data.get("prior_weight"), defaults.prior_weight),
            promoted_at=clamp(as_float(data.get("promoted_at"), defaults.promoted_at), 0.0, 1.0),
            contradicted_below=clamp(
                as_float(data.get("contradicted_below"), defaults.contradicted_below), 0.0, 1.0
            ),
            min_independent_support=max(
                1,
                int(
                    as_float(
                        data.get("min_independent_support"),
                        defaults.min_independent_support,
                    )
                ),
            ),
            limitations=defaults.limitations,
        )


def predictions_differ(
    left: Mapping[str, Any], right: Mapping[str, Any], *, tolerance: float = 0.0
) -> bool:
    """Whether two predictions would be told apart by an observation.

    Keys present in one and not the other differ; numeric values compare within
    the tolerance the matcher will use, so "the selector thinks they differ" and
    "the comparison can distinguish them" cannot disagree.
    """
    if set(left) != set(right):
        return True
    for key, value in left.items():
        other = right[key]
        if isinstance(value, (int, float)) and isinstance(other, (int, float)):
            if abs(float(value) - float(other)) > tolerance:
                return True
            continue
        if value != other:
            return True
    return False


class HypothesisBook:
    """The hypotheses of this process, and the only place their status changes."""

    def __init__(self, *, policy: ConfidencePolicy | None = None) -> None:
        self.policy = policy or ConfidencePolicy()
        self._hypotheses: dict[str, Hypothesis] = {}
        self._order: list[str] = []

    # ── reading ─────────────────────────────────────────────────────────────
    def get(self, hypothesis_id: str) -> Hypothesis | None:
        return self._hypotheses.get(as_text(hypothesis_id))

    def all(self) -> tuple[Hypothesis, ...]:
        return tuple(self._hypotheses[key] for key in self._order)

    def for_objective(self, objective_id: str) -> tuple[Hypothesis, ...]:
        target = as_text(objective_id)
        return tuple(item for item in self.all() if item.objective_id == target)

    def open_for_objective(self, objective_id: str) -> tuple[Hypothesis, ...]:
        return tuple(item for item in self.for_objective(objective_id) if item.open_status)

    def testable_for_objective(self, objective_id: str) -> tuple[Hypothesis, ...]:
        """Hypotheses an experiment could still speak to: the open ones, plus SUPPORTED.

        A supported hypothesis is not a settled fact — support is a threshold, not a
        proof — so it stays testable. What keeps a re-test from being pointless is the
        duplicate rule: the same intervention cannot be re-run once its verdict has
        settled, so the only way to test it again is a DIFFERENT intervention, which
        is the declared-replicate verification path (§8).
        """
        return tuple(
            item
            for item in self.for_objective(objective_id)
            if item.predicted and (item.open_status or item.status is HypothesisStatus.SUPPORTED)
        )

    # ── writing ─────────────────────────────────────────────────────────────
    def propose(
        self,
        *,
        objective_id: str,
        statement: str,
        predicted: Mapping[str, Any] | None = None,
        scope: str = "",
        prior_confidence: float | None = None,
        limitations: Sequence[str] = (),
    ) -> Hypothesis:
        """A new hypothesis, with no evidence — which is exactly its confidence state."""
        existing = len(self.for_objective(objective_id))
        statement_text = as_text(statement)[:400]
        if existing >= MAX_HYPOTHESES_PER_OBJECTIVE:
            raise ValueError(
                f"objective {objective_id!r} already holds {existing} hypotheses, which is "
                f"the ceiling ({MAX_HYPOTHESES_PER_OBJECTIVE}); retire one instead of adding more"
            )
        if not statement_text:
            raise ValueError("a hypothesis needs a statement a person could judge")
        timestamp = now_iso()
        hypothesis = Hypothesis(
            hypothesis_id=new_id("hyp"),
            objective_id=as_text(objective_id),
            statement=statement_text,
            scope=as_text(scope),
            predicted=dict(predicted or {}),
            status=HypothesisStatus.PROPOSED,
            version=1,
            prior_confidence=(
                None if prior_confidence is None else clamp(as_float(prior_confidence), 0.0, 1.0)
            ),
            confidence=None,
            confidence_kind=ConfidenceKind.INSUFFICIENT_EVIDENCE,
            created_at=timestamp,
            updated_at=timestamp,
            limitations=(*tuple(limitations), *self.policy.limitations),
        )
        hypothesis = _with_version(hypothesis, reason="proposed")
        self._hypotheses[hypothesis.hypothesis_id] = hypothesis
        self._order.append(hypothesis.hypothesis_id)
        return hypothesis

    def mark_testable(self, hypothesis_id: str, *, reason: str = "") -> Hypothesis:
        """Record that a distinguishing experiment exists for this hypothesis."""
        current = self._require(hypothesis_id)
        if current.status is not HypothesisStatus.PROPOSED:
            return current
        updated = _revise(
            current,
            status=HypothesisStatus.TESTABLE,
            reason=reason or "an experiment able to distinguish this hypothesis exists",
            timestamp=now_iso(),
        )
        return self._store(updated)

    def apply_evidence(
        self,
        hypothesis_id: str,
        evidence: Sequence[EvidenceReference],
        *,
        reason: str = "",
        experiment_id: str = "",
        observation_refs: Sequence[str] = (),
    ) -> Hypothesis:
        """Record evidence, then recompute status and confidence from ALL of it.

        Recomputation from the full set (rather than nudging a number) is what makes
        a later contradiction able to move a hypothesis that earlier support had
        settled, and what keeps the arithmetic auditable.
        """
        current = self._require(hypothesis_id)
        supporting = list(current.supporting)
        contradicting = list(current.contradicting)
        inconclusive = list(current.inconclusive)
        known = set(supporting) | set(contradicting) | set(inconclusive)
        sources = list(current.source_keys)
        experiments = list(current.experiments)
        observations = list(current.observations)
        new_ids: list[str] = []
        for item in evidence:
            if item.evidence_id in known:
                continue  # the same reading twice is not two readings
            known.add(item.evidence_id)
            new_ids.append(item.evidence_id)
            if item.source_key and item.source_key not in sources:
                sources.append(item.source_key)
            if item.experiment_id and item.experiment_id not in experiments:
                experiments.append(item.experiment_id)
            if item.observation_ref and item.observation_ref not in observations:
                observations.append(item.observation_ref)
            if item.role is EvidenceRole.SUPPORTS:
                supporting.append(item.evidence_id)
            elif item.role is EvidenceRole.CONTRADICTS:
                contradicting.append(item.evidence_id)
            else:
                inconclusive.append(item.evidence_id)
        if experiment_id and experiment_id not in experiments:
            experiments.append(experiment_id)
        for reference in observation_refs:
            text = as_text(reference)
            if text and text not in observations:
                observations.append(text)
        notes = list(current.limitations)
        supporting, contradicting, inconclusive, dropped = _bounded(
            supporting, contradicting, inconclusive
        )
        if dropped:
            notes.append(
                f"{dropped} older evidence reference(s) are no longer listed; the "
                "independent counts below still include them"
            )
        (
            independent_support,
            independent_contradiction,
            support_keys,
            contradict_keys,
        ) = self._independent_counts(supporting, contradicting, evidence, current)
        kind_of_support = self._support_kind(supporting, evidence, current)
        confidence, confidence_kind = self._confidence(
            current, independent_support, independent_contradiction
        )
        status, status_reason = self._status(
            independent_support, independent_contradiction, confidence, kind_of_support
        )
        updated = Hypothesis(
            hypothesis_id=current.hypothesis_id,
            objective_id=current.objective_id,
            statement=current.statement,
            scope=current.scope,
            predicted=current.predicted,
            status=status,
            version=current.version + 1,
            prior_confidence=current.prior_confidence,
            confidence=confidence,
            confidence_kind=confidence_kind,
            supporting=tuple(supporting),
            contradicting=tuple(contradicting),
            inconclusive=tuple(inconclusive),
            independent_support=independent_support,
            independent_contradiction=independent_contradiction,
            source_keys=tuple(sources),
            supporting_sources=support_keys,
            contradicting_sources=contradict_keys,
            experiments=tuple(experiments),
            observations=tuple(observations),
            versions=current.versions,
            superseded_by=current.superseded_by,
            created_at=current.created_at,
            updated_at=now_iso(),
            limitations=tuple(notes),
        )
        return self._store(updated, reason=reason or status_reason, evidence_ids=new_ids)

    def supersede(self, hypothesis_id: str, *, by: str, reason: str = "") -> Hypothesis:
        """Replace a hypothesis with a newer one, keeping the old one readable."""
        current = self._require(hypothesis_id)
        updated = _revise(
            current,
            status=HypothesisStatus.SUPERSEDED,
            reason=reason or f"superseded by {by}",
            timestamp=now_iso(),
            superseded_by=as_text(by),
        )
        return self._store(updated)

    def retire(self, hypothesis_id: str, *, reason: str = "") -> Hypothesis:
        """Set a hypothesis aside. Retiring is a decision, and it is recorded as one."""
        current = self._require(hypothesis_id)
        updated = _revise(
            current,
            status=HypothesisStatus.RETIRED,
            reason=reason or "retired",
            timestamp=now_iso(),
        )
        return self._store(updated)

    def restore(self, hypotheses: Sequence[Hypothesis]) -> int:
        """Rebuild the book from stored records; unreadable rows are counted, not fatal."""
        restored = 0
        for item in hypotheses:
            if not item.hypothesis_id:
                continue
            self._hypotheses[item.hypothesis_id] = item
            if item.hypothesis_id not in self._order:
                self._order.append(item.hypothesis_id)
            restored += 1
        return restored

    # ── internals ───────────────────────────────────────────────────────────
    def _require(self, hypothesis_id: str) -> Hypothesis:
        current = self.get(hypothesis_id)
        if current is None:
            raise KeyError(f"no hypothesis {hypothesis_id!r} in this book")
        return current

    def _store(
        self, hypothesis: Hypothesis, *, reason: str = "", evidence_ids: Sequence[str] = ()
    ) -> Hypothesis:
        stamped = hypothesis
        if hypothesis.versions and hypothesis.versions[-1].version == hypothesis.version:
            stamped = hypothesis
        else:
            stamped = _with_version(hypothesis, reason=reason, evidence_ids=tuple(evidence_ids))
        self._hypotheses[stamped.hypothesis_id] = stamped
        return stamped

    def _independent_counts(
        self,
        supporting: Sequence[str],
        contradicting: Sequence[str],
        incoming: Sequence[EvidenceReference],
        current: Hypothesis,
    ) -> tuple[int, int, tuple[str, ...], tuple[str, ...]]:
        """Distinct source keys per role, counting only observational evidence.

        A prediction is skipped entirely, so a hypothesis whose only "support" is
        its own expectation stays untested. The stored count is never reduced: the
        evidence list is bounded, and a dropped reference must not make a source
        stop having existed.

        The keys accumulated in earlier calls are read back from the record and the
        new ones are added, so the count is the size of the union rather than the
        size of whatever arrived in this one call. Counting only the incoming keys
        left every repeat observation at "1 independent source" forever, which made
        the support threshold unreachable and a rule unlearnable (§7).
        """
        support_ids = set(supporting)
        contradict_ids = set(contradicting)
        support_keys = set(current.supporting_sources)
        contradict_keys = set(current.contradicting_sources)
        for item in incoming:
            if item.kind is EvidenceKind.PREDICTION:
                continue
            key = item.source_key or item.evidence_id
            if item.evidence_id in support_ids:
                support_keys.add(key)
            if item.evidence_id in contradict_ids:
                contradict_keys.add(key)
        return (
            max(len(support_keys), current.independent_support),
            max(len(contradict_keys), current.independent_contradiction),
            tuple(sorted(support_keys)),
            tuple(sorted(contradict_keys)),
        )

    def _support_kind(
        self,
        supporting: Sequence[str],
        incoming: Sequence[EvidenceReference],
        current: Hypothesis,
    ) -> str:
        """Whether the support behind a hypothesis is observed or only inferred.

        A PREDICTION is excluded, exactly as ``_independent_counts`` excludes it: a
        hypothesis's own recorded expectation is not support for it, and counting it
        here made a hypothesis that had only ever been CONTRADICTED look like one
        whose support was "inferred" — which is how a contradicted explanation stayed
        open and kept a cycle spending its budget on the same intervention.
        """
        observed = {
            item.evidence_id: item.kind
            for item in incoming
            if item.evidence_id in set(supporting) and item.kind is not EvidenceKind.PREDICTION
        }
        if not observed:
            return "recorded"
        if any(kind is EvidenceKind.OBSERVATION for kind in observed.values()):
            return "observed"
        return "inferred"

    def _confidence(
        self, current: Hypothesis, support: int, contradiction: int
    ) -> tuple[float | None, ConfidenceKind]:
        """A labelled heuristic, or ``None`` when nothing has been observed."""
        if support == 0 and contradiction == 0:
            return None, ConfidenceKind.INSUFFICIENT_EVIDENCE
        weighted_support = support * self.policy.support_weight
        weighted_contradiction = contradiction * self.policy.contradiction_weight
        prior = 0.0
        if current.prior_confidence is not None and self.policy.prior_weight > 0:
            prior = current.prior_confidence * self.policy.prior_weight
        # Laplace-smoothed ratio of independent evidence. With one support and no
        # contradiction this is 0.75, not 1.0, because one observation is not proof.
        value = (weighted_support + prior + 0.5) / (
            weighted_support + weighted_contradiction + prior + 1.0
        )
        return clamp(value, 0.0, 1.0), ConfidenceKind.HEURISTIC

    def _status(
        self,
        support: int,
        contradiction: int,
        confidence: float | None,
        support_kind: str,
    ) -> tuple[HypothesisStatus, str]:
        """The status lines, stated so a report can quote why."""
        if support == 0 and contradiction == 0:
            return HypothesisStatus.TESTABLE, "no evidence yet; an experiment would test this"
        if support > 0 and contradiction > 0:
            return (
                HypothesisStatus.INCONCLUSIVE,
                f"{support} independent source(s) support and {contradiction} contradict "
                "this hypothesis; the conflict is kept rather than resolved by majority",
            )
        if support_kind == "inferred":
            return (
                HypothesisStatus.INCONCLUSIVE,
                "the support for this hypothesis is inferred, not directly observed",
            )
        if contradiction > 0:
            if confidence is not None and confidence <= self.policy.contradicted_below:
                return (
                    HypothesisStatus.CONTRADICTED,
                    f"{contradiction} independent source(s) contradict this hypothesis",
                )
            return (
                HypothesisStatus.INCONCLUSIVE,
                f"{contradiction} independent source(s) contradict this hypothesis without "
                "settling it",
            )
        if support >= self.policy.min_independent_support and (
            confidence is not None and confidence >= self.policy.promoted_at
        ):
            return (
                HypothesisStatus.SUPPORTED,
                f"{support} independent source(s) support this hypothesis",
            )
        return (
            HypothesisStatus.INCONCLUSIVE,
            f"{support} independent source(s) support this hypothesis, which is below the "
            f"stated threshold ({self.policy.promoted_at})",
        )


def _bounded(
    supporting: list[str], contradicting: list[str], inconclusive: list[str]
) -> tuple[list[str], list[str], list[str], int]:
    """Hold each evidence list to its ceiling, dropping the oldest references."""
    dropped = 0
    total = len(supporting) + len(contradicting) + len(inconclusive)
    if total <= MAX_EVIDENCE_PER_HYPOTHESIS:
        return supporting, contradicting, inconclusive, 0
    overflow = total - MAX_EVIDENCE_PER_HYPOTHESIS
    for bucket in (inconclusive, supporting, contradicting):
        while overflow > 0 and bucket:
            bucket.pop(0)
            overflow -= 1
            dropped += 1
    return supporting, contradicting, inconclusive, dropped


def _with_version(
    hypothesis: Hypothesis, *, reason: str, evidence_ids: tuple[str, ...] = ()
) -> Hypothesis:
    """Append this revision to the hypothesis' history — the auditability rule (§7)."""
    version = HypothesisVersion(
        version=hypothesis.version,
        status=hypothesis.status,
        confidence=hypothesis.confidence,
        confidence_kind=hypothesis.confidence_kind,
        statement=hypothesis.statement,
        reason=reason,
        timestamp=hypothesis.updated_at or now_iso(),
        evidence_ids=evidence_ids,
    )
    # ``replace`` rather than a hand-written field list: a field-by-field copy
    # silently drops whatever field was added later, which is how the per-role
    # source keys never reached the record.
    return with_updates(hypothesis, versions=(*hypothesis.versions, version))


def _revise(
    hypothesis: Hypothesis,
    *,
    status: HypothesisStatus,
    reason: str,
    timestamp: str = "",
    superseded_by: str = "",
) -> Hypothesis:
    """A status change, with the previous revision kept in ``versions``."""
    changed = with_updates(
        hypothesis,
        status=status,
        version=hypothesis.version + 1,
        superseded_by=superseded_by or hypothesis.superseded_by,
        updated_at=timestamp or now_iso(),
    )
    return _with_version(changed, reason=reason)


def declared_candidates(
    environment: Any, *, exclude: Collection[str] = ()
) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    """The candidates an environment declares, read WITHOUT creating anything.

    Separate from :func:`candidate_hypotheses` so a PREVIEW can show what the cycle
    would propose without writing a hypothesis into the book: "show me what you would
    do" must not change what the record says was done.
    """
    producer = getattr(environment, "candidates", None)
    if not callable(producer):
        return ()
    rows: Any = producer()
    if not isinstance(rows, Sequence):
        return ()
    seen = {as_text(item) for item in exclude}
    out: list[tuple[str, Mapping[str, Any]]] = []
    for row in rows:
        if not isinstance(row, Sequence) or len(row) != 2:
            continue
        statement, predicted = row
        text = as_text(statement)
        if not text or not isinstance(predicted, Mapping) or text in seen:
            continue
        seen.add(text)
        out.append((text, dict(predicted)))
    return tuple(out)


def preview_hypotheses(
    environment: Any,
    *,
    objective_id: str,
    scope: str = "",
    exclude: Collection[str] = (),
) -> tuple[Hypothesis, ...]:
    """The hypotheses the cycle WOULD propose — value objects, never stored.

    Their ids are labelled ``preview_`` so a record that leaks into a report cannot
    be mistaken for something the book holds.
    """
    timestamp = now_iso()
    out: list[Hypothesis] = []
    for index, (statement, predicted) in enumerate(
        declared_candidates(environment, exclude=exclude), start=1
    ):
        out.append(
            Hypothesis(
                hypothesis_id=f"preview_{index}",
                objective_id=objective_id,
                statement=statement,
                scope=scope,
                predicted=predicted,
                status=HypothesisStatus.PROPOSED,
                created_at=timestamp,
                updated_at=timestamp,
                limitations=("declared by the environment as a candidate, not observed",),
            )
        )
    return tuple(out)


def candidate_hypotheses(
    environment: Any,
    *,
    objective_id: str,
    book: HypothesisBook,
    scope: str = "",
    exclude: Collection[str] = (),
) -> tuple[Hypothesis, ...]:
    """Propose the explanations an environment DECLARES for its own hidden rules.

    This is the honest form of "form a hypothesis": the candidate space comes from
    the environment, so nothing is invented, and an environment that declares no
    candidates produces no hypotheses at all rather than a guess (§4.2/§5).

    ``exclude`` names statements this objective has ALREADY been offered. Re-proposing
    the same declared candidate would give the cycle a fresh hypothesis id for a
    question it already asked, which is how a bounded loop turns into a loop that
    repeats itself until the budget runs out.
    """
    return tuple(
        book.propose(
            objective_id=objective_id,
            statement=statement,
            predicted=predicted,
            scope=scope,
            limitations=("declared by the environment as a candidate, not observed",),
        )
        for statement, predicted in declared_candidates(environment, exclude=exclude)
    )
