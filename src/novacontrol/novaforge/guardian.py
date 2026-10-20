"""Forge Guardian (NF-02): explicit safety state and boundaries.

Guardian is a POLICY over the one session record, not a second controller. The
manager is the only thing that moves a session or releases input; this module
answers "what is enforced, what is not, and why" in one place so the API, the
panel and the report cannot disagree about it.

The important distinction is between a guarantee that is **enforced** and one
that is **vacuous because the capability is not wired in this session**:

* time limits and emergency stop ARE enforced by the manager;
* input safety is enforced by :func:`input_safety_decision`, which the manager
  asks before every injection. It refuses unless the session is RUNNING, its
  target identity was confirmed for THIS run, the action policy allows the
  request and the action budget still has room. Whether an input path is wired
  at all is a per-session fact (``record.capabilities['input_path']``), so a
  test double, a non-Windows host and a real session report the truth instead of
  a build-wide flag;
* the action budget is enforced where an execution path exists to count
  against, and reported unenforced where it does not.
"""

from __future__ import annotations

from typing import Any

from .models import SESSION_TERMINAL_STATES, SessionRecord, SessionState

GUARDIAN_VERSION = 2


def input_path_available(record: SessionRecord) -> bool:
    """Whether THIS session has a wired gameplay input path.

    A property of the session's runtime (set when the manager started it with an
    injector), not a build-wide constant: a manager constructed without one — a
    test, a host where the capture worker cannot start — must report the absence
    rather than inherit a claim from another machine's configuration.
    """
    return bool(record.capabilities.get("input_path", False))


def action_budget_enforced(record: SessionRecord) -> bool:
    """A budget bounds a session only when an action path can consume it."""
    return bool(record.capabilities.get("action_budget_enforced", False))


def safety_state(record: SessionRecord) -> str:
    """The Guardian's one-word view of the session, derived from its state."""
    if record.state in SESSION_TERMINAL_STATES:
        return "closed"
    return {
        SessionState.READY: "unarmed",
        SessionState.STARTING: "starting",
        SessionState.RUNNING: "observing",
        SessionState.PAUSING: "holding",
        SessionState.PAUSED: "held",
        SessionState.STOPPING: "stopping",
    }.get(record.state, "unknown")


def _target_state(record: SessionRecord) -> tuple[str, str]:
    """(state, reason) for target-window validation, without guessing.

    A terminal session is reported ``closed`` FIRST: a session that ended after a
    target loss is still closed, and saying "lost" forever would describe a
    running-session condition that no longer applies (the loss itself stays in
    the telemetry counters and in the recorded error).
    """
    if record.target is None:
        return "unvalidated", "The session has no validated target window."
    if record.state in SESSION_TERMINAL_STATES:
        return "closed", "The session is closed; no further geometry checks or input occur."
    if record.pause_reason == "target_lost":
        return "lost", "The target window closed; resume is refused until it is reopened."
    if record.pause_reason == "target_changed":
        return "changed", "A different window now carries the title; resume is refused."
    if record.pause_reason == "target_unreadable":
        return "unreadable", "The geometry probe could not read the target; input is not allowed."
    if record.state is SessionState.PAUSED:
        return "held", "The session is paused; no input is allowed while held."
    if record.state is SessionState.RUNNING:
        if record.target_validated:
            return "valid", "The newest geometry check matched the pinned handle and pid."
        return "pending", "The loop has not confirmed the target identity for this run yet."
    return "not_running", f"The session is {record.state.value}; input is only allowed while running."


