"""NF-18 — game-specific knowledge: typed records, evidence and transitions.

The memory subsystem already stores namespace/key/value rows; this module gives
game knowledge its TYPE, so a verified control mapping can never be mistaken for
a transient observation:

* **Every claim carries its evidence.** A record holds bounded
  :class:`KnowledgeEvidence` references naming the observation, objective,
  session and verification method it came from. Repeated copies of the same
  observation are one evidence entry, not two.
* **Verification is a state machine, not a score.** ``unverified → provisional →
  verified`` is explicit, and a verified record can only leave that state through
  ``contradicted`` or ``stale``. Nothing here invents a calibrated probability.
* **Contradictions are preserved.** Different content in the same logical slot is
  a SECOND record that marks the first ``contradicted``; nothing is overwritten.
* **Freshness is explicit.** A confirmed game/configuration version mismatch
  marks version-bound records stale; age alone never invalidates anything, and a
  record can be revalidated on its own.
* **Session state is separate.** :class:`SessionMemory` is bounded, transient and
  has no persistence path; only candidates accepted by the knowledge service are
  durable.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Iterable, Mapping

from .objectives import ObjectiveState, ObjectiveStatus
from .state import GameState, HudStatus

__all__ = [
    "ALLOWED_TRANSITIONS",
    "KNOWN_CATEGORIES",
    "KNOWN_STATUSES",
    "KnowledgeCandidate",
    "KnowledgeCategory",
    "KnowledgeEvidence",
    "KnowledgeRecord",
    "KnowledgeStatus",
    "MAX_EVIDENCE",
    "MAX_HISTORY",
    "MAX_RECORDS_PER_GAME",
    "MIN_VERIFIED_EVIDENCE",
    "RevalidationPolicy",
    "SCHEMA_VERSION",
    "SessionMemory",
    "VerificationMethod",
    "EvidenceSource",
    "can_transition",
    "candidate_from_payload",
    "control_candidate",
    "evidence_for",
    "game_slug",
    "knowledge_id_for",
    "objective_candidates",
    "state_candidates",
    "transition",
    "utc_stamp",
]

#: The schema tag every knowledge record carries.
SCHEMA_VERSION = "nf18.1"

#: Bounded by construction: evidence per record, records per game, text lengths.
MAX_EVIDENCE = 8
MAX_RECORDS_PER_GAME = 200
MAX_TEXT = 300
MAX_CONTENT_ITEMS = 16
#: Bounded per-record status history: who/why a claim changed, kept small.
MAX_HISTORY = 12
#: The explicit promotion rule for ``verified``: independent observations, and a
#: verified-EFFECT experiment for a control mapping.
MIN_VERIFIED_EVIDENCE = 2


class KnowledgeCategory(StrEnum):
    """What a record is ABOUT, so retrieval can filter without free text."""

    CONTROL = "control"
    HUD = "hud"
    MECHANIC = "mechanic"
    OBJECTIVE_PATTERN = "objective_pattern"
    INTERACTION_RULE = "interaction_rule"
    COMPATIBILITY = "compatibility"
    SITUATION = "situation"
    OUTCOME = "outcome"
    QUESTION = "question"


class KnowledgeStatus(StrEnum):
    """How strongly the evidence supports the record — never a probability."""

    UNVERIFIED = "unverified"
    PROVISIONAL = "provisional"
    VERIFIED = "verified"
    CONTRADICTED = "contradicted"
    STALE = "stale"


KNOWN_CATEGORIES = tuple(item.value for item in KnowledgeCategory)
KNOWN_STATUSES = tuple(item.value for item in KnowledgeStatus)


class EvidenceSource(StrEnum):
    """Where an evidence reference came from."""

    OBSERVATION = "observation"
    STATE = "state"
    OBJECTIVE = "objective"
    EXPERIMENT = "experiment"
    PROFILE = "profile"
    CONFIGURATION = "configuration"
    SESSION = "session"
    MANUAL = "manual"


class VerificationMethod(StrEnum):
    """What established the claim: what was seen, what was inferred, what ran."""

    OBSERVED = "observed"
    INFERRED = "inferred"
    EXPERIMENT = "experiment"
    DECLARED = "declared"
    NONE = "none"


class RevalidationPolicy(StrEnum):
    """When a record should be looked at again — never an automatic verdict."""

    MANUAL = "manual"
    VERSION_BOUND = "version_bound"
    OBSERVATION_COUNT = "observation_count"


#: The ONLY permitted moves. Anything else raises: a silent overwrite is how a
#: verified fact quietly becomes an assumption.
ALLOWED_TRANSITIONS: dict[KnowledgeStatus, frozenset[KnowledgeStatus]] = {
    KnowledgeStatus.UNVERIFIED: frozenset(
        {KnowledgeStatus.PROVISIONAL, KnowledgeStatus.CONTRADICTED, KnowledgeStatus.STALE}
    ),
    KnowledgeStatus.PROVISIONAL: frozenset(
        {KnowledgeStatus.VERIFIED, KnowledgeStatus.CONTRADICTED, KnowledgeStatus.STALE}
    ),
    KnowledgeStatus.VERIFIED: frozenset(
        {KnowledgeStatus.CONTRADICTED, KnowledgeStatus.STALE}
    ),
    KnowledgeStatus.CONTRADICTED: frozenset(
        {KnowledgeStatus.PROVISIONAL, KnowledgeStatus.STALE}
    ),
    KnowledgeStatus.STALE: frozenset(
        {KnowledgeStatus.PROVISIONAL, KnowledgeStatus.VERIFIED, KnowledgeStatus.CONTRADICTED}
    ),
}


def can_transition(before: KnowledgeStatus, after: KnowledgeStatus) -> bool:
    return before is after or after in ALLOWED_TRANSITIONS.get(before, frozenset())


def utc_stamp() -> str:
    return datetime.now(UTC).isoformat()


def _clean(value: Any, limit: int = MAX_TEXT) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _content_digest(content: Mapping[str, Any]) -> str:
    try:
        canonical = json.dumps(dict(content), sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - content is validated upstream
        canonical = str(sorted((str(key), str(value)) for key, value in content.items()))
    return _digest(canonical)


def _game_slug(game: str) -> str:
    """A stable namespace suffix: one bucket per game, and no path characters."""
    return _digest(_clean(game, 120).casefold())


def _validate_content(content: Mapping[str, Any]) -> None:
    """Keep a structured claim small and JSON-shaped.

    Bounded storage is not only a row cap: an API caller could otherwise store a
    megabyte in one field. This refuses before anything is hashed or written.
    """
    if not isinstance(content, Mapping) or not content:
        raise ValueError("A knowledge record needs structured content.")
    if len(content) > MAX_CONTENT_ITEMS:
        raise ValueError(f"Content may hold at most {MAX_CONTENT_ITEMS} fields.")

    def walk(value: Any, depth: int) -> None:
        if depth > 3:
            raise ValueError("Content may nest at most three levels.")
        if isinstance(value, str):
            if len(value) > MAX_TEXT:
                raise ValueError(f"A content string must be at most {MAX_TEXT} characters.")
            return
        if isinstance(value, Mapping):
            if len(value) > MAX_CONTENT_ITEMS:
                raise ValueError(f"A nested object may hold at most {MAX_CONTENT_ITEMS} fields.")
            for key, item in value.items():
                if len(str(key)) > 60:
                    raise ValueError("A content key must be at most 60 characters.")
                walk(item, depth + 1)
            return
        if isinstance(value, (list, tuple)):
            if len(value) > MAX_CONTENT_ITEMS:
                raise ValueError(f"A content list may hold at most {MAX_CONTENT_ITEMS} items.")
            for item in value:
                walk(item, depth + 1)
            return
        if value is None or isinstance(value, (int, float, bool)):
            return
        raise ValueError("Content values must be strings, numbers, booleans, null, lists or objects.")

    for key, value in content.items():
        if len(str(key)) > 60:
            raise ValueError("A content key must be at most 60 characters.")
        walk(value, 1)


def game_slug(game: str) -> str:
    """The public name of a game's storage bucket (one per game, no path chars)."""
    return _game_slug(game)


