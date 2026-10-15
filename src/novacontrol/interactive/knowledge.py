"""Knowledge updates — turning evidence into a scoped, reversible rule.

A ``Hypothesis`` is a question; a ``LearnedRule`` is what the system will reason with
afterwards. The step between them is deliberately narrow and loud:

* **A threshold, stated.** A hypothesis becomes a rule only after
  ``support_threshold`` **independent** supporting observations. One successful
  experiment never produces a rule, and the reason a promotion did not happen is
  reported with the counts that stopped it (§7/§8).
* **Contradiction blocks promotion.** A hypothesis with ``contradiction_threshold``
  independent contradictions is not promoted, however much support it also has.
  Contradictory evidence is never discarded to make a rule fit.
* **Scope is carried, not assumed.** The rule records the environment it was tested
  in. ``generalizes_to`` stays empty until a held-out confirmation in another scope
  arrives, and ``RuleStatus`` distinguishes "provisional" from "confirmed" rather
  than pretending one experiment settled it.
* **Every change is recorded and reversible.** ``RuleBook.updates`` is the log, and
  ``revert`` walks one back — the previous version stays readable (§7/§13).

The book stores declarative facts only: a rule's ``condition``/``outcome`` are
key/value mappings, never code, so a learned rule can never become a second
unreviewed way to act.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from novacontrol.interactive.models import (
    MAX_RULES,
    ConfidenceKind,
    Hypothesis,
    KnowledgeChange,
    KnowledgeUpdate,
    LearnedRule,
    RuleStatus,
    as_float,
    as_int,
    as_text,
    new_id,
    now_iso,
)
from novacontrol.interactive.models import (
    with_updates as _replace,
)
from novacontrol.interactive.models import with_updates as _replace_update

__all__ = [
    "DEFAULT_CONTRADICTION_THRESHOLD",
    "DEFAULT_SUPPORT_THRESHOLD",
    "RuleBook",
]

#: Independent supporting observations needed before a hypothesis becomes a rule.
DEFAULT_SUPPORT_THRESHOLD = 3

#: Independent contradictions that stop a promotion outright.
DEFAULT_CONTRADICTION_THRESHOLD = 2

_RULE_LIMITATIONS = (
    "a learned rule is a summary of experiments run so far, not a law",
    "the rule holds in the scope it was tested in",
)


class RuleBook:
    """The learned rules, their evidence, and the log of every change to them."""

    def __init__(
        self,
        *,
        rules: tuple[LearnedRule, ...] = (),
        support_threshold: int = DEFAULT_SUPPORT_THRESHOLD,
        contradiction_threshold: int = DEFAULT_CONTRADICTION_THRESHOLD,
    ) -> None:
        self._rules: dict[str, LearnedRule] = {rule.rule_id: rule for rule in rules}
        self.support_threshold = max(1, support_threshold)
        self.contradiction_threshold = max(1, contradiction_threshold)
        self.updates: list[KnowledgeUpdate] = []
        self._by_update: dict[str, KnowledgeUpdate] = {}

    # ── reads ────────────────────────────────────────────────────────────

    @property
    def rules(self) -> tuple[LearnedRule, ...]:
        return tuple(self._rules.values())

    def get(self, rule_id: str) -> LearnedRule | None:
        return self._rules.get(rule_id)

    def for_objective(self, objective_id: str) -> tuple[LearnedRule, ...]:
        return tuple(rule for rule in self._rules.values() if rule.objective_id == objective_id)

    def active(self) -> tuple[LearnedRule, ...]:
        return tuple(rule for rule in self._rules.values() if rule.status is not RuleStatus.RETIRED)

    def predict(
        self, action: str, *, scope: str = ""
    ) -> tuple[Mapping[str, Any], LearnedRule] | None:
        """What is already known about an action, if anything — the reuse path (§8)."""
        best: tuple[Mapping[str, Any], LearnedRule] | None = None
        for rule in self._rules.values():
            if rule.status is RuleStatus.RETIRED:
                continue
            if scope and rule.scope and rule.scope != scope:
                continue
            outcome = rule.predicts(action)
            if outcome:
                best = (outcome, rule)
        return best

    def rule_id_for(self, hypothesis_id: str, scope: str) -> str:
        """A deterministic id, so re-promoting one hypothesis UPDATES its rule."""
        source = f"{hypothesis_id}:{scope}"
        clean = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in source)
        return f"rule:{clean.lower()}"[:120]

    def absorb(self, other: RuleBook) -> int:
        """Merge another book's rules and update log — the restart door's merge.

        Restore walks one stored payload per objective, and each payload carries the
        whole book as it stood when that objective was last written. Assigning the
        last one READ would silently drop a rule another objective learned later, so
        rules merge by id (the higher ``version`` wins) and updates deduplicate by
        id. Returns how many rules were added or replaced.
        """
        merged = 0
        for rule in other._rules.values():
            current = self._rules.get(rule.rule_id)
            if current is None or rule.version > current.version:
                self._rules[rule.rule_id] = rule
                merged += 1
        for update in other.updates:
            if update.update_id and update.update_id in self._by_update:
                continue
            self.updates.append(update)
            if update.update_id:
                self._by_update[update.update_id] = update
        if len(self.updates) > 64:
            self.updates = self.updates[-64:]
        return merged

    # ── writes ───────────────────────────────────────────────────────────

    def promote(
        self,
        hypothesis: Hypothesis,
        *,
        environment_id: str,
        action: str,
        expected: Mapping[str, Any],
        statement: str = "",
        experiment_id: str = "",
        evidence_id: str = "",
        source_key: str = "",
        now: str = "",
    ) -> KnowledgeUpdate:
        """Try to turn a hypothesis into a rule, or say why it stays a hypothesis.

        Nothing is promoted on the strength of one experiment, and nothing is
        promoted over the objection of independent contradictory evidence.
        """
        stamp = now or now_iso()
        scope = hypothesis.scope or environment_id
        target = self.rule_id_for(hypothesis.hypothesis_id, scope)
        if hypothesis.independent_contradiction >= self.contradiction_threshold:
            return self._record(
                KnowledgeUpdate(
                    update_id=new_id("kup"),
                    change=KnowledgeChange.NO_CHANGE,
                    target_id=target,
                    reason=(
                        f"not promoted: {hypothesis.independent_contradiction} independent "
                        f"observation(s) contradict it, at the refusal threshold "
                        f"({self.contradiction_threshold})"
                    ),
                    evidence_ids=tuple(x for x in (evidence_id,) if x),
                    experiment_id=experiment_id,
                    previous_status=hypothesis.status.value,
                    new_status=hypothesis.status.value,
                    timestamp=stamp,
                    reversible=False,
                )
            )
        if hypothesis.independent_support < self.support_threshold:
            return self._record(
                KnowledgeUpdate(
                    update_id=new_id("kup"),
                    change=KnowledgeChange.NO_CHANGE,
                    target_id=target,
                    reason=(
                        f"not promoted: {hypothesis.independent_support} independent supporting "
                        f"observation(s), below the threshold ({self.support_threshold}); a single "
                        "experiment does not establish a rule"
                    ),
                    evidence_ids=tuple(x for x in (evidence_id,) if x),
                    experiment_id=experiment_id,
                    previous_status=hypothesis.status.value,
                    new_status=hypothesis.status.value,
                    timestamp=stamp,
                    reversible=False,
                )
            )
        existing = self._rules.get(target)
        if existing is not None and existing.status is RuleStatus.CONFIRMED:
            return self._record(
                KnowledgeUpdate(
                    update_id=new_id("kup"),
                    change=KnowledgeChange.NO_CHANGE,
                    target_id=target,
                    reason="the rule already exists as a confirmed rule; nothing was rewritten",
                    previous_version=existing.version,
                    new_version=existing.version,
                    timestamp=stamp,
                    reversible=False,
                )
            )
        if existing is None and len(self._rules) >= MAX_RULES:
            return self._record(
                KnowledgeUpdate(
                    update_id=new_id("kup"),
                    change=KnowledgeChange.NO_CHANGE,
                    target_id=target,
                    reason=f"not promoted: the rule ceiling ({MAX_RULES}) is reached",
                    timestamp=stamp,
                    reversible=False,
                )
            )
        supporting = _merge(existing.supporting if existing else (), hypothesis.supporting)
        contradicting = _merge(existing.contradicting if existing else (), hypothesis.contradicting)
        rule = LearnedRule(
            rule_id=target,
            objective_id=hypothesis.objective_id,
            statement=statement or hypothesis.statement,
            condition={"action": action, "scope": scope},
            outcome=dict(expected),
            scope=scope,
            generalizes_to=existing.generalizes_to if existing else (),
            status=RuleStatus.PROVISIONAL,
            version=1 if existing is None else existing.version + 1,
            supporting=supporting,
            contradicting=contradicting,
            independent_support=hypothesis.independent_support,
            experiments=_merge(
                existing.experiments if existing else (),
                tuple(x for x in (experiment_id,) if x),
            ),
            provenance=_merge(
                existing.provenance if existing else (),
                (hypothesis.hypothesis_id, environment_id),
            ),
            created_at=existing.created_at if existing else stamp,
            updated_at=stamp,
            limitations=_RULE_LIMITATIONS
            + (f"heuristic support count ({hypothesis.confidence_kind.value}); not a probability",),
        )
        self._rules[target] = rule
        return self._record(
            KnowledgeUpdate(
                update_id=new_id("kup"),
                change=KnowledgeChange.PROMOTE,
                target_id=target,
                reason=(
                    f"promoted after {hypothesis.independent_support} independent supporting "
                    f"observation(s) (threshold {self.support_threshold}) in scope {scope!r}"
                ),
                evidence_ids=tuple(x for x in (evidence_id,) if x),
                experiment_id=experiment_id,
                previous_version=existing.version if existing else 0,
                new_version=rule.version,
                previous_status=hypothesis.status.value,
                new_status="provisional",
                timestamp=stamp,
                limitations=_RULE_LIMITATIONS,
            )
        )

    def generalize(
        self, rule_id: str, *, scope: str, note: str = "", now: str = ""
    ) -> KnowledgeUpdate:
        """Record a held-out confirmation in another scope — the only path to CONFIRMED."""
        stamp = now or now_iso()
        rule = self._rules.get(rule_id)
        if rule is None:
            return self._record(
                KnowledgeUpdate(
                    update_id=new_id("kup"),
                    change=KnowledgeChange.NO_CHANGE,
                    target_id=rule_id,
                    reason=f"no rule {rule_id!r} to generalize",
                    timestamp=stamp,
                    reversible=False,
                )
            )
        if not scope or scope == rule.scope:
            return self._record(
                KnowledgeUpdate(
                    update_id=new_id("kup"),
                    change=KnowledgeChange.NO_CHANGE,
                    target_id=rule_id,
                    reason=(
                        "a scope that is the rule's own scope is not a held-out case, so the "
                        "rule stays provisional"
                    ),
                    timestamp=stamp,
                    reversible=False,
                )
            )
        updated = _replace(
            rule,
            status=RuleStatus.CONFIRMED,
            generalizes_to=_merge(rule.generalizes_to, (scope,)),
            version=rule.version + 1,
            updated_at=stamp,
            limitations=rule.limitations
            + (f"confirmed in {scope!r}; that is one more scope, not all scopes",),
        )
        self._rules[rule_id] = updated
        return self._record(
            KnowledgeUpdate(
                update_id=new_id("kup"),
                change=KnowledgeChange.REVISE,
                target_id=rule_id,
                reason=f"held-out confirmation in {scope!r}" + (f": {note}" if note else ""),
                previous_version=rule.version,
                new_version=updated.version,
                previous_status=rule.status.value,
                new_status=updated.status.value,
                timestamp=stamp,
            )
        )

    def contradict(
        self,
        rule_id: str,
        *,
        evidence_id: str = "",
        experiment_id: str = "",
        reason: str = "",
        now: str = "",
    ) -> KnowledgeUpdate:
        """Record contradicting evidence against a rule. It is kept, not smoothed over."""
        stamp = now or now_iso()
        rule = self._rules.get(rule_id)
        if rule is None:
            return self._record(
                KnowledgeUpdate(
                    update_id=new_id("kup"),
                    change=KnowledgeChange.NO_CHANGE,
                    target_id=rule_id,
                    reason=f"no rule {rule_id!r} to contradict",
                    timestamp=stamp,
                    reversible=False,
                )
            )
        contradicting = _merge(rule.contradicting, tuple(x for x in (evidence_id,) if x))
        status = rule.status
        if len(contradicting) >= self.contradiction_threshold:
            status = RuleStatus.RETIRED
        updated = _replace(
            rule,
            contradicting=contradicting,
            status=status,
            version=rule.version + 1,
            updated_at=stamp,
        )
        self._rules[rule_id] = updated
        return self._record(
            KnowledgeUpdate(
                update_id=new_id("kup"),
                change=KnowledgeChange.CONTRADICT,
                target_id=rule_id,
                reason=reason
                or (
                    f"contradicting evidence recorded ({len(contradicting)} of "
                    f"{self.contradiction_threshold} before retirement)"
                ),
                evidence_ids=tuple(x for x in (evidence_id,) if x),
                experiment_id=experiment_id,
                previous_version=rule.version,
                new_version=updated.version,
                previous_status=rule.status.value,
                new_status=updated.status.value,
                timestamp=stamp,
            )
        )

    def retire(self, rule_id: str, *, reason: str = "", now: str = "") -> KnowledgeUpdate:
        """Retire a rule. Retiring is not deleting: the record and its evidence stay."""
        stamp = now or now_iso()
        rule = self._rules.get(rule_id)
        if rule is None:
            return self._record(
                KnowledgeUpdate(
                    update_id=new_id("kup"),
                    change=KnowledgeChange.NO_CHANGE,
                    target_id=rule_id,
                    reason=f"no rule {rule_id!r} to retire",
                    timestamp=stamp,
                    reversible=False,
                )
            )
        updated = _replace(
            rule, status=RuleStatus.RETIRED, version=rule.version + 1, updated_at=stamp
        )
        self._rules[rule_id] = updated
        return self._record(
            KnowledgeUpdate(
                update_id=new_id("kup"),
                change=KnowledgeChange.RETIRE,
                target_id=rule_id,
                reason=reason or "retired; the evidence behind it is kept",
                previous_version=rule.version,
                new_version=updated.version,
                previous_status=rule.status.value,
                new_status=updated.status.value,
                timestamp=stamp,
            )
        )

    def revert(self, update_id: str, *, now: str = "") -> KnowledgeUpdate:
        """Undo one recorded change — the reversibility §7 asks for, recorded itself."""
        stamp = now or now_iso()
        original = self._by_update.get(update_id)
        if original is None:
            return self._record(
                KnowledgeUpdate(
                    update_id=new_id("kup"),
                    change=KnowledgeChange.NO_CHANGE,
                    target_id=update_id,
                    reason=f"no recorded change {update_id!r}",
                    timestamp=stamp,
                    reversible=False,
                )
            )
        if original.change is KnowledgeChange.NO_CHANGE:
            return self._record(
                KnowledgeUpdate(
                    update_id=new_id("kup"),
                    change=KnowledgeChange.NO_CHANGE,
                    target_id=original.target_id,
                    reason="nothing changed, so there is nothing to revert",
                    timestamp=stamp,
                    reversible=False,
                )
            )
        rule = self._rules.get(original.target_id)
        reverted = KnowledgeUpdate(
            update_id=new_id("kup"),
            change=KnowledgeChange.REVISE,
            target_id=original.target_id,
            reason=f"reverted {original.change.value} ({original.update_id})",
            previous_version=rule.version if rule else original.new_version,
            new_version=max(0, original.previous_version),
            previous_status=rule.status.value if rule else "",
            new_status=original.previous_status,
            timestamp=stamp,
        )
        if rule is not None:
            if original.change is KnowledgeChange.PROMOTE:
                self._rules[original.target_id] = _replace(
                    rule,
                    status=RuleStatus.RETIRED,
                    version=rule.version + 1,
                    updated_at=stamp,
                    limitations=rule.limitations + ("reverted to the pre-promotion state",),
                )
            else:
                self._rules[original.target_id] = _replace(
                    rule,
                    version=rule.version + 1,
                    updated_at=stamp,
                )
        self._mark_reverted(original)
        return self._record(reverted)

    # ── persistence ──────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "rules": [rule.to_dict() for rule in self._rules.values()],
            "updates": [item.to_dict() for item in self.updates[-64:]],
            "support_threshold": self.support_threshold,
            "contradiction_threshold": self.contradiction_threshold,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> RuleBook:
        raw = data.get("rules")
        rules = tuple(
            LearnedRule.from_mapping(item)
            for item in (raw if isinstance(raw, list) else [])
            if isinstance(item, Mapping) and as_text(item.get("rule_id"))
        )
        book = cls(
            rules=rules[:MAX_RULES],
            support_threshold=as_int(data.get("support_threshold"), DEFAULT_SUPPORT_THRESHOLD),
            contradiction_threshold=as_int(
                data.get("contradiction_threshold"), DEFAULT_CONTRADICTION_THRESHOLD
            ),
        )
        raw_updates = data.get("updates")
        for item in raw_updates if isinstance(raw_updates, list) else []:
            if isinstance(item, Mapping):
                update = KnowledgeUpdate.from_mapping(item)
                book.updates.append(update)
                if update.update_id:
                    book._by_update[update.update_id] = update
        return book

    # ── internals ────────────────────────────────────────────────────────

    def _record(self, update: KnowledgeUpdate) -> KnowledgeUpdate:
        self.updates.append(update)
        if update.update_id:
            self._by_update[update.update_id] = update
        if len(self.updates) > 512:
            self.updates = self.updates[-256:]
        return update

    def _mark_reverted(self, original: KnowledgeUpdate) -> None:
        reverted = _replace_update(original, reverted=True, reversible=False)
        self._by_update[original.update_id] = reverted
        self.updates = [
            reverted if item.update_id == original.update_id else item for item in self.updates
        ]


def _merge(existing: tuple[str, ...], added: tuple[str, ...]) -> tuple[str, ...]:
    """Additions that are not already present, order preserved — no duplicate evidence."""
    out = list(existing)
    for item in added:
        if item and item not in out:
            out.append(item)
    return tuple(out)


def confidence_of(support: int, contradiction: int) -> tuple[float | None, ConfidenceKind]:
    """A labelled heuristic confidence for a support/contradiction count.

    ``None`` means "not enough evidence to say", which is the honest answer for a
    hypothesis nobody has tested. The number is explicitly a heuristic on the
    ``ConfidenceKind`` it is returned with — it is not a probability.
    """
    if support + contradiction == 0:
        return None, ConfidenceKind.INSUFFICIENT_EVIDENCE
    ratio = support / float(support + contradiction)
    weight = min(1.0, (support + contradiction) / 3.0)
    return round(0.5 + (ratio - 0.5) * weight, 4), ConfidenceKind.HEURISTIC


def support_ratio(rule: LearnedRule) -> float | None:
    """How much of a rule's evidence agrees with it, or ``None`` when there is none."""
    support = float(rule.independent_support)
    contradiction = float(len(rule.contradicting))
    if support + contradiction <= 0:
        return None
    return as_float(round(support / (support + contradiction), 4))
