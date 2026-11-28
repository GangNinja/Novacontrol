"""The NovaForge pilot: gameplay decided from measurements, not from a model.

This module is the decision maker the overlay drives. It is deliberately NOT a
language model client: every decision is a bounded rule over evidence the
observation layer already measured — recognised words and where they are, the
window title, the frame's own pixels and the outcome of the previous action.

The pipeline is the one the mission describes, with no step skipped::

    objective -> task plan -> fresh observation -> target resolved from OCR
        -> action through the shipped control path -> fresh observation
        -> verified outcome (text appeared / disappeared / screen changed)
        -> next step, replan, or an honest stop with the reason

Three properties are structural rather than conventions:

* **An action is only taken against a target that was READ in a fresh frame.**
  A click is the centre of the words that were actually found, so a menu that
  moved cannot be clicked where it used to be.
* **Requested, executed and verified are three separate records.** The engine
  reports ``status="dispatched"`` when input left the controller and
  ``verified=True`` only when a LATER frame shows the expected change.
* **A task that cannot resolve its target stops and says so.** Retries are
  bounded and a repeated action into an unchanged screen is diagnosed instead
  of looped.

Operator instructions are parsed deterministically (``parse_instruction``):
imperative phrases map onto the small action vocabulary, and anything the
parser cannot read becomes a stated refusal rather than a guess.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

#: How long one observation may take before it is reported as unavailable.
OBSERVE_TIMEOUT = 20.0
#: How long an expected screen change is awaited before a step is failed.
VERIFY_TIMEOUT = 8.0
#: Poll interval while waiting for text to appear.
POLL_SECONDS = 0.9
#: Characters ``type_text`` can send through the key table. Anything else is
#: refused by name instead of silently dropped from the middle of a value.
TYPABLE = set("abcdefghijklmnopqrstuvwxyz0123456789 -_.")


# ── evidence ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Word:
    """One recognised word and where it sits in the window's client area."""

    text: str
    x: int
    y: int
    width: int
    height: int
    line: int = 0

    @property
    def cx(self) -> int:
        return self.x + max(1, self.width) // 2

    @property
    def cy(self) -> int:
        return self.y + max(1, self.height) // 2


@dataclass(slots=True)
class Reading:
    """One fresh observation: the frame, its words, and when it was taken."""

    at: str
    frame_path: str = ""
    width: int = 0
    height: int = 0
    handle: int = 0
    pid: int = 0
    words: tuple[Word, ...] = ()
    text: str = ""
    digest: str = ""
    reason: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.words)

    def signature(self) -> tuple[str, ...]:
        """A coarse screen identity: the recognised words, order-insensitive.

        Used to recognise "the screen I was taught on" and "the screen did not
        change". Deliberately token-based rather than a pixel hash, because a
        blinking cursor must not look like a different room.
        """
        return tuple(sorted({word.text.lower() for word in self.words if word.text.strip()}))


def _line_height(words: Sequence[Mapping[str, Any]]) -> int:
    """The text height to assume, measured from the words' own vertical span.

    Some OCR words arrive with an origin and no size. Their coordinates are in
    the CLIENT's logical space (this build's retina capture is 2906x1871 while
    the words sit inside 1440x900), so the frame's pixel height is the wrong
    scale to size them with; the words' own extent is the right one.
    """
    ys = [int(item.get("y") or 0) for item in words]
    span = max(ys) if ys else 0
    if span <= 0:
        return 18
    return max(12, min(56, int(round(span * 0.028))))


def _extent(item: Mapping[str, Any], text: str, *, line_height: int) -> tuple[int, int]:
    """A word's size, synthesised when the OCR reports only its origin.

    Treating a missing width/height as a zero-sized box made ``resolve`` click
    the word's top-left corner — where a menu item's hit area commonly ends — so
    a click could be "sent successfully" onto nothing and the screen never
    changed. A character count puts the target in the MIDDLE of the word, which
    is where a person aims.
    """
    width = int(item.get("width") or 0)
    height = int(item.get("height") or 0)
    if width > 0 and height > 0:
        return width, height
    line = height or line_height
    if width <= 0:
        width = max(line // 2, int(round(len(text) * line * 0.62)))
    return width, line


def _reading_from(frame: Mapping[str, Any], words: Sequence[Mapping[str, Any]], digest: str) -> Reading:
    line = _line_height(words)
    parsed = tuple(
        Word(
            text=str(item.get("text") or ""),
            x=int(item.get("x") or 0),
            y=int(item.get("y") or 0),
            width=_extent(item, str(item.get("text") or ""), line_height=line)[0],
            height=_extent(item, str(item.get("text") or ""), line_height=line)[1],
            line=int(item.get("line") or 0),
        )
        for item in words
        if str(item.get("text") or "").strip()
    )
    return Reading(
        at=str(frame.get("observed_at") or ""),
        frame_path=str(frame.get("path") or ""),
        width=int(frame.get("width") or 0),
        height=int(frame.get("height") or 0),
        handle=int(frame.get("handle") or 0),
        pid=int(frame.get("pid") or 0),
        words=parsed,
        text=" ".join(word.text for word in parsed),
        digest=digest,
        reason=str(frame.get("reason") or ""),
    )


# ── plans ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Step:
    """One bounded action in a plan, with what success looks like.

    ``click`` resolves its target by READING the screen; ``click_at`` replays a
    point a person actually clicked, which is only meaningful on the screen it
    was recorded on (the learned-skill signature is what guarantees that).
    """

    kind: str  # click | click_at | type | key | hold | wait_text | wait
    target: str = ""
    value: str = ""
    duration_ms: int = 0
    alternatives: tuple[str, ...] = ()
    expect: str = ""
    note: str = ""
    x: int | None = None
    y: int | None = None
    #: Offset from the RESOLVED anchor's centre, in window pixels. A field with
    #: no readable text of its own (an empty input box) is reached from the
    #: label that IS readable — the label's position is still measured from the
    #: live frame, so the click moves with the dialog instead of being a
    #: hard-coded screen point.
    dx: int = 0
    dy: int = 0
    optional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "value": self.value,
            "duration_ms": self.duration_ms,
            "alternatives": list(self.alternatives),
            "expect": self.expect,
            "x": self.x,
            "y": self.y,
            "dx": self.dx,
            "dy": self.dy,
            "optional": self.optional,
        }


@dataclass(frozen=True, slots=True)
class Task:
    """A named, repeatable piece of play for one game."""

    name: str
    summary: str
    steps: tuple[Step, ...]
    keywords: tuple[str, ...] = ()
    success_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "keywords": list(self.keywords),
            "success_text": self.success_text,
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass(frozen=True, slots=True)
class GameProfile:
    """What the pilot knows about one game, expressed as measurements.

    ``tasks`` are the game-specific capabilities. A task's steps name the TEXT
    that was read on this build of the game; ``alternatives`` carry the other
    spellings the same button has had, and an unresolved target is a stated
    refusal rather than a blind click.
    """

    name: str
    window_title: str
    tasks: tuple[Task, ...]
    launch_target: str = ""
    #: The game's own controls, as (action, keys) pairs. The overlay draws this
    #: legend so the operator can see what the agent is allowed to send and what
    #: the game expects; it is the profile's knowledge, not a guess.
    controls: tuple[tuple[str, str], ...] = ()
    #: Arguments that make the engine run BORDERLESS-WINDOWED.
    #:
    #: Measured on Supermarket Together: launched by Steam into exclusive
    #: fullscreen (or switched there with Alt+Enter) the process is responsive and
    #: renders, but accepts NEITHER synthetic clicks NOR synthetic keys, so every
    #: verified action changed nothing. Launched with these flags the same clicks
    #: open the same menus. A visible overlay also needs a non-exclusive surface,
    #: so this is the mode the pilot launches games in.
    launch_args: tuple[str, ...] = ()

    def task(self, name: str) -> Task | None:
        for task in self.tasks:
            if task.name == name:
                return task
        return None

    def task_for(self, text: str) -> Task | None:
        lowered = text.lower()
        best: tuple[int, Task] | None = None
        for task in self.tasks:
            hits = sum(1 for keyword in task.keywords if keyword and keyword.lower() in lowered)
            if hits and (best is None or hits > best[0]):
                best = (hits, task)
        return best[1] if best else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "window_title": self.window_title,
            "tasks": [task.to_dict() for task in self.tasks],
            "controls": [list(pair) for pair in self.controls],
        }


