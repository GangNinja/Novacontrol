"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import MutableMapping
from datetime import UTC, datetime
from os import PathLike
from pathlib import Path
from typing import Any
from uuid import uuid4

from starlette.requests import Request

from novacontrol.api.auth import ApiTokenAuthenticator
from novacontrol.api.middleware import (
    OriginGuardMiddleware,
    RateLimitMiddleware,
    RequestLoggingMiddleware,
    SecurityHeadersMiddleware,
)
from novacontrol.api.models import (
    ApiSurface,
    AskRequest,
    BrainDecideRequest,
    CommandPlanRequest,
    ExploreRequest_,
)
from novacontrol.application import NovaControlApplication
from novacontrol.core.events import Event
from novacontrol.explore import ExploreRequest
from novacontrol.explore.trending import TrendingTopicsProvider
from novacontrol.novaforge.knowledge import (
    KnowledgeEvidence,
    candidate_from_payload,
)
from novacontrol.novaforge import (
    AutonomyConflictError,
    BenchmarkConflictError,
    BenchmarkNotFoundError,
    BenchmarkValidationError,
    CoachError,
    CoachNotFoundError,
    EvaluationConflictError,
    EvaluationError,
    ExperimentConflictError,
    ExperimentError,
    ExperimentNotFoundError,
    GameNotFoundError,
    MissionValidationError,
    SessionConflictError,
    SessionTransitionError,
    TimelineQueryError,
)
from novacontrol.novamarket import ConfigurationError as NovaMarketConfigurationError
from novacontrol.novamarket import NovaMarketError
from novacontrol.optimization.models import PrivacyAction
from novacontrol.perception import DEFERRED_PHASES, PERCEPTION_SCHEMA_VERSION
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


def _market_http_error(exc: Exception) -> Any:
    """Map NovaMarket's typed errors onto honest HTTP statuses.

    A provider outage is 502 (an upstream failed), a bad symbol is 404, an
    unsupported interval is 422, and an uninitialised subsystem is 409. The
    distinction matters: a UI that shows every refusal as "400" teaches nobody
    what to change.
    """
    from fastapi import HTTPException

    if isinstance(exc, HTTPException):
        # A validation refusal raised by a handler (e.g. a malformed timestamp)
        # is already an HTTP answer; do not turn it into a 500.
        return exc

    from novacontrol.novamarket.contracts import (
        ConfigurationError,
        InstrumentNotFoundError,
        MarketDataError,
        MarketDataStoreError,
        NotOperationalError,
        ProviderError,
        ProviderSymbolNotFoundError,
        ProviderUnsupportedIntervalError,
    )

    if isinstance(exc, ProviderSymbolNotFoundError):
        status = 404
    elif isinstance(exc, ProviderUnsupportedIntervalError):
        status = 422
    elif isinstance(exc, NotOperationalError):
        status = 409
    elif isinstance(exc, ConfigurationError):
        status = 422
    elif isinstance(exc, InstrumentNotFoundError):
        status = 404
    elif isinstance(exc, MarketDataStoreError):
        status = 503
    elif isinstance(exc, ProviderError):
        status = 502
    elif isinstance(exc, MarketDataError):
        status = 400
    else:
        status = 500
    return HTTPException(status_code=status, detail=str(exc))


def _parse_market_datetime(value: str | None, field: str) -> datetime | None:
    from fastapi import HTTPException

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"{field} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise HTTPException(status_code=422, detail=f"{field} must include a timezone offset")
    return parsed.astimezone(UTC)


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


