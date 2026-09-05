"""Memory repository implementations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Iterator, Protocol, runtime_checkable

from novacontrol.memory.models import MemoryQuery, MemoryRecord, MemorySearchResult


@runtime_checkable
class MemoryRepository(Protocol):
    async def put_record(self, record: MemoryRecord) -> None:
        """Store a typed memory record."""

    async def get_record(self, namespace: str, key: str) -> MemoryRecord | None:
        """Retrieve a typed memory record."""

    async def search_records(self, query: MemoryQuery) -> Sequence[MemorySearchResult]:
        """Search typed memory records."""

    async def cleanup_expired(self) -> int:
        """Remove expired memory records and return the deletion count."""


class InMemoryMemoryStore:
    """Fast process-local memory repository."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], MemoryRecord] = {}

    async def put(self, namespace: str, key: str, value: Mapping[str, Any]) -> None:
        await self.put_record(
            MemoryRecord.create(
                namespace=namespace,
                key=key,
                value=dict(value),
                text=str(value.get("text", "")),
            )
        )

    async def get(self, namespace: str, key: str) -> Mapping[str, Any] | None:
        record = await self.get_record(namespace, key)
        return dict(record.value) if record is not None else None

    async def search(
        self,
        namespace: str,
        query: str,
        limit: int = 10,
    ) -> Sequence[Mapping[str, Any]]:
        results = await self.search_records(MemoryQuery(namespace, query, limit))
        return tuple(result.record.to_dict() for result in results)

    async def put_record(self, record: MemoryRecord) -> None:
        self._records[(record.namespace, record.key)] = record

    async def get_record(self, namespace: str, key: str) -> MemoryRecord | None:
        record = self._records.get((namespace, key))
        if record is None or record.is_expired():
            return None
        return record

    async def search_records(self, query: MemoryQuery) -> Sequence[MemorySearchResult]:
        results = [
            MemorySearchResult(record=record, score=_score(record, query.query))
            for record in self._records.values()
            if record.namespace == query.namespace and not record.is_expired()
        ]
        filtered = [result for result in results if result.score > 0 or not query.query.strip()]
        filtered.sort(key=lambda result: (result.score, result.record.importance), reverse=True)
        return tuple(filtered[: query.limit])

    async def cleanup_expired(self) -> int:
        expired = [key for key, record in self._records.items() if record.is_expired()]
        for key in expired:
            del self._records[key]
        return len(expired)


class SqliteMemoryStore:
    """SQLite memory repository for local durable storage."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = RLock()
        self._ensure_schema()

    async def put(self, namespace: str, key: str, value: Mapping[str, Any]) -> None:
        await self.put_record(
            MemoryRecord.create(
                namespace=namespace,
                key=key,
                value=dict(value),
                text=str(value.get("text", "")),
            )
        )

    async def get(self, namespace: str, key: str) -> Mapping[str, Any] | None:
        record = await self.get_record(namespace, key)
        return dict(record.value) if record is not None else None

    async def search(
        self,
        namespace: str,
        query: str,
        limit: int = 10,
    ) -> Sequence[Mapping[str, Any]]:
        results = await self.search_records(MemoryQuery(namespace, query, limit))
        return tuple(result.record.to_dict() for result in results)

    async def put_record(self, record: MemoryRecord) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                insert into memory_records (
                    id, namespace, key, value_json, text, metadata_json, importance,
                    created_at, updated_at, expires_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(namespace, key) do update set
                    id = excluded.id,
                    value_json = excluded.value_json,
                    text = excluded.text,
                    metadata_json = excluded.metadata_json,
                    importance = excluded.importance,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                _record_to_row(record),
            )

    async def get_record(self, namespace: str, key: str) -> MemoryRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                select id, namespace, key, value_json, text, metadata_json, importance,
                       created_at, updated_at, expires_at
                from memory_records
                where namespace = ? and key = ?
                """,
                (namespace, key),
            ).fetchone()
        if row is None:
            return None
        record = _row_to_record(row)
        return None if record.is_expired() else record

    async def search_records(self, query: MemoryQuery) -> Sequence[MemorySearchResult]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                select id, namespace, key, value_json, text, metadata_json, importance,
                       created_at, updated_at, expires_at
                from memory_records
                where namespace = ?
                """,
                (query.namespace,),
            ).fetchall()
        results = [
            MemorySearchResult(record=record, score=_score(record, query.query))
            for record in (_row_to_record(row) for row in rows)
            if not record.is_expired()
        ]
        filtered = [result for result in results if result.score > 0 or not query.query.strip()]
        filtered.sort(key=lambda result: (result.score, result.record.importance), reverse=True)
        return tuple(filtered[: query.limit])

    async def cleanup_expired(self) -> int:
        now = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                "delete from memory_records where expires_at is not null and expires_at <= ?",
                (now,),
            )
            return cursor.rowcount

    def _ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(
                """
                create table if not exists memory_records (
                    id text not null,
                    namespace text not null,
                    key text not null,
                    value_json text not null,
                    text text not null,
                    metadata_json text not null,
                    importance real not null,
                    created_at text not null,
                    updated_at text not null,
                    expires_at text,
                    primary key (namespace, key)
                )
                """
            )
            connection.execute(
                """
                create index if not exists idx_memory_records_namespace
                on memory_records(namespace)
                """
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            try:
                yield connection
                connection.commit()
            finally:
                connection.close()


def _score(record: MemoryRecord, query: str) -> float:
    normalized = query.strip().lower()
    if not normalized:
        return 1.0 + record.importance

    haystack = " ".join(
        [
            record.key,
            record.text,
            json.dumps(dict(record.value), sort_keys=True, default=str),
            json.dumps(dict(record.metadata), sort_keys=True, default=str),
        ]
    ).lower()
    terms = [term for term in normalized.split() if term]
    hits = sum(1 for term in terms if term in haystack)
    exact_bonus = 2 if normalized in haystack else 0
    return float(hits + exact_bonus) + record.importance


def _record_to_row(record: MemoryRecord) -> tuple[Any, ...]:
    return (
        record.id,
        record.namespace,
        record.key,
        json.dumps(dict(record.value), sort_keys=True, default=str),
        record.text,
        json.dumps(dict(record.metadata), sort_keys=True, default=str),
        record.importance,
        record.created_at.isoformat(),
        record.updated_at.isoformat(),
        record.expires_at.isoformat() if record.expires_at else None,
    )


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        id=row["id"],
        namespace=row["namespace"],
        key=row["key"],
        value=json.loads(row["value_json"]),
        text=row["text"],
        metadata=json.loads(row["metadata_json"]),
        importance=float(row["importance"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
    )
