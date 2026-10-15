"""The built-in task corpora: what this build's benchmark actually asks.

Three properties decide whether this file is honest, and each is checkable by a
test rather than by reading:

1. **The held-out answers are HAND-WRITTEN.** No expected output below is
   produced by calling the operator the task is testing. If a literal and its
   operator disagree, the suite fails — which is the only way a mis-typed answer
   key (or a wrong operator) becomes visible instead of being absorbed by a
   self-fulfilling corpus.
2. **The split is declared per task, not inferred.** ``TRAIN``/``DEV`` tasks are
   the ones the operator library was developed against; ``HELD_OUT`` tasks are
   colour, dimension and arrangement variants of the same rule families, plus
   the ambiguous and unsupported probes, and the manager's leakage check refuses
   to call a run clean when a held-out task shares its answer with a fitting one.
3. **Every task states its own success condition and verifier.** A grid task is
   solved on an EXACT match; a planning task is solved on the OUTCOME (the plan
   is re-simulated and the goal has to be reached), never on a string equality
   with one particular path.

The corpora are deliberately small and named. A benchmark whose tasks nobody can
enumerate is one whose failures nobody can explain.
"""

from __future__ import annotations

from typing import Any

from novacontrol.generalization.models import (
    Difficulty,
    EvaluationTask,
    Split,
    TaskExample,
    TaskKind,
    validate_task,
)

__all__ = [
    "BUILTIN_TASKS",
    "CORPUS_VERSION",
    "GRID_RULES_SUPPORTED",
    "task_by_id",
    "tasks_for",
    "tasks_payload",
]

#: Bump when a task's content or expectation changes.
CORPUS_VERSION = "phase26.corpus.1"

#: The transformation families the library covers, as the report states them.
GRID_RULES_SUPPORTED: tuple[str, ...] = (
    "identity",
    "rotate (90/180/270)",
    "reflection (horizontal, vertical, transposed, anti-transposed)",
    "translation",
    "recolour (per-cell map, single-colour repaint)",
    "selection (largest/smallest object, colour extraction and cropping)",
    "extraction (crop to content, crop to one colour's box)",
    "repetition (tiling, mirror-quadrant assembly)",
    "gravity (down/up/left/right)",
    "movement (move the single object by a fixed offset)",
    "counting (objects, non-background cells, cells per colour)",
    "completion (fill enclosed holes, outline objects)",
    "composition of any two of the above",
)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _example(grid: Any, expected: Any, *, label: str = "") -> TaskExample:
    return TaskExample(inputs={"grid": grid}, expected=expected, label=label)


def _grid_task(
    task_id: str,
    *,
    split: Split,
    difficulty: Difficulty,
    request: str,
    examples: tuple[tuple[Any, Any], ...],
    test_input: Any,
    expected_output: Any,
    rule_family: str,
    seed: int = 0,
    tags: tuple[str, ...] = (),
) -> EvaluationTask:
    """One ARC-style task, with the answer key held by the evaluator."""
    return validate_task(
        {
            "task_id": task_id,
            "kind": TaskKind.GRID_TRANSFORM.value,
            "family_id": "grid_transform",
            "split": split.value,
            "difficulty": difficulty.value,
            "request": request,
            "success_condition": "the produced grid equals the expected grid exactly",
            "verifier": "exact_grid_match",
            "examples": [
                {"inputs": {"grid": source}, "expected": target, "label": f"{task_id}-{index}"}
                for index, (source, target) in enumerate(examples)
            ],
            "test_input": {"grid": test_input},
            "expected_output": expected_output,
            "rule_family": rule_family,
            "seed": seed,
            "tags": list(tags),
            "provenance": {
                "corpus": CORPUS_VERSION,
                "answers": "hand-written",
                "source": "built-in Phase 26 corpus",
            },
        }
    )


# --------------------------------------------------------------------------- #
# Grid tasks — the developed-on set (TRAIN / DEV)
# --------------------------------------------------------------------------- #

