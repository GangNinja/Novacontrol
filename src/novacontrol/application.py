"""NovaControl application composition root."""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Coroutine
from typing import Any, TypeAlias, cast
from uuid import uuid4

from novacontrol.agents import AgentModule, AgentRegistry, AgentTask, CoordinatorAgent, build_default_agents
from novacontrol.application_helpers import (
    ApprovedApprovalGateway,
    build_auto_code_changes,
    build_preview_payload,
    parse_browser_command,
    parse_desktop_command,
    resolve_phone_target,
    verify_code_changes,
)
from novacontrol.automation import AutomationManager
from novacontrol.brain import BrainIntent, BrainRequest, NovaBrain
from novacontrol.browser import BrowserAutomationController, BrowserAutomationModule, PlaywrightBrowserRunner
from novacontrol.core.events import EventBus
from novacontrol.core.runtime import EventDrivenRuntime
from novacontrol.core.security import ApprovalDecision, ApprovalRequest, DenyByDefaultApprovalGateway
from novacontrol.desktop import DesktopAutomationController, DesktopAutomationModule, LocalDesktopRunner
from novacontrol.explore import ExploreModule, ExploreRequest, ExploreService
from novacontrol.integrations import build_llm_provider_from_environment
from novacontrol.knowledge import KnowledgeBase
from novacontrol.memory import MemoryManager, MemoryModule, MemoryNamespace, SqliteMemoryStore
from novacontrol.persistence import JsonStateStore
from novacontrol.phone import PhoneControlController, PhoneControlModule
from novacontrol.planning import PlanningEngine, PlanningModule, WorkflowExecutor
from novacontrol.plugins import PluginMarketplaceModule
from novacontrol.projects import ProjectManager
from novacontrol.scheduler import InMemoryScheduler
from novacontrol.self_improvement import CodeChange, SelfImprovementEngine
from novacontrol.settings import SettingsManager
from novacontrol.skills import SkillRegistry
from novacontrol.tasks import TaskCenter, TaskRecordStatus
from novacontrol.tools import ToolExecutor, ToolModule, ToolRegistry
from novacontrol.vision import VisionModule
from novacontrol.voice import VoiceModule

_APPROVAL_TTL_SECONDS = 300.0  # A planned desktop action must be approved within 5 minutes.


@dataclass(frozen=True, slots=True)
class ApplicationResponse:
    route: str
    intent: str
    summary: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        # One flat, self-describing envelope: the router already knows route/intent
        # and shaping computes summary, so consumers never re-derive the payload shape.
        return {"route": self.route, "intent": self.intent, "summary": self.summary, "payload": self.payload}


_Handler: TypeAlias = Callable[["NovaControlApplication", BrainRequest, str], Coroutine[Any, Any, tuple[str, dict[str, Any]]]]


def _workflow_from_plan(workflow_data: dict[str, Any], *, action_cls: Any, action_type_cls: Any, workflow_cls: Any) -> Any:
    """Rebuild a workflow from the serialized plan stored on the approval token.

    Shared by desktop and browser execution: both serialized plans have the same
    {name, id, actions} shape, only the model classes differ.
    """
    actions = tuple(
        action_cls(
            type=action_type_cls(a["type"]),
            target=a["target"],
            description=a["description"],
            parameters=a.get("parameters", {}),
            id=a["id"],
        )
        for a in workflow_data.get("actions", [])
    )
    return workflow_cls(name=workflow_data["name"], actions=actions, id=workflow_data["id"])


