# Tool System

The Phase 4 tool system provides structured tool registration, schema validation, permission checks, human approval integration, and event-driven execution.

## Components

- `ToolSchema`: declares a tool name, description, and parameter list
- `ToolParameter`: typed argument declaration with required flags
- `ToolRegistry`: stores tool implementations and schemas
- `FunctionTool`: adapts a Python callable into the tool protocol
- `ToolExecutor`: validates requests, asks for approval when permissions are required, runs tools, and returns structured results
- `ToolModule`: executes tools from runtime events

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
