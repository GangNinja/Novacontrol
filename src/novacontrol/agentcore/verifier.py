"""Verification Engine.

The orchestrator must distinguish three different things:
  1. the action executed (the runner returned),
  2. the action apparently succeeded (no error),
  3. the task actually completed (the expected state was OBSERVED).

Only #3 is success. The Verifier compares an expectation against a fresh
UiState / structured evidence and returns a typed result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from novacontrol.agentcore.ui_state import UiState


class VerificationStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class Expectation:
    """What success looks like for one step (or the whole task)."""

    description: str
    # Any of these evidence kinds can satisfy the expectation; the verifier
    # uses what is available and declares INCONCLUSIVE when nothing is.
    ui_contains: tuple[str, ...] = ()          # text that should appear in the UI
    ui_absent: tuple[str, ...] = ()            # text that must be gone (e.g. an error)
    url_contains: str = ""                     # browser URL fragment
    min_elements: int = 0                      # at least this many interactive elements
    requires_observed_change: bool = False     # the UI must differ from before the action
    # D12: the (handle, pid) identity of the window the objective named. When
    # set, a PASS also requires the fresh observation to have bound that SAME
    # window — an action that reported success against another window fails.
    target_identity: tuple[int, int] | None = None
    target_title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "ui_contains": list(self.ui_contains),
            "ui_absent": list(self.ui_absent),
            "url_contains": self.url_contains,
            "min_elements": self.min_elements,
            "requires_observed_change": self.requires_observed_change,
            "target_identity": list(self.target_identity) if self.target_identity else None,
            "target_title": self.target_title,
        }


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: VerificationStatus
    expectation: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "expectation": self.expectation,
            "reason": self.reason,
            "evidence": self.evidence,
            "confidence": self.confidence,
        }


class Verifier:
    """Checks expectations against fresh observations."""

    def verify(self, expectation: Expectation, ui_state: UiState | None, *, before_state: UiState | None = None) -> VerificationResult:
        evidence: dict[str, Any] = {"source": ui_state.source if ui_state else "none"}
        haystack_parts: list[str] = []
        url = ""
        element_count = 0

        if ui_state is not None:
            haystack_parts.append(ui_state.raw_text)
            haystack_parts.extend(element.text for element in ui_state.elements)
            url = ui_state.url
            element_count = len(ui_state.interactive())
            evidence["url"] = url
            evidence["interactive_elements"] = element_count
            evidence["window"] = ui_state.window
        haystack = "\n".join(part for part in haystack_parts if part).lower()

        if not haystack and not url:
            return VerificationResult(
                VerificationStatus.INCONCLUSIVE,
                expectation.description,
                "No observation was available to verify against — the verifier refuses to guess.",
                evidence,
                confidence=0.0,
            )

        # Absence checks first: a visible error invalidates everything else.
        for absent in expectation.ui_absent:
            if absent.lower() in haystack:
                return VerificationResult(
                    VerificationStatus.FAIL,
                    expectation.description,
                    f"Forbidden text is visible: {absent!r}",
                    evidence,
                    confidence=0.9,
                )

        # D12: an action on an objective-named window is verified against THAT
        # window. A fresh observation whose bound target is missing, ambiguous or
        # a different handle/pid means the input may have gone elsewhere, and a
        # successful API return cannot override that.
        if expectation.target_identity is not None:
            target = (ui_state.metadata or {}).get("target") if ui_state is not None else None
            target_dict: dict[str, Any] = target if isinstance(target, dict) else {}
            raw_ref = target_dict.get("ref")
            ref: dict[str, Any] = raw_ref if isinstance(raw_ref, dict) else {}
            try:
                observed_identity = (int(ref.get("handle") or 0), int(ref.get("pid") or 0))
            except (TypeError, ValueError):
                observed_identity = (0, 0)
            evidence["target"] = target_dict or None
            same_target = (
                target_dict.get("status") == "bound"
                and observed_identity == tuple(expectation.target_identity)
            )
            if not same_target:
                intended = expectation.target_title or "the intended window"
                return VerificationResult(
                    VerificationStatus.FAIL,
                    expectation.description,
                    (
                        f"The intended target {intended!r} "
                        f"(handle/pid {tuple(expectation.target_identity)}) is not the "
                        f"window observed after the action (status "
                        f"{target_dict.get('status')!r}, handle/pid {observed_identity}). "
                        "The input cannot be attributed to the intended application."
                    ),
                    evidence,
                    confidence=0.95,
                )

        # Presence checks.
        missing = [needle for needle in expectation.ui_contains if needle.lower() not in haystack]
        if expectation.ui_contains and missing:
            return VerificationResult(
                VerificationStatus.FAIL,
                expectation.description,
                f"Expected text not found in the UI: {missing!r}",
                evidence,
                confidence=0.85,
            )

        if expectation.url_contains and expectation.url_contains.lower() not in url.lower():
            return VerificationResult(
                VerificationStatus.FAIL,
                expectation.description,
                f"Expected URL fragment {expectation.url_contains!r}, current URL is {url!r}",
                evidence,
                confidence=0.9,
            )

        if expectation.min_elements and element_count < expectation.min_elements:
            return VerificationResult(
                VerificationStatus.FAIL,
                expectation.description,
                f"Expected at least {expectation.min_elements} interactive elements, saw {element_count}",
                evidence,
                confidence=0.7,
            )

        if expectation.requires_observed_change:
            if before_state is None:
                return VerificationResult(
                    VerificationStatus.INCONCLUSIVE,
                    expectation.description,
                    "Change verification requested but no before-state was captured.",
                    evidence,
                    confidence=0.0,
                )
            diff = ui_state.diff(before_state) if ui_state else {}
            changed = bool(diff.get("added")) or diff.get("url_changed") or diff.get("window_changed")
            evidence["diff"] = diff
            if not changed:
                return VerificationResult(
                    VerificationStatus.FAIL,
                    expectation.description,
                    "The UI did not change after the action — the action likely had no effect.",
                    evidence,
                    confidence=0.8,
                )

        confidence = 0.9 if (expectation.ui_contains or expectation.url_contains) else 0.6
        return VerificationResult(
            VerificationStatus.PASS,
            expectation.description,
            "Expected state was observed in the environment.",
            evidence,
            confidence=confidence,
        )


def expectation_from_text(description: str, text: str) -> Expectation:
    """Derive a pragmatic expectation from the step description itself: the
    meaningful words of the step become the UI needles. Deliberately shallow —
    planners attach precise expectations when they have them."""
    words = re.findall(r"[a-zA-Z0-9]{4,}", text)
    stop = {"that", "this", "with", "from", "then", "after", "open", "the"}
    needles = tuple(word.lower() for word in words if word.lower() not in stop)[:3]
    return Expectation(description=description, ui_contains=needles, requires_observed_change=False)
