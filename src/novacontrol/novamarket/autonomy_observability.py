"""AT-06 operational reliability report for the autonomous paper pipeline.

This module is deliberately read-only.  It composes the status documents the
existing runtime, monitoring, decision, paper-execution and exit pipelines
already own; it does not open an engine, mutate financial state, retry an
operation or create a second source of truth.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

AT06_CONTRACT_VERSION = "at06-reliability-1.0"

PIPELINE_PHASE = "AT-06"

#: Stable failure categories.  Codes come from the existing AT-01..AT-05
#: contracts; unmapped codes stay ``unknown`` rather than being guessed.
FAILURE_DOMAINS: dict[str, str] = {
    "RUNTIME_DISABLED": "runtime",
    "RUNTIME_PAUSED": "runtime",
    "RUNTIME_NOT_AUTONOMOUS": "runtime",
    "LEASE_INVALID": "lease",
    "LEASE_CONFLICT": "lease",
    "ALREADY_RUNNING": "scheduler",
    "CYCLE_TIMEOUT": "scheduler",
    "CYCLE_CANCELLED": "scheduler",
    "CALENDAR_UNAVAILABLE": "calendar",
    "CALENDAR_UNVERIFIED": "calendar",
    "OUTSIDE_TRADING_SESSION": "session",
    "EXCHANGE_HOLIDAY": "session",
    "MARKET_DATA_UNAVAILABLE": "market_data",
    "STALE_MARKET_DATA": "market_data",
    "DISCOVERY_PROVIDER_FAILURE": "discovery",
    "INVALID_INSTRUMENT": "discovery",
    "NO_CANDIDATES": "discovery",
    "MONITORING_DISABLED": "monitoring",
    "RISK_APPROVAL_INVALID": "risk",
    "RISK_ASSESSMENT_FAILED": "risk",
    "PROPOSAL_EXPIRED": "proposal",
    "EXECUTION_PREFLIGHT_FAILED": "execution",
    "EXECUTION_SUBMISSION_UNKNOWN": "execution",
    "NM10_UNAVAILABLE": "execution",
    "ORDER_REJECTED": "execution",
    "RECONCILIATION_REQUIRED": "reconciliation",
    "RECONCILIATION_FAILED": "reconciliation",
    "EXIT_RECONCILIATION_FAILED": "reconciliation",
    "LEDGER_MISMATCH": "ledger",
    "POSITION_MISMATCH": "ledger",
    "LEARNING_EVIDENCE_FAILED": "learning",
    "AUTHORIZATION_FAILED": "authorization",
    "DATABASE_FAILURE": "database",
}

FAILURE_DOMAIN_ORDER: tuple[str, ...] = (
    "runtime",
    "scheduler",
    "lease",
    "calendar",
    "session",
    "market_data",
    "discovery",
    "monitoring",
    "strategy",
    "risk",
    "proposal",
    "execution",
    "reconciliation",
    "ledger",
    "learning",
    "authorization",
    "database",
    "unknown",
)


def failure_domain(code: object) -> str:
    """Classify one existing error code without inventing a cause."""

    text = str(code or "").strip().upper()
    if not text:
        return "unknown"
    return FAILURE_DOMAINS.get(text, "unknown")


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _int(value: object) -> int:
    try:
        return int(str(value or 0))
    except (TypeError, ValueError):
        return 0


def _text(value: object) -> str:
    return str(value or "").strip()


def _health(value: object) -> dict[str, Any]:
    body = _mapping(value)
    state = _text(body.get("state")) or "unknown"
    reasons = [str(item) for item in (body.get("reasons") or []) if str(item)]
    return {"state": state, "reasons": reasons}


def _iso(value: object) -> str:
    text = _text(value)
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return text


def build_reliability_report(
    *,
    runtime: Mapping[str, Any] | None,
    monitoring: Mapping[str, Any] | None,
    decisions: Mapping[str, Any] | None,
    execution: Mapping[str, Any] | None,
    exits: Mapping[str, Any] | None,
    learning: Mapping[str, Any] | None = None,
    recovery: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose one honest operational view from the existing status records."""

    run = _mapping(runtime)
    monitor = _mapping(monitoring)
    decision = _mapping(decisions)
    paper = _mapping(execution)
    exit_status = _mapping(exits)
    learn = _mapping(learning)
    recover = _mapping(recovery)

    execution_counts = _mapping(paper.get("counts"))
    decision_counts = _mapping(decision.get("counts"))
    monitoring_scheduler = _mapping(monitor.get("scheduler"))
    last_monitor = _mapping(monitor.get("last_cycle"))
    exit_counts = _mapping(exit_status.get("counts"))
    runtime_health = _health(run.get("health"))
    monitor_health = _health(monitor.get("health"))
    decision_health = _health(decision.get("health"))
    execution_health = _health(paper.get("health"))
    exit_health = _health(
        {"state": "degraded" if exit_status.get("last_error") else "ok",
         "reasons": [exit_status.get("last_error")] if exit_status.get("last_error") else []}
    )

    reasons: list[str] = []
    for label, body in (
        ("runtime", runtime_health),
        ("monitoring", monitor_health),
        ("decisions", decision_health),
        ("execution", execution_health),
        ("exits", exit_health),
    ):
        for reason in body["reasons"]:
            reasons.append(f"{label}: {reason}")

    unresolved_intents = sum(
        count
        for status, count in execution_counts.items()
        if _text(status) in {
            "intent",
            "forwarding",
            "unknown",
            "reconciliation_required",
        }
        for count in [_int(count)]
    )
    unresolved_exits = _int(exit_counts.get("reconciliation_required")) + _int(
        exit_counts.get("order_pending")
    )
    learning_rows = list(learn.get("evidence") or [])
    incomplete_learning = sum(
        1
        for row in learning_rows
        if isinstance(row, Mapping)
        and _text(row.get("status")).lower() not in {"complete", "completed"}
    )

    failure_codes: list[str] = []
    for body in (
        run,
        monitor,
        decision,
        paper,
        exit_status,
        recover,
    ):
        code = _text(body.get("last_error_code") or body.get("failure_code"))
        if code:
            failure_codes.append(code)
    domains: dict[str, int] = {name: 0 for name in FAILURE_DOMAIN_ORDER}
    for code in failure_codes:
        domain = failure_domain(code)
        domains[domain] = domains.get(domain, 0) + 1

    lease = _mapping(run.get("lease"))
    mode = _text(run.get("mode")) or "disabled"
    state = _text(run.get("runtime_state")) or "unknown"
    if reasons:
        health_state = "degraded"
    else:
        health_state = "ok"

    pending_orders = _int(execution_counts.get("forwarding")) + _int(
        execution_counts.get("unknown")
    )
    partial_fills = _int(execution_counts.get("partial_orders")) + _int(
        exit_counts.get("partially_filled")
    )
    filled = _int(
        execution_counts.get("filled_orders", execution_counts.get("filled"))
    ) + _int(exit_counts.get("filled"))
    open_decisions = _int(exit_status.get("open_decisions"))
    recovery_unresolved = _int(recover.get("unresolved_count"))
    if recovery_unresolved:
        reasons.append(
            f"recovery: {recovery_unresolved} operation(s) still require reconciliation"
        )
        health_state = "degraded"

    return {
        "ok": health_state == "ok",
        "phase": PIPELINE_PHASE,
        "milestone": "AT-06",
        "contract_version": AT06_CONTRACT_VERSION,
        "paper_only": bool(run.get("paper_only", True)),
        "live_trading": bool(run.get("live_trading", False)),
        "broker_connected": bool(run.get("broker_connected", False)),
        "health": {
            "state": health_state,
            "reasons": reasons,
            "failure_domains": {key: value for key, value in domains.items() if value},
            "last_failure_domain": (
                failure_domain(failure_codes[0]) if failure_codes else ""
            ),
        },
        "runtime": {
            "mode": mode,
            "runtime_state": state,
            "desired_mode": _text(run.get("desired_mode")) or "disabled",
            "started_at": _iso(run.get("started_at")),
            "last_heartbeat": _iso(run.get("heartbeat_at") or lease.get("heartbeat_at")),
            "lease_expires_at": _iso(lease.get("expires_at")),
            "lease": lease,
            "active_cycle": run.get("active_cycle") or None,
            "recovery_required": bool(run.get("recovery_required")),
            "last_error": _text(run.get("last_error")),
            "scheduler_health": monitor_health,
            "scheduler_running": bool(monitoring_scheduler.get("running")),
        },
        "pipeline": {
            "instruments_discovered": (
                _int(monitor.get("instruments_discovered"))
                or _int(last_monitor.get("processed_count"))
            ),
            "strategies_evaluated": _int(decision_counts.get("evaluations")),
            "signals_generated": _int(decision_counts.get("signals")),
            "proposals_created": _int(decision_counts.get("proposals")),
            "risk_assessments": _int(decision_counts.get("risk_assessments")),
            "execution_intents": _int(execution_counts.get("intents")),
            "paper_orders": _int(execution_counts.get("orders")),
            "fills": _int(execution_counts.get("fills")),
            "exit_decisions": _int(exit_counts.get("decisions")),
            "exit_orders": _int(exit_counts.get("orders")),
            "exit_fills": _int(exit_counts.get("fills")),
            "learning_evidence": _int(learn.get("total")) or len(learning_rows),
        },
        "financial": {
            "supervised_positions": _int(exit_status.get("supervised_positions")),
            "pending_orders": pending_orders,
            "filled_orders": filled,
            "partial_fills": partial_fills,
            "open_exit_decisions": open_decisions,
            "reconciliation_pending": unresolved_intents + unresolved_exits,
            "learning_incomplete": incomplete_learning,
        },
        "recovery": {
            "available": True,
            "last_action": _text(recover.get("status")),
            "last_at": _iso(recover.get("recovered_at")),
            "recovered": _int(recover.get("recovered")),
            "reconciled": _int(recover.get("reconciled")),
            "unresolved": list(recover.get("unresolved") or []),
            "unresolved_count": recovery_unresolved,
            "auto_resumed": False,
            "pending_orders_preserved": True,
        },
        "safety": {
            "paper_only": bool(run.get("paper_only", True)),
            "live_trading": bool(run.get("live_trading", False)),
            "broker_connected": bool(run.get("broker_connected", False)),
            "risk_authority": "NM-08",
            "execution_authority": "NM-10",
            "live_orders_possible": False,
            "kill_switch_endpoint": "/market/autonomy/stop",
            "recovery_endpoint": "/market/autonomy/recover",
        },
    }


__all__ = [
    "AT06_CONTRACT_VERSION",
    "FAILURE_DOMAINS",
    "FAILURE_DOMAIN_ORDER",
    "build_reliability_report",
    "failure_domain",
]
