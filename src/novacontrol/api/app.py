"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import MutableMapping
from os import PathLike
from pathlib import Path
from typing import Any
from uuid import uuid4

from novacontrol.api.auth import ApiTokenAuthenticator
from novacontrol.api.middleware import RateLimitMiddleware, RequestLoggingMiddleware, SecurityHeadersMiddleware
from novacontrol.api.models import ApiSurface, AskRequest, BrainDecideRequest, CommandPlanRequest, ExploreRequest_
from novacontrol.application import NovaControlApplication
from novacontrol.core.events import Event
from novacontrol.explore import ExploreRequest
from novacontrol.explore.trending import TrendingTopicsProvider
from novacontrol.planning import PlanningEngine
from novacontrol.release import ReleaseHardeningChecker, RuntimePackageBuilder, SystemHealthMonitor
from novacontrol.settings import ApprovalMode
from novacontrol.telemetry import SystemTelemetry


_ASSET_VERSION_MARKER = "__NC_ASSET_VERSION__"
# Visible UI build stamp in the sidebar footer; same content-derived version,
# shortened, so a stale cached page is instantly distinguishable from current UI.
_UI_VERSION_MARKER = "__NC_UI_VERSION__"

# "Delete All Tasks" undo: wiped records held in memory behind one-shot tokens.
# Process-scoped (an undo cannot reach across a restart) and time-boxed; the
# token store is bounded by the expiry sweep on every undo look-up.
_TASK_UNDO_WINDOW_SECONDS = 30.0
_task_clear_snapshots: dict[str, dict[str, Any]] = {}


def _telemetry_sampler_enabled() -> bool:
    """Whether the slow-sensor sampler thread runs.

    Off by environment for test runs, which would otherwise spawn a probe
    process per app instance. The endpoint still works with it off: the cheap
    metrics are read live and the sampled ones report themselves unavailable.
    """
    disabled = os.environ.get("NOVACONTROL_DISABLE_TELEMETRY_SAMPLER", "").strip().lower()
    return disabled not in {"1", "true", "yes", "on"}


