"""Interruptible voice conversation state management."""

from __future__ import annotations

from novacontrol.voice.models import ConversationState, SpeechTranscript, TtsRequest, TtsResult
from novacontrol.voice.processors import BasicTextToSpeechEngine, TextToSpeechEngine


class InterruptibleConversationManager:
    """Tracks a voice conversation and supports interruption."""

    def __init__(self, *, tts_engine: TextToSpeechEngine | None = None) -> None:
        self.tts_engine = tts_engine or BasicTextToSpeechEngine()
        self.state = ConversationState.IDLE
        self.turns: list[SpeechTranscript | TtsResult] = []

    async def receive(self, transcript: SpeechTranscript) -> None:
        self.state = ConversationState.LISTENING
        self.turns.append(transcript)

    async def speak(self, request: TtsRequest) -> TtsResult:
        self.state = ConversationState.SPEAKING
        result = await self.tts_engine.synthesize(request)
        self.turns.append(result)
        if self.state is not ConversationState.INTERRUPTED:
            self.state = ConversationState.IDLE
        return result

    async def interrupt(self, reason: str = "User interrupted.") -> None:
        self.state = ConversationState.INTERRUPTED
        self.turns.append(
            TtsResult(
                text="",
                audio_reference=None,
                metadata={"interrupted": True, "reason": reason},
            )
        )

    async def reset(self) -> None:
        self.state = ConversationState.IDLE
