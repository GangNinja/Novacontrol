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
            ),
            websocket_paths=("/ws/events",),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "routes": [route.to_dict() for route in self.routes],
            "websocket_paths": self.websocket_paths,
        }
