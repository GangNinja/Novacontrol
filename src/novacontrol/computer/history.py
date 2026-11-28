"""Persisted computer-operation history.

The record is for audit and learning, not for replaying a screen.  It stores
what was planned and executed, what was observed *about* the environment, how
the outcome was verified, what the user approved, and how the run ended.  It
deliberately does not store screenshots, clipboard contents, page bodies or
anything that looks like a credential.

Persistence goes through the same ``JsonStateStore`` the rest of the build uses.
A history failure never breaks a running task.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

_SCHEMA = "phase29.computer_history.1"
_DEFAULT_CAP = 50
_SECRET_KEY = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|authorization|cookie|clipboard|credential)",
    re.IGNORECASE,
)
_URL_CREDENTIAL = re.compile(r"(https?://)[^/@\s]+@", re.IGNORECASE)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


class _StateStore(Protocol):
    def read(self, namespace: str) -> Any:  # pragma: no cover - protocol
        ...

    def write(self, namespace: str, value: Any) -> None:  # pragma: no cover - protocol
        ...


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _redact(value: str, *, limit: int = 500) -> str:
    """Remove obvious credentials and bound free text.

    Redaction is intentionally conservative: history is not a place to discover
    that a password happened to be typed into a field, and an email address in a
    page title is still personal data.
    """
    text = str(value or "")
    text = _URL_CREDENTIAL.sub(r"\1[redacted]@", text)
    text = _EMAIL.sub("[redacted-email]", text)
    text = re.sub(
        r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|cookie|set-cookie|credential)\s*[:=]\s*(?:bearer\s+)?\S+",
        r"\1=[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [redacted]",
        text,
    )
    text = " ".join(text.split())
    return text[:limit]


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    """A bounded JSON-safe projection that drops secret-bearing fields."""
    if depth > 4:
        return "[truncated]"
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _SECRET_KEY.search(name):
                clean[name] = "[redacted]"
                continue
            clean[name] = _sanitize(item, depth=depth + 1)
        return clean
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth=depth + 1) for item in value[:40]]
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _redact(str(value))


@dataclass(slots=True)
class ComputerOperationRecord:
    """One durable task record."""

    objective: str
    status: str
    task_id: str = field(default_factory=lambda: uuid4().hex)
    started_at: str = field(default_factory=_utc_now)
    finished_at: str = field(default_factory=_utc_now)
    duration_seconds: float = 0.0
    environment: dict[str, Any] = field(default_factory=dict)
    planned_actions: list[dict[str, Any]] = field(default_factory=list)
    executed_actions: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    recoveries: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    lessons: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    blocked_reason: str = ""

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": _SCHEMA,
            "task_id": self.task_id,
            "objective": _redact(self.objective, limit=300),
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "environment": _sanitize(self.environment),
            "planned_actions": _sanitize(self.planned_actions),
            "executed_actions": _sanitize(self.executed_actions),
            "observations": _sanitize(self.observations),
            "verification": _sanitize(self.verification),
            "recoveries": _sanitize(self.recoveries),
            "approvals": _sanitize(self.approvals),
            "errors": [_redact(error, limit=300) for error in self.errors[-20:]],
            "lessons": _sanitize(self.lessons[-20:]),
            "summary": _redact(self.summary, limit=800),
            "blocked_reason": _redact(self.blocked_reason, limit=300),
            "stores_screenshots": False,
            "stores_secrets": False,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ComputerOperationRecord":
        return cls(
            task_id=str(payload.get("task_id") or uuid4().hex),
            objective=str(payload.get("objective", "")),
            status=str(payload.get("status", "unknown")),
            started_at=str(payload.get("started_at", _utc_now())),
            finished_at=str(payload.get("finished_at", _utc_now())),
            duration_seconds=float(payload.get("duration_seconds", 0.0) or 0.0),
            environment=dict(payload.get("environment", {}) or {}),
            planned_actions=[dict(item) for item in payload.get("planned_actions", []) or []],
            executed_actions=[dict(item) for item in payload.get("executed_actions", []) or []],
            observations=[dict(item) for item in payload.get("observations", []) or []],
            verification=dict(payload.get("verification", {}) or {}),
            recoveries=[dict(item) for item in payload.get("recoveries", []) or []],
            approvals=[dict(item) for item in payload.get("approvals", []) or []],
            errors=[str(item) for item in payload.get("errors", []) or []],
            lessons=[dict(item) for item in payload.get("lessons", []) or []],
            summary=str(payload.get("summary", "")),
            blocked_reason=str(payload.get("blocked_reason", "")),
        )


class ComputerOperationHistory:
    """Bounded, durable, redacted history of computer-operation tasks."""

    def __init__(self, store: _StateStore | None = None, *, cap: int = _DEFAULT_CAP) -> None:
        self._store = store
        self._cap = max(1, int(cap))
        self._records: list[ComputerOperationRecord] = []
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._store is None:
            return
        try:
            snapshot = self._store.read("computer_history")
        except Exception:
            return
        if not isinstance(snapshot, dict):
            return
        for item in snapshot.get("records", []) or []:
            if not isinstance(item, dict):
                continue
            try:
                self._records.append(ComputerOperationRecord.from_dict(item))
            except Exception:
                continue
        self._records = self._records[-self._cap :]

    def _save(self) -> None:
        if self._store is None:
            return
        try:
            self._store.write(
                "computer_history",
                {
                    "schema": _SCHEMA,
                    "records": [record.to_dict() for record in self._records],
                },
            )
        except Exception:
            # A history write must never fail the action that produced it.
            return

    # -- writes --------------------------------------------------------------

    def record(self, record: ComputerOperationRecord) -> ComputerOperationRecord:
        self._records.append(record)
        if len(self._records) > self._cap:
            self._records = self._records[-self._cap :]
        self._save()
        return record

    def record_task(
        self,
        state: dict[str, Any],
        *,
        environment: dict[str, Any] | None = None,
        approvals: list[dict[str, Any]] | None = None,
        duration_seconds: float = 0.0,
    ) -> ComputerOperationRecord:
        """Project an ``AgentTaskState.to_dict()`` into the durable record.

        Observation payloads are reduced to their metadata: no screenshot path,
        no OCR body, no page content.  That is the difference between "what did
        the task see" and "a copy of the user's screen on disk".
        """
        observations = []
        for observation in state.get("observations", []) or []:
            ui_state = dict(observation.get("ui_state") or {}) if isinstance(observation, dict) else {}
            raw_metadata = ui_state.get("metadata")
            metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
            observations.append(
                {
                    "source": str(observation.get("source", "")),
                    "summary": _redact(str(observation.get("summary", "")), limit=250),
                    "at": str(observation.get("at", "")),
                    "window": _redact(str(ui_state.get("window", "")), limit=160),
                    "application": _redact(str(ui_state.get("application", "")), limit=80),
                    "element_count": len(ui_state.get("elements", []) or []),
                    "source_layers": _redact(str(ui_state.get("source", "")), limit=120),
                    "evidence": bool(metadata.get("evidence", False)),
                    "loading": bool(metadata.get("loading", False)),
                    "dialogs": [ _redact(str(item), limit=120) for item in metadata.get("dialogs", []) or [] ],
                    "errors": [ _redact(str(item), limit=120) for item in metadata.get("error_texts", []) or [] ],
                }
            )
        record = ComputerOperationRecord(
            task_id=str(state.get("task_id") or uuid4().hex),
            objective=str(state.get("goal", "")),
            status=str(state.get("status", "unknown")),
            started_at=str(state.get("created_at", _utc_now())),
            finished_at=str(state.get("updated_at", _utc_now())),
            duration_seconds=duration_seconds,
            environment=dict(environment or {}),
            planned_actions=[
                {
                    "id": item.get("id", ""),
                    "kind": item.get("kind", ""),
                    "description": item.get("description", ""),
                    "target": item.get("target", ""),
                    "expected": item.get("expected", ""),
                    "status": item.get("status", ""),
                }
                for item in state.get("plan", []) or []
                if isinstance(item, dict)
            ],
            executed_actions=[
                {
                    "step_id": item.get("step_id", ""),
                    "action": item.get("action", ""),
                    "target": item.get("target", ""),
                    "success": bool(item.get("success", False)),
                    "detail": item.get("detail", ""),
                    "at": item.get("at", ""),
                }
                for item in state.get("actions", []) or []
                if isinstance(item, dict)
            ],
            observations=observations,
            verification=dict(state.get("verification", {}) or {}),
            recoveries=list(state.get("recoveries", []) or []),
            approvals=list(approvals or []),
            errors=[str(item) for item in state.get("errors", []) or []],
            lessons=list(state.get("learned_information", []) or []),
            summary=str(state.get("summary", "")),
        )
        return self.record(record)

    def record_blocked(
        self,
        objective: str,
        *,
        reason: str,
        environment: dict[str, Any] | None = None,
        approvals: list[dict[str, Any]] | None = None,
    ) -> ComputerOperationRecord:
        now = _utc_now()
        return self.record(
            ComputerOperationRecord(
                objective=objective,
                status="blocked",
                started_at=now,
                finished_at=now,
                environment=dict(environment or {}),
                approvals=list(approvals or []),
                blocked_reason=reason,
                summary=f"Task was not started: {reason}",
            )
        )

    # -- reads ---------------------------------------------------------------

    def list(self, *, limit: int = 20, status: str = "") -> list[dict[str, Any]]:
        records = self._records
        if status:
            records = [record for record in records if record.status == status]
        return [record.to_dict() for record in records[-max(1, int(limit)) :]][::-1]

    def get(self, task_id: str) -> dict[str, Any] | None:
        for record in reversed(self._records):
            if record.task_id == task_id:
                return record.to_dict()
        return None

    def clear(self) -> int:
        removed = len(self._records)
        self._records = []
        self._save()
        return removed

    def summary(self) -> dict[str, Any]:
        total = len(self._records)
        completed = sum(1 for record in self._records if record.completed)
        return {
            "total": total,
            "completed": completed,
            "failed": sum(1 for record in self._records if record.status == "failed"),
            "cancelled": sum(1 for record in self._records if record.status == "cancelled"),
            "blocked": sum(1 for record in self._records if record.status == "blocked"),
            "success_rate": round(completed / total, 3) if total else None,
            "cap": self._cap,
            "stores_screenshots": False,
            "stores_secrets": False,
        }

    def last_objective(self, *, unfinished_only: bool = False) -> str:
        for record in reversed(self._records):
            if unfinished_only and record.completed:
                continue
            if record.objective:
                return record.objective
        return ""