_TRAIN_GRID: tuple[EvaluationTask, ...] = (
    _grid_task(
        "gt_rotate90",
        split=Split.TRAIN,
        difficulty=Difficulty.EASY,
        request="Rotate each input a quarter turn clockwise.",
        examples=(
            (["120", "003"], ["01", "02", "30"]),
            (["1100", "0010"], ["01", "01", "10", "00"]),
        ),
        test_input=["230", "010"],
        expected_output=["02", "13", "00"],
        rule_family="rotation",
        tags=("rotation", "geometry"),
    ),
    _grid_task(
        "gt_flip_horizontal",
        split=Split.TRAIN,
        difficulty=Difficulty.EASY,
        request="Mirror each input left to right.",
        examples=(
            (["120", "003"], ["021", "300"]),
            (["1234", "0000"], ["4321", "0000"]),
        ),
        test_input=["1200", "0340"],
        expected_output=["0021", "0430"],
        rule_family="reflection",
        tags=("reflection", "geometry"),
    ),
    _grid_task(
        "gt_recolor",
        split=Split.TRAIN,
        difficulty=Difficulty.EASY,
        request="Recolour the shapes with the substitute colours shown.",
        examples=(
            (["110", "200"], ["330", "400"]),
            (["010", "020"], ["030", "040"]),
        ),
        test_input=["110", "020"],
        expected_output=["330", "040"],
        rule_family="recolour",
        tags=("recolour",),
    ),
    _grid_task(
        "gt_tile",
        split=Split.TRAIN,
        difficulty=Difficulty.EASY,
        request="Repeat each input to fill the larger grid.",
        examples=(
            (["12", "34"], ["1212", "3434", "1212", "3434"]),
            (["10", "01"], ["1010", "0101", "1010", "0101"]),
        ),
        test_input=["70", "07"],
        expected_output=["7070", "0707", "7070", "0707"],
        rule_family="repetition",
        tags=("repetition",),
    ),
    _grid_task(
        "gt_crop_content",
        split=Split.TRAIN,
        difficulty=Difficulty.EASY,
        request="Trim the empty border away from each input.",
        examples=(
            (["0000", "0120", "0300", "0000"], ["12", "30"]),
            (["000", "101", "000"], ["101"]),
        ),
        test_input=["00000", "00120", "00034", "00000"],
        expected_output=["120", "034"],
        rule_family="extraction",
        tags=("extraction",),
    ),
    _grid_task(
        "gt_count_objects",
        split=Split.TRAIN,
        difficulty=Difficulty.EASY,
        request="Answer with the number of separate objects.",
        examples=(
            (["110", "000", "100"], [[2]]),
            (["100", "000", "000"], [[1]]),
        ),
        test_input=["101", "000", "010"],
        expected_output=[[3]],
        rule_family="counting",
        tags=("counting",),
    ),
    _grid_task(
        "gt_gravity_down",
        split=Split.TRAIN,
        difficulty=Difficulty.MEDIUM,
        request="Let the objects fall until nothing can move further down.",
        examples=(
            (["100", "020", "000"], ["000", "000", "120"]),
            (["0100", "0000", "0020"], ["0000", "0000", "0120"]),
        ),
        test_input=["100", "000", "020"],
        expected_output=["000", "000", "120"],
        rule_family="movement",
        tags=("gravity", "movement"),
    ),
    _grid_task(
        "gt_mirror_tile",
        split=Split.TRAIN,
        difficulty=Difficulty.MEDIUM,
        request="Assemble each input with its mirrored copies.",
        examples=(
            (["12", "34"], ["1221", "3443", "3443", "1221"]),
            (["10", "01"], ["1001", "0110", "0110", "1001"]),
        ),
        test_input=["56", "70"],
        expected_output=["5665", "7007", "7007", "5665"],
        rule_family="repetition",
        tags=("mirror", "repetition"),
    ),
    _grid_task(
        "gt_keep_largest",
        split=Split.TRAIN,
        difficulty=Difficulty.MEDIUM,
        request="Keep only the biggest object.",
        examples=(
            (["110", "000", "001"], ["110", "000", "000"]),
            (["100", "001", "001"], ["000", "001", "001"]),
        ),
        test_input=["010", "000", "110"],
        expected_output=["000", "000", "110"],
        rule_family="selection",
        tags=("selection",),
    ),
    _grid_task(
        "gt_fill_holes",
        split=Split.TRAIN,
        difficulty=Difficulty.MEDIUM,
        request="Fill the enclosed gap with the surrounding colour.",
        # The outputs are deliberately NOT uniform: an output painted one
        # colour would also be explained by "repaint everything", and the corpus
        # would then be testing an ambiguity instead of the operator.
        examples=(
            (
                ["00000", "01110", "01010", "01110", "00000"],
                ["00000", "01110", "01110", "01110", "00000"],
            ),
            (
                ["00000", "00200", "02020", "00200", "00000"],
                ["00000", "00200", "02220", "00200", "00000"],
            ),
        ),
        test_input=["00000", "03330", "03030", "03330", "00000"],
        expected_output=["00000", "03330", "03330", "03330", "00000"],
        rule_family="completion",
        tags=("completion",),
    ),
    _grid_task(
        "gt_outline",
        split=Split.TRAIN,
        difficulty=Difficulty.MEDIUM,
        request="Draw a border around every shape in the new colour.",
        examples=(
            (["000", "010", "000"], ["333", "313", "333"]),
            (["0000", "0010", "0000"], ["0333", "0313", "0333"]),
        ),
        test_input=["000", "001", "000"],
        expected_output=["033", "031", "033"],
        rule_family="completion",
        tags=("completion", "outline"),
    ),
    _grid_task(
        "gt_select_colour",
        split=Split.TRAIN,
        difficulty=Difficulty.MEDIUM,
        request="Keep only the chosen shape and trim to it.",
        examples=(
            (["00000", "02200", "02200", "00033", "00003"], ["33", "03"]),
            (["2000", "0000", "0009"], ["9"]),
        ),
        test_input=["0900", "0990", "0000"],
        expected_output=["90", "99"],
        rule_family="extraction",
        tags=("extraction", "selection"),
    ),
)