#: The games installed on this machine, with tasks that are genuinely distinct
#: per title (a shop, a fighting game, a squad shooter) rather than one shared
#: "press a key" script.
PROFILES: tuple[GameProfile, ...] = (
    GameProfile(
        name="Supermarket Together",
        window_title="Supermarket Together",
        launch_target="Supermarket Together",
        launch_args=(
            "-screen-fullscreen", "0",
            "-popupwindow",
            "-screen-width", "1440", "-screen-height", "900",
        ),
        controls=(
            ("Move", "W A S D"),
            ("Interact", "E"),
            ("Place item", "Left click"),
            ("Pick up", "Left click"),
            ("Sprint", "Shift"),
            ("Menu / back", "Esc"),
        ),
        tasks=(
            Task(
                name="open_new_store",
                summary="From the main menu, open the New Store setup screen and name a store.",
                keywords=("new store", "open a store", "open store", "name a store", "start a store"),
                success_text="Choose a name",
                steps=(
                    Step(kind="click", target="New Store", alternatives=("New store", "NEW STORE"),
                         expect="name"),
                    Step(kind="wait_text", target="Choose a name", duration_ms=8000,
                         alternatives=("Store name", "Name", "Enter a name")),
                    # The dialog's input box has no label of its own; its
                    # placeholder is faint OCR, so the field is reached from the
                    # TITLE above it — measured from the saved 2880x1800 frame:
                    # the white input band spans x 804..1300, y 262..310, so its
                    # centre is (1052, 286) while the title's resolved centre is
                    # (944, 251) -> (+108, +35). The previous (+210, +64) landed
                    # at (1154, 315), five pixels BELOW the field's bottom edge:
                    # the click fell on the dialog border, the field never took
                    # focus, and 11 correctly-dispatched keys went nowhere. The
                    # saved frame showed the empty field as proof.
                    Step(kind="click", target="Choose a name", dx=110, dy=34,
                         alternatives=("Enter text", "Enter text..."),
                         note="focus the store-name field under the dialog title"),
                    # The anchor is the dialog TITLE, not the placeholder: once
                    # the field holds text the placeholder is gone, and a step
                    # that can only find the field while it is empty can never
                    # refocus it to correct what it typed.
                    Step(kind="type", value="novaforge", target="Choose a name", dx=110, dy=34,
                         note="type the store name into the focused field"),
                ),
            ),
            Task(
                name="open_options",
                summary="Open the Options panel and confirm its Graphics tab is on screen.",
                keywords=("options", "settings", "graphics"),
                success_text="Graphics",
                steps=(
                    # Measured: the Options panel draws the three column headings
                    # "Graphics" / "Controls" / "Audio" plus "General"; the word
                    # does not exist on the main menu, so it is real evidence.
                    Step(kind="click", target="Options", alternatives=("OPTIONS", "Settings"),
                         expect="Graphics"),
                ),
            ),
            Task(
                name="close_options",
                summary="Leave the Options panel with its own Back button, returning to the menu.",
                keywords=("close options", "back", "leave settings", "return to menu"),
                success_text="Exit",
                steps=(
                    # Escape does NOT close this panel (measured); the Back
                    # button beside Apply is the control the game actually has.
                    Step(kind="click", target="Back", alternatives=("BACK",),
                         expect="Exit", note="Back is the panel's own way out"),
                ),
            ),
            Task(
                name="open_load_store",
                summary="Open the Load Store save list from the main menu.",
                keywords=("load store", "load a store", "load save", "continue"),
                steps=(
                    Step(kind="click", target="Load Store",
                         alternatives=("Load store", "LOAD STORE"),
                         note="the save list replaces the menu's poster art"),
                ),
            ),
            Task(
                name="open_join_store",
                summary="Open the Join Store lobby list from the main menu.",
                keywords=("join store", "join a store", "multiplayer", "lobby"),
                steps=(
                    Step(kind="click", target="Join Store",
                         alternatives=("Join store", "JOIN STORE"),
                         note="the lobby panel lists stores by owner name"),
                ),
            ),
            Task(
                name="walk_forward",
                summary="Move the character forward for two seconds and confirm the view changed.",
                keywords=("walk", "move forward", "explore"),
                steps=(Step(kind="hold", value="w", duration_ms=2000, note="walk forward"),),
            ),
        ),
    ),
    GameProfile(
        name="TEKKEN 8",
        window_title="TEKKEN 8",
        launch_target="TEKKEN 8",
        controls=(
            ("Left punch", "J"),
            ("Right punch", "K"),
            ("Left kick", "U"),
            ("Right kick", "I"),
            ("Block", "S"),
            ("Pause", "Esc"),
        ),
        tasks=(
            Task(
                name="open_practice",
                summary="Reach the offline Practice mode from the main menu.",
                keywords=("practice", "training", "offline"),
                success_text="PRACTICE",
                steps=(
                    Step(kind="click", target="PRACTICE", alternatives=("Practice", "Practice Mode"),
                         expect="practice"),
                ),
            ),
            Task(
                name="throw_punch",
                summary="Throw a basic attack and confirm the fight state changed.",
                keywords=("punch", "attack", "hit"),
                steps=(
                    Step(kind="key", value="j", note="basic left punch"),
                    Step(kind="key", value="k", note="basic right punch"),
                ),
            ),
        ),
    ),
    GameProfile(
        name="Freedom Fighters",
        window_title="Freedom Fighters",
        launch_target="Freedom Fighters",
        controls=(
            ("Move", "W A S D"),
            ("Look / aim", "Mouse"),
            ("Fire", "Left click"),
            ("Jump", "Space"),
            ("Crouch", "C"),
            ("Use / pick up", "E"),
            ("Menu / back", "Esc"),
        ),
        tasks=(
            Task(
                name="start_new_game",
                summary="Start a new campaign from the main menu.",
                keywords=("new game", "start game", "campaign", "play"),
                steps=(
                    Step(kind="click", target="New Game", alternatives=("NEW GAME", "Start Game"), expect="load"),
                    Step(kind="wait_text", target="", duration_ms=6000, note="wait for the level to load"),
                ),
            ),
            Task(
                name="move_and_look",
                summary="Walk forward and look around, confirming the world responded.",
                keywords=("walk", "move", "look", "explore"),
                steps=(
                    Step(kind="hold", value="w", duration_ms=1500, note="walk forward"),
                    Step(kind="key", value="space", note="jump"),
                ),
            ),
        ),
    ),
)


def profile_for(title: str) -> GameProfile | None:
    lowered = str(title or "").lower()
    for profile in PROFILES:
        if profile.window_title.lower() in lowered or lowered in profile.window_title.lower():
            return profile
    return None


# ── instructions ───────────────────────────────────────────────────────────


