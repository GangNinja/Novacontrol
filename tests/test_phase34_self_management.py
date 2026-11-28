"""Phase 34 — trustworthy self-management.

These tests exercise the real Phase 14 diagnostic roster, the shared permission
manager, the audit writer and the durable state store.  Fakes are limited to the
component probes and recovery handlers a test explicitly supplies; no test
fabricates a health reading from process liveness or claims a recovery succeeded
without a verifier.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from novacontrol.core.diagnostics import HealthState
from novacontrol.core.security import RiskLevel
from novacontrol.diagnostics.manager import DiagnosticManager, DiagnosticResult
from novacontrol.persistence import JsonStateStore
from novacontrol.reliability.permissions import PermissionManager
from novacontrol.self_management import (
    AutonomyMode,
    CauseConfidence,
    ConfigurationManager,
    Diagnosis,
    HealthCollector,
    IncidentManager,
    IncidentSeverity,
    MaintenanceService,
    RecoveryAction,
    RecoveryManager,
    RecoveryRefused,
    RecoveryRegistry,
    SelfManagementService,
)
from novacontrol.self_management.models import IncidentState, MaintenanceKind

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - httpx is an optional dev dependency
    TestClient = None  # type: ignore[assignment]


def diagnosis(
    component: str = "Scheduler",
    *,
    confidence: CauseConfidence = CauseConfidence.CONFIRMED,
    severity: IncidentSeverity = IncidentSeverity.ERROR,
    fingerprint: str = "fp-scheduler",
    evidence: tuple[str, ...] = ("Scheduler: the background loop is not running",),
) -> Diagnosis:
    return Diagnosis(
        rule_id="scheduler_stalled",
        component=component,
        title="The scheduled-work loop is not running.",
        cause="the background loop is not running",
        confidence=confidence,
        severity=severity,
        evidence=evidence,
        impact="Due automations will not start on time.",
        recommendations=("restart-scheduler",),
        rationale="the component's own live probe reported the loop stopped",
        fingerprint=fingerprint,
    )


def incident(manager: IncidentManager, **kwargs: Any) -> Any:
    created = manager.observe((diagnosis(**kwargs),))
    assert created
    return created[0]


def registry_with(
    component: str = "Scheduler",
    *,
    handler: Any = None,
    verifier: Any = None,
    **overrides: Any,
) -> tuple[RecoveryRegistry, RecoveryAction]:
    async def default_handler(row: Any) -> dict[str, Any]:
        return {"component": row.component}

    def default_verifier(row: Any) -> dict[str, Any]:
        return {"healthy": True, "detail": "component probe passed", "component": component}

    action = RecoveryAction(
        id="restart-scheduler",
        component=component,
        title="Restart the scheduled-work loop",
        description="Start the existing loop.",
        risk=RiskLevel.LOW,
        automatic=False,
        requires_approval=False,
        timeout_seconds=1.0,
        max_attempts=2,
        cooldown_seconds=0.0,
        impact="Stored tasks are preserved.",
    )
    action = replace(action, **overrides)
    registry = RecoveryRegistry()
    registry.register(action, handler or default_handler, verifier or default_verifier)
    return registry, action


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_component_observations_are_live_and_optional_readings_are_honest(self) -> None:
        manager = DiagnosticManager(timeout_seconds=1.0)

        async def ok() -> DiagnosticResult:
            return DiagnosticResult("Core", HealthState.OK, "round trip passed")

        async def failing() -> DiagnosticResult:
            return DiagnosticResult("Database", HealthState.FAILING, "read failed")

        async def gpu() -> DiagnosticResult:
            return DiagnosticResult("GPU", HealthState.UNKNOWN, "no adapter visible")

        manager.register("Core", ok, category="core")
        manager.register("Database", failing, category="data")
        manager.register("GPU", gpu, category="hardware")
        metrics = {
            "free_ram_bytes": 128 * 1024**2,
            "critical_ram_bytes": 256 * 1024**2,
            "cpu_percent": 91.0,
            "max_cpu_percent": 80.0,
        }
        collector = HealthCollector(manager, metrics=lambda: metrics)
        snapshot = await collector.collect()
        self.assertEqual(snapshot.overall, HealthState.FAILING)
        self.assertEqual(len(snapshot.components), 3)
        self.assertEqual(snapshot.metrics["cpu_percent"], 91.0)
        self.assertEqual(collector.component("Database").status, HealthState.FAILING)
        self.assertEqual(collector.component("GPU").status, HealthState.UNKNOWN)

    async def test_optional_unknown_does_not_make_the_whole_system_unknown(self) -> None:
        manager = DiagnosticManager(timeout_seconds=1.0)
        manager.register(
            "Core",
            lambda: DiagnosticResult("Core", HealthState.OK, "ready"),
        )
        manager.register(
            "NPU",
            lambda: DiagnosticResult("NPU", HealthState.UNKNOWN, "not present"),
        )
        snapshot = await HealthCollector(manager).collect()
        self.assertEqual(snapshot.overall, HealthState.OK)

    async def test_component_timeout_is_a_failure_not_an_absence(self) -> None:
        manager = DiagnosticManager(timeout_seconds=0.02)

        async def slow() -> DiagnosticResult:
            await asyncio.sleep(0.2)
            return DiagnosticResult("Storage", HealthState.OK, "late")

        manager.register("Storage", slow, category="data")
        snapshot = await HealthCollector(manager).collect()
        self.assertEqual(snapshot.components[0].status, HealthState.FAILING)
        self.assertIn("did not answer", snapshot.components[0].message)

    async def test_unknown_component_selection_is_rejected(self) -> None:
        manager = DiagnosticManager(timeout_seconds=1.0)
        manager.register(
            "Core", lambda: DiagnosticResult("Core", HealthState.OK, "ready")
        )
        with self.assertRaises(KeyError):
            await HealthCollector(manager).collect(only=("Missing",))


class DiagnosisTests(unittest.IsolatedAsyncioTestCase):
    async def test_confidence_labels_follow_the_evidence(self) -> None:
        manager = DiagnosticManager(timeout_seconds=1.0)

        async def failing() -> DiagnosticResult:
            return DiagnosticResult("Core", HealthState.FAILING, "runtime stopped")

        async def degraded() -> DiagnosticResult:
            return DiagnosticResult("Memory services", HealthState.DEGRADED, "load error")

        async def unknown() -> DiagnosticResult:
            return DiagnosticResult("Event bus", HealthState.UNKNOWN, "no reading")

        manager.register("Core", failing)
        manager.register("Memory services", degraded)
        manager.register("Event bus", unknown)
        from novacontrol.self_management.diagnosis import diagnose

        findings = {row.component: row for row in diagnose(await HealthCollector(manager).collect())}
        self.assertEqual(findings["Core"].confidence, CauseConfidence.CONFIRMED)
        self.assertEqual(findings["Memory services"].confidence, CauseConfidence.PROBABLE)
        self.assertEqual(findings["Event bus"].confidence, CauseConfidence.POSSIBLE)
        self.assertTrue(all(row.evidence for row in findings.values()))

    async def test_unknown_hardware_is_reported_but_not_an_incident(self) -> None:
        manager = DiagnosticManager(timeout_seconds=1.0)
        manager.register("GPU", lambda: DiagnosticResult("GPU", HealthState.UNKNOWN, "not present"))
        manager.register("NPU", lambda: DiagnosticResult("NPU", HealthState.UNKNOWN, "not present"))
        from novacontrol.self_management.diagnosis import diagnose

        findings = diagnose(await HealthCollector(manager).collect())
        self.assertEqual(findings, ())

    async def test_a_tight_hardware_reading_is_not_worded_as_unsafe(self) -> None:
        manager = DiagnosticManager(timeout_seconds=1.0)
        manager.register(
            "Hardware manager",
            lambda: DiagnosticResult(
                "Hardware manager",
                HealthState.DEGRADED,
                "the resource governor reports a tight machine reading",
            ),
        )
        from novacontrol.self_management.diagnosis import diagnose

        findings = diagnose(await HealthCollector(manager).collect())
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].confidence, CauseConfidence.PROBABLE)
        self.assertIn("tight machine reading", findings[0].title)
        self.assertNotIn("unsafe", findings[0].title)
        self.assertIn("tight machine reading", findings[0].cause)

    async def test_configuration_issue_is_a_confirmed_finding(self) -> None:
        manager = DiagnosticManager(timeout_seconds=1.0)
        manager.register("Core", lambda: DiagnosticResult("Core", HealthState.OK, "ready"))
        from novacontrol.self_management.diagnosis import diagnose

        findings = diagnose(
            await HealthCollector(manager).collect(),
            configuration_issue="brain_mode is not a supported mode",
        )
        self.assertEqual(findings[0].component, "Configuration")
        self.assertEqual(findings[0].confidence, CauseConfidence.CONFIRMED)


class IncidentTests(unittest.IsolatedAsyncioTestCase):
    def test_dedupe_updates_evidence_and_persists(self) -> None:
        with TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory))
            manager = IncidentManager(store=store)
            first = incident(manager)
            again = manager.observe(
                (diagnosis(evidence=("Scheduler: second probe failed",)),)
            )[0]
            self.assertEqual(first.id, again.id)
            self.assertEqual(len(manager.all), 1)
            self.assertIn("Scheduler: second probe failed", again.evidence)
            retitled = manager.observe(
                (replace(diagnosis(), title="The loop is stopped now."),)
            )[0]
            self.assertEqual(retitled.title, "The loop is stopped now.")
            restored = IncidentManager(store=store)
            self.assertEqual(restored.get(first.id).component, "Scheduler")
            self.assertEqual(restored.report()["total"], 1)

    def test_resolved_incident_recurs_as_a_new_related_record(self) -> None:
        manager = IncidentManager()
        first = incident(manager)
        manager.resolve(first.id, "operator verified recovery")
        second = incident(manager)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(second.recurrence, 1)
        self.assertIn(first.id, second.related)
        self.assertEqual(manager.report()["recurring"], [second.id])

    def test_lifecycle_rejects_an_illegal_transition(self) -> None:
        manager = IncidentManager()
        first = incident(manager)
        manager.resolve(first.id, "done")
        with self.assertRaises(ValueError):
            manager.transition(first.id, IncidentState.RECOVERING)

    def test_evidence_is_redacted_before_storage(self) -> None:
        calls: list[Any] = []

        def redact(value: Any) -> Any:
            calls.append(value)
            return "«redacted»" if isinstance(value, str) and "sk-" in value else value

        manager = IncidentManager(redactor=redact)
        created = manager.observe(
            (diagnosis(evidence=("provider key sk-secret",)),)
        )[0]
        self.assertIn("«redacted»", created.evidence[0])
        self.assertTrue(calls)

    def test_a_self_healed_incident_resolves_without_a_recovery_action(self) -> None:
        manager = IncidentManager()
        created = incident(manager)
        snapshot = SimpleNamespace(
            components=(
                SimpleNamespace(component="Scheduler", status=HealthState.OK),
            )
        )
        resolved = manager.dismiss_healthy(snapshot)
        self.assertEqual([row.id for row in resolved], [created.id])
        self.assertEqual(manager.get(created.id).state, IncidentState.RESOLVED)
        self.assertIn("no recovery action was needed", manager.get(created.id).resolution)

    def test_a_current_diagnosis_prevents_evidence_based_closure(self) -> None:
        manager = IncidentManager()
        created = incident(manager)
        snapshot = SimpleNamespace(
            components=(
                SimpleNamespace(component="Scheduler", status=HealthState.OK),
            )
        )
        self.assertEqual(
            manager.dismiss_healthy(
                snapshot, active_components={"Scheduler"}
            ),
            (),
        )
        self.assertEqual(manager.get(created.id).state, IncidentState.DIAGNOSED)

    def test_an_escalated_incident_is_updated_not_reopened_or_auto_resolved(self) -> None:
        manager = IncidentManager()
        first = incident(manager)
        manager.escalate(first.id, "recovery exhausted")
        again = manager.observe(
            (diagnosis(evidence=("Scheduler: still stopped",)),)
        )[0]
        self.assertEqual(again.id, first.id)
        self.assertEqual(again.state, IncidentState.ESCALATED)
        self.assertEqual(len(manager.all), 1)
        manager.dismiss_healthy(
            SimpleNamespace(
                components=(
                    SimpleNamespace(component="Scheduler", status=HealthState.OK),
                )
            )
        )
        self.assertEqual(manager.get(first.id).state, IncidentState.ESCALATED)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.incidents = IncidentManager()
        self.row = incident(self.incidents)

    async def test_success_resolves_after_live_verification_and_audits(self) -> None:
        audits: list[dict[str, Any]] = []
        registry, _ = registry_with()
        manager = RecoveryManager(
            registry,
            self.incidents,
            permissions=PermissionManager(),
            audit=lambda **fields: audits.append(fields),
            mode=AutonomyMode.CONTROLLED,
        )
        result = await manager.recover(
            self.row.id, "restart-scheduler", approved=True, approved_by="test"
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["incident"]["state"], "resolved")
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["route"], "recovery")

    async def test_resolved_and_escalated_incidents_cannot_be_recovered_again(self) -> None:
        registry, _ = registry_with()
        manager = RecoveryManager(
            registry,
            self.incidents,
            permissions=PermissionManager(),
            mode=AutonomyMode.CONTROLLED,
        )
        self.incidents.resolve(self.row.id, "manual operator resolution")
        with self.assertRaises(RecoveryRefused):
            await manager.recover(self.row.id, "restart-scheduler", approved=True)
        escalated = incident(
            self.incidents, fingerprint="fp-escalated", component="Scheduler"
        )
        self.incidents.escalate(escalated.id, "previous recovery failed")
        with self.assertRaises(RecoveryRefused):
            await manager.recover(escalated.id, "restart-scheduler", approved=True)

    async def test_failed_verification_does_not_claim_success(self) -> None:
        registry, _ = registry_with(verifier=lambda row: {"healthy": False, "detail": "still stopped"})
        manager = RecoveryManager(
            registry,
            self.incidents,
            permissions=PermissionManager(),
            mode=AutonomyMode.CONTROLLED,
        )
        result = await manager.recover(
            self.row.id, "restart-scheduler", approved=True
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("still stopped", result["detail"])

    async def test_timeout_is_bounded_and_recorded(self) -> None:
        async def slow(row: Any) -> dict[str, Any]:
            await asyncio.sleep(0.2)
            return {"ok": True}

        registry, _ = registry_with(handler=slow, timeout_seconds=0.02)
        manager = RecoveryManager(
            registry,
            self.incidents,
            permissions=PermissionManager(),
            mode=AutonomyMode.CONTROLLED,
        )
        with self.assertRaises(RecoveryRefused):
            await manager.recover(self.row.id, "restart-scheduler", approved=True)
        updated = self.incidents.get(self.row.id)
        self.assertEqual(updated.recovery_attempts[-1].status.value, "timed_out")

    async def test_retry_ceiling_escalates_instead_of_looping(self) -> None:
        calls: list[int] = []

        async def fail(row: Any) -> dict[str, Any]:
            calls.append(1)
            raise RuntimeError("restart refused by the OS")

        registry, _ = registry_with(
            handler=fail, max_attempts=2, cooldown_seconds=0.0
        )
        manager = RecoveryManager(
            registry,
            self.incidents,
            permissions=PermissionManager(),
            mode=AutonomyMode.CONTROLLED,
        )
        for _ in range(2):
            result = await manager.recover(
                self.row.id, "restart-scheduler", approved=True
            )
            self.assertFalse(result["ok"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.incidents.get(self.row.id).state, IncidentState.ESCALATED)
        with self.assertRaises(RecoveryRefused):
            await manager.recover(self.row.id, "restart-scheduler", approved=True)
        self.assertEqual(len(calls), 2)

    async def test_cooldown_refuses_an_immediate_retry(self) -> None:
        registry, _ = registry_with(verifier=lambda row: False, cooldown_seconds=60.0)
        manager = RecoveryManager(
            registry,
            self.incidents,
            permissions=PermissionManager(),
            mode=AutonomyMode.CONTROLLED,
        )
        await manager.recover(self.row.id, "restart-scheduler", approved=True)
        with self.assertRaises(RecoveryRefused):
            await manager.recover(self.row.id, "restart-scheduler", approved=True)

    async def test_approval_is_required_even_for_a_safe_action_in_controlled_mode(self) -> None:
        registry, _ = registry_with(requires_approval=True)
        manager = RecoveryManager(
            registry,
            self.incidents,
            permissions=PermissionManager(),
            mode=AutonomyMode.CONTROLLED,
        )
        with self.assertRaises(RecoveryRefused):
            await manager.recover(self.row.id, "restart-scheduler")

    async def test_missing_permission_layer_and_pause_fail_closed(self) -> None:
        registry, _ = registry_with()
        no_permissions = RecoveryManager(
            registry,
            self.incidents,
            permissions=None,
            mode=AutonomyMode.CONTROLLED,
        )
        with self.assertRaises(RecoveryRefused):
            await no_permissions.recover(self.row.id, "restart-scheduler", approved=True)
        paused = RecoveryManager(
            registry,
            self.incidents,
            permissions=PermissionManager(),
            mode=AutonomyMode.CONTROLLED,
            paused=True,
        )
        with self.assertRaises(RecoveryRefused):
            await paused.recover(self.row.id, "restart-scheduler", approved=True)

    async def test_window_budget_blocks_the_next_action(self) -> None:
        second = incident(self.incidents, fingerprint="fp-second", component="Scheduler")
        registry, _ = registry_with()
        manager = RecoveryManager(
            registry,
            self.incidents,
            permissions=PermissionManager(),
            mode=AutonomyMode.CONTROLLED,
            max_actions_per_window=1,
        )
        await manager.recover(self.row.id, "restart-scheduler", approved=True)
        with self.assertRaises(RecoveryRefused):
            await manager.recover(second.id, "restart-scheduler", approved=True)

    async def test_refusal_is_audited(self) -> None:
        audits: list[dict[str, Any]] = []
        registry, _ = registry_with(requires_approval=True)
        manager = RecoveryManager(
            registry,
            self.incidents,
            permissions=PermissionManager(),
            audit=lambda **fields: audits.append(fields),
            mode=AutonomyMode.CONTROLLED,
        )
        with self.assertRaises(RecoveryRefused):
            await manager.recover(self.row.id, "restart-scheduler")
        self.assertTrue(audits)
        self.assertEqual(audits[0]["outcome"], "denied")

    async def test_automatic_mode_only_runs_actions_declared_safe(self) -> None:
        calls: list[int] = []

        async def handler(row: Any) -> dict[str, Any]:
            calls.append(1)
            return {"ok": True}

        registry, _ = registry_with(
            handler=handler, automatic=True, requires_approval=False
        )
        manager = RecoveryManager(
            registry,
            self.incidents,
            permissions=PermissionManager(),
            mode=AutonomyMode.AUTOMATIC,
        )
        outcome = await manager.auto_recover(self.row)
        self.assertIsNotNone(outcome)
        self.assertTrue(outcome["ok"])
        self.assertEqual(calls, [1])

    async def test_automatic_mode_refuses_an_approval_required_action(self) -> None:
        registry, _ = registry_with(automatic=False, requires_approval=True)
        manager = RecoveryManager(
            registry,
            self.incidents,
            permissions=PermissionManager(),
            mode=AutonomyMode.AUTOMATIC,
        )
        self.assertIsNone(await manager.auto_recover(self.row))


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def test_preview_protects_files_and_run_is_approval_gated(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            temp = root / "tmp"
            cache = root / "cache"
            logs = root / "logs"
            for path in (temp, cache, logs):
                path.mkdir()
            old = time.time() - 10 * 24 * 60 * 60
            stale = temp / "old.tmp"
            stale.write_text("temporary", encoding="utf-8")
            os.utime(stale, (old, old))
            protected = temp / "keep.py"
            protected.write_text("source", encoding="utf-8")
            os.utime(protected, (old, old))
            cached = cache / "stale.bin"
            cached.write_bytes(b"cache")
            os.utime(cached, (old, old))
            log = logs / "run.log"
            log.write_text("x" * 5000, encoding="utf-8")
            service = MaintenanceService(
                root,
                stale_cache_seconds=0,
                temp_max_age_seconds=0,
                log_rotate_bytes=1024,
            )
            preview = service.preview()
            self.assertTrue(any(item.protected for item in preview.items))
            self.assertTrue(any(item.requires_approval for item in preview.items))
            with self.assertRaises(PermissionError):
                service.run(approved=False)
            report = service.run(approved=True, approved_by="test")
            self.assertFalse(stale.exists())
            self.assertFalse(cached.exists())
            self.assertTrue(protected.exists())
            self.assertTrue(any(item.status == "skipped_protected" for item in report.items))
            self.assertGreaterEqual(report.changed_files, 3)
            self.assertTrue(list(logs.glob("run.log.*.bak")))

    def test_stale_process_detection_ignores_project_paths_and_parent_shells(self) -> None:
        with TemporaryDirectory() as directory:
            service = MaintenanceService(directory)
            rows = [
                SimpleNamespace(
                    info={
                        "pid": 101,
                        "name": "bash.exe",
                        "cmdline": ["bash", "-c", "python -m novacontrol"],
                    }
                ),
                SimpleNamespace(
                    info={
                        "pid": 102,
                        "name": "python.exe",
                        "cmdline": ["python", "C:\\NovaControl\\novamarket\\vite.js"],
                    }
                ),
                SimpleNamespace(
                    info={
                        "pid": 103,
                        "name": "python.exe",
                        "cmdline": ["python", "-m", "novacontrol"],
                    }
                ),
            ]
            with patch("psutil.process_iter", return_value=rows):
                found = service._stale_processes()
            self.assertEqual([row.metadata["pid"] for row in found], [103])

    def test_configuration_items_are_routed_to_the_real_restore_control(self) -> None:
        with TemporaryDirectory() as directory:
            service = MaintenanceService(
                directory,
                config_report=lambda: {"valid": False, "issues": ["bad value"]},
            )
            preview = service.preview()
            config_items = [
                item for item in preview.items if item.kind is MaintenanceKind.CONFIGURATION
            ]
            self.assertEqual(len(config_items), 1)
            self.assertFalse(config_items[0].requires_approval)
            # A configuration report is not destructive maintenance and must not
            # force an approval prompt for work maintenance cannot perform.
            report = service.run()
            result = [
                item for item in report.items if item.kind is MaintenanceKind.CONFIGURATION
            ][0]
            self.assertEqual(result.status, "manual_action_required")
            self.assertIn("Restore Latest Known-Good", result.detail)
            self.assertEqual(report.changed_files, 0)

    def test_database_maintenance_is_bounded_and_checks_integrity(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "novacontrol.sqlite3"
            connection = sqlite3.connect(str(database))
            try:
                connection.execute("CREATE TABLE sample (id INTEGER)")
                connection.commit()
            finally:
                connection.close()
            service = MaintenanceService(root)
            preview = service.preview()
            item = next(item for item in preview.items if item.kind.value == "database")
            self.assertEqual(item.action, "optimize")
            self.assertFalse(item.destructive)
            report = service.run(approved=False)
            self.assertTrue(any(item.status == "done" for item in report.items))


class ConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def test_invalid_config_is_detected_and_known_good_rollback_is_preserved(self) -> None:
        with TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory))
            valid = {
                "brain_mode": "scratch",
                "execution_mode": "balanced",
                "approval_mode": "ask",
            }
            store.write("settings", valid)
            manager = ConfigurationManager(store, lambda: valid)
            first = manager.snapshot(note="known good")
            self.assertTrue(first.known_good)
            store.write("settings", {**valid, "brain_mode": "not-a-mode"})
            report = manager.report()
            self.assertFalse(report.valid)
            self.assertIn("brain_mode", "; ".join(report.issues))
            target, issues = manager.recover_from_known_good()
            self.assertIsNotNone(target)
            self.assertEqual(target.version, first.version)
            self.assertTrue(issues)
            restored = manager.record_restore(target, restored_by="operator")
            self.assertGreater(restored.version, first.version)
            store.write("settings", dict(target.settings))
            self.assertTrue(manager.report().valid)

    def test_a_tampered_snapshot_is_not_known_good(self) -> None:
        with TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory))
            valid = {"brain_mode": "scratch", "approval_mode": "ask"}
            store.write("settings", valid)
            manager = ConfigurationManager(store, lambda: valid)
            version = manager.snapshot()
            payload = store.read("self_management_config")
            payload["versions"][0]["checksum"] = "0" * 64
            store.write("self_management_config", payload)
            reloaded = ConfigurationManager(store, lambda: valid)
            self.assertFalse(reloaded.versions())
            self.assertIsNotNone(version.checksum)

    def test_invalid_snapshot_is_not_used_as_a_restore_target(self) -> None:
        with TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory))
            manager = ConfigurationManager(store, lambda: {})
            invalid = {"brain_mode": "not-a-mode"}
            row = manager.snapshot(invalid)
            self.assertFalse(row.known_good)
            with self.assertRaises(ValueError):
                manager.restore_target(row.version)


class InterruptedWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoint_survives_restart_and_resume_is_reassessed(self) -> None:
        """Scenario E, using Phase 30's verified checkpoint rather than a copy."""
        from novacontrol.application import NovaControlApplication

        with TemporaryDirectory() as directory:
            app = NovaControlApplication(data_dir=directory)
            await app.start()
            objective = app.operations.create_objective(
                "Interrupted workflow",
                "The checkpoint is preserved and the resume is reviewed.",
            )
            objective, task = app.operations.add_task(
                objective.id,
                "Two-step task",
                "Do the first step, then stop.",
                steps=("first", "second"),
            )
            app.operations.manager.refresh(objective.id)
            app.operations.manager.begin_task(objective.id, task.id)
            app.operations.manager.record_step(
                objective.id,
                task.id,
                "first",
                verified=True,
                evidence="first step verified by its own check",
            )
            app.operations._save()
            await app.stop()

            restarted = NovaControlApplication(data_dir=directory)
            await restarted.start()
            try:
                restored = restarted.operations.manager.get(objective.id)
                restored_task = restored.task(task.id)
                self.assertEqual(restored_task.state.value, "ready")
                self.assertIn("first", restored_task.completed_steps)
                self.assertTrue(restored_task.metadata.get("resume_required"))
                resumed = restarted.operations.resume_objective(objective.id)
                self.assertIn(task.id, resumed["needs_reassessment"])
                after = restarted.operations.manager.get(objective.id).task(task.id)
                self.assertEqual(after.completed_steps, ("first",))
                self.assertEqual(after.checkpoint["completed_steps"], ["first"])
                restarted.operations.manager.begin_task(
                    objective.id, task.id, reassessed=True
                )
                running = restarted.operations.manager.get(objective.id).task(task.id)
                self.assertEqual(running.state.value, "running")
                self.assertEqual(running.completed_steps, ("first",))
                self.assertNotIn("resume_required", running.metadata)
            finally:
                await restarted.stop()


class ApplicationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_scheduler_recovery_and_phase27_learning(self) -> None:
        from novacontrol.application import NovaControlApplication

        with TemporaryDirectory() as directory:
            app = NovaControlApplication(data_dir=directory)
            await app.start()
            try:
                app._stop_automation_ticker()
                result = await app.self_management_health()
                self.assertIn(
                    "Scheduler",
                    {row["component"] for row in result["health"]["components"]},
                )
                rows = [
                    row
                    for row in app.self_management.incidents.list(open_only=True)
                    if row.component == "Scheduler"
                ]
                self.assertTrue(rows)
                recommendation = app.self_management.recommend(rows[0].id)
                self.assertTrue(recommendation["actions"])
                recovered = await app.self_management.recover(
                    rows[0].id,
                    "restart-scheduler",
                    approved=True,
                    approved_by="test",
                )
                self.assertTrue(recovered["ok"])
                self.assertEqual(recovered["incident"]["state"], "resolved")
                self.assertIsNotNone(app._automation_ticker)
                self.assertFalse(app._automation_ticker.done())
                self.assertGreaterEqual(
                    app.continual.status()["by_task_kind"].get(
                        "self_management_recovery", 0
                    ),
                    1,
                )
            finally:
                await app.stop()

    async def test_authorized_automatic_mode_recovers_a_safe_action_for_real(self) -> None:
        from novacontrol.application import NovaControlApplication

        with TemporaryDirectory() as directory:
            app = NovaControlApplication(data_dir=directory)
            await app.start()
            try:
                app._stop_automation_ticker()
                app.self_management.configure(mode="automatic")
                result = await app.self_management.monitor()
                self.assertTrue(result["automatic_recovery"])
                self.assertTrue(
                    all(row.get("ok") for row in result["automatic_recovery"])
                )
                self.assertIsNotNone(app._automation_ticker)
                self.assertFalse(app._automation_ticker.done())
                scheduler = [
                    row
                    for row in app.self_management.incidents.all
                    if row.component == "Scheduler"
                ]
                self.assertTrue(scheduler)
                self.assertEqual(scheduler[-1].state, IncidentState.RESOLVED)
                self.assertTrue(
                    any(
                        entry.route == "recovery" and entry.outcome == "completed"
                        for entry in app.audit.entries(limit=50)
                    )
                )
            finally:
                await app.stop()

    async def test_provider_outage_is_diagnosed_and_reconnect_is_verified(self) -> None:
        from novacontrol.application import NovaControlApplication

        with TemporaryDirectory() as directory:
            app = NovaControlApplication(data_dir=directory)
            await app.start()
            original = app.model_manager.runtime_status
            state = {"reachable": False}

            def fake_status() -> Any:
                return SimpleNamespace(
                    provider="ollama",
                    reachable=state["reachable"],
                    loaded_models=(),
                    active_model="",
                    available_memory_bytes=1024**3,
                )

            app.model_manager.runtime_status = fake_status
            try:
                result = await app.self_management_health()
                components = {
                    row["component"]: row for row in result["health"]["components"]
                }
                self.assertEqual(components["Ollama"]["status"], "degraded")
                rows = [
                    row
                    for row in app.self_management.incidents.list(open_only=True)
                    if row.component == "Ollama"
                ]
                self.assertTrue(rows)
                state["reachable"] = True
                recovered = await app.self_management.recover(
                    rows[0].id,
                    "reconnect-provider",
                    approved=True,
                    approved_by="test",
                )
                self.assertTrue(recovered["ok"])
                self.assertEqual(recovered["verification"]["component"], "Ollama")
            finally:
                app.model_manager.runtime_status = original
                await app.stop()

    async def test_corrupt_configuration_is_detected_and_restored_through_the_app(self) -> None:
        from novacontrol.application import NovaControlApplication

        with TemporaryDirectory() as directory:
            app = NovaControlApplication(data_dir=directory)
            await app.start()
            try:
                version = app.self_management.configuration.report().active_version
                self.assertIsNotNone(version)
                corrupt = dict(app.settings.to_dict())
                corrupt["brain_mode"] = "not-a-mode"
                app.state_store.write("settings", corrupt)
                self.assertFalse(app.self_management.configuration.report().valid)
                await app.self_management_health()
                config_incidents = [
                    row
                    for row in app.self_management.incidents.list(open_only=True)
                    if row.component == "Configuration"
                ]
                self.assertTrue(config_incidents)
                self.assertNotEqual(
                    config_incidents[-1].state, IncidentState.RESOLVED
                )
                preview = app.self_management.maintenance_preview()
                self.assertIn(
                    "configuration",
                    {item["kind"] for item in preview["items"]},
                )
                restored = await app.self_management.restore_configuration(
                    int(version), approved=True, approved_by="test"
                )
                self.assertTrue(restored["ok"])
                self.assertTrue(app.self_management.configuration.report().valid)
                self.assertIn(restored["health"]["status"], {"ok", "degraded"})
            finally:
                await app.stop()

    async def test_memory_reload_and_agent_advice_are_real_and_non_authoritative(self) -> None:
        from novacontrol.application import NovaControlApplication

        with TemporaryDirectory() as directory:
            app = NovaControlApplication(data_dir=directory)
            await app.start()
            try:
                memory = app.self_management.incidents.observe(
                    (
                        diagnosis(
                            component="Memory services",
                            fingerprint="fp-memory",
                            evidence=("Memory services: controlled reload probe",),
                        ),
                    )
                )[0]
                reloaded = await app.self_management.recover(
                    memory.id, "reload-memory", approved=True, approved_by="test"
                )
                self.assertTrue(reloaded["ok"])
                self.assertEqual(reloaded["verification"]["component"], "Memory services")

                calls: list[tuple[str, tuple[str, ...]]] = []

                def advice(component: str, evidence: Any) -> dict[str, Any]:
                    calls.append((component, tuple(evidence)))
                    return {"available": True, "authority": "advisory", "advice": "inspect the event publisher"}

                app.self_management._agent_advice = advice
                agent_row = app.self_management.incidents.observe(
                    (
                        diagnosis(
                            component="Event bus",
                            fingerprint="fp-agent",
                            evidence=("Event bus: controlled publisher probe",),
                        ),
                    )
                )[0]
                without = await app.self_management.investigate(
                    agent_row.id, use_agent=False
                )
                self.assertEqual(calls, [])
                self.assertEqual(without["agent_advice"], {})
                with_agent = await app.self_management.investigate(
                    agent_row.id, use_agent=True
                )
                self.assertEqual(calls[0][0], "Event bus")
                self.assertEqual(with_agent["agent_advice"]["authority"], "advisory")
                self.assertIn("permission system", with_agent["authority"])
                self.assertEqual(with_agent["incident"]["recovery_attempts"], [])
                self.assertNotEqual(with_agent["incident"]["state"], "resolved")
            finally:
                await app.stop()

    async def test_real_advisory_agent_uses_the_public_task_interface(self) -> None:
        from novacontrol.agents import AgentResponse, AgentRole, AgentTask, AgentTaskStatus
        from novacontrol.application import NovaControlApplication

        class FakeDebugAgent:
            name = "phase34-test-debug-agent"
            role = AgentRole.DEBUG

            def __init__(self) -> None:
                self.tasks: list[AgentTask] = []

            async def handle_task(self, task: AgentTask) -> AgentResponse:
                self.tasks.append(task)
                return AgentResponse(
                    task_id=task.id,
                    agent_name=self.name,
                    role=self.role,
                    status=AgentTaskStatus.COMPLETED,
                    content="inspect the publisher; do not execute changes",
                    metadata={"advisory_only": True},
                )

        with TemporaryDirectory() as directory:
            app = NovaControlApplication(data_dir=directory)
            await app.start()
            fake = FakeDebugAgent()
            app.agent_registry.register(fake, prefer=True)
            try:
                advice = await app._self_management_agent_advice(
                    "Storage", ("Storage: controlled probe failure",)
                )
                self.assertTrue(advice["available"])
                self.assertEqual(advice["agent"], fake.name)
                self.assertEqual(len(fake.tasks), 1)
                self.assertEqual(fake.tasks[0].role, AgentRole.DEBUG)
                self.assertIn("advisory only", str(advice["authority"]).lower())
                self.assertTrue(advice["report"]["metadata"]["advisory_only"])
            finally:
                app.agent_registry.unregister(fake.name)
                await app.stop()

    async def test_pause_and_recommend_modes_guard_and_audit_policy_changes(self) -> None:
        from novacontrol.application import NovaControlApplication

        with TemporaryDirectory() as directory:
            app = NovaControlApplication(data_dir=directory)
            await app.start()
            try:
                app.self_management.pause(reason="operator test pause")
                with self.assertRaises(RecoveryRefused):
                    await app.self_management.maintenance_run(approved=True)
                app.self_management.resume()
                app.self_management.configure(mode="recommend")
                version = app.self_management.configuration.report().active_version
                self.assertIsNotNone(version)
                with self.assertRaises(RecoveryRefused):
                    await app.self_management.restore_configuration(
                        int(version), approved=True, approved_by="test"
                    )
                rows = [
                    row
                    for row in app.audit.entries(limit=50)
                    if row.route in ("policy", "maintenance", "configuration")
                ]
                self.assertTrue(rows)
                self.assertTrue(
                    any(
                        row.route == "maintenance" and row.outcome == "denied"
                        for row in rows
                    )
                )
                self.assertTrue(
                    any(
                        row.route == "configuration" and row.outcome == "denied"
                        for row in rows
                    )
                )
                self.assertTrue(
                    any(row.route == "policy" and row.outcome == "completed" for row in rows)
                )
            finally:
                await app.stop()

    async def test_autonomy_mode_and_pause_survive_application_restart(self) -> None:
        from novacontrol.application import NovaControlApplication

        with TemporaryDirectory() as directory:
            first = NovaControlApplication(data_dir=directory)
            await first.start()
            try:
                first.self_management.pause(reason="survive restart")
                first.self_management.configure(
                    mode="automatic", max_actions_per_window=7
                )
            finally:
                await first.stop()
            second = NovaControlApplication(data_dir=directory)
            await second.start()
            try:
                policy = second.self_management_status()["recovery"]
                self.assertEqual(policy["mode"], "automatic")
                self.assertTrue(policy["paused"])
                self.assertEqual(policy["max_actions_per_window"], 7)
            finally:
                await second.stop()

    async def test_status_reports_real_component_and_incident_state(self) -> None:
        from novacontrol.application import NovaControlApplication

        with TemporaryDirectory() as directory:
            app = NovaControlApplication(data_dir=directory)
            await app.start()
            try:
                result = await app.self_management_health()
                status = app.self_management_status()
                self.assertEqual(status["health_checks"], 1)
                self.assertIsNotNone(status["health"])
                self.assertIn("self_management", app.status())
                escalated = app.self_management.incidents.observe(
                    (
                        diagnosis(
                            component="Storage",
                            fingerprint="fp-status-escalated",
                            evidence=("Storage: controlled status probe",),
                        ),
                    )
                )[0]
                app.self_management.incidents.escalate(escalated.id, "needs operator review")
                status = app.self_management_status()
                self.assertIn(
                    escalated.id,
                    {row["id"] for row in status["active_incidents"]},
                )
                self.assertNotIn(
                    escalated.id,
                    {row["id"] for row in status["open_incidents"]},
                )
                self.assertGreaterEqual(len(result["health"]["components"]), 34)
                self.assertTrue(
                    {"Event bus", "Memory services", "Scheduler", "API server"}
                    <= {row["component"] for row in result["health"]["components"]}
                )
            finally:
                await app.stop()


