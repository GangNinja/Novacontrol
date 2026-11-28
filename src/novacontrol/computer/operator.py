"""The Phase 29 computer operator entry point.

One operator, one active desktop task, one approval-aware run.  It composes the
existing `AgenticOrchestrator` rather than adding a loop: planning, perception,
verification and recovery remain there.  The operator contributes lifecycle,
cancellation, device status, contextual resolution, persisted history and the
permission preflight that a user-facing entry point needs.
"""

from __future__ import annotations

import asyncio
import platform as platform_module
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from novacontrol.agentcore.interpreter import TaskInterpreter
from novacontrol.agentcore.orchestrator import AgenticOrchestrator
from novacontrol.agentcore.planner import AdaptivePlanner
from novacontrol.computer.context import ReferenceResolution, resolve_reference
from novacontrol.computer.devices import DeviceRegistry
from novacontrol.computer.history import ComputerOperationHistory
from novacontrol.computer.models import OperationClass, classify_operation


class ComputerTaskBusy(RuntimeError):
    """Raised when a second desktop operation starts while one is running."""


@dataclass(slots=True)
class _OperatorSession:
    id: str
    goal: str
    cancel: asyncio.Event
    started_at: float
    started_iso: str
    task_id: str = ""


@dataclass(slots=True)
class ComputerRunResult:
    """A completed, cancelled or blocked operator run."""

    objective: str
    status: str
    task_id: str = ""
    state: dict[str, Any] = field(default_factory=dict)
    history: dict[str, Any] | None = None
    reason: str = ""

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "status": self.status,
            "task_id": self.task_id,
            "state": self.state,
            "history": self.history,
            "reason": self.reason,
        }


