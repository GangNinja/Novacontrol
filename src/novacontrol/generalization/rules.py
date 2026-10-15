"""Rules, and the bounded search that infers one from examples.

A :class:`CandidateRule` is one or two :class:`RuleStep` values — an operator
name and its parameters — whose parameters were inferred from the example
pairs. :func:`fit_rules` is the whole inference algorithm, and it is small on
purpose:

1. every operator proposes the parameters that plausibly explain the examples
   (a cheap filter whose only job is to keep the search finite);
2. a candidate is BELIEVED only after it reproduces **every** example pair
   exactly — a rule that explains one pair and not the second is not a rule;
3. if nothing fits at depth 1, a second operator is fitted to the RESIDUAL of
   each survivor (its output versus the expected output), which is how
   "compose two supported transformations" is supported without an exponential
   search: compositions are generated from a first step that already fits part
   of the structure, never from the cross product of the library.

Three properties are enforced here rather than hoped for:

* **Bounds.** ``max_candidates`` and ``max_depth`` are hard: exceeding them ends
  the search with ``exhausted=True``, and the caller reports a budget ending
  instead of a wrong answer.
* **The answer key is never consulted.** ``fit_rules`` only ever sees the
  example pairs. The held-out input is transformed, never compared to anything,
  so the search cannot fit its own test.
* **Ambiguity is retained.** When more than one rule survives, the caller
  compares their predictions; disagreement is reported as AMBIGUOUS rather than
  resolved by an arbitrary tie-break.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from novacontrol.generalization.grids import Grid
from novacontrol.generalization.transforms import (
    OPERATORS,
    GridPair,
    operator,
)

__all__ = [
    "MAX_DEPTH",
    "CandidateRule",
    "RuleStep",
    "SearchOutcome",
    "fit_rules",
    "rule_from_mapping",
    "rule_to_mapping",
]

#: The deepest composition this build supports. Two steps: "a supported
#: transformation, then another" is the phase's explicit limit, and a third step
#: multiplies the candidate space without adding a family the report could
#: honestly claim.
MAX_DEPTH = 2

#: The default candidate ceiling for one task.
DEFAULT_MAX_CANDIDATES = 400


@dataclass(frozen=True, slots=True)
class RuleStep:
    """One operator with its inferred parameters."""

    operator: str
    params: tuple[Any, ...] = ()

    def apply(self, grid: Grid) -> Grid:
        return operator(self.operator).apply(grid, self.params)

    def label(self) -> str:
        return operator(self.operator).label(self.params)

    def to_dict(self) -> dict[str, Any]:
        return {"operator": self.operator, "params": _jsonable(list(self.params))}

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> RuleStep:
        return cls(
            operator=str(data.get("operator") or ""),
            params=tuple(_thaw(item) for item in data.get("params") or ()),
        )


@dataclass(frozen=True, slots=True)
class CandidateRule:
    """A rule the search produced: its steps, its origin and its identifier."""

    steps: tuple[RuleStep, ...]

    @property
    def depth(self) -> int:
        return len(self.steps)

    @property
    def rule_id(self) -> str:
        return " then ".join(step.label() for step in self.steps)

    @property
    def families(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(operator(step.operator).family for step in self.steps))

    def apply(self, grid: Grid) -> Grid:
        result = grid
        for step in self.steps:
            result = step.apply(result)
        return result

    def matches(self, pairs: Sequence[GridPair]) -> bool:
        """Whether the rule reproduces EVERY pair exactly.

        An exception from an operator counts as a mismatch here; the caller
        distinguishes a defective transformation from a wrong answer by probing
        the rule separately (see :func:`rule_is_total`).
        """
        for source, target in pairs:
            try:
                produced = self.apply(source)
            except Exception:  # noqa: BLE001 - a raising rule is not a match
                return False
            if produced.cells != target.cells:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "depth": self.depth,
            "families": list(self.families),
            "steps": [step.to_dict() for step in self.steps],
        }


def rule_from_mapping(data: dict[str, Any]) -> CandidateRule:
    """Rebuild a rule from its stored form, refusing an unknown operator.

    Refused as a ``ValueError`` with the known names attached, because this is
    a caller reading PAYLOAD — a stored record naming a transformation this
    build does not have is bad input, and ``KeyError`` (what the operator table
    itself raises) would read as "the key you asked for is missing" rather than
    "this record is not one this build can run".
    """
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("a stored rule has no steps")
    steps = tuple(RuleStep.from_mapping(item) for item in raw_steps if isinstance(item, dict))
    for step in steps:
        try:
            operator(step.operator)  # raises with the known names when unknown
        except KeyError as exc:
            raise ValueError(str(exc.args[0]) if exc.args else str(exc)) from exc
    return CandidateRule(steps=steps)


def rule_to_mapping(rule: CandidateRule) -> dict[str, Any]:
    return rule.to_dict()


def rule_is_total(rule: CandidateRule, grid: Grid) -> tuple[bool, str]:
    """Whether the rule can be applied at all, and why not when it cannot.

    This separates a WRONG answer from a broken transformation: a rule that
    raises is a defect in this build, while a rule that returns the wrong grid
    is a hypothesis that failed.
    """
    try:
        rule.apply(grid)
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """What the search found, and what it spent finding it."""

    fitting: tuple[CandidateRule, ...] = ()
    examined: int = 0
    exhausted: bool = True
    reason: str = ""

    @property
    def ambiguous(self) -> bool:
        return len(self.fitting) > 1


def _propose_for(pairs: Sequence[GridPair], *, remaining: int) -> tuple[CandidateRule, ...]:
    """First-order candidates from every operator's own proposals."""
    candidates: list[CandidateRule] = []
    for operator_ in OPERATORS:
        if len(candidates) >= remaining:
            break
        try:
            proposals = operator_.propose(pairs)
        except Exception:  # noqa: BLE001 - a proposal that raises proposes nothing
            continue
        for params in proposals:
            candidates.append(CandidateRule(steps=(RuleStep(operator_.name, tuple(params)),)))
            if len(candidates) >= remaining:
                break
    return tuple(candidates)


