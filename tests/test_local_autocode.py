from __future__ import annotations

import unittest

from novacontrol.self_improvement.local_autocode import approved_goal, approved_summary


class LocalAutoCodeTests(unittest.TestCase):
    def test_approved_goal_is_available(self) -> None:
        self.assertTrue(approved_goal())
        self.assertIn(approved_goal(), approved_summary())


if __name__ == "__main__":
    unittest.main()
