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

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"command": "open calculator"}]}
    )

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
                ApiRoute("GET", "/status", "Runtime status."),
                ApiRoute("GET", "/system/health", "Aggregated system health."),
                ApiRoute("GET", "/system/telemetry", "Live machine telemetry for the Command Center (CPU, memory, storage, GPU, network, battery, temperature)."),
                ApiRoute("GET", "/system/harden", "Release hardening report."),
                ApiRoute("GET", "/system/package", "Runtime package manifest."),
                # Phase 34: trustworthy self-management. The GETs read stored or
                # cached state; the POSTs are the real health, diagnosis,
                # recovery, maintenance and configuration controls.
                ApiRoute("GET", "/self-management", "Operational health, incidents, recovery policy, maintenance and configuration state.", authenticated=True),
                ApiRoute("POST", "/self-management/health", "Run the live component health roster and open/update incidents; optional `only` list.", authenticated=True),
                ApiRoute("POST", "/self-management/diagnose", "Run evidence-backed diagnosis without opening new incidents.", authenticated=True),
                ApiRoute("GET", "/self-management/incidents", "Incident history, open counts and recurrence groups; query: `open_only`, `component`, `limit`.", authenticated=True),
                ApiRoute("GET", "/self-management/incidents/{incident_id}", "One incident with evidence, diagnosis and registered recovery recommendations.", authenticated=True),
                ApiRoute("POST", "/self-management/incidents/{incident_id}/investigate", "Re-run the roster and attach an evidence-backed diagnosis to one incident.", authenticated=True),
                ApiRoute("POST", "/self-management/incidents/{incident_id}/recommend", "List recovery actions and approval requirements for one incident without executing them.", authenticated=True),
                ApiRoute("POST", "/self-management/incidents/{incident_id}/recover", "Execute one approved recovery action with verification and bounded retry; body: `action_id`, `approved`.", authenticated=True),
                ApiRoute("POST", "/self-management/pause", "Pause autonomous recovery and maintenance execution.", authenticated=True),
                ApiRoute("POST", "/self-management/resume", "Resume autonomous recovery and maintenance execution.", authenticated=True),
                ApiRoute("POST", "/self-management/policy", "Set autonomy mode, pause state and recovery-window budget.", authenticated=True),
                ApiRoute("GET", "/self-management/maintenance", "Maintenance policy, allowlisted roots and last report.", authenticated=True),
                ApiRoute("POST", "/self-management/maintenance/preview", "Preview maintenance impact without changing files.", authenticated=True),
                ApiRoute("POST", "/self-management/maintenance/run", "Run maintenance; approval-required items need `approved`.", authenticated=True),
                ApiRoute("GET", "/self-management/configuration", "Configuration validity, checksum, known-good version history and recovery state.", authenticated=True),
                ApiRoute("POST", "/self-management/configuration/snapshot", "Record a validated configuration snapshot with an optional note.", authenticated=True),
                ApiRoute("POST", "/self-management/configuration/restore", "Restore a known-good configuration version; body: `version`, `approved`.", authenticated=True),
                # Phase 35: evidence-backed business opportunity and validation
                # engine. Research reuses Explore; external experiments are only
                # recorded/approved, never executed by these routes.
                ApiRoute("GET", "/business/status", "Business validation service status, limits, evidence rules and measured resource snapshot.", authenticated=True),
                ApiRoute("GET", "/business/opportunities", "List/filter opportunities; query: `q`, `stage`, `industry`, `limit`, `offset`.", authenticated=True),
                ApiRoute("POST", "/business/discovery/scan", "Run a budgeted Explore scan for source-backed candidate signals; it never invents a problem or solution.", authenticated=True),
                ApiRoute("POST", "/business/opportunities", "Record a sourced opportunity signal or operator brief with duplicate detection.", authenticated=True),
                ApiRoute("GET", "/business/opportunities/compare", "Compare 2–5 opportunities by recorded evidence and score; query: `ids` (comma-separated).", authenticated=True),
                ApiRoute("GET", "/business/opportunities/{opportunity_id}", "Full opportunity: evidence, market, customer, competitors, feasibility, financials, score, experiments, report and history.", authenticated=True),
                ApiRoute("POST", "/business/opportunities/{opportunity_id}/research", "Run one budgeted Explore research pass; sources are stored as unclassified secondary references.", authenticated=True),
                ApiRoute("POST", "/business/opportunities/{opportunity_id}/evidence", "Record a specific sourced claim with its quality, provenance and retrieval time.", authenticated=True),
                ApiRoute("POST", "/business/opportunities/{opportunity_id}/assess", "Compute market/customer/competitor analysis, feasibility, financial scenarios and explainable score; body: `financial_inputs`, `feasibility_inputs`, `assessment_inputs`.", authenticated=True),
                ApiRoute("POST", "/business/opportunities/{opportunity_id}/experiments/plan", "Generate practical validation experiments from the recorded evidence gaps.", authenticated=True),
                ApiRoute("POST", "/business/opportunities/{opportunity_id}/evaluate", "Derive the validation verdict and report from recorded evidence; never from an agent recommendation.", authenticated=True),
                ApiRoute("POST", "/business/opportunities/{opportunity_id}/experiments/{experiment_id}/approval", "Record an operator approval or denial for one experiment; body: `approved`, `note`.", authenticated=True),
                ApiRoute("POST", "/business/opportunities/{opportunity_id}/experiments/{experiment_id}/outcome", "Record an approved experiment's outcome and result; no external action is executed.", authenticated=True),
                ApiRoute("POST", "/business/opportunities/{opportunity_id}/pause", "Pause an opportunity and remember the stage it came from.", authenticated=True),
                ApiRoute("POST", "/business/opportunities/{opportunity_id}/resume", "Resume a paused opportunity to its remembered stage.", authenticated=True),
                ApiRoute("POST", "/business/opportunities/{opportunity_id}/reject", "Reject an opportunity with a required reason.", authenticated=True),
                ApiRoute("POST", "/business/opportunities/{opportunity_id}/archive", "Archive an opportunity; its evidence and history are retained.", authenticated=True),
                # Phase 36: validated products from requirements to launch readiness.
                # All approvals are authenticated human actions; no route deploys,
                # publishes, commits or executes a commercial commitment.
                ApiRoute("GET", "/product/status", "Product pipeline status, budgets, workspace root and external-action boundaries.", authenticated=True),
                ApiRoute("GET", "/product/projects", "List product projects; query: `q`, `state`, `limit`, `offset`.", authenticated=True),
                ApiRoute("POST", "/product/projects", "Create one product project from a VALIDATED or approved CONDITIONALLY_VALIDATED Phase 35 opportunity; body: `opportunity_id`, `name`, `approved`, `approval_note`, `environment`, `quality_gates`.", authenticated=True),
                ApiRoute("GET", "/product/projects/{product_id}", "Full product record: opportunity trace, PRD, architecture, plan, tasks, tests, gates, defects, releases, readiness, approvals and history.", authenticated=True),
                ApiRoute("GET", "/product/projects/{product_id}/history", "Product events, approval history, PRD/architecture revisions and resolved opportunity conditions.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/prd", "Generate a traceable PRD revision; missing information is listed, never invented.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/prd/approval", "Approve or request revision of the current PRD; body: `approved`, `note`, optional `version`.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/architecture", "Generate a technical design and ADRs from the approved PRD.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/architecture/approval", "Approve or reject the architecture and its major decisions; body: `approved`, `note`.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/plan", "Create a milestone/task plan with dependencies, estimates and a critical path.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/plan/approval", "Approve or reject the development plan; body: `approved`, `note`.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/development/start", "Start development after human PRD and architecture approval.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/tasks/run-ready", "Execute dependency-ready tasks through the existing agent registry; body: optional `limit`.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/tasks/{task_id}/run", "Execute one dependency-ready task through the existing agent registry or configured executor.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/tasks/{task_id}/result", "Record an externally verified task result with evidence; body: `verified`, `summary`, `evidence`, optional `files`.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/tasks/{task_id}/retry", "Retry a failed, blocked, cancelled or needs-review task within its attempt budget.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/environment/validate", "Validate the isolated workspace and runtime; dependencies and builds are separate evidence.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/workspace/files", "Write a reviewed file inside the isolated product workspace; body: `path`, `content`, optional `expected_sha256`, `overwrite`.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/workspace/cleanup", "Delete only this product's isolated workspace; body: `approved`.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/build", "Record a real build command result; body: `command`, `exit_code`, `output`, `validated`.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/tests", "Record one real test result against a configured quality gate; passing counts must be consistent.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/quality/run", "Run the built-in pytest quality runner for supported gates; unsupported gates remain not run; body: optional `gates`, `timeout_seconds`.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/defects", "Open a defect with severity and optional requirement/task/test trace.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/defects/{defect_id}/resolve", "Resolve a defect only with passing test evidence; body: `resolution`, `test_result_ids`, optional `fix_task_id`, `verified`.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/defects/{defect_id}/reopen", "Reopen a verified defect with a reason.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/requirements/{requirement_id}/verify", "Record requirement verification or an unmet result with evidence.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/corrective-tasks", "Create bounded corrective tasks for failing gates, blocking defects and unmet requirements.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/release", "Prepare a versioned source-bundle release with notes, checks, deployment docs, rollback and security checklist; no deployment is performed.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/release/approval", "Approve or reject a prepared release; body: `release_id`, `approved`, `note`.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/launch-readiness", "Evaluate launch readiness transparently; this never launches or publishes anything.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/launch/approval", "Record human launch authorization only after a ready, approved release; body: `approved`, `note`.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/conditions/resolve", "Resolve one recorded conditional-validation condition with evidence.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/cancel", "Cancel execution and mark pending/running tasks cancelled; body: `reason`.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/resume", "Resume a cancelled product without reviving cancelled tasks automatically.", authenticated=True),
                ApiRoute("POST", "/product/projects/{product_id}/archive", "Archive a product while retaining its documents, evidence and history.", authenticated=True),
                # Phase 37: customer acquisition, sales, support and retention.
                # One workspace per launch-authorized Phase 36 product; outbound
                # communication requires explicit approval or a narrow policy.
                ApiRoute("GET", "/customer/status", "Customer-growth status, configured integrations, budgets and external-action boundaries.", authenticated=True),
                ApiRoute("GET", "/customer/workspaces", "List customer workspaces; query: `limit`, `offset`.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces", "Create the workspace for a launch-authorized Phase 36 product; body: `product_id`.", authenticated=True),
                ApiRoute("GET", "/customer/workspaces/{product_id}", "Full customer workspace: product snapshot, segments, ICPs, research, customers, leads, deals, communications, tickets, feedback, onboarding, success, analytics and history.", authenticated=True),
                ApiRoute("GET", "/customer/workspaces/{product_id}/history", "Workspace events, approvals, communication audit and ticket histories.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/refresh", "Refresh the recorded Phase 36 product snapshot and re-check launch authorization.", authenticated=True),
                ApiRoute("GET", "/customer/workspaces/{product_id}/analytics", "Acquisition, sales, support and retention analytics; unavailable values are explicit.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/segments", "Record a customer segment with evidence or inferred assumptions; body: `name`, `category`, `needs`, `pain_points`, `evidence`.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/icps", "Record an Ideal Customer Profile linked to a segment; body: `name`, `segment_id`, `criteria`, `evidence`.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/research", "Record sourced market or customer research with retrieval time, confidence and verification state.", authenticated=True),
                ApiRoute("GET", "/customer/workspaces/{product_id}/customers", "List customers in one isolated product workspace.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/customers", "Record a customer with lawful contact, consent and segment links.", authenticated=True),
                ApiRoute("GET", "/customer/workspaces/{product_id}/customers/{customer_id}", "One customer with interactions, deals, tickets, feedback, onboarding and success record.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/customers/{customer_id}/consent", "Record or withdraw customer consent for specific channels; body: `status`, `channels`, `source`.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/customers/{customer_id}/success", "Refresh explainable engagement, adoption and churn-risk indicators for one customer.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/customers/{customer_id}/churn", "Record churn with a required reason; body: `reason`.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/customers/{customer_id}/erasure", "Erase one customer and directly related records with explicit approval; body: `approved`, `reason`.", authenticated=True),
                ApiRoute("GET", "/customer/workspaces/{product_id}/leads", "List/filter leads; query: `status`.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/leads", "Record a lead with a lawful source, consent, segment and optional qualification inputs.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/leads/{lead_id}/consent", "Record or withdraw lead consent for specific channels; body: `status`, `channels`, `source`.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/leads/{lead_id}/qualify", "Run explainable deterministic qualification; body: optional criterion `inputs` or `score`/`evidence` values.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/leads/{lead_id}/status", "Move a lead through an allowed lifecycle transition; body: `status`, optional `lost_reason`.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/leads/{lead_id}/convert", "Convert a qualified lead into a customer; body: optional `customer_inputs`.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/interactions", "Record one real customer interaction against a lead, customer, deal or ticket.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/sales/assist", "Build a traceable sales brief from recorded qualification, research, interactions, objections and proposals; body: `lead_id` or `deal_id`.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/deals", "Create a sales deal from a lead or customer; value is recorded as an estimate.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/deals/{deal_id}/stage", "Move a deal through a configured pipeline stage; body: `stage`, optional `lost_reason`, `realized_amount`.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/deals/{deal_id}/proposals", "Record a sourced proposal revision without implying a contractual commitment.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/deals/{deal_id}/decisions", "Record a deal decision and its source reference.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/policy", "Configure a narrow communication automation policy; enabling requires a named approval note.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/communications", "Draft one consent-aware customer communication for explicit approval.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/communications/{communication_id}/approval", "Approve, reject or cancel one draft communication; body: `approved`, `note`.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/communications/{communication_id}/send", "Attempt delivery through the configured sender; without one the record becomes `not_configured` and nothing is sent.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/knowledge", "Record approved product knowledge for grounded support; body: `title`, `body`, `source`, `approved`, `verified`, optional `steps`.", authenticated=True),
                ApiRoute("GET", "/customer/workspaces/{product_id}/tickets", "List/filter support tickets; query: `status`.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/tickets", "Create a support ticket for a recorded customer.", authenticated=True),
                ApiRoute("GET", "/customer/workspaces/{product_id}/tickets/{ticket_id}", "One ticket with messages, notes, history, escalation and assistance runs.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/tickets/{ticket_id}/triage", "Categorise, prioritise, assign and triage one ticket.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/tickets/{ticket_id}/messages", "Add an internal note or customer-visible response; body: `body`, `internal`.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/tickets/{ticket_id}/status", "Transition a ticket; resolution or escalation text is required where applicable.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/tickets/{ticket_id}/assist", "Generate grounded support suggestions from approved, verified knowledge only.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/feedback", "Record feedback linked to product, version, customer, segment and ticket.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/onboarding", "Start an onboarding checklist for one customer.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/onboarding/{onboarding_id}", "Update one onboarding item with evidence and adoption score; body: `item_id`, `status`, `evidence`.", authenticated=True),
                ApiRoute("POST", "/customer/workspaces/{product_id}/costs", "Record actual acquisition cost from a named source; CAC stays unavailable without these records.", authenticated=True),
                # Phase 38: multi-venture operations and financial intelligence.
                # Records and analyses money; no route transfers, pays, purchases,
                # changes banking details or mutates a finalized ledger entry.
                ApiRoute("GET", "/finance/status", "Business-finance service status, configured readers, limits and no-payment boundaries.", authenticated=True),
                ApiRoute("GET", "/finance/ventures", "List isolated ventures with status, currency, recognized revenue and open alerts; query: `limit`, `offset`, `status`.", authenticated=True),
                ApiRoute("POST", "/finance/ventures", "Create one venture with business model, ownership, authorization, reporting currency, fiscal period and product/customer references.", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}", "Venture detail: accounts, operations, revenue, expenses, trial balance, working capital, health and read-only links.", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}/dashboard", "Multi-dimensional dashboard separating actual, estimated, projected and forecast values.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/status", "Change operating status; body: `status`.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/members", "Assign a venture role; body: `actor`, `role` (viewer, analyst, approver, admin).", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}/operations", "Operational dashboard: task completion, delays, blocked work, dependencies, utilization, cost and risks.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/objectives", "Record an operational objective with owner, due date and linked product.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/tasks", "Record an operational task with dependencies, cost, utilization and linked execution.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/tasks/{task_id}", "Update a recorded task's status, cost, utilization, delivery evidence or execution link.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/milestones", "Record an operational milestone.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/workflows", "Record an operational workflow and ordered steps.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/risks", "Record an operational risk with severity, likelihood, owner and status.", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}/revenue", "List revenue records with separate booked, invoiced, recognized and collected dates.", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}/revenue/summary", "Revenue intelligence: booked, invoiced, recognized, collected, deferred, outstanding, refunds, tax and concentration.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/revenue", "Record revenue with an explicit stage, source and currency; non-reporting currencies require a sourced rate.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/revenue/{revenue_id}/advance", "Advance revenue forward through booked, invoiced, recognized and collected, posting the matching journal entries.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/revenue/{revenue_id}/refund", "Record a refund with a reason and post a contra-revenue/cash entry; body: `amount`, `reason`.", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}/expenses", "List expenses and their actual/estimated/committed/projected status, approval and payment state.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/expenses", "Record an expense with category, currency, date, payment state, source, support and recurrence.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/expenses/{expense_id}/approval", "Approve or reject an expense; only approved actual expenses post to the ledger.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/expenses/{expense_id}/payment", "Record payment of an approved actual expense; this records a payment, it never executes one.", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}/journal", "Read append-only journal entries; query: `limit`.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/journal", "Post a balanced double-entry journal in the venture reporting currency.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/journal/{journal_id}/reverse", "Reverse a posted entry with a new balanced entry; body: `reason`, optional `date`.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/reconciliations", "Reconcile one account against a statement balance without changing ledger history.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/periods/close", "Close a financial period and snapshot its statements; later entries in it are refused.", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}/trial-balance", "Trial balance with debit/credit totals and a balanced check; query: `as_of`.", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}/statements/income", "Income statement from posted entries; query: `start`, `end`.", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}/statements/balance-sheet", "Balance sheet with explicit dividend/retained-earnings treatment; query: `as_of`.", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}/statements/cash-flow", "Cash flow statement grouped by operating, investing and financing; query: `start`, `end`.", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}/statements/comparison", "Compare two financial periods without asserting causation.", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}/cash-flow", "Current-period cash flow and closing cash.", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}/working-capital", "Cash, receivables, payables, inventory, ratios, DSO/DPO/CCC and cash runway; unavailable values are NOT_CALCULABLE.", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}/profitability", "Profitability, margins, CAC/LTV/payback, ARPC, product contribution and break-even; estimates are labelled.", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}/budgets", "List budget drafts, approved versions and revisions.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/budgets", "Create a draft annual, quarterly, monthly, venture, department, category or product budget.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/budgets/{budget_id}/approval", "Approve or reject a budget with a note; consequential revisions require separate approval.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/budgets/{budget_id}/revisions", "Create a new draft revision while retaining the approved budget.", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}/budgets/{budget_id}/report", "Budget versus approved actuals with variance and overrun status.", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}/forecasts", "List stored forecasts with assumptions, sources, method, confidence and limitations.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/forecasts", "Generate a conservative, base, optimistic or custom forecast; never an actual.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/scenarios", "Generate and store four scenario comparisons over the same horizon.", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}/alerts", "List business-health alerts; query: `status`.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/health/refresh", "Refresh business-health thresholds; missing or stale data produces no alert.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/alerts/{alert_id}/resolve", "Resolve an alert with a note.", authenticated=True),
                ApiRoute("GET", "/finance/ventures/{venture_id}/reports", "List generated management reports.", authenticated=True),
                ApiRoute("POST", "/finance/ventures/{venture_id}/reports", "Generate a grounded management report with evidence and limitations.", authenticated=True),
                # Phase 39: portfolio manager over multiple Phase 38 ventures.
                ApiRoute("GET", "/portfolio/status", "Portfolio service status, readers, limits and no-execution boundaries.", authenticated=True),
                ApiRoute("GET", "/portfolios", "List portfolios visible to the authenticated actor; query: `limit`, `offset`.", authenticated=True),
                ApiRoute("POST", "/portfolios", "Create a portfolio with base currency, objectives, risk preferences and allocation policy.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}", "Portfolio detail with role, visible ventures, restricted count and boundaries.", authenticated=True),
                ApiRoute("PATCH", "/portfolios/{portfolio_id}", "Update portfolio metadata, objectives, reporting period or policies (admin).", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/status", "Change portfolio status; pause/archive requires explicit approval.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/members", "Assign a portfolio role; body: `actor`, `role`.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/fx-rates", "Record a sourced FX rate used only for explicit consolidated reporting.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/monitoring", "Configure monitoring intervals, staleness and alert thresholds.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}/ventures", "List venture references visible to the actor with lifecycle and drill-down links.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/ventures", "Register one existing Phase 38 venture with Phase 35–37 links and optional initial lifecycle.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}/ventures/{venture_id}", "Portfolio venture detail: lifecycle evidence, metrics, source timestamps and missing data.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}/ventures/{venture_id}/lifecycle/evidence", "Read the Phase 35–38 evidence used by lifecycle gates.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/ventures/{venture_id}/lifecycle", "Transition lifecycle only when source evidence and approvals support it.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}/analytics", "Consolidated portfolio analytics with currency, completeness and period rules; query: `start`, `end`.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}/comparison", "Compare venture financial and operational rows without ranking missing data.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/evaluation-policies", "Define a configurable weighted evaluation policy and minimum coverage.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/evaluations", "Evaluate all visible ventures and persist ranked evidence with confidence.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}/ranking", "Read the latest persisted ranking or a live evidence-based ranking; query: `policy`.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}/risks", "Read current assessed and manually recorded risks with correlated exposures.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/risks", "Record a manual risk with evidence, severity and mitigation.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/risks/assess", "Assess financial, liquidity, market, operational and execution risks from source data.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/risks/{risk_id}/resolve", "Resolve a risk with a note; no source record is changed.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}/allocations", "Read allocation recommendation batches, shared resources and conflicts.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/allocations", "Propose allocations from approved budgets and actual evidence; never executes them.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/allocations/{batch_id}/decision", "Approve or reject an allocation recommendation; Phase 38 budget approval remains separate.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}/resources", "Read shared-resource capacity, allocation and utilization.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/resources", "Register a shared resource with a hard capacity and unit.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/resources/{resource_id}/allocations", "Allocate shared capacity; over-capacity requests are refused.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}/scenarios", "List stored simulated scenarios; none is actual performance.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/scenarios", "Simulate launch, delay, spend, acquisition, pause, scale or shortfall assumptions.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}/recommendations", "List advisory strategic recommendation batches.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/recommendations", "Generate advisory recommendations from metrics, risks and allocations.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}/decisions", "List proposed, approved and rejected portfolio decisions.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/decisions", "Propose a decision with evidence, alternatives and implications; no action is taken.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/decisions/{decision_id}/approval", "Approve or reject a proposed decision with a human note.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}/coordination", "Multi-venture coordination: responsibilities, priorities, dependencies, resources and escalations.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/coordination/dependencies", "Record a cross-venture dependency and detect cycles.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}/knowledge", "Read portfolio or venture knowledge with provenance and access filtering.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/knowledge", "Record verified facts, observations, interpretations, hypotheses or recommendations.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/knowledge/{knowledge_id}/promote", "Promote knowledge for cross-venture use with confidentiality authorization.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}/alerts", "List portfolio alerts; query: `status`.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/alerts/refresh", "Refresh threshold alerts while suppressing stale source data.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/alerts/{alert_id}/resolve", "Resolve a portfolio alert with a note.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}/reports", "List immutable portfolio report snapshots.", authenticated=True),
                ApiRoute("POST", "/portfolios/{portfolio_id}/reports", "Generate a 12-section daily, weekly, monthly, quarterly or custom portfolio report.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}/reports/{report_id}", "Read one frozen report snapshot and its source timestamps.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}/dashboard", "Portfolio workspace overview, comparison, capital, risk, strategy and coordination.", authenticated=True),
                ApiRoute("GET", "/portfolios/{portfolio_id}/history", "Portfolio events, decisions and report history.", authenticated=True),
                ApiRoute(
                    "POST",
                    "/ask",
                    "Route a natural-language request through NovaControl; an optional `image` path "
                    "attaches a picture for the vision pipeline to read.",
                    authenticated=True,
                ),
                ApiRoute("POST", "/brain/mode", "Switch the chat brain between auto, llm, scratch, and cloud.", authenticated=True),
                ApiRoute("GET", "/brain/mode", "Inspect the active brain mode and provider.", authenticated=True),
                ApiRoute("GET", "/brain/cloud/presets", "List cloud LLM provider presets (no secrets).", authenticated=True),
                ApiRoute("POST", "/brain/cloud", "Install a cloud LLM (ChatGPT/Gemini/Groq) with a local API key.", authenticated=True),
                ApiRoute("POST", "/brain/cloud/test", "Ping a cloud provider with the pasted key (one tiny completion; nothing is saved).", authenticated=True),
                ApiRoute("POST", "/brain/cloud/clear", "Remove the cloud LLM config and its API key.", authenticated=True),
                ApiRoute("GET", "/brain/ollama/models", "List local Ollama models for the brain model picker (fresh probe).", authenticated=True),
                ApiRoute("POST", "/brain/local/model", "Pin the local brain to a specific Ollama model (empty clears the pick).", authenticated=True),
                ApiRoute("POST", "/chat/clear", "Clear the server-side chat conversation memory and the shared transcript.", authenticated=True),
                ApiRoute("GET", "/chat/history", "Read the shared server-persisted chat thread (same for every browser).", authenticated=True),
                ApiRoute("POST", "/chat/history", "Append a chat turn to the shared thread (action=migrate imports a browser's old history once).", authenticated=True),
                ApiRoute("GET", "/tasks", "List tracked task records.", authenticated=True),
                ApiRoute("POST", "/tasks/delete", "Delete one tracked task record by id.", authenticated=True),
                ApiRoute("POST", "/tasks/clear", "Delete all tracked task records (undoable for a short window).", authenticated=True),
                ApiRoute("POST", "/tasks/clear/undo", "Restore the tasks wiped by a recent /tasks/clear (one-shot token).", authenticated=True),
                ApiRoute("POST", "/improve", "Create a self-improvement plan.", authenticated=True),
                ApiRoute("POST", "/improve/workflow", "Create a friendly self-improvement workflow.", authenticated=True),
                ApiRoute("POST", "/improve/preview", "Run a temporary self-improvement preview.", authenticated=True),
                ApiRoute("POST", "/improve/approve", "Approve a preview for code changes.", authenticated=True),
                ApiRoute("POST", "/learn", "Run a local feedback learning cycle.", authenticated=True),
                ApiRoute("POST", "/knowledge/teach", "Teach: persist a typed fact as durable recallable knowledge.", authenticated=True),
                ApiRoute("GET", "/knowledge", "List taught knowledge, optionally filtered by a query.", authenticated=True),
                ApiRoute("POST", "/knowledge/recall", "Recall taught knowledge matching a query.", authenticated=True),
                # Phase 32: the structured personal knowledge model. These routes
                # read and write the SAME long-term memory store through the
                # application's service; there is no parallel memory database.
                ApiRoute("GET", "/memory/personal/status", "Personal knowledge counts, categories, privacy classes and load state.", authenticated=True),
                ApiRoute("GET", "/memory/personal", "Search structured personal knowledge; query: `query`, `project`, `limit`, `include_*`.", authenticated=True),
                ApiRoute("POST", "/memory/personal", "Acquire one knowledge record with category, kind, source, privacy and validity.", authenticated=True),
                ApiRoute("GET", "/memory/personal/export", "Export the owner's personal knowledge and relationships as JSON.", authenticated=True),
                ApiRoute("POST", "/memory/personal/consolidate", "Consolidate equivalent records while preserving provenance and history.", authenticated=True),
                ApiRoute("POST", "/memory/personal/cleanup", "Apply retention/validity cleanup; expired records remain recoverable.", authenticated=True),
                ApiRoute("GET", "/memory/personal/{record_id}", "Inspect one knowledge record, its history, provenance and relations.", authenticated=True),
                ApiRoute("POST", "/memory/personal/{record_id}/correct", "Correct content while preserving the previous version in history.", authenticated=True),
                ApiRoute("POST", "/memory/personal/{record_id}/delete", "Delete one knowledge record and its relations from the shared store.", authenticated=True),
                ApiRoute("POST", "/memory/personal/{record_id}/resolve", "Resolve a detected contradiction by keeping or withdrawing one record.", authenticated=True),
                ApiRoute("POST", "/memory/personal/relations", "Create or update a provenance-bearing relationship between records.", authenticated=True),
                ApiRoute("POST", "/train", "Run bounded autonomous local learning iterations.", authenticated=True),
                ApiRoute("POST", "/brain/decide", "Trace an utterance through the routing gates with a rung preview.", authenticated=True),
                ApiRoute("POST", "/plan", "Create and optionally execute a plan.", authenticated=True),
                ApiRoute(
                    "POST",
                    "/plan/run",
                    "Run a goal through the agent loop; steps needing approval must be named.",
                    authenticated=True,
                ),
                ApiRoute("POST", "/plan/code", "Plan a coding task: language-aware steps plus a drafted code artifact.", authenticated=True),
                ApiRoute("POST", "/build/save", "Save a drafted Build artifact to the build_workspace folder on disk.", authenticated=True),
                ApiRoute("POST", "/build/project", "Draft a small multi-file project with the coding agent: the configured model, or the sandbox-verified scaffold when none is installed.", authenticated=True),
                ApiRoute("GET", "/build/artifacts", "Saved Build artifacts, newest first, for the Run list.", authenticated=True),
                ApiRoute("POST", "/build/run", "Re-execute a saved Build artifact in the sandbox.", authenticated=True),
                ApiRoute("GET", "/models/status", "What the shared model runtime holds right now, and the memory policy it applies to it.", authenticated=True),
                ApiRoute("POST", "/models/load", "Load a model, unloading whatever must go first to make room for it.", authenticated=True),
                ApiRoute("POST", "/models/unload", "Release one resident model, or every resident model when none is named.", authenticated=True),
                ApiRoute("GET", "/models/routing", "Phase 33 provider/model catalogue, health, routing controls and the last selection.", authenticated=True),
                ApiRoute("POST", "/models/routing/route", "Select the most appropriate model for a task, with eligible and rejected reasons.", authenticated=True),
                ApiRoute("POST", "/models/routing/complete", "Execute one routed completion through the selected adapter with bounded fallback.", authenticated=True),
                ApiRoute("POST", "/models/routing/config", "Set adaptive-routing cost, latency, concurrency and preference controls.", authenticated=True),
                ApiRoute("POST", "/models/routing/providers", "Enable or disable one provider for automatic selection.", authenticated=True),
                ApiRoute("GET", "/models/routing/history", "Recent routing decisions and measured provider/model observations.", authenticated=True),
                ApiRoute("GET", "/models/routing/policy", "The learned routing proposal, active policy and supporting evidence.", authenticated=True),
                ApiRoute("POST", "/models/routing/policy/activate", "Activate a validated learned routing policy (previous policy retained).", authenticated=True),
                ApiRoute("POST", "/models/routing/policy/rollback", "Roll back to the routing policy active before the last activation.", authenticated=True),
                ApiRoute("POST", "/command/plan", "Plan a natural desktop or phone command.", authenticated=True),
                ApiRoute("POST", "/command/execute", "Execute an approved natural desktop or phone command.", authenticated=True),
                ApiRoute("POST", "/desktop/plan", "Plan an approval-gated desktop action.", authenticated=True),
                ApiRoute("POST", "/desktop/execute", "Execute an approved desktop action.", authenticated=True),
                ApiRoute("POST", "/browser/plan", "Plan an approval-gated browser action (navigate, search + extract).", authenticated=True),
                ApiRoute("POST", "/browser/execute", "Execute an approved browser action (search returns the top results).", authenticated=True),
                ApiRoute("GET", "/phone/status", "Inspect phone bridge status.", authenticated=True),
                ApiRoute("POST", "/phone/connect", "Run the phone bridge pairing flow.", authenticated=True),
                ApiRoute("GET", "/vision/status", "Vision capability report: model availability and open bug count.", authenticated=True),
                ApiRoute("POST", "/vision/model", "Install a multimodal vision model (ollama or a cloud provider) for element location.", authenticated=True),
                ApiRoute("POST", "/vision/model/clear", "Remove the configured vision model; element location returns to OCR-only.", authenticated=True),
                ApiRoute("POST", "/vision/describe", "Capture the screen and describe it (vision model or window probe).", authenticated=True),
                ApiRoute("POST", "/vision/click", "Vision-locate a labeled element on screen, click it, and verify.", authenticated=True),
                # NovaForge NF-01: a gaming session OBSERVES one validated window.
                # There is no decision or input route here because NF-01 attaches
                # neither — the surface says exactly what the session manager does.
                ApiRoute("GET", "/novaforge/status", "NovaForge readiness, saved mission draft and the active session.", authenticated=True),
                # NF-12: discovery and the VERIFIED launch lifecycle. A row here
                # is installation evidence; `tested` is the only field that says
                # a launch actually ran.
                ApiRoute("GET", "/novaforge/library", "Discovered games with installation evidence, launchability status and this process's own launch claim; query parameter: `q`.", authenticated=True),
                ApiRoute("GET", "/novaforge/library/resolve", "One game name's catalog status without launching anything; query parameter: `name`.", authenticated=True),
                ApiRoute("POST", "/novaforge/library/launch", "Preview a verified game launch and mint its approval token, or execute the plan that token approved (`name`/`command`, `approval_token`).", authenticated=True),
                ApiRoute("POST", "/novaforge/library/close", "Close only the instance this process started (graceful window close) and confirm it exited.", authenticated=True),
                ApiRoute("POST", "/novaforge/library/attach", "Report a game that was already running without taking ownership of it.", authenticated=True),
                ApiRoute("GET", "/novaforge/windows", "Open windows for the NovaForge target picker, or why they cannot be read.", authenticated=True),
                ApiRoute("GET", "/novaforge/mission", "Read the persisted NovaForge mission draft.", authenticated=True),
                ApiRoute("POST", "/novaforge/mission", "Persist a NovaForge mission draft and report its field validation.", authenticated=True),
                ApiRoute("POST", "/novaforge/validate", "Validate a mission and its live target window without starting a session.", authenticated=True),
                ApiRoute("GET", "/novaforge/sessions", "Stored NovaForge sessions, newest first.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions", "Start a validated NovaForge observation session.", authenticated=True),
                ApiRoute("GET", "/novaforge/sessions/{session_id}", "One NovaForge session: state, frames, observations and stages.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/pause", "Pause a running session and wait for the loop to acknowledge.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/resume", "Resume a paused session after re-validating its target window.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/stop", "Cancel a session, release input and persist its final record.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/cancel", "Abort the session loop now, release input and confirm the outcome (idempotent).", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/emergency-stop", "Guardian emergency stop: bounded abort, input cleanup and the confirmed result.", authenticated=True),
                ApiRoute("GET", "/novaforge/capabilities", "Per-capability availability with the exact blocker: capture, action execution, Guardian enforcement, objective verification, persistence, decision pipeline, vision interpretation, game launch.", authenticated=True),
                ApiRoute("GET", "/novaforge/trials", "Policy trial rows and aggregates from runs finished in this process; refused, failed and inconclusive trials are kept.", authenticated=True),
                ApiRoute("GET", "/novaforge/observation", "The unified game observation pipeline: bound target, state, measured rate and latency, the last records, pause reason and bounded history.", authenticated=True),
                ApiRoute("POST", "/novaforge/observation/start", "Bind one target window and start the bounded observation schedule; body: `title`, optional `game`, `pid`, `start_time`, `process`, `interval`.", authenticated=True),
                ApiRoute("POST", "/novaforge/observation/pause", "Pause a user-requested observation (never auto-cleared).", authenticated=True),
                ApiRoute("POST", "/novaforge/observation/resume", "Resume a user pause after re-reading the pinned target identity.", authenticated=True),
                ApiRoute("POST", "/novaforge/observation/stop", "Stop the observation schedule and clean up (idempotent).", authenticated=True),
                ApiRoute("POST", "/novaforge/observation/observe", "Capture and validate one frame now; a concurrent capture answers `busy` and starts nothing.", authenticated=True),
                ApiRoute("GET", "/novaforge/observation/frame", "Serve the newest validated observation frame (PNG); 404 when nothing was validated.", authenticated=True),
                ApiRoute("GET", "/novaforge/state", "The NF-16 game-state reader: last structured state, bounded history, profile table, measured processing latency, counts and refusals.", authenticated=True),
                ApiRoute("POST", "/novaforge/state/analyze", "Read one game state from the newest VALIDATED observation; body may pin `profile_id`. A stale, missing or superseded observation is a refusal with a reason code.", authenticated=True),
                ApiRoute("POST", "/novaforge/state/reset", "Forget the state comparison baseline (history is kept).", authenticated=True),
                ApiRoute("GET", "/novaforge/state/profiles", "The perception profiles this build ships, including rejected ones and why.", authenticated=True),
                ApiRoute("GET", "/novaforge/objectives", "The NF-17 objective picture: understood objectives, evidence, bounded history, diagnostics and measured interpretation latency.", authenticated=True),
                ApiRoute("POST", "/novaforge/objectives/reset", "Forget the current objectives and the observation-ordering baseline; history and events are kept.", authenticated=True),
                ApiRoute("GET", "/novaforge/objectives/profiles", "The objective rules this build ships, including rejected profiles and why.", authenticated=True),
                ApiRoute("GET", "/novaforge/knowledge", "NF-18 game knowledge: per-game counts, revalidation due, the transient session memory and why it is not persisted; query parameter: `game`.", authenticated=True),
                ApiRoute("GET", "/novaforge/knowledge/records", "Knowledge records for ONE game with their evidence and status; query parameters: `game` (required), `category`, `status`, `q`, `limit`.", authenticated=True),
                ApiRoute("POST", "/novaforge/knowledge", "Record one explicit evidence-bearing knowledge candidate; an invalid category, content or evidence reference is a 422.", authenticated=True),
                ApiRoute("POST", "/novaforge/knowledge/{knowledge_id}/revalidate", "Re-check one stale or contradicted record with fresh evidence; a record that needs no revalidation is reported, not changed.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/objective", "Attach a checkable objective (window title) to a live session; unsupported kinds are recorded and reported unsupported.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/actions/plan", "Validate and digest-bind one gameplay action without sending it; a denial carries the Guardian's reason.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/actions/execute", "Execute one digest-bound action proposal after revalidating target identity, foreground, policy and budget.", authenticated=True),
                ApiRoute("GET", "/novaforge/sessions/{session_id}/control", "NF-19 control-interface state: bounds, gate decision, held inputs, the last result and why nothing is running.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/control", "Run ONE typed, bounded input action (`kind` plus its parameters); a refusal is a 200 carrying its code, and completion never means the game reacted.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/control/cancel", "Ask the in-flight control action to stop at its next checkpoint and release what it holds.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/control/release", "Release every input the control interface holds (idempotent; refused while an action is in flight).", authenticated=True),
                ApiRoute("GET", "/novaforge/sessions/{session_id}/plan", "NF-20 planning state: declared rules, the current plan, its candidates and rationale, bounds and the gate.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/plan", "Build ONE bounded plan from verified state, objective and knowledge; nothing is sent, and a missing or stale reading is a refusal with a code.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/plan/execute", "Execute ONE approved plan step by step through NF-19 (`plan_id`, `approved`, `approval_token` from the plan preview); 403 without the single-use token, stops on refusal, failure, staleness or objective change, and never claims an effect.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/plan/cancel", "Cancel the session's pending or running plan and its in-flight input operation (idempotent).", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/plan/replan", "Regenerate a stopped plan from fresh evidence within its bound (`plan_id`, `reason` from the declared triggers); the previous plan is invalidated first.", authenticated=True),
                ApiRoute("GET", "/novaforge/sessions/{session_id}/outcomes", "NF-21 outcome verification state: declared rules, the last verification run, recorded outcomes, recovery counts and the policy's bounds.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/outcomes/verify", "Verify ONE plan's dispatched steps against fresh post-action observations (`plan_id`, `max_steps`); each step is verified_success, verified_failure, partial, unknown, timed out, aborted or not_verifiable, and missing evidence is never reported as a failure.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/outcomes/recover", "Carry out one outcome's recorded recovery decision (`outcome_id`, `approve`); side-effecting responses need explicit approval, unknown outcomes trigger a fresh bounded observation rather than a repeated action, and no recovery path dispatches input.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/outcomes/cancel", "Ask an in-flight outcome verification to stop at its next checkpoint (idempotent).", authenticated=True),
                ApiRoute("GET", "/novaforge/sessions/{session_id}/autonomy", "NF-22 autonomous run state: phase, outcome, pinned objective and completion criteria, current plan and plan-approval status, dispatched actions, last verification, recovery/replan counts, the declared budgets, the resource reading and the sessions a run may drive.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/autonomy/start", "Begin ONE bounded autonomous run over a live session (`approved`, `note`, `task`, `limits`); the operator's approval is the gate, every limit may be narrowed but never widened, and without approval the answer is `approval_required` with nothing started.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/autonomy/pause", "Pause an autonomous run (`reason`); the session pauses and the next cycle re-observes rather than continuing from a pre-pause reading.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/autonomy/resume", "Resume a paused autonomous run; a run that already ended is never restarted.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/autonomy/cancel", "Cancel an autonomous run before its next stage (`reason`) and let it end cleanly, releasing the session's input through the session's own stop path.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/autonomy/approve", "Answer a run parked in WAITING_FOR_APPROVAL (`approved`, `note`); one answer covers exactly one plan and the next plan parks again.", authenticated=True),
                ApiRoute("GET", "/novaforge/autonomy/trace", "The complete autonomous orchestration history (query parameter: `session_id`): every phase move with its reason, observation and state references, objective readings, retrieved knowledge references, plans, approvals, execution results, verification evidence and recovery decisions.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/verify", "Run the independent objective verifier and record its verdict and evidence.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/loop", "Run the bounded closed loop once: observe, decide, plan, execute and verify through the session's own gates; query body: `max_actions`, `deadline_seconds`, `verify_first`.", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/loop/cancel", "Ask a running closed loop to stop before its next action (idempotent).", authenticated=True),
                ApiRoute("POST", "/novaforge/sessions/{session_id}/vision/interpret", "Describe the target's current frame with the configured vision model (read-only); returns the typed interpretation, its evidence references, latency and conflicts, or the exact reason no interpretation exists.", authenticated=True),
                ApiRoute("GET", "/novaforge/sessions/{session_id}/verification", "The objective, its verdict, evidence references and action counts for one session.", authenticated=True),
                ApiRoute("GET", "/novaforge/sessions/{session_id}/telemetry", "Measured telemetry and Guardian status for one session.", authenticated=True),
                ApiRoute("GET", "/novaforge/sessions/{session_id}/timeline", "Read-only evidence timeline for one session (filterable, paged).", authenticated=True),
                ApiRoute("GET", "/novaforge/sessions/{session_id}/timeline/{seq}", "One timeline event with its evidence resolved.", authenticated=True),
                ApiRoute("GET", "/novaforge/timeline/compare", "Compare two persisted sessions from their recorded evidence; query: `left`, `right`.", authenticated=True),
                ApiRoute("GET", "/novaforge/frames/{session_id}", "Serve one captured frame (PNG); query parameter: `seq`.", authenticated=True),
                # NF-04: the benchmark lab is a consumer of the session manager.
                # A run is one real observation session plus a persisted result;
                # unsupported gameplay scenarios stay visible and unstartable.
                ApiRoute("GET", "/novaforge/benchmarks/scenarios", "Benchmark scenario catalog with supported/unsupported status, verifiers and metric definitions.", authenticated=True),
                ApiRoute("GET", "/novaforge/benchmarks/scenarios/{scenario_id}", "One benchmark scenario's requirements and definitions; query parameter: `version`.", authenticated=True),
                ApiRoute("POST", "/novaforge/benchmarks/preflight", "Validate a benchmark scenario and its live target window without starting a run.", authenticated=True),
                ApiRoute("GET", "/novaforge/benchmarks/runs", "Persisted benchmark runs, newest first; query parameters: `limit`, `scenario`.", authenticated=True),
                ApiRoute("POST", "/novaforge/benchmarks/runs", "Start one bounded benchmark observation run.", authenticated=True),
                ApiRoute("GET", "/novaforge/benchmarks/runs/{run_id}", "One benchmark run: configuration, metrics, evidence and limitations.", authenticated=True),
                ApiRoute("POST", "/novaforge/benchmarks/runs/{run_id}/cancel", "Cancel an active benchmark run through its session's stop path (idempotent).", authenticated=True),
                ApiRoute("GET", "/novaforge/benchmarks/compare", "Compare compatible benchmark runs from recorded results; query: `left`, `right`.", authenticated=True),
                ApiRoute("GET", "/novaforge/benchmarks/aggregate", "Per-scenario aggregate metrics for eligible runs; query: `scenario`, `version`.", authenticated=True),
                # NF-05: controlled game evaluation. An evaluation is a compatibility
                # record around one NF-04 benchmark run; no route starts a game.
                ApiRoute("GET", "/novaforge/games", "Game/environment compatibility registry with honest capability counts.", authenticated=True),
                ApiRoute("GET", "/novaforge/games/{game_id}", "One environment's compatibility, capabilities and scenarios; query parameter: `version`.", authenticated=True),
                ApiRoute("GET", "/novaforge/evaluations", "Retained evaluation attempts, newest first; query parameters: `limit`, `game`.", authenticated=True),
                ApiRoute("GET", "/novaforge/evaluations/baseline", "Current baseline for an environment/scenario; query parameters: `game`, `scenario`, `version`.", authenticated=True),
                ApiRoute("POST", "/novaforge/evaluations/preflight", "Judge every readiness check against the live window without starting an evaluation.", authenticated=True),
                ApiRoute("POST", "/novaforge/evaluations", "Start one confirmed evaluation: an observation run, or a controlled plan approved by the digest preflight issued.", authenticated=True),
                ApiRoute("GET", "/novaforge/evaluations/{evaluation_id}", "One evaluation: readiness, benchmark run, baseline and status.", authenticated=True),
                ApiRoute("POST", "/novaforge/evaluations/{evaluation_id}/cancel", "Cancel an active evaluation through its session's stop path (idempotent).", authenticated=True),
                ApiRoute("GET", "/novaforge/evaluations/{evaluation_id}/report", "Per-game evaluation report with evidence links, missing evidence and limitations.", authenticated=True),
                # NF-06: the Forge Coach analyses recorded evidence and plans ONE
                # declared experiment at a time. Approval is bound to the exact
                # configuration, and nothing here trains, tunes or sends input.
                ApiRoute("GET", "/novaforge/coach", "Coach capability card: sources, classifications, experiment rules and honest limits.", authenticated=True),
                ApiRoute("POST", "/novaforge/coach/analyse", "Evidence-backed analysis of one recorded session or evaluation (read-only; findings are recomputed).", authenticated=True),
                ApiRoute("GET", "/novaforge/coach/recommendations", "Persisted recommendation statuses (operator decisions), newest first.", authenticated=True),
                ApiRoute("POST", "/novaforge/coach/recommendations/{recommendation_id}", "Record an operator decision (proposed/approved/rejected/…) about one recommendation.", authenticated=True),
                ApiRoute("GET", "/novaforge/experiments", "Retained controlled experiments, newest first; query parameters: `limit`, `status`.", authenticated=True),
                ApiRoute("POST", "/novaforge/experiments", "Propose an experiment from a recommendation or an explicit spec; starts nothing.", authenticated=True),
                ApiRoute("GET", "/novaforge/experiments/{experiment_id}", "One experiment: spec, approval, arms, results and history.", authenticated=True),
                ApiRoute("POST", "/novaforge/experiments/{experiment_id}/approve", "Approve the CURRENT configuration (its digest is bound to the approval).", authenticated=True),
                ApiRoute("POST", "/novaforge/experiments/{experiment_id}/reject", "Reject a proposal; the rejection is retained with its note.", authenticated=True),
                ApiRoute("POST", "/novaforge/experiments/{experiment_id}/run", "Run both arms through the evaluation lab (background, bounded, approval-checked).", authenticated=True),
                ApiRoute("POST", "/novaforge/experiments/{experiment_id}/cancel", "Cancel an experiment and its active arm (idempotent once terminal).", authenticated=True),
                ApiRoute("GET", "/novaforge/experiments/{experiment_id}/results", "The comparison and verdict, or an explicit not-available-yet answer.", authenticated=True),
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
                    "Perception capability classification with live availability "
                    "on this machine.",
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
                ApiRoute("GET", "/intelligence", "Global Intelligence Layer: telemetry, findings, and capabilities.", authenticated=True),
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
                ApiRoute("GET", "/bugs", "List recorded bugs (what failed, where, and when).", authenticated=True),
                ApiRoute("POST", "/bugs/{bug_id}/fix", "Mark a recorded bug as fixed.", authenticated=True),
                ApiRoute("POST", "/bugs/clear-fixed", "Remove all bugs already marked fixed.", authenticated=True),
                ApiRoute("POST", "/phone/plan", "Plan an approval-gated phone action.", authenticated=True),
                ApiRoute("POST", "/phone/execute", "Execute an approved phone action.", authenticated=True),
                ApiRoute("POST", "/agent/run", "Run the full agentic loop for a natural-language goal.", authenticated=True),
                ApiRoute("GET", "/agent/metrics", "Agentic evaluation metrics (success, recovery, verification rates).", authenticated=True),
                ApiRoute("GET", "/agent/knowledge", "Application knowledge graph: workflows, confidence, freshness.", authenticated=True),
                ApiRoute("POST", "/explore", "Research a topic and return an Explore report.", authenticated=True),
                ApiRoute("GET", "/explore/trending", "Current daily research topics from live top-story news (rotating window).", authenticated=True),
                ApiRoute("GET", "/activity", "Recent completed actions (commands, research, learning) for the web timeline.", authenticated=True),
                ApiRoute("GET", "/events/stream", "Live activity channel: the application EventBus as SSE."),
                ApiRoute("GET", "/settings", "Read local user settings.", authenticated=True),
                ApiRoute("POST", "/settings", "Update local user settings.", authenticated=True),
                ApiRoute("GET", "/plugins", "List plugin API records.", authenticated=True),
                # -- Phase 13: scheduled work and the audit trail ------------------
                # Scheduling stores a request plus its schedule and runs NOTHING:
                # the stored request still travels intent -> decision -> plan ->
                # permission -> execution when its time comes (or when run now),
                # so "run" here is the same gate a due run passes.
                ApiRoute("GET", "/automation", "Scheduled automations: what is stored, armed, and due next.", authenticated=True),
                ApiRoute("POST", "/automation/schedule", "Store a request plus the schedule it states as an automation (nothing runs yet).", authenticated=True),
                ApiRoute("POST", "/automation/approve", "Authorize a pending automation so it may be armed.", authenticated=True),
                ApiRoute("POST", "/automation/cancel", "Cancel an automation so it never runs again.", authenticated=True),
                ApiRoute("POST", "/automation/enable", "Arm an approved automation again.", authenticated=True),
                ApiRoute("POST", "/automation/disable", "Disarm an automation without cancelling it.", authenticated=True),
                ApiRoute("POST", "/automation/run", "Run a stored automation now, under the same permission gate as a due run.", authenticated=True),
                ApiRoute("POST", "/automation/run-due", "Run every automation that is due (the scheduler's own tick).", authenticated=True),
                # -- Phase 30: persistent objectives and personal operations ----
                # These routes reach real state changes; the board is stored, the
                # tick is the same one the background loop runs, and every task
                # that executes passes the shared permission gate.
                ApiRoute("GET", "/operations", "Persistent operations board: objectives, task counts, monitoring conclusions and notification posture.", authenticated=True),
                ApiRoute("GET", "/objectives", "Every stored objective with its milestones, tasks, progress and checkpoints.", authenticated=True),
                ApiRoute("POST", "/objectives", "Create an ongoing objective with an intended outcome and autonomy level.", authenticated=True),
                ApiRoute("POST", "/objectives/update", "Edit an objective's title, outcome, priority, deadline, autonomy or criteria.", authenticated=True),
                ApiRoute("POST", "/objectives/delete", "Delete one objective and its stored work.", authenticated=True),
                ApiRoute("POST", "/objectives/control", "Pause, resume, cancel, prioritize, re-deadline or re-level an objective.", authenticated=True),
                ApiRoute("POST", "/objectives/tasks", "Add a task (with dependencies, steps, due time or recurrence) to an objective.", authenticated=True),
                ApiRoute("POST", "/objectives/milestones", "Add a milestone over a set of tasks.", authenticated=True),
                ApiRoute("POST", "/objectives/run", "Run the ready work of one objective under its autonomy and the permission layer.", authenticated=True),
                ApiRoute("POST", "/objectives/tasks/decide", "Approve, reject, retry or cancel one objective task.", authenticated=True),
                ApiRoute("GET", "/operations/notifications", "Stored operations notifications, newest first, with preferences.", authenticated=True),
                ApiRoute("POST", "/operations/notifications/read", "Acknowledge one notification, or all of them.", authenticated=True),
                ApiRoute("POST", "/operations/notifications/clear", "Delete stored notifications.", authenticated=True),
                ApiRoute("POST", "/operations/notifications/preferences", "Choose which notification kinds are delivered and how long duplicates are suppressed.", authenticated=True),
                ApiRoute("POST", "/operations/tick", "Run one monitoring/execution pass (the background loop's body).", authenticated=True),
                # The audit trail is local by construction and redacted on the way
                # in; these routes read and bound it, and nothing here reaches the
                # model or the network.
                ApiRoute("GET", "/audit", "Operational audit trail status: records, retention, local-only storage, redactions.", authenticated=True),
                ApiRoute("GET", "/audit/entries", "Recent audit records (redacted), oldest first.", authenticated=True),
                ApiRoute("POST", "/audit/prune", "Apply the retention policy and report what was removed.", authenticated=True),
                ApiRoute("POST", "/audit/delete", "Delete one audit record by id.", authenticated=True),
                ApiRoute("POST", "/audit/clear", "Delete every audit record (explicit, not retention).", authenticated=True),
                # Phase 14: the execution mode and privacy controls, the resource
                # governor's reading, the cost estimate, the measured model
                # comparison and the component roster.
                ApiRoute("GET", "/privacy", "Execution mode, privacy controls and every outbound decision.", authenticated=True),
                ApiRoute("POST", "/privacy", "Change the execution mode and/or a privacy control, live.", authenticated=True),
                ApiRoute("GET", "/resources", "The resource governor's reading of this machine, with its reasons.", authenticated=True),
                ApiRoute("POST", "/cost/estimate", "Estimate a request's cost and routing hint (never a gate).", authenticated=True),
                ApiRoute("GET", "/diagnostics", "Run the component roster, or a comma-separated subset via ?only=.", authenticated=True),
                ApiRoute("GET", "/benchmark", "Stored model measurements and the measured comparison per category.", authenticated=True),
                ApiRoute("POST", "/benchmark", "Measure a model on the given tasks through the live provider.", authenticated=True),
                # Phase 15: what was recorded, how it was scored, and what it was
                # worth. Read-only on purpose — this surface reports evidence,
                # it never runs a task, calls a model or drops a row.
                ApiRoute("GET", "/evaluation/summary", "Recorded trajectories, quality verdicts, scores and retention, at a glance.", authenticated=True),
                ApiRoute("GET", "/evaluation/trajectory/{trajectory_id}", "One stored trajectory with its evaluation and reward breakdown.", authenticated=True),
                ApiRoute("GET", "/evaluation/metrics", "Aggregate metrics over stored trajectories (success, latency p50/p95, reward, routing).", authenticated=True),
                ApiRoute("GET", "/evaluation/rewards", "Stored rewards, newest first, with their component and penalty breakdowns.", authenticated=True),
                # Phase 16: supervised fine-tuning. Datasets are built from what
                # Phase 15 recorded; runs are started, paused, resumed and
                # cancelled explicitly; a trained adapter only ever becomes a
                # candidate until a human approves and promotes it.
                ApiRoute("GET", "/training/status", "Training subsystem state: enabled, dry-run, backend, capabilities.", authenticated=True),
                ApiRoute("GET", "/training/summary", "Datasets, runs, checkpoints and models, at a glance.", authenticated=True),
                ApiRoute("POST", "/training/estimate", "Estimate a training config's resources without starting anything.", authenticated=True),
                ApiRoute("GET", "/training/datasets", "Stored fine-tuning dataset versions, newest first.", authenticated=True),
                ApiRoute("POST", "/training/datasets", "Build a dataset version from stored trajectories, deterministically split.", authenticated=True),
                ApiRoute("POST", "/training/datasets/validate", "Validate a dataset version: leakage, duplicates, empty splits.", authenticated=True),
                ApiRoute("GET", "/training/datasets/{dataset_version_id}", "One dataset version with its split counts and provenance.", authenticated=True),
                ApiRoute("GET", "/training/runs", "Training runs, newest first, with their status and resource verdict.", authenticated=True),
                ApiRoute("POST", "/training/runs", "Create a training run from a config (never starts it).", authenticated=True),
                ApiRoute("POST", "/training/runs/start", "Start a run: refused when the estimate is unsafe, real training needs confirmation.", authenticated=True),
                ApiRoute("POST", "/training/runs/pause", "Pause a running training run at the next checkpoint boundary.", authenticated=True),
                ApiRoute("POST", "/training/runs/cancel", "Cancel a run; its checkpoints are kept for inspection.", authenticated=True),
                ApiRoute("POST", "/training/runs/resume", "Resume an interrupted run from its newest valid checkpoint.", authenticated=True),
                ApiRoute("POST", "/training/runs/re-estimate", "Re-estimate a run's resources against the current machine reading.", authenticated=True),
                ApiRoute("POST", "/training/runs/evaluate", "Compare a run's candidate against its base model and record the verdict.", authenticated=True),
                ApiRoute("GET", "/training/runs/{run_id}/checkpoints", "A run's checkpoints with their validity (complete, partial, corrupt).", authenticated=True),
                ApiRoute("GET", "/training/runs/{run_id}", "One training run: config, metrics, progress and checkpoint list.", authenticated=True),
                ApiRoute("GET", "/training/evaluations", "Recorded before/after evaluations with their regression checks.", authenticated=True),
                ApiRoute("GET", "/training/models", "Fine-tuned candidates and registered models, with their lifecycle status.", authenticated=True),
                ApiRoute("GET", "/training/models/{model_id}", "One registered model: adapter metadata, lineage and evaluation history.", authenticated=True),
                ApiRoute("POST", "/training/models/approve", "Approve a candidate that has a recorded passing evaluation (explicit only).", authenticated=True),
                ApiRoute("POST", "/training/models/promote", "Promote an approved model to production, demoting the previous one.", authenticated=True),
                ApiRoute("POST", "/training/models/reject", "Reject a candidate with a recorded reason.", authenticated=True),
                ApiRoute("POST", "/training/models/deprecate", "Deprecate a registered model so it is no longer selected.", authenticated=True),
                ApiRoute("POST", "/training/models/rollback", "Roll a deployment back to the model a promotion replaced.", authenticated=True),
                # Phase 17: preference optimization. Pairs are built from what
                # Phase 15 recorded or submitted by a reviewer; a DPO/ORPO run is
                # created, priced, dry-run and started explicitly; and a
                # preference model is registered in the SAME registry, so
                # approving and promoting it use the /training/models routes.
                ApiRoute("GET", "/preference/status", "Preference subsystem state: datasets, runs, reviews, readiness.", authenticated=True),
                ApiRoute("GET", "/preference/summary", "Datasets, runs, reviews and objectives, at a glance.", authenticated=True),
                ApiRoute("GET", "/preference/algorithms", "The DPO and ORPO objectives, what each costs, and readiness here.", authenticated=True),
                ApiRoute("POST", "/preference/estimate", "Estimate a preference config's resources without starting anything.", authenticated=True),
                ApiRoute("POST", "/preference/dry-run", "Validate a dataset, config and output directory, and price the run — starting nothing.", authenticated=True),
                ApiRoute("GET", "/preference/datasets", "Stored preference dataset versions, newest first.", authenticated=True),
                ApiRoute("POST", "/preference/datasets", "Build a preference dataset version from observed behaviour, deterministically split.", authenticated=True),
                ApiRoute("POST", "/preference/datasets/validate", "Validate a preference dataset: leakage, contradictions, provenance, quality.", authenticated=True),
                ApiRoute("GET", "/preference/datasets/{dataset_version_id}", "One preference dataset version with its splits and statistics.", authenticated=True),
                ApiRoute("GET", "/preference/datasets/{dataset_version_id}/pairs/{preference_id}", "One preference pair: both candidates, evidence, outcomes and split.", authenticated=True),
                ApiRoute("GET", "/preference/reviews", "Pairs waiting on a human reviewer, with both candidates side by side.", authenticated=True),
                ApiRoute("GET", "/preference/reviews/{preference_id}", "One queued pair as a reviewer sees it.", authenticated=True),
                ApiRoute("POST", "/preference/reviews/submit", "Submit a preference a person is asserting (choose A/B, or record a pair).", authenticated=True),
                ApiRoute("POST", "/preference/reviews/decide", "Settle a queued pair: choose A, choose B, mark a tie, or reject it.", authenticated=True),
                ApiRoute("GET", "/preference/runs", "Preference runs, newest first, filtered by objective.", authenticated=True),
                ApiRoute("POST", "/preference/runs", "Create a DPO/ORPO run from a config (never starts it).", authenticated=True),
                ApiRoute("POST", "/preference/runs/start", "Start a preference run: unsafe estimates and unconfirmed real runs are refused.", authenticated=True),
                ApiRoute("POST", "/preference/runs/pause", "Pause a running preference run at the next step boundary.", authenticated=True),
                ApiRoute("POST", "/preference/runs/cancel", "Cancel a preference run; its checkpoints are kept.", authenticated=True),
                ApiRoute("POST", "/preference/runs/resume", "Resume an interrupted preference run from its newest valid checkpoint.", authenticated=True),
                ApiRoute("POST", "/preference/runs/re-estimate", "Re-estimate a run's resources against the current machine reading.", authenticated=True),
                ApiRoute("POST", "/preference/runs/evaluate", "Compare base, SFT and candidate on the held-out pairs and record the verdict.", authenticated=True),
                ApiRoute("POST", "/preference/compare", "Compare three models on a pair dataset without needing a run.", authenticated=True),
                ApiRoute("GET", "/preference/runs/{run_id}/checkpoints", "A preference run's checkpoints with their validity.", authenticated=True),
                ApiRoute("GET", "/preference/runs/{run_id}", "One preference run: algorithm, config, metrics and checkpoints.", authenticated=True),
                ApiRoute("GET", "/preference/evaluations", "Recorded preference comparisons with their regression checks.", authenticated=True),
                ApiRoute("GET", "/preference/models", "Registered preference models, filtered by objective (same registry as training).", authenticated=True),
                ApiRoute("GET", "/preference/models/{model_id}", "One registered preference model, with the objective that produced it.", authenticated=True),
                # Phase 18: RLHF / RLAIF. Feedback (human and AI) becomes a
                # reward signal, datasets are built from it with integrity
                # checks, and an RL run is created, priced, simulated and
                # started explicitly. An RL model registers in the SAME
                # registry, so approving and promoting use /training/models.
                ApiRoute("GET", "/rlhf/status", "RLHF/RLAIF state: feedback, ratings, datasets, runs, readiness.", authenticated=True),
                ApiRoute("GET", "/rlhf/summary", "Feedback, ratings, datasets and runs, at a glance.", authenticated=True),
                ApiRoute("GET", "/rlhf/algorithms", "The RL modes and policy optimizers, and what this machine can do.", authenticated=True),
                ApiRoute("POST", "/rlhf/estimate", "Estimate an RL config's resources without starting anything.", authenticated=True),
                ApiRoute("POST", "/rlhf/dry-run", "Validate, price, plan and simulate an RL run — starting nothing.", authenticated=True),
                ApiRoute("POST", "/rlhf/pipeline", "The stage-by-stage RLHF/RLAIF plan for a configuration and dataset.", authenticated=True),
                ApiRoute("GET", "/rlhf/feedback", "Human feedback rows, newest first, with their verdicts.", authenticated=True),
                ApiRoute("POST", "/rlhf/feedback", "Submit human feedback for a trajectory (never deletes; quality-filtered).", authenticated=True),
                ApiRoute("POST", "/rlhf/feedback/{feedback_id}/decide", "Settle a held feedback row: accept keeps it usable, reject does not.", authenticated=True),
                ApiRoute("POST", "/rlhf/rate", "Ask an evaluator for a structured rating of observable facts.", authenticated=True),
                ApiRoute("GET", "/rlhf/ratings", "Stored AI ratings, newest first, with the source breakdown.", authenticated=True),
                ApiRoute("GET", "/rlhf/disagreements", "Recorded human-vs-AI disagreements, optionally detecting new ones.", authenticated=True),
                ApiRoute("GET", "/rlhf/datasets", "Reward dataset versions, newest first, filtered by mode or name.", authenticated=True),
                ApiRoute("POST", "/rlhf/datasets", "Build a reward dataset version from feedback and ratings, split deterministically.", authenticated=True),
                ApiRoute("GET", "/rlhf/datasets/{dataset_version_id}/validate", "Validate a reward dataset: provenance, integrity, splits and leakage.", authenticated=True),
                ApiRoute("GET", "/rlhf/datasets/{dataset_version_id}/held", "Rows a dataset held back, and why they were excluded.", authenticated=True),
                ApiRoute("GET", "/rlhf/datasets/{dataset_version_id}", "One reward dataset version with its splits and statistics.", authenticated=True),
                ApiRoute("GET", "/rlhf/runs", "RLHF/RLAIF runs, newest first, filtered by mode or status.", authenticated=True),
                ApiRoute("POST", "/rlhf/runs", "Create an RL run from a config (never starts it; rewards are audited first).", authenticated=True),
                ApiRoute("POST", "/rlhf/runs/start", "Start an RL run; unsafe estimates and unconfirmed real runs are refused.", authenticated=True),
                ApiRoute("POST", "/rlhf/runs/pause", "Pause a running RL run at the next step boundary.", authenticated=True),
                ApiRoute("POST", "/rlhf/runs/cancel", "Cancel an RL run; its checkpoints are kept.", authenticated=True),
                ApiRoute("POST", "/rlhf/runs/resume", "Resume an interrupted RL run from its newest valid checkpoint.", authenticated=True),
                ApiRoute("POST", "/rlhf/runs/re-estimate", "Re-estimate a run's resources against the current machine reading.", authenticated=True),
                ApiRoute("POST", "/rlhf/runs/evaluate", "Compare base, SFT, preference and RL candidate on held-out data.", authenticated=True),
                ApiRoute("POST", "/rlhf/compare", "Compare up to four models on a reward dataset without needing a run.", authenticated=True),
                ApiRoute("GET", "/rlhf/runs/{run_id}/checkpoints", "An RL run's checkpoints with their validity and loadability.", authenticated=True),
                ApiRoute("GET", "/rlhf/runs/{run_id}", "One RL run: mode, algorithm, config, metrics and checkpoints.", authenticated=True),
                ApiRoute("GET", "/rlhf/evaluations", "Recorded RL comparisons with their regression checks.", authenticated=True),
                ApiRoute("GET", "/rlhf/models", "Registered RL models, filtered by mode (same registry as training).", authenticated=True),
                ApiRoute("GET", "/rlhf/models/{model_id}", "One registered RL model, with the mode that produced it.", authenticated=True),
                # Phase 19: RLVR + critique-based learning. Verifiable rewards
                # come from registered deterministic verifiers, failures become
                # structured critiques, corrections become datasets, and an
                # RLVR run is created, priced, planned and started explicitly.
                # An RLVR model registers in the SAME registry, so approving
                # and promoting use /training/models.
                ApiRoute("GET", "/rlvr/status", "RLVR state: verifiers, critiques, corrections, datasets, runs.", authenticated=True),
                ApiRoute("GET", "/rlvr/summary", "The same, plus the newest critiques, corrections and datasets.", authenticated=True),
                ApiRoute("GET", "/rlvr/verifiers", "Registered verifiers with categories, versions and integrity state.", authenticated=True),
                ApiRoute("POST", "/rlvr/verifiers/disable", "Stop a verifier supporting rewards (tamper protection: disable, never edit).", authenticated=True),
                ApiRoute("POST", "/rlvr/verifiers/enable", "Let a disabled verifier support rewards again.", authenticated=True),
                ApiRoute("POST", "/rlvr/verify", "Verify checkable questions; expectations are frozen before each check.", authenticated=True),
                ApiRoute("POST", "/rlvr/reward", "Turn verification results into a reward, with its integrity audit.", authenticated=True),
                ApiRoute("POST", "/rlvr/critiques", "Generate structured critiques from recorded evidence and store them.", authenticated=True),
                ApiRoute("GET", "/rlvr/critiques", "Stored critiques, newest first, filtered by category and severity.", authenticated=True),
                ApiRoute("GET", "/rlvr/corrections", "Corrected examples and their verdicts; held rows are reviewable.", authenticated=True),
                ApiRoute("POST", "/rlvr/corrections", "Propose corrections for stored critiques; unverified ones are held.", authenticated=True),
                ApiRoute("GET", "/rlvr/datasets", "Critique dataset versions, newest first, filtered by name.", authenticated=True),
                ApiRoute("POST", "/rlvr/datasets", "Build an immutable critique dataset version from critiques and corrections.", authenticated=True),
                ApiRoute("GET", "/rlvr/datasets/{dataset_version_id}/validate", "Whether a critique dataset can train anything, and what is missing.", authenticated=True),
                ApiRoute("GET", "/rlvr/datasets/{dataset_version_id}/held", "Corrections a dataset held back, so a person can settle them.", authenticated=True),
                ApiRoute("GET", "/rlvr/datasets/{dataset_version_id}/pairs", "The Phase 17 preference pairs a critique dataset yields.", authenticated=True),
                ApiRoute("GET", "/rlvr/datasets/{dataset_version_id}", "One critique dataset version with its splits and statistics.", authenticated=True),
                ApiRoute("POST", "/rlvr/estimate", "Estimate an RLVR config's resources without starting anything.", authenticated=True),
                ApiRoute("POST", "/rlvr/pipeline", "The ten-stage RLVR plan for a configuration and task set.", authenticated=True),
                ApiRoute("POST", "/rlvr/dry-run", "Walk all ten RLVR stages on deterministic inputs; starts nothing.", authenticated=True),
                ApiRoute("POST", "/rlvr/evaluate", "Verifier-side metrics against labels recorded before the call.", authenticated=True),
                ApiRoute("GET", "/rlvr/runs", "RLVR runs, newest first, filtered by status.", authenticated=True),
                ApiRoute("POST", "/rlvr/runs", "Create an RLVR run from a critique dataset (never starts it).", authenticated=True),
                ApiRoute("POST", "/rlvr/runs/start", "Start an RLVR run; unsafe estimates and unconfirmed real runs are refused.", authenticated=True),
                ApiRoute("POST", "/rlvr/runs/pause", "Pause a running RLVR run at the next step boundary.", authenticated=True),
                ApiRoute("POST", "/rlvr/runs/cancel", "Cancel an RLVR run; its checkpoints are kept.", authenticated=True),
                ApiRoute("POST", "/rlvr/runs/resume", "Resume an interrupted RLVR run from its newest valid checkpoint.", authenticated=True),
                ApiRoute("POST", "/rlvr/runs/re-estimate", "Re-estimate a run's resources against the current machine reading.", authenticated=True),
                ApiRoute("POST", "/rlvr/runs/evaluate", "Compare base, SFT and preference models against the RLVR candidate.", authenticated=True),
                ApiRoute("GET", "/rlvr/runs/{run_id}/checkpoints", "An RLVR run's checkpoints with their validity and loadability.", authenticated=True),
                ApiRoute("GET", "/rlvr/runs/{run_id}", "One RLVR run: configuration, metrics and verifier snapshot.", authenticated=True),
                ApiRoute("GET", "/continual/status", "Continual learning state: evidence counts, active strategy versions, policy, boundaries and restore report.", authenticated=True),
                ApiRoute("GET", "/continual/overview", "What Phase 27 ships, its honest limits and its live policy.", authenticated=True),
                ApiRoute("GET", "/continual/experiences", "Recorded executions, newest first, filtered by task kind and outcome.", authenticated=True),
                ApiRoute("GET", "/continual/experiences/{experience_id}", "One execution record with steps, verification and outcome reasons.", authenticated=True),
                ApiRoute("DELETE", "/continual/experiences/{experience_id}", "Forget one recorded execution and recompute derived signals.", authenticated=True),
                ApiRoute("POST", "/continual/feedback", "Record a person's positive or negative verdict on an execution.", authenticated=True),
                ApiRoute("GET", "/continual/signals", "Signals derived from evidence, with support and provenance.", authenticated=True),
                ApiRoute("GET", "/continual/strategies", "Strategy versions with status, provenance and measured performance.", authenticated=True),
                ApiRoute("GET", "/continual/proposals", "Strategy proposals, separated from validated and promoted work.", authenticated=True),
                ApiRoute("POST", "/continual/proposals", "Propose a template from distinct verified successes.", authenticated=True),
                ApiRoute("POST", "/continual/proposals/{proposal_id}/validate", "Validate a proposal against executed evidence.", authenticated=True),
                ApiRoute("POST", "/continual/proposals/{proposal_id}/promote", "Promote a validated proposal, retaining the replaced version.", authenticated=True),
                ApiRoute("POST", "/continual/strategies/{strategy_id}/rollback", "Restore the previous version of a strategy.", authenticated=True),
                ApiRoute("POST", "/continual/recommend", "Advisory recommendation for a goal, with evidence ids and confidence.", authenticated=True),
                ApiRoute("POST", "/continual/benchmark", "Run the isolated baseline, learn, compare benchmark.", authenticated=True),
                ApiRoute("GET", "/computer/status", "Live device/capability registry, supervisor state, focused loop and operation-history counts.", authenticated=True),
                ApiRoute("GET", "/computer/devices", "What this machine can actually operate, what is connected and what is unsupported.", authenticated=True),
                ApiRoute("POST", "/computer/observe", "Read-only screen/window/OCR/DOM observation with element coordinates and state signals.", authenticated=True),
                ApiRoute("POST", "/computer/plan", "Plan a computer objective, classify its risk and mint a single-use token.", authenticated=True),
                ApiRoute("POST", "/computer/run", "Execute a planned computer objective through the closed observe/verify/recover loop.", authenticated=True),
                ApiRoute("POST", "/computer/stop", "Emergency stop: cancel before the next action and release all held input.", authenticated=True),
                ApiRoute("GET", "/computer/history", "Redacted persisted execution history; no screenshots or secrets.", authenticated=True),
                ApiRoute("POST", "/computer/history/clear", "Delete the persisted computer-operation history.", authenticated=True),
                ApiRoute("POST", "/computer/reference", "Resolve a contextual reference against the current observation or history.", authenticated=True),
                ApiRoute("GET", "/collaboration/status", "Multi-agent coordination state: policy, roles, task counts, limits and restore report.", authenticated=True),
                ApiRoute("GET", "/collaboration/overview", "What Phase 28 ships, its architecture and its honest limits.", authenticated=True),
                ApiRoute("POST", "/collaboration/plan", "Decide whether an objective benefits from multiple agents and return its dependency graph.", authenticated=True),
                ApiRoute("POST", "/collaboration/run", "Execute a collaboration plan, with per-subtask verification and aggregation.", authenticated=True),
                ApiRoute("GET", "/collaboration/tasks", "Persisted collaboration tasks, newest first, filtered by status.", authenticated=True),
                ApiRoute("GET", "/collaboration/tasks/{task_id}", "One collaboration task with its subtasks, messages, conflicts and verdict.", authenticated=True),
                ApiRoute("POST", "/collaboration/tasks/{task_id}/cancel", "Stop scheduling a collaboration; safe in-flight work is cancelled.", authenticated=True),
                ApiRoute("POST", "/collaboration/tasks/{task_id}/resume", "Resume pending subtasks, preserving every completed result.", authenticated=True),
                # Phase 31: the skill library. Reading is evidence; mutation is
                # explicit. Acquisition needs more than one verified execution,
                # validation is a separate act, adaptation preserves the source
                # version, and execution passes the permission layer first.
                ApiRoute("GET", "/skills", "Skill library state: lifecycle counts, evidence, executions and adaptation failures.", authenticated=True),
                ApiRoute("GET", "/skills/catalog", "Every stored skill version with its contract, reliability, provenance and evidence.", authenticated=True),
                ApiRoute("GET", "/skills/search", "Retrieve and rank skills for a goal. Query parameters: `query` (required), `environment`, `include_incompatible`, `limit`.", authenticated=True),
                ApiRoute("GET", "/skills/evidence", "Execution evidence and failed adaptations, newest bounded.", authenticated=True),
                ApiRoute("GET", "/skills/{skill_id}", "One skill's contract, its versions and its recent executions (optional `version`).", authenticated=True),
                ApiRoute("POST", "/skills/acquire", "Acquire a reusable skill from repeated verified execution evidence (one success is refused).", authenticated=True),
                ApiRoute("POST", "/skills/validate", "Validate a skill from its own verified execution evidence (explicit, never automatic).", authenticated=True),
                ApiRoute("POST", "/skills/adapt", "Create an independently validatable adapted version for a new goal, inputs or environment.", authenticated=True),
                ApiRoute("POST", "/skills/compose", "Check dependencies, ordering, inputs and tools for a multi-skill workflow.", authenticated=True),
                ApiRoute("POST", "/skills/compose/execute", "Execute an ordered multi-skill workflow, binding declared outputs and stopping on the first unverified step; client approval flags are refused.", authenticated=True),
                ApiRoute("POST", "/skills/execute", "Execute a validated or adapted skill through the existing pipeline; client approval flags are refused and sensitive steps use the objective/task approval path.", authenticated=True),
                ApiRoute("POST", "/skills/resume", "Resume a parked skill execution from its verified checkpoint; client approval flags are refused.", authenticated=True),
                ApiRoute("POST", "/skills/promote", "Make an independently validated skill version the active one.", authenticated=True),
                ApiRoute("POST", "/skills/deprecate", "Deprecate a skill version without deleting its evidence.", authenticated=True),
                ApiRoute("POST", "/skills/rollback", "Restore an earlier validated version and deprecate the replaced one.", authenticated=True),
            ),
            websocket_paths=("/ws/events",),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "routes": [route.to_dict() for route in self.routes],
            "websocket_paths": self.websocket_paths,
        }
