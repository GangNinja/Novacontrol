"""Unified UI state representation.

The three perception layers (visual, structured, semantic) are fused into this
one structure, which the Planner, Action Engine, and Verifier all consume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class UiElement:
    """One interactive element, semantically identified when possible."""

    id: str
    type: str  # button | input | link | menu | tab | checkbox | text | other
    text: str = ""
    enabled: bool = True
    visible: bool = True
    location: tuple[int, int] | None = None  # approximate center; coordinates are a fallback
    selector: str = ""  # structured handle (CSS/XPath/automation id) when available
    layer: str = "structured"  # which perception layer produced it: visual | structured | semantic
    confidence: float = 0.5
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "text": self.text,
            "enabled": self.enabled,
            "visible": self.visible,
            "location": list(self.location) if self.location else None,
            "selector": self.selector,
            "layer": self.layer,
            "confidence": self.confidence,
            "attributes": self.attributes,
        }


@dataclass(frozen=True, slots=True)
class UiState:
    """The current interface, as one unified, diffable snapshot."""

    application: str
    window: str = ""
    url: str = ""
    elements: tuple[UiElement, ...] = ()
    raw_text: str = ""
    source: str = "unknown"  # browser_dom | vision | window_probe | composite
    observed_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "application": self.application,
            "window": self.window,
            "url": self.url,
            "elements": [element.to_dict() for element in self.elements],
            "raw_text": self.raw_text[:4000],
            "source": self.source,
            "observed_at": self.observed_at,
            "metadata": self.metadata,
        }

    def find(self, needle: str) -> UiElement | None:
        """Best visible element whose text or selector contains the needle
        (case-insensitive). Semantic lookup by name, not by coordinates."""
        lowered = needle.strip().lower()
        if not lowered:
            return None
        candidates = [
            element
            for element in self.elements
            if element.visible and lowered in element.text.lower()
        ]
        if not candidates:
            candidates = [e for e in self.elements if e.visible and lowered in e.selector.lower()]
        if not candidates:
            return None
        candidates.sort(key=lambda e: e.confidence, reverse=True)
        return candidates[0]

    def interactive(self) -> tuple[UiElement, ...]:
        return tuple(
            element for element in self.elements if element.visible and element.enabled and element.type != "text"
        )

    def diff(self, previous: "UiState | None") -> dict[str, Any]:
        """Summarize what changed between two snapshots — the raw material for
        the Verifier's `observed_change` and the Recovery Engine's diagnosis."""
        if previous is None:
            return {"added": len(self.elements), "removed": 0, "changed_text": self.raw_text[:200]}
        prev_texts = {e.text.lower() for e in previous.elements}
        now_texts = {e.text.lower() for e in self.elements}
        added = sorted(now_texts - prev_texts)[:12]
        removed = sorted(prev_texts - now_texts)[:12]
        previous_text = previous.raw_text or ""
        current_text = self.raw_text or ""
        text_changed = previous_text != current_text
        previous_pixels = str(previous.metadata.get("screenshot_sha256", ""))
        current_pixels = str(self.metadata.get("screenshot_sha256", ""))
        return {
            "added": added,
            "removed": removed,
            "url_changed": previous.url != self.url,
            "window_changed": previous.window != self.window,
            "text_grew": len(current_text) > len(previous_text),
            "text_changed": text_changed,
            "changed_text": _changed_lines(previous_text, current_text)[:12],
            "screenshot_changed": bool(previous_pixels and current_pixels and previous_pixels != current_pixels),
            "screenshot_comparable": bool(previous_pixels and current_pixels),
            "element_count_changed": len(previous.elements) != len(self.elements),
            "loading": bool(self.metadata.get("loading", False)),
            "dialogs": list(self.metadata.get("dialogs", []) or []),
            "unresponsive_windows": list(self.metadata.get("unresponsive_windows", []) or []),
    }


def _changed_lines(before: str, after: str) -> list[str]:
    """Lines that appeared or disappeared between two observations."""
    old = {line.strip() for line in before.splitlines() if line.strip()}
    new = {line.strip() for line in after.splitlines() if line.strip()}
    changed = sorted(new - old)[:8] + sorted(old - new)[:4]
    return [line[:200] for line in changed]
