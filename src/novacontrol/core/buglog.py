"""Bug log: file-based record of what failed, where, and when.

Every failed action (desktop, phone, browser, vision-guided) that records a
bug lands here as a structured entry the user can review and fix later:

    {"id": ..., "what": "...", "where": "desktop: Open steam",
     "when": "2026-09-06T12:34:56Z", "status": "open", "details": {...}}

Stored as JSON in the app's data directory so it survives restarts and can be
opened directly as a file. `NovaControlApplication.record_bug` is the single
entry point; the Vision panel and /bugs API are readers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(slots=True)
class BugRecord:
    """One observed failure: what happened, where, and when."""

    what: str
    where: str
    when: str = field(default_factory=_utc_now)
    status: str = "open"  # open | fixed
    details: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex[:12])

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "what": self.what,
            "where": self.where,
            "when": self.when,
            "status": self.status,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BugRecord":
        return cls(
            what=str(payload.get("what", "unknown failure")),
            where=str(payload.get("where", "unknown")),
            when=str(payload.get("when", _utc_now())),
            status=str(payload.get("status", "open")),
            details=dict(payload.get("details") or {}),
            id=str(payload.get("id") or uuid4().hex[:12]),
        )


class BugLog:
    """Append-only JSON bug log with open/fixed bookkeeping."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._records: list[BugRecord] = []
        self._load()

    # -- persistence -----------------------------------------------------------

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            records = payload.get("bugs", []) if isinstance(payload, dict) else []
            self._records = [BugRecord.from_dict(item) for item in records]
        except (OSError, ValueError):
            self._records = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"bugs": [r.to_dict() for r in self._records]}, indent=2),
            encoding="utf-8",
        )

    # -- API -------------------------------------------------------------------

    def record(
        self,
        what: str,
        *,
        where: str,
        details: dict[str, Any] | None = None,
    ) -> BugRecord:
        """Record a failure. Returns the stored record."""
        entry = BugRecord(what=what, where=where, details=details or {})
        self._records.insert(0, entry)
        del self._records[200:]  # cap the file; oldest bugs roll off
        self._save()
        return entry

    def all(self, *, include_fixed: bool = True) -> tuple[BugRecord, ...]:
        records = tuple(self._records)
        if include_fixed:
            return records
        return tuple(r for r in records if r.status == "open")

    def open_count(self) -> int:
        return sum(1 for r in self._records if r.status == "open")

    def mark_fixed(self, bug_id: str) -> BugRecord | None:
        for record in self._records:
            if record.id == bug_id:
                record.status = "fixed"
                self._save()
                return record
        return None

    def resolve_matching(
        self,
        *,
        where: str,
        label: str,
        evidence: dict[str, Any],
    ) -> int:
        """Auto-resolve open bugs for one flow when fresh proof it works arrives.

        Called after a verified successful action (a guided click whose
        pixel-diff verification passed): any still-open bug recorded against
        the same ``where`` for the same label is marked fixed, with the new
        evidence embedded under ``details.auto_resolved`` — the log then shows
        the failure was transient and was later confirmed working, including
        when and with what proof. Legacy records that carry the label only in
        their ``what`` text (no ``details.label``) are matched too.

        Returns how many records were resolved.
        """
        target = label.strip().lower()
        resolved = 0
        for record in self._records:
            if record.status != "open" or record.where != where:
                continue
            record_label = str(record.details.get("label", "")).strip().lower()
            if record_label == target or f"'{target}'" in record.what.lower():
                record.status = "fixed"
                record.details = {
                    **record.details,
                    "auto_resolved": {**evidence, "resolved_at": _utc_now()},
                }
                resolved += 1
        if resolved:
            self._save()
        return resolved

    def clear_fixed(self) -> int:
        before = len(self._records)
        self._records = [r for r in self._records if r.status != "fixed"]
        removed = before - len(self._records)
        if removed:
            self._save()
        return removed

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "open_count": self.open_count(),
            "bugs": [r.to_dict() for r in self._records],
        }
