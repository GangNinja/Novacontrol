"""Deterministic CPU training and evaluation for the PI-07 sequence family.

The implementation deliberately composes PI-06's matrix builder, metric helpers
and baseline scoring instead of rebuilding evaluation.  Only the sequence
windowing, the training-only scaler and the PyTorch optimization loop are new.
"""

from __future__ import annotations

import gc
import math
import time
import tracemalloc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

import numpy as np

from novacontrol.novamarket.gradient.models import (
    ClassifierReport,
    FeatureSchema,
    FeatureStats,
    IntervalCalibration,
)
from novacontrol.novamarket.gradient.training import (
    GradientMatrix,
    _classifier_metrics,
    _regression_metrics,
)
from novacontrol.novamarket.prediction.models import Direction, ForecastHorizon
from novacontrol.novamarket.statistical.metrics import StatisticalMetrics
from novacontrol.novamarket.statistical.models import classify_direction

from .models import (
    LSTMConfig,
    LSTMResult,
    LSTMTrainingError,
    LSTMUnavailableError,
    LSTMValidationError,
    SequenceScaler,
)
from .network import build_network, torch_module
from .sequence import SequenceSet, build_sequences, fit_scaler, scaled_sequence

NDArray = np.ndarray[Any, Any]
_LABELS = ("down", "neutral", "up")


@dataclass(slots=True)
class FittedLSTM:
    config: LSTMConfig
    schema: FeatureSchema
    scaler: SequenceScaler
    network: Any
    interval: IntervalCalibration
    feature_stats: FeatureStats
    validation: StatisticalMetrics
    test: StatisticalMetrics | None
    classifier_validation: ClassifierReport | None
    classifier_test: ClassifierReport | None
    feature_importance: tuple[Mapping[str, Any], ...]
    train_samples: int
    validation_samples: int
    test_samples: int
    training_start: datetime | None
    training_end: datetime | None
    sequence_length: int
    training_duration_seconds: float
    inference_latency_ms: float
    peak_memory_mb: float
    best_epoch: int
    epochs_observed: int
    early_stopped: bool
    checkpoint_restored: bool
    validation_loss: float
    walk_forward: tuple[Mapping[str, Any], ...] = ()
    failures: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    validation_sequences: SequenceSet | None = field(default=None, repr=False)
    test_sequences: SequenceSet | None = field(default=None, repr=False)


def _seed_everything(config: LSTMConfig) -> Any:
    torch = torch_module()
    torch.manual_seed(config.random_seed)
    np.random.seed(config.random_seed)
    try:
        torch.set_num_threads(config.num_threads)
        torch.set_num_interop_threads(1)
    except Exception:  # noqa: BLE001 - running process may already have started
        pass
    return torch


def _class_weights(labels: NDArray) -> NDArray:
    counts = np.bincount(labels.astype(np.int64), minlength=3).astype(np.float64)
    present = counts > 0
    weights = np.ones(3, dtype=np.float64)
    if present.any():
        weights[present] = len(labels) / (present.sum() * counts[present])
        weights = np.clip(weights, 0.25, 10.0)
    return weights


def _forward(
    network: Any, values: NDArray, mask: NDArray
) -> tuple[NDArray, NDArray]:
    torch = torch_module()
    with torch.no_grad():
        value_tensor = torch.from_numpy(np.asarray(values, dtype=np.float32))
        mask_tensor = torch.from_numpy(np.asarray(mask, dtype=np.float32))
        prediction, logits = network(value_tensor, mask_tensor)
        probabilities = torch.softmax(logits, dim=-1)
    return (
        prediction.detach().cpu().numpy().astype(np.float64),
        probabilities.detach().cpu().numpy().astype(np.float64),
    )


def _probabilities(probabilities: NDArray) -> tuple[str, ...]:
    return tuple(_LABELS[int(index)] for index in np.argmax(probabilities, axis=1))


