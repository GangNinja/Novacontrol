"""NF-16 — the game-state service: one reading at a time, honestly bounded.

The service is the only piece that knows WHEN a state is read. It:

* takes the newest **validated** frame from the NF-15 observer (never a frame of
  its own, never a screenshot of the desktop);
* asks the injected perception seam for one scene (by default the existing
  ``PerceptionEngine``, whose OCR reader and classical detector this build
  already ships — no second vision stack);
* refuses rather than guesses: no validated frame, a frame older than the
  freshness window, an observation that a newer capture superseded while the
  analysis was running, a second analysis while one is in flight, a timeout;
* keeps the history bounded, publishes structured events on the application's
  ONE bus, and stores a small snapshot that is restored as ``previous`` — never
  as a current state.

Perception is CPU-bound on this laptop, so it runs in a worker thread with its
own event loop: the HTTP handler that asked for the reading does not hold the
application's loop while OCR and region scanning run. A timed-out worker is
abandoned rather than killed, and no second worker may start until it finishes —
the alternative is two scans racing over the same engine.

Nothing here captures, clicks or plans. If the perception seam raises, the
failure is a reported result: slow or broken perception must never take down the
rest of NovaControl.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from novacontrol.novaforge.observer import FAILED, PAUSED, STOPPED
from novacontrol.novaforge.scene import SceneTracker, SceneTransition
from novacontrol.novaforge.state import (
    SCHEMA_VERSION,
    GameState,
    ObservationRef,
    ProfileRegistry,
    build_game_state,
    default_registry,
)

__all__ = [
    "AnalysisOutcome",
    "GameStateService",
    "ObservationSource",
    "STATE_NAMESPACE",
]

#: The state store namespace for the persisted snapshot.
STATE_NAMESPACE = "novaforge_state"

#: Default freshness window: a frame older than this is a record, not a state.
DEFAULT_MAX_AGE_SECONDS = 5.0

#: Default ceiling on one analysis (perception is CPU-bound on this laptop).
DEFAULT_ANALYZE_TIMEOUT = 25.0

#: How many past states the service keeps in memory (and persists, capped).
DEFAULT_MAX_STATES = 8

#: NF-24: consecutive agreeing observations before a scene change is a fact.
#: Two is the smallest number that is not "one frame said so"; a caller with a
#: slow observation interval can lower it, and the payload always states it.
DEFAULT_SCENE_CONFIRMATIONS = 2


class ObservationSource(Protocol):
    """The NF-15 surface this service consumes — status and a validated frame."""

    def status(self) -> dict[str, Any]: ...

    def frame_path(self) -> Any: ...


#: ``perceive(frame) -> PerceptionResult``: the injected perception seam.
Perceive = Callable[[Any], Awaitable[Any]]
#: ``publish(event_type, payload)``: the application's bus seam.
Publish = Callable[[str, dict[str, Any]], Awaitable[None]]


def _now() -> datetime:
    """The one clock the service reads, injectable in a test."""
    return datetime.now(UTC)


def _mapping(value: Any) -> Mapping[str, Any]:
    """A mapping from whatever a status body held, or an empty one."""
    return value if isinstance(value, Mapping) else {}


async def _resolve(awaitable: Awaitable[Any]) -> Any:
    """Adapt an awaitable to something ``asyncio.run`` accepts."""
    return await awaitable


def _age_seconds(stamp: str, now: datetime) -> float | None:
    """How old an ISO timestamp is, or ``None`` when it cannot be read."""
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (now - moment.astimezone(UTC)).total_seconds())


def _consume(future: Any) -> None:
    """Read a worker's exception so an abandoned analysis logs nothing twice."""
    try:
        future.exception()
    except BaseException:  # noqa: BLE001 - the reading already reported its own failure
        pass


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    """The answer to one analyze request: a state, or a refusal with a code."""

    analyzed: bool
    reason_code: str = ""
    reason: str = ""
    state: GameState | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "analyzed": self.analyzed,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "state": self.state.to_dict() if self.state is not None else None,
        }


