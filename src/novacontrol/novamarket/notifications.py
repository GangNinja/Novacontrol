"""Operator email notifications for the NovaMarket runtime.

Two kinds of message leave this module, and only two:

* a **session alert** five to thirty minutes before the verified NSE/BSE open or
  close (default ten);
* a **position alert** when a held stock's unrealised move crosses the
  operator's configured profit or loss threshold.

The email transport is SMTP. Credentials come from the server environment; the
configuration stores only the recipient and the thresholds. A missing SMTP
configuration is reported as ``smtp_configured: false`` and no message is
pretended to have been sent.

The monitor is deliberately ordinary: no trade is sent, no broker is contacted,
and the paper-only boundary is restated in every message.
"""

from __future__ import annotations

import asyncio
import os
import smtplib
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from novacontrol.novamarket.marketdata.calendar import (
    SessionState,
    calendar_by_mic,
)
from novacontrol.novamarket.marketdata.session_schedule import plan_market_refresh

_SMTP_ENV = {
    "host": "NOVAMARKET_SMTP_HOST",
    "port": "NOVAMARKET_SMTP_PORT",
    "username": "NOVAMARKET_SMTP_USER",
    "password": "NOVAMARKET_SMTP_PASSWORD",
    "sender": "NOVAMARKET_SMTP_FROM",
    "tls": "NOVAMARKET_SMTP_TLS",
}


@dataclass(frozen=True, slots=True)
class EmailTransportConfig:
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    sender: str = ""
    use_tls: bool = True

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> EmailTransportConfig:
        values = os.environ if environ is None else environ
        port_text = str(values.get(_SMTP_ENV["port"], "587") or "587").strip()
        try:
            port = int(port_text)
        except ValueError:
            port = 587
        tls_text = str(values.get(_SMTP_ENV["tls"], "true") or "true").strip().lower()
        return cls(
            host=str(values.get(_SMTP_ENV["host"], "") or "").strip(),
            port=port,
            username=str(values.get(_SMTP_ENV["username"], "") or "").strip(),
            password=str(values.get(_SMTP_ENV["password"], "") or ""),
            sender=str(values.get(_SMTP_ENV["sender"], "") or "").strip(),
            use_tls=tls_text not in {"0", "false", "no", "off"},
        )

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)

    def describe(self) -> str:
        if not self.configured:
            return (
                "SMTP is not configured. Set NOVAMARKET_SMTP_HOST and "
                "NOVAMARKET_SMTP_FROM (plus user/password if required)."
            )
        return f"{self.host}:{self.port} as {self.sender}"


class SmtpEmailSender:
    """Small synchronous SMTP sender, called from a worker thread."""

    def __init__(self, config: EmailTransportConfig | None = None) -> None:
        self.config = config or EmailTransportConfig.from_environment()

    @property
    def configured(self) -> bool:
        return self.config.configured

    def describe(self) -> str:
        return self.config.describe()

    def send(self, recipient: str, subject: str, body: str) -> dict[str, Any]:
        if not self.configured:
            return {
                "ok": False,
                "detail": self.config.describe(),
                "sent_at": None,
            }
        if not recipient or "@" not in recipient:
            return {"ok": False, "detail": "a notification email is required", "sent_at": None}
        message = EmailMessage()
        message["From"] = self.config.sender
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)
        try:
            if self.config.port == 465:
                client: smtplib.SMTP = smtplib.SMTP_SSL(
                    self.config.host,
                    self.config.port,
                    timeout=20,
                    context=ssl.create_default_context(),
                )
            else:
                client = smtplib.SMTP(self.config.host, self.config.port, timeout=20)
            with client:
                client.ehlo()
                if self.config.use_tls and self.config.port != 465:
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                if self.config.username:
                    client.login(self.config.username, self.config.password)
                client.send_message(message)
        except Exception as exc:  # noqa: BLE001 - delivery failure is reported
            return {
                "ok": False,
                "detail": f"{type(exc).__name__}: {exc}",
                "sent_at": None,
            }
        return {
            "ok": True,
            "detail": f"delivered to {recipient} via {self.config.host}",
            "sent_at": datetime.now(UTC).isoformat(),
        }