def _validation_loss(
    network: Any,
    values: NDArray,
    mask: NDArray,
    targets: NDArray,
    labels: NDArray,
    *,
    config: LSTMConfig,
    torch: Any,
) -> float:
    network.eval()
    prediction, probabilities = _forward(network, values, mask)
    target_tensor = torch.from_numpy(np.asarray(targets, dtype=np.float32))
    pred_tensor = torch.from_numpy(np.asarray(prediction, dtype=np.float32))
    class_tensor = torch.from_numpy(np.asarray(labels, dtype=np.int64))
    prob_tensor = torch.from_numpy(np.asarray(probabilities, dtype=np.float32))
    if config.loss == "mse":
        regression_loss = torch.nn.functional.mse_loss(pred_tensor, target_tensor)
    else:
        regression_loss = torch.nn.functional.smooth_l1_loss(
            pred_tensor, target_tensor
        )
    class_loss = torch.nn.functional.nll_loss(
        torch.log(prob_tensor.clamp_min(1e-9)),
        class_tensor,
        weight=torch.from_numpy(_class_weights(labels).astype(np.float32)),
    )
    return float(
        regression_loss.detach().cpu().item()
        + float(config.classification_loss_weight) * class_loss.detach().cpu().item()
    )


def _train_network(
    *,
    config: LSTMConfig,
    values: NDArray,
    mask: NDArray,
    targets: NDArray,
    labels: NDArray,
    validation_values: NDArray,
    validation_mask: NDArray,
    validation_targets: NDArray,
    validation_labels: NDArray,
) -> tuple[Any, dict[str, Any]]:
    torch = _seed_everything(config)
    network = build_network(config, int(values.shape[2]))
    parameters = list(network.parameters())
    optimizer_cls = torch.optim.AdamW if config.optimizer == "adamw" else torch.optim.Adam
    optimizer = optimizer_cls(
        parameters,
        lr=float(config.learning_rate),
        weight_decay=float(config.weight_decay),
    )
    regression_loss_fn = (
        torch.nn.MSELoss()
        if config.loss == "mse"
        else torch.nn.SmoothL1Loss()
    )
    class_weight = torch.from_numpy(_class_weights(labels).astype(np.float32))
    class_loss_fn = torch.nn.CrossEntropyLoss(weight=class_weight)
    best_state: dict[str, Any] | None = None
    best_loss = math.inf
    best_epoch = 0
    epochs = 0
    bad_epochs = 0
    trace: list[dict[str, float | int]] = []
    batch_size = int(config.batch_size)
    count = len(values)
    for epoch in range(1, int(config.max_epochs) + 1):
        network.train()
        order = np.arange(count, dtype=np.int64)
        epoch_loss = 0.0
        batches = 0
        for start in range(0, count, batch_size):
            batch = order[start : start + batch_size]
            value_tensor = torch.from_numpy(
                np.asarray(values[batch], dtype=np.float32)
            )
            mask_tensor = torch.from_numpy(np.asarray(mask[batch], dtype=np.float32))
            target_tensor = torch.from_numpy(
                np.asarray(targets[batch], dtype=np.float32)
            )
            label_tensor = torch.from_numpy(np.asarray(labels[batch], dtype=np.int64))
            optimizer.zero_grad(set_to_none=True)
            predicted, logits = network(value_tensor, mask_tensor)
            regression_loss = regression_loss_fn(predicted, target_tensor)
            class_loss = class_loss_fn(logits, label_tensor)
            loss = regression_loss + float(config.classification_loss_weight) * class_loss
            if not bool(torch.isfinite(loss)):
                raise LSTMTrainingError("training loss became non-finite")
            loss.backward()
            if config.gradient_clip > 0:
                torch.nn.utils.clip_grad_norm_(parameters, float(config.gradient_clip))
            optimizer.step()
            epoch_loss += float(loss.detach().cpu().item())
            batches += 1
        epochs = epoch
        validation_loss = _validation_loss(
            network,
            validation_values,
            validation_mask,
            validation_targets,
            validation_labels,
            config=config,
            torch=torch,
        )
        if not math.isfinite(validation_loss):
            raise LSTMTrainingError(
                "validation loss became non-finite; refusing the checkpoint"
            )
        trace.append(
            {
                "epoch": epoch,
                "train_loss": epoch_loss / max(1, batches),
                "validation_loss": validation_loss,
            }
        )
        if validation_loss < best_loss - float(config.early_stopping_min_delta):
            best_loss = validation_loss
            best_epoch = epoch
            bad_epochs = 0
            if config.checkpoint_restore:
                best_state = {
                    name: tensor.detach().clone()
                    for name, tensor in network.state_dict().items()
                }
        else:
            bad_epochs += 1
            if bad_epochs >= int(config.patience):
                break
    restored = False
    if config.checkpoint_restore and best_state is not None:
        network.load_state_dict(best_state)
        restored = best_epoch < epochs
    network.eval()
    return network, {
        "best_epoch": best_epoch,
        "epochs": epochs,
        "early_stopped": epochs < int(config.max_epochs),
        "checkpoint_restored": restored,
        "validation_loss": best_loss,
        "trace": trace,
    }


