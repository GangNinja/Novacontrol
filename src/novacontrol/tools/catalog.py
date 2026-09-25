"""The tool catalogue: every tool this build knows about, described once.

Three surfaces already knew about tools, and each knew a different part of the
truth:

* the **intent catalogue** knows which tool carries out each intent
  (``IntentDefinition.tools``) and what a person says to ask for it;
* the **capability registry** knows the executor, the risk, the required
  entities and how success is verified;
* the **runtime tool registry** knows what is actually registered, with which
  schema and which permission scopes.

Nothing joined them up, so "what tools are there?" had no single answer — and a
tool nobody had described could not be searched for at all. This module is that
join. It builds ONE catalogue of :class:`ToolMetadata`, declared entries first
and contributions folded in from the three surfaces: descriptions stay as
written, risk becomes the highest any source claims, permissions accumulate, and
``registered`` is true only when a callable really exists behind the name. A
tool that is dispatched but not registered is reported as exactly that instead of
being hidden from the search or pretended to be available.

It holds no state beyond the metadata. Nothing here runs a tool, and nothing here
can change a permission.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from novacontrol.core.security import PermissionScope, RiskLevel
from novacontrol.tools.metadata import ToolCategory, ToolMetadata
from novacontrol.tools.models import ToolParameter, ToolSchema

#: Every entity name an intent can require is a string as far as a tool call is
#: concerned: the pipeline resolves *which* application or file, and the tool is
#: handed the resolved name. A schema that claimed otherwise here would be a
#: second, disagreeing type system.
_ENTITY_TYPE = "string"


def _input_schema(
    name: str, description: str, *parameters: tuple[str, str, bool]
) -> ToolSchema:
    """A tool's declared argument shape: (name, type, required) triples."""
    return ToolSchema(
        name=name,
        description=description,
        parameters=tuple(
            ToolParameter(parameter, type_name, required=required)
            for parameter, type_name, required in parameters
        ),
    )


def _output_schema(
    name: str, description: str, *parameters: tuple[str, str]
) -> ToolSchema:
    """A tool's declared RESULT shape: (name, type) pairs.

    A declaration, not a runtime guarantee — the same distinction a plan step's
    ``expected_result`` makes. It is what a planner needs to know before running
    something ("a reading returns a value and a unit"; "a file search returns
    paths"), and extra keys beyond the contract are expected, which is why these
    schemas allow them.
    """
    return ToolSchema(
        name=f"{name}.result",
        description=description,
        parameters=tuple(
            ToolParameter(parameter, type_name) for parameter, type_name in parameters
        ),
        allow_extra_arguments=True,
    )

