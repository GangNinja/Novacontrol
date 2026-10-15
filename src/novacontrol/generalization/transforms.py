"""The bounded library of grid transformation operators.

This is the phase's answer to "do not create a giant collection of hardcoded
answers to benchmark examples". Nothing here knows a task id, a dataset or an
expected output: an operator is a pure function from a grid to a grid, and a
:class:`CandidateRule` is one or two of those functions with the parameters
INFERRED from the example pairs. The solver's job is to pick parameters that
reproduce every example; the operators below are the vocabulary it can say them
in, and the vocabulary is deliberately small because a library nobody can
enumerate is a library nobody can audit.

Each operator comes with two functions:

* ``propose`` — read the example pairs and offer the parameter values that
  PLAUSIBLY explain them. Proposals are cheap filters, never answers: they may
  be wrong, and every one is checked against all examples before it is believed.
* ``apply`` — perform the transformation. Total on valid grids, so a rule that
  fails is a defect in this module and is reported as
  :data:`FailureCategory.TRANSFORMATION_DEFECT` rather than as a wrong answer.

The library's coverage is stated honestly in the report: these families are
supported, a composition of two is supported, and anything else is reported as
UNSUPPORTED instead of guessed at.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from novacontrol.generalization.grids import (
    BBox,
    Grid,
    GridObject,
    InvalidGrid,
    connectivity_labels,
)

__all__ = [
    "OPERATORS",
    "OPERATOR_NAMES",
    "GridPair",
    "Operator",
    "operator",
    "operator_names",
]

#: An example pair: the grid a task shows and the grid it expects.
GridPair = tuple[Grid, Grid]

#: How many parameter sets one operator may propose for one task. Each operator
#: is bounded so that a wide vocabulary cannot become an unbounded search.
MAX_PROPOSALS_PER_OPERATOR = 24


def _dedupe(items: Sequence[tuple[Any, ...]]) -> tuple[tuple[Any, ...], ...]:
    seen: list[tuple[Any, ...]] = []
    for item in items:
        if item not in seen:
            seen.append(item)
        if len(seen) >= MAX_PROPOSALS_PER_OPERATOR:
            break
    return tuple(seen)


def _content_box(grid: Grid, *, background: int | None = None) -> BBox | None:
    """The bounding box of everything that is not background."""
    base = grid.background() if background is None else background
    rows = [
        row
        for row in range(grid.height)
        for column in range(grid.width)
        if grid.at(row, column) != base
    ]
    columns = [
        column
        for row in range(grid.height)
        for column in range(grid.width)
        if grid.at(row, column) != base
    ]
    if not rows or not columns:
        return None
    return BBox(min(rows), min(columns), max(rows), max(columns))


def _consistent(
    propose: Callable[[Grid, Grid], tuple[Any, ...] | None],
) -> Callable[[Sequence[GridPair]], tuple[tuple[Any, ...], ...]]:
    """A proposal built from ONE pair and kept only if it holds for every pair.

    This is the shape almost every operator wants: read the parameters off the
    first example, then discard them unless the same parameters are consistent
    with all of them. It is a filter, not a fit — the real check is the exact
    comparison every candidate goes through afterwards.
    """

    def _propose(pairs: Sequence[GridPair]) -> tuple[tuple[Any, ...], ...]:
        if not pairs:
            return ()
        first = propose(*pairs[0])
        if first is None:
            return ()
        for pair in pairs[1:]:
            if propose(*pair) != first:
                return ()
        return (first,)

    return _propose


def _fixed(*params: tuple[Any, ...]) -> Callable[[Sequence[GridPair]], tuple[tuple[Any, ...], ...]]:
    """A parameter set that does not depend on the examples at all."""

    def _propose(_pairs: Sequence[GridPair]) -> tuple[tuple[Any, ...], ...]:
        return tuple(params)

    return _propose


# --------------------------------------------------------------------------- #
# Operators
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Operator:
    """One transformation, with the parameters it can be given and how to guess them."""

    name: str
    description: str
    parameters: str
    apply: Callable[[Grid, tuple[Any, ...]], Grid]
    propose: Callable[[Sequence[GridPair]], tuple[tuple[Any, ...], ...]]
    family: str = ""

    def label(self, params: tuple[Any, ...]) -> str:
        if not params:
            return self.name
        return f"{self.name}({', '.join(_label_param(item) for item in params)})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "family": self.family,
        }


def _label_param(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_label_param(item) for item in value) + "]"
    return str(value)


# -- geometric: rotations, reflections, transposes ---------------------------


def _apply_rotate(grid: Grid, params: tuple[Any, ...]) -> Grid:
    return grid.rotated(int(params[0]))


def _apply_flip(grid: Grid, params: tuple[Any, ...]) -> Grid:
    return grid.flipped(axis=str(params[0]))


def _apply_transpose(grid: Grid, params: tuple[Any, ...]) -> Grid:
    return grid.transposed()


def _apply_anti_transpose(grid: Grid, params: tuple[Any, ...]) -> Grid:
    return grid.anti_transposed()


def _apply_identity(grid: Grid, params: tuple[Any, ...]) -> Grid:
    return grid


# -- translation -------------------------------------------------------------


def _apply_translate(grid: Grid, params: tuple[Any, ...]) -> Grid:
    down, right = int(params[0]), int(params[1])
    fill = int(params[2]) if len(params) > 2 else grid.background()
    return grid.translated(down, right, fill=fill)


def _propose_translate(pairs: Sequence[GridPair]) -> tuple[tuple[Any, ...], ...]:
    """The shift that moves the content box from the input to the output.

    Only proposed when the two grids have the SAME shape and the same content
    size, because otherwise the "shift" is measuring two unrelated things.
    """

    def _one(source: Grid, target: Grid) -> tuple[Any, ...] | None:
        if source.shape != target.shape:
            return None
        source_box = _content_box(source)
        target_box = _content_box(target)
        if source_box is None or target_box is None:
            return None
        if source_box.height != target_box.height or source_box.width != target_box.width:
            return None
        return (
            target_box.top - source_box.top,
            target_box.left - source_box.left,
            source.background(),
        )

    return _consistent(_one)(pairs)


# -- recolouring -------------------------------------------------------------


def _apply_recolor_map(grid: Grid, params: tuple[Any, ...]) -> Grid:
    return grid.with_color_map(dict(params[0]))


def _apply_recolor_all(grid: Grid, params: tuple[Any, ...]) -> Grid:
    color = int(params[0])
    background = grid.background()
    return grid.with_color_map({cell: color for cell in grid.colors() if cell != background})


def _propose_recolor_map(pairs: Sequence[GridPair]) -> tuple[tuple[Any, ...], ...]:
    """A colour substitution, kept only when it is a FUNCTION on every pair.

    Two input colours mapped to two different outputs is not a substitution but
    a different rule, so the proposal is discarded rather than approximated.
    The mapping is learned as a tuple of pairs so it survives JSON round-trips.
    """

    def _one(source: Grid, target: Grid) -> tuple[Any, ...] | None:
        if source.shape != target.shape:
            return None
        mapping: dict[int, int] = {}
        for row in range(source.height):
            for column in range(source.width):
                before = source.at(row, column)
                after = target.at(row, column)
                if mapping.setdefault(before, after) != after:
                    return None
        if all(before == after for before, after in mapping.items()):
            return None  # identity is not a recolouring worth proposing
        return (tuple(sorted(mapping.items())),)

    return _consistent(_one)(pairs)


def _propose_recolor_all(pairs: Sequence[GridPair]) -> tuple[tuple[Any, ...], ...]:
    """Every colour that could be the single 'paint everything with this' colour."""

    def _one(source: Grid, target: Grid) -> tuple[Any, ...] | None:
        if source.shape != target.shape:
            return None
        colors = target.colors()
        if len(colors) != 1:
            return None
        return (colors[0],)

    result = _consistent(_one)(pairs)
    return result


# -- selection and extraction ------------------------------------------------


def _apply_keep_largest(grid: Grid, params: tuple[Any, ...]) -> Grid:
    return _keep_objects(grid, largest=True)


def _apply_keep_smallest(grid: Grid, params: tuple[Any, ...]) -> Grid:
    return _keep_objects(grid, largest=False)


def _keep_objects(grid: Grid, *, largest: bool) -> Grid:
    objects = grid.objects()
    if not objects:
        return grid
    chosen = objects[0] if largest else objects[-1]
    background = grid.background()
    rows = [[background for _ in range(grid.width)] for _ in range(grid.height)]
    for row, column in chosen.cells:
        rows[row][column] = chosen.color
    return Grid(tuple(tuple(row) for row in rows))


def _apply_crop_content(grid: Grid, params: tuple[Any, ...]) -> Grid:
    box = _content_box(grid)
    return grid if box is None else grid.cropped(box)


def _apply_extract_color(grid: Grid, params: tuple[Any, ...]) -> Grid:
    """Keep only cells of one colour, on the grid's background."""
    color = int(params[0])
    background = grid.background()
    return grid.with_color_map({cell: background for cell in grid.colors() if cell != color})


