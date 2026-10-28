"""NF-16 — game state understanding: the model, the profiles, the extraction.

This module turns ONE validated observation into a structured statement about
what the screen currently shows. It is deliberately a READING, not an
interpretation of the game: the layers it keeps apart are

1. **generic visual observations** — every recognized text line and every
   detected region, exactly as the perception layer measured them;
2. **recognized interface elements** — text read from a region a *profile*
   declares (a counter, a prompt, a menu row);
3. **game-specific state fields** — the profile's element ids and kinds, with
   the value that was parsed out of the text;
4. **derived interpretations** — the interface summary and the change list
   computed by comparing two states.

The rules this module holds itself to are the phase's rules, stated here so a
reader does not have to infer them from absence:

* **A figure that was not measured is ``None``, never a zero.** There is no
  calibrated score anywhere in this model: ``confidence`` is the mean of the
  provider confidences the perception scene actually measured (``None`` when
  none was), and ``quality`` is labelled ``heuristic``.
* **A screen is named from evidence, and a change is confirmed before it is a
  fact.** ``scene`` is what this frame shows (a profile rule, a generic cue, or
  ``unknown``); ``transition`` is the temporal verdict over consecutive
  observations, so one frame can never invent a state change.
* **Missing is not failed.** ``absent`` (the profile's region exists and held
  no text), ``unreadable`` (OCR could not run or raised), ``ambiguous`` (text
  was read but is too short or could not be parsed) and ``unsupported`` (no
  profile covers this element) are four different statements.
* **An unknown game inherits nothing.** With no matching profile there are no
  presumed HUD regions at all: the state carries generic text and regions, and
  says why the game-specific fields are missing.
* **Nothing here reads the game, clicks, or predicts.** A detected region is
  geometry; ``kind`` stays ``unknown`` unless a profile *names* a region it
  declared, and no claim is made about what an entity is.

The service that owns scheduling and lifetime lives in
:mod:`novacontrol.novaforge.state_service`; this module is pure and synchronous
so it can be tested without a desktop, an engine, or an event loop.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4

from novacontrol.novaforge.scene import (
    SceneAssessment,
    SceneKind,
    SceneRule,
    SceneTracker,
    SceneTransition,
    assess_scene,
)
from novacontrol.perception.models import BBox, DetectedObject, OcrText, SceneRepresentation

__all__ = [
    "DEFAULT_MIN_CHARS",
    "ENTITY_STATUSES",
    "HUD_KINDS",
    "HUD_STATUSES",
    "OBSERVATION_CAPABILITIES",
    "PROFILE_REASONS",
    "SCHEMA_VERSION",
    "STATE_STATUSES",
    "UNKNOWN",
    "FieldSpec",
    "GameProfile",
    "GameState",
    "HudReading",
    "InterfaceState",
    "ObservationRef",
    "ProfileMatch",
    "ProfileRegistry",
    "StateChange",
    "StateEntity",
    "UnknownField",
    "build_game_state",
    "builtin_profiles",
    "default_registry",
    "parse_number",
]

#: The schema version of the wire shape this module produces.
#: ``nf24.1`` adds ``scene`` and ``transition`` — a deliberate additive bump, so
#: a consumer can tell a payload that CAN name a screen from one that never tried.
SCHEMA_VERSION = "nf24.1"

#: The one value used wherever a kind cannot be named from evidence.
UNKNOWN = "unknown"

#: Shortest text a declared region accepts as a reading. A one-character read
#: is usually a selection caret or a sliver of an icon.
DEFAULT_MIN_CHARS = 2

#: The observation capabilities a profile may require before it can be read.
#: Every name here is a thing the perception layer can actually be missing, so a
#: profile's requirement is checkable rather than decorative: ``ocr`` is the one
#: ``assess_scene`` refuses over, because a text-driven scene rule cannot have
#: fired without it.
OBSERVATION_CAPABILITIES = ("ocr", "detection", "vision")


class HudKind(StrEnum):
    """What an interface element IS, as far as a profile can declare it.

    ``HEALTH``/``SCORE``/``PROGRESS``/``TIMER`` are declared *categories*, not
    measurements: recognizing one means the region's text was read, never that
    a bar's fill was computed. A profile that declares a kind this build cannot
    measure marks it ``unsupported`` rather than guessing.
    """

    TEXT = "text"
    NUMBER = "number"
    HEALTH = "health"
    STATUS = "status"
    TIMER = "timer"
    MAP = "map"
    MENU = "menu"
    DIALOG = "dialog"
    NOTIFICATION = "notification"
    PROMPT = "prompt"
    SCORE = "score"
    PROGRESS = "progress"


HUD_KINDS = tuple(item.value for item in HudKind)

#: The kinds that describe the interface rather than a game value.
INTERFACE_KINDS = frozenset(
    {HudKind.MENU, HudKind.DIALOG, HudKind.NOTIFICATION, HudKind.PROMPT}
)

#: Kinds a profile may declare but this build has no measurement for.
UNMEASURED_KINDS = frozenset({HudKind.HEALTH, HudKind.MAP, HudKind.PROGRESS})


class HudStatus(StrEnum):
    """The five answers a declared element can give, and they differ.

    ``ABSENT`` is "the region was looked at and held nothing"; ``UNREADABLE`` is
    "no reading could be attempted"; ``AMBIGUOUS`` is "something was read and it
    is not enough to state"; ``UNSUPPORTED`` is "this element has no measurement
    in this build". Collapsing any of them into another would make a failure
    look like a clean screen.
    """

    RECOGNIZED = "recognized"
    ABSENT = "absent"
    AMBIGUOUS = "ambiguous"
    UNREADABLE = "unreadable"
    UNSUPPORTED = "unsupported"


HUD_STATUSES = tuple(item.value for item in HudStatus)


class StateStatus(StrEnum):
    """How complete one state reading is.

    ``OK`` — every required field the profile declares was recognized.
    ``PARTIAL`` — something was read and something was not; ``reasons`` says
    what. ``UNAVAILABLE`` — nothing could be read at all (no OCR, no frame
    content). ``FAILED`` — the reading itself could not be carried out.
    """

    OK = "ok"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


STATE_STATUSES = tuple(item.value for item in StateStatus)


class EntityStatus(StrEnum):
    """Whether entities were detected, none exist, or the detector could not run."""

    DETECTED = "detected"
    NONE = "none"
    UNAVAILABLE = "unavailable"


ENTITY_STATUSES = tuple(item.value for item in EntityStatus)


#: Why a profile was or was not used — stable codes a panel can switch on.
PROFILE_REASONS = (
    "profile_matched",
    "profile_forced",
    "profile_unknown",
    "no_profile_for_title",
    "no_title",
    "resolution_not_declared",
)


def _clean_title(value: str) -> str:
    """A title normalized for token matching — punctuation and case removed."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", str(value or "").lower()).split())


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One region a profile declares, in the profile's own reference pixels.

    ``value_kind`` is ``text`` or ``number``. A ``number`` region whose text is
    read but not parseable is ``ambiguous`` — the reading happened and the value
    is still unknown, which is a different statement from "nothing was there".
    """

    element_id: str
    kind: HudKind
    label: str
    box: BBox
    value_kind: str = "text"
    required: bool = True
    min_chars: int = DEFAULT_MIN_CHARS

    def __post_init__(self) -> None:
        if not str(self.element_id or "").strip():
            raise ValueError("A field spec needs an element_id.")
        if not isinstance(self.kind, HudKind):
            raise ValueError(f"Unknown HUD kind: {self.kind!r}")
        if self.value_kind not in ("text", "number"):
            raise ValueError(
                f"Field {self.element_id!r} declares value_kind {self.value_kind!r}; "
                "only 'text' and 'number' can be read."
            )
        if not self.box.has_extent:
            raise ValueError(f"Field {self.element_id!r} has an empty box.")
        if self.min_chars < 0:
            raise ValueError(f"Field {self.element_id!r} has a negative min_chars.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "element_id": self.element_id,
            "kind": self.kind.value,
            "label": self.label,
            "box": self.box.to_dict(),
            "value_kind": self.value_kind,
            "required": self.required,
            "min_chars": self.min_chars,
        }


@dataclass(frozen=True, slots=True)
class GameProfile:
    """A game-specific reading plan — the only place a game's layout is assumed.

    A profile is versioned and carries where it came from, because a region
    measured off one screenshot of one resolution is an assumption, not a fact
    about the game. ``title_tokens`` is the whole match rule: the profile
    applies when every token appears in the window title. Nothing is inherited
    by similarity, so an unknown title gets no regions at all.
    """

    profile_id: str
    game: str
    title_tokens: tuple[str, ...]
    version: str
    provenance: str
    reference_width: int
    reference_height: int
    fields: tuple[FieldSpec, ...] = ()
    categories: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    #: NF-24: the declared screens of this layout. A rule names a HUD field the
    #: profile already declares and the text that identifies one screen, so a
    #: scene claim carries the same evidence as a field reading. A profile with
    #: no rules still gets the generic cues, reported as INFERRED.
    scene_rules: tuple[SceneRule, ...] = ()
    #: NF-24: what the observation pipeline must provide before this profile can
    #: be interpreted at all. A profile that declares ``ocr`` is refused (with a
    #: structured unknown, never a guess) when OCR is known to be missing, so a
    #: half-wired pipeline cannot silently produce a bare cue-driven answer for
    #: a layout that depends on reading text.
    required_capabilities: tuple[str, ...] = ("ocr",)

    def __post_init__(self) -> None:
        if not str(self.profile_id or "").strip():
            raise ValueError("A profile needs a profile_id.")
        if not str(self.game or "").strip():
            raise ValueError(f"Profile {self.profile_id!r} needs a game name.")
        if not str(self.version or "").strip():
            raise ValueError(f"Profile {self.profile_id!r} needs a version.")
        if self.reference_width <= 0 or self.reference_height <= 0:
            raise ValueError(f"Profile {self.profile_id!r} has no reference size.")
        declared = [item.element_id for item in self.fields]
        if len(set(declared)) != len(declared):
            raise ValueError(f"Profile {self.profile_id!r} declares a duplicate element id.")
        frame = BBox(x=0, y=0, width=self.reference_width, height=self.reference_height)
        for item in self.fields:
            if item.box.right > frame.right or item.box.bottom > frame.bottom:
                raise ValueError(
                    f"Profile {self.profile_id!r} puts {item.element_id!r} outside its "
                    f"{self.reference_width}x{self.reference_height} reference frame."
                )
        for rule in self.scene_rules:
            if rule.field not in declared:
                raise ValueError(
                    f"Profile {self.profile_id!r} declares a scene rule on {rule.field!r}, "
                    "which is not one of its fields: a scene must be read from a "
                    "region this profile already declares."
                )
        required = tuple(str(item).strip() for item in self.required_capabilities)
        unknown = [item for item in required if item not in OBSERVATION_CAPABILITIES]
        if unknown:
            raise ValueError(
                f"Profile {self.profile_id!r} requires unknown observation "
                f"capabilit{'y' if len(unknown) == 1 else 'ies'} {unknown!r}; "
                f"declared capabilities are {list(OBSERVATION_CAPABILITIES)!r}."
            )
        object.__setattr__(self, "required_capabilities", required)

    @property
    def tokens(self) -> tuple[str, ...]:
        return tuple(token for token in (_clean_title(item) for item in self.title_tokens) if token)

    def matches(self, title: str) -> bool:
        """Whether every declared token appears in ``title`` — nothing weaker."""
        tokens = self.tokens
        if not tokens:
            return False
        cleaned = _clean_title(title)
        if not cleaned:
            return False
        words = set(cleaned.split())
        return all(token in words for token in tokens)

    def scale_for(self, width: int, height: int) -> tuple[float, float]:
        """Reference pixels → frame pixels, per axis."""
        if width <= 0 or height <= 0:
            return (0.0, 0.0)
        return (width / self.reference_width, height / self.reference_height)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "game": self.game,
            "title_tokens": list(self.title_tokens),
            "version": self.version,
            "provenance": self.provenance,
            "reference_size": [self.reference_width, self.reference_height],
            "fields": [item.to_dict() for item in self.fields],
            "categories": list(self.categories),
            "limitations": list(self.limitations),
            "scene_rules": [rule.to_dict() for rule in self.scene_rules],
            "required_capabilities": list(self.required_capabilities),
        }


@dataclass(frozen=True, slots=True)
class ProfileMatch:
    """The registry's answer about which profile (if any) applies, and why."""

    profile: GameProfile | None = None
    reason_code: str = "no_profile_for_title"
    reason: str = ""
    forced: bool = False

    @property
    def matched(self) -> bool:
        return self.profile is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile.profile_id if self.profile is not None else "",
            "game": self.profile.game if self.profile is not None else "",
            "version": self.profile.version if self.profile is not None else "",
            "provenance": self.profile.provenance if self.profile is not None else "",
            "matched": self.matched,
            "forced": self.forced,
            "reason_code": self.reason_code,
            "reason": self.reason,
        }


