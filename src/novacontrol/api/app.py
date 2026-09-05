"""FastAPI application factory."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from novacontrol.api.auth import ApiTokenAuthenticator
from novacontrol.api.middleware import RateLimitMiddleware, RequestLoggingMiddleware, SecurityHeadersMiddleware
from novacontrol.api.models import ApiSurface
from novacontrol.application import NovaControlApplication
from novacontrol.core.events import Event, EventBus
from novacontrol.explore import ExploreRequest
from novacontrol.planning import PlanningEngine, WorkflowExecutor
from novacontrol.release import ReleaseHardeningChecker, RuntimePackageBuilder, SystemHealthMonitor
from novacontrol.settings import ApprovalMode


_ASSET_VERSION_MARKER = "__NC_ASSET_VERSION__"
# Visible UI build stamp in the sidebar footer; same content-derived version,
# shortened, so a stale cached page is instantly distinguishable from current UI.
_UI_VERSION_MARKER = "__NC_UI_VERSION__"


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


def create_app() -> Any:
    """Create the NovaControl API app."""
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
        from fastapi.responses import HTMLResponse
        from fastapi.staticfiles import StaticFiles
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError("FastAPI is not installed. Run `pip install -e .` first.") from exc

    class NoCacheStaticFiles(StaticFiles):
        """Static files that are never cached, so UI edits appear on the next load."""

        def file_response(self, path: str, stat_result: Any, scope: Any, **kwargs: Any) -> Any:
            response = super().file_response(path, stat_result, scope, **kwargs)
            response.headers.update(_never_cache_headers())
            return response

    authenticator = ApiTokenAuthenticator(os.getenv("NOVACONTROL_API_TOKEN"))
    api_surface = ApiSurface.default()
    nova = NovaControlApplication(data_dir=Path("data"))
    app = FastAPI(title="NovaControl", version="0.1.0")

    # Middleware (order matters: last added = first executed)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(RateLimitMiddleware, max_requests=120, window_seconds=60.0)

    static_dir = Path(__file__).resolve().parents[1] / "web" / "static"
    if static_dir.exists():
        app.mount("/static", NoCacheStaticFiles(directory=static_dir), name="static")

    @app.on_event("startup")
    async def startup() -> None:
        await nova.start()

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await nova.stop()

    async def require_auth(authorization: str | None = Header(default=None)) -> str:
        result = authenticator.authenticate(authorization)
        if not result.authenticated:
            raise HTTPException(status_code=401, detail=result.reason)
        return result.principal

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    async def web_app() -> Any:
        # Never store index.html, and inject the content-derived asset version
        # into every script/link URL so edits rekey assets without a manual bump.
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        version = _static_asset_version(static_dir)
        # The sidebar footer shows the same content-derived build id (shortened),
        # so a stale cached page is recognizable at a glance: refresh and compare.
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
        )
        nova.persist()
        return settings.to_dict()

    @app.post("/ask")
    async def ask(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        response = await nova.handle_request(str(payload["request"]))
        return response.to_dict()

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

    @app.post("/train")
    async def train(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        return await nova.autonomous_learning_loop(
            str(payload["goal"]),
            iterations=int(payload.get("iterations", 3)),
            feedback=str(payload.get("feedback", "")),
        )

    @app.post("/plan")
    async def plan(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        workflow_plan = PlanningEngine().create_plan(str(payload["goal"]))
        response: dict[str, Any] = {"plan": workflow_plan.to_dict()}
        if payload.get("execute"):
            response["workflow"] = (await WorkflowExecutor().execute(workflow_plan)).to_dict()
        return response

    @app.post("/command/plan")
    async def command_plan(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.plan_command(str(payload["command"]))

    @app.post("/command/execute")
    async def command_execute(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        token = str(payload.get("approval_token", "") or "") or None
        try:
            return await nova.execute_command(str(payload["command"]), approval_token=token)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/desktop/plan")
    async def desktop_plan(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.plan_desktop_command(str(payload["command"]))

    @app.post("/desktop/execute")
    async def desktop_execute(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        token = str(payload.get("approval_token", "") or "") or None
        try:
            return await nova.execute_desktop_command(str(payload["command"]), approval_token=token)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.get("/phone/status")
    async def phone_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.phone.status().to_dict()

    @app.post("/phone/plan")
    async def phone_plan(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.plan_phone_command(str(payload["command"]))

    @app.post("/phone/execute")
    async def phone_execute(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        return await nova.execute_phone_command(str(payload["command"]))

    @app.post("/explore")
    async def explore(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> dict[str, Any]:
        last_topic = str(payload.get("last_topic", "")) or None
        prior_topics = tuple(payload.get("prior_topics", ())) if isinstance(payload.get("prior_topics"), list) else ()
        if last_topic and not prior_topics:
            prior_topics = (last_topic,)
        report = await nova.explore.research(
            ExploreRequest(
                topic=str(payload["topic"]),
                depth=str(payload.get("depth", "deep")),
                include_videos=bool(payload.get("include_videos", True)),
                max_sources=int(payload.get("max_sources", 6)),
                max_videos=int(payload.get("max_videos", 5)),
                prior_topics=prior_topics,
            )
        )
        return report.to_dict()

    @app.post("/explore/stream")
    async def explore_stream(payload: dict[str, Any], _principal: str = Depends(require_auth)) -> Any:
        """Run Explore and stream progress events via Server-Sent Events."""
        from fastapi.responses import StreamingResponse
        import asyncio
        import json

        topic = str(payload["topic"])
        last_topic = str(payload.get("last_topic", "")) or None
        prior_topics = tuple(payload.get("prior_topics", ())) if isinstance(payload.get("prior_topics"), list) else ()
        if last_topic and not prior_topics:
            prior_topics = (last_topic,)
        progress_bus = EventBus()
        # Temporarily attach the progress bus to the explore service
        original_bus = nova.explore._event_bus
        nova.explore._event_bus = progress_bus

        async def stream():
            events: list[dict[str, Any]] = []
            lock = asyncio.Lock()

            async def capture(event: Event) -> None:
                async with lock:
                    events.append({"type": event.type, "step": event.payload.get("step", ""), "detail": event.payload.get("detail", "")})

            await progress_bus.subscribe("explore.progress", capture)
            try:
                research_task = asyncio.create_task(nova.explore.research(
                    ExploreRequest(
                        topic=topic,
                        depth=str(payload.get("depth", "deep")),
                        include_videos=bool(payload.get("include_videos", True)),
                        max_sources=int(payload.get("max_sources", 6)),
                        max_videos=int(payload.get("max_videos", 5)),
                        prior_topics=prior_topics,
                    )
                ))
                # Stream progress events as they arrive
                last_index = 0
                while not research_task.done():
                    await asyncio.sleep(0.1)
                    async with lock:
                        new_events = events[last_index:]
                        last_index = len(events)
                    for evt in new_events:
                        yield f"event: progress\ndata: {json.dumps(evt)}\n\n"
                # Get the result (re-raises exceptions)
                report = research_task.result()
                # Stream any remaining events
                async with lock:
                    for evt in events[last_index:]:
                        yield f"event: progress\ndata: {json.dumps(evt)}\n\n"
                yield f"event: complete\ndata: {json.dumps(report.to_dict())}\n\n"
            except Exception as exc:
                yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
            finally:
                nova.explore._event_bus = original_bus

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
