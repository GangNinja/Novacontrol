# Desktop Automation

The Phase 7 desktop automation subsystem plans desktop workflows and executes them only after explicit approval.

## Components

- `DesktopAction`: one desktop action such as opening an app, organizing files, or executing a script
- `DesktopWorkflow`: ordered list of desktop actions
- `DesktopAutomationController`: planner and approval-gated executor
- `DesktopCommandRunner`: adapter interface for platform-specific execution
- `NoopDesktopRunner`: safe runner that records intent without changing the desktop
- `AutomationAuditLog` / `InMemoryAutomationAuditLog`: timestamped audit log
  (interface + in-memory store) shared by the desktop and browser controllers —
  it lives in `novacontrol.core.audit` and is re-exported from this package for
  backward compatibility
- `DesktopAutomationModule`: event-driven runtime module

## Approval Rule

Every desktop execution request is sent through `ApprovalGateway`. The default gateway denies execution, so the subsystem is safe until a real approval UI/API is connected.

## Events

- `desktop.workflow_requested`: plan and optionally execute a workflow
- `desktop.workflow_planned`: emitted after planning
- `desktop.workflow_completed`: emitted after approved successful execution
- `desktop.workflow_denied`: emitted when approval is denied
- `desktop.workflow_failed`: emitted when execution fails

## Supported Workflow Plans

- Open application
- Execute script or command
- Organize files by extension
