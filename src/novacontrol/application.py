"""NovaControl application composition root."""

from __future__ import annotations

import asyncio
import copy
import logging
import re
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
    parse_phone_command,
    resolve_phone_target,
    verify_code_changes,
)
from novacontrol.agentcore import (
    ActionEngine,
    AdaptivePlanner,
    AgenticOrchestrator,
    ApplicationKnowledgeGraph,
    EvaluationLedger,
    RecoveryEngine,
    TaskInterpreter,
    UiPerceptionEngine,
    Verifier,
)
from novacontrol.automation import AutomationManager
from novacontrol.brain import BrainDecision, BrainIntent, BrainRequest, NovaBrain
from novacontrol.brain.brain import looks_like_research_question
from novacontrol.brain.scratch import scratchable_intent
from novacontrol.browser import BrowserAutomationController, BrowserAutomationModule, PlaywrightBrowserRunner
from novacontrol.core.activity import RecentActivityLog
from novacontrol.core.buglog import BugLog
from novacontrol.core.chat_transcript import ChatTranscriptStore
from novacontrol.core.events import Event, EventBus
from novacontrol.core.runtime import EventDrivenRuntime
from novacontrol.core.security import ApprovalDecision, ApprovalRequest, DenyByDefaultApprovalGateway
from novacontrol.desktop import DesktopAutomationController, DesktopAutomationModule, LocalDesktopRunner
from novacontrol.desktop.vision import VisionController
from novacontrol.explore import ExploreModule, ExploreRequest, ExploreService, set_explore_cache_provider
from novacontrol.intelligence import GlobalInputIntelligence, UnderstandResult
from novacontrol.intelligence.intent import IntentName, RiskLevel
from novacontrol.integrations import (
    CLOUD_LLM_PRESETS,
    build_cloud_provider,
    build_llm_provider_from_environment,
    build_ollama_provider,
    build_vision_provider,
    cloud_llm_presets,
    get_cloud_preset,
    make_ollama_reprobe,
    ollama_models,
    validate_cloud_key,
)
from novacontrol.integrations.llm import _redact_key
from novacontrol.knowledge import KnowledgeBase
from novacontrol.memory import MemoryManager, MemoryModule, MemoryNamespace, SqliteMemoryStore
from novacontrol.persistence import JsonStateStore
from novacontrol.phone import PhoneControlController, PhoneControlModule
from novacontrol.planning import PlanningEngine, PlanningModule, WorkflowExecutor
from novacontrol.planning.engine import steps_id
from novacontrol.plugins import PluginMarketplaceModule
from novacontrol.projects import ProjectManager
from novacontrol.scheduler import InMemoryScheduler
from novacontrol.self_improvement import CodeChange, SelfImprovementEngine
from novacontrol.settings import BRAIN_MODES, SettingsManager
from novacontrol.skills import SkillRegistry
from novacontrol.tasks import TaskCenter, TaskRecordStatus
from novacontrol.telemetry.hardware import HardwareTelemetry
from novacontrol.tools import ToolExecutor, ToolModule, ToolRegistry
from novacontrol.vision import VisionModule
from novacontrol.voice import VoiceModule

_APPROVAL_TTL_SECONDS = 300.0  # A planned desktop action must be approved within 5 minutes.

# Which live metrics each status intent needs. Deliberately per-intent: asking
# "how much RAM do I have" must not pay for a GPU or network probe.
_STATUS_METRICS: dict[str, tuple[str, ...]] = {
    "memory_status": ("memory",),
    "cpu_status": ("cpu",),
    "gpu_status": ("gpu",),
    "battery_status": ("battery",),
    "network_status": ("network",),
    "system_status": ("cpu", "memory", "storage", "battery", "uptime"),
}


def _format_gigabytes(value: object) -> str:
    """Human GB, or 'unknown' — never a confident 0 for an unmeasured value."""
    if not isinstance(value, (int, float)):
        return "unknown"
    return f"{float(value) / 1024 ** 3:.1f} GB"


def _unavailable(subject: str, metric: dict[str, Any]) -> str:
    reason = str(metric.get("reason") or "the operating system did not report it")
    return f"I couldn't measure {subject}: {reason}."


def _system_status_message(kind: str, metrics: dict[str, Any]) -> str:
    """Turn measured metrics into ONE sentence, deterministically.

    This is response generation without a model, which is the point: the figure
    came from the machine, so the sentence about it should not be paraphrased by
    something that never measured it.
    """
    if kind == "memory_status":
        metric = metrics.get("memory") or {}
        if metric.get("available"):
            return (
                f"You're currently using {_format_gigabytes(metric.get('used_bytes'))} of "
                f"{_format_gigabytes(metric.get('total_bytes'))} RAM ({metric.get('percent')}%)."
            )
        return _unavailable("memory usage", metric)
    if kind == "cpu_status":
        metric = metrics.get("cpu") or {}
        if metric.get("available"):
            return f"CPU usage is {metric.get('percent')}% right now."
        return _unavailable("CPU usage", metric)
    if kind == "gpu_status":
        metric = metrics.get("gpu") or {}
        name = str(metric.get("name") or "the GPU")
        if metric.get("available"):
            gpu_memory = metric.get("memory") or {}
            detail = ""
            if gpu_memory.get("available") and gpu_memory.get("used_bytes"):
                detail = f", using {_format_gigabytes(gpu_memory.get('used_bytes'))} of GPU memory"
            return f"{name} is at {metric.get('percent')}% utilization{detail}."
        if metric.get("name"):
            return f"This machine has {metric['name']}, but its utilization could not be measured."
        return _unavailable("GPU usage", metric)
    if kind == "battery_status":
        metric = metrics.get("battery") or {}
        if metric.get("available"):
            if metric.get("charging"):
                state = "charging"
            elif metric.get("on_ac"):
                state = "plugged in"
            else:
                state = "running on battery"
            return f"The battery is at {metric.get('percent')}% and {state}."
        return _unavailable("the battery", metric)
    if kind == "network_status":
        metric = metrics.get("network") or {}
        if metric.get("available"):
            if metric.get("rate_available"):
                download = float(metric.get("download_bps") or 0) / 1024
                upload = float(metric.get("upload_bps") or 0) / 1024
                return f"The network is up — {download:.0f} KB/s down and {upload:.0f} KB/s up."
            return "The network is up; transfer rates are still being measured."
        return _unavailable("the network", metric)
    parts: list[str] = []
    memory = metrics.get("memory") or {}
    if memory.get("available"):
        parts.append(f"RAM {memory.get('percent')}%")
    cpu = metrics.get("cpu") or {}
    if cpu.get("available"):
        parts.append(f"CPU {cpu.get('percent')}%")
    storage = metrics.get("storage") or {}
    if storage.get("available"):
        parts.append(f"disk {storage.get('percent')}% used")
    battery = metrics.get("battery") or {}
    if battery.get("available"):
        parts.append(f"battery {battery.get('percent')}%")
    uptime = metrics.get("uptime") or {}
    if uptime.get("available"):
        parts.append(f"up {float(uptime.get('seconds') or 0) / 3600:.1f} h")
    if not parts:
        return "I couldn't measure any system metric on this machine."
    return "System status: " + ", ".join(parts) + "."


def _nlu_payload(
    understanding: Any,
    understood: UnderstandResult,
    decision: BrainDecision,
    brain: NovaBrain,
) -> dict[str, Any]:
    """Safe operational metadata about how the request was understood.

    This is what a status surface renders ("Understanding: Fast NLU / Intent:
    open_application / Confidence: 97% / Model: None"). It carries NO model
    reasoning — only the chosen component, the resolved intent, the measured
    confidence and latency, and what the request needs next.
    """
    route = str(understanding.decision.get("route", ""))
    escalated = bool(understanding.requires_llm)
    return {
        "understanding": str(understanding.decision.get("understanding", understood.strategy)),
        "intent": understanding.intent.value,
        "goal": understanding.goal,
        "confidence": round(understanding.confidence, 3),
        "route": route,
        "reason": str(understanding.decision.get("reason", "")),
        "model": (brain.model_name or brain.provider_name) if escalated else "",
        "latency_ms": round(understanding.latency_ms, 3),
        "requires_llm": understanding.requires_llm,
        "requires_vision": understanding.requires_vision,
        "requires_web": understanding.requires_web,
        "requires_tools": understanding.requires_tools,
        "requires_confirmation": understanding.requires_confirmation,
        "handler": decision.intent.value,
        "handler_reason": decision.reason,
    }


@dataclass(frozen=True, slots=True)
class ApplicationResponse:
    route: str
    intent: str
    summary: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        # One flat, self-describing envelope: route/intent tell the client which
        # page to render, summary is the answer text, and `data` is the handler
        # payload directly. The handler payload is never nested again under a
        # `payload` key — renderers unwrap the single `data` field or read the
        # top-level summary, never probe for arbitrary nesting.
        return {"route": self.route, "intent": self.intent, "summary": self.summary, "data": self.payload}


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


def _workflow_classes_for_family(family: str) -> tuple[Any, Any, Any] | None:
    """Model classes for a serialized plan's device family (None = unknown)."""
    if family == "desktop":
        from novacontrol.desktop.models import DesktopAction, DesktopActionType, DesktopWorkflow
        return DesktopAction, DesktopActionType, DesktopWorkflow
    if family == "browser":
        from novacontrol.browser.models import BrowserAction, BrowserActionType, BrowserWorkflow
        return BrowserAction, BrowserActionType, BrowserWorkflow
    if family == "phone":
        from novacontrol.phone.models import PhoneAction, PhoneActionType, PhoneWorkflow
        return PhoneAction, PhoneActionType, PhoneWorkflow
    return None


def _summarize_search_results(execution_results: list[dict[str, Any]]) -> str:
    """Compose the web-search answer from an executed browser workflow.

    The search plan's extract step (mode=search_results) returns top result
    titles + links; this renders them as the summary so a search execution
    RETURNS AN ANSWER, not just a page load. Empty when the workflow carried
    no search-results payload (plain navigations, failed extracts, noop runs).
    """
    results: list[dict[str, Any]] = []
    for entry in execution_results:
        output = entry.get("output") or {}
        found = output.get("search_results")
        if isinstance(found, list):
            results.extend(found)
    results = [r for r in results if isinstance(r, dict) and (r.get("title") or r.get("url"))]
    if not results:
        return ""
    lines = [f"Top {len(results)} web results:"]
    for index, item in enumerate(results, start=1):
        title = str(item.get("title") or item.get("url") or "Untitled").strip()
        url = str(item.get("url") or "").strip()
        lines.append(f"{index}. {title}" + (f" — {url}" if url else ""))
    return "\n".join(lines)


