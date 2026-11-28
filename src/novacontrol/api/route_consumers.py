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
    ("GET", "/memory/personal/status"): "no-render",  # personal-memory management is API/CLI until a panel exists
    ("GET", "/memory/personal"): "no-render",  # searchable memory API
    ("POST", "/memory/personal"): "no-render",  # acquisition API
    ("GET", "/memory/personal/export"): "no-render",  # user-controlled export
    ("POST", "/memory/personal/consolidate"): "no-render",  # maintenance API
    ("POST", "/memory/personal/cleanup"): "no-render",  # retention maintenance
    ("GET", "/memory/personal/{record_id}"): "no-render",  # inspect API
    ("POST", "/memory/personal/{record_id}/correct"): "no-render",  # correction API
    ("POST", "/memory/personal/{record_id}/delete"): "no-render",  # deletion API
    ("POST", "/memory/personal/{record_id}/resolve"): "no-render",  # conflict resolution API
    ("POST", "/memory/personal/relations"): "no-render",  # relationship API
    ("POST", "/improve/workflow"): "renderWorkflow",
    ("POST", "/improve/preview"): "renderWorkflow",
    ("POST", "/improve/approve"): "renderWorkflow",
    ("POST", "/learn"): "renderLearning",
    ("POST", "/train"): "renderLearning",
    ("GET", "/system/health"): "renderHealth",
    # Phase 34: the self-management dashboard card. Control POSTs refresh the
    # card from GET /self-management; no POST response is rendered as a card.
    ("GET", "/self-management"): "renderSelfManagement",
    ("POST", "/self-management/health"): "no-render",
    ("POST", "/self-management/diagnose"): "no-render",
    ("GET", "/self-management/incidents"): "no-render",
    ("GET", "/self-management/incidents/{incident_id}"): "no-render",
    ("POST", "/self-management/incidents/{incident_id}/investigate"): "no-render",
    ("POST", "/self-management/incidents/{incident_id}/recommend"): "no-render",
    ("POST", "/self-management/incidents/{incident_id}/recover"): "no-render",
    ("POST", "/self-management/pause"): "no-render",
    ("POST", "/self-management/resume"): "no-render",
    ("POST", "/self-management/policy"): "no-render",
    ("GET", "/self-management/maintenance"): "no-render",
    ("POST", "/self-management/maintenance/preview"): "no-render",
    ("POST", "/self-management/maintenance/run"): "no-render",
    ("GET", "/self-management/configuration"): "no-render",
    ("POST", "/self-management/configuration/snapshot"): "no-render",
    ("POST", "/self-management/configuration/restore"): "no-render",
    # Phase 35: the Business Opportunities workspace. The GET list owns the
    # renderer; detail and all controls are read/updated by its own listener
    # code so a mutation never replaces the panel with a raw JSON card.
    ("GET", "/business/opportunities"): "renderBusinessOpportunities",
    ("GET", "/business/status"): "no-render",
    ("POST", "/business/discovery/scan"): "no-render",
    ("POST", "/business/opportunities"): "no-render",
    ("GET", "/business/opportunities/compare"): "no-render",
    ("GET", "/business/opportunities/{opportunity_id}"): "no-render",
    ("POST", "/business/opportunities/{opportunity_id}/research"): "no-render",
    ("POST", "/business/opportunities/{opportunity_id}/evidence"): "no-render",
    ("POST", "/business/opportunities/{opportunity_id}/assess"): "no-render",
    ("POST", "/business/opportunities/{opportunity_id}/experiments/plan"): "no-render",
    ("POST", "/business/opportunities/{opportunity_id}/evaluate"): "no-render",
    ("POST", "/business/opportunities/{opportunity_id}/experiments/{experiment_id}/approval"): "no-render",
    ("POST", "/business/opportunities/{opportunity_id}/experiments/{experiment_id}/outcome"): "no-render",
    ("POST", "/business/opportunities/{opportunity_id}/pause"): "no-render",
    ("POST", "/business/opportunities/{opportunity_id}/resume"): "no-render",
    ("POST", "/business/opportunities/{opportunity_id}/reject"): "no-render",
    ("POST", "/business/opportunities/{opportunity_id}/archive"): "no-render",
    # Phase 36: the Product workspace owns list/detail rendering and all
    # mutations refresh through its own listeners, so a mutation never replaces
    # the panel with a raw JSON card.
    ("GET", "/product/projects"): "renderProductProjects",
    ("GET", "/product/projects/{product_id}"): "renderProductProject",
    ("GET", "/product/status"): "no-render",
    ("GET", "/product/projects/{product_id}/history"): "no-render",
    ("POST", "/product/projects"): "no-render",
    ("POST", "/product/projects/{product_id}/prd"): "no-render",
    ("POST", "/product/projects/{product_id}/prd/approval"): "no-render",
    ("POST", "/product/projects/{product_id}/architecture"): "no-render",
    ("POST", "/product/projects/{product_id}/architecture/approval"): "no-render",
    ("POST", "/product/projects/{product_id}/plan"): "no-render",
    ("POST", "/product/projects/{product_id}/plan/approval"): "no-render",
    ("POST", "/product/projects/{product_id}/development/start"): "no-render",
    ("POST", "/product/projects/{product_id}/tasks/run-ready"): "no-render",
    ("POST", "/product/projects/{product_id}/tasks/{task_id}/run"): "no-render",
    ("POST", "/product/projects/{product_id}/tasks/{task_id}/result"): "no-render",
    ("POST", "/product/projects/{product_id}/tasks/{task_id}/retry"): "no-render",
    ("POST", "/product/projects/{product_id}/environment/validate"): "no-render",
    ("POST", "/product/projects/{product_id}/workspace/files"): "no-render",
    ("POST", "/product/projects/{product_id}/workspace/cleanup"): "no-render",
    ("POST", "/product/projects/{product_id}/build"): "no-render",
    ("POST", "/product/projects/{product_id}/tests"): "no-render",
    ("POST", "/product/projects/{product_id}/quality/run"): "no-render",
    ("POST", "/product/projects/{product_id}/defects"): "no-render",
    ("POST", "/product/projects/{product_id}/defects/{defect_id}/resolve"): "no-render",
    ("POST", "/product/projects/{product_id}/defects/{defect_id}/reopen"): "no-render",
    ("POST", "/product/projects/{product_id}/requirements/{requirement_id}/verify"): "no-render",
    ("POST", "/product/projects/{product_id}/corrective-tasks"): "no-render",
    ("POST", "/product/projects/{product_id}/release"): "no-render",
    ("POST", "/product/projects/{product_id}/release/approval"): "no-render",
    ("POST", "/product/projects/{product_id}/launch-readiness"): "no-render",
    ("POST", "/product/projects/{product_id}/launch/approval"): "no-render",
    # Phase 37: the Customer & Growth workspace owns list/detail rendering; its
    # controls refresh through the panel's own listeners, never a raw JSON card.
    ("GET", "/customer/workspaces"): "renderCustomerGrowth",
    ("GET", "/customer/status"): "no-render",
    ("GET", "/customer/workspaces/{product_id}"): "no-render",
    ("GET", "/customer/workspaces/{product_id}/history"): "no-render",
    ("GET", "/customer/workspaces/{product_id}/analytics"): "no-render",
    ("GET", "/customer/workspaces/{product_id}/customers"): "no-render",
    ("GET", "/customer/workspaces/{product_id}/customers/{customer_id}"): "no-render",
    ("GET", "/customer/workspaces/{product_id}/leads"): "no-render",
    ("GET", "/customer/workspaces/{product_id}/tickets"): "no-render",
    ("GET", "/customer/workspaces/{product_id}/tickets/{ticket_id}"): "no-render",
    ("POST", "/customer/workspaces"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/refresh"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/segments"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/icps"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/research"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/customers"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/customers/{customer_id}/consent"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/customers/{customer_id}/success"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/customers/{customer_id}/churn"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/customers/{customer_id}/erasure"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/leads"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/leads/{lead_id}/consent"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/leads/{lead_id}/qualify"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/leads/{lead_id}/status"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/leads/{lead_id}/convert"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/sales/assist"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/interactions"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/deals"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/deals/{deal_id}/stage"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/deals/{deal_id}/proposals"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/deals/{deal_id}/decisions"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/policy"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/communications"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/communications/{communication_id}/approval"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/communications/{communication_id}/send"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/knowledge"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/tickets"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/tickets/{ticket_id}/triage"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/tickets/{ticket_id}/messages"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/tickets/{ticket_id}/status"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/tickets/{ticket_id}/assist"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/feedback"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/onboarding"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/onboarding/{onboarding_id}"): "no-render",
    ("POST", "/customer/workspaces/{product_id}/costs"): "no-render",
    # Phase 38: the Finance workspace reads the venture list, one venture and
    # its dashboard. POSTs refresh those views explicitly, so they do not claim
    # an automatic renderer.
    ("GET", "/finance/status"): "no-render",
    ("GET", "/finance/ventures"): "renderFinanceList",
    ("POST", "/finance/ventures"): "no-render",
    ("GET", "/finance/ventures/{venture_id}"): "renderFinanceDetail",
    ("GET", "/finance/ventures/{venture_id}/dashboard"): "renderFinanceDashboard",
    ("POST", "/finance/ventures/{venture_id}/status"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/members"): "no-render",
    ("GET", "/finance/ventures/{venture_id}/operations"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/objectives"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/tasks"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/tasks/{task_id}"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/milestones"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/workflows"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/risks"): "no-render",
    ("GET", "/finance/ventures/{venture_id}/revenue"): "no-render",
    ("GET", "/finance/ventures/{venture_id}/revenue/summary"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/revenue"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/revenue/{revenue_id}/advance"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/revenue/{revenue_id}/refund"): "no-render",
    ("GET", "/finance/ventures/{venture_id}/expenses"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/expenses"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/expenses/{expense_id}/approval"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/expenses/{expense_id}/payment"): "no-render",
    ("GET", "/finance/ventures/{venture_id}/journal"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/journal"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/journal/{journal_id}/reverse"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/reconciliations"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/periods/close"): "no-render",
    ("GET", "/finance/ventures/{venture_id}/trial-balance"): "no-render",
    ("GET", "/finance/ventures/{venture_id}/statements/income"): "no-render",
    ("GET", "/finance/ventures/{venture_id}/statements/balance-sheet"): "no-render",
    ("GET", "/finance/ventures/{venture_id}/statements/cash-flow"): "no-render",
    ("GET", "/finance/ventures/{venture_id}/statements/comparison"): "no-render",
    ("GET", "/finance/ventures/{venture_id}/cash-flow"): "no-render",
    ("GET", "/finance/ventures/{venture_id}/working-capital"): "no-render",
    ("GET", "/finance/ventures/{venture_id}/profitability"): "no-render",
    ("GET", "/finance/ventures/{venture_id}/budgets"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/budgets"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/budgets/{budget_id}/approval"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/budgets/{budget_id}/revisions"): "no-render",
    ("GET", "/finance/ventures/{venture_id}/budgets/{budget_id}/report"): "no-render",
    ("GET", "/finance/ventures/{venture_id}/forecasts"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/forecasts"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/scenarios"): "no-render",
    ("GET", "/finance/ventures/{venture_id}/alerts"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/health/refresh"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/alerts/{alert_id}/resolve"): "no-render",
    ("GET", "/finance/ventures/{venture_id}/reports"): "no-render",
    ("POST", "/finance/ventures/{venture_id}/reports"): "no-render",
    # Phase 39 portfolio workspace.
    ("GET", "/portfolio/status"): "no-render",
    ("GET", "/portfolios"): "renderPortfolioList",
    ("POST", "/portfolios"): "no-render",
    ("GET", "/portfolios/{portfolio_id}"): "renderPortfolioDetail",
    ("PATCH", "/portfolios/{portfolio_id}"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/status"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/members"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/fx-rates"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/monitoring"): "no-render",
    ("GET", "/portfolios/{portfolio_id}/ventures"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/ventures"): "no-render",
    ("GET", "/portfolios/{portfolio_id}/ventures/{venture_id}"): "no-render",
    ("GET", "/portfolios/{portfolio_id}/ventures/{venture_id}/lifecycle/evidence"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/ventures/{venture_id}/lifecycle"): "no-render",
    ("GET", "/portfolios/{portfolio_id}/analytics"): "no-render",
    ("GET", "/portfolios/{portfolio_id}/comparison"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/evaluation-policies"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/evaluations"): "no-render",
    ("GET", "/portfolios/{portfolio_id}/ranking"): "no-render",
    ("GET", "/portfolios/{portfolio_id}/risks"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/risks"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/risks/assess"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/risks/{risk_id}/resolve"): "no-render",
    ("GET", "/portfolios/{portfolio_id}/allocations"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/allocations"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/allocations/{batch_id}/decision"): "no-render",
    ("GET", "/portfolios/{portfolio_id}/resources"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/resources"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/resources/{resource_id}/allocations"): "no-render",
    ("GET", "/portfolios/{portfolio_id}/scenarios"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/scenarios"): "no-render",
    ("GET", "/portfolios/{portfolio_id}/recommendations"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/recommendations"): "no-render",
    ("GET", "/portfolios/{portfolio_id}/decisions"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/decisions"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/decisions/{decision_id}/approval"): "no-render",
    ("GET", "/portfolios/{portfolio_id}/coordination"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/coordination/dependencies"): "no-render",
    ("GET", "/portfolios/{portfolio_id}/knowledge"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/knowledge"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/knowledge/{knowledge_id}/promote"): "no-render",
    ("GET", "/portfolios/{portfolio_id}/alerts"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/alerts/refresh"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/alerts/{alert_id}/resolve"): "no-render",
    ("GET", "/portfolios/{portfolio_id}/reports"): "no-render",
    ("POST", "/portfolios/{portfolio_id}/reports"): "no-render",
    ("GET", "/portfolios/{portfolio_id}/reports/{report_id}"): "no-render",
    ("GET", "/portfolios/{portfolio_id}/dashboard"): "renderPortfolioDashboard",
    ("GET", "/portfolios/{portfolio_id}/history"): "no-render",
    ("POST", "/product/projects/{product_id}/conditions/resolve"): "no-render",
    ("POST", "/product/projects/{product_id}/cancel"): "no-render",
    ("POST", "/product/projects/{product_id}/resume"): "no-render",
    ("POST", "/product/projects/{product_id}/archive"): "no-render",
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
    (
        "GET",
        "/models/routing",
    ): "renderModelRouting",  # System panel: Adaptive Model Routing card
    (
        "POST",
        "/models/routing/route",
    ): "no-render",  # selection/explanation API
    (
        "POST",
        "/models/routing/complete",
    ): "no-render",  # routed completion API
    (
        "POST",
        "/models/routing/config",
    ): "no-render",  # real routing controls
    (
        "POST",
        "/models/routing/providers",
    ): "no-render",  # provider enable/disable control
    (
        "GET",
        "/models/routing/history",
    ): "no-render",  # routing evidence API
    (
        "GET",
        "/models/routing/policy",
    ): "no-render",  # learned-policy report
    (
        "POST",
        "/models/routing/policy/activate",
    ): "no-render",  # policy activation
    (
        "POST",
        "/models/routing/policy/rollback",
    ): "no-render",  # policy rollback
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
    ("GET", "/agent/knowledge"): "no-render",  # application knowledge graph
    # Phase 29: computer operation is an API/CLI surface in this build. No
    # panel claims to render these bodies, so they are declared honestly.
    ("GET", "/computer/status"): "no-render",
    ("GET", "/computer/devices"): "no-render",
    ("POST", "/computer/observe"): "no-render",
    ("POST", "/computer/plan"): "no-render",
    ("POST", "/computer/run"): "no-render",
    ("POST", "/computer/stop"): "no-render",
    ("GET", "/computer/history"): "no-render",
    ("POST", "/computer/history/clear"): "no-render",
    ("POST", "/computer/reference"): "no-render",
  # application knowledge graph dump
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
    # Phase 30: the Operations panel is the consumer. Each control posts to the
    # route that performs exactly that change, and the board re-reads /objectives.
    ("GET", "/operations"): "renderOperations",
    ("GET", "/objectives"): "renderOperations",
    ("POST", "/objectives"): "no-render",
    ("POST", "/objectives/update"): "no-render",
    ("POST", "/objectives/delete"): "no-render",
    ("POST", "/objectives/control"): "no-render",
    ("POST", "/objectives/tasks"): "no-render",
    ("POST", "/objectives/milestones"): "no-render",
    ("POST", "/objectives/run"): "no-render",
    ("POST", "/objectives/tasks/decide"): "no-render",
    ("GET", "/operations/notifications"): "renderOperations",
    ("POST", "/operations/notifications/read"): "no-render",
    ("POST", "/operations/notifications/clear"): "no-render",
    ("POST", "/operations/notifications/preferences"): "no-render",
    ("POST", "/operations/tick"): "no-render",
    # Phase 31: the skill library has an API/CLI surface in this build. No panel
    # claims to render these bodies, so every row is declared non-render rather
    # than promising a control the frontend does not have.
    ("GET", "/skills"): "no-render",
    ("GET", "/skills/catalog"): "no-render",
    ("GET", "/skills/search"): "no-render",
    ("GET", "/skills/evidence"): "no-render",
    ("GET", "/skills/{skill_id}"): "no-render",
    ("POST", "/skills/acquire"): "no-render",
    ("POST", "/skills/validate"): "no-render",
    ("POST", "/skills/adapt"): "no-render",
    ("POST", "/skills/compose"): "no-render",
    ("POST", "/skills/compose/execute"): "no-render",
    ("POST", "/skills/execute"): "no-render",
    ("POST", "/skills/resume"): "no-render",
    ("POST", "/skills/promote"): "no-render",
    ("POST", "/skills/deprecate"): "no-render",
    ("POST", "/skills/rollback"): "no-render",
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
    # Phase 27: infrastructure surfaces with no web panel yet (API/CLI only).
    ("GET", "/continual/status"): "no-render",
    ("GET", "/continual/overview"): "no-render",
    ("GET", "/continual/experiences"): "no-render",
    ("GET", "/continual/experiences/{experience_id}"): "no-render",
    ("DELETE", "/continual/experiences/{experience_id}"): "no-render",
    ("POST", "/continual/feedback"): "no-render",
    ("GET", "/continual/signals"): "no-render",
    ("GET", "/continual/strategies"): "no-render",
    ("GET", "/continual/proposals"): "no-render",
    ("POST", "/continual/proposals"): "no-render",
    ("POST", "/continual/proposals/{proposal_id}/validate"): "no-render",
    ("POST", "/continual/proposals/{proposal_id}/promote"): "no-render",
    ("POST", "/continual/strategies/{strategy_id}/rollback"): "no-render",
    ("POST", "/continual/recommend"): "no-render",
    ("POST", "/continual/benchmark"): "no-render",
    # Phase 28: coordination surfaces with no web panel yet (API/CLI only).
    ("GET", "/collaboration/status"): "no-render",
    ("GET", "/collaboration/overview"): "no-render",
    ("POST", "/collaboration/plan"): "no-render",
    ("POST", "/collaboration/run"): "no-render",
    ("GET", "/collaboration/tasks"): "no-render",
    ("GET", "/collaboration/tasks/{task_id}"): "no-render",
    ("POST", "/collaboration/tasks/{task_id}/cancel"): "no-render",
    ("POST", "/collaboration/tasks/{task_id}/resume"): "no-render",
}

# Markers that are not renderer function names (allowed non-render roles).
NON_RENDER_ROLES = frozenset({"no-render", "load-settings"})
