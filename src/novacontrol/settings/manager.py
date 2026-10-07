"""User settings manager."""

from __future__ import annotations

from dataclasses import replace

from novacontrol.optimization.models import ExecutionMode
from novacontrol.settings.models import (
    BRAIN_MODES,
    ApprovalMode,
    UserSettings,
    clamp_checkpoint_cap,
    clamp_record_cap,
    clamp_retention_days,
)


class SettingsManager:
    def __init__(self, settings: UserSettings | None = None) -> None:
        self._settings = settings or UserSettings()

    @property
    def settings(self) -> UserSettings:
        return self._settings

    def update(
        self,
        *,
        approval_mode: ApprovalMode | None = None,
        detailed_explanations: bool | None = None,
        include_videos_in_explore: bool | None = None,
        brain_mode: str | None = None,
        auto_approve_run: bool | None = None,
        automation_enabled: bool | None = None,
        audit_retention_days: int | None = None,
        audit_max_records: int | None = None,
        audit_redact_sensitive: bool | None = None,
        execution_mode: str | None = None,
        privacy_allow_cloud: bool | None = None,
        privacy_allow_external_search: bool | None = None,
        privacy_allow_external_tools: bool | None = None,
        privacy_allow_telemetry: bool | None = None,
        privacy_allow_remote_model: bool | None = None,
        evaluation_enabled: bool | None = None,
        evaluation_retention_days: int | None = None,
        evaluation_max_records: int | None = None,
        training_enabled: bool | None = None,
        training_dry_run: bool | None = None,
        training_max_checkpoints: int | None = None,
        training_retention_days: int | None = None,
        training_max_records: int | None = None,
        preference_enabled: bool | None = None,
        preference_dry_run: bool | None = None,
        preference_max_checkpoints: int | None = None,
        preference_retention_days: int | None = None,
        preference_max_records: int | None = None,
        rlhf_enabled: bool | None = None,
        rlhf_dry_run: bool | None = None,
        rlhf_max_checkpoints: int | None = None,
        rlhf_retention_days: int | None = None,
        rlhf_max_records: int | None = None,
        rlvr_enabled: bool | None = None,
        rlvr_dry_run: bool | None = None,
        rlvr_max_checkpoints: int | None = None,
        rlvr_retention_days: int | None = None,
        rlvr_max_records: int | None = None,
    ) -> UserSettings:
        self._settings = replace(
            self._settings,
            approval_mode=approval_mode or self._settings.approval_mode,
            detailed_explanations=self._settings.detailed_explanations
            if detailed_explanations is None
            else detailed_explanations,
            include_videos_in_explore=self._settings.include_videos_in_explore
            if include_videos_in_explore is None
            else include_videos_in_explore,
            auto_approve_run=self._settings.auto_approve_run
            if auto_approve_run is None
            else auto_approve_run,
            automation_enabled=self._settings.automation_enabled
            if automation_enabled is None
            else automation_enabled,
            # The retention numbers are validated here too, on the same rule the
            # persisted copy is read with: an out-of-range period is clamped, not
            # obeyed, because "keep nothing" is not a retention policy.
            audit_retention_days=self._settings.audit_retention_days
            if audit_retention_days is None
            else clamp_retention_days(audit_retention_days),
            audit_max_records=self._settings.audit_max_records
            if audit_max_records is None
            else clamp_record_cap(audit_max_records),
            audit_redact_sensitive=self._settings.audit_redact_sensitive
            if audit_redact_sensitive is None
            else audit_redact_sensitive,
            # An out-of-vocabulary mode falls back to auto rather than raising: a
            # stale UI value must never wedge the settings store.
            brain_mode=(brain_mode if brain_mode in BRAIN_MODES else "auto")
            if brain_mode is not None
            else self._settings.brain_mode,
            # Phase 14: the execution mode is validated the same way — an
            # unknown spelling reads as the middle mode, never as the most
            # permissive one — and a control left unset keeps its current value.
            execution_mode=self._settings.execution_mode
            if execution_mode is None
            else ExecutionMode.from_text(execution_mode).value,
            privacy_allow_cloud=self._settings.privacy_allow_cloud
            if privacy_allow_cloud is None
            else privacy_allow_cloud,
            privacy_allow_external_search=self._settings.privacy_allow_external_search
            if privacy_allow_external_search is None
            else privacy_allow_external_search,
            privacy_allow_external_tools=self._settings.privacy_allow_external_tools
            if privacy_allow_external_tools is None
            else privacy_allow_external_tools,
            privacy_allow_telemetry=self._settings.privacy_allow_telemetry
            if privacy_allow_telemetry is None
            else privacy_allow_telemetry,
            privacy_allow_remote_model=self._settings.privacy_allow_remote_model
            if privacy_allow_remote_model is None
            else privacy_allow_remote_model,
            # Phase 15: the trajectory store's switches, validated on the same
            # rule as the audit trail's retention.
            evaluation_enabled=self._settings.evaluation_enabled
            if evaluation_enabled is None
            else evaluation_enabled,
            evaluation_retention_days=self._settings.evaluation_retention_days
            if evaluation_retention_days is None
            else clamp_retention_days(evaluation_retention_days),
            evaluation_max_records=self._settings.evaluation_max_records
            if evaluation_max_records is None
            else clamp_record_cap(evaluation_max_records),
            # Phase 16: the training switches, validated on exactly the same
            # rule — a cap is clamped into range, an unreadable one keeps the
            # current value, and a flag left unset keeps its own.
            training_enabled=self._settings.training_enabled
            if training_enabled is None
            else training_enabled,
            training_dry_run=self._settings.training_dry_run
            if training_dry_run is None
            else training_dry_run,
            training_max_checkpoints=self._settings.training_max_checkpoints
            if training_max_checkpoints is None
            else clamp_checkpoint_cap(training_max_checkpoints),
            training_retention_days=self._settings.training_retention_days
            if training_retention_days is None
            else clamp_retention_days(training_retention_days),
            training_max_records=self._settings.training_max_records
            if training_max_records is None
            else clamp_record_cap(training_max_records),
            # Phase 17: the preference switches, on the same rule a third time —
            # a cap is clamped into range, and a control left unset keeps its
            # current value rather than being reset by whoever edited a form.
            preference_enabled=self._settings.preference_enabled
            if preference_enabled is None
            else preference_enabled,
            preference_dry_run=self._settings.preference_dry_run
            if preference_dry_run is None
            else preference_dry_run,
            preference_max_checkpoints=self._settings.preference_max_checkpoints
            if preference_max_checkpoints is None
            else clamp_checkpoint_cap(preference_max_checkpoints),
            preference_retention_days=self._settings.preference_retention_days
            if preference_retention_days is None
            else clamp_retention_days(preference_retention_days),
            preference_max_records=self._settings.preference_max_records
            if preference_max_records is None
            else clamp_record_cap(preference_max_records),
            # Phase 18: the RLHF switches, on the same rule again — a cap is
            # clamped into range, and a control left unset keeps its current
            # value rather than being reset by whoever edited a form.
            rlhf_enabled=self._settings.rlhf_enabled
            if rlhf_enabled is None
            else rlhf_enabled,
            rlhf_dry_run=self._settings.rlhf_dry_run
            if rlhf_dry_run is None
            else rlhf_dry_run,
            rlhf_max_checkpoints=self._settings.rlhf_max_checkpoints
            if rlhf_max_checkpoints is None
            else clamp_checkpoint_cap(rlhf_max_checkpoints),
            rlhf_retention_days=self._settings.rlhf_retention_days
            if rlhf_retention_days is None
            else clamp_retention_days(rlhf_retention_days),
            rlhf_max_records=self._settings.rlhf_max_records
            if rlhf_max_records is None
            else clamp_record_cap(rlhf_max_records),
            # Phase 19: RLVR's switches, validated like every other section's.
            # The flags that make a reward checkable rather than opinionable
            # (deterministic-only, required evidence) live in the config file
            # and its environment, where the rest of the verifier policy is.
            rlvr_enabled=self._settings.rlvr_enabled
            if rlvr_enabled is None
            else rlvr_enabled,
            rlvr_dry_run=self._settings.rlvr_dry_run
            if rlvr_dry_run is None
            else rlvr_dry_run,
            rlvr_max_checkpoints=self._settings.rlvr_max_checkpoints
            if rlvr_max_checkpoints is None
            else clamp_checkpoint_cap(rlvr_max_checkpoints),
            rlvr_retention_days=self._settings.rlvr_retention_days
            if rlvr_retention_days is None
            else clamp_retention_days(rlvr_retention_days),
            rlvr_max_records=self._settings.rlvr_max_records
            if rlvr_max_records is None
            else clamp_record_cap(rlvr_max_records),
        )
        return self._settings

    def to_dict(self) -> dict[str, object]:
        return self._settings.to_dict()

    @classmethod
    def from_dict(cls, payload: dict[str, object] | None) -> SettingsManager:
        return cls(UserSettings.from_dict(payload))
