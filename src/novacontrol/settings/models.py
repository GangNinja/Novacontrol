"""User settings models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# The brain modes the Chat panel's switch (and /brain/mode) can set. Canonical
# vocabulary lives here so settings persistence, brain.set_mode, and the
# application's validation all share one definition. "cloud" selects the
# user-configured external LLM (ChatGPT/Gemini/Groq/…) configured in Settings;
# it behaves like "llm" (falling back to scratch) when none is configured.
BRAIN_MODES = ("auto", "llm", "scratch", "cloud")


class ApprovalMode(StrEnum):
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class UserSettings:
    approval_mode: ApprovalMode = ApprovalMode.ASK
    detailed_explanations: bool = True
    include_videos_in_explore: bool = True
    brain_mode: str = "auto"
    # Auto-approve & run: planned device commands execute immediately using the
    # same server-minted token path (no click). Off by default — the approval
    # gate is the safe default.
    auto_approve_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_mode": self.approval_mode.value,
            "detailed_explanations": self.detailed_explanations,
            "include_videos_in_explore": self.include_videos_in_explore,
            "brain_mode": self.brain_mode,
            "auto_approve_run": self.auto_approve_run,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "UserSettings":
        if not payload:
            return cls()
        brain_mode = str(payload.get("brain_mode", "auto"))
        return cls(
            approval_mode=ApprovalMode(str(payload.get("approval_mode", ApprovalMode.ASK.value))),
            detailed_explanations=bool(payload.get("detailed_explanations", True)),
            include_videos_in_explore=bool(payload.get("include_videos_in_explore", True)),
            brain_mode=brain_mode if brain_mode in BRAIN_MODES else "auto",
            auto_approve_run=bool(payload.get("auto_approve_run", False)),
        )
