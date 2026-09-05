from __future__ import annotations

import ast
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novacontrol.core.events import Event, EventBus
from novacontrol.voice import (
    BasicSpeechRecognizer,
    ConversationState,
    InterruptibleConversationManager,
    KeywordWakeWordDetector,
    TtsRequest,
    VoiceModule,
)


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_basic_recognizer_reads_text_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "speech.txt"
            path.write_text("hello nova", encoding="utf-8")

            transcript = await BasicSpeechRecognizer().transcribe(str(path))

            self.assertEqual(transcript.text, "hello nova")
            self.assertGreater(transcript.confidence, 0.9)

    async def test_wake_word_detector_finds_keyword(self) -> None:
        result = await KeywordWakeWordDetector("nova").detect("nova please help")

        self.assertTrue(result.detected)
        self.assertEqual(result.wake_word, "nova")

    async def test_conversation_can_be_interrupted(self) -> None:
        conversation = InterruptibleConversationManager()

        await conversation.speak(TtsRequest("This is a long answer."))
        await conversation.interrupt("stop")

        self.assertEqual(conversation.state, ConversationState.INTERRUPTED)
        self.assertTrue(conversation.turns[-1].metadata["interrupted"])

    async def test_voice_module_emits_events(self) -> None:
        from conftest import collect_events
        bus = EventBus()
        module = VoiceModule()
        seen = await collect_events(
            bus, "voice.speech_completed",
            "voice.speak_requested", {"text": "Hello"},
            start_fn=module.start,
        )
        self.assertEqual(seen[0].payload["text"], "Hello")
        self.assertEqual(seen[0].payload["metadata"]["mode"], "text-only")

# ---------------------------------------------------------------------------
# No-shell source contract: real_processors.py launches engines exec-form only
# ---------------------------------------------------------------------------

class VoiceNoShellInvariantTests(unittest.TestCase):
    """Guards the NO-SHELL + escaping invariant in voice/real_processors.py.

    Like the desktop controller guard: exec-form launches only (no shell=True /
    create_subprocess_shell / os.system), and the one shell-capable interpreter
    (PowerShell in _say_windows) still receives user text inside a single-quoted
    segment with quotes doubled — the voice analogue of shlex.split still being
    present where free text is handled.
    """

    def setUp(self) -> None:
        from novacontrol.voice import real_processors as voice_processors

        self.source = Path(voice_processors.__file__).read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_no_shell_launch_is_reintroduced(self) -> None:
        from conftest import shell_launch_offenders

        offenders = shell_launch_offenders(self.source)
        self.assertEqual(
            offenders, [], "Shell launch reintroduced in voice/real_processors.py: " + "; ".join(offenders)
        )

    def test_tts_text_is_quote_doubled_before_powershell(self) -> None:
        """_say_windows still escapes free text (' doubled to '') before Speak()."""
        fn = next(
            (
                n
                for n in ast.walk(self.tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_say_windows"
            ),
            None,
        )
        self.assertIsNotNone(fn, "_say_windows must exist")
        quote_doubled = any(
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "replace"
            and any(
                isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "chr"
                for arg in call.args
            )
            for call in ast.walk(fn)
            if isinstance(call, ast.Call)
        )
        self.assertTrue(
            quote_doubled,
            "_say_windows must escape text via text.replace(chr(39), chr(39)+chr(39)) before Speak()",
        )


if __name__ == "__main__":
    unittest.main()
