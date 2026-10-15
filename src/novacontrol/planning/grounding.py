"""State grounding — what the planner is allowed to believe about now.

A plan built from the user's sentence alone plans a world that may no longer
exist. This module is the seam where planning meets :mod:`novacontrol.world`
(Phase 22): it reads the world model's current state and answers, per key,
WHAT KIND of belief it is — observed, inferred, predicted, unknown — plus how
old it is and whether two sources disagree. The vocabulary is Phase 22's own
(``FactBasis``), deliberately: a planning layer with its own word for
"observed" is how two layers start disagreeing about the same fact.

Three rules:

**Stale is not false, and not true either.** A fact older than the staleness
ceiling stays recorded with its basis and its age, but it CANNOT satisfy a
precondition: ``holds`` answers ``None`` for it, which every caller must read
as "go and look, or ask". That is the difference between a planner that
proceeds on a guess and one that stops at the edge of its knowledge.

**A caller's assertion is an assertion.** What the user said the state is
enters with source ``caller_assertion`` and basis ``UNKNOWN`` until something
observed it — the request is evidence about what the user believes, not a
reading of the machine.

**Conflicts are kept, not resolved.** When two sources report different
values, the fact is marked ``conflicting`` and both values are listed. A
conflicting fact cannot satisfy a precondition either, and the planner is
expected to want an observation rather than pick a side.

Nothing here observes the machine itself: an observation is an ACTION with a
cost, and taking one is the planner's decision (an ``observe`` task), not a
side effect of asking what is known.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from novacontrol.planning.goals import coerce_value
from novacontrol.world.models import FactBasis, WorldState

__all__ = [
    "FactBasis",
    "GroundedFact",
    "StateGrounding",
    "StateGroundingService",
]

#: How long a fact may be believed for planning before it is stale. Five
#: minutes is a default, not a truth: it is configured on the service and
#: stated in every grounding's ``as_of``/``stale_after_seconds`` pair.
DEFAULT_STALE_AFTER_SECONDS = 300.0


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_timestamp(text: str) -> datetime | None:
    """An ISO timestamp this build can reason about, or ``None``."""
    if not text or not isinstance(text, str):
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class GroundedFact:
    """One key's state, with what kind of belief it is and how old it is."""

    key: str
    value: Any = None
    basis: FactBasis = FactBasis.UNKNOWN
    #: When the belief was formed; empty when nothing recorded a time.
    observed_at: str = ""
    #: Seconds since then; ``None`` when no time was recorded, which is not
    #: the same as "just now".
    age_seconds: float | None = None
    stale: bool = False
    conflicting: bool = False
    alternatives: tuple[Any, ...] = ()
    #: Evidence references behind the belief (world evidence ids), never a
    #: narrative: a fact is traceable to records, not to a story.
    evidence: tuple[str, ...] = ()
    source: str = ""
    confidence: float | None = None

    @property
    def unknown(self) -> bool:
        return self.basis is FactBasis.UNKNOWN and not self.conflicting

    @property
    def usable(self) -> bool:
        """Whether this fact may carry a planning decision."""
        return (
            not self.stale
            and not self.conflicting
            and self.basis in {FactBasis.OBSERVED, FactBasis.INFERRED}
            and self.value is not None
        )

    def holds(self, expected: Any = None) -> bool | None:
        """Whether this fact supports ``expected`` — ``None`` when it cannot say.

        Comparison is tolerant of a precondition written as TEXT, because that is
        how preconditions are written: ``tree.clean=True`` names the boolean a
        source reports, not the four-character string. The literal comparison is
        tried first, so a fact whose value really IS the string still matches, and
        the typed reading (:func:`~novacontrol.planning.goals.coerce_value`) is the
        fallback. Only when both disagree is the answer ``False`` — a precondition
        whose text cannot be read as a value is never a silent yes.
        """
        if not self.usable:
            return None
        if expected is None:
            return True
        if self.value == expected:
            return True
        if isinstance(expected, str):
            return bool(self.value == coerce_value(expected))
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "basis": self.basis.value,
            "observed_at": self.observed_at,
            "age_seconds": None if self.age_seconds is None else round(self.age_seconds, 3),
            "stale": self.stale,
            "conflicting": self.conflicting,
            "alternatives": list(self.alternatives),
            "evidence": list(self.evidence),
            "source": self.source,
            "confidence": self.confidence,
            "usable": self.usable,
        }


