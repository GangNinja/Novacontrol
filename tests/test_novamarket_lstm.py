"""PI-07 LSTM verification.

The deterministic fixtures prove window chronology, training-only preprocessing,
regression/classification, multi-horizon configuration, checkpoint and artifact
behavior, uncertainty, comparisons, failure isolation and the real HTTP and
NovaMarket application paths.  No test asserts an accuracy target: measured
market performance belongs in the PI-07 evidence report, not a green test.
"""

from __future__ import annotations

import asyncio
import math
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Sequence
from unittest import mock

import numpy as np
import pytest

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - optional dev dependency
    TestClient = None  # type: ignore[assignment]

from novacontrol.api.app import create_app
from novacontrol.application import NovaControlApplication
from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.novamarket.contracts import ForecastDataError
from novacontrol.novamarket.features.engine import FeatureEngine
from novacontrol.novamarket.forecasting.features import PriceBar
from novacontrol.novamarket.gradient.training import build_training_matrix
from novacontrol.novamarket.lstm import (
    LSTMConfig,
    LSTMForecastingService,
    LSTMUnavailableError,
    LSTMValidationError,
    SequenceScaler,
    build_sequences,
    fit_scaler,
    scaled_sequence,
    train_lstm,
)
from novacontrol.novamarket.lstm.artifacts import LSTMArtifactStore
from novacontrol.novamarket.lstm.models import LSTM_MODEL_VERSION_PREFIX
from novacontrol.novamarket.lstm.network import build_network, torch_module
from novacontrol.novamarket.marketdata.models import DataInterval, OhlcvBar
from novacontrol.novamarket.prediction.models import ForecastHorizon
from novacontrol.novamarket.service import NovaMarketService

_START = datetime(2024, 1, 1, tzinfo=UTC)


def _bars(count: int = 220) -> list[PriceBar]:
    rows: list[PriceBar] = []
    for index in range(count):
        price = 100.0 + 0.11 * index + 2.2 * math.sin(index / 4.0)
        rows.append(
            PriceBar(
                observed_at=_START + timedelta(days=index),
                open=price - 0.4,
                high=price + 0.9,
                low=price - 0.9,
                close=price,
                volume=20_000 + 13 * index,
            )
        )
    return rows


def _request_bars(rows: Sequence[PriceBar]) -> list[dict[str, Any]]:
    return [
        {
            "observed_at": row.observed_at.isoformat(),
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
        }
        for row in rows
    ]


def _ohlcv(rows: Sequence[PriceBar], instrument_id: str = "XNSE:ALPHA") -> list[OhlcvBar]:
    return [
        OhlcvBar(
            bar_id=f"fixture-{index}",
            instrument_id=instrument_id,
            provider="fixture",
            interval=DataInterval.DAY_1,
            observed_at=row.observed_at,
            retrieved_at=row.observed_at,
            source_timezone="UTC",
            currency="INR",
            open=str(row.open),
            high=str(row.high),
            low=str(row.low),
            close=str(row.close),
            volume=str(row.volume),
        )
        for index, row in enumerate(rows)
    ]


def _config(**overrides: Any) -> LSTMConfig:
    body: dict[str, Any] = {
        "sequence_length": 8,
        "hidden_size": 8,
        "num_layers": 1,
        "dropout": 0.0,
        "max_epochs": 2,
        "patience": 1,
        "min_candidates": 1,
        "min_train_samples": 20,
        "min_validation_samples": 4,
        "min_test_samples": 3,
        "batch_size": 16,
        "max_samples": 600,
        "window_bars": 30,
    }
    body.update(overrides)
    return LSTMConfig(**body)


def _matrix(rows: Sequence[PriceBar] | None = None, horizon: str = "1d") -> Any:
    series = list(rows or _bars())
    return build_training_matrix(
        feature_engine=FeatureEngine(),
        instrument_id="XNSE:ALPHA",
        horizon=ForecastHorizon(horizon),
        resolution=DataInterval.DAY_1,
        as_of=series[-1].observed_at,
        bars=_ohlcv(series),
        dataset_id="fixture",
        dataset_version="v1",
        config=_config().matrix_config(),
    )


class _Case:
    def __init__(self, service: LSTMForecastingService) -> None:
        self.service = service
        self.tmp = TemporaryDirectory()
        self.rows = _bars()

    def close(self) -> None:
        self.service.close()
        self.tmp.cleanup()


