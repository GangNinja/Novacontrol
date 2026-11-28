"""NF-27 — a model-backed decision policy: NovaForge chooses, not a script.

NF-08/NF-10 shipped rules over observations (``HarnessReadyPolicy``,
``HarnessStagesPolicy``) and an honest baseline (``FixedKeyPolicy``). All three
are deterministic: the action is a function *this build's author* wrote. This
module adds the missing kind — ``ModelDecisionPolicy`` — where the action is
chosen by a model that is shown the target's own frame.

The contract is the one the closed loop already calls: ``propose(observation)``
returns ONE bounded ``DecisionProposal``. Nothing here executes, approves or
verifies anything; the loop remains the only caller of the plan/execute gates,
so the ActionPolicy, digest binding, Guardian, action budget and InputGuard
apply exactly as they do for a hand-typed command.

Three honesty rules this policy keeps:

* **A claim is not evidence.** Every proposal is labelled
  ``model_backed=True`` and carries the uncertainty that the choice came from a
  model's reading of an image, not from a measured fact. The verifier still
  decides whether the objective was met; a model saying "done" is reported as
  the model's claim and never as success.
* **Only a bounded, typed action leaves here.** The model answers in a strict
  JSON shape; the action is coerced into the kinds this build can send (key,
  hold, click, move) and clamped to the gameplay bounds. Anything else is
  refused with a stated reason rather than approximated.
* **No model, no decision.** ``available`` is False when no viewer is wired or
  the objective is blank, and the loop reports that instead of improvising.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .actions import ACTION_KINDS, DEFAULT_ALLOWED_KEYS
from .observation import GameObservation
from .policy import DecisionProposal, evidence_refs

__all__ = [
    "DecisionSeer",
    "ModelDecisionPolicy",
    "build_decision_prompt",
    "parse_decision",
]


#: ``(prompt, image_path) -> the model's raw answer``. The application supplies
#: this from the LIVE vision provider, so installing a different VLM changes
#: what NovaForge decides without changing this policy.
DecisionSeer = Callable[[str, str], Awaitable[str]]

#: Keys this policy will put in front of a model. The gameplay policy is the
#: authority on what may be SENT; offering a key it would refuse wastes a
#: decision, so the menu is built from its own allowed set.
_MENU_KEYS: tuple[str, ...] = tuple(
    key for key in sorted(DEFAULT_ALLOWED_KEYS) if len(key) > 0
)

#: Recognised expectation checks — the same three the loop can evaluate.
_EXPECT_KEYS = frozenset({"title_contains", "title_equals", "text_contains"})

DEFAULT_STALL_LIMIT = 3


def build_decision_prompt(
    *,
    objective: str,
    success_criteria: str,
    observation: GameObservation,
    recent_actions: Sequence[Mapping[str, Any]] = (),
    width: int,
    height: int,
) -> str:
    """The one prompt this policy sends. Image + measured evidence, one action.

    Everything in it is either the operator's own words (the objective), a
    measurement NovaForge already made (title, recognised text, frame size), or
    the fixed action vocabulary. The screenshot is the only unknown, and it is
    the point: the model is being asked what to do about what it can SEE.
    """
    ocr = ""
    if observation.ocr is not None:
        ocr = " ".join(str(observation.ocr.text or "").split())[:1200]
    changes = "; ".join(observation.changes[:6]) or "(none recorded)"
    history_lines: list[str] = []
    for index, item in enumerate(list(recent_actions)[-5:], start=1):
        action = item.get("action") if isinstance(item, Mapping) else None
        kind = (action or {}).get("kind") if isinstance(action, Mapping) else ""
        key = (action or {}).get("key") if isinstance(action, Mapping) else ""
        point = ""
        if isinstance(action, Mapping) and action.get("x") is not None:
            point = f" at ({action.get('x')}, {action.get('y')})"
        what = f"{kind} {key}".strip() + point or "(unknown action)"
        expectation = str(item.get("expected") or "").strip()
        history_lines.append(
            f"{index}. {what} — expected: {expectation or '(not stated)'}"
        )
    history = "\n".join(history_lines) or "(nothing has been attempted yet)"

    return (
        "You are NovaForge, an agent that plays a PC game by sending bounded "
        "keyboard and mouse input. You are looking at a screenshot of the game "
        "window. Decide the single next action that makes progress on the "
        "operator's objective.\n\n"
        f"OBJECTIVE (the operator's words): {objective.strip()}\n"
        + (
            f"SUCCESS CRITERIA (the operator's words): {success_criteria.strip()}\n"
            if success_criteria.strip()
            else ""
        )
        + f"\nWINDOW TITLE (measured): {observation.title or '(empty)'}\n"
        f"TEXT RECOGNISED ON SCREEN BY OCR (measured, may be partial): {ocr or '(none)'}\n"
        f"WHAT CHANGED SINCE THE LAST FRAME: {changes}\n"
        f"ACTIONS YOU ALREADY TOOK (newest last):\n{history}\n\n"
        f"THE SCREENSHOT IS {width}x{height} PIXELS.\n"
        "You may send exactly ONE action, and only these kinds:\n"
        '  {"kind": "key",  "key": "<name>", "duration_ms": <40-2000>}   '
        "a single key tap\n"
        '  {"kind": "hold", "key": "<name>", "duration_ms": <100-2000>}  '
        "hold a key (walking, driving)\n"
        '  {"kind": "click","x_frac": <0-1>, "y_frac": <0-1>}           '
        "left-click a point, given as a fraction of the image\n"
        '  {"kind": "move", "x_frac": <0-1>, "y_frac": <0-1>}           '
        "move the cursor to a point (no click)\n"
        f"Usable keys: {', '.join(_MENU_KEYS)}\n\n"
        "Aim for ONE action per decision; you will see the next frame before "
        "deciding again. If the objective is already visibly complete, say so "
        "with \"done\": true instead of acting.\n\n"
        "Answer with ONE JSON object and nothing else — no prose, no code fence:\n"
        "{\n"
        '  "done": false,\n'
        '  "action": {"kind": "key", "key": "w", "duration_ms": 300},\n'
        '  "reason": "one sentence: why this action, from what you see",\n'
        '  "expected": "what should be different in the next frame",\n'
        '  "expects": {"text_contains": "optional literal text that should appear"},\n'
        '  "confidence": 0.0,\n'
        '  "uncertainty": ["what you could not establish from this frame"]\n'
        "}"
    )


def _first_json_object(text: str) -> dict[str, Any] | None:
    """The first balanced ``{...}`` in an answer, or None.

    Models wrap JSON in prose and code fences; scanning for a balanced object
    keeps the parser honest about what it accepted (the WHOLE object) instead of
    repairing a truncated answer into a different decision.
    """
    raw = str(text or "")
    start = raw.find("{")
    while start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(raw)):
            char = raw[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(raw[start : index + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = raw.find("{", start + 1)
    return None


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def parse_decision(
    answer: str, *, width: int, height: int
) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    """Read a model's answer into a legal action.

    Returns ``(action, problem, rest)``. A non-empty ``problem`` means the
    answer is NOT a usable decision; the caller reports it rather than guessing
    an action from half an answer. ``rest`` carries the reason, expectation,
    confidence and uncertainty the model stated, so the record keeps them.
    """
    payload = _first_json_object(answer)
    if payload is None:
        excerpt = " ".join(str(answer or "").split())[:200]
        return None, f"the answer contained no JSON object (it said: {excerpt!r}).", {}
    if payload.get("done") is True:
        return (
            None,
            "the model reported the objective as visibly complete — that is the "
            "verifier's verdict to give, so no action was taken.",
            {"done": True, "reason": str(payload.get("reason") or "").strip()},
        )
    action = payload.get("action")
    if not isinstance(action, Mapping):
        return None, "the answer stated no action object.", {}
    kind = str(action.get("kind") or "").strip().lower()
    if kind not in ACTION_KINDS:
        return (
            None,
            f"the action kind {kind or '(blank)'!r} is not one this build can "
            f"send (expected one of {', '.join(sorted(ACTION_KINDS))}).",
            {},
        )
    reason = " ".join(str(payload.get("reason") or "").split())[:400]
    if not reason:
        return None, "the model chose an action but stated no reason; an unexplained action is refused.", {}
    rest: dict[str, Any] = {
        "reason": reason,
        "expected": " ".join(str(payload.get("expected") or "").split())[:300],
        "confidence": _confidence(payload.get("confidence")),
        "uncertainty": [
            " ".join(str(item).split())[:200]
            for item in (payload.get("uncertainty") or [])
            if str(item).strip()
        ][:5],
    }
    expects = payload.get("expects")
    if isinstance(expects, Mapping):
        rest["expects"] = {
            str(key): str(value)
            for key, value in expects.items()
            if str(key) in _EXPECT_KEYS and str(value).strip()
        }
    if kind in {"key", "hold"}:
        key = str(action.get("key") or "").strip().lower()
        if key not in DEFAULT_ALLOWED_KEYS:
            return (
                None,
                f"the model asked for the key {key or '(blank)'!r}, which the gameplay "
                "policy does not allow.",
                {},
            )
        low, high, default = (40, 2000, 120) if kind == "key" else (100, 2000, 400)
        return (
            {"kind": kind, "key": key, "duration_ms": _clamp(action.get("duration_ms"), low, high, default)},
            "",
            rest,
        )
    x = _frac_point(action.get("x_frac"), width)
    y = _frac_point(action.get("y_frac"), height)
    if x is None or y is None:
        return None, "the model asked to click or move without a usable point.", {}
    return {"kind": kind, "x": x, "y": y, "button": "left"}, "", rest


def _frac_point(value: Any, size: int) -> int | None:
    """A 0–1 fraction of the frame, as a client-area pixel inside it."""
    try:
        fraction = float(value)
    except (TypeError, ValueError):
        return None
    if fraction != fraction:  # NaN
        return None
    fraction = max(0.0, min(1.0, fraction))
    return max(0, min(max(0, size - 1), int(round(fraction * size))))


def _confidence(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return max(0.0, min(1.0, number))


@dataclass(slots=True)
class ModelDecisionPolicy:
    """A decision policy whose choice comes from a model looking at the frame.

    ``objective`` is the operator's own words. ``decide`` is the live viewer
    seam; ``viewer_label`` names what is behind it for the record. ``available``
    is the honest gate: no viewer or no objective means no decision, reported as
    exactly that rather than filled in by a rule.
    """

    objective: str = ""
    success_criteria: str = ""
    decide: DecisionSeer | None = None
    viewer_label: str = ""
    unavailable_reason: str = ""
    policy_id: str = "ModelDecisionPolicy"
    version: int = 1
    stall_limit: int = DEFAULT_STALL_LIMIT
    #: Gameplay is multi-step: the objective is expected to be UNMET between
    #: steps, so a verified failure after one action is not a verdict about the
    #: run. The verifier still decides completion, and a policy that stops while
    #: the objective is unmet ends the run as failed.
    fail_on_unmet_verification: bool = False
    _last_reason: str = field(default="", init=False, repr=False)
    _last_action: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _taken: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _repeated: int = field(default=0, init=False, repr=False)
    _stats: dict[str, Any] = field(
        default_factory=lambda: {
            "asked": 0,
            "proposed": 0,
            "refused": 0,
            "unavailable": 0,
            "last_duration_ms": None,
            "last_problem": "",
        },
        init=False,
        repr=False,
    )

    # -- identity -----------------------------------------------------------

    @property
    def name(self) -> str:
        return self.policy_id

    @property
    def available(self) -> bool:
        return self.decide is not None and bool(str(self.objective).strip())

    @property
    def last_reason(self) -> str:
        return self._last_reason

    def capability(self) -> dict[str, Any]:
        viewer = self.viewer_label or "no viewer wired"
        if not self.available:
            reason = self.unavailable_reason or (
                "No vision model is wired, so this policy cannot see the screen and "
                "will not guess an action from nothing."
                if self.decide is None
                else "The session has no objective text, so there is nothing to decide toward."
            )
        else:
            reason = (
                "This policy shows the model the target's frame plus the measured "
                "evidence (window title, OCR text, recent actions) and asks it for ONE "
                "bounded action. Every proposal is labelled model-backed and carries "
                "its uncertainty; the verifier, not the model, decides completion."
            )
        return {
            "policy": self.policy_id,
            "version": self.version,
            "available": self.available,
            "model_backed": self.available,
            "kind": "model-chosen action (vision)",
            "viewer": viewer,
            "objective": str(self.objective).strip()[:300],
            "stats": dict(self._stats),
            "reason": reason,
        }

    # -- decision -----------------------------------------------------------

    async def propose(
        self, observation: GameObservation, *, history: Sequence[GameObservation] = ()
    ) -> DecisionProposal | None:
        self._last_reason = ""
        if not self.available:
            self._stats["unavailable"] += 1
            self._last_reason = self.capability()["reason"]
            return None
        if not observation.actionable:
            self._last_reason = (
                f"The observation is {observation.status}; a policy never acts on a read it "
                "cannot trust."
            )
            return None
        path = str((observation.frame or {}).get("path") or "")
        if not path:
            self._stats["unavailable"] += 1
            self._last_reason = (
                "The observation carries no frame path, so there is no image to decide "
                "from; the policy will not act blind."
            )
            return None
        width, height = _frame_size(observation)
        prompt = build_decision_prompt(
            objective=self.objective,
            success_criteria=self.success_criteria,
            observation=observation,
            recent_actions=self._taken,
            width=width,
            height=height,
        )
        started = time.monotonic()
        self._stats["asked"] += 1
        try:
            answer = await self.decide(prompt, path)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 - a viewer fault stops the run, it does not escape
            self._stats["refused"] += 1
            self._last_reason = (
                f"The vision model could not answer ({type(exc).__name__}: {exc}), so no "
                "action was chosen."
            )
            self._stats["last_problem"] = self._last_reason
            return None
        finally:
            self._stats["last_duration_ms"] = round((time.monotonic() - started) * 1000, 1)

        action, problem, rest = parse_decision(str(answer), width=width, height=height)
        if problem or action is None:
            self._stats["refused"] += 1
            self._stats["last_problem"] = problem
            self._last_reason = (
                ("The model reported the objective complete: " if rest.get("done") else "The model's answer was not a usable decision: ")
                + (problem or "")
            )
            return None

        repeated = self._repeat_count(action, observation, history)
        if repeated >= self.stall_limit:
            self._stats["refused"] += 1
            self._last_reason = (
                f"The model chose the same action {repeated} times and the screen did not "
                "change; repeating it is not a decision, so the loop stops here."
            )
            self._stats["last_problem"] = self._last_reason
            return None

        self._stats["proposed"] += 1
        self._last_action = dict(action)
        self._taken.append(
            {"action": dict(action), "expected": str(rest.get("expected") or "")}
        )
        self._taken = self._taken[-6:]
        uncertainty = [
            "The action was chosen by a model from a screenshot, not read from a "
            "measured fact; the verifier, not the model, decides whether it worked.",
        ]
        uncertainty.extend(rest.get("uncertainty") or [])
        return DecisionProposal(
            action=action,
            reason=rest.get("reason") or "The model chose this action from the frame.",
            evidence=evidence_refs(observation),
            expected=rest.get("expected") or "",
            confidence=rest.get("confidence"),
            policy=self.policy_id,
            policy_version=self.version,
            model_backed=True,
            uncertainty=tuple(uncertainty),
            expects=dict(rest.get("expects") or {}),
        )

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _screen_key(observation: GameObservation) -> tuple[str, str]:
        text = observation.ocr.text if observation.ocr is not None else ""
        return (observation.title, " ".join(str(text or "").split()))

    def _repeat_count(
        self,
        action: Mapping[str, Any],
        observation: GameObservation,
        history: Sequence[GameObservation],
    ) -> int:
        """How many times in a row this action produced no visible change.

        The loop adds the latest observation to its history BEFORE asking for a
        decision, so the observation it compares against is the second-newest.
        An identical action is only a stall when the screen did not move: walking
        forward across several frames is progress, pressing the same key into an
        unchanged screen is not.
        """
        items = list(history)
        earlier = items[-2] if len(items) >= 2 else None
        previous = {key: value for key, value in (self._last_action or {}).items() if key != "action_id"}
        current = {key: value for key, value in dict(action).items() if key != "action_id"}
        same_screen = earlier is not None and self._screen_key(earlier) == self._screen_key(observation)
        if previous == current and same_screen:
            self._repeated += 1
        else:
            self._repeated = 1
        return self._repeated


def _frame_size(observation: GameObservation) -> tuple[int, int]:
    frame = observation.frame or {}
    try:
        width = int(frame.get("width") or 0)
        height = int(frame.get("height") or 0)
    except (TypeError, ValueError):
        width = height = 0
    if width <= 0 or height <= 0:
        width, height = 1280, 720
    return width, height
