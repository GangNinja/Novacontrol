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
    )