@pytest.fixture(scope="module")
def evaluated() -> Any:
    tmp = TemporaryDirectory()
    service = LSTMForecastingService(
        store_path=Path(tmp.name) / "novamarket.sqlite3",
        feature_service=SimpleNamespace(engine=FeatureEngine()),
    )
    rows = _bars()
    try:
        report = asyncio.run(
            service.evaluate(
                "XNSE:ALPHA",
                "1d",
                as_of=rows[-1].observed_at,
                bars=_request_bars(rows),
                config=_config().to_dict(),
            )
        )
        yield SimpleNamespace(
            service=service,
            report=report,
            rows=rows,
            tmp=tmp,
        )
    finally:
        service.close()
        tmp.cleanup()


class TestLSTMConfiguration:
    def test_horizon_specific_sequence_lengths_and_bounded_values(self) -> None:
        config = LSTMConfig()
        assert config.sequence_length_for(
            ForecastHorizon.MINUTES_5, DataInterval.MINUTE_1
        ) == 30
        assert config.sequence_length_for(
            ForecastHorizon.DAY_1, DataInterval.DAY_1
        ) == 30
        assert config.sequence_length_for(
            ForecastHorizon.DAYS_90, DataInterval.DAY_1
        ) == 15
        configured = LSTMConfig(sequence_length=12)
        assert (
            configured.sequence_length_for(
                ForecastHorizon.DAYS_90, DataInterval.DAY_1
            )
            == 12
        )
        with pytest.raises(LSTMValidationError):
            LSTMConfig(dropout=0.9)
        with pytest.raises(LSTMValidationError):
            LSTMConfig(max_epochs=0)
        with pytest.raises(LSTMValidationError):
            LSTMConfig(train_ratio=0.8, validation_ratio=0.2)

    def test_network_receives_values_and_an_explicit_missing_mask(self) -> None:
        network = build_network(_config(hidden_size=8), 4)
        assert network.encoder.input_size == 8
        assert network.encoder.input_size == 2 * 4
        torch = torch_module()
        values = torch.zeros(1, 3, 4)
        mask = torch.ones(1, 3, 4)
        _prediction, logits = network(values, mask)
        assert tuple(logits.shape) == (1, 3)

    def test_round_trip_and_three_bounded_candidates(self) -> None:
        config = LSTMConfig()
        restored = LSTMConfig.from_dict(config.to_dict())
        assert restored.digest == config.digest
        candidates = config.candidates()
        assert 1 <= len(candidates) <= 3
        assert len({item.digest for item in candidates}) == len(candidates)
        assert config.effective_neutral_band_pct == pytest.approx(0.5)


class TestSequenceWindows:
    def test_windows_are_chronological_and_target_aligned(self) -> None:
        matrix = _matrix()
        sequence = build_sequences(matrix, 8, indices=matrix.train_indices)
        assert sequence.raw.shape[1:] == (8, len(matrix.schema.names))
        assert sequence.positions == tuple(sorted(sequence.positions))
        for position in sequence.positions:
            end = matrix.rows[position]
            assert end.target_at > end.decision_at
            assert position >= 7
        assert np.isinf(sequence.raw).sum() == 0

    def test_reordered_matrix_is_refused_before_training(self) -> None:
        matrix = _matrix()
        reversed_rows = tuple(reversed(matrix.rows))
        bad = type(matrix)(
            rows=reversed_rows,
            schema=matrix.schema,
            x=matrix.x,
            y_return=matrix.y_return,
            y_label=matrix.y_label,
            train_indices=matrix.train_indices,
            validation_indices=matrix.validation_indices,
            test_indices=matrix.test_indices,
            feature_stats=matrix.feature_stats,
            validation_start=matrix.validation_start,
            test_start=matrix.test_start,
            validation_end=matrix.validation_end,
            test_end=matrix.test_end,
        )
        with pytest.raises(LSTMValidationError):
            build_sequences(bad, 8)

    def test_short_sequence_length_is_reported_not_padded(self) -> None:
        matrix = _matrix()
        first = min(matrix.train_indices)
        with pytest.raises(LSTMUnavailableError):
            build_sequences(matrix, 120, indices=(first,))


