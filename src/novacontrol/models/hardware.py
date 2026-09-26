"""What this machine actually has — measured, or reported as unmeasured.

Phase 7 asks for hardware awareness, and the operative word is *awareness*.
Every figure here is either read off the machine or reported as unavailable with
the reason; nothing is assumed from a product name, and no accelerator is
claimed that has not been detected. That rule is not decoration: a build that
"supports the NPU" on paper and silently runs on the CPU fabricates a capability
the user paid for, and one that reports the GPU as absent when only its counter
is unreachable sends them looking for a driver problem that does not exist.

The measurement itself is not re-implemented here. ``telemetry/hardware.py``
already has the platform probes (Windows perf counters, psutil, powershell, the
sysfs/proc fallbacks) and its own caching discipline, because a GPU probe spawns
a process and must never run inside a request. ``HardwareMonitor`` is the model
layer's view of that: RAM and CPU as the loader needs them, GPU utilization and
GPU memory as they were last sampled, an NPU probe, the models currently
resident, and the estimate a load decision is made from.

**One honest gap, stated rather than papered over.** The dependency in this
project is ``system RAM``: the local runtime loads weights into it and the
discrete/integrated GPU question does not change that. So ``fits()`` decides on
available RAM, and the GPU figures are reported for the operator and for later
work (offloading, VRAM-aware placement) rather than being invented as a second
constraint the runtime does not honour.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from novacontrol.telemetry.hardware import HardwareTelemetry

#: Bytes per gigabyte, stated once so every figure in this module agrees.
GB = 1024 ** 3

#: The headroom a load must leave free. A model loaded to the last byte pages
#: from the moment it answers, on a machine that also has to run the desktop the
#: model is talking about. The value matches the one the lifecycle layer already
#: uses, so there is one number rather than two that can disagree.
DEFAULT_HEADROOM_BYTES = 512 * 1024 * 1024

#: What a parameter costs resident, as a fraction of a gigabyte, for an
#: estimate made when nothing measured the model. A 4-bit quantized build is
#: roughly 0.55 GB per billion parameters plus a working allowance; this is
#: deliberately on the generous side, because an estimate that under-states a
#: model's footprint is the one that causes paging.
BYTES_PER_PARAMETER = 0.6 * GB


@dataclass(frozen=True, slots=True)
class HardwareSnapshot:
    """The machine, as the model layer sees it, with provenance per figure."""

    total_ram_bytes: int | None
    available_ram_bytes: int | None
    cpu_percent: float | None
    gpu: Mapping[str, Any] = field(default_factory=dict)
    npu: Mapping[str, Any] = field(default_factory=dict)
    resident_models: tuple[str, ...] = ()
    sources: Mapping[str, str] = field(default_factory=dict)

    @property
    def gpu_available(self) -> bool:
        """Whether a GPU was actually detected — not whether one is expected."""
        return bool(self.gpu.get("available"))

    @property
    def npu_available(self) -> bool:
        return bool(self.npu.get("available"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_ram_bytes": self.total_ram_bytes,
            "available_ram_bytes": self.available_ram_bytes,
            "total_ram_gb": _gb(self.total_ram_bytes),
            "available_ram_gb": _gb(self.available_ram_bytes),
            "cpu_percent": self.cpu_percent,
            "gpu": dict(self.gpu),
            "npu": dict(self.npu),
            "resident_models": list(self.resident_models),
            "sources": dict(self.sources),
        }


class HardwareMonitor:
    """RAM, CPU, GPU, NPU and the resident models, for load decisions.

    The telemetry object is injected, so tests drive every branch without a GPU,
    an NPU or a running runtime — and a deployment whose platform probes differ
    swaps the source, not this class.
    """

    def __init__(
        self,
        *,
        telemetry: HardwareTelemetry | None = None,
        resident_models: Callable[[], tuple[str, ...]] | None = None,
        headroom_bytes: int = DEFAULT_HEADROOM_BYTES,
        npu_probe: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self._telemetry = telemetry if telemetry is not None else HardwareTelemetry()
        # Injected so this module never imports a runtime: "which models are
        # resident" is the provider's question, and asking it here keeps the two
        # layers independent.
        self._resident = resident_models if resident_models is not None else (lambda: ())
        self.headroom_bytes = max(0, int(headroom_bytes))
        self._npu_probe = npu_probe

    # -- the measurements a load decision needs ------------------------------
    def total_ram_bytes(self) -> int | None:
        memory = self._telemetry.memory()
        total = memory.get("total_bytes")
        return int(total) if isinstance(total, (int, float)) and total else None

    def available_ram_bytes(self) -> int | None:
        """Free physical memory — ``None`` when the OS would not say.

        ``None`` is load-bearing: a caller must be able to tell "there is
        plenty" from "nobody measured", because only the first is safe to load a
        multi-gigabyte model on.
        """
        memory = self._telemetry.memory()
        available = memory.get("available_bytes")
        if isinstance(available, (int, float)):
            return int(available)
        return None

    def cpu_percent(self) -> float | None:
        cpu = self._telemetry.cpu()
        percent = cpu.get("percent")
        return float(percent) if isinstance(percent, (int, float)) else None

    def gpu(self) -> Mapping[str, Any]:
        """GPU utilization and memory, as the platform probe last saw them."""
        return dict(self._telemetry.gpu())

    def npu(self) -> Mapping[str, Any]:
        """Whether an NPU is present — and nothing beyond what was detected.

        There is no portable way to ask "does this machine have an NPU". The
        probe is therefore injectable, and the DEFAULT answer is unavailable with
        that reason: this build will not claim an accelerator it cannot see, and
        a platform that can see one supplies the probe.
        """
        if self._npu_probe is not None:
            try:
                probed = dict(self._npu_probe())
            except Exception as exc:  # pragma: no cover - a probe must never throw
                return {
                    "available": False,
                    "reason": f"the NPU probe failed: {type(exc).__name__}",
                    "source": "probe",
                }
            probed.setdefault("source", "probe")
            return probed
        return {
            "available": False,
            "reason": "no NPU probe is configured for this platform",
            "source": "none",
            "acceleration_claimed": False,
        }

    def resident_models(self) -> tuple[str, ...]:
        try:
            return tuple(self._resident())
        except Exception:  # pragma: no cover - a probe must never throw
            return ()

    def snapshot(self) -> HardwareSnapshot:
        return HardwareSnapshot(
            total_ram_bytes=self.total_ram_bytes(),
            available_ram_bytes=self.available_ram_bytes(),
            cpu_percent=self.cpu_percent(),
            gpu=self.gpu(),
            npu=self.npu(),
            resident_models=self.resident_models(),
            sources={
                "ram": "telemetry/hardware.py memory()",
                "cpu": "telemetry/hardware.py cpu()",
                "gpu": str(self.gpu().get("source", "") or "unavailable"),
                "npu": str(self.npu().get("source", "") or "none"),
            },
        )

    # -- estimating ----------------------------------------------------------
    def estimate_bytes(
        self, *, memory_bytes: int | None = None, parameters_b: float | None = None
    ) -> int | None:
        """What a model is expected to need resident.

        A measured figure wins; otherwise the parameter count is scaled, and the
        estimate says it is an estimate by being returned at all. Unknown
        parameters give ``None`` — a caller then decides on the evidence it has
        rather than on a number someone invented to fill the gap.
        """
        if memory_bytes:
            return int(memory_bytes)
        if parameters_b and parameters_b > 0:
            return int(parameters_b * BYTES_PER_PARAMETER)
        return None

    def fits(self, needed_bytes: int | None) -> bool | None:
        """Whether ``needed_bytes`` fits in the free memory, headroom included.

        Three answers, not two: ``True``, ``False``, and ``None`` for "could not
        measure". An unknown must never be read as a yes — that is the rule that
        keeps a load from thrashing a machine whose free memory nobody could
        read.
        """
        if needed_bytes is None:
            return None
        available = self.available_ram_bytes()
        if available is None:
            return None
        return available >= needed_bytes + self.headroom_bytes

    def headroom_report(self) -> dict[str, Any]:
        """The figures a load decision leaves in the log."""
        available = self.available_ram_bytes()
        return {
            "available_ram_bytes": available,
            "total_ram_bytes": self.total_ram_bytes(),
            "headroom_bytes": self.headroom_bytes,
            "resident_models": list(self.resident_models()),
            "gpu_available": bool(self.gpu().get("available")),
            "npu_available": bool(self.npu().get("available")),
        }


def gpu_memory_bytes(gpu: Mapping[str, Any]) -> tuple[int, int | None] | None:
    """``(used, total)`` from a GPU metric, or ``None`` when it does not report.

    Kept out of the monitor because it is a shape question, not a measurement:
    the platform probe's answer already distinguishes dedicated from shared
    memory, and total is legitimately ``None`` for an integrated adapter.
    """
    memory = gpu.get("memory")
    if not isinstance(memory, Mapping) or not memory.get("available"):
        return None
    used = memory.get("used_bytes")
    if not isinstance(used, (int, float)):
        return None
    total = memory.get("total_bytes")
    return int(used), int(total) if isinstance(total, (int, float)) and total else None


def _gb(value: int | None) -> float | None:
    return round(value / GB, 2) if isinstance(value, int) else None
