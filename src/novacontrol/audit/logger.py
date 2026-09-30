"""The operational audit logger: what happened, kept locally, and for how long.

Three properties are the whole design:

  * **local only.** The audit trail is stored on this machine, in this machine's
    data directory. A sink that is not local is refused rather than flagged, so
    "the audit stays here" is a property of the code path rather than a promise
    in a docstring.
  * **redacted on the way in.** Values are filtered by
    :class:`~novacontrol.audit.redact.Redactor` BEFORE they are stored, so a
    secret is never written and then cleaned up.
  * **bounded by policy.** A retention period and a hard record cap, both
    applied by :meth:`AuditLogger.prune`, with explicit deletion available for
    the case where a record is wrong and should not wait for an expiry.

The logger stores what it is given and nothing else — it does not reach for
telemetry, the model manager or the planner. The application, which has all of
those, builds each record and hands it over.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from novacontrol.audit.models import AuditRecord, json_safe
from novacontrol.audit.redact import REDACTED, RedactionReport, Redactor

#: Where a JSONL audit trail lives inside an application's data directory.
AUDIT_DIRECTORY = "audit"
AUDIT_FILENAME = "audit.jsonl"


@runtime_checkable
class AuditSink(Protocol):
    """Storage for audit rows.

    ``local`` is not decoration: :class:`AuditLogger` refuses a sink that says it
    is remote, which is what makes "local-only audit" a property of the code and
    not of the operator's configuration discipline.
    """

    name: str
    local: bool

    def append(self, record: Mapping[str, Any]) -> None: ...

    def read(self, limit: int = 0) -> list[Mapping[str, Any]]: ...

    def delete(self, record_id: str) -> bool: ...

    def clear(self) -> int: ...

    def replace(self, records: Sequence[Mapping[str, Any]]) -> None: ...


class InMemoryAuditSink:
    """In-process sink, for tests and for a run with no data directory."""

    name = "memory"
    local = True

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []

    def append(self, record: Mapping[str, Any]) -> None:
        self._records.append(dict(record))

    def read(self, limit: int = 0) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = list(self._records)
        if limit and limit > 0:
            return rows[-limit:]
        return rows

    def delete(self, record_id: str) -> bool:
        for index, row in enumerate(self._records):
            if str(row.get("id")) == str(record_id):
                del self._records[index]
                return True
        return False

    def clear(self) -> int:
        removed = len(self._records)
        self._records.clear()
        return removed

    def replace(self, records: Sequence[Mapping[str, Any]]) -> None:
        self._records = [dict(row) for row in records]


class JsonlAuditSink:
    """Append-only JSONL file — the durable local trail.

    Rewrites (delete, clear, prune) go through a temporary file and an atomic
    replace, so a crash mid-rewrite leaves the previous trail intact rather than
    a half-written one. Reading tolerates a damaged line by skipping it: one
    truncated write must not make the whole trail unreadable.
    """

    name = "jsonl"
    local = True

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, record: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(json_safe(record), ensure_ascii=False) + "\n")

    def read(self, limit: int = 0) -> list[Mapping[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[Mapping[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    rows.append(parsed)
        if limit and limit > 0:
            return rows[-limit:]
        return rows

    def delete(self, record_id: str) -> bool:
        rows = self.read()
        kept = [row for row in rows if str(row.get("id")) != str(record_id)]
        if len(kept) == len(rows):
            return False
        self.replace(kept)
        return True

    def clear(self) -> int:
        removed = len(self.read())
        self.replace([])
        return removed

    def replace(self, records: Sequence[Mapping[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            for row in records:
                handle.write(json.dumps(json_safe(row), ensure_ascii=False) + "\n")
        os.replace(temporary, self.path)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """How much audit history this installation keeps.

    ``retention_days`` and ``max_records`` both default to a real bound rather
    than "forever": an unbounded audit file is a disk-space bug with a privacy
    problem attached. Zero or negative means "no limit" for that dimension, which
    an operator can choose explicitly.
    """

    retention_days: int = 30
    max_records: int = 5000
    redact_sensitive: bool = True
    local_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "retention_days": self.retention_days,
            "max_records": self.max_records,
            "redact_sensitive": self.redact_sensitive,
            "local_only": self.local_only,
        }


class AuditLogger:
    """Records operational audit rows, redacted on the way in and bounded."""

    def __init__(
        self,
        *,
        sink: AuditSink | None = None,
        redactor: Redactor | None = None,
        retention: RetentionPolicy | None = None,
        clock: Any | None = None,
    ) -> None:
        self.retention = retention or RetentionPolicy()
        self.redactor = redactor or Redactor(enabled=self.retention.redact_sensitive)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._records = 0
        self._redaction_total = 0
        self._sink: AuditSink | None = None
        self.attach_sink(sink if sink is not None else InMemoryAuditSink())

    # -- storage ---------------------------------------------------------------

    @property
    def sink(self) -> AuditSink:
        assert self._sink is not None
        return self._sink

    def attach_sink(self, sink: AuditSink) -> None:
        """Point the logger at a sink — refusing one that is not local."""
        if self.retention.local_only and not bool(getattr(sink, "local", False)):
            raise ValueError(
                f"Audit sink {getattr(sink, 'name', '?')!r} is not local, and this "
                "installation audits locally only."
            )
        self._sink = sink
        self._records = len(sink.read())

    # -- writing ---------------------------------------------------------------

    def record(
        self,
        *,
        task_id: str,
        user_request: str,
        outcome: str = "",
        automation_id: str = "",
        source: str = "request",
        route: str = "",
        intent: str = "",
        decision: Mapping[str, Any] | None = None,
        plan: Sequence[Mapping[str, Any]] = (),
        tools: Sequence[str] = (),
        actions: Sequence[str] = (),
        verification: Mapping[str, Any] | None = None,
        failures: Sequence[str] = (),
        recovery: Sequence[Mapping[str, Any]] = (),
        model: str = "",
        provider: str = "",
        latency_ms: float = 0.0,
        resources: Mapping[str, Any] | None = None,
        permission_decisions: Sequence[Mapping[str, Any]] = (),
        timestamp: str = "",
    ) -> AuditRecord:
        """Redact, build, store and (when needed) prune — in that order."""
        request_outcome = self.redactor.redact_text(str(user_request))
        redacted_request = request_outcome.text
        report = RedactionReport(request_outcome.count, request_outcome.kinds)
        structured = {
            "decision": dict(decision or {}),
            "plan": [dict(step) for step in plan],
            "verification": dict(verification or {}),
            "failures": [str(item) for item in failures],
            "recovery": [dict(item) for item in recovery],
            "resources": dict(resources or {}),
            "permission_decisions": [dict(item) for item in permission_decisions],
            "model": str(model),
            "provider": str(provider),
        }
        safe, structured_report = self.redactor.redact_value(structured)
        total = report.count + structured_report.count
        kinds = tuple(dict.fromkeys(report.kinds + structured_report.kinds))
        row = AuditRecord(
            task_id=str(task_id),
            user_request=redacted_request,
            timestamp=str(timestamp) or self._clock().isoformat(),
            automation_id=str(automation_id),
            source=str(source),
            route=str(route),
            intent=str(intent),
            decision=_as_mapping(safe.get("decision")),
            plan=tuple(_as_mapping(item) for item in safe.get("plan") or ()),
            tools=tuple(str(item) for item in tools),
            actions=tuple(str(item) for item in actions),
            verification=_as_mapping(safe.get("verification")),
            failures=tuple(str(item) for item in safe.get("failures") or ()),
            recovery=tuple(_as_mapping(item) for item in safe.get("recovery") or ()),
            model=str(safe.get("model") or ""),
            provider=str(safe.get("provider") or ""),
            latency_ms=float(latency_ms or 0.0),
            resources=_as_mapping(safe.get("resources")),
            permission_decisions=tuple(
                _as_mapping(item) for item in safe.get("permission_decisions") or ()
            ),
            outcome=str(outcome),
            redactions=total,
            redacted_kinds=kinds,
        )
        self.sink.append(row.to_dict())
        self._records += 1
        self._redaction_total += total
        cap = self.retention.max_records
        if cap > 0 and self._records > cap:
            self.prune()
        return row

    # -- reading ---------------------------------------------------------------

    def entries(self, limit: int = 0) -> tuple[AuditRecord, ...]:
        """Stored records, oldest first. Rows this build cannot read are skipped."""
        rows: list[AuditRecord] = []
        for row in self.sink.read(limit):
            try:
                rows.append(AuditRecord.from_dict(row))
            except (TypeError, ValueError):
                continue
        return tuple(rows)

    def tail(self, limit: int = 20) -> tuple[AuditRecord, ...]:
        return self.entries(limit=limit)

    def count(self) -> int:
        return self._records

    # -- deletion and retention ------------------------------------------------

    def delete(self, record_id: str) -> bool:
        """Remove one record by id, immediately. Returns whether one was removed."""
        removed = self.sink.delete(str(record_id))
        if removed:
            self._records = max(0, self._records - 1)
        return removed

    def clear(self) -> int:
        removed = self.sink.clear()
        self._records = 0
        return removed

    def prune(self, now: datetime | None = None) -> int:
        """Apply the retention policy. Returns how many records were removed.

        Two rules, applied together: anything older than the retention period,
        and the oldest records beyond the hard cap. A record with a timestamp
        this build cannot read is kept rather than deleted — an unreadable date
        is not evidence that a record is old.
        """
        clock = now or self._clock()
        rows = list(self.sink.read())
        kept: list[Mapping[str, Any]] = []
        removed = 0
        cutoff = None
        days = self.retention.retention_days
        if days and days > 0:
            cutoff = clock - timedelta(days=days)
        for row in rows:
            stamp = _parse_timestamp(row.get("timestamp"))
            if cutoff is not None and stamp is not None and stamp < cutoff:
                removed += 1
                continue
            kept.append(row)
        cap = self.retention.max_records
        if cap > 0 and len(kept) > cap:
            removed += len(kept) - cap
            kept = kept[-cap:]
        if removed:
            self.sink.replace(kept)
            self._records = len(kept)
        return removed

    # -- reporting -------------------------------------------------------------

    def report(self) -> dict[str, Any]:
        rows = self.sink.read()
        stamps = [str(row.get("timestamp", "")) for row in rows if row.get("timestamp")]
        return {
            "records": len(rows),
            "sink": self.sink.name,
            "local_only": bool(self.retention.local_only),
            "storage": str(getattr(self.sink, "path", "")) or "in-memory",
            "retention": self.retention.to_dict(),
            "redactions_total": self._redaction_total,
            "oldest": min(stamps) if stamps else "",
            "newest": max(stamps) if stamps else "",
            "redaction_marker": REDACTED,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.report()


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


__all__ = [
    "AUDIT_DIRECTORY",
    "AUDIT_FILENAME",
    "AuditLogger",
    "AuditSink",
    "InMemoryAuditSink",
    "JsonlAuditSink",
    "RetentionPolicy",
]
