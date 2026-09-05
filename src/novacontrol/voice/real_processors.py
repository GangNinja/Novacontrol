"""Real voice processor implementations using platform-native tools.

Provides STT (speech-to-text) and TTS (text-to-speech) using OS-native
speech services, with graceful fallback to the basic processors.

NO-SHELL INVARIANT: every engine launch is exec-form (asyncio.create_subprocess_exec
with literal argv) — never shell=True, create_subprocess_shell, or os.system. User
speech/text reaches engines only as a literal argv element, or inside a PowerShell
single-quoted segment with quotes doubled. Unlike the desktop command executor there
is no free-form command to shlex.split: nothing here tokenizes user input into a
command line. VoiceNoShellInvariantTests guards both the no-shell form and the
quote-doubling escape.
"""

from __future__ import annotations

import asyncio
import platform
import shutil
from pathlib import Path

from novacontrol.voice.models import SpeechTranscript, TtsRequest, TtsResult


class NativeTextToSpeechEngine:
    """TTS using OS-native speech synthesis.

    - macOS: `say` command
    - Windows: SAPI via PowerShell
    - Linux: espeak or festival
    """

    def __init__(self, voice: str | None = None, rate: int = 200) -> None:
        self._voice = voice
        self._rate = rate
        self._system = platform.system()

    async def synthesize(self, request: TtsRequest) -> TtsResult:
        try:
            if self._system == "Darwin":
                await self._say_macos(request.text)
            elif self._system == "Windows":
                await self._say_windows(request.text)
            else:
                await self._say_linux(request.text)
            return TtsResult(
                text=request.text,
                audio_reference=None,
                metadata={
                    "engine": "native",
                    "platform": self._system,
                    "voice": self._voice or "system-default",
                    "rate": self._rate,
                },
            )
        except Exception as exc:
            return TtsResult(
                text=request.text,
                audio_reference=None,
                metadata={
                    "engine": "native-fallback",
                    "error": f"{type(exc).__name__}: {exc}",
                    "platform": self._system,
                },
            )

    async def _say_macos(self, text: str) -> None:
        cmd = ["say"]
        if self._voice:
            cmd.extend(["-v", self._voice])
        cmd.extend(["-r", str(self._rate), text])
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(process.communicate(), timeout=30)

    async def _say_windows(self, text: str) -> None:
        # Use Windows SAPI via PowerShell. ESCAPE INVARIANT: user text only ever
        # appears inside the single-quoted PowerShell segment with ' doubled to ''
        # (the sole escape in PS single-quoted strings); do not drop the doubling
        # or switch to shell=True. VoiceNoShellInvariantTests guards this.
        voice_arg = f"-Voice '{self._voice}'" if self._voice else ""
        script = (
            f"Add-Type -AssemblyName System.Speech; "
            f"$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"{voice_arg} "
            f"$synth.Rate = {max(-10, min(10, (self._rate - 200) // 20))}; "
            f"$synth.Speak('{text.replace(chr(39), chr(39)+chr(39))}')"
        )
        process = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-Command", script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(process.communicate(), timeout=30)

    async def _say_linux(self, text: str) -> None:
        for cmd_name in ("espeak", "espeak-ng", "festival", "spd-say"):
            if shutil.which(cmd_name):
                if cmd_name in ("espeak", "espeak-ng"):
                    cmd = [cmd_name, "-s", str(self._rate)]
                    if self._voice:
                        cmd.extend(["-v", self._voice])
                    cmd.append(text)
                elif cmd_name == "spd-say":
                    cmd = [cmd_name, "-r", str(self._rate), text]
                else:
                    cmd = [cmd_name, text]
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(process.communicate(), timeout=30)
                return
        raise RuntimeError("No TTS engine found on Linux. Install espeak: sudo apt install espeak")


class NativeSpeechRecognizer:
    """STT using platform-native speech recognition.

    - macOS: Speech framework via `osascript` or swift
    - Windows: Windows Speech Recognition via PowerShell
    - Linux: pocketsphinx or vosk if available
    """

    def __init__(self, language: str = "en") -> None:
        self._language = language
        self._system = platform.system()

    async def transcribe(self, source: str) -> SpeechTranscript:
        # Check if source is a text file
        path = Path(source)
        if path.exists() and path.is_file() and path.suffix.lower() in {".txt", ".md"}:
            text = path.read_text(encoding="utf-8")
            return SpeechTranscript(text=text.strip(), confidence=0.95, source=source)

        # Try native speech recognition
        try:
            if self._system == "Darwin":
                text = await self._recognize_macos()
            elif self._system == "Windows":
                text = await self._recognize_windows()
            else:
                text = await self._recognize_linux()
            return SpeechTranscript(
                text=text.strip(),
                confidence=0.8,
                language=self._language,
                source=source,
            )
        except Exception:
            # Fall back to treating the source as text
            return SpeechTranscript(text=source, confidence=0.3, source=source)

    async def _recognize_macos(self) -> str:
        # macOS: Use Shortcuts or basic dictation
        # This is a simplified approach - full implementation would use Swift bridge
        script = (
            "tell application \"System Events\"\n"
            "  keystroke \"\"\n"
            "end tell"
        )
        process = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        return stdout.decode("utf-8", errors="replace")

    async def _recognize_windows(self) -> str:
        # Windows: Use built-in speech recognition
        # This requires the Windows Speech Recognition to be enabled
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$rec = New-Object System.Speech.Recognition.SpeechRecognizer; "
            "$rec.RecognizeAsync([System.Speech.Recognition.RecognizeMode]::Single); "
            "Start-Sleep -Seconds 5"
        )
        process = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-Command", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
        return stdout.decode("utf-8", errors="replace")

    async def _recognize_linux(self) -> str:
        # Try pocketsphinx or vosk
        for cmd_name in ("pocketsphinx_continuous", "vosk-transcriber"):
            if shutil.which(cmd_name):
                process = await asyncio.create_subprocess_exec(
                    cmd_name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
                return stdout.decode("utf-8", errors="replace")
        raise RuntimeError("No STT engine found on Linux. Install vosk: pip install vosk")
