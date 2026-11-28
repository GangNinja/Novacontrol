# Personal Knowledge Model (Phase 32)

Phase 32 adds a structured personal-knowledge layer over the existing Phase 3
memory subsystem. It is **not** a second database and it is **not** a second
memory manager. Every knowledge record is a `MemoryRecord` in the shared
`long_term` namespace, written through the same `MemoryManager` and the same
`SqliteMemoryStore` (`data/memory.sqlite3`) that Phase 3 already owns.

The service adds meaning, provenance, validity, relationships, retention and
user control on top of that durable store. It does not add a model call to
acquisition: only explicit user-authored `remember this:` notes and explicit
API writes enter automatically. A model-generated inference can be stored only
as `kind=observation`/`assumption` and is never promoted to a confirmed fact by
the service.

## Architecture

```text
conversation / explicit API / completed-work evidence
                    |
             PersonalKnowledgeService
       (validation, evidence, conflict, history)
                    |
       MemoryRecord(namespace="long_term", key="pk:<id>")
                    |
          MemoryManager -> SqliteMemoryStore
                    |
               data/memory.sqlite3
```

Relationships use the same namespace and store under `pkr:<id>`; they are not a
separate graph database. The in-process index is a query cache rebuilt from
`MemoryRepository.list_records("long_term", "pk:")` at startup; it is
disposable and never the source of truth.

Key files:

- `src/novacontrol/memory/personal.py` — models and `PersonalKnowledgeService`.
- `src/novacontrol/memory/stores.py` — `list_records` and `delete_record` on the
  existing repository protocol and both implementations.
- `src/novacontrol/memory/manager.py` — thin `list_records` / `forget` doors.
- `src/novacontrol/application.py` — construction, chat acquisition/recall,
  agent access, computer-operation context, skill retrieval context and status.
- `src/novacontrol/api/app.py` — the `/memory/personal*` REST surface.

## Record model

Each `KnowledgeRecord` carries:

| Field | Meaning |
|---|---|
| `id` | Stable record id; the storage key is `pk:<id>` |
| `category` | preference, communication, goal, active/completed project, professional, academic, skill, routine, decision, workflow, relationship, fact, history, other |
| `kind` | fact, preference, decision, observation, assumption |
| `status` | current, historical, superseded, unverified, contradicted, expired, archived, merged |
| `content` | The claim/preference/decision, never only a free-form conversation blob |
| `subject`, `predicate`, `project`, `entities` | Structured retrieval and relationship fields |
| `source_type`, `source` | Provenance, e.g. user/chat, application, execution, document, inference |
| `confidence` | Evidence-derived score; a supplied value is clamped and assumptions/observations are capped |
| `verified` | Whether the stated source is evidence-bearing; assumptions and inferences are false |
| `created_at`, `updated_at`, `last_verified_at` | Lifecycle timestamps |
| `valid_from`, `valid_until`, `retention` | Validity and retention policy |
| `version`, `history` | Immutable prior versions, so correction/supersession does not erase history |
| `supersedes`, `superseded_by`, `merged_into` | Evolution lineage |
| `privacy` | public, personal, private, sensitive |
| `metadata` | Small provenance/consolidation annotations; not a substitute for fields |

Default source confidence (before kind caps):

```text
correction 0.99, user 0.96, application 0.95, execution 0.93,
document 0.88, task 0.86, conversation 0.72, agent 0.62, inference 0.32
```

`kind=assumption` is capped at 0.35; `kind=observation` at 0.72. A record is
`verified` only for the evidence-bearing source set and never for an assumption.
The default status is `current` when verified and `unverified` otherwise.

## Acquisition policy

The service accepts acquisition only through:

1. An explicit `remember this:`, `remember that:` or `remember:` request in chat.
   The existing `teach_knowledge` path still runs for the Learn tab; the same
   explicit request also creates a structured record with `source="chat"`.
2. An explicit `POST /memory/personal` call with content and source/kind stated.
3. A caller that has genuine source evidence (application, execution, task,
   document) and supplies the source type and provenance.

The service refuses:
- blank content;
- an assumption whose source is not inference/agent;
- an inference promoted to a user/verified fact by omission;
- a malformed category/kind/privacy/retention value.