#: The tools this build ships, described. Deliberately hand-written rather than
#: derived: a description is a claim about what a tool is FOR, and the source
#: that knows that is a person, not a name.
DEFAULT_TOOL_DECLARATIONS: tuple[ToolMetadata, ...] = (
    ToolMetadata(
        name="system_monitor",
        description=(
            "Read this machine's own telemetry: CPU load, memory use, GPU state, "
            "battery, network throughput, disk space and uptime."
        ),
        category=ToolCategory.SYSTEM,
        risk=RiskLevel.LOW,
        read_only=True,
        # The same readings a person asks about are the ones that move: a cached
        # CPU percentage is a lie by the time it is read.
        cache_ttl_s=300.0,
        volatile_values={
            "metric": ("cpu", "memory", "gpu", "network", "battery", "uptime"),
        },
        input_schema=_input_schema(
            "system_monitor",
            "Which reading to take. An unknown metric reports that it could not measure it.",
            ("metric", "string", False),
        ),
        output_schema=_output_schema(
            "system_monitor",
            "One measured reading: the metric, its value and unit, and the "
            "sentence a person reads.",
            ("metric", "string"),
            ("value", "number"),
            ("unit", "string"),
            ("summary", "string"),
        ),
        tags=(
            "cpu", "load", "memory", "ram", "gpu", "vram", "battery", "power",
            "network", "bandwidth", "disk", "storage", "uptime", "hardware",
            "performance", "processes", "system",
        ),
        examples=(
            "Which programs are consuming most of my memory?",
            "What is chewing up my memory?",
            "How much RAM do I have left?",
            "What is my CPU usage right now?",
            "Is my battery low?",
            "How much disk space is free?",
            "Show me the network status.",
        ),
    ),
    ToolMetadata(
        name="desktop_controller",
        description=(
            "Control applications and windows on this computer: open and close "
            "programs, open folders, type text, press keys."
        ),
        category=ToolCategory.DESKTOP,
        risk=RiskLevel.MEDIUM,
        permissions=(PermissionScope.DESKTOP_CONTROL,),
        cache_ttl_s=0.0,
        output_schema=_output_schema(
            "desktop_controller",
            "What happened to the application or window.",
            ("application", "string"),
            ("action", "string"),
            ("changed", "boolean"),
        ),
        tags=("application", "program", "window", "launch", "open", "close", "desktop", "folder"),
        examples=(
            "Open Chrome.",
            "Close Notepad.",
            "Open my Downloads folder.",
        ),
    ),
    ToolMetadata(
        name="file_manager",
        description=(
            "Find, read, write, move, copy and organize files in the local "
            "file system."
        ),
        category=ToolCategory.FILES,
        risk=RiskLevel.MEDIUM,
        permissions=(PermissionScope.FILESYSTEM_READ, PermissionScope.FILESYSTEM_WRITE),
        cache_ttl_s=0.0,
        output_schema=_output_schema(
            "file_manager",
            "What was found or changed on disk.",
            ("path", "string"),
            ("matches", "array"),
            ("changed", "boolean"),
        ),
        tags=(
            "file", "folder", "directory", "path", "find", "search", "read",
            "write", "move", "copy",
        ),
        examples=(
            "Find my NovaControl project.",
            "Show me the files in this folder.",
            "Move the report into the archive.",
        ),
    ),
    ToolMetadata(
        name="browser_controller",
        description=(
            "Drive a real browser: navigate to a page, search the web, extract a "
            "page's text, fill in a form."
        ),
        category=ToolCategory.BROWSER,
        risk=RiskLevel.MEDIUM,
        permissions=(PermissionScope.NETWORK_ACCESS, PermissionScope.BROWSER_CONTROL),
        cache_ttl_s=0.0,
        output_schema=_output_schema(
            "browser_controller",
            "Where the browser ended up and what the page said.",
            ("url", "string"),
            ("title", "string"),
            ("text", "string"),
        ),
        tags=("browser", "web", "website", "page", "search", "navigate", "url", "form", "http"),
        examples=(
            "Open YouTube and search for Python tutorials.",
            "What is on the NovaControl docs page?",
        ),
    ),
    ToolMetadata(
        name="explore_service",
        description=(
            "Research a topic across several web sources and answer from what "
            "they actually say, with the sources named."
        ),
        category=ToolCategory.KNOWLEDGE,
        risk=RiskLevel.LOW,
        permissions=(PermissionScope.NETWORK_ACCESS,),
        read_only=True,
        cache_ttl_s=900.0,
        output_schema=_output_schema(
            "explore_service",
            "An answer, and the sources it was drawn from.",
            ("summary", "string"),
            ("sources", "array"),
        ),
        tags=(
            "research", "sources", "compare", "summarise", "summarize", "explain",
            "report", "web",
        ),
        examples=(
            "Research the BRICS summit and summarize it.",
            "Compare the two laptops on price and battery.",
        ),
    ),
    ToolMetadata(
        name="chat_brain",
        description=(
            "Answer a question in conversation, using the local or configured "
            "language model."
        ),
        category=ToolCategory.CONVERSATION,
        risk=RiskLevel.LOW,
        read_only=True,
        cache_ttl_s=0.0,
        output_schema=_output_schema(
            "chat_brain",
            "The answer, and which model produced it (empty when none was configured).",
            ("summary", "string"),
            ("model", "string"),
        ),
        tags=("answer", "question", "explain", "chat", "conversation", "how", "why"),
        examples=(
            "What is the capital of France?",
            "Explain what a race condition is.",
        ),
    ),
    ToolMetadata(
        name="scratch_brain",
        description=(
            "Do arithmetic and worded maths offline, deterministically, without "
            "a language model."
        ),
        category=ToolCategory.KNOWLEDGE,
        risk=RiskLevel.LOW,
        read_only=True,
        # A sum is the same sum tomorrow; this is the clearest safe cache there is.
        cache_ttl_s=3600.0,
        output_schema=_output_schema(
            "scratch_brain",
            "The computed answer, and its numeric value when there is one.",
            ("answer", "string"),
            ("value", "number"),
        ),
        tags=("math", "arithmetic", "calculate", "sum", "percent", "conversion", "offline"),
        examples=(
            "What is 15% of 240?",
            "What does 3 times 4 equal?",
        ),
    ),
    ToolMetadata(
        name="code_agent",
        description=(
            "Work on code: analyse a project, generate code, explain or debug "
            "what is failing."
        ),
        category=ToolCategory.CODE,
        risk=RiskLevel.MEDIUM,
        permissions=(PermissionScope.FILESYSTEM_READ, PermissionScope.FILESYSTEM_WRITE),
        cache_ttl_s=0.0,
        output_schema=_output_schema(
            "code_agent",
            "What it did, which files it touched, and the exit code of anything it ran.",
            ("summary", "string"),
            ("files", "array"),
            ("exit_code", "integer"),
        ),
        tags=(
            "code", "project", "test", "tests", "run", "debug", "refactor",
            "python", "repository", "failing", "failed",
        ),
        examples=(
            "Inspect the latest changes and fix the failing tests.",
            "Run the tests and tell me what failed.",
            "Explain this function.",
        ),
    ),
    ToolMetadata(
        name="project_manager",
        description="Create and keep track of a local project.",
        category=ToolCategory.CODE,
        risk=RiskLevel.MEDIUM,
        permissions=(PermissionScope.FILESYSTEM_WRITE,),
        cache_ttl_s=0.0,
        output_schema=_output_schema(
            "project_manager",
            "The project and where it lives on disk.",
            ("project", "string"),
            ("path", "string"),
        ),
        tags=("project", "workspace", "scaffold", "create"),
        examples=("Create a new project called notes.",),
    ),
    ToolMetadata(
        name="memory_manager",
        description=(
            "Remember a fact for later and recall what was remembered."
        ),
        category=ToolCategory.MEMORY,
        risk=RiskLevel.LOW,
        permissions=(PermissionScope.FILESYSTEM_READ, PermissionScope.FILESYSTEM_WRITE),
        cache_ttl_s=0.0,
        output_schema=_output_schema(
            "memory_manager",
            "What was stored, or the facts that matched a recall.",
            ("fact", "string"),
            ("matches", "array"),
        ),
        tags=("memory", "remember", "recall", "knowledge", "fact", "preference"),
        examples=(
            "Remember that my project folder is on the D drive.",
            "What do you know about my project folder?",
        ),
    ),
    ToolMetadata(
        name="planning_engine",
        description=(
            "Break a multi-step goal into ordered steps with the machinery each "
            "one needs."
        ),
        category=ToolCategory.PLANNING,
        risk=RiskLevel.LOW,
        read_only=True,
        cache_ttl_s=0.0,
        output_schema=_output_schema(
            "planning_engine",
            "The goal and the ordered steps that reach it.",
            ("goal", "string"),
            ("steps", "array"),
        ),
        tags=("plan", "steps", "decompose", "task", "workflow"),
        examples=("Plan how to migrate my notes to a new folder.",),
    ),
    ToolMetadata(
        name="agent_coordinator",
        description="Hand a long-running, open-ended goal to coordinated agents.",
        category=ToolCategory.PLANNING,
        risk=RiskLevel.MEDIUM,
        cache_ttl_s=0.0,
        output_schema=_output_schema(
            "agent_coordinator",
            "The run's summary, its steps, and whether the result was verified.",
            ("summary", "string"),
            ("steps", "array"),
            ("verified", "boolean"),
        ),
        tags=("agent", "autonomous", "long-running", "coordinate", "goal"),
        examples=("Work on this task until it is done.",),
    ),
    ToolMetadata(
        name="automation_manager",
        description="Create, list and run local automations.",
        category=ToolCategory.AUTOMATION,
        risk=RiskLevel.HIGH,
        permissions=(PermissionScope.SHELL_EXECUTE, PermissionScope.FILESYSTEM_WRITE),
        cache_ttl_s=0.0,
        output_schema=_output_schema(
            "automation_manager",
            "The automation and what happened when it ran.",
            ("workflow", "string"),
            ("status", "string"),
        ),
        tags=("automation", "workflow", "run", "script", "trigger", "repeat"),
        examples=("Automate backing up my notes every night.",),
    ),
    ToolMetadata(
        name="scheduler",
        description="Schedule a task to happen at a time or on a repeat.",
        category=ToolCategory.AUTOMATION,
        risk=RiskLevel.MEDIUM,
        permissions=(PermissionScope.SHELL_EXECUTE,),
        cache_ttl_s=0.0,
        output_schema=_output_schema(
            "scheduler",
            "The scheduled task and when it will next run.",
            ("task", "string"),
            ("scheduled_for", "string"),
        ),
        tags=("schedule", "timer", "reminder", "later", "calendar", "repeat"),
        examples=("Remind me to check the build at 5pm.",),
    ),
    ToolMetadata(
        name="phone_controller",
        description=(
            "Control a paired phone over the local bridge: open an app, send a "
            "text, place a call, take a screenshot."
        ),
        category=ToolCategory.PHONE,
        risk=RiskLevel.MEDIUM,
        permissions=(PermissionScope.PHONE_CONTROL,),
        cache_ttl_s=0.0,
        output_schema=_output_schema(
            "phone_controller",
            "The device it acted on, whether the bridge accepted it, and any message sent.",
            ("device", "string"),
            ("status", "string"),
            ("message", "string"),
        ),
        tags=("phone", "android", "sms", "text", "call", "dial", "device", "bridge"),
        examples=(
            "Send a text to Alex that I am running late.",
            "Call the office.",
        ),
    ),
    ToolMetadata(
        name="vision_pipeline",
        description=(
            "Look at the screen or an image and answer a question about what is "
            "on it."
        ),
        category=ToolCategory.VISION,
        risk=RiskLevel.LOW,
        permissions=(PermissionScope.FILESYSTEM_READ,),
        cache_ttl_s=0.0,
        output_schema=_output_schema(
            "vision_pipeline",
            "The answer about what is on screen, whether the question was "
            "answered, and the elements seen.",
            ("answer", "string"),
            ("question_answered", "boolean"),
            ("elements", "array"),
        ),
        tags=("screenshot", "screen", "image", "vision", "look", "see", "button", "window"),
        examples=(
            "Look at this screenshot and tell me why the button is not working.",
            "What is on my screen right now?",
        ),
    ),
    ToolMetadata(
        name="self_improvement_engine",
        description=(
            "Propose and apply an improvement to NovaControl's own code or "
            "configuration, with a preview before anything is changed."
        ),
        category=ToolCategory.CODE,
        risk=RiskLevel.HIGH,
        permissions=(PermissionScope.FILESYSTEM_READ, PermissionScope.FILESYSTEM_WRITE),
        cache_ttl_s=0.0,
        output_schema=_output_schema(
            "self_improvement_engine",
            "The proposed change, and whether it was applied (a preview changes nothing).",
            ("proposal", "string"),
            ("applied", "boolean"),
        ),
        tags=("improve", "self", "code", "change", "preview", "maintenance"),
        examples=("Improve how you handle failing tools.",),
    ),
    # ── Read-only introspection: the operations the specification names as the
    # ones worth caching (OS information, hardware information, system
    # capabilities). They really are registered, which is what makes the
    # validation → cache → normalization pipeline live rather than theoretical.
    ToolMetadata(
        name="machine_facts",
        description=(
            "Report stable facts about this machine: operating system, CPU model "
            "and count, total memory and disk capacity."
        ),
        category=ToolCategory.SYSTEM,
        risk=RiskLevel.LOW,
        read_only=True,
        # Stable by definition: total RAM does not change between two calls. The
        # LIVE readings (usage percentages) are system_monitor's, and those are
        # declared volatile there.
        cache_ttl_s=600.0,
        tags=(
            "os", "operating system", "platform", "version", "hardware", "cpu",
            "cores", "memory", "ram", "total", "disk", "storage", "capacity",
            "machine", "specs",
        ),
        examples=(
            "What operating system is this?",
            "How much RAM does this machine have in total?",
            "What CPU is in this computer?",
        ),
        input_schema=_input_schema(
            "machine_facts",
            "Which part of the system description to return.",
            ("scope", "string", False),
        ),
        output_schema=_output_schema(
            "machine_facts",
            "Stable facts about the machine — nothing that changes between calls.",
            ("platform", "string"),
            ("platform_release", "string"),
            ("cpu_model", "string"),
            ("cpu_count", "integer"),
            ("python", "string"),
            ("total_memory_bytes", "number"),
            ("disk_total_bytes", "number"),
        ),
    ),
    ToolMetadata(
        name="capabilities",
        description=(
            "List what this installation can actually do: every registered "
            "capability, with its risk and the executor that carries it out."
        ),
        category=ToolCategory.PLANNING,
        risk=RiskLevel.LOW,
        read_only=True,
        # The capability table only changes when the build or the plugins do.
        cache_ttl_s=1800.0,
        tags=(
            "capabilities", "can you", "abilities", "features", "what can",
            "available", "supported", "executors", "system",
        ),
        examples=(
            "What can you do?",
            "Which capabilities are available on this machine?",
        ),
        output_schema=_output_schema(
            "capabilities",
            "Every capability this installation knows about.",
            ("capabilities", "array"),
            ("count", "integer"),
        ),
    ),
    ToolMetadata(
        name="installed_applications",
        description=(
            "List the applications installed on this machine, from the same "
            "index used to open them by name."
        ),
        category=ToolCategory.DESKTOP,
        risk=RiskLevel.LOW,
        read_only=True,
        # What is installed changes when a person installs something, not between
        # two calls; the index this reads is itself built once per process.
        cache_ttl_s=900.0,
        tags=(
            "installed", "applications", "apps", "programs", "software", "list",
            "desktop", "start menu", "catalog", "inventory",
        ),
        examples=(
            "What applications are installed on this machine?",
            "List the installed apps.",
            "Which programs do I have installed?",
        ),
        input_schema=_input_schema(
            "installed_applications",
            "How much of the installed-application list to return.",
            ("filter", "string", False),
        ),
        output_schema=_output_schema(
            "installed_applications",
            "The installed applications that matched, and how many there are.",
            ("applications", "array"),
            ("count", "integer"),
            ("available", "boolean"),
        ),
    ),
)


