# System Telemetry

The Command Center shows live readings from the machine NovaControl is running
on, and the ribbon carries a compact **NOVA CORE** read-out (`CPU 18% · RAM 40% ·
3 TASKS`) that opens the full detail view. Every number is measured; when a
metric cannot be obtained, the card says **Unavailable** and prints the reason.

```
src/novacontrol/telemetry/hardware.py   # where each number comes from
src/novacontrol/telemetry/service.py    # sampler thread + NovaControl status merge
src/novacontrol/web/static/js/telemetry.js   # cards, NOVA CORE indicator, poll loop
```

`GET /system/telemetry` is the only endpoint: one document, one poll, one render
path for both surfaces.

## Where the numbers come from

| Metric | Source (in order of preference) |
|---|---|
| CPU % | `psutil.cpu_percent` → Win32 `GetSystemTimes` → `/proc/stat` |
| Memory used/available/total | `psutil.virtual_memory` → Win32 `GlobalMemoryStatusEx` → `/proc/meminfo` |
| Storage used/free/total | `shutil.disk_usage` on the system drive (stdlib, every platform) |
| GPU % | `nvidia-smi` → Windows `\GPU Engine(*)\Utilization Percentage` (summed across engines, the figure Task Manager derives) |
| GPU memory | nvidia-smi dedicated VRAM, else Windows `\GPU Adapter Memory(*)\Dedicated Usage`, else **shared** memory usage (integrated GPUs genuinely have no dedicated VRAM, and the card says `shared`) |
| Network up/down | `psutil.net_io_counters` → `Get-NetAdapterStatistics` → `/proc/net/dev` |
| Battery % / charging | `psutil.sensors_battery` → Win32 `GetSystemPowerStatus` → `/sys/class/power_supply` |
| Temperature | `psutil.sensors_temperatures` → `nvidia-smi` → WMI `MSAcpi_ThermalZoneTemperature` |
| Uptime | `psutil.boot_time` → Win32 `GetTickCount64` → `/proc/uptime` (its own `uptime` metric, so the card reads it like any other) |
| Active tasks, AI engine, Vision, Automation | the application's own `status()` / `vision` surfaces — not recomputed here |

`psutil` is optional. The app detects it (as `desktop/controller.py` already
did) and uses it when present; without it the platform's native facilities are
used, and the details table says which backend is in play.

## The honesty contract

* A metric is either **available** with a value produced by the OS, or
  unavailable with a non-empty `reason`. There is no third state and no default.
* Unavailable metrics carry **no value keys at all** — no `percent`, no
  `download_bps`, no `celsius`. The UI cannot accidentally render a zero that
  reads like a measurement.
* Byte rates need **two distinct counter samples**. Until the second sample
  exists, the totals are shown and the rate reports `measuring …` rather than
  `0 B/s` (an early version compared one sample with itself and confidently
  printed zeros — `NetworkRateTests` pins the fix).
* GPU utilisation is a sum over engine instances, so it is labelled with its
  source; a machine with no GPU counter reports unavailable instead of 0%.
* Every sampled value carries its age (`sampled 12s ago`), because a slow-sensor
  reading is not the same thing as a live one.

`tests/test_telemetry.py` enforces this: values are range-checked, RAM total is
cross-checked against an independent OS read, and any unavailable metric must
explain itself.

## Cost model

The two metric classes differ by orders of magnitude:

* **cheap** — CPU, memory, disk, battery, uptime are read in-process in
  microseconds (ctypes / stdlib), so each request serves them live. CPU is the
  delta between two counter reads, so a 2s poll yields a real 2s average,
  reported in `window_seconds`.
* **expensive** — GPU, network and temperature need a WMI/perf-counter round
  trip (seconds on Windows). A daemon thread samples them every
  `DEFAULT_CADENCE_SECONDS` (10s) into a cache, and requests serve that cache.
  **Serving a poll never spawns a process** — `test_serving_a_poll_never_spawns_a_process`
  patches the probe to fail if a request path ever calls it.

The Windows probe is a single PowerShell process per sample, handed over as a
base64 `-EncodedCommand`: no quoting for a shell, no script file on disk, and no
one-process-per-metric. `NOVACONTROL_DISABLE_TELEMETRY_SAMPLER=1` disables the
thread entirely (the test suite sets it); the endpoint still works, with the
cheap metrics live and the sampled ones honestly unavailable.

The browser polls every 2s and pauses while the tab is hidden. Cards are built
once and updated in place, so bars animate to new values instead of being
rebuilt on every tick.

## Relationship to other surfaces

* `/system/health` (`SystemHealthMonitor`) is a **release/configuration** report
  (required files, hardening checks) — a different question from "what is this
  machine doing right now", so telemetry does not replace or duplicate it.
* `/status` remains the runtime summary the metric grid and capabilities list
  render; the telemetry section reads the same `status()` for its
  task/engine/vision/automation pills rather than querying anything twice.
* The **System** panel keeps the deep system actions (health, hardening,
  package, task management). Telemetry is the live read-out; System is the
  operations panel.
