"""The tactical layer — what to attempt next, and how to recover when blocked.

This is deliberately SMALL, and deliberately not a planner. Phase 24 owns
planning: candidate generation, dependency ordering, ranking, preconditions,
policy and replanning. What Phase 24 does not know is the shape of a grid, so
this module answers exactly two questions and nothing else:

* **Given an observation, what is the next bounded attempt?** (a direction that
  reduces the distance to the target, or a wait when nothing can move)
* **Given a failure, what is a DIFFERENT attempt?** (a side-step around a wall
  or boundary, with the failed direction remembered so it is not repeated)

Every answer is a list of Phase 24 :class:`~novacontrol.planning.goals.Task`
objects, which is what makes the tactical layer compose with planning instead
of competing with it: the tasks carry preconditions and effects, so the goal
manager still validates, orders, authorizes and verifies them.

Nothing here claims universal navigation. It routes on a grid when it can read
one, and says so when it cannot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from novacontrol.embodied.models import Observation
from novacontrol.embodied.perception import FACT_PREFIX
from novacontrol.planning.models import StepEffect, VerificationMethod, VerificationSpec

#: A direction and its vector — the tactical layer's whole vocabulary.
_VECTORS: dict[str, tuple[int, int]] = {
    "north": (0, -1),
    "south": (0, 1),
    "east": (1, 0),
    "west": (-1, 0),
}

#: Perpendicular options, tried in this order when a direction is blocked.
_SIDE_STEPS: dict[str, tuple[str, ...]] = {
    "north": ("east", "west"),
    "south": ("west", "east"),
    "east": ("north", "south"),
    "west": ("south", "north"),
}


@dataclass
class TacticalMemory:
    """What the session remembers between attempts — and nothing more.

    A blocked direction is remembered so the same wall is not walked into
    twice, which is also the duplicate-action guard: an attempt identical to
    the one that just failed is not re-sent.
    """

    blocked: set[tuple[str, tuple[int, int]]] = field(default_factory=set)
    failed_directions: dict[tuple[int, int], set[str]] = field(default_factory=dict)
    #: ``(position, direction)`` attempts already made, for the repeat check.
    attempts: list[tuple[tuple[int, int], str]] = field(default_factory=list)

    def remember_blocked(self, position: tuple[int, int], direction: str) -> None:
        self.blocked.add((direction, position))
        self.failed_directions.setdefault(position, set()).add(direction)

    def remember_attempt(self, position: tuple[int, int], direction: str) -> None:
        self.attempts.append((position, direction))

    def is_blocked(self, position: tuple[int, int], direction: str) -> bool:
        return (direction, position) in self.blocked

    def repeats(self, position: tuple[int, int], direction: str) -> int:
        """How many times this exact attempt was already made."""
        return sum(1 for item in self.attempts if item == (position, direction))

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked": sorted([direction, list(position)] for direction, position in self.blocked),
            "failed_directions": {
                f"{key[0]},{key[1]}": sorted(value) for key, value in self.failed_directions.items()
            },
            "attempts": len(self.attempts),
        }


@dataclass(frozen=True, slots=True)
class TacticalChoice:
    """One tactical decision: what to attempt, or why nothing can be attempted."""

    kind: str = "move"  # move | interact | wait | none
    direction: str = ""
    reason: str = ""
    #: The direction that was just blocked, when this choice routes around one.
    recovery_from: str = ""
    position: tuple[int, int] | None = None
    target: tuple[int, int] | None = None
    distance: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "direction": self.direction,
            "reason": self.reason,
            "recovery_from": self.recovery_from,
            "position": list(self.position) if self.position is not None else None,
            "target": list(self.target) if self.target is not None else None,
            "distance": self.distance,
        }


def read_position(observation: Observation) -> tuple[int, int] | None:
    """The agent's own position, from STRUCTURED state only.

    A fogged or failed reading does not know where the agent is, and returning
    a guess here would be the first step of every later lie.
    """
    state = dict(observation.structured or {})
    x = state.get(f"{FACT_PREFIX}agent.x", state.get("agent.x"))
    y = state.get(f"{FACT_PREFIX}agent.y", state.get("agent.y"))
    if x is None or y is None:
        return None
    try:
        return (int(x), int(y))
    except (TypeError, ValueError):
        return None


def read_target(observation: Observation) -> tuple[int, int] | None:
    state = dict(observation.structured or {})
    x = state.get("target.x")
    y = state.get("target.y")
    if x is None or y is None:
        return None
    try:
        return (int(x), int(y))
    except (TypeError, ValueError):
        return None


def read_collected(observation: Observation) -> bool:
    state = dict(observation.structured or {})
    return bool(state.get("target.collected", False))


def choose(
    observation: Observation,
    *,
    want: str = "reach",
    memory: TacticalMemory | None = None,
    last_direction: str = "",
) -> TacticalChoice:
    """What to attempt next, given a FRESH observation.

    ``want`` names the subgoal: ``reach`` moves toward the target cell,
    ``collect`` interacts once standing on it. Anything unreadable is refused
    with the reason rather than guessed at.
    """
    memory = memory or TacticalMemory()
    if not observation.usable_for_decision:
        return TacticalChoice(
            kind="none",
            reason=(
                f"the observation is {observation.freshness.value}, so no "
                "state-dependent attempt can be chosen from it; take a fresh "
                "reading first"
            ),
        )
    if observation.basis.value != "structured":
        return TacticalChoice(
            kind="none",
            reason=(
                "this observation has no structured state (regions only), so the "
                "agent cannot say where it is or where the target is; it will not "
                "navigate on a guess"
            ),
        )
    position = read_position(observation)
    target = read_target(observation)
    if position is None or target is None:
        return TacticalChoice(
            kind="none",
            reason="the observation does not state the agent's position or the target's",
        )
    distance = abs(position[0] - target[0]) + abs(position[1] - target[1])
    if want == "collect" and position == target:
        if read_collected(observation):
            return TacticalChoice(
                kind="none",
                reason="the target is already collected; nothing left to interact with",
                position=position,
                target=target,
                distance=0,
            )
        return TacticalChoice(
            kind="interact",
            reason="standing on the target cell; collect it",
            position=position,
            target=target,
            distance=0,
        )
    if position == target:
        return TacticalChoice(
            kind="none",
            reason="the agent is already on the target cell",
            position=position,
            target=target,
            distance=0,
        )
    preference = _directions_toward(position, target)
    for direction in preference:
        if memory.is_blocked(position, direction):
            continue
        if last_direction and direction == last_direction and memory.repeats(position, direction):
            # The identical attempt just failed: refusing to repeat it is the
            # duplicate-action guard, not a lost opportunity.
            continue
        return TacticalChoice(
            kind="move",
            direction=direction,
            reason=f"move {direction} to close the distance ({distance} cells)",
            position=position,
            target=target,
            distance=distance,
        )
    # Every useful direction is known blocked — try a side-step that is not.
    for direction in preference:
        for side in _SIDE_STEPS.get(direction, ()):
            if not memory.is_blocked(position, side):
                return TacticalChoice(
                    kind="move",
                    direction=side,
                    reason=(
                        f"the way {direction} is blocked, so {side} is the remaining way around"
                    ),
                    recovery_from=direction,
                    position=position,
                    target=target,
                    distance=distance,
                )
    return TacticalChoice(
        kind="none",
        reason=(
            "every direction from this cell is known blocked; the agent is stuck "
            "and needs a different objective or a fresh reading"
        ),
        position=position,
        target=target,
        distance=distance,
    )


def tasks_for(
    choice: TacticalChoice,
    *,
    session_id: str,
    environment_id: str,
    position: tuple[int, int] | None = None,
    max_input_ms: int = 500,
    attempt: int = 1,
) -> list[Any]:
    """The Phase 24 tasks for one tactical choice.

    Kept here, beside the choice, so an action and its declared preconditions
    cannot drift apart. The preconditions are deliberately the FACTS the
    environment reported: the executor's fresh-state gate re-grounds them
    immediately before the input, which is the stale-state rule enforced by the
    machinery that already exists.
    """
    from novacontrol.planning.goals import Task

    if choice.kind == "none":
        return []
    where = position or choice.position or (0, 0)
    if choice.kind == "interact":
        task_id = f"collect-{attempt}"
        # ``choose`` only returns ``interact`` with a target located, but the
        # fallback keeps the task buildable rather than raising here: a choice
        # without a target falls back to the cell the agent is standing on.
        target = choice.target if choice.target is not None else where
        return [
            Task(
                id=task_id,
                title=f"Collect the target at {target[0]},{target[1]}",
                description=(
                    "Interact with the target cell the environment reported. "
                    "Idempotent: asking twice is not an error."
                ),
                action="environment_input",
                tool="environment_input",
                parameters={
                    "session_id": session_id,
                    "environment_id": environment_id,
                    "input": "interact",
                    "duration_ms": max_input_ms,
                    "expected": f"{FACT_PREFIX}target.collected=true",
                    "position": [target[0], target[1]],
                },
                preconditions=(
                    f"{FACT_PREFIX}agent.x={where[0]}",
                    f"{FACT_PREFIX}agent.y={where[1]}",
                ),
                effects=(f"{FACT_PREFIX}target.collected=true",),
                expected_outputs=(f"{FACT_PREFIX}target.collected=true",),
                verification=VerificationSpec(
                    method=VerificationMethod.STATE_OBSERVED,
                    description="the target must be observed collected afterwards",
                    expect=f"{FACT_PREFIX}target.collected=true",
                ),
                effect=StepEffect.LOCAL_WRITE,
                estimated_seconds=0.2,
                estimated_cost=1.0,
            )
        ]
    dx, dy = _VECTORS[choice.direction]
    landing = (where[0] + dx, where[1] + dy)
    task_id = f"move-{choice.direction}-{attempt}"
    return [
        Task(
            id=task_id,
            title=f"Move {choice.direction} to {landing[0]},{landing[1]}",
            description=(f"One bounded move {choice.direction}. {choice.reason}"),
            action="environment_input",
            tool="environment_input",
            parameters={
                "session_id": session_id,
                "environment_id": environment_id,
                "input": "move",
                "direction": choice.direction,
                "duration_ms": max_input_ms,
                "expected": f"{FACT_PREFIX}agent.x={landing[0]},{FACT_PREFIX}agent.y={landing[1]}",
                "position": [where[0], where[1]],
                "landing": [landing[0], landing[1]],
            },
            preconditions=(
                f"{FACT_PREFIX}agent.x={where[0]}",
                f"{FACT_PREFIX}agent.y={where[1]}",
            ),
            effects=(
                f"{FACT_PREFIX}agent.x={landing[0]}",
                f"{FACT_PREFIX}agent.y={landing[1]}",
            ),
            expected_outputs=(
                f"{FACT_PREFIX}agent.x={landing[0]}",
                f"{FACT_PREFIX}agent.y={landing[1]}",
            ),
            verification=VerificationSpec(
                method=VerificationMethod.STATE_OBSERVED,
                description=("the agent must be observed at the landing cell afterwards"),
                expect=f"{FACT_PREFIX}agent.x={landing[0]},{FACT_PREFIX}agent.y={landing[1]}",
            ),
            effect=StepEffect.LOCAL_WRITE,
            estimated_seconds=0.2,
            estimated_cost=1.0,
        )
    ]


def _directions_toward(position: tuple[int, int], target: tuple[int, int]) -> tuple[str, ...]:
    """The directions that reduce the distance, longest axis first.

    A deterministic order rather than a search: this is a subgoal heuristic, and
    Phase 24 is what decides whether the resulting plan is any good.
    """
    dx = target[0] - position[0]
    dy = target[1] - position[1]
    horizontal = "east" if dx > 0 else "west"
    vertical = "south" if dy > 0 else "north"
    rows: list[str] = []
    if abs(dx) >= abs(dy):
        if dx:
            rows.append(horizontal)
        if dy:
            rows.append(vertical)
    else:
        if dy:
            rows.append(vertical)
        if dx:
            rows.append(horizontal)
    return tuple(rows)


__all__ = [
    "TacticalChoice",
    "TacticalMemory",
    "choose",
    "read_collected",
    "read_position",
    "read_target",
    "tasks_for",
]