def _apply_select_color_subgrid(grid: Grid, params: tuple[Any, ...]) -> Grid:
    """The bounding box of one colour, with everything else erased."""
    color = int(params[0])
    rows = [
        row
        for row in range(grid.height)
        for column in range(grid.width)
        if grid.at(row, column) == color
    ]
    columns = [
        column
        for row in range(grid.height)
        for column in range(grid.width)
        if grid.at(row, column) == color
    ]
    if not rows or not columns:
        return grid
    box = BBox(min(rows), min(columns), max(rows), max(columns))
    background = grid.background()
    cropped = grid.cropped(box)
    return cropped.with_color_map({cell: background for cell in cropped.colors() if cell != color})


# -- tiling and mirroring ----------------------------------------------------


def _apply_tile(grid: Grid, params: tuple[Any, ...]) -> Grid:
    return grid.tiled(vertical=int(params[0]), horizontal=int(params[1]))


def _propose_tile(pairs: Sequence[GridPair]) -> tuple[tuple[Any, ...], ...]:
    """Divisor pairs whose product explains the output size on every example."""

    def _options(source: Grid, target: Grid) -> tuple[tuple[Any, ...], ...]:
        if target.height % source.height or target.width % source.width:
            return ()
        vertical = target.height // source.height
        horizontal = target.width // source.width
        if vertical < 1 or horizontal < 1:
            return ()
        options = [(vertical, horizontal)]
        # A tiling may also be a crop of a tiling (the output could be the first
        # k columns of a repeated row), so the SINGLE-axis repeats are offered
        # too and verified like everything else.
        if vertical == 1:
            options.append((1, horizontal))
        if horizontal == 1:
            options.append((vertical, 1))
        return tuple(dict.fromkeys(options))

    if not pairs:
        return ()
    candidates: list[tuple[Any, ...]] = []
    for option in _options(*pairs[0]):
        candidates.append(option)
    return _dedupe(candidates)