class ProfileRegistry:
    """Deterministic profile lookup. A rejected profile is recorded, never raised.

    Registration validates a profile and, when it is malformed, keeps the
    rejection with its reason: a broken profile must leave the pipeline working
    with generic observation rather than take the reading down (Scope F).
    """

    def __init__(self, profiles: Iterable[GameProfile] = ()) -> None:
        self._profiles: dict[str, GameProfile] = {}
        self._rejected: dict[str, str] = {}
        for profile in profiles:
            self.register(profile)

    @property
    def rejected(self) -> dict[str, str]:
        return dict(self._rejected)

    @property
    def ids(self) -> tuple[str, ...]:
        """The registered profile ids, sorted — deterministic for a UI table."""
        return tuple(sorted(self._profiles))

    def register(self, profile: Any) -> str:
        """Register one profile; returns ``""`` on success or the refusal reason."""
        try:
            if not isinstance(profile, GameProfile):
                raise ValueError(
                    f"a profile must be a GameProfile, not {type(profile).__name__}"
                )
            profile_id = profile.profile_id
            if profile_id in self._profiles:
                raise ValueError(f"profile {profile_id!r} is already registered")
            self._profiles[profile_id] = profile
            self._rejected.pop(profile_id, None)
            return ""
        except Exception as exc:  # noqa: BLE001 - a bad profile is data, not a crash
            # A mapping that claims an id is recorded under THAT id, so a broken
            # profile can be found by the name it asked for rather than under a
            # placeholder that hides which one failed.
            claimed = str(profile.get("profile_id") or "") if isinstance(profile, Mapping) else ""
            key = claimed or str(getattr(profile, "profile_id", "") or "unidentified")
            self._rejected[key] = f"{type(exc).__name__}: {exc}"
            return self._rejected[key]

    def get(self, profile_id: str) -> GameProfile | None:
        return self._profiles.get(str(profile_id or "").strip())

    def rows(self) -> list[dict[str, Any]]:
        rows = [profile.to_dict() for _, profile in sorted(self._profiles.items())]
        for profile_id, reason in sorted(self._rejected.items()):
            rows.append(
                {
                    "profile_id": profile_id,
                    "available": False,
                    "reason": reason,
                }
            )
        return rows

    def resolve(
        self, title: str, *, width: int = 0, height: int = 0, profile_id: str = ""
    ) -> ProfileMatch:
        """The profile for this target, or an explicit refusal with a code.

        A forced ``profile_id`` is honoured whether or not the title matches —
        the operator asked for it — but the match is recorded as forced so the
        state never reads as an automatic detection. A title that matches a
        profile at a resolution the profile did not declare is a REFUSAL, not a
        silent stretch: the regions would be measuring different pixels.
        """
        wanted = str(profile_id or "").strip()
        if wanted:
            profile = self.get(wanted)
            if profile is None:
                reason = self._rejected.get(wanted, "no such profile is registered")
                return ProfileMatch(
                    reason_code="profile_unknown",
                    reason=f"Profile {wanted!r} is not available: {reason}.",
                    forced=True,
                )
            return ProfileMatch(
                profile=profile,
                reason_code="profile_forced",
                reason=f"Profile {profile.profile_id} v{profile.version} was requested by id.",
                forced=True,
            )
        cleaned = _clean_title(title)
        if not cleaned:
            return ProfileMatch(
                reason_code="no_title",
                reason="No window title was observed, so no game profile can be selected.",
            )
        candidates = [
            profile for _, profile in sorted(self._profiles.items()) if profile.matches(title)
        ]
        if not candidates:
            return ProfileMatch(
                reason_code="no_profile_for_title",
                reason=(
                    f"No perception profile matches {title.strip()!r}; generic text and "
                    "region observation only, with no presumed HUD layout."
                ),
            )
        declared = [item for item in candidates if item.reference_width and item.reference_height]
        if width > 0 and height > 0 and declared:
            matching = [
                item
                for item in declared
                if abs(item.reference_width - width) <= max(8, item.reference_width // 20)
                and abs(item.reference_height - height) <= max(8, item.reference_height // 20)
            ]
            if not matching:
                reference = declared[0]
                return ProfileMatch(
                    reason_code="resolution_not_declared",
                    reason=(
                        f"Profile {reference.profile_id} declares a "
                        f"{reference.reference_width}x{reference.reference_height} frame and "
                        f"this observation is {width}x{height}; its regions would measure "
                        "the wrong pixels, so no HUD layout is presumed."
                    ),
                )
            candidates = matching
        chosen = candidates[0]
        return ProfileMatch(
            profile=chosen,
            reason_code="profile_matched",
            reason=(
                f"Profile {chosen.profile_id} v{chosen.version} matched the window title "
                f"({', '.join(chosen.title_tokens)})."
            ),
        )


def _field(
    element_id: str,
    kind: HudKind,
    label: str,
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    value_kind: str = "text",
    required: bool = True,
    min_chars: int = DEFAULT_MIN_CHARS,
) -> FieldSpec:
    """A compact constructor used by the built-in profiles below."""
    return FieldSpec(
        element_id=element_id,
        kind=kind,
        label=label,
        box=BBox(x=x, y=y, width=width, height=height),
        value_kind=value_kind,
        required=required,
        min_chars=min_chars,
    )


def builtin_profiles() -> tuple[GameProfile, ...]:
    """The profiles that ship with this build — bounded evaluation targets only.

    Each region below was measured from a real captured frame of a disposable
    harness window (the ``NC FORGE`` windows NF-15's probe and the audit ticks
    drove) or from the shipped ``GAME MENU`` fixture; see the NF-16 report for
    the frames and the coordinates. There is deliberately no profile for a
    commercial game: one screenshot of one menu is not a layout this build can
    claim, so commercial titles take the generic path.
    """
    return (
        GameProfile(
            profile_id="nc-forge-tick@1",
            game="NC FORGE AUDIT TICK (disposable harness)",
            # The captured windows are titled "NC FORGE AUDIT" (the tick is the
            # readout, not the title); requiring "tick" here made the profile
            # unreachable for the very frames its regions were measured from.
            title_tokens=("nc", "forge", "audit"),
            version="1",
            provenance="measured from data/novaforge/frames/*/ (1832x718 tick windows)",
            reference_width=1832,
            reference_height=718,
            fields=(
                _field("header", HudKind.TEXT, "Window header", 60, 12, 340, 40),
                _field(
                    "counter",
                    HudKind.NUMBER,
                    "Tick counter",
                    940,
                    150,
                    320,
                    150,
                    value_kind="number",
                ),
            ),
            categories=("ticks",),
            limitations=(
                "The counter is read as text and parsed; no bar, meter or colour is measured.",
            ),
        ),
        GameProfile(
            profile_id="nc-forge-harness@1",
            game="NC FORGE HARNESS (disposable harness)",
            title_tokens=("nc", "forge", "harness"),
            version="1",
            provenance="measured from data/novaforge/frames/*/ (1040x440 harness windows)",
            reference_width=1040,
            reference_height=440,
            fields=(
                # The title and the state suffix share one title-bar line; the
                # title region stops before the state region so neither reads the
                # other's words twice.
                _field("title", HudKind.TEXT, "Window title text", 60, 12, 240, 40),
                _field("state", HudKind.STATUS, "Harness state", 300, 10, 140, 45),
                _field("prompt", HudKind.PROMPT, "Prompt", 320, 145, 420, 60, required=False),
                _field("note", HudKind.TEXT, "Note", 20, 235, 1000, 110, required=False),
            ),
            categories=("harness_state", "waiting_prompt"),
            limitations=(
                "State is a word read from a declared region; it is not a game state.",
            ),
        ),
        GameProfile(
            profile_id="nc-forge-mission@1",
            game="NC FORGE MISSION (fixture scenario)",
            # The objective panel is a declared FIXTURE layout: its game STATE is
            # generated by the NF-17 evaluation script, so this profile makes no
            # claim about any commercial game's HUD.
            title_tokens=("nc", "forge", "mission"),
            version="1",
            provenance=(
                "declared for the NF-17 fixture scenario (1280x720); the labelled "
                "states are generated by scripts/novaforge_nf17_objective_eval.py"
            ),
            reference_width=1280,
            reference_height=720,
            fields=(
                _field("objective", HudKind.PROMPT, "Objective line", 40, 60, 800, 60),
                _field(
                    "progress",
                    HudKind.NUMBER,
                    "Progress counter",
                    40,
                    140,
                    300,
                    60,
                    value_kind="number",
                    # A counter legitimately reads "0"; one digit is not ambiguous.
                    min_chars=1,
                ),
                _field(
                    "notification",
                    HudKind.NOTIFICATION,
                    "Outcome banner",
                    40,
                    620,
                    900,
                    60,
                    required=False,
                ),
            ),
            categories=("mission",),
            # NF-24: the outcome banner is the one field that names a screen, and
            # the two patterns are the fixture's own words (the NF-17 evaluation
            # feeds "Mission complete" and "Mission failed"). A banner carrying
            # both is a contradiction and stays UNKNOWN, not a coin flip.
            scene_rules=(
                SceneRule(SceneKind.VICTORY, "notification", ("mission complete", "mission accomplished")),
                SceneRule(SceneKind.DEFEAT, "notification", ("mission failed", "mission over")),
            ),
            limitations=(
                "The mission panel is a fixture layout, not a game's HUD; progress is "
                "one numeric counter, and bars/stages are not measured.",
            ),
        ),
        GameProfile(
            profile_id="game-menu@1",
            game="GAME MENU (fixture menu window)",
            title_tokens=("game", "menu"),
            version="1",
            provenance="measured from data/novaforge/frames/3f84fe424db9479c9133cf93328dd365/",
            reference_width=2880,
            reference_height=1800,
            fields=(
                # Abutting, not overlapping: a word's centre belongs to exactly one
                # declared region, or the same text would be read twice.
                _field("title", HudKind.TEXT, "Menu title", 1160, 800, 220, 36),
                _field("menu", HudKind.MENU, "Menu entries", 1160, 838, 440, 40),
            ),
            categories=("menu_entries",),
            # NF-24: the menu row's own words (the fixture reads "SINGLE PLAYER").
            # The title field is not used: "GAME MENU" is the window title, and a
            # scene must be read from the screen, not from the OS caption.
            scene_rules=(
                SceneRule(
                    SceneKind.MENU,
                    "menu",
                    ("single player", "new game", "load game", "quit"),
                ),
            ),
            limitations=(
                "Menu entries are read as one text row; selection highlight is not measured.",
            ),
        ),
    )


def default_registry() -> ProfileRegistry:
    """The registry this build ships, built fresh so a test can mutate its own."""
    return ProfileRegistry(builtin_profiles())


@dataclass(frozen=True, slots=True)
class ObservationRef:
    """Which observation is being read — the anchor every claim hangs from."""

    observation_id: str
    session_id: str = ""
    game: str = ""
    seq: int = 0
    at: str = ""
    width: int | None = None
    height: int | None = None
    backend: str = ""
    frame_id: str = ""

    def __post_init__(self) -> None:
        if not str(self.observation_id or "").strip():
            raise ValueError("An observation reference needs an observation_id.")
        if self.seq < 0:
            raise ValueError("An observation reference cannot have a negative sequence.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "session_id": self.session_id,
            "game": self.game,
            "seq": self.seq,
            "at": self.at,
            "width": self.width,
            "height": self.height,
            "backend": self.backend,
            "frame_id": self.frame_id,
        }


@dataclass(frozen=True, slots=True)
class HudReading:
    """One declared interface element, read (or explicitly not read).

    ``text`` is what the OCR engine placed inside the region; ``value`` is the
    parsed number when the region declares ``number``. Both stay ``None`` when
    nothing was measured, and ``reason`` says which of the five statuses this is.
    """

    element_id: str
    kind: HudKind
    label: str
    status: HudStatus
    text: str = ""
    value: float | None = None
    box: BBox | None = None
    source: str = ""
    reason: str = ""
    profile_id: str = ""
    derived: bool = False
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, HudStatus):
            raise ValueError(f"Unknown HUD status: {self.status!r}")
        if not isinstance(self.kind, HudKind):
            raise ValueError(f"Unknown HUD kind: {self.kind!r}")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("A confidence must be a probability in [0, 1].")

    def to_dict(self) -> dict[str, Any]:
        return {
            "element_id": self.element_id,
            "kind": self.kind.value,
            "label": self.label,
            "status": self.status.value,
            "text": self.text,
            "value": self.value,
            "box": self.box.to_dict() if self.box is not None else None,
            "source": self.source,
            "reason": self.reason,
            "profile_id": self.profile_id,
            "derived": self.derived,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class StateEntity:
    """One detected region, with its classification left explicitly unknown.

    The shipped detector is classical connected components: it can say WHERE
    something is, not WHAT it is. ``classified`` is therefore ``False`` and
    ``kind`` is ``UNKNOWN`` unless a profile names a declared region — and a
    region is an interface element, not a game entity, so even then the entity
    list stays unnamed. This is the distinction the phase requires between
    "detected" and "recognized".
    """

    entity_id: str
    kind: str = UNKNOWN
    label: str = ""
    box: BBox | None = None
    confidence: float | None = None
    classified: bool = False
    source: str = ""
    frame_id: str = ""
    observation_id: str = ""
    reason: str = ""
    overlaps_hud: bool = False

    def __post_init__(self) -> None:
        if not str(self.entity_id or "").strip():
            raise ValueError("An entity needs an id.")
        if not str(self.kind or "").strip():
            raise ValueError("An entity needs a kind; use 'unknown', not an empty string.")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("A confidence must be a probability in [0, 1].")

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "kind": self.kind,
            "label": self.label,
            "box": self.box.to_dict() if self.box is not None else None,
            "confidence": self.confidence,
            "classified": self.classified,
            "source": self.source,
            "frame_id": self.frame_id,
            "observation_id": self.observation_id,
            "reason": self.reason,
            "overlaps_hud": self.overlaps_hud,
        }


@dataclass(frozen=True, slots=True)
class InterfaceState:
    """A derived summary of the menu/prompt/dialog elements that were read.

    ``derived`` is ``True`` on every instance because this is an interpretation
    of readings, not a reading. ``present`` is three-valued: ``None`` means the
    interface state is unknown (no profile, or no interface region was read).
    """

    kind: str = UNKNOWN
    present: bool | None = None
    labels: tuple[str, ...] = ()
    evidence: str = ""
    derived: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "present": self.present,
            "labels": list(self.labels),
            "evidence": self.evidence,
            "derived": self.derived,
        }


