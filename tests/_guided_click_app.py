"""App factory for the guided-click Playwright e2e (imported by the spec only).

Boots the REAL create_app() stack with one substitution: the desktop runner is
``ScriptedVisionRunner``, which turns a VISION_CLICK into two synthetic
screenshot frames on disk plus a real result mapping. That means the live
guided-click flow — plan, locate stub, execute, pixel-diff verification, bug
auto-resolve — runs end to end over HTTP exactly as the UI drives it, without
moving a real cursor.

The runner honors NOVA_VISION_FRAME_MODE:
  changed    — after-frame differs (verification passes: ``changed: true``)
  identical  — frames equal (verification honestly fails: ``changed: false``)

NOVA_DATA_DIR points the application at a disposable data directory so the
real user's data/bugs.json is never touched.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from novacontrol.desktop.models import DesktopAction, DesktopActionType


def _redirected_path(*args: object, **kwargs: object) -> Path:
    """Path() stand-in that turns Path("data") into the disposable data dir.

    The real create_app builds its app with data_dir=Path("data"); patching
    Path in that module's namespace rewrites the literal to NOVA_DATA_DIR so
    every store (bugs.json included) lands in the temp directory.
    """
    if args and args[0] == "data":
        return Path(os.environ["NOVA_DATA_DIR"])
    return Path(*args, **kwargs)  # type: ignore[arg-type]


class ScriptedVisionRunner:
    """Desktop runner that 'clicks' by synthesizing before/after frames."""

    def __init__(self, *, command_timeout_seconds: float = 60, vision_provider: object | None = None) -> None:
        self.command_timeout_seconds = command_timeout_seconds
        self.vision_provider = vision_provider
        self.frame_mode = os.environ.get("NOVA_VISION_FRAME_MODE", "changed")
        self.clicks: list[str] = []

    async def run(self, action: DesktopAction) -> Mapping[str, Any]:
        if action.type is not DesktopActionType.VISION_CLICK:
            return {"would_run": action.to_dict()}
        label = action.target
        self.clicks.append(label)
        before_path = "vision_locate.png"
        after_path = "vision_after.png"
        self._write_frames(before_path, after_path)
        # Deliberately NO _last_click_point: with no click point, verification
        # is a pure pixel-diff (the OCR re-check would call live Windows OCR
        # on synthetic frames — nondeterministic and unnecessary here).
        return {
            "adapter": "local-desktop",
            "action": "vision_click",
            "label": label,
            "coordinates": [150, 80],
            "located_by": "ocr",
            "before": before_path,
            "after": after_path,
            "before_path": before_path,
            "after_path": after_path,
        }

    def _write_frames(self, before_path: str, after_path: str) -> None:
        from PIL import Image

        base = Image.new("RGB", (200, 120), (250, 250, 250))
        base.save(before_path)
        if self.frame_mode == "identical":
            base.save(after_path)
            return
        changed = base.copy()
        for x in range(120, 180):
            for y in range(40, 80):
                changed.putpixel((x, y), (10, 10, 10))
        changed.save(after_path)


def create_app() -> Any:
    """Boot the real API app against a scripted desktop runner."""
    from unittest import mock

    # NOVA_DATA_DIR must point at a disposable directory: the stock factory
    # hardcodes ./data, which would let the test read and auto-resolve the
    # user's REAL bug log. Redirect it by construction, not by assertion.
    data_dir = os.environ.get("NOVA_DATA_DIR")
    assert data_dir, "NOVA_DATA_DIR must point at a disposable data directory"
    with mock.patch("novacontrol.api.app.Path", _redirected_path), \
            mock.patch("novacontrol.application.LocalDesktopRunner", ScriptedVisionRunner):
        from novacontrol.api.app import create_app as real_create_app

        return real_create_app()
