"""Isolated SQLite resilience drills for the NovaMarket financial stores.

Every test owns a temporary database.  No test opens ``data/`` or mutates a
user/financial store; these exercise the same SQLite primitives the portfolio,
wallet and execution stores use.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


def _connect(path: Path, *, timeout: float = 5.0, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


class SqliteRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self._tmp.name) / "financial.sqlite3"
        connection = _connect(self.path)
        connection.execute("CREATE TABLE ledger(id INTEGER PRIMARY KEY, amount TEXT NOT NULL)")
        connection.execute("INSERT INTO ledger(amount) VALUES('100.00')")
        connection.close()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_lock_contention_fails_closed_and_recovers_after_release(self) -> None:
        holder = _connect(self.path)
        contender = _connect(self.path, timeout=0.1)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO ledger(amount) VALUES('locked')")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                contender.execute("BEGIN IMMEDIATE")
                contender.execute("INSERT INTO ledger(amount) VALUES('contender')")
        finally:
            holder.execute("ROLLBACK")
            holder.close()

        contender.execute("BEGIN IMMEDIATE")
        contender.execute("INSERT INTO ledger(amount) VALUES('after-release')")
        contender.execute("COMMIT")
        self.assertEqual(
            [row[0] for row in contender.execute("SELECT amount FROM ledger ORDER BY id")],
            ["100.00", "after-release"],
        )
        contender.close()

    def test_read_only_connection_cannot_write_and_integrity_is_unchanged(self) -> None:
        reader = _connect(self.path, read_only=True)
        with self.assertRaises(sqlite3.OperationalError):
            reader.execute("INSERT INTO ledger(amount) VALUES('forbidden')")
        self.assertEqual(reader.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(reader.execute("SELECT COUNT(*) FROM ledger").fetchone()[0], 1)
        reader.close()

    def test_disk_full_is_a_failed_transaction_not_a_partial_write(self) -> None:
        connection = _connect(self.path)
        current_pages = int(connection.execute("PRAGMA page_count").fetchone()[0])
        connection.execute(f"PRAGMA max_page_count={current_pages + 1}")
        connection.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute(
                    "INSERT INTO ledger(amount) VALUES(?)", ("x" * 2_000_000,)
                )
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0], 1)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        connection.close()

    def test_uncommitted_wal_transaction_is_not_visible_after_reopen(self) -> None:
        writer = _connect(self.path)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO ledger(amount) VALUES('uncommitted')")
        writer.close()  # process termination without COMMIT

        reopened = _connect(self.path)
        self.assertEqual(reopened.execute("SELECT COUNT(*) FROM ledger").fetchone()[0], 1)
        self.assertEqual(reopened.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        reopened.close()

    def test_backup_restores_the_last_consistent_snapshot(self) -> None:
        backup_path = Path(self._tmp.name) / "backup.sqlite3"
        source = _connect(self.path)
        backup = sqlite3.connect(backup_path)
        source.backup(backup)
        backup.close()

        source.execute("INSERT INTO ledger(amount) VALUES('after-backup')")
        self.assertEqual(source.execute("SELECT COUNT(*) FROM ledger").fetchone()[0], 2)
        source.close()

        restored = sqlite3.connect(self.path)
        snapshot = sqlite3.connect(backup_path)
        snapshot.backup(restored)
        snapshot.close()
        self.assertEqual(restored.execute("SELECT COUNT(*) FROM ledger").fetchone()[0], 1)
        self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(restored.execute("SELECT amount FROM ledger").fetchone()[0], "100.00")
        restored.close()


if __name__ == "__main__":
    unittest.main()