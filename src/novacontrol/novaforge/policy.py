"""NF-08 C — state-dependent decision policy contracts.

A decision is a *proposal* with its reason and the evidence it was made from.
That is the whole difference between a policy and a script: the proposal is a
function of the latest observation, and the loop re-reads the environment after
acting instead of assuming the script's next line is still the right one.

NF-08 shipped ONE deterministic rule (``HarnessReadyPolicy``). NF-10 adds a
state-dependent policy (``HarnessStagesPolicy``) and the fixed-action BASELINE
it is compared against (``FixedKeyPolicy``). None of them is a gameplay brain:
each reports ``model_backed`` honestly, and a policy that consults a model's
description labels that decision. NovaControl still has no trained gameplay
policy, and an assistant model has not been shown to choose actions from a
screen. Adding one remains implementing ``DecisionProvider`` — not changing
the loop.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .observation import GameObservation


class PolicyUnavailable(Exception):
    """No policy can decide on this host; the loop must say so, not improvise."""


@dataclass(frozen=True, slots=True)
class DecisionProposal:
    """One proposed action, with why and what it should cause."""

    action: dict[str, Any]
    reason: str
    evidence: tuple[str, ...] = ()
    expected: str = ""
    confidence: float | None = None
    policy: str = ""
    policy_version: int = 1
    #: True only when the CHOICE was made from a model's output. A deterministic
    #: rule that merely reads a model's description as corroboration is not
    #: model-backed, and a scripted action never is.
    model_backed: bool = False
    #: What the policy could not establish, in its own words. A proposal can be
    #: worth making while still carrying uncertainty; the loop records both.
    uncertainty: tuple[str, ...] = ()
    #: A checkable expectation for the NEXT observation, e.g.
    #: ``{"title_contains": "READY"}``. The loop compares it after acting; it
    #: never substitutes for the verifier's verdict.
    expects: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.action, Mapping) or not self.action.get("kind"):
            raise ValueError("A proposal needs an action with a kind.")
        if not str(self.reason).strip():
            raise ValueError("A proposal needs a reason; an unexplained action is refused.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": dict(self.action),
            "reason": self.reason,
            "evidence": list(self.evidence),
            "expected": self.expected,
            "confidence": self.confidence,
            "policy": self.policy,
            "policy_version": self.policy_version,
            "model_backed": self.model_backed,
            "uncertainty": list(self.uncertainty),
            "expects": dict(self.expects),
        }


@runtime_checkable
class DecisionProvider(Protocol):
    """Everything a closed loop needs from a policy."""

    @property
    def name(self) -> str:
        """A short, stable identity for the record."""

    @property
    def available(self) -> bool:
        """Whether this policy can decide anything right now."""

    async def propose(
        self, observation: GameObservation, *, history: Sequence[GameObservation] = ()
    ) -> DecisionProposal | None:
        """The next action, or None when there is nothing honest left to do."""


def evidence_refs(observation: GameObservation) -> tuple[str, ...]:
    """The references a proposal cites — the observation's own evidence."""
    refs = [str(item.get("reference") or "") for item in observation.evidence]
    refs.append(f"observation:{observation.observed_at}")
    return tuple(item for item in refs if item)


