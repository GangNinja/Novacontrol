"""NF-17 — objective and progress understanding: what the screen ASKS for.

This module turns one NF-16 :class:`~novacontrol.novaforge.state.GameState` into
typed objectives — what the game appears to ask the player to do, what progress
was observed, and what evidence would let a completion or failure be believed.

The rules the module is built around, in the order they matter:

* **Explicit beats inferred.** An objective whose text was READ is ``explicit``.
  An objective derived from a declared counter and target says so (``inferred``)
  and never borrows the authority of printed instructions.
* **Disappearance is not completion.** An objective that stops being displayed
  becomes ``NOT_VERIFIABLE`` with that reason; it is never closed.
* **Completion needs evidence that says so.** An explicit completion message, or
  a declared counter reaching its declared target. A moving counter is progress,
  not success, and an OCR read is a reading, not a verdict.
* **Nothing is invented.** No objective is created from arbitrary UI text: the
  generic reader only accepts lines that name themselves (``Objective:``,
  ``Mission:``, …), and a game-specific profile must declare its regions.
* **Conflicts stay conflicts.** Completion and failure text in the same reading
  produce a diagnostic and no verdict, never a coin flip.
* **Corrections are recorded.** A terminal verdict contradicted by later
  evidence is corrected with an event that says what changed the mind.

Game-specific knowledge lives in :class:`ObjectiveProfile` objects keyed by the
SAME id as the NF-16 perception profile; the interpretation logic never branches
on a game's name.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from .state import GameState, HudReading, HudStatus

__all__ = [
    "OBJECTIVE_CATEGORIES",
    "OBJECTIVE_STATUSES",
    "ObjectiveCategory",
    "ObjectiveDependency",
    "ObjectiveDiagnostics",
    "ObjectiveEvent",
    "ObjectiveEventKind",
    "ObjectiveEvidenceRef",
    "ObjectiveProfile",
    "ObjectiveProfileRegistry",
    "ObjectiveRule",
    "ObjectiveState",
    "ObjectiveStatus",
    "ProgressKind",
    "ProgressRecord",
    "SCHEMA_VERSION",
    "UnderstoodObjective",
    "builtin_objective_profiles",
    "default_objective_registry",
    "interpret",
    "objective_id_for",
]

#: The schema tag every objective record carries.
SCHEMA_VERSION = "1"

#: Bounded sizes: an objective list and its event log are small by construction.
MAX_OBJECTIVES = 12
MAX_EVENTS = 64
MAX_DESCRIPTION = 300
MAX_SOURCE_TEXT = 400
MAX_EVIDENCE = 8


class ObjectiveStatus(StrEnum):
    """Where one objective stands, from discovery to a believed outcome.

    ``NOT_VERIFIABLE`` is deliberately not a synonym for ``UNKNOWN``: it means an
    interpretation was attempted and the available evidence cannot establish a
    verdict (the objective vanished, or a completion banner could not be
    attributed). ``UNKNOWN`` is "an objective is evidently on screen and its
    content could not be read at all".
    """

    UNKNOWN = "unknown"
    DISCOVERED = "discovered"
    ACTIVE = "active"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"
    NOT_VERIFIABLE = "not_verifiable"


OBJECTIVE_STATUSES = tuple(item.value for item in ObjectiveStatus)

#: The statuses a later observation may not silently overwrite.
TERMINAL_STATUSES = frozenset({ObjectiveStatus.COMPLETED, ObjectiveStatus.FAILED})


class ObjectiveCategory(StrEnum):
    """What kind of task the objective looks like — a label, not a claim."""

    MISSION = "mission"
    QUEST = "quest"
    TUTORIAL = "tutorial"
    COUNTER = "counter"
    SURVIVAL = "survival"
    SCORE = "score"
    ROUND = "round"
    UNKNOWN = "unknown"


OBJECTIVE_CATEGORIES = tuple(item.value for item in ObjectiveCategory)


class EvidenceKind(StrEnum):
    """What one piece of evidence IS, so a verdict can name its basis."""

    OBJECTIVE_TEXT = "objective_text"
    PROGRESS = "progress"
    COMPLETION_TEXT = "completion_text"
    FAILURE_TEXT = "failure_text"
    COUNTER_TARGET = "counter_target"
    DEPENDENCY = "dependency"
    SESSION = "session"
    #: NF-24: a CONFIRMED outcome screen (victory/defeat) is evidence about the
    #: run, not about one field — the summary always names how it was read.
    SCENE = "scene"


class ProgressKind(StrEnum):
    """How progress is known. ``NONE`` is a real answer."""

    NUMERIC = "numeric"
    STAGE = "stage"
    QUALITATIVE = "qualitative"
    NONE = "none"


class ObjectiveEventKind(StrEnum):
    """The transitions the history records, and nothing beyond them."""

    DISCOVERED = "discovered"
    ACTIVATED = "activated"
    PROGRESS = "progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"
    CORRECTED = "corrected"
    UNVERIFIABLE = "unverifiable"


#: Only a line that names itself as an objective is read as one. An instruction
#: inside a legal disclaimer that happens to say "mission" is not an objective,
#: which is why the keyword has to LEAD the line and be followed by a separator.
_GENERIC_OBJECTIVE = re.compile(
    r"^\s*(?P<keyword>objective|mission|quest|task|tutorial)\s*(?:[:\-–—]|\u2014)\s*(?P<text>\S.*)$",
    re.IGNORECASE,
)

#: A declared completion message. Both words must be present and adjacent: "the
#: mission failed to start" must not read as a completion.
_COMPLETION_TEXT = re.compile(
    r"\b(?:objective|mission|quest|task)\s+(?:complete|completed|accomplished|successful)\b",
    re.IGNORECASE,
)
_FAILURE_TEXT = re.compile(
    r"\b(?:objective|mission|quest|task)\s+(?:failed|failure)\b",
    re.IGNORECASE,
)

_CATEGORY_FOR_KEYWORD = {
    "mission": ObjectiveCategory.MISSION,
    "objective": ObjectiveCategory.MISSION,
    "task": ObjectiveCategory.MISSION,
    "quest": ObjectiveCategory.QUEST,
    "tutorial": ObjectiveCategory.TUTORIAL,
}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _norm(value: str) -> str:
    """Case- and spacing-insensitive form used to compare objective text."""
    return " ".join(str(value or "").casefold().split())


def utc_stamp() -> str:
    return datetime.now(UTC).isoformat()


def objective_id_for(source: str, text: str) -> str:
    """A stable id for one objective: same wording, same id, across readings."""
    return f"{source}:{_digest(_norm(text) or text)}"


def _rule_objective_id(profile_id: str, rule_id: str, text: str) -> str:
    """An id whose prefix names the rule slot, so a replacement is detectable."""
    return f"{profile_id}:{rule_id}:{_digest(_norm(text) or text)}"


# ---------------------------------------------------------------------------
# Evidence and progress
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObjectiveEvidenceRef:
    """One reading a claim rests on, traceable to the exact observation.

    ``summary`` is the sentence a panel shows; ``field``/``text``/``value`` keep
    the raw reading so a person can check the claim instead of trusting it.
    """

    kind: EvidenceKind
    summary: str
    at: str
    observation_id: str
    session_id: str
    field: str = ""
    text: str = ""
    value: float | None = None
    box: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EvidenceKind):
            raise ValueError(f"Unknown evidence kind: {self.kind!r}")
        if not str(self.summary or "").strip():
            raise ValueError("Evidence needs a summary a person can read.")
        if not str(self.observation_id or "").strip():
            raise ValueError("Evidence must name the observation it came from.")
        if self.value is not None and not math.isfinite(self.value):
            raise ValueError("Evidence values must be finite numbers.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "summary": self.summary,
            "at": self.at,
            "observation_id": self.observation_id,
            "session_id": self.session_id,
            "field": self.field,
            "text": self.text,
            "value": self.value,
            "box": dict(self.box) if self.box is not None else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "ObjectiveEvidenceRef | None":
        data = dict(payload or {})
        try:
            kind = EvidenceKind(str(data.get("kind", "")))
            return cls(
                kind=kind,
                summary=str(data.get("summary", "") or ""),
                at=str(data.get("at", "") or ""),
                observation_id=str(data.get("observation_id", "") or ""),
                session_id=str(data.get("session_id", "") or ""),
                field=str(data.get("field", "") or ""),
                text=str(data.get("text", "") or ""),
                value=_as_float(data.get("value")),
                box=data.get("box") if isinstance(data.get("box"), Mapping) else None,
            )
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class ProgressRecord:
    """What progress was OBSERVED, with the arithmetic kept honest.

    ``fraction`` exists only when a start, a value and a target are all known and
    the target is ahead of the start; otherwise it is ``None`` rather than a
    zero that reads like "no progress". ``resets`` counts observed decreases, so
    a reversal is reported instead of smoothed away.
    """

    kind: ProgressKind
    text: str = ""
    value: float | None = None
    target: float | None = None
    start: float | None = None
    fraction: float | None = None
    observation_id: str = ""
    at: str = ""
    resets: int = 0
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ProgressKind):
            raise ValueError(f"Unknown progress kind: {self.kind!r}")
        for name in ("value", "target", "start", "fraction"):
            figure = getattr(self, name)
            if figure is not None and not math.isfinite(figure):
                raise ValueError(f"Progress {name} must be a finite number.")
        if self.fraction is not None and not 0.0 <= self.fraction <= 1.0:
            raise ValueError("Progress fraction must be a fraction in [0, 1].")
        if self.resets < 0:
            raise ValueError("A reset count cannot be negative.")

    @property
    def numeric(self) -> bool:
        return self.kind is ProgressKind.NUMERIC and self.value is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "text": self.text,
            "value": self.value,
            "target": self.target,
            "start": self.start,
            "fraction": self.fraction,
            "observation_id": self.observation_id,
            "at": self.at,
            "resets": self.resets,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "ProgressRecord | None":
        data = dict(payload or {})
        try:
            record = cls(
                kind=ProgressKind(str(data.get("kind", "none"))),
                text=str(data.get("text", "") or ""),
                value=_as_float(data.get("value")),
                target=_as_float(data.get("target")),
                start=_as_float(data.get("start")),
                fraction=_as_float(data.get("fraction")),
                observation_id=str(data.get("observation_id", "") or ""),
                at=str(data.get("at", "") or ""),
                resets=int(data.get("resets", 0) or 0),
                reason=str(data.get("reason", "") or ""),
            )
        except (ValueError, TypeError):
            return None
        return record


def progress_fraction(value: float | None, start: float | None, target: float | None) -> float | None:
    """The only place a progress percentage is computed — or refused.

    ``None`` unless all three figures exist, the target is ahead of the start and
    the value is inside the observed range. A negative or >100% fraction would be
    an arithmetic artefact of a wrong target, not a measurement.
    """
    if value is None or start is None or target is None:
        return None
    span = target - start
    if span <= 0 or value < start or value > target:
        return None
    return round((value - start) / span, 4)


@dataclass(frozen=True, slots=True)
class ObjectiveDependency:
    """A prerequisite this build can REPRESENT, where a profile declares one."""

    requires: str
    source: str = "declared"
    satisfied: bool | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not str(self.requires or "").strip():
            raise ValueError("A dependency must name what it requires.")
        if self.source not in {"declared", "inferred"}:
            raise ValueError("A dependency source is 'declared' or 'inferred'.")
        if self.source == "inferred":
            raise ValueError(
                "This build records only declared dependencies: inferring one from "
                "screen order would invent a sequence the game never stated."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "requires": self.requires,
            "source": self.source,
            "satisfied": self.satisfied,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# The objective itself
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnderstoodObjective:
    """One objective, its evidence, and every reason to doubt it.

    ``confidence`` is ``None`` throughout this build: nothing measures a
    calibrated probability, and an invented figure beside a reading is worse than
    an explicit absence.
    """

    objective_id: str
    description: str
    session_id: str
    game: str = ""
    source: str = ""
    source_text: str = ""
    category: ObjectiveCategory = ObjectiveCategory.UNKNOWN
    status: ObjectiveStatus = ObjectiveStatus.DISCOVERED
    explicit: bool = True
    progress: ProgressRecord | None = None
    completion_conditions: tuple[str, ...] = ()
    failure_conditions: tuple[str, ...] = ()
    dependencies: tuple[ObjectiveDependency, ...] = ()
    evidence: tuple[ObjectiveEvidenceRef, ...] = ()
    first_seen: str = ""
    last_updated: str = ""
    reason: str = ""
    superseded_by: str = ""
    revision: int = 1
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not str(self.objective_id or "").strip():
            raise ValueError("An objective needs an id.")
        if not str(self.description or "").strip():
            raise ValueError("An objective needs a description.")
        if not str(self.session_id or "").strip():
            raise ValueError("An objective must be tied to a session.")
        if not isinstance(self.status, ObjectiveStatus):
            raise ValueError(f"Unknown objective status: {self.status!r}")
        if not isinstance(self.category, ObjectiveCategory):
            raise ValueError(f"Unknown objective category: {self.category!r}")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("A confidence must be a probability in [0, 1].")
        if self.revision < 1:
            raise ValueError("An objective revision starts at 1.")
        if self.status is ObjectiveStatus.SUPERSEDED and not self.superseded_by:
            raise ValueError("A superseded objective must name what replaced it.")
        if len(self.description) > MAX_DESCRIPTION:
            raise ValueError(f"An objective description must be at most {MAX_DESCRIPTION} characters.")

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def checkable(self) -> bool:
        """Whether any declared condition could ever establish a verdict."""
        return bool(self.completion_conditions or self.failure_conditions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective_id": self.objective_id,
            "description": self.description,
            "session_id": self.session_id,
            "game": self.game,
            "source": self.source,
            "source_text": self.source_text,
            "category": self.category.value,
            "status": self.status.value,
            "explicit": self.explicit,
            "progress": self.progress.to_dict() if self.progress is not None else None,
            "completion_conditions": list(self.completion_conditions),
            "failure_conditions": list(self.failure_conditions),
            "dependencies": [item.to_dict() for item in self.dependencies],
            "evidence": [item.to_dict() for item in self.evidence],
            "first_seen": self.first_seen,
            "last_updated": self.last_updated,
            "reason": self.reason,
            "superseded_by": self.superseded_by,
            "revision": self.revision,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "UnderstoodObjective | None":
        data = dict(payload or {})
        try:
            evidence = tuple(
                row
                for row in (
                    ObjectiveEvidenceRef.from_dict(item) for item in data.get("evidence") or []
                )
                if row is not None
            )
            dependencies = tuple(
                ObjectiveDependency(
                    requires=str(item.get("requires", "")),
                    source=str(item.get("source", "declared")),
                    satisfied=item.get("satisfied") if isinstance(item.get("satisfied"), bool) else None,
                    reason=str(item.get("reason", "") or ""),
                )
                for item in data.get("dependencies") or []
                if isinstance(item, Mapping)
            )
            return cls(
                objective_id=str(data.get("objective_id", "")),
                description=str(data.get("description", "")),
                session_id=str(data.get("session_id", "")),
                game=str(data.get("game", "") or ""),
                source=str(data.get("source", "") or ""),
                source_text=str(data.get("source_text", "") or ""),
                category=ObjectiveCategory(str(data.get("category", "unknown"))),
                status=ObjectiveStatus(str(data.get("status", "unknown"))),
                explicit=bool(data.get("explicit", True)),
                progress=ProgressRecord.from_dict(data.get("progress")),
                completion_conditions=tuple(str(item) for item in data.get("completion_conditions") or []),
                failure_conditions=tuple(str(item) for item in data.get("failure_conditions") or []),
                dependencies=dependencies,
                evidence=evidence,
                first_seen=str(data.get("first_seen", "") or ""),
                last_updated=str(data.get("last_updated", "") or ""),
                reason=str(data.get("reason", "") or ""),
                superseded_by=str(data.get("superseded_by", "") or ""),
                revision=int(data.get("revision", 1) or 1),
                confidence=_as_float(data.get("confidence")),
            )
        except (ValueError, TypeError):
            return None


@dataclass(frozen=True, slots=True)
class ObjectiveEvent:
    """One recorded transition, with the evidence that caused it."""

    kind: ObjectiveEventKind
    at: str
    session_id: str
    objective_id: str
    before: str = ""
    after: str = ""
    reason: str = ""
    evidence: tuple[ObjectiveEvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ObjectiveEventKind):
            raise ValueError(f"Unknown objective event kind: {self.kind!r}")
        if not str(self.session_id or "").strip():
            raise ValueError("An event must name its session.")
        if not str(self.objective_id or "").strip():
            raise ValueError("An event must name its objective.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "at": self.at,
            "session_id": self.session_id,
            "objective_id": self.objective_id,
            "before": self.before,
            "after": self.after,
            "reason": self.reason,
            "evidence": [item.to_dict() for item in self.evidence],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "ObjectiveEvent | None":
        data = dict(payload or {})
        try:
            evidence = tuple(
                row
                for row in (
                    ObjectiveEvidenceRef.from_dict(item) for item in data.get("evidence") or []
                )
                if row is not None
            )
            return cls(
                kind=ObjectiveEventKind(str(data.get("kind", ""))),
                at=str(data.get("at", "") or ""),
                session_id=str(data.get("session_id", "")),
                objective_id=str(data.get("objective_id", "")),
                before=str(data.get("before", "") or ""),
                after=str(data.get("after", "") or ""),
                reason=str(data.get("reason", "") or ""),
                evidence=evidence,
            )
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class ObjectiveDiagnostics:
    """Problems worth surfacing: conflicts and dependency declarations."""

    conflicts: tuple[str, ...] = ()
    dependency_issues: tuple[str, ...] = ()
    unattributed: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflicts": list(self.conflicts),
            "dependency_issues": list(self.dependency_issues),
            "unattributed": list(self.unattributed),
        }


@dataclass(frozen=True, slots=True)
class ObjectiveState:
    """Everything one interpretation produced, with its provenance."""

    session_id: str
    game: str
    observation_id: str
    at: str
    sequence: int = 0
    objectives: tuple[UnderstoodObjective, ...] = ()
    events: tuple[ObjectiveEvent, ...] = ()
    diagnostics: ObjectiveDiagnostics = field(default_factory=ObjectiveDiagnostics)
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    profile_id: str = ""
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not str(self.observation_id or "").strip():
            raise ValueError("An interpretation must name the observation it read.")
        if self.sequence < 0:
            raise ValueError("An interpretation sequence cannot be negative.")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"This build produces schema {SCHEMA_VERSION!r}, not {self.schema_version!r}."
            )

    @property
    def counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in ObjectiveStatus}
        for objective in self.objectives:
            counts[objective.status.value] += 1
        counts["total"] = len(self.objectives)
        counts["explicit"] = len([item for item in self.objectives if item.explicit])
        counts["inferred"] = len([item for item in self.objectives if not item.explicit])
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "game": self.game,
            "observation_id": self.observation_id,
            "at": self.at,
            "sequence": self.sequence,
            "profile_id": self.profile_id,
            "objectives": [item.to_dict() for item in self.objectives],
            "events": [item.to_dict() for item in self.events],
            "diagnostics": self.diagnostics.to_dict(),
            "counts": self.counts,
            "reasons": list(self.reasons),
            "limitations": list(self.limitations),
        }


# ---------------------------------------------------------------------------
# Game-specific objective rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObjectiveRule:
    """One game-specific objective shape: where its text and counter live.

    A rule names HUD element ids from the matching perception profile. It never
    names a pixel region itself — the state's readings are the input, so a rule
    that points at a field the profile does not declare is a configuration error
    the interpreter reports rather than a guess.
    """

    rule_id: str
    description: str
    category: ObjectiveCategory = ObjectiveCategory.MISSION
    objective_field: str = ""
    counter_field: str = ""
    target: float | None = None
    completion_texts: tuple[str, ...] = ()
    failure_texts: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    #: The rule's objective is inferred, not printed: a counter target with no
    #: objective text. Recorded so a reader can weight it accordingly.
    inferred: bool = False

    def __post_init__(self) -> None:
        if not str(self.rule_id or "").strip():
            raise ValueError("An objective rule needs an id.")
        if not str(self.description or "").strip():
            raise ValueError(f"Rule {self.rule_id!r} needs a description.")
        if not self.objective_field and not self.counter_field:
            raise ValueError(
                f"Rule {self.rule_id!r} names neither an objective field nor a counter field."
            )
        if self.target is not None and (not math.isfinite(self.target) or self.target <= 0):
            raise ValueError(f"Rule {self.rule_id!r} has a target that is not a positive number.")
        if self.target is not None and not self.counter_field:
            raise ValueError(f"Rule {self.rule_id!r} declares a target with no counter field.")
        if self.inferred and not self.counter_field:
            raise ValueError(f"Rule {self.rule_id!r} is inferred from nothing but text.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "description": self.description,
            "category": self.category.value,
            "objective_field": self.objective_field,
            "counter_field": self.counter_field,
            "target": self.target,
            "completion_texts": list(self.completion_texts),
            "failure_texts": list(self.failure_texts),
            "dependencies": list(self.dependencies),
            "inferred": self.inferred,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "ObjectiveRule | None":
        data = dict(payload or {})
        try:
            return cls(
                rule_id=str(data.get("rule_id", "")),
                description=str(data.get("description", "")),
                category=ObjectiveCategory(str(data.get("category", "mission"))),
                objective_field=str(data.get("objective_field", "") or ""),
                counter_field=str(data.get("counter_field", "") or ""),
                target=_as_float(data.get("target")),
                completion_texts=tuple(str(item) for item in data.get("completion_texts") or []),
                failure_texts=tuple(str(item) for item in data.get("failure_texts") or []),
                dependencies=tuple(str(item) for item in data.get("dependencies") or []),
                inferred=bool(data.get("inferred", False)),
            )
        except ValueError:
            return None


MAX_RULES = 16


@dataclass(frozen=True, slots=True)
class ObjectiveProfile:
    """The objective rules for one game layout, with where they came from."""

    profile_id: str
    game: str
    version: str
    provenance: str
    rules: tuple[ObjectiveRule, ...] = ()
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.profile_id or "").strip():
            raise ValueError("An objective profile needs a profile_id.")
        if not str(self.version or "").strip():
            raise ValueError(f"Objective profile {self.profile_id!r} needs a version.")
        if not str(self.provenance or "").strip():
            raise ValueError(f"Objective profile {self.profile_id!r} needs provenance.")
        if len(self.rules) > MAX_RULES:
            raise ValueError(f"Objective profile {self.profile_id!r} declares too many rules.")

    def problems(self) -> list[str]:
        """Everything wrong with this profile, deterministically — empty is valid."""
        problems: list[str] = []
        rules = {rule.rule_id: rule for rule in self.rules}
        if len(rules) != len(self.rules):
            problems.append("two rules share a rule_id")
        for rule in self.rules:
            for dependency in rule.dependencies:
                if dependency not in rules:
                    problems.append(
                        f"rule {rule.rule_id!r} requires {dependency!r}, which this profile "
                        "does not declare"
                    )
            if rule.rule_id in rule.dependencies:
                problems.append(f"rule {rule.rule_id!r} depends on itself")
        cycle = self._cycle()
        if cycle:
            problems.append(f"dependency cycle: {' -> '.join(cycle)}")
        return problems

    def _cycle(self) -> tuple[str, ...]:
        """The first dependency cycle, as a path, or an empty tuple."""
        edges = {rule.rule_id: tuple(rule.dependencies) for rule in self.rules}
        state: dict[str, int] = {}  # 0 unvisited, 1 visiting, 2 done
        stack: list[str] = []

        def walk(node: str) -> tuple[str, ...]:
            state[node] = 1
            stack.append(node)
            for child in edges.get(node, ()):
                if child not in edges:
                    continue
                if state.get(child) == 1:
                    start = stack.index(child)
                    return tuple(stack[start:] + [child])
                if state.get(child, 0) == 0:
                    found = walk(child)
                    if found:
                        return found
            stack.pop()
            state[node] = 2
            return ()

        for rule in self.rules:
            if state.get(rule.rule_id, 0) == 0:
                found = walk(rule.rule_id)
                if found:
                    return found
        return ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "game": self.game,
            "version": self.version,
            "provenance": self.provenance,
            "rules": [rule.to_dict() for rule in self.rules],
            "limitations": list(self.limitations),
        }


class ObjectiveProfileRegistry:
    """Deterministic lookup; a broken profile is a recorded rejection, not a crash."""

    def __init__(self, profiles: Iterable[ObjectiveProfile] = ()) -> None:
        self._profiles: dict[str, ObjectiveProfile] = {}
        self._rejected: dict[str, str] = {}
        for profile in profiles:
            self.register(profile)

    @property
    def rejected(self) -> dict[str, str]:
        return dict(self._rejected)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))

    def register(self, profile: Any) -> str:
        try:
            if not isinstance(profile, ObjectiveProfile):
                raise ValueError(
                    f"an objective profile must be an ObjectiveProfile, not {type(profile).__name__}"
                )
            problems = profile.problems()
            if problems:
                raise ValueError("; ".join(problems))
            if profile.profile_id in self._profiles:
                raise ValueError(f"objective profile {profile.profile_id!r} is already registered")
            self._profiles[profile.profile_id] = profile
            self._rejected.pop(profile.profile_id, None)
            return ""
        except Exception as exc:  # noqa: BLE001 - a bad profile is data, not a crash
            claimed = str(profile.get("profile_id") or "") if isinstance(profile, Mapping) else ""
            key = claimed or str(getattr(profile, "profile_id", "") or "unidentified")
            self._rejected[key] = f"{type(exc).__name__}: {exc}"
            return self._rejected[key]

    def get(self, profile_id: str) -> ObjectiveProfile | None:
        return self._profiles.get(str(profile_id or "").strip())

    def rows(self) -> list[dict[str, Any]]:
        rows = [profile.to_dict() for _, profile in sorted(self._profiles.items())]
        for profile_id, reason in sorted(self._rejected.items()):
            rows.append({"profile_id": profile_id, "available": False, "reason": reason})
        return rows


def builtin_objective_profiles() -> tuple[ObjectiveProfile, ...]:
    """The one supported objective target, plus its declared limitations.

    The audit/harness windows this build captures print a tick counter and an
    input prompt, not an objective. There is deliberately **no** objective rule
    for them: a counter with no declared target is progress evidence, not a task,
    and inventing "reach 1000 ticks" would be exactly the fabrication this phase
    forbids. The mission profile below describes the fixture scenario used by the
    offline evaluation, whose labelled STATES are generated by the evaluation
    script (NF-17 consumes states, so no pixels are involved).
    """
    return (
        ObjectiveProfile(
            profile_id="nc-forge-mission@1",
            game="NC FORGE MISSION (fixture scenario)",
            version="1",
            provenance=(
                "declared for the NF-17 offline fixture scenario; its labelled states "
                "are generated by scripts/novaforge_nf17_objective_eval.py, not read "
                "from a game"
            ),
            rules=(
                ObjectiveRule(
                    rule_id="collect",
                    description="Collect the audit tokens",
                    category=ObjectiveCategory.MISSION,
                    objective_field="objective",
                    counter_field="progress",
                    target=3,
                    completion_texts=("mission complete", "objective complete"),
                    failure_texts=("mission failed", "objective failed"),
                ),
            ),
            limitations=(
                "The mission panel is a fixture layout, not a game's HUD: the rule "
                "demonstrates the mechanism, and no commercial game has a profile.",
                "Progress is read from one declared counter; other progress shapes "
                "(bars, checkpoints, stages) are not measured by this build.",
            ),
        ),
    )


def default_objective_registry() -> ObjectiveProfileRegistry:
    """A fresh registry per caller, so a test can mutate its own."""
    return ObjectiveProfileRegistry(builtin_objective_profiles())


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        figure = float(value)
    except (TypeError, ValueError):
        return None
    return figure if math.isfinite(figure) else None


def _evidence(
    kind: EvidenceKind,
    summary: str,
    state: GameState,
    *,
    field: str = "",
    text: str = "",
    value: float | None = None,
    box: Mapping[str, Any] | None = None,
) -> ObjectiveEvidenceRef:
    return ObjectiveEvidenceRef(
        kind=kind,
        summary=_clean(summary, MAX_SOURCE_TEXT),
        at=state.observation.at or state.timestamp,
        observation_id=state.observation.observation_id,
        session_id=state.observation.session_id,
        field=field,
        text=_clean(text, MAX_SOURCE_TEXT),
        value=value,
        box=box,
    )


def _scene_outcome(state: GameState) -> tuple[str, tuple[ObjectiveEvidenceRef, ...], tuple[ObjectiveEvidenceRef, ...]]:
    """The confirmed outcome screen, and the evidence it is one.

    Only the frame that CONFIRMED the transition carries it. A victory screen
    that stays up is not fresh evidence: an objective discovered afterwards must
    not be closed by a banner that was already there. The summary names the
    scene's own status and source, so an inferred cue never reads as a read
    field.
    """
    transition = getattr(state, "transition", None)
    scene = getattr(state, "scene", None)
    if transition is None or scene is None:
        return "", (), ()
    if str(getattr(transition, "state", "")) != "confirmed":
        return "", (), ()
    outcome = str(getattr(transition, "confirmed", ""))
    if outcome not in ("victory", "defeat"):
        return "", (), ()
    status = str(getattr(scene, "status", "unknown"))
    source = str(getattr(scene, "source", ""))
    notes = tuple(getattr(scene, "evidence", ()) or ())
    summary = (
        f"confirmed {outcome} screen after "
        f"{int(getattr(transition, 'required', 0) or 0)} consecutive observation(s), "
        f"read as {status}"
        + (f" ({source})" if source else "")
        + (f"; {notes[0]}" if notes else "")
    )
    ref = _evidence(EvidenceKind.SCENE, summary, state, field="scene", text=outcome)
    if outcome == "victory":
        return outcome, (ref,), ()
    return outcome, (), (ref,)


def _field_map(state: GameState) -> dict[str, HudReading]:
    return {row.element_id: row for row in state.hud}


def _text_hits(state: GameState, patterns: Sequence[str]) -> list[tuple[str, str]]:
    """Where a declared message was read: (field or "screen", matched text).

    Deduplicated case-insensitively: a HUD field's text also appears as a screen
    line in most scenes, and counting it twice would make one reading look like
    two independent confirmations.
    """
    lowered = [pattern.casefold() for pattern in patterns if pattern]
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for field, text in [
        *((row.element_id, row.text) for row in state.hud
          if row.status is HudStatus.RECOGNIZED and row.text),
        *(("screen", str(line.get("text", "") or "")) for line in state.text),
    ]:
        key = _norm(text)
        if not key or key in seen:
            continue
        if any(pattern in text.casefold() for pattern in lowered):
            seen.add(key)
            hits.append((field, text))
    return hits


def _message_hits(state: GameState, regex: re.Pattern[str]) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for row in state.hud:
        if row.status is HudStatus.RECOGNIZED and row.text and regex.search(row.text):
            hits.append((row.element_id, row.text))
    for line in state.text:
        text = str(line.get("text", "") or "")
        if text and regex.search(text):
            hits.append(("screen", text))
    return hits


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One candidate objective found in a state, before transitions are applied."""

    objective_id: str
    description: str
    source: str
    source_text: str
    category: ObjectiveCategory
    explicit: bool
    progress: ProgressRecord | None = None
    completion: tuple[ObjectiveEvidenceRef, ...] = ()
    failure: tuple[ObjectiveEvidenceRef, ...] = ()
    dependencies: tuple[str, ...] = ()
    completion_conditions: tuple[str, ...] = ()
    failure_conditions: tuple[str, ...] = ()
    unreadable: str = ""
    rule_id: str = ""
    counter_field: str = ""
    reasons: tuple[str, ...] = ()


