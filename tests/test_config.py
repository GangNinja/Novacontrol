from __future__ import annotations

import os
import unittest

from novacontrol.core.config import NovaControlConfig


class ConfigTests(unittest.TestCase):
    def test_mapping_loads_module_settings(self) -> None:
        config = NovaControlConfig.from_mapping(
            {
                "app": {"environment": "test"},
                "modules": {"memory": {"enabled": False, "backend": "sqlite"}},
            }
        )

        self.assertEqual(config.app.environment, "test")
        self.assertFalse(config.modules["memory"].enabled)
        self.assertEqual(config.modules["memory"].options["backend"], "sqlite")

    def test_environment_overrides_defaults(self) -> None:
        old_env = os.environ.copy()
        try:
            os.environ["NOVACONTROL_ENV"] = "ci"
            os.environ["NOVACONTROL_REQUIRE_APPROVAL"] = "false"

            config = NovaControlConfig.from_environment()

            self.assertEqual(config.app.environment, "ci")
            self.assertFalse(config.security.require_approval_for_sensitive_actions)
        finally:
            os.environ.clear()
            os.environ.update(old_env)


if __name__ == "__main__":
    unittest.main()
