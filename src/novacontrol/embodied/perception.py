"""Turning an observation into world-model facts — without inventing anything.

This is the seam the phase's hardest honesty rule lives on. An observation has
three kinds of content and they must NOT be flattened into one:

* **structured state** — the environment's own report. It becomes an OBSERVED
  fact with the environment as its source and the observation as its evidence.
* **entities with a label** — a detection that named something. It becomes an
  observed fact about that label, with a confidence attached.
* **entities without a label** — a region nobody can identify. It becomes an
  UNKNOWN fact about a region, and it never becomes ``enemy``, ``weapon``,
  ``player`` or ``item``. Manufacturing a name from a classless region is the
  single most tempting way for an embodied agent to lie, so the code refuses to
  do it: :func:`facts_from_observation` has no branch that could produce one.

The facts are produced in the ``key=value`` vocabulary Phase 24's grounding
service already reads, which is what lets a precondition be checked against a
real reading rather than against a plan's own assumption.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from novacontrol.embodied.models import (
    EntityRead,
    Observation,
    ObservationBasis,
)

#: The name prefix every fact from this package carries, so a reader can tell
#: an embodied environment fact from a machine fact at a glance.
FACT_PREFIX = "env."

#: Labels that would BE an unsupported semantic claim about a game. An
#: observation may carry them only if the provider actually produced them, and
#: this set is what the guard checks against.
SEMANTIC_LABEL_CLAIMS = frozenset(
    {
        "enemy",
        "player",
        "weapon",
        "item",
        "npc",
        "ally",
        "monster",
        "loot",
        "health",
        "score",
    }
)


@dataclass(frozen=True, slots=True)
class FactSet:
    """Facts for the world model, split by how much they can be trusted.

    ``observed`` may satisfy a precondition. ``uncertain`` may be REPORTED but
    never authorize an action — it is the bucket an unlabeled region lands in.
    """

    observed: Mapping[str, Any]
    uncertain: Mapping[str, Any]
    #: Labels the observation claimed, kept so a caller can see what was
    #: actually asserted rather than only what this layer accepted.
    claimed_labels: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    @property
    def all(self) -> dict[str, Any]:
        return {**dict(self.uncertain), **dict(self.observed)}

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed": dict(self.observed),
            "uncertain": dict(self.uncertain),
            "claimed_labels": list(self.claimed_labels),
            "limitations": list(self.limitations),
        }


def facts_from_observation(observation: Observation) -> FactSet:
    """Facts from one observation, with uncertainty preserved rather than lost.

    Only a ``STRUCTURED`` observation (or a labeled entity with real support)
    contributes ``observed`` facts. A visual or unknown reading contributes
    ``uncertain`` facts carrying its basis, so the planner can see that a
    region exists while being unable to conclude what it is.
    """
    observed: dict[str, Any] = {}
    uncertain: dict[str, Any] = {}
    labels: list[str] = []
    limitations: list[str] = []

    if observation.freshness.value != "fresh":
        limitations.append(
            f"the observation is {observation.freshness.value}, not fresh, so its facts "
            "describe the past and must not authorize an action"
        )

    structured = dict(observation.structured or {})
    if structured:
        for key, value in structured.items():
            name = str(key).strip()
            if not name:
                continue
            observed[f"{FACT_PREFIX}{name}"] = _plain(value)
    elif observation.basis is ObservationBasis.VISUAL:
        limitations.append(
            "this is a visual reading: regions are reported but no object identity "
            "is claimed, because nothing here can support one"
        )

    for entity in observation.entities:
        rows = _entity_facts(entity)
        # Only the environment's OWN state is a measurement. A region seen in a
        # frame, however confident, is a claim — so it lands in ``uncertain``
        # and can never satisfy a precondition by itself.
        bucket = (
            observed if entity.basis is ObservationBasis.STRUCTURED and entity.named else uncertain
        )
        for key, value in rows.items():
            bucket[key] = value
        if not entity.named:
            continue
        labels.append(entity.label)
        if (
            entity.label.strip().lower() in SEMANTIC_LABEL_CLAIMS
            and entity.basis is not ObservationBasis.STRUCTURED
        ):
            if entity.confidence is None:
                limitations.append(
                    f"{entity.label!r} is claimed by a non-structured reading with no "
                    "confidence at all, so it is kept as unconfirmed and cannot "
                    "authorize an action"
                )
            elif float(entity.confidence) < 0.5:
                limitations.append(
                    f"{entity.label!r} is claimed with confidence "
                    f"{float(entity.confidence):.2f}, below the 0.50 bar for a "
                    "semantic claim, so it stays unconfirmed"
                )

    if observation.objective_progress:
        for key, value in dict(observation.objective_progress).items():
            observed[f"{FACT_PREFIX}progress.{str(key).strip()}"] = _plain(value)

    return FactSet(
        observed=observed,
        uncertain=uncertain,
        claimed_labels=tuple(dict.fromkeys(labels)),
        limitations=tuple(limitations),
    )


def to_world_observation(
    observation: Observation,
    *,
    session_id: str = "",
    scope: str = "",
) -> dict[str, Any]:
    """The Phase 22 ingest body for one embodied observation.

    Entities are forwarded with their basis and confidence intact, so the world
    model records a region as a region. Facts carry the environment as their
    source and the observation id as their evidence — provenance the world
    model already knows how to keep.
    """
    entities: list[dict[str, Any]] = []
    for entity in observation.entities:
        entities.append(
            {
                "entity_id": entity.entity_id,
                "label": entity.label or f"region:{entity.entity_id}",
                "entity_type": entity.kind or "region",
                "bbox": list(entity.bbox) if entity.bbox is not None else None,
                "attributes": [
                    {
                        "name": _prefixed(f"{entity.label or entity.entity_id}.{key}"),
                        "value": _plain(value),
                        "basis": "observed"
                        if entity.basis is ObservationBasis.STRUCTURED
                        else "unknown",
                        "confidence": entity.confidence,
                        "source": observation.provenance or observation.environment_id,
                    }
                    for key, value in dict(entity.attributes).items()
                ],
            }
        )
    facts = [
        {
            "name": _prefixed(name),
            "value": value,
            "basis": "observed" if observation.basis is ObservationBasis.STRUCTURED else "unknown",
            "confidence": observation.confidence,
            "source": observation.provenance or observation.environment_id,
            "observed_at": observation.taken_at,
            "evidence": list(observation.evidence),
        }
        for name, value in _structured_rows(observation)
    ]
    return {
        "source": "external",
        "source_id": session_id or observation.session_id or observation.environment_id,
        "scope": scope or observation.environment_id,
        "timestamp": observation.taken_at,
        "complete": bool(observation.structured),
        "entities": entities,
        "facts": facts,
        "confidence": observation.confidence,
        "correlation_id": session_id or observation.session_id,
        "metadata": {
            "provenance": observation.provenance,
            "observation_id": observation.observation_id,
        },
    }


def same_value(observed: Any, expected_text: str) -> bool:
    """Compare an observed value with a ``key=value`` expectation, tolerantly.

        The same rule Phase 24's grounding service uses for a precondition: the
        literal comparison first, then the typed reading of the text. ``true``
        matches ``True`` because that is how the condition was written. One owner
        for the comparison, so a plan precondition and an action effect cannot
    disagree about whether a fact holds.
    """
    if observed is None:
        return False
    if isinstance(observed, bool):
        return observed == (expected_text.strip().lower() in {"true", "1", "yes", "on"})
    text = expected_text.strip()
    if isinstance(observed, (int, float)) and not isinstance(observed, bool):
        try:
            return float(observed) == float(text)
        except (TypeError, ValueError):
            pass
    if str(observed) == text:
        return True
    return str(observed).strip().lower() == text.lower()


def effect_observed(
    expected: Sequence[str], observation: Observation | None
) -> tuple[bool | None, str]:
    """Whether a fresh observation shows the effect an action declared.

    ``True`` means every declared fact was seen holding. ``False`` means a fact
    the reading COULD have reported was seen holding something else — the check
    was made and failed. ``None`` means the check could not be made at all (no
    declared effect, no observation, or a reading that cannot report the fact),
    and that is never rounded up to a success: an unverifiable action is an
    unverified action.
    """
    declared = tuple(item for item in (expected or ()) if "=" in str(item))
    if not declared:
        return None, "the action declared no observable effect"
    if observation is None:
        return None, "the environment returned no observation to check against"
    fact_set = facts_from_observation(observation)
    reported = dict(fact_set.observed)
    # The environment reports both its own state and the facts derived from it;
    # accept either spelling of a key, which is what keeps a declared
    # ``env.agent.x`` and a reported ``agent.x`` the same fact.
    for key, value in dict(fact_set.observed).items():
        text = str(key)
        if text.startswith(FACT_PREFIX):
            reported.setdefault(text[len(FACT_PREFIX) :], value)
        else:
            reported.setdefault(f"{FACT_PREFIX}{text}", value)
    can_report = observation.basis is ObservationBasis.STRUCTURED and bool(observation.structured)
    verified: list[str] = []
    wrong: list[str] = []
    unknown: list[str] = []
    for fact in declared:
        key, _, value = str(fact).partition("=")
        name = key.strip()
        seen = reported.get(name, reported.get(f"{FACT_PREFIX}{name}", _MISSING))
        if seen is _MISSING:
            unknown.append(f"{name} was not reported")
            continue
        if same_value(seen, value):
            verified.append(f"{name}={seen}")
        else:
            wrong.append(f"{name} is {seen!r}, expected {value.strip()!r}")
    if wrong:
        return False, "; ".join(wrong[:3])
    if unknown:
        if can_report:
            return False, "; ".join(unknown[:3])
        return None, "; ".join(unknown[:3])
    return True, "observed " + ", ".join(verified[:3])


def _prefixed(name: str) -> str:
    """One fact name, carrying the prefix the whole pipeline uses.

    The prefix is what makes an embodied fact's key the SAME string in the world
    model, in a planning precondition and in an action's declared effect. Without
    it the same fact would be ``agent.x`` in one place and ``env.agent.x`` in
    another, and the fresh-state gate would refuse every step it could not match
    — which is exactly the kind of silent mismatch a shared vocabulary prevents.
    """
    text = str(name).strip()
    if not text:
        return text
    return text if text.startswith(FACT_PREFIX) else f"{FACT_PREFIX}{text}"


def _structured_rows(observation: Observation) -> list[tuple[str, Any]]:
    return [
        (str(key), _plain(value))
        for key, value in dict(observation.structured or {}).items()
        if str(key).strip()
    ]


def _entity_facts(entity: EntityRead) -> dict[str, Any]:
    """Facts about one entity — as a REGION when it has no label.

    A missing label produces a region fact keyed by the entity id. There is no
    branch here that could name it, which is the guardrail made structural
    instead of documented.
    """
    key = entity.label.strip().lower() if entity.named else f"region.{entity.entity_id}"
    rows: dict[str, Any] = {}
    if entity.position is not None:
        rows[f"{FACT_PREFIX}{key}.x"] = _plain(entity.position[0])
        rows[f"{FACT_PREFIX}{key}.y"] = _plain(entity.position[1])
        rows[f"{FACT_PREFIX}{key}.present"] = True
    if entity.named and entity.confidence is not None:
        rows[f"{FACT_PREFIX}{key}.confidence"] = _plain(entity.confidence)
    for name, value in dict(entity.attributes).items():
        rows[f"{FACT_PREFIX}{key}.{str(name)}"] = _plain(value)
    return rows


def _plain(value: Any) -> Any:
    """JSON-safe and comparable — a tuple is a list, a set is sorted."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


_MISSING = object()


__all__ = [
    "FACT_PREFIX",
    "SEMANTIC_LABEL_CLAIMS",
    "FactSet",
    "effect_observed",
    "facts_from_observation",
    "same_value",
    "to_world_observation",
]
