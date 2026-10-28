"""NF-23 verification: what verdict does a backend failure produce?

The lifecycle probe measured a destroyed window as ``target_unreadable`` from
the router, with no frame written. A refusal that produced NO frame must not
claim ``capture_ok`` — and the panel must not describe it as an identity
mismatch ("the frame is not the target") when there is no frame at all.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novacontrol.desktop.capture_backends import (  # noqa: E402
    BackendCapabilities,
    CaptureRouter,
    CaptureTarget,
    FrameCapture,
)


class FailingBackend:
    """A backend that cannot render the target: no frame, an error code."""

    name = "printwindow"

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            backend=self.name,
            captures="window_surface",
            unfocused=True,
            occluded=True,
            minimized=False,
            requires_foreground=False,
            notes="probe backend",
        )

    def available(self) -> tuple[bool, str]:
        return True, ""

    async def capture_frame(self, path: str, target: CaptureTarget) -> FrameCapture:
        return FrameCapture(
            ok=False, backend=self.name, source="window_surface", path=path,
            error="target_unreadable",
        )


async def main() -> int:
    router = CaptureRouter([FailingBackend()])
    target = CaptureTarget(
        title="NC NF23 PROBE", handle=1,
        bounds=(0, 0, 100, 100), foreground=True, minimized=False, visible=True,
    )
    outcome = await router.capture(str(ROOT / ".nc23_tmp" / "out" / "refusal.png"), target)
    print(json.dumps({
        "outcome_ok": outcome.ok,
        "reason": outcome.reason,
        "verdict": outcome.verdict,
        "attempts": list(outcome.attempts),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
