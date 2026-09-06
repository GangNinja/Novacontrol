"""Standard result envelope: ONE result shape across all subsystems.

The orchestrator reasons about `StandardResult` regardless of whether the
executor was JARVIS, research, browser, desktop, phone, or agentcore.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class ResultStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    DENIED = "denied"
    CLARIFICATION = "clarification"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class StandardResult:
    task_id: str
    status: ResultStatus
    intent: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    next_action: str | None = None
    summary: str = ""
    id: str = field(default_factory=lambda: uuid4().hex)

    @property
    def verified(self) -> bool:
        return bool(self.verification.get("verified"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "status": self.status.value,
            "intent": self.intent,
            "result": self.result,
            "evidence": self.evidence,
            "verification": {**self.verification, "verified": self.verified},
            "errors": self.errors,
            "next_action": self.next_action,
            "summary": self.summary,
        }

    @classmethod
    def from_payload(
        cls,
        task_id: str,
        payload: dict[str, Any],
        *,
        intent: str = "",
        status: ResultStatus | None = None,
        summary: str = "",
    ) -> "StandardResult":
        """Adapt a legacy subsystem payload into the standard envelope.

        Subsystems keep their native payload under `result`; status/verification
        are read from common conventions without forcing rewrites.
        """
        resolved_status = status
        if resolved_status is None:
            detail = str(payload.get("status", ""))
            if detail in ("waiting_for_approval", "waiting_for_phone_bridge", "not_an_action"):
                resolved_status = ResultStatus.CLARIFICATION if detail == "not_an_action" else ResultStatus.PARTIAL
            elif detail == "executed":
                resolved_status = ResultStatus.SUCCESS
            else:
                resolved_status = ResultStatus.SUCCESS
        errors = [str(payload["error"])] if payload.get("error") else []
        verification = {"verified": False, "strategy": payload.get("verification_strategy", "")}
        if payload.get("verified") is True:
            verification["verified"] = True
        return cls(
            task_id=task_id,
            status=resolved_status,
            intent=intent,
            result=dict(payload),
            errors=errors,
            verification=verification,
            summary=summary or str(payload.get("summary", "")),
        )
