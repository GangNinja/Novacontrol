"""Brain domain models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class BrainIntent(StrEnum):
    CHAT = "chat"
    EXPLORE = "explore"
    PLAN = "plan"
    AGENT = "agent"
    SELF_IMPROVEMENT = "self_improvement"
    DESKTOP_AUTOMATION = "desktop_automation"
    PHONE_CONTROL = "phone_control"
    BROWSER_AUTOMATION = "browser_automation"
    MEMORY = "memory"
    PROJECT = "project"
    CLARIFY = "clarify"


@dataclass(frozen=True, slots=True)
class BrainRequest:
    text: str
    context: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True, slots=True)
class BrainDecision:
    intent: BrainIntent
    reason: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "reason": self.reason,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class BrainResponse:
    decision: BrainDecision
    payload: dict[str, Any]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.to_dict(),
            "payload": self.payload,
            "summary": self.summary,
        }