def default_tool_declarations() -> tuple[ToolMetadata, ...]:
    """The shipped declarations, as a function so callers can extend them."""
    return DEFAULT_TOOL_DECLARATIONS


class ToolCatalog:
    """A lookup of tool metadata by name, with the evidence for each entry."""

    def __init__(self, tools: Iterable[ToolMetadata] = ()) -> None:
        self._tools: dict[str, ToolMetadata] = {}
        for metadata in tools:
            self.register(metadata)

    def register(self, metadata: ToolMetadata) -> None:
        """Add or merge one tool's metadata (merging keeps the highest risk)."""
        existing = self._tools.get(metadata.name)
        self._tools[metadata.name] = existing.merged(metadata) if existing else metadata

    def get(self, name: str) -> ToolMetadata | None:
        return self._tools.get(name)

    def __contains__(self, name: object) -> bool:
        return str(name) in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def tools(self) -> tuple[ToolMetadata, ...]:
        return tuple(self._tools[name] for name in sorted(self._tools))

    def registered(self) -> tuple[ToolMetadata, ...]:
        """Only the tools that really have a callable behind them."""
        return tuple(tool for tool in self.tools() if tool.registered)

    def registered_names(self) -> tuple[str, ...]:
        """The names of those tools, for a caller that only needs the list."""
        return tuple(tool.name for tool in self.registered())

    def categories(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for tool in self._tools.values():
            counts[tool.category.value] = counts.get(tool.category.value, 0) + 1
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "tools": [tool.to_dict() for tool in self.tools()],
            "count": len(self._tools),
            "registered": len(self.registered()),
            "categories": self.categories(),
        }