@dataclass(frozen=True, slots=True)
class StateChange:
    """One OBSERVED difference between two states of the same session.

    ``inferred`` is always ``False`` in this build: every change here is a diff
    of two readings. A kind that would need interpretation ("the player took
    damage") is deliberately absent from the vocabulary.
    """

    kind: str
    field_name: str
    before: str = ""
    after: str = ""
    detail: str = ""
    observation_id: str = ""
    inferred: bool = False

    def __post_init__(self) -> None:
        if not str(self.kind or "").strip():
            raise ValueError("A change needs a kind.")
        if not str(self.field_name or "").strip():
            raise ValueError("A change needs the field it is about.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "field": self.field_name,
            "before": self.before,
            "after": self.after,
            "detail": self.detail,
            "observation_id": self.observation_id,
            "inferred": self.inferred,
        }


@dataclass(frozen=True, slots=True)
class UnknownField:
    """A field the profile declared, or a category, that could not be stated."""

    field_name: str
    kind: str = UNKNOWN
    status: str = HudStatus.UNSUPPORTED.value
    reason: str = ""

    def __post_init__(self) -> None:
        if not str(self.field_name or "").strip():
            raise ValueError("An unknown field needs a name.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field_name,
            "kind": self.kind,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class Tracking:
    """Whether this reading was compared with a previous one, and why not.

    ``dropped_observations`` is the gap between the two observation sequences
    when they skipped numbers: an irregular capture interval is reported, not
    hidden. It is ``None`` when nothing comparable came before.
    """

    compared: bool = False
    previous_state_id: str = ""
    previous_observation_id: str = ""
    reason: str = "no previous state in this session"
    dropped_observations: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "compared": self.compared,
            "previous_state_id": self.previous_state_id,
            "previous_observation_id": self.previous_observation_id,
            "reason": self.reason,
            "dropped_observations": self.dropped_observations,
        }


