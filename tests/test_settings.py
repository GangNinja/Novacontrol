from __future__ import annotations

import unittest

from novacontrol.settings import ApprovalMode, SettingsManager, UserSettings


class SettingsTests(unittest.TestCase):
    def test_settings_round_trip(self) -> None:
        settings = UserSettings(approval_mode=ApprovalMode.DENY, include_videos_in_explore=False)

        restored = UserSettings.from_dict(settings.to_dict())

        self.assertEqual(restored.approval_mode, ApprovalMode.DENY)
        self.assertFalse(restored.include_videos_in_explore)

    def test_settings_manager_updates_values(self) -> None:
        manager = SettingsManager()

        updated = manager.update(approval_mode=ApprovalMode.DENY, detailed_explanations=False)

        self.assertEqual(updated.approval_mode, ApprovalMode.DENY)
        self.assertFalse(updated.detailed_explanations)


if __name__ == "__main__":
    unittest.main()
