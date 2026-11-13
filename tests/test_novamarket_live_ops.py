"""Focused tests for the live NSE/BSE universe and notification runtime."""

from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from novacontrol.novamarket.config import NovaMarketConfig
from novacontrol.novamarket.marketdata.models import Instrument
from novacontrol.novamarket.marketdata.universe import (
    build_indian_universe,
    parse_nse_equity_csv,
)
from novacontrol.novamarket.notifications import (
    EmailTransportConfig,
    NotificationCenter,
    market_session_snapshot,
)

NSE_SAMPLE = """SYMBOL,NAME OF COMPANY,SERIES,DATE OF LISTING,PAID UP VALUE,MARKET LOT,ISIN NUMBER,FACE VALUE
RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,250,INE002A01018,10
M&M,Mahindra & Mahindra Limited,EQ,29-NOV-1995,5,700,INE101A01026,5
INFY,Infosys Limited,BE,08-FEB-1995,5,400,INE009A01021,5
"""


class UniverseReaderTests(unittest.TestCase):
    def test_nse_parser_keeps_only_active_equity_rows_and_allows_ampersand(self) -> None:
        rows = parse_nse_equity_csv(NSE_SAMPLE)
        self.assertEqual([row.symbol for row in rows], ["RELIANCE", "M&M"])
        self.assertEqual(rows[1].provider_symbol, "M&M.NS")
        instrument = Instrument.create(
            symbol="M&M",
            name="Mahindra & Mahindra Limited",
            exchange="NSE",
            mic="XNSE",
            currency="INR",
            timezone="Asia/Kolkata",
            provider_symbols={"yahoo": "M&M.NS"},
        )
        self.assertEqual(instrument.instrument_id, "XNSE:M&M")

    def test_bse_cross_listings_are_built_when_the_bse_catalogue_is_unavailable(self) -> None:
        plan = build_indian_universe(nse_text=NSE_SAMPLE, bse_text="[]")
        self.assertEqual(plan.nse_count, 2)
        self.assertEqual(plan.bse_count, 2)
        self.assertEqual(
            {(row.mic, row.symbol) for row in plan.members},
            {
                ("XNSE", "RELIANCE"),
                ("XNSE", "M&M"),
                ("XBOM", "RELIANCE"),
                ("XBOM", "M&M"),
            },
        )
        self.assertIn("BSE catalogue unavailable", plan.errors[0])
        self.assertIn("cross-listings", plan.coverage)


class SessionSnapshotTests(unittest.TestCase):
    def test_the_snapshot_names_the_exchange_and_the_session(self) -> None:
        moment = datetime(2026, 11, 4, 9, 6, tzinfo=ZoneInfo("Asia/Kolkata"))
        snapshot = market_session_snapshot(moment)
        self.assertEqual(snapshot["exchange"], "NSE/BSE")
        self.assertEqual(snapshot["state"], "pre_open")
        self.assertEqual(snapshot["label"], "NSE/BSE STOCK MARKET: PRE-OPEN")
        self.assertTrue(str(snapshot["next_open"]).startswith("2026-11-04T09:15:00"))


class FakeSender:
    configured = True

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def describe(self) -> str:
        return "fake SMTP"

    def send(self, recipient: str, subject: str, body: str) -> dict[str, object]:
        self.messages.append((recipient, subject, body))
        return {"ok": True, "detail": "delivered", "sent_at": "2026-11-04T03:36:00+00:00"}


class FakeService:
    state = SimpleNamespace(value="running")

    def __init__(self, config: NovaMarketConfig) -> None:
        self.config = config

    def portfolios_list(self, *, limit: int = 50) -> dict[str, object]:
        return {"portfolios": [{"portfolio_id": "pf-1"}]}

    def portfolio_detail(self, portfolio_id: str) -> dict[str, object]:
        self.assertion = portfolio_id
        return {
            "holdings": [
                {
                    "instrument_id": "XNSE:RELIANCE",
                    "average_cost": "100.00",
                    "last_price": "110.00",
                    "stale": False,
                }
            ]
        }


class NotificationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_open_and_profit_threshold_send_real_messages(self) -> None:
        sender = FakeSender()
        config = NovaMarketConfig(
            notification_email="ops@example.test",
            profit_alert_pct=5.0,
            loss_alert_pct=3.0,
            alert_lead_minutes=10,
        )
        center = NotificationCenter(
            FakeService(config),
            sender=sender,
            clock=lambda: datetime(2026, 11, 4, 9, 6, tzinfo=ZoneInfo("Asia/Kolkata")),
        )
        result = await center.tick()
        self.assertTrue(result["ok"])
        subjects = [message[1] for message in sender.messages]
        self.assertEqual(len(subjects), 2)
        self.assertTrue(any("opens in" in subject for subject in subjects))
        self.assertTrue(any("profit alert" in subject for subject in subjects))
        self.assertTrue(all(message[0] == "ops@example.test" for message in sender.messages))

    async def test_pause_skips_without_sending(self) -> None:
        sender = FakeSender()
        center = NotificationCenter(
            FakeService(
                NovaMarketConfig(
                    notification_email="ops@example.test",
                    paused=True,
                )
            ),
            sender=sender,
        )
        result = await center.tick()
        self.assertEqual(result["skipped"], "runtime paused")
        self.assertEqual(sender.messages, [])

    def test_email_transport_reads_the_server_environment(self) -> None:
        config = EmailTransportConfig.from_environment(
            {
                "NOVAMARKET_SMTP_HOST": "smtp.example.test",
                "NOVAMARKET_SMTP_PORT": "465",
                "NOVAMARKET_SMTP_FROM": "desk@example.test",
                "NOVAMARKET_SMTP_TLS": "false",
            }
        )
        self.assertTrue(config.configured)
        self.assertEqual(config.port, 465)
        self.assertFalse(config.use_tls)


if __name__ == "__main__":
    unittest.main()
