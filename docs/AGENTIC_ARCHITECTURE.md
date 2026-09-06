# NovaControl — Agentic Architecture (agentcore)

## Gap analysis (what existed vs what was missing)

| Capability | Before | After |
|---|---|---|
| Orchestrator | `handle_request` dispatched one intent → one handler, single-shot, no task state | `agentcore.orchestrator.AgenticOrchestrator` runs the full loop (interpret → plan → perceive → act → observe → verify → recover → learn) with persistent `AgentTaskState` |
| Task Interpreter | brain intent classifier (7 intents) | `TaskInterpreter`: structured goals (subtasks, constraints, tools, risks, confirmation), LLM-enriched when available, heuristic when not |
| Planner | `PlanningEngine` split a goal into static steps | `AdaptivePlanner`: typed steps (observe/browser/desktop/phone/research/reason/verify) each with its own expectation + `replan()` on divergence |
| UI perception | browser DOM extract, desktop window probe, vision — **separate and unused together**; `VisionModule()` ran the deterministic fallback forever | `UiPerceptionEngine` fuses **visual (vision/LLM screenshot)** + **structured (DOM/window probe)** + **semantic (LLM labeling)** into one `UiState` |
| UI state | ad-hoc dicts per runner | `UiState`/`UiElement` with semantic `find()`, `interactive()`, `diff()` |
| Action engine | device-specific controllers with coordinate/selector targets | `ActionEngine`: semantic-first actions (click by label/purpose, coordinates/selector as fallback) returning `ActionOutcome`; shell gated behind approval tokens |
| Verification | desktop window-verification for launches only | `Verifier`: expectations (ui_contains/ui_absent/url/min_elements/requires_observed_change) → PASS/FAIL/INCONCLUSIVE; distinguishes *executed* vs *apparently succeeded* vs *completed* |
| Recovery | none (single attempt, then error) | `RecoveryEngine`: diagnose → RETRY / RELOCATE (semantic re-find) / RESEARCH / ABORT; destructive actions never auto-retried; budget-capped |
| Application knowledge | flat `KnowledgeBase` articles | `ApplicationKnowledgeGraph`: UI paths, workflows, alternative paths, confidence with **freshness decay**, failure counters; low-confidence paths are never proposed |
| Research integration | Explore standalone | research_fn bridge: agents can call Explore mid-loop; findings stored as lessons |
| Evaluation | none | `EvaluationLedger`: success/recovery/verification rates, steps per task |
| Self-improvement | `SelfImprovementEngine` code plans + approval-gated preview/apply (kept) | loop output (lessons, workflows, metrics) feeds it; existing human-approval gates unchanged |

Existing surfaces (chat, explore, JARVIS desktop/phone, brain switching, cloud LLM) are untouched.

## The loop

```
User goal
  → TaskInterpreter.interpret()         structured goal + risks + tools
  → memory: ApplicationKnowledgeGraph   previously verified workflow (shape, not truth)
  → AdaptivePlanner.create_plan()       observe … act … verify steps
  → loop (max iterations):
       UiPerceptionEngine.observe()     fused visual+structured+semantic UiState
       ActionEngine.<action>()          semantic-first, ActionOutcome
       Verifier.verify()                PASS / FAIL / INCONCLUSIVE vs expectation
       RecoveryEngine.diagnose()        RETRY / RELOCATE / RESEARCH / ABORT
  → finalize: summary, lessons, knowledge update, evaluation record
```

## API

- `POST /agent/run {"request": "..."}` → full loop, returns the task state.
- `GET /agent/metrics` → evaluation metrics + recent runs.
- `GET /agent/knowledge` → the application knowledge graph.

## Storage

`data/agentic_knowledge.json` + `data/agentic_evaluation.json` via the existing
`JsonStateStore`. No new database; memory stays in SQLite.

## Vision model as the base

`_build_vision_processor()` wires the **live brain provider** (Ollama vision
models like llava, or cloud multimodal) into `MultimodalVisionProcessor`, with
automatic fallback to the deterministic processor when no multimodal model is
reachable. Perception layer 1 (visual) upgrades automatically as better vision
models are installed; layer 2 (DOM/automation) is deterministic; layer 3
(semantic labels) rides the same provider. All three fuse into `UiState`.

## Safety

- Shell/terminal actions require an explicit approval token; the engine refuses otherwise.
- Destructive action failures abort instead of retrying.
- Phone actions stay in the J.A.R.V.I.S approval-gated flow.
- Knowledge confidence decays with age and failures; stale paths are proposals, never autopilot.
- Existing approval-token execution for desktop/phone/browser is unchanged.

## Future work (explicitly not faked)

- Coordinates fallback in `UiElement.location` is modeled but the desktop runner
  does not yet accept point targets (needs a UIA/PyAutoGUI adapter).
- Windows UI Automation tree is modeled as the `window_probe` seam; today it is
  fed by the existing Get-Process window enumeration.
- Benchmark suite lives in `EvaluationLedger`; scenario fixtures are the next step.