It never saves every conversation turn. Conversation memory remains a separate
`conversation` namespace with its own importance and retrieval semantics.

### Duplicate and conflict rules

A record's identity is `(normalized project, normalized subject, normalized predicate, category)`.
When no predicate was declared, identity falls back to
`(subject, content:<normalized content>, category)`, because two arbitrary facts
about the same user must not be treated as contradictory merely because they
share a category. Contradiction detection therefore needs a declared field
(`database`, `timezone`, `reporting_style`, ...).

- Exact duplicate content with the same identity returns `duplicate` and updates
  `last_verified_at`/`updated_at` on the existing record.
- A verified record that matches an unverified record's content promotes it.
- An explicit `correction` or `supersedes=[id]` replaces the old record and keeps
  the old record as `superseded` with history.
- Two verified records that disagree without explicit replacement become
  `contradicted`; the incoming record is retained too, and nothing is selected
  as the answer. The user resolves it explicitly.
- An unverified incoming record that conflicts with verified knowledge is
  retained as `contradicted`, never promoted over the evidence.

## Retrieval

`PersonalKnowledgeService.retrieve`/`context_for` combine:

- exact content and subject/entity matches;
- content-term overlap with a multiword relevance gate;
- project scope;
- current validity (expired/future records are excluded by default);
- status (unverified, contradicted, historical and expired are opt-in);
- confidence;
- recency decay;
- relation expansion for connected records;
- explicit limits and a token/word budget for model context.

Default retrieval returns only `current` records. Historical and superseded
records are available through `include_historical=true`; contradicted records
through `include_contradicted=true`; expired records through
`include_expired=true`; unverified records through `include_unverified=true`.
`context_for` never injects the whole store: it stops at its budget and reports
the number of omitted hits. A hit that does not fit is omitted rather than
exceeding the caller's ceiling; if nothing fits, the context text is empty and
`tokens` is zero.

`KnowledgeQuery` is the typed query shape; the service's method signature is the
public door used by the application and API.

## Personal knowledge graph

A `KnowledgeRelation` records `source_id`, `predicate`, optional `target_id` or
`target_label`, target type, provenance, confidence and timestamps. Relations
are stored under `pkr:<relation_id>` in the same `long_term` namespace.

`relate()` deduplicates by `(source, predicate, target)` and updates confidence
instead of creating parallel edges. `graph(id, depth)` performs bounded
traversal over relation edges, filters every node through the same privacy
access check, and returns nodes, edges and entity labels. Deleting a record also
deletes the relations that point to or from it.

## Evolution, correction and consolidation

- `correct(id, content, reason)` keeps the same id, increments `version`, adds
  the prior version to `history`, records the correction source, and resolves
  other live records with the same identity (`superseded`).
- `resolve_conflict(id, keep=True/False, reason)` makes one side current or
  archives the selected side, and supersedes the other contradicted side. Keeping
  a side records `source_type=correction` / `source="user conflict resolution"`
  so an unverified original is not silently promoted without provenance. It is
  an explicit operator action, not an automatic majority vote.
- `consolidate()` groups only records with the same identity **and** identical
  normalized content. It keeps the highest-evidence record, unions entities,
  stores `consolidation_batch` and `consolidated_from` on the canonical record,
  marks duplicates `merged` with `merged_into` and retains their source text.
  `undo_consolidation(batch_id)` restores the merged records.
- `cleanup(now=...)` applies validity/retention. Long-term expired records become
  `expired`; ephemeral/standard records become `archived`. They remain
  recoverable; cleanup does not silently erase history.

## Privacy and user control

`KnowledgeAccess` separates the owner from an agent:

- owner: sees public/personal/private by default; bulk search/export of
  sensitive records needs `allow_sensitive`, while an explicit by-id
  inspect/correct/delete/resolve/relate operation is an owner control action;
- agent: never sees private or sensitive; personal only when
  `allow_personal` is true; project-scoped records require an explicit project
  (an agent with no project gets only global records).

Retrieval, inspection, graph traversal and export all use the same visibility
check. Agent requests receive only permitted records, and the application
records the access posture alongside the task metadata.

User operations:

- `GET /memory/personal/status` — counts, categories, privacy classes, load/error
  state; never content.
