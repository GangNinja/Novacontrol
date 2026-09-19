"""Real hardware telemetry, or an honest "unavailable".

Every number this module returns comes from the operating system: psutil when it
happens to be installed, the platform's own facilities otherwise (Win32 APIs
through ctypes, /proc and /sys on Linux). Nothing is estimated, smoothed into
plausibility, or defaulted to a nice-looking value — a machine with no battery,
no thermal zone or no dedicated GPU memory reports exactly that, and the UI shows
"Unavailable" with the reason instead of a 0% that reads like a measurement.

Two cadences, because the costs differ by orders of magnitude:

* **cheap** metrics (CPU, RAM, disk, battery, uptime) are read in-process in
  microseconds — CPU from the two-clock delta between calls — so the endpoint can
  serve them live on every request;
* **expensive** metrics (GPU utilization/memory, network counters, temperature)
  need a WMI/perf-counter/subprocess round trip costing seconds on Windows, so
  they are sampled by a background thread on a slow cadence and the endpoint
  serves the cached sample. Serving a request must never spawn a process.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

# Percentages are real measurements, so they are clamped only to the range the
# unit allows — never massaged to look calmer.
_WINDOWS = sys.platform.startswith("win")
_PROBE_TIMEOUT_SECONDS = 30.0

# The GPU/network/temperature probe: one PowerShell process per sample (not one
# per metric), emitting a single compact JSON object. It is handed over as a
# base64 `-EncodedCommand` (see _run_windows_probe) so nothing has to be quoted
# for a shell and no script file is written to disk.
_WINDOWS_PROBE_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = [ordered]@{}
try {
  $samples = (Get-Counter '\GPU Engine(*)\Utilization Percentage' -MaxSamples 1).CounterSamples
  $out.gpu_util = [double]($samples | Measure-Object -Property CookedValue -Sum).Sum
} catch {}
try {
  $ded = (Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage' -MaxSamples 1).CounterSamples
  $out.gpu_dedicated_used = [double]($ded | Measure-Object -Property CookedValue -Sum).Sum
} catch {}
try {
  $shared = (Get-Counter '\GPU Adapter Memory(*)\Shared Usage' -MaxSamples 1).CounterSamples
  $out.gpu_shared_used = [double]($shared | Measure-Object -Property CookedValue -Sum).Sum
} catch {}
try {
  $out.gpu_name = [string](Get-CimInstance Win32_VideoController |
    Sort-Object -Property AdapterRAM -Descending | Select-Object -First 1 -ExpandProperty Name)
} catch {}
try {
  $adapters = Get-NetAdapterStatistics
  $out.net_received_bytes = [double]($adapters | Measure-Object -Property ReceivedBytes -Sum).Sum
  $out.net_sent_bytes = [double]($adapters | Measure-Object -Property SentBytes -Sum).Sum
  $out.net_interfaces = @($adapters | Where-Object { $_.ReceivedBytes -gt 0 -or $_.SentBytes -gt 0 } |
    ForEach-Object { $_.Name })
} catch {}
try {
  $zone = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature |
    Select-Object -First 1 -ExpandProperty CurrentTemperature
  if ($zone) { $out.temperature_celsius = [double](($zone / 10) - 273.15) }
} catch {}
$out | ConvertTo-Json -Compress
"""


def _metric_unavailable(reason: str, source: str = "") -> dict[str, Any]:
    """The shape every missing metric takes: no value, and a real explanation."""
    return {"available": False, "reason": reason, "source": source}


def _percent(used: float, total: float) -> float | None:
    if total <= 0:
        return None
    return round(max(0.0, min(100.0, used / total * 100.0)), 1)


