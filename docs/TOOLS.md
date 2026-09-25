# Tool System

The tool system provides structured tool registration, schema validation, permission checks, human approval integration, and event-driven execution. Phase 5 added the layer above it: **metadata, discovery, validation, normalization and caching** — finding the few relevant tools for a request instead of handing every definition to a model.

## Components

- `ToolSchema`: declares a tool name, description, and parameter list
- `ToolParameter`: typed argument declaration with required flags
- `ToolRegistry`: stores tool implementations and schemas
- `FunctionTool`: adapts a Python callable into the tool protocol
- `ToolExecutor`: validates requests, asks for approval when permissions are required, reuses cacheable results, normalizes output, runs tools, and returns structured results
- `ToolModule`: executes tools from runtime events
- `ToolMetadata`: what a tool IS — description, category, capabilities, input/output schema, risk, permissions, examples, tags, and its caching contract
- `ToolCatalog`: every tool joined from the declarations, the intent catalogue, the capability registry and the runtime registry
- `ToolRetriever`: ranks catalogued tools for a request, with scores and the evidence for them
- `ToolCall` / `validate_tool_call`: the value a planner produces and the gate that stops an invalid one before execution
- `OutputNormalizer`: bounds a tool result before it reaches a context, counting everything it drops
- `ToolResultCache`: TTL caching for read-only operations a tool has declared non-volatile

## Tool metadata (Phase 5)

Each tool is described well enough to be searched, ranked and judged before anything runs:

| field | meaning |
| --- | --- |
| `description` | one sentence, in the words a request would use |
| `category` | system / files / desktop / browser / phone / code / knowledge / memory / automation / vision / planning / conversation |
| `capabilities` | the named capabilities it serves, as the registry knows them |
| `input_schema` / `output_schema` | what a valid call looks like, and what a caller may expect back |
| `risk` | low / medium / high / critical — the HIGHEST any source claims |
| `permissions` | the same scopes the approval layer gates on; `requires_approval()` is derived from them, never a second opinion |
| `examples` | requests this tool is the answer to, used by the retriever |
| `tags` | operator vocabulary (cpu, ram, hardware, os) |
| `read_only`, `cache_ttl_s`, `volatile_values` | the caching contract — see below |
| `registered`, `intents` | whether a callable exists, and which intents reach it |

`requires_approval()` is derived from `permissions`, never a second opinion, and the cache contract is enforced in one place (`is_cacheable` / `cache_refusal`).

The catalogue is assembled from four sources so nothing has to be registered twice: the hand-written declarations, the **intent catalogue** (which tool carries each intent, plus the phrases a person says), the **capability registry** (executor, risk, capability name) and the **runtime registry** (what is actually registered, with its schema and permission scopes). `registered` is true only when a callable really exists behind the name, so a capability the build can dispatch but has not wired up is reported rather than hidden.

**Both schemas come from somewhere real.** An `input_schema` is *derived* from the required and optional entities the two surfaces that name them already record — the intents a tool carries out (`desktop_controller` expects an `application` because `open_application` requires one) **and** the capabilities it executes (`file_manager` expects a `file` because `find_file` requires one, even though the hand-written intent entry never names the tool back) — unless the tool declares its own. Emptiness is therefore only honest where nothing declares anything, which the tests assert. An `output_schema` is a declared contract — what a planner needs to know before running something ("a reading returns a value and a unit"; "a file search returns paths") — and always allows extra keys, because a result is evidence rather than a promise.

Two fields may legitimately be empty, and the emptiness is meaningful: `capabilities` is empty exactly for tools that are no registered capability's executor (the introspection tools below), and `permissions` is empty for tools that touch nothing the approval layer gates. Both are asserted as invariants in the tests rather than left to interpretation.

## Tool discovery

```
UserIntent -> Decision -> Tool Discovery -> top relevant tools -> Planner / LLM -> Execution
```

`ToolRetriever.search(query)` returns the few tools that could carry the request, best first, each with its score and the evidence:

```python
app.discover_tools("Which programs are consuming most of my memory?")
# [{'tool': 'system_monitor', 'score': 0.73, 'lexical': 1.0, 'semantic': 0.24,
#   'matched_terms': ['program', 'consuming', 'most', 'memory'], ...}]
```

Two components, both of them the lightweight machinery the NLU layer already uses — no new dependency, and embedding support stays optional:

- **lexical**: the query's content words, weighted by the SQUARE of their rarity across the tool corpus, scored as the fraction of the query's meaning a tool covers;
- **semantic**: cosine similarity over hashed word/bigram/character-ngram features (the same deterministic offline vector space as `intelligence/semantic.py`). A caller with an embedding model passes it as `embedder` and this layer uses it instead.

