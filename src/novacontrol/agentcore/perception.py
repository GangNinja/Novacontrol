"""Multi-layer UI perception engine.

Layer 1 (visual): screenshots through NovaControl's vision processor.
Layer 2 (structured): browser DOM via Playwright, desktop windows via the
    window probe.
Layer 3 (semantic): the LLM reasons over the fused layers to label what
    elements mean — only when a provider is configured.

Output is one unified `UiState`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from novacontrol.agentcore.ui_state import UiElement, UiState


class _VisionProcessor(Protocol):
    async def understand_screen(self, source: str) -> Any:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True, slots=True)
class _PerceptionBundle:
    application: str
    window: str
    url: str
    elements: tuple[UiElement, ...]
    raw_text: str
    source: str
    metadata: dict[str, Any]


class UiPerceptionEngine:
    """Fuses visual, structured, and semantic perception into a UiState."""

    def __init__(
        self,
        *,
        vision_processor: Any | None = None,
        browser_runner: Any | None = None,
        window_probe: Any | None = None,
        completion_provider: Any | None = None,
    ) -> None:
        self._vision = vision_processor
        self._browser = browser_runner
        self._window_probe = window_probe
        self._provider = completion_provider

    async def observe(
        self,
        *,
        application: str = "",
        url: str = "",
        screenshot_path: str | None = None,
        include_dom: bool = True,
        include_semantic: bool = True,
    ) -> UiState:
        bundle = await self._collect(application=application, url=url, screenshot_path=screenshot_path, include_dom=include_dom)
        if include_semantic and self._provider is not None and bundle.raw_text:
            bundle = await self._add_semantic_layer(bundle)
        return UiState(
            application=bundle.application,
            window=bundle.window,
            url=bundle.url,
            elements=bundle.elements,
            raw_text=bundle.raw_text,
            source=bundle.source,
            observed_at=datetime.now(UTC).isoformat(),
            metadata=bundle.metadata,
        )

    # -- layer 2: structured -------------------------------------------------

    async def _collect(
        self, *, application: str, url: str, screenshot_path: str | None, include_dom: bool
    ) -> _PerceptionBundle:
        elements: list[UiElement] = []
        raw_texts: list[str] = []
        window_title = ""
        resolved_app = application
        source_parts: list[str] = []
        metadata: dict[str, Any] = {}

        # Desktop window probe (structured): what windows exist and are visible.
        probe = self._window_probe
        if probe is not None:
            try:
                lines = await probe()  # callable returning "process|title" lines
                for line in lines or []:
                    name, _, title = str(line).partition("|")
                    window_title = window_title or title.strip()
                    resolved_app = resolved_app or name.strip()
                    if title.strip():
                        elements.append(UiElement(
                            id=f"win:{name.strip()}",
                            type="window",
                            text=title.strip(),
                            layer="structured",
                            confidence=0.6,
                        ))
                source_parts.append("window_probe")
            except Exception:
                metadata["window_probe"] = "unavailable"

        # Browser DOM (structured): interactive elements from the live page.
        browser = self._browser
        if include_dom and browser is not None:
            try:
                result = await browser.run(_dom_probe_action(url))
                payload = dict(result)
                for item in payload.get("interactive_elements", []) or []:
                    elements.append(UiElement(
                        id=str(item.get("id") or f"dom:{len(elements)}"),
                        type=str(item.get("type", "other")),
                        text=str(item.get("text", ""))[:200],
                        enabled=bool(item.get("enabled", True)),
                        visible=bool(item.get("visible", True)),
                        selector=str(item.get("selector", "")),
                        layer="structured",
                        confidence=0.85,
                        attributes={"tag": item.get("tag", "")},
                    ))
                raw_texts.append(str(payload.get("text", "")))
                url = url or str(payload.get("url", ""))
                resolved_app = resolved_app or "browser"
                source_parts.append("browser_dom")
                metadata["dom_url"] = payload.get("url", "")
            except Exception as exc:
                metadata["browser_dom"] = f"unavailable: {type(exc).__name__}"

        # Vision (layer 1): screenshot understanding, used for text and labels
        # that structured layers cannot see (native app canvas, images, PDFs).
        if self._vision is not None and screenshot_path:
            try:
                understanding = await self._vision.understand_screen(screenshot_path)
                summary = str(getattr(understanding, "summary", "") or "")
                text = str(getattr(understanding, "text", "") or "")
                raw_texts.append(summary or text)
                for window in getattr(understanding, "windows", ()) or ():
                    title = str(getattr(window, "title", "") or "")
                    if title:
                        window_title = window_title or title
                        resolved_app = resolved_app or title.split()[0]
                source_parts.append("vision")
                metadata["vision_summary"] = summary[:300]
            except Exception as exc:
                metadata["vision"] = f"unavailable: {type(exc).__name__}"

        if not source_parts:
            source_parts.append("none")
        return _PerceptionBundle(
            application=resolved_app or "unknown",
            window=window_title,
            url=url,
            elements=tuple(elements),
            raw_text="\n".join(part for part in raw_texts if part).strip(),
            source="+".join(source_parts),
            metadata=metadata,
        )

    # -- layer 3: semantic ----------------------------------------------------

    async def _add_semantic_layer(self, bundle: _PerceptionBundle) -> _PerceptionBundle:
        complete = getattr(self._provider, "complete", None)
        if complete is None:
            return bundle
        try:
            listing = "\n".join(
                f"- {element.type}: {element.text!r} (selector={element.selector!r})"
                for element in bundle.elements[:40]
            ) or bundle.raw_text[:2000]
            prompt = (
                "You label UI elements for a computer-control agent. For each numbered element below, "
                "reply with one line 'N: <what it is for>' describing its purpose semantically "
                "(e.g. 'submits the login form'). No extra text.\n\n" + listing
            )
            raw = str(await complete([{"role": "user", "content": prompt}]))
            purposes: dict[int, str] = {}
            for line in raw.splitlines():
                match = re.match(r"\s*(\d+)\s*[:.]\s*(.+)", line)
                if match:
                    purposes[int(match.group(1))] = match.group(2).strip()[:160]
            if not purposes:
                return bundle
            semantic: list[UiElement] = []
            for index, element in enumerate(bundle.elements):
                purpose = purposes.get(index)
                if purpose:
                    semantic.append(UiElement(
                        id=element.id,
                        type=element.type,
                        text=element.text or purpose,
                        enabled=element.enabled,
                        visible=element.visible,
                        location=element.location,
                        selector=element.selector,
                        layer="semantic",
                        confidence=min(0.95, element.confidence + 0.1),
                        attributes={**element.attributes, "purpose": purpose},
                    ))
                else:
                    semantic.append(element)
            return dataclass_replace(bundle, elements=tuple(semantic), source=bundle.source + "+semantic")
        except Exception:
            return bundle  # semantic layer is best-effort, never load-bearing


def dataclass_replace(bundle: _PerceptionBundle, **changes: Any) -> _PerceptionBundle:
    from dataclasses import replace

    return replace(bundle, **changes)


def _dom_probe_action(url: str) -> Any:
    """Build a BrowserAction the existing runner already understands.

    Uses the browser controller's EXTRACT action against 'body' — the runner
    returns page text; the DOM probe decorator below enriches it. Kept as a
    small adapter so agentcore never imports browser models directly.
    """
    from novacontrol.browser.models import BrowserAction, BrowserActionType

    target = url if url.startswith(("http://", "https://")) else "current_page"
    return BrowserAction(
        type=BrowserActionType.EXTRACT,
        target=target,
        description="Perception DOM probe",
        parameters={"selector": "a, button, input, select, textarea, [role=button]"},
    )