_CLICK = re.compile(r"\b(?:click|open|go to|tap|select|choose)\s+(?:the\s+)?(.+)", re.I)
_TYPE = re.compile(r"\b(?:type|enter|write)\s+[\"']?(.+?)[\"']?\s*(?:into|in)?\s*$", re.I)
_PRESS = re.compile(r"\b(?:press|hit|tap|push)\s+(?:the\s+)?([a-z0-9]+)\s*(?:key)?", re.I)
_HOLD = re.compile(r"\bhold\s+(?:the\s+)?([a-z0-9]+)\s*(?:key)?(?:\s+for\s+([\d.]+)\s*(?:s|sec|seconds?)?)?", re.I)
_WAIT = re.compile(r"\bwait\s+for\s+(?:the\s+)?(.+)", re.I)

_SPLIT = re.compile(r"\s*(?:,\s*then\s+|;\s*|\band then\b|\bthen\b)\s*", re.I)

#: A leading verb the NLU layer leaves inside an entity value ("press escape"),
#: and the filler noun people append to a screen label ("the options menu").
#: Both are removed before the label is looked for ON SCREEN, because "options
#: menu" is not a line of text the game draws — it draws "Options".
_CLEAN_ARG = re.compile(
    r"^(?:the\s+|please\s+)?(?:click|open|go\s+to|tap|select|choose|press|hit|push|type|enter|write)\s+",
    re.I,
)
_CLEAN_TAIL = re.compile(r"\s+(?:menu|button|tab|screen|option|item|please)$", re.I)


def parse_instruction(text: str) -> tuple[tuple[Step, ...], str]:
    """Read one operator instruction into steps, or say why it cannot be read.

    This is the natural-language door, and it is deterministic on purpose: the
    operator's words are matched against the action vocabulary this build can
    actually perform. An unreadable instruction returns no steps and a reason,
    which the overlay shows — it is never turned into a guessed click.
    """
    raw = " ".join(str(text or "").split())
    if not raw:
        return (), "The instruction is empty."
    steps: list[Step] = []
    for clause in _SPLIT.split(raw):
        piece = clause.strip()
        if not piece:
            continue
        match = _HOLD.search(piece)
        if match:
            seconds = float(match.group(2) or 1.0)
            steps.append(Step(kind="hold", value=match.group(1).lower(),
                              duration_ms=int(max(0.1, min(10.0, seconds)) * 1000)))
            continue
        match = _WAIT.search(piece)
        if match:
            steps.append(Step(kind="wait_text", target=match.group(1).strip(), duration_ms=10000))
            continue
        match = _TYPE.search(piece)
        if match and match.group(1).strip():
            steps.append(Step(kind="type", value=match.group(1).strip()))
            continue
        match = _PRESS.search(piece)
        if match and not _CLICK.search(piece):
            steps.append(Step(kind="key", value=match.group(1).lower()))
            continue
        match = _CLICK.search(piece)
        if match:
            target = match.group(1).strip().strip("\"'")
            target = re.sub(r"\s+(?:button|tab|option|menu item|item)$", "", target, flags=re.I)
            if target:
                steps.append(Step(kind="click", target=target))
                continue
        return (), (
            f"I could not read {piece!r} as an action. Say what to click "
            "(\"click New Store\"), type (\"type Market\"), or press (\"press enter\")."
        )
    return tuple(steps), ""


# ── learned skills ─────────────────────────────────────────────────────────


