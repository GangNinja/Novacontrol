"""NM-08 persistence: decisions, alerts, policies, overrides and stress runs.

Risk decisions are append-only. A decision is never edited or deleted; a
correction is a new decision, and an override or consumption is a separate row
that points at it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable

from novacontrol.novamarket.risk.models import (
    AlertStatus,
    RiskAlert,
    RiskDecision,
    RiskOverride,
    RiskPolicy,
    Severity,
    utcnow,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS risk_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_policies (
    policy_id TEXT NOT NULL,
    version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL,
    PRIMARY KEY (policy_id, version)
);
CREATE TABLE IF NOT EXISTS risk_decisions (
    decision_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    portfolio_id TEXT NOT NULL DEFAULT '',
    proposal_id TEXT NOT NULL DEFAULT '',
    instrument_id TEXT NOT NULL DEFAULT '',
    policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    state_version TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_decisions_portfolio
    ON risk_decisions(portfolio_id, created_at);
CREATE TABLE IF NOT EXISTS risk_alerts (
    alert_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    portfolio_id TEXT NOT NULL DEFAULT '',
    instrument_id TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_alerts_status
    ON risk_alerts(status, created_at);
CREATE TABLE IF NOT EXISTS risk_overrides (
    override_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_consumptions (
    decision_id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    result TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_stress (
    stress_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    portfolio_id TEXT NOT NULL,
    scenario TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS risk_decisions_immutable_update
BEFORE UPDATE ON risk_decisions
BEGIN
    SELECT RAISE(ABORT, 'risk decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS risk_decisions_immutable_delete
BEFORE DELETE ON risk_decisions
BEGIN
    SELECT RAISE(ABORT, 'risk decisions are immutable');
END;
"""