class TestPreprocessing:
    def test_scaler_is_fitted_on_training_rows_only_and_preserves_missing(self) -> None:
        matrix = _matrix()
        train = build_sequences(matrix, 8, indices=matrix.train_indices)
        validation = build_sequences(matrix, 8, indices=matrix.validation_indices)
        scaler = fit_scaler(train, matrix.schema)
        changed = validation.raw.copy()
        changed[:] = 9_999_999.0
        changed[0, 0, 0] = np.nan
        assert scaler.medians == fit_scaler(train, matrix.schema).medians
        values, mask = scaler.transform(changed)
        assert mask[0, 0, 0] == 0.0
        assert values[0, 0, 0] == 0.0
        # A genuine zero is scaled, not treated as missing.
        raw = np.zeros((1, 1, len(matrix.schema.names)), dtype=float)
        zero_values, zero_mask = scaler.transform(raw)
        assert zero_mask[0, 0, 0] == 1.0
        assert zero_values[0, 0, 0] == pytest.approx(
            (0.0 - scaler.medians[matrix.schema.names[0]])
            / scaler.scales[matrix.schema.names[0]]
        )

    def test_scaler_metadata_round_trip_and_inverse_target(self) -> None:
        matrix = _matrix()
        train = build_sequences(matrix, 8, indices=matrix.train_indices)
        scaler = fit_scaler(train, matrix.schema)
        restored = SequenceScaler.from_dict(scaler.to_dict())
        assert restored.to_dict() == scaler.to_dict()
        for value in (-2.0, 0.0, 1.5):
            expected = value * scaler.target_std + scaler.target_mean
            assert restored.inverse_target(value) == pytest.approx(expected)
        scaled_values, _mask, scaled_targets = scaled_sequence(train, scaler)
        assert np.isfinite(scaled_values).all()
        assert np.isfinite(scaled_targets).all()


