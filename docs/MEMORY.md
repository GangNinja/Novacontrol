# Memory System

The Phase 3 memory subsystem gives NovaControl a production-shaped memory boundary with local durable storage and future vector database support. Phase 32 adds a structured personal-knowledge model **over the same** `MemoryManager` and `SqliteMemoryStore`; it does not add a second database.

## Namespaces

- `conversation`: conversational context and preferences
- `project`: project state, milestones, and decisions
- `knowledge`: durable research and reference knowledge
- `short_term`: expiring working memory
- `long_term`: durable user and system memory; Phase 32 personal records use `pk:<id>` and relations `pkr:<id>` here

## Components

- `MemoryRecord`: typed immutable record with metadata, importance, timestamps, and optional expiration
- `MemoryManager`: high-level remember, recall, retrieve, summarize, cleanup, list and forget API
- `MemoryModule`: event-driven runtime module for memory write and cleanup events
- `InMemoryMemoryStore`: fast process-local store for tests and embedded use
- `SqliteMemoryStore`: local durable store using the Python standard library
- `VectorIndex`: interface for vector database adapters
- `HashingVectorIndex`: dependency-free lexical similarity index for development and tests
- `ExtractiveMemorySummarizer`: deterministic summarizer used until an LLM summarizer is configured
- `PersonalKnowledgeService` (Phase 32): structured personal records, evidence/provenance, conflicts, versions, relations, privacy, retention, consolidation, retrieval/context, export and deletion.

## Phase 32 personal knowledge

Each personal record is a JSON `MemoryRecord.value` in `long_term`, keyed by
`pk:<id>`. The service rebuilds its in-memory query index from
`MemoryRepository.list_records("long_term", "pk:")` at startup and keeps SQLite
as the source of truth. The same repository now supports `list_records` with a
literal prefix and `delete_record`; `MemoryManager` exposes `list_records` and
`forget`.

The structured model adds category, kind, status, subject/predicate, project,
entities, source/provenance, confidence, verified flag, validity window,
retention, privacy, version/history and supersession/merge lineage. Facts from
evidence-bearing sources can be current; inference/observation/assumption remain
unverified. Corrections and supersession preserve history; unresolved
contradictions keep both sides and require explicit resolution. Similar records
merge only when identity **and** normalized content match, with a recoverable
batch and provenance.

Retrieval is budgeted and scoped. Default results are current; unverified,
contradicted, historical and expired records require explicit opt-in. Record
identity includes the project, so the same field/value in two projects cannot
supersede or contradict the other project. Owner bulk access sees
private/personal records, sensitive only with opt-in; an explicit by-id owner
control operation can inspect/correct/delete a sensitive record. Agent access
never sees private or sensitive records, and project-scoped records require an
explicit project. The full architecture, API and limitations are in
[PERSONAL_KNOWLEDGE.md](PERSONAL_KNOWLEDGE.md); acceptance evidence is in
[PHASE32_REPORT.md](PHASE32_REPORT.md).

## Events

- `memory.remember`: writes a memory record
- `memory.stored`: emitted after a record is stored
- `memory.cleanup_requested`: removes expired records
- `memory.cleanup_completed`: emitted after cleanup
- `personal_knowledge.*`: optional lifecycle notifications for remembered,
  updated, conflicted, corrected, resolved, consolidated and deleted records.
  These are observational only; persistence never depends on a subscriber.

## Future Adapters

The vector interface is intentionally narrow so Chroma, pgvector, Redis, FAISS, or cloud vector stores can be added without changing the manager or core runtime. Phase 32 retrieval is deterministic lexical/entity/graph scoring and does not silently introduce a second embedding index.
