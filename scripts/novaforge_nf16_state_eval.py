"""NF-16 offline evaluation — what the state reading gets right, measured.

Two separate evaluations, deliberately not mixed:

1. **Fixture evaluation** (deterministic, no OCR). Synthetic perception scenes
   for the ``nc-forge-tick@1`` profile are read by the real builder and compared
   with hand-written expectations. This measures the parts that are code: field
   statuses, value parsing, change detection, unknown bookkeeping. It reports
   accuracy/precision/recall because every case has a ground truth here.

2. **Recorded-frame pass** (real OCR, no ground truth). The real perception
   engine reads frames this repository already captured, and the script reports
   latency, recognized text and whether each declared field was read. It
   reports NO accuracy figure, because there are no labelled expectations for
   those pixels; inventing one would be the fabrication Scope E forbids.

There is no live game evaluation in this script. Launching, focusing or
inputting into a game is a Scope K safety decision made by a person against a
real target, not something an evaluation script does on its own; the report
records that as ``NOT_TESTED``.

Usage::

    PYTHONPATH=src python scripts/novaforge_nf16_state_eval.py [--frames N] [--json PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import struct
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from novacontrol.novaforge.state import (
    HudStatus,
    ObservationRef,
    default_registry,
    build_game_state,
)
from novacontrol.perception.models import BBox, OcrText, SceneRepresentation

FRAME_ROOT = Path("data/novaforge/frames")
TICK_SESSION = "312f07f5ed684916984afa66079dfc5e"  # 1832x718 NC FORGE AUDIT windows
HARNESS_SESSION = "1955f226a5ce4c55aadf07e0de02ea5b"  # 1040x440 harness windows
EVAL_JSON = Path("data/novaforge_nf16_state_eval.json")


# ---------------------------------------------------------------------------
# Fixture evaluation
# ---------------------------------------------------------------------------


def _tick_scene(counter: str | None, header: str = "NC FORGE AUDIT", *, width: int = 1832,
                height: int = 718) -> SceneRepresentation:
    """One scene of the tick harness: header top-left, big counter at 940..1260."""
    text: list[OcrText] = []
    if header:
        text.append(OcrText(text=header, bbox=BBox(x=72, y=20, width=200, height=24),
                            reading_order=0, source="fixture"))
    if counter is not None:
        text.append(OcrText(text=counter, bbox=BBox(x=1000, y=180, width=190, height=80),
                            reading_order=1, source="fixture"))
    return SceneRepresentation(width=width, height=height, text=tuple(text),
                               source_id="fixture", frame_id="fixture")


def _observation(seq: int, *, session: str = "eval-session", width: int = 1832,
                 height: int = 718) -> ObservationRef:
    at = (datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=seq)).isoformat()
    return ObservationRef(
        observation_id=f"{session}:{seq:04d}",
        session_id=session,
        seq=seq,
        at=at,
        width=width,
        height=height,
    )


def _state(seq: int, scene: SceneRepresentation, *, previous: Any = None,
           session: str = "eval-session", title: str = "NC FORGE AUDIT") -> Any:
    match = default_registry().resolve(title, width=1832, height=718)
    return build_game_state(
        scene,
        _observation(seq, session=session),
        sequence=seq,
        profile_match=match,
        previous=previous,
        ocr_available=True,
        detection_available=True,
    )


def evaluate_fixtures() -> dict[str, Any]:
    """Read a hand-written sequence and score the reading against expectations."""
    facts: list[dict[str, Any]] = []
    cases = 0

    def check(name: str, actual: Any, expected: Any) -> None:
        nonlocal cases
        cases += 1
        facts.append(
            {
                "check": name,
                "ok": actual == expected,
                "expected": expected,
                "actual": actual,
            }
        )

    # 786 → 782 → (same) → (counter gone) → "LEVEL 26" (not a number)
    s1 = _state(1, _tick_scene("786"))
    s2 = _state(2, _tick_scene("782"), previous=s1)
    s3 = _state(3, _tick_scene("782"), previous=s2)
    s4 = _state(4, _tick_scene(None), previous=s3)
    s5 = _state(5, _tick_scene("LEVEL 26"), previous=s4)
    # A different session must not be compared at all.
    s6 = _state(1, _tick_scene("900"), previous=s5, session="other-session")

    def hud(state: Any, element: str) -> Any:
        return {row.element_id: row for row in state.hud}[element]

    check("first state has no changes", len(s1.changes), 0)
    check("first state is ok", s1.status.value, "ok")
    check("counter read as a number", hud(s1, "counter").value, 786.0)
    check("header read as text", hud(s1, "header").text, "NC FORGE AUDIT")

    check("786→782 is one value change",
          [change.kind for change in s2.changes], ["hud_value_changed"])
    check("the change names both ends",
          (s2.changes[0].before, s2.changes[0].after) if s2.changes else None,
          ("786", "782"))

    check("an unchanged counter reports nothing",
          [change.kind for change in s3.changes], [])

    check("a region that stops reading is a status change",
          [change.kind for change in s4.changes], ["hud_status_changed"])
    check("the empty region is absent, not zero",
          hud(s4, "counter").status.value, "absent")
    check("the absent field carries no value", hud(s4, "counter").value, None)
    check("an absent field is recorded as unknown",
          "counter" in {row.field_name for row in s4.unknown}, True)

    check("unparseable text is ambiguous, not a value",
          hud(s5, "counter").status.value, "ambiguous")
    check("the ambiguous text is preserved", hud(s5, "counter").text, "LEVEL 26")
    check("an ambiguous field is an unknown with a reason",
          any(row.field_name == "counter" and "not a number" in row.reason
              for row in s5.unknown), True)

    check("a new session is not compared", s6.tracking.compared, False)
    check("the reason is stated", "different session" in s6.tracking.reason, True)

    # An unknown title takes the generic path: no HUD is presumed at all.
    generic = _state(1, _tick_scene("786", header="HITMAN 3"), title="Hitman 3")
    check("an unknown game presumes no HUD",
          [row.element_id for row in generic.hud], [])
    # No game-specific field was read, but plain text was: PARTIAL says exactly
    # that, and UNAVAILABLE would claim nothing was seen at all.
    check("the generic reading is partial, not ok",
          generic.status.value, "partial")
    check("the generic reading says why",
          any("profile" in reason.lower() for reason in generic.reasons), True)

    # A dropped observation is reported as a count, not smoothed over.
    s7 = _state(7, _tick_scene("790"), previous=s4)
    check("a dropped observation is counted", s7.tracking.dropped_observations, 2)

    passed = sum(1 for row in facts if row["ok"])
    return {
        "evaluation": "offline_fixture",
        "cases": cases,
        "passed": passed,
        "accuracy": round(passed / cases, 4) if cases else None,
        "facts": facts,
    }


# ---------------------------------------------------------------------------
# Recorded-frame pass (real OCR, no ground truth)
# ---------------------------------------------------------------------------


def _png_size(path: Path) -> tuple[int, int] | None:
    try:
        head = path.read_bytes()[:24]
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", head[16:24])
    return int(width), int(height)


class _FrameObserver:
    """The smallest observer that satisfies the service's protocol."""

    def __init__(self, path: Path, title: str, *, width: int, height: int) -> None:
        self._path = path
        self._title = title
        self._width = width
        self._height = height
        self._at = datetime.now(UTC).isoformat()

    def status(self) -> dict[str, Any]:
        return {
            "session_id": "nf16-eval",
            "target": {"title": self._title, "pid": 4242, "handle": "1", "start_time": "0"},
            "last_success": {
                "observation_id": "nf16-eval:0001",
                "seq": 1,
                "at": self._at,
                "width": self._width,
                "height": self._height,
                "backend": "recorded-file",
            },
        }

    def frame_path(self) -> Any:
        return self._path


