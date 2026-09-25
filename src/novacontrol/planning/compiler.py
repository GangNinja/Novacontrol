"""The planner: turning a goal into an executable, verifiable plan.

Phase 3 answers *what should we do with this request?* — a route, a capability,
a model, the flags. This is the layer after it: given that answer and the goal
itself, what are the actual STEPS, in what order, with which tool, what does a
good result look like, and how will it be checked?

The plan for

    "Open VS Code, open my NovaControl project, run the tests and tell me what
    failed."

is not four steps, because the request contains prerequisites nobody said out
loud. The project has to be LOCATED before it can be opened; a test run has to
be COLLECTED before its failures can be ANALYSED; and an analysis has to exist
before it can be SUMMARISED:

    1. locate project      find_file            the project's path
    2. open VS Code        open_application     the editor is running
    3. open project        open_folder          the project is open in it
    4. execute tests       run_command          the suite ran
    5. collect output      run_command/output   stdout and the exit code
    6. analyze failures    (reasoning)          the failing tests, named
    7. summarize result    (reasoning)          an answer to the question

Every step carries its effect on the world (``StepEffect``), because that single
field decides three separate rules downstream: whether the step may run in
parallel, whether its result must be verified, and whether it may ever be
retried. A plan that cannot say what a step DOES cannot be executed safely.

The compiler is deterministic and executes nothing. It is also deliberately
conservative: a step whose tool is not registered keeps an EMPTY tool rather
than an invented one, so the executor reports "nothing here can carry this out"
instead of the plan looking complete and failing somewhere less obvious.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from novacontrol.planning.models import (
    Plan,
    PlanStep,
    RetryPolicy,
    StepEffect,
    VerificationMethod,
    VerificationPolicy,
    VerificationSpec,
)

#: Resolves an intent name to the tool that would carry it out, or "" when
#: nothing is registered. Injected so the compiler never reaches into the
#: catalog itself and stays testable with a two-line stub.
ToolLookup = Callable[[str], str]


class ClauseKind(StrEnum):
    """What a clause of the goal is asking for."""

    LOCATE = "locate"
    OPEN_APPLICATION = "open_application"
    OPEN_PROJECT = "open_project"
    RUN_TESTS = "run_tests"
    RUN_COMMAND = "run_command"
    READ_SYSTEM = "read_system"
    BROWSER = "browser"
    RESEARCH = "research"
    WRITE_FILE = "write_file"
    DELETE = "delete"
    SEND = "send"
    SETTINGS = "settings"
    DESKTOP = "desktop"
    REMEMBER = "remember"
    SUMMARIZE = "summarize"
    REASON = "reason"


# --------------------------------------------------------------------------- #
# Clause recognition
# --------------------------------------------------------------------------- #

_RUN_TESTS = re.compile(
    r"\b(run|execute|start|re-?run)\b[^,;]*\b(tests?|test suite|pytest|unit tests?|specs?)\b",
    re.I,
)
_ANALYZE = re.compile(
    r"\b(analy[sz]e|diagnose|investigate|why|what failed|what broke|what went wrong|"
    r"figure out|find (?:out )?(?:what|why|which))\b",
    re.I,
)
_REPORT_VERB = re.compile(
    r"\b(tell me|report|summari[sz]e|explain|describe|let me know|show me)\b", re.I
)
_OPEN_APPLICATION = re.compile(
    r"\b(open|launch|start|run)\b\s+(?!my\b|the\b|this\b)([a-z0-9][\w .+-]*?)\s*$", re.I
)
_OPEN_PROJECT = re.compile(
    r"\b(open|load|switch to)\b[^,;]*\b(my|the|our)?\s*"
    r"([\w .+-]+?)\s+(project|workspace|repo|repository|folder|codebase)\b",
    re.I,
)
_LOCATE = re.compile(r"\b(find|locate|where is|look for)\b", re.I)
_RUN_COMMAND = re.compile(
    r"\b(run|execute)\b\s+(?:(?:the|a)\s+)?"
    r"(command|shell|terminal|script|scripte?d?\s+\w+)\b",
    re.I,
)
_BROWSER = re.compile(
    r"\b(browse|navigate|go to|visit|open (?:the )?(?:website|url|page|site)|"
    r"search (?:the )?(?:web|online|for))\b",
    re.I,
)
_RESEARCH = re.compile(r"\b(research|look up|find documentation|compare|gather sources)\b", re.I)
_WRITE_FILE = re.compile(
    r"\b(write|create|save|append|overwrite|generate)\b[^,;]*"
    r"\b(file|document|report|note|folder|directory)\b",
    re.I,
)
_DELETE = re.compile(r"\b(delete|remove|erase|wipe|trash|uninstall|clean up)\b", re.I)
_SEND = re.compile(
    r"\b(send|email|e-?mail|text|message|post|tweet|upload|purchase|buy|order|pay)\b",
    re.I,
)
_SETTINGS = re.compile(
    r"\b(set|change|enable|disable|turn (?:on|off)|adjust)\b[^,;]*"
    r"\b(setting|volume|brightness|wifi|wi-fi|bluetooth|default|config)\b",
    re.I,
)
_DESKTOP = re.compile(r"\b(click|type|press|scroll|screenshot|window|desktop|close)\b", re.I)
_REMEMBER = re.compile(r"\b(remember|note that|keep in mind|don'?t forget)\b", re.I)
_SUMMARIZE = re.compile(
    r"\b(summari[sz]e|summary|tell me what failed|what failed|"
    r"report (?:the|on)|conclusion)\b",
    re.I,
)

#: Ordered matchers: the first that matches names the clause's kind. Order is
#: meaningful — "run the tests" must be read as tests before the generic
#: command matcher can see the word "run", and a destructive verb must win over
#: a generic write.
_MATCHERS: tuple[tuple[re.Pattern[str], ClauseKind], ...] = (
    (_RUN_TESTS, ClauseKind.RUN_TESTS),
    (_DELETE, ClauseKind.DELETE),
    (_SEND, ClauseKind.SEND),
    (_SETTINGS, ClauseKind.SETTINGS),
    (_SUMMARIZE, ClauseKind.SUMMARIZE),
    (_ANALYZE, ClauseKind.REASON),
    (_OPEN_PROJECT, ClauseKind.OPEN_PROJECT),
    (_LOCATE, ClauseKind.LOCATE),
    (_RUN_COMMAND, ClauseKind.RUN_COMMAND),
    (_BROWSER, ClauseKind.BROWSER),
    (_RESEARCH, ClauseKind.RESEARCH),
    (_WRITE_FILE, ClauseKind.WRITE_FILE),
    (_REMEMBER, ClauseKind.REMEMBER),
    (_DESKTOP, ClauseKind.DESKTOP),
    (_OPEN_APPLICATION, ClauseKind.OPEN_APPLICATION),
)

#: Metric words a reading clause may name, mapped to their intent.
_METRIC_INTENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cpu_status", ("cpu", "processor")),
    ("memory_status", ("ram", "memory")),
    ("gpu_status", ("gpu", "graphics", "vram", "video card")),
    ("battery_status", ("battery", "charge", "power level")),
    ("network_status", ("network", "wifi", "wi-fi", "internet", "connection")),
    ("system_info", ("disk", "storage", "disk space", "specs", "uptime")),
)

_READ_SYSTEM = re.compile(
    r"\b(check|read|show|report|get|grab|fetch|how much|how many|what(?:'s| is)?)\b[^,;]*\b("
    + "|".join(word for _, words in _METRIC_INTENTS for word in words)
    + r")\b",
    re.I,
)

_CLAUSE_SPLIT = re.compile(
    r"\s*(?:,|;|\n|\bthen\b|\band then\b|\band\b|\bafter that\b|\balso\b)\s*", re.I
)

#: Verbs a project name must not carry into the plan.
_VERB_PREFIX = re.compile(
    r"^(?:please\s+)?(?:open|load|switch to|find|locate|run|execute|start|launch|"
    r"check|analyse|analyze|tell me about|report on)\s+",
    re.I,
)


@dataclass(frozen=True, slots=True)
class Clause:
    """One recognised piece of the goal."""

    text: str
    kind: ClauseKind
    #: The thing the clause is about: an application, a project, a metric.
    target: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "kind": self.kind.value, "target": self.target}


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StepTemplate:
    """A step the compiler emits, before it is turned into a PlanStep."""

    title: str
    action: str
    intent: str
    description: str
    expected_result: str
    effect: StepEffect
    id: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)
    verification: VerificationSpec = field(default_factory=VerificationSpec)
    parallel_safe: bool = False
    depends_on: tuple[str, ...] = ()
    #: True for a step the goal IMPLIED (locate, collect, analyse, summarise)
    #: rather than one the user asked for. Those bring their own dependencies,
    #: while spoken steps are chained behind the previous step that changes
    #: something — a write must not overtake another write.
    implied: bool = False


class PlanCompiler:
    """Builds a plan from a goal and (optionally) the decision about it."""

    def __init__(
        self,
        *,
        tool_lookup: ToolLookup | None = None,
        retry_policy: RetryPolicy | None = None,
        verification_policy: VerificationPolicy | None = None,
    ) -> None:
        self._tool_lookup = tool_lookup or (lambda _intent: "")
        self.retry_policy = retry_policy or RetryPolicy()
        self.verification_policy = verification_policy or VerificationPolicy()

    # -- public ---------------------------------------------------------------

    def compile(
        self,
        goal: str,
        *,
        decision: object | None = None,
        intent: object | None = None,
    ) -> Plan:
        """Compile a goal into a plan, honouring a decision that says "ask first"."""
        decision_payload = _decision_payload(decision)
        retry_policy = self.retry_policy

        if _demands_clarification(decision_payload, intent):
            return Plan(
                goal=goal,
                steps=(),
                needs_clarification=True,
                retry_policy=retry_policy,
                verification_policy=self.verification_policy,
                decision=decision_payload,
            )

        clauses = self.recognise(goal)
        templates = self._assemble(clauses, decision_payload)
        if not templates:
            return Plan(
                goal=goal,
                steps=(),
                needs_clarification=True,
                retry_policy=retry_policy,
                verification_policy=self.verification_policy,
                decision=decision_payload,
            )

        steps = tuple(self._materialise(template, decision_payload) for template in templates)
        return Plan(
            goal=goal,
            steps=steps,
            retry_policy=retry_policy,
            verification_policy=self.verification_policy,
            decision=decision_payload,
        )

    def recognise(self, goal: str) -> tuple[Clause, ...]:
        """Split a goal into clauses, and name what each one asks for.

        Clause-level recognition is reported rather than hidden: a request with
        a clause nothing matches still produces a step (as reasoning), because
        silently dropping half a sentence is the failure mode this whole layer
        exists to prevent.
        """
        clauses: list[Clause] = []
        for raw in _CLAUSE_SPLIT.split(goal.strip()):
            text = raw.strip(" .")
            if not text:
                continue
            clauses.extend(self._recognise_clause(text))
        return tuple(clauses)

    # -- recognition ----------------------------------------------------------

    def _recognise_clause(self, text: str) -> list[Clause]:
        metrics = self._metrics(text)
        if metrics and _READ_SYSTEM.search(text) and not _SUMMARIZE.search(text):
            return [Clause(text, ClauseKind.READ_SYSTEM, metric) for metric in metrics]
        for pattern, kind in _MATCHERS:
            if not pattern.search(text):
                continue
            return [Clause(text, kind, _target_for(kind, text))]
        # "how much RAM is free" without a verb, "the failing tests": a clause
        # that only names metrics is still a reading.
        if metrics:
            return [Clause(text, ClauseKind.READ_SYSTEM, metric) for metric in metrics]
        return [Clause(text, ClauseKind.REASON, _target_for(ClauseKind.REASON, text))]

    @staticmethod
    def _metrics(text: str) -> list[str]:
        lowered = text.lower()
        found: list[str] = []
        for intent, words in _METRIC_INTENTS:
            if any(word in lowered for word in words):
                found.append(intent)
        return found

    # -- assembly -------------------------------------------------------------

    def _assemble(
        self, clauses: Sequence[Clause], decision: Mapping[str, Any]
    ) -> list[StepTemplate]:
        """Turn clauses into ordered templates, then add what they imply.

        Ordering rules, applied in this order:

          1. clauses keep the order they were spoken in;
          2. a locate step is HOISTED to the front — it is a prerequisite of
             everything that names the project, so a plan that opens a project
             before knowing where it is has the order wrong in a way that only
             shows up at run time;
          3. a test or command run gains a collect step, and the goal's own
             question adds an analysis step — inserted where they belong rather
             than appended to the end;
          4. a step that CHANGES something waits for the previous such step;
             read-only steps do not, which is what lets independent readings
             share a wave.
        """
        locate: StepTemplate | None = None
        spoken: list[StepTemplate] = []
        used: set[str] = set()
        wants_summary = False
        wants_analysis = False

        for clause in clauses:
            if clause.kind is ClauseKind.REASON:
                wants_analysis = True
            if clause.kind is ClauseKind.SUMMARIZE:
                wants_summary = True
                wants_analysis = wants_analysis or bool(_ANALYZE.search(clause.text))
            for template in self._templates_for(clause, decision):
                if template.action == "locate_project":
                    locate = locate or template
                    continue
                if template.action == "summarize_result":
                    # Built after the steps it summarises, with a real edge.
                    wants_summary = True
                    continue
                if template.action == "open_project" and locate is not None:
                    template = replace(
                        template, depends_on=_dedupe((*template.depends_on, locate.id))
                    )
                if template.action == "run_tests" and locate is not None:
                    # A test run happens IN a project; without one located, the
                    # run has no working directory to be about.
                    template = replace(
                        template, depends_on=_dedupe((*template.depends_on, locate.id))
                    )
                spoken.append(_unique(template, used))
                used.add(spoken[-1].id)

        ordered: list[StepTemplate] = [*([locate] if locate else []), *spoken]

        # Working out what happened needs the output of something that RAN.
        goal_text = " ".join(clause.text for clause in clauses)
        runner = next(
            (item for item in ordered if item.action in ("run_tests", "run_command")), None
        )
        if runner is not None:
            ordered.insert(ordered.index(runner) + 1, _collect_output(runner.id))

        if wants_analysis or (runner is not None and bool(_ANALYZE.search(goal_text))):
            wants_analysis = True

        if wants_analysis and ordered:
            anchor_id = _analysis_anchor(ordered)
            if anchor_id:
                step = _analyze_result(anchor_id)
                ordered.insert(_index_after(ordered, anchor_id) + 1, step)

        if wants_summary and ordered:
            ordered.append(_summarize_result(ordered[-1].id))

        return _chain(ordered)

    def _templates_for(
        self, clause: Clause, decision: Mapping[str, Any]
    ) -> list[StepTemplate]:
        text = clause.text
        kind = clause.kind
        target = clause.target

        if kind is ClauseKind.READ_SYSTEM:
            intent = target or "system_status"
            label = _metric_label(intent)
            return [
                StepTemplate(
                    title=f"Read {label}",
                    action=f"read_{intent.replace('_status', '')}",
                    intent=intent,
                    description=f"Read the live {label} from this machine.",
                    expected_result=f"The current {label}, or an explicit 'unavailable'.",
                    effect=StepEffect.READ_ONLY,
                    id=f"read-{intent.replace('_status', '')}",
                    parameters={"metric": intent},
                    parallel_safe=True,
                    # A reading CAN be checked, and should be: the check is that
                    # the reading reported either a value or why it could not
                    # measure one. A verifier that does not know this check
                    # reports it inconclusive rather than assuming a pass.
                    verification=VerificationSpec(
                        method=VerificationMethod.CALLABLE,
                        target="metric_read",
                        description=f"the {label} reading reports a value or a reason",
                    ),
                )
            ]
        if kind is ClauseKind.LOCATE:
            project = _project_name(text) or target
            return [_locate_project(project or text)]
        if kind is ClauseKind.OPEN_PROJECT:
            project = _project_name(text) or target
            return [
                _locate_project(project or text),
                StepTemplate(
                    title=f"Open {project}" if project else "Open project",
                    action="open_project",
                    intent="open_folder",
                    description=f"Open {project or 'the project'} in the editor.",
                    expected_result="The project is open and the editor shows it.",
                    effect=StepEffect.LOCAL_WRITE,
                    id="open-project",
                    parameters={"project": project} if project else {},
                    verification=VerificationSpec(
                        method=VerificationMethod.STATE_OBSERVED,
                        description="the editor reports the project as open",
                    ),
                ),
            ]
        if kind is ClauseKind.OPEN_APPLICATION:
            application = _application_name(text) or target
            return [
                StepTemplate(
                    title=f"Open {application}" if application else "Open the application",
                    action="open_application",
                    intent="open_application",
                    description=f"Start {application or 'the application'}.",
                    expected_result=f"{application or 'The application'} is running.",
                    effect=StepEffect.LOCAL_WRITE,
                    id="open-application",
                    parameters={"application": application} if application else {},
                    verification=VerificationSpec(
                        method=VerificationMethod.PROCESS_RUNNING,
                        target=application,
                        description=f"{application or 'the application'} is running",
                    ),
                )
            ]
        if kind is ClauseKind.RUN_TESTS:
            return [
                StepTemplate(
                    title="Run tests",
                    action="run_tests",
                    intent="run_command",
                    description=f"Execute the test suite: {text}",
                    expected_result="The suite ran and reported an exit code.",
                    effect=StepEffect.LOCAL_WRITE,
                    id="run-tests",
                    parameters={"scope": _test_scope(text)},
                    # Any exit code is accepted: with "run the tests and tell me
                    # what failed", a non-zero exit is the RESULT, not a failure
                    # of the step. The code is captured for the analysis.
                    verification=VerificationSpec(
                        method=VerificationMethod.EXIT_CODE,
                        expect=None,
                        description="the suite ran and reported an exit code",
                    ),
                )
            ]
        if kind is ClauseKind.RUN_COMMAND:
            return [
                StepTemplate(
                    title="Run command",
                    action="run_command",
                    intent="run_command",
                    description=f"Run the command: {text}",
                    expected_result="The command ran and reported an exit code.",
                    effect=StepEffect.LOCAL_WRITE,
                    id="run-command",
                    parameters={"command": _command_text(text)},
                    verification=VerificationSpec(
                        method=VerificationMethod.EXIT_CODE,
                        expect=None,
                        description="the command ran and reported an exit code",
                    ),
                )
            ]
        if kind is ClauseKind.BROWSER:
            return [
                StepTemplate(
                    title="Browse",
                    action="navigate",
                    intent="navigate",
                    description=f"Carry out the browser work: {text}",
                    expected_result="The requested page is open and readable.",
                    effect=StepEffect.READ_ONLY,
                    id="browse",
                    parameters={"request": text},
                )
            ]
        if kind is ClauseKind.RESEARCH:
            return [
                StepTemplate(
                    title="Research",
                    action="research",
                    intent="research",
                    description=f"Research, with sources: {text}",
                    expected_result="Findings cited to their sources.",
                    effect=StepEffect.READ_ONLY,
                    id="research",
                    parameters={"question": text},
                )
            ]
        if kind is ClauseKind.WRITE_FILE:
            return [
                StepTemplate(
                    title="Write file",
                    action="write_file",
                    intent="write_file",
                    description=f"Write the file: {text}",
                    expected_result="The file exists with the expected contents.",
                    effect=StepEffect.LOCAL_WRITE,
                    id="write-file",
                    parameters={"request": text},
                    verification=VerificationSpec(
                        method=VerificationMethod.FILE_EXISTS,
                        description="the written file exists on disk",
                    ),
                )
            ]
        if kind is ClauseKind.DELETE:
            return [
                StepTemplate(
                    title="Delete",
                    action="delete_file",
                    intent="delete_file",
                    description=f"Delete: {text}",
                    expected_result="The target no longer exists, and nothing else changed.",
                    effect=StepEffect.DESTRUCTIVE,
                    id="delete",
                    parameters={"request": text, "target": target},
                    verification=VerificationSpec(
                        method=VerificationMethod.FILE_EXISTS,
                        expect=False,
                        description="the deleted path no longer exists",
                    ),
                )
            ]
        if kind is ClauseKind.SEND:
            return [
                StepTemplate(
                    title="Send",
                    action="send_external",
                    intent="send_external",
                    description=f"Send, outside this machine: {text}",
                    expected_result="The message left the machine, once.",
                    effect=StepEffect.EXTERNAL,
                    id="send",
                    parameters={"request": text, "target": target},
                )
            ]
        if kind is ClauseKind.SETTINGS:
            return [
                StepTemplate(
                    title="Change settings",
                    action="change_settings",
                    intent="change_settings",
                    description=f"Change a machine setting: {text}",
                    expected_result="The setting holds the requested value.",
                    effect=StepEffect.SYSTEM,
                    id="change-settings",
                    parameters={"request": text},
                    verification=VerificationSpec(
                        method=VerificationMethod.STATE_OBSERVED,
                        description="the setting reports the new value",
                    ),
                )
            ]
        if kind is ClauseKind.DESKTOP:
            return [
                StepTemplate(
                    title="Desktop action",
                    action="desktop_action",
                    intent="desktop_action",
                    description=f"Carry out the desktop action: {text}",
                    expected_result="The screen changed as the action intended.",
                    effect=StepEffect.LOCAL_WRITE,
                    id="desktop-action",
                    parameters={"request": text},
                    verification=VerificationSpec(
                        method=VerificationMethod.STATE_OBSERVED,
                        description="the screen changed after the action",
                    ),
                )
            ]
        if kind is ClauseKind.REMEMBER:
            return [
                StepTemplate(
                    title="Remember",
                    action="remember",
                    intent="remember",
                    description=f"Store for later: {text}",
                    expected_result="The fact is stored and can be recalled.",
                    effect=StepEffect.LOCAL_WRITE,
                    id="remember",
                    parameters={"request": text},
                )
            ]
        if kind is ClauseKind.SUMMARIZE:
            return [_summarize_result("")]
        # A clause nothing matched is still work: reason about it, do not drop it.
        return [
            StepTemplate(
                title="Reason",
                action="reason",
                intent="answer_question",
                description=f"Work out, from the results so far: {text}",
                expected_result="An answer grounded in what actually happened.",
                effect=StepEffect.READ_ONLY,
                id="reason",
                parameters={"question": text},
            )
        ]

    # -- materialisation ------------------------------------------------------

    def _materialise(
        self, template: StepTemplate, decision: Mapping[str, Any]
    ) -> PlanStep:
        """Resolve the template's tool and carry the plan's policies with it.

        A step whose effect the policy requires verifying but which carries no
        check is left WITHOUT one on purpose: the executor then reports it as
        inconclusive, which is the truth. Attaching a made-up check here would
        manufacture the evidence this whole layer exists to demand.
        """
        return PlanStep(
            title=template.title,
            description=template.description,
            depends_on=_dedupe(template.depends_on),
            id=template.id,
            action=template.action,
            tool=self._tool_for(template.intent, decision),
            parameters=dict(template.parameters),
            expected_result=template.expected_result,
            verification=template.verification,
            effect=template.effect,
            parallel_safe=template.parallel_safe,
        )

    def _tool_for(self, intent: str, decision: Mapping[str, Any]) -> str:
        """The tool for an intent, or the decision's own answer for its intent.

        The decision already resolved a tool for the capability it selected
        (Phase 5's selection), and re-deriving it here would let the plan and
        the decision disagree about what runs. Where the decision has no answer,
        the catalog is asked; where neither has one, the step keeps an empty
        tool and the executor reports that nothing can carry it out.
        """
        selected = str(decision.get("selected_tool") or "")
        if selected and str(decision.get("selected_capability") or "") == intent:
            return selected
        try:
            return str(self._tool_lookup(intent) or "")
        except Exception:
            # An unknown intent name is not a plan failure: it means this
            # installation has nothing registered, which the executor reports.
            return ""


# --------------------------------------------------------------------------- #
# Implied steps
# --------------------------------------------------------------------------- #


def _locate_project(name: str) -> StepTemplate:
    label = name.strip() or "the project"
    return StepTemplate(
        title=f"Locate {label}" if name else "Locate project",
        implied=True,
        action="locate_project",
        intent="find_file",
        description=f"Find {label} on disk before anything tries to use it.",
        expected_result="A path that exists, or a clear 'not found'.",
        effect=StepEffect.READ_ONLY,
        id="locate-project",
        parameters={"project": name} if name else {},
        verification=VerificationSpec(
            method=VerificationMethod.FILE_EXISTS,
            target="",
            description="the located path exists on disk",
        ),
        parallel_safe=True,
    )


def _collect_output(run_step_id: str) -> StepTemplate:
    return StepTemplate(
        title="Collect output",
        implied=True,
        action="collect_output",
        intent="run_command",
        description="Capture the command's stdout, stderr and exit code.",
        expected_result="The full output and exit code, captured once.",
        effect=StepEffect.READ_ONLY,
        id="collect-output",
        depends_on=(run_step_id,),
        verification=VerificationSpec(
            method=VerificationMethod.STATE_OBSERVED,
            description="output was actually captured",
        ),
        parallel_safe=True,
    )


def _analyze_result(depends_on: str) -> StepTemplate:
    return StepTemplate(
        title="Analyze failures",
        implied=True,
        action="analyze_result",
        intent="answer_question",
        description="Read the captured output and identify what actually failed.",
        expected_result="The failing items, named, with the evidence for each.",
        effect=StepEffect.READ_ONLY,
        id="analyze-result",
        depends_on=(depends_on,) if depends_on else (),
    )


def _summarize_result(depends_on: str) -> StepTemplate:
    return StepTemplate(
        title="Summarize result",
        implied=True,
        action="summarize_result",
        intent="summarize",
        description="Answer the user in one short summary of what happened.",
        expected_result="A direct answer, including anything that failed or was not verified.",
        effect=StepEffect.READ_ONLY,
        id="summarize-result",
        depends_on=(depends_on,) if depends_on else (),
    )


# --------------------------------------------------------------------------- #
# Ordering helpers
# --------------------------------------------------------------------------- #


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    """Dependency edges, in order, without duplicates."""
    return tuple(dict.fromkeys(value for value in values if value))


def _unique(template: StepTemplate, used: set[str]) -> StepTemplate:
    """Give a repeated step kind its own id (two "open" clauses, two runs)."""
    if template.id not in used:
        return template
    index = 2
    while f"{template.id}-{index}" in used:
        index += 1
    return replace(template, id=f"{template.id}-{index}")


def _analysis_anchor(ordered: Sequence[StepTemplate]) -> str:
    """The step whose result an analysis step should read: the captured output,
    else whatever ran, else the last thing the plan did."""
    for action in ("collect_output", "run_tests", "run_command"):
        for template in ordered:
            if template.action == action:
                return template.id
    return ordered[-1].id if ordered else ""


def _index_after(ordered: Sequence[StepTemplate], step_id: str) -> int:
    for index, template in enumerate(ordered):
        if template.id == step_id:
            return index
    return len(ordered) - 1


def _chain(templates: Sequence[StepTemplate]) -> list[StepTemplate]:
    """Make every step that CHANGES something wait for the previous such step.

    Spoken steps are chained so two writes cannot swap order behind the user's
    back; read-only steps and implied steps are left alone, because an implied
    step's dependencies are exact and a reading has nothing to conflict with.
    """
    chained: list[StepTemplate] = []
    previous_writer = ""
    for template in templates:
        if not template.parallel_safe:
            needs_edge = (
                not template.implied
                and bool(previous_writer)
                and previous_writer not in template.depends_on
            )
            if needs_edge:
                template = replace(
                    template, depends_on=(*template.depends_on, previous_writer)
                )
            previous_writer = template.id
        chained.append(template)
    return chained


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _demands_clarification(decision: Mapping[str, Any], intent: object | None) -> bool:
    if str(decision.get("route", "")) == "clarify":
        return True
    if str(decision.get("reason_code", "")) == "clarification_needed":
        return True
    # Without a decision, the NLU's own flag is the only evidence there is.
    return bool(getattr(intent, "needs_clarification", None) is True and not decision)


def _decision_payload(decision: object | None) -> dict[str, Any]:
    if decision is None:
        return {}
    to_dict = getattr(decision, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        return dict(payload) if isinstance(payload, Mapping) else {}
    if isinstance(decision, Mapping):
        # Stored as safe operational metadata only: a plan carries WHY it was
        # built, never the reasoning behind it.
        return {str(key): value for key, value in decision.items()}
    return {}


def _target_for(kind: ClauseKind, text: str) -> str:
    if kind is ClauseKind.OPEN_APPLICATION:
        return _application_name(text)
    if kind in (ClauseKind.OPEN_PROJECT, ClauseKind.LOCATE):
        return _project_name(text)
    return ""


_APPLICATION_VERBS = re.compile(
    r"^(?:please\s+)?(?:open|launch|start|run)\s+(?:the\s+|my\s+)?", re.I
)
_TRAILING_NOISE = re.compile(r"\s+(?:please|for me|now|up|again)\s*$", re.I)


def _application_name(text: str) -> str:
    cleaned = _TRAILING_NOISE.sub("", text.strip())
    match = _OPEN_APPLICATION.search(cleaned)
    raw = match.group(2) if match else _APPLICATION_VERBS.sub("", cleaned)
    guard = raw.strip(" .") or ""
    # "open the project" is not an application; the project matcher owns it.
    if guard.lower() in ("project", "folder", "file", "tests", "test suite", "terminal"):
        return ""
    return _titlecase_name(guard)


def _project_name(text: str) -> str:
    match = _OPEN_PROJECT.search(text)
    if match:
        candidate = (match.group(3) or "").strip()
        return candidate
    cleaned = _VERB_PREFIX.sub("", text.strip())
    cleaned = re.sub(
        r"\b(?:project|workspace|repo|repository|folder|codebase)\b", "", cleaned, flags=re.I
    )
    cleaned = re.sub(r"^(?:my|the|our)\s+", "", cleaned.strip(), flags=re.I)
    return cleaned.strip(" .") or ""


def _metric_label(intent: str) -> str:
    return {
        "cpu_status": "CPU",
        "memory_status": "RAM",
        "gpu_status": "GPU",
        "battery_status": "battery",
        "network_status": "network",
        "system_info": "system specs",
    }.get(intent, intent.replace("_", " "))


def _test_scope(text: str) -> str:
    match = re.search(r"\b([\w./\\-]*(?:tests?|specs?)[\w./\\-]*)\b", text, re.I)
    return match.group(1) if match else "tests"


def _command_text(text: str) -> str:
    match = re.search(r"\b(?:run|execute)\s+(.+)$", text, re.I)
    return (match.group(1) if match else text).strip(" .")


def _titlecase_name(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return ""
    # Keep an acronym or an existing capitalisation ("VS Code", "GTA V") and
    # only fix an all-lowercase name ("chrome" -> "Chrome").
    if stripped.islower():
        return " ".join(word.capitalize() for word in stripped.split())
    return stripped
