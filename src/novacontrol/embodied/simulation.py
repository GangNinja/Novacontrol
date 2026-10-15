"""A deterministic, owned grid world — the environment this phase validates.

This is the "controlled game or simulation environment" the phase asks for. It
is not a game and does not pretend to be one: it is a small grid world, owned
by this build, whose whole state is readable through a real interface. That is
precisely why it is the right place to prove the perception→action loop — every
failure it can produce is a failure the loop must handle, and every one of them
is reproducible.

What it genuinely supports:

* a bounded grid with walls, an agent, and one collectible target;
* structured observations (the environment's own state, AUTHORITATIVE) and an
  optional ``fog`` reading that reports unlabeled regions only — which is how
  the uncertainty rule is exercised with real data rather than a stub;
* bounded inputs: ``move``/``interact``/``wait``/``observe``/``release``, one
  step each; the duration budget is bounded and enforced at the manager's
  single input door, not re-derived here;
* explicit, NAMED fault injections — ``timeout``, ``drift``, ``reject``,
  ``freeze`` — so a workflow can make the environment misbehave on purpose.

Every injection is declared in the adapter's own capability table, which is the
difference between a fault this build can reproduce and a fault this build
pretends it might have.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from novacontrol.embodied.environment import ConnectReport, ReleaseReport
from novacontrol.embodied.models import (
    ActionResult,
    ActionStatus,
    EntityRead,
    EnvironmentAction,
    EnvironmentCapability,
    EnvironmentInfo,
    EnvironmentKind,
    EnvironmentStatus,
    Observation,
    ObservationBasis,
    ObservationFreshness,
)

#: The directions a move may name. Anything else is a rejection, not a guess.
_DIRECTIONS = {
    "north": (0, -1),
    "south": (0, 1),
    "east": (1, 0),
    "west": (-1, 0),
    "up": (0, -1),
    "down": (0, 1),
    "right": (1, 0),
    "left": (-1, 0),
}

#: Inputs every simulation adapter answers for.
_ACTIONS = ("move", "interact", "wait", "observe", "release")

#: The faults a scenario may ask for, by name — read by a status surface and by
#: a test, so neither has to guess at the fields of :class:`Faults`.
FAULT_NAMES: tuple[str, ...] = ("timeout", "drift", "reject", "freeze")

#: How many moves one action may chain. One: a bounded input is bounded.
MAX_MOVE_STEPS = 1


@dataclass(frozen=True, slots=True)
class Faults:
    """The faults an environment is asked to produce, deliberately.

    Every field is a COUNT rather than a flag so a scenario can say "fail the
    next two inputs" and stay deterministic. ``drift`` makes a move land
    somewhere other than where it was told to go — the unexpected outcome the
    agent must notice rather than assume away.
    """

    timeout: int = 0
    drift: int = 0
    reject: int = 0
    freeze: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeout": self.timeout,
            "drift": self.drift,
            "reject": self.reject,
            "freeze": self.freeze,
        }


@dataclass
class _World:
    """The simulation's mutable state — the whole of it, nothing hidden."""

    agent: tuple[int, int] = (0, 0)
    target: tuple[int, int] = (3, 0)
    walls: frozenset[tuple[int, int]] = frozenset()
    collected: bool = False
    tick: int = 0
    moves: int = 0
    terminal: bool = False


