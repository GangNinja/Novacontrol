"""What the audit trail records, in one shape.

The phase asks for a record of a task's whole life: the request, the intent, the
decision, the plan, the tools and actions, the verification, the failures, the
recovery, the model, the latency, the resource usage, and the permission
decisions. All of it is here, and NOTHING about how any of it was reasoned
about: there is no chain-of-thought field, no prompt, and no model output. What
is stored is operational metadata — identifiers, names, statuses, numbers.

That is a design decision rather than an omission. An audit trail is kept for a
long time and read by more people than a debug log; a free-text field of a
model's reasoning would be the largest privacy surface in the system and the
least useful thing in the file. The named fields are what a question about "what
did this system do, under whose authority, and how did it end" is actually
answered from.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4


def json_safe(value: Any, *, depth: int = 0) -> Any:
    """A JSON-serializable copy of a value, for a record that must round-trip.

    Anything the JSON encoder cannot represent becomes its ``str``: an audit
    record that fails to persist is worse than one carrying a label where an
    object was.
    """
    if depth > 8:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): json_safe(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item, depth=depth + 1) for item in value]
    return str(value)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One request or one automation run, as an operational fact."""

    #: The task record this request created (the correlation id every event of
    #: the request carried), so the audit row and the event thread agree.
    task_id: str
    #: What was asked, already redacted. Never the model's answer.
    user_request: str
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    #: Which automation triggered this, when one did.
    automation_id: str = ""
    #: What caused the record: a spoken request, or a scheduled run.
    source: str = "request"
    route: str = ""
    intent: str = ""
    decision: Mapping[str, Any] = field(default_factory=dict)
    plan: tuple[Mapping[str, Any], ...] = ()
    tools: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    verification: Mapping[str, Any] = field(default_factory=dict)
    failures: tuple[str, ...] = ()
    recovery: tuple[Mapping[str, Any], ...] = ()
    model: str = ""
    provider: str = ""
    latency_ms: float = 0.0
    resources: Mapping[str, Any] = field(default_factory=dict)
    permission_decisions: tuple[Mapping[str, Any], ...] = ()
    outcome: str = ""
    #: How many sensitive values were replaced on the way in, and of what kinds.
    #: Recorded because "this request carried a credential" is itself useful
    #: operational information — the credential is what is missing, not the fact.
    redactions: int = 0
    redacted_kinds: tuple[str, ...] = ()
    id: str = field(default_factory=lambda: uuid4().hex)

    @property
    def ok(self) -> bool:
        return self.outcome == "completed"

    def to_dict(self) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            json_safe(
                {
                    "id": self.id,
                    "task_id": self.task_id,
                    "automation_id": self.automation_id,
                    "timestamp": self.timestamp,
                    "source": self.source,
                    "user_request": self.user_request,
                    "route": self.route,
                    "intent": self.intent,
                    "decision": dict(self.decision),
                    "plan": [dict(step) for step in self.plan],
                    "tools": list(self.tools),
                    "actions": list(self.actions),
                    "verification": dict(self.verification),
                    "failures": list(self.failures),
                    "recovery": [dict(item) for item in self.recovery],
                    "model": self.model,
                    "provider": self.provider,
                    "latency_ms": self.latency_ms,
                    "resources": dict(self.resources),
                    "permission_decisions": [
                        dict(item) for item in self.permission_decisions
                    ],
                    "outcome": self.outcome,
                    "redactions": self.redactions,
                    "redacted_kinds": list(self.redacted_kinds),
                }
            ),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AuditRecord:
        def _tuple_of_mappings(value: Any) -> tuple[Mapping[str, Any], ...]:
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                return ()
            return tuple(item for item in value if isinstance(item, Mapping))

        return cls(
            id=str(payload.get("id") or uuid4().hex),
            task_id=str(payload.get("task_id", "")),
            automation_id=str(payload.get("automation_id", "")),
            timestamp=str(payload.get("timestamp", "")),
            source=str(payload.get("source", "request")),
            user_request=str(payload.get("user_request", "")),
            route=str(payload.get("route", "")),
            intent=str(payload.get("intent", "")),
            decision=dict(payload.get("decision") or {}),
            plan=_tuple_of_mappings(payload.get("plan")),
            tools=tuple(str(item) for item in payload.get("tools") or ()),
            actions=tuple(str(item) for item in payload.get("actions") or ()),
            verification=dict(payload.get("verification") or {}),
            failures=tuple(str(item) for item in payload.get("failures") or ()),
            recovery=_tuple_of_mappings(payload.get("recovery")),
            model=str(payload.get("model", "")),
            provider=str(payload.get("provider", "")),
            latency_ms=float(payload.get("latency_ms") or 0.0),
            resources=dict(payload.get("resources") or {}),
            permission_decisions=_tuple_of_mappings(payload.get("permission_decisions")),
            outcome=str(payload.get("outcome", "")),
            redactions=int(payload.get("redactions") or 0),
            redacted_kinds=tuple(str(item) for item in payload.get("redacted_kinds") or ()),
        )
