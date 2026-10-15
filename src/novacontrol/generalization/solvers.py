"""The solvers — one per task family, plus the baselines to compare them with.

Every solver is a pure function of the TASK it is given: it reads the examples,
produces an answer for the task's test input, and never sees the expected
output. Nothing here is tuned per task, and nothing is looked up by task id —
a solver that read an answer key would make every metric below meaningless, so
the evaluator keeps the key and the solver's inputs are the examples only.

The six families and what solves each:

======================  =====================================  ==================
family                   how it is solved                       verifier
======================  =====================================  ==================
``grid_transform``       bounded rule inference (§5/§6)         ``exact_grid_match``
``spatial_relation``     objects + geometry off the parsed grid  ``relation_holds``
``state_transition``     the transition inferred from examples   ``state_equals``
``small_planning``       bounded breadth-first search            ``plan_reaches_goal``
``tool_selection``       example-weighted token overlap          ``tool_matches``
``multi_step_reasoning`` the sequence rule inferred from examples ``chain_matches``
======================  =====================================  ==================

Baselines exist for the same reason: a number only means something next to
another number measured on the same tasks with the same budget.
"""

from __future__ import annotations

import random
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.generalization.grids import (
    BBox,
    Grid,
    GridObject,
    InvalidGrid,
    connectivity_labels,
    parse_grid,
)
from novacontrol.generalization.models import (
    EvaluationTask,
    FailureCategory,
    RuleSource,
    TaskKind,
    TaskStatus,
)
from novacontrol.generalization.rules import (
    DEFAULT_MAX_CANDIDATES,
    MAX_DEPTH,
    CandidateRule,
    RuleStep,
    distinct_predictions,
    fit_rules,
    rule_is_total,
)
from novacontrol.generalization.transforms import OPERATORS, GridPair

__all__ = [
    "BASELINES",
    "Budget",
    "BudgetExceeded",
    "SOLVERS",
    "SolveOutcome",
    "solve",
]

#: Named solvers. ``infer`` is this build's candidate; the others are baselines
#: a comparison needs in order to mean anything.
SOLVERS: tuple[str, ...] = ("infer",)

#: Named baselines, each documented in the report with what it can and cannot do.
BASELINES: tuple[str, ...] = ("identity", "random_rule", "no_answer")


class BudgetExceeded(RuntimeError):
    """The solver ran out of the budget it was given — never a wrong answer."""


#: The planning family's own exploration cap. Separate from the time budget on
#: purpose: a search that stops because it hit this says so, rather than
#: reporting unreachability it did not prove.
MAX_PLANNING_STATES = 4000


@dataclass(frozen=True, slots=True)
class Budget:
    """The limits one solve may spend, and the clock it must finish inside."""

    max_candidates: int = DEFAULT_MAX_CANDIDATES
    max_depth: int = MAX_DEPTH
    max_seconds: float = 5.0
    max_model_calls: int = 0
    started: float = field(default_factory=time.monotonic)

    def remaining_seconds(self) -> float:
        return self.max_seconds - (time.monotonic() - self.started)

    def check(self) -> None:
        """Raise when the clock is spent. Called between bounded steps only."""
        if self.max_seconds > 0 and self.remaining_seconds() <= 0:
            raise BudgetExceeded(
                f"the solver's {self.max_seconds:g}s budget is spent; no answer is "
                "reported rather than a partial one"
            )


@dataclass(frozen=True, slots=True)
class SolveOutcome:
    """What a solver produced, before the evaluator judges it.

    ``SOLVED`` here means the solver PRODUCED AN ANSWER. Whether the answer is
    right is not the solver's claim to make: the evaluator holds the expected
    output, verifies against it, and downgrades ``SOLVED`` to ``FAILED`` (with a
    classified reason) whenever verification rejects the answer. A solver that
    cannot answer says so instead — ``UNSUPPORTED``, ``AMBIGUOUS``,
    ``BUDGET_EXHAUSTED`` or ``INVALID`` — and those are never downgraded into a
    failure, because none of them is a wrong answer.
    """

    status: TaskStatus
    predicted: Any = None
    candidate: str = ""
    source: RuleSource = RuleSource.NONE
    failure: FailureCategory | None = None
    failure_reason: str = ""
    candidates_examined: int = 0
    candidates_fitting: int = 0
    search_depth: int = 0
    evidence: tuple[str, ...] = ()
    rule_ambiguous: bool = False


