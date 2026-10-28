"""End-to-end: the REAL application wiring, one observation-only session.

This is the production path — `NovaControlApplication` builds the desktop
runner, hands NovaForge its capture/geometry/window-surface seams, and the
session loop captures frames. No action is proposed or executed, so no gameplay
input is sent.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from novacontrol.application import NovaControlApplication

TITLE = sys.argv[1] if len(sys.argv) > 1 else "Freedom Fighters"
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0


async def main() -> None:
    report: dict = {"target": TITLE}
    with TemporaryDirectory() as temp_dir:
        app = NovaControlApplication(data_dir=Path(temp_dir))
        await app.start()
        try:
            manager = app.novaforge
            report["router_wired"] = manager._capture_router is not None
            report["capture_capabilities"] = manager.capabilities()["capture"]
            report["desktop_runner"] = type(app.desktop.runner).__name__
            validation = await manager.validate(
                {
                    "name": "Universal capture probe",
                    "objective": "Observe the running game without sending input.",
                    "target_title": TITLE,
                    "success_criteria": "Validated frames are recorded.",
                }
            )
            report["validation"] = {
                "valid": validation["valid"], "errors": validation["errors"],
                "target": validation["target"],
            }
            if not validation["valid"]:
                print(json.dumps(report, indent=2))
                return
            session = await manager.start(
                {
                    "name": "Universal capture probe",
                    "objective": "Observe the running game without sending input.",
                    "target_title": TITLE,
                    "success_criteria": "Validated frames are recorded.",
                    "time_limit_seconds": int(SECONDS) + 20,
                    "max_actions": 5,
                }
            )
            session_id = session["session_id"]
            cover = sys.argv[3] if len(sys.argv) > 3 else ""
            cover_at = time.monotonic() + 3.0
            covered = False
            deadline = time.monotonic() + SECONDS
            frames: list[dict] = []
            while time.monotonic() < deadline:
                if cover and not covered and time.monotonic() >= cover_at:
                    import subprocess as _sp

                    _sp.run(
                        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                         ".nc_win_ctl.ps1", "-Action", "focus", "-Title", cover],
                        capture_output=True, text=True, timeout=60,
                    )
                    covered = True
                    report["cover_focus"] = cover
                await asyncio.sleep(0.4)
                payload = manager.describe(manager.get(session_id))
                frame = payload.get("frame")
                if frame and (not frames or frame["seq"] != frames[-1]["seq"]):
                    frames.append(
                        {
                            "seq": frame["seq"],
                            "backend": frame["backend"],
                            "source": frame["source"],
                            "content": frame["content"],
                            "livable": frame["validation"].get("livable"),
                            "capture_ok": frame["validation"].get("capture_ok"),
                            "fresh": frame["validation"].get("fresh"),
                            "width": frame["width"],
                            "height": frame["height"],
                            "capture_ms": frame["capture_ms"],
                            "age_ms": frame["age_ms"],
                            "stale": frame["stale"],
                        }
                    )
            record = manager.get(session_id)
            report["session"] = {
                "state": record.state.value,
                "pause_reason": record.pause_reason,
                "frames_recorded": len(record.frames),
                "observations": len(record.observations),
                "errors": record.errors[-3:],
            }
            report["frames"] = frames
            report["status_capabilities"] = app.status().get("novaforge", {})
            await manager.stop(session_id, reason="probe finished")
        finally:
            await app.stop()
    print(json.dumps(report, indent=2))


asyncio.run(main())
