"""NovaForge data model: missions, sessions, frames and stage records.

The vocabulary is deliberately small. NF-01 owns *when* a gaming session runs and
*what was observed*; it does not own a planner, a verifier or an action engine, so
nothing here pretends those exist. Three rules shape the model:

* **A stage is not a timeline position.** ``Observed``, ``Proposed``,
  ``Executed`` and ``Verified`` are separate records because they are separate
  facts: a proposal is not an action, an action is not a success, and only a
  verifier reading the target can populate ``Verified``. A stage this build
  cannot produce (or that has not happened yet) is reported with a reason
  (``StageRecord.available = False``), never fabricated.
* **Times are ISO-8601 UTC strings.** They travel through JSON persistence and
  the HTTP boundary unchanged; durations are DERIVED at read time so a record
  cannot carry a stale "duration" after a restart.
* **The state machine raises.** ``SessionRecord.transition`` refuses an illegal
  move instead of overwriting the old state, which is the only way a UI can
  trust the state it renders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from .objective import ObjectiveDefinition
from .telemetry import TelemetrySnapshot

__all__ = [
    "FrameRecord",
    "MissionConfig",
    "ObservationRecord",
    "SESSION_TRANSITIONS",
    "SESSION_TERMINAL_STATES",
    "SessionRecord",
    "SessionState",
    "StageRecord",
    "TargetWindow",
    "utc_now",
]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class SessionState(StrEnum):
    """The lifecycle a session is allowed to move through.

    ``COMPLETED`` is reached by ONE path: a checkable objective was VERIFIED
    against the target window. Ending because a limit ran out is ``STOPPED``
    with an ``end_reason`` — the distinction is the difference between "the
    mission succeeded" and "we ran out of clock". Both the time limit and the
    action budget can end a session, and the budget is enforced at execution
    (a denied action never consumes it).
    """

    READY = "READY"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


#: Every legal move, and only those. ``PAUSED -> RUNNING`` is resume;
#: ``RUNNING -> PAUSING -> PAUSED`` is pause, and the intermediate state is what
#: makes "the UI asked" distinguishable from "the loop acknowledged".
SESSION_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.READY: frozenset({SessionState.STARTING}),
    SessionState.STARTING: frozenset(
        {SessionState.RUNNING, SessionState.STOPPING, SessionState.FAILED}
    ),
    SessionState.RUNNING: frozenset(
        {SessionState.PAUSING, SessionState.STOPPING, SessionState.FAILED}
    ),
    SessionState.PAUSING: frozenset(
        {SessionState.PAUSED, SessionState.STOPPING, SessionState.FAILED}
    ),
    SessionState.PAUSED: frozenset(
        {SessionState.RUNNING, SessionState.STOPPING, SessionState.FAILED}
    ),
    # A verified objective is the ONE move into COMPLETED, and it happens while
    # closing (RUNNING -> STOPPING -> COMPLETED). Every other ending is STOPPED
    # with an end_reason; a stopped session is never a success.
    SessionState.STOPPING: frozenset(
        {SessionState.STOPPED, SessionState.COMPLETED, SessionState.FAILED}
    ),
    SessionState.STOPPED: frozenset(),
    SessionState.COMPLETED: frozenset(),
    SessionState.FAILED: frozenset(),
}

SESSION_TERMINAL_STATES = frozenset(
    {SessionState.STOPPED, SessionState.COMPLETED, SessionState.FAILED}
)


class SessionTransitionError(RuntimeError):
    """An illegal lifecycle move. Raised, never softened into an overwrite."""


class SessionConflictError(RuntimeError):
    """A start was refused because another session is still active."""


class MissionValidationError(ValueError):
    """A mission (or its target) is not runnable; carries field-level reasons."""

    def __init__(self, errors: list[str], *, target_candidates: tuple[str, ...] = ()) -> None:
        super().__init__("; ".join(errors) or "Mission validation failed.")
        self.errors = tuple(errors)
        self.target_candidates = target_candidates


@dataclass(frozen=True, slots=True)
class MissionConfig:
    """What the operator asked for, before anything ran."""

    name: str
    objective: str
    target_title: str
    success_criteria: str = ""
    time_limit_seconds: int | None = None
    max_actions: int | None = None
    #: NF-04: a per-session observation interval. None means the manager's
    #: configured default; a benchmark scenario records the value it used.
    frame_interval_seconds: float | None = None

    MAX_NAME = 80
    MAX_OBJECTIVE = 500
    MAX_TARGET = 200
    MAX_CRITERIA = 500
    MAX_TIME_LIMIT = 3600
    MAX_ACTIONS = 10_000
    MIN_FRAME_INTERVAL = 0.05
    MAX_FRAME_INTERVAL = 5.0

    def errors(self) -> list[str]:
        """Field-level validation reasons; an empty list means runnable."""
        problems: list[str] = []
        if not self.name.strip():
            problems.append("Mission name is required.")
        elif len(self.name.strip()) > self.MAX_NAME:
            problems.append(f"Mission name must be at most {self.MAX_NAME} characters.")
        if not self.objective.strip():
            problems.append("Mission objective is required.")
        elif len(self.objective.strip()) > self.MAX_OBJECTIVE:
            problems.append(f"Mission objective must be at most {self.MAX_OBJECTIVE} characters.")
        if not self.target_title.strip():
            problems.append("A target window title is required.")
        elif len(self.target_title.strip()) > self.MAX_TARGET:
            problems.append(f"Target window title must be at most {self.MAX_TARGET} characters.")
        if len(self.success_criteria.strip()) > self.MAX_CRITERIA:
            problems.append(f"Success criteria must be at most {self.MAX_CRITERIA} characters.")
        if self.time_limit_seconds is not None and not (1 <= self.time_limit_seconds <= self.MAX_TIME_LIMIT):
            problems.append(
                f"Time limit must be between 1 and {self.MAX_TIME_LIMIT} seconds, or blank."
            )
        if self.max_actions is not None and not (1 <= self.max_actions <= self.MAX_ACTIONS):
            problems.append(
                f"Action budget must be between 1 and {self.MAX_ACTIONS}, or blank."
            )
        if self.frame_interval_seconds is not None and not (
            self.MIN_FRAME_INTERVAL <= self.frame_interval_seconds <= self.MAX_FRAME_INTERVAL
        ):
            problems.append(
                f"Observation interval must be between {self.MIN_FRAME_INTERVAL} and "
                f"{self.MAX_FRAME_INTERVAL} seconds, or blank."
            )
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name.strip(),
            "objective": self.objective.strip(),
            "target_title": self.target_title.strip(),
            "success_criteria": self.success_criteria.strip(),
            "time_limit_seconds": self.time_limit_seconds,
            "max_actions": self.max_actions,
            "frame_interval_seconds": self.frame_interval_seconds,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "MissionConfig":
        """Best-effort read of persisted/request data; validation is separate."""
        data = dict(payload or {})

        def _optional_int(key: str) -> int | None:
            value = data.get(key)
            if value in (None, ""):
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                # Keep the invalid value visible as a sentinel that fails
                # validation instead of silently turning "abc" into None.
                return -1

        def _optional_float(key: str) -> float | None:
            value = data.get(key)
            if value in (None, ""):
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return -1.0

        return cls(
            name=str(data.get("name", "") or ""),
            objective=str(data.get("objective", "") or ""),
            target_title=str(data.get("target_title", "") or data.get("target", "") or ""),
            success_criteria=str(data.get("success_criteria", "") or ""),
            time_limit_seconds=_optional_int("time_limit_seconds"),
            max_actions=_optional_int("max_actions"),
            frame_interval_seconds=_optional_float("frame_interval_seconds"),
        )


@dataclass(frozen=True, slots=True)
class TargetWindow:
    """The window a session is pinned to, captured at start.

    Identity is stored, not just the title: a session must be able to notice
    that the window it was addressing was closed and another window took the
    same title. ``scale`` is the window's DPI multiplier, and ``bounds`` are
    physical pixels — the same space the capture worker writes.
    """

    title: str
    handle: int
    pid: int
    bounds: tuple[int, int, int, int]
    client: tuple[int, int, int, int]
    scale: float = 1.0
    foreground: bool = False

    @property
    def width(self) -> int:
        return max(0, self.bounds[2] - self.bounds[0])

    @property
    def height(self) -> int:
        return max(0, self.bounds[3] - self.bounds[1])

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "handle": self.handle,
            "pid": self.pid,
            "bounds": list(self.bounds),
            "client": list(self.client),
            "width": self.width,
            "height": self.height,
            "scale": self.scale,
            "foreground": self.foreground,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "TargetWindow | None":
        data = dict(payload or {})
        try:
            bounds = tuple(int(value) for value in data["bounds"])
            client = tuple(int(value) for value in data["client"])
            handle = int(data["handle"])
        except (KeyError, TypeError, ValueError):
            return None
        if len(bounds) != 4 or len(client) != 4:
            return None
        return cls(
            title=str(data.get("title", "") or ""),
            handle=handle,
            pid=int(data.get("pid", 0) or 0),
            bounds=(bounds[0], bounds[1], bounds[2], bounds[3]),
            client=(client[0], client[1], client[2], client[3]),
            scale=float(data.get("scale", 1.0) or 1.0),
            foreground=bool(data.get("foreground", False)),
        )


@dataclass(frozen=True, slots=True)
class FrameRecord:
    """One captured frame, and whether it is really this session's frame."""

    seq: int
    captured_at: str
    path: str
    width: int
    height: int
    size_bytes: int
    capture_ms: float
    capture_mode: str
    fresh: bool
    source: str  # "window_crop" | "screen" | "unavailable"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "captured_at": self.captured_at,
            "path": self.path,
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "capture_ms": self.capture_ms,
            "capture_mode": self.capture_mode,
            "fresh": self.fresh,
            "source": self.source,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    """What the session actually saw, in its own words and with its evidence."""

    index: int
    at: str
    source: str  # "desktop_capture" | "window_probe"
    summary: str
    evidence: bool
    latency_ms: float
    frame_seq: int | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "at": self.at,
            "source": self.source,
            "summary": self.summary,
            "evidence": self.evidence,
            "latency_ms": self.latency_ms,
            "frame_seq": self.frame_seq,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class StageRecord:
    """One stage of ``Observed -> Proposed -> Executed -> Verified``.

    ``available=False`` is a first-class answer: this phase has no decision or
    action pipeline attached, so the later stages say exactly that instead of
    showing a placeholder that could be mistaken for a record.
    """

    stage: str
    available: bool
    at: str = ""
    summary: str = ""
    reason: str = ""
    records: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "available": self.available,
            "at": self.at,
            "summary": self.summary,
            "reason": self.reason,
            "records": list(self.records),
        }