def input_safety_decision(record: SessionRecord) -> dict[str, Any]:
    """Whether an input-producing operation may proceed, and why.

    NF-02 has no gameplay input path. This function exists so a future caller
    cannot forget the two checks that make input safe: the session must be
    RUNNING against the window it validated. Today it also refuses because the
    path itself does not exist — a refusal, not a capability claim.
    """
    if record.state in SESSION_TERMINAL_STATES:
        return {
            "allow": False,
            "state": "closed",
            "reason": f"The session is {record.state.value}; no input is allowed.",
        }
    if record.state in {SessionState.PAUSING, SessionState.PAUSED}:
        return {
            "allow": False,
            "state": "held",
            "reason": f"The session is {record.state.value}; input is not allowed while held.",
        }
    if record.state is not SessionState.RUNNING:
        return {
            "allow": False,
            "state": "not_running",
            "reason": f"Input is only allowed while RUNNING; the session is {record.state.value}.",
        }
    if record.target is None:
        return {
            "allow": False,
            "state": "unvalidated",
            "reason": "No target window was validated for this session.",
        }
    if record.pause_reason:
        return {
            "allow": False,
            "state": "held",
            "reason": f"The session is held ({record.pause_reason}); input is not allowed.",
        }
    if not record.target_validated:
        return {
            "allow": False,
            "state": "pending",
            "reason": (
                "The observation loop has not confirmed the target identity for the "
                "current run; input is not allowed."
            ),
        }
    if not input_path_available(record):
        return {
            "allow": False,
            "state": "unsupported",
            "reason": (
                "No gameplay input path is wired to this session, so nothing can be "
                "injected. This refusal is a capability statement, not a policy that "
                "could be bypassed."
            ),
        }
    budget = record.mission.max_actions
    if budget is not None and record.actions_executed >= budget:
        return {
            "allow": False,
            "state": "budget_exhausted",
            "reason": (
                f"The {budget}-action budget is exhausted ({record.actions_executed} executed); "
                "no further action is allowed in this session."
            ),
        }
    if budget is not None and not action_budget_enforced(record):
        return {
            "allow": False,
            "state": "budget_unenforced",
            "reason": (
                "An action budget is configured but cannot be enforced for this session, "
                "so input is refused rather than sent without a bound."
            ),
        }
    return {
        "allow": True,
        "state": "allowed",
        "reason": (
            "The session is running against the target it validated, the input path is "
            "wired, and the action budget (when configured) has room."
        ),
        "remaining_actions": (
            None if budget is None else max(0, budget - record.actions_executed)
        ),
    }


