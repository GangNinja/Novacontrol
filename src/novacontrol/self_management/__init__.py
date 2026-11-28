"""Phase 34 — trustworthy self-management over existing NovaControl services."""

from __future__ import annotations

from novacontrol.self_management.configuration import ConfigurationManager
from novacontrol.self_management.diagnosis import diagnose, explain_unknown
from novacontrol.self_management.health import HealthCollector
from novacontrol.self_management.incidents import IncidentManager, InvalidIncidentTransition
from novacontrol.self_management.maintenance import MaintenanceService
from novacontrol.self_management.models import (
    AutonomyMode,
    CauseConfidence,
    ComponentHealth,
    ConfigReport,
    ConfigVersion,
    Diagnosis,
    HealthSnapshot,
    Incident,
    IncidentSeverity,
    IncidentState,
    MaintenanceItem,
    MaintenanceKind,
    MaintenanceReport,
    RecoveryAction,
    RecoveryAttempt,
    RecoveryStatus,
)
from novacontrol.self_management.recovery import (
    RecoveryManager,
    RecoveryRefused,
    RecoveryRegistry,
    RegisteredAction,
)
from novacontrol.self_management.service import SelfManagementService

__all__ = [
    "AutonomyMode",
    "CauseConfidence",
    "ComponentHealth",
    "ConfigReport",
    "ConfigVersion",
    "ConfigurationManager",
    "Diagnosis",
    "HealthCollector",
    "HealthSnapshot",
    "Incident",
    "IncidentManager",
    "IncidentSeverity",
    "IncidentState",
    "InvalidIncidentTransition",
    "MaintenanceItem",
    "MaintenanceKind",
    "MaintenanceReport",
    "MaintenanceService",
    "RecoveryAction",
    "RecoveryAttempt",
    "RecoveryManager",
    "RecoveryRefused",
    "RecoveryRegistry",
    "RecoveryStatus",
    "RegisteredAction",
    "SelfManagementService",
    "diagnose",
    "explain_unknown",
]
