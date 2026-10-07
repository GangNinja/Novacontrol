"""Policies: what chooses an action, and what it is allowed to say.

:class:`AgentPolicy` is the interface this phase adds to Phase 18's simple
:class:`~novacontrol.rlhf.rollout.Policy`. Phase 18's policy sees an observation
and returns one action mapping; an agentic policy OBESERVES a structured state,
SELECTS among the actions the mask left standing, and can UPDATE itself from the
outcome — which is what makes a learned policy possible later without changing
the rollout layer that calls it.

Three implementations ship, and the honest labels matter:

  * :class:`RuleBasedPolicy` — deterministic, explainable, no model. It is what
    normal NovaControl keeps using when no learned policy is enabled, and it
    selects by structured rules (the goal names a tool, the last action failed,
    an action is a no-op) with a structured REASON CODE rather than a narrative.
  * :class:`MockPolicy` — deterministic script replay for dry runs and tests.
    ``learns`` is False and every record says so.
  * :class:`LLMPolicyAdapter` — an ADAPTER over a provider that already exists.
    It loads no model, downloads nothing and invents no action: the model
    proposes a structured choice and the adapter validates it against the action
    space. A proposal that names an action that is not in the space is REFUSED,
    not guessed at.

None of them can bypass the action mask: :meth:`AgentPolicy.select_action`
receives the mask's allowed set and a decision that names a masked action is
rejected by the rollout manager before anything runs.
"""

from __future__ import annotations

import abc
import random
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from novacontrol.agentic.models import (
    ActionType,
    AgentAction,
    AgentState,
    PolicyDecision,
    PolicyFeedback,
    PolicyReasonCode,
    as_mapping,
    as_real,
    as_text,
)

#: The actions a policy may say it is unable to choose from.
NO_ACTION = "policy_not_available"


class PolicyUnavailable(RuntimeError):
    """A policy that cannot act says so rather than guessing."""


class AgentPolicy(abc.ABC):
    """Observe a state, select among the allowed actions, learn from the outcome."""

    policy_id: str = "abstract"
    version: str = "phase20.1"
    model_id: str = ""
    #: Whether this policy changes its behaviour from feedback. The rule-based
    #: and mock policies do not, and they say so.
    learns: bool = False
    #: Whether the policy is a stand-in rather than a policy.
    simulated: bool = True

    @property
    def name(self) -> str:
        return self.policy_id

    def reset(self) -> None:
        """Called before an episode; a stateful policy clears itself here."""
        return None

    def observe(self, state: AgentState) -> None:
        """Record what the policy was shown. Never required, never a prompt log."""
        return None

    @abc.abstractmethod
    def select_action(
        self,
        state: AgentState,
        actions: Sequence[AgentAction],
        *,
        mask: Any | None = None,
    ) -> PolicyDecision:
        """Choose one action from ``actions`` (already the masked set)."""

    def update(self, feedback: PolicyFeedback) -> dict[str, Any]:
        """Learn from an episode's outcome. The default records nothing."""
        del feedback
        return {
            "policy_id": self.policy_id,
            "learning": self.learns,
            "updated": False,
            "note": (
                "this policy does not change its behaviour from feedback"
                if not self.learns
                else "no update rule is implemented for this policy"
            ),
        }

    def describe(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "model_id": self.model_id,
            "learns": self.learns,
            "simulated": self.simulated,
            "loads_model": False,
            "requires_cuda": False,
        }


def _decision_for(
    actions: Sequence[AgentAction],
    *,
    reason_code: str,
    confidence: float,
    notes: Sequence[str] = (),
    mask: Any | None = None,
) -> PolicyDecision:
    """Build a decision, or the honest refusal when nothing was selectable."""
    masked = tuple(getattr(mask, "blocked", ()))
    if not actions:
        return PolicyDecision(
            selected_action=None,
            confidence=0.0,
            reason_code=reason_code,
            notes=(*notes, "no action survived the mask, so the policy selected none"),
            masked=tuple(item.to_dict() for item in masked),
        )
    return PolicyDecision(
        selected_action=actions[0],
        confidence=confidence,
        alternatives=tuple(actions[1:]),
        reason_code=reason_code,
        notes=tuple(notes),
        masked=tuple(item.to_dict() for item in masked),
    )