def _interval(
    *,
    predicted: NDArray,
    actual: NDArray,
    level: float,
    minimum_samples: int,
) -> IntervalCalibration:
    residuals = np.asarray(actual, dtype=np.float64) - np.asarray(
        predicted, dtype=np.float64
    )
    residuals = residuals[np.isfinite(residuals)]
    if len(residuals) < minimum_samples:
        return IntervalCalibration(
            level=level,
            lower_offset=0.0,
            upper_offset=0.0,
            samples=len(residuals),
            supported=False,
        )
    alpha = (1.0 - level) / 2.0
    lower = float(np.quantile(residuals, alpha))
    upper = float(np.quantile(residuals, 1.0 - alpha))
    return IntervalCalibration(
        level=level,
        lower_offset=lower,
        upper_offset=upper,
        samples=len(residuals),
        supported=True,
    )


def _direction_labels(
    predicted: NDArray,
    lower: NDArray,
    upper: NDArray,
    *,
    config: LSTMConfig,
    interval_supported: bool,
) -> list[str]:
    labels: list[str] = []
    for index, value in enumerate(predicted):
        direction, _reason = classify_direction(
            float(value),
            float(lower[index]),
            float(upper[index]),
            neutral_band_pct=config.effective_neutral_band_pct,
            interval_supported=interval_supported,
        )
        labels.append(direction.value if isinstance(direction, Direction) else str(direction))
    return labels


def _metrics_for(
    *,
    rows: Sequence[Any],
    positions: Sequence[int],
    predicted: NDArray,
    lower: NDArray,
    upper: NDArray,
    labels: Sequence[str],
    config: LSTMConfig,
    instrument_id: str,
    horizon: ForecastHorizon,
    interval_supported: bool,
) -> StatisticalMetrics:
    return _regression_metrics(
        rows,
        positions,
        np.asarray(predicted, dtype=np.float64),
        np.asarray(lower, dtype=np.float64),
        np.asarray(upper, dtype=np.float64),
        labels,
        label="lstm",
        instrument_id=instrument_id,
        horizon=horizon.value,
        neutral_band_pct=config.effective_neutral_band_pct,
        intervals_supported=interval_supported,
    )


def _feature_importance(
    *,
    network: Any,
    values: NDArray,
    mask: NDArray,
    targets: NDArray,
    scaler: SequenceScaler,
    top: int = 12,
) -> tuple[Mapping[str, Any], ...]:
    if not len(values):
        return ()
    baseline = _forward(network, values, mask)[0]
    baseline_error = float(np.mean(np.abs(baseline - targets)))
    output: list[Mapping[str, Any]] = []
    for index, name in enumerate(scaler.schema.names):
        altered = values.copy()
        altered[:, :, index] = 0.0
        altered_mask = mask.copy()
        altered_mask[:, :, index] = 1.0
        predicted = _forward(network, altered, altered_mask)[0]
        error = float(np.mean(np.abs(predicted - targets)))
        output.append(
            {
                "name": name,
                "method": "validation median occlusion (scaled zero)",
                "mae_increase": round(error - baseline_error, 10),
                "baseline_mae": round(baseline_error, 10),
            }
        )
    output.sort(key=lambda item: abs(float(item["mae_increase"])), reverse=True)
    return tuple(output[:top])


