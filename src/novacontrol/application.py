"""NovaControl application composition root."""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import re
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Coroutine, Mapping, Sequence
from contextvars import ContextVar
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
from novacontrol.core.config import NovaControlConfig
from novacontrol.core.events import Event, EventBus, EventType, task_event_name
from novacontrol.core.runtime import EventDrivenRuntime
from novacontrol.core.security import ApprovalDecision, ApprovalRequest, DenyByDefaultApprovalGateway
from novacontrol.desktop import DesktopAutomationController, DesktopAutomationModule, LocalDesktopRunner
from novacontrol.desktop.vision import VisionController
from novacontrol.explore import ExploreModule, ExploreRequest, ExploreService, set_explore_cache_provider
from novacontrol.decision import (
    Decision,
    DecisionEngine,
    DecisionEnvironment,
    DecisionRoute,
    DecisionType,
    INTENT_HANDLERS,
    SEQUENCING_HANDLERS,
    build_decision_provider,
)
from novacontrol.intelligence import GlobalInputIntelligence, UnderstandResult
from novacontrol.intelligence.model_manager import OllamaBackend
from novacontrol.intelligence.intent import (
    CapabilityAvailability,
    CapabilityRegistry,
    IntentName,
    RiskLevel,
    StructuredIntent,
    resolve_intent,
)
from novacontrol.intelligence.telemetry import request_id_for
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
from novacontrol.integrations.llm import _redact_key, provider_supports_vision
from novacontrol.knowledge import KnowledgeBase
from novacontrol.memory import MemoryManager, MemoryModule, MemoryNamespace, SqliteMemoryStore
from novacontrol.models import (
    KeepAliveSettings,
    ModelCapability,
    ModelLoadOutcome,
    ModelManager,
    ModelProfile,
    ModelRegistry,
)
from novacontrol.persistence import JsonStateStore
from novacontrol.phone import PhoneControlController, PhoneControlModule
from novacontrol.planning import (
    AgentLoop,
    Plan,
    PlanCompiler,
    PlanStep,
    PlanningEngine,
    PlanningModule,
    RetryPolicy,
    StepContext,
    VerificationSpec,
    WorkflowExecutor,
    WorkflowResult,
)
from novacontrol.planning.engine import steps_id
from novacontrol.reliability import (
    PermissionManager,
    TaskController,
    TaskSnapshot,
    TaskState,
    VerificationEngine,
)
# Aliased on purpose. Two recovery engines exist and they work at different
# levels: ``agentcore.RecoveryEngine`` (imported below) diagnoses a failed
# AGENT ACTION against a fresh UI state, while this one recovers a PLAN STEP
# under the plan's retry policy and its verification. Importing it under its
# own name would shadow the agentcore one for the orchestrator wiring.
from novacontrol.reliability import RecoveryEngine as StepRecoveryEngine
from novacontrol.plugins import PluginMarketplaceModule
from novacontrol.projects import ProjectManager
from novacontrol.scheduler import InMemoryScheduler
from novacontrol.self_improvement import CodeChange, SelfImprovementEngine
from novacontrol.settings import BRAIN_MODES, SettingsManager
from novacontrol.skills import SkillRegistry
from novacontrol.tasks import TaskCenter, TaskRecordStatus
from novacontrol.telemetry.hardware import HardwareTelemetry
from novacontrol.tools import (
    FunctionTool,
    OutputNormalizer,
    ToolExecutor,
    ToolModule,
    ToolParameter,
    ToolRegistry,
    ToolRequest,
    ToolResultCache,
    ToolRetriever,
    ToolSchema,
    ToolSelector,
    ToolStatus,
    build_tool_catalog,
    tool_descriptions,
)
from novacontrol.vision import (
    NullVisionProvider,
    VisionManager,
    VisionModule,
    VisionProvider,
    VisionRequest,
    VisionResult,
    VisionTaskKind,
    task_for_question,
)
from novacontrol.vision.providers import build_vision_provider as build_vision_pipeline_provider
from novacontrol.voice import VoiceModule

_APPROVAL_TTL_SECONDS = 300.0  # A planned desktop action must be approved within 5 minutes.

# "Look at the screen" with no question in it. These phrases are the
# INSTRUCTION to capture, not a question about the capture, so passing them to
# the vision model would ask it to answer the words the user already used to
# ask for the picture. Anything more specific is a real question and travels.
_VISION_BARE_LOOK = re.compile(
    r"^\s*(?:please\s+)?(?:can you\s+|could you\s+|would you\s+)?"
    r"(?:look at|check|see|analy[sz]e|describe|read|what(?:'s| is) on|what do you see on)"
    r"(?: the)?(?: my)?(?: current)?(?: screen| screenshot| image| picture| window| desktop)"
    r"[\s.!?]*$"
)

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

# Which intent's sentence describes each metric. A request that named several
# metrics ("check my RAM and CPU") reads them all from one probe and answers
# each in the sentence that intent already owns, rather than inventing a
# combined format for a case the single-metric paths must keep unchanged.
_METRIC_KINDS: dict[str, str] = {
    "memory": "memory_status",
    "cpu": "cpu_status",
    "gpu": "gpu_status",
    "battery": "battery_status",
    "network": "network_status",
}

#: Reader attribute for each metric intent — the inverse of the map above, so a
#: plan step that names an intent ("memory_status") can read the right probe
#: ("memory") without a second lookup table to keep in step.
_READER_FOR_INTENT: dict[str, str] = {kind: name for name, kind in _METRIC_KINDS.items()}


# ──────────────────────────────────────────────────────────────────────────
# Phase 4: what a plan step may ask to have checked
# ──────────────────────────────────────────────────────────────────────────


def _metric_read_check(spec: VerificationSpec, output: Mapping[str, Any]) -> tuple[bool, str]:
    """Did the reading actually report a reading?

    A metric that is unavailable IS a successful reading when the failure is
    stated: a GPU-less machine answering "there is no GPU" has answered the
    question. What fails this check is a reading that reports neither a value
    nor a reason — silence dressed up as a measurement.
    """
    readings = output.get("metrics")
    if not isinstance(readings, dict) or not readings:
        return False, "the step reported no readings at all"
    silent = [
        str(name)
        for name, value in readings.items()
        if not isinstance(value, dict) or "available" not in value
    ]
    if silent:
        return False, f"these readings report neither a value nor a reason: {', '.join(silent)}"
    return True, "every reading reports either a value or why it could not be measured"


#: The named checks this installation can perform for a plan. A plan may ask
#: for one by name; a name that is not here is reported INCONCLUSIVE, never
#: assumed to have passed.
_PLAN_VERIFICATIONS: dict[
    str, Callable[[VerificationSpec, Mapping[str, Any]], tuple[bool, str]]
] = {
    "metric_read": _metric_read_check,
}

#: Lines in captured output that name a failure worth reporting back.
_FAILURE_LINE = re.compile(
    r"\b(fail(?:ed|ure|ures|ing)?|error|assert\w*|traceback|exception)\b", re.I
)

#: Which request is currently being served, as a context variable. Every event
#: the work underneath a request causes — a tool call, a check, a recovery —
#: carries the REQUEST's correlation id because of it, so one thread of work can
#: be followed end to end instead of arriving as unrelated announcements.
#:
#: Deliberately never reset inside the request: ``asyncio`` copies the context
#: when a task is created, so work STARTED by a request keeps its id (which is
#: correct — that work is the request's), while work started outside any request
#: sees the empty default.
_CURRENT_REQUEST: ContextVar[str] = ContextVar("novacontrol_current_request", default="")

#: The model roles a capability may require, mapped to the model capability that
#: answers them. A role is spelled the way a capability declares it
#: (``required_models=("chat",)``), and the mapping lives here so the two
#: spellings cannot drift into two vocabularies.
_MODEL_ROLE_CAPABILITIES: dict[str, ModelCapability] = {
    "chat": ModelCapability.TEXT,
    "text": ModelCapability.TEXT,
    "reasoning": ModelCapability.REASONING,
    "coding": ModelCapability.CODING,
    "vision": ModelCapability.VISION,
    "tools": ModelCapability.TOOL_CALLING,
    "tool_calling": ModelCapability.TOOL_CALLING,
    "structured_output": ModelCapability.STRUCTURED_OUTPUT,
    "embeddings": ModelCapability.EMBEDDINGS,
}


# ──────────────────────────────────────────────────────────────────────────
# Phase 4: the describing steps a plan implies
# ──────────────────────────────────────────────────────────────────────────


def _collect_step_output(step: PlanStep, context: StepContext) -> dict[str, Any]:
    """Capture what the step this one depends on produced.

    Collecting is a step of its own because the output must be captured ONCE
    and named: a plan that re-ran a command to look at its output again would be
    a plan that ran the command twice.
    """
    upstream = context.output_of(step.depends_on)
    text = str(upstream.get("stdout") or upstream.get("output") or upstream.get("summary") or "")
    exit_code = upstream.get("exit_code")
    detail = f"Captured {len(text)} characters of output"
    if exit_code is not None:
        detail = f"{detail} (exit code {exit_code})"
    return {
        "summary": f"{detail}.",
        "captured": text,
        "exit_code": exit_code,
        "from": list(step.depends_on),
        # The state that was observed is the output itself; "changed" is what
        # the state verification reads, so it says whether anything arrived.
        "changed": bool(text) or exit_code is not None,
    }


def _analyze_step_output(step: PlanStep, context: StepContext) -> dict[str, Any]:
    """Read the captured output and name what failed — deterministically.

    No model is involved, and none is needed: a test log says which tests
    failed, and matching the lines that say so is both faster and checkable.
    When nothing matches, that is reported as what it is rather than as a clean
    bill of health.
    """
    upstream = context.output_of(step.depends_on)
    text = str(upstream.get("captured") or "")
    exit_code = upstream.get("exit_code")
    failures = [line.strip() for line in text.splitlines() if _FAILURE_LINE.search(line)][:20]
    if not text:
        summary = "There was no captured output to analyse."
    elif failures:
        sample = "; ".join(failures[:5])
        summary = f"{len(failures)} line(s) look like failures: {sample}"
    elif exit_code == 0:
        summary = "The command exited 0 and nothing in its output looks like a failure."
    else:
        summary = (
            f"The command exited {exit_code}, but no failing line was recognised "
            "in its output."
        )
    return {"summary": summary, "failures": failures, "exit_code": exit_code}


def _summarize_step_output(step: PlanStep, context: StepContext) -> dict[str, Any]:
    """The last step: say what happened, from what the earlier steps reported."""
    upstream = context.output_of(step.depends_on)
    summary = str(upstream.get("summary") or "")
    if not summary:
        summary = "The plan finished without producing anything to report."
    return {
        "summary": summary,
        "answer": summary,
        "steps_reported": len(context.outputs),
    }


#: Depth and breadth caps for the bounded project search. A "find my project"
#: step must not walk an entire home directory: it looks in the places projects
#: actually live, to a shallow depth, and reports honestly when it finds nothing.
_PROJECT_SEARCH_DEPTH = 3
_PROJECT_SEARCH_VISITS = 4000
_PROJECT_SEARCH_SKIP = frozenset(
    {"node_modules", ".git", ".venv", "venv", "__pycache__", ".kilo", "site-packages"}
)


