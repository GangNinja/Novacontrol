"""NF-24 live check on a DISPOSABLE WINDOW (explicitly not a game).

The real ``GameStateService`` reads fresh frames of the harness window through
the same manager wiring production uses. What this proves is the negative the
phase demands: a window with no game profile and no scene rule must report an
UNKNOWN scene with its reason, and must not invent a transition. It is NOT
live-game evidence and is reported separately.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / ".nc23_tmp"))

from observer_lifecycle_probe import TITLE, process_identity, say, spawn_window  # noqa: E402
from novacontrol.desktop.capture import PersistentScreenCapture  # noqa: E402
from novacontrol.novaforge.manager import NovaForgeManager  # noqa: E402


async def wait_for(predicate, timeout: float, step: float = 0.3) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return False


async def main() -> int:
    window = spawn_window(seconds=120)
    worker = PersistentScreenCapture(startup_timeout=60.0, frame_timeout=60.0)
    if not await worker.ensure_started():
        say(error=worker.last_error)
        return 1
    geometry = None
    for _ in range(40):
        geometry = await worker.window_geometry(title=TITLE)
        if geometry is not None:
            break
        await asyncio.sleep(0.5)
    if geometry is None:
        say(error="the disposable window never appeared")
        return 1
    handle = int(getattr(geometry, "handle"))
    pid = int(getattr(geometry, "pid"))
    say(
        target={"handle": handle, "pid": pid, "title": str(getattr(geometry, "title", ""))},
        target_bounds=list(getattr(geometry, "bounds")),
    )

    manager = NovaForgeManager(
        data_dir=Path(tempfile.mkdtemp(prefix="nf24-window-")),
        capture=lambda path: worker.capture(path, thumbnail=True),
        geometry=lambda title: worker.window_geometry(title=title),
        window_lines=lambda: None,
        capture_window=lambda h, path: worker.capture_window(h, path, thumbnail=True),
        process_identity=process_identity,
        # Slower than one perception pass: the service (correctly) discards a
        # reading whose frame a newer capture replaced, so a 0.5 s interval
        # makes every reading superseded and none attributable.
        observation_interval=3.0,
    )
    readings: list[dict[str, Any]] = []
    try:
        started = await manager.start_observation(
            {"title": TITLE, "pid": pid, "interval": 3.0}
        )
        say(started=bool(started.get("started")), start_state=started.get("state"))
        reached = await wait_for(
            lambda: manager.observation_status()["counts"]["observed"] >= 2, 30.0
        )
        status = manager.observation_status()
        say(
            observed_reached=reached,
            observed_state=status["state"],
            observed_counts=status["counts"],
            observed_backend=status["backend"],
            observed_dims=sorted(
                {
                    (row.get("width"), row.get("height"))
                    for row in status["history"]
                    if row.get("status") == "observed"
                }
            ),
        )
        for index in range(2):
            before = manager.observation_status()["counts"]["observed"]
            await wait_for(
                lambda: manager.observation_status()["counts"]["observed"] > before, 20.0
            )
            started_at = time.monotonic()
            outcome = await manager.state.analyze()
            row: dict[str, Any] = {
                "reading": index + 1,
                "analyzed": outcome.analyzed,
                "refusal": outcome.reason_code,
                "wall_ms": round((time.monotonic() - started_at) * 1000.0, 1),
            }
            if outcome.state is not None:
                state = outcome.state
                row.update(
                    {
                        "observation": state.observation.observation_id,
                        "status": state.status.value,
                        "profile": (
                            state.profile.profile.profile_id if state.profile.profile else ""
                        ),
                        "scene": state.scene.to_dict(),
                        "transition": state.transition.to_dict(),
                        "hud": [
                            {
                                "element": item.element_id,
                                "status": item.status.value,
                                "text": item.text,
                            }
                            for item in state.hud
                        ],
                        "processing_ms": state.latency.get("processing_ms"),
                        "text_lines": [str(line.get("text")) for line in state.text][:5],
                    }
                )
                objectives = manager.objective_state.current
                row["objectives_status"] = (
                    [item.status.value for item in objectives.objectives]
                    if objectives is not None
                    else []
                )
            readings.append(row)
            say(**row)
        say(service_status={"scene": manager.state.status()["scene"]})
    finally:
        try:
            await manager.stop_observation("probe complete")
        except Exception:  # noqa: BLE001 - reported through close below
            pass
        closer = getattr(worker, "aclose", None)
        if callable(closer):
            await closer()

    # The window exits on its own (-Seconds); wait for that instead of killing it.
    exited = False
    for _ in range(90):
        if window.poll() is not None:
            exited = True
            break
        await asyncio.sleep(1.0)
    say(window_exited=exited, window_seconds_left="(own timer)")
    say(
        summary={
            "readings": len(readings),
            "scenes": [row.get("scene", {}).get("kind") for row in readings],
            "scene_status": [row.get("scene", {}).get("status") for row in readings],
            "transitions": [row.get("transition", {}).get("state") for row in readings],
            "processing_ms": [row.get("processing_ms") for row in readings],
            "profiles": [row.get("profile") for row in readings],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
