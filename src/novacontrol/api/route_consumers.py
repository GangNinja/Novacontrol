"""Shared registry: which frontend consumer owns each ApiSurface route.

Single source of truth for the route-to-consumer contract, extracted from
``tests/test_web_platform.py`` so multiple audiences share one table:

  * the backend/frontend contract test (imported directly),
  * ``scripts/generate_api_reference.py``, which regenerates the REST section
    of ``docs/API.md`` (with ``--check`` for CI),
  * the generated ``docs/API.md`` itself (release checklist depends on it).

The registry lives beside ``ApiSurface`` (the wire contract) so the route
list, its wire contract, and the frontend ownership map stay synchronized:
add a route to ``ApiSurface`` and the contract test fails until it gets a
consumer here, and the docs generator keeps the human-readable reference in
sync for free.
"""

from __future__ import annotations

# Route -> frontend consumer that owns the response body, or one of the
# NON_RENDER_ROLES markers:
#   no-render     - infrastructure / channel / CLI-only endpoint, no panel
#   load-settings - GET /settings fills the settings form, no card render
# Every other value names a render-panels.js / render-utils.js renderer.
ROUTE_CONSUMERS: dict[tuple[str, str], str] = {
    # NovaMarket NM-03: NovaCoin wallets and the deterministic ledger. There is
    # no wallet panel yet (the workspace is deferred past NovaControl Phase 39),
    # so these are API/CLI surfaces rather than renderer-owned bodies.
    ("GET", "/market/wallet/status"): "no-render",
    ("GET", "/market/wallets"): "no-render",
    ("POST", "/market/wallets"): "no-render",
    ("POST", "/market/wallets/transfer"): "no-render",
    ("GET", "/market/wallets/{wallet_id}"): "no-render",
    ("GET", "/market/wallets/{wallet_id}/transactions"): "no-render",
    ("GET", "/market/wallets/{wallet_id}/ledger"): "no-render",
    ("POST", "/market/wallets/{wallet_id}/deposit"): "no-render",
    ("POST", "/market/wallets/{wallet_id}/withdraw"): "no-render",
    ("POST", "/market/wallets/{wallet_id}/status"): "no-render",
    ("GET", "/market/wallets/{wallet_id}/reconcile"): "no-render",
    ("POST", "/market/wallet/reconcile"): "no-render",
    ("POST", "/market/wallet/reversals"): "no-render",
    # Rendered panels (run(type) -> renderer dispatch, render-utils.js).
    ("POST", "/ask"): "renderChatResult",  # envelope branches by route/intent
    ("POST", "/brain/decide"): "no-render",  # routing-explorer.js renders the trace itself
    ("POST", "/explore"): "renderExplore",
    (
        "GET",
        "/explore/trending",
    ): "no-render",  # example chips fetched by js/routing-explorer.js sibling app.js loader, not run()
    ("POST", "/command/plan"): "renderCommand",
    ("POST", "/command/execute"): "renderCommand",
    ("POST", "/desktop/plan"): "renderCommand",
    ("POST", "/desktop/execute"): "renderCommand",
    ("POST", "/phone/plan"): "renderCommand",
    ("POST", "/phone/execute"): "renderCommand",
    ("POST", "/browser/plan"): "renderCommand",
    ("POST", "/browser/execute"): "renderCommand",
    ("POST", "/plan"): "renderBuild",
    # The agent-loop run has no panel yet: API callers (and the CLI) consume the
    # phase trace, the plan and the unverified list directly, and claiming a
    # renderer here would put a control in the contract that does not exist.
    ("POST", "/plan/run"): "no-render",
    ("POST", "/plan/code"): "renderBuild",
    ("POST", "/knowledge/teach"): "renderLearning",
    ("GET", "/knowledge"): "renderLearning",
    ("POST", "/knowledge/recall"): "renderLearning",
    ("POST", "/improve/workflow"): "renderWorkflow",
    ("POST", "/improve/preview"): "renderWorkflow",
    ("POST", "/improve/approve"): "renderWorkflow",
    ("POST", "/learn"): "renderLearning",
    ("POST", "/train"): "renderLearning",
    ("GET", "/system/health"): "renderHealth",
    # Command Center telemetry has its own poll loop and in-place card updater
    # (js/telemetry.js) rather than going through run(type) -> render().
    ("GET", "/system/telemetry"): "renderTelemetry",
    ("GET", "/system/harden"): "renderHealth",
    ("GET", "/system/package"): "renderGeneric",  # explicit generic fallback
    ("GET", "/status"): "renderGeneric",  # home metrics + generic fallback card
    ("POST", "/settings"): "renderSettingsResult",
    # Enumerated non-render consumers.
    ("POST", "/brain/mode"): "no-render",  # switch handler; refreshStatus re-renders
    ("GET", "/brain/mode"): "no-render",  # syncBrainSwitch reads the persisted mode
    ("GET", "/brain/cloud/presets"): "no-render",  # cloud card provider picker
    ("POST", "/brain/cloud"): "no-render",  # connectCloudLlm + refreshStatus
    ("POST", "/brain/cloud/test"): "no-render",  # testCloudLlm button + status line
    ("POST", "/brain/cloud/clear"): "no-render",  # removeCloudLlm + refreshStatus
    ("GET", "/brain/ollama/models"): "no-render",  # brain model picker list
    ("POST", "/brain/local/model"): "no-render",  # picker change handler; refreshStatus re-renders
    ("POST", "/chat/clear"): "no-render",  # clearChatButton wipes the shared thread
    ("GET", "/chat/history"): "no-render",  # renderChatHistory seeds the shared thread
    ("POST", "/chat/history"): "no-render",  # recordChatTurn append + one-time migrate
    ("POST", "/build/save"): "no-render",  # saveBuildArtifact button + toast
    ("POST", "/build/project"): "no-render",  # Build panel drafts a project into its own file list
    ("GET", "/build/artifacts"): "no-render",  # Build panel's Run list of saved artifacts
    ("POST", "/build/run"): "no-render",  # Build panel re-runs a saved artifact + output pane
    ("GET", "/models/status"): "no-render",  # no panel: model-manager status (CLI/API)
    ("POST", "/models/load"): "no-render",  # no panel: load a model (CLI/API)
    ("POST", "/models/unload"): "no-render",  # no panel: release a model (CLI/API)
    ("GET", "/tasks"): "no-render",  # reserved for future explicit lists
    ("POST", "/tasks/delete"): "no-render",  # deleteTask row button + refreshStatus
    ("POST", "/tasks/clear"): "no-render",  # clearAllTasksButton + toast-undo
    ("POST", "/tasks/clear/undo"): "no-render",  # toast Undo button + refreshStatus
    ("GET", "/settings"): "load-settings",
    ("GET", "/events/stream"): "no-render",  # EventSource activity channel
    ("GET", "/"): "no-render",  # served HTML (index.html)
    ("GET", "/health"): "no-render",  # infrastructure probe
    ("POST", "/improve"): "no-render",  # raw plan API (CLI/demos only)
    ("GET", "/phone/status"): "no-render",  # bridge status (CLI/demos only)
    (
        "POST",
        "/phone/connect",
    ): "renderPhoneStatus",  # Connect Phone button - pairing flow, bridge-shaped result
    ("GET", "/vision/status"): "no-render",  # Vision panel toast-only status check
    ("POST", "/vision/model"): "no-render",  # vision model card connect button; refreshes card
    ("POST", "/vision/model/clear"): "no-render",  # vision model card remove button; refreshes card
    ("POST", "/vision/describe"): "renderGeneric",  # Vision panel Describe Screen
    ("POST", "/vision/click"): "renderGeneric",  # Vision panel guided click
    # NovaMarket NM-01 is managed programmatically; the final workspace/UI is
    # deferred until after NovaControl Phase 39, so these own no panel yet.
    ("GET", "/market/status"): "no-render",
    ("GET", "/market/overview"): "no-render",  # standalone market-workspace.js
    ("GET", "/market/contracts"): "no-render",
    ("GET", "/market/config"): "no-render",
    ("POST", "/market/config"): "no-render",
    ("POST", "/market/notifications/test"): "no-render",  # settings card shows the toast
    ("POST", "/market/universe/sync"): "no-render",  # settings card shows the count
    ("POST", "/market/enable"): "no-render",
    ("POST", "/market/disable"): "no-render",
    # AT-01 autonomy shell is rendered by the standalone React Autonomy page.
    ("GET", "/market/autonomy/contracts"): "no-render",
    ("GET", "/market/autonomy/status"): "no-render",
    ("GET", "/market/autonomy/health"): "no-render",
    ("GET", "/market/autonomy/reliability"): "no-render",
    ("POST", "/market/autonomy/recover"): "no-render",
    ("POST", "/market/autonomy/start"): "no-render",
    ("POST", "/market/autonomy/pause"): "no-render",
    ("POST", "/market/autonomy/resume"): "no-render",
    ("POST", "/market/autonomy/stop"): "no-render",
    # AT-02 session/monitoring reads and the operator-gated cycle controls are
    # rendered by the same standalone React Autonomy page.
    ("GET", "/market/autonomy/session"): "no-render",
    ("GET", "/market/autonomy/monitoring/contracts"): "no-render",
    ("GET", "/market/autonomy/monitoring"): "no-render",
    ("GET", "/market/autonomy/monitoring/cycles"): "no-render",
    ("GET", "/market/autonomy/monitoring/cycles/{cycle_id}"): "no-render",
    ("POST", "/market/autonomy/monitoring/run"): "no-render",
    ("POST", "/market/autonomy/monitoring/config"): "no-render",
    # AT-03 decisions and NM-08 risk assessment are rendered by the standalone
    # React Autonomy page; an approved proposal is never shown as a trade.
    ("GET", "/market/autonomy/decisions/contracts"): "no-render",
    ("GET", "/market/autonomy/decisions/status"): "no-render",
    ("GET", "/market/autonomy/decisions/runs"): "no-render",
    ("GET", "/market/autonomy/decisions/runs/{run_id}"): "no-render",
    ("GET", "/market/autonomy/decisions/signals"): "no-render",
    ("GET", "/market/autonomy/decisions/proposals"): "no-render",
    ("GET", "/market/autonomy/decisions/proposals/{proposal_id}"): "no-render",
    ("GET", "/market/autonomy/decisions/predictions"): "no-render",
    ("GET", "/market/autonomy/decisions/predictions/{decision_id}"): "no-render",
    ("GET", "/market/autonomy/decisions/outcomes"): "no-render",
    ("GET", "/market/autonomy/decisions/forecast-policy"): "no-render",
    ("PUT", "/market/autonomy/decisions/forecast-policy"): "no-render",
    ("POST", "/market/autonomy/decisions/outcomes/run"): "no-render",
    ("POST", "/market/autonomy/decisions/run"): "no-render",
    ("GET", "/market/autonomy/executions/contracts"): "no-render",
    ("GET", "/market/autonomy/executions/status"): "no-render",
    ("GET", "/market/autonomy/executions"): "no-render",
    ("GET", "/market/autonomy/executions/failures"): "no-render",
    ("GET", "/market/autonomy/executions/{execution_id}"): "no-render",
    ("GET", "/market/autonomy/executions/{execution_id}/events"): "no-render",
    ("POST", "/market/autonomy/executions/run"): "no-render",
    ("POST", "/market/autonomy/executions/{execution_id}/reconcile"): "no-render",
    # AT-05 protective exits are rendered by the standalone React Autonomy page.
    ("GET", "/market/autonomy/exits/contracts"): "no-render",
    ("GET", "/market/autonomy/exits/status"): "no-render",
    ("GET", "/market/autonomy/exits/positions"): "no-render",
    ("GET", "/market/autonomy/exits/configurations"): "no-render",
    ("PUT", "/market/autonomy/exits/configurations/{portfolio_id}/{instrument_id}"): "no-render",
    ("DELETE", "/market/autonomy/exits/configurations/{portfolio_id}/{instrument_id}"): "no-render",
    ("GET", "/market/autonomy/exits/decisions"): "no-render",
    ("GET", "/market/autonomy/exits/decisions/{decision_id}"): "no-render",
    ("GET", "/market/autonomy/exits/decisions/{decision_id}/events"): "no-render",
    ("GET", "/market/autonomy/exits/learning"): "no-render",
    ("POST", "/market/autonomy/exits/run"): "no-render",
    ("POST", "/market/autonomy/exits/decisions/{decision_id}/reconcile"): "no-render",
    ("GET", "/market/workspace"): "no-render",  # standalone marketplace HTML shell
    # NM-02 market data: programmatic reads and collection. The workspace that
    # renders them is deferred, so no client consumes these yet.
    ("GET", "/market/data/status"): "no-render",
    ("GET", "/market/data/providers"): "no-render",
    ("GET", "/market/instruments"): "no-render",
    ("POST", "/market/instruments"): "no-render",
    ("POST", "/market/instruments/lookup"): "no-render",
    ("POST", "/market/data/ingest"): "no-render",
    ("GET", "/market/data/quote/{instrument_id}"): "no-render",
    ("GET", "/market/data/history/{instrument_id}"): "no-render",
    ("GET", "/market/data/bars/{instrument_id}"): "no-render",
    ("GET", "/market/screener"): "no-render",
    # NM-14 workbenches are rendered by the standalone React app
    # (web/src/pages/OrderBlock.tsx and TradingWatch.tsx), not the legacy panel.
    ("GET", "/market/orderblock"): "no-render",
    ("GET", "/market/orderblock/{instrument_id}"): "no-render",
    ("GET", "/market/tradingwatch"): "no-render",
    ("GET", "/market/tradingwatch/watchlists"): "no-render",
    ("GET", "/market/tradingwatch/replay/{instrument_id}"): "no-render",
    ("POST", "/market/watchlists/{watchlist_id}/rename"): "no-render",
    # NovaForge has its own panel module (js/novaforge.js) that polls the session
    # and updates in place — it never goes through run(type) -> render(), so each
    # route is enumerated as no-render rather than claiming a renderer it lacks.
    ("GET", "/novaforge/status"): "no-render",  # panel bootstrap/refresh
    ("GET", "/novaforge/windows"): "no-render",  # target picker list
    # NF-12: Game Library tab. Discovery, the launch preview, the approve step
    # and the graceful close all repaint the tab in place.
    ("GET", "/novaforge/library"): "no-render",  # library list + counts
    ("GET", "/novaforge/library/resolve"): "no-render",  # status answer card
    ("POST", "/novaforge/library/launch"): "no-render",  # Launch Game preview + approve
    ("POST", "/novaforge/library/close"): "no-render",  # close the test-launched game
    ("POST", "/novaforge/library/attach"): "no-render",  # attach to a running game
    ("GET", "/novaforge/mission"): "no-render",  # fills the mission form
    ("POST", "/novaforge/mission"): "no-render",  # Save Mission button + field errors
    ("POST", "/novaforge/validate"): "no-render",  # Validate button + readiness card
    ("GET", "/novaforge/capabilities"): "no-render",  # readiness card + Guardian tab
    ("GET", "/novaforge/trials"): "no-render",  # Guardian tab trial history + metrics
    # NF-15: the observation card in Mission Control owns these responses and
    # repaints in place, like the rest of js/novaforge.js.
    ("GET", "/novaforge/observation"): "no-render",  # observation card poll
    ("POST", "/novaforge/observation/start"): "no-render",  # Start Observable
    ("POST", "/novaforge/observation/pause"): "no-render",  # Pause observation
    ("POST", "/novaforge/observation/resume"): "no-render",  # Resume observation
    ("POST", "/novaforge/observation/stop"): "no-render",  # Stop observation
    ("POST", "/novaforge/observation/observe"): "no-render",  # Observe Once
    ("GET", "/novaforge/observation/frame"): "no-render",  # validated frame <img>
    ("GET", "/novaforge/state"): "no-render",  # game-state card poll
    ("POST", "/novaforge/state/analyze"): "no-render",  # Analyze Once button
    ("POST", "/novaforge/state/reset"): "no-render",  # Reset Baseline button
    ("GET", "/novaforge/state/profiles"): "no-render",  # profiles table
    ("GET", "/novaforge/objectives"): "no-render",  # objective card poll
    ("POST", "/novaforge/objectives/reset"): "no-render",  # Reset Objectives button
    ("GET", "/novaforge/objectives/profiles"): "no-render",  # objective rules table
    # NF-18: the game-knowledge card in the NovaForge panel owns these responses
    # and repaints in place; it shows evidence and status, never a strategy.
    ("GET", "/novaforge/knowledge"): "no-render",  # knowledge card poll
    ("GET", "/novaforge/knowledge/records"): "no-render",  # record list
    ("POST", "/novaforge/knowledge"): "no-render",  # explicit record (API/CLI)
    ("POST", "/novaforge/knowledge/{knowledge_id}/revalidate"): "no-render",  # revalidate
    ("POST", "/novaforge/sessions/{session_id}/objective"): "no-render",  # objective form
    ("POST", "/novaforge/sessions/{session_id}/actions/plan"): "no-render",  # Plan Action button
    ("POST", "/novaforge/sessions/{session_id}/actions/execute"): "no-render",  # Run Action button
    # NF-19: the control card polls its own state and its buttons repaint in
    # place; an action result is rendered from the POST body, never re-fetched.
    ("GET", "/novaforge/sessions/{session_id}/control"): "no-render",  # control card poll
    ("POST", "/novaforge/sessions/{session_id}/control"): "no-render",  # one control action request
    ("POST", "/novaforge/sessions/{session_id}/control/cancel"): "no-render",  # Cancel action
    ("POST", "/novaforge/sessions/{session_id}/control/release"): "no-render",  # Release inputs
    ("GET", "/novaforge/sessions/{session_id}/plan"): "no-render",  # plan card poll
    ("POST", "/novaforge/sessions/{session_id}/plan"): "no-render",  # Plan Next Actions
    ("POST", "/novaforge/sessions/{session_id}/plan/execute"): "no-render",  # Execute Approved Plan
    ("POST", "/novaforge/sessions/{session_id}/plan/cancel"): "no-render",  # Cancel Plan
    ("POST", "/novaforge/sessions/{session_id}/plan/replan"): "no-render",  # Replan
    # NF-21: outcomes are read back from the service and the session timeline; the
    # API is the caller here (no panel renders these responses yet), so they are
    # declared no-render with the rest of the panel-internal surface.
    ("GET", "/novaforge/sessions/{session_id}/outcomes"): "no-render",  # outcome state read
    (
        "POST",
        "/novaforge/sessions/{session_id}/outcomes/verify",
    ): "no-render",  # verify a plan's steps
    (
        "POST",
        "/novaforge/sessions/{session_id}/outcomes/recover",
    ): "no-render",  # apply one recovery
    (
        "POST",
        "/novaforge/sessions/{session_id}/outcomes/cancel",
    ): "no-render",  # cancel verification
    # NF-22: the autonomous-run card (JARVIS panel) polls its own state and its
    # buttons repaint in place, so the panel renders these responses itself rather
    # than through the generic result renderer.
    ("GET", "/novaforge/sessions/{session_id}/autonomy"): "no-render",  # autonomy card poll
    (
        "POST",
        "/novaforge/sessions/{session_id}/autonomy/start",
    ): "no-render",  # Start Autonomous Run
    ("POST", "/novaforge/sessions/{session_id}/autonomy/pause"): "no-render",  # Pause Run button
    ("POST", "/novaforge/sessions/{session_id}/autonomy/resume"): "no-render",  # Resume Run button
    ("POST", "/novaforge/sessions/{session_id}/autonomy/cancel"): "no-render",  # Cancel Run button
    ("POST", "/novaforge/sessions/{session_id}/autonomy/approve"): "no-render",  # per-plan approval
    ("GET", "/novaforge/autonomy/trace"): "no-render",  # trace view
    ("POST", "/novaforge/sessions/{session_id}/verify"): "no-render",  # Verify button
    ("POST", "/novaforge/sessions/{session_id}/loop"): "no-render",  # Run Closed Loop button
    ("POST", "/novaforge/sessions/{session_id}/loop/cancel"): "no-render",  # Stop Loop button
    (
        "POST",
        "/novaforge/sessions/{session_id}/vision/interpret",
    ): "no-render",  # Describe Frame button
    ("GET", "/novaforge/sessions/{session_id}/verification"): "no-render",  # verdict card
    ("GET", "/novaforge/sessions"): "no-render",  # session history list
    ("POST", "/novaforge/sessions"): "no-render",  # Start button
    ("GET", "/novaforge/sessions/{session_id}"): "no-render",  # session poll + history row
    ("POST", "/novaforge/sessions/{session_id}/pause"): "no-render",  # Pause button
    ("POST", "/novaforge/sessions/{session_id}/resume"): "no-render",  # Resume button
    ("POST", "/novaforge/sessions/{session_id}/stop"): "no-render",  # Stop button
    ("POST", "/novaforge/sessions/{session_id}/cancel"): "no-render",  # Cancel button
    ("POST", "/novaforge/sessions/{session_id}/emergency-stop"): "no-render",  # Guardian tab
    ("GET", "/novaforge/sessions/{session_id}/telemetry"): "no-render",  # Telemetry tab
    ("GET", "/novaforge/sessions/{session_id}/timeline"): "no-render",  # Timeline tab
    ("GET", "/novaforge/sessions/{session_id}/timeline/{seq}"): "no-render",  # event detail
    ("GET", "/novaforge/timeline/compare"): "no-render",  # comparison card
    ("GET", "/novaforge/frames/{session_id}"): "no-render",  # frame <img> source
    # NF-04 Benchmark Lab: js/novaforge.js owns these responses and renders them
    # in the Benchmark tab (catalog, preflight, run detail, history, comparison).
    ("GET", "/novaforge/benchmarks/scenarios"): "no-render",  # scenario catalog
    ("GET", "/novaforge/benchmarks/scenarios/{scenario_id}"): "no-render",  # scenario detail
    ("POST", "/novaforge/benchmarks/preflight"): "no-render",  # preflight card
    ("GET", "/novaforge/benchmarks/runs"): "no-render",  # run history list
    ("POST", "/novaforge/benchmarks/runs"): "no-render",  # Start Benchmark button
    ("GET", "/novaforge/benchmarks/runs/{run_id}"): "no-render",  # progress poll + run detail
    ("POST", "/novaforge/benchmarks/runs/{run_id}/cancel"): "no-render",  # Cancel button
    ("GET", "/novaforge/benchmarks/compare"): "no-render",  # comparison card
    ("GET", "/novaforge/benchmarks/aggregate"): "no-render",  # aggregate card
    # NF-05 Controlled Game Evaluation: js/novaforge.js renders the registry,
    # preflight checklist, live evaluation and report in the Evaluation tab.
    ("GET", "/novaforge/games"): "no-render",  # environment registry picker
    ("GET", "/novaforge/games/{game_id}"): "no-render",  # compatibility card
    ("GET", "/novaforge/evaluations"): "no-render",  # evaluation history list
    ("GET", "/novaforge/evaluations/baseline"): "no-render",  # baseline card
    ("POST", "/novaforge/evaluations/preflight"): "no-render",  # readiness checklist
    ("POST", "/novaforge/evaluations"): "no-render",  # confirmed Start button
    ("GET", "/novaforge/evaluations/{evaluation_id}"): "no-render",  # live poll + detail
    ("POST", "/novaforge/evaluations/{evaluation_id}/cancel"): "no-render",  # Cancel button
    ("GET", "/novaforge/evaluations/{evaluation_id}/report"): "no-render",  # report card
    # NF-06 Forge Coach: js/novaforge.js owns the analysis, the recommendation
    # decisions and the experiment lifecycle; every response is rendered there.
    ("GET", "/novaforge/coach"): "no-render",  # capability card
    ("POST", "/novaforge/coach/analyse"): "no-render",  # Analyse button + findings
    ("GET", "/novaforge/coach/recommendations"): "no-render",  # persisted decisions
    (
        "POST",
        "/novaforge/coach/recommendations/{recommendation_id}",
    ): "no-render",  # approve/reject a recommendation
    ("GET", "/novaforge/experiments"): "no-render",  # experiment history list
    ("POST", "/novaforge/experiments"): "no-render",  # Propose button
    ("GET", "/novaforge/experiments/{experiment_id}"): "no-render",  # live poll + spec review
    ("POST", "/novaforge/experiments/{experiment_id}/approve"): "no-render",  # Approve button
    ("POST", "/novaforge/experiments/{experiment_id}/reject"): "no-render",  # Reject button
    ("POST", "/novaforge/experiments/{experiment_id}/run"): "no-render",  # Run button
    ("POST", "/novaforge/experiments/{experiment_id}/cancel"): "no-render",  # Cancel button
    ("GET", "/novaforge/experiments/{experiment_id}/results"): "no-render",  # comparison card
    # Phase 21 has no panel of its own yet: these are the API/CLI surfaces for the
    # perception layer, and claiming a renderer here would put a control in the
    # contract that does not exist (the same reasoning as POST /plan/run).
    ("GET", "/perception/status"): "no-render",  # operator surface: providers + telemetry
    ("GET", "/perception/capabilities"): "no-render",  # capability classification
    ("POST", "/perception"): "no-render",  # structured scene for API callers; reading only
    ("GET", "/world/status"): "no-render",  # operator surface: versions + retention
    ("GET", "/world/state"): "no-render",  # state inspection, read-only, no panel yet
    ("POST", "/world/observe"): "no-render",  # ingest for API callers; recording only
    ("POST", "/world/query"): "no-render",  # structured state query for API callers
    ("POST", "/world/predict"): "no-render",  # prediction boundary; often unavailable
    # Phase 23 has no panel of its own yet: these are the API/CLI surfaces for
    # interactive learning, and claiming a renderer here would put a control in the
    # contract that the frontend does not have (the same reasoning as POST /plan/run).
    ("GET", "/learning/status"): "no-render",  # operator surface; surfaced via /status
    ("GET", "/learning/environments"): "no-render",  # environment inventory
    ("POST", "/learning/environments"): "no-render",  # register a simulator
    ("POST", "/learning/objectives"): "no-render",  # create; runs nothing
    ("GET", "/learning/objectives"): "no-render",  # objective list
    ("GET", "/learning/objectives/{objective_id}"): "no-render",  # the record
    ("POST", "/learning/objectives/{objective_id}/preview"): "no-render",  # dry run
    ("POST", "/learning/objectives/{objective_id}/run"): "no-render",  # the cycle
    ("POST", "/learning/objectives/{objective_id}/approve"): "no-render",  # approval
    ("POST", "/learning/objectives/{objective_id}/reject"): "no-render",  # refusal
    ("POST", "/learning/objectives/{objective_id}/cancel"): "no-render",  # cancellation
    ("GET", "/learning/objectives/{objective_id}/resumable"): "no-render",  # resume gate
    ("POST", "/learning/objectives/{objective_id}/resume"): "no-render",  # resume
    ("GET", "/learning/metrics"): "no-render",  # learning metrics
    ("GET", "/learning/evidence/{evidence_id}"): "no-render",  # provenance lookup
    ("POST", "/learning/transfer"): "no-render",  # held-out check
    # Phase 24 has no panel of its own yet: these are the API/CLI surfaces for
    # goal-level planning, and claiming a renderer here would put a control in the
    # contract that the frontend does not have (the same reasoning as POST
    # /plan/run). A goal is created and planned over the API, executed over the
    # API, and reported through /status for the operator surfaces.
    ("GET", "/planning/status"): "no-render",  # operator surface; surfaced via /status
    ("GET", "/planning/goals"): "no-render",  # goal list
    ("POST", "/planning/goals"): "no-render",  # create + plan; runs nothing
    ("GET", "/planning/goals/{goal_id}"): "no-render",  # the record
    ("POST", "/planning/goals/{goal_id}/run"): "no-render",  # the gated execution
    ("POST", "/planning/goals/{goal_id}/reassess"): "no-render",  # replan decision
    ("POST", "/planning/goals/{goal_id}/cancel"): "no-render",  # cancellation
    # Phase 25 has no panel of its own yet: an embodied session is created and
    # driven over the API/CLI and reported through /status, exactly like the
    # Phase 23 learning and Phase 24 planning surfaces. Claiming a renderer here
    # would put a control in the contract that the frontend does not have.
    ("GET", "/embodied/status"): "no-render",  # operator surface; also in /status
    ("GET", "/embodied/overview"): "no-render",  # the phase's own statement
    ("GET", "/embodied/environments"): "no-render",  # allowlist + scenarios
    ("GET", "/embodied/sessions"): "no-render",  # session list
    ("POST", "/embodied/sessions"): "no-render",  # create; runs nothing
    ("GET", "/embodied/sessions/{session_id}"): "no-render",  # the record
    ("POST", "/embodied/sessions/{session_id}/run"): "no-render",  # the loop
    ("POST", "/embodied/sessions/{session_id}/pause"): "no-render",  # pause
    ("POST", "/embodied/sessions/{session_id}/resume"): "no-render",  # resume
    ("POST", "/embodied/sessions/{session_id}/cancel"): "no-render",  # cancellation
    # Phase 26 has no panel of its own yet: the benchmark is run over the API/CLI
    # and reported through /status, exactly like the Phase 23 learning, Phase 24
    # planning and Phase 25 embodied surfaces. Claiming a renderer here would put
    # a control in the contract that the frontend does not have.
    ("GET", "/generalization/status"): "no-render",  # operator surface; also in /status
    ("GET", "/generalization/overview"): "no-render",  # the phase's own statement
    ("GET", "/generalization/families"): "no-render",  # families + the no-aggregate rule
    ("GET", "/generalization/operators"): "no-render",  # the transformation library
    ("GET", "/generalization/tasks"): "no-render",  # task list, answers withheld
    ("GET", "/generalization/tasks/{task_id}"): "no-render",  # one task with its key
    ("GET", "/generalization/splits"): "no-render",  # split counts + leakage verdict
    ("POST", "/generalization/tasks"): "no-render",  # register a definition; runs nothing
    ("POST", "/generalization/evaluate"): "no-render",  # the bounded evaluation run
    ("GET", "/generalization/runs"): "no-render",  # trimmed run rows
    ("GET", "/generalization/runs/{run_id}"): "no-render",  # one run, whole
    ("GET", "/generalization/runs/{run_id}/export"): "no-render",  # review payload
    ("GET", "/generalization/compare"): "no-render",  # baseline vs candidate
    ("GET", "/generalization/failures"): "no-render",  # classified failures
    ("GET", "/generalization/recommendations"): "no-render",  # non-executing advice
    ("GET", "/generalization/game-eval/contracts"): "no-render",  # §13 definitions
    ("GET", "/generalization/game-eval"): "no-render",  # contracts over real sessions
    ("GET", "/intelligence"): "no-render",  # GIL telemetry (API/CLI surface; surfaced via /status)
    ("GET", "/capabilities"): "no-render",  # capability browser for API/CLI callers
    ("GET", "/capabilities/discover"): "no-render",  # 9.3: answers a question, runs nothing
    ("GET", "/bugs"): "no-render",  # Bug log rows rendered by app.js renderBugs, not run()
    ("POST", "/bugs/{bug_id}/fix"): "no-render",  # Mark-fixed row button re-fetches /bugs
    ("POST", "/bugs/clear-fixed"): "no-render",  # Clear Fixed button re-fetches /bugs
    (
        "POST",
        "/agent/run",
    ): "no-render",  # agentic loop (API/CLI surface; UI lands with the JARVIS panel)
    ("GET", "/agent/metrics"): "no-render",  # evaluation ledger metrics
    ("GET", "/agent/knowledge"): "no-render",  # application knowledge graph dump
    ("GET", "/plugins"): "no-render",  # plugin marketplace API (CLI only)
    ("GET", "/activity"): "no-render",  # timeline seed fetched by js/activity.js, not run()
    # Phase 13: automation and audit are API/CLI surfaces for now — the scheduler
    # has no panel yet, and claiming a renderer here would put a control in the
    # contract that the frontend does not have.
    ("GET", "/automation"): "no-render",
    ("POST", "/automation/schedule"): "no-render",
    ("POST", "/automation/approve"): "no-render",
    ("POST", "/automation/cancel"): "no-render",
    ("POST", "/automation/enable"): "no-render",
    ("POST", "/automation/disable"): "no-render",
    ("POST", "/automation/run"): "no-render",
    ("POST", "/automation/run-due"): "no-render",
    ("GET", "/audit"): "no-render",
    ("GET", "/audit/entries"): "no-render",
    ("POST", "/audit/prune"): "no-render",
    ("POST", "/audit/delete"): "no-render",
    ("POST", "/audit/clear"): "no-render",
    ("GET", "/privacy"): "no-render",
    ("POST", "/privacy"): "no-render",
    ("GET", "/resources"): "no-render",
    ("POST", "/cost/estimate"): "no-render",
    ("GET", "/diagnostics"): "no-render",
    ("GET", "/benchmark"): "no-render",
    ("POST", "/benchmark"): "no-render",
    # Phase 15: the evaluation surface is API/CLI for now — the trajectories,
    # scores and rewards are consumed by scripts and by whoever is reading the
    # store, so claiming a renderer here would put a control in the contract
    # that the frontend does not have.
    ("GET", "/evaluation/summary"): "no-render",
    ("GET", "/evaluation/trajectory/{trajectory_id}"): "no-render",
    ("GET", "/evaluation/metrics"): "no-render",
    ("GET", "/evaluation/rewards"): "no-render",
    # Phase 16: the fine-tuning surface is API/CLI for now. Starting, cancelling
    # or promoting are deliberate acts, and the frontend has no training panel
    # yet, so every row here stays non-render rather than promising a control
    # that does not exist.
    ("GET", "/training/status"): "no-render",
    ("GET", "/training/summary"): "no-render",
    ("POST", "/training/estimate"): "no-render",
    ("GET", "/training/datasets"): "no-render",
    ("POST", "/training/datasets"): "no-render",
    ("POST", "/training/datasets/validate"): "no-render",
    ("GET", "/training/datasets/{dataset_version_id}"): "no-render",
    ("GET", "/training/runs"): "no-render",
    ("POST", "/training/runs"): "no-render",
    ("POST", "/training/runs/start"): "no-render",
    ("POST", "/training/runs/pause"): "no-render",
    ("POST", "/training/runs/cancel"): "no-render",
    ("POST", "/training/runs/resume"): "no-render",
    ("POST", "/training/runs/re-estimate"): "no-render",
    ("POST", "/training/runs/evaluate"): "no-render",
    ("GET", "/training/runs/{run_id}/checkpoints"): "no-render",
    ("GET", "/training/runs/{run_id}"): "no-render",
    ("GET", "/training/evaluations"): "no-render",
    ("GET", "/training/models"): "no-render",
    ("GET", "/training/models/{model_id}"): "no-render",
    ("POST", "/training/models/approve"): "no-render",
    ("POST", "/training/models/promote"): "no-render",
    ("POST", "/training/models/reject"): "no-render",
    ("POST", "/training/models/deprecate"): "no-render",
    ("POST", "/training/models/rollback"): "no-render",
    # Phase 17: the preference surface is API/CLI for now, on the same reasoning
    # as the training surface above. Reviewing a pair is the one operation that
    # would naturally live in a UI, and until there is one the reviewer works
    # through these routes — so the queue renders nothing rather than promising a
    # control the frontend does not have.
    ("GET", "/preference/status"): "no-render",
    ("GET", "/preference/summary"): "no-render",
    ("GET", "/preference/algorithms"): "no-render",
    ("POST", "/preference/estimate"): "no-render",
    ("POST", "/preference/dry-run"): "no-render",
    ("GET", "/preference/datasets"): "no-render",
    ("POST", "/preference/datasets"): "no-render",
    ("POST", "/preference/datasets/validate"): "no-render",
    ("GET", "/preference/datasets/{dataset_version_id}"): "no-render",
    ("GET", "/preference/datasets/{dataset_version_id}/pairs/{preference_id}"): "no-render",
    ("GET", "/preference/reviews"): "no-render",
    ("GET", "/preference/reviews/{preference_id}"): "no-render",
    ("POST", "/preference/reviews/submit"): "no-render",
    ("POST", "/preference/reviews/decide"): "no-render",
    ("GET", "/preference/runs"): "no-render",
    ("POST", "/preference/runs"): "no-render",
    ("POST", "/preference/runs/start"): "no-render",
    ("POST", "/preference/runs/pause"): "no-render",
    ("POST", "/preference/runs/cancel"): "no-render",
    ("POST", "/preference/runs/resume"): "no-render",
    ("POST", "/preference/runs/re-estimate"): "no-render",
    ("POST", "/preference/runs/evaluate"): "no-render",
    ("POST", "/preference/compare"): "no-render",
    ("GET", "/preference/runs/{run_id}/checkpoints"): "no-render",
    ("GET", "/preference/runs/{run_id}"): "no-render",
    ("GET", "/preference/evaluations"): "no-render",
    ("GET", "/preference/models"): "no-render",
    ("GET", "/preference/models/{model_id}"): "no-render",
    # Phase 18: the RLHF/RLAIF surface is API/CLI for now, on the same reasoning
    # as the training and preference surfaces above. Submitting feedback is the
    # one operation that would naturally live in a UI, and until there is one
    # the reviewer works through these routes — so the surface renders nothing
    # rather than promising a control the frontend does not have.
    ("GET", "/rlhf/status"): "no-render",
    ("GET", "/rlhf/summary"): "no-render",
    ("GET", "/rlhf/algorithms"): "no-render",
    ("POST", "/rlhf/estimate"): "no-render",
    ("POST", "/rlhf/dry-run"): "no-render",
    ("POST", "/rlhf/pipeline"): "no-render",
    ("GET", "/rlhf/feedback"): "no-render",
    ("POST", "/rlhf/feedback"): "no-render",
    ("POST", "/rlhf/feedback/{feedback_id}/decide"): "no-render",
    ("POST", "/rlhf/rate"): "no-render",
    ("GET", "/rlhf/ratings"): "no-render",
    ("GET", "/rlhf/disagreements"): "no-render",
    ("GET", "/rlhf/datasets"): "no-render",
    ("POST", "/rlhf/datasets"): "no-render",
    ("GET", "/rlhf/datasets/{dataset_version_id}/validate"): "no-render",
    ("GET", "/rlhf/datasets/{dataset_version_id}/held"): "no-render",
    ("GET", "/rlhf/datasets/{dataset_version_id}"): "no-render",
    ("GET", "/rlhf/runs"): "no-render",
    ("POST", "/rlhf/runs"): "no-render",
    ("POST", "/rlhf/runs/start"): "no-render",
    ("POST", "/rlhf/runs/pause"): "no-render",
    ("POST", "/rlhf/runs/cancel"): "no-render",
    ("POST", "/rlhf/runs/resume"): "no-render",
    ("POST", "/rlhf/runs/re-estimate"): "no-render",
    ("POST", "/rlhf/runs/evaluate"): "no-render",
    ("POST", "/rlhf/compare"): "no-render",
    ("GET", "/rlhf/runs/{run_id}/checkpoints"): "no-render",
    ("GET", "/rlhf/runs/{run_id}"): "no-render",
    ("GET", "/rlhf/evaluations"): "no-render",
    ("GET", "/rlhf/models"): "no-render",
    ("GET", "/rlhf/models/{model_id}"): "no-render",
    # Phase 19: the RLVR / critique-based learning surface is consumed by API
    # callers and the CLI (there is no verifier/critique panel yet), and every
    # route reports rows a caller reads directly rather than a rendered card.
    ("GET", "/rlvr/status"): "no-render",
    ("GET", "/rlvr/summary"): "no-render",
    ("GET", "/rlvr/verifiers"): "no-render",
    ("POST", "/rlvr/verifiers/disable"): "no-render",
    ("POST", "/rlvr/verifiers/enable"): "no-render",
    ("POST", "/rlvr/verify"): "no-render",
    ("POST", "/rlvr/reward"): "no-render",
    ("POST", "/rlvr/critiques"): "no-render",
    ("GET", "/rlvr/critiques"): "no-render",
    ("GET", "/rlvr/corrections"): "no-render",
    ("POST", "/rlvr/corrections"): "no-render",
    ("GET", "/rlvr/datasets"): "no-render",
    ("POST", "/rlvr/datasets"): "no-render",
    ("GET", "/rlvr/datasets/{dataset_version_id}/validate"): "no-render",
    ("GET", "/rlvr/datasets/{dataset_version_id}/held"): "no-render",
    ("GET", "/rlvr/datasets/{dataset_version_id}/pairs"): "no-render",
    ("GET", "/rlvr/datasets/{dataset_version_id}"): "no-render",
    ("POST", "/rlvr/estimate"): "no-render",
    ("POST", "/rlvr/pipeline"): "no-render",
    ("POST", "/rlvr/dry-run"): "no-render",
    ("POST", "/rlvr/evaluate"): "no-render",
    ("GET", "/rlvr/runs"): "no-render",
    ("POST", "/rlvr/runs"): "no-render",
    ("POST", "/rlvr/runs/start"): "no-render",
    ("POST", "/rlvr/runs/pause"): "no-render",
    ("POST", "/rlvr/runs/cancel"): "no-render",
    ("POST", "/rlvr/runs/resume"): "no-render",
    ("POST", "/rlvr/runs/re-estimate"): "no-render",
    ("POST", "/rlvr/runs/evaluate"): "no-render",
    ("GET", "/rlvr/runs/{run_id}/checkpoints"): "no-render",
    ("GET", "/rlvr/runs/{run_id}"): "no-render",
    ("GET", "/market/events"): "no-render",
    ("GET", "/market/events/sources"): "no-render",
    ("POST", "/market/events/sources/{source_id}/enabled"): "no-render",
    ("POST", "/market/events/refresh"): "no-render",
    ("POST", "/market/events/expire"): "no-render",
    ("GET", "/market/events/instrument/{instrument_id}"): "no-render",
    ("GET", "/market/events/{event_id}"): "no-render",
    ("GET", "/market/events/{event_id}/evidence"): "no-render",
    ("GET", "/market/events/{event_id}/history"): "no-render",
    ("GET", "/market/discovery/status"): "no-render",
    ("GET", "/market/discovery/candidates"): "no-render",
    ("GET", "/market/discovery/candidates/{instrument_id}"): "no-render",
    ("POST", "/market/discovery/cycle"): "no-render",
    ("POST", "/market/discovery/reevaluate"): "no-render",
    ("POST", "/market/discovery/events/{event_id}"): "no-render",
    ("POST", "/market/discovery/scheduler"): "no-render",
    ("GET", "/market/discovery/history"): "no-render",
    ("GET", "/market/watchlists"): "no-render",
    ("POST", "/market/watchlists"): "no-render",
    ("GET", "/market/watchlists/{watchlist_id}"): "no-render",
    ("POST", "/market/watchlists/{watchlist_id}/members"): "no-render",
    ("DELETE", "/market/watchlists/{watchlist_id}/members/{instrument_id}"): "no-render",
    # PI-20 is consumed by the React Prediction Intelligence workspace, not by
    # the static render-panels dispatcher; every row stays non-render there.
    ("GET", "/market/intelligence/status"): "no-render",
    ("GET", "/market/portfolio-management/status"): "no-render",
    ("GET", "/market/portfolio-management/contracts"): "no-render",
    ("GET", "/market/portfolio-management/navigation"): "no-render",
    ("GET", "/market/portfolio-management/config"): "no-render",
    ("PUT", "/market/portfolio-management/config"): "no-render",
    ("POST", "/market/portfolio-management/review"): "no-render",
    ("GET", "/market/portfolio-management/cycles"): "no-render",
    ("GET", "/market/portfolio-management/cycles/{cycle_id}"): "no-render",
    ("GET", "/market/portfolio-management/cycles/{cycle_id}/events"): "no-render",
    ("POST", "/market/portfolio-management/cycles/{cycle_id}/handoff"): "no-render",
    ("POST", "/market/portfolio-management/cycles/{cycle_id}/reconcile"): "no-render",
    ("POST", "/market/portfolio-management/recover"): "no-render",
    ("GET", "/market/portfolio-management/portfolios/{portfolio_id}/snapshot"): "no-render",
    ("GET", "/market/portfolio-management/portfolios/{portfolio_id}/positions"): "no-render",
    ("GET", "/market/portfolio-management/portfolios/{portfolio_id}/opportunities"): "no-render",
    ("GET", "/market/portfolio-management/portfolios/{portfolio_id}/decisions"): "no-render",
    ("GET", "/market/portfolio-management/portfolios/{portfolio_id}/capital"): "no-render",
    ("GET", "/market/portfolio-management/portfolios/{portfolio_id}/cash"): "no-render",
    ("GET", "/market/portfolio-management/portfolios/{portfolio_id}/exposure"): "no-render",
    ("GET", "/market/portfolio-management/portfolios/{portfolio_id}/rebalancing"): "no-render",
    ("GET", "/market/portfolio-management/portfolios/{portfolio_id}/analytics"): "no-render",
    # PI-22 is consumed by the React Risk Intelligence workspace.
    ("GET", "/market/risk-intelligence/status"): "no-render",
    ("GET", "/market/risk-intelligence/contracts"): "no-render",
    ("GET", "/market/risk-intelligence/navigation"): "no-render",
    ("GET", "/market/risk-intelligence/config"): "no-render",
    ("PUT", "/market/risk-intelligence/config"): "no-render",
    ("POST", "/market/risk-intelligence/analyze"): "no-render",
    ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/overview"): "no-render",
    ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/positions"): "no-render",
    ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/concentration"): "no-render",
    ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/correlation"): "no-render",
    ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/volatility"): "no-render",
    ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/drawdown"): "no-render",
    ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/liquidity"): "no-render",
    ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/regime"): "no-render",
    ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/history"): "no-render",
    ("GET", "/market/risk-intelligence/portfolios/{portfolio_id}/trend"): "no-render",
    (
        "GET",
        "/market/risk-intelligence/portfolios/{portfolio_id}/recommendations",
    ): "no-render",
    ("GET", "/market/risk-intelligence/alerts"): "no-render",
    ("POST", "/market/risk-intelligence/alerts/{alert_id}/action"): "no-render",
    ("GET", "/market/risk-intelligence/scenarios"): "no-render",
    ("POST", "/market/risk-intelligence/scenarios"): "no-render",
    (
        "POST",
        "/market/risk-intelligence/portfolios/{portfolio_id}/scenarios/run",
    ): "no-render",
    (
        "GET",
        "/market/risk-intelligence/portfolios/{portfolio_id}/scenario-runs",
    ): "no-render",
    (
        "GET",
        "/market/risk-intelligence/portfolios/{portfolio_id}/stress-tests",
    ): "no-render",
    (
        "POST",
        "/market/risk-intelligence/portfolios/{portfolio_id}/stress-tests",
    ): "no-render",
    ("POST", "/market/risk-intelligence/recover"): "no-render",
    # PI-23 is consumed by the React Forecast Optimization workspace.
    ("GET", "/market/forecast-intelligence/status"): "no-render",
    ("GET", "/market/forecast-intelligence/contracts"): "no-render",
    ("GET", "/market/forecast-intelligence/navigation"): "no-render",
    ("GET", "/market/forecast-intelligence/config"): "no-render",
    ("PUT", "/market/forecast-intelligence/config"): "no-render",
    ("GET", "/market/forecast-intelligence/registry"): "no-render",
    ("GET", "/market/forecast-intelligence/history"): "no-render",
    ("GET", "/market/forecast-intelligence/overview"): "no-render",
    ("GET", "/market/forecast-intelligence/comparison/{instrument_id}"): "no-render",
    ("GET", "/market/forecast-intelligence/horizons/{instrument_id}"): "no-render",
    ("GET", "/market/forecast-intelligence/regimes/{instrument_id}"): "no-render",
    ("POST", "/market/forecast-intelligence/instruments"): "no-render",
    ("POST", "/market/forecast-intelligence/evaluate"): "no-render",
    ("POST", "/market/forecast-intelligence/select"): "no-render",
    ("POST", "/market/forecast-intelligence/calibration"): "no-render",
    ("POST", "/market/forecast-intelligence/uncertainty"): "no-render",
    ("POST", "/market/forecast-intelligence/disagreement"): "no-render",
    ("POST", "/market/forecast-intelligence/consistency"): "no-render",
    ("POST", "/market/forecast-intelligence/drift"): "no-render",
    ("POST", "/market/forecast-intelligence/degradation"): "no-render",
    ("POST", "/market/forecast-intelligence/weights"): "no-render",
    ("POST", "/market/forecast-intelligence/reliability"): "no-render",
    ("POST", "/market/forecast-intelligence/fallback"): "no-render",
    ("POST", "/market/forecast-intelligence/analyze"): "no-render",
    ("GET", "/market/forecast-intelligence/health"): "no-render",
    ("GET", "/market/event-intelligence/status"): "no-render",
    ("GET", "/market/event-intelligence/contracts"): "no-render",
    ("GET", "/market/event-intelligence/navigation"): "no-render",
    ("GET", "/market/event-intelligence/config"): "no-render",
    ("PUT", "/market/event-intelligence/config"): "no-render",
    ("GET", "/market/event-intelligence/events"): "no-render",
    ("GET", "/market/event-intelligence/events/{event_id}"): "no-render",
    ("GET", "/market/event-intelligence/timeline"): "no-render",
    ("POST", "/market/event-intelligence/relevance"): "no-render",
    ("POST", "/market/event-intelligence/impact"): "no-render",
    ("POST", "/market/event-intelligence/warnings"): "no-render",
    ("POST", "/market/event-intelligence/features"): "no-render",
    ("POST", "/market/event-intelligence/forecast-context"): "no-render",
    ("POST", "/market/event-intelligence/outcomes"): "no-render",
    ("POST", "/market/event-intelligence/outcomes/study"): "no-render",
    ("POST", "/market/event-intelligence/forecast-evaluation"): "no-render",
    ("POST", "/market/event-intelligence/agents"): "no-render",
    ("GET", "/market/event-intelligence/portfolio/{portfolio_id}/context"): "no-render",
    ("POST", "/market/event-intelligence/sync"): "no-render",
    ("POST", "/market/event-intelligence/ingest"): "no-render",
    ("POST", "/market/event-intelligence/analyze"): "no-render",
    ("GET", "/market/event-intelligence/overview"): "no-render",
    ("GET", "/market/event-intelligence/history"): "no-render",
    ("GET", "/market/event-intelligence/health"): "no-render",
    ("GET", "/market/intelligence/contracts"): "no-render",
    ("GET", "/market/intelligence/navigation"): "no-render",
    ("GET", "/market/intelligence/overview"): "no-render",
    ("GET", "/market/intelligence/instruments/{instrument_id}"): "no-render",
    ("GET", "/market/intelligence/forecasts"): "no-render",
    ("GET", "/market/intelligence/compare"): "no-render",
    ("GET", "/market/intelligence/horizons/{instrument_id}"): "no-render",
    ("GET", "/market/intelligence/lifecycle"): "no-render",
    ("GET", "/market/intelligence/performance"): "no-render",
    ("GET", "/market/intelligence/regimes"): "no-render",
    ("GET", "/market/intelligence/decisions"): "no-render",
    ("GET", "/market/intelligence/decisions/{cycle_id}"): "no-render",
    ("GET", "/market/intelligence/explain/{subject_id}"): "no-render",
    ("GET", "/market/intelligence/attribution/{forecast_id}"): "no-render",
    ("GET", "/market/intelligence/evidence/{subject_id}"): "no-render",
    ("GET", "/market/intelligence/research"): "no-render",
    ("GET", "/market/intelligence/portfolio"): "no-render",
    ("GET", "/market/intelligence/policies"): "no-render",
    ("GET", "/market/intelligence/replay/{cycle_id}"): "no-render",
    ("GET", "/market/intelligence/filters"): "no-render",
    ("PUT", "/market/intelligence/filters"): "no-render",
    ("POST", "/market/forecasts"): "no-render",
    ("GET", "/market/forecasts"): "no-render",
    ("GET", "/market/forecasts/status"): "no-render",
    ("GET", "/market/forecasts/models"): "no-render",
    ("GET", "/market/forecasts/performance"): "no-render",
    ("GET", "/market/forecasts/calibration"): "no-render",
    ("POST", "/market/forecasts/train"): "no-render",
    ("POST", "/market/forecasts/evaluate"): "no-render",
    ("POST", "/market/forecasts/models/{model_id}/promote"): "no-render",
    ("GET", "/market/forecasts/{forecast_id}/explain"): "no-render",
    ("GET", "/market/forecasts/{forecast_id}"): "no-render",
    ("GET", "/market/forecasts/{forecast_id}/outcome"): "no-render",
    ("POST", "/market/forecasts/cycles"): "no-render",
    ("GET", "/market/forecasts/cycles"): "no-render",
    ("GET", "/market/forecasts/cycles/{cycle_id}"): "no-render",
    ("GET", "/market/forecasts/scheduler"): "no-render",
    ("POST", "/market/forecasts/scheduler/tick"): "no-render",
    ("GET", "/market/forecasts/missed"): "no-render",
    ("GET", "/market/forecasts/evaluations/pending"): "no-render",
    ("GET", "/market/forecasts/accuracy"): "no-render",
    ("POST", "/market/forecasts/horizons"): "no-render",
    ("GET", "/market/forecasts/horizons/status"): "no-render",
    ("GET", "/market/forecasts/horizons/performance"): "no-render",
    ("GET", "/market/forecasts/horizons/{instrument_id}/context"): "no-render",
    ("GET", "/market/forecasts/horizons/{instrument_id}"): "no-render",
    ("POST", "/market/portfolios"): "no-render",
    ("GET", "/market/portfolios"): "no-render",
    ("GET", "/market/portfolios/status"): "no-render",
    ("GET", "/market/portfolios/horizons"): "no-render",
    ("GET", "/market/portfolios/{portfolio_id}"): "no-render",
    ("GET", "/market/portfolios/{portfolio_id}/holdings"): "no-render",
    ("GET", "/market/portfolios/{portfolio_id}/valuation"): "no-render",
    ("GET", "/market/portfolios/{portfolio_id}/valuations"): "no-render",
    ("GET", "/market/portfolios/{portfolio_id}/performance"): "no-render",
    ("GET", "/market/portfolios/{portfolio_id}/decisions"): "no-render",
    ("GET", "/market/portfolios/{portfolio_id}/proposals"): "no-render",
    ("GET", "/market/portfolios/{portfolio_id}/lifecycle"): "no-render",
    ("GET", "/market/portfolios/{portfolio_id}/reconcile"): "no-render",
    ("POST", "/market/portfolios/{portfolio_id}/rebalance"): "no-render",
    ("POST", "/market/portfolios/{portfolio_id}/apply"): "no-render",
    ("POST", "/market/portfolios/{portfolio_id}/status"): "no-render",
    ("POST", "/market/portfolios/{portfolio_id}/close"): "no-render",
    ("POST", "/market/risk/assess"): "no-render",
    ("GET", "/market/risk/status"): "no-render",
    ("GET", "/market/risk/contracts"): "no-render",
    ("GET", "/market/risk/decisions"): "no-render",
    ("GET", "/market/risk/decisions/{decision_id}"): "no-render",
    ("POST", "/market/risk/decisions/{decision_id}/apply"): "no-render",
    ("GET", "/market/risk/portfolio/{portfolio_id}"): "no-render",
    ("GET", "/market/risk/portfolio/{portfolio_id}/positions"): "no-render",
    ("GET", "/market/risk/position/{portfolio_id}/{instrument_id}"): "no-render",
    ("GET", "/market/risk/exposure"): "no-render",
    ("GET", "/market/risk/policies"): "no-render",
    ("POST", "/market/risk/policies/{policy_id}"): "no-render",
    ("GET", "/market/risk/alerts"): "no-render",
    ("POST", "/market/risk/alerts/{alert_id}/acknowledge"): "no-render",
    ("POST", "/market/risk/alerts/{alert_id}/resolve"): "no-render",
    ("POST", "/market/risk/stress"): "no-render",
    ("GET", "/market/risk/stress"): "no-render",
    ("GET", "/market/risk/overrides"): "no-render",
    ("POST", "/market/risk/overrides"): "no-render",
    ("GET", "/market/strategies/catalog"): "no-render",
    ("GET", "/market/strategies"): "no-render",
    ("POST", "/market/strategies"): "no-render",
    ("GET", "/market/strategies/status"): "no-render",
    ("GET", "/market/strategies/contracts"): "no-render",
    ("GET", "/market/strategies/proposals"): "no-render",
    ("GET", "/market/strategies/proposals/{proposal_id}"): "no-render",
    ("POST", "/market/strategies/proposals/{proposal_id}/forward"): "no-render",
    ("GET", "/market/strategies/conflicts"): "no-render",
    ("GET", "/market/strategies/signals"): "no-render",
    ("GET", "/market/strategies/signals/{signal_id}"): "no-render",
    ("GET", "/market/strategies/{strategy_id}"): "no-render",
    ("POST", "/market/strategies/{strategy_id}/config"): "no-render",
    ("GET", "/market/strategies/{strategy_id}/versions"): "no-render",
    ("POST", "/market/strategies/{strategy_id}/activate"): "no-render",
    ("POST", "/market/strategies/{strategy_id}/pause"): "no-render",
    ("POST", "/market/strategies/{strategy_id}/suspend"): "no-render",
    ("POST", "/market/strategies/{strategy_id}/retire"): "no-render",
    ("POST", "/market/strategies/{strategy_id}/degrade"): "no-render",
    ("POST", "/market/strategies/{strategy_id}/evaluate"): "no-render",
    ("GET", "/market/strategies/{strategy_id}/signals"): "no-render",
    ("GET", "/market/strategies/{strategy_id}/performance"): "no-render",
    ("GET", "/market/strategies/{strategy_id}/lifecycle"): "no-render",
    ("POST", "/market/strategies/run"): "no-render",
    ("POST", "/market/execution/orders"): "no-render",
    ("GET", "/market/execution/orders"): "no-render",
    ("GET", "/market/execution/orders/{order_id}"): "no-render",
    ("GET", "/market/execution/orders/{order_id}/fills"): "no-render",
    ("GET", "/market/execution/orders/{order_id}/history"): "no-render",
    ("POST", "/market/execution/orders/{order_id}/process"): "no-render",
    ("POST", "/market/execution/orders/{order_id}/cancel"): "no-render",
    ("GET", "/market/execution/fills"): "no-render",
    ("GET", "/market/execution/status"): "no-render",
    ("GET", "/market/execution/contracts"): "no-render",
    ("GET", "/market/execution/fees"): "no-render",
    ("GET", "/market/execution/slippage"): "no-render",
    ("POST", "/market/execution/reconcile"): "no-render",
    ("GET", "/market/execution/reconciliations"): "no-render",
    ("GET", "/market/backtests/status"): "no-render",
    ("GET", "/market/backtests/contracts"): "no-render",
    ("POST", "/market/backtests/validate"): "no-render",
    ("POST", "/market/backtests"): "no-render",
    ("GET", "/market/backtests"): "no-render",
    ("POST", "/market/backtests/compare"): "no-render",
    ("GET", "/market/backtests/comparisons"): "no-render",
    ("POST", "/market/backtests/{backtest_id}/walk-forward"): "no-render",
    ("POST", "/market/backtests/{backtest_id}/start"): "no-render",
    ("POST", "/market/backtests/{backtest_id}/cancel"): "no-render",
    ("GET", "/market/backtests/{backtest_id}"): "no-render",
    ("GET", "/market/backtests/{backtest_id}/results"): "no-render",
    ("GET", "/market/backtests/{backtest_id}/metrics"): "no-render",
    ("GET", "/market/backtests/{backtest_id}/trades"): "no-render",
    ("GET", "/market/backtests/{backtest_id}/orders"): "no-render",
    ("GET", "/market/backtests/{backtest_id}/risk-decisions"): "no-render",
    ("GET", "/market/backtests/{backtest_id}/signals"): "no-render",
    ("GET", "/market/backtests/{backtest_id}/data-quality"): "no-render",
    ("GET", "/market/backtests/{backtest_id}/verify"): "no-render",
    ("POST", "/market/backtests/{backtest_id}/sensitivity"): "no-render",
    # PI-13: the RL research workspace is a NovaMarket web page; these are
    # research APIs and controls, not legacy static-panel bodies.
    ("GET", "/market/rl/status"): "no-render",
    ("GET", "/market/rl/contracts"): "no-render",
    ("GET", "/market/rl/health"): "no-render",
    ("POST", "/market/rl/environments"): "no-render",
    ("GET", "/market/rl/environments"): "no-render",
    ("GET", "/market/rl/environments/{environment_id}"): "no-render",
    ("POST", "/market/rl/environments/{environment_id}/episodes"): "no-render",
    ("GET", "/market/rl/episodes"): "no-render",
    ("GET", "/market/rl/episodes/{episode_id}"): "no-render",
    ("POST", "/market/rl/episodes/{episode_id}/step"): "no-render",
    ("POST", "/market/rl/episodes/{episode_id}/reset"): "no-render",
    ("POST", "/market/rl/episodes/{episode_id}/cancel"): "no-render",
    ("GET", "/market/rl/episodes/{episode_id}/transitions"): "no-render",
    ("GET", "/market/rl/episodes/{episode_id}/experiences"): "no-render",
    # PI-14: the Reward & Policy Evaluation page is a NovaMarket web page;
    # these research APIs are not legacy static-panel bodies.
    ("GET", "/market/rewards/status"): "no-render",
    ("GET", "/market/rewards/contracts"): "no-render",
    ("GET", "/market/rewards/health"): "no-render",
    ("GET", "/market/rewards/baselines"): "no-render",
    ("POST", "/market/rewards/configurations"): "no-render",
    ("GET", "/market/rewards/configurations"): "no-render",
    ("GET", "/market/rewards/configurations/{config_id}"): "no-render",
    ("POST", "/market/rewards/episodes/{episode_id}/calculate"): "no-render",
    ("GET", "/market/rewards/episodes/{episode_id}"): "no-render",
    ("GET", "/market/rewards/results/{reward_id}"): "no-render",
    ("POST", "/market/rewards/evaluations"): "no-render",
    ("POST", "/market/rewards/evaluations/cancel"): "no-render",
    ("GET", "/market/rewards/evaluations"): "no-render",
    ("GET", "/market/rewards/evaluations/{evaluation_id}"): "no-render",
    ("GET", "/market/rewards/comparisons"): "no-render",
    ("POST", "/market/rewards/stability"): "no-render",
    ("POST", "/market/rewards/walk-forward"): "no-render",
    # PI-15: the RL Training & Optimisation workspace is a NovaMarket web page.
    ("GET", "/market/training/status"): "no-render",
    ("GET", "/market/training/contracts"): "no-render",
    ("GET", "/market/training/health"): "no-render",
    ("GET", "/market/training/datasets"): "no-render",
    ("POST", "/market/training/configurations"): "no-render",
    ("GET", "/market/training/configurations"): "no-render",
    ("GET", "/market/training/configurations/{config_id}"): "no-render",
    ("POST", "/market/training/validate"): "no-render",
    ("POST", "/market/training/runs"): "no-render",
    ("GET", "/market/training/runs"): "no-render",
    ("GET", "/market/training/runs/{run_id}"): "no-render",
    ("POST", "/market/training/runs/{run_id}/pause"): "no-render",
    ("POST", "/market/training/runs/{run_id}/resume"): "no-render",
    ("POST", "/market/training/runs/{run_id}/cancel"): "no-render",
    ("POST", "/market/training/runs/{run_id}/retry"): "no-render",
    ("GET", "/market/training/runs/{run_id}/metrics"): "no-render",
    ("GET", "/market/training/runs/{run_id}/checkpoints"): "no-render",
    ("POST", "/market/training/runs/{run_id}/checkpoints"): "no-render",
    ("GET", "/market/training/checkpoints/{checkpoint_id}"): "no-render",
    ("POST", "/market/training/checkpoints/{checkpoint_id}/resume"): "no-render",
    ("GET", "/market/training/policies"): "no-render",
    ("GET", "/market/training/policies/{policy_id}"): "no-render",
    ("POST", "/market/training/policies/{policy_id}/archive"): "no-render",
    ("POST", "/market/training/policies/{policy_id}/validate"): "no-render",
    ("POST", "/market/training/policies/{policy_id}/test"): "no-render",
    ("POST", "/market/training/compare"): "no-render",
    ("POST", "/market/training/optimize"): "no-render",
    ("GET", "/market/training/searches/{search_id}"): "no-render",
    ("GET", "/market/training/evaluations"): "no-render",
    # PI-16: the Adaptive Intelligence workspace is a new React page; the API
    # contract still declares every response as not rendered by the legacy UI.
    ("GET", "/market/adaptive/status"): "no-render",
    ("GET", "/market/adaptive/contracts"): "no-render",
    ("GET", "/market/adaptive/health"): "no-render",
    ("GET", "/market/adaptive/dashboard"): "no-render",
    ("POST", "/market/adaptive/configurations"): "no-render",
    ("GET", "/market/adaptive/configurations"): "no-render",
    ("GET", "/market/adaptive/configurations/{config_id}"): "no-render",
    ("POST", "/market/adaptive/regime/analyze"): "no-render",
    ("GET", "/market/adaptive/regime/latest"): "no-render",
    ("POST", "/market/adaptive/regime/annotations"): "no-render",
    ("GET", "/market/adaptive/regime/annotations"): "no-render",
    ("POST", "/market/adaptive/regime/evaluations"): "no-render",
    ("GET", "/market/adaptive/regime/evaluations"): "no-render",
    ("POST", "/market/adaptive/experiences/query"): "no-render",
    ("POST", "/market/adaptive/changes/detect"): "no-render",
    ("GET", "/market/adaptive/changes"): "no-render",
    ("POST", "/market/adaptive/drift/detect"): "no-render",
    ("GET", "/market/adaptive/drift"): "no-render",
    ("POST", "/market/adaptive/degradation/monitor"): "no-render",
    ("GET", "/market/adaptive/degradation"): "no-render",
    ("POST", "/market/adaptive/suitability/assess"): "no-render",
    ("GET", "/market/adaptive/suitability"): "no-render",
    ("POST", "/market/adaptive/recommendations"): "no-render",
    ("GET", "/market/adaptive/recommendations"): "no-render",
    ("POST", "/market/adaptive/recommendations/{recommendation_id}/acknowledge"): "no-render",
    ("POST", "/market/adaptive/retraining"): "no-render",
    ("GET", "/market/adaptive/retraining"): "no-render",
    ("POST", "/market/adaptive/reassessments"): "no-render",
    ("GET", "/market/adaptive/reassessments"): "no-render",
    ("POST", "/market/adaptive/reassessments/{reassessment_id}/run"): "no-render",
    ("GET", "/market/adaptive/reassessments/{reassessment_id}"): "no-render",
    # PI-17 is consumed by the dedicated React Multi-Agent Intelligence page,
    # not the legacy static renderer bundle.
    ("GET", "/market/multiagent/status"): "no-render",
    ("GET", "/market/multiagent/contracts"): "no-render",
    ("GET", "/market/multiagent/health"): "no-render",
    ("GET", "/market/multiagent/agents"): "no-render",
    ("GET", "/market/multiagent/agents/{agent_id}"): "no-render",
    ("PUT", "/market/multiagent/agents/{agent_id}/config"): "no-render",
    ("POST", "/market/multiagent/cycles"): "no-render",
    ("GET", "/market/multiagent/cycles"): "no-render",
    ("GET", "/market/multiagent/cycles/{cycle_id}"): "no-render",
    ("POST", "/market/multiagent/cycles/{cycle_id}/run"): "no-render",
    ("POST", "/market/multiagent/cycles/{cycle_id}/cancel"): "no-render",
    ("GET", "/market/multiagent/cycles/{cycle_id}/agents"): "no-render",
    ("GET", "/market/multiagent/cycles/{cycle_id}/evidence"): "no-render",
    ("GET", "/market/multiagent/cycles/{cycle_id}/conflicts"): "no-render",
    ("GET", "/market/multiagent/cycles/{cycle_id}/decision"): "no-render",
    ("GET", "/market/multiagent/cycles/{cycle_id}/handoff"): "no-render",
    ("GET", "/market/multiagent/history"): "no-render",
    ("GET", "/market/multiagent/performance"): "no-render",
    ("GET", "/market/multiagent/performance/{agent_id}"): "no-render",
    ("GET", "/market/optimization/status"): "no-render",
    ("GET", "/market/optimization/contracts"): "no-render",
    ("GET", "/market/optimization/health"): "no-render",
    ("POST", "/market/optimization/configs"): "no-render",
    ("GET", "/market/optimization/configs"): "no-render",
    ("GET", "/market/optimization/configs/{config_id}"): "no-render",
    ("POST", "/market/optimization/validate"): "no-render",
    ("POST", "/market/optimization/runs"): "no-render",
    ("GET", "/market/optimization/runs"): "no-render",
    ("GET", "/market/optimization/history"): "no-render",
    ("GET", "/market/optimization/latest"): "no-render",
    ("GET", "/market/optimization/runs/{run_id}"): "no-render",
    ("GET", "/market/optimization/runs/{run_id}/allocations"): "no-render",
    ("GET", "/market/optimization/runs/{run_id}/diversification"): "no-render",
    ("GET", "/market/optimization/runs/{run_id}/risk"): "no-render",
    ("GET", "/market/optimization/runs/{run_id}/liquidity"): "no-render",
    ("GET", "/market/optimization/runs/{run_id}/rebalancing"): "no-render",
    ("GET", "/market/optimization/runs/{run_id}/scenarios"): "no-render",
    ("POST", "/market/optimization/runs/{run_id}/scenarios"): "no-render",
    ("POST", "/market/optimization/runs/{run_id}/backtests"): "no-render",
    ("GET", "/market/optimization/runs/{run_id}/handoff"): "no-render",
    ("POST", "/market/optimization/runs/{run_id}/cancel"): "no-render",
    ("GET", "/market/optimization/backtests"): "no-render",
    ("GET", "/market/optimization/backtests/{backtest_id}"): "no-render",
    ("GET", "/market/research/status"): "no-render",
    ("GET", "/market/research/contracts"): "no-render",
    ("GET", "/market/research/health"): "no-render",
    ("POST", "/market/research/runs"): "no-render",
    ("POST", "/market/research/runs/{run_id}/run"): "no-render",
    ("POST", "/market/research/runs/{run_id}/cancel"): "no-render",
    ("POST", "/market/research/runs/{run_id}/refresh"): "no-render",
    ("GET", "/market/research/runs"): "no-render",
    ("GET", "/market/research/history"): "no-render",
    ("GET", "/market/research/latest"): "no-render",
    ("GET", "/market/research/runs/{run_id}"): "no-render",
    ("GET", "/market/research/runs/{run_id}/company"): "no-render",
    ("GET", "/market/research/runs/{run_id}/financials"): "no-render",
    ("GET", "/market/research/runs/{run_id}/events"): "no-render",
    ("GET", "/market/research/runs/{run_id}/news"): "no-render",
    ("GET", "/market/research/runs/{run_id}/sector"): "no-render",
    ("GET", "/market/research/runs/{run_id}/peers"): "no-render",
    ("GET", "/market/research/runs/{run_id}/valuation"): "no-render",
    ("GET", "/market/research/runs/{run_id}/thesis"): "no-render",
    ("GET", "/market/research/runs/{run_id}/evidence"): "no-render",
    ("GET", "/market/research/runs/{run_id}/claims"): "no-render",
    ("GET", "/market/research/runs/{run_id}/features"): "no-render",
    ("GET", "/market/research/runs/{run_id}/handoff"): "no-render",
    ("POST", "/market/research/monitor"): "no-render",
    ("GET", "/market/evaluations/status"): "no-render",
    ("GET", "/market/evaluations/contracts"): "no-render",
    ("GET", "/market/evaluations"): "no-render",
    ("POST", "/market/evaluations"): "no-render",
    ("GET", "/market/evaluations/{evaluation_id}"): "no-render",
    ("GET", "/market/evaluations/{evaluation_id}/verify"): "no-render",
    ("POST", "/market/evaluations/{evaluation_id}/reproduce"): "no-render",
    ("GET", "/market/shadow/status"): "no-render",
    ("GET", "/market/shadow/contracts"): "no-render",
    ("POST", "/market/shadow/sessions"): "no-render",
    ("GET", "/market/shadow/sessions"): "no-render",
    ("GET", "/market/shadow/sessions/{session_id}"): "no-render",
    ("GET", "/market/shadow/sessions/{session_id}/history"): "no-render",
    ("POST", "/market/shadow/sessions/{session_id}/start"): "no-render",
    ("POST", "/market/shadow/sessions/{session_id}/pause"): "no-render",
    ("POST", "/market/shadow/sessions/{session_id}/resume"): "no-render",
    ("POST", "/market/shadow/sessions/{session_id}/stop"): "no-render",
    ("POST", "/market/shadow/sessions/{session_id}/complete"): "no-render",
    ("POST", "/market/shadow/sessions/{session_id}/cancel"): "no-render",
    ("POST", "/market/shadow/sessions/{session_id}/cycle"): "no-render",
    ("POST", "/market/shadow/sessions/{session_id}/track-outcomes"): "no-render",
    ("GET", "/market/shadow/sessions/{session_id}/decisions"): "no-render",
    ("GET", "/market/shadow/sessions/{session_id}/projections"): "no-render",
    ("GET", "/market/shadow/sessions/{session_id}/outcomes"): "no-render",
    ("GET", "/market/shadow/sessions/{session_id}/analytics"): "no-render",
    ("GET", "/market/shadow/sessions/{session_id}/errors"): "no-render",
    ("GET", "/market/shadow/sessions/{session_id}/health"): "no-render",
    ("POST", "/market/shadow/sessions/{session_id}/compare"): "no-render",
    ("GET", "/market/shadow/sessions/{session_id}/comparisons"): "no-render",
    ("GET", "/market/learning/status"): "no-render",
    ("GET", "/market/learning/contracts"): "no-render",
    ("GET", "/market/learning/dashboard"): "no-render",
    ("GET", "/market/learning/health"): "no-render",
    ("POST", "/market/learning/evidence"): "no-render",
    ("GET", "/market/learning/evidence"): "no-render",
    ("GET", "/market/learning/evidence/{evidence_id}"): "no-render",
    ("POST", "/market/learning/monitor"): "no-render",
    ("GET", "/market/learning/alerts"): "no-render",
    ("POST", "/market/learning/alerts/{alert_id}/acknowledge"): "no-render",
    ("POST", "/market/learning/experiments"): "no-render",
    ("GET", "/market/learning/experiments"): "no-render",
    ("GET", "/market/learning/experiments/{experiment_id}"): "no-render",
    ("POST", "/market/learning/experiments/{experiment_id}/start"): "no-render",
    ("POST", "/market/learning/experiments/{experiment_id}/run"): "no-render",
    ("POST", "/market/learning/experiments/{experiment_id}/cancel"): "no-render",
    ("GET", "/market/learning/experiments/{experiment_id}/candidates"): "no-render",
    ("GET", "/market/learning/candidates/{candidate_id}"): "no-render",
    ("POST", "/market/learning/candidates/{candidate_id}/evaluate"): "no-render",
    ("POST", "/market/learning/candidates/{candidate_id}/approve"): "no-render",
    ("POST", "/market/learning/candidates/{candidate_id}/reject"): "no-render",
    ("POST", "/market/learning/candidates/{candidate_id}/shadow-validate"): "no-render",
    ("POST", "/market/learning/candidates/{candidate_id}/promote"): "no-render",
    ("GET", "/market/learning/strategies/{strategy_id}/versions"): "no-render",
    ("GET", "/market/learning/strategies/{strategy_id}/active-version"): "no-render",
    ("POST", "/market/learning/strategies/{strategy_id}/rollback"): "no-render",
}

# Markers that are not renderer function names (allowed non-render roles).
NON_RENDER_ROLES = frozenset({"no-render", "load-settings"})