class RuleBasedPolicy(AgentPolicy):
    """Deterministic, explainable selection with no model anywhere.

    The rules, in the order they are tried — and every one of them produces a
    structured reason code, never a narrative:

      1. an action the goal TEXT names is required (``REQUIRED_TOOL``);
      2. an action that repeats the action which just failed is avoided, and a
         different one is preferred (``RECOVERY``);
      3. an action that gathers information while the state has an active error is
         preferred over repeating work (``USER_GOAL``);
      4. otherwise the first non-no-op action the mask allowed
         (``PLANNER_DECISION``).

    It is deliberately shallow. Its job is to be the deterministic baseline every
    candidate policy is compared against, not to be clever.
    """

    policy_id = "rule_based"
    learns = False
    simulated = False

    def __init__(self, *, policy_id: str = "", version: str = "phase20.1") -> None:
        if policy_id:
            self.policy_id = policy_id
        self.version = version

    def select_action(
        self,
        state: AgentState,
        actions: Sequence[AgentAction],
        *,
        mask: Any | None = None,
    ) -> PolicyDecision:
        if not actions:
            return _decision_for(
                (),
                reason_code=PolicyReasonCode.SAFETY_CONSTRAINT.value,
                confidence=0.0,
                mask=mask,
            )
        succeeded = _succeeded_signatures(state)

        # 1. A clue in the CONTEXT: an action the observation's context names.
        clue_words = _context_words(state)
        if clue_words:
            from_clue = _ranked_by_words(actions, clue_words)
            if from_clue:
                return _decision_for(
                    _ordered(from_clue, actions),
                    reason_code=PolicyReasonCode.USER_GOAL.value,
                    confidence=0.75,
                    notes=(f"the context names {from_clue[0].name!r}",),
                    mask=mask,
                )

        # 2. A tool the GOAL names, best match first. Between two actions the goal
        #    names equally, the one that has NOT already worked is preferred —
        #    which is how a dependent sequence comes out in order.
        goal_words = set(_words(state.goal))
        if goal_words:
            named = _ranked_by_words(
                actions, goal_words, prefer_not_in=succeeded
            )
            if named:
                return _decision_for(
                    _ordered(named, actions),
                    reason_code=PolicyReasonCode.REQUIRED_TOOL.value,
                    confidence=0.9,
                    notes=(f"the goal names {named[0].name!r}",),
                    mask=mask,
                )

        # 3. Recovery: do not repeat the action that just failed when there is
        #    another useful one. The retry itself is bounded by the rollout's
        #    recovery layer, never by this rule.
        failed = _last_failed_signature(state)
        if failed:
            alternatives = [
                item
                for item in actions
                if item.signature() != failed or item.signature() in succeeded
            ]
            preferred = [item for item in alternatives if not item.is_noop]
            if preferred:
                return _decision_for(
                    _ordered(preferred, actions),
                    reason_code=PolicyReasonCode.RECOVERY.value,
                    confidence=0.6,
                    notes=(f"the previous action ({failed}) failed, so another is tried",),
                    mask=mask,
                )

        # 4. With an error active, gather information before repeating work.
        if state.active_errors:
            informative = [
                item
                for item in actions
                if item.action_type == ActionType.INFORMATION.value
                and item.signature() not in succeeded
            ]
            if informative:
                return _decision_for(
                    _ordered(informative, actions),
                    reason_code=PolicyReasonCode.USER_GOAL.value,
                    confidence=0.55,
                    notes=("an error is active, so information is gathered first",),
                    mask=mask,
                )

        # 5. Whatever the mask allowed, preferring something that does work.
        #    Doing the same useful thing again is allowed here: a repeated
        #    ``advance`` is how a long task is finished, and a policy that cannot
        #    repeat itself cannot finish one.
        useful = [item for item in actions if not item.is_noop] or list(actions)
        return _decision_for(
            _ordered(useful, actions),
            reason_code=PolicyReasonCode.PLANNER_DECISION.value,
            confidence=0.5,
            notes=("the first useful action the mask allowed",),
            mask=mask,
        )


