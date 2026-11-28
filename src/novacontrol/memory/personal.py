"""Phase 32 — structured long-term personal knowledge.

This module is the structured layer **over** the existing Phase 3 memory
subsystem. It does not open a second database: every record is a
``MemoryRecord`` in the shared ``long_term`` namespace, written through
``MemoryManager``/``SqliteMemoryStore``. The service keeps a query index in
memory, rebuilds it from that same store at boot, and preserves provenance,
version history, validity and privacy on every record.

There is deliberately no model call here. Extraction from a conversation is
limited to explicit, user-authored markers; everything else enters through the
documented API with its source and kind stated. An inference is never promoted
to a confirmed fact by this layer.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Callable
from uuid import uuid4

from novacontrol.memory.manager import MemoryManager
from novacontrol.memory.models import MemoryNamespace

SCHEMA_VERSION = "phase32.personal_knowledge.1"
RECORD_PREFIX = "pk:"
RELATION_PREFIX = "pkr:"

_SOURCE_CONFIDENCE: dict[str, float] = {
    "user": 0.96,
    "correction": 0.99,
    "document": 0.88,
    "application": 0.95,
    "execution": 0.93,
    "task": 0.86,
    "conversation": 0.72,
    "agent": 0.62,
    "inference": 0.32,
}
_VERIFIED_SOURCES = {
    "user",
    "correction",
    "document",
    "application",
    "execution",
    "task",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _as_utc(value: Any) -> datetime | None:
    """Coerce a stored/user timestamp to an aware UTC datetime.

    The API accepts ISO timestamps without an offset. Passing such a naive
    value into status/validity comparisons with ``utcnow()`` raises
    ``TypeError`` on the next read, so normalize at the record boundary and
    reject values that cannot be parsed.
    """
    if value is None or value == "":
        return None
    parsed: datetime | None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = _dt(value)
        if parsed is None:
            raise ValueError(f"invalid timestamp: {value!r}")
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _norm(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def _words(value: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9][a-z0-9_+.-]*", _norm(value)) if len(word) > 1}


def _clamp(value: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("a numeric value must be finite")
    return round(max(0.0, min(1.0, number)), 4)


class KnowledgeCategory(StrEnum):
    PREFERENCE = "preference"
    COMMUNICATION = "communication"
    GOAL = "goal"
    ACTIVE_PROJECT = "active_project"
    COMPLETED_PROJECT = "completed_project"
    PROFESSIONAL = "professional"
    ACADEMIC = "academic"
    SKILL = "skill"
    ROUTINE = "routine"
    DECISION = "decision"
    WORKFLOW = "workflow"
    RELATIONSHIP = "relationship"
    FACT = "fact"
    HISTORY = "history"
    OTHER = "other"


class KnowledgeKind(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    DECISION = "decision"
    OBSERVATION = "observation"
    ASSUMPTION = "assumption"


class KnowledgeStatus(StrEnum):
    CURRENT = "current"
    HISTORICAL = "historical"
    SUPERSEDED = "superseded"
    UNVERIFIED = "unverified"
    CONTRADICTED = "contradicted"
    EXPIRED = "expired"
    ARCHIVED = "archived"
    MERGED = "merged"


class KnowledgePrivacy(StrEnum):
    PUBLIC = "public"
    PERSONAL = "personal"
    PRIVATE = "private"
    SENSITIVE = "sensitive"


class KnowledgeRetention(StrEnum):
    EPHEMERAL = "ephemeral"
    STANDARD = "standard"
    LONG_TERM = "long_term"
    PERMANENT = "permanent"


class KnowledgeSource(StrEnum):
    USER = "user"
    CORRECTION = "correction"
    DOCUMENT = "document"
    APPLICATION = "application"
    EXECUTION = "execution"
    TASK = "task"
    CONVERSATION = "conversation"
    AGENT = "agent"
    INFERENCE = "inference"


@dataclass(frozen=True, slots=True)
class KnowledgeVersion:
    version: int
    content: str
    status: KnowledgeStatus
    confidence: float
    verified: bool
    source_type: KnowledgeSource
    reason: str = ""
    at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "content": self.content,
            "status": self.status.value,
            "confidence": self.confidence,
            "verified": self.verified,
            "source_type": self.source_type.value,
            "reason": self.reason,
            "at": _iso(self.at),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> KnowledgeVersion | None:
        try:
            return cls(
                version=max(1, int(payload.get("version", 1) or 1)),
                content=str(payload.get("content", "") or ""),
                status=KnowledgeStatus(str(payload.get("status", "historical"))),
                confidence=_clamp(float(payload.get("confidence", 0.0) or 0.0)),
                verified=bool(payload.get("verified", False)),
                source_type=KnowledgeSource(str(payload.get("source_type", "inference"))),
                reason=str(payload.get("reason", "") or ""),
                at=_dt(payload.get("at")) or utcnow(),
            )
        except (ValueError, TypeError):
            return None


@dataclass(frozen=True, slots=True)
class KnowledgeRecord:
    content: str
    subject: str = "user"
    predicate: str = ""
    category: KnowledgeCategory = KnowledgeCategory.FACT
    kind: KnowledgeKind = KnowledgeKind.FACT
    status: KnowledgeStatus = KnowledgeStatus.CURRENT
    source_type: KnowledgeSource = KnowledgeSource.USER
    source: str = ""
    project: str = ""
    entities: tuple[str, ...] = ()
    confidence: float = 0.0
    privacy: KnowledgePrivacy = KnowledgePrivacy.PERSONAL
    retention: KnowledgeRetention = KnowledgeRetention.LONG_TERM
    verified: bool = False
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    last_verified_at: datetime | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    version: int = 1
    history: tuple[KnowledgeVersion, ...] = ()
    supersedes: tuple[str, ...] = ()
    superseded_by: str = ""
    merged_into: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if not str(self.content).strip():
            raise ValueError("A knowledge record needs content.")
        if not str(self.subject).strip():
            raise ValueError("A knowledge record needs a subject.")
        if self.version < 1:
            raise ValueError("A knowledge record version starts at 1.")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be between 0 and 1.")
        for name in (
            "created_at",
            "updated_at",
            "last_verified_at",
            "valid_from",
            "valid_until",
        ):
            normalized: datetime | None = _as_utc(getattr(self, name))
            if normalized is not getattr(self, name):
                object.__setattr__(self, name, normalized)
        if self.valid_from is not None and self.valid_until is not None:
            if self.valid_until < self.valid_from:
                raise ValueError("valid_until cannot precede valid_from.")

    @property
    def normalized_content(self) -> str:
        return _norm(self.content)

    @property
    def identity(self) -> tuple[str, str, str, str]:
        """The field two records must share to be compared or superseded.

        A blank predicate means no field was declared. Treating every record
        with the same subject/category as one field made unrelated facts
        conflict merely because they were both facts about the user, so blank
        predicates compare by exact normalized content instead. Contradiction
        detection needs a declared predicate (``database``, ``timezone``, ...);
        without one, near-duplicate consolidation still works, but the system
        never guesses that two sentences describe the same property.
        Project scope is part of identity: the same database decision in two
        projects must not supersede or contradict the other project's record.
        """
        project = _norm(self.project)
        if _norm(self.predicate):
            return (
                project,
                _norm(self.subject),
                _norm(self.predicate),
                self.category.value,
            )
        return (
            project,
            _norm(self.subject),
            f"content:{self.normalized_content}",
            self.category.value,
        )

    def effective_status(self, now: datetime | None = None) -> KnowledgeStatus:
        now = now or utcnow()
        if self.status in {
            KnowledgeStatus.HISTORICAL,
            KnowledgeStatus.SUPERSEDED,
            KnowledgeStatus.CONTRADICTED,
            KnowledgeStatus.EXPIRED,
            KnowledgeStatus.ARCHIVED,
            KnowledgeStatus.MERGED,
        }:
            return self.status
        if self.valid_until is not None and self.valid_until <= now:
            return KnowledgeStatus.EXPIRED
        if self.valid_from is not None and self.valid_from > now:
            return KnowledgeStatus.UNVERIFIED
        if not self.verified or self.status is KnowledgeStatus.UNVERIFIED:
            return KnowledgeStatus.UNVERIFIED
        return KnowledgeStatus.CURRENT

    def is_current(self, now: datetime | None = None) -> bool:
        return self.effective_status(now) is KnowledgeStatus.CURRENT

    def with_(self, **changes: Any) -> KnowledgeRecord:
        return replace(self, **changes)

    def version_row(self, reason: str = "") -> KnowledgeVersion:
        return KnowledgeVersion(
            version=self.version,
            content=self.content,
            status=self.effective_status(),
            confidence=self.confidence,
            verified=self.verified,
            source_type=self.source_type,
            reason=reason,
            at=self.updated_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "subject": self.subject,
            "predicate": self.predicate,
            "category": self.category.value,
            "kind": self.kind.value,
            "status": self.effective_status().value,
            "stored_status": self.status.value,
            "source_type": self.source_type.value,
            "source": self.source,
            "project": self.project,
            "entities": list(self.entities),
            "confidence": self.confidence,
            "privacy": self.privacy.value,
            "retention": self.retention.value,
            "verified": self.verified,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "last_verified_at": _iso(self.last_verified_at),
            "valid_from": _iso(self.valid_from),
            "valid_until": _iso(self.valid_until),
            "version": self.version,
            "history": [row.to_dict() for row in self.history],
            "supersedes": list(self.supersedes),
            "superseded_by": self.superseded_by,
            "merged_into": self.merged_into,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> KnowledgeRecord | None:
        try:
            category = KnowledgeCategory(str(payload.get("category", "fact")))
            kind = KnowledgeKind(str(payload.get("kind", "fact")))
            status = KnowledgeStatus(
                str(payload.get("stored_status", payload.get("status", "current")))
            )
            source_type = KnowledgeSource(str(payload.get("source_type", "inference")))
            privacy = KnowledgePrivacy(str(payload.get("privacy", "personal")))
            retention = KnowledgeRetention(str(payload.get("retention", "long_term")))
            history = tuple(
                row
                for row in (
                    KnowledgeVersion.from_dict(item)
                    for item in payload.get("history", ()) or ()
                    if isinstance(item, Mapping)
                )
                if row is not None
            )
            return cls(
                id=str(payload.get("id", "") or uuid4().hex),
                content=str(payload.get("content", "") or ""),
                subject=str(payload.get("subject", "user") or "user"),
                predicate=str(payload.get("predicate", "") or ""),
                category=category,
                kind=kind,
                status=status,
                source_type=source_type,
                source=str(payload.get("source", "") or ""),
                project=str(payload.get("project", "") or ""),
                entities=tuple(str(item) for item in payload.get("entities", ()) or ()),
                confidence=_clamp(float(payload.get("confidence", 0.0) or 0.0)),
                privacy=privacy,
                retention=retention,
                verified=bool(payload.get("verified", False)),
                created_at=_dt(payload.get("created_at")) or utcnow(),
                updated_at=_dt(payload.get("updated_at")) or utcnow(),
                last_verified_at=_dt(payload.get("last_verified_at")),
                valid_from=_dt(payload.get("valid_from")),
                valid_until=_dt(payload.get("valid_until")),
                version=max(1, int(payload.get("version", 1) or 1)),
                history=history,
                supersedes=tuple(str(item) for item in payload.get("supersedes", ()) or ()),
                superseded_by=str(payload.get("superseded_by", "") or ""),
                merged_into=str(payload.get("merged_into", "") or ""),
                metadata=dict(payload.get("metadata") or {}),
            )
        except (ValueError, TypeError):
            return None


@dataclass(frozen=True, slots=True)
class KnowledgeRelation:
    source_id: str
    predicate: str
    target_id: str = ""
    target_type: str = "entity"
    target_label: str = ""
    provenance: str = ""
    confidence: float = 0.5
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("A relation needs a source record.")
        if not self.predicate.strip():
            raise ValueError("A relation needs a predicate.")

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.source_id, _norm(self.predicate), self.target_id or _norm(self.target_label))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "predicate": self.predicate,
            "target_id": self.target_id,
            "target_type": self.target_type,
            "target_label": self.target_label,
            "provenance": self.provenance,
            "confidence": self.confidence,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> KnowledgeRelation | None:
        try:
            source_id = str(payload.get("source_id", "") or "")
            predicate = str(payload.get("predicate", "") or "")
            if not source_id or not predicate:
                return None
            return cls(
                id=str(payload.get("id", "") or uuid4().hex),
                source_id=source_id,
                predicate=predicate,
                target_id=str(payload.get("target_id", "") or ""),
                target_type=str(payload.get("target_type", "entity") or "entity"),
                target_label=str(payload.get("target_label", "") or ""),
                provenance=str(payload.get("provenance", "") or ""),
                confidence=_clamp(float(payload.get("confidence", 0.5) or 0.5)),
                created_at=_dt(payload.get("created_at")) or utcnow(),
                updated_at=_dt(payload.get("updated_at")) or utcnow(),
            )
        except (ValueError, TypeError):
            return None


@dataclass(frozen=True, slots=True)
class KnowledgeAccess:
    """Who is asking. Agent access is narrower than the owner's access."""

    audience: str = "user"
    project: str = ""
    allow_personal: bool = True
    allow_private: bool = True
    allow_sensitive: bool = False

    @classmethod
    def user(cls, *, project: str = "", allow_sensitive: bool = False) -> KnowledgeAccess:
        return cls(
            audience="user",
            project=project,
            allow_personal=True,
            allow_private=True,
            allow_sensitive=allow_sensitive,
        )

    @classmethod
    def agent(cls, *, project: str = "", allow_personal: bool = True) -> KnowledgeAccess:
        return cls(
            audience="agent",
            project=project,
            allow_personal=allow_personal,
            allow_private=False,
            allow_sensitive=False,
        )