@dataclass(frozen=True, slots=True)
class GameState:
    """One structured reading of the screen, with its evidence and its gaps.

    The model keeps the four layers separate: ``text`` and the region-derived
    ``entities`` are what perception measured; ``hud`` holds the profile's
    fields; ``interface`` and ``changes`` are derived; ``unknown`` lists what
    was declared and could not be stated.
    """

    game: str
    observation: ObservationRef
    sequence: int = 0
    timestamp: str = ""
    status: StateStatus = StateStatus.PARTIAL
    state_id: str = field(default_factory=lambda: f"state-{uuid4().hex[:12]}")
    hud: tuple[HudReading, ...] = ()
    text: tuple[dict[str, Any], ...] = ()
    entities: tuple[StateEntity, ...] = ()
    entity_status: EntityStatus = EntityStatus.UNAVAILABLE
    entity_reason: str = ""
    interface: InterfaceState = field(default_factory=InterfaceState)
    changes: tuple[StateChange, ...] = ()
    tracking: Tracking = field(default_factory=Tracking)
    unknown: tuple[UnknownField, ...] = ()
    unsupported: tuple[str, ...] = ()
    profile: ProfileMatch = field(default_factory=ProfileMatch)
    #: NF-24: what THIS frame shows, and whether a change to it is a fact yet.
    scene: SceneAssessment = field(default_factory=SceneAssessment)
    transition: SceneTransition = field(default_factory=SceneTransition)
    confidence: float | None = None
    uncertainty: float | None = None
    quality: Mapping[str, Any] = field(default_factory=dict)
    latency: Mapping[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("A state sequence cannot be negative.")
        if not isinstance(self.status, StateStatus):
            raise ValueError(f"Unknown state status: {self.status!r}")
        if not isinstance(self.entity_status, EntityStatus):
            raise ValueError(f"Unknown entity status: {self.entity_status!r}")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("A confidence must be a probability in [0, 1].")
        coverage = self.quality.get("coverage") if isinstance(self.quality, Mapping) else None
        if coverage is not None and not 0.0 <= float(coverage) <= 1.0:
            raise ValueError("Quality coverage must be a fraction in [0, 1] or None.")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"This build produces schema {SCHEMA_VERSION!r}, not {self.schema_version!r}."
            )

    @property
    def recognized(self) -> tuple[HudReading, ...]:
        return tuple(item for item in self.hud if item.status is HudStatus.RECOGNIZED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "state_id": self.state_id,
            "game": self.game,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "status": self.status.value,
            "observation": self.observation.to_dict(),
            "profile": self.profile.to_dict(),
            "hud": [item.to_dict() for item in self.hud],
            "text": [dict(row) for row in self.text],
            "entities": [item.to_dict() for item in self.entities],
            "entity_status": self.entity_status.value,
            "entity_reason": self.entity_reason,
            "interface": self.interface.to_dict(),
            "scene": self.scene.to_dict(),
            "transition": self.transition.to_dict(),
            "changes": [item.to_dict() for item in self.changes],
            "tracking": self.tracking.to_dict(),
            "unknown": [item.to_dict() for item in self.unknown],
            "unsupported": list(self.unsupported),
            "confidence": self.confidence,
            "uncertainty": self.uncertainty,
            "quality": dict(self.quality),
            "latency": dict(self.latency),
            "reasons": list(self.reasons),
            "limitations": list(self.limitations),
        }