class MockPolicy(AgentPolicy):
    """Replays a script, deterministically — for dry runs and tests."""

    policy_id = "mock"
    learns = False
    simulated = True

    def __init__(
        self,
        script: Sequence[str] = (),
        *,
        policy_id: str = "",
        model_id: str = "",
    ) -> None:
        self.script = tuple(as_text(item) for item in script if as_text(item))
        if policy_id:
            self.policy_id = policy_id
        self.model_id = model_id
        self.index = 0

    def reset(self) -> None:
        self.index = 0

    def select_action(
        self,
        state: AgentState,
        actions: Sequence[AgentAction],
        *,
        mask: Any | None = None,
    ) -> PolicyDecision:
        del state
        if self.index < len(self.script):
            wanted = self.script[self.index]
            self.index += 1
            for item in actions:
                if item.name == wanted:
                    return _decision_for(
                        _ordered([item], actions),
                        reason_code=PolicyReasonCode.PLANNER_DECISION.value,
                        confidence=0.8,
                        notes=(f"the script says {wanted!r}",),
                        mask=mask,
                    )
            return PolicyDecision(
                selected_action=None,
                confidence=0.0,
                reason_code=PolicyReasonCode.SAFETY_CONSTRAINT.value,
                notes=(
                    f"the script asked for {wanted!r}, which is not in the masked action set",
                ),
                masked=tuple(item.to_dict() for item in getattr(mask, "blocked", ())),
            )
        self.index += 1
        useful = [item for item in actions if not item.is_noop]
        return _decision_for(
            _ordered(useful or list(actions), actions),
            reason_code=PolicyReasonCode.PLANNER_DECISION.value,
            confidence=0.4,
            notes=("the script is exhausted, so the first useful action is taken",),
            mask=mask,
        )


#: What an LLM proposer may return. Deliberately tiny: an action NAME, a
#: confidence and a reason code. There is no field for reasoning text, and keys
#: that look like one are ignored rather than stored.
LLMProposal = Callable[
    [AgentState, Sequence[AgentAction]],
    Mapping[str, Any],
]

#: Keys an adapter accepts from a proposer. Anything else — including anything
#: that would be a chain of thought — is dropped.
PROPOSAL_KEYS: tuple[str, ...] = (
    "action",
    "tool",
    "action_id",
    "confidence",
    "reason_code",
    "expected_value",
)


