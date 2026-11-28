"""Contextual reference resolution for computer operation.

"Click the Save button" and "continue from where you stopped" are not new
planners.  They are requests to resolve a reference against the current
observation and the persisted operation history, and then hand an ordinary
goal to the existing loop.  Ambiguity is returned as ambiguity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from novacontrol.agentcore.ui_state import UiElement, UiState

_BROWSER_WORDS = ("browser", "chrome", "edge", "firefox", "brave", "web page", "website")
_ERROR_WORDS = ("error", "failed", "failure", "exception", "not responding", "unresponsive")


@dataclass(frozen=True, slots=True)
class ReferenceResolution:
    """The result of resolving one contextual phrase."""

    text: str
    status: str  # resolved | ambiguous | unresolved
    goal: str = ""
    reason: str = ""
    evidence: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "status": self.status,
            "goal": self.goal,
            "reason": self.reason,
            "evidence": self.evidence,
        }


def _quoted_or_named(text: str) -> str:
    quoted = re.search(r"['\"]([^'\"]{1,120})['\"]", text)
    if quoted:
        return quoted.group(1).strip()
    return ""


def resolve_reference(
    text: str,
    *,
    ui_state: UiState | None = None,
    history_objective: str = "",
    history_unfinished: str = "",
) -> ReferenceResolution:
    """Resolve a contextual reference against observation and history.

    This never guesses that an absent element exists.  A reference to a missing
    target is ``unresolved`` and asks for clarification; when more than one
    plausible match exists it is ``ambiguous`` and lists them.
    """
    raw = " ".join(str(text or "").strip().split())
    lowered = raw.lower()
    if not raw:
        return ReferenceResolution(raw, "unresolved", reason="No reference was supplied.")

    if re.search(r"\b(continue|resume|pick up|from where (?:i|we) stopped)\b", lowered):
        goal = history_unfinished or history_objective
        if goal:
            return ReferenceResolution(
                raw,
                "resolved",
                goal=goal,
                reason="Resolved to the last unfinished persisted objective.",
                evidence={"history_objective": goal, "source": "computer_history"},
            )
        return ReferenceResolution(raw, "unresolved", reason="No earlier objective is recorded to continue.")

    if re.search(r"\bgo back\b|\bback to the previous page\b", lowered):
        return ReferenceResolution(
            raw,
            "resolved",
            goal="go back in the browser",
            reason="Resolved to browser history navigation.",
            evidence={"url": ui_state.url if ui_state else "", "source": "current_observation"},
        )

    if re.search(r"\berror\b|\bfailed\b|\bexception\b|\bnot responding\b", lowered) and re.search(
        r"\b(find|show|in this window|on this screen)\b", lowered
    ):
        if ui_state is None or not ui_state.raw_text.strip():
            return ReferenceResolution(raw, "unresolved", reason="There is no observation to read an error from.")
        lines = [
            " ".join(line.split())[:240]
            for line in ui_state.raw_text.splitlines()
            if any(word in line.lower() for word in _ERROR_WORDS)
        ]
        if not lines:
            return ReferenceResolution(
                raw,
                "unresolved",
                reason="The current observation contains no error text.",
                evidence={"application": ui_state.application, "window": ui_state.window},
            )
        return ReferenceResolution(
            raw,
            "resolved",
            goal=f"inspect the visible error: {lines[0]}",
            reason="Resolved to the error text visible in the current window.",
            evidence={"error": lines[0], "window": ui_state.window},
        )

    if re.search(r"\b(switch|focus|go)\s+to\s+(?:the\s+)?(?:my\s+)?browser\b", lowered):
        if ui_state is None:
            return ReferenceResolution(raw, "unresolved", reason="No current observation is available.")
        windows = tuple(str(item) for item in (ui_state.metadata.get("windows", []) or []))
        matches = [window for window in windows if any(word in window.lower() for word in _BROWSER_WORDS)]
        if not matches:
            # A browser may not have a window title containing "browser"; only
            # claim a match when the current window is already the browser.
            if any(word in (ui_state.window or "").lower() for word in _BROWSER_WORDS):
                return ReferenceResolution(raw, "resolved", goal="focus browser", reason="The active window is already the browser.")
            return ReferenceResolution(
                raw,
                "unresolved",
                reason="No visible browser window was found; start the browser first.",
                evidence={"windows": list(windows[:12])},
            )
        return ReferenceResolution(
            raw,
            "resolved",
            goal=f"focus {matches[0]}",
            reason="Resolved to a visible browser window title.",
            evidence={"window": matches[0]},
        )

    click_match = re.search(r"\bclick\s+(?:on\s+)?(?:the\s+)?(.+)", lowered)
    if click_match:
        label = _quoted_or_named(raw) or re.sub(r"^(?:please\s+)?click\s+(?:on\s+)?(?:the\s+)?", "", raw, flags=re.IGNORECASE).strip()
        label = re.sub(r"\s+(?:button|link|tab|menu item|option)$", "", label, flags=re.IGNORECASE).strip()
        if not label:
            return ReferenceResolution(raw, "unresolved", reason="The click reference names no target.")
        if ui_state is None:
            return ReferenceResolution(raw, "unresolved", reason="No current observation is available.")
        needles = [label]
        if " " in label:
            needles.extend(part for part in label.split() if len(part) > 3)
        element_matches: list[UiElement] = []
        for needle in needles:
            found = ui_state.find(needle)
            if found is not None and found.text not in {item.text for item in element_matches}:
                element_matches.append(found)
        if not element_matches:
            return ReferenceResolution(
                raw,
                "unresolved",
                reason=f"No visible element matched {label!r}; re-observe or name it differently.",
                evidence={"label": label, "window": ui_state.window},
            )
        if len(element_matches) > 1:
            return ReferenceResolution(
                raw,
                "ambiguous",
                reason=f"More than one visible element matched {label!r}.",
                evidence={"matches": [element.text for element in element_matches[:6]], "label": label},
            )
        element = element_matches[0]
        return ReferenceResolution(
            raw,
            "resolved",
            goal=f"click {element.text or label}",
            reason="Resolved to the single visible element with that label.",
            evidence={
                "element": element.to_dict(),
                "window": ui_state.window,
                "source": "current_observation",
            },
        )

    if re.search(r"\bthe file i was working on\b|\bmy (?:current|last) file\b", lowered):
        if history_objective:
            return ReferenceResolution(
                raw,
                "resolved",
                goal=history_objective,
                reason="Resolved to the objective recorded in the operation history.",
                evidence={"history_objective": history_objective},
            )
        return ReferenceResolution(
            raw,
            "unresolved",
            reason="No file or objective is recorded in the operation history; name the file.",
        )

    return ReferenceResolution(
        raw,
        "unresolved",
        reason="This is not a reference the operator can resolve from observation or history.",
    )
