"""System telemetry service: one sampler thread, one merged payload.

The Command Center renders this payload and nothing else — hardware metrics from
:mod:`novacontrol.telemetry.hardware`, plus NovaControl's own status (tasks,
brain, vision, automation) taken from the application's existing surfaces. No
second copy of any of it is computed here.

Cost control is the point of the thread: GPU, network and temperature need a
WMI/perf-counter round trip that takes seconds on Windows, so a background
daemon samples them on a slow cadence and requests serve the cached reading.
Polling the endpoint therefore costs microseconds and spawns nothing.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any, Protocol

from novacontrol.telemetry.hardware import HardwareTelemetry

# GPU/network/temperature round trip is seconds; sampling faster than this would
# burn a process for no new information. The cheap metrics stay live per request.
DEFAULT_CADENCE_SECONDS = 10.0


class _AppLike(Protocol):
    def status(self) -> dict[str, Any]: ...


class SystemTelemetry:
    """Owns the hardware collector and the background sampler."""

    def __init__(
        self,
        app: _AppLike | None = None,
        *,
        cadence_seconds: float = DEFAULT_CADENCE_SECONDS,
        hardware: HardwareTelemetry | None = None,
        background: bool = True,
    ) -> None:
        self._app = app
        self._cadence = max(1.0, float(cadence_seconds))
        self._hardware = hardware or HardwareTelemetry()
        self._background = background
        # Set once a sampler thread has ever produced an expensive sample, so the
        # UI can tell "sampling" apart from "this machine has no such sensor".
        self._sampled_once = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────────

    def attach(self, app: _AppLike) -> None:
        self._app = app

    def start(self) -> bool:
        """Start the sampler thread once. Returns True if this call started it."""
        if not self._background:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="novacontrol-telemetry", daemon=True
            )
            self._thread.start()
            return True

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self.sample_now()
            # Event.wait, not sleep: stopping must not wait out a full cadence.
            self._stop.wait(self._cadence)

    def sample_now(self) -> dict[str, Any]:
        """Force one expensive sample on the calling thread (tests, first paint)."""
        payload = self._hardware.sample_expensive()
        self._sampled_once.set()
        return payload

    # ── payload ───────────────────────────────────────────────────────────

    def payload(self) -> dict[str, Any]:
        """The full telemetry document served to the UI."""
        hardware = self._hardware.snapshot()
        hardware["sampler"]["sampled_once"] = self._sampled_once.is_set()
        if not self._sampled_once.is_set():
            hardware["sampler"]["note"] = (
                "the slow sensor sample has not completed yet; GPU, network rate and "
                "temperature appear as soon as it does"
            )
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "hardware": hardware,
            "novacontrol": self._novacontrol_section(),
        }

    def _novacontrol_section(self) -> dict[str, Any]:
        app = self._app
        if app is None:
            return {"available": False, "reason": "the application status is not attached"}
        status = app.status()
        tasks = [task for task in status.get("tasks", []) if isinstance(task, dict)]
        active = [
            task
            for task in tasks
            if str(task.get("status", "")) in {"pending", "running"}
        ]
        brain = dict(status.get("brain") or {})
        cloud = dict(brain.get("cloud") or {})
        # The vision controller is read through getattr: it is a real surface on
        # NovaControlApplication (with a read-only `has_vision_model` property),
        # but this section must not invent a second protocol for it, and must stay
        # importable for a host that has no vision module at all.
        vision_controller = getattr(app, "vision", None)
        vision_model = bool(getattr(vision_controller, "has_vision_model", False))
        return {
            "available": True,
            "active_tasks": len(active),
            # Titles come straight from the task records — the UI never invents a
            # name for work it cannot see.
            "active_task_titles": [str(task.get("title", "")) for task in active[:4]],
            "tracked_tasks": int(status.get("tracked_tasks") or 0),
            "projects": int(status.get("projects") or 0),
            "modules": len(status.get("modules") or []),
            "runtime_started": bool(status.get("runtime_started")),
            "ai_engine": {
                "available": True,
                "mode": brain.get("mode"),
                "effective_mode": brain.get("effective_mode"),
                "provider": brain.get("provider"),
                "model": brain.get("model"),
                "model_configured": brain.get("model_configured"),
                "cloud_configured": bool(cloud.get("configured")),
                "cloud_state": cloud.get("state"),
            },
            "vision": {
                "available": vision_model,
                "reason": "" if vision_model else "no vision model is installed",
            },
            "automation": {
                "available": True,
                "workflows": int(status.get("automation_workflows") or 0),
                "desktop_runner": status.get("desktop_runner"),
                "browser_runner": status.get("browser_runner"),
                "browser_available": bool(status.get("browser_adapter_available")),
            },
            "phone_bridge": status.get("phone_bridge"),
        }


__all__ = ["DEFAULT_CADENCE_SECONDS", "SystemTelemetry"]