# ---------------------------------------------------------------------------
# Extraction — pure functions over one perception scene
# ---------------------------------------------------------------------------

_NUMBER = re.compile(r"^[-+]?\d+(?:\.\d+)?$")


def parse_number(text: str) -> tuple[float | None, str]:
    """Parse a HUD number, or say why the text is not one.

    Only a value the text FULLY states is accepted. ``"786"``, ``"1,234"`` and
    ``"87%"`` parse; ``"LEVEL 26"`` does not, because the region was supposed to
    hold a number and returning 26 from a sentence would be inventing which part
    of the text the field meant.
    """
    cleaned = str(text or "").strip().replace(",", "").replace(" ", "")
    if cleaned.endswith("%"):
        cleaned = cleaned[:-1]
    if not cleaned:
        return None, "the region held no text"
    if not _NUMBER.match(cleaned):
        return None, f"{text.strip()!r} is not a number"
    try:
        return float(cleaned), ""
    except ValueError:  # pragma: no cover - the regex already guarantees this
        return None, f"{text.strip()!r} could not be parsed"


def _line_text(lines: Sequence[str]) -> str:
    """Join the lines read inside one region, keeping lines distinguishable."""
    return " / ".join(line for line in lines if line)


def _text_rows(scene: SceneRepresentation) -> tuple[dict[str, Any], ...]:
    """Every recognized line, in the engine's reading order, as plain rows."""
    rows: list[dict[str, Any]] = []
    for item in scene.text:
        text = str(item.text or "").strip()
        if not text:
            continue
        rows.append(
            {
                "line": item.reading_order,
                "text": text,
                "bbox": item.bbox.to_dict() if item.bbox is not None else None,
                "confidence": item.confidence,
                "source": item.source,
            }
        )
    rows.sort(key=lambda row: (row["line"] is None, row["line"] if row["line"] is not None else 0))
    return tuple(rows)


def _region_box(box: BBox, scale_x: float, scale_y: float) -> BBox:
    """A profile region in the observed frame's pixels."""
    return BBox(
        x=int(round(box.x * scale_x)),
        y=int(round(box.y * scale_y)),
        width=int(round(box.width * scale_x)),
        height=int(round(box.height * scale_y)),
    )


def _inside(word: BBox, region: BBox) -> bool:
    """Whether a word's centre sits in the region — the one assignment rule."""
    if not word.has_extent:
        return False
    center_x, center_y = word.center
    return region.x <= center_x <= region.right and region.y <= center_y <= region.bottom


