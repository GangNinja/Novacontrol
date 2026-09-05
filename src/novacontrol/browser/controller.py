"""Browser automation controller."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
import importlib.util
from typing import Any, Protocol, runtime_checkable

from novacontrol.browser.models import (
    BrowserAction,
    BrowserActionResult,
    BrowserActionStatus,
    BrowserActionType,
    BrowserWorkflow,
)
from novacontrol.core.security import (
    ApprovalGateway,
    ApprovalRequest,
    DenyByDefaultApprovalGateway,
    PermissionScope,
    RiskLevel,
)


@runtime_checkable
class BrowserRunner(Protocol):
    async def run(self, action: BrowserAction) -> Mapping[str, Any]:
        """Run a browser action through an adapter such as Playwright."""


class NoopBrowserRunner:
    """Safe runner that records browser intent without launching a browser."""

    @classmethod
    def is_available(cls) -> bool:
        """A noop runner never provides a real browser adapter."""
        return False

    async def run(self, action: BrowserAction) -> Mapping[str, Any]:
        return {"would_run": action.to_dict()}


class PlaywrightBrowserRunner:
    """Runs approved browser actions through Playwright."""

    def __init__(self, *, headless: bool = True, timeout_ms: float = 10_000) -> None:
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._page: Any | None = None

    @classmethod
    def is_available(cls) -> bool:
        return importlib.util.find_spec("playwright") is not None

    async def run(self, action: BrowserAction) -> Mapping[str, Any]:
        if action.type is BrowserActionType.NAVIGATE:
            page = await self._ensure_page()
            response = await page.goto(
                action.target,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
            return await self._page_snapshot(page, status_code=_response_status(response))
        if action.type is BrowserActionType.EXTRACT:
            return await self._extract(action)
        if action.type is BrowserActionType.FILL_FORM:
            return await self._fill_form(action)
        if action.type is BrowserActionType.TEST_WEB_APP:
            return await self._test_web_app(action)
        if action.type is BrowserActionType.SCREENSHOT:
            return await self._screenshot(action)
        if action.type is BrowserActionType.CLICK:
            return await self._click(action)
        if action.type is BrowserActionType.WAIT_FOR:
            return await self._wait_for(action)
        if action.type is BrowserActionType.EVALUATE_JS:
            return await self._evaluate_js(action)
        raise ValueError(f"Unsupported browser action type: {action.type}")

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._playwright = None
        self._browser = None
        self._page = None

    async def _ensure_page(self) -> Any:
        if self._page is not None:
            return self._page
        try:
            from playwright.async_api import async_playwright
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional install
            raise RuntimeError("Playwright is not installed. Run `pip install -e .` first.") from exc

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._page = await self._browser.new_page()
        return self._page

    async def _extract(self, action: BrowserAction) -> Mapping[str, Any]:
        page = await self._ensure_page()
        if action.target != "current_page" and action.target.startswith(("http://", "https://")):
            await page.goto(action.target, wait_until="domcontentloaded", timeout=self.timeout_ms)
        selector = str(action.parameters.get("selector", "body"))
        values = await page.locator(selector).all_text_contents()
        return {
            "adapter": "playwright",
            "selector": selector,
            "url": page.url,
            "matches": [value.strip() for value in values if value.strip()],
        }

    async def _fill_form(self, action: BrowserAction) -> Mapping[str, Any]:
        page = await self._ensure_page()
        if action.target.startswith(("http://", "https://")):
            await page.goto(action.target, wait_until="domcontentloaded", timeout=self.timeout_ms)
        fields = dict(action.parameters.get("fields", {}))
        filled: list[str] = []
        for selector, value in fields.items():
            await page.fill(str(selector), str(value))
            filled.append(str(selector))
        return {"adapter": "playwright", "url": page.url, "filled": filled}

    async def _test_web_app(self, action: BrowserAction) -> Mapping[str, Any]:
        page = await self._ensure_page()
        await page.goto(action.target, wait_until="domcontentloaded", timeout=self.timeout_ms)
        title = await page.title()
        content = await page.content()
        assertions = tuple(str(assertion) for assertion in action.parameters.get("assertions", ()))
        results = [
            {
                "assertion": assertion,
                "passed": _browser_assertion_passed(
                    assertion,
                    title=title,
                    url=page.url,
                    content=content,
                ),
            }
            for assertion in assertions
        ]
        failed = [result for result in results if not result["passed"]]
        if failed:
            raise AssertionError(f"Browser assertions failed: {failed}")
        snapshot = await self._page_snapshot(page)
        return {**snapshot, "assertions": results}

    async def _page_snapshot(self, page: Any, *, status_code: int | None = None) -> Mapping[str, Any]:
        return {
            "adapter": "playwright",
            "url": page.url,
            "title": await page.title(),
            "status_code": status_code,
        }

    async def _screenshot(self, action: BrowserAction) -> Mapping[str, Any]:
        """Take a screenshot of the current page."""
        page = await self._ensure_page()
        save_path = str(action.parameters.get("save_path", "browser_screenshot.png"))
        full_page = bool(action.parameters.get("full_page", False))
        await page.screenshot(path=save_path, full_page=full_page)
        from pathlib import Path
        saved = Path(save_path).resolve()
        return {
            "adapter": "playwright",
            "action": "screenshot",
            "url": page.url,
            "path": str(saved),
            "exists": saved.exists(),
            "size_bytes": saved.stat().st_size if saved.exists() else 0,
        }

    async def _click(self, action: BrowserAction) -> Mapping[str, Any]:
        """Click an element on the page."""
        page = await self._ensure_page()
        selector = action.target
        timeout = int(action.parameters.get("timeout_ms", self.timeout_ms))
        await page.click(selector, timeout=timeout)
        return {
            "adapter": "playwright",
            "action": "click",
            "selector": selector,
            "url": page.url,
        }

    async def _wait_for(self, action: BrowserAction) -> Mapping[str, Any]:
        """Wait for a selector or condition."""
        page = await self._ensure_page()
        selector = action.target
        timeout = int(action.parameters.get("timeout_ms", self.timeout_ms))
        state = action.parameters.get("state", "visible")
        await page.wait_for_selector(selector, state=state, timeout=timeout)
        return {
            "adapter": "playwright",
            "action": "wait_for",
            "selector": selector,
            "state": state,
            "url": page.url,
        }

    async def _evaluate_js(self, action: BrowserAction) -> Mapping[str, Any]:
        """Evaluate JavaScript in the page context."""
        page = await self._ensure_page()
        expression = action.target
        result = await page.evaluate(expression)
        return {
            "adapter": "playwright",
            "action": "evaluate_js",
            "url": page.url,
            "result": result,
        }


class BrowserAutomationController:
    """Plans and executes browser automation workflows."""

    def __init__(
        self,
        *,
        approval_gateway: ApprovalGateway | None = None,
        runner: BrowserRunner | None = None,
    ) -> None:
        self.approval_gateway = approval_gateway or DenyByDefaultApprovalGateway()
        self.runner = runner or NoopBrowserRunner()

    def plan_navigation(self, url: str) -> BrowserWorkflow:
        return BrowserWorkflow(
            name=f"Navigate to {url}",
            actions=(
                BrowserAction(
                    type=BrowserActionType.NAVIGATE,
                    target=url,
                    description=f"Navigate browser to {url}",
                ),
            ),
        )

    def plan_extraction(self, selector: str, *, source: str = "current_page") -> BrowserWorkflow:
        return BrowserWorkflow(
            name="Extract browser data",
            actions=(
                BrowserAction(
                    type=BrowserActionType.EXTRACT,
                    target=source,
                    description=f"Extract data using selector {selector}",
                    parameters={"selector": selector},
                ),
            ),
        )

    def plan_form_fill(self, url: str, fields: Mapping[str, str]) -> BrowserWorkflow:
        return BrowserWorkflow(
            name=f"Fill form at {url}",
            actions=(
                BrowserAction(
                    type=BrowserActionType.NAVIGATE,
                    target=url,
                    description=f"Navigate browser to {url}",
                ),
                BrowserAction(
                    type=BrowserActionType.FILL_FORM,
                    target=url,
                    description="Fill browser form fields.",
                    parameters={"fields": dict(fields)},
                ),
            ),
        )

    def plan_web_test(self, url: str, assertions: tuple[str, ...]) -> BrowserWorkflow:
        return BrowserWorkflow(
            name=f"Test web app at {url}",
            actions=(
                BrowserAction(
                    type=BrowserActionType.NAVIGATE,
                    target=url,
                    description=f"Navigate browser to {url}",
                ),
                BrowserAction(
                    type=BrowserActionType.TEST_WEB_APP,
                    target=url,
                    description="Run browser assertions.",
                    parameters={"assertions": assertions},
                ),
            ),
        )

    async def execute_workflow(
        self,
        workflow: BrowserWorkflow,
        *,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[BrowserActionResult, ...]:
        """Run each action in order, optionally announcing each one as it starts.

        The optional `progress` coroutine is awaited with a human line ("Running
        action 1/2: Navigate to ...") right before the action executes.
        """
        results = []
        total = len(workflow.actions)
        for index, action in enumerate(workflow.actions, start=1):
            if progress is not None:
                await progress(f"Running action {index}/{total}: {action.description}")
            result = await self.execute_action(action)
            results.append(result)
            if result.status is not BrowserActionStatus.COMPLETED:
                break
        return tuple(results)

    async def execute_action(self, action: BrowserAction) -> BrowserActionResult:
        if _requires_approval(action.type):
            approval = await self.approval_gateway.request_approval(
                ApprovalRequest(
                    action=action.description,
                    reason="Sensitive browser automation requires explicit user approval.",
                    permissions=_permissions_for(action.type),
                    risk=RiskLevel.MEDIUM,
                    metadata=action.to_dict(),
                )
            )
            if not approval.approved:
                return BrowserActionResult(
                    action_id=action.id,
                    status=BrowserActionStatus.DENIED,
                    output={},
                    error=approval.reason or "Browser automation was not approved.",
                    approval_id=approval.request_id,
                )

        try:
            output = await self.runner.run(action)
            return BrowserActionResult(
                action_id=action.id,
                status=BrowserActionStatus.COMPLETED,
                output=dict(output),
            )
        except Exception as exc:
            return BrowserActionResult(
                action_id=action.id,
                status=BrowserActionStatus.FAILED,
                output={},
                error=f"{type(exc).__name__}: {exc}",
            )

    async def close(self) -> None:
        close = getattr(self.runner, "close", None)
        if close is not None:
            await close()


def _requires_approval(action_type: BrowserActionType) -> bool:
    return action_type in {
        BrowserActionType.NAVIGATE,
        BrowserActionType.FILL_FORM,
        BrowserActionType.DOWNLOAD,
        BrowserActionType.TEST_WEB_APP,
    }


def _permissions_for(action_type: BrowserActionType) -> tuple[PermissionScope, ...]:
    if action_type is BrowserActionType.DOWNLOAD:
        return (
            PermissionScope.BROWSER_CONTROL,
            PermissionScope.NETWORK_ACCESS,
            PermissionScope.FILESYSTEM_WRITE,
        )
    return (PermissionScope.BROWSER_CONTROL, PermissionScope.NETWORK_ACCESS)


def _response_status(response: Any) -> int | None:
    if response is None:
        return None
    return int(getattr(response, "status", 0)) or None


def _browser_assertion_passed(assertion: str, *, title: str, url: str, content: str) -> bool:
    lowered = assertion.lower()
    if lowered.startswith("title contains:"):
        expected = assertion.split(":", 1)[1].strip()
        return expected.lower() in title.lower()
    if lowered.startswith("url contains:"):
        expected = assertion.split(":", 1)[1].strip()
        return expected.lower() in url.lower()
    if lowered.startswith("text contains:"):
        expected = assertion.split(":", 1)[1].strip()
        return expected.lower() in content.lower()
    return assertion.lower() in content.lower()
