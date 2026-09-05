"""CLI argument parser for NovaControl."""

from __future__ import annotations

import argparse

COMPLETED_PHASES = (
    "phase1", "phase2", "phase3", "phase4", "phase5", "phase6", "phase7",
    "phase8", "phase9", "phase10", "phase11", "phase12", "phase13", "phase14",
    "phase15", "phase16", "phase17", "phase18", "phase19", "phase20", "phase21",
    "phase22", "phase23", "phase24", "phase25", "phase26", "phase27", "phase28",
    "phase29", "phase30", "phase31", "explore",
)


def build_parser() -> argparse.ArgumentParser:
    """Build the NovaControl CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="novacontrol",
        description="NovaControl phase demos and utilities.",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("status", help="Show runtime status.")
    subparsers.add_parser("phases", help="List completed phases and demo commands.")
    subparsers.add_parser("doctor", help="Check local NovaControl environment readiness.")
    subparsers.add_parser("harden", help="Run release hardening checks.")
    subparsers.add_parser("health", help="Show aggregated system health.")
    subparsers.add_parser("package", help="Show local launch manifest.")

    improve = subparsers.add_parser("improve", help="Create a self-improvement plan.")
    improve.add_argument("goal")

    settings = subparsers.add_parser("settings", help="Show or preview user settings.")
    settings.add_argument("--approval-mode", choices=("ask", "deny"))
    settings.add_argument("--no-videos", action="store_true")

    train = subparsers.add_parser("train", help="Run bounded autonomous local learning iterations.")
    train.add_argument("goal")
    train.add_argument("--feedback", default="")
    train.add_argument("--iterations", type=int, default=3)

    ask = subparsers.add_parser("ask", help="Ask NovaControl through the integrated app router.")
    ask.add_argument("request")

    demo = subparsers.add_parser("demo", help="Run a phase demo.")
    demo.add_argument("phase", choices=(*COMPLETED_PHASES, "all"))

    plan = subparsers.add_parser("plan", help="Create and optionally execute a workflow plan.")
    plan.add_argument("goal")
    plan.add_argument("--execute", action="store_true")

    explore_cmd = subparsers.add_parser("explore", help="Research a topic online and include videos.")
    explore_cmd.add_argument("topic")
    explore_cmd.add_argument("--depth", default="deep", choices=("simple", "deep"))
    explore_cmd.add_argument("--no-videos", action="store_true")
    explore_cmd.add_argument("--sources", type=int, default=6)
    explore_cmd.add_argument("--videos", type=int, default=5)

    vision_cmd = subparsers.add_parser("vision", help="Run a vision task against a source.")
    vision_cmd.add_argument("source")
    vision_cmd.add_argument(
        "--task",
        default="image_understanding",
        choices=("ocr", "screen_understanding", "window_detection", "image_understanding", "document_understanding"),
    )

    gui_cmd = subparsers.add_parser("gui", help="Launch the NovaControl desktop GUI.")
    gui_cmd.add_argument("--dry-run", action="store_true", help="Print GUI tabs without opening a window.")

    return parser
