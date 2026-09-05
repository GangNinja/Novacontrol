"""User settings manager."""

from __future__ import annotations

from dataclasses import replace

from novacontrol.settings.models import ApprovalMode, UserSettings


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
        )
        return self._settings

    def to_dict(self) -> dict[str, object]:
        return self._settings.to_dict()

    @classmethod
    def from_dict(cls, payload: dict[str, object] | None) -> "SettingsManager":
        return cls(UserSettings.from_dict(payload))