def _apply_mirror_tile(grid: Grid, params: tuple[Any, ...]) -> Grid:
    """The classic quadrant pattern: the grid with its mirror images around it.

    Assembled by concatenating rows rather than by drawing onto the input: an
    output twice the size cannot be pasted into a grid that is the original size
    (the paste clips, and the operator silently returned its input).
    """
    del params
    rows = tuple(grid.cells) + tuple(grid.flipped(axis="vertical").cells)
    mirrored = tuple(tuple(reversed(row)) for row in rows)
    return Grid(tuple(tuple(row) + mirror for row, mirror in zip(rows, mirrored, strict=True)))


def _propose_mirror_tile(pairs: Sequence[GridPair]) -> tuple[tuple[Any, ...], ...]:
    def _one(source: Grid, target: Grid) -> tuple[Any, ...] | None:
        if target.height != source.height * 2 or target.width != source.width * 2:
            return None
        return ()

    return _consistent(_one)(pairs)


# -- gravity -----------------------------------------------------------------


def _apply_gravity(grid: Grid, params: tuple[Any, ...]) -> Grid:
    """Every non-background cell falls until it is blocked, keeping its column/row."""
    direction = str(params[0])
    background = grid.background()
    rows = [list(row) for row in grid.cells]
    if direction in ("down", "up"):
        for column in range(grid.width):
            column_cells = [
                rows[row][column] for row in range(grid.height) if rows[row][column] != background
            ]
            if direction == "down":
                column_cells = [background] * (grid.height - len(column_cells)) + column_cells
            else:
                column_cells = column_cells + [background] * (grid.height - len(column_cells))
            for row in range(grid.height):
                rows[row][column] = column_cells[row]
    elif direction in ("right", "left"):
        for row in range(grid.height):
            row_cells = [cell for cell in rows[row] if cell != background]
            if direction == "right":
                row_cells = [background] * (grid.width - len(row_cells)) + row_cells
            else:
                row_cells = row_cells + [background] * (grid.width - len(row_cells))
            rows[row] = row_cells
    else:
        raise InvalidGrid(f"gravity direction {direction!r} is not one of down/up/left/right")
    return Grid(tuple(tuple(row) for row in rows))


