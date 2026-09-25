"""Normalizing tool output before anything else sees it.

A tool that ran ``pytest`` and failed returns ten thousand lines. Pasting that
into a model's context costs the whole conversation's budget to communicate one
number and one sentence, and it buries the sentence somewhere in the middle. The
normalizer is the layer that decides what a RESULT is, once, for every caller:

    {
        "exit_code": 1,
        "error_summary": "E   AssertionError: expected 3, got 4",
        "relevant_lines": ["FAILED tests/test_math.py::test_add", "..."],
        "lines": 10432,
        "truncated": true
    }

Two rules make this honest rather than lossy:

* **Nothing disappears silently.** Whatever is dropped is counted, and the count
  travels with the result (``truncated``, ``elided``, ``lines``). A caller that
  needs everything can act on that number; a caller that ignores it has still
  been told.
* **Small results are untouched.** ``{"echo": "hello"}`` comes back as
  ``{"echo": "hello"}`` — the layer exists to bound context, not to rewrite every
  value that passes through it. Only terminal-shaped output is always
  summarized, because that is what a terminal result IS.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: What "this line is the interesting one" looks like. Deliberately broad: a
#: missed error line is worse than an extra one, and the cap keeps the noise
#: bounded either way.
#: The exception-name alternative is not decoration: ``\berror\b`` does NOT
#: match ``ValueError`` or ``AssertionError`` — there is no word boundary inside
#: the name — so a pattern with only the plain word missed the single most
#: informative line of a Python traceback (measured: the summary became the
#: 9000-character stdout blob instead of "E ValueError: bad input").
_ERROR_PATTERN = re.compile(
    r"(?:[A-Za-z_]*Error\b|[A-Za-z_]*Exception\b|\b(?:error|errors|fail|failed|"
    r"failing|failure|traceback|assert|denied|refused|timeout|timed out|"
    r"not found|no such file|cannot|can't|unable|fatal|panic|segmentation|abort)\b)",
    re.IGNORECASE,
)

#: Keys whose value is the program's exit status.
_EXIT_CODE_KEYS = ("exit_code", "exitcode", "returncode", "return_code")

#: Keys that hold the program's textual output.
_TEXT_KEYS = ("stdout", "output", "log", "text", "result", "lines", "console")
_ERROR_KEYS = ("stderr", "error", "errors")

#: Fields that are structure rather than bulk and are never truncated away:
#: an exit code reported as "0" is the answer, not context to spend.
_KEEP_ALWAYS_KEYS = frozenset(_EXIT_CODE_KEYS) | {
    "command", "tool", "success", "status", "ok",
}


@dataclass(frozen=True, slots=True)
class NormalizationLimits:
    """How much of a result may survive into a context."""

    max_chars: int = 2000
    max_lines: int = 20
    max_items: int = 20
    max_depth: int = 3
    head_items: int = 12
    tail_items: int = 6
    relevant_lines: int = 8
    #: Longest single line kept. A stack trace's "line" can be a whole minified
    #: bundle or a 9000-character JSON blob on one line, so capping the NUMBER
    #: of lines is not enough to bound the result.
    max_line_chars: int = 400

    def __post_init__(self) -> None:
        for name in (
            "max_chars",
            "max_lines",
            "max_items",
            "max_depth",
            "max_line_chars",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1.")


class OutputNormalizer:
    """Turns a raw tool result into something safe to put in front of a model."""

    def __init__(self, limits: NormalizationLimits | None = None) -> None:
        self.limits = limits or NormalizationLimits()

    def normalize(self, tool_name: str, output: Mapping[str, Any]) -> dict[str, Any]:
        """The bounded, summarized form of one tool result.

        ``tool_name`` is accepted for the traceability of future limits per tool
        and is otherwise unused: the shape of the result, not the name of the
        tool, is what decides how it is summarized.
        """
        del tool_name
        if _is_terminal_output(output):
            return self._terminal(output)
        return self._bounded_mapping(output, depth=0)[0]

    # -- terminal-shaped results ------------------------------------------------

    def _terminal(self, output: Mapping[str, Any]) -> dict[str, Any]:
        stdout = _text_lines(_first_text(output, _TEXT_KEYS))
        stderr = _text_lines(_first_text(output, _ERROR_KEYS))
        code = _exit_code(output)
        combined = [*stderr, *stdout]
        named = [line for line in combined if _ERROR_PATTERN.search(line)]
        # An error line names the problem; failing that, the LAST thing a failed
        # command said is what it was complaining about. Taking the first line of
        # the tail instead reported "line one" for a three-line failure — the
        # oldest thing it said, which is the one line least likely to be the point.
        tail = combined[-self.limits.relevant_lines :]
        if named:
            summary, relevant = _summary_line(named), named
        elif code not in (0, None):
            summary, relevant = _summary_line(tail), tail
        else:
            # A command that succeeded has no failure to summarize, and inventing
            # one out of its last output line is how "ok" became the error.
            summary, relevant = _default_summary(code), []
        bounded_relevant, cut_relevant = _bound_lines(relevant, self.limits.max_line_chars)
        bounded_stdout, cut_stdout = _bound_lines(
            stdout[-self.limits.tail_items :], self.limits.max_line_chars
        )
        bounded_stderr, cut_stderr = _bound_lines(
            stderr[-self.limits.tail_items :], self.limits.max_line_chars
        )
        normalized: dict[str, Any] = {
            "exit_code": code,
            "error_summary": _bound_line(summary, self.limits.max_line_chars),
            "relevant_lines": bounded_relevant[: self.limits.relevant_lines],
            "stdout_tail": bounded_stdout,
            "stderr_tail": bounded_stderr,
            "lines": len(combined),
            "truncated": (
                len(combined) > self.limits.max_lines
                or cut_relevant
                or cut_stdout
                or cut_stderr
            ),
        }
        command = str(output.get("command", "") or "")
        if command:
            normalized["command"] = command
        # Any non-textual structure the tool also returned (counts, paths,
        # matched files) is kept, bounded — it is often the actual answer.
        extra = {
            key: value
            for key, value in output.items()
            if key not in _TEXT_KEYS and key not in _ERROR_KEYS and key not in normalized
        }
        if extra:
            bounded, dropped = self._bounded_mapping(extra, depth=1)
            normalized["data"] = bounded
            if dropped:
                normalized["truncated"] = True
        return normalized

    # -- everything else --------------------------------------------------------

    def _bounded_mapping(
        self, output: Mapping[str, Any], *, depth: int
    ) -> tuple[dict[str, Any], int]:
        """A mapping with every value bounded; also returns the total dropped.

        The per-field counts under ``elided`` are DIRECT (what that field lost)
        while the returned total is cumulative, so a caller can report the size
        of the loss and still see which field caused it.
        """
        bounded: dict[str, Any] = {}
        elided: dict[str, int] = {}
        total = 0
        for key, value in output.items():
            result, dropped = self._bound_value(value, depth=depth, key=str(key))
            bounded[str(key)] = result
            total += dropped
            if dropped:
                elided[str(key)] = dropped
        if elided:
            bounded["elided"] = elided
        return bounded, total

    def _bound_value(self, value: Any, *, depth: int, key: str = "") -> tuple[Any, int]:
        """One value, bounded; returns it with how much was dropped."""
        if key in _KEEP_ALWAYS_KEYS:
            return value, 0
        if isinstance(value, str):
            return self._bound_text(value)
        if isinstance(value, Mapping):
            if depth >= self.limits.max_depth:
                return self._bound_text(str(value))
            return self._bounded_mapping(value, depth=depth + 1)
        if isinstance(value, tuple | list):
            return self._bound_sequence(list(value), depth=depth)
        if isinstance(value, set | frozenset):
            return self._bound_sequence(sorted(value, key=str), depth=depth)
        return value, 0

    def _bound_text(self, value: str) -> tuple[str, int]:
        limit = self.limits.max_chars
        if len(value) <= limit:
            return value, 0
        dropped = len(value) - limit
        return f"{value[:limit]} … {dropped} characters elided …", dropped

    def _bound_sequence(self, values: Sequence[Any], *, depth: int) -> tuple[Any, int]:
        items = [self._bound_value(item, depth=depth + 1)[0] for item in values]
        if len(items) <= self.limits.max_items:
            return items, 0
        head = items[: self.limits.head_items]
        tail = items[-self.limits.tail_items :]
        dropped = len(items) - len(head) - len(tail)
        return (
            [*head, f"… {dropped} items elided …", *tail],
            dropped,
        )


def _is_scalar(value: Any) -> bool:
    return isinstance(value, str | int | float | bool) or value is None


def _is_terminal_output(output: Mapping[str, Any]) -> bool:
    """Whether this looks like the result of running a command."""
    if any(key in output for key in _EXIT_CODE_KEYS):
        return True
    text = _first_text(output, _TEXT_KEYS)
    return bool(text) and len(_text_lines(text)) > 1


def _first_text(output: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, tuple | list) and value:
            return "\n".join(str(item) for item in value)
    return ""


def _text_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _bound_line(line: str, limit: int) -> str:
    """One line, capped — a single line can be as long as the whole output."""
    stripped = line.strip()
    if len(stripped) <= limit:
        return stripped
    return f"{stripped[:limit]} … {len(stripped) - limit} characters elided …"


def _bound_lines(lines: Sequence[str], limit: int) -> tuple[list[str], bool]:
    """Bound each line; also says whether anything had to be cut at all."""
    bounded = [_bound_line(line, limit) for line in lines]
    return bounded, bounded != list(lines)


def _summary_line(lines: Sequence[str]) -> str:
    """The line that names the failure: the first matched one, else the last."""
    if not lines:
        return ""
    return lines[0].strip() if _ERROR_PATTERN.search(lines[0]) else lines[-1].strip()


def _exit_code(output: Mapping[str, Any]) -> int | None:
    for key in _EXIT_CODE_KEYS:
        value = output.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
    return None


def _default_summary(code: int | None) -> str:
    if code is None:
        return ""
    return "The command reported success." if code == 0 else f"The command exited {code}."