class TestTrainingAndEvaluation:
    def test_evaluation_reports_regression_classification_and_walk_forward(
        self, evaluated: Any
    ) -> None:
        report = evaluated.report
        family = report["families"]["lstm"]
        assert family["status"] in {
            "lstm_suitable",
            "baselines_preferred",
            "provisional",
            "trained",
        }
        assert family["test"] is not None
        assert family["validation"] is not None
        assert family["classifier_test"]["confusion"]
        assert family["classifier_test"]["probabilities_calibrated"] is False
        assert family["interval"]["calibrated_probability"] is False
        assert report["test_used_for_selection"] is False
        assert report["leakage_safe"] is True
        assert report["automatic_promotion"] is False
        assert "naive_last" in report["baselines"]
        assert "xgboost" in report["pi06_comparison"]
        assert "lightgbm" in report["pi06_comparison"]
        assert report["pi06_comparison"]["xgboost"]["test"]["samples"] == family[
            "test"
        ]["samples"]
        assert any(
            fold["status"] == "evaluated" for fold in family["walk_forward"]
        )

    def test_early_stopping_checkpoint_and_training_provenance(self, evaluated: Any) -> None:
        family = evaluated.report["families"]["lstm"]
        assert family["epochs_observed"] >= 1
        assert family["best_epoch"] >= 1
        assert family["training_duration_seconds"] > 0
        assert family["inference_latency_ms"] > 0
        assert family["peak_memory_mb"] > 0
        assert family["sequence_length"] == 8
        assert family["dependency_version"]
        assert family["config"]["checkpoint_restore"] is True
        assert family["artifact"]["format"] == "torch_state_dict"

    def test_failed_training_cleans_up_memory_tracing(self) -> None:
        tracing_before = tracemalloc.is_tracing()
        with pytest.raises(AttributeError):
            train_lstm(
                matrix=None,  # type: ignore[arg-type]
                config=_config(),
                instrument_id="XNSE:ALPHA",
                horizon=ForecastHorizon.DAY_1,
                resolution=DataInterval.DAY_1,
                as_of=_START,
            )
        assert tracemalloc.is_tracing() is tracing_before

    def test_report_and_registered_model_share_the_same_report_id(
        self, evaluated: Any
    ) -> None:
        report = evaluated.report
        assert report["report_id"] == report["run_id"]
        metadata = evaluated.service._latest_lstm_model(
            ForecastHorizon.DAY_1, instrument_id="XNSE:ALPHA"
        )
        assert metadata is not None
        assert metadata.metrics["report_id"] == report["report_id"]

    def test_model_selection_rejects_a_different_target_span(
        self, evaluated: Any
    ) -> None:
        service = evaluated.service
        metadata = service._latest_lstm_model(
            ForecastHorizon.DAY_1, instrument_id="XNSE:ALPHA"
        )
        assert metadata is not None
        target_steps = int(metadata.metrics["target_steps"])
        assert (
            service._latest_lstm_model(
                ForecastHorizon.DAY_1,
                instrument_id="XNSE:ALPHA",
                target_steps=target_steps,
            )
            is not None
        )
        assert (
            service._latest_lstm_model(
                ForecastHorizon.DAY_1,
                instrument_id="XNSE:ALPHA",
                target_steps=target_steps + 1,
            )
            is None
        )

    def test_artifact_is_persisted_reloaded_and_checksum_guarded(
        self, evaluated: Any
    ) -> None:
        service = evaluated.service
        models = service.models(horizon="1d")
        assert models["total"] == 1
        metadata = service._latest_lstm_model(ForecastHorizon.DAY_1, instrument_id="XNSE:ALPHA")
        assert metadata is not None
        assert metadata.family.value == "lstm"
        assert metadata.version.startswith(LSTM_MODEL_VERSION_PREFIX)
        loaded_metadata, loaded = service._load_lstm_model(
            ForecastHorizon.DAY_1, instrument_id="XNSE:ALPHA"
        )
        assert loaded_metadata.version == metadata.version
        assert loaded.artifact.loaded_verified is True
        artifact_path = Path(loaded.artifact.state_path)
        original = artifact_path.read_bytes()
        assert original
        try:
            artifact_path.write_bytes(b"tampered")
            with pytest.raises(LSTMUnavailableError):
                service._load_lstm_model(
                    ForecastHorizon.DAY_1, instrument_id="XNSE:ALPHA"
                )
        finally:
            artifact_path.write_bytes(original)

    def test_forecast_contract_withholds_probabilities_and_marks_validity(
        self, evaluated: Any
    ) -> None:
        service = evaluated.service
        rows = evaluated.rows
        forecast = asyncio.run(
            service.generate(
                "XNSE:ALPHA",
                "1d",
                as_of=rows[-1].observed_at,
                bars=_request_bars(rows),
            )
        )
        assert forecast["forecast_id"].startswith("lstm_")
        assert forecast["model_id"] == "lstm"
        assert forecast["p_up"] is None
        assert forecast["probability_status"] == "unavailable"
        assert forecast["calibration_status"] == "not_applicable"
        assert forecast["forecast_uncertainty"] in {"low", "medium", "high", "unknown"}
        assert forecast["sequence_length"] == 8
        assert forecast["forecast_validity"] in {"valid", "stale", "invalid"}
        assert forecast["probabilities_withheld"] is True
        assert "not a recommendation" in " ".join(
            forecast["forecast_validity_conditions"]
        ).lower()

    def test_multi_horizon_matrix_uses_a_different_target(self) -> None:
        one_day = _matrix(horizon="1d")
        five_day = _matrix(horizon="5d")
        assert build_sequences(one_day, 8).raw.shape[0] > 0
        assert build_sequences(five_day, 8).raw.shape[0] > 0
        assert one_day.rows[0].target_at != five_day.rows[0].target_at
        assert one_day.schema.digest_value != "" and five_day.schema.digest_value != ""

    def test_future_rows_are_excluded_from_the_inference_series(
        self, evaluated: Any
    ) -> None:
        service = evaluated.service
        rows = evaluated.rows
        last = rows[-1]
        future = PriceBar(
            observed_at=last.observed_at + timedelta(days=1),
            open=last.close * 1.05,
            high=last.close * 1.10,
            low=last.close * 0.95,
            close=last.close * 1.05,
            volume=last.volume * 2,
        )
        forecast = asyncio.run(
            service.generate(
                "XNSE:ALPHA",
                "1d",
                as_of=last.observed_at,
                bars=_request_bars([*rows, future]),
            )
        )
        assert forecast["underlying_data_timestamp"] == last.observed_at.isoformat()
        assert float(forecast["reference_price"]) == pytest.approx(last.close)

    def test_daily_bars_cannot_establish_an_intraday_lstm_claim(
        self, evaluated: Any
    ) -> None:
        rows = evaluated.rows
        with pytest.raises(ForecastDataError):
            asyncio.run(
                evaluated.service.evaluate(
                    "XNSE:ALPHA",
                    "5m",
                    as_of=rows[-1].observed_at,
                    bars=_request_bars(rows),
                )
            )

    def test_unavailable_runtime_reports_instead_of_crashing(self) -> None:
        """A missing PyTorch runtime is a reported state, never an AttributeError."""

        tmp = TemporaryDirectory()
        service = LSTMForecastingService(
            store_path=Path(tmp.name) / "novamarket.sqlite3",
            feature_service=SimpleNamespace(engine=FeatureEngine()),
        )
        rows = _bars()
        try:
            with mock.patch(
                "novacontrol.novamarket.lstm.service.backend_available",
                return_value=False,
            ):
                report = asyncio.run(
                    service.evaluate(
                        "XNSE:ALPHA",
                        "1d",
                        as_of=rows[-1].observed_at,
                        bars=_request_bars(rows),
                        config=_config().to_dict(),
                    )
                )
            family = report["families"]["lstm"]
            assert family["status"] == "failed"
            assert "PyTorch is not installed" in family["reason"]
            assert report["splits"]["train"]["samples"] == 0
            assert report["splits"]["validation"]["samples"] == 0
            assert report["splits"]["test"]["samples"] == 0
        finally:
            service.close()
            tmp.cleanup()

    def test_one_training_failure_is_isolated_and_comparisons_survive(self) -> None:
        tmp = TemporaryDirectory()
        service = LSTMForecastingService(
            store_path=Path(tmp.name) / "novamarket.sqlite3",
            feature_service=SimpleNamespace(engine=FeatureEngine()),
        )
        rows = _bars()
        try:
            with mock.patch(
                "novacontrol.novamarket.lstm.service.train_lstm",
                side_effect=RuntimeError("deliberate failure"),
            ):
                report = asyncio.run(
                    service.evaluate(
                        "XNSE:ALPHA",
                        "1d",
                        as_of=rows[-1].observed_at,
                        bars=_request_bars(rows),
                        config=_config().to_dict(),
                    )
                )
            family = report["families"]["lstm"]
            assert family["status"] == "failed"
            assert "deliberate failure" in family["reason"]
            assert report["pi06_comparison"]["xgboost"]["status"] in {
                "trained",
                "failed",
                "unsupported",
            }
            assert report["baselines"]
            assert service.models(horizon="1d")["total"] == 0
        finally:
            service.close()
            tmp.cleanup()

    def test_artifact_store_accepts_only_checksums_and_never_reuses_a_bad_version(self) -> None:
        tmp = TemporaryDirectory()
        try:
            store = LSTMArtifactStore(Path(tmp.name))
            with pytest.raises(LSTMUnavailableError):
                store.load(
                    type(
                        "Artifact",
                        (),
                        {
                            "state_path": str(Path(tmp.name) / "missing.pt"),
                            "state_sha256": "0" * 64,
                            "state_bytes": 0,
                        },
                    )(),
                    config=LSTMConfig(),
                    input_size=3,
                )
        finally:
            tmp.cleanup()


