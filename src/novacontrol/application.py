"""NovaControl application composition root."""

from __future__ import annotations

import asyncio
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
from novacontrol.core.events import Event, EventBus
from novacontrol.core.runtime import EventDrivenRuntime
from novacontrol.core.security import ApprovalDecision, ApprovalRequest, DenyByDefaultApprovalGateway
from novacontrol.desktop import DesktopAutomationController, DesktopAutomationModule, LocalDesktopRunner
from novacontrol.desktop.vision import VisionController
from novacontrol.explore import ExploreModule, ExploreRequest, ExploreService
from novacontrol.intelligence import GlobalInputIntelligence, UnderstandResult
from novacontrol.intelligence.intent import IntentName, RiskLevel
from novacontrol.integrations import (
    CLOUD_LLM_PRESETS,
    build_cloud_provider,
    build_llm_provider_from_environment,
    build_vision_provider,
    cloud_llm_presets,
    get_cloud_preset,
    make_ollama_reprobe,
)
from novacontrol.integrations.llm import _redact_key
from novacontrol.knowledge import KnowledgeBase
from novacontrol.memory import MemoryManager, MemoryModule, MemoryNamespace, SqliteMemoryStore
from novacontrol.persistence import JsonStateStore
from novacontrol.phone import PhoneControlController, PhoneControlModule
from novacontrol.planning import PlanningEngine, PlanningModule, WorkflowExecutor
from novacontrol.plugins import PluginMarketplaceModule
from novacontrol.projects import ProjectManager
from novacontrol.scheduler import InMemoryScheduler
from novacontrol.self_improvement import CodeChange, SelfImprovementEngine
from novacontrol.settings import BRAIN_MODES, SettingsManager
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
        # Lazy Ollama upgrade: if boot found no LLM, each chat request probes for a
        # freshly started Ollama (rate-limited) and hot-swaps it in — for the brain
        # AND Explore synthesis — without a server restart. The callback resolves
        # self.explore lazily: it fires on a chat request, long after __init__.
        self._ollama_reprobe = make_ollama_reprobe()
        # Read the persisted brain mode FIRST so the boot provider honors it: a
        # user who switched to scratch must not get one LLM answer before the UI
        # loads. The on_provider_upgrade callback resolves self.explore lazily —
        # it fires on a chat request, long after __init__.
        self.data_dir = Path(data_dir) if data_dir is not None else self._DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_store = JsonStateStore(self.data_dir)
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
        # Persisted cloud LLM config (provider/model + the API key). The store
        # lives in the app's data dir (gitignored runtime state, same as
        # tasks.json) — the key NEVER leaves this machine except to the
        # provider itself, and no API endpoint ever returns it.
        # _cloud_llm_store: JsonStateStore namespace name; read()/write() go
        # through self.state_store.read("cloud_llm") / .write("cloud_llm", …).
        self._cloud_llm_store = self.state_store
        stored_cloud = self._cloud_llm_store.read("cloud_llm") if self._cloud_llm_store is not None else {}
        boot_cloud = build_cloud_provider(
            str(stored_cloud.get("provider", "")),
            str(stored_cloud.get("api_key", "")),
            model=str(stored_cloud.get("model", "")),
        ) if stored_cloud.get("provider") and stored_cloud.get("api_key") else None
        self.brain = NovaBrain(
            completion_provider=build_llm_provider_from_environment(),
            ollama_reprobe=self._ollama_reprobe,
            on_provider_upgrade=lambda provider: self._sync_explore_provider(),
        )
        if boot_cloud is not None:
            self.brain.set_cloud_provider(boot_cloud)
        self.brain.set_mode(boot_mode)
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
        """Point Explore's synthesizer at the brain's live provider."""
        self.explore.explainer.set_completion_provider(self.brain.completion_provider)

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
        return {
            "configured": True,
            "provider": provider.split(":", 1)[-1],
            "label": preset["label"] if preset else provider,
            "model": self.brain.model_name if self.brain.mode == "cloud" else "",
            "api_key_hint": self._redacted_cloud_key(),
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
        provider = build_cloud_provider(provider_id, api_key, model=model)
        assert provider is not None  # preset + non-empty key are validated above
        if self._cloud_llm_store is not None:
            self._cloud_llm_store.write("cloud_llm", {"provider": provider_id, "api_key": api_key.strip(), "model": model.strip()})
        self.brain.set_cloud_provider(provider)
        self._sync_explore_provider()
        self.settings.update(brain_mode="cloud")
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

        handler = self._HANDLERS.get(decision.intent)
        if handler:
            route, payload = await handler(self, request, text)
        else:
            route, payload = await self._handle_agent(request, text)

        response = await self.brain.shape_response(request, decision, payload)
        self.tasks.update(task.id, TaskRecordStatus.COMPLETED, progress=1.0, result=response.to_dict())
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
                query = step.get("query") or step["target"]
                action_descriptions.append(f"Search the web for {query}")
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
        else:
            workflow = self.phone.plan_open_application(argument)
            summary = f"Prepared a phone action for {argument}."
            target = argument
        status = self.phone.status()
        plan: dict[str, Any] = {
            "route": "phone_control",
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
            },
        }