# --------------------------------------------------------------------------- #
# Family 1: grid transformation (the ARC-style inference)
# --------------------------------------------------------------------------- #


def _grid_pairs(task: EvaluationTask) -> tuple[GridPair, ...]:
    """The example pairs. Raises :class:`InvalidGrid` on an unreadable one."""
    pairs: list[GridPair] = []
    for example in task.examples:
        source = parse_grid(example.inputs.get("grid"))
        if example.expected is None:
            continue
        pairs.append((source, parse_grid(example.expected)))
    return tuple(pairs)


def solve_grid(task: EvaluationTask, budget: Budget) -> SolveOutcome:
    """Infer a rule from the examples, then apply it to the unseen input."""
    try:
        pairs = _grid_pairs(task)
        test_input = parse_grid(task.test_input.get("grid"))
    except InvalidGrid as exc:
        return SolveOutcome(
            status=TaskStatus.INVALID,
            failure=FailureCategory.INPUT_PARSING_FAILURE,
            failure_reason=str(exc),
        )
    if not pairs:
        return SolveOutcome(
            status=TaskStatus.INVALID,
            failure=FailureCategory.INVALID_TASK_DEFINITION,
            failure_reason="the task supplies no example with an output to infer from",
        )
    budget.check()
    outcome = fit_rules(pairs, max_candidates=budget.max_candidates, max_depth=budget.max_depth)
    budget.check()
    if not outcome.fitting:
        return SolveOutcome(
            status=TaskStatus.UNSUPPORTED,
            failure=FailureCategory.NO_CANDIDATE_RULE_FOUND,
            failure_reason=(
                outcome.reason
                or "no transformation in this build's library reproduced every example"
            ),
            candidates_examined=outcome.examined,
            evidence=(f"{len(pairs)} example pairs", f"{outcome.examined} candidates examined"),
        )

    # A rule that cannot even run is a defect in this build, reported as such.
    for rule in outcome.fitting:
        total, why = rule_is_total(rule, test_input)
        if not total:
            return SolveOutcome(
                status=TaskStatus.ERROR,
                candidate=rule.rule_id,
                failure=FailureCategory.TRANSFORMATION_DEFECT,
                failure_reason=f"the inferred transformation {rule.rule_id!r} raised: {why}",
                candidates_examined=outcome.examined,
                candidates_fitting=len(outcome.fitting),
            )

    predictions = distinct_predictions(outcome.fitting, test_input)
    simplest = _simplest(outcome.fitting)
    if len(predictions) > 1:
        # Two rules that fit every example and disagree on the unseen input: the
        # examples do not decide, and picking the first one would dress a guess
        # up as an inference.
        return SolveOutcome(
            status=TaskStatus.AMBIGUOUS,
            candidate=simplest.rule_id,
            failure=FailureCategory.MULTIPLE_PLAUSIBLE_RULES,
            failure_reason=(
                f"{len(outcome.fitting)} rules reproduce every example and give "
                f"{len(predictions)} different answers for the unseen input: "
                + "; ".join(rule.rule_id for rule in outcome.fitting[:4])
            ),
            candidates_examined=outcome.examined,
            candidates_fitting=len(outcome.fitting),
            search_depth=simplest.depth,
            rule_ambiguous=True,
            evidence=tuple(rule.rule_id for rule in outcome.fitting[:8]),
        )
    return SolveOutcome(
        status=TaskStatus.SOLVED,
        predicted=simplest.apply(test_input).rows(),
        candidate=simplest.rule_id,
        source=RuleSource.INFERRED,
        candidates_examined=outcome.examined,
        candidates_fitting=len(outcome.fitting),
        search_depth=simplest.depth,
        rule_ambiguous=len(outcome.fitting) > 1,
        evidence=(
            f"{len(outcome.fitting)} rule(s) reproduced all {len(pairs)} examples",
            *(rule.rule_id for rule in outcome.fitting[:8]),
        ),
    )


def _simplest(rules: Sequence[CandidateRule]) -> CandidateRule:
    """The shortest rule, ties broken by the operator table's order.

    Deterministic on purpose: the "simplest explanation" only has to be stable,
    and a stable choice is what makes a stored run reproducible.
    """
    order = {operator_.name: index for index, operator_ in enumerate(OPERATORS)}
    return min(
        rules,
        key=lambda rule: (
            rule.depth,
            tuple(order[step.operator] for step in rule.steps),
            rule.rule_id,
        ),
    )


