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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from novacontrol.desktop.capture import THUMB_SUFFIX, sweep_orphan_thumbnails
from novacontrol.desktop.capture_backends import (
    CaptureOutcome,
    CaptureRouter,
    CaptureTarget,
    DisplayCropBackend,
    WindowSurfaceBackend,
)
from novacontrol.memory import InMemoryMemoryStore, MemoryManager
from novacontrol.persistence import JsonStateStore

from .compat import EMBEDDING_EMBEDDED, CompatibilityClass, CompatibilityLog
from .launcher import AMBIGUOUS_MATCH, NOT_FOUND_ON_INTERNAL_STORAGE

from .autonomy import AutonomyLimits, AutonomousSessionController
from .learning import (
    ExperienceStore,
    LearningService,
    StrategyExperiment,
    run_experiment,
)
from .tasks import AutonomyTaskGateway, TaskRunner, TaskSpec
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
from .control import (
    CONTROL_KINDS,
    ControlAction,
    ControlPolicy,
    ControlTarget,
    GameControlService,
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
from .observer import (
    DEFAULT_INTERVAL as DEFAULT_OBSERVATION_INTERVAL,
    CapturedFrame,
    GameObserver,
    WindowState,
    target_visibility,
    window_state,
)
from .knowledge import KnowledgeCandidate, KnowledgeRecord, SessionMemory
from .knowledge_service import KnowledgeService
from .objective_service import ObjectiveService
from .planner import GamePlanService, PlanPolicy, PlanStatus, PlanningContext
from .verification import (
    GameVerificationService,
    ObservationEvidence,
    VerificationContext,
    VerificationPolicy,
)
from .state import GameState
from .state_service import GameStateService
from .objective import (
    GameStateFileVerifier,
    ObjectiveDefinition,
    ObjectiveOutcome,
    ObjectiveRegistry,
    ObjectiveVerification,
    OcrTextVerifier,
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

#: Every field a session-start payload may carry: the mission's own fields plus
#: the two identity pins the UI sends after a validated window. Anything else is
#: refused by name in :meth:`NovaForgeManager.validate` rather than dropped in
#: silence — a caller who spells the time limit ``max_seconds`` (the task layer's
#: word) or a checkable objective ``objective_mode`` used to get a session with
#: neither, believing both were set. The autonomy start path already refuses
#: unknown bounds the same way.
_START_PAYLOAD_KEYS = frozenset(MissionConfig.__dataclass_fields__) | {
    "expected_handle",
    "expected_pid",
}

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

#: Refusal codes that describe the TARGET's state rather than a transient
#: failure. A capture rejected for one of these pauses the session (the operator
#: must restore the window); anything else is recorded and retried next tick.
_TARGET_PAUSE_REASONS = frozenset(
    {
        "target_lost",
        "target_changed",
        "target_minimized",
        "target_hidden",
        "target_offscreen",
        "target_not_foreground",
        "target_unreadable",
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


#: The stages between "installed on this machine" and "an observation was
#: accepted", in order. ONE list: the ladder report, the panel and the tests all
#: read it, so a stage cannot be added in one place and forgotten in another.
OBSERVATION_LADDER_STAGES: tuple[str, ...] = (
    "discovered",
    "launchable",
    "process_started",
    "window_identified",
    "embedding_compatible",
    "embedded",
    "backend_selected",
    "frame_acquired",
    "frame_validated",
    "observation_accepted",
)

#: The two rows that describe NATIVE EMBEDDING. They belong to the ladder (a
#: caller must be able to tell embedding from external capture) and are excluded
#: from ``held_at``: an observation does not require an embed, so a refused or
#: never-attempted embed is not where an observation is held up.
EMBEDDING_LADDER_STAGES: tuple[str, ...] = ("embedding_compatible", "embedded")


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
    #: The window-surface seam: ``capture_window(handle, path)``. When wired with
    #: ``capture`` above, the manager builds a capability-based router that tries
    #: the target's own surface first and refuses to treat a covered window's
    #: desktop crop as a frame. Without it the historical display-crop path runs,
    #: with the same rejection rules it has always had.
    capture_window: Callable[[int, str], Awaitable[Any]] | None = None
    #: Explicit backend list (tests and alternate hosts). Takes precedence over
    #: building one from the two seams; an empty tuple means "legacy path".
    capture_backends: tuple[Any, ...] = ()
    release_inputs: Callable[[], Awaitable[tuple[int, int]]] | None = None
    #: The gameplay injector: press/release edges and a cursor move, all through
    #: the same DPI-aware worker that captures frames. None means this session
    #: has no action path, and the Guardian refuses every action by naming that.
    action_input: Any | None = None
    #: Reads one window's current geometry by handle. The objective verifier
    #: uses it as its INDEPENDENT evidence source; the input path cannot write
    #: to it. None means no verifier can run.
    objective_probe: Callable[[int], Awaitable[Any | None]] | None = None
    #: Reads text out of a PNG the capture seam wrote. The application wires it
    #: to the same Windows OCR engine the vision-guided click uses, so the
    #: ``ocr_text`` verifier reads the SCREEN rather than the session's story
    #: about it. None means screen text cannot be read on this host, and the
    #: verifier reports itself unavailable instead of guessing.
    ocr_reader: Callable[[str], Awaitable[Any]] | None = None
    #: Set by the application once a closed loop exists; reports whether a
    #: decision policy is installed and what it is. Absent means none is, and
    #: the capability says so rather than naming a class that never ran.
    decision_capability: Callable[[], Mapping[str, Any]] | None = None
    #: NF-11: the game catalog + launch lifecycle service, when the host has one.
    #: A session never launches anything itself; this only makes the capability
    #: report truthful instead of claiming launch is impossible on every host.
    game_launcher: Any | None = None
    #: NF-15: ``process_identity(pid)`` -> a row with ``pid``/``start_time``, or
    #: None when the process is not running. None (unwired) means the observer
    #: cannot distinguish an exited process from a destroyed window, and says so
    #: instead of guessing.
    process_identity: Callable[[int], Awaitable[Mapping[str, Any] | None]] | None = None
    #: NF-15: the default observation interval, clamped by the observer's bounds.
    observation_interval: float = DEFAULT_OBSERVATION_INTERVAL
    #: The directories a ``game_state_file`` objective may name, relative to
    #: one of them. Empty means that verifier is not ready on this host, which
    #: is reported as unsupported rather than read from somewhere unapproved.
    artifact_roots: tuple[Any, ...] = ()
    action_policy: ActionPolicy = field(default_factory=ActionPolicy)
    #: NF-19: the typed control interface's own bounds. Separate from
    #: ``action_policy`` because the two paths answer different questions (a
    #: digest-bound proposal vs. one bounded input operation), and one widening
    #: the other would be invisible.
    control_policy: ControlPolicy = field(default_factory=ControlPolicy)
    #: NF-20: the planner's own bounds. Separate from ``control_policy`` so
    #: widening an input limit can never silently widen a plan's action count.
    plan_policy: PlanPolicy = field(default_factory=PlanPolicy)
    #: NF-21: the verifier's own bounds. Separate again, for the same reason: how
    #: long an outcome may take to establish, and how many recoveries a session
    #: may take, are not properties of a plan or of one input operation.
    verification_policy: VerificationPolicy = field(default_factory=VerificationPolicy)
    #: NF-22: the resource gate an autonomous run is admitted through. The
    #: application wires this to the SAME governor every other load asks, so a run
    #: cannot be admitted into a machine the rest of the build has classed as 
    #: under pressure. None (unwired) is reported as "not measured", never as
    #: permission.
    autonomy_resources: Callable[[], Mapping[str, Any]] | None = None
    permissions: Any | None = None
    publish: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
    capture_supported: bool = True
    frame_interval: float = 1.0
    pause_timeout: float = 3.0
    stop_timeout: float = 8.0
    max_sessions_kept: int = 40
    max_frames_on_disk: int = 30
    #: Observation frames are not session evidence, so they are kept separately
    #: and more tightly: a loop writes one per step and an unbounded directory
    #: is a leak that never announces itself.
    max_observation_frames: int = 20
    #: Telemetry is aggregated every tick; the snapshot is written at most this
    #: often. Lifecycle moves (start/pause/resume/stop) persist immediately.
    persist_interval: float = 2.0
    store: JsonStateStore | None = None
    frame_root: Path | None = field(default=None, repr=False)
    #: Built in ``__post_init__`` from ``action_input``/``objective_probe``.
    action_runner: ActionRunner | None = field(default=None, init=False, repr=False)
    #: NF-19: the ONE control interface for this manager. It composes
    #: ``action_runner`` (so every edge still travels the same InputGuard) and
    #: asks the Guardian before dispatch; it never creates an injector of its own.
    control: GameControlService = field(init=False, repr=False)
    #: NF-20: bounded planning over THIS session's own evidence and control
    #: service. Built in ``__post_init__`` so a plan can never be built from a
    #: different window than the session it belongs to.
    planner: GamePlanService = field(init=False, repr=False)
    #: NF-21: outcome verification and bounded recovery. It reads the SAME state
    #: service NF-20 plans from, writes its outcomes back into the plan through
    #: the planner's own record, and has no dispatch seam at all — so no recovery
    #: it selects can repeat an action.
    verification: GameVerificationService = field(init=False, repr=False)
    #: NF-22: the ONE autonomous supervisor, built on first use. It is bound to
    #: this manager (its gateway), so a rebuilt manager cannot be driven by a
    #: supervisor still pointing at the old one.
    _autonomy: AutonomousSessionController | None = field(default=None, init=False, repr=False)
    #: NF-25: the bounded-task layer over that one supervisor, built on first use.
    #: It holds no loop of its own — it preflights a task, hands it to the
    #: supervisor and judges the ending from the evidence the run produced.
    _tasks: TaskRunner | None = field(default=None, init=False, repr=False)
    #: NF-26: the ONE learning service over that task layer, built on first use.
    #: It reads judged task reports and writes bounded experience to this
    #: manager's own store; its only output is a strategy hint for the planner.
    _learning: LearningService | None = field(default=None, init=False, repr=False)
    #: Built in ``__post_init__``; None means the historical single-source path.
    _capture_router: CaptureRouter | None = field(default=None, init=False, repr=False)
    #: NF-15: the unified observation pipeline. Built in ``__post_init__`` from
    #: this manager's own probe/capture seams, so it can never read a different
    #: window than a session does.
    observer: GameObserver = field(init=False, repr=False)
    #: NF-16: the game-state reader. It consumes the observer's newest VALIDATED
    #: frame — it never captures anything itself, so perception and capture stay
    #: separate — and it is built here so a manager and its state can never point
    #: at different windows.
    state: GameStateService = field(init=False, repr=False)
    #: NF-17: what the states above APPEAR to ask for. Distinct from
    #: ``objectives`` (the NF-07 verifier registry), which verifies a stated
    #: objective; this one interprets the screen. The two must never share a
    #: name: one overwrote the other and left gameplay verification without its
    #: registry.
    objective_state: ObjectiveService = field(init=False, repr=False)
    #: NF-18: the SAME memory manager the application uses (SQLite on this host);
    #: None means an in-memory store, so a test or a host without a memory path
    #: still records knowledge for its own lifetime and never writes elsewhere.
    memory: MemoryManager | None = None
    #: NF-18: durable game knowledge, scoped per game and evidence-first.
    knowledge: KnowledgeService = field(init=False, repr=False)
    #: NF-18: what happened in THIS session — transient by construction, never
    #: an input to the knowledge service.
    session_memory: SessionMemory = field(default_factory=SessionMemory, init=False, repr=False)
    #: The observation session currently allowed to contribute knowledge, and the
    #: sessions that already ended (their evidence is refused, not stored).
    _knowledge_session: str = field(default="", init=False, repr=False)
    _knowledge_obsolete: set[str] = field(default_factory=set, init=False, repr=False)
    #: The content hash of the newest ACCEPTED frame, per session. A repeated
    #: hash is reported as a limitation, never silently treated as fresh.
    _last_content: dict[str, str] = field(default_factory=dict, init=False, repr=False)
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
        self.control = GameControlService(
            policy=self.control_policy,
            runner=self.action_runner,
            read_target=self._control_target,
            gate=self._control_gate,
            publish=self._publish_control,
        )
        verifiers: list[Any] = []
        if self.objective_probe is not None:
            verifiers.append(WindowTitleVerifier(probe=self.objective_probe))
        if self.ocr_reader is not None:
            verifiers.append(OcrTextVerifier(probe=self.read_screen_text))
        if self.artifact_roots:
            verifiers.append(GameStateFileVerifier(roots=tuple(self.artifact_roots)))
        self.objectives = ObjectiveRegistry(verifiers=tuple(verifiers))
        if self.capture_backends:
            self._capture_router = CaptureRouter(tuple(self.capture_backends))
        elif self.capture_window is not None and self.capture is not None:
            self._capture_router = CaptureRouter(
                (WindowSurfaceBackend(self.capture_window), DisplayCropBackend(self.capture))
            )
        self.observer = GameObserver(
            frame_dir=self.frame_root / "observed",
            resolve=self.geometry,
            capture=self.capture_observation,
            identity=self.process_identity,
            publish=self._publish_observation,
            store=self.store,
            interval=self.observation_interval,
        )
        # NF-17 reads the states NF-16 produces: one direction, no second capture
        # path, and the state service stays the only reader of the observer.
        self.objective_state = ObjectiveService(
            publish=self._publish_observation, store=self.store
        )
        # NF-18 consumes what NF-16 produced (through the objective reader) and
        # never reads a frame of its own. The manager supplies the session gate:
        # evidence from an ended observation session is refused.
        if self.memory is None:
            # One in-process store for a manager nobody wired one to: knowledge
            # still works for the process, and nothing is written elsewhere.
            self.memory = MemoryManager(InMemoryMemoryStore())
        self.knowledge = KnowledgeService(
            memory=self.memory,
            publish=self._publish_observation,
            session_validator=self._knowledge_session_valid,
        )
        self.state = GameStateService(
            observer=self.observer,
            publish=self._publish_observation,
            store=self.store,
            on_state=self._consume_state,
        )
        # NF-20 composes the three services above and the ONE control interface:
        # it reads evidence, never a window, and submits every step through
        # ``submit_control_action`` (the same NF-19 path a direct request uses).
        self.planner = GamePlanService(
            policy=self.plan_policy,
            context_reader=self._planning_context,
            submit=self.submit_control_action,
            cancel_action=self.cancel_control_action,
            publish=self._publish_plan,
        )
        # NF-21 composes the same evidence the planner reads and the same plan
        # store a decision belongs to. ``record`` is the planner's own method, so
        # a verified outcome lands on the plan it describes; the recovery seams
        # are NF-20's and the session's own interfaces, never a second planner and
        # never an input path.
        self.verification = GameVerificationService(
            policy=self.verification_policy,
            context_reader=self._verification_context,
            observe=self._observe_for_verification,
            record=self._record_outcomes,
            invalidate=self._invalidate_plan_for_recovery,
            replan=self._replan_for_recovery,
            pause=self._pause_for_recovery,
            stop=self._stop_for_recovery,
            notify=self._notify_operator,
            publish=self._publish_verification,
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
                "A checkable objective is read from the target: its window title, or text recognised by OCR on a fresh frame of it. A session with none never reports success.",
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
            "backend": frame.backend,
            "content": frame.content,
            "validation": dict(frame.validation),
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
        # Nothing is started with a field this build would ignore: an unread
        # keyword is a typo or a different layer's spelling, and staying silent
        # about it is how an operator ends up believing a bound or an objective
        # is in force when it is not.
        unknown = sorted({str(key) for key in payload} - _START_PAYLOAD_KEYS)
        if unknown:
            errors.append(
                "Unknown session field(s) "
                + ", ".join(repr(key) for key in unknown)
                + ": this build would ignore them. Accepted fields are "
                + ", ".join(sorted(_START_PAYLOAD_KEYS))
                + "."
            )
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
                        measured = _target_from_geometry(geometry)
                        issue, detail = _target_visibility_issue(measured)
                        if issue:
                            errors.append(
                                f"The target window cannot be observed: {detail} "
                                "Restore it (a fullscreen game minimized by focus loss "
                                "must be returned to the foreground) and validate again."
                            )
                        target = measured.to_dict()
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
            kinds = self.objectives.available_kinds()
            record.capabilities = {
                "input_path": self.action_runner is not None,
                "action_budget_enforced": self.action_runner is not None,
                "objective_verifier": bool(kinds),
                "objective_kinds": kinds,
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
            # Handle first, like the tick: a retitled window is still the target
            # the operator pinned, so resuming must not depend on its old label.
            geometry: Any | None = None
            if self.objective_probe is not None and record.target.handle:
                try:
                    geometry = await self.objective_probe(record.target.handle)
                except Exception:  # noqa: BLE001 - fall through to the title lookup
                    geometry = None
            if geometry is None:
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
            visibility_issue, visibility_detail = _target_visibility_issue(resolved)
            if visibility_issue:
                record.target = resolved
                raise MissionValidationError(
                    [
                        f"Resume refused: {visibility_detail} "
                        "Restore the window to the foreground before resuming."
                    ]
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
        # A plan belongs to the session's lifetime: stopping the session stops
        # any outstanding plan work, before the lock the running step holds.
        with contextlib.suppress(Exception):
            await self.planner.cancel(session_id)
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
        # A plan cannot outlive the session it was built for. Cancelling it first
        # also asks the in-flight NF-19 action to stop and release what it holds.
        with contextlib.suppress(Exception):
            await self.planner.cancel(session_id)
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

    # -- observation and OCR seams ------------------------------------------

    async def window_probe(self, handle: int) -> Any | None:
        """Read one window's geometry for an observation.

        The same probe the objective verifier reads, exposed so observation and
        verification can never disagree about what window they looked at. None
        means this host cannot read windows (or this one is gone).
        """
        if self.objective_probe is None:
            return None
        try:
            return await self.objective_probe(handle)
        except Exception:  # noqa: BLE001 - an unreadable window is a stated gap
            return None

    async def _acquire_frame(
        self, target: TargetWindow, path: str, *, session_id: str = ""
    ) -> dict[str, Any]:
        """Capture one frame through the router, or through the legacy crop path.

        The returned mapping is the historical capture shape PLUS the backend's
        own facts (``source``, ``backend``, ``validation``, ``reason_code``). The
        caller records a frame only when ``fresh`` is true and the source is a
        frame OF the target — a fresh desktop fallback is still not evidence.
        """
        router = self._capture_router
        if router is not None:
            request = CaptureTarget(
                handle=target.handle,
                title=target.title,
                bounds=target.bounds,
                minimized=target.minimized,
                visible=target.visible,
                foreground=target.foreground,
            )
            outcome: CaptureOutcome = await router.capture(
                path, request, previous_hash=self._last_content.get(session_id, "")
            )
            if outcome.ok:
                inspection = outcome.inspection or {}
                self._last_content[session_id] = str(inspection.get("content_hash") or "")
            limitations = list(outcome.verdict.get("limitations") or [])
            return {
                "fresh": outcome.ok,
                "verification": "" if outcome.ok else outcome.reason,
                "error": "" if outcome.ok else outcome.reason,
                "width": outcome.width,
                "height": outcome.height,
                "size_bytes": outcome.size_bytes,
                "source": outcome.source,
                "backend": outcome.backend,
                "note": str(limitations[0]) if outcome.ok and limitations else "",
                "capture_ms": outcome.elapsed_ms,
                "content": str(outcome.verdict.get("content") or ""),
                "validation": dict(outcome.verdict),
                "reason_code": str(outcome.verdict.get("code") or ("ok" if outcome.ok else "capture_rejected")),
                "attempts": [dict(attempt) for attempt in outcome.attempts],
            }
        result = await self.capture(path)
        fresh = bool(result.get("fresh"))
        if not fresh:
            return {
                "fresh": False,
                "verification": str(
                    result.get("verification") or result.get("error") or "not fresh"
                ),
            }
        width, height, size_bytes, source, note = _crop_to_target(Path(path), target)
        return {
            "fresh": True,
            "width": width,
            "height": height,
            "size_bytes": size_bytes,
            "source": source,
            "backend": "gdi_display",
            "note": note,
            "capture_ms": float(result.get("capture_ms") or 0.0),
            "content": "",
            "validation": {},
            "reason_code": "ok" if source == "window_crop" else "screen_fallback",
            "attempts": [],
        }

    async def capture_frame(self, title: str) -> dict[str, Any]:
        """One FRESH frame of the window matching ``title``, cropped to it.

        This is the observation loop's capture seam: the same worker, the same
        freshness rule and the same crop as a session frame, but written outside
        a session because an observation is not a recording. A missing window or
        a stale capture is reported as ``fresh: False`` with the reason, never
        as a just-captured frame.
        """
        try:
            geometry = await self.geometry(title)
        except Exception as exc:  # noqa: BLE001 - a rejected probe is a reported gap
            return {
                "fresh": False,
                "reason": f"The target window could not be read: {type(exc).__name__}: {exc}.",
            }
        if geometry is None:
            return {
                "fresh": False,
                "reason": f"No open window matches {title!r}, so nothing was captured.",
            }
        target = _target_from_geometry(geometry)
        issue, detail = _target_visibility_issue(target)
        if issue:
            return {
                "fresh": False,
                "reason": f"{detail} No target frame was captured.",
                "target_issue": issue,
            }
        return await self._capture_observation_frame(target)

    # -- NF-15: unified game observation -----------------------------------

    def observation_status(self) -> dict[str, Any]:
        """The observation pipeline's own state, never a session's."""
        return self.observer.status()

    async def start_observation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Bind a target window and begin the bounded observation schedule.

        ``accepted`` is separate from the session manager on purpose: observing
        a window is not a gameplay session, and a failed bind creates no
        session at all. The target identity may be pinned by ``pid`` and
        ``start_time`` (as a verified launch reports them); when supplied they
        are checked before anything is bound.
        """
        data = dict(payload or {})
        title = str(
            data.get("title") or data.get("target_title") or data.get("window") or ""
        ).strip()
        if not self.capture_supported:
            # A host with no capture capability must refuse observation up front,
            # not bind a target and fail five ticks later.
            return {
                **self.observer.status(),
                "started": False,
                "reason_code": "capture_unsupported",
                "reason": (
                    "Screen capture is not available on this host, so no observation "
                    "can be bound."
                ),
            }
        try:
            pid = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            pid = -1
        if pid < 0:
            return {
                **self.observer.status(),
                "started": False,
                "reason_code": "invalid_pid",
                "reason": "The requested process id is not a number.",
            }
        interval = data.get("interval") or data.get("interval_seconds")
        try:
            requested = float(interval) if interval not in (None, "") else None
        except (TypeError, ValueError):
            requested = None
        return await self.observer.start(
            title=title,
            game=str(data.get("game") or data.get("name") or ""),
            pid=pid,
            start_time=str(data.get("start_time") or ""),
            process=str(data.get("process") or data.get("process_name") or ""),
            interval=requested,
        )

    async def pause_observation(self, reason: str = "user") -> dict[str, Any]:
        return await self.observer.pause(reason)

    async def resume_observation(self) -> dict[str, Any]:
        return await self.observer.resume()

    async def stop_observation(self, reason: str = "user") -> dict[str, Any]:
        """Stop observing, and close that session to further knowledge writes."""
        status = self.observer.status()
        session_id = str(status.get("session_id") or "") if isinstance(status, Mapping) else ""
        result = await self.observer.stop(reason)
        current = self._knowledge_session
        if current:
            self._knowledge_obsolete.add(current)
        if session_id:
            self._knowledge_obsolete.add(session_id)
        self._knowledge_session = ""
        return result

    # -- NF-18: knowledge ingestion -----------------------------------------

    def _knowledge_session_valid(self, session_id: str) -> bool:
        """Only the live observation session may contribute knowledge.

        An empty id means the evidence names no session (a declared profile, a
        manual note); that is allowed, because it is not a claim about a run.
        """
        if not session_id:
            return True
        if session_id in self._knowledge_obsolete:
            return False
        return not self._knowledge_session or session_id == self._knowledge_session

    async def _consume_state(self, state: GameState) -> None:
        """The one path from a validated state to durable knowledge.

        NF-17 interprets first (it is the consumer that existed), then both the
        state's recognized fields and the interpretation become candidates. A
        transient observation only ever reaches knowledge as an explicit
        candidate; nothing here promotes session memory.
        """
        self.session_memory.note_state(state)
        session_id = str(state.observation.session_id or "")
        if session_id:
            self._knowledge_session = session_id
        # NF-17 and NF-18 are separate consumers of the same state: an unexpected
        # interpreter failure must not also suppress what NF-16 could RECOGNISE.
        interpretation: Any | None = None
        try:
            interpretation = await self.objective_state.observe(state)
        except Exception as exc:  # noqa: BLE001 - knowledge of the screen outranks the guess
            # Session memory is where a failure belongs: it explains why this run
            # learned less, and it is never written as knowledge.
            self.session_memory.note_failure(
                "objective_interpretation_failed", f"{type(exc).__name__}: {exc}"
            )
            interpretation = None
        try:
            await self.knowledge.record_state(state)
            if interpretation is not None:
                self.session_memory.note_objectives(interpretation)
                await self.knowledge.record_objectives(interpretation)
        except Exception as exc:  # noqa: BLE001 - a knowledge bug cannot fail a reading
            self.session_memory.note_failure(
                "knowledge_ingest_failed", f"{type(exc).__name__}: {exc}"
            )
            return

    async def record_knowledge(self, candidate: KnowledgeCandidate) -> dict[str, Any]:
        """Accept one explicit candidate (a manual note, an experiment result)."""
        record = await self.knowledge.record(candidate)
        if record is None:
            return {
                "recorded": False,
                "reason_code": "candidate_refused",
                "reason": (self.knowledge.status().get("last_error") or {}).get("reason", ""),
            }
        return {
            "recorded": True,
            "record": record.to_dict(),
            "status": self.knowledge.status(),
        }

    async def revalidate_knowledge(
        self, knowledge_id: str, evidence: Any, *, game: str = ""
    ) -> dict[str, Any]:
        rows = evidence if isinstance(evidence, (list, tuple)) else [evidence]
        return await self.knowledge.revalidate(knowledge_id, rows, game=game)

    async def knowledge_records(
        self,
        *,
        game: str,
        category: Any = None,
        status: Any = None,
        query: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        rows = await self.knowledge.records(
            game=game, category=category, status=status, query=query, limit=limit
        )
        return {
            "game": game,
            "count": len(rows),
            "records": [record.to_dict() for record in rows],
            "summary": await self.knowledge.summary(game=game),
        }

    async def knowledge_status(self, *, game: str = "") -> dict[str, Any]:
        """The read surface: service status, per-game summary, session memory.

        Session memory is reported SEPARATELY and says ``persisted: False``, so
        the UI can never present a transient objective as durable knowledge.
        """
        summary = await self.knowledge.summary(game=game)
        return {
            "available": True,
            "game": game,
            "knowledge": self.knowledge.status(),
            "summary": summary,
            "session_memory": self.session_memory.rows(),
            "separation": (
                "Session memory is transient and is not stored; only evidence-backed "
                "candidates become knowledge."
            ),
        }

    async def observe_frame_once(self) -> dict[str, Any]:
        """One immediate read; a concurrent read is answered ``busy``."""
        return await self.observer.observe_once()

    def observation_frame_path(self) -> Path | None:
        """The newest VALIDATED observation frame, or None.

        Confined to the observer's own session directory by the observer, so a
        route cannot use it to read an arbitrary file.
        """
        return self.observer.frame_path()

    async def _publish_observation(self, event_type: str, payload: dict[str, Any]) -> None:
        """Relay an observation event on the application's ONE bus.

        A lost event must not stop the observation that produced it, and the
        payload never carries pixels or free-text reasoning.
        """
        if self.publish is None:
            return
        try:
            await self.publish(event_type, payload)
        except Exception:  # noqa: BLE001 - observation outranks its announcement
            return

    async def read_frame_text(self, path: str) -> Any:
        """Read one frame with the injected engine; None when it is unavailable.

        The observation layer asks the manager rather than reaching for the
        engine itself, so there is ONE OCR seam and a host without an engine
        never starts a subprocess that could not read anything.
        """
        if self.ocr_reader is None:
            return None
        return await self.ocr_reader(path)

    async def read_screen_text(self, handle: int) -> dict[str, Any] | None:
        """Read the text on one window's CURRENT screen, as verifier evidence.

        A fresh frame is captured and cropped to the window, then read by the
        injected OCR engine. The returned mapping is the shape
        :class:`OcrTextVerifier` accepts, and it states what it did NOT measure:
        Windows' OCR exposes words but no confidence, so ``confidence`` is
        ``None`` and ``confidence_reported`` is ``False`` rather than a made-up
        number. ``None`` means the window itself could not be read, which the
        verifier reports UNKNOWN instead of "text absent".
        """
        if self.ocr_reader is None:
            return {
                "available": False,
                "reason": "No OCR engine is wired on this host.",
            }
        if self.objective_probe is None:
            return None
        try:
            geometry = await self.objective_probe(handle)
        except Exception:  # noqa: BLE001 - an unreadable window is not a verdict
            return None
        if geometry is None:
            return None
        frame = await self._capture_observation_frame(_target_from_geometry(geometry))
        if not bool(frame.get("fresh")):
            return {
                "available": True,
                "fresh": False,
                "path": str(frame.get("path") or ""),
                "reason": str(frame.get("reason") or "the capture reported no fresh frame"),
            }
        try:
            words = await self.ocr_reader(str(frame.get("path") or ""))
        except Exception as exc:  # noqa: BLE001 - a broken engine is unavailable
            return {
                "available": False,
                "reason": f"The OCR engine failed: {type(exc).__name__}: {exc}.",
            }
        if words is None:
            return {
                "available": False,
                "reason": "The OCR engine is unavailable on this host.",
            }
        normalised = [_ocr_word(word) for word in words]
        normalised = [word for word in normalised if word["text"]]
        lines: dict[int, list[str]] = {}
        for word in normalised:
            lines.setdefault(int(word["line"]), []).append(str(word["text"]))
        return {
            "available": True,
            "fresh": True,
            "captured_at": str(frame.get("captured_at") or ""),
            "age_seconds": frame.get("age_seconds"),
            "path": str(frame.get("path") or ""),
            "text": "\n".join(" ".join(parts) for _, parts in sorted(lines.items())),
            "words": normalised,
            "confidence": None,
            "confidence_reported": False,
            "engine": "windows.media.ocr",
            "reason": "",
        }

    async def capture_observation(
        self, window: WindowState, path: str, *, previous_hash: str = ""
    ) -> CapturedFrame:
        """One validated frame OF ``window`` for the observation pipeline.

        This is the single capture implementation NF-15 adds nothing beside: it
        uses the SAME capability router a session uses (so a covered window is
        captured from its own surface, and a desktop crop of a background window
        is refused), and falls back to the historical display-crop path only on
        a host that has no router at all. The returned ``CapturedFrame`` keeps
        the verdict and the backend; a caller must not infer ``ok`` from the
        existence of a file.
        """
        target = TargetWindow(
            title=window.title,
            handle=window.handle,
            pid=window.pid,
            bounds=window.bounds,
            client=window.client,
            scale=window.scale,
            foreground=window.foreground,
            minimized=window.minimized,
            visible=window.visible,
        )
        router = self._capture_router
        if router is not None:
            outcome: CaptureOutcome = await router.capture(
                path,
                CaptureTarget(
                    handle=target.handle,
                    title=target.title,
                    bounds=target.bounds,
                    minimized=target.minimized,
                    visible=target.visible,
                    foreground=target.foreground,
                ),
                previous_hash=previous_hash,
            )
            verdict = dict(outcome.verdict)
            limitations = [str(item) for item in verdict.get("limitations") or () if item]
            try:
                age_seconds: float | None = round(
                    max(0.0, time.time() - Path(outcome.path or path).stat().st_mtime), 3
                )
            except OSError:
                age_seconds = None
            return CapturedFrame(
                ok=outcome.ok,
                path=outcome.path or str(path),
                width=outcome.width,
                height=outcome.height,
                size_bytes=outcome.size_bytes,
                backend=outcome.backend,
                source=outcome.source,
                elapsed_ms=(
                    round(float(outcome.elapsed_ms), 1)
                    if outcome.elapsed_ms is not None
                    else None
                ),
                age_seconds=age_seconds,
                reason=outcome.reason,
                reason_code=str(verdict.get("code") or ("ok" if outcome.ok else "capture_rejected")),
                verdict=verdict,
                content_hash=str((outcome.inspection or {}).get("content_hash") or ""),
                # The router keeps the backend's note about an accepted frame;
                # that is where "this crop is clipped to the display" lives. It
                # must not be inferred from the verdict reason, which is empty
                # for a valid frame.
                clipped="clipped" in str(outcome.note or "").lower(),
                note=str(outcome.note or (limitations[0] if limitations else "")),
            )
        requested = datetime.now(UTC)
        started = time.monotonic()
        try:
            result = await self.capture(str(path))
        except Exception as exc:  # noqa: BLE001 - a failed capture is a reported result
            return CapturedFrame(
                ok=False,
                path=str(path),
                backend="gdi_display",
                source="display",
                reason=f"Capture failed: {type(exc).__name__}: {exc}.",
                reason_code="capture_failed",
                verdict={"capture_ok": False, "target_visible": True, "fresh": False,
                         "content_ok": False, "livable": False},
            )
        if not bool(result.get("fresh")):
            reason = str(result.get("verification") or result.get("error") or "not fresh")
            return CapturedFrame(
                ok=False,
                path=str(path),
                backend="gdi_display",
                source="display",
                elapsed_ms=round(max(0.0, time.monotonic() - started) * 1000.0, 1),
                reason=reason,
                reason_code="capture_rejected",
                verdict={"capture_ok": False, "target_visible": True, "fresh": False,
                         "content_ok": False, "livable": False},
            )
        width, height, size, source, note = _crop_to_target(Path(path), target)
        elapsed = round(max(0.0, time.monotonic() - started) * 1000.0, 1)
        try:
            age_seconds = round(max(0.0, time.time() - Path(path).stat().st_mtime), 3)
        except OSError:
            age_seconds = None
        ok = source == "window_crop"
        return CapturedFrame(
            ok=ok,
            path=str(path),
            width=width,
            height=height,
            size_bytes=size,
            backend="gdi_display",
            source=source,
            elapsed_ms=elapsed,
            age_seconds=age_seconds,
            reason="" if ok else (note or f"The capture did not contain the target (source: {source})."),
            reason_code="ok" if ok else "screen_fallback",
            verdict={
                "capture_ok": True,
                "target_visible": True,
                "fresh": ok,
                "content_ok": ok,
                "livable": ok,
                "reason": "" if ok else note,
                "content": "valid" if ok else "not_target",
                "backend": "gdi_display",
                "source": source,
                "limitations": [note] if note else [],
            },
            clipped="clipped" in note.lower(),
            note=note,
        )

    async def _capture_observation_frame(self, target: TargetWindow) -> dict[str, Any]:
        """Capture one frame outside a session, crop it, and bound the kept files.

        Kept as the historical mapping adapter over :meth:`capture_observation`:
        NF-08's observation builder and the OCR verifier read this shape, and
        NF-15's observer reads the typed result directly. There is one capture.
        """
        issue, detail = _target_visibility_issue(target)
        if issue:
            return {
                "fresh": False,
                "reason": f"{detail} No target frame was captured.",
                "target_issue": issue,
            }
        root = self.frame_root or (self.data_dir / "novaforge" / "frames")
        directory = root / "observations"
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {
                "fresh": False,
                "reason": f"The observation frame directory is unusable: {exc}.",
            }
        path = directory / f"{time.time_ns()}.png"
        requested = datetime.now(UTC)
        window = window_state(target)
        if window is None:
            return {
                "fresh": False,
                "path": str(path),
                "reason": "The target window could not be measured, so no frame was captured.",
            }
        frame = await self.capture_observation(
            window, str(path), previous_hash=self._last_content.get("observation", "")
        )
        self._prune_observation_frames(directory)
        if not frame.ok:
            return {
                "fresh": False,
                "path": frame.path or str(path),
                "reason": frame.reason or "not fresh",
                "backend": frame.backend,
                "validation": frame.verdict,
                "reason_code": frame.reason_code,
            }
        self._last_content["observation"] = frame.content_hash
        return {
            "path": frame.path,
            "fresh": True,
            "captured_at": requested.isoformat(),
            "age_seconds": frame.age_seconds,
            "width": frame.width,
            "height": frame.height,
            "size_bytes": frame.size_bytes,
            "source": frame.source,
            "backend": frame.backend,
            "capture_ms": frame.elapsed_ms,
            "note": frame.note,
            # The window this frame was taken from. The observation builder
            # compares these with the handle/pid it PROBED, so a same-titled
            # window cannot have its pixels described as the target's.
            "handle": target.handle,
            "pid": target.pid,
            # A window extending past the display yields a PARTIAL frame. NF-09
            # refuses to send such a frame to a vision model: a description of a
            # cropped screen would be reported as a description of the target.
            "clipped": frame.clipped,
            "validation": frame.verdict,
            "reason_code": frame.reason_code,
        }

    def _prune_observation_frames(self, directory: Path) -> None:
        """Keep the newest observation frames; disk use is bounded, not hopeful."""
        try:
            files = sorted(
                (path for path in directory.glob("*.png") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
            )
        except OSError:
            return
        for path in files[: max(0, len(files) - self.max_observation_frames)]:
            with contextlib.suppress(OSError):
                path.unlink()
            # The paint side car goes with its frame; the sweep below catches
            # one whose frame was already gone (a crash mid-frame).
            with contextlib.suppress(OSError):
                Path(str(path) + THUMB_SUFFIX).unlink()
        sweep_orphan_thumbnails(directory)

    # -- gameplay actions, objectives and verification ----------------------

    def _capture_capabilities(self) -> list[dict[str, Any]]:
        """Every wired capture backend and what it DECLARES it can see.

        A machine with only the display-crop source reports that it requires the
        foreground; a machine with the window-surface source reports that it can
        see a covered window. Both are facts about this wiring, not guesses.
        """
        if self._capture_router is not None:
            return self._capture_router.capabilities()
        backend = DisplayCropBackend(self.capture)
        available, reason = backend.available()
        return [
            {
                **backend.capabilities().to_dict(),
                "available": available,
                "unavailable_reason": reason,
            }
        ]

    def capabilities(self) -> dict[str, Any]:
        """What THIS runtime can actually do, per capability, with reasons.

        Availability is a property of the machine and the wiring, not of the
        request: a manager built without an injector reports no action path, and
        one built without a probe reports no objective verification, rather than
        claiming either because the class exists.
        """
        input_ready = self.action_runner is not None
        kinds = self.objectives.available_kinds()
        verifier_ready = bool(kinds)
        return {
            "window_discovery": {
                "available": bool(self.capture_supported),
                "reason": "" if self.capture_supported else "Window probing is unavailable on this host.",
            },
            "observation": {
                **self.observer.capability(),
                # The observer's seams are always callable (a missing host seam is
                # a stub that refuses), so its own row alone would claim capture
                # on a host where the manager reports none. Observation cannot be
                # available without capture.
                "available": bool(
                    self.capture_supported and self.observer.capability()["available"]
                ),
                "reason": ""
                if self.capture_supported
                else "Screen capture is unavailable on this host, so no frame can be observed.",
                "state": self.observer.status()["state"],
            },
            "capture": {
                "available": bool(self.capture_supported),
                "reason": "" if self.capture_supported else "Screen capture is unavailable on this host.",
                # One row per wired backend, with its DECLARED capability. A
                # caller can see why a background target may or may not be
                # captured instead of reading a single boolean.
                "backends": self._capture_capabilities(),
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
            "game_control": {
                "available": input_ready,
                "reason": (
                    "Bounded key/mouse operations run through the session's InputGuard, "
                    "behind the same Guardian gate as the plan/execute path."
                    if input_ready
                    else "No gameplay injector is wired; no control action can be sent."
                ),
                "kinds": sorted(CONTROL_KINDS),
                "policy": self.control_policy.to_dict(),
                "target_verified_before_dispatch": True,
                # The phase's central honesty row: an input operation completing
                # is not the game reacting. NF-21 owns effect verification.
                "effect_verified": False,
                "effect_note": (
                    "NF-19 sends wrapped input and reports what the input interface "
                    "did; it never observes or claims the game's reaction."
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
            "outcome_verification": {
                # The verifier itself is always present (it is pure logic over a
                # reading); what varies is whether any observable is DECLARED. A
                # build with no rule and no observable-declaring knowledge reports
                # NOT_VERIFIABLE per step instead of a result.
                "available": True,
                "reason": "",
                "rules": len(self.verification.rules.all()),
                "declared_observables": sum(
                    len(rule.requirements) for rule in self.verification.rules.all()
                ),
                "fixture_only": bool(self.verification.rules.all())
                and all(rule.fixture_only for rule in self.verification.rules.all()),
                "policy": self.verification_policy.to_dict(),
                "repeats_actions": False,
                "note": (
                    "An outcome needs a fresh observation. A session with no validated "
                    "frame reports OUTCOME_UNKNOWN, never a result."
                ),
            },
            "objective_verification": {
                "available": verifier_ready,
                "kinds": self.objectives.availability(),
                "ready_kinds": kinds,
                "reason": (
                    "A verdict is read from evidence the input path cannot write to: the "
                    f"target's own state ({', '.join(kinds)})."
                    if verifier_ready
                    else (
                        "No objective verifier is ready on this host; every objective is "
                        "reported UNSUPPORTED. A window probe and/or a declared artifact "
                        "root is required."
                    )
                ),
            },
            "decision_pipeline": dict(
                self.decision_capability()
                if self.decision_capability is not None
                else {
                    "available": False,
                    "reason": (
                        "No planner chooses actions for a session; a caller proposes them "
                        "explicitly. Perception does not silently become a policy."
                    ),
                }
            ),
            "game_launch": self._launch_capability(),
            "persistence": {
                "available": self.store is not None,
                "reason": "" if self.store is not None else "No state store is wired.",
            },
            "action_policy": self.action_policy.to_dict(),
        }

    def _launch_capability(self) -> dict[str, Any]:
        """Whether this host can launch a game, and through what.

        Launching is coordinated by the JARVIS desktop runner (a Steam deep link
        or a verified executable) and verified against a process/window. It is
        never part of a session, and it never installs anything. A host without
        the service reports the missing seam rather than a missing feature.
        """
        launcher = self.game_launcher
        if launcher is None:
            return {
                "available": False,
                "state": "not_wired",
                "reason": (
                    "No game launch service is wired on this host; a target window has to "
                    "be opened by the operator."
                ),
            }
        return {
            "available": True,
            "state": "available",
            "reason": (
                "Game discovery and launch run through the JARVIS desktop runner: a Steam "
                "deep link or a verified executable, checked against a real process and "
                "window. Sessions never launch or install anything."
            ),
            "coordinates_sessions": False,
            "installs": False,
        }

    def _compatibility_log(self) -> CompatibilityLog | None:
        """The embedding compatibility history, as a READ-ONLY reader.

        The history belongs to whoever attempted an embed; the desktop host is
        the writer. This reads the same snapshot (``novaforge_embedding_compat``
        under the state root) so the ladder can report what embedding actually
        did, without a second compatibility store and without writing anything.
        Built per call: the host may record an attempt while this process runs.
        """
        try:
            return CompatibilityLog(self.store)
        except Exception:  # noqa: BLE001 - an unreadable history is a gap, not an error
            return None

    async def observation_ladder(self, title: str = "") -> dict[str, Any]:
        """How far a target has got between "installed" and an accepted observation.

        Ten separate capabilities, and every row is decided from ITS OWN
        evidence: a title that is discovered and launchable reports
        ``process_started`` from a process probe, never because the stage before
        it was true. ``held_at`` names the first stage on the OBSERVATION path
        that is not true — the one an operator can act on.

        Native embedding is MEASURED here, never required. Its two rows come from
        the host's compatibility history and are excluded from ``held_at``:
        external capture is a supported observation path on its own, so a refused
        or never-attempted embed is not where observation is held up. Nothing in
        this report claims an embed happened because a window existed, or an
        observation happened because a frame was written.
        """
        status = self.observer.status()
        target = dict(status.get("target") or {})
        want = str(title or target.get("title") or "").strip()
        stages: list[dict[str, Any]] = []

        def add(stage: str, state: str, detail: str, **evidence: Any) -> None:
            stages.append(
                {"stage": stage, "state": state, "detail": detail, "evidence": evidence}
            )

        # 1-2: what this machine has, read from the catalog that already knows.
        launcher = self.game_launcher
        resolve = getattr(getattr(launcher, "catalog", None), "resolve", None)
        row: dict[str, Any] = {}
        if not callable(resolve):
            add(
                "discovered",
                "not_wired",
                "No game catalog is wired on this host, so this build cannot name the "
                "titles installed here; a window can still be observed by title.",
                title=want,
            )
            add(
                "launchable",
                "not_wired",
                "The catalog is what knows how a title would start, and it is not wired.",
            )
        elif not want:
            add(
                "discovered",
                "unknown",
                "Nothing to resolve: no title was requested and no observation is bound.",
            )
            add("launchable", "unknown", "Nothing to resolve.")
        else:
            resolution: dict[str, Any] = {}
            try:
                resolution = dict(await resolve(want) or {})
            except Exception as exc:  # noqa: BLE001 - an unreadable catalog is a gap
                add(
                    "discovered",
                    "unknown",
                    f"The catalog could not be read: {type(exc).__name__}: {exc}.",
                    title=want,
                )
                add("launchable", "unknown", "The catalog could not be read.")
            else:
                row = dict(resolution.get("entry") or {})
                artifact = dict(row.get("artifact") or {})
                # The RESOLUTION's status is authoritative: an ambiguous or a
                # missing title answers with no entry at all.
                state = str(resolution.get("status") or row.get("status") or "")
                candidates = list(resolution.get("candidates") or row.get("candidates") or [])
                found = state == AMBIGUOUS_MATCH or (
                    bool(row.get("title")) and state != NOT_FOUND_ON_INTERNAL_STORAGE
                )
                discovered_detail = (
                    "More than one installed edition matches this name, so no single "
                    "edition is reported."
                    if state == AMBIGUOUS_MATCH
                    else str(row.get("reason") or "")
                )
                if not discovered_detail:
                    discovered_detail = (
                        f"The catalog resolved {want!r}."
                        if found
                        else f"No installed title matches {want!r}."
                    )
                add(
                    "discovered",
                    "true" if found else "false",
                    discovered_detail,
                    status=state,
                    title=str(row.get("title") or want),
                    key=str(artifact.get("key") or ""),
                    source=str(artifact.get("source") or ""),
                    candidates=candidates,
                )
                launchable = artifact.get("launchable")
                if state == AMBIGUOUS_MATCH:
                    # Two installed editions matched: an edition is never guessed,
                    # so this is not launchable AND not a gap — it is an answer.
                    add(
                        "launchable",
                        "false",
                        "More than one installed edition matches this name; an edition "
                        "is never guessed.",
                        candidates=candidates,
                    )
                elif launchable is None:
                    add(
                        "launchable",
                        "unknown",
                        f"The catalog reports {state or 'no status'} without a launchable "
                        "artifact to decide from.",
                        status=state,
                    )
                else:
                    detail = str(row.get("reason") or "")
                    if not detail:
                        kind = str(artifact.get("launch_kind") or "")
                        detail = (
                            f"Launchable through {kind}."
                            if launchable
                            else "The catalog has no launch path for this title on this "
                            "host."
                        )
                    add(
                        "launchable",
                        "true" if launchable else "false",
                        detail,
                        kind=str(artifact.get("launch_kind") or ""),
                        launch_target=str(artifact.get("launch_target") or ""),
                        executable_exists=bool(artifact.get("executable_exists")),
                    )

        # 3: a process, from whichever source actually saw one. A bound
        # observation is one such source — it holds a pid and a start time.
        owned: Any | None = None
        owned_read = getattr(launcher, "owned", None)
        if callable(owned_read):
            try:
                owned = owned_read()
            except Exception:  # noqa: BLE001 - an unreadable record is a gap
                owned = None
        owner_matches = bool(owned is not None and want) and (
            want.lower() in str(getattr(owned, "name", "")).lower()
            or str(getattr(owned, "name", "")).lower() in want.lower()
        )
        running = dict(row.get("running") or {})
        target_pid = int(target.get("pid") or 0)
        if target_pid:
            add(
                "process_started",
                "true",
                f"Observation is bound to pid {target_pid} "
                f"({target.get('process') or 'process'}); a bound target is a running "
                "process by construction.",
                pid=target_pid,
                start_time=str(target.get("start_time") or ""),
                source="observation",
            )
        elif running.get("pid"):
            add(
                "process_started",
                "true",
                "The catalog's process probe found this title running.",
                pid=int(running.get("pid") or 0),
                process=str(running.get("name") or ""),
                start_time=str(running.get("start_time") or ""),
                window=str(running.get("window") or ""),
                source="catalog_process_probe",
            )
        elif owner_matches and int(getattr(owned, "pid", 0) or 0):
            add(
                "process_started",
                "true",
                "A launch this build made is still running.",
                pid=int(getattr(owned, "pid", 0) or 0),
                start_time=str(getattr(owned, "start_time", "") or ""),
                source="launch_record",
            )
        elif row:
            add(
                "process_started",
                "false",
                "The catalog's process probe found no running process for this title.",
                status=str(row.get("status") or ""),
            )
        else:
            add(
                "process_started",
                "unknown",
                "Nothing has probed for this title's process yet.",
            )

        # 4: a window, from the observation's own binding or a verified launch.
        handle = int(target.get("handle") or 0)
        if handle and target_pid:
            add(
                "window_identified",
                "true",
                "Observation is bound to a window and its process.",
                handle=handle,
                title=str(target.get("title") or ""),
                pid=target_pid,
                identity=str(target.get("identity") or ""),
            )
        elif owner_matches and bool(getattr(owned, "verified", False)):
            add(
                "window_identified",
                "true",
                "The launch verification reported the window it found.",
                window=str(getattr(owned, "window", "") or ""),
                pid=int(getattr(owned, "pid", 0) or 0),
            )
        elif int(running.get("pid") or 0) and str(running.get("window") or ""):
            add(
                "window_identified",
                "true",
                "The process probe reported a window for this title's process.",
                window=str(running.get("window") or ""),
                pid=int(running.get("pid") or 0),
                source="catalog_process_probe",
            )
        else:
            add(
                "window_identified",
                "unknown",
                "No window has been identified yet; the probe runs when an observation "
                "binds a title.",
            )

        # 5-6: embedding, measured from the host's own history (read-only).
        log = self._compatibility_log()
        record: Any | None = None
        if log is not None and want:
            try:
                record = log.latest(want)
                if record is None:
                    # The host records the name it was handed, which is often the
                    # window title: a containment match is the honest fallback.
                    for candidate in log.all():
                        game = str(getattr(candidate, "game", "") or "").lower()
                        if game and (want.lower() in game or game in want.lower()):
                            record = candidate
                            break
            except Exception:  # noqa: BLE001 - an unreadable history is a gap
                record = None
        if record is None:
            checked = len(log.all()) if log is not None else 0
            add(
                "embedding_compatible",
                "unknown",
                "No compatibility record for this title yet; the desktop host writes "
                "one when it checks a window. Observation does not need it.",
                records=checked,
            )
            add(
                "embedded",
                "false",
                "Nothing has attempted a native embed for this title; external capture "
                "is unaffected.",
            )
        else:
            data = dict(record.to_dict())
            classification = str(getattr(record, "classification", "") or "")
            if classification in (
                str(CompatibilityClass.SUPPORTED),
                str(CompatibilityClass.SUPPORTED_WITH_LIMITATIONS),
            ):
                compatible = "true"
            elif classification == str(CompatibilityClass.UNSUPPORTED):
                compatible = "false"
            else:
                compatible = "unknown"
            limitations = [str(item) for item in (data.get("limitations") or [])]
            add(
                "embedding_compatible",
                compatible,
                str(data.get("reason") or "")
                or f"The last compatibility check classified this window "
                f"{classification or 'UNKNOWN'}.",
                classification=classification,
                reason_code=str(data.get("reason_code") or ""),
                window_mode=str(data.get("window_mode") or ""),
                checked_at=str(data.get("checked_at") or ""),
                limitations=limitations,
            )
            embedding = str(getattr(record, "embedding", "") or "")
            embedded = embedding == EMBEDDING_EMBEDDED
            add(
                "embedded",
                "true" if embedded else "false",
                (
                    "The last attempt reported the window embedded in the host."
                    if embedded
                    else f"The last attempt's embedding state is "
                    f"{embedding or 'unknown'}; observation never depends on it."
                ),
                embedding=embedding,
                capture=str(data.get("capture") or ""),
            )

        # 7: which backend would serve (and did, if anything has been attempted).
        declared = self._capture_capabilities()
        names = [str(item.get("backend") or "") for item in declared]
        # A backend that declares itself UNAVAILABLE cannot be selected: reporting
        # that as "no frame yet" would read as "untested" on a host where capture
        # is impossible.
        usable = [item for item in declared if item.get("available") is not False]
        last = dict(status.get("last_observation") or {})
        last_verdict = dict(last.get("validation") or {})
        # A REFUSED read still names the backend that served it (the verdict
        # carries it), so a rejected frame does not read as "no backend chose".
        last_backend = str(
            status.get("backend")
            or last.get("backend")
            or last_verdict.get("backend")
            or ""
        )
        if not declared or not usable:
            reason = ""
            if declared:
                reason = str(declared[0].get("unavailable_reason") or "")
            add(
                "backend_selected",
                "not_wired",
                reason or "No capture backend on this host reports itself available.",
                declared=names,
            )
        elif last_backend:
            add(
                "backend_selected",
                "true",
                f"Backend {last_backend!r} served the last attempt.",
                backend=last_backend,
                source=str(last.get("source") or ""),
                declared=names,
            )
        else:
            add(
                "backend_selected",
                "unknown",
                "No frame has been attempted yet; the backend is chosen from the "
                "target's state at that moment.",
                declared=names,
            )

        # 8: a frame, from the observation's own success record.
        success = dict(status.get("last_success") or {})
        if success:
            add(
                "frame_acquired",
                "true",
                "A frame was written and recorded.",
                width=success.get("width"),
                height=success.get("height"),
                backend=str(success.get("backend") or ""),
                capture_ms=success.get("capture_ms"),
                source=str(success.get("source") or ""),
                at=str(success.get("at") or ""),
            )
        elif last:
            add(
                "frame_acquired",
                "false",
                str(last.get("reason") or "The last attempt produced no frame."),
                reason_code=str(last.get("reason_code") or ""),
                status=str(last.get("status") or ""),
            )
        else:
            add("frame_acquired", "unknown", "No frame has been attempted yet.")

        # 9: validated — the verdict the observation service itself recorded.
        validation = last_verdict
        if success:
            # The evidence for a validated stage is the verdict of the VALIDATED
            # record. When the newest record is a later refusal, its content is
            # evidence for that refusal, not for the success this row reports.
            evidence: dict[str, Any] = {}
            if str(last.get("status") or "") == "observed":
                evidence = {
                    "content": str(validation.get("content") or ""),
                    "limitations": [
                        str(item) for item in (validation.get("limitations") or [])
                    ],
                }
            add(
                "frame_validated",
                "true",
                "The recorded frame passed the observation service's identity, "
                "freshness and content checks.",
                **evidence,
            )
        elif validation:
            add(
                "frame_validated",
                "false",
                str(validation.get("reason") or "The last frame was judged invalid."),
                reason_code=str(last.get("reason_code") or ""),
                content=str(validation.get("content") or ""),
            )
        elif last:
            add(
                "frame_validated",
                "false",
                "The last attempt produced no frame to validate.",
            )
        else:
            add("frame_validated", "unknown", "Nothing has been validated yet.")

        # 10: accepted — counted by the observation service, not inferred.
        counts = dict(status.get("counts") or {})
        observed = int(counts.get("observed") or 0)
        if observed:
            add(
                "observation_accepted",
                "true",
                f"The observation service recorded {observed} validated frame(s); the "
                f"newest is observation {success.get('observation_id') or 'recorded'}.",
                observed=observed,
                observation_id=str(success.get("observation_id") or ""),
                seq=success.get("seq"),
            )
        else:
            add(
                "observation_accepted",
                "false",
                "No validated frame has been accepted into an observation yet.",
                observed=0,
                rejected=int(counts.get("rejected") or 0),
                unavailable=int(counts.get("unavailable") or 0),
            )

        by_name = {entry["stage"]: entry for entry in stages}
        held = next(
            (
                stage
                for stage in OBSERVATION_LADDER_STAGES
                if stage not in EMBEDDING_LADDER_STAGES
                and str(by_name.get(stage, {}).get("state") or "") != "true"
            ),
            "",
        )
        return {
            "target": {"title": want, **{k: v for k, v in target.items() if k != "title"}},
            "stages": stages,
            "stage_order": list(OBSERVATION_LADDER_STAGES),
            "embedding_stages": list(EMBEDDING_LADDER_STAGES),
            "held_at": held,
            "accepted": str(by_name.get("observation_accepted", {}).get("state")) == "true",
            "note": (
                "Each row is decided from its own evidence: an embed is not a capture, "
                "a frame is not an observation and a launched process is not an "
                "identified window. Native embedding is measured, never required — "
                "external capture is a supported observation path, so embedding rows "
                "are excluded from held_at."
            ),
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
        verifier = self.objectives.verifier_for(definition)
        supported = verifier is not None
        kinds = self.objectives.available_kinds()
        # The kind list is a per-session fact once an objective is attached: a
        # restart must report what THIS session could check, not what the reading
        # process happens to have wired.
        record.capabilities["objective_verifier"] = supported
        record.capabilities["objective_kinds"] = kinds
        self._event(
            record,
            "objective.configured",
            source="objective",
            payload={
                **definition.to_dict(),
                "supported": supported,
                "verifier": getattr(verifier, "verifier_id", ""),
                "verifier_version": getattr(verifier, "version", 0),
                "digest": definition.digest(),
            },
            evidence=[session_ref(record.session_id)],
        )
        self._persist()
        return {
            "session_id": record.session_id,
            "objective": definition.to_dict(),
            "supported": supported,
            "verifier": getattr(verifier, "verifier_id", ""),
            "verifier_version": getattr(verifier, "version", 0),
            "digest": definition.digest(),
            "reason": (
                f"{getattr(verifier, 'verifier_id', 'A verifier')} will read the declared "
                "evidence for this objective, independently of the action record."
                if supported
                else (
                    "No ready verifier can check this objective on this host; verification "
                    "will report UNSUPPORTED. Ready kinds: "
                    + (", ".join(kinds) or "none")
                    + "."
                )
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

    # -- NF-19: the bounded game-control interface ---------------------------

    async def _control_target(self, session_id: str) -> Any | None:
        """Read the pinned window's LIVE geometry for one control action."""
        record = self._sessions.get(session_id)
        if record is None:
            return None
        return await self._probe_target(record)

    def _control_gate(self, session_id: str) -> Mapping[str, Any]:
        """The Guardian's own decision, asked at the moment of dispatch.

        ``input_safety_decision`` already refuses a closed, paused, unvalidated,
        unwired or over-budget session. This adds only what it cannot see: a
        session id that names no record at all.
        """
        record = self._sessions.get(session_id)
        if record is None:
            return {
                "allow": False,
                "state": "no_session",
                "reason": f"No NovaForge session is named {session_id!r}.",
            }
        return input_safety_decision(record)

    async def _publish_control(self, event_type: str, payload: dict[str, Any]) -> None:
        """Announce a control edge on the ONE event bus (best effort)."""
        await self._emit(event_type, payload)

    async def _publish_plan(self, event_type: str, payload: dict[str, Any]) -> None:
        """Announce one planning edge on the bus AND the session's timeline.

        The timeline is the audit trail a person can read back: the plan's status,
        code and reason — never the action's input beyond its kind/ids — so the
        decisions and the inputs they led to sit in one sequence. A broken
        recorder can never fail planning.
        """
        await self._emit(event_type, payload)
        record = self._sessions.get(str(payload.get("session_id") or ""))
        if record is None:
            return
        try:
            status = str(payload.get("status") or "")
            self._event(
                record,
                event_type,
                source="planner",
                severity=(
                    "info"
                    if status in {"planned", "awaiting_approval", "executed"}
                    else "warning"
                ),
                payload=dict(payload),
                evidence=[session_ref(record.session_id)],
            )
            self._persist()
        except Exception:  # noqa: BLE001 - an audit write is not the decision
            return

    async def _publish_verification(self, event_type: str, payload: dict[str, Any]) -> None:
        """Announce one outcome edge on the bus AND the session's timeline.

        The BUS gets the whole verdict; the DURABLE timeline gets a bounded
        summary of it, because a timeline payload is capped and an overflowing
        one is truncated rather than stored — and a verdict a person cannot read
        back is not a record. The summary carries the classification, its code
        and reason, the per-action results with their confidence and recovery
        decision, and the evidence references; never the raw reading text (the
        state it came from is already in the timeline). A broken recorder can
        never fail a verification.
        """
        await self._emit(event_type, payload)
        record = self._sessions.get(str(payload.get("session_id") or ""))
        if record is None:
            return
        try:
            status = str(payload.get("status") or "")
            self._event(
                record,
                event_type,
                source="verifier",
                severity=(
                    "info"
                    if status in {"verified", ""} and event_type.endswith(".verified")
                    else "warning"
                ),
                payload=(
                    self._verification_summary(payload)
                    if event_type.endswith(".verified")
                    else dict(payload)
                ),
                evidence=[session_ref(record.session_id)],
            )
            self._persist()
        except Exception:  # noqa: BLE001 - an audit write is not the verdict
            return

    @staticmethod
    def _verification_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
        """A bounded, ordered summary of one verification verdict.

        Ordered so the verdict reads first; sized so every field survives the
        timeline's own payload bound instead of being silently cut.
        """
        raw_outcomes = payload.get("outcomes")
        rows = (
            [row for row in raw_outcomes if isinstance(row, Mapping)]
            if isinstance(raw_outcomes, (list, tuple))
            else []
        )
        counts = payload.get("counts")
        return {
            "session_id": str(payload.get("session_id") or ""),
            "plan_id": str(payload.get("plan_id") or ""),
            "verification_id": str(payload.get("verification_id") or ""),
            "status": str(payload.get("status") or ""),
            "code": str(payload.get("code") or ""),
            "reason": str(payload.get("reason") or ""),
            "effect_verified": bool(payload.get("effect_verified")),
            "steps_verified": payload.get("steps_verified"),
            "steps_skipped": payload.get("steps_skipped"),
            "steps_capped": payload.get("steps_capped"),
            "counts": dict(counts) if isinstance(counts, Mapping) else {},
            "outcomes": [
                {
                    "outcome_id": str(row.get("outcome_id") or ""),
                    "action_id": str(row.get("action_id") or ""),
                    "status": str(row.get("status") or ""),
                    "code": str(row.get("code") or ""),
                    "confidence": row.get("confidence"),
                    "effect_verified": bool(row.get("effect_verified")),
                    "recovery": str(row.get("recovery") or ""),
                }
                for row in rows[:8]
            ],
            "evidence_refs": [str(item) for item in (payload.get("evidence_refs") or [])][:8],
            "limitations": [str(item) for item in (payload.get("limitations") or [])][:2],
        }

    def control_status(self, session_id: str) -> dict[str, Any]:
        """The control interface's state for one session, with the gate's reason."""
        record = self._require(session_id)
        data = self.control.status()
        # One service serves every session: never attribute another session's
        # in-flight action, result or history to this one.
        active = data.get("active") or {}
        if active.get("session_id") not in (None, "", session_id):
            data["active"] = None
        last = data.get("last_result") or {}
        if last.get("session_id") not in (None, "", session_id):
            data["last_result"] = None
        data["history"] = [
            row
            for row in self.control.history(10)
            if row.get("session_id") in ("", session_id)
        ]
        data["session"] = {
            "session_id": record.session_id,
            "state": record.state.value,
            "target": record.target.to_dict() if record.target is not None else None,
            "gate": dict(input_safety_decision(record)),
            "actions": {
                "attempted": record.actions_attempted,
                "executed": record.actions_executed,
                "remaining": (
                    None
                    if record.mission.max_actions is None
                    else max(0, record.mission.max_actions - record.actions_executed)
                ),
            },
        }
        return data

    async def _control_target_for(self, record: SessionRecord) -> ControlTarget:
        """Read the LIVE identity of a session's pinned window for one action."""
        target = record.target
        if target is None:
            raise MissionValidationError(
                ["The session has no validated target window."]
            )
        identity: Mapping[str, Any] | None = None
        if self.process_identity is not None and target.pid:
            try:
                identity = await self.process_identity(target.pid)
            except Exception:  # noqa: BLE001 - an unreadable process is not a crash
                identity = None
        return ControlTarget.from_window(
            target,
            record.session_id,
            process=str((identity or {}).get("name", "") or ""),
            start_time=str((identity or {}).get("start_time", "") or ""),
        )

    @staticmethod
    def _control_refusal(session_id: str, code: str, reason: str) -> dict[str, Any]:
        """A refusal envelope in the same shape a dispatched result uses."""
        return {
            "session_id": session_id,
            "status": "rejected",
            "code": code,
            "ok": False,
            "reason": reason,
            "result": None,
            "effect_verified": False,
        }

    async def control_execute(
        self, session_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Run ONE typed, bounded input action against the session's target.

        Every refusal is a RESULT with a code, not an exception: the caller has
        to be able to tell "the target was gone" from "the key is not allowed"
        from "another action was in flight". Only an unparseable body is a
        validation error, because there is no action to report on.

        The manager's own lock is taken for the duration, so a control action and
        a digest-bound action can never inject concurrently through one guard.
        """
        record = self._require(session_id)
        if record.target is None:
            return self._control_refusal(
                session_id,
                "unvalidated",
                "The session has no validated target window, so there is nothing "
                "to address an input to.",
            )
        control_target = await self._control_target_for(record)
        try:
            action = ControlAction.from_payload(
                payload, session_id=session_id, target=control_target
            )
        except ValueError as exc:
            raise MissionValidationError([str(exc)]) from exc
        return await self._run_control_action(record, action)

    async def submit_control_action(
        self, session_id: str, action: ControlAction
    ) -> dict[str, Any]:
        """Submit ONE already-validated plan step through the SAME NF-19 path.

        The action was built from the target the plan was made against. This
        re-reads the live identity first: a plan whose window was replaced (a
        reused pid, a new handle) is refused rather than re-aimed, and a refusal
        carries no dispatch. The plan's own action id is preserved so its record
        names exactly one operation.
        """
        record = self._require(session_id)
        if record.target is None:
            return self._control_refusal(
                session_id,
                "unvalidated",
                "The session has no validated target window; the planned step was not sent.",
            )
        pinned = action.target
        if action.session_id != session_id or pinned.session_id != session_id:
            return self._control_refusal(
                session_id,
                "wrong_session",
                "The planned step belongs to a different session; it was not sent.",
            )
        live = await self._control_target_for(record)
        changed: list[str] = []
        if pinned.handle and live.handle and pinned.handle != live.handle:
            changed.append(f"handle 0x{pinned.handle:x} -> 0x{live.handle:x}")
        if pinned.pid and live.pid and pinned.pid != live.pid:
            changed.append(f"pid {pinned.pid} -> {live.pid}")
        if pinned.start_time and live.start_time and pinned.start_time != live.start_time:
            changed.append("process start time changed")
        if changed:
            return self._control_refusal(
                session_id,
                "target_changed",
                "The plan's target is no longer the live window ("
                + "; ".join(changed)
                + "); the planned step was not sent.",
            )
        # Rebind to the live identity so every check inside the control service
        # compares against what is on screen now, not what the plan remembered.
        # ``replace`` keeps the planned action id, so the record names one step.
        return await self._run_control_action(record, replace(action, target=live))

    async def _run_control_action(
        self, record: SessionRecord, action: ControlAction
    ) -> dict[str, Any]:
        """Execute one action under the manager lock and record its outcome."""
        session_id = record.session_id
        async with self._lock_or_new():
            result = await self.control.execute(action)
            if result.code in {"target_lost", "target_changed"}:
                # The identity this session was pinned to is gone: hold the
                # session through the same path a tick uses, so the next loop
                # iteration cannot carry on against the wrong window.
                await self._pause_for(record, result.code)
            data = result.to_dict()
            # The control path consumes the SAME per-session action budget the
            # Guardian gates on: a direct input that sent something is an action
            # this session took, or the budget would be a bound with a door.
            if result.dispatched_at:
                record.actions_attempted += 1
            if result.ok:
                record.actions_executed += 1
            record.record_action(data)
            self._event(
                record,
                f"control.{data['status']}",
                source="control",
                severity=(
                    "info"
                    if result.ok
                    else "warning"
                    if data["status"] in {"rejected", "cancelled", "timed_out"}
                    else "error"
                ),
                payload={
                    "action_id": data["action_id"],
                    "kind": data["kind"],
                    "status": data["status"],
                    "code": data["code"],
                    "reason": data["reason"],
                    "dispatched": bool(data["dispatched_at"]),
                    "cleanup": data["cleanup"],
                    "effect": None,
                },
                evidence=[session_ref(record.session_id)],
            )
            self._persist()
            return {
                "session_id": session_id,
                "status": data["status"],
                "code": data["code"],
                "ok": data["ok"],
                "reason": data["reason"],
                "result": data,
                "remaining_actions": (
                    None
                    if record.mission.max_actions is None
                    else max(0, record.mission.max_actions - record.actions_executed)
                ),
                "effect_verified": False,
            }

    async def cancel_control_action(self, session_id: str) -> dict[str, Any]:
        """Ask the in-flight control action to stop at its next checkpoint.

        Scoped to the session the request names: the service is shared by every
        session, and a stale id must not stop the live session's action.
        """
        self._require(session_id)
        outcome = await self.control.cancel("api", session_id=session_id)
        return {"session_id": session_id, **outcome}

    async def release_control_inputs(self, session_id: str) -> dict[str, Any]:
        """Release every input this interface still holds; idempotent.

        The guard is process-wide, so only the CURRENT session may ask: a stale
        session's release must not free a key the live session is holding.
        """
        record = self._require(session_id)
        if self._current_id and self._current_id != session_id:
            return {
                "session_id": session_id,
                "cleanup": {
                    "ok": False,
                    "released": 0,
                    "failed": [],
                    "held_after": len(self.control.held()),
                    "reason": "wrong_session",
                    "note": (
                        "Another NovaForge session is current; releasing here could free "
                        "input it is holding. Nothing was released."
                    ),
                },
                "held": self.control.held(),
            }
        cleanup = await self.control.release("api")
        self._event(
            record,
            "control.released",
            source="control",
            severity="info" if cleanup.get("ok") else "error",
            payload=dict(cleanup),
            evidence=[session_ref(record.session_id)],
        )
        self._persist()
        return {"session_id": session_id, "cleanup": cleanup, "held": self.control.held()}

    def report_control_event(
        self,
        session_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        severity: str = "info",
        evidence: list[dict[str, Any]] | None = None,
    ) -> None:
        """Record an evaluation-level control event on the session's timeline.

        One door for the caller that approved a plan: the approval, the plan and
        each refusal become timeline events beside the actions they govern, so
        the evidence reads as one sequence instead of a second log.
        """
        record = self._require(session_id)
        self._event(
            record,
            event_type,
            source="control",
            severity=severity,
            payload=dict(payload or {}),
            evidence=[session_ref(record.session_id), *(evidence or [])],
        )
        self._persist()

    # -- NF-20: evidence-grounded planning ---------------------------------

    @staticmethod
    def _state_age_seconds(state: GameState | None) -> float | None:
        """How old a state reading is, or ``None`` when its stamp is unreadable.

        ``None`` is not "fresh": the planner refuses to plan on an age it cannot
        measure, exactly like the freshness gate in NF-16.
        """
        if state is None:
            return None
        stamp = str(state.timestamp or state.observation.at or "").strip()
        if not stamp:
            return None
        try:
            at = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        return max(0.0, (datetime.now(UTC) - at).total_seconds())

    async def _planning_context(
        self, session_id: str, refresh: bool
    ) -> PlanningContext:
        """Assemble ONE planning decision's evidence from this session's services.

        ``refresh`` is at most ONE bounded re-analysis, and only when the newest
        state is missing or older than the planner's freshness limit. It is not
        a capture: the state service reads the observer's newest VALIDATED frame,
        and a refused analysis simply leaves "no state", which the planner
        reports as insufficient information instead of guessing.
        """
        record = self._require(session_id)
        gate = self._control_gate(session_id)
        age = self._state_age_seconds(self.state.current)
        if refresh and gate.get("allow") and self.plan_policy.refresh_stale_state:
            if age is None or age > self.plan_policy.max_state_age_seconds:
                try:
                    await self.state.analyze()
                except Exception:  # noqa: BLE001 - a refusal is the answer
                    pass
        state = self.state.current
        game = ""
        if state is not None:
            game = str(state.game or state.observation.game or "")
        knowledge: tuple[KnowledgeRecord, ...] = ()
        if game:
            try:
                knowledge = tuple(await self.knowledge.records(game=game, limit=40))
            except Exception:  # noqa: BLE001 - unreadable memory is no memory
                knowledge = ()
        strategy = None
        try:
            # NF-26: the active strategy's bounded recommendation, or None. An
            # unreadable learning ledger is no strategy — never a broken plan.
            strategy = self.learning().hint()
        except Exception:  # noqa: BLE001 - learning is an input, not a dependency
            strategy = None
        target = (
            ControlTarget.from_window(record.target, session_id)
            if record.target is not None
            else None
        )
        return PlanningContext(
            session_id=session_id,
            game=game,
            target=target,
            state=state,
            objective_state=self.objective_state.current,
            knowledge=knowledge,
            gate=gate,
            budget_remaining=(
                None
                if record.mission.max_actions is None
                else max(0, record.mission.max_actions - record.actions_executed)
            ),
            state_age_seconds=self._state_age_seconds(state),
            control_policy=self.control_policy,
            actions_executed=record.actions_executed,
            strategy=strategy,
        )

    def planning_status(self, session_id: str) -> dict[str, Any]:
        """The NF-20 view for ONE session: rules, plans, bounds and the gate."""
        record = self._require(session_id)
        data = self.planner.status()
        current = self.planner.current_for(session_id)
        state = self.state.current
        data["current"] = current.to_dict() if current is not None else None
        data["history"] = self.planner.history(session_id, 10)
        data["session"] = {
            "session_id": session_id,
            "state": record.state.value,
            "gate": dict(input_safety_decision(record)),
            "actions": {
                "executed": record.actions_executed,
                "remaining": (
                    None
                    if record.mission.max_actions is None
                    else max(0, record.mission.max_actions - record.actions_executed)
                ),
            },
            "game": str(state.game or state.observation.game or "") if state else "",
            "state_id": state.state_id if state is not None else "",
            "state_age_seconds": self._state_age_seconds(state),
            "objective_id": (
                self.objective_state.current.objectives[0].objective_id
                if self.objective_state.current is not None
                and self.objective_state.current.objectives
                else ""
            ),
        }
        data["effect_verified"] = bool(
            current is not None and current.execution.get("effect_verified")
        )
        return data

    async def plan_actions(
        self, session_id: str, *, refresh: bool = True
    ) -> dict[str, Any]:
        """Build ONE bounded plan; nothing is sent by this call."""
        self._require(session_id)
        plan = await self.planner.plan(session_id, refresh=refresh)
        return {
            "session_id": session_id,
            "plan": plan.to_dict(),
            "effect_verified": False,
        }

    def plan_gate(self, plan_id: str) -> dict[str, Any]:
        """Read-only approval view of ONE stored plan, for the app's token gate.

        The approval inside a plan is a REQUEST FLAG; the application is what
        issues and consumes approval tokens, so it has to be able to ask whether
        a given plan is still awaiting the operator's approval without reaching
        into the planner itself. An unknown id answers `{}` — no gate — and the
        planner's own refusal for that id stays the answer.
        """
        plan = self.planner.get(plan_id.strip())
        if plan is None:
            return {}
        return {"status": plan.status.value, "approval": dict(plan.approval)}

    async def execute_plan(
        self, session_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Execute ONE approved plan, step by step, through NF-19."""
        self._require(session_id)
        plan_id = str(payload.get("plan_id") or "").strip()
        if not plan_id:
            raise MissionValidationError(["A plan_id is required."])
        approved = payload.get("approved") is True
        plan = await self.planner.execute(session_id, plan_id, approved=approved)
        return {
            "session_id": session_id,
            "plan": plan.to_dict(),
            "effect_verified": False,
        }

    async def cancel_plan(
        self, session_id: str, plan_id: str = ""
    ) -> dict[str, Any]:
        """Cancel the session's running or pending plan (idempotent)."""
        self._require(session_id)
        plan = await self.planner.cancel(session_id, plan_id.strip())
        stopped = plan is not None and (
            plan.status is PlanStatus.CANCELLED or plan.code == "cancel_requested"
        )
        return {
            "session_id": session_id,
            "cancelled": stopped,
            "plan": plan.to_dict() if plan is not None else None,
            "effect_verified": False,
        }

    async def replan_actions(
        self, session_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Regenerate one stopped plan from FRESH evidence, within its bound."""
        self._require(session_id)
        plan_id = str(payload.get("plan_id") or "").strip()
        if not plan_id:
            raise MissionValidationError(["A plan_id is required."])
        reason = str(payload.get("reason") or "operator").strip() or "operator"
        plan = await self.planner.replan(session_id, plan_id, reason=reason)
        return {
            "session_id": session_id,
            "plan": plan.to_dict(),
            "effect_verified": False,
        }

    # ── NF-21: outcome verification and bounded recovery ──────────────────

    def verification_status(self, session_id: str) -> dict[str, Any]:
        """The NF-21 view for ONE session: bounds, outcomes and recovery counts."""
        record = self._require(session_id)
        data = self.verification.status(session_id)
        current = self.planner.current_for(session_id)
        state = self.state.current
        data["session"] = {
            "session_id": session_id,
            "state": record.state.value,
            "gate": dict(input_safety_decision(record)),
            "game": str(state.game or state.observation.game or "") if state else "",
            "state_id": state.state_id if state is not None else "",
            "state_age_seconds": self._state_age_seconds(state),
            "observation_id": (
                state.observation.observation_id if state is not None else ""
            ),
            "objective_id": (
                self.objective_state.current.objectives[0].objective_id
                if self.objective_state.current is not None
                and self.objective_state.current.objectives
                else ""
            ),
        }
        data["plan"] = current.to_dict() if current is not None else None
        data["outcomes"] = self.verification.outcomes(session_id, 10)
        data["history"] = self.verification.history(session_id, 5)
        data["effect_verified"] = bool(
            current is not None and current.execution.get("effect_verified")
        )
        return data

    async def verify_outcomes(
        self, session_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Verify the dispatched steps of ONE plan against fresh observations.

        The newest plan is verified when the body names none. Every step that was
        dispatched gets a contract (a declared observable, or an explicit
        ``not_verifiable``), one bounded post-action reading decides it, and the
        outcomes are recorded back onto the plan. Nothing is sent by this call.
        """
        self._require(session_id)
        plan_id = str(payload.get("plan_id") or "").strip()
        if not plan_id:
            current = self.planner.current_for(session_id)
            plan_id = current.plan_id if current is not None else ""
        if not plan_id:
            raise MissionValidationError(["A plan_id is required to verify outcomes."])
        raw_steps = payload.get("max_steps")
        limit = int(raw_steps) if isinstance(raw_steps, (int, float)) else None
        result = await self.verification.verify(session_id, plan_id, max_steps=limit)
        return {
            "session_id": session_id,
            "plan_id": plan_id,
            "verification": result.to_dict(),
            "effect_verified": result.effect_verified,
        }

    async def recover_outcome(
        self, session_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Carry out the recorded recovery decision for ONE outcome.

        A side-effecting response (replan, pause, stop) needs ``approve: true``
        while the policy requires it; the refusal names the approval instead of
        performing anything. Recovery never dispatches input.
        """
        self._require(session_id)
        outcome_id = str(payload.get("outcome_id") or "").strip()
        if not outcome_id:
            raise MissionValidationError(["An outcome_id is required to recover."])
        return await self.verification.recover(
            session_id, outcome_id, approve=payload.get("approve") is True
        )

    # ── NF-22: ONE bounded autonomous gameplay session ─────────────────────
    # The supervisor is a supervisor: it owns no window, captures no frame and
    # injects no input. Every stage below travels the SAME session, control, plan
    # and verification services a spoken command uses, through this manager's own
    # methods — and the session's lifecycle stays this manager's, so ending a run
    # ends by the session's own ``stop`` (which releases held input). There is no
    # second session manager here, and no second dispatch path anywhere.
    def autonomy(self) -> AutonomousSessionController:
        """The ONE autonomous supervisor over this manager's sessions."""
        controller = self._autonomy
        if controller is None:
            controller = AutonomousSessionController(
                gateway=self,
                # The evidence comes from the real observation pipeline. The
                # simulated acceptance harness builds its own controller and says
                # ``simulated`` there; no run of THIS controller is simulated.
                evidence_source="live",
                resource_probe=self.autonomy_resources,
                publish=self._publish_autonomy,
                store=self.store,
            )
            self._autonomy = controller
        return controller

    # ── NF-25: ONE bounded real-game task over that supervisor ─────────────
    def tasks(self) -> TaskRunner:
        """The ONE task runner, bound to this manager's supervisor and readers.

        Built here rather than by the caller so every reader is the manager's own:
        the same sessions, the same state service, the same control policy and the
        same resource gate the rest of the build asks. A rebuilt manager builds a
        new runner instead of inheriting one that points at stale services.
        """
        runner = self._tasks
        if runner is None:
            runner = TaskRunner(
                AutonomyTaskGateway(
                    autonomy=self.autonomy(),
                    sessions=self.get,
                    target_title=lambda session_id: (
                        self._target_title(record)
                        if (record := self.get(session_id)) is not None
                        else ""
                    ),
                    read_state=self.read_state,
                    objective_picture=self.objective_picture,
                    # An unavailable control layer has NO performable action, which
                    # is different from "all kinds are supported".
                    allowed_actions=lambda: (
                        sorted(self.control.status().get("kinds") or [])
                        if self.control.available
                        else []
                    ),
                    resource_gate=lambda: (
                        self.autonomy_resources() if self.autonomy_resources is not None else {}
                    ),
                    # The control layer's own held-input list is the only honest
                    # answer to "was input released?": the supervisor's cancel
                    # answer carries no cleanup record.
                    held_inputs=lambda: self.control.held(),
                )
            )
            self._tasks = runner
        return runner

    # ── NF-26: ONE learning layer over that task layer ─────────────────────
    def learning(self) -> LearningService:
        """The ONE learning service, bound to this manager's own store.

        Built here rather than by the caller so it reads and writes the same
        snapshot store the rest of the build does. It has no window, no input
        path and no verifier: its whole input is NF-25's judged report, and its
        whole output is a bounded hint the planner may ignore.
        """
        service = self._learning
        if service is None:
            service = LearningService(
                store=self.store,
                require_approval=bool(self.plan_policy.require_approval),
            )
            self._learning = service
        return service

    def _autonomy_run_payload(self, session_id: str) -> dict[str, Any] | None:
        """NF-22's own run record, when it still holds one for this session.

        The task report carries the judged facts; the run payload carries the
        submitted steps and the observed transitions, which is what makes an
        experience record's provenance complete. An unreadable record is `None`,
        and the record then says those two facts were not measured.
        """
        record = self.autonomy().runs.get(session_id)
        if record is None:
            return None
        try:
            payload = record.to_dict()
        except Exception:  # noqa: BLE001 - an unreadable record is not an experience
            return None
        return dict(payload) if isinstance(payload, Mapping) else None

    async def run_task(
        self, spec: TaskSpec, session_id: str, *, approved: bool = False, note: str = ""
    ) -> dict[str, Any]:
        """Start and judge one bounded task, returning the task's own report.

        The approval is the OPERATOR's and is never supplied here: a task sends
        input, so a caller that has not been approved gets a report saying why
        nothing ran.

        A finished attempt is recorded as an NF-26 experience against the strategy
        that was in force when it started. Recording never changes the answer: the
        report is returned exactly as NF-25 judged it.
        """
        try:
            profile = self.learning().active_profile()
        except Exception:  # noqa: BLE001 - learning must never gate a task
            profile = None
        report = await self.tasks().run(spec, session_id, approved=approved, note=note)
        payload = dict(report.to_dict())
        try:
            self.learning().record_task_report(
                payload,
                run=self._autonomy_run_payload(session_id),
                strategy_id=profile.strategy_id if profile is not None else "baseline",
                strategy_version=profile.version if profile is not None else 0,
                # No profile argument: the record takes the state layer's own
                # profile id from the run, rather than passing the game's NAME as
                # if it were a profile identity.
            )
        except Exception:  # noqa: BLE001 - recording must not fail the task
            pass
        return payload

    async def run_experiment(
        self,
        experiment: StrategyExperiment,
        *,
        approved: bool = False,
        confidence_probe: Callable[[], float | None] | None = None,
    ) -> dict[str, Any]:
        """Compare a baseline and a candidate strategy inside an approved task.

        Both arms run through ``run_task`` — the same preflight gates, the same
        supervisor, the same judge — and differ only in the strategy hint the
        planner receives. The experiment's conditions must name the session it
        may use; an unknown one refuses like any other task.
        """
        session_id = str((experiment.conditions or {}).get("session_id") or "")

        async def arm_runner(profile: Any, spec: Any) -> dict[str, Any]:
            previous = self.learning().use_profile(profile)
            try:
                return await self.run_task(spec, session_id, approved=approved)
            finally:
                self.learning().restore_profile(previous)

        result = await run_experiment(
            experiment, arm_runner, approved=approved, confidence_probe=confidence_probe
        )
        return dict(result.to_dict())

    # ── the gateway the supervisor drives (its whole vocabulary) ──────────
    async def read_state(self) -> dict[str, Any]:
        """NF-16's reading of the newest validated frame — the supervisor's eyes."""
        outcome = await self.state.analyze()
        return outcome.to_dict()

    def objective_picture(self, session_id: str = "") -> dict[str, Any]:
        """NF-17's newest interpretation — the only authority on completion."""
        current = self.objective_state.current
        return current.to_dict() if current is not None else {}

    # ── the operator's surface ────────────────────────────────────────────
    def autonomy_status(self, session_id: str = "") -> dict[str, Any]:
        """The panel's view: the bounds, the runs, and the sessions they may drive."""
        controller = self.autonomy()
        status = controller.status(session_id)
        status["sessions"] = [
            {
                "session_id": record.session_id,
                "state": record.state.value,
                "target_validated": bool(record.target_validated),
                "title": self._target_title(record),
            }
            for record in self._sessions.values()
            if not session_id or record.session_id == session_id
        ]
        status["approval_required"] = bool(controller.policy.require_approval)
        return status

    def autonomy_trace(self, session_id: str = "") -> dict[str, Any]:
        """The complete orchestration history of the runs, newest last."""
        trace = self.autonomy().trace(session_id)
        trace["sessions"] = [
            {"session_id": record.session_id, "state": record.state.value}
            for record in self._sessions.values()
            if not session_id or record.session_id == session_id
        ]
        return trace

    async def start_autonomy(
        self,
        session_id: str,
        *,
        approved: bool = False,
        note: str = "",
        task: str = "",
        limits: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Begin ONE bounded autonomous run over a live session.

        The approval is the gate, and it is the OPERATOR's: this method never
        supplies one. A refused or impossible start comes back as a normal answer
        naming its code, so the panel can explain it instead of showing a fault.
        """
        controller = self.autonomy()
        parsed = self._autonomy_limits(limits)
        answer = await controller.start(
            session_id, approved=approved, note=note, task=task, limits=parsed
        )
        self._autonomy_event(
            session_id,
            "autonomy.started" if answer.get("started") else "autonomy.declined",
            {
                "run_id": str(answer.get("run_id") or ""),
                "started": bool(answer.get("started")),
                "code": str(answer.get("code") or ""),
                "reason": str(answer.get("reason") or ""),
                "objective_id": str(answer.get("objective_id") or ""),
            },
        )
        return answer

    async def pause_autonomy(self, session_id: str, *, reason: str = "") -> dict[str, Any]:
        answer = await self.autonomy().pause(session_id, reason=reason)
        self._autonomy_event(session_id, "autonomy.paused", {"reason": str(answer.get("reason") or reason)})
        return answer

    async def resume_autonomy(self, session_id: str) -> dict[str, Any]:
        answer = await self.autonomy().resume(session_id)
        self._autonomy_event(session_id, "autonomy.resumed", {"code": str(answer.get("code") or "")})
        return answer

    async def cancel_autonomy(self, session_id: str, *, reason: str = "") -> dict[str, Any]:
        """Stop a run before its next stage, then let it end cleanly."""
        answer = await self.autonomy().cancel(session_id, reason=reason)
        self._autonomy_event(
            session_id,
            "autonomy.cancelled",
            {"code": str(answer.get("code") or ""), "reason": str(answer.get("reason") or reason)},
        )
        return answer

    async def approve_autonomy_plan(
        self, session_id: str, *, approved: bool = False, note: str = ""
    ) -> dict[str, Any]:
        """Answer a run parked in WAITING_FOR_APPROVAL (the per-plan policy)."""
        answer = await self.autonomy().approve_plan(session_id, approved=approved, note=note)
        self._autonomy_event(
            session_id,
            "autonomy.plan_answered",
            {
                "approved": bool(answer.get("approved")),
                "code": str(answer.get("code") or ""),
                "note": str(note or ""),
            },
        )
        return answer

    async def shutdown_autonomy(self) -> dict[str, Any]:
        """Stop every autonomous run and release the sessions they were driving."""
        if self._autonomy is None:
            return {"stopped": [], "runs": 0}
        return await self._autonomy.shutdown()

    def _autonomy_limits(self, payload: Mapping[str, Any] | None) -> AutonomyLimits:
        """The run's bounds: the declared defaults, each one overridable downward.

        Every field is validated by :class:`AutonomyLimits` itself, so a caller
        that asks for a wider bound is refused rather than quietly clamped.
        """
        if not payload:
            return AutonomyLimits()
        known = {
            "max_cycles",
            "max_actions",
            "max_actions_per_plan",
            "session_seconds",
            "observation_timeout_seconds",
            "planning_timeout_seconds",
            "action_timeout_seconds",
            "verification_timeout_seconds",
            "observation_attempts",
            "recovery_attempts",
            "replans",
        }
        values = {key: payload[key] for key in known if key in payload}
        return AutonomyLimits(**values)

    @staticmethod
    def _target_title(record: SessionRecord) -> str:
        target = getattr(record, "target", None)
        return str(getattr(target, "title", "") or "")

    def _autonomy_event(
        self, session_id: str, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        """One autonomy event in the SESSION's own timeline (best effort)."""
        record = self.get(session_id) if session_id else None
        if record is None:
            return
        self._event(record, event_type, source="autonomy", payload=payload)

    async def _publish_autonomy(self, event_type: str, payload: Mapping[str, Any]) -> None:
        """Announce one phase move on the app's bus AND in the session's timeline."""
        self._autonomy_event(str(payload.get("session_id") or ""), event_type, payload)
        await self._emit(event_type, dict(payload))

    async def cancel_verification(self, session_id: str) -> dict[str, Any]:
        """Ask an in-flight verification to stop at its next checkpoint."""
        self._require(session_id)
        return await self.verification.cancel(session_id)

    async def _verification_context(
        self, session_id: str, plan_id: str
    ) -> VerificationContext:
        """Assemble ONE verification run's evidence before it observes anything.

        The plan under verification supplies the pre-action reference; the state
        service's bounded history still holds that reading if it has not been
        evicted, and its HUD rows become the baseline relative observables compare
        against. When it is gone, the baseline is simply absent and those
        observables report themselves indeterminate — a missing baseline is never
        read as "nothing changed".
        """
        record = self._require(session_id)
        plan = self.planner.get(plan_id)
        if plan is not None and plan.session_id != session_id:
            plan = None
        state = self.state.current
        game = ""
        if state is not None:
            game = str(state.game or state.observation.game or "")
        knowledge: tuple[Any, ...] = ()
        if game:
            try:
                knowledge = tuple(await self.knowledge.records(game=game, limit=40))
            except Exception:  # noqa: BLE001 - unreadable memory is no memory
                knowledge = ()
        baseline_hud: dict[str, Mapping[str, Any]] = {}
        baseline_state_id = plan.state_id if plan is not None else ""
        if baseline_state_id:
            try:
                for row in self.state.status().get("history") or []:
                    if not isinstance(row, Mapping) or row.get("state_id") != baseline_state_id:
                        continue
                    for reading in row.get("hud") or []:
                        if isinstance(reading, Mapping) and reading.get("element_id"):
                            baseline_hud[str(reading["element_id"])] = dict(reading)
                    break
            except Exception:  # noqa: BLE001 - no baseline is an honest answer
                baseline_hud = {}
        return VerificationContext(
            session_id=session_id,
            game=game,
            plan=plan,
            target=self._pinned_target(record),
            gate=self._control_gate(session_id),
            objective_state=self.objective_state.current,
            baseline_hud=baseline_hud,
            baseline_state_id=baseline_state_id,
            baseline_observation_id=plan.observation_id if plan is not None else "",
            baseline_sequence=plan.state_sequence if plan is not None else 0,
            knowledge=knowledge,
        )

    async def _observe_for_verification(self, session_id: str) -> ObservationEvidence:
        """Take ONE bounded post-action reading, or say why there is none.

        ``state.analyze()`` is NF-16's own reader: it refuses a stale, missing or
        superseded frame with a code, and that refusal is returned as REJECTED
        evidence rather than as a negative result. This never captures anything —
        NF-15's observation pipeline owns that — so verification cannot conjure a
        fresh frame, and a session that produced no new frame is reported unknown.
        """
        record = self._sessions.get(session_id)
        if record is None:
            return ObservationEvidence(
                session_id=session_id,
                code="unknown_session",
                reason=f"No NovaForge session is named {session_id!r}.",
            )
        target = self._observed_target()
        try:
            outcome = await self.state.analyze()
        except Exception as exc:  # noqa: BLE001 - an unreadable reading is rejected evidence
            return ObservationEvidence(
                session_id=session_id,
                code="analysis_failed",
                reason=f"The state reader raised: {type(exc).__name__}: {exc}",
                target=target,
            )
        if not outcome.analyzed or outcome.state is None:
            code = str(outcome.reason_code or "no_state")
            return ObservationEvidence(
                session_id=session_id,
                # One code vocabulary: NF-16's staleness refusal IS this phase's
                # "stale_reading", so the recovery layer recognises it.
                code="stale_reading" if code == "observation_stale" else code,
                reason=str(outcome.reason or "No state reading was produced."),
                target=target,
            )
        state = outcome.state
        age = self._state_age_seconds(state)
        accepted = True
        code = ""
        reason = ""
        observed_session = str(state.observation.session_id or session_id)
        if observed_session != session_id:
            accepted = False
            code = "wrong_session"
            reason = (
                f"The reading belongs to session {observed_session!r}, not {session_id!r}."
            )
        elif age is None:
            accepted = False
            code = "age_unknown"
            reason = "The reading has no readable timestamp, so its freshness is unknown."
        elif age > self.verification_policy.max_state_age_seconds:
            accepted = False
            code = "stale_reading"
            reason = (
                f"The reading is {age:.1f}s old (limit "
                f"{self.verification_policy.max_state_age_seconds:.1f}s)."
            )
        return ObservationEvidence(
            observation_id=state.observation.observation_id,
            state_id=state.state_id,
            session_id=observed_session,
            game=str(state.game or state.observation.game or ""),
            sequence=state.sequence,
            at=str(state.timestamp or state.observation.at or ""),
            age_seconds=age,
            status=state.status.value,
            accepted=accepted,
            code=code,
            reason=reason,
            readings={row.element_id: row.to_dict() for row in state.hud},
            changes=tuple(row.to_dict() for row in state.changes),
            capture={
                "width": state.observation.width,
                "height": state.observation.height,
                "backend": state.observation.backend,
                "validated": True,
            },
            target=target,
            state=state,
        )

    @staticmethod
    def _pinned_target(record: SessionRecord) -> dict[str, Any]:
        """The identity an action was sent to, as the verifier compares it."""
        target = record.target
        if target is None:
            return {}
        return {"handle": target.handle, "pid": target.pid, "title": target.title}

    def _observed_target(self) -> dict[str, Any]:
        """The target the OBSERVATION was taken from, per the observer itself."""
        try:
            row = self.observer.status().get("target")
        except Exception:  # noqa: BLE001 - an unreadable observer names no target
            return {}
        return dict(row) if isinstance(row, Mapping) else {}

    async def _record_outcomes(
        self, session_id: str, plan_id: str, outcomes: Sequence[Mapping[str, Any]]
    ) -> Any:
        """Record NF-21's outcomes on the plan they describe (NF-20's own API)."""
        return self.planner.record_effects(session_id, plan_id, outcomes)

    async def _invalidate_plan_for_recovery(
        self, session_id: str, plan_id: str
    ) -> Any:
        """Stop an obsolete plan through the planner's cancel path."""
        plan = await self.planner.cancel(session_id, plan_id)
        return plan.to_dict() if plan is not None else None

    async def _replan_for_recovery(
        self, session_id: str, plan_id: str, reason: str
    ) -> Any:
        """Ask the planner for a new plan; it still needs its own approval."""
        plan = await self.planner.replan(session_id, plan_id, reason=reason)
        return plan.to_dict()

    async def _pause_for_recovery(self, session_id: str, reason: str) -> Any:
        """Pause the session through the same path a pause request uses."""
        return await self.pause(session_id)

    async def _stop_for_recovery(self, session_id: str, reason: str) -> Any:
        """Stop the session through the same path a stop request uses."""
        return await self.stop(session_id, reason=reason)

    async def _notify_operator(
        self, session_id: str, payload: Mapping[str, Any]
    ) -> Any:
        """Escalate an unresolved outcome to the operator on the session timeline."""
        record = self._sessions.get(session_id)
        if record is None:
            return None
        event = self._event(
            record,
            "novaforge.outcome.recovery",
            source="verifier",
            severity="warning",
            payload={**dict(payload), "operator": "requested"},
            evidence=[session_ref(record.session_id)],
        )
        self._persist()
        return event.to_dict() if event is not None else None

    async def probe_objective(
        self, session_id: str, *, action_id: str = ""
    ) -> dict[str, Any]:
        """Read the objective's CURRENT state without recording a verdict.

        A caller that has to wait for evidence (a game that writes its state a
        moment after receiving a key) polls this, then records ONE verdict with
        :meth:`verify_objective`. Recording every poll would fill the timeline
        with "still in progress" rows that describe the clock, not the game.
        """
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
                "verifier": "",
                "verifier_version": 0,
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
        return verification.to_dict()

    async def verify_objective(
        self, session_id: str, *, reason: str = "manual", action_id: str = ""
    ) -> dict[str, Any]:
        """Ask the independent verifier what the objective's state is, now."""
        record = self._require(session_id)
        if record.objective is None:
            return await self.probe_objective(session_id, action_id=action_id)
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
            and record.state in {SessionState.RUNNING, SessionState.PAUSED}
        ):
            # A verified objective is the ONE way a session reaches COMPLETED.
            # A PAUSED session is included on purpose: the verdict is decided by
            # reading the window, so a session the loop had paused (an operator
            # pause, a window the observer briefly could not measure) has still
            # met it -- and PAUSED -> STOPPING -> COMPLETED is a legal close.
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
        evidence is lost. NF-15's observer is stopped on the same path, before
        the early return, so an idle application still stops an observation.
        """
        # An autonomous run is stopped FIRST: its supervisor waits on a run task
        # that stops the session, and a session stopped underneath it would leave
        # the run reporting an ending it did not choose. Stopping it here also
        # releases whatever input it held through the session's own cleanup.
        with contextlib.suppress(Exception):
            await self.shutdown_autonomy()
        with contextlib.suppress(Exception):
            await self.verification.shutdown()
        with contextlib.suppress(Exception):
            await self.planner.shutdown()
        with contextlib.suppress(Exception):
            await self.knowledge.shutdown()
        with contextlib.suppress(Exception):
            await self.objective_state.shutdown()
        with contextlib.suppress(Exception):
            await self.state.shutdown()
        with contextlib.suppress(Exception):
            await self.observer.shutdown()
        active = self.active()
        if active is None:
            self._task = None
            with contextlib.suppress(Exception):
                await self.control.shutdown()
            return
        try:
            await self.stop(active.session_id, reason="application_shutdown")
        except Exception:  # noqa: BLE001 - shutdown must keep going
            pass
        finally:
            self._task = None
            with contextlib.suppress(Exception):
                self._persist()
        with contextlib.suppress(Exception):
            await self.control.shutdown()

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
        # A closed session has no clock left to run out. Without this guard a
        # session that COMPLETED on a verified objective kept its loop alive for
        # another tick, and because ``_finalize`` clears the monotonic origin the
        # fallback wall-clock measure made the limit fire on the finished record
        # -- rewriting ``end_reason`` to time_limit and recording "the objective
        # was NOT verified" on a session whose objective had just been verified.
        if record.state in SESSION_TERMINAL_STATES:
            return False
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
        # A frame recorded after the session ended would be evidence of a run
        # that no longer exists.
        if record.state in SESSION_TERMINAL_STATES:
            return
        target = record.target
        if target is None:
            raise RuntimeError("Session has no target window.")
        # Identity is handle + pid. A TITLE is a label the target is free to
        # change mid-run -- and the one objective this build verifies is
        # satisfied by exactly that change -- so resolving by title first read a
        # retitled window as "the target closed" the moment its objective came
        # true, pausing the session one tick after the action that met it. The
        # handle probe is therefore asked first; the title lookup stays as the
        # fallback for a host without one.
        geometry: Any | None = None
        if self.objective_probe is not None and target.handle:
            try:
                geometry = await self.objective_probe(target.handle)
            except Exception:  # noqa: BLE001 - fall through to the title lookup
                geometry = None
        if geometry is None:
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
        visibility_issue, visibility_detail = _target_visibility_issue(resolved)
        if visibility_issue:
            # Keep the newest measured state in the record so the Guardian and
            # panel can say "minimized" even though identity still matches.
            record.target = resolved
            await self._pause_for(record, visibility_issue, detail=visibility_detail)
            return
        record.target = resolved
        if resolved.title != target.title:
            # Same window, new label. Following it (rather than pausing) is what
            # makes a title objective verifiable: the title change IS the
            # success signal, so calling it a lost target would refuse to see it.
            record.target = resolved
            self._event(
                record,
                "target.validated",
                source="target",
                payload={
                    "title": resolved.title,
                    "handle": resolved.handle,
                    "pid": resolved.pid,
                    "reason": "The window was retitled; its handle and process still match.",
                },
                evidence=[session_ref(record.session_id)],
            )
        elif resolved.bounds != target.bounds:
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
            result = await self._acquire_frame(
                resolved, str(path), session_id=record.session_id
            )
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
            # A refusal that names a TARGET condition (minimized, hidden, off-screen, behind
            # another window, unpainted) is not a transient error to keep polling
            # through: the session pauses so the panel can say why. A transient
            # capture failure (a worker restart, a timeout) is recorded and the
            # next tick retries — pausing on it would end the run for a hiccup.
            pause_reason = str(result.get("reason_code") or "")
            if pause_reason in _TARGET_PAUSE_REASONS:
                await self._pause_for(record, pause_reason, detail=reason)
            self._persist_throttled()
            return
        width = int(result.get("width") or 0)
        height = int(result.get("height") or 0)
        size_bytes = int(result.get("size_bytes") or 0)
        source = str(result.get("source") or "")
        note = str(result.get("note") or "")
        verdict = dict(result.get("validation") or {})
        target_frame = source in {"window_crop", "window_surface"}
        if verdict and verdict.get("livable") is False:
            target_frame = False
        if not target_frame:
            # The backend produced a fresh PNG, but it is not a frame OF the
            # target: a minimized/off-screen window falls back to the whole
            # desktop. Recording it as a session frame is how H02 displayed the
            # NovaForge UI as "LIVE" gameplay. Keep the bytes as rejected
            # evidence, but never present them as the target.
            reason = self._rejection_reason(result, note=note, source=source)
            record.record_error(
                f"Capture produced no usable frame of the target: {note or source}",
                where="capture",
            )
            observation = ObservationRecord(
                index=len(record.observations) + 1,
                at=utc_now(),
                source="desktop_capture",
                summary="Capture did not contain the target window; nothing is shown as live.",
                evidence=False,
                latency_ms=round(latency_ms, 1),
                note=(note or f"capture source: {source}")[:300],
            )
            record.record_observation(observation)
            self._event(
                record,
                "frame.rejected",
                source="capture",
                severity="warning",
                payload={
                    "reason": note or f"capture source: {source}",
                    "fresh": True,
                    "source": source,
                    "backend": str(result.get("backend") or ""),
                    "content": str(result.get("content") or ""),
                    "latency_ms": round(latency_ms, 1),
                },
                evidence=[observation_ref(record.session_id, observation.index, observation.at)],
            )
            await self._pause_for(record, reason, detail=note)
            self._persist_throttled()
            return
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
            backend=str(result.get("backend") or ""),
            content=str(result.get("content") or ""),
            validation=verdict,
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
                "backend": frame.backend,
                "content": frame.content,
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

    def _rejection_reason(
        self, result: Mapping[str, Any], *, note: str, source: str
    ) -> str:
        """Translate a rejected capture into the reason the session pauses for.

        The backend's stable code wins; the human note is only a fallback. That
        ordering matters: a note is prose and can change, while the Guardian and
        the pause reason are contract values the UI and tests rely on.
        """
        code = str(result.get("reason_code") or "")
        if code in {
            "target_minimized", "target_hidden", "target_not_foreground",
            "target_offscreen", "target_unreadable", "capture_unavailable",
        }:
            return code
        verdict = dict(result.get("validation") or {})
        text = str(verdict.get("reason") or note or "").lower()
        if source == "screen" and "outside the captured display" in text:
            return "target_offscreen"
        if "minimized" in text:
            return "target_minimized"
        if "not foreground" in text or "behind another window" in text:
            return "target_not_foreground"
        if "blank" in text or "unpainted" in text:
            return "target_unreadable"
        return "target_unreadable"

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
                "target_minimized": "was minimized; capture is suspended rather than showing the desktop.",
                "target_hidden": "is not visible; capture is suspended rather than showing the desktop.",
                "target_offscreen": "has no usable on-screen area; capture is suspended.",
                "target_not_foreground": (
                    "is behind another window and no surface backend could read it; "
                    "capture is suspended rather than showing what covers it."
                ),
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
                "target_minimized": "target.minimized",
                "target_hidden": "target.hidden",
                "target_offscreen": "target.offscreen",
                "target_not_foreground": "target.background",
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
        # Closing a session stops its loop. ``_finalize`` can be reached from
        # outside the loop (a verified objective), and without this signal the
        # loop kept capturing and re-checking limits against a record that had
        # already ended.
        if self._stop_event is not None:
            self._stop_event.set()
        if self._current_id == record.session_id:
            self._started_monotonic = None
        # NF-19: an in-flight control action is asked to stop and given a bounded
        # moment to release its OWN input before the session cleanup runs. Closing
        # underneath it would make its release fail on a key already freed and
        # report the race as the action's failure.
        if self.control.active:
            await self.control.cancel("session_closed")
            await self.control.wait_idle()
        try:
            with contextlib.suppress(SessionTransitionError):
                record.transition(SessionState.STOPPING, reason=reason)
            cleanup = await self._release_inputs()
            record.input_cleanup = cleanup
            self.control.adopt_cleanup(cleanup)
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
            with contextlib.suppress(OSError):
                Path(str(path) + THUMB_SUFFIX).unlink()
        sweep_orphan_thumbnails(directory)

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
        minimized=bool(getattr(geometry, "minimized", False)),
        visible=bool(getattr(geometry, "visible", True)),
    )


def _target_visibility_issue(target: TargetWindow) -> tuple[str, str]:
    """Why this window is not currently capturable, or ``("", "")``.

    Identity (handle + pid) can stay valid while the window is gone from the
    screen. That distinction is the H02 failure: a minimized fullscreen game
    kept answering its handle and pid, so the session called it a valid target
    while every screen capture contained the desktop. The rules live in
    :func:`novacontrol.novaforge.observer.target_visibility` — ONE
    implementation, so a session and an observation can never disagree about a
    minimized target.
    """
    window = window_state(target)
    if window is None:
        return "target_offscreen", "The target window has no usable identity."
    return target_visibility(window)


def _ocr_word(word: Any) -> dict[str, Any]:
    """One OCR word in the shape both observers and verifiers read.

    The Windows engine returns JSON mappings with ``w``/``h``; the observation
    layer reads attributes with ``width``/``height``. Normalising here means a
    region filter compares the same coordinates whichever consumer asks.
    """

    def field(name: str, default: Any = 0) -> Any:
        if isinstance(word, Mapping):
            return word.get(name, default)
        return getattr(word, name, default)

    def number(*names: str) -> int:
        for key in names:
            value = field(key, None)
            if value in (None, ""):
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return 0

    return {
        "text": str(field("text", "") or ""),
        "line": number("line"),
        "x": number("x"),
        "y": number("y"),
        "width": number("width", "w"),
        "height": number("height", "h"),
    }


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