class NovaControlApplication:
    """Runnable local composition of NovaControl services."""

    _DEFAULT_DATA_DIR = Path("data")

    def __init__(self, *, data_dir: str | Path | None = None) -> None:
        self.event_bus = EventBus(continue_on_error=True)
        self.runtime = EventDrivenRuntime(self.event_bus)
        # Server-side recent-activity journal: every completed command, research
        # run, and learning cycle lands here, so the web timeline can be seeded
        # once and then fed live from /events/stream — no localStorage, no polling.
        self.activity = RecentActivityLog(limit=50)
        self.data_dir = Path(data_dir) if data_dir is not None else self._DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_store = JsonStateStore(self.data_dir)
        # Persisted cloud LLM config (provider/model + the API key) and the
        # picked LOCAL brain model. The store lives in the app's data dir
        # (gitignored runtime state, same as tasks.json) — the key NEVER leaves
        # this machine except to the provider itself, and no API endpoint ever
        # returns it.
        # _cloud_llm_store: JsonStateStore namespace name; read()/write() go
        # through self.state_store.read("cloud_llm") / .write("cloud_llm", …).
        self._cloud_llm_store = self.state_store
        stored_cloud = self._cloud_llm_store.read("cloud_llm") if self._cloud_llm_store is not None else {}
        # Lazy Ollama upgrade: if boot found no LLM, each chat request probes for a
        # freshly started Ollama (rate-limited) and hot-swaps it in — for the brain
        # AND Explore synthesis — without a server restart. The callback resolves
        # self.explore lazily: it fires on a chat request, long after __init__.
        # A picked local model ("local_model") pins the upgrade to that model so
        # the picker's choice survives a lazy Ollama startup.
        self._ollama_reprobe = make_ollama_reprobe(model=str(stored_cloud.get("local_model", "")))
        # Read the persisted brain mode FIRST so the boot provider honors it: a
        # user who switched to scratch must not get one LLM answer before the UI
        # loads. The on_provider_upgrade callback resolves self.explore lazily —
        # it fires on a chat request, long after __init__.
        # Configured vision model (None = OCR/landmarks only). Set BEFORE the
        # desktop runner, VisionController, and agentcore perception are built
        # so boot wiring picks it up; set_vision_llm/clear_vision_llm hot-swap
        # it later through _apply_vision_provider.
        self.settings = (
            SettingsManager.from_dict(self.state_store.read("settings"))
            if self.state_store is not None
            else SettingsManager()
        )
        boot_mode = self.settings.settings.brain_mode
        boot_cloud = build_cloud_provider(
            str(stored_cloud.get("provider", "")),
            str(stored_cloud.get("api_key", "")),
            model=str(stored_cloud.get("model", "")),
        ) if stored_cloud.get("provider") and stored_cloud.get("api_key") else None
        # A picked LOCAL brain model ("local_model") re-arms the boot provider
        # pinned to that Ollama model; empty means auto-pick as before.
        boot_local_model = str(stored_cloud.get("local_model", "")).strip()
        boot_local = (
            build_ollama_provider(model=boot_local_model)
            if boot_local_model and not boot_cloud
            else None
        )
        self.brain = NovaBrain(
            completion_provider=build_llm_provider_from_environment(),
            ollama_reprobe=self._ollama_reprobe,
            on_provider_upgrade=lambda provider: self._sync_explore_provider(),
        )
        if boot_cloud is not None:
            self.brain.set_cloud_provider(boot_cloud)
        elif boot_local is not None:
            self.brain.set_local_provider(boot_local)
        self.brain.set_mode(boot_mode)
        # Server-persisted chat transcript: the SAME thread for every browser,
        # tab, and client — not a per-browser localStorage copy. The UI reads
        # it on load and appends via POST /chat/history; /chat/clear wipes it.
        self.chat_transcript = ChatTranscriptStore(self.state_store)
        memory_store = SqliteMemoryStore(self.data_dir / "memory.sqlite3")
        self.memory = MemoryManager(memory_store)
        self.self_improvement = SelfImprovementEngine(Path.cwd())
        # Explore publishes its progress on the ONE app-wide bus, so a single
        # activity channel (/events/stream) sees research steps live instead of
        # each request swapping a private bus into the service.
        self.explore = ExploreService(
            completion_provider=self.brain.completion_provider,
            event_bus=self.event_bus,
        )
        # Scratch mode means the local brain everywhere — Chat AND Explore
        # synthesis — while llm/auto keep whatever boot resolved.
        self._sync_explore_provider()
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
        self._improvement_previews: dict[str, dict[str, Any]] = {}
        # Server-side approval tokens: token -> {"command", "plan", "expires_at"}. Minted by
        # plan_desktop_command and consumed once by execute_desktop_command. A client can never
        # self-approve: execution requires a token the server itself issued for the exact command.
        self.approval_ttl_seconds = _APPROVAL_TTL_SECONDS
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        # Explicitly configured vision model (None = OCR/landmarks only). The
        # runner/vision/agentic wiring below reads it; set_vision_llm hot-swaps
        # it later via _apply_vision_provider.
        self.desktop = DesktopAutomationController(
            runner=LocalDesktopRunner(
                # The brain's multimodal provider (an Ollama vision model when
                # wired, the Echo fallback otherwise) feeds vision-guided
                # element location inside the runner that performs clicks.
                vision_provider=self.brain.completion_provider,
            ),
        )
        self.phone = PhoneControlController()
        self.browser = BrowserAutomationController(runner=PlaywrightBrowserRunner())
        # Vision-guided control + the shared bug log (data/bugs.json survives
        # restarts; the Vision panel and /bugs endpoints read the same file).
        self.bug_log = BugLog(self.data_dir / "bugs.json")
        self.vision = VisionController(
            self.desktop, bug_log=self.bug_log,
            llm_provider=self.brain.completion_provider,
        )
        # A dedicated vision model (configured in the Vision panel) overrides
        # the shared chat provider for element location — a user running a
        # small text chat model can still wire llava for vision. Restored from
        # the persisted config at the end of __init__ (_restore_vision_provider).
        self._vision_provider: object | None = None
        # THE Global Intelligence Layer — the single language brain every
        # entry point consumes before any subsystem sees raw text. It shares
        # the brain's LLM provider (semantic fallback only when deterministic
        # layers cannot parse) and keeps its own rolling interaction context.
        self.intelligence = GlobalInputIntelligence(
            completion_provider=self.brain.completion_provider,
        )

        # ── Agentic architecture (agentcore) ─────────────────────────
        # The orchestrator composes the controllers above; it owns task state,
        # planning, perception, verification, recovery, application knowledge,
        # and the evaluation ledger. Vision gets the REAL completion provider
        # (falling back internally when no multimodal model is available).
        self._agentic_knowledge = ApplicationKnowledgeGraph(
            self.state_store.read("agentic_knowledge") if self.state_store is not None else None
        )
        self._agentic_evaluation = EvaluationLedger()
        self._agentic_evaluation.load(self.state_store.read("agentic_evaluation") if self.state_store is not None else {})
        self._agentic_persist()
        self.agentic = AgenticOrchestrator(
            perception=UiPerceptionEngine(
                vision_processor=self._build_vision_processor(),
                browser_runner=self.browser.runner,
                completion_provider=self.brain.completion_provider,
            ),
            actions=ActionEngine(browser_runner=self.browser.runner, desktop_runner=self.desktop.runner),
            planner=AdaptivePlanner(),
            verifier=Verifier(),
            recovery=RecoveryEngine(knowledge=self._agentic_knowledge),
            knowledge=self._agentic_knowledge,
            evaluation=self._agentic_evaluation,
            interpreter=TaskInterpreter(completion_provider=self.brain.completion_provider),
            research_fn=self._agentic_research,
            persist_fn=self._agentic_persist,
            event_bus=self.event_bus,
        )
        # Last boot step: re-apply any persisted vision model so element
        # location uses it immediately (no restart, no UI round-trip).
        self._restore_vision_provider()

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

    # --- Brain mode (local scratch ↔ local LLM) ---

    def _build_vision_processor(self) -> object:
        """Multimodal vision processor backed by the live LLM provider.

        The old registration `VisionModule()` silently used the deterministic
        fallback forever; the agentic perception engine needs the real thing,
        with graceful internal fallback when no multimodal model is present.
        """
        from novacontrol.vision.multimodal import MultimodalVisionProcessor

        return MultimodalVisionProcessor(llm_provider=self.brain.completion_provider)

    async def _agentic_research(self, question: str) -> dict[str, Any]:
        """Research bridge: agentcore asks, the existing Explore engine answers.

        Returns a flat dict (summary + sources) so agentcore stays decoupled
        from the explore package.
        """
        report = await self.explore.research(
            ExploreRequest(topic=question, depth="quick", max_sources=4, include_videos=False)
        )
        return {
            "summary": report.overview or report.answer or report.detailed_explanation[:500],
            "sources": [source.to_dict() for source in report.sources[:5]],
        }

    def _agentic_persist(self) -> None:
        """Persist agentic knowledge and evaluation state as JSON namespaces."""
        if self.state_store is None:
            return
        try:
            self.state_store.write("agentic_knowledge", self._agentic_knowledge.to_dict())
            self.state_store.write("agentic_evaluation", self._agentic_evaluation.snapshot())
        except Exception:
            pass  # persistence failures never break a running task

    async def run_agentic_task(self, request: str) -> dict[str, Any]:
        """Run the full agentic loop for a natural-language goal."""
        state = await self.agentic.run_task(request)
        return state.to_dict()

    def agentic_metrics(self) -> dict[str, Any]:
        return self._agentic_evaluation.to_dict()

    def agentic_knowledge(self) -> dict[str, Any]:
        return self._agentic_knowledge.to_dict()

    def set_brain_mode(self, mode: str) -> dict[str, Any]:
        """Hot-swap the chat brain between auto, llm, and scratch.

        The brain is the source of truth for which provider runs; the setting is
        mirrored here so the choice survives restarts, and Explore's synthesizer
        follows the brain so "scratch" is local everywhere, not just in Chat.
        """
        if mode not in BRAIN_MODES:
            raise ValueError(
                f"Unknown brain mode: {mode!r}. Valid modes: {', '.join(BRAIN_MODES)}"
            )
        self.brain.set_mode(mode)
        self._sync_explore_provider()
        self.settings.update(brain_mode=mode)
        self.persist()
        return self.brain_status()

    def _sync_explore_provider(self) -> None:
        """Point Explore's synthesizer at the brain's live provider.

        Mirrors the brain mode EXACTLY: scratch (Echo) passes None so the
        synthesizer uses its local template path rather than trying an LLM
        call on Echo (which try_llm_synthesis would skip anyway — passing None
        keeps the two layers in lockstep by construction). llm/auto/cloud pass
        the brain's live provider as-is, so Chat and Explore always speak with
        the same voice. Report caching is keyed on the synthesis provider, so
        a mode switch never serves a report synthesized by the OLD brain.
        """
        provider = None if self.brain.provider_name == "scratch" else self.brain.completion_provider
        self.explore.explainer.set_completion_provider(provider)
        set_explore_cache_provider(self.explore, provider)

    def brain_status(self) -> dict[str, Any]:
        """What the UI's switch and AI Brain card render."""
        return {
            "mode": self.brain.mode,
            "effective_mode": self.brain.effective_mode,
            "provider": self.brain.provider_name,
            "model": self.brain.model_name,
            "model_configured": self.brain.model_configured,
            "cloud": self.cloud_llm_status(),
        }

    # --- Shared chat transcript (server-persisted, all clients) ---

    def chat_history(self) -> dict[str, Any]:
        """The persisted chat thread, oldest first (every client renders this)."""
        return {"turns": self.chat_transcript.turns()}

    def record_chat_turn(self, role: str, text: str, *, route: str = "") -> dict[str, Any]:
        """Append a client-reported turn (voice, CLI, anything not /ask)."""
        turn = self.chat_transcript.append(role, text, route=route)
        return {"recorded": turn is not None, "turns": len(self.chat_transcript.turns())}

    def import_chat_history(self, turns: list[dict[str, Any]]) -> dict[str, Any]:
        """One-time migration: merge a browser's localStorage thread into the
        shared transcript. Idempotent per (role, text, at) signature so a
        re-running migration can never duplicate turns."""
        existing = {
            (str(t.get("role")), str(t.get("text")), int(t.get("at") or 0))
            for t in self.chat_transcript.turns()
        }
        seen: set[tuple[str, str, int]] = set()
        fresh: list[dict[str, Any]] = []
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            signature = (str(turn.get("role", "")), str(turn.get("text", "")), int(turn.get("at") or 0))
            if signature in existing or signature in seen:
                continue
            seen.add(signature)
            fresh.append(turn)
        if not fresh:
            return {"imported": 0, "total": len(self.chat_transcript.turns())}
        merged = self.chat_transcript.turns() + fresh
        self.chat_transcript.replace_all(merged)
        return {"imported": len(fresh), "total": len(self.chat_transcript.turns())}

    # --- Local brain model picker (Ollama) ---

    async def local_models(self) -> dict[str, Any]:
        """Models available on the local Ollama for the brain model picker.

        Always a fresh probe (never the boot-time snapshot): models pulled
        after boot must appear. ``picked`` is the persisted choice ("" =
        auto-pick), not necessarily the currently active model.

        The probe is a BLOCKING urlopen, so it runs in a worker thread with
        a short timeout — a dead Ollama costs the picker ~300ms and never
        freezes the event loop (SSE streams and every other route stay live).
        """
        stored = self._cloud_llm_store.read("cloud_llm") if self._cloud_llm_store is not None else {}
        available = await asyncio.to_thread(ollama_models, timeout=0.5)
        return {
            "available": available,
            "picked": str(stored.get("local_model", "")),
            "active_model": self.brain.model_name,
        }

    def set_local_model(self, model: str) -> dict[str, Any]:
        """Pin the LOCAL brain to a specific Ollama model (hot swap, no restart).

        Empty string clears the pick (back to auto-picking the best detected
        model). The choice persists in the same gitignored store as the cloud
        LLM config and re-arms at boot. Only a local provider is touched: a
        configured cloud LLM stays exactly where it is, and the current mode
        decides whether the swap takes effect immediately (auto/llm) or when
        the user switches back (cloud/scratch).
        """
        model = model.strip()
        available = ollama_models()
        if model and available and model not in available:
            # Membership is only enforced when Ollama ANSWERS: unreachable
            # Ollama must not block a pick (it re-arms via the lazy re-probe
            # when Ollama starts), but a live probe catching a typo should.
            raise ValueError(
                f"Model {model!r} is not available on the local Ollama. "
                "Pull it first, e.g. `ollama pull " + model + "`."
            )
        stored = self._cloud_llm_store.read("cloud_llm") if self._cloud_llm_store is not None else {}
        self._cloud_llm_store.write("cloud_llm", {**stored, "local_model": model})
        provider = build_ollama_provider(model=model)
        if provider is None:
            # Persisted anyway: the pick re-arms via the lazy re-probe when
            # Ollama starts. With no Ollama now there is nothing to swap onto.
            return self.brain_status()
        self.brain.set_local_provider(provider)
        self._sync_explore_provider()
        self.persist()
        return self.brain_status()

    # --- Cloud LLM (ChatGPT / Gemini / Groq / …) ---

    def cloud_llm_presets(self) -> list[dict[str, str]]:
        """Provider picker metadata for the Settings panel (no secrets)."""
        return cloud_llm_presets()

    def cloud_llm_status(self) -> dict[str, Any]:
        """Configured cloud LLM summary. The API key is NEVER included — only
        a redacted tail, so no endpoint can leak it."""
        provider = self.brain.cloud_provider_name
        if not provider:
            return {"configured": False}
        preset = get_cloud_preset(provider.split(":", 1)[-1])
        # Lifetime telemetry from the live provider object (usage accumulates
        # on every completion; last_error holds the most recent failure).
        live = self.brain._cloud_provider
        usage = getattr(live, "usage", None)
        last_error = str(getattr(live, "last_error", "") or "")
        return {
            "configured": True,
            "provider": provider.split(":", 1)[-1],
            "label": preset["label"] if preset else provider,
            # The CONFIGURED model, shown whether or not Cloud is the active
            # mode: the Settings card describes what is saved, and the Brain
            # switch plus the status line carry what is actually running.
            "model": str(getattr(live, "model", "") or ""),
            "api_key_hint": self._redacted_cloud_key(),
            "usage": dict(usage) if isinstance(usage, dict) else None,
            "last_error": last_error,
        }

    def _redacted_cloud_key(self) -> str:
        stored = self._cloud_llm_store.read("cloud_llm") if self._cloud_llm_store is not None else {}
        key = str(stored.get("api_key", ""))
        return _redact_key(key) if key else ""

    def set_cloud_llm(self, provider_id: str, api_key: str, *, model: str = "") -> dict[str, Any]:
        """Install a cloud LLM (ChatGPT/Gemini/Groq/…) as the active brain.

        The provider + key persist locally (data/cloud_llm.json, gitignored);
        the key is only ever sent to the provider's own endpoint.
        """
        preset = get_cloud_preset(provider_id)
        if preset is None:
            raise ValueError(
                f"Unknown cloud LLM provider: {provider_id!r}. "
                f"Valid providers: {', '.join(row['id'] for row in CLOUD_LLM_PRESETS)}"
            )
        if not api_key.strip():
            raise ValueError("An API key is required to configure a cloud LLM.")
        # Paste-time format gate: a key that cannot be valid for this
        # provider (the Vertex-token-vs-AI-Studio-key mixup) is rejected
        # here, before anything is stored or the brain is switched.
        validate_cloud_key(provider_id, api_key)
        provider = build_cloud_provider(provider_id, api_key, model=model)
        assert provider is not None  # preset + non-empty key are validated above
        if self._cloud_llm_store is not None:
            self._cloud_llm_store.write("cloud_llm", {"provider": provider_id, "api_key": api_key.strip(), "model": model.strip()})
        # Installing a key only STORES it: the brain stays on whatever mode the
        # user is running (local by default) until they pick Cloud themselves in
        # the Brain switch. Auto-switching here is how a pasted key silently
        # became the brain for every chat and research request, cloud latency
        # and all.
        self.brain.set_cloud_provider(provider)
        self._sync_explore_provider()
        self.persist()
        return self.brain_status()

    def clear_cloud_llm(self) -> dict[str, Any]:
        """Remove the stored cloud LLM config and its API key."""
        if self._cloud_llm_store is not None:
            self._cloud_llm_store.write("cloud_llm", {})
        self.brain.set_cloud_provider(None)
        self._sync_explore_provider()
        self.persist()
        return self.brain_status()

    async def test_cloud_llm(self, provider_id: str, api_key: str, *, model: str = "") -> dict[str, Any]:
        """Ping a cloud provider with the PASTED key — before saving anything.

        Builds a throwaway provider (same construction path the real connect
        uses, so what is tested is exactly what would be stored) and sends one
        tiny completion. NOTHING is persisted and the brain is untouched: a
        failed test must not leave half-configured state behind. The response
        is honest about which stage failed: unreachable, auth rejected, or
        model unknown.
        """
        preset = get_cloud_preset(provider_id)
        if preset is None:
            raise ValueError(
                f"Unknown cloud LLM provider: {provider_id!r}. "
                f"Valid providers: {', '.join(row['id'] for row in CLOUD_LLM_PRESETS)}"
            )
        if not api_key.strip():
            raise ValueError("Paste an API key to test first.")
        # Same paste-time format gate as connect: catch impossible keys
        # before spending a network round-trip on a guaranteed 404/401.
        validate_cloud_key(provider_id, api_key)
        provider = build_cloud_provider(provider_id, api_key, model=model)
        assert provider is not None  # preset + non-empty key are validated above
        try:
            answer = await asyncio.wait_for(
                provider.complete([{"role": "user", "content": "Reply with the single word: ok"}], max_tokens=8),
                timeout=20.0,
            )
        except asyncio.TimeoutError as exc:
            raise ValueError(f"{preset['label']} did not respond within 20s — check your network or try later.") from exc
        except Exception as exc:  # noqa: BLE001 - every provider error is user-facing here
            raise ValueError(f"{preset['label']} rejected the connection: {type(exc).__name__}: {exc}") from exc
        answer = (answer or "").strip()
        return {
            "ok": True,
            "provider": provider_id,
            "label": preset["label"],
            "model": getattr(provider, "model", ""),
            "sample": answer[:80],
            "saved": False,
        }

    # -- Vision model configuration -----------------------------------

    # Namespaces in the same gitignored JsonStateStore as the chat cloud LLM:
    # keys never leave this machine except to the provider's own endpoint.
    _VISION_LLM_NAMESPACE = "vision_llm"

    def vision_llm_status(self) -> dict[str, Any]:
        """Configured vision model summary. The key is NEVER included — only a
        redacted tail — so no endpoint can leak it."""
        provider = self._vision_provider
        if provider is None:
            return {"configured": False}
        name = str(getattr(provider, "name", ""))
        surface = name.split(":", 1)[-1] if ":" in name else name
        stored = self._cloud_llm_store.read(self._VISION_LLM_NAMESPACE) if self._cloud_llm_store is not None else {}
        preset = get_cloud_preset(surface)
        return {
            "configured": True,
            "provider": surface,
            "label": ("Ollama (local)" if surface == "ollama" else (preset["label"] if preset else surface)),
            "model": str(getattr(provider, "model", "")),
            "api_key_hint": _redact_key(str(stored.get("api_key", ""))) if stored.get("api_key") else "",
        }

    def set_vision_llm(self, provider_id: str, credential: str = "", *, model: str = "") -> dict[str, Any]:
        """Install a multimodal model for the vision layer (hot swap, no restart).

        ``ollama`` probes the running instance for a vision model (llava,
        llama3.2-vision, …); a cloud preset id (openai/gemini/openrouter) uses
        the same key shape as the chat cloud LLM. The provider is swapped into
        every vision consumer: the desktop runner's element locator, the
        VisionController, and the agentcore perception engine.
        """
        provider, reason = build_vision_provider(provider_id, credential, model=model)
        if provider is None:
            raise ValueError(reason)
        if self._cloud_llm_store is not None:
            self._cloud_llm_store.write(
                self._VISION_LLM_NAMESPACE,
                {"provider": provider_id, "api_key": credential.strip(), "model": model.strip()},
            )
        self._apply_vision_provider(provider)
        return self.vision_llm_status()

    def clear_vision_llm(self) -> dict[str, Any]:
        """Remove the stored vision model config; the layer returns to OCR-only."""
        if self._cloud_llm_store is not None:
            self._cloud_llm_store.write(self._VISION_LLM_NAMESPACE, {})
        self._apply_vision_provider(None)
        return self.vision_llm_status()

    def _apply_vision_provider(self, provider: object | None) -> None:
        """Swap the vision provider into every consumer that locates elements.

        Boot-time wiring happens in __init__; this is the runtime path used by
        set/clear so a model can be installed or removed without a restart.
        """
        self._vision_provider = provider
        # 1. The desktop runner's locate_element (guided clicks).
        self.desktop.runner.vision_provider = provider
        # 2. The VisionController (describe/verify surface).
        self.vision.set_llm_provider(provider)
        # 3. The agentcore perception engine.
        self.agentic.perception._provider = provider

    def _restore_vision_provider(self) -> None:
        """Boot-time restore of a persisted vision model (never raises)."""
        stored = self._cloud_llm_store.read(self._VISION_LLM_NAMESPACE) if self._cloud_llm_store is not None else {}
        provider_id = str(stored.get("provider", ""))
        if not provider_id:
            return
        provider, _reason = build_vision_provider(
            provider_id,
            str(stored.get("api_key", "")),
            model=str(stored.get("model", "")),
        )
        if provider is not None:
            self._apply_vision_provider(provider)

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

    # -- Global Intelligence Layer --------------------------------------------

    # The ONE place raw language is interpreted. Every handler below consumes
    # the structured intent this produces — no subsystem re-parses text.
    _GIL_ROUTES: dict[IntentName, str] = {
        # Device / J.A.R.V.I.S surface.
        IntentName.OPEN_APPLICATION: "desktop",
        IntentName.CLOSE_APPLICATION: "desktop",
        IntentName.OPEN_FOLDER: "desktop",
        IntentName.TAKE_SCREENSHOT: "desktop",
        IntentName.TYPE_TEXT: "desktop",
        IntentName.PRESS_KEY: "desktop",
        IntentName.PHONE_OPEN_APP: "phone",
        IntentName.PHONE_SEND_TEXT: "phone",
        IntentName.PHONE_CALL: "phone",
        IntentName.PHONE_SCREENSHOT: "phone",
        IntentName.PHONE_CONNECT: "phone",
        IntentName.PHONE_STATUS: "phone",
        IntentName.NAVIGATE: "browser",
        IntentName.SEARCH_WEB: "browser",
        IntentName.EXTRACT_PAGE: "browser",
        IntentName.FILL_FORM: "browser",
        # Knowledge / research surface.
        IntentName.RESEARCH: "explore",
        IntentName.ANSWER_QUESTION: "chat",
        IntentName.SUMMARIZE: "explore",
        IntentName.COMPARE: "explore",
        IntentName.GENERATE_REPORT: "explore",
        IntentName.CHAT: "chat",
        # Planning / automation surface.
        IntentName.PLAN_TASK: "plan",
        IntentName.CREATE_AUTOMATION: "plan",
        IntentName.RUN_AUTOMATION: "plan",
        IntentName.SCHEDULE_TASK: "plan",
        # Memory / project / improvement surface (system status stays with the
        # brain, whose chat already reports status with full context).
        IntentName.REMEMBER: "memory",
        IntentName.RECALL: "memory",
        IntentName.CREATE_PROJECT: "project",
        IntentName.IMPROVE_SELF: "self_improvement",
        IntentName.AGENTIC_TASK: "agent",
        # System status: measured on this machine and answered without a model.
        IntentName.SYSTEM_STATUS: "system",
        IntentName.CPU_STATUS: "system",
        IntentName.MEMORY_STATUS: "system",
        IntentName.GPU_STATUS: "system",
        IntentName.BATTERY_STATUS: "system",
        IntentName.NETWORK_STATUS: "system",
        # Composite browser work routes to the real browser controller.
        IntentName.BROWSER_ACTION: "browser",
        # Visual understanding goes to the vision pipeline (a dedicated VLM),
        # never to a text-only chat model.
        IntentName.SCREENSHOT_ANALYSIS: "vision",
        # Language-only intents stay with the chat handler, which already knows
        # how to answer them locally (scratch) or through the configured model.
        IntentName.CONVERSATION: "chat",
        IntentName.GENERAL_QUESTION: "chat",
        IntentName.CALCULATE: "chat",
    }

    # Handler keys used by _GIL_ROUTES -> BrainIntent values (the legacy
    # handler table). One adapter keeps the two vocabularies decoupled.
    _GIL_HANDLER_KEYS: dict[str, BrainIntent] = {
        "desktop": BrainIntent.DESKTOP_AUTOMATION,
        "phone": BrainIntent.PHONE_CONTROL,
        "browser": BrainIntent.BROWSER_AUTOMATION,
        "explore": BrainIntent.EXPLORE,
        "chat": BrainIntent.CHAT,
        "plan": BrainIntent.PLAN,
        "memory": BrainIntent.MEMORY,
        "project": BrainIntent.PROJECT,
        "self_improvement": BrainIntent.SELF_IMPROVEMENT,
        "agent": BrainIntent.AGENT,
        "system": BrainIntent.SYSTEM_STATUS,
        "vision": BrainIntent.VISION,
    }

    async def handle_request(self, text: str) -> ApplicationResponse:
        """Route a natural-language request through the available subsystems.

        The Global Intelligence Layer understands first (normalization, typo
        tolerance, references, multi-intent decomposition) and picks the
        capability handler; the brain still runs that handler and shapes the
        response, so task records, conversation memory, and the response
        envelope stay identical to the legacy flow. Only what the GIL leaves
        unresolved reaches the legacy `brain.decide` classifier.
        """
        await self.memory.remember(
            MemoryNamespace.CONVERSATION,
            f"request-{len((await self.memory.retrieve(MemoryNamespace.CONVERSATION, '', limit=100)))}",
            {"text": text}, text=text, importance=0.2,
        )
        request = BrainRequest(text=text, context=self.status())
        task = self.tasks.create(text, kind="ask")
        self.tasks.update(task.id, TaskRecordStatus.RUNNING, progress=0.1)

        # GLOBAL INPUT INTELLIGENCE: choose the capability from meaning, not
        # exact phrasing (normalization, typo tolerance, references,
        # multi-intent). Fallback: the legacy brain classifier.
        understood = await self.intelligence.understand_async(text)
        gil_intent = understood.intent.intent if understood.strategy != "clarification" else None
        handler_key = self._GIL_ROUTES.get(gil_intent) if gil_intent is not None else None
        brain_intent = self._GIL_HANDLER_KEYS.get(handler_key) if handler_key is not None else None
        if brain_intent is not None:
            decision = BrainDecision(
                intent=brain_intent,
                reason=f"global-intelligence:{understood.strategy}",
                confidence=understood.intent.confidence,
            )
            # A chat-classified request that is really a research question
            # ("what is X", "compare A and B" — with no canned local answer)
            # goes to the SAME research pipeline as the Explore tab, so Chat
            # answers with a sourced report AND the UI receives explore.progress
            # events: live research stages replace the static scan-line. The
            # gate is the brain's own classifier, so both paths agree.
            if decision.intent is BrainIntent.CHAT:
                lower = text.strip().lower()
                if looks_like_research_question(lower) and scratchable_intent(lower) is None:
                    decision = BrainDecision(
                        intent=BrainIntent.EXPLORE,
                        reason="global-intelligence:research_question",
                        confidence=decision.confidence,
                    )
        else:
            decision = self.brain.decide(request)

        # Handlers receive the STRUCTURED understanding alongside the raw text,
        # so no capability has to re-parse the sentence to know what was asked.
        understanding = understood.intent
        request = BrainRequest(
            text=text,
            context={**request.context, "nlu": understanding.to_dict()},
        )
        handler = self._HANDLERS.get(decision.intent)
        if handler:
            route, payload = await handler(self, request, text)
        else:
            route, payload = await self._handle_agent(request, text)

        # The request-understanding block travels with every response: safe
        # operational metadata only (what understood it, the intent, the
        # confidence, the cost) — never chain-of-thought or model reasoning.
        payload = {**payload, "nlu": _nlu_payload(understanding, understood, decision, self.brain)}
        response = await self.brain.shape_response(request, decision, payload)
        self.tasks.update(task.id, TaskRecordStatus.COMPLETED, progress=1.0, result=response.to_dict())
        # Record the turn in the SHARED transcript so every client renders the
        # same thread (the UI used to keep its own per-browser copy).
        self.chat_transcript.append("user", text)
        self.chat_transcript.append(
            "assistant",
            response.summary,
            route=route or decision.intent.value,
        )
        # Downstream resolution ("open it", "do the same thing") needs this
        # history; remember only when the GIL actually understood the input.
        if gil_intent is not None:
            self.intelligence.context.remember_intent(understood.intent.to_dict())
            self.intelligence.context.remember_utterance(understood.intent.normalized_input)
        return ApplicationResponse(route, decision.intent.value, response.summary, payload)

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
        payload = report.to_dict()
        # Research answers join the conversation, so a follow-up in Chat has
        # the same context as one asked straight after a chat answer.
        self.brain.conversation.add_assistant_message(
            str(payload.get("answer") or payload.get("overview") or ""),
            metadata={"intent": "explore", "mode": "research"},
        )
        self.brain.conversation.add_turn(
            text, str(payload.get("answer") or payload.get("overview") or ""), intent="explore"
        )
        return "explore", payload

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
        # Teach path: "remember this/that: …" stores the fact as durable
        # KNOWLEDGE, not just conversation history — so the Learn tab's
        # "what did I teach you" recall works across sessions.
        for marker in ("remember this:", "remember that:", "remember:"):
            if marker in text.lower():
                fact = text.split(marker, 1)[-1].strip()
                if fact:
                    taught = await self.teach_knowledge(fact, source="chat")
                    return "memory", {
                        "mode": "memory_store",
                        "message": taught["message"],
                        "memory": taught["memory"],
                        "results": [],
                    }
        # Recall path: search taught knowledge first, then conversation memory.
        taught = await self.list_knowledge(text, limit=3)
        conversation = await self.memory.retrieve(MemoryNamespace.CONVERSATION, text, limit=5)
        results = [r.record.to_dict() | {"score": r.score} for r in conversation]
        if taught["facts"]:
            # Surface the best taught fact directly when it clearly matches.
            best = taught["facts"][0]
            if best["score"] >= 3.0:
                return "memory", {
                    "mode": "memory_recall",
                    "message": f"You taught me: {best['text']}",
                    "facts": taught["facts"],
                    "results": results,
                }
        return "memory", {
            "mode": "memory_recall",
            "message": "I don't have taught knowledge matching that yet — use the Learn tab or say 'remember this: …'.",
            "facts": [],
            "results": results,
        }

    async def _handle_project(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        project = self.projects.create_project(text[:60], description=text)
        return "project", project.to_dict()

    async def _handle_agent(self, _request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        response = await self.coordinator.delegate(AgentTask(text), self.agent_registry)
        return "agent", response.to_dict()

    # ── Deterministic and vision surfaces ─────────────────────────────────

    @property
    def _hardware_status(self) -> HardwareTelemetry:
        """The live machine reader, created once so CPU deltas have a baseline."""
        existing = getattr(self, "_hardware_status_reader", None)
        if existing is None:
            existing = HardwareTelemetry()
            self._hardware_status_reader = existing
        return cast(HardwareTelemetry, existing)

    async def _handle_system_status(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        """Answer a status question from the machine, never from a language model.

        A model has no access to this machine's RAM, CPU, or battery. Asking one
        would be slower, less accurate, and unable to carry the `available` flag
        the telemetry layer attaches to every metric it could not measure — so a
        measured reading is the only honest answer here.
        """
        understanding = request.context.get("nlu")
        kind = ""
        if isinstance(understanding, dict):
            kind = str(understanding.get("intent", ""))
        names = _STATUS_METRICS.get(kind, _STATUS_METRICS["system_status"])
        reader = self._hardware_status
        metrics = {name: getattr(reader, name)() for name in names}
        message = _system_status_message(kind, metrics)
        return "system", {
            "deterministic": True,
            "message": message,
            "summary": message,
            "metrics": metrics,
            "measured_at": time.time(),
        }

    async def _handle_vision(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        """Capture and interpret the screen through the vision pipeline.

        The dedicated vision provider (a VLM configured in the Vision panel) does
        the looking; the text chat model is never handed an image. When no vision
        model is wired, the controller reports what it could actually determine
        instead of inventing a description.
        """
        described = await self.vision.describe_screen()
        payload: dict[str, Any] = dict(described)
        payload.setdefault("summary", str(payload.get("message", "")))
        return "vision", payload

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
        BrainIntent.SYSTEM_STATUS: _handle_system_status,
        BrainIntent.VISION: _handle_vision,
    }

    # --- Learning ---

    _TEACH_PREFIXES = ("remember that ", "learn that ", "remember: ", "learn: ")

    def _teach_fact(self, text: str) -> str | None:
        """Strip teach phrasing ('remember that …', 'learn: …') -> the fact.

        Returns None when the text is a generic learning goal rather than a
        storable fact ("improve NovaControl", "write more tests").
        """
        lower = text.lower().strip()
        for prefix in self._TEACH_PREFIXES:
            if lower.startswith(prefix):
                fact = text[len(prefix):].strip()
                if fact:
                    return fact
        return None

    async def teach_knowledge(self, fact: str, *, source: str = "learn_tab") -> dict[str, Any]:
        """Persist a typed fact as recallable knowledge (KNOWLEDGE namespace).

        This is the Learn tab's real 'learn' path: what the user types becomes
        durable knowledge that chat and memory recall can retrieve later.
        """
        fact = fact.strip()
        if not fact:
            raise ValueError("Nothing to learn: the fact is empty.")
        existing = await self.memory.retrieve(MemoryNamespace.KNOWLEDGE, fact, limit=5)
        for result in existing:
            if result.record.text == fact:
                return {
                    "mode": "knowledge_teach",
                    "message": "I already know this.",
                    "memory": result.record.to_dict(),
                    "duplicate": True,
                }
        key = f"fact-{uuid4().hex[:12]}"
        record = await self.memory.remember(
            MemoryNamespace.KNOWLEDGE, key,
            {"fact": fact, "source": source},
            text=fact,
            importance=0.9,
        )
        self._record_activity("learn", "Knowledge learned", fact[:60])
        return {
            "mode": "knowledge_teach",
            "message": f"Learned: {fact[:80]}{'…' if len(fact) > 80 else ''}",
            "memory": record.to_dict(),
            "duplicate": False,
        }

    async def list_knowledge(self, query: str = "", *, limit: int = 20) -> dict[str, Any]:
        """List taught knowledge, optionally filtered by a search query."""
        results = await self.memory.retrieve(MemoryNamespace.KNOWLEDGE, query, limit=limit)
        facts = [result.record.to_dict() | {"score": result.score} for result in results]
        return {"mode": "knowledge_list", "facts": facts, "count": len(facts)}

    async def build_code_plan(self, goal: str, *, language: str = "python") -> dict[str, Any]:
        """Plan a coding task with language awareness and a concrete artifact.

        Uses the configured LLM to draft the code artifact when available, and
        always falls back to a deterministic plan (the engine knows language
        conventions: module docstrings, test scaffolds, language-appropriate
        file names) so the Build tab plans real coding work, not a generic
        three-step wrapper.
        """
        goal = goal.strip()
        if not goal:
            raise ValueError("Describe what to build first.")
        language = _CODE_LANGUAGES.get(language.lower().strip(), language.lower().strip() or "python")
        artifact_name = _code_artifact_name(goal, language)

        llm_code = ""
        completion = getattr(self.brain, "completion_provider", None)
        configured = bool(getattr(self.brain, "model_configured", False))
        agent_trace: list[dict[str, str]] = []
        agent_meta: dict[str, Any] = {}
        if completion is not None and configured:
            # Real coding agent: draft → run → read the actual error → fix →
            # re-run, bounded rounds. Code that merely parses isn't enough; the
            # artifact shipped is one that RAN clean (or the honest failure).
            from novacontrol.core.code_agent import run_coding_agent

            try:
                result = await run_coding_agent(goal, language, completion, filename=artifact_name)
                llm_code = result.content
                agent_trace = [s.to_dict() for s in result.steps]
                agent_meta = {
                    "ran_ok": result.ran_ok,
                    "fix_rounds": result.fix_rounds,
                    "final_output": result.final_output,
                    "model": result.model_name,
                    "generated_by": result.generated_by,
                }
            except Exception as exc:  # noqa: BLE001 - fallback plan must survive provider errors
                logger.warning("build_code_plan agent loop failed (%s); using deterministic plan", exc)
                llm_code = ""
                agent_trace = [{"kind": "gave_up", "detail": f"Agent loop failed: {exc}", "output": ""}]

        if llm_code:
            generated_by = str(agent_meta.get("generated_by", "llm"))
            verify = agent_meta.get("ran_ok")
            if verify is True:
                verified = " — verified: ran clean"
            elif verify is False:
                verified = " — ran with issues (see agent trace)"
            else:
                verified = ""
            artifact = {"path": artifact_name, "language": language, "content": llm_code, "generated_by": generated_by}
            summary = (
                f"Agent-drafted a {language} implementation for: {goal[:70]}{'…' if len(goal) > 70 else ''}"
                f"{verified}"
            )
        else:
            # No model wired: still return REAL, runnable starter code — an
            # empty editor made "Plan The Code" feel like nothing gets coded.
            # The hint tells the user exactly how to unlock full LLM drafting.
            steps = _code_plan_steps(goal, language)
            artifact = {
                "path": artifact_name,
                "language": language,
                "content": _scaffold_code(goal, language, artifact_name),
                "generated_by": "scaffold",
            }
            summary = (
                f"Scaffolded a {language} starter for: {goal[:70]}{'…' if len(goal) > 70 else ''}"
                " — connect an LLM (Ollama or a cloud key in Settings) for full auto-drafting"
            )

        steps = _code_plan_steps(goal, language)
        from novacontrol.planning.models import Plan, PlanStep

        plan = Plan(goal=goal, steps=tuple(
            PlanStep(title=title, description=description, id=steps_id(i))
            for i, (title, description) in enumerate(steps)
        ))
        self._record_activity("build", "Code plan", f"{language}: {goal[:50]}")
        return {
            "mode": "code_plan",
            "summary": summary,
            "language": language,
            "plan": plan.to_dict(),
            "artifact": artifact,
            "agent": {"steps": agent_trace, **agent_meta},
        }

    # Workspace root for saved Build artifacts: inside the checkout, gitignored
    # (never touches project code), sibling of the app's data dir.
    BUILD_WORKSPACE_DIRNAME = "build_workspace"

    def save_build_artifact(
        self,
        *,
        filename: str,
        content: str,
        language: str = "python",
        goal: str = "",
    ) -> dict[str, Any]:
        """Write a generated code artifact to the Build workspace on disk.

        This is the missing half of the Build tab: /plan/code can DRAFT code,
        but nothing ever wrote it to disk, so "nothing is getting coded". The
        artifact lands in build_workspace/ (gitignored, sibling of data/) and
        the response carries the absolute path so the UI can show it.
        Filenames are sanitized to a bare name — no paths, no traversal.
        """
        from novacontrol.core.build_workspace import safe_artifact_name

        safe_name = safe_artifact_name(filename, language)
        if not content.strip():
            raise ValueError("Nothing to save — the artifact is empty. Generate code first.")
        workspace = Path.cwd() / self.BUILD_WORKSPACE_DIRNAME
        workspace.mkdir(parents=True, exist_ok=True)
        target = workspace / safe_name
        target.write_text(content, encoding="utf-8")
        self._record_activity("build", "Artifact saved", f"{safe_name} ({language})")
        return {
            "mode": "artifact_saved",
            "path": str(target),
            "filename": safe_name,
            "language": language,
            "bytes": len(content.encode("utf-8")),
            "goal": goal[:120],
        }

    async def build_code_project(
        self, goal: str, *, language: str = "python"
    ) -> dict[str, Any]:
        """Plan AND draft a small multi-file project with the coding agent.

        The agent drafts a file map (2-5 files + entry point), runs the entry
        in the sandbox, feeds real errors back, and fixes until it runs clean
        or the budget is spent. Every file is returned so the UI can show the
        whole project; each file can then be saved to the workspace.
        """
        goal = goal.strip()
        if not goal:
            raise ValueError("Describe what to build first.")
        language = _CODE_LANGUAGES.get(language.lower().strip(), language.lower().strip() or "python")
        completion = getattr(self.brain, "completion_provider", None)
        configured = bool(getattr(self.brain, "model_configured", False))

        project_name = _code_artifact_name(goal, language).rsplit(".", 1)[0]

        result = None
        fallback_reason = ""
        if completion is not None and configured:
            from novacontrol.core.code_agent import run_project_agent

            try:
                result = await run_project_agent(goal, language, completion)
            except Exception as exc:  # noqa: BLE001 - scaffold fallback must survive provider errors
                logger.warning("build_code_project agent loop failed (%s); using deterministic scaffold", exc)
                fallback_reason = f"Agent loop failed: {exc}"

        if result is not None:
            files = result.files
            entry = result.entry
            ran_ok = result.ran_ok
            steps: list[dict[str, str]] = [s.to_dict() for s in result.steps]
            final_output = result.final_output
            fix_rounds = result.fix_rounds
            model_name = result.model_name
            generated_by = "agent"
            summary = (
                f"Agent-drafted a {len(files)}-file {language} project "
                f"(entry {entry})"
                + (" — verified: ran clean" if ran_ok
                   else " — ran with issues (see trace)")
            )
        else:
            # No model wired (or the provider hard-failed): still return a
            # REAL, runnable multi-file starter — an error wall made "Plan The
            # Project" feel like nothing gets coded. The scaffold's entry
            # point actually runs in the sandbox, and the trace records the
            # honest reason full agent drafting wasn't used.
            files, entry = _scaffold_project(goal, language)
            steps = []
            if fallback_reason:
                steps.append({"kind": "gave_up", "detail": fallback_reason, "output": ""})
            ran_ok = False
            final_output = ""
            fix_rounds = 0
            model_name = ""
            generated_by = "scaffold"
            from novacontrol.core.code_agent import runtime_available, run_sandboxed

            if runtime_available(language):
                try:
                    ran_ok, final_output = await run_sandboxed(
                        language,
                        [(f["path"], f["content"]) for f in files],
                        entry,
                    )
                    steps.append({
                        "kind": "run",
                        "detail": "Scaffold verified: entry point executed"
                        if ran_ok else "Scaffold entry point failed (see output)",
                        "output": final_output,
                    })
                except Exception as exc:  # noqa: BLE001 - sandbox failure must not hide the scaffold
                    steps.append({"kind": "gave_up", "detail": f"Sandbox execution failed: {exc}", "output": ""})
            else:
                steps.append({
                    "kind": "skipped",
                    "detail": f"No {language} runtime here — scaffold provided but not executed",
                    "output": "",
                })
            for f in files:
                f["generated_by"] = "scaffold"
            summary = (
                f"Scaffolded a {len(files)}-file {language} project starter (entry {entry})"
                + (" — verified: ran clean" if ran_ok else " — not executed here")
                + " — connect a working LLM (Ollama or a cloud key in Settings) for full auto-drafting"
            )

        self._record_activity("build", "Project drafted", f"{language}: {goal[:50]}")
        return {
            "mode": "code_project",
            "project": project_name,
            "goal": goal,
            "language": language,
            "entry": entry,
            "files": files,
            "ran_ok": ran_ok,
            "steps": steps,
            "final_output": final_output,
            "fix_rounds": fix_rounds,
            "model": model_name,
            "generated_by": generated_by,
            "summary": summary,
        }

    async def run_saved_artifact(self, filename: str) -> dict[str, Any]:
        """Re-execute a saved workspace artifact in the same sandbox.

        The Run button for build_workspace/ files: runs the saved code through
        run_sandboxed (fresh temp cwd, timeout, *nix network isolation) and
        reports the real output — no drafting, no model needed.
        """
        from novacontrol.core.build_workspace import safe_artifact_name
        from novacontrol.core.code_agent import RuntimeUnavailable, run_sandboxed

        safe_name = safe_artifact_name(filename, "python")
        path = Path.cwd() / self.BUILD_WORKSPACE_DIRNAME / safe_name
        if not path.is_file():
            raise ValueError(f"{safe_name} is not in the Build workspace — save it first.")
        language = {
            ".py": "python", ".js": "javascript",
        }.get(path.suffix.lower(), "")
        if not language:
            raise ValueError(
                f"{safe_name} is {path.suffix or 'unknown'} — only Python and "
                "JavaScript artifacts can be executed here."
            )
        code = path.read_text(encoding="utf-8")
        try:
            ran_ok, output = await run_sandboxed(language, [(safe_name, code)], safe_name)
        except RuntimeUnavailable as exc:
            raise ValueError(str(exc)) from exc
        self._record_activity("build", "Artifact run", safe_name)
        return {
            "mode": "artifact_run",
            "filename": safe_name,
            "language": language,
            "ran_ok": ran_ok,
            "output": output,
        }

    def list_workspace_artifacts(self) -> dict[str, Any]:
        """Saved artifacts in the Build workspace, newest first."""
        workspace = Path.cwd() / self.BUILD_WORKSPACE_DIRNAME
        if not workspace.is_dir():
            return {"artifacts": []}
        artifacts = []
        for path in sorted(workspace.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.is_file() and not path.name.startswith("."):
                artifacts.append({
                    "filename": path.name,
                    "bytes": path.stat().st_size,
                    "runnable": path.suffix.lower() in {".py", ".js"},
                })
        return {"artifacts": artifacts[:30]}

    async def learning_cycle(self, goal: str, *, feedback: str = "") -> dict[str, Any]:
        plan = self.self_improvement.plan(goal)
        key = f"learning-{len((await self.memory.retrieve(MemoryNamespace.LONG_TERM, 'learning', limit=100)))}"
        record = await self.memory.remember(
            MemoryNamespace.LONG_TERM, key,
            {"goal": goal, "feedback": feedback, "actions": [a.to_dict() for a in plan.actions], "findings": [f.to_dict() for f in plan.findings]},
            text=f"Learning cycle for {goal}. Feedback: {feedback}", importance=0.8,
        )
        self._record_activity("learn", "Learning cycle", goal)
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
        self._record_activity("learn", "Training run", goal)
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
        """Parse a JARVIS command into a workflow, mint an approval token, and return the plan.

        The desktop controller owns parsing and per-step planning (plan_command);
        this method only wraps the result in the plan envelope and mints the
        approval token, so a new step kind touches desktop/controller.py only.

        The NATIVE desktop parser plans the whole command first: its chain
        parser keeps cross-clause context ("open steam and go to library and
        launch gta v" knows 'library' and 'gta v' belong to Steam), which the
        GIL's clause-wise split destroys. The GIL clause-merge is the fallback
        for chains the native parser cannot span — e.g. "open notepad and
        take a screenshot", whose clauses belong to different domains. A
        native plan containing a fallback 'execute' step means the parser
        gave up, so the GIL merge takes over.
        """
        from novacontrol.desktop.models import DesktopActionType as _DAT
        from novacontrol.desktop.models import DesktopWorkflow as _DW

        combined, actions_descriptions, first_target = self.desktop.plan_command(command)
        native_failed = any(
            action.type is _DAT.EXECUTE_SCRIPT for action in combined.actions
        )
        if native_failed:
            # GIL clause-merge fallback: plan each clause in order and merge.
            understood = self.intelligence.understand(command)
            clauses = (
                [i.normalized_input for i in understood.intents]
                if understood.strategy == "multi_intent" and understood.intents
                else None
            )
            if clauses:
                merged_actions: list[Any] = []
                descriptions: list[str] = []
                first_target = ""
                for clause in clauses:
                    clause_workflow, clause_descriptions, clause_target = self.desktop.plan_command(clause)
                    merged_actions.extend(clause_workflow.actions)
                    descriptions.extend(clause_descriptions)
                    if not first_target:
                        first_target = clause_target
                combined = _DW(name=command[:40] or "Command", actions=tuple(merged_actions))
                actions_descriptions = descriptions
        plan: dict[str, Any] = {
            "route": "desktop_automation",
            "family": "desktop",
            "status": "waiting_for_approval",
            "command": command,
            "target": first_target,
            "steps": actions_descriptions,
            "summary": "Planned: " + " → ".join(actions_descriptions),
            "approval": {"required": True, "approved": False, "message": "Review the actions before running them on your computer."},
            "workflow": combined.to_dict(),
        }
        plan["approval"]["token"] = self._mint_approval(command, plan)
        return plan

    async def execute_desktop_command(
        self, command: str, *, approval_token: str | None = None, correlation_id: str = ""
    ) -> dict[str, Any]:
        """Execute a previously planned desktop command, guarded by a server-issued approval token.

        Execution requires a valid, unconsumed token minted by plan_desktop_command for the exact
        command; a missing, unknown, expired, or replayed token raises ValueError (HTTP 403) and
        nothing runs. Use plan_desktop_command to preview before approving.
        ``correlation_id`` (optional) tags the run's progress events so concurrent
        executions interleave cleanly in the UI; blank mints a server-side id.
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
            correlation_id=correlation_id,
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
            elif step["action"] == "search":
                # The parser already resolved the query into a search-engine URL.
                wf = self.browser.plan_navigation(step["target"])
                browser_actions.extend(wf.actions)
                # Follow the navigation with an extract step so execution
                # RETURNS the top results (an answer), not just a page load.
                extract_wf = self.browser.plan_search_results(limit=6)
                browser_actions.extend(extract_wf.actions)
                query = step.get("query") or step["target"]
                action_descriptions.append(
                    f"Search the web for {query} and read the top results"
                )
        if not browser_actions:
            return {
                "route": "browser_automation",
                "status": "not_an_action",
                "command": command,
                "target": "browser",
                "summary": "I couldn't plan a browser action from that. Try 'navigate to example.com', 'search the web for something', or 'fill the form at https://example.com/login with username=admin'.",
                "approval": {"required": False, "approved": False, "message": "No browser navigation or form-fill action was detected."},
                "workflow": {"id": "", "name": "No action", "actions": []},
            }
        combined = BrowserWorkflow(name=command[:60], actions=tuple(browser_actions))
        plan: dict[str, Any] = {
            "route": "browser_automation",
            "family": "browser",
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

    async def execute_browser_command(
        self, command: str, *, approval_token: str | None = None, correlation_id: str = ""
    ) -> dict[str, Any]:
        """Execute a previously planned browser command, guarded by a server-issued approval token.

        Browser actions (navigate, fill forms) require a valid, unconsumed token minted by
        plan_browser_command for the exact command; a missing, unknown, expired, or replayed token
        raises ValueError (HTTP 403) and nothing runs. Use plan_browser_command to preview first.
        ``correlation_id`` (optional) tags the run's progress events.
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
            correlation_id=correlation_id,
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
        correlation_id: str = "",
    ) -> dict[str, Any]:
        """Consume a valid token, rebuild the stored plan, and run it through a controller.

        Shared by desktop and browser execution: both validate the same token, rebuild the
        serialized workflow via _workflow_from_plan, execute under auto-approval (a server-minted
        token was already validated), then mark the plan executed.
        """
        plan = self._consume_approval(approval_token, command)
        # The token's plan records which device family minted it. The caller
        # dispatches by brain intent, but the inline Approve button posts
        # /command/execute for plans minted by ANY /plan endpoint — so the
        # stored family wins when it disagrees with the intent dispatch, or a
        # desktop execute_script plan would be rebuilt as a browser action and
        # crash ('execute_script' is not a valid BrowserActionType).
        family = str(plan.get("family", "") or "")
        family_classes = _workflow_classes_for_family(family)
        if family_classes is not None:
            action_cls, action_type_cls, workflow_cls = family_classes
        workflow = _workflow_from_plan(
            plan["workflow"],
            action_cls=action_cls, action_type_cls=action_type_cls, workflow_cls=workflow_cls,
        )

        # Announce each action on the app bus as it starts, so the single
        # activity channel shows live command progress while actions run. Every
        # announcement of ONE execution shares a run-scoped correlation_id so a
        # UI can interleave two concurrent executions of the same family
        # (two Approve And Runs in two tabs) without mixing their rows.
        correlation_id = correlation_id.strip() or uuid4().hex

        async def announce(detail: str) -> None:
            await self.event_bus.publish(
                Event(
                    type="command.progress",
                    payload={"step": "executing", "detail": detail, "correlation_id": correlation_id, "command": command},
                    source="command",
                )
            )

        # Execute with auto-approval (a server-minted token was validated above)
        controller.approval_gateway = ApprovedApprovalGateway()
        try:
            results = await controller.execute_workflow(workflow, progress=announce)
        finally:
            controller.approval_gateway = DenyByDefaultApprovalGateway()
        plan["status"] = "executed"
        plan["approval"]["approved"] = True
        plan["execution_results"] = [r.to_dict() for r in results]
        succeeded = sum(1 for r in results if r.status.value == "completed")
        plan["summary"] = f"Executed {succeeded}/{len(results)} {result_label}(s) successfully."
        # A search workflow carries an extract step AFTER its navigation; when
        # it ran, turn its results into the answer the user actually asked
        # for (top titles + links), not just a successful page load.
        if command.strip().lower().startswith(("search the web", "google ", "look up ", "web search")):
            answer = _summarize_search_results(plan["execution_results"])
            if answer:
                plan["summary"] = answer
                plan["search_answer"] = answer
        if succeeded:
            self._record_activity("command", "Command executed", command)
        return plan

    def _record_activity(self, type_: str, title: str, detail: str = "") -> None:
        """Record one completed action in the recent-activity journal AND
        announce it on the bus as ``<type>.completed`` so every open UI tab
        appends it to the Recent Activity timeline over the shared SSE channel
        — including actions that started from another client (CLI, GUI, a
        second tab). Journal is in-memory (bounded); the SSE frame carries the
        same {type, title, detail, at} shape as /activity returns."""
        self.activity.record(type_, title, detail)
        entry = self.activity.recent(limit=1)
        payload = dict(entry[0]) if entry else {"type": type_, "title": title, "detail": detail}
        self._bus_schedule(
            Event(type=f"{type_}.completed", payload=payload, source="activity")
        )

    def _bus_schedule(self, event: Event) -> None:
        """Publish on the app bus, tolerating a closed/absent loop (journal
        recording must never fail the completed action it reports)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.event_bus.publish(event))

    def plan_phone_command(self, command: str) -> dict[str, Any]:
        """Plan a phone workflow for open/text/call/screenshot phrasing.

        parse_phone_command classifies the phrasing; each kind maps to one
        controller planner so the workflow carries the right PhoneActionType.
        """
        kind, argument = parse_phone_command(command)
        if kind == "text":
            workflow = self.phone.plan_send_text(argument)
            summary = "Prepared a text message for your phone."
            target = workflow.actions[0].target or "messaging"
        elif kind == "call":
            contact = argument.replace("call ", "", 1).replace("dial ", "", 1).strip() or "contact"
            workflow = self.phone.plan_call(contact)
            summary = f"Prepared a call to {contact} on your phone."
            target = contact
        elif kind == "screenshot":
            workflow = self.phone.plan_screenshot()
            summary = "Prepared a screenshot capture on your phone."
            target = "screen"
        elif kind == "search":
            workflow = self.phone.plan_search(argument)
            query, provider = workflow.actions[0].parameters["query"], workflow.actions[0].parameters["provider"]
            summary = f"Prepared a {provider} search for {query!r} on your phone."
            target = provider
        elif kind == "open_files":
            workflow = self.phone.plan_open_file(argument)
            summary = f"Prepared to open {argument!r} on your phone."
            target = workflow.actions[0].target
        else:
            workflow = self.phone.plan_open_application(argument)
            summary = f"Prepared a phone action for {argument}."
            target = argument
        status = self.phone.status()
        plan: dict[str, Any] = {
            "route": "phone_control",
            "family": "phone",
            "status": "waiting_for_approval" if status.available else "waiting_for_phone_bridge",
            "command": command,
            "target": target,
            "summary": summary,
            "approval": {
                "required": True,
                "approved": False,
                "message": "Pair and review the phone action before running it.",
            },
            "bridge": status.to_dict(),
            "workflow": workflow.to_dict(),
        }
        if status.available:
            # A paired device can really be driven, so execution must be approval-gated
            # like desktop/browser: mint a token only the server can validate.
            plan["approval"]["token"] = self._mint_approval(command, plan)
        return plan

    async def execute_phone_command(
        self, command: str, *, approval_token: str | None = None, correlation_id: str = ""
    ) -> dict[str, Any]:
        """Run a planned phone action, guarded by a server-issued approval token.

        While no bridge/device is available, phone execution is not real: return the
        pairing plan so the UI guides the user to pair a device (nothing runs and no
        token is required). Once a device is available, execution requires a valid,
        unconsumed token minted by plan_phone_command for the exact command; a
        missing, unknown, expired, or replayed token raises ValueError (HTTP 403).
        """
        if not self.phone.status().available:
            return self.plan_phone_command(command)
        if not approval_token:
            raise ValueError(
                "Executing a phone action requires an approval token. "
                "Call /phone/plan first, review the preview, then execute with its token."
            )
        from novacontrol.phone.models import PhoneAction, PhoneActionType, PhoneWorkflow
        return await self._execute_planned_command(
            command, approval_token,
            controller=self.phone,
            action_cls=PhoneAction, action_type_cls=PhoneActionType, workflow_cls=PhoneWorkflow,
            result_label="phone action",
            correlation_id=correlation_id,
        )

    _PHONE_INTENTS = frozenset({
        IntentName.PHONE_OPEN_APP, IntentName.PHONE_SEND_TEXT, IntentName.PHONE_CALL,
        IntentName.PHONE_SCREENSHOT, IntentName.PHONE_CONNECT, IntentName.PHONE_STATUS,
    })
    _BROWSER_INTENTS = frozenset({
        IntentName.NAVIGATE, IntentName.SEARCH_WEB, IntentName.EXTRACT_PAGE, IntentName.FILL_FORM,
    })
    _DESKTOP_INTENTS = frozenset({
        IntentName.OPEN_APPLICATION, IntentName.CLOSE_APPLICATION, IntentName.OPEN_FOLDER,
        IntentName.TAKE_SCREENSHOT, IntentName.TYPE_TEXT, IntentName.PRESS_KEY,
    })

    def _dispatch_device_command(self, command: str) -> dict[str, Any]:
        """Route a device command to the appropriate plan function.

        The Global Intelligence Layer decides first (normalization, typo
        tolerance, references, multi-intent decomposition); the legacy brain
        is the fallback for anything it leaves unresolved.
        """
        understood = self.intelligence.understand(command)
        intent = understood.intent.intent if understood.strategy != "clarification" else None
        if intent in self._PHONE_INTENTS:
            return self.plan_phone_command(command)
        if intent in self._BROWSER_INTENTS:
            return self.plan_browser_command(command)
        if intent in self._DESKTOP_INTENTS:
            return self.plan_desktop_command(command)
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

    async def execute_command(
        self, command: str, *, approval_token: str | None = None, correlation_id: str = ""
    ) -> dict[str, Any]:
        """Execute a previously planned command, guarded by a server-issued approval token.

        ``correlation_id`` (optional) tags the run's progress events so two
        concurrent executions of the same family interleave cleanly in the UI.
        """
        decision = self.brain.decide(BrainRequest(text=command, context=self.status()))
        if decision.intent is BrainIntent.DESKTOP_AUTOMATION:
            return await self.execute_desktop_command(command, approval_token=approval_token, correlation_id=correlation_id)
        if decision.intent is BrainIntent.BROWSER_AUTOMATION:
            return await self.execute_browser_command(command, approval_token=approval_token, correlation_id=correlation_id)
        if decision.intent is BrainIntent.PHONE_CONTROL:
            return await self.execute_phone_command(command, approval_token=approval_token, correlation_id=correlation_id)
        return self._dispatch_device_command(command)

    # --- Status ---

    def status(self) -> dict[str, Any]:
        return {
            "runtime_started": self.runtime.state.started,
            "modules": self.runtime.module_names(),
            "skills": [skill.schema.name for skill in self.skills.list()],
            "scheduled_tasks": len(self.scheduler.tasks()),
            "tracked_tasks": len(self.tasks.list()),
            # Trimmed views (no `result` blobs — a completed /ask embeds whole
            # reports) so the System panel can list tasks with delete buttons
            # without inflating every BrainRequest context.
            "tasks": [
                {
                    "id": task.id,
                    "title": task.title,
                    "kind": task.kind,
                    "status": task.status.value,
                    "created_at": task.created_at.isoformat(),
                }
                for task in self.tasks.list()[-40:]
            ],
            "projects": len(self.projects.list_projects()),
            "automation_workflows": len(self.automation.list_workflows()),
            "desktop_available": True,
            "browser_available": True,
            "desktop_runner": type(self.desktop.runner).__name__,
            "browser_runner": type(self.browser.runner).__name__,
            "browser_adapter_available": PlaywrightBrowserRunner.is_available(),
            "self_improvement_available": True,
            "brain": self.brain_status(),
            "phone_bridge": self.phone.status().to_dict(),
            "settings": self.settings.to_dict(),
            # Global Intelligence Layer: interpretation health + the capability
            # table the orchestrator plans from (self-improvement feed).
            "intelligence": {
                "telemetry": self.intelligence.telemetry.to_dict(),
                "findings": self.intelligence.telemetry.improvement_findings(),
                "capabilities": self.intelligence.capabilities.to_dict(),
                # The live routing policy, so "why did that go to the model?" is
                # answerable from the running system rather than from the source.
                "thresholds": self.intelligence.thresholds.to_dict(),
                "lexical_exemplars": self.intelligence.lexical.size,
            },
        }


# --- Coding-plan helpers (Build tab) -----------------------------------------

logger = logging.getLogger(__name__)

_CODE_LANGUAGES: dict[str, str] = {
    "py": "python", "python": "python",
    "js": "javascript", "javascript": "javascript", "node": "javascript",
    "ts": "typescript", "typescript": "typescript",
    "java": "java", "c": "c", "cpp": "cpp", "c++": "cpp", "c#": "csharp", "cs": "csharp",
    "go": "go", "golang": "go", "rust": "rust", "rs": "rust",
    "rb": "ruby", "ruby": "ruby", "php": "php", "swift": "swift", "kotlin": "kotlin",
    "sh": "shell", "bash": "shell", "shell": "shell", "sql": "sql", "html": "html", "css": "css",
}

_CODE_EXT: dict[str, str] = {
    "python": "py", "javascript": "js", "typescript": "ts", "java": "java",
    "c": "c", "cpp": "cpp", "csharp": "cs", "go": "go", "rust": "rs",
    "ruby": "rb", "php": "php", "swift": "swift", "kotlin": "kt",
    "shell": "sh", "sql": "sql", "html": "html", "css": "css",
}

_CODE_TEST_NAMES: dict[str, str] = {
    "python": "test_{name}.py",
    "javascript": "{name}.test.js",
    "typescript": "{name}.test.ts",
    "java": "{name}Test.java",
    "go": "{name}_test.go",
    "rust": "{name}.rs",  # tests live in the same file behind #[cfg(test)]
    "ruby": "{name}_spec.rb",
    "shell": "test_{name}.sh",
}


def _code_artifact_name(goal: str, language: str) -> str:
    """Derive a kebab/snake artifact file name from the goal words."""
    words = [w for w in re.findall(r"[a-zA-Z0-9]+", goal.lower()) if w not in {
        "a", "an", "the", "write", "create", "build", "make", "implement", "code",
        "program", "function", "class", "script", "in", "for", "that", "with", "to",
        "of", "and", "me", "my", "please", "can", "you",
    }][:4]
    stem = "_".join(words) or "artifact"
    return f"{stem}.{_CODE_EXT.get(language, 'txt')}"


def _scaffold_code(goal: str, language: str, artifact_name: str) -> str:
    """Language-appropriate, RUNNABLE starter code for the typed goal.

    Deterministic by design (no model needed): a correct docstringed entry
    point, a working example implementation for the common "function/util"
    shape, and a main guard. The user edits from something that already runs —
    not from an empty file. Falls back to a commented header for languages
    without a dedicated template.
    """
    title = goal.strip().rstrip(".") or "the task"
    func = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40] or "solution"
    if func[0].isdigit():
        func = f"task_{func}"
    templates: dict[str, str] = {
        "python": (
            f'"""{title}.\n\nGenerated by NovaControl Build — edit freely, then Save to disk.\n"""\n\n'
            f"\ndef {func}(text: str = '') -> str:\n"
            f'    """Solve: {title}.\n\n'
            '    Replace this body with the real implementation; the shape\n'
            '    below already runs and is trivially testable.\n'
            '    """\n'
            '    if not text:\n'
            '        return ""\n'
            '    return text.strip()\n\n\n'
            'def _self_check() -> None:\n'
            '    """Quick smoke check: `python ' + artifact_name + '` runs it."""\n'
            f'    assert {func}("  hi ") == "hi"\n'
            '    print("self-check passed")\n\n\n'
            'if __name__ == "__main__":\n'
            '    _self_check()\n'
        ),
        "javascript": (
            f"/**\n * {title}.\n * Generated by NovaControl Build — edit freely, then Save to disk.\n */\n\n"
            f"export function {func}(input) {{\n"
            '  if (input == null) return "";\n'
            '  return String(input).trim();\n'
            '}\n\n'
            'if (import.meta.url === `file://${process.argv[1]}`) {\n'
            '  console.log("self-check", {func}("  hi ") === "hi");\n'
            '}\n'
        ),
    }
    if language in templates:
        return templates[language]
    return (
        f"// {title}\n"
        f"// Generated by NovaControl Build — edit freely, then Save to disk.\n\n"
        f"// TODO: implement {func}\n"
    )


def _scaffold_project(goal: str, language: str) -> tuple[list[dict[str, str]], str]:
    """Deterministic multi-file starter for the typed goal (no model needed).

    Project-scope sibling of _scaffold_code: a small, honest file map whose
    entry point actually runs. Python gets main.py + a logic module + a real
    pytest file; JavaScript gets main.js + a logic module + package.json with
    "type": "module" so the ESM import executes under plain `node` in the
    sandbox. Other languages get a structured two-file header split. Every
    file's content is runnable-or-honest — nothing decorative that lies.
    """
    title = goal.strip().rstrip(".") or "the task"
    func = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40] or "solution"
    if func[0].isdigit():
        func = f"task_{func}"
    stem = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:32] or "project"

    def files_map(pairs: list[tuple[str, str]]) -> list[dict[str, str]]:
        return [{"path": path, "content": content} for path, content in pairs]

    if language == "python":
        # The logic module's import name must be a valid identifier; fall
        # back to a plain name when the goal-derived stem is not.
        module = f"{stem}_logic"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module):
            module = "logic"
        logic_file = f"{module}.py"
        files = files_map([
            (
                "main.py",
                f'"""{title} — entry point.\n\n'
                'Generated by NovaControl Build (project scaffold) — edit freely,\n'
                'then save each file to disk from the Build tab.\n"""\n\n'
                f"from {module} import {func}\n\n\n"
                "def main() -> None:\n"
                f'    demo = {func}("  NovaControl ")\n'
                f'    print("{func} demo ->", repr(demo))\n\n\n'
                'if __name__ == "__main__":\n'
                "    main()\n",
            ),
            (
                logic_file,
                f'"""{title} — core module.\n\n'
                'Generated by NovaControl Build — edit freely.\n"""\n\n\n'
                f"def {func}(text: str = '') -> str:\n"
                f'    """Solve: {title}.\n\n'
                '    Replace this body with the real implementation; the shape\n'
                '    below already runs and is trivially testable.\n'
                '    """\n'
                '    if not text:\n'
                '        return ""\n'
                '    return text.strip()\n',
            ),
            (
                f"test_{stem}.py",
                f'"""Tests for: {title}.\"""\n\n'
                f"from {module} import {func}\n\n\n"
                f"def test_{func}_trims_input() -> None:\n"
                f'    assert {func}("  hi ") == "hi"\n\n\n'
                f"def test_{func}_empty_input() -> None:\n"
                f'    assert {func}("") == ""\n',
            ),
        ])
        return files, "main.py"

    if language == "javascript":
        logic_file = f"{stem}_logic.js"
        files = files_map([
            (
                "main.js",
                f"/**\n * {title} — entry point.\n"
                " * Generated by NovaControl Build (project scaffold) — edit freely.\n */\n\n"
                f'import {{ {func} }} from "./{logic_file}";\n\n'
                f'console.log("{func} demo ->", {func}("  NovaControl "));\n',
            ),
            (
                logic_file,
                f"/**\n * {title} — core module.\n"
                " * Generated by NovaControl Build — edit freely.\n */\n\n"
                f"export function {func}(input) {{\n"
                '  if (input == null) return "";\n'
                "  return String(input).trim();\n"
                "}\n",
            ),
            (
                "package.json",
                '{\n'
                f'  "name": "{stem}",\n'
                '  "type": "module",\n'
                '  "private": true\n'
                '}\n',
            ),
        ])
        return files, "main.js"

    ext = _CODE_EXT.get(language, "txt")
    header = (
        f"// {title} — {{role}}\n"
        "// Generated by NovaControl Build (project scaffold) — edit freely.\n\n"
        "// TODO: implement\n"
    )
    files = files_map([
        (f"main.{ext}", header.format(role="entry point")),
        (f"{stem}_logic.{ext}", header.format(role="core module")),
    ])
    return files, f"main.{ext}"


def _code_plan_steps(goal: str, language: str) -> list[tuple[str, str]]:
    """Language-aware coding plan steps (deterministic, no fake data)."""
    test_name = _CODE_TEST_NAMES.get(language, "test_{name}").format(
        name=re.sub(r"\.[a-z]+$", "", _code_artifact_name(goal, language))
    )
    return [
        ("Clarify requirements", f"Pin down inputs, outputs, and edge cases for: {goal}"),
        (f"Design the {language} module", f"Choose functions/classes and data flow for {_code_artifact_name(goal, language)}"),
        (f"Implement in {language}", f"Write the core logic in {_code_artifact_name(goal, language)} following {language} conventions"),
        ("Write tests", f"Add {test_name} covering happy path and edge cases"),
        ("Verify", f"Run the {language} toolchain (lint + tests) and fix findings"),
    ]
