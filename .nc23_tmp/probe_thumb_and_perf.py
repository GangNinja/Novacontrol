"""NF-23: does the worker's paint side car agree with the frame, and what does it save?

Read-only with respect to the desktop: one display capture and one window capture
of this console's own window. No game is launched, no input is sent, nothing is
focused or moved.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image  # noqa: E402

from novacontrol.desktop.capture import (  # noqa: E402
    THUMB_SUFFIX,
    PersistentScreenCapture,
    sweep_orphan_thumbnails,
)
from novacontrol.desktop.capture_validation import (  # noqa: E402
    THUMB_BYTES,
    THUMB_HEIGHT,
    THUMB_WIDTH,
    inspect_frame,
)

OUT = ROOT / ".nc23_tmp" / "out"


def channel_means_bgr(path: Path) -> tuple[float, float, float] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) != THUMB_BYTES:
        return None
    red = sum(data[index + 2] for index in range(0, len(data), 3)) / (THUMB_BYTES // 3)
    green = sum(data[index + 1] for index in range(0, len(data), 3)) / (THUMB_BYTES // 3)
    blue = sum(data[index] for index in range(0, len(data), 3)) / (THUMB_BYTES // 3)
    return red, green, blue


def reference_means(path: Path) -> tuple[float, float, float]:
    with Image.open(path) as image:
        reduced = image.convert("RGB").resize((THUMB_WIDTH, THUMB_HEIGHT), Image.BICUBIC)
    pixels = list(reduced.getdata())
    total = len(pixels)
    return (
        sum(p[0] for p in pixels) / total,
        sum(p[1] for p in pixels) / total,
        sum(p[2] for p in pixels) / total,
    )


def timed(fn, runs: int) -> dict[str, float]:
    samples: list[float] = []
    for _ in range(runs):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    return {
        "runs": runs,
        "min_ms": round(min(samples), 2),
        "median_ms": round(statistics.median(samples), 2),
        "max_ms": round(max(samples), 2),
    }


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    worker = PersistentScreenCapture(startup_timeout=60.0, frame_timeout=60.0)
    if not await worker.ensure_started():
        print(json.dumps({"error": worker.last_error}))
        return 1
    print(json.dumps({"worker_ready": True, "screen": list(worker.size), "spawns": worker.spawns}))

    frame = OUT / "display.png"
    plain = await worker.capture(str(frame))
    print(json.dumps({"plain_ok": plain.ok, "ms": plain.elapsed_ms, "bytes": plain.size_bytes}))

    thumbed = OUT / "display_thumb.png"
    with_thumb = await worker.capture(str(thumbed), thumbnail=True)
    thumb_path = Path(with_thumb.thumbnail)
    print(
        json.dumps(
            {
                "thumb_ok": with_thumb.ok,
                "ms": with_thumb.elapsed_ms,
                "bytes": with_thumb.size_bytes,
                "thumbnail": str(thumb_path),
                "thumbnail_exists": thumb_path.exists(),
                "thumbnail_bytes": thumb_path.stat().st_size if thumb_path.exists() else 0,
                "expected_thumbnail_bytes": THUMB_BYTES,
            }
        )
    )

    # Correctness: the side car must describe the same pixels, in the order the
    # reader assumes. A swapped B/R would show up as the reference's red/blue
    # means landing on the side car's blue/red.
    side = channel_means_bgr(thumb_path)
    reference = reference_means(thumbed)
    print(
        json.dumps(
            {
                "reference_rgb_means": [round(value, 2) for value in reference],
                "side_car_bgr_as_rgb_means": (
                    [round(value, 2) for value in side] if side else None
                ),
                "per_channel_delta": (
                    [round(abs(a - b), 2) for a, b in zip(side, reference)] if side else None
                ),
            }
        )
    )

    full_inspection = inspect_frame(thumbed)
    thumb_inspection = inspect_frame(thumbed, thumbnail=thumb_path)
    print(
        json.dumps(
            {
                "full": {
                    "width": full_inspection.width,
                    "height": full_inspection.height,
                    "blank": full_inspection.blank,
                    "mean_luma": full_inspection.mean_luma,
                    "non_black_ratio": full_inspection.non_black_ratio,
                    "uniform_ratio": full_inspection.uniform_ratio,
                },
                "side_car": {
                    "width": thumb_inspection.width,
                    "height": thumb_inspection.height,
                    "blank": thumb_inspection.blank,
                    "mean_luma": thumb_inspection.mean_luma,
                    "non_black_ratio": thumb_inspection.non_black_ratio,
                    "uniform_ratio": thumb_inspection.uniform_ratio,
                },
                "verdict_agrees": full_inspection.blank == thumb_inspection.blank,
            }
        )
    )

    # Cost: full decode vs side car on the same frame.
    full_time = timed(lambda: inspect_frame(thumbed), 5)
    thumb_time = timed(lambda: inspect_frame(thumbed, thumbnail=thumb_path), 5)
    print(json.dumps({"inspect_full_decode": full_time, "inspect_side_car": thumb_time}))

    # A window capture with a side car (this console's own window, found by title).
    found = await worker.send_input("find Freebuff")
    detail = getattr(found, "detail", "") or ""
    handle = 0
    if detail and detail != "none":
        handle = int(detail.split("|")[0])
    print(json.dumps({"window_found": bool(handle), "detail": detail[:120]}))
    if handle:
        window_frame = OUT / "window_thumb.png"
        window = await worker.capture_window(handle, str(window_frame), thumbnail=True)
        window_thumb = Path(window.thumbnail)
        print(
            json.dumps(
                {
                    "window_ok": window.ok,
                    "ms": window.elapsed_ms,
                    "size": [window.width, window.height],
                    "thumbnail_exists": window_thumb.exists(),
                    "thumbnail_bytes": (
                        window_thumb.stat().st_size if window_thumb.exists() else 0
                    ),
                    "inspection": (
                        inspect_frame(window_frame, thumbnail=window_thumb).to_dict()
                        if window_thumb.exists()
                        else None
                    ),
                }
            )
        )

    # 5.2 MP decode cost, independent of the current desktop: the size H03
    # measured at 2880x1800, so the two runs are comparable.
    synthetic = OUT / "synthetic_2880x1800.png"
    Image.new("RGB", (2880, 1800), (40, 90, 140)).save(synthetic, format="PNG")
    print(json.dumps({"inspect_synthetic_5mp": timed(lambda: inspect_frame(synthetic), 3)}))

    # Side cars swept where their frame is gone.
    orphan = OUT / ("orphan.png" + THUMB_SUFFIX)
    orphan.write_bytes(b"\x00" * THUMB_BYTES)
    removed = sweep_orphan_thumbnails(OUT)
    print(json.dumps({"orphans_removed": removed, "orphan_exists": orphan.exists()}))

    await worker.aclose()
    print(json.dumps({"closed": True, "spawns": worker.spawns, "frames": worker.frames}))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
