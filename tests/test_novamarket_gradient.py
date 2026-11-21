"""PI-06 XGBoost and LightGBM verification.

The deterministic fixtures prove contracts, leakage-safe training, artifact
reloads, explainability, failure isolation and the real HTTP application path.
They deliberately do not assert an accuracy threshold; market performance is
reported separately as measured evidence.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Sequence
from unittest import mock

import numpy as np
import pytest

try:
    from fastapi.testclient import TestClient  # requires httpx
except Exception:  # pragma: no cover - optional dev dependency
    TestClient = None  # type: ignore[assignment]

from novacontrol.api.app import create_app
from novacontrol.application import NovaControlApplication
from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner
from novacontrol.novamarket.datasets.training import build_training_dataset
from novacontrol.novamarket.features.engine import FeatureEngine
from novacontrol.novamarket.features.models import FeatureSet, FeatureStatus
from novacontrol.novamarket.forecasting.features import PriceBar
from novacontrol.novamarket.gradient import (
    GRADIENT_MODEL_VERSION_PREFIX,
    FeatureSchema,
    GradientConfig,
    GradientFamily,
    GradientForecastingService,
    GradientStatus,
    GradientUnavailableError,
    GradientValidationError,
    build_training_matrix,
    feature_vector,
    selection_suitability,
)
from novacontrol.novamarket.marketdata.models import AssetClass, DataInterval
from novacontrol.novamarket.prediction.models import ForecastHorizon
from novacontrol.novamarket.statistical.validation import horizon_steps

_START = datetime(2024, 1, 1, tzinfo=UTC)


def _bars(
    count: int = 220,
    *,
    start: datetime = _START,
    step: timedelta = timedelta(days=1),
    close: Any = lambda index: 100.0 + 0.11 * index + 2.2 * math.sin(index / 4.0),
) -> list[PriceBar]:
    rows: list[PriceBar] = []
    for index in range(count):
        price = float(close(index))
        rows.append(
            PriceBar(
                observed_at=start + step * index,
                open=price - 0.4,
                high=price + 0.9,
                low=max(0.01, price - 0.9),
                close=price,
                volume=20_000 + 13 * index,
            )
        )
    return rows


def _config(**overrides: Any) -> GradientConfig:
    body: dict[str, Any] = {
        "n_estimators": 25,
        "max_depth": 3,
        "num_leaves": 7,
        "learning_rate": 0.08,
        "window_bars": 30,
        "min_train_samples": 20,
        "min_validation_samples": 4,
        "min_test_samples": 3,
        "min_candidates": 2,
        "max_samples": 400,
        "early_stopping_rounds": 5,
        "n_jobs": 1,
    }
    body.update(overrides)
    return GradientConfig(**body)


def _ohlcv(rows: Sequence[PriceBar], resolution: str) -> list[Any]:
    from novacontrol.novamarket.marketdata.models import OhlcvBar

    return [
        OhlcvBar(
            bar_id=f"fixture-{resolution}-{index}",
            instrument_id="XNSE:ALPHA",
            provider="fixture",
            interval=DataInterval(resolution),
            observed_at=row.observed_at,
            retrieved_at=row.observed_at + timedelta(minutes=1),
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


class _Case:
    """A real service over a temporary on-disk prediction/forecast store."""

    def __init__(self, rows: Sequence[PriceBar] | None = None) -> None:
        self._tmp = TemporaryDirectory()
        self.rows = list(rows or _bars())
        self.service = GradientForecastingService(
            store_path=Path(self._tmp.name) / "novamarket.sqlite3",
            feature_service=SimpleNamespace(engine=FeatureEngine()),
        )

    def close(self) -> None:
        self.service.close()
        self._tmp.cleanup()


@pytest.fixture(scope="module")
def evaluated() -> Any:
    case = _Case()
    try:
        report = asyncio.run(
            case.service.evaluate(
                "XNSE:ALPHA",
                "1d",
                as_of=case.rows[-1].observed_at,
                bars=_request_bars(case.rows),
                config=_config(),
            )
        )
        yield case, report
    finally:
        case.close()


class TestGradientConfig:
    def test_bounded_config_is_validated_and_target_is_not_test_tuned(self) -> None:
        config = _config(neutral_band_pct=0.1, round_trip_cost_bps=15.0)
        assert config.effective_neutral_band_pct == pytest.approx(0.15)
        assert "future_close" in config.to_dict()["target_definition"]
        assert "neutral" in config.to_dict()["direction_rule"]
        with pytest.raises(GradientValidationError):
            _config(num_leaves=32, max_depth=3)
        with pytest.raises(GradientValidationError):
            _config(train_ratio=0.7, validation_ratio=0.3)
        with pytest.raises(GradientValidationError):
            _config(interval_level=1.0)

    def test_config_round_trip_preserves_target_and_direction_rules(self) -> None:
        original = _config()
        restored = GradientConfig.from_dict(original.to_dict())
        assert restored.digest == original.digest
        assert restored.to_dict()["target_definition"] == original.to_dict()[
            "target_definition"
        ]
        assert restored.to_dict()["direction_rule"] == original.to_dict()[
            "direction_rule"
        ]

    def test_training_resolves_the_registered_instrument_calendar(self) -> None:
        case = _Case()
        try:
            case.service._market_data = SimpleNamespace(
                get_instrument=lambda instrument_id: SimpleNamespace(
                    asset_class=AssetClass.EQUITY, mic="XNSE"
                )
            )
            calendar = case.service._calendar_for("XNSE:ALPHA")
            assert calendar is not None
            assert calendar.mic == "XNSE"
            case.service._market_data = None
            assert case.service._calendar_for("XNSE:ALPHA") is None
        finally:
            case.close()


class TestHorizonTargets:
    @pytest.mark.parametrize(
        ("horizon", "resolution", "step"),
        [
            ("5m", "1m", timedelta(minutes=1)),
            ("15m", "5m", timedelta(minutes=5)),
            ("20m", "5m", timedelta(minutes=5)),
            ("1h", "15m", timedelta(minutes=15)),
            ("1d", "1d", timedelta(days=1)),
            ("3d", "1d", timedelta(days=1)),
            ("5d", "1d", timedelta(days=1)),
            ("30d", "1d", timedelta(days=1)),
            ("90d", "1d", timedelta(days=1)),
        ],
    )
    def test_every_supported_horizon_binds_its_target_to_real_future_bars(
        self, horizon: str, resolution: str, step: timedelta
    ) -> None:
        count = 260 if resolution == "1d" else (1_200 if resolution == "1m" else 320)
        rows = _bars(count, step=step)
        _, samples = build_training_dataset(
            dataset_id="fixture",
            dataset_version="v1",
            resolution=DataInterval(resolution),
            horizon=ForecastHorizon(horizon),
            created_at=rows[-1].observed_at,
            bars_by_instrument={"XNSE:ALPHA": _ohlcv(rows, resolution)},
            window_bars=20,
            train_ratio=0.6,
            validation_ratio=0.2,
            max_samples=2_000,
        )
        assert samples
        expected = horizon_steps(ForecastHorizon(horizon), DataInterval(resolution))
        for sample in samples:
            assert sample.target_at > sample.decision_at
            assert sample.target_at - sample.decision_at == step * expected
            # Targets are close instants, so the final bar's target lands one
            # bar duration after its observation stamp.
            assert sample.target_at <= rows[-1].observed_at + step
            assert sample.decision_at < sample.target_at

    def test_horizon_specific_matrices_do_not_share_a_label_horizon(self) -> None:
        rows = _bars(220)
        # Use the same feature-bar adapter the service uses to keep the
        # fixture faithful to production input.
        converted = _ohlcv(rows, "1d")
        one_day = build_training_matrix(
            feature_engine=FeatureEngine(),
            instrument_id="XNSE:ALPHA",
            horizon=ForecastHorizon.DAY_1,
            resolution=DataInterval("1d"),
            as_of=rows[-1].observed_at,
            bars=converted,
            dataset_id="fixture",
            dataset_version="v1",
            config=_config(),
        )
        five_day = build_training_matrix(
            feature_engine=FeatureEngine(),
            instrument_id="XNSE:ALPHA",
            horizon=ForecastHorizon.DAYS_5,
            resolution=DataInterval("1d"),
            as_of=rows[-1].observed_at,
            bars=converted,
            dataset_id="fixture",
            dataset_version="v1",
            config=_config(),
        )
        assert one_day.rows[0].target_at != five_day.rows[0].target_at
        assert (
            five_day.rows[0].target_at - five_day.rows[0].decision_at
        ) > (one_day.rows[0].target_at - one_day.rows[0].decision_at)
        # The matrices are separate model contracts with their own schema and
        # chronological boundaries; neither is a multi-output shortcut.
        assert one_day.schema.digest_value != five_day.schema.digest_value or (
            one_day.test_start != five_day.test_start
        )


class TestFeatureContract:
    def test_feature_vector_preserves_zero_and_keeps_missing_distinct(self) -> None:
        row = FeatureSet(
            feature_set_id="fs_test",
            version="pi04-feature-set-1.0",
            instrument_id="XNSE:ALPHA",
            as_of=_START,
            resolution="1d",
            profile="swing",
            values={"a": 0.0, "c": 3.0},
            statuses={
                "a": FeatureStatus.ZERO,
                "b": FeatureStatus.MISSING,
                "c": FeatureStatus.IMPUTED,
            },
            feature_names=("a", "b", "c"),
        )
        schema = FeatureSchema(
            names=("a", "b", "c"),
            feature_set_version="pi04-feature-set-1.0",
            profile="swing",
            source_names=("a", "b", "c"),
        )
        vector, values, fraction = feature_vector(row, schema)
        assert vector[0] == 0.0 and math.isfinite(vector[0])
        assert math.isnan(vector[1]) and math.isnan(vector[2])
        assert values == {"a": 0.0, "b": None, "c": None}
        assert fraction == pytest.approx(2 / 3)

    def test_schema_mismatch_is_refused_not_silently_reordered(self) -> None:
        row = FeatureSet(
            feature_set_id="fs_test",
            version="pi04-feature-set-1.0",
            instrument_id="XNSE:ALPHA",
            as_of=_START,
            resolution="1d",
            profile="swing",
            values={"a": 1.0},
            statuses={"a": FeatureStatus.VALUE},
            feature_names=("a",),
        )
        schema = FeatureSchema(
            names=("a", "not_present"),
            feature_set_version="pi04-feature-set-1.0",
            profile="swing",
            source_names=("a", "not_present"),
        )
        with pytest.raises(GradientValidationError, match="schema mismatch"):
            feature_vector(row, schema)


class TestTrainingAndPersistence:
    def test_both_families_train_regress_classify_and_persist(
        self, evaluated: Any
    ) -> None:
        case, report = evaluated
        assert set(report["families"]) == {"xgboost", "lightgbm"}
        assert report["test_used_for_selection"] is False
        assert report["selected_model"] in {"xgboost", "lightgbm"}
        for name in ("xgboost", "lightgbm"):
            family = report["families"][name]
            assert family["status"] in {
                GradientStatus.GRADIENT_SUITABLE.value,
                GradientStatus.BASELINES_PREFERRED.value,
                GradientStatus.PROVISIONAL.value,
                GradientStatus.INSUFFICIENT_DATA.value,
            }
            assert family["test"]["samples"] >= 3
            assert family["test"]["price_mae"] is not None
            assert family["test"]["directional_accuracy"] is not None
            assert family["classifier_test"]["accuracy"] is not None
            assert family["classifier_test"]["probabilities_calibrated"] is False
            assert family["feature_importance"]
            assert family["artifact"]["regressor_path"]
            assert family["dependency_version"]
            assert family["version"].startswith(GRADIENT_MODEL_VERSION_PREFIX)
            assert family["walk_forward"]
            evaluated_folds = [
                fold for fold in family["walk_forward"] if fold["status"] == "evaluated"
            ]
            assert len(evaluated_folds) >= 2
            for fold in evaluated_folds:
                assert fold["test_start"] > fold["train_end"]
                assert fold["test_start"] > fold["train_target_end"]
                assert fold["test_start"] > fold["validation_target_end"]
                assert fold["purged_samples"] >= 0
                assert fold["metrics"]["samples"] == fold["test_samples"]
                assert fold["metrics"]["price_mae"] is not None
            assert len(family["feature_schema"]["names"]) == len(
                set(family["feature_schema"]["names"])
            )

        assert set(report["baselines"]) == {
            "naive_last",
            "random_walk",
            "sma",
            "ses",
            "arima",
        }
        assert all(
            value["price_mae"] is not None for value in report["baselines"].values()
        )
        metadata = {
            item["model_id"]: item for item in case.service.models()["models"]
        }
        assert set(metadata) >= {"xgboost", "lightgbm"}
        for item in metadata.values():
            assert item["metrics"]["validation"]
            assert item["metrics"]["test"]
            assert item["metrics"]["automatic_promotion"] is False
            assert item["metrics"]["artifact"]["regressor_bytes"] > 0

    def test_selection_is_validation_only_and_baselines_are_honest(
        self, evaluated: Any
    ) -> None:
        _, report = evaluated
        valid = [
            (name, value)
            for name, value in report["families"].items()
            if value["validation"] is not None and value["test"] is not None
        ]
        expected = min(
            valid, key=lambda item: (item[1]["validation"]["price_mae"], item[0])
        )[0]
        assert report["selected_model"] == expected
        assert report["selection_metric"] == "validation_price_mae"
        assert report["preferred_overall"] in {
            "xgboost",
            "lightgbm",
            "statistical_baseline",
            "insufficient_data",
        }
        assert report["automatic_promotion"] is False

    def test_artifact_can_be_loaded_with_the_persisted_feature_schema(
        self, evaluated: Any
    ) -> None:
        case, report = evaluated
        name = report["selected_model"]
        metadata, loaded = case.service._load_model(
            GradientFamily(name), ForecastHorizon.DAY_1
        )
        assert metadata.version == report["families"][name]["version"]
        assert Path(
            metadata.metrics["artifact"]["regressor_path"]
        ).is_file()
        assert loaded.schema.names == tuple(
            report["families"][name]["feature_schema"]["names"]
        )
        assert loaded.regressor is not None
        assert loaded.version == metadata.version
        with pytest.raises(GradientUnavailableError):
            case.service._load_model(
                GradientFamily(name),
                ForecastHorizon.DAY_1,
                instrument_id="XNSE:OTHER",
            )

    def test_suitability_compares_direction_with_baselines_not_a_fixed_cutoff(
        self,
    ) -> None:
        family = SimpleNamespace(
            test=SimpleNamespace(
                price_mae=10.0,
                directional_accuracy=0.30,
                samples=40,
            )
        )
        weaker_baseline = {
            "naive": SimpleNamespace(price_mae=12.0, directional_accuracy=0.25)
        }
        stronger_direction = {
            "naive": SimpleNamespace(price_mae=12.0, directional_accuracy=0.35)
        }
        assert (
            selection_suitability(
                family, weaker_baseline, minimum_test_samples=5
            )["status"]
            == "gradient_suitable"
        )
        rejected = selection_suitability(
            family, stronger_direction, minimum_test_samples=5
        )
        assert rejected["status"] == "baselines_preferred"
        assert rejected["best_baseline_directional_accuracy"] == 0.35
        # The price-winning baseline is `cheap`; a different baseline with a
        # higher direction score must not make the comparison inconsistent.
        mixed = {
            "cheap": SimpleNamespace(price_mae=12.0, directional_accuracy=0.25),
            "sharp_but_worse": SimpleNamespace(
                price_mae=20.0, directional_accuracy=0.65
            ),
        }
        consistent = selection_suitability(
            family, mixed, minimum_test_samples=5
        )
        assert consistent["status"] == "gradient_suitable"
        assert consistent["best_baseline_model"] == "cheap"
        assert consistent["best_baseline_directional_accuracy"] == 0.25

    def test_forecast_uses_pi01_contract_and_explanation_is_local(
        self, evaluated: Any
    ) -> None:
        case, report = evaluated
        selected = report["selected_model"]
        body = asyncio.run(
            case.service.generate(
                "XNSE:ALPHA",
                "1d",
                as_of=case.rows[-1].observed_at,
                model_id=selected,
                bars=_request_bars(case.rows),
            )
        )
        assert body["forecast_id"].startswith("gbf_")
        assert body["model_id"] == selected
        assert body["forecast_horizon"] == "1d"
        assert body["predicted_price"] is not None
        assert body["predicted_return"] is not None
        assert body["predicted_direction"] in {"up", "down", "neutral"}
        assert body["lower_price"] is not None and body["upper_price"] is not None
        assert body["forecast_target_timestamp"] > body["forecast_generation_timestamp"]
        assert body["underlying_data_timestamp"] is not None
        assert body["forecast_validity"] in {"valid", "degraded"}
        assert body["p_up"] is None and body["p_down"] is None
        assert body["model_version"] == report["families"][selected]["version"]

        # Local explanations are an on-demand read, never part of normal
        # inference. If inference called TreeSHAP this guard would fail.
        with mock.patch(
            "novacontrol.novamarket.gradient.service.local_contributions",
            side_effect=AssertionError("TreeSHAP must not run during inference"),
        ) as eager:
            asyncio.run(
                case.service.generate(
                    "XNSE:ALPHA",
                    "1d",
                    as_of=case.rows[-1].observed_at,
                    model_id=selected,
                    bars=_request_bars(case.rows),
                )
            )
        assert eager.call_count == 0

        explanation = case.service.explain(body["forecast_id"])
        assert explanation["model_id"] == selected
        assert explanation["model_version"] == body["model_version"]
        assert explanation["instrument_id"] == "XNSE:ALPHA"
        assert explanation["method"].endswith("pred_contribs_tree_shap")
        assert explanation["base_value"] is not None
        assert explanation["local_contributions"]
        assert explanation["global_importance"]
        assert explanation["limitations"]
        assert all(
            math.isfinite(float(item["contribution"]))
            for item in explanation["local_contributions"]
        )

    def test_models_and_suitability_are_retrievable(self, evaluated: Any) -> None:
        case, _ = evaluated
        models = case.service.models(instrument_id="XNSE:ALPHA", horizon="1d")
        assert {item["model_id"] for item in models["models"]} >= {
            "xgboost",
            "lightgbm",
        }
        suitability = case.service.suitability(
            "XNSE:ALPHA",
            "1d",
            as_of=case.rows[-1].observed_at,
            bars=_request_bars(case.rows),
        )
        assert suitability["instrument_id"] == "XNSE:ALPHA"
        assert suitability["latest_evaluation"]
        assert suitability["automatic_promotion"] is False

    def test_one_failed_family_leaves_the_other_available(self) -> None:
        from novacontrol.novamarket.gradient.training import train_family as real_train

        case = _Case()

        def fit(family: GradientFamily, **kwargs: Any) -> Any:
            if family is GradientFamily.XGBOOST:
                raise RuntimeError("simulated fit failure")
            return real_train(family=family, **kwargs)

        try:
            with mock.patch(
                "novacontrol.novamarket.gradient.service.train_family",
                side_effect=fit,
            ) as patched:
                report = asyncio.run(
                    case.service.evaluate(
                        "XNSE:ALPHA",
                        "1d",
                        as_of=case.rows[-1].observed_at,
                        bars=_request_bars(case.rows),
                        config=_config(),
                        families=("xgboost", "lightgbm"),
                    )
                )
            assert patched.call_count == 2
            assert report["families"]["xgboost"]["status"] == "failed"
            assert report["families"]["lightgbm"]["test"] is not None
            assert report["selected_model"] == "lightgbm"
            # A fit failure must not crash the service or erase the measured
            # baseline comparison.
            assert report["baselines"]
            assert case.service.status()["implemented"] is True
        finally:
            case.close()

    def test_failure_is_isolated_to_one_horizon_family(self) -> None:
        case = _Case()
        try:
            with mock.patch(
                "novacontrol.novamarket.gradient.service.backend_available",
                side_effect=lambda family: family is GradientFamily.LIGHTGBM,
            ), mock.patch(
                "novacontrol.novamarket.gradient.service.train_family",
                wraps=__import__(
                    "novacontrol.novamarket.gradient.training",
                    fromlist=["train_family"],
                ).train_family,
            ):
                report = asyncio.run(
                    case.service.evaluate(
                        "XNSE:ALPHA",
                        "1d",
                        as_of=case.rows[-1].observed_at,
                        bars=_request_bars(case.rows),
                        config=_config(),
                        families=("xgboost", "lightgbm"),
                    )
                )
            assert report["families"]["xgboost"]["status"] == "unsupported"
            assert report["families"]["lightgbm"]["test"] is not None
        finally:
            case.close()


class TestApiIntegration:
    """The actual FastAPI application, not a mocked gradient service."""

    @pytest.fixture(autouse=True)
    def _application(self) -> Any:
        if TestClient is None:
            pytest.skip("fastapi.testclient requires httpx")
        self._tmp = TemporaryDirectory()
        self._nova: NovaControlApplication | None = None

        def build(**kwargs: Any) -> NovaControlApplication:
            kwargs["data_dir"] = self._tmp.name
            application = NovaControlApplication(**kwargs)
            self._nova = application
            return application

        patchers = [
            mock.patch("novacontrol.api.app.NovaControlApplication", side_effect=build),
            mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
            mock.patch(
                "novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner
            ),
        ]
        for patcher in patchers:
            patcher.start()
        self.client = TestClient(create_app())
        self.client.__enter__()
        yield
        self.client.__exit__(None, None, None)
        for patcher in reversed(patchers):
            patcher.stop()
        self._tmp.cleanup()

    def test_forecast_train_generate_and_explain_routes(self) -> None:
        rows = _bars(220)
        as_of = rows[-1].observed_at.isoformat()
        trained = self.client.post(
            "/market/forecasts/train",
            json={
                "engine": "gradient",
                "instrument_id": "XNSE:ALPHA",
                "horizon": "1d",
                "as_of": as_of,
                "bars": _request_bars(rows),
                "config": _config().to_dict(),
                "models": ["xgboost", "lightgbm"],
            },
        )
        assert trained.status_code == 200, trained.text
        report = trained.json()
        assert report["selected_model"] in {"xgboost", "lightgbm"}
        assert report["test_used_for_selection"] is False
        assert set(report["families"]) == {"xgboost", "lightgbm"}
        assert report["baselines"]["random_walk"]["price_mae"] is not None

        created = self.client.post(
            "/market/forecasts",
            json={
                "engine": "gradient",
                "instrument_id": "XNSE:ALPHA",
                "horizon": "1d",
                "model_id": report["selected_model"],
                "as_of": as_of,
                "bars": _request_bars(rows),
            },
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["forecast_id"].startswith("gbf_")
        assert body["model_id"] == report["selected_model"]
        assert body["p_up"] is None

        forced = self.client.post(
            "/market/forecasts",
            json={
                "engine": "xgboost",
                "instrument_id": "XNSE:ALPHA",
                "horizon": "1d",
                "as_of": as_of,
                "bars": _request_bars(rows),
            },
        )
        assert forced.status_code == 200, forced.text
        assert forced.json()["model_id"] == "xgboost"

        explained = self.client.get(
            f"/market/forecasts/{body['forecast_id']}/explain"
        )
        assert explained.status_code == 200, explained.text
        assert explained.json()["local_contributions"]

        models = self.client.get(
            "/market/forecasts/models",
            params={"engine": "gradient", "instrument_id": "XNSE:ALPHA", "horizon": "1d"},
        )
        assert models.status_code == 200, models.text
        assert {item["model_id"] for item in models.json()["models"]} >= {
            "xgboost",
            "lightgbm",
        }
        # A model listing carries no price series; the suitability block says
        # so honestly instead of inventing coverage.
        assert models.json()["suitability"]["ok"] is False
        status = self.client.get("/market/forecasts/status")
        assert status.status_code == 200, status.text
        assert status.json()["gradient"]["phase"] == "PI-06"
        performance = self.client.get(
            "/market/forecasts/performance", params={"engine": "gradient"}
        )
        assert performance.status_code == 200, performance.text
        assert performance.json()["engine"] == "gradient"

    def test_unsupported_gradient_horizon_is_a_client_error(self) -> None:
        rows = _bars(80)
        response = self.client.post(
            "/market/forecasts/train",
            json={
                "engine": "gradient",
                "instrument_id": "XNSE:ALPHA",
                "horizon": "5m",
                "as_of": rows[-1].observed_at.isoformat(),
                "bars": _request_bars(rows),
                "config": _config(min_train_samples=10).to_dict(),
            },
        )
        assert response.status_code == 422, response.text
        assert "observations" in response.json()["detail"] or "horizon" in response.json()[
            "detail"
        ]


class TestNoAuthority:
    def test_gradient_status_denies_financial_and_execution_authority(self) -> None:
        case = _Case()
        try:
            status = case.service.status()
            assert status["phase"] == "PI-06"
            assert status["implemented"] is True
            assert status["automatic_promotion"] is False
            assert status["risk_authority"] == "NM-08"
            assert status["execution_authority"] == "NM-10"
            orders = [name for name in status if "order" in name or "trade" in name]
            assert orders == []
        finally:
            case.close()
