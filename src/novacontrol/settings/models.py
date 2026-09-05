"""User settings models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ApprovalMode(StrEnum):
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class UserSettings:
    approval_mode: ApprovalMode = ApprovalMode.ASK
    detailed_explanations: bool = True
    include_videos_in_explore: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_mode": self.approval_mode.value,
            "detailed_explanations": self.detailed_explanations,
            "include_videos_in_explore": self.include_videos_in_explore,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "UserSettings":
        if not payload:
            return cls()
        return cls(
            approval_mode=ApprovalMode(str(payload.get("approval_mode", ApprovalMode.ASK.value))),
            detailed_explanations=bool(payload.get("detailed_explanations", True)),
            include_videos_in_explore=bool(payload.get("include_videos_in_explore", True)),
        )