def _command_with_resolved_references(understanding: object, text: str) -> str:
    """``text`` with a context-resolved reference restated as its real target.

    "open it" is a sentence about a pronoun. The reading resolves the pronoun
    (application=chrome) and records WHICH words were references; the desktop
    and browser parsers cannot resolve anything, so planning the raw words
    launches a program called "it". Only a request that IS the reference is
    restated: a chain keeps its own words, because one template cannot carry
    its other clauses.
    """
    if not isinstance(understanding, dict) or not understanding.get("references"):
        return text
    template = _REFERENCE_COMMANDS.get(str(understanding.get("intent") or ""))
    if template is None:
        return text
    phrase, key = template
    entities = understanding.get("entities")
    target = str(entities.get(key) or "").strip() if isinstance(entities, dict) else ""
    if not target:
        return text
    normalized = str(understanding.get("normalized_input") or text).strip().lower()
    if " and " in normalized:
        return text
    return phrase.format(**{key: target})


#: Intents whose target the reading may have RESOLVED from context ("open it"),
#: restated as a command the native parsers accept. The resolution happens in
#: the understanding layer, so these templates are the only thing that carries
#: it into planning.
_REFERENCE_COMMANDS: dict[str, tuple[str, str]] = {
    IntentName.OPEN_APPLICATION.value: ("open {application}", "application"),
    IntentName.CLOSE_APPLICATION.value: ("close {application}", "application"),
    IntentName.OPEN_FOLDER.value: ("open folder {folder}", "folder"),
    IntentName.NAVIGATE.value: ("navigate to {url}", "url"),
    IntentName.SEARCH_WEB.value: ("search the web for {query}", "query"),
}


#: How long a plan's own command may run before it is stopped. A real project's
#: suite is the slow case, so it gets the longer bound; both are hard
#: wall-clock limits, so a hung command cannot hold the run open forever.
_TEST_TIMEOUT_SECONDS = 600.0
_COMMAND_TIMEOUT_SECONDS = 120.0
#: How much of a command's output travels with the result. The analysis step
#: reads the END of a log (a test summary is there), so the tail is kept.
_OUTPUT_TAIL_CHARS = 8000


def _argv_for_command(command: str) -> list[str]:
    """The literal argv a shell command names, split WITHOUT a shell.

    The no-shell invariant holds here too: a command a plan asked for becomes
    tokens executed exec-form, so a metacharacter cannot chain a second command
    past the one that was approved. Windows quoting is not POSIX quoting, so
    the split follows the platform it runs on.
    """
    return shlex.split(command, posix=os.name != "nt")


def _test_argv(project: str, scope: str) -> tuple[list[str], str] | None:
    """The argv that runs ``project``'s tests, or None when none is recognisable.

    Recognising the runner — rather than guessing one — is the difference
    between running the project's own suite and running whatever happens to be
    installed: a Node project gets ``npm test``, a Python project gets pytest
    from the interpreter that owns this process, and anything else is reported
    as unrecognised instead of guessed at.
    """
    root = Path(project)
    npm = shutil.which("npm")
    if (root / "package.json").is_file() and npm:
        return [npm, "test"], "npm test"
    scope_path = root / scope
    looks_python = (
        (root / "pytest.ini").is_file()
        or (root / "pyproject.toml").is_file()
        or (root / "setup.cfg").is_file()
        or (root / "tests").is_dir()
        or scope_path.exists()
    )
    if not looks_python:
        return None
    argv = [sys.executable, "-m", "pytest", "-q"]
    if scope_path.exists():
        argv.append(scope)
    return argv, "python -m pytest"


def _output_tail(text: str) -> str:
    """The last part of a command's output — where a test summary lives."""
    if len(text) <= _OUTPUT_TAIL_CHARS:
        return text
    return "...\n" + text[-_OUTPUT_TAIL_CHARS:]


