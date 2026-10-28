"""NF-08 C — the bounded, state-dependent closed loop.

    Observe -> Interpret -> Select -> Validate -> Execute -> Observe -> Verify
              ^                                                      |
              +---------------------- continue or stop <------------+

This is a *supervisor*, not a second brain. Every capability it needs already
exists and is injected:

* observations come from :mod:`novacontrol.novaforge.observation`;
* decisions come from a :class:`~novacontrol.novaforge.policy.DecisionProvider`;
* validation, digest binding, the Guardian, the action budget and the input
  guard are the EXISTING :class:`~novacontrol.novaforge.manager.NovaForgeManager`
  methods — the loop never injects input itself;
* the verdict is the existing objective verifier's, read from the target.

The loop's own responsibilities are the ones a caller cannot get elsewhere:
stopping. It stops before acting on a stale or unreadable observation, when the
target changes, when the plan is refused, when the budget or deadline is spent,
when the operator cancels, and when the policy has nothing honest left to say.
Every stop is a recorded reason, never a silent success.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from .observation import (
    DEFAULT_MAX_FRAME_AGE,
    GameObservation,
    ObservationBuilder,
    ObservationHistory,
    summarise,
)
from .policy import DecisionProposal, DecisionProvider, PolicyUnavailable

#: Terminal states of one loop run. ``completed`` means the VERIFIER decided
#: success — never that the plan finished.
LOOP_TERMINAL = (
    "completed",
    "failed",
    "stopped",
    "refused",
    "canceled",
    "unavailable",
    "error",
)


def utc_stamp() -> str:
    return datetime.now(UTC).isoformat()


@runtime_checkable
class SessionGateway(Protocol):
    """The slice of the session manager a loop is allowed to use."""

    def get(self, session_id: str) -> Any | None:
        """The live session record, for its pinned target."""

    def plan_action(self, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and digest-bind one action (injects nothing)."""

    async def execute_action(
        self, session_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Execute one approved proposal through the guarded runner."""

    async def verify_objective(
        self, session_id: str, *, reason: str = "manual", action_id: str = ""
    ) -> dict[str, Any]:
        """The independent verifier's current verdict."""


@dataclass(frozen=True, slots=True)
class LoopLimits:
    """The bounds a loop may not exceed, whatever the policy wants."""

    max_actions: int = 8
    deadline_seconds: float = 60.0
    verify_timeout_seconds: float = 20.0
    #: One whole observation (probe + capture + OCR + optional model) is bounded
    #: by the builder's per-probe timeouts; this is the outer ceiling.
    observation_timeout_seconds: float = 15.0
    #: A policy is a dependency like any other: one that never returns would hold
    #: the loop, the cancel path and the operator's stop-button for ever. The
    #: decision gets its own bound, and a spent bound STOPS the loop.
    decision_timeout_seconds: float = 10.0
    #: A budget above the action policy's own ceiling is refused at construction.
    MAX_ACTIONS_CEILING: int = 64

    def errors(self) -> list[str]:
        problems: list[str] = []
        if not 1 <= int(self.max_actions) <= self.MAX_ACTIONS_CEILING:
            problems.append(
                f"max_actions must be between 1 and {self.MAX_ACTIONS_CEILING}."
            )
        if not 0 < float(self.deadline_seconds) <= 3600:
            problems.append("deadline_seconds must be between 0 and 3600.")
        if float(self.verify_timeout_seconds) <= 0:
            problems.append("verify_timeout_seconds must be positive.")
        if not 0 < float(self.decision_timeout_seconds) <= 600:
            problems.append("decision_timeout_seconds must be between 0 and 600.")
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_actions": self.max_actions,
            "deadline_seconds": self.deadline_seconds,
            "verify_timeout_seconds": self.verify_timeout_seconds,
            "observation_timeout_seconds": self.observation_timeout_seconds,
            "decision_timeout_seconds": self.decision_timeout_seconds,
        }