@dataclass(frozen=True, slots=True)
class KnowledgeQuery:
    query: str = ""
    project: str = ""
    entity: str = ""
    categories: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    limit: int = 10
    include_unverified: bool = False
    include_historical: bool = False
    include_contradicted: bool = False
    include_expired: bool = False
    expand_relations: bool = True

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit must be at least 1.")


@dataclass(frozen=True, slots=True)
class KnowledgeMatch:
    record: KnowledgeRecord
    score: float
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.to_dict(),
            "score": round(self.score, 4),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class KnowledgeContext:
    text: str
    hits: tuple[KnowledgeMatch, ...] = ()
    omitted: int = 0
    tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "hits": [hit.to_dict() for hit in self.hits],
            "omitted": self.omitted,
            "tokens": self.tokens,
        }


@dataclass(frozen=True, slots=True)
class KnowledgeConflict:
    existing: tuple[str, ...]
    incoming: str
    reason: str
    resolved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "existing": list(self.existing),
            "incoming": self.incoming,
            "reason": self.reason,
            "resolved": self.resolved,
        }


@dataclass(frozen=True, slots=True)
class KnowledgeWriteResult:
    action: str
    record: KnowledgeRecord | None = None
    duplicate_of: str = ""
    conflicts: tuple[KnowledgeConflict, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.action in {"created", "updated", "duplicate", "corrected", "resolved"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "ok": self.ok,
            "record": self.record.to_dict() if self.record else None,
            "duplicate_of": self.duplicate_of,
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class ConsolidationResult:
    batch_id: str
    groups: int = 0
    merged: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "groups": self.groups,
            "merged": list(self.merged),
            "kept": list(self.kept),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class KnowledgeGraph:
    nodes: tuple[KnowledgeRecord, ...] = ()
    edges: tuple[KnowledgeRelation, ...] = ()
    entities: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [record.to_dict() for record in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "entities": list(self.entities),
        }


class PersonalKnowledgeService:
    """Structured, evidence-aware knowledge over the shared memory store."""

    def __init__(
        self,
        memory: MemoryManager,
        *,
        publish: Callable[..., Any] | None = None,
        max_records: int = 5000,
        max_relations: int = 20000,
    ) -> None:
        self.memory = memory
        self._publish = publish
        self.max_records = max(100, int(max_records))
        self.max_relations = max(100, int(max_relations))
        self.records: dict[str, KnowledgeRecord] = {}
        self.relations: dict[str, KnowledgeRelation] = {}
        self._loaded = False
        self._lock = asyncio.Lock()
        self.last_error = ""

    # -- lifecycle ------------------------------------------------------------

    async def load(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            records = await self.memory.list_records(
                MemoryNamespace.LONG_TERM, prefix=RECORD_PREFIX
            )
            for stored in records:
                payload = stored.value.get("record") if isinstance(stored.value, Mapping) else None
                if not isinstance(payload, Mapping):
                    continue
                record = KnowledgeRecord.from_dict(payload)
                if record is not None:
                    self.records[record.id] = record
            relations = await self.memory.list_records(
                MemoryNamespace.LONG_TERM, prefix=RELATION_PREFIX
            )
            for stored in relations:
                payload = stored.value.get("relation") if isinstance(stored.value, Mapping) else None
                if not isinstance(payload, Mapping):
                    continue
                relation = KnowledgeRelation.from_dict(payload)
                if relation is not None:
                    self.relations[relation.id] = relation
            self.last_error = ""
            self._loaded = True

    def snapshot(self) -> dict[str, Any]:
        """Cheap status for the synchronous application status surface."""
        counts: dict[str, int] = defaultdict(int)
        categories: dict[str, int] = defaultdict(int)
        privacy: dict[str, int] = defaultdict(int)
        now = utcnow()
        for record in self.records.values():
            counts[record.effective_status(now).value] += 1
            categories[record.category.value] += 1
            privacy[record.privacy.value] += 1
        return {
            "schema": SCHEMA_VERSION,
            "loaded": self._loaded,
            "records": len(self.records),
            "relations": len(self.relations),
            "status": dict(sorted(counts.items())),
            "categories": dict(sorted(categories.items())),
            "privacy": dict(sorted(privacy.items())),
            "last_error": self.last_error,
        }

    async def status(self) -> dict[str, Any]:
        await self.load()
        return {**self.snapshot(), "store": type(self.memory).__name__}

    # -- acquisition ----------------------------------------------------------

    async def remember(
        self,
        content: str,
        *,
        subject: str = "user",
        predicate: str = "",
        category: KnowledgeCategory | str = KnowledgeCategory.FACT,
        kind: KnowledgeKind | str = KnowledgeKind.FACT,
        source_type: KnowledgeSource | str = KnowledgeSource.USER,
        source: str = "",
        project: str = "",
        entities: Sequence[str] = (),
        confidence: float | None = None,
        privacy: KnowledgePrivacy | str = KnowledgePrivacy.PERSONAL,
        retention: KnowledgeRetention | str = KnowledgeRetention.LONG_TERM,
        verified: bool | None = None,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        ttl_seconds: float | None = None,
        supersedes: Sequence[str] = (),
        correction: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> KnowledgeWriteResult:
        try:
            category = KnowledgeCategory(str(category))
            kind = KnowledgeKind(str(kind))
            source_enum = KnowledgeSource(str(source_type))
            privacy_enum = KnowledgePrivacy(str(privacy))
            retention_enum = KnowledgeRetention(str(retention))
        except ValueError as exc:
            return KnowledgeWriteResult("rejected", reasons=(str(exc),))
        clean = " ".join(str(content or "").split())
        if not clean:
            return KnowledgeWriteResult("rejected", reasons=("content is required",))
        if kind is KnowledgeKind.ASSUMPTION and source_enum not in {
            KnowledgeSource.INFERENCE,
            KnowledgeSource.AGENT,
        }:
            return KnowledgeWriteResult(
                "rejected",
                reasons=("an assumption must be sourced from inference or an agent",),
            )
        try:
            valid_from = _as_utc(valid_from)
            valid_until = _as_utc(valid_until)
            ttl_value = None if ttl_seconds is None else max(0.0, float(ttl_seconds))
            if ttl_value is not None and not math.isfinite(ttl_value):
                raise ValueError("ttl_seconds must be finite")
            if confidence is not None:
                confidence = float(confidence)
            metadata_dict = dict(metadata or {})
            entity_values = (
                [entities]
                if isinstance(entities, str)
                else [str(item) for item in entities]
            )
            supersedes_values = tuple(
                dict.fromkeys(
                    str(item)
                    for item in (
                        [supersedes]
                        if isinstance(supersedes, str)
                        else supersedes
                    )
                    if str(item).strip()
                )
            )
        except (TypeError, ValueError) as exc:
            return KnowledgeWriteResult(
                "rejected", reasons=(f"invalid knowledge field: {exc}",)
            )
        if valid_from is not None and valid_until is not None and valid_until < valid_from:
            return KnowledgeWriteResult(
                "rejected", reasons=("valid_until cannot precede valid_from",)
            )
        await self.load()
        if ttl_value is not None:
            valid_until = utcnow() + timedelta(seconds=ttl_value)
        if valid_until is None and retention_enum is KnowledgeRetention.EPHEMERAL:
            valid_until = utcnow() + timedelta(days=1)
        source_verified = (
            source_enum.value in _VERIFIED_SOURCES and kind is not KnowledgeKind.ASSUMPTION
        )
        # ``verified`` can make an evidence-bearing record less certain, but it
        # cannot turn a model/conversation observation into verified evidence.
        is_verified = source_verified if verified is None else bool(verified) and source_verified
        if kind is KnowledgeKind.ASSUMPTION:
            is_verified = False
        try:
            confidence_value = self._confidence(source_enum, kind, confidence)
        except (TypeError, ValueError) as exc:
            return KnowledgeWriteResult(
                "rejected", reasons=(f"invalid confidence: {exc}",)
            )
        now = utcnow()
        status = (
            KnowledgeStatus.CURRENT
            if is_verified
            else KnowledgeStatus.UNVERIFIED
        )
        record = KnowledgeRecord(
            content=clean,
            subject=str(subject).strip() or "user",
            predicate=str(predicate).strip(),
            category=category,
            kind=kind,
            status=status,
            source_type=source_enum,
            source=str(source).strip() or source_enum.value,
            project=str(project).strip(),
            entities=tuple(
                dict.fromkeys(
                    [str(subject).strip() or "user", *[item.strip() for item in entity_values if item.strip()]]
                )
            ),
            confidence=confidence_value,
            privacy=privacy_enum,
            retention=retention_enum,
            verified=is_verified,
            created_at=now,
            updated_at=now,
            last_verified_at=now if is_verified else None,
            valid_from=valid_from,
            valid_until=valid_until,
            metadata=metadata_dict,
            supersedes=supersedes_values,
        )
        async with self._lock:
            same_identity = [
                item
                for item in self.records.values()
                if item.identity == record.identity
                and item.effective_status(now)
                in {
                    KnowledgeStatus.CURRENT,
                    KnowledgeStatus.UNVERIFIED,
                    KnowledgeStatus.CONTRADICTED,
                }
            ]
            exact = next(
                (
                    item
                    for item in same_identity
                    if item.normalized_content == record.normalized_content
                ),
                None,
            )
            if exact is not None:
                if is_verified and not exact.verified:
                    updated = exact.with_(
                        status=KnowledgeStatus.CURRENT,
                        verified=True,
                        confidence=max(exact.confidence, confidence_value),
                        last_verified_at=now,
                        updated_at=now,
                        history=(*exact.history, exact.version_row("promoted by verified evidence")),
                        metadata={**dict(exact.metadata), "promoted_by": source or source_enum.value},
                    )
                    if not await self._store(updated):
                        return KnowledgeWriteResult("failed", reasons=(self.last_error,))
                    self.records[updated.id] = updated
                    await self._emit("personal_knowledge.updated", record_id=updated.id, status=updated.status.value)
                    return KnowledgeWriteResult("updated", record=updated)
                updated = exact.with_(
                    last_verified_at=now if is_verified else exact.last_verified_at,
                    updated_at=now,
                )
                if not await self._store(updated):
                    return KnowledgeWriteResult("failed", reasons=(self.last_error,))
                self.records[updated.id] = updated
                return KnowledgeWriteResult("duplicate", record=updated, duplicate_of=exact.id)
            explicit = bool(correction) or bool(supersedes)
            conflicts: list[KnowledgeConflict] = []
            superseded_targets: list[KnowledgeRecord] = []
            contradicted_targets: list[KnowledgeRecord] = []
            if exact is None and same_identity:
                if explicit:
                    superseded_targets.extend(same_identity)
                elif is_verified and all(
                    not old.verified or old.kind is KnowledgeKind.ASSUMPTION
                    for old in same_identity
                ):
                    superseded_targets.extend(same_identity)
                elif is_verified:
                    contradicted_targets.extend(same_identity)
                    for old in same_identity:
                        conflicts.append(
                            KnowledgeConflict(
                                existing=(old.id,),
                                incoming=record.id,
                                reason=(
                                    f"both records are verified and disagree on "
                                    f"{record.subject!r}/{record.predicate or 'value'}; "
                                    "resolve with an explicit correction"
                                ),
                            )
                        )
                    if conflicts:
                        record = record.with_(status=KnowledgeStatus.CONTRADICTED)
                else:
                    conflicts.append(
                        KnowledgeConflict(
                            existing=tuple(old.id for old in same_identity),
                            incoming=record.id,
                            reason=(
                                "the incoming record is unverified and conflicts with existing "
                                "knowledge; it is retained as unverified, not promoted"
                            ),
                        )
                    )
                    record = record.with_(status=KnowledgeStatus.CONTRADICTED)
            for old_id in supersedes_values:
                superseded_record = self.records.get(str(old_id))
                if (
                    superseded_record is not None
                    and superseded_record.superseded_by != record.id
                ):
                    superseded_targets.append(superseded_record)
            # Persist the incoming record before changing the old ones. If the
            # incoming write fails, the previously current record must not be
            # left superseded with no replacement.
            target_ids = tuple(
                dict.fromkeys(old.id for old in superseded_targets)
            )
            record = record.with_(
                supersedes=tuple(dict.fromkeys((*record.supersedes, *target_ids)))
            )
            if not await self._store(record):
                return KnowledgeWriteResult("failed", reasons=(self.last_error,))
            self.records[record.id] = record
            for old in superseded_targets:
                await self._supersede(
                    old,
                    record.id,
                    reason=(
                        "explicitly replaced"
                        if explicit
                        else "confirmed evidence superseded an unverified record"
                    ),
                )
            for old in contradicted_targets:
                contradicted = old.with_(
                    status=KnowledgeStatus.CONTRADICTED,
                    updated_at=now,
                    history=(
                        *old.history,
                        old.version_row("conflicting verified information"),
                    ),
                )
                if await self._store(contradicted):
                    self.records[contradicted.id] = contradicted
            self._trim()
            if conflicts:
                await self._emit(
                    "personal_knowledge.conflict",
                    record_id=record.id,
                    conflicts=len(conflicts),
                    status=record.status.value,
                )
                return KnowledgeWriteResult("conflict", record=record, conflicts=tuple(conflicts))
            await self._emit("personal_knowledge.remembered", record_id=record.id, status=record.status.value)
            return KnowledgeWriteResult("created", record=record)

    async def correct(
        self,
        record_id: str,
        content: str,
        *,
        source: str = "",
        reason: str = "",
        confidence: float | None = None,
        verified: bool | None = True,
        access: KnowledgeAccess | None = None,
    ) -> KnowledgeWriteResult:
        await self.load()
        access = access or KnowledgeAccess.user()
        async with self._lock:
            old = self.records.get(str(record_id))
            if old is None or not self._visible(old, access):
                return KnowledgeWriteResult("failed", reasons=("knowledge record not found",))
            clean = " ".join(str(content or "").split())
            if not clean:
                return KnowledgeWriteResult("rejected", reasons=("content is required",))
            now = utcnow()
            try:
                confidence_value = self._confidence(
                    KnowledgeSource.CORRECTION, old.kind, confidence
                )
            except (TypeError, ValueError) as exc:
                return KnowledgeWriteResult(
                    "rejected", reasons=(f"invalid confidence: {exc}",)
                )
            new = old.with_(
                content=clean,
                source_type=KnowledgeSource.CORRECTION,
                source=source or "user correction",
                confidence=confidence_value,
                verified=bool(verified),
                status=KnowledgeStatus.CURRENT,
                updated_at=now,
                last_verified_at=now if verified else old.last_verified_at,
                version=old.version + 1,
                history=(*old.history, old.version_row(reason or "corrected by the user")),
                metadata={**dict(old.metadata), "corrected_from": old.content[:500]},
                superseded_by="",
                merged_into="",
            )
            if not await self._store(new):
                return KnowledgeWriteResult("failed", reasons=(self.last_error,))
            for other in list(self.records.values()):
                if other.id != old.id and other.identity == old.identity and other.is_current(now):
                    contradicted = other.with_(
                        status=KnowledgeStatus.SUPERSEDED,
                        superseded_by=old.id,
                        updated_at=now,
                        history=(*other.history, other.version_row(reason or "superseded by correction")),
                    )
                    if await self._store(contradicted):
                        self.records[contradicted.id] = contradicted
                elif other.id != old.id and other.identity == old.identity and other.status is KnowledgeStatus.CONTRADICTED:
                    resolved = other.with_(
                        status=KnowledgeStatus.SUPERSEDED,
                        superseded_by=old.id,
                        updated_at=now,
                    )
                    if await self._store(resolved):
                        self.records[resolved.id] = resolved
            self.records[new.id] = new
            await self._emit("personal_knowledge.corrected", record_id=new.id)
            return KnowledgeWriteResult("corrected", record=new)

    async def delete(
        self,
        record_id: str,
        *,
        reason: str = "",
        access: KnowledgeAccess | None = None,
    ) -> bool:
        await self.load()
        access = access or KnowledgeAccess.user()
        async with self._lock:
            record = self.records.get(str(record_id))
            if record is None or not self._visible(record, access):
                return False
            await self.memory.forget(MemoryNamespace.LONG_TERM, self._key(record.id))
            self.records.pop(str(record_id), None)
            for relation in list(self.relations.values()):
                if relation.source_id == record_id or relation.target_id == record_id:
                    await self.memory.forget(MemoryNamespace.LONG_TERM, self._relation_key(relation.id))
                    self.relations.pop(relation.id, None)
            await self._emit("personal_knowledge.deleted", record_id=str(record_id))
            return True

    async def resolve_conflict(
        self,
        record_id: str,
        *,
        keep: bool = True,
        reason: str = "",
        access: KnowledgeAccess | None = None,
    ) -> KnowledgeWriteResult:
        await self.load()
        access = access or KnowledgeAccess.user()
        async with self._lock:
            chosen = self.records.get(str(record_id))
            if chosen is None or not self._visible(chosen, access):
                return KnowledgeWriteResult("failed", reasons=("knowledge record not found",))
            now = utcnow()
            if keep:
                resolved = chosen.with_(
                    status=KnowledgeStatus.CURRENT,
                    verified=True,
                    source_type=KnowledgeSource.CORRECTION,
                    source="user conflict resolution",
                    updated_at=now,
                    last_verified_at=now,
                    history=(*chosen.history, chosen.version_row(reason or "conflict resolved by user")),
                    superseded_by="",
                    merged_into="",
                )
                if not await self._store(resolved):
                    return KnowledgeWriteResult("failed", reasons=(self.last_error,))
                self.records[resolved.id] = resolved
                for other in list(self.records.values()):
                    if other.id != resolved.id and other.identity == resolved.identity and other.status is KnowledgeStatus.CONTRADICTED:
                        superseded = other.with_(
                            status=KnowledgeStatus.SUPERSEDED,
                            superseded_by=resolved.id,
                            updated_at=now,
                            history=(*other.history, other.version_row(reason or "conflict resolved elsewhere")),
                        )
                        if await self._store(superseded):
                            self.records[superseded.id] = superseded
            else:
                archived = chosen.with_(
                    status=KnowledgeStatus.ARCHIVED,
                    updated_at=now,
                    history=(*chosen.history, chosen.version_row(reason or "conflict withdrawn")),
                )
                if not await self._store(archived):
                    return KnowledgeWriteResult("failed", reasons=(self.last_error,))
                self.records[archived.id] = archived
                resolved = archived
                # Withdrawing one side of a contradiction must leave a usable
                # record, not two dead ends. Restore the opposing
                # contradicted records so the explicit choice is observable in
                # subsequent retrieval.
                for other in list(self.records.values()):
                    if (
                        other.id != archived.id
                        and other.identity == archived.identity
                        and other.status is KnowledgeStatus.CONTRADICTED
                    ):
                        restored = other.with_(
                            status=KnowledgeStatus.CURRENT,
                            updated_at=now,
                            last_verified_at=(
                                now if other.verified else other.last_verified_at
                            ),
                            superseded_by="",
                            merged_into="",
                            history=(
                                *other.history,
                                other.version_row(
                                    reason or "conflict resolved by withdrawing the other side"
                                ),
                            ),
                        )
                        if await self._store(restored):
                            self.records[restored.id] = restored
            await self._emit("personal_knowledge.resolved", record_id=resolved.id)
            return KnowledgeWriteResult("resolved", record=resolved)

    # -- retrieval ------------------------------------------------------------

    async def retrieve(
        self,
        query: str = "",
        *,
        access: KnowledgeAccess | None = None,
        limit: int = 10,
        project: str = "",
        entity: str = "",
        categories: Sequence[str] = (),
        kinds: Sequence[str] = (),
        include_unverified: bool = False,
        include_historical: bool = False,
        include_contradicted: bool = False,
        include_expired: bool = False,
        expand_relations: bool = True,
    ) -> tuple[KnowledgeMatch, ...]:
        await self.load()
        access = access or KnowledgeAccess.user()
        now = utcnow()
        wanted_terms = _words(query)
        wanted_categories = {str(item) for item in categories}
        wanted_kinds = {str(item) for item in kinds}
        statuses = {KnowledgeStatus.CURRENT}
        if include_unverified:
            statuses.add(KnowledgeStatus.UNVERIFIED)
        if include_historical:
            statuses.update({KnowledgeStatus.HISTORICAL, KnowledgeStatus.SUPERSEDED})
        if include_contradicted:
            statuses.add(KnowledgeStatus.CONTRADICTED)
        if include_expired:
            statuses.update({KnowledgeStatus.EXPIRED, KnowledgeStatus.ARCHIVED})
        candidates: list[KnowledgeMatch] = []
        for record in self.records.values():
            if not self._visible(record, access):
                continue
            status = record.effective_status(now)
            if status not in statuses:
                continue
            if wanted_categories and record.category.value not in wanted_categories:
                continue
            if wanted_kinds and record.kind.value not in wanted_kinds:
                continue
            if project and record.project and record.project != project:
                continue
            if entity and entity.lower() not in {
                item.lower() for item in record.entities
            } and entity.lower() not in _norm(record.content):
                continue
            score, reasons = self._score(record, wanted_terms, query, project, now)
            if query.strip() and score <= 0:
                continue
            candidates.append(KnowledgeMatch(record, score, tuple(reasons)))
        candidates.sort(
            key=lambda match: (match.score, match.record.confidence, match.record.updated_at),
            reverse=True,
        )
        if expand_relations and candidates:
            candidate_ids = {match.record.id for match in candidates[: min(5, len(candidates))]}
            extra: dict[str, float] = {}
            for relation in self.relations.values():
                if relation.source_id in candidate_ids and relation.target_id:
                    other = self.records.get(relation.target_id)
                    if other is not None and self._visible(other, access) and _terms_in(relation.target_label, wanted_terms):
                        extra[other.id] = max(extra.get(other.id, 0.0), 0.4)
                if relation.target_id in candidate_ids:
                    other = self.records.get(relation.source_id)
                    if other is not None and self._visible(other, access) and _terms_in(
                        relation.predicate, wanted_terms
                    ):
                        extra[other.id] = max(extra.get(other.id, 0.0), 0.3)
            for record_id, bonus in extra.items():
                if any(match.record.id == record_id for match in candidates):
                    continue
                record = self.records[record_id]
                if record.effective_status(now) is KnowledgeStatus.CURRENT:
                    candidates.append(
                        KnowledgeMatch(record, bonus, ("related knowledge graph",))
                    )
            candidates.sort(
                key=lambda match: (match.score, match.record.confidence, match.record.updated_at),
                reverse=True,
            )
        return tuple(candidates[: max(1, int(limit))])

    async def context_for(
        self,
        query: str,
        *,
        access: KnowledgeAccess | None = None,
        budget_tokens: int = 400,
        project: str = "",
        limit: int = 8,
        include_unverified: bool = False,
        include_historical: bool = False,
    ) -> KnowledgeContext:
        hits = await self.retrieve(
            query,
            access=access,
            limit=limit,
            project=project,
            include_unverified=include_unverified,
            include_historical=include_historical,
            expand_relations=True,
        )
        if not hits:
            return KnowledgeContext("")
        header = "Relevant personal knowledge (evidence-bearing, not a model assumption):"
        selected: list[KnowledgeMatch] = []
        omitted = 0
        lines = [header]
        budget = max(1, int(budget_tokens))
        used = len(header.split())
        for hit in hits:
            record = hit.record
            provenance = record.source or record.source_type.value
            line = (
                f"- [{record.category.value}/{record.kind.value}; "
                f"{record.effective_status().value}; "
                f"{record.confidence:.2f} confidence; source {provenance}] {record.content}"
            )
            cost = len(line.split())
            if used + cost > budget:
                omitted += 1
                continue
            selected.append(hit)
            lines.append(line)
            used += cost
        omitted += max(0, len(hits) - len(selected) - omitted)
        return KnowledgeContext(
            "\n".join(lines) if selected else "",
            tuple(selected),
            omitted,
            used if selected else 0,
        )

    async def inspect(
        self,
        record_id: str,
        *,
        access: KnowledgeAccess | None = None,
        include_relations: bool = True,
    ) -> dict[str, Any]:
        await self.load()
        access = access or KnowledgeAccess.user()
        record = self.records.get(str(record_id))
        if record is None or not self._visible(record, access):
            return {"found": False, "record_id": str(record_id)}
        relations = (
            [
                relation.to_dict()
                for relation in self.relations.values()
                if (
                    (relation.source_id == record.id or relation.target_id == record.id)
                    and self._relation_visible(relation, access)
                )
            ]
            if include_relations
            else []
        )
        return {"found": True, "record": record.to_dict(), "relations": relations}

    async def graph(
        self,
        record_id: str,
        *,
        depth: int = 1,
        access: KnowledgeAccess | None = None,
    ) -> KnowledgeGraph:
        await self.load()
        access = access or KnowledgeAccess.user()
        root = self.records.get(str(record_id))
        if root is None or not self._visible(root, access):
            return KnowledgeGraph()
        seen = {root.id}
        nodes: list[KnowledgeRecord] = [root]
        edges: list[KnowledgeRelation] = []
        frontier = {root.id}
        for _ in range(max(0, min(4, int(depth)))):
            next_frontier: set[str] = set()
            for relation in self.relations.values():
                if relation.source_id in frontier:
                    other_id = relation.target_id
                elif relation.target_id in frontier:
                    other_id = relation.source_id
                else:
                    continue
                other = self.records.get(other_id)
                if other is None or not self._visible(other, access):
                    continue
                if relation.id not in {edge.id for edge in edges}:
                    edges.append(relation)
                if other.id not in seen:
                    seen.add(other.id)
                    nodes.append(other)
                    next_frontier.add(other.id)
            frontier = next_frontier
            if not frontier:
                break
        entities = tuple(
            dict.fromkeys(
                [
                    entity
                    for node in nodes
                    for entity in node.entities
                    if entity
                ]
                + [edge.target_label for edge in edges if edge.target_label]
            )
        )
        return KnowledgeGraph(tuple(nodes), tuple(edges), entities)

    async def relations_for(
        self, record_id: str, *, access: KnowledgeAccess | None = None
    ) -> tuple[KnowledgeRelation, ...]:
        graph = await self.graph(record_id, depth=1, access=access)
        return graph.edges

    async def relate(
        self,
        source_id: str,
        predicate: str,
        *,
        target_id: str = "",
        target_type: str = "entity",
        target_label: str = "",
        provenance: str = "",
        confidence: float = 0.6,
        access: KnowledgeAccess | None = None,
    ) -> KnowledgeRelation | None:
        await self.load()
        access = access or KnowledgeAccess.user()
        source = self.records.get(str(source_id))
        if source is None or not self._visible(source, access):
            return None
        if not str(predicate).strip():
            return None
        if target_id and target_id == source_id:
            return None
        if target_id:
            target = self.records.get(str(target_id))
            if target is None or not self._visible(target, access):
                return None
            target_label = target_label or target.content[:120]
        try:
            relation = KnowledgeRelation(
                source_id=source.id,
                predicate=str(predicate).strip(),
                target_id=str(target_id),
                target_type=str(target_type or "entity"),
                target_label=str(target_label),
                provenance=provenance or source.source,
                confidence=_clamp(confidence),
            )
        except (TypeError, ValueError):
            return None
        existing = next(
            (
                item
                for item in self.relations.values()
                if item.key == relation.key
            ),
            None,
        )
        if existing is not None:
            updated = replace(
                existing,
                target_label=relation.target_label,
                provenance=relation.provenance,
                confidence=max(existing.confidence, relation.confidence),
                updated_at=utcnow(),
            )
            if not await self._store_relation(updated):
                return None
            self.relations[updated.id] = updated
            return updated
        async with self._lock:
            if not await self._store_relation(relation):
                return None
            self.relations[relation.id] = relation
            self._trim()
        await self._emit(
            "personal_knowledge.relation",
            relation_id=relation.id,
            source_id=relation.source_id,
            target_id=relation.target_id,
        )
        return relation

    # -- consolidation and retention -----------------------------------------

    async def consolidate(self, *, access: KnowledgeAccess | None = None) -> ConsolidationResult:
        await self.load()
        access = access or KnowledgeAccess.user()
        groups: dict[tuple[tuple[str, str, str, str], str], list[KnowledgeRecord]] = defaultdict(list)
        for record in self.records.values():
            if record.effective_status() is KnowledgeStatus.CURRENT and self._visible(record, access):
                groups[(record.identity, record.normalized_content)].append(record)
        batch = uuid4().hex
        merged: list[str] = []
        kept: list[str] = []
        reasons: list[str] = []
        async with self._lock:
            for group in groups.values():
                if len(group) < 2:
                    continue
                canonical = sorted(
                    group,
                    key=lambda item: (
                        item.verified,
                        item.confidence,
                        item.updated_at,
                    ),
                    reverse=True,
                )[0]
                union_entities = tuple(
                    dict.fromkeys(entity for item in group for entity in item.entities)
                )
                updated = canonical.with_(
                    entities=union_entities,
                    metadata={
                        **dict(canonical.metadata),
                        "consolidation_batch": batch,
                        "consolidated_from": [item.id for item in group if item.id != canonical.id],
                    },
                    updated_at=utcnow(),
                )
                if not await self._store(updated):
                    reasons.append(self.last_error)
                    continue
                self.records[updated.id] = updated
                kept.append(updated.id)
                for duplicate in group:
                    if duplicate.id == canonical.id:
                        continue
                    archived = duplicate.with_(
                        status=KnowledgeStatus.MERGED,
                        merged_into=updated.id,
                        updated_at=utcnow(),
                        history=(
                            *duplicate.history,
                            duplicate.version_row("consolidated into an equivalent record"),
                        ),
                        metadata={**dict(duplicate.metadata), "consolidation_batch": batch},
                    )
                    if await self._store(archived):
                        self.records[archived.id] = archived
                        merged.append(archived.id)
                    else:
                        reasons.append(self.last_error)
                await self._emit(
                    "personal_knowledge.consolidated",
                    batch_id=batch,
                    kept=updated.id,
                    merged=len(group) - 1,
                )
        return ConsolidationResult(batch, len(kept), tuple(merged), tuple(kept), tuple(reasons))

    async def undo_consolidation(
        self, batch_id: str, *, access: KnowledgeAccess | None = None
    ) -> int:
        await self.load()
        access = access or KnowledgeAccess.user()
        restored = 0
        canonical_ids: set[str] = set()
        async with self._lock:
            for record in list(self.records.values()):
                if (
                    record.status is KnowledgeStatus.MERGED
                    and str(record.metadata.get("consolidation_batch", "")) == str(batch_id)
                    and self._visible(record, access)
                ):
                    canonical_ids.add(str(record.merged_into))
                    restored_record = record.with_(
                        status=KnowledgeStatus.CURRENT,
                        merged_into="",
                        updated_at=utcnow(),
                        history=(
                            *record.history,
                            record.version_row("consolidation undone"),
                        ),
                    )
                    if await self._store(restored_record):
                        self.records[restored_record.id] = restored_record
                        restored += 1
            for canonical_id in canonical_ids:
                canonical = self.records.get(canonical_id)
                if (
                    canonical is None
                    or not self._visible(canonical, access)
                    or str(canonical.metadata.get("consolidation_batch", ""))
                    != str(batch_id)
                ):
                    continue
                metadata = dict(canonical.metadata)
                metadata.pop("consolidation_batch", None)
                metadata.pop("consolidated_from", None)
                updated = canonical.with_(
                    metadata=metadata,
                    updated_at=utcnow(),
                    history=(*canonical.history, canonical.version_row("consolidation undone")),
                )
                if await self._store(updated):
                    self.records[updated.id] = updated
        return restored

    async def cleanup(self, *, now: datetime | None = None) -> dict[str, int]:
        await self.load()
        now = now or utcnow()
        expired = 0
        archived = 0
        async with self._lock:
            for record in list(self.records.values()):
                if record.status in {
                    KnowledgeStatus.EXPIRED,
                    KnowledgeStatus.ARCHIVED,
                    KnowledgeStatus.MERGED,
                    KnowledgeStatus.SUPERSEDED,
                    KnowledgeStatus.CONTRADICTED,
                    KnowledgeStatus.HISTORICAL,
                }:
                    continue
                if record.valid_until is not None and record.valid_until <= now:
                    updated = record.with_(
                        status=(
                            KnowledgeStatus.ARCHIVED
                            if record.retention in {
                                KnowledgeRetention.EPHEMERAL,
                                KnowledgeRetention.STANDARD,
                            }
                            else KnowledgeStatus.EXPIRED
                        ),
                        updated_at=now,
                        history=(*record.history, record.version_row("validity period ended")),
                    )
                    if await self._store(updated):
                        self.records[updated.id] = updated
                        expired += 1
                        if updated.status is KnowledgeStatus.ARCHIVED:
                            archived += 1
        return {"expired": expired, "archived": archived}

    async def export(
        self,
        *,
        access: KnowledgeAccess | None = None,
        include_historical: bool = True,
        include_sensitive: bool = False,
    ) -> dict[str, Any]:
        await self.load()
        access = access or KnowledgeAccess.user(allow_sensitive=include_sensitive)
        records = [
            record.to_dict()
            for record in self.records.values()
            if (include_historical or record.effective_status() is KnowledgeStatus.CURRENT)
            and self._visible(record, access)
        ]
        ids = {str(record["id"]) for record in records}
        relations = [
            relation.to_dict()
            for relation in self.relations.values()
            if relation.source_id in ids
            and (not relation.target_id or relation.target_id in ids)
            and self._relation_visible(relation, access)
        ]
        return {
            "schema": SCHEMA_VERSION,
            "exported_at": _iso(utcnow()),
            "records": records,
            "relations": relations,
            "counts": {"records": len(records), "relations": len(relations)},
        }

    # -- helpers --------------------------------------------------------------

    def _visible(self, record: KnowledgeRecord, access: KnowledgeAccess) -> bool:
        if access.audience == "agent":
            # An agent without a declared project must not inherit the union of
            # every project's memory. Project records require an explicit
            # project scope; global records remain available subject to the
            # privacy checks below.
            if record.project and not access.project:
                return False
            if record.privacy in {
                KnowledgePrivacy.PRIVATE,
                KnowledgePrivacy.SENSITIVE,
            }:
                return False
            if record.privacy is KnowledgePrivacy.PERSONAL and not access.allow_personal:
                return False
        elif record.privacy is KnowledgePrivacy.SENSITIVE and not access.allow_sensitive:
            return False
        if access.project and record.project and record.project != access.project:
            return False
        return True

    def _relation_visible(
        self, relation: KnowledgeRelation, access: KnowledgeAccess
    ) -> bool:
        """A relation is visible only when both of its record endpoints are.

        Filtering only the source would leak a hidden target's id and label
        through inspect/export; a label-only edge has no record endpoint to
        hide and is governed by its visible source.
        """
        source = self.records.get(relation.source_id)
        if source is None or not self._visible(source, access):
            return False
        if not relation.target_id:
            return True
        target = self.records.get(relation.target_id)
        return target is not None and self._visible(target, access)

    def _score(
        self,
        record: KnowledgeRecord,
        wanted_terms: set[str],
        query: str,
        project: str,
        now: datetime,
    ) -> tuple[float, list[str]]:
        reasons: list[str] = []
        record_terms = _words(
            " ".join(
                [
                    record.content,
                    record.subject,
                    record.predicate,
                    record.project,
                    record.category.value,
                    *record.entities,
                ]
            )
        )
        overlap = wanted_terms & record_terms
        score = float(len(overlap)) * 1.4
        if overlap:
            reasons.append("content/entity match")
        # One generic shared word ("project", "use") is not evidence that a
        # record belongs to a multiword question. Require subject/entity
        # evidence or at least two content terms, so unrelated project records
        # do not ride along with the project that was actually asked about.
        subject_match = bool(record.subject and record.subject.lower() in _norm(query))
        entity_match = any(
            entity and entity.lower() in _norm(query) for entity in record.entities
        )
        if (
            query.strip()
            and len(wanted_terms) >= 2
            and len(overlap) < 2
            and not subject_match
            and not entity_match
        ):
            return 0.0, ["insufficient relevance to the question"]
        normalized = _norm(query)
        if normalized and normalized in _norm(record.content):
            score += 2.0
            reasons.append("exact content match")
        if record.subject and record.subject.lower() in _norm(query):
            score += 2.2
            reasons.append("subject match")
        if project and record.project == project:
            score += 1.2
            reasons.append("project match")
        for entity in record.entities:
            if entity and entity.lower() in _norm(query):
                score += 1.0
                reasons.append("entity match")
                break
        if not query.strip():
            score = 0.2
            reasons.append("recent/active knowledge")
        age_days = max(0.0, (now - record.updated_at).total_seconds() / 86400.0)
        recency = 1.0 / (1.0 + age_days / 14.0)
        score *= 0.65 + (0.35 * recency)
        score *= 0.55 + (0.45 * record.confidence)
        if record.kind is KnowledgeKind.DECISION:
            score += 0.2
            reasons.append("decision")
        if record.verified:
            reasons.append("verified evidence")
        reasons.append(f"validity {record.effective_status(now).value}")
        return round(score, 4), reasons

    def _confidence(
        self,
        source: KnowledgeSource,
        kind: KnowledgeKind,
        supplied: float | None,
    ) -> float:
        if supplied is not None:
            value = _clamp(supplied)
        else:
            value = _SOURCE_CONFIDENCE.get(source.value, 0.4)
        if kind is KnowledgeKind.ASSUMPTION:
            value = min(value, 0.35)
        elif kind is KnowledgeKind.OBSERVATION:
            value = min(value, 0.72)
        return _clamp(value)

    async def _supersede(
        self, old: KnowledgeRecord, new_id: str, *, reason: str
    ) -> None:
        if old.status is KnowledgeStatus.SUPERSEDED and old.superseded_by == new_id:
            return
        superseded = old.with_(
            status=KnowledgeStatus.SUPERSEDED,
            superseded_by=new_id,
            updated_at=utcnow(),
            history=(*old.history, old.version_row(reason)),
        )
        if await self._store(superseded):
            self.records[superseded.id] = superseded

    def _key(self, record_id: str) -> str:
        return f"{RECORD_PREFIX}{record_id}"

    def _relation_key(self, relation_id: str) -> str:
        return f"{RELATION_PREFIX}{relation_id}"

    async def _store(self, record: KnowledgeRecord) -> bool:
        try:
            await self.memory.remember(
                MemoryNamespace.LONG_TERM,
                self._key(record.id),
                {"schema": SCHEMA_VERSION, "record": record.to_dict()},
                text=record.content,
                metadata={
                    "schema": SCHEMA_VERSION,
                    "record_id": record.id,
                    "category": record.category.value,
                    "privacy": record.privacy.value,
                    "status": record.status.value,
                },
                importance=record.confidence,
            )
            self.last_error = ""
            return True
        except Exception as exc:  # persistence must be reported, never fabricated
            self.last_error = f"persistence failed: {type(exc).__name__}: {exc}"
            return False

    async def _store_relation(self, relation: KnowledgeRelation) -> bool:
        try:
            await self.memory.remember(
                MemoryNamespace.LONG_TERM,
                self._relation_key(relation.id),
                {"schema": SCHEMA_VERSION, "relation": relation.to_dict()},
                text=f"{relation.source_id} {relation.predicate} {relation.target_label}",
                metadata={
                    "schema": SCHEMA_VERSION,
                    "relation_id": relation.id,
                    "source_id": relation.source_id,
                },
                importance=relation.confidence,
            )
            self.last_error = ""
            return True
        except Exception as exc:
            self.last_error = f"persistence failed: {type(exc).__name__}: {exc}"
            return False

    def _trim(self) -> None:
        if len(self.records) > self.max_records:
            keep = sorted(
                self.records.values(), key=lambda item: item.updated_at
            )[-self.max_records :]
            self.records = {item.id: item for item in keep}
        if len(self.relations) > self.max_relations:
            keep_relations = sorted(
                self.relations.values(), key=lambda item: item.updated_at
            )[-self.max_relations :]
            self.relations = {item.id: item for item in keep_relations}

    async def _emit(self, type_: str, **payload: Any) -> None:
        if self._publish is None:
            return
        try:
            produced = self._publish(type_, **payload)
            if inspect.isawaitable(produced):
                await produced
        except Exception:
            # Knowledge is stored even when an observer cannot be notified.
            return


def _terms_in(value: str, terms: set[str]) -> bool:
    return bool(_words(value) & terms)


def remember_explicit_text(text: str) -> tuple[str, KnowledgeCategory, KnowledgeKind] | None:
    """Extract only explicit "remember this:" notes from a request.

    This is the narrow automatic path. It never treats an arbitrary sentence as
    permanent memory: the user has to ask for it, exactly like the existing
    Phase 3 teach path.
    """
    clean = " ".join(str(text or "").split())
    for marker in ("remember this:", "remember that:", "remember:"):
        index = clean.lower().find(marker)
        if index < 0:
            continue
        prefix = clean[:index]
        if re.search(
            r"\b(?:do not|don't|dont)\b[^.!?]{0,40}$",
            prefix,
            re.IGNORECASE,
        ):
            # "please do not remember this: ..." is an instruction not to
            # acquire, not an explicit acquisition. Do not turn an opt-out into
            # a permanent record.
            continue
        content = clean[index + len(marker):].strip()
        if not content:
            return None
        lower = content.lower()
        if any(word in lower for word in ("prefer", "like ", "always ", "never ", "favorite", "concise")):
            return content, KnowledgeCategory.PREFERENCE, KnowledgeKind.PREFERENCE
        if any(word in lower for word in ("decided", "decision", "we will", "we should", "use ")):
            return content, KnowledgeCategory.DECISION, KnowledgeKind.DECISION
        if any(word in lower for word in ("goal", "want to", "plan to", "objective")):
            return content, KnowledgeCategory.GOAL, KnowledgeKind.FACT
        if "project" in lower:
            return content, KnowledgeCategory.ACTIVE_PROJECT, KnowledgeKind.FACT
        return content, KnowledgeCategory.FACT, KnowledgeKind.FACT
    return None
