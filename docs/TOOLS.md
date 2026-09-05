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
