"""SQLite persistence for NM-10 orders, fills, reservations and reconciliation.

The execution store owns only execution records.  Wallet balances, portfolio
positions and risk decisions remain in their own stores; this module never
pretends a single SQLite transaction spans those stores.  Cross-store writes are
ordered, idempotent and independently reconciled.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from novacontrol.novamarket.execution.models import (
    Fill,
    Order,
    OrderStatus,
    ReconciliationResult,
    Reservation,
    ensure_transition,
    money,
    quantity,
    text,
)


class ExecutionStoreError(RuntimeError):
    """An execution record could not be read or written."""


class ExecutionStore:
    def __init__(self, path: str | Path, *, clock: Any | None = None) -> None:
        self.path = path
        self._clock = clock
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self._initialise()

    def _initialise(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS execution_orders (
                    order_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL UNIQUE,
                    portfolio_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_execution_orders_portfolio
                    ON execution_orders(portfolio_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_execution_orders_status
                    ON execution_orders(status);

                CREATE TABLE IF NOT EXISTS execution_order_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL REFERENCES execution_orders(order_id),
                    from_status TEXT NOT NULL,
                    to_status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_execution_transitions_order
                    ON execution_order_transitions(order_id, id);

                CREATE TABLE IF NOT EXISTS execution_fills (
                    fill_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL REFERENCES execution_orders(order_id),
                    sequence INTEGER NOT NULL,
                    executed_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(order_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_execution_fills_order
                    ON execution_fills(order_id, sequence);

                CREATE TABLE IF NOT EXISTS execution_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL REFERENCES execution_orders(order_id),
                    portfolio_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    remaining TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_execution_reservations_order
                    ON execution_reservations(order_id);
                CREATE INDEX IF NOT EXISTS idx_execution_reservations_portfolio
                    ON execution_reservations(portfolio_id, kind, status);

                CREATE TABLE IF NOT EXISTS execution_reconciliations (
                    reconciliation_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL DEFAULT '',
                    ok INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS execution_inflight_fills (
                    fill_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_execution_inflight_status
                    ON execution_inflight_fills(status, created_at);

                CREATE TABLE IF NOT EXISTS autonomy_execution_intents (
                    execution_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    order_id TEXT NOT NULL DEFAULT '',
                    risk_decision_id TEXT NOT NULL DEFAULT '',
                    signal_id TEXT NOT NULL DEFAULT '',
                    portfolio_id TEXT NOT NULL DEFAULT '',
                    instrument_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_autonomy_execution_status
                    ON autonomy_execution_intents(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_autonomy_execution_portfolio
                    ON autonomy_execution_intents(portfolio_id, updated_at);

                CREATE TABLE IF NOT EXISTS autonomy_execution_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_id TEXT NOT NULL,
                    at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_autonomy_events_execution
                    ON autonomy_execution_events(execution_id, event_id);

                CREATE TABLE IF NOT EXISTS autonomy_exit_states (
                    portfolio_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (portfolio_id, instrument_id)
                );
                CREATE TABLE IF NOT EXISTS autonomy_exit_decisions (
                    decision_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL UNIQUE,
                    portfolio_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    order_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_autonomy_exit_decisions_position
                    ON autonomy_exit_decisions(portfolio_id, instrument_id, updated_at);
                CREATE TABLE IF NOT EXISTS autonomy_exit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id TEXT NOT NULL,
                    at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_autonomy_exit_events_decision
                    ON autonomy_exit_events(decision_id, event_id);
                CREATE TABLE IF NOT EXISTS autonomy_learning_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    decision_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS autonomy_exit_configurations (
                    config_id TEXT PRIMARY KEY,
                    portfolio_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    position_id TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL,
                    target_price TEXT NOT NULL DEFAULT '',
                    trailing_stop_fraction TEXT NOT NULL DEFAULT '',
                    high_water_mark TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(portfolio_id, instrument_id)
                );
                CREATE INDEX IF NOT EXISTS idx_autonomy_exit_configurations_position
                    ON autonomy_exit_configurations(portfolio_id, instrument_id, updated_at);

                CREATE TRIGGER IF NOT EXISTS execution_fills_no_update
                BEFORE UPDATE ON execution_fills
                BEGIN SELECT RAISE(ABORT, 'execution fills are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS execution_fills_no_delete
                BEFORE DELETE ON execution_fills
                BEGIN SELECT RAISE(ABORT, 'execution fills are immutable'); END;
                """
            )

    # -- orders -------------------------------------------------------------
    @staticmethod
    def _order(row: sqlite3.Row) -> Order:
        return Order.from_dict(json.loads(row["payload"]))

    def save_order(self, order: Order) -> Order:
        payload = json.dumps(order.to_dict(), sort_keys=True)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO execution_orders(order_id, proposal_id, portfolio_id, status, "
                "created_at, updated_at, payload) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(order_id) DO UPDATE SET status=excluded.status, "
                "updated_at=excluded.updated_at, payload=excluded.payload",
                (
                    order.order_id,
                    order.proposal_id,
                    order.portfolio_id,
                    order.status.value,
                    order.created_at.isoformat(),
                    order.updated_at.isoformat(),
                    payload,
                ),
            )
        return order

    def order(self, order_id: str) -> Order | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM execution_orders WHERE order_id=?", (order_id,)
            ).fetchone()
        return self._order(row) if row else None

    def order_for_proposal(self, proposal_id: str) -> Order | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM execution_orders WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        return self._order(row) if row else None

    def orders(
        self,
        *,
        status: str = "",
        portfolio_id: str = "",
        limit: int = 100,
    ) -> tuple[Order, ...]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if portfolio_id:
            clauses.append("portfolio_id=?")
            params.append(portfolio_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM execution_orders" + where + " ORDER BY created_at, order_id LIMIT ?",
                (*params, max(1, min(int(limit), 1000))),
            ).fetchall()
        return tuple(self._order(row) for row in rows)

    def transition(
        self, order: Order, target: OrderStatus | str, *, reason: str = "", at: datetime | None = None
    ) -> Order:
        current, wanted = ensure_transition(order.status, target)
        from novacontrol.novamarket.execution.models import Order

        if current is wanted:
            return order
        changed_at = at or (self._clock() if callable(self._clock) else None) or datetime.now().astimezone()
        updated = Order(
            **{
                **{name: getattr(order, name) for name in order.__dataclass_fields__},
                "status": wanted,
                "updated_at": changed_at,
                "version": order.version + 1,
            }
        )
        payload = json.dumps(updated.to_dict(), sort_keys=True)
        with self._lock, self._connection:
            changed = self._connection.execute(
                "UPDATE execution_orders SET status=?, updated_at=?, payload=? "
                "WHERE order_id=? AND status=?",
                (
                    wanted.value,
                    updated.updated_at.isoformat(),
                    payload,
                    order.order_id,
                    current.value,
                ),
            )
            if changed.rowcount != 1:
                raise ExecutionStoreError(
                    f"order {order.order_id} changed concurrently; expected {current.value}"
                )
            self._connection.execute(
                "INSERT INTO execution_order_transitions(order_id, from_status, to_status, "
                "reason, at) VALUES (?,?,?,?,?)",
                (order.order_id, current.value, wanted.value, str(reason), changed_at.isoformat()),
            )
        return updated

    def transitions(self, order_id: str) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM execution_order_transitions WHERE order_id=? ORDER BY id",
                (order_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    # -- fills ---------------------------------------------------------------
    @staticmethod
    def _fill(row: sqlite3.Row) -> Fill:
        return Fill.from_dict(json.loads(row["payload"]))

    def save_fill(self, fill: Fill) -> Fill:
        payload = json.dumps(fill.to_dict(), sort_keys=True)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO execution_fills(fill_id, order_id, sequence, executed_at, payload) "
                "VALUES (?,?,?,?,?)",
                (fill.fill_id, fill.order_id, fill.sequence, fill.executed_at.isoformat(), payload),
            )
        return fill

    def record_fill(
        self,
        fill: Fill,
        order: Order,
        target: OrderStatus | str,
        *,
        reason: str = "paper fill recorded",
        reservations: Iterable[Reservation] = (),
    ) -> Order:
        """Persist a fill, its order update, transition and reservations together.

        Wallet and portfolio stores remain separate by design; this transaction
        still closes the execution-owned half of the write so a fill can never
        exist without the order quantity and reservation state that produced it.
        """
        current, wanted = ensure_transition(order.status, target)
        updated = Order(
            **{
                **{name: getattr(order, name) for name in order.__dataclass_fields__},
                "status": wanted,
                "updated_at": order.updated_at,
                "version": order.version + 1,
            }
        )
        payload = json.dumps(updated.to_dict(), sort_keys=True)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO execution_fills(fill_id, order_id, sequence, executed_at, payload) "
                "VALUES (?,?,?,?,?)",
                (
                    fill.fill_id,
                    fill.order_id,
                    fill.sequence,
                    fill.executed_at.isoformat(),
                    json.dumps(fill.to_dict(), sort_keys=True),
                ),
            )
            changed = self._connection.execute(
                "UPDATE execution_orders SET status=?, updated_at=?, payload=? "
                "WHERE order_id=? AND status=?",
                (
                    wanted.value,
                    updated.updated_at.isoformat(),
                    payload,
                    order.order_id,
                    current.value,
                ),
            )
            if changed.rowcount != 1:
                raise ExecutionStoreError(
                    f"order {order.order_id} changed concurrently; expected "
                    f"{current.value}"
                )
            if wanted is not current:
                self._connection.execute(
                    "INSERT INTO execution_order_transitions(order_id, from_status, to_status, "
                    "reason, at) VALUES (?,?,?,?,?)",
                    (
                        order.order_id,
                        current.value,
                        wanted.value,
                        str(reason),
                        updated.updated_at.isoformat(),
                    ),
                )
            for reservation in reservations:
                changed_reservation = self._connection.execute(
                    "UPDATE execution_reservations SET remaining=?, status=?, updated_at=? "
                    "WHERE reservation_id=? AND order_id=?",
                    (
                        text(reservation.remaining),
                        reservation.status,
                        reservation.updated_at.isoformat(),
                        reservation.reservation_id,
                        order.order_id,
                    ),
                )
                if changed_reservation.rowcount != 1:
                    raise ExecutionStoreError(
                        f"reservation {reservation.reservation_id} is missing"
                    )
            self._connection.execute(
                "UPDATE execution_inflight_fills SET status='committed', updated_at=?, "
                "payload=? WHERE fill_id=? AND status='pending'",
                (
                    self._now_iso(),
                    json.dumps(fill.to_dict(), sort_keys=True),
                    fill.fill_id,
                ),
            )
        return updated

    def fill(self, fill_id: str) -> Fill | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM execution_fills WHERE fill_id=?", (fill_id,)
            ).fetchone()
        return self._fill(row) if row else None

    def fills(self, order_id: str | None = None) -> tuple[Fill, ...]:
        with self._lock:
            if order_id:
                rows = self._connection.execute(
                    "SELECT * FROM execution_fills WHERE order_id=? ORDER BY sequence",
                    (order_id,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM execution_fills ORDER BY executed_at, sequence"
                ).fetchall()
        return tuple(self._fill(row) for row in rows)

    def fill_count(self, order_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM execution_fills WHERE order_id=?", (order_id,)
            ).fetchone()
        return int(row["n"])

    # -- reservations --------------------------------------------------------
    def save_reservation(self, reservation: Reservation) -> Reservation:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO execution_reservations(reservation_id, order_id, portfolio_id, kind, "
                "amount, remaining, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(reservation_id) DO UPDATE SET "
                "remaining=excluded.remaining, status=excluded.status, updated_at=excluded.updated_at",
                (
                    reservation.reservation_id,
                    reservation.order_id,
                    reservation.portfolio_id,
                    reservation.kind,
                    text(reservation.amount),
                    text(reservation.remaining),
                    reservation.status,
                    reservation.created_at.isoformat(),
                    reservation.updated_at.isoformat(),
                ),
            )
        return reservation

    def reservations(self, *, order_id: str = "", portfolio_id: str = "", kind: str = "", status: str = "") -> tuple[Reservation, ...]:
        clauses: list[str] = []
        params: list[Any] = []
        for name, value in (("order_id", order_id), ("portfolio_id", portfolio_id), ("kind", kind), ("status", status)):
            if value:
                clauses.append(f"{name}=?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM execution_reservations" + where + " ORDER BY created_at, reservation_id",
                tuple(params),
            ).fetchall()
        return tuple(
            Reservation(
                reservation_id=row["reservation_id"],
                order_id=row["order_id"],
                portfolio_id=row["portfolio_id"],
                kind=row["kind"],
                amount=Decimal(row["amount"]),
                remaining=Decimal(row["remaining"]),
                status=row["status"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        )

    def reserved_total(self, portfolio_id: str, kind: str) -> Decimal:
        rows = self.reservations(portfolio_id=portfolio_id, kind=kind, status="active")
        return sum((row.remaining for row in rows), Decimal("0"))

    # -- durable in-flight fill journal --------------------------------------
    def save_inflight(self, fill: Fill, *, status: str = "pending") -> None:
        """Record a fill's intent before any wallet or portfolio write.

        Recovery can then tell whether external effects belong to a committed
        execution record or must be compensated.  The journal is execution-owned
        state; it is not a second ledger and never changes wallet balances.
        """
        now = self._now_iso()
        payload = json.dumps(fill.to_dict(), sort_keys=True)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO execution_inflight_fills(fill_id, order_id, status, "
                "created_at, updated_at, payload) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(fill_id) DO UPDATE SET status=excluded.status, "
                "updated_at=excluded.updated_at, payload=excluded.payload",
                (fill.fill_id, fill.order_id, status, now, now, payload),
            )

    def update_inflight(self, fill: Fill) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE execution_inflight_fills SET updated_at=?, payload=? "
                "WHERE fill_id=? AND status='pending'",
                (self._now_iso(), json.dumps(fill.to_dict(), sort_keys=True), fill.fill_id),
            )

    def settle_inflight(self, fill_id: str, status: str) -> None:
        if status not in {"pending", "committed", "rolled_back"}:
            raise ExecutionStoreError(f"unknown in-flight fill status {status!r}")
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE execution_inflight_fills SET status=?, updated_at=? "
                "WHERE fill_id=?",
                (status, self._now_iso(), fill_id),
            )

    def inflight(self, fill_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM execution_inflight_fills WHERE fill_id=?", (fill_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "fill": Fill.from_dict(json.loads(row["payload"])),
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def pending_inflight(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM execution_inflight_fills WHERE status='pending' "
                "ORDER BY created_at, fill_id"
            ).fetchall()
        return tuple(
            {
                "fill": Fill.from_dict(json.loads(row["payload"])),
                "status": str(row["status"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        )

    def _now_iso(self) -> str:
        moment = self._clock() if callable(self._clock) else datetime.now().astimezone()
        return moment.isoformat()

    # -- reconciliation ------------------------------------------------------
    def save_reconciliation(self, result: ReconciliationResult) -> ReconciliationResult:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO execution_reconciliations(reconciliation_id, order_id, ok, "
                "created_at, payload) VALUES (?,?,?,?,?)",
                (
                    result.reconciliation_id,
                    result.order_id,
                    1 if result.ok else 0,
                    result.created_at.isoformat(),
                    json.dumps(result.to_dict(), sort_keys=True),
                ),
            )
        return result

    def reconciliations(self, limit: int = 50) -> tuple[ReconciliationResult, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM execution_reconciliations ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return tuple(ReconciliationResult.from_dict(json.loads(row["payload"])) for row in rows)

    # -- AT-04 autonomous paper-execution journal ---------------------------
    # The journal records AT-04 intents; orders and fills remain the NM-10
    # records above.  Persisting the intent before forwarding is what makes a
    # retry safe, and proposal_id/idempotency_key are both unique barriers.
    def save_autonomy_intent(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        record = dict(payload)
        execution_id_value = str(record.get("execution_id") or "").strip()
        proposal_id = str(record.get("proposal_id") or "").strip()
        if not execution_id_value or not proposal_id:
            raise ExecutionStoreError(
                "an autonomy execution intent needs execution_id and proposal_id"
            )
        key = str(record.get("idempotency_key") or "").strip()
        if not key:
            key = f"at04:{proposal_id}"
            record["idempotency_key"] = key
        now = str(record.get("updated_at") or self._now_iso())
        record["updated_at"] = now
        record.setdefault("created_at", now)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO autonomy_execution_intents("
                "execution_id, proposal_id, idempotency_key, status, order_id, "
                "risk_decision_id, signal_id, portfolio_id, instrument_id, "
                "created_at, updated_at, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    execution_id_value,
                    proposal_id,
                    key,
                    str(record.get("status") or "intent"),
                    str(record.get("order_id") or ""),
                    str(record.get("risk_decision_id") or ""),
                    str(record.get("signal_id") or ""),
                    str(record.get("portfolio_id") or ""),
                    str(record.get("instrument_id") or ""),
                    str(record.get("created_at") or now),
                    now,
                    json.dumps(record, sort_keys=True),
                ),
            )
            row = self._connection.execute(
                "SELECT payload FROM autonomy_execution_intents WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
        if row is None:
            raise ExecutionStoreError(
                f"autonomy execution intent for {proposal_id!r} was not persisted"
            )
        return dict(json.loads(row["payload"]))

    def update_autonomy_intent(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        record = dict(payload)
        execution_id_value = str(record.get("execution_id") or "").strip()
        if not execution_id_value:
            raise ExecutionStoreError("updating an autonomy intent needs execution_id")
        now = str(record.get("updated_at") or self._now_iso())
        record["updated_at"] = now
        with self._lock, self._connection:
            changed = self._connection.execute(
                "UPDATE autonomy_execution_intents SET status=?, order_id=?, "
                "updated_at=?, payload=? WHERE execution_id=?",
                (
                    str(record.get("status") or "intent"),
                    str(record.get("order_id") or ""),
                    now,
                    json.dumps(record, sort_keys=True),
                    execution_id_value,
                ),
            )
            if changed.rowcount != 1:
                raise ExecutionStoreError(
                    f"unknown autonomy execution intent {execution_id_value!r}"
                )
            row = self._connection.execute(
                "SELECT payload FROM autonomy_execution_intents WHERE execution_id=?",
                (execution_id_value,),
            ).fetchone()
        if row is None:
            raise ExecutionStoreError(
                f"autonomy execution intent {execution_id_value!r} disappeared"
            )
        return dict(json.loads(row["payload"]))

    def autonomy_intent(
        self, *, proposal_id: str = "", execution_id: str = ""
    ) -> dict[str, Any] | None:
        column = "execution_id" if execution_id else "proposal_id"
        value = execution_id or proposal_id
        if not value:
            return None
        with self._lock:
            row = self._connection.execute(
                f"SELECT payload FROM autonomy_execution_intents WHERE {column}=?",
                (value,),
            ).fetchone()
        return dict(json.loads(row["payload"])) if row else None

    def autonomy_intents(
        self,
        *,
        status: str = "",
        portfolio_id: str = "",
        limit: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if portfolio_id:
            clauses.append("portfolio_id=?")
            params.append(portfolio_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM autonomy_execution_intents"
                + where
                + " ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (*params, max(1, min(int(limit), 500))),
            ).fetchall()
        return tuple(dict(json.loads(row["payload"])) for row in rows)

    def append_autonomy_event(
        self,
        execution_id_value: str,
        kind: str,
        *,
        message: str = "",
        payload: Mapping[str, Any] | None = None,
        at: str = "",
    ) -> dict[str, Any]:
        moment = at or self._now_iso()
        body = dict(payload or {})
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT INTO autonomy_execution_events(execution_id, at, kind, "
                "message, payload) VALUES (?,?,?,?,?)",
                (execution_id_value, moment, kind, message, json.dumps(body, sort_keys=True)),
            )
            # The journal is auditable but bounded: retain the newest 500
            # events per execution.  Order/fill history itself stays immutable
            # in the NM-10 tables.
            self._connection.execute(
                "DELETE FROM autonomy_execution_events WHERE execution_id=? AND "
                "event_id NOT IN (SELECT event_id FROM autonomy_execution_events "
                "WHERE execution_id=? ORDER BY event_id DESC LIMIT 500)",
                (execution_id_value, execution_id_value),
            )
        return {
            "event_id": int(cursor.lastrowid or 0),
            "execution_id": execution_id_value,
            "at": moment,
            "kind": kind,
            "message": message,
            "payload": body,
        }

    def autonomy_events(
        self, execution_id_value: str, *, limit: int = 200
    ) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM autonomy_execution_events WHERE execution_id=? "
                "ORDER BY event_id LIMIT ?",
                (execution_id_value, max(1, min(int(limit), 1000))),
            ).fetchall()
        return tuple(
            {
                "event_id": int(row["event_id"]),
                "execution_id": str(row["execution_id"]),
                "at": str(row["at"]),
                "kind": str(row["kind"]),
                "message": str(row["message"]),
                "payload": dict(json.loads(row["payload"])),
            }
            for row in rows
        )

    def autonomy_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT status, COUNT(*) AS n FROM autonomy_execution_intents GROUP BY status"
            ).fetchall()
            total_row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM autonomy_execution_intents"
            ).fetchone()
            event_row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM autonomy_execution_events"
            ).fetchone()
            order_row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM execution_orders"
            ).fetchone()
            filled_order_row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM execution_orders WHERE status='filled'"
            ).fetchone()
            partial_order_row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM execution_orders WHERE status='partially_filled'"
            ).fetchone()
            fill_row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM execution_fills"
            ).fetchone()
        result = {str(row["status"]): int(row["n"]) for row in rows}
        result["intents"] = int(total_row["n"]) if total_row else 0
        result["events"] = int(event_row["n"]) if event_row else 0
        result["orders"] = int(order_row["n"]) if order_row else 0
        result["filled_orders"] = int(filled_order_row["n"]) if filled_order_row else 0
        result["partial_orders"] = int(partial_order_row["n"]) if partial_order_row else 0
        result["fills"] = int(fill_row["n"]) if fill_row else 0
        return result

    # -- AT-05 exit supervision journal -------------------------------------
    def save_exit_state(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        portfolio_id = str(body.get("portfolio_id") or "")
        instrument_id = str(body.get("instrument_id") or "")
        if not portfolio_id or not instrument_id:
            raise ExecutionStoreError("an exit state needs portfolio_id and instrument_id")
        now = str(body.get("updated_at") or self._now_iso())
        body["updated_at"] = now
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO autonomy_exit_states(portfolio_id, instrument_id, updated_at, payload) "
                "VALUES (?,?,?,?) ON CONFLICT(portfolio_id, instrument_id) DO UPDATE SET "
                "updated_at=excluded.updated_at, payload=excluded.payload",
                (portfolio_id, instrument_id, now, json.dumps(body, sort_keys=True)),
            )
        return body

    def exit_state(self, portfolio_id: str, instrument_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM autonomy_exit_states WHERE portfolio_id=? AND instrument_id=?",
                (portfolio_id, instrument_id),
            ).fetchone()
        return dict(json.loads(row["payload"])) if row else None

    def save_exit_decision(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        decision_id = str(body.get("decision_id") or "")
        proposal_id = str(body.get("proposal_id") or "")
        if not decision_id or not proposal_id:
            raise ExecutionStoreError("an exit decision needs decision_id and proposal_id")
        now = str(body.get("updated_at") or self._now_iso())
        body["updated_at"] = now
        body.setdefault("created_at", now)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO autonomy_exit_decisions(decision_id, proposal_id, "
                "portfolio_id, instrument_id, status, order_id, created_at, updated_at, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (decision_id, proposal_id, str(body.get("portfolio_id") or ""),
                 str(body.get("instrument_id") or ""), str(body.get("status") or "intent"),
                 str(body.get("order_id") or ""), str(body.get("created_at") or now), now,
                 json.dumps(body, sort_keys=True)),
            )
            row = self._connection.execute(
                "SELECT payload FROM autonomy_exit_decisions WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        return dict(json.loads(row["payload"])) if row else body

    def update_exit_decision(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        decision_id = str(body.get("decision_id") or "")
        now = str(body.get("updated_at") or self._now_iso())
        body["updated_at"] = now
        with self._lock, self._connection:
            changed = self._connection.execute(
                "UPDATE autonomy_exit_decisions SET status=?, order_id=?, updated_at=?, payload=? "
                "WHERE decision_id=?",
                (str(body.get("status") or "intent"), str(body.get("order_id") or ""), now,
                 json.dumps(body, sort_keys=True), decision_id),
            )
            if changed.rowcount != 1:
                raise ExecutionStoreError(f"unknown exit decision {decision_id!r}")
        return body

    def exit_decision(self, decision_id: str = "", proposal_id: str = "") -> dict[str, Any] | None:
        column = "decision_id" if decision_id else "proposal_id"
        value = decision_id or proposal_id
        if not value:
            return None
        with self._lock:
            row = self._connection.execute(
                f"SELECT payload FROM autonomy_exit_decisions WHERE {column}=?", (value,)
            ).fetchone()
        return dict(json.loads(row["payload"])) if row else None

    def exit_decisions(self, *, portfolio_id: str = "", limit: int = 100) -> tuple[dict[str, Any], ...]:
        where = " WHERE portfolio_id=?" if portfolio_id else ""
        params: list[Any] = [portfolio_id] if portfolio_id else []
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM autonomy_exit_decisions" + where +
                " ORDER BY updated_at DESC, rowid DESC LIMIT ?", tuple(params)
            ).fetchall()
        return tuple(dict(json.loads(row["payload"])) for row in rows)

    def save_learning_evidence(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        evidence_id = str(body.get("evidence_id") or "")
        decision_id = str(body.get("decision_id") or "")
        if not evidence_id or not decision_id:
            raise ExecutionStoreError("learning evidence needs evidence_id and decision_id")
        now = str(body.get("created_at") or self._now_iso())
        body["created_at"] = now
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO autonomy_learning_evidence(evidence_id, decision_id, "
                "status, created_at, payload) VALUES (?,?,?,?,?) "
                "ON CONFLICT(decision_id) DO UPDATE SET status=excluded.status, "
                "payload=excluded.payload",
                (evidence_id, decision_id, str(body.get("status") or "incomplete"), now,
                 json.dumps(body, sort_keys=True)),
            )
            row = self._connection.execute(
                "SELECT payload FROM autonomy_learning_evidence WHERE decision_id=?", (decision_id,)
            ).fetchone()
        return dict(json.loads(row["payload"])) if row else body

    def learning_evidence(self, decision_id: str = "", limit: int = 100) -> tuple[dict[str, Any], ...]:
        where = " WHERE decision_id=?" if decision_id else ""
        params: list[Any] = [decision_id] if decision_id else []
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM autonomy_learning_evidence" + where +
                " ORDER BY created_at DESC, rowid DESC LIMIT ?", tuple(params)
            ).fetchall()
        return tuple(dict(json.loads(row["payload"])) for row in rows)

    def exit_events(self, decision_id: str, limit: int = 200) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT event_id, decision_id, at, kind, message, payload "
                "FROM autonomy_exit_events WHERE decision_id=? ORDER BY event_id LIMIT ?",
                (decision_id, max(1, min(int(limit), 1000))),
            ).fetchall()
        return tuple(
            {
                "event_id": row["event_id"],
                "decision_id": row["decision_id"],
                "at": row["at"],
                "kind": row["kind"],
                "message": row["message"],
                "payload": dict(json.loads(row["payload"])),
            }
            for row in rows
        )

    def exit_states(self, limit: int = 200) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM autonomy_exit_states ORDER BY updated_at DESC, rowid DESC "
                "LIMIT ?", (max(1, min(int(limit), 500)),)
            ).fetchall()
        return tuple(dict(json.loads(row["payload"])) for row in rows)

    def record_exit_event(self, decision_id: str, kind: str, message: str,
                          payload: Mapping[str, Any]) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO autonomy_exit_events(decision_id, at, kind, message, payload) "
                "VALUES (?,?,?,?,?)",
                (decision_id, self._now_iso(), str(kind), str(message),
                 json.dumps(dict(payload), sort_keys=True)),
            )

    # -- AT-05 target/trailing configuration --------------------------------
    def save_exit_configuration(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Insert or version one exit configuration, preserving high-water state.

        The configuration row owns the target/trailing definition and the
        trailing high-water mark.  Cash, positions, orders, fills and the
        ledger remain in their existing authoritative stores.
        """

        body = dict(payload)
        portfolio_id = str(body.get("portfolio_id") or "")
        instrument_id = str(body.get("instrument_id") or "")
        if not portfolio_id or not instrument_id:
            raise ExecutionStoreError(
                "an exit configuration needs portfolio_id and instrument_id"
            )
        existing = self.exit_configuration(portfolio_id, instrument_id)
        if existing is not None:
            body["config_id"] = str(existing.get("config_id") or body.get("config_id") or "")
            body["created_at"] = str(existing.get("created_at") or body.get("created_at") or "")
            body["version"] = int(existing.get("version") or 0) + 1
            if str(existing.get("trailing_stop_fraction") or "") == str(
                body.get("trailing_stop_fraction") or ""
            ):
                body.setdefault("high_water_mark", existing.get("high_water_mark") or "")
                body.setdefault("high_water_at", existing.get("high_water_at") or "")
        else:
            body["version"] = max(1, int(body.get("version") or 1))
        now = str(body.get("updated_at") or self._now_iso())
        body["updated_at"] = now
        body.setdefault("created_at", now)
        body.setdefault("high_water_mark", "")
        body.setdefault("high_water_at", "")
        body.setdefault("target_price", "")
        body.setdefault("trailing_stop_fraction", "")
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO autonomy_exit_configurations(config_id, portfolio_id, "
                "instrument_id, position_id, version, target_price, "
                "trailing_stop_fraction, high_water_mark, created_at, updated_at, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(portfolio_id, instrument_id) "
                "DO UPDATE SET position_id=excluded.position_id, version=excluded.version, "
                "target_price=excluded.target_price, "
                "trailing_stop_fraction=excluded.trailing_stop_fraction, "
                "high_water_mark=excluded.high_water_mark, updated_at=excluded.updated_at, "
                "payload=excluded.payload",
                (
                    str(body["config_id"]),
                    portfolio_id,
                    instrument_id,
                    str(body.get("position_id") or ""),
                    int(body["version"]),
                    str(body.get("target_price") or ""),
                    str(body.get("trailing_stop_fraction") or ""),
                    str(body.get("high_water_mark") or ""),
                    str(body["created_at"]),
                    now,
                    json.dumps(body, sort_keys=True),
                ),
            )
        return dict(body)

    def update_exit_configuration_state(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Persist market/trigger state without changing configuration version."""

        body = dict(payload)
        config_id = str(body.get("config_id") or "")
        if not config_id:
            raise ExecutionStoreError("an exit configuration state needs config_id")
        now = str(body.get("updated_at") or self._now_iso())
        body["updated_at"] = now
        with self._lock, self._connection:
            changed = self._connection.execute(
                "UPDATE autonomy_exit_configurations SET position_id=?, "
                "target_price=?, trailing_stop_fraction=?, high_water_mark=?, "
                "updated_at=?, payload=? WHERE config_id=?",
                (
                    str(body.get("position_id") or ""),
                    str(body.get("target_price") or ""),
                    str(body.get("trailing_stop_fraction") or ""),
                    str(body.get("high_water_mark") or ""),
                    now,
                    json.dumps(body, sort_keys=True),
                    config_id,
                ),
            )
            if changed.rowcount != 1:
                raise ExecutionStoreError(f"unknown exit configuration {config_id!r}")
        return body

    def exit_configuration(
        self, portfolio_id: str, instrument_id: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM autonomy_exit_configurations "
                "WHERE portfolio_id=? AND instrument_id=?",
                (portfolio_id, instrument_id),
            ).fetchone()
        return dict(json.loads(row["payload"])) if row else None

    def exit_configurations(self, limit: int = 200) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM autonomy_exit_configurations "
                "ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return tuple(dict(json.loads(row["payload"])) for row in rows)

    def delete_exit_configuration(self, portfolio_id: str, instrument_id: str) -> bool:
        with self._lock, self._connection:
            changed = self._connection.execute(
                "DELETE FROM autonomy_exit_configurations "
                "WHERE portfolio_id=? AND instrument_id=?",
                (portfolio_id, instrument_id),
            )
        return changed.rowcount == 1

    # -- reporting -----------------------------------------------------------
    def counts(self) -> dict[str, int]:
        with self._lock:
            order_rows = self._connection.execute(
                "SELECT status, COUNT(*) AS n FROM execution_orders GROUP BY status"
            ).fetchall()
            fill_row = self._connection.execute("SELECT COUNT(*) AS n FROM execution_fills").fetchone()
            reconciliation_row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM execution_reconciliations"
            ).fetchone()
            inflight_row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM execution_inflight_fills WHERE status='pending'"
            ).fetchone()
        result: dict[str, Any] = {
            "orders": sum(int(row["n"]) for row in order_rows),
            "fills": int(fill_row["n"]),
            "reconciliations": int(reconciliation_row["n"]),
            "inflight_pending": int(inflight_row["n"]),
            "by_status": {row["status"]: int(row["n"]) for row in order_rows},
        }
        return result

    def close(self) -> None:
        with self._lock:
            self._connection.close()
