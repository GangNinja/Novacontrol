"""Deterministic, evidence-labelled operational diagnosis.

The rules are deliberately ordinary: a direct probe failure is CONFIRMED, an
event pattern that agrees with a degraded probe is PROBABLE, and a weak signal
is POSSIBLE.  No language model is consulted here, so diagnosis still works
when every provider is down.  The structured result leaves room for an offline
operator or a Phase 28 investigator to add interpretation without upgrading the
confidence of a guess.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from novacontrol.core.diagnostics import HealthState
from novacontrol.self_management.models import (
    CauseConfidence,
    ComponentHealth,
    Diagnosis,
    HealthSnapshot,
    IncidentSeverity,
)

#: A small, reviewable table.  The key is the component name used by the Phase
#: 14 roster; the value says what a failure of that component usually means and
#: which corrective action IDs make sense.  A component absent from this table
#: still produces an evidence-backed finding, and its cause stays UNKNOWN.
_RULES: Mapping[str, tuple[str, str, tuple[str, ...], str]] = {
    "Core": (
        "The application's core runtime or event path is unavailable.",
        "core_runtime_unavailable",
        ("reinitialize-core",),
        "Requests and background work may stop being dispatched.",
    ),
    "Event bus": (
        "The event bus could not complete a round trip.",
        "event_bus_unresponsive",
        ("reinitialize-event-bus",),
        "Live progress, audit correlation and module lifecycle announcements may be lost.",
    ),
    "API server": (
        "The HTTP surface is not reporting as ready.",
        "api_server_unavailable",
        (),
        "The desktop interface and API clients cannot rely on this process.",
    ),
    "Desktop interface": (
        "The static interface assets are missing or unreadable.",
        "desktop_assets_unavailable",
        (),
        "The browser UI may load an incomplete page.",
    ),
    "Database": (
        "The persistent state store could not be read.",
        "database_unavailable",
        ("reinitialize-storage",),
        "Settings, tasks, incidents and other durable state may not survive a restart.",
    ),
    "Storage": (
        "The data volume is unwritable or critically low on space.",
        "storage_unavailable",
        (),
        "Persistent writes can fail; model pulls and maintenance may be unsafe.",
    ),
    "Ollama": (
        "The local model runtime is not answering its status probe.",
        "provider_unreachable",
        ("reconnect-provider",),
        "Local model requests cannot be served until the runtime returns.",
    ),
    "Model manager": (
        "The shared model lifecycle manager cannot report a usable runtime.",
        "model_manager_unavailable",
        ("reconnect-provider",),
        "Model selection, residency and keep-alive decisions are degraded.",
    ),
    "Models": (
        "No usable model is declared or discovered.",
        "model_catalogue_empty",
        (),
        "Model-backed chat and research fall back to deterministic paths.",
    ),
    "Tools": (
        "The tool registry is empty or failing.",
        "tool_registry_degraded",
        (),
        "Planned actions that require tools cannot execute.",
    ),
    "Plugin system": (
        "One or more plugins were rejected or failed to initialise.",
        "plugin_validation_degraded",
        (),
        "The affected plugin capabilities are unavailable; unrelated tools remain usable.",
    ),
    "Agent execution": (
        "Agent execution is reporting failures or is not wired.",
        "agent_execution_degraded",
        (),
        "Delegated and collaborative work may fail or return unverified results.",
    ),
    "Memory services": (
        "The long-term memory service reported a load or persistence error.",
        "memory_service_degraded",
        ("reload-memory",),
        "Personal context and recall may be incomplete; stored data is not deleted.",
    ),
    "Context": (
        "The shared memory/context path is not reporting as ready.",
        "context_degraded",
        ("reload-memory",),
        "Answers may lose conversational or personal context.",
    ),
    "Scheduler": (
        "The scheduled-work loop is not running.",
        "scheduler_stalled",
        ("restart-scheduler",),
        "Due automations and recurring work will not start on time.",
    ),
    "Background workers": (
        "One or more background workers are stopped or stalled.",
        "background_worker_stalled",
        ("restart-scheduler",),
        "Persistent operations and monitoring passes may stop making progress.",
    ),
    "Scheduled workflows": (
        "The workflow store or its scheduler loop is degraded.",
        "workflow_scheduler_degraded",
        ("restart-scheduler",),
        "Recurring workflows may miss their next run.",
    ),
    "Computer operation": (
        "The computer-operation supervisor is not available.",
        "computer_supervisor_degraded",
        (),
        "Computer-operation tasks cannot be started safely.",
    ),
    "Hardware manager": (
        "The resource governor reports an unsafe machine reading.",
        "resource_pressure",
        (),
        "Model loads and long-running tasks may be refused or become unstable.",
    ),
    "Configuration": (
        "The persisted configuration failed validation or differs from its last known-good snapshot.",
        "configuration_invalid",
        ("restore-known-good-config",),
        "New work may use inconsistent settings; the previous snapshot is still available.",
    ),
    "GPU": (
        "The GPU probe is unavailable.",
        "gpu_unavailable",
        (),
        "GPU acceleration cannot be selected; CPU paths remain available.",
    ),
    "NPU": (
        "The NPU probe is unavailable.",
        "npu_unavailable",
        (),
        "NPU acceleration cannot be selected; CPU paths remain available.",
    ),
}

#: A short probe that takes longer than this is a latency signal, not a failure.
#: It is deliberately generous: the health roster includes storage and runtime
#: probes that are allowed to be slow on a busy machine.
_LATENCY_SIGNAL_MS = 5_000.0


def _fingerprint(rule_id: str, component: str) -> str:
    return hashlib.sha256(f"{rule_id}|{component}".encode("utf-8")).hexdigest()[:20]


def _event_evidence(snapshot: HealthSnapshot, component: str) -> tuple[str, ...]:
    rows = [
        row
        for row in snapshot.event_signals
        if str(row.get("component", "")) == component
    ]
    evidence: list[str] = []
    for row in rows:
        evidence.append(f"{int(row.get('failures', 0) or 0)} recent failure event(s)")
        evidence.extend(str(item) for item in row.get("examples", ()) if str(item))
    return tuple(evidence[:5])


def _event_count(snapshot: HealthSnapshot, component: str) -> int:
    return sum(
        int(row.get("failures", 0) or 0)
        for row in snapshot.event_signals
        if str(row.get("component", "")) == component
    )


def _row_diagnosis(row: ComponentHealth, snapshot: HealthSnapshot) -> Diagnosis | None:
    if row.status.healthy:
        return None
    # An accelerator the platform does not expose is an optional capability, not
    # an operational incident. Reporting it as "unknown" is honest; opening a
    # recurring incident for it would be a false alarm on every CPU-only host.
    if row.component in {"GPU", "NPU"} and row.status is HealthState.UNKNOWN:
        return None
    title, rule_id, actions, impact = _RULES.get(
        row.component,
        (
            f"{row.component} reported {row.status.value}.",
            "component_degraded",
            (),
            "The affected capability may be unavailable or unreliable.",
        ),
    )
    if row.component == "Hardware manager" and row.status is HealthState.DEGRADED:
        # The governor's comfort/tight line is a warning, not proof that the
        # machine is unsafe. Reserve that wording for a failing probe.
        title = "The resource governor reports a tight machine reading."
    event_evidence = _event_evidence(snapshot, row.component)
    failures = _event_count(snapshot, row.component)
    # A failed probe is a direct observation of the component, but a probe that
    # says "degraded" does not prove WHY.  Direct component failures are
    # confirmed; optional/derived readings stay probable or possible.
    if row.status is HealthState.FAILING:
        confidence = CauseConfidence.CONFIRMED
        rationale = "the component's own live probe failed"
    elif row.status is HealthState.DEGRADED:
        confidence = CauseConfidence.PROBABLE
        rationale = "the component's live probe reported degradation"
    else:
        confidence = CauseConfidence.POSSIBLE
        rationale = "the component could not be read; this is a possibility, not a confirmed cause"
    if failures >= 2:
        confidence = CauseConfidence.CONFIRMED if row.status is HealthState.FAILING else CauseConfidence.PROBABLE
        rationale += f"; {failures} recent failure event(s) corroborate the reading"
    # Probe latency is a measurement, not a stable fact: keeping it in the
    # evidence list adds a new unique "probe duration" entry on every pass and
    # crowds out the actual symptom. It stays in metadata instead.
    evidence = tuple([f"{row.component}: {row.message}"] + list(event_evidence))
    return Diagnosis(
        rule_id=rule_id,
        component=row.component,
        title=title,
        cause=row.message or title,
        confidence=confidence,
        severity=row.severity,
        evidence=evidence,
        impact=impact,
        recommendations=actions,
        rationale=rationale,
        fingerprint=_fingerprint(rule_id, row.component),
        metadata={
            "status": row.status.value,
            "category": row.category,
            "probe_message": row.message,
            "probe_duration_ms": row.duration_ms,
            "event_failures": failures,
        },
    )


def _resource_diagnosis(snapshot: HealthSnapshot) -> Diagnosis | None:
    metrics = snapshot.metrics
    free = metrics.get("free_ram_bytes")
    critical = metrics.get("critical_ram_bytes")
    cpu = metrics.get("cpu_percent")
    max_cpu = metrics.get("max_cpu_percent")
    reasons: list[str] = []
    if isinstance(free, (int, float)) and isinstance(critical, (int, float)):
        if free < critical:
            reasons.append(
                f"available RAM {int(free)} bytes is below the critical line {int(critical)} bytes"
            )
    if isinstance(cpu, (int, float)) and isinstance(max_cpu, (int, float)):
        if cpu > max_cpu:
            reasons.append(f"CPU {float(cpu):.1f}% is above the configured {float(max_cpu):.1f}% ceiling")
    if not reasons:
        return None
    title, rule_id, actions, impact = _RULES["Hardware manager"]
    return Diagnosis(
        rule_id=rule_id,
        component="Hardware manager",
        title=title,
        cause="; ".join(reasons),
        confidence=CauseConfidence.CONFIRMED,
        severity=IncidentSeverity.ERROR
        if isinstance(free, (int, float)) and isinstance(critical, (int, float)) and free < critical
        else IncidentSeverity.WARNING,
        evidence=tuple(reasons),
        impact=impact,
        recommendations=actions,
        rationale="the resource governor's live reading crossed a configured safety line",
        fingerprint=_fingerprint(rule_id, "Hardware manager"),
        metadata={"metrics": dict(metrics)},
    )


def _latency_diagnosis(row: ComponentHealth) -> Diagnosis | None:
    if row.duration_ms is None or row.duration_ms <= _LATENCY_SIGNAL_MS:
        return None
    if not row.status.healthy:
        return None
    return Diagnosis(
        rule_id="component_latency_high",
        component=row.component,
        title=f"{row.component} responded unusually slowly.",
        cause=f"the health probe took {row.duration_ms:.0f} ms",
        confidence=CauseConfidence.POSSIBLE,
        severity=IncidentSeverity.WARNING,
        evidence=(f"probe duration {row.duration_ms:.1f} ms exceeds {_LATENCY_SIGNAL_MS:.0f} ms",),
        impact="A slow component can delay requests and may indicate contention or a resource leak.",
        recommendations=(),
        rationale="latency alone is a symptom; the cause is not confirmed by this reading",
        fingerprint=_fingerprint("component_latency_high", row.component),
        metadata={"duration_ms": row.duration_ms},
    )


def diagnose(
    snapshot: HealthSnapshot,
    *,
    configuration_issue: str = "",
) -> tuple[Diagnosis, ...]:
    """Return one finding per affected component, with explicit confidence."""
    findings: list[Diagnosis] = []
    seen: set[str] = set()
    for row in snapshot.components:
        finding = _row_diagnosis(row, snapshot)
        if finding is not None:
            findings.append(finding)
            seen.add(finding.component)
        slow = _latency_diagnosis(row)
        if slow is not None and slow.component not in seen:
            findings.append(slow)
            seen.add(slow.component)
    resource = _resource_diagnosis(snapshot)
    if resource is not None:
        findings.append(resource)
        seen.add(resource.component)
    if configuration_issue:
        title, rule_id, actions, impact = _RULES["Configuration"]
        findings.append(
            Diagnosis(
                rule_id=rule_id,
                component="Configuration",
                title=title,
                cause=configuration_issue,
                confidence=CauseConfidence.CONFIRMED,
                severity=IncidentSeverity.ERROR,
                evidence=(configuration_issue,),
                impact=impact,
                recommendations=actions,
                rationale="the persisted configuration failed the same validator the application uses",
                fingerprint=_fingerprint(rule_id, "Configuration"),
                metadata={"issue": configuration_issue},
            )
        )
    # Event-only failures remain evidence-backed even when the roster is healthy:
    # the events were published by real work, and ignoring them because a probe
    # passed would hide repeated operational failures.
    for signal in snapshot.event_signals:
        component = str(signal.get("component", ""))
        failures = int(signal.get("failures", 0) or 0)
        if component in seen or failures < 2:
            continue
        title, rule_id, actions, impact = _RULES.get(
            component,
            (
                f"{component} has repeated execution failures.",
                "repeated_failures",
                (),
                "The affected work may fail repeatedly until the cause is addressed.",
            ),
        )
        findings.append(
            Diagnosis(
                rule_id=rule_id,
                component=component,
                title=title,
                cause=f"{failures} failure events were observed without a failed health probe",
                confidence=CauseConfidence.PROBABLE,
                severity=IncidentSeverity.WARNING,
                evidence=_event_evidence(snapshot, component),
                impact=impact,
                recommendations=actions,
                rationale="repeated failure events are evidence, but no live component probe confirmed the cause",
                fingerprint=_fingerprint(rule_id, component),
                metadata={"event_failures": failures},
            )
        )
    return tuple(findings)


def explain_unknown(component: str, snapshot: HealthSnapshot | None = None) -> Diagnosis:
    """The honest answer when an investigation finds no supported cause."""
    evidence: tuple[str, ...] = ()
    if snapshot is not None:
        evidence = tuple(
            f"{row.component}: {row.status.value} — {row.message}"
            for row in snapshot.components
            if row.component == component
        )
    return Diagnosis(
        rule_id="unknown_cause",
        component=component,
        title=f"No supported cause was identified for {component}.",
        cause="unknown",
        confidence=CauseConfidence.UNKNOWN,
        severity=IncidentSeverity.WARNING,
        evidence=evidence,
        impact="The symptom is real, but the available evidence does not identify a cause.",
        recommendations=("collect more logs or a targeted probe before taking corrective action",),
        rationale="no rule matched confirmed or corroborated evidence; a guess would be unsafe",
        fingerprint=_fingerprint("unknown_cause", component),
        metadata={"unknown": True},
    )


__all__ = ["diagnose", "explain_unknown"]
