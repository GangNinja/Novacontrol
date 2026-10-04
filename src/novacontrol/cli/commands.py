"""CLI command handlers for NovaControl."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
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


def _training_overrides(pairs: Sequence[str]) -> dict[str, Any]:
    """Turn repeated --set KEY=VALUE into the mapping a config is built from.

    Values stay strings unless they are plainly a number or a boolean, which is
    the same rule `TrainingConfig.from_mapping` applies on the other side: an
    unusable value keeps its default instead of silently becoming 0.
    """
    overrides: dict[str, Any] = {}
    for pair in pairs:
        key, separator, value = str(pair).partition("=")
        key = key.strip()
        if not separator or not key:
            raise ValueError(f"--set needs KEY=VALUE, got {pair!r}")
        text = value.strip()
        lowered = text.lower()
        if lowered in ("true", "false"):
            overrides[key] = lowered == "true"
            continue
        for caster in (int, float):
            try:
                overrides[key] = caster(text)
                break
            except ValueError:
                continue
        else:
            overrides[key] = text
    return overrides


async def run_training(
    action: str,
    *,
    identifier: str = "",
    dataset_type: str = "nlu",
    name: str = "",
    model: str = "",
    dataset_version: str = "",
    epochs: int | None = None,
    limit: int = 50,
    overrides: Sequence[str] = (),
    confirm: bool = False,
    override: bool = False,
    reason: str = "",
    note: str = "",
    approved_by: str = "",
) -> None:
    """Supervised fine-tuning: datasets, runs, checkpoints and trained models.

    Read-only actions (status, datasets, runs, models…) answer with what is
    stored. Actions that change something go through the application's own
    methods, so the CLI cannot start, approve or promote anything the API could
    not — and a refusal reports the manager's reason and exits non-zero instead
    of printing a cheerful payload.
    """
    app = NovaControlApplication(data_dir=Path("data"))
    await app.start()
    try:
        result = await _training_action(
            app,
            action,
            identifier=identifier,
            dataset_type=dataset_type,
            name=name,
            model=model,
            dataset_version=dataset_version,
            epochs=epochs,
            limit=limit,
            overrides=overrides,
            confirm=confirm,
            override=override,
            reason=reason,
            note=note,
            approved_by=approved_by,
        )
    except (KeyError, ValueError, TypeError) as exc:
        print_json({"status": "error", "reason": f"{type(exc).__name__}: {exc}"})
        raise SystemExit(1) from exc
    finally:
        await app.stop()
    refused = not result.get("ok", True)
    print_json({"status": "refused" if refused else "ok", "training": result})
    if refused:
        raise SystemExit(1)


async def _training_action(app: NovaControlApplication, action: str, **args: Any) -> dict[str, Any]:
    """Dispatch one training action to the application method that owns it."""
    identifier = str(args["identifier"])
    limit = int(args["limit"])
    if action == "status":
        return app.training_status()
    if action == "summary":
        return app.training_summary()
    if action == "datasets":
        return app.training_datasets(dataset_type=str(args["dataset_type"]), limit=limit)
    if action == "dataset":
        return app.training_dataset(identifier)
    if action == "build":
        rules: dict[str, Any] = {}
        if args["overrides"]:
            rules = _training_overrides(args["overrides"])
        return app.create_training_dataset(
            str(args["name"]) or str(args["dataset_type"]),
            str(args["dataset_type"]),
            rules=rules or None,
        )
    if action == "validate":
        return app.validate_training_dataset(identifier)
    if action == "estimate":
        config: dict[str, Any] = _training_overrides(args["overrides"])
        if args["model"]:
            config["base_model"] = str(args["model"])
        if args["dataset_version"]:
            config["dataset_version"] = str(args["dataset_version"])
        if args["epochs"] is not None:
            config["epochs"] = int(args["epochs"])
        return app.estimate_training(config)
    if action == "create":
        return app.create_training_run(
            str(args["model"]),
            str(args["dataset_version"]),
            config=_training_overrides(args["overrides"]) or None,
            name=str(args["name"]),
        )
    if action == "runs":
        return app.training_runs(limit=limit)
    if action == "run":
        return app.training_run(identifier)
    if action == "checkpoints":
        return app.training_checkpoints(identifier)
    if action == "evaluate":
        return await app.evaluate_training_run(identifier)
    if action == "evaluations":
        return app.training_evaluations(limit=limit)
    if action == "models":
        return app.training_models(limit=limit)
    if action == "model":
        return app.training_model(identifier)
    if action == "start":
        return await app.start_training_run(
            identifier, confirm=bool(args["confirm"]), override=bool(args["override"])
        )
    if action == "pause":
        return app.pause_training_run(identifier)
    if action == "resume":
        return await app.resume_training_run(identifier)
    if action == "cancel":
        return app.cancel_training_run(identifier)
    if action == "approve":
        return app.approve_training_model(
            identifier, approved_by=str(args["approved_by"]), note=str(args["note"])
        )
    if action == "promote":
        return app.promote_training_model(identifier, note=str(args["note"]))
    if action == "reject":
        return app.reject_training_model(identifier, reason=str(args["reason"]))
    if action == "deprecate":
        return app.deprecate_training_model(identifier, reason=str(args["reason"]))
    if action == "rollback":
        return app.rollback_training_model(identifier, reason=str(args["reason"]))
    raise ValueError(f"unsupported training action {action!r}")


async def run_preference(
    action: str,
    *,
    identifier: str = "",
    dataset_type: str = "nlu",
    algorithm: str = "",
    name: str = "",
    model: str = "",
    dataset_version: str = "",
    epochs: int | None = None,
    beta: float | None = None,
    limit: int = 50,
    pairs: str = "",
    decision: str = "",
    reviewer: str = "",
    overrides: Sequence[str] = (),
    confirm: bool = False,
    override: bool = False,
    reason: str = "",
    note: str = "",
) -> None:
    """Preference optimization (DPO/ORPO): pair datasets, runs and reviews.

    Same contract as the `training` command beside it: read-only actions answer
    with what is stored, changing actions go through the application's own
    methods (so the CLI can do nothing the API could not), a refusal exits
    non-zero, and nothing here starts a real run without --confirm.
    """
    app = NovaControlApplication(data_dir=Path("data"))
    await app.start()
    try:
        result = await _preference_action(
            app,
            action,
            identifier=identifier,
            dataset_type=dataset_type,
            algorithm=algorithm,
            name=name,
            model=model,
            dataset_version=dataset_version,
            epochs=epochs,
            beta=beta,
            limit=limit,
            pairs=pairs,
            decision=decision,
            reviewer=reviewer,
            overrides=overrides,
            confirm=confirm,
            override=override,
            reason=reason,
            note=note,
        )
    except (KeyError, ValueError, TypeError, OSError) as exc:
        print_json({"status": "error", "reason": f"{type(exc).__name__}: {exc}"})
        raise SystemExit(1) from exc
    finally:
        await app.stop()
    refused = not result.get("ok", True)
    print_json({"status": "refused" if refused else "ok", "preference": result})
    if refused:
        raise SystemExit(1)


async def _preference_action(
    app: NovaControlApplication, action: str, **args: Any
) -> dict[str, Any]:
    """Dispatch one preference action to the application method that owns it."""
    identifier = str(args["identifier"])
    limit = int(args["limit"])
    algorithm = str(args["algorithm"])
    if action == "status":
        return app.preference_status()
    if action == "summary":
        return app.preference_summary()
    if action == "algorithms":
        return app.preference_algorithms()
    if action == "datasets":
        return app.preference_datasets(dataset_type=str(args["dataset_type"]), limit=limit)
    if action == "dataset":
        return app.preference_dataset(identifier)
    if action == "pair":
        dataset_version, _, preference_id = identifier.partition(":")
        if not preference_id:
            raise ValueError("--id needs DATASET_VERSION:PREFERENCE_ID for a pair")
        return app.preference_pair(dataset_version, preference_id)
    if action == "build":
        rules = _training_overrides(args["overrides"]) if args["overrides"] else {}
        return app.create_preference_dataset(
            str(args["name"]) or str(args["dataset_type"]),
            str(args["dataset_type"]),
            rules=rules or None,
        )
    if action == "validate":
        return app.validate_preference_dataset(identifier)
    if action == "estimate":
        return app.estimate_preference(_preference_config_args(args))
    if action == "dry-run":
        return app.dry_run_preference(
            str(args["model"]),
            str(args["dataset_version"]),
            config=_preference_config_args(args) or None,
            algorithm=algorithm,
        )
    if action == "create":
        return app.create_preference_run(
            str(args["model"]),
            str(args["dataset_version"]),
            config=_preference_config_args(args) or None,
            name=str(args["name"]),
        )
    if action == "runs":
        return app.preference_runs(algorithm=algorithm, limit=limit)
    if action == "run":
        return app.preference_run(identifier)
    if action == "checkpoints":
        return app.preference_checkpoints(identifier)
    if action == "evaluate":
        return await app.evaluate_preference_run(identifier)
    if action == "evaluations":
        return app.preference_evaluations(limit=limit)
    if action == "reviews":
        return app.preference_reviews(limit=limit)
    if action == "review":
        return app.preference_review(identifier)
    if action == "submit":
        return app.submit_preference_pair(**_submitted_pair(args))
    if action == "decide":
        if not str(args["decision"]):
            raise ValueError("decide needs --decision choose_a|choose_b|tie|reject")
        return app.decide_preference_review(
            identifier,
            str(args["decision"]),
            reviewer=str(args["reviewer"]),
            reason=str(args["reason"]),
        )
    if action == "models":
        return app.preference_models(algorithm=algorithm, limit=limit)
    if action == "model":
        return app.preference_model(identifier)
    if action == "start":
        return await app.start_preference_run(
            identifier, confirm=bool(args["confirm"]), override=bool(args["override"])
        )
    if action == "pause":
        return app.pause_preference_run(identifier)
    if action == "resume":
        return await app.resume_preference_run(identifier)
    if action == "cancel":
        return app.cancel_preference_run(identifier)
    raise ValueError(f"unsupported preference action {action!r}")


def _preference_config_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """The configuration flags shared by `estimate`, `dry-run` and `create`."""
    config: dict[str, Any] = _training_overrides(args["overrides"])
    if args["algorithm"]:
        config["algorithm"] = str(args["algorithm"])
    if args["model"]:
        config["base_model"] = str(args["model"])
    if args["dataset_version"]:
        config["preference_dataset_version"] = str(args["dataset_version"])
    if args["epochs"] is not None:
        config["epochs"] = int(args["epochs"])
    if args["beta"] is not None:
        config["beta"] = float(args["beta"])
    return config


def _submitted_pair(args: Mapping[str, Any]) -> dict[str, Any]:
    """Read a submitted pair from a JSON file: one pair, or a list of them.

    A file rather than a dozen flags because a pair is two structured outputs —
    an intent, a decision, a tool call with its arguments — and inventing flag
    syntax for every one of them would be a worse interface than the structure it
    is describing.
    """
    raw = _read_json_file(str(args["pairs"]))
    payload = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(payload, Mapping):
        raise ValueError("--pairs needs a JSON object or a list of JSON objects")
    pair = dict(payload)
    dataset_type = str(args["dataset_type"])
    return {
        "dataset_type": str(pair.get("dataset_type", dataset_type)),
        "prompt": _required_mapping(pair, "prompt"),
        "chosen": _required_mapping(pair, "chosen"),
        "rejected": _required_mapping(pair, "rejected"),
        "context": _optional_mapping(pair, "context"),
        "chosen_outcome": _optional_mapping(pair, "chosen_outcome"),
        "rejected_outcome": _optional_mapping(pair, "rejected_outcome"),
        "reviewer": str(args["reviewer"] or pair.get("reviewer", "")),
        "reason": str(args["reason"] or pair.get("reason", "")),
        "confidence": float(pair.get("confidence", 1.0) or 1.0),
        "group_key": str(pair.get("group_key", "")),
        "tags": tuple(str(tag) for tag in pair.get("tags", ()) or ()),
        "enqueue": bool(pair.get("enqueue", False)),
    }


def _read_json_file(path: str) -> Any:
    """Read a JSON document from disk (a pair, or a list of pairs)."""
    import json

    if not path:
        raise ValueError("--pairs needs a path to a JSON file")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _required_mapping(pair: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = pair.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"a submitted pair needs a {key} object")
    return dict(value)


def _optional_mapping(pair: Mapping[str, Any], key: str) -> dict[str, Any] | None:
    value = pair.get(key)
    return dict(value) if isinstance(value, Mapping) else None


async def run_rlhf(
    action: str,
    *,
    identifier: str = "",
    mode: str = "",
    algorithm: str = "",
    name: str = "",
    model: str = "",
    dataset_version: str = "",
    feedback_type: str = "",
    trajectory: str = "",
    task: str = "",
    rating: float | None = None,
    confidence: float | None = None,
    candidate: str = "",
    reason_category: str = "",
    evaluator: str = "",
    criteria: str = "",
    limit: int = 50,
    file: str = "",
    decision: str = "",
    reviewer: str = "",
    overrides: Sequence[str] = (),
    confirm: bool = False,
    override: bool = False,
    detect: bool = False,
    reason: str = "",
    note: str = "",
) -> None:
    """RLHF / RLAIF: feedback, reward datasets, rollout simulation and runs.

    Same contract as the `preference` and `training` commands beside it:
    read-only actions answer with what is stored, changing actions go through
    the application's own methods (so the CLI can do nothing the API could
    not), a refusal exits non-zero, and nothing here starts a real run without
    --confirm. `rlhf dry-run` is the safe first look.
    """
    app = NovaControlApplication(data_dir=Path("data"))
    await app.start()
    try:
        result = await _rlhf_action(
            app,
            action,
            identifier=identifier,
            mode=mode,
            algorithm=algorithm,
            name=name,
            model=model,
            dataset_version=dataset_version,
            feedback_type=feedback_type,
            trajectory=trajectory,
            task=task,
            rating=rating,
            confidence=confidence,
            candidate=candidate,
            reason_category=reason_category,
            evaluator=evaluator,
            criteria=criteria,
            limit=limit,
            file=file,
            decision=decision,
            reviewer=reviewer,
            overrides=overrides,
            confirm=confirm,
            override=override,
            detect=detect,
            reason=reason,
            note=note,
        )
    except (KeyError, ValueError, TypeError, OSError) as exc:
        print_json({"status": "error", "reason": f"{type(exc).__name__}: {exc}"})
        raise SystemExit(1) from exc
    finally:
        await app.stop()
    refused = not result.get("ok", True)
    print_json({"status": "refused" if refused else "ok", "rlhf": result})
    if refused:
        raise SystemExit(1)


async def _rlhf_action(
    app: NovaControlApplication, action: str, **args: Any
) -> dict[str, Any]:
    """Dispatch one RLHF/RLAIF action to the application method that owns it.

    The CLI can do nothing the API could not: every action lands on the same
    application method the route calls, so a refusal reads the same either way.
    """
    identifier = str(args["identifier"])
    limit = int(args["limit"])
    mode = str(args["mode"])
    algorithm = str(args["algorithm"])
    trajectory = str(args["trajectory"])
    if action == "status":
        return app.rlhf_status()
    if action == "summary":
        return app.rlhf_summary()
    if action == "algorithms":
        return app.rlhf_algorithms()
    if action == "feedback":
        return app.rlhf_feedback(
            feedback_type=str(args["feedback_type"]),
            trajectory_id=trajectory,
            limit=limit,
        )
    if action == "submit":
        return app.submit_rlhf_feedback(_rlhf_feedback_payload(args))
    if action == "decide":
        if not str(args["decision"]):
            raise ValueError("decide needs --decision accept|reject")
        return app.decide_rlhf_feedback(
            identifier,
            str(args["decision"]),
            reviewer=str(args["reviewer"]),
            reason=str(args["reason"]),
        )
    if action == "rate":
        return app.rate_rlhf_subject(
            _rlhf_rating_subject(args),
            evaluator=str(args["evaluator"]) or "auto",
            criteria=tuple(
                part.strip()
                for part in str(args["criteria"]).split(",")
                if part.strip()
            ),
        )
    if action == "ratings":
        return app.rlhf_ratings(trajectory_id=trajectory, limit=limit)
    if action == "disagreements":
        return app.rlhf_disagreements(detect=bool(args["detect"]), limit=limit)
    if action == "datasets":
        return app.rlhf_datasets(mode=mode, limit=limit)
    if action == "dataset":
        return app.rlhf_dataset(identifier)
    if action == "build":
        if not mode:
            raise ValueError("build needs --mode rlhf|rlaif")
        return app.create_rlhf_dataset(
            str(args["name"]) or mode,
            mode=mode,
            rules=_training_overrides(args["overrides"]) or None,
            description=str(args["note"]),
        )
    if action == "validate":
        return app.validate_rlhf_dataset(identifier)
    if action == "held":
        return app.rlhf_held(identifier)
    if action == "estimate":
        return app.estimate_rlhf(_rlhf_config_args(args))
    if action == "dry-run":
        return app.dry_run_rlhf(
            str(args["model"]),
            str(args["dataset_version"]),
            config=_rlhf_config_args(args) or None,
            mode=mode,
            algorithm=algorithm,
        )
    if action == "pipeline":
        return app.rlhf_pipeline(
            config=_rlhf_config_args(args) or None,
            dataset_version=str(args["dataset_version"]),
        )
    if action == "create":
        return app.create_rlhf_run(
            str(args["model"]),
            str(args["dataset_version"]),
            config=_rlhf_config_args(args) or None,
            name=str(args["name"]),
        )
    if action == "runs":
        return app.rlhf_runs(mode=mode, limit=limit)
    if action == "run":
        return app.rlhf_run(identifier)
    if action == "checkpoints":
        return app.rlhf_checkpoints(identifier)
    if action == "start":
        return await app.start_rlhf_run(
            identifier, confirm=bool(args["confirm"]), override=bool(args["override"])
        )
    if action == "pause":
        return app.pause_rlhf_run(identifier)
    if action == "resume":
        return await app.resume_rlhf_run(identifier)
    if action == "cancel":
        return app.cancel_rlhf_run(identifier)
    if action == "evaluate":
        return await app.evaluate_rlhf_run(identifier)
    if action == "evaluations":
        return app.rlhf_evaluations(limit=limit)
    if action == "compare":
        return app.compare_rlhf_models(**_rlhf_compare_args(args))
    if action == "models":
        return app.rlhf_models(mode=mode, limit=limit)
    if action == "model":
        return app.rlhf_model(identifier)
    raise ValueError(f"unsupported rlhf action {action!r}")


def _rlhf_config_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """The configuration flags shared by `estimate`, `dry-run` and `create`."""
    config: dict[str, Any] = _training_overrides(args["overrides"])
    if args["mode"]:
        config["mode"] = str(args["mode"])
    if args["algorithm"]:
        config["algorithm"] = str(args["algorithm"])
    if args["model"]:
        config["base_model"] = str(args["model"])
    if args["dataset_version"]:
        config["reward_dataset_version"] = str(args["dataset_version"])
    return config


def _rlhf_feedback_payload(args: Mapping[str, Any]) -> dict[str, Any]:
    """One feedback row: flags, or a JSON file when the row is easier to write out."""
    path = str(args["file"])
    if path:
        payload = _read_json_file(path)
        if not isinstance(payload, Mapping):
            raise ValueError("--file needs a JSON object for a feedback row")
        return dict(payload)
    feedback_type = str(args["feedback_type"])
    if not feedback_type:
        raise ValueError("submit needs --feedback-type or --file")
    payload = {
        "feedback_type": feedback_type,
        "trajectory_id": str(args["trajectory"]),
        "task_id": str(args["task"]),
        "selected_candidate": str(args["candidate"]),
        "reason_category": str(args["reason_category"]),
        "reason": str(args["reason"]),
    }
    if args["rating"] is not None:
        payload["rating"] = float(args["rating"])
    if args["confidence"] is not None:
        payload["confidence"] = float(args["confidence"])
    return payload


def _rlhf_rating_subject(args: Mapping[str, Any]) -> dict[str, Any]:
    """The observable facts an evaluator rates, from a JSON file.

    A file rather than flags because a subject is structured — a task, a
    candidate output or action, the constraints it had to respect and the
    outcome that was observed — and inventing flag syntax for each field would
    be a worse interface than the structure it is describing.
    """
    path = str(args["file"])
    if not path:
        raise ValueError("rate needs --file pointing at the subject JSON")
    subject = _read_json_file(path)
    if not isinstance(subject, Mapping):
        raise ValueError("--file needs a JSON object for a rating subject")
    return dict(subject)


def _rlhf_compare_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """A comparison from a JSON file: dataset_version, base and candidate."""
    path = str(args["file"])
    if not path:
        raise ValueError("compare needs --file with the models to compare")
    raw = _read_json_file(path)
    if not isinstance(raw, Mapping):
        raise ValueError("--file needs a JSON object for a comparison")
    dataset_version = str(raw.get("dataset_version", args["dataset_version"]))
    base = raw.get("base")
    candidate = raw.get("candidate")
    if not dataset_version or base is None or candidate is None:
        raise ValueError("the comparison needs dataset_version, base and candidate")
    return {
        "dataset_version": dataset_version,
        "base": base,
        "candidate": candidate,
        "sft": raw.get("sft"),
        "preference": raw.get("preference"),
        "split": str(raw.get("split", "test")),
        "run_id": str(raw.get("run_id", "")),
    }


async def run_ask(request: str) -> None:
    """Ask NovaControl through the integrated app router."""
    app = NovaControlApplication()
    await app.start()
    try:
        response = await app.handle_request(request)
        print_json({"status": "ok", "app": app.status(), "response": response.to_dict()})
    finally:
        await app.stop()