@dataclass(frozen=True, slots=True)
class StateGrounding:
    """What the planner may assume about now, and on whose authority."""

    facts: tuple[GroundedFact, ...] = ()
    as_of: str = ""
    world_version: int | None = None
    world_id: str = ""
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS
    #: True when the world model was not wired at all — every fact is then an
    #: assertion or an unknown, and the report says so rather than implying a
    #: state was read.
    world_available: bool = False
    limitations: tuple[str, ...] = ()

    def fact(self, key: str) -> GroundedFact | None:
        for item in self.facts:
            if item.key == key:
                return item
        return None

    def holds(self, key: str, expected: Any = None) -> bool | None:
        """Whether a precondition holds — ``None`` means "cannot tell"."""
        found = self.fact(key)
        if found is None:
            return None
        return found.holds(expected)

    def unknown_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.facts if item.unknown)

    def stale_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.facts if item.stale)

    def conflicting_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.facts if item.conflicting)

    def critical_unknowns(self, keys: Sequence[str]) -> tuple[GroundedFact, ...]:
        """The keys that cannot carry a decision, in the order they were asked.

        A key with no fact at all is included as an UNKNOWN fact: "nobody ever
        asked" is a state of knowledge, and hiding it is how a precondition
        check passes on silence.
        """
        out: list[GroundedFact] = []
        for key in keys:
            found = self.fact(key)
            if found is None:
                out.append(GroundedFact(key=key, basis=FactBasis.UNKNOWN))
            elif not found.usable:
                out.append(found)
        return tuple(out)

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "world_id": self.world_id,
            "world_version": self.world_version,
            "world_available": self.world_available,
            "stale_after_seconds": self.stale_after_seconds,
            "facts": [item.to_dict() for item in self.facts],
            "unknown_keys": list(self.unknown_keys()),
            "stale_keys": list(self.stale_keys()),
            "conflicting_keys": list(self.conflicting_keys()),
            "limitations": list(self.limitations),
        }


