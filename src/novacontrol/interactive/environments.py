"""Controlled environments — the simulator, the read-only world, and the bridge.

An experiment needs somewhere to act. Three environments ship here, and the
difference between them is the point of the module:

``DeterministicEnvironment``
    A simulator. Repeatable state transitions, declared rules, configurable
    failures, delays and deliberately incomplete observations. It says
    ``kind=simulator`` everywhere it appears, because learning in a simulator is
    evidence about the simulator (§4.4/§10).

``WorldStateEnvironment``
    READ-ONLY. It observes Phase 22's world state through an injected reader and
    exposes no actions at all: an environment that could act on the world without
    the approval path is exactly what §11 forbids. Asking it to act is a refusal
    with a reason, not a hidden no-op.

``BridgedEnvironment``
    Real actions, and only when a caller injects an executor. With no executor
    wired — the default everywhere in this build — every action is BLOCKED with a
    reason. That is the honest posture: Phase 23 must not invent a second way to
    touch the machine.

Nothing here picks a strategy, writes a hypothesis or learns anything. This module
answers one question: *what can be done here, and what came of doing it.*
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from novacontrol.interactive.models import (
    ActionResult,
    ActionSpec,
    EnvironmentKind,
    EnvironmentObservation,
    as_bool,
    as_float,
    as_int,
    as_text,
    elapsed_seconds,
    now_iso,
)

__all__ = [
    "BridgedEnvironment",
    "DeterministicEnvironment",
    "Environment",
    "EnvironmentRegistry",
    "WorldStateEnvironment",
    "builtin_environments",
    "environment_from_spec",
]


@runtime_checkable
class Environment(Protocol):
    """What an environment must expose. Observation is read-only; action is not.

    ``observe`` and ``execute`` are coroutines because a real environment may be
    slow; a simulator simply never awaits anything.
    """

    environment_id: str
    kind: EnvironmentKind

    async def observe(self) -> EnvironmentObservation:
        """The environment as it is now, bounded and honest about completeness."""
        ...

    def state(self) -> Mapping[str, Any]:
        """The environment's own state, for a caller that needs to reason about it."""
        ...

    def actions(self) -> tuple[ActionSpec, ...]:
        """Every action this environment exposes — declared, never guessed."""
        ...

    def validate(self, action: str, parameters: Mapping[str, Any]) -> tuple[bool, str]:
        """Whether the action could run now, and why not when it could not."""
        ...

    async def execute(self, action: str, parameters: Mapping[str, Any]) -> ActionResult:
        """Carry out one action and report what happened, including refusals."""
        ...

    def status(self) -> dict[str, Any]:
        """What this environment is and what it has done — cheap and read-only."""
        ...


def _mapping_field(value: Any) -> dict[str, Any]:
    """A declared mapping, read tolerantly — a wrong type is ignored, not fatal."""
    return dict(value) if isinstance(value, Mapping) else {}


def _texts_field(value: Any) -> tuple[str, ...]:
    """A declared sequence read as text; a bare string is ONE entry, not characters."""
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def candidate_rows(
    candidates: Mapping[str, Sequence[Any]],
) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    """A declared candidate space as (statement, predicted observation) pairs.

    One reader for both environments, so the shape a hypothesis is proposed from is
    the same whether the environment is a simulator or a real one behind a bridge. A
    candidate is a possible OUTCOME expressed the way an observation expresses it — a
    mapping of observed keys to values — because that is what comparison matches
    against; a bare scalar is read as the value of ``value``.
    """
    out: list[tuple[str, Mapping[str, Any]]] = []
    for action in sorted(str(key) for key in candidates):
        for outcome in candidates[action]:
            predicted = dict(outcome) if isinstance(outcome, Mapping) else {"value": outcome}
            if not predicted:
                continue
            out.append(
                (f"when {action!r} runs, the observed state becomes {predicted!r}", predicted)
            )
    return tuple(out)


# ── deterministic simulator ──────────────────────────────────────────────────


