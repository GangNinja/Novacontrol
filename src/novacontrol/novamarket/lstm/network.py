"""Small CPU-first PyTorch LSTM network, imported only when LSTM is used.

Torch is an optional PI-07 dependency.  Keeping the import inside the factory
means the rest of NovaMarket still boots and forecasts when PyTorch is absent;
the sequence service reports that capability state explicitly.
"""

from __future__ import annotations

import importlib
from typing import Any

from .models import LSTMConfig, LSTMUnavailableError, LSTMValidationError


def torch_module() -> Any:
    try:
        return importlib.import_module("torch")
    except Exception as exc:  # noqa: BLE001 - absence is a capability state
        raise LSTMUnavailableError(
            "PyTorch is not installed; install the optional lstm extra to use "
            "sequence models"
        ) from exc


def backend_available() -> bool:
    return bool(backend_version())


def backend_version() -> str:
    try:
        module = importlib.import_module("torch")
    except Exception:  # noqa: BLE001
        return ""
    return str(getattr(module, "__version__", "") or "unknown")


def hardware_status() -> dict[str, Any]:
    """CPU-only by default; GPU is reported, never assumed or auto-enabled."""
    try:
        torch = torch_module()
    except LSTMUnavailableError:
        return {
            "available": False,
            "version": "",
            "device": "cpu",
            "cuda": False,
            "mps": False,
            "threads": 0,
        }
    cuda = bool(getattr(torch, "cuda", None) and torch.cuda.is_available())
    mps = bool(
        getattr(torch, "backends", None)
        and getattr(torch.backends, "mps", None)
        and torch.backends.mps.is_available()
    )
    return {
        "available": True,
        "version": backend_version(),
        "device": "cpu",
        "cuda": cuda,
        "mps": mps,
        "threads": int(torch.get_num_threads()),
        "note": (
            "CPU is the supported default; optional accelerators are not "
            "auto-selected and were not required for PI-07 evidence"
        ),
    }


def build_network(config: LSTMConfig, input_size: int) -> Any:
    if input_size <= 0:
        raise LSTMValidationError("the LSTM input size must be positive")
    torch = torch_module()
    nn = torch.nn

    class _LSTMNetwork(nn.Module):  # type: ignore[name-defined, misc]
        def __init__(self) -> None:
            super().__init__()
            dropout = float(config.dropout) if config.num_layers > 1 else 0.0
            # Two channels per feature: the scaled value and its observed
            # mask.  Keeping the mask as an input (rather than multiplying
            # missing values to zero) preserves the distinction between an
            # observed zero and a value that was absent and imputed.
            self.encoder = nn.LSTM(
                input_size=int(input_size) * 2,
                hidden_size=int(config.hidden_size),
                num_layers=int(config.num_layers),
                batch_first=True,
                dropout=dropout,
            )
            self.norm = nn.LayerNorm(int(config.hidden_size))
            self.regression = nn.Linear(int(config.hidden_size), 1)
            self.classification = nn.Linear(int(config.hidden_size), 3)

        def forward(self, values: Any, mask: Any) -> tuple[Any, Any]:
            clean = torch.cat((values, mask), dim=-1)
            _output, (hidden, _cell) = self.encoder(clean)
            last = self.norm(hidden[-1])
            return self.regression(last).squeeze(-1), self.classification(last)

    return _LSTMNetwork()


__all__ = [
    "backend_available",
    "backend_version",
    "build_network",
    "hardware_status",
    "torch_module",
]
