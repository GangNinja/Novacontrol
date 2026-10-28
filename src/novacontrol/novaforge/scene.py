"""NF-24 — what SCREEN a frame shows, and when a change to it is a fact.

NF-16 answered *which declared fields were read*. This module answers the next
question — menu, loading, gameplay, pause, death, victory, defeat, or unknown —
without inventing an answer:

* A scene is **observed** only when a profile's declared rule fires over a
  RECOGNIZED HUD field. The rule names the field and the text patterns, so the
  evidence is the same kind NF-16 already records.
* With no rule, an unambiguous generic cue (``"paused"``, ``"mission complete"``,
  …) may classify the screen as **inferred**, the cue is quoted, and the
  assessment says so. No cue ⇒ ``unknown``.
* Two rules/cues naming different scenes is a CONTRADICTION, recorded as one;
  the answer stays ``unknown`` rather than picking a favourite.

A single frame is never a transition. :class:`SceneTracker` requires a
configured run of consecutive agreeing observations before a new scene is
CONFIRMED; until then the observation is reported as ``pending`` and the last
confirmed scene is left alone. A frame that cannot be classified neither
confirms nor resets a run (an OCR dropout is not evidence about the game), and
a re-read of the SAME observation never counts twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only; no runtime import cycle
    from collections.abc import Mapping, Sequence

    from novacontrol.novaforge.state import GameProfile, HudReading

__all__ = [
    "SCENE_ASSESSMENT_STATES",
    "SCENE_KINDS",
    "SceneAssessment",
    "SceneKind",
    "SceneRule",
    "SceneTracker",
    "SceneTransition",
    "assess_scene",
]


class SceneKind(StrEnum):
    """The screens this build can name — and ``UNKNOWN``, which is an answer."""

    MENU = "menu"
    LOADING = "loading"
    GAMEPLAY = "gameplay"
    PAUSE = "pause"
    DEATH = "death"
    VICTORY = "victory"
    DEFEAT = "defeat"
    UNKNOWN = "unknown"


SCENE_KINDS = tuple(item.value for item in SceneKind)

#: How a scene was decided. ``observed`` needs a declared rule over a read
#: field; ``inferred`` is a generic cue; ``unknown`` means nothing decided it.
SCENE_ASSESSMENT_STATES = ("observed", "inferred", "unknown")

#: How many contradiction messages a tracker keeps. They are operational notes,
#: not history: an unbounded list would grow for the life of the process while
#: the run count itself is reset on every confirmation.
MAX_TRACKED_CONTRADICTIONS = 8


@lru_cache(maxsize=256)
def _cue_pattern(text: str) -> re.Pattern[str]:
    """One cue as a whole-word phrase, compiled once and reused.

    A bare substring test makes ``"game over"`` match inside ``"game overlay"``
    and ``"options"`` inside ``"controller options help"`` — the first is a death
    screen accusation, the second is a menu accusation, and both would be false.
    Word boundaries keep a cue to the words it names while still matching
    punctuation and casing as written.

    Cached because profile rules call it per frame, in a loop: the same handful
    of phrases must not be compiled again for every observation.
    """
    parts = [re.escape(part) for part in str(text).split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(parts) + r"(?!\w)", re.IGNORECASE)

#: Stable order for alternatives/contradictions in a payload.
_SCENE_ORDER = {kind: index for index, kind in enumerate(SceneKind)}


def _kind(value: Any) -> SceneKind:
    try:
        return SceneKind(str(value))
    except ValueError:
        return SceneKind.UNKNOWN


@dataclass(frozen=True, slots=True)
class SceneRule:
    """One declared screen rule: a HUD field plus the text that names a scene.

    Both parts are required. A rule without a field would match free text and
    call it observed; a rule without patterns would claim every recognized field
    is the same screen. ``patterns`` are matched as whole-word phrases, and the
    reading's own text is quoted as the evidence.

    ``min_confidence`` is the rule's own floor on the reading it fires over. A
    profile whose layout is ambiguous can require a well-read field before its
    rule is allowed to name a screen; a reading with no measured confidence
    never clears a floor, because an unmeasured score is not a high one.
    """

    scene: SceneKind
    field: str
    patterns: tuple[str, ...]
    min_confidence: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scene, SceneKind) or self.scene is SceneKind.UNKNOWN:
            raise ValueError(f"A scene rule needs a named scene, not {self.scene!r}.")
        if not str(self.field or "").strip():
            raise ValueError(f"Scene rule {self.scene.value!r} names no HUD field.")
        cleaned = tuple(str(item).strip().casefold() for item in self.patterns if str(item).strip())
        if not cleaned:
            raise ValueError(f"Scene rule {self.scene.value!r} declares no text pattern.")
        object.__setattr__(self, "patterns", cleaned)
        if self.min_confidence is not None:
            floor = float(self.min_confidence)
            if not 0.0 <= floor <= 1.0:
                raise ValueError(
                    f"Scene rule {self.scene.value!r} requires a confidence in [0, 1], "
                    f"not {floor!r}."
                )
            object.__setattr__(self, "min_confidence", floor)

    def matches(self, text: str) -> str:
        """The pattern that fired, or ``""``."""
        for pattern in self.patterns:
            if _cue_pattern(pattern).search(str(text or "")):
                return pattern
        return ""

    def meets_confidence(self, confidence: float | None) -> bool:
        """Whether a reading's measured confidence clears this rule's floor."""
        if self.min_confidence is None:
            return True
        return confidence is not None and float(confidence) >= self.min_confidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene": self.scene.value,
            "field": self.field,
            "patterns": list(self.patterns),
            "min_confidence": self.min_confidence,
        }


@dataclass(frozen=True, slots=True)
class SceneAssessment:
    """One frame's answer about the screen, with its evidence and its gaps."""

    kind: SceneKind = SceneKind.UNKNOWN
    status: str = "unknown"
    source: str = "none"
    confidence: float | None = None
    evidence: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    profile_id: str = ""

    def __post_init__(self) -> None:
        if self.status not in SCENE_ASSESSMENT_STATES:
            raise ValueError(f"Unknown scene assessment status: {self.status!r}")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("A scene confidence must be a probability in [0, 1].")
        if self.kind is SceneKind.UNKNOWN and self.status != "unknown":
            raise ValueError("An unknown scene cannot be reported as observed or inferred.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "status": self.status,
            "source": self.source,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "alternatives": list(self.alternatives),
            "contradictions": list(self.contradictions),
            "profile_id": self.profile_id,
        }


