"""Drive ONE controlled NovaForge evaluation through the HTTP API, for evidence.

NF-05's controlled mode is proven by tests against a stubbed desktop, and by the
manager-level acceptance script. This script proves the same chain the way an
operator reaches it: over HTTP, against a server this script starts, with a REAL
disposable window and REAL injected keystrokes.

Run it on Windows:

    python scripts/novaforge_controlled_evaluation_acceptance.py

The script starts its own API server on a free loopback port, in a temporary
working directory, so ``data/`` is disposable and the user's own ``data/`` is
never touched. It then launches its own ``NC FORGE HARNESS`` window
(``scripts/forge_harness_window.ps1``) and closes it again. It refuses to start
when a harness window is already open, because a window left in the ``READY``
state by a previous run would make "the verifier read READY" unfalsifiable.

Every step is an assertion about observed behaviour:

  1. the API answers and reports the harness as the only target window;
  2. a control plan with no objective is refused by preflight (no run, no token);
  3. a complete plan passes preflight and one digest-bound approval is issued;
  4. a start with no approval digest, and one carrying another plan's digest,
     are both refused as 422 and start no benchmark run;
  5. the approved start executes exactly one Guardian-gated key, the harness
     retitles, and the evaluation ends COMPLETED + valid with a decided verdict;
  6. the report separates execution from verification: the objective was read
     from the window, the plan's step is marked executed, and the evidence links
     resolve to a benchmark run and a session timeline.

Safety notes: input only goes to the harness window this script owns. Nothing is
installed, downloaded or launched beyond the API server and the harness, and the
user's own windows are never addressed.

Pass ``--base http://127.0.0.1:8001`` to run against an already-running server
instead (the harness window must be opened by the caller then).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
HARNESS = Path(__file__).with_name("forge_harness_window.ps1")
TARGET = "NC FORGE HARNESS"
READY = "READY"
STEPS: list[dict[str, Any]] = []


def record(step: str, ok: bool, detail: Any = "") -> None:
    STEPS.append({"step": step, "ok": bool(ok), "detail": detail})
    print(json.dumps({"step": step, "ok": bool(ok), "detail": detail}), flush=True)


def request(
    base: str, method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[int, Any]:
    """One HTTP call. A 4xx is DATA here: the negative checks assert on it."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    call = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(call, timeout=30) as response:  # noqa: S310 - loopback
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, raw
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        # A server that is still booting is not an answer: status 0 lets the
        # bounded wait below distinguish "not up yet" from an HTTP refusal.
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_server(workdir: Path, port: int, log_path: Path) -> subprocess.Popen[bytes]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    # A boot probe for a local model is not part of this acceptance run.
    env["NOVACONTROL_DISABLE_OLLAMA"] = "1"
    log = log_path.open("wb")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "novacontrol.api.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(workdir),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    # The child owns the descriptor now; keeping it open here would lock the
    # disposable directory against removal when the run ends.
    log.close()
    return process


def _wait_health(base: str, process: subprocess.Popen[bytes], log_path: Path) -> bool:
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        status, body = request(base, "GET", "/health")
        if status == 200 and isinstance(body, dict) and body.get("status") == "ok":
            return True
        time.sleep(0.25)
    return False


def _stop(process: subprocess.Popen[bytes] | None) -> None:
    """Stop a process THIS SCRIPT started, never anything it did not.

    On Windows the API server spawns helper processes (its input worker is a
    PowerShell child), and terminating only the parent leaves those children
    holding the disposable working directory. ``/T`` kills exactly this tree.
    """
    if process is None or process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:  # pragma: no cover - this script is Windows-only
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - a wedged host
        process.kill()
        process.wait(timeout=10)


def _target_windows(base: str) -> list[dict[str, Any]]:
    status, body = request(base, "GET", "/novaforge/windows")
    if status != 200 or not isinstance(body, dict):
        return []
    return [
        dict(row)
        for row in body.get("windows") or []
        if TARGET.lower() in str(row.get("title") or "").lower()
    ]


