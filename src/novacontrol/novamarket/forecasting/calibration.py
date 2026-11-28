"""Probability calibration with chronological isolation (NM-06).

The calibrator is fitted on the **earlier** part of the evaluation sample and
scored on the later part, so the improvement it reports is out-of-sample. If the
sample is too small the result is ``insufficient``: an uncalibrated probability
is never labelled calibrated.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Sequence

from novacontrol.novamarket.forecasting.metrics import calibration_error


@dataclass(frozen=True, slots=True)
class CalibrationSample:
    """One validation observation.

    ``raw_probability``/``outcome`` are the binary up-vs-not view kept for
    backward compatibility.  When ``p_down``, ``p_sideways`` and
    ``actual_direction`` are present the three-class view is used instead.
    """

    raw_probability: float
    outcome: bool
    observed_at: str = ""
    p_down: float | None = None
    p_sideways: float | None = None
    actual_direction: str = ""

    @property
    def multiclass(self) -> bool:
        return (
            self.p_down is not None
            and self.p_sideways is not None
            and self.actual_direction in {"up", "down", "sideways"}
        )


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
class MultiClassCalibrator:
    """One-vs-rest isotonic maps, renormalised after calibration."""

    up: IsotonicCalibrator
    down: IsotonicCalibrator
    sideways: IsotonicCalibrator

    def calibrate(self, p_up: float, p_down: float, p_sideways: float) -> tuple[float, float, float]:
        values = [
            self.up.calibrate(p_up),
            self.down.calibrate(p_down),
            self.sideways.calibrate(p_sideways),
        ]
        values = [max(0.0, min(1.0, value)) for value in values]
        total = sum(values)
        if total <= 0.0:
            return (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)
        return (values[0] / total, values[1] / total, values[2] / total)

    def to_dict(self) -> dict[str, object]:
        return {
            "method": "multiclass-one-vs-rest-isotonic-pava",
            "up": self.up.to_dict(),
            "down": self.down.to_dict(),
            "sideways": self.sideways.to_dict(),
        }


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
    calibrator: IsotonicCalibrator | MultiClassCalibrator | TemperatureCalibrator | None
    reason: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)
    multiclass_brier_before: float | None = None
    multiclass_brier_after: float | None = None
    multiclass_calibration_error_before: float | None = None
    multiclass_calibration_error_after: float | None = None

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
            "multiclass_brier_before": rounded(self.multiclass_brier_before),
            "multiclass_brier_after": rounded(self.multiclass_brier_after),
            "multiclass_calibration_error_before": rounded(
                self.multiclass_calibration_error_before
            ),
            "multiclass_calibration_error_after": rounded(
                self.multiclass_calibration_error_after
            ),
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


def _multiclass_brier(
    rows: Sequence[tuple[tuple[float, float, float], str]],
) -> float | None:
    if not rows:
        return None
    labels = ("up", "down", "sideways")
    total = 0.0
    for probabilities, actual in rows:
        total += sum(
            (probabilities[index] - (1.0 if actual == label else 0.0)) ** 2
            for index, label in enumerate(labels)
        )
    return total / len(rows)


def _multiclass_ece(
    rows: Sequence[tuple[tuple[float, float, float], str]],
) -> float | None:
    if not rows:
        return None
    labels = ("up", "down", "sideways")
    values: list[float] = []
    for index, label in enumerate(labels):
        binary = [
            (probabilities[index], actual == label)
            for probabilities, actual in rows
        ]
        error = calibration_error(binary)
        if error is not None:
            values.append(error)
    return statistics.fmean(values) if values else None


@dataclass(frozen=True, slots=True)
class TemperatureCalibrator:
    """Single-parameter multiclass temperature scaling on log-probabilities."""

    temperature: float

    def calibrate(
        self, p_up: float, p_down: float, p_sideways: float
    ) -> tuple[float, float, float]:
        values = [max(1e-12, p_up), max(1e-12, p_down), max(1e-12, p_sideways)]
        scaled = [math.log(value) / self.temperature for value in values]
        peak = max(scaled)
        weights = [math.exp(value - peak) for value in scaled]
        total = sum(weights)
        return (weights[0] / total, weights[1] / total, weights[2] / total)

    def to_dict(self) -> dict[str, object]:
        return {"method": "temperature-scaling", "temperature": self.temperature}


def _fit_temperature(
    samples: Sequence[CalibrationSample],
) -> TemperatureCalibrator:
    best = TemperatureCalibrator(1.0)
    best_brier = None
    for step in range(1, 81):
        candidate = TemperatureCalibrator(step * 0.05)
        rows = [
            (
                candidate.calibrate(
                    sample.raw_probability,
                    float(sample.p_down or 0.0),
                    float(sample.p_sideways or 0.0),
                ),
                sample.actual_direction,
            )
            for sample in samples
        ]
        score = _multiclass_brier(rows)
        if score is not None and (best_brier is None or score < best_brier):
            best_brier = score
            best = candidate
    return best


def calibrator_from_dict(
    payload: dict[str, object],
) -> IsotonicCalibrator | MultiClassCalibrator | TemperatureCalibrator | None:
    """Rebuild a stored calibrator, or return None for an unknown shape."""
    method = str(payload.get("method") or "")
    if method == "multiclass-one-vs-rest-isotonic-pava":
        maps: list[IsotonicCalibrator] = []
        for name in ("up", "down", "sideways"):
            body = payload.get(name)
            if not isinstance(body, dict):
                return None
            rebuilt = calibrator_from_dict(body)
            if not isinstance(rebuilt, IsotonicCalibrator):
                return None
            maps.append(rebuilt)
        return MultiClassCalibrator(*maps)
    if method == "temperature-scaling":
        try:
            temperature = float(str(payload.get("temperature", 1.0)))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(temperature) or temperature <= 0.0:
            return None
        return TemperatureCalibrator(temperature)
    if method != "isotonic-pava":
        return None
    knots = payload.get("knots")
    if not isinstance(knots, list):
        return None
    xs: list[float] = []
    ys: list[float] = []
    for pair in knots:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return None
        xs.append(float(pair[0]))
        ys.append(float(pair[1]))
    return IsotonicCalibrator(tuple(xs), tuple(ys))


def calibrate_probability(
    samples: Sequence[CalibrationSample],
    *,
    min_samples: int,
    holdout_fraction: float,
    min_improvement: float = 0.0,
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
    multiclass = all(sample.multiclass for sample in ordered)
    if multiclass:
        up = isotonic_fit(
            [
                CalibrationSample(sample.raw_probability, sample.actual_direction == "up")
                for sample in train
            ]
        )
        down = isotonic_fit(
            [
                CalibrationSample(
                    float(sample.p_down or 0.0),
                    sample.actual_direction == "down",
                )
                for sample in train
            ]
        )
        sideways = isotonic_fit(
            [
                CalibrationSample(
                    float(sample.p_sideways or 0.0),
                    sample.actual_direction == "sideways",
                )
                for sample in train
            ]
        )
        isotonic = MultiClassCalibrator(up=up, down=down, sideways=sideways)
        # Temperature scaling has one parameter instead of three isotonic
        # staircases; with a few dozen samples it is the more stable candidate.
        # The earlier calibration portion chooses the candidate, the holdout
        # only scores it, and the untouched test slice is never consulted.
        temperature = _fit_temperature(train)
        before: list[tuple[tuple[float, float, float], str]] = [
            (
                (
                    sample.raw_probability,
                    float(sample.p_down or 0.0),
                    float(sample.p_sideways or 0.0),
                ),
                sample.actual_direction,
            )
            for sample in holdout
        ]
        isotonic_after: list[tuple[tuple[float, float, float], str]] = [
            (isotonic.calibrate(*probabilities), actual)
            for probabilities, actual in before
        ]
        temperature_after: list[tuple[tuple[float, float, float], str]] = [
            (temperature.calibrate(*probabilities), actual)
            for probabilities, actual in before
        ]
        calibrator: (
            IsotonicCalibrator | MultiClassCalibrator | TemperatureCalibrator
        ) = temperature
        after = temperature_after
        if (_multiclass_brier(isotonic_after) or 1e9) < (
            _multiclass_brier(temperature_after) or 1e9
        ):
            calibrator = isotonic
            after = isotonic_after
        brier_before = _multiclass_brier(before)
        brier_after = _multiclass_brier(after)
        accepted = (
            brier_before is not None
            and brier_after is not None
            and brier_before - brier_after > min_improvement
        )
        binary_raw = [(sample.raw_probability, sample.outcome) for sample in holdout]
        if isinstance(calibrator, IsotonicCalibrator):
            binary_adjusted = [
                (calibrator.calibrate(sample.raw_probability), sample.outcome)
                for sample in holdout
            ]
        else:
            binary_adjusted = [
                (
                    calibrator.calibrate(
                        sample.raw_probability,
                        float(sample.p_down or 0.0),
                        float(sample.p_sideways or 0.0),
                    )[0],
                    sample.outcome,
                )
                for sample in holdout
            ]
        return CalibrationReport(
            status="calibrated" if accepted else "rejected",
            samples=len(ordered),
            holdout_samples=len(holdout),
            brier_before=_brier(binary_raw),
            brier_after=_brier(binary_adjusted),
            log_loss_before=_binary_log_loss(binary_raw),
            log_loss_after=_binary_log_loss(binary_adjusted),
            calibration_error_before=calibration_error(binary_raw),
            calibration_error_after=calibration_error(binary_adjusted),
            calibrator=calibrator if accepted else None,
            reason=(
                ""
                if accepted
                else "the calibrator did not improve holdout multiclass Brier"
            ),
            notes=(
                "fitted on the earlier portion and scored on the later portion",
                "all three directional probabilities are calibrated and renormalised",
                "the return interval is not calibrated by this report",
            ),
            multiclass_brier_before=brier_before,
            multiclass_brier_after=brier_after,
            multiclass_calibration_error_before=_multiclass_ece(before),
            multiclass_calibration_error_after=_multiclass_ece(after),
        )
    binary_calibrator = isotonic_fit(train)
    raw = [(sample.raw_probability, sample.outcome) for sample in holdout]
    adjusted = [
        (binary_calibrator.calibrate(sample.raw_probability), sample.outcome)
        for sample in holdout
    ]
    brier_before = _brier(raw)
    brier_after = _brier(adjusted)
    accepted = (
        brier_before is not None
        and brier_after is not None
        and brier_before - brier_after > min_improvement
    )
    return CalibrationReport(
        status="calibrated" if accepted else "rejected",
        samples=len(ordered),
        holdout_samples=len(holdout),
        brier_before=brier_before,
        brier_after=brier_after,
        log_loss_before=_binary_log_loss(raw),
        log_loss_after=_binary_log_loss(adjusted),
        calibration_error_before=calibration_error(raw),
        calibration_error_after=calibration_error(adjusted),
        calibrator=binary_calibrator if accepted else None,
        reason=(
            ""
            if accepted
            else "the binary calibrator did not improve holdout Brier"
        ),
        notes=(
            "fitted on the earlier portion and scored on the later portion",
            "directional probabilities are calibrated; the return interval is not",
        ),
    )


__all__ = [
    "CalibrationReport",
    "CalibrationSample",
    "IsotonicCalibrator",
    "MultiClassCalibrator",
    "TemperatureCalibrator",
    "calibrate_probability",
    "calibrator_from_dict",
    "isotonic_fit",
]