@dataclass(frozen=True, slots=True)
class SceneTransition:
    """What changed about the confirmed scene for one observation — or did not.

    ``confirmed`` is the stable answer (initialised ``unknown``); ``candidate`` is
    what this frame proposed. ``state`` is one of ``none`` (no change or nothing
    to say), ``pending`` (a candidate is accumulating), ``confirmed`` (the
    candidate reached the required run), ``contradicted`` (the candidate changed
    before it could be confirmed) and ``duplicate`` (the same observation was
    read again).
    """

    state: str = "none"
    confirmed: str = SceneKind.UNKNOWN.value
    candidate: str = SceneKind.UNKNOWN.value
    confirmations: int = 0
    required: int = 2
    previous: str = ""
    observation_id: str = ""
    sequence: int | None = None
    reason: str = ""
    contradictions: tuple[str, ...] = ()
    interrupted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "confirmed": self.confirmed,
            "candidate": self.candidate,
            "confirmations": self.confirmations,
            "required": self.required,
            "previous": self.previous,
            "observation_id": self.observation_id,
            "sequence": self.sequence,
            "reason": self.reason,
            "contradictions": list(self.contradictions),
            "interrupted": self.interrupted,
        }


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

#: Generic cues, used ONLY when no profile rule fired. They are evidence a
#: person can check (the cue is quoted), never a calibrated guess, so they are
#: reported as ``inferred``. ``gameplay`` has no cue: "absence of a menu" is not
#: evidence of gameplay, and a cue like "health" appears on menus too.
#:
#: A bare screen noun ("victory", "defeat") is deliberately NOT a cue. Mission
#: instructions use exactly those words — an on-screen objective reading "Defeat
#: the boss" would otherwise name the DEFEAT screen, and two such frames would
#: confirm it and fail the objective. A banner that says only that word is a
#: profile's job, because a profile declares the region it was verified in.
_GENERIC_CUES: tuple[tuple[SceneKind, tuple[str, ...]], ...] = (
    (SceneKind.LOADING, ("loading", "please wait", "now loading")),
    (SceneKind.PAUSE, ("paused", "resume game", "press esc to resume")),
    (SceneKind.DEATH, ("you died", "you are dead", "game over")),
    (SceneKind.VICTORY, ("mission complete", "level complete", "you win")),
    (SceneKind.DEFEAT, ("mission failed", "you lose", "you have been defeated")),
    (
        SceneKind.MENU,
        ("main menu", "press start", "press any key", "new game", "options", "quit game"),
    ),
)