def build_tool_catalog(
    *,
    declared: Sequence[ToolMetadata] = (),
    intents: Any | None = None,
    capabilities: Any | None = None,
    registry: Any | None = None,
) -> ToolCatalog:
    """One catalogue from every surface that knows about tools.

    ``intents`` is an ``IntentCatalog`` (or anything with ``all()`` returning
    definitions that have ``tools``/``examples``/``intent``), ``capabilities`` is
    a ``CapabilityRegistry`` (or anything with ``all()``), and ``registry`` is the
    runtime ``ToolRegistry``. Everything is optional: a catalogue built with no
    sources still answers with the shipped declarations, which is what makes the
    retriever usable in a test without booting an application.
    """
    catalog = ToolCatalog(declared or default_tool_declarations())
    definitions = _definitions(intents)
    for metadata in _intent_contributions(definitions):
        catalog.register(metadata)
    for metadata in _capability_contributions(capabilities):
        catalog.register(metadata)
    for metadata in _registry_contributions(registry):
        catalog.register(metadata)
    for metadata in _derived_input_schemas(catalog, definitions, capabilities):
        catalog.register(metadata)
    return catalog


def _definitions(intents: Any | None) -> tuple[Any, ...]:
    if intents is None:
        return ()
    return tuple(intents.all() if hasattr(intents, "all") else intents)


