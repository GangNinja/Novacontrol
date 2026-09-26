"""Default capability registrations: what NovaControl can do, by intent.

Every entry declares the contract the orchestrator reasons over — required and
optional entities, risk, executor, and how success is verified — so planning
and confirmation policy are derived from this registry instead of hard-coded
per call site. ONE source of truth shared by JARVIS, research, automation,
browser, desktop, phone, and future agents.

Phase 9.2 added the metadata that makes a capability *discoverable* — a dotted
id, tags and examples a task can match on, the models it needs, its inputs and
outputs, its permissions. That metadata lives in :data:`_CAPABILITY_METADATA`
and is merged in by :func:`default_capabilities`, so the entries below stay
readable and nothing has to be restated: a capability that declares no tools
inherits the ones its intent names in the catalog, and one with no category
inherits the namespace of the subsystem that implements it.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from novacontrol.intelligence.intent import Capability, IntentName, RiskLevel


def _declared_capabilities() -> tuple[Capability, ...]:
    """Capabilities derived from the intent rules — the executor and verifier
    names mirror the subsystems that already implement each intent."""
    return (
        # ── Device control (phone) ──────────────────────────────────────
        Capability(
            capability="phone_connect", intent=IntentName.PHONE_CONNECT,
            description="Pair a phone bridge (adb over USB).",
            required=(), optional=("device",), risk=RiskLevel.LOW,
            executor="phone_controller", verifier="bridge_status_verifier",
            supported_environments=("phone",),
        ),
        Capability(
            capability="phone_status", intent=IntentName.PHONE_STATUS,
            description="Report phone bridge and device state.",
            risk=RiskLevel.LOW, executor="phone_controller",
            verifier="bridge_status_verifier", supported_environments=("phone",),
        ),
        Capability(
            capability="phone_send_text", intent=IntentName.PHONE_SEND_TEXT,
            description="Send a text message to a contact via the paired phone.",
            required=("contact",), optional=("message",), risk=RiskLevel.MEDIUM,
            executor="phone_controller", verifier="phone_state_verifier",
            supported_environments=("phone",),
        ),
        Capability(
            capability="phone_call", intent=IntentName.PHONE_CALL,
            description="Start a phone call to a contact.",
            required=("contact",), risk=RiskLevel.MEDIUM,
            executor="phone_controller", verifier="phone_state_verifier",
            supported_environments=("phone",),
        ),
        Capability(
            capability="phone_screenshot", intent=IntentName.PHONE_SCREENSHOT,
            description="Capture the paired phone's screen.",
            risk=RiskLevel.LOW, executor="phone_controller",
            verifier="file_exists_verifier", supported_environments=("phone",),
        ),
        Capability(
            capability="phone_open_app", intent=IntentName.PHONE_OPEN_APP,
            description="Open an application on the paired phone.",
            required=("application",), risk=RiskLevel.LOW,
            executor="phone_controller", verifier="phone_state_verifier",
            supported_environments=("phone",),
        ),
        # ── Desktop ─────────────────────────────────────────────────────
        Capability(
            capability="open_application", intent=IntentName.OPEN_APPLICATION,
            description="Open an application on this computer.",
            required=("application",), risk=RiskLevel.LOW,
            executor="desktop_controller", verifier="application_state_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="close_application", intent=IntentName.CLOSE_APPLICATION,
            description="Close an application window gracefully.",
            required=("application",), risk=RiskLevel.MEDIUM,
            executor="desktop_controller", verifier="application_state_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="open_folder", intent=IntentName.OPEN_FOLDER,
            description="Open a file-system folder in the file manager.",
            required=("folder",), risk=RiskLevel.LOW,
            executor="desktop_controller", verifier="application_state_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="take_screenshot", intent=IntentName.TAKE_SCREENSHOT,
            description="Capture the screen to a saved image.",
            risk=RiskLevel.LOW, executor="desktop_controller",
            verifier="file_exists_verifier", supported_environments=("desktop",),
        ),
        Capability(
            capability="type_text", intent=IntentName.TYPE_TEXT,
            description="Type dictated text into the focused application.",
            required=("text",), risk=RiskLevel.LOW,
            executor="desktop_controller", verifier="clipboard_or_focus_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="press_key", intent=IntentName.PRESS_KEY,
            description="Press a key or key combination.",
            required=("key",), risk=RiskLevel.MEDIUM,
            executor="desktop_controller", verifier="application_state_verifier",
            supported_environments=("desktop",),
        ),
        # ── Browser ─────────────────────────────────────────────────────
        Capability(
            capability="navigate", intent=IntentName.NAVIGATE,
            description="Navigate the browser to a URL or named site.",
            required=("url",), risk=RiskLevel.LOW,
            executor="browser_controller", verifier="page_state_verifier",
            supported_environments=("browser",),
        ),
        Capability(
            capability="search_web", intent=IntentName.SEARCH_WEB,
            description="Search the web in the real browser.",
            required=("query",), risk=RiskLevel.LOW,
            executor="browser_controller", verifier="page_state_verifier",
            supported_environments=("browser",),
        ),
        Capability(
            capability="extract_page", intent=IntentName.EXTRACT_PAGE,
            description="Extract structured content from the current page.",
            required=("target",), risk=RiskLevel.LOW,
            executor="browser_controller", verifier="content_verifier",
            supported_environments=("browser",),
        ),
        Capability(
            capability="fill_form", intent=IntentName.FILL_FORM,
            description="Fill a web form's fields.",
            required=("target",), optional=("fields",), risk=RiskLevel.MEDIUM,
            executor="browser_controller", verifier="page_state_verifier",
            supported_environments=("browser",),
        ),
        # ── Knowledge / research ────────────────────────────────────────
        Capability(
            capability="research", intent=IntentName.RESEARCH,
            description="Research a topic online and synthesize a sourced report.",
            required=("topic",), risk=RiskLevel.LOW,
            executor="explore_service", verifier="report_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="answer_question", intent=IntentName.ANSWER_QUESTION,
            description="Answer conversationally from the chat model or scratch brain.",
            required=("question",), risk=RiskLevel.LOW,
            executor="chat_brain", verifier="response_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="summarize", intent=IntentName.SUMMARIZE,
            description="Summarize a topic or document.",
            required=("topic",), risk=RiskLevel.LOW,
            executor="explore_service", verifier="report_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="compare", intent=IntentName.COMPARE,
            description="Compare options and recommend one.",
            required=("topic",), risk=RiskLevel.LOW,
            executor="explore_service", verifier="report_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="generate_report", intent=IntentName.GENERATE_REPORT,
            description="Generate and save a report on a topic.",
            required=("topic",), risk=RiskLevel.LOW,
            executor="explore_service", verifier="report_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="chat", intent=IntentName.CHAT,
            description="Open-ended conversation with the brain.",
            required=("message",), risk=RiskLevel.LOW,
            executor="chat_brain", verifier="response_verifier",
            supported_environments=("desktop",),
        ),
        # ── Planning / automation ───────────────────────────────────────
        Capability(
            capability="plan_task", intent=IntentName.PLAN_TASK,
            description="Break a goal into an ordered plan.",
            required=("goal",), risk=RiskLevel.LOW,
            executor="planning_engine", verifier="plan_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="create_automation", intent=IntentName.CREATE_AUTOMATION,
            description="Create a saved automation workflow.",
            required=("name", "steps"), risk=RiskLevel.MEDIUM,
            executor="automation_manager", verifier="workflow_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="run_automation", intent=IntentName.RUN_AUTOMATION,
            description="Run a saved automation workflow.",
            required=("name",), risk=RiskLevel.MEDIUM,
            executor="automation_manager", verifier="workflow_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="schedule_task", intent=IntentName.SCHEDULE_TASK,
            description="Schedule a task for a future time.",
            required=("when", "goal"), risk=RiskLevel.LOW,
            executor="scheduler", verifier="schedule_verifier",
            supported_environments=("desktop",),
        ),
        # ── Memory / project / improvement ──────────────────────────────
        Capability(
            capability="remember", intent=IntentName.REMEMBER,
            description="Store a fact in long-term memory.",
            required=("fact",), risk=RiskLevel.LOW,
            executor="memory_manager", verifier="memory_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="recall", intent=IntentName.RECALL,
            description="Recall facts from memory.",
            required=("query",), risk=RiskLevel.LOW,
            executor="memory_manager", verifier="memory_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="create_project", intent=IntentName.CREATE_PROJECT,
            description="Create a managed project.",
            required=("name",), risk=RiskLevel.LOW,
            executor="project_manager", verifier="project_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="improve_self", intent=IntentName.IMPROVE_SELF,
            description="Produce a sandboxed self-improvement plan.",
            required=("goal",), risk=RiskLevel.MEDIUM,
            executor="self_improvement_engine", verifier="preview_gate_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="agentic_task", intent=IntentName.AGENTIC_TASK,
            description="Delegate a task to a specialized agent.",
            required=("task",), risk=RiskLevel.MEDIUM,
            executor="agent_coordinator", verifier="response_verifier",
            supported_environments=("desktop",),
        ),
        # ── System status: measured, never guessed ──────────────────────
        # These carry `system_monitor` because they are read from the live
        # telemetry layer. A language model has no access to this machine's
        # memory, so asking one would be slower AND less accurate than reading
        # the same numbers the Command Center already displays.
        Capability(
            capability="memory_status", intent=IntentName.MEMORY_STATUS,
            description="Report live memory usage and the processes using it.",
            risk=RiskLevel.LOW, executor="system_monitor", verifier="telemetry_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="cpu_status", intent=IntentName.CPU_STATUS,
            description="Report live CPU load and temperature.",
            risk=RiskLevel.LOW, executor="system_monitor", verifier="telemetry_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="gpu_status", intent=IntentName.GPU_STATUS,
            description="Report GPU and video-memory state when a probe is available.",
            risk=RiskLevel.LOW, executor="system_monitor", verifier="telemetry_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="battery_status", intent=IntentName.BATTERY_STATUS,
            description="Report battery charge and power source.",
            risk=RiskLevel.LOW, executor="system_monitor", verifier="telemetry_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="network_status", intent=IntentName.NETWORK_STATUS,
            description="Report network connectivity and throughput.",
            risk=RiskLevel.LOW, executor="system_monitor", verifier="telemetry_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="system_status", intent=IntentName.SYSTEM_STATUS,
            description="Report a combined system health snapshot.",
            risk=RiskLevel.LOW, executor="system_monitor", verifier="telemetry_verifier",
            supported_environments=("desktop",),
        ),
        # ── Device controls ─────────────────────────────────────────────
        Capability(
            capability="volume_control", intent=IntentName.VOLUME_CONTROL,
            description="Change the system output volume or mute it.",
            optional=("level",), risk=RiskLevel.LOW,
            executor="desktop_controller", verifier="application_state_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="brightness_control", intent=IntentName.BRIGHTNESS_CONTROL,
            description="Change the display brightness.",
            optional=("level",), risk=RiskLevel.LOW,
            executor="desktop_controller", verifier="application_state_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="media_control", intent=IntentName.MEDIA_CONTROL,
            description="Play, pause, skip, or stop the current media.",
            risk=RiskLevel.LOW, executor="desktop_controller",
            verifier="application_state_verifier",
            supported_environments=("desktop",),
        ),
        # ── Vision (a dedicated VLM, never the text chat model) ──────────
        Capability(
            capability="screenshot_analysis", intent=IntentName.SCREENSHOT_ANALYSIS,
            description="Interpret a screenshot or image through the vision pipeline.",
            risk=RiskLevel.LOW, executor="vision_pipeline", verifier="content_verifier",
            supported_environments=("desktop",),
        ),
        # ── Filesystem operations ───────────────────────────────────────
        Capability(
            capability="find_file", intent=IntentName.FIND_FILE,
            description="Locate a file on this machine.",
            required=("file",), risk=RiskLevel.LOW,
            executor="file_manager", verifier="file_exists_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="modify_file", intent=IntentName.MODIFY_FILE,
            description="Change the contents of an existing file.",
            required=("file",), risk=RiskLevel.MEDIUM,
            executor="file_manager", verifier="file_content_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="delete_file", intent=IntentName.DELETE_FILE,
            description="Delete a file.",
            required=("file",), risk=RiskLevel.HIGH,
            executor="file_manager", verifier="file_absent_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="move_file", intent=IntentName.MOVE_FILE,
            description="Move a file to another location.",
            required=("file",), optional=("folder",), risk=RiskLevel.MEDIUM,
            executor="file_manager", verifier="file_exists_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="copy_file", intent=IntentName.COPY_FILE,
            description="Copy or duplicate a file.",
            required=("file",), risk=RiskLevel.MEDIUM,
            executor="file_manager", verifier="file_exists_verifier",
            supported_environments=("desktop",),
        ),
        # ── Composite browser work ──────────────────────────────────────
        Capability(
            capability="browser_action", intent=IntentName.BROWSER_ACTION,
            description="Open a browser and carry out a search or navigation in one request.",
            required=("query",), optional=("application", "website"), risk=RiskLevel.LOW,
            executor="browser_controller", verifier="page_state_verifier",
            supported_environments=("browser",),
        ),
        # ── Code / project work ─────────────────────────────────────────
        Capability(
            capability="code_generation", intent=IntentName.CODE_GENERATION,
            description="Write new code for a described goal.",
            required=("goal",), risk=RiskLevel.MEDIUM,
            executor="code_agent", verifier="response_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="code_explanation", intent=IntentName.CODE_EXPLANATION,
            description="Explain what existing code does.",
            required=("goal",), risk=RiskLevel.LOW,
            executor="code_agent", verifier="response_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="code_debugging", intent=IntentName.CODE_DEBUGGING,
            description="Diagnose a failure and propose a fix.",
            required=("goal",), risk=RiskLevel.LOW,
            executor="code_agent", verifier="response_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="project_analysis", intent=IntentName.PROJECT_ANALYSIS,
            description="Review a project or codebase and report its shape.",
            required=("goal",), risk=RiskLevel.LOW,
            executor="code_agent", verifier="report_verifier",
            supported_environments=("desktop",),
        ),
        # ── Language-only ───────────────────────────────────────────────
        Capability(
            capability="calculate", intent=IntentName.CALCULATE,
            description="Evaluate an arithmetic expression locally.",
            required=("expression",), risk=RiskLevel.LOW,
            executor="scratch_brain", verifier="math_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="general_question", intent=IntentName.GENERAL_QUESTION,
            description="Answer a general question in conversation.",
            risk=RiskLevel.LOW, executor="chat_brain", verifier="response_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="conversation", intent=IntentName.CONVERSATION,
            description="Reply to social or conversational input.",
            risk=RiskLevel.LOW, executor="chat_brain", verifier="response_verifier",
            supported_environments=("desktop",),
        ),
        # ── Filesystem verbs the rules already understand but nobody had ──
        # named here: reading, writing and listing a file were reachable by
        # intent and had no capability entry, so "what can you do with files?"
        # had no honest answer. Risk follows the specification's table: reading
        # is LOW, creating is LOW, and only modifying/deleting is above it.
        Capability(
            capability="read_file", intent=IntentName.READ_FILE,
            description="Read a file and show what is in it.",
            required=("file",), risk=RiskLevel.LOW,
            executor="file_manager", verifier="file_content_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="write_file", intent=IntentName.WRITE_FILE,
            description="Create or overwrite a file with given content.",
            required=("file",), optional=("content",), risk=RiskLevel.LOW,
            executor="file_manager", verifier="file_exists_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="list_files", intent=IntentName.LIST_FILES,
            description="List the files in a folder.",
            optional=("folder",), risk=RiskLevel.LOW,
            executor="file_manager", verifier="file_exists_verifier",
            supported_environments=("desktop",),
        ),
        Capability(
            capability="system_info", intent=IntentName.SYSTEM_INFO,
            description="Report this machine's specifications.",
            risk=RiskLevel.LOW, executor="system_monitor", verifier="telemetry_verifier",
            supported_environments=("desktop",),
        ),
    )


#: Phase 9.2: what a capability declares about itself for discovery, keyed by
#: capability name. Everything left out is DERIVED — the id from the executor's
#: namespace, the tools and examples from the intent catalog — so this is an
#: enrichment table, never a second registry that could drift out of step.
#:
#: ``tags`` are the words a task uses, not the words a capability uses: someone
#: asking about a *failing project* does not say "project_analysis".
_CAPABILITY_METADATA: dict[str, dict[str, Any]] = {
    # -- filesystem ---------------------------------------------------------
    "read_file": {
        "capability_id": "filesystem.read",
        "tags": ("read", "file", "files", "log", "logs", "content", "contents", "report"),
        "examples": (
            "Read report.pdf.",
            "Show me what is in main.py.",
            "What does the build log say?",
        ),
        "outputs": ("text",),
    },
    "write_file": {
        "capability_id": "filesystem.write",
        "tags": ("write", "create", "save", "file", "output", "note", "notes", "report"),
        "examples": ("Write this to report.txt.", "Create a notes file.", "Save the summary."),
        "inputs": ("file", "content"),
        "outputs": ("path",),
    },
    "list_files": {
        "capability_id": "filesystem.list",
        "tags": ("list", "files", "folder", "directory", "contents"),
        "examples": ("List the files in my Downloads folder.", "What is in this folder?"),
        "outputs": ("files",),
    },
    "find_file": {
        "capability_id": "filesystem.find",
        "tags": ("find", "locate", "file", "folder", "where", "project"),
        "examples": ("Find my NovaControl project.", "Where is report.pdf?"),
        "outputs": ("path",),
    },
    "modify_file": {
        "capability_id": "filesystem.modify",
        "tags": ("edit", "modify", "change", "patch", "source", "code", "file"),
        "examples": ("Update the config file.", "Change the version in pyproject.toml."),
        "outputs": ("path",),
    },
    "delete_file": {
        "capability_id": "filesystem.delete",
        "tags": ("delete", "remove", "file", "trash"),
        "examples": ("Delete the old build folder.",),
    },
    "move_file": {
        "capability_id": "filesystem.move",
        "tags": ("move", "file", "folder", "rename", "into"),
        "examples": ("Move report.pdf into Documents.",),
    },
    "copy_file": {
        "capability_id": "filesystem.copy",
        "tags": ("copy", "duplicate", "file", "backup"),
        "examples": ("Copy the report into my backup folder.",),
    },
    # -- browser ------------------------------------------------------------
    "navigate": {
        "capability_id": "browser.open_url",
        "tags": ("open", "url", "website", "site", "page", "navigate", "go"),
        "examples": ("Open youtube.com.", "Go to the GitHub page.", "Open my dashboard."),
        "outputs": ("page",),
    },
    "search_web": {
        "capability_id": "browser.search",
        "tags": ("search", "web", "google", "look", "up", "query", "find", "online"),
        "examples": ("Search the web for the latest Python release.", "Google the weather."),
        "inputs": ("query",),
        "outputs": ("results", "answer"),
    },
    "extract_page": {
        "capability_id": "browser.extract_page",
        "tags": ("extract", "scrape", "content", "page", "results", "links"),
        "examples": ("Pull the links off this page.",),
        "outputs": ("content", "links"),
    },
    "browser_action": {
        "capability_id": "browser.act",
        "tags": ("browser", "click", "type", "scroll", "site", "page"),
        "examples": ("Click the login button on this page.",),
    },
    "fill_form": {
        "capability_id": "browser.fill_form",
        "tags": ("fill", "form", "field", "submit", "enter", "browser"),
        "examples": ("Fill in the search box with my address.",),
    },
    # -- system -------------------------------------------------------------
    "system_info": {
        "capability_id": "system.info",
        "tags": ("system", "machine", "hardware", "spec", "specs", "ram", "cpu", "gpu"),
        "examples": ("What are this machine's specs?", "How much RAM do I have?"),
        "outputs": ("value",),
    },
    "system_status": {
        "capability_id": "system.status",
        "tags": ("system", "status", "health", "load", "busy", "cpu", "ram"),
        "examples": ("How is the system doing?", "Is the machine under load?"),
        "outputs": ("value",),
    },
    "cpu_status": {
        "capability_id": "system.cpu",
        "tags": ("cpu", "processor", "load", "usage", "busy"),
        "examples": ("What is my CPU usage?", "How busy is the processor?"),
        "outputs": ("value",),
    },
    "memory_status": {
        "capability_id": "system.get_ram",
        "tags": ("ram", "memory", "usage", "free", "used"),
        "examples": ("How much RAM is free?", "Is my memory full?"),
        "outputs": ("value",),
    },
    "gpu_status": {
        "capability_id": "system.gpu",
        "tags": ("gpu", "graphics", "vram", "card"),
        "examples": ("How much VRAM is in use?",),
        "outputs": ("value",),
    },
    "battery_status": {
        "capability_id": "system.battery",
        "tags": ("battery", "charge", "power", "percent", "plugged"),
        "examples": ("What is my battery level?",),
        "outputs": ("value",),
    },
    "network_status": {
        "capability_id": "system.network",
        "tags": ("network", "wifi", "internet", "connection", "online"),
        "examples": ("Am I connected to the network?",),
        "outputs": ("value",),
    },
    # -- developer ------------------------------------------------------------
    "project_analysis": {
        "capability_id": "developer.inspect_project",
        "tags": (
            "project", "python", "code", "codebase", "failing", "fails", "failure",
            "broken", "build", "tests", "logs", "dependencies", "structure", "inspect",
            "analyze", "analyse", "review",
        ),
        "examples": (
            "Check why my Python project is failing.",
            "Why does the build break?",
            "Review this project's structure.",
        ),
        "required_models": ("chat",),
        "inputs": ("goal",),
        "outputs": ("report",),
    },
    "code_debugging": {
        "capability_id": "developer.debug",
        "tags": (
            "debug", "failing", "failure", "error", "errors", "traceback", "exception",
            "stack", "bug", "fix", "tests",
        ),
        "examples": ("Why is my test failing?", "Debug this traceback.", "Fix this error."),
        "required_models": ("chat",),
        "outputs": ("report",),
    },
    "code_generation": {
        "capability_id": "developer.write_code",
        "tags": ("write", "generate", "code", "function", "script", "implement", "new"),
        "examples": ("Write a Python function that parses this.", "Scaffold a Flask app."),
        "required_models": ("chat",),
        "outputs": ("code",),
    },
    "code_explanation": {
        "capability_id": "developer.explain_code",
        "tags": ("explain", "code", "function", "understand", "what", "walk"),
        "examples": ("Explain what this function does.",),
        "required_models": ("chat",),
        "outputs": ("text",),
    },
    # ``developer.run_command`` is deliberately absent: running a command is a
    # step the APPLICATION's plan executor carries out, so the application
    # declares it (see ``NovaControlApplication._declare_actions``). A row here
    # would be metadata for a capability nothing registers — which is how a
    # table starts describing a system that does not exist.
    # -- vision, desktop, senses ---------------------------------------------
    "screenshot_analysis": {
        "capability_id": "vision.analyze_screen",
        "tags": ("screenshot", "screen", "look", "see", "error", "dialog", "image"),
        "examples": ("What does this error say?", "Look at my screen."),
        "required_models": ("vision",),
        "outputs": ("text", "answer"),
    },
    "take_screenshot": {
        "capability_id": "desktop.screenshot",
        "tags": ("screenshot", "capture", "screen", "save"),
        "examples": ("Take a screenshot.",),
        "outputs": ("path",),
    },
    "open_application": {
        "capability_id": "desktop.open_application",
        "tags": ("open", "launch", "start", "run", "application", "app", "program", "editor"),
        "examples": ("Open VS Code.", "Launch Chrome.", "Start Spotify."),
        "outputs": ("window",),
    },
    "close_application": {
        "capability_id": "desktop.close_application",
        "tags": ("close", "quit", "exit", "application", "app", "window"),
        "examples": ("Close Chrome.",),
    },
    "open_folder": {
        "capability_id": "desktop.open_folder",
        "tags": ("open", "folder", "directory", "explorer", "files"),
        "examples": ("Open my Downloads folder.",),
    },
    "type_text": {
        "capability_id": "desktop.type_text",
        "tags": ("type", "type text", "dictate", "write", "into", "field"),
        "examples": ("Type this into the form.",),
    },
    "press_key": {
        "capability_id": "desktop.press_key",
        "tags": ("press", "key", "keyboard", "shortcut", "hotkey", "enter", "escape"),
        "examples": ("Press enter.", "Hit control s."),
    },
    "volume_control": {
        "capability_id": "desktop.volume",
        "tags": ("volume", "sound", "louder", "quieter", "mute", "audio"),
        "examples": ("Turn the volume down.", "Mute the sound."),
    },
    "brightness_control": {
        "capability_id": "desktop.brightness",
        "tags": ("brightness", "screen", "dim", "brighter", "darker"),
        "examples": ("Turn the brightness down.",),
    },
    "media_control": {
        "capability_id": "desktop.media",
        "tags": ("play", "pause", "music", "song", "track", "next", "previous", "media"),
        "examples": ("Pause the music.", "Play the next track."),
    },
    # -- language, research, memory ------------------------------------------
    "answer_question": {
        "capability_id": "chat.answer",
        "tags": ("answer", "question", "explain", "how", "what", "why"),
        "examples": ("What is a race condition?", "Explain async in Python."),
        "required_models": ("chat",),
        "outputs": ("answer",),
    },
    "chat": {
        "capability_id": "chat.chat",
        "tags": ("chat", "talk", "hello", "hey", "thanks"),
        "examples": ("Hello there.", "Thanks, that helped."),
        "required_models": ("chat",),
        "outputs": ("response",),
    },
    "conversation": {
        "capability_id": "chat.conversation",
        "tags": ("chat", "conversation", "small", "talk", "hello"),
        "examples": ("How are you doing today?",),
        "required_models": ("chat",),
        "outputs": ("response",),
    },
    "general_question": {
        "capability_id": "chat.question",
        "tags": ("question", "general", "ask", "know", "about"),
        "examples": ("What can you do?",),
        "required_models": ("chat",),
        "outputs": ("answer",),
    },
    "research": {
        "capability_id": "research.web",
        "tags": ("research", "investigate", "compare", "sources", "study", "web"),
        "examples": ("Research the best laptops for Python.",),
        "required_models": ("chat",),
        "outputs": ("report",),
    },
    "summarize": {
        "capability_id": "research.summarize",
        "tags": ("summarize", "summary", "shorten", "gist", "brief"),
        "examples": ("Summarize this article.",),
        "required_models": ("chat",),
        "outputs": ("summary",),
    },
    "compare": {
        "capability_id": "research.compare",
        "tags": ("compare", "versus", "difference", "better", "between"),
        "examples": ("Compare FastAPI and Flask.",),
        "required_models": ("chat",),
        "outputs": ("report",),
    },
    "generate_report": {
        "capability_id": "research.report",
        "tags": ("report", "write", "draft", "paper", "document"),
        "examples": ("Write a report on this topic.",),
        "required_models": ("chat",),
        "outputs": ("report",),
    },
    "calculate": {
        "capability_id": "compute.calculate",
        "tags": ("calculate", "math", "sum", "multiply", "percent", "arithmetic"),
        "examples": ("What is 15% of 240?", "Multiply 42 by 7."),
        "outputs": ("value",),
    },
    "remember": {
        "capability_id": "memory.remember",
        "tags": ("remember", "note", "save", "fact", "memory"),
        "examples": ("Remember that my editor is VS Code.",),
    },
    "recall": {
        "capability_id": "memory.recall",
        "tags": ("recall", "remember", "what", "did", "memory", "forget"),
        "examples": ("What did I tell you about my editor?",),
        "outputs": ("facts",),
    },
    # -- planning, automation, projects, self-improvement ---------------------
    "plan_task": {
        "capability_id": "planning.plan",
        "tags": ("plan", "steps", "workflow", "break", "down", "task"),
        "examples": ("Plan how to migrate this project.",),
        "required_models": ("chat",),
        "outputs": ("plan",),
    },
    "agentic_task": {
        "capability_id": "agents.agentic_task",
        "tags": ("agent", "agents", "multi", "step", "delegate", "complex"),
        "examples": ("Take care of setting up this project.",),
        "required_models": ("chat",),
        "outputs": ("result",),
    },
    "create_automation": {
        "capability_id": "automation.create",
        "tags": ("automation", "automate", "rule", "whenever", "workflow", "create"),
        "examples": ("Create an automation that backs up my notes.",),
        "outputs": ("workflow",),
    },
    "run_automation": {
        "capability_id": "automation.run",
        "tags": ("automation", "run", "trigger", "workflow"),
        "examples": ("Run my backup automation.",),
        "outputs": ("result",),
    },
    "schedule_task": {
        "capability_id": "scheduler.schedule",
        "tags": ("schedule", "later", "every", "tomorrow", "remind", "timer"),
        "examples": ("Remind me to run the tests tomorrow at 9.",),
        "outputs": ("job",),
    },
    "create_project": {
        "capability_id": "project.create",
        "tags": ("project", "scaffold", "new", "create", "initialise", "initialize"),
        "examples": ("Create a new Python project called nova.",),
        "outputs": ("path",),
    },
    "improve_self": {
        "capability_id": "self_improvement.propose",
        "tags": ("improve", "yourself", "self", "refactor", "better", "upgrade"),
        "examples": ("Improve how you handle long requests.",),
        "required_models": ("chat",),
        "outputs": ("proposal",),
    },
    # -- phone ----------------------------------------------------------------
    "phone_connect": {
        "capability_id": "phone.connect",
        "tags": ("phone", "connect", "pair", "bridge", "adb", "usb"),
        "examples": ("Connect to my phone.",),
    },
    "phone_status": {
        "capability_id": "phone.status",
        "tags": ("phone", "status", "paired", "device", "battery"),
        "examples": ("Is my phone connected?",),
    },
    "phone_send_text": {
        "capability_id": "phone.send_text",
        "tags": ("text", "message", "sms", "send", "phone", "tell"),
        "examples": ("Text Sam that I am running late.",),
    },
    "phone_call": {
        "capability_id": "phone.call",
        "tags": ("call", "phone", "dial", "ring"),
        "examples": ("Call Sam.",),
    },
    "phone_screenshot": {
        "capability_id": "phone.screenshot",
        "tags": ("phone", "screenshot", "screen", "capture"),
        "examples": ("Take a screenshot on my phone.",),
    },
    "phone_open_app": {
        "capability_id": "phone.open_app",
        "tags": ("phone", "open", "app", "application", "launch"),
        "examples": ("Open WhatsApp on my phone.",),
    },
}


#: Which entity kinds an input/output pair is about, so a capability that
#: declares neither still reports something honest: the entities its rules
#: extract ARE its inputs, and its verifier names what it produces.
_OUTPUT_KINDS: dict[str, tuple[str, ...]] = {
    "file_exists_verifier": ("path",),
    "file_content_verifier": ("text",),
    "file_absent_verifier": ("path",),
    "content_verifier": ("content",),
    "page_state_verifier": ("page",),
    "application_state_verifier": ("window",),
    "telemetry_verifier": ("value",),
    "report_verifier": ("report",),
    "response_verifier": ("response",),
    "memory_verifier": ("facts",),
    "plan_verifier": ("plan",),
    "workflow_verifier": ("workflow",),
    "schedule_verifier": ("job",),
    "project_verifier": ("path",),
    "preview_gate_verifier": ("proposal",),
    "math_verifier": ("value",),
    "phone_state_verifier": ("state",),
    "bridge_status_verifier": ("status",),
}


def _enrich(capability: Capability) -> Capability:
    """Apply the declared metadata, then fill what a capability did not declare.

    Filling is deliberately shallow and provable: a capability's inputs are the
    entities its rules extract, and its outputs are what its verifier checks —
    both already stated above, neither restated. Anything that would be a guess
    stays empty.
    """
    metadata = dict(_CAPABILITY_METADATA.get(capability.capability, {}))
    inputs = tuple(metadata.pop("inputs", ()))
    outputs = tuple(metadata.pop("outputs", ()))
    enriched = replace(capability, **metadata)
    return replace(
        enriched,
        supported_inputs=enriched.supported_inputs
        or inputs
        or (*enriched.required, *enriched.optional),
        supported_outputs=enriched.supported_outputs
        or outputs
        or _OUTPUT_KINDS.get(enriched.verifier, ()),
    )


def default_capabilities() -> tuple[Capability, ...]:
    """The shipped capabilities, with their discovery metadata filled in."""
    return tuple(_enrich(capability) for capability in _declared_capabilities())