def knowledge_id_for(
    game: str, category: KnowledgeCategory, key: str, content: Mapping[str, Any], version: str = ""
) -> str:
    """The record's identity: same game/config/claim → same record, new evidence.

    A DIFFERENT claim in the same (game, category, key) slot gets a different id,
    which is what lets a contradiction be preserved instead of overwritten.
    """
    payload = "|".join(
        [_clean(game, 120).casefold(), category.value, _clean(key, 120), _clean(version, 60)]
        + [_content_digest(content)]
    )
    return f"k-{_digest(payload)}"


@dataclass(frozen=True, slots=True)
class KnowledgeEvidence:
    """One traceable fact a record rests on."""

    source: EvidenceSource
    summary: str
    method: VerificationMethod = VerificationMethod.OBSERVED
    #: True only when the expected EFFECT was observed (a control mapping with a
    #: sent input and no effect is NOT verified).
    verified: bool = False
    at: str = ""
    game: str = ""
    game_version: str = ""
    session_id: str = ""
    observation_id: str = ""
    objective_id: str = ""
    detail: str = ""
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.source, EvidenceSource):
            raise ValueError(f"Unknown evidence source: {self.source!r}")
        if not isinstance(self.method, VerificationMethod):
            raise ValueError(f"Unknown verification method: {self.method!r}")
        if not str(self.summary or "").strip():
            raise ValueError("Evidence needs a summary a person can read.")
        if len(str(self.summary)) > MAX_TEXT or len(str(self.detail)) > MAX_TEXT:
            raise ValueError(f"Evidence text must be at most {MAX_TEXT} characters.")
        for name, value, limit in (
            ("at", self.at, 60),
            ("game", self.game, 120),
            ("game_version", self.game_version, 60),
            ("session_id", self.session_id, 80),
            ("observation_id", self.observation_id, 80),
            ("objective_id", self.objective_id, 80),
        ):
            if len(str(value)) > limit:
                raise ValueError(f"Evidence {name} must be at most {limit} characters.")
        if len(str(self.fingerprint)) > 80:
            raise ValueError("An evidence fingerprint must be at most 80 characters.")
        if not self.fingerprint:
            payload = "|".join(
                [
                    self.source.value,
                    self.method.value,
                    self.session_id,
                    self.observation_id,
                    self.objective_id,
                    _clean(self.summary),
                ]
            )
            object.__setattr__(self, "fingerprint", f"e-{_digest(payload)}")

    @property
    def independent_key(self) -> str:
        """What makes two evidence entries independent observations.

        The same observation counted twice is one observation, however many times
        it is handed to the service. A SESSION is not an observation: two distinct
        readings in one session must be able to add two pieces of evidence, so the
        fallback is the fingerprint (which includes the session and the claim).
        """
        return self.observation_id or self.objective_id or self.fingerprint

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "summary": self.summary,
            "method": self.method.value,
            "verified": self.verified,
            "at": self.at,
            "game": self.game,
            "game_version": self.game_version,
            "session_id": self.session_id,
            "observation_id": self.observation_id,
            "objective_id": self.objective_id,
            "detail": self.detail,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "KnowledgeEvidence | None":
        data = dict(payload or {})
        try:
            return cls(
                source=EvidenceSource(str(data.get("source", "manual"))),
                summary=str(data.get("summary", "") or ""),
                method=VerificationMethod(str(data.get("method", "none"))),
                verified=bool(data.get("verified", False)),
                at=str(data.get("at", "") or ""),
                game=str(data.get("game", "") or ""),
                game_version=str(data.get("game_version", "") or ""),
                session_id=str(data.get("session_id", "") or ""),
                observation_id=str(data.get("observation_id", "") or ""),
                objective_id=str(data.get("objective_id", "") or ""),
                detail=str(data.get("detail", "") or ""),
                fingerprint=str(data.get("fingerprint", "") or ""),
            )
        except (TypeError, ValueError):
            return None


