"""Vision-guided element location for desktop automation.

Given a screenshot and a spoken label ("the Library button", "File"), locate
the element on screen. Layered strategy:

  1. LLM vision (when a real provider is wired): the model names the region,
     and the phrase (e.g. "middle-right") is converted to an approximate point.
  2. OCR/text extraction + phrase matching: find the label's text on screen and
     click its location.
  3. Heuristic UI landmarks: common menu/button positions (menu bar strip,
     top-left rail) when OCR is unavailable.

Returns an (x, y, how) tuple, or None when the element cannot be located —
the caller must fail honestly instead of clicking blind coordinates.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from novacontrol.integrations.llm import provider_supports_vision
from novacontrol.intelligence.normalize import normalize

try:  # Pillow is optional at import time; degrade gracefully.
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]


def _region_point(region: str, width: int, height: int) -> tuple[int, int] | None:
    """Convert an LLM region phrase ('middle-right', 'top left') to a point."""
    text = region.lower()
    horizontal = 0.5
    vertical = 0.5
    if "left" in text:
        horizontal = 0.18
    elif "right" in text:
        horizontal = 0.82
    if "top" in text:
        vertical = 0.12
    elif "bottom" in text:
        vertical = 0.88
    if horizontal == 0.5 and vertical == 0.5 and not any(
        word in text for word in ("center", "middle")
    ):
        return None
    return int(width * horizontal), int(height * vertical)


def _extract_words(image: Any) -> list[tuple[str, str, int, int]]:
    """Crude on-image word extraction: dark-glyph pixel clusters on light UI.

    Scans the top band (menu bars, Steam-style nav rails) and merges adjacent
    glyph runs on the same row into word candidates. Glyph shapes are NOT read
    (that needs a real OCR engine at this seam), but run WIDTH and position
    let us rank candidates against the label's shape: 'File' (4 chars) is a
    short run near the left edge; 'LIBRARY' is a wider run further right.
    """
    grayscale = image.convert("L")
    width, height = grayscale.size
    pixels: Any = grayscale.load()
    runs: list[tuple[int, int, int]] = []  # (x_start, x_end, y)
    band_height = max(8, int(height * 0.12))
    for row in range(0, band_height):
        run_start: int | None = None
        for col in range(width):
            dark = int(pixels[col, row]) < 110
            if dark and run_start is None:
                run_start = col
            elif not dark and run_start is not None:
                if col - run_start >= 2:
                    runs.append((run_start, col, row))
                run_start = None
    # Merge runs on the same row separated by small gaps (letter spacing).
    words: list[tuple[str, str, int, int]] = []
    used = [False] * len(runs)
    for i, (x0, x1, y) in enumerate(runs):
        if used[i]:
            continue
        merged_x1 = x1
        merged_y = y
        for j in range(i + 1, len(runs)):
            if used[j]:
                continue
            jx0, jx1, jy = runs[j]
            if abs(jy - y) <= 3 and 0 <= jx0 - merged_x1 <= 12:
                merged_x1 = jx1
                used[j] = True
        used[i] = True
        width_px = merged_x1 - x0
        if width_px >= 8:  # ignore specks
            # Approximate letter count from pixel width (a glyph is ~6-9 px
            # at typical menu font sizes) for label-shape matching.
            approx_chars = max(2, round(width_px / 7.5))
            words.append((f"w{approx_chars}", "?" * approx_chars, x0, merged_y))
    return words


# Windows.Media.Ocr — the OS-built OCR engine. This is what makes vision
# work WITHOUT any LLM wired: real text recognition with real coordinates,
# replacing the old pixel-run guessing for every common label.
_OCR_POWERSHELL_PREFIX = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
Function Await($WinRtTask, $ResultType) {
  $t = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($WinRtTask))
  $t.Wait(-1) | Out-Null
  $t.Result
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('en-US')) }
if (-not $engine) { Write-Output '[]'; exit 0 }
"""

_OCR_POWERSHELL_BODY = """
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$words = @()
$lineIndex = 0
foreach ($line in $result.Lines) {
  $order = 0
  foreach ($word in $line.Words) {
    $r = $word.BoundingRect
    $words += @{ text = $word.Text; line = $lineIndex; order = $order; x = [int]$r.X; y = [int]$r.Y; w = [int]$r.Width; h = [int]$r.Height }
    $order++
  }
  $lineIndex++
}
Write-Output (@($words) | ConvertTo-Json -Compress)
"""


