"""Voice domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class VoiceEventType(StrEnum):
    TRANSCRIPTION = "transcription"
    SPEECH = "speech"
    WAKE_WORD = "wake_word"
    INTERRUPT = "interrupt"


class ConversationState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class SpeechTranscript:
    text: str
    confidence: float = 0.0
    language: str = "en"
    source: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "confidence": self.confidence,
            "language": self.language,
            "source": self.source,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class TtsRequest:
    text: str
    voice: str = "default"
    interruptible: bool = True
    id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("TTS text is required.")


@dataclass(frozen=True, slots=True)
class TtsResult:
    text: str
    audio_reference: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "audio_reference": self.audio_reference,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class WakeWordResult:
    detected: bool
    wake_word: str
    confidence: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "detected": self.detected,
            "wake_word": self.wake_word,
            "confidence": self.confidence,
            "text": self.text,
        }