def _as_text_list(value: object) -> list[str]:
    """A PowerShell array with one element serializes as a bare scalar.

    `@('Wi-Fi') | ConvertTo-Json` is the string `"Wi-Fi"`, not `["Wi-Fi"]`, so a
    one-adapter machine would hand the UI a string where it expects a list. The
    wire shape is normalized here rather than trusted.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


# ────────────────────────────────────────────────────────────────────────────
# Windows in-process reads (ctypes — microseconds, no subprocess)
# ────────────────────────────────────────────────────────────────────────────


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

    def value(self) -> int:
        return int((self.dwHighDateTime << 32) | self.dwLowDateTime)


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_uint32),
        ("BatteryFullLifeTime", ctypes.c_uint32),
    ]


# The Windows-only reads below are guarded with `sys.platform` (not the module
# _WINDOWS flag) because the type checker checks this file under BOTH platform
# views — CI verifies the Windows view from Linux with `mypy --platform win32`
# — and only a literal `sys.platform` comparison lets it narrow the branch and
# accept `ctypes.windll`/`winreg`, which typeshed declares Windows-only.


def _win_cpu_times() -> tuple[float, float, float] | None:
    """(idle, kernel, user) as 100ns FILETIME ticks, or None if unreadable."""
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.windll.kernel32
    idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
    ok = kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    )
    if not ok:
        return None
    return float(idle.value()), float(kernel.value()), float(user.value())


def _win_memory() -> dict[str, Any] | None:
    if sys.platform != "win32":
        return None
    status = _MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return {
        "total_bytes": int(status.ullTotalPhys),
        "available_bytes": int(status.ullAvailPhys),
        "used_bytes": int(status.ullTotalPhys - status.ullAvailPhys),
    }


def _win_battery() -> dict[str, Any] | None:
    if sys.platform != "win32":
        return None
    status = _SYSTEM_POWER_STATUS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return None
    # 128 = "no system battery", 255 = "unknown" in both fields.
    if status.BatteryFlag == 128:
        return None
    percent = None if status.BatteryLifePercent == 255 else int(status.BatteryLifePercent)
    on_ac = {0: False, 1: True}.get(status.ACLineStatus)
    charging = True if on_ac else (None if on_ac is None else False)
    return {"percent": percent, "on_ac": on_ac, "charging": charging}


def _win_uptime() -> float | None:
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.windll.kernel32
    kernel32.GetTickCount64.restype = ctypes.c_uint64
    return float(kernel32.GetTickCount64()) / 1000.0


# ────────────────────────────────────────────────────────────────────────────
# Linux/macOS reads
# ────────────────────────────────────────────────────────────────────────────


def _proc_cpu_times() -> tuple[float, float, float] | None:
    try:
        line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return None
    fields = [float(part) for part in line.split()[1:] if part.isdigit()]
    if len(fields) < 4:
        return None
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)  # idle + iowait
    total = sum(fields)
    return idle, total - idle, 0.0


def _proc_memory() -> dict[str, Any] | None:
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return None
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key.strip()] = int(parts[0]) * 1024
    total, available = values.get("MemTotal"), values.get("MemAvailable")
    if not total or available is None:
        return None
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": total - available,
    }


def _sysfs_battery() -> dict[str, Any] | None:
    for supply in sorted(Path("/sys/class/power_supply").glob("BAT*")) if Path(
        "/sys/class/power_supply"
    ).is_dir() else []:
        try:
            percent = int((supply / "capacity").read_text().strip())
        except (OSError, ValueError):
            continue
        try:
            state = (supply / "status").read_text().strip().lower()
        except OSError:
            state = ""
        return {
            "percent": percent,
            "on_ac": state == "charging" or state == "full",
            "charging": state == "charging",
        }
    return None


def _proc_uptime() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, IndexError, ValueError):
        return None


# ────────────────────────────────────────────────────────────────────────────
# Optional psutil fast path (the repo already probes for it; it is not required)
# ────────────────────────────────────────────────────────────────────────────


def _import_psutil() -> Any:
    try:
        import psutil  # noqa: PLC0415 — optional dependency, probed at runtime

        return psutil
    except ImportError:
        return None


def _cpu_model_name() -> str:
    """The marketing CPU name, not `platform.processor()`'s family string."""
    if sys.platform == "win32":  # literal form: the type checker narrows this
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            )
            with key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    else:
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except (OSError, IndexError):
            pass
    return platform.processor() or platform.machine() or "unknown"


# ────────────────────────────────────────────────────────────────────────────
# Collector
# ────────────────────────────────────────────────────────────────────────────


