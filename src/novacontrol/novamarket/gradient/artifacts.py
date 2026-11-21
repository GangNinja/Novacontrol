"""Native tree-model artifact persistence for PI-06.

Only the boosters' own serialization formats are written, plus a small JSON
metadata sidecar that pins the feature order, statistics and configuration.
The existing ``PredictionStore`` remains the authoritative registry; this module
does not create a second one.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from novacontrol.novamarket.gradient.models import (
    FeatureSchema,
    FeatureStats,
    GradientFamily,
    GradientUnavailableError,
    IntervalCalibration,
    LoadedGradient,
    ModelArtifact,
)
from novacontrol.novamarket.gradient.training import FittedGradient


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, body: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(body, encoding="utf-8")
    os.replace(temporary, path)


class GradienArtifactStore:
    """One bounded directory tree under the prediction store's data directory."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _directory(self, instrument_id: str, version: str) -> Path:
        safe_instrument = (
            instrument_id.replace(":", "_").replace("/", "_").replace("\\", "_")
        )
        if not safe_instrument or not version:
            raise GradientUnavailableError("artifact identity is incomplete")
        directory = (self.root / safe_instrument / version).resolve()
        if not directory.is_relative_to(self.root):
            raise GradientUnavailableError("artifact path escapes the model store")
        return directory

    def save(
        self,
        fitted: FittedGradient,
        *,
        instrument_id: str,
        version: str,
    ) -> ModelArtifact:
        directory = self._directory(instrument_id, version)
        directory.mkdir(parents=True, exist_ok=True)
        suffix = ".json"
        regressor_path = directory / f"regressor{suffix}"
        fitted.regressor.save_model(str(regressor_path))
        classifier_path = directory / f"classifier{suffix}"
        classifier_bytes = 0
        classifier_sha = ""
        if fitted.classifier is not None:
            fitted.classifier.save_model(str(classifier_path))
            classifier_bytes = classifier_path.stat().st_size
            classifier_sha = _sha256(classifier_path)
        metadata = {
            "schema_version": 1,
            "family": fitted.family.value,
            "version": version,
            "instrument_id": instrument_id,
            "config": fitted.config.to_dict(),
            "config_digest": fitted.config.digest,
            "regressor_iterations": fitted.regressor_iterations,
            "classifier_iterations": fitted.classifier_iterations,
            "feature_schema": fitted.schema.to_dict(),
            "feature_stats": fitted.feature_stats.to_dict(),
            "interval": fitted.interval.to_dict(),
            "feature_importance": [dict(item) for item in fitted.feature_importance],
            "classifier_present": fitted.classifier is not None,
            "backend_version": importlib.import_module(
                "xgboost"
                if fitted.family is GradientFamily.XGBOOST
                else "lightgbm"
            ).__version__,
        }
        _atomic_write(
            directory / "metadata.json",
            json.dumps(metadata, indent=2, sort_keys=True, default=str) + "\n",
        )
        artifact = ModelArtifact(
            family=fitted.family,
            version=version,
            regressor_path=str(regressor_path),
            regressor_sha256=_sha256(regressor_path),
            regressor_bytes=regressor_path.stat().st_size,
            classifier_path=str(classifier_path) if fitted.classifier is not None else "",
            classifier_sha256=classifier_sha,
            classifier_bytes=classifier_bytes,
        )
        # A write is not a success until the bytes can be read back and match.
        loaded = self.load(artifact, fitted.schema)
        if (
            loaded.artifact.regressor_sha256 != artifact.regressor_sha256
            or loaded.schema.digest_value != fitted.schema.digest_value
        ):
            raise GradientUnavailableError(
                "artifact verification failed after writing the model"
            )
        return ModelArtifact(
            family=artifact.family,
            version=artifact.version,
            regressor_path=artifact.regressor_path,
            regressor_sha256=artifact.regressor_sha256,
            regressor_bytes=artifact.regressor_bytes,
            classifier_path=artifact.classifier_path,
            classifier_sha256=artifact.classifier_sha256,
            classifier_bytes=artifact.classifier_bytes,
            loaded_verified=True,
        )

    def load(
        self,
        artifact: ModelArtifact,
        schema: FeatureSchema,
    ) -> LoadedGradient:
        directory = self._directory(
            str(Path(artifact.regressor_path).parent.parent.name),
            artifact.version,
        )
        regressor_path = Path(artifact.regressor_path).resolve()
        if not regressor_path.is_relative_to(self.root):
            raise GradientUnavailableError("artifact path escapes the model store")
        if not regressor_path.is_file():
            raise GradientUnavailableError(
                f"model artifact is missing: {artifact.version}"
            )
        if _sha256(regressor_path) != artifact.regressor_sha256:
            raise GradientUnavailableError(
                f"model artifact checksum mismatch: {artifact.version}"
            )
        metadata_path = regressor_path.parent / "metadata.json"
        if not metadata_path.is_file():
            raise GradientUnavailableError(
                f"model metadata is missing: {artifact.version}"
            )
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GradientUnavailableError(
                f"model metadata is unreadable: {artifact.version}"
            ) from exc
        if not isinstance(metadata, Mapping):
            raise GradientUnavailableError(
                f"model metadata is not an object: {artifact.version}"
            )
        if metadata.get("version") != artifact.version:
            raise GradientUnavailableError(
                "artifact version does not match its metadata; refusing to load"
            )
        if metadata.get("family") != artifact.family.value:
            raise GradientUnavailableError(
                "artifact family does not match its metadata; refusing to load"
            )
        stored_schema = metadata.get("feature_schema") or {}
        if not isinstance(stored_schema, Mapping):
            raise GradientUnavailableError(
                f"model feature schema is unreadable: {artifact.version}"
            )
        if stored_schema.get("digest") != schema.digest_value:
            raise GradientUnavailableError(
                "feature schema does not match the persisted model; refusing to "
                "silently reorder or reinterpret columns"
            )
        if artifact.family is GradientFamily.XGBOOST:
            xgb = importlib.import_module("xgboost")
            regressor = xgb.Booster()
            regressor.load_model(str(regressor_path))
            classifier = None
            if artifact.classifier_path:
                classifier_path = Path(artifact.classifier_path).resolve()
                if not classifier_path.is_relative_to(self.root):
                    raise GradientUnavailableError(
                        "classifier artifact path escapes the model store"
                    )
                if _sha256(classifier_path) != artifact.classifier_sha256:
                    raise GradientUnavailableError(
                        "classifier artifact checksum mismatch"
                    )
                classifier = xgb.Booster()
                classifier.load_model(str(classifier_path))
        else:
            lgb = importlib.import_module("lightgbm")
            regressor = lgb.Booster(model_file=str(regressor_path))
            classifier = None
            if artifact.classifier_path:
                classifier_path = Path(artifact.classifier_path).resolve()
                if not classifier_path.is_relative_to(self.root):
                    raise GradientUnavailableError(
                        "classifier artifact path escapes the model store"
                    )
                if _sha256(classifier_path) != artifact.classifier_sha256:
                    raise GradientUnavailableError(
                        "classifier artifact checksum mismatch"
                    )
                classifier = lgb.Booster(model_file=str(classifier_path))
        stats = metadata.get("feature_stats") or {}
        interval = metadata.get("interval") or {}
        if not isinstance(stats, Mapping) or not isinstance(interval, Mapping):
            raise GradientUnavailableError(
                f"model statistics are unreadable: {artifact.version}"
            )
        from novacontrol.novamarket.gradient.models import GradientConfig

        stored_config = GradientConfig.from_dict(metadata.get("config") or None)
        return LoadedGradient(
            family=artifact.family,
            version=artifact.version,
            config=stored_config,
            schema=schema,
            regressor=regressor,
            classifier=classifier,
            regressor_iterations=int(metadata.get("regressor_iterations") or 1),
            classifier_iterations=int(metadata.get("classifier_iterations") or 1),
            interval=IntervalCalibration(
                level=float(interval.get("level") or 0.8),
                lower_offset=float(interval.get("lower_offset") or 0.0),
                upper_offset=float(interval.get("upper_offset") or 0.0),
                samples=int(interval.get("samples") or 0),
                method=str(interval.get("method") or "validation_residual_quantiles"),
                supported=bool(interval.get("supported", False)),
            ),
            feature_stats=FeatureStats(
                medians={
                    str(key): float(value)
                    for key, value in (stats.get("medians") or {}).items()
                },
                scales={
                    str(key): float(value)
                    for key, value in (stats.get("scales") or {}).items()
                },
                minimums={
                    str(key): float(value)
                    for key, value in (stats.get("minimums") or {}).items()
                },
                maximums={
                    str(key): float(value)
                    for key, value in (stats.get("maximums") or {}).items()
                },
            ),
            feature_importance=tuple(
                dict(item) for item in (metadata.get("feature_importance") or [])
            ),
            artifact=artifact,
        )

    def delete(self, artifact: ModelArtifact) -> None:
        """Best-effort cleanup for a model whose registry write failed."""
        path = Path(artifact.regressor_path).resolve()
        if path.is_relative_to(self.root):
            directory = path.parent
            for child in directory.glob("*"):
                try:
                    child.unlink()
                except OSError:
                    continue
            try:
                directory.rmdir()
            except OSError:
                pass


def artifact_metadata(artifact: ModelArtifact) -> Mapping[str, Any]:
    path = Path(artifact.regressor_path).parent / "metadata.json"
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(body, Mapping):
        return {}
    return body


__all__ = ["GradienArtifactStore", "artifact_metadata"]
