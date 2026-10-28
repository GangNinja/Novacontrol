"""NF-18 — the knowledge service: durable, game-scoped, evidence-first.

This layer decides what an observation is ALLOWED to become. It composes the
memory subsystem that already exists (``MemoryManager`` + ``SqliteMemoryStore``)
rather than opening a second store: one namespace per game, one row per claim,
and an index of the games it knows.

Rules, in the order they matter:

* **Game identity is the scope.** A read never crosses games, and a record is
  written under the game it was observed in.
* **Evidence decides status.** A record starts provisional; it becomes verified
  only when independent evidence reaches the declared minimum (and, for a
  control mapping, only when the expected EFFECT was observed). No score is
  invented and none is called a probability.
* **Contradictions are preserved.** A different claim in the same slot is a
  second record, and the earlier one is marked contradicted with a reason. A
  write never replaces a claim.
* **Version mismatch is the one automatic staleness.** Age alone never
  invalidates a record; revalidation is explicit and needs fresh evidence.
* **A write can never fail a reading.** Every failure is recorded in
  ``last_error`` and returns instead of raising.
* **Session memory is not an input.** Only explicit candidates are durable.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from novacontrol.memory import MemoryManager

from .knowledge import (
    MAX_CONTENT_ITEMS,
    MAX_EVIDENCE,
    MAX_RECORDS_PER_GAME,
    MIN_VERIFIED_EVIDENCE,
    SCHEMA_VERSION,
    KnowledgeCandidate,
    KnowledgeCategory,
    KnowledgeEvidence,
    KnowledgeRecord,
    KnowledgeStatus,
    RevalidationPolicy,
    VerificationMethod,
    game_slug,
    objective_candidates,
    state_candidates,
    transition,
)
from .objectives import ObjectiveState
from .state import GameState

__all__ = [
    "KNOWLEDGE_INDEX",
    "KNOWLEDGE_NAMESPACE_PREFIX",
    "KnowledgeService",
]

#: One namespace per game; the suffix is a digest, so no game name is a path.
KNOWLEDGE_NAMESPACE_PREFIX = "game_knowledge:"
#: The registry of game names, so a restart can list what it knows.
KNOWLEDGE_INDEX = "game_knowledge_index"

DEFAULT_MIN_VERIFIED_EVIDENCE = MIN_VERIFIED_EVIDENCE
DEFAULT_MAX_RECORDS = MAX_RECORDS_PER_GAME
DEFAULT_MAX_EVIDENCE = MAX_EVIDENCE
#: Records older than this are REPORTED as due for revalidation; nothing is
#: marked stale because a clock moved.
DEFAULT_REVALIDATE_AFTER_DAYS = 30

#: Higher is stronger. Used only to order a display, never to change a status.
_STATUS_RANK = {
    KnowledgeStatus.VERIFIED: 4,
    KnowledgeStatus.PROVISIONAL: 3,
    KnowledgeStatus.UNVERIFIED: 2,
    KnowledgeStatus.STALE: 1,
    KnowledgeStatus.CONTRADICTED: 0,
}


class Publish(Protocol):
    def __call__(self, event_type: str, payload: dict[str, Any]) -> Any: ...


@dataclass
class KnowledgeService:
    """Bounded, game-scoped knowledge over the application's memory store."""

    memory: MemoryManager
    publish: Publish | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    #: Tells the service whether evidence belongs to the live session. An
    #: obsolete session's evidence is refused rather than recorded as history.
    session_validator: Callable[[str], bool] | None = None
    min_verified_evidence: int = DEFAULT_MIN_VERIFIED_EVIDENCE
    max_records_per_game: int = DEFAULT_MAX_RECORDS
    max_evidence: int = DEFAULT_MAX_EVIDENCE
    revalidate_after_days: int = DEFAULT_REVALIDATE_AFTER_DAYS
    _cache: dict[str, dict[str, KnowledgeRecord]] = field(
        default_factory=dict, init=False, repr=False
    )
    _games: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _received: int = field(default=0, init=False, repr=False)
    _duplicates: int = field(default=0, init=False, repr=False)
    _rejected: int = field(default=0, init=False, repr=False)
    _promoted: int = field(default=0, init=False, repr=False)
    _contradicted: int = field(default=0, init=False, repr=False)
    _stale: int = field(default=0, init=False, repr=False)
    _failed: int = field(default=0, init=False, repr=False)
    _durations: list[float] = field(default_factory=list, init=False, repr=False)
    _last_error: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _loaded: bool = field(default=False, init=False, repr=False)
    _stopped: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.min_verified_evidence = max(1, int(self.min_verified_evidence))
        self.max_records_per_game = max(1, int(self.max_records_per_game))
        self.max_evidence = max(1, int(self.max_evidence))
        self.revalidate_after_days = max(0, int(self.revalidate_after_days))

    # ── persistence ────────────────────────────────────────────────────────
    @staticmethod
    def _namespace(game: str) -> str:
        return f"{KNOWLEDGE_NAMESPACE_PREFIX}{game_slug(game)}"

    async def _load_index(self) -> None:
        if self._loaded:
            return
        try:
            rows = await self.memory.retrieve(KNOWLEDGE_INDEX, "", limit=1)
        except Exception as exc:  # noqa: BLE001 - an unreadable index is not a crash
            # NOT marked loaded: a transient failure must not hide every game for
            # the rest of the process. The next read tries again.
            self._record_error("index_unreadable", f"The knowledge index could not be read: {exc}")
            return
        games: list[str] = []
        for row in rows:
            value = getattr(getattr(row, "record", None), "value", None)
            if isinstance(value, Mapping):
                games = [str(item) for item in value.get("games") or [] if str(item).strip()]
        # MERGE, do not replace: games saved in this process while the index was
        # unreadable must not disappear when it becomes readable again. Game
        # identity is case-insensitive, so a stored casing variant is one game.
        for game in games:
            self._games.setdefault(game, self._namespace(game))
        self._dedupe_games()
        self._loaded = True

    def _dedupe_games(self) -> None:
        seen: set[str] = set()
        for game in list(self._games):
            key = game.casefold()
            if key in seen:
                self._games.pop(game, None)
                continue
            seen.add(key)

    async def _save_index(self, game: str) -> None:
        for known in list(self._games):
            if known.casefold() == game.casefold() and known != game:
                self._games.pop(known, None)
        self._games[game] = self._namespace(game)
        try:
            await self.memory.remember(
                KNOWLEDGE_INDEX,
                "games",
                {"games": sorted(self._games)},
                text="known games",
                metadata={"kind": "index"},
                importance=1.0,
            )
        except Exception:  # noqa: BLE001 - the index is auxiliary
            return

    async def load_game(self, game: str) -> dict[str, KnowledgeRecord]:
        """Read one game's records from the store, once, then serve from cache."""
        await self._load_index()
        namespace = self._namespace(game)
        cached = self._cache.get(namespace)
        if cached is not None:
            return cached
        records: dict[str, KnowledgeRecord] = {}
        try:
            rows = await self.memory.retrieve(namespace, "", limit=self.max_records_per_game)
        except Exception as exc:  # noqa: BLE001 - a broken store means "unknown"
            self._record_error("store_unreadable", f"Knowledge could not be read: {exc}")
            rows = []
        for row in rows:
            value = getattr(getattr(row, "record", None), "value", None)
            if not isinstance(value, Mapping):
                continue
            record = KnowledgeRecord.from_dict(value)
            # The store is shared; a row from another game is never adopted.
            # Casing is display, not identity: NF-16 reads it from a window title.
            if record is None or record.game.casefold() != game.casefold():
                continue
            records[record.knowledge_id] = record
        self._cache[namespace] = records
        return records

    async def _persist(self, record: KnowledgeRecord) -> None:
        await self.memory.remember(
            self._namespace(record.game),
            record.knowledge_id,
            record.to_dict(),
            text=f"{record.category.value} {record.key} {record.status.value}",
            metadata={
                "game": record.game,
                "category": record.category.value,
                "status": record.status.value,
                "knowledge_id": record.knowledge_id,
            },
            importance=float(_STATUS_RANK[record.status]),
        )
        self._cache.setdefault(self._namespace(record.game), {})[record.knowledge_id] = record

    # ── recording ──────────────────────────────────────────────────────────
    async def record(self, candidate: KnowledgeCandidate | None) -> KnowledgeRecord | None:
        """Accept one candidate, or explain why it was refused. Never raises."""
        started = time.monotonic()
        result: KnowledgeRecord | None = None
        try:
            result = await self._record(candidate)
        except Exception as exc:  # noqa: BLE001 - knowledge must not fail a reading
            self._failed += 1
            self._record_error("record_failed", f"{type(exc).__name__}: {exc}", context="record")
        finally:
            self._durations.append(round((time.monotonic() - started) * 1000.0, 3))
            del self._durations[:-50]
        return result

    async def _record(self, candidate: KnowledgeCandidate | None) -> KnowledgeRecord | None:
        if self._stopped:
            self._refuse("service_stopped", "The knowledge service is shut down.")
            return None
        if not isinstance(candidate, KnowledgeCandidate):
            self._refuse("invalid_candidate", "The object is not a knowledge candidate.")
            return None
        if not candidate.game.strip() or not candidate.key.strip() or not candidate.content:
            self._refuse("invalid_candidate", "A candidate needs a game, a key and content.")
            return None
        if len(candidate.key) > 120 or len(candidate.content) > MAX_CONTENT_ITEMS:
            self._refuse(
                "invalid_candidate",
                f"A candidate key is limited to 120 characters and {MAX_CONTENT_ITEMS} "
                "content fields.",
            )
            return None
        for evidence in candidate.evidence:
            # Evidence belongs to the game it names. An empty game is "not
            # stated" (a declared profile, a manual note); a DIFFERENT game is a
            # provenance lie and never supports this claim.
            if evidence.game and evidence.game.casefold() != candidate.game.casefold():
                self._refuse(
                    "evidence_game_mismatch",
                    f"Evidence from {evidence.game!r} cannot support a claim about "
                    f"{candidate.game!r}.",
                )
                return None
            if (
                evidence.session_id
                and self.session_validator is not None
                and not self.session_validator(evidence.session_id)
            ):
                self._refuse(
                    "obsolete_session",
                    f"Evidence from session {evidence.session_id!r} is not the current "
                    "session; it was not recorded.",
                )
                return None

        records = await self.load_game(candidate.game)
        existing = records.get(candidate.knowledge_id)
        now = self.clock().isoformat()
        if existing is not None:
            return await self._update(existing, candidate, now=now)
        if len(records) >= self.max_records_per_game:
            self._refuse(
                "record_limit",
                f"{candidate.game} already holds {len(records)} knowledge records; "
                "no new record was added.",
            )
            return None

        record = KnowledgeRecord(
            knowledge_id=candidate.knowledge_id,
            game=candidate.game,
            category=candidate.category,
            key=candidate.key,
            content=dict(candidate.content),
            status=(
                KnowledgeStatus.UNVERIFIED if not candidate.evidence else KnowledgeStatus.PROVISIONAL
            ),
            game_version=candidate.game_version,
            evidence=_bounded_evidence(candidate.evidence, self.max_evidence),
            provenance=candidate.provenance,
            revalidation=candidate.revalidation,
            created_at=now,
            updated_at=now,
            reason=(
                "recorded as provisional evidence"
                if candidate.evidence
                else "recorded without evidence; it cannot be verified"
            ),
            revision=1,
        )

        # A DIFFERENT claim in the same logical slot is a contradiction, not an
        # update: preserve both, and say which challenged which.
        conflicts = [
            row
            for row in records.values()
            if row.key == candidate.key
            and row.category is candidate.category
            and row.game_version == candidate.game_version
            and row.content_digest != record.content_digest
            and row.status
            in (KnowledgeStatus.UNVERIFIED, KnowledgeStatus.PROVISIONAL, KnowledgeStatus.VERIFIED)
        ]
        if conflicts:
            record = KnowledgeRecord(
                knowledge_id=record.knowledge_id,
                game=record.game,
                category=record.category,
                key=record.key,
                content=record.content,
                # A challenge with no evidence is still an unsupported claim; a
                # contradiction does not manufacture the evidence it lacks.
                status=(
                    KnowledgeStatus.PROVISIONAL
                    if record.evidence
                    else KnowledgeStatus.UNVERIFIED
                ),
                game_version=record.game_version,
                evidence=record.evidence,
                provenance=record.provenance,
                revalidation=record.revalidation,
                created_at=record.created_at,
                updated_at=record.updated_at,
                reason=(
                    "recorded as a challenge to "
                    + ", ".join(row.knowledge_id for row in conflicts)
                    + "; a contradiction needs resolution, so it is not verified"
                ),
                revision=record.revision,
                contradicts=tuple(row.knowledge_id for row in conflicts),
                history=record.history,
            )
        else:
            record = self._maybe_verify(record, at=now)
        # The claim itself is stored BEFORE anything is demoted. A refused write
        # therefore leaves the slot exactly as it was, and a failure during the
        # demotions leaves a challenge a reader can see rather than a demotion
        # whose challenger was never written (the memory store has no
        # transaction that could make both writes one unit).
        await self._persist(record)
        for conflict in conflicts:
            challenged = transition(
                conflict,
                KnowledgeStatus.CONTRADICTED,
                at=now,
                reason=(
                    f"a different claim was observed for {candidate.key!r} at {now}; "
                    "both are kept and neither is treated as the fact"
                ),
            )
            await self._persist(challenged)
            self._contradicted += 1
            await self._announce(challenged, before=conflict.status, reason=challenged.reason)
        await self._save_index(candidate.game)
        self._received += 1
        if record.status is KnowledgeStatus.VERIFIED:
            # Counted only after the write landed: a counter must not claim a
            # promotion the store refused.
            self._promoted += 1
        await self._announce(record, before="", reason=record.reason)
        # A known configuration version makes older version-bound records stale;
        # an unknown version never does.
        if candidate.game_version:
            await self._stale_other_versions(record, at=now)
        return record

    async def _update(
        self, existing: KnowledgeRecord, candidate: KnowledgeCandidate, *, now: str
    ) -> KnowledgeRecord:
        seen_keys = {item.independent_key for item in existing.evidence}
        seen_fingerprints = {item.fingerprint for item in existing.evidence}
        fresh: list[KnowledgeEvidence] = []
        for evidence in candidate.evidence:
            # The same observation handed over twice is one observation.
            if evidence.independent_key in seen_keys or evidence.fingerprint in seen_fingerprints:
                continue
            seen_keys.add(evidence.independent_key)
            seen_fingerprints.add(evidence.fingerprint)
            fresh.append(evidence)
        if not fresh:
            self._duplicates += 1
            return existing
        combined = tuple([*existing.evidence, *fresh])[: self.max_evidence]
        updated = KnowledgeRecord(
            knowledge_id=existing.knowledge_id,
            game=existing.game,
            category=existing.category,
            key=existing.key,
            content=existing.content,
            status=existing.status,
            game_version=existing.game_version or candidate.game_version,
            evidence=combined,
            provenance=existing.provenance,
            revalidation=existing.revalidation,
            created_at=existing.created_at,
            updated_at=now,
            last_verified_at=existing.last_verified_at,
            reason=f"{len(fresh)} independent observation(s) were added",
            revision=existing.revision + 1,
            contradicts=existing.contradicts,
            content_digest=existing.content_digest,
            history=existing.history,
        )
        before = existing.status
        updated = self._maybe_verify(updated, at=now)
        await self._persist(updated)
        self._received += 1
        if updated.status is KnowledgeStatus.VERIFIED and before is not KnowledgeStatus.VERIFIED:
            self._promoted += 1
        if updated.status is not before:
            await self._announce(updated, before=before, reason=updated.reason)
        return updated

    def _maybe_verify(self, record: KnowledgeRecord, *, at: str) -> KnowledgeRecord:
        """The ONE promotion rule: independent evidence, and a real effect."""
        if record.status is not KnowledgeStatus.PROVISIONAL:
            return record
        if record.category is KnowledgeCategory.QUESTION:
            # A question is the record of NOT knowing; observations cannot close it.
            return record
        if record.independent_evidence < self.min_verified_evidence:
            return record
        if record.category in (KnowledgeCategory.CONTROL, KnowledgeCategory.OUTCOME) and not (
            record.verified_effect
        ):
            # A mapping needs its EFFECT, and an outcome needs explicit outcome
            # evidence; observing the same unsupported reading twice is not proof.
            return record
        if all(item.method is VerificationMethod.INFERRED for item in record.evidence):
            # An inference repeated is still an inference. A configured/declared
            # profile or an observed reading may support verification; a guess may not.
            return record
        if record.contradicts:
            return record
        return transition(
            record,
            KnowledgeStatus.VERIFIED,
            at=at,
            reason=(
                f"{record.independent_evidence} independent observations agree, "
                "with no unresolved contradiction"
            ),
        )

    async def _stale_other_versions(self, record: KnowledgeRecord, *, at: str) -> None:
        records = self._cache.get(self._namespace(record.game)) or {}
        for row in list(records.values()):
            if row.knowledge_id == record.knowledge_id:
                continue
            if row.revalidation is not RevalidationPolicy.VERSION_BOUND:
                continue
            if not row.game_version or not record.game_version:
                continue
            if row.game_version == record.game_version:
                continue
            if row.status not in (
                KnowledgeStatus.UNVERIFIED,
                KnowledgeStatus.PROVISIONAL,
                KnowledgeStatus.VERIFIED,
            ):
                continue
            stale = transition(
                row,
                KnowledgeStatus.STALE,
                at=at,
                reason=(
                    f"recorded under configuration {row.game_version!r}; the current "
                    f"observation reports {record.game_version!r}"
                ),
            )
            await self._persist(stale)
            self._stale += 1
            await self._announce(stale, before=row.status, reason=stale.reason)

    async def record_state(self, state: GameState) -> list[KnowledgeRecord]:
        """Ingest one NF-16 state. Candidates are built by the pure builder."""
        return await self._record_many(state_candidates(state))

    async def record_objectives(self, state: ObjectiveState) -> list[KnowledgeRecord]:
        """Ingest one NF-17 interpretation."""
        return await self._record_many(objective_candidates(state))

    async def _record_many(self, candidates: Iterable[KnowledgeCandidate]) -> list[KnowledgeRecord]:
        results: list[KnowledgeRecord] = []
        for candidate in candidates:
            record = await self.record(candidate)
            if record is not None:
                results.append(record)
        return results

    # ── verification ───────────────────────────────────────────────────────
    async def find(self, knowledge_id: str, *, game: str = "") -> KnowledgeRecord | None:
        await self._load_index()
        games = [game] if game else sorted(self._games)
        for name in games:
            records = await self.load_game(name)
            if knowledge_id in records:
                return records[knowledge_id]
        return None

    async def revalidate(
        self, knowledge_id: str, evidence: Sequence[KnowledgeEvidence], *, game: str = ""
    ) -> dict[str, Any]:
        """Re-check ONE stale/contradicted record with fresh evidence.

        A record that is not stale or contradicted is not revalidated: it already
        has the status its evidence supports. No new evidence means no change.
        """
        try:
            record = await self.find(knowledge_id, game=game)
        except Exception as exc:  # noqa: BLE001 - a read failure is a report
            self._failed += 1
            self._record_error("revalidate_failed", f"{type(exc).__name__}: {exc}")
            return {"revalidated": False, "reason": "the knowledge store could not be read"}
        if record is None:
            return {"revalidated": False, "reason": "unknown knowledge record"}
        if record.status not in (KnowledgeStatus.STALE, KnowledgeStatus.CONTRADICTED):
            return {
                "revalidated": False,
                "reason": f"the record is {record.status.value}, not stale or contradicted",
                "status": record.status.value,
            }
        if not isinstance(evidence, (list, tuple)) or any(
            not isinstance(item, KnowledgeEvidence) for item in evidence
        ):
            return {
                "revalidated": False,
                "reason": "evidence must be a list of evidence references",
                "status": record.status.value,
            }
        foreign = [
            item.game
            for item in evidence
            if item.game and item.game.casefold() != record.game.casefold()
        ]
        if foreign:
            return {
                "revalidated": False,
                "reason": (
                    f"evidence names another game ({sorted(set(foreign))[0]!r}); "
                    f"it cannot support {record.game!r}"
                ),
                "status": record.status.value,
            }
        known_keys = {item.independent_key for item in record.evidence}
        fresh = [
            item
            for item in evidence
            if item.independent_key not in known_keys
            and (
                not item.session_id
                or self.session_validator is None
                or self.session_validator(item.session_id)
            )
        ]
        if not fresh:
            return {
                "revalidated": False,
                "reason": "no new independent evidence from the current session was supplied",
                "status": record.status.value,
            }
        now = self.clock().isoformat()
        updated = KnowledgeRecord(
            knowledge_id=record.knowledge_id,
            game=record.game,
            category=record.category,
            key=record.key,
            content=record.content,
            status=record.status,
            game_version=record.game_version,
            evidence=tuple([*record.evidence, *fresh])[: self.max_evidence],
            provenance=record.provenance,
            revalidation=record.revalidation,
            created_at=record.created_at,
            updated_at=now,
            last_verified_at=record.last_verified_at,
            reason="revalidated with fresh evidence",
            revision=record.revision + 1,
            contradicts=(),
            content_digest=record.content_digest,
            history=record.history,
        )
        before = record.status
        updated = transition(
            updated,
            KnowledgeStatus.PROVISIONAL,
            at=now,
            reason="fresh evidence was observed for a record that needed revalidation",
        )
        updated = self._maybe_verify(updated, at=now)
        await self._persist(updated)
        if updated.status is KnowledgeStatus.VERIFIED:
            # Before this call the record was stale or contradicted, never verified.
            self._promoted += 1
        await self._announce(updated, before=before, reason=updated.reason)
        return {
            "revalidated": True,
            "knowledge_id": updated.knowledge_id,
            "status": updated.status.value,
            "evidence": updated.independent_evidence,
        }

    # ── retrieval ──────────────────────────────────────────────────────────
    async def records(
        self,
        *,
        game: str,
        category: KnowledgeCategory | str | None = None,
        status: KnowledgeStatus | str | None = None,
        key: str = "",
        query: str = "",
        limit: int = 50,
        include_history: bool = True,
    ) -> list[KnowledgeRecord]:
        """Records for ONE game, filtered — never another game's knowledge."""
        if not str(game or "").strip():
            return []
        records = await self.load_game(game)
        try:
            wanted_category = KnowledgeCategory(str(category)) if category else None
            wanted_status = KnowledgeStatus(str(status)) if status else None
        except ValueError:
            return []
        term = str(query or "").strip().casefold()
        found: list[KnowledgeRecord] = []
        for record in records.values():
            if wanted_category is not None and record.category is not wanted_category:
                continue
            if wanted_status is not None and record.status is not wanted_status:
                continue
            if key and record.key != key:
                continue
            if not include_history and record.status in (
                KnowledgeStatus.CONTRADICTED,
                KnowledgeStatus.STALE,
            ):
                continue
            if term:
                # A reader asks for what relates to an observation or objective,
                # so the evidence references are part of the searchable text.
                haystack = " ".join(
                    [
                        record.key,
                        record.category.value,
                        record.provenance,
                        record.game_version,
                        " ".join(str(value) for value in record.content.values()),
                        " ".join(
                            f"{item.summary} {item.observation_id} {item.objective_id} "
                            f"{item.session_id}"
                            for item in record.evidence
                        ),
                    ]
                ).casefold()
                if term not in haystack:
                    continue
            found.append(record)
        found.sort(
            key=lambda row: (_STATUS_RANK[row.status], row.updated_at, row.key), reverse=True
        )
        return found[: max(1, int(limit))]

    async def verified_controls(self, game: str) -> list[KnowledgeRecord]:
        return await self.records(
            game=game, category=KnowledgeCategory.CONTROL, status=KnowledgeStatus.VERIFIED
        )

    async def unresolved_questions(self, game: str) -> list[KnowledgeRecord]:
        return await self.records(game=game, category=KnowledgeCategory.QUESTION)

    async def summary(self, *, game: str = "") -> dict[str, Any]:
        """Counts and diagnostics for one game (or every known game)."""
        await self._load_index()
        games = [game] if game else sorted(self._games)
        rows: list[KnowledgeRecord] = []
        for name in games:
            rows.extend((await self.load_game(name)).values())
        by_status = {status.value: 0 for status in KnowledgeStatus}
        by_category = {category.value: 0 for category in KnowledgeCategory}
        for record in rows:
            by_status[record.status.value] += 1
            by_category[record.category.value] += 1
        now = self.clock()
        due: list[dict[str, Any]] = []
        for record in rows:
            if record.revalidation is RevalidationPolicy.MANUAL:
                continue
            if record.status not in (KnowledgeStatus.VERIFIED, KnowledgeStatus.PROVISIONAL):
                continue
            stamp = record.last_verified_at or record.updated_at or record.created_at
            try:
                recorded = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            age = now - recorded
            if age >= timedelta(days=self.revalidate_after_days):
                due.append(
                    {
                        "knowledge_id": record.knowledge_id,
                        "key": record.key,
                        "status": record.status.value,
                        "age_days": round(age.total_seconds() / 86400.0, 1),
                    }
                )
        return {
            "schema_version": SCHEMA_VERSION,
            "game": game,
            "games": sorted(self._games),
            "records": len(rows),
            "by_status": by_status,
            "by_category": by_category,
            "verified": by_status[KnowledgeStatus.VERIFIED.value],
            "provisional": by_status[KnowledgeStatus.PROVISIONAL.value],
            "unverified": by_status[KnowledgeStatus.UNVERIFIED.value],
            "contradicted": by_status[KnowledgeStatus.CONTRADICTED.value],
            "stale": by_status[KnowledgeStatus.STALE.value],
            "unresolved_questions": by_category[KnowledgeCategory.QUESTION.value],
            "due_for_revalidation": due,
            "revalidate_after_days": self.revalidate_after_days,
            "note": (
                "A record's status is the strength of its evidence, not a probability. "
                "An old record is reported as due for revalidation; nothing is marked "
                "stale because time passed."
            ),
        }

    def status(self) -> dict[str, Any]:
        durations = list(self._durations)
        return {
            "schema_version": SCHEMA_VERSION,
            "available": True,
            "stopped": self._stopped,
            "games": sorted(self._games),
            "counts": {
                "received": self._received,
                "duplicates": self._duplicates,
                "rejected": self._rejected,
                "promoted": self._promoted,
                "contradicted": self._contradicted,
                "stale": self._stale,
                "failed": self._failed,
            },
            "measured": {
                "record_ms": {
                    "last": durations[-1] if durations else None,
                    "average": round(sum(durations) / len(durations), 3) if durations else None,
                }
            },
            "last_error": dict(self._last_error) if self._last_error is not None else None,
            "limits": {
                "max_records_per_game": self.max_records_per_game,
                "max_evidence": self.max_evidence,
                "min_verified_evidence": self.min_verified_evidence,
            },
            "persistence": "novacontrol memory store (SQLite on this host)",
            "limitations": [
                "A record is knowledge read from the screen or an interpretation, not a "
                "game's internal state.",
                "Verification is a declared evidence threshold, not a calibrated probability.",
                "Only one game can be the source of a record; cross-game reuse is not claimed.",
                "Session memory is transient and is never written as knowledge.",
            ],
        }

    # ── lifecycle and helpers ──────────────────────────────────────────────
    async def shutdown(self) -> None:
        self._stopped = True

    async def _announce(self, record: KnowledgeRecord, *, before: str, reason: str) -> None:
        if self.publish is None:
            return
        try:
            result = self.publish(
                "novaforge.knowledge.updated",
                {
                    "knowledge_id": record.knowledge_id,
                    "game": record.game,
                    "category": record.category.value,
                    "key": record.key,
                    "before": before,
                    "after": record.status.value,
                    "reason": reason,
                    "evidence": record.independent_evidence,
                },
            )
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - a lost event cannot fail a write
            return

    def _refuse(self, code: str, reason: str) -> None:
        self._rejected += 1
        self._record_error(code, reason, context="refused")

    def _record_error(self, code: str, reason: str, *, context: str = "") -> None:
        self._last_error = {
            "reason_code": code,
            "reason": reason,
            "context": context,
            "at": self.clock().isoformat(),
        }


def _bounded_evidence(
    evidence: Sequence[KnowledgeEvidence], limit: int
) -> tuple[KnowledgeEvidence, ...]:
    """Bounded, and one entry per independent observation."""
    kept: list[KnowledgeEvidence] = []
    seen: set[str] = set()
    for item in evidence:
        if item.independent_key in seen:
            continue
        seen.add(item.independent_key)
        kept.append(item)
        if len(kept) >= limit:
            break
    return tuple(kept)