class NovaControlApplication:
    """Runnable local composition of NovaControl services."""

    _DEFAULT_DATA_DIR = Path("data")

    def __init__(self, *, data_dir: str | Path | None = None) -> None:
        self.event_bus = EventBus(continue_on_error=True)
        self.runtime = EventDrivenRuntime(self.event_bus)
        self.brain = NovaBrain(completion_provider=build_llm_provider_from_environment())
        self.data_dir = Path(data_dir) if data_dir is not None else self._DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_store = JsonStateStore(self.data_dir)
        memory_store = SqliteMemoryStore(self.data_dir / "memory.sqlite3")
        self.memory = MemoryManager(memory_store)
        self.self_improvement = SelfImprovementEngine(Path.cwd())
        self.explore = ExploreService(completion_provider=self.brain.completion_provider)
        self.planning = PlanningEngine()
        self.workflow_executor = WorkflowExecutor()
        self.agent_registry = AgentRegistry()
        for agent in build_default_agents():
            self.agent_registry.register(agent)
        self.coordinator = CoordinatorAgent()
        self.tools = ToolRegistry()
        self.tool_executor = ToolExecutor(self.tools)
        self.skills = SkillRegistry()
        self.scheduler = (
            InMemoryScheduler.from_dict(self.state_store.read("scheduler"))
            if self.state_store is not None
            else InMemoryScheduler()
        )
        self.projects = (
            ProjectManager.from_dict(self.state_store.read("projects"))
            if self.state_store is not None
            else ProjectManager()
        )
        self.knowledge = (
            KnowledgeBase.from_dict(self.state_store.read("knowledge"))
            if self.state_store is not None
            else KnowledgeBase()
        )
        self.automation = (
            AutomationManager.from_dict(self.state_store.read("automation"))
            if self.state_store is not None
            else AutomationManager()
        )
        self.tasks = (
            TaskCenter.from_dict(self.state_store.read("tasks"))
            if self.state_store is not None
            else TaskCenter()
        )
        self.settings = (
            SettingsManager.from_dict(self.state_store.read("settings"))
            if self.state_store is not None
            else SettingsManager()
        )
        self._improvement_previews: dict[str, dict[str, Any]] = {}
        # Server-side approval tokens: token -> {"command", "plan", "expires_at"}. Minted by
        # plan_desktop_command and consumed once by execute_desktop_command. A client can never
        # self-approve: execution requires a token the server itself issued for the exact command.
        self.approval_ttl_seconds = _APPROVAL_TTL_SECONDS
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        self.desktop = DesktopAutomationController(runner=LocalDesktopRunner())
        self.phone = PhoneControlController()
        self.browser = BrowserAutomationController(runner=PlaywrightBrowserRunner())

        self.runtime.register_module(MemoryModule(self.memory))
        self.runtime.register_module(PlanningModule(self.planning, self.workflow_executor))
        self.runtime.register_module(AgentModule(self.agent_registry, self.coordinator))
        self.runtime.register_module(ToolModule(self.tool_executor))
        self.runtime.register_module(ExploreModule(self.explore))
        self.runtime.register_module(PluginMarketplaceModule())
        self.runtime.register_module(DesktopAutomationModule(self.desktop))
        self.runtime.register_module(PhoneControlModule(self.phone))
        self.runtime.register_module(BrowserAutomationModule(self.browser))
        self.runtime.register_module(VisionModule())
        self.runtime.register_module(VoiceModule())

    async def start(self) -> None:
        await self.runtime.start()

    async def stop(self) -> None:
        self.persist()
        await self.browser.close()
        await self.runtime.stop()

    def persist(self) -> None:
        if self.state_store is None:
            return
        self.state_store.write("scheduler", self.scheduler.to_dict())
        self.state_store.write("projects", self.projects.to_dict())
        self.state_store.write("knowledge", self.knowledge.to_dict())
        self.state_store.write("automation", self.automation.to_dict())
        self.state_store.write("tasks", self.tasks.to_dict())
        self.state_store.write("settings", self.settings.to_dict())

    async def handle_request(self, text: str) -> ApplicationResponse:
        """Route a natural-language request through the available subsystems."""
        await self.memory.remember(
            MemoryNamespace.CONVERSATION,
            f"request-{len((await self.memory.retrieve(MemoryNamespace.CONVERSATION, '', limit=100)))}",
            {"text": text}, text=text, importance=0.2,
        )
        request = BrainRequest(text=text, context=self.status())
        task = self.tasks.create(text, kind="ask")
        self.tasks.update(task.id, TaskRecordStatus.RUNNING, progress=0.1)
        decision = self.brain.decide(request)

        handler = self._HANDLERS.get(decision.intent)
        if handler:
            route, payload = await handler(self, request, text)
        else:
            route, payload = await self._handle_agent(request, text)

        response = await self.brain.shape_response(request, decision, payload)
        self.tasks.update(task.id, TaskRecordStatus.COMPLETED, progress=1.0, result=response.to_dict())
        return ApplicationResponse(route, decision.intent.value, response.summary, response.payload)

    # ── Intent handlers ──────────────────────────────────

    async def _handle_clarify(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        return "brain", {"message": "Please provide a little more detail."}

    async def _handle_chat(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        response = await self.brain.chat(request)
        return "chat", response.payload

    async def _handle_explore(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        report = await self.explore.research(
            ExploreRequest(text, include_videos=self.settings.settings.include_videos_in_explore, max_sources=4, max_videos=3)
        )
        return "explore", report.to_dict()

    async def _handle_plan(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        plan = self.planning.create_plan(text)
        payload: dict[str, Any] = {"plan": plan.to_dict()}
        if not plan.needs_clarification:
            payload["workflow"] = (await self.workflow_executor.execute(plan)).to_dict()
        return "planning", payload

    async def _handle_self_improvement(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        return "self_improvement", self.self_improvement.plan(text).to_dict()

    async def _handle_desktop(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        return "desktop_automation", self.plan_desktop_command(text)

    async def _handle_phone(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        return "phone_control", self.plan_phone_command(text)

    async def _handle_browser(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        return "browser_automation", self.plan_browser_command(text)

    async def _handle_memory(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        results = await self.memory.retrieve(MemoryNamespace.CONVERSATION, text, limit=5)
        return "memory", {"results": [r.record.to_dict() | {"score": r.score} for r in results]}

    async def _handle_project(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        project = self.projects.create_project(text[:60], description=text)
        return "project", project.to_dict()

    async def _handle_agent(self, _request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        response = await self.coordinator.delegate(AgentTask(text), self.agent_registry)
        return "agent", response.to_dict()

    _HANDLERS: dict[BrainIntent, _Handler] = {
        BrainIntent.CLARIFY: _handle_clarify,
        BrainIntent.CHAT: _handle_chat,
        BrainIntent.EXPLORE: _handle_explore,
        BrainIntent.PLAN: _handle_plan,
        BrainIntent.SELF_IMPROVEMENT: _handle_self_improvement,
        BrainIntent.DESKTOP_AUTOMATION: _handle_desktop,
        BrainIntent.PHONE_CONTROL: _handle_phone,
        BrainIntent.BROWSER_AUTOMATION: _handle_browser,
        BrainIntent.MEMORY: _handle_memory,
        BrainIntent.PROJECT: _handle_project,
    }

    # --- Learning ---

    async def learning_cycle(self, goal: str, *, feedback: str = "") -> dict[str, Any]:
        plan = self.self_improvement.plan(goal)
        key = f"learning-{len((await self.memory.retrieve(MemoryNamespace.LONG_TERM, 'learning', limit=100)))}"
        record = await self.memory.remember(
            MemoryNamespace.LONG_TERM, key,
            {"goal": goal, "feedback": feedback, "actions": [a.to_dict() for a in plan.actions], "findings": [f.to_dict() for f in plan.findings]},
            text=f"Learning cycle for {goal}. Feedback: {feedback}", importance=0.8,
        )
        return {"mode": "local_feedback_learning", "message": "Recorded a learning cycle and produced a safe self-improvement plan.", "memory": record.to_dict(), "plan": plan.to_dict()}

    async def autonomous_learning_loop(self, goal: str, *, iterations: int = 3, feedback: str = "") -> dict[str, Any]:
        bounded_iterations = max(1, min(iterations, 5))
        results: list[dict[str, Any]] = []
        next_feedback = feedback
        for index in range(bounded_iterations):
            cycle = await self.learning_cycle(f"{goal} (iteration {index + 1})", feedback=next_feedback)
            findings = cycle["plan"]["findings"]
            results.append({"iteration": index + 1, "memory_key": cycle["memory"]["key"], "action_count": len(cycle["plan"]["actions"]), "finding_count": len(findings), "top_findings": findings[:3]})
            next_feedback = "Focus next cycle on: " + "; ".join(finding["message"] for finding in findings[:3])
        return {"mode": "autonomous_local_learning", "training_scope": "feedback memory, planning heuristics, and verified code-improvement plans", "weight_training": False, "iterations": results, "next_feedback": next_feedback}

    # --- Improvement Workflow ---

    def improvement_workflow(self, goal: str) -> dict[str, Any]:
        plan = self.self_improvement.plan(goal)
        findings = [finding.to_dict() for finding in plan.findings]
        actions = [action.to_dict() for action in plan.actions]
        return {
            "goal": goal, "status": "waiting_for_approval",
            "summary": "I inspected the project and prepared a safe improvement workflow.",
            "approval": {"required": True, "approved": False, "temporary_first": True, "message": "Run a temporary preview first, then approve before code changes are promoted."},
            "completed": [
                {"title": "Understood the request", "detail": "Converted your goal into a self-improvement task."},
                {"title": "Inspected the codebase", "detail": f"{plan.profile.source_files} source files, {plan.profile.test_files} test files, {plan.profile.python_lines} Python lines."},
                {"title": "Prepared safe next actions", "detail": f"{len(actions)} actions prepared with test/health verification."},
            ],
            "current": {"title": "Waiting for approval before editing code", "detail": "NovaControl can plan and verify improvements now; code writes remain approval-gated."},
            "remaining": [{"title": action["title"], "detail": action["description"], "verification": action.get("verification", [])} for action in actions],
            "findings": findings[:5],
            "raw_plan": plan.to_dict(),
        }

    def preview_improvement_workflow(self, goal: str) -> dict[str, Any]:
        workflow = self.improvement_workflow(goal)
        changes = build_auto_code_changes(goal)
        verification = verify_code_changes(changes)
        preview_id = uuid4().hex
        self._improvement_previews[preview_id] = {"goal": goal, "changes": changes, "verification": verification}
        workflow["status"] = "temporary_preview_ready"
        workflow["summary"] = "Temporary code preview is ready. Nothing has been written into the project yet."
        workflow["approval"] = {"required": True, "approved": False, "temporary_first": True, "message": "Review this temporary result. Approve only if it works for you."}
        workflow["preview"] = build_preview_payload(preview_id, changes, verification)
        workflow["completed"].append({"title": "Generated temporary code proposal", "detail": f"{len(changes)} file change(s) prepared and syntax checked without touching source files."})
        workflow["current"] = {"title": "Review the temporary preview", "detail": "Approve And Apply will promote this exact preview into the codebase."}
        return workflow

    async def approve_improvement_workflow(self, goal: str, *, preview_id: str | None = None) -> dict[str, Any]:
        if not preview_id:
            preview_id = self.preview_improvement_workflow(goal)["preview"]["id"]
        preview = self._improvement_previews.get(preview_id)
        if preview is None:
            raise ValueError(f"Improvement preview not found: {preview_id}")
        changes = tuple(preview["changes"])
        verification = dict(preview["verification"])
        workflow = self.improvement_workflow(goal)
        if verification.get("ok"):
            results = await self.self_improvement.apply_changes(changes, approval_gateway=ApprovedApprovalGateway())
        else:
            results = ()
        all_applied = bool(results) and all(result.status.value == "applied" for result in results)
        workflow["status"] = "approved_and_applied" if all_applied else "approval_blocked"
        workflow["summary"] = "Approved preview was applied to the codebase." if all_applied else "Approval was received, but the preview did not pass verification."
        workflow["approval"] = {"required": True, "approved": True, "temporary_first": True, "message": "You approved this workflow after preview."}
        workflow["preview"] = build_preview_payload(preview_id, changes, verification)
        workflow["apply_results"] = [result.to_dict() for result in results]
        workflow["completed"].extend([
            {"title": "Approval recorded", "detail": "User approved promoting the temporary preview into code changes."},
            {"title": "Applied approved code changes" if all_applied else "Skipped code apply", "detail": f"{len(results)} change(s) written to the project." if all_applied else "Fix preview verification errors before applying changes."},
        ])
        workflow["current"] = {"title": "Run verification", "detail": "Run the test suite and health check after approved code changes are written."}
        return workflow

    # --- Desktop / Phone Command Helpers ---

    def _mint_approval(self, command: str, plan: dict[str, Any]) -> str:
        """Mint a server-side approval token bound to an exact planned command."""
        now = time.time()
        for stale_token in [t for t, r in self._pending_approvals.items() if r["expires_at"] <= now]:
            self._pending_approvals.pop(stale_token, None)
        token = uuid4().hex
        self._pending_approvals[token] = {
            "command": command,
            "plan": copy.deepcopy(plan),
            "expires_at": now + self.approval_ttl_seconds,
        }
        return token

    def _consume_approval(self, token: str, command: str) -> dict[str, Any]:
        """Validate and consume a single-use approval token for a specific command."""
        record = self._pending_approvals.get(token)
        if record is None:
            raise ValueError(
                "Approval token is unknown. Plan the command again and approve the freshly previewed action."
            )
        if time.time() > record["expires_at"]:
            self._pending_approvals.pop(token, None)
            raise ValueError("Approval token has expired. Plan the command again and approve it within the time window.")
        if record["command"] != command:
            raise ValueError("Approval token does not match this command. Re-plan and approve the exact command.")
        # Single-use: remove before returning, so the caller may mutate the plan (status, results)
        # without affecting any record. No copy needed — nothing else references the stored plan now.
        self._pending_approvals.pop(token, None)
        return cast(dict[str, Any], record["plan"])

    def plan_desktop_command(self, command: str) -> dict[str, Any]:
        """Parse a JARVIS command into a workflow, mint an approval token, and return the plan."""
        parsed_actions = parse_desktop_command(command)
        actions_descriptions: list[str] = []
        desktop_actions: list[Any] = []
        for step in parsed_actions:
            action_type = step["action"]
            if action_type == "open":
                wf = self.desktop.plan_open_application(step["target"])
                desktop_actions.extend(wf.actions)
                actions_descriptions.append(f"Open {step['target']}")
            elif action_type == "open_folder":
                wf = self.desktop.plan_open_folder(step["target"])
                desktop_actions.extend(wf.actions)
                actions_descriptions.append(f"Open folder {step['target']}")
            elif action_type == "type":
                wf = self.desktop.plan_type_text(step["text"])
                desktop_actions.extend(wf.actions)
                actions_descriptions.append(f"Type '{step['text']}'")
            elif action_type == "search":
                wf = self.desktop.plan_search_start_menu(step["text"])
                desktop_actions.extend(wf.actions)
                actions_descriptions.append(f"Search for '{step['text']}'")
            elif action_type == "execute":
                wf = self.desktop.plan_execute_script(step["target"])
                desktop_actions.extend(wf.actions)
                actions_descriptions.append(f"Execute: {step['target']}")
        from novacontrol.desktop.models import DesktopWorkflow
        combined = DesktopWorkflow(
            name=command[:60],
            actions=tuple(desktop_actions),
        )
        summary_steps = " → ".join(actions_descriptions)
        first_target = parsed_actions[0].get("target", parsed_actions[0].get("text", command)) if parsed_actions else command
        plan: dict[str, Any] = {
            "route": "desktop_automation",
            "status": "waiting_for_approval",
            "command": command,
            "target": first_target,
            "steps": actions_descriptions,
            "summary": f"Planned: {summary_steps}",
            "approval": {"required": True, "approved": False, "message": "Review the actions before running them on your computer."},
            "workflow": combined.to_dict(),
        }
        plan["approval"]["token"] = self._mint_approval(command, plan)
        return plan

    async def execute_desktop_command(self, command: str, *, approval_token: str | None = None) -> dict[str, Any]:
        """Execute a previously planned desktop command, guarded by a server-issued approval token.

        Execution requires a valid, unconsumed token minted by plan_desktop_command for the exact
        command; a missing, unknown, expired, or replayed token raises ValueError (HTTP 403) and
        nothing runs. Use plan_desktop_command to preview before approving.
        """
        if not approval_token:
            raise ValueError(
                "Executing a desktop command requires an approval token. "
                "Call /command/plan first, review the preview, then execute with its token."
            )
        from novacontrol.desktop.models import DesktopAction, DesktopActionType, DesktopWorkflow
        return await self._execute_planned_command(
            command, approval_token,
            controller=self.desktop,
            action_cls=DesktopAction, action_type_cls=DesktopActionType, workflow_cls=DesktopWorkflow,
            result_label="action",
        )

    def plan_browser_command(self, command: str) -> dict[str, Any]:
        """Parse a browser command into a workflow, mint an approval token, and return the plan."""
        from novacontrol.browser.models import BrowserWorkflow

        parsed_actions = parse_browser_command(command)
        browser_actions: list[Any] = []
        action_descriptions: list[str] = []
        for step in parsed_actions:
            if step["action"] == "navigate":
                wf = self.browser.plan_navigation(step["target"])
                browser_actions.extend(wf.actions)
                action_descriptions.append(f"Navigate to {step['target']}")
            elif step["action"] == "fill_form":
                fields = dict(step.get("fields") or {})
                wf = self.browser.plan_form_fill(step["target"], fields)
                browser_actions.extend(wf.actions)
                field_note = f" ({len(fields)} field(s))" if fields else ""
                action_descriptions.append(f"Fill form at {step['target']}{field_note}")
        if not browser_actions:
            return {
                "route": "browser_automation",
                "status": "not_an_action",
                "command": command,
                "target": "browser",
                "summary": "I couldn't plan a browser action from that. Try 'navigate to example.com' or 'fill the form at https://example.com/login with username=admin'.",
                "approval": {"required": False, "approved": False, "message": "No browser navigation or form-fill action was detected."},
                "workflow": {"id": "", "name": "No action", "actions": []},
            }
        combined = BrowserWorkflow(name=command[:60], actions=tuple(browser_actions))
        plan: dict[str, Any] = {
            "route": "browser_automation",
            "status": "waiting_for_approval",
            "command": command,
            "target": parsed_actions[0]["target"],
            "steps": action_descriptions,
            "summary": "Planned: " + " → ".join(action_descriptions),
            "approval": {"required": True, "approved": False, "message": "Review the browser actions before running them."},
            "workflow": combined.to_dict(),
        }
        plan["approval"]["token"] = self._mint_approval(command, plan)
        return plan

    async def execute_browser_command(self, command: str, *, approval_token: str | None = None) -> dict[str, Any]:
        """Execute a previously planned browser command, guarded by a server-issued approval token.

        Browser actions (navigate, fill forms) require a valid, unconsumed token minted by
        plan_browser_command for the exact command; a missing, unknown, expired, or replayed token
        raises ValueError (HTTP 403) and nothing runs. Use plan_browser_command to preview first.
        """
        if not approval_token:
            raise ValueError(
                "Executing a browser action requires an approval token. "
                "Call /command/plan first, review the preview, then execute with its token."
            )
        from novacontrol.browser.models import BrowserAction, BrowserActionType, BrowserWorkflow
        return await self._execute_planned_command(
            command, approval_token,
            controller=self.browser,
            action_cls=BrowserAction, action_type_cls=BrowserActionType, workflow_cls=BrowserWorkflow,
            result_label="browser action",
        )

    async def _execute_planned_command(
        self,
        command: str,
        approval_token: str,
        *,
        controller: Any,
        action_cls: Any,
        action_type_cls: Any,
        workflow_cls: Any,
        result_label: str,
    ) -> dict[str, Any]:
        """Consume a valid token, rebuild the stored plan, and run it through a controller.

        Shared by desktop and browser execution: both validate the same token, rebuild the
        serialized workflow via _workflow_from_plan, execute under auto-approval (a server-minted
        token was already validated), then mark the plan executed.
        """
        plan = self._consume_approval(approval_token, command)
        workflow = _workflow_from_plan(
            plan["workflow"],
            action_cls=action_cls, action_type_cls=action_type_cls, workflow_cls=workflow_cls,
        )
        # Execute with auto-approval (a server-minted token was validated above)
        controller.approval_gateway = ApprovedApprovalGateway()
        try:
            results = await controller.execute_workflow(workflow)
        finally:
            controller.approval_gateway = DenyByDefaultApprovalGateway()
        plan["status"] = "executed"
        plan["approval"]["approved"] = True
        plan["execution_results"] = [r.to_dict() for r in results]
        succeeded = sum(1 for r in results if r.status.value == "completed")
        plan["summary"] = f"Executed {succeeded}/{len(results)} {result_label}(s) successfully."
        return plan

    def plan_phone_command(self, command: str) -> dict[str, Any]:
        target = resolve_phone_target(command)
        status = self.phone.status()
        workflow = self.phone.plan_open_application(target)
        return {"route": "phone_control", "status": "waiting_for_approval" if status.available else "waiting_for_phone_bridge", "command": command, "target": target, "summary": f"Prepared a phone action for {target}.", "approval": {"required": True, "approved": False, "message": "Pair and review the phone action before running it."}, "bridge": status.to_dict(), "workflow": workflow.to_dict()}

    async def execute_phone_command(self, command: str) -> dict[str, Any]:
        return self.plan_phone_command(command)

    def _dispatch_device_command(self, command: str) -> dict[str, Any]:
        """Route a device command to the appropriate plan function."""
        decision = self.brain.decide(BrainRequest(text=command, context=self.status()))
        if decision.intent is BrainIntent.PHONE_CONTROL:
            return self.plan_phone_command(command)
        if decision.intent is BrainIntent.DESKTOP_AUTOMATION:
            return self.plan_desktop_command(command)
        if decision.intent is BrainIntent.BROWSER_AUTOMATION:
            return self.plan_browser_command(command)
        return {
            "route": decision.intent.value, "status": "not_an_action",
            "command": command, "target": "NovaControl",
            "summary": "This command is not a direct device action. Use Chat or Explore for this request.",
            "approval": {"required": False, "approved": False, "message": "No desktop, phone, or browser action was detected."},
            "workflow": {"id": "", "name": "No action", "actions": []},
        }

    def plan_command(self, command: str) -> dict[str, Any]:
        return self._dispatch_device_command(command)

    async def execute_command(self, command: str, *, approval_token: str | None = None) -> dict[str, Any]:
        """Execute a previously planned command, guarded by a server-issued approval token."""
        decision = self.brain.decide(BrainRequest(text=command, context=self.status()))
        if decision.intent is BrainIntent.DESKTOP_AUTOMATION:
            return await self.execute_desktop_command(command, approval_token=approval_token)
        if decision.intent is BrainIntent.BROWSER_AUTOMATION:
            return await self.execute_browser_command(command, approval_token=approval_token)
        if decision.intent is BrainIntent.PHONE_CONTROL:
            return self.plan_phone_command(command)
        return self._dispatch_device_command(command)

    # --- Status ---

    def status(self) -> dict[str, Any]:
        return {
            "runtime_started": self.runtime.state.started,
            "modules": self.runtime.module_names(),
            "skills": [skill.schema.name for skill in self.skills.list()],
            "scheduled_tasks": len(self.scheduler.tasks()),
            "tracked_tasks": len(self.tasks.list()),
            "projects": len(self.projects.list_projects()),
            "automation_workflows": len(self.automation.list_workflows()),
            "desktop_available": True,
            "browser_available": True,
            "desktop_runner": type(self.desktop.runner).__name__,
            "browser_runner": type(self.browser.runner).__name__,
            "browser_adapter_available": PlaywrightBrowserRunner.is_available(),
            "self_improvement_available": True,
            "brain": {"provider": self.brain.provider_name, "model_configured": self.brain.model_configured},
            "phone_bridge": self.phone.status().to_dict(),
            "settings": self.settings.to_dict(),
        }
