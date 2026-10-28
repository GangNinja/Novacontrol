"""NF-18 — game knowledge and memory: the model, the service and its surfaces.

Everything here runs against a temporary SQLite memory store and deterministic
states built from the REAL NF-16 records; no game is launched, no screen is
captured and no claim about a commercial title's controls is made.
"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock
from pathlib import Path
from typing import Any

from test_novaforge_objectives import mission_state
from test_web_api import _IsolatedApiTestCase

from novacontrol.memory import InMemoryMemoryStore, MemoryManager, SqliteMemoryStore
from novacontrol.novaforge.knowledge import (
    ALLOWED_TRANSITIONS,
    MAX_EVIDENCE,
    EvidenceSource,
    KnowledgeCandidate,
    KnowledgeCategory,
    KnowledgeEvidence,
    KnowledgeRecord,
    KnowledgeStatus,
    RevalidationPolicy,
    SessionMemory,
    VerificationMethod,
    candidate_from_payload,
    can_transition,
    control_candidate,
    evidence_for,
    knowledge_id_for,
    objective_candidates,
    state_candidates,
    transition,
)
from novacontrol.novaforge.knowledge_service import KnowledgeService
from novacontrol.novaforge.manager import NovaForgeManager
from novacontrol.novaforge.state import GameState, HudStatus, ObservationRef

GAME = "NC FORGE MISSION"
OTHER = "OTHER GAME"
NOW = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)


class Clock:
    """A clock a test can advance; every record's stamps follow it."""

    def __init__(self, moment: datetime = NOW) -> None:
        self.now = moment

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> None:
        self.now = self.now + timedelta(**kwargs)


def record(
    *,
    knowledge_id: str = "k-1",
    game: str = GAME,
    category: KnowledgeCategory = KnowledgeCategory.HUD,
    key: str = "hud:hp",
    content: dict[str, Any] | None = None,
    status: KnowledgeStatus = KnowledgeStatus.UNVERIFIED,
    **kwargs: Any,
) -> KnowledgeRecord:
    return KnowledgeRecord(
        knowledge_id=knowledge_id,
        game=game,
        category=category,
        key=key,
        content=content if content is not None else {"label": "HP", "value": 90},
        status=status,
        created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
        last_verified_at=kwargs.pop(
            "last_verified_at", NOW.isoformat() if status is KnowledgeStatus.VERIFIED else ""
        ),
        **kwargs,
    )


def evidence(
    observation: str,
    *,
    session: str = "s1",
    verified: bool = False,
    source: EvidenceSource = EvidenceSource.STATE,
    summary: str = "",
    game: str = GAME,
) -> KnowledgeEvidence:
    return evidence_for(
        source,
        summary or f"observed {observation}",
        session_id=session,
        observation_id=observation,
        game=game,
        verified=verified,
    )


def candidate(
    *,
    game: str = GAME,
    category: KnowledgeCategory = KnowledgeCategory.HUD,
    key: str = "hud:hp",
    content: dict[str, Any] | None = None,
    rows: tuple[KnowledgeEvidence, ...] | None = None,
    version: str = "1",
    policy: RevalidationPolicy = RevalidationPolicy.VERSION_BOUND,
) -> KnowledgeCandidate:
    return KnowledgeCandidate(
        game=game,
        category=category,
        key=key,
        content=content if content is not None else {"label": "HP", "value": 90},
        evidence=rows if rows is not None else (evidence("o1", game=game),),
        game_version=version,
        provenance="test",
        revalidation=policy,
    )