class RiskStore:
    """SQLite-backed risk history. One connection, guarded by an RLock."""

    def __init__(
        self, path: str = ":memory:", *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.path = str(path)
        self.schema_version = SCHEMA_VERSION
        self._clock = clock or utcnow
        self._lock = threading.RLock()
        self._depth = 0
        self._connection = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._connection.execute(
                "INSERT OR IGNORE INTO risk_meta(key, value) VALUES(?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self._connection.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            finally:
                self._depth = 0

    # -- policies -------------------------------------------------------------

    def save_policy(self, policy: RiskPolicy, *, actor: str = "") -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT OR IGNORE INTO risk_policies(policy_id, version, created_at, "
                "actor, payload) VALUES (?,?,?,?,?)",
                (
                    policy.policy_id,
                    policy.version,
                    self._clock().isoformat(),
                    actor,
                    json.dumps(policy.to_dict(), sort_keys=True),
                ),
            )

    def latest_policy(self, policy_id: str) -> RiskPolicy | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM risk_policies WHERE policy_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (policy_id,),
            ).fetchone()
        if row is None:
            return None
        body = json.loads(row["payload"])
        return _policy_from_dict(body)

    def policies(self, policy_id: str | None = None) -> list[dict[str, Any]]:
        clause = " WHERE policy_id=?" if policy_id else ""
        params = (policy_id,) if policy_id else ()
        with self._lock:
            rows = self._connection.execute(
                "SELECT policy_id, version, created_at, actor, payload FROM risk_policies"
                + clause
                + " ORDER BY created_at ASC, rowid ASC",
                params,
            ).fetchall()
        return [
            {
                "policy_id": row["policy_id"],
                "version": row["version"],
                "created_at": row["created_at"],
                "actor": row["actor"],
                **json.loads(row["payload"]),
            }
            for row in rows
        ]

    # -- decisions ------------------------------------------------------------

    def save_decision(self, decision: RiskDecision) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO risk_decisions(decision_id, created_at, action, outcome, "
                "portfolio_id, proposal_id, instrument_id, policy_id, policy_version, "
                "state_version, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision.decision_id,
                    decision.created_at.isoformat(),
                    decision.action,
                    decision.outcome.value,
                    decision.portfolio_id,
                    decision.proposal_id,
                    decision.instrument_id,
                    decision.policy_id,
                    decision.policy_version,
                    decision.state_version,
                    json.dumps(decision.to_dict(), sort_keys=True),
                ),
            )

    def decision(self, decision_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM risk_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def decisions(
        self,
        *,
        portfolio_id: str = "",
        outcome: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if portfolio_id:
            clauses.append("portfolio_id=?")
            params.append(portfolio_id)
        if outcome:
            clauses.append("outcome=?")
            params.append(outcome)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM risk_decisions"
                + where
                + " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    # -- alerts ---------------------------------------------------------------

    def save_alert(self, alert: RiskAlert) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO risk_alerts(alert_id, dedupe_key, created_at, updated_at, "
                "status, severity, category, portfolio_id, instrument_id, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    alert.alert_id,
                    alert.dedupe_key,
                    alert.created_at.isoformat(),
                    alert.updated_at.isoformat(),
                    alert.status.value,
                    alert.severity.value,
                    alert.category.value,
                    alert.portfolio_id,
                    alert.instrument_id,
                    json.dumps(alert.to_dict(), sort_keys=True),
                ),
            )

    def open_alert(self, dedupe_key: str) -> dict[str, Any] | None:
        """Return an alert that is still active (open or acknowledged)."""
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM risk_alerts WHERE dedupe_key=? "
                "AND status<>? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (dedupe_key, AlertStatus.RESOLVED.value),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def alerts(self, *, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        clause = " WHERE status=?" if status else ""
        params: tuple[Any, ...] = (
            (status, max(1, min(int(limit), 500)))
            if status
            else (max(1, min(int(limit), 500)),)
        )
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM risk_alerts"
                + clause
                + " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                params,
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def set_alert_status(self, alert_id: str, status: AlertStatus) -> dict[str, Any] | None:
        now = self._clock().isoformat()
        with self.transaction():
            row = self._connection.execute(
                "SELECT payload FROM risk_alerts WHERE alert_id=?", (alert_id,)
            ).fetchone()
            if row is None:
                return None
            body = json.loads(row["payload"])
            body["status"] = status.value
            body["updated_at"] = now
            self._connection.execute(
                "UPDATE risk_alerts SET status=?, updated_at=?, payload=? "
                "WHERE alert_id=?",
                (status.value, now, json.dumps(body, sort_keys=True), alert_id),
            )
        return dict(body)

    # -- overrides / consumption ---------------------------------------------

    def save_override(self, override: RiskOverride) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO risk_overrides(override_id, decision_id, created_at, "
                "expires_at, actor, payload) VALUES (?,?,?,?,?,?)",
                (
                    override.override_id,
                    override.decision_id,
                    override.created_at.isoformat(),
                    override.expires_at.isoformat(),
                    override.actor,
                    json.dumps(override.to_dict(), sort_keys=True),
                ),
            )

    def overrides(self, *, decision_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        clause = " WHERE decision_id=?" if decision_id else ""
        params: tuple[Any, ...] = (
            (decision_id, max(1, min(int(limit), 500)))
            if decision_id
            else (max(1, min(int(limit), 500)),)
        )
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM risk_overrides"
                + clause
                + " ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def active_override(self, decision_id: str) -> dict[str, Any] | None:
        for row in self.overrides(decision_id=decision_id, limit=20):
            try:
                if datetime.fromisoformat(str(row["expires_at"])) > self._clock():
                    return row
            except ValueError:
                continue
        return None

    def record_consumption(
        self, decision_id: str, *, result: str, payload: dict[str, Any]
    ) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT OR IGNORE INTO risk_consumptions(decision_id, applied_at, "
                "result, payload) VALUES (?,?,?,?)",
                (
                    decision_id,
                    self._clock().isoformat(),
                    result,
                    json.dumps(payload, sort_keys=True),
                ),
            )

    def consumption(self, decision_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT applied_at, result, payload FROM risk_consumptions "
                "WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "decision_id": decision_id,
            "applied_at": row["applied_at"],
            "result": row["result"],
            "payload": json.loads(row["payload"]),
        }

    # -- stress ---------------------------------------------------------------

    def save_stress(self, stress_id: str, portfolio_id: str, scenario: str, payload: dict[str, Any]) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO risk_stress(stress_id, created_at, portfolio_id, scenario, "
                "payload) VALUES (?,?,?,?,?)",
                (
                    stress_id,
                    self._clock().isoformat(),
                    portfolio_id,
                    scenario,
                    json.dumps(payload, sort_keys=True),
                ),
            )

    def stress_results(
        self, *, portfolio_id: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        clause = " WHERE portfolio_id=?" if portfolio_id else ""
        params: tuple[Any, ...] = (
            (portfolio_id, max(1, min(int(limit), 200)))
            if portfolio_id
            else (max(1, min(int(limit), 200)),)
        )
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM risk_stress"
                + clause
                + " ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    # -- observability --------------------------------------------------------

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            decisions = self._connection.execute(
                "SELECT outcome, COUNT(*) AS n FROM risk_decisions GROUP BY outcome"
            ).fetchall()
            alerts = self._connection.execute(
                "SELECT status, severity, COUNT(*) AS n FROM risk_alerts "
                "GROUP BY status, severity"
            ).fetchall()
            counts = {
                "decisions": self._connection.execute(
                    "SELECT COUNT(*) AS n FROM risk_decisions"
                ).fetchone()["n"],
                "alerts": self._connection.execute(
                    "SELECT COUNT(*) AS n FROM risk_alerts"
                ).fetchone()["n"],
                "open_alerts": self._connection.execute(
                    "SELECT COUNT(*) AS n FROM risk_alerts WHERE status=?",
                    (AlertStatus.OPEN.value,),
                ).fetchone()["n"],
                "overrides": self._connection.execute(
                    "SELECT COUNT(*) AS n FROM risk_overrides"
                ).fetchone()["n"],
                "stress_runs": self._connection.execute(
                    "SELECT COUNT(*) AS n FROM risk_stress"
                ).fetchone()["n"],
            }
        return {
            **counts,
            "by_outcome": {row["outcome"]: row["n"] for row in decisions},
            "alerts_by_status": {
                f"{row['status']}:{row['severity']}": row["n"] for row in alerts
            },
        }


def _policy_from_dict(body: dict[str, Any]) -> RiskPolicy:
    from decimal import Decimal

    return RiskPolicy(
        policy_id=str(body["policy_id"]),
        version=str(body["version"]),
        label=str(body.get("label") or body["policy_id"]),
        max_position_weight=Decimal(str(body["max_position_weight"])),
        max_sector_weight=Decimal(str(body["max_sector_weight"])),
        max_invested_fraction=Decimal(str(body["max_invested_fraction"])),
        min_cash_fraction=Decimal(str(body["min_cash_fraction"])),
        max_drawdown_pct=Decimal(str(body["max_drawdown_pct"])),
        max_volatility_pct=Decimal(str(body["max_volatility_pct"])),
        min_liquidity_value=Decimal(str(body["min_liquidity_value"])),
        max_turnover=Decimal(str(body["max_turnover"])),
        max_forecast_age_hours=float(body["max_forecast_age_hours"]),
        allowed_model_states=tuple(str(item) for item in body["allowed_model_states"]),
        max_uncertainty=str(body["max_uncertainty"]),
        max_data_age_hours=float(body["max_data_age_hours"]),
        max_state_age_hours=float(body["max_state_age_hours"]),
        min_observations=int(body["min_observations"]),
        max_aggregate_sector_weight=Decimal(str(body["max_aggregate_sector_weight"])),
        restricted_instruments=tuple(
            str(item) for item in body.get("restricted_instruments", ())
        ),
        aggregate=bool(body.get("aggregate")),
    )


__all__ = ["RiskStore", "SCHEMA_VERSION"]
