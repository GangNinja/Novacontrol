"""CLI command handlers for NovaControl."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from novacontrol.application import NovaControlApplication
from novacontrol.core.config import NovaControlConfig
from novacontrol.explore import ExploreRequest, ExploreService
from novacontrol.planning import PlanningEngine, WorkflowExecutor
from novacontrol.release import EnvironmentDoctor, ReleaseHardeningChecker, RuntimePackageBuilder, SystemHealthMonitor
from novacontrol.settings import ApprovalMode, SettingsManager
from novacontrol.vision import VisionTaskType, BasicScreenUnderstandingProcessor

from novacontrol.cli.helpers import print_json


def run_status() -> None:
    """Show runtime status."""
    config = NovaControlConfig.from_environment()
    print_json(
        {
            "name": config.app.name,
            "environment": config.app.environment,
            "log_level": config.app.log_level,
            "status": "complete-through-phase-31",
            "next_phase": "phase32_plugin_and_external_service_expansion",
            "commands": {
                "all_demos": "python -m novacontrol demo all",
                "ask": 'python -m novacontrol ask "explain transformers in AI"',
                "doctor": "python -m novacontrol doctor",
                "explore": 'python -m novacontrol explore "transformers in AI"',
                "harden": "python -m novacontrol harden",
                "health": "python -m novacontrol health",
                "improve": 'python -m novacontrol improve "make NovaControl better at coding itself"',
                "package": "python -m novacontrol package",
                "plan": 'python -m novacontrol plan "research options then implement the best one" --execute',
                "settings": "python -m novacontrol settings",
                "tests": "python -m unittest discover -s tests",
                "train": 'python -m novacontrol train "improve coding intelligence" --iterations 3',
            },
        }
    )


def run_plan(goal: str, *, execute: bool) -> None:
    """Create and optionally execute a workflow plan."""
    plan = PlanningEngine().create_plan(goal)
    payload: dict[str, Any] = {"plan": plan.to_dict()}
    if execute:
        payload["workflow"] = asyncio.run(WorkflowExecutor().execute(plan)).to_dict()
    print_json(payload)


async def run_explore(topic: str, *, depth: str, include_videos: bool, max_sources: int, max_videos: int) -> None:
    """Research a topic online."""
    report = await ExploreService().research(
        ExploreRequest(topic, depth=depth, include_videos=include_videos, max_sources=max_sources, max_videos=max_videos)
    )
    print_json(report.to_dict())


async def run_vision(source: str, task: str) -> None:
    """Run a vision task against a source."""
    processor = BasicScreenUnderstandingProcessor()
    task_type = VisionTaskType(task)
    if task_type is VisionTaskType.OCR:
        result = (await processor.ocr(source)).to_dict()
    elif task_type is VisionTaskType.SCREEN_UNDERSTANDING:
        result = (await processor.understand_screen(source)).to_dict()
    elif task_type is VisionTaskType.WINDOW_DETECTION:
        result = {"windows": [window.to_dict() for window in await processor.detect_windows(source)]}
    elif task_type is VisionTaskType.IMAGE_UNDERSTANDING:
        result = (await processor.understand_image(source)).to_dict()
    else:
        result = (await processor.understand_document(source)).to_dict()
    print_json({"task": task_type.value, "source": source, "result": result})


def run_gui(*, dry_run: bool) -> None:
    """Launch the desktop GUI."""
    if dry_run:
        from novacontrol.gui import DashboardViewModel

        view_model = DashboardViewModel.with_default_tabs()
        print_json({"status": "ok", "dashboard": view_model.state.to_dict()})
        return
    from novacontrol.gui.app import main as run_gui_main

    run_gui_main()


def run_doctor() -> None:
    """Check local environment readiness."""
    report = EnvironmentDoctor(Path.cwd()).run()
    print_json({"status": "ok" if report.ok else "needs-attention", "doctor": report.to_dict()})


def run_package() -> None:
    """Show local launch manifest."""
    package = RuntimePackageBuilder(Path.cwd()).build()
    print_json({"status": "ok" if package.ready else "missing-scripts", "package": package.to_dict()})


def run_health() -> None:
    """Show aggregated system health."""
    app = NovaControlApplication()
    report = SystemHealthMonitor(Path.cwd()).run(app.status())
    asyncio.run(app.stop())
    print_json({"status": "ok" if report.ok else "needs-attention", "health": report.to_dict()})


def run_harden() -> None:
    """Run release hardening checks."""
    report = ReleaseHardeningChecker(Path.cwd()).run()
    print_json({"status": "ok" if report.ready else "needs-attention", "hardening": report.to_dict()})


def run_settings(approval_mode: str | None, no_videos: bool) -> None:
    """Show or update user settings."""
    manager = SettingsManager()
    if approval_mode is not None or no_videos:
        manager.update(
            approval_mode=ApprovalMode(approval_mode) if approval_mode is not None else None,
            include_videos_in_explore=False if no_videos else None,
        )
    print_json({"status": "ok", "settings": manager.to_dict()})


async def run_improve(goal: str) -> None:
    """Create a self-improvement plan."""
    app = NovaControlApplication()
    await app.start()
    try:
        response = await app.handle_request(goal)
        print_json({"status": "ok", "response": response.to_dict()})
    finally:
        await app.stop()


async def run_train(goal: str, *, iterations: int, feedback: str) -> None:
    """Run bounded autonomous learning iterations."""
    app = NovaControlApplication(data_dir=Path("data"))
    await app.start()
    try:
        result = await app.autonomous_learning_loop(goal, iterations=iterations, feedback=feedback)
        print_json({"status": "ok", "training": result})
    finally:
        await app.stop()


async def run_ask(request: str) -> None:
    """Ask NovaControl through the integrated app router."""
    app = NovaControlApplication()
    await app.start()
    try:
        response = await app.handle_request(request)
        print_json({"status": "ok", "app": app.status(), "response": response.to_dict()})
    finally:
        await app.stop()
