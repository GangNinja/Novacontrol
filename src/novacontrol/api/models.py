"""API metadata models and request bodies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

# ── Request bodies ────────────────────────────────────────────────
#
# Pydantic models give the API contract real teeth: a missing required key is
# a 422 with a field-level error (not a KeyError masked as a 500), and empty
# or whitespace-only input is rejected at the edge with a 400 before it can
# reach — and crash — the research pipeline.


class AskRequest(BaseModel):
    """POST /ask — one natural-language request, optionally with a picture.

    ``image`` is a path to a picture that travels WITH the request (a file the
    client attached), which is a different thing from asking for the screen: it
    makes the request need eyes — ``requires_vision`` — and the vision pipeline
    reads THAT image rather than capturing the desktop.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"request": "open chrome"},
                {"request": "what is this error?", "image": "captures/trace.png"},
            ]
        }
    )

    request: str
    image: str = ""

    @field_validator("request")
    @classmethod
    def _request_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("request must not be empty or whitespace")
        return value

    @field_validator("image")
    @classmethod
    def _image_is_a_path_or_nothing(cls, value: str) -> str:
        return value.strip()


class BrainDecideRequest(BaseModel):
    """POST /brain/decide — trace an utterance through the routing gates."""

    text: str

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be empty or whitespace")
        return value


class ExploreRequest_(BaseModel):
    """POST /explore — research one topic.

    Named with a trailing underscore to avoid shadowing the application-layer
    ExploreRequest dataclass imported by this module's consumers.
    """

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"topic": "how do black holes form"}]}
    )

    topic: str
    depth: str = "deep"
    include_videos: bool = True
    max_sources: int = 6
    max_videos: int = 5
    last_topic: str = ""
    prior_topics: list[str] = []
    # Run-scoped correlation id minted by the client: echoed on every
    # explore.progress event so concurrent researches interleave in the UI
    # without mixing rows. Optional; the server mints one when absent.
    correlation_id: str = ""

    @field_validator("topic")
    @classmethod
    def _topic_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("topic must not be empty or whitespace")
        return value


