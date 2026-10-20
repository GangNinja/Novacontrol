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
        screenshot_provider: Any | None = None,
        screen_text_reader: Any | None = None,
    ) -> None:
        self._vision = vision_processor
        self._browser = browser_runner
        self._window_probe = window_probe
        self._provider = completion_provider
        # Where a FRESH screenshot comes from when the caller does not supply
        # one, and how text is read off it. Both are injected callables — the
        # same seam ``ScreenFrameSource`` uses — so this module stays out of the
        # desktop package and out of the approval question: the application
        # passes its already approval-gated capture and its OCR reader. With no
        # provider the screen layer is simply not attempted, and the observation
        # says so rather than reporting an unread screen as an empty one.
        self._screenshot_provider = screenshot_provider
        self._screen_text_reader = screen_text_reader

    async def observe(
        self,
        *,
        application: str = "",
        url: str = "",
        screenshot_path: str | None = None,
        include_desktop: bool = False,
        include_dom: bool = True,
        include_semantic: bool = True,
    ) -> UiState:
        captured_path, capture_note = await self._resolve_screenshot(
            screenshot_path, include_desktop=include_desktop
        )
        bundle = await self._collect(
            application=application,
            url=url,
            screenshot_path=captured_path,
            capture_note=capture_note,
            include_desktop=include_desktop,
            include_dom=include_dom,
        )
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

    async def _resolve_screenshot(
        self, screenshot_path: str | None, *, include_desktop: bool
    ) -> tuple[str | None, str]:
        """A path to a FRESH capture, plus an honest note about its provenance.

        An explicit ``screenshot_path`` is used as given — its caller has
        already decided how it was captured. Otherwise a wired provider is
        asked, and EVERY outcome is reported: no provider, a raise, or an empty
        result each yield no path plus a reason. Nothing here lets an older file
        on disk stand in for a fresh observation, which is the difference
        between observing and remembering.
        """
        if screenshot_path:
            return screenshot_path, f"supplied by caller: {screenshot_path}"
        if not include_desktop:
            return None, "not requested for this observation"
        provider = self._screenshot_provider
        if provider is None:
            return None, "unavailable: no screen capture is wired"
        try:
            produced = provider()
            if hasattr(produced, "__await__"):
                produced = await produced
        except Exception as exc:
            return None, f"unavailable: {type(exc).__name__}: {exc}"
        path = str(produced or "").strip()
        if not path:
            return None, "unavailable: the screen capture produced no image"
        return path, f"captured now: {path}"

    # -- layer 2: structured -------------------------------------------------

    async def _collect(
        self,
        *,
        application: str,
        url: str,
        screenshot_path: str | None,
        capture_note: str,
        include_desktop: bool,
        include_dom: bool,
    ) -> _PerceptionBundle:
        elements: list[UiElement] = []
        raw_texts: list[str] = []
        window_title = ""
        resolved_app = application
        source_parts: list[str] = []
        metadata: dict[str, Any] = {}

        # Desktop window probe (structured): what windows exist and are visible.
        # The source is claimed only when the probe actually produced windows.
        # An available-but-empty probe is recorded as empty rather than reported
        # as window evidence — reporting it either way is what once made a blind
        # observation look sourced (the audit's "browser |  | elements=0").
        probe = self._window_probe
        if not include_desktop:
            metadata["window_probe"] = "not requested for this observation"
        elif probe is None:
            metadata["window_probe"] = "unavailable: no probe is wired"
        else:
            try:
                # None means the probe could not read the OS; [] means it read
                # it and found nothing. Collapsing those two would report an
                # unreadable desktop as an empty one.
                raw_lines = await probe()
                lines = [str(line) for line in (raw_lines or []) if str(line).strip()]
                if raw_lines is None:
                    metadata["window_probe"] = "unavailable: the probe could not read windows"
                titles: list[str] = []
                for line in lines:
                    name, _, title = line.partition("|")
                    window_title = window_title or title.strip()
                    resolved_app = resolved_app or name.strip()
                    if title.strip():
                        titles.append(title.strip())
                        elements.append(UiElement(
                            id=f"win:{name.strip()}",
                            type="window",
                            text=title.strip(),
                            layer="structured",
                            confidence=0.6,
                        ))
                if titles:
                    source_parts.append("window_probe")
                    metadata["windows"] = titles
                elif raw_lines is not None:
                    metadata["window_probe"] = "available but reported no windows"
            except Exception as exc:
                metadata["window_probe"] = f"unavailable: {type(exc).__name__}"

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

        # Screen evidence: one fresh capture, read two ways. OCR gives REAL text
        # this machine can read with no model at all; the vision model is
        # consulted only when it can genuinely see (below).
        metadata["screenshot"] = capture_note
        if screenshot_path:
            reader = self._screen_text_reader
            if reader is not None:
                try:
                    words = await reader(screenshot_path)
                    if words is None:
                        metadata["screen_text"] = "unavailable: no OCR engine"
                    else:
                        text = _words_to_text(words)
                        if text:
                            raw_texts.append(text)
                            source_parts.append("screen_text")
                            metadata["screen_text"] = f"{len(words)} word(s) recognized"
                        else:
                            metadata["screen_text"] = "no text recognized on screen"
                except Exception as exc:
                    metadata["screen_text"] = f"unavailable: {type(exc).__name__}"

            # Vision (layer 1): only when the processor can actually SEE. A
            # text-only provider answers about pixels it never received and the
            # dependency-free baseline synthesizes a window title from the image
            # FILE NAME — either would put fabricated text into an observation
            # the verifier trusts. Window titles stay the window probe's job: it
            # reads the OS, so its titles are evidence and a synthesized one is
            # not.
            if self._vision is not None and _vision_can_see(self._vision):
                try:
                    understanding = await self._vision.understand_screen(screenshot_path)
                    summary = str(getattr(understanding, "summary", "") or "")
                    text = str(getattr(understanding, "text", "") or "")
                    if summary or text:
                        raw_texts.append(summary or text)
                        source_parts.append("vision")
                        metadata["vision_summary"] = (summary or text)[:300]
                except Exception as exc:
                    metadata["vision"] = f"unavailable: {type(exc).__name__}"

        if not source_parts:
            source_parts.append("none")
        raw_text = "\n".join(part for part in raw_texts if part).strip()
        # Evidence marker: consumers can tell an observation that carried
        # something verifiable from one that carried nothing, without having to
        # re-derive it from three fields.
        metadata["evidence"] = bool(elements) or bool(raw_text) or bool(url)
        if not metadata["evidence"]:
            metadata["evidence_gap"] = (
                "nothing observable: no window titles, no screen text and no page content"
            )
        return _PerceptionBundle(
            application=resolved_app or "unknown",
            window=window_title,
            url=url,
            elements=tuple(elements),
            raw_text=raw_text,
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


def _words_to_text(words: list[dict[str, Any]]) -> str:
    """OCR word records → readable text, one line per recognized line.

    The engine returns words carrying a line index; grouping by it keeps the
    text the way a person (or a needle match in the verifier) would read it,
    instead of one long word soup where a phrase spanning a line break can no
    longer be found.
    """
    lines: dict[int, list[str]] = {}
    for word in words or []:
        try:
            index = int(word.get("line", 0))
        except (TypeError, ValueError):
            index = 0
        text = str(word.get("text", "")).strip()
        if text:
            lines.setdefault(index, []).append(text)
    return "\n".join(" ".join(lines[key]) for key in sorted(lines))


def _vision_can_see(processor: Any) -> bool:
    """Whether a vision processor can actually SEE, not merely be called.

    A text-only provider returns a confident sentence about pixels it never
    received, and the dependency-free baseline synthesizes a window title from
    the image FILE NAME — a fabricated title being the more dangerous of the
    two, because it reads exactly like a window that exists and would be
    verified against. ``provider_supports_vision`` is the same check
    ``VisionController.has_vision_model`` uses, so the two surfaces cannot
    disagree about what "has a vision model" means.
    """
    if processor is None:
        return False
    try:
        from novacontrol.integrations.llm import provider_supports_vision
    except Exception:  # pragma: no cover - import guard
        return False
    provider = getattr(processor, "llm_provider", None)
    if provider is None:
        provider = getattr(processor, "_llm_provider", None)
    try:
        return bool(provider_supports_vision(provider))
    except Exception:  # pragma: no cover - defensive
        return False