class ComputerOperator:
    """Runs and supervises one context-aware computer task at a time."""

    def __init__(
        self,
        *,
        orchestrator: AgenticOrchestrator,
        perception: Any | None = None,
        history: ComputerOperationHistory | None = None,
        devices: DeviceRegistry | None = None,
        permissions: Any | None = None,
        interpreter: TaskInterpreter | None = None,
        planner: AdaptivePlanner | None = None,
        desktop_runner: Any | None = None,
        desktop_controller: Any | None = None,
        browser_controller: Any | None = None,
        on_complete: Any | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._perception = perception if perception is not None else orchestrator.perception
        self.history = history or ComputerOperationHistory()
        self.devices = devices or DeviceRegistry(desktop_runner=desktop_runner)
        self._permissions = permissions
        self._interpreter = interpreter or orchestrator.interpreter
        self._planner = planner or orchestrator.planner
        self._desktop_controller = desktop_controller
        self._browser_controller = browser_controller
        self._desktop = desktop_runner or getattr(desktop_controller, "runner", None)
        self._on_complete = on_complete
        self._lock = asyncio.Lock()
        self._sessions: dict[str, _OperatorSession] = {}
        self._last_result: dict[str, Any] | None = None
        self._stop_count = 0

    # -- planning and observation -------------------------------------------

    async def plan(self, objective: str) -> dict[str, Any]:
        """Describe the work and its risk without executing anything."""
        clean = " ".join(str(objective or "").strip().split())
        if not clean:
            raise ValueError("A computer objective is required.")
        interpretation = await self._interpreter.interpret(clean)
        plan = self._planner.create_plan(clean, interpretation=interpretation.to_dict())
        operation_class = classify_operation(clean)
        decision = None
        if self._permissions is not None:
            decision = self._permissions.assess(action=clean)
        requires_confirmation = bool(
            operation_class in (OperationClass.SENSITIVE, OperationClass.DESTRUCTIVE, OperationClass.PRIVILEGED)
            or (decision is not None and decision.requires_confirmation)
        )
        return {
            "objective": clean,
            "operation_class": operation_class.value,
            "requires_confirmation": requires_confirmation,
            "approval": "required" if requires_confirmation else "not required for this plan",
            "interpretation": interpretation.to_dict(),
            "plan": plan.to_dict(),
            "transport": "agentcore/observe-plan-act-observe-verify-recover",
        }

    async def observe(self, *, include_semantic: bool = True) -> dict[str, Any]:
        """A read-only observation with live device availability."""
        devices = await self.devices.discover()
        try:
            state = await self._perception.observe(
                include_desktop=True,
                include_dom=True,
                include_semantic=include_semantic,
            )
            observation = state.to_dict()
        except Exception as exc:
            observation = {
                "application": "unknown",
                "source": "failed",
                "metadata": {"error": f"{type(exc).__name__}: {exc}"},
            }
        return {
            "observation": observation,
            "devices": devices.to_dict(),
            "supported_operations": list(devices.supported_operations),
        }

    def resolve(
        self,
        text: str,
        *,
        ui_state: Any | None = None,
        history_objective: str | None = None,
    ) -> ReferenceResolution:
        objective = self.history.last_objective() if history_objective is None else history_objective
        unfinished = self.history.last_objective(unfinished_only=True)
        return resolve_reference(
            text,
            ui_state=ui_state,
            history_objective=objective,
            history_unfinished=unfinished,
        )

    # -- execution -----------------------------------------------------------

    async def run(
        self,
        objective: str,
        *,
        context: dict[str, Any] | None = None,
        approved: bool = False,
        correlation_id: str = "",
    ) -> ComputerRunResult:
        """Run the existing closed loop under operator supervision.

        A plan classified sensitive/destructive/privileged must be approved by
        the caller.  The API obtains that decision from a server-minted token
        that the user reviewed; this method never self-approves.
        """
        clean = " ".join(str(objective or "").strip().split())
        if not clean:
            raise ValueError("A computer objective is required.")

        environment = await self._environment()
        operation_class = classify_operation(clean)
        if operation_class in (OperationClass.SENSITIVE, OperationClass.DESTRUCTIVE, OperationClass.PRIVILEGED) and not approved:
            reason = (
                f"This objective is classified {operation_class.value} and requires explicit approval "
                "before any computer input is sent."
            )
            record = self.history.record_blocked(
                clean,
                reason=reason,
                environment=environment,
                approvals=[{"required": True, "approved": False, "source": "operator_preflight"}],
            )
            raise PermissionError(reason)

        if self._lock.locked():
            reason = "Another computer operation is already running; it was not interrupted by this request."
            self.history.record_blocked(clean, reason=reason, environment=environment)
            raise ComputerTaskBusy(reason)

        async with self._lock:
            session = _OperatorSession(
                id=uuid4().hex,
                goal=clean,
                cancel=asyncio.Event(),
                started_at=time.monotonic(),
                started_iso=datetime.now(UTC).isoformat(),
            )
            self._sessions[session.id] = session
            started = time.monotonic()
            # The API has already validated a single-use, server-minted token
            # for this exact objective before calling approved=True. While THIS
            # run is in flight, the desktop controller receives that approval
            # per action, so each action still passes its permission and audit
            # layers instead of reaching the raw runner.
            previous_gateway = getattr(self._desktop_controller, "approval_gateway", None)
            previous_browser_gateway = getattr(self._browser_controller, "approval_gateway", None)
            if approved:
                from novacontrol.application_helpers import ApprovedApprovalGateway

                approved_gateway = ApprovedApprovalGateway()
                if self._desktop_controller is not None:
                    self._desktop_controller.approval_gateway = approved_gateway
                if self._browser_controller is not None:
                    self._browser_controller.approval_gateway = approved_gateway
            try:
                state = await self._orchestrator.run_task(
                    clean,
                    context=dict(context or {}),
                    cancel_event=session.cancel,
                )
                session.task_id = state.id
                state_dict = state.to_dict()
                approvals = [
                    {
                        "required": operation_class
                        in (OperationClass.SENSITIVE, OperationClass.DESTRUCTIVE, OperationClass.PRIVILEGED),
                        "approved": approved,
                        "source": "server_plan_token" if approved else "no confirmation required",
                    }
                ]
                record = self.history.record_task(
                    state_dict,
                    environment=environment,
                    approvals=approvals,
                    duration_seconds=time.monotonic() - started,
                )
                if self._on_complete is not None:
                    try:
                        await self._on_complete(state, record)
                    except Exception:
                        pass
                result = ComputerRunResult(
                    objective=clean,
                    status=str(state_dict.get("status", "unknown")),
                    task_id=state.id,
                    state=state_dict,
                    history=record.to_dict(),
                )
                self._last_result = result.to_dict()
                return result
            except asyncio.CancelledError:
                # External cancellation of the awaiting task is also a stop.
                session.cancel.set()
                await self._release_input()
                raise
            except Exception as exc:
                # A loop that raises before returning a state is still a task
                # that ran. Record it as failed instead of losing the only
                # durable evidence of what happened.
                reason = f"{type(exc).__name__}: {exc}"
                failed_state = {
                    "task_id": session.id,
                    "goal": clean,
                    "status": "failed",
                    "created_at": session.started_iso,
                    "updated_at": datetime.now(UTC).isoformat(),
                    "plan": [],
                    "actions": [],
                    "observations": [],
                    "verification": {"final": {"status": "error", "reason": reason}},
                    "recoveries": [],
                    "errors": [reason],
                    "learned_information": [],
                    "summary": f"Task failed before the loop returned a state. {reason}",
                }
                record = self.history.record_task(
                    failed_state,
                    environment=environment,
                    approvals=[
                        {
                            "required": operation_class
                            in (OperationClass.SENSITIVE, OperationClass.DESTRUCTIVE, OperationClass.PRIVILEGED),
                            "approved": approved,
                            "source": "server_plan_token" if approved else "no confirmation required",
                        }
                    ],
                    duration_seconds=time.monotonic() - started,
                )
                self._last_result = ComputerRunResult(
                    objective=clean,
                    status="failed",
                    task_id=session.id,
                    history=record.to_dict(),
                    reason=reason,
                ).to_dict()
                raise
            finally:
                if self._desktop_controller is not None and previous_gateway is not None:
                    self._desktop_controller.approval_gateway = previous_gateway
                if self._browser_controller is not None and previous_browser_gateway is not None:
                    self._browser_controller.approval_gateway = previous_browser_gateway
                self._sessions.pop(session.id, None)

    async def cancel(self, task_id: str = "") -> dict[str, Any]:
        """Ask one task, or every active task, to stop before its next action."""
        selected = [
            session
            for session in self._sessions.values()
            if not task_id or session.task_id == task_id or session.id == task_id
        ]
        for session in selected:
            session.cancel.set()
        released = await self._release_input()
        self._stop_count += 1 if selected else 0
        return {
            "cancelled": bool(selected),
            "tasks": [session.id for session in selected],
            "input": released,
            "reason": "The loop stops before its next action; the input guard releases anything already held.",
        }

    async def emergency_stop(self) -> dict[str, Any]:
        """Immediate safe stop: cancel the task and release every held input."""
        result = await self.cancel()
        result["emergency_stop"] = True
        result["at"] = datetime.now(UTC).isoformat()
        return result

    async def _release_input(self) -> dict[str, Any]:
        target = self._desktop_controller or self._desktop
        if target is None:
            return {"released": False, "reason": "no desktop runner is wired"}
        stopper = getattr(target, "stop_input", None)
        if stopper is None:
            return {"released": False, "reason": "the desktop runner exposes no input stop seam"}
        try:
            result = stopper()
            if hasattr(result, "__await__"):
                result = await result
            return {"released": True, **(dict(result) if isinstance(result, dict) else {"result": result})}
        except Exception as exc:
            return {"released": False, "reason": f"{type(exc).__name__}: {exc}"}

    def status(self) -> dict[str, Any]:
        """Synchronous supervisor view; device probing is the async path."""
        active = [
            {"session_id": session.id, "task_id": session.task_id, "goal": session.goal, "started_at": session.started_iso}
            for session in self._sessions.values()
        ]
        return {
            "active": active,
            "active_count": len(active),
            "busy": self._lock.locked(),
            "emergency_stops": self._stop_count,
            "history": self.history.summary(),
            "last_result": self._last_result,
            "loop": "observe → interpret → plan → act → observe → verify → recover",
            "closed_loop": True,
        }

    async def _environment(self) -> dict[str, Any]:
        try:
            report = await self.devices.discover()
            return {
                "platform": report.platform,
                "devices": [
                    {
                        "id": device.id,
                        "kind": device.kind.value,
                        "available": device.available,
                        "connected": device.connected,
                        "supported_operations": list(device.supported_operations),
                    }
                    for device in report.devices
                ],
            }
        except Exception:
            return {"platform": platform_module.platform()}

    async def close(self) -> None:
        await self.cancel()