# --------------------------------------------------------------------------- #
# Family 2: spatial relations over the parsed scene
# --------------------------------------------------------------------------- #

_RELATIONS: tuple[str, ...] = (
    "left_of",
    "right_of",
    "above",
    "below",
    "touching",
    "same_row",
    "same_column",
    "larger_than",
    "smaller_than",
    "inside",
)


def _object_of(grid: Grid, color: int) -> GridObject | None:
    """The largest connected component of one colour, or ``None`` if there is none.

    Measured from the components themselves rather than from
    :meth:`Grid.objects`, which deliberately excludes the background: a question
    about "the shape of colour 4" has an answer even when colour 4 is what a
    corners rule would call the background (a frame that touches all four
    corners does exactly that), and answering "no such shape" there would report
    a limitation this build does not have.
    """
    best: GridObject | None = None
    for cells, component_color in connectivity_labels(grid, connectivity=4):
        if component_color != color:
            continue
        rows = [row for row, _ in cells]
        columns = [column for _, column in cells]
        candidate = GridObject(
            cells=tuple(sorted(cells)),
            color=color,
            bbox=BBox(min(rows), min(columns), max(rows), max(columns)),
        )
        if best is None or (candidate.size, candidate.anchor) > (best.size, best.anchor):
            best = candidate
    return best


def solve_spatial(task: EvaluationTask, budget: Budget) -> SolveOutcome:
    """Answer a relation question from the grid's own geometry.

    The relation is measured, not guessed: the two objects are located by their
    colours, and the answer is the geometric fact between them — which is why
    this family has an objectively verifiable answer at all.
    """
    budget.check()
    inputs = task.test_input
    relation = str(inputs.get("relation") or "")
    if relation not in _RELATIONS:
        return SolveOutcome(
            status=TaskStatus.UNSUPPORTED,
            failure=FailureCategory.NO_CANDIDATE_RULE_FOUND,
            failure_reason=(
                f"the relation {relation!r} is not one this build measures; it knows "
                f"{', '.join(_RELATIONS)}"
            ),
        )
    try:
        grid = parse_grid(inputs.get("grid"))
    except InvalidGrid as exc:
        return SolveOutcome(
            status=TaskStatus.INVALID,
            failure=FailureCategory.INPUT_PARSING_FAILURE,
            failure_reason=str(exc),
        )
    try:
        left_color = int(inputs["a"])
        right_color = int(inputs["b"])
    except (KeyError, TypeError, ValueError) as exc:
        return SolveOutcome(
            status=TaskStatus.INVALID,
            failure=FailureCategory.INVALID_TASK_DEFINITION,
            failure_reason=f"the relation question names no two colours: {exc}",
        )
    first = _object_of(grid, left_color)
    second = _object_of(grid, right_color)
    if first is None or second is None:
        # Genuinely unknown: an object that is not there cannot be in a
        # relation, and answering "false" would invent a fact.
        return SolveOutcome(
            status=TaskStatus.UNSUPPORTED,
            failure=FailureCategory.VERIFICATION_FAILURE,
            failure_reason=(
                f"the grid has no object of colour {left_color if first is None else right_color}"
            ),
        )
    answer = _relation_holds(relation, first, second)
    return SolveOutcome(
        status=TaskStatus.SOLVED,
        predicted=answer,
        candidate=f"geometry:{relation}",
        source=RuleSource.DETERMINISTIC,
        candidates_examined=1,
        candidates_fitting=1,
        evidence=(
            f"{relation}({left_color},{right_color})={answer}",
            f"boxes {first.bbox.to_dict()} and {second.bbox.to_dict()}",
        ),
    )


def _relation_holds(relation: str, first: GridObject, second: GridObject) -> bool:
    if relation == "left_of":
        return first.bbox.right < second.bbox.left
    if relation == "right_of":
        return first.bbox.left > second.bbox.right
    if relation == "above":
        return first.bbox.bottom < second.bbox.top
    if relation == "below":
        return first.bbox.top > second.bbox.bottom
    if relation == "same_row":
        return first.bbox.top == second.bbox.top
    if relation == "same_column":
        return first.bbox.left == second.bbox.left
    if relation == "larger_than":
        return first.size > second.size
    if relation == "smaller_than":
        return first.size < second.size
    if relation == "inside":
        return (
            first.bbox.top >= second.bbox.top
            and first.bbox.left >= second.bbox.left
            and first.bbox.bottom <= second.bbox.bottom
            and first.bbox.right <= second.bbox.right
        )
    if relation == "touching":
        return _touching(first, second)
    raise ValueError(f"unhandled relation {relation!r}")


