"""Probability calibration with chronological isolation (NM-06).

The calibrator is fitted on the **earlier** part of the evaluation sample and
scored on the later part, so the improvement it reports is out-of-sample. If the
sample is too small the result is ``insufficient``: an uncalibrated probability
is never labelled calibrated.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Sequence

from novacontrol.novamarket.forecasting.metrics import calibration_error


@dataclass(frozen=True, slots=True)
class CalibrationSample:
    raw_probability: float
    outcome: bool
    observed_at: str = ""


@dataclass(frozen=True, slots=True)
class IsotonicCalibrator:
    """A monotone step map fitted with pool-adjacent-violators."""

    knots_x: tuple[float, ...]
    knots_y: tuple[float, ...]

    def calibrate(self, probability: float) -> float:
        if not self.knots_x:
            return probability
        value = max(0.0, min(1.0, probability))
        if value <= self.knots_x[0]:
            return self.knots_y[0]
        if value >= self.knots_x[-1]:
            return self.knots_y[-1]
        for index in range(1, len(self.knots_x)):
            if value <= self.knots_x[index]:
                left_x, right_x = self.knots_x[index - 1], self.knots_x[index]
                left_y, right_y = self.knots_y[index - 1], self.knots_y[index]
                if right_x == left_x:
                    return right_y
                weight = (value - left_x) / (right_x - left_x)
                return left_y * (1.0 - weight) + right_y * weight
        return self.knots_y[-1]

    def to_dict(self) -> dict[str, object]:
        return {
            "knots": [list(pair) for pair in zip(self.knots_x, self.knots_y)],
            "method": "isotonic-pava",
        }


def isotonic_fit(samples: Sequence[CalibrationSample]) -> IsotonicCalibrator:
    """Pool-adjacent-violators over (probability, outcome) pairs."""
    if not samples:
        return IsotonicCalibrator((), ())
    ordered = sorted(samples, key=lambda sample: sample.raw_probability)
    blocks: list[list[float]] = []  # [sum_y, weight, min_x, max_x]
    for sample in ordered:
        blocks.append(
            [1.0 if sample.outcome else 0.0, 1.0, sample.raw_probability, sample.raw_probability]
        )
        while len(blocks) > 1:
            left, right = blocks[-2], blocks[-1]
            if left[0] / left[1] <= right[0] / right[1]:
                break
            blocks[-2] = [
                left[0] + right[0],
                left[1] + right[1],
                left[2],
                right[3],
            ]
            blocks.pop()
    xs: list[float] = []
    ys: list[float] = []
    for total, weight, low, high in blocks:
        xs.append(low)
        ys.append(total / weight)
        if high != low:
            xs.append(high)
            ys.append(total / weight)
    return IsotonicCalibrator(tuple(xs), tuple(ys))


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    status: str
    samples: int
    holdout_samples: int
    brier_before: float | None
    brier_after: float | None
    log_loss_before: float | None
    log_loss_after: float | None
    calibration_error_before: float | None
    calibration_error_after: float | None
    calibrator: IsotonicCalibrator | None
    reason: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        def rounded(value: float | None) -> float | None:
            return None if value is None else round(value, 6)

        return {
            "status": self.status,
            "samples": self.samples,
            "holdout_samples": self.holdout_samples,
            "brier_before": rounded(self.brier_before),
            "brier_after": rounded(self.brier_after),
            "log_loss_before": rounded(self.log_loss_before),
            "log_loss_after": rounded(self.log_loss_after),
            "calibration_error_before": rounded(self.calibration_error_before),
            "calibration_error_after": rounded(self.calibration_error_after),
            "calibrator": self.calibrator.to_dict() if self.calibrator else None,
            "reason": self.reason,
            "notes": list(self.notes),
        }


def _binary_log_loss(
    rows: Sequence[tuple[float, bool]], *, floor: float = 1e-12
) -> float | None:
    if not rows:
        return None
    total = 0.0
    for probability, outcome in rows:
        clipped = max(min(probability, 1.0 - floor), floor)
        total -= _log(clipped if outcome else 1.0 - clipped)
    return total / len(rows)


def _log(value: float) -> float:
    import math

    return math.log(value)


def _brier(rows: Sequence[tuple[float, bool]]) -> float | None:
    if not rows:
        return None
    return statistics.fmean(
        (probability - (1.0 if outcome else 0.0)) ** 2
        for probability, outcome in rows
    )


def calibrate_probability(
    samples: Sequence[CalibrationSample],
    *,
    min_samples: int,
    holdout_fraction: float,
) -> CalibrationReport:
    """Fit on the earlier portion, score the improvement on the later portion."""
    ordered = list(samples)
    if len(ordered) < min_samples:
        return CalibrationReport(
            status="insufficient",
            samples=len(ordered),
            holdout_samples=0,
            brier_before=None,
            brier_after=None,
            log_loss_before=None,
            log_loss_after=None,
            calibration_error_before=None,
            calibration_error_after=None,
            calibrator=None,
            reason=(
                f"{len(ordered)} calibration samples; at least {min_samples} are "
                "required before a probability is called calibrated"
            ),
        )
    split = int(len(ordered) * (1.0 - holdout_fraction))
    if split < 2 or len(ordered) - split < 2:
        return CalibrationReport(
            status="insufficient",
            samples=len(ordered),
            holdout_samples=max(0, len(ordered) - split),
            brier_before=None,
            brier_after=None,
            log_loss_before=None,
            log_loss_after=None,
            calibration_error_before=None,
            calibration_error_after=None,
            calibrator=None,
            reason="the calibration and holdout portions are too small",
        )
    train, holdout = ordered[:split], ordered[split:]
    calibrator = isotonic_fit(train)
    raw = [(sample.raw_probability, sample.outcome) for sample in holdout]
    adjusted = [
        (calibrator.calibrate(sample.raw_probability), sample.outcome)
        for sample in holdout
    ]
    return CalibrationReport(
        status="calibrated",
        samples=len(ordered),
        holdout_samples=len(holdout),
        brier_before=_brier(raw),
        brier_after=_brier(adjusted),
        log_loss_before=_binary_log_loss(raw),
        log_loss_after=_binary_log_loss(adjusted),
        calibration_error_before=calibration_error(raw),
        calibration_error_after=calibration_error(adjusted),
        calibrator=calibrator,
        notes=(
            "fitted on the earlier portion and scored on the later portion",
            "directional probabilities are calibrated; the return interval is not",
        ),
    )


__all__ = [
    "CalibrationReport",
    "CalibrationSample",
    "IsotonicCalibrator",
    "calibrate_probability",
    "isotonic_fit",
]