# -- counting ----------------------------------------------------------------


def _apply_count_objects(grid: Grid, params: tuple[Any, ...]) -> Grid:
    return Grid(((len(grid.objects()) % 10,),))


def _apply_count_colors(grid: Grid, params: tuple[Any, ...]) -> Grid:
    """One cell per colour present, in ascending colour order, holding its count."""
    counts = grid.color_counts()
    return Grid((tuple(counts[color] % 10 for color in sorted(counts)),))


def _apply_count_non_background(grid: Grid, params: tuple[Any, ...]) -> Grid:
    background = grid.background()
    total = sum(
        1
        for row in range(grid.height)
        for column in range(grid.width)
        if grid.at(row, column) != background
    )
    return Grid(((total % 10,),))


# -- object movement and completion -----------------------------------------


def _apply_move_object(grid: Grid, params: tuple[Any, ...]) -> Grid:
    """Move the single non-background object by ``(down, right)``."""
    down, right = int(params[0]), int(params[1])
    objects = grid.objects()
    if not objects:
        return grid
    chosen = objects[0]
    background = grid.background()
    rows = [[background for _ in range(grid.width)] for _ in range(grid.height)]
    for row, column in chosen.cells:
        target_row, target_column = row + down, column + right
        if 0 <= target_row < grid.height and 0 <= target_column < grid.width:
            rows[target_row][target_column] = chosen.color
    return Grid(tuple(tuple(row) for row in rows))


def _propose_move_object(pairs: Sequence[GridPair]) -> tuple[tuple[Any, ...], ...]:
    """The displacement between the single object's box in input and output."""

    def _one(source: Grid, target: Grid) -> tuple[Any, ...] | None:
        if source.shape != target.shape:
            return None
        source_objects = source.objects()
        target_objects = target.objects()
        if len(source_objects) != 1 or len(target_objects) != 1:
            return None
        if source_objects[0].shape != target_objects[0].shape:
            return None
        return (
            target_objects[0].bbox.top - source_objects[0].bbox.top,
            target_objects[0].bbox.left - source_objects[0].bbox.left,
        )

    return _consistent(_one)(pairs)


def _apply_fill_holes(grid: Grid, params: tuple[Any, ...]) -> Grid:
    """Fill the parts of the background that the border cannot reach.

    A background region that does NOT touch the grid's border is a hole, because
    every background region that reaches the border is, by definition, outside
    the picture. The colour to fill with is the hole's own surrounding colour —
    the most common different colour among its 4-neighbours, ties broken by the
    smaller value — rather than "the background's opposite colour", so a hole
    inside a shape is filled with the shape, not with whatever else is nearby.

    The background itself is read from :meth:`Grid.background`, whose corner rule
    matters here: on a grid where the shape dominates, the majority colour is the
    SHAPE, and reading it as background inverts the picture (a shape component
    then looks like a hole and gets painted away).
    """
    del params  # the surrounding colour is measured, never supplied
    height, width = grid.height, grid.width
    background = grid.background()
    rows = [list(row) for row in grid.cells]
    for cells, colour in connectivity_labels(grid, connectivity=4):
        if colour != background:
            continue  # only the background can hold a hole
        if any(row in (0, height - 1) or column in (0, width - 1) for row, column in cells):
            continue  # it reaches the border, so it is outside the picture
        neighbours: dict[int, int] = {}
        for row, column in cells:
            for delta_row, delta_column in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                next_row, next_column = row + delta_row, column + delta_column
                if not (0 <= next_row < height and 0 <= next_column < width):
                    continue
                other = grid.at(next_row, next_column)
                if other == colour:
                    continue
                neighbours[other] = neighbours.get(other, 0) + 1
        if not neighbours:
            continue
        fill = min(neighbours, key=lambda value: (-neighbours[value], value))
        for row, column in cells:
            rows[row][column] = fill
    return Grid(tuple(tuple(row) for row in rows))