def _profile_candidates(
    state: GameState, profile: ObjectiveProfile, reasons: list[str]
) -> list[_Candidate]:
    """Rules applied to this state's readings — never to pixels."""
    found: list[_Candidate] = []
    fields = _field_map(state)
    for rule in profile.rules:
        rule_reasons: list[str] = []
        objective_reading = fields.get(rule.objective_field) if rule.objective_field else None
        counter_reading = fields.get(rule.counter_field) if rule.counter_field else None
        if rule.objective_field and objective_reading is None:
            reasons.append(
                f"Rule {rule.rule_id!r} names field {rule.objective_field!r}, which this "
                "state does not declare; the rule was not applied."
            )
            continue
        if rule.counter_field and counter_reading is None:
            reasons.append(
                f"Rule {rule.rule_id!r} names counter {rule.counter_field!r}, which this "
                "state does not declare; progress is not claimed."
            )
        text = ""
        unreadable = ""
        if objective_reading is not None:
            if objective_reading.status is HudStatus.RECOGNIZED:
                text = _clean(objective_reading.text, MAX_SOURCE_TEXT)
            elif objective_reading.status is HudStatus.ABSENT:
                # The region is declared and held nothing: no objective is shown.
                reasons.append(
                    f"Rule {rule.rule_id!r}: nothing was read in the objective region "
                    f"{rule.objective_field!r}, so no objective is claimed from it."
                )
                continue
            else:
                unreadable = (
                    f"the objective region {rule.objective_field!r} could not be read "
                    f"({objective_reading.status.value}: {objective_reading.reason or 'no reason given'})"
                )
        completion: list[ObjectiveEvidenceRef] = []
        failure: list[ObjectiveEvidenceRef] = []
        for field_name, hit_text in _text_hits(state, rule.completion_texts):
            completion.append(
                _evidence(
                    EvidenceKind.COMPLETION_TEXT,
                    f"completion text {hit_text!r} was read in {field_name}",
                    state,
                    field=field_name,
                    text=hit_text,
                )
            )
        for field_name, hit_text in _text_hits(state, rule.failure_texts):
            failure.append(
                _evidence(
                    EvidenceKind.FAILURE_TEXT,
                    f"failure text {hit_text!r} was read in {field_name}",
                    state,
                    field=field_name,
                    text=hit_text,
                )
            )
        progress: ProgressRecord | None = None
        if counter_reading is not None:
            if counter_reading.status is HudStatus.RECOGNIZED and counter_reading.value is not None:
                progress = ProgressRecord(
                    kind=ProgressKind.NUMERIC,
                    text=counter_reading.text,
                    value=counter_reading.value,
                    target=rule.target,
                    observation_id=state.observation.observation_id,
                    at=state.observation.at or state.timestamp,
                    reason=(
                        "read from the declared counter"
                        if rule.target is not None
                        else "read from the declared counter; no completion target is declared"
                    ),
                )
                if rule.target is not None and counter_reading.value >= rule.target:
                    completion.append(
                        _evidence(
                            EvidenceKind.COUNTER_TARGET,
                            f"counter reached {counter_reading.value:g} of {rule.target:g}",
                            state,
                            field=rule.counter_field,
                            text=counter_reading.text,
                            value=counter_reading.value,
                            box=counter_reading.box.to_dict() if counter_reading.box is not None else None,
                        )
                    )
            elif counter_reading.status is HudStatus.ABSENT:
                rule_reasons.append(
                    f"the declared counter {rule.counter_field!r} held no text; no progress "
                    "is claimed from this frame"
                )
            else:
                # The reading's own reason travels with the objective: "could not
                # be read" without WHY is the least useful sentence a panel can show.
                rule_reasons.append(
                    f"the counter {rule.counter_field!r} could not be read "
                    f"({counter_reading.status.value}"
                    + (f": {counter_reading.reason}" if counter_reading.reason else "")
                    + "); no progress is claimed from this frame"
                )
        description = text or rule.description
        source_text = text
        objective_id = _rule_objective_id(
            profile.profile_id, rule.rule_id, text or rule.description
        )
        if unreadable:
            description = rule.description
            objective_id = _rule_objective_id(profile.profile_id, rule.rule_id, "unread")
        conditions = (
            (f"reach {rule.target:g} on {rule.counter_field}",) if rule.target is not None else ()
        ) + tuple(f"read {message!r}" for message in rule.completion_texts)
        failure_conditions = tuple(f"read {message!r}" for message in rule.failure_texts)
        found.append(
            _Candidate(
                objective_id=objective_id,
                description=description,
                source=profile.profile_id,
                source_text=source_text,
                category=rule.category,
                explicit=not rule.inferred and bool(text),
                progress=progress,
                completion=tuple(completion),
                failure=tuple(failure),
                dependencies=rule.dependencies,
                completion_conditions=conditions,
                failure_conditions=failure_conditions,
                unreadable=unreadable,
                rule_id=rule.rule_id,
                counter_field=rule.counter_field,
                reasons=tuple(rule_reasons),
            )
        )
    return found