#: The same cues, compiled once as whole-word phrases (see ``_cue_pattern``).
_GENERIC_CUE_PATTERNS: tuple[tuple[SceneKind, tuple[tuple[str, re.Pattern[str]], ...]], ...] = tuple(
    (scene, tuple((cue, _cue_pattern(cue)) for cue in cues)) for scene, cues in _GENERIC_CUES
)


def _mean(values: Sequence[float | None]) -> float | None:
    measured = [float(value) for value in values if value is not None]
    if not measured:
        return None
    return round(sum(measured) / len(measured), 4)


def _reading_confidence(readings: Sequence[HudReading]) -> float | None:
    return _mean([getattr(reading, "confidence", None) for reading in readings])


def _generic_cues(
    text_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[SceneKind, list[str]], float | None]:
    """Which scenes a generic cue names, the quotes, and the measured confidence."""
    hits: dict[SceneKind, list[str]] = {}
    scores: list[float | None] = []
    for row in text_rows:
        line = str(row.get("text") or "").strip()
        if not line:
            continue
        for scene, cues in _GENERIC_CUE_PATTERNS:
            for cue, pattern in cues:
                if pattern.search(line) is None:
                    continue
                quote = f"{cue!r} in {line!r}"
                bucket = hits.setdefault(scene, [])
                if quote not in bucket:
                    bucket.append(quote)
                confidence = row.get("confidence")
                scores.append(confidence if isinstance(confidence, (int, float)) else None)
    return hits, _mean(scores)


