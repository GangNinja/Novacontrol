# Local Knowledge & Project Awareness (Phase 11)

NovaControl can read what is on this machine — documentation, notes, source code,
PDFs — and answer from it. It can also tell you, without being told, which code
project you are standing in and what is going on there.

Both are local. No document is uploaded, no embedding model is required, and
nothing in this subsystem writes to your files.

Everything lives in `src/novacontrol/knowledge/`:

| Module | What it owns |
| --- | --- |
| `models.py` | `KnowledgeSource`, `KnowledgeChunk`, `KnowledgeHit`, `KnowledgeContext`, `IngestReport`, the token estimate |
| `extract.py` | file → text, or an honest reason why not (encoding fallbacks, binary sniff, the PDF extractor ladder) |
| `chunking.py` | text → retrievable pieces (Markdown structure, code definitions, prose paragraphs) |
| `index.py` | the hybrid index: BM25 postings for every chunk, plus optional vectors |
| `retrieve.py` | reranking (priority, recency, deduplication) and the token budget |
| `project.py` | `ProjectContext` / `ProjectDetector`: the code project, read and never written |
| `manager.py` | `KnowledgeManager`: the pipeline, the bookkeeping, persistence |
| `tools.py` | the three agent-facing tools |
| `runtime.py` | `KnowledgeModule`: the same engine on the event bus |

## The pipeline

```text
INGEST → CHUNK → INDEX → RETRIEVE → RERANK → CONTEXT → (the LLM)
```

Each arrow is a module above, and the whole path is exercised by
`tests/test_knowledge_rag.py`.

```bash
python -m novacontrol demo phase11_rag      # a corpus written for the demo, end to end
```

```python
from novacontrol.knowledge import KnowledgeManager

manager = KnowledgeManager()
await manager.ingest_path("~/notes")          # a file or a whole tree
hits = await manager.search("where is the retry policy configured?")
context = await manager.context_for(
    "Fix the authentication issue.", active_task="fix auth", budget_tokens=1200
)
```

## Local-first, and never dependent on a model

Retrieval has two signals and one rule about them:

* **BM25 is always there.** It needs no model, no download and no network. For the
  queries this feature exists for — *"where is the retry policy configured?"*,
  *"which file defines the approval token"* — a rare term in the document is
  exactly what identifies it.
* **Embeddings are optional and pluggable.** `use_embedding_model(model)` points
  the index at anything with `embed` (the same `Embedder` seam the intent matcher
  already uses) and rebuilds the stored vectors so a query is never scored against
  vectors from a different model. `release_embedding_model()` gives it back. The
  default is the dependency-free hashing embedder, so **a machine with no model
  still answers**, lexically, with no configuration.

Every hit reports which signal produced it (`terms:`, `bm25=`, `embedding=`), so a
surprising result can be traced to the evidence rather than appearing by magic.

## Sources are versioned, and duplicates never enter twice

A source is identified by project + path and versioned by its content hash:

* reading a tree twice costs one hash per file — unchanged bytes come back
  `UNCHANGED`;
* an edit replaces **exactly that source's** chunks and bumps its `version`,
  leaving every other source untouched and no stale chunk retrievable;
* the same file ingested three times is still one source, and two names that
  legitimately differ are two.

Each source carries `source_id`, `source_type` (`text`, `markdown`,
`documentation`, `code`, `pdf`, `note`), `path`, `project`, `content_hash`,
`version`, `chunk_count`, `bytes`, `indexed_at` and `metadata`. Each chunk carries
`chunk_id` (`<source_id>:<ordinal>`), its line range and the heading it sits under,
so a citation can be followed to a file and a line.