def _touching(first: GridObject, second: GridObject) -> bool:
    """Whether the two objects share an edge (4-adjacency)."""
    cells = set(second.cells)
    for row, column in first.cells:
        for delta_row, delta_column in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            if (row + delta_row, column + delta_column) in cells:
                return True
    return False


# --------------------------------------------------------------------------- #
# Family 3: state transition prediction
# --------------------------------------------------------------------------- #


def solve_state_transition(task: EvaluationTask, budget: Budget) -> SolveOutcome:
    """Infer one transition rule from the examples and apply it once.

    The rule is learned as a per-field DIFFERENCE (numeric fields move by a
    constant; boolean fields flip or hold; string fields repeat their mapped
    value). That is a real inference over the examples: the solver never sees
    the expected next state, and a field whose examples disagree is reported as
    ambiguous rather than filled with a guess.
    """
    budget.check()
    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]] = []
    for example in task.examples:
        if example.expected is None:
            continue
        state = example.inputs.get("state")
        action = example.inputs.get("action")
        if isinstance(state, Mapping) and isinstance(action, Mapping):
            pairs.append((dict(state), dict(action), _as_mapping(example.expected)))
    if not pairs:
        return SolveOutcome(
            status=TaskStatus.INVALID,
            failure=FailureCategory.INVALID_TASK_DEFINITION,
            failure_reason="no example carried a state, an action and a next state",
        )
    test_state = _as_mapping(task.test_input.get("state"))
    test_action = _as_mapping(task.test_input.get("action"))
    if not test_state:
        return SolveOutcome(
            status=TaskStatus.INVALID,
            failure=FailureCategory.INVALID_TASK_DEFINITION,
            failure_reason="the test input names no state",
        )
    rules: dict[str, tuple[str, Any]] = {}
    evidence: list[str] = []
    for key, value in test_state.items():
        observed: list[Any] = []
        ok = True
        for state, _action, nxt in pairs:
            if key not in state or key not in nxt:
                ok = False
                break
            observed.append((state[key], nxt[key]))
        if not ok or not observed:
            return SolveOutcome(
                status=TaskStatus.UNSUPPORTED,
                failure=FailureCategory.NO_CANDIDATE_RULE_FOUND,
                failure_reason=f"field {key!r} is not present in every example",
            )
        learned = _learn_field(value, observed)
        if learned is None:
            return SolveOutcome(
                status=TaskStatus.AMBIGUOUS,
                failure=FailureCategory.MULTIPLE_PLAUSIBLE_RULES,
                failure_reason=(
                    f"the examples disagree about what happens to {key!r}, so its next "
                    "value cannot be inferred"
                ),
                evidence=(f"field {key!r} observations: {observed[:6]}",),
            )
        rules[key] = learned
        evidence.append(f"{key}: {learned[0]}")
    predicted = dict(test_state)
    for key, (kind, delta) in rules.items():
        predicted[key] = _apply_field(kind, predicted[key], delta, test_action)
    return SolveOutcome(
        status=TaskStatus.SOLVED,
        predicted=predicted,
        candidate="state_transition(" + ", ".join(sorted(rules)) + ")",
        source=RuleSource.INFERRED,
        candidates_examined=len(pairs) * max(1, len(test_state)),
        candidates_fitting=1,
        evidence=tuple(evidence[:8]),
    )


def _learn_field(value: Any, observed: Sequence[tuple[Any, Any]]) -> tuple[str, Any] | None:
    """The transition rule for one field, or ``None`` when the examples disagree."""
    if isinstance(value, bool):
        moves = dict(observed)
        if all(before == after for before, after in moves.items()):
            return ("hold", None)
        if all(before != after for before, after in moves.items()):
            return ("flip", None)
        return None
    if isinstance(value, int):
        deltas = {after - before for before, after in observed}
        if len(deltas) == 1:
            return ("increment", deltas.pop())
        return None
    if isinstance(value, str):
        table = dict(observed)
        if len(table) == len({before for before, _ in observed}):
            return ("map", tuple(sorted(table.items())))
        return None
    if value is None:
        return ("hold", None)
    return None