def market_session_snapshot(moment: datetime | None = None) -> dict[str, Any]:
    """The NSE/BSE session, with the calendar's own reason for its answer."""
    now = moment or datetime.now(UTC)
    calendar = calendar_by_mic("XNSE")
    refresh = plan_market_refresh(now, calendar=calendar).to_dict()
    if calendar is None:
        return {
            "exchange": "NSE/BSE",
            "state": "unknown",
            "label": "NSE/BSE SESSION UNKNOWN",
            "as_of": now.isoformat(),
            "coverage": False,
            "detail": "no verified NSE/BSE calendar is installed",
            "refresh": refresh,
        }
    local = calendar.local(now)
    state = calendar.state(now)
    bounds = calendar.session_bounds(local.date())
    opening = calendar.next_session_open(now)
    closing = bounds[1] if bounds and bounds[1] > now else None
    labels = {
        SessionState.OPEN: "NSE/BSE MARKET OPEN",
        SessionState.PRE_OPEN: "NSE/BSE STOCK MARKET: PRE-OPEN",
        SessionState.CLOSED: "NSE/BSE MARKET CLOSED",
        SessionState.WEEKEND: "NSE/BSE WEEKEND — CLOSED",
        SessionState.HOLIDAY: "NSE/BSE HOLIDAY — CLOSED",
        SessionState.UNKNOWN: "NSE/BSE SESSION UNKNOWN",
        SessionState.CONTINUOUS: "NSE/BSE CONTINUOUS",
    }
    return {
        "exchange": "NSE/BSE",
        "state": state.value,
        "label": labels.get(state, "NSE/BSE SESSION UNKNOWN"),
        "as_of": now.isoformat(),
        "local_time": local.isoformat(),
        "timezone": calendar.timezone,
        "coverage": calendar.coverage(now),
        "holiday": calendar.holiday_name(local.date()),
        "next_open": opening.isoformat() if opening else None,
        "next_close": closing.isoformat() if closing else None,
        "sessions": [
            {
                "open": item.open_time.isoformat(timespec="minutes"),
                "close": item.close_time.isoformat(timespec="minutes"),
            }
            for item in calendar.sessions
        ],
        "refresh": refresh,
    }


