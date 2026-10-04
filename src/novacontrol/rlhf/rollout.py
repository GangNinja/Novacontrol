"""Environments and rollouts: what a policy does, and what came back.

A rollout is the unit reinforcement learning consumes: a state, a sequence of
policy actions, the observations the environment returned, and the reward each
step earned. The abstraction is deliberately generic — an environment here is
anything with reset/step/observe/is_done/get_state/get_metadata, and it carries
no assumption that it is a game. A NovaControl task, a simulator, a benchmark
loop and a test fixture all fit the same interface.

Two safety properties are built in rather than bolted on:

  * The mock environment is DETERMINISTIC, in-memory and read-only. It never
    touches the filesystem, the network or another system, and the same seed
    produces the same episode every time — which is what makes rollouts testable
    at all.
  * Nothing in this module grants an action permission. An environment that
    fronts something real must go through Phase 8's permission layer; a policy
    cannot use a rollout to sidestep a gate, because the rollout layer has no
    gate to bypass — it only calls what the environment exposes.

Reward propagation is separate from collection: :class:`RewardPropagator` reads a
finished rollout and writes the step/intermediate/terminal/cumulative/discounted
reading onto a Phase 15 trajectory (whose schema gained the optional field for
exactly this). Phase 15's single total is untouched.
"""

from __future__ import annotations

import abc
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.evaluation.models import AgentTrajectory
from novacontrol.evaluation.reward import RewardResult
from novacontrol.rlhf.models import (
    RewardBreakdown,
    RewardSource,
    Rollout,
    RolloutStatus,
    RolloutStep,
)

#: A step reward, as a number or a mapping with ``total_reward``. ``None`` means
#: "this step earned nothing measurable", which is stored as 0 and reported.
StepRewardFn = Callable[
    [Mapping[str, Any], Mapping[str, Any], bool],
    "float | Mapping[str, float] | None",
]

#: How many times the same action may repeat before the mock reward calls it
#: inflation. Matches the integrity checker's default on purpose.
DEFAULT_REPEAT_LIMIT = 2

#: The shaped reward the built-in signal gives. Small on purpose: the terminal
#: outcome dominates, so a policy cannot farm step rewards to look successful.
STEP_REWARD = 0.1
REPEAT_PENALTY = 0.05
SUCCESS_REWARD = 1.0
FAILURE_REWARD = -1.0


