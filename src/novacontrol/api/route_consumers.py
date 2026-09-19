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
    # Rendered panels (run(type) -> renderer dispatch, render-utils.js).
    ("POST", "/ask"): "renderChatResult",  # envelope branches by route/intent
    ("POST", "/brain/decide"): "no-render",  # routing-explorer.js renders the trace itself
    ("POST", "/explore"): "renderExplore",
    ("GET", "/explore/trending"): "no-render",  # example chips fetched by js/routing-explorer.js sibling app.js loader, not run()
    ("POST", "/command/plan"): "renderCommand",
    ("POST", "/command/execute"): "renderCommand",
    ("POST", "/desktop/plan"): "renderCommand",
    ("POST", "/desktop/execute"): "renderCommand",
    ("POST", "/phone/plan"): "renderCommand",
    ("POST", "/phone/execute"): "renderCommand",
    ("POST", "/browser/plan"): "renderCommand",
    ("POST", "/browser/execute"): "renderCommand",
    ("POST", "/plan"): "renderBuild",
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
    ("POST", "/phone/connect"): "renderPhoneStatus",  # Connect Phone button - pairing flow, bridge-shaped result
    ("GET", "/vision/status"): "no-render",  # Vision panel toast-only status check
    ("POST", "/vision/model"): "no-render",  # vision model card connect button; refreshes card
    ("POST", "/vision/model/clear"): "no-render",  # vision model card remove button; refreshes card
    ("POST", "/vision/describe"): "renderGeneric",  # Vision panel Describe Screen
    ("POST", "/vision/click"): "renderGeneric",  # Vision panel guided click
    ("GET", "/intelligence"): "no-render",  # GIL telemetry (API/CLI surface; surfaced via /status)
    ("GET", "/bugs"): "no-render",  # Bug log rows rendered by app.js renderBugs, not run()
    ("POST", "/bugs/{bug_id}/fix"): "no-render",  # Mark-fixed row button re-fetches /bugs
    ("POST", "/bugs/clear-fixed"): "no-render",  # Clear Fixed button re-fetches /bugs
    ("POST", "/agent/run"): "no-render",  # agentic loop (API/CLI surface; UI lands with the JARVIS panel)
    ("GET", "/agent/metrics"): "no-render",  # evaluation ledger metrics
    ("GET", "/agent/knowledge"): "no-render",  # application knowledge graph dump
    ("GET", "/plugins"): "no-render",  # plugin marketplace API (CLI only)
    ("GET", "/activity"): "no-render",  # timeline seed fetched by js/activity.js, not run()
}

# Markers that are not renderer function names (allowed non-render roles).
NON_RENDER_ROLES = frozenset({"no-render", "load-settings"})