@dataclass(slots=True)
class SessionRecord:
    """The authoritative session state; every UI read is derived from this."""

    mission: MissionConfig
    session_id: str = field(default_factory=lambda: uuid4().hex)
    state: SessionState = SessionState.READY
    created_at: str = field(default_factory=utc_now)
    started_at: str = ""
    ended_at: str = ""
    target: TargetWindow | None = None
    frames: list[FrameRecord] = field(default_factory=list)
    observations: list[ObservationRecord] = field(default_factory=list)
    actions_attempted: int = 0
    actions_executed: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    recovery_attempts: int = 0
    pause_reason: str = ""
    end_reason: str = ""
    #: True only while a geometry check has CONFIRMED this run's target identity.
    #: The input boundary refuses when this is False, so a future input-producing
    #: phase cannot ride on a check from before a resume or a previous run.
    target_validated: bool = False
    input_cleanup: dict[str, Any] = field(default_factory=dict)
    permission: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    telemetry: TelemetrySnapshot = field(default_factory=TelemetrySnapshot)
    interventions: list[dict[str, Any]] = field(default_factory=list)
    state_change_count: int = 0
    #: The checkable objective, when the operator configured one. None means the
    #: session has no verifiable objective, which is reported as exactly that —
    #: never as "pending success".
    objective: ObjectiveDefinition | None = None
    #: The newest verification verdict (``ObjectiveVerification.to_dict()``).
    #: Kept as a plain dict so a persisted record stays readable, and so the
    #: verdict cannot be re-derived from the session state by accident.
    verification: dict[str, Any] = field(default_factory=dict)
    #: Digest-bound proposals, bounded. A proposal is NOT an action.
    action_proposals: list[dict[str, Any]] = field(default_factory=list)
    #: Every action that was attempted, with its ending. Bounded, newest last.
    action_records: list[dict[str, Any]] = field(default_factory=list)
    #: What this session's runtime actually has, set at start: the input path and
    #: the objective verifier. Persisted so a restart reports the truth about the
    #: session it restores instead of the capability of the process reading it.
    capabilities: dict[str, Any] = field(default_factory=dict)
    #: One finalize at a time; never serialized (it describes a live call, not state).
    _finalizing: bool = field(default=False, repr=False)

    MAX_FRAMES_KEPT = 30
    MAX_OBSERVATIONS_KEPT = 30
    MAX_ERRORS_KEPT = 20
    MAX_INTERVENTIONS_KEPT = 20
    MAX_ACTION_RECORDS_KEPT = 50
    MAX_PROPOSALS_KEPT = 20

    # -- lifecycle ----------------------------------------------------------

    def transition(self, target: SessionState, *, reason: str = "") -> None:
        """Move to ``target`` or raise; a refused move writes no history entry."""
        allowed = SESSION_TRANSITIONS[self.state]
        if target not in allowed:
            raise SessionTransitionError(
                f"Session {self.session_id} cannot move {self.state.value} -> {target.value}."
            )
        previous = self.state
        self.state = target
        self.state_change_count += 1
        entry = {"from": previous.value, "to": target.value, "at": utc_now()}
        if reason:
            entry["reason"] = reason
        self.history.append(entry)
        del self.history[:-50]

    def record_error(self, message: str, *, where: str = "") -> None:
        self.errors.append({"message": str(message)[:500], "where": where, "at": utc_now()})
        del self.errors[:-self.MAX_ERRORS_KEPT]

    def record_frame(self, frame: FrameRecord) -> None:
        self.frames.append(frame)
        del self.frames[:-self.MAX_FRAMES_KEPT]

    def record_observation(self, observation: ObservationRecord) -> None:
        self.observations.append(observation)
        del self.observations[:-self.MAX_OBSERVATIONS_KEPT]

    def add_intervention(
        self,
        kind: str,
        reason: str,
        outcome: str,
        *,
        detail: str = "",
        at: str | None = None,
    ) -> dict[str, Any]:
        """Record one Guardian decision; bounded, newest last."""
        entry = {
            "at": at or utc_now(),
            "kind": str(kind),
            "reason": str(reason),
            "outcome": str(outcome),
            "detail": str(detail)[:300],
        }
        self.interventions.append(entry)
        del self.interventions[:-self.MAX_INTERVENTIONS_KEPT]
        return entry

    def record_proposal(self, proposal: dict[str, Any]) -> None:
        """Remember one digest-bound proposal; bounded, newest last."""
        self.action_proposals.append(dict(proposal))
        del self.action_proposals[:-self.MAX_PROPOSALS_KEPT]

    def find_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        for entry in reversed(self.action_proposals):
            if str(entry.get("proposal_id", "")) == proposal_id:
                return dict(entry)
        return None

    def record_action(self, outcome: dict[str, Any], *, at: str | None = None) -> dict[str, Any]:
        """Record one attempted action and its ending, exactly once per attempt."""
        entry = dict(outcome)
        entry["at"] = at or utc_now()
        self.action_records.append(entry)
        del self.action_records[:-self.MAX_ACTION_RECORDS_KEPT]
        return entry

    def set_verification(self, verification: dict[str, Any]) -> None:
        """Store the newest verdict; the outcome is never inferred from state."""
        self.verification = dict(verification)

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The full record. Derived values (duration, frame age) are added by
        the manager's ``describe`` so persistence never stores a stale clock."""
        return {
            "version": 1,
            "session_id": self.session_id,
            "state": self.state.value,
            "mission": self.mission.to_dict(),
            "target": self.target.to_dict() if self.target else None,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "pause_reason": self.pause_reason,
            "end_reason": self.end_reason,
            "target_validated": self.target_validated,
            "frames": [frame.to_dict() for frame in self.frames],
            "observations": [obs.to_dict() for obs in self.observations],
            "counts": {
                "frames": len(self.frames),
                "observations": len(self.observations),
                "actions_attempted": self.actions_attempted,
                "actions_executed": self.actions_executed,
                "recovery_attempts": self.recovery_attempts,
                "state_changes": self.state_change_count,
            },
            "telemetry": self.telemetry.to_dict(),
            "interventions": list(self.interventions),
            "errors": list(self.errors),
            "input_cleanup": dict(self.input_cleanup),
            "permission": dict(self.permission),
            "history": list(self.history),
            "limits": {
                "time_limit_seconds": self.mission.time_limit_seconds,
                "max_actions": self.mission.max_actions,
                # Enforced where an execution path exists to count against. A
                # budget with no action path is reported as NOT enforced, never
                # as a bound the session had.
                "max_actions_enforced": bool(self.capabilities.get("action_budget_enforced", False)),
            },
            "objective": self.objective.to_dict() if self.objective else None,
            "actions": {
                "available": bool(self.capabilities.get("input_path", False)),
                "attempted": self.actions_attempted,
                "executed": self.actions_executed,
                "records": list(self.action_records[-10:]),
                "reason": (
                    ""
                    if self.capabilities.get("input_path", False)
                    else "No gameplay input path is wired to this session."
                ),
            },
            "verification": self._verification_view(),
            "pipeline": {
                "decision": {
                    "available": False,
                    "reason": (
                        "No decision pipeline is attached to a NovaForge session: actions "
                        "are proposed explicitly by the operator or a caller, not chosen "
                        "by a planner."
                    ),
                },
                "actions": {
                    "available": bool(self.capabilities.get("input_path", False)),
                    "reason": (
                        ""
                        if self.capabilities.get("input_path", False)
                        else "No gameplay input path is wired to this session."
                    ),
                },
                "recovery": {
                    "available": False,
                    "reason": "Recovery requires a decision pipeline, which this build does not attach.",
                },
            },
            "limitations": list(self._limitations()),
        }

    def _verification_view(self) -> dict[str, Any]:
        """The verification section: a real verdict, or exactly why there is none."""
        if self.verification:
            return dict(self.verification)
        if self.objective is not None:
            return {
                "outcome": "in_progress",
                "verified": None,
                "achieved": None,
                "decided": False,
                "supported": True,
                "status": "unverified",
                "reason": (
                    "The objective is configured but has not been verified yet; a running "
                    "session is not a satisfied objective."
                ),
                "definition": self.objective.to_dict(),
            }
        return {
            "outcome": "unsupported",
            "verified": None,
            "achieved": None,
            "decided": False,
            "supported": False,
            "objective_configured": False,
            "status": "unverified",
            "reason": (
                "No checkable objective was configured for this session, so no game "
                "outcome can be claimed — ending is not success."
            ),
        }

    def _limitations(self) -> tuple[str, ...]:
        limitations = ["Capture is primary-display only and cropped to the target window."]
        if not self.capabilities.get("input_path", False):
            limitations.insert(0, "No gameplay input path is wired; no action was sent.")
        if self.capabilities.get("action_budget_enforced", False) is not True:
            limitations.append("An action budget, when configured, could not be enforced for this session.")
        if self.objective is None:
            limitations.append("No checkable objective was configured, so gameplay outcome is unverified.")
        return tuple(limitations)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SessionRecord | None":
        try:
            record = cls(
                mission=MissionConfig.from_dict(payload.get("mission")),
                session_id=str(payload["session_id"]),
                state=SessionState(str(payload.get("state", "READY"))),
                created_at=str(payload.get("created_at", "")) or utc_now(),
                started_at=str(payload.get("started_at", "")),
                ended_at=str(payload.get("ended_at", "")),
                target=TargetWindow.from_dict(payload.get("target")),
            )
        except (KeyError, TypeError, ValueError):
            return None
        for frame in payload.get("frames") or []:
            try:
                record.frames.append(
                    FrameRecord(
                        seq=int(frame["seq"]),
                        captured_at=str(frame["captured_at"]),
                        path=str(frame["path"]),
                        width=int(frame["width"]),
                        height=int(frame["height"]),
                        size_bytes=int(frame["size_bytes"]),
                        capture_ms=float(frame.get("capture_ms", 0.0)),
                        capture_mode=str(frame.get("capture_mode", "")),
                        fresh=bool(frame.get("fresh", False)),
                        source=str(frame.get("source", "screen")),
                        note=str(frame.get("note", "")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        for obs in payload.get("observations") or []:
            try:
                record.observations.append(
                    ObservationRecord(
                        index=int(obs["index"]),
                        at=str(obs["at"]),
                        source=str(obs["source"]),
                        summary=str(obs["summary"]),
                        evidence=bool(obs.get("evidence", False)),
                        latency_ms=float(obs.get("latency_ms", 0.0)),
                        frame_seq=int(obs["frame_seq"]) if obs.get("frame_seq") is not None else None,
                        note=str(obs.get("note", "")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        counts = payload.get("counts") or {}
        try:
            record.actions_attempted = int(counts.get("actions_attempted", 0))
            record.actions_executed = int(counts.get("actions_executed", 0))
            record.recovery_attempts = int(counts.get("recovery_attempts", 0))
        except (TypeError, ValueError):
            pass
        record.errors = [
            dict(entry) for entry in payload.get("errors") or [] if isinstance(entry, dict)
        ][-cls.MAX_ERRORS_KEPT:]
        record.pause_reason = str(payload.get("pause_reason", ""))
        record.end_reason = str(payload.get("end_reason", ""))
        # An older snapshot has no such field; False is the safe default because
        # it makes the input boundary refuse until a fresh check says otherwise.
        record.target_validated = bool(payload.get("target_validated", False))
        record.input_cleanup = dict(payload.get("input_cleanup") or {})
        record.permission = dict(payload.get("permission") or {})
        record.history = [
            dict(entry) for entry in payload.get("history") or [] if isinstance(entry, dict)
        ][-50:]
        record.interventions = [
            dict(entry) for entry in payload.get("interventions") or [] if isinstance(entry, dict)
        ][-cls.MAX_INTERVENTIONS_KEPT:]
        record.telemetry = TelemetrySnapshot.from_dict(payload.get("telemetry"))
        record.objective = ObjectiveDefinition.from_dict(payload.get("objective"))
        record.verification = dict(payload.get("verification") or {})
        actions = payload.get("actions") or {}
        record.action_records = [
            dict(entry) for entry in actions.get("records") or [] if isinstance(entry, dict)
        ][-cls.MAX_ACTION_RECORDS_KEPT:]
        record.action_proposals = [
            dict(entry) for entry in payload.get("action_proposals") or [] if isinstance(entry, dict)
        ][-cls.MAX_PROPOSALS_KEPT:]
        record.capabilities = dict(payload.get("capabilities") or {})
        try:
            record.state_change_count = max(0, int(counts.get("state_changes", 0) or 0))
        except (TypeError, ValueError):
            record.state_change_count = 0
        return record
