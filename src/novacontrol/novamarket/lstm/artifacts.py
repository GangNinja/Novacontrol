"""Native PyTorch state-dict artifacts with checksums and atomic replacement.

The authoritative model row lives in ``PredictionStore``; this module only owns
the native weight file.  A failed save removes its temporary file and cannot
corrupt an earlier version because the final path is replaced atomically.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Any, Mapping

from .models import LSTMArtifact, LSTMConfig, LSTMUnavailableError, LSTMValidationError
from .network import torch_module


def _safe_segment(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in str(value)
    ).strip("._")
    if not cleaned:
        raise LSTMValidationError("artifact path segment is empty")
    return cleaned[:120]


class LSTMArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path_for(self, instrument_id: str, version: str) -> Path:
        return self.root / _safe_segment(instrument_id) / _safe_segment(version) / "model.pt"

    def save(
        self,
        *,
        instrument_id: str,
        version: str,
        network: Any,
        config: LSTMConfig,
        input_size: int,
    ) -> LSTMArtifact:
        path = self.path_for(instrument_id, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        torch = torch_module()
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            # Saving a detached CPU state dict keeps the artifact independent of
            # the training process and of any optional accelerator.
            state = {
                key: value.detach().cpu()
                for key, value in network.state_dict().items()
            }
            torch.save(state, temporary)
            digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
            size = temporary.stat().st_size
            os.replace(temporary, path)
        except Exception as exc:  # noqa: BLE001 - cleanup then report one failure
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise LSTMUnavailableError(
                f"LSTM artifact save failed: {type(exc).__name__}: {exc}"
            ) from exc
        return LSTMArtifact(
            version=version,
            state_path=str(path),
            state_sha256=digest,
            state_bytes=size,
            architecture={
                "input_size": int(input_size),
                "hidden_size": int(config.hidden_size),
                "num_layers": int(config.num_layers),
                "dropout": float(config.dropout),
                "outputs": ["return", "direction_3_class"],
            },
        )

    def load(
        self,
        artifact: LSTMArtifact,
        *,
        config: LSTMConfig,
        input_size: int,
        network: Any | None = None,
    ) -> Any:
        path = Path(artifact.state_path)
        if not path.is_file():
            raise LSTMUnavailableError(f"LSTM model artifact is missing: {path}")
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise LSTMUnavailableError(
                f"LSTM model artifact could not be read: {type(exc).__name__}: {exc}"
            ) from exc
        digest = hashlib.sha256(payload).hexdigest()
        if digest != artifact.state_sha256:
            raise LSTMUnavailableError(
                "LSTM model artifact checksum mismatch; refusing to load it"
            )
        if artifact.state_bytes != len(payload):
            raise LSTMUnavailableError(
                "LSTM model artifact size mismatch; refusing to load it"
            )
        torch = torch_module()
        try:
            try:
                state = torch.load(path, map_location="cpu", weights_only=True)
            except TypeError:  # pragma: no cover - older torch signature
                state = torch.load(path, map_location="cpu")
        except Exception as exc:  # noqa: BLE001
            raise LSTMUnavailableError(
                f"LSTM model artifact could not be loaded: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(state, Mapping):
            raise LSTMUnavailableError("LSTM state dict is not a mapping")
        if network is None:
            from .network import build_network

            network = build_network(config, input_size)
        try:
            network.load_state_dict(state)
            network.eval()
        except Exception as exc:  # noqa: BLE001
            raise LSTMUnavailableError(
                f"LSTM state dict is incompatible with its configuration: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return network


__all__ = ["LSTMArtifactStore"]
