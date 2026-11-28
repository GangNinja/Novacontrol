"""The Phase 34 coordinator: health -> diagnosis -> incident -> recovery.

This service composes the existing diagnostic roster, permission layer,
audit trail, state store, Phase 30 notifications and Phase 27 learning seam.
It never edits source code, installs dependencies, runs arbitrary commands from
logs, or performs a destructive maintenance action without a named approval.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from inspect import isawaitable
from typing import Any

from novacontrol.self_management.configuration import ConfigurationManager
from novacontrol.self_management.diagnosis import diagnose, explain_unknown
from novacontrol.self_management.health import HealthCollector
from novacontrol.self_management.incidents import IncidentManager
from novacontrol.self_management.maintenance import MaintenanceService
from novacontrol.self_management.models import (
    AutonomyMode,
    CauseConfidence,
    Diagnosis,
    HealthSnapshot,
    Incident,
    IncidentState,
    MaintenanceReport,
    RecoveryStatus,
    utcnow,
)
from novacontrol.self_management.recovery import RecoveryManager, RecoveryRefused

AuditWriter = Callable[..., Any]
Notify = Callable[..., Any]
ApplyConfiguration = Callable[[Mapping[str, Any]], Any]


class SelfManagementService:
    """One control plane over health, diagnostics, recovery and maintenance."""

    def __init__(
        self,
        *,
        health: HealthCollector,
        incidents: IncidentManager,
        recovery: RecoveryManager,
        maintenance: MaintenanceService,
        configuration: ConfigurationManager,
        policy_store: Any | None = None,
        audit: AuditWriter | None = None,
        notify: Notify | None = None,
        apply_configuration: ApplyConfiguration | None = None,
        learning: Callable[[Mapping[str, Any]], Any] | None = None,
        learning_advice: Callable[[str], Mapping[str, Any]] | None = None,
        agent_advice: Callable[[str, Sequence[str]], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.health = health
        self.incidents = incidents
        self.recovery = recovery
        self.maintenance = maintenance
        self.configuration = configuration
        self._policy_store = policy_store
        self._audit = audit
        self._notify = notify
        self._apply_configuration = apply_configuration
        self._learning = learning
        self._learning_advice = learning_advice
        self._agent_advice = agent_advice
        self._learning_reports: list[dict[str, Any]] = []
        self._clock = clock or utcnow
        self._lock = asyncio.Lock()
        self.checks = 0
        self.last_check_at: datetime | None = None
        self.last_diagnoses: tuple[Diagnosis, ...] = ()
        self.last_cycle: dict[str, Any] = {}
        self._load_policy()

    def _load_policy(self) -> None:
        """Restore the operator's autonomy choice and pause across restarts."""
        if self._policy_store is None:
            return
        try:
            payload = self._policy_store.read("self_management_policy")
        except Exception:
            # A corrupt policy store must not stop the application from booting;
            # the safe default is the in-memory CONTROLLED policy.
            return
        if not isinstance(payload, Mapping):
            return
        mode = payload.get("mode")
        parsed_mode: AutonomyMode | None = None
        if isinstance(mode, str):
            try:
                parsed_mode = AutonomyMode(mode)
            except ValueError:
                parsed_mode = None
        try:
            self.recovery.configure(
                mode=parsed_mode,
                paused=payload.get("paused")
                if isinstance(payload.get("paused"), bool)
                else None,
                max_actions_per_window=payload.get("max_actions_per_window")
                if isinstance(payload.get("max_actions_per_window"), int)
                else None,
            )
        except Exception:
            return

    def _save_policy(self) -> None:
        if self._policy_store is None:
            return
        try:
            self._policy_store.write(
                "self_management_policy",
                {
                    "mode": self.recovery.mode.value,
                    "paused": self.recovery.paused,
                    "max_actions_per_window": self.recovery.max_actions_per_window,
                },
            )
        except Exception:
            return

    # -- monitoring ----------------------------------------------------------
    async def health_check(
        self, *, only: tuple[str, ...] | None = None, explain: bool = True
    ) -> dict[str, Any]:
        """Run the real roster and optionally turn findings into incidents."""
        async with self._lock:
            snapshot = await self.health.collect(only=only)
            config_report = self.configuration.report()
            issue = "" if config_report.valid else "; ".join(config_report.issues)
            diagnoses = diagnose(snapshot, configuration_issue=issue)
            self.checks += 1
            self.last_check_at = self._clock()
            self.last_diagnoses = diagnoses
            opened: tuple[Incident, ...] = ()
            if explain and only is None:
                opened = self.incidents.observe(diagnoses, snapshot)
                self.incidents.dismiss_healthy(
                    snapshot,
                    active_components={row.component for row in diagnoses},
                )
                for incident in self.incidents.list(open_only=True, limit=20):
                    if incident.component in {row.component for row in snapshot.unhealthy}:
                        self.recovery.recommend(incident)
                self._notify_new(opened)
            self.last_cycle = {
                "snapshot_id": snapshot.snapshot_id,
                "overall": snapshot.overall.value,
                "diagnoses": len(diagnoses),
                "incidents_changed": len(opened),
                "checked_at": self._clock().isoformat(),
            }
            return {
                "health": snapshot.to_dict(),
                "diagnoses": [row.to_dict() for row in diagnoses],
                "incidents": [row.to_dict() for row in opened],
                "configuration": config_report.to_dict(),
            }

    async def monitor(self) -> dict[str, Any]:
        """One background pass: detect, then auto-recover only safe actions."""
        result = await self.health_check(explain=True)
        recovered: list[dict[str, Any]] = []
        if self.recovery.mode.automatic and not self.recovery.paused:
            for incident in self.incidents.list(open_only=True, limit=20):
                outcome = await self.recovery.auto_recover(incident)
                if outcome is not None:
                    recovered.append(outcome)
                    self._record_learning(incident.id, outcome, automatic=True)
        result["automatic_recovery"] = recovered
        return result

    def _notify_new(self, incidents: Sequence[Incident]) -> None:
        if self._notify is None:
            return
        for incident in incidents:
            try:
                self._notify(
                    title=f"NovaControl incident: {incident.component}",
                    body=(
                        f"{incident.title} "
                        f"(severity {incident.severity.value}, "
                        f"confidence {incident.cause_confidence.value})."
                    ),
                    severity=incident.severity.value,
                    dedupe_key=f"incident:{incident.id}",
                    evidence=incident.evidence,
                    incident_id=incident.id,
                )
            except Exception:
                continue

    # -- investigation -------------------------------------------------------
    async def investigate(
        self, incident_id: str, *, explain: bool = True, use_agent: bool = False
    ) -> dict[str, Any]:
        incident = self.incidents.get(incident_id)
        self.incidents.mark_investigating(incident.id)
        snapshot = await self.health.collect()
        config_report = self.configuration.report()
        issue = "" if config_report.valid else "; ".join(config_report.issues)
        findings = diagnose(snapshot, configuration_issue=issue)
        matching = next(
            (row for row in findings if row.component == incident.component), None
        )
        if matching is None:
            matching = explain_unknown(incident.component, snapshot)
        updated = self.incidents.apply_diagnosis(incident.id, matching)
        agent_report: Mapping[str, Any] = {}
        if use_agent and self._agent_advice is not None:
            try:
                advisory = self._agent_advice(matching.component, matching.evidence)
                if isawaitable(advisory):
                    advisory = await advisory
                agent_report = dict(advisory) if isinstance(advisory, Mapping) else {}
            except Exception as exc:
                agent_report = {
                    "available": False,
                    "reason": f"the advisory agent failed: {type(exc).__name__}",
                }
        return {
            "incident": updated.to_dict(),
            "diagnosis": matching.to_dict(),
            "health": snapshot.to_dict(),
            "evidence": list(updated.evidence),
            "agent_advice": agent_report,
            "authority": "diagnosis and recovery authority remain with the deterministic layer and the permission system",
        }

    def recommend(self, incident_id: str) -> dict[str, Any]:
        incident = self.incidents.get(incident_id)
        if incident.state in (IncidentState.RESOLVED,):
            raise ValueError("A resolved incident has no recovery to recommend.")
        if incident.state is IncidentState.ESCALATED:
            raise ValueError("An escalated incident needs operator investigation before retry.")
        self.incidents.mark_recovery_pending(incident.id)
        actions = self.recovery.recommend(self.incidents.get(incident.id))
        learning: Mapping[str, Any] = {}
        if self._learning_advice is not None:
            try:
                learning = dict(
                    self._learning_advice(
                        f"recover {incident.component}: {incident.title}"
                    )
                )
            except Exception:
                learning = {}
        return {
            "incident": self.incidents.get(incident.id).to_dict(),
            "actions": [dict(row) for row in actions],
            "approval_required": any(bool(row.get("approval_required")) for row in actions),
            "learning": learning,
            "note": (
                "Recommendations are not execution. Choose an action and approve it explicitly."
                if actions
                else "No registered safe recovery action exists for this component; investigation or escalation is required."
            ),
        }

    # -- recovery ------------------------------------------------------------
    async def recover(
        self,
        incident_id: str,
        action_id: str,
        *,
        approved: bool = False,
        approved_by: str = "",
    ) -> dict[str, Any]:
        try:
            result = await self.recovery.recover(
                incident_id, action_id, approved=approved, approved_by=approved_by
            )
        except RecoveryRefused as exc:
            self._record_learning(
                incident_id,
                {
                    "ok": False,
                    "status": "refused",
                    "detail": str(exc),
                    "verified": False,
                },
                automatic=False,
                action_id=action_id,
            )
            raise
        self._record_learning(
            incident_id, result, automatic=False, action_id=action_id
        )
        return result

    def _record_learning(
        self,
        incident_id: str,
        outcome: Mapping[str, Any],
        *,
        automatic: bool,
        action_id: str = "",
    ) -> None:
        """Record a VERIFIED outcome for Phase 27; never promotes an action."""
        if self._learning is None:
            return
        try:
            incident = self.incidents.get(incident_id)
        except KeyError:
            return
        payload = {
            "incident_id": incident.id,
            "component": incident.component,
            "title": incident.title,
            "action_id": action_id or str(outcome.get("action_id", "")),
            "status": str(outcome.get("status", "")),
            "verified": bool(outcome.get("verified", False)),
            "detail": str(outcome.get("detail", ""))[:500],
            "automatic": bool(automatic),
            "confidence": incident.cause_confidence.value,
            "severity": incident.severity.value,
        }
        try:
            report = self._learning(payload)
            self._learning_reports.append(
                {
                    "incident_id": incident.id,
                    "action_id": payload["action_id"],
                    "verified": payload["verified"],
                    "recorded": bool(
                        report.get("recorded", True)
                        if isinstance(report, Mapping)
                        else True
                    ),
                }
            )
            self._learning_reports = self._learning_reports[-20:]
        except Exception:
            return

    def pause(self, *, reason: str = "") -> dict[str, Any]:
        self.recovery.configure(paused=True)
        self._save_policy()
        self._audit_event(
            task_id="self-management-pause",
            user_request=f"Pause autonomous recovery: {reason or 'operator request'}",
            route="policy",
            actions=["pause"],
            resources={"paused": True, "reason": reason},
        )
        return {"paused": True, "reason": reason, "policy": self.recovery.policy()}

    def resume(self) -> dict[str, Any]:
        self.recovery.configure(paused=False)
        self._save_policy()
        self._audit_event(
            task_id="self-management-resume",
            user_request="Resume autonomous recovery",
            route="policy",
            actions=["resume"],
            resources={"paused": False},
        )
        return {"paused": False, "policy": self.recovery.policy()}

    def configure(
        self,
        *,
        mode: str | None = None,
        paused: bool | None = None,
        max_actions_per_window: int | None = None,
    ) -> dict[str, Any]:
        try:
            parsed = AutonomyMode(mode) if mode is not None else None
        except ValueError as exc:
            self._audit_event(
                task_id="self-management-policy",
                user_request=f"Change self-management policy: {mode!r}",
                route="policy",
                actions=["configure-policy"],
                outcome="failed",
                failures=[str(exc)],
            )
            raise
        policy = self.recovery.configure(
            mode=parsed,
            paused=paused,
            max_actions_per_window=max_actions_per_window,
        )
        self._save_policy()
        self._audit_event(
            task_id="self-management-policy",
            user_request="Change self-management autonomy policy",
            route="policy",
            actions=["configure-policy"],
            resources={
                "mode": policy.get("mode"),
                "paused": policy.get("paused"),
                "max_actions_per_window": policy.get("max_actions_per_window"),
            },
        )
        return policy

    # -- maintenance ---------------------------------------------------------
    def maintenance_preview(self) -> dict[str, Any]:
        return self.maintenance.preview().to_dict()

    async def maintenance_run(
        self, *, approved: bool = False, approved_by: str = ""
    ) -> dict[str, Any]:
        if self.recovery.paused:
            reason = "Autonomous maintenance is paused; resume it before running maintenance."
            self._audit_event(
                task_id="self-management-maintenance",
                user_request="Run self-management maintenance pass",
                route="maintenance",
                actions=["maintenance-run"],
                outcome="denied",
                failures=[reason],
            )
            raise RecoveryRefused(reason)
        try:
            report = self.maintenance.run(approved=approved, approved_by=approved_by)
        except PermissionError as exc:
            self._audit_event(
                task_id="self-management-maintenance",
                user_request="Run self-management maintenance pass",
                route="maintenance",
                actions=["maintenance-run"],
                outcome="denied",
                failures=[str(exc)],
            )
            raise
        self._audit_maintenance(report)
        return report.to_dict()

    # -- configuration -------------------------------------------------------
    def configuration_report(self) -> dict[str, Any]:
        return self.configuration.report().to_dict()

    def snapshot_configuration(self, *, note: str = "") -> dict[str, Any]:
        version = self.configuration.snapshot(note=note or "manual snapshot from self-management")
        self._audit_event(
            task_id=f"config-snapshot-{version.version}",
            user_request="Snapshot the current runtime configuration",
            route="configuration",
            actions=["snapshot-configuration"],
            resources={
                "version": version.version,
                "known_good": version.known_good,
                "checksum": version.checksum,
            },
        )
        return {"version": version.to_dict(), "report": self.configuration.report().to_dict()}

    async def restore_configuration(
        self,
        version: int,
        *,
        approved: bool = False,
        approved_by: str = "",
    ) -> dict[str, Any]:
        if not approved:
            self._audit_configuration_denied(
                version, "Restoring configuration requires explicit operator approval."
            )
            raise RecoveryRefused(
                "Restoring configuration requires explicit operator approval."
            )
        if not self.recovery.mode.executes:
            reason = (
                f"Autonomy mode {self.recovery.mode.value} cannot restore configuration; "
                "use controlled or automatic mode."
            )
            self._audit_configuration_denied(version, reason)
            raise RecoveryRefused(reason)
        if self.recovery.paused:
            reason = "Autonomous recovery is paused; resume it before restoring configuration."
            self._audit_configuration_denied(version, reason)
            raise RecoveryRefused(reason)
        if self.recovery.permissions is None:
            reason = "The permission layer is not wired, so configuration cannot be restored."
            self._audit_configuration_denied(version, reason)
            raise RecoveryRefused(reason)
        decision = self.recovery.permissions.check(
            "self_management.restore-known-good-config",
            action="restore_known_good_config",
            approved=True,
        )
        if not decision.allow:
            self._audit_configuration_denied(version, decision.reason)
            raise RecoveryRefused(decision.reason)
        try:
            target = self.configuration.restore_target(version)
        except (KeyError, ValueError) as exc:
            self._audit_event(
                task_id=f"config-restore-{version}",
                user_request=f"Restore known-good configuration version {version}",
                route="configuration",
                actions=["restore-known-good-config"],
                outcome="failed",
                failures=[str(exc)],
            )
            raise
        if self._apply_configuration is None:
            self._audit_configuration_denied(
                version, "No configuration application seam is wired."
            )
            raise RecoveryRefused("No configuration application seam is wired.")
        try:
            applied = self._apply_configuration(target.settings)
            if isawaitable(applied):
                await applied
        except Exception as exc:
            self._audit_event(
                task_id=f"config-restore-{version}",
                user_request=f"Restore known-good configuration version {version}",
                route="configuration",
                actions=["restore-known-good-config"],
                outcome="failed",
                failures=[f"{type(exc).__name__}: {exc}"],
                permission=decision.to_dict(),
            )
            raise
        try:
            recorded = self.configuration.record_restore(
                target, restored_by=approved_by or "operator"
            )
        except Exception as exc:
            self._audit_event(
                task_id=f"config-restore-{version}",
                user_request=f"Restore known-good configuration version {version}",
                route="configuration",
                actions=["restore-known-good-config"],
                outcome="failed",
                failures=[
                    "configuration applied but history could not be recorded: "
                    f"{type(exc).__name__}: {exc}"
                ],
                permission=decision.to_dict(),
            )
            raise
        self._audit_event(
            task_id=f"config-restore-{version}",
            user_request=f"Restore known-good configuration version {version}",
            route="configuration",
            actions=["restore-known-good-config"],
            permission=decision.to_dict(),
            resources={
                "restored_from": target.version,
                "recorded_version": recorded.version,
            },
        )
        return {
            "ok": True,
            "restored_from": target.to_dict(),
            "recorded_version": recorded.to_dict(),
            "configuration": self.configuration.report().to_dict(),
            "health": (await self.health.check_component("Configuration")).to_dict()
            if hasattr(self.health, "check_component")
            else None,
        }

    # -- reporting -----------------------------------------------------------
    def pending_approvals(self) -> tuple[dict[str, Any], ...]:
        pending: list[dict[str, Any]] = []
        for incident in self.incidents.list(open_only=True, limit=50):
            # An escalated incident needs investigation before another
            # recommendation; recovery.recommend intentionally refuses it.
            if incident.state is IncidentState.ESCALATED:
                continue
            for action in self.recovery.recommend(incident):
                if action.get("approval_required") and action.get("available"):
                    pending.append(
                        {
                            "incident_id": incident.id,
                            "component": incident.component,
                            "action": action,
                            "state": incident.state.value,
                        }
                    )
        return tuple(pending)

    def status(self) -> dict[str, Any]:
        snapshot = self.health.last
        open_incidents = self.incidents.list(open_only=True, limit=20)
        active_incidents = tuple(
            row
            for row in self.incidents.list(limit=50)
            if row.state is not IncidentState.RESOLVED
        )[:20]
        return {
            "enabled": True,
            "health": snapshot.to_dict() if snapshot else None,
            "health_checks": self.checks,
            "last_check_at": self.last_check_at.isoformat() if self.last_check_at else "",
            "diagnoses": [row.to_dict() for row in self.last_diagnoses],
            "incidents": self.incidents.report(),
            "open_incidents": [row.to_dict() for row in open_incidents],
            "active_incidents": [row.to_dict() for row in active_incidents],
            "recurrence": list(self.incidents.recurrence_groups()),
            "recovery": self.recovery.policy(),
            "pending_approvals": list(self.pending_approvals()),
            "maintenance": self.maintenance.status(),
            "configuration": self.configuration.report().to_dict(),
            "last_cycle": dict(self.last_cycle),
            "model_integration": {
                "diagnosis_mode": "deterministic",
                "external_provider_required": False,
                "privacy": "health and diagnostic evidence stay local unless a separate caller explicitly exports them",
            },
            "learning": {
                "wired": self._learning is not None,
                "recorded": len(self._learning_reports),
                "recent": list(self._learning_reports[-5:]),
                "promotion": "advisory only; a verified recovery never becomes an automatic action by itself",
            },
            "boundaries": {
                "source_code_modification": "unsupported; never applied by self-management",
                "dependency_installation": "unsupported",
                "deployment": "unsupported",
                "destructive_maintenance": "operator approval required",
                "automatic_recovery": "only registered low-risk reversible actions",
            },
        }

    # -- audit ---------------------------------------------------------------
    def _audit_event(
        self,
        *,
        task_id: str,
        user_request: str,
        route: str,
        actions: Sequence[str],
        outcome: str = "completed",
        failures: Sequence[str] = (),
        permission: Mapping[str, Any] | None = None,
        resources: Mapping[str, Any] | None = None,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit(
                task_id=task_id,
                user_request=user_request,
                source="self_management",
                route=route,
                actions=list(actions),
                recovery=[],
                permission_decisions=[dict(permission)] if permission else [],
                outcome=outcome,
                failures=list(failures),
                resources=dict(resources or {}),
            )
        except Exception:
            return

    def _audit_configuration_denied(self, version: int, reason: str) -> None:
        self._audit_event(
            task_id=f"config-restore-{version}",
            user_request=f"Restore known-good configuration version {version}",
            route="configuration",
            actions=["restore-known-good-config"],
            outcome="denied",
            failures=[reason],
        )

    def _audit_maintenance(self, report: MaintenanceReport) -> None:
        if self._audit is None:
            return
        try:
            self._audit(
                task_id=f"maintenance-{report.started_at.isoformat()}",
                user_request="Self-management maintenance pass",
                source="self_management",
                route="maintenance",
                actions=[row.action for row in report.items if row.status == "done"],
                recovery=[],
                outcome="completed" if not report.errors else "failed",
                failures=list(report.errors),
                resources={
                    "removed_bytes": report.removed_bytes,
                    "changed_files": report.changed_files,
                },
            )
        except Exception:
            return


__all__ = ["SelfManagementService"]
