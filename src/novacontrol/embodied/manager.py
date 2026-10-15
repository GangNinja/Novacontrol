"""The session registry and the ONE door an environment input goes through.

Two responsibilities, kept in one place on purpose:

* **Sessions** — create, list, pause, resume, cancel, run, persist, restore.
  A session is the unit an operator reasons about, so the registry is where its
  budgets are clamped and where its record lives.
* **The input door** — :meth:`EmbodiedManager.execute_step`. Every environment
  input in this build passes through it, because it is the step handler the
  application's own executor calls for an ``environment_input`` action. Safety
  is enforced HERE, at the last place before an input goes out: the session must
  be live, not paused, not awaiting revalidation, the environment must be
  connected and allowed, the duration is clamped, the post-observation is read
  back, and the result is written to the audit trail.

Restart is handled honestly: sessions are persisted, and on restore anything
that was mid-run becomes INTERRUPTED with ``needs_revalidation`` set. No input
is sent until the environment has been reconnected and a fresh structured
reading has been taken — the rule the phase asks for, enforced by the door
rather than remembered by a caller.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from novacontrol.core.audit import AutomationAuditLog, InMemoryAutomationAuditLog
from novacontrol.embodied.agent import (
    AGENT_OBJECTIVE_FAILED,
    AGENT_SESSION_CANCELLED,
    AGENT_SESSION_ENDED,
    AGENT_SESSION_PAUSED,
    EmbodiedAgent,
)
from novacontrol.embodied.environment import (
    EnvironmentRefused,
    EnvironmentRegistry,
)
from novacontrol.embodied.models import (
    ActionKind,
    ActionStatus,
    EmbodiedActionResult,
    EnvironmentAction,
    EnvironmentInfo,
    EnvironmentStatus,
    Evaluation,
    Objective,
    ObjectiveVerdict,
    Observation,
    SessionState,
    SessionStatus,
    TrajectoryEntry,
    validate_objective,
)
from novacontrol.embodied.perception import effect_observed, to_world_observation
from novacontrol.embodied.simulation import FAULT_NAMES, Faults, adapter_for, scenarios

#: The permission scope every embodied input requires. Declared once, here, so
#: the goal's requirement and the action's declaration cannot drift.
ENVIRONMENT_SCOPE = "environment:control"

#: The tool/action name an environment input is declared under on the shared
#: permission layer.
ENVIRONMENT_INPUT = "environment_input"

#: Persistence key. One key holds every session, bounded, and is written on
#: every state change that matters.
STORE_KEY = "embodied_sessions"

#: Hard ceilings a configuration cannot exceed, whatever it asks for.
HARD_MAX_SESSIONS = 4
HARD_MAX_ACTIONS = 400
HARD_MAX_SECONDS = 1800.0
HARD_MAX_INPUT_MS = 2000
HARD_MAX_REPLANS = 20
HARD_MAX_TRAJECTORY = 1000


@dataclass(frozen=True, slots=True)
class EmbodiedSettingsView:
    """The manager's limits, resolved and clamped to hard bounds."""

    enabled: bool = True
    persist: bool = True
    max_sessions: int = 1
    max_actions: int = 40
    max_seconds: float = 120.0
    #: How many RECOVERY attempts one session may spend on failed attempts.
    #: Distinct from the plan count below: "this did not work, try something
    #: else" is a resilience allowance, while "how many plans" is a cost bound.
    max_replans: int = 3
    #: The plan ceiling for one session. It defaults to the same number as the
    #: action ceiling so that planning can never become the binding constraint
    #: before the input budget does — a session that is allowed 40 inputs is
    #: allowed 40 plans to produce them.
    max_plans: int = 40
    max_input_ms: int = 500
    action_interval_ms: int = 50
    refresh_attempts: int = 2
    max_trajectory: int = 200
    environments: tuple[str, ...] = ("gridworld",)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> EmbodiedSettingsView:
        raw = dict(data or {})
        defaults = cls()
        return cls(
            enabled=_bool(raw.get("enabled"), defaults.enabled),
            persist=_bool(raw.get("persist"), defaults.persist),
            max_sessions=_count(raw.get("max_sessions"), defaults.max_sessions, HARD_MAX_SESSIONS),
            max_actions=_count(raw.get("max_actions"), defaults.max_actions, HARD_MAX_ACTIONS),
            max_seconds=_seconds(raw.get("max_seconds"), defaults.max_seconds, HARD_MAX_SECONDS),
            max_replans=_count(raw.get("max_replans"), defaults.max_replans, HARD_MAX_REPLANS),
            max_plans=_count(raw.get("max_plans"), defaults.max_plans, HARD_MAX_ACTIONS),
            max_input_ms=_count(raw.get("max_input_ms"), defaults.max_input_ms, HARD_MAX_INPUT_MS),
            action_interval_ms=_count(
                raw.get("action_interval_ms"), defaults.action_interval_ms, HARD_MAX_INPUT_MS
            ),
            refresh_attempts=_count(raw.get("refresh_attempts"), defaults.refresh_attempts, 5),
            max_trajectory=_count(
                raw.get("max_trajectory"), defaults.max_trajectory, HARD_MAX_TRAJECTORY
            ),
            environments=tuple(
                str(item).strip()
                for item in (raw.get("environments") or defaults.environments)
                if str(item).strip()
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "persist": self.persist,
            "max_sessions": self.max_sessions,
            "max_actions": self.max_actions,
            "max_seconds": self.max_seconds,
            "max_replans": self.max_replans,
            "max_plans": self.max_plans,
            "max_input_ms": self.max_input_ms,
            "action_interval_ms": self.action_interval_ms,
            "refresh_attempts": self.refresh_attempts,
            "max_trajectory": self.max_trajectory,
            "environments": list(self.environments),
        }


class EmbodiedManager:
    """Owns the sessions, the adapters and the one input door."""

    def __init__(
        self,
        *,
        registry: EnvironmentRegistry | None = None,
        world: Any | None = None,
        goals: Any | None = None,
        permissions: Any | None = None,
        #: The SHARED automation audit trail, the same ``AutomationAuditLog`` the
        #: desktop, browser and phone controllers write to. Optional and in
        #: memory by default, exactly like those controllers: every sent input is
        #: appended here once, whatever its ending.
        audit_log: AutomationAuditLog | None = None,
        observer: Callable[[str, Mapping[str, Any]], None] | None = None,
        store: Any | None = None,
        settings: EmbodiedSettingsView | Mapping[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        register_default_adapters: bool = True,
    ) -> None:
        self.settings = (
            settings
            if isinstance(settings, EmbodiedSettingsView)
            else EmbodiedSettingsView.from_mapping(settings)
        )
        self._registry = registry or EnvironmentRegistry(allowed=self.settings.environments)
        if register_default_adapters:
            for name in self.settings.environments or ("gridworld",):
                if self._registry.get(name) is None and name == "gridworld":
                    self._registry.register(adapter_for(scenarios()["open-east"]))
        self.world = world
        self.goals = goals
        self.permissions = permissions
        self.audit_log: AutomationAuditLog = audit_log or InMemoryAutomationAuditLog()
        self._observer = observer
        self._store = store
        self._clock = clock
        self._sessions: dict[str, SessionState] = {}
        self._agents: dict[str, EmbodiedAgent] = {}
        self._order: list[str] = []
        self._running: set[str] = set()
        #: How many inputs each session's environment REFUSED. Recorded here
        #: because this is where the refusal happened: the run the loop sees is
        #: a failed step, and a failure and a refusal are different facts.
        self._refused: dict[str, int] = {}
        self._last_input_at: float = 0.0
        self._restore_report: dict[str, Any] = {}

    # -- environments -----------------------------------------------------------

    def registry(self) -> EnvironmentRegistry:
        return self._registry

    def environments(self) -> tuple[EnvironmentInfo, ...]:
        return self._registry.environments()

    def environment(self, environment_id: str) -> EnvironmentInfo | None:
        for info in self.environments():
            if info.environment_id == environment_id:
                return info
        return None

    def scenarios(self) -> tuple[str, ...]:
        return tuple(sorted(scenarios()))

    # -- sessions ---------------------------------------------------------------

    def create_session(
        self,
        *,
        environment_id: str = "gridworld",
        objective: Objective | Mapping[str, Any],
        scenario: str | None = None,
        faults: Mapping[str, Any] | None = None,
        allow_new_environment: bool = True,
    ) -> SessionState:
        """Record a session. Runs NOTHING — creating is not starting.

        A refused objective, an unknown environment and a disallowed one are
        three different errors here, because an operator reading the message
        needs to know which of them they hold.
        """
        parsed = (
            objective if isinstance(objective, Objective) else Objective.from_mapping(objective)
        )
        valid, why = validate_objective(parsed)
        if not valid:
            raise ValueError(why)
        name = str(environment_id or "").strip()
        adapter = self._registry.get(name)
        if allow_new_environment and name == "gridworld" and (scenario or faults):
            # A scenario (and an injected fault) is CONFIGURATION for the owned
            # simulation, so a differently configured world is its own
            # environment id — ``gridworld:drifting`` — rather than a mutation of
            # the one somebody else may be driving. The allowlist gates the
            # family, so permitting ``gridworld`` permits its scenarios.
            name = self._mint_configuration(name, scenario, faults)
            adapter = self._registry.get(name)
        if adapter is None:
            raise ValueError(
                f"no environment adapter is registered as {name!r}; this build has: "
                + (", ".join(item.environment_id for item in self.environments()) or "none")
            )
        if not self._registry.is_allowed(name):
            raise EnvironmentRefused(
                f"environment {name!r} is not in the allowlist "
                f"({', '.join(self.settings.environments) or 'none'}); it will not be driven"
            )
        scenario_name = (
            str(scenario).strip() if scenario else ("open-east" if name == "gridworld" else "")
        )
        live = [item for item in self._order if not self._sessions[item].terminal]
        if len(live) >= self.settings.max_sessions:
            raise ValueError(
                f"this build allows {self.settings.max_sessions} live session(s); "
                f"{len(live)} already exist. Cancel one, or raise embodied.max_sessions"
            )
        clamped = replace(
            parsed,
            max_actions=min(parsed.max_actions, self.settings.max_actions),
            max_seconds=min(parsed.max_seconds, self.settings.max_seconds),
            max_replans=min(parsed.max_replans, self.settings.max_replans),
            max_input_ms=min(parsed.max_input_ms, self.settings.max_input_ms),
        )
        state = SessionState(
            environment_id=name,
            scenario=scenario_name,
            status=SessionStatus.CREATED,
            objective=clamped,
            created_at=_now(),
            updated_at=_now(),
            reason="created; nothing has been sent to the environment",
        )
        self._sessions[state.session_id] = state
        self._order.append(state.session_id)
        self._agents[state.session_id] = self._build_agent(state, adapter)
        self.save(state.session_id)
        # Creating announces NOTHING on the bus: the session has not started, and
        # the phase's own rule is that creating is not starting. The event fires
        # when the loop connects and takes its first reading.
        return state

    def _mint_configuration(
        self, name: str, scenario: str | None, faults: Mapping[str, Any] | None
    ) -> str:
        """Register the world one configured environment needs, and name it.

        One owner for the configuration → adapter mapping, because creating a
        session and RESTORING one must land in the same world: the id is derived
        from the configuration rather than generated, so the second caller finds
        the first caller's adapter instead of minting a second one.
        """
        configured = _configuration_key(name, scenario, faults)
        if self._registry.get(configured) is None:
            self._registry.register(
                adapter_for(
                    scenario or "open-east",
                    faults=Faults(**dict(faults or {})),
                    environment_id=configured,
                )
            )
        return configured

    def register_adapter(self, adapter: Any) -> None:
        """Add an adapter (a test fixture, a future real environment)."""
        self._registry.register(adapter)

    def sessions(self) -> tuple[SessionState, ...]:
        return tuple(self._sessions[name] for name in self._order)

    def get(self, session_id: str) -> SessionState | None:
        return self._sessions.get(str(session_id))

    def view(self, session_id: str) -> dict[str, Any]:
        state = self._require(session_id)
        payload = state.to_dict()
        info = self.environment(state.environment_id)
        payload["environment"] = info.to_dict() if info is not None else {}
        payload["released"] = bool(getattr(self._agents.get(session_id), "state", state).released)
        return payload

    def summary(self) -> dict[str, Any]:
        rows = self.sessions()
        return {
            "sessions": len(rows),
            "live": sum(1 for item in rows if not item.terminal),
            "completed": sum(1 for item in rows if item.status is SessionStatus.COMPLETED),
            "cancelled": sum(1 for item in rows if item.status is SessionStatus.CANCELLED),
            "interrupted": sum(1 for item in rows if item.status is SessionStatus.INTERRUPTED),
            "actions_sent": sum(item.actions_taken for item in rows),
            "ids": [item.session_id for item in rows],
        }

    def pause(self, session_id: str, reason: str = "") -> SessionState:
        return self._transition(lambda: self._lifecycle(session_id, "pause", reason))

    def resume(self, session_id: str) -> SessionState:
        return self._transition(lambda: self._lifecycle(session_id, "resume", ""))

    def cancel(self, session_id: str, reason: str = "") -> SessionState:
        return self._transition(lambda: self._lifecycle(session_id, "cancel", reason))

    def _lifecycle(self, session_id: str, move: str, reason: str) -> SessionState:
        """Apply one lifecycle move — with the session's environment, or without.

        The environment can be gone by the time the move arrives: a restored
        session whose configured world this process cannot rebuild, or one an
        operator unregistered. The move must still work, because cancelling is
        exactly what an operator does with a session that can no longer run, and
        a session nobody can close holds a live-session slot forever. With no
        adapter there is nothing to release, so the state move is recorded in the
        registry the door reads and announced the same way the agent announces
        it.
        """
        try:
            agent = self._agent(session_id)
        except EnvironmentRefused:
            return self._move_without_environment(session_id, move, reason)
        if move == "pause":
            return agent.pause(reason).with_(updated_at=_now())
        if move == "resume":
            return agent.resume().with_(updated_at=_now())
        return agent.cancel(reason).with_(updated_at=_now())

    def _move_without_environment(self, session_id: str, move: str, reason: str) -> SessionState:
        """A lifecycle move for a session whose environment is not registered."""
        state = self._require(session_id)
        now = _now()
        if move == "pause":
            if state.terminal:
                return state
            updated = state.with_(
                status=SessionStatus.PAUSED,
                released=True,
                reason=reason or "paused by the operator",
                updated_at=now,
            )
            self._announce(
                AGENT_SESSION_PAUSED,
                {"session_id": session_id, "reason": updated.reason},
            )
            return updated
        if move == "resume":
            if state.status is not SessionStatus.PAUSED:
                return state
            # The same rule the agent applies: a pause may have been long enough
            # for the reading to be worthless, so a resumed session is
            # revalidated before any input — and with no environment to
            # revalidate against, running it is refused until one exists.
            return state.with_(
                status=SessionStatus.CREATED,
                needs_revalidation=True,
                released=True,
                reason="resumed; the environment must be revalidated before any input",
                updated_at=now,
            )
        if state.terminal:
            return state
        updated = state.with_(
            status=SessionStatus.CANCELLED,
            released=True,
            reason=reason or "cancelled by the operator",
            finished_at=state.finished_at or now,
            updated_at=now,
        )
        self._announce(
            AGENT_SESSION_ENDED,
            {
                "session_id": session_id,
                "status": updated.status.value,
                "reason": updated.reason,
            },
        )
        self._announce(
            AGENT_OBJECTIVE_FAILED,
            {"session_id": session_id, "reason": updated.reason},
        )
        self._announce(
            AGENT_SESSION_CANCELLED,
            {"session_id": session_id, "reason": updated.reason},
        )
        return updated

    def _transition(self, action: Callable[[], SessionState]) -> SessionState:
        """Apply one lifecycle move and RECORD it.

        The registry is the authority on a session's status, not the agent
        object: the input door, the live-session ceiling, the list surface and
        the next restore all read ``self._sessions``. A transition the registry
        never heard about made a pause cosmetic (the door still saw a live
        session and would send an input), let a cancelled session keep its slot,
        and lost the status on restart — so pausing, resuming and cancelling all
        go through here, which stores the state and persists it.
        """
        state = action()
        session_id = state.session_id
        self._sessions[session_id] = state
        self.save(session_id)
        return state

    async def run_session(self, session_id: str, *, max_actions: int | None = None) -> SessionState:
        """Drive one session's bounded loop.

        A second concurrent run of the SAME session is refused rather than
        queued: two loops sending inputs to one environment is how a controller
        gets two contradictory instructions at once.
        """
        if not self.settings.enabled:
            raise ValueError(
                "embodied sessions are disabled in this build (embodied.enabled=false)"
            )
        state = self._require(session_id)
        if state.status is SessionStatus.PAUSED:
            raise ValueError(
                "this session is paused; resume it (which revalidates the environment) "
                "before running"
            )
        if state.terminal:
            raise ValueError(
                f"this session is already {state.status.value}; create a new session instead"
            )
        if session_id in self._running:
            raise ValueError("this session is already running; one loop per session")
        self._running.add(session_id)
        try:
            try:
                agent = self._agent(session_id)
            except EnvironmentRefused as exc:
                # A pre-flight refusal, not an ending: the loop never started, so
                # the session keeps its status and can run once its environment
                # exists. Raised as a refusal because that is what it is — an
                # unregistered environment is not something an agent can act on,
                # and reporting it as a 500 hid the session's real state.
                raise ValueError(
                    f"{exc}; this session cannot run until that environment is "
                    "registered in this build"
                ) from exc
            agent.max_actions = max(1, int(max_actions or self.settings.max_actions))
            agent.state = state.with_(observations_taken=state.observations_taken)
            result = await agent.run(max_actions=max_actions)
            self._sessions[session_id] = result
            self.save(session_id)
            return result
        finally:
            self._running.discard(session_id)

    # -- the input door ---------------------------------------------------------

    async def execute_step(self, step: Any, context: Any = None) -> Mapping[str, Any]:
        """Send ONE environment input — the only place an input may leave.

        Called by the application's plan step handler for an
        ``environment_input`` action, which means the input already travelled
        Phase 24's policy, its fresh-state gate and the executor's verification
        ordering. This method adds the session-level rules: the session must be
        live, the environment allowed and connected, the duration clamped, the
        result observed back, and the outcome audited.
        """
        parameters = dict(getattr(step, "parameters", {}) or {})
        session_id = str(parameters.get("session_id") or "")
        state = self._sessions.get(session_id)
        if state is None:
            raise RuntimeError(
                f"step {getattr(step, 'id', '')!r} names session {session_id!r}, which does "
                "not exist; no input was sent"
            )
        name = str(parameters.get("input") or "").strip().lower()
        if not name:
            raise RuntimeError(f"step {getattr(step, 'id', '')!r} names no environment input")
        if state.status is SessionStatus.PAUSED:
            raise PermissionError("the session is paused, so no input was sent; resume it first")
        if state.terminal:
            raise PermissionError(
                f"the session is {state.status.value}, so no further input was sent"
            )
        if state.needs_revalidation:
            raise PermissionError(
                "the session has not been revalidated against a live environment since the "
                "restart, so no input was sent; run the session to revalidate first"
            )
        try:
            adapter = self._registry.require(state.environment_id)
        except EnvironmentRefused as exc:
            raise PermissionError(str(exc)) from exc
        if adapter.status() is not EnvironmentStatus.OPERATIONAL:
            raise RuntimeError(
                f"environment {state.environment_id!r} is not operational "
                f"({adapter.status().value}); no input was sent"
            )

        interval = max(0, self.settings.action_interval_ms) / 1000.0
        if interval and self._last_input_at:
            waited = self._clock() - self._last_input_at
            if waited < interval:
                time.sleep(interval - waited)
        self._last_input_at = self._clock()

        duration_ms = min(
            int(parameters.get("duration_ms") or 0),
            self.settings.max_input_ms,
            state.objective.max_input_ms or self.settings.max_input_ms,
        )
        expected = parameters.get("expected")
        expected_facts = _expected_facts(expected)
        action = EnvironmentAction(
            session_id=session_id,
            kind=_kind_for(name),
            input_name=name,
            parameters={
                key: value
                for key, value in parameters.items()
                if key in {"direction", "move", "target", "cell", "position", "landing"}
            },
            duration_ms=duration_ms,
            preconditions=tuple(getattr(step, "parameters", {}).get("preconditions") or ()),
            expected_effect=str(expected or ""),
            effects=tuple(parameters.get("effects") or ()),
            effect=str(getattr(step, "effect", "local_write")),
            permission=ENVIRONMENT_SCOPE,
            reversible=True,
            destructive=False,
            release_after=True,
            rationale=str(getattr(step, "description", "") or ""),
        )
        risk = self._risk_of(name)
        result = adapter.send_input(action)
        await self._audit(action, result, risk)

        observed = result.observed
        if observed is not None:
            self._remember(state, observed)
        if result.status is ActionStatus.TIMED_OUT:
            # The input may or may not have landed. It is NOT re-sent here —
            # that decision belongs to the loop, which re-observes first.
            state = state.with_(
                actions_taken=state.actions_taken + 1,
                last_action=action,
                last_result=result,
                updated_at=_now(),
                reason=(
                    "an input timed out; the environment state will be re-read before "
                    "anything else is sent"
                ),
            )
            self._sessions[session_id] = state
            self.save(session_id)
            raise TimeoutError(
                "the environment accepted the input but never reported back; the "
                "state must be re-observed before another attempt"
            )
        if result.status is ActionStatus.REJECTED:
            self._refused[session_id] = self._refused.get(session_id, 0) + 1
            state = replace(
                state,
                actions_taken=state.actions_taken + 1,
                last_action=action,
                last_result=result,
                updated_at=_now(),
            )
            self._sessions[session_id] = state
            self.save(session_id)
            raise RuntimeError(f"the environment rejected the input: {result.detail}")
        if not result.ok:
            state = replace(
                state,
                actions_taken=state.actions_taken + 1,
                last_action=action,
                last_result=result,
                updated_at=_now(),
            )
            self._sessions[session_id] = state
            self.save(session_id)
            raise RuntimeError(
                f"the environment failed the input ({result.status.value}): "
                f"{result.error or result.detail}"
            )

        matched, verification = (
            effect_observed(expected_facts, observed)
            if observed
            else (
                None,
                "the environment returned no observation to check against",
            )
        )
        changed = True if matched else (False if matched is False else None)
        output: dict[str, Any] = {
            "session_id": session_id,
            "input": name,
            "status": result.status.value,
            "changed": changed,
            "verified": matched,
            "verification": verification,
            "detail": result.detail,
            "observed": _observed_summary(observed),
            "released": result.released,
        }
        for fact in expected_facts:
            key, _, value = fact.partition("=")
            output[key.strip()] = _coerce(value.strip())
        if observed is not None:
            for key, value in dict(observed.structured or {}).items():
                output[f"env.{key}"] = value
        state = replace(
            state,
            actions_taken=state.actions_taken + 1,
            last_action=action,
            last_result=replace(result, verified=matched, verification=verification),
            last_observation=observed or state.last_observation,
            updated_at=_now(),
        )
        self._sessions[session_id] = state
        self.save(session_id)
        return output

    # -- persistence ------------------------------------------------------------

    def restore(self) -> dict[str, Any]:
        """Rebuild the sessions a previous process held — as INTERRUPTED.

        Nothing here reconnects an environment or sends an input: a restored
        session must be revalidated first, and that is enforcement, not advice.
        """
        report: dict[str, Any] = {"restored": 0, "skipped": 0, "interrupted": [], "reason": ""}
        if self._store is None:
            report["reason"] = "no state store is wired, so nothing was restored"
            self._restore_report = report
            return report
        payload = self._store.read(STORE_KEY) or {}
        rows = payload.get("sessions") if isinstance(payload, dict) else None
        if not isinstance(rows, Mapping):
            report["reason"] = "no stored sessions were found"
            self._restore_report = report
            return report
        for session_id, record in rows.items():
            if not isinstance(record, Mapping):
                report["skipped"] += 1
                continue
            try:
                state = _session_from_mapping(record)
            except Exception:  # noqa: BLE001 - an unreadable record is skipped, not fatal
                report["skipped"] += 1
                continue
            if state.terminal:
                # A finished session is history, not work: keeping it would let
                # a completed objective be re-run by accident.
                report["skipped"] += 1
                continue
            restored = replace(
                state,
                status=SessionStatus.INTERRUPTED,
                needs_revalidation=True,
                released=True,
                reason=(
                    "restored after a restart; the environment and the session must be "
                    "revalidated before any input is sent"
                ),
                updated_at=_now(),
            )
            adapter = self._restore_environment(restored)
            if adapter is None:
                # A world this process cannot rebuild is STATED in the record
                # rather than left looking runnable: the session stays readable
                # and cancellable, and running it is refused with this reason.
                restored = replace(
                    restored,
                    reason=(
                        f"restored after a restart, but its environment "
                        f"{restored.environment_id!r} is not one this build can "
                        "rebuild, so it cannot run until that environment is available"
                    ),
                )
            self._sessions[session_id] = restored
            if session_id not in self._order:
                self._order.append(session_id)
            if adapter is not None:
                self._agents[session_id] = self._build_agent(restored, adapter)
            report["restored"] += 1
            report["interrupted"].append(session_id)
        self._restore_report = report
        return report

    def restore_report(self) -> dict[str, Any]:
        return dict(self._restore_report)

    def _restore_environment(self, state: SessionState) -> Any | None:
        """The environment a restored session needs, rebuilt when it is missing.

        A session records the CONFIGURED environment (``gridworld:drifting``),
        and a new process has only minted the default one — so without this a
        restored scenario session could never be revalidated and would sit
        unusable forever, which is not something "restored" may mean. The rebuild
        is deterministic because the key is this manager's own encoding: the
        scenario comes from the record and the fault budget is read back off the
        id. A configuration that cannot be rebuilt returns None and is reported,
        never invented.
        """
        adapter = self._registry.get(state.environment_id)
        if adapter is not None:
            return adapter
        try:
            family, scenario, faults = _split_configuration_key(state.environment_id)
        except ValueError:
            return None
        scenario = state.scenario or scenario
        if not scenario or family != "gridworld" or not self._registry.is_allowed(family):
            return None
        try:
            return self._registry.get(self._mint_configuration(family, scenario, faults))
        except KeyError:
            # An unknown scenario is a configuration this build no longer has.
            return None
        except Exception:  # noqa: BLE001 - an unbuildable world is reported, not fatal
            return None

    def save(self, session_id: str) -> bool:
        if self._store is None or not self.settings.persist:
            return False
        state = self._sessions.get(str(session_id))
        if state is None:
            return False
        payload = {
            "sessions": {item.session_id: _session_to_mapping(item) for item in self.sessions()}
        }
        try:
            self._store.write(STORE_KEY, payload)
        except Exception:  # noqa: BLE001 - persistence never breaks a session
            return False
        return True

    def close(self) -> None:
        """Release every held input and shut every adapter down."""
        for agent in self._agents.values():
            try:
                agent._release()
            except Exception:  # noqa: BLE001 - shutting down is best effort
                continue
        for adapter in self._registry.adapters():
            try:
                adapter.shutdown()
            except Exception:  # noqa: BLE001
                continue

    # -- status -----------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """The operator surface: counts, statuses and limits — no content."""
        return {
            "enabled": self.settings.enabled,
            "defaults": self.settings.to_dict(),
            "environments": [item.to_dict() for item in self.environments()],
            "allowed": list(self.settings.environments),
            "scenarios": list(self.scenarios()),
            "sessions": self.summary(),
            "restore": dict(self._restore_report),
            "wired": {
                "planner": self.goals is not None,
                "world_model": self.world is not None,
                "permissions": self.permissions is not None,
                "audit_log": type(self.audit_log).__name__,
                "persistence": self._store is not None,
            },
            "scope": ENVIRONMENT_SCOPE,
            "limits": {
                "max_sessions": self.settings.max_sessions,
                "max_actions": self.settings.max_actions,
                "max_seconds": self.settings.max_seconds,
                "max_replans": self.settings.max_replans,
                "max_plans": self.settings.max_plans,
                "max_input_ms": self.settings.max_input_ms,
            },
        }

    # -- internals --------------------------------------------------------------

    def _require(self, session_id: str) -> SessionState:
        state = self._sessions.get(str(session_id))
        if state is None:
            raise KeyError(f"no session {session_id!r} exists")
        return state

    def _agent(self, session_id: str) -> EmbodiedAgent:
        state = self._require(session_id)
        agent = self._agents.get(session_id)
        if agent is None:
            adapter = self._registry.get(state.environment_id)
            if adapter is None:
                raise EnvironmentRefused(
                    f"environment {state.environment_id!r} is no longer registered"
                )
            agent = self._build_agent(state, adapter)
            self._agents[session_id] = agent
        agent.state = state
        return agent

    def _build_agent(self, state: SessionState, adapter: Any) -> EmbodiedAgent:
        session_id = state.session_id
        return EmbodiedAgent(
            state=state,
            adapter=adapter,
            goals=self.goals,
            world=self.world,
            permissions=self.permissions,
            observer=self._observer,
            # The input door owns the count of inputs that were actually sent;
            # the loop reads it here instead of keeping a second tally.
            input_counter=lambda: self._sessions.get(session_id, state).actions_taken,
            refusal_counter=lambda: self._refused.get(session_id, 0),
            on_state=self._on_agent_state,
            plan_ceiling=self.settings.max_plans,
            refresh_attempts=self.settings.refresh_attempts,
            max_trajectory=self.settings.max_trajectory,
            max_actions=self.settings.max_actions,
        )

    def _on_agent_state(self, state: SessionState) -> None:
        """Mirror the agent's own state into the registry — it is the authority.

        Every transition the LOOP makes (clearing ``needs_revalidation`` after a
        revalidation, a status move, the verdict, the counters) has to land in
        the registry, because that is what the input door, the list surface and
        the boot restore read. The one thing the agent does NOT own is what the
        door actually SENT: ``actions_taken``, ``last_action`` and
        ``last_result`` are written by the door while the run is in flight, and
        the agent reads that count back rather than keeping a second tally — so
        those three are taken from the registry and never overwritten here.
        """
        current = self._sessions.get(state.session_id)
        if current is not None:
            state = state.with_(
                actions_taken=current.actions_taken,
                last_action=state.last_action or current.last_action,
                last_result=state.last_result or current.last_result,
            )
        self._sessions[state.session_id] = state

    def _remember(self, state: SessionState, observation: Observation) -> None:
        if self.world is None:
            return
        try:
            self.world.observe(
                to_world_observation(
                    observation,
                    session_id=state.session_id,
                    scope=state.environment_id,
                )
            )
        except Exception:  # noqa: BLE001 - remembering is not the work
            return

    def _risk_of(self, input_name: str) -> str:
        layer = self.permissions
        if layer is None:
            return ""
        try:
            return str(layer.risk_of(ENVIRONMENT_INPUT).value)
        except Exception:  # noqa: BLE001 - a layer that cannot answer is unknown
            return ""

    async def _audit(self, action: EnvironmentAction, result: Any, risk: str) -> None:
        """Append one consequential input to the shared automation audit trail.

        Once per input, on EVERY ending — completed, refused, timed out, failed —
        and never as part of the work: a trail that can raise would be a new way
        for an input to fail. The record is the SAME ``AuditedResult`` shape the
        desktop, browser and phone controllers append, so "what did automation
        do" is one question with one answer.
        """
        row = EmbodiedActionResult(
            action_id=action.action_id,
            status=result.status,
            output={
                "session_id": action.session_id,
                "environment_id": str(getattr(result, "environment_id", "") or ""),
                "input": action.input_name,
                "kind": action.kind.value,
                "risk": risk,
                "released": bool(result.released),
                "verified": result.verified,
                "detail": str(result.detail or "")[:200],
                "at": _now(),
            },
            error=str(result.error or "") or None,
        )
        try:
            await self.audit_log.append(row)
        except Exception:  # noqa: BLE001 - the audit never breaks the work
            return

    def _announce(self, kind: str, payload: Mapping[str, Any]) -> None:
        if self._observer is None:
            return
        try:
            self._observer(kind, dict(payload))
        except Exception:  # noqa: BLE001
            return


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _split_configuration_key(key: str) -> tuple[str, str, dict[str, int]]:
    """Read back what :func:`_configuration_key` wrote: family, scenario, faults.

    The encoder is this module's own, so the decoder is too — that is what lets a
    restored session land in the world it was created in instead of a default
    one. A key that names no scenario raises ``ValueError`` rather than guessing.
    """
    parts = str(key or "").split(":")
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        raise ValueError(f"{key!r} names no configured environment")
    return parts[0].strip(), parts[1].strip(), _faults_from_tags(":".join(parts[2:]))


def _faults_from_tags(text: str) -> dict[str, int]:
    """The fault budget a configuration key encodes, read back by name."""
    rows: dict[str, int] = {}
    for tag in str(text or "").split("+"):
        for name in sorted(FAULT_NAMES, key=len, reverse=True):
            if tag.startswith(name):
                count = _as_int(tag[len(name) :])
                if count:
                    rows[name] = count
                break
    return rows


def _configuration_key(
    environment_id: str, scenario: str | None, faults: Mapping[str, Any] | None
) -> str:
    """The id of one configured environment, readable and reproducible.

    ``gridworld:drifting`` for a scenario, with the fault budget appended when
    one was asked for (``gridworld:open-east:drift2``), so a status surface names
    the world a session is actually in rather than "gridworld" for all of them.
    """
    parts = [str(environment_id).strip(), str(scenario or "open-east").strip()]
    tags = [
        f"{str(key)}{int(value)}"
        for key, value in sorted(dict(faults or {}).items())
        if _as_int(value)
    ]
    if tags:
        parts.append("+".join(tags))
    return ":".join(parts)


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _kind_for(name: str) -> ActionKind:
    if name == "move":
        return ActionKind.MOVE
    if name == "interact":
        return ActionKind.INTERACT
    if name == "observe":
        return ActionKind.OBSERVE
    if name == "release":
        return ActionKind.RELEASE
    return ActionKind.WAIT


def _expected_facts(expected: Any) -> tuple[str, ...]:
    """The ``key=value`` facts an input claims it will establish."""
    if expected is None:
        return ()
    if isinstance(expected, str):
        return tuple(item.strip() for item in expected.split(",") if item.strip() and "=" in item)
    return tuple(str(item).strip() for item in expected if str(item).strip())


def _coerce(text: str) -> Any:
    lowered = text.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(text)
    except (TypeError, ValueError):
        pass
    try:
        return float(text)
    except (TypeError, ValueError):
        pass
    return text


def _observed_summary(observation: Observation | None) -> dict[str, Any]:
    if observation is None:
        return {}
    return {
        "observation_id": observation.observation_id,
        "basis": observation.basis.value,
        "freshness": observation.freshness.value,
        "structured": {
            key: value
            for key, value in dict(observation.structured or {}).items()
            if key in {"agent.x", "agent.y", "target.collected", "tick", "at_target"}
        },
    }


def _session_to_mapping(state: SessionState) -> dict[str, Any]:
    return {
        "session_id": state.session_id,
        "environment_id": state.environment_id,
        "scenario": state.scenario,
        "status": state.status.value,
        "reason": state.reason,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "started_at": state.started_at,
        "finished_at": state.finished_at,
        "objective": state.objective.to_dict(),
        "verdict": state.verdict.to_dict(),
        "goal_id": state.goal_id,
        "actions_taken": state.actions_taken,
        "observations_taken": state.observations_taken,
        "replans_used": state.replans_used,
        "recoveries": state.recoveries,
        "needs_revalidation": state.needs_revalidation,
        "released": state.released,
        "trajectory": [item.to_dict() for item in state.trajectory],
        "evaluation": state.evaluation.to_dict(),
    }


def _session_from_mapping(record: Mapping[str, Any]) -> SessionState:
    objective = Objective.from_mapping(record.get("objective") or {})
    try:
        status = SessionStatus(str(record.get("status") or SessionStatus.CREATED.value))
    except ValueError:
        status = SessionStatus.INTERRUPTED
    verdict_payload = record.get("verdict") or {}
    verdict = ObjectiveVerdict(
        met=bool(verdict_payload.get("met")),
        verified=bool(verdict_payload.get("verified")),
        reason=str(verdict_payload.get("reason") or ""),
        missing=tuple(verdict_payload.get("missing") or ()),
        observed=tuple(verdict_payload.get("observed") or ()),
        observation_id=str(verdict_payload.get("observation_id") or ""),
    )
    trajectory = tuple(
        TrajectoryEntry(
            index=int(item.get("index") or 0),
            at=str(item.get("at") or ""),
            phase=str(item.get("phase") or ""),
            summary=str(item.get("summary") or ""),
            status=str(item.get("status") or ""),
            expected=str(item.get("expected") or ""),
            observed=str(item.get("observed") or ""),
            verified=item.get("verified"),
            basis=str(item.get("basis") or ""),
            detail={str(key): value for key, value in dict(item.get("detail") or {}).items()},
        )
        for item in (record.get("trajectory") or ())
        if isinstance(item, Mapping)
    )
    return SessionState(
        session_id=str(record.get("session_id") or ""),
        environment_id=str(record.get("environment_id") or "gridworld"),
        scenario=str(record.get("scenario") or ""),
        status=status,
        reason=str(record.get("reason") or ""),
        created_at=str(record.get("created_at") or ""),
        updated_at=str(record.get("updated_at") or ""),
        objective=objective,
        verdict=verdict,
        goal_id=str(record.get("goal_id") or ""),
        actions_taken=int(record.get("actions_taken") or 0),
        observations_taken=int(record.get("observations_taken") or 0),
        replans_used=int(record.get("replans_used") or 0),
        recoveries=int(record.get("recoveries") or 0),
        trajectory=trajectory,
        # The evaluation is part of the record the store writes, so it is read
        # back rather than silently replaced by an empty one: a stored field
        # nobody reads is data loss waiting for the next change, and the record
        # is what a status surface publishes.
        evaluation=Evaluation.from_mapping(record.get("evaluation") or {}),
        needs_revalidation=bool(record.get("needs_revalidation")),
        released=bool(record.get("released", True)),
        started_at=str(record.get("started_at") or ""),
        finished_at=str(record.get("finished_at") or ""),
    )


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _count(value: Any, default: int, ceiling: int) -> int:
    try:
        number = int(value) if value is not None else int(default)
    except (TypeError, ValueError):
        number = int(default)
    return max(1, min(number, ceiling))


def _seconds(value: Any, default: float, ceiling: float) -> float:
    try:
        number = float(value) if value is not None else float(default)
    except (TypeError, ValueError):
        number = float(default)
    return max(0.1, min(number, ceiling))


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


__all__ = [
    "ENVIRONMENT_INPUT",
    "ENVIRONMENT_SCOPE",
    "HARD_MAX_ACTIONS",
    "HARD_MAX_INPUT_MS",
    "HARD_MAX_SECONDS",
    "HARD_MAX_SESSIONS",
    "STORE_KEY",
    "EmbodiedManager",
    "EmbodiedSettingsView",
]
