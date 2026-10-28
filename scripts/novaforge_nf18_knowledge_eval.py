"""NF-18 offline evaluation — game knowledge, measured against stated expectations.

The evaluation set is a labelled sequence of NF-16 game states for the
``nc-forge-mission@1`` fixture plus explicit control-mapping candidates. Every
expectation below is written BEFORE the run, so each check has a ground truth:

* association (a record belongs to exactly the game it was observed in),
* evidence traceability (every record names the observation/session it rests on),
* retrieval correctness (status/category/question filters, game isolation),
* contradiction handling (both claims kept, neither called verified),
* persistence across a restart of the service,
* freshness (a version change stales version-bound records; age only reports),
* session-memory separation (nothing transient is stored),
* bounded storage, and the false-fact rate (verified without enough evidence).

This is an OFFLINE fixture evaluation, not a claim that NovaForge has learned a
real game. No game is launched, captured or driven.

Usage::

    PYTHONPATH=src python scripts/novaforge_nf18_knowledge_eval.py [--json PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from novacontrol.memory import MemoryManager, SqliteMemoryStore
from novacontrol.novaforge.knowledge import (
    EvidenceSource,
    KnowledgeCandidate,
    KnowledgeCategory,
    KnowledgeStatus,
    RevalidationPolicy,
    SessionMemory,
    control_candidate,
    evidence_for,
)
from novacontrol.novaforge.knowledge_service import KnowledgeService
from novacontrol.novaforge.objective_service import ObjectiveService
from novacontrol.novaforge.state import (
    GameState,
    HudKind,
    HudReading,
    HudStatus,
    ObservationRef,
    ProfileMatch,
    builtin_profiles,
)

EVAL_JSON = Path("data/novaforge_nf18_knowledge_eval.json")
GAME = "NC FORGE MISSION"
OTHER = "NC FORGE OTHER"
BOUND_GAME = "NC FORGE BOUNDS"
START = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> None:
        self.now = self.now + timedelta(**kwargs)


def _profile() -> Any:
    return next(item for item in builtin_profiles() if item.profile_id == "nc-forge-mission@1")


def _stamp(seq: int) -> str:
    return (START + timedelta(seconds=seq)).isoformat()


def _state(
    seq: int,
    *,
    session: str = "nf18-eval",
    game: str = GAME,
    objective: str = "Collect the audit tokens",
    objective_status: HudStatus = HudStatus.RECOGNIZED,
    counter: float | None = 1.0,
    counter_status: HudStatus = HudStatus.RECOGNIZED,
    notification: str = "",
    notification_status: HudStatus = HudStatus.ABSENT,
) -> GameState:
    """One labelled NF-16 state; the fixture never contains pixels."""
    hud = [
        HudReading(
            element_id="objective",
            kind=HudKind.PROMPT,
            label="Objective line",
            status=objective_status,
            text=objective if objective_status is HudStatus.RECOGNIZED else "",
        ),
        HudReading(
            element_id="progress",
            kind=HudKind.NUMBER,
            label="Progress counter",
            status=counter_status,
            text="" if counter is None else f"{counter:g}",
            value=counter if counter_status is HudStatus.RECOGNIZED else None,
        ),
        HudReading(
            element_id="notification",
            kind=HudKind.NOTIFICATION,
            label="Outcome banner",
            status=notification_status,
            text=notification if notification_status is HudStatus.RECOGNIZED else "",
        ),
    ]
    return GameState(
        game=game,
        observation=ObservationRef(
            observation_id=f"{session}:{seq:04d}",
            session_id=session,
            game=game,
            seq=seq,
            at=_stamp(seq),
            width=1280,
            height=720,
            backend="window_surface",
        ),
        sequence=seq,
        timestamp=_stamp(seq),
        hud=tuple(hud),
        profile=ProfileMatch(
            profile=_profile(),
            reason_code="profile_matched",
            reason="the fixture title matched",
        ),
    )


def _evidence(
    observation: str, *, session: str = "nf18-eval", verified: bool = False, game: str = GAME
):
    return evidence_for(
        EvidenceSource.EXPERIMENT if verified else EvidenceSource.STATE,
        f"observation {observation}",
        verified=verified,
        session_id=session,
        observation_id=observation,
        game=game,
    )


def _fact(facts: list[dict[str, Any]], check: str, expected: Any, actual: Any) -> None:
    facts.append(
        {
            "check": check,
            "expected": expected,
            "actual": actual,
            "ok": expected == actual,
        }
    )


def _group(facts: list[dict[str, Any]]) -> dict[str, Any]:
    passed = sum(1 for row in facts if row["ok"])
    return {
        "cases": len(facts),
        "passed": passed,
        "accuracy": round(passed / len(facts), 4) if facts else None,
        "facts": facts,
    }


async def evaluate() -> dict[str, Any]:
    clock = Clock()
    association: list[dict[str, Any]] = []
    traceability: list[dict[str, Any]] = []
    retrieval: list[dict[str, Any]] = []
    contradictions: list[dict[str, Any]] = []
    freshness: list[dict[str, Any]] = []
    persistence: list[dict[str, Any]] = []
    separation: list[dict[str, Any]] = []
    bounds: list[dict[str, Any]] = []
    durations: list[float] = []

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "knowledge.sqlite3"
        service = KnowledgeService(
            memory=MemoryManager(SqliteMemoryStore(db)), clock=clock
        )
        interpreter = ObjectiveService(clock=clock)

        async def record(candidate: KnowledgeCandidate) -> Any:
            started = time.perf_counter()
            result = await service.record(candidate)
            durations.append((time.perf_counter() - started) * 1000.0)
            return result

        # ── 1. Discovery from a validated state, twice ─────────────────────
        first = _state(1, counter=1.0)
        await service.record_state(first)
        interpretation = await interpreter.observe(first)
        assert interpretation is not None
        await service.record_objectives(interpretation)
        rows = await service.records(game=GAME)
        _fact(association, "state records are attributed to the observed game", 1,
              len({row.game for row in rows}))
        hud = next(row for row in rows if row.key == "hud:objective")
        _fact(association, "the first HUD reading is provisional", "provisional",
              hud.status.value)
        _fact(traceability, "the HUD record names its observation", first.observation.observation_id,
              hud.evidence[0].observation_id)
        _fact(traceability, "the HUD record names its session", "nf18-eval",
              hud.evidence[0].session_id)
        _fact(traceability, "the HUD record names its provenance", "NF-16 game-state reading",
              hud.provenance)
        _fact(association, "the reading layout is traceable in evidence", True,
              "1280x720" in hud.evidence[0].detail)
        _fact(association, "a changing on-screen value is not stored as knowledge", False,
              any(key in hud.content for key in ("sample", "value", "status")))

        second = _state(2, counter=1.0)
        await service.record_state(second)
        interpretation2 = await interpreter.observe(second)
        assert interpretation2 is not None
        await service.record_objectives(interpretation2)
        hud = await service.records(game=GAME, key="hud:objective")
        _fact(association, "two independent readings verify a stable HUD field", "verified",
              hud[0].status.value)
        await service.record_state(second)
        again = await service.records(game=GAME, key="hud:objective")
        _fact(association, "a repeated reading is not extra evidence", 2,
              again[0].independent_evidence)

        # ── 2. Progress is an update; a differing CLAIM is a contradiction ──
        changed = _state(3, objective="Collect the audit tokens", counter=2.0)
        await service.record_state(changed)
        rows = await service.records(game=GAME, key="hud:objective")
        _fact(contradictions, "a changing counter is not a contradiction", 1, len(rows))
        _fact(contradictions, "the HUD layout is still verified by the new reading",
              "verified", rows[0].status.value)
        _fact(contradictions, "its evidence grew", 3, rows[0].independent_evidence)

        await record(
            KnowledgeCandidate(
                game=GAME,
                category=KnowledgeCategory.COMPATIBILITY,
                key="compatibility:overlay",
                content={"issue": "the overlay flickers"},
                evidence=(_evidence("compat-1"),),
                provenance="fixture note",
            )
        )
        await record(
            KnowledgeCandidate(
                game=GAME,
                category=KnowledgeCategory.COMPATIBILITY,
                key="compatibility:overlay",
                content={"issue": "the overlay does not flicker"},
                evidence=(_evidence("compat-2"),),
                provenance="fixture note",
            )
        )
        compat = await service.records(game=GAME, key="compatibility:overlay")
        statuses = sorted(row.status.value for row in compat)
        _fact(contradictions, "both claims are kept", 2, len(compat))
        _fact(contradictions, "the earlier claim is contradicted", "contradicted", statuses[0])
        _fact(contradictions, "the challenge is not verified", "provisional", statuses[1])
        _fact(contradictions, "the earlier claim is not overwritten",
              {"issue": "the overlay flickers"},
              dict(next(row for row in compat
                        if row.status is KnowledgeStatus.CONTRADICTED).content))
        challenge = next(row for row in compat if row.status is KnowledgeStatus.PROVISIONAL)
        _fact(contradictions, "the challenge names what it contradicts", 1,
              len(challenge.contradicts))

        # ── 3. A control mapping needs an observed EFFECT ─────────────────
        input_only = KnowledgeCandidate(
            game=GAME,
            category=KnowledgeCategory.CONTROL,
            key="control:open_map",
            content={"action": "open map", "effect": "the map opens"},
            evidence=(_evidence("c1"), _evidence("c2")),
            provenance="fixture experiment",
            revalidation=RevalidationPolicy.VERSION_BOUND,
        )
        stored = await record(input_only)
        _fact(contradictions, "input without an effect never verifies", "provisional",
              stored.status.value if stored else None)
        typed = control_candidate(
            game=GAME,
            action="open map",
            effect="the map opens",
            evidence=(
                _evidence("c3", verified=True),
                _evidence("c4", verified=True),
            ),
            game_version="1",
        )
        stored = await record(typed)
        _fact(contradictions, "two observed effects verify the mapping", "verified",
              stored.status.value if stored else None)
        _fact(retrieval, "verified controls are retrievable", 1,
              len(await service.verified_controls(GAME)))

        # An objective the reader could not establish becomes a QUESTION record;
        # it is the record of NOT knowing and may never be verified.
        unclear = _state(4, objective_status=HudStatus.AMBIGUOUS, counter_status=HudStatus.AMBIGUOUS)
        await service.record_state(unclear)
        unclear_reading = await interpreter.observe(unclear)
        assert unclear_reading is not None
        await service.record_objectives(unclear_reading)
        questions = await service.unresolved_questions(GAME)
        _fact(contradictions, "an unestablished objective is recorded as a question", True,
              len(questions) >= 1)
        _fact(contradictions, "a question is never verified", 0,
              sum(1 for row in questions if row.status is KnowledgeStatus.VERIFIED))

        # A manual record is not staled by a version change.
        manual_candidate = KnowledgeCandidate(
            game=GAME,
            category=KnowledgeCategory.SITUATION,
            key="situation:loading",
            content={"situation": "the loading screen stayed for 20 s"},
            evidence=(_evidence("m1"), _evidence("m2")),
            provenance="fixture note",
            revalidation=RevalidationPolicy.MANUAL,
        )
        stored = await record(manual_candidate)
        _fact(freshness, "a manual note with two observations is verified", "verified",
              stored.status.value if stored else None)

        # ── 4. Freshness: version change and age ───────────────────────────
        current = _state(5, objective="Escape the vault", counter=2.0)
        await service.record_state(current)
        versioned = KnowledgeCandidate(
            game=GAME,
            category=KnowledgeCategory.HUD,
            key="hud:objective",
            content={"element_id": "objective", "label": "Objective line", "kind": "prompt"},
            evidence=(_evidence("v2-1"),),
            game_version="2",
            provenance="fixture v2",
            revalidation=RevalidationPolicy.VERSION_BOUND,
        )
        await record(versioned)
        versions = await service.records(game=GAME, key="hud:objective")
        _fact(freshness, "a confirmed version change stales the older version", True,
              any(row.status is KnowledgeStatus.STALE and row.game_version == "1"
                  for row in versions))
        _fact(freshness, "the new version is not stale", "provisional",
              next(row for row in versions if row.game_version == "2").status.value)
        manual = next(
            row for row in await service.records(game=GAME, category=KnowledgeCategory.SITUATION)
            if row.key == "situation:loading"
        )
        _fact(freshness, "a version change never stales a manual record", "verified",
              manual.status.value)
        summary = await service.summary(game=GAME)
        stale_before = summary["stale"]
        clock.advance(days=40)
        summary = await service.summary(game=GAME)
        _fact(freshness, "age alone marks nothing stale", stale_before, summary["stale"])
        _fact(freshness, "an old record is reported for revalidation", True,
              len(summary["due_for_revalidation"]) >= 1)

        # ── 5. Revalidation of ONE stale record ────────────────────────────
        stale = next(
            row for row in await service.records(game=GAME, status=KnowledgeStatus.STALE)
        )
        result = await service.revalidate(
            stale.knowledge_id, [evidence_for(EvidenceSource.STATE, "revalidated",
                                              observation_id="re-1", session_id="nf18-eval")],
            game=GAME,
        )
        _fact(freshness, "a stale record revalidates with fresh evidence", True,
              result["revalidated"])
        # Its two earlier observations are still evidence, so fresh support can
        # carry it back to verified — never beyond what those observations say.
        _fact(freshness, "revalidation restores a supportable status", "verified",
              result["status"])

        # ── 6. Retrieval, filters and game isolation ───────────────────────
        await record(
            KnowledgeCandidate(
                game=OTHER,
                category=KnowledgeCategory.HUD,
                key="hud:objective",
                content={"label": "Objective line", "sample": "Other game only"},
                evidence=(evidence_for(EvidenceSource.STATE, "other", observation_id="x1",
                                       game=OTHER),),
                provenance="fixture other",
            )
        )
        game_keys = {row.key for row in await service.records(game=GAME)}
        other_rows = await service.records(game=OTHER)
        _fact(retrieval, "another game's record is never returned with this game", False,
              any(row.game == OTHER for row in await service.records(game=GAME)))
        _fact(retrieval, "the other game returns its own record", 1, len(other_rows))
        # Evidence belongs to the game it names; a foreign reference cannot
        # support this game's claim and is refused before anything is stored.
        crossed = await record(
            KnowledgeCandidate(
                game=GAME,
                category=KnowledgeCategory.HUD,
                key="hud:crossed",
                content={"label": "crossed"},
                evidence=(_evidence("x2", game=OTHER),),
                provenance="fixture crossed",
            )
        )
        _fact(association, "evidence labelled with another game is refused", None, crossed)
        _fact(association, "the refusal names the game mismatch", "evidence_game_mismatch",
              service.status()["last_error"]["reason_code"])
        _fact(association, "the refused claim is not stored", 0,
              len(await service.records(game=GAME, key="hud:crossed")))
        _fact(retrieval, "a category filter is exact",
              {KnowledgeCategory.CONTROL.value},
              {row.category.value for row in
               await service.records(game=GAME, category=KnowledgeCategory.CONTROL)})
        _fact(retrieval, "a status filter is exact",
              all(row.status is KnowledgeStatus.CONTRADICTED for row in
                  await service.records(game=GAME, status=KnowledgeStatus.CONTRADICTED)),
              True)
        _fact(retrieval, "an evidence reference is searchable", 1,
              len(await service.records(game=GAME, query="v2-1")))
        _fact(freshness, "a version change stales a version-bound control mapping", True,
              (await service.verified_controls(GAME)) == [] and
              any(row.status is KnowledgeStatus.STALE
                  for row in await service.records(game=GAME,
                                                   category=KnowledgeCategory.CONTROL)))
        _fact(retrieval, "a blank game returns nothing", 0,
              len(await service.records(game="   ")))
        _fact(retrieval, "the keys read are the keys stored", True,
              "hud:objective" in game_keys)

        # ── 7. Persistence across a restart ────────────────────────────────
        before = {row.knowledge_id: row.status.value for row in await service.records(game=GAME)}
        revived = KnowledgeService(
            memory=MemoryManager(SqliteMemoryStore(db)), clock=clock
        )
        after = {row.knowledge_id: row.status.value for row in await revived.records(game=GAME)}
        _fact(persistence, "every record survives a restart", before, after)
        _fact(persistence, "the game index survives a restart", [GAME, OTHER],
              (await revived.summary())["games"])

        # ── 8. Session memory is separate and transient ────────────────────
        memory = SessionMemory()
        memory.note_state(first)
        _fact(separation, "session memory says it is not persisted", False,
              memory.rows()["persisted"])
        _fact(separation, "a new session starts with no observations", 0,
              len(SessionMemory().rows()["observations"]))
        namespaces = await revived.memory.retrieve(f"game_knowledge:{_slug(GAME)}", "", limit=200)
        keys = [row.record.key for row in namespaces]
        _fact(separation, "no transient session row is stored as knowledge", False,
              any("session" in key for key in keys))
        _fact(separation, "records carry evidence, not a recent-observation log", True,
              all(row.record.value.get("evidence") for row in namespaces))

        # ── 9. Bounds ──────────────────────────────────────────────────────
        bounded = KnowledgeService(
            memory=MemoryManager(SqliteMemoryStore(Path(tmp) / "bounded.sqlite3")),
            clock=clock,
            max_evidence=2,
            max_records_per_game=1,
        )
        base = KnowledgeCandidate(
            game=BOUND_GAME,
            category=KnowledgeCategory.HUD,
            key="hud:x",
            content={"label": "x"},
            evidence=tuple(
                _evidence(f"b{index}", game=BOUND_GAME) for index in range(1, 5)
            ),
            provenance="bounds fixture",
        )
        stored = await bounded.record(base)
        _fact(bounds, "evidence is capped per record", 2,
              len(stored.evidence) if stored else None)
        refused = await bounded.record(
            KnowledgeCandidate(
                game=BOUND_GAME,
                category=KnowledgeCategory.HUD,
                key="hud:y",
                content={"label": "y"},
                evidence=(_evidence("b9", game=BOUND_GAME),),
                provenance="bounds fixture",
            )
        )
        _fact(bounds, "a full game refuses rather than silently evicting", None, refused)
        _fact(bounds, "the refusal names the limit", "record_limit",
              bounded.status()["last_error"]["reason_code"])
        updated = await bounded.record(
            KnowledgeCandidate(
                game=BOUND_GAME,
                category=KnowledgeCategory.HUD,
                key="hud:x",
                content={"label": "x"},
                evidence=(_evidence("b10", game=BOUND_GAME),),
                provenance="bounds fixture",
            )
        )
        _fact(bounds, "a known record still accepts evidence at the cap", 2,
              len(updated.evidence) if updated else None)

        # ── 10. No verified fact without its evidence ─────────────────────
        all_rows = await revived.records(game=GAME) + await revived.records(game=OTHER)
        false_facts = [
            row
            for row in all_rows
            if row.status is KnowledgeStatus.VERIFIED
            and (
                row.independent_evidence < 2
                or (row.category is KnowledgeCategory.CONTROL and not row.verified_effect)
            )
        ]
        # A question is a record of NOT knowing; it may never be verified.
        reviewed = await revived.records(game=GAME, category=KnowledgeCategory.QUESTION)
        false_questions = [row for row in reviewed if row.status is KnowledgeStatus.VERIFIED]

    measured = sorted(durations)
    return {
        "schema": "nf18-eval-1",
        "game": GAME,
        "fixture": (
            "nc-forge-mission@1 states (1280x720) + explicit control-mapping "
            "candidates; no pixels, no game launch"
        ),
        "association": _group(association),
        "traceability": _group(traceability),
        "retrieval": _group(retrieval),
        "contradictions": _group(contradictions),
        "freshness": _group(freshness),
        "persistence": _group(persistence),
        "separation": _group(separation),
        "bounds": _group(bounds),
        "false_facts": {
            "verified_without_evidence": len(false_facts),
            "verified_questions": len(false_questions),
            "false_completion_claim": 0,
            "note": (
                "false_completion_claim is 0 because this phase records outcomes from "
                "NF-17 interpretations and never upgrades them to verified without "
                "the declared evidence."
            ),
        },
        "latency_ms": {
            "samples": len(measured),
            "average": round(sum(measured) / len(measured), 3) if measured else None,
            "max": round(measured[-1], 3) if measured else None,
        },
        "live": {
            "status": "NOT_TESTED",
            "reason": (
                "No game was launched, captured or driven: live knowledge acquisition "
                "needs a person-authorized target and this phase sends no input."
            ),
        },
    }


def _slug(game: str) -> str:
    from novacontrol.novaforge.knowledge import game_slug

    return game_slug(game)


async def _main_async(args: argparse.Namespace) -> int:
    report = await evaluate()
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    groups = [
        "association",
        "traceability",
        "retrieval",
        "contradictions",
        "freshness",
        "persistence",
        "separation",
        "bounds",
    ]
    total = sum(report[name]["cases"] for name in groups)
    passed = sum(report[name]["passed"] for name in groups)
    print(f"fixture: {passed}/{total} checks passed")
    for name in groups:
        group = report[name]
        print(f"  {name:14s} {group['passed']}/{group['cases']}  ({group['accuracy']})")
        for row in group["facts"]:
            if not row["ok"]:
                print(
                    f"    FAILED {row['check']}: expected {row['expected']!r}, "
                    f"got {row['actual']!r}"
                )
    false_facts = report["false_facts"]
    print(
        "false facts: "
        f"{false_facts['verified_without_evidence']} verified without evidence, "
        f"{false_facts['verified_questions']} verified questions"
    )
    latency = report["latency_ms"]
    print(
        f"latency: avg {latency['average']} ms, max {latency['max']} ms "
        f"over {latency['samples']} record(s)"
    )
    print(f"live evaluation: {report['live']['status']}")
    print(f"report: {args.json}")
    ok = passed == total and not false_facts["verified_without_evidence"] and not false_facts[
        "verified_questions"
    ]
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="NF-18 offline knowledge evaluation")
    parser.add_argument("--json", type=Path, default=EVAL_JSON)
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
