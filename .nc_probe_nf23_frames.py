"""NF-23/NF-24 live evidence on the project's OWN disposable harness window.

Launches `scripts/forge_harness_window.ps1` (a Windows Forms window, not a game),
starts a NovaForge session against it through the shipped application, and reads
the frames the real capture ladder produced. No input is sent and no game is
touched; the harness closes itself on its timeout.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from tempfile import TemporaryDirectory

from novacontrol.application import NovaControlApplication

TITLE_FRAGMENT = "NC FORGE HARNESS"


async def main() -> int:
    proc = subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "scripts/forge_harness_window.ps1",
            "-Seconds",
            "45",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with TemporaryDirectory() as tmp:
            app = NovaControlApplication(data_dir=tmp)
            await app.start()
            try:
                title = ""
                for _ in range(40):
                    rows = (await app.novaforge.windows()).get("windows") or []
                    hit = [r for r in rows if TITLE_FRAGMENT.lower() in str(r.get("title", "")).lower()]
                    if hit:
                        title = str(hit[0].get("title"))
                        break
                    await asyncio.sleep(0.25)
                print("harness window:", title or "NOT FOUND")
                if not title:
                    return 2

                started = await app.novaforge.start(
                    {
                        "name": "nf23 live harness",
                        "objective": "the harness reports READY",
                        "target_title": title,
                        "max_actions": 1,
                        "time_limit_seconds": 20,
                        "frame_interval_seconds": 0.4,
                    }
                )
                print("start:", json.dumps(started, default=str)[:400])
                session_id = str(started.get("session_id") or "")
                if not session_id:
                    return 3

                deadline = time.monotonic() + 12
                record = None
                while time.monotonic() < deadline:
                    record = app.novaforge.get(session_id)
                    if record is not None and len(record.frames) >= 3:
                        break
                    await asyncio.sleep(0.4)

                record = app.novaforge.get(session_id)
                frames = list(record.frames) if record is not None else []
                rows = []
                for frame in frames[-6:]:
                    rows.append(
                        {
                            "seq": frame.seq,
                            "backend": frame.backend,
                            "source": frame.source,
                            "dims": f"{frame.width}x{frame.height}",
                            "fresh": frame.fresh,
                            "content": frame.content,
                            "live": bool((frame.validation or {}).get("livable")),
                            "reason": str((frame.validation or {}).get("reason") or "")[:70],
                        }
                    )
                print("target_validated:", bool(record.target_validated) if record else None)
                print("state:", getattr(record.state, "value", record.state))
                verification = dict(record.verification or {}) if record else {}
                print("verification:", json.dumps(verification, default=str)[:500])
                print("verified_is_true:", verification.get("verified") is True)
                print("frames:", json.dumps(rows, indent=1))
                print("distinct_content:", len({f.content for f in frames}))
                print("live_frames:", sum(1 for f in frames if (f.validation or {}).get("livable")))
                observations = list(record.observations) if record is not None else []
                latest = observations[-1].to_dict() if observations else {}
                print("observation_count:", len(observations))
                print("latest_observation:", json.dumps(latest, default=str)[:600])

                # NF-24 negative control: a checkable objective that the window does
                # NOT satisfy must never come back verified, before or after input.
                attached = app.novaforge.set_objective(
                    session_id,
                    {"kind": "window_title", "mode": "contains", "parameter": "READY"},
                )
                print("objective_supported:", attached.get("supported"), attached.get("verifier"))
                verdict = await app.novaforge.verify_objective(session_id, reason="audit negative control")
                print("verdict:", json.dumps(verdict, default=str)[:600])
                inner = verdict.get("verification") if isinstance(verdict.get("verification"), dict) else verdict
                print("verdict_outcome:", inner.get("outcome"), "achieved:", inner.get("achieved"))
                print("negative_control_ok:", inner.get("outcome") != "verified_success")

                # NF-25: an UNapproved task is refused before anything is sent.
                from novacontrol.novaforge.models import MissionConfig
                from novacontrol.novaforge.tasks import TaskSpec

                spec = TaskSpec(
                    task_id="audit-task",
                    mission=MissionConfig(
                        name="audit", objective="reach READY", target_title=title, max_actions=1
                    ),
                    game="NC FORGE HARNESS",
                    success_conditions=("the title says READY",),
                    required_evidence=("scene:menu",),
                    permitted_actions=("key_press", "key_release"),
                    max_seconds=5.0,
                    max_cycles=1,
                )
                refused = await app.novaforge.run_task(spec, session_id, approved=False)
                print(
                    "unapproved_task:",
                    json.dumps(
                        {
                            "status": refused.get("status"),
                            "verdict": refused.get("verdict"),
                            "code": refused.get("code"),
                            "reason": str(refused.get("reason") or "")[:120],
                        },
                        default=str,
                    ),
                )
                await app.novaforge.stop(session_id, reason="audit probe")
            finally:
                await app.stop()
    finally:
        try:
            proc.terminate()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