def _fit_candidate(
    *,
    config: LSTMConfig,
    train: SequenceSet,
    validation: SequenceSet,
    scaler: SequenceScaler,
) -> tuple[Any, dict[str, Any], Any, NDArray, NDArray, NDArray, NDArray]:
    train_values, train_mask, train_targets = scaled_sequence(train, scaler)
    validation_values, validation_mask, validation_targets = scaled_sequence(
        validation, scaler
    )
    network, diagnostics = _train_network(
        config=config,
        values=train_values,
        mask=train_mask,
        targets=train_targets,
        labels=train.labels,
        validation_values=validation_values,
        validation_mask=validation_mask,
        validation_targets=validation_targets,
        validation_labels=validation.labels,
    )
    return (
        network,
        diagnostics,
        scaler,
        train_values,
        train_mask,
        validation_values,
        validation_mask,
    )


def _fit_train_validation(
    *,
    config: LSTMConfig,
    train: SequenceSet,
    validation: SequenceSet,
    scaler: SequenceScaler,
) -> tuple[Any, dict[str, Any]]:
    train_values, train_mask, train_targets = scaled_sequence(train, scaler)
    validation_values, validation_mask, validation_targets = scaled_sequence(
        validation, scaler
    )
    return _train_network(
        config=config,
        values=train_values,
        mask=train_mask,
        targets=train_targets,
        labels=train.labels,
        validation_values=validation_values,
        validation_mask=validation_mask,
        validation_targets=validation_targets,
        validation_labels=validation.labels,
    )


