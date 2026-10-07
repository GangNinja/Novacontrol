"""The rollout manager: one episode, from reset to termination, with the mask on.

This is the loop the whole phase is built around, and every safety property is
enforced INSIDE it rather than left to a caller:

    reset → observe → mask → policy → (explore) → authorize → execute → verify
          → reward → next state → repeat → terminate

The rules it enforces, in order:

  * **the mask runs first.** Candidates come from the environment; the mask
    removes what the state does not have, what the permission layer refuses and
    what needs a person who is not there. A policy chooses from the masked set.
  * **a decision is validated, not trusted.** An action that is not in the masked
    set is refused and recorded, never executed — including one a policy made up.
  * **nothing needing approval is executed in a run with no approval.** The
    transition says so and the episode is stopped as a SAFETY_STOP once the
    refusal budget is spent, rather than looping on a question nobody can answer.
  * **verification is in the loop.** An action that changes something is verified
    before it is rewarded; when a Phase 8 verification engine is wired, its
    verdict is combined with the environment's and the WORSE one wins, because a
    policy must never assume success where a check is available.
  * **recovery is bounded.** Failures are handed to the recovery layer inside the
    configured retry ceiling, and a destructive or external action, a refusal or a
    missing dependency is never retried — retrying cannot install a package.
  * **every loop has a hard bound.** Steps, planning horizon, retries, wall-clock
    timeout and resource budget can each end the episode, and the exact
    termination reason is recorded.

An episode ends with a :class:`~novacontrol.agentic.models.Episode` carrying the
transitions, the step and terminal rewards (dimensions intact), the verification
summary, the credit assignment and the resource usage — everything the learning
side and the reports need, and nothing that resembles a private thought.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import uuid4

from novacontrol.agentic.actions import (
    MASK_CONFIRMATION_REQUIRED,
    MASK_UNAUTHORIZED,
    MASK_UNSAFE,
    ActionMasker,
    action_to_plan_step,
)
from novacontrol.agentic.config import AgenticRLConfig
from novacontrol.agentic.environments import AgenticEnvironment
from novacontrol.agentic.exploration import STOP_REASONS, ExplorationPolicy
from novacontrol.agentic.models import (
    SAFETY_DIMENSION,
    ActionType,
    AgentAction,
    AgentState,
    CreditMethod,
    Episode,
    PolicyFeedback,
    PolicyReasonCode,
    StateTransition,
    StepPenalty,
    StepReward,
    TerminationReason,
    as_flag,
    as_mapping,
    as_real,
    as_text,
)
from novacontrol.agentic.policies import AgentPolicy
from novacontrol.agentic.rewards import AgenticRewardEngine, CreditAssigner, StepContext
from novacontrol.core.security import RiskLevel
from novacontrol.evaluation.models import now_iso
from novacontrol.planning.models import MAX_STEP_ATTEMPTS, PlanStep, RetryPolicy, StepError
from novacontrol.reliability.recovery import RecoveryKind, RecoveryPlan

#: How many refusals (an action the mask removed, or one needing approval) are
#: tolerated before the episode stops as a safety stop.
DEFAULT_REFUSAL_TOLERANCE = 2

#: The detail recorded when a termination's reason needs a sentence.
TERMINATION_DETAIL_RESOURCE = "the resource budget was exhausted"
TERMINATION_DETAIL_HORIZON = "the planning horizon was reached"
TERMINATION_DETAIL_CANCELLED = "a caller cancelled the episode"
TERMINATION_DETAIL_TIMEOUT = "the wall-clock timeout expired"
TERMINATION_DETAIL_APPROVAL = "every available action needed an approval nobody could give"
TERMINATION_DETAIL_MASK = "the policy could not select an action the mask allowed"


@dataclass(frozen=True, slots=True)
class RolloutLimits:
    """The explicit bounds every episode runs under."""

    max_steps: int = 16
    max_planning_horizon: int = 8
    max_retries: int = 2
    timeout_seconds: float = 60.0
    resource_budget: float = 1.0
    refusal_tolerance: int = DEFAULT_REFUSAL_TOLERANCE

    @classmethod
    def from_config(cls, config: AgenticRLConfig) -> RolloutLimits:
        return cls(
            max_steps=max(1, int(config.max_episode_steps)),
            max_planning_horizon=max(1, int(config.max_planning_horizon)),
            max_retries=max(0, min(MAX_STEP_ATTEMPTS - 1, int(config.max_retries))),
            timeout_seconds=max(0.001, float(config.timeout_seconds)),
            resource_budget=max(0.001, float(config.resource_budget)),
        )

    @property
    def retry_policy(self) -> RetryPolicy:
        """The Phase 8 retry policy these limits imply, within its own ceiling."""
        return RetryPolicy(max_attempts=max(1, min(MAX_STEP_ATTEMPTS, 1 + self.max_retries)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "max_planning_horizon": self.max_planning_horizon,
            "max_retries": self.max_retries,
            "timeout_seconds": self.timeout_seconds,
            "resource_budget": self.resource_budget,
            "refusal_tolerance": self.refusal_tolerance,
        }


@dataclass(frozen=True, slots=True)
class RolloutOutcome:
    """An episode plus the bookkeeping around running it."""

    episode: Episode
    explored_steps: int = 0
    refusals: int = 0
    exploration_stop_reason: str = ""
    termination_detail: str = ""
    updates: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode": self.episode.to_dict(),
            "explored_steps": self.explored_steps,
            "refusals": self.refusals,
            "exploration_stop_reason": self.exploration_stop_reason,
            "termination_detail": self.termination_detail,
            "updates": dict(self.updates),
        }


class AgenticRolloutManager:
    """Runs episodes: reset, mask, decide, execute, verify, reward, terminate."""

    def __init__(
        self,
        policy: AgentPolicy,
        *,
        reward_engine: AgenticRewardEngine | None = None,
        masker: ActionMasker | None = None,
        exploration: ExplorationPolicy | None = None,
        credit_assigner: CreditAssigner | None = None,
        limits: RolloutLimits | None = None,
        verification_engine: Any | None = None,
        recovery_engine: Any | None = None,
        verifier: Callable[[AgentAction, Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]
        | None = None,
        cancelled: Callable[[], bool] | None = None,
        clock: Callable[[], float] | None = None,
        gamma: float = 0.99,
        step_observer: Callable[[int, AgentState, Any, Any], None] | None = None,
        confirmation: Callable[[AgentAction, AgentState], bool] | None = None,
    ) -> None:
        self.policy = policy
        self.reward_engine = reward_engine if reward_engine is not None else AgenticRewardEngine()
        self.masker = masker if masker is not None else ActionMasker()
        self.exploration = exploration
        self.credit_assigner = (
            credit_assigner
            if credit_assigner is not None
            else CreditAssigner(CreditMethod.DISCOUNTED_RETURN.value, gamma=gamma)
        )
        self.limits = limits if limits is not None else RolloutLimits()
        self.verification_engine = verification_engine
        self.recovery_engine = recovery_engine
        self.verifier = verifier
        self.cancelled = cancelled
        self.clock = clock if clock is not None else time.monotonic
        self.gamma = max(0.0, min(1.0, float(gamma)))
        #: An optional observer called for every decision that is about to be
        #: executed: ``(index, state, mask, decision)``. This is what lets a
        #: SHADOW policy propose beside the one that acts, and it can never break
        #: a run — an observer that raises is recorded and ignored.
        self.step_observer = step_observer
        self.observer_errors: list[str] = []
        #: How a person is asked about an action the mask kept only because a
        #: person is reachable. There is no default on purpose: with no hook, an
        #: action that needs an approval cannot be approved, so it is refused.
        self.confirmation = confirmation
        self.confirmation_errors: list[str] = []

    @classmethod
    def from_config(
        cls,
        config: AgenticRLConfig,
        policy: AgentPolicy,
        **kwargs: Any,
    ) -> AgenticRolloutManager:
        """A manager wired from a configuration, with its exploration budget."""
        exploration = ExplorationPolicy(config.exploration, seed=config.seed)
        return cls(
            policy,
            reward_engine=AgenticRewardEngine(config.reward),
            exploration=exploration,
            credit_assigner=CreditAssigner(config.credit_method, gamma=config.gamma),
            limits=RolloutLimits.from_config(config),
            gamma=config.gamma,
            **kwargs,
        )

    def with_policy(self, policy: AgentPolicy) -> AgenticRolloutManager:
        """A copy of this manager that runs a different policy.

        Everything reusable is SHARED — the reward engine, the masker, the credit
        assigner, the limits and the wired engines — because an evaluation must
        measure two policies under identical rules. The exploration policy is
        re-created so two policies never share one exploration budget, which
        would otherwise let the first one exhaust the second one's allowance.
        """
        exploration = self.exploration
        if exploration is not None:
            exploration = ExplorationPolicy(exploration.config, seed=exploration.seed)
        return AgenticRolloutManager(
            policy,
            reward_engine=self.reward_engine,
            masker=self.masker,
            exploration=exploration,
            credit_assigner=self.credit_assigner,
            limits=self.limits,
            verification_engine=self.verification_engine,
            recovery_engine=self.recovery_engine,
            verifier=self.verifier,
            cancelled=self.cancelled,
            clock=self.clock,
            gamma=self.gamma,
            step_observer=self.step_observer,
        )

    # -- one episode ------------------------------------------------------------

    def run_episode(
        self,
        environment: AgenticEnvironment,
        *,
        task: Mapping[str, Any] | None = None,
        seed: int = 0,
        episode_id: str = "",
    ) -> RolloutOutcome:
        started_at = self.clock()
        observation = as_mapping(environment.reset(task=task, seed=seed))
        self.policy.reset()
        if self.exploration is not None:
            self.exploration.reset()
        goal = environment.goal()
        actions = list(environment.available_actions())
        state = AgentState(
            goal=goal,
            task_state={"status": as_text(observation.get("status")), "task": dict(task or {})},
            capabilities=tuple(sorted({item.capability or item.tool for item in actions if item.name})),
            tools=tuple(sorted({item.name for item in actions if item.name})),
            environment_state=observation,
            resource_state=as_mapping(environment.resource_state()),
            episode_id=episode_id,
        )
        initial_state = state

        transitions: list[StateTransition] = []
        executed_actions: list[str] = []
        signature_attempts: dict[str, int] = {}
        signature_failures: dict[str, int] = {}
        refusals = 0
        explored_steps = 0
        exploration_stop = ""
        cumulative = 0.0
        index = 0
        termination = ""
        termination_detail = ""
        total_failures = 0
        give_up_reason = ""

        while not environment.is_done():
            if index >= self.limits.max_steps:
                termination = TerminationReason.MAX_STEPS.value
                termination_detail = f"the step ceiling ({self.limits.max_steps}) was reached"
                break
            if index >= self.limits.max_planning_horizon and not environment.is_done():
                termination = TerminationReason.MAX_STEPS.value
                termination_detail = TERMINATION_DETAIL_HORIZON
                break
            if self.cancelled is not None and self.cancelled():
                termination = TerminationReason.CANCELLED.value
                termination_detail = TERMINATION_DETAIL_CANCELLED
                break
            if self.clock() - started_at > self.limits.timeout_seconds:
                termination = TerminationReason.TIMEOUT.value
                termination_detail = TERMINATION_DETAIL_TIMEOUT
                break
            resource = as_mapping(environment.resource_state())
            used = as_real(resource.get("used"), 0.0) or 0.0
            if used > self.limits.resource_budget:
                termination = TerminationReason.FAILURE.value
                termination_detail = TERMINATION_DETAIL_RESOURCE
                break
            step_started_at = self.clock()

            # 1. Candidates, then the mask. Nothing below may skip the mask.
            candidates = list(environment.available_actions())
            mask = self.masker.mask(state, candidates)
            decision = self.policy.select_action(state, mask.allowed, mask=mask)
            explored = False
            if self.exploration is not None and mask.allowed:
                exploration_decision = self.exploration.explore(
                    decision, mask.allowed, state=state
                )
                decision = exploration_decision.decision
                explored = exploration_decision.explored
                if exploration_decision.reason in STOP_REASONS:
                    exploration_stop = exploration_stop or exploration_decision.reason
            action = decision.selected_action

            # 2. A decision is validated, never trusted.
            refusal, refusal_kind = self._refusal_reason(action, mask, state)
            if refusal:
                refused_action = action if action is not None else AgentAction(
                    action_type="noop", label="no selectable action"
                )
                reward = StepReward(
                    index=index,
                    total=0.0,
                    dimensions=(
                        {
                            SAFETY_DIMENSION: round(
                                -self.reward_engine.weights.safety
                                * self.reward_engine.weights.unsafe_penalty,
                                6,
                            )
                        }
                        if refusal_kind == "safety"
                        else {}
                    ),
                    penalties=(StepPenalty.UNSAFE_ACTION.value,)
                    if refusal_kind == "safety"
                    else (),
                    reasons=(refusal,),
                    safety=(
                        round(
                            -self.reward_engine.weights.safety
                            * self.reward_engine.weights.unsafe_penalty,
                            6,
                        )
                        if refusal_kind == "safety"
                        else 0.0
                    ),
                    source=self.reward_engine.source,
                )
                refusals += 1
                # A safety refusal IS a safety event: the policy proposed
                # something it may not do, so exploration is told.
                if refusal_kind == "safety" and self.exploration is not None:
                    stop = self.exploration.record_outcome(
                        explored=False, failed=True, reward=0.0, unsafe=True
                    )
                    exploration_stop = exploration_stop or stop
                next_state = state.advanced(
                    observation={"refused": True, "reason": refusal},
                    verification={"status": "skipped", "reason": "the action was never executed"},
                    error=f"failed_action:{refused_action.signature()}" if action is not None else "",
                )
                transitions.append(
                    StateTransition(
                        index=index,
                        previous_state=state,
                        action=refused_action,
                        observation={"refused": True, "reason": refusal},
                        result={"refused": True, "reason": refusal},
                        next_state=next_state,
                        verification={"status": "skipped", "reason": refusal},
                        reward=reward,
                        step_reward=reward.total,
                        cumulative_reward=cumulative,
                        policy_metadata=self._policy_metadata(decision, explored),
                        error=refusal,
                        executed=False,
                        terminal=False,
                    )
                )
                state = next_state
                index += 1
                if refusals > self.limits.refusal_tolerance:
                    # A refusal the SAFETY rules produced ends the episode as a
                    # safety stop; a policy that simply cannot name an allowed
                    # action ends it as a failure, because that is what it is.
                    termination = (
                        TerminationReason.SAFETY_STOP.value
                        if refusal_kind == "safety"
                        else TerminationReason.FAILURE.value
                    )
                    termination_detail = (
                        TERMINATION_DETAIL_APPROVAL
                        if refusal_kind == "safety"
                        else TERMINATION_DETAIL_MASK
                    )
                    break
                continue

            assert action is not None  # guaranteed by the refusal check above
            self._notify(index, state, mask, decision)
            # 3. Execute the authorized action.
            observation = as_mapping(environment.step(action))
            step_record = environment.steps[-1] if environment.steps else None
            result = as_mapping(step_record.result) if step_record is not None else {}
            error = as_text(step_record.error) if step_record is not None else ""
            signature = action.signature()
            signature_attempts[signature] = signature_attempts.get(signature, 0) + 1
            failures_before = total_failures
            if error:
                signature_failures[signature] = signature_failures.get(signature, 0) + 1
                total_failures += 1
            repeated = signature_attempts[signature] - 1
            executed_actions.append(signature)

            # 4. Verification, in the loop. A step that changes something is
            #    checked before it is rewarded.
            verification = self._verify(environment, action, observation, result)
            verification = dict(verification)
            verification.setdefault("action_signature", signature)
            verified = as_text(verification.get("status")) == "pass"
            progressed = bool(verified and not error)

            # 5. Recovery, bounded, only where a failure actually happened.
            recovery_event = self._recovery(action, error, signature_attempts.get(signature, 1))
            if recovery_event.get("strategy") == "stop":
                give_up_reason = as_text(recovery_event.get("diagnosis"), error)

            # 6. Reward for the step.
            context = StepContext(
                index=index,
                action=action,
                observation=observation,
                result=result,
                verification=verification,
                error=error,
                progressed=progressed,
                repeated=repeated > 0,
                repeat_count=repeated,
                latency_ms=round(max(0.0, (self.clock() - step_started_at) * 1000.0), 6),
                resource_used=as_real(as_mapping(environment.resource_state()).get("used")),
                expected_tool=(
                    as_text(step_record.expected_action) if step_record is not None else ""
                )
                or environment.expected_action(),
                recovery=recovery_event,
                done=bool(observation.get("done")),
                success=as_flag(observation.get("success")),
                termination_reason=as_text(environment.termination_reason),
                unsafe=bool(as_mapping(observation).get("unsafe")),
                unnecessary=decision.reason_code == PolicyReasonCode.RECOVERY.value
                and not error
                and failures_before == 0,
            )
            reward = self.reward_engine.step_reward(context)
            cumulative = round(math.fsum([cumulative, reward.total]), 6)

            # 7. Next state.
            next_state = state.advanced(
                observation=observation,
                action=signature,
                verification=verification,
                error=error,
                environment_state=observation,
                task_state={
                    "status": as_text(observation.get("status")),
                    "task": dict(task or {}),
                },
                resource_state=as_mapping(environment.resource_state()),
            )
            terminal = bool(observation.get("done")) or environment.is_done()
            transitions.append(
                StateTransition(
                    index=index,
                    previous_state=state,
                    action=action,
                    observation=observation,
                    result=result,
                    next_state=next_state,
                    verification=verification,
                    reward=reward,
                    step_reward=reward.total,
                    cumulative_reward=cumulative,
                    recovery_event=recovery_event,
                    policy_metadata=self._policy_metadata(decision, explored),
                    error=error,
                    executed=True,
                    terminal=terminal,
                    termination_reason=environment.termination_reason if terminal else "",
                )
            )
            state = next_state
            index += 1
            if explored:
                explored_steps += 1
            if self.exploration is not None:
                stop = self.exploration.record_outcome(
                    explored=explored,
                    failed=bool(error),
                    reward=reward.total,
                    unsafe=bool(as_mapping(observation).get("unsafe")),
                )
                exploration_stop = exploration_stop or stop
            if give_up_reason:
                termination = TerminationReason.FAILURE.value
                termination_detail = f"recovery stopped: {give_up_reason}"
                break

        if not termination:
            if environment.is_done():
                termination = (
                    environment.termination_reason or TerminationReason.FAILURE.value
                )
            else:
                termination = TerminationReason.MAX_STEPS.value
                termination_detail = f"the step ceiling ({self.limits.max_steps}) was reached"
        success = self._success_for(termination, environment)

        episode = Episode(
            episode_id=episode_id or uuid4().hex,
            task_id=as_text((task or {}).get("task_id")),
            environment_id=environment.environment_id,
            end_time=now_iso(),
            initial_state=initial_state,
            goal=goal,
            steps=tuple(transitions),
            total_reward=round(math.fsum(step.step_reward for step in transitions), 6),
            success=success,
            termination_reason=termination,
            verification_summary=self._verification_summary(transitions, verification=environment),
            resource_usage={
                **as_mapping(environment.resource_state()),
                "steps": len(transitions),
                "explored_steps": explored_steps,
                "refusals": refusals,
                "elapsed_seconds": round(max(0.0, self.clock() - started_at), 6),
            },
            policy_version=self.policy.version,
            model_id=self.policy.model_id,
            difficulty=environment.difficulty(),
            metadata={
                "seed": int(seed),
                "reward_source": self.reward_engine.source,
                "limits": self.limits.to_dict(),
                "termination_detail": termination_detail,
                "refusals": refusals,
                "explored_steps": explored_steps,
                "exploration_stop_reason": exploration_stop,
                "executed_actions": list(executed_actions),
                "policy_id": self.policy.policy_id,
                "policy_learns": self.policy.learns,
                "simulated": self.policy.simulated,
            },
        )

        # 8. Reward summary, credit assignment and the policy's update.
        summary = self.reward_engine.episode_reward(episode)
        assignment = self.credit_assigner.assign(episode)
        episode = episode.with_reward(
            float(summary["total_reward"]),
            {
                **dict(summary),
                "dimension_totals": dict(summary["dimension_totals"]),
                "credit_method": assignment.method,
            },
        )
        episode = replace(
            episode,
            dimension_totals=dict(summary["dimension_totals"]),
            credits=dict(assignment.credits),
            returns=dict(assignment.returns),
        )
        updates = self.policy.update(PolicyFeedback.for_episode(episode))
        return RolloutOutcome(
            episode=episode,
            explored_steps=explored_steps,
            refusals=refusals,
            exploration_stop_reason=exploration_stop,
            termination_detail=termination_detail,
            updates=as_mapping(updates),
        )

    # -- batches ----------------------------------------------------------------

    def run_episodes(
        self,
        environments: Sequence[AgenticEnvironment],
        *,
        task: Mapping[str, Any] | None = None,
        seed: int = 0,
    ) -> tuple[RolloutOutcome, ...]:
        """One episode per environment, with a seed offset per episode."""
        outcomes: list[RolloutOutcome] = []
        for offset, environment in enumerate(environments):
            outcomes.append(self.run_episode(environment, task=task, seed=seed + offset))
        return tuple(outcomes)

    # -- the pieces -------------------------------------------------------------

    def _notify(self, index: int, state: AgentState, mask: Any, decision: Any) -> None:
        """Tell the observer about a decision that is about to be executed."""
        if self.step_observer is None:
            return
        try:
            self.step_observer(index, state, mask, decision)
        except Exception as error:  # pragma: no cover - a broken observer
            self.observer_errors.append(f"{type(error).__name__}: {error}")

    def _refusal_reason(
        self, action: AgentAction | None, mask: Any, state: AgentState
    ) -> tuple[str, str]:
        """Why this decision may not run (empty means it may), and of what kind.

        The kind is ``safety`` when a safety rule produced the refusal (an
        unauthorized action, a masked one that needed approval, a masked unsafe
        one) and ``mask`` when the action was simply not available here. The two
        end an episode differently: one is a safety stop, the other is a failure.
        """
        if action is None:
            return TERMINATION_DETAIL_MASK, "mask"
        allowed = set(mask.ids())
        if action.action_id not in allowed:
            blocked_reason = as_text(mask.blocked_reason(action.action_id))
            reason = (
                f"the action {action.name!r} was not in the masked action set"
                + (f" ({blocked_reason})" if blocked_reason else "")
            )
            unsafe = blocked_reason in {
                MASK_UNSAFE,
                MASK_UNAUTHORIZED,
                MASK_CONFIRMATION_REQUIRED,
            }
            if not blocked_reason and self._declares_itself_unsafe(action):
                # The mask never saw this action, because the policy invented it.
                # Naming something that is not on offer is a failure — UNLESS the
                # action declares that it needs an approval, cannot be undone or
                # is rated HIGH or worse. Refusing one of those is a safety
                # matter, and the episode should stop saying so.
                unsafe = True
            return reason, ("safety" if unsafe else "mask")
        # An action the mask kept ONLY because a person is reachable still needs
        # that person. The mask is what knows which ones those are, and what the
        # action declares itself counts too.
        needs_approval = bool(
            mask.needs_confirmation(action.action_id)
            or (action.requires_confirmation and not self.masker.is_approved(action))
        )
        if needs_approval:
            # Ask, and treat anything but a yes as a refusal: an approval that
            # never happened is not an approval.
            if not self.masker.allow_confirmation:
                return (
                    f"the action {action.name!r} needs an approval nobody can give in this run",
                    "safety",
                )
            if not self._confirm(action, state):
                return (
                    f"the action {action.name!r} needs an approval and it was not given",
                    "safety",
                )
        return "", ""

    @staticmethod
    def _declares_itself_unsafe(action: AgentAction) -> bool:
        """Whether the ACTION says a person's approval is required for it."""
        return bool(
            action.requires_confirmation
            or not action.reversible
            or action.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
        )

    def _confirm(self, action: AgentAction, state: AgentState) -> bool:
        """Ask the caller's confirmation hook. No hook, or a hook that breaks,
        means no approval — never a silent yes."""
        if self.confirmation is None:
            return False
        try:
            return bool(self.confirmation(action, state))
        except Exception as error:  # pragma: no cover - a broken approval hook
            self.confirmation_errors.append(f"{type(error).__name__}: {error}")
            return False

    def _verify(
        self,
        environment: AgenticEnvironment,
        action: AgentAction,
        observation: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """The environment's verdict, combined with any verifier that is wired."""
        if not environment.requires_verification(action):
            return {
                "status": "skipped",
                "reason": "the action is read-only, so there is nothing to verify",
                "expectation": action.expected_effect,
            }
        verdicts: list[Mapping[str, Any]] = [as_mapping(environment.verify(action, observation, result))]
        if self.verification_engine is not None:
            # Phase 8's engine is an ADDITIONAL check. It is wired explicitly,
            # because its derived default check is only meaningful for tools a
            # caller has registered a strategy for; an engine with no strategy
            # reports ``skipped``, which never overturns the environment's own
            # observation.
            engine_verdict = self._engine_verification(action, result)
            if engine_verdict:
                verdicts.append(engine_verdict)
        if self.verifier is not None:
            hook_verdict = as_mapping(self.verifier(action, observation, result))
            if hook_verdict:
                verdicts.append(hook_verdict)
        return self._combine(verdicts)

    def _engine_verification(
        self, action: AgentAction, result: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Phase 8's VerificationEngine, asked about this one action."""
        engine = self.verification_engine
        if engine is None:  # pragma: no cover - the caller checked
            return {}
        step: PlanStep = action_to_plan_step(action)
        try:
            outcome = engine.verify(step, dict(result))
        except Exception as error:  # pragma: no cover - a broken verifier
            return {
                "status": "inconclusive",
                "reason": f"the verification engine raised {type(error).__name__}",
                "expectation": action.expected_effect,
            }
        if hasattr(outcome, "to_dict"):
            return as_mapping(outcome.to_dict())
        return as_mapping(outcome)

    @staticmethod
    def _combine(verdicts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """The most cautious MEANINGFUL verdict wins.

        ``pass`` and ``fail`` are opinions; ``skipped`` and ``inconclusive`` are
        the absence of one, so they never overturn a check that ran: an
        environment that observed the expected state is not un-verified because a
        second engine had no strategy for the tool. What cannot happen is a
        ``pass`` surviving a ``fail`` beside it, which is the property that makes
        verification-in-the-loop worth anything.
        """
        order = {"fail": 0, "pass": 1, "inconclusive": 2, "skipped": 3}
        if not verdicts:
            return {"status": "skipped", "reason": "no verification was performed"}
        ranked = sorted(
            enumerate(verdicts),
            key=lambda pair: order.get(as_text(pair[1].get("status")), 2),
        )
        winner = ranked[0][1]
        worst = dict(winner)
        statuses = [as_text(item.get("status"), "inconclusive") for item in verdicts]
        worst["status"] = as_text(worst.get("status"), "inconclusive")
        worst["verdicts"] = statuses
        # The verdicts that did not win are kept with their reasons, so a check
        # that RAN and crashed (or had no strategy) never disappears behind the
        # one that did: absence of evidence is recorded, not overwritten.
        others = [
            {"status": statuses[index], "reason": as_text(verdicts[index].get("reason"))}
            for index, _in_rank in ranked[1:]
        ]
        if others:
            worst["other_verdicts"] = others
        if "pass" in statuses and "fail" in statuses:
            worst["reason"] = as_text(worst.get("reason")) or "two checks disagreed"
            worst["disagreement"] = True
        return worst

    def _recovery(self, action: AgentAction, error: str, attempts: int) -> dict[str, Any]:
        """What recovery says about a failed action (empty when nothing failed)."""
        if not error:
            return {}
        if self.recovery_engine is not None:
            return self._engine_recovery(action, error, attempts)
        return self._builtin_recovery(action, error, attempts)

    def _engine_recovery(
        self, action: AgentAction, error: str, attempts: int
    ) -> dict[str, Any]:
        """Phase 8's recovery engine, asked about one failed action."""
        engine = self.recovery_engine
        if engine is None:  # pragma: no cover - the caller checked
            return {}
        step = action_to_plan_step(action)
        try:
            classified: StepError = engine.classify(step, error)
            plan: RecoveryPlan = engine.plan_recovery(
                step,
                classified,
                attempts_made=max(1, int(attempts)),
                retry_policy=self.limits.retry_policy,
            )
        except Exception as failure:  # pragma: no cover - a broken recovery engine
            return {
                "strategy": "stop",
                "diagnosis": f"the recovery engine raised {type(failure).__name__}",
                "attempts": attempts,
                "succeeded": None,
                "unnecessary": False,
                "safety": True,
            }
        strategy = {
            RecoveryKind.RETRY: "retry",
            RecoveryKind.ALTERNATIVE: "alternative",
            RecoveryKind.ESCALATE: "escalate",
            RecoveryKind.ASK_USER: "ask_user",
            RecoveryKind.STOP: "stop",
        }.get(plan.kind, "stop")
        return {
            "strategy": strategy,
            "diagnosis": as_text(plan.diagnosis) or as_text(plan.reason),
            "attempts": int(attempts),
            "attempts_left": int(plan.attempts_left),
            "retry_safe": bool(plan.retry_safe),
            "stopped_safely": bool(plan.stopped_safely),
            "succeeded": None,
            "unnecessary": False,
            "source": "reliability.recovery",
            "failure_kind": as_text(getattr(classified, "kind", "")),
        }

    def _builtin_recovery(
        self, action: AgentAction, error: str, attempts: int
    ) -> dict[str, Any]:
        return builtin_recovery_decision(
            action, error, attempts, limits=self.limits
        )

    def _policy_metadata(self, decision: Any, explored: bool) -> dict[str, Any]:
        """The structured decision record: no reasoning, only the choice and why."""
        action = getattr(decision, "selected_action", None)
        return {
            "policy_id": self.policy.policy_id,
            "policy_version": getattr(decision, "policy_version", "") or self.policy.version,
            "model_id": getattr(decision, "model_id", "") or self.policy.model_id,
            "reason_code": as_text(getattr(decision, "reason_code", "")),
            "confidence": round(float(getattr(decision, "confidence", 0.0) or 0.0), 6),
            "expected_value": getattr(decision, "expected_value", None),
            "explored": bool(explored),
            "selected": action.as_mapping() if isinstance(action, AgentAction) else None,
            "alternatives": [
                item.name for item in getattr(decision, "alternatives", ()) if isinstance(item, AgentAction)
            ],
            "masked": [dict(item) for item in getattr(decision, "masked", ())],
            "learns": self.policy.learns,
        }

    def _success_for(
        self, termination: str, environment: AgenticEnvironment
    ) -> bool | None:
        """The episode's success: decided only where something decided it."""
        if termination == TerminationReason.SUCCESS.value:
            return True if environment.success is None else bool(environment.success)
        if termination in {
            TerminationReason.FAILURE.value,
            TerminationReason.ENVIRONMENT_ERROR.value,
        }:
            return False
        # A stopped run is neither a success nor a failure: nobody decided the
        # task was impossible, it was bounded.
        return None

    @staticmethod
    def _verification_summary(
        transitions: Sequence[StateTransition],
        *,
        verification: AgenticEnvironment | None = None,
    ) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for step in transitions:
            status = as_text(step.verification.get("status"), "skipped")
            counts[status] = counts.get(status, 0) + 1
        executed = [step for step in transitions if step.executed]
        return {
            "checked": sum(1 for step in executed if as_text(step.verification.get("status")) != "skipped"),
            "passed": counts.get("pass", 0),
            "failed": counts.get("fail", 0),
            "inconclusive": counts.get("inconclusive", 0),
            "skipped": counts.get("skipped", 0),
            "executed": len(executed),
            "final": (
                as_text(verification.termination_reason)
                if verification is not None
                else ""
            ),
        }


def builtin_recovery_decision(
    action: AgentAction, error: str, attempts: int, *, limits: RolloutLimits
) -> dict[str, Any]:
    """What recovery decides when no recovery engine is wired.

    Deliberately a strict SUBSET of Phase 8's rules rather than a second
    implementation of them: a destructive or irreversible action and an external
    one are never retried, a missing dependency is never retried, and the attempt
    ceiling is the configured one. A permission refusal never reaches this
    function — it is refused by the mask before anything runs.
    """
    lowered = error.lower()
    destructive = (
        action.requires_confirmation
        or not action.reversible
        or action.action_type == ActionType.COMMUNICATION.value
    )
    dependency_markers = (
        "not installed",
        "no module named",
        "executable not found",
        "not available",
        "not found on path",
    )
    if destructive:
        return {
            "strategy": "stop",
            "diagnosis": (
                "a destructive or irreversible action is never retried "
                "automatically: a second one is a new risk, not a second chance"
            ),
            "attempts": attempts,
            "succeeded": None,
            "unnecessary": False,
            "safety": True,
            "source": "builtin",
        }
    if any(marker in lowered for marker in dependency_markers):
        return {
            "strategy": "stop",
            "diagnosis": (
                "a required dependency is missing and retrying cannot install it"
            ),
            "attempts": attempts,
            "succeeded": None,
            "unnecessary": False,
            "safety": False,
            "source": "builtin",
        }
    if attempts > limits.max_retries:
        return {
            "strategy": "stop",
            "diagnosis": (
                f"the action failed {attempts} time(s) and the retry budget "
                f"({limits.max_retries}) is spent"
            ),
            "attempts": attempts,
            "attempts_left": 0,
            "retry_safe": False,
            "succeeded": None,
            "unnecessary": False,
            "source": "builtin",
        }
    return {
        "strategy": "retry",
        "diagnosis": "the failure looks transient, so it is retried inside the budget",
        "attempts": attempts,
        "attempts_left": max(0, limits.max_retries - attempts),
        "retry_safe": True,
        "succeeded": None,
        "unnecessary": False,
        "source": "builtin",
    }


def run_episode(
    environment: AgenticEnvironment,
    policy: AgentPolicy,
    *,
    config: AgenticRLConfig | None = None,
    task: Mapping[str, Any] | None = None,
    seed: int = 0,
    **kwargs: Any,
) -> RolloutOutcome:
    """Run one episode with a manager built from a configuration."""
    settings = config if config is not None else AgenticRLConfig()
    manager = (
        AgenticRolloutManager.from_config(settings, policy, **kwargs)
        if config is not None
        else AgenticRolloutManager(policy, **kwargs)
    )
    return manager.run_episode(environment, task=task, seed=seed)


__all__ = [
    "DEFAULT_REFUSAL_TOLERANCE",
    "TERMINATION_DETAIL_APPROVAL",
    "TERMINATION_DETAIL_CANCELLED",
    "TERMINATION_DETAIL_HORIZON",
    "TERMINATION_DETAIL_MASK",
    "TERMINATION_DETAIL_RESOURCE",
    "TERMINATION_DETAIL_TIMEOUT",
    "AgenticRolloutManager",
    "RolloutLimits",
    "RolloutOutcome",
    "builtin_recovery_decision",
    "run_episode",
]