@dataclass(slots=True)
class LoopStep:
    """One observed decision: what was seen, chosen, planned and executed."""

    index: int
    at: str
    observation: dict[str, Any]
    proposal: dict[str, Any] | None = None
    plan_status: str = ""
    execute_status: str = ""
    executed: bool = False
    reason: str = ""
    remaining_actions: int | None = None
    expected_outcome: str = ""
    outcome_met: bool | None = None
    #: Measurement, never a score: how old the frame was when the decision was
    #: made, how long the policy took, and how many evidence references the
    #: proposal cited. ``None`` means the observation carried no frame age —
    #: not zero.
    observation_age_seconds: float | None = None
    #: Sub-millisecond policies are common; integer milliseconds rounded a real
    #: measurement to 0 and made "fast" indistinguishable from "unmeasured".
    decision_latency_ms: float | None = None
    evidence_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "at": self.at,
            "observation": dict(self.observation),
            "proposal": dict(self.proposal) if self.proposal else None,
            "plan_status": self.plan_status,
            "execute_status": self.execute_status,
            "executed": self.executed,
            "reason": self.reason,
            "remaining_actions": self.remaining_actions,
            "expected_outcome": self.expected_outcome,
            "outcome_met": self.outcome_met,
            "observation_age_seconds": self.observation_age_seconds,
            "decision_latency_ms": self.decision_latency_ms,
            "evidence_count": self.evidence_count,
        }


@dataclass(slots=True)
class LoopResult:
    """What one loop run did and why it ended."""

    status: str
    reason: str
    session_id: str = ""
    policy: str = ""
    policy_version: int = 0
    #: True when at least one decision in this run was made from a model's
    #: output. ``False`` is the ordinary deterministic case, not a failure.
    model_backed: bool = False
    started_at: str = ""
    ended_at: str = ""
    steps: list[LoopStep] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] | None = None
    objective: dict[str, Any] | None = None
    limits: dict[str, Any] = field(default_factory=dict)
    actions_executed: int = 0
    actions_refused: int = 0
    decisions: int = 0
    elapsed_seconds: float | None = None

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "session_id": self.session_id,
            "policy": self.policy,
            "policy_version": self.policy_version,
            "model_backed": self.model_backed,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "steps": [step.to_dict() for step in self.steps],
            "observations": list(self.observations),
            "verification": dict(self.verification) if self.verification else None,
            "objective": dict(self.objective) if self.objective else None,
            "limits": dict(self.limits),
            "actions_executed": self.actions_executed,
            "actions_refused": self.actions_refused,
            "decisions": self.decisions,
            "elapsed_seconds": self.elapsed_seconds,
            "note": (
                "A completed run means the objective's verifier decided success from the "
                "target's own evidence. It does not mean the plan ran without incident, "
                "and a stopped run is not a failure unless the verifier said so."
            ),
        }


def _proposal_problem(proposal: Any) -> str:
    """Why a policy's answer is not a usable proposal, or "" when it is.

    A policy is a plugin point, so its answer is checked HERE rather than
    trusted: a proposal with no action, no reason or an unreadable expectation
    must stop the run with a stated fault instead of reaching the planner with
    a shape the gates do not expect. ``None`` is not a malformed proposal — it
    is the policy's explicit "nothing honest left to do" and is handled by the
    caller.
    """
    if proposal is None:
        return ""
    action = getattr(proposal, "action", None)
    if not isinstance(action, Mapping):
        return f"its action is {type(action).__name__}, not a mapping."
    kind = action.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        return "its action states no kind."
    reason = getattr(proposal, "reason", None)
    if not isinstance(reason, str) or not reason.strip():
        return "it states no reason."
    evidence = getattr(proposal, "evidence", None)
    if evidence is not None and (isinstance(evidence, str) or not isinstance(evidence, Sequence)):
        return "its evidence is not a sequence of references."
    expects = getattr(proposal, "expects", None)
    if expects is not None and not isinstance(expects, Mapping):
        return "its expectations are not a mapping."
    return ""


