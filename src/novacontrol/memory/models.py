"""Memory domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import uuid4


class MemoryNamespace(StrEnum):
    CONVERSATION = "conversation"
    PROJECT = "project"
    KNOWLEDGE = "knowledge"
    SHORT_TERM = "short_term"
    LONG_TERM = "long_term"


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    namespace: str
    key: str
    value: MappingProxyType[str, Any] | dict[str, Any]
    text: str = ""
    metadata: MappingProxyType[str, Any] | dict[str, Any] = field(default_factory=dict)
    importance: float = 0.0
    id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.namespace:
            raise ValueError("namespace is required.")
        if not self.key:
            raise ValueError("key is required.")
        if not isinstance(self.value, MappingProxyType):
            object.__setattr__(self, "value", MappingProxyType(dict(self.value)))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        if self.importance < 0:
            raise ValueError("importance cannot be negative.")

    @classmethod
    def create(
        cls,
        *,
        namespace: str,
        key: str,
        value: dict[str, Any],
        text: str,
        metadata: dict[str, Any] | None = None,
        importance: float = 0.0,
        expires_at: datetime | None = None,
    ) -> "MemoryRecord":
        now = datetime.now(UTC)
        return cls(
            namespace=namespace,
            key=key,
            value=value,
            text=text,
            metadata=metadata or {},
            importance=importance,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= (now or datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "namespace": self.namespace,
            "key": self.key,
            "value": dict(self.value),
            "text": self.text,
            "metadata": dict(self.metadata),
            "importance": self.importance,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    namespace: str
    query: str
    limit: int = 10

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit must be at least 1.")


@dataclass(frozen=True, slots=True)
class MemorySearchResult:
    record: MemoryRecord
    score: float
