"""Deterministic environments: where an agentic episode happens.

Phase 20 ships no game, no simulator and no robot. What it ships is the generic
interface every future environment implements — reset / step / observe / verify /
available actions / difficulty / termination — plus six DETERMINISTIC
environments so the whole pipeline can be built, tested and dry-run on a machine
with no discrete GPU:

    simple          choose the one action that finishes the task      level 1
    multi_step      complete dependent steps in order                 level 2
    tool_selection  choose the CORRECT tool among decoys              level 3
    recovery        recover after a simulated tool failure            level 4
    planning        reach the goal within an efficient budget         level 5
    contextual      the right action depends on the context           level 6

Every one of them is in-memory, read-only and seed-free-deterministic: the same
(task, seed, actions) triple always produces the same episode. None of them
touches the filesystem, the network or another process, and none of them grants
an action permission — an environment that fronts something real must go through
the permission layer, exactly like every other action in NovaControl.

Future phases (visual environments, simulators, robotics, permitted games,
ARC-style interactive tasks, computer-use benchmarks) implement
:class:`AgenticEnvironment` and nothing else: the rollout manager, the reward
layer, the credit assignment, the curriculum and the evaluator are written
against this interface and never against a particular environment.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.agentic.actions import effect_for
from novacontrol.agentic.models import (
    ActionType,
    AgentAction,
    TaskDifficulty,
    TerminationReason,
    as_int,
    as_mapping,
    as_text,
)
from novacontrol.core.security import RiskLevel
from novacontrol.planning.models import StepEffect
from novacontrol.rlhf.rollout import Environment


@dataclass(frozen=True, slots=True)
class EnvironmentStep:
    """One action applied, and everything the environment observed about it."""

    index: int = 0
    action: AgentAction = field(default_factory=AgentAction)
    observation: Mapping[str, Any] = field(default_factory=dict)
    result: Mapping[str, Any] = field(default_factory=dict)
    verification: Mapping[str, Any] = field(default_factory=dict)
    done: bool = False
    success: bool | None = None
    termination_reason: str = ""
    error: str = ""
    #: The action name this task expected FOR THIS STEP, recorded when the step
    #: was applied. Reading it afterwards (from ``expected_action()``) would
    #: judge the step against whatever is expected NEXT, which is how a correct
    #: step got scored as the wrong tool.
    expected_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action.as_mapping(),
            "observation": dict(self.observation),
            "result": dict(self.result),
            "verification": dict(self.verification),
            "done": self.done,
            "success": self.success,
            "termination_reason": self.termination_reason,
            "error": self.error,
            "expected_action": self.expected_action,
        }


class AgenticEnvironment(Environment):
    """The generic environment: Phase 18's interface, extended for agents.

    Phase 18 already defined ``reset``/``step``/``observe``/``is_done``/
    ``get_state``/``get_metadata`` and this class KEEPS all of them — a Phase 18
    rollout runner can drive an agentic environment unchanged. What it adds is
    what an agentic loop needs: which actions are available here, what
    verification says, how hard the task is, and why the episode ended.
    """

    kind = "agentic"
    environment_id = "abstract"
    read_only = True
    #: Whether this environment's actions change anything. True for every
    #: environment in this module; an adapter over something real says False and
    #: is gated by the permission layer.
    writable = False

    def __init__(self, *, max_steps: int = 8, seed: int = 0, **_: Any) -> None:
        self.max_steps = max(1, int(max_steps))
        self.seed = int(seed)
        self.index = 0
        self.done = False
        self.termination_reason = ""
        self.success: bool | None = None
        self.task: dict[str, Any] = {}
        self.actions: list[AgentAction] = []
        self.steps: list[EnvironmentStep] = []
        self.observation: dict[str, Any] = {}
        self.resource_used = 0.0

    # -- Phase 18 interface ----------------------------------------------------

    def reset(self, *, task: Mapping[str, Any] | None = None, seed: int = 0) -> Mapping[str, Any]:
        self.task = as_mapping(task)
        self.seed = int(seed)
        self.index = 0
        self.done = False
        self.termination_reason = ""
        self.success = None
        self.actions = []
        self.steps = []
        self.resource_used = 0.0
        self.observation = self._initial_observation()
        return dict(self.observation)

    def step(self, action: Mapping[str, Any] | AgentAction) -> Mapping[str, Any]:
        if self.done:
            return dict(self.observation)
        agent_action = (
            action
            if isinstance(action, AgentAction)
            else AgentAction.from_dict(as_mapping(action))
        )
        self.actions.append(agent_action)
        self.index += 1
        step = self._apply(agent_action)
        self.steps.append(step)
        observation = dict(step.observation)
        observation.setdefault("step", self.index)
        observation["done"] = step.done
        observation["success"] = step.success
        if step.done:
            self.done = True
            self.termination_reason = step.termination_reason or (
                TerminationReason.SUCCESS.value
                if step.success
                else TerminationReason.FAILURE.value
            )
            self.success = step.success
        elif self.index >= self.max_steps:
            self.done = True
            self.termination_reason = TerminationReason.MAX_STEPS.value
            observation["done"] = True
        self.observation = observation
        return dict(observation)

    def observe(self) -> Mapping[str, Any]:
        return dict(self.observation)

    def is_done(self) -> bool:
        return self.done

    def get_state(self) -> Mapping[str, Any]:
        return {
            "environment_id": self.environment_id,
            "index": self.index,
            "done": self.done,
            "seed": self.seed,
            "task": dict(self.task),
            "termination_reason": self.termination_reason,
            "success": self.success,
            "resource_used": round(self.resource_used, 6),
            "internal": self._internal_state(),
        }

    def get_metadata(self) -> Mapping[str, Any]:
        return {
            **super().get_metadata(),
            "kind": self.kind,
            "environment_id": self.environment_id,
            "max_steps": self.max_steps,
            "difficulty": self.difficulty().to_dict(),
        }

    # -- the agentic extension -------------------------------------------------

    def goal(self) -> str:
        return as_text(self.task.get("goal"), self._goal())

    def available_actions(self) -> tuple[AgentAction, ...]:
        return tuple(self._available_actions())

    def accepts(self, state: Any, action: AgentAction) -> tuple[bool, str]:
        """Whether the environment will accept this action in this state."""
        del state
        return self._accepts(action)

    def verify(
        self,
        action: AgentAction,
        observation: Mapping[str, Any],
        result: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """What verification says about the most recent step.

        The verdict describes THIS action's outcome; it is never invented for an
        action the environment did not see. A caller asking about an older action
        gets a ``skipped`` verdict with the reason, because a check nobody ran is
        not a pass.
        """
        del observation
        for step in reversed(self.steps):
            if step.action.action_id == action.action_id:
                verdict = dict(step.verification)
                if result is not None:
                    verdict["reported_result"] = dict(result)
                return verdict
        return {
            "status": "skipped",
            "reason": "this action has not been applied, so there is nothing to verify",
            "expectation": "",
        }

    def requires_verification(self, action: AgentAction) -> bool:
        """Whether this action's outcome MUST be checked.

        Everything except "do nothing" is checked: an action that changed
        something needs to be observed, and an action that gathered information
        needs to say whether it found anything. An environment that cannot check
        a particular action returns a ``skipped`` verdict, which is recorded as
        not-a-pass rather than rounded up.
        """
        if effect_for(action.action_type) is not StepEffect.READ_ONLY:
            return True
        return bool(action.expected_effect) or action.action_type != ActionType.NOOP.value

    def difficulty(self) -> TaskDifficulty:
        return self._difficulty()

    def expected_action(self) -> str:
        """The action name this state expects next, when the task knows it.

        Empty means "this task does not single out one correct action", and the
        reward layer then scores tool correctness from what it observed rather
        than from a fact nobody stated.
        """
        return ""

    def resource_state(self) -> Mapping[str, Any]:
        return {
            "used": round(self.resource_used, 6),
            "steps": self.index,
            "max_steps": self.max_steps,
        }

    def failure_reason(self) -> str:
        """Why the episode ended in failure, from the last step that said."""
        for step in reversed(self.steps):
            if step.error:
                return step.error
        return ""

    # -- what a subclass implements --------------------------------------------

    def _initial_observation(self) -> dict[str, Any]:
        raise NotImplementedError

    def _apply(self, action: AgentAction) -> EnvironmentStep:
        raise NotImplementedError

    def _available_actions(self) -> Sequence[AgentAction]:
        raise NotImplementedError

    def _difficulty(self) -> TaskDifficulty:
        return TaskDifficulty()

    def _goal(self) -> str:
        return ""

    def _accepts(self, action: AgentAction) -> tuple[bool, str]:
        available = {item.action_id for item in self._available_actions()}
        if available and action.action_id not in available:
            # A caller may build the same action with a different declaration (no
            # expected effect, another label), which is a different id. Compare by
            # name too: the environment accepts what it OFFERS, not only its own
            # instances. The mask stays stricter — it authorizes exact content.
            names = {item.name for item in self._available_actions()}
            if action.name not in names:
                return False, f"the environment does not offer action {action.name!r} here"
        return True, ""

    def _internal_state(self) -> dict[str, Any]:
        return {}


def _verdict(status: str, expectation: str, reason: str, **extra: Any) -> dict[str, Any]:
    """A verification verdict in the verifier's own vocabulary (pass/fail/…).

    An environment states what it OBSERVED about one action. It never awards
    itself a success: ``success`` is set only when the episode actually ends.
    """
    return {
        "status": status,
        "expectation": expectation,
        "reason": reason,
        **extra,
    }


# ── level 1: simple ──────────────────────────────────────────────────────────


class SimpleEnvironment(AgenticEnvironment):
    """One action finishes the task; the rest are decoys that cost a step."""

    kind = "deterministic"
    environment_id = "simple"

    def _goal(self) -> str:
        return "Complete the task with the correct action"

    def _initial_observation(self) -> dict[str, Any]:
        return {
            "step": 0,
            "task": dict(self.task),
            "goal": self.goal(),
            "status": "ready",
            "completed": False,
        }

    def _available_actions(self) -> Sequence[AgentAction]:
        return (
            _action("complete", "finish the task correctly", expected_effect="local_write"),
            _action("wait", "do nothing useful", ActionType.NOOP.value),
            _action("repeat", "perform an unnecessary repeat", expected_effect="local_write"),
        )

    def _apply(self, action: AgentAction) -> EnvironmentStep:
        correct = action.tool == "complete"
        done = correct
        observation = {
            "status": "completed" if correct else "idle",
            "completed": correct,
            "step": self.index,
        }
        return EnvironmentStep(
            index=self.index,
            action=action,
            observation=observation,
            result={"tool": action.tool, "applied": True},
            verification=(
                _verdict(
                    "pass",
                    "the task is complete",
                    "the completing action was applied",
                )
                if correct
                else _verdict(
                    "fail",
                    "the task is complete",
                    f"{action.tool!r} does not complete the task",
                )
            ),
            done=done,
            success=True if done else None,
            termination_reason=TerminationReason.SUCCESS.value if done else "",
            error="" if correct else f"{action.tool!r} made no progress",
            expected_action="complete",
        )

    def expected_action(self) -> str:
        return "complete"

    def _difficulty(self) -> TaskDifficulty:
        # One tool finishes the task; the other two candidates are decoys, and a
        # decoy is what makes acting badly possible rather than a tool the task
        # needs — so the tool count is 1 and the branching factor is 3.
        return TaskDifficulty(steps=1, tools=1, branching_factor=3, planning_horizon=1)


# ── level 2: multi-step ──────────────────────────────────────────────────────


class MultiStepEnvironment(AgenticEnvironment):
    """Dependent steps: read needs open, write needs read."""

    kind = "deterministic"
    environment_id = "multi_step"

    ORDER: tuple[str, ...] = ("open", "read", "write")

    def _goal(self) -> str:
        target = as_text(self.task.get("target"), "report.txt")
        return f"Open, read and write {target}"

    def _initial_observation(self) -> dict[str, Any]:
        self._done_steps: list[str] = []
        return {
            "step": 0,
            "task": dict(self.task),
            "goal": self.goal(),
            "completed_steps": [],
            "next_expected": self.ORDER[0],
        }

    def _available_actions(self) -> Sequence[AgentAction]:
        return tuple(
            _action(name, f"{name} the task target", expected_effect="local_write")
            for name in self.ORDER
        ) + (_action("skip", "skip ahead", ActionType.NOOP.value),)

    def _apply(self, action: AgentAction) -> EnvironmentStep:
        expected = self._next_expected()
        expected_for_step = expected or ""
        applied = action.tool
        error = ""
        progress = applied == expected
        if applied == "skip":
            error = "skipping ahead does nothing"
        elif not progress:
            error = f"{applied!r} ran out of order; {expected!r} was required first"
        else:
            self._done_steps.append(applied)
        done = self._next_expected() is None
        self.resource_used += 0.1
        observation = {
            "step": self.index,
            "completed_steps": list(self._done_steps),
            "next_expected": self._next_expected(),
            "status": "completed" if done else "in_progress",
        }
        return EnvironmentStep(
            index=self.index,
            action=action,
            observation=observation,
            result={"tool": applied, "progress": progress, "steps_done": len(self._done_steps)},
            verification=_verdict(
                "pass" if progress else "fail",
                f"{expected!r} completes the next dependent step",
                "the step was applied in order" if progress else error,
            ),
            done=done,
            success=True if done else None,
            termination_reason=TerminationReason.SUCCESS.value if done else "",
            error=error,
            expected_action=expected_for_step,
        )

    def expected_action(self) -> str:
        return self._next_expected() or ""

    def _next_expected(self) -> str | None:
        for name in self.ORDER:
            if name not in self._done_steps:
                return name
        return None

    def _internal_state(self) -> dict[str, Any]:
        return {"done_steps": list(getattr(self, "_done_steps", []))}

    def _difficulty(self) -> TaskDifficulty:
        return TaskDifficulty(steps=3, tools=4, branching_factor=4, planning_horizon=3)


# ── level 3: tool selection ──────────────────────────────────────────────────


class ToolSelectionEnvironment(AgenticEnvironment):
    """Pick the RIGHT tool: several look plausible, one is correct."""

    kind = "deterministic"
    environment_id = "tool_selection"

    def _hint_text(self) -> str:
        return as_text(self.task.get("hint"), "search the files for the answer")

    def _goal(self) -> str:
        query = as_text(self.task.get("query"), "the target")
        return f"Find {query}: {self._hint_text()}"

    def _initial_observation(self) -> dict[str, Any]:
        self._correct_tool = as_text(self.task.get("correct_tool"), "search_files")
        return {
            "step": 0,
            "task": dict(self.task),
            "goal": self.goal(),
            # The description is CONTEXT the agent can read; the answer
            # (``correct_tool``) is not published here, it is what the reward
            # layer measures against. A policy has to read the description.
            "hint": self._hint_text(),
            "tools": ["search_web", "search_files", "summarise", "noop"],
            "found": False,
        }

    def _available_actions(self) -> Sequence[AgentAction]:
        return (
            _action("search_web", "search the web for the target", ActionType.INFORMATION.value, information=True),
            _action("search_files", "search local files for the target", ActionType.INFORMATION.value, information=True),
            _action("summarise", "summarise what is already known", ActionType.LOCAL_COMPUTATION.value),
            _action("noop", "do nothing", ActionType.NOOP.value),
        )

    def _apply(self, action: AgentAction) -> EnvironmentStep:
        correct = action.tool == self._correct_tool
        done = correct
        observation = {
            "step": self.index,
            "found": correct,
            "tool": action.tool,
            "status": "found" if correct else "searching",
        }
        self.resource_used += 0.2
        return EnvironmentStep(
            index=self.index,
            action=action,
            observation=observation,
            result={"tool": action.tool, "correct_tool": correct},
            verification=_verdict(
                "pass" if correct else "fail",
                f"the correct tool ({self._correct_tool}) finds the target",
                "the correct tool was chosen" if correct else f"{action.tool!r} is the wrong tool for this task",
            ),
            done=done,
            success=True if done else None,
            termination_reason=TerminationReason.SUCCESS.value if done else "",
            error="" if correct else f"the wrong tool {action.tool!r} was selected",
            expected_action=self._correct_tool,
        )

    def expected_action(self) -> str:
        return self._correct_tool

    def _difficulty(self) -> TaskDifficulty:
        return TaskDifficulty(
            steps=2, tools=4, branching_factor=4, planning_horizon=2, verification_complexity=0.25
        )


# ── level 4: recovery ────────────────────────────────────────────────────────


class RecoveryEnvironment(AgenticEnvironment):
    """A tool fails a configurable number of times, then succeeds.

    The failure is deterministic (a count, not a coin), so "did the agent recover
    within the retry ceiling" is an exact, testable question.
    """

    kind = "deterministic"
    environment_id = "recovery"

    def _goal(self) -> str:
        return f"Complete the flaky action (it fails {self._failures_before_success} time(s))"

    def _initial_observation(self) -> dict[str, Any]:
        self._failures_before_success = max(0, as_int(self.task.get("failures"), 1))
        self._attempts = 0
        return {
            "step": 0,
            "task": dict(self.task),
            "goal": self.goal(),
            "attempts": 0,
            "status": "ready",
        }

    def _available_actions(self) -> Sequence[AgentAction]:
        return (
            _action("flaky_action", "perform the flaky action", expected_effect="local_write"),
            _action("give_up", "stop trying", ActionType.NOOP.value),
        )

    def _apply(self, action: AgentAction) -> EnvironmentStep:
        if action.tool != "flaky_action":
            return EnvironmentStep(
                index=self.index,
                action=action,
                observation={"step": self.index, "status": "abandoned"},
                result={"tool": action.tool, "applied": False},
                verification=_verdict("fail", "the flaky action completes", "the action was abandoned"),
                done=True,
                success=False,
                termination_reason=TerminationReason.FAILURE.value,
                error="the agent gave up instead of recovering",
                expected_action="flaky_action",
            )
        self._attempts += 1
        self.resource_used += 0.3
        failed = self._attempts <= self._failures_before_success
        if failed:
            return EnvironmentStep(
                index=self.index,
                action=action,
                observation={"step": self.index, "attempts": self._attempts, "status": "failed"},
                result={
                    "tool": action.tool,
                    "attempt": self._attempts,
                    "error": f"transient failure {self._attempts} of {self._failures_before_success}",
                },
                verification=_verdict(
                    "fail",
                    "the flaky action completes",
                    f"transient failure on attempt {self._attempts}",
                ),
                done=False,
                success=None,
                error=f"transient failure {self._attempts}",
                expected_action="flaky_action",
            )
        return EnvironmentStep(
            index=self.index,
            action=action,
            observation={"step": self.index, "attempts": self._attempts, "status": "completed"},
            result={"tool": action.tool, "attempt": self._attempts, "recovered": self._attempts > 1},
            verification=_verdict(
                "pass",
                "the flaky action completes",
                f"succeeded on attempt {self._attempts}",
            ),
            done=True,
            success=True,
            termination_reason=TerminationReason.SUCCESS.value,
            expected_action="flaky_action",
        )

    def expected_action(self) -> str:
        return "flaky_action"

    def _internal_state(self) -> dict[str, Any]:
        return {"attempts": getattr(self, "_attempts", 0)}

    def _difficulty(self) -> TaskDifficulty:
        return TaskDifficulty(
            steps=3, tools=2, branching_factor=2, recovery_required=True, planning_horizon=3
        )


# ── level 5: planning ────────────────────────────────────────────────────────


class PlanningEnvironment(AgenticEnvironment):
    """Reach the goal within an efficient step budget by choosing a path."""

    kind = "deterministic"
    environment_id = "planning"

    def _goal(self) -> str:
        return f"Reach the target in at most {self._budget()} step(s)"

    def _budget(self) -> int:
        return max(1, as_int(self.task.get("budget"), 3))

    def _initial_observation(self) -> dict[str, Any]:
        self._progress = 0
        self._detours = 0
        return {
            "step": 0,
            "task": dict(self.task),
            "goal": self.goal(),
            "progress": 0,
            "budget": self._budget(),
        }

    def _available_actions(self) -> Sequence[AgentAction]:
        return (
            _action("advance", "advance toward the target", expected_effect="local_write"),
            _action("detour", "take an unnecessary detour", ActionType.LOCAL_COMPUTATION.value),
            _action("wait", "wait", ActionType.NOOP.value),
        )

    def _apply(self, action: AgentAction) -> EnvironmentStep:
        progress = action.tool == "advance"
        if progress:
            self._progress += 1
        elif action.tool == "detour":
            self._detours += 1
        self.resource_used += 0.15
        reached = self._progress >= self._budget()
        over_budget = not reached and self.index >= self._budget()
        done = reached or over_budget
        observation = {
            "step": self.index,
            "progress": self._progress,
            "detours": self._detours,
            "budget": self._budget(),
            "status": "reached" if reached else ("over_budget" if over_budget else "in_progress"),
        }
        if done and not reached:
            return EnvironmentStep(
                index=self.index,
                action=action,
                observation=observation,
                result={"progress": self._progress, "budget": self._budget()},
                verification=_verdict(
                    "fail",
                    "the target is reached inside the budget",
                    f"{self.index} step(s) were spent without reaching the target",
                ),
                done=True,
                success=False,
                termination_reason=TerminationReason.FAILURE.value,
                error="the planning budget was exhausted",
                expected_action="advance",
            )
        return EnvironmentStep(
            index=self.index,
            action=action,
            observation=observation,
            result={
                "progress": self._progress,
                "progressed": progress,
                "detours": self._detours,
            },
            verification=_verdict(
                "pass" if progress else "fail",
                "each step advances toward the target",
                "the step advanced the task" if progress else f"{action.tool!r} did not advance the task",
            ),
            done=reached,
            success=True if reached else None,
            termination_reason=TerminationReason.SUCCESS.value if reached else "",
            error="" if progress else f"{action.tool!r} wasted a step",
            expected_action="advance",
        )

    def expected_action(self) -> str:
        return "advance"

    def _internal_state(self) -> dict[str, Any]:
        return {"progress": self._progress, "detours": self._detours}

    def _difficulty(self) -> TaskDifficulty:
        return TaskDifficulty(
            steps=self._budget(),
            tools=3,
            branching_factor=3,
            planning_horizon=max(6, self._budget()),
            verification_complexity=0.25,
        )


# ── level 6: contextual ──────────────────────────────────────────────────────


class ContextualEnvironment(AgenticEnvironment):
    """The correct action depends on a clue in the context, not on the goal text.

    This is the smallest honest version of "ambiguous or context-dependent": two
    tasks look identical until the context says which one is meant, and the agent
    has to READ the context to choose. Nothing here guesses for the agent.
    """

    kind = "deterministic"
    environment_id = "contextual"

    def _goal(self) -> str:
        return "Choose the action the context asks for"

    def _initial_observation(self) -> dict[str, Any]:
        self._expected = as_text(self.task.get("expected_tool"), "alpha")
        self._context = as_mapping(self.task.get("context"))
        return {
            "step": 0,
            "task": dict(self.task),
            "goal": self.goal(),
            "context": dict(self._context),
            "clue": as_text(self._context.get("clue")),
            "status": "ready",
        }

    def _available_actions(self) -> Sequence[AgentAction]:
        return (
            _action("alpha", "take the alpha path", ActionType.TOOL.value),
            _action("beta", "take the beta path", ActionType.TOOL.value),
            _action("gamma", "take the gamma path", ActionType.TOOL.value),
        )

    def _apply(self, action: AgentAction) -> EnvironmentStep:
        correct = action.tool == self._expected
        done = correct
        self.resource_used += 0.2
        observation = {
            "step": self.index,
            "chosen": action.tool,
            "status": "matched" if correct else "mismatched",
        }
        return EnvironmentStep(
            index=self.index,
            action=action,
            observation=observation,
            result={"tool": action.tool, "matched_context": correct},
            verification=_verdict(
                "pass" if correct else "fail",
                f"the context asks for {self._expected!r}",
                "the context clue was followed" if correct else f"{action.tool!r} contradicts the context clue",
            ),
            done=done,
            success=True if done else None,
            termination_reason=TerminationReason.SUCCESS.value if done else "",
            error="" if correct else f"{action.tool!r} contradicts the context clue",
            expected_action=self._expected,
        )

    def expected_action(self) -> str:
        return self._expected

    def _difficulty(self) -> TaskDifficulty:
        return TaskDifficulty(
            steps=2,
            tools=3,
            branching_factor=3,
            ambiguity=0.6,
            planning_horizon=2,
            verification_complexity=0.3,
        )


# ── registry ─────────────────────────────────────────────────────────────────


def _action(
    tool: str,
    label: str,
    action_type: str = ActionType.TOOL.value,
    *,
    information: bool = False,
    expected_effect: str = "",
) -> AgentAction:
    """One candidate action, built the same way everywhere in this module."""
    if information and action_type == ActionType.TOOL.value:
        action_type = ActionType.INFORMATION.value
    return AgentAction(
        action_type=action_type,
        capability=tool,
        tool=tool,
        label=label,
        expected_effect=expected_effect,
        risk_level=RiskLevel.LOW,
        reversible=True,
        requires_confirmation=False,
        metadata={"deterministic": True},
    )


#: The environments this phase ships, one per curriculum level. A future
#: environment registers itself here and gets the whole pipeline for free.
ENVIRONMENT_FACTORIES: Mapping[str, Callable[..., AgenticEnvironment]] = {
    "simple": SimpleEnvironment,
    "multi_step": MultiStepEnvironment,
    "tool_selection": ToolSelectionEnvironment,
    "recovery": RecoveryEnvironment,
    "planning": PlanningEnvironment,
    "contextual": ContextualEnvironment,
}

#: The level each built-in environment is meant to exercise.
ENVIRONMENT_LEVELS: Mapping[str, int] = {
    "simple": 1,
    "multi_step": 2,
    "tool_selection": 3,
    "recovery": 4,
    "planning": 5,
    "contextual": 6,
}


def environment_names() -> tuple[str, ...]:
    return tuple(ENVIRONMENT_FACTORIES)


def build_environment(name: str, **kwargs: Any) -> AgenticEnvironment:
    """The environment one name asks for, or a clear refusal."""
    wanted = as_text(name)
    factory = ENVIRONMENT_FACTORIES.get(wanted)
    if factory is None:
        raise KeyError(
            f"unknown environment {wanted!r}: this phase ships "
            + ", ".join(environment_names())
            + ". A future environment implements AgenticEnvironment and registers here."
        )
    return factory(**kwargs)


def environments_for_level(level: int) -> tuple[str, ...]:
    """Every built-in environment at or below one curriculum level."""
    ceiling = max(1, int(level))
    return tuple(
        sorted(
            (name for name, value in ENVIRONMENT_LEVELS.items() if value <= ceiling),
            key=lambda name: (ENVIRONMENT_LEVELS[name], name),
        )
    )


def environment_specs() -> dict[str, dict[str, Any]]:
    """The environments, described without building them."""
    specs: dict[str, dict[str, Any]] = {}
    for name, factory in ENVIRONMENT_FACTORIES.items():
        probe = factory(max_steps=1)
        specs[name] = {
            "name": name,
            "level": ENVIRONMENT_LEVELS[name],
            "kind": probe.kind,
            "read_only": probe.read_only,
            "difficulty": probe.difficulty().to_dict(),
            "actions": [item.tool for item in probe.available_actions()],
            "deterministic": True,
            "requires_cuda": False,
        }
    return specs


__all__ = [
    "ENVIRONMENT_FACTORIES",
    "ENVIRONMENT_LEVELS",
    "AgenticEnvironment",
    "ContextualEnvironment",
    "EnvironmentStep",
    "MultiStepEnvironment",
    "PlanningEnvironment",
    "RecoveryEnvironment",
    "SimpleEnvironment",
    "ToolSelectionEnvironment",
    "build_environment",
    "environment_names",
    "environment_specs",
    "environments_for_level",
]
