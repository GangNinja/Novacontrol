# Voice

The Phase 10 voice subsystem defines speech recognition, text-to-speech, wake word detection, and interruptible conversation boundaries.

## Components

- `SpeechTranscript`: transcribed text, confidence, language, and source
- `TtsRequest`: speech synthesis request
- `TtsResult`: speech synthesis result or safe speech intent
- `WakeWordResult`: wake word detection output
- `ConversationState`: idle, listening, thinking, speaking, or interrupted
- `SpeechRecognizer`: recognizer adapter interface
- `TextToSpeechEngine`: TTS adapter interface
- `WakeWordDetector`: wake word adapter interface
- `BasicSpeechRecognizer`: dependency-free text/file baseline
- `BasicTextToSpeechEngine`: safe text-only TTS baseline
- `KeywordWakeWordDetector`: deterministic wake word detector
- `InterruptibleConversationManager`: tracks voice turns and interruptions
- `VoiceModule`: event-driven runtime module

## Events

- `voice.transcription_requested`
- `voice.transcription_completed`
- `voice.speak_requested`
- `voice.speech_completed`
- `voice.wake_word_requested`
- `voice.wake_word_completed`
- `voice.interrupt_requested`
- `voice.interrupted`

## CLI

```powershell
python -m novacontrol demo phase10
```

## Future Adapters

Whisper, system microphones, wake word engines, and local or cloud TTS engines can implement the adapter interfaces without changing the runtime.
