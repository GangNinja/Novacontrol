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
    ("GET", "/intelligence"): "no-render",  # GIL telemetry (API/CLI surface; surfaced via /status)
    ("GET", "/capabilities"): "no-render",  # capability browser for API/CLI callers
    ("GET", "/capabilities/discover"): "no-render",  # 9.3: answers a question, runs nothing
    ("GET", "/bugs"): "no-render",  # Bug log rows rendered by app.js renderBugs, not run()
    ("POST", "/bugs/{bug_id}/fix"): "no-render",  # Mark-fixed row button re-fetches /bugs
    ("POST", "/bugs/clear-fixed"): "no-render",  # Clear Fixed button re-fetches /bugs
    ("POST", "/agent/run"): "no-render",  # agentic loop (API/CLI surface; UI lands with the JARVIS panel)
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
}

# Markers that are not renderer function names (allowed non-render roles).
NON_RENDER_ROLES = frozenset({"no-render", "load-settings"})
