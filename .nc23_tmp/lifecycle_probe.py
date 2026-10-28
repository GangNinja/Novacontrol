"""NF-23 F-02b + F-05: the window-surface path on a REAL window, and its lifecycle.

Read-only with respect to the operator's work: the probe's own disposable window
is created, occluded, minimized, restored, resized, destroyed and recreated. No
game is launched, no input is synthesized, no other application is touched. The
cover layer is the probe's second window, not somebody else's.

Evidence printed as JSON lines, one claim per line, so the report can quote them.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image  # noqa: E402

from novacontrol.desktop.capture import (  # noqa: E402
    PersistentScreenCapture,
)
from novacontrol.desktop.capture_backends import (  # noqa: E402
    CaptureRouter,
    CaptureTarget,
    DisplayCropBackend,
    WindowSurfaceBackend,
)
from novacontrol.desktop.capture_validation import (  # noqa: E402
    THUMB_BYTES,
    THUMB_HEIGHT,
    THUMB_WIDTH,
    inspect_frame,
)

OUT = ROOT / ".nc23_tmp" / "out"
WINDOW = ROOT / ".nc23_tmp" / "lifecycle_window.ps1"
TITLE = "NC NF23 LIFECYCLE PROBE"
SECOND_TITLE = "NC NF23 REPLACEMENT PROBE"

user32 = ctypes.windll.user32
SW_MINIMIZE = 6
SW_RESTORE = 9
WM_CLOSE = 0x0010
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010


def say(**fields: object) -> None:
    print(json.dumps(fields, default=str), flush=True)


def spawn_window(*, title_tweak: bool = False, cover: bool = False, seconds: int = 120) -> subprocess.Popen:
    script = WINDOW
    args = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-Seconds", str(seconds),
    ]
    if cover:
        args.append("-Cover")
    process = subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if title_tweak:
        # The replacement window is the SAME script: only its identity differs.
        pass
    return process


def channel_means_bgr(path: Path) -> tuple[float, float, float] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) != THUMB_BYTES:
        return None
    span = THUMB_BYTES // 3
    return (
        sum(data[index + 2] for index in range(0, len(data), 3)) / span,
        sum(data[index + 1] for index in range(0, len(data), 3)) / span,
        sum(data[index] for index in range(0, len(data), 3)) / span,
    )


def reference_means(path: Path) -> tuple[float, float, float]:
    with Image.open(path) as image:
        reduced = image.convert("RGB").resize((THUMB_WIDTH, THUMB_HEIGHT), Image.BICUBIC)
    pixels = list(reduced.getdata())
    total = len(pixels)
    return (
        sum(pixel[0] for pixel in pixels) / total,
        sum(pixel[1] for pixel in pixels) / total,
        sum(pixel[2] for pixel in pixels) / total,
    )


async def find_window(worker: PersistentScreenCapture, title: str, timeout: float = 20.0) -> object | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        geometry = await worker.window_geometry(title=title)
        if geometry is not None:
            return geometry
        await asyncio.sleep(0.5)
    return None


async def main() -> int:  # noqa: C901 - a scripted probe, linear by design
    OUT.mkdir(parents=True, exist_ok=True)
    worker = PersistentScreenCapture(startup_timeout=60.0, frame_timeout=60.0)
    if not await worker.ensure_started():
        say(error=worker.last_error)
        return 1
    say(worker_ready=True, screen=list(worker.size))

    first = spawn_window(seconds=150)
    geometry = await find_window(worker, TITLE)
    if geometry is None:
        say(error="the disposable window never appeared")
        first.terminate()
        await worker.aclose()
        return 1
    handle = int(getattr(geometry, "handle"))
    bounds = tuple(getattr(geometry, "bounds"))
    say(
        window_found=True,
        handle=handle,
        pid=int(getattr(geometry, "pid", 0)),
        bounds=list(bounds),
        foreground=bool(getattr(geometry, "foreground", False)),
        minimized=bool(getattr(geometry, "minimized", False)),
        visible=bool(getattr(geometry, "visible", True)),
    )

    # ── B. the side car on the window path, and its channel order ────────
    frame = OUT / "life_window.png"
    thumb_path = Path(str(frame) + ".thumb")
    started = time.perf_counter()
    captured = await worker.capture_window(handle, str(frame), thumbnail=True)
    latency_ms = (time.perf_counter() - started) * 1000.0
    say(
        b_window_capture_ok=captured.ok,
        b_latency_ms=round(latency_ms, 1),
        b_reported_ms=captured.elapsed_ms,
        b_bytes=captured.size_bytes,
        b_dims=[captured.width, captured.height],
        b_error=captured.error,
    )
    if not captured.ok:
        say(error=f"window capture failed: {captured.error}")
        first.terminate()
        await worker.aclose()
        return 1
    side_car_exists = thumb_path.exists()
    say(
        b_side_car=captured.thumbnail,
        b_side_car_exists=side_car_exists,
        b_side_car_bytes=thumb_path.stat().st_size if side_car_exists else 0,
        b_expected_side_car_bytes=THUMB_BYTES,
    )
    if side_car_exists:
        side = channel_means_bgr(thumb_path)
        reference = reference_means(frame)
        say(
            b_reference_rgb_means=[round(value, 2) for value in reference],
            b_side_car_bgr_as_rgb_means=[round(value, 2) for value in side] if side else None,
            b_per_channel_delta=(
                [round(abs(a - b), 2) for a, b in zip(side, reference)] if side else None
            ),
        )
        full = inspect_frame(frame)
        sparse = inspect_frame(frame, thumbnail=thumb_path)
        say(
            b_full_width=full.width,
            b_full_height=full.height,
            b_full_blank=full.blank,
            b_full_mean_luma=full.mean_luma,
            b_side_car_blank=sparse.blank,
            b_side_car_mean_luma=sparse.mean_luma,
            b_hash_agrees=bool(full.content_hash) and full.content_hash == sparse.content_hash,
            b_verdict_agrees=full.blank == sparse.blank,
        )
        decode_samples: list[float] = []
        for _ in range(3):
            at = time.perf_counter()
            inspect_frame(frame)
            decode_samples.append((time.perf_counter() - at) * 1000.0)
        side_samples: list[float] = []
        for _ in range(3):
            at = time.perf_counter()
            inspect_frame(frame, thumbnail=thumb_path)
            side_samples.append((time.perf_counter() - at) * 1000.0)
        say(
            b_inspect_full_decode_ms=round(statistics.median(decode_samples), 2),
            b_inspect_side_car_ms=round(statistics.median(side_samples), 2),
            b_frame_bytes=frame.stat().st_size,
        )

    # ── C. the ROUTER, exactly as the product wires it ───────────────────
    def window_seam(h: int, path: str):
        return worker.capture_window(h, path, thumbnail=True)

    router = CaptureRouter((WindowSurfaceBackend(window_seam), DisplayCropBackend(worker.capture)))
    say(router_capabilities=router.capabilities())

    def target_from(geom: object | None) -> CaptureTarget | None:
        if geom is None:
            return None
        return CaptureTarget(
            handle=int(getattr(geom, "handle")),
            title=str(getattr(geom, "title", "")),
            bounds=tuple(getattr(geom, "bounds")),  # type: ignore[arg-type]
            minimized=bool(getattr(geom, "minimized", False)),
            visible=bool(getattr(geom, "visible", True)),
            foreground=bool(getattr(geom, "foreground", False)),
        )

    async def routed(label: str = "c") -> tuple[dict, float]:
        geom = await worker.window_geometry(handle=handle)
        target = target_from(geom)
        if target is None:
            at = time.perf_counter()
            outcome = await router.capture(str(OUT / "life_route.png"), CaptureTarget(
                handle=handle, title=TITLE, bounds=tuple(bounds),
                minimized=True, visible=True, foreground=False,
            ))
            return {
                f"{label}_ok": outcome.ok,
                f"{label}_reason": outcome.reason,
                f"{label}_code": outcome.verdict.get("code", ""),
                f"{label}_attempts": list(outcome.attempts),
                f"{label}_geometry": None,
            }, (time.perf_counter() - at) * 1000.0
        at = time.perf_counter()
        route_frame = OUT / f"life_route_{label}.png"
        outcome = await router.capture(str(route_frame), target)
        elapsed = (time.perf_counter() - at) * 1000.0
        return {
            f"{label}_ok": outcome.ok,
            f"{label}_backend": outcome.backend,
            f"{label}_source": outcome.source,
            f"{label}_dims": [outcome.width, outcome.height],
            f"{label}_reason": outcome.reason,
            f"{label}_verdict": outcome.verdict,
            f"{label}_attempts": list(outcome.attempts),
            f"{label}_side_car_left": Path(str(route_frame) + ".thumb").exists(),
            f"{label}_geometry": {
                "handle": target.handle,
                "bounds": list(target.bounds),
                "foreground": target.foreground,
                "minimized": target.minimized,
                "visible": target.visible,
            },
        }, elapsed

    unfocused, unfocused_ms = await routed("c_unfocused")
    say(**unfocused, c_unfocused_ms=round(unfocused_ms, 1))

    # ── D. occluded: the probe's own TopMost cover window, same position ──
    cover = spawn_window(cover=True, seconds=150)
    await asyncio.sleep(2.0)
    covered_target = await worker.window_geometry(handle=handle)
    say(
        d_target_still_reported=True,
        d_target_foreground=bool(getattr(covered_target, "foreground", False))
        if covered_target
        else None,
    )
    covered, covered_ms = await routed("d_covered")
    say(**covered, d_covered_ms=round(covered_ms, 1))
    # What the display path WOULD serve while covered: the cover's pixels.
    display_only = OUT / "life_display_covered.png"
    display_capture = await worker.capture(str(display_only), thumbnail=True)
    if display_capture.ok:
        clip = Image.open(display_only).crop(tuple(bounds))
        pixels = list(clip.resize((THUMB_WIDTH, THUMB_HEIGHT), Image.BICUBIC).getdata())
        total = len(pixels)
        say(
            d_display_crop_rgb_means=[
                round(sum(p[0] for p in pixels) / total, 2),
                round(sum(p[1] for p in pixels) / total, 2),
                round(sum(p[2] for p in pixels) / total, 2),
            ],
            d_display_crop_size=list(clip.size),
        )
        clip.close()
    if covered.get("d_covered_ok") and side_car_exists:
        covered_thumb = Path(str(OUT / "life_route_d_covered.png") + ".thumb")
        # The router consumed the side car; compare the SAVED frame instead.
        means = reference_means(OUT / "life_route_d_covered.png")
        say(d_covered_frame_rgb_means=[round(value, 2) for value in means])
    cover.terminate()

    # ── E. minimized ─────────────────────────────────────────────────────
    user32.ShowWindow(handle, SW_MINIMIZE)
    await asyncio.sleep(1.5)
    minimized, minimized_ms = await routed("e_minimized")
    say(**minimized, e_detect_ms=round(minimized_ms, 1))
    say(
        e_is_iconic=bool(user32.IsIconic(handle)),
        e_frame_written=Path(str(OUT / "life_route_e_minimized.png")).exists(),
    )

    # ── F. restored ──────────────────────────────────────────────────────
    user32.ShowWindow(handle, SW_RESTORE)
    await asyncio.sleep(1.5)
    restored_geometry = await worker.window_geometry(handle=handle)
    restored, restored_ms = await routed("f_restored")
    say(
        **restored,
        f_restore_ms=round(restored_ms, 1),
        f_restored_bounds=list(getattr(restored_geometry, "bounds"))
        if restored_geometry
        else None,
    )

    # ── G. resized ───────────────────────────────────────────────────────
    if restored_geometry:
        left, top, right, bottom = tuple(getattr(restored_geometry, "bounds"))
        user32.SetWindowPos(
            handle, 0, left, top, (right - left) + 120, (bottom - top) + 80,
            SWP_NOZORDER | SWP_NOACTIVATE,
        )
        await asyncio.sleep(1.5)
        resized, resized_ms = await routed("g_resized")
        new_geometry = await worker.window_geometry(handle=handle)
        say(
            **resized,
            g_new_bounds=list(getattr(new_geometry, "bounds")) if new_geometry else None,
            g_resize_ms=round(resized_ms, 1),
        )

    # ── H. destroyed ─────────────────────────────────────────────────────
    user32.PostMessageW(handle, WM_CLOSE, 0, 0)
    gone_at = time.monotonic()
    while time.monotonic() - gone_at < 10.0:
        if await worker.window_geometry(handle=handle) is None:
            break
        await asyncio.sleep(0.3)
    destroyed_ms = (time.monotonic() - gone_at) * 1000.0
    after_close = await worker.capture_window(handle, str(OUT / "life_dead.png"), thumbnail=True)
    say(
        h_window_gone_ms=round(destroyed_ms, 1),
        h_capture_ok=after_close.ok,
        h_capture_error=after_close.error,
        h_frame_written=Path(str(OUT / "life_dead.png")).exists(),
    )

    # ── I. replaced: a DIFFERENT process under the same title ────────────
    second = spawn_window(seconds=60)
    replacement = await find_window(worker, TITLE)
    if replacement is not None:
        new_handle = int(getattr(replacement, "handle"))
        say(
            i_replacement_found=True,
            i_new_handle=new_handle,
            i_old_handle=handle,
            i_handle_changed=new_handle != handle,
            i_new_pid=int(getattr(replacement, "pid", 0)),
        )
        stale = await worker.capture_window(handle, str(OUT / "life_stale.png"), thumbnail=True)
        say(
            i_old_handle_capture_ok=stale.ok,
            i_old_handle_capture_error=stale.error,
            i_old_handle_frame_written=Path(str(OUT / "life_stale.png")).exists(),
        )
    second.terminate()
    first.terminate()
    await asyncio.sleep(0.5)

    stats = await worker.send_input("stats")
    say(worker_stats=stats.detail)
    await worker.aclose()
    say(closed=True, spawns=worker.spawns, frames=worker.frames)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