@dataclass
class GameStateService:
    """Read game state from validated observations, on request and bounded.

    Every collaborator is injected so the whole service can be driven in a test
    with a fake observer and a fake scene; the defaults are the real engine and
    the real state store, so production wiring is one line.
    """

    observer: ObservationSource
    registry: ProfileRegistry = field(default_factory=default_registry)
    perceive: Perceive | None = None
    publish: Publish | None = None
    #: Told about each successful state, AFTER it is stored. NF-17's objective
    #: service is the only consumer; a failure there must not fail the reading,
    #: which is why it is awaited inside its own guard.
    on_state: Callable[[GameState], Any] | None = None
    store: Any | None = None
    clock: Callable[[], datetime] = _now
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS
    analyze_timeout: float = DEFAULT_ANALYZE_TIMEOUT
    max_states: int = DEFAULT_MAX_STATES
    max_entities: int = 32
    profile_id: str = ""
    scene_confirmations: int = DEFAULT_SCENE_CONFIRMATIONS
    max_history: int = field(init=False, repr=False)
    _engine: Any = field(default=None, init=False, repr=False)
    _history: deque[dict[str, Any]] = field(default_factory=deque, init=False, repr=False)
    _last_state: GameState | None = field(default=None, init=False, repr=False)
    _last_error: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _last_refusal: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _previous: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _sequence: int = field(default=0, init=False, repr=False)
    _session_id: str = field(default="", init=False, repr=False)
    _analyzed: int = field(default=0, init=False, repr=False)
    _failed: int = field(default=0, init=False, repr=False)
    _superseded: int = field(default=0, init=False, repr=False)
    _durations: list[float] = field(default_factory=list, init=False, repr=False)
    #: NF-24: the scene run across observations. It is deliberately NOT reset by
    #: every reading — that is what makes a transition temporal.
    _scene: SceneTracker = field(default_factory=SceneTracker, init=False, repr=False)
    _transitions: int = field(default=0, init=False, repr=False)
    _task: asyncio.Task[Any] | None = field(default=None, init=False, repr=False)
    _worker: asyncio.Future[Any] | None = field(default=None, init=False, repr=False)
    _stopped: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.max_history = max(1, int(self.max_states))
        self._history = deque(maxlen=self.max_history)
        self._scene = SceneTracker(confirmations=max(1, int(self.scene_confirmations)))
        self._load()

    # ── persistence ────────────────────────────────────────────────────────
    def _load(self) -> None:
        if self.store is None:
            return
        try:
            payload = self.store.read(STATE_NAMESPACE)
        except Exception:  # noqa: BLE001 - an unreadable store is not a crash
            return
        if not isinstance(payload, Mapping):
            return
        states = payload.get("states")
        if isinstance(states, list):
            self._history = deque(
                [dict(row) for row in states if isinstance(row, Mapping)][-self.max_history :],
                maxlen=self.max_history,
            )
        previous = payload.get("last")
        if isinstance(previous, Mapping):
            self._previous = {**dict(previous), "restored": True}
        sequence = payload.get("sequence")
        if isinstance(sequence, int) and sequence >= 0:
            self._sequence = sequence
        session = str(payload.get("session_id") or "")
        if session:
            self._session_id = session
        scene = payload.get("scene")
        if isinstance(scene, Mapping):
            # A run that was accumulating when the service stopped continues
            # where it left off; it does not silently start over at zero.
            try:
                self._scene.restore(_transition_from(scene))
            except Exception:  # noqa: BLE001 - a stale record is not a crash
                self._scene.reset()

    def _persist(self) -> None:
        if self.store is None:
            return
        try:
            self.store.write(
                STATE_NAMESPACE,
                {
                    "schema_version": SCHEMA_VERSION,
                    "saved_at": self.clock().isoformat(),
                    "session_id": self._session_id,
                    "sequence": self._sequence,
                    "last": self._last_state.to_dict() if self._last_state is not None else None,
                    "scene": (
                        self._scene.last.to_dict()
                        if self._scene.last is not None
                        else None
                    ),
                    "states": [dict(row) for row in self._history],
                    "counts": {
                        "analyzed": self._analyzed,
                        "failed": self._failed,
                        "superseded": self._superseded,
                    },
                },
            )
        except Exception:  # noqa: BLE001 - a store must not fail the reading
            return

    # ── perception seam ────────────────────────────────────────────────────
    def _perception(self) -> Any:
        """The shared engine, built once — a second engine would load OCR again."""
        if self._engine is None:
            from novacontrol.perception import PerceptionEngine

            self._engine = PerceptionEngine()
        return self._engine

    async def _perceive_default(self, frame: Any) -> Any:
        """One scene from the existing engine: OCR + regions, no model call."""
        from novacontrol.perception import PerceptionMode, PerceptionRequest

        request = PerceptionRequest(
            source=str(frame.path),
            source_kind="image",
            mode=PerceptionMode.FAST,
            allow_vlm=False,
            temporal_context=True,
            max_objects=max(8, int(self.max_entities)),
        )
        return await self._perception().perceive(request, frame=frame)

    async def _perceive(self, frame: Any) -> Any:
        """Run the seam in a worker thread, with a ceiling and no overlap."""
        worker = self._worker
        if worker is not None and not worker.done():
            raise RuntimeError("the previous perception pass is still finishing")
        seam = self.perceive or self._perceive_default

        def runner() -> Any:
            return asyncio.run(_resolve(seam(frame)))

        future = asyncio.get_running_loop().run_in_executor(None, runner)
        future.add_done_callback(_consume)
        self._worker = future
        try:
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=max(0.1, float(self.analyze_timeout))
            )
        finally:
            if future.done() and self._worker is future:
                self._worker = None

    # ── the reading ────────────────────────────────────────────────────────
    async def analyze(self, *, profile_id: str | None = None) -> AnalysisOutcome:
        """Read one state from the newest validated observation, or refuse.

        The pre- and post-checks are the point of this method: the observation
        is re-read AFTER perception finishes, so a frame that a new capture
        replaced mid-analysis cannot be attributed to the state it no longer
        describes.
        """
        if self._stopped:
            return self._refuse("stopped", "The state service has been shut down.")
        if self._task is not None and not self._task.done():
            return self._refuse(
                "analysis_in_progress",
                "An analysis is already running; one reading at a time.",
            )
        if self._worker is not None and not self._worker.done():
            return self._refuse(
                "analysis_in_progress",
                "A previous perception pass is still finishing after a timeout; no "
                "second reading is started over the same engine.",
            )
        task = asyncio.current_task()
        self._task = task
        try:
            return await self._analyze(profile_id=profile_id)
        finally:
            if self._task is task:
                self._task = None

    async def _analyze(self, *, profile_id: str | None) -> AnalysisOutcome:
        started = time.monotonic()
        try:
            status = self.observer.status()
        except Exception as exc:  # noqa: BLE001 - a broken observer is a refusal
            return await self._fail(
                "observer_unavailable", f"The observer could not be read: {exc}"
            )
        paused = self._pause_refusal(status)
        if paused is not None:
            return self._refuse(*paused)
        success = _mapping(status.get("last_success"))
        if not success:
            return self._refuse(
                "no_validated_frame",
                "No validated observation exists yet; start observing a target first.",
            )
        session_id = str(status.get("session_id") or "")
        observation_id = str(success.get("observation_id") or "")
        if not observation_id:
            return self._refuse(
                "no_validated_frame", "The last observation carries no identifier."
            )
        age = _age_seconds(str(success.get("at") or ""), self.clock())
        if age is None:
            return self._refuse(
                "observation_time_unknown",
                "The last validated observation has no readable timestamp, so its "
                "freshness cannot be established.",
            )
        if age > max(0.0, float(self.max_age_seconds)):
            return self._refuse(
                "observation_stale",
                f"The last validated frame is {age:.1f}s old (limit "
                f"{self.max_age_seconds:.1f}s); this is a record, not a current state.",
            )
        try:
            path = self.observer.frame_path()
        except Exception as exc:  # noqa: BLE001 - an unreadable path is a refusal
            return await self._fail(
                "frame_unavailable", f"The observation frame could not be read: {exc}"
            )
        if path is None:
            return self._refuse(
                "frame_unavailable",
                "The observer has no validated frame file for the last success.",
            )
        target = _mapping(status.get("target"))
        target_key = (
            str(target.get("handle") or ""),
            str(target.get("pid") or ""),
            str(target.get("start_time") or ""),
        )
        match = self.registry.resolve(
            str(target.get("title") or ""),
            width=int(success.get("width") or 0),
            height=int(success.get("height") or 0),
            profile_id=(profile_id if profile_id is not None else self.profile_id),
        )
        if session_id != self._session_id:
            # A new session starts its own sequence; previous states are not
            # comparable across sessions, which the builder also enforces. The
            # scene run goes too: a confirmed screen from one session/target is
            # not a fact about this one.
            self._session_id = session_id
            self._sequence = 0
            self._scene.reset()
        self._sequence += 1
        sequence = self._sequence
        observation = ObservationRef(
            observation_id=observation_id,
            session_id=session_id,
            game=str(status.get("game") or ""),
            seq=int(success.get("seq") or 0),
            at=str(success.get("at") or ""),
            width=success.get("width"),
            height=success.get("height"),
            backend=str(success.get("backend") or ""),
            frame_id=f"{observation_id}:frame",
        )
        from novacontrol.perception.frames import Frame

        frame = Frame(
            frame_id=observation.frame_id,
            source_id=observation.observation_id,
            sequence=sequence,
            timestamp=self.clock().isoformat(),
            path=str(path),
            width=int(success.get("width") or 0) or None,
            height=int(success.get("height") or 0) or None,
        )
        try:
            result = await self._perceive(frame)
        except TimeoutError:
            return await self._fail(
                "analysis_timeout",
                f"Perception exceeded {self.analyze_timeout:.1f}s and the reading was "
                "abandoned; the worker is allowed to finish before another starts.",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a provider failure is a result
            return await self._fail(
                "analysis_failed", f"Perception failed: {type(exc).__name__}: {exc}"
            )
        # Supersession: the frame this reading was built from must still be the
        # observer's newest success, and the target must not have changed.
        try:
            after = self.observer.status()
        except Exception:  # noqa: BLE001 - a lost re-read is a refusal, not a crash
            return self._refuse(
                "observer_unavailable",
                "The observer could not be re-read after perception, so the result "
                "cannot be attributed to a current observation.",
            )
        paused_after = self._pause_refusal(after)
        if paused_after is not None:
            return self._refuse(*paused_after)
        after_success = _mapping(after.get("last_success"))
        after_target = _mapping(after.get("target"))
        after_key = (
            str(after_target.get("handle") or ""),
            str(after_target.get("pid") or ""),
            str(after_target.get("start_time") or ""),
        )
        if (
            str(after.get("session_id") or "") != session_id
            or str(after_success.get("observation_id") or "") != observation_id
            or after_key != target_key
        ):
            self._superseded += 1
            self._persist()
            return self._refuse(
                "observation_superseded",
                "The target or the newest observation changed while the state was "
                "being read; the result was discarded rather than attributed to it.",
            )
        scene = getattr(result, "scene", None)
        ocr_row = _capability(result, "ocr")
        detection_row = _capability(result, "detection")
        processing_ms = round((time.monotonic() - started) * 1000.0, 1)
        latency = {
            "processing_ms": processing_ms,
            "perception": {
                str(key): round(float(value), 3)
                for key, value in (getattr(result, "latency", {}) or {}).items()
            },
            "model_load_ms": None,
            "model_load_note": "No model is loaded: OCR and region detection are the shipped path.",
        }
        state = build_game_state(
            scene,
            observation,
            game=observation.game,
            sequence=sequence,
            timestamp=self.clock().isoformat(),
            profile_match=match,
            previous=self._last_state,
            ocr_available=getattr(ocr_row, "available", None),
            ocr_reason=getattr(ocr_row, "reason", ""),
            detection_available=getattr(detection_row, "available", None),
            detection_reason=getattr(detection_row, "reason", ""),
            max_entities=self.max_entities,
            latency=latency,
            extra_limitations=(
                f"Perception status for this reading: {getattr(result, 'status', 'unknown')}",
            ),
            scene_tracker=self._scene,
        )
        if state.transition.state == "confirmed":
            self._transitions += 1
        self._last_state = state
        self._last_error = None
        self._last_refusal = None
        self._analyzed += 1
        self._durations.append(processing_ms)
        del self._durations[:-50]
        self._history.append(state.to_dict())
        self._persist()
        await self._notify_state(state)
        await self._emit(
            "novaforge.state.analyzed",
            {
                "state_id": state.state_id,
                "session_id": session_id,
                "observation_id": observation_id,
                "sequence": sequence,
                "status": state.status.value,
                "profile_id": match.profile.profile_id if match.profile is not None else "",
                "profile_reason": match.reason_code,
                "fields_declared": state.quality.get("declared", 0),
                "fields_recognized": state.quality.get("recognized", 0),
                "entities": len(state.entities),
                "entity_status": state.entity_status.value,
                "changes": len(state.changes),
                "scene": state.scene.kind.value,
                "scene_status": state.scene.status,
                "scene_source": state.scene.source,
                "scene_confirmed": state.transition.confirmed,
                "transition": state.transition.state,
                "processing_ms": processing_ms,
            },
        )
        return AnalysisOutcome(analyzed=True, state=state)

    @staticmethod
    def _pause_refusal(status: Mapping[str, Any]) -> tuple[str, str] | None:
        """Why this pipeline state cannot supply a CURRENT reading, or None.

        This is not a freshness question: when the pipeline is paused or has
        ended, its newest frame is whatever was there when it stopped — however
        young that is. Reading it as a state is how a panel shows a
        live-looking reading for a window that has been minimized, destroyed or
        exited; the service's own declared contract ("a stale, paused or missing
        frame is a refusal with a code") is this check. NF-23 measured the gap:
        a state was read one second after the target window was destroyed, and
        again after the observed process exited.

        An unknown state (a caller whose status does not report one) is not
        refused here: this check speaks only for the states it can name.
        """
        state = str(status.get("state") or "")
        if state == PAUSED:
            pause = _mapping(status.get("pause"))
            code = str(pause.get("code") or "paused").strip() or "paused"
            detail = str(pause.get("detail") or "").strip()
            if bool(pause.get("user_requested")):
                return (
                    "observation_paused",
                    "Observation is paused by request, so the last frame is not a "
                    "current reading of the target.",
                )
            return (
                "observation_paused",
                f"Observation is paused ({code})"
                + (f": {detail}" if detail else "")
                + "; the last frame is not a current reading of the target.",
            )
        if state in (STOPPED, FAILED):
            ended = str(status.get("reason") or status.get("reason_code") or "ended")
            return (
                "observation_inactive",
                f"Observation has {state} ({ended}); its last frame is a record, not "
                "a current reading of the target. Start observing again to read one.",
            )
        return None

    # ── refusals ───────────────────────────────────────────────────────────
    def _refuse(self, code: str, reason: str) -> AnalysisOutcome:
        """Nothing was attempted or the result was discarded — recorded, not a failure.

        A refusal is not counted in ``failed`` (nothing broke); it is kept in
        ``last_refusal`` so the panel can still say WHY a reading did not happen
        after the page is reloaded.
        """
        self._last_refusal = {
            "reason_code": code,
            "reason": reason,
            "at": self.clock().isoformat(),
        }
        return AnalysisOutcome(analyzed=False, reason_code=code, reason=reason)

    async def _fail(self, code: str, reason: str) -> AnalysisOutcome:
        self._failed += 1
        self._last_refusal = None
        self._last_error = {
            "reason_code": code,
            "reason": reason,
            "at": self.clock().isoformat(),
        }
        self._persist()
        await self._emit(
            "novaforge.state.failed",
            {
                "reason_code": code,
                "reason": reason,
                "session_id": self._session_id,
                "state_id": (self._last_state.state_id if self._last_state is not None else ""),
            },
        )
        return AnalysisOutcome(analyzed=False, reason_code=code, reason=reason)

    async def _notify_state(self, state: GameState) -> None:
        """Hand the state to its consumer, and never let that consumer fail it."""
        if self.on_state is None:
            return
        try:
            result = self.on_state(state)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a consumer bug is not a reading failure
            return

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """Publish on the ONE bus; a lost event cannot fail the reading."""
        if self.publish is None:
            return
        try:
            await self.publish(event_type, payload)
        except Exception:  # noqa: BLE001 - the announcement is not the work
            return

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def shutdown(self) -> None:
        """Stop accepting readings and cancel one in flight. Safe to repeat."""
        self._stopped = True
        task = self._task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 - shutdown is total by design
                pass
        self._task = None

    def reset(self, reason: str = "requested") -> dict[str, Any]:
        """Forget the comparison baseline without forgetting what was measured.

        The restored ``previous`` record goes too: it is the baseline a restart
        would compare against, and keeping it after a reset would let the panel
        show a prior state the reader no longer holds.
        """
        self._last_state = None
        self._previous = None
        self._sequence = 0
        self._scene.reset()
        self._persist()
        return {"reset": True, "reason": reason, "history_kept": len(self._history)}

    # ── status ─────────────────────────────────────────────────────────────
    @property
    def current(self) -> GameState | None:
        """The newest state, for a consumer that reasons over it (NF-20)."""
        return self._last_state

    def status(self) -> dict[str, Any]:
        """The panel's whole view: last state, bounded history, measurements."""
        durations = list(self._durations)
        return {
            "schema_version": SCHEMA_VERSION,
            "available": True,
            "tested": self._last_state is not None,
            "analyzing": bool(self._task is not None and not self._task.done()),
            "stopped": self._stopped,
            "profile_id": (
                self._last_state.profile.profile.profile_id
                if self._last_state is not None and self._last_state.profile.profile is not None
                else ""
            ),
            "profiles": self.registry.rows(),
            "state": self._last_state.to_dict() if self._last_state is not None else None,
            "history": [dict(row) for row in self._history],
            "previous": dict(self._previous) if self._previous is not None else None,
            "last_error": dict(self._last_error) if self._last_error is not None else None,
            "last_refusal": (
                dict(self._last_refusal) if self._last_refusal is not None else None
            ),
            "counts": {
                "analyzed": self._analyzed,
                "failed": self._failed,
                "superseded": self._superseded,
                "history": len(self._history),
                "scene_transitions": self._transitions,
            },
            "scene": {
                "confirmed": self._scene.confirmed.value,
                "candidate": self._scene.pending.value,
                "confirmations": self._scene.streak,
                "required": self._scene.confirmations,
                "observation_id": self._scene.last_observation_id,
                "contradictions": list(self._scene.contradictions[-4:]),
                "last_transition": (
                    self._scene.last.to_dict() if self._scene.last is not None else None
                ),
            },
            "measured": {
                "processing_ms": {
                    "last": durations[-1] if durations else None,
                    "average": round(sum(durations) / len(durations), 1) if durations else None,
                    "max": max(durations) if durations else None,
                },
                "freshness_limit_seconds": self.max_age_seconds,
                "analyze_timeout_seconds": self.analyze_timeout,
            },
            "limitations": [
                "A state is a reading of the screen. It is not an understanding of the "
                "game, and it never predicts or acts.",
                "Only a validated NF-15 frame from a LIVE observation is read; a stale "
                "frame, a pause or an ended session is a refusal with a code.",
                "Entity kinds stay 'unknown': no semantic detector is wired.",
                "A scene is named only from a profile's declared rules or an "
                "unambiguous text cue; 'unknown' is a real answer, not a failure.",
                "A scene change is confirmed after consecutive agreeing "
                f"observations (currently {self._scene.confirmations}); one frame is "
                "never a transition.",
                "Confidence is the mean of the provider confidences a scene measured; "
                "when no provider reports one it stays None, and quality is a "
                "heuristic coverage count.",
                "Frames are read at their captured resolution: shrinking them would "
                "move the profile's pixel regions, so no resize option is offered.",
            ],
        }

    def profiles(self) -> dict[str, Any]:
        """The profile table, including rejected profiles and why."""
        return {
            "profiles": self.registry.rows(),
            "defaults": list(self.registry.ids),
            "rejected": self.registry.rejected,
        }



def _transition_from(record: Mapping[str, Any]) -> SceneTransition:
    """Rebuild a persisted transition, tolerating a record from an older build."""
    contradictions = record.get("contradictions")
    raw_sequence = record.get("sequence")
    # A run resumed across a restart carries the sequence it reached, so the
    # first observation after the restart is recognised as the interruption it
    # is instead of counting as agreement with the run before it.
    sequence = int(raw_sequence) if isinstance(raw_sequence, (int, float)) else None
    return SceneTransition(
        state=str(record.get("state") or "none"),
        confirmed=str(record.get("confirmed") or "unknown"),
        candidate=str(record.get("candidate") or "unknown"),
        confirmations=int(record.get("confirmations") or 0),
        required=int(record.get("required") or DEFAULT_SCENE_CONFIRMATIONS),
        previous=str(record.get("previous") or ""),
        observation_id=str(record.get("observation_id") or ""),
        sequence=sequence,
        reason=str(record.get("reason") or ""),
        interrupted=bool(record.get("interrupted")),
        contradictions=(
            tuple(str(item) for item in contradictions)
            if isinstance(contradictions, (list, tuple))
            else ()
        ),
    )


def _capability(result: Any, name: str) -> Any:
    """One capability row from a perception result, or ``None`` when absent."""
    getter = getattr(result, "capability", None)
    if callable(getter):
        try:
            return getter(name)
        except Exception:  # noqa: BLE001 - an unreadable row is no row
            return None
    return None