class TestApplicationIntegration:
    def test_novamarket_service_routes_lstm_end_to_end(self) -> None:
        rows = _bars()
        tmp = TemporaryDirectory()

        async def run() -> None:
            service = NovaMarketService(data_dir=tmp.name)
            await service.initialize(reason="PI-07 integration")
            await service.enable(reason="PI-07 integration")
            try:
                status = service.forecast_status()
                assert status["lstm"]["backends"]["lstm"]["available"] is True
                assert service.forecast_contract()["lstm"]["implemented"] is True
                report = await service.forecast_train(
                    "XNSE:ALPHA",
                    "1d",
                    engine="lstm",
                    as_of=rows[-1].observed_at,
                    bars=_request_bars(rows),
                    config=_config().to_dict(),
                )
                assert report["families"]["lstm"]["test"] is not None
                forecast = await service.forecast_generate(
                    "XNSE:ALPHA",
                    "1d",
                    engine="lstm",
                    as_of=rows[-1].observed_at,
                    bars=_request_bars(rows),
                )
                assert forecast["model_id"] == "lstm"
                assert service.forecast_get(forecast["forecast_id"])["model_id"] == "lstm"
                assert service.forecast_list(engine="lstm")["total"] >= 1
                assert service.forecast_models(
                    engine="lstm", instrument_id="XNSE:ALPHA", horizon="1d"
                )["total"] >= 1
                performance = service.forecast_performance(engine="lstm")
                assert performance["performance"]
            finally:
                await service.shutdown(reason="PI-07 integration done")

        try:
            asyncio.run(run())
        finally:
            tmp.cleanup()