def assess_scene(
    *,
    hud: Sequence[HudReading],
    text: Sequence[Mapping[str, Any]],
    profile: GameProfile | None = None,
    ocr_available: bool | None = None,
    ocr_reason: str = "",
) -> SceneAssessment:
    """Name the screen from what was READ, or say why it cannot be named.

    Order is deliberate: a profile's declared rule outranks a generic cue (the
    profile knows this layout), and a generic cue is never promoted to
    ``observed``. When two declared rules disagree, or two generic cues name
    different screens, the answer is ``unknown`` plus the contradiction — the
    caller must not pick one.

    A profile's ``required_capabilities`` are a precondition, not a preference: a
    profile that needs OCR is refused outright when OCR is known to be missing,
    because its rules cannot have been read and returning ``unknown`` with that
    reason is more useful than a cue from a half-wired pipeline.
    """
    profile_id = str(getattr(profile, "profile_id", "") or "")
    required = tuple(str(item) for item in (getattr(profile, "required_capabilities", ()) or ()))
    if "ocr" in required and ocr_available is False:
        return SceneAssessment(
            kind=SceneKind.UNKNOWN,
            status="unknown",
            source="capability",
            evidence=(
                "this profile requires OCR ("
                f"{ocr_reason or 'no OCR engine is available'}), so its screen rules "
                "cannot be read",
            ),
            profile_id=profile_id,
        )
    readings = {
        str(getattr(reading, "element_id", "")): reading
        for reading in hud
        if getattr(reading, "status", None) is not None
    }
    #: Scenes whose declared rule matched the text but failed its confidence
    #: floor. A weaker cue is not allowed to decide the same claim afterwards:
    #: evidence that was rejected cannot be replaced by lesser evidence.
    blocked: set[SceneKind] = set()
    if profile is not None and profile.scene_rules:
        matched: dict[SceneKind, list[tuple[str, str, str, float | None]]] = {}
        missing: list[str] = []
        for rule in profile.scene_rules:
            reading = readings.get(rule.field)
            if reading is None or str(getattr(reading.status, "value", reading.status)) != "recognized":
                missing.append(f"rule {rule.scene.value!r} needs field {rule.field!r}, which was not recognized")
                continue
            reading_text = str(getattr(reading, "text", "") or "")
            hit = rule.matches(reading_text)
            if not hit:
                continue
            confidence = getattr(reading, "confidence", None)
            measured = float(confidence) if confidence is not None else None
            if not rule.meets_confidence(measured):
                blocked.add(rule.scene)
                floor = rule.min_confidence
                missing.append(
                    f"rule {rule.scene.value!r} matched field {rule.field!r} but the "
                    + (
                        "reading has no measured confidence"
                        if measured is None
                        else f"reading confidence {measured:.2f}"
                    )
                    + f" is below the declared floor {floor:.2f}"
                )
                continue
            matched.setdefault(rule.scene, []).append(
                (rule.field, hit, reading_text, measured)
            )
        if len(matched) == 1:
            scene, rows = next(iter(matched.items()))
            evidence = tuple(
                f"declared rule for {scene.value!r}: field {field!r} read {text!r} (matched {hit!r})"
                for field, hit, text, _confidence in rows
            )
            return SceneAssessment(
                kind=scene,
                status="observed",
                source="profile_rule",
                confidence=_mean([row[3] for row in rows]),
                evidence=evidence,
                profile_id=profile_id,
            )
        if len(matched) > 1:
            alternatives = tuple(kind.value for kind in sorted(matched, key=lambda k: _SCENE_ORDER[k]))
            contradictions = tuple(
                f"declared rules name different scenes: {', '.join(alternatives)}"
                for _ in (0,)
            )
            evidence = tuple(
                f"rule for {scene.value!r} fired on field {field!r} ({hit!r})"
                for scene, rows in matched.items()
                for field, hit, _text, _confidence in rows
            )
            return SceneAssessment(
                kind=SceneKind.UNKNOWN,
                status="unknown",
                source="profile_rule",
                evidence=evidence,
                alternatives=alternatives,
                contradictions=contradictions,
                profile_id=profile_id,
            )
        # No rule fired: fall through to generic cues, but keep the profile's
        # misses as evidence so an operator can see why no rule applied.
        rule_notes = tuple(dict.fromkeys(missing))[:4]

    else:
        rule_notes = ()

    hits, confidence = _generic_cues(text)
    for scene in blocked:
        hits.pop(scene, None)
    ordered = sorted(hits, key=lambda kind: _SCENE_ORDER.get(kind, 99))
    quotes = tuple(f"generic cue {quote}" for kind in ordered for quote in hits[kind])
    if len(ordered) == 1:
        return SceneAssessment(
            kind=ordered[0],
            status="inferred",
            source="text_cue",
            confidence=confidence,
            evidence=quotes[:4] + rule_notes,
            profile_id=profile_id,
        )
    if ordered:
        # Cues named more than one scene: a contradiction, not a choice.
        return SceneAssessment(
            kind=SceneKind.UNKNOWN,
            status="unknown",
            source="text_cue",
            evidence=quotes[:4] + rule_notes,
            alternatives=tuple(kind.value for kind in ordered),
            contradictions=("generic cues name different scenes",),
            profile_id=profile_id,
        )
    if ocr_available is False:
        reason = ocr_reason or "no OCR engine is available, so no text could name the screen"
    elif profile is None:
        reason = "no profile matched, so no scene rule exists for this screen and no unambiguous cue was read"
    else:
        reason = "no declared scene rule fired and no unambiguous cue was read"
    return SceneAssessment(
        kind=SceneKind.UNKNOWN,
        status="unknown",
        source="none",
        evidence=(reason, *rule_notes),
        profile_id=profile_id,
    )