def _placed_words(scene: SceneRepresentation) -> tuple[tuple[str, int | None, BBox, str], ...]:
    """Per-WORD text with its own box, when the reader placed words individually.

    ``scene.text`` is line granularity: one record per line with the union of its
    words' boxes. A HUD line often holds a label and its value (``TICK 786``), so
    that union's centre can sit outside the region the value occupies and the
    field would read as absent even though the number was recognized exactly.
    The same reader publishes each word as a text detection, and those boxes are
    measurements rather than a derived union, so a region can be read word by
    word when they exist.
    """
    words: list[tuple[str, int | None, BBox, str]] = []
    for item in getattr(scene, "objects", ()):
        metadata = getattr(item, "metadata", None) or {}
        if str(metadata.get("kind") or "") != "text":
            continue
        text = str(getattr(item, "label", "") or "").strip()
        box = getattr(item, "bbox", None)
        if not text or box is None or not box.has_extent:
            continue
        line = metadata.get("line")
        words.append(
            (text, line if isinstance(line, int) else None, box, str(getattr(item, "source", "") or ""))
        )
    return tuple(words)


def _words_in(
    scene: SceneRepresentation, region: BBox
) -> tuple[tuple[str, int | None, str], ...]:
    """The text, line index and reader of every word whose centre is inside.

    Word boxes are preferred when the reader provided them; a reader that only
    reports lines (a model, a plain text file) still contributes its lines, and
    that fallback is what keeps a geometry-poor scene readable rather than empty.
    The reader's own name travels with the text, so a field can say WHICH engine
    read it instead of a fixed ``"ocr"`` label.
    """
    placed = _placed_words(scene)
    found: list[tuple[str, int | None, str]] = []
    if placed:
        for text, line, box, source in placed:
            if _inside(box, region):
                found.append((text, line, source))
        return tuple(found)
    for item in scene.text:
        text = str(item.text or "").strip()
        if not text or item.bbox is None:
            continue
        if _inside(item.bbox, region):
            found.append((text, item.reading_order, str(item.source or "")))
    return tuple(found)


def _read_field(
    spec: FieldSpec,
    scene: SceneRepresentation,
    *,
    scale_x: float,
    scale_y: float,
    profile_id: str,
    ocr_available: bool | None,
    ocr_reason: str,
) -> HudReading:
    """Read one declared field, and never blur "absent" into "unreadable"."""
    box = _region_box(spec.box, scale_x, scale_y)
    if spec.kind in UNMEASURED_KINDS:
        return HudReading(
            element_id=spec.element_id,
            kind=spec.kind,
            label=spec.label,
            box=box,
            profile_id=profile_id,
            status=HudStatus.UNSUPPORTED,
            reason=(
                f"{spec.kind.value!r} needs a measurement this build does not have "
                "(no bar, meter or map reader is wired)."
            ),
        )
    if ocr_available is False:
        return HudReading(
            element_id=spec.element_id,
            kind=spec.kind,
            label=spec.label,
            box=box,
            profile_id=profile_id,
            status=HudStatus.UNREADABLE,
            reason=ocr_reason or "No OCR engine is available, so no text could be read.",
        )
    words = _words_in(scene, box)
    if not words:
        return HudReading(
            element_id=spec.element_id,
            kind=spec.kind,
            label=spec.label,
            box=box,
            profile_id=profile_id,
            status=HudStatus.ABSENT,
            reason=(
                "The declared region held no recognized text. That is not proof the "
                "element is absent from the game — only that nothing was read there."
            ),
        )
    # Group by the engine's own line index so a region holding two lines keeps
    # them apart instead of interleaving them into one sentence.
    source = next((src for _, _, src in words if src), "")
    grouped: dict[int | None, list[str]] = {}
    for text, line, _reader in words:
        grouped.setdefault(line, []).append(text)
    text = _line_text([" ".join(grouped[key]) for key in sorted(grouped, key=lambda k: (k is None, k or 0))])
    compact = text.replace(" ", "")
    if len(compact) < max(1, spec.min_chars):
        return HudReading(
            element_id=spec.element_id,
            kind=spec.kind,
            label=spec.label,
            box=box,
            profile_id=profile_id,
            status=HudStatus.AMBIGUOUS,
            text=text,
            source=source,
            reason=(
                f"{len(compact)} character(s) were read where at least "
                f"{max(1, spec.min_chars)} are expected; too short to state."
            ),
        )
    if spec.value_kind == "number":
        value, why = parse_number(text)
        if value is None:
            return HudReading(
                element_id=spec.element_id,
                kind=spec.kind,
                label=spec.label,
                box=box,
                profile_id=profile_id,
                status=HudStatus.AMBIGUOUS,
                text=text,
                source=source,
                reason=f"The region was read as {text!r} but {why}.",
            )
        return HudReading(
            element_id=spec.element_id,
            kind=spec.kind,
            label=spec.label,
            box=box,
            profile_id=profile_id,
            status=HudStatus.RECOGNIZED,
            text=text,
            value=value,
            source=source,
        )
    return HudReading(
        element_id=spec.element_id,
        kind=spec.kind,
        label=spec.label,
        box=box,
        profile_id=profile_id,
        status=HudStatus.RECOGNIZED,
        text=text,
        source=source,
    )


def _entities(
    scene: SceneRepresentation,
    observation: ObservationRef,
    *,
    hud_boxes: Sequence[BBox],
    detection_available: bool | None,
    detection_reason: str,
    max_entities: int,
) -> tuple[tuple[StateEntity, ...], EntityStatus, str]:
    """Detected regions as entities, with the classification left unknown."""
    rows: list[StateEntity] = []
    for index, item in enumerate(scene.objects):
        if not isinstance(item, DetectedObject):
            continue
        kind = str(item.metadata.get("kind") or "") if isinstance(item.metadata, Mapping) else ""
        if kind == "text":
            continue
        if not item.bbox.has_extent:
            continue
        box = item.bbox
        overlaps = any(box.intersects(other) for other in hud_boxes)
        rows.append(
            StateEntity(
                entity_id=f"{observation.observation_id}:e{index}",
                kind=UNKNOWN,
                label=str(item.label or ""),
                box=box,
                confidence=item.confidence,
                classified=False,
                source=str(item.source or ""),
                frame_id=str(item.frame_id or observation.frame_id),
                observation_id=observation.observation_id,
                reason=(
                    "Connected-region geometry only: no semantic detector is wired, so "
                    "what this region depicts is unknown."
                ),
                overlaps_hud=overlaps,
            )
        )
        if len(rows) >= max_entities:
            break
    if rows:
        return tuple(rows), EntityStatus.DETECTED, ""
    if detection_available is False:
        return (
            (),
            EntityStatus.UNAVAILABLE,
            detection_reason or "No object detector is available, so nothing was detected.",
        )
    if detection_available is True:
        return (
            (),
            EntityStatus.NONE,
            "The detector ran and found no region above its threshold.",
        )
    return (
        (),
        EntityStatus.UNAVAILABLE,
        detection_reason or "The detector reported no result, so absence is not claimed.",
    )