# --------------------------------------------------------------------------- #
# Grid tasks — the held-out set
# --------------------------------------------------------------------------- #


_HELD_OUT_GRID: tuple[EvaluationTask, ...] = (
    _grid_task(
        "ho_rotate180_non_square",
        split=Split.HELD_OUT,
        difficulty=Difficulty.EASY,
        request="Turn each input half a turn (a two-row grid becomes two rows again).",
        examples=(
            (["120", "003"], ["300", "021"]),
            (["567", "890"], ["098", "765"]),
        ),
        test_input=["341", "002"],
        expected_output=["200", "143"],
        rule_family="rotation",
        seed=101,
        tags=("rotation", "held-out"),
    ),
    _grid_task(
        "ho_flip_vertical_colours",
        split=Split.HELD_OUT,
        difficulty=Difficulty.EASY,
        request="Mirror each input top to bottom, with colours never seen in training.",
        examples=(
            (["110", "000", "001"], ["001", "000", "110"]),
            (["500", "000", "066"], ["066", "000", "500"]),
        ),
        test_input=["110", "000", "001", "000", "101"],
        expected_output=["101", "000", "001", "000", "110"],
        rule_family="reflection",
        seed=102,
        tags=("reflection", "held-out"),
    ),
    _grid_task(
        "ho_recolour_bigger_grid",
        split=Split.HELD_OUT,
        difficulty=Difficulty.EASY,
        request="Apply the same colour substitution to a larger grid.",
        examples=(
            (["110", "200"], ["880", "900"]),
            (["010", "020"], ["080", "090"]),
        ),
        test_input=["1000", "0220", "0220", "0001"],
        expected_output=["8000", "0990", "0990", "0008"],
        rule_family="recolour",
        seed=103,
        tags=("recolour", "held-out"),
    ),
    _grid_task(
        "ho_tile_one_by_three",
        split=Split.HELD_OUT,
        difficulty=Difficulty.MEDIUM,
        request="Repeat each input three times across, never down.",
        examples=(
            (["12", "34"], ["121212", "343434"]),
            (["10", "01"], ["101010", "010101"]),
        ),
        test_input=["56", "70", "89"],
        expected_output=["565656", "707070", "898989"],
        rule_family="repetition",
        seed=104,
        tags=("repetition", "held-out"),
    ),
    _grid_task(
        "ho_crop_sparse",
        split=Split.HELD_OUT,
        difficulty=Difficulty.MEDIUM,
        request="Trim a sparse grid to what is actually drawn.",
        examples=(
            (["00000", "00120", "00030", "00000"], ["12", "03"]),
            (["00000", "00012", "00000", "00300", "00000"], ["012", "000", "300"]),
        ),
        test_input=["00000", "00100", "01210", "00100", "00000"],
        expected_output=["010", "121", "010"],
        rule_family="extraction",
        seed=105,
        tags=("extraction", "held-out"),
    ),
    _grid_task(
        "ho_count_non_background",
        split=Split.HELD_OUT,
        difficulty=Difficulty.MEDIUM,
        request="Answer with how many cells are not background.",
        examples=(
            (["00000", "00110", "00100", "00000", "00000"], [[3]]),
            (["00000", "00000", "00110", "00000", "00000"], [[2]]),
        ),
        test_input=["00000", "01100", "01100", "00000", "00000"],
        expected_output=[[4]],
        rule_family="counting",
        seed=106,
        tags=("counting", "held-out"),
    ),
    _grid_task(
        "ho_move_object",
        split=Split.HELD_OUT,
        difficulty=Difficulty.MEDIUM,
        request="Move the shape one step down, leaving no trail.",
        # Four rows on purpose: in a THREE-row grid "move down one" and "flip
        # top to bottom" happen to produce the same picture, so the examples
        # would not decide which rule is meant.
        examples=(
            (["010", "010", "000", "000"], ["000", "010", "010", "000"]),
            (["100", "100", "000", "000"], ["000", "100", "100", "000"]),
        ),
        test_input=["001", "001", "000", "000"],
        expected_output=["000", "001", "001", "000"],
        rule_family="movement",
        seed=107,
        tags=("movement", "held-out"),
    ),
    _grid_task(
        "ho_rotate_then_recolour",
        split=Split.HELD_OUT,
        difficulty=Difficulty.HARD,
        request="Turn each input a quarter turn clockwise, then apply the colour change.",
        examples=(
            (["12", "03"], ["07", "98"]),
            (["13", "20"], ["87", "09"]),
        ),
        test_input=["12", "30"],
        expected_output=["97", "08"],
        rule_family="composition",
        seed=108,
        tags=("composition", "held-out"),
    ),
    _grid_task(
        "ho_ambiguous_double_reading",
        split=Split.HELD_OUT,
        difficulty=Difficulty.HARD,
        request="One example, which more than one transformation explains.",
        examples=((["11", "11"], ["11", "11"]),),
        test_input=["12", "34"],
        expected_output=["12", "34"],
        rule_family="ambiguity-probe",
        seed=109,
        tags=("ambiguity", "held-out"),
    ),
    _grid_task(
        "ho_unsupported_row_order",
        split=Split.HELD_OUT,
        difficulty=Difficulty.HARD,
        request="Slide every row one cell right and wrap the last cell to the front.",
        examples=(
            (["120", "003"], ["012", "300"]),
            (["567", "890"], ["756", "089"]),
            (["100", "000", "010"], ["010", "000", "001"]),
        ),
        test_input=["123", "456"],
        expected_output=["312", "645"],
        rule_family="unsupported-probe",
        seed=110,
        tags=("unsupported", "held-out"),
    ),
)

