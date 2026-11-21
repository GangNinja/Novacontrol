"""Isolated persistence for PI-22 risk-intelligence research records.

Only ``ri_*`` analytical tables exist here.  Portfolio, cash, ledger, risk
decisions and execution records remain in their authoritative stores.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from novacontrol.novamarket.risk_intelligence.models import (
    AnalysisStatus,
    RiskIntelligenceNotFound,
    RiskThresholds,
    parse_time,
)

SCHEMA_VERSION = 1


def _json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))


def _load(value: object) -> Any:
    if not value:
        return {}
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return {}


def _now() -> datetime:
    return datetime.now(UTC)


class RiskIntelligenceStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
        self._connection.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._connection.execute("PRAGMA journal_mode=WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ri_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ri_configs (
                    version INTEGER PRIMARY KEY,
                    digest TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS ri_assessments (
                    assessment_id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    status TEXT NOT NULL,
                    snapshot_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ri_assessments_portfolio
                    ON ri_assessments(portfolio_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS ri_position_assessments (
                    assessment_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    liquidity_class TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(assessment_id, instrument_id),
                    FOREIGN KEY(assessment_id) REFERENCES ri_assessments(assessment_id)
                );
                CREATE INDEX IF NOT EXISTS idx_ri_positions_instrument
                    ON ri_position_assessments(instrument_id, assessment_id);
                CREATE TABLE IF NOT EXISTS ri_metric_snapshots (
                    assessment_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(assessment_id, kind),
                    FOREIGN KEY(assessment_id) REFERENCES ri_assessments(assessment_id)
                );
                CREATE TABLE IF NOT EXISTS ri_scenarios (
                    scenario_id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ri_scenarios_portfolio
                    ON ri_scenarios(portfolio_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS ri_stress_runs (
                    stress_id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    scenario_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ri_stress_portfolio
                    ON ri_stress_runs(portfolio_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS ri_scenario_runs (
                    run_id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    scenario_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ri_scenario_runs_portfolio
                    ON ri_scenario_runs(portfolio_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS ri_alerts (
                    alert_id TEXT PRIMARY KEY,
                    dedupe_key TEXT NOT NULL,
                    portfolio_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ri_alerts_portfolio
                    ON ri_alerts(portfolio_id, last_seen DESC);
                CREATE INDEX IF NOT EXISTS idx_ri_alerts_dedupe
                    ON ri_alerts(dedupe_key, status, last_seen DESC);
                CREATE TABLE IF NOT EXISTS ri_recommendations (
                    recommendation_id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ri_recommendations_portfolio
                    ON ri_recommendations(portfolio_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS ri_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    portfolio_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO ri_meta(key, value, updated_at) VALUES(?,?,?)",
                ("schema_version", str(SCHEMA_VERSION), _now().isoformat()),
            )
            count = self._connection.execute("SELECT COUNT(*) n FROM ri_configs").fetchone()
            if not count or int(count["n"]) == 0:
                config = RiskThresholds()
                self._connection.execute(
                    "INSERT INTO ri_configs(version, digest, payload, created_at, active) "
                    "VALUES(?,?,?,?,1)",
                    (1, config.digest, _json(config.to_dict()), _now().isoformat()),
                )

    # -- config --------------------------------------------------------------
    def config(self) -> tuple[int, RiskThresholds]:
        row = self._connection.execute(
            "SELECT version, payload FROM ri_configs WHERE active=1 ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return 1, RiskThresholds()
        return int(row["version"]), RiskThresholds.from_mapping(_load(row["payload"]))

    def save_config(self, config: RiskThresholds) -> dict[str, Any]:
        row = self._connection.execute("SELECT MAX(version) v FROM ri_configs").fetchone()
        version = int(row["v"] or 0) + 1
        now = _now().isoformat()
        with self._connection:
            self._connection.execute("UPDATE ri_configs SET active=0")
            self._connection.execute(
                "INSERT INTO ri_configs(version, digest, payload, created_at, active) "
                "VALUES(?,?,?,?,1)",
                (version, config.digest, _json(config.to_dict()), now),
            )
        return {
            "version": version,
            "digest": config.digest,
            "created_at": now,
            "config": config.to_dict(),
        }

    def config_history(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT version, digest, payload, created_at, active FROM ri_configs "
            "ORDER BY version DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
        return [
            {
                "version": int(row["version"]),
                "digest": str(row["digest"]),
                "created_at": str(row["created_at"]),
                "active": bool(row["active"]),
                "config": _load(row["payload"]),
            }
            for row in rows
        ]

    # -- assessments ---------------------------------------------------------
    def save_assessment(
        self,
        assessment: Mapping[str, Any],
        positions: Iterable[Mapping[str, Any]],
        metric_groups: Mapping[str, Any],
    ) -> None:
        assessment_id = str(assessment.get("assessment_id") or "")
        if not assessment_id:
            raise ValueError("an assessment needs an id")
        created = str(assessment.get("generated_at") or _now().isoformat())
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO ri_assessments(assessment_id, portfolio_id, horizon, "
                "status, snapshot_version, created_at, payload) VALUES(?,?,?,?,?,?,?)",
                (
                    assessment_id,
                    str(assessment.get("portfolio_id") or ""),
                    str(assessment.get("horizon") or ""),
                    str(assessment.get("status") or ""),
                    str(assessment.get("snapshot_version") or ""),
                    created,
                    _json(assessment),
                ),
            )
            for row in positions:
                self._connection.execute(
                    "INSERT OR IGNORE INTO ri_position_assessments(assessment_id, "
                    "instrument_id, risk_level, liquidity_class, payload) VALUES(?,?,?,?,?)",
                    (
                        assessment_id,
                        str(row.get("instrument_id") or ""),
                        str(row.get("risk_level") or ""),
                        str(row.get("liquidity_class") or ""),
                        _json(row),
                    ),
                )
            for kind, payload in metric_groups.items():
                self._connection.execute(
                    "INSERT OR IGNORE INTO ri_metric_snapshots(assessment_id, kind, payload) "
                    "VALUES(?,?,?)",
                    (assessment_id, str(kind), _json(payload)),
                )
            self._connection.execute(
                "INSERT INTO ri_events(portfolio_id, kind, message, payload, created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    str(assessment.get("portfolio_id") or ""),
                    "assessment.recorded",
                    "Risk-intelligence assessment recorded",
                    _json({"assessment_id": assessment_id}),
                    created,
                ),
            )

    def get_assessment(self, assessment_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT payload FROM ri_assessments WHERE assessment_id=?", (assessment_id,)
        ).fetchone()
        if row is None:
            raise RiskIntelligenceNotFound(f"unknown risk assessment {assessment_id!r}")
        return dict(_load(row["payload"]))

    def latest_assessment(self, portfolio_id: str = "") -> dict[str, Any] | None:
        if portfolio_id:
            row = self._connection.execute(
                "SELECT payload FROM ri_assessments WHERE portfolio_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (portfolio_id,),
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT payload FROM ri_assessments ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return dict(_load(row["payload"])) if row else None

    def assessments(
        self, *, portfolio_id: str = "", limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        page = max(1, min(int(limit), 200))
        start = max(0, int(offset))
        where = " WHERE portfolio_id=?" if portfolio_id else ""
        params: tuple[Any, ...] = (portfolio_id,) if portfolio_id else ()
        total = int(
            self._connection.execute(
                "SELECT COUNT(*) n FROM ri_assessments" + where, params
            ).fetchone()["n"]
        )
        rows = self._connection.execute(
            "SELECT payload FROM ri_assessments"
            + where
            + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, page, start),
        ).fetchall()
        return {
            "items": [dict(_load(row["payload"])) for row in rows],
            "total": total,
            "limit": page,
            "offset": start,
            "has_more": start + len(rows) < total,
        }

    def metric_snapshots(self, assessment_id: str) -> dict[str, Any]:
        rows = self._connection.execute(
            "SELECT kind, payload FROM ri_metric_snapshots WHERE assessment_id=?",
            (assessment_id,),
        ).fetchall()
        return {str(row["kind"]): _load(row["payload"]) for row in rows}

    # -- scenarios and stress ------------------------------------------------
    def save_scenario(self, scenario: Mapping[str, Any]) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO ri_scenarios(scenario_id, portfolio_id, name, kind, "
                "created_at, payload) VALUES(?,?,?,?,?,?)",
                (
                    str(scenario.get("scenario_id") or ""),
                    str(scenario.get("portfolio_id") or ""),
                    str(scenario.get("name") or ""),
                    str(scenario.get("kind") or ""),
                    str(scenario.get("created_at") or _now().isoformat()),
                    _json(scenario),
                ),
            )

    def scenario(self, scenario_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT payload FROM ri_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise RiskIntelligenceNotFound(f"unknown scenario {scenario_id!r}")
        return dict(_load(row["payload"]))

    def scenarios(self, *, portfolio_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if portfolio_id:
            rows = self._connection.execute(
                "SELECT payload FROM ri_scenarios WHERE portfolio_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (portfolio_id, max(1, min(int(limit), 200))),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT payload FROM ri_scenarios ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(_load(row["payload"])) for row in rows]

    def save_stress(self, result: Mapping[str, Any]) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO ri_stress_runs(stress_id, portfolio_id, scenario_id, "
                "mode, status, digest, created_at, payload) VALUES(?,?,?,?,?,?,?,?)",
                (
                    str(result.get("stress_id") or ""),
                    str(result.get("portfolio_id") or ""),
                    str(result.get("scenario_id") or ""),
                    str(result.get("mode") or ""),
                    str(result.get("status") or ""),
                    str(result.get("result_digest") or ""),
                    str(result.get("generated_at") or _now().isoformat()),
                    _json(result),
                ),
            )

    def stress_results(self, *, portfolio_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        if portfolio_id:
            rows = self._connection.execute(
                "SELECT payload FROM ri_stress_runs WHERE portfolio_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (portfolio_id, max(1, min(int(limit), 100))),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT payload FROM ri_stress_runs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [dict(_load(row["payload"])) for row in rows]

    def save_scenario_run(self, result: Mapping[str, Any]) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO ri_scenario_runs(run_id, portfolio_id, scenario_id, "
                "status, digest, created_at, payload) VALUES(?,?,?,?,?,?,?)",
                (
                    str(result.get("run_id") or ""),
                    str(result.get("portfolio_id") or ""),
                    str(result.get("scenario_id") or ""),
                    str(result.get("status") or ""),
                    str(result.get("result_digest") or ""),
                    str(result.get("generated_at") or _now().isoformat()),
                    _json(result),
                ),
            )

    def scenario_runs(self, *, portfolio_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        if portfolio_id:
            rows = self._connection.execute(
                "SELECT payload FROM ri_scenario_runs WHERE portfolio_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (portfolio_id, max(1, min(int(limit), 100))),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT payload FROM ri_scenario_runs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [dict(_load(row["payload"])) for row in rows]

    # -- alerts and recommendations -----------------------------------------
    def save_alert(
        self, alert: Mapping[str, Any], *, cooldown_hours: float
    ) -> tuple[dict[str, Any], bool]:
        now = parse_time(alert.get("observed_at")) or _now()
        cutoff = now.timestamp() - max(0.0, cooldown_hours) * 3600.0
        existing = self._connection.execute(
            "SELECT * FROM ri_alerts WHERE dedupe_key=? AND status='open' "
            "ORDER BY last_seen DESC LIMIT 1",
            (str(alert.get("dedupe_key") or ""),),
        ).fetchone()
        if existing is not None:
            last = parse_time(existing["last_seen"])
            if last is not None and last.timestamp() >= cutoff:
                payload = _load(existing["payload"])
                payload["last_seen"] = now.isoformat()
                payload["occurrences"] = int(payload.get("occurrences") or 1) + 1
                with self._connection:
                    self._connection.execute(
                        "UPDATE ri_alerts SET last_seen=?, payload=? WHERE alert_id=?",
                        (now.isoformat(), _json(payload), existing["alert_id"]),
                    )
                return payload, False
        body = dict(alert)
        body["first_seen"] = now.isoformat()
        body["last_seen"] = now.isoformat()
        body["occurrences"] = 1
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO ri_alerts(alert_id, dedupe_key, portfolio_id, "
                "instrument_id, category, severity, status, first_seen, last_seen, payload) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    str(body.get("alert_id") or ""),
                    str(body.get("dedupe_key") or ""),
                    str(body.get("portfolio_id") or ""),
                    str(body.get("instrument_id") or ""),
                    str(body.get("category") or ""),
                    str(body.get("severity") or ""),
                    "open",
                    body["first_seen"],
                    body["last_seen"],
                    _json(body),
                ),
            )
        return body, True

    def alerts(
        self,
        *,
        portfolio_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if portfolio_id:
            where.append("portfolio_id=?")
            params.append(portfolio_id)
        if status:
            where.append("status=?")
            params.append(status)
        clause = " WHERE " + " AND ".join(where) if where else ""
        rows = self._connection.execute(
            "SELECT payload FROM ri_alerts" + clause + " ORDER BY last_seen DESC LIMIT ?",
            (*params, max(1, min(int(limit), 500))),
        ).fetchall()
        return [dict(_load(row["payload"])) for row in rows]

    def update_alert(self, alert_id: str, *, status: str, actor: str) -> dict[str, Any]:
        if status not in {"acknowledged", "resolved", "open"}:
            raise ValueError("alert status must be open, acknowledged or resolved")
        row = self._connection.execute(
            "SELECT payload FROM ri_alerts WHERE alert_id=?", (alert_id,)
        ).fetchone()
        if row is None:
            raise RiskIntelligenceNotFound(f"unknown risk alert {alert_id!r}")
        payload = _load(row["payload"])
        now = _now().isoformat()
        payload["status"] = status
        payload["updated_at"] = now
        payload["updated_by"] = actor
        with self._connection:
            self._connection.execute(
                "UPDATE ri_alerts SET status=?, payload=? WHERE alert_id=?",
                (status, _json(payload), alert_id),
            )
        return cast(dict[str, Any], payload)

    def save_recommendation(self, recommendation: Mapping[str, Any]) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO ri_recommendations(recommendation_id, portfolio_id, "
                "instrument_id, category, action, created_at, payload) VALUES(?,?,?,?,?,?,?)",
                (
                    str(recommendation.get("recommendation_id") or ""),
                    str(recommendation.get("portfolio_id") or ""),
                    str(recommendation.get("instrument_id") or ""),
                    str(recommendation.get("risk_category") or ""),
                    str(recommendation.get("action") or ""),
                    str(recommendation.get("created_at") or _now().isoformat()),
                    _json(recommendation),
                ),
            )

    def recommendations(self, *, portfolio_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if portfolio_id:
            rows = self._connection.execute(
                "SELECT payload FROM ri_recommendations WHERE portfolio_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (portfolio_id, max(1, min(int(limit), 500))),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT payload FROM ri_recommendations ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(_load(row["payload"])) for row in rows]

    # -- lifecycle -----------------------------------------------------------
    def recover(self, *, at: datetime | None = None) -> dict[str, Any]:
        recovered_at = (at or _now()).isoformat()
        rows = self._connection.execute(
            "SELECT stress_id, payload FROM ri_stress_runs WHERE status=?",
            (AnalysisStatus.CALCULATION_FAILED.value,),
        ).fetchall()
        ids = [str(row["stress_id"]) for row in rows]
        # An analysis is synchronous and is never left running.  This clears a
        # failed stress marker without replaying a calculation.
        with self._connection:
            for row in rows:
                payload = _load(row["payload"])
                payload["status"] = AnalysisStatus.REQUIRES_REVIEW.value
                payload["recovered_at"] = recovered_at
                payload["recovery_note"] = (
                    "Failure marker cleared without replaying the stress calculation."
                )
                self._connection.execute(
                    "UPDATE ri_stress_runs SET status=?, payload=? WHERE stress_id=?",
                    (
                        AnalysisStatus.REQUIRES_REVIEW.value,
                        _json(payload),
                        row["stress_id"],
                    ),
                )
        return {
            "stress_runs_requiring_review": ids,
            "count": len(ids),
            "auto_resumed": False,
            "recovered_at": recovered_at,
        }

    def status(self) -> dict[str, Any]:
        counts = {
            "assessments": int(
                self._connection.execute("SELECT COUNT(*) n FROM ri_assessments").fetchone()["n"]
            ),
            "positions": int(
                self._connection.execute(
                    "SELECT COUNT(*) n FROM ri_position_assessments"
                ).fetchone()["n"]
            ),
            "scenarios": int(
                self._connection.execute("SELECT COUNT(*) n FROM ri_scenarios").fetchone()["n"]
            ),
            "stress_runs": int(
                self._connection.execute("SELECT COUNT(*) n FROM ri_stress_runs").fetchone()["n"]
            ),
            "scenario_runs": int(
                self._connection.execute("SELECT COUNT(*) n FROM ri_scenario_runs").fetchone()["n"]
            ),
            "alerts": int(
                self._connection.execute("SELECT COUNT(*) n FROM ri_alerts").fetchone()["n"]
            ),
            "recommendations": int(
                self._connection.execute("SELECT COUNT(*) n FROM ri_recommendations").fetchone()[
                    "n"
                ]
            ),
        }
        try:
            integrity = str(self._connection.execute("PRAGMA integrity_check").fetchone()[0])
        except Exception as exc:  # noqa: BLE001
            integrity = f"unavailable: {type(exc).__name__}: {exc}"
        return {
            "schema_version": SCHEMA_VERSION,
            "path": self.path,
            "counts": counts,
            "integrity": integrity,
            "financial_tables": [],
            "read_only_financial": True,
        }

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._connection.close()


__all__ = ["RiskIntelligenceStore", "SCHEMA_VERSION"]
