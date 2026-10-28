"""NF-24 measurement: the REAL pipeline over recorded frames (not a live game).

Drives the shipped ``GameStateService`` (real perception engine, real OCR, real
profiles) over consecutive frames this repository captured earlier, and reports
what it read, how long it took, and what the scene tracker confirmed.

This is RECORDED-FRAME evidence. It is not a live-game run and must not be
reported as one.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from novacontrol.novaforge.knowledge import state_candidates
from novacontrol.novaforge.objectives import default_objective_registry, interpret
from novacontrol.novaforge.state_service import GameStateService

SESSION = Path("data/novaforge/frames/3f84fe424db9479c9133cf93328dd365")
TITLE = "GAME MENU"


class RecordedObserver:
    """An observer whose 'newest validated frame' is a recorded file."""

    def __init__(self, frames: list[Path], title: str) -> None:
        self.frames = frames
        self.title = title
        self.index = 0

    def advance(self) -> None:
        self.index = min(self.index + 1, len(self.frames) - 1)

    def status(self) -> dict[str, Any]:
        frame = self.frames[self.index]
        return {
            "state": "observing",
            "session_id": "recorded-nf24",
            "game": self.title,
            "target": {
                "title": self.title,
                "handle": 40,
                "pid": 4411,
                "start_time": "2026-10-26T12:00:00+00:00",
            },
            "last_success": {
                "observation_id": f"recorded-nf24:{self.index + 1:04d}",
                "seq": self.index + 1,
                "at": datetime.now(UTC).isoformat(),
                "width": 2880,
                "height": 1800,
                "backend": "printwindow",
            },
        }

    def frame_path(self) -> Path:
        return self.frames[self.index]


async def main() -> None:
    frames = sorted(SESSION.glob("*.png"))[:3]
    observer = RecordedObserver(frames, TITLE)
    service = GameStateService(observer=observer)
    rows: list[dict[str, Any]] = []
    previous_objectives = None
    try:
        for index in range(len(frames)):
            if index:
                observer.advance()
            started = time.monotonic()
            outcome = await service.analyze()
            wall_ms = round((time.monotonic() - started) * 1000.0, 1)
            row: dict[str, Any] = {
                "frame": frames[index].name,
                "analyzed": outcome.analyzed,
                "refusal": outcome.reason_code,
                "wall_ms": wall_ms,
            }
            if outcome.state is not None:
                state = outcome.state
                row.update(
                    {
                        "status": state.status.value,
                        "scene": state.scene.to_dict(),
                        "transition": state.transition.to_dict(),
                        "hud": [
                            {
                                "element": item.element_id,
                                "status": item.status.value,
                                "text": item.text,
                            }
                            for item in state.hud
                        ],
                        "confidence": state.confidence,
                        "processing_ms": state.latency.get("processing_ms"),
                        "entities": len(state.entities),
                        "quality": dict(state.quality),
                    }
                )
                objectives = interpret(state, previous_objectives)
                previous_objectives = objectives
                row["objectives"] = {
                    "statuses": [item.status.value for item in objectives.objectives],
                    "unattributed": list(objectives.diagnostics.unattributed),
                }
                row["knowledge_candidates"] = len(state_candidates(state))
            rows.append(row)
            print(json.dumps(row, indent=2))
    finally:
        await service.shutdown()
    print("=== measured ===")
    print(json.dumps(service.status()["measured"], indent=2))
    print(json.dumps({"scene": service.status()["scene"]}, indent=2))
    durations = [
        row["processing_ms"] for row in rows if isinstance(row.get("processing_ms"), (int, float))
    ]
    if durations:
        print(
            json.dumps(
                {
                    "processing_ms_min": min(durations),
                    "processing_ms_max": max(durations),
                    "processing_ms_mean": round(sum(durations) / len(durations), 1),
                    "frames": len(durations),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