class KnowledgeModelTests(unittest.TestCase):
    """The typed record refuses what it cannot mean."""

    def test_a_record_refuses_unknown_values_and_an_unsourced_verified_state(self) -> None:
        with self.assertRaises(ValueError):
            record(knowledge_id="")
        with self.assertRaises(ValueError):
            record(game="")
        with self.assertRaises(ValueError):
            record(category="lore")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            record(status="probably")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            record(content={})
        with self.assertRaises(ValueError):
            record(key="")
        with self.assertRaises(ValueError):
            record(revision=0)
        with self.assertRaises(ValueError):
            record(status=KnowledgeStatus.VERIFIED, last_verified_at="")
        with self.assertRaises(ValueError):
            record(evidence=tuple(evidence(f"o{index}") for index in range(MAX_EVIDENCE + 1)))
        with self.assertRaises(ValueError):
            record(history=tuple({"at": "x", "from": "a", "to": "b"} for _ in range(13)))

    def test_a_record_needs_structured_content_with_bounded_fields(self) -> None:
        with self.assertRaises(ValueError):
            record(content={"a": 1, **{f"k{index}": index for index in range(20)}})
        # Bounded storage includes the VALUES, not only the field count.
        with self.assertRaises(ValueError):
            record(content={"a": "x" * 301})
        with self.assertRaises(ValueError):
            record(content={"a": object()})
        with self.assertRaises(ValueError):
            record(content={"a": {"b" * 61: 1}})
        with self.assertRaises(ValueError):
            record(content={"a": {"b": {"c": {"d": 1}}}})
        with self.assertRaises(ValueError):
            record(game_version="v" * 61)
        with self.assertRaises(ValueError):
            candidate_from_payload(
                {"game": GAME, "category": "hud", "key": "k", "content": {"a": "x" * 301}}
            )

    def test_evidence_refuses_an_unknown_source_and_an_empty_summary(self) -> None:
        with self.assertRaises(ValueError):
            KnowledgeEvidence(source="rumour", summary="x")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            KnowledgeEvidence(
                source=EvidenceSource.STATE, summary="x", method="guess"  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            KnowledgeEvidence(source=EvidenceSource.STATE, summary="   ")
        self.assertFalse(KnowledgeEvidence(source=EvidenceSource.STATE, summary="x").verified)

    def test_evidence_identity_is_stable_and_independent(self) -> None:
        first = evidence("o1")
        self.assertEqual(first.fingerprint, evidence("o1").fingerprint)
        self.assertNotEqual(first.fingerprint, evidence("o2").fingerprint)
        self.assertEqual(first.independent_key, "o1")
        # No observation id: the fingerprint is still a usable independent key.
        manual = evidence_for(EvidenceSource.MANUAL, "a person typed this")
        self.assertEqual(manual.independent_key, manual.fingerprint)

    def test_the_transition_table_is_enforced(self) -> None:
        unverified = record()
        provisional = transition(
            unverified, KnowledgeStatus.PROVISIONAL, at=NOW.isoformat(), reason="evidence seen"
        )
        self.assertEqual(provisional.status, KnowledgeStatus.PROVISIONAL)
        verified = transition(
            provisional, KnowledgeStatus.VERIFIED, at=NOW.isoformat(), reason="two observations"
        )
        self.assertTrue(verified.last_verified_at)
        with self.assertRaises(ValueError):
            transition(
                verified, KnowledgeStatus.PROVISIONAL, at=NOW.isoformat(), reason="downgrade"
            )
        with self.assertRaises(ValueError):
            transition(
                record(status=KnowledgeStatus.CONTRADICTED),
                KnowledgeStatus.VERIFIED,
                at=NOW.isoformat(),
                reason="skip",
            )
        with self.assertRaises(ValueError):
            transition(provisional, KnowledgeStatus.VERIFIED, at=NOW.isoformat(), reason=" ")
        # The table itself never permits a move the code refuses.
        for before, after in (
            (KnowledgeStatus.UNVERIFIED, KnowledgeStatus.PROVISIONAL),
            (KnowledgeStatus.VERIFIED, KnowledgeStatus.STALE),
            (KnowledgeStatus.CONTRADICTED, KnowledgeStatus.PROVISIONAL),
        ):
            self.assertTrue(can_transition(before, after))
            self.assertIn(after, ALLOWED_TRANSITIONS[before])
        self.assertFalse(can_transition(KnowledgeStatus.VERIFIED, KnowledgeStatus.UNVERIFIED))

    def test_a_record_round_trips_through_its_wire_shape(self) -> None:
        original = record(
            status=KnowledgeStatus.VERIFIED,
            evidence=(evidence("o1"), evidence("o2")),
        )
        restored = KnowledgeRecord.from_dict(original.to_dict())
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.knowledge_id, original.knowledge_id)
        self.assertEqual(restored.status, KnowledgeStatus.VERIFIED)
        self.assertEqual(restored.independent_evidence, 2)
        self.assertEqual(restored.content_digest, original.content_digest)
        # A corrupt row is None, never a half-record.
        for broken in (
            None,
            {},
            {"knowledge_id": "k-1"},
            {"knowledge_id": "k-1", "game": GAME, "category": "not-a-category"},
        ):
            self.assertIsNone(KnowledgeRecord.from_dict(broken))

    def test_a_candidate_id_changes_with_the_claim_and_not_with_its_evidence(self) -> None:
        first = candidate()
        same = candidate(rows=(evidence("o9"),))
        different = candidate(content={"label": "HP", "value": 40})
        other_game = candidate(game=OTHER)
        self.assertEqual(first.knowledge_id, same.knowledge_id)
        self.assertNotEqual(first.knowledge_id, different.knowledge_id)
        self.assertNotEqual(first.knowledge_id, other_game.knowledge_id)
        self.assertEqual(
            first.knowledge_id,
            knowledge_id_for(GAME, KnowledgeCategory.HUD, "hud:hp", {"label": "HP", "value": 90}, "1"),
        )
        # A different configuration is a different record, not an overwrite.
        self.assertNotEqual(first.knowledge_id, candidate(version="2").knowledge_id)

    def test_a_control_mapping_cannot_be_recorded_from_an_input_alone(self) -> None:
        with self.assertRaises(ValueError):
            control_candidate(game=GAME, action="jump", effect="the player jumps", evidence=())
        with self.assertRaises(ValueError):
            control_candidate(
                game=GAME,
                action="jump",
                effect="the player jumps",
                evidence=(evidence("o1", source=EvidenceSource.EXPERIMENT),),
            )
        built = control_candidate(
            game=GAME,
            action="jump",
            effect="the player jumps",
            evidence=(
                evidence("o1", source=EvidenceSource.EXPERIMENT, verified=True),
                evidence("o2", source=EvidenceSource.EXPERIMENT, verified=True),
            ),
        )
        self.assertEqual(built.category, KnowledgeCategory.CONTROL)
        self.assertEqual(built.key, "control:jump")

    def test_a_payload_builds_a_candidate_or_names_what_is_wrong(self) -> None:
        built = candidate_from_payload(
            {
                "game": GAME,
                "category": "hud",
                "key": "hud:hp",
                "content": {"label": "HP"},
                "evidence": [evidence("o1").to_dict()],
                "game_version": "1",
            }
        )
        self.assertEqual(built.category, KnowledgeCategory.HUD)
        self.assertEqual(len(built.evidence), 1)
        invalid = (
            {"category": "hud", "key": "k", "content": {"a": 1}},  # no game
            {"game": GAME, "category": "lore", "key": "k", "content": {"a": 1}},
            {"game": GAME, "category": "hud", "key": "k", "content": "text"},
            {"game": GAME, "category": "hud", "key": "k", "content": {"a": 1}, "evidence": [{}]},
            {
                "game": GAME,
                "category": "hud",
                "key": "k",
                "content": {"a": 1},
                "evidence": ["nope"],
            },
            {
                "game": GAME,
                "category": "hud",
                "key": "k",
                "content": {"a": 1},
                "revalidation": "whenever",
            },
        )
        for payload in invalid:
            with self.assertRaises(ValueError):
                candidate_from_payload(payload)

    def test_session_memory_is_bounded_and_clears_on_a_new_session(self) -> None:
        memory = SessionMemory(max_items=2)
        memory.note_state(mission_state(seq=1))
        memory.note_state(mission_state(seq=2))
        memory.note_state(mission_state(seq=3))
        self.assertEqual(len(memory.observations), 2)
        self.assertFalse(memory.rows()["persisted"])
        self.assertEqual(memory.rows()["session_id"], "s1")
        memory.note_state(mission_state(session="s2", seq=9))
        self.assertEqual(memory.session_id, "s2")
        self.assertEqual(len(memory.observations), 1)