def _capability_entities(
    capabilities: Any | None,
) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """The required and optional entities each executor's capabilities declare.

    Keyed by EXECUTOR, because that is the name the catalogue uses for the tool.
    A capability records what a person must supply whether or not the intent it
    serves happens to name the tool back — ``find_file`` requires a ``file`` and
    says ``file_manager`` carries it out, and the ``file_manager`` tool must know
    that even though no hand-written intent names it.
    """
    if capabilities is None:
        return {}
    entries = capabilities.all() if hasattr(capabilities, "all") else tuple(capabilities)
    collected: dict[str, tuple[list[str], list[str]]] = {}
    for entry in entries:
        executor = str(getattr(entry, "executor", "") or "")
        if not executor:
            continue
        required, optional = collected.setdefault(executor, ([], []))
        for entity in tuple(getattr(entry, "required", ()) or ()):
            if entity not in required:
                required.append(entity)
        for entity in tuple(getattr(entry, "optional", ()) or ()):
            if entity not in optional:
                optional.append(entity)
    return {
        name: (tuple(required), tuple(optional))
        for name, (required, optional) in collected.items()
    }


def _derived_input_schemas(
    catalog: ToolCatalog,
    definitions: Sequence[Any],
    capabilities: Any | None = None,
) -> tuple[ToolMetadata, ...]:
    """An argument shape for every tool that did not declare one.

    The entities are not invented: they are the required and optional entities
    the intent catalogue records for the intents this tool carries out, plus
    those the capability registry records for the capabilities it executes.
    Both surfaces are needed — ``desktop_controller`` is told to expect
    ``application`` because ``open_application`` requires one, and
    ``file_manager`` is told to expect ``file`` because the ``find_file``
    capability requires one even though no hand-written intent names the tool.
    This is also what makes the shortlist shown to a model carry a real call
    shape instead of an empty one.
    """
    by_executor = _capability_entities(capabilities)
    derived: list[ToolMetadata] = []
    for tool in catalog.tools():
        if tool.input_schema is not None:
            continue
        required: list[str] = []
        optional: list[str] = []
        for definition in definitions:
            if tool.name not in tuple(getattr(definition, "tools", ()) or ()):
                continue
            for entity in tuple(getattr(definition, "required_entities", ()) or ()):
                if entity not in required:
                    required.append(entity)
            for entity in tuple(getattr(definition, "optional_entities", ()) or ()):
                if entity not in optional:
                    optional.append(entity)
        capability_required, capability_optional = by_executor.get(tool.name, ((), ()))
        for entity in capability_required:
            if entity not in required:
                required.append(entity)
        for entity in capability_optional:
            if entity not in optional:
                optional.append(entity)
        parameters = [(entity, _ENTITY_TYPE, True) for entity in required]
        parameters += [
            (entity, _ENTITY_TYPE, False)
            for entity in optional
            if entity not in required
        ]
        derived.append(
            ToolMetadata(
                name=tool.name,
                input_schema=_input_schema(
                    tool.name,
                    f"What {tool.name} accepts, from the intents it carries out.",
                    *parameters,
                ),
                read_only=True,
            )
        )
    return tuple(derived)