def guardian_state(
    record: SessionRecord,
    *,
    frame_interval_seconds: float,
    max_frames_on_disk: int,
    max_sessions_kept: int,
    stop_timeout_seconds: float,
) -> dict[str, Any]:
    """The Guardian payload, built from the authoritative session record."""
    target_state, target_reason = _target_state(record)
    target = record.target
    cleanup = dict(record.input_cleanup or {})
    cleanup_view: dict[str, Any]
    if not cleanup:
        cleanup_view = {
            "state": "not_run",
            "ok": None,
            "held_after": None,
            "presses": None,
            "releases": None,
            "note": "Input cleanup runs when the session closes; it has not run yet.",
        }
    else:
        ok = cleanup.get("ok")
        cleanup_view = {
            "state": "ok" if ok is True else "failed" if ok is False else "unknown",
            "ok": ok if isinstance(ok, bool) else None,
            "held_after": cleanup.get("held_after"),
            "presses": cleanup.get("presses"),
            "releases": cleanup.get("releases"),
            "note": str(cleanup.get("note", "") or ""),
        }

    time_limit = record.mission.time_limit_seconds
    action_budget = record.mission.max_actions
    path_available = input_path_available(record)
    budget_enforced = action_budget_enforced(record)
    verification_supported = bool(record.capabilities.get("objective_verifier", False))
    terminal = record.state in SESSION_TERMINAL_STATES
    interventions = [
        dict(entry) for entry in record.interventions[-8:] if isinstance(entry, dict)
    ]

    guarantees: list[dict[str, Any]] = [
        {
            "name": "time_limit",
            "enforced": time_limit is not None,
            "state": "enforced" if time_limit is not None else "not_configured",
            "detail": (
                "Checked while running and while paused; reaching it ends the session "
                "STOPPED/time_limit and releases input."
                if time_limit is not None
                else "No time limit is configured for this mission."
            ),
        },
        {
            "name": "emergency_stop",
            "enforced": True,
            "state": "enforced",
            "detail": (
                "Cancels the loop within a bounded wait, releases input through the "
                "verified guard path, records the outcome and is idempotent."
            ),
        },
        {
            "name": "input_release",
            "enforced": True,
            "state": "enforced",
            "detail": "Runs in the session's single finalize path for stop, cancel, failure and restart.",
        },
        {
            "name": "target_identity",
            "enforced": True,
            "state": "enforced",
            "detail": "Handle and pid are pinned at start and checked on every tick and on resume.",
        },
        {
            "name": "action_budget",
            "enforced": action_budget is not None and budget_enforced,
            "state": (
                "enforced" if action_budget is not None and budget_enforced
                else "configured_not_enforceable" if action_budget is not None
                else "not_configured"
            ),
            "detail": (
                f"Every action is counted against the {action_budget} bound before injection; "
                "the next action is refused once it is exhausted."
                if action_budget is not None and budget_enforced
                else "An action budget is configured but this session has no action path, "
                "so there is no count to bound."
                if action_budget is not None
                else "No action budget is configured for this mission."
            ),
        },
        {
            "name": "gameplay_input",
            "enforced": path_available,
            "state": "enforced" if path_available else "unsupported",
            "detail": (
                "Every injected key/button goes through the InputGuard, which releases "
                "what it held on success, error, cancellation, timeout, stop and target loss."
                if path_available
                else "No gameplay input path is wired to this session; no key or click is sent."
            ),
        },
        {
            "name": "objective_verification",
            "enforced": verification_supported,
            "state": "enforced" if verification_supported else "unsupported",
            "detail": (
                "A verdict is read from the target window (not from the action record) and "
                "reported as verified_success / verified_failure / unknown."
                if verification_supported
                else "No verifier is attached, so no gameplay outcome can be claimed."
            ),
        },
    ]

    return {
        "version": GUARDIAN_VERSION,
        "safety_state": safety_state(record),
        "session_state": record.state.value,
        "terminal": terminal,
        "target": {
            "state": target_state,
            "valid": target_state == "valid",
            "validated": record.target_validated,
            "title": target.title if target else "",
            "handle": target.handle if target else None,
            "pid": target.pid if target else None,
            "last_checked_at": record.telemetry.last_target_check_at,
            "reason": target_reason,
        },
        "input": input_safety_decision(record),
        "input_path_available": path_available,
        "verification": {
            "supported": verification_supported,
            "outcome": str(record.verification.get("outcome", "")) or None,
            "achieved": record.verification.get("achieved"),
            "reason": str(record.verification.get("reason", "")) or (
                "No verdict has been produced for this session yet."
                if verification_supported
                else "No objective verifier is attached to this session."
            ),
        },
        "limits": {
            "time_limit": {
                "configured": time_limit is not None,
                "seconds": time_limit,
                "enforced": time_limit is not None,
                "state": "enforced" if time_limit is not None else "not_configured",
            },
            "action_budget": {
                "configured": action_budget is not None,
                "value": action_budget,
                "used": record.actions_executed,
                "remaining": (
                    None if action_budget is None else max(0, action_budget - record.actions_executed)
                ),
                "enforced": action_budget is not None and budget_enforced,
                "state": (
                    "enforced" if action_budget is not None and budget_enforced
                    else "configured_not_enforceable" if action_budget is not None
                    else "not_configured"
                ),
                "reason": (
                    "Every action is counted before injection; the next one is refused once "
                    "the budget is exhausted."
                    if action_budget is not None and budget_enforced
                    else "No action-execution path exists for this session, so the budget "
                    "cannot be enforced."
                    if action_budget is not None
                    else "No action budget is configured."
                ),
            },
            "observation_loop": {
                "interval_seconds": frame_interval_seconds,
                "max_frames_on_disk": max_frames_on_disk,
                "max_sessions_kept": max_sessions_kept,
                "pending_operations": 1,
                "retries": 0,
                "bounded": True,
            },
        },
        "controls": {
            "emergency_stop": {
                "available": not terminal,
                "idempotent": True,
                "bounded_seconds": stop_timeout_seconds,
                "endpoint": f"/novaforge/sessions/{record.session_id}/emergency-stop",
            },
            "cancel": {
                "available": not terminal,
                "idempotent": True,
                "bounded_seconds": stop_timeout_seconds,
            },
        },
        "cleanup": cleanup_view,
        "interventions": interventions,
        "guarantees": guarantees,
    }