- `GET /memory/personal?query=...` — search with explicit opt-ins for
  unverified/historical/contradicted/expired/sensitive.
- `POST /memory/personal` — acquire; a detected contradiction returns HTTP 409
  with the inspectable conflict (nothing is silently selected).
- `GET /memory/personal/{id}` — record, history and direct relations.
- `POST /memory/personal/{id}/correct` — correction with prior version.
- `POST /memory/personal/{id}/delete` — real deletion of the record and its
  relations from the shared store.
- `POST /memory/personal/{id}/resolve` — explicit conflict resolution.
- `GET /memory/personal/export` — owner export; sensitive records require
  `include_sensitive=true`.
- `POST /memory/personal/consolidate`, `POST /memory/personal/cleanup` —
  maintenance with provenance retained.
- `POST /memory/personal/relations` — provenance-bearing relationship edge.

The API reference is generated from `ApiSurface`; the Phase 32 surface adds 11
routes to the existing application, bringing the reference to **445 routes**.

## Integration points

- **Chat** — `handle_request` retrieves a budgeted personal context for the
  request and passes it in `BrainRequest.context["personal_knowledge_context"]`;
  `NovaBrain._build_chat_messages` places it in a separate labelled system
  message. Chat's explicit `remember this:` path writes both taught knowledge and the
  structured record; memory recall checks the structured layer first.
- **Phase 27 continual learning** — personal facts/preferences remain in the
  long-term knowledge layer; validated execution strategies remain in the
  separate continual-learning corpus. Neither is silently promoted into the
  other. Skill retrieval can use personal context, but it does not change a
  learned strategy or grant permissions.
- **Phase 28 agents** — `_handle_agent` retrieves with
  `KnowledgeAccess.agent(project=request project)`, attaches the permitted rows
  to `AgentTask.metadata["personal_knowledge"]`, and the shared
  `SpecialistPipeline` adds them to the run's context text. Private/sensitive
  records and other-project records are absent; without a declared project an
  agent receives no project-scoped records.
- **Phase 29 computer operation** — `plan_computer_task` and
  `execute_computer_task` add a bounded personal context for the objective to
  the plan/run context. It is context, not approval; the existing single-use
  token, permission and audit gates are unchanged.
- **Phase 30 persistent autonomy** — objective tasks execute through the
  ordinary request path, which receives the same personal context; a task state
  still takes precedence over old memory. No memory record can bypass a
  permission or overwrite a newer task state.
- **Phase 31 skills** — `retrieve_skills_with_context` keeps the skill catalog
  and personal knowledge distinct while returning both to the caller; a skill's
  declared tools and permissions remain the execution authority.

## Verification assets

- `tests/test_phase32_memory.py` — model, acquisition, retrieval, evolution,
  consolidation, privacy, persistence, application and cross-phase seams.
- `tests/test_phase32_api.py` — real FastAPI routes, HTTP status behavior,
  correction/export/deletion and sensitive filtering.
- `tmp/phase32/verify_phase32.py` — live scenarios A–G against real temporary
  SQLite stores and fresh application instances.
- `tmp/phase32/evidence.json` — machine-readable live results and benchmark.
- `docs/PHASE32_REPORT.md` — requirement-by-requirement acceptance evidence.

## Known limitations

- Retrieval is deterministic lexical/entity/relationship scoring. It does not
  call an external embedding model; the existing optional knowledge-index
  embeddings are not silently duplicated into this layer. This keeps ordinary
  tasks responsive and local, but true synonym-level semantic matching is not
  claimed.
- Automatic acquisition from connected applications and approved document
  ingestion is an API/source-type capability, not a new connector; a connector
  must supply its evidence explicitly.
- Contradiction detection needs a declared predicate. Two arbitrary facts with
  no shared field are retained independently rather than guessed to conflict.
- Consolidation needs duplicate records to exist (for example imported legacy
  rows or concurrent writes); the public `remember` door deduplicates exact
  content before it can create such a pair.
- The live scenarios restart the application process, not the operating system.
  OS-level restart and a multi-hour soak were not performed.
- No personal-knowledge panel exists yet; the routes are API/CLI surfaces
  (`no-render` in the route-consumer contract) until a UI is authorized.