# --------------------------------------------------------------------------- #
# The other five families — same contracts, different verifiers
# --------------------------------------------------------------------------- #


def _family_task(
    task_id: str,
    *,
    kind: TaskKind,
    family_id: str,
    split: Split,
    difficulty: Difficulty,
    request: str,
    success_condition: str,
    verifier: str,
    examples: tuple[tuple[Any, Any], ...],
    test_input: dict[str, Any],
    expected_output: Any,
    seed: int = 0,
    tags: tuple[str, ...] = (),
    rule_family: str = "",
) -> EvaluationTask:
    """One task in a non-grid family, with its own verifier and success condition."""
    return validate_task(
        {
            "task_id": task_id,
            "kind": kind.value,
            "family_id": family_id,
            "split": split.value,
            "difficulty": difficulty.value,
            "request": request,
            "success_condition": success_condition,
            "verifier": verifier,
            "examples": [
                {"inputs": inputs, "expected": answer, "label": f"{task_id}-{index}"}
                for index, (inputs, answer) in enumerate(examples)
            ],
            "test_input": test_input,
            "expected_output": expected_output,
            "rule_family": rule_family,
            "seed": seed,
            "tags": list(tags),
            "provenance": {
                "corpus": CORPUS_VERSION,
                "answers": "hand-written",
                "source": "built-in Phase 26 corpus",
            },
        }
    )