class KnowledgeServiceTests(unittest.IsolatedAsyncioTestCase):
    """The service is the gate between an observation and durable knowledge."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "memory.sqlite3"
        self.clock = Clock()
        self.events: list[tuple[str, dict[str, Any]]] = []

    def manager(self) -> MemoryManager:
        return MemoryManager(SqliteMemoryStore(self.path))

    def service(self, **kwargs: Any) -> KnowledgeService:
        async def publish(event_type: str, payload: dict[str, Any]) -> None:
            self.events.append((event_type, payload))

        kwargs.setdefault("publish", publish)
        return KnowledgeService(memory=self.manager(), clock=self.clock, **kwargs)

    async def test_an_observation_is_provisional_until_independent_evidence_agrees(self) -> None:
        service = self.service()
        first = await service.record(candidate())
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first.status, KnowledgeStatus.PROVISIONAL)
        self.assertFalse(first.last_verified_at)
        second = await service.record(candidate(rows=(evidence("o2"),)))
        assert second is not None
        self.assertEqual(second.status, KnowledgeStatus.VERIFIED)
        self.assertTrue(second.last_verified_at)
        self.assertEqual(second.independent_evidence, 2)
        # revision 1 → evidence added (2) → promotion is its own transition (3).
        self.assertEqual(second.revision, 3)
        self.assertEqual(
            [(row["from"], row["to"]) for row in second.history],
            [("provisional", "verified")],
        )
        self.assertIn("independent observations", second.history[0]["reason"])
        self.assertEqual(service.status()["counts"]["promoted"], 1)
        self.assertEqual([row["after"] for _, row in self.events], ["provisional", "verified"])

    async def test_a_claim_without_evidence_is_stored_unverified(self) -> None:
        stored = await self.service().record(candidate(rows=()))
        assert stored is not None
        self.assertEqual(stored.status, KnowledgeStatus.UNVERIFIED)
        self.assertIn("cannot be verified", stored.reason)
        self.assertEqual(stored.independent_evidence, 0)

    async def test_an_evidence_free_challenge_is_unverified_not_provisional(self) -> None:
        service = self.service()
        first = await service.record(candidate())
        assert first is not None
        challenge = await service.record(candidate(content={"label": "HP", "value": 40}, rows=()))
        assert challenge is not None
        self.assertEqual(challenge.status, KnowledgeStatus.UNVERIFIED)
        self.assertEqual(challenge.contradicts, (first.knowledge_id,))
        self.assertEqual(len(await service.records(game=GAME)), 2)

    async def test_an_inference_repeated_is_still_an_inference(self) -> None:
        service = self.service()
        inferred = tuple(
            evidence_for(
                EvidenceSource.OBJECTIVE,
                f"inferred objective {index}",
                method=VerificationMethod.INFERRED,
                observation_id=f"i{index}",
            )
            for index in (1, 2)
        )
        stored = await service.record(
            candidate(category=KnowledgeCategory.OBJECTIVE_PATTERN, rows=inferred)
        )
        assert stored is not None
        self.assertEqual(stored.status, KnowledgeStatus.PROVISIONAL)
        self.assertEqual(stored.independent_evidence, 2)
        # One observed reading may carry it over the line; a guess may not.
        mixed = (
            *inferred,
            evidence_for(
                EvidenceSource.OBJECTIVE,
                "the objective was displayed",
                method=VerificationMethod.OBSERVED,
                observation_id="i3",
            ),
        )
        promoted = await service.record(
            candidate(category=KnowledgeCategory.OBJECTIVE_PATTERN, rows=mixed)
        )
        assert promoted is not None
        self.assertEqual(promoted.status, KnowledgeStatus.VERIFIED)

    async def test_an_outcome_needs_explicit_outcome_evidence(self) -> None:
        service = self.service()
        unbacked = tuple(
            evidence_for(
                EvidenceSource.OBJECTIVE,
                "an objective seemed finished",
                observation_id=f"u{index}",
            )
            for index in (1, 2)
        )
        stored = await service.record(
            candidate(
                category=KnowledgeCategory.OUTCOME,
                key="outcome:o1",
                content={"description": "Finish the mission", "status": "completed"},
                rows=unbacked,
            )
        )
        assert stored is not None
        self.assertEqual(stored.status, KnowledgeStatus.PROVISIONAL)
        backed = (
            *unbacked,
            evidence_for(
                EvidenceSource.OBJECTIVE,
                "a mission-complete screen was read",
                observation_id="u3",
                verified=True,
            ),
        )
        promoted = await service.record(
            candidate(
                category=KnowledgeCategory.OUTCOME,
                key="outcome:o1",
                content={"description": "Finish the mission", "status": "completed"},
                rows=backed,
            )
        )
        assert promoted is not None
        self.assertEqual(promoted.status, KnowledgeStatus.VERIFIED)

    async def test_a_failed_write_leaves_the_previous_record_intact(self) -> None:
        service = self.service()
        first = await service.record(candidate())
        assert first is not None
        with mock.patch.object(service.memory, "remember", side_effect=OSError("disk full")):
            failed = await service.record(candidate(content={"label": "HP", "value": 40}))
        self.assertIsNone(failed)
        rows = await service.records(game=GAME)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].knowledge_id, first.knowledge_id)
        self.assertEqual(rows[0].status, KnowledgeStatus.PROVISIONAL)
        self.assertEqual(rows[0].content["value"], 90)
        self.assertEqual(service.status()["last_error"]["reason_code"], "record_failed")

    async def test_repeating_one_observation_is_not_second_evidence(self) -> None:
        service = self.service()
        await service.record(candidate())
        again = await service.record(candidate())
        assert again is not None
        self.assertEqual(again.status, KnowledgeStatus.PROVISIONAL)
        self.assertEqual(again.independent_evidence, 1)
        self.assertEqual(service.status()["counts"]["duplicates"], 1)
        self.assertEqual(service.status()["counts"]["received"], 1)

    async def test_a_contradiction_is_preserved_and_never_silently_resolved(self) -> None:
        service = self.service()
        first = await service.record(candidate())
        assert first is not None
        challenge = await service.record(candidate(content={"label": "HP", "value": 40}))
        assert challenge is not None
        self.assertEqual(challenge.status, KnowledgeStatus.PROVISIONAL)
        self.assertEqual(challenge.contradicts, (first.knowledge_id,))
        rows = await service.records(game=GAME)
        self.assertEqual(len(rows), 2)
        by_id = {row.knowledge_id: row for row in rows}
        self.assertEqual(by_id[first.knowledge_id].status, KnowledgeStatus.CONTRADICTED)
        self.assertEqual(by_id[first.knowledge_id].content["value"], 90)
        self.assertEqual(
            [(row["from"], row["to"]) for row in by_id[first.knowledge_id].history],
            [("provisional", "contradicted")],
        )
        self.assertIn("different claim", by_id[first.knowledge_id].history[0]["reason"])
        self.assertEqual(by_id[challenge.knowledge_id].content["value"], 40)
        self.assertEqual(service.status()["counts"]["contradicted"], 1)
        # Fresh observations of the challenger do NOT promote past a conflict.
        promoted = await service.record(
            candidate(content={"label": "HP", "value": 40}, rows=(evidence("o2"),))
        )
        assert promoted is not None
        self.assertEqual(promoted.status, KnowledgeStatus.PROVISIONAL)

    async def test_a_demotion_is_never_written_before_its_challenger(self) -> None:
        class DemotionFails:
            """Knowledge writes land until a record is demoted; that write fails."""

            def __init__(self, inner: Any) -> None:
                self.inner = inner

            async def retrieve(self, *args: Any, **kwargs: Any) -> Any:
                return await self.inner.retrieve(*args, **kwargs)

            async def remember(
                self, namespace: Any, key: str, value: Any, **kwargs: Any
            ) -> Any:
                if isinstance(value, dict) and value.get("status") == "contradicted":
                    raise OSError("disk full on the demotion")
                return await self.inner.remember(namespace, key, value, **kwargs)

        service = KnowledgeService(
            memory=DemotionFails(self.manager()), clock=self.clock  # type: ignore[arg-type]
        )
        first = await service.record(candidate())
        assert first is not None
        self.assertIsNone(
            await service.record(candidate(content={"label": "HP", "value": 40}))
        )
        self.assertEqual(service.status()["last_error"]["reason_code"], "record_failed")
        rows = await service.records(game=GAME)
        by_id = {row.knowledge_id: row for row in rows}
        # The challenge exists and names what it disputed; the incumbent was NOT
        # demoted, because no claim may be contradicted by nothing.
        challenge = next(row for row in rows if row.knowledge_id != first.knowledge_id)
        self.assertEqual(challenge.status, KnowledgeStatus.PROVISIONAL)
        self.assertEqual(challenge.contradicts, (first.knowledge_id,))
        self.assertEqual(by_id[first.knowledge_id].status, KnowledgeStatus.PROVISIONAL)
        self.assertEqual(service.status()["counts"]["contradicted"], 0)

    async def test_a_control_mapping_without_an_observed_effect_never_verifies(self) -> None:
        service = self.service()
        # Inputs were sent; nothing shows the game reacted. That is not a mapping.
        for index in range(1, 5):
            await service.record(
                KnowledgeCandidate(
                    game=GAME,
                    category=KnowledgeCategory.CONTROL,
                    key="control:jump",
                    content={"action": "jump", "effect": "the player jumps"},
                    evidence=(evidence(f"o{index}", source=EvidenceSource.EXPERIMENT),),
                    provenance="test",
                )
            )
        rows_now = await service.records(game=GAME, category=KnowledgeCategory.CONTROL)
        self.assertEqual(len(rows_now), 1)
        self.assertEqual(rows_now[0].independent_evidence, 4)
        self.assertFalse(rows_now[0].verified_effect)
        self.assertEqual(rows_now[0].status, KnowledgeStatus.PROVISIONAL)

    async def test_a_control_mapping_verifies_with_two_effects_and_not_one(self) -> None:
        service = self.service()
        one = control_candidate(
            game=GAME,
            action="jump",
            effect="the player jumps",
            evidence=(evidence("o1", source=EvidenceSource.EXPERIMENT, verified=True),),
        )
        first = await service.record(one)
        assert first is not None
        self.assertEqual(first.status, KnowledgeStatus.PROVISIONAL)
        second = control_candidate(
            game=GAME,
            action="jump",
            effect="the player jumps",
            evidence=(
                evidence("o1", source=EvidenceSource.EXPERIMENT, verified=True),
                evidence("o2", source=EvidenceSource.EXPERIMENT, verified=True),
            ),
        )
        updated = await service.record(second)
        assert updated is not None
        self.assertEqual(updated.status, KnowledgeStatus.VERIFIED)
        self.assertTrue(updated.verified_effect)

    async def test_an_unresolved_question_is_never_closed_by_observations(self) -> None:
        service = self.service()
        question = candidate(
            category=KnowledgeCategory.QUESTION,
            key="question:objective",
            content={"description": "what is the goal?"},
            rows=(evidence("o1"), evidence("o2"), evidence("o3")),
        )
        stored = await service.record(question)
        assert stored is not None
        self.assertEqual(stored.status, KnowledgeStatus.PROVISIONAL)
        self.assertEqual(len(await service.unresolved_questions(GAME)), 1)

    async def test_a_version_change_stales_only_version_bound_records(self) -> None:
        service = self.service()
        await service.record(candidate(version="1"))
        await service.record(
            candidate(
                category=KnowledgeCategory.SITUATION,
                key="situation:menu",
                content={"situation": "main menu"},
                version="1",
                policy=RevalidationPolicy.MANUAL,
            )
        )
        await service.record(
            candidate(
                content={"label": "HP", "value": 100},
                version="2",
            )
        )
        rows = await service.records(game=GAME)
        statuses = {(row.category.value, row.game_version): row.status for row in rows}
        self.assertEqual(statuses[("hud", "1")], KnowledgeStatus.STALE)
        self.assertEqual(statuses[("situation", "1")], KnowledgeStatus.PROVISIONAL)
        self.assertEqual(statuses[("hud", "2")], KnowledgeStatus.PROVISIONAL)
        self.assertEqual(service.status()["counts"]["stale"], 1)

    async def test_an_unknown_version_never_stales_anything(self) -> None:
        service = self.service()
        await service.record(candidate(version=""))
        recent = await service.record(
            candidate(content={"label": "HP", "value": 95}, version="")
        )
        assert recent is not None
        rows = await service.records(game=GAME)
        self.assertTrue(all(row.status is not KnowledgeStatus.STALE for row in rows))

    async def test_age_alone_is_a_revalidation_report_not_a_stale_mark(self) -> None:
        service = self.service()
        stored = await service.record(candidate())
        assert stored is not None
        self.clock.advance(days=40)
        summary = await service.summary(game=GAME)
        self.assertEqual(summary["stale"], 0)
        self.assertEqual(len(summary["due_for_revalidation"]), 1)
        self.assertEqual(summary["due_for_revalidation"][0]["knowledge_id"], stored.knowledge_id)
        self.assertIn("not a probability", summary["note"])
        # A manual record is not nagged about: its revalidation is a person's call.
        await service.record(
            candidate(
                category=KnowledgeCategory.SITUATION,
                key="situation:x",
                content={"situation": "x"},
                policy=RevalidationPolicy.MANUAL,
            )
        )
        summary = await service.summary(game=GAME)
        self.assertEqual(len(summary["due_for_revalidation"]), 1)

    async def test_a_stale_record_revalidates_with_fresh_evidence_only(self) -> None:
        service = self.service(min_verified_evidence=3)
        await service.record(candidate(version="1"))
        stale = await service.record(candidate(content={"label": "HP", "value": 95}, version="2"))
        assert stale is not None
        old = (await service.records(game=GAME, status=KnowledgeStatus.STALE))[0]
        refused = await service.revalidate(old.knowledge_id, [], game=GAME)
        self.assertFalse(refused["revalidated"])
        repeated = await service.revalidate(old.knowledge_id, [evidence("o1")], game=GAME)
        self.assertFalse(repeated["revalidated"])
        self.assertIn("no new independent evidence", repeated["reason"])
        first = await service.revalidate(old.knowledge_id, [evidence("o7")], game=GAME)
        self.assertTrue(first["revalidated"], first)
        self.assertEqual(first["status"], "provisional")
        # No longer stale: further evidence arrives through the normal path.
        second = await service.record(
            candidate(version="1", rows=(evidence("o8"),))
        )
        assert second is not None
        self.assertEqual(second.status, KnowledgeStatus.VERIFIED)
        self.assertEqual(second.knowledge_id, old.knowledge_id)
        # A record that needs no revalidation is reported, not changed.
        current = await service.revalidate(old.knowledge_id, [evidence("o9")], game=GAME)
        self.assertFalse(current["revalidated"])
        self.assertIn("not stale or contradicted", current["reason"])

    async def test_unknown_and_unreadable_records_are_refusals_not_exceptions(self) -> None:
        service = self.service()
        unknown = await service.revalidate("k-missing", [evidence("o1")], game=GAME)
        self.assertFalse(unknown["revalidated"])
        self.assertEqual(unknown["reason"], "unknown knowledge record")

    async def test_retrieval_is_scoped_to_one_game_and_filters_honestly(self) -> None:
        service = self.service()
        await service.record(candidate())
        await service.record(candidate(game=OTHER, key="hud:hp"))
        await service.record(
            candidate(
                game=OTHER,
                category=KnowledgeCategory.COMPATIBILITY,
                key="compatibility:overlay",
                content={"issue": "overlay flickers"},
            )
        )
        self.assertEqual(len(await service.records(game=GAME)), 1)
        self.assertEqual(len(await service.records(game=OTHER)), 2)
        self.assertEqual(await service.records(game="   "), [])
        self.assertEqual(await service.records(game="NEVER OBSERVED"), [])
        self.assertEqual(
            len(await service.records(game=OTHER, category=KnowledgeCategory.COMPATIBILITY)), 1
        )
        self.assertEqual(
            len(await service.records(game=OTHER, category="not-a-category")), 0
        )
        self.assertEqual(
            len(await service.records(game=OTHER, status=KnowledgeStatus.CONTRADICTED)), 0
        )
        self.assertEqual(len(await service.records(game=OTHER, query="flicker")), 1)
        # Evidence references are searchable: a reader can ask what relates to an
        # observation or objective, not only what a key happens to be named.
        self.assertEqual(len(await service.records(game=GAME, query="o1")), 1)
        self.assertEqual(len(await service.records(game=OTHER, limit=1)), 1)
        self.assertEqual(await service.verified_controls(GAME), [])

    async def test_knowledge_survives_a_restart_of_the_service(self) -> None:
        service = self.service()
        first = await service.record(candidate())
        second = await service.record(candidate(rows=(evidence("o2"),)))
        assert first is not None and second is not None
        await service.shutdown()
        revived = KnowledgeService(memory=self.manager(), clock=self.clock)
        rows = await revived.records(game=GAME)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, KnowledgeStatus.VERIFIED)
        self.assertEqual(rows[0].independent_evidence, 2)
        self.assertEqual(rows[0].knowledge_id, first.knowledge_id)
        summary = await revived.summary()
        self.assertEqual(summary["games"], [GAME])
        self.assertEqual(summary["verified"], 1)

    async def test_a_transient_index_failure_is_retried_not_remembered(self) -> None:
        service = self.service()
        stored = await service.record(candidate())
        assert stored is not None
        revived = KnowledgeService(memory=self.manager(), clock=self.clock)
        original = revived.memory.retrieve
        calls = {"count": 0}

        async def flaky(*args: Any, **kwargs: Any) -> Any:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("index temporarily unavailable")
            return await original(*args, **kwargs)

        with mock.patch.object(revived.memory, "retrieve", side_effect=flaky):
            first = await revived.summary()
            self.assertEqual(first["games"], [])
            second = await revived.summary()
        self.assertEqual(second["games"], [GAME])
        self.assertEqual(second["records"], 1)

    async def test_a_stored_row_from_an_older_schema_still_loads(self) -> None:
        service = self.service()
        legacy_id = "k-legacy-000001"
        await service.memory.remember(
            service._namespace(GAME),
            legacy_id,
            {
                "knowledge_id": legacy_id,
                "game": GAME,
                "category": "hud",
                "key": "hud:legacy",
                "content": {"label": "Legacy field"},
                "status": "provisional",
                "evidence": [evidence("legacy-1").to_dict()],
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
                "revision": 2,
            },
            text="legacy knowledge row",
        )
        await service.memory.remember(
            service._namespace(GAME),
            "k-corrupt-00001",
            {"knowledge_id": "k-corrupt-00001", "game": GAME, "category": "lore"},
            text="corrupt row",
        )
        rows = await service.records(game=GAME)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].knowledge_id, legacy_id)
        self.assertEqual(rows[0].status, KnowledgeStatus.PROVISIONAL)
        self.assertEqual(rows[0].history, ())
        self.assertEqual(rows[0].revision, 2)

    async def test_cancellation_propagates_and_leaves_no_record(self) -> None:
        class CancellingMemory:
            async def retrieve(self, *_args: Any, **_kwargs: Any) -> Any:
                return ()

            async def remember(self, *_args: Any, **_kwargs: Any) -> Any:
                raise asyncio.CancelledError()

        service = KnowledgeService(memory=CancellingMemory(), clock=self.clock)  # type: ignore[arg-type]
        with self.assertRaises(asyncio.CancelledError):
            await service.record(candidate())
        self.assertEqual(service.status()["counts"]["received"], 0)
        self.assertEqual(await service.records(game=GAME), [])

    async def test_revalidation_refuses_something_that_is_not_evidence(self) -> None:
        service = self.service()
        await service.record(candidate(version="1"))
        await service.record(candidate(content={"label": "HP", "value": 95}, version="2"))
        stale = (await service.records(game=GAME, status=KnowledgeStatus.STALE))[0]
        result = await service.revalidate(
            stale.knowledge_id, ["not evidence"], game=GAME  # type: ignore[list-item]
        )
        self.assertFalse(result["revalidated"])
        self.assertIn("evidence references", result["reason"])

    async def test_an_unreadable_store_answers_unknown_and_never_raises(self) -> None:
        class BrokenMemory:
            async def retrieve(self, *_args: Any, **_kwargs: Any) -> Any:
                raise RuntimeError("store down")

            async def remember(self, *_args: Any, **_kwargs: Any) -> Any:
                raise RuntimeError("store down")

        service = KnowledgeService(memory=BrokenMemory(), clock=self.clock)  # type: ignore[arg-type]
        self.assertEqual(await service.records(game=GAME), [])
        status = service.status()
        self.assertEqual(status["last_error"]["reason_code"], "store_unreadable")
        self.assertIsNone(await service.record(candidate()))
        after = service.status()
        self.assertGreaterEqual(after["counts"]["failed"], 1)
        self.assertEqual(after["counts"]["received"], 0)

    async def test_a_write_that_cannot_persist_is_counted_not_raised(self) -> None:
        class ReadOnlyMemory:
            async def retrieve(self, *_args: Any, **_kwargs: Any) -> Any:
                return ()

            async def remember(self, *_args: Any, **_kwargs: Any) -> Any:
                raise OSError("disk full")

        service = KnowledgeService(memory=ReadOnlyMemory(), clock=self.clock)  # type: ignore[arg-type]
        self.assertIsNone(await service.record(candidate()))
        status = service.status()
        self.assertEqual(status["last_error"]["reason_code"], "record_failed")
        self.assertEqual(status["counts"]["failed"], 1)
        self.assertEqual(status["counts"]["received"], 0)

    async def test_an_obsolete_session_is_refused_and_an_unsourced_claim_is_allowed(self) -> None:
        service = self.service(session_validator=lambda session: session == "live")
        refused = await service.record(candidate(rows=(evidence("o1", session="dead"),)))
        self.assertIsNone(refused)
        self.assertEqual(service.status()["last_error"]["reason_code"], "obsolete_session")
        unnamed = await service.record(
            candidate(rows=(evidence_for(EvidenceSource.PROFILE, "declared by the profile"),))
        )
        self.assertIsNotNone(unnamed)

    async def test_evidence_naming_another_game_is_refused_not_stored(self) -> None:
        service = self.service()
        foreign = evidence_for(
            EvidenceSource.STATE, "read on another game", game=OTHER, observation_id="x1"
        )
        self.assertIsNone(await service.record(candidate(rows=(foreign,))))
        self.assertEqual(service.status()["last_error"]["reason_code"], "evidence_game_mismatch")
        self.assertEqual(await service.records(game=GAME), [])
        # Evidence that names no game is not a cross-game claim: a declared
        # profile or a manual note has no observation to attribute.
        declared = evidence_for(EvidenceSource.PROFILE, "the profile declares this")
        self.assertIsNotNone(await service.record(candidate(rows=(declared,))))
        # Revalidation refuses foreign evidence too, and leaves the record alone.
        second = self.service()
        await second.record(candidate(version="1"))
        await second.record(candidate(content={"label": "HP", "value": 95}, version="2"))
        stale = (await second.records(game=GAME, status=KnowledgeStatus.STALE))[0]
        refused = await second.revalidate(stale.knowledge_id, [foreign], game=GAME)
        self.assertFalse(refused["revalidated"])
        self.assertIn("another game", refused["reason"])
        unchanged = await second.find(stale.knowledge_id, game=GAME)
        assert unchanged is not None
        self.assertEqual(unchanged.status, KnowledgeStatus.STALE)

    async def test_the_record_cap_refuses_new_claims_and_still_updates_known_ones(self) -> None:
        service = self.service(max_records_per_game=1)
        first = await service.record(candidate())
        assert first is not None
        refused = await service.record(candidate(content={"label": "MP", "value": 5}))
        self.assertIsNone(refused)
        self.assertEqual(service.status()["last_error"]["reason_code"], "record_limit")
        # The known claim still accepts new evidence: the cap is on records.
        updated = await service.record(candidate(rows=(evidence("o2"),)))
        assert updated is not None
        self.assertEqual(updated.status, KnowledgeStatus.VERIFIED)

    async def test_evidence_is_bounded_per_record(self) -> None:
        service = self.service(max_evidence=2)
        rows = tuple(evidence(f"o{index}") for index in range(1, 6))
        stored = await service.record(candidate(rows=rows))
        assert stored is not None
        self.assertEqual(len(stored.evidence), 2)
        self.assertEqual(stored.independent_evidence, 2)

    async def test_a_stopped_service_refuses_writes_and_a_bad_candidate_is_named(self) -> None:
        service = self.service()
        self.assertIsNone(await service.record(object()))  # type: ignore[arg-type]
        self.assertEqual(service.status()["last_error"]["reason_code"], "invalid_candidate")
        # The MODEL refuses an out-of-bounds key at construction ...
        with self.assertRaises(ValueError):
            candidate(key="k" * 130)
        # ... and the SERVICE refuses one that skipped the model (built by hand),
        # because it validates whatever object it is handed.
        unchecked = copy.copy(candidate())
        object.__setattr__(unchecked, "key", "k" * 130)
        self.assertIsNone(await service.record(unchecked))
        self.assertEqual(service.status()["last_error"]["reason_code"], "invalid_candidate")
        await service.shutdown()
        self.assertIsNone(await service.record(candidate()))
        self.assertEqual(service.status()["last_error"]["reason_code"], "service_stopped")
        self.assertTrue(service.status()["stopped"])

    async def test_state_candidates_come_only_from_recognized_fields(self) -> None:
        state = mission_state(seq=1, counter_status=HudStatus.UNREADABLE)
        built = state_candidates(state)
        self.assertTrue(built)
        keys = {row.key for row in built}
        self.assertIn("hud:objective", keys)
        self.assertNotIn("hud:progress", keys)
        # The record is the LAYOUT; the text read is evidence, not content, so a
        # changing counter/objective line is an update rather than a contradiction.
        objective = next(row for row in built if row.key == "hud:objective")
        self.assertNotIn("sample", objective.content)
        self.assertNotIn("value", objective.content)
        self.assertIn("1280x720", objective.evidence[0].detail)
        self.assertIn("Collect the audit tokens", objective.evidence[0].summary)
        # A state with no game identity makes no claim about any game.
        bare = GameState(
            game="",
            observation=ObservationRef(observation_id="o1", session_id="s1"),
            hud=state.hud,
            profile=state.profile,
        )
        self.assertEqual(state_candidates(bare), [])

    async def test_objective_candidates_keep_patterns_outcomes_and_questions_apart(self) -> None:
        from novacontrol.novaforge.objective_service import ObjectiveService

        service = ObjectiveService()
        state = await service.observe(mission_state(seq=1, counter=1.0))
        assert state is not None
        self.assertTrue(state.objectives)
        built = objective_candidates(state)
        self.assertIn(KnowledgeCategory.OBJECTIVE_PATTERN, {row.category for row in built})
        # An objective the reader could not establish becomes a QUESTION, not a
        # guessed objective.
        unclear = await service.observe(
            mission_state(
                session="s2",
                seq=1,
                objective_status=HudStatus.AMBIGUOUS,
                counter=None,
            )
        )
        assert unclear is not None
        self.assertIn(
            KnowledgeCategory.QUESTION,
            {row.category for row in objective_candidates(unclear)},
        )

    async def test_progress_is_an_update_not_a_contradiction_of_the_objective(self) -> None:
        from novacontrol.novaforge.objective_service import ObjectiveService

        service = self.service()
        interpreter = ObjectiveService()
        in_progress = await interpreter.observe(mission_state(seq=1, counter=1.0))
        completed = await interpreter.observe(mission_state(seq=2, counter=3.0))
        assert in_progress is not None and completed is not None
        first = await service.record_objectives(in_progress)
        second = await service.record_objectives(completed)
        patterns = [
            row
            for row in await service.records(game=GAME)
            if row.category is KnowledgeCategory.OBJECTIVE_PATTERN
        ]
        self.assertEqual(len(patterns), 1, [(row.key, row.status) for row in patterns])
        self.assertIsNot(patterns[0].status, KnowledgeStatus.CONTRADICTED)
        self.assertEqual(patterns[0].independent_evidence, 2)
        # The outcome and question are separate records with their own status.
        self.assertTrue(second)
        self.assertTrue(first)

    async def test_a_summary_counts_what_it_has_and_lists_the_games(self) -> None:
        service = self.service()
        await service.record(candidate())
        await service.record(candidate(game=OTHER, key="hud:hp"))
        summary = await service.summary()
        self.assertEqual(summary["games"], sorted([GAME, OTHER]))
        self.assertEqual(summary["records"], 2)
        self.assertEqual(summary["provisional"], 2)
        self.assertEqual(summary["by_category"]["hud"], 2)
        per_game = await service.summary(game=GAME)
        self.assertEqual(per_game["records"], 1)
        self.assertEqual(per_game["games"], sorted([GAME, OTHER]))


class KnowledgeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """NF-18 consumes NF-16/NF-17 output and keeps sessions apart from facts."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def manager(self, **kwargs: Any) -> NovaForgeManager:
        async def capture(_path: str) -> dict[str, Any]:
            return {"fresh": False, "reason": "this test captures nothing"}

        async def geometry(_title: str) -> None:
            return None

        async def windows() -> list[str]:
            return []

        return NovaForgeManager(
            data_dir=self.data_dir,
            capture=capture,
            geometry=geometry,
            window_lines=windows,
            **kwargs,
        )

    async def test_a_validated_state_becomes_hud_and_objective_knowledge(self) -> None:
        manager = self.manager()
        await manager._consume_state(mission_state(seq=1))
        rows = await manager.knowledge.records(game=GAME)
        self.assertTrue(rows)
        self.assertTrue(any(row.category is KnowledgeCategory.HUD for row in rows))
        self.assertTrue(
            any(row.category is KnowledgeCategory.OBJECTIVE_PATTERN for row in rows), rows
        )
        self.assertTrue(all(row.independent_evidence >= 1 for row in rows))
        self.assertTrue(all(row.provenance for row in rows))
        session = manager.session_memory.rows()
        self.assertFalse(session["persisted"])
        self.assertEqual(session["session_id"], "s1")
        self.assertEqual(len(session["objectives"]), 1)

    async def test_a_second_state_promotes_a_stable_reading_to_verified(self) -> None:
        manager = self.manager()
        await manager._consume_state(mission_state(seq=1))
        await manager._consume_state(mission_state(seq=2))
        rows = await manager.knowledge.records(game=GAME, category=KnowledgeCategory.HUD)
        objective = next(row for row in rows if row.key == "hud:objective")
        self.assertEqual(objective.status, KnowledgeStatus.VERIFIED)
        self.assertEqual(objective.independent_evidence, 2)

    async def test_session_memory_never_reaches_the_store_and_clears_on_stop(self) -> None:
        manager = self.manager()
        await manager._consume_state(mission_state(seq=1))
        self.assertIsNotNone(manager.memory)
        assert manager.memory is not None
        namespaces = await manager.memory.retrieve("game_knowledge_index", "", limit=5)
        self.assertTrue(namespaces)
        self.assertIn(GAME, (namespaces[0].record.value.get("games") or []))
        # Session memory is not durable: a fresh manager starts empty.
        fresh = self.manager()
        self.assertEqual(fresh.session_memory.rows()["observations"], [])
        self.assertEqual(fresh.session_memory.rows()["session_id"], "")

    async def test_a_stopped_observation_session_cannot_add_knowledge(self) -> None:
        manager = self.manager()
        await manager._consume_state(mission_state(seq=1))
        await manager.stop_observation()
        refused = await manager.record_knowledge(
            KnowledgeCandidate(
                game=GAME,
                category=KnowledgeCategory.HUD,
                key="hud:late",
                content={"label": "late"},
                evidence=(evidence("late-1", session="s1"),),
                provenance="test",
            )
        )
        self.assertFalse(refused["recorded"])
        self.assertIn("current session", refused["reason"])
        status = await manager.knowledge_status(game=GAME)
        self.assertTrue(status["separation"])

    async def test_knowledge_status_reports_counts_session_memory_and_limits(self) -> None:
        manager = self.manager()
        await manager._consume_state(mission_state(seq=1))
        body = await manager.knowledge_status(game=GAME)
        self.assertIn("knowledge", body)
        self.assertEqual(body["knowledge"]["schema_version"], "nf18.1")
        self.assertGreaterEqual(body["summary"]["records"], 1)
        self.assertFalse(body["session_memory"]["persisted"])
        self.assertEqual(body["knowledge"]["limits"]["min_verified_evidence"], 2)
        listed = await manager.knowledge_records(game=GAME)
        self.assertEqual(listed["count"], len(listed["records"]))
        self.assertTrue(all("evidence" in row for row in listed["records"]))

    async def test_an_interpreter_failure_does_not_suppress_recognized_hud_knowledge(self) -> None:
        manager = self.manager()
        with mock.patch.object(
            manager.objective_state, "observe", side_effect=RuntimeError("interpreter exploded")
        ):
            await manager._consume_state(mission_state(seq=1))
        rows = await manager.knowledge.records(game=GAME, category=KnowledgeCategory.HUD)
        self.assertTrue(rows)
        self.assertTrue(any(row.key == "hud:objective" for row in rows))
        # No objective was interpreted, so no objective knowledge is invented.
        self.assertEqual(
            await manager.knowledge.records(game=GAME, category=KnowledgeCategory.OBJECTIVE_PATTERN),
            [],
        )
        # The failure is session memory's business: it explains why this run
        # learned less, and it was not offered to the knowledge service.
        failures = manager.session_memory.rows()["failures"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["reason_code"], "objective_interpretation_failed")
        self.assertIn("interpreter exploded", failures[0]["reason"])
        self.assertFalse(manager.session_memory.rows()["persisted"])

    async def test_a_manager_without_a_memory_wiring_still_keeps_knowledge_in_process(self) -> None:
        manager = self.manager()
        self.assertIsInstance(manager.knowledge.memory, MemoryManager)
        self.assertIsInstance(manager.knowledge.memory.repository, InMemoryMemoryStore)
        recorded = await manager.record_knowledge(candidate())
        self.assertTrue(recorded["recorded"])
        self.assertEqual((await manager.knowledge.records(game=GAME))[0].key, "hud:hp")