def evidence_for(
    source: EvidenceSource,
    summary: str,
    *,
    method: VerificationMethod = VerificationMethod.OBSERVED,
    verified: bool = False,
    game: str = "",
    game_version: str = "",
    session_id: str = "",
    observation_id: str = "",
    objective_id: str = "",
    detail: str = "",
    at: str = "",
) -> KnowledgeEvidence:
    """Small constructor so candidate builders do not repeat eleven keywords."""
    return KnowledgeEvidence(
        source=source,
        summary=summary,
        method=method,
        verified=verified,
        at=at,
        game=game,
        game_version=game_version,
        session_id=session_id,
        observation_id=observation_id,
        objective_id=objective_id,
        detail=detail,
    )


@dataclass(frozen=True, slots=True)
class KnowledgeRecord:
    """One durable claim about one game, with its history and its doubts."""

    knowledge_id: str
    game: str
    category: KnowledgeCategory
    key: str
    content: Mapping[str, Any]
    status: KnowledgeStatus = KnowledgeStatus.UNVERIFIED
    game_version: str = ""
    evidence: tuple[KnowledgeEvidence, ...] = ()
    provenance: str = ""
    revalidation: RevalidationPolicy = RevalidationPolicy.MANUAL
    created_at: str = ""
    updated_at: str = ""
    last_verified_at: str = ""
    reason: str = ""
    revision: int = 1
    contradicts: tuple[str, ...] = ()
    content_digest: str = ""
    #: Bounded status changes, oldest first. Evidence never lives only here;
    #: this says WHY a status moved, which the current status alone cannot.
    history: tuple[Mapping[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not str(self.knowledge_id or "").strip():
            raise ValueError("A knowledge record needs an id.")
        if len(str(self.knowledge_id)) > 80:
            raise ValueError("A knowledge id must be at most 80 characters.")
        if not str(self.game or "").strip():
            raise ValueError("A knowledge record needs a game identity.")
        if len(str(self.game)) > 120:
            raise ValueError("A game name must be at most 120 characters.")
        if not isinstance(self.category, KnowledgeCategory):
            raise ValueError(f"Unknown knowledge category: {self.category!r}")
        if not isinstance(self.status, KnowledgeStatus):
            raise ValueError(f"Unknown knowledge status: {self.status!r}")
        if not str(self.key or "").strip():
            raise ValueError("A knowledge record needs a key naming what it is about.")
        _validate_content(self.content)
        if len(str(self.game_version)) > 60:
            raise ValueError("A game version must be at most 60 characters.")
        if not isinstance(self.revalidation, RevalidationPolicy):
            raise ValueError(f"Unknown revalidation policy: {self.revalidation!r}")
        if len(self.evidence) > MAX_EVIDENCE:
            raise ValueError(f"A record may hold at most {MAX_EVIDENCE} evidence references.")
        if len(self.history) > MAX_HISTORY:
            raise ValueError(f"A record may hold at most {MAX_HISTORY} status changes.")
        if self.revision < 1:
            raise ValueError("A knowledge revision starts at 1.")
        if len(str(self.key)) > 120:
            raise ValueError("A knowledge key must be at most 120 characters.")
        if len(str(self.provenance)) > 120:
            raise ValueError("Provenance must be at most 120 characters.")
        if not self.content_digest:
            object.__setattr__(self, "content_digest", _content_digest(self.content))
        if self.status is KnowledgeStatus.VERIFIED and not self.last_verified_at:
            raise ValueError("A verified record must say when it was last verified.")

    @property
    def independent_evidence(self) -> int:
        return len({item.independent_key for item in self.evidence})

    @property
    def verified_effect(self) -> bool:
        """Whether the EFFECT of a control was observed, not merely the input."""
        return any(item.verified for item in self.evidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "knowledge_id": self.knowledge_id,
            "game": self.game,
            "game_version": self.game_version,
            "category": self.category.value,
            "key": self.key,
            "content": dict(self.content),
            "content_digest": self.content_digest,
            "status": self.status.value,
            "evidence": [item.to_dict() for item in self.evidence],
            # Derived, never stored: a reader gets the counts the service used.
            "independent_evidence": self.independent_evidence,
            "verified_effect": self.verified_effect,
            "provenance": self.provenance,
            "revalidation": self.revalidation.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_verified_at": self.last_verified_at,
            "reason": self.reason,
            "revision": self.revision,
            "contradicts": list(self.contradicts),
            "history": [dict(item) for item in self.history],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "KnowledgeRecord | None":
        data = dict(payload or {})
        try:
            evidence = tuple(
                row
                for row in (
                    KnowledgeEvidence.from_dict(item) for item in data.get("evidence") or []
                )
                if row is not None
            )
            return cls(
                knowledge_id=str(data.get("knowledge_id", "")),
                game=str(data.get("game", "")),
                category=KnowledgeCategory(str(data.get("category", ""))),
                key=str(data.get("key", "")),
                content=dict(data.get("content") or {}),
                status=KnowledgeStatus(str(data.get("status", "unverified"))),
                game_version=str(data.get("game_version", "") or ""),
                evidence=evidence,
                provenance=str(data.get("provenance", "") or ""),
                revalidation=RevalidationPolicy(str(data.get("revalidation", "manual"))),
                created_at=str(data.get("created_at", "") or ""),
                updated_at=str(data.get("updated_at", "") or ""),
                last_verified_at=str(data.get("last_verified_at", "") or ""),
                reason=str(data.get("reason", "") or ""),
                revision=int(data.get("revision", 1) or 1),
                contradicts=tuple(str(item) for item in data.get("contradicts") or []),
                content_digest=str(data.get("content_digest", "") or ""),
                history=tuple(
                    {str(key): str(value) for key, value in dict(item).items()}
                    for item in data.get("history") or []
                    if isinstance(item, Mapping)
                )[:MAX_HISTORY],
            )
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class KnowledgeCandidate:
    """A proposed claim, before the service decides what it may become."""

    game: str
    category: KnowledgeCategory
    key: str
    content: Mapping[str, Any]
    evidence: tuple[KnowledgeEvidence, ...] = ()
    game_version: str = ""
    provenance: str = ""
    revalidation: RevalidationPolicy = RevalidationPolicy.MANUAL

    def __post_init__(self) -> None:
        if not str(self.game or "").strip():
            raise ValueError("A knowledge candidate needs a game.")
        if len(str(self.game)) > 120:
            raise ValueError("A game name must be at most 120 characters.")
        if not isinstance(self.category, KnowledgeCategory):
            raise ValueError(f"Unknown knowledge category: {self.category!r}")
        if not str(self.key or "").strip():
            raise ValueError("A knowledge candidate needs a key.")
        if len(str(self.key)) > 120:
            raise ValueError("A knowledge key must be at most 120 characters.")
        _validate_content(self.content)
        if len(str(self.game_version)) > 60:
            raise ValueError("A game version must be at most 60 characters.")
        if len(str(self.provenance)) > 120:
            raise ValueError("Provenance must be at most 120 characters.")
        if len(self.evidence) > MAX_EVIDENCE:
            raise ValueError(f"A candidate may hold at most {MAX_EVIDENCE} evidence references.")

    @property
    def knowledge_id(self) -> str:
        return knowledge_id_for(
            self.game, self.category, self.key, self.content, self.game_version
        )


def transition(
    record: KnowledgeRecord, status: KnowledgeStatus, *, at: str, reason: str
) -> KnowledgeRecord:
    """Move a record to a new status, refusing any move the table forbids."""
    if not isinstance(status, KnowledgeStatus):
        raise ValueError(f"Unknown knowledge status: {status!r}")
    if status is record.status and not reason:
        return record
    if not can_transition(record.status, status):
        raise ValueError(
            f"A {record.status.value} record cannot become {status.value}: "
            f"allowed moves are {sorted(item.value for item in ALLOWED_TRANSITIONS[record.status])}"
        )
    if not str(reason or "").strip():
        raise ValueError("A status change must record why it happened.")
    return KnowledgeRecord(
        knowledge_id=record.knowledge_id,
        game=record.game,
        category=record.category,
        key=record.key,
        content=record.content,
        status=status,
        game_version=record.game_version,
        evidence=record.evidence,
        provenance=record.provenance,
        revalidation=record.revalidation,
        created_at=record.created_at,
        updated_at=at,
        last_verified_at=(
            at if status is KnowledgeStatus.VERIFIED else record.last_verified_at
        ),
        reason=_clean(reason),
        revision=record.revision + 1,
        contradicts=record.contradicts,
        content_digest=record.content_digest,
        history=(
            *record.history,
            {
                "at": at,
                "from": record.status.value,
                "to": status.value,
                "reason": _clean(reason),
            },
        )[-MAX_HISTORY:],
    )


# ---------------------------------------------------------------------------
# Candidates from the services that actually observed something
# ---------------------------------------------------------------------------


def _state_game(state: GameState) -> str:
    return _clean(state.observation.game or state.game, 120)


def _profile_version(state: GameState) -> str:
    profile = state.profile.profile
    return _clean(profile.version, 60) if profile is not None else ""


def state_candidates(state: GameState) -> list[KnowledgeCandidate]:
    """HUD/interface knowledge from ONE validated state — nothing inferred.

    Only fields the state actually RECOGNIZED become candidates. An absent or
    unreadable field is not knowledge about the game, so it is not stored; it
    stays in the state's own unknown list.
    """
    game = _state_game(state)
    if not game:
        return []
    version = _profile_version(state)
    observation = state.observation
    found: list[KnowledgeCandidate] = []
    for reading in state.hud:
        if reading.status is not HudStatus.RECOGNIZED:
            continue
        # What is LEARNED is the layout: the element, its label and its kind.
        # The text read this frame is an observation, not a claim about the game
        # — putting a changing value in the record made every counter update look
        # like a contradiction, and stored transient state as knowledge.
        content: dict[str, Any] = {
            "element_id": _clean(reading.element_id, 60),
            "label": _clean(reading.label, 80),
            "kind": reading.kind.value,
        }
        detail = [
            f"profile {state.profile.profile.profile_id if state.profile.profile else 'none'}",
        ]
        if observation.width and observation.height:
            detail.append(f"resolution {observation.width}x{observation.height}")
        evidence = evidence_for(
            EvidenceSource.STATE,
            _clean(f"HUD field {reading.element_id!r} was read as {reading.text!r}", MAX_TEXT),
            method=VerificationMethod.OBSERVED,
            game=game,
            game_version=version,
            session_id=observation.session_id,
            observation_id=observation.observation_id,
            at=observation.at or state.timestamp,
            detail=_clean("; ".join(detail), MAX_TEXT),
        )
        found.append(
            KnowledgeCandidate(
                game=game,
                game_version=version,
                category=KnowledgeCategory.HUD,
                key=f"hud:{reading.element_id}",
                content=content,
                evidence=(evidence,),
                provenance="NF-16 game-state reading",
                revalidation=RevalidationPolicy.VERSION_BOUND,
            )
        )
    interface = getattr(state, "interface", None)
    kind = str(getattr(interface, "kind", "") or "")
    if kind and kind != "unknown":
        content = {"kind": kind}
        labels = tuple(getattr(interface, "labels", ()) or ())
        if labels:
            content["labels"] = [_clean(label, 80) for label in labels[:4]]
        evidence = evidence_for(
            EvidenceSource.STATE,
            _clean(f"the interface was read as {kind}", MAX_TEXT),
            game=game,
            game_version=version,
            session_id=observation.session_id,
            observation_id=observation.observation_id,
            at=observation.at or state.timestamp,
            detail=_clean(
                f"resolution {observation.width}x{observation.height}"
                if observation.width and observation.height
                else "resolution not reported",
                MAX_TEXT,
            ),
        )
        found.append(
            KnowledgeCandidate(
                game=game,
                game_version=version,
                category=KnowledgeCategory.HUD,
                key="hud:interface",
                content=content,
                evidence=(evidence,),
                provenance="NF-16 game-state reading",
                revalidation=RevalidationPolicy.VERSION_BOUND,
            )
        )
    # NF-24: a CONFIRMED screen change is a durable fact about the game's flow
    # ("a victory screen follows gameplay"). A single frame's guess is not, so
    # only a confirmation is recorded, and an inferred cue says so in its method.
    transition = getattr(state, "transition", None)
    scene = getattr(state, "scene", None)
    confirmed = str(getattr(transition, "confirmed", "") or "")
    if (
        str(getattr(transition, "state", "")) == "confirmed"
        and confirmed
        and confirmed != "unknown"
    ):
        previous = str(getattr(transition, "previous", "") or "")
        scene_status = str(getattr(scene, "status", "unknown"))
        notes = tuple(getattr(scene, "evidence", ()) or ())
        evidence = evidence_for(
            EvidenceSource.STATE,
            _clean(
                f"the screen was confirmed as {confirmed!r}"
                + (f" after {previous!r}" if previous else "")
                + f" (read as {scene_status})",
                MAX_TEXT,
            ),
            method=(
                VerificationMethod.OBSERVED
                if scene_status == "observed"
                else VerificationMethod.INFERRED
            ),
            game=game,
            game_version=version,
            session_id=observation.session_id,
            observation_id=observation.observation_id,
            at=observation.at or state.timestamp,
            detail=_clean(
                notes[0] if notes else f"confirmed after {getattr(transition, 'required', 0)} observation(s)",
                MAX_TEXT,
            ),
        )
        found.append(
            KnowledgeCandidate(
                game=game,
                game_version=version,
                category=KnowledgeCategory.SITUATION,
                key=f"scene:{confirmed}",
                content={"scene": confirmed, "after": previous},
                evidence=(evidence,),
                provenance="NF-24 confirmed scene transition",
            )
        )
    return found


def objective_candidates(state: ObjectiveState) -> list[KnowledgeCandidate]:
    """Objective patterns, outcomes and open questions from one interpretation."""
    game = _clean(state.game, 120)
    if not game:
        return []
    found: list[KnowledgeCandidate] = []
    for objective in state.objectives:
        if objective.status is ObjectiveStatus.SUPERSEDED:
            continue
        common: dict[str, Any] = dict(
            game=game,
            session_id=state.session_id,
            observation_id=state.observation_id,
            at=state.at,
        )
        pattern_evidence = evidence_for(
            EvidenceSource.OBJECTIVE,
            _clean(
                f"objective {objective.description!r} was {objective.status.value}", MAX_TEXT
            ),
            method=VerificationMethod.OBSERVED if objective.explicit else VerificationMethod.INFERRED,
            objective_id=objective.objective_id,
            detail=_clean(objective.source, MAX_TEXT),
            **common,
        )
        found.append(
            KnowledgeCandidate(
                game=game,
                category=KnowledgeCategory.OBJECTIVE_PATTERN,
                key=f"objective:{objective.objective_id}",
                # The PATTERN is the task, not its progress: a status change is
                # an update to the reading, never a contradiction of the task.
                content={
                    "description": _clean(objective.description, 160),
                    "category": objective.category.value,
                    "explicit": objective.explicit,
                },
                evidence=(pattern_evidence,),
                provenance="NF-17 objective understanding",
            )
        )
        if objective.status in (ObjectiveStatus.COMPLETED, ObjectiveStatus.FAILED):
            # A confirmed outcome screen verifies the outcome exactly as a
            # printed banner does; without it in this set, an objective closed by
            # a victory screen would be stored as an UNVERIFIED outcome.
            effect = any(
                item.kind.value
                in {"completion_text", "failure_text", "counter_target", "scene"}
                for item in objective.evidence
            )
            outcome_evidence = evidence_for(
                EvidenceSource.OBJECTIVE,
                _clean(
                    f"objective {objective.description!r} was {objective.status.value} "
                    f"({objective.reason})",
                    MAX_TEXT,
                ),
                method=VerificationMethod.OBSERVED,
                verified=effect,
                objective_id=objective.objective_id,
                **common,
            )
            found.append(
                KnowledgeCandidate(
                    game=game,
                    category=KnowledgeCategory.OUTCOME,
                    key=f"outcome:{objective.objective_id}",
                    # The outcome KIND is durable; why it happened is evidence.
                    content={
                        "description": _clean(objective.description, 160),
                        "status": objective.status.value,
                    },
                    evidence=(outcome_evidence,),
                    provenance="NF-17 objective understanding",
                )
            )
        if objective.status in (ObjectiveStatus.UNKNOWN, ObjectiveStatus.NOT_VERIFIABLE):
            question_evidence = evidence_for(
                EvidenceSource.OBJECTIVE,
                _clean(
                    f"objective {objective.description!r} could not be established "
                    f"({objective.status.value}): {objective.reason}",
                    MAX_TEXT,
                ),
                method=VerificationMethod.OBSERVED,
                objective_id=objective.objective_id,
                **common,
            )
            found.append(
                KnowledgeCandidate(
                    game=game,
                    category=KnowledgeCategory.QUESTION,
                    key=f"question:{objective.objective_id}",
                    content={"description": _clean(objective.description, 160)},
                    evidence=(question_evidence,),
                    provenance="NF-17 objective understanding",
                )
            )
    return found


def control_candidate(
    *,
    game: str,
    action: str,
    effect: str,
    evidence: Iterable[KnowledgeEvidence],
    game_version: str = "",
    provenance: str = "declared control mapping",
) -> KnowledgeCandidate:
    """A control mapping — refused unless the EFFECT was observed.

    Sending input is not evidence that the game did anything; the phase text is
    explicit about it, so this builder rejects evidence that is not marked
    verified rather than leaving the check to a caller.
    """
    rows = tuple(evidence)
    if not rows:
        raise ValueError("A control mapping needs evidence that the effect occurred.")
    if not any(item.verified for item in rows):
        raise ValueError(
            "A control mapping cannot be recorded from an input alone: at least one "
            "evidence reference must show the expected EFFECT was observed."
        )
    return KnowledgeCandidate(
        game=_clean(game, 120),
        game_version=_clean(game_version, 60),
        category=KnowledgeCategory.CONTROL,
        key=f"control:{_clean(action, 80).casefold()}",
        content={"action": _clean(action, 80), "effect": _clean(effect, 160)},
        evidence=rows[:MAX_EVIDENCE],
        provenance=_clean(provenance, 120),
        revalidation=RevalidationPolicy.VERSION_BOUND,
    )


def candidate_from_payload(payload: Mapping[str, Any] | None) -> KnowledgeCandidate:
    """Build a candidate from an API body, refusing anything a record would reject.

    Invalid evidence is REFUSED rather than silently dropped: a caller that
    believes it supplied three observations must not be handed a record with
    one. An unknown category or status is named in the error.
    """
    data = dict(payload or {})
    try:
        category = KnowledgeCategory(str(data.get("category", "")))
    except ValueError as exc:
        raise ValueError(
            "category must be one of: " + ", ".join(KNOWN_CATEGORIES)
        ) from exc
    try:
        revalidation = RevalidationPolicy(str(data.get("revalidation", "manual")))
    except ValueError as exc:
        raise ValueError(
            "revalidation must be one of: "
            + ", ".join(item.value for item in RevalidationPolicy)
        ) from exc
    raw_evidence = data.get("evidence") or []
    if not isinstance(raw_evidence, (list, tuple)):
        raise ValueError("evidence must be a list of evidence references")
    evidence: list[KnowledgeEvidence] = []
    for index, row in enumerate(raw_evidence):
        if not isinstance(row, Mapping):
            raise ValueError(f"evidence[{index}] must be an object")
        parsed = KnowledgeEvidence.from_dict(row)
        if parsed is None:
            raise ValueError(
                f"evidence[{index}] is invalid: it needs a known source, method and summary"
            )
        evidence.append(parsed)
    content = data.get("content")
    if not isinstance(content, Mapping):
        raise ValueError("content must be an object of named fields")
    game = str(data.get("game") or "")
    if not game.strip():
        raise ValueError("game is required: knowledge is scoped to one game")
    if not evidence and bool(data.get("require_evidence", False)):
        raise ValueError("evidence is required for this claim")
    return KnowledgeCandidate(
        game=game,
        category=category,
        key=str(data.get("key") or ""),
        content=dict(content),
        evidence=tuple(evidence[:MAX_EVIDENCE]),
        game_version=str(data.get("game_version") or ""),
        provenance=str(data.get("provenance") or "manual knowledge entry"),
        revalidation=revalidation,
    )


# ---------------------------------------------------------------------------
# Session memory — bounded, transient, and deliberately NOT persistable
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SessionMemory:
    """What happened in the CURRENT session, kept apart from durable knowledge.

    Nothing here is written anywhere: the knowledge service accepts candidates,
    and this object is never one of its inputs. A session change clears it, so a
    stale objective cannot look like a fact on the next run.
    """

    max_items: int = 20
    session_id: str = ""
    game: str = ""
    observations: deque[dict[str, Any]] = field(default_factory=deque)
    objectives: deque[dict[str, Any]] = field(default_factory=deque)
    failures: deque[dict[str, Any]] = field(default_factory=deque)

    def __post_init__(self) -> None:
        self.max_items = max(1, int(self.max_items))
        self.observations = deque(maxlen=self.max_items)
        self.objectives = deque(maxlen=self.max_items)
        self.failures = deque(maxlen=self.max_items)

    def _switch(self, session_id: str, game: str) -> None:
        if session_id and session_id != self.session_id:
            self.clear()
            self.session_id = session_id
        if game:
            self.game = game

    def clear(self) -> None:
        self.observations.clear()
        self.objectives.clear()
        self.failures.clear()
        self.session_id = ""
        self.game = ""

    def note_state(self, state: GameState) -> None:
        self._switch(state.observation.session_id, _state_game(state))
        self.observations.append(
            {
                "observation_id": state.observation.observation_id,
                "at": state.observation.at or state.timestamp,
                "status": state.status.value,
                "recognized": len(state.recognized),
            }
        )

    def note_objectives(self, state: ObjectiveState) -> None:
        self._switch(state.session_id, state.game)
        self.objectives.append(
            {
                "observation_id": state.observation_id,
                "at": state.at,
                "objectives": [
                    {
                        "description": item.description,
                        "status": item.status.value,
                    }
                    for item in state.objectives
                    if item.status is not ObjectiveStatus.SUPERSEDED
                ][:8],
            }
        )

    def note_failure(self, reason_code: str, reason: str) -> None:
        self.failures.append(
            {"reason_code": _clean(reason_code, 60), "reason": _clean(reason, 200), "at": utc_stamp()}
        )

    def rows(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "game": self.game,
            "persisted": False,
            "observations": list(self.observations),
            "objectives": list(self.objectives),
            "failures": list(self.failures),
        }