_SPATIAL: tuple[EvaluationTask, ...] = (
    _family_task(
        "sp_left_of",
        kind=TaskKind.SPATIAL_RELATION,
        family_id="spatial_relation",
        split=Split.TRAIN,
        difficulty=Difficulty.EASY,
        request="Is the first shape to the left of the second?",
        success_condition="the measured relation between the two shapes is the expected one",
        verifier="relation_holds",
        examples=(
            ({"grid": ["120", "300"], "a": 1, "b": 2, "relation": "left_of"}, True),
            ({"grid": ["021", "300"], "a": 1, "b": 2, "relation": "left_of"}, False),
        ),
        test_input={"grid": ["100", "020"], "a": 1, "b": 2, "relation": "left_of"},
        expected_output=True,
        tags=("spatial", "geometry"),
    ),
    _family_task(
        "sp_above",
        kind=TaskKind.SPATIAL_RELATION,
        family_id="spatial_relation",
        split=Split.TRAIN,
        difficulty=Difficulty.EASY,
        request="Is the first shape above the second?",
        success_condition="the measured relation between the two shapes is the expected one",
        verifier="relation_holds",
        examples=(
            ({"grid": ["100", "020"], "a": 1, "b": 2, "relation": "above"}, True),
            ({"grid": ["020", "100"], "a": 1, "b": 2, "relation": "above"}, False),
        ),
        test_input={"grid": ["100", "000", "020"], "a": 1, "b": 2, "relation": "above"},
        expected_output=True,
        tags=("spatial", "geometry"),
    ),
    _family_task(
        "ho_sp_touching",
        kind=TaskKind.SPATIAL_RELATION,
        family_id="spatial_relation",
        split=Split.HELD_OUT,
        difficulty=Difficulty.MEDIUM,
        request="Do the two shapes share an edge?",
        success_condition="the measured relation between the two shapes is the expected one",
        verifier="relation_holds",
        examples=(
            ({"grid": ["110", "200"], "a": 1, "b": 2, "relation": "touching"}, True),
            ({"grid": ["100", "020"], "a": 1, "b": 2, "relation": "touching"}, False),
        ),
        test_input={"grid": ["110", "020"], "a": 1, "b": 2, "relation": "touching"},
        expected_output=True,
        seed=201,
        tags=("spatial", "held-out"),
    ),
    _family_task(
        "ho_sp_inside",
        kind=TaskKind.SPATIAL_RELATION,
        family_id="spatial_relation",
        split=Split.HELD_OUT,
        difficulty=Difficulty.MEDIUM,
        request="Is the first shape completely inside the second?",
        success_condition="the measured relation between the two shapes is the expected one",
        verifier="relation_holds",
        examples=(
            ({"grid": ["333", "323", "333"], "a": 2, "b": 3, "relation": "inside"}, True),
            ({"grid": ["320", "330", "333"], "a": 2, "b": 3, "relation": "inside"}, False),
        ),
        test_input={
            "grid": ["44444", "42224", "44444"],
            "a": 2,
            "b": 4,
            "relation": "inside",
        },
        expected_output=True,
        seed=202,
        tags=("spatial", "held-out"),
    ),
    _family_task(
        "ho_sp_larger_than",
        kind=TaskKind.SPATIAL_RELATION,
        family_id="spatial_relation",
        split=Split.HELD_OUT,
        difficulty=Difficulty.EASY,
        request="Does the first shape cover more cells than the second?",
        success_condition="the measured relation between the two shapes is the expected one",
        verifier="relation_holds",
        examples=(
            ({"grid": ["111", "020"], "a": 1, "b": 2, "relation": "larger_than"}, True),
            ({"grid": ["110", "022"], "a": 1, "b": 2, "relation": "larger_than"}, False),
        ),
        test_input={"grid": ["1111", "0200"], "a": 1, "b": 2, "relation": "larger_than"},
        expected_output=True,
        seed=203,
        tags=("spatial", "held-out"),
    ),
    _family_task(
        "ho_sp_unsupported_relation",
        kind=TaskKind.SPATIAL_RELATION,
        family_id="spatial_relation",
        split=Split.HELD_OUT,
        difficulty=Difficulty.HARD,
        request="Do the two shapes overlap? (a relation this build does not measure)",
        success_condition="the measured relation between the two shapes is the expected one",
        verifier="relation_holds",
        examples=(({"grid": ["110", "020"], "a": 1, "b": 2, "relation": "overlaps"}, False),),
        test_input={"grid": ["110", "020"], "a": 1, "b": 2, "relation": "overlaps"},
        expected_output=False,
        seed=204,
        rule_family="unsupported-probe",
        tags=("spatial", "unsupported", "held-out"),
    ),
)


