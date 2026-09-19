"""Shared test helpers used across multiple test modules."""

from __future__ import annotations

import ast
import os
from collections.abc import Mapping, Sequence
from typing import Any

# LLM detection is env-gated at boot AND at chat-time (lazy re-probe); disabling
# it process-wide keeps suites deterministic on hosts where Ollama is running.
# The detection tests pass their own explicit environ to the factory functions.
os.environ.setdefault("NOVACONTROL_DISABLE_OLLAMA", "1")

# The telemetry sampler spawns a PowerShell process per cadence to read GPU,
# network and thermal counters. Tests never need it: /system/telemetry still
# serves the live cheap metrics and reports the sampled ones unavailable, which
# is exactly the degradation the suite pins.
os.environ.setdefault("NOVACONTROL_DISABLE_TELEMETRY_SAMPLER", "1")


def shell_launch_offenders(source_code: str) -> list[str]:
    """AST scan for shell-capable subprocess launches in a module's source.

    Returns human-readable findings (line + form) for any shell=True keyword on a
    subprocess launch, create_subprocess_shell, or os.system / os.popen call, so a
    refactor that reintroduces a shell fails the calling contract test. AST-based:
    comments mentioning these forms cannot trip it.
    """
    launch_names = {"Popen", "run", "call", "check_call", "check_output", "create_subprocess_exec"}
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source_code)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
        if name is None:
            continue
        if name == "create_subprocess_shell":
            offenders.append(f"line {node.lineno}: create_subprocess_shell")
        if name in launch_names:
            shell_kw = next((kw for kw in node.keywords if kw.arg == "shell"), None)
            if shell_kw is not None and isinstance(shell_kw.value, ast.Constant) and shell_kw.value.value is True:
                offenders.append(f"line {node.lineno}: shell=True on {name}")
        if name in {"system", "popen"} and isinstance(func, ast.Attribute) and func.value.id == "os":
            offenders.append(f"line {node.lineno}: os.{name}")
    return offenders

from novacontrol.core.events import Event, EventBus
from novacontrol.core.security import ApprovalDecision, ApprovalRequest


class AllowGateway:
    """Approval gateway that always approves, for testing."""

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(request.id, approved=True, decided_by="test")


class FailingSearchProvider:
    """Search provider that always raises, for testing offline fallback."""

    async def search(self, query: str, *, limit: int = 6) -> tuple[Any, ...]:
        raise OSError("socket blocked")


class FakeSearchProvider:
    """Search provider returning deterministic results."""

    def __init__(self) -> None:
        self.calls = 0

    async def search(self, query: str, *, limit: int = 6) -> tuple[Any, ...]:
        from novacontrol.explore import ResearchSource

        self.calls += 1
        return (
            ResearchSource(
                title=f"{query} basics",
                url="https://example.com/basics",
                snippet="A beginner friendly explanation with examples.",
            ),
            ResearchSource(
                title=f"{query} advanced",
                url="https://example.com/advanced",
                snippet="A deeper explanation of tradeoffs and practical use.",
            ),
        )[:limit]


class FakeVideoProvider:
    """Video provider returning deterministic results."""

    async def search_videos(self, query: str, *, limit: int = 5) -> tuple[Any, ...]:
        from novacontrol.explore import VideoResult

        return (
            VideoResult(
                title=f"{query} full course",
                url="https://video.example.com/watch",
                channel="Example Channel",
                reason="Clear visual walkthrough.",
                thumbnail_url="https://video.example.com/thumb.jpg",
            ),
        )[:limit]


class FailingProvider:
    """LLM provider that always raises."""

    @property
    def name(self) -> str:
        return "failing"

    async def complete(self, messages: Sequence[Mapping[str, str]], **kwargs: object) -> str:
        raise RuntimeError("Simulated LLM failure")


class EchoProvider:
    """LLM provider that echoes the last user message with prefix."""

    def __init__(self, prefix: str = "Echo") -> None:
        self._prefix = prefix + ":" if not prefix.endswith(":") else prefix
        self.call_count = 0

    @property
    def name(self) -> str:
        return "test-echo"

    async def complete(self, messages: Sequence[Mapping[str, str]], **kwargs: object) -> str:
        self.call_count += 1
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return f"{self._prefix} {msg['content']}"
        return f"{self._prefix} (no user message)"


class OfflinePageReader:
    """Page reader that reads nothing, for hermetic tests.

    Reading a source page is real network I/O. Any test that constructs an
    ExploreService without naming a reader would otherwise fetch six live URLs
    and slow the suite down by seconds per case, so the offline reader below
    replaces the DEFAULT one for the whole run (see `install_offline_page_reader`).
    Tests that want the reading path inject their own reader through the
    service's `page_reader=` argument.
    """

    def __init__(self, pages: Mapping[str, str] | None = None, **kwargs: object) -> None:
        self.pages = dict(pages or {})
        self.read_calls: list[str] = []

    def read(self, url: str) -> str:
        self.read_calls.append(url)
        return self.pages.get(url, "")

    async def read_many(self, urls: Sequence[str], *, limit: int | None = None) -> dict[str, str]:
        for url in urls:
            self.read_calls.append(url)
        return {url: text for url in urls if (text := self.pages.get(url, ""))}


def install_offline_page_reader() -> None:
    """Make ExploreService's default page reader read nothing, process-wide."""
    from novacontrol.explore import service as explore_service

    explore_service.PageReader = OfflinePageReader  # type: ignore[misc, assignment]


install_offline_page_reader()


async def collect_events(
    bus: EventBus,
    topic: str,
    trigger_type: str,
    trigger_payload: dict[str, Any],
    start_fn: Any = None,
) -> list[Event]:
    """Subscribe, optionally start a module, publish, and return captured events."""
    seen: list[Event] = []

    async def capture(event: Event) -> None:
        seen.append(event)

    await bus.subscribe(topic, capture)
    if start_fn is not None:
        await start_fn(bus)
    await bus.publish(Event(type=trigger_type, payload=trigger_payload))
    return seen
