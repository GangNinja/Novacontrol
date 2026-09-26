"""Computer use, as a proposal the approval layer still has to authorise.

The pipeline that ends in a click is longer than the click::

    1. screenshot                                  (desktop controller)
    2. understand the UI                           (VisionManager)
    3. identify the target                         (VisionManager)
    4. action proposal                             (this module)
    5. permission / safety                         (core.security approval gate)
    6. click / type                                (desktop controller)
    7. screenshot again                            (desktop controller)
    8. verify the result                           (desktop vision verification)

Steps 1 and 6-8 already exist and are approval-gated; steps 2 and 3 are the
Phase 6 manager; step 5 is the same gate every other side-effecting action
passes through. Only step 4 is new, and it is new on purpose: this module turns
"what was seen" into "what could be done" and stops there.

**Nothing here executes anything, and nothing here can.** A proposal carries
``requires_approval = True`` as a constant rather than a field a caller could
flip, because a component that can approve its own action is the one thing this
layer must never be. There is deliberately no function that clicks: the caller
that wants to act takes the proposal to the existing approval-gated desktop
path, where a proposal with no approvable target is refused like any other.

A proposal is only produced when the target was actually SEEN with geometry to
act on. A model that says "the button is probably top-right" without a point
produces no proposal at all — an unlocatable guess is exactly what a click must
not be built on.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from novacontrol.vision.models import VisionElement, VisionResult

#: The pipeline, with the component that owns each step. Exposed as data so the
#: design is inspectable (and testable) rather than living only in a document.
COMPUTER_USE_PIPELINE: tuple[tuple[str, str], ...] = (
    ("screenshot", "desktop controller"),
    ("understand_ui", "vision manager"),
    ("identify_target", "vision manager"),
    ("action_proposal", "vision proposals"),
    ("permission_safety", "approval gate"),
    ("click_or_type", "desktop controller"),
    ("screenshot_again", "desktop controller"),
    ("verify_result", "desktop vision verification"),
)


@dataclass(frozen=True, slots=True)
class VisionActionProposal:
    """What could be done about what was seen — never what was done.

    ``requires_approval`` is not configurable: every proposal is approval-gated,
    and ``executed`` does not exist because this type cannot execute. A caller
    acts by handing the target to the desktop controller, which applies the same
    permission checks as any other action.
    """

    action: str
    target: str
    point: Mapping[str, int] = field(default_factory=dict)
    bounds: Mapping[str, int] = field(default_factory=dict)
    confidence: float = 0.0
    reason: str = ""

    #: Constant, not a field anyone can set to False at a call site.
    requires_approval: bool = True

    @property
    def actionable(self) -> bool:
        """Whether this proposal carries somewhere to act."""
        return bool(self.point or self.bounds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "target": self.target,
            "point": dict(self.point),
            "bounds": dict(self.bounds),
            "confidence": self.confidence,
            "requires_approval": self.requires_approval,
            "actionable": self.actionable,
            "reason": self.reason,
        }


def propose_click(result: VisionResult, target: str) -> VisionActionProposal | None:
    """Propose clicking ``target``, if the result actually located it.

    Returns ``None`` when nothing seen matches the target, or when the match
    carries no geometry — the two cases where a click would be a guess. The
    model's confidence is carried through rather than recomputed, and a missing
    confidence stays 0.0 instead of being invented.
    """
    label = " ".join(str(target or "").split()).strip()
    if not label:
        return None
    element = _match_element(result.ui_elements, label)
    if element is None:
        return None
    bounds = {str(key): int(value) for key, value in dict(element.bounds).items()}
    if element.bounds:
        width = abs(int(element.bounds.get("width", 0)))
        height = abs(int(element.bounds.get("height", 0)))
        point = {
            "x": int(element.bounds.get("x", 0)) + width // 2,
            "y": int(element.bounds.get("y", 0)) + height // 2,
        }
    else:
        point = {}
    proposal = VisionActionProposal(
        action="click",
        target=element.label,
        point=point,
        bounds=bounds,
        confidence=element.confidence,
        reason=f"{element.label} was located in a {result.image_type.value}.",
    )
    # A match with nowhere to act is not a proposal — it is a description.
    return proposal if proposal.actionable else None


def _match_element(
    elements: tuple[VisionElement, ...], label: str
) -> VisionElement | None:
    """The element that IS the label, exactly, then one that contains it.

    Exact (case-folded) first, then whole-word, then — for a label long enough
    to be meaningful — containment, so "login" finds a button labelled
    "Login with SSO" without a two-letter fragment matching everything. Among
    equals the shortest label wins, which keeps the choice deterministic.
    """
    folded = label.casefold()
    candidates = [element for element in elements if element.label.strip()]
    for element in candidates:
        if element.label.strip().casefold() == folded:
            return element
    for element in candidates:
        if re.search(rf"\b{re.escape(folded)}\b", element.label.casefold()):
            return element
    if len(folded) >= 4:
        contained = [
            element for element in candidates if folded in element.label.casefold()
        ]
        if contained:
            return min(contained, key=lambda item: (len(item.label), item.label))
    return None


def pipeline_description() -> tuple[Mapping[str, str], ...]:
    """The computer-use pipeline and who owns each step, as data."""
    return tuple({"step": step, "owner": owner} for step, owner in COMPUTER_USE_PIPELINE)