def _evaluate_expectation(
    proposal: DecisionProposal, observation: GameObservation
) -> bool | None:
    """Did the next observation show what the action predicted? None = unknown."""
    expects = dict(proposal.expects or {})
    if not expects:
        return None
    checks: list[bool] = []
    if "title_contains" in expects:
        needle = str(expects["title_contains"])
        checks.append(needle.lower() in observation.title.lower())
    if "text_contains" in expects:
        needle = str(expects["text_contains"])
        text = observation.ocr.text if observation.ocr is not None else ""
        checks.append(needle.lower() in text.lower())
    if "title_equals" in expects:
        checks.append(observation.title == str(expects["title_equals"]))
    return all(checks) if checks else None


class ClosedLoopRunner:
    """Run a policy against one live session, inside every existing gate."""

    def __init__(
        self,
        *,
        gateway: SessionGateway,
        observations: ObservationBuilder,
        policy: DecisionProvider,
        limits: LoopLimits | None = None,
        cancel: asyncio.Event | None = None,
        history_limit: int = 12,
        clock: Callable[[], float] = time.monotonic,
        annotate: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.gateway = gateway
        self.observations = observations
        self.policy = policy
        self.limits = limits or LoopLimits()
        problems = self.limits.errors()
        if problems:
            raise ValueError("Invalid loop limits: " + "; ".join(problems))
        self.cancel = cancel or asyncio.Event()
        self.history = ObservationHistory(limit=history_limit)
        self.clock = clock
        self.annotate = annotate

    # -- capability ---------------------------------------------------------

    def capability(self) -> dict[str, Any]:
        observation = self.observations.capability()
        policy = (
            self.policy.capability()
            if hasattr(self.policy, "capability")
            else {
                "policy": self.policy.name,
                "available": bool(self.policy.available),
                "model_backed": False,
                "reason": "",
            }
        )
        available = bool(policy.get("available")) and observation["observation"]["capture"]
        return {
            "closed_loop": {
                "available": available,
                # A top-level reason, so a caller that does not dig into the
                # nested rows still learns what this pipeline can and cannot do.
                "reason": (
                    "The loop observes the target, asks the installed policy for one action, "
                    "and executes it through the session's plan/execute gates; it stops at "
                    "the first observation it cannot trust."
                    if available
                    else str(
                        policy.get("reason")
                        or observation["observation"].get("reason")
                        or "No decision policy or capture seam is available on this host."
                    )
                ),
                "observation": observation["observation"],
                "interpretation": observation["interpretation"],
                "policy": policy,
                "limits": self.limits.to_dict(),
                "safety": {
                    "guarded_actions": True,
                    "reason": (
                        "Every action is planned and executed by the session manager, so the "
                        "ActionPolicy, digest binding, Guardian, action budget and input guard "
                        "apply exactly as they do for a manual action."
                    ),
                },
                "stopping": [
                    "the observation is stale, degraded or unavailable",
                    "the target window changed or vanished",
                    "the plan was refused or the action was denied",
                    "the action budget or wall-clock deadline was spent",
                    "the operator cancelled",
                    "the policy proposed nothing",
                    "the policy did not decide inside its bound",
                    "the policy returned a malformed proposal or raised",
                ],
            }
        }

    # -- run ----------------------------------------------------------------

    async def run(
        self,
        session_id: str,
        *,
        verify_before_first_action: bool = True,
        describe_prompt: str = "Describe the visible game state in one sentence.",
    ) -> LoopResult:
        started = self.clock()
        started_at = utc_stamp()
        result = LoopResult(
            status="stopped",
            reason="",
            session_id=session_id,
            policy=self.policy.name,
            policy_version=int(getattr(self.policy, "version", 0) or 0),
            started_at=started_at,
            limits=self.limits.to_dict(),
        )
        record = self.gateway.get(session_id)
        if record is None:
            result.status = "unavailable"
            result.reason = f"Session {session_id!r} does not exist, so there is nothing to observe."
            result.ended_at = utc_stamp()
            return result
        target = getattr(record, "target", None)
        if target is None:
            result.status = "unavailable"
            result.reason = (
                "The session has not validated a target window; a loop never acts on an "
                "unpinned window."
            )
            result.ended_at = utc_stamp()
            return result
        handle = int(getattr(target, "handle", 0) or 0)
        pid = int(getattr(target, "pid", 0) or 0) or None
        result.objective = (
            record.objective.to_dict() if getattr(record, "objective", None) else None
        )

        if not bool(getattr(self.policy, "available", False)):
            result.status = "unavailable"
            result.reason = (
                "No decision policy is available; autonomous decisions are unavailable in "
                "this build rather than simulated by a fixed script."
            )
            result.ended_at = utc_stamp()
            return result

        if verify_before_first_action:
            verdict = await self._verify(session_id, reason="loop_precheck", result=result)
            outcome = str((verdict or {}).get("outcome") or "")
            # A precheck can only SAVE work (the objective already holds). A
            # ``verified_failure`` here is the ordinary starting state — the
            # marker is not on screen yet, which is why the loop exists — so it
            # is recorded and the loop proceeds instead of ending as failed
            # before its first action.
            if outcome == "verified_success":
                result.status = "completed"
                result.reason = str((verdict or {}).get("reason") or "The verifier decided.")
                result.ended_at = utc_stamp()
                result.elapsed_seconds = round(self.clock() - started, 3)
                return result

        index = 0
        while True:
            if self.cancel.is_set():
                result.status = "canceled"
                result.reason = "The operator cancelled; no further action was attempted."
                break
            if self.clock() - started > float(self.limits.deadline_seconds):
                result.status = "stopped"
                result.reason = (
                    f"The {self.limits.deadline_seconds:g} s deadline was reached; the loop "
                    "stopped instead of acting late."
                )
                break
            if index >= int(self.limits.max_actions):
                result.status = "stopped"
                result.reason = (
                    f"The action budget of {self.limits.max_actions} was spent; the loop stopped."
                )
                break

            observation = await self._observe(handle, pid, describe_prompt)
            result.observations.append(summarise(observation, limit=300))
            self.history.add(observation)
            frame_age = observation.frame.get("age_seconds") if observation.frame else None
            self._record_step(
                result,
                LoopStep(
                    index=index,
                    at=observation.observed_at,
                    observation=summarise(observation),
                    reason=observation.reason,
                    observation_age_seconds=(
                        round(float(frame_age), 3)
                        if isinstance(frame_age, (int, float)) and not isinstance(frame_age, bool)
                        else None
                    ),
                ),
            )
            annotated = {
                "kind": "observation",
                "index": index,
                "status": observation.status,
                "confidence": observation.confidence,
                "uncertainty": list(observation.uncertainty),
                "changes": list(observation.changes),
            }
            if self.annotate is not None:
                self.annotate(annotated)

            if not observation.actionable:
                result.status = (
                    "unavailable" if index == 0 and observation.status == "unavailable"
                    else "stopped"
                )
                result.reason = (
                    f"The observation is {observation.status}: {observation.reason} "
                    "No action is taken on an observation the loop cannot trust."
                )
                break

            verdict = await self._verify(session_id, reason=f"loop_step_{index}", result=result)
            outcome = str((verdict or {}).get("outcome") or "")
            if outcome == "verified_success":
                result.status = "completed"
                result.reason = str((verdict or {}).get("reason") or "The verifier decided.")
                break
            if (
                outcome == "verified_failure"
                and result.actions_executed > 0
                and bool(getattr(self.policy, "fail_on_unmet_verification", True))
            ):
                # Only a failure AFTER an action is a verdict about the loop's
                # attempt; before one it is the state that made the loop start.
                # A MULTI-STEP policy opts out: its objective is expected to be
                # unmet between stages, and this loop ends it only when the
                # policy has nothing left to do (see the ``proposal is None``
                # branch below, which reports the verifier's verdict).
                result.status = "failed"
                result.reason = str((verdict or {}).get("reason") or "The verifier decided.")
                break

            # The policy sees the LATEST observation; this is the state-dependent
            # step. A policy that returns None has nothing honest left to do.
            decision_started = self.clock()
            try:
                proposal = await asyncio.wait_for(
                    self.policy.propose(observation, history=tuple(self.history.items)),
                    timeout=float(self.limits.decision_timeout_seconds),
                )
            except (TimeoutError, asyncio.TimeoutError):
                # A cancel that arrived while the policy was deciding is still a
                # cancel; report the operator's stop rather than only its bound.
                if self.cancel.is_set():
                    result.status = "canceled"
                    result.reason = (
                        "The operator cancelled while the policy was deciding; the "
                        "decision's own bound ended the wait and no action was attempted."
                    )
                else:
                    result.status = "stopped"
                    result.reason = (
                        "The policy did not decide within "
                        f"{self.limits.decision_timeout_seconds:g} s; the loop stopped "
                        "instead of waiting or acting late."
                    )
                break
            except PolicyUnavailable as exc:
                result.status = "unavailable"
                result.reason = str(exc)
                break
            except Exception as exc:  # noqa: BLE001 - a policy fault stops the run, it does not escape
                result.status = "error"
                result.reason = (
                    f"The policy raised {type(exc).__name__}: {exc}. No action was "
                    "attempted, and the fault is reported rather than retried."
                )
                break
            if proposal is None:
                last = str(getattr(self.policy, "last_reason", "") or "")
                # A policy that stops while the verifier has JUST said the
                # objective is unmet has ended in failure, not in a neutral
                # stop. Saying "stopped" there would hide the verdict.
                if outcome == "verified_failure":
                    result.status = "failed"
                    result.reason = (
                        str((verdict or {}).get("reason") or "The verifier decided.")
                        + (f" The policy stopped: {last}" if last else "")
                    )
                    break
                result.status = "stopped"
                result.reason = last or "The policy proposed no further action."
                break
            problem = _proposal_problem(proposal)
            if problem:
                result.status = "error"
                result.reason = (
                    f"The policy returned a malformed proposal: {problem} No action was "
                    "attempted."
                )
                break
            result.decisions += 1
            if proposal.model_backed:
                result.model_backed = True
            step = LoopStep(
                index=index,
                at=utc_stamp(),
                observation=summarise(observation),
                proposal=proposal.to_dict(),
                expected_outcome=proposal.expected,
                decision_latency_ms=round((self.clock() - decision_started) * 1000, 3),
                evidence_count=len(proposal.evidence),
            )
            self._record_step(result, step)

            if self.cancel.is_set():
                result.status = "canceled"
                result.reason = "The operator cancelled before the proposal was planned."
                break

            try:
                planned = self.gateway.plan_action(session_id, dict(proposal.action))
            except Exception as exc:  # noqa: BLE001 - a planner fault is recorded, not propagated
                result.status = "error"
                result.reason = (
                    f"The session's planner raised {type(exc).__name__}: {exc}. No action "
                    "was attempted."
                )
                step.reason = result.reason
                break
            step.plan_status = str(planned.get("status") or "")
            if step.plan_status != "proposed":
                result.actions_refused += 1
                step.reason = str(
                    (planned.get("outcome") or {}).get("reason")
                    or "The session refused to plan this action."
                )
                result.status = "refused"
                result.reason = step.reason
                break
            proposal_id = str((planned.get("proposal") or {}).get("proposal_id") or "")
            digest = str((planned.get("proposal") or {}).get("digest") or "")
            remaining = planned.get("remaining_actions")
            step.remaining_actions = remaining if isinstance(remaining, int) else None

            try:
                executed = await self.gateway.execute_action(
                    session_id, {"proposal_id": proposal_id, "digest": digest}
                )
            except Exception as exc:  # noqa: BLE001 - an executor fault is recorded, not propagated
                result.status = "error"
                result.reason = (
                    f"The session's executor raised {type(exc).__name__}: {exc}. The "
                    "action's outcome is unknown, so the loop stopped."
                )
                step.reason = result.reason
                break
            step.execute_status = str(executed.get("status") or "")
            step.executed = step.execute_status == "executed"
            if not step.executed:
                result.actions_refused += 1
                step.reason = str(
                    (executed.get("outcome") or {}).get("reason")
                    or "The action did not execute."
                )
                result.status = "refused"
                result.reason = step.reason
                break
            result.actions_executed += 1
            index += 1

            # Re-observe BEFORE deciding the next action, and record whether the
            # action's own prediction held. The verdict still comes from the
            # verifier on the next iteration.
            follow_up = await self._observe(handle, pid, describe_prompt)
            self.history.add(follow_up)
            result.observations.append(summarise(follow_up, limit=300))
            step.outcome_met = _evaluate_expectation(proposal, follow_up)
            if self.annotate is not None:
                self.annotate(
                    {
                        "kind": "outcome",
                        "index": index - 1,
                        "executed": True,
                        "expectation_met": step.outcome_met,
                        "follow_up_status": follow_up.status,
                        "changes": list(follow_up.changes),
                    }
                )
            if not follow_up.actionable:
                result.status = "stopped"
                result.reason = (
                    f"After the action the observation is {follow_up.status}: "
                    f"{follow_up.reason} The loop stops rather than deciding on it."
                )
                break

        result.ended_at = utc_stamp()
        result.elapsed_seconds = round(self.clock() - started, 3)
        return result

    # -- internals ----------------------------------------------------------

    async def _observe(
        self, handle: int, pid: int | None, describe_prompt: str
    ) -> GameObservation:
        try:
            return await asyncio.wait_for(
                self.observations.observe(
                    handle,
                    pid=pid,
                    previous=self.history.latest,
                    describe_prompt=describe_prompt,
                ),
                timeout=float(self.limits.observation_timeout_seconds),
            )
        except (TimeoutError, asyncio.TimeoutError):
            from .observation import GameObservation as _Observation

            return _Observation(
                observed_at=utc_stamp(),
                status="unavailable",
                target={"handle": handle, "pid": pid},
                uncertainty=("The observation did not finish inside its bound.",),
                reason=(
                    f"Observation exceeded {self.limits.observation_timeout_seconds:g} s; "
                    "no partial read is used."
                ),
            )

    async def _verify(
        self, session_id: str, *, reason: str, result: LoopResult
    ) -> dict[str, Any] | None:
        try:
            verdict = await asyncio.wait_for(
                self.gateway.verify_objective(session_id, reason=reason),
                timeout=float(self.limits.verify_timeout_seconds),
            )
        except (TimeoutError, asyncio.TimeoutError):
            result.verification = {
                "outcome": "verification_error",
                "reason": (
                    f"The verifier did not answer within "
                    f"{self.limits.verify_timeout_seconds:g} s; the loop does not guess."
                ),
            }
            return result.verification
        except Exception as exc:  # noqa: BLE001 - a verifier fault is reported, not raised
            result.verification = {
                "outcome": "verification_error",
                "reason": f"The verifier raised {type(exc).__name__}: {exc}",
            }
            return result.verification
        if isinstance(verdict, Mapping):
            result.verification = dict(verdict)
            return result.verification
        result.verification = {
            "outcome": "unknown",
            "reason": "The verifier returned no readable verdict.",
        }
        return result.verification

    @staticmethod
    def _record_step(result: LoopResult, step: LoopStep) -> None:
        if result.steps and result.steps[-1].index == step.index:
            result.steps[-1] = step
        else:
            result.steps.append(step)


def default_max_frame_age() -> float:
    """The observation module's freshness bound, for callers that need it."""
    return DEFAULT_MAX_FRAME_AGE


def observations_from(result: LoopResult) -> Sequence[dict[str, Any]]:
    """The observation summaries a result carries, newest last."""
    return tuple(result.observations)