def _apply_field(kind: str, current: Any, delta: Any, action: Mapping[str, Any]) -> Any:
    """Apply one learned field rule. ``action`` is available for rules that read it."""
    del action  # no supported field rule reads the action yet; the seam is here
    if kind == "hold":
        return current
    if kind == "flip":
        return not bool(current)
    if kind == "increment":
        return int(current) + int(delta)
    if kind == "map":
        table = dict(delta)
        return table.get(current, current)
    raise ValueError(f"unhandled field rule {kind!r}")


# --------------------------------------------------------------------------- #
# Family 4: small planning
# --------------------------------------------------------------------------- #


def solve_planning(task: EvaluationTask, budget: Budget) -> SolveOutcome:
    """Find a bounded shortest path with breadth-first search.

    Success is not "the plan looks right": the evaluator re-simulates the plan
    on the grid and checks the goal is reached, so a plan that walks through a
    wall is a failure however plausible it reads.
    """
    budget.check()
    inputs = task.test_input
    start = _cell(inputs.get("start"))
    goal = _cell(inputs.get("goal"))
    walls = set(filter(None, (_cell(item) for item in inputs.get("walls") or ())))
    limits = inputs.get("limits") or {}
    max_steps = int(dict(limits).get("max_steps") or 40)
    width = int(inputs.get("width") or 0)
    height = int(inputs.get("height") or 0)
    if start is None or goal is None or width <= 0 or height <= 0:
        return SolveOutcome(
            status=TaskStatus.INVALID,
            failure=FailureCategory.INVALID_TASK_DEFINITION,
            failure_reason="a planning task needs a start, a goal and a bounded grid",
        )
    path, visited, closed = _breadth_first(start, goal, walls, width, height, max_steps)
    if not path:
        if closed:
            # The search space CLOSED without reaching the goal: that is a proof
            # of unreachability, which is an ANSWER (and a correct one for a task
            # whose goal is genuinely walled off), not a failure to answer.
            return SolveOutcome(
                status=TaskStatus.SOLVED,
                predicted={"path": None, "steps": None},
                candidate="breadth_first_search:unreachable",
                source=RuleSource.DETERMINISTIC,
                candidates_examined=visited,
                candidates_fitting=1,
                evidence=(f"the search space closed after {visited} states: no path exists",),
            )
        return SolveOutcome(
            status=TaskStatus.BUDGET_EXHAUSTED,
            failure=FailureCategory.RESOURCE_BUDGET_EXHAUSTED,
            failure_reason=(
                f"the search was cut off after {visited} states without proving whether a "
                f"path exists; no answer is reported rather than a guess"
            ),
            candidates_examined=visited,
        )
    return SolveOutcome(
        status=TaskStatus.SOLVED,
        predicted={"path": [[row, column] for row, column in path], "steps": len(path) - 1},
        candidate="breadth_first_search",
        source=RuleSource.DETERMINISTIC,
        candidates_examined=visited,
        candidates_fitting=1,
        evidence=(f"{visited} states expanded", f"path length {len(path) - 1}"),
    )


def _breadth_first(
    start: tuple[int, int],
    goal: tuple[int, int],
    walls: set[tuple[int, int]],
    width: int,
    height: int,
    max_steps: int,
) -> tuple[tuple[tuple[int, int], ...], int, bool]:
    """A bounded BFS. Returns ``(path, expanded, closed)``.

    ``closed`` is what makes "no path exists" an ANSWER rather than a failure:
    ``True`` means the search space was exhausted (so unreachability is proved),
    ``False`` with no path means the exploration cap stopped it, and the caller
    reports an exhausted budget instead of pretending to know.
    """
    from collections import deque

    queue: deque[tuple[tuple[int, int], ...]] = deque([(start,)])
    seen: set[tuple[int, int]] = {start}
    expanded = 0
    while queue:
        path = queue.popleft()
        expanded += 1
        if expanded > MAX_PLANNING_STATES:
            return (), expanded, False
        head = path[-1]
        if head == goal:
            return path, expanded, True
        if len(path) - 1 >= max_steps:
            continue
        for delta_row, delta_column in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            candidate = (head[0] + delta_row, head[1] + delta_column)
            if not (0 <= candidate[0] < height and 0 <= candidate[1] < width):
                continue
            if candidate in walls or candidate in seen:
                continue
            seen.add(candidate)
            queue.append((*path, candidate))
    return (), expanded, True


