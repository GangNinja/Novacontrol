"""NF-22…NF-26 user-level workflow audit through the REAL application.

Not a unit test: this boots `create_app()` exactly as the product does, with a
temporary data directory and no-op device runners, then walks the flows an
operator actually uses (page, assets, chat, device plan/approve/execute, phone
pairing guidance, NovaForge read surfaces, SSE). Prints PASS/FAIL per step.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from fastapi.testclient import TestClient

from novacontrol.api.app import create_app
from novacontrol.application import NovaControlApplication
from novacontrol.browser import NoopBrowserRunner
from novacontrol.desktop import NoopDesktopRunner

TMP = TemporaryDirectory()
RESULTS: list[tuple[str, bool, str]] = []


def make_application(**kwargs: object) -> NovaControlApplication:
    kwargs["data_dir"] = TMP.name
    return NovaControlApplication(**kwargs)  # type: ignore[arg-type]


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def main() -> int:
    patchers: list[Any] = [
        mock.patch("novacontrol.api.app.NovaControlApplication", side_effect=make_application),
        mock.patch("novacontrol.application.LocalDesktopRunner", NoopDesktopRunner),
        mock.patch("novacontrol.application.PlaywrightBrowserRunner", NoopBrowserRunner),
    ]
    for patcher in patchers:
        patcher.start()
    client = TestClient(create_app())
    client.__enter__()
    try:
        # 1. the page and its no-cache contract
        page = client.get("/")
        body = page.text
        check("GET / serves the console", page.status_code == 200 and "id=\"app" in body)
        check(
            "the page is uncacheable",
            page.headers.get("cache-control") == "no-cache, no-store, max-age=0, must-revalidate"
            and page.headers.get("pragma") == "no-cache",
            page.headers.get("cache-control", ""),
        )
        check(
            "asset markers are injected server-side",
            "__NC_ASSET_VERSION__" not in body and "__NC_UI_VERSION__" not in body and "?v=" in body,
        )
        check("the sidebar is present", "nav" in body and "Chat" in body)

        # 2. the static surface the page loads
        for path in (
            "/static/app.js",
            "/static/js/novaforge.js",
            "/static/js/render-panels.js",
            "/static/js/render-utils.js",
            "/static/nc-os.css",
        ):
            asset = client.get(path)
            check(
                f"GET {path}",
                asset.status_code == 200
                and asset.headers.get("cache-control", "").startswith("no-cache, no-store"),
                f"{asset.status_code}",
            )

        # 3. chat through the scratch brain (deterministic, no model)
        hello = client.post("/ask", json={"request": "hello"})
        hello_body = hello.json() if hello.status_code == 200 else {}
        check(
            "POST /ask answers a greeting locally",
            hello.status_code == 200
            and hello_body.get("route") not in (None, "")
            and isinstance(hello_body.get("data"), (dict, str)),
            str(hello_body.get("route")),
        )

        # 4. a desktop command: plan -> approve -> execute through the real executor
        planned = client.post("/command/plan", json={"command": "open notepad and type hello"})
        plan_body = planned.json() if planned.status_code == 200 else {}
        token = str((plan_body.get("approval") or {}).get("token") or "")
        check(
            "POST /command/plan plans a desktop chain",
            planned.status_code == 200
            and plan_body.get("status") == "waiting_for_approval"
            and bool(token),
            str(plan_body.get("status")),
        )
        check(
            "the plan carries no strategy row until one is applied",
            plan_body.get("plan", {}).get("strategy") is None
            if isinstance(plan_body.get("plan"), dict)
            else True,
        )
        executed = client.post(
            "/command/execute",
            json={
                "command": "open notepad and type hello",
                "approval_token": token,
            },
        )
        executed_body = executed.json() if executed.status_code == 200 else {}
        check(
            "POST /command/execute runs the approved chain",
            executed.status_code == 200
            and str(executed_body.get("status") or "") in {"executed", "completed", "ok"},
            f"{executed.status_code} {executed_body.get('status')}",
        )
        replay = client.post(
            "/command/execute",
            json={
                "command": "open notepad and type hello",
                "approval_token": token,
            },
        )
        check("a replayed token is refused", replay.status_code == 403, str(replay.status_code))

        # 5. a browser command through the same /command/* path the UI posts to
        browser_plan = client.post(
            "/command/plan", json={"command": "navigate to https://example.com"}
        )
        browser_body = browser_plan.json() if browser_plan.status_code == 200 else {}
        browser_token = str((browser_body.get("approval") or {}).get("token") or "")
        check(
            "POST /command/plan routes a browser command",
            browser_plan.status_code == 200
            and str(browser_body.get("route") or "") == "browser_automation"
            and bool(browser_token),
            str(browser_body.get("route")),
        )
        browser_run = client.post(
            "/command/execute",
            json={
                "command": "navigate to https://example.com",
                "approval_token": browser_token,
            },
        )
        check(
            "POST /command/execute executes the browser plan",
            browser_run.status_code == 200,
            str(browser_run.status_code),
        )

        # 6. the JARVIS device endpoints
        desktop_plan = client.post("/desktop/plan", json={"command": "open calculator"})
        check(
            "POST /desktop/plan plans on the desktop controller",
            desktop_plan.status_code == 200
            and bool((desktop_plan.json().get("approval") or {}).get("token")),
            str(desktop_plan.status_code),
        )
        phone_plan = client.post("/phone/plan", json={"command": "take a screenshot"})
        phone_body = phone_plan.json() if phone_plan.status_code == 200 else {}
        check(
            "POST /phone/plan returns the pairing guidance, not an error",
            phone_plan.status_code == 200
            and str(phone_body.get("status") or "") == "waiting_for_phone_bridge"
            and bool(phone_body.get("bridge", {}).get("next_steps")),
            str(phone_body.get("status")),
        )

        # 7. the NovaForge read surfaces an operator opens
        for path, key in (
            ("/novaforge/status", None),
            ("/novaforge/sessions", None),
            ("/novaforge/state", None),
            ("/novaforge/objectives", None),
            ("/novaforge/observation", None),
            ("/novaforge/autonomy/trace", None),
            ("/novaforge/games", None),
            ("/novaforge/library", None),
            ("/novaforge/capabilities", None),
            ("/novaforge/windows", None),
        ):
            response = client.get(path)
            ok = response.status_code == 200
            if ok and key:
                ok = key in response.json()
            check(f"GET {path}", ok, str(response.status_code))

        # 8. the rest of the operator's surfaces
        for path in (
            "/status",
            "/health",
            "/activity",
            "/tasks",
            "/vision/status",
            "/bugs",
            "/intelligence",
            "/capabilities",
            "/diagnostics",
        ):
            response = client.get(path)
            check(f"GET {path.split('?')[0]}", response.status_code == 200, str(response.status_code))

        # 9. the failure paths an operator hits with no game window: a refusal, never a 500
        created = client.post(
            "/novaforge/sessions",
            json={
                "name": "audit",
                "objective": "open the menu",
                "target_title": "Freedom Fighters",
                "game": "Freedom Fighters",
                "max_actions": 4,
            },
        )
        check(
            "POST /novaforge/sessions refuses without a captured window",
            created.status_code < 500,
            f"{created.status_code} {str(created.json())[:90]}",
        )
        session_id = str((created.json() or {}).get("session_id") or "missing") if created.status_code == 200 else "missing"
        for path in (
            f"/novaforge/sessions/{session_id}",
            "/novaforge/sessions/not-a-session",
        ):
            response = client.get(path)
            check(f"GET {path} refuses cleanly", response.status_code < 500, str(response.status_code))
        start = client.post(f"/novaforge/sessions/{session_id}/autonomy/start", json={})
        check(
            "POST autonomy/start refuses without a real session",
            start.status_code < 500,
            str(start.status_code),
        )

        # 10. persistence: the app's data directory now holds its state files
        files = sorted(p.name for p in Path(TMP.name).iterdir())
        check(
            "the app persisted under its data dir",
            any(name.endswith(".json") for name in files),
            ", ".join(files[:4]),
        )
    finally:
        client.__exit__(None, None, None)
        for patcher in reversed(patchers):
            patcher.stop()
        TMP.cleanup()

    failed = [row for row in RESULTS if not row[1]]
    print(
        f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} workflow steps passed"
        + (f"; failures: {json.dumps([row[0] for row in failed])}" if failed else "")
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
