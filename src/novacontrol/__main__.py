"""Command line entrypoint for NovaControl."""

from __future__ import annotations

import asyncio
from typing import Any

from novacontrol.cli.commands import (
    run_doctor,
    run_explore,
    run_gui,
    run_harden,
    run_health,
    run_improve,
    run_package,
    run_plan,
    run_ask,
    run_settings,
    run_status,
    run_train,
    run_vision,
)
from novacontrol.cli.demos import run_phase_demo
from novacontrol.cli.helpers import print_json
from novacontrol.cli.parser import COMPLETED_PHASES, build_parser


def main(argv: list[str] | None = None) -> None:
    """Run NovaControl commands."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args.command = "status"

    if args.command == "status":
        run_status()
    elif args.command == "phases":
        print_json(_phase_summary())
    elif args.command == "demo":
        asyncio.run(_run_demo(args.phase))
    elif args.command == "plan":
        run_plan(args.goal, execute=args.execute)
    elif args.command == "explore":
        asyncio.run(run_explore(args.topic, depth=args.depth, include_videos=not args.no_videos, max_sources=args.sources, max_videos=args.videos))
    elif args.command == "vision":
        asyncio.run(run_vision(args.source, args.task))
    elif args.command == "gui":
        run_gui(dry_run=args.dry_run)
    elif args.command == "ask":
        asyncio.run(run_ask(args.request))
    elif args.command == "doctor":
        run_doctor()
    elif args.command == "package":
        run_package()
    elif args.command == "health":
        run_health()
    elif args.command == "harden":
        run_harden()
    elif args.command == "improve":
        asyncio.run(run_improve(args.goal))
    elif args.command == "settings":
        run_settings(args.approval_mode, args.no_videos)
    elif args.command == "train":
        asyncio.run(run_train(args.goal, iterations=args.iterations, feedback=args.feedback))
    else:
        parser.error(f"Unsupported command: {args.command}")


async def _run_demo(phase: str) -> None:
    phases = COMPLETED_PHASES if phase == "all" else (phase,)
    output: dict[str, Any] = {}
    for selected in phases:
        output[selected] = await run_phase_demo(selected)
    print_json(output)


def _phase_summary() -> dict[str, Any]:
    return {
        "phase1": {"name": "Project foundation", "command": "python -m novacontrol demo phase1", "does": "Loads config and shows the package is runnable."},
        "phase2": {"name": "Core engine", "command": "python -m novacontrol demo phase2", "does": "Starts runtime, publishes events, records journal, reports health."},
        "phase3": {"name": "Memory", "command": "python -m novacontrol demo phase3", "does": "Stores, retrieves, searches, and summarizes memory."},
        "phase4": {"name": "Tool system", "command": "python -m novacontrol demo phase4", "does": "Registers and executes a schema-validated tool."},
        "phase5": {"name": "Agents", "command": "python -m novacontrol demo phase5", "does": "Routes a task through the coordinator to a specialized agent."},
        "phase6": {"name": "Planning", "command": "python -m novacontrol demo phase6", "does": "Creates and executes a dependency-aware workflow plan."},
        "phase7": {"name": "Desktop automation", "command": "python -m novacontrol demo phase7", "does": "Plans desktop automation and demonstrates approval denial by default."},
        "phase8": {"name": "Browser automation", "command": "python -m novacontrol demo phase8", "does": "Plans browser automation and demonstrates approval denial by default."},
        "phase9": {"name": "Vision", "command": "python -m novacontrol demo phase9", "does": "Runs baseline image/screen understanding."},
        "phase10": {"name": "Voice", "command": "python -m novacontrol demo phase10", "does": "Runs baseline transcription, wake word detection, TTS, and interruption."},
        "phase11": {"name": "GUI", "command": "python -m novacontrol demo phase11", "does": "Builds the dashboard state model and verifies the Explore tab."},
        "phase12": {"name": "API", "command": "python -m novacontrol demo phase12", "does": "Lists REST/WebSocket routes and verifies bearer-token authentication."},
        "phase13": {"name": "Plugin marketplace", "command": "python -m novacontrol demo phase13", "does": "Installs a safe plugin and shows sensitive plugin denial by default."},
        "phase14": {"name": "Performance optimization", "command": "python -m novacontrol demo phase14", "does": "Records metrics, uses cache, profiles work, and runs a load-test helper."},
        "phase15": {"name": "Documentation and release", "command": "python -m novacontrol demo phase15", "does": "Runs the release readiness checker."},
        "phase16": {"name": "AI brain and provider integration", "command": "python -m novacontrol demo phase16", "does": "Classifies requests and routes them through the central brain."},
        "phase17": {"name": "Local persistence", "command": "python -m novacontrol demo phase17", "does": "Persists projects, knowledge, scheduler, automation, and memory state."},
        "phase18": {"name": "Task center and approval polish", "command": "python -m novacontrol demo phase18", "does": "Tracks ask requests as long-running task records."},
        "phase19": {"name": "External adapter hardening", "command": "python -m novacontrol demo phase19", "does": "Verifies cached Explore research and provider integration behavior."},
        "phase20": {"name": "Packaging and local install doctor", "command": "python -m novacontrol demo phase20", "does": "Checks Python, dependencies, data directory, and launch scripts."},
        "phase21": {"name": "Real local adapters", "command": "python -m novacontrol demo phase21", "does": "Uses approved local desktop execution and reports Playwright browser adapter readiness."},
        "phase22": {"name": "Runtime packaging", "command": "python -m novacontrol demo phase22", "does": "Builds the local launch manifest for dashboard, API, tests, doctor, and package commands."},
        "phase23": {"name": "System health monitoring", "command": "python -m novacontrol demo phase23", "does": "Aggregates doctor, launch manifest, app modules, and adapter readiness into one health report."},
        "phase24": {"name": "Self-coding intelligence", "command": "python -m novacontrol demo phase24", "does": "Inspects the codebase, builds a self-improvement plan, and safely applies approved bounded code changes."},
        "phase25": {"name": "User settings and policies", "command": "python -m novacontrol demo phase25", "does": "Persists local user settings for approval behavior, detailed explanations, and Explore videos."},
        "phase26": {"name": "Dashboard intelligence controls", "command": "python -m novacontrol demo phase26", "does": "Verifies the dashboard Intelligence tab and produces health plus self-improvement reports."},
        "phase27": {"name": "Local web and voice platform", "command": "python -m novacontrol demo phase27", "does": "Serves a browser UI for chat, research, improvement, learning, health, voice input, and speech output."},
        "phase28": {"name": "Release hardening", "command": "python -m novacontrol demo phase28", "does": "Checks environment, launch scripts, syntax, and test coverage presence before local use."},
        "phase29": {"name": "Autonomous local learning loop", "command": "python -m novacontrol demo phase29", "does": "Runs bounded feedback-memory learning iterations that produce safer self-improvement plans."},
        "phase30": {"name": "Friendly web workflow UX", "command": "python -m novacontrol demo phase30", "does": "Verifies the website uses friendly pages, voice controls, future-ready system tabs, and done/current/remaining improvement steps."},
        "phase31": {"name": "AI Mode chat and research UX", "command": "python -m novacontrol demo phase31", "does": "Verifies Chat and Explore render query chips, readable AI answers, source rails, and video cards instead of raw data."},
        "explore": {"name": "Explore research backend", "command": 'python -m novacontrol explore "your topic"', "does": "Searches online, explains clearly, and includes related videos."},
    }


if __name__ == "__main__":
    main()
