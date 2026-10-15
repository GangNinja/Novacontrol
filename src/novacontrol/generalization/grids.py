"""Grids: parsing, validation and the analysis a grid task is solved from.

An ARC-style task is a handful of input/output grids and one unseen input. This
module is the part that must be RIGHT before any reasoning happens, because
every mistake here is silent: a grid read transposed, a ragged row padded to
fit, a value outside the palette accepted — each of those turns a missing
transformation into a confidently wrong answer, and the evaluation would count
it as a model failure rather than as the parsing bug it is.

So the rules are strict and NAMED. A grid is a non-empty rectangle of integers
in the ARC palette (0–9), at most :data:`MAX_GRID_DIM` on a side and
:data:`MAX_GRID_CELLS` in total; anything else raises :class:`InvalidGrid` with
the reason, and the caller reports an INVALID task rather than scoring a miss.

On top of that sit the analyses the operators are built from — connected
components (objects), bounding boxes, colour counts, symmetry — and every one of
them is deterministic and order-stable, because a solver whose answer depends on
set iteration order is not reproducible.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "MAX_GRID_CELLS",
    "MAX_GRID_DIM",
    "PALETTE",
    "BBox",
    "Grid",
    "GridObject",
    "InvalidGrid",
    "connectivity_labels",
    "grid_equals",
    "parse_grid",
]

#: The ARC palette: ten colours, 0–9. Anything else is not a grid value.
PALETTE: tuple[int, ...] = tuple(range(10))

#: A side of a grid never exceeds this. A 32×32 grid is already 1,024 cells of
#: search space; the point of the bound is that a candidate rule can be applied
#: without anyone weighing whether to.
MAX_GRID_DIM = 32

#: The total-cell bound, consistent with :data:`MAX_GRID_DIM`.
MAX_GRID_CELLS = MAX_GRID_DIM * MAX_GRID_DIM


class InvalidGrid(ValueError):
    """A value that is not a grid, carrying the reason it is not."""


@dataclass(frozen=True, slots=True)
class BBox:
    """An inclusive bounding box in row/column space."""

    top: int
    left: int
    bottom: int
    right: int

    @property
    def height(self) -> int:
        return self.bottom - self.top + 1

    @property
    def width(self) -> int:
        return self.right - self.left + 1

    @property
    def cells(self) -> int:
        return self.height * self.width

    def to_dict(self) -> dict[str, int]:
        return {
            "top": self.top,
            "left": self.left,
            "bottom": self.bottom,
            "right": self.right,
            "height": self.height,
            "width": self.width,
        }


@dataclass(frozen=True, slots=True)
class GridObject:
    """One connected component: its cells, its colour and where it sits.

    ``shape`` is the object normalized to its bounding box, which is what makes
    two objects of the same shape comparable across positions — the property a
    "move the object" or "keep the odd one out" rule needs.
    """

    cells: tuple[tuple[int, int], ...]
    color: int
    bbox: BBox

    @property
    def size(self) -> int:
        return len(self.cells)

    @property
    def height(self) -> int:
        return self.bbox.height

    @property
    def width(self) -> int:
        return self.bbox.width

    @property
    def shape(self) -> tuple[tuple[int, ...], ...]:
        """The object as a 0/1 mask of its bounding box."""
        rows = [[0 for _ in range(self.width)] for _ in range(self.height)]
        for row, column in self.cells:
            rows[row - self.bbox.top][column - self.bbox.left] = 1
        return tuple(tuple(row) for row in rows)

    @property
    def anchor(self) -> tuple[int, int]:
        """Top-left corner — the stable identity of an object for ordering."""
        return (self.bbox.top, self.bbox.left)

    def to_dict(self) -> dict[str, Any]:
        return {
            "color": self.color,
            "size": self.size,
            "bbox": self.bbox.to_dict(),
            "cells": [[row, column] for row, column in self.cells],
            "shape": [list(row) for row in self.shape],
        }


@dataclass(frozen=True, slots=True)
class Grid:
    """An immutable grid. Every transformation returns a new one."""

    cells: tuple[tuple[int, ...], ...]

    def __post_init__(self) -> None:
        _validate_cells(self.cells)

    # -- shape -------------------------------------------------------------
    @property
    def height(self) -> int:
        return len(self.cells)

    @property
    def width(self) -> int:
        return len(self.cells[0]) if self.cells else 0

    @property
    def size(self) -> int:
        return self.height * self.width

    @property
    def shape(self) -> tuple[int, int]:
        return (self.height, self.width)

    def at(self, row: int, column: int) -> int:
        return self.cells[row][column]

    def rows(self) -> list[list[int]]:
        return [list(row) for row in self.cells]

    def render(self) -> str:
        """A compact text form, for a report or a failure message."""
        return "\n".join("".join(str(cell) for cell in row) for row in self.cells)

    # -- colour analysis ---------------------------------------------------
    def colors(self) -> tuple[int, ...]:
        """Every colour present, ascending — a stable, comparable list."""
        return tuple(sorted({cell for row in self.cells for cell in row}))

    def count(self, color: int) -> int:
        return sum(1 for row in self.cells for cell in row if cell == color)

    def color_counts(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for row in self.cells:
            for cell in row:
                counts[cell] = counts.get(cell, 0) + 1
        return counts

    def background(self) -> int:
        """The colour the content sits ON — the four corners first, then the majority.

        Corners first because "most common colour" is simply wrong for a grid
        whose shape dominates it: on ``111/101/111`` the majority colour is the
        1 that DRAWS the shape, and treating it as background turns the picture
        inside out (objects become holes, gravity moves the wrong cells). A
        background in practice surrounds its content, so the four corners agree
        on it; when they do not, the majority decides.

        Both rules break ties by the SMALLER colour value, because a rule built
        on "the most common colour" is a different rule depending on which colour
        wins a tie — and a solver whose answer depends on that is not
        reproducible.
        """
        corners = {
            self.cells[0][0],
            self.cells[0][-1],
            self.cells[-1][0],
            self.cells[-1][-1],
        }
        if len(corners) == 1:
            return corners.pop()
        counts = self.color_counts()
        return min(counts, key=lambda color: (-counts[color], color))

    # -- objects -----------------------------------------------------------
    def objects(self, *, connectivity: int = 4) -> tuple[GridObject, ...]:
        """Connected components of non-background colours.

        Sorted by size (descending) then top-left position, so "the largest
        object" and "the first object" are the same objects every run.
        """
        if connectivity not in (4, 8):
            raise ValueError("connectivity must be 4 or 8")
        background = self.background()
        found: list[GridObject] = []
        for label_cells, color in connectivity_labels(self, connectivity=connectivity):
            if color == background:
                continue
            rows = [row for row, _ in label_cells]
            columns = [column for _, column in label_cells]
            found.append(
                GridObject(
                    cells=tuple(sorted(label_cells)),
                    color=color,
                    bbox=BBox(min(rows), min(columns), max(rows), max(columns)),
                )
            )
        found.sort(key=lambda item: (-item.size, item.anchor))
        return tuple(found)

    def object_count(self, *, connectivity: int = 4) -> int:
        return len(self.objects(connectivity=connectivity))

    # -- symmetry ----------------------------------------------------------
    def symmetries(self) -> dict[str, bool]:
        """Which reflections the grid already has. Reported, never assumed.

        A task whose input is symmetric cannot distinguish a rotation from a
        reflection, and saying so is the ambiguity rule — so the solver reads
        these rather than guessing which transformation a task "meant".
        """
        return {
            "horizontal": self == self.flipped(axis="horizontal"),
            "vertical": self == self.flipped(axis="vertical"),
            "diagonal": self == self.transposed(),
            "anti_diagonal": self == self.anti_transposed(),
            "rotational": self == self.rotated(2),
        }

    # -- transformations ---------------------------------------------------
    def rotated(self, quarter_turns: int) -> Grid:
        """Rotate clockwise by ``quarter_turns × 90°``."""
        turns = quarter_turns % 4
        result = self
        for _ in range(turns):
            height, width = result.shape
            result = Grid(
                tuple(
                    tuple(result.cells[height - 1 - row][column] for row in range(height))
                    for column in range(width)
                )
            )
        return result

    def flipped(self, *, axis: str) -> Grid:
        if axis == "horizontal":
            return Grid(tuple(tuple(reversed(row)) for row in self.cells))
        if axis == "vertical":
            return Grid(tuple(reversed(self.cells)))
        raise ValueError(f"axis must be 'horizontal' or 'vertical', not {axis!r}")

    def transposed(self) -> Grid:
        return Grid(tuple(zip(*self.cells, strict=False)))

    def anti_transposed(self) -> Grid:
        return self.rotated(1).flipped(axis="horizontal")

    def with_color_map(self, mapping: Mapping[int, int]) -> Grid:
        """Recolour, leaving unmapped colours alone."""
        return Grid(
            tuple(tuple(int(mapping.get(cell, cell)) for cell in row) for row in self.cells)
        )

    def translated(self, down: int, right: int, *, fill: int = 0) -> Grid:
        """Shift every cell, filling what enters the grid with ``fill``."""
        rows: list[tuple[int, ...]] = []
        for row in range(self.height):
            source_row = row - down
            new_row: list[int] = []
            for column in range(self.width):
                source_column = column - right
                if 0 <= source_row < self.height and 0 <= source_column < self.width:
                    new_row.append(self.cells[source_row][source_column])
                else:
                    new_row.append(fill)
            rows.append(tuple(new_row))
        return Grid(tuple(rows))

    def cropped(self, box: BBox) -> Grid:
        _check_box(self, box)
        return Grid(
            tuple(
                tuple(self.cells[row][box.left : box.right + 1])
                for row in range(box.top, box.bottom + 1)
            )
        )

    def pasted(self, other: Grid, *, at: tuple[int, int]) -> Grid:
        """Draw ``other`` onto this grid at ``(row, column)``, on-grid parts only."""
        top, left = at
        rows = [list(row) for row in self.cells]
        for row in range(other.height):
            target_row = top + row
            if not 0 <= target_row < self.height:
                continue
            for column in range(other.width):
                target_column = left + column
                if not 0 <= target_column < self.width:
                    continue
                rows[target_row][target_column] = other.cells[row][column]
        return Grid(tuple(tuple(row) for row in rows))

    def tiled(self, *, vertical: int, horizontal: int) -> Grid:
        """Repeat the grid ``vertical × horizontal`` times.

        Bounded by :data:`MAX_GRID_CELLS`: a tiling that would exceed the limit
        raises rather than quietly producing a grid no operator may touch.
        """
        vertical = max(1, int(vertical))
        horizontal = max(1, int(horizontal))
        if self.height * vertical * self.width * horizontal > MAX_GRID_CELLS:
            raise InvalidGrid(
                f"tiling {self.height}×{self.width} by {vertical}×{horizontal} exceeds the "
                f"{MAX_GRID_CELLS}-cell limit"
            )
        rows: list[tuple[int, ...]] = []
        for _ in range(vertical):
            for row in self.cells:
                rows.append(tuple(row) * horizontal)
        return Grid(tuple(rows))

    # -- conversion --------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "cells": self.rows(),
            "height": self.height,
            "width": self.width,
            "colors": list(self.colors()),
            "background": self.background(),
        }

    def fingerprint(self) -> tuple[tuple[int, ...], ...]:
        """A hashable identity, for comparing grids across records."""
        return self.cells

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.render()


# --------------------------------------------------------------------------- #
# Parsing and validation
# --------------------------------------------------------------------------- #


def parse_grid(value: Any) -> Grid:
    """Read a grid from rows, refusing anything that is not one.

    Refusals name the defect, because a caller has to be able to tell "you sent
    a ragged grid" from "you sent a colour this palette does not have" — one is
    a typo and the other is a different corpus.
    """
    if isinstance(value, Grid):
        return value
    if isinstance(value, str):
        return _parse_text_grid(value)
    if not isinstance(value, Sequence):
        raise InvalidGrid(f"a grid must be a list of rows, not {type(value).__name__}")
    rows: list[tuple[int, ...]] = []
    for index, raw_row in enumerate(value):
        if isinstance(raw_row, str):
            cells = tuple(_parse_cell(character) for character in raw_row.strip())
        elif isinstance(raw_row, Sequence):
            cells = tuple(_parse_cell(cell) for cell in raw_row)
        else:
            raise InvalidGrid(
                f"row {index} is neither a string nor a list of cells ({type(raw_row).__name__})"
            )
        if not cells:
            raise InvalidGrid(f"row {index} is empty")
        rows.append(cells)
    if not rows:
        raise InvalidGrid("a grid has no rows")
    _validate_cells(tuple(rows))
    return Grid(tuple(rows))


def _parse_text_grid(value: str) -> Grid:
    rows = [line.strip() for line in value.strip().splitlines() if line.strip()]
    if not rows:
        raise InvalidGrid("a grid rendered as text has no rows")
    return parse_grid([tuple(_parse_cell(character) for character in row) for row in rows])


def _parse_cell(value: Any) -> int:
    if isinstance(value, bool):
        raise InvalidGrid("a boolean is not a grid colour")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if len(stripped) != 1 or not stripped.isdigit():
            raise InvalidGrid(f"{value!r} is not a single palette digit")
        return int(stripped)
    raise InvalidGrid(f"{value!r} is not a grid colour")


def _validate_cells(cells: Any) -> None:
    if not isinstance(cells, tuple) or not cells:
        raise InvalidGrid("a grid has no rows")
    if len(cells) > MAX_GRID_DIM:
        raise InvalidGrid(f"a grid is at most {MAX_GRID_DIM} rows; this one has {len(cells)}")
    width = len(cells[0])
    if width == 0:
        raise InvalidGrid("row 0 is empty")
    if width > MAX_GRID_DIM:
        raise InvalidGrid(f"a grid row is at most {MAX_GRID_DIM} cells; this one has {width}")
    for index, row in enumerate(cells):
        if len(row) != width:
            raise InvalidGrid(
                f"row {index} has {len(row)} cells where row 0 has {width}: a grid is a "
                "rectangle, and a ragged one is a defect rather than something to pad"
            )
        for cell in row:
            if not isinstance(cell, int) or isinstance(cell, bool) or cell not in PALETTE:
                raise InvalidGrid(
                    f"row {index} holds {cell!r}, which is not a palette colour (0–9)"
                )
    if len(cells) * width > MAX_GRID_CELLS:
        raise InvalidGrid(
            f"a grid is at most {MAX_GRID_CELLS} cells; this one has {len(cells) * width}"
        )


def _check_box(grid: Grid, box: BBox) -> None:
    if not (0 <= box.top <= box.bottom < grid.height and 0 <= box.left <= box.right < grid.width):
        raise InvalidGrid(f"the box {box} does not lie inside a {grid.height}×{grid.width} grid")


def grid_equals(left: Any, right: Any) -> bool:
    """Whether two payloads are the SAME grid — the exact-match comparison.

    Two unreadable payloads are not equal to each other, and an unreadable one
    is never equal to a readable one: an invalid answer is a failure, never a
    lucky match.
    """
    try:
        return parse_grid(left).cells == parse_grid(right).cells
    except InvalidGrid:
        return False


def connectivity_labels(
    grid: Grid, *, connectivity: int = 4
) -> tuple[tuple[tuple[tuple[int, int], ...], int], ...]:
    """Flood-fill labels: one ``(cells, colour)`` pair per connected component.

    Deterministic by construction — cells are visited in row/column order — so
    the component ORDER is stable and a rule that depends on it is reproducible.
    """
    if connectivity not in (4, 8):
        raise ValueError("connectivity must be 4 or 8")
    neighbours = (
        ((-1, 0), (1, 0), (0, -1), (0, 1))
        if connectivity == 4
        else (
            (-1, -1),
            (-1, 0),
            (-1, 1),
            (0, -1),
            (0, 1),
            (1, -1),
            (1, 0),
            (1, 1),
        )
    )
    seen: set[tuple[int, int]] = set()
    labels: list[tuple[tuple[tuple[int, int], ...], int]] = []
    for row in range(grid.height):
        for column in range(grid.width):
            if (row, column) in seen:
                continue
            color = grid.at(row, column)
            stack = [(row, column)]
            component: list[tuple[int, int]] = []
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                component.append(current)
                current_row, current_column = current
                for delta_row, delta_column in neighbours:
                    next_row = current_row + delta_row
                    next_column = current_column + delta_column
                    if not (0 <= next_row < grid.height and 0 <= next_column < grid.width):
                        continue
                    if (next_row, next_column) in seen:
                        continue
                    if grid.at(next_row, next_column) != color:
                        continue
                    stack.append((next_row, next_column))
            labels.append((tuple(sorted(component)), color))
    return tuple(labels)


def iter_cells(grid: Grid) -> Iterable[tuple[int, int, int]]:
    """``(row, column, colour)`` in reading order."""
    for row in range(grid.height):
        for column in range(grid.width):
            yield row, column, grid.at(row, column)