class HardwareTelemetry:
    """Reads this machine's real metrics. Cheap metrics are read per call.

    CPU is a *delta* between two reads of the OS's own tick counters, so the
    value reflects the window since the previous call (which, for a 2s UI poll,
    is a 2s average — real, and labelled as such by `window_seconds`).
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._psutil = _import_psutil()
        self._cpu_prev: tuple[float, tuple[float, float, float]] | None = None
        self._cpu_last: dict[str, Any] | None = None
        self._expensive: dict[str, Any] = {}
        self._expensive_at: float | None = None
        # Two consecutive counter reads plus the sample they each belong to. A
        # rate needs two *different* samples: comparing one sample with itself
        # would report a confident 0 B/s that was never measured.
        self._network_prev: tuple[int, float, int, int] | None = None
        self._network_current: tuple[int, float, int, int] | None = None
        self._network_sample = 0

    # ── cheap, live metrics ───────────────────────────────────────────────

    def cpu(self) -> dict[str, Any]:
        psutil = self._psutil
        if psutil is not None:
            try:
                percent = psutil.cpu_percent(interval=None)
            except Exception:  # pragma: no cover - platform specific
                percent = None
            if percent is not None:
                return {
                    "available": True,
                    "percent": round(float(percent), 1),
                    "source": "psutil.cpu_percent",
                }
        times = _win_cpu_times() if _WINDOWS else _proc_cpu_times()
        if times is None:
            return _metric_unavailable("this platform exposes no CPU tick counters")
        now = self._clock()
        with self._lock:
            previous = self._cpu_prev
            self._cpu_prev = (now, times)
        if previous is None:
            # One short warm-up read so the very first request returns a real
            # number instead of a placeholder. Costs 120ms once per process.
            time.sleep(0.12)
            second = _win_cpu_times() if _WINDOWS else _proc_cpu_times()
            if second is None:
                return _metric_unavailable("CPU counters became unreadable")
            with self._lock:
                self._cpu_prev = (self._clock(), second)
            previous, times = (now, times), second
        elapsed = max(1e-6, self._clock() - previous[0])
        percent = self._cpu_delta_percent(previous[1], times)
        if percent is None:
            # The OS tick counters only advance every ~15ms, so two reads inside
            # the same tick legitimately have no delta. One bounded re-read past
            # the tick settles it; only then does the previous real reading get
            # reused, explicitly marked stale — never a fresh-looking number.
            retry_started = self._clock()
            time.sleep(0.02)
            retry = _win_cpu_times() if _WINDOWS else _proc_cpu_times()
            if retry is not None:
                percent = self._cpu_delta_percent(times, retry)
                with self._lock:
                    self._cpu_prev = (retry_started, retry)
                if percent is not None:
                    # Report the window the surviving delta actually covers.
                    elapsed = max(1e-6, self._clock() - retry_started)
        if percent is None:
            if self._cpu_last is not None:
                stale = dict(self._cpu_last)
                stale["stale"] = True
                stale["reason"] = "the CPU counters did not advance within the sampling window"
                return stale
            return _metric_unavailable("CPU counters did not advance")
        reading = {
            "available": True,
            "percent": percent,
            "window_seconds": round(elapsed, 2),
            "source": "win32 GetSystemTimes" if _WINDOWS else "/proc/stat",
        }
        self._cpu_last = reading  # last real reading, for the no-delta fallback
        return dict(reading)

    @staticmethod
    def _cpu_delta_percent(
        previous: tuple[float, float, float], current: tuple[float, float, float]
    ) -> float | None:
        """Busy share between two tick-counter reads, or None if none elapsed.

        Windows' GetSystemTimes counts kernel time as kernel *including* idle and
        excludes idle from user, so its total is kernel + user. /proc/stat lists
        idle separately, so its (idle, busy, 0) triple needs idle added back in to
        describe the same total. Either way: busy = total - idle.
        """
        idle_delta = current[0] - previous[0]
        kernel_delta = current[1] - previous[1]
        user_delta = current[2] - previous[2]
        total_delta = kernel_delta + user_delta + (0.0 if _WINDOWS else idle_delta)
        if total_delta <= 0:
            return None
        percent = 100.0 * (total_delta - idle_delta) / total_delta
        return round(max(0.0, min(100.0, percent)), 1)

    def memory(self) -> dict[str, Any]:
        psutil = self._psutil
        raw: dict[str, Any] | None = None
        source = ""
        if psutil is not None:
            try:
                vm = psutil.virtual_memory()
                raw = {
                    "total_bytes": int(vm.total),
                    "available_bytes": int(vm.available),
                    "used_bytes": int(vm.total - vm.available),
                }
                source = "psutil.virtual_memory"
            except Exception:  # pragma: no cover - platform specific
                raw = None
        if raw is None:
            raw = _win_memory() if _WINDOWS else _proc_memory()
            source = "win32 GlobalMemoryStatusEx" if _WINDOWS else "/proc/meminfo"
        if raw is None:
            return _metric_unavailable("memory counters are not readable on this platform")
        raw["percent"] = _percent(float(raw["used_bytes"]), float(raw["total_bytes"]))
        raw["available"] = raw["percent"] is not None
        raw["source"] = source
        if not raw["available"]:
            return _metric_unavailable("the OS reported a zero total memory size", source)
        return raw

    def storage(self) -> dict[str, Any]:
        root = f"{os.environ.get('SystemDrive', 'C:')}\\" if _WINDOWS else "/"
        try:
            usage = shutil.disk_usage(root)
        except OSError as error:
            reason = error.strerror or error.__class__.__name__
            return _metric_unavailable(f"disk usage for {root} is unreadable: {reason}")
        return {
            "available": True,
            "mount": root,
            "total_bytes": int(usage.total),
            "used_bytes": int(usage.used),
            "free_bytes": int(usage.free),
            "percent": _percent(float(usage.used), float(usage.total)),
            "source": "shutil.disk_usage",
        }

    def battery(self) -> dict[str, Any]:
        psutil = self._psutil
        if psutil is not None:
            try:
                battery = psutil.sensors_battery()
            except Exception:  # pragma: no cover - platform specific
                battery = None
            if battery is not None:
                return {
                    "available": True,
                    "percent": round(float(battery.percent), 1),
                    "on_ac": bool(battery.power_plugged),
                    "charging": bool(battery.power_plugged),
                    "source": "psutil.sensors_battery",
                }
            return _metric_unavailable("psutil reports no battery on this machine")
        raw = _win_battery() if _WINDOWS else _sysfs_battery()
        if raw is None:
            return _metric_unavailable(
                "no battery: this is a desktop machine, or the OS exposes no battery state"
            )
        if raw["percent"] is None:
            return _metric_unavailable("the OS reports a battery but no charge percentage")
        return {
            "available": True,
            "percent": float(raw["percent"]),
            "on_ac": raw["on_ac"],
            "charging": raw["charging"],
            "source": "win32 GetSystemPowerStatus" if _WINDOWS else "/sys/class/power_supply",
        }

    def uptime(self) -> dict[str, Any]:
        """Seconds since boot, as its own metric (a card reads this directly)."""
        seconds = None
        source = ""
        psutil = self._psutil
        if psutil is not None:
            try:
                seconds = max(0.0, time.time() - float(psutil.boot_time()))
                source = "psutil.boot_time"
            except Exception:  # pragma: no cover - platform specific
                seconds = None
        if seconds is None:
            seconds = _win_uptime() if _WINDOWS else _proc_uptime()
            source = "win32 GetTickCount64" if _WINDOWS else "/proc/uptime"
        if seconds is None:
            return _metric_unavailable("this platform exposes no boot clock", source)
        return {
            "available": True,
            "seconds": round(float(seconds), 1),
            "source": source,
        }

    def host(self) -> dict[str, Any]:
        uptime = self.uptime()
        return {
            "hostname": socket.gethostname(),
            "platform": platform.system(),
            "platform_release": platform.release(),
            "cpu_model": _cpu_model_name(),
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
            "uptime_seconds": uptime.get("seconds"),
            "uptime_source": uptime.get("source", ""),
            "psutil": self._psutil is not None,
        }

    # ── expensive, sampled metrics ────────────────────────────────────────

    def sample_expensive(self) -> dict[str, Any]:
        """Run the slow probe (GPU/network/temperature) and cache the result.

        Called from the background sampler thread only — never from a request
        handler, so serving telemetry never spawns a process.
        """
        if self._psutil is not None:
            payload = self._sample_via_psutil()
        elif _WINDOWS:
            payload = self._sample_via_powershell()
        else:
            payload = self._sample_posix()
        with self._lock:
            self._expensive = payload
            self._expensive_at = self._clock()
        self._record_network(payload)
        return payload

    def _sample_via_psutil(self) -> dict[str, Any]:
        psutil = self._psutil
        payload: dict[str, Any] = {}
        try:
            counters = psutil.net_io_counters()
            payload["net_received_bytes"] = int(counters.bytes_recv)
            payload["net_sent_bytes"] = int(counters.bytes_sent)
        except Exception:  # pragma: no cover - platform specific
            pass
        try:
            temperatures = psutil.sensors_temperatures() or {}
            for name in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
                entries = temperatures.get(name) or []
                if entries:
                    payload["temperature_celsius"] = round(
                        float(max(entry.current for entry in entries)), 1
                    )
                    payload["temperature_source"] = f"psutil.sensors_temperatures[{name}]"
                    break
        except Exception:  # pragma: no cover - platform specific
            pass
        payload.update(self._sample_gpu_external())
        if _WINDOWS:
            # psutil has no GPU support anywhere, so utilization/GPU memory still
            # come from the Windows perf counters — one process, GPU keys only.
            probe = self._run_windows_probe() or {}
            for key in ("gpu_util", "gpu_dedicated_used", "gpu_dedicated_total", "gpu_shared_used", "gpu_name"):
                if probe.get(key) is not None:
                    payload.setdefault(key, probe[key])
        return payload

    def _sample_via_powershell(self) -> dict[str, Any]:
        raw = self._run_windows_probe()
        if raw is None:
            return {}
        payload: dict[str, Any] = {}
        for key in (
            "gpu_util",
            "gpu_dedicated_used",
            "gpu_shared_used",
            "gpu_name",
            "net_received_bytes",
            "net_sent_bytes",
            "temperature_celsius",
        ):
            if key in raw and raw[key] is not None:
                payload[key] = raw[key]
        payload["net_interfaces"] = _as_text_list(raw.get("net_interfaces"))
        return payload

    def _sample_posix(self) -> dict[str, Any]:
        payload = self._sample_gpu_external()
        try:
            text = Path("/proc/net/dev").read_text(encoding="utf-8")
        except OSError:
            return payload
        received = sent = 0
        interfaces: list[str] = []
        for line in text.splitlines()[2:]:
            name, _, rest = line.partition(":")
            columns = rest.split()
            if len(columns) < 9 or name.strip() == "lo":
                continue
            received += int(columns[0])
            sent += int(columns[8])
            interfaces.append(name.strip())
        payload["net_received_bytes"] = received
        payload["net_sent_bytes"] = sent
        payload["net_interfaces"] = interfaces
        return payload

    def _sample_gpu_external(self) -> dict[str, Any]:
        """nvidia-smi, when an NVIDIA driver happens to be present."""
        binary = shutil.which("nvidia-smi")
        if not binary:
            return {}
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [
                    binary,
                    "--query-gpu=utilization.gpu,memory.used,memory.total,name,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        line = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            return {}
        payload: dict[str, Any] = {"gpu_name": parts[3]}
        for key, value in (
            ("gpu_util", parts[0]),
            ("gpu_dedicated_used", parts[1]),
            ("gpu_dedicated_total", parts[2]),
        ):
            try:
                payload[key] = float(value)
            except ValueError:
                pass
        # nvidia-smi reports memory in MiB; everything else here is bytes.
        if "gpu_dedicated_used" in payload:
            payload["gpu_dedicated_used"] = payload["gpu_dedicated_used"] * 1024 * 1024
        if "gpu_dedicated_total" in payload:
            payload["gpu_dedicated_total"] = payload["gpu_dedicated_total"] * 1024 * 1024
        if len(parts) > 4:
            try:
                payload["temperature_celsius"] = float(parts[4])
                payload["temperature_source"] = "nvidia-smi"
            except ValueError:
                pass
        return payload

    def _run_windows_probe(self) -> dict[str, Any] | None:
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            return None
        # -EncodedCommand takes UTF-16LE base64: no quoting, no shell metacharacter
        # exposure, and no temp script on disk. (Windows PowerShell ignores a
        # piped-in script with `-Command -` in a non-interactive host, so stdin is
        # not an option.)
        encoded = base64.b64encode(_WINDOWS_PROBE_SCRIPT.encode("utf-16-le")).decode("ascii")
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    encoded,
                ],
                capture_output=True,
                text=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        text = completed.stdout.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _record_network(self, payload: dict[str, Any]) -> None:
        received = payload.get("net_received_bytes")
        sent = payload.get("net_sent_bytes")
        if not isinstance(received, (int, float)) or not isinstance(sent, (int, float)):
            return
        # Only ever called from the sampler thread, so this is the whole critical
        # section; the read side takes the lock defensively.
        with self._lock:
            self._network_sample += 1
            self._network_prev = self._network_current
            self._network_current = (
                self._network_sample,
                self._clock(),
                int(received),
                int(sent),
            )

    def gpu(self) -> dict[str, Any]:
        with self._lock:
            cached = dict(self._expensive)
        utilization = cached.get("gpu_util")
        name = cached.get("gpu_name")
        if utilization is None and name is None:
            return _metric_unavailable(
                "no GPU utilization counter and no GPU adapter were reachable"
            )
        metric: dict[str, Any] = {
            "available": utilization is not None,
            "name": name,
            "source": "Windows GPU Engine perf counters",
        }
        if utilization is None:
            metric.update(reason="the GPU is present but its utilization counter returned nothing")
        else:
            # Summed engine utilization, the same figure Task Manager derives.
            metric["percent"] = round(min(100.0, max(0.0, float(utilization))), 1)
        dedicated_used = cached.get("gpu_dedicated_used")
        dedicated_total = cached.get("gpu_dedicated_total")
        shared_used = cached.get("gpu_shared_used")
        if dedicated_used or dedicated_total:
            metric["memory"] = {
                "available": True,
                "kind": "dedicated",
                "used_bytes": int(dedicated_used or 0),
                "total_bytes": int(dedicated_total) if dedicated_total else None,
                "source": metric["source"],
            }
        elif shared_used:
            # Integrated GPUs have no dedicated VRAM at all: the honest figure is
            # how much system memory the GPU is using, labelled as shared.
            metric["memory"] = {
                "available": True,
                "kind": "shared",
                "used_bytes": int(shared_used),
                "total_bytes": None,
                "source": metric["source"],
            }
        else:
            metric["memory"] = _metric_unavailable(
                "the adapter reports no GPU memory usage", metric["source"]
            )
        return metric

    def network(self) -> dict[str, Any]:
        with self._lock:
            cached = dict(self._expensive)
            previous = self._network_prev
            current = self._network_current
        if current is None:
            return _metric_unavailable(
                "network byte counters are unavailable (install psutil, or no adapter is up)"
            )
        metric: dict[str, Any] = {
            "available": True,
            "interfaces": cached.get("net_interfaces") or [],
            "received_bytes": current[2],
            "sent_bytes": current[3],
            "source": "psutil.net_io_counters" if self._psutil is not None else "Get-NetAdapterStatistics",
        }
        if previous is None or previous[0] == current[0]:
            # Rates need two distinct samples. The totals above are already real,
            # so the rates report themselves unmeasured rather than as 0 B/s.
            metric["rate_available"] = False
            metric["reason"] = "measuring: byte rates need a second sample"
            return metric
        elapsed = max(1e-6, current[1] - previous[1])
        metric["rate_available"] = True
        metric["window_seconds"] = round(elapsed, 2)
        metric["download_bps"] = round(max(0.0, (current[2] - previous[2]) / elapsed), 1)
        metric["upload_bps"] = round(max(0.0, (current[3] - previous[3]) / elapsed), 1)
        return metric

    def temperature(self) -> dict[str, Any]:
        with self._lock:
            cached = dict(self._expensive)
        value = cached.get("temperature_celsius")
        if value is None:
            return _metric_unavailable(
                "no thermal sensor is exposed: the firmware reports no ACPI thermal zone"
                if _WINDOWS
                else "no thermal sensor is readable through psutil or /sys"
            )
        return {
            "available": True,
            "celsius": round(float(value), 1),
            "source": cached.get("temperature_source", "Windows ACPI thermal zone"),
        }

    # ── payload ───────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            sampled_at = self._expensive_at
            sampled = bool(self._expensive)
        age = None if sampled_at is None else round(max(0.0, self._clock() - sampled_at), 1)
        return {
            "host": self.host(),
            "cpu": self.cpu(),
            "memory": self.memory(),
            "storage": self.storage(),
            "gpu": self.gpu(),
            "network": self.network(),
            "battery": self.battery(),
            "temperature": self.temperature(),
            "uptime": self.uptime(),
            "sampler": {
                "expensive_sampled": sampled,
                "expensive_age_seconds": age,
                "expensive_available": self.expensive_available,
            },
        }

    @property
    def expensive_available(self) -> bool:
        """Whether the slow probe can work here at all (drives the UI's note)."""
        if self._psutil is not None or not _WINDOWS:
            return True
        return bool(shutil.which("powershell") or shutil.which("pwsh"))