async def _run_approved_process(
    argv: Sequence[str], *, cwd: str, timeout: float
) -> tuple[int, str, str]:
    """Run one already-approved command exec-form and capture its output."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"{argv[0]!r} is not installed, so the command could not run."
        ) from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(
            f"The command did not finish within {timeout:g} seconds and was stopped."
        ) from None
    return (
        int(process.returncode or 0),
        _output_tail((stdout or b"").decode("utf-8", errors="replace")),
        _output_tail((stderr or b"").decode("utf-8", errors="replace")),
    )


def _find_project_directory(name: str) -> str | None:
    """Find a directory whose name matches ``name``, in the usual places."""
    needle = name.strip().lower()
    if not needle:
        return None
    home = Path.home()
    roots = [
        Path.cwd(),
        home / "Desktop",
        home / "OneDrive" / "Desktop",
        home / "Documents",
        home / "projects",
        home / "source",
        home / "repos",
    ]
    visited = 0
    for root in roots:
        if not root.is_dir():
            continue
        if needle in root.name.lower():
            return str(root)
        queue: list[tuple[Path, int]] = [(root, 0)]
        while queue:
            current, depth = queue.pop(0)
            if depth >= _PROJECT_SEARCH_DEPTH:
                continue
            try:
                entries = sorted(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                visited += 1
                if visited > _PROJECT_SEARCH_VISITS:
                    return None
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                if entry.name in _PROJECT_SEARCH_SKIP:
                    continue
                if needle in entry.name.lower():
                    return str(entry)
                queue.append((entry, depth + 1))
    return None


def _escalation_text(goal: str, workflow: WorkflowResult) -> str:
    """What is said when a run needs reasoning and no model is configured."""
    head = f'I could not finish "{goal}" on this machine.'
    body = "\n".join(
        f"- {step_id}: {message}" for step_id, message in workflow.errors.items()
    )
    return (
        f"{head}\n{body or f'- {workflow.summary}'}\n"
        "This step needs reasoning I cannot do locally, so it is reported rather than guessed at."
    )


def _escalation_prompt(
    goal: str,
    workflow: WorkflowResult,
    tools: Sequence[Mapping[str, Any]] = (),
) -> str:
    """The question handed to the model when a plan fails for want of reasoning.

    ``tools`` is the DISCOVERED shortlist for this goal, never the whole tool
    list: a model asked to choose between every tool a system owns picks by name
    recognition, and the names say least about what a request meant. An empty
    shortlist is stated as such rather than left to be inferred from silence —
    "no tool fits this" is information the model needs.
    """
    details = "\n".join(
        f"- {step_id}: {message}" for step_id, message in workflow.errors.items()
    )
    if tools:
        lines = []
        for entry in tools:
            availability = "available" if entry.get("available") else "not installed"
            arguments = ", ".join(entry.get("parameters", {})) or "no arguments"
            lines.append(
                f"- {entry.get('name')}: {entry.get('description')} "
                f"({availability}; takes: {arguments})"
            )
        tool_section = "Tools that could carry this out:\n" + "\n".join(lines) + "\n"
    else:
        tool_section = (
            "No tool in this installation fits this goal, so say so if that is the cause.\n"
        )
    return (
        "A plan I was executing failed, and the failure needs reasoning rather than another try.\n"
        f"Goal: {goal}\n"
        f"What happened:\n{details or workflow.summary}\n"
        f"{tool_section}"
        "Explain the most likely cause and the smallest change that would let this goal succeed. "
        "Be specific and brief."
    )


def _status_message(kind: str, metric_names: tuple[str, ...], readings: dict[str, Any]) -> str:
    """One sentence per metric the request named — deterministically.

    A single metric keeps the exact sentence it always had; several are joined
    in the order the user said them, because "you're using 10.9 GB of RAM. CPU
    usage is 31% right now." answers both halves of the question that the
    one-metric path used to answer halfway.
    """
    kinds = [_METRIC_KINDS[name] for name in metric_names if name in _METRIC_KINDS]
    if len(kinds) <= 1:
        return _system_status_message(kind, readings)
    return " ".join(_system_status_message(item, readings) for item in kinds)


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
    layer: Decision | None = None,
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
        # What the DECISION layer chose to do about the understanding above:
        # the route, the executor key it resolved to, what the work therefore
        # needs, and a templated sentence. Safe operational metadata only — the
        # same fields the status surface reads, never reasoning.
        "decision": layer.to_dict() if layer is not None else {},
        # The id the telemetry record carries, so what the client sees and what
        # the outcome is reported against are the same request.
        "request_id": request_id_for(understanding.id),
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
        # Configuration is read ONCE, at construction, and the layers below are
        # built from it: a limit that only some paths honour is a limit that is
        # not really there. Environment variables are the override channel, so
        # nothing here needs a code change to be tuned.
        self.config = NovaControlConfig.from_environment()
        # Phase 9.1: the bus that carries the lifecycle vocabulary, with error
        # isolation ON (one broken watcher must not fail the request that
        # announced itself to it) and a bounded history so "what just happened?"
        # is answerable without the durable journal.
        self.event_bus = EventBus(continue_on_error=True, history=200)
        self.runtime = EventDrivenRuntime(self.event_bus)
        # A COPY of the routing table, per application. The class attribute is
        # the declaration; a shared mutable dict would let anyone who registers
        # a handler (or a test that injects one) silently re-route every OTHER
        # application in the process.
        self._HANDLERS = dict(type(self)._HANDLERS)
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
        self.agent_registry = AgentRegistry()
        for agent in build_default_agents():
            self.agent_registry.register(agent)
        self.coordinator = CoordinatorAgent()
        self.tools = ToolRegistry()
        # The executor itself is built once the tool CATALOGUE exists (below):
        # validation, caching and normalization all read the same metadata, so
        # the thing that runs tools is constructed after the thing that
        # describes them.
        # ── Phase 4: the planner and the plan executor ───────────────────────
        # The compiler turns a decision plus a goal into steps that name a tool,
        # an effect, an expected result and a way to be checked. The executor
        # runs them in dependency order and reports what could NOT be verified.
        # Both reach the machine through ONE step runner, which is also the only
        # place a plan can start real work — so there is no route from a plan to
        # an action that skips the approval layer wired below.
        self.plan_compiler = PlanCompiler(
            tool_lookup=self._tool_for_intent_name,
            retry_policy=RetryPolicy(max_attempts=self.config.planning.max_step_attempts),
        )
        # Phase 8.1: the verification ENGINE, not a second verifier. It is a
        # DeterministicVerifier subclass, so every check registered above keeps
        # working; what it adds is a check for the steps that attached none
        # (a write is checked against the file, a command against its exit
        # code) and a per-tool strategy registry for the tools that know how
        # to check themselves. A step that states its own check still wins.
        self.plan_verifier = VerificationEngine(callables=_PLAN_VERIFICATIONS)
        # Step ids this caller has EXPLICITLY approved for the current run. Empty
        # by default: a step that needs confirmation and has not been approved is
        # denied rather than attempted, because silence is not consent.
        self.approved_plan_steps: set[str] = set()
        self.workflow_executor = WorkflowExecutor(
            step_handler=self._run_plan_step,
            verifier=self.plan_verifier,
            retry_policy=RetryPolicy(max_attempts=self.config.planning.max_step_attempts),
            confirmation=self._confirm_plan_step,
            announcer=self._step_announcement,
        )
        # Phase 8.2: recovery around a single action, bounded by the same retry
        # policy the executor uses and verified by the engine above. Its
        # alternatives are filtered against the LIVE tool registry, which is
        # why it is given a question to ask rather than a snapshot taken now —
        # the registry is still empty at this point in startup.
        self.recovery_engine = StepRecoveryEngine(
            retry_policy=RetryPolicy(max_attempts=self.config.planning.max_step_attempts),
            verifier=self.plan_verifier,
            known_tools=lambda: tuple(entry.tool.name for entry in self.tools.list()),
            confirmation_available=True,
        )
        # Phase 8.3/8.4: the task state machines and the pause/resume/cancel
        # controller. Nothing here is wired to a runner yet: a task is only
        # registered by the caller that starts one, so a control command can
        # never act on work this process is not actually doing. Phase 9.1: the
        # state machine's observer is where its transitions reach the bus, so a
        # watcher learns about a pause from the transition itself rather than
        # from whoever happened to issue the command.
        self.task_control = TaskController(observer=self._task_transition)
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
        # ── Phase 7: the model manager ─────────────────────────────────────
        # ONE place answers "which model should serve this, and is there room
        # for it?". It is built BEFORE the vision pipeline on purpose: the
        # vision provider below asks it which model declares VISION, so the
        # manager is on the request path rather than beside it.
        #
        # Its provider is the SAME ``OllamaBackend`` the lifecycle layer drives
        # (``intelligence/model_manager.py``), imported rather than re-written:
        # a second implementation of "list/load/unload/size" would be a second
        # set of bugs. No ``lifecycle`` is passed because the manager's own load
        # already performs the eviction the lifecycle would — in the order the
        # specification asks for, with the protection the lifecycle lacks: a
        # model an ACTIVE task is using is refused, never unloaded underneath it.
        model_config = self.config.models
        self.model_manager = ModelManager(
            OllamaBackend(),
            registry=ModelRegistry(
                [
                    ModelProfile.from_mapping(declaration)
                    for declaration in model_config.declarations
                    if str(declaration.get("name", "")).strip()
                ]
            ),
            keep_alive=KeepAliveSettings.from_mapping(model_config.keep_alive_settings()),
            headroom_bytes=model_config.headroom_bytes,
        )
        # ── Phase 6: the vision pipeline ─────────────────────────────────
        # ONE manager owns "what is in this image?": OCR first (cheap and
        # deterministic), the model only when the text cannot answer it, and a
        # structured result either way. Its provider is resolved from
        # configuration, which is what makes the VLM a plug rather than a
        # dependency — see vision/providers.py and configs' `vision` section.
        self.vision_manager = VisionManager(
            provider=self._vision_pipeline_provider(),
            prefer_ocr=self.config.vision.prefer_ocr,
        )
        # The last structured result, kept so a follow-up ("now summarise it",
        # "click that button") plans against what was SEEN rather than against
        # the prose a person read. This is the "structured vision result ->
        # planner" hand-off: one bounded object, replaced each time.
        self._last_vision_result: VisionResult | None = None
        # THE Global Intelligence Layer — the single language brain every
        # entry point consumes before any subsystem sees raw text. It shares
        # the brain's LLM provider (semantic fallback only when deterministic
        # layers cannot parse) and keeps its own rolling interaction context.
        self.intelligence = GlobalInputIntelligence(
            completion_provider=self.brain.completion_provider,
        )
        # ── Decision engine (Phase 3) ────────────────────────────────
        # Between understanding and execution: which machinery does this request
        # deserve — a deterministic capability, a subsystem handler, the planner,
        # the agentic loop, the vision pipeline, the local model or the cloud —
        # and what does it therefore need. It executes NOTHING; the approval
        # layer is untouched. Local by default, and an external provider is only
        # ever an advisor (see decision/providers.py).
        decision_config = self.config.decision
        # Phase 5: ONE selector, shared by the decision layer and the planning
        # path, so "which tool would carry this out" has a single answer wherever
        # it is asked. It reads the same catalog the understanding layer plans
        # from, plus the live tool registry, and names a tool but never runs one.
        self.tool_selector = ToolSelector(
            catalog=self.intelligence.catalog,
            capabilities=self.intelligence.capabilities,
            # Live, not a snapshot: tools registered after boot (a plugin, a
            # restored artifact) must be selectable without a restart.
            registered=lambda: tuple(entry.tool.name for entry in self.tools.list()),
            # Phase 9.1: one observer on the ONE selector, so "tool.selected"
            # is announced for the decision path and the planning path alike.
            observer=self._tool_selected,
        )
        # ── Phase 5: tool metadata, discovery and the execution pipeline ─────
        # ONE catalogue describes every tool this build knows about — declared
        # entries, plus what the intent catalogue, the capability registry and
        # the runtime registry each contribute — and ONE retriever ranks it
        # against a request. The planner and the model escalation are given the
        # few tools that FIT the request rather than all of them: a list of
        # seventeen names is not a choice, it is a lottery.
        self._register_read_only_tools()
        self.tool_catalog = build_tool_catalog(
            intents=self.intelligence.catalog,
            capabilities=self.intelligence.capabilities,
            registry=self.tools,
        )
        self.tool_retriever = ToolRetriever(self.tool_catalog)
        self.tool_cache = ToolResultCache()
        # Phase 8.5: ONE risk/permission layer, reading the same catalogue the
        # executor already gates on. The executor asks it what an action IS, so
        # a tool that declares no permission scopes but is destructive or
        # external is still put to a person instead of running unannounced.
        self.risk = PermissionManager(catalog=self.tool_catalog)
        self.tool_executor = ToolExecutor(
            self.tools,
            catalog=self.tool_catalog,
            cache=self.tool_cache,
            normalizer=OutputNormalizer(),
            risk=self.risk,
            events=self._tool_event,
        )
        # Phase 9.2/9.3: the capability registry is the ONE place that answers
        # what this installation can do. It is the registry the intelligence
        # layer already maintains — attached to the catalogues rather than
        # restated — plus the tools from the catalogue (projected) and the plan
        # actions this application can actually carry out, declared below.
        self.capabilities: CapabilityRegistry = self.intelligence.capabilities
        # Attached, not fed: the registry projects the tools the catalogue says
        # are really here at QUERY time, so a tool that becomes runnable later
        # needs no re-registration, and there is no second copy of the tool set
        # that could disagree with the catalogue.
        self.capabilities.attach(
            catalog=self.intelligence.catalog,
            tools=self.tool_catalog,
            tool_names=lambda: tuple(entry.tool.name for entry in self.tools.list()),
            model_probe=self._model_available,
        )
        self._declare_actions()
        self.decision = DecisionEngine(
            capabilities=self.intelligence.capabilities,
            provider=build_decision_provider(
                decision_config.provider,
                endpoint=decision_config.jev_endpoint,
                timeout_s=decision_config.jev_timeout_s,
                allow_remote=decision_config.allow_remote,
            ),
            requested_provider=decision_config.provider,
            selector=self.tool_selector,
        )
        # The agent loop drives the phases in order and cannot skip the last
        # two: results are verified, and anything unverified travels into the
        # final response instead of being rounded up to success. It has no
        # opinion about tools or models — every phase is one of this
        # application's own methods.
        self.agent_loop = AgentLoop(
            understand=self.intelligence.understand_async,
            decide=self._decide_for_agent,
            plan=self._plan_for_agent,
            execute=self.workflow_executor.execute,
            escalate=self._escalate_plan_run,
            max_cycles=self.config.planning.max_cycles,
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
        # 4. The Phase 6 pipeline — re-resolved through configuration, so an
        #    operator who set `vision.provider: none` keeps that refusal even
        #    while a model is installed for the desktop's own element location.
        self.vision_manager.set_provider(self._vision_pipeline_provider())

    def _vision_pipeline_provider(self) -> VisionProvider:
        """The vision provider the pipeline should use, from configuration.

        The preference order is the whole design, so it is written down once:

        1. ``vision.provider: none`` wins over everything. A deployment that
           must not run a vision model says so, and installing one for the
           desktop's element locator must not quietly change that.
        2. A dedicated vision model an operator configured — it exists precisely
           because the chat brain is usually a text model that cannot see.
        3. A LOCAL model pinned by name in configuration.
        4. Whatever the MODEL MANAGER selects for a capability requirement of
           ``{VISION}`` — the capability registry, not a name. The runtime is
           asked what it actually offers first, so an undeclared vision build
           that was pulled five minutes ago is selectable, and a text-only
           build that merely LOOKS multimodal is not (see
           ``models/profiles.py``); the pool is what is installed, so a model
           that is not there cannot be chosen.
        5. Whatever the chat brain happens to be. On a default install that is
           the Echo fallback, which resolves to the honest null provider.

        Nothing here can make a text model into a vision model: the capability
        gate in ``build_vision_provider`` refuses one, so a machine with only a
        text brain reports ``available: false`` instead of inventing what a
        screenshot contains.
        """
        settings = self.config.vision
        if settings.provider == "none":
            return NullVisionProvider()
        if self._vision_provider is not None:
            return build_vision_pipeline_provider(self._vision_provider)
        if settings.model:
            local = build_ollama_provider(model=settings.model)
            if local is not None and provider_supports_vision(local):
                return build_vision_pipeline_provider(
                    local, name="ollama", model=settings.model
                )
        selected = self._selected_vision_model()
        if selected:
            local = build_ollama_provider(model=selected)
            # A SECOND gate, deliberately: the manager chose by declared and
            # runtime-reported capability, and this checks the built provider
            # really can carry an image. Selection says what SHOULD see; only the
            # provider can say what does.
            if local is not None and provider_supports_vision(local):
                return build_vision_pipeline_provider(
                    local, name="ollama", model=selected
                )
        return build_vision_pipeline_provider(self.brain.completion_provider)

    def _selected_vision_model(self) -> str:
        """The model the capability registry picks for a vision requirement.

        Called once, while the pipeline is being wired: the runtime is asked to
        describe its models (bounded — see ``refresh_capabilities``), the
        registry is reconciled with the answers, and the smallest model that
        declares ``VISION`` wins. Returns "" when the runtime is unreachable or
        nothing declares vision, which is what leaves the decision to the
        remaining fallbacks rather than pinning a model that is not there.
        """
        try:
            self.model_manager.refresh_capabilities()
            selection = self.model_manager.select_for_request(requires_vision=True)
        except Exception:  # pragma: no cover - a probe must never block boot
            return ""
        return selection.model if selection.route == "local" else ""

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
    # Intent -> executor key. The table itself lives in the decision layer
    # (decision/routing.py), because "this intent is carried out by that
    # subsystem" IS a decision rather than application policy. This is a view of
    # it, under the name existing callers and tests already use, so there is
    # still exactly one table.
    _GIL_ROUTES: dict[IntentName, str] = INTENT_HANDLERS

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

    # ── Model + hardware lifecycle ──────────────────────────────────────

    async def model_status(self) -> dict[str, Any]:
        """Resident models, available memory, and the exclusivity policy.

        This machine has 16 GB of RAM and a chat model and a vision model each
        want a large slice of it, so the manager is exclusive by default: one
        model resident at a time. The probe talks to the runtime, so it runs in
        a worker thread — a status call must never stall the event loop.
        """
        manager = self.intelligence.model_manager
        status = await asyncio.to_thread(manager.get_model_status)
        # Phase 7's readout, measured off the event loop: what is resident, what
        # the machine has, and which model the capability registry would choose
        # for each kind of work. The lifecycle layer above still reports the
        # runtime's own view, so the two can be compared rather than trusted.
        health = await asyncio.to_thread(self.model_manager.health)
        # The keep-alive sweep runs HERE, in a worker thread, on the status read
        # every client already polls — there is no timer thread in this process,
        # and inventing one would be a second policy nobody asked for. Under the
        # default policy (no idle window) it returns before touching the runtime,
        # so an unconfigured install pays nothing; a deployment that configured
        # "keep it warm for N minutes" gets the release applied the next time
        # anyone asks about the models (and on every explicit load).
        expired = await asyncio.to_thread(self.model_manager.enforce_keep_alive)
        return {
            **status.to_dict(),
            "exclusive": manager.exclusive,
            "keep_alive_expired": list(expired),
            "chat_model": self.brain.model_name,
            "vision_model": str(self.vision_llm_status().get("model", "") or ""),
            "local_models": list(await asyncio.to_thread(manager.get_models)),
            "pipeline": await asyncio.to_thread(self._vision_pipeline_report),
            "manager": health,
            "selections": await asyncio.to_thread(self._model_routing_report),
        }

    def _vision_pipeline_report(self) -> dict[str, Any]:
        """Which model the vision pipeline is actually wired to, and which one
        the capability registry would choose for it.

        Two different facts, reported side by side on purpose: the FIRST is what
        the next screenshot will be read by (resolved at boot and swappable by an
        operator), the SECOND is what a fresh resolution would pick now. A
        disagreement means a model was installed or removed since boot, which is
        exactly what an operator looking at this panel needs to see.
        """
        status = dict(self.vision_manager.status())
        return {
            "vision_model": self._selected_vision_model(),
            "pipeline_model": str(getattr(self.vision_manager.provider, "model", "") or ""),
            "provider": str(getattr(self.vision_manager.provider, "name", "") or ""),
            "available": bool(getattr(self.vision_manager.provider, "available", False)),
            "reader": str(status.get("reader", "") or ""),
            "prefer_ocr": self.config.vision.prefer_ocr,
        }

    def _model_routing_report(self) -> dict[str, Any]:
        """What the manager would choose for each kind of work, right now.

        The four questions the specification's example flows ask, answered from
        capabilities and measured resources: nothing, plain reasoning, vision,
        and the strongest local model. ``route`` says whether the answer is a
        local model, the cloud, or nothing — the three outcomes a caller has to
        distinguish.
        """
        report: dict[str, Any] = {}
        for label, kwargs in (
            ("reasoning", {"needs_reasoning": True}),
            ("coding", {"needs_coding": True}),
            ("vision", {"requires_vision": True}),
            ("vision_tools", {"requires_vision": True, "needs_tools": True}),
            ("strongest", {"needs_reasoning": True, "latency_preference": "quality"}),
        ):
            try:
                report[label] = self.model_manager.select_for_request(**kwargs).to_dict()
            except Exception:  # pragma: no cover - a readout must never throw
                report[label] = {}
        return report

    async def load_model(self, model: str) -> dict[str, Any]:
        """Load one model, freeing room FIRST when the measurement requires it.

        One place requests a load, so the memory rule lives here rather than in
        every caller: another model being resident is a reason to unload it,
        never a reason to hold both.
        """
        name = model.strip()
        if not name:
            raise ValueError("A model name is required.")
        # Phase 7: route a load through the MANAGER, so an explicit request gets
        # the same measured, verified treatment as an automatic one — the
        # registry supplies the profile whose footprint the fit check uses, room
        # is made when the measurement says so, and the runtime is asked
        # afterwards whether the model is really there. Without this the endpoint
        # and the automatic path would be two policies over one machine.
        with self.intelligence.telemetry.stage("model_load"):
            outcome = await asyncio.to_thread(self.model_manager.load, name)
        await self._announce(
            EventType.MODEL_LOADED.value,
            model=outcome.model,
            loaded=outcome.loaded,
            verified=outcome.verified,
            evicted=list(outcome.evicted),
            reason=outcome.reason,
        )
        steps = [dict(step) for step in outcome.steps]
        # The response keeps the shape callers already read and adds the
        # manager's record of the six steps, so a refusal can be explained with
        # the measurements behind it rather than a single sentence.
        return {
            "model": outcome.model,
            "loaded": outcome.loaded,
            "evicted": list(outcome.evicted),
            "reason": outcome.reason,
            "available_memory_bytes": outcome.available_memory_bytes,
            "fits_after_evict": next(
                (step.get("fits") for step in steps if step.get("step") == "estimate"),
                None,
            ),
            "verified": outcome.verified,
            "refused": outcome.refused,
            "manager": outcome.to_dict(),
        }

    async def unload_model(self, model: str = "") -> dict[str, Any]:
        """Release one model, or every resident model when none is named.

        "Free the RAM" has to be one call: unloading a model that is not named
        is the actual intent when someone asks for memory back.
        """
        # Through the MANAGER for the same reason a load is: it is the layer that
        # knows which models a task is currently using, so it is the layer that
        # can refuse to pull one out from under a running task. The lifecycle
        # layer below would unload whatever it was handed.
        name = model.strip()
        released = await asyncio.to_thread(self.model_manager.unload, name)
        for released_model in released or ():
            await self._announce(EventType.MODEL_UNLOADED.value, model=released_model)
        if name:
            return {"unloaded": bool(released), "models": list(released)}
        return {"unloaded": True, "models": list(released)}

    def _tool_for_intent(self, intent: IntentName) -> str:
        """The tool an intent would reach, or "" when none is registered.

        The catalog is the single source: the NLU records the tool it resolved,
        and the decision layer names the same one, so "what would run" cannot
        disagree between the readout and the routing.
        """
        tools = self.intelligence.catalog.tools_for(intent)
        return tools[0] if tools else ""

    def _decision_environment(self) -> DecisionEnvironment:
        """What this machine can actually route TO, described to the decision layer.

        The decision engine never reaches into the application to ask "is a model
        configured?" — a decision made against assumed hardware is how a request
        ends up queued behind a model that was never installed. The application
        describes its providers here instead, and the engine stays a pure
        function of what it was told.

        Deliberately cheap and side-effect free: this runs on EVERY understood
        request, so it reads configuration the providers already hold and probes
        nothing. Resident-model state changes on its own schedule and belongs to
        the Model Manager's status surface, not to a per-request decision.
        """
        cloud = self.cloud_llm_status()
        vision = self.vision_llm_status()
        # A local model can be configured AND a cloud key saved: the mode decides
        # which one answers, so both are reported and the mode travels with them.
        local_model = self.brain.model_name if self.brain.model_configured else ""
        return DecisionEnvironment(
            local_model=local_model,
            cloud_model=str(cloud.get("model", "") or ""),
            cloud_configured=bool(cloud.get("configured")),
            vision_model=str(vision.get("model", "") or ""),
            vision_available=bool(vision.get("configured")),
            mode=str(getattr(self.brain, "mode", "auto")),
        )

    async def handle_request(self, text: str, *, image: str = "") -> ApplicationResponse:
        """Route a natural-language request through the available subsystems.

        The Global Intelligence Layer understands first (normalization, typo
        tolerance, references, multi-intent decomposition) and picks the
        capability handler; the brain still runs that handler and shapes the
        response, so task records, conversation memory, and the response
        envelope stay identical to the legacy flow. Only what the GIL leaves
        unresolved reaches the legacy `brain.decide` classifier.

        ``image`` is a picture that travelled WITH the request — a file path a
        client attached, not a screen to capture. Its presence is a fact the
        understanding layer is told rather than left to infer from the wording,
        so *"what is this?"* arrives needing eyes while *"what is this?"* with
        nothing attached is read as the plain question it is. The vision
        handler then reads THAT image instead of taking a screenshot.
        """
        await self.memory.remember(
            MemoryNamespace.CONVERSATION,
            f"request-{len((await self.memory.retrieve(MemoryNamespace.CONVERSATION, '', limit=100)))}",
            {"text": text}, text=text, importance=0.2,
        )
        attached = str(image or "").strip()
        request = BrainRequest(
            text=text,
            context={**self.status(), **({"image": attached} if attached else {})},
        )
        task = self.tasks.create(text, kind="ask")
        self.tasks.update(task.id, TaskRecordStatus.RUNNING, progress=0.1)
        # Phase 9.1: the request's own id is the correlation id every event it
        # causes carries, so one thread of work can be followed end to end
        # ("the intent, the decision about it, the tool it ran") in the history.
        correlation = task.id
        # Everything this request goes on to do is correlated as ITS work.
        _CURRENT_REQUEST.set(correlation)
        await self._announce(
            EventType.TASK_STARTED.value,
            correlation_id=correlation,
            task_id=task.id,
            kind="ask",
        )
        # Phase 7 telemetry: ONE trace per request, opened before understanding
        # and closed after the response is shaped. RAM is sampled at both ends by
        # the caller (this layer owns the monitor), so what a request cost in
        # memory is a measured fact rather than an average of samples that were
        # taken for another purpose.
        trace = self.intelligence.telemetry.begin_request(
            ram_before=self.model_manager.monitor.available_ram_bytes()
        )

        # GLOBAL INPUT INTELLIGENCE: choose the capability from meaning, not
        # exact phrasing (normalization, typo tolerance, references,
        # multi-intent). Fallback: the legacy brain classifier.
        with self.intelligence.telemetry.stage("nlu"):
            understood = await self.intelligence.understand_async(text, has_image=bool(attached))
        await self._announce(
            EventType.INTENT_DETECTED.value,
            correlation_id=correlation,
            intent=understood.intent.intent.value,
            strategy=understood.strategy,
            confidence=understood.intent.confidence,
            has_image=bool(attached),
        )
        await self._announce(
            EventType.CONTEXT_RESOLVED.value,
            correlation_id=correlation,
            strategy=understood.strategy,
            intent=understood.intent.intent.value,
        )
        # DECISION ENGINE: given what was understood, decide what to DO with it —
        # a deterministic capability, a subsystem handler, the planner, the
        # agentic loop, the vision pipeline, a language model, or one clarifying
        # question — and what that therefore needs. Deterministic and offline by
        # default, and it executes NOTHING: the approval layer still gates every
        # action, and an external provider can only ever advise a route.
        decision_started = time.perf_counter()
        with self.intelligence.telemetry.stage("decision"):
            decision_layer = await self.decision.decide_async(
                understood.intent,
                context=self.intelligence.context,
                environment=self._decision_environment(),
                strategy=understood.strategy,
            )
        await self._announce(
            EventType.DECISION_CREATED.value,
            correlation_id=correlation,
            route=decision_layer.route.value,
            decision_type=decision_layer.decision_type.value,
            capability=decision_layer.selected_capability,
            model=decision_layer.selected_model,
            requires_planning=decision_layer.requires_planning,
            requires_confirmation=decision_layer.requires_confirmation,
        )
        self.intelligence.telemetry.record_decision(
            route=decision_layer.route.value,
            decision_type=decision_layer.decision_type.value,
            capability=decision_layer.selected_capability,
            model=decision_layer.selected_model,
            provider=decision_layer.provider,
            reason_code=decision_layer.reason_code.value,
            fallback=bool(decision_layer.metadata.get("provider_fallback")),
            requires_planning=decision_layer.requires_planning,
            requires_confirmation=decision_layer.requires_confirmation,
            latency_ms=(time.perf_counter() - decision_started) * 1000.0,
            request_id=request_id_for(understood.intent.id),
        )
        gil_intent = understood.intent.intent if understood.strategy != "clarification" else None
        handler_key = self._GIL_ROUTES.get(gil_intent) if gil_intent is not None else None
        # The decision layer is authoritative on the EXECUTOR, not only on the
        # route. Two cases where the intent alone cannot name one:
        #   * an intent the routing table does not list at all. No entry means
        #     "no subsystem claims this intent", which is NOT the same as "this
        #     request has no executor" — the decision may have chosen the planner
        #     precisely because the reading decomposed into several clauses;
        #   * an image that must be LOOKED AT: its route is `vision`, and asking a
        #     question cannot answer a question about a picture.
        # Handling neither here is what silently dropped the second and third
        # clause of a multi-clause request: the decision said "plan these three
        # steps" and the handler lookup said "unknown", so the request became
        # whatever the legacy classifier guessed from its FIRST clause.
        decided_handler = str(getattr(decision_layer, "handler", "") or "")
        # ...UNLESS the decision is simply "the model will handle this". Rule 3's
        # bare reasoning fallback says no subsystem claims the reading and language
        # handling should take it, which is exactly the case the assistant's own
        # classifier knows more about than the word "chat" does — so it keeps the
        # legacy path rather than being funnelled into a chat reply.
        bare_reasoning_fallback = (
            decision_layer.decision_type is DecisionType.REASONING
            and decided_handler == "chat"
        )
        if (
            handler_key is None
            and not bare_reasoning_fallback
            and decided_handler in self._GIL_HANDLER_KEYS
        ):
            handler_key = decided_handler
        if decision_layer.route is DecisionRoute.VISION and handler_key != "vision":
            handler_key = "vision"
        # A decision that says the request must be SEQUENCED cannot be carried out
        # by a handler that does one thing. The reader decomposed the sentence into
        # several actions and the planner is the layer that orders them — so when
        # the executor the layers landed on cannot sequence (chat answers, explore
        # researches: one step each) and the decision says the work needs planning,
        # the planning handler takes it. This is the third of the specification's
        # example flows: *"find the project, run the tests and explain why they
        # fail"* is three actions, and answering it with a chat reply performs one
        # of them and silently drops the other two.
        #
        # Narrow on purpose: ONE action with a sequencing requirement (a single
        # composite browser command, a plain question the assessment called hard)
        # keeps the executor it has — those handlers either sequence internally or
        # do not need to, and re-routing them would be churn.
        if (
            decision_layer.requires_planning
            and len(tuple(getattr(decision_layer, "actions", ()) or ())) > 1
            and handler_key not in SEQUENCING_HANDLERS
        ):
            handler_key = "plan"
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
        # so no capability has to re-parse the sentence to know what was asked —
        # and the DECISION beside it, so the planner and the agent loop execute
        # what was decided (which executor, which tool, whether sequencing is
        # required) instead of re-deriving it from the words.
        understanding = understood.intent
        request = BrainRequest(
            text=text,
            context={
                **request.context,
                "nlu": understanding.to_dict(),
                "decision": decision_layer.to_dict(),
            },
        )
        handler = self._HANDLERS.get(decision.intent)
        # Whether the request was actually CARRIED OUT is known here, not in the
        # understanding layer, so the outcome is reported against the same
        # request id. An exception is re-raised unchanged: recording a failure
        # must not swallow it.
        try:
            if handler:
                route, payload = await handler(self, request, text)
            else:
                route, payload = await self._handle_agent(request, text)
        except Exception as exc:
            self.intelligence.telemetry.record_outcome(
                request_id=request_id_for(understanding.id),
                success=False,
                detail=type(exc).__name__,
            )
            # A request that FAILED was still a request: measuring only the
            # happy path is how a latency average ends up describing the one
            # case nobody complains about.
            self._record_request(
                trace,
                decision_layer=decision_layer,
                understanding=understanding,
                success=False,
            )
            await self._announce(
                EventType.TASK_FAILED.value,
                correlation_id=correlation,
                task_id=task.id,
                error=type(exc).__name__,
                route=decision.intent.value,
            )
            raise
        self.intelligence.telemetry.record_outcome(
            request_id=request_id_for(understanding.id), success=True
        )

        # The request-understanding block travels with every response: safe
        # operational metadata only (what understood it, the intent, the
        # confidence, the cost) — never chain-of-thought or model reasoning.
        with self.intelligence.telemetry.stage("response"):
            payload = {
                **payload,
                "nlu": _nlu_payload(
                    understanding, understood, decision, self.brain, decision_layer
                ),
            }
            response = await self.brain.shape_response(request, decision, payload)
        self.tasks.update(task.id, TaskRecordStatus.COMPLETED, progress=1.0, result=response.to_dict())
        await self._announce(
            EventType.TASK_COMPLETED.value,
            correlation_id=correlation,
            task_id=task.id,
            route=route or decision.intent.value,
        )
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
        self._record_request(
            trace, decision_layer=decision_layer, understanding=understanding, success=True
        )
        return ApplicationResponse(route, decision.intent.value, response.summary, payload)

    def _record_request(
        self,
        trace: Any,
        *,
        decision_layer: Decision,
        understanding: Any,
        success: bool,
    ) -> None:
        """Close a request trace and file the row.

        The row answers the specification's questions in one place: total
        latency, where the time went, which model and provider were SELECTED (by
        the decision engine, before any model ran), whether a model was needed
        at all, and how much RAM the request moved. No prompt, no answer, no
        reasoning — durations and identifiers only, because ``/status`` and
        ``/intelligence`` are reachable over HTTP.
        """
        route = decision_layer.route
        # "Fast" is not "answered without the decision engine": it is a request
        # carried out by the machine's own deterministic layers, which is what
        # keeps the model completely out of the loop.
        fast_path = route in {
            DecisionRoute.DIRECT_TOOL,
            DecisionRoute.SYSTEM_TOOLS,
            DecisionRoute.LOCAL_CAPABILITY,
        } and not bool(getattr(understanding, "used_model", False))
        try:
            self.intelligence.telemetry.end_request(
                trace,
                ram_after=self.model_manager.monitor.available_ram_bytes(),
                model=decision_layer.selected_model,
                provider=decision_layer.provider,
                fast_path=fast_path,
                success=success,
            )
        except Exception:  # pragma: no cover - telemetry must never break a request
            self.intelligence.telemetry.end_request(trace)

    # ── Intent handlers ──────────────────────────────────

    async def _handle_clarify(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        return "brain", {"message": "Please provide a little more detail."}

    async def _handle_chat(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        """Answer with the configured model, with its residency policy applied.

        Phase 7 brackets the call: the local chat model is the one the manager
        reports as ACTIVE, so it is recorded as IN USE for the duration — which is
        what stops an eviction (a vision request making room for a VLM) from
        unloading the weights underneath an answer that is still being written —
        and the keep-alive policy decides what happens the moment it finishes.
        Model-free answers (the scratch path, a machine with no model configured)
        take no activity at all rather than a fake one.
        """
        model = self.brain.model_name if self.brain.model_configured else ""
        if model:
            self.model_manager.begin_activity(model)
        try:
            response = await self.brain.chat(request)
        finally:
            if model:
                self.model_manager.release_after_use(model)
            self._record_model_timings("chat", self.brain.completion_provider)
        return "chat", response.payload

    # ── Phase 7: model residency on the request path ────────────────────

    def _vision_pipeline_model(self) -> str:
        """The LOCAL model the vision pipeline will call, or "".

        Read from the wired provider rather than from configuration, because the
        provider is what will actually be called: a cloud preset, the null
        provider and a test double all report a name that is not a local
        runtime's to load, and only the local Ollama backend is.
        """
        provider = self.vision_manager.provider
        if str(getattr(provider, "name", "")) != "llm:ollama":
            return ""
        return str(getattr(provider, "model", "") or "")

    async def _acquire_vision_model(self, model: str) -> ModelLoadOutcome | None:
        """Make room for the vision model BEFORE it is used, or ``None``.

        The specification's own switching example, on the automatic path: the
        chat model is resident, a screenshot needs a VLM, so the manager measures,
        unloads what it may, loads the vision model and verifies it. Only for a
        local runtime this layer can actually drive — a cloud provider is loaded
        by someone else and a model the runtime has never heard of is not
        something the manager can make room for. Never raises: a resource
        decision must not be the reason a request fails.
        """
        if not model:
            return None
        try:
            return await asyncio.to_thread(self.model_manager.acquire, model)
        except Exception:  # pragma: no cover - acquire is already defensive
            return None

    async def _release_model(self, model: str) -> None:
        """Apply the keep-alive policy to a model whose use has finished."""
        try:
            await asyncio.to_thread(self.model_manager.release_after_use, model)
        except Exception:  # pragma: no cover - a release must never throw
            return

    def _record_model_timings(
        self, reason: str, provider: object | None
    ) -> dict[str, float]:
        """File the model's OWN timing breakdown for the call that just finished.

        Phase 7's telemetry asks for first-token latency and generation rate, and
        until now they were recorded only when the NLU gave up and escalated — so
        the two figures described one path out of several and the model that
        answered most requests reported nothing. The breakdown lives on the
        provider that made the call, so it is read from there and filed under the
        reason that produced it. Silence records nothing rather than a zero, and
        a provider without measurements costs the request nothing.
        """
        measured = getattr(provider, "last_timings", None)
        if not isinstance(measured, dict) or not measured:
            return {}
        try:
            return self.intelligence.telemetry.record_model_timings(
                reason=reason, timings=measured
            )
        except Exception:  # pragma: no cover - telemetry must never break a request
            return {}

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
        plan = self.plan_for(text, decision=request.context.get("decision"))
        payload: dict[str, Any] = {"plan": plan.to_dict()}
        # Phase 4 + 5: the plan runs with the DECISION and the SELECTION beside
        # it, so a client can show which tool a step would reach and whether the
        # decision asked for sequencing or confirmation — without the planner
        # re-reading the sentence to work either out.
        decision = request.context.get("decision")
        if isinstance(decision, dict):
            payload["decision"] = decision
        understanding = request.context.get("nlu")
        if isinstance(understanding, dict):
            payload["selection"] = self._selection_for(request, understanding)
        # Phase 5: the tools that FIT this goal, discovered rather than dumped.
        # Reported beside the plan so the choice a step made can be checked
        # against what the tool layer thought the request was about — discovery
        # informs, it never rewrites a step's tool, because a tool that merely
        # looks related is not one that was asked for.
        payload["tools"] = self.discover_tools(text, limit=3)
        # Phase 6: when an image has ALREADY been read this turn, the plan is
        # compiled with what was seen beside it, so a step about "that error"
        # resolves against structured evidence (the error lines, the elements)
        # rather than against a summary the planner would have to re-read. The
        # result is evidence, never an instruction: it cannot add a step.
        vision_evidence = self.vision_evidence()
        if vision_evidence:
            payload["vision"] = vision_evidence
        if not plan.needs_clarification:
            workflow = await self.workflow_executor.execute(plan)
            payload["workflow"] = workflow.to_dict()
            # The plan's own state, published with it: a caller can see per-step
            # status, what was verified, and what was left unconfirmed.
            payload["state"] = workflow.state()
        return "planning", payload

    # ── Phase 6: the vision pipeline, end to end ────────────────────────

    async def analyze_image(
        self,
        source: str,
        *,
        question: str = "",
        target: str = "",
        task: VisionTaskKind | None = None,
        prefer_ocr: bool | None = None,
        allow_vlm: bool = True,
    ) -> VisionResult:
        """Read one image and return a STRUCTURED result — the pipeline's door.

        This is the entry point every visual request goes through, whatever the
        caller: the request path, the API, a test. It picks the task from the
        question when the caller did not ("read the text" is OCR, "where is the
        login button" is a locate), delegates to the manager, and records the
        result so a later step can plan against what was seen.

        The result is stored BEFORE it is returned, so a caller that raises on
        the way out has still left the pipeline's own memory consistent.
        """
        asked = " ".join(str(question or "").split())
        request = VisionRequest(
            source=source,
            question=asked,
            task=task or task_for_question(asked, target=target),
            target=" ".join(str(target or "").split()),
            prefer_ocr=self.vision_manager.prefer_ocr if prefer_ocr is None else prefer_ocr,
            allow_vlm=allow_vlm,
        )
        result = await self.vision_manager.analyze(request)
        self._last_vision_result = result
        return result

    @property
    def last_vision_result(self) -> VisionResult | None:
        """The most recent structured result, for the planner and the status surface."""
        return self._last_vision_result

    def vision_evidence(self) -> dict[str, Any]:
        """The last result as data, or nothing when no image has been read.

        Nothing is a real answer: an empty shape would let a planner believe an
        image was seen when none was.
        """
        result = self._last_vision_result
        return result.to_dict() if result is not None else {}

    # ── Phase 4: planning, executing and escalating ──────────────────────

    def plan_for(self, goal: str, *, decision: object | None = None) -> Plan:
        """The plan for a goal — from the compiler, with the old engine as fallback.

        The compiler produces steps that name a tool, an effect and a way to be
        checked. It is also deliberately conservative: a goal it cannot
        recognise still yields a reasoning step rather than an empty plan, and
        only a decision that says "ask first" produces a clarification.
        PlanningEngine remains the fallback for the plain "turn this sentence
        into ordered steps" path other callers use.
        """
        with self.intelligence.telemetry.stage("planning"):
            plan = self.plan_compiler.compile(goal, decision=decision)
        if plan.steps or plan.needs_clarification:
            self._announce_plan_created(plan)
            return plan
        legacy = self.planning.create_plan(goal)
        return self._announce_plan_created(
            Plan(
                goal=legacy.goal,
                steps=tuple(
                    PlanStep(
                        title=step.title,
                        description=step.description,
                        depends_on=step.depends_on,
                        id=step.id,
                    )
                    for step in legacy.steps
                ),
                needs_clarification=legacy.needs_clarification,
            )
        )

    def _announce_plan_created(self, plan: Plan) -> Plan:
        """Announce a plan the moment it exists, and hand it straight back.

        Returned rather than rebuilt so the announcement cannot become a second
        place a plan is made: what a watcher hears about IS what will run.
        """
        self._announce_soon(
            EventType.PLAN_CREATED,
            goal=plan.goal,
            steps=tuple(step.id for step in plan.steps),
            titles=tuple(step.title for step in plan.steps),
            needs_clarification=plan.needs_clarification,
        )
        return plan

    async def run_plan(self, goal: str, *, approved: Sequence[str] = ()) -> dict[str, Any]:
        """Run a goal through the whole agent loop, and report what happened.

        ``approved`` names the steps the caller has authorized. It is the ONLY
        way a step that needs confirmation may run: the loop cannot approve its
        own plan, and a step that was not named is denied rather than attempted.
        """
        self.approved_plan_steps = {str(step_id) for step_id in approved}
        try:
            run = await self.agent_loop.run(goal)
        finally:
            self.approved_plan_steps = set()
        return run.to_dict()

    async def _decide_for_agent(self, understood: Any) -> Decision:
        """The decision phase of the loop: the same engine the request path uses."""
        return await self.decision.decide_async(
            understood.intent,
            context=self.intelligence.context,
            environment=self._decision_environment(),
            strategy=understood.strategy,
        )

    def _plan_for_agent(self, goal: str, understood: Any, decision: Decision) -> Plan:
        """The plan phase: compile from the goal AND the decision about it."""
        return self.plan_for(goal, decision=decision)

    async def _escalate_plan_run(self, goal: str, plan: Plan, workflow: WorkflowResult) -> str:
        """Hand a failed run to the model when one is configured.

        Only reasoning problems reach here — a refusal does not, because no
        amount of thinking grants a permission a person declined. Without a
        model the escalation is reported as what it is rather than dressed up
        as an answer. The escalation joins the conversation on purpose: it is
        part of what the user asked for, and its outcome is theirs to see.
        """
        if not self.brain.model_configured:
            return _escalation_text(goal, workflow)
        # Discovery, not a dump: the model is shown the handful of tools that fit
        # THIS goal, chosen by the same retriever the planner consults.
        prompt = _escalation_prompt(goal, workflow, self.discovered_tools(goal))
        try:
            response = await self.brain.chat(BrainRequest(text=prompt, context=self.status()))
        except Exception as exc:  # a failed escalation is reported, never hidden
            detail = f"{type(exc).__name__}: {exc}"
            return f"{_escalation_text(goal, workflow)}\n(Model escalation failed: {detail})"
        return str(response.summary or _escalation_text(goal, workflow))

    def _tool_for_intent_name(self, name: str) -> str:
        """The registered tool for an intent NAME, for the plan compiler."""
        try:
            return self._tool_for_intent(IntentName(name))
        except ValueError:
            return ""

    # ── Phase 5: discovery, normalization, caching ────────────────────────

    def _register_read_only_tools(self) -> None:
        """Register the read-only introspection tools this build can really run.

        Until now the registry was empty on a default install, so the pipeline's
        later stages — validation, caching, normalization — had no path that
        exercised them in production even though each was tested in isolation.
        These three are the specification's own examples of work worth caching
        (OS information, hardware information, installed applications, system
        capabilities), backed by the readers this application already owns rather
        than by new machinery.

        All three return ONLY stable facts. ``machine_facts`` reports total memory and
        disk CAPACITY, never free space or uptime: a tool whose declared cache
        lifetime is ten minutes must not return a number that changes in one, or
        the cache becomes the lie it exists to avoid. Free space and uptime are
        live readings and stay with ``system_monitor``, which declares them
        volatile — this split is the whole reason the caching contract is worth
        having.
        """
        self.tools.register(
            FunctionTool(
                "machine_facts",
                ToolSchema(
                    "machine_facts",
                    "Report stable facts about this machine.",
                    parameters=(
                        ToolParameter("scope", "string", description="all|host|storage"),
                    ),
                ),
                self._machine_facts_payload,
            )
        )
        self.tools.register(
            FunctionTool(
                "capabilities",
                ToolSchema("capabilities", "List what this installation can do."),
                self._capabilities_payload,
            )
        )
        self.tools.register(
            FunctionTool(
                "installed_applications",
                ToolSchema(
                    "installed_applications",
                    "List the applications installed on this machine.",
                    parameters=(
                        ToolParameter(
                            "filter", "string", description="substring to match"
                        ),
                    ),
                ),
                self._installed_applications_payload,
            )
        )

    def _machine_facts_payload(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Stable machine facts, from the reader the metric path already uses."""
        scope = str(arguments.get("scope") or "all").strip().lower()
        # ``_hardware_status`` is a property, deliberately: the reader is created
        # once so CPU deltas have a baseline, and calling it would make a second
        # reader with no baseline. Getting that wrong is how the first version of
        # this tool failed with "'HardwareTelemetry' object is not callable".
        reader = self._hardware_status
        host = reader.host()
        payload: dict[str, Any] = {
            "scope": scope,
            "platform": host.get("platform", ""),
            "platform_release": host.get("platform_release", ""),
            "cpu_model": host.get("cpu_model", ""),
            "cpu_count": host.get("cpu_count"),
            "python": host.get("python", ""),
        }
        if scope in ("all", "memory"):
            memory = reader.memory()
            # Totals only. Used and available bytes move between calls, and this
            # result may be reused for ten minutes.
            payload["total_memory_bytes"] = memory.get("total_bytes")
            payload["memory_available"] = memory.get("available", False)
        if scope in ("all", "storage"):
            storage = reader.storage()
            payload["disk_total_bytes"] = storage.get("total_bytes")
            payload["disk_available"] = storage.get("available", False)
        return payload

    def _capabilities_payload(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """The capability table, as the registry holds it."""
        del arguments
        entries = [
            {
                "capability": capability.capability,
                # A capability the routing table answers for has an intent; a
                # projected one (a tool, an action) has none, and "" is that
                # fact rather than an AttributeError.
                "intent": capability.intent.value if capability.intent is not None else "",
                "description": capability.description,
                "risk": capability.risk.value,
                "executor": capability.executor,
                "environments": list(capability.supported_environments),
            }
            for capability in self.intelligence.capabilities.all()
        ]
        return {"capabilities": entries, "count": len(entries)}

    def _installed_applications_payload(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """The installed applications, from the index used to open them by name.

        The same index the desktop controller resolves "open <app>" against, so
        this answers with what can actually be launched rather than with a second
        opinion about it. Display names, not the casefolded keys the resolver
        matches on: a Start Menu shortcut and a registry ``App Paths`` entry both
        carry the real name in the file stem, so presenting it invents nothing.
        """
        from novacontrol.desktop.controller import _app_index

        needle = str(arguments.get("filter") or "").strip().casefold()
        index = _app_index()
        names: list[str] = []
        seen: set[str] = set()
        for key in sorted(index):
            display = Path(index[key]).stem or key
            folded = display.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            if needle and needle not in folded:
                continue
            names.append(display)
        return {"applications": names, "count": len(names), "available": bool(index)}

    def discover_tools(self, query: str, *, limit: int = 3) -> list[dict[str, Any]]:
        """The tools that could carry this request, best first, with scores.

        The readout the specification's discovery stage exists for: a caller —
        a planner, a model escalation, a person reading the UI — gets a few
        candidates and the evidence for them instead of every tool definition.
        """
        with self.intelligence.telemetry.stage("tool_selection"):
            matches = self.tool_retriever.search(query, limit=limit)
        return [match.to_dict() for match in matches]

    def discovered_tools(self, query: str, *, limit: int = 3) -> tuple[dict[str, Any], ...]:
        """The same shortlist in the compact shape a prompt may carry."""
        names = self.tool_retriever.prompt_tools(query, limit=limit)
        return tool_descriptions(self.tool_catalog, names)

    def models_status(self) -> dict[str, Any]:
        """The model layer's readout, with NO runtime probe.

        Deliberately probe-free: this travels in every BrainRequest context (see
        :meth:`status`), so it reports what is already known — the capability
        table with each row's provenance, the lifecycle policy, and the
        selections and loads that have happened. The MEASURED figures (resident
        models, free RAM, GPU/NPU) live in :meth:`model_status` and
        ``/intelligence``, where the probe can run off the event loop.
        """
        manager = self.model_manager
        return {
            "provider": manager.provider_name(),
            "keep_alive": manager.keep_alive.to_dict(),
            "headroom_bytes": manager.headroom_bytes,
            "registry": manager.registry_report(),
            "telemetry": manager.telemetry.to_dict(),
            "active_tasks": list(manager.active_models()),
        }

    def tools_status(self) -> dict[str, Any]:
        """What the tool layer knows, what it found, and what it reused."""
        return {
            "catalog": {
                "tools": len(self.tool_catalog),
                "registered": len(self.tool_catalog.registered()),
                "categories": self.tool_catalog.categories(),
            },
            "discovery": self.tool_retriever.to_dict(),
            "cache": self.tool_executor.cache_report(),
        }

    def _confirm_plan_step(self, step: PlanStep) -> bool:
        """Is this step authorized? Only an explicit approval from the caller counts."""
        return step.id in self.approved_plan_steps

    async def _run_plan_step(self, step: PlanStep, context: StepContext) -> dict[str, Any]:
        """Carry out one plan step, or refuse to pretend it was carried out.

        The deterministic work this application can genuinely perform is handled
        here by action name. Everything else goes to the tool registry through
        the approval-gated executor; a step naming a tool this installation does
        not have RAISES, which the recovery advisor turns into an escalation,
        because a plan that quietly reports success for a step nobody ran is the
        exact failure this layer exists to prevent.
        """
        action = step.action
        if action.startswith("read_"):
            return self._read_metric_step(step)
        if action == "collect_output":
            return _collect_step_output(step, context)
        if action == "analyze_result":
            return _analyze_step_output(step, context)
        if action == "summarize_result":
            return _summarize_step_output(step, context)
        if action in ("run_command", "run_tests"):
            return await self._run_command_step(step, context)
        if action == "locate_project":
            return await self._locate_project_step(step)
        if action == "reason":
            raise RuntimeError(
                f"No executor carries reasoning locally, so step {step.id!r} "
                "needs escalation."
            )
        return await self._dispatch_plan_step(step)

    def _read_metric_step(self, step: PlanStep) -> dict[str, Any]:
        """A live reading from this machine — never from a model."""
        intent = str(step.parameters.get("metric") or "system_status")
        reader = self._hardware_status
        metric_name = _READER_FOR_INTENT.get(intent)
        names = (metric_name,) if metric_name else _STATUS_METRICS["system_status"]
        readings = {name: getattr(reader, name)() for name in names}
        kind = intent if intent in _STATUS_METRICS else "system_status"
        message = _status_message(kind, names, readings)
        return {"summary": message, "message": message, "metrics": readings, "kind": kind}

    async def _locate_project_step(self, step: PlanStep) -> dict[str, Any]:
        """Find a named project on disk, in bounded places, off the event loop."""
        project = str(step.parameters.get("project") or "").strip()
        if not project:
            raise RuntimeError("No project name was given, so there is nothing to locate.")
        found = await asyncio.to_thread(_find_project_directory, project)
        if found is None:
            raise RuntimeError(f"No directory matching {project!r} was found in the usual places.")
        return {"path": found, "summary": f"Found {project} at {found}."}

    async def _run_command_step(self, step: PlanStep, context: StepContext) -> dict[str, Any]:
        """Run a command a plan asked for — only with the caller's approval.

        A step that runs a command runs code nobody reviewed line by line, so it
        is gated exactly like this application's other privileged work: the
        caller names the step in ``run_plan(approved=...)`` or it is refused.
        The check lives HERE as well as in the executor order, because a step
        handler is the last place that can decline to start a process — with no
        approval there is nothing to explain away afterwards.
        """
        if not self._confirm_plan_step(step):
            raise PermissionError(
                f"{step.title} runs a command and needs explicit approval; "
                "approve this step to let it run."
            )
        located = str(context.output_of(("locate-project",)).get("path") or "").strip()
        if step.action == "run_tests":
            if not located or not Path(located).is_dir():
                raise RuntimeError("No project directory was located, so there is no suite to run.")
            runner = _test_argv(located, str(step.parameters.get("scope") or "tests"))
            if runner is None:
                raise RuntimeError(
                    f"No test runner I recognise was found in {located}, so nothing was run."
                )
            argv, label = runner
            cwd = located
            timeout = _TEST_TIMEOUT_SECONDS
        else:
            command = str(step.parameters.get("command") or "").strip()
            if not command:
                raise RuntimeError("No command text was given for this step.")
            argv = _argv_for_command(command)
            if not argv:
                raise RuntimeError("The command was empty once split into arguments.")
            label = command
            cwd = located if located and Path(located).is_dir() else str(Path.cwd())
            timeout = _COMMAND_TIMEOUT_SECONDS
        exit_code, stdout, stderr = await _run_approved_process(argv, cwd=cwd, timeout=timeout)
        return {
            "command": label,
            "argv": list(argv),
            "cwd": cwd,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "summary": f"'{label}' finished with exit code {exit_code}.",
        }

    async def _dispatch_plan_step(self, step: PlanStep) -> dict[str, Any]:
        """Send a step to its tool — through the executor that owns approvals."""
        if not step.tool:
            raise RuntimeError(
                f"No executor is registered for action {step.action!r}, so it cannot run."
            )
        try:
            self.tools.get(step.tool)
        except KeyError as exc:
            raise RuntimeError(
                f"Tool {step.tool!r} is not registered on this installation."
            ) from exc
        with self.intelligence.telemetry.stage("tool_execution"):
            result = await self.tool_executor.execute(
                ToolRequest(
                    tool_name=step.tool,
                    arguments=dict(step.parameters),
                    reason=step.description,
                )
            )
        if result.status is ToolStatus.DENIED:
            raise PermissionError(result.error or f"Tool {step.tool!r} was not approved.")
        if result.status is ToolStatus.FAILED:
            raise RuntimeError(result.error or f"Tool {step.tool!r} failed.")
        return dict(result.output)

    def _selection_for(
        self, request: BrainRequest, understanding: dict[str, Any]
    ) -> dict[str, Any]:
        """The tool selection for a plan, rebuilt from the structured reading.

        Rebuilt rather than carried as an object because the handler receives a
        payload: a selection that travelled as a dict is validated back into the
        structured intent, so a plan path can never act on a shape it merely
        assumed. A reading this handler cannot reconstruct yields an empty
        selection, which is reported rather than guessed.
        """
        intent = understanding.get("intent")
        resolved = resolve_intent(str(intent)) if intent else None
        if resolved is None:
            return {}
        selection = self.tool_selector.select(
            StructuredIntent(
                raw_input=request.text,
                normalized_input=str(understanding.get("normalized_input", "")),
                intent=resolved,
                action=str(understanding.get("action", "")),
                entities=dict(understanding.get("entities", {}) or {}),
                confidence=float(understanding.get("confidence", 0.0) or 0.0),
                requires_confirmation=bool(understanding.get("requires_confirmation", False)),
            )
        )
        return selection.to_dict()

    async def _handle_self_improvement(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        return "self_improvement", self.self_improvement.plan(text).to_dict()

    async def _handle_desktop(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        """Plan what the reading RESOLVED, never just the words it read.

        The understanding layer resolves "open it" to the application it stands
        for; the desktop parser cannot, so planning the raw words launched a
        program called "it". The resolved target is restated as the command the
        parser already accepts (see ``_command_with_resolved_references``).
        """
        return "desktop_automation", self.plan_desktop_command(
            _command_with_resolved_references(request.context.get("nlu"), text)
        )

    async def _handle_phone(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        return "phone_control", self.plan_phone_command(text)

    async def _handle_browser(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        """Plan the resolved browser command, for the same reason as desktop."""
        return "browser_automation", self.plan_browser_command(
            _command_with_resolved_references(request.context.get("nlu"), text)
        )

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
        named: tuple[str, ...] = ()
        if isinstance(understanding, dict):
            kind = str(understanding.get("intent", ""))
            # Every metric the READING said was asked for ("check my RAM and
            # CPU" names two), so the answer covers the request instead of the
            # first half of it. Whitelisted against the metric vocabulary, which
            # is also the reader's own attribute set, so nothing else can be
            # read from a payload.
            parameters = understanding.get("parameters")
            if isinstance(parameters, dict):
                named = tuple(
                    str(item) for item in (parameters.get("metrics") or ()) if str(item)
                )
        metric_names = tuple(
            name for name in named if name in _METRIC_KINDS
        ) or _STATUS_METRICS.get(kind, _STATUS_METRICS["system_status"])
        reader = self._hardware_status
        metrics = {name: getattr(reader, name)() for name in metric_names}
        message = _status_message(kind, metric_names, metrics)
        return "system", {
            "deterministic": True,
            "message": message,
            "summary": message,
            "metrics": metrics,
            "measured_at": time.time(),
        }

    async def _handle_vision(self, request: BrainRequest, text: str) -> tuple[str, dict[str, Any]]:
        """Capture the screen and interpret it THROUGH THE VISION MANAGER.

        The order is the one Phase 6 exists for: capture, then READ (OCR, cheap
        and deterministic), then — only when the text cannot answer the question
        — the vision provider (a VLM configured in the Vision panel). The text
        chat model is never handed an image; a machine with no vision model
        still answers text questions and says so when it cannot.

        The user's own question travels with the capture: "describe the image on
        screen" and "why isn't the button working?" are the same pixels and
        different work, and a model handed the checklist alone answers the first
        one either way.

        A picture that came WITH the request is read instead of the screen: an
        attached image is what the question is about, and capturing the desktop
        would answer about the wrong pixels when the user attached the one they
        meant.
        """
        attached = str(request.context.get("image", "") or "").strip()
        captured = (
            {"screenshot": attached, "captured": True, "vision_model": True}
            if attached
            else await self.vision.capture_screen()
        )
        payload: dict[str, Any] = dict(captured)
        if not payload.get("captured"):
            payload.setdefault("summary", str(payload.get("message", "")))
            payload.setdefault("question_answered", False)
            await self._announce(
                EventType.VISION_COMPLETED.value,
                image=attached or "screen",
                answered=False,
                reason=str(payload.get("message", "")),
            )
            return "vision", payload
        question = self._vision_question(request)
        # Announced once the question is known and the capture is real: an event
        # that says "vision started" before there is anything to look at would
        # be a statement about an intention rather than about work.
        # Phase 7: the RAM-aware load runs BEFORE the VLM is asked to look. An
        # automatic request used to reach the runtime with the chat model still
        # resident — the conflict this layer exists to resolve — because only the
        # explicit load endpoint went through the manager. The acquisition is a
        # no-op when the model is already there, and a REFUSAL is a measurement
        # rather than a hiccup: the cheap path answers from the screen's text and
        # says the model was not loaded, instead of paging the machine to run a
        # model that does not fit.
        await self._announce(
            EventType.VISION_STARTED.value,
            image=str(payload["screenshot"]),
            question=question,
            attached=bool(attached),
        )
        model = self._vision_pipeline_model()
        outcome = await self._acquire_vision_model(model)
        allow_vlm = not (outcome is not None and outcome.refused)
        if outcome is not None:
            payload["model_lifecycle"] = outcome.to_dict()
        try:
            with self.intelligence.telemetry.stage("vision"):
                result = await self.analyze_image(
                    str(payload["screenshot"]), question=question, allow_vlm=allow_vlm
                )
        finally:
            if outcome is not None:
                await self._release_model(model)
            self._record_model_timings("vision", self._vision_provider_source())
        # The structured result IS the payload: image_type, detected_text,
        # ui_elements, errors, relevant_regions, summary, confidence — the shape
        # the planner and the UI read, rather than prose each re-parses.
        payload.update(result.to_dict())
        # The honest bit travels at the top level as well as inside metadata: a
        # client should not have to dig to see that nothing answered the
        # question, and the refusal case is exactly when that matters.
        payload["answered"] = result.answered
        payload["question_answered"] = bool(result.metadata.get("question_answered"))
        payload["vision_model"] = self.vision_manager.provider.available
        # `message` is what a person reads, and it is built from the result's OWN
        # provenance so it can never claim more than was actually determined.
        payload["message"] = self._vision_message(
            result,
            refused=outcome.reason if outcome is not None and outcome.refused else "",
        )
        await self._announce(
            EventType.VISION_COMPLETED.value,
            image=str(payload["screenshot"]),
            answered=bool(result.answered),
            image_type=str(payload.get("image_type", "")),
            model=str(getattr(outcome, "model", "") or ""),
        )
        return "vision", payload

    def reliability_status(self) -> dict[str, Any]:
        """What Phase 8 can verify, recover, control and refuse — as one readout.

        Reported rather than inferred: a caller (a UI, a doctor command) can see
        which checks this build has, how many retries it will spend, which
        tasks a control command would act on, and which risk policy is in
        force, instead of discovering them by watching something fail.
        """
        return {
            "verification": self.plan_verifier.report(),
            "recovery": {
                "max_attempts": self.recovery_engine.retry_policy.max_attempts,
                "alternatives": list(self.recovery_engine.alternatives()),
                "confirmation_available": self.recovery_engine.confirmation_available,
                "escalation_available": self.recovery_engine.escalation_available,
            },
            "tasks": [snapshot.to_dict() for snapshot in self.task_control.tasks()],
            "risk": self.risk.to_dict(),
        }

    # ──────────────────────────────────────────────────────────────────────
    # Phase 9: what this installation can do, declared by the layer that does it
    # ──────────────────────────────────────────────────────────────────────

    def _declare_actions(self) -> None:
        """Declare the plan steps THIS application carries out, once, next to
        the code that carries them out.

        ``_run_plan_step`` is the executor for these actions, so the application
        is the layer that knows them; a registry told about them by anyone else
        would be second-hand knowledge. Steps that dispatch to a TOOL are
        deliberately absent — the tool catalogue already declares them, and two
        tables for one fact is the duplication this registry exists to remove.
        """
        declare = self.capabilities.register_action
        declare(
            "read_metric",
            capability_id="system.read_metric",
            intent=IntentName.SYSTEM_INFO,
            description="Read a live hardware reading from this machine, never from a model.",
            inputs=("metric",),
            outputs=("metrics", "message"),
            tags=(
                "read", "metric", "ram", "memory", "cpu", "gpu", "disk",
                "battery", "temperature", "status",
            ),
            examples=(
                "How much RAM is free?",
                "What is my CPU usage?",
                "What's the battery level?",
            ),
        )
        declare(
            "locate_project",
            capability_id="developer.locate_project",
            description="Find a project directory on this machine, in bounded places.",
            inputs=("project",),
            outputs=("path",),
            tags=("find", "locate", "project", "directory", "folder", "repo", "workspace"),
            examples=("Find my novacontrol project.", "Where is the reports folder?"),
        )
        declare(
            "run_tests",
            capability_id="developer.run_tests",
            intent=IntentName.RUN_COMMAND,
            description="Run a located project's test suite and capture its exit code.",
            risk=RiskLevel.MEDIUM,
            permissions=("process.execute",),
            inputs=("scope",),
            outputs=("exit_code", "stdout", "stderr"),
            tags=("test", "tests", "suite", "pytest", "run", "check", "failing"),
            examples=("Run the test suite.", "Run my project's tests."),
        )
        declare(
            "run_command",
            capability_id="developer.run_command",
            intent=IntentName.RUN_COMMAND,
            description="Run a shell command the caller approved and capture its exit code.",
            risk=RiskLevel.MEDIUM,
            permissions=("process.execute",),
            inputs=("command",),
            outputs=("exit_code", "stdout", "stderr"),
            tags=("run", "command", "shell", "terminal", "tests", "build", "install"),
            examples=("Run the test suite.", "Run pip install -r requirements.txt."),
        )
        declare(
            "collect_output",
            capability_id="developer.collect_output",
            description="Capture the output of an upstream step once, and name it.",
            inputs=("depends_on",),
            outputs=("captured", "exit_code"),
            tags=("collect", "capture", "output", "log", "logs", "stdout"),
            examples=("Collect the output of the test run.",),
        )
        declare(
            "analyze_result",
            capability_id="developer.analyze_result",
            description="Read captured output and name what failed, deterministically.",
            inputs=("depends_on",),
            outputs=("failures", "summary"),
            tags=(
                "analyze", "analyse", "diagnose", "failure", "failures", "error",
                "errors", "log", "logs", "traceback", "why",
            ),
            examples=("Analyse the test output.", "Why did the suite fail?"),
        )
        declare(
            "summarize_result",
            capability_id="developer.summarize_result",
            intent=IntentName.SUMMARIZE,
            description="Summarise what a plan's steps found, in the order they ran.",
            inputs=("depends_on",),
            outputs=("summary",),
            tags=("summarize", "summarise", "report", "explain", "tell", "result"),
            examples=("Tell me what the run found.", "Summarise the diagnostics."),
        )
        # A REAL gap, declared as one: a plan step that asks to reason is refused
        # by this executor (there is no local reasoning engine), so the registry
        # says so rather than advertising a capability that would fail.
        declare(
            "reason",
            capability_id="system.reason",
            description="Reason about a goal with no executor to carry it out.",
            availability=CapabilityAvailability.UNAVAILABLE,
            availability_reason=(
                "no executor carries reasoning locally; a step that asks to reason "
                "is escalated to a model instead"
            ),
            tags=("reason", "think", "infer"),
        )

    def _model_available(self, role: str) -> bool:
        """Whether a model that can serve ``role`` is usable right now.

        Answered by the layers that already decide this rather than from a list
        kept here: "vision" is what the vision pipeline's provider says it can
        carry, and every other role is a model DECLARING that capability in the
        registry the selection layer routes on, or a configured cloud provider —
        the same two ways a request actually gets an answer. A role nobody
        recognises, or a probe that raises, is not claimed to be available.
        """
        wanted = str(role or "").strip().lower()
        if not wanted:
            return True
        if wanted == "vision":
            try:
                return bool(self.vision_manager.status()["provider"]["available"])
            except Exception:  # pragma: no cover - a probe must never block a query
                return False
        capability = _MODEL_ROLE_CAPABILITIES.get(wanted)
        if capability is None:
            return False
        try:
            declared = any(
                capability in profile.capabilities
                for profile in self.model_manager.registry.all()
            )
        except Exception:  # pragma: no cover - a probe must never block a query
            declared = False
        return declared or bool(self.brain.cloud_provider_name)

    # ──────────────────────────────────────────────────────────────────────
    # Phase 9.1: the lifecycle vocabulary, published from the live path
    # ──────────────────────────────────────────────────────────────────────

    async def _announce(
        self,
        type_: EventType | str,
        /,
        *,
        correlation_id: str = "",
        **payload: Any,
    ) -> None:
        """Announce a lifecycle event, never failing the work it describes.

        ``emit`` already refuses to raise because a SUBSCRIBER failed; a payload
        that does not match its event's declared fields still raises, because
        that is a bug in this publisher rather than in a watcher. With no
        explicit correlation the event inherits the request being served, so
        depth costs nothing and no publisher has to thread an id around.
        """
        await self.event_bus.emit(
            type_,
            source="application",
            correlation_id=correlation_id or _CURRENT_REQUEST.get(),
            **payload,
        )

    def _announce_soon(
        self, type_: EventType | str, /, *, correlation_id: str = "", **payload: Any
    ) -> None:
        """Announce from a SYNCHRONOUS seam — a state transition, a planner.

        The payload is validated here, at the publisher, so a missing field
        raises where the mistake is instead of inside a task nobody is awaiting;
        the publish itself is scheduled when a loop is running and is a no-op
        otherwise, exactly like ``_bus_schedule``.

        ``source``, ``correlation_id`` and ``causation_id`` are RESERVED (the bus
        sets them for every event), so a payload field may not use those names —
        use a name that says what the value is (``selection_source``,
        ``image``) rather than passing something that cannot travel.
        """
        event = Event.of(
            type_,
            source="application",
            correlation_id=correlation_id or _CURRENT_REQUEST.get(),
            **payload,
        )
        self._bus_schedule(event)

    def _task_transition(self, snapshot: TaskSnapshot, previous: TaskState) -> None:
        """Publish a task's transition (the state machine's own observer).

        The state table names the event, with one reading the table cannot make:
        RUNNING entered out of PAUSED is a RESUME, not a start — publishing
        "started" for a task somebody had paused would be the kind of small lie
        a status channel must not tell.
        """
        state = str(snapshot.current_state)
        type_ = task_event_name(state)
        if type_ is None:
            return
        if type_ is EventType.TASK_STARTED and str(previous) == TaskState.PAUSED.value:
            type_ = EventType.TASK_RESUMED
        self._announce_soon(
            type_,
            task_id=snapshot.task_id,
            state=state,
            previous=str(previous),
            step=snapshot.current_step,
            parent_task_id=snapshot.parent_task_id,
        )

    async def _tool_event(self, type_: str, payload: Mapping[str, Any]) -> None:
        """The tool executor's sink: one event per tool call, as it happens."""
        await self._announce(type_, **dict(payload))

    def _tool_selected(self, selection: Any) -> None:
        """The selector's observer: which tool this request would reach, and why.

        Synchronous because a selection is: this announces the answer without
        making the selector wait for anyone to hear it.

        The selection's provenance is published as ``selection_source``, NOT
        ``source``: every event already carries a ``source`` (the publisher's
        own name) and a payload field that shadows it cannot be passed to
        ``Event.of`` at all.
        """
        self._announce_soon(
            EventType.TOOL_SELECTED,
            tool=str(getattr(selection, "tool", "")),
            capability=str(getattr(selection, "capability", "")),
            executor=str(getattr(selection, "executor", "")),
            selection_source=getattr(getattr(selection, "source", None), "value", ""),
            confidence=float(getattr(selection, "confidence", 0.0) or 0.0),
            availability=str(getattr(selection, "availability", "")),
            degraded=bool(getattr(selection, "degraded", False)),
            requires_confirmation=bool(getattr(selection, "requires_confirmation", False)),
        )

    async def _step_announcement(self, type_: str, payload: Mapping[str, Any]) -> None:
        """The plan executor's announcer: checks and recoveries, as they happen."""
        await self._announce(type_, **dict(payload))

    def _vision_provider_source(self) -> object | None:
        """The completion provider behind the vision pipeline's provider, if any.

        The pipeline wraps whatever can complete a multimodal message (see
        ``vision/providers.py``), and the wrapper is the only object that knows
        what it wrapped. The model's timing breakdown lives on the INNER provider,
        so it is read from there rather than assumed to be the chat brain's.
        """
        return getattr(self.vision_manager.provider, "source", None)

    def _vision_message(self, result: VisionResult, *, refused: str = "") -> str:
        """One honest line about what was determined, and by what.

        ``refused`` is the resource decision, when there was one: a machine that
        could not fit the vision model must not present its OCR answer as though
        no model was ever needed.
        """
        provenance = dict(result.metadata)
        answered = bool(provenance.get("question_answered"))
        if not result.answered:
            reason = str(provenance.get("reason", "no answer"))
            if refused:
                reason = f"{reason}; the vision model was not loaded — {refused}"
            return f"{result.summary} ({reason})"
        if provenance.get("escalated"):
            return f"Vision model report: {result.summary}"
        if answered and result.metadata.get("question"):
            return f"Answered from the screen's text: {result.summary}"
        return result.summary or "Screen captured, but nothing could be read from it."

    @staticmethod
    def _vision_question(request: BrainRequest) -> str:
        """What the user asked about the picture, in their own words.

        The NLU's goal when it read one (it is already the cleaned-up request),
        the raw text otherwise — but never for a bare "look at this" phrasing,
        where the words are the instruction rather than a question about the
        screen and would only confuse the model.
        """
        understanding = request.context.get("nlu")
        if isinstance(understanding, dict):
            goal = str(understanding.get("goal", "") or "").strip()
            if goal:
                return goal
        text = " ".join(request.text.split())
        return "" if _VISION_BARE_LOOK.search(text.lower()) else text

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
            "tools": self.tools_status(),
            # Phase 7: which models this build knows, what each one can do and
            # where that claim came from, the lifecycle policy in force, and how
            # many selections and loads have gone each way.
            "models": self.models_status(),
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
            # What the vision pipeline would do right now: which reader would
            # answer, which model (if any) would be consulted, and whether the
            # cheap OCR path is tried first.
            "vision_pipeline": dict(self.vision_manager.status()),
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
                # Which decision provider decides, and which one answers when it
                # declines — a configured provider that silently stopped being
                # used is visible here rather than inferred from behaviour.
                "decision": self.decision.status(),
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
