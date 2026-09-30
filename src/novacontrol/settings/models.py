"""User settings models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from novacontrol.optimization.models import ExecutionMode

# The brain modes the Chat panel's switch (and /brain/mode) can set. Canonical
# vocabulary lives here so settings persistence, brain.set_mode, and the
# application's validation all share one definition. "cloud" selects the
# user-configured external LLM (ChatGPT/Gemini/Groq/…) configured in Settings;
# it behaves like "llm" (falling back to scratch) when none is configured.
BRAIN_MODES = ("auto", "llm", "scratch", "cloud")


class ApprovalMode(StrEnum):
    ASK = "ask"
    DENY = "deny"


#: Bounds on the audit retention settings. Both are read from a persisted file
#: a user can hand-edit, so neither is taken on trust.
MAX_AUDIT_RETENTION_DAYS = 3650
MAX_AUDIT_RECORDS = 1_000_000


def clamp_retention_days(value: Any) -> int:
    """A retention period in days; unusable input keeps the default.

    Zero or negative is preserved: it means "do not expire on age", which is a
    real choice an operator may make, and it is the CAP that still bounds the
    file in that case.
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 30
    if parsed < 0:
        return 30
    return min(parsed, MAX_AUDIT_RETENTION_DAYS)


def clamp_record_cap(value: Any) -> int:
    """A hard record cap; unusable input keeps the default, and never unbounded."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 5000
    if parsed < 1:
        return 5000
    return min(parsed, MAX_AUDIT_RECORDS)


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
    # Phase 13.1: whether scheduled work is policed at all in this installation.
    # Off means the ticker never runs a task; stored tasks stay visible, and a
    # manual run still goes through the permission layer.
    automation_enabled: bool = True
    # Phase 13.4: the audit trail's retention, as the operator sets it. Kept in
    # user settings because it is a privacy choice, not a tuning constant.
    audit_retention_days: int = 30
    audit_max_records: int = 5000
    audit_redact_sensitive: bool = True
    # Phase 14.2/14.3: the execution mode and the privacy controls, kept in
    # user settings because they are the operator's choices about this machine
    # — not tuning constants. The defaults are the permissive-but-safe middle:
    # BALANCED, every control on, and the policy still refuses anything the
    # operator switches off.
    execution_mode: str = "balanced"
    privacy_allow_cloud: bool = True
    privacy_allow_external_search: bool = True
    privacy_allow_external_tools: bool = True
    privacy_allow_telemetry: bool = True
    privacy_allow_remote_model: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_mode": self.approval_mode.value,
            "detailed_explanations": self.detailed_explanations,
            "include_videos_in_explore": self.include_videos_in_explore,
            "brain_mode": self.brain_mode,
            "auto_approve_run": self.auto_approve_run,
            "automation_enabled": self.automation_enabled,
            "audit_retention_days": self.audit_retention_days,
            "audit_max_records": self.audit_max_records,
            "audit_redact_sensitive": self.audit_redact_sensitive,
            "execution_mode": self.execution_mode,
            "privacy_allow_cloud": self.privacy_allow_cloud,
            "privacy_allow_external_search": self.privacy_allow_external_search,
            "privacy_allow_external_tools": self.privacy_allow_external_tools,
            "privacy_allow_telemetry": self.privacy_allow_telemetry,
            "privacy_allow_remote_model": self.privacy_allow_remote_model,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> UserSettings:
        if not payload:
            return cls()
        brain_mode = str(payload.get("brain_mode", "auto"))
        return cls(
            approval_mode=ApprovalMode(str(payload.get("approval_mode", ApprovalMode.ASK.value))),
            detailed_explanations=bool(payload.get("detailed_explanations", True)),
            include_videos_in_explore=bool(payload.get("include_videos_in_explore", True)),
            brain_mode=brain_mode if brain_mode in BRAIN_MODES else "auto",
            auto_approve_run=bool(payload.get("auto_approve_run", False)),
            automation_enabled=bool(payload.get("automation_enabled", True)),
            # A retention setting is a NUMBER, and an unusable one keeps the
            # default rather than becoming "delete everything": the value is read
            # back from a file a user can edit, so it cannot be trusted.
            audit_retention_days=clamp_retention_days(payload.get("audit_retention_days", 30)),
            audit_max_records=clamp_record_cap(payload.get("audit_max_records", 5000)),
            audit_redact_sensitive=bool(payload.get("audit_redact_sensitive", True)),
            # An unknown mode reads as BALANCED rather than as the most
            # permissive spelling, and a missing control keeps its default.
            execution_mode=ExecutionMode.from_text(
                payload.get("execution_mode", "balanced")
            ).value,
            privacy_allow_cloud=bool(payload.get("privacy_allow_cloud", True)),
            privacy_allow_external_search=bool(
                payload.get("privacy_allow_external_search", True)
            ),
            privacy_allow_external_tools=bool(
                payload.get("privacy_allow_external_tools", True)
            ),
            privacy_allow_telemetry=bool(payload.get("privacy_allow_telemetry", True)),
            privacy_allow_remote_model=bool(
                payload.get("privacy_allow_remote_model", True)
            ),
        )