async def _read_recorded(service: Any, path: Path, title: str) -> dict[str, Any]:
    """One reading over the SAME service, so per-frame time excludes engine setup."""
    size = _png_size(path)
    if size is None:
        return {"frame": str(path), "status": "skipped", "reason": "not a readable PNG"}
    service.observer = _FrameObserver(path, title, width=size[0], height=size[1])
    started = time.perf_counter()
    try:
        outcome = await asyncio.wait_for(service.analyze(), timeout=120)
    except Exception as exc:  # noqa: BLE001 - a failure is a result to report
        return {
            "frame": str(path),
            "size": list(size),
            "status": "failed",
            "reason": f"{type(exc).__name__}: {exc}",
            "processing_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    payload = outcome.to_dict()
    state = payload.get("state") or {}
    if not payload.get("analyzed"):
        return {
            "frame": str(path),
            "size": list(size),
            "status": "refused",
            "reason_code": payload.get("reason_code"),
            "reason": payload.get("reason"),
            "processing_ms": elapsed_ms,
        }
    return {
        "frame": str(path),
        "size": list(size),
        "status": "analyzed",
        "processing_ms": elapsed_ms,
        "service_processing_ms": state.get("latency", {}).get("processing_ms"),
        "model_load_ms": state.get("latency", {}).get("model_load_ms"),
        "state_status": state.get("status"),
        "profile": (state.get("profile") or {}).get("profile_id") or None,
        "hud": [
            {"element": row.get("element_id"), "status": row.get("status"),
             "text": row.get("text") or "", "value": row.get("value")}
            for row in state.get("hud") or []
        ],
        "text": [row.get("text") for row in state.get("text") or []][:12],
        "entities": len(state.get("entities") or []),
        "entity_status": state.get("entity_status"),
        "quality": state.get("quality"),
        "reasons": state.get("reasons") or [],
    }


def _run_session(session: str, title: str, limit: int) -> list[dict[str, Any]]:
    """Read a session's recorded frames through ONE service (one engine)."""
    from novacontrol.novaforge.state_service import GameStateService

    paths = [Path(path) for path in sorted(glob.glob(str(FRAME_ROOT / session / "*.png")))[:limit]]
    if not paths:
        return []
    first = _png_size(paths[0]) or (0, 0)
    service = GameStateService(
        observer=_FrameObserver(paths[0], title, width=first[0], height=first[1]),
        max_age_seconds=3600,
    )
    results: list[dict[str, Any]] = []
    try:
        for index, path in enumerate(paths):
            row = asyncio.run(_read_recorded(service, path, title))
            if index:
                row["warm"] = True
            results.append(row)
    finally:
        asyncio.run(service.shutdown())
    return results


def evaluate_recorded_frames(limit: int) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for session, title in ((TICK_SESSION, "NC FORGE AUDIT"),
                           (HARNESS_SESSION, "NC FORGE HARNESS - idle")):
        results.extend(_run_session(session, title, limit))
    durations = [row["processing_ms"] for row in results if "processing_ms" in row]
    analyzed = [row for row in results if row.get("status") == "analyzed"]
    recognized = sum(
        1 for row in analyzed for field in row["hud"] if field["status"] == HudStatus.RECOGNIZED.value
    )
    declared = sum(len(row["hud"]) for row in analyzed)
    # Frame 1 of each session pays the engine's construction cost; the rest are
    # steady state. Reported apart so neither number is mistaken for the other.
    warm = [row["processing_ms"] for row in results if row.get("warm")]
    cold = [row["processing_ms"] for row in results if not row.get("warm")]
    return {
        "evaluation": "offline_recorded_frames",
        "frames": len(results),
        "analyzed": len(analyzed),
        "failed": len([row for row in results if row.get("status") == "failed"]),
        "refused": len([row for row in results
                        if row.get("status") not in {"analyzed", "failed"}]),
        "latency_ms": {
            "first_analysis_includes_engine_setup": durations[0] if durations else None,
            "per_session_first": cold,
            "steady_state_average": round(sum(warm) / len(warm), 1) if warm else None,
            "steady_state_samples": len(warm),
            "max": max(durations) if durations else None,
            "samples": len(durations),
        },
        "declared_fields": declared,
        "recognized_fields": recognized,
        "field_accuracy": None,  # no labelled ground truth for these pixels
        "ocr_accuracy": None,
        "entity_precision": None,
        "entity_recall": None,
        "note": (
            "No accuracy, precision or recall is reported: these frames have no "
            "labelled expectations, so only latency, statuses and the text that was "
            "actually read are measurements."
        ),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=2, help="recorded frames per session")
    parser.add_argument("--json", type=Path, default=EVAL_JSON)
    parser.add_argument("--skip-frames", action="store_true", help="fixtures only")
    args = parser.parse_args()

    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "commit": "uncommitted working tree",
        "fixtures": evaluate_fixtures(),
    }
    if not args.skip_frames:
        report["recorded_frames"] = evaluate_recorded_frames(max(1, args.frames))
    report["live"] = {
        "status": "NOT_TESTED",
        "reason": (
            "No game was launched, focused or driven: live evaluation is a Scope K "
            "safety decision made by a person against a real target."
        ),
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    fixtures = report["fixtures"]
    print(f"fixtures: {fixtures['passed']}/{fixtures['cases']} checks passed "
          f"(accuracy {fixtures['accuracy']})")
    for row in fixtures["facts"]:
        if not row["ok"]:
            print(f"  FAILED {row['check']}: expected {row['expected']!r}, got {row['actual']!r}")
    frames = report.get("recorded_frames")
    if frames:
        print(f"recorded frames: {frames['analyzed']}/{frames['frames']} analyzed, "
              f"latency first={frames['latency_ms']['first_analysis_includes_engine_setup']}ms "
              f"steady={frames['latency_ms']['steady_state_average']}ms "
              f"max={frames['latency_ms']['max']}ms, "
              f"fields {frames['recognized_fields']}/{frames['declared_fields']} recognized")
        for row in frames["results"]:
            print(f"  {Path(row['frame']).name} [{row['status']}] "
                  f"{row.get('state_status', row.get('reason_code', ''))} "
                  f"{row.get('processing_ms')}ms hud={[f['status'] for f in row.get('hud', [])]}")
    print(f"report: {args.json}")
    return 0 if fixtures["passed"] == fixtures["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
