"""Pins for the one NSE/BSE refresh schedule.

The test times are UTC and correspond to Asia/Kolkata (UTC+05:30), so the
09:00/17:00 window and the 09:15/15:30 exchange session are explicit.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from novacontrol.novamarket.marketdata.calendar import calendar_by_mic
from novacontrol.novamarket.marketdata.session_schedule import (
    RefreshPhase,
    plan_market_refresh,
)
from novacontrol.novamarket.notifications import market_session_snapshot


class RefreshScheduleTests(unittest.TestCase):
    def _plan(self, moment: datetime):
        return plan_market_refresh(moment, calendar=calendar_by_mic("XNSE"))

    def test_before_nine_wakes_hourly_and_at_nine(self) -> None:
        plan = self._plan(datetime(2026, 11, 5, 2, 30, tzinfo=UTC))  # 08:00 IST

        self.assertEqual(plan.phase, RefreshPhase.OVERNIGHT)
        self.assertEqual(plan.interval_seconds, 3600)
        self.assertEqual(plan.next_check_at.astimezone(UTC), datetime(2026, 11, 5, 3, 30, tzinfo=UTC))
        self.assertFalse(plan.live)

    def test_nine_am_checks_a_pre_open_market_and_schedules_the_first_probe(self) -> None:
        plan = self._plan(datetime(2026, 11, 5, 3, 30, tzinfo=UTC))  # 09:00 IST

        self.assertEqual(plan.phase, RefreshPhase.PROBING)
        self.assertEqual(plan.session_state, "pre_open")
        self.assertEqual(plan.attempts, 1)
        self.assertEqual(plan.max_attempts, 6)
        self.assertEqual(plan.interval_seconds, 900)
        self.assertEqual(plan.next_check_at.astimezone(UTC), datetime(2026, 11, 5, 3, 45, tzinfo=UTC))

    def test_open_market_uses_the_fifteen_minute_live_cadence(self) -> None:
        plan = self._plan(datetime(2026, 11, 5, 4, 0, tzinfo=UTC))  # 09:30 IST

        self.assertEqual(plan.phase, RefreshPhase.LIVE)
        self.assertTrue(plan.live)
        self.assertEqual(plan.interval_seconds, 15 * 60)
        self.assertEqual(plan.to_dict()["interval_ms"], 15 * 60 * 1000)
        self.assertIn("delayed", plan.provider_note.lower())
        self.assertIn("15-minute", plan.provider_note)

    def test_six_closed_probes_are_exhausted_at_ten_thirty(self) -> None:
        plan = self._plan(datetime(2026, 11, 8, 5, 0, tzinfo=UTC))  # Sunday 10:30 IST

        self.assertEqual(plan.phase, RefreshPhase.HOURLY)
        self.assertEqual(plan.session_state, "weekend")
        self.assertEqual(plan.attempts, 6)
        self.assertEqual(plan.interval_seconds, 3600)
        self.assertFalse(plan.live)

    def test_exchange_close_switches_to_hourly(self) -> None:
        plan = self._plan(datetime(2026, 11, 5, 10, 0, tzinfo=UTC))  # 15:30 IST

        self.assertEqual(plan.phase, RefreshPhase.HOURLY)
        self.assertEqual(plan.session_state, "closed")
        self.assertEqual(plan.interval_seconds, 3600)

    def test_five_pm_ends_the_live_window(self) -> None:
        plan = self._plan(datetime(2026, 11, 5, 11, 30, tzinfo=UTC))  # 17:00 IST

        self.assertEqual(plan.phase, RefreshPhase.HOURLY)
        self.assertFalse(plan.live)
        self.assertIn("17:00", plan.reason)

    def test_the_session_snapshot_carries_the_same_plan(self) -> None:
        snapshot = market_session_snapshot(datetime(2026, 11, 5, 4, 0, tzinfo=UTC))

        self.assertEqual(snapshot["refresh"]["phase"], "live")
        self.assertEqual(snapshot["refresh"]["interval_ms"], 15 * 60 * 1000)


if __name__ == "__main__":
    unittest.main()