class LLMPolicyAdapter(AgentPolicy):
    """Adapts an EXISTING provider into a policy, without loading anything.

    The adapter takes a ``proposer`` callable — the thing that actually talks to a
    model, wired by whoever has one. The adapter itself never loads a model, never
    downloads one, and never runs one at import time: with no proposer it reports
    itself unavailable and refuses to act.

    A proposal is validated, not trusted: an action name that is not in the masked
    set is REFUSED (the decision carries no action and says why), which is what
    keeps a hallucinated tool call from reaching an executor.
    """

    policy_id = "llm_adapter"
    #: A model can change its behaviour from feedback; THIS adapter does not
    #: implement an update rule, so it says False rather than pretending.
    learns = False
    simulated = True

    def __init__(
        self,
        proposer: LLMProposal | None = None,
        *,
        policy_id: str = "",
        model_id: str = "",
        version: str = "phase20.1",
        allow_confirmation: bool = False,
    ) -> None:
        self.proposer = proposer
        if policy_id:
            self.policy_id = policy_id
        self.model_id = model_id
        self.version = version
        self.allow_confirmation = bool(allow_confirmation)

    @property
    def available(self) -> bool:
        return self.proposer is not None

    def describe(self) -> dict[str, Any]:
        described = super().describe()
        described.update(
            {
                "available": self.available,
                "loads_model": False,
                "note": (
                    "the adapter quotes a provider that was wired in; it never "
                    "loads, downloads or selects a model itself"
                ),
            }
        )
        return described

    def select_action(
        self,
        state: AgentState,
        actions: Sequence[AgentAction],
        *,
        mask: Any | None = None,
    ) -> PolicyDecision:
        if self.proposer is None:
            raise PolicyUnavailable(
                "no language-model proposer is wired: this adapter quotes a "
                "provider that already exists and never loads one itself, so "
                f"policy {self.policy_id!r} has nothing to ask"
            )
        if not actions:
            return _decision_for(
                (),
                reason_code=PolicyReasonCode.SAFETY_CONSTRAINT.value,
                confidence=0.0,
                mask=mask,
            )
        raw = self.proposer(state, actions)
        rows = as_mapping(raw)
        wanted = as_text(rows.get("action")) or as_text(rows.get("tool")) or as_text(
            rows.get("action_id")
        )
        match = self._match(wanted, actions)
        if match is None:
            return PolicyDecision(
                selected_action=None,
                confidence=0.0,
                policy_version=self.version,
                model_id=self.model_id,
                reason_code=PolicyReasonCode.PLANNER_DECISION.value,
                notes=(
                    f"the proposal named {wanted!r}, which is not in the masked action "
                    "set: it is refused rather than guessed at",
                ),
                masked=tuple(item.to_dict() for item in getattr(mask, "blocked", ())),
            )
        reason = as_text(rows.get("reason_code"), PolicyReasonCode.PLANNER_DECISION.value)
        if reason not in tuple(member.value for member in PolicyReasonCode):
            reason = PolicyReasonCode.PLANNER_DECISION.value
        return PolicyDecision(
            selected_action=match,
            confidence=min(1.0, max(0.0, as_real(rows.get("confidence"), 0.5) or 0.0)),
            alternatives=tuple(item for item in actions if item.action_id != match.action_id),
            expected_value=as_real(rows.get("expected_value")),
            policy_version=self.version,
            model_id=self.model_id,
            requires_confirmation=match.requires_confirmation,
            reason_code=reason,
            notes=(
                "the proposal carried structured fields only; any free text it "
                "included was dropped rather than stored",
            ),
            masked=tuple(item.to_dict() for item in getattr(mask, "blocked", ())),
        )

    @staticmethod
    def _match(wanted: str, actions: Sequence[AgentAction]) -> AgentAction | None:
        if not wanted:
            return None
        needle = wanted.strip().lower()
        for item in actions:
            if needle in (item.action_id.lower(), item.name.lower(), item.label.lower()):
                return item
        return None


# ── helpers ──────────────────────────────────────────────────────────────────


#: Words that carry no selection signal. Without this list "the" would match a
#: tool called "the" and every goal would name every action.
STOP_WORDS: frozenset[str] = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "into", "then",
        "its", "use", "you", "your", "are", "was", "has", "have", "any", "all",
        "out", "not", "but", "can", "done", "first", "next", "task", "goal",
    }
)


def _words(text: str) -> tuple[str, ...]:
    """The meaningful words of a piece of text, lower-cased and de-stopworded."""
    cleaned = "".join(character if character.isalnum() else " " for character in text.lower())
    return tuple(
        word for word in cleaned.split() if len(word) > 1 and word not in STOP_WORDS
    )


def _name_tokens(action: AgentAction) -> tuple[str, ...]:
    """An action's name split into its meaningful tokens (``search_web`` → 2)."""
    return _words(action.name.replace("_", " ")) or (action.name.lower(),)


def _ranked_by_words(
    actions: Sequence[AgentAction],
    words: set[str] | frozenset[str],
    *,
    prefer_not_in: frozenset[str] = frozenset(),
) -> list[AgentAction]:
    """Actions whose name shares words with ``words``, best match first.

    Ranking by how many tokens match is what lets a shallow deterministic rule
    tell ``search_web`` from ``search_files`` when the goal mentions the web —
    without knowing anything the state did not publish. ``prefer_not_in`` is a
    TIE-BREAK, not a filter: between two equally named actions the one that has
    not already verified successfully is preferred, and an action that is still
    the right one keeps working.
    """
    scored: list[tuple[int, int, int, AgentAction]] = []
    for position, action in enumerate(actions):
        if action.is_noop:
            continue
        tokens = _name_tokens(action)
        score = sum(1 for token in tokens if token in words)
        if score:
            fresh = 0 if action.signature() in prefer_not_in else 1
            scored.append((score, fresh, -position, action))
    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return [item[3] for item in scored]