def _intent_contributions(definitions: Sequence[Any]) -> tuple[ToolMetadata, ...]:
    """What the intent catalogue adds: the phrases a person would actually say."""
    contributions: list[ToolMetadata] = []
    for definition in definitions:
        intent = getattr(definition, "intent", "")
        examples = tuple(getattr(definition, "examples", ()) or ())[:3]
        for name in tuple(getattr(definition, "tools", ()) or ()):
            contributions.append(
                ToolMetadata(
                    name=str(name),
                    examples=examples,
                    intents=(str(getattr(intent, "value", intent)),),
                    # Neutral on every claim it does not make: the merge ANDs
                    # read-only, so a contribution must not be the reason a tool
                    # is treated as a write.
                    read_only=True,
                )
            )
    return tuple(contributions)


def _capability_contributions(capabilities: Any | None) -> tuple[ToolMetadata, ...]:
    """What the capability registry adds: names, risk, and which intents it serves."""
    if capabilities is None:
        return ()
    entries = capabilities.all() if hasattr(capabilities, "all") else tuple(capabilities)
    contributions: list[ToolMetadata] = []
    for entry in entries:
        executor = str(getattr(entry, "executor", "") or "")
        if not executor:
            continue
        risk = getattr(entry, "risk", RiskLevel.LOW)
        contributions.append(
            ToolMetadata(
                name=executor,
                capabilities=(str(getattr(entry, "capability", "") or ""),),
                risk=risk if isinstance(risk, RiskLevel) else RiskLevel.LOW,
                intents=(str(getattr(getattr(entry, "intent", ""), "value", "")),),
                read_only=True,
            )
        )
    return tuple(contributions)


