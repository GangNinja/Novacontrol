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
    ("POST", "/phone/connect"): "renderPhoneStatus",  # Connect Phone button - pairing flow, bridge-shaped result
    ("GET", "/vision/status"): "no-render",  # Vision panel toast-only status check
    ("POST", "/vision/model"): "no-render",  # vision model card connect button; refreshes card
    ("POST", "/vision/model/clear"): "no-render",  # vision model card remove button; refreshes card
    ("POST", "/vision/describe"): "renderGeneric",  # Vision panel Describe Screen
    ("POST", "/vision/click"): "renderGeneric",  # Vision panel guided click
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
    ("POST", "/novaforge/sessions/{session_id}/outcomes/verify"): "no-render",  # verify a plan's steps
    ("POST", "/novaforge/sessions/{session_id}/outcomes/recover"): "no-render",  # apply one recovery
    ("POST", "/novaforge/sessions/{session_id}/outcomes/cancel"): "no-render",  # cancel verification
    # NF-22: the autonomous-run card (JARVIS panel) polls its own state and its
    # buttons repaint in place, so the panel renders these responses itself rather
    # than through the generic result renderer.
    ("GET", "/novaforge/sessions/{session_id}/autonomy"): "no-render",  # autonomy card poll
    ("POST", "/novaforge/sessions/{session_id}/autonomy/start"): "no-render",  # Start Autonomous Run
    ("POST", "/novaforge/sessions/{session_id}/autonomy/pause"): "no-render",  # Pause Run button
    ("POST", "/novaforge/sessions/{session_id}/autonomy/resume"): "no-render",  # Resume Run button
    ("POST", "/novaforge/sessions/{session_id}/autonomy/cancel"): "no-render",  # Cancel Run button
    ("POST", "/novaforge/sessions/{session_id}/autonomy/approve"): "no-render",  # per-plan approval
    ("GET", "/novaforge/autonomy/trace"): "no-render",  # trace view
    ("POST", "/novaforge/sessions/{session_id}/verify"): "no-render",  # Verify button
    ("POST", "/novaforge/sessions/{session_id}/loop"): "no-render",  # Run Closed Loop button
    ("POST", "/novaforge/sessions/{session_id}/loop/cancel"): "no-render",  # Stop Loop button
    ("POST", "/novaforge/sessions/{session_id}/vision/interpret"): "no-render",  # Describe Frame button
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
    ("POST", "/novaforge/coach/recommendations/{recommendation_id}"): "no-render",  # approve/reject a recommendation
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