@dataclass
class GridWorldSimulation:
    """A deterministic grid world behind the :class:`EnvironmentAdapter` contract.

    Constructed from a plain mapping so a scenario is data:

    ``{"width": 6, "height": 4, "start": [0, 0], "target": [3, 0],
       "walls": [[1, 0]], "collect": true, "reaches": true}``
    """

    environment_id: str = "gridworld"
    name: str = "Owned grid-world simulation"
    config: Mapping[str, Any] = field(default_factory=dict)
    faults: Faults = field(default_factory=Faults)
    fresh_after_ms: float = 5000.0
    _world: _World = field(default_factory=_World, init=False)
    _connected: bool = field(default=False, init=False)
    _held: dict[str, Any] = field(default_factory=dict, init=False)
    _faults_used: dict[str, int] = field(default_factory=dict, init=False)
    _observations: int = field(default=0, init=False)
    _inputs: int = field(default=0, init=False)
    _rejections: int = field(default=0, init=False)
    _failures: int = field(default=0, init=False)
    _last_observation_at: float = field(default=0.0, init=False)
    _started_at: float = field(default=0.0, init=False)
    _now: Any = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._now = getattr(self.config, "now", None) or time.monotonic
        self.close()

    # -- identity ---------------------------------------------------------------

    @property
    def info(self) -> EnvironmentInfo:
        return EnvironmentInfo(
            environment_id=self.environment_id,
            name=self.name,
            kind=EnvironmentKind.SIMULATION,
            status=self.status(),
            reason=(
                "owned simulation; deterministic and fully observable"
                if self._connected
                else "not connected yet"
            ),
            actions=_ACTIONS,
            observations=("structured_state", "fogged_regions"),
            capabilities=self.capabilities(),
        )

    def capabilities(self) -> tuple[EnvironmentCapability, ...]:
        """What this adapter can do, and whether it was verified end to end."""
        return (
            EnvironmentCapability(
                "structured_observation",
                "Read the environment's own grid state (authoritative).",
                status=EnvironmentStatus.OPERATIONAL
                if self._connected
                else EnvironmentStatus.UNAVAILABLE,
                verified=True,
            ),
            EnvironmentCapability(
                "fogged_observation",
                "Return unlabeled regions only — the uncertain-perception path.",
                status=EnvironmentStatus.OPERATIONAL,
                verified=True,
                reason="labels are deliberately withheld; a region is not a name",
            ),
            EnvironmentCapability(
                "bounded_input",
                "Send one bounded move/interact/wait input.",
                status=EnvironmentStatus.OPERATIONAL,
                verified=True,
            ),
            EnvironmentCapability(
                "input_release",
                "Release every held input; always possible, never refused.",
                status=EnvironmentStatus.OPERATIONAL,
                verified=True,
            ),
            EnvironmentCapability(
                "fault_injection",
                "Produce named faults on request (timeout/drift/reject/freeze).",
                status=EnvironmentStatus.OPERATIONAL,
                verified=True,
                reason="test-only knob; off unless a scenario asks for it",
            ),
            EnvironmentCapability(
                "semantic_object_recognition",
                "Name objects from pixels (enemy, weapon, player).",
                status=EnvironmentStatus.UNSUPPORTED,
                verified=False,
                reason=(
                    "this simulation has no vision provider and will not invent an "
                    "identity for a classless region"
                ),
            ),
        )

    # -- lifecycle --------------------------------------------------------------

    def connect(self) -> ConnectReport:
        """Reset the world to its configured start and report readiness."""
        self.close()
        start = tuple(self.config.get("start") or (0, 0))
        target = tuple(self.config.get("target") or (3, 0))
        walls = frozenset(tuple(item) for item in self.config.get("walls") or ())
        self._world = _World(
            agent=(int(start[0]), int(start[1])),
            target=(int(target[0]), int(target[1])),
            walls=walls,
        )
        self._connected = True
        self._held.clear()
        self._faults_used = {"timeout": 0, "drift": 0, "reject": 0, "freeze": 0}
        self._observations = 0
        self._inputs = 0
        self._rejections = 0
        self._failures = 0
        self._started_at = float(self._now())
        self._last_observation_at = self._started_at
        self._world.terminal = bool(self.config.get("terminal", False))
        return ConnectReport(
            environment_id=self.environment_id,
            connected=True,
            status=EnvironmentStatus.OPERATIONAL,
            reason="owned simulation ready",
            actions=_ACTIONS,
            observations=("structured_state", "fogged_regions"),
            mock=False,
            detail={
                "grid": [int(self.config.get("width", 6)), int(self.config.get("height", 4))],
                "start": [self._world.agent[0], self._world.agent[1]],
                "target": [self._world.target[0], self._world.target[1]],
                "walls": sorted(list(item) for item in self._world.walls),
            },
        )

    def status(self) -> EnvironmentStatus:
        if not self._connected:
            return EnvironmentStatus.UNAVAILABLE
        return EnvironmentStatus.OPERATIONAL

    def shutdown(self) -> None:
        self.close()

    def close(self) -> None:
        """Release everything and mark the adapter disconnected."""
        self._held.clear()
        self._connected = False

    # -- observation ------------------------------------------------------------

    def observe(self, *, session_id: str = "") -> Observation:
        """Read the environment — as a measurement, or as a fogged reading.

        ``fog`` in the config withholds the structured state and returns
        unlabeled regions instead. That is a different claim about the world, so
        it produces ``basis=VISUAL`` entities with no labels rather than a
        dressed-up version of the real state.
        """
        self._observations += 1
        now = float(self._now())
        age_ms = (now - self._last_observation_at) * 1000.0 if self._last_observation_at else 0.0
        if self._frozen():
            return Observation(
                session_id=session_id,
                environment_id=self.environment_id,
                taken_at=_stamp(now),
                basis=ObservationBasis.UNKNOWN,
                freshness=ObservationFreshness.STALE,
                age_ms=age_ms,
                reason="the adapter was asked to freeze; no fresh reading is available",
                provenance="simulation:freeze",
            )
        self._last_observation_at = now
        if self.config.get("fog"):
            return Observation(
                session_id=session_id,
                environment_id=self.environment_id,
                taken_at=_stamp(now),
                basis=ObservationBasis.VISUAL,
                freshness=ObservationFreshness.FRESH,
                age_ms=0.0,
                structured={},
                entities=self._fogged_entities(),
                reason=(
                    "fogged reading: these are unlabeled regions, not identified "
                    "objects; no provider can say what they are"
                ),
                provenance="simulation:fog",
            )
        return Observation(
            session_id=session_id,
            environment_id=self.environment_id,
            taken_at=_stamp(now),
            basis=ObservationBasis.STRUCTURED,
            freshness=ObservationFreshness.FRESH,
            age_ms=0.0,
            structured=dict(self._structured_state()),
            objective_progress=self._progress(),
            confidence=1.0,
            provenance="simulation:state",
            evidence=(f"simulation:{self.environment_id}:tick={self._world.tick}",),
        )

    def _structured_state(self) -> dict[str, Any]:
        world = self._world
        return {
            "agent.x": world.agent[0],
            "agent.y": world.agent[1],
            "target.x": world.target[0],
            "target.y": world.target[1],
            "target.collected": bool(world.collected),
            "tick": int(world.tick),
            "moves": int(world.moves),
            "at_target": bool(world.agent == world.target),
            "walls": sorted([list(item) for item in world.walls]),
            "terminal": bool(world.terminal),
        }

    def _progress(self) -> dict[str, Any]:
        world = self._world
        distance = abs(world.agent[0] - world.target[0]) + abs(world.agent[1] - world.target[1])
        return {
            "distance_to_target": distance,
            "collected": bool(world.collected),
            "moves": int(world.moves),
        }

    def _fogged_entities(self) -> tuple[EntityRead, ...]:
        """Regions with positions — and deliberately NO names."""
        world = self._world
        rows = [
            EntityRead(
                entity_id="region-target",
                label="",
                kind="region",
                position=(float(world.target[0]), float(world.target[1])),
                confidence=0.6,
                basis=ObservationBasis.VISUAL,
                evidence=("simulation:fog:region",),
            )
        ]
        for index, (x, y) in enumerate(sorted(world.walls)):
            rows.append(
                EntityRead(
                    entity_id=f"region-wall-{index}",
                    label="",
                    kind="region",
                    position=(float(x), float(y)),
                    confidence=0.4,
                    basis=ObservationBasis.VISUAL,
                    evidence=("simulation:fog:region",),
                )
            )
        return tuple(rows)

    # -- acting -----------------------------------------------------------------

    def available_actions(self) -> tuple[str, ...]:
        if not self._connected:
            return ()
        return _ACTIONS

    def send_input(self, action: EnvironmentAction) -> ActionResult:
        """Send one bounded input and report exactly what happened."""
        started = float(self._now())
        name = str(action.input_name or "").strip().lower()
        started_at = _stamp(started)
        if not self._connected:
            self._failures += 1
            return ActionResult(
                action_id=action.action_id,
                session_id=action.session_id,
                status=ActionStatus.FAILED,
                input_name=name,
                detail="the environment is not connected",
                error="not connected",
                started_at=started_at,
                finished_at=_stamp(float(self._now())),
                duration_ms=0.0,
            )
        if name == "release":
            report = self.release_inputs()
            return ActionResult(
                action_id=action.action_id,
                session_id=action.session_id,
                status=ActionStatus.COMPLETED,
                input_name=name,
                detail="nothing was held; the release contract is satisfied",
                started_at=started_at,
                finished_at=_stamp(float(self._now())),
                released=report.released,
            )
        if name not in _ACTIONS:
            self._rejections += 1
            return ActionResult(
                action_id=action.action_id,
                session_id=action.session_id,
                status=ActionStatus.REJECTED,
                input_name=name,
                detail=f"the environment does not support input {name!r}",
                error=f"unsupported input: {name}",
                started_at=started_at,
                finished_at=_stamp(float(self._now())),
                released=True,
            )
        if self._fault_due("reject"):
            self._rejections += 1
            return ActionResult(
                action_id=action.action_id,
                session_id=action.session_id,
                status=ActionStatus.REJECTED,
                input_name=name,
                detail="the environment rejected this input (injected fault)",
                error="rejected by the environment",
                started_at=started_at,
                finished_at=_stamp(float(self._now())),
                released=True,
            )
        if self._fault_due("timeout"):
            self._failures += 1
            return ActionResult(
                action_id=action.action_id,
                session_id=action.session_id,
                status=ActionStatus.TIMED_OUT,
                input_name=name,
                detail=(
                    "the input was accepted but the environment never reported "
                    "back (injected fault); the input is released and NOT retried "
                    "blindly"
                ),
                error="timeout",
                started_at=started_at,
                finished_at=_stamp(float(self._now())),
                duration_ms=(float(self._now()) - started) * 1000.0,
                released=True,
            )

        self._inputs += 1
        detail, rejected = self._apply(name, action, drift=self._fault_due("drift"))
        if rejected:
            self._rejections += 1
            return ActionResult(
                action_id=action.action_id,
                session_id=action.session_id,
                status=ActionStatus.REJECTED,
                input_name=name,
                detail=detail,
                started_at=started_at,
                finished_at=_stamp(float(self._now())),
                duration_ms=(float(self._now()) - started) * 1000.0,
                released=True,
            )
        self._world.tick += 1
        if action.release_after:
            self._held.clear()
        observed = None if self._frozen() else self.observe(session_id=action.session_id)
        return ActionResult(
            action_id=action.action_id,
            session_id=action.session_id,
            status=ActionStatus.COMPLETED,
            input_name=name,
            detail=detail,
            started_at=started_at,
            finished_at=_stamp(float(self._now())),
            duration_ms=(float(self._now()) - started) * 1000.0,
            observed=observed,
            verified=None,  # the AGENT verifies; the environment only reports
            released=True,
        )

    def release_inputs(self) -> ReleaseReport:
        """Release everything held. Always succeeds — that is the contract."""
        held = tuple(sorted(self._held))
        self._held.clear()
        return ReleaseReport(released=True, held=held, reason="all inputs released")

    # -- internals --------------------------------------------------------------

    def _apply(
        self, name: str, action: EnvironmentAction, *, drift: bool = False
    ) -> tuple[str, bool]:
        world = self._world
        parameters = dict(action.parameters or {})
        if name == "observe":
            return "took a fresh observation", False
        if name == "wait":
            world.tick += 1
            return "waited one tick", False
        if name == "move":
            if world.terminal:
                return "the environment is in a terminal state", True
            direction = (
                str(parameters.get("direction") or parameters.get("move") or "").strip().lower()
            )
            if direction not in _DIRECTIONS:
                return f"unsupported direction {direction!r}", True
            dx, dy = _DIRECTIONS[direction]
            if drift and self._world.moves == 0:
                # The injected unexpected outcome: the environment does not obey.
                dx, dy = -dx, -dy
            x, y = world.agent
            candidate = (x + dx * MAX_MOVE_STEPS, y + dy * MAX_MOVE_STEPS)
            if candidate in world.walls:
                return f"blocked by a wall at {candidate[0]},{candidate[1]}", False
            width = int(self.config.get("width", 6))
            height = int(self.config.get("height", 4))
            if not (0 <= candidate[0] < width and 0 <= candidate[1] < height):
                return f"blocked by the boundary at {candidate[0]},{candidate[1]}", False
            world.agent = candidate
            world.moves += 1
            return f"moved {direction} to {candidate[0]},{candidate[1]}", False
        if name == "interact":
            if world.terminal:
                return "the environment is in a terminal state", True
            if world.collected:
                # An idempotent refusal, not a failure: asking twice is not an error.
                return "the target is already collected", False
            if world.agent != world.target:
                return "nothing to interact with here (not on the target cell)", False
            world.collected = True
            if bool(self.config.get("terminal_on_collect", True)):
                world.terminal = True
            return "collected the target", False
        return f"unsupported input {name!r}", True

    def _fault_due(self, kind: str) -> bool:
        """Whether this input should be the next one of a named fault."""
        budget = int(getattr(self.faults, kind, 0) or 0)
        used = int(self._faults_used.get(kind, 0))
        if used >= budget:
            return False
        self._faults_used[kind] = used + 1
        return True

    def _frozen(self) -> bool:
        """Whether this environment is frozen.

        ``freeze`` is a scenario-level condition rather than a count: an
        environment asked to freeze stays frozen, which is the failure mode the
        agent must notice (a reading that is not current) rather than one it can
        retry past. A zero budget means never frozen — the comparison is on the
        CONFIGURED budget, so an unfrozen adapter cannot be reported frozen.
        """
        return int(self.faults.freeze or 0) > 0

    def stuck(self) -> bool:
        """Whether the world is frozen by an injected fault (test/scenario helper)."""
        return self._frozen()

    def counters(self) -> dict[str, int]:
        """Operational counters (never content) for status surfaces."""
        return {
            "observations": self._observations,
            "inputs": self._inputs,
            "rejections": self._rejections,
            "failures": self._failures,
            "ticks": int(self._world.tick),
            "faults_used": sum(self._faults_used.values()),
        }


