"""Hardware detection and backend policy, without assuming CUDA exists.

The development machine this project targets has Intel integrated graphics and
an Intel NPU, no discrete NVIDIA GPU. So nothing here says "GPU" and means
"CUDA": a device is reported only when something actually reported it, and the
absence of a GPU is a normal answer rather than an error.

Two detection levels, deliberately separated:

  * **cheap detection** (the default) uses ``importlib.util.find_spec`` only —
    it answers "is torch installed?" without importing torch, which keeps a
    status endpoint fast and keeps the test suite free of a multi-second import.
  * **runtime probing** (``probe_runtime=True``) actually imports torch and asks
    it about CUDA/MPS/XPU. That is an explicit operator action, never a side
    effect of running NovaControl.

The training dependencies (torch, transformers, peft, accelerate) are OPTIONAL.
NovaControl runs normally without any of them; only a real (non-dry-run)
training run needs them, and it says exactly what is missing when they are.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from novacontrol.training.models import HardwarePolicy

#: The optional packages a real training backend needs, in the order a message
#: should name them. ``bitsandbytes`` is only needed for QLoRA.
TRAINING_DEPENDENCIES: tuple[str, ...] = ("torch", "transformers", "peft")
RECOMMENDED_DEPENDENCIES: tuple[str, ...] = ("accelerate",)
QUANTISATION_DEPENDENCIES: tuple[str, ...] = ("bitsandbytes",)


def _installed(package: str) -> bool:
    """Whether a package is importable, WITHOUT importing it."""
    try:
        return importlib.util.find_spec(package) is not None
    except (ImportError, ValueError):
        return False


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _whole(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


@dataclass(frozen=True, slots=True)
class HardwareCapabilities:
    """What this machine can do, and how that was found out.

    ``backends`` lists the DEVICES training could use (always including
    ``cpu``); the dependency booleans say whether a PEFT training stack could
    run at all. A machine with no torch still reports ``cpu`` — because dry-run
    and dataset validation do not need anything installed.
    """

    cpu_count: int = 0
    total_ram_bytes: int | None = None
    available_ram_bytes: int | None = None
    gpu_available: bool = False
    gpu_name: str = ""
    gpu_memory_total_bytes: int | None = None
    npu_available: bool = False
    npu_name: str = ""
    torch_available: bool = False
    transformers_available: bool = False
    peft_available: bool = False
    accelerate_available: bool = False
    bitsandbytes_available: bool = False
    backends: tuple[str, ...] = ("cpu",)
    notes: tuple[str, ...] = ()
    probed_runtime: bool = False

    @property
    def training_dependencies_ready(self) -> bool:
        """Whether a real PEFT/LoRA run could start at all."""
        return self.torch_available and self.transformers_available and self.peft_available

    def missing_dependencies(self, *, quantised: bool = False) -> tuple[str, ...]:
        """Which optional packages are absent, read from this reading's own flags.

        The flags are READ rather than re-probed. ``detect_hardware`` is the one
        place that asks the machine, and ``training_dependencies_ready`` already
        reads these same booleans — so re-probing here could make one reading
        say "the training stack is missing" and its own missing-list say nothing
        is missing. A captured or injected capability set is a reading too, and
        it has to be answerable.
        """
        missing = [
            name for name in TRAINING_DEPENDENCIES if not self.dependency_flag(name)
        ]
        if quantised:
            missing.extend(
                name
                for name in QUANTISATION_DEPENDENCIES
                if not self.dependency_flag(name)
            )
        return tuple(missing)

    def dependency_flag(self, package: str) -> bool:
        """Whether this reading says ``package`` is importable.

        A name this class carries no flag for is probed, so a dependency added
        later is neither silently reported present nor silently missing.
        """
        flags = {
            "torch": self.torch_available,
            "transformers": self.transformers_available,
            "peft": self.peft_available,
            "accelerate": self.accelerate_available,
            "bitsandbytes": self.bitsandbytes_available,
        }
        return flags[package] if package in flags else _installed(package)

    def device_available(self, device: str) -> bool:
        return device in self.backends

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu_count": self.cpu_count,
            "total_ram_bytes": self.total_ram_bytes,
            "available_ram_bytes": self.available_ram_bytes,
            "gpu_available": self.gpu_available,
            "gpu_name": self.gpu_name,
            "gpu_memory_total_bytes": self.gpu_memory_total_bytes,
            "npu_available": self.npu_available,
            "npu_name": self.npu_name,
            "torch_available": self.torch_available,
            "transformers_available": self.transformers_available,
            "peft_available": self.peft_available,
            "accelerate_available": self.accelerate_available,
            "bitsandbytes_available": self.bitsandbytes_available,
            "backends": list(self.backends),
            "notes": list(self.notes),
            "probed_runtime": self.probed_runtime,
            "training_dependencies_ready": self.training_dependencies_ready,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HardwareCapabilities:
        backends = data.get("backends")
        notes = data.get("notes")
        return cls(
            cpu_count=max(0, _whole(data.get("cpu_count")) or 0),
            total_ram_bytes=_whole(data.get("total_ram_bytes")),
            available_ram_bytes=_whole(data.get("available_ram_bytes")),
            gpu_available=bool(data.get("gpu_available", False)),
            gpu_name=_text(data.get("gpu_name")),
            gpu_memory_total_bytes=_whole(data.get("gpu_memory_total_bytes")),
            npu_available=bool(data.get("npu_available", False)),
            npu_name=_text(data.get("npu_name")),
            torch_available=bool(data.get("torch_available", False)),
            transformers_available=bool(data.get("transformers_available", False)),
            peft_available=bool(data.get("peft_available", False)),
            accelerate_available=bool(data.get("accelerate_available", False)),
            bitsandbytes_available=bool(data.get("bitsandbytes_available", False)),
            backends=tuple(str(item) for item in backends)
            if isinstance(backends, (list, tuple)) and backends
            else ("cpu",),
            notes=tuple(str(item) for item in notes)
            if isinstance(notes, (list, tuple))
            else (),
            probed_runtime=bool(data.get("probed_runtime", False)),
        )


def _memory_from_psutil() -> tuple[int | None, int | None]:
    """(total, available) from psutil when it is importable, else (None, None)."""
    if not _installed("psutil"):
        return (None, None)
    try:
        import psutil  # noqa: PLC0415 - optional dependency, probed at runtime

        memory = psutil.virtual_memory()
        return (int(memory.total), int(memory.available))
    except Exception:  # noqa: BLE001 - a probe must never break a report
        return (None, None)


def _probe_torch_runtime() -> tuple[bool, str, int | None, bool, str, tuple[str, ...]]:
    """Ask torch what it has. Only called when runtime probing was requested.

    Returns (gpu_available, gpu_name, gpu_memory_total, npu_available, npu_name,
    notes). Every failure is a note, never an exception.
    """
    notes: list[str] = []
    gpu_available = False
    gpu_name = ""
    gpu_memory: int | None = None
    npu_available = False
    npu_name = ""
    try:
        import torch  # noqa: PLC0415 - optional dependency, probed at runtime
    except Exception as exc:  # noqa: BLE001 - absence is a normal answer
        return (False, "", None, False, "", (f"torch could not be imported: {type(exc).__name__}",))
    try:
        if torch.cuda.is_available():
            gpu_available = True
            gpu_name = str(torch.cuda.get_device_name(0))
            properties = torch.cuda.get_device_properties(0)
            gpu_memory = int(getattr(properties, "total_memory", 0)) or None
            notes.append(f"torch reports CUDA device {gpu_name!r}")
        else:
            notes.append("torch reports no CUDA device (expected on this hardware)")
    except Exception as exc:  # noqa: BLE001 - a probe must never break a report
        notes.append(f"CUDA probe failed: {type(exc).__name__}")
    try:
        xpu = getattr(torch, "xpu", None)
        if xpu is not None and xpu.is_available():
            npu_available = True
            npu_name = str(xpu.get_device_name(0)) if hasattr(xpu, "get_device_name") else "xpu"
            notes.append(f"torch reports XPU device {npu_name!r}")
    except Exception as exc:  # noqa: BLE001 - a probe must never break a report
        notes.append(f"XPU probe failed: {type(exc).__name__}")
    try:
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            gpu_available = True
            gpu_name = gpu_name or "Apple MPS"
            notes.append("torch reports an MPS device")
    except Exception as exc:  # noqa: BLE001 - a probe must never break a report
        notes.append(f"MPS probe failed: {type(exc).__name__}")
    return (gpu_available, gpu_name, gpu_memory, npu_available, npu_name, tuple(notes))


def detect_hardware(
    *,
    monitor: Any = None,
    probe_runtime: bool = False,
    extra_memory: tuple[int | None, int | None] | None = None,
) -> HardwareCapabilities:
    """Read this machine's abilities.

    ``monitor`` is anything with ``total_ram_bytes()``/``available_ram_bytes()``
    (the application's ``HardwareMonitor``); when it is absent, psutil is used
    if importable, and an unmeasured memory simply stays ``None``.
    """
    notes: list[str] = []
    cpu_count = os.cpu_count() or 0
    total_ram: int | None = None
    available_ram: int | None = None
    if monitor is not None:
        try:
            total_ram = _whole(monitor.total_ram_bytes())
            available_ram = _whole(monitor.available_ram_bytes())
            notes.append("memory read from the application's hardware monitor")
        except Exception:  # noqa: BLE001 - a probe must never break a report
            notes.append("the hardware monitor could not report memory")
    if extra_memory is not None:
        if total_ram is None:
            total_ram = extra_memory[0]
        if available_ram is None:
            available_ram = extra_memory[1]
    if total_ram is None and available_ram is None:
        psutil_total, psutil_available = _memory_from_psutil()
        total_ram = psutil_total
        available_ram = psutil_available
        if psutil_total is not None:
            notes.append("memory read from psutil")
        else:
            notes.append("free memory could not be measured on this machine")

    torch_available = _installed("torch")
    transformers_available = _installed("transformers")
    peft_available = _installed("peft")
    accelerate_available = _installed("accelerate")
    bitsandbytes_available = _installed("bitsandbytes")
    openvino_available = _installed("openvino")

    gpu_available = False
    gpu_name = ""
    gpu_memory: int | None = None
    npu_available = openvino_available
    npu_name = "openvino" if openvino_available else ""
    probed = False
    if probe_runtime:
        probed = True
        (
            gpu_available,
            gpu_name,
            gpu_memory,
            torch_npu,
            torch_npu_name,
            torch_notes,
        ) = _probe_torch_runtime()
        notes.extend(torch_notes)
        if torch_npu:
            npu_available = True
            npu_name = torch_npu_name or npu_name
    if openvino_available and not probed:
        notes.append("openvino is installed: an Intel NPU may be available")
    if not torch_available:
        notes.append(
            "torch is not installed: real training is unavailable, dry-run and "
            "dataset validation still work"
        )

    backends = ["cpu"]
    if gpu_available:
        backends.append("cuda")
    if npu_available:
        backends.append("npu")
    return HardwareCapabilities(
        cpu_count=cpu_count,
        total_ram_bytes=total_ram,
        available_ram_bytes=available_ram,
        gpu_available=gpu_available,
        gpu_name=gpu_name,
        gpu_memory_total_bytes=gpu_memory,
        npu_available=npu_available,
        npu_name=npu_name,
        torch_available=torch_available,
        transformers_available=transformers_available,
        peft_available=peft_available,
        accelerate_available=accelerate_available,
        bitsandbytes_available=bitsandbytes_available,
        backends=tuple(backends),
        notes=tuple(notes),
        probed_runtime=probed,
    )


@dataclass(frozen=True, slots=True)
class BackendChoice:
    """Which device a policy resolves to, and why."""

    device: str = "cpu"
    policy: str = HardwarePolicy.AUTO.value
    available: bool = True
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "policy": self.policy,
            "available": self.available,
            "reason": self.reason,
        }


def resolve_backend(policy: str, capabilities: HardwareCapabilities) -> BackendChoice:
    """Resolve a hardware policy against what this machine actually has.

    ``AUTO`` prefers a real accelerator and falls back to the CPU — the one
    device that always exists. An explicit policy that names a device this
    machine does not have says so instead of silently downgrading.
    """
    chosen = policy if policy in {member.value for member in HardwarePolicy} else (
        HardwarePolicy.AUTO.value
    )
    if chosen == HardwarePolicy.LOCAL_GPU.value:
        if capabilities.gpu_available:
            return BackendChoice("cuda", chosen, True, "a CUDA device was reported")
        return BackendChoice(
            "", chosen, False, "no CUDA device was reported: real training cannot use one"
        )
    if chosen == HardwarePolicy.LOCAL_NPU.value:
        if capabilities.npu_available:
            return BackendChoice(
                "npu", chosen, True, f"an NPU was reported ({capabilities.npu_name or 'unknown'})"
            )
        return BackendChoice("", chosen, False, "no NPU was reported on this machine")
    if chosen == HardwarePolicy.LOCAL_CPU.value:
        return BackendChoice("cpu", chosen, True, "the CPU was requested explicitly")
    if capabilities.gpu_available:
        return BackendChoice("cuda", chosen, True, "auto: a CUDA device is available")
    if capabilities.npu_available:
        return BackendChoice(
            "npu", chosen, True, f"auto: an NPU is available ({capabilities.npu_name or 'unknown'})"
        )
    return BackendChoice("cpu", chosen, True, "auto: no accelerator was reported, using the CPU")


__all__ = [
    "QUANTISATION_DEPENDENCIES",
    "RECOMMENDED_DEPENDENCIES",
    "TRAINING_DEPENDENCIES",
    "BackendChoice",
    "HardwareCapabilities",
    "detect_hardware",
    "resolve_backend",
]