def fit_rules(
    pairs: Sequence[GridPair],
    *,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    max_depth: int = MAX_DEPTH,
) -> SearchOutcome:
    """Every rule of depth ≤ ``max_depth`` that reproduces all ``pairs`` exactly.

    Deterministic: the operator table's order decides candidate order, and the
    returned tuple is in that order, so two runs of the same search produce the
    same list — which is what makes a stored run reproducible.
    """
    if not pairs:
        return SearchOutcome(fitting=(), examined=0, exhausted=False, reason="no examples")
    limit = max(1, int(max_candidates))
    first = _propose_for(pairs, remaining=limit)
    examined = len(first)
    fitting = tuple(candidate for candidate in first if candidate.matches(pairs))
    if fitting or max_depth < 2 or examined >= limit:
        return SearchOutcome(
            fitting=fitting,
            examined=examined,
            exhausted=examined >= limit and not fitting,
            reason="" if fitting else "no single transformation reproduced every example",
        )

    # Depth 2: fit a second step to the RESIDUAL of a first step that did not
    # finish the job on its own.
    compositions: list[CandidateRule] = []
    for candidate in first:
        if examined >= limit:
            break
        residuals: list[GridPair] = []
        usable = True
        for source, target in pairs:
            try:
                residuals.append((candidate.apply(source), target))
            except Exception:  # noqa: BLE001 - an unusable first step composes with nothing
                usable = False
                break
        if not usable:
            continue
        for second in _propose_for(residuals, remaining=limit - examined):
            examined += 1
            composed = CandidateRule(steps=(*candidate.steps, second.steps[0]))
            if composed.matches(pairs):
                compositions.append(composed)
            if examined >= limit:
                break
    return SearchOutcome(
        fitting=tuple(compositions),
        examined=examined,
        exhausted=examined >= limit and not compositions,
        reason=""
        if compositions
        else "no composition of two transformations reproduced every example",
    )


def distinct_predictions(rules: Iterable[CandidateRule], grid: Grid) -> tuple[tuple[str, ...], ...]:
    """The distinct grids the surviving rules predict, as fingerprints.

    Two rules that agree are not an ambiguity about the ANSWER, however
    different their spelling is; two that disagree are, and the caller reports
    it instead of picking one.
    """
    seen: list[tuple[str, ...]] = []
    for rule in rules:
        try:
            produced = rule.apply(grid)
        except Exception:  # noqa: BLE001 - a rule that cannot run joins no group
            continue
        fingerprint = tuple("".join(str(cell) for cell in row) for row in produced.cells)
        if fingerprint not in seen:
            seen.append(fingerprint)
    return tuple(seen)


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_thaw(item) for item in value)
    return value