def _generic_candidates(
    state: GameState, reasons: list[str]
) -> list[_Candidate]:
    """Objectives printed on screen, read from LINES that name themselves."""
    found: dict[str, _Candidate] = {}
    for line in state.text:
        text = _clean(line.get("text", ""), MAX_SOURCE_TEXT)
        if not text:
            continue
        match = _GENERIC_OBJECTIVE.match(text)
        if not match:
            continue
        description = _clean(match.group("text"), MAX_SOURCE_TEXT)
        if not description:
            continue
        keyword = match.group("keyword").casefold()
        objective_id = objective_id_for("generic", description)
        if objective_id in found:
            continue
        found[objective_id] = _Candidate(
            objective_id=objective_id,
            description=description,
            source="generic-text",
            source_text=text,
            category=_CATEGORY_FOR_KEYWORD.get(keyword, ObjectiveCategory.UNKNOWN),
            explicit=True,
            completion_conditions=(),
            failure_conditions=(),
        )
    if not found:
        reasons.append(
            "No line named itself as an objective (\"objective:\", \"mission:\", …), so "
            "nothing on screen was read as one."
        )
    return list(found.values())


# ---------------------------------------------------------------------------
# Interpretation
# ---------------------------------------------------------------------------


def _new_objective(
    candidate: _Candidate,
    state: GameState,
    *,
    status: ObjectiveStatus,
    at: str,
    evidence: tuple[ObjectiveEvidenceRef, ...],
    reason: str,
    progress: ProgressRecord | None,
) -> UnderstoodObjective:
    return UnderstoodObjective(
        objective_id=candidate.objective_id,
        description=candidate.description,
        session_id=state.observation.session_id,
        game=state.observation.game or state.game,
        source=candidate.source,
        source_text=candidate.source_text,
        category=candidate.category,
        status=status,
        explicit=candidate.explicit,
        progress=progress,
        # A declared prerequisite is carried from the FIRST reading too: dropping it
        # here meant a newly discovered objective was never blocked by it.
        dependencies=tuple(
            ObjectiveDependency(requires=item) for item in candidate.dependencies
        ),
        completion_conditions=candidate.completion_conditions,
        failure_conditions=candidate.failure_conditions,
        evidence=evidence[:MAX_EVIDENCE],
        first_seen=at,
        last_updated=at,
        reason=reason,
        revision=1,
    )