async def _ocr_words(image_path: str) -> list[dict[str, Any]] | None:
    """OCR the screenshot with Windows' built-in engine; None when unavailable.

    Returns word records {text, line, order, x, y, w, h} — real recognized text
    with real screen coordinates. Any failure (non-Windows, no language pack,
    timeout) degrades to None so callers fall back to weaker strategies.
    """
    if not sys.platform.startswith("win"):
        return None
    # GetFileFromPathAsync demands an ABSOLUTE path — a relative one raises
    # and the whole OCR call degrades to None.
    from pathlib import Path

    absolute = str(Path(image_path).resolve())
    script = _OCR_POWERSHELL_PREFIX + f"$path = '{absolute}'\n" + _OCR_POWERSHELL_BODY
    try:
        process = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-Command", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=25)
        # strict=False: OCR'd words can carry literal control characters and
        # PowerShell's ConvertTo-Json does not escape them — strict parsing
        # would reject the whole payload (a real bug this caught live).
        data = json.loads(stdout.decode("utf-8", errors="replace").strip() or "[]", strict=False)
    except Exception:
        return None
    if isinstance(data, dict):  # single-word JSON comes back unwrapped
        data = [data]
    return data or None


def _word_center(word: dict[str, Any]) -> tuple[int, int]:
    return int(word["x"]) + int(word["w"]) // 2, int(word["y"]) + int(word["h"]) // 2


def _span_center(words: list[dict[str, Any]]) -> tuple[int, int]:
    left = min(int(w["x"]) for w in words)
    top = min(int(w["y"]) for w in words)
    right = max(int(w["x"]) + int(w["w"]) for w in words)
    bottom = max(int(w["y"]) + int(w["h"]) for w in words)
    return (left + right) // 2, (top + bottom) // 2


def _match_ocr_words(words: list[dict[str, Any]], label: str) -> tuple[int, int] | None:
    """Match a spoken label against OCR'd words; click-center of the match.

    Three passes, strictest first: an exact consecutive-word phrase on one
    line ("play gta v" ↔ "PLAY GTA V"), an exact single word ("Library"),
    then a word containing the label ("PLAY" inside the line "PLAY GTA V").
    All comparisons run through the GIL normalizer so case/punctuation never
    block a match.
    """
    target = normalize(label).strip()
    if not target or not words:
        return None
    lines: dict[int, list[dict[str, Any]]] = {}
    for word in words:
        lines.setdefault(int(word.get("line", 0)), []).append(word)
    ordered = [lines[key] for key in sorted(lines)]
    # 1) exact phrase of consecutive words on one line
    for line_words in ordered:
        for start in range(len(line_words)):
            for end in range(start + 1, len(line_words) + 1):
                span = line_words[start:end]
                joined = normalize(" ".join(str(w["text"]) for w in span)).strip()
                if joined == target:
                    return _span_center(span)
    # 2) exact single word
    for line_words in ordered:
        for word in line_words:
            if normalize(str(word["text"])).strip() == target:
                return _word_center(word)
    # 3) a single word containing the label (short label inside a longer word)
    for line_words in ordered:
        for word in line_words:
            if target in normalize(str(word["text"])):
                return _word_center(word)
    return None


def _heuristic_landmark(label: str, width: int, height: int) -> tuple[int, int] | None:
    """Common UI landmarks for well-known labels."""
    normalized = label.strip().lower()
    # Menu bar: 'click file' -> the File menu is the first item of the menu bar.
    menu_items = ["file", "edit", "view", "insert", "format", "tools", "help", "window"]
    for index, name in enumerate(menu_items):
        if normalized == name or normalized.startswith(f"{name} "):
            x = int(width * (0.035 + index * 0.045))
            return x, int(height * 0.045)
    if normalized in ("library", "store", "news", "community"):
        # Steam-style top nav: LIBRARY is the 3rd of five tabs.
        order = {"store": 0, "library": 1, "news": 2, "community": 3, "home": 0}
        index = order.get(normalized, 1)
        return int(width * (0.10 + index * 0.06)), int(height * 0.115)
    if "play" in normalized or "launch" in normalized:
        # Steam library play button: top of the selected game's page,
        # left-of-center under the nav rail (NOT mid-screen).
        return int(width * 0.33), int(height * 0.105)
    return None


