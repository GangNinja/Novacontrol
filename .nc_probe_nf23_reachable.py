"""NF-23 reachability probe: the SHIPPED application with the REAL desktop runner.

Read-only: enumerates windows and reports the capture ladder. No session is
started, no game is launched, no input is sent.
"""

from __future__ import annotations

import asyncio
import json
from tempfile import TemporaryDirectory

from novacontrol.application import NovaControlApplication


async def main() -> int:
    with TemporaryDirectory() as tmp:
        app = NovaControlApplication(data_dir=tmp)
        await app.start()
        try:
            windows = await app.novaforge.windows()
            caps = app.novaforge.capabilities()
            rows = windows.get("windows") or []
            print(
                json.dumps(
                    {
                        "windows_available": windows.get("available"),
                        "capture_supported": windows.get("capture_supported"),
                        "window_count": len(rows),
                        "reason": windows.get("reason", ""),
                        "first_titles": [str(r.get("title", ""))[:40] for r in rows][:10],
                    },
                    indent=1,
                )
            )
            print(json.dumps(caps, indent=1, default=str)[:1600])
        finally:
            await app.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