_STATE_TRANSITION: tuple[EvaluationTask, ...] = (
    _family_task(
        "st_counter",
        kind=TaskKind.STATE_TRANSITION,
        family_id="state_transition",
        split=Split.TRAIN,
        difficulty=Difficulty.EASY,
        request="What state does this action produce next?",
        success_condition="the predicted state equals the expected state exactly",
        verifier="state_equals",
        examples=(
            (
                {
                    "state": {"x": 1, "goal": 5, "mode": "idle"},
                    "action": {"kind": "step"},
                },
                {"x": 2, "goal": 5, "mode": "running"},
            ),
            (
                {
                    "state": {"x": 2, "goal": 5, "mode": "running"},
                    "action": {"kind": "step"},
                },
                {"x": 3, "goal": 5, "mode": "running"},
            ),
        ),
        test_input={
            "state": {"x": 4, "goal": 5, "mode": "running"},
            "action": {"kind": "step"},
        },
        expected_output={"x": 5, "goal": 5, "mode": "running"},
        tags=("state", "transition"),
    ),
    _family_task(
        "ho_st_unseen_values",
        kind=TaskKind.STATE_TRANSITION,
        family_id="state_transition",
        split=Split.HELD_OUT,
        difficulty=Difficulty.MEDIUM,
        request="Predict the next state on values never shown in the examples.",
        success_condition="the predicted state equals the expected state exactly",
        verifier="state_equals",
        examples=(
            (
                {
                    "state": {"level": 1, "hp": 10, "boss": False, "phase": "calm"},
                    "action": {"kind": "advance"},
                },
                {"level": 2, "hp": 8, "boss": False, "phase": "fight"},
            ),
            (
                {
                    "state": {"level": 2, "hp": 8, "boss": False, "phase": "fight"},
                    "action": {"kind": "advance"},
                },
                {"level": 3, "hp": 6, "boss": False, "phase": "fight"},
            ),
        ),
        test_input={
            "state": {"level": 5, "hp": 4, "boss": False, "phase": "fight"},
            "action": {"kind": "advance"},
        },
        expected_output={"level": 6, "hp": 2, "boss": False, "phase": "fight"},
        seed=301,
        tags=("state", "held-out"),
    ),
)