A result below `floor` (0.25, calibrated on the shipped corpus) is not returned at all: *"no tool fits this"* is a real answer, and it is what stops a planner reaching for an unrelated tool just to have one. Filters (`category`, `tags`, `max_risk`, `require_registered`) are applied before ranking, and the same request twice is a dictionary lookup.

A request made entirely of function words (*"what can you do?"*) has no content words, so both layers see an empty query and every tool scores zero. That request has a real answer — it is the `capabilities` tool's own declared example — so when there is nothing to weigh, the query is compared directly against the phrases each tool declares (character-trigram overlap against its examples and name). The floor still applies: *"can you help me?"* returns **no tool**, which is the honest answer.

The model escalation shows only this shortlist — `app.discovered_tools(goal)` — never the whole toolbox.

### The registered read-only tools

A default install registers three tools, so the pipeline below is exercised by real work rather than only by tests — and they are the operations the specification names as worth caching (OS information, hardware information, installed applications, system capabilities):

| tool | returns | cache |
| --- | --- | --- |
| `machine_facts` | operating system and release, CPU model and count, Python version, total memory, disk capacity | 600 s, **stable facts only** |
| `capabilities` | every registered capability with its risk and executor | 1800 s |
| `installed_applications` | the applications installed on this machine, from the index `open <app>` resolves against | 900 s |

None returns a moving number. Free space and uptime are *live readings* and belong to `system_monitor`, which declares them volatile — a tool whose result is reused for ten minutes must not answer with a value that changes in one, or the cache becomes the lie it exists to prevent.

`installed_applications` answers with the real inventory rather than a second opinion about it: it reads the same index the desktop controller opens programs by name from, so what it lists can be launched. An optional `filter` narrows by substring, and the result is bounded by the normalizer like every other — the count of what was dropped travels with it.

## Validation

Every call passes schema validation before execution, and the schema is strict:

- an **undeclared argument** is an error (`Unexpected argument: recursive`), not something passed along;
- a **required argument given no value** is an error — `{"command": ""}` runs nothing and reports success;
- a **type mismatch** names what was expected and what arrived;
- every problem is reported at once, so a planner fixing three mistakes needs one round trip, not three;
- a type name no schema can satisfy fails when the schema is constructed, not when a request is in flight.

`validate_tool_call(schema, ToolCall(...))` gives the same verdict to a caller that wants to check before running; `validation.request()` refuses to build a request from an invalid call.

## Result normalization

Tool output is bounded before it reaches a model, and everything dropped is counted:

```json
{
  "exit_code": 1,
  "error_summary": "FAILED tests/test_math.py::test_add",
  "relevant_lines": ["FAILED tests/test_math.py::test_add", "E   AssertionError: expected 3, got 4"],
  "stdout_tail": ["..."],
  "lines": 10432,
  "truncated": true
}
```

A small result is left untouched. Long text, long lists and deep structures are capped (`max_chars`, `max_items`, `max_depth`, `max_line_chars`) with an explicit marker plus an `elided` count per field.

## Caching

Safe read-only work is reused, and nothing else:

- only a tool may make its results cacheable (`cache_ttl_s`), and absent metadata means no caching at all;
- `volatile_values` names the operations that change between calls — a cached CPU reading is a lie nobody can see, so it is refused (`cache_refusal()` says why);
- failures, denials and empty results are never stored;
- the cache is bounded (`max_entries`, LRU eviction) and reports hits, misses, stores, evictions and refusals — including what it declined and why — through `tools_status()["cache"]`.

## Events

- `tool.execute_requested`: asks the tool module to execute a registered tool
- `tool.execution_completed`: emitted when a tool succeeds
- `tool.execution_failed`: emitted when validation or execution fails
- `tool.execution_denied`: emitted when approval is denied

## Security

Tools with required permission scopes are treated as sensitive. Sensitive tools are routed through the configured `ApprovalGateway` before execution.

The default approval gateway denies sensitive actions, which keeps the system secure until a GUI/API approval flow is connected.

## Supported Schema Types

- `string`
- `integer`
- `number`
- `boolean`
- `object`
- `array`

## Routing Ladder Demo (docs/tools/routing-ladder-demo.html)

A self-contained, dependency-free HTML page that walks NovaBrain's routing ladder interactively: type an utterance and it evaluates every gate in source order — including the math/scratch gate that decides whether a phrase is answered locally or falls through to research.

Open it directly in any browser (no server, no build step). It is a **teaching snapshot**: the rung triggers and confidences are mirrored verbatim from `brain.py`/`scratch.py` as of its writing and are pinned upstream by the `INTENT_ROUTING` test tables. For live behavior on the current code, use the app's **Routing panel** (which traces `POST /brain/decide` against the running server), not this page.