def _observation_batch(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The observations one ``/world/observe`` body carries — one or many.

    Accepts a single observation, an ``observations`` list, or a ``perception`` /
    ``scene`` payload folded into one observation. The batch shape exists because
    a caller replaying a capture has several observations and should not need one
    round trip per frame.
    """
    many = payload.get("observations")
    if isinstance(many, list) and many:
        shared = {key: value for key, value in payload.items() if key != "observations"}
        merged: list[dict[str, Any]] = []
        for item in many:
            if isinstance(item, dict):
                merged.append({**shared, **item})
        return merged or [payload]
    return [payload]


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
                "outline": [
                    str(a.get("type") or a.get("action") or "")
                    for a in device_plan.get("workflow", {}).get("actions", [])
                ][:8],
                "target": device_plan.get("target"),
                "summary": str(device_plan.get("summary", ""))[:200],
            }
        if intent == "chat":
            return {"kind": "info", "text": "Answered by the configured chat model."}
        return {"kind": "info", "text": f"Routed to {intent}. Open that panel to run it."}
    except Exception as exc:  # a broken preview must never break the trace
        return {"kind": "info", "text": f"Preview unavailable: {type(exc).__name__}"}


def create_app(*, data_dir: str | Path | None = None) -> Any:
    """Create the NovaControl API app.

    ``data_dir`` lets a caller (an acceptance harness, an embedded server) put
    the application's stores somewhere other than the process working
    directory's ``data`` folder. The default preserves the existing layout.
    """
    # Daily-updates provider for the Explore panel's topic suggestions: one
    # per app (cached + rotating), edition defaults to India.
    # Phase 14.3: headline topics come from outside the machine, so the pool
    # obeys the same allow_external_search control Explore does — behind the
    # policy's ONE decision rather than a second check of its own.
    trending = TrendingTopicsProvider(
        external_allowed=lambda: nova.privacy.allows(PrivacyAction.EXTERNAL_SEARCH)
    )
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.middleware.gzip import GZipMiddleware
        from fastapi.responses import FileResponse, HTMLResponse
        from fastapi.staticfiles import StaticFiles
        from starlette.middleware.trustedhost import TrustedHostMiddleware
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError("FastAPI is not installed. Run `pip install -e .` first.") from exc

    def _required_field(payload: dict[str, Any], key: str = "run_id") -> str:
        """A field the handler cannot work without, or a 422 that names it.

        A missing required field is the caller's mistake, not a server fault:
        reading it with ``payload[key]`` raised a KeyError and answered 500 with
        a traceback naming an internal field. Every handler that needs a field
        to do anything now gets 422 + that field's name, which is the contract
        the rest of this API already uses (``/plan/run``, ``/audit/delete``).
        """
        # A key present with a JSON null is as absent as a missing one:
        # str(None) is "None", which would travel on as a real value.
        raw = payload.get(key)
        value = "" if raw is None else str(raw).strip()
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

    api_token = os.getenv("NOVACONTROL_API_TOKEN")
    authenticator = ApiTokenAuthenticator(api_token)
    # AT-01: autonomy controls use a SEPARATE credential.  The ordinary API
    # bearer token authorizes the rest of NovaMarket; it must not authorize a
    # runtime start/stop merely because the caller can read the market API.  A
    # deployment that reuses the same value for both is refused closed rather
    # than pretending the permission is separate.
    operator_token = os.getenv("NOVACONTROL_AUTONOMY_OPERATOR_TOKEN")
    if operator_token and operator_token == api_token:
        operator_token = ""
    operator_authenticator = ApiTokenAuthenticator(operator_token)
    # The local admin token is accepted only from loopback clients.  It lets the
    # local Vite proxy open the desk without a per-tab prompt and acts as both
    # the ordinary API credential and the autonomy-operator credential.  Never
    # accept it from a remote address: a remote deployment still needs the
    # configured bearer/operator tokens.
    # The predictable local key is opt-in, never a built-in default: a
    # deployed backend reached through a loopback reverse proxy would
    # otherwise inherit a guessable admin credential.  The local launchers
    # (start.ps1) export NOVACONTROL_ADMIN_TOKEN so the Vite proxy keeps
    # auto-connecting; an unset value disables this credential entirely.
    admin_token = (os.getenv("NOVACONTROL_ADMIN_TOKEN") or "").strip()
    admin_authenticator = ApiTokenAuthenticator(admin_token)
    api_surface = ApiSurface.default()
    nova = NovaControlApplication(data_dir=Path(data_dir) if data_dir is not None else Path("data"))
    app = FastAPI(title="NovaControl", version="0.1.0")

    # Middleware (order matters: last added = first executed)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(RateLimitMiddleware, max_requests=120, window_seconds=60.0)
    # A separately hosted NovaMarket frontend is an explicit, opt-in origin.
    # The default is no cross-origin access; the standalone dev server uses a
    # same-origin Vite proxy, and production must name its exact origin here.
    cors_origins = [
        item.strip() for item in os.getenv("NOVAMARKET_CORS_ORIGINS", "").split(",") if item.strip()
    ]

    # Reject foreign Host headers.  Without this, a hostile DNS name can be
    # rebound to 127.0.0.1 and the browser will then treat the API as
    # same-origin.  The defaults are the loopback names the launcher serves;
    # NOVACONTROL_TRUSTED_HOSTS extends them only when a legitimate hostname is
    # actually used (and then the bearer token must be configured too).
    trusted_hosts = [
        item.strip()
        for item in os.getenv(
            "NOVACONTROL_TRUSTED_HOSTS", "127.0.0.1,localhost,::1,testserver"
        ).split(",")
        if item.strip()
    ]
    allowed_origins = sorted(
        {
            *cors_origins,
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://[::1]:5173",
            *(
                item.strip()
                for item in os.getenv("NOVAMARKET_ALLOWED_ORIGINS", "").split(",")
                if item.strip()
            ),
        }
    )
    app.add_middleware(OriginGuardMiddleware, allowed_origins=allowed_origins)

    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Autonomy-Operator-Token"],
        )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

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
        # The continuous NovaMarket monitor is an application lifecycle task,
        # not a request handler: it keeps the session and paper book watched
        # while the API is up, and its own pause flag is the only thing that
        # stops the monitoring work.
        nova.novamarket.start_runtime()

    @app.on_event("shutdown")
    async def shutdown() -> None:
        telemetry.stop()
        await nova.novamarket.stop_runtime()
        await nova.stop()

    def _is_loopback_request(request: Request) -> bool:
        host = (request.client.host if request.client else "").strip().lower()
        return host in {"127.0.0.1", "::1", "localhost"} or host.startswith("127.")

    def _admin_token_authorized(value: str | None) -> bool:
        if not admin_authenticator.enabled or not value:
            return False
        return admin_authenticator.authenticate(f"Bearer {value.strip()}").authenticated

    def _bearer_credential(authorization: str | None) -> str | None:
        if not authorization:
            return None
        scheme, separator, value = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer":
            return None
        return value.strip() or None

    def _local_admin_authorized(
        request: Request,
        operator_value: str | None = None,
        authorization: str | None = None,
    ) -> bool:
        """The admin key opens every local door from either credential header."""

        if not _is_loopback_request(request):
            return False
        return _admin_token_authorized(operator_value) or _admin_token_authorized(
            _bearer_credential(authorization)
        )

    async def require_auth(
        request: Request, authorization: str | None = Header(default=None)
    ) -> str:
        result = authenticator.authenticate(authorization)
        if not result.authenticated and _is_loopback_request(request):
            if _admin_token_authorized(_bearer_credential(authorization)):
                return "local-admin"
        if not result.authenticated:
            raise HTTPException(status_code=401, detail=result.reason)
        return result.principal

    def _autonomy_operator_authorized(
        value: str | None,
        request: Request,
        authorization: str | None = None,
    ) -> bool:
        if _local_admin_authorized(request, value, authorization):
            return True
        if not value:
            return False
        if not operator_authenticator.enabled:
            return False
        result = operator_authenticator.authenticate(f"Bearer {value.strip()}")
        return result.authenticated

    async def autonomy_operator_state(
        request: Request,
        x_autonomy_operator_token: str | None = Header(
            default=None, alias="X-Autonomy-Operator-Token"
        ),
        authorization: str | None = Header(default=None),
    ) -> bool:
        """Read-only permission marker for status/health responses."""

        return _autonomy_operator_authorized(
            x_autonomy_operator_token, request, authorization
        )

    async def require_autonomy_operator(
        request: Request,
        x_autonomy_operator_token: str | None = Header(
            default=None, alias="X-Autonomy-Operator-Token"
        ),
        authorization: str | None = Header(default=None),
    ) -> str:
        """Fail closed unless the dedicated autonomy-operator token is proven.

        The ordinary bearer token is deliberately not consulted here.  A
        missing server-side operator token is a configuration refusal, not an
        implicit local permission.
        """

        if _local_admin_authorized(
            request, x_autonomy_operator_token, authorization
        ):
            return "local-admin"
        if not operator_authenticator.enabled:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "autonomy_operator_not_configured",
                    "message": (
                        "Autonomy controls require NOVACONTROL_AUTONOMY_OPERATOR_TOKEN "
                        "on the server; ordinary API authentication is not sufficient."
                    ),
                },
            )
        result = operator_authenticator.authenticate(
            f"Bearer {x_autonomy_operator_token.strip()}"
            if x_autonomy_operator_token and x_autonomy_operator_token.strip()
            else None
        )
        if not result.authenticated:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "autonomy_operator_forbidden",
                    "message": "The dedicated autonomy-operator permission is required.",
                },
            )
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
    async def status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return {
            "name": "NovaControl",
            "status": "ok",
            "api": api_surface.to_dict(),
            "auth_enabled": authenticator.enabled,
            "app": nova.status(),
        }

    @app.get("/system/health")
    async def system_health(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return SystemHealthMonitor(Path.cwd()).run(nova.status()).to_dict()

    @app.get("/system/telemetry")
    async def system_telemetry(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Live machine metrics for the Command Center, all of them real.

        Cheap metrics (CPU, RAM, disk, battery, uptime) are read live on each
        request; the expensive ones come from the sampler thread's cache. Every
        metric carries `available` and, when false, the reason — the UI renders
        "Unavailable" rather than a plausible-looking number nobody measured.
        """
        return telemetry.payload()

    @app.get("/system/harden")
    async def system_harden(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return ReleaseHardeningChecker(Path.cwd()).run().to_dict()

    @app.get("/system/package")
    async def system_package(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return RuntimePackageBuilder(Path.cwd()).build().to_dict()

    @app.get("/settings")
    async def get_settings(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.settings.to_dict()

    @app.post("/settings")
    async def update_settings(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            retention_days = (
                int(payload["audit_retention_days"]) if "audit_retention_days" in payload else None
            )
            max_records = (
                int(payload["audit_max_records"]) if "audit_max_records" in payload else None
            )
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
                int(payload["training_max_records"]) if "training_max_records" in payload else None
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
                int(payload["rlhf_max_checkpoints"]) if "rlhf_max_checkpoints" in payload else None
            )
            rlhf_retention_days = (
                int(payload["rlhf_retention_days"]) if "rlhf_retention_days" in payload else None
            )
            rlhf_max_records = (
                int(payload["rlhf_max_records"]) if "rlhf_max_records" in payload else None
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail="Retention must be a whole number."
            ) from exc
        settings = nova.settings.update(
            approval_mode=ApprovalMode(str(payload["approval_mode"]))
            if "approval_mode" in payload
            else None,
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
            rlhf_enabled=bool(payload["rlhf_enabled"]) if "rlhf_enabled" in payload else None,
            rlhf_dry_run=bool(payload["rlhf_dry_run"]) if "rlhf_dry_run" in payload else None,
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
    async def brain_mode(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
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
    async def brain_cloud_set(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
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
    async def brain_cloud_test(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
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
    async def brain_local_model(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
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
        return {
            "status": "cleared",
            "conversation_turns": 0,
            "transcript_dropped": transcript_dropped,
        }

    @app.get("/chat/history")
    async def chat_history_get(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The shared chat thread (server-persisted, same for every browser)."""
        return nova.chat_history()

    @app.post("/chat/history")
    async def chat_history_post(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Append one client turn to the shared thread, or (action=migrate)
        one-time import a browser's old localStorage thread. The import is
        idempotent per (role, text, at) so re-running it never duplicates."""
        if str(payload.get("action", "")) == "migrate":
            raw_turns = payload.get("turns")
            turns = (
                [t for t in raw_turns if isinstance(t, dict)] if isinstance(raw_turns, list) else []
            )
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
    async def tasks_delete(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
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
    async def tasks_clear_undo(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
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
    async def improve(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        return nova.self_improvement.plan(_required_field(payload, "goal")).to_dict()

    @app.post("/improve/workflow")
    async def improve_workflow(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        return nova.improvement_workflow(_required_field(payload, "goal"))

    @app.post("/improve/preview")
    async def improve_preview(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        return nova.preview_improvement_workflow(_required_field(payload, "goal"))

    @app.post("/improve/approve")
    async def improve_approve(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        return await nova.approve_improvement_workflow(
            _required_field(payload, "goal"),
            preview_id=str(payload.get("preview_id") or "") or None,
        )

    @app.post("/learn")
    async def learn(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        return await nova.learning_cycle(
            _required_field(payload, "goal"),
            feedback=str(payload.get("feedback", "")),
        )

    @app.post("/knowledge/teach")
    async def knowledge_teach(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Teach: persist a typed fact as durable, recallable knowledge."""
        try:
            return await nova.teach_knowledge(_required_field(payload, "fact"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/knowledge")
    async def knowledge_list(
        query: str = "", _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        return await nova.list_knowledge(query)

    @app.post("/knowledge/recall")
    async def knowledge_recall(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        return await nova.list_knowledge(
            str(payload.get("query", "")), limit=int(payload.get("limit", 20))
        )

    @app.post("/train")
    async def train(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        return await nova.autonomous_learning_loop(
            _required_field(payload, "goal"),
            iterations=int(payload.get("iterations", 3)),
            feedback=str(payload.get("feedback", "")),
        )

    @app.post("/plan")
    async def plan(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Plan a goal: steps, tools, effects, dependencies and how each is checked.

        The planner is the application's own compiler (Phase 4), so a plan here
        and a plan the agent loop follows are the same plan. ``execute`` walks
        it; steps that change something still need confirmation, and this
        endpoint cannot grant it — use ``/plan/run`` to approve steps explicitly.
        """
        workflow_plan = nova.plan_for(_required_field(payload, "goal"))
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
    async def plan_code(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Code-aware planning: language-aware steps + a drafted code artifact.

        Uses the configured LLM when one is wired; otherwise returns the
        deterministic language-aware plan so the Build tab always plans real
        coding work.
        """
        try:
            return await nova.build_code_plan(
                _required_field(payload, "goal"), language=str(payload.get("language", "python"))
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/build/project")
    async def build_project(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Draft a small multi-file project with the coding agent.

        Uses the configured model when available; without one it falls back
        to a deterministic, sandbox-verified scaffold (mode still
        `code_project`, `generated_by` = "scaffold"). Returns the whole
        file map + agent trace.
        """
        try:
            return await nova.build_code_project(
                _required_field(payload, "goal"), language=str(payload.get("language", "python"))
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/build/artifacts")
    async def build_artifacts(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Saved workspace artifacts (newest first) for the Run list."""
        return nova.list_workspace_artifacts()

    @app.post("/build/run")
    async def build_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Re-execute a saved workspace artifact in the sandbox."""
        try:
            return await nova.run_saved_artifact(str(payload.get("filename", "")))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/build/save")
    async def build_save(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
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
    async def command_plan(
        payload: CommandPlanRequest, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Plan a device command — or a catalog game launch, which is verified.

        A game phrase returns the library's plan (resolution, resource gate,
        approval token) instead of a desktop chain, because the library is the
        only path that verifies the process and window that actually appeared.
        """
        return await nova.plan_command_or_game(payload.command)

    @app.post("/command/execute")
    async def command_execute(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        token = str(payload.get("approval_token", "") or "") or None
        try:
            return await nova.execute_command(
                _required_field(payload, "command"),
                approval_token=token,
                correlation_id=str(payload.get("correlation_id", "") or ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/desktop/plan")
    async def desktop_plan(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        return nova.plan_desktop_command(_required_field(payload, "command"))

    @app.post("/desktop/execute")
    async def desktop_execute(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        token = str(payload.get("approval_token", "") or "") or None
        try:
            return await nova.execute_desktop_command(
                _required_field(payload, "command"),
                approval_token=token,
                correlation_id=str(payload.get("correlation_id", "") or ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/browser/plan")
    async def browser_plan(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Plan an approval-gated browser action (navigate / search + extract).

        Dedicated device endpoint — same contract as /desktop/plan: a plan with
        an approval token, so a browser phrase can never land on the desktop
        controller (and vice versa) regardless of intent classification.
        """
        return nova.plan_browser_command(_required_field(payload, "command"))

    @app.post("/browser/execute")
    async def browser_execute(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        token = str(payload.get("approval_token", "") or "") or None
        try:
            return await nova.execute_browser_command(
                _required_field(payload, "command"),
                approval_token=token,
                correlation_id=str(payload.get("correlation_id", "") or ""),
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
    async def vision_model_set(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
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
    async def vision_click(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Vision-locate a labeled element on screen, click it, verify."""
        label = str(payload.get("label", "")).strip()
        if not label:
            raise HTTPException(status_code=422, detail="A 'label' is required.")
        try:
            return await nova.vision.guided_click(label)
        except ValueError as exc:
            # Command-shaped labels and empty plans are user-fixable errors.
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ── NovaMarket NM-01: the financial-intelligence subsystem contract ────────
    # These routes are the programmatic management surface the phase requires:
    # detect, inspect health/capabilities, validate configuration, enable and
    # disable. They invoke the real service, which owns the lifecycle. There is
    # deliberately no market-data, portfolio, forecast or trading route: those
    # phases have not shipped, and an endpoint that returned a placeholder would
    # be a false claim rather than a contract.
    @app.get("/market/workspace")
    async def market_workspace() -> Any:
        """The standalone NovaMarket operational workspace.

        This is intentionally a separate page from the NovaControl root UI:
        NM-14 does not decide product placement. The page consumes the real
        authenticated market API and never embeds a price or balance.
        """
        path = static_dir / "market-workspace.html"
        if not path.exists():
            raise HTTPException(status_code=404, detail="NovaMarket workspace is not installed.")
        version = _static_asset_version(static_dir)
        html = path.read_text(encoding="utf-8").replace(_ASSET_VERSION_MARKER, version)
        html = html.replace(_UI_VERSION_MARKER, version[:8])
        return HTMLResponse(content=html, headers=_never_cache_headers())

    # NovaMarket reads are synchronous over SQLite and the analysis workbench.
    # Keep these handlers as `def` so Starlette runs them in its worker
    # threadpool; an `async def` with a synchronous body blocks the event loop
    # and makes every concurrent read wait behind the screener.
    @app.get("/market/status")
    def market_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """NovaMarket lifecycle, health, capabilities, dependencies and config."""
        return nova.novamarket.status()

    @app.get("/market/overview")
    def market_overview(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """One real, read-only integration view for the NM-14 workspace."""
        return nova.novamarket.workspace_overview()

    @app.get("/market/contracts")
    async def market_contracts(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The versioned contracts: lifecycle, events, permissions, horizons, data."""
        return nova.novamarket.contracts()

    @app.get("/market/config")
    async def market_config(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The resolved (secret-reference-only) configuration and its schema."""
        from novacontrol.novamarket.config import NovaMarketConfig

        return {
            "config": nova.novamarket.config.to_dict(),
            "valid": not nova.novamarket.config_error,
            "error": nova.novamarket.config_error,
            "schema": list(NovaMarketConfig.schema()),
        }

    @app.post("/market/notifications/test")
    async def market_notification_test(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Send one real test message through the configured SMTP transport."""
        result = await nova.novamarket.send_test_notification()
        if not result.get("ok"):
            raise HTTPException(
                status_code=422, detail=str(result.get("detail") or "email delivery failed")
            )
        return result

    @app.post("/market/config")
    async def market_config_update(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Validate and apply a configuration change; 422 names the invalid field."""
        try:
            return await nova.novamarket.update_config(payload, reason="api configuration change")
        except NovaMarketConfigurationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except NovaMarketError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/market/enable")
    async def market_enable(
        payload: dict[str, Any] | None = None, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Initialise and start NovaMarket; a failure is reported, not hidden."""
        reason = str((payload or {}).get("reason") or "api enable")
        try:
            return await nova.novamarket.enable(reason=reason)
        except NovaMarketError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/market/disable")
    async def market_disable(
        payload: dict[str, Any] | None = None, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Stand NovaMarket down and withdraw everything it contributed."""
        reason = str((payload or {}).get("reason") or "api disable")
        return await nova.novamarket.disable(reason=reason)

    # ── NovaMarket AT-01: autonomy runtime and safety shell ────────────────
    # These routes manage a persisted mode and lease only.  There is no scan,
    # signal, proposal, order, fill, exit or learning route in this group, and
    # every mutation requires the dedicated operator credential above in
    # addition to ordinary API authentication.
    def _autonomy_http_error(exc: Exception) -> HTTPException:
        from novacontrol.novamarket.autonomy import (
            AutonomyLeaseError,
            AutonomyStateError,
            AutonomyStoreError,
            AutonomyValidationError,
        )

        if isinstance(exc, AutonomyValidationError):
            return HTTPException(
                status_code=422,
                detail={"error": "invalid_autonomy_request", "message": str(exc)},
            )
        if isinstance(exc, (AutonomyStateError, AutonomyLeaseError)):
            return HTTPException(
                status_code=409,
                detail={"error": "autonomy_state_conflict", "message": str(exc)},
            )
        if isinstance(exc, AutonomyStoreError):
            return HTTPException(
                status_code=503,
                detail={"error": "autonomy_store_unavailable", "message": str(exc)},
            )
        from novacontrol.novamarket.autonomy.monitoring import MonitoringRefused

        if isinstance(exc, MonitoringRefused):
            return HTTPException(
                status_code=404 if exc.code == "CYCLE_NOT_FOUND" else 409,
                detail={
                    "error": "monitoring_refused",
                    "reason_code": exc.code,
                    "message": exc.message,
                },
            )
        if isinstance(exc, ValueError):
            return HTTPException(
                status_code=422,
                detail={"error": "invalid_monitoring_request", "message": str(exc)},
            )
        return HTTPException(
            status_code=500,
            detail={"error": "autonomy_internal_error", "message": str(exc)},
        )

    def _autonomy_control_response(result: dict[str, Any]) -> dict[str, Any]:
        """A control call that passed the dependency is authorized by that token."""

        result["operator_authorized"] = True
        result["operator_authorization_configured"] = (
            operator_authenticator.enabled or admin_authenticator.enabled
        )
        return result

    @app.get("/market/autonomy/contracts")
    def market_autonomy_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Static AT-01 modes, transitions, safety flags and limitations."""

        return nova.novamarket.autonomy_contract()

    @app.get("/market/autonomy/status")
    def market_autonomy_status(
        request: Request,
        _principal: str = Depends(require_auth),
        operator_ok: bool = Depends(autonomy_operator_state),
    ) -> dict[str, Any]:
        """Current mode, runtime state, lease ownership and safety flags."""

        try:
            return nova.novamarket.autonomy_status(
                operator_authorized=operator_ok,
                operator_configured=(
                    operator_authenticator.enabled or _is_loopback_request(request)
                ),
            )
        except Exception as exc:  # noqa: BLE001 - refused with a truthful HTTP status
            raise _autonomy_http_error(exc) from exc

    @app.get("/market/autonomy/health")
    def market_autonomy_health(
        request: Request,
        _principal: str = Depends(require_auth),
        operator_ok: bool = Depends(autonomy_operator_state),
    ) -> dict[str, Any]:
        """Lease/health slice of the same server-authoritative status."""

        try:
            status = nova.novamarket.autonomy_status(
                operator_authorized=operator_ok,
                operator_configured=(
                    operator_authenticator.enabled or _is_loopback_request(request)
                ),
            )
        except Exception as exc:  # noqa: BLE001
            raise _autonomy_http_error(exc) from exc
        health = status.get("health", {})
        return {
            "ok": bool(health.get("state") == "ok"),
            "runtime_state": status.get("runtime_state"),
            "health": health,
            "lease": status.get("lease"),
            "trading_implemented": bool(status.get("trading_implemented")),
            "orders_possible": bool(status.get("orders_possible")),
        }

    @app.get("/market/autonomy/reliability")
    def market_autonomy_reliability(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """AT-06 read-only reliability, reconciliation and recovery snapshot."""

        try:
            return nova.novamarket.autonomy_reliability()
        except Exception as exc:  # noqa: BLE001
            raise _autonomy_http_error(exc) from exc

    @app.post("/market/autonomy/recover")
    async def market_autonomy_recover(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        """Reconcile durable work and return to disabled mode; never auto-resume."""

        try:
            return await nova.novamarket.autonomy_recover(
                reason=str((payload or {}).get("reason") or "operator recovery")
            )
        except Exception as exc:  # noqa: BLE001
            raise _autonomy_http_error(exc) from exc

    @app.post("/market/autonomy/start")
    def market_autonomy_start(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        """Start/renew monitoring or the non-trading autonomous shell state."""

        body = payload or {}
        try:
            return _autonomy_control_response(
                nova.novamarket.autonomy_start(
                    str(body.get("mode") or "monitoring"),
                    reason=str(body.get("reason") or "operator start"),
                    acknowledge_paper_only=bool(body.get("acknowledge_paper_only", False)),
                )
            )
        except Exception as exc:  # noqa: BLE001
            raise _autonomy_http_error(exc) from exc

    @app.post("/market/autonomy/pause")
    def market_autonomy_pause(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        """Pause new autonomous work while retaining the shell and lease."""

        try:
            return _autonomy_control_response(
                nova.novamarket.autonomy_pause(
                    reason=str((payload or {}).get("reason") or "operator pause")
                )
            )
        except Exception as exc:  # noqa: BLE001
            raise _autonomy_http_error(exc) from exc

    @app.post("/market/autonomy/resume")
    def market_autonomy_resume(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        """Resume a paused shell into monitoring or autonomous state."""

        body = payload or {}
        try:
            return _autonomy_control_response(
                nova.novamarket.autonomy_resume(
                    str(body.get("mode") or "monitoring"),
                    reason=str(body.get("reason") or "operator resume"),
                    acknowledge_paper_only=bool(body.get("acknowledge_paper_only", False)),
                )
            )
        except Exception as exc:  # noqa: BLE001
            raise _autonomy_http_error(exc) from exc

    @app.post("/market/autonomy/stop")
    def market_autonomy_stop(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        """Disable the shell and release its lease; no orders are affected."""

        try:
            return _autonomy_control_response(
                nova.novamarket.autonomy_stop(
                    reason=str((payload or {}).get("reason") or "operator stop")
                )
            )
        except Exception as exc:  # noqa: BLE001
            raise _autonomy_http_error(exc) from exc

    # ── AT-02: session-aware monitoring and discovery (read + monitoring only) ─

    @app.get("/market/autonomy/session")
    def market_autonomy_session(
        exchange: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Verified exchange-local session state; fail closed outside coverage."""

        try:
            return nova.novamarket.autonomy_session_status(exchange=exchange)
        except Exception as exc:  # noqa: BLE001
            raise _autonomy_http_error(exc) from exc

    @app.get("/market/autonomy/monitoring/contracts")
    def market_autonomy_monitoring_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Static AT-02 capabilities, thresholds and refusal reasons."""

        return nova.novamarket.autonomy_monitoring_contract()

    @app.get("/market/autonomy/monitoring")
    def market_autonomy_monitoring(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Scheduler, session, rotation and health of the AT-02 runner."""

        try:
            return nova.novamarket.autonomy_monitoring_status()
        except Exception as exc:  # noqa: BLE001
            raise _autonomy_http_error(exc) from exc

    @app.get("/market/autonomy/monitoring/cycles")
    def market_autonomy_monitoring_cycles(
        limit: int = 20,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Persistent monitoring-cycle history, newest first."""

        if limit < 1 or limit > 200:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_limit",
                    "message": "limit must be between 1 and 200",
                },
            )
        try:
            return nova.novamarket.autonomy_monitoring_cycles(limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _autonomy_http_error(exc) from exc

    @app.get("/market/autonomy/monitoring/cycles/{cycle_id}")
    def market_autonomy_monitoring_cycle(
        cycle_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """One cycle with its processed, deferred and rejected candidates."""

        try:
            return nova.novamarket.autonomy_monitoring_cycle(cycle_id)
        except Exception as exc:  # noqa: BLE001
            raise _autonomy_http_error(exc) from exc

    @app.post("/market/autonomy/monitoring/run")
    async def market_autonomy_monitoring_run(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        """Run one monitoring cycle through the same gated runner."""

        try:
            result = await nova.novamarket.autonomy_monitoring_run(
                trigger=str((payload or {}).get("trigger") or "manual")
            )
        except Exception as exc:  # noqa: BLE001
            raise _autonomy_http_error(exc) from exc
        if result.get("status") == "refused":
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "monitoring_refused",
                    "reason_code": result.get("reason_code"),
                    "message": result.get("reason"),
                },
            )
        return _autonomy_control_response(result)

    @app.post("/market/autonomy/monitoring/config")
    def market_autonomy_monitoring_config(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        """Persist validated monitoring configuration (caps and floors enforced)."""

        try:
            result = nova.novamarket.autonomy_monitoring_configure(payload or {})
        except Exception as exc:  # noqa: BLE001
            raise _autonomy_http_error(exc) from exc
        return _autonomy_control_response(result)

    # ── AT-03: decision and NM-08 risk assessment (no execution) ──────────────
    # The decision pipeline composes NM-09 evaluation and the NM-08 risk engine.
    # An approved proposal is a record only: this group has no order, fill,
    # forward-to-execution or portfolio-mutation route, and the run mutation
    # requires the separate autonomy-operator credential.
    def _decision_http_error(exc: Exception) -> HTTPException:
        from novacontrol.novamarket.decision import (
            DecisionError,
            DecisionNotFound,
            DecisionStateError,
            DecisionValidationError,
            ForecastDecisionConfigError,
        )

        if isinstance(exc, ForecastDecisionConfigError):
            return HTTPException(
                status_code=422,
                detail={"error": "invalid_forecast_policy", "message": str(exc)},
            )
        if isinstance(exc, DecisionNotFound):
            return HTTPException(
                status_code=404,
                detail={
                    "error": "decision_not_found",
                    "message": exc.message,
                },
            )
        if isinstance(exc, DecisionValidationError):
            return HTTPException(
                status_code=422,
                detail={"error": exc.code, "message": exc.message},
            )
        if isinstance(exc, DecisionStateError):
            return HTTPException(
                status_code=409,
                detail={"error": exc.code, "message": exc.message},
            )
        if isinstance(exc, DecisionError):
            return HTTPException(
                status_code=exc.status,
                detail={"error": exc.code, "message": exc.message},
            )
        from novacontrol.novamarket.autonomy.models import AutonomyStoreError

        if isinstance(exc, AutonomyStoreError):
            return HTTPException(
                status_code=503,
                detail={"error": "decision_store_unavailable", "message": str(exc)},
            )
        return HTTPException(
            status_code=500,
            detail={"error": "decision_internal_error", "message": str(exc)},
        )

    def _execution_http_error(exc: Exception) -> HTTPException:
        from novacontrol.novamarket.autonomy_execution import (
            AutonomyExecutionError,
            ExecutionNotFound,
        )
        from novacontrol.novamarket.autonomy_exits import AutonomyExitError
        from novacontrol.novamarket.execution import ExecutionStoreError

        if isinstance(exc, ExecutionNotFound):
            return HTTPException(
                status_code=404,
                detail={"error": exc.code, "message": exc.message},
            )
        if isinstance(exc, AutonomyExecutionError):
            return HTTPException(
                status_code=exc.http_status,
                detail={"error": exc.code, "message": exc.message},
            )
        if isinstance(exc, AutonomyExitError):
            return HTTPException(
                status_code=exc.http_status,
                detail={"error": exc.code, "message": exc.message},
            )
        if isinstance(exc, ExecutionStoreError):
            return HTTPException(
                status_code=503,
                detail={"error": "execution_journal_unavailable", "message": str(exc)},
            )
        return HTTPException(
            status_code=500,
            detail={"error": "execution_internal_error", "message": str(exc)},
        )

    @app.get("/market/autonomy/decisions/contracts")
    def market_autonomy_decisions_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Static AT-03 decision/risk capabilities and safety boundaries."""

        try:
            return nova.novamarket.autonomy_decision_contract()
        except Exception as exc:  # noqa: BLE001
            raise _decision_http_error(exc) from exc

    @app.get("/market/autonomy/decisions/status")
    def market_autonomy_decisions_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Pipeline health, latest run, signals, proposals and counts."""

        try:
            return nova.novamarket.autonomy_decision_status()
        except Exception as exc:  # noqa: BLE001
            raise _decision_http_error(exc) from exc

    @app.get("/market/autonomy/decisions/runs")
    def market_autonomy_decision_runs(
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Persistent decision-run history, newest first."""

        if limit < 1 or limit > 200:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_limit",
                    "message": "limit must be between 1 and 200",
                },
            )
        try:
            return nova.novamarket.autonomy_decision_runs(limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _decision_http_error(exc) from exc

    @app.get("/market/autonomy/decisions/runs/{run_id}")
    def market_autonomy_decision_run(
        run_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """One run with evaluations, signals, proposals, risk and events."""

        try:
            return nova.novamarket.autonomy_decision_run_detail(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _decision_http_error(exc) from exc

    @app.get("/market/autonomy/decisions/signals")
    def market_autonomy_decision_signals(
        run_id: str = "",
        strategy_id: str = "",
        portfolio_id: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Generated signals with validation and provenance."""

        if limit < 1 or limit > 500:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_limit",
                    "message": "limit must be between 1 and 500",
                },
            )
        try:
            return nova.novamarket.autonomy_decision_signals(
                run_id=run_id,
                strategy_id=strategy_id,
                portfolio_id=portfolio_id,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            raise _decision_http_error(exc) from exc

    @app.get("/market/autonomy/decisions/proposals")
    def market_autonomy_decision_proposals(
        run_id: str = "",
        portfolio_id: str = "",
        instrument_id: str = "",
        status: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Proposal history; risk_approved is not an executed trade."""

        if limit < 1 or limit > 500:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_limit",
                    "message": "limit must be between 1 and 500",
                },
            )
        try:
            return nova.novamarket.autonomy_decision_proposals(
                run_id=run_id,
                portfolio_id=portfolio_id,
                instrument_id=instrument_id,
                status=status,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            raise _decision_http_error(exc) from exc

    @app.get("/market/autonomy/decisions/proposals/{proposal_id}")
    def market_autonomy_decision_proposal(
        proposal_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """One proposal with signal, evaluation, NM-08 decision and events."""

        try:
            return nova.novamarket.autonomy_decision_proposal(proposal_id)
        except Exception as exc:  # noqa: BLE001
            raise _decision_http_error(exc) from exc

    @app.get("/market/autonomy/decisions/predictions")
    def market_autonomy_prediction_decisions(
        run_id: str = "",
        portfolio_id: str = "",
        instrument_id: str = "",
        decision_type: str = "",
        limit: int = 50,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """PI-12 forecast decisions with their immutable evidence and status."""

        if limit < 1 or limit > 500 or offset < 0:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_pagination",
                    "message": "limit must be 1-500 and offset must not be negative",
                },
            )
        normalized = decision_type.strip().upper()
        if normalized and normalized not in {
            "BUY",
            "SELL",
            "HOLD",
            "WAIT",
            "NO_ACTION",
        }:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_decision_type",
                    "message": "decision_type must be BUY, SELL, HOLD, WAIT or NO_ACTION",
                },
            )
        try:
            return nova.novamarket.autonomy_prediction_decisions(
                run_id=run_id,
                portfolio_id=portfolio_id,
                instrument_id=instrument_id,
                decision_type=normalized,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:  # noqa: BLE001
            raise _decision_http_error(exc) from exc

    @app.get("/market/autonomy/decisions/predictions/{decision_id}")
    def market_autonomy_prediction_decision(
        decision_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """One forecast decision with audit history, risk, proposal and outcome."""

        try:
            return nova.novamarket.autonomy_prediction_decision(decision_id)
        except Exception as exc:  # noqa: BLE001
            raise _decision_http_error(exc) from exc

    @app.get("/market/autonomy/decisions/outcomes")
    def market_autonomy_prediction_outcomes(
        limit: int = 100,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Measured outcomes, kept separate from the original decisions."""

        if limit < 1 or limit > 500 or offset < 0:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_pagination",
                    "message": "limit must be 1-500 and offset must not be negative",
                },
            )
        try:
            return nova.novamarket.autonomy_prediction_outcomes(limit=limit, offset=offset)
        except Exception as exc:  # noqa: BLE001
            raise _decision_http_error(exc) from exc

    @app.get("/market/autonomy/decisions/forecast-policy")
    def market_autonomy_forecast_policy(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The effective multi-horizon forecast decision policy."""

        try:
            return nova.novamarket.autonomy_forecast_policy()
        except Exception as exc:  # noqa: BLE001
            raise _decision_http_error(exc) from exc

    @app.put("/market/autonomy/decisions/forecast-policy")
    def market_autonomy_forecast_policy_update(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        """Persist a validated policy; this can never promote a model."""

        body: dict[str, Any] = dict(payload or {})
        nested = body.get("policy")
        policy: dict[str, Any] = nested if isinstance(nested, dict) else body
        try:
            result = nova.novamarket.autonomy_forecast_policy_update(policy)
        except Exception as exc:  # noqa: BLE001
            raise _decision_http_error(exc) from exc
        return _autonomy_control_response(result)

    @app.post("/market/autonomy/decisions/outcomes/run")
    def market_autonomy_prediction_outcomes_run(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        """Measure due outcomes; no portfolio or ledger mutation is permitted."""

        try:
            limit = int((payload or {}).get("limit", 50))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_limit",
                    "message": "limit must be an integer between 1 and 500",
                },
            ) from None
        if limit < 1 or limit > 500:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_limit",
                    "message": "limit must be between 1 and 500",
                },
            )
        try:
            result = nova.novamarket.track_prediction_outcomes(limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _decision_http_error(exc) from exc
        return _autonomy_control_response(result)

    @app.post("/market/autonomy/decisions/run")
    async def market_autonomy_decision_run_trigger(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        """Run the decision pipeline; requires the operator token and never trades."""

        body = payload or {}
        try:
            result = await nova.novamarket.autonomy_decision_run(
                monitoring_cycle_id=str(body.get("monitoring_cycle_id") or ""),
                trigger=str(body.get("trigger") or "manual"),
                actor="autonomy-operator",
            )
        except Exception as exc:  # noqa: BLE001
            raise _decision_http_error(exc) from exc
        return _autonomy_control_response(result)

    # ── AT-04: autonomous paper execution through NM-10 ───────────────────────
    # Reads expose the durable AT-04 journal and NM-10's authoritative order and
    # fill records. The run mutation still requires autonomous mode, a valid
    # AT-01 lease and the dedicated operator token; it submits paper orders only
    # through NM-10 and has no broker, live-order or financial-write route.
    @app.get("/market/autonomy/executions/contracts")
    def market_autonomy_executions_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Static AT-04 paper-execution capabilities and safety boundaries."""

        try:
            return nova.novamarket.autonomy_execution_contract()
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc

    @app.get("/market/autonomy/executions/status")
    def market_autonomy_executions_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """AT-04 journal health, counts, runtime gate and recent executions."""

        try:
            return nova.novamarket.autonomy_execution_status()
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc

    @app.get("/market/autonomy/executions")
    def market_autonomy_executions(
        status: str = "",
        portfolio_id: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Autonomous paper-execution intents, newest first; filters are validated."""

        if limit < 1 or limit > 500:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_limit",
                    "message": "limit must be between 1 and 500",
                },
            )
        try:
            return nova.novamarket.autonomy_executions(
                status=status, portfolio_id=portfolio_id, limit=limit
            )
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc

    @app.get("/market/autonomy/executions/failures")
    def market_autonomy_execution_failures(
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Execution intents carrying a failure or unresolved reconciliation."""

        if limit < 1 or limit > 500:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_limit",
                    "message": "limit must be between 1 and 500",
                },
            )
        try:
            return nova.novamarket.autonomy_execution_failures(limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc

    @app.get("/market/autonomy/executions/{execution_id}")
    def market_autonomy_execution(
        execution_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """One execution with its journal, authoritative NM-10 order and fills."""

        try:
            return nova.novamarket.autonomy_execution(execution_id)
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc

    @app.get("/market/autonomy/executions/{execution_id}/events")
    def market_autonomy_execution_events(
        execution_id: str,
        limit: int = 200,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Append-only AT-04 execution journal events for one attempt."""

        if limit < 1 or limit > 1000:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_limit",
                    "message": "limit must be between 1 and 1000",
                },
            )
        try:
            return nova.novamarket.autonomy_execution_events(execution_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc

    @app.post("/market/autonomy/executions/run")
    async def market_autonomy_executions_run(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        """Forward eligible risk-approved proposals to NM-10 paper execution."""

        body = payload or {}
        try:
            result = await nova.novamarket.autonomy_execution_run(
                trigger=str(body.get("trigger") or "manual"),
                actor="autonomy-operator",
                proposal_id=str(body.get("proposal_id") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc
        return _autonomy_control_response(result)

    @app.post("/market/autonomy/executions/{execution_id}/reconcile")
    async def market_autonomy_execution_reconcile(
        execution_id: str,
        _principal: str = Depends(require_auth),
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        """Reconcile one execution with NM-10; never creates an order or fill."""

        try:
            result = await nova.novamarket.autonomy_execution_reconcile(execution_id)
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc
        return _autonomy_control_response(result)

    # ── AT-05: protective exit supervision and learning evidence ─────────────
    # Reads expose the durable exit journal, NM-08 assessment, NM-10 order and
    # fills, and advisory learning evidence. The run/reconcile controls require
    # the dedicated operator token and still pass the runtime mode, lease and
    # NM-08/NM-10 gates; there is no direct financial-write route.
    @app.get("/market/autonomy/exits/contracts")
    def market_autonomy_exits_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Static AT-05 stop-loss exit capabilities and unsupported gaps."""

        try:
            return nova.novamarket.autonomy_exits_contract()
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc

    @app.get("/market/autonomy/exits/status")
    def market_autonomy_exits_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """AT-05 mode, last cycle, counts and supervised position state."""

        try:
            return nova.novamarket.autonomy_exits_status()
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc

    @app.get("/market/autonomy/exits/positions")
    def market_autonomy_exit_positions(
        limit: int = 200,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Durable supervision state for positions seen by AT-05."""

        if limit < 1 or limit > 500:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_limit",
                    "message": "limit must be between 1 and 500",
                },
            )
        try:
            return nova.novamarket.autonomy_exit_positions(limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc

    @app.get("/market/autonomy/exits/configurations")
    def market_autonomy_exit_configurations(
        limit: int = 200,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Persisted target/trailing definitions for long paper positions."""

        if limit < 1 or limit > 500:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_limit",
                    "message": "limit must be between 1 and 500",
                },
            )
        try:
            return nova.novamarket.autonomy_exit_configurations(limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc

    @app.put("/market/autonomy/exits/configurations/{portfolio_id}/{instrument_id}")
    def market_autonomy_exit_configuration_set(
        portfolio_id: str,
        instrument_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        """Create or version a target/trailing configuration; no order is created."""

        try:
            return nova.novamarket.set_autonomy_exit_configuration(
                portfolio_id, instrument_id, payload or {}
            )
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc

    @app.delete("/market/autonomy/exits/configurations/{portfolio_id}/{instrument_id}")
    def market_autonomy_exit_configuration_delete(
        portfolio_id: str,
        instrument_id: str,
        _principal: str = Depends(require_auth),
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        """Remove a target/trailing configuration without touching the position."""

        try:
            return nova.novamarket.delete_autonomy_exit_configuration(portfolio_id, instrument_id)
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc

    @app.get("/market/autonomy/exits/decisions")
    def market_autonomy_exit_decisions(
        portfolio_id: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Exit decisions, newest first; reads never submit an order."""

        if limit < 1 or limit > 500:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_limit",
                    "message": "limit must be between 1 and 500",
                },
            )
        try:
            return nova.novamarket.autonomy_exit_decisions(portfolio_id=portfolio_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc

    @app.get("/market/autonomy/exits/decisions/{decision_id}/events")
    def market_autonomy_exit_events(
        decision_id: str,
        limit: int = 200,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Append-only AT-05 journal events for one exit decision."""

        if limit < 1 or limit > 1000:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_limit",
                    "message": "limit must be between 1 and 1000",
                },
            )
        try:
            return nova.novamarket.autonomy_exit_events(decision_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc

    @app.get("/market/autonomy/exits/decisions/{decision_id}")
    def market_autonomy_exit_decision(
        decision_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """One exit decision with NM-08 provenance, NM-10 order and fills."""

        try:
            return nova.novamarket.autonomy_exit_decision(decision_id)
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc

    @app.get("/market/autonomy/exits/learning")
    def market_autonomy_exit_learning(
        decision_id: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Advisory entry-to-exit learning evidence with completeness state."""

        if limit < 1 or limit > 500:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_limit",
                    "message": "limit must be between 1 and 500",
                },
            )
        try:
            return nova.novamarket.autonomy_learning_evidence(decision_id=decision_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc

    @app.post("/market/autonomy/exits/run")
    async def market_autonomy_exits_run(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        """Run one stop-loss supervision cycle in monitoring, autonomous or paused mode."""

        body = payload or {}
        try:
            result = await nova.novamarket.run_autonomy_exits(
                trigger=str(body.get("trigger") or "manual")
            )
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc
        return result

    @app.post("/market/autonomy/exits/decisions/{decision_id}/reconcile")
    async def market_autonomy_exit_reconcile(
        decision_id: str,
        _principal: str = Depends(require_auth),
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        """Reconcile an exit with NM-10 order, fills, position and ledger; never creates one."""

        try:
            result = await nova.novamarket.reconcile_autonomy_exit(decision_id)
        except Exception as exc:  # noqa: BLE001
            raise _execution_http_error(exc) from exc
        return {"ok": True, "decision": result}

    # ── NovaMarket NM-02: validated market data ────────────────────────────────
    # Every handler drives the ONE engine behind the lifecycle. Reads return
    # stored, validated observations with their real freshness and provenance;
    # nothing here invents a price or presents a delayed close as a live quote.
    # ── NovaMarket NM-03: NovaCoin wallets and the deterministic ledger ───────
    # Money moves only through a posted, balanced transaction; there is no route
    # that writes a balance. Every read is the ledger's own record of what
    # happened, and a rejected request changes nothing.
    def _wallet_http_error(exc: Exception) -> HTTPException:
        from novacontrol.novamarket.contracts import (
            InsufficientFundsError,
            LedgerIntegrityError,
            NotOperationalError,
            WalletConflictError,
            WalletError,
            WalletNotFoundError,
            WalletNotActiveError,
            WalletValidationError,
        )

        if isinstance(exc, WalletNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, NotOperationalError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(
            exc,
            (InsufficientFundsError, WalletNotActiveError, WalletConflictError),
        ):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, WalletValidationError):
            return HTTPException(status_code=422, detail=str(exc))
        if isinstance(exc, LedgerIntegrityError):
            return HTTPException(status_code=500, detail=str(exc))
        if isinstance(exc, WalletError):
            return HTTPException(status_code=400, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    @app.get("/market/wallet/status")
    async def market_wallet_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Ledger totals, schema version and the last reconciliation result."""
        return nova.novamarket.wallet_status()

    @app.get("/market/wallets")
    def market_wallets(
        owner: str | None = None,
        limit: int = 100,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """List NovaCoin wallets with their available and total balances."""
        try:
            return nova.novamarket.wallets_list(owner=owner, limit=limit, offset=offset)
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _wallet_http_error(exc) from exc

    @app.post("/market/wallets")
    async def market_wallet_create(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Create a NovaCoin wallet; it always opens with a zero balance."""
        try:
            return await nova.novamarket.create_wallet(
                str(payload.get("owner") or ""),
                label=str(payload.get("label") or ""),
                wallet_id=payload.get("wallet_id") or None,
            )
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _wallet_http_error(exc) from exc

    @app.post("/market/wallets/transfer")
    async def market_wallet_transfer(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Transfer NC between two wallets in one atomic, balanced transaction."""
        try:
            return await nova.novamarket.wallet_transfer(
                str(payload.get("from_wallet_id") or ""),
                str(payload.get("to_wallet_id") or ""),
                payload.get("amount"),
                idempotency_key=str(payload.get("idempotency_key") or ""),
                reference=str(payload.get("reference") or ""),
                description=str(payload.get("description") or ""),
                owner=payload.get("owner"),
            )
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _wallet_http_error(exc) from exc

    @app.get("/market/wallets/{wallet_id}")
    async def market_wallet_detail(
        wallet_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One wallet with its balance and the counts behind it."""
        try:
            return nova.novamarket.wallet_detail(wallet_id)
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _wallet_http_error(exc) from exc

    @app.get("/market/wallets/{wallet_id}/transactions")
    async def market_wallet_transactions(
        wallet_id: str,
        limit: int = 100,
        offset: int = 0,
        ascending: bool = False,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The wallet's transactions, including transfers it received."""
        try:
            return nova.novamarket.wallet_transactions(
                wallet_id=wallet_id, limit=limit, offset=offset, ascending=ascending
            )
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _wallet_http_error(exc) from exc

    @app.get("/market/wallets/{wallet_id}/ledger")
    async def market_wallet_ledger(
        wallet_id: str,
        limit: int = 100,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The immutable ledger entries that produced the wallet's balance."""
        try:
            return nova.novamarket.wallet_entries(wallet_id=wallet_id, limit=limit, offset=offset)
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _wallet_http_error(exc) from exc

    @app.post("/market/wallets/{wallet_id}/deposit")
    async def market_wallet_deposit(
        wallet_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Deposit simulated NC from the issuance account into one wallet."""
        try:
            return await nova.novamarket.wallet_deposit(
                wallet_id,
                payload.get("amount"),
                idempotency_key=str(payload.get("idempotency_key") or ""),
                reference=str(payload.get("reference") or ""),
                description=str(payload.get("description") or ""),
                owner=payload.get("owner"),
            )
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _wallet_http_error(exc) from exc

    @app.post("/market/wallets/{wallet_id}/withdraw")
    async def market_wallet_withdraw(
        wallet_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Withdraw simulated NC from one wallet back to the issuance account."""
        try:
            return await nova.novamarket.wallet_withdraw(
                wallet_id,
                payload.get("amount"),
                idempotency_key=str(payload.get("idempotency_key") or ""),
                reference=str(payload.get("reference") or ""),
                description=str(payload.get("description") or ""),
                owner=payload.get("owner"),
            )
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _wallet_http_error(exc) from exc

    @app.post("/market/wallets/{wallet_id}/status")
    async def market_wallet_set_status(
        wallet_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Freeze or close a wallet; closing requires a zero balance."""
        try:
            return await nova.novamarket.wallet_set_status(
                wallet_id,
                str(payload.get("status") or ""),
                reason=str(payload.get("reason") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _wallet_http_error(exc) from exc

    @app.get("/market/wallets/{wallet_id}/reconcile")
    async def market_wallet_reconcile(
        wallet_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Recompute one wallet's balance from its ledger and report integrity."""
        try:
            return await nova.novamarket.wallet_reconcile(wallet_id)
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _wallet_http_error(exc) from exc

    @app.post("/market/wallet/reconcile")
    async def market_wallet_reconcile_all(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Reconcile every wallet, the whole-ledger zero sum included."""
        try:
            return await nova.novamarket.wallet_reconcile(None)
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _wallet_http_error(exc) from exc

    @app.post("/market/wallet/reversals")
    async def market_wallet_reversal(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Reverse a posted transaction with mirrored entries; never edits it."""
        try:
            return await nova.novamarket.wallet_reverse(
                str(payload.get("transaction_id") or ""),
                reason=str(payload.get("reason") or ""),
                idempotency_key=str(payload.get("idempotency_key") or ""),
                owner=payload.get("owner"),
            )
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _wallet_http_error(exc) from exc

    # -- NM-04: evidence-backed market events --------------------------------

    def _event_http_error(exc: Exception) -> HTTPException:
        """Map an event-radar failure to one HTTP status, in one place."""
        from novacontrol.novamarket.contracts import (
            EventNotFoundError,
            EventSourceError,
            EventStateError,
            EventValidationError,
            SourceRateLimitedError,
        )

        if isinstance(exc, EventNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, (SourceRateLimitedError,)):
            return HTTPException(status_code=429, detail=str(exc))
        if isinstance(exc, (EventStateError,)):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, (EventValidationError,)):
            return HTTPException(status_code=422, detail=str(exc))
        if isinstance(exc, (EventSourceError,)):
            return HTTPException(status_code=502, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    @app.get("/market/events")
    async def market_events(
        category: str | None = None,
        subcategory: str = "",
        verification: str | None = None,
        importance: str | None = None,
        instrument_id: str = "",
        sector: str = "",
        market: str = "",
        source_id: str = "",
        text: str = "",
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Search stored market events by category, verification or instrument."""
        try:
            return nova.novamarket.radar_search(
                category=category,
                subcategory=subcategory,
                verification=verification,
                importance=importance,
                instrument_id=instrument_id,
                sector=sector,
                market=market,
                source_id=source_id,
                text=text,
                since=since,
                until=until,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _event_http_error(exc) from exc

    @app.get("/market/events/sources")
    async def market_event_sources(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Configured event feeds, their enablement and their last fetch outcome."""
        try:
            return nova.novamarket.radar_sources()
        except Exception as exc:  # noqa: BLE001
            raise _event_http_error(exc) from exc

    @app.post("/market/events/sources/{source_id}/enabled")
    async def market_event_source_enabled(
        source_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Enable or disable one declared feed."""
        try:
            return nova.novamarket.radar_set_source_enabled(
                source_id, bool(payload.get("enabled", False))
            )
        except Exception as exc:  # noqa: BLE001
            raise _event_http_error(exc) from exc

    @app.post("/market/events/refresh")
    async def market_event_refresh(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Fetch one source (or every enabled source) and ingest what it reports."""
        source_id = str((payload or {}).get("source_id") or "") or None
        try:
            return await nova.novamarket.radar_refresh(source_id)
        except Exception as exc:  # noqa: BLE001
            raise _event_http_error(exc) from exc

    @app.post("/market/events/expire")
    async def market_event_expire(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Expire events outside their category review window; history is kept."""
        try:
            return await nova.novamarket.radar_expire()
        except Exception as exc:  # noqa: BLE001
            raise _event_http_error(exc) from exc

    @app.get("/market/events/instrument/{instrument_id}")
    async def market_events_for_instrument(
        instrument_id: str,
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Events mapped to one canonical instrument (MIC:SYMBOL)."""
        try:
            return nova.novamarket.radar_instrument_events(instrument_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _event_http_error(exc) from exc

    @app.get("/market/events/{event_id}/evidence")
    async def market_event_evidence(
        event_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Every source record behind one event, preserved as retrieved."""
        try:
            return nova.novamarket.radar_evidence(event_id)
        except Exception as exc:  # noqa: BLE001
            raise _event_http_error(exc) from exc

    @app.get("/market/events/{event_id}/history")
    async def market_event_history(
        event_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """An event's version history and lifecycle transitions, oldest first."""
        try:
            return nova.novamarket.radar_history(event_id)
        except Exception as exc:  # noqa: BLE001
            raise _event_http_error(exc) from exc

    @app.get("/market/events/{event_id}")
    async def market_event_detail(
        event_id: str,
        version: int | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """One event version with its evidence and lifecycle transitions."""
        try:
            return nova.novamarket.radar_event(event_id, version=version)
        except Exception as exc:  # noqa: BLE001
            raise _event_http_error(exc) from exc

    # -- NM-05: asset discovery and watchlists --------------------------------

    def _discovery_http_error(exc: Exception) -> HTTPException:
        """Map a discovery failure to one HTTP status, in one place."""
        from novacontrol.novamarket.contracts import (
            DiscoveryNotFoundError,
            DiscoveryStateError,
            DiscoveryValidationError,
            EventNotFoundError,
            EventSourceError,
        )

        if isinstance(exc, (DiscoveryNotFoundError, EventNotFoundError)):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, DiscoveryStateError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, DiscoveryValidationError):
            return HTTPException(status_code=422, detail=str(exc))
        if isinstance(exc, EventSourceError):
            return HTTPException(status_code=502, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    @app.get("/market/discovery/status")
    async def market_discovery_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Discovery cycles, sources and totals."""
        return nova.novamarket.discovery_status()

    @app.get("/market/discovery/candidates")
    async def market_discovery_candidates(
        limit: int = 50,
        min_score: float = 0.0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Scored candidates, highest first."""
        try:
            return nova.novamarket.discovery_candidates(limit=limit, min_score=min_score)
        except Exception as exc:  # noqa: BLE001
            raise _discovery_http_error(exc) from exc

    @app.get("/market/discovery/candidates/{instrument_id}")
    async def market_discovery_candidate(
        instrument_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One candidate with its evaluations."""
        try:
            return nova.novamarket.discovery_candidate(instrument_id)
        except Exception as exc:  # noqa: BLE001
            raise _discovery_http_error(exc) from exc

    @app.post("/market/discovery/cycle")
    async def market_discovery_cycle(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Run one bounded discovery cycle."""
        force = bool((payload or {}).get("force", False))
        try:
            return await nova.novamarket.discovery_cycle(force=force)
        except Exception as exc:  # noqa: BLE001
            raise _discovery_http_error(exc) from exc

    @app.post("/market/discovery/reevaluate")
    async def market_discovery_reevaluate(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Reevaluate watchlist members."""
        watchlist_id = str((payload or {}).get("watchlist_id") or "") or None
        try:
            return await nova.novamarket.discovery_reevaluate(watchlist_id=watchlist_id)
        except Exception as exc:  # noqa: BLE001
            raise _discovery_http_error(exc) from exc

    @app.post("/market/discovery/events/{event_id}")
    async def market_discovery_event(
        event_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Evaluate the instruments one verified event names."""
        try:
            return await nova.novamarket.discovery_consume_event(event_id)
        except Exception as exc:  # noqa: BLE001
            raise _discovery_http_error(exc) from exc

    @app.post("/market/discovery/scheduler")
    async def market_discovery_scheduler(
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Start or stop the periodic discovery runner."""
        action = str(payload.get("action") or "")
        interval = payload.get("interval_seconds")
        try:
            return await nova.novamarket.discovery_scheduler(
                action,
                interval_seconds=int(interval) if interval is not None else None,
            )
        except Exception as exc:  # noqa: BLE001
            raise _discovery_http_error(exc) from exc

    @app.get("/market/discovery/history")
    async def market_discovery_history(
        watchlist_id: str = "",
        instrument_id: str = "",
        limit: int = 200,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Membership history and evaluation records."""
        try:
            return nova.novamarket.discovery_history(
                watchlist_id=watchlist_id or None,
                instrument_id=instrument_id or None,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            raise _discovery_http_error(exc) from exc

    @app.get("/market/watchlists")
    async def market_watchlists(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Every watchlist and its active member count."""
        return nova.novamarket.watchlists()

    @app.post("/market/watchlists")
    async def market_watchlist_create(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Create a watchlist."""
        try:
            return await nova.novamarket.create_watchlist(
                str(payload.get("name") or ""),
                kind=str(payload.get("kind") or "custom"),
                description=str(payload.get("description") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise _discovery_http_error(exc) from exc

    @app.get("/market/watchlists/{watchlist_id}")
    async def market_watchlist(
        watchlist_id: str,
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """One watchlist with its ranked members."""
        try:
            return nova.novamarket.watchlist(watchlist_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _discovery_http_error(exc) from exc

    @app.post("/market/watchlists/{watchlist_id}/members")
    async def market_watchlist_add(
        watchlist_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Add an instrument to a watchlist."""
        try:
            return await nova.novamarket.watchlist_add(
                watchlist_id,
                str(payload.get("instrument_id") or ""),
                reason=str(payload.get("reason") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise _discovery_http_error(exc) from exc

    @app.delete("/market/watchlists/{watchlist_id}/members/{instrument_id}")
    async def market_watchlist_remove(
        watchlist_id: str,
        instrument_id: str,
        reason: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Remove a member, keeping its history."""
        try:
            return await nova.novamarket.watchlist_remove(
                watchlist_id, instrument_id, reason=reason
            )
        except Exception as exc:  # noqa: BLE001
            raise _discovery_http_error(exc) from exc

    @app.post("/market/watchlists/{watchlist_id}/rename")
    async def market_watchlist_rename(
        watchlist_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Rename a list in place; its id and membership history are stable."""
        try:
            return await nova.novamarket.rename_watchlist(
                watchlist_id, str(payload.get("name") or "")
            )
        except Exception as exc:  # noqa: BLE001
            raise _discovery_http_error(exc) from exc

    # -- NM-06: probabilistic forecasting --------------------------------------

    def _forecast_http_error(exc: Exception) -> HTTPException:
        """Map a forecasting failure to one HTTP status, in one place."""
        from novacontrol.novamarket.contracts import (
            ForecastDataError,
            ForecastNotFoundError,
            ForecastStateError,
            ForecastValidationError,
        )
        from novacontrol.novamarket.statistical.models import (
            StatisticalModelError,
            StatisticalValidationError,
            UnsupportedHorizonError,
        )
        from novacontrol.novamarket.gradient.models import GradientError
        from novacontrol.novamarket.ensemble.models import (
            EnsembleError,
            EnsembleUnavailableError,
        )
        from novacontrol.novamarket.prediction.models import (
            PredictionNotFoundError,
            PredictionValidationError,
        )

        if isinstance(exc, (ForecastNotFoundError, PredictionNotFoundError)):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(
            exc,
            (
                ForecastValidationError,
                ForecastDataError,
                StatisticalValidationError,
                UnsupportedHorizonError,
                PredictionValidationError,
            ),
        ):
            return HTTPException(status_code=422, detail=str(exc))
        if isinstance(
            exc, (StatisticalModelError, GradientError, EnsembleError, EnsembleUnavailableError)
        ):
            return HTTPException(status_code=422, detail=str(exc))
        if isinstance(exc, ForecastStateError):
            return HTTPException(status_code=409, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    def _forecast_as_of(value: Any) -> datetime | None:
        """Parse an optional cutoff without turning bad input into a 500."""
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                from novacontrol.novamarket.contracts import (
                    ForecastValidationError,
                )

                raise ForecastValidationError("as_of must be an ISO-8601 timestamp") from exc
        from novacontrol.novamarket.contracts import ForecastValidationError

        raise ForecastValidationError("as_of must be an ISO-8601 timestamp")

    @app.post("/market/forecasts")
    async def market_forecast_create(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Generate one NM-06, PI-05, PI-06, PI-07 or PI-08 forecast."""
        try:
            as_of = _forecast_as_of(payload.get("as_of"))
            return await nova.novamarket.forecast_generate(
                str(payload.get("instrument_id") or ""),
                str(payload.get("horizon") or ""),
                statistical=str(payload.get("engine") or "").lower() == "statistical",
                engine=str(payload.get("engine") or "").lower(),
                model_id=str(payload.get("model_id") or ""),
                as_of=as_of,
                resolution=payload.get("resolution"),
                dataset_id=str(payload.get("dataset_id") or ""),
                dataset_version=str(payload.get("dataset_version") or ""),
                feature_set_version=str(payload.get("feature_set_version") or "unspecified"),
                feature_set_id=str(payload.get("feature_set_id") or ""),
                exchange=str(payload.get("exchange") or ""),
                currency=str(payload.get("currency") or ""),
                bars=payload.get("bars"),
                config=payload.get("config"),
                fundamentals=payload.get("fundamentals") or (),
                news_events=payload.get("news_events") or (),
                regime=str(payload.get("regime") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.get("/market/forecasts")
    async def market_forecasts(
        instrument_id: str = "",
        horizon: str = "",
        status: str = "",
        limit: int = 50,
        engine: str = "",
        model_id: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Stored forecasts, newest first."""
        try:
            return nova.novamarket.forecast_list(
                instrument_id=instrument_id or None,
                horizon=horizon or None,
                status=status or None,
                limit=limit,
                engine=engine,
                model_id=model_id or None,
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.get("/market/forecasts/status")
    async def market_forecast_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Horizons, policy, model states and totals."""
        return nova.novamarket.forecast_status()

    @app.get("/market/forecasts/models")
    async def market_forecast_models(
        instrument_id: str = "",
        horizon: str = "",
        engine: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Model versions, states and measured metrics."""
        try:
            return nova.novamarket.forecast_models(
                instrument_id=instrument_id or None,
                horizon=horizon or None,
                engine=engine,
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.get("/market/forecasts/performance")
    async def market_forecast_performance(
        model_id: str = "",
        instrument_id: str = "",
        horizon: str = "",
        limit: int = 50,
        engine: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Measured performance: snapshots and the live aggregate."""
        try:
            return nova.novamarket.forecast_performance(
                model_id=model_id or None,
                instrument_id=instrument_id or None,
                horizon=horizon or None,
                limit=limit,
                engine=engine,
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.get("/market/forecasts/calibration")
    async def market_forecast_calibration(
        model_id: str = "",
        horizon: str = "",
        engine: str = "",
        instrument_id: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The calibration report for one model and horizon."""
        try:
            return nova.novamarket.forecast_calibration(
                model_id, horizon, engine=engine, instrument_id=instrument_id
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.post("/market/forecasts/train")
    async def market_forecast_train(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Run chronological walk-forward comparison and calibration."""
        try:
            as_of = _forecast_as_of(payload.get("as_of"))
            requested_engine = str(payload.get("engine") or "").lower()
            return await nova.novamarket.forecast_train(
                str(payload.get("instrument_id") or ""),
                str(payload.get("horizon") or ""),
                engine=requested_engine,
                as_of=as_of,
                resolution=payload.get("resolution"),
                dataset_id=str(payload.get("dataset_id") or ""),
                dataset_version=str(payload.get("dataset_version") or ""),
                bars=payload.get("bars"),
                config=payload.get("config"),
                models=payload.get("models"),
                fundamentals=payload.get("fundamentals") or (),
                news_events=payload.get("news_events") or (),
                benchmark_bars=payload.get("benchmark_bars"),
                sector_bars=payload.get("sector_bars"),
                context_bars=payload.get("context_bars"),
                train_ratio=payload.get("train_ratio", 0.6),
                validation_ratio=payload.get("validation_ratio", 0.2),
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.post("/market/forecasts/evaluate")
    async def market_forecast_evaluate(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Resolve due NM-06 forecasts, or run a PI-05 chronological evaluation."""
        try:
            body = payload or {}
            as_of = _forecast_as_of(body.get("as_of"))
            return await nova.novamarket.forecast_evaluate(
                engine=str(body.get("engine") or ""),
                instrument_id=str(body.get("instrument_id") or ""),
                horizon=str(body.get("horizon") or ""),
                as_of=as_of,
                resolution=body.get("resolution"),
                dataset_id=str(body.get("dataset_id") or ""),
                dataset_version=str(body.get("dataset_version") or ""),
                bars=body.get("bars"),
                config=body.get("config"),
                models=body.get("models"),
                fundamentals=body.get("fundamentals") or (),
                news_events=body.get("news_events") or (),
                benchmark_bars=body.get("benchmark_bars"),
                sector_bars=body.get("sector_bars"),
                context_bars=body.get("context_bars"),
                train_ratio=body.get("train_ratio", 0.6),
                validation_ratio=body.get("validation_ratio", 0.2),
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.post("/market/forecasts/models/{model_id}/promote")
    async def market_forecast_promote(
        model_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Promote a model that already has walk-forward evidence."""
        try:
            return await nova.novamarket.forecast_promote(
                model_id,
                str(payload.get("instrument_id") or ""),
                str(payload.get("horizon") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.post("/market/forecasts/cycles")
    async def market_forecast_cycle_create(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Schedule one durable PI-09 forecast lifecycle; never creates an order."""
        try:
            target = _forecast_as_of(payload.get("target_at"))
            return nova.novamarket.schedule_forecast_cycle(
                instrument_id=str(payload.get("instrument_id") or ""),
                horizon=str(payload.get("horizon") or ""),
                exchange=str(payload.get("exchange") or ""),
                target_at=target,
                resolution=payload.get("resolution"),
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.get("/market/forecasts/cycles")
    async def market_forecast_cycles(
        instrument_id: str = "",
        exchange: str = "",
        horizon: str = "",
        state: str = "",
        target_date: str = "",
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> list[dict[str, Any]]:
        """PI-09 cycles, those still outstanding first, then newest scheduled.

        A cycle that still has checkpoints to run (scheduled, active, awaiting
        confirmation or its outcome evaluation) is listed before purely
        historical cycles, so an older lifecycle that is still due is never
        buried under recently finished history or cut off by the limit; each
        group is newest-created first.
        """
        try:
            return nova.novamarket.forecast_cycles(
                instrument_id=instrument_id or None,
                exchange=exchange or None,
                horizon=horizon or None,
                state=state or None,
                target_date=target_date or None,
                limit=limit,
                outstanding_first=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.get("/market/forecasts/cycles/{cycle_id}")
    async def market_forecast_cycle(
        cycle_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One cycle with its checkpoint timeline, immutable versions and outcomes."""
        try:
            return nova.novamarket.forecast_cycle(cycle_id)
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.get("/market/forecasts/scheduler")
    async def market_forecast_scheduler(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """PI-09 scheduler health, durable queue state and forecast-only authority."""
        return nova.novamarket.forecast_scheduler_status()

    @app.post("/market/forecasts/scheduler/tick")
    async def market_forecast_scheduler_tick(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Run a bounded, authenticated due-checkpoint batch; never trades."""
        try:
            body = payload or {}
            limit = int(body.get("limit") or 5)
            if not 1 <= limit <= 20:
                raise ValueError("limit must be between 1 and 20")
            return await nova.novamarket.forecast_scheduler_tick(limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.get("/market/forecasts/missed")
    async def market_forecast_missed(
        limit: int = 100, _principal: str = Depends(require_auth)
    ) -> list[dict[str, Any]]:
        """Checkpoints whose planned or retry time passed without completion."""
        try:
            return nova.novamarket.forecast_missed_checkpoints(limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.get("/market/forecasts/evaluations/pending")
    async def market_forecast_pending_evaluations(
        _principal: str = Depends(require_auth),
    ) -> list[dict[str, Any]]:
        """Target-time evaluations waiting on validated observations."""
        try:
            return nova.novamarket.forecast_pending_evaluations()
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.get("/market/forecasts/accuracy")
    async def market_forecast_accuracy(
        instrument_id: str = "",
        horizon: str = "",
        stage: str = "",
        cycle_id: str = "",
        model_id: str = "",
        model_version: str = "",
        ensemble_id: str = "",
        target_date: str = "",
        regime: str = "",
        limit: int = 1000,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Forecast accuracy history with sample counts and reliability caveats."""
        try:
            return nova.novamarket.forecast_accuracy(
                instrument_id=instrument_id or None,
                horizon=horizon or None,
                stage=stage or None,
                cycle_id=cycle_id or None,
                model_id=model_id or None,
                model_version=model_version or None,
                ensemble_id=ensemble_id or None,
                target_date=target_date or None,
                regime=regime or None,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    # -- PI-10: swing and long-term outlook ----------------------------------

    @app.post("/market/forecasts/horizons")
    async def market_forecast_horizons_create(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Generate 1d/3d/5d/30d/90d forecasts through the existing PI-08 stack."""
        try:
            requested = payload.get("horizons") or payload.get("horizon") or None
            if isinstance(requested, str):
                requested = [item.strip() for item in requested.split(",") if item.strip()]
            return await nova.novamarket.outlook_forecast(
                str(payload.get("instrument_id") or ""),
                horizons=requested,
                as_of=_forecast_as_of(payload.get("as_of")),
                resolution=payload.get("resolution"),
                dataset_id=str(payload.get("dataset_id") or ""),
                dataset_version=str(payload.get("dataset_version") or ""),
                feature_set_version=str(payload.get("feature_set_version") or ""),
                feature_set_id=str(payload.get("feature_set_id") or ""),
                exchange=str(payload.get("exchange") or ""),
                currency=str(payload.get("currency") or ""),
                bars=payload.get("bars"),
                config=payload.get("config"),
                fundamentals=payload.get("fundamentals") or (),
                news_events=payload.get("news_events") or (),
                regime=str(payload.get("regime") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.get("/market/forecasts/horizons/status")
    async def market_forecast_horizons_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """PI-10 horizon profiles, evidence rules and unchanged authorities."""
        return nova.novamarket.outlook_status()

    @app.get("/market/forecasts/horizons/performance")
    async def market_forecast_horizons_performance(
        instrument_id: str = "",
        horizon: str = "",
        family: str = "",
        limit: int = 1000,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Horizon-separated forecast outcomes; never one blended accuracy."""
        try:
            if not 1 <= int(limit) <= 10_000:
                from novacontrol.novamarket.outlook import OutlookValidationError

                raise OutlookValidationError("limit must be between 1 and 10000")
            return nova.novamarket.outlook_accuracy(
                instrument_id=instrument_id or None,
                horizon=horizon or None,
                family=family or None,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.get("/market/forecasts/horizons/{instrument_id}/context")
    async def market_forecast_horizons_context(
        instrument_id: str,
        as_of: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Validated point-in-time sector, benchmark, peer and event context."""
        try:
            return await nova.novamarket.outlook_context(
                instrument_id, as_of=_forecast_as_of(as_of)
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.get("/market/forecasts/horizons/{instrument_id}")
    async def market_forecast_horizons_latest(
        instrument_id: str,
        as_of: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Latest stored forecast for each horizon plus cross-horizon analysis."""
        try:
            return nova.novamarket.outlook_latest(instrument_id, as_of=_forecast_as_of(as_of))
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.get("/market/forecasts/{forecast_id}/explain")
    async def market_forecast_explain(
        forecast_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """On-demand constituent/importance detail for one PI-06/07/08 forecast."""
        try:
            if forecast_id.startswith(("ens_", "ensfb_")):
                return nova.novamarket.ensemble_explain(forecast_id)
            if forecast_id.startswith("lstm_"):
                return nova.novamarket.lstm_explain(forecast_id)
            return nova.novamarket.gradient_explain(forecast_id)
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.get("/market/forecasts/{forecast_id}/outcome")
    async def market_forecast_outcome(
        forecast_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """The recorded outcome, or a pending state with the reason."""
        try:
            return nova.novamarket.forecast_outcome(forecast_id)
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    @app.get("/market/forecasts/{forecast_id}")
    async def market_forecast(
        forecast_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One immutable forecast record."""
        try:
            return nova.novamarket.forecast_get(forecast_id)
        except Exception as exc:  # noqa: BLE001
            raise _forecast_http_error(exc) from exc

    def _portfolio_http_error(exc: Exception) -> HTTPException:
        """Map a portfolio failure to one HTTP status, in one place."""
        from novacontrol.novamarket.contracts import (
            PortfolioDataError,
            PortfolioNotFoundError,
            PortfolioStateError,
            PortfolioValidationError,
        )

        if isinstance(exc, PortfolioNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, (PortfolioValidationError, PortfolioDataError)):
            return HTTPException(status_code=422, detail=str(exc))
        if isinstance(exc, PortfolioStateError):
            return HTTPException(status_code=409, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    def _risk_ttl(value: object) -> float:
        from novacontrol.novamarket.contracts import RiskValidationError

        try:
            if value is None or str(value).strip() == "":
                return 24.0
            return float(str(value))
        except (TypeError, ValueError) as exc:
            raise RiskValidationError("ttl_hours must be numeric") from exc

    def _risk_http_error(exc: Exception) -> HTTPException:
        """Map a risk failure to one HTTP status, in one place."""
        from novacontrol.novamarket.contracts import (
            RiskNotFoundError,
            RiskStateError,
            RiskValidationError,
            StrategyNotFoundError,
            StrategyStateError,
            StrategyValidationError,
        )

        if isinstance(exc, StrategyNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, StrategyValidationError):
            return HTTPException(status_code=422, detail=str(exc))
        if isinstance(exc, StrategyStateError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, RiskNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, RiskValidationError):
            return HTTPException(status_code=422, detail=str(exc))
        if isinstance(exc, RiskStateError):
            return HTTPException(status_code=409, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    @app.post("/market/portfolios")
    async def market_portfolio_create(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Create and fund one simulated three-horizon portfolio."""
        try:
            return await nova.novamarket.portfolio_create(
                str(payload.get("horizon") or ""),
                name=str(payload.get("name") or ""),
                funding_wallet_id=str(payload.get("funding_wallet_id") or ""),
                capital=payload.get("capital"),
            )
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_http_error(exc) from exc

    @app.get("/market/portfolios")
    def market_portfolios(
        horizon: str = "",
        status: str = "",
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Every portfolio, newest first."""
        try:
            return nova.novamarket.portfolios_list(
                horizon=horizon or None, status=status or None, limit=limit
            )
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_http_error(exc) from exc

    @app.get("/market/portfolios/status")
    async def market_portfolio_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Portfolio counts, states and the three horizon policies."""
        return nova.novamarket.portfolio_status()

    @app.get("/market/portfolios/horizons")
    async def market_portfolio_horizons(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The three horizon policies as data."""
        return nova.novamarket.portfolio_horizons()

    @app.get("/market/portfolios/{portfolio_id}")
    async def market_portfolio(
        portfolio_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One portfolio with its valuation, holdings and constraints."""
        try:
            return nova.novamarket.portfolio_detail(portfolio_id)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_http_error(exc) from exc

    @app.get("/market/portfolios/{portfolio_id}/holdings")
    async def market_portfolio_holdings(
        portfolio_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Current holdings with weights and unpriced markers."""
        try:
            return nova.novamarket.portfolio_holdings(portfolio_id)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_http_error(exc) from exc

    @app.get("/market/portfolios/{portfolio_id}/valuation")
    async def market_portfolio_valuation(
        portfolio_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Value the book, store a snapshot and publish the result."""
        try:
            return await nova.novamarket.portfolio_valuation(portfolio_id)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_http_error(exc) from exc

    @app.get("/market/portfolios/{portfolio_id}/valuations")
    async def market_portfolio_valuations(
        portfolio_id: str,
        limit: int = 200,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The stored valuation snapshots, oldest first."""
        try:
            return nova.novamarket.portfolio_valuations(portfolio_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_http_error(exc) from exc

    @app.get("/market/portfolios/{portfolio_id}/performance")
    async def market_portfolio_performance(
        portfolio_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Measured performance, gated on enough observations for ratios."""
        try:
            return nova.novamarket.portfolio_performance(portfolio_id)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_http_error(exc) from exc

    @app.get("/market/portfolios/{portfolio_id}/decisions")
    async def market_portfolio_decisions(
        portfolio_id: str,
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The decision history with selection and sizing evidence."""
        try:
            return nova.novamarket.portfolio_decisions(portfolio_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_http_error(exc) from exc

    @app.get("/market/portfolios/{portfolio_id}/proposals")
    async def market_portfolio_proposals(
        portfolio_id: str,
        status: str = "",
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Rebalance proposals and their statuses."""
        try:
            return nova.novamarket.portfolio_proposals(
                portfolio_id, status=status or None, limit=limit
            )
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_http_error(exc) from exc

    @app.get("/market/portfolios/{portfolio_id}/lifecycle")
    async def market_portfolio_lifecycle(
        portfolio_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """The portfolio's lifecycle transitions."""
        try:
            return nova.novamarket.portfolio_lifecycle(portfolio_id)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_http_error(exc) from exc

    @app.get("/market/portfolios/{portfolio_id}/reconcile")
    async def market_portfolio_reconcile(
        portfolio_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Compare the recorded funding and realised P&L with their history."""
        try:
            return nova.novamarket.portfolio_reconcile(portfolio_id)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_http_error(exc) from exc

    @app.post("/market/portfolios/{portfolio_id}/rebalance")
    async def market_portfolio_rebalance(
        portfolio_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Propose a validated rebalance; nothing is applied."""
        try:
            return await nova.novamarket.portfolio_rebalance(
                portfolio_id, force=bool(payload.get("force", False))
            )
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_http_error(exc) from exc

    @app.post("/market/portfolios/{portfolio_id}/apply")
    async def market_portfolio_apply(
        portfolio_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Apply a validated proposal to the simulated book."""
        try:
            return await nova.novamarket.portfolio_apply(str(payload.get("proposal_id") or ""))
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_http_error(exc) from exc

    @app.post("/market/portfolios/{portfolio_id}/status")
    async def market_portfolio_set_status(
        portfolio_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Pause, resume or suspend portfolio monitoring."""
        try:
            return await nova.novamarket.portfolio_set_status(
                portfolio_id,
                str(payload.get("action") or ""),
                reason=str(payload.get("reason") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_http_error(exc) from exc

    @app.post("/market/portfolios/{portfolio_id}/close")
    async def market_portfolio_close(
        portfolio_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Liquidate and release the portfolio's capital through the ledger."""
        try:
            return await nova.novamarket.portfolio_close(
                portfolio_id, reason=str(payload.get("reason") or "")
            )
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_http_error(exc) from exc

    @app.post("/market/risk/assess")
    async def market_risk_assess(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Assess one proposed action; the response is an enforceable decision."""
        try:
            return await nova.novamarket.risk_assess(
                action=str(payload.get("action") or "apply_proposal"),
                portfolio_id=str(payload.get("portfolio_id") or ""),
                proposal_id=str(payload.get("proposal_id") or ""),
                instrument_id=str(payload.get("instrument_id") or ""),
                side=str(payload.get("side") or ""),
                notional=payload.get("notional"),
                actor=_principal,
            )
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _risk_http_error(exc) from exc

    @app.get("/market/risk/status")
    async def market_risk_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Risk counts, policies and assessment latency."""
        return nova.novamarket.risk_status()

    @app.get("/market/risk/contracts")
    async def market_risk_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The versioned risk contract."""
        return nova.novamarket.risk_contract()

    @app.get("/market/risk/decisions")
    async def market_risk_decisions(
        portfolio_id: str = "",
        outcome: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Risk decision history, newest first."""
        return nova.novamarket.risk_decisions(
            portfolio_id=portfolio_id, outcome=outcome, limit=limit
        )

    @app.get("/market/risk/decisions/{decision_id}")
    async def market_risk_decision(
        decision_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One risk decision with its evidence."""
        try:
            return nova.novamarket.risk_decision(decision_id)
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.post("/market/risk/decisions/{decision_id}/apply")
    async def market_risk_apply(
        decision_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Apply an approved decision to the exact proposal it assessed."""
        try:
            return await nova.novamarket.risk_apply_decision(decision_id)
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.get("/market/risk/portfolio/{portfolio_id}")
    async def market_risk_portfolio(
        portfolio_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Portfolio risk metrics, breaches and positions."""
        try:
            return nova.novamarket.risk_portfolio(portfolio_id)
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.get("/market/risk/portfolio/{portfolio_id}/positions")
    async def market_risk_positions(
        portfolio_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Position-level risk for one portfolio."""
        try:
            body = nova.novamarket.risk_portfolio(portfolio_id)
            return {
                "ok": True,
                "portfolio_id": portfolio_id,
                "positions": body["positions"],
                "outcome": body["outcome"],
            }
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.get("/market/risk/position/{portfolio_id}/{instrument_id}")
    async def market_risk_position(
        portfolio_id: str,
        instrument_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Risk for one held instrument."""
        try:
            return nova.novamarket.risk_position(portfolio_id, instrument_id)
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.get("/market/risk/exposure")
    async def market_risk_exposure(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Combined exposure across all portfolios."""
        return nova.novamarket.risk_exposure()

    @app.get("/market/risk/policies")
    async def market_risk_policies(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Current policies and their version history."""
        return nova.novamarket.risk_policies()

    @app.post("/market/risk/policies/{policy_id}")
    async def market_risk_policy_update(
        policy_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Create a new audited policy version."""
        try:
            raw_changes = payload.get("changes") or {}
            if not isinstance(raw_changes, dict):
                from novacontrol.novamarket.contracts import RiskValidationError

                raise RiskValidationError("changes must be an object")
            changes = dict(raw_changes)
            return await nova.novamarket.risk_update_policy(policy_id, changes, actor=_principal)
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.get("/market/risk/alerts")
    async def market_risk_alerts(
        status: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Risk alerts by lifecycle status."""
        return nova.novamarket.risk_alerts(status=status, limit=limit)

    @app.post("/market/risk/alerts/{alert_id}/acknowledge")
    async def market_risk_alert_acknowledge(
        alert_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Acknowledge an alert."""
        try:
            return await nova.novamarket.risk_acknowledge_alert(alert_id, actor=_principal)
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.post("/market/risk/alerts/{alert_id}/resolve")
    async def market_risk_alert_resolve(
        alert_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Resolve an alert."""
        try:
            return await nova.novamarket.risk_resolve_alert(alert_id, actor=_principal)
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.post("/market/risk/stress")
    async def market_risk_stress(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Run a labelled hypothetical stress scenario."""
        try:
            return await nova.novamarket.risk_stress(
                str(payload.get("portfolio_id") or ""),
                str(payload.get("scenario") or "market_decline"),
                actor=_principal,
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.get("/market/risk/stress")
    async def market_risk_stress_results(
        portfolio_id: str = "",
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Stored stress-scenario results."""
        return nova.novamarket.risk_stress_results(portfolio_id, limit)

    @app.get("/market/risk/overrides")
    async def market_risk_overrides(
        decision_id: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Risk override history."""
        return nova.novamarket.risk_overrides(decision_id, limit)

    @app.post("/market/risk/overrides")
    async def market_risk_override(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Record a time-bounded override for a reviewable decision."""
        try:
            return await nova.novamarket.risk_record_override(
                str(payload.get("decision_id") or ""),
                actor=_principal,
                reason=str(payload.get("reason") or ""),
                ttl_hours=_risk_ttl(payload.get("ttl_hours")),
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.get("/market/strategies/catalog")
    async def market_strategy_catalog(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Implemented strategy families and their documented rules."""
        return nova.novamarket.strategy_catalog()

    @app.get("/market/strategies/status")
    async def market_strategy_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Strategy counts, signals, proposals and conflicts."""
        return nova.novamarket.strategy_status()

    @app.get("/market/strategies/contracts")
    async def market_strategy_contracts(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The versioned NM-09 contract."""
        return nova.novamarket.strategy_contract()

    @app.get("/market/strategies")
    async def market_strategies(
        status: str = "",
        category: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.strategies(status=status, category=category, limit=limit)

    @app.post("/market/strategies")
    async def market_strategy_create(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.create_strategy(
                str(payload.get("catalog_id") or ""),
                name=str(payload.get("name") or ""),
                config=payload.get("config") or {},
                horizons=payload.get("horizons") or None,
                asset_classes=payload.get("asset_classes") or None,
                priority=payload.get("priority", 50),
                actor=_principal,
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.get("/market/strategies/proposals")
    async def market_strategy_proposals(
        strategy_id: str = "",
        portfolio_id: str = "",
        status: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.strategy_proposals(
            strategy_id=strategy_id, portfolio_id=portfolio_id, status=status, limit=limit
        )

    @app.get("/market/strategies/proposals/{proposal_id}")
    async def market_strategy_proposal(
        proposal_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.strategy_proposal(proposal_id)
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.post("/market/strategies/proposals/{proposal_id}/forward")
    async def market_strategy_forward(
        proposal_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.forward_strategy_proposal(proposal_id)
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.get("/market/strategies/conflicts")
    async def market_strategy_conflicts(
        limit: int = 100, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        return nova.novamarket.strategy_conflicts(limit)

    @app.get("/market/strategies/signals")
    async def market_strategy_signals(
        strategy_id: str = "",
        portfolio_id: str = "",
        signal_type: str = "",
        limit: int = 200,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.strategy_signals(
            strategy_id=strategy_id,
            portfolio_id=portfolio_id,
            signal_type=signal_type,
            limit=limit,
        )

    @app.get("/market/strategies/signals/{signal_id}")
    async def market_strategy_signal(
        signal_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.strategy_signal(signal_id)
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.post("/market/strategies/run")
    async def market_strategy_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.run_strategies(
                str(payload.get("portfolio_id") or ""),
                strategy_ids=payload.get("strategy_ids") or None,
                instrument_ids=payload.get("instrument_ids") or None,
                conflict_policy=str(payload.get("conflict_policy") or "review"),
                actor=_principal,
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.get("/market/strategies/{strategy_id}")
    async def market_strategy(
        strategy_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.strategy(strategy_id)
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.post("/market/strategies/{strategy_id}/config")
    async def market_strategy_config(
        strategy_id: str, payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.update_strategy_config(
                strategy_id,
                payload.get("changes") or {},
                actor=_principal,
                reason=str(payload.get("reason") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.get("/market/strategies/{strategy_id}/versions")
    async def market_strategy_versions(
        strategy_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.strategy_versions(strategy_id)
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.post("/market/strategies/{strategy_id}/activate")
    async def market_strategy_activate(
        strategy_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.strategy_transition(
                strategy_id,
                "activate",
                actor=_principal,
                reason=str((payload or {}).get("reason") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.post("/market/strategies/{strategy_id}/pause")
    async def market_strategy_pause(
        strategy_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.strategy_transition(
                strategy_id,
                "pause",
                actor=_principal,
                reason=str((payload or {}).get("reason") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.post("/market/strategies/{strategy_id}/suspend")
    async def market_strategy_suspend(
        strategy_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.strategy_transition(
                strategy_id,
                "suspend",
                actor=_principal,
                reason=str((payload or {}).get("reason") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.post("/market/strategies/{strategy_id}/retire")
    async def market_strategy_retire(
        strategy_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.strategy_transition(
                strategy_id,
                "retire",
                actor=_principal,
                reason=str((payload or {}).get("reason") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.post("/market/strategies/{strategy_id}/degrade")
    async def market_strategy_degrade(
        strategy_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.strategy_transition(
                strategy_id,
                "degrade",
                actor=_principal,
                reason=str((payload or {}).get("reason") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.post("/market/strategies/{strategy_id}/evaluate")
    async def market_strategy_evaluate(
        strategy_id: str, payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.evaluate_strategy(
                strategy_id,
                str(payload.get("portfolio_id") or ""),
                instrument_ids=payload.get("instrument_ids") or None,
                actor=_principal,
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.get("/market/strategies/{strategy_id}/signals")
    async def market_strategy_strategy_signals(
        strategy_id: str, limit: int = 200, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.strategy_signals(strategy_id=strategy_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.get("/market/strategies/{strategy_id}/performance")
    async def market_strategy_performance(
        strategy_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.strategy_performance(strategy_id)
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.get("/market/strategies/{strategy_id}/lifecycle")
    async def market_strategy_lifecycle(
        strategy_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.strategy_lifecycle(strategy_id)
        except Exception as exc:  # noqa: BLE001
            raise _risk_http_error(exc) from exc

    @app.get("/market/data/status")
    async def market_data_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Market-data repository, providers, ingestion, cache and verification."""
        return nova.novamarket.market_data_status()

    @app.get("/market/data/providers")
    async def market_data_providers(
        probe: bool = False, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Installed adapters, declared limits, measured health and verified operations."""
        try:
            return await nova.novamarket.market_providers(probe=probe)
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _market_http_error(exc) from exc

    @app.get("/market/instruments")
    async def market_instruments(
        asset_class: str | None = None,
        exchange: str | None = None,
        limit: int = 100,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The registered canonical instrument catalogue."""
        try:
            return nova.novamarket.market_instruments(
                asset_class=asset_class,
                exchange=exchange,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _market_http_error(exc) from exc

    @app.post("/market/instruments")
    async def market_instrument_register(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Register one canonical instrument identity (MIC:SYMBOL)."""
        try:
            return nova.novamarket.register_market_instrument(payload)
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _market_http_error(exc) from exc

    @app.post("/market/instruments/lookup")
    async def market_instrument_lookup(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Resolve and register instrument metadata through one provider."""
        try:
            return await nova.novamarket.lookup_market_instrument(payload)
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _market_http_error(exc) from exc

    @app.post("/market/universe/sync")
    async def market_universe_sync(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Register the public NSE catalogue and its BSE listings in bulk."""
        try:
            return await nova.novamarket.sync_market_universe(
                include_bse=bool((payload or {}).get("include_bse", True))
            )
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _market_http_error(exc) from exc

    @app.post("/market/data/ingest")
    async def market_data_ingest(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Run one bounded collection cycle and report exactly what arrived."""
        body = payload or {}
        instrument_ids = body.get("instrument_ids")
        intervals = body.get("intervals")
        include_quotes = body.get("include_quotes")
        try:
            return await nova.novamarket.ingest_market_data(
                instrument_ids=(
                    [str(item) for item in instrument_ids]
                    if isinstance(instrument_ids, list)
                    else None
                ),
                intervals=(
                    [str(item) for item in intervals] if isinstance(intervals, list) else None
                ),
                trigger=str(body.get("trigger") or "api"),
                force_quotes=bool(body.get("force_quotes", False)),
                include_quotes=(include_quotes if isinstance(include_quotes, bool) else None),
            )
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _market_http_error(exc) from exc

    @app.get("/market/data/quote/{instrument_id}")
    async def market_data_quote(
        instrument_id: str,
        provider: str | None = None,
        refresh: bool = False,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The latest reconciled observation, with freshness and provenance."""
        try:
            return await nova.novamarket.market_quote(
                instrument_id, provider=provider, refresh=refresh
            )
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _market_http_error(exc) from exc

    @app.get("/market/data/history/{instrument_id}")
    async def market_data_history(
        instrument_id: str,
        provider: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        ascending: bool = False,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Paginated price observations in a bounded window."""
        try:
            return nova.novamarket.market_history(
                instrument_id,
                provider=provider,
                start=_parse_market_datetime(start, "start"),
                end=_parse_market_datetime(end, "end"),
                limit=limit,
                offset=offset,
                ascending=ascending,
            )
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _market_http_error(exc) from exc

    @app.get("/market/data/bars/{instrument_id}")
    async def market_data_bars(
        instrument_id: str,
        interval: str | None = None,
        provider: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        ascending: bool = False,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Paginated validated OHLCV bars for one interval."""
        try:
            return nova.novamarket.market_bars(
                instrument_id,
                interval=interval,
                provider=provider,
                start=_parse_market_datetime(start, "start"),
                end=_parse_market_datetime(end, "end"),
                limit=limit,
                offset=offset,
                ascending=ascending,
            )
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _market_http_error(exc) from exc

    # ── OrderBlock and TradingWatch (NM-14 workbenches) ──────────────────────
    # Both screens read the SAME normalized OHLCV bars as NM-02. They do not
    # fetch, retry, synthesise or execute anything; coverage and data freshness
    # travel with every response.
    @app.get("/market/screener")
    def market_screener(
        exchange: str = "all",
        cap: str = "all",
        index: str = "all",
        sector: str = "all",
        sort: str = "default",
        movers: str = "all",
        query: str = "",
        available_only: bool = False,
        limit: int = 100,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Market Overview rows with exchange, index, cap and mover filters."""
        try:
            return nova.novamarket.market_screener(
                exchange=exchange,
                cap=cap,
                index=index,
                sector=sector,
                sort=sort,
                movers=movers,
                query=query,
                available_only=available_only,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _market_http_error(exc) from exc

    @app.get("/market/orderblock")
    async def market_orderblock(
        interval: str = "1d",
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Ranked structure boards, X-Ray, sector flow, liquidity and coverage."""
        try:
            return nova.novamarket.market_orderblock(interval=interval, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - the mapper names the status
            raise _market_http_error(exc) from exc

    @app.get("/market/orderblock/{instrument_id}")
    async def market_orderblock_instrument(
        instrument_id: str,
        interval: str = "1d",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """One instrument's closed-candle structure, blocks, levels and chart."""
        try:
            return nova.novamarket.market_orderblock_instrument(instrument_id, interval=interval)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise _market_http_error(exc) from exc

    @app.get("/market/tradingwatch")
    async def market_tradingwatch(
        interval: str = "1d",
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Momentum radar, zones, heatmap, flow and unsupported-options state."""
        try:
            return nova.novamarket.market_tradingwatch(interval=interval, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise _market_http_error(exc) from exc

    @app.get("/market/tradingwatch/watchlists")
    async def market_tradingwatch_watchlists(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Watchlists with their latest stored price and freshness."""
        try:
            return nova.novamarket.market_tradingwatch_watchlists()
        except Exception as exc:  # noqa: BLE001
            raise _market_http_error(exc) from exc

    @app.get("/market/tradingwatch/replay/{instrument_id}")
    async def market_tradingwatch_replay(
        instrument_id: str,
        end_date: str | None = None,
        interval: str = "1d",
        limit: int = 180,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Historical replay cut at `end_date`; no future-bar input."""
        try:
            return nova.novamarket.market_tradingwatch_replay(
                instrument_id,
                end_date=end_date,
                interval=interval,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise _market_http_error(exc) from exc

    # ── NovaForge: gaming workspace, session foundation, telemetry, Guardian ──
    # Every route here reads or drives the SESSION MANAGER, which is the only
    # source of session state. Nothing in this block decides anything about a
    # game: NF-02 observes one validated window and measures the run, and the
    # routes say that plainly rather than dressing an observation up as gameplay.
    @app.get("/novaforge/status")
    async def novaforge_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Readiness, the saved mission draft, and the active session if any."""
        manager = nova.novaforge
        active = manager.active()
        return {
            "status": manager.status().to_dict(),
            "mission": manager.saved_mission(),
            "active": manager.describe(active) if active is not None else None,
        }

    # ── NF-12: the game library (discovery + verified launch) ────────────
    # These are the user-facing doors to the NF-11 catalog and lifecycle
    # service. Discovery, resolution, the resource gate and the process/window
    # identity check all stay in that service; these handlers only shape the
    # request and map a refusal to its HTTP status.
    @app.get("/novaforge/library")
    async def novaforge_library(
        q: str = "", _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Discovered games, launchability evidence and this process's own claim."""
        return await nova.game_library(q)

    @app.get("/novaforge/library/resolve")
    async def novaforge_library_resolve(
        name: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One name's catalog status, without preparing or launching anything."""
        return await nova.game_library_resolve(name)

    @app.post("/novaforge/library/launch")
    async def novaforge_library_launch(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Preview a launch, or execute the exact plan its token approved.

        Without a token this is READ-ONLY: it resolves the name, measures the
        resource gate and returns a plan. With the token that plan minted it
        starts the game and verifies the process and window.
        """
        command = str(payload.get("command") or payload.get("name") or "").strip()
        if not command:
            raise HTTPException(status_code=422, detail="A 'name' or 'command' is required.")
        token = str(payload.get("approval_token") or "").strip() or None
        try:
            if token:
                return await nova.execute_game_launch(command, approval_token=token)
            return await nova.plan_game_launch(command)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/novaforge/library/close")
    async def novaforge_library_close(
        payload: dict[str, Any] | None = None, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Close the instance this process started, confirming that it exited."""
        try:
            return await nova.close_game_launch(
                launch_id=str((payload or {}).get("launch_id") or "")
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/novaforge/library/attach")
    async def novaforge_library_attach(
        payload: dict[str, Any] | None = None, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Report a game that was already running; it is never closed here."""
        try:
            return await nova.attach_game(str((payload or {}).get("name") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/novaforge/windows")
    async def novaforge_windows(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Open windows for the target picker.

        ``available=False`` is a real answer here: it means the OS probe could
        not read windows (or capture is unsupported), which is a different fact
        from "no windows are open".
        """
        return await nova.novaforge.windows()

    @app.get("/novaforge/mission")
    async def novaforge_mission(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.novaforge.saved_mission()

    @app.post("/novaforge/mission")
    async def novaforge_save_mission(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Persist the mission draft and report its field-level validation.

        A draft may be incomplete: saving one does not require it to be
        runnable, because losing what a person typed is worse than storing a
        blank objective. Starting a session is where validation is enforced.
        """
        return nova.novaforge.save_mission(payload)

    @app.post("/novaforge/validate")
    async def novaforge_validate(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Validate fields AND the live target window, starting nothing."""
        return await nova.novaforge.validate(payload)

    @app.get("/novaforge/sessions")
    async def novaforge_sessions(
        limit: int = 20, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Stored session history, newest first (bounded by ``limit``)."""
        sessions = nova.novaforge.history(limit=max(1, min(limit, 100)))
        return {"sessions": sessions, "count": len(sessions)}

    @app.post("/novaforge/sessions")
    async def novaforge_start(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Validate the mission and start its real observation session."""
        try:
            return await nova.novaforge.start(payload)
        except MissionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SessionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SessionTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/novaforge/sessions/{session_id}")
    async def novaforge_session(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        record = nova.novaforge.get(session_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")
        return nova.novaforge.describe(record)

    async def _novaforge_control(session_id: str, action: str) -> dict[str, Any]:
        """Drive one lifecycle move; a refused move is a 409, a missing id 404."""
        if action in {"pause", "stop", "cancel", "emergency-stop"}:
            # A running closed loop must not keep proposing actions against a
            # session that is being paused or closed underneath it.
            nova.cancel_novaforge_loop(session_id)
        try:
            if action == "pause":
                return await nova.novaforge.pause(session_id)
            if action == "resume":
                return await nova.novaforge.resume(session_id)
            if action == "cancel":
                return await nova.novaforge.cancel(session_id)
            if action == "emergency-stop":
                return await nova.novaforge.emergency_stop(session_id)
            return await nova.novaforge.stop(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
        except MissionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SessionTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/novaforge/sessions/{session_id}/pause")
    async def novaforge_pause(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        return await _novaforge_control(session_id, "pause")

    @app.post("/novaforge/sessions/{session_id}/resume")
    async def novaforge_resume(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        return await _novaforge_control(session_id, "resume")

    @app.post("/novaforge/sessions/{session_id}/stop")
    async def novaforge_stop(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        return await _novaforge_control(session_id, "stop")

    @app.post("/novaforge/sessions/{session_id}/cancel")
    async def novaforge_cancel(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Abort the loop now and release input; idempotent once terminal."""
        return await _novaforge_control(session_id, "cancel")

    @app.post("/novaforge/sessions/{session_id}/emergency-stop")
    async def novaforge_emergency_stop(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Guardian emergency stop: bounded abort, cleanup, confirmed outcome."""
        return await _novaforge_control(session_id, "emergency-stop")

    @app.get("/novaforge/capabilities")
    async def novaforge_capabilities(
        title: str = "", _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """What this build can actually do, per capability, with the blocker named.

        Availability is a property of the machine and the wiring, not a request:
        a host without a capture worker reports no capture, and a build without
        an injector reports no action path, rather than claiming either because
        the class exists.

        ``observation_ladder`` reports the ten separate capabilities between a
        discovered title and an accepted observation, each decided from its own
        evidence. ``title`` names a catalog title to ask about; without one the
        ladder reports the observation actually bound to a target.
        """
        capabilities = nova.novaforge.capabilities()
        # The closed loop is composed by the APPLICATION around the manager, so
        # its capability is read from the application: a manager swapped at
        # runtime (a test seam, a rebuild) must not be able to hide a loop that
        # exists, or advertise one that does not.
        capabilities["decision_pipeline"] = nova._novaforge_loop_capability()
        # NF-09: visual interpretation has its own row, so a caller does not have
        # to dig into the loop's observation block to learn whether a model can
        # see, which one, and why not.
        capabilities["vision_interpretation"] = nova._novaforge_vision_capability()
        # NF-23: the observation ladder, so a caller never has to infer "a window
        # exists" from "a frame was written" (or either from a discovered title).
        capabilities["observation_ladder"] = await nova.novaforge.observation_ladder(title)
        return {"capabilities": capabilities, "policy": capabilities["action_policy"]}

    @app.get("/novaforge/trials")
    async def novaforge_trials(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Policy trial rows and aggregates recorded by this process (read-only).

        Every row is read from a finished loop's own result, and the aggregates
        come from the same evaluation module the acceptance script uses. A
        refused, failed or inconclusive run is kept: a history that only shows
        successes cannot be used to judge a policy.
        """
        return nova.novaforge_trial_history()

    # ── NovaForge NF-15: unified game observation ─────────────────────────
    # An observation is NOT a session: it binds one window, captures it on a
    # bounded schedule and publishes validated frames. It sends no input and
    # cannot plan, approve or execute anything. A target that is minimized,
    # hidden, gone or replaced pauses it — the routes below never resume by
    # themselves.

    @app.get("/novaforge/observation")
    async def novaforge_observation(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The observation pipeline's state, measurements and last records."""
        return nova.novaforge.observation_status()

    @app.post("/novaforge/observation/start")
    async def novaforge_observation_start(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Bind a target and start observing; a refusal names the reason.

        Body: ``title`` (or ``target_title``), optional ``game``, ``pid``,
        ``start_time``, ``process`` and ``interval`` (seconds; clamped to the
        observer's bounds). A pinned identity is verified before binding.
        """
        return await nova.novaforge.start_observation(payload)

    @app.post("/novaforge/observation/pause")
    async def novaforge_observation_pause(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Pause a user-requested observation; this pause is never auto-cleared."""
        return await nova.novaforge.pause_observation()

    @app.post("/novaforge/observation/resume")
    async def novaforge_observation_resume(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Resume a user pause after re-reading the pinned target."""
        return await nova.novaforge.resume_observation()

    @app.post("/novaforge/observation/stop")
    async def novaforge_observation_stop(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Stop the observation schedule and clean up (idempotent)."""
        return await nova.novaforge.stop_observation()

    @app.post("/novaforge/observation/observe")
    async def novaforge_observation_observe(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Capture and validate one frame now; a concurrent read is ``busy``."""
        return await nova.novaforge.observe_frame_once()

    @app.get("/novaforge/observation/frame")
    async def novaforge_observation_frame(
        _principal: str = Depends(require_auth),
    ) -> Any:
        """Serve the newest VALIDATED observation frame (PNG).

        A rejected frame is deleted, so this can only answer with pixels that
        passed validation. No frame yet is a 404 — an empty image would look
        like a rendering bug instead of "nothing was observed".
        """
        path = nova.novaforge.observation_frame_path()
        if path is None or not path.is_file():
            raise HTTPException(
                status_code=404, detail="No validated observation frame is available."
            )
        return FileResponse(
            path,
            media_type="image/png",
            headers={
                **_never_cache_headers(),
                "Content-Disposition": "inline",
            },
        )

    # ── NovaForge NF-16: game state understanding ──────────────────────────
    # A state is a READING of one validated observation, not an understanding of
    # the game and not a plan. It never captures, clicks or acts: the frame it
    # reads comes from NF-15's observer, and every refusal names its reason.

    @app.get("/novaforge/state")
    async def novaforge_state(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The state reader's last reading, bounded history and measurements."""
        return nova.novaforge.state.status()

    @app.post("/novaforge/state/analyze")
    async def novaforge_state_analyze(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Read one state from the newest validated observation.

        Body (optional): ``profile_id`` pins a perception profile by id; an
        omitted key leaves the automatic title match in charge. A stale frame,
        a missing frame, a superseded observation or a second concurrent reading
        is a refusal carrying ``reason_code`` — never a fabricated state.
        """
        body = payload or {}
        pinned = str(body.get("profile_id") or "").strip()
        outcome = await nova.novaforge.state.analyze(
            profile_id=(pinned if "profile_id" in body else None)
        )
        return outcome.to_dict()

    @app.post("/novaforge/state/reset")
    async def novaforge_state_reset(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Forget the comparison baseline; the bounded history is kept."""
        return nova.novaforge.state.reset()

    @app.get("/novaforge/state/profiles")
    async def novaforge_state_profiles(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The perception profiles this build ships, rejected ones included."""
        return nova.novaforge.state.profiles()

    # ── NovaForge NF-17: objective and progress understanding ──────────────
    # Objectives are derived from the states NF-16 produces; there is no separate
    # capture here and nothing is executed. Completion is only ever reported with
    # the evidence that established it, and a vanished objective stays unverified.

    @app.get("/novaforge/objectives")
    async def novaforge_objectives(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The current objective picture, bounded history and diagnostics."""
        return nova.novaforge.objective_state.status()

    @app.post("/novaforge/objectives/reset")
    async def novaforge_objectives_reset(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Forget the current objectives and ordering baseline; history is kept."""
        return nova.novaforge.objective_state.reset()

    @app.get("/novaforge/objectives/profiles")
    async def novaforge_objective_profiles(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The objective rules this build ships, rejected profiles included."""
        return nova.novaforge.objective_state.profiles()

    # ── NovaForge NF-18: game knowledge and memory ──────────────────────────
    # Knowledge is scoped to ONE game and every record names its evidence. This
    # surface only stores and retrieves beliefs: it plans nothing, sends nothing
    # and never turns a transient observation into a fact.

    @app.get("/novaforge/knowledge")
    async def novaforge_knowledge(
        game: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Knowledge status, per-game counts and the transient session memory."""
        return await nova.novaforge.knowledge_status(game=game.strip())

    @app.get("/novaforge/knowledge/records")
    async def novaforge_knowledge_records(
        game: str = "",
        category: str = "",
        status: str = "",
        q: str = "",
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Records for one game; filters: `category`, `status`, `q`, `limit`."""
        if not game.strip():
            raise HTTPException(
                status_code=422,
                detail="game is required: knowledge is scoped to one game",
            )
        return await nova.novaforge.knowledge_records(
            game=game.strip(),
            category=category.strip() or None,
            status=status.strip() or None,
            query=q,
            limit=max(1, min(int(limit), 200)),
        )

    @app.post("/novaforge/knowledge")
    async def novaforge_record_knowledge(
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Record one explicit, evidence-bearing knowledge candidate.

        A refused candidate is a 422 with the reason: an unparseable evidence
        reference or an unknown category must never be silently dropped, because
        the caller would then believe a claim was stored when it was not.
        """
        try:
            candidate = candidate_from_payload(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return await nova.novaforge.record_knowledge(candidate)

    @app.post("/novaforge/knowledge/{knowledge_id}/revalidate")
    async def novaforge_revalidate_knowledge(
        knowledge_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Re-check one stale/contradicted record with fresh evidence."""
        body = payload or {}
        rows = body.get("evidence") or []
        if not isinstance(rows, list):
            raise HTTPException(status_code=422, detail="evidence must be a list")
        evidence: list[KnowledgeEvidence] = []
        for index, row in enumerate(rows):
            parsed = KnowledgeEvidence.from_dict(row) if isinstance(row, dict) else None
            if parsed is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"evidence[{index}] is invalid: it needs a known source, "
                    + "method and summary",
                )
            evidence.append(parsed)
        if not evidence:
            raise HTTPException(
                status_code=422,
                detail="evidence is required: revalidation needs a fresh observation",
            )
        return await nova.novaforge.revalidate_knowledge(
            knowledge_id, evidence, game=str(body.get("game") or "").strip()
        )

    @app.post("/novaforge/sessions/{session_id}/objective")
    async def novaforge_set_objective(
        session_id: str, payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Attach a checkable objective to a live session (nothing is executed)."""
        try:
            return nova.novaforge.set_objective(session_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
        except MissionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SessionTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/novaforge/sessions/{session_id}/actions/plan")
    async def novaforge_plan_action(
        session_id: str, payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Validate and digest-bind one gameplay action. Sends NOTHING.

        A denied plan is a 200 with `status: denied` and the Guardian's reason:
        the refusal is the answer the UI has to show, not an error to swallow.
        """
        try:
            return nova.novaforge.plan_action(session_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
        except MissionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SessionTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/novaforge/sessions/{session_id}/actions/execute")
    async def novaforge_execute_action(
        session_id: str, payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Execute ONE digest-bound proposal, after revalidating target and policy."""
        try:
            return await nova.novaforge.execute_action(session_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
        except MissionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/novaforge/sessions/{session_id}/control")
    async def novaforge_control_status(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """The NF-19 control interface's state, bounds, held inputs and last result."""
        try:
            return nova.novaforge.control_status(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc

    @app.post("/novaforge/sessions/{session_id}/control")
    async def novaforge_control_execute(
        session_id: str, payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Run ONE typed, bounded input action; a refusal is a 200 with its code.

        Body: `kind` (`key_press`/`key_release`/`key_hold`/`mouse_move`/
        `mouse_press`/`mouse_release`/`mouse_hold`/`wait`/`focus`) plus the
        kind's own parameters (`key`, `button`, `x`, `y`, `duration_ms`,
        `deadline_ms`). Completion is the input operation's contract, never a
        verified effect --- except `focus`, whose worker re-reads the foreground
        window and refuses when the target did not arrive in front.
        """
        try:
            return await nova.novaforge.control_execute(session_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
        except MissionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/novaforge/sessions/{session_id}/control/cancel")
    async def novaforge_control_cancel(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Ask the in-flight control action to stop and release what it holds."""
        try:
            return await nova.novaforge.cancel_control_action(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc

    @app.post("/novaforge/sessions/{session_id}/control/release")
    async def novaforge_control_release(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Release every input the control interface still holds (idempotent)."""
        try:
            return await nova.novaforge.release_control_inputs(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc

    @app.get("/novaforge/sessions/{session_id}/plan")
    async def novaforge_plan_status(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """The NF-20 planner's state: declared rules, current plan, bounds, gate.

        A plan awaiting approval is served WITH a freshly minted single-use
        approval token, so the review the operator sees and the approval they
        give refer to this plan even after a reload.
        """
        try:
            return nova.forge_plan_receipt(nova.novaforge.planning_status(session_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc

    @app.post("/novaforge/sessions/{session_id}/plan")
    async def novaforge_plan_build(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Build ONE bounded plan from the session's verified evidence; nothing is sent.

        A receipt carries `plan.status`: `awaiting_approval` means approval is
        still required (and the receipt carries the token that approval needs),
        `insufficient_information` / `no_action` mean no step could be justified,
        and every receipt carries `effect_verified: false`.
        """
        try:
            return nova.forge_plan_receipt(await nova.novaforge.plan_actions(session_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc

    @app.post("/novaforge/sessions/{session_id}/plan/execute")
    async def novaforge_plan_execute(
        session_id: str, payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Execute ONE approved plan, step by step, through the NF-19 control path.

        Body: `plan_id`, `approved: true` AND `approval_token` from the plan
        preview this approval refers to. Without the token nothing runs (403):
        the flag alone is a request, the token is the approval. A plan that needs
        no approval (or one that already ran) is unaffected, and a refusal is a
        result, not a 500.
        """
        try:
            return await nova.execute_forge_plan(session_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
        except MissionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/novaforge/sessions/{session_id}/plan/cancel")
    async def novaforge_plan_cancel(
        session_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Cancel the session's pending or running plan (idempotent)."""
        try:
            return await nova.novaforge.cancel_plan(
                session_id, str((payload or {}).get("plan_id") or "")
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc

    @app.post("/novaforge/sessions/{session_id}/plan/replan")
    async def novaforge_plan_replan(
        session_id: str, payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Regenerate a stopped plan from FRESH evidence, within its replan bound.

        Body: `plan_id`, optional `reason` from the declared trigger set. The
        previous plan is invalidated before the new one exists, so it can never
        be executed afterwards by an id that was still lying around, and the
        fresh plan is served with the approval token it needs.
        """
        try:
            return nova.forge_plan_receipt(await nova.novaforge.replan_actions(session_id, payload))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
        except MissionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ── NovaForge NF-21: outcome verification and bounded recovery ──────────
    # A verdict is read from a FRESH observation of the declared observable, never
    # from "the input operation completed". These routes can observe, invalidate a
    # plan, ask for a new plan, pause or stop a session — and they contain no path
    # that sends input, so no recovery here can repeat an action.

    @app.get("/novaforge/sessions/{session_id}/outcomes")
    async def novaforge_outcomes(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """NF-21 state: declared rules, the last verification, outcomes, bounds."""
        try:
            return nova.novaforge.verification_status(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc

    @app.post("/novaforge/sessions/{session_id}/outcomes/verify")
    async def novaforge_outcomes_verify(
        session_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Verify a plan's dispatched steps against fresh readings.

        Body (optional): `plan_id` (the newest plan by default) and `max_steps`.
        Each step gets a contract from a declared rule or from verified knowledge
        that declares its own observable; a step neither covers is reported
        `not_verifiable`. Missing evidence is `outcome_unknown` — never a failure,
        and never a success.
        """
        try:
            return await nova.novaforge.verify_outcomes(session_id, payload or {})
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
        except MissionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/novaforge/sessions/{session_id}/outcomes/recover")
    async def novaforge_outcomes_recover(
        session_id: str, payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Carry out ONE outcome's recorded recovery decision, within its bounds.

        Body: `outcome_id`, optionally `approve: true`. A side-effecting response
        (replan, pause, stop) is refused without that approval and names it; a
        fresh-observation response re-reads instead of repeating the action.
        """
        try:
            return await nova.novaforge.recover_outcome(session_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
        except MissionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/novaforge/sessions/{session_id}/outcomes/cancel")
    async def novaforge_outcomes_cancel(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Ask an in-flight verification to stop at its next checkpoint."""
        try:
            return await nova.novaforge.cancel_verification(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc

    # ── NovaForge NF-22: the first bounded autonomous gameplay session ─────
    # The supervisor behind these routes owns no window, captures no frame and
    # injects no input: each cycle observes, interprets, reads the objective,
    # plans, is approved, executes through NF-19, is verified by NF-21 and then
    # continues or stops inside its declared budgets. Approval is the OPERATOR's
    # (nothing here supplies one), and a run ends only at a terminal outcome — it
    # is never restarted by a route.

    @app.get("/novaforge/sessions/{session_id}/autonomy")
    async def novaforge_autonomy_status(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """The run's phase, objective, plan, budgets, last verdict and bounds."""
        try:
            return nova.novaforge.autonomy_status(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc

    @app.post("/novaforge/sessions/{session_id}/autonomy/start")
    async def novaforge_autonomy_start(
        session_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Begin ONE bounded autonomous run over a live session.

        Body: `approved: true` (the operator's own approval — the run sends input
        to a game), optional `note`, `task` and `limits` (each limit may be
        narrowed, never widened). Without the approval this answers
        `code: approval_required` and starts nothing.
        """
        body = dict(payload or {})
        try:
            return await nova.novaforge.start_autonomy(
                session_id,
                approved=body.get("approved") is True,
                note=str(body.get("note") or ""),
                task=str(body.get("task") or ""),
                limits=body.get("limits") if isinstance(body.get("limits"), dict) else None,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
        except AutonomyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/novaforge/sessions/{session_id}/autonomy/pause")
    async def novaforge_autonomy_pause(
        session_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Pause the run: the session pauses and the next cycle re-observes."""
        reason = (
            str((payload or {}).get("reason") or "operator")
            if isinstance(payload, dict)
            else "operator"
        )
        return await nova.novaforge.pause_autonomy(session_id, reason=reason)

    @app.post("/novaforge/sessions/{session_id}/autonomy/resume")
    async def novaforge_autonomy_resume(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Resume a paused run. A run that ended is never restarted."""
        return await nova.novaforge.resume_autonomy(session_id)

    @app.post("/novaforge/sessions/{session_id}/autonomy/cancel")
    async def novaforge_autonomy_cancel(
        session_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Cancel the run before its next stage, then let it end cleanly."""
        reason = (
            str((payload or {}).get("reason") or "operator")
            if isinstance(payload, dict)
            else "operator"
        )
        return await nova.novaforge.cancel_autonomy(session_id, reason=reason)

    @app.post("/novaforge/sessions/{session_id}/autonomy/approve")
    async def novaforge_autonomy_approve(
        session_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Answer a run parked in WAITING_FOR_APPROVAL (the per-plan policy).

        Body: `approved: true|false`, optional `note`. One answer covers exactly
        one plan; the next plan parks again.
        """
        body = dict(payload or {})
        return await nova.novaforge.approve_autonomy_plan(
            session_id, approved=body.get("approved") is True, note=str(body.get("note") or "")
        )

    @app.get("/novaforge/autonomy/trace")
    async def novaforge_autonomy_trace(
        session_id: str = "", _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """The whole orchestration history: phases, evidence, plans and verdicts."""
        return nova.novaforge.autonomy_trace(session_id)

    @app.post("/novaforge/sessions/{session_id}/verify")
    async def novaforge_verify(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Ask the independent verifier for the objective's current verdict."""
        try:
            return await nova.novaforge.verify_objective(session_id, reason="api")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc

    @app.post("/novaforge/sessions/{session_id}/loop")
    async def novaforge_loop_run(
        session_id: str, payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Run the closed loop once: observe, decide, plan, execute, verify.

        Every action still travels the session's own plan/execute path, so a
        Guardian denial or a stopped session ends the run with that reason
        rather than being retried around.
        """
        try:
            return await nova.run_novaforge_loop(session_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
        except MissionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SessionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/novaforge/sessions/{session_id}/loop/cancel")
    async def novaforge_loop_cancel(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Ask a running closed loop to stop before its next action."""
        return nova.cancel_novaforge_loop(session_id)

    @app.post("/novaforge/sessions/{session_id}/vision/interpret")
    async def novaforge_vision_interpret(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Describe the target's current frame with the vision model (read-only).

        Returns the typed interpretation plus the measured evidence refs it sits
        beside. Nothing here can plan, approve or execute an action, and the
        answer never becomes an objective verdict.
        """
        try:
            return await nova.interpret_novaforge_frame(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
        except MissionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/novaforge/sessions/{session_id}/verification")
    async def novaforge_verification(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """The verdict, its evidence references and what this session could check."""
        try:
            return nova.novaforge.verification_payload(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc

    @app.get("/novaforge/sessions/{session_id}/telemetry")
    async def novaforge_telemetry(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Measured telemetry and Guardian status for one session."""
        try:
            return nova.novaforge.telemetry_payload(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc

    def _novaforge_filters(value: str | None) -> list[str] | None:
        """A comma-separated filter parameter, or None when nothing was asked."""
        if not value:
            return None
        items = [item.strip() for item in value.split(",") if item.strip()]
        return items or None

    @app.get("/novaforge/sessions/{session_id}/timeline")
    async def novaforge_timeline(
        session_id: str,
        limit: int = 100,
        after_seq: int = 0,
        types: str | None = None,
        categories: str | None = None,
        sources: str | None = None,
        severities: str | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """One page of a session's evidence timeline. READ-ONLY.

        This route inspects persisted evidence only: it cannot start, resume,
        stop, or drive a session, and NF-03 has no input path. An unknown filter
        value is a 422 naming the allowed values rather than a silently empty
        page that looks like "nothing happened".
        """
        try:
            return nova.novaforge.timeline_payload(
                session_id,
                limit=limit,
                after_seq=after_seq,
                types=_novaforge_filters(types),
                categories=_novaforge_filters(categories),
                sources=_novaforge_filters(sources),
                severities=_novaforge_filters(severities),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}") from exc
        except TimelineQueryError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/novaforge/sessions/{session_id}/timeline/{seq}")
    async def novaforge_timeline_event(
        session_id: str, seq: int, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One timeline event with its evidence resolved; read-only."""
        try:
            return nova.novaforge.timeline_event(session_id, seq)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"Unknown session or timeline event: {session_id} #{seq}"
            ) from exc

    @app.get("/novaforge/timeline/compare")
    async def novaforge_timeline_compare(
        left: str, right: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Compare two persisted sessions from their recorded evidence."""
        try:
            return nova.novaforge.compare_sessions(left, right)
        except KeyError as exc:
            missing = left if nova.novaforge.get(left) is None else right
            raise HTTPException(status_code=404, detail=f"Unknown session: {missing}") from exc

    @app.get("/novaforge/frames/{session_id}")
    async def novaforge_frame(
        session_id: str,
        seq: int | None = None,
        _principal: str = Depends(require_auth),
    ) -> Any:
        """Serve one captured frame (PNG), never cached and never guessed.

        ``seq`` selects a recorded frame; without it the newest one is served.
        The path comes from the session record and is confined to the session's
        own frame directory by the manager, so this route cannot be used to read
        an arbitrary file. A missing frame is a 404, not an empty image: a broken
        image looks like a rendering bug, a 404 is the truth.
        """
        path = nova.novaforge.frame_path(session_id, seq=seq)
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="No such frame for this session.")
        return FileResponse(
            path,
            media_type="image/png",
            headers={
                **_never_cache_headers(),
                "Content-Disposition": "inline",
            },
        )

    # ── NovaForge NF-04: Forge Benchmark Lab ──────────────────────────────
    # A benchmark run is ONE real observation session plus a persisted result.
    # These routes never decide a success: the lab reads the session's own
    # telemetry and timeline, and a completed run says "data complete", never
    # "objective achieved".
    @app.get("/novaforge/benchmarks/scenarios")
    async def novaforge_benchmark_scenarios(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The scenario catalog, with supported/unsupported status and metrics."""
        return nova.novaforge_benchmarks.scenarios()

    @app.get("/novaforge/benchmarks/scenarios/{scenario_id}")
    async def novaforge_benchmark_scenario(
        scenario_id: str,
        version: int | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """One scenario's requirements, verifier availability and metric definitions."""
        try:
            return nova.novaforge_benchmarks.scenario(scenario_id, version)
        except BenchmarkNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/novaforge/benchmarks/preflight")
    async def novaforge_benchmark_preflight(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Validate a scenario, its budget and the live target window. Starts nothing."""
        return await nova.novaforge_benchmarks.preflight(payload)

    @app.get("/novaforge/benchmarks/runs")
    async def novaforge_benchmark_runs(
        limit: int = 20,
        scenario: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Persisted benchmark runs, newest first (every attempt is listed)."""
        return nova.novaforge_benchmarks.history(limit=limit, scenario_id=scenario)

    @app.post("/novaforge/benchmarks/runs")
    async def novaforge_benchmark_start(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Validate and start one bounded observation run."""
        try:
            return await nova.novaforge_benchmarks.start(payload)
        except BenchmarkValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except BenchmarkConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/novaforge/benchmarks/runs/{run_id}")
    async def novaforge_benchmark_run(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One benchmark run: configuration, metrics, evidence and limitations."""
        run = nova.novaforge_benchmarks.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Unknown benchmark run: {run_id}")
        return run

    @app.post("/novaforge/benchmarks/runs/{run_id}/cancel")
    async def novaforge_benchmark_cancel(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Abort the run's session through its stop path; idempotent once terminal."""
        try:
            return await nova.novaforge_benchmarks.cancel(run_id)
        except BenchmarkNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def _benchmark_ids(value: str) -> list[str]:
        ids = [item.strip() for item in str(value or "").split(",") if item.strip()]
        # Bounded: a comparison is a reading of a few runs, not a bulk export.
        return list(dict.fromkeys(ids))[:10]

    @app.get("/novaforge/benchmarks/compare")
    async def novaforge_benchmark_compare(
        left: str,
        right: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Compare compatible runs; query params accept comma-separated run ids."""
        try:
            return nova.novaforge_benchmarks.compare(_benchmark_ids(left), _benchmark_ids(right))
        except BenchmarkValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except BenchmarkNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/novaforge/benchmarks/aggregate")
    async def novaforge_benchmark_aggregate(
        scenario: str,
        version: int | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Per-scenario aggregates over eligible runs, with every exclusion counted."""
        try:
            return nova.novaforge_benchmarks.aggregate(scenario, version)
        except BenchmarkNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ── NF-05: controlled game evaluation ───────────────────────────────
    @app.get("/novaforge/games")
    async def novaforge_games(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The game/environment compatibility registry and its honest counts."""
        return nova.novaforge_evaluations.games()

    @app.get("/novaforge/games/{game_id}")
    async def novaforge_game(
        game_id: str,
        version: int | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """One environment's compatibility, capabilities, scenarios and limitations."""
        try:
            return nova.novaforge_evaluations.game(game_id, version)
        except GameNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/novaforge/evaluations")
    async def novaforge_evaluations(
        limit: int = 20,
        game: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Every retained evaluation attempt, newest first, with its exclusion reason."""
        return nova.novaforge_evaluations.history(limit=limit, game_id=game)

    @app.get("/novaforge/evaluations/baseline")
    async def novaforge_evaluation_baseline(
        game: str,
        scenario: str = "observation-baseline",
        version: int | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The current baseline for one environment/scenario/version, or why none exists."""
        try:
            return nova.novaforge_evaluations.baseline(game, scenario, version)
        except GameNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/novaforge/evaluations/preflight")
    async def novaforge_evaluation_preflight(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Judge every readiness check against the live window. Starts nothing."""
        return await nova.novaforge_evaluations.preflight(payload)

    @app.post("/novaforge/evaluations")
    async def novaforge_evaluation_start(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Start one confirmed evaluation: observation-only, or an approved controlled plan."""
        try:
            return await nova.novaforge_evaluations.start(payload)
        except GameNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except EvaluationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except EvaluationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except BenchmarkConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except BenchmarkValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/novaforge/evaluations/{evaluation_id}")
    async def novaforge_evaluation(
        evaluation_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One evaluation: its readiness, benchmark run, baseline and current status."""
        record = nova.novaforge_evaluations.get(evaluation_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown evaluation: {evaluation_id}")
        return record

    @app.post("/novaforge/evaluations/{evaluation_id}/cancel")
    async def novaforge_evaluation_cancel(
        evaluation_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Cancel through the benchmark/session stop path; idempotent once terminal."""
        try:
            return await nova.novaforge_evaluations.cancel(evaluation_id)
        except GameNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/novaforge/evaluations/{evaluation_id}/report")
    async def novaforge_evaluation_report(
        evaluation_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """The per-game evaluation report, including missing evidence and limitations."""
        try:
            return nova.novaforge_evaluations.report(evaluation_id)
        except GameNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ── NF-06: Forge Coach and controlled experiments ───────────────────
    @app.get("/novaforge/coach")
    async def novaforge_coach(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """What the Coach can analyse, what it refuses, and how experiments run."""
        return nova.novaforge_coach.status()

    @app.post("/novaforge/coach/analyse")
    async def novaforge_coach_analyse(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Evidence-backed analysis of one recorded session or evaluation.

        READ ONLY: findings are recomputed from the records on every call, no
        record is modified, and a subject with no recorded evidence answers
        ``insufficient_evidence`` instead of a diagnosis.
        """
        try:
            return nova.novaforge_coach.analyse(
                session_id=str(payload.get("session_id") or ""),
                evaluation_id=str(payload.get("evaluation_id") or ""),
                limit=int(payload.get("limit") or 100),
            )
        except CoachNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CoachError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/novaforge/coach/recommendations")
    async def novaforge_coach_recommendations(
        limit: int = 50,
        status: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Persisted recommendation statuses (operator decisions), newest first."""
        try:
            return nova.novaforge_coach.recommendations(limit=limit, status=status)
        except CoachError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/novaforge/coach/recommendations/{recommendation_id}")
    async def novaforge_coach_recommendation_status(
        recommendation_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Record an operator decision about one recommendation (it is not evidence)."""
        try:
            return nova.novaforge_coach.set_recommendation_status(
                recommendation_id,
                str(payload.get("status") or ""),
                note=str(payload.get("note") or ""),
            )
        except CoachNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CoachError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/novaforge/experiments")
    async def novaforge_experiments(
        limit: int = 20,
        status: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Retained experiments, newest first, with their proposal/approval state."""
        try:
            return nova.novaforge_experiments.history(limit=limit, status=status)
        except ExperimentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/novaforge/experiments")
    async def novaforge_experiment_propose(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Propose an experiment from a recommendation, or from an explicit spec.

        Starts nothing. An unsupported spec is retained as ``blocked`` with its
        reasons, which is a decision record rather than an error page.
        """
        try:
            return nova.novaforge_experiments.propose(payload, coach=nova.novaforge_coach)
        except ExperimentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ExperimentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except CoachNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CoachError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/novaforge/experiments/{experiment_id}")
    async def novaforge_experiment(
        experiment_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One experiment: spec, approval, arms, results and history."""
        record = nova.novaforge_experiments.get(experiment_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown experiment: {experiment_id}")
        return record

    @app.post("/novaforge/experiments/{experiment_id}/approve")
    async def novaforge_experiment_approve(
        experiment_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Bind an approval to THIS configuration (its digest is recorded)."""
        try:
            return await nova.novaforge_experiments.approve(
                experiment_id, note=str(payload.get("note") or "")
            )
        except ExperimentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ExperimentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/novaforge/experiments/{experiment_id}/reject")
    async def novaforge_experiment_reject(
        experiment_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Reject a proposal; the rejection is retained with its note."""
        try:
            return await nova.novaforge_experiments.reject(
                experiment_id, note=str(payload.get("note") or "")
            )
        except ExperimentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ExperimentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/novaforge/experiments/{experiment_id}/run")
    async def novaforge_experiment_run(
        experiment_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Run both arms through the evaluation lab; the arms run in the background."""
        try:
            return await nova.novaforge_experiments.run(experiment_id)
        except ExperimentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ExperimentConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ExperimentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/novaforge/experiments/{experiment_id}/cancel")
    async def novaforge_experiment_cancel(
        experiment_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Cancel the experiment (idempotent once terminal)."""
        try:
            return await nova.novaforge_experiments.cancel(
                experiment_id, reason=str((payload or {}).get("reason") or "")
            )
        except ExperimentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/novaforge/experiments/{experiment_id}/results")
    async def novaforge_experiment_results(
        experiment_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """The comparison and verdict, or an explicit \"not available yet\"."""
        record = nova.novaforge_experiments.get(experiment_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown experiment: {experiment_id}")
        results = record.get("results")
        return {
            "version": record.get("version"),
            "experiment_id": experiment_id,
            "status": record.get("status"),
            "available": results is not None,
            "reason": ""
            if results is not None
            else "The experiment has not finished, so there is no result to compare yet.",
            "results": results,
        }

    # ── Phase 21: real-time perception and abstraction ────────────────
    @app.get("/perception/status")
    async def perception_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """What the perception layer is wired to right now — and what it can do.

        Providers and their LIVE availability, the budgets in force, the
        tracker and temporal state, the telemetry counters, and the identity of
        the last scene. Deliberately no frame path, no text and no pixels: this
        is an operator surface, not a window into somebody's screen.
        """
        return nova.perception.status()

    @app.get("/perception/capabilities")
    async def perception_capabilities(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The capability classification, with availability probed live.

        Every row says what it IS (implemented / partially implemented /
        provider-dependent / unavailable / future) and whether it can run on
        THIS machine, so "no vision model is wired" is answered here rather
        than discovered from a request that quietly returned nothing.
        """
        rows = nova.perception.capabilities()
        return {
            "schema_version": PERCEPTION_SCHEMA_VERSION,
            "capabilities": rows,
            "count": len(rows),
            "deferred": list(DEFERRED_PHASES),
        }

    @app.post("/perception")
    async def perception_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Perceive an image, a frame sequence, the screen or a camera.

        READING ONLY: nothing here clicks, types, opens or runs anything — the
        approval gate stands in front of the action, not in front of the
        looking, and this endpoint never crosses it. An unreadable frame is a
        422 naming the problem; a refused deep path is a 200 whose status is
        PARTIAL or UNAVAILABLE and whose reason names the capability that could
        not answer.
        """
        source = str(payload.get("source", "") or "").strip()
        source_kind = str(payload.get("source_kind", "") or "image").strip().lower()
        if not source and source_kind not in {"screen", "camera"}:
            raise HTTPException(
                status_code=422,
                detail=(
                    "A 'source' is required (a path, or a ';'-separated frame list), "
                    "or set source_kind to 'screen' or 'camera'."
                ),
            )
        result = await nova.perception.perceive(payload)
        return result.to_dict()

    # ── Phase 22: world model, memory and state reasoning ────────────────
    @app.get("/world/status")
    async def world_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """What the world model holds and can do, and what it refuses to claim.

        Versions, entity and relationship counts, the policies and retention in
        force, the open uncertainty, and the honest prediction posture (which is
        normally "no predictive model is wired"). Deliberately no attribute
        values, no observation bodies and no reasoning: this is an operator
        surface, not a window into what the system has seen.
        """
        return nova.world.status()

    @app.get("/world/state")
    async def world_state(
        entity_id: str = "", limit: int = 50, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """A bounded, content-light view of the current state.

        Entity labels, statuses, boxes and confidences; relationships; and the
        open uncertainty. Attribute VALUES are not included — a caller that wants
        a value asks ``/world/query`` for it, which is where confidence, basis and
        evidence travel with it.
        """
        summary = nova.world.state_summary()
        if entity_id:
            summary["entities"] = [
                item for item in summary.get("entities", []) if item.get("entity_id") == entity_id
            ]
        bounded = max(1, min(200, int(limit or 50)))
        summary["entities"] = summary.get("entities", [])[:bounded]
        summary["relationships"] = summary.get("relationships", [])[:bounded]
        summary["requested_entity_id"] = entity_id
        return summary

    @app.post("/world/observe")
    async def world_observe(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Ingest one observation and advance the world state (RECORDING only).

        Nothing here executes an action: this endpoint remembers what was
        reported and nothing else. An observation that carries nothing to record
        is a 422 naming the accepted shapes; one that is merely unreadable in part
        is a 200 whose status is ``partial`` and whose reason says what was
        unreadable.
        """
        shapes = {"entities", "facts", "observations", "perception", "scene"}
        if not shapes & set(payload):
            raise HTTPException(
                status_code=422,
                detail=(
                    "An observation needs 'entities' (a list of thing/id/label/bbox "
                    "records), 'facts', a Phase 21 'perception' result or a 'scene'. "
                    "Set 'complete' to true when the source covered the whole scope, "
                    "so absence can mean something."
                ),
            )
        reports: list[dict[str, Any]] = []
        for item in _observation_batch(payload):
            report = nova.world.observe(item)
            if report.status.value == "rejected":
                raise HTTPException(status_code=422, detail=report.reason)
            reports.append(report.to_dict())
        if len(reports) == 1:
            return {**reports[0], "batch": 1}
        return {
            "batch": len(reports),
            "version": nova.world.state().version,
            "state_id": nova.world.state().state_id,
            "reports": reports,
        }

    @app.post("/world/query")
    async def world_query(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Answer a bounded structured question about current or historical state.

        Kinds: entities, entity, entity_history, entity_seen, relationships,
        changes_since, state_at, uncertain, stale, evidence, diff. A question that
        is missing what it needs is a 422 naming the field; a historical question
        nothing retained covers is a 200 whose status is ``not_found`` and whose
        limitations say the current state was NOT substituted for that moment.
        """
        result = nova.world.query(payload)
        if result.status.value == "invalid":
            raise HTTPException(status_code=422, detail=result.reason)
        return result.to_dict()

    @app.post("/world/predict")
    async def world_predict(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Ask for a future state, and receive the honest answer.

        This build wires no predictive model, so the normal answer is
        ``model_unavailable``. When a rule projection is enabled the answer is
        ``predicted`` with ``rule_based: true`` and a limitation saying no model
        was involved. A prediction is NEVER fabricated to satisfy the call.
        """
        return nova.world.predict(payload).to_dict()

    # ── Phase 23: interactive learning and exploration ───────────────────
    @app.get("/learning/status")
    async def learning_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """What interactive learning can do here, and what it refuses to claim.

        The environments and their live capability probe, the policy and ceilings in
        force, the learned rules, the metrics so far and the honest provider posture
        (normally "no predictive model is wired; a declared or learned rule is what
        makes an expectation possible"). This is an operator surface: it carries
        statuses, counts and references, never an observation body or a reasoning
        trace.
        """
        return nova.learning_status()

    @app.get("/learning/environments")
    async def learning_environments(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Every environment this build can experiment in, and what each exposes.

        Actions are DECLARED, so this is what a caller may plan against; the
        simulator flag says whether a result here is evidence about a simulator
        rather than about the real world.
        """
        return nova.learning_environments()

    @app.post("/learning/environments")
    async def learning_add_environment(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Register a deterministic SIMULATOR declared as data.

        'rules' maps an action name to the state patch it applies; 'hidden' names
        the actions whose real rule the learner is not told; 'candidates' declares
        the possible outcomes for an action, which is the space a hypothesis may be
        drawn from. A failed action, a slow one, a misleading value and an omitted
        key are all declarable, because those are the cases a learner must survive.
        Only simulators can be registered here: a real environment appears when a
        caller injects one in code, and it never acts without an executor, a
        permission layer and approval.
        """
        try:
            return nova.learning_register_environment(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/learning/objectives")
    async def learning_create_objective(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Create a bounded learning objective (a question, an environment, ceilings).

        Creating an objective runs NOTHING. A blank question, an unknown environment
        or a strategy this build does not have is a 422 naming the field.
        """
        try:
            return nova.learning_create_objective(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/learning/objectives")
    async def learning_objectives(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The objectives this session holds, and which are still open."""
        return nova.learning_objectives()

    @app.get("/learning/objectives/{objective_id}")
    async def learning_objective(
        objective_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One objective with everything recorded about it.

        The cycle's steps in order, every hypothesis with its evidence counts and
        version history, every experiment with its status and the reason it got it,
        every run with its prediction and its comparison, the evidence references
        (so a conclusion can be traced to the observations behind it), and the rules
        learned in this scope. 404 when the id is unknown.
        """
        try:
            return nova.learning_objective(objective_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/learning/objectives/{objective_id}/preview")
    async def learning_preview(
        objective_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The candidate experiments, ranked, with the reason for every refusal.

        Nothing is executed: this is what WOULD happen, including which candidates
        the safety policy and the budget refuse and why.
        """
        body = payload or {}
        try:
            return nova.learning_preview(objective_id, strategy=str(body.get("strategy", "")))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/learning/objectives/{objective_id}/run")
    async def learning_run(
        objective_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Run the bounded cycle: observe, hypothesize, select, gate, act, compare, update.

        Body (all optional): {'approval': true} authorizes ONE real (non-simulator)
        experiment, {'max_experiments': n} lowers the cycle's own bound, and
        {'strategy': name} picks the selection strategy. Every ceiling and permission
        requirement is checked BEFORE an action, and a refused or interrupted
        experiment is never reported as a success.
        """
        body = payload or {}
        try:
            return await nova.learning_run(objective_id, body)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/learning/objectives/{objective_id}/approve")
    async def learning_approve(
        objective_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Approve ONE real experiment for this objective. The approval is single use."""
        body = payload or {}
        try:
            return nova.learning_approve(objective_id, reason=str(body.get("reason", "")))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/learning/objectives/{objective_id}/reject")
    async def learning_reject(
        objective_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Refuse an objective; the refusal becomes the reason it stopped."""
        body = payload or {}
        try:
            return nova.learning_reject(objective_id, reason=str(body.get("reason", "")))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/learning/objectives/{objective_id}/cancel")
    async def learning_cancel(
        objective_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Ask the objective's cycle to stop at its next checkpoint."""
        body = payload or {}
        try:
            return nova.learning_cancel(objective_id, reason=str(body.get("reason", "")))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/learning/objectives/{objective_id}/resumable")
    async def learning_resumable(
        objective_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Whether an interrupted objective may be resumed, and why not when it may not.

        Resuming is allowed only when the environment's state still matches what the
        interrupted run started from; anything else stays interrupted and needs a
        person, which is the honest answer for an action taken on the real world.
        """
        try:
            return nova.learning_resume_capability(objective_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/learning/objectives/{objective_id}/resume")
    async def learning_resume(
        objective_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Re-run an interrupted objective when the safety preconditions can be verified."""
        body = payload or {}
        try:
            return await nova.learning_resume(objective_id, body)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/learning/metrics")
    async def learning_metrics(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """What the experiments actually produced, inconclusive results included.

        A completed experiment is counted as completed; a hypothesis moves only when
        evidence moved it; agreement is over EVALUABLE predictions only, with the
        unevaluable count beside it; and a rate nobody measured is null rather than
        zero.
        """
        return nova.learning_metrics()

    @app.get("/learning/evidence/{evidence_id}")
    async def learning_evidence(
        evidence_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One evidence reference: what it supports or contradicts, and from where.

        This is the provenance door: an observation, an inference and a prediction
        are different kinds and are kept distinguishable, so a conclusion can be
        traced back to the experiments and observations behind it.
        """
        try:
            return nova.learning_evidence(evidence_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/learning/transfer")
    async def learning_transfer(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Check a learned rule against cases the caller held out.

        Body: {'rule_id': id, 'held_out': [{'action': ..., 'expected': {...}}, ...]}. The
        rule predicts each case and the agreements are counted, with the limitation
        stated plainly: this build cannot verify that the cases were held out, so the
        result is a check on the caller's cases and not a claim of generalization.
        """
        try:
            return nova.learning_transfer(payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ── Phase 24: planning, reasoning and action policy ───────────────────
    @app.get("/planning/status")
    async def planning_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """What goal-level planning can do here, and what it refuses to claim.

        The goals this session holds, how many were planned and how many runs
        verified, the replans spent, the ceilings a new goal inherits, the boot
        restore report, and the capability table with its LIVE availability —
        normally reporting explicitly that no world model means every grounded
        fact is an assertion. An operator surface: statuses, counts and
        references only, never a plan body or an observation.
        """
        return nova.goal_planning_status()

    @app.get("/planning/goals")
    async def planning_goals(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Every goal this session holds, and which of them have a selected plan."""
        return nova.goal_planning_goals()

    @app.post("/planning/goals")
    async def planning_create_goal(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Record a goal, decompose it into tasks, and produce CANDIDATE plans.

        Body: the ``request``, the ``tasks`` to run (each with an ``id`` and a
        ``title``), the ``success_conditions`` that define done, optional
        ``initial_state``, ``constraints``, ``exclusions``,
        ``required_permissions``, budget ceilings and ``correlation_id``.

        Creating and planning run NOTHING: the response is the grounding, the
        decomposition with its issues, every candidate with its comparison, the
        selected candidate, the per-step policy verdicts and the estimates with
        their basis. A missing or unmeasurable condition is reported as an issue
        rather than filled in, and a goal whose preconditions no state supports
        yields a refusal naming them. Execute it with POST
        /planning/goals/{goal_id}/run.
        """
        try:
            return nova.plan_goal(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/planning/goals/{goal_id}")
    async def planning_goal(
        goal_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One goal: its stated conditions and budget, the decomposition with every
        issue, the candidate plans with the comparison that ranked them, the
        selected plan, the last run with its revalidations and completion verdict,
        and the replan ceiling left. 404 when the id is unknown."""
        try:
            return nova.goal_planning_view(goal_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/planning/goals/{goal_id}/run")
    async def planning_run_goal(
        goal_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Execute the goal's SELECTED plan through the existing Phase 4 executor.

        Body (all optional): {'approved': [step_id, ...]} approves exactly those
        steps (a step the policy says needs a person and that was not approved is
        DENIED, not attempted), and {'keys': [fact, ...]} names the facts to read
        after the run for the completion check.

        Each step is re-checked against fresh state immediately before it runs; a
        step whose preconditions no longer hold is refused by the gate and never
        retried. The status is ``completed`` only when the goal's required success
        conditions were VERIFIED against an observation — the end of the steps is
        never completion, and an unobservable condition reports ``unknown``.
        """
        try:
            return await nova.execute_goal(goal_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/planning/goals/{goal_id}/reassess")
    async def planning_reassess_goal(
        goal_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Whether this goal should be replanned — and whether its ceiling is spent.

        A deviation alone is not a reason to replan. The decision names the
        deviations it saw, the replans already used and whether the goal's own
        ceiling is exhausted, so "try again" can never become an unbounded loop.
        """
        try:
            return nova.reassess_goal(goal_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/planning/goals/{goal_id}/cancel")
    async def planning_cancel_goal(
        goal_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Cancel a goal. Cancelling is not deleting: the record stays readable."""
        try:
            return nova.cancel_goal(goal_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ── Phase 25: embodied sessions and controlled environments ──────────
    @app.get("/embodied/status")
    async def embodied_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """What embodied sessions can do here, and what they refuse to claim.

        The allowlisted environments with their LIVE availability, the scenarios
        each offers, session counts, the ceilings in force, the input door's
        action and scope, the boot restore report and the capability table —
        including the honest absences (no real game or window adapter ships; a
        semantic reading needs a vision provider). An operator surface: statuses,
        counts and references only, never an observation body or a frame.
        """
        return nova.embodied_status()

    @app.get("/embodied/overview")
    async def embodied_overview(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The phase's own statement of what it ships and what it refuses to do.

        The loop it runs, the lifecycle vocabularies, the scenarios, the event
        types, the capabilities and the honesty flags — each flag a thing this
        build does NOT do (no real environment, no invented object identity, no
        acting on a stale reading, no retrying through a refusal).
        """
        return nova.embodied_overview()

    @app.get("/embodied/environments")
    async def embodied_environments(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The environments this build may drive, and the scenarios on offer.

        Each adapter reports its status, the actions and observations it
        answers for, and its capabilities with whether each was VERIFIED end to
        end — an adapter may declare a capability it has never run, and this is
        where the difference is visible.
        """
        return nova.embodied_environments()

    @app.get("/embodied/sessions")
    async def embodied_sessions(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Every embodied session this process holds, with its lifecycle state."""
        return nova.embodied_sessions()

    @app.post("/embodied/sessions")
    async def embodied_create_session(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Record a session. Runs NOTHING — creating is not starting.

        Body: an ``objective`` mapping (``request`` plus the
        ``success_conditions`` that define done, written as ``key=value``
        strings), an optional ``environment_id``, an optional ``scenario`` and an
        optional ``faults`` mapping. Budget ceilings are clamped by configuration
        and never raised above it, and an objective whose conditions cannot be
        checked is refused with 422 rather than run until its budget dies. Drive
        it with POST /embodied/sessions/{session_id}/run.
        """
        try:
            return nova.embodied_create_session(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/embodied/sessions/{session_id}")
    async def embodied_session(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One session: its objective, status and reason, the completion verdict
        with the conditions that were and were not observed, the bounded
        trajectory and the evaluation. 404 when the id is unknown."""
        try:
            return nova.embodied_view(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/embodied/sessions/{session_id}/run")
    async def embodied_run_session(
        session_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Drive one session's bounded loop: observe, decide through the goal
        planner, send an authorized input through the ONE door, verify the result
        from a fresh reading, recover or replan, then report the ending.

        Body (optional): {'max_actions': n} caps this run below the session's own
        budget. The status is ``completed`` only when every success condition was
        OBSERVED holding; a refusal, a stale reading and an exhausted budget are
        their own endings with their own reasons. A timed-out input is never
        blindly re-sent — the state is re-read first.
        """
        try:
            return await nova.embodied_run_session(session_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/embodied/sessions/{session_id}/pause")
    async def embodied_pause_session(
        session_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Pause a session. What it holds is released BEFORE the status moves, so
        a paused session is never holding an input."""
        try:
            return nova.embodied_pause_session(session_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/embodied/sessions/{session_id}/resume")
    async def embodied_resume_session(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Resume a paused session — as NOT revalidated. The environment and the
        session must both be re-established before another input is sent, because
        a pause may have been long enough for the reading to be worthless."""
        try:
            return nova.embodied_resume_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/embodied/sessions/{session_id}/cancel")
    async def embodied_cancel_session(
        session_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Cancel a session. Cancelling is not deleting: the record stays readable,
        and whatever the session held is released first."""
        try:
            return nova.embodied_cancel_session(session_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ── Phase 26: generalization, ARC-style reasoning and evaluation ─────
    # Every route here is a THIN door onto the application method of the same
    # name. Nothing on this surface trains anything, changes a weight or reports
    # a number the per-family metrics did not measure: a run's answer key is
    # never in a list, and a refused request says which rule refused it.
    @app.get("/generalization/status")
    async def generalization_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """What this build can measure about its own generalization — and what it
        will not claim.

        The corpus and its split counts with the three-way leakage verdict, the
        six families with their success conditions and verifiers, the solver and
        baseline names, the transformation vocabulary, the search ceilings in
        force, the run counters, the provider posture, the boot restore report
        and the capability table with its honest states. No score appears here
        unless a run actually produced one.
        """
        return nova.generalization_status()

    @app.get("/generalization/overview")
    async def generalization_overview(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The phase's own statement of what it ships and what it refuses to do.

        The evaluation cycle it runs, the lifecycle vocabularies, the families,
        the operator library, the verifiers, the failure categories and the
        honesty flags — each flag a thing this build does NOT do (no general ARC
        solver, no hardcoded answers, no answer key during search, no single
        intelligence score, no training, no fabricated scores, no gameplay).
        """
        return nova.generalization_overview()

    @app.get("/generalization/families")
    async def generalization_families(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The task families, each with its own success condition and verifier.

        Deliberately a list rather than one blended score: the families are
        measured in different units, so per-family metrics are what a reader
        compares, and an aggregate would hide a family failing behind a mean.
        """
        return nova.generalization_families()

    @app.get("/generalization/operators")
    async def generalization_operators(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The transformation library the search may use, and its depth ceiling.

        Each operator is a pure function with no task id and no answer inside it.
        The search proposes parameters from the examples and believes nothing
        until EVERY example is reproduced exactly; anything the library cannot
        express is reported UNSUPPORTED rather than guessed at.
        """
        return nova.generalization_operators()

    @app.get("/generalization/tasks")
    async def generalization_tasks(
        family: str = "",
        split: str = "",
        difficulty: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The registered tasks matching a filter — WITHOUT their answer keys.

        A task list is a browsing surface, so the expected outputs are withheld
        and the single-task endpoint is where a key lives. 422 when the split or
        difficulty names something this build does not have.
        """
        try:
            return nova.generalization_tasks(
                {"family": family, "split": split, "difficulty": difficulty}
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/generalization/tasks/{task_id}")
    async def generalization_task(
        task_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One task, whole — request, success condition, verifier, split, examples
        and the expected output the evaluator holds. 404 when the id is unknown."""
        try:
            return nova.generalization_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/generalization/splits")
    async def generalization_splits(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """How the corpus is split, with the leakage verdict attached.

        Three checks are reported separately because they fail differently: a
        task in both splits, a held-out answer already present in the fitting
        set, and an example flagged as already used while tuning. ``clean`` is
        ``None`` only when the check could not run — never when it merely found
        nothing.
        """
        return nova.generalization_splits()

    @app.post("/generalization/tasks")
    async def generalization_register_task(
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Register one task on this process's corpus. Runs NOTHING — a task is a
        definition, not an evaluation.

        The body is the task itself: ``task_id``, ``kind``, ``split``,
        ``difficulty``, ``request``, ``success_condition``, ``examples``,
        ``test_input`` and ``expected_output``. Refused with 422, and the reason
        says which rule refused it: a missing success condition (a task nobody
        can verify), a missing answer key (a held-out task whose answer is
        unknown cannot be scored), a verifier this build does not implement, an

        unreadable grid, and an id already in use — a corpus that can be
        overwritten cannot be cited as evidence.

        Registration is in-memory for the life of the process; the RUNS are what
        persists. Drive it with POST /generalization/evaluate.
        """
        try:
            return nova.generalization_register_task(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/generalization/evaluate")
    async def generalization_evaluate(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Run one bounded evaluation over the SAME tasks for candidate and baseline.

        Body (all optional): ``family``, ``split`` (defaults to held-out),
        ``solver`` (default ``infer``), ``baseline`` (default ``identity``),
        ``seed``, ``task_ids``, ``max_tasks``, ``max_candidates``, ``max_depth``
        and ``max_seconds`` — each clamped by configuration, never raised above
        it. Returns the run: per-family metrics, classified failures, the frozen
        settings, the leakage verdict and a fingerprint over outcomes only.

        422 when the request names a solver, family or split this build does not
        have, when no task matches, or when the held-out split is NOT clean — a
        number measured over leaked examples is refused rather than reported.
        """
        try:
            return nova.generalization_evaluate(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/generalization/runs")
    async def generalization_runs(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The stored runs as trimmed rows: lifecycle fields and counts only.

        The per-task results live on the single-run view — a list that embedded
        whole records would make the lightest surface the heaviest one, and a
        summary is all a list is for.
        """
        return nova.generalization_runs()

    @app.get("/generalization/runs/{run_id}")
    async def generalization_run(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One run, whole: settings, seed, per-family metrics, classified failures
        with their evidence, the recommendations those failures produced, and the
        per-task results. 404 when the id is unknown."""
        try:
            return nova.generalization_view(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/generalization/runs/{run_id}/export")
    async def generalization_export(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """A report payload for review: the run, the corpus and its splits, the
        operator library, the supported rules, the reproduce block (solver,
        baseline, seed, frozen settings, task ids) and the outcome fingerprint.

        It is data for a person to read. Nothing here schedules work, writes a
        dataset or promotes the candidate — the decision to act on a result stays
        outside the benchmark. 404 when the id is unknown.
        """
        try:
            return nova.generalization_export(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/generalization/compare")
    async def generalization_compare(
        baseline: str,
        candidate: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Two stored runs compared task by task, with neither record modified.

        ``regressed`` names the tasks solved before and unsolved now; a task only
        one run evaluated is reported as appeared/disappeared rather than counted
        as a movement, and differing seeds are called out because a movement could
        then come from the seed rather than from the change. 404 on an unknown id.
        """
        try:
            return nova.generalization_compare(baseline, candidate)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/generalization/failures")
    async def generalization_failures(
        run: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Classified failures, grouped by category — one run with ``?run=``, or
        every stored run without it.

        The categories separate the causes a solver's ending cannot: no rule
        found is not a rule that fit and missed, ambiguity is not failure, a
        defective transformation is not a wrong answer, and a broken evaluation
        SETUP is reported as itself rather than counted as a model miss. 404 for
        an unknown run id.
        """
        try:
            return nova.generalization_failures(run)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/generalization/recommendations")
    async def generalization_recommendations(
        run: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Structured, NON-EXECUTING improvement recommendations from real failures.

        One per recurring category, naming the tasks and families that produced it
        and the advice that belongs to it. Nothing is executed: no training run is
        started, no dataset is written and no model is touched. 404 for an unknown
        run id.
        """
        try:
            return nova.generalization_recommendations(run)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/generalization/game-eval/contracts")
    async def generalization_game_eval_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The nine evaluation contracts the post-Phase-26 game milestone will be
        measured on: objective completion, action efficiency, observation
        freshness, recovery success, constraint violations, time to completion,
        initial-condition spread, reproducibility and resource use.

        Definitions and their sources — an adapter over the Phase 25 session
        record, NOT a game, and no gameplay capability is claimed.
        """
        return nova.generalization_game_eval_contracts()

    @app.get("/generalization/game-eval")
    async def generalization_game_eval(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Those contracts measured over the embodied sessions this build actually
        ran, per environment and never pooled.

        Every row names the environment it summarised, so a simulated session is
        never presented as gameplay; a figure the records did not measure comes
        back ``null`` rather than as a zero; and with no sessions yet the report
        says so instead of returning an empty scorecard that reads as perfect.
        """
        return nova.generalization_game_eval()

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
        latency. It runs in a worker thread and on ONE runtime-probe budget shared
        by the whole block (audit defect D4), because this endpoint is
        reachable from the UI and a status call must never stall the loop.
        """
        return {
            "telemetry": nova.intelligence.telemetry.to_dict(),
            "findings": nova.intelligence.telemetry.improvement_findings(),
            "capabilities": nova.intelligence.capabilities.to_dict(),
            "thresholds": nova.intelligence.thresholds.to_dict(),
            "models": await nova.intelligence_model_report(),
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
    async def phone_plan(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        return nova.plan_phone_command(_required_field(payload, "command"))

    @app.post("/phone/execute")
    async def phone_execute(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        token = str(payload.get("approval_token", "") or "") or None
        try:
            return await nova.execute_phone_command(
                _required_field(payload, "command"),
                approval_token=token,
                correlation_id=str(payload.get("correlation_id", "") or ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/agent/run")
    async def agent_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Run the full agentic loop (interpret → plan → perceive → act → verify → recover)."""
        return await nova.run_agentic_task(_required_field(payload, "request"))

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
        """The automation a task action targets, or a 422 that names the field.

        ``/automation`` reports each task under ``id`` and the actions used to
        demand ``automation_id``, so the obvious round trip — read the report,
        post the id back — was refused for using the name the report itself
        printed. Both spellings are accepted; ``automation_id`` wins when a body
        carries both.
        """
        automation_id = str(payload.get("automation_id") or payload.get("id") or "").strip()
        if not automation_id:
            raise HTTPException(status_code=422, detail="automation_id is required.")
        return automation_id

    @app.get("/automation")
    async def automation_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """What is scheduled, what is armed, and when the next run happens."""
        return nova.automation_status()

    @app.post("/automation/schedule")
    async def automation_schedule(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Store a request plus the schedule its wording states.

        ``authorize`` is the caller saying it has approved the task; without it
        the automation is stored pending and stays disarmed until
        ``/automation/approve``, which is the only way it becomes runnable.
        """
        try:
            return nova.schedule_automation(
                _required_field(payload, "request"),
                name=str(payload.get("name", "")),
                authorize=bool(payload.get("authorize", False)),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/automation/approve")
    async def automation_approve(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.approve_automation(
                _automation_id(payload), by=str(payload.get("by", "operator"))
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/automation/cancel")
    async def automation_cancel(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.cancel_automation(_automation_id(payload))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/automation/enable")
    async def automation_enable(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.enable_automation(_automation_id(payload))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/automation/disable")
    async def automation_disable(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.disable_automation(_automation_id(payload))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/automation/run")
    async def automation_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
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
    async def audit_entries(
        limit: int = 20, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        return {"entries": nova.audit_entries(limit)}

    @app.post("/audit/prune")
    async def audit_prune(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return nova.audit_prune()

    @app.post("/audit/delete")
    async def audit_delete(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
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
    async def privacy_update(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Change the execution mode and/or a control, and apply it live."""
        return await nova.apply_privacy_settings(
            execution_mode=str(payload["execution_mode"]) if "execution_mode" in payload else None,
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
    async def cost_estimate(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
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
    async def benchmark_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
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
        return _training_refused(nova.pause_training_run(_required_field(payload)), "pause")

    @app.post("/training/runs/cancel")
    async def cancel_training_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """End a run: a live one at its next step, a stored one immediately."""
        return _training_refused(nova.cancel_training_run(_required_field(payload)), "cancellation")

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
            nova.estimate_training_run(_required_field(payload)), "estimate", status_code=404
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
        return _training_refused(
            result, "evaluation", status_code=404 if _is_missing(result) else 409
        )

    @app.get("/training/runs/{run_id}/checkpoints")
    async def training_checkpoints(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Every checkpoint of one run, with its kind, size and loadability."""
        return nova.training_checkpoints(run_id)

    @app.get("/training/runs/{run_id}")
    async def training_run(run_id: str, _principal: str = Depends(require_auth)) -> dict[str, Any]:
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
            _required_field(payload, "model_id"),
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
            _required_field(payload, "model_id"), note=str(payload.get("note", ""))
        )
        return _training_refused(result, "promotion")

    @app.post("/training/models/reject")
    async def reject_training_model(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Reject a candidate; a rejected model is never promoted by accident."""
        result = nova.reject_training_model(
            _required_field(payload, "model_id"), reason=str(payload.get("reason", ""))
        )
        return _training_refused(result, "rejection")

    @app.post("/training/models/deprecate")
    async def deprecate_training_model(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Retire a production or approved model without deleting its record."""
        result = nova.deprecate_training_model(
            _required_field(payload, "model_id"), reason=str(payload.get("reason", ""))
        )
        return _training_refused(result, "deprecation")

    @app.post("/training/models/rollback")
    async def rollback_training_model(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Return production to the model this one replaced."""
        result = nova.rollback_training_model(
            _required_field(payload, "model_id"), reason=str(payload.get("reason", ""))
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
            raise HTTPException(status_code=422, detail="dataset_type and prompt are required.")
        if not isinstance(chosen, dict) or not isinstance(rejected, dict):
            raise HTTPException(status_code=422, detail="chosen and rejected must both be objects.")
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
            rejected_outcome=(rejected_outcome if isinstance(rejected_outcome, dict) else None),
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
            _required_field(payload, "preference_id"),
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
            _required_field(payload),
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
        return _training_refused(nova.pause_preference_run(_required_field(payload)), "pause")

    @app.post("/preference/runs/cancel")
    async def cancel_preference_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """End a run: a live one at its next step, a stored one immediately."""
        return _training_refused(
            nova.cancel_preference_run(_required_field(payload)), "cancellation"
        )

    @app.post("/preference/runs/resume")
    async def resume_preference_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Resume an interrupted run from its newest valid checkpoint."""
        result = await nova.resume_preference_run(
            _required_field(payload), checkpoint_id=str(payload.get("checkpoint_id", ""))
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
            nova.estimate_preference_run(_required_field(payload)), "estimate", status_code=404
        )

    @app.post("/preference/runs/evaluate")
    async def evaluate_preference_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Compare base, SFT and candidate on the held-out pairs."""
        result = await nova.evaluate_preference_run(
            _required_field(payload),
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
                [str(item) for item in criteria] if isinstance(criteria, (list, tuple)) else ()
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
            raise HTTPException(status_code=422, detail="name and mode are required.")
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
            tags=[str(tag) for tag in tags] if isinstance(tags, (list, tuple)) else (),
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=422,
                detail=str(result.get("reason", "the reward dataset could not be built")),
            )
        return result

    @app.get("/rlhf/datasets/{dataset_version_id}/validate")
    async def validate_rlhf_dataset(
        dataset_version_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.validate_rlhf_dataset(dataset_version_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/rlhf/datasets/{dataset_version_id}/held")
    async def rlhf_held(
        dataset_version_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        result = nova.rlhf_held(dataset_version_id)
        if not result.get("ok", True):
            detail = str(result.get("reason") or "no such dataset")
            raise HTTPException(status_code=404, detail=detail)
        return result

    @app.get("/rlhf/datasets/{dataset_version_id}")
    async def rlhf_dataset(
        dataset_version_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
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
            _required_field(payload),
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
        return _training_refused(nova.pause_rlhf_run(_required_field(payload)), "pause")

    @app.post("/rlhf/runs/cancel")
    async def cancel_rlhf_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """End an RL run: a live one at its next step, a stored one now."""
        return _training_refused(nova.cancel_rlhf_run(_required_field(payload)), "cancellation")

    @app.post("/rlhf/runs/resume")
    async def resume_rlhf_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Resume an interrupted RL run from its newest valid checkpoint."""
        result = await nova.resume_rlhf_run(
            _required_field(payload),
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
            nova.estimate_rlhf_run(_required_field(payload)), "estimate", status_code=404
        )

    @app.post("/rlhf/runs/evaluate")
    async def evaluate_rlhf_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Compare base, SFT, preference and RL candidate on held-out data."""
        result = await nova.evaluate_rlhf_run(
            _required_field(payload),
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
        result = nova.rlhf_checkpoints(run_id)
        if not result.get("ok", True):
            raise HTTPException(status_code=404, detail=str(result.get("reason") or "no such run"))
        return result

    @app.get("/rlhf/runs/{run_id}")
    async def rlhf_run(run_id: str, _principal: str = Depends(require_auth)) -> dict[str, Any]:
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
    async def rlhf_model(model_id: str, _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """One registered model, with the RL mode that produced it."""
        try:
            return nova.rlhf_model(model_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ── Phase 19: RLVR + critique-based learning ────────────────────────────
    # Same discipline as /rlhf: reads do not start anything, a refused action
    # is its own status, a missing thing is a 404, and real runs need explicit
    # confirmation and an unsafe override. RLVR is OFF by default, never loads
    # a model, and a verifier can be disabled but never edited into passing.

    @app.get("/rlvr/status")
    async def rlvr_status(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """RLVR state: verifiers, critiques, corrections, datasets, runs."""
        return nova.rlvr_status()

    @app.get("/rlvr/summary")
    async def rlvr_summary(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """The same, plus the newest critiques, corrections and datasets."""
        return nova.rlvr_summary()

    @app.get("/rlvr/verifiers")
    async def rlvr_verifiers(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        """Registered verifiers, their categories, versions and integrity state."""
        return nova.rlvr_verifiers()

    @app.post("/rlvr/verifiers/disable")
    async def disable_rlvr_verifier(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Stop a verifier supporting rewards. Disabling is the only "edit"."""
        result = nova.disable_rlvr_verifier(
            str(payload.get("verifier_id") or ""),
            reason=str(payload.get("reason") or ""),
        )
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=str(result.get("reason")))
        return result

    @app.post("/rlvr/verifiers/enable")
    async def enable_rlvr_verifier(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Let a previously disabled verifier support rewards again."""
        result = nova.enable_rlvr_verifier(str(payload.get("verifier_id") or ""))
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=str(result.get("reason")))
        return result

    @app.post("/rlvr/verify")
    async def verify_rlvr(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Verify checkable questions; each expectation is frozen first."""
        return nova.verify_rlvr(payload)

    @app.post("/rlvr/reward")
    async def rlvr_reward(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Turn verifications into a reward, with its integrity audit."""
        return nova.rlvr_reward(payload)

    @app.post("/rlvr/critiques")
    async def record_rlvr_critiques(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Generate structured critiques from recorded evidence and store them."""
        return nova.record_rlvr_critiques(payload)

    @app.get("/rlvr/critiques")
    async def rlvr_critiques(
        trajectory_id: str = "",
        category: str = "",
        severity: str = "",
        source: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Stored critiques, newest first, filterable by what failed and how badly."""
        return nova.rlvr_critiques(
            trajectory_id=trajectory_id,
            category=category,
            severity=severity,
            source=source,
            limit=limit,
        )

    @app.get("/rlvr/corrections")
    async def rlvr_corrections(
        status: str = "",
        pending_only: bool = False,
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Corrected examples and their verdicts; held ones are reviewable."""
        return nova.rlvr_corrections(status=status, limit=limit, pending_only=pending_only)

    @app.post("/rlvr/corrections")
    async def propose_rlvr_corrections(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Propose corrections for stored critiques; unverified ones are held."""
        return nova.propose_rlvr_corrections(payload)

    @app.get("/rlvr/datasets")
    async def rlvr_critique_datasets(
        name: str = "", limit: int = 50, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Critique dataset versions, newest first, filtered by name."""
        return nova.rlvr_critique_datasets(name=name, limit=limit)

    @app.post("/rlvr/datasets")
    async def create_rlvr_critique_dataset(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Build a critique dataset version; it is immutable once stored."""
        return nova.create_rlvr_critique_dataset(payload)

    @app.get("/rlvr/datasets/{dataset_version_id}/validate")
    async def validate_rlvr_critique_dataset(
        dataset_version_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Whether a critique dataset can train anything, and what is missing."""
        try:
            return nova.validate_rlvr_critique_dataset(dataset_version_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/rlvr/datasets/{dataset_version_id}/held")
    async def rlvr_held(
        dataset_version_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Corrections a dataset held back, so a person can settle them."""
        return nova.rlvr_held(dataset_version_id)

    @app.get("/rlvr/datasets/{dataset_version_id}/pairs")
    async def rlvr_preference_pairs(
        dataset_version_id: str,
        verified_only: bool = True,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """The Phase 17 preference pairs a critique dataset yields."""
        try:
            return nova.rlvr_preference_pairs(dataset_version_id, verified_only=verified_only)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/rlvr/datasets/{dataset_version_id}")
    async def rlvr_critique_dataset(
        dataset_version_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """One critique dataset version with its splits and statistics."""
        try:
            return nova.rlvr_critique_dataset(dataset_version_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/rlvr/estimate")
    async def estimate_rlvr(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Validate an RLVR configuration and price it; trains nothing."""
        raw = payload.get("config")
        cfg = raw if isinstance(raw, dict) else {}
        return nova.estimate_rlvr(cfg)

    @app.post("/rlvr/pipeline")
    async def rlvr_pipeline(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """The ten-stage RLVR plan for this configuration and task set."""
        cfg = payload.get("config") if isinstance(payload.get("config"), dict) else None
        tasks = payload.get("tasks")
        return nova.rlvr_pipeline(
            cfg,
            tasks=[dict(item) for item in tasks if isinstance(item, dict)]
            if isinstance(tasks, (list, tuple))
            else (),
            dataset_version=str(payload.get("dataset_version", "")),
        )

    @app.post("/rlvr/dry-run")
    async def dry_run_rlvr(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Walk all ten stages on deterministic inputs; starts nothing."""
        cfg = payload.get("config") if isinstance(payload.get("config"), dict) else None
        tasks = payload.get("tasks")
        labels = payload.get("labels")
        return nova.dry_run_rlvr(
            cfg,
            tasks=[dict(item) for item in tasks if isinstance(item, dict)]
            if isinstance(tasks, (list, tuple))
            else (),
            dataset_version=str(payload.get("dataset_version", "")),
            labels={str(key): str(value) for key, value in labels.items()}
            if isinstance(labels, dict)
            else None,
        )

    @app.get("/rlvr/runs")
    async def rlvr_runs(
        status: str = "", limit: int = 50, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """RLVR runs, newest first, filtered by status."""
        return nova.rlvr_runs(status=status, limit=limit)

    @app.post("/rlvr/runs")
    async def create_rlvr_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Create an RLVR run from a critique dataset (never starts it)."""
        result = nova.create_rlvr_run(
            str(payload.get("model") or ""),
            str(payload.get("dataset_version") or ""),
            config=payload.get("config") if isinstance(payload.get("config"), dict) else None,
            name=str(payload.get("name") or ""),
        )
        if not result.get("ok"):
            raise HTTPException(status_code=422, detail=str(result.get("reason")))
        return result

    @app.post("/rlvr/runs/start")
    async def start_rlvr_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Start an RLVR run; unsafe estimates and unconfirmed real runs refused."""
        return await nova.start_rlvr_run(
            str(payload.get("run_id") or ""),
            override=bool(payload.get("override")),
            confirm=bool(payload.get("confirm")),
        )

    @app.post("/rlvr/runs/pause")
    async def pause_rlvr_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Pause a running RLVR run at the next step boundary."""
        return nova.pause_rlvr_run(str(payload.get("run_id") or ""))

    @app.post("/rlvr/runs/cancel")
    async def cancel_rlvr_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Cancel an RLVR run; its checkpoints are kept."""
        return nova.cancel_rlvr_run(str(payload.get("run_id") or ""))

    @app.post("/rlvr/runs/resume")
    async def resume_rlvr_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Resume an interrupted RLVR run from its newest valid checkpoint."""
        return await nova.resume_rlvr_run(
            str(payload.get("run_id") or ""),
            checkpoint_id=str(payload.get("checkpoint_id") or ""),
        )

    @app.post("/rlvr/runs/re-estimate")
    async def estimate_rlvr_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Re-estimate a run's resources against the current machine reading."""
        return nova.estimate_rlvr_run(str(payload.get("run_id") or ""))

    @app.post("/rlvr/runs/evaluate")
    async def evaluate_rlvr_run(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Compare base, SFT and preference models against the RLVR candidate."""
        return await nova.evaluate_rlvr_run(
            str(payload.get("run_id") or ""),
            base=payload.get("base"),
            candidate=payload.get("candidate"),
            sft=payload.get("sft"),
            preference=payload.get("preference"),
            dataset_version=str(payload.get("dataset_version") or ""),
            split=str(payload.get("split") or "test"),
            tolerance=payload.get("tolerance")
            if isinstance(payload.get("tolerance"), (int, float))
            else None,
        )

    @app.post("/rlvr/evaluate")
    async def rlvr_evaluation(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Verifier-side metrics against labels recorded before the call."""

        def rows(key: str) -> list[dict[str, Any]]:
            value = payload.get(key)
            if not isinstance(value, (list, tuple)):
                return []
            return [dict(item) for item in value if isinstance(item, dict)]

        labels = payload.get("labels")
        categories = payload.get("expected_categories")
        return nova.rlvr_evaluation(
            verifications=rows("verifications"),
            labels={str(key): str(value) for key, value in labels.items()}
            if isinstance(labels, dict)
            else None,
            critiques=rows("critiques"),
            expected_categories={str(key): str(value) for key, value in categories.items()}
            if isinstance(categories, dict)
            else None,
            integrity=rows("integrity"),
            dataset_version=str(payload.get("dataset_version") or ""),
            run_id=str(payload.get("run_id") or ""),
        )

    @app.get("/rlvr/runs/{run_id}/checkpoints")
    async def rlvr_checkpoints(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """An RLVR run's checkpoints with their validity and loadability."""
        return nova.rlvr_checkpoints(run_id)

    @app.get("/rlvr/runs/{run_id}")
    async def rlvr_run(run_id: str, _principal: str = Depends(require_auth)) -> dict[str, Any]:
        """One RLVR run: configuration, metrics, verifier snapshot."""
        try:
            return nova.rlvr_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/brain/decide")
    async def brain_decide(
        payload: BrainDecideRequest, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
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
            routed["preview"] = {
                "kind": "info",
                "text": f"Preview unavailable: {type(exc).__name__}",
            }
        return routed

    @app.get("/explore/trending")
    async def explore_trending(
        count: int = 6, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
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
    async def explore(
        payload: ExploreRequest_, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
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
        request: Request,
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
        if not result.authenticated and not _local_admin_authorized(
            request, token, authorization
        ):
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

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/plugins")
    async def plugins(_principal: str = Depends(require_auth)) -> dict[str, Any]:
        return {"plugins": [], "marketplace_enabled": False}

    # -- NM-10: realistic paper execution ---------------------------------
    def _paper_http_error(exc: Exception) -> Any:
        from fastapi import HTTPException
        from novacontrol.novamarket.contracts import NotOperationalError
        from novacontrol.novamarket.execution import (
            ExecutionError,
            ExecutionValidationError,
            UnsupportedOrderType,
        )

        if isinstance(exc, HTTPException):
            return exc
        if isinstance(exc, (ExecutionValidationError, UnsupportedOrderType)):
            return HTTPException(status_code=422, detail=str(exc))
        if isinstance(exc, ExecutionError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, NotOperationalError):
            return HTTPException(status_code=409, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    @app.post("/market/execution/orders")
    async def market_execution_submit(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Execute an already-approved NM-09 proposal as a simulated order."""
        try:
            result = await nova.novamarket.submit_paper_order(
                str(payload.get("proposal_id") or ""),
                quantity=payload.get("quantity"),
                order_type=str(payload.get("order_type") or "market"),
                limit_price=payload.get("limit_price"),
                trigger_price=payload.get("trigger_price"),
                expires_at=payload.get("expires_at"),
                idempotency_key=str(payload.get("idempotency_key") or ""),
                actor=_principal,
                execute=bool(payload.get("execute", True)),
            )
        except Exception as exc:  # noqa: BLE001
            raise _paper_http_error(exc) from exc
        if not result.get("ok") and result.get("order", {}).get("status") == "rejected":
            from fastapi import HTTPException

            raise HTTPException(
                status_code=422,
                detail={"error": result.get("error", "rejected"), "order": result.get("order")},
            )
        return result

    @app.get("/market/execution/orders")
    async def market_execution_orders(
        status: str = "",
        portfolio_id: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.paper_orders(
                status=status, portfolio_id=portfolio_id, limit=limit
            )
        except Exception as exc:  # noqa: BLE001
            raise _paper_http_error(exc) from exc

    @app.get("/market/execution/orders/{order_id}")
    async def market_execution_order(
        order_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.paper_order(order_id)
        except Exception as exc:  # noqa: BLE001
            raise _paper_http_error(exc) from exc

    @app.get("/market/execution/orders/{order_id}/fills")
    async def market_execution_order_fills(
        order_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.paper_fills(order_id)
        except Exception as exc:  # noqa: BLE001
            raise _paper_http_error(exc) from exc

    @app.get("/market/execution/orders/{order_id}/history")
    async def market_execution_order_history(
        order_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            full = nova.novamarket.paper_order(order_id)
            return {
                "ok": True,
                "order_id": order_id,
                "transitions": full.get("transitions", []),
            }
        except Exception as exc:  # noqa: BLE001
            raise _paper_http_error(exc) from exc

    @app.post("/market/execution/orders/{order_id}/process")
    async def market_execution_process(
        order_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.process_paper_order(order_id)
        except Exception as exc:  # noqa: BLE001
            raise _paper_http_error(exc) from exc

    @app.post("/market/execution/orders/{order_id}/cancel")
    async def market_execution_cancel(
        order_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.cancel_paper_order(
                order_id,
                reason=str((payload or {}).get("reason") or "cancelled by operator"),
                actor=_principal,
            )
        except Exception as exc:  # noqa: BLE001
            raise _paper_http_error(exc) from exc

    @app.get("/market/execution/fills")
    async def market_execution_fills(
        order_id: str = "", _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.paper_fills(order_id)
        except Exception as exc:  # noqa: BLE001
            raise _paper_http_error(exc) from exc

    @app.get("/market/execution/status")
    async def market_execution_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.execution_status()

    @app.get("/market/execution/contracts")
    async def market_execution_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.execution_contract()

    @app.get("/market/execution/fees")
    async def market_execution_fees(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.execution_fees()
        except Exception as exc:  # noqa: BLE001
            raise _paper_http_error(exc) from exc

    @app.get("/market/execution/slippage")
    async def market_execution_slippage(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.execution_slippage()
        except Exception as exc:  # noqa: BLE001
            raise _paper_http_error(exc) from exc

    @app.post("/market/execution/reconcile")
    async def market_execution_reconcile(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.reconcile_paper_execution(
                str((payload or {}).get("order_id") or "")
            )
        except Exception as exc:  # noqa: BLE001
            raise _paper_http_error(exc) from exc

    @app.get("/market/execution/reconciliations")
    async def market_execution_reconciliations(
        limit: int = 50, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.paper_reconciliations(limit)
        except Exception as exc:  # noqa: BLE001
            raise _paper_http_error(exc) from exc

    # -- NM-11: reproducible historical backtesting ------------------------
    def _backtest_http_error(exc: Exception) -> Any:
        from fastapi import HTTPException
        from novacontrol.novamarket.backtesting import BacktestError

        if isinstance(exc, HTTPException):
            return exc
        if isinstance(exc, BacktestError):
            text = str(exc)
            if "unknown" in text:
                return HTTPException(status_code=404, detail=text)
            if "already running" in text:
                return HTTPException(status_code=409, detail=text)
            return HTTPException(status_code=422, detail=text)
        return HTTPException(status_code=500, detail=str(exc))

    @app.get("/market/backtests/status")
    async def market_backtest_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.backtest_status()

    @app.get("/market/backtests/contracts")
    async def market_backtest_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.backtest_contract()

    @app.post("/market/backtests/validate")
    async def market_backtest_validate(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.backtest_validate(
                payload, load_data=bool(payload.get("load_data", False))
            )
        except Exception as exc:  # noqa: BLE001
            raise _backtest_http_error(exc) from exc

    @app.post("/market/backtests")
    async def market_backtest_create(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.backtest_create(payload)
        except Exception as exc:  # noqa: BLE001
            raise _backtest_http_error(exc) from exc

    @app.get("/market/backtests")
    async def market_backtests(
        limit: int = 50, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.backtest_list(limit)
        except Exception as exc:  # noqa: BLE001
            raise _backtest_http_error(exc) from exc

    @app.post("/market/backtests/compare")
    async def market_backtest_compare(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.backtest_compare(
                [str(item) for item in payload.get("backtest_ids") or ()]
            )
        except Exception as exc:  # noqa: BLE001
            raise _backtest_http_error(exc) from exc

    @app.get("/market/backtests/comparisons")
    async def market_backtest_comparisons(
        limit: int = 50, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.backtest_comparisons(limit)
        except Exception as exc:  # noqa: BLE001
            raise _backtest_http_error(exc) from exc

    @app.post("/market/backtests/{backtest_id}/start")
    async def market_backtest_start(
        backtest_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.backtest_start(backtest_id)
        except Exception as exc:  # noqa: BLE001
            raise _backtest_http_error(exc) from exc

    @app.post("/market/backtests/{backtest_id}/cancel")
    async def market_backtest_cancel(
        backtest_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.backtest_cancel(backtest_id)
        except Exception as exc:  # noqa: BLE001
            raise _backtest_http_error(exc) from exc

    @app.get("/market/backtests/{backtest_id}")
    async def market_backtest(
        backtest_id: str,
        include_bars: bool = False,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.backtest(backtest_id, include_bars=include_bars)
        except Exception as exc:  # noqa: BLE001
            raise _backtest_http_error(exc) from exc

    @app.get("/market/backtests/{backtest_id}/results")
    async def market_backtest_results(
        backtest_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.backtest_results(backtest_id)
        except Exception as exc:  # noqa: BLE001
            raise _backtest_http_error(exc) from exc

    @app.get("/market/backtests/{backtest_id}/metrics")
    async def market_backtest_metrics(
        backtest_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.backtest_metrics(backtest_id)
        except Exception as exc:  # noqa: BLE001
            raise _backtest_http_error(exc) from exc

    @app.get("/market/backtests/{backtest_id}/trades")
    async def market_backtest_trades(
        backtest_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.backtest_trades(backtest_id)
        except Exception as exc:  # noqa: BLE001
            raise _backtest_http_error(exc) from exc

    @app.get("/market/backtests/{backtest_id}/orders")
    async def market_backtest_orders(
        backtest_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.backtest_orders(backtest_id)
        except Exception as exc:  # noqa: BLE001
            raise _backtest_http_error(exc) from exc

    @app.get("/market/backtests/{backtest_id}/risk-decisions")
    async def market_backtest_risk_decisions(
        backtest_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.backtest_decisions(backtest_id)
        except Exception as exc:  # noqa: BLE001
            raise _backtest_http_error(exc) from exc

    @app.get("/market/backtests/{backtest_id}/signals")
    async def market_backtest_signals(
        backtest_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.backtest_signals(backtest_id)
        except Exception as exc:  # noqa: BLE001
            raise _backtest_http_error(exc) from exc

    @app.get("/market/backtests/{backtest_id}/data-quality")
    async def market_backtest_data_quality(
        backtest_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.backtest_data_quality(backtest_id)
        except Exception as exc:  # noqa: BLE001
            raise _backtest_http_error(exc) from exc

    @app.get("/market/backtests/{backtest_id}/verify")
    async def market_backtest_verify(
        backtest_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.backtest_verify(backtest_id)
        except Exception as exc:  # noqa: BLE001
            raise _backtest_http_error(exc) from exc

    @app.post("/market/backtests/{backtest_id}/sensitivity")
    async def market_backtest_sensitivity(
        backtest_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.backtest_sensitivity(
                backtest_id, payload.get("variants") or ()
            )
        except Exception as exc:  # noqa: BLE001
            raise _backtest_http_error(exc) from exc

    @app.post("/market/backtests/{backtest_id}/walk-forward")
    async def market_backtest_walk_forward(
        backtest_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.backtest_walk_forward(
                backtest_id,
                payload.get("train_bars", 0),
                payload.get("test_bars", 0),
            )
        except Exception as exc:  # noqa: BLE001
            raise _backtest_http_error(exc) from exc

    # -- PI-13: isolated reinforcement-learning research --------------------
    def _rl_http_error(exc: Exception) -> HTTPException:
        from novacontrol.novamarket.rl import (
            RLDataError,
            RLEpisodeError,
            RLInvalidActionError,
            RLStoreError,
            RLValidationError,
        )

        if isinstance(exc, HTTPException):
            return exc
        text = str(exc)
        if isinstance(exc, RLInvalidActionError):
            return HTTPException(status_code=422, detail=text)
        if isinstance(exc, RLValidationError):
            return HTTPException(status_code=404 if "unknown" in text.lower() else 422, detail=text)
        if isinstance(exc, RLEpisodeError):
            return HTTPException(status_code=409, detail=text)
        if isinstance(exc, (RLDataError, RLStoreError)):
            return HTTPException(status_code=422, detail=text)
        return HTTPException(status_code=500, detail=text)

    @app.get("/market/rl/status")
    async def market_rl_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.rl_status()

    @app.get("/market/rl/contracts")
    async def market_rl_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.rl_contract()
        except Exception as exc:  # noqa: BLE001
            raise _rl_http_error(exc) from exc

    @app.get("/market/rl/health")
    async def market_rl_health(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.rl_health()
        except Exception as exc:  # noqa: BLE001
            raise _rl_http_error(exc) from exc

    @app.post("/market/rl/environments")
    async def market_rl_create_environment(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.rl_create_environment(payload)
        except Exception as exc:  # noqa: BLE001
            raise _rl_http_error(exc) from exc

    @app.get("/market/rl/environments")
    async def market_rl_environments(
        limit: int = 50, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.rl_environments(limit)
        except Exception as exc:  # noqa: BLE001
            raise _rl_http_error(exc) from exc

    @app.get("/market/rl/environments/{environment_id}")
    async def market_rl_environment(
        environment_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.rl_environment(environment_id)
        except Exception as exc:  # noqa: BLE001
            raise _rl_http_error(exc) from exc

    @app.post("/market/rl/environments/{environment_id}/episodes")
    async def market_rl_start_episode(
        environment_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.rl_start_episode(environment_id, payload or {})
        except Exception as exc:  # noqa: BLE001
            raise _rl_http_error(exc) from exc

    @app.get("/market/rl/episodes")
    async def market_rl_episodes(
        environment_id: str = "",
        limit: int = 50,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.rl_episodes(
                environment_id=environment_id or None, limit=limit, offset=offset
            )
        except Exception as exc:  # noqa: BLE001
            raise _rl_http_error(exc) from exc

    @app.get("/market/rl/episodes/{episode_id}")
    async def market_rl_episode(
        episode_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.rl_episode(episode_id)
        except Exception as exc:  # noqa: BLE001
            raise _rl_http_error(exc) from exc

    @app.post("/market/rl/episodes/{episode_id}/step")
    async def market_rl_step(
        episode_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.rl_step(episode_id, payload)
        except Exception as exc:  # noqa: BLE001
            raise _rl_http_error(exc) from exc

    @app.post("/market/rl/episodes/{episode_id}/reset")
    async def market_rl_reset(
        episode_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.rl_reset_episode(episode_id)
        except Exception as exc:  # noqa: BLE001
            raise _rl_http_error(exc) from exc

    @app.post("/market/rl/episodes/{episode_id}/cancel")
    async def market_rl_cancel(
        episode_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.rl_cancel(episode_id)
        except Exception as exc:  # noqa: BLE001
            raise _rl_http_error(exc) from exc

    @app.get("/market/rl/episodes/{episode_id}/transitions")
    async def market_rl_transitions(
        episode_id: str,
        limit: int = 100,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.rl_transitions(episode_id, limit=limit, offset=offset)
        except Exception as exc:  # noqa: BLE001
            raise _rl_http_error(exc) from exc

    @app.get("/market/rl/episodes/{episode_id}/experiences")
    async def market_rl_experiences(
        episode_id: str,
        limit: int = 100,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.rl_experiences(episode_id, limit=limit, offset=offset)
        except Exception as exc:  # noqa: BLE001
            raise _rl_http_error(exc) from exc

    # -- PI-14: reward calculation and policy evaluation --------------------
    def _reward_http_error(exc: Exception) -> HTTPException:
        from novacontrol.novamarket.rewards import (
            PolicyEvaluationError,
            RewardStoreError,
            RewardValidationError,
        )

        if isinstance(exc, HTTPException):
            return exc
        text = str(exc)
        if isinstance(exc, RewardValidationError):
            return HTTPException(status_code=404 if "unknown" in text.lower() else 422, detail=text)
        if isinstance(exc, PolicyEvaluationError):
            return HTTPException(status_code=422, detail=text)
        if isinstance(exc, RewardStoreError):
            return HTTPException(status_code=409, detail=text)
        return HTTPException(status_code=500, detail=text)

    @app.get("/market/rewards/status")
    async def market_rewards_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.reward_status()

    @app.get("/market/rewards/contracts")
    async def market_rewards_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.reward_contract()
        except Exception as exc:  # noqa: BLE001
            raise _reward_http_error(exc) from exc

    @app.get("/market/rewards/health")
    async def market_rewards_health(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.reward_health()
        except Exception as exc:  # noqa: BLE001
            raise _reward_http_error(exc) from exc

    @app.get("/market/rewards/baselines")
    async def market_rewards_baselines(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.reward_baselines()
        except Exception as exc:  # noqa: BLE001
            raise _reward_http_error(exc) from exc

    @app.post("/market/rewards/configurations")
    async def market_rewards_create_configuration(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.reward_create_config(payload)
        except Exception as exc:  # noqa: BLE001
            raise _reward_http_error(exc) from exc

    @app.get("/market/rewards/configurations")
    async def market_rewards_configurations(
        limit: int = 50, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.reward_configs(limit)
        except Exception as exc:  # noqa: BLE001
            raise _reward_http_error(exc) from exc

    @app.get("/market/rewards/configurations/{config_id}")
    async def market_rewards_configuration(
        config_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.reward_config(config_id)
        except Exception as exc:  # noqa: BLE001
            raise _reward_http_error(exc) from exc

    @app.post("/market/rewards/episodes/{episode_id}/calculate")
    async def market_rewards_calculate_episode(
        episode_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                nova.novamarket.reward_calculate_episode,
                episode_id,
                config_id=str((payload or {}).get("config_id") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise _reward_http_error(exc) from exc

    @app.get("/market/rewards/episodes/{episode_id}")
    async def market_rewards_episode(
        episode_id: str,
        limit: int = 200,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.reward_episode_rewards(episode_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _reward_http_error(exc) from exc

    @app.get("/market/rewards/results/{reward_id}")
    async def market_rewards_result(
        reward_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.reward_result(reward_id)
        except Exception as exc:  # noqa: BLE001
            raise _reward_http_error(exc) from exc

    @app.post("/market/rewards/evaluations")
    async def market_rewards_evaluate(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                nova.novamarket.reward_evaluate,
                payload,
                request_id=str(payload.get("request_id") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise _reward_http_error(exc) from exc

    @app.post("/market/rewards/evaluations/cancel")
    async def market_rewards_cancel_evaluation(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.reward_cancel_evaluation(str(payload.get("request_id") or ""))
        except Exception as exc:  # noqa: BLE001
            raise _reward_http_error(exc) from exc

    @app.get("/market/rewards/evaluations")
    async def market_rewards_evaluations(
        limit: int = 50, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.reward_evaluations(limit)
        except Exception as exc:  # noqa: BLE001
            raise _reward_http_error(exc) from exc

    @app.get("/market/rewards/evaluations/{evaluation_id}")
    async def market_rewards_evaluation(
        evaluation_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.reward_evaluation(evaluation_id)
        except Exception as exc:  # noqa: BLE001
            raise _reward_http_error(exc) from exc

    @app.get("/market/rewards/comparisons")
    async def market_rewards_comparisons(
        limit: int = 50, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.reward_comparisons(limit)
        except Exception as exc:  # noqa: BLE001
            raise _reward_http_error(exc) from exc

    @app.post("/market/rewards/stability")
    async def market_rewards_stability(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(nova.novamarket.reward_stability, payload)
        except Exception as exc:  # noqa: BLE001
            raise _reward_http_error(exc) from exc

    @app.post("/market/rewards/walk-forward")
    async def market_rewards_walk_forward(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(nova.novamarket.reward_walk_forward, payload)
        except Exception as exc:  # noqa: BLE001
            raise _reward_http_error(exc) from exc

    # -- PI-15: experimental RL training and optimisation -------------------
    def _training_http_error(exc: Exception) -> HTTPException:
        from novacontrol.novamarket.agent_training.models import (
            TrainingError,
            TrainingResourceError,
            TrainingStateError,
            TrainingStoreError,
            TrainingValidationError,
        )
        from novacontrol.novamarket.rl.models import RLValidationError

        if isinstance(exc, HTTPException):
            return exc
        text = str(exc)
        # A malformed configuration is the caller's error, not a server fault:
        # the RL environment contract rejects missing dataset fields with its own
        # exception type, which used to answer 500 for an empty form.  Bare
        # ValueError is deliberately NOT mapped: a genuine internal failure must
        # report as a server fault instead of being blamed on the request.
        if isinstance(exc, (TrainingValidationError, RLValidationError)):
            return HTTPException(
                status_code=404 if "unknown" in text.lower() else 422, detail=text
            )
        if isinstance(exc, TrainingError):
            return HTTPException(status_code=422, detail=text)
        if isinstance(exc, (TrainingStateError, TrainingResourceError)):
            return HTTPException(status_code=409, detail=text)
        if isinstance(exc, TrainingStoreError):
            return HTTPException(status_code=409, detail=text)
        return HTTPException(status_code=500, detail=text)

    @app.get("/market/training/status")
    async def market_training_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.training_status()

    @app.get("/market/training/contracts")
    async def market_training_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.training_contract()
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.get("/market/training/health")
    async def market_training_health(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.training_health()
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.post("/market/training/configurations")
    async def market_training_create_configuration(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(nova.novamarket.training_create_config, payload)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.get("/market/training/configurations")
    async def market_training_configurations(
        limit: int = 50, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.training_configs(limit)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.get("/market/training/configurations/{config_id}")
    async def market_training_configuration(
        config_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.training_config(config_id)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.get("/market/training/datasets")
    async def market_training_datasets(
        dataset_id: str = "",
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Stored PI-02 dataset versions a training run can actually reference.

        Each row also names the instruments it holds, because a run over an
        instrument the dataset does not contain cannot prepare any split.
        """
        try:
            versions = nova.novamarket.dataset_versions(
                dataset_id.strip() or None, limit=limit
            )
            items = []
            for item in versions.get("items", ()):
                row = dict(item)
                try:
                    coverage = nova.novamarket.dataset_coverage(
                        str(row.get("dataset_id") or ""), str(row.get("version") or "")
                    )
                except Exception:  # noqa: BLE001 - a listing still answers
                    coverage = {}
                row["instruments"] = [
                    str(segment.get("instrument_id") or "")
                    for segment in coverage.get("segments", ())
                    if segment.get("instrument_id")
                ]
                items.append(row)
            return {**versions, "items": items}
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.post("/market/training/validate")
    async def market_training_validate(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(nova.novamarket.training_validate, payload)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.post("/market/training/runs")
    async def market_training_start(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(nova.novamarket.training_start, payload)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.get("/market/training/runs")
    async def market_training_runs(
        limit: int = 50,
        status: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.training_runs(limit, status=status)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.get("/market/training/runs/{run_id}")
    async def market_training_run(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.training_run(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.post("/market/training/runs/{run_id}/pause")
    async def market_training_pause(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.training_pause(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.post("/market/training/runs/{run_id}/resume")
    async def market_training_resume(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.training_resume(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.post("/market/training/runs/{run_id}/cancel")
    async def market_training_cancel(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.training_cancel(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.post("/market/training/runs/{run_id}/retry")
    async def market_training_retry(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(nova.novamarket.training_retry, run_id)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.get("/market/training/runs/{run_id}/metrics")
    async def market_training_metrics(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.training_metrics(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.get("/market/training/runs/{run_id}/checkpoints")
    async def market_training_run_checkpoints(
        run_id: str,
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.training_checkpoints(run_id, limit)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.post("/market/training/runs/{run_id}/checkpoints")
    async def market_training_request_checkpoint(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.training_request_checkpoint(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.get("/market/training/checkpoints/{checkpoint_id}")
    async def market_training_checkpoint(
        checkpoint_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.training_checkpoint(checkpoint_id)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.post("/market/training/checkpoints/{checkpoint_id}/resume")
    async def market_training_resume_checkpoint(
        checkpoint_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                nova.novamarket.training_resume_checkpoint, checkpoint_id
            )
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.get("/market/training/policies")
    async def market_training_policies(
        limit: int = 50,
        status: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.training_policies(limit, status=status)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.get("/market/training/policies/{policy_id}")
    async def market_training_policy(
        policy_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.training_policy(policy_id)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.post("/market/training/policies/{policy_id}/archive")
    async def market_training_archive_policy(
        policy_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.training_archive_policy(policy_id)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.post("/market/training/policies/{policy_id}/validate")
    async def market_training_validate_policy(
        policy_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(nova.novamarket.training_validate_policy, policy_id)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.post("/market/training/policies/{policy_id}/test")
    async def market_training_test_policy(
        policy_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(nova.novamarket.training_test_policy, policy_id)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.post("/market/training/compare")
    async def market_training_compare(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.training_compare(payload)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.post("/market/training/optimize")
    async def market_training_optimize(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(nova.novamarket.training_optimize, payload)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.get("/market/training/searches/{search_id}")
    async def market_training_search(
        search_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.training_search(search_id)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    @app.get("/market/training/evaluations")
    async def market_training_evaluations(
        policy_id: str = "",
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.training_evaluations(policy_id, limit)
        except Exception as exc:  # noqa: BLE001
            raise _training_http_error(exc) from exc

    # -- PI-16: adaptive learning and market-regime intelligence ------------
    def _adaptive_http_error(exc: Exception) -> HTTPException:
        from novacontrol.novamarket.adaptive import (
            AdaptiveError,
            AdaptiveNotFoundError,
            AdaptiveResourceError,
            AdaptiveStateError,
            AdaptiveStoreError,
            AdaptiveValidationError,
        )

        if isinstance(exc, HTTPException):
            return exc
        detail = str(exc)
        if isinstance(exc, AdaptiveNotFoundError):
            return HTTPException(status_code=404, detail=detail)
        if isinstance(exc, AdaptiveValidationError):
            return HTTPException(status_code=422, detail=detail)
        if isinstance(exc, (AdaptiveStateError, AdaptiveStoreError, AdaptiveResourceError)):
            return HTTPException(status_code=409, detail=detail)
        if isinstance(exc, AdaptiveError):
            return HTTPException(status_code=422, detail=detail)
        return HTTPException(status_code=500, detail=detail)

    @app.get("/market/adaptive/status")
    async def market_adaptive_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.adaptive_status()

    @app.get("/market/adaptive/contracts")
    async def market_adaptive_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.adaptive_contract()

    @app.get("/market/adaptive/health")
    async def market_adaptive_health(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.adaptive_health()

    @app.get("/market/adaptive/dashboard")
    async def market_adaptive_dashboard(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.adaptive_dashboard()

    @app.post("/market/adaptive/configurations")
    async def market_adaptive_create_config(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.adaptive_create_config(payload)
        except Exception as exc:  # noqa: BLE001
            raise _adaptive_http_error(exc) from exc

    @app.get("/market/adaptive/configurations")
    async def market_adaptive_configs(
        limit: int = 50, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        return nova.novamarket.adaptive_configs(limit)

    @app.get("/market/adaptive/configurations/{config_id}")
    async def market_adaptive_config(
        config_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.adaptive_config(config_id)
        except Exception as exc:  # noqa: BLE001
            raise _adaptive_http_error(exc) from exc

    @app.post("/market/adaptive/regime/analyze")
    async def market_adaptive_regime_analyze(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.adaptive_regime_analysis(payload)
        except Exception as exc:  # noqa: BLE001
            raise _adaptive_http_error(exc) from exc

    @app.get("/market/adaptive/regime/latest")
    async def market_adaptive_latest_regime(
        instrument_id: str = "", _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.adaptive_latest_regime(instrument_id)
        except Exception as exc:  # noqa: BLE001
            raise _adaptive_http_error(exc) from exc

    @app.post("/market/adaptive/regime/annotations")
    async def market_adaptive_annotate_regimes(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.adaptive_annotate_regimes(payload)
        except Exception as exc:  # noqa: BLE001
            raise _adaptive_http_error(exc) from exc

    @app.get("/market/adaptive/regime/annotations")
    async def market_adaptive_annotations(
        instrument_id: str = "",
        limit: int = 100,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.adaptive_annotations(instrument_id, limit, offset)

    @app.post("/market/adaptive/regime/evaluations")
    async def market_adaptive_record_regime_evaluation(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.adaptive_record_regime_evaluation(payload)
        except Exception as exc:  # noqa: BLE001
            raise _adaptive_http_error(exc) from exc

    @app.get("/market/adaptive/regime/evaluations")
    async def market_adaptive_regime_evaluations(
        policy_id: str = "",
        regime: str = "",
        limit: int = 100,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.adaptive_regime_performance(
            policy_id, regime=regime, limit=limit, offset=offset
        )

    @app.post("/market/adaptive/experiences/query")
    async def market_adaptive_regime_experiences(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.adaptive_regime_experiences(payload)
        except Exception as exc:  # noqa: BLE001
            raise _adaptive_http_error(exc) from exc

    @app.post("/market/adaptive/changes/detect")
    async def market_adaptive_detect_change(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.adaptive_detect_change(payload)
        except Exception as exc:  # noqa: BLE001
            raise _adaptive_http_error(exc) from exc

    @app.get("/market/adaptive/changes")
    async def market_adaptive_changes(
        instrument_id: str = "",
        limit: int = 100,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.adaptive_changes(instrument_id, limit, offset)

    @app.post("/market/adaptive/drift/detect")
    async def market_adaptive_detect_drift(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.adaptive_detect_drift(payload)
        except Exception as exc:  # noqa: BLE001
            raise _adaptive_http_error(exc) from exc

    @app.get("/market/adaptive/drift")
    async def market_adaptive_drift(
        subject: str = "",
        limit: int = 100,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.adaptive_drift(subject, limit, offset)

    @app.post("/market/adaptive/degradation/monitor")
    async def market_adaptive_monitor_degradation(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            result = nova.novamarket.adaptive_monitor_degradation(payload)
        except Exception as exc:  # noqa: BLE001
            raise _adaptive_http_error(exc) from exc
        await nova.novamarket.announce_policy_degradation(result)
        return result

    @app.get("/market/adaptive/degradation")
    async def market_adaptive_degradation(
        policy_id: str = "",
        limit: int = 100,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.adaptive_degradation(policy_id, limit, offset)

    @app.post("/market/adaptive/suitability/assess")
    async def market_adaptive_assess_suitability(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.adaptive_assess_suitability(payload)
        except Exception as exc:  # noqa: BLE001
            raise _adaptive_http_error(exc) from exc

    @app.get("/market/adaptive/suitability")
    async def market_adaptive_suitability(
        policy_id: str = "",
        limit: int = 100,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.adaptive_suitability(policy_id, limit, offset)

    @app.post("/market/adaptive/recommendations")
    async def market_adaptive_recommend(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.adaptive_recommend(payload)
        except Exception as exc:  # noqa: BLE001
            raise _adaptive_http_error(exc) from exc

    @app.get("/market/adaptive/recommendations")
    async def market_adaptive_recommendations(
        policy_id: str = "",
        status: str = "",
        limit: int = 100,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.adaptive_recommendations(
            policy_id, status=status, limit=limit, offset=offset
        )

    @app.post("/market/adaptive/recommendations/{recommendation_id}/acknowledge")
    async def market_adaptive_acknowledge_recommendation(
        recommendation_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.adaptive_acknowledge_recommendation(recommendation_id, payload)
        except Exception as exc:  # noqa: BLE001
            raise _adaptive_http_error(exc) from exc

    @app.post("/market/adaptive/retraining")
    async def market_adaptive_retraining_recommendation(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.adaptive_retraining_recommendation(payload)
        except Exception as exc:  # noqa: BLE001
            raise _adaptive_http_error(exc) from exc

    @app.get("/market/adaptive/retraining")
    async def market_adaptive_retraining(
        policy_id: str = "",
        limit: int = 100,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.adaptive_retraining(policy_id, limit, offset)

    @app.post("/market/adaptive/reassessments")
    async def market_adaptive_create_reassessment(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.adaptive_create_reassessment(payload)
        except Exception as exc:  # noqa: BLE001
            raise _adaptive_http_error(exc) from exc

    @app.get("/market/adaptive/reassessments")
    async def market_adaptive_reassessments(
        policy_id: str = "",
        limit: int = 100,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.adaptive_reassessments(policy_id, limit, offset)

    @app.post("/market/adaptive/reassessments/{reassessment_id}/run")
    async def market_adaptive_run_reassessment(
        reassessment_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                nova.novamarket.adaptive_run_reassessment, reassessment_id
            )
        except Exception as exc:  # noqa: BLE001
            raise _adaptive_http_error(exc) from exc

    @app.get("/market/adaptive/reassessments/{reassessment_id}")
    async def market_adaptive_reassessment(
        reassessment_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.adaptive_reassessment(reassessment_id)
        except Exception as exc:  # noqa: BLE001
            raise _adaptive_http_error(exc) from exc

    # -- PI-17: advisory multi-agent financial intelligence ------------------
    def _multiagent_http_error(exc: Exception) -> HTTPException:
        from novacontrol.novamarket.multiagent import (
            MultiAgentError,
            MultiAgentNotFoundError,
            MultiAgentResourceError,
            MultiAgentStateError,
            MultiAgentStoreError,
            MultiAgentValidationError,
        )

        if isinstance(exc, HTTPException):
            return exc
        detail = str(exc)
        if isinstance(exc, MultiAgentNotFoundError):
            return HTTPException(status_code=404, detail=detail)
        if isinstance(exc, MultiAgentValidationError):
            return HTTPException(status_code=422, detail=detail)
        if isinstance(exc, MultiAgentStateError):
            return HTTPException(status_code=409, detail=detail)
        if isinstance(exc, MultiAgentResourceError):
            return HTTPException(status_code=429, detail=detail)
        if isinstance(exc, MultiAgentStoreError):
            return HTTPException(status_code=503, detail=detail)
        if isinstance(exc, MultiAgentError):
            return HTTPException(status_code=422, detail=detail)
        if isinstance(exc, PermissionError):
            return HTTPException(status_code=403, detail=detail)
        return HTTPException(status_code=500, detail=detail)

    @app.get("/market/multiagent/status")
    async def market_multiagent_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.multiagent_status()
        except Exception as exc:  # noqa: BLE001
            raise _multiagent_http_error(exc) from exc

    @app.get("/market/multiagent/contracts")
    async def market_multiagent_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.multiagent_contract()
        except Exception as exc:  # noqa: BLE001
            raise _multiagent_http_error(exc) from exc

    @app.get("/market/multiagent/health")
    async def market_multiagent_health(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.multiagent_health()
        except Exception as exc:  # noqa: BLE001
            raise _multiagent_http_error(exc) from exc

    @app.get("/market/multiagent/agents")
    async def market_multiagent_agents(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.multiagent_agents()
        except Exception as exc:  # noqa: BLE001
            raise _multiagent_http_error(exc) from exc

    @app.get("/market/multiagent/agents/{agent_id}")
    async def market_multiagent_agent(
        agent_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.multiagent_agent(agent_id)
        except Exception as exc:  # noqa: BLE001
            raise _multiagent_http_error(exc) from exc

    @app.put("/market/multiagent/agents/{agent_id}/config")
    async def market_multiagent_update_config(
        agent_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.multiagent_update_config(agent_id, payload)
        except Exception as exc:  # noqa: BLE001
            raise _multiagent_http_error(exc) from exc

    @app.post("/market/multiagent/cycles")
    async def market_multiagent_create_cycle(
        payload: dict[str, Any],
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.multiagent_create_cycle(payload, idempotency_key=idempotency_key)
        except Exception as exc:  # noqa: BLE001
            raise _multiagent_http_error(exc) from exc

    @app.get("/market/multiagent/cycles")
    async def market_multiagent_cycles(
        instrument_id: str = "",
        cycle_status: str = "",
        limit: int = 50,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.multiagent_cycles(
                instrument_id=instrument_id,
                status=cycle_status,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:  # noqa: BLE001
            raise _multiagent_http_error(exc) from exc

    @app.get("/market/multiagent/cycles/{cycle_id}")
    async def market_multiagent_cycle(
        cycle_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.multiagent_cycle(cycle_id)
        except Exception as exc:  # noqa: BLE001
            raise _multiagent_http_error(exc) from exc

    @app.post("/market/multiagent/cycles/{cycle_id}/run")
    async def market_multiagent_run_cycle(
        cycle_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(nova.novamarket.multiagent_run_cycle, cycle_id)
        except Exception as exc:  # noqa: BLE001
            raise _multiagent_http_error(exc) from exc
        # A completed cycle must refresh the open dashboard view without polling.
        await nova.novamarket.announce_agent_cycle(result)
        return result

    @app.post("/market/multiagent/cycles/{cycle_id}/cancel")
    async def market_multiagent_cancel_cycle(
        cycle_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.multiagent_cancel_cycle(cycle_id)
        except Exception as exc:  # noqa: BLE001
            raise _multiagent_http_error(exc) from exc

    @app.get("/market/multiagent/cycles/{cycle_id}/agents")
    async def market_multiagent_cycle_agents(
        cycle_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.multiagent_runs(cycle_id)
        except Exception as exc:  # noqa: BLE001
            raise _multiagent_http_error(exc) from exc

    @app.get("/market/multiagent/cycles/{cycle_id}/evidence")
    async def market_multiagent_cycle_evidence(
        cycle_id: str,
        agent_id: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.multiagent_evidence(cycle_id, agent_id=agent_id)
        except Exception as exc:  # noqa: BLE001
            raise _multiagent_http_error(exc) from exc

    @app.get("/market/multiagent/cycles/{cycle_id}/conflicts")
    async def market_multiagent_cycle_conflicts(
        cycle_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.multiagent_conflicts(cycle_id)
        except Exception as exc:  # noqa: BLE001
            raise _multiagent_http_error(exc) from exc

    @app.get("/market/multiagent/cycles/{cycle_id}/decision")
    async def market_multiagent_cycle_decision(
        cycle_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.multiagent_decision(cycle_id)
        except Exception as exc:  # noqa: BLE001
            raise _multiagent_http_error(exc) from exc

    @app.get("/market/multiagent/cycles/{cycle_id}/handoff")
    async def market_multiagent_cycle_handoff(
        cycle_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.multiagent_handoff(cycle_id)
        except Exception as exc:  # noqa: BLE001
            raise _multiagent_http_error(exc) from exc

    @app.get("/market/multiagent/history")
    async def market_multiagent_history(
        instrument_id: str = "",
        limit: int = 50,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.multiagent_history(
                instrument_id=instrument_id, limit=limit, offset=offset
            )
        except Exception as exc:  # noqa: BLE001
            raise _multiagent_http_error(exc) from exc

    @app.get("/market/multiagent/performance")
    async def market_multiagent_performance(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.multiagent_performance()
        except Exception as exc:  # noqa: BLE001
            raise _multiagent_http_error(exc) from exc

    @app.get("/market/multiagent/performance/{agent_id}")
    async def market_multiagent_agent_performance(
        agent_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.multiagent_performance(agent_id)
        except Exception as exc:  # noqa: BLE001
            raise _multiagent_http_error(exc) from exc

    # -- PI-19: advanced market intelligence and financial research ----------
    def _research_http_error(exc: Exception) -> HTTPException:
        from novacontrol.novamarket.research import (
            ResearchError,
            ResearchNotFoundError,
            ResearchResourceError,
            ResearchStateError,
            ResearchStoreError,
            ResearchValidationError,
        )

        if isinstance(exc, HTTPException):
            return exc
        detail = str(exc)
        if isinstance(exc, ResearchNotFoundError):
            return HTTPException(status_code=404, detail=detail)
        if isinstance(exc, ResearchValidationError):
            return HTTPException(status_code=422, detail=detail)
        if isinstance(exc, ResearchStateError):
            return HTTPException(status_code=409, detail=detail)
        if isinstance(exc, ResearchResourceError):
            return HTTPException(status_code=429, detail=detail)
        if isinstance(exc, ResearchStoreError):
            return HTTPException(status_code=503, detail=detail)
        if isinstance(exc, ResearchError):
            return HTTPException(status_code=422, detail=detail)
        if isinstance(exc, PermissionError):
            return HTTPException(status_code=403, detail=detail)
        return HTTPException(status_code=500, detail=detail)

    @app.get("/market/research/status")
    async def market_research_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.research_status()
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.get("/market/research/contracts")
    async def market_research_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.research_contract()
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.get("/market/research/health")
    async def market_research_health(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.research_health()
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.post("/market/research/runs")
    async def market_research_create(
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.research_create(payload)
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.post("/market/research/runs/{run_id}/run")
    async def market_research_run(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(nova.novamarket.research_run, run_id)
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc
        await nova.novamarket.announce_research_run(result)
        return result

    @app.post("/market/research/runs/{run_id}/cancel")
    async def market_research_cancel(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.research_cancel(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.post("/market/research/runs/{run_id}/refresh")
    async def market_research_refresh(
        run_id: str,
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(nova.novamarket.research_refresh, run_id, payload or {})
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.get("/market/research/runs")
    async def market_research_runs(
        instrument_id: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.research_history(
                instrument_id=instrument_id,
                status=status,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.get("/market/research/history")
    async def market_research_history(
        instrument_id: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.research_history(
                instrument_id=instrument_id,
                status=status,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.get("/market/research/latest")
    async def market_research_latest(
        instrument_id: str,
        horizon: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return {
                "ok": True,
                "run": nova.novamarket.research_latest(instrument_id, horizon=horizon),
            }
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.get("/market/research/runs/{run_id}")
    async def market_research_detail(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.research_detail(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.get("/market/research/runs/{run_id}/company")
    async def market_research_company(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.research_section(run_id, "company")
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.get("/market/research/runs/{run_id}/financials")
    async def market_research_financials(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.research_section(run_id, "financials")
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.get("/market/research/runs/{run_id}/events")
    async def market_research_events(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.research_section(run_id, "events")
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.get("/market/research/runs/{run_id}/news")
    async def market_research_news(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.research_section(run_id, "news")
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.get("/market/research/runs/{run_id}/sector")
    async def market_research_sector(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.research_section(run_id, "sector")
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.get("/market/research/runs/{run_id}/peers")
    async def market_research_peers(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.research_section(run_id, "peers")
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.get("/market/research/runs/{run_id}/valuation")
    async def market_research_valuation(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.research_section(run_id, "valuation")
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.get("/market/research/runs/{run_id}/thesis")
    async def market_research_thesis(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            run = nova.novamarket.research_detail(run_id)
            return {"run_id": run_id, "thesis": run.get("thesis")}
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.get("/market/research/runs/{run_id}/evidence")
    async def market_research_evidence(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.research_evidence(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.get("/market/research/runs/{run_id}/claims")
    async def market_research_claims(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.research_claims(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.get("/market/research/runs/{run_id}/features")
    async def market_research_features(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.research_features(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.get("/market/research/runs/{run_id}/handoff")
    async def market_research_handoff(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.research_handoff(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    @app.post("/market/research/monitor")
    async def market_research_monitor(
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            instrument_id = str(payload.get("instrument_id") or "")
            return await asyncio.to_thread(
                nova.novamarket.research_monitor,
                instrument_id,
                horizon=str(payload.get("horizon") or ""),
                min_interval_hours=float(payload.get("min_interval_hours", 6.0)),
            )
        except Exception as exc:  # noqa: BLE001
            raise _research_http_error(exc) from exc

    # -- PI-18: read-only portfolio optimization and capital allocation ------
    def _optimization_http_error(exc: Exception) -> HTTPException:
        from novacontrol.novamarket.optimization import (
            OptimizationError,
            OptimizationNotFoundError,
            OptimizationResourceError,
            OptimizationStateError,
            OptimizationStoreError,
            OptimizationValidationError,
        )

        if isinstance(exc, HTTPException):
            return exc
        detail = str(exc)
        if isinstance(exc, OptimizationNotFoundError):
            return HTTPException(status_code=404, detail=detail)
        if isinstance(exc, OptimizationValidationError):
            return HTTPException(status_code=422, detail=detail)
        if isinstance(exc, OptimizationStateError):
            return HTTPException(status_code=409, detail=detail)
        if isinstance(exc, OptimizationResourceError):
            return HTTPException(status_code=429, detail=detail)
        if isinstance(exc, OptimizationStoreError):
            return HTTPException(status_code=503, detail=detail)
        if isinstance(exc, OptimizationError):
            return HTTPException(status_code=422, detail=detail)
        if isinstance(exc, PermissionError):
            return HTTPException(status_code=403, detail=detail)
        return HTTPException(status_code=500, detail=detail)

    @app.get("/market/optimization/status")
    async def market_optimization_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.optimization_status()
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.get("/market/optimization/contracts")
    async def market_optimization_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.optimization_contract()
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.get("/market/optimization/health")
    async def market_optimization_health(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.optimization_health()
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.post("/market/optimization/configs")
    async def market_optimization_create_config(
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.optimization_create_config(payload)
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.get("/market/optimization/configs")
    async def market_optimization_configs(
        portfolio_id: str = "",
        limit: int = 50,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.optimization_configs(
                portfolio_id=portfolio_id, limit=limit, offset=offset
            )
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.get("/market/optimization/configs/{config_id}")
    async def market_optimization_config(
        config_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.optimization_config(config_id)
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.post("/market/optimization/validate")
    async def market_optimization_validate(
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(nova.novamarket.optimization_validate, payload)
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.post("/market/optimization/runs")
    async def market_optimization_run(
        payload: dict[str, Any],
        idempotency_key: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(
                nova.novamarket.optimization_run,
                payload,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc
        await nova.novamarket.announce_optimization_run(result)
        return result

    @app.get("/market/optimization/runs")
    async def market_optimization_runs(
        portfolio_id: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.optimization_runs(
                portfolio_id=portfolio_id,
                status=status,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.get("/market/optimization/history")
    async def market_optimization_history(
        portfolio_id: str = "",
        limit: int = 50,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.optimization_runs(
                portfolio_id=portfolio_id, limit=limit, offset=offset
            )
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.get("/market/optimization/latest")
    async def market_optimization_latest(
        portfolio_id: str,
        horizon: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            row = nova.novamarket.optimization_latest(portfolio_id, horizon=horizon)
            return {"ok": True, "run": row}
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.get("/market/optimization/runs/{run_id}")
    async def market_optimization_run_record(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.optimization_run_record(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.get("/market/optimization/runs/{run_id}/allocations")
    async def market_optimization_allocations(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.optimization_allocations(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.get("/market/optimization/runs/{run_id}/diversification")
    async def market_optimization_diversification(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.optimization_diversification(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.get("/market/optimization/runs/{run_id}/risk")
    async def market_optimization_risk(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.optimization_risk(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.get("/market/optimization/runs/{run_id}/liquidity")
    async def market_optimization_liquidity(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.optimization_liquidity(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.get("/market/optimization/runs/{run_id}/rebalancing")
    async def market_optimization_rebalancing(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.optimization_rebalancing(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.get("/market/optimization/runs/{run_id}/scenarios")
    async def market_optimization_scenarios(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.optimization_scenarios(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.post("/market/optimization/runs/{run_id}/scenarios")
    async def market_optimization_run_scenarios(
        run_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                nova.novamarket.optimization_run_scenarios, run_id, payload
            )
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.post("/market/optimization/runs/{run_id}/backtests")
    async def market_optimization_backtest(
        run_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(nova.novamarket.optimization_backtest, run_id, payload)
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.get("/market/optimization/runs/{run_id}/handoff")
    async def market_optimization_handoff(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.optimization_handoff(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.post("/market/optimization/runs/{run_id}/cancel")
    async def market_optimization_cancel(
        run_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.optimization_cancel(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.get("/market/optimization/backtests")
    async def market_optimization_backtests(
        run_id: str = "",
        limit: int = 50,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.optimization_backtests(run_id=run_id, limit=limit, offset=offset)
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    @app.get("/market/optimization/backtests/{backtest_id}")
    async def market_optimization_backtest_record(
        backtest_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.optimization_backtest_record(backtest_id)
        except Exception as exc:  # noqa: BLE001
            raise _optimization_http_error(exc) from exc

    # -- PI-20: prediction intelligence dashboard (read-only) ----------------
    def _dashboard_http_error(exc: Exception) -> HTTPException:
        from novacontrol.novamarket.dashboard import (
            DashboardError,
            DashboardNotFoundError,
            DashboardStoreError,
            DashboardValidationError,
        )

        if isinstance(exc, HTTPException):
            return exc
        detail = str(exc)
        if isinstance(exc, DashboardNotFoundError):
            return HTTPException(status_code=404, detail=detail)
        if isinstance(exc, DashboardValidationError):
            return HTTPException(status_code=422, detail=detail)
        if isinstance(exc, DashboardStoreError):
            return HTTPException(status_code=503, detail=detail)
        if isinstance(exc, DashboardError):
            return HTTPException(status_code=422, detail=detail)
        if isinstance(exc, PermissionError):
            return HTTPException(status_code=403, detail=detail)
        return HTTPException(status_code=500, detail=detail)

    def _dashboard_filters(
        instrument_id: str = "",
        exchange: str = "",
        sector: str = "",
        regime: str = "",
        horizon: str = "",
        model_id: str = "",
        direction: str = "",
        validity: str = "",
        evaluation_start: str = "",
        evaluation_end: str = "",
        decision_status: str = "",
        agent_id: str = "",
        portfolio_id: str = "",
        research_status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        from novacontrol.novamarket.dashboard import DashboardFilters

        return DashboardFilters.from_mapping(
            {
                "instrument_id": instrument_id,
                "exchange": exchange,
                "sector": sector,
                "regime": regime,
                "horizon": horizon,
                "model_id": model_id,
                "direction": direction,
                "validity": validity,
                "evaluation_start": evaluation_start,
                "evaluation_end": evaluation_end,
                "decision_status": decision_status,
                "agent_id": agent_id,
                "portfolio_id": portfolio_id,
                "research_status": research_status,
                "limit": limit,
                "offset": offset,
            }
        ).to_dict()

    @app.get("/market/intelligence/status")
    async def market_intelligence_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.dashboard_status()
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/contracts")
    async def market_intelligence_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.dashboard_contract()
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/navigation")
    async def market_intelligence_navigation(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.dashboard_navigation()
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/overview")
    async def market_intelligence_overview(
        instrument_id: str = "",
        exchange: str = "",
        sector: str = "",
        regime: str = "",
        horizon: str = "",
        model_id: str = "",
        direction: str = "",
        validity: str = "",
        limit: int = 50,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            filters = _dashboard_filters(
                instrument_id=instrument_id,
                exchange=exchange,
                sector=sector,
                regime=regime,
                horizon=horizon,
                model_id=model_id,
                direction=direction,
                validity=validity,
                limit=limit,
                offset=offset,
            )
            return nova.novamarket.dashboard_overview(filters)
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/instruments/{instrument_id}")
    async def market_intelligence_instrument(
        instrument_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.dashboard_instrument(instrument_id)
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/forecasts")
    async def market_intelligence_forecasts(
        instrument_id: str = "",
        exchange: str = "",
        sector: str = "",
        regime: str = "",
        horizon: str = "",
        model_id: str = "",
        direction: str = "",
        validity: str = "",
        limit: int = 50,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            filters = _dashboard_filters(
                instrument_id=instrument_id,
                exchange=exchange,
                sector=sector,
                regime=regime,
                horizon=horizon,
                model_id=model_id,
                direction=direction,
                validity=validity,
                limit=limit,
                offset=offset,
            )
            return nova.novamarket.dashboard_forecasts(filters)
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/compare")
    async def market_intelligence_compare(
        instrument_id: str = "",
        exchange: str = "",
        sector: str = "",
        regime: str = "",
        horizon: str = "",
        model_id: str = "",
        direction: str = "",
        validity: str = "",
        limit: int = 100,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            filters = _dashboard_filters(
                instrument_id=instrument_id,
                exchange=exchange,
                sector=sector,
                regime=regime,
                horizon=horizon,
                model_id=model_id,
                direction=direction,
                validity=validity,
                limit=limit,
                offset=offset,
            )
            return nova.novamarket.dashboard_compare(filters)
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/horizons/{instrument_id}")
    async def market_intelligence_horizons(
        instrument_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.dashboard_horizons(instrument_id)
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/lifecycle")
    async def market_intelligence_lifecycle(
        cycle_id: str = "",
        instrument_id: str = "",
        limit: int = 20,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.dashboard_lifecycle(
                cycle_id=cycle_id, instrument_id=instrument_id, limit=limit
            )
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/performance")
    async def market_intelligence_performance(
        evaluation_id: str = "",
        instrument_id: str = "",
        horizon: str = "",
        evaluation_start: str = "",
        evaluation_end: str = "",
        limit: int = 20,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.dashboard_performance(
                evaluation_id=evaluation_id,
                instrument_id=instrument_id,
                horizon=horizon,
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/regimes")
    async def market_intelligence_regimes(
        instrument_id: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.dashboard_regimes(instrument_id=instrument_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/decisions")
    async def market_intelligence_decisions(
        instrument_id: str = "",
        status: str = "",
        agent_id: str = "",
        limit: int = 30,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.dashboard_decisions(
                instrument_id=instrument_id,
                status=status,
                agent_id=agent_id,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/decisions/{cycle_id}")
    async def market_intelligence_decision(
        cycle_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.dashboard_decision(cycle_id)
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/explain/{subject_id}")
    async def market_intelligence_explain(
        subject_id: str,
        kind: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.dashboard_explain(subject_id, kind=kind)
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/attribution/{forecast_id}")
    async def market_intelligence_attribution(
        forecast_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.dashboard_attribution(forecast_id)
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/evidence/{subject_id}")
    async def market_intelligence_evidence(
        subject_id: str,
        kind: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.dashboard_evidence(subject_id, kind=kind)
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/research")
    async def market_intelligence_research(
        instrument_id: str = "",
        run_id: str = "",
        research_status: str = "",
        limit: int = 20,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.dashboard_research(
                instrument_id=instrument_id,
                run_id=run_id,
                research_status=research_status,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/portfolio")
    async def market_intelligence_portfolio(
        portfolio_id: str = "",
        limit: int = 20,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.dashboard_portfolio(portfolio_id=portfolio_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/policies")
    async def market_intelligence_policies(
        policy_id: str = "",
        limit: int = 30,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.dashboard_policies(policy_id=policy_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/replay/{cycle_id}")
    async def market_intelligence_replay(
        cycle_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.dashboard_replay(cycle_id)
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.get("/market/intelligence/filters")
    async def market_intelligence_filters(
        name: str = "default",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.dashboard_filters(name=name)
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    @app.put("/market/intelligence/filters")
    async def market_intelligence_save_filters(
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.dashboard_save_filters(
                payload, name=str(payload.get("name") or "default")
            )
        except Exception as exc:  # noqa: BLE001
            raise _dashboard_http_error(exc) from exc

    # -- PI-21: advanced autonomous portfolio management ---------------------
    def _portfolio_management_http_error(exc: Exception) -> HTTPException:
        from novacontrol.novamarket.portfolio_management import PortfolioManagementError

        if isinstance(exc, HTTPException):
            return exc
        if isinstance(exc, PortfolioManagementError):
            return HTTPException(
                status_code=exc.http_status,
                detail={"error": exc.code, "message": exc.message},
            )
        if isinstance(exc, PermissionError):
            return HTTPException(status_code=403, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    @app.get("/market/portfolio-management/status")
    async def market_portfolio_management_status(
        operator_ok: bool = Depends(autonomy_operator_state),
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.portfolio_management_status(operator_authorized=operator_ok)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.get("/market/portfolio-management/contracts")
    async def market_portfolio_management_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.portfolio_management_contract()
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.get("/market/portfolio-management/navigation")
    async def market_portfolio_management_navigation(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.portfolio_management_navigation()
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.get("/market/portfolio-management/config")
    async def market_portfolio_management_config(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.portfolio_management_config()
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.put("/market/portfolio-management/config")
    async def market_portfolio_management_update_config(
        payload: dict[str, Any],
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.portfolio_management_update_config(payload, actor=_operator)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.post("/market/portfolio-management/review")
    async def market_portfolio_management_review(
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        body = payload or {}
        try:
            return await nova.novamarket.portfolio_management_run(
                portfolio_id=str(body.get("portfolio_id") or ""),
                # No horizon: the service follows the portfolio's declared
                # NM-07 policy horizon instead of a hardcoded 5d window.
                horizon=str(body.get("horizon") or ""),
                trigger=str(body.get("trigger") or "manual"),
                request_id=str(body.get("request_id") or ""),
                actor=_operator,
            )
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.get("/market/portfolio-management/cycles")
    async def market_portfolio_management_cycles(
        portfolio_id: str = "",
        status: str = "",
        limit: int = 50,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.portfolio_management_cycles(
                portfolio_id=portfolio_id,
                status=status,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.get("/market/portfolio-management/cycles/{cycle_id}")
    async def market_portfolio_management_cycle(
        cycle_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.portfolio_management_cycle(cycle_id)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.get("/market/portfolio-management/cycles/{cycle_id}/events")
    async def market_portfolio_management_cycle_events(
        cycle_id: str,
        limit: int = 200,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.portfolio_management_events(cycle_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.post("/market/portfolio-management/cycles/{cycle_id}/handoff")
    async def market_portfolio_management_handoff(
        cycle_id: str,
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        body = payload or {}
        try:
            return await nova.novamarket.portfolio_management_handoff(
                cycle_id,
                actor=_operator,
                max_changes=int(body.get("max_changes") or 10),
            )
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.post("/market/portfolio-management/cycles/{cycle_id}/reconcile")
    async def market_portfolio_management_reconcile(
        cycle_id: str,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.portfolio_management_reconcile(cycle_id, actor=_operator)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.post("/market/portfolio-management/recover")
    async def market_portfolio_management_recover(
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        body = payload or {}
        try:
            return await nova.novamarket.portfolio_management_recover(
                reason=str(body.get("reason") or "PI-21 operator recovery")
            )
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.get("/market/portfolio-management/portfolios/{portfolio_id}/snapshot")
    async def market_portfolio_management_snapshot(
        portfolio_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.portfolio_management_snapshot(portfolio_id)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.get("/market/portfolio-management/portfolios/{portfolio_id}/positions")
    async def market_portfolio_management_positions(
        portfolio_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.portfolio_management_positions(portfolio_id)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.get("/market/portfolio-management/portfolios/{portfolio_id}/opportunities")
    async def market_portfolio_management_opportunities(
        portfolio_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.portfolio_management_opportunities(portfolio_id)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.get("/market/portfolio-management/portfolios/{portfolio_id}/decisions")
    async def market_portfolio_management_decisions(
        portfolio_id: str,
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.portfolio_management_decisions(portfolio_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.get("/market/portfolio-management/portfolios/{portfolio_id}/capital")
    async def market_portfolio_management_capital(
        portfolio_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.portfolio_management_capital(portfolio_id)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.get("/market/portfolio-management/portfolios/{portfolio_id}/cash")
    async def market_portfolio_management_cash(
        portfolio_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.portfolio_management_cash(portfolio_id)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.get("/market/portfolio-management/portfolios/{portfolio_id}/exposure")
    async def market_portfolio_management_exposure(
        portfolio_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.portfolio_management_exposure(portfolio_id)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.get("/market/portfolio-management/portfolios/{portfolio_id}/rebalancing")
    async def market_portfolio_management_rebalancing(
        portfolio_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.portfolio_management_rebalance(portfolio_id)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    @app.get("/market/portfolio-management/portfolios/{portfolio_id}/analytics")
    async def market_portfolio_management_analytics(
        portfolio_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.portfolio_management_analytics(portfolio_id)
        except Exception as exc:  # noqa: BLE001
            raise _portfolio_management_http_error(exc) from exc

    # -- PI-22: advanced risk intelligence ----------------------------------
    def _risk_intelligence_http_error(exc: Exception) -> HTTPException:
        from novacontrol.novamarket.risk_intelligence import RiskIntelligenceError

        if isinstance(exc, HTTPException):
            return exc
        if isinstance(exc, RiskIntelligenceError):
            return HTTPException(
                status_code=exc.http_status,
                detail={"error": exc.code, "message": exc.message},
            )
        if isinstance(exc, PermissionError):
            return HTTPException(status_code=403, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    @app.get("/market/risk-intelligence/status")
    async def market_risk_intelligence_status(
        operator_ok: bool = Depends(autonomy_operator_state),
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_status(operator_authorized=operator_ok)
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.get("/market/risk-intelligence/contracts")
    async def market_risk_intelligence_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_contract()
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.get("/market/risk-intelligence/navigation")
    async def market_risk_intelligence_navigation(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_navigation()
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.get("/market/risk-intelligence/config")
    async def market_risk_intelligence_config(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_config()
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.put("/market/risk-intelligence/config")
    async def market_risk_intelligence_update_config(
        payload: dict[str, Any],
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_update_config(payload, actor=_operator)
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.post("/market/risk-intelligence/analyze")
    async def market_risk_intelligence_analyze(
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        body = payload or {}
        try:
            return await nova.novamarket.risk_intelligence_analyze(
                portfolio_id=str(body.get("portfolio_id") or ""),
                horizon=str(body.get("horizon") or ""),
                trigger=str(body.get("trigger") or "manual"),
                request_id=str(body.get("request_id") or ""),
                actor=_operator,
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.get("/market/risk-intelligence/portfolios/{portfolio_id}/overview")
    async def market_risk_intelligence_overview(
        portfolio_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_overview(portfolio_id)
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.get("/market/risk-intelligence/portfolios/{portfolio_id}/positions")
    async def market_risk_intelligence_positions(
        portfolio_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_section(portfolio_id, "positions")
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.get("/market/risk-intelligence/portfolios/{portfolio_id}/concentration")
    async def market_risk_intelligence_concentration(
        portfolio_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_section(portfolio_id, "concentration")
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.get("/market/risk-intelligence/portfolios/{portfolio_id}/correlation")
    async def market_risk_intelligence_correlation(
        portfolio_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_section(portfolio_id, "correlation")
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.get("/market/risk-intelligence/portfolios/{portfolio_id}/volatility")
    async def market_risk_intelligence_volatility(
        portfolio_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_section(portfolio_id, "volatility")
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.get("/market/risk-intelligence/portfolios/{portfolio_id}/drawdown")
    async def market_risk_intelligence_drawdown(
        portfolio_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_section(portfolio_id, "drawdown")
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.get("/market/risk-intelligence/portfolios/{portfolio_id}/liquidity")
    async def market_risk_intelligence_liquidity(
        portfolio_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_section(portfolio_id, "liquidity")
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.get("/market/risk-intelligence/portfolios/{portfolio_id}/regime")
    async def market_risk_intelligence_regime(
        portfolio_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_section(portfolio_id, "regime")
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.get("/market/risk-intelligence/portfolios/{portfolio_id}/history")
    async def market_risk_intelligence_history(
        portfolio_id: str,
        limit: int = 50,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_history(
                portfolio_id, limit=max(1, min(limit, 200)), offset=max(0, offset)
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.get("/market/risk-intelligence/portfolios/{portfolio_id}/trend")
    async def market_risk_intelligence_trend(
        portfolio_id: str,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_trend(portfolio_id)
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.get("/market/risk-intelligence/portfolios/{portfolio_id}/recommendations")
    async def market_risk_intelligence_recommendations(
        portfolio_id: str,
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_recommendations(
                portfolio_id=portfolio_id, limit=max(1, min(limit, 200))
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.get("/market/risk-intelligence/alerts")
    async def market_risk_intelligence_alerts(
        portfolio_id: str = "",
        status: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_alerts(
                portfolio_id=portfolio_id, status=status, limit=max(1, min(limit, 200))
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.post("/market/risk-intelligence/alerts/{alert_id}/action")
    async def market_risk_intelligence_alert_action(
        alert_id: str,
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        body = payload or {}
        try:
            return await nova.novamarket.risk_intelligence_update_alert(
                alert_id, action=str(body.get("action") or ""), actor=_operator
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.get("/market/risk-intelligence/scenarios")
    async def market_risk_intelligence_scenarios(
        portfolio_id: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_scenarios(
                portfolio_id, limit=max(1, min(limit, 200))
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.post("/market/risk-intelligence/scenarios")
    async def market_risk_intelligence_create_scenario(
        payload: dict[str, Any],
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_create_scenario(payload, actor=_operator)
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.post("/market/risk-intelligence/portfolios/{portfolio_id}/scenarios/run")
    async def market_risk_intelligence_run_scenario(
        portfolio_id: str,
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        body = payload or {}
        try:
            return await nova.novamarket.risk_intelligence_run_scenario(
                portfolio_id,
                scenario_id=str(body.get("scenario_id") or ""),
                config_payload=body,
                actor=_operator,
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.get("/market/risk-intelligence/portfolios/{portfolio_id}/scenario-runs")
    async def market_risk_intelligence_scenario_runs(
        portfolio_id: str,
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_scenario_runs(
                portfolio_id, limit=max(1, min(limit, 200))
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.get("/market/risk-intelligence/portfolios/{portfolio_id}/stress-tests")
    async def market_risk_intelligence_stress_results(
        portfolio_id: str,
        limit: int = 50,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.risk_intelligence_stress_results(
                portfolio_id, limit=max(1, min(limit, 200))
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.post("/market/risk-intelligence/portfolios/{portfolio_id}/stress-tests")
    async def market_risk_intelligence_run_stress(
        portfolio_id: str,
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        body = payload or {}
        try:
            shock = body.get("shock_pct")
            try:
                shock_pct = None if shock is None else float(shock)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="shock_pct must be numeric") from exc
            return await nova.novamarket.risk_intelligence_run_stress(
                portfolio_id,
                mode=str(body.get("mode") or "hypothetical"),
                scenario_id=str(body.get("scenario_id") or ""),
                shock_pct=shock_pct,
                actor=_operator,
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    @app.post("/market/risk-intelligence/recover")
    async def market_risk_intelligence_recover(
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        body = payload or {}
        try:
            return await nova.novamarket.risk_intelligence_recover(
                reason=str(body.get("reason") or "PI-22 operator recovery")
            )
        except Exception as exc:  # noqa: BLE001
            raise _risk_intelligence_http_error(exc) from exc

    # -- PI-23: forecast optimization ----------------------------------------
    def _forecast_intelligence_http_error(exc: Exception) -> HTTPException:
        from novacontrol.novamarket.contracts import NotOperationalError
        from novacontrol.novamarket.forecast_intelligence import ForecastIntelligenceError

        if isinstance(exc, HTTPException):
            return exc
        if isinstance(exc, ForecastIntelligenceError):
            return HTTPException(
                status_code=exc.http_status,
                detail={"error": exc.code, "message": exc.message},
            )
        if isinstance(exc, PermissionError):
            return HTTPException(status_code=403, detail=str(exc))
        if isinstance(exc, NotOperationalError):
            return HTTPException(status_code=503, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    @app.get("/market/forecast-intelligence/status")
    async def market_forecast_intelligence_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_status()
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.get("/market/forecast-intelligence/contracts")
    async def market_forecast_intelligence_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_contract()
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.get("/market/forecast-intelligence/navigation")
    async def market_forecast_intelligence_navigation(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_navigation()
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.get("/market/forecast-intelligence/config")
    async def market_forecast_intelligence_config(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_config()
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.put("/market/forecast-intelligence/config")
    async def market_forecast_intelligence_update_config(
        payload: dict[str, Any],
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_update_config(
                payload, actor=_operator
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.get("/market/forecast-intelligence/registry")
    async def market_forecast_intelligence_registry(
        instrument_id: str = "",
        horizon: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_registry(
                instrument_id=instrument_id, horizon=horizon
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.get("/market/forecast-intelligence/history")
    async def market_forecast_intelligence_history(
        instrument_id: str = "",
        horizon: str = "",
        kind: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_history(
                instrument_id=instrument_id, horizon=horizon, kind=kind, limit=limit
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.get("/market/forecast-intelligence/overview")
    async def market_forecast_intelligence_overview(
        instrument_id: str = "",
        horizon: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_overview(
                instrument_id=instrument_id, horizon=horizon, limit=limit
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.get("/market/forecast-intelligence/comparison/{instrument_id}")
    async def market_forecast_intelligence_comparison(
        instrument_id: str,
        horizon: str = "5d",
        task: str = "return_regression",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_comparison(
                instrument_id, horizon, task
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.get("/market/forecast-intelligence/horizons/{instrument_id}")
    async def market_forecast_intelligence_horizons(
        instrument_id: str,
        task: str = "return_regression",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_horizons(instrument_id, task)
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.get("/market/forecast-intelligence/regimes/{instrument_id}")
    async def market_forecast_intelligence_regimes(
        instrument_id: str,
        horizon: str = "5d",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_regimes(instrument_id, horizon)
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.post("/market/forecast-intelligence/instruments")
    async def market_forecast_intelligence_instruments(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        body = payload or {}
        raw = body.get("instrument_ids") or ()
        ids = [str(item) for item in raw] if isinstance(raw, (list, tuple)) else []
        try:
            return nova.novamarket.forecast_intelligence_instruments(
                ids,
                str(body.get("horizon") or "5d"),
                str(body.get("task") or "return_regression"),
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.post("/market/forecast-intelligence/evaluate")
    async def market_forecast_intelligence_evaluate(
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_evaluate(
                payload or {}, actor=_operator
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.post("/market/forecast-intelligence/select")
    async def market_forecast_intelligence_select(
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_select(
                payload or {}, actor=_operator
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.post("/market/forecast-intelligence/calibration")
    async def market_forecast_intelligence_calibration(
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_calibrate(
                payload or {}, actor=_operator
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.post("/market/forecast-intelligence/uncertainty")
    async def market_forecast_intelligence_uncertainty(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_uncertainty(payload or {})
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.post("/market/forecast-intelligence/disagreement")
    async def market_forecast_intelligence_disagreement(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_disagreement(payload or {})
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.post("/market/forecast-intelligence/consistency")
    async def market_forecast_intelligence_consistency(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        body = payload or {}
        try:
            return nova.novamarket.forecast_intelligence_consistency(
                str(body.get("instrument_id") or "")
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.post("/market/forecast-intelligence/drift")
    async def market_forecast_intelligence_drift(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_drift(payload or {})
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.post("/market/forecast-intelligence/degradation")
    async def market_forecast_intelligence_degradation(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_degradation(payload or {})
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.post("/market/forecast-intelligence/weights")
    async def market_forecast_intelligence_weights(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_weights(payload or {})
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.post("/market/forecast-intelligence/reliability")
    async def market_forecast_intelligence_reliability(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_reliability(payload or {})
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.post("/market/forecast-intelligence/fallback")
    async def market_forecast_intelligence_fallback(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_fallback(payload or {})
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.post("/market/forecast-intelligence/analyze")
    async def market_forecast_intelligence_analyze(
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_analyze(
                payload or {}, actor=_operator
            )
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    @app.get("/market/forecast-intelligence/health")
    async def market_forecast_intelligence_health(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.forecast_intelligence_health()
        except Exception as exc:  # noqa: BLE001
            raise _forecast_intelligence_http_error(exc) from exc

    # -- PI-24: event-aware predictive intelligence --------------------------
    def _event_intelligence_http_error(exc: Exception) -> HTTPException:
        from novacontrol.novamarket.contracts import NotOperationalError
        from novacontrol.novamarket.event_intelligence import EventIntelligenceError

        if isinstance(exc, EventIntelligenceError):
            return HTTPException(
                status_code=exc.http_status,
                detail={"error": exc.code, "message": exc.message},
            )
        if isinstance(exc, PermissionError):
            return HTTPException(status_code=403, detail=str(exc))
        if isinstance(exc, NotOperationalError):
            return HTTPException(status_code=503, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    @app.get("/market/event-intelligence/status")
    async def market_event_intelligence_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_status()
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.get("/market/event-intelligence/contracts")
    async def market_event_intelligence_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_contract()
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.get("/market/event-intelligence/navigation")
    async def market_event_intelligence_navigation(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_navigation()
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.get("/market/event-intelligence/config")
    async def market_event_intelligence_config(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_config()
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.put("/market/event-intelligence/config")
    async def market_event_intelligence_update_config(
        payload: dict[str, Any],
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_update_config(
                payload, actor=_operator
            )
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.get("/market/event-intelligence/events")
    async def market_event_intelligence_events(
        instrument_id: str = "",
        status: str = "",
        group: str = "",
        limit: int = 50,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_events(
                instrument_id=instrument_id,
                status=status,
                group=group,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.get("/market/event-intelligence/events/{event_id}")
    async def market_event_intelligence_event(
        event_id: str,
        version: int | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_event(
                event_id, version=version
            )
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.get("/market/event-intelligence/timeline")
    async def market_event_intelligence_timeline(
        event_id: str = "",
        instrument_id: str = "",
        horizon: str = "5d",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_timeline(
                {
                    "event_id": event_id,
                    "instrument_id": instrument_id,
                    "horizon": horizon,
                }
            )
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.post("/market/event-intelligence/relevance")
    async def market_event_intelligence_relevance(
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_relevance(
                payload or {}, actor=_operator
            )
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.post("/market/event-intelligence/impact")
    async def market_event_intelligence_impact(
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_impact(payload or {})
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.post("/market/event-intelligence/warnings")
    async def market_event_intelligence_warnings(
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_warnings(payload or {})
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.post("/market/event-intelligence/features")
    async def market_event_intelligence_features(
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_features(payload or {})
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.post("/market/event-intelligence/forecast-context")
    async def market_event_intelligence_forecast_context(
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_forecasts(payload or {})
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.post("/market/event-intelligence/outcomes")
    async def market_event_intelligence_outcomes(
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_outcomes(payload or {})
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.post("/market/event-intelligence/outcomes/study")
    async def market_event_intelligence_outcome_study(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_outcome_study(payload or {})
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.post("/market/event-intelligence/forecast-evaluation")
    async def market_event_intelligence_forecast_evaluation(
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        """Event-aware versus non-event-aware forecast error (persisted evidence).

        The cohorts split on declared event awareness; records that only sit near
        a stored event are reported separately and excluded from the comparison.
        """

        try:
            return nova.novamarket.event_intelligence_forecast_evaluation(payload or {})
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.post("/market/event-intelligence/agents")
    async def market_event_intelligence_agents(
        payload: dict[str, Any] | None = None,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        body = payload or {}
        try:
            return nova.novamarket.event_intelligence_agent_context(
                str(body.get("instrument_id") or ""),
                str(body.get("horizon") or "5d"),
                portfolio_id=str(body.get("portfolio_id") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.get("/market/event-intelligence/portfolio/{portfolio_id}/context")
    async def market_event_intelligence_portfolio_context(
        portfolio_id: str,
        horizon: str = "5d",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_portfolio_context(
                portfolio_id, horizon=horizon
            )
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.post("/market/event-intelligence/sync")
    async def market_event_intelligence_sync(
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        body = payload or {}
        try:
            return nova.novamarket.event_intelligence_sync(
                instrument_id=str(body.get("instrument_id") or ""),
                limit=int(body.get("limit") or 200),
            )
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.post("/market/event-intelligence/ingest")
    async def market_event_intelligence_ingest(
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.event_intelligence_ingest(
                str((payload or {}).get("source_id") or "")
            )
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.post("/market/event-intelligence/analyze")
    async def market_event_intelligence_analyze(
        payload: dict[str, Any] | None = None,
        _operator: str = Depends(require_autonomy_operator),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_analyze(
                payload or {}, actor=_operator
            )
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.get("/market/event-intelligence/overview")
    async def market_event_intelligence_overview(
        instrument_id: str = "",
        horizon: str = "5d",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_overview(
                instrument_id=instrument_id, horizon=horizon
            )
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.get("/market/event-intelligence/history")
    async def market_event_intelligence_history(
        kind: str = "",
        event_id: str = "",
        instrument_id: str = "",
        horizon: str = "",
        limit: int = 100,
        offset: int = 0,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_history(
                kind=kind,
                event_id=event_id,
                instrument_id=instrument_id,
                horizon=horizon,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    @app.get("/market/event-intelligence/health")
    async def market_event_intelligence_health(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.event_intelligence_health()
        except Exception as exc:  # noqa: BLE001
            raise _event_intelligence_http_error(exc) from exc

    # -- PI-11: forecast evaluation ------------------------------------------
    def _evaluation_http_error(exc: Exception) -> Any:
        from fastapi import HTTPException
        from novacontrol.novamarket.evaluation import (
            EvaluationError,
            EvaluationNotFoundError,
        )

        if isinstance(exc, HTTPException):
            return exc
        if isinstance(exc, EvaluationNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, EvaluationError):
            return HTTPException(status_code=422, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    @app.get("/market/evaluations/status")
    async def market_evaluation_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.evaluation_status()

    @app.get("/market/evaluations/contracts")
    async def market_evaluation_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.evaluation_contract()

    @app.get("/market/evaluations")
    async def market_evaluations(
        limit: int = 50, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.evaluation_list(limit)
        except Exception as exc:  # noqa: BLE001
            raise _evaluation_http_error(exc) from exc

    @app.post("/market/evaluations")
    async def market_evaluation_create(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        """Evaluate stored forecasts; never generates or trades a forecast."""
        try:
            return await nova.novamarket.evaluation_create(payload)
        except Exception as exc:  # noqa: BLE001
            raise _evaluation_http_error(exc) from exc

    @app.get("/market/evaluations/{evaluation_id}")
    async def market_evaluation(
        evaluation_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.evaluation(evaluation_id)
        except Exception as exc:  # noqa: BLE001
            raise _evaluation_http_error(exc) from exc

    @app.get("/market/evaluations/{evaluation_id}/verify")
    async def market_evaluation_verify(
        evaluation_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.evaluation_verify(evaluation_id)
        except Exception as exc:  # noqa: BLE001
            raise _evaluation_http_error(exc) from exc

    @app.post("/market/evaluations/{evaluation_id}/reproduce")
    async def market_evaluation_reproduce(
        evaluation_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.evaluation_reproduce(evaluation_id)
        except Exception as exc:  # noqa: BLE001
            raise _evaluation_http_error(exc) from exc

    # -- NM-12: non-executing shadow mode ------------------------------------
    def _shadow_http_error(exc: Exception) -> Any:
        from fastapi import HTTPException
        from novacontrol.novamarket.contracts import NotOperationalError
        from novacontrol.novamarket.shadow import (
            ShadowNotFoundError,
            ShadowStateError,
            ShadowValidationError,
        )

        if isinstance(exc, HTTPException):
            return exc
        if isinstance(exc, NotOperationalError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, ShadowNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, ShadowStateError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, ShadowValidationError):
            return HTTPException(status_code=422, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    @app.get("/market/shadow/status")
    async def market_shadow_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.shadow_status()

    @app.get("/market/shadow/contracts")
    async def market_shadow_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.shadow_contract()

    @app.post("/market/shadow/sessions")
    async def market_shadow_create(
        payload: dict[str, Any], _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.shadow_create(payload)
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    @app.get("/market/shadow/sessions")
    async def market_shadow_sessions(
        limit: int = 50, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.shadow_sessions(limit)
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    @app.get("/market/shadow/sessions/{session_id}")
    async def market_shadow_session(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.shadow_session(session_id)
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    @app.get("/market/shadow/sessions/{session_id}/history")
    async def market_shadow_history(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.shadow_history(session_id)
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    @app.post("/market/shadow/sessions/{session_id}/start")
    async def market_shadow_start(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.shadow_start(session_id)
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    @app.post("/market/shadow/sessions/{session_id}/pause")
    async def market_shadow_pause(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.shadow_pause(session_id)
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    @app.post("/market/shadow/sessions/{session_id}/resume")
    async def market_shadow_resume(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.shadow_resume(session_id)
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    @app.post("/market/shadow/sessions/{session_id}/stop")
    async def market_shadow_stop(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.shadow_stop(session_id)
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    @app.post("/market/shadow/sessions/{session_id}/complete")
    async def market_shadow_complete(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.shadow_complete(session_id)
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    @app.post("/market/shadow/sessions/{session_id}/cancel")
    async def market_shadow_cancel(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.shadow_cancel(session_id)
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    @app.post("/market/shadow/sessions/{session_id}/cycle")
    async def market_shadow_cycle(
        session_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.shadow_cycle(session_id, payload)
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    @app.post("/market/shadow/sessions/{session_id}/track-outcomes")
    async def market_shadow_track_outcomes(
        session_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.shadow_track_outcomes(session_id, payload)
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    @app.get("/market/shadow/sessions/{session_id}/decisions")
    async def market_shadow_decisions(
        session_id: str,
        limit: int = 200,
        instrument_id: str = "",
        strategy_id: str = "",
        signal_type: str = "",
        status: str = "",
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.shadow_decisions(
                session_id,
                limit=limit,
                instrument_id=instrument_id,
                strategy_id=strategy_id,
                signal_type=signal_type,
                status=status,
            )
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    @app.get("/market/shadow/sessions/{session_id}/projections")
    async def market_shadow_projections(
        session_id: str, limit: int = 200, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.shadow_projections(session_id, limit)
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    @app.get("/market/shadow/sessions/{session_id}/outcomes")
    async def market_shadow_outcomes(
        session_id: str, limit: int = 500, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.shadow_outcomes(session_id, limit)
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    @app.get("/market/shadow/sessions/{session_id}/analytics")
    async def market_shadow_analytics(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.shadow_analytics(session_id)
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    @app.get("/market/shadow/sessions/{session_id}/errors")
    async def market_shadow_errors(
        session_id: str, limit: int = 100, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.shadow_errors(session_id, limit)
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    @app.get("/market/shadow/sessions/{session_id}/health")
    async def market_shadow_health(
        session_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.shadow_health(session_id)
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    @app.post("/market/shadow/sessions/{session_id}/compare")
    async def market_shadow_compare(
        session_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.shadow_compare(session_id, payload)
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    @app.get("/market/shadow/sessions/{session_id}/comparisons")
    async def market_shadow_comparisons(
        session_id: str, limit: int = 50, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.shadow_comparisons(session_id, limit)
        except Exception as exc:  # noqa: BLE001
            raise _shadow_http_error(exc) from exc

    # -- NM-13: controlled adaptive learning --------------------------------
    def _learning_http_error(exc: Exception) -> Any:
        from fastapi import HTTPException
        from novacontrol.novamarket.contracts import NotOperationalError
        from novacontrol.novamarket.learning import (
            LearningNotFoundError,
            LearningStateError,
            LearningValidationError,
        )

        if isinstance(exc, HTTPException):
            return exc
        if isinstance(exc, NotOperationalError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, LearningNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, LearningStateError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, LearningValidationError):
            return HTTPException(status_code=422, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    @app.get("/market/learning/status")
    async def market_learning_status(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.learning_status()

    @app.get("/market/learning/contracts")
    async def market_learning_contracts(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return nova.novamarket.learning_contract()

    @app.get("/market/learning/dashboard")
    async def market_learning_dashboard(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.learning_dashboard()
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.get("/market/learning/health")
    async def market_learning_health(
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.learning_health()
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.post("/market/learning/evidence")
    async def market_learning_record_evidence(
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.learning_record_evidence(payload)
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.get("/market/learning/evidence")
    async def market_learning_evidence(
        strategy_id: str = "",
        kind: str = "",
        status: str = "",
        limit: int = 200,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.learning_evidence(
                strategy_id=strategy_id, kind=kind, status=status, limit=limit
            )
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.get("/market/learning/evidence/{evidence_id}")
    async def market_learning_evidence_record(
        evidence_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.learning_evidence_record(evidence_id)
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.post("/market/learning/monitor")
    async def market_learning_monitor(
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.learning_monitor(
                str(payload.get("strategy_id") or ""),
                kind=str(payload.get("kind") or ""),
                window_days=int(payload.get("window_days") or 30),
            )
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.get("/market/learning/alerts")
    async def market_learning_alerts(
        strategy_id: str = "",
        status: str = "",
        limit: int = 100,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.learning_alerts(
                strategy_id=strategy_id, status=status, limit=limit
            )
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.post("/market/learning/alerts/{alert_id}/acknowledge")
    async def market_learning_acknowledge_alert(
        alert_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.learning_acknowledge_alert(
                alert_id, str(payload.get("actor") or "")
            )
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.post("/market/learning/experiments")
    async def market_learning_create_experiment(
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.learning_create_experiment(payload)
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.get("/market/learning/experiments")
    async def market_learning_experiments(
        limit: int = 50, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.learning_experiments(limit)
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.get("/market/learning/experiments/{experiment_id}")
    async def market_learning_experiment(
        experiment_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.learning_experiment(experiment_id)
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.post("/market/learning/experiments/{experiment_id}/start")
    async def market_learning_start_experiment(
        experiment_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.learning_start_experiment(experiment_id)
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.post("/market/learning/experiments/{experiment_id}/run")
    async def market_learning_run_experiment(
        experiment_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.learning_run_experiment(experiment_id)
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.post("/market/learning/experiments/{experiment_id}/cancel")
    async def market_learning_cancel_experiment(
        experiment_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.learning_cancel_experiment(
                experiment_id, str(payload.get("reason") or "")
            )
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.get("/market/learning/experiments/{experiment_id}/candidates")
    async def market_learning_candidates(
        experiment_id: str,
        strategy_id: str = "",
        limit: int = 200,
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.learning_candidates(
                experiment_id=experiment_id, strategy_id=strategy_id, limit=limit
            )
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.get("/market/learning/candidates/{candidate_id}")
    async def market_learning_candidate(
        candidate_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.learning_candidate(candidate_id)
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.post("/market/learning/candidates/{candidate_id}/evaluate")
    async def market_learning_evaluate_candidate(
        candidate_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.learning_evaluate_candidate(candidate_id)
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.post("/market/learning/candidates/{candidate_id}/approve")
    async def market_learning_approve_candidate(
        candidate_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.learning_approve_candidate(candidate_id, payload)
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.post("/market/learning/candidates/{candidate_id}/reject")
    async def market_learning_reject_candidate(
        candidate_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.learning_reject_candidate(
                candidate_id,
                str(payload.get("reason") or ""),
                str(payload.get("actor") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.post("/market/learning/candidates/{candidate_id}/shadow-validate")
    async def market_learning_shadow_validate(
        candidate_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return await nova.novamarket.learning_shadow_validate(candidate_id, payload)
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.post("/market/learning/candidates/{candidate_id}/promote")
    async def market_learning_promote_candidate(
        candidate_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.learning_promote_candidate(candidate_id, payload)
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.get("/market/learning/strategies/{strategy_id}/versions")
    async def market_learning_versions(
        strategy_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.learning_versions(strategy_id)
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.get("/market/learning/strategies/{strategy_id}/active-version")
    async def market_learning_active_version(
        strategy_id: str, _principal: str = Depends(require_auth)
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.learning_active_version(strategy_id)
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

    @app.post("/market/learning/strategies/{strategy_id}/rollback")
    async def market_learning_rollback(
        strategy_id: str,
        payload: dict[str, Any],
        _principal: str = Depends(require_auth),
    ) -> dict[str, Any]:
        try:
            return nova.novamarket.learning_rollback(strategy_id, payload)
        except Exception as exc:  # noqa: BLE001
            raise _learning_http_error(exc) from exc

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