def _evaluate_walk_forward(
    *,
    config: LSTMConfig,
    matrix: GradientMatrix,
    instrument_id: str,
    horizon: ForecastHorizon,
    sequence_length: int,
) -> tuple[Mapping[str, Any], ...]:
    ordered = sorted(int(index) for index in matrix.test_indices)
    if len(ordered) < 2:
        return ()
    fold_count = max(1, min(3, len(ordered)))
    base_size, remainder = divmod(len(ordered), fold_count)
    blocks: list[list[int]] = []
    cursor = 0
    for fold in range(fold_count):
        size = base_size + (1 if fold < remainder else 0)
        blocks.append(ordered[cursor : cursor + size])
        cursor += size
    output: list[Mapping[str, Any]] = []
    for number, block in enumerate(blocks, 1):
        if not block:
            continue
        test_start = matrix.rows[block[0]].decision_at
        past_all = [index for index in range(block[0]) if index < len(matrix.rows)]
        past = [
            index for index in past_all if matrix.rows[index].target_at < test_start
        ]
        purged = len(past_all) - len(past)
        validation_size = max(
            config.min_validation_samples, max(1, len(past) // 5)
        )
        if len(past) - validation_size < config.min_train_samples:
            output.append(
                {
                    "fold": number,
                    "status": "insufficient_data",
                    "train_samples": max(0, len(past) - validation_size),
                    "validation_samples": min(validation_size, len(past)),
                    "test_samples": len(block),
                    "purged_rows": purged,
                    "reason": "not enough chronological history before the fold",
                }
            )
            continue
        try:
            train_indices = past[: len(past) - validation_size]
            validation_indices = past[len(past) - validation_size :]
            train = build_sequences(matrix, sequence_length, indices=train_indices)
            validation = build_sequences(
                matrix, sequence_length, indices=validation_indices
            )
            test = build_sequences(matrix, sequence_length, indices=block)
            if len(train) < config.min_train_samples:
                raise LSTMUnavailableError(
                    "fold training sequences are below the configured minimum"
                )
            if len(validation) < config.min_validation_samples:
                raise LSTMUnavailableError(
                    "fold validation sequences are below the configured minimum"
                )
            scaler = fit_scaler(train, matrix.schema)
            network, _diagnostics = _fit_train_validation(
                config=config,
                train=train,
                validation=validation,
                scaler=scaler,
            )
            validation_values, validation_mask, _ = scaled_sequence(validation, scaler)
            validation_prediction, _ = _forward(
                network, validation_values, validation_mask
            )
            validation_prediction = np.asarray(
                [scaler.inverse_target(value) for value in validation_prediction]
            )
            interval = _interval(
                predicted=validation_prediction,
                actual=validation.targets,
                level=config.interval_level,
                minimum_samples=config.min_validation_samples,
            )
            test_values, test_mask, _ = scaled_sequence(test, scaler)
            prediction, _probabilities = _forward(network, test_values, test_mask)
            prediction = np.asarray([scaler.inverse_target(value) for value in prediction])
            lower, upper = zip(*(interval.apply(float(value)) for value in prediction))
            lower_array = np.asarray(lower)
            upper_array = np.asarray(upper)
            predicted_labels = _direction_labels(
                prediction,
                lower_array,
                upper_array,
                config=config,
                interval_supported=interval.supported,
            )
            metrics = _metrics_for(
                rows=matrix.rows,
                positions=test.positions,
                predicted=prediction,
                lower=lower_array,
                upper=upper_array,
                labels=predicted_labels,
                config=config,
                instrument_id=instrument_id,
                horizon=horizon,
                interval_supported=interval.supported,
            )
            output.append(
                {
                    "fold": number,
                    "status": "evaluated",
                    "train_samples": len(train),
                    "validation_samples": len(validation),
                    "test_samples": len(test),
                    "purged_rows": purged,
                    "test_start": matrix.rows[block[0]].decision_at.isoformat(),
                    "test_end": matrix.rows[block[-1]].decision_at.isoformat(),
                    "metrics": metrics.to_dict(),
                    "interval": interval.to_dict(),
                }
            )
        except Exception as exc:  # noqa: BLE001 - one fold must not abort the report
            output.append(
                {
                    "fold": number,
                    "status": "refused",
                    "train_samples": max(0, len(past) - validation_size),
                    "validation_samples": min(validation_size, len(past)),
                    "test_samples": len(block),
                    "purged_rows": purged,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
    return tuple(output)


def _train_lstm_impl(
    *,
    matrix: GradientMatrix,
    config: LSTMConfig,
    instrument_id: str,
    horizon: ForecastHorizon,
    resolution: Any,
    as_of: datetime,
) -> FittedLSTM:
    """Train bounded candidates, select only on validation, report test once."""
    torch = _seed_everything(config)
    sequence_length = config.sequence_length_for(horizon, resolution)
    train = build_sequences(matrix, sequence_length, indices=matrix.train_indices)
    validation = build_sequences(matrix, sequence_length, indices=matrix.validation_indices)
    test = build_sequences(matrix, sequence_length, indices=matrix.test_indices)
    if len(train) < config.min_train_samples:
        raise LSTMUnavailableError(
            f"only {len(train)} training sequences; at least {config.min_train_samples} required"
        )
    if len(validation) < config.min_validation_samples:
        raise LSTMUnavailableError(
            f"only {len(validation)} validation sequences; "
            f"at least {config.min_validation_samples} required"
        )
    if len(test) < config.min_test_samples:
        raise LSTMUnavailableError(
            f"only {len(test)} test sequences; at least {config.min_test_samples} required"
        )
    started = time.perf_counter()
    failures: list[str] = []
    candidates: list[dict[str, Any]] = []
    best: tuple[
        float, Any, dict[str, Any], LSTMConfig, SequenceScaler, NDArray, NDArray, NDArray, NDArray
    ] | None = None
    for candidate in config.candidates():
        try:
            scaler = fit_scaler(train, matrix.schema)
            network, diagnostics, _scaler, tv, tm, vv, vm = _fit_candidate(
                config=candidate,
                train=train,
                validation=validation,
                scaler=scaler,
            )
            validation_prediction, _ = _forward(network, vv, vm)
            # Candidate ranking uses a price-scale-independent validation loss,
            # already computed from scaled targets; MAE on normalized targets
            # would be equivalent, but keeping the training objective makes the
            # monotonic relationship explicit.
            rank = float(diagnostics["validation_loss"])
            candidates.append(
                {
                    "config": candidate.to_dict(),
                    "config_digest": candidate.digest,
                    "validation_loss": rank,
                    "best_epoch": diagnostics["best_epoch"],
                    "epochs": diagnostics["epochs"],
                    "early_stopped": diagnostics["early_stopped"],
                }
            )
            if best is None or rank < best[0]:
                best = (rank, network, diagnostics, candidate, scaler, tv, tm, vv, vm)
        except Exception as exc:  # noqa: BLE001 - bounded candidate isolation
            failures.append(
                f"candidate hidden={candidate.hidden_size} layers={candidate.num_layers}: "
                f"{type(exc).__name__}: {exc}"
            )
    if best is None:
        raise LSTMTrainingError(
            "every LSTM candidate failed: " + "; ".join(failures[:4])
        )
    rank, network, diagnostics, chosen, scaler, _tv, _tm, validation_values, validation_mask = best
    validation_prediction_scaled, _ = _forward(network, validation_values, validation_mask)
    validation_prediction = np.asarray(
        [scaler.inverse_target(value) for value in validation_prediction_scaled]
    )
    interval = _interval(
        predicted=validation_prediction,
        actual=validation.targets,
        level=config.interval_level,
        minimum_samples=config.min_validation_samples,
    )
    # A selected validation checkpoint was fitted on the training split only.
    # It is deliberately not refitted on validation: validation is the honest
    # early-stopping and interval-calibration window.
    test_values, test_mask, _ = scaled_sequence(test, scaler)
    inference_started = time.perf_counter()
    test_prediction_scaled, probabilities = _forward(network, test_values, test_mask)
    inference_latency_ms = (
        (time.perf_counter() - inference_started) * 1000.0 / max(1, len(test))
    )
    prediction = np.asarray(
        [scaler.inverse_target(value) for value in test_prediction_scaled]
    )
    lower, upper = zip(*(interval.apply(float(value)) for value in prediction))
    lower_array = np.asarray(lower)
    upper_array = np.asarray(upper)
    predicted_labels = _direction_labels(
        prediction,
        lower_array,
        upper_array,
        config=chosen,
        interval_supported=interval.supported,
    )
    test_metrics = _metrics_for(
        rows=matrix.rows,
        positions=test.positions,
        predicted=prediction,
        lower=lower_array,
        upper=upper_array,
        labels=predicted_labels,
        config=chosen,
        instrument_id=instrument_id,
        horizon=horizon,
        interval_supported=interval.supported,
    )
    validation_labels = _direction_labels(
        validation_prediction,
        np.asarray([interval.apply(float(value))[0] for value in validation_prediction]),
        np.asarray([interval.apply(float(value))[1] for value in validation_prediction]),
        config=chosen,
        interval_supported=interval.supported,
    )
    validation_metrics = _metrics_for(
        rows=matrix.rows,
        positions=validation.positions,
        predicted=validation_prediction,
        lower=np.asarray(
            [interval.apply(float(value))[0] for value in validation_prediction]
        ),
        upper=np.asarray(
            [interval.apply(float(value))[1] for value in validation_prediction]
        ),
        labels=validation_labels,
        config=chosen,
        instrument_id=instrument_id,
        horizon=horizon,
        interval_supported=interval.supported,
    )
    classifier_validation = _classifier_metrics(
        [_LABELS[int(index)] for index in validation.labels],
        _probabilities(_forward(network, validation_values, validation_mask)[1]),
    )
    classifier_test = _classifier_metrics(
        [_LABELS[int(index)] for index in test.labels],
        _probabilities(probabilities),
    )
    importance = _feature_importance(
        network=network,
        values=validation_values,
        mask=validation_mask,
        targets=(validation.targets - scaler.target_mean) / scaler.target_std,
        scaler=scaler,
    )
    walk_forward = _evaluate_walk_forward(
        config=chosen,
        matrix=matrix,
        instrument_id=instrument_id,
        horizon=horizon,
        sequence_length=sequence_length,
    )
    elapsed = time.perf_counter() - started
    current, peak = tracemalloc.get_traced_memory()
    gc.collect()
    training_start = train.start_at
    training_end = train.end_at
    notes = [
        f"selected candidate validation loss {rank:.8f}",
        "preprocessing fit on the training windows only and persisted with the model",
        "test metrics are reported once and were not used for candidate selection",
        "sequence overlap is historical input only; each label is the final row's future target",
        f"walk-forward folds: {sum(1 for fold in walk_forward if fold.get('status') == 'evaluated')}",
        "classifier probabilities are uncalibrated and withheld from the forecast contract",
    ]
    return FittedLSTM(
        config=chosen,
        schema=matrix.schema,
        scaler=scaler,
        network=network,
        interval=interval,
        feature_stats=matrix.feature_stats,
        validation=validation_metrics,
        test=test_metrics,
        classifier_validation=classifier_validation,
        classifier_test=classifier_test,
        feature_importance=importance,
        train_samples=len(train),
        validation_samples=len(validation),
        test_samples=len(test),
        training_start=training_start,
        training_end=training_end,
        sequence_length=sequence_length,
        training_duration_seconds=elapsed,
        inference_latency_ms=inference_latency_ms,
        peak_memory_mb=peak / (1024.0 * 1024.0),
        best_epoch=int(diagnostics["best_epoch"]),
        epochs_observed=int(diagnostics["epochs"]),
        early_stopped=bool(diagnostics["early_stopped"]),
        checkpoint_restored=bool(diagnostics["checkpoint_restored"]),
        validation_loss=float(rank),
        walk_forward=walk_forward,
        failures=tuple(failures),
        notes=tuple(notes),
        validation_sequences=validation,
        test_sequences=test,
    )


def train_lstm(
    *,
    matrix: GradientMatrix,
    config: LSTMConfig,
    instrument_id: str,
    horizon: ForecastHorizon,
    resolution: Any,
    as_of: datetime,
) -> FittedLSTM:
    """Run training with scoped memory tracing, even when training fails."""
    already_tracing = tracemalloc.is_tracing()
    if not already_tracing:
        tracemalloc.start()
    try:
        return _train_lstm_impl(
            matrix=matrix,
            config=config,
            instrument_id=instrument_id,
            horizon=horizon,
            resolution=resolution,
            as_of=as_of,
        )
    finally:
        if not already_tracing and tracemalloc.is_tracing():
            tracemalloc.stop()


def fitted_to_result(
    fitted: FittedLSTM,
    *,
    version: str,
    dependency_version: str,
) -> LSTMResult:
    return LSTMResult(
        model_id="lstm",
        version=version,
        status="trained",
        config=fitted.config,
        schema=fitted.schema,
        scaler=fitted.scaler,
        validation=fitted.validation,
        test=fitted.test,
        classifier_validation=fitted.classifier_validation,
        classifier_test=fitted.classifier_test,
        interval=fitted.interval,
        feature_importance=fitted.feature_importance,
        artifact=None,
        train_samples=fitted.train_samples,
        validation_samples=fitted.validation_samples,
        test_samples=fitted.test_samples,
        training_start=fitted.training_start,
        training_end=fitted.training_end,
        dependency_version=dependency_version,
        feature_stats=fitted.feature_stats,
        sequence_length=fitted.sequence_length,
        training_duration_seconds=fitted.training_duration_seconds,
        inference_latency_ms=fitted.inference_latency_ms,
        peak_memory_mb=fitted.peak_memory_mb,
        best_epoch=fitted.best_epoch,
        epochs_observed=fitted.epochs_observed,
        early_stopped=fitted.early_stopped,
        checkpoint_restored=fitted.checkpoint_restored,
        reason="",
        notes=fitted.notes,
        failures=fitted.failures,
        walk_forward=fitted.walk_forward,
        regime_samples=(
            dict(fitted.test.samples_by_regime) if fitted.test is not None else {}
        ),
    )


__all__ = ["FittedLSTM", "fitted_to_result", "train_lstm"]
