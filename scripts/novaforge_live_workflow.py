"""Drive ONE real gameplay session through the running API, for live evidence.

This is the same chain the NovaForge panel performs with the same endpoints —
validate a target, start a session, configure a checkable objective, plan an
action, approve it, read the verdict — but issued over HTTP against a server the
user is actually running, so the numbers it prints are the ones the UI shows.

    python -m uvicorn novacontrol.api.app:create_app --factory --port 8001
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts/forge_harness_window.ps1 -KeepOpen
    python scripts/novaforge_live_workflow.py --base http://127.0.0.1:8001

It prints one JSON line per step and exits non-zero if the chain does not reach a
verified success. Input only ever goes to the ``NC FORGE HARNESS`` window.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

TARGET = "NC FORGE HARNESS"


def call(base: str, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310 - loopback only
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {path} -> HTTP {exc.code}: {exc.read().decode('utf-8')[:400]}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    base = args.base
    steps: list[dict[str, Any]] = []

    def record(step: str, ok: bool, detail: Any = "") -> None:
        steps.append({"step": step, "ok": bool(ok), "detail": detail})
        print(json.dumps({"step": step, "ok": bool(ok), "detail": detail}), flush=True)

    status = call(base, "GET", "/novaforge/status")
    record("status reports readiness", bool(status["status"]["capture_supported"]),
           status["status"]["limitations"][0])

    windows = call(base, "GET", "/novaforge/windows")
    titles = [row["title"] for row in windows.get("windows") or []]
    matches = [title for title in titles if TARGET.lower() in title.lower()]
    record("the harness window is listed", len(matches) == 1, matches)

    mission = {
        "name": "Live API acceptance",
        "objective": "Prove the guarded action path over HTTP",
        "target_title": TARGET,
        "time_limit_seconds": 120,
        "max_actions": 3,
    }
    validation = call(base, "POST", "/novaforge/validate", mission)
    record("validate pins the target", bool(validation["valid"]),
           {"target": validation.get("target"), "errors": validation.get("errors")})

    session = call(base, "POST", "/novaforge/sessions", mission)
    session_id = session["session_id"]
    record("session started", session.get("state") == "RUNNING", session_id)

    objective = call(base, "POST", f"/novaforge/sessions/{session_id}/objective",
                     {"kind": "window_title", "parameter": "READY"})
    record("checkable objective configured", bool(objective["supported"]), objective["objective"])

    plan = call(base, "POST", f"/novaforge/sessions/{session_id}/actions/plan",
                {"kind": "key", "key": "w"})
    proposal = plan["proposal"]
    record("plan is a proposal", plan["status"] == "proposed" and plan["executed"] is False,
           proposal["proposal_id"])

    denied = call(base, "POST", f"/novaforge/sessions/{session_id}/actions/execute",
                  {"proposal_id": proposal["proposal_id"], "digest": "0" * 64})
    record("an unapproved digest is refused", denied["status"] == "denied",
           denied["outcome"]["reason"][:120])

    executed = call(base, "POST", f"/novaforge/sessions/{session_id}/actions/execute",
                    {"proposal_id": proposal["proposal_id"], "digest": proposal["digest"]})
    record("the approved action executed", executed["status"] == "executed",
           {"presses": executed["outcome"]["presses"], "releases": executed["outcome"]["releases"],
            "held_after": executed["outcome"]["held_after"]})

    verdict = executed.get("verification") or {}
    record("the window was read back and agrees", verdict.get("outcome") == "verified_success",
           {"achieved": verdict.get("achieved"), "evidence": (verdict.get("evidence") or [{}])[0]})

    described = call(base, "GET", f"/novaforge/sessions/{session_id}")
    record("the session completed on that verdict", described.get("state") == "COMPLETED",
           described.get("state"))

    ok = all(step["ok"] for step in steps)
    print(json.dumps({"summary": {"session_id": session_id, "steps": len(steps),
                                  "failed": [s["step"] for s in steps if not s["ok"]], "ok": ok}}))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
