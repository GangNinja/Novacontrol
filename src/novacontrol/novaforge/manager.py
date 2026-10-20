"""NovaForge session manager (NF-01 foundation, NF-02 telemetry and Guardian).

This module owns *when* a gaming session runs and *what it saw*. It does not own
how to decide or act — that belongs to a later phase — so a session here
captures real frames from one validated window, measures what happened, records
what it observed, and stops honestly. NF-02 adds bounded telemetry aggregation
and the Guardian's abort/cleanup path here rather than in a second manager.

Design commitments, each of which exists because the alternative silently lies:

* **The backend is the only source of state.** Every transition goes through
  :meth:`SessionRecord.transition`, which raises on an illegal move; the API
  returns the record the manager holds, and the UI re-renders from that.
* **A frame is fresh or it is not a frame.** The capture seam reports freshness
  (previous mtime + recency + a PNG signature); a capture that is not fresh is
  recorded as an error and an observation with ``evidence=False`` — never shown
  as live.
* **Pause is acknowledged, not requested.** ``pause()`` moves to ``PAUSING`` and
  waits for the loop to reach ``PAUSED``. If the loop does not acknowledge, the
  call fails with the state it is really in.
* **Stop releases input through the verified path.** The runner's
  ``release_all_inputs`` is always called (success, failure, cancellation,
  restart reconciliation) and its press/release counts are stored.
* **Limits are not success.** Reaching the time limit ends the session as
  ``STOPPED`` with an ``end_reason``; ``COMPLETED`` is reserved for a verified
  objective, which this phase does not have. An action budget cannot fire here
  (no actions are sent) and is reported as ``max_actions_enforced: False``
  rather than presented as a bound that works. The time limit is measured with
  ``time.monotonic()`` so a clock change cannot extend it.
* **Recording never fails the session.** Persistence and event publication are
  wrapped: losing a snapshot must not stop the observation that would be
  recorded next.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from novacontrol.persistence import JsonStateStore

from .actions import (
    ActionKind,
    ActionOutcome,
    ActionPolicy,
    ActionProposal,
    ActionRunner,
    ActionStatus,
    GameplayAction,
    ScreenInput,
    action_digest,
)
from .guardian import guardian_state, input_safety_decision, input_path_available
from .models import (
    FrameRecord,
    MissionConfig,
    MissionValidationError,
    ObservationRecord,
    SESSION_TERMINAL_STATES,
    SessionConflictError,
    SessionRecord,
    SessionState,
    SessionTransitionError,
    StageRecord,
    TargetWindow,
    utc_now,
)
from .objective import (
    ObjectiveDefinition,
    ObjectiveOutcome,
    ObjectiveRegistry,
    ObjectiveVerification,
    WindowTitleVerifier,
    utc_stamp,
)
from .telemetry import TelemetrySnapshot, gap_threshold_for
from .timeline import (
    MAX_EVENTS_PER_SESSION,
    ORDERING_NOTE,
    SOURCE_NOTE,
    TIMELINE_VERSION,
    TimelineEvent,
    TimelineLog,
    TimelineQueryError,
    classify,
    compare,
    derived_events,
    filter_events,
    frame_ref,
    intervention_ref,
    observation_ref,
    paginate,
    session_ref,
    telemetry_ref,
    summary_text,
    timeline_namespace,
    validate_query,
)

__all__ = ["NovaForgeManager", "NovaForgeStatus"]

#: Namespaces in the shared state store. Two files, both gitignored runtime data:
#: session records (bounded) and the last mission draft.
_SESSIONS_NAMESPACE = "novaforge_sessions"
_MISSION_NAMESPACE = "novaforge_mission"

#: A session that is not terminal is "active" and blocks a second start.
_ACTIVE_STATES = frozenset(
    {
        SessionState.STARTING,
        SessionState.RUNNING,
        SessionState.PAUSING,
        SessionState.PAUSED,
        SessionState.STOPPING,
    }
)


@dataclass(frozen=True, slots=True)
class NovaForgeStatus:
    """The readiness picture the UI reads once, then keeps polling per session."""

    capture_supported: bool
    windows_available: bool
    active_session: str | None
    session_count: int
    limitations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_supported": self.capture_supported,
            "windows_available": self.windows_available,
            "active_session": self.active_session,
            "session_count": self.session_count,
            "limitations": list(self.limitations),
        }


@dataclass(slots=True)
class NovaForgeManager:
    """Owns sessions, their observation loop and their persisted history.

    All desktop access is injected as coroutines so the manager is testable
    without an OS: ``capture(path)`` returns the capture result mapping (with
    ``fresh``), ``geometry(title)`` resolves a window, ``window_lines()`` lists
    them, and ``release_inputs()`` is the verified cleanup path. The application
    wires these to the SAME desktop runner a spoken command uses.
    """

    data_dir: Path
    capture: Callable[[str], Awaitable[Mapping[str, Any]]]
    geometry: Callable[[str], Awaitable[Any | None]]
    window_lines: Callable[[], Awaitable[list[str] | None]]
    release_inputs: Callable[[], Awaitable[tuple[int, int]]] | None = None
    #: The gameplay injector: press/release edges and a cursor move, all through
    #: the same DPI-aware worker that captures frames. None means this session
    #: has no action path, and the Guardian refuses every action by naming that.
    action_input: Any | None = None
    #: Reads one window's current geometry by handle. The objective verifier
    #: uses it as its INDEPENDENT evidence source; the input path cannot write
    #: to it. None means no verifier can run.
    objective_probe: Callable[[int], Awaitable[Any | None]] | None = None
    action_policy: ActionPolicy = field(default_factory=ActionPolicy)
    permissions: Any | None = None
    publish: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
    capture_supported: bool = True
    frame_interval: float = 1.0
    pause_timeout: float = 3.0
    stop_timeout: float = 8.0
    max_sessions_kept: int = 40
    max_frames_on_disk: int = 30
    #: Telemetry is aggregated every tick; the snapshot is written at most this
    #: often. Lifecycle moves (start/pause/resume/stop) persist immediately.
    persist_interval: float = 2.0
    store: JsonStateStore | None = None
    frame_root: Path | None = field(default=None, repr=False)
    #: Built in ``__post_init__`` from ``action_input``/``objective_probe``.
    action_runner: ActionRunner | None = field(default=None, init=False, repr=False)
    objectives: ObjectiveRegistry = field(default_factory=ObjectiveRegistry, init=False, repr=False)
    _sessions: dict[str, SessionRecord] = field(default_factory=dict, repr=False)
    _current_id: str | None = field(default=None, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _last_persist: float | None = field(default=None, repr=False)
    #: NF-03: one bounded event log per session. ``_timeline_dirty`` names the
    #: logs changed since the last store write, so a tick persists one small file
    #: instead of every session's history.
    _timelines: dict[str, TimelineLog] = field(default_factory=dict, repr=False)
    _timeline_dirty: set[str] = field(default_factory=set, repr=False)
    _stop_event: asyncio.Event | None = field(default=None, repr=False)
    _pause_event: asyncio.Event | None = field(default=None, repr=False)
    _running_ack: asyncio.Event | None = field(default=None, repr=False)
    _lock: asyncio.Lock | None = field(default=None, repr=False)
    #: Monotonic start of the current session, so a wall-clock adjustment cannot
    #: extend a time limit. It is not persisted: a restarted process reconciles
    #: the session instead of pretending it can still measure that run.
    _started_monotonic: float | None = field(default=None, repr=False)

    # -- construction / persistence ----------------------------------------

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.store = self.store if self.store is not None else JsonStateStore(self.data_dir)
        self.frame_root = self.frame_root or (self.data_dir / "novaforge" / "frames")
        policy_errors = self.action_policy.errors()
        if policy_errors:
            raise ValueError("Invalid gameplay action policy: " + "; ".join(policy_errors))
        # ``action_input`` is an injector (``press``/``release``/``move``) — the
        # application passes ``ScreenInput(send=worker.send_input)``, a test
        # passes a recorder. The runner owns the guard; this object only carries
        # the edges.
        self.action_runner = (
            ActionRunner(input=self.action_input) if self.action_input is not None else None
        )
        self.objectives = ObjectiveRegistry(
            verifiers=(WindowTitleVerifier(probe=self.objective_probe),)
            if self.objective_probe is not None
            else ()
        )
        self._load()

    def _load(self) -> None:
        payload = self.store.read(_SESSIONS_NAMESPACE) if self.store is not None else {}
        self._sessions = {}
        for entry in payload.get("sessions") or []:
            if not isinstance(entry, dict):
                continue
            record = SessionRecord.from_dict(entry)
            if record is not None:
                self._sessions[record.session_id] = record
        current = str(payload.get("current") or "")
        # NF-03: restore each session's persisted event log. A session that
        # predates NF-03 (or whose store is unavailable) has no log, and keeps
        # reporting a DERIVED timeline instead of pretending one was recorded.
        self._timelines = {}
        self._timeline_dirty = set()
        for record in self._sessions.values():
            log = self._load_timeline(record.session_id)
            if log is not None:
                self._timelines[record.session_id] = log
        # Reconcile: a record persisted mid-run has no loop after a restart, so
        # reporting it as RUNNING would be a stale state the UI trusts. It is
        # closed honestly as STOPPED/restart, and any held-input claim is
        # replaced with the newly measured cleanup result.
        reconciled = False
        for record in self._sessions.values():
            if record.state in _ACTIVE_STATES:
                with contextlib.suppress(SessionTransitionError):
                    record.transition(SessionState.STOPPING, reason="application_restarted")
                    record.transition(SessionState.STOPPED, reason="application_restarted")
                record.ended_at = record.ended_at or utc_now()
                record.end_reason = "application_restarted"
                message = (
                    "The application restarted while this session was active; it was closed "
                    "without claiming completion."
                )
                record.record_error(message, where="reconcile")
                record.input_cleanup = {
                    "released": 0,
                    "held_after": 0,
                    "ok": True,
                    "note": "No inputs are held by a process that has exited.",
                }
                record.telemetry.mark_interrupted()
                extra = record.add_intervention(
                    "restart_reconcile",
                    "application_restarted",
                    "stopped",
                    detail="The application restarted while this session was active.",
                )
                # Only a session NF-03 was already recording gets the reconcile
                # facts appended; a legacy session's derived timeline already
                # reconstructs them from the record (history, error, intervention,
                # cleanup), and appending here would make its timeline START at
                # the restart and lose everything before it.
                if record.session_id in self._timelines:
                    self._event(record, "session.reconciled", source="store",
                                payload={"reason": "application_restarted", "message": message})
                    self._event(record, "error.recorded", source="store",
                                payload={"message": message, "where": "reconcile"})
                    self._event(record, "guardian.intervention", source="guardian",
                                payload=dict(extra))
                    self._event(record, "input.cleanup", source="session",
                                payload=dict(record.input_cleanup))
                reconciled = True
        self._current_id = current if current in self._sessions else None
        removed = self._trim_sessions()
        if reconciled or removed:
            # The in-memory answer and the snapshot must not disagree: a file
            # still saying RUNNING (or still listing a session that was evicted
            # by the cap) would be read by the next boot (or by a person) as
            # state that is somehow alive. Recording stays
            # non-fatal through ``_persist``'s own guard.
            self._persist()

    def _trim_sessions(self) -> int:
        """Bound stored sessions AND their frames on disk.

        Keeping only the newest records in memory while leaving every evicted
        session's PNGs behind would make ``max_frames_on_disk`` a per-session
        bound with no total bound at all: the directory would grow for the life
        of the installation. An evicted record can no longer be read (its
        frames are unreachable through ``frame_path``), so its files go too.
        The active session is never evicted, even if an odd persisted
        ``created_at`` sorts it first. Returns how many records were evicted.
        """
        if len(self._sessions) <= self.max_sessions_kept:
            return 0
        ordered = sorted(self._sessions.values(), key=lambda item: item.created_at)
        excess = len(self._sessions) - self.max_sessions_kept
        removed = 0
        for record in ordered:
            if excess <= 0:
                break
            if record.session_id == self._current_id:
                continue
            self._sessions.pop(record.session_id, None)
            self._drop_frames_dir(record.session_id)
            self._drop_timeline(record.session_id)
            removed += 1
            excess -= 1
        return removed

    def _drop_frames_dir(self, session_id: str) -> None:
        """Remove one session's frame directory, staying inside ``frames_root``.

        Never fatal: this runs from a persistence pass, and losing a snapshot
        must not stop the session. The containment check matters because a
        persisted record's id is data, not a trusted path component.
        """
        root = self.frames_root
        try:
            candidate = (root / session_id).resolve()
            candidate.relative_to(root.resolve())
        except (OSError, ValueError):
            return
        if candidate == root.resolve():
            return
        with contextlib.suppress(OSError):
            if candidate.is_dir():
                shutil.rmtree(candidate, ignore_errors=True)
            elif candidate.exists():
                candidate.unlink()

    def _persist(self) -> None:
        if self.store is None:
            return
        self._trim_sessions()
        try:
            self.store.write(
                _SESSIONS_NAMESPACE,
                {
                    "version": 2,
                    "current": self._current_id or "",
                    "sessions": [record.to_dict() for record in self._sessions.values()],
                },
            )
        except Exception:  # noqa: BLE001 - recording must never fail the session
            return
        self._last_persist = time.monotonic()
        self._persist_timelines()

    def _persist_timelines(self) -> None:
        """Write only the logs that changed since the last store write.

        One small file per session keeps a tick cheap: the alternative (every
        session's whole history in one snapshot) would rewrite hundreds of
        kilobytes to record one frame. The store drops a snapshot it cannot
        replace instead of raising, so this cannot fail the session; whether
        the newest event actually reached disk is VERIFIED when the session
        closes, never assumed here.
        """
        if self.store is None:
            return
        for session_id in list(self._timeline_dirty):
            log = self._timelines.get(session_id)
            namespace = timeline_namespace(session_id)
            if log is None or namespace is None:
                self._timeline_dirty.discard(session_id)
                continue
            log.persistence = {
                **log.persistence,
                "state": str(log.persistence.get("state") or "unverified"),
                "last_attempt_at": utc_now(),
            }
            try:
                self.store.write(namespace, log.to_dict())
            except Exception as exc:  # noqa: BLE001 - recorded, not raised
                failures = int(log.persistence.get("failures") or 0) + 1
                log.persistence = {
                    **log.persistence,
                    "state": "failed",
                    "ok": False,
                    "failures": failures,
                    "reason": f"The timeline snapshot could not be written: {type(exc).__name__}: {exc}",
                }
                continue
            self._timeline_dirty.discard(session_id)

    def _persist_throttled(self) -> None:
        """A tick's snapshot, at most once per ``persist_interval``.

        NF-01 wrote the whole session (and every retained frame's metadata) on
        every captured frame. That is unnecessary work in the observation loop;
        the UI reads memory, and a crash now loses at most a couple of seconds
        of measurements instead of none. Lifecycle moves still persist at once.
        """
        now = time.monotonic()
        if self._last_persist is None or now - self._last_persist >= self.persist_interval:
            self._persist()

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.publish is None:
            return
        try:
            await self.publish(event_type, payload)
        except Exception:  # noqa: BLE001 - a broken observer cannot fail a session
            return

    # -- read surfaces ------------------------------------------------------

    def _lock_or_new(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def active(self) -> SessionRecord | None:
        record = self._sessions.get(self._current_id) if self._current_id else None
        return record if record is not None and record.state in _ACTIVE_STATES else None

    def get(self, session_id: str) -> SessionRecord | None:
        return self._sessions.get(session_id)

    def status(self) -> NovaForgeStatus:
        active = self.active()
        return NovaForgeStatus(
            capture_supported=self.capture_supported,
            windows_available=self.capture_supported,
            active_session=active.session_id if active else None,
            session_count=len(self._sessions),
            limitations=(
                "Input is limited to the declared gameplay keys: system keys, chords and text entry are outside the policy.",
                "Capture is primary-display only; frames are cropped to the target window.",
                "Target windows must be already open; NovaForge never launches a game.",
                "Only a checkable objective (window title) can be verified; a session with none never reports success.",
                "The action budget is enforced at execution, and a denied action never consumes it.",
            ),
        )

    async def windows(self) -> dict[str, Any]:
        """Visible windows for the mission target picker.

        ``available=False`` distinguishes "the probe could not read the OS" from
        "nothing is open"; the UI shows the reason instead of an empty list that
        looks like a desktop with no windows.
        """
        if not self.capture_supported:
            return {
                "available": False,
                "capture_supported": False,
                "reason": "Screen capture is not implemented on this platform.",
                "windows": [],
            }
        try:
            lines = await self.window_lines()
        except Exception as exc:  # noqa: BLE001 - a failed probe is an unavailable answer
            return {
                "available": False,
                "capture_supported": True,
                "reason": f"Window enumeration failed: {type(exc).__name__}: {exc}",
                "windows": [],
            }
        if lines is None:
            return {
                "available": False,
                "capture_supported": True,
                "reason": "Window enumeration is unavailable on this host.",
                "windows": [],
            }
        entries: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for line in lines:
            name, _, title = str(line).partition("|")
            title = title.strip()
            process = name.strip()
            if not title or (process, title) in seen:
                continue
            # Two PROCESSES can own windows with the same title. Both are
            # listed: collapsing them to one entry would let the picker promise
            # a target that validation then refuses as ambiguous, and would hide
            # the fact that the title cannot identify one window.
            seen.add((process, title))
            entries.append({"process": process, "title": title})
        entries.sort(key=lambda item: (item["title"].lower(), item["process"].lower()))
        return {
            "available": True,
            "capture_supported": True,
            "reason": "",
            "windows": entries,
        }

    def describe(
        self,
        record: SessionRecord,
        *,
        include_stages: bool = True,
        include_telemetry: bool = True,
    ) -> dict[str, Any]:
        """The full session as the API returns it, with derived values added."""
        now = time.monotonic()
        duration = _duration_seconds(record.started_at, record.ended_at or utc_now())
        data = record.to_dict()
        latest = record.frames[-1] if record.frames else None
        data["frame"] = self._frame_payload(record, latest) if latest else None
        data["duration_seconds"] = duration
        data["active"] = record.state in _ACTIVE_STATES
        terminal = record.state in SESSION_TERMINAL_STATES
        data["can"] = {
            "pause": record.state is SessionState.RUNNING,
            "resume": record.state is SessionState.PAUSED,
            "stop": not terminal,
            "cancel": not terminal,
            "emergency_stop": not terminal,
        }
        # NF-03: a link and an honest source label, never the whole timeline (the
        # timeline endpoint pages it so polling a session stays cheap).
        log = self._timelines.get(record.session_id)
        data["timeline"] = {
            "endpoint": f"/novaforge/sessions/{record.session_id}/timeline",
            "source": "recorded" if log is not None and log.events else "derived",
            "events": len(log.events) if log is not None else len(derived_events(record)),
            "dropped": log.dropped if log is not None else 0,
            "persistence": dict(log.persistence) if log is not None else {"state": "not_stored"},
            "read_only": True,
        }
        if include_telemetry:
            telemetry = record.telemetry.describe(
                state=record.state.value,
                duration_seconds=duration,
                state_changes=record.state_change_count,
                end_reason=record.end_reason,
                now=now,
            )
            # Errors and recovery live on the RECORD, not the measurement
            # snapshot; they are reported here so the telemetry payload answers
            # the phase's "error counts, retry counts, recovery attempts"
            # requirement without a second counter that could disagree.
            telemetry["session"]["errors"] = len(record.errors)
            telemetry["recovery"] = {
                "available": False,
                "attempts": record.recovery_attempts,
                "reason": (
                    "No retry or recovery path is attached to a NovaForge session; no "
                    "attempt exists and none is claimed."
                ),
            }
            data["telemetry"] = telemetry
            data["guardian"] = guardian_state(
                record,
                frame_interval_seconds=self._interval_for(record),
                max_frames_on_disk=self.max_frames_on_disk,
                max_sessions_kept=self.max_sessions_kept,
                stop_timeout_seconds=self.stop_timeout,
            )
        if include_stages:
            data["stages"] = self._stages(record)
        return data

    def telemetry_payload(self, session_id: str) -> dict[str, Any]:
        """The detailed telemetry + Guardian view for one session."""
        record = self._require(session_id)
        data = self.describe(record, include_stages=False)
        return {
            "session_id": record.session_id,
            "telemetry": data["telemetry"],
            "guardian": data["guardian"],
        }

    def _frame_payload(self, record: SessionRecord, frame: FrameRecord) -> dict[str, Any]:
        age_ms = _age_ms(frame.captured_at)
        return {
            "seq": frame.seq,
            "url": f"/novaforge/frames/{record.session_id}?seq={frame.seq}",
            "captured_at": frame.captured_at,
            "age_ms": age_ms,
            "stale": age_ms is None or age_ms > 5000,
            "width": frame.width,
            "height": frame.height,
            "size_bytes": frame.size_bytes,
            "capture_ms": frame.capture_ms,
            "capture_mode": frame.capture_mode,
            "fresh": frame.fresh,
            "source": frame.source,
            "note": frame.note,
        }

    def _stages(self, record: SessionRecord) -> list[dict[str, Any]]:
        observed = record.observations
        observed_stage = StageRecord(
            "observed",
            available=bool(observed),
            at=observed[-1].at if observed else "",
            summary=(
                f"{len(record.frames)} frame(s); latest observation #{observed[-1].index}"
                if observed
                else "No observation has been recorded yet."
            ),
            records=tuple(item.to_dict() for item in observed[-5:]),
        )
        proposals = record.action_proposals
        proposed_stage = StageRecord(
            "proposed",
            available=bool(proposals),
            at=str(proposals[-1].get("created_at", "")) if proposals else "",
            summary=(
                f"{len(proposals)} proposal(s); latest {proposals[-1].get('proposal_id', '')}"
                if proposals
                else "No action has been proposed for this session."
            ),
            reason=(
                ""
                if proposals
                else "No decision pipeline proposes actions; a proposal is explicit and "
                "digest-bound when a caller makes one."
            ),
            records=tuple(dict(item) for item in proposals[-5:]),
        )
        executed_rows = [
            item
            for item in record.action_records
            if str(item.get("status", "")) in {"executed", "failed", "aborted"}
        ]
        executed_stage = StageRecord(
            "executed",
            available=bool(executed_rows),
            at=str(executed_rows[-1].get("at", "")) if executed_rows else "",
            summary=(
                f"{len(executed_rows)} executed action(s); {record.actions_executed} counted "
                f"against the budget"
                if executed_rows
                else "No action has been sent in this session."
            ),
            reason=(
                ""
                if executed_rows
                else (
                    "No gameplay input path is wired to this session."
                    if not record.capabilities.get("input_path", False)
                    else "No action has been executed yet."
                )
            ),
            records=tuple(dict(item) for item in executed_rows[-5:]),
        )
        verdict = record.verification
        verified_stage = StageRecord(
            "verified",
            available=bool(verdict),
            at=str(verdict.get("at", "")) if verdict else "",
            summary=(
                f"{verdict.get('outcome', '')}: {verdict.get('reason', '')}"
                if verdict
                else "No verification verdict has been produced."
            ),
            reason=(
                ""
                if verdict
                else (
                    "No objective is configured for this session, so nothing is claimed."
                    if record.objective is None
                    else "The objective is configured but has not been verified yet."
                )
            ),
            records=(dict(verdict),) if verdict else (),
        )
        return [
            observed_stage.to_dict(),
            proposed_stage.to_dict(),
            executed_stage.to_dict(),
            verified_stage.to_dict(),
        ]

    def history(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Compact history rows: enough to inspect a past session, bounded in size."""
        ordered = sorted(self._sessions.values(), key=lambda item: item.created_at, reverse=True)
        rows: list[dict[str, Any]] = []
        for record in ordered[: max(0, limit)]:
            data = self.describe(
                record, include_stages=False, include_telemetry=False
            )
            # ``to_dict`` carries the RAW persisted snapshot, including the
            # latency window. A history row is a compact summary, so the raw
            # arrays go with the derived payload: twenty rows of 120 floats is
            # a payload nobody reads and every poll pays for.
            data.pop("telemetry", None)
            data["frames"] = data["frames"][-1:]
            data["observations"] = data["observations"][-3:]
            data["history"] = data["history"][-6:]
            rows.append(data)
        return rows

    # -- mission configuration ---------------------------------------------

    def saved_mission(self) -> dict[str, Any]:
        payload = self.store.read(_MISSION_NAMESPACE) if self.store is not None else {}
        return dict(payload or {})

    def save_mission(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        mission = MissionConfig.from_dict(dict(payload))
        errors = mission.errors()
        fields = mission.to_dict()
        if self.store is not None:
            try:
                self.store.write(_MISSION_NAMESPACE, {"version": 1, **fields})
            except Exception:  # noqa: BLE001 - a store must not fail the request
                pass
        # Read it back rather than assume it: ``JsonStateStore`` drops a
        # snapshot it cannot replace instead of raising, so "saved" has to be a
        # measurement. A draft that was NOT kept must not be reported as saved.
        stored = self.saved_mission()
        persisted = self.store is not None and all(
            stored.get(key) == value for key, value in fields.items()
        )
        return {"mission": fields, "errors": errors, "valid": not errors, "persisted": persisted}

    async def validate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validate fields, the platform, and that exactly one window matches."""
        mission = MissionConfig.from_dict(dict(payload))
        errors = mission.errors()
        candidates: list[str] = []
        target: dict[str, Any] | None = None
        if not self.capture_supported:
            errors.append("Screen capture is not implemented on this platform.")
        if not errors:
            probe_error = ""
            try:
                lines = await self.window_lines()
            except Exception as exc:  # noqa: BLE001 - reported as a field-level reason
                lines = None
                probe_error = f"{type(exc).__name__}: {exc}"
            if lines is None:
                errors.append(
                    "Window enumeration is unavailable on this host"
                    + (f" ({probe_error})." if probe_error else ".")
                )
            else:
                # Same key the picker uses: one entry per (process, title), so a
                # probe that repeats a line cannot make a listed target look
                # ambiguous, while two PROCESSES sharing a title still do.
                pairs: list[tuple[str, str]] = []
                seen_pairs: set[tuple[str, str]] = set()
                for line in lines:
                    name, _, title = str(line).partition("|")
                    title = title.strip()
                    key = (name.strip(), title)
                    if not title or key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    pairs.append(key)
                titles = [title for _process, title in pairs]
                wanted = mission.target_title.strip().lower()
                candidates = [title for title in titles if wanted in title.lower()]
                if not candidates:
                    errors.append(
                        f"No open window matches {mission.target_title.strip()!r}. "
                        "Open the target window first, then refresh the window list."
                    )
                elif len(candidates) > 1:
                    errors.append(
                        f"{len(candidates)} windows match {mission.target_title.strip()!r}; "
                        "use a more specific title so the session cannot address the wrong window."
                    )
                else:
                    try:
                        geometry = await self.geometry(candidates[0])
                    except Exception as exc:  # noqa: BLE001 - a probe failure is not "no window"
                        geometry = None
                        errors.append(
                            "The matching window could not be measured: "
                            f"{type(exc).__name__}: {exc}"
                        )
                    if geometry is None:
                        if not errors:
                            errors.append(
                                "The matching window disappeared before it could be measured."
                            )
                    else:
                        target = _target_from_geometry(geometry).to_dict()
        return {
            "valid": not errors,
            "errors": errors,
            "candidates": candidates,
            "target": target,
            "capture_supported": self.capture_supported,
            "mission": mission.to_dict(),
        }

    # -- lifecycle ----------------------------------------------------------

    async def start(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validate, mint a session, and start its real observation loop."""
        mission = MissionConfig.from_dict(dict(payload))
        async with self._lock_or_new():
            running = self.active()
            if running is not None:
                raise SessionConflictError(
                    f"Session {running.session_id} is {running.state.value}; stop it before "
                    "starting another."
                )
            validation = await self.validate(payload)
            if not validation["valid"]:
                raise MissionValidationError(
                    list(validation["errors"]),
                    target_candidates=tuple(validation["candidates"]),
                )
            target = TargetWindow.from_dict(validation.get("target"))
            if target is None:
                raise MissionValidationError(["The target window could not be measured."])
            # The operator validated a WINDOW, not a title. If the caller pins the
            # identity it measured, a different window that took the same title
            # between validate and start is refused instead of silently driven.
            expected_handle = _optional_identity(payload.get("expected_handle"), "handle")
            expected_pid = _optional_identity(payload.get("expected_pid"), "process id")
            if expected_handle is not None and expected_handle != target.handle:
                raise MissionValidationError(
                    ["The target window changed since it was validated; validate again before starting."]
                )
            if expected_pid is not None and expected_pid != target.pid:
                raise MissionValidationError(
                    ["The target window changed since it was validated; validate again before starting."]
                )
            permission = self._permission_decision()
            if not permission.get("allow", True):
                raise MissionValidationError([str(permission.get("reason", "Permission refused."))])
            record = SessionRecord(mission=mission, target=target, permission=permission)
            # Per-session capability facts, persisted with the record. A restart
            # reports what THIS session could do, not what the reading process can.
            record.capabilities = {
                "input_path": self.action_runner is not None,
                "action_budget_enforced": self.action_runner is not None,
                "objective_verifier": bool(self.objectives.verifiers),
            }
            record.transition(SessionState.STARTING)
            record.started_at = record.history[-1]["at"]
            interval = self._interval_for(record)
            record.telemetry = TelemetrySnapshot(
                frame_interval_ms=max(20.0, interval * 1000.0),
                gap_threshold_ms=gap_threshold_for(interval),
            )
            self._started_monotonic = time.monotonic()
            record.telemetry.begin(self._started_monotonic)
            self._sessions[record.session_id] = record
            self._current_id = record.session_id
            self._event(
                record,
                "session.created",
                payload={
                    "mission": mission.name,
                    "objective": mission.objective,
                    "target_title": mission.target_title,
                    "time_limit_seconds": mission.time_limit_seconds,
                    "max_actions": mission.max_actions,
                },
                evidence=[session_ref(record.session_id)],
            )
            self._event(
                record,
                "target.validated",
                source="target",
                payload=target.to_dict(),
                evidence=[session_ref(record.session_id)],
            )
            self._persist()
            self._stop_event = asyncio.Event()
            self._pause_event = asyncio.Event()
            self._running_ack = asyncio.Event()
            if self.action_runner is not None:
                # A stop request and the press/release totals belong to the
                # session that produced them. A new session starts armed with
                # zeroed totals, or one earlier cancel would abort every later
                # action and one earlier tap would be counted in this session's
                # input cleanup.
                self.action_runner.begin_session()
            self._task = asyncio.create_task(self._loop(record))
        await self._emit(
            "novaforge.session.changed",
            {"session_id": record.session_id, "state": record.state.value},
        )
        try:
            await asyncio.wait_for(self._running_ack.wait(), timeout=self.pause_timeout)
        except TimeoutError as exc:
            # The loop did not confirm it started. Close it through the same
            # stop path so a failed start cannot leave a task or a held input.
            await self.stop(record.session_id, reason="start_timeout")
            raise SessionTransitionError(
                "The session was created but its capture loop did not start."
            ) from exc
        return self.describe(record)

    async def pause(self, session_id: str, *, reason: str = "user") -> dict[str, Any]:
        """Request a pause and wait until the loop has actually stopped capturing."""
        async with self._lock_or_new():
            record = self._require(session_id)
            if record.state is not SessionState.RUNNING:
                raise SessionTransitionError(
                    f"Session is {record.state.value}; only a RUNNING session can pause."
                )
            record.pause_reason = reason
            record.transition(SessionState.PAUSING, reason=reason)
            self._event(record, "session.pausing", payload={"reason": reason})
            assert self._pause_event is not None
            self._pause_event.set()
            self._persist()
        await self._emit(
            "novaforge.session.changed",
            {"session_id": record.session_id, "state": record.state.value, "reason": reason},
        )
        deadline = time.monotonic() + self.pause_timeout
        while time.monotonic() < deadline:
            # The loop mutates the record from another task, so mypy's narrowing
            # from the check above is stale here; compare the values instead.
            if record.state.value == SessionState.PAUSED.value:
                return self.describe(record)
            await asyncio.sleep(0.02)
        raise SessionTransitionError(
            f"Session {session_id} did not acknowledge the pause in {self.pause_timeout:.1f}s; "
            f"it is still {record.state.value}."
        )

    async def resume(self, session_id: str) -> dict[str, Any]:
        """Re-validate the target, then let the paused loop continue."""
        async with self._lock_or_new():
            record = self._require(session_id)
            if record.state is not SessionState.PAUSED:
                raise SessionTransitionError(
                    f"Session is {record.state.value}; only a PAUSED session can resume."
                )
            if record.target is None:
                raise MissionValidationError(["The session has no target window."])
            try:
                geometry = await self.geometry(record.target.title)
            except Exception as exc:  # noqa: BLE001 - a failed probe refuses the resume
                raise MissionValidationError(
                    [
                        "The target window could not be read: "
                        f"{type(exc).__name__}: {exc}. The session stays paused."
                    ]
                ) from exc
            if geometry is None:
                raise MissionValidationError(
                    ["The target window is gone; reopen it before resuming the session."]
                )
            resolved = _target_from_geometry(geometry)
            if resolved.handle != record.target.handle or resolved.pid != record.target.pid:
                raise MissionValidationError(
                    ["The window behind this title is a different window now; refusing to resume."]
                )
            # A moved/resized window is fine: capture follows its current bounds.
            # This check is a fresh confirmation, so the input boundary may allow
            # the resumed run without waiting for the next tick.
            record.target = resolved
            record.target_validated = True
            record.pause_reason = ""
            if self.action_runner is not None:
                # A resumed session is running again, so a stop that interrupted
                # the action before the pause must not keep aborting it.
                self.action_runner.reset_abort()
            record.transition(SessionState.RUNNING, reason="resumed")
            record.telemetry.note_resume(time.monotonic())
            self._event(
                record,
                "target.validated",
                source="target",
                payload={"title": resolved.title, "handle": resolved.handle, "pid": resolved.pid,
                         "reason": "Resume re-read the window and matched the pinned identity."},
                evidence=[session_ref(record.session_id)],
            )
            self._event(record, "session.resumed", payload={"reason": "resumed"})
            assert self._pause_event is not None
            self._pause_event.clear()
            self._persist()
        await self._emit(
            "novaforge.session.changed",
            {"session_id": record.session_id, "state": record.state.value},
        )
        return self.describe(record)

    async def stop(self, session_id: str, *, reason: str = "user") -> dict[str, Any]:
        """Cancel the loop, release input through the verified path, finalize."""
        started = time.monotonic()
        async with self._lock_or_new():
            record = self._require(session_id)
            if record.state in SESSION_TERMINAL_STATES:
                return self.describe(record)
            intervention = self._intervene(record, "stop", reason, "requested", severity="warning")
            with contextlib.suppress(SessionTransitionError):
                record.transition(SessionState.STOPPING, reason=reason)
            self._event(record, "session.stopping", payload={"reason": reason})
            record.end_reason = record.end_reason or reason
            if self._stop_event is not None:
                self._stop_event.set()
            if self._pause_event is not None:
                self._pause_event.clear()  # let the paused loop observe the stop
            self._persist()
            task = self._task
        await self._emit(
            "novaforge.session.changed",
            {"session_id": record.session_id, "state": record.state.value, "reason": reason},
        )
        if task is not None and task is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self.stop_timeout)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                self._error(record, "The capture loop did not stop in time and was cancelled.", where="stop")
                await self._finalize(record, SessionState.STOPPED, reason="stop_timeout")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - finalize still records it
                self._error(record, f"The capture loop failed while stopping: {exc}", where="stop")
                await self._finalize(record, SessionState.STOPPED, reason="stop_after_error")
        else:
            await self._finalize(record, SessionState.STOPPED, reason=reason)
        intervention["outcome"] = "stopped" if record.state in SESSION_TERMINAL_STATES else "incomplete"
        intervention["detail"] = f"{int((time.monotonic() - started) * 1000)} ms"
        self._persist()
        return self.describe(record)

    async def cancel(self, session_id: str) -> dict[str, Any]:
        """Abort the loop now (not gracefully) and release input; idempotent."""
        return await self._abort(session_id, kind="cancel", reason="cancelled")

    async def emergency_stop(self, session_id: str) -> dict[str, Any]:
        """Guardian emergency stop: abort, release, record, confirm; idempotent."""
        return await self._abort(session_id, kind="emergency_stop", reason="emergency_stop")

    async def _abort(self, session_id: str, *, kind: str, reason: str) -> dict[str, Any]:
        """One bounded abort path for cancel and emergency stop.

        The task is cancelled rather than politely asked to stop, because the
        whole point of these controls is that the caller no longer trusts the
        loop to finish. Input cleanup is still the single finalize path; a
        repeated request finds a terminal record and does no second release.
        """
        started = time.monotonic()
        # Interrupting in-flight input is the FIRST thing an abort does, and it
        # must not queue behind the lock a running action holds: ``execute_action``
        # keeps that lock for the whole injection (up to a policy-bounded hold),
        # so setting the flag after acquiring it would let a held key outlive the
        # operator's stop. The flag is a one-shot on the runner, safe to set
        # without the lock, and ``reset_abort`` clears it for the next session.
        if self.action_runner is not None:
            self.action_runner.abort.stop()
        async with self._lock_or_new():
            record = self._require(session_id)
            if record.state in SESSION_TERMINAL_STATES:
                payload = self.describe(record)
                payload["already_stopped"] = True
                payload["stop_latency_ms"] = None
                return payload
            intervention = self._intervene(
                record,
                kind,
                reason,
                "requested",
                detail="Requested by the Guardian control path.",
                severity="warning",
            )
            self._event(record, "session.stopping", payload={"reason": reason, "kind": kind})
            # Name the ending: a Guardian abort must be readable in the record as
            # one, not as the finalize default ("stopped") that a later reader
            # could not tell apart from a polite stop.
            record.end_reason = record.end_reason or reason
            if self._stop_event is not None:
                self._stop_event.set()
            if self._pause_event is not None:
                self._pause_event.clear()  # a paused loop must observe the stop
            self._persist()
            task = self._task
        timed_out = False
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self.stop_timeout)
            except TimeoutError:
                # The cancellation was delivered but the loop did not finish
                # inside the bound. The record is still closed below; what must
                # NOT happen is calling that a clean stop.
                timed_out = True
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - cleanup below still runs
                pass
        # The loop's own finally normally finalizes; this covers a task that
        # never started or a timeout, and is a no-op once the record is terminal.
        for _ in range(2):
            await self._finalize(record, SessionState.STOPPED, reason=reason)
            if record.state in SESSION_TERMINAL_STATES:
                break
            await asyncio.sleep(0.02)
        elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
        if timed_out:
            self._error(
                record,
                f"The capture loop did not confirm the {kind.replace('_', ' ')} within "
                f"{self.stop_timeout:.1f}s; it was cancelled and the session was closed. "
                "The confirmed bound was EXCEEDED.",
                where=kind,
            )
        intervention["outcome"] = (
            "stopped" if record.state in SESSION_TERMINAL_STATES and not timed_out else "timeout"
        )
        intervention["detail"] = (
            f"{elapsed_ms} ms; cleanup {record.input_cleanup.get('state', 'recorded')}"
            + (
                f"; loop confirmation exceeded {self.stop_timeout:.1f}s"
                if timed_out
                else ""
            )
        )
        self._persist()
        await self._emit(
            "novaforge.guardian.intervention",
            {
                "session_id": record.session_id,
                "kind": kind,
                "reason": reason,
                "outcome": intervention["outcome"],
                "latency_ms": elapsed_ms,
            },
        )
        payload = self.describe(record)
        payload["already_stopped"] = False
        payload["stop_latency_ms"] = elapsed_ms
        return payload

    # -- gameplay actions, objectives and verification ----------------------

    def capabilities(self) -> dict[str, Any]:
        """What THIS runtime can actually do, per capability, with reasons.

        Availability is a property of the machine and the wiring, not of the
        request: a manager built without an injector reports no action path, and
        one built without a probe reports no objective verification, rather than
        claiming either because the class exists.
        """
        input_ready = self.action_runner is not None
        verifier_ready = bool(self.objectives.verifiers)
        return {
            "window_discovery": {
                "available": bool(self.capture_supported),
                "reason": "" if self.capture_supported else "Window probing is unavailable on this host.",
            },
            "capture": {
                "available": bool(self.capture_supported),
                "reason": "" if self.capture_supported else "Screen capture is unavailable on this host.",
            },
            "action_execution": {
                "available": input_ready,
                "reason": (
                    "Keys, holds, clicks and moves are injected through the DPI-aware "
                    "capture worker, behind the InputGuard."
                    if input_ready
                    else "No gameplay injector is wired; no input can be sent."
                ),
            },
            "guardian_enforcement": {
                "available": True,
                "reason": (
                    "Every action is refused unless the session is RUNNING with a target "
                    "identity confirmed for this run and budget remaining."
                ),
                "time_limit_enforced": True,
                "action_budget_enforced": input_ready,
            },
            "objective_verification": {
                "available": verifier_ready,
                "kinds": self.objectives.availability(),
                "reason": (
                    "A verdict is read from the target window's title, independently of "
                    "the action record."
                    if verifier_ready
                    else "No objective probe is wired; every objective is reported UNSUPPORTED."
                ),
            },
            "decision_pipeline": {
                "available": False,
                "reason": (
                    "No planner chooses actions for a session; a caller proposes them "
                    "explicitly. Perception does not silently become a policy."
                ),
            },
            "game_launch": {
                "available": False,
                "reason": "NovaForge never launches or installs a game.",
            },
            "persistence": {
                "available": self.store is not None,
                "reason": "" if self.store is not None else "No state store is wired.",
            },
            "action_policy": self.action_policy.to_dict(),
        }

    def set_objective(self, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Attach a checkable objective to a live session.

        A definition this build cannot check is still accepted as a DEFINITION: it
        is recorded and reported ``unsupported``, so the operator sees the truth
        rather than a missing field that looks like success pending.
        """
        record = self._require(session_id)
        if record.state in SESSION_TERMINAL_STATES:
            raise SessionTransitionError(
                f"Session is {record.state.value}; an objective cannot be added after it ended."
            )
        try:
            definition = ObjectiveDefinition.from_payload(dict(payload))
        except ValueError as exc:
            raise MissionValidationError([str(exc)]) from exc
        problems = definition.errors()
        if problems:
            raise MissionValidationError(problems)
        record.objective = definition
        # A new objective invalidates the old verdict: it answered a different
        # question, and keeping it would attribute a past success to this one.
        record.verification = {}
        supported = self.objectives.verifier_for(definition) is not None
        self._event(
            record,
            "objective.configured",
            source="objective",
            payload={**definition.to_dict(), "supported": supported, "digest": definition.digest()},
            evidence=[session_ref(record.session_id)],
        )
        self._persist()
        return {
            "session_id": record.session_id,
            "objective": definition.to_dict(),
            "supported": supported,
            "reason": (
                "A verifier will read the target window's title for this objective."
                if supported
                else "No verifier implements this objective kind; verification will report UNSUPPORTED."
            ),
            "kinds": self.objectives.availability(),
        }

    def _denied(
        self, record: SessionRecord, action: GameplayAction, reason: str, *, state: str = "denied"
    ) -> dict[str, Any]:
        """Record a refusal exactly once. A refusal is not a failure of the game."""
        record.actions_attempted += 1
        outcome = ActionOutcome(
            status=ActionStatus.DENIED,
            ok=False,
            reason=reason,
            action=action.to_dict(),
        ).to_dict()
        record.record_action(outcome)
        self._event(
            record,
            "action.denied",
            source="guardian",
            severity="warning",
            payload={"action": action.to_dict(), "reason": reason, "guardian_state": state},
            evidence=[session_ref(record.session_id)],
        )
        self._persist()
        return outcome

    def plan_action(self, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and digest-bind an action. NOTHING is injected here."""
        record = self._require(session_id)
        try:
            action = GameplayAction.from_payload(dict(payload))
        except ValueError as exc:
            raise MissionValidationError([str(exc)]) from exc
        if record.state in SESSION_TERMINAL_STATES:
            raise SessionTransitionError(
                f"Session is {record.state.value}; no action can be planned for it."
            )
        decision = input_safety_decision(record)
        if not decision.get("allow"):
            return {
                "status": ActionStatus.DENIED.value,
                "outcome": self._denied(record, action, str(decision.get("reason", "")), state=str(decision.get("state", "denied"))),
                "remaining_actions": decision.get("remaining_actions"),
            }
        problems = action.validate(self.action_policy)
        if problems:
            return {
                "status": ActionStatus.DENIED.value,
                "outcome": self._denied(record, action, " ".join(problems), state="policy"),
                "remaining_actions": decision.get("remaining_actions"),
            }
        target = record.target
        assert target is not None
        proposal = ActionProposal(
            proposal_id=f"act_{int(time.time() * 1000):x}",
            session_id=record.session_id,
            action=action,
            digest=action_digest(record.session_id, action, target),
            created_at=utc_now(),
            target=target.to_dict(),
            policy=self.action_policy.to_dict(),
        )
        record.record_proposal(proposal.to_dict())
        self._event(
            record,
            "action.proposed",
            source="action",
            payload={
                "proposal_id": proposal.proposal_id,
                "digest": proposal.digest,
                "action": action.to_dict(),
                "target": proposal.target,
            },
            evidence=[session_ref(record.session_id)],
        )
        self._persist()
        return {
            "status": ActionStatus.PROPOSED.value,
            "proposal": proposal.to_dict(),
            "remaining_actions": decision.get("remaining_actions"),
            "executed": False,
            "note": "A proposal sends nothing. Execution needs this proposal's id and digest.",
        }

    async def _probe_target(self, record: SessionRecord) -> Any | None:
        """Read the pinned window's CURRENT geometry, by handle when possible."""
        target = record.target
        if target is None:
            return None
        if self.objective_probe is not None:
            try:
                return await self.objective_probe(target.handle)
            except Exception:  # noqa: BLE001 - a probe failure is a refusal, not a crash
                return None
        try:
            return await self.geometry(target.title)
        except Exception:  # noqa: BLE001
            return None

    async def execute_action(self, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Execute ONE digest-bound proposal, revalidating everything first.

        Approval binding: the caller must echo the proposal id AND its digest.
        The digest covers the action and the target identity, so a proposal made
        for window A cannot be replayed against the same title in window B.
        """
        async with self._lock_or_new():
            record = self._require(session_id)
            runner = self.action_runner
            proposal_id = str(payload.get("proposal_id", "") or "")
            digest = str(payload.get("digest", "") or "")
            stored = record.find_proposal(proposal_id) if proposal_id else None
            action = GameplayAction.from_dict((stored or {}).get("action"))
            if stored is None or action is None:
                raise MissionValidationError(
                    ["Unknown action proposal; plan the action again before executing it."]
                )
            target = record.target
            if target is None:
                raise MissionValidationError(["The session has no validated target window."])
            expected = action_digest(record.session_id, action, target)
            if not digest or digest != stored.get("digest") or digest != expected:
                return {
                    "status": ActionStatus.DENIED.value,
                    "outcome": self._denied(
                        record,
                        action,
                        "The approval does not match this proposal and target; plan it again.",
                        state="approval_mismatch",
                    ),
                }
            if runner is None:
                return {
                    "status": ActionStatus.DENIED.value,
                    "outcome": self._denied(record, action, "No gameplay input path is wired.", state="unsupported"),
                }
            decision = input_safety_decision(record)
            if not decision.get("allow"):
                return {
                    "status": ActionStatus.DENIED.value,
                    "outcome": self._denied(
                        record, action, str(decision.get("reason", "")), state=str(decision.get("state", "denied"))
                    ),
                }
            problems = action.validate(self.action_policy)
            if problems:
                return {
                    "status": ActionStatus.DENIED.value,
                    "outcome": self._denied(record, action, " ".join(problems), state="policy"),
                }
            geometry = await self._probe_target(record)
            if geometry is None:
                # The target went away between plan and execute. Pause through
                # the same path a tick uses, so the session is held rather than
                # driven, and refuse the action.
                await self._pause_for(record, "target_lost")
                return {
                    "status": ActionStatus.DENIED.value,
                    "outcome": self._denied(
                        record,
                        action,
                        "The target window could not be read immediately before execution; the action was refused.",
                        state="target_lost",
                    ),
                }
            observed_handle = int(getattr(geometry, "handle", 0) or 0)
            observed_pid = int(getattr(geometry, "pid", 0) or 0)
            if observed_handle != target.handle or (observed_pid and observed_pid != target.pid):
                await self._pause_for(record, "target_changed")
                return {
                    "status": ActionStatus.DENIED.value,
                    "outcome": self._denied(
                        record,
                        action,
                        (
                            f"The pinned window changed (handle {target.handle}/{observed_handle}, "
                            f"pid {target.pid}/{observed_pid}); the action was refused."
                        ),
                        state="target_changed",
                    ),
                }
            if self.action_policy.require_foreground and not bool(getattr(geometry, "foreground", False)):
                return {
                    "status": ActionStatus.DENIED.value,
                    "outcome": self._denied(
                        record,
                        action,
                        "The target is not the foreground window; input would land elsewhere.",
                        state="not_foreground",
                    ),
                }

            record.actions_attempted += 1
            outcome = await runner.run(action, geometry, self.action_policy)
            payload_out = outcome.to_dict()
            payload_out["proposal_id"] = proposal_id
            payload_out["digest"] = digest
            record.record_action(payload_out)
            if outcome.status is ActionStatus.EXECUTED:
                record.actions_executed += 1
            self._event(
                record,
                "action.executed" if outcome.status is ActionStatus.EXECUTED else f"action.{outcome.status.value}",
                source="action",
                severity="info" if outcome.status is ActionStatus.EXECUTED else "warning",
                payload=payload_out,
                evidence=[session_ref(record.session_id)],
            )
            if outcome.held_after or not outcome.ok:
                self._intervene(
                    record,
                    "action",
                    outcome.status.value,
                    "held",
                    detail=outcome.error or outcome.reason,
                    severity="error" if outcome.held_after else "warning",
                )
            self._persist()
            result: dict[str, Any] = {
                "status": outcome.status.value,
                "outcome": payload_out,
                "remaining_actions": (
                    None
                    if record.mission.max_actions is None
                    else max(0, record.mission.max_actions - record.actions_executed)
                ),
                "verification": None,
            }
            if outcome.status is ActionStatus.EXECUTED and record.objective is not None:
                result["verification"] = await self.verify_objective(
                    record.session_id, reason="after_action", action_id=action.action_id
                )
            return result

    async def verify_objective(
        self, session_id: str, *, reason: str = "manual", action_id: str = ""
    ) -> dict[str, Any]:
        """Ask the independent verifier what the objective's state is, now."""
        record = self._require(session_id)
        if record.objective is None:
            return {
                "outcome": ObjectiveOutcome.UNSUPPORTED.value,
                "verified": None,
                "achieved": None,
                "decided": False,
                "method": "none",
                "reason": (
                    "No checkable objective is configured for this session, so there is "
                    "nothing to verify."
                ),
                "at": utc_stamp(),
                "definition": None,
                "evidence": [],
                "confidence": None,
                "session_id": record.session_id,
                "action_id": action_id,
            }
        verifier = self.objectives.verifier_for(record.objective)
        if verifier is None or record.target is None:
            verification = self.objectives.unavailable(record.objective)
        else:
            verification = await verifier.verify(
                record.objective,
                handle=record.target.handle,
                pid=record.target.pid,
                session_id=record.session_id,
                action_id=action_id,
            )
        data = verification.to_dict()
        record.set_verification(data)
        self._event(
            record,
            "objective.verified",
            source="objective",
            severity=(
                "info"
                if verification.outcome is ObjectiveOutcome.VERIFIED_SUCCESS
                else "warning"
                if verification.outcome.decided
                else "error"
            ),
            payload={
                "outcome": data["outcome"],
                "achieved": data["achieved"],
                "reason": data["reason"],
                "reason_for_run": reason,
                "definition": data["definition"],
            },
            evidence=[*(item.reference() for item in verification.evidence), session_ref(record.session_id)],
        )
        self._persist()
        if (
            verification.outcome is ObjectiveOutcome.VERIFIED_SUCCESS
            and record.state is SessionState.RUNNING
        ):
            # A verified objective is the ONE way a session reaches COMPLETED.
            await self._finalize(record, SessionState.COMPLETED, reason="objective_verified")
        return data

    def verification_payload(self, session_id: str) -> dict[str, Any]:
        """The verdict plus what this session could check, for the UI and the API."""
        record = self._require(session_id)
        return {
            "session_id": record.session_id,
            "objective": record.objective.to_dict() if record.objective else None,
            "verification": record.to_dict()["verification"],
            "supported": bool(record.capabilities.get("objective_verifier", False)),
            "kinds": self.objectives.availability(),
            "actions": {
                "attempted": record.actions_attempted,
                "executed": record.actions_executed,
                "remaining": (
                    None
                    if record.mission.max_actions is None
                    else max(0, record.mission.max_actions - record.actions_executed)
                ),
                "records": list(record.action_records[-10:]),
            },
        }

    async def shutdown(self) -> None:
        """Close an active session on application shutdown (best effort).

        The stop path is what records the terminal events, releases input and
        verifies persistence. If it raises part-way, the evidence recorded so
        far must still reach the store: shutdown flushes the dirty logs in a
        ``finally`` so a failed cleanup cannot be how the end of a session's
        evidence is lost.
        """
        active = self.active()
        if active is None:
            self._task = None
            return
        try:
            await self.stop(active.session_id, reason="application_shutdown")
        except Exception:  # noqa: BLE001 - shutdown must keep going
            pass
        finally:
            self._task = None
            with contextlib.suppress(Exception):
                self._persist()

    # -- internals ----------------------------------------------------------

    # -- NF-03: evidence timeline --------------------------------------------

    def _timeline_for(self, record: SessionRecord) -> TimelineLog:
        """The session's log, created on first use with its monotonic origin."""
        log = self._timelines.get(record.session_id)
        if log is None:
            start = (
                self._started_monotonic
                if record.session_id == self._current_id
                else None
            )
            log = TimelineLog(session_id=record.session_id, started_monotonic=start)
            self._timelines[record.session_id] = log
        return log

    def _event(
        self,
        record: SessionRecord,
        event_type: str,
        *,
        source: str = "session",
        payload: Mapping[str, Any] | None = None,
        evidence: list[dict[str, Any]] | None = None,
        severity: str | None = None,
        at: str | None = None,
        elapsed_ms: float | None | object = ...,
    ) -> TimelineEvent | None:
        """Append one event; a timeline problem never fails the session."""
        try:
            log = self._timeline_for(record)
            event = log.append(
                event_type,
                source=source,
                payload=dict(payload or {}),
                evidence=evidence,
                severity=severity,
                at=at,
                elapsed_ms=elapsed_ms,
            )
            self._timeline_dirty.add(record.session_id)
            return event
        except Exception:  # noqa: BLE001 - evidence recording is auxiliary
            return None

    def _error(self, record: SessionRecord, message: str, *, where: str = "") -> None:
        """Record an error on the record AND in the timeline (one call site)."""
        record.record_error(message, where=where)
        self._event(
            record,
            "error.recorded",
            source="session",
            severity="error",
            payload={"message": message, "where": where},
        )

    def _intervene(
        self,
        record: SessionRecord,
        kind: str,
        reason: str,
        outcome: str,
        *,
        detail: str = "",
        severity: str | None = None,
    ) -> dict[str, Any]:
        """Record a Guardian intervention and its timeline event."""
        entry = record.add_intervention(kind, reason, outcome, detail=detail)
        self._event(
            record,
            "guardian.intervention",
            source="guardian",
            payload=dict(entry),
            severity=severity,
        )
        return entry

    def _load_timeline(self, session_id: str) -> TimelineLog | None:
        if self.store is None:
            return None
        namespace = timeline_namespace(session_id)
        if namespace is None:
            return None
        stored = self.store.read(namespace)
        if not stored or not stored.get("events"):
            return None
        return TimelineLog.from_dict(stored, session_id=session_id)

    def _drop_timeline(self, session_id: str) -> None:
        """Eviction removes the log AND its stored copy, so nothing lingers."""
        self._timelines.pop(session_id, None)
        self._timeline_dirty.discard(session_id)
        if self.store is None:
            return
        namespace = timeline_namespace(session_id)
        if namespace is None:
            return
        try:
            self.store.write(
                namespace,
                {
                    "version": TIMELINE_VERSION,
                    "session_id": session_id,
                    "cleared": True,
                    "events": [],
                },
            )
        except Exception:  # noqa: BLE001 - eviction cleanup is auxiliary
            return

    def _verify_timeline(self, record: SessionRecord) -> None:
        """Read the snapshot back: "persisted" is a measurement, not a wish.

        ``JsonStateStore`` drops a snapshot it cannot replace instead of
        raising, so a successful ``write`` call is not evidence that the newest
        event is on disk. When the session closes, the stored copy is read back
        and compared with the last sequence number this process recorded. A
        mismatch is recorded as an error and a failure event — visible in the
        timeline and in the session's errors — rather than silently losing the
        end of the evidence.
        """
        log = self._timelines.get(record.session_id)
        if log is None:
            return
        namespace = timeline_namespace(record.session_id)
        if self.store is None or namespace is None:
            log.persistence = {
                **log.persistence,
                "state": "not_persisted",
                "ok": False,
                "checked_at": utc_now(),
                "reason": (
                    "No state store is available for this session id, so the timeline "
                    "exists only in memory."
                ),
            }
            return
        stored = self.store.read(namespace)
        stored_events = [
            item for item in (stored.get("events") or []) if isinstance(item, dict)
        ]
        stored_last = max(
            (int(item.get("seq") or 0) for item in stored_events if str(item.get("seq") or "").isdigit()),
            default=0,
        )
        expected = log.events[-1].seq if log.events else 0
        ok = (
            str(stored.get("session_id") or "") == record.session_id
            and stored_last >= expected
            and not stored.get("cleared")
        )
        if ok:
            log.persistence = {
                **log.persistence,
                "state": "verified",
                "ok": True,
                "checked_at": utc_now(),
                "expected_last_seq": expected,
                "stored_last_seq": stored_last,
                "reason": "",
            }
            # The verification result is itself evidence: write it once so a
            # later read reports what was actually checked, not "unverified".
            self._timeline_dirty.add(record.session_id)
            self._persist_timelines()
            return
        failures = int(log.persistence.get("failures") or 0) + 1
        reason = (
            "The timeline snapshot did not read back with its newest event "
            f"(expected seq {expected}, stored {stored_last}). Evidence recorded after "
            "the last successful store write may be missing."
        )
        log.persistence = {
            **log.persistence,
            "state": "failed",
            "ok": False,
            "checked_at": utc_now(),
            "failures": failures,
            "expected_last_seq": expected,
            "stored_last_seq": stored_last,
            "reason": reason,
        }
        self._error(record, reason, where="timeline_persist")
        self._event(
            record,
            "timeline.persistence_failed",
            source="store",
            severity="error",
            payload={
                "expected_last_seq": expected,
                "stored_last_seq": stored_last,
                "failures": failures,
                "reason": reason,
            },
        )
        # One last best-effort write so the failure event itself has a chance to
        # reach disk, then mark it as attempted. It is deliberately NOT verified
        # again: the reported result is the failure that was measured.
        self._persist_timelines()

    def _telemetry_summary(self, record: SessionRecord) -> dict[str, Any]:
        """The bounded telemetry excerpt stored on the final timeline event."""
        telemetry = record.telemetry.describe(
            state=record.state.value,
            duration_seconds=_duration_seconds(record.started_at, record.ended_at or utc_now()),
            state_changes=record.state_change_count,
            end_reason=record.end_reason,
        )
        capture = dict(telemetry.get("capture") or {})
        latency = dict(capture.get("latency_ms") or {})
        session = dict(telemetry.get("session") or {})
        return {
            "frames": capture.get("frames"),
            "stale_frames": capture.get("stale_frames"),
            "capture_failures": capture.get("capture_failures"),
            "frame_gaps": capture.get("frame_gaps"),
            "max_gap_ms": capture.get("max_gap_ms"),
            "latency_median_ms": latency.get("median_ms"),
            "latency_p95_ms": latency.get("p95_ms"),
            "p95_available": latency.get("p95_available"),
            "active_seconds": session.get("active_seconds"),
            "paused_seconds": session.get("paused_seconds"),
            "end_reason": session.get("end_reason"),
        }

    def _events_for(self, record: SessionRecord) -> list[TimelineEvent]:
        """Recorded events when a log exists, otherwise a derived timeline."""
        log = self._timelines.get(record.session_id)
        if log is not None and log.events:
            return sorted(log.events, key=lambda event: event.seq)
        return derived_events(record)

    def _resolve_ref(self, record: SessionRecord, ref: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve one bounded evidence reference, or explain why it cannot."""
        resolved = dict(ref)
        kind = str(ref.get("kind") or "")
        if kind == "frame":
            try:
                seq = int(str(ref.get("seq")))
            except (TypeError, ValueError):
                return {**resolved, "available": False, "reason": "The frame reference has no valid seq."}
            path = self.frame_path(record.session_id, seq=seq)
            frame = next((item for item in record.frames if item.seq == seq), None)
            if path is None:
                return {
                    **resolved,
                    "available": False,
                    "reason": (
                        "Frame " + str(seq) + " is no longer retained. Frame retention keeps "
                        "the newest frames on disk; the event remains inspectable without its image."
                    ),
                }
            return {
                **resolved,
                "available": True,
                "url": f"/novaforge/frames/{record.session_id}?seq={seq}",
                "captured_at": frame.captured_at if frame else "",
                "width": frame.width if frame else None,
                "height": frame.height if frame else None,
            }
        if kind == "observation":
            # The index alone is NOT an identity: the record trims to its newest
            # observations and new entries are numbered from the list length, so
            # several retained observations can share an index. Resolve by the
            # recorded timestamp; an index-only reference is ambiguous, and
            # saying so is the only alternative to substituting another record.
            try:
                index = int(str(ref.get("index")))
            except (TypeError, ValueError):
                index = -1
            at = str(ref.get("at") or "")
            matches = [
                item for item in record.observations
                if (not at or item.at == at) and (index < 0 or item.index == index)
            ]
            if not matches:
                return {
                    **resolved,
                    "available": False,
                    "reason": (
                        "Observation " + str(index) + " is no longer retained "
                        "(the record keeps its newest observations)."
                    ),
                }
            if len(matches) > 1:
                return {
                    **resolved,
                    "available": False,
                    "reason": (
                        "The observation reference is ambiguous: " + str(len(matches)) +
                        " retained observations share that index and no timestamp "
                        "identifies which one was referenced."
                    ),
                }
            observation = matches[0]
            return {
                **resolved,
                "available": True,
                "summary": observation.summary,
                "evidence": observation.evidence,
                "at": observation.at,
                "frame_seq": observation.frame_seq,
            }
        if kind == "intervention":
            # Same rule as observations: the bounded list reindexes, so the
            # entry is matched by its recorded timestamp.
            at = str(ref.get("at") or "")
            if not at:
                return {
                    **resolved,
                    "available": False,
                    "reason": (
                        "The intervention reference has no timestamp, so it cannot be "
                        "matched after the bounded list reindexed."
                    ),
                }
            entries = [
                entry for entry in record.interventions
                if isinstance(entry, dict) and str(entry.get("at") or "") == at
            ]
            if len(entries) == 1:
                return {**resolved, "available": True, "entry": dict(entries[0])}
            if not entries:
                return {
                    **resolved,
                    "available": False,
                    "reason": "The intervention is no longer in the record's bounded list.",
                }
            # Two recorded interventions share the timestamp. Saying "no
            # timestamp" here was wrong: the reference HAS one, it just does not
            # identify a single entry, and substituting one of them would show a
            # different Guardian decision than the event named.
            return {
                **resolved,
                "available": False,
                "reason": (
                    "The intervention reference is ambiguous: " + str(len(entries)) +
                    " recorded interventions share that timestamp."
                ),
            }
        if kind == "telemetry":
            return {
                **resolved,
                "available": True,
                "url": f"/novaforge/sessions/{record.session_id}/telemetry",
            }
        if kind == "session":
            return {
                **resolved,
                "available": True,
                "url": f"/novaforge/sessions/{record.session_id}",
            }
        if kind == "event":
            return {**resolved, "available": True}
        return {
            **resolved,
            "available": False,
            "reason": "NF-03 has no resolver for this evidence kind.",
        }

    def _resolve_event(self, record: SessionRecord, event: TimelineEvent) -> dict[str, Any]:
        resolved = event.to_dict()
        resolved["evidence"] = [self._resolve_ref(record, ref) for ref in event.evidence]
        flags = [bool(item.get("available")) for item in resolved["evidence"]]
        resolved["evidence_available"] = None if not flags else all(flags)
        resolved["summary"] = summary_text(event)
        return resolved

    def timeline_payload(
        self,
        session_id: str,
        *,
        limit: int = 100,
        after_seq: int = 0,
        types: list[str] | None = None,
        categories: list[str] | None = None,
        sources: list[str] | None = None,
        severities: list[str] | None = None,
    ) -> dict[str, Any]:
        """One page of a session's timeline plus its classification.

        Read-only: nothing here can move a session or send input. The full event
        list is used for classification (so a filtered page cannot change the
        diagnosis), while only the requested page is resolved and returned.
        """
        record = self._require(session_id)
        query = validate_query(
            types=types,
            categories=categories,
            sources=sources,
            severities=severities,
            limit=limit,
            after_seq=after_seq,
        )
        log = self._timelines.get(record.session_id)
        recorded = log is not None and bool(log.events)
        events = self._events_for(record)
        filtered = filter_events(
            events, **{key: query[key] for key in ("types", "categories", "sources", "severities")}
        )
        page, page_info = paginate(filtered, after_seq=query["after_seq"], limit=query["limit"])
        policy = dict(log.persistence) if log is not None else {
            "state": "not_stored",
            "ok": None,
            "reason": (
                "This session has no NF-03 event log; its timeline is reconstructed "
                "from the persisted session record."
            ),
        }
        # Limitations that APPLY to this timeline. A recorded session's order is
        # its sequence numbers, so the derived-order caveat is omitted rather
        # than shown next to a timeline it does not describe.
        limitations = [
            "An event's elapsed_ms is null when the monotonic origin was lost "
            "(for example after an application restart).",
            (
                "This session has no action or verification stages, so it emits none."
                if not record.capabilities.get("input_path", False) and record.objective is None
                else
                "Action and verification events appear only where the session had an action "
                "path or a configured objective; their absence is not evidence of either."
            ),
        ]
        if not recorded:
            limitations.insert(
                0,
                "A derived timeline is reconstructed from the session record; its "
                "order follows recorded timestamps, which can tie.",
            )
        return {
            "version": TIMELINE_VERSION,
            "session_id": record.session_id,
            "state": record.state.value,
            "generated_at": utc_now(),
            "source": "recorded" if recorded else "derived",
            "source_note": SOURCE_NOTE,
            "events": [self._resolve_event(record, event) for event in page],
            "page": page_info,
            "filters": {
                "types": sorted(query["types"]) if query["types"] else [],
                "categories": sorted(query["categories"]) if query["categories"] else [],
                "sources": sorted(query["sources"]) if query["sources"] else [],
                "severities": sorted(query["severities"]) if query["severities"] else [],
            },
            "retention": {
                "max_events": MAX_EVENTS_PER_SESSION,
                "stored": len(events),
                "dropped": log.dropped if log is not None else 0,
                "note": (
                    "The newest events are kept. A dropped count above zero means the "
                    "oldest events of this session are no longer part of its timeline."
                ),
            },
            "persistence": policy,
            "ordering": {
                "primary": "seq",
                "elapsed": "time.monotonic since the session started",
                "wall_clock": "UTC ISO-8601 label only; never used for ordering",
                "note": ORDERING_NOTE,
            },
            "classification": classify(record, events, persistence=policy),
            "replay": {
                "read_only": True,
                "can_start": False,
                "can_resume": False,
                "can_stop": False,
                "can_execute_action": False,
                "can_send_input": False,
                "reason": (
                    "Replay inspects persisted evidence. It cannot start, resume, stop "
                    "or drive a session, and NF-03 has no input path at all."
                ),
            },
            "read_only": True,
            "limitations": limitations,
        }

    def timeline_event(self, session_id: str, seq: int) -> dict[str, Any]:
        """One event with its evidence resolved; a missing seq is a KeyError."""
        record = self._require(session_id)
        event = next((item for item in self._events_for(record) if item.seq == int(seq)), None)
        if event is None:
            raise KeyError(f"Unknown timeline event seq {seq} for session {session_id}.")
        return {
            "version": TIMELINE_VERSION,
            "session_id": record.session_id,
            "event": self._resolve_event(record, event),
            "replay": {"read_only": True, "can_execute_action": False, "can_send_input": False},
        }

    def compare_sessions(self, left_id: str, right_id: str) -> dict[str, Any]:
        """Two persisted sessions side by side; read-only and never re-executed."""
        left = self._require(left_id)
        right = self._require(right_id)

        def telemetry_for(record: SessionRecord) -> dict[str, Any]:
            return record.telemetry.describe(
                state=record.state.value,
                duration_seconds=_duration_seconds(
                    record.started_at, record.ended_at or utc_now()
                ),
                state_changes=record.state_change_count,
                end_reason=record.end_reason,
                now=time.monotonic(),
            )

        payload = compare(left, telemetry_for(left), right, telemetry_for(right))
        for side, record in (("left", left), ("right", right)):
            log = self._timelines.get(record.session_id)
            policy = dict(log.persistence) if log is not None else {"state": "not_stored"}
            events = self._events_for(record)
            payload[side]["classification"] = classify(record, events, persistence=policy)
            payload[side]["timeline"] = {
                "source": "recorded" if log is not None and log.events else "derived",
                "events": len(events),
                "persistence": policy,
            }
        return payload

    def _require(self, session_id: str) -> SessionRecord:
        record = self._sessions.get(session_id)
        if record is None:
            raise KeyError(f"Unknown session: {session_id}")
        return record

    def _permission_decision(self) -> dict[str, Any]:
        """Ask the ONE permission layer; record which source decided.

        A session observes a window and sends no input, so the declaration is
        MEDIUM risk with no confirmation. The assessment is stored in the
        session record, which is what makes "who allowed this" answerable later.
        """
        if self.permissions is None:
            return {"allow": True, "source": "no-permission-layer", "reason": "No permission layer is wired."}
        try:
            decision = self.permissions.check(
                "novaforge.session", action="start_session", approved=True
            )
            payload: dict[str, Any] = dict(decision.to_dict())
            payload["allow"] = bool(decision.allow)
            return payload
        except Exception as exc:  # noqa: BLE001 - an unusable gate is a refusal
            return {"allow": False, "source": "error", "reason": f"{type(exc).__name__}: {exc}"}

    async def _loop(self, record: SessionRecord) -> None:
        """The observation loop: capture, verify freshness, record, publish."""
        try:
            record.transition(SessionState.RUNNING, reason="started")
            self._event(record, "session.started", payload={"reason": "started"})
            self._persist()
            assert self._running_ack is not None
            self._running_ack.set()
            await self._emit(
                "novaforge.session.changed",
                {"session_id": record.session_id, "state": record.state.value},
            )
            while True:
                if self._stop_event is None or self._stop_event.is_set():
                    return
                if self._pause_event is not None and self._pause_event.is_set():
                    if record.state is SessionState.PAUSING:
                        record.transition(SessionState.PAUSED, reason=record.pause_reason)
                        record.telemetry.note_pause(time.monotonic())
                        self._event(
                            record,
                            "session.paused",
                            payload={"reason": record.pause_reason},
                        )
                        self._persist()
                        await self._emit(
                            "novaforge.session.changed",
                            {
                                "session_id": record.session_id,
                                "state": record.state.value,
                                "reason": record.pause_reason,
                            },
                        )
                    # Wait in small slices: stop must be observable while paused,
                    # and resume() clears the event and sets RUNNING. The time
                    # limit is still checked here: a pause does not make the
                    # clock stop, and a limit that only applied while running
                    # would let a paused session outlive its own bound.
                    while (
                        self._pause_event is not None
                        and self._pause_event.is_set()
                        and self._stop_event is not None
                        and not self._stop_event.is_set()
                    ):
                        if self._limit_reached(record):
                            return
                        await asyncio.sleep(0.02)
                    continue
                if self._limit_reached(record):
                    return
                await self._tick(record)
                await asyncio.sleep(max(0.02, self._interval_for(record)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a loop failure is a FAILED session
            self._error(record, f"{type(exc).__name__}: {exc}", where="capture_loop")
            await self._finalize(record, SessionState.FAILED, reason="error")
        finally:
            # Runs for stop, limits, errors and cancellation alike. Idempotent:
            # a record already finalized by the branch above is left alone.
            await self._finalize(record, SessionState.STOPPED, reason=record.end_reason or "stopped")

    def _stop_requested(self) -> bool:
        """Whether a stop/abort has been requested for the current session."""
        return self._stop_event is not None and self._stop_event.is_set()

    def _interval_for(self, record: SessionRecord) -> float:
        """The observation interval this session was asked for.

        NF-01/NF-02 used one manager-wide interval. NF-04 records a scenario's
        interval per run, so the loop and the telemetry snapshot must read the
        SESSION's value; unknown or absent means the manager default.
        """
        value = record.mission.frame_interval_seconds
        return float(value) if value else self.frame_interval

    def _limit_reached(self, record: SessionRecord) -> bool:
        limit = record.mission.time_limit_seconds
        if limit is not None:
            # Monotonic, so changing the system clock cannot extend a bound. The
            # wall-clock duration is a label in the record, never the gate.
            if self._started_monotonic is not None:
                elapsed = max(0.0, time.monotonic() - self._started_monotonic)
            else:
                elapsed = _duration_seconds(record.started_at, utc_now())
            if elapsed >= limit:
                record.end_reason = "time_limit"
                self._error(
                    record,
                    f"The {limit}s session time limit was reached. The objective was NOT "
                    "verified; the session ended without claiming completion.",
                    where="limits",
                )
                self._intervene(
                    record,
                    "time_limit",
                    "time_limit",
                    "stopping",
                    detail=f"The {limit}s limit was reached; the session is ending.",
                    severity="warning",
                )
                return True
        budget = record.mission.max_actions
        if budget is not None and record.actions_executed >= budget:
            record.end_reason = "action_budget"
            self._error(
                record,
                f"The {budget}-action budget was exhausted. The objective was NOT verified.",
                where="limits",
            )
            self._intervene(
                record,
                "action_budget",
                "action_budget",
                "stopping",
                detail="The action budget was exhausted.",
                severity="warning",
            )
            return True
        return False

    async def _tick(self, record: SessionRecord) -> None:
        target = record.target
        if target is None:
            raise RuntimeError("Session has no target window.")
        try:
            geometry = await self.geometry(target.title)
        except Exception as exc:  # noqa: BLE001 - a probe failure pauses, never fakes a frame
            if self._stop_requested():
                return
            await self._pause_for(
                record, "target_unreadable", detail=f"{type(exc).__name__}: {exc}"
            )
            return
        if self._stop_requested():
            return
        if geometry is None:
            await self._pause_for(record, "target_lost")
            return
        resolved = _target_from_geometry(geometry)
        record.telemetry.note_target_check(utc_now())
        if resolved.handle != target.handle or resolved.pid != target.pid:
            await self._pause_for(record, "target_changed")
            return
        if resolved.bounds != target.bounds:
            # The window moved or resized: follow it, but say so in the record.
            record.target = resolved
        if not record.target_validated:
            # Recorded once per confirmed run, not once per tick: a per-second
            # "still validated" event is noise, not evidence.
            self._event(
                record,
                "target.validated",
                source="target",
                payload={"title": resolved.title, "handle": resolved.handle, "pid": resolved.pid},
                evidence=[session_ref(record.session_id)],
            )
        record.target_validated = True
        if self._stop_requested():
            return
        seq = (record.frames[-1].seq + 1) if record.frames else 1
        session_dir = self._frame_dir(record.session_id)
        path = session_dir / f"{seq:06d}.png"
        started = time.perf_counter()
        result: Mapping[str, Any] = {}
        try:
            result = await self.capture(str(path))
        except Exception as exc:  # noqa: BLE001 - reported, not fatal to the session
            if self._stop_requested():
                # The session is being stopped: a capture that failed on the way
                # out is not new evidence and must not be recorded as one.
                return
            latency_ms = (time.perf_counter() - started) * 1000
            record.telemetry.record_capture_failure()
            self._error(record, f"Capture failed: {type(exc).__name__}: {exc}", where="capture")
            observation = ObservationRecord(
                index=len(record.observations) + 1,
                at=utc_now(),
                source="desktop_capture",
                summary="Capture failed; no screen evidence was recorded.",
                evidence=False,
                latency_ms=round(latency_ms, 1),
                note=f"{type(exc).__name__}: {exc}",
            )
            record.record_observation(observation)
            self._event(
                record,
                "capture.failed",
                source="capture",
                severity="error",
                payload={"error": f"{type(exc).__name__}: {exc}", "latency_ms": round(latency_ms, 1)},
                evidence=[observation_ref(record.session_id, observation.index, observation.at)],
            )
            self._persist_throttled()
            return
        if self._stop_requested():
            # A stop/abort landed while the capture was in flight. Committing the
            # result would add evidence AFTER the backend confirmed the abort.
            return
        latency_ms = (time.perf_counter() - started) * 1000
        fresh = bool(result.get("fresh"))
        if not fresh:
            reason = str(result.get("verification") or result.get("error") or "not fresh")
            record.telemetry.record_stale()
            self._error(record, f"Capture reported no fresh frame: {reason}", where="capture")
            observation = ObservationRecord(
                index=len(record.observations) + 1,
                at=utc_now(),
                source="desktop_capture",
                summary="Capture produced no fresh frame; nothing is shown as live.",
                evidence=False,
                latency_ms=round(latency_ms, 1),
                note=reason[:300],
            )
            record.record_observation(observation)
            self._event(
                record,
                "frame.rejected",
                source="capture",
                severity="warning",
                payload={"reason": reason, "fresh": False, "latency_ms": round(latency_ms, 1)},
                evidence=[observation_ref(record.session_id, observation.index, observation.at)],
            )
            self._persist_throttled()
            return
        width, height, size_bytes, source, note = _crop_to_target(path, resolved)
        frame = FrameRecord(
            seq=seq,
            captured_at=str(result.get("captured_at") or utc_now()),
            path=str(path.resolve()),
            width=width,
            height=height,
            size_bytes=size_bytes,
            capture_ms=float(result.get("capture_ms") or round(latency_ms, 1)),
            capture_mode=str(result.get("capture_mode") or ""),
            fresh=True,
            source=source,
            note=note,
        )
        record.record_frame(frame)
        # The latency telemetry records is the measured duration around the
        # capture await, not a value the capture backend reports about itself.
        record.telemetry.record_frame(round(latency_ms, 3), frame.captured_at, time.monotonic())
        observation = ObservationRecord(
            index=len(record.observations) + 1,
            at=utc_now(),
            source="desktop_capture",
            summary=(
                f"{width}x{height} frame of {resolved.title!r} "
                f"(seq {seq}, {frame.capture_ms:.0f} ms)."
            ),
            evidence=True,
            latency_ms=round(latency_ms, 1),
            frame_seq=seq,
        )
        record.record_observation(observation)
        self._event(
            record,
            "frame.captured",
            source="capture",
            payload={
                "frame_seq": seq,
                "width": width,
                "height": height,
                "size_bytes": size_bytes,
                "capture_ms": frame.capture_ms,
                "capture_mode": frame.capture_mode,
                "source": source,
                "note": frame.note,
                "observation_index": observation.index,
            },
            evidence=[
                frame_ref(record.session_id, seq),
                observation_ref(record.session_id, observation.index, observation.at),
                telemetry_ref(record.session_id),
            ],
        )
        self._prune_frames(record, session_dir)
        self._persist_throttled()
        await self._emit(
            "novaforge.frame.captured",
            {
                "session_id": record.session_id,
                "seq": seq,
                "captured_at": frame.captured_at,
                "width": width,
                "height": height,
                "capture_ms": frame.capture_ms,
            },
        )

    async def _pause_for(
        self, record: SessionRecord, reason: str, *, detail: str = ""
    ) -> None:
        """Suspend safely when the target is gone, replaced or unreadable."""
        if record.state is SessionState.RUNNING:
            # The input boundary must refuse from here on: the identity that was
            # confirmed no longer holds for this run.
            record.target_validated = False
            record.pause_reason = reason
            record.transition(SessionState.PAUSING, reason=reason)
            record.transition(SessionState.PAUSED, reason=reason)
            record.telemetry.note_pause(time.monotonic())
            record.telemetry.note_target_event(reason)
            assert self._pause_event is not None
            self._pause_event.set()
            endings = {
                "target_lost": "closed.",
                "target_changed": "was replaced by a different window.",
                "target_unreadable": "could not be read.",
            }
            message = (
                "The session paused because its target window "
                + endings.get(reason, "could not be used.")
            )
            if detail:
                message += f" ({detail})"
            event_type = {
                "target_lost": "target.lost",
                "target_changed": "target.changed",
                "target_unreadable": "target.unreadable",
            }.get(reason)
            if event_type:
                self._event(
                    record,
                    event_type,
                    source="target",
                    severity="warning",
                    payload={"reason": reason, "detail": detail or message},
                    evidence=[session_ref(record.session_id)],
                )
            self._error(record, message, where="target")
            self._intervene(
                record,
                "target",
                reason,
                "paused",
                detail=detail or message,
                severity="warning",
            )
            self._persist()
            await self._emit(
                "novaforge.session.changed",
                {"session_id": record.session_id, "state": record.state.value, "reason": reason},
            )

    async def _finalize(self, record: SessionRecord, state: SessionState, *, reason: str) -> None:
        """Close a session exactly once, releasing input and persisting it.

        The ``_finalizing`` flag is what keeps a loop's own ``finally`` and an
        emergency-stop call from both releasing input: whichever arrives first
        closes the session, and the other returns without doing the work twice.
        """
        if record.state in SESSION_TERMINAL_STATES or record._finalizing:
            return
        record._finalizing = True
        if self._current_id == record.session_id:
            self._started_monotonic = None
        try:
            with contextlib.suppress(SessionTransitionError):
                record.transition(SessionState.STOPPING, reason=reason)
            cleanup = await self._release_inputs()
            record.input_cleanup = cleanup
            record.ended_at = record.ended_at or utc_now()
            record.end_reason = record.end_reason or reason
            record.telemetry.close(time.monotonic())
            target_state = (
                state
                if state in {SessionState.STOPPED, SessionState.FAILED, SessionState.COMPLETED}
                else SessionState.STOPPED
            )
            with contextlib.suppress(SessionTransitionError):
                record.transition(target_state, reason=record.end_reason)
            self._event(
                record,
                "session.failed" if target_state is SessionState.FAILED else "session.stopped",
                payload={
                    "state": target_state.value,
                    "end_reason": record.end_reason,
                    "requested_reason": reason,
                },
                evidence=[session_ref(record.session_id)],
            )
            self._event(
                record,
                "input.cleanup",
                payload=dict(cleanup),
                severity="info" if cleanup.get("ok") is True else "error",
                evidence=[session_ref(record.session_id)],
            )
            self._event(
                record,
                "telemetry.snapshot",
                source="telemetry",
                payload=self._telemetry_summary(record),
                evidence=[telemetry_ref(record.session_id)],
            )
        finally:
            record._finalizing = False
        self._persist()
        self._verify_timeline(record)
        if self._current_id == record.session_id:
            self._current_id = record.session_id  # keep for reads; `active()` filters terminal
        await self._emit(
            "novaforge.session.changed",
            {
                "session_id": record.session_id,
                "state": record.state.value,
                "reason": record.end_reason,
            },
        )

    async def _release_inputs(self) -> dict[str, Any]:
        """Release held inputs through the verified path, reporting the result.

        TWO guards, in order: this session's own gameplay guard (the one its
        actions actually used) and then the desktop runner's shared guard, so a
        key held by either is released. The counts reported are the gameplay
        guard's, because that is the input this session sent; the shared release
        is a safety net whose result is named rather than added to a total that
        would then describe two different things.
        """
        gameplay: tuple[int, int] | None = None
        if self.action_runner is not None:
            gameplay = await self.action_runner.release_all()
        if self.release_inputs is None:
            if gameplay is None:
                return {
                    "released": 0,
                    "held_after": 0,
                    "ok": True,
                    "note": "No input layer is wired to NovaForge; no input was sent.",
                }
            presses, releases = gameplay
            held_after = max(0, presses - releases)
            return {
                "released": releases,
                "presses": presses,
                "releases": releases,
                "held_after": held_after,
                "ok": held_after == 0,
                "note": (
                    "Every press has a matching release; no gameplay input is held."
                    if held_after == 0
                    else "The gameplay guard reports a press without a release."
                ),
            }
        try:
            presses, releases = await self.release_inputs()
        except Exception as exc:  # noqa: BLE001 - cleanup is reported, not raised
            return {"released": 0, "held_after": None, "ok": False, "note": f"{type(exc).__name__}: {exc}"}
        if gameplay is not None:
            presses, releases = gameplay
        pressed, released = max(0, int(presses)), max(0, int(releases))
        held_after = max(0, pressed - released)
        if held_after:
            note = "The guard reports a press without a release; investigate before the next session."
        elif released > pressed:
            # No key is held, but the counters disagree; repeating "presses
            # matched releases" here would describe a match that did not happen.
            note = (
                "No input is held, but the guard released more inputs than it "
                "pressed; the counters disagree."
            )
        else:
            note = "Every press has a matching release; no input is held."
        return {
            "released": released,
            "presses": pressed,
            "releases": released,
            "held_after": held_after,
            "ok": held_after == 0,
            "note": note,
        }

    # -- frames -------------------------------------------------------------

    @property
    def frames_root(self) -> Path:
        """The frame directory root, resolved once the manager is constructed."""
        root = self.frame_root
        if root is None:
            root = self.data_dir / "novaforge" / "frames"
            self.frame_root = root
        return root

    def _frame_dir(self, session_id: str) -> Path:
        directory = self.frames_root / session_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _prune_frames(self, record: SessionRecord, directory: Path) -> None:
        """Bound disk use: the newest ``max_frames_on_disk`` frames are kept."""
        files = sorted(directory.glob("*.png"))
        for path in files[: max(0, len(files) - self.max_frames_on_disk)]:
            with contextlib.suppress(OSError):
                path.unlink()

    def frame_path(self, session_id: str, *, seq: int | None = None) -> Path | None:
        """A frame file for serving, confined to this session's frame directory."""
        record = self._sessions.get(session_id)
        if record is None or not record.frames:
            return None
        wanted = seq if seq is not None else record.frames[-1].seq
        match = next((frame for frame in record.frames if frame.seq == wanted), None)
        if match is None:
            return None
        path = Path(match.path)
        try:
            path.resolve().relative_to(self.frames_root.resolve())
        except (OSError, ValueError):
            return None
        return path if path.is_file() else None


def _optional_identity(value: Any, label: str) -> int | None:
    """Read an optional validated target identity; a malformed one is refused."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise MissionValidationError([f"The validated target {label} is malformed; validate again."]) from exc


def _age_ms(captured_at: str) -> float | None:
    try:
        stamp = datetime.fromisoformat(captured_at)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return round((datetime.now(UTC) - stamp).total_seconds() * 1000, 1)


def _duration_seconds(start: str, end: str) -> float:
    try:
        first = datetime.fromisoformat(start)
        last = datetime.fromisoformat(end)
    except ValueError:
        return 0.0
    return round(max(0.0, (last - first).total_seconds()), 2)


def _target_from_geometry(geometry: Any) -> TargetWindow:
    """Adapt the desktop layer's geometry object without importing it eagerly."""
    bounds = tuple(int(value) for value in getattr(geometry, "bounds", (0, 0, 0, 0)))
    client = tuple(int(value) for value in getattr(geometry, "client", (0, 0, 0, 0)))
    if len(bounds) != 4 or len(client) != 4:  # pragma: no cover - the seam guarantees 4
        raise ValueError("Window geometry must carry four bounds and four client values.")
    return TargetWindow(
        title=str(getattr(geometry, "title", "") or ""),
        handle=int(getattr(geometry, "handle", 0) or 0),
        pid=int(getattr(geometry, "pid", 0) or 0),
        bounds=(bounds[0], bounds[1], bounds[2], bounds[3]),
        client=(client[0], client[1], client[2], client[3]),
        scale=float(getattr(geometry, "scale", 1.0) or 1.0),
        foreground=bool(getattr(geometry, "foreground", False)),
    )


def _crop_to_target(path: Path, target: TargetWindow) -> tuple[int, int, int, str, str]:
    """Crop a full-screen frame to the target window, in place.

    The capture worker writes PHYSICAL pixels in one DPI-aware space, and the
    window's bounds are read from that same worker, so the crop needs no scale
    conversion. Pillow is a hard dependency of the vision layer; if it is
    unavailable the full frame is kept and the source says ``screen`` — a
    truthful fallback rather than a fabricated window-sized frame.
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - optional here, required for cropping
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return 0, 0, size, "screen", "Pillow is unavailable; serving the full screen frame."
    try:
        with Image.open(path) as image:
            width, height = image.size
            left = max(0, min(width, target.bounds[0]))
            top = max(0, min(height, target.bounds[1]))
            right = max(0, min(width, target.bounds[2]))
            bottom = max(0, min(height, target.bounds[3]))
            if right - left < 8 or bottom - top < 8:
                size = path.stat().st_size
                return width, height, size, "screen", (
                    "The target window is outside the captured display; showing the full screen."
                )
            cropped = image.crop((left, top, right, bottom))
            cropped_size = cropped.size
            # A window that extends past the captured display is still cropped,
            # but the frame is PARTIAL. Saying so is the difference between a
            # recorded limitation and a frame that silently looks complete.
            clipped = (
                target.bounds[0] < 0
                or target.bounds[1] < 0
                or target.bounds[2] > width
                or target.bounds[3] > height
            )
        # Saved AFTER the source handle closes: Windows refuses to replace a
        # file that is still open for reading.
        cropped.save(path, format="PNG")
        return (
            cropped_size[0],
            cropped_size[1],
            path.stat().st_size,
            "window_crop",
            "The target window extends beyond the captured display; this frame is clipped."
            if clipped
            else "",
        )
    except Exception as exc:  # noqa: BLE001 - keep serving a real frame
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return 0, 0, size, "screen", f"The window crop failed ({type(exc).__name__}); full frame kept."