class DeterministicEnvironment:
    """A simulated environment with declared rules and controllable misbehaviour.

    Everything is seeded and repeatable: the same seed and the same action sequence
    produce the same values, which is what makes a learning result reproducible in a
    regression test. The knobs exist so the DEFECT-shaped cases can be exercised
    rather than hoped about — an action that fails, an action that is slow enough to
    time out, an observation that omits the key the prediction was about, and an
    environment that returns a value different from the one its rule implies.

    ``hidden`` names the actions whose real rule the learner is NOT told: those are
    what a candidate explanation is for. ``candidates`` declares the candidate
    outcomes for them, so hypothesis generation has a declared space to draw from
    instead of inventing one.
    """

    def __init__(
        self,
        environment_id: str = "sim",
        *,
        initial_state: Mapping[str, Any] | None = None,
        rules: Mapping[str, Any] | None = None,
        hidden: Sequence[str] = (),
        candidates: Mapping[str, Sequence[Any]] | None = None,
        fail_actions: Sequence[str] = (),
        slow_actions: Mapping[str, float] | None = None,
        incomplete_keys: Sequence[str] = (),
        misleading: Mapping[str, Any] | None = None,
        conflicting_keys: Sequence[str] = (),
        noise: Mapping[str, float] | None = None,
        observation_complete: bool = True,
        seed: int = 0,
        declared_actions: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.environment_id = as_text(environment_id, "sim")
        self.kind = EnvironmentKind.SIMULATOR
        self.is_simulator = True
        self._state: dict[str, Any] = dict(initial_state or {})
        self._initial: dict[str, Any] = dict(initial_state or {})
        self._rules: dict[str, Any] = {str(key): value for key, value in (rules or {}).items()}
        self._hidden: tuple[str, ...] = tuple(as_text(item) for item in hidden if as_text(item))
        self._candidates: dict[str, tuple[Any, ...]] = {
            str(key): tuple(value)
            for key, value in (candidates or {}).items()
            if isinstance(value, Sequence)
        }
        self._fail = {as_text(item) for item in fail_actions if as_text(item)}
        self._slow: dict[str, float] = {
            str(key): as_float(value) for key, value in (slow_actions or {}).items()
        }
        self._incomplete = {as_text(item) for item in incomplete_keys if as_text(item)}
        self._misleading: dict[str, Any] = dict(misleading or {})
        self._conflicting = {as_text(item) for item in conflicting_keys if as_text(item)}
        self._noise: dict[str, float] = {
            str(key): as_float(value) for key, value in (noise or {}).items()
        }
        self._observation_complete = bool(observation_complete)
        self._random = random.Random(seed)
        self.seed = seed
        self._declared: dict[str, dict[str, Any]] = {
            str(key): dict(value) for key, value in (declared_actions or {}).items()
        }
        self.executed = 0
        self.blocked = 0
        self.failed = 0
        self.observations = 0
        self.history: list[str] = []
        self._sequence = 0

    # ── spec ────────────────────────────────────────────────────────────────
    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> DeterministicEnvironment:
        """A simulator declared as DATA — how a caller describes one over the API.

        The specification is the same shape ``rules``/``candidates``/``fail_actions``
        take here, so there is no second way to describe a simulator. Every field is
        read tolerantly: a value of the wrong type is IGNORED rather than crashing a
        request, and the resulting environment reports what it actually has.
        """
        return cls(
            environment_id=as_text(spec.get("environment_id"), "sim"),
            initial_state=_mapping_field(spec.get("initial_state")),
            rules=_mapping_field(spec.get("rules")),
            hidden=_texts_field(spec.get("hidden")),
            candidates=_mapping_field(spec.get("candidates")),
            fail_actions=_texts_field(spec.get("fail_actions")),
            slow_actions=_mapping_field(spec.get("slow_actions")),
            incomplete_keys=_texts_field(spec.get("incomplete_keys")),
            misleading=_mapping_field(spec.get("misleading")),
            conflicting_keys=_texts_field(spec.get("conflicting_keys")),
            noise=_mapping_field(spec.get("noise")),
            observation_complete=as_bool(spec.get("observation_complete"), True),
            seed=as_int(spec.get("seed"), 0),
            declared_actions=_mapping_field(spec.get("actions")),
        )

    # ── contract ────────────────────────────────────────────────────────────
    async def observe(self) -> EnvironmentObservation:
        """The current state as an observation, with completeness told honestly.

        A key named in ``incomplete_keys`` is genuinely OMITTED, not blanked: an
        observation that silently keeps the value it claims to have lost would make
        "a partial look" unverifiable, and a test could not tell the two apart.
        """
        values = dict(self._state)
        for key in self._incomplete:
            values.pop(key, None)
        for key in self._conflicting:
            values[key] = [values.get(key), self._misleading.get(key, values.get(key))]
        for key, spread in self._noise.items():
            base = as_float(values.get(key), 0.0)
            values[key] = base + (self._random.random() - 0.5) * 2.0 * spread
        self.observations += 1
        self._sequence += 1
        return EnvironmentObservation(
            observation_ref=f"{self.environment_id}-obs-{self._sequence}",
            values=values,
            complete=self._observation_complete and not self._incomplete,
            quality=1.0 if not self._incomplete else 0.5,
            timestamp=now_iso(),
            source=self.environment_id,
            kind=EnvironmentKind.SIMULATOR,
            note=self._note(),
            conflicting=bool(self._conflicting),
        )

    def _note(self) -> str:
        if self._incomplete:
            return f"this observation omits {sorted(self._incomplete)} (a partial look)"
        if self._misleading:
            return "this environment returns values its rule does not imply"
        return ""

    def state(self) -> Mapping[str, Any]:
        return dict(self._state)

    def actions(self) -> tuple[ActionSpec, ...]:
        specs: list[ActionSpec] = []
        for name in sorted(self._rules):
            declared = self._declared.get(name, {})
            specs.append(
                ActionSpec(
                    name=name,
                    description=as_text(declared.get("description"), f"Apply {name}."),
                    parameters=tuple(str(item) for item in declared.get("parameters", ()) or ()),
                    preconditions=tuple(
                        str(item) for item in declared.get("preconditions", ()) or ()
                    ),
                    destructive=as_bool(declared.get("destructive")),
                    reversible=as_bool(declared.get("reversible"), True),
                    estimated_seconds=as_float(declared.get("estimated_seconds"), 0.02),
                    simulator=True,
                )
            )
        return tuple(specs)

    def validate(self, action: str, parameters: Mapping[str, Any]) -> tuple[bool, str]:
        """Whether the action is declared here and could run now."""
        name = as_text(action)
        if not name:
            return False, "no action was named"
        if name not in self._rules:
            return False, f"{name!r} is not an action of environment {self.environment_id!r}"
        declared = self._declared.get(name, {})
        for key in declared.get("parameters", ()) or ():
            if str(key) not in parameters:
                return False, f"{name!r} needs parameter {key!r}"
        for precondition in declared.get("preconditions", ()) or ():
            if str(precondition) not in self._state:
                return False, (
                    f"{name!r} requires {precondition!r}, which this state does not have"
                )
        return True, ""

    def candidates(self) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        """The declared candidate explanations, as (statement, predicted observation) pairs.

        A candidate is a possible OUTCOME expressed the way an observation expresses
        it — a mapping of observed keys to values — because that is what comparison
        matches against. A bare scalar is accepted and read as the value of ``value``.

        ``hidden`` and ``candidates`` are separate knobs on purpose: ``hidden``
        controls whether the real rule is declared to the learner, while
        ``candidates`` says which outcomes are thinkable. A VISIBLE rule the
        environment then contradicts (a misleading environment) needs candidates just
        as much as a hidden one, and it is a scenario worth testing.
        """
        return candidate_rows(self._candidates)

    def available_candidates(self, action: str) -> tuple[Any, ...]:
        """Declared candidate outcomes for ONE action, in declared order."""
        return self._candidates.get(as_text(action), ())

    def declared_rule(self, action: str) -> Mapping[str, Any] | None:
        """The rule for an action, when it is NOT hidden — what a prediction may read.

        A hidden action returns ``None`` on purpose: hiding a rule is how a scenario
        asks the learner to discover it, and a rule that leaked here would make the
        discovery meaningless.
        """
        name = as_text(action)
        if not name or name in self._hidden:
            return None
        patch = self._apply_rule(name)
        return dict(patch) or None

    async def execute(self, action: str, parameters: Mapping[str, Any]) -> ActionResult:
        """Perform the action, or report honestly that it did not happen."""
        name = as_text(action)
        started = now_iso()
        ok, reason = self.validate(name, parameters)
        if not ok:
            self.blocked += 1
            return ActionResult(
                ok=False, status="blocked", reason=reason, duration_ms=0.0, actions=0
            )
        delay = self._slow.get(name, 0.0)
        if delay > 0:
            await asyncio.sleep(delay)
        if name in self._fail:
            self.failed += 1
            self.executed += 1
            self.history.append(name)
            return ActionResult(
                ok=False,
                status="failed",
                error=f"{name!r} failed in {self.environment_id!r}",
                reason="the environment reported a failure for this action",
                duration_ms=max(0.0, self._elapsed_ms(started)),
                actions=1,
            )
        patch = self._apply_rule(name)
        for key, value in patch.items():
            self._state[key] = value
        for key in self._misleading:
            if key in self._state:
                self._state[key] = self._misleading[key]
        self.executed += 1
        self.history.append(name)
        return ActionResult(
            ok=True,
            status="completed",
            observation=None,
            duration_ms=max(0.0, self._elapsed_ms(started)),
            actions=1,
        )

    def _apply_rule(self, action: str) -> dict[str, Any]:
        rule = self._rules.get(action)
        if rule is None:
            return {}
        if callable(rule):
            produced = rule(dict(self._state))
            return dict(produced) if isinstance(produced, Mapping) else {}
        if isinstance(rule, Mapping):
            if "state" in rule and isinstance(rule["state"], Mapping):
                return dict(rule["state"])
            return dict(rule)
        return {}

    def _elapsed_ms(self, started: str) -> float:
        return elapsed_seconds(started, now_iso()) * 1000.0

    def reset(self) -> None:
        """Back to the initial state — so one scenario can be re-run exactly."""
        self._state = dict(self._initial)
        self.history.clear()

    def status(self) -> dict[str, Any]:
        return {
            "environment_id": self.environment_id,
            "kind": self.kind.value,
            "simulator": True,
            "actions": [item.name for item in self.actions()],
            "hidden_actions": list(self._hidden),
            "candidate_actions": sorted(self._candidates),
            "fail_actions": sorted(self._fail),
            "slow_actions": dict(sorted(self._slow.items())),
            "incomplete_keys": sorted(self._incomplete),
            "misleading_keys": sorted(self._misleading),
            "conflicting_keys": sorted(self._conflicting),
            "observation_complete": self._observation_complete,
            "seed": self.seed,
            "executed": self.executed,
            "blocked": self.blocked,
            "failed": self.failed,
            "observations": self.observations,
            "state_keys": sorted(self._state),
            "limitations": [
                "a simulator's result is evidence about the simulator, not about the real world",
            ],
        }


# ── the read-only world ──────────────────────────────────────────────────────

Reader = Callable[[], Mapping[str, Any]]


class WorldStateEnvironment:
    """Phase 22's world state as an environment — observation only.

    The reader is injected (the application passes a callable over the world
    engine), so this class owns no access to the world model's internals and a
    caller without one gets an honest "unavailable" observation rather than an
    empty dict pretending to be state.
    """

    def __init__(
        self,
        reader: Reader | None = None,
        *,
        environment_id: str = "world",
        scope: str = "default",
    ) -> None:
        self.environment_id = as_text(environment_id, "world")
        self.kind = EnvironmentKind.WORLD
        self.is_simulator = False
        self.scope = as_text(scope, "default")
        self._reader = reader
        self.observations = 0
        self._sequence = 0

    async def observe(self) -> EnvironmentObservation:
        self.observations += 1
        self._sequence += 1
        if self._reader is None:
            return EnvironmentObservation(
                observation_ref=f"{self.environment_id}-obs-{self._sequence}",
                values={},
                complete=False,
                quality=0.0,
                timestamp=now_iso(),
                source=self.environment_id,
                kind=EnvironmentKind.WORLD,
                note="no world reader is wired, so nothing could be read",
            )
        values = dict(self._reader())
        return EnvironmentObservation(
            observation_ref=f"{self.environment_id}-obs-{self._sequence}",
            values=values,
            complete=True,
            quality=1.0,
            timestamp=now_iso(),
            source=self.environment_id,
            kind=EnvironmentKind.WORLD,
            note="read from the world model; this observation is a summary, not content",
        )

    def state(self) -> Mapping[str, Any]:
        return dict(self._reader()) if self._reader is not None else {}

    def actions(self) -> tuple[ActionSpec, ...]:
        """None: this environment observes. Acting on the world needs the approval path."""
        return ()

    def validate(self, action: str, parameters: Mapping[str, Any]) -> tuple[bool, str]:
        return False, (
            "the world environment is read-only: interactive learning observes it and "
            "never acts on it, so no action is available here"
        )

    async def execute(self, action: str, parameters: Mapping[str, Any]) -> ActionResult:
        ok, reason = self.validate(action, parameters)
        return ActionResult(ok=False, status="blocked", reason=reason)

    def status(self) -> dict[str, Any]:
        return {
            "environment_id": self.environment_id,
            "kind": self.kind.value,
            "simulator": False,
            "read_only": True,
            "reader_wired": self._reader is not None,
            "actions": [],
            "observations": self.observations,
            "scope": self.scope,
            "limitations": [
                "read-only: an experiment cannot act on the world from here",
                "the observation is a bounded summary, not the world's content",
            ],
        }


# ── the bridge for real actions ──────────────────────────────────────────────

ExecutorFunction = Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


class BridgedEnvironment:
    """A real environment whose actions run through an INJECTED executor.

    With no executor injected there is no way to act, and every attempt is blocked
    with a reason — the default posture in this build. When one is injected, the
    action's own declaration still decides whether a person must approve it
    (``simulator=False``), and that decision is made by the safety gate, not here.
    """

    def __init__(
        self,
        environment_id: str = "bridged",
        *,
        action_specs: Sequence[Mapping[str, Any]] = (),
        executor: ExecutorFunction | None = None,
        observer: Reader | None = None,
        status_of: Callable[[], Mapping[str, Any]] | None = None,
        candidates: Mapping[str, Sequence[Any]] | None = None,
    ) -> None:
        self.environment_id = as_text(environment_id, "bridged")
        self.kind = EnvironmentKind.BRIDGED
        self.is_simulator = False
        self._specs: dict[str, ActionSpec] = {
            spec.name: spec for spec in (ActionSpec.from_mapping(item) for item in action_specs)
        }
        #: The explanation space the CALLER declares for this environment. Without
        #: one, no hypothesis about it can be formed — which is the honest answer for
        #: a real environment nobody has described, rather than a guess.
        self._candidates: dict[str, tuple[Any, ...]] = {
            str(key): tuple(value)
            for key, value in (candidates or {}).items()
            if isinstance(value, Sequence)
        }
        self._executor = executor
        self._observer = observer
        self._status_of = status_of
        self.executed = 0
        self.blocked = 0
        self._sequence = 0

    async def observe(self) -> EnvironmentObservation:
        self._sequence += 1
        if self._observer is None:
            return EnvironmentObservation(
                observation_ref=f"{self.environment_id}-obs-{self._sequence}",
                values={},
                complete=False,
                quality=0.0,
                timestamp=now_iso(),
                source=self.environment_id,
                kind=EnvironmentKind.BRIDGED,
                note="no observer is wired for this environment",
            )
        return EnvironmentObservation(
            observation_ref=f"{self.environment_id}-obs-{self._sequence}",
            values=dict(self._observer()),
            complete=True,
            quality=1.0,
            timestamp=now_iso(),
            source=self.environment_id,
            kind=EnvironmentKind.BRIDGED,
        )

    def state(self) -> Mapping[str, Any]:
        return dict(self._observer()) if self._observer is not None else {}

    def actions(self) -> tuple[ActionSpec, ...]:
        return tuple(self._specs[name] for name in sorted(self._specs))

    def candidates(self) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        """The candidate explanations the caller declared, if any were."""
        return candidate_rows(self._candidates)

    def validate(self, action: str, parameters: Mapping[str, Any]) -> tuple[bool, str]:
        name = as_text(action)
        if name not in self._specs:
            return False, f"{name!r} is not an action of environment {self.environment_id!r}"
        if self._executor is None:
            return False, (
                f"no executor is wired for environment {self.environment_id!r}, so "
                f"{name!r} cannot run; nothing was attempted"
            )
        return True, ""

    async def execute(self, action: str, parameters: Mapping[str, Any]) -> ActionResult:
        started = now_iso()
        ok, reason = self.validate(action, parameters)
        if not ok:
            self.blocked += 1
            return ActionResult(ok=False, status="blocked", reason=reason)
        executor = self._executor
        if executor is None:  # pragma: no cover - validate already refused
            self.blocked += 1
            return ActionResult(ok=False, status="blocked", reason="no executor is wired")
        self.executed += 1
        try:
            values = await executor(as_text(action), dict(parameters))
        except Exception as exc:  # noqa: BLE001 - an environment error is a result
            return ActionResult(
                ok=False,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                reason="the injected executor raised",
                duration_ms=0.0,
                actions=0,
            )
        duration = elapsed_seconds(started, now_iso()) * 1000.0
        reported = dict(values)
        return ActionResult(
            ok=True,
            status="completed",
            observation=EnvironmentObservation(
                observation_ref=f"{self.environment_id}-obs-{self._sequence + 1}",
                values=reported,
                # An executor that returned nothing observed nothing: the reading is
                # incomplete, which is not the same as a value that did not change.
                complete=bool(reported),
                timestamp=now_iso(),
                source=self.environment_id,
                kind=EnvironmentKind.BRIDGED,
                note=(
                    "reported by the injected executor"
                    if reported
                    else "the executor returned no observed values"
                ),
            ),
            duration_ms=duration,
            actions=1,
        )

    def status(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "environment_id": self.environment_id,
            "kind": self.kind.value,
            "simulator": False,
            "executor_wired": self._executor is not None,
            "observer_wired": self._observer is not None,
            "candidate_actions": sorted(self._candidates),
            "actions": [item.to_dict() for item in self.actions()],
            "executed": self.executed,
            "blocked": self.blocked,
            "limitations": [
                "real actions: every one needs approval, and none run without an executor",
            ],
        }
        if self._status_of is not None:
            reported = self._status_of()
            base["reported"] = dict(reported)
        return base


# ── the registry ─────────────────────────────────────────────────────────────


class EnvironmentRegistry:
    """Which environments this build can experiment in, and what they are.

    The registry holds instances a caller registered; it does not invent one when
    an id is unknown, because "an environment appeared" is how a learning system
    starts experimenting somewhere nobody approved.
    """

    def __init__(self) -> None:
        self._environments: dict[str, Environment] = {}

    def register(self, environment: Environment) -> None:
        self._environments[environment.environment_id] = environment

    def unregister(self, environment_id: str) -> bool:
        return self._environments.pop(as_text(environment_id), None) is not None

    def get(self, environment_id: str) -> Environment | None:
        return self._environments.get(as_text(environment_id))

    def identifiers(self) -> tuple[str, ...]:
        return tuple(sorted(self._environments))

    def all(self) -> tuple[Environment, ...]:
        return tuple(self._environments[key] for key in self.identifiers())

    def simulators(self) -> tuple[str, ...]:
        return tuple(
            key
            for key in self.identifiers()
            if self._environments[key].kind is EnvironmentKind.SIMULATOR
        )

    def status(self) -> dict[str, Any]:
        rows = [self._environments[key].status() for key in self.identifiers()]
        return {
            "environments": rows,
            "count": len(rows),
            "simulators": list(self.simulators()),
            "real": [
                row["environment_id"]
                for row in rows
                if row.get("kind") != EnvironmentKind.SIMULATOR.value
            ],
            "limitations": [
                "only deterministic simulators ship; a real environment appears when a "
                "caller injects one, and it never acts without an executor and approval",
            ],
        }


def environment_from_spec(
    spec: Mapping[str, Any], registry: EnvironmentRegistry
) -> Environment | None:
    """Register and return a simulator declared as data, or ``None`` if unreadable.

    An empty specification is refused rather than turned into a default simulator:
    a caller that described nothing should get nothing, not an environment they did
    not ask for.
    """
    if not isinstance(spec, Mapping) or not spec:
        return None
    environment = DeterministicEnvironment.from_spec(spec)
    if not environment.actions():
        return None
    registry.register(environment)
    return environment


# ── the scenarios this build ships ───────────────────────────────────────────


def _dial() -> DeterministicEnvironment:
    """An unknown state transition: what does ``turn`` do to the level?"""
    return DeterministicEnvironment(
        "sim.dial",
        initial_state={"level": 0},
        rules={"turn": {"level": 3}},
        hidden=("turn",),
        candidates={"turn": ({"level": 3}, {"level": 0})},
        declared_actions={
            "turn": {
                "description": "Turn the dial by one notch.",
                "estimated_seconds": 0.01,
                # The action DECLARES that it may be repeated. That declaration is
                # what makes a second turn a different intervention rather than a
                # duplicate, and it is the only way this build tests a supported
                # hypothesis again (§8/§11).
                "parameters": ("replicate",),
            }
        },
    )


def _switch() -> DeterministicEnvironment:
    """Two explanations that predict different results for the same action."""
    return DeterministicEnvironment(
        "sim.switch",
        initial_state={"lamp": "off"},
        rules={"press": {"lamp": "on"}},
        hidden=("press",),
        candidates={"press": ({"lamp": "on"}, {"lamp": "off"})},
        declared_actions={"press": {"description": "Press the switch.", "estimated_seconds": 0.01}},
    )


def _misleading() -> DeterministicEnvironment:
    """A DECLARED rule the environment then contradicts — a failed prediction."""
    return DeterministicEnvironment(
        "sim.misleading",
        initial_state={"battery": 0},
        rules={"charge": {"battery": 100}},
        candidates={"charge": ({"battery": 100}, {"battery": 0})},
        misleading={"battery": 0},
        declared_actions={
            "charge": {"description": "Charge the battery.", "estimated_seconds": 0.01}
        },
    )


def _blind() -> DeterministicEnvironment:
    """A post-action observation that omits the very key the question is about."""
    return DeterministicEnvironment(
        "sim.blind",
        initial_state={"reading": 0},
        rules={"scan": {"reading": 7}},
        hidden=("scan",),
        candidates={"scan": ({"reading": 7}, {"reading": 0})},
        incomplete_keys=("reading",),
        declared_actions={"scan": {"description": "Scan the sample.", "estimated_seconds": 0.01}},
    )


def _opaque() -> DeterministicEnvironment:
    """Nothing declared: no rule to read and no candidate to test — honestly stuck."""
    return DeterministicEnvironment(
        "sim.opaque",
        initial_state={"gauge": 0},
        rules={"poke": {"gauge": 1}},
        hidden=("poke",),
        declared_actions={"poke": {"description": "Poke the device.", "estimated_seconds": 0.01}},
    )


def builtin_environments() -> EnvironmentRegistry:
    """The registry this build starts with — deterministic simulators, all declared.

    Each scenario exists because a required workflow needs it: an unknown state
    transition, two competing explanations, a rule the environment contradicts, an
    observation that omits what the question was about, and a device that declares
    nothing. They are simulators, they say so in ``kind``, and every report and
    metric that touches them repeats that (§4.4/§10). A caller may register more
    through :func:`environment_from_spec`, and the application registers Phase 22's
    read-only world environment when a world reader is wired.
    """
    registry = EnvironmentRegistry()
    for environment in (_dial(), _switch(), _misleading(), _blind(), _opaque()):
        registry.register(environment)
    return registry
