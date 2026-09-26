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
- `GET /intelligence`: Global Intelligence Layer: telemetry, findings, and capabilities. *(frontend: no web panel - API/CLI or infrastructure)*
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
