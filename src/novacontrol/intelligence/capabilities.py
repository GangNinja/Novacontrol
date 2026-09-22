"""Default capability registrations: what NovaControl can do, by intent.

Every entry declares the contract the orchestrator reasons over — required and
optional entities, risk, executor, and how success is verified — so planning
and confirmation policy are derived from this registry instead of hard-coded
per call site. ONE source of truth shared by JARVIS, research, automation,
browser, desktop, phone, and future agents.
"""

from __future__ import annotations

from novacontrol.intelligence.intent import Capability, IntentName, RiskLevel


def default_capabilities() -> tuple[Capability, ...]:
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
    )