class StateGroundingService:
    """Reads the world model into planner-readable facts.

    ``world`` may be a :class:`~novacontrol.world.engine.WorldModelEngine`
    (anything with ``state()``), a plain mapping of facts (the benchmark and
    tests inject one), or ``None`` — and ``None`` is an honest answer, not a
    default world: every requested key comes back UNKNOWN with the reason that
    no world model is wired.
    """

    def __init__(
        self,
        *,
        world: Any = None,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._world = world
        self.stale_after_seconds = max(0.0, float(stale_after_seconds))
        self._now = now or _now

    def ground(
        self,
        keys: Sequence[str] = (),
        *,
        asserted: Mapping[str, Any] | None = None,
    ) -> StateGrounding:
        """Ground ``keys`` against the world, merging caller assertions."""
        wanted = tuple(dict.fromkeys(str(key).strip() for key in keys if str(key).strip()))
        collected: dict[str, list[GroundedFact]] = {}
        world_id = ""
        world_version: int | None = None
        limitations: list[str] = []
        available = self._world is not None

        if available:
            rows, world_id, world_version = self._read_world()
            for row in rows:
                collected.setdefault(row.key, []).append(row)
        else:
            limitations.append(
                "no world model is wired, so nothing could be observed; every fact "
                "below is an assertion or an unknown"
            )

        for key, value in dict(asserted or {}).items():
            name = str(key).strip()
            if not name:
                continue
            collected.setdefault(name, []).append(
                GroundedFact(
                    key=name,
                    value=value,
                    # An assertion is not an observation: the caller saying so is
                    # evidence about the request, not a reading of the machine.
                    basis=FactBasis.UNKNOWN,
                    source="caller_assertion",
                )
            )

        now = self._now()
        facts: list[GroundedFact] = []
        for key in wanted:
            rows = collected.get(key, [])
            facts.append(self._merge(key, rows, now))
        return StateGrounding(
            facts=tuple(facts),
            as_of=now.isoformat(),
            world_version=world_version,
            world_id=world_id,
            stale_after_seconds=self.stale_after_seconds,
            world_available=available,
            limitations=tuple(limitations),
        )

    # -- internals -------------------------------------------------------------

    def _read_world(self) -> tuple[list[GroundedFact], str, int | None]:
        """Every fact the world model currently holds, as grounded rows.

        Two shapes are read: a world MODEL (``state()`` returning a
        ``WorldState``), and a plain mapping — the benchmark and the tests
        inject the latter so a deterministic scenario has deterministic state
        with deterministic timestamps.
        """
        if isinstance(self._world, Mapping):
            return self._read_mapping_world(), "injected", None
        state = self._state()
        if state is None:
            return [], "", None
        rows: list[GroundedFact] = []
        stamp = getattr(state, "timestamp", "")
        for entity in getattr(state, "entities", ()) or ():
            label = _entity_label(entity)
            for attribute in getattr(entity, "attributes", ()) or ():
                rows.append(
                    GroundedFact(
                        key=str(attribute.name),
                        value=attribute.value,
                        basis=_basis(attribute.basis),
                        observed_at=str(attribute.observed_at or stamp or ""),
                        evidence=tuple(
                            str(item) for item in getattr(attribute, "evidence", ()) or ()
                        ),
                        source=str(attribute.source or label),
                        confidence=getattr(attribute, "confidence", None),
                    )
                )
            rows.append(
                GroundedFact(
                    key=f"entity:{label}",
                    value=getattr(entity, "status", "present"),
                    basis=FactBasis.OBSERVED,
                    observed_at=str(getattr(entity, "last_confirmed", "") or stamp or ""),
                    evidence=tuple(str(item) for item in getattr(entity, "evidence", ()) or ()),
                    source=label,
                )
            )
        for condition in getattr(state, "conditions", ()) or ():
            rows.append(
                GroundedFact(
                    key=str(condition.name),
                    value=condition.value,
                    basis=_basis(condition.basis),
                    observed_at=str(condition.observed_at or stamp or ""),
                    evidence=tuple(str(item) for item in getattr(condition, "evidence", ()) or ()),
                    source=str(condition.source or "world"),
                    confidence=getattr(condition, "confidence", None),
                )
            )
        return rows, str(getattr(state, "world_id", "") or ""), getattr(state, "version", None)

    def _read_mapping_world(self) -> list[GroundedFact]:
        """Facts from a plain mapping. A value may be bare, or a
        ``(value, observed_at, basis)`` triple so a scenario can age a fact on
        purpose and watch the planner refuse to lean on it."""
        rows: list[GroundedFact] = []
        for key, raw in dict(self._world).items():
            name = str(key).strip()
            if not name:
                continue
            value, observed_at, basis_text = raw, "", "observed"
            if isinstance(raw, tuple):
                if len(raw) >= 1:
                    value = raw[0]
                if len(raw) >= 2:
                    observed_at = str(raw[1] or "")
                if len(raw) >= 3:
                    basis_text = str(raw[2] or "observed")
            rows.append(
                GroundedFact(
                    key=name,
                    value=value,
                    basis=_basis(basis_text),
                    observed_at=observed_at,
                    source="injected_state",
                )
            )
        return rows

    def _state(self) -> WorldState | None:
        getter = getattr(self._world, "state", None)
        if not callable(getter):
            return None
        try:
            state = getter()
        except Exception:  # noqa: BLE001 - a world that cannot answer is unknown
            return None
        return state if isinstance(state, WorldState) else None

    def _merge(self, key: str, rows: Sequence[GroundedFact], now: datetime) -> GroundedFact:
        """One key's rows as ONE fact — conflicts and all."""
        if not rows:
            return GroundedFact(key=key, basis=FactBasis.UNKNOWN)
        freshest = max(rows, key=lambda item: _parse_timestamp(item.observed_at) or _now_epoch())
        seen: list[Any] = []
        for row in rows:
            if row.value not in seen:
                seen.append(row.value)
        conflicting = len(seen) > 1
        age = _age_seconds(freshest.observed_at, now)
        stale = age is not None and age > self.stale_after_seconds
        evidence = tuple(dict.fromkeys(item for row in rows for item in row.evidence))
        sources = tuple(dict.fromkeys(row.source for row in rows if row.source))
        return GroundedFact(
            key=key,
            value=freshest.value,
            basis=freshest.basis,
            observed_at=freshest.observed_at,
            age_seconds=age,
            stale=stale,
            conflicting=conflicting,
            alternatives=tuple(value for value in seen if value != freshest.value),
            evidence=evidence,
            source=", ".join(sources),
            confidence=freshest.confidence,
        )


def _entity_label(entity: Any) -> str:
    labels = getattr(entity, "labels", ()) or ()
    if labels:
        return str(labels[0])
    return str(getattr(entity, "entity_id", "entity"))


def _basis(value: Any) -> FactBasis:
    if isinstance(value, FactBasis):
        return value
    try:
        return FactBasis(str(value))
    except ValueError:
        return FactBasis.UNKNOWN


def _age_seconds(stamp: str, now: datetime) -> float | None:
    parsed = _parse_timestamp(stamp)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds())


def _now_epoch() -> datetime:
    return datetime.fromtimestamp(0, tz=UTC)
