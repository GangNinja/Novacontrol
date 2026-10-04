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
from novacontrol.optimization.models import PrivacyAction
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


def _csv(value: str) -> tuple[str, ...]:
    """A comma-separated query parameter as a tuple, with no empty entries.

    Query strings carry lists as text; parsing them in ONE place keeps every
    endpoint's idea of "a list of names" the same as the next one's.
    """
    return tuple(item.strip() for item in str(value or "").split(",") if item.strip())


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
    # Phase 14.3: headline topics come from outside the machine, so the pool
    # obeys the same allow_external_search control Explore does — behind the
    # policy's ONE decision rather than a second check of its own.
    trending = TrendingTopicsProvider(
        external_allowed=lambda: nova.privacy.allows(PrivacyAction.EXTERNAL_SEARCH)
    )
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
        from fastapi.middleware.gzip import GZipMiddleware
        from fastapi.responses import HTMLResponse
        from fastapi.staticfiles import StaticFiles
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError("FastAPI is not installed. Run `pip install -e .` first.") from exc

    def _training_id(payload: dict[str, Any], key: str = "run_id") -> str:
        """The identifier a training action needs, or a 422 that names the field."""
        value = str(payload.get(key, "")).strip()
        if not value:
            raise HTTPException(status_code=422, detail=f"{key} is required.")
        return value

    def _is_missing(result: dict[str, Any]) -> bool:
        """Whether a refusal is really a 404 (that thing does not exist)."""
        reason = str(result.get("reason", ""))
        return reason.startswith("no run ") or reason.startswith("no dataset version")

    def _training_refused(
        result: dict[str, Any], what: str, *, status_code: int = 409
    ) -> dict[str, Any]:
        """A refused training action is its own HTTP status, never a silent 200.

        The body the manager returns already names the reason ("a completed run
        cannot be cancelled", "approval requires a recorded passing evaluation"),
        and that reason is what the caller needs to read — so it is passed through
        as the detail rather than replaced with a generic message.
        """
        if not result.get("ok"):
            raise HTTPException(
                status_code=status_code,
                detail=str(result.get("reason", f"the {what} was refused")),
            )
        return result

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
        try:
            retention_days = int(payload["audit_retention_days"]) if "audit_retention_days" in payload else None
            max_records = int(payload["audit_max_records"]) if "audit_max_records" in payload else None
            evaluation_retention_days = (
                int(payload["evaluation_retention_days"])
                if "evaluation_retention_days" in payload
                else None
            )
            evaluation_max_records = (
                int(payload["evaluation_max_records"])
                if "evaluation_max_records" in payload
                else None
            )
            training_max_checkpoints = (
                int(payload["training_max_checkpoints"])
                if "training_max_checkpoints" in payload
                else None
            )
            training_retention_days = (
                int(payload["training_retention_days"])
                if "training_retention_days" in payload
                else None
            )
            training_max_records = (
                int(payload["training_max_records"])
                if "training_max_records" in payload
                else None
            )
            preference_max_checkpoints = (
                int(payload["preference_max_checkpoints"])
                if "preference_max_checkpoints" in payload
                else None
            )
            preference_retention_days = (
                int(payload["preference_retention_days"])
                if "preference_retention_days" in payload
                else None
            )
            preference_max_records = (
                int(payload["preference_max_records"])
                if "preference_max_records" in payload
                else None
            )
            rlhf_max_checkpoints = (
                int(payload["rlhf_max_checkpoints"])
                if "rlhf_max_checkpoints" in payload
                else None
            )
            rlhf_retention_days = (
                int(payload["rlhf_retention_days"])
                if "rlhf_retention_days" in payload
                else None
            )
            rlhf_max_records = (
                int(payload["rlhf_max_records"])
                if "rlhf_max_records" in payload
                else None
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail="Retention must be a whole number."
            ) from exc
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
            automation_enabled=bool(payload["automation_enabled"])
            if "automation_enabled" in payload
            else None,
            audit_retention_days=retention_days,
            audit_max_records=max_records,
            audit_redact_sensitive=bool(payload["audit_redact_sensitive"])
            if "audit_redact_sensitive" in payload
            else None,
            execution_mode=str(payload["execution_mode"]) if "execution_mode" in payload else None,
            privacy_allow_cloud=bool(payload["privacy_allow_cloud"])
            if "privacy_allow_cloud" in payload
            else None,
            privacy_allow_external_search=bool(payload["privacy_allow_external_search"])
            if "privacy_allow_external_search" in payload
            else None,
            privacy_allow_external_tools=bool(payload["privacy_allow_external_tools"])
            if "privacy_allow_external_tools" in payload
            else None,
            privacy_allow_telemetry=bool(payload["privacy_allow_telemetry"])
            if "privacy_allow_telemetry" in payload
            else None,
            privacy_allow_remote_model=bool(payload["privacy_allow_remote_model"])
            if "privacy_allow_remote_model" in payload
            else None,
            evaluation_enabled=bool(payload["evaluation_enabled"])
            if "evaluation_enabled" in payload
            else None,
            evaluation_retention_days=evaluation_retention_days,
            evaluation_max_records=evaluation_max_records,
            training_enabled=bool(payload["training_enabled"])
            if "training_enabled" in payload
            else None,
            training_dry_run=bool(payload["training_dry_run"])
            if "training_dry_run" in payload
            else None,
            training_max_checkpoints=training_max_checkpoints,
            training_retention_days=training_retention_days,
            training_max_records=training_max_records,
            preference_enabled=bool(payload["preference_enabled"])
            if "preference_enabled" in payload
            else None,
            preference_dry_run=bool(payload["preference_dry_run"])
            if "preference_dry_run" in payload
            else None,
            preference_max_checkpoints=preference_max_checkpoints,
            preference_retention_days=preference_retention_days,
            preference_max_records=preference_max_records,
            rlhf_enabled=bool(payload["rlhf_enabled"])
            if "rlhf_enabled" in payload
            else None,
            rlhf_dry_run=bool(payload["rlhf_dry_run"])
            if "rlhf_dry_run" in payload
            else None,
            rlhf_max_checkpoints=rlhf_max_checkpoints,
            rlhf_retention_days=rlhf_retention_days,
            rlhf_max_records=rlhf_max_records,
        )
        # The audit logger holds its own copy of the retention policy, so a changed
        # setting is re-applied here rather than waiting for the next restart.
        nova.apply_audit_settings()
        # Phase 14: the same round-trip re-derives the privacy policy from the
        # settings and re-points the ONE cloud switch, so a mode change is live.
        await nova.apply_privacy_settings()
        # Phase 15: and re-points the trajectory recorder at the operator's
        # switch, so "stop recording" takes effect on this request rather than
        # on the next restart.
        nova.apply_evaluation_settings()
        # Phase 16: and the training defaults (the dry-run switch, the
        # checkpoint cap), so "do not train on this machine" is live too.
        nova.apply_training_settings()
        # Phase 17: and the preference defaults — the same two switches, for the
        # DPO/ORPO subsystem, applied without waiting for a restart.
        nova.apply_preference_settings()
        # Phase 18: and the RLHF/RLAIF defaults — the same two switches, for the
        # feedback-as-reward subsystem, applied without waiting for a restart.
        nova.apply_rlhf_settings()
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

    @app.get("/capabilities")
    async def capability_inventory(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Every capability this installation has, and whether it can run now.

        The whole projection — declared verbs, tools projected from the
        catalogue, and the plan actions this application carries out — so "what
        can you do?" is answered from ONE place instead of three.
        """
        return nova.capabilities.to_dict(include_projected=True)

    @app.get("/capabilities/discover")
    async def capability_discovery(
        query: str = "",
        intent: str = "",
        category: str = "",
        tools: str = "",
        models: str = "",
        include_unavailable: bool = False,
        limit: int = 8,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Phase 9.3: what capabilities are available for this task?

        A RANKING with its evidence, never an execution: discovery says what
        could carry the work out and why, and an unavailable capability is
        reported with the reason (a missing model, a missing tool) rather than
        quietly left out unless the caller asks for it.
        """
        matches = nova.capabilities.discover(
            query,
            intent=intent or None,
            tools=_csv(tools),
            models=_csv(models),
            categories=_csv(category),
            include_unavailable=include_unavailable,
            limit=max(0, limit),
        )
        return {
            "query": query,
            "count": len(matches),
            "matches": [match.to_dict() for match in matches],
            "registry": nova.capabilities.report(),
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

    # ── Phase 13: scheduled automations and the audit trail ─────────────────
    #
    # Scheduling stores a request and its schedule; it runs nothing. Execution
    # still travels intent → decision → plan → permission → execution, whether a
    # run is due or asked for now, so these endpoints cannot become a second way
    # to make the machine act. The audit routes only read and bound the local
    # trail — they never reach a model or the network.

    def _automation_id(payload: dict[str, Any]) -> str:
        automation_id = str(payload.get("automation_id", "")).strip()
        if not automation_id:
            raise HTTPException(status_code=422, detail="automation_id is required.")
        return automation_id

    @app.get("/automation")
    async def automation_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """What is scheduled, what is armed, and when the next run happens."""
        return nova.automation_status()

    @app.post("/automation/schedule")
    async def automation_schedule(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Store a request plus the schedule its wording states.

        ``authorize`` is the caller saying it has approved the task; without it
        the automation is stored pending and stays disarmed until
        ``/automation/approve``, which is the only way it becomes runnable.
        """
        try:
            return nova.schedule_automation(
                str(payload["request"]),
                name=str(payload.get("name", "")),
                authorize=bool(payload.get("authorize", False)),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/automation/approve")
    async def automation_approve(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return nova.approve_automation(
                _automation_id(payload), by=str(payload.get("by", "operator"))
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/automation/cancel")
    async def automation_cancel(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return nova.cancel_automation(_automation_id(payload))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/automation/enable")
    async def automation_enable(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return nova.enable_automation(_automation_id(payload))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/automation/disable")
    async def automation_disable(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return nova.disable_automation(_automation_id(payload))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/automation/run")
    async def automation_run(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Run one stored automation now — the same gate a due run passes."""
        try:
            return await nova.run_automation(_automation_id(payload))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/automation/run-due")
    async def automation_run_due(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Run everything that is due, exactly as the scheduler's own tick would."""
        return {"runs": await nova.run_due_automations()}

    @app.get("/audit")
    async def audit_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.audit_status()

    @app.get("/audit/entries")
    async def audit_entries(limit: int = 20, _principal: str = Depends(require_auth)) -> dict[str, Any]:
        return {"entries": nova.audit_entries(limit)}

    @app.post("/audit/prune")
    async def audit_prune(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.audit_prune()

    @app.post("/audit/delete")
    async def audit_delete(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        record_id = str(payload.get("record_id", "")).strip()
        if not record_id:
            raise HTTPException(status_code=422, detail="record_id is required.")
        return nova.audit_delete(record_id)

    @app.post("/audit/clear")
    async def audit_clear(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.audit_clear()

    @app.get("/privacy")
    async def privacy_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The execution mode, the controls, and every outbound decision."""
        return nova.privacy_status()

    @app.post("/privacy")
    async def privacy_update(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Change the execution mode and/or a control, and apply it live."""
        return await nova.apply_privacy_settings(
            execution_mode=str(payload["execution_mode"])
            if "execution_mode" in payload
            else None,
            allow_cloud=bool(payload["allow_cloud"]) if "allow_cloud" in payload else None,
            allow_external_search=bool(payload["allow_external_search"])
            if "allow_external_search" in payload
            else None,
            allow_external_tools=bool(payload["allow_external_tools"])
            if "allow_external_tools" in payload
            else None,
            allow_telemetry=bool(payload["allow_telemetry"])
            if "allow_telemetry" in payload
            else None,
            allow_remote_model=bool(payload["allow_remote_model"])
            if "allow_remote_model" in payload
            else None,
        )

    @app.get("/resources")
    async def resources_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The governor's reading of this machine, with the reasons behind it."""
        return nova.resource_status()

    @app.post("/cost/estimate")
    async def cost_estimate(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Estimate a request's cost — a hint for routing, never a gate."""
        request = str(payload.get("request", "")).strip()
        if not request:
            raise HTTPException(status_code=422, detail="request is required.")
        return nova.estimate_cost(request)

    @app.get("/diagnostics")
    async def diagnostics(
        only: str = "", _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Run the component roster (or a comma-separated subset)."""
        names = [part.strip() for part in only.split(",") if part.strip()]
        try:
            return await nova.diagnostics_report(only=names or None)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/benchmark")
    async def benchmark_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Stored measurements and the measured comparison, per category."""
        return nova.benchmark_status()

    @app.post("/benchmark")
    async def benchmark_run(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Measure a model on the given tasks through the live provider."""
        model = str(payload.get("model", "")).strip()
        tasks = payload.get("tasks")
        category = str(payload.get("category", "")).strip() or "general"
        if not model:
            raise HTTPException(status_code=422, detail="model is required.")
        if not isinstance(tasks, list) or not tasks:
            raise HTTPException(status_code=422, detail="tasks must be a non-empty list.")
        try:
            return await nova.benchmark_model(
                model, [str(task) for task in tasks], category=category
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Phase 15: the evaluation layer's inspection surface. Read-only by
    # construction — nothing here runs a model, quotes a prompt, or writes a
    # row: it reports what was recorded, how it was scored, and what the
    # weighted reward made of it (with every factor that produced it).
    @app.get("/evaluation/summary")
    async def evaluation_summary(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """What has been recorded, how it was scored, and what is held."""
        return nova.evaluation_summary()

    @app.get("/evaluation/trajectory/{trajectory_id}")
    async def evaluation_trajectory(
        trajectory_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One run: its trajectory, its nine-dimension evaluation and its reward."""
        try:
            return nova.evaluation_trajectory(trajectory_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/evaluation/metrics")
    async def evaluation_metrics(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Aggregate figures over everything stored (success, latency, reward…)."""
        return nova.evaluation_metrics()

    @app.get("/evaluation/rewards")
    async def evaluation_rewards(
        limit: int = 20,
        min_total: float | None = None,
        max_total: float | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Stored rewards, newest first, optionally filtered by total."""
        return nova.evaluation_rewards(limit=limit, min_total=min_total, max_total=max_total)

    # Phase 16: supervised fine-tuning. Building a dataset or a run is data
    # work — it writes one row and estimates a cost — while starting a run is
    # the one operation on this surface that can occupy the machine: it is
    # refusal-first (an UNSAFE estimate is refused unless the DEPLOYMENT allows
    # an override), a real (non-dry-run) configuration additionally needs
    # confirmation, and the work happens in a worker thread so the API keeps
    # answering (and pause/cancel stay reachable) while it trains.
    @app.get("/training/status")
    async def training_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Datasets, runs, models, and what this machine can train with."""
        return nova.training_status()

    @app.get("/training/summary")
    async def training_summary(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The same, plus the newest datasets, runs and models by name."""
        return nova.training_summary()

    @app.post("/training/estimate")
    async def estimate_training(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Validate a training configuration and estimate it; trains nothing."""
        return nova.estimate_training(payload)

    @app.get("/training/datasets")
    async def training_datasets(
        dataset_type: str = "",
        name: str = "",
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Stored dataset versions, newest first, optionally filtered."""
        return nova.training_datasets(dataset_type=dataset_type, name=name, limit=limit)

    @app.post("/training/datasets")
    async def create_training_dataset(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Build one immutable dataset version from the recorded trajectories."""
        name = str(payload.get("name", "")).strip()
        dataset_type = str(payload.get("dataset_type", "")).strip()
        if not name or not dataset_type:
            raise HTTPException(status_code=422, detail="name and dataset_type are required.")
        rules = payload.get("rules")
        split = payload.get("split")
        tags = payload.get("tags")
        result = nova.create_training_dataset(
            name,
            dataset_type,
            rules=rules if isinstance(rules, dict) else None,
            split=split if isinstance(split, dict) else None,
            version=str(payload.get("version", "")),
            description=str(payload.get("description", "")),
            tags=[str(tag) for tag in tags] if isinstance(tags, (list, tuple)) else (),
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=422, detail=str(result.get("reason", "the dataset could not be built"))
            )
        return result

    @app.post("/training/datasets/validate")
    async def validate_training_dataset(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Check a stored dataset: ids, split references, leakage, statistics."""
        # Both spellings are accepted on purpose: the path parameter is named
        # `dataset_version_id` while the config field is `dataset_version`, and
        # a caller should not have to guess which one a body wants.
        dataset_version = str(
            payload.get("dataset_version_id") or payload.get("dataset_version") or ""
        ).strip()
        if not dataset_version:
            raise HTTPException(
                status_code=422, detail="dataset_version_id is required (name@version)."
            )
        return nova.validate_training_dataset(dataset_version)

    @app.get("/training/datasets/{dataset_version_id}")
    async def training_dataset(
        dataset_version_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One dataset version: its examples, splits, rules and statistics."""
        try:
            return nova.training_dataset(dataset_version_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/training/runs")
    async def training_runs(
        status: str = "",
        model: str = "",
        dataset_version: str = "",
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Stored training runs, newest first, optionally filtered."""
        return nova.training_runs(
            status=status, model=model, dataset_version=dataset_version, limit=limit
        )

    @app.post("/training/runs")
    async def create_training_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Validate a configuration, estimate it, and store a CREATED run."""
        model = str(payload.get("model", "")).strip()
        dataset_version = str(payload.get("dataset_version", "")).strip()
        if not model or not dataset_version:
            raise HTTPException(status_code=422, detail="model and dataset_version are required.")
        config = payload.get("config")
        result = nova.create_training_run(
            model,
            dataset_version,
            config=config if isinstance(config, dict) else None,
            name=str(payload.get("name", "")),
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=422, detail=str(result.get("reason", "the run could not be created"))
            )
        return result

    @app.post("/training/runs/start")
    async def start_training_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Start a run (dry-run by default) in a worker thread."""
        run_id = str(payload.get("run_id", "")).strip()
        if not run_id:
            raise HTTPException(status_code=422, detail="run_id is required.")
        result = await nova.start_training_run(
            run_id,
            override=bool(payload.get("override", False)),
            confirm=bool(payload.get("confirm", False)),
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=409,
                detail=str(result.get("reason", "the run could not be started")),
            )
        return result

    @app.post("/training/runs/pause")
    async def pause_training_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Ask a live run to stop at its next step; it stays resumable."""
        return _training_refused(nova.pause_training_run(_training_id(payload)), "pause")

    @app.post("/training/runs/cancel")
    async def cancel_training_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """End a run: a live one at its next step, a stored one immediately."""
        return _training_refused(
            nova.cancel_training_run(_training_id(payload)), "cancellation"
        )

    @app.post("/training/runs/resume")
    async def resume_training_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Continue a paused/failed/cancelled run from a loadable checkpoint."""
        run_id = str(payload.get("run_id", "")).strip()
        if not run_id:
            raise HTTPException(status_code=422, detail="run_id is required.")
        result = await nova.resume_training_run(
            run_id, checkpoint_id=str(payload.get("checkpoint_id", ""))
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=409,
                detail=str(result.get("reason", "the run could not be resumed")),
            )
        return result

    @app.post("/training/runs/re-estimate")
    async def estimate_training_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Re-estimate a stored run against the machine as it is NOW."""
        return _training_refused(
            nova.estimate_training_run(_training_id(payload)), "estimate", status_code=404
        )

    @app.post("/training/runs/evaluate")
    async def evaluate_training_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Compare the base model against the candidate on the held-out split.

        Without two predictors there is nothing to measure, and the run is
        recorded as unevaluated — a model is never approved on its loss curve.
        """
        run_id = str(payload.get("run_id", "")).strip()
        if not run_id:
            raise HTTPException(status_code=422, detail="run_id is required.")
        split = str(payload.get("split", "test")).strip() or "test"
        tolerance = payload.get("tolerance")
        result = await nova.evaluate_training_run(
            run_id,
            split=split,
            tolerance=float(tolerance) if isinstance(tolerance, (int, float)) else None,
        )
        # A comparison that measured NOTHING is a refusal, not a pass: the run is
        # recorded as unevaluated and the caller gets a non-2xx, exactly as the
        # CLI exits non-zero. A comparison that ran (pass, regress, even
        # inconclusive) is a 200 with its verdict in the body.
        return _training_refused(result, "evaluation", status_code=404 if _is_missing(result) else 409)

    @app.get("/training/runs/{run_id}/checkpoints")
    async def training_checkpoints(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Every checkpoint of one run, with its kind, size and loadability."""
        return nova.training_checkpoints(run_id)

    @app.get("/training/runs/{run_id}")
    async def training_run(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One run: its configuration, status, losses, checkpoints and estimate."""
        try:
            return nova.training_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/training/evaluations")
    async def training_evaluations(
        limit: int = 50, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Stored base-versus-candidate comparisons, newest first."""
        return nova.training_evaluations(limit=limit)

    @app.get("/training/models")
    async def training_models(
        status: str = "",
        base_model: str = "",
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Trained adapters/models by status; EXPERIMENTAL until approved."""
        return nova.training_models(status=status, base_model=base_model, limit=limit)

    @app.get("/training/models/{model_id}")
    async def training_model(
        model_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One trained model: its adapter metadata and evaluation history."""
        try:
            return nova.training_model(model_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/training/models/approve")
    async def approve_training_model(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Approve from recorded evidence only — never from training loss."""
        result = nova.approve_training_model(
            _training_id(payload, "model_id"),
            approved_by=str(payload.get("approved_by", "")),
            note=str(payload.get("note", "")),
        )
        return _training_refused(result, "approval")

    @app.post("/training/models/promote")
    async def promote_training_model(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Make an APPROVED model the production one; explicit, never automatic."""
        result = nova.promote_training_model(
            _training_id(payload, "model_id"), note=str(payload.get("note", ""))
        )
        return _training_refused(result, "promotion")

    @app.post("/training/models/reject")
    async def reject_training_model(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Reject a candidate; a rejected model is never promoted by accident."""
        result = nova.reject_training_model(
            _training_id(payload, "model_id"), reason=str(payload.get("reason", ""))
        )
        return _training_refused(result, "rejection")

    @app.post("/training/models/deprecate")
    async def deprecate_training_model(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Retire a production or approved model without deleting its record."""
        result = nova.deprecate_training_model(
            _training_id(payload, "model_id"), reason=str(payload.get("reason", ""))
        )
        return _training_refused(result, "deprecation")

    @app.post("/training/models/rollback")
    async def rollback_training_model(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Return production to the model this one replaced."""
        result = nova.rollback_training_model(
            _training_id(payload, "model_id"), reason=str(payload.get("reason", ""))
        )
        return _training_refused(result, "rollback")

    # ── Phase 17: preference optimization (DPO / ORPO) ─────────────────────────
    #
    # The same conventions as the training surface above: a refusal is its own
    # HTTP status rather than a 200 with ok=false, a missing thing is a 404, and
    # a real run is never started by a read. The registry operations are NOT
    # restated here — a preference model is registered in the SAME registry, so
    # approving, promoting and rolling one back are the /training/models routes.

    @app.get("/preference/status")
    async def preference_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Pair datasets, runs, the review queue and what this machine can do."""
        return nova.preference_status()

    @app.get("/preference/summary")
    async def preference_summary(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The same, plus the newest datasets and runs by name."""
        return nova.preference_summary()

    @app.get("/preference/algorithms")
    async def preference_algorithms(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The DPO and ORPO objectives, what each costs, and readiness here."""
        return nova.preference_algorithms()

    @app.post("/preference/estimate")
    async def estimate_preference(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Validate a preference configuration and price it; trains nothing."""
        return nova.estimate_preference(payload)

    @app.post("/preference/dry-run")
    async def dry_run_preference(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Validate the dataset, config, backend and output directory; start nothing."""
        config = payload.get("config")
        return nova.dry_run_preference(
            str(payload.get("model", "")),
            str(payload.get("dataset_version", payload.get("dataset_version_id", ""))),
            config=config if isinstance(config, dict) else None,
            algorithm=str(payload.get("algorithm", "")),
        )

    @app.get("/preference/datasets")
    async def preference_datasets(
        dataset_type: str = "",
        name: str = "",
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Stored preference dataset versions, newest first."""
        return nova.preference_datasets(dataset_type=dataset_type, name=name, limit=limit)

    @app.post("/preference/datasets")
    async def create_preference_dataset(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Build one immutable pair dataset version from what was recorded."""
        name = str(payload.get("name", "")).strip()
        dataset_type = str(payload.get("dataset_type", "")).strip()
        if not name or not dataset_type:
            raise HTTPException(status_code=422, detail="name and dataset_type are required.")
        rules = payload.get("rules")
        quality = payload.get("quality")
        split = payload.get("split")
        tags = payload.get("tags")
        queue = payload.get("queue_for_review")
        result = nova.create_preference_dataset(
            name,
            dataset_type,
            rules=rules if isinstance(rules, dict) else None,
            quality=quality if isinstance(quality, dict) else None,
            split=split if isinstance(split, dict) else None,
            version=str(payload.get("version", "")),
            description=str(payload.get("description", "")),
            tags=[str(tag) for tag in tags] if isinstance(tags, (list, tuple)) else (),
            queue_for_review=True if queue is None else bool(queue),
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=422,
                detail=str(result.get("reason", "the dataset could not be built")),
            )
        return result

    @app.post("/preference/datasets/validate")
    async def validate_preference_dataset(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Check a stored pair dataset: splits, leakage, provenance, quality."""
        dataset_version = str(
            payload.get("dataset_version_id", payload.get("dataset_version", ""))
        ).strip()
        if not dataset_version:
            raise HTTPException(
                status_code=422,
                detail="dataset_version_id is required (name@version).",
            )
        return nova.validate_preference_dataset(dataset_version)

    @app.get("/preference/datasets/{dataset_version_id}")
    async def preference_dataset(
        dataset_version_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One preference dataset version with its splits and statistics."""
        try:
            return nova.preference_dataset(dataset_version_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/preference/datasets/{dataset_version_id}/pairs/{preference_id}")
    async def preference_pair(
        dataset_version_id: str,
        preference_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """One pair: both candidates, the evidence, the outcomes, the split."""
        result = nova.preference_pair(dataset_version_id, preference_id)
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=str(result.get("reason", "no pair")))
        return result

    @app.get("/preference/reviews")
    async def preference_reviews(
        pending_only: bool = True,
        dataset_version: str = "",
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The review queue, with both candidates and the evidence side by side."""
        return nova.preference_reviews(
            pending_only=pending_only, limit=limit, dataset_version=dataset_version
        )

    @app.get("/preference/reviews/{preference_id}")
    async def preference_review(
        preference_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One queued pair as a reviewer sees it."""
        try:
            return nova.preference_review(preference_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/preference/reviews/submit")
    async def submit_preference_pair(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Record a preference a person is asserting, as a reviewed pair."""
        dataset_type = str(payload.get("dataset_type", "")).strip()
        prompt = payload.get("prompt")
        chosen = payload.get("chosen")
        rejected = payload.get("rejected")
        if not dataset_type or not isinstance(prompt, dict):
            raise HTTPException(
                status_code=422, detail="dataset_type and prompt are required."
            )
        if not isinstance(chosen, dict) or not isinstance(rejected, dict):
            raise HTTPException(
                status_code=422, detail="chosen and rejected must both be objects."
            )
        context = payload.get("context")
        chosen_outcome = payload.get("chosen_outcome")
        rejected_outcome = payload.get("rejected_outcome")
        tags = payload.get("tags")
        result = nova.submit_preference_pair(
            dataset_type=dataset_type,
            prompt=prompt,
            chosen=chosen,
            rejected=rejected,
            context=context if isinstance(context, dict) else None,
            chosen_outcome=chosen_outcome if isinstance(chosen_outcome, dict) else None,
            rejected_outcome=(
                rejected_outcome if isinstance(rejected_outcome, dict) else None
            ),
            reviewer=str(payload.get("reviewer", "")),
            reason=str(payload.get("reason", "")),
            confidence=float(payload.get("confidence", 1.0) or 1.0),
            group_key=str(payload.get("group_key", "")),
            tags=[str(tag) for tag in tags] if isinstance(tags, (list, tuple)) else (),
            enqueue=bool(payload.get("enqueue", False)),
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=422, detail=str(result.get("reason", "the pair was refused"))
            )
        return result

    @app.post("/preference/reviews/decide")
    async def decide_preference_review(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Settle a queued pair: choose A, choose B, mark a tie, or reject it."""
        result = nova.decide_preference_review(
            _training_id(payload, "preference_id"),
            str(payload.get("decision", "")),
            reviewer=str(payload.get("reviewer", "")),
            reason=str(payload.get("reason", "")),
        )
        if not result.get("ok"):
            reason = str(result.get("reason", "the decision was refused"))
            raise HTTPException(
                status_code=404 if reason.startswith("no queued pair") else 422,
                detail=reason,
            )
        return result

    @app.get("/preference/runs")
    async def preference_runs(
        algorithm: str = "",
        status: str = "",
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Preference runs, newest first, optionally filtered by objective."""
        return nova.preference_runs(algorithm=algorithm, status=status, limit=limit)

    @app.post("/preference/runs")
    async def create_preference_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Create a DPO/ORPO run from a config; never starts it."""
        model = str(payload.get("model", "")).strip()
        dataset_version = str(
            payload.get("dataset_version", payload.get("dataset_version_id", ""))
        ).strip()
        config = payload.get("config")
        if not dataset_version:
            raise HTTPException(
                status_code=422,
                detail="dataset_version is required (name@version).",
            )
        result = nova.create_preference_run(
            model,
            dataset_version,
            config=config if isinstance(config, dict) else None,
            name=str(payload.get("name", "")),
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=422, detail=str(result.get("reason", "the run could not be created"))
            )
        return result

    @app.post("/preference/runs/start")
    async def start_preference_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Start a preference run (dry-run by default) in a worker thread."""
        result = await nova.start_preference_run(
            _training_id(payload),
            override=bool(payload.get("override", False)),
            confirm=bool(payload.get("confirm", False)),
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=409,
                detail=str(result.get("reason", "the run could not be started")),
            )
        return result

    @app.post("/preference/runs/pause")
    async def pause_preference_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Pause a running preference run at its next step boundary."""
        return _training_refused(nova.pause_preference_run(_training_id(payload)), "pause")

    @app.post("/preference/runs/cancel")
    async def cancel_preference_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """End a run: a live one at its next step, a stored one immediately."""
        return _training_refused(
            nova.cancel_preference_run(_training_id(payload)), "cancellation"
        )

    @app.post("/preference/runs/resume")
    async def resume_preference_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Resume an interrupted run from its newest valid checkpoint."""
        result = await nova.resume_preference_run(
            _training_id(payload), checkpoint_id=str(payload.get("checkpoint_id", ""))
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=409,
                detail=str(result.get("reason", "the run could not be resumed")),
            )
        return result

    @app.post("/preference/runs/re-estimate")
    async def estimate_preference_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Re-price a stored run against the current machine reading."""
        return _training_refused(
            nova.estimate_preference_run(_training_id(payload)), "estimate", status_code=404
        )

    @app.post("/preference/runs/evaluate")
    async def evaluate_preference_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Compare base, SFT and candidate on the held-out pairs."""
        result = await nova.evaluate_preference_run(
            _training_id(payload),
            base=payload.get("base"),
            candidate=payload.get("candidate"),
            sft=payload.get("sft"),
            split=str(payload.get("split", "test")),
            tolerance=payload.get("tolerance"),
        )
        return _training_refused(
            result, "evaluation", status_code=404 if _is_missing(result) else 409
        )

    @app.post("/preference/compare")
    async def compare_preference_models(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Compare three models on a pair dataset, without needing a run."""
        dataset_version = str(
            payload.get("dataset_version", payload.get("dataset_version_id", ""))
        ).strip()
        base = payload.get("base")
        candidate = payload.get("candidate")
        if not dataset_version or base is None or candidate is None:
            raise HTTPException(
                status_code=422,
                detail="dataset_version, base and candidate are required.",
            )
        return nova.compare_preference_models(
            dataset_version,
            base=base,
            candidate=candidate,
            sft=payload.get("sft"),
            split=str(payload.get("split", "test")),
            tolerance=payload.get("tolerance"),
            algorithm=str(payload.get("algorithm", "")),
        )

    @app.get("/preference/runs/{run_id}/checkpoints")
    async def preference_checkpoints(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """A run's checkpoints with their validity and loadability."""
        return nova.preference_checkpoints(run_id)

    @app.get("/preference/runs/{run_id}")
    async def preference_run(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One preference run: algorithm, config, progress and checkpoints."""
        try:
            return nova.preference_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/preference/evaluations")
    async def preference_evaluations(
        limit: int = 50, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Stored comparisons with their readings and regression checks."""
        return nova.preference_evaluations(limit=limit)

    @app.get("/preference/models")
    async def preference_models(
        status: str = "",
        algorithm: str = "",
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Registered preference models — the same registry, filtered by objective."""
        return nova.preference_models(status=status, algorithm=algorithm, limit=limit)

    @app.get("/preference/models/{model_id}")
    async def preference_model(
        model_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One registered model, with the objective that produced it."""
        try:
            return nova.preference_model(model_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ── Phase 18: RLHF / RLAIF (feedback-as-reward training) ────────────────
    # Same discipline as /preference: reads do not start anything; a refused
    # action is its own HTTP status; a missing thing is a 404; real runs need
    # explicit confirmation and an unsafe override. RLHF/RLAIF is OFF by
    # default and never starts a real optimizer automatically.

    @app.get("/rlhf/status")
    async def rlhf_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """RLHF / RLAIF state: enabled, mode, feedback, ratings, datasets, runs."""
        return nova.rlhf_status()

    @app.get("/rlhf/summary")
    async def rlhf_summary(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The same, plus the newest datasets and runs by name."""
        return nova.rlhf_summary()

    @app.get("/rlhf/algorithms")
    async def rlhf_algorithms(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The RL modes and policy optimizers, and what this machine can do."""
        return nova.rlhf_algorithms()

    @app.post("/rlhf/estimate")
    async def estimate_rlhf(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Validate an RL configuration and price it; trains nothing."""
        raw = payload.get("config")
        cfg = raw if isinstance(raw, dict) else {}
        return nova.estimate_rlhf(cfg)

    @app.post("/rlhf/dry-run")
    async def dry_run_rlhf(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Validate, price, plan and simulate an RL run; starts nothing."""
        cfg = payload.get("config") if isinstance(payload.get("config"), dict) else None
        return nova.dry_run_rlhf(
            model=str(payload.get("model", "")),
            dataset_version=str(payload.get("dataset_version", "")),
            config=cfg,
            mode=str(payload.get("mode", "")),
            algorithm=str(payload.get("algorithm", "")),
        )

    @app.post("/rlhf/pipeline")
    async def rlhf_pipeline(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Build and report the RLHF/RLAIF pipeline plan; starts nothing."""
        cfg = payload.get("config") if isinstance(payload.get("config"), dict) else None
        return nova.rlhf_pipeline(
            config=cfg,
            dataset_version=str(payload.get("dataset_version", "")),
        )

    @app.get("/rlhf/feedback")
    async def rlhf_feedback(
        status: str = "",  # empty means every status; "pending" is not a status
        feedback_type: str = "",
        trajectory_id: str = "",
        pending_only: bool = True,
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """List human feedback rows (status / type / trajectory filters)."""
        return nova.rlhf_feedback(
            status=status,
            feedback_type=feedback_type,
            trajectory_id=trajectory_id,
            pending_only=pending_only,
            limit=limit,
        )

    @app.post("/rlhf/feedback")
    async def submit_rlhf_feedback(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Submit human feedback for a trajectory/action (never deletes)."""
        return nova.submit_rlhf_feedback(payload)

    @app.post("/rlhf/feedback/{feedback_id}/decide")
    async def decide_rlhf_feedback(
        feedback_id: str, payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Settle a held feedback row: accept keeps it usable, reject does not."""
        result = nova.decide_rlhf_feedback(
            feedback_id,
            str(payload.get("decision") or ""),
            reviewer=str(payload.get("reviewer") or ""),
            reason=str(payload.get("reason") or ""),
        )
        if not result.get("ok"):
            reason = str(result.get("reason", "the decision was refused"))
            raise HTTPException(
                status_code=404 if reason.startswith("no feedback") else 422,
                detail=reason,
            )
        return result

    @app.post("/rlhf/rate")
    async def rate_rlhf_subject(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Ask an evaluator for a structured rating of observable facts."""
        criteria = payload.get("criteria")
        return nova.rate_rlhf_subject(
            payload.get("subject") or {},
            evaluator=str(payload.get("evaluator") or "auto"),
            criteria=(
                [str(item) for item in criteria]
                if isinstance(criteria, (list, tuple))
                else ()
            ),
            save=bool(payload.get("save", True)),
        )

    @app.get("/rlhf/ratings")
    async def rlhf_ratings(
        trajectory_id: str = "",
        evaluator_id: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Stored AI ratings, newest first, with the source breakdown."""
        return nova.rlhf_ratings(
            trajectory_id=trajectory_id, evaluator_id=evaluator_id, limit=limit
        )

    @app.get("/rlhf/disagreements")
    async def rlhf_disagreements(
        detect: bool = False,
        kind: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Recorded human-vs-AI disagreements, optionally detecting new ones."""
        return nova.rlhf_disagreements(detect=detect, kind=kind, limit=limit)

    @app.get("/rlhf/datasets")
    async def rlhf_datasets(
        mode: str = "",
        name: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Reward dataset versions, newest first, optionally by mode or name."""
        return nova.rlhf_datasets(mode=mode, name=name, limit=limit)

    @app.post("/rlhf/datasets")
    async def create_rlhf_dataset(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Build one immutable reward dataset version from what was recorded."""
        name = str(payload.get("name") or "").strip()
        mode = str(payload.get("mode") or "").strip()
        if not name or not mode:
            raise HTTPException(
                status_code=422, detail="name and mode are required."
            )
        rules = payload.get("rules")
        split = payload.get("split")
        tags = payload.get("tags")
        result = nova.create_rlhf_dataset(
            name,
            mode=mode,
            rules=rules if isinstance(rules, dict) else None,
            split=split if isinstance(split, dict) else None,
            version=str(payload.get("version") or ""),
            description=str(payload.get("description") or ""),
            tags=[str(tag) for tag in tags]
            if isinstance(tags, (list, tuple))
            else (),
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=422,
                detail=str(
                    result.get("reason", "the reward dataset could not be built")
                ),
            )
        return result

    @app.get("/rlhf/datasets/{dataset_version_id}/validate")
    async def validate_rlhf_dataset(dataset_version_id: str, _principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.validate_rlhf_dataset(dataset_version_id)

    @app.get("/rlhf/datasets/{dataset_version_id}/held")
    async def rlhf_held(dataset_version_id: str, _principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.rlhf_held(dataset_version_id)

    @app.get("/rlhf/datasets/{dataset_version_id}")
    async def rlhf_dataset(dataset_version_id: str, _principal: str = Depends(require_auth)) -> dict[str, Any]:
        try:
            return nova.rlhf_dataset(dataset_version_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/rlhf/runs")
    async def rlhf_runs(
        mode: str = "",
        status: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """RLHF/RLAIF runs, newest first, optionally filtered by mode or status."""
        return nova.rlhf_runs(mode=mode, status=status, limit=limit)

    @app.post("/rlhf/runs")
    async def create_rlhf_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Validate an RL configuration, audit its rewards, and store it CREATED."""
        model = str(payload.get("model") or "").strip()
        dataset_version = str(
            payload.get("dataset_version") or payload.get("dataset_version_id") or ""
        ).strip()
        config = payload.get("config")
        if not dataset_version:
            raise HTTPException(
                status_code=422,
                detail="dataset_version is required (name@version).",
            )
        result = nova.create_rlhf_run(
            model,
            dataset_version,
            config=config if isinstance(config, dict) else None,
            name=str(payload.get("name") or ""),
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=422,
                detail=str(result.get("reason", "the run could not be created")),
            )
        return result

    @app.post("/rlhf/runs/start")
    async def start_rlhf_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Start an RL run in a worker thread (simulated/dry-run by default)."""
        result = await nova.start_rlhf_run(
            _training_id(payload),
            override=bool(payload.get("override", False)),
            confirm=bool(payload.get("confirm", False)),
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=409,
                detail=str(result.get("reason", "the run could not be started")),
            )
        return result

    @app.post("/rlhf/runs/pause")
    async def pause_rlhf_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Pause a running RL run at its next step boundary."""
        return _training_refused(nova.pause_rlhf_run(_training_id(payload)), "pause")

    @app.post("/rlhf/runs/cancel")
    async def cancel_rlhf_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """End an RL run: a live one at its next step, a stored one now."""
        return _training_refused(
            nova.cancel_rlhf_run(_training_id(payload)), "cancellation"
        )

    @app.post("/rlhf/runs/resume")
    async def resume_rlhf_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Resume an interrupted RL run from its newest valid checkpoint."""
        result = await nova.resume_rlhf_run(
            _training_id(payload),
            checkpoint_id=str(payload.get("checkpoint_id") or ""),
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=409,
                detail=str(result.get("reason", "the run could not be resumed")),
            )
        return result

    @app.post("/rlhf/runs/re-estimate")
    async def estimate_rlhf_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Re-price a stored RL run against the current machine reading."""
        return _training_refused(
            nova.estimate_rlhf_run(_training_id(payload)), "estimate", status_code=404
        )

    @app.post("/rlhf/runs/evaluate")
    async def evaluate_rlhf_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Compare base, SFT, preference and RL candidate on held-out data."""
        result = await nova.evaluate_rlhf_run(
            _training_id(payload),
            base=payload.get("base"),
            candidate=payload.get("candidate"),
            sft=payload.get("sft"),
            preference=payload.get("preference"),
            dataset_version=str(payload.get("dataset_version") or ""),
            split=str(payload.get("split") or "test"),
            tolerance=payload.get("tolerance"),
        )
        return _training_refused(
            result, "evaluation", status_code=404 if _is_missing(result) else 409
        )

    @app.post("/rlhf/compare")
    async def compare_rlhf_models(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Compare up to four models on a reward dataset, without needing a run."""
        dataset_version = str(
            payload.get("dataset_version") or payload.get("dataset_version_id") or ""
        ).strip()
        base = payload.get("base")
        candidate = payload.get("candidate")
        if not dataset_version or base is None or candidate is None:
            raise HTTPException(
                status_code=422,
                detail="dataset_version, base and candidate are required.",
            )
        return nova.compare_rlhf_models(
            dataset_version,
            base=base,
            candidate=candidate,
            sft=payload.get("sft"),
            preference=payload.get("preference"),
            split=str(payload.get("split") or "test"),
            tolerance=payload.get("tolerance"),
            run_id=str(payload.get("run_id") or ""),
        )

    @app.get("/rlhf/runs/{run_id}/checkpoints")
    async def rlhf_checkpoints(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """A run's checkpoints with their validity and loadability."""
        return nova.rlhf_checkpoints(run_id)

    @app.get("/rlhf/runs/{run_id}")
    async def rlhf_run(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One RL run: mode, algorithm, config, progress and checkpoints."""
        try:
            return nova.rlhf_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/rlhf/evaluations")
    async def rlhf_evaluations(
        limit: int = 50, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Stored RL comparisons with their readings and regression checks."""
        return nova.rlhf_evaluations(limit=limit)

    @app.get("/rlhf/models")
    async def rlhf_models(
        status: str = "",
        mode: str = "",
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Registered RL models — the same registry, filtered by mode."""
        return nova.rlhf_models(status=status, mode=mode, limit=limit)

    @app.get("/rlhf/models/{model_id}")
    async def rlhf_model(
        model_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One registered model, with the RL mode that produced it."""
        try:
            return nova.rlhf_model(model_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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