Taught articles (the Learn tab's *Teach* box) can be ingested as NOTE sources, so
one search covers everything the installation knows rather than two indexes that
disagree.

A source that is removed is reported as `PRUNED`, not `SKIPPED`: nothing was read,
something was forgotten, and the difference matters to whoever is looking at the
counts.

### Scope, and the one fallback

Retrieval is project-aware by default (`retrieve(..., active_only=True)`) and
search is not (`search(..., active_only=False)`): a *search* is the general
question — what do we know about X — and a *retrieval* is the project-aware one —
what does **this** project say about X. Both use one rule: when the active project
has anything indexed, only its sources are candidates; when it has nothing indexed
yet, the whole index is searched. That fallback exists because a freshly opened
project that answers "nothing" to every question is indistinguishable from a
broken engine, and every hit still names the project it came from.

### What is skipped, and why it says so

Unrecognised suffixes, binary files (a NUL byte in the first block is the tell),
oversized files and PDFs with no extractable text are **skipped with a reason**,
never indexed as noise. PDFs are read with whichever of `pypdf`, `PyPDF2`,
`PyMuPDF`/`fitz`, `pdfminer.six` is installed; when none is, a small
standard-library reader decodes Flate-compressed content streams and pulls the
text-showing operators out. Anything it cannot read raises `UnsupportedDocument`,
and the ingest reports that instead of pretending.

## Project awareness

`ProjectDetector` answers *what is this project?* by reading, never guessing:

| Field | Where it comes from |
| --- | --- |
| `root` | the first directory up the tree that looks like a project (`.git`, `pyproject.toml`, `package.json`, …) |
| `repository` | `.git/config` — read directly, because running git is a command you did not ask for |
| `branch` | `.git/HEAD`, reported whole (`feature/timeouts`, not `timeouts`) |
| `language` / `framework` | the manifests that are actually present, and the framework only when a dependency names it |
| `recent_files` | the newest source files on disk |
| `configuration` | the configuration and manifest files at the root |
| `recent_errors` | the shared bug log (`data/bugs.json`), when one can supply them |
| `test_status` | the runner this project actually has, or the real outcome of the last suite NovaControl ran — never a fabricated pass |
| `active_task` | whatever the caller set |

Nothing in `project.py` writes a file, and that is asserted by a test that
compares the whole tree byte-for-byte before and after a detection — and again
after a full ingest → search → retrieve → context pass.

## Keeping it current

Two things an incremental update has to handle beyond "the file changed":

* **A tree larger than the cap.** `max_files_per_tree` (default 5000) bounds an
  ingest, because a mistaken path points at a home directory. When the cap is
  reached it is *reported* — a skipped entry plus a warning — because "5000 files
  ingested" and "5000 of 120000 files ingested" are different facts and only one
  of them is useful.
* **A file that no longer exists.** An ingest can only report on files that are
  there, so a deleted document would leave its chunks retrievable forever:
  knowledge that looks current and is not. `prune_missing(root)` forgets every
  file-backed source under `root` whose file is gone, and `ingest_path(root,
  prune=True)` runs it as part of the ingest and counts what it removed. It is
  opt-in, and scoped to the tree you name, because a temporarily unreadable mount
  must not silently empty an index. A note or a taught article carries a virtual
  path (`note/…`, `taught/…`) and can never be swept up by a prune of a real
directory. Each removal is announced on the bus with `status="pruned"`.

## The context budget

No project is ever dumped into a model. `retrieve()` and `context_for()` spend a
budget (default 2000 tokens, `max_context_tokens`) best-first, and everything
they decline is counted:

* **relevance** — the fused index score;
* **source priority** — documentation and code ahead of a scan-quality PDF;
* **recency** — the same project and recently indexed sources win ties;
* **deduplication** — two chunks of one paragraph are one answer;
* **per-source cap** — one verbose file cannot take every slot.

The result reports `tokens`, `budget`, `considered`, `omitted` and `truncated`, so
a short answer is never mistaken for a complete one. A single hit that does not
fit is cut to fit rather than dropped — an honestly shortened excerpt beats
"nothing matched".

`tokens` is the size of the block that was actually rendered, not the sum of the
pieces: the request line, the hit count, each hit's label and the cut marker are
all real tokens in the request, so they are charged to the budget and counted in
what is reported. `context_for` budgets its project header the same way (a project
with forty open errors gets a truncated header rather than a context that
silently exceeds its own budget).

**Better-ranking copies win the deduplication.** Candidates are weighted (score ×
source priority × recency × project boost) and sorted *before* near-duplicates are
removed, so of two identical paragraphs the one that ranks better is kept —
including when the better one is the project you are standing in.

## How the rest of the build reaches it

**Tools** (registered on a default install, found by the same retriever as any
built-in):

| Tool | What it does | Permission |
| --- | --- | --- |
| `knowledge_search` | ranked, cited excerpts for a query | — (the disk work was paid at ingest) |
| `knowledge_ingest` | read a file or directory into the index | `filesystem:read` |
| `project_context` | the active project, its branch, errors, tests | `filesystem:read` |

**Events** (`KnowledgeModule`, so a module or plugin can ask without importing the
engine): `knowledge.search_requested`, `knowledge.ingest_requested`,
`knowledge.context_requested`, `project.detect_requested`, each answered with a
correlated reply. The typed vocabulary also gained `knowledge.indexed` and
`knowledge.retrieved`, published by the manager itself, so a watcher sees the same
events whether a tool, the CLI or the bus drove the work.

**Application** methods: `search_knowledge`, `ingest_knowledge`,
`project_context`, `knowledge_context`, and `answer_with_knowledge`. The index is
persisted with the rest of the state (`data/knowledge_index.json`) and restored on
boot — vectors are recomputed locally, so reopening it needs no model.

**The last arrow: `CONTEXT → LLM`.** `BrainRequest.knowledge` carries retrieved
text, and the brain inserts it as its own system message, labelled as local
knowledge with the instruction to cite the file it came from and not to invent
beyond it (an unlabelled dump reads as part of the conversation).
`answer_with_knowledge(question)` is the whole pipeline in one call: retrieve,
budget, hand to the model, return the answer plus the citations and the block that
was used. When no model is configured the scratch brain answers locally and the
payload says so through `brain_mode` — the method does not claim a model used
something the model never saw.

## What this does not do

* It does not modify, move or delete your files. Read-only, by construction.
* It does not learn from the conversation implicitly; what is indexed is what you
  pointed it at (or taught it).
* It does not require or download an embedding model, and it does not hold one
  resident: supply one per batch, release it after.
* It does not notice a file being deleted by itself: run `prune_missing` (or
  `ingest_path(..., prune=True)`) to drop sources whose files are gone.
* It has no HTTP routes yet — the surfaces are the tools, the bus, the CLI demo
  and the application methods. A Knowledge panel in the web UI is on the roadmap.