def _static_asset_version(static_dir: Path) -> str:
    """Content-derived cache-bust version for every static asset.

    There is no build step — the files on disk are the product — so the version
    is a hash of the asset tree itself: any HTML/CSS/JS edit changes the token
    and rekeys every asset URL. That forces fresh fetches even for browsers with
    heuristic cache entries from before the no-store headers existed, without
    anyone hand-bumping a date token. Computed per request, so edits made while
    the server is running take effect on the next page load.
    """
    digest = hashlib.sha1()
    for path in sorted(static_dir.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(static_dir).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def _never_cache_headers() -> dict[str, str]:
    """Headers that forbid storing HTML/CSS/JS, so UI edits show on the next load.

    This is a local dev-style app with no build step: the files on disk ARE the
    product, so every response must be non-storable. "no-cache" alone only forces
    revalidation and still allows heuristic reuse of entries cached before the
    header existed; "no-store" forbids caching the response at all, which is the
    only guarantee that a future request can never be served stale bytes.
    """
    return {
        "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }


def sse_frame(event: Event) -> str:
    """Serialize one bus event as an SSE frame with a correlation id.

    Correlation contract: every frame names its activity. Sources that track
    runs (explore/command progress) put the RUN-scoped id in the payload;
    everything else falls back to the event's own id, so a consumer can always
    group frames into activities.
    """
    payload = dict(event.payload)
    payload["event_type"] = event.type
    if event.correlation_id and "correlation_id" not in payload:
        payload["correlation_id"] = event.correlation_id
    return f"event: {event.type}\ndata: {json.dumps(payload, default=str)}\n\n"


def _routing_preview(nova: NovaControlApplication, text: str, intent: str) -> dict[str, Any]:
    """A small preview of what the user would actually see for a landing rung.

    Cheap and side-effect free by design: scratch answers run locally, plan
    outlines are deterministic, and the research "headline" uses the same
    offline overview generator the report would show — full research (sources,
    key points) is one click away in the Explore panel, never triggered here.
    """
    from novacontrol.brain.scratch import scratchable_intent
    from novacontrol.explore.sections import overview as overview_headline

    try:
        scratchable = scratchable_intent(text.lower())
        if scratchable:
            payload = nova.brain.scratch.answer(text, context=nova.status())
            return {"kind": "scratch", "text": str(payload.get("message", ""))[:400]}
        if intent == "explore":
            return {
                "kind": "explore",
                "headline": overview_headline(text, ())[:200],
                "note": "Running Explore adds sources, key points, and a detailed explanation.",
            }
        if intent == "plan":
            plan = PlanningEngine().create_plan(text)
            return {
                "kind": "plan",
                "outline": [step.title for step in plan.steps][:8],
                "needs_clarification": bool(getattr(plan, "needs_clarification", False)),
            }
        if intent in ("desktop_automation", "phone_control", "browser_automation"):
            device_plan = nova.plan_command(text)
            return {
                "kind": "plan",
                "outline": [str(a.get("type") or a.get("action") or "") for a in device_plan.get("workflow", {}).get("actions", [])][:8],
                "target": device_plan.get("target"),
                "summary": str(device_plan.get("summary", ""))[:200],
            }
        if intent == "chat":
            return {"kind": "info", "text": "Answered by the configured chat model."}
        return {"kind": "info", "text": f"Routed to {intent}. Open that panel to run it."}
    except Exception as exc:  # a broken preview must never break the trace
        return {"kind": "info", "text": f"Preview unavailable: {type(exc).__name__}"}


def create_app() -> Any:
    """Create the NovaControl API app."""
    # Daily-updates provider for the Explore panel's topic suggestions: one
    # per app (cached + rotating), edition defaults to India.
    trending = TrendingTopicsProvider()
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
        from fastapi.middleware.gzip import GZipMiddleware
        from fastapi.responses import HTMLResponse
        from fastapi.staticfiles import StaticFiles
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError("FastAPI is not installed. Run `pip install -e .` first.") from exc

    class NoCacheStaticFiles(StaticFiles):
        """Static files that are never cached, so UI edits appear on the next load."""

        def file_response(
            self,
            full_path: str | PathLike[str],
            stat_result: os.stat_result,
            scope: MutableMapping[str, Any],
            status_code: int = 200,
        ) -> Any:
            response = super().file_response(full_path, stat_result, scope, status_code)
            response.headers.update(_never_cache_headers())
            return response

    authenticator = ApiTokenAuthenticator(os.getenv("NOVACONTROL_API_TOKEN"))
    api_surface = ApiSurface.default()
    nova = NovaControlApplication(data_dir=Path("data"))
    app = FastAPI(title="NovaControl", version="0.1.0")

    # Middleware (order matters: last added = first executed)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(RateLimitMiddleware, max_requests=120, window_seconds=60.0)

    static_dir = Path(__file__).resolve().parents[1] / "web" / "static"
    if static_dir.exists():
        app.mount("/static", NoCacheStaticFiles(directory=static_dir), name="static")

    # Real system telemetry for the Command Center. The GPU/network/temperature
    # probe costs seconds, so a daemon thread samples it on a slow cadence and
    # /system/telemetry serves the cached reading — a poll never spawns anything.
    telemetry = SystemTelemetry(nova, background=_telemetry_sampler_enabled())

    @app.on_event("startup")
    async def start_telemetry() -> None:
        telemetry.start()

    @app.on_event("startup")
    async def startup() -> None:
        await nova.start()

    @app.on_event("shutdown")
    async def shutdown() -> None:
        telemetry.stop()
        await nova.stop()

    async def require_auth(authorization: str | None = Header(default=None)) -> str:
        result = authenticator.authenticate(authorization)
        if not result.authenticated:
            raise HTTPException(status_code=401, detail=result.reason)
        return result.principal

    @app.get("/health")
    async def health() -> dict[str, str]:
        # The build id belongs to liveness, not decoration: the UI files on disk
        # ARE the product (no build step), so a long-lived page can be rendering
        # CSS/JS from before an edit — which is how an already-fixed layout bug
        # keeps being reported. The page compares its own stamp against this and
        # offers a reload, so "which build am I looking at" is never a mystery.
        return {"status": "ok", "ui_version": _static_asset_version(static_dir)}

    @app.get("/")
    async def web_app() -> Any:
        # Never store index.html, and inject the content-derived asset version
        # into every script/link URL so edits rekey assets without a manual bump.
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        version = _static_asset_version(static_dir)
        # The sidebar footer shows the same content-derived build id (shortened)
        # that /health reports, so a stale cached page is recognizable at a
        # glance and self-detecting: the page polls /health and says so.
        # Never hand-write a date token here — the hash is the stamp.
        html = html.replace(_ASSET_VERSION_MARKER, version).replace(_UI_VERSION_MARKER, version[:8])
        return HTMLResponse(content=html, headers=_never_cache_headers())

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return {
            "name": "NovaControl",
            "status": "ok",
            "api": api_surface.to_dict(),
            "auth_enabled": authenticator.enabled,
            "app": nova.status(),
        }

    @app.get("/system/health")
    async def system_health() -> dict[str, Any]:
        return SystemHealthMonitor(Path.cwd()).run(nova.status()).to_dict()

    @app.get("/system/telemetry")
    async def system_telemetry() -> dict[str, Any]:
        """Live machine metrics for the Command Center, all of them real.

        Cheap metrics (CPU, RAM, disk, battery, uptime) are read live on each
        request; the expensive ones come from the sampler thread's cache. Every
        metric carries `available` and, when false, the reason — the UI renders
        "Unavailable" rather than a plausible-looking number nobody measured.
        """
        return telemetry.payload()

    @app.get("/system/harden")
    async def system_harden() -> dict[str, Any]:
        return ReleaseHardeningChecker(Path.cwd()).run().to_dict()

    @app.get("/system/package")
    async def system_package() -> dict[str, Any]:
        return RuntimePackageBuilder(Path.cwd()).build().to_dict()

    @app.get("/settings")
    async def get_settings(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.settings.to_dict()

    @app.post("/settings")
    async def update_settings(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        settings = nova.settings.update(
            approval_mode=ApprovalMode(str(payload["approval_mode"])) if "approval_mode" in payload else None,
            detailed_explanations=bool(payload["detailed_explanations"])
            if "detailed_explanations" in payload
            else None,
            include_videos_in_explore=bool(payload["include_videos_in_explore"])
            if "include_videos_in_explore" in payload
            else None,
            auto_approve_run=bool(payload["auto_approve_run"])
            if "auto_approve_run" in payload
            else None,
        )
        nova.persist()
        return settings.to_dict()

    @app.post("/ask")
    async def ask(payload: AskRequest, _principal: str = Depends(require_auth)) -> dict[str, Any]:
        response = await nova.handle_request(payload.request, image=payload.image)
        return response.to_dict()

    @app.post("/brain/mode")
    async def brain_mode(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Switch the chat brain: auto | llm | scratch (hot-swap, no restart).

        The setting persists server-side, so the choice survives reloads and
        restarts; the returned status is what the Chat panel's switch renders.
        """
        try:
            return nova.set_brain_mode(str(payload.get("mode", "")))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/brain/mode")
    async def brain_mode_get(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.brain_status()

    @app.get("/brain/cloud/presets")
    async def brain_cloud_presets(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Cloud LLM provider picker metadata (no secrets)."""
        return {"presets": nova.cloud_llm_presets()}

    @app.post("/brain/cloud")
    async def brain_cloud_set(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Install a cloud LLM (ChatGPT/Gemini/Groq/…) as the active brain.

        The API key is stored only on this machine (data/cloud_llm.json,
        gitignored) and is never returned by any endpoint — responses carry a
        redacted tail hint only.
        """
        try:
            return nova.set_cloud_llm(
                str(payload.get("provider", "")),
                str(payload.get("api_key", "")),
                model=str(payload.get("model", "")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/brain/cloud/clear")
    async def brain_cloud_clear(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Remove the stored cloud LLM config and its API key."""
        return nova.clear_cloud_llm()

    @app.post("/brain/cloud/test")
    async def brain_cloud_test(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Ping a cloud provider with the pasted key BEFORE saving anything.

        One tiny completion through the exact provider construction the real
        connect would use. Nothing is persisted; the key is validated (or its
        rejection explained) and discarded.
        """
        try:
            return await nova.test_cloud_llm(
                str(payload.get("provider", "")),
                str(payload.get("api_key", "")),
                model=str(payload.get("model", "")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/brain/ollama/models")
    async def brain_ollama_models(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Models available on the local Ollama for the brain model picker.

        Always a fresh probe (models pulled after boot appear), never the
        boot-time snapshot. Awaited (not inline): the Ollama probe blocks a
        worker thread briefly; the event loop never stalls.
        """
        return await nova.local_models()

    @app.post("/brain/local/model")
    async def brain_local_model(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Pin the LOCAL brain to a specific Ollama model (hot swap, no restart).

        Empty model clears the pick (auto-pick returns). The choice persists
        locally and re-arms at boot; only the local provider is touched — a
        configured cloud LLM stays exactly where it is.
        """
        try:
            return nova.set_local_model(str(payload.get("model", "")))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/models/status")
    async def models_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """What the model runtime holds right now, and the memory policy.

        The chat model and the vision model both compete for the same RAM on
        this machine, so the manager is exclusive by default: the panel asks
        here rather than each surface probing the runtime itself.
        """
        return await nova.model_status()

    @app.post("/models/load")
    async def models_load(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Load a model, unloading whatever must go first to make room."""
        try:
            return await nova.load_model(str(payload.get("model", "")))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/models/unload")
    async def models_unload(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Release one model, or every resident model when none is named."""
        return await nova.unload_model(str(payload.get("model", "")))

    @app.post("/chat/clear")
    async def chat_clear(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Wipe the conversation: server memory AND the shared transcript."""
        nova.brain.conversation.clear()
        transcript_dropped = nova.chat_transcript.clear()
        nova.persist()
        return {"status": "cleared", "conversation_turns": 0, "transcript_dropped": transcript_dropped}

    @app.get("/chat/history")
    async def chat_history_get(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The shared chat thread (server-persisted, same for every browser)."""
        return nova.chat_history()

    @app.post("/chat/history")
    async def chat_history_post(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Append one client turn to the shared thread, or (action=migrate)
        one-time import a browser's old localStorage thread. The import is
        idempotent per (role, text, at) so re-running it never duplicates."""
        if str(payload.get("action", "")) == "migrate":
            raw_turns = payload.get("turns")
            turns = [t for t in raw_turns if isinstance(t, dict)] if isinstance(raw_turns, list) else []
            return nova.import_chat_history(turns)
        return nova.record_chat_turn(
            str(payload.get("role", "")),
            str(payload.get("text", "")),
            route=str(payload.get("route", "")),
        )

    @app.get("/tasks")
    async def tasks_list(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Trimmed task views for the System panel's delete buttons.

        TaskRecord.to_dict() embeds the full `result` blob (a completed /ask
        stores a whole response in it), so the listing keeps only the fields the
        rows render — never the payload.
        """
        return {
            "tasks": [
                {
                    "id": task.id,
                    "title": task.title,
                    "kind": task.kind,
                    "status": task.status.value,
                    "created_at": task.created_at.isoformat(),
                }
                for task in nova.tasks.list()
            ]
        }

    @app.post("/tasks/delete")
    async def tasks_delete(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Delete one tracked task by id (404 when the id is unknown)."""
        task_id = str(payload.get("id", ""))
        try:
            deleted = nova.tasks.delete(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown task id: {task_id}") from exc
        nova.persist()
        return {"status": "deleted", "id": deleted.id}

    @app.post("/tasks/clear")
    async def tasks_clear(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Delete every tracked task record — undoable for a short window.

        The wiped records are held (in memory only) behind a one-shot undo
        token so a misclicked "Delete All" can be taken back. The snapshot
        dies with the token: after the window (or process exit) the wipe is
        permanent, exactly like before.
        """
        records = nova.tasks.clear_snapshot()
        nova.persist()
        if not records:
            return {"status": "cleared", "deleted": 0}
        token = uuid4().hex
        _task_clear_snapshots[token] = {
            "records": records,
            "expires": time.time() + _TASK_UNDO_WINDOW_SECONDS,
        }
        return {
            "status": "cleared",
            "deleted": len(records),
            "undo": {"token": token, "window_seconds": _TASK_UNDO_WINDOW_SECONDS},
        }

    @app.post("/tasks/clear/undo")
    async def tasks_clear_undo(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Undo a recent /tasks/clear by token (one-shot; unknown/expired → 409)."""
        token = str(payload.get("token", ""))
        snapshot = _task_clear_snapshots.pop(token, None)
        if snapshot is None or time.time() > float(snapshot["expires"]):
            _task_clear_snapshots.pop(token, None)  # expired leftover: sweep it
            raise HTTPException(status_code=409, detail="Undo window has closed.")
        restored = nova.tasks.restore(snapshot["records"])
        nova.persist()
        return {"status": "restored", "restored": restored}

    @app.post("/improve")
    async def improve(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.self_improvement.plan(str(payload["goal"])).to_dict()

    @app.post("/improve/workflow")
    async def improve_workflow(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.improvement_workflow(str(payload["goal"]))

    @app.post("/improve/preview")
    async def improve_preview(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.preview_improvement_workflow(str(payload["goal"]))

    @app.post("/improve/approve")
    async def improve_approve(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        return await nova.approve_improvement_workflow(
            str(payload["goal"]),
            preview_id=str(payload.get("preview_id") or "") or None,
        )

    @app.post("/learn")
    async def learn(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        return await nova.learning_cycle(
            str(payload["goal"]),
            feedback=str(payload.get("feedback", "")),
        )

    @app.post("/knowledge/teach")
    async def knowledge_teach(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Teach: persist a typed fact as durable, recallable knowledge."""
        try:
            return await nova.teach_knowledge(str(payload["fact"]))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/knowledge")
    async def knowledge_list(query: str = "", _principal: str = Depends(require_auth)) -> dict[str, Any]:
        return await nova.list_knowledge(query)

    @app.post("/knowledge/recall")
    async def knowledge_recall(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        return await nova.list_knowledge(str(payload.get("query", "")), limit=int(payload.get("limit", 20)))

    @app.post("/train")
    async def train(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        return await nova.autonomous_learning_loop(
            str(payload["goal"]),
            iterations=int(payload.get("iterations", 3)),
            feedback=str(payload.get("feedback", "")),
        )

    @app.post("/plan")
    async def plan(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Plan a goal: steps, tools, effects, dependencies and how each is checked.

        The planner is the application's own compiler (Phase 4), so a plan here
        and a plan the agent loop follows are the same plan. ``execute`` walks
        it; steps that change something still need confirmation, and this
        endpoint cannot grant it — use ``/plan/run`` to approve steps explicitly.
        """
        workflow_plan = nova.plan_for(str(payload["goal"]))
        response: dict[str, Any] = {"plan": workflow_plan.to_dict()}
        if payload.get("execute"):
            workflow = await nova.workflow_executor.execute(workflow_plan)
            response["workflow"] = workflow.to_dict()
            response["state"] = workflow.state()
        return response

    @app.post("/plan/run")
    async def plan_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Run a goal end to end through the agent loop, and say what was proven.

        ``approved`` names the steps this caller authorizes; nothing else may
        run a step that needs confirmation. The response carries the phase
        trace, the plan, the workflow and — deliberately — the steps that could
        NOT be verified.
        """
        goal = str(payload.get("goal", "")).strip()
        if not goal:
            raise HTTPException(status_code=422, detail="A goal is required.")
        approved = payload.get("approved") or ()
        if not isinstance(approved, list | tuple):
            raise HTTPException(status_code=422, detail="approved must be a list of step ids.")
        return await nova.run_plan(goal, approved=[str(step_id) for step_id in approved])

    @app.post("/plan/code")
    async def plan_code(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Code-aware planning: language-aware steps + a drafted code artifact.

        Uses the configured LLM when one is wired; otherwise returns the
        deterministic language-aware plan so the Build tab always plans real
        coding work.
        """
        try:
            return await nova.build_code_plan(
                str(payload["goal"]), language=str(payload.get("language", "python"))
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/build/project")
    async def build_project(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Draft a small multi-file project with the coding agent.

        Uses the configured model when available; without one it falls back
        to a deterministic, sandbox-verified scaffold (mode still
        `code_project`, `generated_by` = "scaffold"). Returns the whole
        file map + agent trace.
        """
        try:
            return await nova.build_code_project(
                str(payload["goal"]), language=str(payload.get("language", "python"))
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/build/artifacts")
    async def build_artifacts(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Saved workspace artifacts (newest first) for the Run list."""
        return nova.list_workspace_artifacts()

    @app.post("/build/run")
    async def build_run(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Re-execute a saved workspace artifact in the sandbox."""
        try:
            return await nova.run_saved_artifact(str(payload.get("filename", "")))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/build/save")
    async def build_save(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Save a drafted Build artifact to the workspace on disk.

        Closes the Build loop: draft (LLM or plan) → edit in the panel → save
        to build_workspace/<name> (gitignored). Returns the absolute path.
        """
        try:
            return nova.save_build_artifact(
                filename=str(payload.get("filename", "")),
                content=str(payload.get("content", "")),
                language=str(payload.get("language", "python")),
                goal=str(payload.get("goal", "")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/command/plan")
    async def command_plan(payload: CommandPlanRequest, _principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.plan_command(payload.command)

    @app.post("/command/execute")
    async def command_execute(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        token = str(payload.get("approval_token", "") or "") or None
        try:
            return await nova.execute_command(
                str(payload["command"]), approval_token=token, correlation_id=str(payload.get("correlation_id", "") or "")
            )
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/desktop/plan")
    async def desktop_plan(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.plan_desktop_command(str(payload["command"]))

    @app.post("/desktop/execute")
    async def desktop_execute(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        token = str(payload.get("approval_token", "") or "") or None
        try:
            return await nova.execute_desktop_command(
                str(payload["command"]), approval_token=token, correlation_id=str(payload.get("correlation_id", "") or "")
            )
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/browser/plan")
    async def browser_plan(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Plan an approval-gated browser action (navigate / search + extract).

        Dedicated device endpoint — same contract as /desktop/plan: a plan with
        an approval token, so a browser phrase can never land on the desktop
        controller (and vice versa) regardless of intent classification.
        """
        return nova.plan_browser_command(str(payload["command"]))

    @app.post("/browser/execute")
    async def browser_execute(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        token = str(payload.get("approval_token", "") or "") or None
        try:
            return await nova.execute_browser_command(
                str(payload["command"]), approval_token=token, correlation_id=str(payload.get("correlation_id", "") or "")
            )
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.get("/phone/status")
    async def phone_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.phone.status().to_dict()

    # ── Vision tab + bug log ─────────────────────────────────────────
    @app.get("/vision/status")
    async def vision_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Vision capability report: model availability + open bug count."""
        return {
            "vision_model": nova.vision.has_vision_model,
            "vision_llm": nova.vision_llm_status(),
            "open_bugs": nova.bug_log.open_count(),
            "bugs_path": str(nova.bug_log.path),
        }

    @app.post("/vision/model")
    async def vision_model_set(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Install a multimodal vision model (hot swap, no restart).

        provider=ollama probes the local Ollama for a vision model (llava,
        llama3.2-vision, …); provider=openai|gemini|openrouter uses the same
        API-key shape as the chat cloud LLM. The key is stored only on this
        machine (data/novacontrol-state.json vision_llm namespace, gitignored)
        and never returned by any endpoint.
        """
        try:
            return nova.set_vision_llm(
                str(payload.get("provider", "")),
                str(payload.get("api_key", "")),
                model=str(payload.get("model", "")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/vision/model/clear")
    async def vision_model_clear(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Remove the configured vision model; element location returns to OCR-only."""
        return nova.clear_vision_llm()

    @app.post("/vision/describe")
    async def vision_describe(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Capture the screen and describe it (vision model or window probe)."""
        return await nova.vision.describe_screen()

    @app.post("/vision/click")
    async def vision_click(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Vision-locate a labeled element on screen, click it, verify."""
        label = str(payload.get("label", "")).strip()
        if not label:
            raise HTTPException(status_code=422, detail="A 'label' is required.")
        try:
            return await nova.vision.guided_click(label)
        except ValueError as exc:
            # Command-shaped labels and empty plans are user-fixable errors.
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/intelligence")
    async def intelligence_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Global Intelligence Layer health: interpretation telemetry (including
        per-layer latency, per-stage duration, request memory and routing and
        escalation counts), the self-improvement findings it produced, the
        capability registry, and the live confidence thresholds the routing
        policy is currently using.

        The MODEL half joins them here (Phase 7): the capability table with the
        provenance of every claim, the lifecycle policy in force, and what the
        manager has actually done — loads, evictions, refusals, switching
        latency. The runtime probe runs in a worker thread, because this endpoint
        is reachable from the UI and a status call must never stall the loop.
        """
        return {
            "telemetry": nova.intelligence.telemetry.to_dict(),
            "findings": nova.intelligence.telemetry.improvement_findings(),
            "capabilities": nova.intelligence.capabilities.to_dict(),
            "thresholds": nova.intelligence.thresholds.to_dict(),
            "models": {
                **nova.models_status(),
                "runtime": await asyncio.to_thread(nova.model_manager.health),
                "hardware": await asyncio.to_thread(
                    nova.model_manager.monitor.headroom_report
                ),
                "selection": await asyncio.to_thread(nova._model_routing_report),
            },
            "lexical": {"exemplars": nova.intelligence.lexical.size},
            # What the optional embedding layer is (a backend, an index size) and
            # how it has actually been used (cache hits), so "semantic matching"
            # is a measurable part of the status rather than a claim.
            "semantic": nova.intelligence.semantic.to_dict(),
        }

    @app.get("/bugs")
    async def list_bugs(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.bug_log.to_dict()

    @app.post("/bugs/{bug_id}/fix")
    async def fix_bug(bug_id: str, _principal: str = Depends(require_auth)) -> dict[str, Any]:
        record = nova.bug_log.mark_fixed(bug_id)
        if record is None:
            raise HTTPException(status_code=404, detail="No such bug.")
        return record.to_dict()

    @app.post("/bugs/clear-fixed")
    async def clear_fixed_bugs(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return {"removed": nova.bug_log.clear_fixed()}

    @app.post("/phone/connect")
    async def phone_connect(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Run the phone bridge pairing flow (starts ADB, probes for devices).

        Read-only toward the device: the phone user still has to accept the RSA
        authorization prompt; this only makes the prompt appear.
        """
        return nova.phone.connect().to_dict()

    @app.post("/phone/plan")
    async def phone_plan(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.plan_phone_command(str(payload["command"]))

    @app.post("/phone/execute")
    async def phone_execute(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        token = str(payload.get("approval_token", "") or "") or None
        try:
            return await nova.execute_phone_command(
                str(payload["command"]), approval_token=token, correlation_id=str(payload.get("correlation_id", "") or "")
            )
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/agent/run")
    async def agent_run(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Run the full agentic loop (interpret → plan → perceive → act → verify → recover)."""
        return await nova.run_agentic_task(str(payload["request"]))

    @app.get("/agent/metrics")
    async def agent_metrics(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.agentic_metrics()

    @app.get("/agent/knowledge")
    async def agent_knowledge(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.agentic_knowledge()

    @app.post("/brain/decide")
    async def brain_decide(payload: BrainDecideRequest, _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Trace an utterance through the routing gates with a rung preview.

        The routing explorer in the web UI posts here while the server is up:
        the response carries the landing intent plus the full gate trace and a
        small preview of what the user would actually see (scratch answer text,
        research headline, or plan outline). The UI's embedded mirror is only a
        fallback for when this server is unreachable.
        """
        routed = nova.brain.route_utterance(payload.text)
        # The trace is the contract; a preview failure must never break it.
        try:
            routed["preview"] = _routing_preview(nova, payload.text, routed["intent"])
        except Exception as exc:
            routed["preview"] = {"kind": "info", "text": f"Preview unavailable: {type(exc).__name__}"}
        return routed

    @app.get("/explore/trending")
    async def explore_trending(count: int = 6, _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Current daily research topics from live top-story news.

        The pool refetches at most every 30 minutes and is invalidated by day;
        the visible window rotates hourly, so the Explore panel offers a
        different slice of what's happening in the world each visit — never a
        hardcoded list. ``exclude`` (comma-separated) lets the UI hide topics
        already shown elsewhere on the page.
        """
        bounded = max(1, min(count, 12))
        return trending.topics(count=bounded)

    @app.post("/explore")
    async def explore(payload: ExploreRequest_, _principal: str = Depends(require_auth)) -> dict[str, Any]:
        last_topic = payload.last_topic or None
        prior_topics = tuple(payload.prior_topics)
        if last_topic and not prior_topics:
            prior_topics = (last_topic,)
        report = await nova.explore.research(
            ExploreRequest(
                topic=payload.topic,
                depth=payload.depth,
                include_videos=payload.include_videos,
                max_sources=payload.max_sources,
                max_videos=payload.max_videos,
                prior_topics=prior_topics,
                # Run-scoped correlation: the client's id comes back on every
                # explore.progress frame so concurrent researches interleave
                # cleanly in the UI. Blank → the request mints its own.
                id=str(getattr(payload, "correlation_id", "") or "") or uuid4().hex,
            )
        )
        # The service announces explore.completed on the bus for live tabs; the
        # journal record happens here where the request (and its topic) lives.
        nova.activity.record("research", "Research complete", payload.topic)
        return report.to_dict()

    @app.get("/activity")
    async def activity(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Recent completed actions, newest first — seeds the web timeline."""
        return {"activity": nova.activity.recent()}

    @app.get("/events/stream")
    async def events_stream(
        token: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> Any:
        """Single live activity channel: relay the application EventBus as SSE.

        The web UI keeps ONE EventSource open here and receives every bus event
        (explore.progress research steps, command.progress action lines, ...)
        while an operation is running. Browser EventSource cannot set an
        Authorization header, so when token auth is enabled the token is accepted
        as a query parameter (`?token=`) as well as via the header for non-browser
        clients.
        """
        from fastapi.responses import StreamingResponse
        import asyncio
        import json

        result = authenticator.authenticate(authorization)
        if not result.authenticated and token:
            result = authenticator.authenticate(f"Bearer {token}")
        if not result.authenticated:
            raise HTTPException(status_code=401, detail=result.reason)

        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=256)

        async def forward(event: Event) -> None:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow client: drop the event rather than stall the bus

        async def stream() -> Any:
            await nova.event_bus.subscribe("*", forward)
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"  # comment frame; ignored by EventSource
                        continue
                    yield sse_frame(event)
            finally:
                await nova.event_bus.unsubscribe("*", forward)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        })

    @app.get("/plugins")
    async def plugins(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return {"plugins": [], "marketplace_enabled": False}

    @app.websocket("/ws/events")
    async def events(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"type": "connected", "service": "novacontrol"})
        try:
            while True:
                message = await websocket.receive_json()
                if message.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                else:
                    await websocket.send_json({"type": "echo", "payload": message})
        except WebSocketDisconnect:
            return

    return app


# Module-level app for uvicorn: novacontrol.api.app:app
app = create_app()