@dataclass(slots=True)
class HarnessReadyPolicy:
    """The disposable harness's rule, as a state-dependent policy.

    While the target's title does not contain ``trigger`` it proposes one key
    tap; once the title contains it, it proposes nothing because the objective
    is verifiable and the loop must verify rather than keep pressing.

    ``stall_limit`` is the honesty guard: if the last N observations show no
    change at all, pressing again is not a decision, it is a loop, so the policy
    stops and says that.
    """

    trigger: str = "READY"
    key: str = "space"
    duration_ms: int = 60
    stall_limit: int = 3
    policy_id: str = "HarnessReadyPolicy"
    version: int = 1
    #: Its one action CLAIMS to satisfy the objective, so a verifier failure
    #: after it is a failed attempt. A multi-step policy sets this False: the
    #: objective is expected to fail until the LAST stage, and treating an
    #: intermediate "not yet" as a verdict would fail every honest run.
    fail_on_unmet_verification: bool = True
    _last_reason: str = field(default="", init=False, repr=False)

    @property
    def name(self) -> str:
        return self.policy_id

    @property
    def available(self) -> bool:
        return True

    @property
    def last_reason(self) -> str:
        """Why the policy last proposed nothing (for the loop's stop reason)."""
        return self._last_reason

    def capability(self) -> dict[str, Any]:
        return {
            "policy": self.policy_id,
            "version": self.version,
            "available": True,
            "model_backed": False,
            "kind": "deterministic state rule",
            "reason": (
                "This policy is an explicit rule over the latest observation (press a key "
                "while a known marker is absent). No model or learned policy is involved, "
                "and it is not evidence of gameplay intelligence."
            ),
        }

    async def propose(
        self, observation: GameObservation, *, history: Sequence[GameObservation] = ()
    ) -> DecisionProposal | None:
        self._last_reason = ""
        if not observation.actionable:
            self._last_reason = (
                f"The observation is {observation.status}; a policy never acts on a read it "
                "cannot trust."
            )
            return None
        title = observation.title
        if self.trigger.lower() in title.lower():
            self._last_reason = (
                f"The target already reports {self.trigger!r}; the objective is for the "
                "verifier to decide, not for the policy to keep acting."
            )
            return None
        if len(history) >= self.stall_limit and self._stalled(history):
            self._last_reason = (
                f"The last {len(history)} observations show no change; repeating the same "
                "action is not a decision."
            )
            return None
        return DecisionProposal(
            action={"kind": "key", "key": self.key, "duration_ms": self.duration_ms},
            reason=(
                f"The title {title!r} does not contain {self.trigger!r}, so the harness has "
                f"not yet registered a key press; one {self.key!r} tap is the next step."
            ),
            evidence=evidence_refs(observation),
            expected=f"the window title will contain {self.trigger!r}",
            confidence=0.9,
            policy=self.policy_id,
            # The proposal carries its own identity; a caller holding one must
            # not have to look at the loop to learn which policy version chose it.
            policy_version=self.version,
            expects={"title_contains": self.trigger},
        )

    def _stalled(self, history: Sequence[GameObservation]) -> bool:
        window = list(history)[-self.stall_limit :]
        if len(window) < self.stall_limit:
            return False
        first = window[0]
        for item in window[1:]:
            if item.title != first.title:
                return False
            if (item.ocr.text if item.ocr else "") != (first.ocr.text if first.ocr else ""):
                return False
        return True


#: The disposable harness's NF-10 stage task, as data. The window title names
#: the stage (``NC FORGE HARNESS - STAGE IDLE``), so the policy and the verifier
#: read the same measured fact. A wrong key puts the window in FAULT, which is
#: the recovery branch: R returns it to IDLE instead of the run simply failing.
HARNESS_STAGES: tuple[str, ...] = ("idle", "armed", "fault", "complete")

STAGE_ACTIONS: dict[str, str] = {
    "idle": "a",
    "armed": "b",
    "fault": "r",
}

STAGE_EXPECTATIONS: dict[str, str] = {
    "idle": "STAGE ARMED",
    "armed": "STAGE COMPLETE",
    "fault": "STAGE IDLE",
}


def _stage_from_title(title: str) -> str:
    """The stage the WINDOW TITLE states, or "" when it states none.

    Only the ``STAGE <name>`` form is accepted. Matching a bare word would let
    unrelated window text ("loading complete?") be read as a game state, which
    is the same mistake as guessing an objective from a title.
    """
    text = " ".join(str(title or "").lower().split())
    marker = "stage "
    start = text.find(marker)
    if start < 0:
        return ""
    rest = text[start + len(marker) :]
    word = rest.split(" ", 1)[0].strip("\u2014-:|,.;")
    return word if word in HARNESS_STAGES else ""