def _parse_llm_point(answer: str, width: int, height: int) -> tuple[int, int] | None:
    """Parse a vision model's locate answer into a pixel point.

    Accepts the JSON contract {"found": bool, "x": 0-1000, "y": 0-1000} with
    coordinates on a normalized 0-1000 grid (resolution-independent, the same
    convention vision models are commonly RL-trained on), and degrades to the
    legacy coarse-region vocabulary ("middle-right") when the JSON is absent.
    Anything else — including coordinate-free region words that _region_point
    cannot resolve — returns None so the caller falls back to OCR.
    """
    text = str(answer).strip()
    if not text:
        return None
    if "{" in text:
        import json as _json
        import re as _re

        match = _re.search(r"\{.*\}", text, _re.S)
        if match:
            try:
                data = _json.loads(match.group(0))
                if isinstance(data, dict) and data.get("found") is True:
                    x_raw, y_raw = data.get("x"), data.get("y")
                    if isinstance(x_raw, (int, float)) and isinstance(y_raw, (int, float)):
                        if 0 <= x_raw <= 1000 and 0 <= y_raw <= 1000:
                            return int(x_raw / 1000 * width), int(y_raw / 1000 * height)
            except (_json.JSONDecodeError, ValueError, TypeError):
                pass  # malformed JSON: fall through to region vocabulary
    return _region_point(normalize(text), width, height)


async def _llm_region(provider: Any, image_path: str, label: str) -> tuple[int, int] | None:
    """Ask the vision model where the label is; returns a pixel point.

    The screenshot is embedded in the message as OpenAI-format multimodal
    content — providers drop unknown kwargs like `image=`, so it MUST travel
    inside the message content or the model never sees the picture. The answer
    is parsed by _parse_llm_point (JSON 0-1000 grid, region fallback).
    """
    from novacontrol.vision.multimodal import _load_image_base64

    image_data = _load_image_base64(image_path)
    if not image_data:
        return None
    prompt = (
        "You are locating a UI element in a screenshot for a computer-control agent. "
        f"Find the element labeled '{label}'. "
        "Answer with ONLY a JSON object: "
        '{"found": true, "x": <0-1000>, "y": <0-1000>} '
        "where x/y are the element's center on a 0-1000 grid across the whole "
        "image (0,0 top-left, 1000,1000 bottom-right). "
        'If it is not visible answer {"found": false}. No other text.'
    )
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_data}", "detail": "high"},
                },
            ],
        }
    ]
    try:
        answer = await provider.complete(messages)
    except Exception:
        return None
    from PIL import Image as PILImage

    with PILImage.open(image_path) as img:
        return _parse_llm_point(str(answer), img.width, img.height)


async def locate_element(
    screenshot_path: str, label: str, *, llm_provider: Any | None = None
) -> tuple[int, int, str] | None:
    """Locate `label` in the screenshot. Returns (x, y, how) or None."""
    if Image is None:  # pragma: no cover
        return None

    # Only a REAL multimodal provider may participate in location. The Echo
    # fallback "answers" by echoing the prompt back — and the prompt embeds the
    # whole screenshot as base64 plus the region vocabulary itself, so the echo
    # always contains words like 'top-left' and can never carry real screen
    # understanding (the exact trap vision.py's has_vision_model gates against;
    # this runner-side gate closes the same hole for locating). "Not Echo" is
    # not enough either: a text-only local model (qwen3, llama3.2) accepts the
    # request, cannot see the image, and answers with invented coordinates that
    # this layer would then click — provider_supports_vision asks the provider
    # for real capability metadata instead.
    if provider_supports_vision(llm_provider):
        point = await _llm_region(llm_provider, screenshot_path, label)
        if point is not None:
            return point[0], point[1], "llm_vision"

    # Real OCR next: recognized text beats both geometric landmarks and the
    # pixel-run band, because it knows WHAT the words say, not just where
    # dark pixels cluster.
    ocr = await _ocr_words(screenshot_path)
    if ocr:
        match = _match_ocr_words(ocr, label)
        if match is not None:
            return match[0], match[1], "ocr"

    loop = asyncio.get_running_loop()

    def _sync_locate() -> tuple[int, int, str] | None:
        try:
            with Image.open(screenshot_path) as img:
                width, height = img.size
                landmark = _heuristic_landmark(label, width, height)
                if landmark is not None:
                    return landmark[0], landmark[1], "ui_landmark"
                # OCR band: pick the glyph-run candidate whose approximate
                # width best matches the label — NOT just the first run, which
                # would click the leftmost word whatever it says.
                target_len = len(label.strip())
                best = None
                best_diff = 10**9
                for _tag, approx, x, y in _extract_words(img):
                    diff = abs(approx_len(approx) - target_len)
                    if diff < best_diff:
                        best_diff = diff
                        best = (x, y)
                if best is not None and best_diff <= max(3, target_len // 2):
                    return best[0] + 10, best[1] + 8, "ocr_band"
        except Exception:
            return None
        return None

    def approx_len(mask: str) -> int:
        return len(mask)

    return await loop.run_in_executor(None, _sync_locate)