def _interface(hud: Sequence[HudReading], profile: ProfileMatch) -> InterfaceState:
    """Derive the interface summary from the interface-kind readings."""
    elements = [item for item in hud if item.kind in INTERFACE_KINDS]
    if not elements:
        return InterfaceState(
            kind=UNKNOWN,
            present=None,
            evidence=(
                "No interface element is declared for this observation."
                if not profile.matched
                else "The profile declares no menu, dialog, prompt or notification region."
            ),
        )
    recognized = [item for item in elements if item.status is HudStatus.RECOGNIZED]
    kind = elements[0].kind.value
    if not recognized:
        return InterfaceState(
            kind=kind,
            present=None,
            evidence="An interface element is declared but none of them was read.",
        )
    return InterfaceState(
        kind=kind,
        present=True,
        labels=tuple(item.text for item in recognized),
        evidence=(
            f"{len(recognized)} of {len(elements)} declared interface element(s) were read: "
            + "; ".join(f"{item.element_id}={item.text!r}" for item in recognized)
        ),
    )


def _changes(previous: GameState, current: HudReading, *, observation_id: str) -> tuple[StateChange, ...]:
    """Observed differences for one field between two states of one session."""
    before = next(
        (item for item in previous.hud if item.element_id == current.element_id), None
    )
    if before is None:
        return (
            StateChange(
                kind="hud_appeared",
                field_name=current.element_id,
                after=current.text,
                detail=f"{current.label} was not declared in the previous state.",
                observation_id=observation_id,
            ),
        )
    changes: list[StateChange] = []
    if before.status is not current.status:
        changes.append(
            StateChange(
                kind="hud_status_changed",
                field_name=current.element_id,
                before=before.status.value,
                after=current.status.value,
                detail=f"{current.label}: {before.status.value} → {current.status.value}.",
                observation_id=observation_id,
            )
        )
    elif current.status is HudStatus.RECOGNIZED:
        if before.value is not None and current.value is not None and before.value != current.value:
            changes.append(
                StateChange(
                    kind="hud_value_changed",
                    field_name=current.element_id,
                    before=f"{before.value:g}",
                    after=f"{current.value:g}",
                    detail=(
                        f"{current.label} changed from {before.value:g} to "
                        f"{current.value:g} (read as {current.text!r})."
                    ),
                    observation_id=observation_id,
                )
            )
        elif before.text != current.text and (
            before.value is None or current.value is None
        ):
            changes.append(
                StateChange(
                    kind="hud_text_changed",
                    field_name=current.element_id,
                    before=before.text,
                    after=current.text,
                    detail=f"{current.label} text changed.",
                    observation_id=observation_id,
                )
            )
    return tuple(changes)


def _quality(hud: Sequence[HudReading], profile: ProfileMatch) -> dict[str, Any]:
    """A heuristic coverage count — never a confidence score."""
    declared = [item for item in hud if item.status is not HudStatus.UNSUPPORTED]
    recognized = [item for item in declared if item.status is HudStatus.RECOGNIZED]
    coverage = (len(recognized) / len(declared)) if declared else None
    notes = [
        "Coverage is a count of declared fields that were read — a heuristic, not a "
        "calibrated probability. No component reports a confidence on this machine.",
    ]
    if not profile.matched:
        notes.append("No profile matched, so no game-specific field was declared.")
    return {
        "heuristic": True,
        "declared": len(declared),
        "recognized": len(recognized),
        "coverage": round(coverage, 4) if coverage is not None else None,
        "notes": notes,
    }


def _status_for(
    hud: Sequence[HudReading],
    *,
    profile: ProfileMatch,
    text: Sequence[Mapping[str, Any]],
    entity_status: EntityStatus,
    ocr_available: bool | None,
) -> tuple[StateStatus, tuple[str, ...]]:
    """The state's own verdict, with the reason that decided it."""
    reasons: list[str] = []
    if ocr_available is False:
        reasons.append(
            "OCR is unavailable on this machine, so no interface text could be read."
        )
    if not profile.matched:
        reasons.append(profile.reason or "No perception profile matched this target.")
    declared = [item for item in hud if item.status is not HudStatus.UNSUPPORTED]
    recognized = [item for item in declared if item.status is HudStatus.RECOGNIZED]
    unmet = [
        item
        for item in declared
        if item.status is not HudStatus.RECOGNIZED
        and any(
            spec_missing
            for spec_missing in (item.status is HudStatus.AMBIGUOUS, item.status is HudStatus.ABSENT)
        )
    ]
    for item in unmet:
        reasons.append(f"{item.element_id}: {item.status.value} — {item.reason}")
    unmeasured = [item for item in hud if item.status is HudStatus.UNSUPPORTED]
    for item in unmeasured:
        reasons.append(f"{item.element_id}: unsupported — {item.reason}")
    if not profile.matched:
        if text or entity_status is EntityStatus.DETECTED:
            return StateStatus.PARTIAL, tuple(reasons)
        return StateStatus.UNAVAILABLE, tuple(reasons)
    required = [item for item in declared if item.status is not HudStatus.UNSUPPORTED]
    required_ids = {
        spec.element_id
        for spec in (profile.profile.fields if profile.profile is not None else ())
        if spec.required
    }
    required_failed = [
        item
        for item in required
        if item.element_id in required_ids and item.status is not HudStatus.RECOGNIZED
    ]
    if required_failed:
        return StateStatus.PARTIAL, tuple(reasons)
    if not recognized and not text:
        return StateStatus.UNAVAILABLE, tuple(reasons)
    return StateStatus.OK, tuple(reasons)