class _ApiCase:
    def __init__(self) -> None:
        self.tmp = TemporaryDirectory()
        self.nova: NovaControlApplication | None = None

        def build(**kwargs: Any) -> NovaControlApplication:
            kwargs["data_dir"] = self.tmp.name
            application = NovaControlApplication(**kwargs)
            self.nova = application
            return application

        self.patchers = [
            mock.patch("novacontrol.api.app.NovaControlApplication", side_effect=build),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.client = TestClient(create_app())
        self.client.__enter__()

    def close(self) -> None:
        self.client.__exit__(None, None, None)
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()


class TestFrontendIntegration:
    def test_forecast_page_exposes_the_lstm_engine_and_sequence_evidence(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "web"
            / "src"
            / "pages"
            / "ForecastModels.tsx"
        ).read_text(encoding="utf-8")
        assert 'value: "lstm"' in source
        assert 'engine === "lstm"' in source
        assert "Experimental LSTM sequence models" in source
        assert "sequence_length" in source
        assert "PI-07" in source


class TestHTTPIntegration:
    def test_lstm_train_generate_read_and_explain_routes(self) -> None:
        if TestClient is None:
            pytest.skip("fastapi.testclient requires httpx")
        case = _ApiCase()
        rows = _bars()
        try:
            trained = case.client.post(
                "/market/forecasts/train",
                json={
                    "engine": "lstm",
                    "instrument_id": "XNSE:ALPHA",
                    "horizon": "1d",
                    "as_of": rows[-1].observed_at.isoformat(),
                    "bars": _request_bars(rows),
                    "config": _config().to_dict(),
                },
            )
            assert trained.status_code == 200, trained.text
            report = trained.json()
            assert set(report["families"]) == {"lstm"}
            assert report["test_used_for_selection"] is False
            assert report["pi06_comparison"]

            created = case.client.post(
                "/market/forecasts",
                json={
                    "engine": "lstm",
                    "instrument_id": "XNSE:ALPHA",
                    "horizon": "1d",
                    "as_of": rows[-1].observed_at.isoformat(),
                    "bars": _request_bars(rows),
                },
            )
            assert created.status_code == 200, created.text
            forecast = created.json()
            assert forecast["forecast_id"].startswith("lstm_")
            assert forecast["probability_status"] == "unavailable"

            fetched = case.client.get(f"/market/forecasts/{forecast['forecast_id']}")
            assert fetched.status_code == 200, fetched.text
            assert fetched.json()["model_id"] == "lstm"

            conflict = case.client.post(
                "/market/forecasts",
                json={
                    "engine": "lstm",
                    "model_id": "xgboost",
                    "instrument_id": "XNSE:ALPHA",
                    "horizon": "1d",
                    "as_of": rows[-1].observed_at.isoformat(),
                    "bars": _request_bars(rows),
                },
            )
            assert conflict.status_code == 422, conflict.text

            explained = case.client.get(
                f"/market/forecasts/{forecast['forecast_id']}/explain"
            )
            assert explained.status_code == 200, explained.text
            assert explained.json()["feature_importance"] is not None

            models = case.client.get(
                "/market/forecasts/models",
                params={"engine": "lstm", "instrument_id": "XNSE:ALPHA", "horizon": "1d"},
            )
            assert models.status_code == 200, models.text
            assert models.json()["models"]
            performance = case.client.get(
                "/market/forecasts/performance", params={"engine": "lstm"}
            )
            assert performance.status_code == 200, performance.text
            assert performance.json()["engine"] == "lstm"
            status = case.client.get("/market/forecasts/status")
            assert status.status_code == 200
            assert "lstm" in status.json()
        finally:
            case.close()