class LearnedSkills:
    """What a person taught the pilot by playing, stored as observable steps.

    A skill is ``{game, objective, signature, steps}``: the words that were on
    screen when the person started, the objective they were pursuing, and the
    steps their own input amounted to. Replay requires the SAME objective and a
    screen whose words mostly match, so a skill learned in one room cannot fire
    in another.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._skills: list[dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a missing or broken file is "nothing learned"
            self._skills = []
            return
        rows = data.get("skills") if isinstance(data, dict) else data
        self._skills = [row for row in rows or [] if isinstance(row, dict)]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"skills": self._skills}, indent=1), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001 - teaching must not fail the session
            pass

    def count(self) -> int:
        return len(self._skills)

    def teach(self, *, game: str, objective: str, signature: Sequence[str],
              steps: Sequence[Step]) -> dict[str, Any]:
        record = {
            "game": game,
            "objective": objective.strip(),
            "signature": sorted({token.lower() for token in signature if token}),
            "steps": [step.to_dict() for step in steps],
            "taught_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        # One row per (game, objective): teaching the same thing again REPLACES
        # it, so a corrected demonstration does not leave the old one winning.
        self._skills = [
            row for row in self._skills
            if not (
                str(row.get("game")) == record["game"]
                and str(row.get("objective")).lower() == str(record["objective"]).lower()
            )
        ]
        self._skills.append(record)
        self.save()
        return record

    def match(self, *, game: str, objective: str, signature: Sequence[str],
              minimum: float = 0.6) -> list[Step] | None:
        current = {token.lower() for token in signature if token}
        if not current:
            return None
        for row in reversed(self._skills):
            if str(row.get("game")) != game:
                continue
            if str(row.get("objective", "")).lower() != objective.strip().lower():
                continue
            taught = {str(token).lower() for token in row.get("signature") or []}
            if not taught:
                continue
            overlap = len(current & taught) / max(1, min(len(current), len(taught)))
            if overlap < minimum:
                continue
            steps: list[Step] = []
            for item in row.get("steps") or []:
                if not isinstance(item, Mapping):
                    continue
                steps.append(
                    Step(
                        kind=str(item.get("kind") or ""),
                        target=str(item.get("target") or ""),
                        value=str(item.get("value") or ""),
                        duration_ms=int(item.get("duration_ms") or 0),
                        alternatives=tuple(item.get("alternatives") or ()),
                        expect=str(item.get("expect") or ""),
                        x=int(item["x"]) if item.get("x") is not None else None,
                        y=int(item["y"]) if item.get("y") is not None else None,
                        dx=int(item.get("dx") or 0),
                        dy=int(item.get("dy") or 0),
                        optional=bool(item.get("optional")),
                    )
                )
            if steps:
                return steps
        return None

    def rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._skills]


# ── the engine ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class StepResult:
    """What happened to one step: decided, dispatched, verified — separately."""

    step: Step
    status: str = "decided"  # decided | dispatched | verified | failed | skipped | blocked
    reason: str = ""
    target: dict[str, Any] = field(default_factory=dict)
    before: str = ""
    after: str = ""
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step.to_dict(),
            "status": self.status,
            "reason": self.reason,
            "target": dict(self.target),
            "before": self.before,
            "after": self.after,
            "latency_ms": self.latency_ms,
        }


class PilotEngine:
    """Runs tasks on a live game through NovaForge's own session and control path."""

    def __init__(self, app: Any, *, learned_path: str | Path = "data/novaforge_learned.json") -> None:
        self.app = app
        self.learned = LearnedSkills(learned_path)
        self.title = ""
        self.profile: GameProfile | None = None
        self.session_id = ""
        self.state = "idle"  # idle | launching | running | paused | takeover | stopped | failed
        self.objective = ""
        self.task: Task | None = None
        self.last_decision = ""
        self.last_action = ""
        self.last_outcome = ""
        self.last_verification = ""
        self.obstacle = ""
        self.history: list[dict[str, Any]] = []
        self._pause = asyncio.Event()
        self._pause.set()  # set == running
        self._stop = asyncio.Event()
        self._takeover = False
        self._frame_handle = 0
        self._frame_size: tuple[int, int] = (0, 0)
        #: Client origin and size in the INJECTING worker's physical-pixel
        #: space, cached with each frame so a click is mapped into the space
        #: the input actually travels in.
        self._client_origin: tuple[int, int] | None = None
        self._client_size: tuple[int, int] = (0, 0)
        self._on_event: Callable[[str, dict[str, Any]], None] | None = None

    # -- surface ------------------------------------------------------------

    def on_event(self, callback: Callable[[str, dict[str, Any]], None] | None) -> None:
        self._on_event = callback

    def _emit(self, kind: str, **payload: Any) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(kind, payload)
        except Exception:  # noqa: BLE001 - a listener cannot break play
            pass

    def status(self) -> dict[str, Any]:
        return {
            "game": self.title,
            "profile": self.profile.name if self.profile else "",
            "state": self.state,
            "objective": self.objective,
            "task": self.task.name if self.task else "",
            "decision": self.last_decision,
            "action": self.last_action,
            "outcome": self.last_outcome,
            "verification": self.last_verification,
            "obstacle": self.obstacle,
            "learned": self.learned.count(),
            "history": list(self.history[-8:]),
        }

    # -- lifecycle ----------------------------------------------------------

    async def _titles(self) -> list[str]:
        """Every visible window title right now (an unreadable probe is empty)."""
        try:
            windows = await self.app.novaforge.windows()
        except Exception:  # noqa: BLE001 - an unreadable probe is "nothing seen"
            return []
        return [str(row.get("title") or "") for row in windows.get("windows") or []]

    async def launch(self, game: str) -> None:
        """Launch (or attach to) a game and open a verified session on its window.

        A game that is ALREADY running is attached to: launching it again would
        either refuse (a duplicate instance) or restart what the operator is
        looking at. A launch the service REFUSED is reported as that refusal —
        the old wait-then-blame-the-window path turned "only 2.0 GB free" into
        "no window appeared after 45 s", which sends every later decision after
        the wrong problem.
        """
        self.title = game
        self.profile = profile_for(game)
        self.state = "launching"
        self.obstacle = ""
        exact = [title for title in await self._titles() if game.lower() in title.lower()]
        if exact:
            self.last_action = f"attach to the running {game!r} window"
            self.last_outcome = "already running; nothing was launched"
        elif self.profile is not None and self.profile.launch_args and await self._launch_windowed(game):
            self.last_decision = (
                f"Launch {game} borderless-windowed so input and the overlay both work."
            )
            self.last_action = "launch (borderless-windowed) " + " ".join(
                self.profile.launch_args
            )
            self.last_outcome = "started by the pilot"
            for _ in range(45):
                exact = [t for t in await self._titles() if game.lower() in t.lower()]
                if exact:
                    break
                await asyncio.sleep(1.0)
            if not exact:
                self.state = "failed"
                self.obstacle = f"No window matching {game!r} appeared after 45 s."
                self._emit("failed", reason=self.obstacle)
                return
        else:
            self.last_decision = f"Launch {game} and bind a session to its own window."
            result = await self.app.games.launch(game)
            self.last_action = f"launch {game}: {result.get('status')}"
            self.last_outcome = str(result.get("reason") or "")
            if not result.get("launched"):
                # A refused launch is NOT a window timeout: the reason the
                # service gave is the whole diagnosis, and nothing will appear.
                self.state = "failed"
                self.obstacle = f"launch refused: {self.last_outcome}"
                self._emit("failed", reason=self.obstacle)
                return
            for _ in range(45):
                exact = [title for title in await self._titles() if game.lower() in title.lower()]
                if exact:
                    break
                await asyncio.sleep(1.0)
        if not exact:
            self.state = "failed"
            self.obstacle = f"No window matching {game!r} appeared after 45 s."
            self._emit("failed", reason=self.obstacle)
            return
        target = exact[0]
        if len(exact) > 1:
            # Several windows match: prefer the one whose title EQUALS the game,
            # so a video or a launcher never becomes the target.
            target = next((t for t in exact if t.strip().lower() == game.strip().lower()), exact[0])
        started = await self.app.novaforge.start(
            {
                "name": f"pilot:{game}",
                "objective": "Play the game as instructed.",
                "success_criteria": "the operator's task is completed",
                "target_title": target,
                "time_limit_seconds": 1800,
                "max_actions": 400,
                "frame_interval_seconds": 0.75,
            }
        )
        self.session_id = (
            started["session"]["session_id"] if "session" in started else started["session_id"]
        )
        self.state = "running"
        self.last_verification = f"session {self.session_id} on {target!r}"
        self._emit("session", session_id=self.session_id, target=target)

    async def _launch_windowed(self, game: str) -> bool:
        """Start the game's executable with the profile's windowed arguments.

        False means "this profile has no measured arguments, use the ordinary
        launch path" — never a silent failure, because the caller then reports the
        real launch result instead of claiming a mode it did not use.
        """
        assert self.profile is not None
        try:
            resolution = await self.app.games.catalog.resolve(game)
        except Exception:  # noqa: BLE001 - no catalog answer means the normal path
            return False
        artifact = ((resolution or {}).get("entry") or {}).get("artifact") or {}
        target = str(artifact.get("launch_target") or "")
        if not target or not Path(target).exists():
            return False
        try:
            subprocess.Popen(  # noqa: S603 - a resolved, verified install path
                [target, *self.profile.launch_args],
                cwd=str(Path(target).parent),
            )
        except Exception:  # noqa: BLE001
            return False
        return True

    async def ensure_session(self) -> bool:
        if self.session_id:
            return True
        if not self.title:
            return False
        await self.launch(self.title)
        return bool(self.session_id)

    async def stop(self) -> None:
        self.state = "stopped"
        self._stop.set()
        self._pause.set()
        if self.session_id:
            try:
                await self.app.novaforge.stop(self.session_id)
            except Exception as exc:  # noqa: BLE001 - a stop failure is reported, not hidden
                self.last_outcome = f"stop failed: {type(exc).__name__}: {exc}"
            self.session_id = ""
        self._emit("stopped", reason="operator stop")

    def pause(self) -> None:
        if self.state in {"running", "takeover"}:
            self._pause.clear()
            self.state = "paused"
            self.last_decision = "Paused by the operator; no new gameplay input is sent."
            self._emit("paused")

    def resume(self) -> None:
        self._takeover = False
        self._pause.set()
        self.state = "running"
        self.last_decision = "Resumed; the next decision reads a FRESH frame before acting."
        self._emit("resumed")

    def take_over(self) -> None:
        """The operator plays; the agent sends nothing until handed back."""
        self._takeover = True
        self._pause.clear()
        self.state = "takeover"
        self.last_decision = "Operator takeover: the agent is not sending input."
        self._emit("takeover")

    async def hand_back(self) -> None:
        """The agent takes over again from whatever screen the operator left."""
        self._takeover = False
        self._pause.set()
        self.state = "running"
        self.last_decision = "Handed back: reading the current screen before deciding."
        self._emit("handback")

    # -- observation --------------------------------------------------------

    async def observe(self) -> Reading:
        if not self.session_id:
            return Reading(at="", reason="no session")
        frame: dict[str, Any] = {}
        try:
            frame = await asyncio.wait_for(
                self.app.novaforge.capture_frame_for_observation(self.title),
                timeout=OBSERVE_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - an unreadable frame is a stated gap
            return Reading(at="", reason=f"capture failed: {type(exc).__name__}: {exc}")
        path = str(frame.get("path") or "")
        if not path:
            return Reading(at="", reason=str(frame.get("reason") or "no frame"))
        words: Sequence[Mapping[str, Any]] = []
        try:
            words = await asyncio.wait_for(
                self.app.novaforge.read_frame_text(path), timeout=OBSERVE_TIMEOUT
            )
            # The reader answers None when no engine is wired: that is "nothing
            # was read", not "read an empty list". Without this the very next
            # frame raised TypeError and took the whole task down mid-run.
            words = words or []
        except Exception as exc:  # noqa: BLE001
            return Reading(at="", frame_path=path, reason=f"OCR failed: {type(exc).__name__}: {exc}")
        digest = ""
        try:
            from hashlib import sha256

            digest = sha256(Path(path).read_bytes()).hexdigest()[:16]
            # Remember which window these coordinates belong to, and the frame's
            # own pixel size: a click has to be mapped out of IMAGE space.
            self._frame_handle = int(frame.get("handle") or 0)
            self._frame_size = (int(frame.get("width") or 0), int(frame.get("height") or 0))
            await self._cache_placement(self._frame_handle)

        except Exception:  # noqa: BLE001
            digest = ""
        return _reading_from(frame, words, digest)

    @classmethod
    def shows_value(cls, screen_text: str, value: str) -> bool:
        """Whether a text field visibly shows ``value``.

        Spaces and caret artefacts are removed before comparing: OCR renders a
        live caret as a trailing glyph ("novaforgel") and can drop the space
        inside a typed name, neither of which means the value did not land. The
        comparison still requires the whole value, in order, so a partially
        typed name is not called a success.
        """
        compact_screen = re.sub(r"[^0-9a-z]", "", screen_text.lower())
        compact_value = re.sub(r"[^0-9a-z]", "", value.lower())
        return bool(compact_value) and compact_value in compact_screen

    @staticmethod
    def _has_word(haystack: str, token: str) -> bool:
        """True when ``token`` appears as a WHOLE word in ``haystack``.

        Substring matching is how "Back" resolved to "Backups" and "Exit" to
        "Exit Game": the click went to a different control than the one the
        decision named, and the step still reported that it had clicked.
        """
        return bool(re.search(rf"(?<![0-9a-z]){re.escape(token)}(?![0-9a-z])", haystack))

    @classmethod
    def resolve(cls, reading: Reading, target: str, alternatives: Sequence[str] = ()) -> dict[str, Any] | None:
        """Find the words for a target and return their centre, or None.

        Multi-word targets are matched as a LINE that contains every token, in
        any order, so "New Store" is found even when OCR splits it across two
        words; the click point is the centre of exactly those words. Tokens match
        whole words only, so "Back" never resolves to "Backups".
        """
        wanted = [target, *alternatives]
        for phrase in wanted:
            tokens = [token for token in re.split(r"\s+", phrase.strip().lower()) if token]
            if not tokens:
                continue
            for line in sorted({word.line for word in reading.words}):
                line_words = [word for word in reading.words if word.line == line]
                haystack = " ".join(word.text.lower() for word in line_words)
                if not all(cls._has_word(haystack, token) for token in tokens):
                    continue
                matched = [
                    word for word in line_words
                    if any(cls._has_word(word.text.lower(), token) for token in tokens)
                ]
                if not matched:
                    continue
                # A one- or two-character OCR token is a fragment of a sentence,
                # never a button. The suite once reported "clicked 'a'" for a
                # Back step and counted the screen change as success: the click
                # went into the DLC banner at the bottom of the screen while the
                # step claimed it had used the panel's Back button.
                if len(phrase.strip()) > 2 and len("".join(word.text for word in matched).strip()) <= 2:
                    continue
                x0 = min(word.x for word in matched)
                x1 = max(word.x + word.width for word in matched)
                y0 = min(word.y for word in matched)
                y1 = max(word.y + word.height for word in matched)
                return {
                    "text": " ".join(word.text for word in matched),
                    "x": (x0 + x1) // 2,
                    "y": (y0 + y1) // 2,
                    "box": [x0, y0, x1, y1],
                }
        return None

    # -- input --------------------------------------------------------------

    async def _control(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = await self.app.novaforge.control_execute(self.session_id, dict(payload))
        payload_status = str(result.get("status") or "")
        if payload_status and payload_status not in {"completed", "dispatched"}:
            self.obstacle = (
                f"control {payload.get('kind')} refused: {result.get('reason') or payload_status}"
            )
        return dict(result)

    async def _focus(self) -> bool:
        """Bring the game to the front, retrying once; True when it is in front.

        Focus is measurable (the control layer verifies GetForegroundWindow), so
        a failed attempt is retried rather than ignored: a shell flyout that
        owned the foreground is dismissed by the retry, and a pointer that is
        still not in front must not be followed by a click that lands elsewhere.
        """
        for attempt in range(2):
            try:
                result = await self._control({"kind": "focus"})
            except Exception as exc:  # noqa: BLE001 - a refused focus is a result
                result = {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
            if str(result.get("status") or "") == "completed":
                await asyncio.sleep(0.25)
                return True
            self.obstacle = f"focus attempt {attempt + 1} failed: {result.get('reason') or ''}"
            await asyncio.sleep(0.35)
        return False

    async def _cache_placement(self, handle: int) -> None:
        """Remember where the target's CLIENT area sits, in PHYSICAL pixels.

        The placement must come from the SAME DPI-aware worker that injects the
        input. Reading it here with ``ctypes`` is wrong on a scaled display:
        this interpreter is NOT DPI-aware, so Windows virtualises GetClientRect
        and ClientToScreen to 1440x900 while the capture — and SetCursorPos in
        the worker — speak 2880x1800 physical pixels. The halved points landed
        every click in the top-left quarter of the screen: on 2026-11-01 the
        "Back" button of the New Store dialog, at frame (933, 622), was clicked
        at (466, 311) and the dialog never closed, while the same mapping sent
        a "Load Store" click onto "New Store" (the operator's first report).
        """
        if not handle:
            return
        runner = getattr(getattr(self.app, "desktop", None), "runner", None)
        # Address the worker by handle THROUGH the factory that builds the
        # injecting worker: ``runner.window_geometry`` takes a title, and a title
        # can resolve to a different window than the one the frame came from.
        factory = getattr(runner, "input_worker", None)
        if not callable(factory):
            return
        try:
            geometry = await factory().window_geometry(handle=handle)
        except Exception:  # noqa: BLE001 - no placement is "map raw", not a crash
            return
        if geometry is None:
            return
        try:
            origin = tuple(int(value) for value in geometry.client_origin)
            client = tuple(int(value) for value in geometry.client)
        except Exception:  # noqa: BLE001
            return
        if len(origin) != 2 or len(client) != 4 or client[2] <= 0 or client[3] <= 0:
            return
        self._client_origin = (origin[0], origin[1])
        self._client_size = (client[2], client[3])

    def _frame_to_screen(self, x: int, y: int) -> tuple[int, int]:
        """Map a point in the FRAME IMAGE onto the screen's PHYSICAL pixels.

        The frame's pixel size and the client's physical size differ only when
        the capture and the window disagree; dividing by that measured ratio
        puts the click on the pixels that were read. Both numbers come from the
        worker that injects the input, so image space and input space are one
        space rather than two that happen to agree at 100% zoom.
        """
        frame = getattr(self, "_frame_size", (0, 0))
        origin = getattr(self, "_client_origin", None)
        client = getattr(self, "_client_size", (0, 0))
        if not frame[0] or not frame[1] or origin is None or not client[0] or not client[1]:
            return int(x), int(y)
        return (
            int(origin[0]) + int(round(x * client[0] / frame[0])),
            int(origin[1]) + int(round(y * client[1] / frame[1])),
        )

    async def click(self, x: int, y: int) -> dict[str, Any]:
        if not await self._focus():
            # Nothing may be clicked into a window that is not in front: the
            # same coordinates would hit whatever IS, which is how a "clicked"
            # step could land in a different application entirely.
            return {
                "status": "rejected",
                "reason": f"the game window could not be brought to the foreground ({self.obstacle})",
            }
        x, y = self._frame_to_screen(x, y)
        await self._control({"kind": "mouse_move", "x": x, "y": y})
        await asyncio.sleep(0.15)
        await self._control({"kind": "mouse_press", "x": x, "y": y, "button": "left"})
        await asyncio.sleep(0.08)
        return await self._control({"kind": "mouse_release", "x": x, "y": y, "button": "left"})

    async def tap(self, key: str) -> dict[str, Any]:
        if not await self._focus():
            return {
                "status": "rejected",
                "reason": f"the game window could not be brought to the foreground ({self.obstacle})",
            }
        await self._control({"kind": "key_press", "key": key})
        await asyncio.sleep(0.06)
        return await self._control({"kind": "key_release", "key": key})

    async def hold(self, key: str, duration_ms: int) -> dict[str, Any]:
        if not await self._focus():
            return {
                "status": "rejected",
                "reason": f"the game window could not be brought to the foreground ({self.obstacle})",
            }
        return await self._control({"kind": "key_hold", "key": key, "duration_ms": duration_ms})

    async def clear_field(self, *, limit: int = 24) -> str:
        """Erase what an input box already holds, bounded by ``limit`` presses.

        Without this, retrying a typed value APPENDS to the previous attempt and
        the field ends up holding the value twice, which no verification can
        honestly call success. ``end`` comes first because Backspace deletes to
        the LEFT of the caret only, and a click into existing text leaves the
        caret in the middle of it.

        Returns an empty string on success, or the refusal reason. A silent
        return is what let a field keep its old contents while the step claimed
        it had cleared them.
        """
        end = await self._control({"kind": "key_hold", "key": "end", "duration_ms": 15})
        if str(end.get("status")) != "completed":
            return f"the caret could not be moved to the end of the field: {end.get('reason')}"
        # One long hold deletes a burst through the OS repeat rate; the presses
        # after it cover a field that ignores repeat. Each individual press is a
        # full control action (~0.3 s), so 80 of them would cost half a minute.
        bulk = await self._control({"kind": "key_hold", "key": "backspace", "duration_ms": 350})
        if str(bulk.get("status")) != "completed":
            return f"Backspace was refused: {bulk.get('reason')}"
        for _ in range(max(1, limit)):
            result = await self._control({"kind": "key_hold", "key": "backspace", "duration_ms": 15})
            if str(result.get("status")) != "completed":
                return f"Backspace was refused: {result.get('reason')}"
            await asyncio.sleep(0.01)
        return ""

    async def type_text(self, text: str) -> dict[str, Any]:
        """Type a value by holding each character's own key briefly.

        ``key_hold`` is the primitive because a press and a release are SEPARATE
        actions and each action cleans up the input it pressed — a press that
        ends by releasing its own key made the following release fail with "the
        key is not held". One hold per character presses and releases inside a
        single action, which is what actually reaches the game.

        Only lower-case letters, digits and `` -_.`` can be produced: the
        control interface addresses keys and has no way to hold shift across two
        actions. Anything else is refused by name rather than silently dropped.
        """
        lowered = text.lower()
        bad = {char for char in lowered if char not in TYPABLE}
        if bad:
            return {"status": "failed", "reason": f"cannot type {sorted(bad)}"}
        if not await self._focus():
            return {"status": "failed", "reason": "the game window is not in the foreground"}
        for char in lowered:
            key = "space" if char == " " else char
            await self._control({"kind": "key_hold", "key": key, "duration_ms": 25})
            await asyncio.sleep(0.02)
        return {"status": "completed", "reason": f"typed {len(lowered)} characters"}

    # -- waiting and verification ------------------------------------------

    async def read_region(
        self,
        frame_path: str,
        x: int,
        y: int,
        *,
        half_w: int = 460,
        half_h: int = 70,
        scale: int = 2,
    ) -> str:
        """Read the text of a SMALL region, enlarged.

        A text field's line is a few dozen pixels tall inside a 2880-pixel
        frame, and the whole-frame OCR pass drops it about half the time — the
        pixels are right there (the saved frame shows the typed name), so
        calling the value absent would be a false negative. The crop is read
        from the SAME frame; nothing is guessed.
        """
        try:
            from PIL import Image

            with Image.open(frame_path) as image:
                left = max(0, int(x) - half_w)
                top = max(0, int(y) - half_h)
                right = min(image.width, int(x) + half_w)
                bottom = min(image.height, int(y) + half_h)
                if right - left < 8 or bottom - top < 8:
                    return ""
                crop = image.crop((left, top, right, bottom))
                if scale > 1:
                    crop = crop.resize(
                        (crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS
                    )
                out = Path(frame_path).with_name(Path(frame_path).stem + "_region.png")
                crop.save(out)
            words = await asyncio.wait_for(
                self.app.novaforge.read_frame_text(str(out)), timeout=OBSERVE_TIMEOUT
            )
            return " ".join(str(item.get("text") or "") for item in words)
        except Exception:  # noqa: BLE001 - an unreadable region is "no evidence"
            return ""

    async def wait_for(self, targets: Sequence[str], timeout: float) -> Reading | None:
        """Poll FRESH frames until one contains any target, or return None."""
        deadline = time.monotonic() + max(0.5, timeout)
        last: Reading | None = None
        while time.monotonic() < deadline:
            reading = await self.observe()
            last = reading
            if reading.usable:
                for target in targets:
                    if target and self.resolve(reading, target):
                        return reading
            await asyncio.sleep(POLL_SECONDS)
        return None

    async def _settle(self, before: Reading, expect: str, timeout: float = VERIFY_TIMEOUT) -> tuple[bool, Reading]:
        """Wait for a change, then say whether it is evidence the step worked."""
        deadline = time.monotonic() + max(1.0, timeout)
        after = before
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_SECONDS)
            after = await self.observe()
            if not after.usable:
                continue
            if expect and self.resolve(after, expect):
                return True, after
            if not expect and (after.digest != before.digest or after.signature() != before.signature()):
                return True, after
        return False, after

    # -- the loop -----------------------------------------------------------

    async def _await_turn(self) -> None:
        while not self._pause.is_set() and not self._stop.is_set():
            await asyncio.sleep(0.2)
        if self._stop.is_set():
            raise asyncio.CancelledError("stopped")

    async def run_steps(self, steps: Sequence[Step], *, label: str) -> dict[str, Any]:
        """Execute a plan, verifying every step against a later frame."""
        results: list[StepResult] = []
        for index, step in enumerate(steps, start=1):
            await self._await_turn()
            if self._stop.is_set():
                break
            record = StepResult(step=step)
            started = time.monotonic()
            self.last_decision = self._describe(step, index, len(steps))
            try:
                await self._execute(step, record)
            except asyncio.CancelledError:
                record.status = "skipped"
                record.reason = "stopped by the operator"
                results.append(record)
                break
            except Exception as exc:  # noqa: BLE001 - a step fault is a result
                record.status = "failed"
                record.reason = f"{type(exc).__name__}: {exc}"
            record.latency_ms = round((time.monotonic() - started) * 1000, 1)
            results.append(record)
            self.history.append({"label": label, **record.to_dict()})
            self._emit("step", step=record.to_dict())
            if record.status in {"failed", "blocked"}:
                self.obstacle = record.reason
                break
        ok = bool(results) and all(row.status in {"verified", "skipped"} for row in results)
        return {
            "label": label,
            "ok": ok,
            "steps": [row.to_dict() for row in results],
            "stopped": self._stop.is_set(),
        }

    async def _execute(self, step: Step, record: StepResult) -> None:
        if step.kind == "click":
            await self._do_click(step, record)
        elif step.kind == "click_at":
            # A point the operator clicked during a demonstration. It is only
            # used when the learned-skill signature matched the current screen.
            before = await self.observe()
            record.before = before.text[:200]
            x = int(step.x or 0)
            y = int(step.y or 0)
            self.last_action = f"click the taught point ({x}, {y})"
            result = await self.click(x, y)
            dispatched = str(result.get("status") or "") == "completed"
            record.status = "dispatched" if dispatched else "failed"
            record.reason = str(result.get("reason") or "")
            if dispatched:
                ok, after = await self._settle(before, step.expect)
                record.after = after.text[:200]
                record.status = "verified" if ok else "unverified"
                record.reason = (
                    "the screen changed after the taught click" if ok
                    else "no visible change after the taught click"
                )
            self.last_outcome = record.reason
            self.last_verification = record.status
        elif step.kind == "type":
            # Typing is verified by the VALUE appearing on screen, never by "the
            # screen changed": a keystroke that lands nowhere still changes the
            # screen (a hover highlight, a blinking caret), which is how a store
            # name that was never entered could be reported as typed.
            self.last_action = f"type {step.value!r}"
            before = await self.observe()
            record.before = before.text[:200]
            # The check below is against the FIELD that was just written, never
            # against whatever the screen already showed: a value left over from
            # an earlier run satisfied the old pre-check and the step "passed"
            # without sending a single keystroke. Always enter the value afresh.
            echoed = False
            attempts: list[str] = []
            # No annotation: ``result`` already exists in this function's scope
            # from the click branch above, and re-declaring it is a mypy error.
            result = {}
            cleared = False
            field: dict[str, Any] | None = None
            for attempt in range(2):
                if step.target and (attempt == 0 or not echoed):
                    # Focus the FIELD first: a dialog's input box is not focused
                    # just because it is open, and typing into an unfocused
                    # dialog goes nowhere while still reporting that keys were
                    # sent.
                    point = self.resolve(before, step.target, step.alternatives)
                    if point is not None:
                        field = {
                            "x": int(point["x"]) + step.dx,
                            "y": int(point["y"]) + step.dy,
                            "text": point["text"],
                        }
                        await self.click(field["x"], field["y"])
                        await asyncio.sleep(0.4)
                        before = await self.observe()
                if not cleared:
                    # Replace, never append: a second attempt into a field that
                    # kept the first one is how "NovaForge" became
                    # "novafornovaforge".
                    problem = await self.clear_field()
                    if problem:
                        record.reason = problem
                    cleared = True
                    if field is not None:
                        # A clear that did NOT clear is a refusal, not a detail:
                        # typing into the old value would "verify" against text
                        # that was already there.
                        probe = await self.observe()
                        leftover = ""
                        if probe.frame_path:
                            leftover = await self.read_region(
                                probe.frame_path, field["x"], field["y"]
                            )
                        if self.shows_value(leftover, step.value):
                            problem = await self.clear_field(limit=40)
                            probe = await self.observe()
                            if probe.frame_path:
                                leftover = await self.read_region(
                                    probe.frame_path, field["x"], field["y"]
                                )
                            if self.shows_value(leftover, step.value):
                                record.status = "failed"
                                record.reason = (
                                    "the field still shows the previous value after clearing; "
                                    f"read: {leftover[:120]!r}"
                                )
                                self.last_outcome = record.reason
                                self.last_verification = "failed"
                                return
                result = await self.type_text(step.value)
                attempts.append(str(result.get("status")))
                if str(result.get("status")) != "completed":
                    break
                after = await self.observe()
                record.after = after.text[:200]
                echoed = self.shows_value(after.text, step.value)
                if not echoed and field is not None and after.frame_path:
                    # The full-frame pass can miss a small field line; read the
                    # field itself, enlarged, from the SAME frame.
                    region = await self.read_region(
                        after.frame_path, field["x"], field["y"]
                    )
                    echoed = self.shows_value(region, step.value)
                    if echoed:
                        record.after = f"field: {region[:180]}"
                if echoed:
                    break
                before = after
            dispatched = bool(attempts) and attempts[-1] == "completed"
            record.status = "verified" if dispatched and echoed else "failed"
            record.reason = (
                "the field shows the typed value" if echoed
                else f"the field never showed {step.value!r} (attempts: {', '.join(attempts) or 'none'})"
            )
            self.last_outcome = record.reason
            self.last_verification = record.status
        elif step.kind == "key":
            self.last_action = f"press {step.value}"
            result = await self.tap(step.value)
            record.status = "dispatched" if str(result.get("status")) == "completed" else "failed"
            record.reason = str(result.get("reason") or "")
            if record.status == "dispatched":
                before = await self.observe()
                ok, after = await self._settle(before, step.expect)
                record.before, record.after = before.text[:200], after.text[:200]
                record.status = "verified" if ok else "unverified"
                record.reason = (
                    f"screen changed after pressing {step.value}" if ok
                    else f"no observable change after pressing {step.value}"
                )
            self.last_outcome = record.reason
            self.last_verification = record.status
        elif step.kind == "hold":
            self.last_action = f"hold {step.value} for {step.duration_ms} ms"
            before = await self.observe()
            result = await self.hold(step.value, step.duration_ms)
            dispatched = str(result.get("status")) == "completed"
            record.status = "dispatched" if dispatched else "failed"
            record.reason = str(result.get("reason") or "")
            if dispatched:
                ok, after = await self._settle(before, "", timeout=4.0)
                record.before, record.after = before.text[:200], after.text[:200]
                record.status = "verified" if ok else "unverified"
                record.reason = (
                    f"screen changed while holding {step.value}" if ok
                    else f"no visible movement while holding {step.value}"
                )
            self.last_outcome = record.reason
            self.last_verification = record.status
        elif step.kind == "wait_text":
            targets = [target for target in (step.target, *step.alternatives) if target]
            self.last_action = f"wait for {targets or 'the screen to change'}"
            before = await self.observe()
            record.before = before.text[:200]
            if not targets:
                await asyncio.sleep(max(1.0, step.duration_ms / 1000))
                after = await self.observe()
                record.after = after.text[:200]
                changed = after.digest != before.digest or after.signature() != before.signature()
                record.status = "verified" if changed else "unverified"
                record.reason = (
                    "the screen changed while waiting" if changed
                    else "nothing changed while waiting"
                )
            else:
                found = await self.wait_for(targets, max(2.0, step.duration_ms / 1000))
                record.status = "verified" if found else ("skipped" if step.optional else "blocked")
                record.reason = (
                    f"found {step.target!r}" if found
                    else f"{step.target!r} never appeared; the screen shows {before.text[:120]!r}"
                )
                if found:
                    record.after = found.text[:200]
            self.last_outcome = record.reason
            self.last_verification = record.status
        elif step.kind == "wait":
            await asyncio.sleep(max(0.2, step.duration_ms / 1000))
            record.status = "verified"
            record.reason = "waited"
        else:
            record.status = "failed"
            record.reason = f"unknown step kind {step.kind!r}"

    async def _do_click(self, step: Step, record: StepResult) -> None:
        reading = await self.observe()
        record.before = reading.text[:200]
        targets = [step.target, *step.alternatives]
        point = self.resolve(reading, step.target, step.alternatives)
        if point is None:
            # A menu that is still drawing is not a missing button: wait for it
            # once, with a fresh frame, before declaring the target absent.
            found = await self.wait_for([target for target in targets if target], 6.0)
            if found is not None:
                reading = found
                point = self.resolve(reading, step.target, step.alternatives)
        if point is None:
            if step.optional:
                # An optional step is "dismiss this dialog if it is open": its
                # absence is the desired state, not a failure.
                record.status = "skipped"
                record.reason = f"{step.target!r} is not on screen; the step did not apply"
                self.last_action = f"skipped {step.target!r} (not on screen)"
                self.last_outcome = record.reason
                self.last_verification = "skipped"
                return
            record.status = "blocked"
            record.reason = (
                f"{step.target!r} is not on screen; read: {reading.text[:160]!r}"
            )
            self.last_action = f"refused to click {step.target!r} (not found)"
            self.last_outcome = record.reason
            self.last_verification = "blocked"
            return
        # The point must actually BE the target: the resolved words are joined
        # back and every word of the request has to appear in them. Without this
        # a step that resolved the wrong line still clicked it and reported the
        # screen change as its own success (the suite logged "clicked 'a'" for a
        # Back step and called it PASS).
        resolved_text = str(point.get("text") or "").lower()
        wanted_tokens = [token for token in re.split(r"\s+", step.target.strip().lower()) if token]
        if wanted_tokens and not all(self._has_word(resolved_text, token) for token in wanted_tokens):
            record.status = "blocked"
            record.reason = (
                f"{step.target!r} resolved to {resolved_text!r}, which is not that control; "
                "nothing was clicked."
            )
            self.last_action = f"refused to click {step.target!r} (resolved {resolved_text!r})"
            self.last_outcome = record.reason
            self.last_verification = "blocked"
            return
        record.target = point
        x, y = int(point["x"]) + step.dx, int(point["y"]) + step.dy
        offset = f" (anchored {step.dx:+d},{step.dy:+d})" if (step.dx or step.dy) else ""
        self.last_action = f"click {point['text']!r} at ({x}, {y}){offset}"
        result = await self.click(x, y)
        dispatched = str(result.get("status") or "") == "completed"
        record.status = "dispatched" if dispatched else "failed"
        record.reason = str(result.get("reason") or "")
        if not dispatched:
            self.last_outcome = f"input refused: {record.reason}"
            self.last_verification = "failed"
            return
        ok, after = await self._settle(reading, step.expect)
        record.after = after.text[:200]
        record.status = "verified" if ok else "unverified"
        record.reason = (
            f"the screen changed after clicking {point['text']!r}" if ok
            else f"no visible change after clicking {point['text']!r}"
        )
        self.last_outcome = record.reason
        self.last_verification = record.status

    @staticmethod
    def _describe(step: Step, index: int, total: int) -> str:
        if step.kind == "click":
            return f"step {index}/{total}: {step.target!r} is on screen, click it."
        if step.kind == "type":
            return f"step {index}/{total}: the name field is focused, type the value."
        if step.kind == "key":
            return f"step {index}/{total}: press {step.value!r} to advance the screen."
        if step.kind == "hold":
            return f"step {index}/{total}: hold {step.value!r} to move."
        if step.kind == "wait_text":
            return f"step {index}/{total}: wait until {step.target!r} is readable."
        return f"step {index}/{total}: {step.kind}."

    # -- objectives ---------------------------------------------------------

    async def _understand(self, text: str) -> tuple[tuple[Step, ...], str]:
        """Read an instruction with the SHARED NLU layer before the local parser.

        The pilot used to understand only its own regex vocabulary. The project
        already has one language layer (``GlobalInputIntelligence``) that turns
        "open the options menu" / "press escape" into an intent, so the pilot asks
        it first and only falls back to the clause parser for phrasing the layer
        declines.

        The layer returns the VERB inside the entity value ("key=\"press
        escape\""), so the argument is cleaned here rather than trusted: sending
        the phrase as a key name would press nothing at all.
        """
        try:
            result = await self.app.intelligence.understand_async(text)
        except Exception:  # noqa: BLE001 - no NLU is not a lost instruction
            return parse_instruction(text)
        intent = getattr(result, "intent", None)
        action = str(getattr(intent, "action", "") or "").lower()
        entities = dict(getattr(intent, "entities", {}) or {})
        if str(getattr(result, "strategy", "")) == "clarification" or action in ("", "clarify"):
            return parse_instruction(text)
        argument = str(
            entities.get("key")
            or entities.get("text")
            or entities.get("application")
            or entities.get("target")
            or ""
        ).strip()
        argument = _CLEAN_ARG.sub("", argument).strip().strip("\"'")
        argument = _CLEAN_TAIL.sub("", argument).strip()
        if not argument:
            return parse_instruction(text)
        if action in ("open", "click", "select", "choose", "navigate", "launch"):
            self.last_decision = f"NLU read {text!r} as: click {argument!r}."
            return (Step(kind="click", target=argument),), ""
        if action == "type":
            return (Step(kind="type", value=argument),), ""
        if action == "press":
            return (Step(kind="key", value=argument.lower()),), ""
        return parse_instruction(text)

    async def run_objective(self, objective: str) -> dict[str, Any]:
        """Turn the operator's words into a plan and run it."""
        self.objective = " ".join(objective.split())
        self.state = "running"
        self._stop.clear()
        self._pause.set()
        await self.ensure_session()
        if not self.session_id:
            return {"ok": False, "reason": "no session"}
        reading = await self.observe()
        steps, problem = await self._understand(self.objective)
        task: Task | None = None
        if not steps and self.profile is not None:
            task = self.profile.task_for(self.objective)
            if task is not None:
                steps = task.steps
                self.last_decision = (
                    f"The objective matches the {task.name!r} task for {self.profile.name}; "
                    f"it runs that plan."
                )
        if not steps and self.profile is not None:
            # A taught skill is the last word: it is a demonstration by the
            # operator on this exact game and this exact screen.
            taught = self.learned.match(
                game=self.profile.name,
                objective=self.objective,
                signature=reading.signature(),
            )
            if taught:
                steps = tuple(taught)
                self.last_decision = "Using the steps the operator taught on this screen."
        if not steps:
            self.obstacle = problem or (
                "No plan matches this objective. Name a screen element to click, "
                "or teach the pilot by playing once."
            )
            self.state = "failed"
            return {"ok": False, "reason": self.obstacle}
        self.task = task
        if self.last_decision.startswith("step ") or not self.last_decision:
            self.last_decision = f"Plan from the instruction: {problem or 'read successfully'}"
        result = await self.run_steps(steps, label=self.objective)
        self.state = "running" if result["ok"] else "failed"
        self._emit("objective", objective=self.objective, ok=result["ok"])
        return result

    async def run_task(self, name: str) -> dict[str, Any]:
        if self.profile is None:
            return {"ok": False, "reason": "no game profile"}
        task = self.profile.task(name)
        if task is None:
            return {"ok": False, "reason": f"no task {name!r} in {self.profile.name}"}
        self.task = task
        self.objective = task.summary
        self.last_decision = f"Running the {task.name!r} task: {task.summary}"
        await self.ensure_session()
        if not self.session_id:
            return {"ok": False, "reason": "no session"}
        result = await self.run_steps(task.steps, label=task.name)
        self.state = "running" if result["ok"] else "failed"
        return result
