"""Explore research domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class ExploreRequest:
    topic: str
    depth: str = "deep"
    include_videos: bool = True
    max_sources: int = 6
    max_videos: int = 5
    prior_topics: tuple[str, ...] = ()
    id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if not self.topic.strip():
            raise ValueError("Explore topic is required.")
        if self.max_sources < 1:
            raise ValueError("max_sources must be at least 1.")
        if self.max_videos < 0:
            raise ValueError("max_videos cannot be negative.")


@dataclass(frozen=True, slots=True)
class ResearchSource:
    title: str
    url: str
    snippet: str = ""
    source_type: str = "web"
    # The readable prose of the page itself, when PageReader could fetch it.
    # Deliberately NOT part of to_dict(): it is evidence for synthesis, and
    # shipping six pages of article text to the browser on every report would
    # bloat the response for a field the UI does not render.
    content: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source_type": self.source_type,
        }


@dataclass(frozen=True, slots=True)
class VideoResult:
    title: str
    url: str
    channel: str = ""
    duration: str = ""
    reason: str = ""
    thumbnail_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "channel": self.channel,
            "duration": self.duration,
            "reason": self.reason,
            "thumbnail_url": self.thumbnail_url,
        }


@dataclass(frozen=True, slots=True)
class ExploreReport:
    topic: str
    overview: str
    key_points: tuple[str, ...]
    detailed_explanation: str
    sources: tuple[ResearchSource, ...]
    videos: tuple[VideoResult, ...] = ()
    answer: str = ""
    answer_highlights: tuple[str, ...] = ()
    sections: tuple[dict[str, Any], ...] = ()
    source_chips: tuple[dict[str, Any], ...] = ()
    learning_path: tuple[str, ...] = ()
    follow_up_questions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    provider_status: str = "online"
    verification: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "topic": self.topic,
            "overview": self.overview,
            "key_points": self.key_points,
            "detailed_explanation": self.detailed_explanation,
            "sources": [source.to_dict() for source in self.sources],
            "videos": [video.to_dict() for video in self.videos],
            "answer": self.answer,
            "answer_highlights": self.answer_highlights,
            "sections": self.sections,
            "source_chips": self.source_chips,
            "learning_path": self.learning_path,
            "follow_up_questions": self.follow_up_questions,
            "warnings": self.warnings,
            "provider_status": self.provider_status,
            "verification": self.verification,
            "created_at": self.created_at.isoformat(),
        }