class NotificationCenter:
    """Evaluate the clock and the books, then send what crossed a threshold."""

    def __init__(
        self,
        service: Any,
        *,
        sender: Any | None = None,
        clock: Any | None = None,
        store: Any | None = None,
        namespace: str = "",
    ) -> None:
        self._service = service
        self._sender = sender or SmtpEmailSender()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._store = store
        self._namespace = namespace
        self._state = self._load()
        self._last_tick: datetime | None = None

    @property
    def sender(self) -> Any:
        return self._sender

    def _load(self) -> dict[str, Any]:
        if self._store is None or not self._namespace:
            return {"sent": {}, "last_error": "", "last_sent_at": ""}
        try:
            loaded = self._store.read(self._namespace)
        except Exception:  # noqa: BLE001 - a notification log must not fail boot
            return {"sent": {}, "last_error": "", "last_sent_at": ""}
        sent = loaded.get("sent")
        return {
            "sent": dict(sent) if isinstance(sent, Mapping) else {},
            "last_error": str(loaded.get("last_error") or ""),
            "last_sent_at": str(loaded.get("last_sent_at") or ""),
        }

    def _save(self) -> None:
        if self._store is None or not self._namespace:
            return
        try:
            self._store.write(self._namespace, self._state)
        except Exception:  # noqa: BLE001 - delivery already happened
            pass

    def _config(self) -> Any:
        return self._service.config

    @property
    def configured(self) -> bool:
        return bool(getattr(self._sender, "configured", False))

    def status(self) -> dict[str, Any]:
        config = self._config()
        sent = self._state.get("sent") if isinstance(self._state.get("sent"), Mapping) else {}
        return {
            "enabled": bool(config.notifications_enabled),
            "email": config.notification_email,
            "smtp_configured": self.configured,
            "smtp": self._sender.describe(),
            "notify_market_open": bool(config.notify_market_open),
            "notify_market_close": bool(config.notify_market_close),
            "profit_alert_pct": config.profit_alert_pct,
            "loss_alert_pct": config.loss_alert_pct,
            "alert_lead_minutes": config.alert_lead_minutes,
            "paused": bool(config.paused),
            "last_sent_at": self._state.get("last_sent_at") or "",
            "last_error": self._state.get("last_error") or "",
            "sent_keys": len(sent) if isinstance(sent, Mapping) else 0,
        }

    def _already_sent(self, key: str) -> bool:
        sent = self._state.get("sent")
        return isinstance(sent, Mapping) and key in sent

    def _record(
        self,
        key: str,
        *,
        recipient: str,
        subject: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = self._clock().isoformat()
        sent = dict(self._state.get("sent") or {})
        sent[key] = {
            "at": now,
            "recipient": recipient,
            "subject": subject,
            "ok": bool(result.get("ok")),
            "detail": str(result.get("detail") or "")[:500],
        }
        self._state["sent"] = dict(list(sent.items())[-100:])
        if result.get("ok"):
            self._state["last_sent_at"] = now
            self._state["last_error"] = ""
        else:
            self._state["last_error"] = str(result.get("detail") or "delivery failed")
        self._save()
        return dict(result)

    async def send_test(self) -> dict[str, Any]:
        config = self._config()
        recipient = str(config.notification_email or "").strip()
        if not recipient:
            return {
                "ok": False,
                "detail": "Add a notification email in NovaMarket Settings first.",
                "sent_at": None,
            }
        if not self.configured:
            return {
                "ok": False,
                "detail": getattr(self._sender, "describe", lambda: "SMTP is not configured.")(),
                "sent_at": None,
            }
        subject = "NovaMarket test notification"
        body = (
            "This is a NovaMarket delivery test.\n\n"
            "The paper-only market desk will send NSE/BSE open/close alerts and "
            "per-stock profit/loss alerts to this address when the configured "
            "thresholds are crossed.\n\n"
            "No broker is connected and no real-money order is ever sent."
        )
        result = await asyncio.to_thread(self._sender.send, recipient, subject, body)
        return self._record(
            f"test:{self._clock().isoformat()}",
            recipient=recipient,
            subject=subject,
            result=result,
        )

    async def tick(self) -> dict[str, Any]:
        config = self._config()
        now = self._clock()
        if self._last_tick is not None and now - self._last_tick < timedelta(seconds=30):
            return {"ok": True, "skipped": "throttled"}
        self._last_tick = now
        if not config.notifications_enabled:
            return {"ok": True, "skipped": "notifications disabled"}
        if config.paused:
            return {"ok": True, "skipped": "runtime paused"}
        if not config.notification_email:
            return {"ok": True, "skipped": "no notification email"}
        if not self.configured:
            self._state["last_error"] = getattr(
                self._sender, "describe", lambda: "SMTP is not configured."
            )()
            self._save()
            return {"ok": False, "skipped": "smtp not configured"}
        if self._service.state.value not in {"running", "degraded"}:
            return {"ok": True, "skipped": "subsystem not running"}

        results: list[dict[str, Any]] = []
        session = market_session_snapshot(now)
        results.extend(await self._session_alerts(session, now))
        results.extend(await self._position_alerts(now))
        return {
            "ok": all(item.get("ok", True) for item in results),
            "sent": [item for item in results if item.get("ok")],
            "failed": [item for item in results if not item.get("ok")],
        }

    async def _send(self, key: str, subject: str, body: str) -> dict[str, Any]:
        config = self._config()
        recipient = str(config.notification_email or "").strip()
        result = await asyncio.to_thread(self._sender.send, recipient, subject, body)
        return self._record(key, recipient=recipient, subject=subject, result=result)

    async def _session_alerts(
        self, session: Mapping[str, Any], now: datetime
    ) -> list[dict[str, Any]]:
        config = self._config()
        lead = int(config.alert_lead_minutes)
        local_day = str(session.get("local_time") or now.isoformat())[:10]
        output: list[dict[str, Any]] = []

        if config.notify_market_open and session.get("next_open"):
            opening = datetime.fromisoformat(str(session["next_open"]))
            minutes = (opening - now.astimezone(opening.tzinfo)).total_seconds() / 60
            if 0 < minutes <= lead:
                key = f"session:open:{opening.date().isoformat()}"
                if not self._already_sent(key):
                    output.append(
                        await self._send(
                            key,
                            f"NovaMarket: NSE/BSE opens in {max(1, round(minutes))} min",
                            (
                                "The verified NSE/BSE session opens at "
                                f"{opening.isoformat()}.\n\n"
                                "NovaMarket is running and will keep monitoring the "
                                "paper workspace. This is a paper-only desk; no "
                                "broker is connected and no real-money order is sent."
                            ),
                        )
                    )

        if config.notify_market_close and session.get("next_close"):
            closing = datetime.fromisoformat(str(session["next_close"]))
            minutes = (closing - now.astimezone(closing.tzinfo)).total_seconds() / 60
            if 0 < minutes <= lead:
                key = f"session:close:{closing.date().isoformat()}"
                if not self._already_sent(key):
                    output.append(
                        await self._send(
                            key,
                            f"NovaMarket: NSE/BSE closes in {max(1, round(minutes))} min",
                            (
                                "The verified NSE/BSE session closes at "
                                f"{closing.isoformat()} on {local_day}.\n\n"
                                "NovaMarket will continue to hold the paper book and "
                                "send threshold alerts from the last validated prices. "
                                "No broker is connected and no real-money order is sent."
                            ),
                        )
                    )
        return output

    async def _position_alerts(self, now: datetime) -> list[dict[str, Any]]:
        config = self._config()
        day = now.astimezone(UTC).date().isoformat()
        try:
            listed = self._service.portfolios_list(limit=200)
        except Exception:  # noqa: BLE001 - an unopened book is not an alert
            return []
        output: list[dict[str, Any]] = []
        for row in listed.get("portfolios") or []:
            if not isinstance(row, Mapping):
                continue
            portfolio_id = str(row.get("portfolio_id") or "")
            if not portfolio_id:
                continue
            try:
                detail = self._service.portfolio_detail(portfolio_id)
            except Exception:  # noqa: BLE001 - one unreadable book is not all books
                continue
            for holding in detail.get("holdings") or []:
                if not isinstance(holding, Mapping) or holding.get("stale"):
                    continue
                instrument = str(holding.get("instrument_id") or "")
                if not instrument:
                    continue
                try:
                    average = Decimal(str(holding.get("average_cost") or "0"))
                    last = Decimal(str(holding.get("last_price") or "0"))
                except (InvalidOperation, ValueError):
                    continue
                if average <= 0 or last <= 0:
                    continue
                change = (last - average) / average * Decimal("100")
                direction = ""
                if change >= Decimal(str(config.profit_alert_pct)):
                    direction = "profit"
                elif change <= -Decimal(str(config.loss_alert_pct)):
                    direction = "loss"
                if not direction:
                    continue
                key = f"position:{portfolio_id}:{instrument}:{direction}:{day}"
                if self._already_sent(key):
                    continue
                signed = f"{change:+.2f}%"
                output.append(
                    await self._send(
                        key,
                        f"NovaMarket {direction} alert: {instrument} {signed}",
                        (
                            f"{instrument} in portfolio {portfolio_id} is {signed} "
                            f"against its average cost of ₹{average:.2f}; the last "
                            f"validated price is ₹{last:.2f}.\n\n"
                            f"Your configured {direction} threshold is "
                            f"{config.profit_alert_pct if direction == 'profit' else config.loss_alert_pct:.2f}%.\n\n"
                            "This is an observation from the paper workspace. No "
                            "broker is connected and no real-money order is sent."
                        ),
                    )
                )
        return output


__all__ = [
    "EmailTransportConfig",
    "NotificationCenter",
    "SmtpEmailSender",
    "market_session_snapshot",
]