class KnowledgeApiTests(_IsolatedApiTestCase):
    """The HTTP surface: scoped reads, explicit writes, honest refusals."""

    def _payload(self, **overrides: Any) -> dict[str, Any]:
        payload = {
            "game": GAME,
            "category": "hud",
            "key": "hud:health",
            "content": {"label": "Health", "value": 90},
            "game_version": "1",
            "provenance": "test payload",
            "evidence": [evidence("api-1").to_dict()],
        }
        payload.update(overrides)
        return payload

    def test_status_is_available_before_anything_is_known(self) -> None:
        body = self._client.get("/novaforge/knowledge").json()
        self.assertEqual(body["knowledge"]["schema_version"], "nf18.1")
        self.assertEqual(body["knowledge"]["counts"]["received"], 0)
        self.assertEqual(body["summary"]["records"], 0)
        self.assertFalse(body["session_memory"]["persisted"])

    def test_records_require_a_game_and_return_an_empty_scoped_list(self) -> None:
        missing = self._client.get("/novaforge/knowledge/records")
        self.assertEqual(missing.status_code, 422, missing.text)
        body = self._client.get(
            "/novaforge/knowledge/records", params={"game": GAME}
        ).json()
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["records"], [])

    def test_a_candidate_is_recorded_with_its_evidence_and_promoted_by_a_second_one(self) -> None:
        first = self._client.post("/novaforge/knowledge", json=self._payload())
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        self.assertTrue(body["recorded"])
        self.assertEqual(body["record"]["status"], "provisional")
        self.assertEqual(len(body["record"]["evidence"]), 1)
        second = self._client.post(
            "/novaforge/knowledge",
            json=self._payload(evidence=[evidence("api-2").to_dict()]),
        ).json()
        self.assertEqual(second["record"]["status"], "verified")
        self.assertTrue(second["record"]["last_verified_at"])
        listed = self._client.get(
            "/novaforge/knowledge/records", params={"game": GAME}
        ).json()
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["records"][0]["independent_evidence"], 2)

    def test_an_invalid_candidate_is_a_422_with_the_reason(self) -> None:
        for payload in (
            self._payload(category="lore"),
            self._payload(game=""),
            self._payload(content="text"),
            self._payload(evidence=[{}]),
        ):
            response = self._client.post("/novaforge/knowledge", json=payload)
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(
            self._client.get("/novaforge/knowledge").json()["knowledge"]["counts"]["received"],
            0,
        )

    def test_revalidation_requires_evidence_and_reports_unknown_ids(self) -> None:
        unknown = self._client.post(
            "/novaforge/knowledge/k-missing/revalidate",
            json={"game": GAME, "evidence": [evidence("api-9").to_dict()]},
        )
        self.assertEqual(unknown.status_code, 200, unknown.text)
        self.assertFalse(unknown.json()["revalidated"])
        empty = self._client.post(
            "/novaforge/knowledge/k-missing/revalidate", json={"game": GAME}
        )
        self.assertEqual(empty.status_code, 422, empty.text)
        invalid = self._client.post(
            "/novaforge/knowledge/k-missing/revalidate",
            json={"game": GAME, "evidence": [{"source": "rumour", "summary": "x"}]},
        )
        self.assertEqual(invalid.status_code, 422, invalid.text)

    def test_the_panel_card_is_served_with_its_controls(self) -> None:
        html = self._client.get("/").text
        for marker in (
            'id="forgeKnowledgeBadge"',
            'id="forgeKnowledgeGame"',
            'id="forgeKnowledgeRefreshButton"',
            'id="forgeKnowledgeHint"',
            'id="forgeKnowledgeFacts"',
            'id="forgeKnowledgeList"',
            'id="forgeKnowledgeQuestions"',
            'id="forgeKnowledgeDue"',
            'id="forgeKnowledgeHistory"',
        ):
            self.assertIn(marker, html)
        self.assertIn("Session memory", html)

    def test_a_contradiction_is_visible_and_neither_claim_is_called_verified(self) -> None:
        self._client.post("/novaforge/knowledge", json=self._payload())
        second = self._client.post(
            "/novaforge/knowledge",
            json=self._payload(
                content={"label": "Health", "value": 40},
                evidence=[evidence("api-2").to_dict()],
            ),
        ).json()
        self.assertEqual(second["record"]["status"], "provisional")
        self.assertEqual(len(second["record"]["contradicts"]), 1)
        summary = self._client.get(
            "/novaforge/knowledge", params={"game": GAME}
        ).json()["summary"]
        self.assertEqual(summary["records"], 2)
        self.assertEqual(summary["verified"], 0)
        self.assertEqual(summary["contradicted"], 1)


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class KnowledgePanelLogicTests(unittest.TestCase):
    """The card's rows and sentences are pure functions, run in Node without a DOM."""

    NEEDLE = """
const fs = require('fs');
global.window = {};
global.document = { getElementById: function () { return null; } };
eval(fs.readFileSync(process.argv[2], 'utf8'));
const L = global.window.NovaForgeLogic;
if (!L) { console.log(JSON.stringify({ error: 'NovaForgeLogic missing' })); process.exit(0); }
const record = {
  knowledge_id: 'k-1', game: 'NC FORGE MISSION', category: 'hud', key: 'hud:objective',
  status: 'contradicted', game_version: '1', independent_evidence: 2,
  updated_at: '2026-10-25T10:00:00+00:00',
  reason: 'a different claim was observed for hud:objective',
  history: [
    { at: '2026-10-25T09:59:00+00:00', from: 'provisional', to: 'verified',
      reason: '2 independent observations agree' },
    { at: '2026-10-25T10:00:00+00:00', from: 'verified', to: 'contradicted',
      reason: 'a different claim was observed' },
  ],
};
const verified = Object.assign({}, record, { status: 'verified', key: 'hud:health' });
const question = Object.assign({}, record, { category: 'question', key: 'question:objective',
  status: 'provisional', reason: 'the objective could not be established' });
const body = {
  game: 'NC FORGE MISSION',
  knowledge: { schema_version: 'nf18.1', persistence: 'novacontrol memory store',
    last_error: { reason_code: 'obsolete_session', reason: 'that session ended' } },
  summary: { records: 3, verified: 1, provisional: 1, contradicted: 1, stale: 0,
    unresolved_questions: 1, revalidate_after_days: 30,
    due_for_revalidation: [{ knowledge_id: 'k-2', key: 'hud:health', status: 'verified', age_days: 41.2 }] },
  session_memory: { persisted: false, observations: [{ observation_id: 'o1' }],
    objectives: [{ observation_id: 'o1' }] },
  records: [record, verified, question],
};
const out = {
  row: L.knowledgeRecordRow(record),
  plainRow: L.knowledgeRecordRow(verified),
  single: L.knowledgeRecordRow(Object.assign({}, verified, { independent_evidence: 1 })),
  facts: L.knowledgeFacts(body),
  noFacts: L.knowledgeFacts(null),
  hints: {
    untested: L.knowledgeHint(null),
    noGame: L.knowledgeHint({ game: '', summary: { games: ['NC FORGE MISSION'] }, knowledge: {} }),
    empty: L.knowledgeHint({ game: 'NC FORGE MISSION', summary: { records: 0 }, knowledge: {} }),
    current: L.knowledgeHint(body),
    error: L.knowledgeHint({ game: 'NC FORGE MISSION', error: 'the store is down' }),
  },
  history: L.knowledgeHistoryRows([record, verified]),
  emptyHistory: L.knowledgeHistoryRows([]),
};
console.log(JSON.stringify(out));
"""

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(cls.NEEDLE)
            cls._driver = Path(handle.name)
        completed = subprocess.run(
            ["node", str(cls._driver), str(Path("src/novacontrol/web/static/js/novaforge.js"))],
            capture_output=True,
            timeout=60,
            check=False,
        )
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        if completed.returncode != 0:
            raise AssertionError(f"node failed: {stderr[:800]}")
        try:
            cls.data = json.loads(stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:  # pragma: no cover - harness failure
            raise AssertionError(f"driver produced no JSON: {stdout[:400]!r}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        cls._driver.unlink(missing_ok=True)

    def test_a_row_names_its_evidence_status_and_need_for_revalidation(self) -> None:
        row = self.data["row"]
        self.assertIn("hud · hud:objective", row)
        self.assertIn("contradicted", row)
        self.assertIn("2 observations", row)
        self.assertIn("version 1", row)
        self.assertIn("a different claim was observed", row)
        self.assertIn("needs revalidation", row)
        self.assertIn("updated ", row)
        self.assertIn("verified → contradicted", row)
        self.assertIn("1 observation ·", self.data["single"])
        self.assertNotIn("needs revalidation", self.data["plainRow"])

    def test_facts_keep_session_memory_apart_from_stored_records(self) -> None:
        rows = {row[0]: row[1] for row in self.data["facts"]}
        self.assertEqual(rows["Game"], GAME)
        self.assertEqual(rows["Records"], "3")
        self.assertEqual(rows["Verified"], "1")
        self.assertEqual(rows["Contradicted"], "1")
        self.assertEqual(rows["Open questions"], "1")
        self.assertIn("1 due", rows["Revalidation"])
        self.assertIn("30 days", rows["Revalidation"])
        self.assertIn("not persisted", rows["Session memory"])
        self.assertEqual(rows["Persistence"], "novacontrol memory store")
        self.assertIn("obsolete_session", rows["Last error"])
        never = {row[0]: row[1] for row in self.data["noFacts"]}
        self.assertEqual(never["Game"], "not selected")
        self.assertEqual(never["Records"], "not read")
        self.assertEqual(never["Last error"], "none")

    def test_the_hint_says_which_scope_is_missing_and_never_promises_more(self) -> None:
        hints = self.data["hints"]
        self.assertIn("No game knowledge has been read", hints["untested"])
        self.assertIn("Known games: NC FORGE MISSION", hints["noGame"])
        self.assertIn("No knowledge is stored", hints["empty"])
        self.assertIn("3 record(s)", hints["current"])
        self.assertIn("1 due for revalidation", hints["current"])
        self.assertIn("Session memory is not part of these records", hints["current"])
        self.assertIn("could not be read: the store is down", hints["error"])

    def test_history_rows_name_both_statuses_and_the_reason(self) -> None:
        history = self.data["history"]
        self.assertEqual(len(history), 4)
        self.assertIn("provisional → verified", history[0])
        self.assertIn("2 independent observations agree", history[0])
        self.assertIn("verified → contradicted", history[-1])
        self.assertEqual(self.data["emptyHistory"], [])


class KnowledgePanelStaticTests(unittest.TestCase):
    """The card retrieves knowledge through the real routes and never writes from a click."""

    def test_the_panel_reads_the_scoped_routes_and_refreshes_after_a_state_read(self) -> None:
        source = Path("src/novacontrol/web/static/js/novaforge.js").read_text(encoding="utf-8")
        self.assertIn('"/novaforge/knowledge"', source)
        self.assertIn('"/novaforge/knowledge/records"', source)
        self.assertIn("loadKnowledge();", source)
        # The state reading ingests knowledge, so its refresh must repaint the
        # card: a stale knowledge card beside a fresh state is a disagreement.
        analyze = source.split("async function analyzeGameState", 1)[1].split(
            "async function resetGameState", 1
        )[0]
        self.assertIn("await loadKnowledge()", analyze)

    def test_the_card_is_read_only(self) -> None:
        source = Path("src/novacontrol/web/static/js/novaforge.js").read_text(encoding="utf-8")
        start = source.index("async function loadKnowledge")
        end = source.index("/* ── Actions", start)
        body = source[start:end]
        self.assertIn('requestJson("/novaforge/knowledge"', body)
        self.assertNotIn("method:", body)
        self.assertNotIn('"/command', body)