class Environment(abc.ABC):
    """The minimal interface a rollout needs. Replaceable, and generic by design."""

    kind: str = "abstract"
    #: Whether stepping this environment can change anything outside the process.
    #: The mock is read-only; anything that is not must be gated elsewhere.
    read_only: bool = True

    @abc.abstractmethod
    def reset(
        self, *, task: Mapping[str, Any] | None = None, seed: int = 0
    ) -> Mapping[str, Any]:
        """Start a new episode and return the initial observation."""

    @abc.abstractmethod
    def step(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        """Apply one action and return the resulting observation."""

    @abc.abstractmethod
    def observe(self) -> Mapping[str, Any]:
        """The current observation, without changing state."""

    @abc.abstractmethod
    def is_done(self) -> bool:
        """Whether the episode has ended."""

    @abc.abstractmethod
    def get_state(self) -> Mapping[str, Any]:
        """The environment's own state (for checkpointing and inspection)."""

    def get_metadata(self) -> Mapping[str, Any]:
        """What this environment is, in structured form."""
        return {
            "kind": self.kind,
            "read_only": self.read_only,
            "deterministic": True,
        }


class MockEnvironment(Environment):
    """A deterministic, in-memory environment for dry runs and tests.

    It takes an optional SCRIPT of observations: step *i* returns the *i*-th
    entry, so a test can describe an episode exactly. An action never changes
    anything outside this object, and the same (task, seed, actions) triple
    always produces the same episode.
    """

    kind = "mock"
    read_only = True

    def __init__(
        self,
        script: Sequence[Mapping[str, Any]] = (),
        *,
        max_steps: int = 8,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.script = tuple(dict(item) for item in script)
        self.max_steps = max(1, int(max_steps))
        self._metadata = dict(metadata or {})
        self.task: dict[str, Any] = {}
        self.seed = 0
        self.index = 0
        self.done = False
        self.actions: list[dict[str, Any]] = []
        self.observation: dict[str, Any] = {}

    def reset(
        self, *, task: Mapping[str, Any] | None = None, seed: int = 0
    ) -> Mapping[str, Any]:
        self.task = dict(task or {})
        self.seed = int(seed)
        self.index = 0
        self.done = False
        self.actions = []
        self.observation = {
            "step": 0,
            "task": dict(self.task),
            "success": None,
            "done": False,
        }
        return dict(self.observation)

    def step(self, action: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.done:
            return dict(self.observation)
        self.actions.append(dict(action))
        scripted = self.script[self.index] if self.index < len(self.script) else {}
        self.index += 1
        observation: dict[str, Any] = {
            "step": self.index,
            "task": dict(self.task),
            "accepted": True,
            "success": None,
            "done": False,
        }
        observation.update(scripted)
        done = bool(observation.get("done")) or self.index >= self.max_steps
        observation["done"] = done
        # ``success`` is deliberately NOT invented here: a scripted observation
        # says what happened, and an episode that ended without saying keeps
        # ``None`` ("nobody decided") rather than a fabricated outcome.
        self.done = done
        self.observation = observation
        return dict(observation)

    def observe(self) -> Mapping[str, Any]:
        return dict(self.observation)

    def is_done(self) -> bool:
        return self.done

    def get_state(self) -> Mapping[str, Any]:
        return {
            "index": self.index,
            "done": self.done,
            "seed": self.seed,
            "task": dict(self.task),
            "actions": [dict(action) for action in self.actions],
        }

    def get_metadata(self) -> Mapping[str, Any]:
        return {
            **super().get_metadata(),
            "max_steps": self.max_steps,
            "scripted_steps": len(self.script),
            **self._metadata,
        }


class Policy(abc.ABC):
    """Anything that chooses an action from an observation."""

    policy_id: str = "abstract"

    @property
    def name(self) -> str:
        return self.policy_id

    @abc.abstractmethod
    def act(
        self, observation: Mapping[str, Any], state: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Choose the next action. Must be deterministic for a fixed input."""

    def reset(self) -> None:
        """Called before an episode; a stateful policy clears itself here."""
        return None


class ScriptedPolicy(Policy):
    """A policy that replays a fixed action list — the deterministic default.

    When the script runs out it repeats a no-op, so a rollout terminates on the
    environment's own limits rather than on an exception. This is what makes an
    episode reproducible without a model.
    """

    policy_id = "scripted"

    def __init__(
        self,
        actions: Sequence[Mapping[str, Any]] = (),
        *,
        policy_id: str = "",
    ) -> None:
        self.actions = tuple(dict(action) for action in actions)
        if policy_id:
            self.policy_id = policy_id
        self.index = 0

    def reset(self) -> None:
        self.index = 0

    def act(
        self, observation: Mapping[str, Any], state: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        del observation, state
        if self.index < len(self.actions):
            action = self.actions[self.index]
        else:
            action = {"action": "noop", "reason": "the script is exhausted"}
        self.index += 1
        return dict(action)


class CallablePolicy(Policy):
    """A policy implemented as a function, for tests and ad-hoc experiments."""

    def __init__(
        self,
        fn: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
        *,
        policy_id: str = "callable",
    ) -> None:
        self.fn = fn
        self.policy_id = policy_id

    def act(
        self, observation: Mapping[str, Any], state: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return dict(self.fn(observation, state))


@dataclass(frozen=True, slots=True)
class RolloutRunner:
    """Collects an episode, step by step, into a :class:`Rollout`.

    The runner owns no policy and no reward of its own: it walks whatever
    environment and policy it is given, and scores each step with the supplied
    function or with the deterministic built-in rule. That rule is small and
    observable — a terminal success is worth ``+1``, a failure ``-1``, a step the
    environment marked with ``reward`` carries it, and repeated actions pay a
    token penalty — which is exactly enough for tests and dry runs and is
    clearly not a learned reward.
    """

    max_steps: int = 8
    gamma: float = 1.0
    repeat_limit: int = DEFAULT_REPEAT_LIMIT
    source: str = RewardSource.RULE.value

    def run(
        self,
        environment: Environment,
        policy: Policy,
        *,
        task: Mapping[str, Any] | None = None,
        seed: int = 0,
        reward_fn: StepRewardFn | None = None,
        trajectory_id: str = "",
        task_id: str = "",
    ) -> Rollout:
        limit = max(1, int(self.max_steps))
        observation = environment.reset(task=task, seed=seed)
        policy.reset()
        steps: list[RolloutStep] = []
        seen: dict[str, int] = {}
        index = 0
        while not environment.is_done() and index < limit:
            action = dict(policy.act(observation, environment.get_state()))
            observation = dict(environment.step(action))
            name = str(action.get("action", ""))
            seen[name] = seen.get(name, 0) + 1
            repeats = seen[name] - 1
            done = environment.is_done()
            reward = self._step_reward(reward_fn, action, observation, done, repeats)
            steps.append(
                RolloutStep(
                    index=index,
                    action=action,
                    observation=observation,
                    reward=reward,
                    terminal=bool(done),
                    metadata={"repeat_count": repeats},
                )
            )
            index += 1
        status = RolloutStatus.COMPLETED.value
        if index >= limit and not environment.is_done():
            status = RolloutStatus.TRUNCATED.value
        total = sum(
            float(step.reward.get("total_reward", 0.0) or 0.0) for step in steps
        )
        return Rollout(
            task_id=task_id or str((task or {}).get("task_id", "")),
            trajectory_id=trajectory_id,
            environment=environment.kind,
            policy=policy.name,
            seed=int(seed),
            status=status,
            steps=tuple(steps),
            total_reward=total,
            source=self.source,
            metadata={
                "steps_run": len(steps),
                "read_only": environment.read_only,
                "environment": dict(environment.get_metadata()),
            },
        )

    def _step_reward(
        self,
        reward_fn: StepRewardFn | None,
        action: Mapping[str, Any],
        observation: Mapping[str, Any],
        done: bool,
        repeats: int,
    ) -> dict[str, Any]:
        if reward_fn is not None:
            value = reward_fn(action, observation, done)
            if isinstance(value, Mapping):
                return {str(key): float(item) for key, item in value.items()}
            if value is None:
                return {}
            return {"total_reward": float(value)}
        total = 0.0
        reasons: list[str] = []
        shaped = observation.get("reward")
        if isinstance(shaped, (int, float)) and not isinstance(shaped, bool):
            total += float(shaped)
            reasons.append("the environment reported a step reward")
        if done:
            if observation.get("success") is True:
                total += SUCCESS_REWARD
                reasons.append("the episode ended in success")
            elif observation.get("success") is False:
                total += FAILURE_REWARD
                reasons.append("the episode ended in failure")
        if repeats > self.repeat_limit:
            total -= REPEAT_PENALTY
            reasons.append("the same action was repeated beyond the tolerance")
        elif shaped is None and not done:
            total += STEP_REWARD
            reasons.append("the step was taken")
        return {
            "total_reward": round(total, 6),
            "reason": "; ".join(reasons) or "nothing measurable happened",
        }


def single_step_rollout(
    trajectory: AgentTrajectory,
    *,
    reward: RewardResult | None = None,
    source: str = "",
    task: Mapping[str, Any] | None = None,
) -> Rollout:
    """A recorded run as a one-step, terminal rollout.

    RL does not only learn from interactive episodes: most of NovaControl's
    experience is already recorded, and a trajectory IS an episode that happened
    once. This projects it into the same shape so the same machinery can consume
    it, without pretending the run was collected by a policy.
    """
    step = RolloutStep(
        index=0,
        action={"action": "recorded_run", "final_result": dict(trajectory.final_result)},
        observation={
            "success": trajectory.success,
            "status": trajectory.status,
            "done": True,
            "full_result": None,
        },
        reward=dict(reward.to_dict()) if reward is not None else {},
        terminal=True,
        metadata={"recorded": True},
    )
    return Rollout(
        task_id=trajectory.task_id,
        trajectory_id=trajectory.trajectory_id,
        environment="recorded_trajectory",
        policy=str(trajectory.model_information.get("model", "")),
        status=RolloutStatus.COMPLETED.value,
        steps=(step,),
        total_reward=float(reward.total_reward) if reward is not None else 0.0,
        source=source or (reward.reward_source if reward is not None else RewardSource.RULE.value),
        metadata={
            "recorded": True,
            "task": dict(task or trajectory.final_result),
            "read_only": True,
        },
    )


@dataclass(frozen=True, slots=True)
class RolloutSummary:
    """What a batch of rollouts did, in aggregate."""

    rollouts: int = 0
    steps: int = 0
    mean_reward: float = 0.0
    mean_length: float = 0.0
    truncated: int = 0
    by_environment: Mapping[str, int] = field(default_factory=dict)


def summarise(rollouts: Sequence[Rollout]) -> RolloutSummary:
    if not rollouts:
        return RolloutSummary()
    rewards = [rollout.total_reward for rollout in rollouts]
    by_environment: dict[str, int] = {}
    for rollout in rollouts:
        by_environment[rollout.environment] = by_environment.get(rollout.environment, 0) + 1
    return RolloutSummary(
        rollouts=len(rollouts),
        steps=sum(rollout.length for rollout in rollouts),
        mean_reward=sum(rewards) / len(rewards),
        mean_length=sum(rollout.length for rollout in rollouts) / len(rollouts),
        truncated=sum(1 for rollout in rollouts if rollout.status == RolloutStatus.TRUNCATED.value),
        by_environment=by_environment,
    )


class RewardPropagator:
    """Writes a rollout's reward breakdown onto the trajectory it came from.

    Phase 15's trajectory carries ONE total; Phase 18 adds the optional
    step/intermediate/terminal/cumulative/discounted reading BESIDE it, never
    instead of it. A recorder that already wrote a total keeps it, and a reader
    that only knows Phase 15 keeps working.
    """

    def __init__(self, *, gamma: float = 1.0) -> None:
        self.gamma = max(0.0, min(1.0, float(gamma)))

    def breakdown(self, rollout: Rollout) -> RewardBreakdown:
        return rollout.breakdown(gamma=self.gamma)

    def apply(self, rollout: Rollout, trajectory: AgentTrajectory) -> AgentTrajectory:
        """The same trajectory, with the RL reading attached."""
        return trajectory.with_reward_breakdown(
            self.breakdown(rollout).to_dict()
        )


__all__ = [
    "CallablePolicy",
    "DEFAULT_REPEAT_LIMIT",
    "Environment",
    "FAILURE_REWARD",
    "MockEnvironment",
    "Policy",
    "REPEAT_PENALTY",
    "RolloutRunner",
    "RolloutSummary",
    "RewardPropagator",
    "STEP_REWARD",
    "SUCCESS_REWARD",
    "ScriptedPolicy",
    "StepRewardFn",
    "single_step_rollout",
    "summarise",
]
