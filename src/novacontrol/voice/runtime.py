"""Event-driven voice runtime module."""

from __future__ import annotations

from novacontrol.core.events import Event, EventBus
from novacontrol.core.interfaces import Capability
from novacontrol.voice.conversation import InterruptibleConversationManager
from novacontrol.voice.models import TtsRequest
from novacontrol.voice.processors import (
    BasicSpeechRecognizer,
    KeywordWakeWordDetector,
    SpeechRecognizer,
    WakeWordDetector,
)


class VoiceModule:
    """Runtime module for speech recognition, TTS, wake word, and interrupts."""

    def __init__(
        self,
        *,
        recognizer: SpeechRecognizer | None = None,
        wake_word_detector: WakeWordDetector | None = None,
        tts_engine: object | None = None,
        conversation: InterruptibleConversationManager | None = None,
    ) -> None:
        self.recognizer = recognizer or BasicSpeechRecognizer()
        self.wake_word_detector = wake_word_detector or KeywordWakeWordDetector()
        self._tts_engine = tts_engine
        self.conversation = conversation or InterruptibleConversationManager()
        self._event_bus: EventBus | None = None

    @property
    def name(self) -> str:
        return "voice"

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability("voice.transcribe", "Transcribe speech."),
            Capability("voice.speak", "Synthesize speech."),
            Capability("voice.wake_word", "Detect wake words."),
            Capability("voice.interrupt", "Interrupt voice output."),
        )

    async def start(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        await event_bus.subscribe("voice.transcription_requested", self._handle_transcription)
        await event_bus.subscribe("voice.speak_requested", self._handle_speak)
        await event_bus.subscribe("voice.wake_word_requested", self._handle_wake_word)
        await event_bus.subscribe("voice.interrupt_requested", self._handle_interrupt)

    async def stop(self) -> None:
        self._event_bus = None

    async def _handle_transcription(self, event: Event) -> None:
        transcript = await self.recognizer.transcribe(str(event.payload["source"]))
        await self.conversation.receive(transcript)
        await self._publish("voice.transcription_completed", transcript.to_dict(), event)

    async def _handle_speak(self, event: Event) -> None:
        # Use real TTS engine if available, otherwise fall back to conversation manager
        if self._tts_engine is not None:
            from novacontrol.voice.processors import TextToSpeechEngine
            if isinstance(self._tts_engine, TextToSpeechEngine):
                result = await self._tts_engine.synthesize(
                    TtsRequest(
                        text=str(event.payload["text"]),
                        voice=str(event.payload.get("voice", "default")),
                        interruptible=bool(event.payload.get("interruptible", True)),
                    )
                )
                await self._publish("voice.speech_completed", result.to_dict(), event)
                return
        result = await self.conversation.speak(
            TtsRequest(
                text=str(event.payload["text"]),
                voice=str(event.payload.get("voice", "default")),
                interruptible=bool(event.payload.get("interruptible", True)),
            )
        )
        await self._publish("voice.speech_completed", result.to_dict(), event)

    async def _handle_wake_word(self, event: Event) -> None:
        result = await self.wake_word_detector.detect(str(event.payload["source"]))
        await self._publish("voice.wake_word_completed", result.to_dict(), event)

    async def _handle_interrupt(self, event: Event) -> None:
        await self.conversation.interrupt(str(event.payload.get("reason", "User interrupted.")))
        await self._publish(
            "voice.interrupted",
            {"state": self.conversation.state.value},
            event,
        )

    async def _publish(self, event_type: str, payload: dict[str, object], source_event: Event) -> None:
        if self._event_bus is not None:
            await self._event_bus.publish(
                Event(
                    type=event_type,
                    payload=payload,
                    source="voice",
                    correlation_id=source_event.correlation_id,
                    causation_id=source_event.correlation_id,
                )
            )