def _registry_contributions(registry: Any | None) -> tuple[ToolMetadata, ...]:
    """What the runtime registry adds: what is ACTUALLY registered.

    Neutral on ``read_only``: the merge ANDs that claim, so a contribution must
    not be the reason a declared read-only tool looks like a write. Nothing is
    granted by being neutral — caching also needs a declared ``cache_ttl_s``, and
    an undeclared tool has none.
    """
    if registry is None or not hasattr(registry, "list"):
        return ()
    contributions: list[ToolMetadata] = []
    for registered in registry.list():
        tool = getattr(registered, "tool", None)
        schema = getattr(registered, "schema", None)
        name = str(getattr(tool, "name", "") or getattr(schema, "name", ""))
        if not name:
            continue
        permissions = tuple(
            scope if isinstance(scope, PermissionScope) else PermissionScope(str(scope))
            for scope in (getattr(tool, "required_permissions", ()) or ())
        )
        contributions.append(
            ToolMetadata(
                name=name,
                description=str(getattr(schema, "description", "") or ""),
                input_schema=schema if isinstance(schema, ToolSchema) else None,
                permissions=permissions,
                # Neutral, per this function's docstring: the TTL below is what
                # decides whether anything may be reused, and it comes from a
                # declaration.
                read_only=True,
                registered=True,
                risk=RiskLevel.MEDIUM if permissions else RiskLevel.LOW,
            )
        )
    return tuple(contributions)


def tool_descriptions(catalog: ToolCatalog, names: Sequence[str]) -> tuple[dict[str, Any], ...]:
    """Compact descriptions of just these tools — what a model may be shown.

    Deliberately not the full metadata: a prompt needs what a tool DOES and what
    a call to it looks like, not its cache lifetime. An unknown name yields a
    stub that says so, because a plan naming a tool nobody has must be visible
    rather than silently dropped.
    """
    described: list[dict[str, Any]] = []
    for name in names:
        metadata = catalog.get(name)
        if metadata is None:
            described.append({"name": name, "description": "", "available": False})
            continue
        described.append(
            {
                "name": metadata.name,
                "description": metadata.description or f"Carries out {name}.",
                "category": metadata.category.value,
                "risk": metadata.risk.value,
                "requires_approval": metadata.requires_approval(),
                "parameters": {
                    parameter.name: {
                        "type": parameter.type,
                        "required": parameter.required,
                        "description": parameter.description,
                    }
                    for parameter in (
                        metadata.input_schema.parameters if metadata.input_schema else ()
                    )
                },
                "available": metadata.registered,
            }
        )
    return tuple(described)


def catalog_summary(catalog: ToolCatalog) -> Mapping[str, Any]:
    """Operational view of the catalogue (safe to return from an endpoint)."""
    return catalog.to_dict()
