# API

The Phase 12 API subsystem provides REST and WebSocket entrypoints through FastAPI.

## REST Routes

<!-- BEGIN GENERATED: api-reference (scripts/generate_api_reference.py -- do not edit inside) -->

- `GET /`: Local web platform. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /health`: Health check. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /status`: Runtime status. *(frontend: web panel `renderGeneric`)*
- `GET /system/health`: Aggregated system health. *(frontend: web panel `renderHealth`)*
- `GET /system/telemetry`: Live machine telemetry for the Command Center (CPU, memory, storage, GPU, network, battery, temperature). *(frontend: web panel `renderTelemetry`)*
- `GET /system/harden`: Release hardening report. *(frontend: web panel `renderHealth`)*
- `GET /system/package`: Runtime package manifest. *(frontend: web panel `renderGeneric`)*
- `POST /ask`: Route a natural-language request through NovaControl; an optional `image` path attaches a picture for the vision pipeline to read. *(frontend: web panel `renderChatResult`)*
- `POST /brain/mode`: Switch the chat brain between auto, llm, scratch, and cloud. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /brain/mode`: Inspect the active brain mode and provider. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /brain/cloud/presets`: List cloud LLM provider presets (no secrets). *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /brain/cloud`: Install a cloud LLM (ChatGPT/Gemini/Groq) with a local API key. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /brain/cloud/test`: Ping a cloud provider with the pasted key (one tiny completion; nothing is saved). *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /brain/cloud/clear`: Remove the cloud LLM config and its API key. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /brain/ollama/models`: List local Ollama models for the brain model picker (fresh probe). *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /brain/local/model`: Pin the local brain to a specific Ollama model (empty clears the pick). *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /chat/clear`: Clear the server-side chat conversation memory and the shared transcript. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /chat/history`: Read the shared server-persisted chat thread (same for every browser). *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /chat/history`: Append a chat turn to the shared thread (action=migrate imports a browser's old history once). *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /tasks`: List tracked task records. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /tasks/delete`: Delete one tracked task record by id. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /tasks/clear`: Delete all tracked task records (undoable for a short window). *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /tasks/clear/undo`: Restore the tasks wiped by a recent /tasks/clear (one-shot token). *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /improve`: Create a self-improvement plan. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /improve/workflow`: Create a friendly self-improvement workflow. *(frontend: web panel `renderWorkflow`)*
- `POST /improve/preview`: Run a temporary self-improvement preview. *(frontend: web panel `renderWorkflow`)*
- `POST /improve/approve`: Approve a preview for code changes. *(frontend: web panel `renderWorkflow`)*
- `POST /learn`: Run a local feedback learning cycle. *(frontend: web panel `renderLearning`)*
- `POST /knowledge/teach`: Teach: persist a typed fact as durable recallable knowledge. *(frontend: web panel `renderLearning`)*
- `GET /knowledge`: List taught knowledge, optionally filtered by a query. *(frontend: web panel `renderLearning`)*
- `POST /knowledge/recall`: Recall taught knowledge matching a query. *(frontend: web panel `renderLearning`)*
- `POST /train`: Run bounded autonomous local learning iterations. *(frontend: web panel `renderLearning`)*
- `POST /brain/decide`: Trace an utterance through the routing gates with a rung preview. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /plan`: Create and optionally execute a plan. *(frontend: web panel `renderBuild`)*
- `POST /plan/run`: Run a goal through the agent loop; steps needing approval must be named. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /plan/code`: Plan a coding task: language-aware steps plus a drafted code artifact. *(frontend: web panel `renderBuild`)*
- `POST /build/save`: Save a drafted Build artifact to the build_workspace folder on disk. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /command/plan`: Plan a natural desktop or phone command. *(frontend: web panel `renderCommand`)*
- `POST /command/execute`: Execute an approved natural desktop or phone command. *(frontend: web panel `renderCommand`)*
- `POST /desktop/plan`: Plan an approval-gated desktop action. *(frontend: web panel `renderCommand`)*
- `POST /desktop/execute`: Execute an approved desktop action. *(frontend: web panel `renderCommand`)*
- `POST /browser/plan`: Plan an approval-gated browser action (navigate, search + extract). *(frontend: web panel `renderCommand`)*
- `POST /browser/execute`: Execute an approved browser action (search returns the top results). *(frontend: web panel `renderCommand`)*
- `GET /phone/status`: Inspect phone bridge status. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /phone/connect`: Run the phone bridge pairing flow. *(frontend: web panel `renderPhoneStatus`)*
- `GET /vision/status`: Vision capability report: model availability and open bug count. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /vision/model`: Install a multimodal vision model (ollama or a cloud provider) for element location. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /vision/model/clear`: Remove the configured vision model; element location returns to OCR-only. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /vision/describe`: Capture the screen and describe it (vision model or window probe). *(frontend: web panel `renderGeneric`)*
- `POST /vision/click`: Vision-locate a labeled element on screen, click it, and verify. *(frontend: web panel `renderGeneric`)*
- `GET /perception/status`: Perception layer report: providers, budgets, tracking and telemetry (no content). *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /perception/capabilities`: Perception capability classification with live availability on this machine. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /perception`: Perceive an image, frame list, screen or camera into a structured scene (reading only - nothing is executed). *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /world/status`: World model report: versions, entities, policies, retention and the honest prediction posture (no content). *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /world/state`: Bounded, content-light view of the current world state (labels, statuses, boxes - no attribute values). *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /world/observe`: Ingest one observation (or a batch) and advance the world state - recording only, nothing is executed. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /world/query`: Answer a bounded structured question about current or historical world state, including evidence, uncertainty and staleness. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /world/predict`: Ask for a future state; reports model_unavailable when no predictive model is wired and never fabricates a prediction. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /intelligence`: Global Intelligence Layer: telemetry, findings, and capabilities. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /capabilities`: Every capability this installation has (declared, tool, action), with its availability, risk, permissions, tools, inputs and outputs. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /capabilities/discover`: What capabilities are available for this task? Query parameters: `query` (required), `intent`, `category`, `tools`, `models`, `include_unavailable`, `limit`. Discovery only - nothing runs. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /bugs`: List recorded bugs (what failed, where, and when). *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /bugs/{bug_id}/fix`: Mark a recorded bug as fixed. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /bugs/clear-fixed`: Remove all bugs already marked fixed. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /phone/plan`: Plan an approval-gated phone action. *(frontend: web panel `renderCommand`)*
- `POST /phone/execute`: Execute an approved phone action. *(frontend: web panel `renderCommand`)*
- `POST /agent/run`: Run the full agentic loop for a natural-language goal. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /agent/metrics`: Agentic evaluation metrics (success, recovery, verification rates). *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /agent/knowledge`: Application knowledge graph: workflows, confidence, freshness. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /explore`: Research a topic and return an Explore report. *(frontend: web panel `renderExplore`)*
- `GET /explore/trending`: Current daily research topics from live top-story news (rotating window). *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /activity`: Recent completed actions (commands, research, learning) for the web timeline. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /events/stream`: Live activity channel: the application EventBus as SSE. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /settings`: Read local user settings. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /settings`: Update local user settings. *(frontend: web panel `renderSettingsResult`)*
- `GET /plugins`: List plugin API records. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /automation`: Scheduled automations: what is stored, armed, and due next. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /automation/schedule`: Store a request plus the schedule it states as an automation (nothing runs yet). *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /automation/approve`: Authorize a pending automation so it may be armed. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /automation/cancel`: Cancel an automation so it never runs again. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /automation/enable`: Arm an approved automation again. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /automation/disable`: Disarm an automation without cancelling it. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /automation/run`: Run a stored automation now, under the same permission gate as a due run. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /automation/run-due`: Run every automation that is due (the scheduler's own tick). *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /audit`: Operational audit trail status: records, retention, local-only storage, redactions. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /audit/entries`: Recent audit records (redacted), oldest first. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /audit/prune`: Apply the retention policy and report what was removed. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /audit/delete`: Delete one audit record by id. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /audit/clear`: Delete every audit record (explicit, not retention). *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /privacy`: Execution mode, privacy controls and every outbound decision. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /privacy`: Change the execution mode and/or a privacy control, live. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /resources`: The resource governor's reading of this machine, with its reasons. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /cost/estimate`: Estimate a request's cost and routing hint (never a gate). *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /diagnostics`: Run the component roster, or a comma-separated subset via ?only=. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /benchmark`: Stored model measurements and the measured comparison per category. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /benchmark`: Measure a model on the given tasks through the live provider. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /evaluation/summary`: Recorded trajectories, quality verdicts, scores and retention, at a glance. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /evaluation/trajectory/{trajectory_id}`: One stored trajectory with its evaluation and reward breakdown. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /evaluation/metrics`: Aggregate metrics over stored trajectories (success, latency p50/p95, reward, routing). *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /evaluation/rewards`: Stored rewards, newest first, with their component and penalty breakdowns. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /training/status`: Training subsystem state: enabled, dry-run, backend, capabilities. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /training/summary`: Datasets, runs, checkpoints and models, at a glance. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /training/estimate`: Estimate a training config's resources without starting anything. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /training/datasets`: Stored fine-tuning dataset versions, newest first. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /training/datasets`: Build a dataset version from stored trajectories, deterministically split. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /training/datasets/validate`: Validate a dataset version: leakage, duplicates, empty splits. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /training/datasets/{dataset_version_id}`: One dataset version with its split counts and provenance. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /training/runs`: Training runs, newest first, with their status and resource verdict. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /training/runs`: Create a training run from a config (never starts it). *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /training/runs/start`: Start a run: refused when the estimate is unsafe, real training needs confirmation. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /training/runs/pause`: Pause a running training run at the next checkpoint boundary. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /training/runs/cancel`: Cancel a run; its checkpoints are kept for inspection. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /training/runs/resume`: Resume an interrupted run from its newest valid checkpoint. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /training/runs/re-estimate`: Re-estimate a run's resources against the current machine reading. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /training/runs/evaluate`: Compare a run's candidate against its base model and record the verdict. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /training/runs/{run_id}/checkpoints`: A run's checkpoints with their validity (complete, partial, corrupt). *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /training/runs/{run_id}`: One training run: config, metrics, progress and checkpoint list. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /training/evaluations`: Recorded before/after evaluations with their regression checks. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /training/models`: Fine-tuned candidates and registered models, with their lifecycle status. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /training/models/{model_id}`: One registered model: adapter metadata, lineage and evaluation history. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /training/models/approve`: Approve a candidate that has a recorded passing evaluation (explicit only). *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /training/models/promote`: Promote an approved model to production, demoting the previous one. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /training/models/reject`: Reject a candidate with a recorded reason. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /training/models/deprecate`: Deprecate a registered model so it is no longer selected. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /training/models/rollback`: Roll a deployment back to the model a promotion replaced. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /preference/status`: Preference subsystem state: datasets, runs, reviews, readiness. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /preference/summary`: Datasets, runs, reviews and objectives, at a glance. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /preference/algorithms`: The DPO and ORPO objectives, what each costs, and readiness here. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /preference/estimate`: Estimate a preference config's resources without starting anything. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /preference/dry-run`: Validate a dataset, config and output directory, and price the run — starting nothing. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /preference/datasets`: Stored preference dataset versions, newest first. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /preference/datasets`: Build a preference dataset version from observed behaviour, deterministically split. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /preference/datasets/validate`: Validate a preference dataset: leakage, contradictions, provenance, quality. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /preference/datasets/{dataset_version_id}`: One preference dataset version with its splits and statistics. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /preference/datasets/{dataset_version_id}/pairs/{preference_id}`: One preference pair: both candidates, evidence, outcomes and split. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /preference/reviews`: Pairs waiting on a human reviewer, with both candidates side by side. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /preference/reviews/{preference_id}`: One queued pair as a reviewer sees it. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /preference/reviews/submit`: Submit a preference a person is asserting (choose A/B, or record a pair). *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /preference/reviews/decide`: Settle a queued pair: choose A, choose B, mark a tie, or reject it. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /preference/runs`: Preference runs, newest first, filtered by objective. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /preference/runs`: Create a DPO/ORPO run from a config (never starts it). *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /preference/runs/start`: Start a preference run: unsafe estimates and unconfirmed real runs are refused. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /preference/runs/pause`: Pause a running preference run at the next step boundary. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /preference/runs/cancel`: Cancel a preference run; its checkpoints are kept. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /preference/runs/resume`: Resume an interrupted preference run from its newest valid checkpoint. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /preference/runs/re-estimate`: Re-estimate a run's resources against the current machine reading. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /preference/runs/evaluate`: Compare base, SFT and candidate on the held-out pairs and record the verdict. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /preference/compare`: Compare three models on a pair dataset without needing a run. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /preference/runs/{run_id}/checkpoints`: A preference run's checkpoints with their validity. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /preference/runs/{run_id}`: One preference run: algorithm, config, metrics and checkpoints. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /preference/evaluations`: Recorded preference comparisons with their regression checks. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /preference/models`: Registered preference models, filtered by objective (same registry as training). *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /preference/models/{model_id}`: One registered preference model, with the objective that produced it. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlhf/status`: RLHF/RLAIF state: feedback, ratings, datasets, runs, readiness. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlhf/summary`: Feedback, ratings, datasets and runs, at a glance. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlhf/algorithms`: The RL modes and policy optimizers, and what this machine can do. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlhf/estimate`: Estimate an RL config's resources without starting anything. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlhf/dry-run`: Validate, price, plan and simulate an RL run — starting nothing. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlhf/pipeline`: The stage-by-stage RLHF/RLAIF plan for a configuration and dataset. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlhf/feedback`: Human feedback rows, newest first, with their verdicts. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlhf/feedback`: Submit human feedback for a trajectory (never deletes; quality-filtered). *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlhf/feedback/{feedback_id}/decide`: Settle a held feedback row: accept keeps it usable, reject does not. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlhf/rate`: Ask an evaluator for a structured rating of observable facts. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlhf/ratings`: Stored AI ratings, newest first, with the source breakdown. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlhf/disagreements`: Recorded human-vs-AI disagreements, optionally detecting new ones. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlhf/datasets`: Reward dataset versions, newest first, filtered by mode or name. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlhf/datasets`: Build a reward dataset version from feedback and ratings, split deterministically. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlhf/datasets/{dataset_version_id}/validate`: Validate a reward dataset: provenance, integrity, splits and leakage. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlhf/datasets/{dataset_version_id}/held`: Rows a dataset held back, and why they were excluded. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlhf/datasets/{dataset_version_id}`: One reward dataset version with its splits and statistics. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlhf/runs`: RLHF/RLAIF runs, newest first, filtered by mode or status. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlhf/runs`: Create an RL run from a config (never starts it; rewards are audited first). *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlhf/runs/start`: Start an RL run; unsafe estimates and unconfirmed real runs are refused. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlhf/runs/pause`: Pause a running RL run at the next step boundary. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlhf/runs/cancel`: Cancel an RL run; its checkpoints are kept. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlhf/runs/resume`: Resume an interrupted RL run from its newest valid checkpoint. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlhf/runs/re-estimate`: Re-estimate a run's resources against the current machine reading. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlhf/runs/evaluate`: Compare base, SFT, preference and RL candidate on held-out data. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlhf/compare`: Compare up to four models on a reward dataset without needing a run. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlhf/runs/{run_id}/checkpoints`: An RL run's checkpoints with their validity and loadability. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlhf/runs/{run_id}`: One RL run: mode, algorithm, config, metrics and checkpoints. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlhf/evaluations`: Recorded RL comparisons with their regression checks. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlhf/models`: Registered RL models, filtered by mode (same registry as training). *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlhf/models/{model_id}`: One registered RL model, with the mode that produced it. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlvr/status`: RLVR state: verifiers, critiques, corrections, datasets, runs. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlvr/summary`: The same, plus the newest critiques, corrections and datasets. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlvr/verifiers`: Registered verifiers with categories, versions and integrity state. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlvr/verifiers/disable`: Stop a verifier supporting rewards (tamper protection: disable, never edit). *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlvr/verifiers/enable`: Let a disabled verifier support rewards again. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlvr/verify`: Verify checkable questions; expectations are frozen before each check. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlvr/reward`: Turn verification results into a reward, with its integrity audit. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlvr/critiques`: Generate structured critiques from recorded evidence and store them. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlvr/critiques`: Stored critiques, newest first, filtered by category and severity. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlvr/corrections`: Corrected examples and their verdicts; held rows are reviewable. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlvr/corrections`: Propose corrections for stored critiques; unverified ones are held. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlvr/datasets`: Critique dataset versions, newest first, filtered by name. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlvr/datasets`: Build an immutable critique dataset version from critiques and corrections. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlvr/datasets/{dataset_version_id}/validate`: Whether a critique dataset can train anything, and what is missing. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlvr/datasets/{dataset_version_id}/held`: Corrections a dataset held back, so a person can settle them. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlvr/datasets/{dataset_version_id}/pairs`: The Phase 17 preference pairs a critique dataset yields. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlvr/datasets/{dataset_version_id}`: One critique dataset version with its splits and statistics. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlvr/estimate`: Estimate an RLVR config's resources without starting anything. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlvr/pipeline`: The ten-stage RLVR plan for a configuration and task set. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlvr/dry-run`: Walk all ten RLVR stages on deterministic inputs; starts nothing. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlvr/evaluate`: Verifier-side metrics against labels recorded before the call. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlvr/runs`: RLVR runs, newest first, filtered by status. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlvr/runs`: Create an RLVR run from a critique dataset (never starts it). *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlvr/runs/start`: Start an RLVR run; unsafe estimates and unconfirmed real runs are refused. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlvr/runs/pause`: Pause a running RLVR run at the next step boundary. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlvr/runs/cancel`: Cancel an RLVR run; its checkpoints are kept. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlvr/runs/resume`: Resume an interrupted RLVR run from its newest valid checkpoint. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlvr/runs/re-estimate`: Re-estimate a run's resources against the current machine reading. *(frontend: no web panel - API/CLI or infrastructure)*
- `POST /rlvr/runs/evaluate`: Compare base, SFT and preference models against the RLVR candidate. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlvr/runs/{run_id}/checkpoints`: An RLVR run's checkpoints with their validity and loadability. *(frontend: no web panel - API/CLI or infrastructure)*
- `GET /rlvr/runs/{run_id}`: One RLVR run: configuration, metrics and verifier snapshot. *(frontend: no web panel - API/CLI or infrastructure)*

<!-- END GENERATED: api-reference -->

## WebSocket Routes

- `WS /ws/events`: event channel with ping/pong and echo behavior

## Authentication

Set `NOVACONTROL_API_TOKEN` to require bearer-token authentication for protected routes.

```powershell
$env:NOVACONTROL_API_TOKEN='change-me'
python -m uvicorn novacontrol.api.app:create_app --factory --reload
```

Example protected request:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/plan `
  -Headers @{ Authorization = 'Bearer change-me' } `
  -ContentType 'application/json' `
  -Body '{"goal":"research options then implement one","execute":true}'
```

## CLI

```powershell
python -m novacontrol demo phase12
```
