"""Bounded walk-forward fold planning and the overlap gate.

PI-15's walk-forward requirement is met here without a second training
pipeline.  This module only *plans* folds and *refuses* leaks; every fold is
executed by ``TrainingManager``'s existing lifecycle (configuration → data
preparation → isolated worker → validation → test → artifact), so a fold is an
ordinary immutable ``TrainingConfig`` whose three chronological windows are
shifted forward from the base configuration.

Two properties are enforced rather than assumed:

* folds are strictly sequential in time — the next fold starts one microsecond
  after the previous fold's test window ends, so a boundary bar belongs to
  exactly one fold;
* any overlap between two folds is refused before the first run is launched,
  because a bar inside one fold's test window must never be available to
  another fold's training, validation or model selection.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from .models import TrainingConfig, TrainingValidationError

#: A bounded sequence: five folds is the most one walk-forward request may fan out to.
MAX_WALK_FORWARD_FOLDS = 5
DEFAULT_WALK_FORWARD_FOLDS = 3
#: The next fold starts just past the previous test end.  Bars are timestamped
#: far coarser than a microsecond, so the boundary bar can never belong to two
#: folds while the next fold still picks up the very next available bar.
_FOLD_ADVANCE = timedelta(microseconds=1)
_SPLIT_LABELS = ("train", "validation", "test")


def _dt(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        at = value
    else:
        try:
            at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise TrainingValidationError(f"{name} must be an ISO timestamp") from exc
    return at


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    """One fold's three chronological, non-overlapping windows."""

    index: int
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime
    test_start: datetime
    test_end: datetime

    @property
    def windows(self) -> tuple[tuple[str, datetime, datetime], ...]:
        return (
            ("train", self.train_start, self.train_end),
            ("validation", self.validation_start, self.validation_end),
            ("test", self.test_start, self.test_end),
        )

    def payload(self) -> dict[str, dict[str, str]]:
        """The ``TrainingConfig.splits`` payload for this fold."""
        return {
            label: {"label": label, "start": start.isoformat(), "end": end.isoformat()}
            for label, start, end in self.windows
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "window_start": self.train_start.isoformat(),
            "window_end": self.test_end.isoformat(),
            "splits": self.payload(),
        }


def _overlaps(
    first_start: datetime,
    first_end: datetime,
    second_start: datetime,
    second_end: datetime,
) -> bool:
    """Closed-interval intersection: a shared timestamp is an overlap."""
    return first_start <= second_end and second_start <= first_end


def assert_fold_separation(folds: Sequence[WalkForwardFold]) -> None:
    """Refuse any fold whose windows touch another fold's windows."""
    for position, earlier in enumerate(folds):
        for later in folds[position + 1 :]:
            for earlier_label, earlier_start, earlier_end in earlier.windows:
                for later_label, later_start, later_end in later.windows:
                    if _overlaps(earlier_start, earlier_end, later_start, later_end):
                        raise TrainingValidationError(
                            f"fold {earlier.index} {earlier_label} window "
                            f"{earlier_start.isoformat()}..{earlier_end.isoformat()} overlaps "
                            f"fold {later.index} {later_label} window "
                            f"{later_start.isoformat()}..{later_end.isoformat()}; "
                            "refusing fold overlap"
                        )


def build_walk_forward_folds(
    config: TrainingConfig, folds: int = DEFAULT_WALK_FORWARD_FOLDS
) -> tuple[WalkForwardFold, ...]:
    """Plan ``folds`` rolling chronological folds from ``config``'s windows.

    The fold window is the base configuration's ``train_start`` .. ``test_end``
    span; each fold rolls forward by exactly that span, so fold ``n + 1`` begins
    where fold ``n``'s test window ended and no bar is shared.
    """
    if isinstance(folds, bool) or not isinstance(folds, int):
        raise TrainingValidationError("folds must be an integer")
    if not 1 <= folds <= MAX_WALK_FORWARD_FOLDS:
        raise TrainingValidationError(
            f"folds must be within [1, {MAX_WALK_FORWARD_FOLDS}]"
        )
    base = {
        label: (
            _dt(config.splits[label]["start"], f"{label} start"),
            _dt(config.splits[label]["end"], f"{label} end"),
        )
        for label in _SPLIT_LABELS
    }
    span = base["test"][1] - base["train"][0]
    if span <= timedelta(0):
        raise TrainingValidationError(
            "the walk-forward window must end after it starts"
        )
    environment_end = _dt(config.environment.get("end"), "environment end")
    advance = span + _FOLD_ADVANCE
    planned: list[WalkForwardFold] = []
    for index in range(folds):
        shift = advance * index
        fold = WalkForwardFold(
            index=index,
            train_start=base["train"][0] + shift,
            train_end=base["train"][1] + shift,
            validation_start=base["validation"][0] + shift,
            validation_end=base["validation"][1] + shift,
            test_start=base["test"][0] + shift,
            test_end=base["test"][1] + shift,
        )
        if fold.test_end > environment_end:
            raise TrainingValidationError(
                f"fold {index} ends {fold.test_end.isoformat()}, past the environment "
                f"window end {environment_end.isoformat()}; request fewer folds or a "
                "shorter window"
            )
        planned.append(fold)
    assert_fold_separation(planned)
    return tuple(planned)


def fold_config(config: TrainingConfig, fold: WalkForwardFold) -> TrainingConfig:
    """The immutable per-fold configuration, ready for the training lifecycle."""
    return replace(
        config,
        name=f"{config.name} [walk-forward fold {fold.index + 1}]",
        splits=fold.payload(),
        description=(
            f"{config.description}; walk-forward fold {fold.index + 1}: "
            f"{fold.train_start.isoformat()} -> {fold.test_end.isoformat()}"
            if config.description
            else f"Walk-forward fold {fold.index + 1}: "
            f"{fold.train_start.isoformat()} -> {fold.test_end.isoformat()}"
        ),
    )


def select_fold(folds: Sequence[Mapping[str, Any]]) -> int | None:
    """The best completed fold by VALIDATION score, or None.

    Only ``validation_score`` is read.  A fold's out-of-sample (test) result is
    reported beside it and can never decide the selection, which is what keeps
    another fold's test window out of this fold's selection.
    """
    selected: int | None = None
    best: float | None = None
    for position, row in enumerate(folds):
        if str(row.get("status") or "") != "completed":
            continue
        score = row.get("validation_score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            continue
        if best is None or float(score) > best:
            best = float(score)
            index = row.get("index")
            selected = int(index) if isinstance(index, int) else position
    return selected


__all__ = [
    "DEFAULT_WALK_FORWARD_FOLDS",
    "MAX_WALK_FORWARD_FOLDS",
    "WalkForwardFold",
    "assert_fold_separation",
    "build_walk_forward_folds",
    "fold_config",
    "select_fold",
]