def _stage_from_vision(observation: GameObservation) -> str:
    """A stage a MODEL claims to see, or "" from anything short of a claim.

    A model interpretation is usable here only when it is a structured reading
    with no recorded conflict. Prose that happens to contain a word is not a
    state: ``status`` must be ``interpreted`` and the description must mention
    the stage marker, so a vague sentence cannot choose an action.
    """
    vision = observation.vision or {}
    if str(vision.get("status") or "") != "interpreted":
        return ""
    if observation.conflicts or vision.get("conflicts"):
        return ""
    text = " ".join(str(vision.get("description") or "").lower().split())
    marker = "stage "
    start = text.find(marker)
    if start < 0:
        return ""
    rest = text[start + len(marker) :]
    word = rest.split(" ", 1)[0].strip("\u2014-:|,.;")
    return word if word in HARNESS_STAGES else ""


@dataclass(slots=True)
class HarnessStagesPolicy:
    """NF-10: a state-dependent policy over the disposable harness's stages.

    The measured window title decides. Each stage has its own action, so the
    same key is never always correct and the next action depends on what the
    previous one caused:

    ``IDLE`` --A--> ``ARMED`` --B--> ``COMPLETE``
    ``FAULT`` --R--> ``IDLE``   (recovery after a wrong key)

    A model's description is consulted ONLY when the title states no stage, and
    a decision made that way is returned with ``model_backed=True`` and its
    uncertainty. The policy cannot execute, approve or verify anything; it
    returns a proposal and the loop remains the only caller of the gates.
    """

    policy_id: str = "HarnessStagesPolicy"
    version: int = 1
    duration_ms: int = 60
    stall_limit: int = 3
    vision_fallback: bool = True
    #: The objective is only expected to hold at COMPLETE. An intermediate
    #: verified_failure means "not finished", which is why the loop continues.
    fail_on_unmet_verification: bool = False
    _last_reason: str = field(default="", init=False, repr=False)
    _last_model_backed: bool = field(default=False, init=False, repr=False)

    @property
    def name(self) -> str:
        return self.policy_id

    @property
    def available(self) -> bool:
        return True

    @property
    def last_reason(self) -> str:
        """Why the policy last proposed nothing (for the loop's stop reason)."""
        return self._last_reason

    def capability(self) -> dict[str, Any]:
        return {
            "policy": self.policy_id,
            "version": self.version,
            "available": True,
            "model_backed": False,
            "kind": "deterministic stage rule",
            "vision_fallback": bool(self.vision_fallback),
            "stages": {stage: STAGE_ACTIONS.get(stage, "(verify)") for stage in HARNESS_STAGES},
            "reason": (
                "This policy reads the stage from the measured window title and chooses "
                "that stage's action; a model description is used only when the title "
                "states no stage, and every such proposal is labelled model-backed. It "
                "is a rule over observations, not a learned or general gameplay policy."
            ),
        }

    async def propose(
        self, observation: GameObservation, *, history: Sequence[GameObservation] = ()
    ) -> DecisionProposal | None:
        self._last_reason = ""
        self._last_model_backed = False
        if not observation.actionable:
            self._last_reason = (
                f"The observation is {observation.status}; a policy never acts on a read it "
                "cannot trust."
            )
            return None
        stage = _stage_from_title(observation.title)
        used_vision = False
        uncertainty: list[str] = []
        if not stage and self.vision_fallback:
            stage = _stage_from_vision(observation)
            used_vision = bool(stage)
            if stage:
                uncertainty.append(
                    "The window title states no stage, so this decision rests on a model's "
                    "description of a frame — a claim, not measured evidence."
                )
        if not stage:
            self._last_reason = (
                "The observation does not state a recognised harness stage in its window "
                "title, and no usable model interpretation supplies one; the policy refuses "
                "to guess an action."
            )
            return None
        if stage == "complete":
            self._last_reason = (
                "The station reports COMPLETE; success is the objective verifier's verdict, "
                "not something the policy should act on."
            )
            return None
        if len(history) >= self.stall_limit and self._stalled(history):
            self._last_reason = (
                f"The last {len(history)} observations show the same stage and text; "
                "repeating the action is not a decision."
            )
            return None
        key = STAGE_ACTIONS[stage]
        expected = STAGE_EXPECTATIONS[stage]
        self._last_model_backed = used_vision
        return DecisionProposal(
            action={"kind": "key", "key": key, "duration_ms": self.duration_ms},
            reason=(
                f"The harness is in stage {stage.upper()}; {key!r} is that stage's action "
                f"and should move it to {expected.removeprefix('STAGE ')}."
            ),
            evidence=evidence_refs(observation),
            expected=f"the window title will contain {expected!r}",
            # A rule over measured evidence is trusted as a RULE, not as a
            # probability: there is no calibrated confidence to report.
            confidence=None,
            policy=self.policy_id,
            policy_version=self.version,
            model_backed=used_vision,
            uncertainty=tuple(uncertainty),
            expects={"title_contains": expected},
        )

    def _stalled(self, history: Sequence[GameObservation]) -> bool:
        window = list(history)[-self.stall_limit :]
        if len(window) < self.stall_limit:
            return False
        first = window[0]
        for item in window[1:]:
            if item.title != first.title:
                return False
            if (item.ocr.text if item.ocr else "") != (first.ocr.text if first.ocr else ""):
                return False
        return True


