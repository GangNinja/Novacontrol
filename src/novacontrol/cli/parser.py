"""CLI argument parser for NovaControl."""

from __future__ import annotations

import argparse

from novacontrol.preference import PreferenceAlgorithm, PreferenceDatasetType
from novacontrol.rlhf import HumanFeedbackType, PolicyAlgorithm, RLMode
from novacontrol.training import DatasetType

COMPLETED_PHASES = (
    "phase1", "phase2", "phase3", "phase4", "phase5", "phase6", "phase7",
    "phase8", "phase9", "phase10", "phase11", "phase12", "phase13", "phase14",
    "phase15", "phase16", "phase17", "phase18", "phase19", "phase20", "phase21",
    "phase22", "phase23", "phase24", "phase25", "phase26", "phase27", "phase28",
    "phase29", "phase30", "phase31", "phase10_sdk", "phase11_rag", "phase12_agents",
    "explore",
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

    # Supervised fine-tuning (Phase 16). One command with an action, the way
    # `settings` carries flags: the actions are the lifecycle in order — build a
    # dataset, estimate, create a run, start it, watch it, evaluate it, approve
    # it, promote it — and nothing here starts training on its own.
    training = subparsers.add_parser(
        "training",
        help="Supervised fine-tuning: datasets, runs, checkpoints and trained models.",
    )
    training.add_argument(
        "action",
        choices=(
            "status", "summary", "datasets", "dataset", "build", "validate",
            "estimate", "create", "runs", "run", "checkpoints", "start", "pause",
            "resume", "cancel", "evaluate", "evaluations", "models", "model",
            "approve", "promote", "reject", "deprecate", "rollback",
        ),
        help="What to do; `training datasets` is a safe first look.",
    )
    training.add_argument("--id", default="", help="Run id, dataset version id or model id.")
    training.add_argument(
        "--type",
        dest="dataset_type",
        default=DatasetType.NLU.value,
        choices=tuple(item.value for item in DatasetType),
        help="Dataset family (one target schema each).",
    )
    training.add_argument("--name", default="", help="Dataset name; defaults to the type.")
    training.add_argument("--model", default="", help="Base model a run or estimate names.")
    training.add_argument("--dataset-version", default="", help="Dataset version a run trains on.")
    training.add_argument("--epochs", type=int, default=None)
    training.add_argument("--limit", type=int, default=50, help="How many rows to list.")
    training.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Config override, repeatable (e.g. --set learning_rate=0.0001).",
    )
    training.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm a REAL (non-dry-run) run; without it a real run is refused.",
    )
    training.add_argument(
        "--override",
        action="store_true",
        help="Override an UNSAFE resource verdict (the deployment must allow it).",
    )
    training.add_argument("--reason", default="")
    training.add_argument("--note", default="")
    training.add_argument("--approved-by", default="", help="Who is approving, for the record.")

    # Preference optimization (Phase 17). Deliberately shaped like `training`:
    # same flags, same order, plus the two things this phase adds — an objective
    # and a review decision. `preference dry-run` is the safe first look, and
    # nothing here starts a real run on its own either.
    preference = subparsers.add_parser(
        "preference",
        help="Preference optimization (DPO/ORPO): pair datasets, runs and reviews.",
    )
    preference.add_argument(
        "action",
        choices=(
            "status", "summary", "algorithms", "datasets", "dataset", "build",
            "validate", "pair", "estimate", "dry-run", "create", "runs", "run",
            "checkpoints", "start", "pause", "resume", "cancel", "evaluate",
            "evaluations", "reviews", "review", "submit", "decide",
            "models", "model",
        ),
        help="What to do; `preference status` is a safe first look.",
    )
    preference.add_argument(
        "--id", default="", help="Run id, dataset version id, model id or pair id."
    )
    preference.add_argument(
        "--type",
        dest="dataset_type",
        default=PreferenceDatasetType.NLU.value,
        choices=tuple(item.value for item in PreferenceDatasetType),
        help="Preference family (one prompt shape and one pair of outputs each).",
    )
    preference.add_argument(
        "--algorithm",
        default="",
        choices=("", *(item.value for item in PreferenceAlgorithm)),
        help="The objective: dpo (reference-anchored) or orpo (reference-free).",
    )
    preference.add_argument("--name", default="", help="Dataset name; defaults to the type.")
    preference.add_argument("--model", default="", help="Base model a run or estimate names.")
    preference.add_argument(
        "--dataset-version", default="", help="Preference dataset version a run trains on."
    )
    preference.add_argument("--epochs", type=int, default=None)
    preference.add_argument("--beta", type=float, default=None, help="Preference strength.")
    preference.add_argument("--limit", type=int, default=50, help="How many rows to list.")
    preference.add_argument(
        "--pairs",
        default="",
        help="JSON file of pairs a person submitted (a list of pairs, or one pair).",
    )
    preference.add_argument(
        "--decision",
        default="",
        choices=("choose_a", "choose_b", "tie", "reject"),
        help="A reviewer's answer about a queued pair.",
    )
    preference.add_argument(
        "--reviewer", default="", help="Who decided, for the record."
    )
    preference.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Config override, repeatable (e.g. --set beta=0.05).",
    )
    preference.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm a REAL (non-dry-run) preference run; without it it is refused.",
    )
    preference.add_argument(
        "--override",
        action="store_true",
        help="Override an UNSAFE resource verdict (the deployment must allow it).",
    )
    preference.add_argument("--reason", default="")
    preference.add_argument("--note", default="")

    # RLHF / RLAIF (Phase 18). Same shape as `preference` beside it: read-only
    # actions answer with what is stored, changing actions go through the
    # application's own methods, and nothing here starts a real run without
    # --confirm. `rlhf dry-run` is the safe first look.
    rlhf = subparsers.add_parser(
        "rlhf",
        help="RLHF / RLAIF: feedback, reward datasets, rollout simulation and runs.",
    )
    rlhf.add_argument(
        "action",
        choices=(
            "status", "summary", "algorithms", "feedback", "submit", "decide",
            "rate", "ratings", "disagreements", "datasets", "dataset", "build",
            "validate", "held", "estimate", "dry-run", "pipeline", "create",
            "runs", "run", "checkpoints", "start", "pause", "resume", "cancel",
            "evaluate", "evaluations", "compare", "models", "model",
        ),
        help="What to do; `rlhf status` is a safe first look.",
    )
    rlhf.add_argument(
        "--id", default="", help="Run id, dataset version id, feedback id or model id."
    )
    rlhf.add_argument(
        "--mode",
        default="",
        choices=("", *(item.value for item in RLMode)),
        help="The feedback loop: rlhf (human) or rlaif (AI).",
    )
    rlhf.add_argument(
        "--algorithm",
        default="",
        choices=("", *(item.value for item in PolicyAlgorithm)),
        help="Policy optimizer: mock_policy now; ppo/grpo are planned and refused.",
    )
    rlhf.add_argument("--name", default="", help="Dataset or run name.")
    rlhf.add_argument("--model", default="", help="Base model a run names.")
    rlhf.add_argument(
        "--dataset-version", default="", help="Reward dataset version a run trains on."
    )
    rlhf.add_argument(
        "--feedback-type",
        default="",
        choices=("", *(item.value for item in HumanFeedbackType)),
        help="What a person is asserting about a trajectory.",
    )
    rlhf.add_argument("--trajectory", default="", help="Trajectory a feedback row names.")
    rlhf.add_argument("--task", default="", help="Task id a feedback row names.")
    rlhf.add_argument("--rating", type=float, default=None, help="A numeric rating.")
    rlhf.add_argument("--confidence", type=float, default=None)
    rlhf.add_argument("--candidate", default="", help="A selected candidate label.")
    rlhf.add_argument("--reason-category", default="")
    rlhf.add_argument("--evaluator", default="", help="Which evaluator rates a subject.")
    rlhf.add_argument(
        "--criteria",
        default="",
        help="Comma-separated criterion names for a rating (default: the evaluator's own).",
    )
    rlhf.add_argument("--limit", type=int, default=50, help="How many rows to list.")
    rlhf.add_argument(
        "--file",
        default="",
        help="JSON file: one feedback row (submit) or one rating subject (rate).",
    )
    rlhf.add_argument(
        "--decision",
        default="",
        choices=("accept", "reject"),
        help="A reviewer's answer about a held feedback row.",
    )
    rlhf.add_argument("--reviewer", default="", help="Who decided, for the record.")
    rlhf.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Config override, repeatable (e.g. --set rollout_count=2).",
    )
    rlhf.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm a REAL (non-dry-run) RL run; without it it is refused.",
    )
    rlhf.add_argument(
        "--override",
        action="store_true",
        help="Override an UNSAFE resource verdict (the deployment must allow it).",
    )
    rlhf.add_argument(
        "--detect",
        action="store_true",
        help="For `disagreements`: detect new ones before listing them.",
    )
    rlhf.add_argument("--reason", default="")
    rlhf.add_argument("--note", default="")

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
