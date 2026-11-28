"""SQLite persistence for NM-07 portfolios (same file as the rest of NovaMarket).

Valuations, decisions and lifecycle rows are append-only at the database level:
triggers abort ``UPDATE`` and ``DELETE``. A portfolio's status changes through
:meth:`PortfolioStore.set_status`, which validates the lifecycle table first.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any

from novacontrol.novamarket.portfolio.models import (
    DecisionRecord,
    PortfolioHorizon,
    PortfolioRecord,
    PortfolioStatus,
    Position,
    ProposalStatus,
    RebalanceProposal,
    ValuationSnapshot,
    dec,
    ensure_portfolio_transition,
    money_text,
    quantity_text,
    utcnow,
)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolios (
    portfolio_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    horizon TEXT NOT NULL,
    funding_wallet_id TEXT NOT NULL,
    wallet_id TEXT NOT NULL,
    initial_capital TEXT NOT NULL,
    realized_pnl TEXT NOT NULL DEFAULT '0.00',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_evaluated_at TEXT,
    last_rebalanced_at TEXT,
    closed_at TEXT,
    close_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_portfolios_horizon
    ON portfolios(horizon, status);

CREATE TABLE IF NOT EXISTS portfolio_positions (
    portfolio_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    quantity TEXT NOT NULL,
    average_cost TEXT NOT NULL,
    realized_pnl TEXT NOT NULL DEFAULT '0.00',
    sector TEXT NOT NULL DEFAULT '',
    asset_class TEXT NOT NULL DEFAULT '',
    last_price TEXT,
    priced_at TEXT,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    PRIMARY KEY (portfolio_id, instrument_id)
);

CREATE TABLE IF NOT EXISTS portfolio_position_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    quantity TEXT NOT NULL,
    price TEXT NOT NULL,
    notional TEXT NOT NULL,
    realized TEXT NOT NULL DEFAULT '0.00',
    at TEXT NOT NULL,
    reference TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_portfolio_history
    ON portfolio_position_history(portfolio_id, at DESC);

CREATE TABLE IF NOT EXISTS portfolio_valuations (
    snapshot_id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL,
    at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_portfolio_valuations
    ON portfolio_valuations(portfolio_id, at DESC);
CREATE TRIGGER IF NOT EXISTS portfolio_valuations_no_update
BEFORE UPDATE ON portfolio_valuations
BEGIN SELECT RAISE(ABORT, 'valuations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS portfolio_valuations_no_delete
BEFORE DELETE ON portfolio_valuations
BEGIN SELECT RAISE(ABORT, 'valuations are immutable'); END;

CREATE TABLE IF NOT EXISTS portfolio_proposals (
    proposal_id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_portfolio_proposals
    ON portfolio_proposals(portfolio_id, created_at DESC);

CREATE TABLE IF NOT EXISTS portfolio_decisions (
    decision_id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_portfolio_decisions
    ON portfolio_decisions(portfolio_id, at DESC);
CREATE TRIGGER IF NOT EXISTS portfolio_decisions_no_update
BEFORE UPDATE ON portfolio_decisions
BEGIN SELECT RAISE(ABORT, 'decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS portfolio_decisions_no_delete
BEFORE DELETE ON portfolio_decisions
BEGIN SELECT RAISE(ABORT, 'decisions are immutable'); END;

CREATE TABLE IF NOT EXISTS portfolio_lifecycle (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_portfolio_lifecycle
    ON portfolio_lifecycle(portfolio_id, id DESC);

CREATE TABLE IF NOT EXISTS portfolio_capital (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    amount TEXT NOT NULL,
    at TEXT NOT NULL,
    UNIQUE(portfolio_id, transaction_id, direction)
);
"""


