"""Phase demo handlers for NovaControl CLI."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from novacontrol.agents import AgentRegistry, AgentTask, CoordinatorAgent, build_default_agents
from novacontrol.application import NovaControlApplication
from novacontrol.browser import BrowserAutomationController, PlaywrightBrowserRunner
from novacontrol.core.config import NovaControlConfig
from novacontrol.core.events import Event, EventBus
from novacontrol.core.journal import InMemoryEventJournal
from novacontrol.core.runtime import EventDrivenRuntime
from novacontrol.desktop import DesktopAutomationController, LocalDesktopRunner
from novacontrol.explore import ExploreRequest, ExploreService
from novacontrol.gui import DashboardTab, DashboardViewModel
from novacontrol.memory import InMemoryMemoryStore, MemoryManager, MemoryNamespace
from novacontrol.performance import LoadTester, MetricsRegistry, Profiler, TtlCache
from novacontrol.planning import PlanningEngine, WorkflowExecutor
from novacontrol.plugins import PluginCapability, PluginManifest, PluginMarketplace
from novacontrol.release import (
    EnvironmentDoctor,
    ReleaseReadinessChecker,
    ReleaseHardeningChecker,
    RuntimePackageBuilder,
    SystemHealthMonitor,
)
from novacontrol.self_improvement import CodeChange, SelfImprovementEngine
from novacontrol.settings import ApprovalMode, SettingsManager
from novacontrol.tools import FunctionTool, ToolExecutor, ToolParameter, ToolRegistry, ToolRequest, ToolSchema
from novacontrol.vision import BasicScreenUnderstandingProcessor, VisionTaskType
from novacontrol.voice import (
    BasicSpeechRecognizer,
    InterruptibleConversationManager,
    KeywordWakeWordDetector,
    TtsRequest,
)

from novacontrol.cli.helpers import AllowApprovalGateway


async def run_phase_demo(phase: str) -> Mapping[str, Any]:
    """Run a single phase demo and return the result."""
    handler = _PHASE_HANDLERS.get(phase)
    if handler is None:
        raise ValueError(f"Unsupported phase: {phase}")
    return await handler()


async def _phase1() -> Mapping[str, Any]:
    config = NovaControlConfig.from_environment()
    return {"status": "ok", "name": config.app.name, "environment": config.app.environment}


async def _phase2() -> Mapping[str, Any]:
    journal = InMemoryEventJournal()
    bus = EventBus(journal=journal)
    runtime = EventDrivenRuntime(bus)
    seen: list[str] = []

    async def capture(event: Event) -> None:
        seen.append(event.type)

    await bus.subscribe("*", capture)
    await runtime.start()
    health = await runtime.health()
    events = await journal.read()
    await runtime.stop()
    return {
        "status": "ok",
        "health": [report.state.value for report in health],
        "events_seen": seen,
        "journaled_events": [event.type for event in events],
    }


async def _phase3() -> Mapping[str, Any]:
    manager = MemoryManager(InMemoryMemoryStore())
    await manager.remember(
        MemoryNamespace.CONVERSATION,
        "preference",
        {"preference": "clear detailed explanations"},
        text="User prefers clear, detailed explanations until they understand.",
        importance=0.9,
    )
    results = await manager.retrieve(MemoryNamespace.CONVERSATION, "detailed explanations")
    return {
        "status": "ok",
        "top_memory": results[0].record.to_dict(),
        "summary": await manager.summarize(MemoryNamespace.CONVERSATION),
    }


async def _phase4() -> Mapping[str, Any]:
    registry = ToolRegistry()
    registry.register(
        FunctionTool(
            "echo",
            ToolSchema(
                "echo",
                "Echo a message.",
                (ToolParameter("message", "string", required=True),),
            ),
            lambda arguments: {"echo": arguments["message"]},
        )
    )
    result = await ToolExecutor(registry).execute(
        ToolRequest("echo", {"message": "Tool system is working."})
    )
    return {"status": "ok", "result": result.to_dict()}


async def _phase5() -> Mapping[str, Any]:
    registry = AgentRegistry()
    for agent in build_default_agents():
        registry.register(agent)
    response = await CoordinatorAgent().delegate(AgentTask("Research vector databases"), registry)
    return {"status": "ok", "response": response.to_dict()}


async def _phase6() -> Mapping[str, Any]:
    engine = PlanningEngine()
    plan = engine.create_plan("Research options then implement the best one")
    result = await WorkflowExecutor().execute(plan)
    return {"status": "ok", "plan": plan.to_dict(), "workflow": result.to_dict()}


async def _phase7() -> Mapping[str, Any]:
    controller = DesktopAutomationController()
    workflow = controller.plan_open_application("Code")
    results = await controller.execute_workflow(workflow)
    return {
        "status": "safe-default",
        "workflow": workflow.to_dict(),
        "results": [result.to_dict() for result in results],
    }


async def _phase8() -> Mapping[str, Any]:
    controller = BrowserAutomationController()
    workflow = controller.plan_navigation("https://example.com")
    results = await controller.execute_workflow(workflow)
    return {
        "status": "safe-default",
        "workflow": workflow.to_dict(),
        "results": [result.to_dict() for result in results],
    }


async def _phase9() -> Mapping[str, Any]:
    processor = BasicScreenUnderstandingProcessor()
    image = await processor.understand_image("nova_control_dashboard.png")
    screen = await processor.understand_screen("nova_control_dashboard.png")
    return {"status": "ok", "image": image.to_dict(), "screen": screen.to_dict()}


async def _phase10() -> Mapping[str, Any]:
    recognizer = BasicSpeechRecognizer()
    wake_detector = KeywordWakeWordDetector("nova")
    conversation = InterruptibleConversationManager()
    transcript = await recognizer.transcribe("nova please explain the project")
    wake_word = await wake_detector.detect(transcript.text)
    speech = await conversation.speak(TtsRequest("NovaControl voice baseline is working."))
    await conversation.interrupt("demo interruption")
    return {
        "status": "ok",
        "transcript": transcript.to_dict(),
        "wake_word": wake_word.to_dict(),
        "speech": speech.to_dict(),
        "conversation_state": conversation.state.value,
    }


async def _phase11() -> Mapping[str, Any]:
    view_model = DashboardViewModel.with_default_tabs()
    view_model.activate(DashboardTab.EXPLORE)
    view_model.update_counts(tasks=1, memories=1, plugins=0)
    return {"status": "ok", "dashboard": view_model.state.to_dict()}


async def _phase12() -> Mapping[str, Any]:
    from novacontrol.api import ApiSurface, ApiTokenAuthenticator

    authenticator = ApiTokenAuthenticator("demo-token")
    return {
        "status": "ok",
        "api": ApiSurface.default().to_dict(),
        "auth": {
            "missing": authenticator.authenticate(None).authenticated,
            "valid": authenticator.authenticate("Bearer demo-token").authenticated,
        },
        "server_command": "python -m uvicorn novacontrol.api.app:create_app --factory --reload",
    }


async def _phase13() -> Mapping[str, Any]:
    marketplace = PluginMarketplace()
    safe = PluginManifest(
        name="safe-demo",
        version="1.0.0",
        description="Safe demo plugin",
        capabilities=(PluginCapability("demo-skill", "skill", "demo:skill"),),
    )
    sensitive = PluginManifest(
        name="sensitive-demo",
        version="1.0.0",
        description="Sensitive demo plugin",
        permissions=("network:access",),
    )
    safe_record = await marketplace.install(safe)
    trusted_record = await marketplace.trust(safe.name)
    enabled_record = await marketplace.enable(safe.name)
    denied_record = await marketplace.install(sensitive)
    return {
        "status": "ok",
        "safe_install": safe_record.to_dict(),
        "safe_trusted": trusted_record.to_dict(),
        "safe_enabled": enabled_record.to_dict(),
        "sensitive_denied": denied_record.to_dict(),
    }


async def _phase14() -> Mapping[str, Any]:
    metrics = MetricsRegistry()
    cache: TtlCache[str] = TtlCache()
    cache.set("answer", "cached", ttl_seconds=60)
    metrics.increment("demo.requests")
    metrics.gauge("cache.size", cache.size())

    async def operation() -> str:
        return cache.get("answer") or "missing"

    profile = await Profiler().measure_async("cache-read", operation)
    load = await LoadTester().run(operation, requests=5, concurrency=2)
    metrics.timing("cache.read.seconds", profile.duration_seconds)
    return {
        "status": "ok",
        "cache_value": profile.result,
        "profile": profile.to_dict(),
        "load": load.to_dict(),
        "metrics": metrics.snapshot(),
    }


async def _phase15() -> Mapping[str, Any]:
    report = ReleaseReadinessChecker(Path.cwd()).check()
    return {"status": "ok" if report.ready else "missing-files", "release": report.to_dict()}


async def _phase16() -> Mapping[str, Any]:
    app = NovaControlApplication()
    await app.start()
    try:
        planning = await app.handle_request("make a roadmap for NovaControl")
        agent = await app.handle_request("create tests for the scheduler")
        return {
            "status": "ok",
            "planning": planning.to_dict(),
            "agent": agent.to_dict(),
            "app": app.status(),
        }
    finally:
        await app.stop()


async def _phase17() -> Mapping[str, Any]:
    with TemporaryDirectory() as temp_dir:
        app = NovaControlApplication(data_dir=Path(temp_dir))
        app.projects.create_project("Persisted Project")
        app.knowledge.add_article("Persistence", "Local state survives restarts")
        app.scheduler.schedule_once("demo-task", delay_seconds=60)
        app.automation.create_workflow("Demo Flow", ())
        await app.handle_request("remember persistence works")
        await app.stop()
        restored = NovaControlApplication(data_dir=Path(temp_dir))
        memory_results = await restored.memory.retrieve("conversation", "persistence", limit=5)
        return {
            "status": "ok",
            "projects": [project.to_dict() for project in restored.projects.list_projects()],
            "knowledge": [result.to_dict() for result in restored.knowledge.search("persistence")],
            "scheduled_tasks": [task.to_dict() for task in restored.scheduler.tasks()],
            "automation": [workflow.to_dict() for workflow in restored.automation.list_workflows()],
            "memory_count": len(memory_results),
        }


async def _phase18() -> Mapping[str, Any]:
    app = NovaControlApplication()
    await app.start()
    try:
        response = await app.handle_request("create tests for the scheduler")
        return {
            "status": "ok",
            "response": response.to_dict(),
            "tasks": [task.to_dict() for task in app.tasks.list()],
        }
    finally:
        await app.stop()


async def _phase19() -> Mapping[str, Any]:
    request = ExploreRequest("adapter caching demo", max_sources=2, max_videos=1)
    service = ExploreService()
    first = await service.research(request)
    second = await service.research(request)
    return {
        "status": "ok",
        "cache_reused": first.id == second.id,
        "report": second.to_dict(),
    }


async def _phase20() -> Mapping[str, Any]:
    report = EnvironmentDoctor(Path.cwd()).run()
    return {"status": "ok" if report.ok else "needs-attention", "doctor": report.to_dict()}


async def _phase21() -> Mapping[str, Any]:
    desktop = DesktopAutomationController(
        approval_gateway=AllowApprovalGateway(),
        runner=LocalDesktopRunner(command_timeout_seconds=5),
    )
    workflow = desktop.plan_execute_script("echo phase21-real-adapter")
    results = await desktop.execute_workflow(workflow)
    app = NovaControlApplication()
    status = app.status()
    await app.stop()
    return {
        "status": "ok",
        "desktop": {
            "workflow": workflow.to_dict(),
            "results": [result.to_dict() for result in results],
        },
        "browser": {
            "runner": PlaywrightBrowserRunner.__name__,
            "playwright_available": PlaywrightBrowserRunner.is_available(),
        },
        "app": {
            "desktop_runner": status["desktop_runner"],
            "browser_runner": status["browser_runner"],
            "browser_adapter_available": status["browser_adapter_available"],
        },
    }


async def _phase22() -> Mapping[str, Any]:
    package = RuntimePackageBuilder(Path.cwd()).build()
    return {"status": "ok" if package.ready else "missing-scripts", "package": package.to_dict()}


async def _phase23() -> Mapping[str, Any]:
    app = NovaControlApplication()
    status = app.status()
    report = SystemHealthMonitor(Path.cwd()).run(status)
    await app.stop()
    return {"status": "ok" if report.ok else "needs-attention", "health": report.to_dict()}


async def _phase24() -> Mapping[str, Any]:
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "src" / "novacontrol").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "src" / "novacontrol" / "__init__.py").write_text("", encoding="utf-8")
        engine = SelfImprovementEngine(root)
        plan = engine.plan("make NovaControl code itself and improve")
        results = await engine.apply_changes(
            (
                CodeChange(
                    "src/novacontrol/generated_improvement.py",
                    '"""Generated by NovaControl self-improvement demo."""\n\nVALUE = "improved"\n',
                    "Create a bounded approved source change.",
                ),
            ),
            approval_gateway=AllowApprovalGateway(),
        )
        return {
            "status": "ok",
            "plan": plan.to_dict(),
            "applied_changes": [result.to_dict() for result in results],
        }


async def _phase25() -> Mapping[str, Any]:
    with TemporaryDirectory() as temp_dir:
        app = NovaControlApplication(data_dir=Path(temp_dir))
        app.settings.update(approval_mode=ApprovalMode.DENY, include_videos_in_explore=False)
        await app.stop()
        restored = NovaControlApplication(data_dir=Path(temp_dir))
        status = restored.status()
        await restored.stop()
        return {
            "status": "ok",
            "settings": restored.settings.to_dict(),
            "app_status": status["settings"],
        }


async def _phase26() -> Mapping[str, Any]:
    view_model = DashboardViewModel.with_default_tabs()
    view_model.activate(DashboardTab.INTELLIGENCE)
    app = NovaControlApplication()
    plan = app.self_improvement.plan("make NovaControl more capable at coding itself")
    health = SystemHealthMonitor(Path.cwd()).run(app.status())
    await app.stop()
    return {
        "status": "ok" if health.ok else "needs-attention",
        "dashboard": view_model.state.to_dict(),
        "plan": plan.to_dict(),
        "health": health.to_dict(),
    }


async def _phase27() -> Mapping[str, Any]:
    static_dir = Path("src/novacontrol/web/static")
    required = ("index.html", "styles.css", "app.js")
    missing = [name for name in required if not (static_dir / name).exists()]
    app = NovaControlApplication()
    learning = await app.learning_cycle(
        "make NovaControl think and improve code safely",
        feedback="Use local feedback records and bounded approved code plans.",
    )
    await app.stop()
    return {
        "status": "ok" if not missing else "missing-web-assets",
        "url": "http://127.0.0.1:8000",
        "launch": "cmd /c scripts/run_api.cmd",
        "assets": {"missing": missing, "static_dir": str(static_dir)},
        "voice": {
            "input": "Browser SpeechRecognition when supported.",
            "output": "Browser speechSynthesis.",
        },
        "learning": learning,
    }


async def _phase28() -> Mapping[str, Any]:
    report = ReleaseHardeningChecker(Path.cwd()).run()
    return {"status": "ok" if report.ready else "needs-attention", "hardening": report.to_dict()}


async def _phase29() -> Mapping[str, Any]:
    app = NovaControlApplication()
    result = await app.autonomous_learning_loop(
        "improve NovaControl coding intelligence",
        iterations=3,
        feedback="Prefer tests first and no unsafe automatic writes.",
    )
    await app.stop()
    return {"status": "ok", "training": result}


async def _phase30() -> Mapping[str, Any]:
    static_dir = Path("src/novacontrol/web/static")
    app = NovaControlApplication()
    workflow = app.improvement_workflow("become the best AI agent available")
    await app.stop()
    return {
        "status": "ok",
        "website": {
            "url": "http://127.0.0.1:8000",
            "friendly_outputs": True,
            "raw_output_location": "CLI tab only",
            "voice_input": "browser SpeechRecognition",
            "voice_output": "browser speechSynthesis",
            "future_ready": True,
            "assets": sorted(path.name for path in static_dir.iterdir()),
        },
        "workflow": {
            "completed": len(workflow["completed"]),
            "current": workflow["current"]["title"],
            "remaining": len(workflow["remaining"]),
        },
    }


async def _phase31() -> Mapping[str, Any]:
    static_dir = Path("src/novacontrol/web/static")
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    css = (static_dir / "styles.css").read_text(encoding="utf-8")
    js = (static_dir / "app.js").read_text(encoding="utf-8")
    return {
        "status": "ok",
        "chat": {
            "ai_mode_output": "chatStream" in html and "renderChatResult" in js,
            "text_chat": "chatInput" in html and "askButton" in html,
            "voice_input": "voiceButton" in html and "SpeechRecognition" in js,
            "voice_chat": "voiceChatButton" in html and "submitChat" in js,
            "voice_output": "speakButton" in html and "speechSynthesis" in js,
        },
        "build": {
            "temporary_preview": "previewButton" in html and "/improve/preview" in js,
            "approval": "approveButton" in html and "/improve/approve" in js,
        },
        "research": {
            "ai_answer_layout": "ai-answer-layout" in css and "renderAiAnswerPage" in js,
            "warnings": "answer-notice" in css,
            "query_chip": "query-chip" in css,
            "source_rail": "source-rail" in css and "sourceRail" in js,
            "video_cards": "video-card" in css and "videoCard" in js and "thumbnail_url" in js,
        },
        "raw_output_location": "CLI tab",
    }


async def _phase_explore() -> Mapping[str, Any]:
    request = ExploreRequest("NovaControl AI operating system", max_sources=2, max_videos=1)
    report = await ExploreService().research(request)
    return {"status": "ok", "report": report.to_dict()}


# Phase handler registry
_PHASE_HANDLERS: dict[str, Callable[[], Awaitable[Mapping[str, Any]]]] = {
    "phase1": _phase1,
    "phase2": _phase2,
    "phase3": _phase3,
    "phase4": _phase4,
    "phase5": _phase5,
    "phase6": _phase6,
    "phase7": _phase7,
    "phase8": _phase8,
    "phase9": _phase9,
    "phase10": _phase10,
    "phase11": _phase11,
    "phase12": _phase12,
    "phase13": _phase13,
    "phase14": _phase14,
    "phase15": _phase15,
    "phase16": _phase16,
    "phase17": _phase17,
    "phase18": _phase18,
    "phase19": _phase19,
    "phase20": _phase20,
    "phase21": _phase21,
    "phase22": _phase22,
    "phase23": _phase23,
    "phase24": _phase24,
    "phase25": _phase25,
    "phase26": _phase26,
    "phase27": _phase27,
    "phase28": _phase28,
    "phase29": _phase29,
    "phase30": _phase30,
    "phase31": _phase31,
    "explore": _phase_explore,
}
