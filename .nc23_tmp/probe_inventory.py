"""NF-23 reconnaissance: what games exist on THIS machine, and are they safe to test?

Untracked scratch (like .nc22_tmp). Read-only: no launch, no input, no window touch.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novacontrol.desktop.controller import LocalDesktopRunner  # noqa: E402


async def main() -> int:
    runner = LocalDesktopRunner()
    print("=== steam inventory ===")
    games = runner.installed_steam_games()
    for row in games:
        install = str(row.get("install_dir") or "")
        exe = str(row.get("executable") or "")
        exists = Path(install).is_dir() if install else False
        print(
            json.dumps(
                {
                    "name": str(row.get("name") or ""),
                    "app_id": str(row.get("app_id") or ""),
                    "install_exists": exists,
                    "install_dir": install,
                    "exe": exe,
                    "exe_exists": bool(exe) and Path(exe).is_file(),
                },
                ensure_ascii=False,
            )
        )
    print(f"steam_rows={len(games)}")

    print("=== memory gate ===")
    snapshot = await runner.memory_snapshot()
    print(json.dumps(snapshot, default=str))

    print("=== running processes matching installed names ===")
    names: list[str] = []
    for row in games:
        name = str(row.get("name") or "").strip()
        if name:
            names.append(name)
    rows = await runner.game_processes(names) if names else []
    for row in rows:
        print(json.dumps(row, default=str))

    print("=== executable-shaped processes (top 40 by name) ===")
    import subprocess

    raw = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-Process | Where-Object { $_.MainWindowTitle } | "
            "Select-Object ProcessName,Id,MainWindowTitle | ConvertTo-Json -Compress",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()
    try:
        parsed = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        print("unparsed:", raw[:400])
        parsed = []
    if isinstance(parsed, dict):
        parsed = [parsed]
    for row in parsed:
        print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