def _propose_fill_holes(pairs: Sequence[GridPair]) -> tuple[tuple[Any, ...], ...]:
    """Offered when a two-colour grid stays the same size and gains no new colour.

    A completed hole can consume the background entirely (the output of a fully
    enclosed shape is ONE colour), so requiring the colour COUNT to be unchanged
    would refuse the very case this operator exists for.
    """

    def _one(source: Grid, target: Grid) -> tuple[Any, ...] | None:
        if source.shape != target.shape:
            return None
        if len(source.colors()) != 2:
            return None
        if not set(target.colors()) <= set(source.colors()):
            return None
        return ()

    return _consistent(_one)(pairs)


def _apply_outline_objects(grid: Grid, params: tuple[Any, ...]) -> Grid:
    """Draw a one-cell border of ``color`` around every object."""
    color = int(params[0]) if params else grid.background()
    rows = [list(row) for row in grid.cells]
    for object_ in grid.objects():
        for row in range(object_.bbox.top - 1, object_.bbox.bottom + 2):
            for column in range(object_.bbox.left - 1, object_.bbox.right + 2):
                if not (0 <= row < grid.height and 0 <= column < grid.width):
                    continue
                if grid.at(row, column) == grid.background():
                    rows[row][column] = color
    return Grid(tuple(tuple(row) for row in rows))


def _propose_outline(pairs: Sequence[GridPair]) -> tuple[tuple[Any, ...], ...]:
    def _one(source: Grid, target: Grid) -> tuple[Any, ...] | None:
        if source.shape != target.shape:
            return None
        added = sorted(set(target.colors()) - set(source.colors()))
        if len(added) != 1:
            return None
        return (added[0],)

    return _consistent(_one)(pairs)


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


def _propose_color_params(pairs: Sequence[GridPair]) -> tuple[tuple[Any, ...], ...]:
    """Every colour of the input is offered as the one to select on.

    Bounded by :data:`MAX_PROPOSALS_PER_OPERATOR` and ordered by colour, so a
    grid with many colours produces a stable, truncated list rather than an
    unbounded one.
    """
    colors: list[tuple[Any, ...]] = []
    for pair in pairs:
        for color in pair[0].colors():
            colors.append((color,))
    return _dedupe(colors)


