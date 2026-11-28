"""Objective → observed-window binding (D12 remediation).

A computer-operation objective that NAMES a window must be grounded on a window
the operating system actually reports, matched unambiguously, and carried through
planning, execution and verification as a stable identity (window handle + pid).

History this module exists for: perception used to take "the first titled window
the probe returned" as the observed window. An objective that named one
application therefore typed into whichever application happened to be enumerated
first, and the verifier — which was handed that same wrong title — confirmed it.
Nothing here falls back to another application: an unbound objective either binds
to exactly one observed window or is refused.

The module is pure: no I/O, no OS calls, no model. Observations are passed in.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "WindowRef",
    "WindowBinding",
    "bind_window",
    "binding_from_dict",
    "normalize_title",
    "observed_windows",
    "requested_target",
    "revalidate",
]


#: Words that name a ROLE, not a window. Binding one of these would recreate the
#: fallback this module removes, so they are never accepted as a target.
_GENERIC_TARGETS = frozenset(
    {
        "a",
        "an",
        "app",
        "application",
        "browser",
        "computer",
        "current",
        "desktop",
        "foreground",
        "my",
        "program",
        "screen",
        "that",
        "the",
        "this",
        "window",
    }
)

_QUOTED = re.compile(
    r"[\"'‘’“”](?P<name>[^\"'‘’“”]{2,60})[\"'‘’“”]"
)
_WINDOW_PHRASE = re.compile(
    r"(?:the\s+|my\s+)?(?P<name>[A-Za-z0-9][\w .'\-]{0,60}?)"
    r"\s+(?:window|app|application|program)\b",
    re.IGNORECASE,
)
_INTO_PHRASE = re.compile(
    r"\b(?:into|in|on|to|within)\s+(?:the\s+|my\s+)?"
    r"(?P<name>[A-Za-z0-9][\w.'\-]{1,60})(?=[\s,.;]|$)",
    re.IGNORECASE,
)

#: Prepositions that separate "what to do" from "where": a window phrase like
#: "type hello into Notepad window" must not capture the whole sentence.
_SPLITTERS = (" into ", " in ", " on ", " to ", " within ", " for ", " from ")


def normalize_title(text: str) -> str:
    """Case/punctuation-insensitive form used for every comparison."""
    cleaned = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())
    return " ".join(cleaned.split())


def _clean_candidate(raw: str) -> str:
    text = str(raw or "").strip().strip(".,;:!?\"'")
    padded = f" {text} "
    for separator in _SPLITTERS:
        if separator in padded:
            text = padded.rsplit(separator, 1)[-1].strip()
            padded = f" {text} "
    text = re.sub(r"^(?:the|my|a|an)\s+", "", text, flags=re.IGNORECASE)
    return text.strip(" .,;:'\"")


def requested_target(goal: str) -> str:
    """The window/application an objective names, or ``""`` when it names none.

    A quoted phrase wins (an operator who quotes a title means that title), then
    an explicit "<name> window" phrase, then an "into/in/on <name>" phrase. A
    generic role word ("the window", "the browser") is not a target: returning
    one would be the same guess this module exists to remove.
    """
    text = str(goal or "")
    if not text.strip():
        return ""
    for pattern in (_QUOTED, _WINDOW_PHRASE, _INTO_PHRASE):
        for match in pattern.finditer(text):
            name = _clean_candidate(match.group("name"))
            normalized = normalize_title(name)
            if not normalized or normalized in _GENERIC_TARGETS:
                continue
            if len(normalized) < 2:
                continue
            return name
    return ""


@dataclass(frozen=True, slots=True)
class WindowRef:
    """One window the OS reported, with the identity that survives renaming."""

    title: str
    handle: int = 0
    pid: int = 0
    process: str = ""
    foreground: bool = False

    @property
    def identity(self) -> tuple[int, int]:
        return (int(self.handle), int(self.pid))

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "handle": self.handle,
            "pid": self.pid,
            "process": self.process,
            "foreground": self.foreground,
        }


@dataclass(frozen=True, slots=True)
class WindowBinding:
    """The result of grounding a requested target on the observed windows."""

    status: str  # bound | absent | ambiguous | none_requested | identity_changed | retitled
    requested: str = ""
    ref: WindowRef | None = None
    candidates: tuple[str, ...] = ()
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "bound" and self.ref is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "requested": self.requested,
            "ref": self.ref.to_dict() if self.ref is not None else None,
            "candidates": list(self.candidates),
            "reason": self.reason,
        }


def binding_from_dict(data: Mapping[str, Any] | None) -> WindowBinding:
    """Rebuild a binding that was serialised into an observation's metadata."""
    source = data if isinstance(data, Mapping) else {}
    ref_data = source.get("ref")
    ref = None
    if isinstance(ref_data, Mapping):
        try:
            handle = int(ref_data.get("handle") or 0)
        except (TypeError, ValueError):
            handle = 0
        try:
            pid = int(ref_data.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        ref = WindowRef(
            title=str(ref_data.get("title") or ""),
            handle=handle,
            pid=pid,
            process=str(ref_data.get("process") or ""),
            foreground=bool(ref_data.get("foreground")),
        )
    return WindowBinding(
        status=str(source.get("status") or "none_requested"),
        requested=str(source.get("requested") or ""),
        ref=ref,
        candidates=tuple(str(item) for item in (source.get("candidates") or ())),
        reason=str(source.get("reason") or ""),
    )


def observed_windows(metadata: Mapping[str, Any] | None) -> tuple[WindowRef, ...]:
    """The windows an observation actually reported, in probe order."""
    source = metadata if isinstance(metadata, Mapping) else {}
    details = source.get("window_details") or ()
    refs: list[WindowRef] = []
    for item in details:
        if not isinstance(item, Mapping):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        try:
            handle = int(item.get("handle") or 0)
        except (TypeError, ValueError):
            handle = 0
        try:
            pid = int(item.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        refs.append(
            WindowRef(
                title=title,
                handle=handle,
                pid=pid,
                process=str(item.get("process") or ""),
                foreground=bool(item.get("foreground")),
            )
        )
    return tuple(refs)


def _exact(refs: Iterable[WindowRef], wanted: str) -> list[WindowRef]:
    return [ref for ref in refs if normalize_title(ref.title) == wanted]


def _partial(refs: Iterable[WindowRef], wanted: str) -> list[WindowRef]:
    """Windows whose title contains the target, or is contained by it.

    "Notepad" matches "Untitled - Notepad", and "NovaGroup1MidAction" matches
    "NovaGroup1MidAction - Tk". The caller still requires exactly one match:
    a similar name is never picked out of several, and an exact match always
    beats a partial one.
    """
    if not wanted:
        return []
    matches: list[WindowRef] = []
    for ref in refs:
        title = normalize_title(ref.title)
        if not title:
            continue
        if wanted in title or title in wanted:
            matches.append(ref)
    return matches


def bind_window(requested: str, windows: Iterable[WindowRef]) -> WindowBinding:
    """Ground a requested target on exactly one observed window, or refuse."""
    refs = tuple(windows)
    wanted_name = str(requested or "").strip()
    wanted = normalize_title(wanted_name)
    if not wanted or wanted in _GENERIC_TARGETS:
        return WindowBinding(
            status="none_requested",
            requested=wanted_name,
            reason="the objective names no specific window",
        )
    exact = _exact(refs, wanted)
    if len(exact) == 1:
        return WindowBinding(
            status="bound",
            requested=wanted_name,
            ref=exact[0],
            reason=f"exactly one observed window is titled {exact[0].title!r}",
        )
    if len(exact) > 1:
        titles = tuple(ref.title for ref in exact)
        return WindowBinding(
            status="ambiguous",
            requested=wanted_name,
            candidates=titles,
            reason=(
                f"{len(exact)} observed windows are titled {wanted_name!r}; "
                "refusing to choose one"
            ),
        )
    partial = _partial(refs, wanted)
    if len(partial) == 1:
        return WindowBinding(
            status="bound",
            requested=wanted_name,
            ref=partial[0],
            reason=(
                f"one observed window matches {wanted_name!r}: "
                f"{partial[0].title!r}"
            ),
        )
    if len(partial) > 1:
        titles = tuple(ref.title for ref in partial)
        return WindowBinding(
            status="ambiguous",
            requested=wanted_name,
            candidates=titles,
            reason=(
                f"{len(partial)} observed windows match {wanted_name!r} "
                f"({', '.join(titles[:4])}); refusing to choose one"
            ),
        )
    return WindowBinding(
        status="absent",
        requested=wanted_name,
        candidates=tuple(ref.title for ref in refs),
        reason=f"no observed window matches {wanted_name!r}",
    )


def revalidate(binding: WindowBinding, windows: Iterable[WindowRef]) -> WindowBinding:
    """Re-check a bound target against a FRESH observation, immediately before input.

    Identity is the window handle plus its process id. A same-title window owned
    by a different process is an identity change, not the same target; a target
    that kept its identity but was retitled is reported too, because verification
    compares titles as well as handles.
    """
    refs = tuple(windows)
    wanted_name = binding.requested
    if binding.ref is None:
        return bind_window(wanted_name, refs)
    identity = binding.ref.identity
    handle, pid = identity
    if handle or pid:
        for ref in refs:
            if (handle and ref.handle == handle) and (not pid or ref.pid == pid):
                if normalize_title(ref.title) != normalize_title(binding.ref.title):
                    return WindowBinding(
                        status="retitled",
                        requested=wanted_name,
                        ref=ref,
                        reason=(
                            f"the bound window was retitled from "
                            f"{binding.ref.title!r} to {ref.title!r}"
                        ),
                    )
                return WindowBinding(
                    status="bound",
                    requested=wanted_name,
                    ref=ref,
                    reason="the bound window is still present with the same identity",
                )
    # The identity is gone. A window wearing the same title is NOT the same
    # target: that is how a different application inherits a title.
    same_title = _exact(refs, normalize_title(binding.ref.title))
    if same_title:
        return WindowBinding(
            status="identity_changed",
            requested=wanted_name,
            candidates=tuple(ref.title for ref in same_title),
            reason=(
                f"a window titled {binding.ref.title!r} exists with a different "
                "handle/pid; the intended target is gone"
            ),
        )
    return WindowBinding(
        status="absent",
        requested=wanted_name,
        candidates=tuple(ref.title for ref in refs),
        reason=f"the bound window {binding.ref.title!r} is no longer present",
    )