@dataclass(slots=True)
class FixedKeyPolicy:
    """The NF-10 BASELINE: one action, every observation.

    This is what "a script" looks like in the same interface. It deliberately
    ignores the state, so on a multi-state task it can only succeed by luck. It
    exists to be compared against :class:`HarnessStagesPolicy` under identical
    limits — not as a fallback the product would ship.
    """

    key: str = "space"
    duration_ms: int = 60
    policy_id: str = "FixedKeyPolicy"
    version: int = 1
    #: A fixed action is an attempt at the objective, so one verifier failure
    #: after it ends the trial — the baseline is not given extra retries by a
    #: softer rule than the candidate's.
    fail_on_unmet_verification: bool = True
    _last_reason: str = field(default="", init=False, repr=False)

    @property
    def name(self) -> str:
        return self.policy_id

    @property
    def available(self) -> bool:
        return True

    @property
    def last_reason(self) -> str:
        return self._last_reason

    def capability(self) -> dict[str, Any]:
        return {
            "policy": self.policy_id,
            "version": self.version,
            "available": True,
            "model_backed": False,
            "kind": "fixed action script (baseline)",
            "reason": (
                "This baseline presses one fixed key on every observation. It does not "
                "read the state, so it is the comparison a state-dependent policy must "
                "beat — not a gameplay policy."
            ),
        }

    async def propose(
        self, observation: GameObservation, *, history: Sequence[GameObservation] = ()
    ) -> DecisionProposal | None:
        self._last_reason = ""
        if not observation.actionable:
            self._last_reason = (
                f"The observation is {observation.status}; even the baseline does not act "
                "on a read it cannot trust."
            )
            return None
        return DecisionProposal(
            action={"kind": "key", "key": self.key, "duration_ms": self.duration_ms},
            reason=(
                f"The baseline is a fixed script: {self.key!r} is pressed on every "
                "observation, whatever the stage says."
            ),
            evidence=evidence_refs(observation),
            expected=f"the fixed action {self.key!r} will be sent",
            confidence=None,
            policy=self.policy_id,
            policy_version=self.version,
            model_backed=False,
            expects={},
        )


@dataclass(frozen=True, slots=True)
class UnavailablePolicy:
    """A named refusal to decide, for a build with no policy installed."""

    reason: str = "No gameplay decision policy is installed in this build."
    policy_id: str = "unavailable"

    @property
    def name(self) -> str:
        return self.policy_id

    @property
    def available(self) -> bool:
        return False

    async def propose(
        self, observation: GameObservation, *, history: Sequence[GameObservation] = ()
    ) -> DecisionProposal | None:
        raise PolicyUnavailable(self.reason)

    def capability(self) -> dict[str, Any]:
        return {
            "policy": self.policy_id,
            "available": False,
            "model_backed": False,
            "kind": "none",
            "reason": self.reason,
        }