# ---------------------------------------------------------------------------
# Temporal confirmation
# ---------------------------------------------------------------------------


@dataclass
class SceneTracker:
    """A run of agreeing observations, and the last scene that earned a fact.

    The tracker never edits the observation's own assessment: ``pending`` and
    ``confirmed`` are separate answers, so a panel can always show what this
    frame looked like AND what the last confirmed screen was.
    """

    confirmations: int = 2
    confirmed: SceneKind = SceneKind.UNKNOWN
    pending: SceneKind = SceneKind.UNKNOWN
    streak: int = 0
    last_observation_id: str = ""
    #: The sequence number of the last observation fed in. A jump means the
    #: capture path lost frames, so the run on either side is not contiguous.
    last_sequence: int | None = None
    contradictions: list[str] = field(default_factory=list)
    #: The verdict of the last ``observe`` call, so it can be persisted and
    #: resumed — a restart continues a run instead of forgetting it.
    last: SceneTransition | None = None

    def __post_init__(self) -> None:
        self.confirmations = max(1, int(self.confirmations))
        self.confirmed = _kind(self.confirmed)
        self.pending = _kind(self.pending)

    def reset(self, *, confirmed: SceneKind = SceneKind.UNKNOWN) -> None:
        """Forget the run (a new session or a reset), keeping the configured rule."""
        self.confirmed = confirmed
        self.pending = SceneKind.UNKNOWN
        self.streak = 0
        self.last_observation_id = ""
        self.last_sequence = None
        self.contradictions.clear()
        self.last = None

    def restore(self, transition: SceneTransition | None) -> None:
        """Continue from a persisted transition instead of starting over.

        A state service that is restarted mid-run must not require the whole
        confirmation window again for a scene that was already confirmed, and
        must not forget a candidate that was accumulating.
        """
        if transition is None:
            return
        self.confirmed = _kind(transition.confirmed)
        self.pending = _kind(transition.candidate)
        self.streak = max(0, int(transition.confirmations))
        self.last_observation_id = str(transition.observation_id or "")
        self.last_sequence = transition.sequence
        self.contradictions = list(transition.contradictions)
        self._trim_contradictions()
        self.last = transition

    def _trim_contradictions(self) -> None:
        """Keep the contradiction note list bounded (oldest notes go first)."""
        if len(self.contradictions) > MAX_TRACKED_CONTRADICTIONS:
            del self.contradictions[: -MAX_TRACKED_CONTRADICTIONS]

    def observe(
        self,
        assessment: SceneAssessment,
        observation_id: str,
        *,
        sequence: int | None = None,
    ) -> SceneTransition:
        """Feed one frame's assessment; get the transition it justifies.

        Rereading the SAME observation is a duplicate, not a second confirmation:
        otherwise clicking "analyze" twice would confirm any one frame.

        ``sequence`` is the observation's own monotonically increasing number.
        A jump means the capture path dropped frames, and sightings on either
        side of a gap never count as a consecutive run — otherwise a scene seen
        once before an interruption plus once after would "confirm" itself
        while the screen in between was never observed at all.
        """
        if observation_id and observation_id == self.last_observation_id:
            return self._transition(
                "duplicate",
                observation_id=observation_id,
                reason=(
                    "the same observation was read again; a repeated reading is not a "
                    "transition"
                ),
            )
        self.last_observation_id = str(observation_id or "")
        interrupted = False
        gap_note = ""
        if sequence is not None and self.last_sequence is not None:
            if int(sequence) != self.last_sequence + 1:
                interrupted = True
                gap_note = (
                    f"observation sequence skipped from {self.last_sequence} to "
                    f"{int(sequence)}; sightings across a capture interruption are not "
                    "a consecutive run"
                )
                if self.streak > 0 and self.pending is not SceneKind.UNKNOWN:
                    self.contradictions.append(
                        f"dropped {self.streak} unconfirmed {self.pending.value!r} "
                        "sighting(s) across a capture interruption"
                    )
                self.pending = SceneKind.UNKNOWN
                self.streak = 0
        if sequence is not None:
            self.last_sequence = int(sequence)
        kind = _kind(assessment.kind)
        if kind is SceneKind.UNKNOWN:
            # An unreadable frame says nothing about the game: keep the run.
            return self._transition(
                "none",
                observation_id=observation_id,
                reason=(
                    "the screen could not be classified, so the confirmed scene is "
                    "unchanged and the run is not counted"
                ),
                interrupted=interrupted,
            )
        if kind is self.confirmed:
            self.pending = SceneKind.UNKNOWN
            self.streak = 0
            self.contradictions.clear()
            return self._transition(
                "none",
                observation_id=observation_id,
                reason=(
                    f"the confirmed scene is still {kind.value}"
                    + (f" ({gap_note})" if gap_note else "")
                ),
                interrupted=interrupted,
            )
        contradictions: tuple[str, ...] = ()
        if self.pending is not kind and self.pending is not SceneKind.UNKNOWN:
            contradictions = (
                f"the candidate changed from {self.pending.value!r} to {kind.value!r} "
                "before either could be confirmed",
            )
            self.contradictions.extend(contradictions)
            self.streak = 1
        else:
            self.streak += 1
        self.pending = kind
        if self.streak >= self.confirmations:
            previous = self.confirmed
            self.confirmed = kind
            self.pending = SceneKind.UNKNOWN
            self.streak = 0
            return self._transition(
                "confirmed",
                observation_id=observation_id,
                previous=previous.value if previous is not SceneKind.UNKNOWN else "",
                reason=(
                    f"confirmed {kind.value!r} after {self.confirmations} consecutive "
                    "observation(s) agreed"
                    + (f" ({gap_note})" if gap_note else "")
                ),
                contradictions=contradictions,
                interrupted=interrupted,
            )
        return self._transition(
            "contradicted" if contradictions else "pending",
            observation_id=observation_id,
            reason=(
                contradictions[0]
                if contradictions
                else (
                    f"{kind.value!r} seen {self.streak} of {self.confirmations} required "
                    "time(s); the confirmed scene is unchanged"
                    + (f" ({gap_note})" if gap_note else "")
                )
            ),
            contradictions=contradictions,
            interrupted=interrupted,
        )

    def _transition(
        self,
        state: str,
        *,
        observation_id: str,
        reason: str,
        previous: str = "",
        contradictions: tuple[str, ...] = (),
        interrupted: bool = False,
    ) -> SceneTransition:
        self._trim_contradictions()
        verdict = SceneTransition(
            state=state,
            confirmed=self.confirmed.value,
            candidate=self.pending.value
            if self.pending is not SceneKind.UNKNOWN
            else self.confirmed.value,
            confirmations=self.streak,
            required=self.confirmations,
            previous=previous,
            observation_id=str(observation_id or ""),
            sequence=self.last_sequence,
            reason=reason,
            contradictions=contradictions,
            interrupted=interrupted,
        )
        self.last = verdict
        return verdict