def build_game_state(
    scene: SceneRepresentation | None,
    observation: ObservationRef,
    *,
    game: str = "",
    sequence: int = 0,
    timestamp: str = "",
    profile_match: ProfileMatch | None = None,
    previous: GameState | None = None,
    ocr_available: bool | None = None,
    ocr_reason: str = "",
    detection_available: bool | None = None,
    detection_reason: str = "",
    max_entities: int = 32,
    latency: Mapping[str, Any] | None = None,
    extra_limitations: Sequence[str] = (),
    scene_tracker: SceneTracker | None = None,
) -> GameState:
    """Build one state from one perception scene.

    A ``scene`` of ``None`` is not an error here — it is an observation that
    produced no perception at all, and the state says so with ``UNAVAILABLE``
    and the reason, rather than pretending an empty screen was seen.
    """
    profile = profile_match or ProfileMatch(
        reason_code="no_profile_for_title",
        reason="No perception profile was resolved for this observation.",
    )
    rows = _text_rows(scene) if scene is not None else ()
    hud: list[HudReading] = []
    hud_boxes: list[BBox] = []
    scale_x = scale_y = 0.0
    if profile.profile is not None and scene is not None:
        scale_x, scale_y = profile.profile.scale_for(
            int(scene.width or observation.width or 0), int(scene.height or observation.height or 0)
        )
    if profile.profile is not None and (scale_x <= 0 or scale_y <= 0):
        # A profile with no measurable frame is a refusal, not a stretch.
        hud = [
            HudReading(
                element_id=spec.element_id,
                kind=spec.kind,
                label=spec.label,
                status=HudStatus.UNREADABLE,
                profile_id=profile.profile.profile_id,
                reason="The observation has no measured frame size, so regions cannot be placed.",
            )
            for spec in profile.profile.fields
        ]
    elif profile.profile is not None and scene is not None:
        for spec in profile.profile.fields:
            reading = _read_field(
                spec,
                scene,
                scale_x=scale_x,
                scale_y=scale_y,
                profile_id=profile.profile.profile_id,
                ocr_available=ocr_available,
                ocr_reason=ocr_reason,
            )
            hud.append(reading)
            if reading.box is not None:
                hud_boxes.append(reading.box)
    if scene is not None:
        entities, entity_status, entity_reason = _entities(
            scene,
            observation,
            hud_boxes=hud_boxes,
            detection_available=detection_available,
            detection_reason=detection_reason,
            max_entities=max_entities,
        )
    else:
        # Without a scene nothing was looked for, so the answer is unavailable —
        # never ``none``, which would claim a clean screen was seen.
        entities, entity_status = (), EntityStatus.UNAVAILABLE
        entity_reason = (
            detection_reason or "No perception scene was produced, so no entity was detected."
        )
    unknown: list[UnknownField] = []
    for item in hud:
        if item.status is HudStatus.RECOGNIZED:
            continue
        unknown.append(
            UnknownField(
                field_name=item.element_id,
                kind=item.kind.value,
                status=item.status.value,
                reason=item.reason,
            )
        )
    unsupported: list[str] = []
    if profile.profile is None:
        unsupported.append(
            "Game-specific HUD fields: no profile matched, so none is presumed "
            "(health, score, timers, menus and objectives are all unread)."
        )
    else:
        for item in hud:
            if item.status is HudStatus.UNSUPPORTED:
                unsupported.append(f"{item.label} ({item.kind.value}): {item.reason}")
        unsupported.extend(profile.profile.limitations)
    status, reasons = _status_for(
        hud,
        profile=profile,
        text=rows,
        entity_status=entity_status,
        ocr_available=ocr_available,
    )
    # NF-24: name the screen this frame shows, then ask the tracker whether the
    # change earned a fact. Both answers are the frame's own; neither is carried
    # over from the previous state unless the tracker confirmed it.
    scene_assessment = assess_scene(
        hud=hud,
        text=rows,
        profile=profile.profile,
        ocr_available=ocr_available,
        ocr_reason=ocr_reason,
    )
    transition = (
        scene_tracker.observe(
            scene_assessment,
            observation.observation_id,
            sequence=observation.seq,
        )
        if scene_tracker is not None
        else SceneTransition(
            observation_id=observation.observation_id,
            reason=(
                "No scene tracker is attached, so this reading carries the screen it "
                "saw and no transition verdict."
            ),
        )
    )
    tracking = Tracking()
    changes: tuple[StateChange, ...] = ()
    if previous is not None:
        same_session = previous.observation.session_id == observation.session_id
        same_profile = (
            (previous.profile.profile.profile_id if previous.profile.profile else "")
            == (profile.profile.profile_id if profile.profile else "")
        )
        if not same_session:
            tracking = Tracking(reason="the previous state belongs to a different session")
        elif not same_profile:
            tracking = Tracking(
                reason="the perception profile changed, so the fields are not comparable"
            )
        else:
            dropped = observation.seq - previous.observation.seq - 1
            tracking = Tracking(
                compared=True,
                previous_state_id=previous.state_id,
                previous_observation_id=previous.observation.observation_id,
                reason="compared with the previous observation in this session",
                dropped_observations=max(0, dropped) if dropped >= 0 else None,
            )
            collected: list[StateChange] = []
            for item in hud:
                if item.status is HudStatus.UNSUPPORTED:
                    continue
                collected.extend(
                    _changes(previous, item, observation_id=observation.observation_id)
                )
            for item in previous.hud:
                if item.element_id in {row.element_id for row in hud}:
                    continue
                collected.append(
                    StateChange(
                        kind="hud_disappeared",
                        field_name=item.element_id,
                        before=item.text,
                        detail=f"{item.label} is no longer declared by this profile.",
                        observation_id=observation.observation_id,
                    )
                )
            if previous.entity_status is not entity_status:
                collected.append(
                    StateChange(
                        kind="entity_status_changed",
                        field_name="entities",
                        before=previous.entity_status.value,
                        after=entity_status.value,
                        detail="The detector's answer about entities changed.",
                        observation_id=observation.observation_id,
                    )
                )
            elif len(previous.entities) != len(entities):
                collected.append(
                    StateChange(
                        kind="entity_count_changed",
                        field_name="entities",
                        before=str(len(previous.entities)),
                        after=str(len(entities)),
                        detail=(
                            f"{len(previous.entities)} region(s) became {len(entities)}; this "
                            "is a count of detected regions, not of game objects."
                        ),
                        observation_id=observation.observation_id,
                    )
                )
            # A first confirmation has no previous scene (it was unknown), and
            # that is still a change: "unknown → victory" is a fact worth listing.
            if transition.state == "confirmed":
                collected.append(
                    StateChange(
                        kind="scene_changed",
                        field_name="scene",
                        before=transition.previous,
                        after=transition.confirmed,
                        detail=(
                            f"Confirmed after {transition.required} consecutive "
                            f"observation(s) agreed; the scene was read as "
                            f"{scene_assessment.status} ({scene_assessment.source})."
                        ),
                        observation_id=observation.observation_id,
                        # A generic-cue scene change is an inference about the
                        # screen, not a field reading, and says so.
                        inferred=scene_assessment.status != "observed",
                    )
                )
            changes = tuple(collected)
    limitations = [
        "This is a reading of the screen, not an understanding of the game: no "
        "objective, health, inventory or player intent is inferred.",
        "Entity kinds stay 'unknown': the shipped detector is connected components, "
        "and no semantic detector is wired on this machine.",
    ]
    measured = getattr(scene, "confidence", None) if scene is not None else None
    if measured is None:
        limitations.append(
            "No confidence is reported because no perception provider measured one for "
            "this scene; 'quality' is a heuristic coverage count."
        )
    else:
        limitations.append(
            f"Confidence {float(measured):.2f} is the mean of the provider confidences "
            "that were measured, not a calibrated probability that this state is correct."
        )
    limitations.append(
        "The screen label is only as good as the profile's declared rules or an "
        "unambiguous text cue; 'unknown' means this build could not name the screen."
    )
    limitations.extend(extra_limitations)
    if profile.profile is not None:
        limitations.extend(profile.profile.limitations)
    quality = _quality(hud, profile)
    confidence = None if measured is None else round(float(measured), 4)
    return GameState(
        game=game or observation.game,
        observation=observation,
        sequence=sequence,
        timestamp=timestamp,
        status=status,
        hud=tuple(hud),
        text=rows,
        entities=entities,
        entity_status=entity_status,
        entity_reason=entity_reason,
        interface=_interface(hud, profile),
        scene=scene_assessment,
        transition=transition,
        changes=changes,
        tracking=tracking,
        unknown=tuple(unknown),
        unsupported=tuple(unsupported),
        profile=profile,
        confidence=confidence,
        uncertainty=(None if confidence is None else round(1.0 - confidence, 4)),
        quality=quality,
        latency=dict(latency or {}),
        reasons=reasons,
        limitations=tuple(limitations),
    )