OPERATORS: tuple[Operator, ...] = (
    Operator(
        name="identity",
        description="Return the grid unchanged — the baseline that must exist.",
        parameters="none",
        apply=_apply_identity,
        propose=_fixed(()),
        family="identity",
    ),
    Operator(
        name="rotate",
        description="Rotate clockwise by quarter turns.",
        parameters="turns (1–3)",
        apply=_apply_rotate,
        propose=_fixed((1,), (2,), (3,)),
        family="rotation",
    ),
    Operator(
        name="flip",
        description="Reflect horizontally (left-right) or vertically (top-bottom).",
        parameters="axis ('horizontal' | 'vertical')",
        apply=_apply_flip,
        propose=_fixed(("horizontal",), ("vertical",)),
        family="reflection",
    ),
    Operator(
        name="transpose",
        description="Reflect across the main diagonal.",
        parameters="none",
        apply=_apply_transpose,
        propose=_fixed(()),
        family="reflection",
    ),
    Operator(
        name="anti_transpose",
        description="Reflect across the anti-diagonal.",
        parameters="none",
        apply=_apply_anti_transpose,
        propose=_fixed(()),
        family="reflection",
    ),
    Operator(
        name="translate",
        description="Shift the content by a fixed offset, filling with the background.",
        parameters="down, right, fill",
        apply=_apply_translate,
        propose=_propose_translate,
        family="translation",
    ),
    Operator(
        name="recolor_map",
        description="Substitute every colour through a learned colour→colour map.",
        parameters="pairs of (from, to)",
        apply=_apply_recolor_map,
        propose=_propose_recolor_map,
        family="recolour",
    ),
    Operator(
        name="recolor_all",
        description="Paint every non-background cell one colour.",
        parameters="color",
        apply=_apply_recolor_all,
        propose=_propose_recolor_all,
        family="recolour",
    ),
    Operator(
        name="keep_largest_object",
        description="Keep the largest connected object and erase the rest.",
        parameters="none",
        apply=_apply_keep_largest,
        propose=_fixed(()),
        family="selection",
    ),
    Operator(
        name="keep_smallest_object",
        description="Keep the smallest connected object and erase the rest.",
        parameters="none",
        apply=_apply_keep_smallest,
        propose=_fixed(()),
        family="selection",
    ),
    Operator(
        name="crop_to_content",
        description="Crop to the bounding box of everything that is not background.",
        parameters="none",
        apply=_apply_crop_content,
        propose=_fixed(()),
        family="extraction",
    ),
    Operator(
        name="extract_color",
        description="Keep only the cells of one colour.",
        parameters="color",
        apply=_apply_extract_color,
        propose=_propose_color_params,
        family="selection",
    ),
    Operator(
        name="select_color_subgrid",
        description="Crop to the bounding box of one colour, erasing the rest.",
        parameters="color",
        apply=_apply_select_color_subgrid,
        propose=_propose_color_params,
        family="extraction",
    ),
    Operator(
        name="tile",
        description="Repeat the grid vertically and horizontally.",
        parameters="vertical, horizontal",
        apply=_apply_tile,
        propose=_propose_tile,
        family="repetition",
    ),
    Operator(
        name="mirror_tile",
        description="Assemble the grid with its mirrored copies into a 2×2 quadrant.",
        parameters="none",
        apply=_apply_mirror_tile,
        propose=_propose_mirror_tile,
        family="repetition",
    ),
    Operator(
        name="gravity",
        description="Let every non-background cell fall in one direction.",
        parameters="direction ('down' | 'up' | 'left' | 'right')",
        apply=_apply_gravity,
        propose=_fixed(("down",), ("up",), ("left",), ("right",)),
        family="movement",
    ),
    Operator(
        name="move_object",
        description="Move the single object by a fixed offset, leaving no trail.",
        parameters="down, right",
        apply=_apply_move_object,
        propose=_propose_move_object,
        family="movement",
    ),
    Operator(
        name="count_objects",
        description="Answer with a 1×1 grid holding the number of connected objects.",
        parameters="none",
        apply=_apply_count_objects,
        propose=_fixed(()),
        family="counting",
    ),
    Operator(
        name="count_colors",
        description="Answer with one cell per colour present, in colour order, holding its count.",
        parameters="none",
        apply=_apply_count_colors,
        propose=_fixed(()),
        family="counting",
    ),
    Operator(
        name="count_non_background",
        description="Answer with a 1×1 grid holding the number of non-background cells.",
        parameters="none",
        apply=_apply_count_non_background,
        propose=_fixed(()),
        family="counting",
    ),
    Operator(
        name="fill_holes",
        description="Fill background regions that are not reachable from the border.",
        parameters="none",
        apply=_apply_fill_holes,
        propose=_propose_fill_holes,
        family="completion",
    ),
    Operator(
        name="outline_objects",
        description="Draw a one-cell border around every object in a new colour.",
        parameters="color",
        apply=_apply_outline_objects,
        propose=_propose_outline,
        family="completion",
    ),
)


#: The operator names in table order — the order the search walks them in, and
#: therefore part of what makes a stored run reproducible.
OPERATOR_NAMES: tuple[str, ...] = tuple(operator_.name for operator_ in OPERATORS)

_BY_NAME: Mapping[str, Operator] = {operator.name: operator for operator in OPERATORS}


def operator(name: str) -> Operator:
    """Look an operator up by name, refusing an unknown one by name."""
    try:
        return _BY_NAME[name]
    except KeyError as exc:
        raise KeyError(
            f"{name!r} is not a transformation this build has; it knows "
            f"{', '.join(sorted(_BY_NAME))}"
        ) from exc


def operator_names() -> tuple[str, ...]:
    return OPERATOR_NAMES


def operator_catalogue() -> tuple[dict[str, Any], ...]:
    """The library as rows — what the API and the report publish."""
    return tuple(operator_.to_dict() for operator_ in OPERATORS)


def object_shapes(grid: Grid) -> tuple[tuple[GridObject, ...], ...]:
    """Connected components per connectivity, for a caller that needs both."""
    return (grid.objects(connectivity=4), grid.objects(connectivity=8))
