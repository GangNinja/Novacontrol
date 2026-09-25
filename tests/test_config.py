from __future__ import annotations

import os
import unittest

from novacontrol.core.config import MAX_PLAN_CYCLES, NovaControlConfig


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


class PlanningSettingsTests(unittest.TestCase):
    """The plan layer's loop bounds are configuration, not code."""

    def test_a_mapping_can_bound_the_plan_loops(self) -> None:
        config = NovaControlConfig.from_mapping(
            {"planning": {"max_cycles": 4, "max_step_attempts": 3}}
        )

        self.assertEqual(config.planning.max_cycles, 4)
        self.assertEqual(config.planning.max_step_attempts, 3)
        self.assertEqual(
            config.planning.to_mapping(), {"max_cycles": 4, "max_step_attempts": 3}
        )

    def test_environment_overrides_the_plan_loops(self) -> None:
        old_env = os.environ.copy()
        try:
            os.environ["NOVACONTROL_PLANNING_MAX_CYCLES"] = "5"
            os.environ["NOVACONTROL_PLANNING_MAX_STEP_ATTEMPTS"] = "4"

            config = NovaControlConfig.from_environment()

            self.assertEqual(config.planning.max_cycles, 5)
            self.assertEqual(config.planning.max_step_attempts, 4)
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    def test_an_unbounded_loop_is_not_a_configuration(self) -> None:
        # A ceiling that can be configured away is not a ceiling, and unusable
        # input keeps the default rather than stopping boot.
        clamped = NovaControlConfig.from_mapping({"planning": {"max_cycles": 100000}})
        unusable = NovaControlConfig.from_mapping(
            {"planning": {"max_cycles": "lots", "max_step_attempts": 0}}
        )

        self.assertEqual(clamped.planning.max_cycles, MAX_PLAN_CYCLES)
        self.assertEqual(unusable.planning.max_cycles, 2)
        self.assertEqual(unusable.planning.max_step_attempts, 2)

    def test_the_environment_ceiling_matches_the_mapping_ceiling(self) -> None:
        old_env = os.environ.copy()
        try:
            os.environ["NOVACONTROL_PLANNING_MAX_CYCLES"] = "100000"
            config = NovaControlConfig.from_environment()
            self.assertEqual(config.planning.max_cycles, MAX_PLAN_CYCLES)
        finally:
            os.environ.clear()
            os.environ.update(old_env)


if __name__ == "__main__":
    unittest.main()
