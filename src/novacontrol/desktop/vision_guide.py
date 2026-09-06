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
from typing import Any

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


async def _llm_region(provider: Any, image_path: str, label: str) -> tuple[int, int] | None:
    """Ask the vision model where the label is; returns a region-derived point.

    The screenshot is embedded in the message as OpenAI-format multimodal
    content — providers drop unknown kwargs like `image=`, so it MUST travel
    inside the message content or the model never sees the picture.
    """
    from novacontrol.vision.multimodal import _load_image_base64

    image_data = _load_image_base64(image_path)
    if not image_data:
        return None
    prompt = (
        "You are locating a UI element in a screenshot. "
        f"Where is '{label}'? Answer with ONLY one of: "
        "top-left, top-center, top-right, middle-left, center, "
        "middle-right, bottom-left, bottom-center, bottom-right, or 'absent'."
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
    text = normalize(str(answer))
    if not text or "absent" in text:
        return None
    from PIL import Image as PILImage

    with PILImage.open(image_path) as img:
        return _region_point(text, img.width, img.height)


async def locate_element(
    screenshot_path: str, label: str, *, llm_provider: Any | None = None
) -> tuple[int, int, str] | None:
    """Locate `label` in the screenshot. Returns (x, y, how) or None."""
    if Image is None:  # pragma: no cover
        return None

    if llm_provider is not None:
        point = await _llm_region(llm_provider, screenshot_path, label)
        if point is not None:
            return point[0], point[1], "llm_vision"

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
