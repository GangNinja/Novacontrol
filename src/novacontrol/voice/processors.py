"""Voice processor interfaces and safe baseline implementations."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from novacontrol.voice.models import SpeechTranscript, TtsRequest, TtsResult, WakeWordResult


@runtime_checkable
class SpeechRecognizer(Protocol):
    async def transcribe(self, source: str) -> SpeechTranscript:
        """Transcribe audio from a source."""


@runtime_checkable
class TextToSpeechEngine(Protocol):
    async def synthesize(self, request: TtsRequest) -> TtsResult:
        """Synthesize speech."""


@runtime_checkable
class WakeWordDetector(Protocol):
    async def detect(self, text_or_source: str) -> WakeWordResult:
        """Detect a wake word."""


class BasicSpeechRecognizer:
    """Dependency-free recognizer that reads text files or treats input as text."""

    async def transcribe(self, source: str) -> SpeechTranscript:
        path = Path(source)
        if path.exists() and path.is_file() and path.suffix.lower() in {".txt", ".md"}:
            text = path.read_text(encoding="utf-8")
            confidence = 0.95
        else:
            text = source
            confidence = 0.6
        return SpeechTranscript(text=text.strip(), confidence=confidence, source=source)


class BasicTextToSpeechEngine:
    """Safe TTS baseline that returns a speech intent instead of playing audio."""

    async def synthesize(self, request: TtsRequest) -> TtsResult:
        return TtsResult(
            text=request.text,
            audio_reference=None,
            metadata={
                "voice": request.voice,
                "interruptible": request.interruptible,
                "mode": "text-only",
            },
        )


class KeywordWakeWordDetector:
    """Simple wake word detector for text transcripts."""

    def __init__(self, wake_word: str = "nova") -> None:
        self.wake_word = wake_word.lower()

    async def detect(self, text_or_source: str) -> WakeWordResult:
        transcript = await BasicSpeechRecognizer().transcribe(text_or_source)
        text = transcript.text.lower()
        detected = self.wake_word in text.split() or text.startswith(self.wake_word)
        return WakeWordResult(
            detected=detected,
            wake_word=self.wake_word,
            confidence=0.9 if detected else 0.0,
            text=transcript.text,
        )
