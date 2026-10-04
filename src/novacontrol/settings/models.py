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


#: How many training checkpoints a user setting may keep. The ceiling matches
#: ``novacontrol.training.config.MAX_CHECKPOINTS``; kept here so the settings
#: layer does not import the training subsystem.
MAX_TRAINING_CHECKPOINTS = 100


def clamp_checkpoint_cap(value: Any) -> int:
    """How many checkpoints to keep; unusable input keeps the default (3).

    Zero is not a retention policy here: "keep no checkpoints" would silently
    make every run unresumable, so it reads as the default rather than as an
    instruction to delete a run's only way back.
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 3
    if parsed < 1:
        return 3
    return min(parsed, MAX_TRAINING_CHECKPOINTS)


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
    # Phase 15: whether this installation keeps trajectories at all, and how
    # long it keeps them. On by default — the phase exists to collect the data
    # a future learning phase needs — and switchable off, because "this machine
    # records what it did" must be a choice the operator can decline.
    evaluation_enabled: bool = True
    evaluation_retention_days: int = 30
    evaluation_max_records: int = 2000
    # Phase 16: whether this installation offers supervised fine-tuning, and
    # whether a run actually trains. On and dry-run by default: the surface is
    # available while nothing spends hours on the CPU until the operator says
    # so — and "do not train on my machine" stays one switch away.
    training_enabled: bool = True
    training_dry_run: bool = True
    training_max_checkpoints: int = 3
    training_retention_days: int = 30
    training_max_records: int = 2000
    # Phase 17: whether this installation offers preference optimization
    # (DPO/ORPO) at all, and whether such a run actually trains. The same shape
    # as the training switches and separate from them, because a person may be
    # willing to fine-tune on data they verified and unwilling to optimise a
    # model toward preferences — or the reverse.
    preference_enabled: bool = True
    preference_dry_run: bool = True
    preference_max_checkpoints: int = 3
    preference_retention_days: int = 30
    preference_max_records: int = 2000
    # Phase 18: whether this installation offers RLHF/RLAIF at all. The same
    # shape once more, and separate for the strongest version of the same
    # reason: only a mock policy optimizer ships with the phase, so an operator
    # may well want the pipeline planned and simulated here while refusing a
    # real run — and this switch is how they say so without disabling anything
    # else they use.
    rlhf_enabled: bool = True
    rlhf_dry_run: bool = True
    rlhf_max_checkpoints: int = 3
    rlhf_retention_days: int = 30
    rlhf_max_records: int = 2000

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
            "evaluation_enabled": self.evaluation_enabled,
            "evaluation_retention_days": self.evaluation_retention_days,
            "evaluation_max_records": self.evaluation_max_records,
            "training_enabled": self.training_enabled,
            "training_dry_run": self.training_dry_run,
            "training_max_checkpoints": self.training_max_checkpoints,
            "training_retention_days": self.training_retention_days,
            "training_max_records": self.training_max_records,
            "preference_enabled": self.preference_enabled,
            "preference_dry_run": self.preference_dry_run,
            "preference_max_checkpoints": self.preference_max_checkpoints,
            "preference_retention_days": self.preference_retention_days,
            "preference_max_records": self.preference_max_records,
            "rlhf_enabled": self.rlhf_enabled,
            "rlhf_dry_run": self.rlhf_dry_run,
            "rlhf_max_checkpoints": self.rlhf_max_checkpoints,
            "rlhf_retention_days": self.rlhf_retention_days,
            "rlhf_max_records": self.rlhf_max_records,
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
            # The evaluation store is a file a user can hand-edit, so its
            # retention is read on the same rule the audit trail's is: an
            # unreadable period keeps the default rather than becoming forever.
            evaluation_enabled=bool(payload.get("evaluation_enabled", True)),
            evaluation_retention_days=clamp_retention_days(
                payload.get("evaluation_retention_days", 30)
            ),
            evaluation_max_records=clamp_record_cap(
                payload.get("evaluation_max_records", 2000)
            ),
            # Phase 16: the same rule again. The training store and the
            # checkpoint count are read from a file a user can edit, so an
            # unreadable value keeps the default instead of becoming "no
            # checkpoints" or "keep everything forever".
            training_enabled=bool(payload.get("training_enabled", True)),
            training_dry_run=bool(payload.get("training_dry_run", True)),
            training_max_checkpoints=clamp_checkpoint_cap(
                payload.get("training_max_checkpoints", 3)
            ),
            training_retention_days=clamp_retention_days(
                payload.get("training_retention_days", 30)
            ),
            training_max_records=clamp_record_cap(
                payload.get("training_max_records", 2000)
            ),
            # Phase 17: the preference switches, on the same rule once more.
            # "Do not optimise my machine's model" is a switch, and an
            # unreadable cap keeps its default instead of becoming zero.
            preference_enabled=bool(payload.get("preference_enabled", True)),
            preference_dry_run=bool(payload.get("preference_dry_run", True)),
            preference_max_checkpoints=clamp_checkpoint_cap(
                payload.get("preference_max_checkpoints", 3)
            ),
            preference_retention_days=clamp_retention_days(
                payload.get("preference_retention_days", 30)
            ),
            preference_max_records=clamp_record_cap(
                payload.get("preference_max_records", 2000)
            ),
            # Phase 18: the RLHF switches, on the same rule once more. An
            # unreadable cap keeps its default rather than becoming zero, and
            # "do not run reinforcement learning on my machine" is one switch.
            rlhf_enabled=bool(payload.get("rlhf_enabled", True)),
            rlhf_dry_run=bool(payload.get("rlhf_dry_run", True)),
            rlhf_max_checkpoints=clamp_checkpoint_cap(
                payload.get("rlhf_max_checkpoints", 3)
            ),
            rlhf_retention_days=clamp_retention_days(
                payload.get("rlhf_retention_days", 30)
            ),
            rlhf_max_records=clamp_record_cap(
                payload.get("rlhf_max_records", 2000)
            ),
        )