def scenarios() -> dict[str, dict[str, Any]]:
    """The named deterministic worlds this phase validates against.

    A named scenario is data, not code: the same dictionary can be sent over
    the API, used by a test, and replayed by an operator.
    """
    return {
        # A clear straight line: east three cells to the target.
        "open-east": {
            "width": 6,
            "height": 3,
            "start": [0, 1],
            "target": [3, 1],
            "walls": [],
        },
        # A wall directly east: the agent must route around it.
        "detour": {
            "width": 6,
            "height": 3,
            "start": [0, 0],
            "target": [3, 0],
            "walls": [[1, 0], [2, 2]],
        },
        # The environment disagrees with the first move (unexpected outcome).
        # The fault travels IN the scenario so the name is the truth: a scenario
        # with a faulted-sounding name that behaves like a clean world is a lie
        # an operator would have to read the code to catch.
        "drifting": {
            "width": 6,
            "height": 3,
            "start": [0, 0],
            "target": [3, 0],
            "walls": [],
            "faults": {"drift": 1},
        },
        # The controller refuses the first two inputs: the retry path.
        "refusing": {
            "width": 6,
            "height": 3,
            "start": [0, 0],
            "target": [3, 0],
            "walls": [],
            "faults": {"reject": 2},
        },
        # The environment stops answering: the stale-reading path.
        "frozen": {
            "width": 6,
            "height": 3,
            "start": [0, 0],
            "target": [3, 0],
            "walls": [],
            "faults": {"freeze": 1},
        },
        # An input that never reports back, so the agent must re-observe before
        # deciding whether it landed at all.
        "timing-out": {
            "width": 6,
            "height": 3,
            "start": [0, 0],
            "target": [3, 0],
            "walls": [],
            "faults": {"timeout": 1},
        },
        # Fogged: regions only, no structured state and no labels.
        "fogged": {
            "width": 6,
            "height": 3,
            "start": [0, 0],
            "target": [3, 0],
            "walls": [],
            "fog": True,
        },
    }