#: One example instance per planning task documents the action semantics; the
#: success condition is the OUTCOME the plan produces, never one particular path.
_PLANNING: tuple[EvaluationTask, ...] = (
    _family_task(
        "pl_corridor",
        kind=TaskKind.SMALL_PLANNING,
        family_id="small_planning",
        split=Split.TRAIN,
        difficulty=Difficulty.EASY,
        request="Find a route to the goal without crossing a wall.",
        success_condition="the proposed path reaches the goal, stays in bounds and avoids walls",
        verifier="plan_reaches_goal",
        examples=(
            (
                {
                    "width": 3,
                    "height": 1,
                    "start": [0, 0],
                    "goal": [0, 2],
                    "walls": [],
                    "limits": {"max_steps": 10},
                },
                {"reaches_goal": True},
            ),
        ),
        test_input={
            "width": 4,
            "height": 4,
            "start": [0, 0],
            "goal": [3, 3],
            "walls": [[1, 1], [1, 2]],
            "limits": {"max_steps": 20},
        },
        expected_output={"reaches_goal": True},
        tags=("planning",),
    ),
    _family_task(
        "pl_walled_off",
        kind=TaskKind.SMALL_PLANNING,
        family_id="small_planning",
        split=Split.TRAIN,
        difficulty=Difficulty.MEDIUM,
        request="Is the goal reachable at all?",
        success_condition="the proposed path reaches the goal, or the answer says no path exists",
        verifier="plan_reaches_goal",
        examples=(
            (
                {
                    "width": 3,
                    "height": 1,
                    "start": [0, 0],
                    "goal": [0, 2],
                    "walls": [[0, 1]],
                    "limits": {"max_steps": 10},
                },
                {"reaches_goal": False},
            ),
        ),
        test_input={
            "width": 3,
            "height": 3,
            "start": [0, 0],
            "goal": [2, 2],
            "walls": [[1, 1], [1, 2], [2, 1]],
            "limits": {"max_steps": 20},
        },
        expected_output={"reaches_goal": False},
        tags=("planning", "unreachable"),
    ),
    _family_task(
        "ho_pl_maze",
        kind=TaskKind.SMALL_PLANNING,
        family_id="small_planning",
        split=Split.HELD_OUT,
        difficulty=Difficulty.HARD,
        request="Route around a wall through its only gap.",
        success_condition="the proposed path reaches the goal, stays in bounds and avoids walls",
        verifier="plan_reaches_goal",
        examples=(
            (
                {
                    "width": 3,
                    "height": 3,
                    "start": [0, 0],
                    "goal": [2, 0],
                    "walls": [[0, 1], [1, 1]],
                    "limits": {"max_steps": 12},
                },
                {"reaches_goal": True},
            ),
        ),
        test_input={
            "width": 7,
            "height": 7,
            "start": [0, 0],
            "goal": [6, 6],
            "walls": [[0, 3], [1, 3], [2, 3], [3, 3], [4, 3], [5, 3]],
            "limits": {"max_steps": 40},
        },
        expected_output={"reaches_goal": True},
        seed=401,
        tags=("planning", "held-out"),
    ),
    _family_task(
        "ho_pl_walled_off",
        kind=TaskKind.SMALL_PLANNING,
        family_id="small_planning",
        split=Split.HELD_OUT,
        difficulty=Difficulty.MEDIUM,
        request="Is a goal that is ringed by walls reachable?",
        success_condition="the proposed path reaches the goal, or the answer says no path exists",
        verifier="plan_reaches_goal",
        examples=(
            (
                {
                    "width": 3,
                    "height": 1,
                    "start": [0, 0],
                    "goal": [0, 2],
                    "walls": [[0, 1]],
                    "limits": {"max_steps": 10},
                },
                {"reaches_goal": False},
            ),
        ),
        test_input={
            "width": 5,
            "height": 5,
            "start": [0, 0],
            "goal": [2, 2],
            "walls": [
                [1, 1],
                [1, 2],
                [1, 3],
                [2, 1],
                [2, 3],
                [3, 1],
                [3, 2],
                [3, 3],
            ],
            "limits": {"max_steps": 20},
        },
        expected_output={"reaches_goal": False},
        seed=402,
        tags=("planning", "unreachable", "held-out"),
    ),
)


_TOOL_SELECTION: tuple[EvaluationTask, ...] = (
    _family_task(
        "ts_builtin",
        kind=TaskKind.TOOL_SELECTION,
        family_id="tool_selection",
        split=Split.TRAIN,
        difficulty=Difficulty.EASY,
        request="Which tool does this request need?",
        success_condition="the chosen tool is the expected one",
        verifier="tool_matches",
        examples=(
            ({"request": "open the browser and search the web"}, "browser"),
            ({"request": "read the file report.pdf and summarise it"}, "files"),
            ({"request": "send a message to the team"}, "chat"),
        ),
        test_input={
            "request": "search the web for the weather",
            "tools": [{"id": "browser"}, {"id": "files"}, {"id": "chat"}],
        },
        expected_output="browser",
        tags=("tools", "selection"),
    ),
    _family_task(
        "ho_ts_unseen_phrasing",
        kind=TaskKind.TOOL_SELECTION,
        family_id="tool_selection",
        split=Split.HELD_OUT,
        difficulty=Difficulty.MEDIUM,
        request="Choose the tool for a request phrased differently from the examples.",
        success_condition="the chosen tool is the expected one",
        verifier="tool_matches",
        examples=(
            ({"request": "open the door in the simulation"}, "sim"),
            ({"request": "compile the project sources"}, "build"),
            ({"request": "email the summary to the client"}, "email"),
        ),
        test_input={
            "request": "compile the sources and report errors",
            "tools": [{"id": "sim"}, {"id": "build"}, {"id": "email"}],
        },
        expected_output="build",
        seed=501,
        tags=("tools", "held-out"),
    ),
)