class CommandPlanRequest(BaseModel):
    """POST /command/plan — plan one natural device command."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"command": "open calculator"}]})

    command: str

    @field_validator("command")
    @classmethod
    def _command_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command must not be empty or whitespace")
        return value


@dataclass(frozen=True, slots=True)
class ApiRoute:
    method: str
    path: str
    description: str
    authenticated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "path": self.path,
            "description": self.description,
            "authenticated": self.authenticated,
        }


@dataclass(frozen=True, slots=True)
class ApiSurface:
    routes: tuple[ApiRoute, ...]
    websocket_paths: tuple[str, ...]

    @classmethod
    def default(cls) -> ApiSurface:
        return cls(
            routes=(
                ApiRoute("GET", "/", "Local web platform."),
                ApiRoute("GET", "/health", "Health check."),
                ApiRoute("GET", "/status", "Runtime status.", authenticated=True),
                ApiRoute("GET", "/system/health", "Aggregated system health.", authenticated=True),
                ApiRoute(
                    "GET",
                    "/system/telemetry",
                    "Live machine telemetry for the Command Center (CPU, memory, storage, GPU, network, battery, temperature).",
                    authenticated=True,
                ),
                ApiRoute("GET", "/system/harden", "Release hardening report.", authenticated=True),
                ApiRoute("GET", "/system/package", "Runtime package manifest.", authenticated=True),
                ApiRoute(
                    "POST",
                    "/ask",
                    "Route a natural-language request through NovaControl; an optional `image` path "
                    "attaches a picture for the vision pipeline to read.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/brain/mode",
                    "Switch the chat brain between auto, llm, scratch, and cloud.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/brain/mode",
                    "Inspect the active brain mode and provider.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/brain/cloud/presets",
                    "List cloud LLM provider presets (no secrets).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/brain/cloud",
                    "Install a cloud LLM (ChatGPT/Gemini/Groq) with a local API key.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/brain/cloud/test",
                    "Ping a cloud provider with the pasted key (one tiny completion; nothing is saved).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/brain/cloud/clear",
                    "Remove the cloud LLM config and its API key.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/brain/ollama/models",
                    "List local Ollama models for the brain model picker (fresh probe).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/brain/local/model",
                    "Pin the local brain to a specific Ollama model (empty clears the pick).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/chat/clear",
                    "Clear the server-side chat conversation memory and the shared transcript.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/chat/history",
                    "Read the shared server-persisted chat thread (same for every browser).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/chat/history",
                    "Append a chat turn to the shared thread (action=migrate imports a browser's old history once).",
                    authenticated=True,
                ),
                ApiRoute("GET", "/tasks", "List tracked task records.", authenticated=True),
                ApiRoute(
                    "POST",
                    "/tasks/delete",
                    "Delete one tracked task record by id.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/tasks/clear",
                    "Delete all tracked task records (undoable for a short window).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/tasks/clear/undo",
                    "Restore the tasks wiped by a recent /tasks/clear (one-shot token).",
                    authenticated=True,
                ),
                ApiRoute("POST", "/improve", "Create a self-improvement plan.", authenticated=True),
                ApiRoute(
                    "POST",
                    "/improve/workflow",
                    "Create a friendly self-improvement workflow.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/improve/preview",
                    "Run a temporary self-improvement preview.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/improve/approve",
                    "Approve a preview for code changes.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST", "/learn", "Run a local feedback learning cycle.", authenticated=True
                ),
                ApiRoute(
                    "POST",
                    "/knowledge/teach",
                    "Teach: persist a typed fact as durable recallable knowledge.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/knowledge",
                    "List taught knowledge, optionally filtered by a query.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/knowledge/recall",
                    "Recall taught knowledge matching a query.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/train",
                    "Run bounded autonomous local learning iterations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/brain/decide",
                    "Trace an utterance through the routing gates with a rung preview.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST", "/plan", "Create and optionally execute a plan.", authenticated=True
                ),
                ApiRoute(
                    "POST",
                    "/plan/run",
                    "Run a goal through the agent loop; steps needing approval must be named.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/plan/code",
                    "Plan a coding task: language-aware steps plus a drafted code artifact.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/build/save",
                    "Save a drafted Build artifact to the build_workspace folder on disk.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/build/project",
                    "Draft a small multi-file project with the coding agent: the configured model, or the sandbox-verified scaffold when none is installed.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/build/artifacts",
                    "Saved Build artifacts, newest first, for the Run list.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/build/run",
                    "Re-execute a saved Build artifact in the sandbox.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/models/status",
                    "What the shared model runtime holds right now, and the memory policy it applies to it.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/models/load",
                    "Load a model, unloading whatever must go first to make room for it.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/models/unload",
                    "Release one resident model, or every resident model when none is named.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/command/plan",
                    "Plan a natural desktop or phone command.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/command/execute",
                    "Execute an approved natural desktop or phone command.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/desktop/plan",
                    "Plan an approval-gated desktop action.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/desktop/execute",
                    "Execute an approved desktop action.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/browser/plan",
                    "Plan an approval-gated browser action (navigate, search + extract).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/browser/execute",
                    "Execute an approved browser action (search returns the top results).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET", "/phone/status", "Inspect phone bridge status.", authenticated=True
                ),
                ApiRoute(
                    "POST",
                    "/phone/connect",
                    "Run the phone bridge pairing flow.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/vision/status",
                    "Vision capability report: model availability and open bug count.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/vision/model",
                    "Install a multimodal vision model (ollama or a cloud provider) for element location.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/vision/model/clear",
                    "Remove the configured vision model; element location returns to OCR-only.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/vision/describe",
                    "Capture the screen and describe it (vision model or window probe).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/vision/click",
                    "Vision-locate a labeled element on screen, click it, and verify.",
                    authenticated=True,
                ),
                # NovaMarket NM-01: the programmatic management surface. Each route
                # drives the real subsystem lifecycle; there is no market-data or
                # trading route because those phases have not shipped.
                ApiRoute(
                    "GET",
                    "/market/status",
                    "NovaMarket lifecycle, health, capabilities, dependencies and configuration.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/overview",
                    "Integrated NM-14 operational overview with real component state, recent records, activity and audit entries.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/contracts",
                    "NovaMarket's versioned contracts: lifecycle, events, permissions, horizons, domains and storage.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/config",
                    "NovaMarket's resolved configuration (secret references only) and its schema.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/config",
                    "Validate and apply a NovaMarket configuration change; invalid fields are refused with the reason.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/notifications/test",
                    "Send one real test email through the configured NovaMarket SMTP transport.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/universe/sync",
                    "Register the public NSE equity catalogue and its BSE listings in bulk; source coverage is reported honestly.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/enable",
                    "Initialise and start NovaMarket through its real lifecycle, reporting an initialization failure.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/disable",
                    "Disable NovaMarket and withdraw the capabilities, permissions and subscription it contributed.",
                    authenticated=True,
                ),
                # AT-01: the autonomy runtime/safety shell. Mutations additionally
                # require the dedicated X-Autonomy-Operator-Token credential; the
                # ordinary bearer token is not sufficient for these controls.
                ApiRoute(
                    "GET",
                    "/market/autonomy/contracts",
                    "AT-01 autonomy modes, transitions, storage, safety flags and limitations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/status",
                    "Current autonomy mode, runtime state, lease ownership and safety flags; no order capability is claimed.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/health",
                    "Autonomy lease/health slice; a missing or foreign lease is reported as degraded.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/reliability",
                    "AT-06 read-only reliability view: runtime/scheduler health, pipeline and financial counts, reconciliation pending, recovery state and failure domains.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/autonomy/recover",
                    "Reconcile durable execution and exit work after a restart and return to disabled mode without auto-resume; requires the operator token.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/autonomy/start",
                    "Start/renew the autonomy shell in monitoring or non-trading autonomous mode; requires the operator token.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/autonomy/pause",
                    "Pause autonomous work while retaining the runtime shell and lease; requires the operator token.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/autonomy/resume",
                    "Resume a paused autonomy shell in monitoring or autonomous mode; requires the operator token.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/autonomy/stop",
                    "Disable the autonomy shell and release its lease; requires the operator token.",
                    authenticated=True,
                ),
                # AT-02: session-aware monitoring and discovery. Reads never trade;
                # the two mutations require the separate autonomy operator token.
                ApiRoute(
                    "GET",
                    "/market/autonomy/session",
                    "Verified exchange-local session decision for an exchange (`exchange` query): open, not yet open, closed, weekend, holiday, unverified or unavailable; fail closed.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/monitoring/contracts",
                    "Static AT-02 capabilities, batch cap, refresh floor, cycle statuses and refusal reasons; no trading capability is claimed.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/monitoring",
                    "Session, scheduler, rotation, last cycle and health of the single AT-02 monitoring runner.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/monitoring/cycles",
                    "Persistent monitoring-cycle history, newest first (`limit` 1-200).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/monitoring/cycles/{cycle_id}",
                    "One monitoring cycle with its processed, deferred and rejected instruments.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/autonomy/monitoring/run",
                    "Run one monitoring cycle through the session, lease and rotation gates; requires the operator token and never creates an order or proposal.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/autonomy/monitoring/config",
                    "Persist validated monitoring configuration (interval floor, 20-instrument cap); requires the operator token.",
                    authenticated=True,
                ),
                # AT-03: decisions and NM-08 risk assessment. Reads expose persisted
                # provenance; the run mutation is operator-gated and cannot trade.
                ApiRoute(
                    "GET",
                    "/market/autonomy/decisions/contracts",
                    "Static AT-03 decision/risk capabilities, statuses, error codes and explicit non-goals.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/decisions/status",
                    "Decision pipeline health, latest run, recent signals/proposals and record counts.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/decisions/runs",
                    "Decision-run history, newest first (`limit` 1-200).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/decisions/runs/{run_id}",
                    "One decision run with evaluations, signals, proposals, risk and lifecycle events.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/decisions/signals",
                    "Generated signals with validation and provenance; filters: `run_id`, `strategy_id`, `portfolio_id`, `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/decisions/proposals",
                    "Proposal history with NM-08 outcomes; filters: `run_id`, `portfolio_id`, `instrument_id`, `status`, `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/decisions/proposals/{proposal_id}",
                    "One proposal with its signal, evaluation, NM-08 decision and events; never an order.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/decisions/predictions",
                    "PI-12 forecast-driven decisions with evidence, horizon, forecast identity and current risk/order status; filters: `run_id`, `portfolio_id`, `instrument_id`, `decision_type`, `limit`, `offset`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/decisions/predictions/{decision_id}",
                    "One PI-12 decision with its forecast evidence, immutable audit events, NM-08 result, AT-03 proposal and stored outcome.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/decisions/outcomes",
                    "Measured PI-12 outcomes separated into forecast accuracy, decision quality, execution and paper P&L; `limit`, `offset`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/decisions/forecast-policy",
                    "The effective PI-12 multi-horizon selection and reliability policy, its digest and source.",
                    authenticated=True,
                ),
                ApiRoute(
                    "PUT",
                    "/market/autonomy/decisions/forecast-policy",
                    "Validate and persist the PI-12 horizon selection/reliability policy; requires the operator token and cannot promote a model.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/autonomy/decisions/outcomes/run",
                    "Measure due PI-12 outcomes from stored forecasts, market bars and NM-10 records; requires the operator token and never mutates a portfolio or ledger.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/autonomy/decisions/run",
                    "Run AT-03 forecast/strategy evaluation and NM-08 risk assessment; requires the operator token and never submits an order, fill or financial mutation.",
                    authenticated=True,
                ),
                # AT-04: controlled paper execution through NM-10. Reads expose
                # the durable intent journal and authoritative order/fill state;
                # the run/reconcile controls require the operator token and the
                # engine still enforces autonomous mode, lease and idempotency.
                ApiRoute(
                    "GET",
                    "/market/autonomy/executions/contracts",
                    "Static AT-04 paper-execution capabilities, statuses, idempotency rules, error codes and non-goals.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/executions/status",
                    "AT-04 journal health, counts, runtime mode/lease gate and recent execution intents.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/executions",
                    "Autonomous paper-execution intents, newest first; filters: `status`, `portfolio_id`, `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/executions/failures",
                    "Execution intents with a failure, unknown submission or unresolved reconciliation; `limit` 1-500.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/executions/{execution_id}",
                    "One execution with its durable journal, authoritative NM-10 order, fills and transitions.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/executions/{execution_id}/events",
                    "Append-only AT-04 journal events for one execution; `limit` 1-1000.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/autonomy/executions/run",
                    "Forward eligible risk-approved AT-03 proposals to NM-10 paper execution; requires autonomous mode, a valid lease and the operator token.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/autonomy/executions/{execution_id}/reconcile",
                    "Reconcile one execution with NM-10 order, fill and ledger records; never creates an order or fill; requires the operator token.",
                    authenticated=True,
                ),
                # AT-05: stop-loss exit supervision over existing positions.
                # Reads expose the durable decision journal and the authoritative
                # NM-08/NM-10 records. The run/reconcile controls are operator
                # gated; there is no direct financial-write route. Target-price and
                # trailing-stop supervision are reported as unsupported baseline
                # gaps rather than invented.
                ApiRoute(
                    "GET",
                    "/market/autonomy/exits/contracts",
                    "Static AT-05 stop-loss exit capabilities, supported/unsupported conditions, safety boundaries and non-goals.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/exits/status",
                    "AT-05 mode, last supervision cycle, decision counts and supervised position state.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/exits/positions",
                    "Durable supervision and exit-trigger state for positions seen by AT-05, including target price, trailing stop, high-water mark and freshness; `limit` 1-500.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/exits/configurations",
                    "Persisted target-price and trailing-stop definitions for long paper positions; `limit` 1-500.",
                    authenticated=True,
                ),
                ApiRoute(
                    "PUT",
                    "/market/autonomy/exits/configurations/{portfolio_id}/{instrument_id}",
                    "Create or version one long-position target/trailing exit configuration; target must exceed average cost and trailing percent must be in (0, 100); requires the operator token.",
                    authenticated=True,
                ),
                ApiRoute(
                    "DELETE",
                    "/market/autonomy/exits/configurations/{portfolio_id}/{instrument_id}",
                    "Remove one persisted target/trailing exit configuration; the position is unchanged and stop-loss supervision remains active; requires the operator token.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/exits/decisions",
                    "Protective-exit decisions, newest first; filters: `portfolio_id`, `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/exits/decisions/{decision_id}",
                    "One exit decision with NM-08 provenance, NM-10 order, fills and reconciliation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/exits/decisions/{decision_id}/events",
                    "Append-only AT-05 exit journal events for one decision; `limit` 1-1000.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/autonomy/exits/learning",
                    "Advisory entry-to-exit learning evidence with completeness and attribution state; `decision_id`, `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/autonomy/exits/run",
                    "Run one configured exit-supervision cycle (stop-loss, target or trailing); monitoring evaluates only, autonomous and paused may submit risk-approved paper exits through NM-10; requires the operator token.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/autonomy/exits/decisions/{decision_id}/reconcile",
                    "Reconcile an exit with NM-10 order, fills, position and ledger records; never creates an order or fill; requires the operator token.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/workspace",
                    "The standalone NovaMarket operational workspace HTML; it consumes the authenticated market API and embeds no market values.",
                    authenticated=False,
                ),
                # NovaMarket NM-02: validated market data. Reads return stored
                # observations with real freshness and provenance; ingestion
                # reports what actually arrived rather than a claimed success.
                ApiRoute(
                    "GET",
                    "/market/data/status",
                    "Market-data repository, enabled and installed providers, ingestion worker, cache and verification state.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/data/providers",
                    "Installed provider adapters with declared limits, measured health and verified operations; `probe=true` runs a real liveness check.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/instruments",
                    "The registered canonical instrument catalogue; query parameters: `asset_class`, `exchange`, `limit`, `offset`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/instruments",
                    "Register one canonical instrument identity (exchange MIC + symbol) with explicit currency and timezone.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/instruments/lookup",
                    "Resolve and register instrument metadata through one provider (`provider`, `symbol`).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/data/ingest",
                    "Run one bounded collection cycle over the configured universe or named instruments and report per-instrument outcomes.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/data/quote/{instrument_id}",
                    "The latest reconciled price observation with quality, freshness and provenance; `refresh=true` fetches first. Path values contain a colon, e.g. XNSE:RELIANCE.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/data/history/{instrument_id}",
                    "Paginated price observations; query parameters: `provider`, `start`, `end`, `limit`, `offset`, `ascending`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/data/bars/{instrument_id}",
                    "Paginated validated OHLCV bars; query parameters: `interval`, `provider`, `start`, `end`, `limit`, `offset`, `ascending`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/screener",
                    "Market Overview rows with stored quote freshness plus exchange, Nifty-index, market-cap, sector, mover, search and arrangement filters; query parameters: `exchange`, `cap`, `index`, `sector`, `sort`, `movers`, `query`, `available_only`, `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/orderblock",
                    "Ranked bullish and bearish structure boards, F&O coverage, liquidity, Market X-Ray and sector flow over stored OHLCV; query parameters: `interval`, `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/orderblock/{instrument_id}",
                    "One instrument's closed-candle structure, order blocks, liquidity, invalidation levels and chart; query parameter: `interval`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/tradingwatch",
                    "Momentum radar, zone board, heatmap, market flow and explicit index-options availability; query parameters: `interval`, `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/tradingwatch/watchlists",
                    "Persisted watchlists enriched with the latest stored price and its freshness.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/tradingwatch/replay/{instrument_id}",
                    "Historical replay cut at `end_date`, with indicator values computed only from the prefix at each cursor.",
                    authenticated=True,
                ),
                # NovaMarket NM-03: NovaCoin wallets and the deterministic
                # ledger. Money moves only through balanced, append-only
                # transactions; no route writes a balance directly.
                ApiRoute(
                    "GET",
                    "/market/wallet/status",
                    "Ledger totals, schema version and the last reconciliation result.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/wallets",
                    "List NovaCoin wallets with available and total balances.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/wallets",
                    "Create a NovaCoin wallet; it always opens with a zero balance.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/wallets/transfer",
                    "Transfer NC between two wallets in one atomic, balanced transaction.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/wallets/{wallet_id}",
                    "One wallet with its balance and the transaction/entry counts behind it.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/wallets/{wallet_id}/transactions",
                    "The wallet's transactions, including transfers it received; query parameters: `limit`, `offset`, `ascending`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/wallets/{wallet_id}/ledger",
                    "The immutable ledger entries that produced the wallet's balance; query parameters: `limit`, `offset`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/wallets/{wallet_id}/deposit",
                    "Deposit simulated NC from the issuance account into one wallet.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/wallets/{wallet_id}/withdraw",
                    "Withdraw simulated NC from one wallet back to the issuance account.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/wallets/{wallet_id}/status",
                    "Freeze or close a wallet; closing requires a zero balance.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/wallets/{wallet_id}/reconcile",
                    "Recompute one wallet's balance from its ledger and report integrity.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/wallet/reconcile",
                    "Reconcile every wallet, the whole-ledger zero sum included.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/wallet/reversals",
                    "Reverse a posted transaction with mirrored entries; the original is never edited.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/events",
                    "Search market events by category, verification, importance, instrument, sector or text. Query parameters: `since`, `until`, `limit`, `offset`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/events/sources",
                    "Every declared event feed with its enablement and last fetch outcome.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/events/sources/{source_id}/enabled",
                    "Enable or disable one declared feed. Body: `enabled`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/events/refresh",
                    "Fetch one source, or every enabled source, and ingest what it reports. Body: optional `source_id`; nothing is stored when a fetch fails.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/events/expire",
                    "Expire events outside their category review window; history is retained.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/events/instrument/{instrument_id}",
                    "Events mapped to one canonical instrument (`MIC:SYMBOL`). Query parameters: `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/events/{event_id}",
                    "One event version with its evidence and lifecycle transitions. Query parameters: `version`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/events/{event_id}/evidence",
                    "Every source record behind one event, preserved as retrieved.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/events/{event_id}/history",
                    "An event's version history and lifecycle transitions, oldest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/discovery/status",
                    "Discovery cycles, universe boundaries, sources and totals.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/discovery/candidates",
                    "Scored candidates, newest first. Query parameters: `limit`, `min_score`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/discovery/candidates/{instrument_id}",
                    "One candidate with its latest evaluation and evaluation history.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/discovery/cycle",
                    "Run one bounded discovery cycle. Body: optional `force`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/discovery/reevaluate",
                    "Reevaluate watchlist members and apply lifecycle changes. Body: optional `watchlist_id`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/discovery/events/{event_id}",
                    "Evaluate the instruments an NM-04 event names; unverified events never add members.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/discovery/scheduler",
                    "Start or stop the periodic cycle runner. Body: `action` (`start`/`stop`) and optional `interval_seconds`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/discovery/history",
                    "Membership history and evaluation records. Query parameters: `watchlist_id`, `instrument_id`, `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/watchlists",
                    "Every watchlist with its kind and active member count.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/watchlists",
                    "Create a watchlist. Body: `name`, optional `kind` and `description`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/watchlists/{watchlist_id}",
                    "One watchlist with its ranked members. Query parameters: `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/watchlists/{watchlist_id}/members",
                    "Add an instrument. Body: `instrument_id`, optional `reason`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "DELETE",
                    "/market/watchlists/{watchlist_id}/members/{instrument_id}",
                    "Remove a member, keeping its membership history. Query parameter: optional `reason`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/watchlists/{watchlist_id}/rename",
                    "Rename a watchlist in place without changing its stable id or membership history. Body: `name`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/forecasts",
                    "Generate and store one forecast. Body: `instrument_id`, `horizon`, optional `engine` (`statistical`, `gradient`, `lstm`).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecasts",
                    "Stored forecasts, newest first. Query parameters: `instrument_id`, `horizon`, `status`, `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecasts/status",
                    "Forecast horizons, policy, model-family states (statistical, gradient, LSTM) and totals.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecasts/models",
                    "Statistical, gradient and LSTM model versions with lifecycle states and measured metrics. Query parameters: `instrument_id`, `horizon`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecasts/performance",
                    "Measured performance snapshots and the live aggregate. Query parameters: `model_id`, `instrument_id`, `horizon`, `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecasts/calibration",
                    "The calibration report for one model and horizon. Query parameters: `model_id`, `horizon`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/forecasts/train",
                    "Run chronological walk-forward training and comparison for the selected engine. Body: `instrument_id`, `horizon`, optional `engine` (`statistical`, `gradient`, `lstm`).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/forecasts/evaluate",
                    "Resolve every forecast whose target data now exists; optional `engine=lstm` resolves only LSTM forecasts.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/forecasts/models/{model_id}/promote",
                    "Promote a model that already has walk-forward evidence. Body: `instrument_id`, `horizon`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecasts/{forecast_id}/explain",
                    "On-demand global importance and local attribution for one statistical, gradient or LSTM forecast.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecasts/{forecast_id}",
                    "One immutable forecast record.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecasts/{forecast_id}/outcome",
                    "The recorded outcome, or a pending state with the reason.",
                    authenticated=True,
                ),
                # PI-09: durable forecast lifecycle scheduling. The mutations
                # only schedule/evaluate forecasts and remain authenticated;
                # no route can order, fill or mutate a portfolio.
                ApiRoute(
                    "POST",
                    "/market/forecasts/cycles",
                    "Schedule one durable forecast lifecycle for an instrument and horizon; optional `target_at`, `resolution`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecasts/cycles",
                    "Forecast cycles, those still having outstanding work (scheduled, active, awaiting confirmation or outcome) before purely historical ones, newest first inside each group. Query parameters: `instrument_id`, `exchange`, `horizon`, `state`, `target_date`, `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecasts/cycles/{cycle_id}",
                    "One forecast cycle with checkpoint timeline, immutable versions, revisions and outcomes.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecasts/scheduler",
                    "PI-09 scheduler health, durable queue state, missed checkpoints and forecast-only authority.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/forecasts/scheduler/tick",
                    "Run a bounded authenticated due-checkpoint batch; never creates an order or fill. Body: optional `limit` 1-20.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecasts/missed",
                    "Checkpoints whose planned or retry time passed without completion. Query parameter: `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecasts/evaluations/pending",
                    "Target-time evaluations waiting on validated observations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecasts/accuracy",
                    "Forecast accuracy history by stage with sample counts and reliability caveats. Query parameters: `instrument_id`, `horizon`, `stage`, `cycle_id`, `model_id`, `model_version`, `ensemble_id`, `target_date`, `regime`, `limit`.",
                    authenticated=True,
                ),
                # PI-10: swing and long-term horizons over the same forecasts.
                ApiRoute(
                    "POST",
                    "/market/forecasts/horizons",
                    "Generate 1d/3d/5d/30d/90d forecasts through the existing PI-08 ensemble, with point-in-time fundamentals and validated context. Body: `instrument_id`, optional `horizons`, `as_of`, `fundamentals`, `news_events`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecasts/horizons/status",
                    "PI-10 horizon profiles, target semantics, evidence rules and unchanged NM-08/NM-10 authorities.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecasts/horizons/performance",
                    "Outcome metrics separated by horizon, stage and family; no blended cross-horizon accuracy. Query parameters: `instrument_id`, `horizon`, `family`, `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecasts/horizons/{instrument_id}/context",
                    "Point-in-time sector, benchmark, peer, fundamental and event context. Query parameter: optional `as_of`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecasts/horizons/{instrument_id}",
                    "Latest stored forecast for each of the five horizons with uncertainty, evidence and cross-horizon alignment. Query parameter: optional `as_of`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/portfolios",
                    "Create and fund one simulated portfolio. Body: `horizon`, `funding_wallet_id`, `capital`, optional `name`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolios",
                    "Every portfolio. Query parameters: `horizon`, `status`, `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolios/status",
                    "Portfolio counts, states and the three horizon policies.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolios/horizons",
                    "The three horizon policies as data.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolios/{portfolio_id}",
                    "One portfolio with its valuation, holdings and constraints.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolios/{portfolio_id}/holdings",
                    "Current holdings with weights and unpriced markers.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolios/{portfolio_id}/valuation",
                    "Value the book, store a snapshot and publish the result.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolios/{portfolio_id}/valuations",
                    "Stored valuation snapshots. Query parameter: `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolios/{portfolio_id}/performance",
                    "Measured performance, gated on enough observations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolios/{portfolio_id}/decisions",
                    "Decision history. Query parameter: `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolios/{portfolio_id}/proposals",
                    "Rebalance proposals. Query parameters: `status`, `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolios/{portfolio_id}/lifecycle",
                    "Lifecycle transitions.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolios/{portfolio_id}/reconcile",
                    "Compare recorded funding and realised P&L with history.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/portfolios/{portfolio_id}/rebalance",
                    "Propose a validated rebalance. Body: optional `force`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/portfolios/{portfolio_id}/apply",
                    "Apply a validated proposal. Body: `proposal_id`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/portfolios/{portfolio_id}/status",
                    "Pause, resume or suspend. Body: `action`, optional `reason`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/portfolios/{portfolio_id}/close",
                    "Liquidate and release capital. Body: optional `reason`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/risk/assess",
                    "Assess a proposed action and persist one enforceable decision.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk/status",
                    "Risk counts, policies and assessment latency.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk/contracts",
                    "Versioned risk outcomes, metrics, guarantees and limits.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk/decisions",
                    "Risk decision history, newest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk/decisions/{decision_id}",
                    "One risk decision with its evidence.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/risk/decisions/{decision_id}/apply",
                    "Apply an approved decision to its bound proposal.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk/portfolio/{portfolio_id}",
                    "Portfolio risk metrics, breaches and positions.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk/portfolio/{portfolio_id}/positions",
                    "Position-level risk for one portfolio.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk/position/{portfolio_id}/{instrument_id}",
                    "Risk for one held instrument.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk/exposure",
                    "Combined exposure across all three portfolios.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk/policies",
                    "Current risk policies and their version history.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/risk/policies/{policy_id}",
                    "Create a new audited policy version.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk/alerts",
                    "Risk alerts by lifecycle status.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/risk/alerts/{alert_id}/acknowledge",
                    "Acknowledge an alert.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/risk/alerts/{alert_id}/resolve",
                    "Resolve an alert.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/risk/stress",
                    "Run a labelled hypothetical stress scenario.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk/stress",
                    "Stored stress-scenario results.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET", "/market/risk/overrides", "Risk override history.", authenticated=True
                ),
                ApiRoute(
                    "POST",
                    "/market/risk/overrides",
                    "Record a time-bounded override for a reviewable decision.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/strategies/catalog",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET", "/market/strategies", "NM-09 strategy operation.", authenticated=True
                ),
                ApiRoute(
                    "POST", "/market/strategies", "NM-09 strategy operation.", authenticated=True
                ),
                ApiRoute(
                    "GET",
                    "/market/strategies/status",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/strategies/contracts",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/strategies/proposals",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/strategies/proposals/{proposal_id}",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/strategies/proposals/{proposal_id}/forward",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/strategies/conflicts",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/strategies/signals",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/strategies/signals/{signal_id}",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/strategies/{strategy_id}",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/strategies/{strategy_id}/config",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/strategies/{strategy_id}/versions",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/strategies/{strategy_id}/activate",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/strategies/{strategy_id}/pause",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/strategies/{strategy_id}/suspend",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/strategies/{strategy_id}/retire",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/strategies/{strategy_id}/degrade",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/strategies/{strategy_id}/evaluate",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/strategies/{strategy_id}/signals",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/strategies/{strategy_id}/performance",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/strategies/{strategy_id}/lifecycle",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/strategies/run",
                    "NM-09 strategy operation.",
                    authenticated=True,
                ),
                # NM-10: approved proposals become simulated orders; no route
                # places a real order or accepts an arbitrary fill.
                ApiRoute(
                    "POST",
                    "/market/execution/orders",
                    "Submit an approved NM-09 proposal as a simulated paper order.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/execution/orders",
                    "List simulated orders with optional `status` and `portfolio_id` filters.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/execution/orders/{order_id}",
                    "One paper order with transitions, fills and reservations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/execution/orders/{order_id}/fills",
                    "Fills recorded for one paper order.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/execution/orders/{order_id}/history",
                    "The audited order-state transition history.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/execution/orders/{order_id}/process",
                    "Re-evaluate and fill an open paper order against fresh market data.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/execution/orders/{order_id}/cancel",
                    "Cancel remaining quantity, preserving completed fills and releasing reservations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/execution/fills",
                    "All recorded paper fills, newest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/execution/status",
                    "Paper execution counts, latency, configuration and safety flags.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/execution/contracts",
                    "Versioned paper-execution contract, order types and lifecycle.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/execution/fees",
                    "The configured simulated fee schedule and disclaimer.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/execution/slippage",
                    "The configured slippage model, assumptions and limitations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/execution/reconcile",
                    "Run independent reconciliation for one order or all orders; never repairs.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/execution/reconciliations",
                    "Stored reconciliation reports, newest first.",
                    authenticated=True,
                ),
                # NM-11: historical backtests are isolated simulations. The
                # surface exposes no broker, wallet or live-order operation.
                ApiRoute(
                    "GET",
                    "/market/backtests/status",
                    "Backtest counts, active runs and isolation guarantees.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/backtests/contracts",
                    "Backtest frequencies, order types, guarantees and limitations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/backtests/validate",
                    "Validate a backtest configuration and optionally its historical data coverage.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/backtests",
                    "Create an immutable historical backtest configuration.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/backtests",
                    "List persisted backtests, newest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/backtests/compare",
                    "Compare completed runs across several metrics without ranking by final value.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/backtests/comparisons",
                    "Persisted comparisons and parameter-sensitivity runs, newest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/backtests/{backtest_id}/walk-forward",
                    "Run sequential out-of-sample train/test folds for a configuration.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/backtests/{backtest_id}/start",
                    "Replay a created backtest against point-in-time historical bars.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/backtests/{backtest_id}/cancel",
                    "Cancel a created or active backtest.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/backtests/{backtest_id}",
                    "One backtest with its configuration, result and evidence.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/backtests/{backtest_id}/results",
                    "Stored performance, benchmark, robustness and data-quality results.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/backtests/{backtest_id}/metrics",
                    "Performance and execution metrics for one completed run.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/backtests/{backtest_id}/trades",
                    "Simulated fills with fees and slippage.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/backtests/{backtest_id}/orders",
                    "Simulated order lifecycle records including partial fills and expiry.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/backtests/{backtest_id}/risk-decisions",
                    "Historical risk-policy decisions for every proposed action.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/backtests/{backtest_id}/signals",
                    "Strategy signals generated from point-in-time history.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/backtests/{backtest_id}/data-quality",
                    "Dataset provenance, unavailable bars and documented limitations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/backtests/{backtest_id}/verify",
                    "Replay the persisted bars and verify the reproducibility hash.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/backtests/{backtest_id}/sensitivity",
                    "Run explicit parameter variants without claiming out-of-sample validity.",
                    authenticated=True,
                ),
                # PI-13: isolated RL research. These routes only touch the RL
                # simulated book and its own experience database.
                ApiRoute(
                    "GET",
                    "/market/rl/status",
                    "RL environment counts, isolation guarantees and health.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/rl/contracts",
                    "Versioned observation, action, reward and transition contracts.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/rl/health",
                    "Bounded resource, latency, error and storage health for RL research.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/rl/environments",
                    "Create an immutable isolated RL environment configuration.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/rl/environments",
                    "List persisted RL environment configurations, newest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/rl/environments/{environment_id}",
                    "One RL environment configuration and its version evidence.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/rl/environments/{environment_id}/episodes",
                    "Start a reproducible isolated historical RL episode.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/rl/episodes",
                    "List RL episodes and their simulated summaries.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/rl/episodes/{episode_id}",
                    "One RL episode, its observation and simulated portfolio state.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/rl/episodes/{episode_id}/step",
                    "Apply one validated RL action and record the resulting transition.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/rl/episodes/{episode_id}/reset",
                    "Reset one RL episode to its initial simulated state and clear its experiences.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/rl/episodes/{episode_id}/cancel",
                    "Cancel a live RL episode without touching any production book.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/rl/episodes/{episode_id}/transitions",
                    "Bounded transition history for one RL episode.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/rl/episodes/{episode_id}/experiences",
                    "Bounded experience records with dataset and feature provenance.",
                    authenticated=True,
                ),
                # PI-14: rewards and policy evaluation. These routes read only
                # isolated simulated transitions and never submit an order.
                ApiRoute(
                    "GET",
                    "/market/rewards/status",
                    "Reward and policy-evaluation counts, isolation guarantees and health.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/rewards/contracts",
                    "Reward component, normalization and policy-evaluation contracts.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/rewards/health",
                    "Bounded reward-store and calculation health.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/rewards/baselines",
                    "Baseline policies, shared evaluation conditions and limitations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/rewards/configurations",
                    "Create or reuse an immutable experimental reward configuration.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/rewards/configurations",
                    "List persisted reward configurations, newest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/rewards/configurations/{config_id}",
                    "One immutable reward configuration and its version evidence.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/rewards/episodes/{episode_id}/calculate",
                    "Calculate and persist component rewards for stored PI-13 transitions.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/rewards/episodes/{episode_id}",
                    "Persisted reward results and attribution for one RL episode.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/rewards/results/{reward_id}",
                    "One reproducible reward result and its component breakdown.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/rewards/evaluations",
                    "Evaluate policies on identical chronological historical conditions.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/rewards/evaluations/cancel",
                    "Request cancellation of a running bounded policy evaluation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/rewards/evaluations",
                    "List persisted policy evaluations, newest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/rewards/evaluations/{evaluation_id}",
                    "One policy evaluation with metrics and limitations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/rewards/comparisons",
                    "Persisted baseline comparisons with identical-condition evidence.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/rewards/stability",
                    "Compare policy stability across named chronological splits.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/rewards/walk-forward",
                    "Evaluate successive out-of-sample historical windows without test fitting.",
                    authenticated=True,
                ),
                # PI-15: isolated experimental RL training. These routes own
                # research artifacts only and have no execution/autonomy path.
                ApiRoute(
                    "GET",
                    "/market/training/status",
                    "Experimental RL training counts, resource bounds and isolation guarantees.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/training/contracts",
                    "DQN selection rationale, bounded limits and training contracts.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/training/health",
                    "Training worker, storage and resource health.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/training/datasets",
                    "Stored PI-02 dataset versions a training run can reference, with their instruments.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/training/configurations",
                    "Create or reuse an immutable versioned training configuration.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/training/configurations",
                    "List persisted training configurations, newest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/training/configurations/{config_id}",
                    "One immutable training configuration and its validation evidence.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/training/validate",
                    "Validate a training configuration without starting a run.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/training/runs",
                    "Start a bounded isolated DQN training run.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/training/runs",
                    "List experimental training runs and progress.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/training/runs/{run_id}",
                    "One training run, its lifecycle and latest metrics.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/training/runs/{run_id}/pause",
                    "Request a safe checkpoint pause of a training run.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/training/runs/{run_id}/resume",
                    "Resume a paused training run in its worker.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/training/runs/{run_id}/cancel",
                    "Cancel a training run at the next safe step boundary.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/training/runs/{run_id}/retry",
                    "Start a new training run from a terminal run's configuration.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/training/runs/{run_id}/metrics",
                    "Bounded learning, action and resource metrics for one run.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/training/runs/{run_id}/checkpoints",
                    "Checkpoint history with integrity metadata.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/training/runs/{run_id}/checkpoints",
                    "Request a manual checkpoint at the next safe boundary.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/training/checkpoints/{checkpoint_id}",
                    "One checkpoint and its integrity metadata.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/training/checkpoints/{checkpoint_id}/resume",
                    "Resume training from a validated checkpoint as a new run.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/training/policies",
                    "List experimental policy artifacts with lifecycle status.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/training/policies/{policy_id}",
                    "One experimental policy artifact and its evaluation evidence.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/training/policies/{policy_id}/archive",
                    "Archive a research policy; never activate it.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/training/policies/{policy_id}/validate",
                    "Validate a trained policy on the chronological validation split.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/training/policies/{policy_id}/test",
                    "Evaluate a validated policy on the untouched out-of-sample split.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/training/compare",
                    "Compare policies only when their recorded historical conditions match.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/training/optimize",
                    "Start a bounded random or grid hyperparameter search using train/validation only.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/training/searches/{search_id}",
                    "One bounded hyperparameter search and its trial evidence.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/training/evaluations",
                    "Persisted PI-15 validation and out-of-sample evidence records.",
                    authenticated=True,
                ),
                # PI-16: adaptive intelligence is advisory only. Every route
                # below reads or records research evidence; none can activate a
                # policy, start training, submit an order or change autonomy.
                ApiRoute(
                    "GET",
                    "/market/adaptive/status",
                    "Adaptive-intelligence counts, source availability and advisory guarantees.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/adaptive/contracts",
                    "Detectors, thresholds, recommendation vocabulary and safety boundaries.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/adaptive/health",
                    "Adaptive store and source health.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/adaptive/dashboard",
                    "Latest regime, suitability, drift, degradation and adaptation evidence.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/adaptive/configurations",
                    "Create or reuse an immutable detection configuration.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/adaptive/configurations",
                    "List persisted detection configurations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/adaptive/configurations/{config_id}",
                    "One immutable detection configuration.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/adaptive/regime/analyze",
                    "Analyze a PI-04 regime snapshot without inventing a conflicting label.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/adaptive/regime/latest",
                    "Latest persisted regime analysis for one instrument.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/adaptive/regime/annotations",
                    "Create a versioned derived regime annotation without mutating experiences.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/adaptive/regime/annotations",
                    "List derived regime annotations and preserved source timestamps.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/adaptive/regime/evaluations",
                    "Record one PI-14 evaluation as regime-specific policy evidence.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/adaptive/regime/evaluations",
                    "Regime-specific policy performance with sample-size caveats.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/adaptive/experiences/query",
                    "Retrieve regime-labelled experiences available at an explicit as-of time.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/adaptive/changes/detect",
                    "Detect meaningful market-condition change with supporting evidence.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/adaptive/changes",
                    "Persisted market-condition change events.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/adaptive/drift/detect",
                    "Detect data or concept drift with future outcomes excluded.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/adaptive/drift",
                    "Persisted drift results and their parameters.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/adaptive/degradation/monitor",
                    "Monitor persistent policy-performance degradation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/adaptive/degradation",
                    "Persisted policy-degradation assessments.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/adaptive/suitability/assess",
                    "Assess experimental policy suitability for one regime.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/adaptive/suitability",
                    "Persisted policy-suitability assessments.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/adaptive/recommendations",
                    "Generate an evidence-based advisory adaptation recommendation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/adaptive/recommendations",
                    "Persisted adaptation recommendations with hysteresis state.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/adaptive/recommendations/{recommendation_id}/acknowledge",
                    "Acknowledge an advisory recommendation; never activates a policy.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/adaptive/retraining",
                    "Record a retraining recommendation without starting training.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/adaptive/retraining",
                    "Persisted retraining recommendations and required conditions.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/adaptive/reassessments",
                    "Create an explicit bounded reassessment request.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/adaptive/reassessments",
                    "List reassessment requests and outcomes.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/adaptive/reassessments/{reassessment_id}/run",
                    "Run one explicitly authorised bounded reassessment.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/adaptive/reassessments/{reassessment_id}",
                    "One reassessment request and its result.",
                    authenticated=True,
                ),
                # PI-17: advisory multi-agent research. These routes can create,
                # run and cancel research cycles only; none can approve risk,
                # submit an order, mutate a portfolio or change autonomy.
                ApiRoute(
                    "GET",
                    "/market/multiagent/status",
                    "Multi-agent counts, advisory guarantees and preserved authority boundaries.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/multiagent/contracts",
                    "Agent roles, communication contracts, conflict rules and resource limits.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/multiagent/health",
                    "Agent health, isolated store health and authority isolation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/multiagent/agents",
                    "Registered financial-intelligence agents, configuration and performance.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/multiagent/agents/{agent_id}",
                    "One agent's contract, health, configuration history and performance.",
                    authenticated=True,
                ),
                ApiRoute(
                    "PUT",
                    "/market/multiagent/agents/{agent_id}/config",
                    "Update an agent's enabled state, timeout or read capabilities within its fixed role boundary.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/multiagent/cycles",
                    "Create a bounded decision cycle for one instrument and one horizon.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/multiagent/cycles",
                    "List decision cycles and their orchestration status.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/multiagent/cycles/{cycle_id}",
                    "One decision cycle with runs, conflicts, decision and events.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/multiagent/cycles/{cycle_id}/run",
                    "Run a pending decision cycle with bounded concurrent agents.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/multiagent/cycles/{cycle_id}/cancel",
                    "Cancel a pending or running research cycle; never cancels an order.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/multiagent/cycles/{cycle_id}/agents",
                    "Agent responses and validated communication records for a cycle.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/multiagent/cycles/{cycle_id}/evidence",
                    "Shared evidence with provenance, freshness and validation state.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/multiagent/cycles/{cycle_id}/conflicts",
                    "Conflicts, severity, resolution rules and unresolved disagreements.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/multiagent/cycles/{cycle_id}/decision",
                    "The advisory recommendation with confidence limitations and explanation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/multiagent/cycles/{cycle_id}/handoff",
                    "Explicit advisory handoff into PI-12, NM-08 and NM-10; never an order.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/multiagent/history",
                    "Advisory decision history kept separate from financial accounting.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/multiagent/performance",
                    "Execution reliability for every agent, with historical quality left unclaimed.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/multiagent/performance/{agent_id}",
                    "One agent's latency, failure, validation and evidence reliability.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/optimization/status",
                    "Optimization store counts, limits and preserved NM-08/NM-10 authority.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/optimization/contracts",
                    "Objectives, methods, constraints, statuses and safety boundaries.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/optimization/health",
                    "Isolated optimization-store health and authority isolation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/optimization/configs",
                    "Create a validated portfolio optimization configuration.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/optimization/configs",
                    "List saved optimization configurations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/optimization/configs/{config_id}",
                    "One saved optimization configuration.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/optimization/validate",
                    "Validate authoritative portfolio, market, forecast and covariance inputs without generating an allocation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/optimization/runs",
                    "Run a constrained optimization and persist an isolated advisory result.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/optimization/runs",
                    "List optimization runs and statuses.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/optimization/history",
                    "Optimization research history kept separate from portfolio accounting.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/optimization/latest",
                    "Latest completed optimization for a portfolio and optional horizon.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/optimization/runs/{run_id}",
                    "One optimization run with provenance, warnings and full analysis.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/optimization/runs/{run_id}/allocations",
                    "Target weights, capital and cash reserve for one run.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/optimization/runs/{run_id}/diversification",
                    "Concentration and correlation-based diversification metrics.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/optimization/runs/{run_id}/risk",
                    "Research risk metrics; NM-08 remains the operational authority.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/optimization/runs/{run_id}/liquidity",
                    "Liquidity participation estimates and limitations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/optimization/runs/{run_id}/rebalancing",
                    "Advisory rebalancing actions, costs and thresholds.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/optimization/runs/{run_id}/scenarios",
                    "Historical and hypothetical stress scenarios for one run.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/optimization/runs/{run_id}/scenarios",
                    "Add operator-defined hypothetical shocks to a run.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/optimization/runs/{run_id}/backtests",
                    "Compare allocation strategies on point-in-time history with costs.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/optimization/runs/{run_id}/handoff",
                    "Advisory handoff into PI-12, NM-08 and NM-10; never an order.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/optimization/runs/{run_id}/cancel",
                    "Cancel a running research optimization; never cancels an order.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/optimization/backtests",
                    "List stored strategy backtests.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/optimization/backtests/{backtest_id}",
                    "One stored strategy backtest and its method comparison.",
                    authenticated=True,
                ),
                # PI-19: financial research is advisory and isolated from finance.
                ApiRoute(
                    "GET",
                    "/market/research/status",
                    "Financial-research store counts, limits and preserved NM-08/NM-10 authority.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/research/contracts",
                    "Research horizons, analysis types, verification rules, point-in-time rules and authority boundaries.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/research/health",
                    "Isolated research-store health and authority isolation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/research/runs",
                    "Create a bounded point-in-time company-research task.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/research/runs/{run_id}/run",
                    "Execute a pending research task and persist its evidence-linked report.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/research/runs/{run_id}/cancel",
                    "Cancel an eligible research task; never cancels an order.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/research/runs/{run_id}/refresh",
                    "Refresh a completed research report into a new version without overwriting the prior conclusion.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/research/runs",
                    "List research tasks and statuses.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/research/history",
                    "Research history kept separate from financial accounting.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/research/latest",
                    "Latest completed research for an instrument and optional horizon.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/research/runs/{run_id}",
                    "One research task with provenance, claims, warnings and full analysis.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/research/runs/{run_id}/company",
                    "Company profile with evidence, verification and missing-field disclosure.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/research/runs/{run_id}/financials",
                    "Point-in-time statement analysis, TTM, ratios and restatements.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/research/runs/{run_id}/events",
                    "Corporate events with announcement/effective dates and verification.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/research/runs/{run_id}/news",
                    "News evidence, sentiment interpretation and untrusted-content flags.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/research/runs/{run_id}/sector",
                    "Sector and industry context with source period.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/research/runs/{run_id}/peers",
                    "Documented peer selection and comparison results.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/research/runs/{run_id}/valuation",
                    "Relative or DCF valuation assumptions, range, sensitivity and limitations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/research/runs/{run_id}/thesis",
                    "Evidence-linked investment thesis and uncertainty; never an execution authorization.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/research/runs/{run_id}/evidence",
                    "Every eligible evidence record with source, time and verification.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/research/runs/{run_id}/claims",
                    "Material claims linked to evidence, including conflicts and unverified claims.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/research/runs/{run_id}/features",
                    "Versioned, point-in-time research features for PI-04 evaluation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/research/runs/{run_id}/handoff",
                    "Structured advisory handoff to PI-12/PI-17/PI-18 and NM-08/NM-10.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/research/monitor",
                    "Rate-limited targeted refresh check for one instrument; no background polling.",
                    authenticated=True,
                ),
                # PI-20: unified read-only prediction intelligence dashboard.
                ApiRoute(
                    "GET",
                    "/market/intelligence/status",
                    "Dashboard initialisation, preference-store health and preserved authority boundaries.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/contracts",
                    "Dashboard statuses, horizons, separation rules and read-only safety contract.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/navigation",
                    "Dashboard views and supported filters for consistent frontend navigation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/overview",
                    "Prediction, model, decision, regime, research and portfolio summary with bounded filters.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/instruments/{instrument_id}",
                    "Instrument price, freshness, chart, regime, research and forecast intelligence.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/forecasts",
                    "Normalized forecast rows with model versions, intervals and calibration status.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/compare",
                    "Model comparison only within a shared reference timestamp, target and horizon.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/horizons/{instrument_id}",
                    "Separate intraday, swing and long-term forecast families without merged targets.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/lifecycle",
                    "Recorded forecast checkpoint status, lateness and immutable versions.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/performance",
                    "Forecast evaluation metrics with sample size and accuracy/profit separation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/regimes",
                    "Current and historical market regimes, transitions, drift and suitability.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/decisions",
                    "Paginated multi-agent decision history with confidence provenance.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/decisions/{cycle_id}",
                    "Complete agent contributions, evidence, conflicts and final recommendation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/explain/{subject_id}",
                    "Decision, forecast or research explanation assembled from recorded evidence only.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/attribution/{forecast_id}",
                    "Model-specific feature attribution where the model actually computed it.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/evidence/{subject_id}",
                    "Evidence provenance timeline for a decision or research run.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/research",
                    "Financial research runs, reported facts, thesis, uncertainty and provenance.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/portfolio",
                    "Authoritative portfolio snapshot separated from experimental optimization advice.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/policies",
                    "Experimental RL policies, training evidence, drift, degradation and retraining advice.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/replay/{cycle_id}",
                    "Point-in-time decision replay with future news, evidence and research excluded.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/intelligence/filters",
                    "Saved presentation-only dashboard filter preset and available presets.",
                    authenticated=True,
                ),
                ApiRoute(
                    "PUT",
                    "/market/intelligence/filters",
                    "Save a presentation-only dashboard filter preset; no financial state is changed.",
                    authenticated=True,
                ),
                # PI-21: advisory portfolio management over NM-07/08/10 and PI-12.
                ApiRoute(
                    "GET",
                    "/market/portfolio-management/status",
                    "Management cycles, runtime mode, lease, kill-switch and authority boundaries.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolio-management/contracts",
                    "Portfolio-management actions, horizons, statuses and financial-isolation contract.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolio-management/navigation",
                    "Advanced portfolio-management views and supported controls.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolio-management/config",
                    "Versioned management thresholds, reserve target and change history.",
                    authenticated=True,
                ),
                ApiRoute(
                    "PUT",
                    "/market/portfolio-management/config",
                    "Update advisory management thresholds; no portfolio, cash or ledger state is changed.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/portfolio-management/review",
                    "Run one authoritative-snapshot portfolio review and record explainable advisory decisions.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolio-management/cycles",
                    "Paginated management-cycle history, newest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolio-management/cycles/{cycle_id}",
                    "One management cycle with snapshot references, decisions, analytics and events.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolio-management/cycles/{cycle_id}/events",
                    "Durable management-cycle event history.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/portfolio-management/cycles/{cycle_id}/handoff",
                    "Ask existing PI-12/AT-04 execution to continue matching persisted NM-08-approved proposals.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/portfolio-management/cycles/{cycle_id}/reconcile",
                    "Reconcile PI-21 handoff records through the authoritative NM-10 execution engine.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/portfolio-management/recover",
                    "Interrupt abandoned PI-21 cycles and existing autonomy work; recovery always returns to disabled.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolio-management/portfolios/{portfolio_id}/snapshot",
                    "Latest authoritative portfolio snapshot reference with valuation provenance and anomalies.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolio-management/portfolios/{portfolio_id}/positions",
                    "Position assessments with evidence, regime, research and recommended action.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolio-management/portfolios/{portfolio_id}/opportunities",
                    "Bounded opportunity discovery with portfolio compatibility and allocation proposals.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolio-management/portfolios/{portfolio_id}/decisions",
                    "Portfolio decision history with supporting and contradictory evidence.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolio-management/portfolios/{portfolio_id}/capital",
                    "Capital deployment recommendation that reconciles available and reserved virtual cash.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolio-management/portfolios/{portfolio_id}/cash",
                    "Cash and reserve management recommendation without modifying balances.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolio-management/portfolios/{portfolio_id}/exposure",
                    "Instrument, sector, concentration and diversification exposure research metrics.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolio-management/portfolios/{portfolio_id}/rebalancing",
                    "Bounded PI-18-informed rebalancing proposal with costs, turnover and risk restrictions.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/portfolio-management/portfolios/{portfolio_id}/analytics",
                    "Portfolio performance separated from recommendation and decision quality.",
                    authenticated=True,
                ),
                # PI-22: advisory risk intelligence over NM-07/08/10 and PI-12.
                ApiRoute(
                    "GET",
                    "/market/risk-intelligence/status",
                    "Risk-intelligence runtime, store, assessment and authority status.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk-intelligence/contracts",
                    "Risk levels, statuses, methodologies, limits and financial-isolation contract.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk-intelligence/navigation",
                    "Risk-intelligence workspace views and supported controls.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk-intelligence/config",
                    "Versioned analytical risk thresholds and change history.",
                    authenticated=True,
                ),
                ApiRoute(
                    "PUT",
                    "/market/risk-intelligence/config",
                    "Update analytical thresholds; no portfolio, cash or ledger state is changed.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/risk-intelligence/analyze",
                    "Run one bounded advisory risk assessment from authoritative snapshots.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk-intelligence/portfolios/{portfolio_id}/overview",
                    "Latest portfolio risk overview with evidence, trend and warnings.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk-intelligence/portfolios/{portfolio_id}/positions",
                    "Position-level risk, liquidity, drawdown, forecast and NM-08 references.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk-intelligence/portfolios/{portfolio_id}/concentration",
                    "Instrument, sector, industry, HHI and effective-holdings concentration.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk-intelligence/portfolios/{portfolio_id}/correlation",
                    "Timestamp-aligned pairwise and rolling correlation evidence.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk-intelligence/portfolios/{portfolio_id}/volatility",
                    "Historical volatility, downside deviation, VaR and CVaR.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk-intelligence/portfolios/{portfolio_id}/drawdown",
                    "Authoritative portfolio drawdown episodes, duration and recovery.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk-intelligence/portfolios/{portfolio_id}/liquidity",
                    "Position/ADV, spread, impact ranges and liquidity classification.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk-intelligence/portfolios/{portfolio_id}/regime",
                    "Stored PI-04/PI-16 regime exposure with confidence.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk-intelligence/portfolios/{portfolio_id}/history",
                    "Paginated immutable risk-assessment history, newest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk-intelligence/portfolios/{portfolio_id}/trend",
                    "Risk trend across recorded assessments, separate from performance.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk-intelligence/portfolios/{portfolio_id}/recommendations",
                    "Advisory protective recommendations with NM-08 workflow references.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk-intelligence/alerts",
                    "Deduplicated risk warnings filtered by portfolio and status.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/risk-intelligence/alerts/{alert_id}/action",
                    "Acknowledge, resolve or reopen a risk warning; no financial mutation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk-intelligence/scenarios",
                    "Saved hypothetical scenario configurations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/risk-intelligence/scenarios",
                    "Create a bounded hypothetical scenario; never a prediction or order.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/risk-intelligence/portfolios/{portfolio_id}/scenarios/run",
                    "Run an isolated scenario against the latest analytical snapshot.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk-intelligence/portfolios/{portfolio_id}/scenario-runs",
                    "Reproducible scenario-run history with assumptions and digests.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/risk-intelligence/portfolios/{portfolio_id}/stress-tests",
                    "Historical, hypothetical, volatility, correlation, liquidity and combined stress results.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/risk-intelligence/portfolios/{portfolio_id}/stress-tests",
                    "Run an isolated stress test with explicit assumptions and limits.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/risk-intelligence/recover",
                    "Recover interrupted analytical work; recovery never enables trading.",
                    authenticated=True,
                ),
                # PI-23: advisory forecast optimization over PI-05 through PI-11.
                ApiRoute(
                    "GET",
                    "/market/forecast-intelligence/status",
                    "Forecast-optimization runtime, evidence and authority status.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecast-intelligence/contracts",
                    "Selection, calibration, drift, reliability and financial-isolation contracts.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecast-intelligence/navigation",
                    "Forecast-optimization workspace views and supported controls.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecast-intelligence/config",
                    "Current bounded evidence thresholds for PI-23 decisions.",
                    authenticated=True,
                ),
                ApiRoute(
                    "PUT",
                    "/market/forecast-intelligence/config",
                    "Update advisory thresholds; no model, weight, risk or order is changed.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecast-intelligence/registry",
                    "Model evidence registry without automatic promotion.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecast-intelligence/history",
                    "Append-only PI-23 analysis history, newest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecast-intelligence/overview",
                    "Forecast-optimization overview from persisted evaluations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecast-intelligence/comparison/{instrument_id}",
                    "Compare models on one instrument, horizon and task.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecast-intelligence/horizons/{instrument_id}",
                    "Cross-horizon consistency and per-horizon evidence.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecast-intelligence/regimes/{instrument_id}",
                    "Regime-conditioned model evidence and reliability.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/forecast-intelligence/instruments",
                    "Cross-instrument consistency for a bounded instrument set.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/forecast-intelligence/evaluate",
                    "Evaluate chronological model evidence without a test-set selection.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/forecast-intelligence/select",
                    "Recommend a model from validation evidence; never promote it.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/forecast-intelligence/calibration",
                    "Fit and holdout-evaluate a calibration without production mutation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/forecast-intelligence/uncertainty",
                    "Separate interval, model and regime uncertainty.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/forecast-intelligence/disagreement",
                    "Model disagreement with stale and missing evidence named.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/forecast-intelligence/consistency",
                    "Per-instrument multi-horizon forecast consistency.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/forecast-intelligence/drift",
                    "PSI/KS drift and residual degradation evidence.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/forecast-intelligence/degradation",
                    "Performance degradation against a chronological baseline.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/forecast-intelligence/weights",
                    "Bounded advisory weight recommendations; production weights are unchanged.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/forecast-intelligence/reliability",
                    "Categorical reliability states with evidence limitations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/forecast-intelligence/fallback",
                    "Fallback or abstention recommendation without executing a forecast.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/forecast-intelligence/analyze",
                    "Run the full advisory forecast-optimization analysis.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/forecast-intelligence/health",
                    "Store integrity and dependency health for PI-23.",
                    authenticated=True,
                ),
                # PI-24: event-aware predictive intelligence over NM-04 evidence.
                ApiRoute(
                    "GET",
                    "/market/event-intelligence/status",
                    "PI-24 counts, latest event evidence and authority boundaries.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/event-intelligence/contracts",
                    "Event groups, statuses, relevance classes and limitations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/event-intelligence/navigation",
                    "Event-intelligence workspace views and authority contract.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/event-intelligence/config",
                    "Bounded event-intelligence thresholds.",
                    authenticated=True,
                ),
                ApiRoute(
                    "PUT",
                    "/market/event-intelligence/config",
                    "Update bounded event-intelligence thresholds.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/event-intelligence/events",
                    "Normalized NM-04 events with PI-24 status and provenance.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/event-intelligence/events/{event_id}",
                    "One normalized event with source evidence and transitions.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/event-intelligence/timeline",
                    "Event version, publication and processing timeline.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/event-intelligence/relevance",
                    "Assess instrument, sector, market or insufficient event relevance.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/event-intelligence/impact",
                    "Event impact channels and observed historical co-movement.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/event-intelligence/warnings",
                    "Event-approaching, uncertainty and volatility warnings for a horizon.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/event-intelligence/features",
                    "Point-in-time event feature snapshot for PI-04.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/event-intelligence/forecast-context",
                    "Event-aware forecast context without forecast or model mutation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/event-intelligence/outcomes",
                    "Record an observed event outcome from stored market bars.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/event-intelligence/outcomes/study",
                    "Reproducible historical event-outcome study.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/event-intelligence/forecast-evaluation",
                    "Event-aware versus non-event-aware forecast error comparison, split on declared event awareness.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/event-intelligence/agents",
                    "Read-only verified event evidence for existing PI-17 agents.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/event-intelligence/portfolio/{portfolio_id}/context",
                    "Portfolio event exposure and uncertainty for PI-21/PI-22.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/event-intelligence/sync",
                    "Normalize current NM-04 events into the PI-24 evidence store.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/event-intelligence/ingest",
                    "Refresh an existing NM-04 feed and normalize its events.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/event-intelligence/analyze",
                    "Run relevance, features, warnings, forecast context and agent evidence.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/event-intelligence/overview",
                    "Event counts, status distribution and current warnings.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/event-intelligence/history",
                    "Immutable PI-24 relevance, feature, warning and outcome records.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/event-intelligence/health",
                    "PI-24 store integrity, counts and source errors.",
                    authenticated=True,
                ),
                # PI-11: forecast evaluation is separate from trading replay.
                ApiRoute(
                    "GET",
                    "/market/evaluations/status",
                    "Forecast-evaluation counts, evidence sources and authority boundaries.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/evaluations/contracts",
                    "Forecast-evaluation methodology, leakage controls and limitations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/evaluations",
                    "List persisted forecast-evaluation reports, newest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/evaluations",
                    "Evaluate stored forecasts and target outcomes without generating a forecast or trading.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/evaluations/{evaluation_id}",
                    "One immutable forecast-evaluation report with provenance and metrics.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/evaluations/{evaluation_id}/verify",
                    "Recompute the stored evaluation report hash; never overwrites the report.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/evaluations/{evaluation_id}/reproduce",
                    "Re-run the stored evaluation configuration and compare its evidence hash.",
                    authenticated=True,
                ),
                # NM-12: Shadow Mode observes live data and records hypothetical
                # decisions. The surface exposes no execution, wallet or ledger route.
                ApiRoute(
                    "GET",
                    "/market/shadow/status",
                    "Shadow session counts and non-execution guarantees.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/shadow/contracts",
                    "Shadow lifecycle, pipeline, projection semantics and safety boundaries.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/shadow/sessions",
                    "Create a non-executing shadow session with an immutable baseline.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/shadow/sessions",
                    "List persisted shadow sessions, newest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/shadow/sessions/{session_id}",
                    "One shadow session with its captured baseline and lifecycle.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/shadow/sessions/{session_id}/history",
                    "Session lifecycle timestamps and record counts.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/shadow/sessions/{session_id}/start",
                    "Capture the baseline and start observing.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/shadow/sessions/{session_id}/pause",
                    "Pause observation without discarding the session.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/shadow/sessions/{session_id}/resume",
                    "Resume a paused shadow session.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/shadow/sessions/{session_id}/stop",
                    "Stop observation while preserving records.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/shadow/sessions/{session_id}/complete",
                    "Complete a shadow session.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/shadow/sessions/{session_id}/cancel",
                    "Cancel a non-completed shadow session.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/shadow/sessions/{session_id}/cycle",
                    "Run one shadow observation cycle against NM-02 data or explicit observations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/shadow/sessions/{session_id}/track-outcomes",
                    "Measure hypothetical outcomes from later market observations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/shadow/sessions/{session_id}/decisions",
                    "Shadow strategy decisions, market references and risk results.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/shadow/sessions/{session_id}/projections",
                    "Hypothetical order projections; never execution records.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/shadow/sessions/{session_id}/outcomes",
                    "Hypothetical outcomes, MFE/MAE and unresolved intervals.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/shadow/sessions/{session_id}/analytics",
                    "Session analytics with explicit small-sample handling.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/shadow/sessions/{session_id}/errors",
                    "Structured data, strategy, risk and cycle failures.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/shadow/sessions/{session_id}/health",
                    "Session freshness, cycle count and recent failures.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/shadow/sessions/{session_id}/compare",
                    "Compare shadow evidence with NM-08/09/10/11 references.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/shadow/sessions/{session_id}/comparisons",
                    "Persisted shadow comparison records, newest first.",
                    authenticated=True,
                ),
                # NM-13: controlled adaptive learning. Evidence, experiments and
                # versions are persisted; no route executes a trade.
                ApiRoute(
                    "GET",
                    "/market/learning/status",
                    "Learning counts, evidence quality and non-execution guarantees.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/learning/contracts",
                    "Evidence kinds, experiment states, governance and limitations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/learning/dashboard",
                    "Active versions, approvals, experiments, alerts and rollback history.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/learning/health",
                    "Learning-system health and open alert count.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/learning/evidence",
                    "Record or import backtest, paper or shadow evidence with provenance.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/learning/evidence",
                    "Read persisted evidence filtered by strategy, kind and status.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/learning/evidence/{evidence_id}",
                    "One evidence record with its eligibility reasons and provenance.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/learning/monitor",
                    "Monitor one strategy by evidence origin and raise deterioration alerts.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/learning/alerts",
                    "Learning alerts, newest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/learning/alerts/{alert_id}/acknowledge",
                    "Acknowledge one learning alert.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/learning/experiments",
                    "Create an idempotent bounded strategy-configuration experiment.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/learning/experiments",
                    "List learning experiments, newest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/learning/experiments/{experiment_id}",
                    "One experiment with its windows, policy and results.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/learning/experiments/{experiment_id}/start",
                    "Start an experiment in the background under the concurrency limit.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/learning/experiments/{experiment_id}/run",
                    "Run an experiment synchronously for deterministic operation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/learning/experiments/{experiment_id}/cancel",
                    "Cancel a created or running experiment.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/learning/experiments/{experiment_id}/candidates",
                    "Candidates generated for one experiment.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/learning/candidates/{candidate_id}",
                    "One candidate with its evaluation, risk and approval state.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/learning/candidates/{candidate_id}/evaluate",
                    "Evaluate one generated candidate against the baseline.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/learning/candidates/{candidate_id}/approve",
                    "Approve a pending candidate after its risk and shadow gates.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/learning/candidates/{candidate_id}/reject",
                    "Reject a candidate with a recorded reason.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/learning/candidates/{candidate_id}/shadow-validate",
                    "Validate an approved candidate through NM-12 Shadow Mode.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/learning/candidates/{candidate_id}/promote",
                    "Promote an approved candidate and record an immutable version.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/learning/strategies/{strategy_id}/versions",
                    "Immutable learning versions and the active pointer.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/market/learning/strategies/{strategy_id}/active-version",
                    "The currently active learning version for one strategy.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/market/learning/strategies/{strategy_id}/rollback",
                    "Roll back to an earlier promoted version with audit history.",
                    authenticated=True,
                ),
                # NovaForge NF-01: a gaming session OBSERVES one validated window.
                # There is no decision or input route here because NF-01 attaches
                # neither — the surface says exactly what the session manager does.
                ApiRoute(
                    "GET",
                    "/novaforge/status",
                    "NovaForge readiness, saved mission draft and the active session.",
                    authenticated=True,
                ),
                # NF-12: discovery and the VERIFIED launch lifecycle. A row here
                # is installation evidence; `tested` is the only field that says
                # a launch actually ran.
                ApiRoute(
                    "GET",
                    "/novaforge/library",
                    "Discovered games with installation evidence, launchability status and this process's own launch claim; query parameter: `q`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/library/resolve",
                    "One game name's catalog status without launching anything; query parameter: `name`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/library/launch",
                    "Preview a verified game launch and mint its approval token, or execute the plan that token approved (`name`/`command`, `approval_token`).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/library/close",
                    "Close only the instance this process started (graceful window close) and confirm it exited.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/library/attach",
                    "Report a game that was already running without taking ownership of it.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/windows",
                    "Open windows for the NovaForge target picker, or why they cannot be read.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/mission",
                    "Read the persisted NovaForge mission draft.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/mission",
                    "Persist a NovaForge mission draft and report its field validation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/validate",
                    "Validate a mission and its live target window without starting a session.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/sessions",
                    "Stored NovaForge sessions, newest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions",
                    "Start a validated NovaForge observation session.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/sessions/{session_id}",
                    "One NovaForge session: state, frames, observations and stages.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/pause",
                    "Pause a running session and wait for the loop to acknowledge.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/resume",
                    "Resume a paused session after re-validating its target window.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/stop",
                    "Cancel a session, release input and persist its final record.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/cancel",
                    "Abort the session loop now, release input and confirm the outcome (idempotent).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/emergency-stop",
                    "Guardian emergency stop: bounded abort, input cleanup and the confirmed result.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/capabilities",
                    "Per-capability availability with the exact blocker: capture, action execution, Guardian enforcement, objective verification, persistence, decision pipeline, vision interpretation, game launch.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/trials",
                    "Policy trial rows and aggregates from runs finished in this process; refused, failed and inconclusive trials are kept.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/observation",
                    "The unified game observation pipeline: bound target, state, measured rate and latency, the last records, pause reason and bounded history.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/observation/start",
                    "Bind one target window and start the bounded observation schedule; body: `title`, optional `game`, `pid`, `start_time`, `process`, `interval`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/observation/pause",
                    "Pause a user-requested observation (never auto-cleared).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/observation/resume",
                    "Resume a user pause after re-reading the pinned target identity.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/observation/stop",
                    "Stop the observation schedule and clean up (idempotent).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/observation/observe",
                    "Capture and validate one frame now; a concurrent capture answers `busy` and starts nothing.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/observation/frame",
                    "Serve the newest validated observation frame (PNG); 404 when nothing was validated.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/state",
                    "The NF-16 game-state reader: last structured state, bounded history, profile table, measured processing latency, counts and refusals.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/state/analyze",
                    "Read one game state from the newest VALIDATED observation; body may pin `profile_id`. A stale, missing or superseded observation is a refusal with a reason code.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/state/reset",
                    "Forget the state comparison baseline (history is kept).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/state/profiles",
                    "The perception profiles this build ships, including rejected ones and why.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/objectives",
                    "The NF-17 objective picture: understood objectives, evidence, bounded history, diagnostics and measured interpretation latency.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/objectives/reset",
                    "Forget the current objectives and the observation-ordering baseline; history and events are kept.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/objectives/profiles",
                    "The objective rules this build ships, including rejected profiles and why.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/knowledge",
                    "NF-18 game knowledge: per-game counts, revalidation due, the transient session memory and why it is not persisted; query parameter: `game`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/knowledge/records",
                    "Knowledge records for ONE game with their evidence and status; query parameters: `game` (required), `category`, `status`, `q`, `limit`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/knowledge",
                    "Record one explicit evidence-bearing knowledge candidate; an invalid category, content or evidence reference is a 422.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/knowledge/{knowledge_id}/revalidate",
                    "Re-check one stale or contradicted record with fresh evidence; a record that needs no revalidation is reported, not changed.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/objective",
                    "Attach a checkable objective (window title) to a live session; unsupported kinds are recorded and reported unsupported.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/actions/plan",
                    "Validate and digest-bind one gameplay action without sending it; a denial carries the Guardian's reason.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/actions/execute",
                    "Execute one digest-bound action proposal after revalidating target identity, foreground, policy and budget.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/sessions/{session_id}/control",
                    "NF-19 control-interface state: bounds, gate decision, held inputs, the last result and why nothing is running.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/control",
                    "Run ONE typed, bounded input action (`kind` plus its parameters); a refusal is a 200 carrying its code, and completion never means the game reacted.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/control/cancel",
                    "Ask the in-flight control action to stop at its next checkpoint and release what it holds.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/control/release",
                    "Release every input the control interface holds (idempotent; refused while an action is in flight).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/sessions/{session_id}/plan",
                    "NF-20 planning state: declared rules, the current plan, its candidates and rationale, bounds and the gate.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/plan",
                    "Build ONE bounded plan from verified state, objective and knowledge; nothing is sent, and a missing or stale reading is a refusal with a code.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/plan/execute",
                    "Execute ONE approved plan step by step through NF-19 (`plan_id`, `approved`, `approval_token` from the plan preview); 403 without the single-use token, stops on refusal, failure, staleness or objective change, and never claims an effect.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/plan/cancel",
                    "Cancel the session's pending or running plan and its in-flight input operation (idempotent).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/plan/replan",
                    "Regenerate a stopped plan from fresh evidence within its bound (`plan_id`, `reason` from the declared triggers); the previous plan is invalidated first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/sessions/{session_id}/outcomes",
                    "NF-21 outcome verification state: declared rules, the last verification run, recorded outcomes, recovery counts and the policy's bounds.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/outcomes/verify",
                    "Verify ONE plan's dispatched steps against fresh post-action observations (`plan_id`, `max_steps`); each step is verified_success, verified_failure, partial, unknown, timed out, aborted or not_verifiable, and missing evidence is never reported as a failure.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/outcomes/recover",
                    "Carry out one outcome's recorded recovery decision (`outcome_id`, `approve`); side-effecting responses need explicit approval, unknown outcomes trigger a fresh bounded observation rather than a repeated action, and no recovery path dispatches input.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/outcomes/cancel",
                    "Ask an in-flight outcome verification to stop at its next checkpoint (idempotent).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/sessions/{session_id}/autonomy",
                    "NF-22 autonomous run state: phase, outcome, pinned objective and completion criteria, current plan and plan-approval status, dispatched actions, last verification, recovery/replan counts, the declared budgets, the resource reading and the sessions a run may drive.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/autonomy/start",
                    "Begin ONE bounded autonomous run over a live session (`approved`, `note`, `task`, `limits`); the operator's approval is the gate, every limit may be narrowed but never widened, and without approval the answer is `approval_required` with nothing started.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/autonomy/pause",
                    "Pause an autonomous run (`reason`); the session pauses and the next cycle re-observes rather than continuing from a pre-pause reading.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/autonomy/resume",
                    "Resume a paused autonomous run; a run that already ended is never restarted.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/autonomy/cancel",
                    "Cancel an autonomous run before its next stage (`reason`) and let it end cleanly, releasing the session's input through the session's own stop path.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/autonomy/approve",
                    "Answer a run parked in WAITING_FOR_APPROVAL (`approved`, `note`); one answer covers exactly one plan and the next plan parks again.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/autonomy/trace",
                    "The complete autonomous orchestration history (query parameter: `session_id`): every phase move with its reason, observation and state references, objective readings, retrieved knowledge references, plans, approvals, execution results, verification evidence and recovery decisions.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/verify",
                    "Run the independent objective verifier and record its verdict and evidence.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/loop",
                    "Run the bounded closed loop once: observe, decide, plan, execute and verify through the session's own gates; query body: `max_actions`, `deadline_seconds`, `verify_first`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/loop/cancel",
                    "Ask a running closed loop to stop before its next action (idempotent).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/sessions/{session_id}/vision/interpret",
                    "Describe the target's current frame with the configured vision model (read-only); returns the typed interpretation, its evidence references, latency and conflicts, or the exact reason no interpretation exists.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/sessions/{session_id}/verification",
                    "The objective, its verdict, evidence references and action counts for one session.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/sessions/{session_id}/telemetry",
                    "Measured telemetry and Guardian status for one session.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/sessions/{session_id}/timeline",
                    "Read-only evidence timeline for one session (filterable, paged).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/sessions/{session_id}/timeline/{seq}",
                    "One timeline event with its evidence resolved.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/timeline/compare",
                    "Compare two persisted sessions from their recorded evidence; query: `left`, `right`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/frames/{session_id}",
                    "Serve one captured frame (PNG); query parameter: `seq`.",
                    authenticated=True,
                ),
                # NF-04: the benchmark lab is a consumer of the session manager.
                # A run is one real observation session plus a persisted result;
                # unsupported gameplay scenarios stay visible and unstartable.
                ApiRoute(
                    "GET",
                    "/novaforge/benchmarks/scenarios",
                    "Benchmark scenario catalog with supported/unsupported status, verifiers and metric definitions.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/benchmarks/scenarios/{scenario_id}",
                    "One benchmark scenario's requirements and definitions; query parameter: `version`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/benchmarks/preflight",
                    "Validate a benchmark scenario and its live target window without starting a run.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/benchmarks/runs",
                    "Persisted benchmark runs, newest first; query parameters: `limit`, `scenario`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/benchmarks/runs",
                    "Start one bounded benchmark observation run.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/benchmarks/runs/{run_id}",
                    "One benchmark run: configuration, metrics, evidence and limitations.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/benchmarks/runs/{run_id}/cancel",
                    "Cancel an active benchmark run through its session's stop path (idempotent).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/benchmarks/compare",
                    "Compare compatible benchmark runs from recorded results; query: `left`, `right`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/benchmarks/aggregate",
                    "Per-scenario aggregate metrics for eligible runs; query: `scenario`, `version`.",
                    authenticated=True,
                ),
                # NF-05: controlled game evaluation. An evaluation is a compatibility
                # record around one NF-04 benchmark run; no route starts a game.
                ApiRoute(
                    "GET",
                    "/novaforge/games",
                    "Game/environment compatibility registry with honest capability counts.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/games/{game_id}",
                    "One environment's compatibility, capabilities and scenarios; query parameter: `version`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/evaluations",
                    "Retained evaluation attempts, newest first; query parameters: `limit`, `game`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/evaluations/baseline",
                    "Current baseline for an environment/scenario; query parameters: `game`, `scenario`, `version`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/evaluations/preflight",
                    "Judge every readiness check against the live window without starting an evaluation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/evaluations",
                    "Start one confirmed evaluation: an observation run, or a controlled plan approved by the digest preflight issued.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/evaluations/{evaluation_id}",
                    "One evaluation: readiness, benchmark run, baseline and status.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/evaluations/{evaluation_id}/cancel",
                    "Cancel an active evaluation through its session's stop path (idempotent).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/evaluations/{evaluation_id}/report",
                    "Per-game evaluation report with evidence links, missing evidence and limitations.",
                    authenticated=True,
                ),
                # NF-06: the Forge Coach analyses recorded evidence and plans ONE
                # declared experiment at a time. Approval is bound to the exact
                # configuration, and nothing here trains, tunes or sends input.
                ApiRoute(
                    "GET",
                    "/novaforge/coach",
                    "Coach capability card: sources, classifications, experiment rules and honest limits.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/coach/analyse",
                    "Evidence-backed analysis of one recorded session or evaluation (read-only; findings are recomputed).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/coach/recommendations",
                    "Persisted recommendation statuses (operator decisions), newest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/coach/recommendations/{recommendation_id}",
                    "Record an operator decision (proposed/approved/rejected/…) about one recommendation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/experiments",
                    "Retained controlled experiments, newest first; query parameters: `limit`, `status`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/experiments",
                    "Propose an experiment from a recommendation or an explicit spec; starts nothing.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/experiments/{experiment_id}",
                    "One experiment: spec, approval, arms, results and history.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/experiments/{experiment_id}/approve",
                    "Approve the CURRENT configuration (its digest is bound to the approval).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/experiments/{experiment_id}/reject",
                    "Reject a proposal; the rejection is retained with its note.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/experiments/{experiment_id}/run",
                    "Run both arms through the evaluation lab (background, bounded, approval-checked).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/novaforge/experiments/{experiment_id}/cancel",
                    "Cancel an experiment and its active arm (idempotent once terminal).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/novaforge/experiments/{experiment_id}/results",
                    "The comparison and verdict, or an explicit not-available-yet answer.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/perception/status",
                    "Perception layer report: providers, budgets, tracking and "
                    "telemetry (no content).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/perception/capabilities",
                    "Perception capability classification with live availability on this machine.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/perception",
                    "Perceive an image, frame list, screen or camera into a structured scene "
                    "(reading only - nothing is executed).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/world/status",
                    "World model report: versions, entities, policies, retention and the "
                    "honest prediction posture (no content).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/world/state",
                    "Bounded, content-light view of the current world state "
                    "(labels, statuses, boxes - no attribute values).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/world/observe",
                    "Ingest one observation (or a batch) and advance the world state - "
                    "recording only, nothing is executed.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/world/query",
                    "Answer a bounded structured question about current or historical "
                    "world state, including evidence, uncertainty and staleness.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/world/predict",
                    "Ask for a future state; reports model_unavailable when no "
                    "predictive model is wired and never fabricates a prediction.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/learning/status",
                    "Interactive learning report: environments, policy and ceilings, "
                    "learned rules, metrics and the honest provider posture.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/learning/environments",
                    "Every environment experiments may run in, with the actions each "
                    "declares and whether it is a simulator.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/learning/environments",
                    "Register a deterministic SIMULATOR declared as data (rules, hidden "
                    "actions, candidate outcomes, failures, delays, partial observations).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/learning/objectives",
                    "Create a bounded learning objective (a question, an environment, "
                    "ceilings). Creating one runs nothing.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/learning/objectives",
                    "The learning objectives this session holds, and which are open.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/learning/objectives/{objective_id}",
                    "One objective: cycle steps, hypotheses with their evidence, "
                    "experiments with their statuses, runs, evidence references and rules.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/learning/objectives/{objective_id}/preview",
                    "Ranked candidate experiments for an objective, with the reason each "
                    "other candidate was refused. Nothing is executed.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/learning/objectives/{objective_id}/run",
                    "Run the bounded learning cycle: observe, hypothesize, select, gate, "
                    "act, compare, update. Refused and interrupted experiments are never "
                    "reported as successes.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/learning/objectives/{objective_id}/approve",
                    "Approve ONE real (non-simulator) experiment. The approval is single use.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/learning/objectives/{objective_id}/reject",
                    "Refuse an objective; the refusal becomes the recorded reason it stopped.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/learning/objectives/{objective_id}/cancel",
                    "Ask the objective's cycle to stop at its next checkpoint.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/learning/objectives/{objective_id}/resumable",
                    "Whether an interrupted objective may be resumed, and why not when it may not.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/learning/objectives/{objective_id}/resume",
                    "Re-run an interrupted objective when the safety preconditions can be "
                    "verified; otherwise it stays interrupted.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/learning/metrics",
                    "Learning metrics: hypotheses moved, agreement over evaluable "
                    "predictions, duplicates avoided, refusals and costs.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/learning/evidence/{evidence_id}",
                    "One evidence reference and the experiment behind it - the "
                    "provenance door for tracing a conclusion.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/learning/transfer",
                    "Check a learned rule against cases the caller held out, with the "
                    "limits of that check stated plainly.",
                    authenticated=True,
                ),
                # Phase 24: goal-level planning. Creating and planning run
                # NOTHING; only /run touches the machine, and only through the
                # existing Phase 4 executor with a fresh-state gate in front of
                # every step.
                ApiRoute(
                    "GET",
                    "/planning/status",
                    "Goal-planning report: goals, planned and verified counts, replan "
                    "budgets, the boot restore report and the capability table with live "
                    "availability (including the honest provider posture).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/planning/goals",
                    "Every goal this session holds, and which have a selected plan.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/planning/goals",
                    "Record a goal, decompose it into validated tasks and produce CANDIDATE "
                    "plans with their comparison and the selected one. Runs nothing.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/planning/goals/{goal_id}",
                    "One goal: decomposition issues, candidate plans, the selected plan, "
                    "the last run's revalidations and completion verdict.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/planning/goals/{goal_id}/run",
                    "Execute the selected plan through the existing executor, re-checking "
                    "each step's preconditions against fresh state and verifying the "
                    "goal's success conditions before reporting completion.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/planning/goals/{goal_id}/reassess",
                    "Whether a goal should be replanned, the deviations seen and whether "
                    "its replan ceiling is exhausted.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/planning/goals/{goal_id}/cancel",
                    "Cancel a goal. Cancelling is not deleting: the record stays readable.",
                    authenticated=True,
                ),
                # Phase 25: embodied sessions and controlled environments.
                # Creating a session runs NOTHING; only /run drives the loop, and
                # only through the ONE input door (session live, environment
                # allowlisted and connected, duration clamped, permission layer
                # asked, result audited). The shipped environment is a simulation
                # this build owns — a real game or window adapter is not shipped.
                ApiRoute(
                    "GET",
                    "/embodied/status",
                    "Embodied sessions: allowlisted environments with live availability, "
                    "scenarios, session counts, ceilings, the input door's scope and the "
                    "capability table with the honest absences.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/embodied/overview",
                    "What Phase 25 ships and what it refuses to claim: the loop, the "
                    "lifecycle vocabularies, the scenarios, the event types and the "
                    "honesty flags.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/embodied/environments",
                    "The environments this build may drive, with their status and the "
                    "capabilities each declares as verified or merely declared.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/embodied/sessions",
                    "Every embodied session this process holds, with its lifecycle state.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/embodied/sessions",
                    "Record a session from an objective and its environments. Runs "
                    "nothing: creating is not starting.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/embodied/sessions/{session_id}",
                    "One session: objective, status and reason, the completion verdict "
                    "with the conditions observed and missing, the bounded trajectory and "
                    "the evaluation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/embodied/sessions/{session_id}/run",
                    "Drive the bounded loop: observe, decide through the goal planner, "
                    "send ONE authorized input, verify it from a fresh reading, then "
                    "recover or replan and report the ending.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/embodied/sessions/{session_id}/pause",
                    "Pause a session; what it holds is released before the status moves.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/embodied/sessions/{session_id}/resume",
                    "Resume a paused session, which returns it as needing revalidation "
                    "before any further input.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/embodied/sessions/{session_id}/cancel",
                    "Cancel a session and release what it holds. Cancelling is not "
                    "deleting: the record stays readable.",
                    authenticated=True,
                ),
                # Phase 26: generalization, ARC-style reasoning and intelligence
                # evaluation. The corpus, the split and the numbers: a run is
                # bounded, a dirty held-out split REFUSES the run, a task list
                # never carries an answer key, and nothing here trains anything
                # or reports a score the per-family metrics did not measure.
                ApiRoute(
                    "GET",
                    "/generalization/status",
                    "What this build can measure about its own generalization: corpus "
                    "and split counts with the leakage verdict, families with their "
                    "verifiers, solvers, baselines, operators, search ceilings, run "
                    "counters, provider posture and the capability table.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/generalization/overview",
                    "What Phase 26 ships and refuses to claim: the evaluation cycle, the "
                    "lifecycle vocabularies, the families, the operator library, the "
                    "verifiers, the failure categories and the honesty flags.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/generalization/families",
                    "The task families, each with its own success condition and "
                    "verifier. No aggregate score: the families are measured in "
                    "different units.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/generalization/operators",
                    "The bounded grid transformation library the search may use, with "
                    "its composition depth ceiling and the rule families it covers.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/generalization/tasks",
                    "The registered tasks matching a filter, WITHOUT their answer keys. "
                    "Query parameters: `family`, `split`, `difficulty`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/generalization/tasks/{task_id}",
                    "One task whole: request, success condition, verifier, split, "
                    "examples and the expected output the evaluator holds.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/generalization/splits",
                    "How the corpus is split, with the three-way leakage verdict: "
                    "overlapping ids, shared answers and examples already used while "
                    "tuning.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/generalization/tasks",
                    "Register one task on this process's corpus. Runs nothing: a task is "
                    "a definition with a success condition and an answer key, not an "
                    "evaluation. 422 when the task is unverifiable or its id is taken.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/generalization/evaluate",
                    "Run one bounded evaluation over the same tasks for candidate and "
                    "baseline, and return the run with its per-family metrics, "
                    "classified failures, frozen settings and outcome fingerprint.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/generalization/runs",
                    "The stored runs as trimmed rows: lifecycle fields and counts only, "
                    "never the per-task result bodies.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/generalization/runs/{run_id}",
                    "One run whole: settings, seed, per-family metrics, classified "
                    "failures with evidence, recommendations and the per-task results.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/generalization/runs/{run_id}/export",
                    "A review payload: the run, the corpus and splits, the operator "
                    "library, the reproduce block and the outcome fingerprint. Schedules "
                    "nothing and promotes nothing.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/generalization/compare",
                    "Two stored runs compared task by task, with neither record "
                    "modified. Query parameters: `baseline`, `candidate`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/generalization/failures",
                    "Classified failures grouped by category. Query parameter: `run` "
                    "(one run, or every stored run without it).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/generalization/recommendations",
                    "Structured, non-executing improvement recommendations from real "
                    "failures. Query parameter: `run`.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/generalization/game-eval/contracts",
                    "The nine evaluation contracts the post-Phase-26 game milestone will "
                    "be measured on. Definitions and sources, not a game.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/generalization/game-eval",
                    "Those contracts measured over the embodied sessions this build "
                    "actually ran, per environment, with unmeasured figures as null.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/intelligence",
                    "Global Intelligence Layer: telemetry, findings, and capabilities.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/capabilities",
                    "Every capability this installation has (declared, tool, action), with "
                    "its availability, risk, permissions, tools, inputs and outputs.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/capabilities/discover",
                    "What capabilities are available for this task? Query parameters: "
                    "`query` (required), `intent`, `category`, `tools`, `models`, "
                    "`include_unavailable`, `limit`. Discovery only - nothing runs.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/bugs",
                    "List recorded bugs (what failed, where, and when).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/bugs/{bug_id}/fix",
                    "Mark a recorded bug as fixed.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/bugs/clear-fixed",
                    "Remove all bugs already marked fixed.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/phone/plan",
                    "Plan an approval-gated phone action.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/phone/execute",
                    "Execute an approved phone action.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/agent/run",
                    "Run the full agentic loop for a natural-language goal.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/agent/metrics",
                    "Agentic evaluation metrics (success, recovery, verification rates).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/agent/knowledge",
                    "Application knowledge graph: workflows, confidence, freshness.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/explore",
                    "Research a topic and return an Explore report.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/explore/trending",
                    "Current daily research topics from live top-story news (rotating window).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/activity",
                    "Recent completed actions (commands, research, learning) for the web timeline.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/events/stream",
                    "Live activity channel: the application EventBus as SSE.",
                ),
                ApiRoute("GET", "/settings", "Read local user settings.", authenticated=True),
                ApiRoute("POST", "/settings", "Update local user settings.", authenticated=True),
                ApiRoute("GET", "/plugins", "List plugin API records.", authenticated=True),
                # -- Phase 13: scheduled work and the audit trail ------------------
                # Scheduling stores a request plus its schedule and runs NOTHING:
                # the stored request still travels intent -> decision -> plan ->
                # permission -> execution when its time comes (or when run now),
                # so "run" here is the same gate a due run passes.
                ApiRoute(
                    "GET",
                    "/automation",
                    "Scheduled automations: what is stored, armed, and due next.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/automation/schedule",
                    "Store a request plus the schedule it states as an automation (nothing runs yet).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/automation/approve",
                    "Authorize a pending automation so it may be armed.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/automation/cancel",
                    "Cancel an automation so it never runs again.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/automation/enable",
                    "Arm an approved automation again.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/automation/disable",
                    "Disarm an automation without cancelling it.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/automation/run",
                    "Run a stored automation now, under the same permission gate as a due run.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/automation/run-due",
                    "Run every automation that is due (the scheduler's own tick).",
                    authenticated=True,
                ),
                # The audit trail is local by construction and redacted on the way
                # in; these routes read and bound it, and nothing here reaches the
                # model or the network.
                ApiRoute(
                    "GET",
                    "/audit",
                    "Operational audit trail status: records, retention, local-only storage, redactions.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/audit/entries",
                    "Recent audit records (redacted), oldest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/audit/prune",
                    "Apply the retention policy and report what was removed.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST", "/audit/delete", "Delete one audit record by id.", authenticated=True
                ),
                ApiRoute(
                    "POST",
                    "/audit/clear",
                    "Delete every audit record (explicit, not retention).",
                    authenticated=True,
                ),
                # Phase 14: the execution mode and privacy controls, the resource
                # governor's reading, the cost estimate, the measured model
                # comparison and the component roster.
                ApiRoute(
                    "GET",
                    "/privacy",
                    "Execution mode, privacy controls and every outbound decision.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/privacy",
                    "Change the execution mode and/or a privacy control, live.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/resources",
                    "The resource governor's reading of this machine, with its reasons.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/cost/estimate",
                    "Estimate a request's cost and routing hint (never a gate).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/diagnostics",
                    "Run the component roster, or a comma-separated subset via ?only=.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/benchmark",
                    "Stored model measurements and the measured comparison per category.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/benchmark",
                    "Measure a model on the given tasks through the live provider.",
                    authenticated=True,
                ),
                # Phase 15: what was recorded, how it was scored, and what it was
                # worth. Read-only on purpose — this surface reports evidence,
                # it never runs a task, calls a model or drops a row.
                ApiRoute(
                    "GET",
                    "/evaluation/summary",
                    "Recorded trajectories, quality verdicts, scores and retention, at a glance.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/evaluation/trajectory/{trajectory_id}",
                    "One stored trajectory with its evaluation and reward breakdown.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/evaluation/metrics",
                    "Aggregate metrics over stored trajectories (success, latency p50/p95, reward, routing).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/evaluation/rewards",
                    "Stored rewards, newest first, with their component and penalty breakdowns.",
                    authenticated=True,
                ),
                # Phase 16: supervised fine-tuning. Datasets are built from what
                # Phase 15 recorded; runs are started, paused, resumed and
                # cancelled explicitly; a trained adapter only ever becomes a
                # candidate until a human approves and promotes it.
                ApiRoute(
                    "GET",
                    "/training/status",
                    "Training subsystem state: enabled, dry-run, backend, capabilities.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/training/summary",
                    "Datasets, runs, checkpoints and models, at a glance.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/training/estimate",
                    "Estimate a training config's resources without starting anything.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/training/datasets",
                    "Stored fine-tuning dataset versions, newest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/training/datasets",
                    "Build a dataset version from stored trajectories, deterministically split.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/training/datasets/validate",
                    "Validate a dataset version: leakage, duplicates, empty splits.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/training/datasets/{dataset_version_id}",
                    "One dataset version with its split counts and provenance.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/training/runs",
                    "Training runs, newest first, with their status and resource verdict.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/training/runs",
                    "Create a training run from a config (never starts it).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/training/runs/start",
                    "Start a run: refused when the estimate is unsafe, real training needs confirmation.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/training/runs/pause",
                    "Pause a running training run at the next checkpoint boundary.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/training/runs/cancel",
                    "Cancel a run; its checkpoints are kept for inspection.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/training/runs/resume",
                    "Resume an interrupted run from its newest valid checkpoint.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/training/runs/re-estimate",
                    "Re-estimate a run's resources against the current machine reading.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/training/runs/evaluate",
                    "Compare a run's candidate against its base model and record the verdict.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/training/runs/{run_id}/checkpoints",
                    "A run's checkpoints with their validity (complete, partial, corrupt).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/training/runs/{run_id}",
                    "One training run: config, metrics, progress and checkpoint list.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/training/evaluations",
                    "Recorded before/after evaluations with their regression checks.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/training/models",
                    "Fine-tuned candidates and registered models, with their lifecycle status.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/training/models/{model_id}",
                    "One registered model: adapter metadata, lineage and evaluation history.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/training/models/approve",
                    "Approve a candidate that has a recorded passing evaluation (explicit only).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/training/models/promote",
                    "Promote an approved model to production, demoting the previous one.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/training/models/reject",
                    "Reject a candidate with a recorded reason.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/training/models/deprecate",
                    "Deprecate a registered model so it is no longer selected.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/training/models/rollback",
                    "Roll a deployment back to the model a promotion replaced.",
                    authenticated=True,
                ),
                # Phase 17: preference optimization. Pairs are built from what
                # Phase 15 recorded or submitted by a reviewer; a DPO/ORPO run is
                # created, priced, dry-run and started explicitly; and a
                # preference model is registered in the SAME registry, so
                # approving and promoting it use the /training/models routes.
                ApiRoute(
                    "GET",
                    "/preference/status",
                    "Preference subsystem state: datasets, runs, reviews, readiness.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/preference/summary",
                    "Datasets, runs, reviews and objectives, at a glance.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/preference/algorithms",
                    "The DPO and ORPO objectives, what each costs, and readiness here.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/preference/estimate",
                    "Estimate a preference config's resources without starting anything.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/preference/dry-run",
                    "Validate a dataset, config and output directory, and price the run — starting nothing.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/preference/datasets",
                    "Stored preference dataset versions, newest first.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/preference/datasets",
                    "Build a preference dataset version from observed behaviour, deterministically split.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/preference/datasets/validate",
                    "Validate a preference dataset: leakage, contradictions, provenance, quality.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/preference/datasets/{dataset_version_id}",
                    "One preference dataset version with its splits and statistics.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/preference/datasets/{dataset_version_id}/pairs/{preference_id}",
                    "One preference pair: both candidates, evidence, outcomes and split.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/preference/reviews",
                    "Pairs waiting on a human reviewer, with both candidates side by side.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/preference/reviews/{preference_id}",
                    "One queued pair as a reviewer sees it.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/preference/reviews/submit",
                    "Submit a preference a person is asserting (choose A/B, or record a pair).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/preference/reviews/decide",
                    "Settle a queued pair: choose A, choose B, mark a tie, or reject it.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/preference/runs",
                    "Preference runs, newest first, filtered by objective.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/preference/runs",
                    "Create a DPO/ORPO run from a config (never starts it).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/preference/runs/start",
                    "Start a preference run: unsafe estimates and unconfirmed real runs are refused.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/preference/runs/pause",
                    "Pause a running preference run at the next step boundary.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/preference/runs/cancel",
                    "Cancel a preference run; its checkpoints are kept.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/preference/runs/resume",
                    "Resume an interrupted preference run from its newest valid checkpoint.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/preference/runs/re-estimate",
                    "Re-estimate a run's resources against the current machine reading.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/preference/runs/evaluate",
                    "Compare base, SFT and candidate on the held-out pairs and record the verdict.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/preference/compare",
                    "Compare three models on a pair dataset without needing a run.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/preference/runs/{run_id}/checkpoints",
                    "A preference run's checkpoints with their validity.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/preference/runs/{run_id}",
                    "One preference run: algorithm, config, metrics and checkpoints.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/preference/evaluations",
                    "Recorded preference comparisons with their regression checks.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/preference/models",
                    "Registered preference models, filtered by objective (same registry as training).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/preference/models/{model_id}",
                    "One registered preference model, with the objective that produced it.",
                    authenticated=True,
                ),
                # Phase 18: RLHF / RLAIF. Feedback (human and AI) becomes a
                # reward signal, datasets are built from it with integrity
                # checks, and an RL run is created, priced, simulated and
                # started explicitly. An RL model registers in the SAME
                # registry, so approving and promoting use /training/models.
                ApiRoute(
                    "GET",
                    "/rlhf/status",
                    "RLHF/RLAIF state: feedback, ratings, datasets, runs, readiness.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlhf/summary",
                    "Feedback, ratings, datasets and runs, at a glance.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlhf/algorithms",
                    "The RL modes and policy optimizers, and what this machine can do.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlhf/estimate",
                    "Estimate an RL config's resources without starting anything.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlhf/dry-run",
                    "Validate, price, plan and simulate an RL run — starting nothing.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlhf/pipeline",
                    "The stage-by-stage RLHF/RLAIF plan for a configuration and dataset.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlhf/feedback",
                    "Human feedback rows, newest first, with their verdicts.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlhf/feedback",
                    "Submit human feedback for a trajectory (never deletes; quality-filtered).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlhf/feedback/{feedback_id}/decide",
                    "Settle a held feedback row: accept keeps it usable, reject does not.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlhf/rate",
                    "Ask an evaluator for a structured rating of observable facts.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlhf/ratings",
                    "Stored AI ratings, newest first, with the source breakdown.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlhf/disagreements",
                    "Recorded human-vs-AI disagreements, optionally detecting new ones.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlhf/datasets",
                    "Reward dataset versions, newest first, filtered by mode or name.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlhf/datasets",
                    "Build a reward dataset version from feedback and ratings, split deterministically.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlhf/datasets/{dataset_version_id}/validate",
                    "Validate a reward dataset: provenance, integrity, splits and leakage.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlhf/datasets/{dataset_version_id}/held",
                    "Rows a dataset held back, and why they were excluded.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlhf/datasets/{dataset_version_id}",
                    "One reward dataset version with its splits and statistics.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlhf/runs",
                    "RLHF/RLAIF runs, newest first, filtered by mode or status.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlhf/runs",
                    "Create an RL run from a config (never starts it; rewards are audited first).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlhf/runs/start",
                    "Start an RL run; unsafe estimates and unconfirmed real runs are refused.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlhf/runs/pause",
                    "Pause a running RL run at the next step boundary.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlhf/runs/cancel",
                    "Cancel an RL run; its checkpoints are kept.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlhf/runs/resume",
                    "Resume an interrupted RL run from its newest valid checkpoint.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlhf/runs/re-estimate",
                    "Re-estimate a run's resources against the current machine reading.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlhf/runs/evaluate",
                    "Compare base, SFT, preference and RL candidate on held-out data.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlhf/compare",
                    "Compare up to four models on a reward dataset without needing a run.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlhf/runs/{run_id}/checkpoints",
                    "An RL run's checkpoints with their validity and loadability.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlhf/runs/{run_id}",
                    "One RL run: mode, algorithm, config, metrics and checkpoints.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlhf/evaluations",
                    "Recorded RL comparisons with their regression checks.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlhf/models",
                    "Registered RL models, filtered by mode (same registry as training).",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlhf/models/{model_id}",
                    "One registered RL model, with the mode that produced it.",
                    authenticated=True,
                ),
                # Phase 19: RLVR + critique-based learning. Verifiable rewards
                # come from registered deterministic verifiers, failures become
                # structured critiques, corrections become datasets, and an
                # RLVR run is created, priced, planned and started explicitly.
                # An RLVR model registers in the SAME registry, so approving
                # and promoting use /training/models.
                ApiRoute(
                    "GET",
                    "/rlvr/status",
                    "RLVR state: verifiers, critiques, corrections, datasets, runs.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlvr/summary",
                    "The same, plus the newest critiques, corrections and datasets.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlvr/verifiers",
                    "Registered verifiers with categories, versions and integrity state.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlvr/verifiers/disable",
                    "Stop a verifier supporting rewards (tamper protection: disable, never edit).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlvr/verifiers/enable",
                    "Let a disabled verifier support rewards again.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlvr/verify",
                    "Verify checkable questions; expectations are frozen before each check.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlvr/reward",
                    "Turn verification results into a reward, with its integrity audit.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlvr/critiques",
                    "Generate structured critiques from recorded evidence and store them.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlvr/critiques",
                    "Stored critiques, newest first, filtered by category and severity.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlvr/corrections",
                    "Corrected examples and their verdicts; held rows are reviewable.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlvr/corrections",
                    "Propose corrections for stored critiques; unverified ones are held.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlvr/datasets",
                    "Critique dataset versions, newest first, filtered by name.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlvr/datasets",
                    "Build an immutable critique dataset version from critiques and corrections.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlvr/datasets/{dataset_version_id}/validate",
                    "Whether a critique dataset can train anything, and what is missing.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlvr/datasets/{dataset_version_id}/held",
                    "Corrections a dataset held back, so a person can settle them.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlvr/datasets/{dataset_version_id}/pairs",
                    "The Phase 17 preference pairs a critique dataset yields.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlvr/datasets/{dataset_version_id}",
                    "One critique dataset version with its splits and statistics.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlvr/estimate",
                    "Estimate an RLVR config's resources without starting anything.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlvr/pipeline",
                    "The ten-stage RLVR plan for a configuration and task set.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlvr/dry-run",
                    "Walk all ten RLVR stages on deterministic inputs; starts nothing.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlvr/evaluate",
                    "Verifier-side metrics against labels recorded before the call.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlvr/runs",
                    "RLVR runs, newest first, filtered by status.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlvr/runs",
                    "Create an RLVR run from a critique dataset (never starts it).",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlvr/runs/start",
                    "Start an RLVR run; unsafe estimates and unconfirmed real runs are refused.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlvr/runs/pause",
                    "Pause a running RLVR run at the next step boundary.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlvr/runs/cancel",
                    "Cancel an RLVR run; its checkpoints are kept.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlvr/runs/resume",
                    "Resume an interrupted RLVR run from its newest valid checkpoint.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlvr/runs/re-estimate",
                    "Re-estimate a run's resources against the current machine reading.",
                    authenticated=True,
                ),
                ApiRoute(
                    "POST",
                    "/rlvr/runs/evaluate",
                    "Compare base, SFT and preference models against the RLVR candidate.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlvr/runs/{run_id}/checkpoints",
                    "An RLVR run's checkpoints with their validity and loadability.",
                    authenticated=True,
                ),
                ApiRoute(
                    "GET",
                    "/rlvr/runs/{run_id}",
                    "One RLVR run: configuration, metrics and verifier snapshot.",
                    authenticated=True,
                ),
            ),
            websocket_paths=("/ws/events",),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "routes": [route.to_dict() for route in self.routes],
            "websocket_paths": self.websocket_paths,
        }
