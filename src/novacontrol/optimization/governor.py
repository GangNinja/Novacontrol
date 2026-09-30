"""Resource governance: the machine's room, read before anything spends it.

Phase 14.4's examples are the whole design, restated as rules:

    RAM available: 2.5 GB            -> TIGHT; do not load another large model,
                                        prefer a lightweight one, unload what is
                                        inactive.
    GPU memory pressure high         -> TIGHT; do not add a vision model to a
                                        crowded adapter.

The governor answers those two questions — *how much room is there* and *may
this load proceed* — from ACTUAL runtime telemetry, and it says ``None``
("could not measure") where the machine would not answer. It never decides
which model to use: selection is the manager's job, done by capability.
``advise_load`` only ever says yes, no, or unknown, WITH the readings that
produced the answer, so a refusal in the status surface can be traced to a
figure rather than to a hunch.

Thresholds are configuration, not constants of nature: a 16 GB laptop with an
integrated GPU and an 8 GB Arc card is the machine this was written for, and
the defaults encode its comfort lines (4 GB free is comfortable, 1.5 GB is
critical, 85 °C is hot, 20 % battery on the road is low). Every one of them is
overridable, because the second machine this runs on will not be this one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from novacontrol.models.hardware import HardwareMonitor, gpu_memory_bytes
from novacontrol.optimization.models import (
    ResourceAdvice,
    ResourceAssessment,
    ResourceLevel,
)

#: Megabyte, stated once so every threshold below is readable.
MB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class GovernorThresholds:
    """Where "ample" stops, per dimension. All overridable by configuration."""

    tight_free_ram_mb: int = 4096
    critical_free_ram_mb: int = 1536
    max_cpu_percent: float = 90.0
    max_gpu_utilization: float = 95.0
    max_temperature_c: float = 85.0
    min_battery_percent: float = 20.0

    @property
    def tight_free_ram_bytes(self) -> int:
        return max(0, int(self.tight_free_ram_mb)) * MB

    @property
    def critical_free_ram_bytes(self) -> int:
        return max(0, int(self.critical_free_ram_mb)) * MB

    def to_dict(self) -> dict[str, Any]:
        return {
            "tight_free_ram_mb": self.tight_free_ram_mb,
            "critical_free_ram_mb": self.critical_free_ram_mb,
            "max_cpu_percent": self.max_cpu_percent,
            "max_gpu_utilization": self.max_gpu_utilization,
            "max_temperature_c": self.max_temperature_c,
            "min_battery_percent": self.min_battery_percent,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> GovernorThresholds:
        """Read configured thresholds; an unusable value keeps its default."""
        data = data or {}
        defaults = cls()

        def number(key: str, current: float) -> float:
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            return current

        def count(key: str, current: int) -> int:
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
            return current

        tight = count("tight_free_ram_mb", defaults.tight_free_ram_mb)
        critical = count("critical_free_ram_mb", defaults.critical_free_ram_mb)
        if critical > tight:
            # A critical line above the tight line would make "tight" unreachable.
            critical = tight
        return cls(
            tight_free_ram_mb=tight,
            critical_free_ram_mb=critical,
            max_cpu_percent=number("max_cpu_percent", defaults.max_cpu_percent),
            max_gpu_utilization=number("max_gpu_utilization", defaults.max_gpu_utilization),
            max_temperature_c=number("max_temperature_c", defaults.max_temperature_c),
            min_battery_percent=number("min_battery_percent", defaults.min_battery_percent),
        )


class ResourceGovernor:
    """Assess the machine, and advise a load decision.

    The monitor is injected (so a fake machine drives every branch in tests) and
    the thresholds are configuration. This class performs no I/O of its own
    beyond the probes the monitor already owns, and holds no locks: the
    application asks it on the load path, where the model layer's own locks
    already serialize the decision that matters.
    """

    def __init__(
        self,
        monitor: HardwareMonitor | None = None,
        *,
        thresholds: GovernorThresholds | None = None,
    ) -> None:
        self.monitor = monitor if monitor is not None else HardwareMonitor()
        self.thresholds = thresholds if thresholds is not None else GovernorThresholds()
        self._last: ResourceAssessment | None = None

    # -- assessment ------------------------------------------------------------
    def assess(self) -> ResourceAssessment:
        """Read the machine and class it AMPLE, TIGHT or CRITICAL.

        The level is the WORST any considered dimension reached, and every
        escalation carries the figure that produced it. Unmeasured dimensions do
        not escalate anything — an absent sensor is not pressure — but they do
        add a reason, so a report never silently omits what it could not read.
        """
        monitor = self.monitor
        thresholds = self.thresholds
        available = monitor.available_ram_bytes()
        total = monitor.total_ram_bytes()
        cpu = monitor.cpu_percent()
        gpu = dict(monitor.gpu())
        gpu_utilization = (
            float(gpu["percent"])
            if gpu.get("available") and isinstance(gpu.get("percent"), (int, float))
            else None
        )
        memory = gpu_memory_bytes(gpu)
        gpu_used = memory[0] if memory else None
        gpu_total = memory[1] if memory else None
        npu_available = bool(monitor.npu().get("available"))
        temperature = monitor.temperature_celsius()
        battery = dict(monitor.battery())
        battery_percent: float | None = None
        on_battery: bool | None = None
        if battery.get("available"):
            percent = battery.get("percent")
            if isinstance(percent, (int, float)):
                battery_percent = float(percent)
            on_ac = battery.get("on_ac")
            if isinstance(on_ac, bool):
                on_battery = not on_ac

        reasons: list[str] = []
        level = ResourceLevel.AMPLE

        def escalate(candidate: ResourceLevel) -> None:
            nonlocal level
            if list(ResourceLevel).index(candidate) > list(ResourceLevel).index(level):
                level = candidate

        def gb(value: int) -> str:
            return f"{value / (1024 ** 3):.1f} GB"

        if available is not None:
            if available < thresholds.critical_free_ram_bytes:
                escalate(ResourceLevel.CRITICAL)
                reasons.append(
                    f"free RAM is {gb(available)}, below the "
                    f"{gb(thresholds.critical_free_ram_bytes)} critical line"
                )
            elif available < thresholds.tight_free_ram_bytes:
                escalate(ResourceLevel.TIGHT)
                reasons.append(
                    f"free RAM is {gb(available)}, below the "
                    f"{gb(thresholds.tight_free_ram_bytes)} comfort line"
                )
        else:
            reasons.append("free memory could not be measured")

        if cpu is not None and cpu > thresholds.max_cpu_percent:
            escalate(ResourceLevel.TIGHT)
            reasons.append(
                f"CPU utilization is {cpu:.0f}%, above the "
                f"{thresholds.max_cpu_percent:.0f}% line"
            )
        if gpu_utilization is not None and gpu_utilization > thresholds.max_gpu_utilization:
            escalate(ResourceLevel.TIGHT)
            reasons.append(
                f"GPU utilization is {gpu_utilization:.0f}%, above the "
                f"{thresholds.max_gpu_utilization:.0f}% line"
            )
        if gpu_used is not None and gpu_total:
            ratio = gpu_used / gpu_total
            if ratio >= 0.9:
                escalate(ResourceLevel.TIGHT)
                reasons.append(
                    f"GPU memory is {ratio * 100:.0f}% used "
                    f"({gb(gpu_used)} of {gb(gpu_total)})"
                )
        if temperature is not None and temperature > thresholds.max_temperature_c:
            escalate(
                ResourceLevel.CRITICAL
                if temperature >= thresholds.max_temperature_c + 10.0
                else ResourceLevel.TIGHT
            )
            reasons.append(
                f"temperature is {temperature:.0f} °C, above the "
                f"{thresholds.max_temperature_c:.0f} °C line"
            )
        if (
            on_battery
            and battery_percent is not None
            and battery_percent < thresholds.min_battery_percent
        ):
            escalate(ResourceLevel.TIGHT)
            reasons.append(
                f"on battery at {battery_percent:.0f}%, below the "
                f"{thresholds.min_battery_percent:.0f}% line"
            )

        if not reasons:
            reasons.append("resources are ample")

        assessment = ResourceAssessment(
            level=level,
            reasons=tuple(reasons),
            available_ram_bytes=available,
            total_ram_bytes=total,
            cpu_percent=float(cpu) if cpu is not None else None,
            gpu_utilization=gpu_utilization,
            gpu_memory_used_bytes=gpu_used,
            gpu_memory_total_bytes=gpu_total,
            npu_available=npu_available,
            temperature_c=temperature,
            battery_percent=battery_percent,
            on_battery=on_battery,
            loaded_models=monitor.resident_models(),
            headroom_bytes=monitor.headroom_bytes,
        )
        self._last = assessment
        return assessment

    # -- advice -----------------------------------------------------------------
    def advise_load(
        self,
        needed_bytes: int | None,
        *,
        model: str = "",
        loaded: Sequence[str] | None = None,
        active: Sequence[str] = (),
    ) -> ResourceAdvice:
        """Whether a load of ``needed_bytes`` may proceed, and what to unload.

        Three answers, and the middle one is load-bearing. ``None`` means the
        figures to decide were not available; a caller that treats it as a yes
        is choosing to, and this method will not make that choice for it. A
        CRITICAL machine refuses even an unmeasured load — the pressure is
        already visible and a large model is exactly what must not arrive into
        it — while a TIGHT machine still allows a load that fits, because
        "busy" is not "full".

        ``unload`` lists the inactive models worth releasing, most recently
        loaded first: the manager's own arithmetic decides what is sufficient,
        and this list only says what is ELIGIBLE.
        """
        assessment = self.assess()
        names = tuple(loaded) if loaded is not None else assessment.loaded_models
        keep = {str(name).casefold() for name in active}
        wanted = str(model or "").casefold()
        unload = tuple(
            name
            for name in reversed(names)
            if name.casefold() not in keep and name.casefold() != wanted
        )
        fits = assessment.allows(needed_bytes)
        prefer_light = assessment.level is not ResourceLevel.AMPLE

        if assessment.level is ResourceLevel.CRITICAL:
            return ResourceAdvice(
                allow=False,
                level=assessment.level,
                reason=(
                    "the machine is under critical resource pressure: "
                    + "; ".join(assessment.reasons)
                ),
                needed_bytes=needed_bytes,
                unload=unload,
                prefer_lightweight=True,
            )
        if fits is False:
            needed_text = (
                f"{needed_bytes / (1024 ** 3):.1f} GB"
                if needed_bytes is not None
                else "an unmeasured amount"
            )
            free_text = (
                f"{assessment.available_ram_bytes / (1024 ** 3):.1f} GB"
                if assessment.available_ram_bytes is not None
                else "an unknown amount"
            )
            return ResourceAdvice(
                allow=False,
                level=assessment.level,
                reason=(
                    f"the load needs {needed_text} and only {free_text} of memory is free"
                    + (f"; releasing {', '.join(unload)} would help" if unload else "")
                ),
                needed_bytes=needed_bytes,
                unload=unload,
                prefer_lightweight=prefer_light,
            )
        if fits is None:
            return ResourceAdvice(
                allow=None,
                level=assessment.level,
                reason=(
                    "the fit of this load could not be measured, so the caller "
                    "must decide with the figures it has"
                ),
                needed_bytes=needed_bytes,
                unload=unload,
                prefer_lightweight=prefer_light,
            )
        return ResourceAdvice(
            allow=True,
            level=assessment.level,
            reason="; ".join(assessment.reasons),
            needed_bytes=needed_bytes,
            unload=unload,
            prefer_lightweight=prefer_light,
        )

    # -- reporting ---------------------------------------------------------------
    @property
    def last_assessment(self) -> ResourceAssessment | None:
        """The most recent reading, or ``None`` before the first one."""
        return self._last

    def report(self) -> dict[str, Any]:
        assessment = self._last if self._last is not None else self.assess()
        return {
            "assessment": assessment.to_dict(),
            "thresholds": self.thresholds.to_dict(),
            "hardware": self.monitor.headroom_report(),
        }

    def to_dict(self) -> dict[str, Any]:
        return self.report()


__all__ = [
    "MB",
    "GovernorThresholds",
    "ResourceGovernor",
]