def _launch_harness(seconds: int) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(HARNESS),
            "-Seconds",
            str(seconds),
            "-KeepOpen",
        ],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def _wait_evaluation(base: str, evaluation_id: str, timeout: float = 90.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        status, body = request(base, "GET", f"/novaforge/evaluations/{evaluation_id}")
        if status != 200 or not isinstance(body, dict):
            return {}
        if body.get("status") != "running":
            return body
        time.sleep(0.25)
    return body


def _control_request() -> dict[str, Any]:
    """The plan this script approves: one key, one objective, bounded."""
    return {
        "game_id": "artifact-harness",
        "scenario_id": "controlled-objective",
        "target_title": TARGET,
        "duration_seconds": 15,
        "frame_interval_seconds": 0.25,
        "action_interval_seconds": 0.5,
        "verify_timeout_seconds": 10,
        "mode": "control",
        "objective": {"kind": "window_title", "parameter": READY},
        "action_plan": [{"kind": "key", "key": "w"}],
        "max_actions": 1,
    }


def _detail(body: Any, limit: int = 300) -> str:
    return str(body)[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default="",
        help="an already-running server; omit to start a disposable one",
    )
    args = parser.parse_args()

    if sys.platform != "win32":
        print(
            json.dumps(
                {
                    "step": "platform",
                    "ok": False,
                    "detail": f"the controlled evaluation needs the Windows artifact harness; "
                    f"this is {sys.platform}",
                }
            )
        )
        return 2

    server: subprocess.Popen[bytes] | None = None
    harness: subprocess.Popen[bytes] | None = None
    workdir: TemporaryDirectory[str] | None = None
    try:
        if args.base:
            base = args.base.rstrip("/")
            status, health = request(base, "GET", "/health")
            record(
                "the API is answering",
                status == 200 and isinstance(health, dict) and health.get("status") == "ok",
                _detail(health),
            )
        else:
            workdir = TemporaryDirectory(prefix="nc_controlled_eval_")
            port = _free_port()
            base = f"http://127.0.0.1:{port}"
            log_path = Path(workdir.name) / "uvicorn.log"
            server = _start_server(Path(workdir.name), port, log_path)
            alive = _wait_health(base, server, log_path)
            detail: Any = base
            if not alive:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
                detail = {"base": base, "log_tail": tail}
            record("a disposable API server answers /health", alive, detail)
            if not alive:
                return 2

        pre_existing = _target_windows(base)
        record("no harness window was already open", not pre_existing, pre_existing[:3])
        if pre_existing:
            print(json.dumps({"hint": "close the open NC FORGE HARNESS window first"}))
            return 2

        harness = _launch_harness(600)
        found: list[dict[str, Any]] = []
        deadline = time.monotonic() + 40.0
        while time.monotonic() < deadline:
            found = _target_windows(base)
            if found:
                break
            time.sleep(0.3)
        record("the disposable harness window is listed exactly once", len(found) == 1, found)

        invalid = request(
            base,
            "POST",
            "/novaforge/evaluations/preflight",
            {**_control_request(), "objective": None},
        )
        invalid_body = invalid[1] if isinstance(invalid[1], dict) else {}
        record(
            "a control plan with no objective is refused, and no approval is issued",
            invalid[0] == 200
            and not invalid_body.get("ready")
            and invalid_body.get("approval") is None
            and any("objective" in str(item).lower() for item in invalid_body.get("errors") or []),
            {
                "ready": invalid_body.get("ready"),
                "errors": invalid_body.get("errors"),
                "approval": invalid_body.get("approval"),
            },
        )

        status, preflight = request(
            base, "POST", "/novaforge/evaluations/preflight", _control_request()
        )
        preflight = preflight if isinstance(preflight, dict) else {}
        control = dict(preflight.get("control") or {})
        approval = dict(preflight.get("approval") or {})
        digest = str(control.get("plan_digest") or "")
        record(
            "preflight passes and issues one digest-bound approval",
            status == 200
            and bool(preflight.get("ready"))
            and preflight.get("mode") == "control"
            and len(digest) == 64
            and approval.get("digest") == digest
            and approval.get("single_use") is True,
            {
                "ready": preflight.get("ready"),
                "blocking": preflight.get("blocking"),
                "control_state": control.get("state"),
                "plan_digest": digest,
                "approval_digest": approval.get("digest"),
            },
        )
        record(
            "every required readiness check passed",
            (preflight.get("counts") or {}).get("fail") == 0,
            preflight.get("counts"),
        )
        if not digest:
            return 1

        started_runs = 0
        no_digest = request(
            base,
            "POST",
            "/novaforge/evaluations",
            {**_control_request(), "confirm": True},
        )
        runs_after_no_digest = request(base, "GET", "/novaforge/benchmarks/runs")[1]
        started_runs = int((runs_after_no_digest or {}).get("count") or 0)
        record(
            "starting without an approval digest is refused and starts nothing",
            no_digest[0] == 422
            and started_runs == 0
            and "approval" in _detail(no_digest[1]).lower(),
            {"status": no_digest[0], "detail": _detail(no_digest[1], 200), "runs": started_runs},
        )

        changed = {
            **_control_request(),
            "confirm": True,
            "action_plan": [{"kind": "key", "key": "enter"}],
            "approval_digest": digest,
        }
        wrong_plan = request(base, "POST", "/novaforge/evaluations", changed)
        runs_after_wrong_plan = request(base, "GET", "/novaforge/benchmarks/runs")[1]
        started_runs = int((runs_after_wrong_plan or {}).get("count") or 0)
        record(
            "an approval for a different plan is refused and starts nothing",
            wrong_plan[0] == 422
            and started_runs == 0
            and "does not match" in _detail(wrong_plan[1]).lower(),
            {"status": wrong_plan[0], "detail": _detail(wrong_plan[1], 200), "runs": started_runs},
        )

        # Injected keys go to the FOREGROUND window, and the Guardian refuses an
        # action aimed at a target that is not focused. The harness reclaims the
        # foreground on its own timer; wait until the API reports it, so the run
        # measures the plan rather than a race with whatever the host focused.
        focus_ok = False
        target: dict[str, Any] = {}
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            validate_status, validation = request(
                base,
                "POST",
                "/novaforge/validate",
                {
                    "name": "Controlled acceptance focus check",
                    "objective": "Read the harness foreground state",
                    "target_title": TARGET,
                    "time_limit_seconds": 15,
                    "max_actions": 1,
                },
            )
            validation = validation if isinstance(validation, dict) else {}
            target = dict(validation.get("target") or {})
            if validate_status == 200 and validation.get("valid") and target.get("foreground"):
                focus_ok = True
                break
            time.sleep(0.25)
        record(
            "the harness holds the foreground before the plan runs",
            focus_ok,
            {"title": target.get("title"), "foreground": target.get("foreground")},
        )
        if not focus_ok:
            return 1

        status, started = request(
            base,
            "POST",
            "/novaforge/evaluations",
            {**_control_request(), "confirm": True, "approval_digest": digest},
        )
        started = started if isinstance(started, dict) else {}
        evaluation_id = str(started.get("id") or "")
        record(
            "the approved plan starts a controlled evaluation",
            status == 200
            and started.get("mode") == "control"
            and started.get("status") == "running"
            and bool(evaluation_id),
            {
                "status": status,
                "id": evaluation_id,
                "mode": started.get("mode"),
                "detail": _detail(started, 400),
            },
        )
        if not evaluation_id:
            return 1

        finished = _wait_evaluation(base, evaluation_id)
        objective = dict(finished.get("objective") or {})
        run_control = dict(finished.get("control") or {})
        steps = [dict(row) for row in run_control.get("steps") or []]
        states = {str(row.get("stage")): row.get("state") for row in run_control.get("states") or []}
        record(
            "the evaluation completed with a decided, achieved objective",
            finished.get("status") == "completed"
            and bool(finished.get("valid"))
            and objective.get("decided") is True
            and objective.get("achieved") is True
            and objective.get("outcome") == "verified_success",
            {
                "status": finished.get("status"),
                "valid": finished.get("valid"),
                "objective": {
                    "decided": objective.get("decided"),
                    "achieved": objective.get("achieved"),
                    "outcome": objective.get("outcome"),
                    "evidence": (objective.get("evidence") or [{}])[0],
                },
                "reason": finished.get("reason"),
            },
        )
        record(
            "exactly one approved action executed, then the verdict was recorded",
            [row.get("status") for row in steps] == ["executed"]
            and run_control.get("state") == "verified"
            and run_control.get("verdict") == "verified_success"
            and states.get("execution") == "completed"
            and states.get("verification") == "verified_success"
            and states.get("cleanup") == "completed",
            {"steps": steps, "states": states, "control_state": run_control.get("state")},
        )

        metrics = dict(finished.get("metrics") or {})
        record(
            "the controlled metrics were measured, not defaulted",
            (metrics.get("objective_success") or {}).get("value") == 1.0
            and (metrics.get("actions_executed") or {}).get("value") == 1.0
            and (metrics.get("frames") or {}).get("available") is True,
            {
                "objective_success": metrics.get("objective_success"),
                "actions_executed": metrics.get("actions_executed"),
                "frames": (metrics.get("frames") or {}).get("value"),
            },
        )

        report_status, report = request(
            base, "GET", f"/novaforge/evaluations/{evaluation_id}/report"
        )
        report = report if isinstance(report, dict) else {}
        record(
            "the report separates execution from a verified objective",
            report_status == 200
            and report.get("objective_verified") is True
            and report.get("objective_outcome") == "verified_success"
            and report.get("verdict") == "verified_success"
            and report.get("real_gameplay_control_exercised") is True
            and report.get("plan_digest") == digest
            and report.get("approval", {}).get("digest") == digest,
            {
                "objective_verified": report.get("objective_verified"),
                "verdict": report.get("verdict"),
                "control_exercised": report.get("real_gameplay_control_exercised"),
                "missing_evidence": report.get("missing_evidence"),
            },
        )

        benchmark_run_id = str(report.get("benchmark_run_id") or "")
        run_status, run = request(
            base, "GET", f"/novaforge/benchmarks/runs/{benchmark_run_id}"
        ) if benchmark_run_id else (0, {})
        run = run if isinstance(run, dict) else {}
        timeline_link = str((report.get("evidence") or {}).get("timeline") or "")
        timeline_status, timeline = (
            request(base, "GET", timeline_link) if timeline_link else (0, {})
        )
        timeline = timeline if isinstance(timeline, dict) else {}
        events = [row for row in timeline.get("events") or [] if isinstance(row, dict)]
        record(
            "the evidence resolves to the benchmark run and its session timeline",
            run_status == 200
            and str((run.get("objective") or {}).get("outcome") or "") == "verified_success"
            and timeline_status == 200
            and bool(events),
            {
                "benchmark_run_id": benchmark_run_id,
                "run_status": run.get("status"),
                "timeline": timeline_link,
                "timeline_source": timeline.get("source"),
                "timeline_events": [str(row.get("type") or "") for row in events],
            },
        )
    finally:
        _stop(harness)
        _stop(server)
        if workdir is not None:
            workdir.cleanup()

    ok = all(step["ok"] for step in STEPS)
    print(
        json.dumps(
            {
                "summary": {
                    "steps": len(STEPS),
                    "failed": [step["step"] for step in STEPS if not step["ok"]],
                    "ok": ok,
                }
            }
        ),
        flush=True,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