def _cell(value: Any) -> tuple[int, int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return (int(value[0]), int(value[1]))
        except (TypeError, ValueError):
            return None
    return None


# --------------------------------------------------------------------------- #
# Family 5: tool selection
# --------------------------------------------------------------------------- #


def solve_tool_selection(task: EvaluationTask, budget: Budget) -> SolveOutcome:
    """Choose a tool by example-weighted token overlap.

    This is a real, explainable classifier: each example contributes its tokens
    as evidence for the tool it was paired with, and the test request is scored
    against that evidence. It is deliberately small, and the report states
    exactly that — it generalizes to new phrasings that share vocabulary and
    fails on phrasings that do not, which is a limit, not a success.
    """
    budget.check()
    request = str(task.test_input.get("request") or "").strip()
    tools = task.test_input.get("tools") or ()
    known = {str(item.get("id")) for item in tools if isinstance(item, Mapping) and item.get("id")}
    if not request:
        return SolveOutcome(
            status=TaskStatus.INVALID,
            failure=FailureCategory.INVALID_TASK_DEFINITION,
            failure_reason="the selection task states no request",
        )
    if not known:
        return SolveOutcome(
            status=TaskStatus.INVALID,
            failure=FailureCategory.INVALID_TASK_DEFINITION,
            failure_reason="the selection task lists no candidate tools",
        )
    weights: dict[str, Counter[str]] = {}
    labeled = 0
    for example in task.examples:
        if example.expected is None:
            continue
        tool = str(example.expected)
        text = str(example.inputs.get("request") or "")
        if not text:
            continue
        labeled += 1
        weights.setdefault(tool, Counter()).update(_tokens(text))
    if not labeled:
        return SolveOutcome(
            status=TaskStatus.UNSUPPORTED,
            failure=FailureCategory.NO_CANDIDATE_RULE_FOUND,
            failure_reason="no example paired a request with a tool, so nothing was learned",
        )
    query = Counter(_tokens(request))
    scores = {
        tool: sum(count * weights.get(tool, Counter())[token] for token, count in query.items())
        for tool in sorted(known)
    }
    best = max(scores.values())
    winners = sorted(tool for tool, score in scores.items() if score == best)
    if best <= 0:
        return SolveOutcome(
            status=TaskStatus.UNSUPPORTED,
            failure=FailureCategory.NO_CANDIDATE_RULE_FOUND,
            failure_reason=(
                "the request shares no vocabulary with any example, so no tool was "
                "chosen rather than guessing one"
            ),
            candidates_examined=len(known),
        )
    if len(winners) > 1:
        return SolveOutcome(
            status=TaskStatus.AMBIGUOUS,
            failure=FailureCategory.MULTIPLE_PLAUSIBLE_RULES,
            failure_reason=f"{len(winners)} tools score identically ({best}): {winners}",
            candidates_examined=len(known),
            rule_ambiguous=True,
        )
    return SolveOutcome(
        status=TaskStatus.SOLVED,
        predicted=winners[0],
        candidate=f"token_overlap({winners[0]})",
        source=RuleSource.INFERRED,
        candidates_examined=len(known),
        candidates_fitting=1,
        evidence=(f"score {best} from {labeled} labeled examples",),
    )


def _tokens(text: str) -> list[str]:
    cleaned = "".join(character if character.isalnum() else " " for character in text.lower())
    return [token for token in cleaned.split() if len(token) > 2]


# --------------------------------------------------------------------------- #
# Family 6: multi-step reasoning
# --------------------------------------------------------------------------- #


def solve_reasoning(task: EvaluationTask, budget: Budget) -> SolveOutcome:
    """Infer an arithmetic/sequence rule from the examples and apply it once.

    Two shapes are supported and named: a constant STEP (``4, 7, 10 → 13``) and
    a constant RATIO. Anything else is UNSUPPORTED — the solver says it does not
    know rather than producing a number that looks like an answer.
    """
    budget.check()
    sequences: list[tuple[list[int], int]] = []
    for example in task.examples:
        if example.expected is None:
            continue
        series = example.inputs.get("sequence")
        if not isinstance(series, (list, tuple)) or len(series) < 2:
            continue
        try:
            values = [int(item) for item in series]
        except (TypeError, ValueError):
            continue
        try:
            answer = int(example.expected)
        except (TypeError, ValueError):
            continue
        sequences.append((values, answer))
    series = task.test_input.get("sequence")
    if not isinstance(series, (list, tuple)) or len(series) < 2:
        return SolveOutcome(
            status=TaskStatus.INVALID,
            failure=FailureCategory.INVALID_TASK_DEFINITION,
            failure_reason="the reasoning task supplies no sequence of at least two terms",
        )
    try:
        test_values = [int(item) for item in series]
    except (TypeError, ValueError) as exc:
        return SolveOutcome(
            status=TaskStatus.INVALID,
            failure=FailureCategory.INPUT_PARSING_FAILURE,
            failure_reason=f"the sequence is not a list of integers: {exc}",
        )
    if not sequences:
        return SolveOutcome(
            status=TaskStatus.UNSUPPORTED,
            failure=FailureCategory.NO_CANDIDATE_RULE_FOUND,
            failure_reason="the task gave no worked example of the sequence rule",
        )
    # A worked example states the NEXT term after the sequence it shows, so the
    # step it implies is (given answer − last shown term) — and the rule is only
    # believed when EVERY example implies the same step. The cross-check below
    # re-derives that from the examples rather than from the test prediction,
    # because checking a rule against the answer it produced is not a check.
    steps = {answer - terms[-1] for terms, answer in sequences}
    ratios: set[int] = set()
    ratio_case = True
    for terms, answer in sequences:
        if not terms or terms[-1] == 0 or answer % terms[-1]:
            ratio_case = False
            break
        ratios.add(answer // terms[-1])
    candidates: list[tuple[str, int]] = []
    if len(steps) == 1:
        step = steps.pop()
        if all(terms[-1] + step == answer for terms, answer in sequences):
            candidates.append(("arithmetic", test_values[-1] + step))
    if ratio_case and len(ratios) == 1:
        ratio = ratios.pop()
        if all(terms[-1] * ratio == answer for terms, answer in sequences):
            candidates.append(("geometric", test_values[-1] * ratio))
    if not candidates:
        return SolveOutcome(
            status=TaskStatus.UNSUPPORTED,
            failure=FailureCategory.NO_CANDIDATE_RULE_FOUND,
            failure_reason=(
                "the worked examples follow neither a constant step nor a constant ratio, "
                "so this build does not know the rule"
            ),
            candidates_examined=len(sequences),
        )
    if len({value for _kind, value in candidates}) > 1:
        return SolveOutcome(
            status=TaskStatus.AMBIGUOUS,
            failure=FailureCategory.MULTIPLE_PLAUSIBLE_RULES,
            failure_reason=(
                "both a constant step and a constant ratio explain the examples and they "
                f"disagree: {candidates}"
            ),
            candidates_examined=len(sequences),
            rule_ambiguous=True,
        )
    kind, answer = candidates[0]
    return SolveOutcome(
        status=TaskStatus.SOLVED,
        predicted=answer,
        candidate=f"sequence:{kind}",
        source=RuleSource.INFERRED,
        candidates_examined=len(sequences),
        candidates_fitting=1,
        evidence=(f"{len(sequences)} worked examples", f"rule: {kind}"),
    )


# --------------------------------------------------------------------------- #
# Baselines
# --------------------------------------------------------------------------- #


def _baseline_identity(task: EvaluationTask, budget: Budget) -> SolveOutcome:
    """Answer with the input unchanged — the deterministic do-nothing baseline."""
    budget.check()
    if task.kind is TaskKind.GRID_TRANSFORM:
        try:
            grid = parse_grid(task.test_input.get("grid"))
        except InvalidGrid as exc:
            return SolveOutcome(
                status=TaskStatus.INVALID,
                failure=FailureCategory.INPUT_PARSING_FAILURE,
                failure_reason=str(exc),
            )
        return SolveOutcome(
            status=TaskStatus.SOLVED,
            predicted=grid.rows(),
            candidate="identity",
            source=RuleSource.DETERMINISTIC,
            candidates_examined=1,
            candidates_fitting=1,
        )
    return SolveOutcome(
        status=TaskStatus.UNSUPPORTED,
        failure=FailureCategory.NO_CANDIDATE_RULE_FOUND,
        failure_reason="the identity baseline only answers grid tasks",
    )


def _baseline_random(task: EvaluationTask, budget: Budget) -> SolveOutcome:
    """Apply a SEEDED random operator — the "is inference better than chance" bound."""
    budget.check()
    if task.kind is not TaskKind.GRID_TRANSFORM:
        return SolveOutcome(
            status=TaskStatus.UNSUPPORTED,
            failure=FailureCategory.NO_CANDIDATE_RULE_FOUND,
            failure_reason="the random baseline only answers grid tasks",
        )
    try:
        grid = parse_grid(task.test_input.get("grid"))
    except InvalidGrid as exc:
        return SolveOutcome(
            status=TaskStatus.INVALID,
            failure=FailureCategory.INPUT_PARSING_FAILURE,
            failure_reason=str(exc),
        )
    rng = random.Random(f"{task.task_id}:{task.seed}")
    candidates: list[tuple[str, tuple[Any, ...]]] = []
    for operator_ in OPERATORS:
        try:
            proposals = operator_.propose(((grid, grid),))
        except Exception:  # noqa: BLE001 - a proposal that raises proposes nothing
            continue
        for params in proposals:
            candidates.append((operator_.name, tuple(params)))
    if not candidates:
        return SolveOutcome(
            status=TaskStatus.UNSUPPORTED,
            failure=FailureCategory.NO_CANDIDATE_RULE_FOUND,
            failure_reason="the library proposed nothing for this input",
        )
    name, params = rng.choice(candidates)
    rule = CandidateRule(steps=(RuleStep(operator=name, params=tuple(params)),))
    total, why = rule_is_total(rule, grid)
    if not total:
        return SolveOutcome(
            status=TaskStatus.ERROR,
            candidate=rule.rule_id,
            failure=FailureCategory.TRANSFORMATION_DEFECT,
            failure_reason=why,
        )
    return SolveOutcome(
        status=TaskStatus.SOLVED,
        predicted=rule.apply(grid).rows(),
        candidate=f"random:{rule.rule_id}",
        source=RuleSource.DETERMINISTIC,
        candidates_examined=len(candidates),
        candidates_fitting=1,
    )


def _baseline_no_answer(task: EvaluationTask, budget: Budget) -> SolveOutcome:
    """Decline to answer — the floor a real solver must beat by construction."""
    budget.check()
    return SolveOutcome(
        status=TaskStatus.UNSUPPORTED,
        failure=FailureCategory.NO_CANDIDATE_RULE_FOUND,
        failure_reason="this baseline never answers",
    )


_BASELINE_SOLVERS = {
    "identity": _baseline_identity,
    "random_rule": _baseline_random,
    "no_answer": _baseline_no_answer,
}


def solve(
    task: EvaluationTask, *, solver: str = "infer", budget: Budget | None = None
) -> SolveOutcome:
    """Solve one task with a named solver, inside its budget.

    The budget is enforced here, at the seams between bounded steps, and a spent
    budget ends the solve as BUDGET_EXHAUSTED — never as a wrong answer and
    never as a success.
    """
    limits = budget or Budget()
    try:
        limits.check()
        if solver == "infer":
            outcome = _SOLVE_BY_KIND[task.kind](task, limits)
        elif solver in _BASELINE_SOLVERS:
            outcome = _BASELINE_SOLVERS[solver](task, limits)
        else:
            return SolveOutcome(
                status=TaskStatus.INVALID,
                failure=FailureCategory.INVALID_TASK_DEFINITION,
                failure_reason=(
                    f"solver {solver!r} is not one this build has; it knows "
                    f"'infer' and {', '.join(BASELINES)}"
                ),
            )
        limits.check()
        return outcome
    except BudgetExceeded as exc:
        return SolveOutcome(
            status=TaskStatus.BUDGET_EXHAUSTED,
            failure=FailureCategory.RESOURCE_BUDGET_EXHAUSTED,
            failure_reason=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - an infrastructure failure is reported, not raised
        return SolveOutcome(
            status=TaskStatus.ERROR,
            failure=FailureCategory.EVALUATION_INFRASTRUCTURE,
            failure_reason=f"the {solver} solver raised {type(exc).__name__}: {exc}",
        )


_SOLVE_BY_KIND = {
    TaskKind.GRID_TRANSFORM: solve_grid,
    TaskKind.SPATIAL_RELATION: solve_spatial,
    TaskKind.STATE_TRANSITION: solve_state_transition,
    TaskKind.SMALL_PLANNING: solve_planning,
    TaskKind.TOOL_SELECTION: solve_tool_selection,
    TaskKind.MULTI_STEP_REASONING: solve_reasoning,
}


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}