def faults_for(scenario: Mapping[str, Any], faults: Faults | None = None) -> Faults:
    """The faults a scenario declares, with an explicit set layered over them.

    A scenario carries its own faults so its NAME is the whole truth about it,
    and a caller that passes ``faults`` explicitly is asking for MORE trouble
    than the scenario already has — never for less, which would make a faulted
    scenario silently clean.
    """
    declared = dict(scenario.get("faults") or {}) if isinstance(scenario, Mapping) else {}
    stated = (
        {key: int(value) for key, value in faults.to_dict().items()} if faults is not None else {}
    )
    merged: dict[str, int] = {}
    for name in FAULT_NAMES:
        merged[name] = max(int(declared.get(name, 0) or 0), int(stated.get(name, 0) or 0))
    return Faults(**merged)


def adapter_for(
    scenario: Mapping[str, Any] | str | None = None,
    *,
    faults: Faults | None = None,
    environment_id: str = "gridworld",
) -> GridWorldSimulation:
    """An adapter for a named scenario or an explicit configuration mapping."""
    if scenario is None:
        config: Mapping[str, Any] = scenarios()["open-east"]
    elif isinstance(scenario, str):
        known = scenarios()
        if scenario not in known:
            raise KeyError(
                f"unknown scenario {scenario!r}; this build has: {', '.join(sorted(known))}"
            )
        config = known[scenario]
    else:
        config = dict(scenario)
    return GridWorldSimulation(
        environment_id=environment_id,
        config=config,
        faults=faults_for(config, faults),
    )


def _stamp(seconds: float) -> str:
    """A monotonic stamp that is still readable in a report."""
    return f"t+{seconds:.6f}"


__all__ = [
    "FAULT_NAMES",
    "faults_for",
    "MAX_MOVE_STEPS",
    "Faults",
    "GridWorldSimulation",
    "adapter_for",
    "scenarios",
]