def _succeeded_signatures(state: AgentState) -> frozenset[str]:
    """The action signatures the state says already verified successfully."""
    succeeded: set[str] = set()
    for row in state.verification_results:
        if as_text(row.get("status")) != "pass":
            continue
        signature = as_text(row.get("action_signature")) or as_text(row.get("action"))
        if signature:
            succeeded.add(signature)
    return frozenset(succeeded)


def _context_words(state: AgentState) -> frozenset[str]:
    """The words the state's own context publishes, for the clue rule."""
    parts: list[str] = []
    for key in ("clue", "context", "hint", "situation"):
        value = state.environment_state.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, Mapping):
            parts.extend(str(item) for item in value.values() if isinstance(item, str))
    for value in state.context.values():
        if isinstance(value, str):
            parts.append(value)
    return frozenset(_words(" ".join(parts)))


def _last_failed_signature(state: AgentState) -> str:
    """The signature of the action the state says just failed, if it says so.

    Read from the structured error the rollout writes, not by parsing prose: an
    error that does not name a failing action simply yields no signature, and the
    rule is skipped rather than guessed.
    """
    for row in reversed(state.verification_results):
        if as_text(row.get("status")) == "fail":
            failed = as_text(row.get("action_signature"))
            if failed:
                return failed
    for error in reversed(state.active_errors):
        if ":" in error and error.startswith("failed_action:"):
            return error.split(":", 1)[1].strip()
    return ""


def _ordered(preferred: Sequence[AgentAction], all_actions: Sequence[AgentAction]) -> list[AgentAction]:
    """``preferred`` first, then the rest, each in its original order."""
    chosen = list(preferred)
    seen = {item.action_id for item in chosen}
    for item in all_actions:
        if item.action_id not in seen:
            chosen.append(item)
            seen.add(item.action_id)
    return chosen


# ── registry ─────────────────────────────────────────────────────────────────

#: The policy kinds a configuration may name.
POLICY_KINDS: tuple[str, ...] = ("rule_based", "mock", "llm_adapter", "scripted")


def build_policy(name: str, **kwargs: Any) -> AgentPolicy:
    """The policy one name asks for, built from what the caller supplies.

    A learned-policy slot is deliberately absent: until a policy that LEARNS
    exists, a configuration naming one gets a clear refusal rather than a
    placeholder that would make "no learned policy is enabled" ambiguous.
    """
    wanted = as_text(name)
    if wanted in {"rule_based", "deterministic", "baseline"}:
        return RuleBasedPolicy(**kwargs)
    if wanted in {"mock", "scripted", "deterministic_mock"}:
        script = kwargs.pop("script", ())
        return MockPolicy(script, **kwargs)
    if wanted in {"llm", "llm_adapter", "language_model"}:
        return LLMPolicyAdapter(**kwargs)
    raise KeyError(
        f"unknown policy {wanted!r}: this phase ships "
        + ", ".join(POLICY_KINDS)
        + ". A learned policy implements AgentPolicy and is wired in explicitly; "
        "normal NovaControl keeps using its own decision engine and planner."
    )


def policy_descriptions() -> dict[str, dict[str, Any]]:
    """What each policy is, without building one."""
    described: dict[str, dict[str, Any]] = {}
    for name in POLICY_KINDS:
        if name in {"rule_based", "deterministic"}:
            described[name] = RuleBasedPolicy().describe()
        elif name in {"mock", "scripted"}:
            described[name] = MockPolicy().describe()
        else:
            described[name] = LLMPolicyAdapter().describe()
    return described


def random_action(actions: Sequence[AgentAction], *, seed: int) -> AgentAction | None:
    """A deterministic uniform choice from a set, for exploration.

    Seeded rather than system-random so an episode is reproducible: the same
    seed explores the same way, which is what makes exploration testable at all.
    """
    if not actions:
        return None
    generator = random.Random(int(seed))
    return actions[generator.randrange(len(actions))]


__all__ = [
    "NO_ACTION",
    "POLICY_KINDS",
    "PROPOSAL_KEYS",
    "AgentPolicy",
    "LLMPolicyAdapter",
    "LLMProposal",
    "MockPolicy",
    "PolicyUnavailable",
    "RuleBasedPolicy",
    "build_policy",
    "policy_descriptions",
    "random_action",
]
