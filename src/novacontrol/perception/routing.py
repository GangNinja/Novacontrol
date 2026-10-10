"""Routing: which perception path a request deserves, decided before any work.

The rule this module exists to enforce is simple to state and easy to break:
**a vision model is never the first thing asked.** It costs seconds on a CPU-only
machine, it is unavailable on plenty of machines, and most questions about a
screen are answered by reading the screen's text. So the router classifies the
request and produces a plan:

    "read the text on screen"          -> OCR (+ regions); no model
    "what objects are visible?"        -> detection first; no model
    "where is the login button?"       -> OCR geometry, then the model if the
                                          word is not among the read ones
    "describe what is happening"       -> fast evidence, then the model, because
                                          describing needs semantics the fast
                                          path cannot produce

Escalation is therefore a DECISION with a reason, not a habit. The plan records
the reading that produced it, so a status surface can explain why a given frame
did or did not reach a model. ``escalate_when_fast_insufficient`` distinguishes
"ask the model because the question requires semantics" from "ask the model only
if the fast evidence came back empty".

The classifier reads MEANING, not keywords about the pipeline: it reuses Phase
6's own question classifier (``task_for_question``) so a question is understood
the same way by both layers, and adds only the distinction this layer needs —
whether a request is about text, objects, a location, or the scene as a whole.
"""

from __future__ import annotations

import re

from novacontrol.perception.models import (
    PerceptionCapability,
    PerceptionMode,
    PerceptionPlan,
    PerceptionRequest,
)
from novacontrol.vision.manager import task_for_question
from novacontrol.vision.models import VisionTaskKind

__all__ = ["PerceptionRouter", "describe_question"]

#: A question about the OBJECTS in a frame rather than about its text.
_OBJECT_QUESTION = re.compile(
    r"\b(?:what objects?|which objects?|how many objects?|what is visible"
    r"|what can you see|what do you see|what is on (?:the|my) (?:screen|desk|table)"
    r"|list the objects?|detect|identify)\b",
    re.IGNORECASE,
)

#: A question that wants the scene EXPLAINED — the one that genuinely needs eyes.
_SCENE_QUESTION = re.compile(
    r"\b(?:describe|explain|what is happening|what'?s happening|what is going on"
    r"|summari[sz]e|what is this (?:scene|picture|image|screenshot) (?:about|showing)"
    r"|tell me about (?:this|the) (?:scene|image|picture|screenshot))\b",
    re.IGNORECASE,
)


def describe_question(question: str, *, target: str = "") -> str:
    """What KIND of question this is — the router's own reading of it.

    Deliberately a small, named vocabulary (``text``/``locate``/``objects``/
    ``scene``/``general``) because each value maps to a different plan, and a
    free-form description would not be something a caller could branch on.
    """
    asked = " ".join(str(question or "").split())
    if str(target or "").strip():
        return "locate"
    if _SCENE_QUESTION.search(asked):
        return "scene"
    if _OBJECT_QUESTION.search(asked):
        return "objects"
    task = task_for_question(asked)
    if task is VisionTaskKind.OCR:
        return "text"
    if task is VisionTaskKind.LOCATE:
        return "locate"
    if not asked:
        return "general"
    return "general"


class PerceptionRouter:
    """Turns a request plus what this machine can do into a plan."""

    def __init__(self, *, escalate_scene_questions: bool = True) -> None:
        self.escalate_scene_questions = escalate_scene_questions

    def plan(
        self,
        request: PerceptionRequest,
        *,
        vlm_available: bool | None = None,
        ocr_available: bool | None = None,
        detection_available: bool | None = None,
        segmentation_available: bool | None = None,
    ) -> PerceptionPlan:
        """The plan for one request, with the reasons that produced it.

        Availability is passed IN rather than probed here: the router decides
        what to attempt, the engine knows what is wired, and a router that went
        looking for providers would be a second place that knows how to probe.
        """
        kind = describe_question(request.asked, target=request.target)
        reasons: list[str] = [f"the question reads as a {kind} question"]
        fast: list[PerceptionCapability] = []
        if request.allow_ocr and (ocr_available is not False):
            fast.append(PerceptionCapability.OCR)
        if (
            request.allow_detection
            and kind in {"objects", "scene", "general"}
            and detection_available is not False
        ):
            fast.append(PerceptionCapability.DETECTION)
        if request.allow_segmentation and kind == "objects":
            fast.append(PerceptionCapability.SEGMENTATION)
        wants_scene = kind in {"scene", "locate", "general"}
        deep = self._wants_model(request, kind, reasons)
        if deep and request.allow_vlm and vlm_available is False:
            reasons.append("no vision model is wired, so the deep path cannot run")
        if request.allow_vlm and vlm_available is False and wants_scene and not deep:
            reasons.append("a vision model would be needed to go further, and none is wired")
        if not fast and not deep:
            reasons.append(
                "nothing in the request is enabled on this machine: no OCR, no "
                "detection, and no vision model"
            )
        unavailable = ""
        if not fast and not deep and request.allow_vlm and vlm_available is True:
            unavailable = "every perception capability was disabled by the request"
        return PerceptionPlan(
            mode=request.mode,
            fast=tuple(fast),
            deep=deep,
            reasons=tuple(reasons),
            escalate_when_fast_insufficient=request.mode
            in {PerceptionMode.AUTO, PerceptionMode.HYBRID, PerceptionMode.DEEP},
            deep_question=request.asked
            or str(request.target or "")
            or "Describe what is visible, and any errors or warnings.",
            unavailable_reason=unavailable,
        )

    def _wants_model(self, request: PerceptionRequest, kind: str, reasons: list[str]) -> bool:
        """Whether a vision model belongs in this plan at all — and the why."""
        if not request.allow_vlm:
            reasons.append("the request forbids a vision model")
            return False
        if request.mode is PerceptionMode.FAST:
            reasons.append("the request asks for the fast path only")
            return False
        if request.mode is PerceptionMode.DEEP:
            reasons.append("the request asks for the deep path")
            return True
        if kind == "locate":
            reasons.append(
                "a locator question is answered by the read word when it was placed, "
                "so the model is the fallback rather than the first attempt"
            )
            return True
        if kind == "scene" and self.escalate_scene_questions:
            reasons.append(
                "describing a scene needs semantics the fast path does not produce"
            )
            return True
        if kind in {"objects", "text"}:
            reasons.append(
                "the fast path can answer this without a model; a model is used only "
                "if the fast evidence comes back empty"
            )
            return False
        reasons.append("no route was recognised, so the model is allowed as a fallback")
        return True

