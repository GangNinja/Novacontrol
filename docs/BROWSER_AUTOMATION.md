# Browser Automation

The Phase 8 browser automation subsystem provides workflow planning, approval-aware execution, and adapter boundaries for Playwright or another browser driver.

## Components

- `BrowserAction`: one browser action such as navigation, extraction, form fill, download, or web test
- `BrowserWorkflow`: ordered list of browser actions
- `BrowserAutomationController`: planner and executor
- `BrowserRunner`: adapter interface for browser drivers
- `NoopBrowserRunner`: safe runner that records intent without launching a browser
- `BrowserAutomationModule`: event-driven runtime module

## Approval Policy

Sensitive browser actions require approval:

- Navigation
- Form filling
- Downloads
- Web app test execution

Extraction from an already-available page context can run without approval because it does not mutate external state by itself.

## Events

- `browser.workflow_requested`: plan and optionally execute a workflow
- `browser.workflow_planned`: emitted after planning
- `browser.workflow_completed`: emitted after successful execution
- `browser.workflow_denied`: emitted when approval is denied
- `browser.workflow_failed`: emitted when execution fails

## Future Adapter

The `BrowserRunner` interface is ready for a Playwright implementation that handles real pages, downloads, selectors, screenshots, and browser-based end-to-end tests.