_REASONING: tuple[EvaluationTask, ...] = (
    _family_task(
        "mr_arithmetic",
        kind=TaskKind.MULTI_STEP_REASONING,
        family_id="multi_step_reasoning",
        split=Split.TRAIN,
        difficulty=Difficulty.EASY,
        request="Continue the sequence.",
        success_condition="the next term equals the expected term",
        verifier="chain_matches",
        examples=(
            ({"sequence": [4, 7]}, 10),
            ({"sequence": [10, 13]}, 16),
        ),
        test_input={"sequence": [3, 6, 9]},
        expected_output=12,
        tags=("reasoning", "sequence"),
    ),
    _family_task(
        "ho_mr_geometric",
        kind=TaskKind.MULTI_STEP_REASONING,
        family_id="multi_step_reasoning",
        split=Split.HELD_OUT,
        difficulty=Difficulty.MEDIUM,
        request="Continue a sequence that grows by a different rule than the first family.",
        success_condition="the next term equals the expected term",
        verifier="chain_matches",
        examples=(
            ({"sequence": [2, 6]}, 18),
            ({"sequence": [5, 10]}, 30),
        ),
        test_input={"sequence": [3, 9, 27]},
        expected_output=81,
        seed=601,
        tags=("reasoning", "held-out"),
    ),
    _family_task(
        "ho_mr_ambiguous_step",
        kind=TaskKind.MULTI_STEP_REASONING,
        family_id="multi_step_reasoning",
        split=Split.HELD_OUT,
        difficulty=Difficulty.HARD,
        request="Continue a sequence whose rule one example cannot decide.",
        success_condition="the next term equals the expected term",
        verifier="chain_matches",
        examples=(({"sequence": [4, 16]}, 64),),
        test_input={"sequence": [2, 4]},
        expected_output=16,
        seed=602,
        rule_family="ambiguity-probe",
        tags=("reasoning", "ambiguity", "held-out"),
    ),
)


BUILTIN_TASKS: tuple[EvaluationTask, ...] = (
    tuple(_TRAIN_GRID)
    + tuple(_HELD_OUT_GRID)
    + tuple(_SPATIAL)
    + tuple(_STATE_TRANSITION)
    + tuple(_PLANNING)
    + tuple(_TOOL_SELECTION)
    + tuple(_REASONING)
)

_BY_ID = {task.task_id: task for task in BUILTIN_TASKS}


def task_by_id(task_id: str) -> EvaluationTask:
    """Look a built-in task up by id, refusing an unknown one by name."""
    try:
        return _BY_ID[task_id]
    except KeyError as exc:
        raise KeyError(f"no built-in task is named {task_id!r}") from exc


def tasks_for(
    *, family_id: str = "", split: Split | None = None, difficulty: Difficulty | None = None
) -> tuple[EvaluationTask, ...]:
    """The built-in tasks matching a filter, in corpus order (deterministic)."""
    selected = []
    for task in BUILTIN_TASKS:
        if family_id and task.family_id != family_id:
            continue
        if split is not None and task.split is not split:
            continue
        if difficulty is not None and task.difficulty is not difficulty:
            continue
        selected.append(task)
    return tuple(selected)


def tasks_payload(*, include_answers: bool = False) -> list[dict[str, Any]]:
    """The corpus as rows. Answers are withheld unless explicitly asked for."""
    return [task.to_dict(include_answer=include_answers) for task in BUILTIN_TASKS]
