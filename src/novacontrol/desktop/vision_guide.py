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
import os
import re
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


# Common JSON shapes a vision model uses for a 0-1000 grid location. The
# documented contract is {"found": true, "x": .., "y": ..}, but Qwen-class VL
# models also return a centre/point pair, axis-alias keys, or a bounding box.
# Accepting them costs a few lines and turns an answer that would otherwise be
# thrown away into a real click target.
_GRID_X_KEYS: tuple[str, ...] = ("x", "center_x", "centre_x", "cx", "left")
_GRID_Y_KEYS: tuple[str, ...] = ("y", "center_y", "centre_y", "cy", "top")
_GRID_POINT_KEYS: tuple[str, ...] = ("point", "center", "centre", "coordinates")
_GRID_BOX_KEYS: tuple[str, ...] = ("bbox", "box", "bounds", "rectangle")
_GRID_MIN = 0.0
_GRID_MAX = 1000.0

# A bare region word ("middle-right") is a legitimate legacy answer; a SENTENCE
# that merely mentions a direction is not. This bounds the prose length that may
# be read as a location, so "the top of the window shows nothing" cannot click
# the top edge of the screen.
_MAX_REGION_PHRASE = 40


def _grid_number(value: Any) -> float | None:
    """Coerce the shapes a model emits for one grid coordinate (500, 500.0, '500')."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if match is not None:
            return float(match.group(0))
    return None


def _grid_point(data: dict[str, Any]) -> tuple[float, float] | None:
    """Pull a 0-1000 grid point out of the JSON shapes models actually emit."""
    for key in _GRID_POINT_KEYS:
        value = data.get(key)
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            x, y = _grid_number(value[0]), _grid_number(value[1])
            if x is not None and y is not None:
                return x, y
    for key in _GRID_BOX_KEYS:
        value = data.get(key)
        if isinstance(value, (list, tuple)) and len(value) >= 4:
            x1, y1, x2, y2 = (_grid_number(v) for v in value[:4])
            if x1 is None or y1 is None or x2 is None or y2 is None:
                continue
            # The CENTRE of the detected box: a corner would click the edge.
            return (x1 + x2) / 2, (y1 + y2) / 2
    x_value = next((data[key] for key in _GRID_X_KEYS if key in data), None)
    y_value = next((data[key] for key in _GRID_Y_KEYS if key in data), None)
    x, y = _grid_number(x_value), _grid_number(y_value)
    if x is not None and y is not None:
        return x, y
    return None


def _interpret_llm_answer(answer: str, width: int, height: int) -> tuple[str, tuple[int, int] | None]:
    """Classify a vision model's locate answer into (status, pixel point).

    status is one of:

      ``"found"``       — the element was located; the point is its pixel centre.
      ``"absent"``      — the model explicitly reported it is not visible.
      ``"unparseable"`` — no usable JSON and no bare region phrase, so the
                          answer carries no location at all.

    "absent" is deliberately distinct from "unparseable": a model that already
    answered ``{"found": false}`` HAS answered, so only an unparseable reply is
    worth a stricter re-ask (a second slow inference would just re-confirm it).

    Coordinates arrive on a 0-1000 grid (resolution independent, the convention
    vision models are commonly trained on) and are mapped onto the ORIGINAL
    image size — so a capture downscaled to save tokens still produces correct
    full-resolution click points.
    """
    text = str(answer).strip()
    if not text:
        return "unparseable", None
    if "{" in text:
        match = re.search(r"\{.*\}", text, re.S)
        if match is not None:
            try:
                data = json.loads(match.group(0))
            except (json.JSONDecodeError, ValueError, TypeError):
                data = None
            if isinstance(data, dict):
                grid_point = _grid_point(data)
                if grid_point is not None and all(
                    _GRID_MIN <= value <= _GRID_MAX for value in grid_point
                ):
                    gx, gy = grid_point
                    return "found", (int(gx / 1000 * width), int(gy / 1000 * height))
                if data.get("found") is False:
                    return "absent", None
                if data.get("found") is True:
                    # Claimed a find but supplied no in-grid coordinate.
                    return "unparseable", None
    # Legacy coarse-region vocabulary, accepted only as a BARE phrase.
    if len(text) <= _MAX_REGION_PHRASE:
        region = _region_point(normalize(text), width, height)
        if region is not None:
            return "found", region
    return "unparseable", None


def _parse_llm_point(answer: str, width: int, height: int) -> tuple[int, int] | None:
    """Parse a vision model's locate answer into a pixel point (or None).

    Thin wrapper over _interpret_llm_answer for callers that only need the
    point: "found" yields the point, while "absent" and "unparseable" both
    yield None so the caller falls back to the next strategy (OCR, landmarks).
    """
    return _interpret_llm_answer(answer, width, height)[1]


# Reply budget for a LOCATE request, much smaller than a chat answer. Locating
# an element needs one JSON object (~25 tokens), so a model that has not
# answered within this budget is not going to: a reasoning-heavy one spends
# every token thinking (measured: 600 tokens, no answer) and an unbounded
# request hangs the caller until the socket timeout. Capping it means the
# locate layer fails fast and falls back to OCR instead of stalling.
_LOCATE_MAX_TOKENS_DEFAULT = 256


def _locate_max_tokens() -> int:
    """Reply budget for a locate request (NOVACONTROL_VISION_MAX_TOKENS)."""
    raw = os.environ.get(
        "NOVACONTROL_VISION_MAX_TOKENS", str(_LOCATE_MAX_TOKENS_DEFAULT)
    ).strip()
    try:
        return int(float(raw))
    except ValueError:
        return _LOCATE_MAX_TOKENS_DEFAULT


def _budget_exhausted(provider: Any) -> bool:
    """True when the provider's last reply ran out of tokens with no answer.

    Distinguishes "the model could not format its answer" (recoverable with a
    stricter prompt) from "the model never reached an answer at all": re-asking
    the second case would burn an identical budget for an identical result.
    """
    return bool(getattr(provider, "answer_was_truncated", False))


def _locate_messages(prompt: str, image_data: str) -> list[dict[str, Any]]:
    """OpenAI-format multimodal message carrying the screenshot IN the content.

    Providers drop unknown kwargs like ``image=``, so the picture must travel
    inside the message content or the model never sees it at all.
    """
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{image_data}",
                        "detail": "high",
                    },
                },
            ],
        }
    ]


async def _llm_region(provider: Any, image_path: str, label: str) -> tuple[int, int] | None:
    """Ask the vision model where the label is; returns a pixel point.

    Two accuracy measures live here beyond the prompt itself:

      * the capture is downscaled to a token budget before sending (see
        ``_load_image_base64_for_vision``) — coordinates stay correct because
        the answer is mapped onto the original dimensions read before encoding;
      * a reply with no usable location gets ONE stricter re-ask, because a
        model wrapping its JSON in prose is recoverable while OCR cannot name
        an unlabeled icon at all.
    """
    from novacontrol.vision.multimodal import _load_image_base64_for_vision

    if Image is None:  # pragma: no cover - Pillow is required for this layer
        return None
    try:
        with Image.open(image_path) as image:
            width, height = image.size
    except Exception:
        return None
    image_data = _load_image_base64_for_vision(image_path)
    if not image_data:
        return None
    prompt = (
        "You are locating a UI element in a screenshot for a computer-control agent. "
        f"Find the element labeled '{label}'. "
        "Answer with ONLY a JSON object: "
        '{"found": true, "x": <0-1000>, "y": <0-1000>} '
        "where x/y are the element's center on a 0-1000 grid across the whole "
        "image as shown (0,0 top-left, 1000,1000 bottom-right). "
        'If it is not visible answer {"found": false}. No other text.'
    )
    budget = _locate_max_tokens()
    try:
        answer = await provider.complete(
            _locate_messages(prompt, image_data),
            **({"max_tokens": budget} if budget > 0 else {}),
        )
    except Exception:
        return None
    status, point = _interpret_llm_answer(str(answer), width, height)
    if status == "unparseable" and not _budget_exhausted(provider):
        retry_prompt = (
            f"Locate the element labeled '{label}'. "
            "Reply with ONLY this JSON object, no prose and no code fences: "
            '{"found": true, "x": <0-1000>, "y": <0-1000>}. '
            'If the element is not visible reply {"found": false}.'
        )
        try:
            answer = await provider.complete(
                _locate_messages(retry_prompt, image_data),
                **({"max_tokens": budget} if budget > 0 else {}),
            )
        except Exception:
            return None
        status, point = _interpret_llm_answer(str(answer), width, height)
    return point if status == "found" else None


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
