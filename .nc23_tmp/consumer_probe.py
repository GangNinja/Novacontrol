"""NF-23 Objective E: does a fresh observation reach the NF-16 consumer?

The observation interval decides this: the state service re-reads the observer
after perception and discards a reading whose frame a newer capture replaced
(``observation_superseded``). At the 0.5 s probe interval perception never
finished first; this probe runs the same path at 2.5 s and records what the
consumer actually produced — including the observation reference it carries.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / ".nc23_tmp"))

from novacontrol.desktop.capture import PersistentScreenCapture  # noqa: E402
from novacontrol.novaforge.manager import NovaForgeManager  # noqa: E402
from observer_lifecycle_probe import process_identity, spawn_window  # noqa: E402

TITLE = "NC NF23 LIFECYCLE PROBE"


def say(**fields: object) -> None:
    print(json.dumps(fields, default=str), flush=True)


async def main() -> int:  # noqa: C901 - a scripted probe, linear by design
    window = spawn_window(seconds=180)
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
        window.terminate()
        await worker.aclose()
        return 1
    pid = int(getattr(geometry, "pid"))

    tmp = tempfile.TemporaryDirectory()
    manager = NovaForgeManager(
        data_dir=Path(tmp.name),
        capture=lambda path: worker.capture(path, thumbnail=True),
        geometry=lambda title: worker.window_geometry(title=title),
        window_lines=lambda: None,
        capture_window=lambda h, path: worker.capture_window(h, path, thumbnail=True),
        process_identity=process_identity,
        observation_interval=2.5,
    )
    started = await manager.start_observation({"title": TITLE, "pid": pid, "interval": 2.5})
    say(started=bool(started.get("started")), interval=started.get("interval"))

    for _ in range(80):
        if manager.observation_status()["counts"]["observed"] >= 4:
            break
        await asyncio.sleep(0.25)
    status = manager.observation_status()
    say(
        frames=status["counts"],
        last_success=status["last_success"],
        state=status["state"],
    )

    outcome = await manager.state.analyze()
    payload = {
        "analyzed": bool(getattr(outcome, "analyzed", False)),
        "reason_code": str(getattr(outcome, "reason_code", "")),
        "reason": str(getattr(outcome, "reason", ""))[:200],
    }
    state = getattr(outcome, "state", None)
    if state is not None:
        row = state.to_dict()
        payload["state_id"] = row.get("state_id")
        payload["status"] = row.get("status")
        payload["observation"] = row.get("observation")
        payload["quality"] = row.get("quality")
        payload["changes"] = len(row.get("changes") or [])
        payload["entities"] = len(row.get("entities") or [])
    say(state_read=payload)
    say(
        state_counts=manager.state.status()["counts"],
        measured=manager.state.status()["measured"],
    )

    second = await manager.state.analyze()
    say(
        second_read_analyzed=bool(getattr(second, "analyzed", False)),
        second_read_code=str(getattr(second, "reason_code", "")),
    )

    stopped = await manager.stop_observation("consumer probe complete")
    say(stop_state=stopped.get("state"))
    await worker.aclose()
    window.terminate()
    tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
