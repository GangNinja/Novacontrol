"""Bounded recovery procedures over registered, reviewable actions.

The recovery manager executes only actions the application registered.  It does
not infer a shell command from a log line, restart by name pattern, or retry
forever.  Each attempt is permission-checked, timeout-bounded, verified by a
live probe, written to the audit trail, and recorded on the incident.  Repeated
failures escalate instead of looping.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from inspect import isawaitable
from typing import Any

from novacontrol.reliability.permissions import PermissionManager
from novacontrol.self_management.incidents import IncidentManager
from novacontrol.self_management.models import (
    AutonomyMode,
    Diagnosis,
    Incident,
    IncidentState,
    RecoveryAction,
    RecoveryAttempt,
    RecoveryStatus,
    utcnow,
)

Handler = Callable[[Incident], "Mapping[str, Any] | str | Awaitable[Mapping[str, Any] | str]"]
Verifier = Callable[
    [Incident],
    "bool | Mapping[str, Any] | Awaitable[bool | Mapping[str, Any]]",
]
AuditWriter = Callable[..., Any]
Publisher = Callable[..., None]


class RecoveryRefused(PermissionError):
    """A recovery was refused for a stated safety, budget or loop reason."""


@dataclass(frozen=True, slots=True)
class RegisteredAction:
    action: RecoveryAction
    handler: Handler
    verify: Verifier


class RecoveryRegistry:
    """The allowlisted recovery actions; no action exists outside this table."""

    def __init__(self) -> None:
        self._actions: dict[str, RegisteredAction] = {}

    def register(self, action: RecoveryAction, handler: Handler, verify: Verifier) -> None:
        if not action.id.strip():
            raise ValueError("a recovery action needs an id")
        if action.id in self._actions:
            raise ValueError(f"recovery action already registered: {action.id}")
        if not action.safe_automatic and action.automatic:
            # Fail closed: an action cannot claim to be automatic while being
            # destructive, irreversible, high-risk or approval-required.
            raise ValueError(
                f"recovery action {action.id!r} claims automatic execution but is not safe to automate"
            )
        self._actions[action.id] = RegisteredAction(action, handler, verify)

    def get(self, action_id: str) -> RegisteredAction:
        try:
            return self._actions[action_id]
        except KeyError as exc:
            raise KeyError(f"Recovery action not found: {action_id}") from exc

    def for_component(self, component: str) -> tuple[RecoveryAction, ...]:
        return tuple(
            row.action
            for row in self._actions.values()
            if row.action.component == component
        )

    def actions(self) -> tuple[RecoveryAction, ...]:
        return tuple(row.action for row in self._actions.values())


class RecoveryManager:
    """Plans and executes bounded recovery, with approval and loop prevention."""

    def __init__(
        self,
        registry: RecoveryRegistry,
        incidents: IncidentManager,
        *,
        permissions: PermissionManager | None,
        audit: AuditWriter | None = None,
        publisher: Publisher | None = None,
        mode: AutonomyMode = AutonomyMode.RECOMMEND,
        paused: bool = False,
        max_actions_per_window: int = 4,
        window_seconds: float = 900.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.registry = registry
        self.incidents = incidents
        self.permissions = permissions
        self._audit = audit
        self._publish = publisher
        self.mode = mode
        self.paused = bool(paused)
        self.max_actions_per_window = max(1, int(max_actions_per_window))
        self.window_seconds = max(1.0, float(window_seconds))
        self._clock = clock or utcnow
        self._executions: list[float] = []
        self._lock = asyncio.Lock()

    # -- policy --------------------------------------------------------------
    def configure(
        self,
        *,
        mode: AutonomyMode | None = None,
        paused: bool | None = None,
        max_actions_per_window: int | None = None,
    ) -> dict[str, Any]:
        if mode is not None:
            self.mode = mode
        if paused is not None:
            self.paused = bool(paused)
        if max_actions_per_window is not None:
            self.max_actions_per_window = max(1, int(max_actions_per_window))
        return self.policy()

    def policy(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "paused": self.paused,
            "max_actions_per_window": self.max_actions_per_window,
            "window_seconds": self.window_seconds,
            "actions": [row.to_dict() for row in self.registry.actions()],
        }

    def recommend(self, incident: Incident) -> tuple[dict[str, Any], ...]:
        rows: list[dict[str, Any]] = []
        for action in self.registry.for_component(incident.component):
            attempts = [
                row
                for row in incident.recovery_attempts
                if row.action_id == action.id
                and row.status is not RecoveryStatus.RUNNING
            ]
            rows.append(
                {
                    **action.to_dict(),
                    "attempts": len(attempts),
                    "remaining_attempts": max(0, action.max_attempts - len(attempts)),
                    "approval_required": (
                        action.requires_approval or not action.safe_automatic
                    ),
                    "available": not self._cooldown_active(incident, action),
                    "cooldown_seconds_remaining": self._cooldown_remaining(incident, action),
                    "reason": (
                        "no attempts remain"
                        if len(attempts) >= action.max_attempts
                        else "in cooldown after a recent attempt"
                        if self._cooldown_active(incident, action)
                        else action.impact
                    ),
                }
            )
        return tuple(rows)

    # -- execution -----------------------------------------------------------
    async def recover(
        self,
        incident_id: str,
        action_id: str,
        *,
        approved: bool = False,
        approved_by: str = "",
    ) -> dict[str, Any]:
        """Execute one action and audit every refusal before it is raised.

        A refusal is an operationally significant event too: an operator asked
        for something the policy would not allow, and the trail must say so.
        The inner body keeps a single refusal point; this wrapper records it.
        """
        incident = self.incidents.get(incident_id)
        registered = self.registry.get(action_id)
        try:
            return await self._recover(
                incident_id,
                action_id,
                approved=approved,
                approved_by=approved_by,
            )
        except RecoveryRefused as exc:
            self._audit_refusal(incident, registered.action, str(exc), approved_by)
            raise

    async def _recover(
        self,
        incident_id: str,
        action_id: str,
        *,
        approved: bool = False,
        approved_by: str = "",
    ) -> dict[str, Any]:
        incident = self.incidents.get(incident_id)
        registered = self.registry.get(action_id)
        action = registered.action
        if action.component != incident.component:
            raise RecoveryRefused(
                f"Action {action.id} is registered for {action.component}, not {incident.component}."
            )
        if incident.state is IncidentState.RESOLVED:
            raise RecoveryRefused(
                "This incident is already resolved; recovery is not appropriate."
            )
        if incident.state is IncidentState.ESCALATED:
            raise RecoveryRefused(
                "This incident is escalated; investigate it before attempting another recovery."
            )
        if self.paused:
            raise RecoveryRefused("Autonomous recovery is paused; resume it before executing an action.")
        if not self.mode.executes:
            raise RecoveryRefused(
                f"Autonomy mode {self.mode.value} cannot execute recovery actions; use controlled or automatic mode."
            )
        automatic = self.mode.automatic and action.safe_automatic
        if not automatic and not approved:
            raise RecoveryRefused(
                "This recovery action requires explicit operator approval before it runs."
            )
        if action.destructive and not approved:
            raise RecoveryRefused(
                "A destructive recovery action always requires explicit operator approval."
            )
        if self.permissions is None:
            raise RecoveryRefused(
                "The permission layer is not wired, so no recovery action may run."
            )
        decision = self.permissions.check(
            f"self_management.{action.id}",
            action=action.id.replace("-", "_"),
            approved=automatic or approved,
        )
        if not decision.allow:
            raise RecoveryRefused(decision.reason)
        if self._cooldown_active(incident, action):
            raise RecoveryRefused(
                f"Recovery action {action.id} is in cooldown; refusing to retry it immediately."
            )
        attempts = [
            row
            for row in incident.recovery_attempts
            if row.action_id == action.id and row.status is not RecoveryStatus.RUNNING
        ]
        if len(attempts) >= action.max_attempts:
            self.incidents.escalate(
                incident.id,
                f"Recovery action {action.id} reached its {action.max_attempts}-attempt ceiling.",
            )
            raise RecoveryRefused(
                f"Recovery action {action.id} has no attempts left; the incident was escalated."
            )
        if self._budget_exhausted():
            self.incidents.escalate(
                incident.id, "The recovery budget for this window is exhausted."
            )
            raise RecoveryRefused(
                "The recovery budget for this window is exhausted; no more actions may run."
            )

        async with self._lock:
            # Re-read: another request may have moved the incident while waiting.
            incident = self.incidents.get(incident_id)
            attempts = [
                row
                for row in incident.recovery_attempts
                if row.action_id == action.id
                and row.status is not RecoveryStatus.RUNNING
            ]
            if len(attempts) >= action.max_attempts:
                raise RecoveryRefused(
                    f"Recovery action {action.id} has no attempts left; the incident was escalated."
                )
            self.incidents.mark_recovery_pending(incident.id)
            started = self._clock()
            running = RecoveryAttempt(
                action_id=action.id,
                status=RecoveryStatus.RUNNING,
                started_at=started,
                approved_by=approved_by if approved else "automatic",
                attempts=len(attempts) + 1,
                before=incident.recovery_attempts[-1].status.value
                if incident.recovery_attempts
                else "healthy",
            )
            self.incidents.record_attempt(incident.id, running)
            self._executions.append(time.monotonic())
            try:
                output = await asyncio.wait_for(
                    self._call_handler(registered, incident),
                    timeout=max(0.1, action.timeout_seconds),
                )
                verified, verification = await asyncio.wait_for(
                    self._call_verifier(registered, self.incidents.get(incident.id)),
                    timeout=max(0.1, action.timeout_seconds),
                )
            except TimeoutError:
                finished = RecoveryAttempt(
                    action_id=action.id,
                    status=RecoveryStatus.TIMED_OUT,
                    started_at=started,
                    finished_at=self._clock(),
                    detail=f"the action exceeded its {action.timeout_seconds:g}s timeout",
                    approved_by=running.approved_by,
                    attempts=running.attempts,
                    before=running.before,
                )
                self.incidents.record_attempt(incident.id, finished)
                self._escalate_if_exhausted(incident.id, action)
                self._audit_recovery(incident, finished, decision.to_dict())
                raise RecoveryRefused(
                    f"Recovery action {action.id} timed out after {action.timeout_seconds:g}s."
                )
            except Exception as exc:
                finished = RecoveryAttempt(
                    action_id=action.id,
                    status=RecoveryStatus.FAILED,
                    started_at=started,
                    finished_at=self._clock(),
                    detail=f"{type(exc).__name__}: {exc}",
                    approved_by=running.approved_by,
                    attempts=running.attempts,
                    before=running.before,
                )
                self.incidents.record_attempt(incident.id, finished)
                self._escalate_if_exhausted(incident.id, action)
                self._audit_recovery(incident, finished, decision.to_dict())
                return {
                    "ok": False,
                    "verified": False,
                    "status": finished.status.value,
                    "detail": finished.detail,
                    "incident": self.incidents.get(incident.id).to_dict(),
                    "action": action.to_dict(),
                }

            finished = RecoveryAttempt(
                action_id=action.id,
                status=RecoveryStatus.SUCCEEDED if verified else RecoveryStatus.FAILED,
                started_at=started,
                finished_at=self._clock(),
                detail=str(verification.get("detail", "")) if isinstance(verification, Mapping) else str(verification),
                approved_by=running.approved_by,
                attempts=running.attempts,
                before=running.before,
                after="healthy" if verified else "still unhealthy",
                verification=dict(verification) if isinstance(verification, Mapping) else {},
            )
            self.incidents.record_attempt(incident.id, finished)
            if not verified:
                self._escalate_if_exhausted(incident.id, action)
            self._audit_recovery(incident, finished, decision.to_dict())
            return {
                "ok": bool(verified),
                "verified": bool(verified),
                "status": finished.status.value,
                "detail": finished.detail,
                "output": dict(output) if isinstance(output, Mapping) else str(output),
                "verification": dict(verification) if isinstance(verification, Mapping) else {},
                "incident": self.incidents.get(incident.id).to_dict(),
                "action": action.to_dict(),
            }

    async def auto_recover(self, incident: Incident) -> dict[str, Any] | None:
        """Run at most one safe automatic action, or leave the recommendation."""
        if not self.mode.automatic or self.paused:
            return None
        if incident.state in (IncidentState.RESOLVED, IncidentState.ESCALATED):
            return None
        for action in self.registry.for_component(incident.component):
            if not action.safe_automatic:
                continue
            if self._cooldown_active(incident, action):
                continue
            attempts = [
                row
                for row in incident.recovery_attempts
                if row.action_id == action.id
                and row.status is not RecoveryStatus.RUNNING
            ]
            if len(attempts) >= action.max_attempts:
                continue
            try:
                return await self.recover(
                    incident.id, action.id, approved=False, approved_by="automatic"
                )
            except RecoveryRefused as exc:
                return {"ok": False, "status": "refused", "detail": str(exc)}
        return None

    async def _call_handler(
        self, registered: RegisteredAction, incident: Incident
    ) -> Mapping[str, Any] | str:
        result = registered.handler(incident)
        if isawaitable(result):
            result = await result
        return result

    async def _call_verifier(
        self, registered: RegisteredAction, incident: Incident
    ) -> tuple[bool, Mapping[str, Any] | str | bool]:
        result = registered.verify(incident)
        if isawaitable(result):
            result = await result
        if isinstance(result, Mapping):
            return bool(result.get("healthy")), result
        return bool(result), result

    # -- loop prevention -----------------------------------------------------
    def _cooldown_active(self, incident: Incident, action: RecoveryAction) -> bool:
        recent = [row for row in incident.recovery_attempts if row.action_id == action.id]
        if not recent:
            return False
        last = max(
            (row.finished_at or row.started_at for row in recent),
            default=None,
        )
        if last is None:
            return False
        return (self._clock() - last).total_seconds() < action.cooldown_seconds

    def _cooldown_remaining(self, incident: Incident, action: RecoveryAction) -> float:
        recent = [row for row in incident.recovery_attempts if row.action_id == action.id]
        if not recent:
            return 0.0
        last = max((row.finished_at or row.started_at for row in recent), default=None)
        if last is None:
            return 0.0
        remaining = action.cooldown_seconds - (self._clock() - last).total_seconds()
        return round(max(0.0, remaining), 1)

    def _budget_exhausted(self) -> bool:
        cutoff = time.monotonic() - self.window_seconds
        self._executions = [item for item in self._executions if item >= cutoff]
        return len(self._executions) >= self.max_actions_per_window

    def _escalate_if_exhausted(self, incident_id: str, action: RecoveryAction) -> None:
        incident = self.incidents.get(incident_id)
        failed = [
            row
            for row in incident.recovery_attempts
            if row.action_id == action.id
            and row.status
            in (RecoveryStatus.FAILED, RecoveryStatus.TIMED_OUT, RecoveryStatus.REFUSED)
        ]
        if len(failed) >= action.max_attempts:
            self.incidents.escalate(
                incident.id,
                f"Recovery action {action.id} failed {len(failed)} time(s); escalation is required.",
            )

    # -- audit ---------------------------------------------------------------
    def _audit_recovery(
        self,
        incident: Incident,
        attempt: RecoveryAttempt,
        permission: Mapping[str, Any],
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit(
                task_id=incident.id,
                user_request=f"Self-management recovery for {incident.component}: {incident.title}",
                source="self_management",
                route="recovery",
                actions=[attempt.action_id],
                recovery=[attempt.to_dict()],
                permission_decisions=[dict(permission)],
                outcome="completed" if attempt.status is RecoveryStatus.SUCCEEDED else "failed",
                failures=[] if attempt.status is RecoveryStatus.SUCCEEDED else [attempt.detail],
            )
        except Exception:
            return

    def _audit_refusal(
        self,
        incident: Incident,
        action: RecoveryAction,
        reason: str,
        approved_by: str,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit(
                task_id=incident.id,
                user_request=f"Self-management recovery for {incident.component}: {incident.title}",
                source="self_management",
                route="recovery",
                actions=[action.id],
                recovery=[
                    {
                        "action_id": action.id,
                        "status": RecoveryStatus.REFUSED.value,
                        "detail": reason,
                        "approved_by": approved_by,
                    }
                ],
                outcome="denied",
                failures=[reason],
            )
        except Exception:
            return


__all__ = [
    "RecoveryManager",
    "RecoveryRefused",
    "RecoveryRegistry",
    "RegisteredAction",
]