def _carried_progress(previous: UnderstoodObjective | None, candidate: _Candidate) -> ProgressRecord | None:
    """Progress with a stable start value, and a reset counted when it decreases."""
    fresh = candidate.progress
    if fresh is None:
        return None
    start = fresh.value
    resets = 0
    if previous is not None and previous.progress is not None:
        before = previous.progress
        start = before.start if before.start is not None else before.value
        resets = before.resets
        if (
            before.value is not None
            and fresh.value is not None
            and fresh.value < before.value
        ):
            resets += 1
    return ProgressRecord(
        kind=fresh.kind,
        text=fresh.text,
        value=fresh.value,
        target=fresh.target if fresh.target is not None else (
            previous.progress.target if previous is not None and previous.progress is not None else None
        ),
        start=start,
        fraction=progress_fraction(
            fresh.value,
            start,
            fresh.target if fresh.target is not None else (
                previous.progress.target
                if previous is not None and previous.progress is not None
                else None
            ),
        ),
        observation_id=fresh.observation_id,
        at=fresh.at,
        resets=resets,
        reason=fresh.reason,
    )


def _status_for(
    candidate: _Candidate,
    previous: UnderstoodObjective | None,
    *,
    conflicts: list[str],
    events: list[ObjectiveEvent],
    at: str,
    state: GameState,
    scene_completion: tuple[ObjectiveEvidenceRef, ...] = (),
    scene_failure: tuple[ObjectiveEvidenceRef, ...] = (),
) -> tuple[ObjectiveStatus, str, tuple[ObjectiveEvidenceRef, ...], ProgressRecord | None]:
    """One objective's next status, with the event that explains a change."""
    progress = _carried_progress(previous, candidate)
    evidence: list[ObjectiveEvidenceRef] = []
    if candidate.unreadable:
        evidence.append(
            _evidence(EvidenceKind.OBJECTIVE_TEXT, candidate.unreadable, state, field="")
        )
        return (
            ObjectiveStatus.UNKNOWN,
            candidate.unreadable,
            tuple(evidence),
            None,
        )
    if candidate.explicit and candidate.source_text:
        evidence.append(
            _evidence(
                EvidenceKind.OBJECTIVE_TEXT,
                f"objective text {candidate.source_text!r}",
                state,
                text=candidate.source_text,
            )
        )
    completion = list(candidate.completion) + list(scene_completion)
    failure = list(candidate.failure) + list(scene_failure)
    if completion and failure:
        conflicts.append(
            f"{candidate.objective_id}: completion and failure evidence were read in the "
            "same observation; no verdict is claimed"
        )
    if progress is not None:
        evidence.append(
            _evidence(
                EvidenceKind.PROGRESS,
                f"counter read as {progress.text or progress.value}",
                state,
                field=candidate.counter_field,
                text=progress.text,
                value=progress.value,
            )
        )
    evidence.extend(completion)
    evidence.extend(failure)

    if completion and not failure:
        before = previous.status.value if previous is not None else ""
        if previous is None or previous.status is not ObjectiveStatus.COMPLETED:
            events.append(
                ObjectiveEvent(
                    kind=ObjectiveEventKind.COMPLETED,
                    at=at,
                    session_id=state.observation.session_id,
                    objective_id=candidate.objective_id,
                    before=before,
                    after=ObjectiveStatus.COMPLETED.value,
                    reason=completion[0].summary,
                    evidence=tuple(completion),
                )
            )
        return (
            ObjectiveStatus.COMPLETED,
            completion[0].summary,
            tuple(evidence),
            progress,
        )
    if failure and not completion:
        before = previous.status.value if previous is not None else ""
        if previous is None or previous.status is not ObjectiveStatus.FAILED:
            events.append(
                ObjectiveEvent(
                    kind=ObjectiveEventKind.FAILED,
                    at=at,
                    session_id=state.observation.session_id,
                    objective_id=candidate.objective_id,
                    before=before,
                    after=ObjectiveStatus.FAILED.value,
                    reason=failure[0].summary,
                    evidence=tuple(failure),
                )
            )
        return ObjectiveStatus.FAILED, failure[0].summary, tuple(evidence), progress
    if completion and failure:
        return (
            previous.status if previous is not None else ObjectiveStatus.ACTIVE,
            "completion and failure evidence conflict; no verdict is claimed",
            tuple(evidence),
            progress,
        )

    if previous is None:
        status = ObjectiveStatus.IN_PROGRESS if progress is not None else ObjectiveStatus.DISCOVERED
        events.append(
            ObjectiveEvent(
                kind=ObjectiveEventKind.DISCOVERED,
                at=at,
                session_id=state.observation.session_id,
                objective_id=candidate.objective_id,
                before="",
                after=status.value,
                reason=(
                    f"read as {candidate.source_text!r}"
                    if candidate.source_text
                    else f"declared as {candidate.description!r} with no printed text"
                ),
                evidence=tuple(evidence[:MAX_EVIDENCE]),
            )
        )
        return status, "objective discovered", tuple(evidence), progress

    if previous.status in TERMINAL_STATUSES:
        # The objective is on screen again after a terminal verdict. That is a
        # contradiction, not a confirmation: the earlier verdict is corrected.
        events.append(
            ObjectiveEvent(
                kind=ObjectiveEventKind.CORRECTED,
                at=at,
                session_id=state.observation.session_id,
                objective_id=candidate.objective_id,
                before=previous.status.value,
                after=ObjectiveStatus.ACTIVE.value,
                reason=(
                    "the objective is displayed again after a "
                    f"{previous.status.value} verdict, so that verdict is corrected"
                ),
                evidence=tuple(evidence[:MAX_EVIDENCE]),
            )
        )
        return (
            ObjectiveStatus.ACTIVE,
            f"corrected from {previous.status.value}: the objective is displayed again",
            tuple(evidence),
            progress,
        )

    before_progress = previous.progress
    if progress is not None and progress.value is not None:
        before_value = before_progress.value if before_progress is not None else None
        if before_value is None or progress.value != before_value:
            reason = f"counter moved from {before_value:g} to {progress.value:g}" if (
                before_value is not None
            ) else f"counter read as {progress.value:g}"
            if before_value is not None and progress.value < before_value:
                reason = (
                    f"counter decreased from {before_value:g} to {progress.value:g}; "
                    "that is a reset or a misread, not progress"
                )
            events.append(
                ObjectiveEvent(
                    kind=ObjectiveEventKind.PROGRESS,
                    at=at,
                    session_id=state.observation.session_id,
                    objective_id=candidate.objective_id,
                    before="" if before_value is None else f"{before_value:g}",
                    after=f"{progress.value:g}",
                    reason=reason,
                    evidence=tuple(evidence[:MAX_EVIDENCE]),
                )
            )
            return ObjectiveStatus.IN_PROGRESS, reason, tuple(evidence), progress
        return (
            previous.status
            if previous.status is not ObjectiveStatus.DISCOVERED
            else ObjectiveStatus.ACTIVE,
            "the objective is displayed; the counter has not changed since the "
            "previous reading",
            tuple(evidence),
            progress,
        )

    if previous.status in (ObjectiveStatus.DISCOVERED, ObjectiveStatus.NOT_VERIFIABLE):
        events.append(
            ObjectiveEvent(
                kind=ObjectiveEventKind.ACTIVATED,
                at=at,
                session_id=state.observation.session_id,
                objective_id=candidate.objective_id,
                before=previous.status.value,
                after=ObjectiveStatus.ACTIVE.value,
                reason=(
                    "the objective is displayed again after being unverifiable"
                    if previous.status is ObjectiveStatus.NOT_VERIFIABLE
                    else "the objective was still displayed on a later observation"
                ),
                evidence=tuple(evidence[:MAX_EVIDENCE]),
            )
        )
        return (
            ObjectiveStatus.ACTIVE,
            "the objective is displayed; no progress evidence was read",
            tuple(evidence),
            progress,
        )
    return (
        previous.status,
        "the objective is displayed; no progress evidence was read",
        tuple(evidence),
        progress,
    )