class PortfolioStore:
    """Portfolios, positions, valuations, proposals, decisions and history."""

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
                "INSERT OR IGNORE INTO portfolio_meta(key, value) VALUES(?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    # -- lifecycle of the store itself ---------------------------------------

    def close(self) -> None:
        with self._lock:
            try:
                self._connection.close()
            except sqlite3.Error:  # pragma: no cover - closing twice
                pass

    def __enter__(self) -> "PortfolioStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """One atomic unit across several writes; inner calls do not commit.

        Nested blocks use savepoints instead of a second ``BEGIN``.  SQLite
        rejects a nested ``BEGIN IMMEDIATE`` outright, so the previous shape
        made the documented nested-call contract fail before the inner body ran.
        An inner failure rolls back only that savepoint; the outer block may
        still commit the writes it accepted.
        """
        with self._lock:
            outermost = self._depth == 0
            savepoint = f"portfolio_tx_{self._depth}"
            if outermost:
                self._connection.execute("BEGIN IMMEDIATE")
            else:
                self._connection.execute(f"SAVEPOINT {savepoint}")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outermost:
                    self._connection.rollback()
                else:
                    self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            self._depth -= 1
            if outermost:
                self._connection.commit()
            else:
                self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")

    def _commit(self) -> None:
        if self._depth == 0:
            self._connection.commit()

    # -- portfolios -----------------------------------------------------------

    def create_portfolio(self, record: PortfolioRecord) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO portfolios(portfolio_id, name, horizon, "
                "funding_wallet_id, wallet_id, initial_capital, realized_pnl, status, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    record.portfolio_id,
                    record.name,
                    record.horizon.value,
                    record.funding_wallet_id,
                    record.wallet_id,
                    money_text(record.initial_capital),
                    money_text(record.realized_pnl),
                    record.status.value,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
            self._commit()

    @staticmethod
    def _portfolio_row(row: sqlite3.Row) -> PortfolioRecord:
        return PortfolioRecord(
            portfolio_id=row["portfolio_id"],
            name=row["name"],
            horizon=PortfolioHorizon(row["horizon"]),
            funding_wallet_id=row["funding_wallet_id"],
            wallet_id=row["wallet_id"],
            initial_capital=dec(row["initial_capital"], field_name="initial_capital"),
            realized_pnl=dec(row["realized_pnl"], field_name="realized_pnl"),
            status=PortfolioStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            last_evaluated_at=(
                datetime.fromisoformat(row["last_evaluated_at"])
                if row["last_evaluated_at"]
                else None
            ),
            last_rebalanced_at=(
                datetime.fromisoformat(row["last_rebalanced_at"])
                if row["last_rebalanced_at"]
                else None
            ),
            closed_at=(
                datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None
            ),
            close_reason=row["close_reason"],
        )

    def portfolio(self, portfolio_id: str) -> PortfolioRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM portfolios WHERE portfolio_id=?", (portfolio_id,)
            ).fetchone()
        return self._portfolio_row(row) if row is not None else None

    def portfolios(
        self,
        *,
        horizon: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[PortfolioRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if horizon:
            clauses.append("horizon=?")
            params.append(horizon)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM portfolios{where} ORDER BY created_at DESC LIMIT ?",
                (*params, max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._portfolio_row(row) for row in rows]

    def set_status(
        self,
        portfolio_id: str,
        status: PortfolioStatus,
        *,
        reason: str,
        at: datetime,
    ) -> PortfolioRecord | None:
        current = self.portfolio(portfolio_id)
        if current is None:
            return None
        ensure_portfolio_transition(current.status, status)
        with self._lock:
            self._connection.execute(
                "UPDATE portfolios SET status=?, updated_at=?, closed_at=?, "
                "close_reason=? WHERE portfolio_id=?",
                (
                    status.value,
                    at.isoformat(),
                    at.isoformat() if status is PortfolioStatus.CLOSED else current.closed_at,
                    reason if status is PortfolioStatus.CLOSED else current.close_reason,
                    portfolio_id,
                ),
            )
            self._connection.execute(
                "INSERT INTO portfolio_lifecycle(portfolio_id, from_status, to_status, "
                "reason, at) VALUES (?,?,?,?,?)",
                (portfolio_id, current.status.value, status.value, reason, at.isoformat()),
            )
            self._commit()
        return self.portfolio(portfolio_id)

    def touch(
        self,
        portfolio_id: str,
        *,
        at: datetime,
        evaluated: bool = False,
        rebalanced: bool = False,
        realized_delta: Decimal | None = None,
    ) -> None:
        with self._lock:
            row = self._connection.execute(
                "SELECT realized_pnl, last_evaluated_at, last_rebalanced_at FROM "
                "portfolios WHERE portfolio_id=?",
                (portfolio_id,),
            ).fetchone()
            if row is None:
                return
            realized = dec(row["realized_pnl"], field_name="realized_pnl")
            total = realized + (realized_delta or Decimal("0"))
            self._connection.execute(
                "UPDATE portfolios SET realized_pnl=?, updated_at=?, "
                "last_evaluated_at=COALESCE(?, last_evaluated_at), "
                "last_rebalanced_at=COALESCE(?, last_rebalanced_at) "
                "WHERE portfolio_id=?",
                (
                    money_text(total),
                    at.isoformat(),
                    at.isoformat() if evaluated else None,
                    at.isoformat() if rebalanced else None,
                    portfolio_id,
                ),
            )
            self._commit()

    # -- positions ------------------------------------------------------------

    def positions(
        self, portfolio_id: str, *, include_closed: bool = False
    ) -> list[Position]:
        clause = "" if include_closed else " AND closed_at IS NULL"
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM portfolio_positions WHERE portfolio_id=?" + clause
                + " ORDER BY instrument_id",
                (portfolio_id,),
            ).fetchall()
        return [self._position_row(row) for row in rows]

    def position(self, portfolio_id: str, instrument_id: str) -> Position | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM portfolio_positions WHERE portfolio_id=? AND "
                "instrument_id=?",
                (portfolio_id, instrument_id),
            ).fetchone()
        return self._position_row(row) if row is not None else None

    @staticmethod
    def _position_row(row: sqlite3.Row) -> Position:
        return Position(
            portfolio_id=row["portfolio_id"],
            instrument_id=row["instrument_id"],
            quantity=dec(row["quantity"], field_name="quantity"),
            average_cost=dec(row["average_cost"], field_name="average_cost"),
            realized_pnl=dec(row["realized_pnl"], field_name="realized_pnl"),
            sector=row["sector"],
            asset_class=row["asset_class"],
            opened_at=datetime.fromisoformat(row["opened_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            last_price=(
                dec(row["last_price"], field_name="last_price")
                if row["last_price"]
                else None
            ),
            priced_at=(
                datetime.fromisoformat(row["priced_at"]) if row["priced_at"] else None
            ),
            closed_at=(
                datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None
            ),
        )

    def upsert_position(self, position: Position) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO portfolio_positions(portfolio_id, instrument_id, "
                "quantity, average_cost, realized_pnl, sector, asset_class, "
                "last_price, priced_at, opened_at, updated_at, closed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(portfolio_id, instrument_id) DO UPDATE SET "
                "quantity=excluded.quantity, average_cost=excluded.average_cost, "
                "realized_pnl=excluded.realized_pnl, sector=excluded.sector, "
                "asset_class=excluded.asset_class, last_price=excluded.last_price, "
                "priced_at=excluded.priced_at, updated_at=excluded.updated_at, "
                "closed_at=excluded.closed_at",
                (
                    position.portfolio_id,
                    position.instrument_id,
                    quantity_text(position.quantity),
                    money_text(position.average_cost),
                    money_text(position.realized_pnl),
                    position.sector,
                    position.asset_class,
                    money_text(position.last_price) if position.last_price else None,
                    position.priced_at.isoformat() if position.priced_at else None,
                    position.opened_at.isoformat(),
                    position.updated_at.isoformat(),
                    position.closed_at.isoformat() if position.closed_at else None,
                ),
            )
            self._commit()

    def record_position_history(
        self,
        *,
        portfolio_id: str,
        instrument_id: str,
        kind: str,
        quantity: Decimal,
        price: Decimal,
        notional: Decimal,
        realized: Decimal,
        at: datetime,
        reference: str = "",
    ) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO portfolio_position_history(portfolio_id, instrument_id, "
                "kind, quantity, price, notional, realized, at, reference) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    portfolio_id,
                    instrument_id,
                    kind,
                    quantity_text(quantity),
                    money_text(price),
                    money_text(notional),
                    money_text(realized),
                    at.isoformat(),
                    reference,
                ),
            )
            self._commit()

    def position_history(
        self,
        portfolio_id: str,
        *,
        instrument_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clause = " AND instrument_id=?" if instrument_id else ""
        params: tuple[Any, ...] = (
            (portfolio_id, instrument_id) if instrument_id else (portfolio_id,)
        )
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM portfolio_position_history WHERE portfolio_id=?"
                + clause
                + " ORDER BY id DESC LIMIT ?",
                (*params, max(1, min(int(limit), 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- valuations -----------------------------------------------------------

    def save_valuation(self, snapshot: ValuationSnapshot) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO portfolio_valuations(snapshot_id, portfolio_id, "
                "at, payload) VALUES (?,?,?,?)",
                (
                    snapshot.snapshot_id,
                    snapshot.portfolio_id,
                    snapshot.at.isoformat(),
                    json.dumps(snapshot.to_dict(), sort_keys=True),
                ),
            )
            self._commit()

    def valuations(
        self, portfolio_id: str, *, limit: int = 200, latest: bool = False
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM portfolio_valuations WHERE portfolio_id=? "
                + ("ORDER BY at DESC LIMIT ?" if latest else "ORDER BY at ASC LIMIT ?"),
                (portfolio_id, max(1, min(int(limit), 500))),
            ).fetchall()
        payloads = [json.loads(row["payload"]) for row in rows]
        return list(reversed(payloads)) if latest else payloads

    # -- proposals ------------------------------------------------------------

    def save_proposal(self, proposal: RebalanceProposal) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO portfolio_proposals(proposal_id, portfolio_id, created_at, "
                "status, expires_at, payload) VALUES (?,?,?,?,?,?)",
                (
                    proposal.proposal_id,
                    proposal.portfolio_id,
                    proposal.created_at.isoformat(),
                    proposal.status.value,
                    proposal.expires_at.isoformat(),
                    json.dumps(proposal.to_dict(), sort_keys=True),
                ),
            )
            self._commit()

    def _proposal_row(self, row: sqlite3.Row) -> RebalanceProposal:
        body = json.loads(row["payload"])
        from novacontrol.novamarket.portfolio.models import Side, TradeIntent

        return RebalanceProposal(
            proposal_id=row["proposal_id"],
            portfolio_id=row["portfolio_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            status=ProposalStatus(row["status"]),
            drift=dec(body["drift"]),
            turnover=dec(body["turnover"]),
            intents=tuple(
                TradeIntent(
                    instrument_id=item["instrument_id"],
                    side=Side(str(item["side"])),
                    target_weight=dec(item["target_weight"]),
                    current_weight=dec(item["current_weight"]),
                    notional=dec(item["notional"]),
                    quantity_delta=dec(item["quantity_delta"]),
                    price=dec(item["price"]),
                    reason=item["reason"],
                )
                for item in body["intents"]
            ),
            rationale=body["rationale"],
            policy_version=body["policy_version"],
            expires_at=datetime.fromisoformat(row["expires_at"]),
            cash_after=dec(body["cash_after"]),
            digest=body["digest"],
            notes=tuple(body.get("notes", ())),
        )

    def proposal(self, proposal_id: str) -> RebalanceProposal | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM portfolio_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        return self._proposal_row(row) if row is not None else None

    def proposals(
        self,
        portfolio_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[RebalanceProposal]:
        clause = " AND status=?" if status else ""
        params: tuple[Any, ...] = (
            (portfolio_id, status) if status else (portfolio_id,)
        )
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM portfolio_proposals WHERE portfolio_id=?" + clause
                + " ORDER BY created_at DESC LIMIT ?",
                (*params, max(1, min(int(limit), 200))),
            ).fetchall()
        return [self._proposal_row(row) for row in rows]

    def set_proposal_status(
        self, proposal_id: str, status: ProposalStatus
    ) -> RebalanceProposal | None:
        with self._lock:
            self._connection.execute(
                "UPDATE portfolio_proposals SET status=? WHERE proposal_id=?",
                (status.value, proposal_id),
            )
            self._commit()
        return self.proposal(proposal_id)

    # -- decisions ------------------------------------------------------------

    def save_decision(self, decision: DecisionRecord) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO portfolio_decisions(decision_id, portfolio_id, "
                "at, kind, payload) VALUES (?,?,?,?,?)",
                (
                    decision.decision_id,
                    decision.portfolio_id,
                    decision.at.isoformat(),
                    decision.kind,
                    json.dumps(decision.to_dict(), sort_keys=True),
                ),
            )
            self._commit()

    def decisions(self, portfolio_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM portfolio_decisions WHERE portfolio_id=? "
                "ORDER BY at DESC LIMIT ?",
                (portfolio_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    # -- lifecycle and capital ------------------------------------------------

    def lifecycle(self, portfolio_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM portfolio_lifecycle WHERE portfolio_id=? "
                "ORDER BY id DESC LIMIT ?",
                (portfolio_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_capital_move(
        self,
        *,
        portfolio_id: str,
        transaction_id: str,
        direction: str,
        amount: Decimal,
        at: datetime,
    ) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO portfolio_capital(portfolio_id, transaction_id, "
                "direction, amount, at) VALUES (?,?,?,?,?)",
                (
                    portfolio_id,
                    transaction_id,
                    direction,
                    money_text(amount),
                    at.isoformat(),
                ),
            )
            self._commit()

    def capital_moves(self, portfolio_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM portfolio_capital WHERE portfolio_id=? "
                "ORDER BY id DESC LIMIT ?",
                (portfolio_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- observability --------------------------------------------------------

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            by_status = {
                row["status"]: int(row["count"])
                for row in self._connection.execute(
                    "SELECT status, COUNT(*) AS count FROM portfolios GROUP BY status"
                ).fetchall()
            }
            open_positions = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM portfolio_positions WHERE closed_at IS NULL"
                ).fetchone()[0]
            )
            valuations = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM portfolio_valuations"
                ).fetchone()[0]
            )
            proposals = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM portfolio_proposals"
                ).fetchone()[0]
            )
            decisions = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM portfolio_decisions"
                ).fetchone()[0]
            )
        return {
            "portfolios": sum(by_status.values()),
            "by_status": by_status,
            "open_positions": open_positions,
            "valuations": valuations,
            "proposals": proposals,
            "decisions": decisions,
        }


__all__ = ["SCHEMA_VERSION", "PortfolioStore"]
