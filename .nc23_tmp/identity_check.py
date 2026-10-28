"""Does the probe's identity read answer for a live process and None for a dead one?"""

from __future__ import annotations

import asyncio
import ctypes
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".nc23_tmp"))

from observer_lifecycle_probe import process_identity  # noqa: E402


async def main() -> int:
    sleeping = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command", "Start-Sleep -Seconds 120"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    await asyncio.sleep(1.5)
    alive = await process_identity(sleeping.pid)
    print("alive_row:", alive)
    sleeping.terminate()
    try:
        sleeping.wait(timeout=10)
    except subprocess.TimeoutExpired:
        print("terminate did not end it")
    await asyncio.sleep(0.5)
    dead = await process_identity(sleeping.pid)
    print("dead_row:", dead)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