@unittest.skipIf(TestClient is None, "FastAPI TestClient is unavailable")
class ApiTests(unittest.TestCase):
    def test_self_management_routes_return_real_state_and_enforce_approval(self) -> None:
        from novacontrol.api.app import create_app

        app = create_app()
        with TestClient(app) as client:
            status = client.get("/self-management")
            self.assertEqual(status.status_code, 200)
            self.assertIn("boundaries", status.json())
            health = client.post("/self-management/health", json={})
            self.assertEqual(health.status_code, 200)
            self.assertIn("health", health.json())
            incidents = client.get("/self-management/incidents")
            self.assertEqual(incidents.status_code, 200)
            self.assertIn("report", incidents.json())
            preview = client.post("/self-management/maintenance/preview", json={})
            self.assertEqual(preview.status_code, 200)
            config = client.get("/self-management/configuration")
            self.assertEqual(config.status_code, 200)
            version = config.json()["active_version"]
            refused = client.post(
                "/self-management/configuration/restore",
                json={"version": version, "approved": False},
            )
            self.assertEqual(refused.status_code, 403)
            policy = client.post("/self-management/policy", json={"mode": "not-a-mode"})
            self.assertEqual(policy.status_code, 422)
            paused = client.post("/self-management/pause", json={"reason": "test"})
            self.assertTrue(paused.json()["paused"])
            resumed = client.post("/self-management/resume", json={})
            self.assertFalse(resumed.json()["paused"])


__all__ = ["HealthTests", "DiagnosisTests", "IncidentTests", "RecoveryTests", "MaintenanceTests", "ConfigurationTests", "ApplicationIntegrationTests", "ApiTests"]