def interpret(
    state: GameState,
    previous: ObjectiveState | None = None,
    *,
    registry: ObjectiveProfileRegistry | None = None,
    now: datetime | None = None,
) -> ObjectiveState:
    """Read one game state as objectives, transitions and evidence.

    Pure: the same ``(state, previous)`` always produces the same result, which
    is what lets the service, the tests and the evaluation share one reading.
    """
    registry = registry or default_objective_registry()
    at = (now or datetime.now(UTC)).isoformat()
    reasons: list[str] = []
    events: list[ObjectiveEvent] = []
    conflicts: list[str] = []
    unattributed: list[str] = []
    dependency_issues: list[str] = []

    profile_id = state.profile.profile.profile_id if state.profile.profile is not None else ""
    profile = registry.get(profile_id) if profile_id else None

    previously: dict[str, UnderstoodObjective] = {}
    if previous is not None and previous.session_id == state.observation.session_id:
        previously = {item.objective_id: item for item in previous.objectives}
    elif previous is not None:
        for item in previous.objectives:
            if item.status in TERMINAL_STATUSES or item.status is ObjectiveStatus.SUPERSEDED:
                continue
            events.append(
                ObjectiveEvent(
                    kind=ObjectiveEventKind.SUPERSEDED,
                    at=at,
                    session_id=previous.session_id,
                    objective_id=item.objective_id,
                    before=item.status.value,
                    after=ObjectiveStatus.SUPERSEDED.value,
                    reason=(
                        "the observation session changed, so the objective is no longer "
                        "current; its verdicts stay in the history"
                    ),
                    evidence=(),
                )
            )

    candidates: list[_Candidate] = []
    if profile is not None:
        candidates = _profile_candidates(state, profile, reasons)
        if not candidates and not reasons:
            reasons.append(
                f"Objective profile {profile.profile_id} declares no rule that could be "
                "applied to this state."
            )
    else:
        if profile_id:
            reasons.append(
                f"No objective rules are declared for perception profile {profile_id!r}; "
                "only text that names itself as an objective is read."
            )
        else:
            reasons.append(
                "No perception profile matched, so no game-specific objective rules exist "
                "for this target."
            )
        candidates = _generic_candidates(state, reasons)

    # NF-24: a CONFIRMED outcome screen is evidence about the run. It travels to
    # a displayed objective only when that objective is the ONLY candidate in the
    # frame, and to a single no-longer-displayed objective below — never to a
    # crowd of them: a run outcome does not say which objective it closes.
    scene_outcome, scene_completion, scene_failure = _scene_outcome(state)
    if scene_outcome and len(candidates) > 1:
        unattributed.append(
            f"a confirmed {scene_outcome} screen was read while {len(candidates)} "
            "objectives were displayed; none was closed by it — a run outcome does "
            "not say which objective it closes"
        )
        scene_completion, scene_failure = (), ()

    objectives: list[UnderstoodObjective] = []
    seen: set[str] = set()
    for candidate in candidates:
        seen.add(candidate.objective_id)
        prev = previously.get(candidate.objective_id)
        status, reason, evidence, progress = _status_for(
            candidate,
            prev,
            conflicts=conflicts,
            events=events,
            at=at,
            state=state,
            scene_completion=scene_completion,
            scene_failure=scene_failure,
        )
        if prev is None:
            objective = _new_objective(
                candidate,
                state,
                status=status,
                at=at,
                evidence=evidence,
                reason=reason,
                progress=progress,
            )
        else:
            objective = UnderstoodObjective(
                objective_id=prev.objective_id,
                description=candidate.description,
                session_id=state.observation.session_id,
                game=prev.game,
                source=prev.source,
                source_text=candidate.source_text or prev.source_text,
                category=candidate.category,
                status=status,
                explicit=candidate.explicit,
                progress=progress if progress is not None else prev.progress,
                completion_conditions=candidate.completion_conditions or prev.completion_conditions,
                failure_conditions=candidate.failure_conditions or prev.failure_conditions,
                dependencies=prev.dependencies,
                evidence=evidence[:MAX_EVIDENCE],
                first_seen=prev.first_seen,
                last_updated=at,
                reason=reason,
                revision=prev.revision + (1 if status is not prev.status else 0),
            )
        if candidate.reasons:
            # Whatever kept this field from being read belongs beside the status,
            # so "no progress" is never mistaken for "no change".
            objective = UnderstoodObjective(
                **{
                    **_as_kwargs(objective),
                    "reason": "; ".join([objective.reason, *candidate.reasons]),
                }
            )
        objectives.append(objective)

    # Objectives that were current and are no longer displayed. A profile slot
    # whose text changed supersedes its old objective; anything else that simply
    # vanished is NOT closed — disappearance is not completion. The one
    # exception is an OUTCOME SCREEN that was confirmed on this frame, and only
    # when exactly one objective is left unattributed: a banner closes the run's
    # single live objective, it does not close a list of them by guesswork.
    pending = [
        item
        for objective_id, item in previously.items()
        if objective_id not in seen
        and item.status not in TERMINAL_STATUSES
        and item.status is not ObjectiveStatus.SUPERSEDED
    ]
    if scene_outcome and len(pending) > 1:
        unattributed.append(
            f"a confirmed {scene_outcome} screen was read while {len(pending)} "
            "objectives were still current; none was completed by it — a run outcome "
            "does not say which objective it closes"
        )
    slot_for = {candidate.rule_id: candidate.objective_id for candidate in candidates}
    for objective_id, prev in previously.items():
        if objective_id in seen:
            continue
        if prev.status in TERMINAL_STATUSES or prev.status is ObjectiveStatus.SUPERSEDED:
            objectives.append(prev)
            continue
        slot = ""
        if profile is not None:
            for rule in profile.rules:
                if objective_id.startswith(f"{profile.profile_id}:{rule.rule_id}:"):
                    slot = rule.rule_id
                    break
        replacement = slot_for.get(slot) if slot else None
        if replacement and replacement != objective_id:
            objectives.append(
                UnderstoodObjective(
                    **{
                        **_as_kwargs(prev),
                        "status": ObjectiveStatus.SUPERSEDED,
                        "superseded_by": replacement,
                        "last_updated": at,
                        "reason": (
                            "the objective text in this slot changed, so this objective was "
                            "replaced"
                        ),
                        "revision": prev.revision + 1,
                    }
                )
            )
            events.append(
                ObjectiveEvent(
                    kind=ObjectiveEventKind.SUPERSEDED,
                    at=at,
                    session_id=state.observation.session_id,
                    objective_id=objective_id,
                    before=prev.status.value,
                    after=ObjectiveStatus.SUPERSEDED.value,
                    reason="a different objective was displayed in the same declared slot",
                    evidence=(),
                )
            )
            continue
        if scene_outcome and len(pending) == 1:
            # Attribution is unambiguous: one objective is left, and this frame
            # confirmed the run's outcome screen.
            verdict = (
                ObjectiveStatus.COMPLETED
                if scene_outcome == "victory"
                else ObjectiveStatus.FAILED
            )
            evidence = (
                scene_completion if scene_outcome == "victory" else scene_failure
            )
            objectives.append(
                UnderstoodObjective(
                    **{
                        **_as_kwargs(prev),
                        "status": verdict,
                        "last_updated": at,
                        "reason": (
                            evidence[0].summary
                            if evidence
                            else f"the run reached a confirmed {scene_outcome} screen"
                        ),
                        "evidence": (evidence[-1:] or tuple(prev.evidence))[:MAX_EVIDENCE],
                        "revision": prev.revision + 1,
                    }
                )
            )
            events.append(
                ObjectiveEvent(
                    kind=(
                        ObjectiveEventKind.COMPLETED
                        if verdict is ObjectiveStatus.COMPLETED
                        else ObjectiveEventKind.FAILED
                    ),
                    at=at,
                    session_id=state.observation.session_id,
                    objective_id=objective_id,
                    before=prev.status.value,
                    after=verdict.value,
                    reason=(
                        f"the only current objective, after a confirmed {scene_outcome} "
                        "screen; the objective text was no longer displayed"
                    ),
                    evidence=evidence,
                )
            )
            continue
        if prev.status is ObjectiveStatus.NOT_VERIFIABLE:
            objectives.append(prev)
            continue
        objectives.append(
            UnderstoodObjective(
                **{
                    **_as_kwargs(prev),
                    "status": ObjectiveStatus.NOT_VERIFIABLE,
                    "last_updated": at,
                    "reason": (
                        "the objective stopped being displayed; that is not completion, so "
                        "no verdict is claimed"
                    ),
                    "revision": prev.revision + 1,
                }
            )
        )
        events.append(
            ObjectiveEvent(
                kind=ObjectiveEventKind.UNVERIFIABLE,
                at=at,
                session_id=state.observation.session_id,
                objective_id=objective_id,
                before=prev.status.value,
                after=ObjectiveStatus.NOT_VERIFIABLE.value,
                reason="the objective is no longer displayed; disappearance is not completion",
                evidence=(),
            )
        )

    # A completion/failure banner with no attribution to one objective is
    # reported, never assigned to whichever objective happens to be first.
    current = [item for item in objectives if item.status not in (ObjectiveStatus.SUPERSEDED,)]
    if len(current) > 1 and not profile:
        banner = _message_hits(state, _COMPLETION_TEXT) + _message_hits(state, _FAILURE_TEXT)
        if banner:
            unattributed.append(
                f"{len(banner)} outcome message(s) were read while {len(current)} objectives "
                "are current; none was attributed"
            )

    # Declared dependencies: satisfied only by a COMPLETED objective in this
    # session. An unknown reference is a profile problem, reported once.
    by_rule: dict[str, UnderstoodObjective] = {}
    if profile is not None:
        for rule in profile.rules:
            for objective in objectives:
                if objective.objective_id.startswith(f"{profile.profile_id}:{rule.rule_id}:"):
                    by_rule[rule.rule_id] = objective
                    break
        for rule in profile.rules:
            for dependency in rule.dependencies:
                target = by_rule.get(dependency)
                if target is None:
                    dependency_issues.append(
                        f"rule {rule.rule_id!r} requires {dependency!r}, which produced no "
                        "objective in this reading"
                    )
    if by_rule:
        resolved: list[UnderstoodObjective] = []
        for objective in objectives:
            if not objective.dependencies:
                resolved.append(objective)
                continue
            blocked_reasons: list[str] = []
            dependencies: list[ObjectiveDependency] = []
            for requirement in objective.dependencies:
                target = by_rule.get(requirement.requires)
                satisfied = bool(target is not None and target.status is ObjectiveStatus.COMPLETED)
                dependencies.append(
                    ObjectiveDependency(
                        requires=requirement.requires,
                        source="declared",
                        satisfied=satisfied,
                        reason=(
                            "the required objective is completed"
                            if satisfied
                            else "the required objective is not completed in this session"
                        ),
                    )
                )
                if not satisfied:
                    blocked_reasons.append(requirement.requires)
            if blocked_reasons and objective.status not in TERMINAL_STATUSES:
                if objective.status is not ObjectiveStatus.BLOCKED:
                    events.append(
                        ObjectiveEvent(
                            kind=ObjectiveEventKind.BLOCKED,
                            at=at,
                            session_id=state.observation.session_id,
                            objective_id=objective.objective_id,
                            before=objective.status.value,
                            after=ObjectiveStatus.BLOCKED.value,
                            reason=(
                                "a declared prerequisite is not completed: "
                                + ", ".join(blocked_reasons)
                            ),
                            evidence=(),
                        )
                    )
                objective = UnderstoodObjective(
                    **{
                        **_as_kwargs(objective),
                        "status": ObjectiveStatus.BLOCKED,
                        "dependencies": tuple(dependencies),
                        "reason": "blocked by an incomplete declared prerequisite",
                        "revision": (
                            objective.revision + 1
                            if objective.status is not ObjectiveStatus.BLOCKED
                            else objective.revision
                        ),
                    }
                )
            else:
                if objective.status is ObjectiveStatus.BLOCKED:
                    # The prerequisite is satisfied now: the block is lifted, with
                    # the event naming the evidence that lifted it.
                    events.append(
                        ObjectiveEvent(
                            kind=ObjectiveEventKind.ACTIVATED,
                            at=at,
                            session_id=state.observation.session_id,
                            objective_id=objective.objective_id,
                            before=ObjectiveStatus.BLOCKED.value,
                            after=ObjectiveStatus.ACTIVE.value,
                            reason="the declared prerequisite is completed",
                            evidence=(),
                        )
                    )
                    objective = UnderstoodObjective(
                        **{
                            **_as_kwargs(objective),
                            "status": (
                                ObjectiveStatus.IN_PROGRESS
                                if objective.progress is not None
                                else ObjectiveStatus.ACTIVE
                            ),
                            "dependencies": tuple(dependencies),
                            "reason": "the declared prerequisite is completed",
                            "revision": objective.revision + 1,
                        }
                    )
                else:
                    objective = UnderstoodObjective(
                        **{**_as_kwargs(objective), "dependencies": tuple(dependencies)}
                    )
            resolved.append(objective)
        objectives = resolved

    if len(objectives) > MAX_OBJECTIVES:
        reasons.append(
            f"{len(objectives)} objectives were read; only the first {MAX_OBJECTIVES} are kept."
        )
        objectives = objectives[:MAX_OBJECTIVES]

    uncheckable = [item for item in objectives if item.status is not ObjectiveStatus.SUPERSEDED]
    if uncheckable and not any(item.checkable for item in uncheckable):
        reasons.append(
            "No current objective declares a completion condition, so completion cannot be "
            "verified from this target."
        )

    limitations = [
        "An objective is an interpretation of what the screen says, not a game's internal "
        "task state: nothing here reads the game's memory, files or quest log.",
        "Completion needs explicit evidence — a declared completion message or a declared "
        "counter reaching its target. A disappearing objective is never completion.",
        "Only declared dependencies are represented; this build does not infer a quest "
        "order from screen order.",
    ]
    if profile is not None:
        limitations.extend(profile.limitations)
    if not profile:
        limitations.append(
            "No objective rules exist for this target, so only lines that name themselves "
            "as objectives are read."
        )

    return ObjectiveState(
        session_id=state.observation.session_id,
        game=state.observation.game or state.game,
        observation_id=state.observation.observation_id,
        at=at,
        sequence=state.sequence,
        objectives=tuple(objectives),
        events=tuple(events),
        diagnostics=ObjectiveDiagnostics(
            conflicts=tuple(conflicts),
            dependency_issues=tuple(dependency_issues),
            unattributed=tuple(unattributed),
        ),
        reasons=tuple(reasons),
        limitations=tuple(limitations),
        profile_id=profile_id,
    )


def _as_kwargs(objective: UnderstoodObjective) -> dict[str, Any]:
    """Every field of an objective, for building a modified copy of a frozen record."""
    return {
        "objective_id": objective.objective_id,
        "description": objective.description,
        "session_id": objective.session_id,
        "game": objective.game,
        "source": objective.source,
        "source_text": objective.source_text,
        "category": objective.category,
        "status": objective.status,
        "explicit": objective.explicit,
        "progress": objective.progress,
        "completion_conditions": objective.completion_conditions,
        "failure_conditions": objective.failure_conditions,
        "dependencies": objective.dependencies,
        "evidence": objective.evidence,
        "first_seen": objective.first_seen,
        "last_updated": objective.last_updated,
        "reason": objective.reason,
        "superseded_by": objective.superseded_by,
        "revision": objective.revision,
        "confidence": objective.confidence,
    }
