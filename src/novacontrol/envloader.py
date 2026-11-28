"""Load a project ``.env`` file into ``os.environ``.

Why this exists
---------------
The application reads every credential and transport setting from the process
environment (``NOVACONTROL_*``, ``NOVAMARKET_SMTP_*``).  Operators naturally put
those values in a ``.env`` file next to the project, but nothing on the Python
side ever read that file — so a correctly written ``.env`` had no effect and the
UI kept reporting "SMTP is not configured".

This loader is deliberately tiny and dependency-free.  It accepts both forms an
operator may reasonably paste:

    NOVAMARKET_SMTP_HOST=smtp.gmail.com
    export NOVAMARKET_SMTP_HOST="smtp.gmail.com"
    $env:NOVAMARKET_SMTP_HOST = "smtp.gmail.com"     # PowerShell

Rules
-----
* The real process environment always wins: values are applied with
  ``setdefault`` semantics, so an explicit ``NOVACONTROL_API_TOKEN=...`` on the
  command line is never silently overridden by a file.
* Parsing never raises.  A malformed line is skipped; a missing/unreadable file
  is a no-op.  A bad ``.env`` must not stop the server.
* Secrets are never logged.  Only variable *names* are returned.
* During a test run the file is ignored (``pytest`` in ``sys.modules``) or when
  ``NOVACONTROL_SKIP_DOTENV`` is set, so a developer's local ``.env`` cannot
  change suite behaviour.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator, MutableMapping
from pathlib import Path

#: ``[export ]NAME=value`` or ``$env:NAME = value``; the key may not contain a
#: colon after the optional ``$env:`` prefix, which keeps Windows paths out.
_LINE = re.compile(
    r"^\s*(?:export\s+)?(?:\$env:)?"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"
    r"\s*=\s*(?P<value>.*?)\s*$"
)

_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_MAX_PARENT_DEPTH = 4


def _flag(value: str | None) -> bool:
    return str(value or "").strip().lower() not in _FALSE_VALUES | {""}


def _unquote(value: str) -> str:
    """Return the value without matching quotes and without trailing comments."""

    text = value.strip()
    if text[:1] in "\"'":
        quote = text[0]
        end = text.find(quote, 1)
        if end > 0:
            inner = text[1:end]
            if quote == '"':
                inner = inner.replace('\\"', '"').replace("\\n", "\n")
            # Anything after the closing quote (a comment) is ignored.
            return inner
        return text
    # An unquoted trailing comment needs whitespace before '#', so a password
    # that legitimately contains '#' is not truncated.
    marker = text.find(" #")
    if marker >= 0:
        return text[:marker].rstrip()
    return text


def _candidate_paths(start: Path) -> Iterator[Path]:
    current = start.resolve()
    for _ in range(_MAX_PARENT_DEPTH):
        yield current / ".env"
        nested = current / "novamarket" / ".env"
        if nested != current / ".env":
            yield nested
        if current.parent == current:
            break
        current = current.parent


def find_env_file(start: Path | None = None) -> Path | None:
    """Return the first existing ``.env`` from ``start`` upwards, if any."""

    base = Path(start) if start is not None else Path.cwd()
    seen: set[Path] = set()
    for candidate in _candidate_paths(base):
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def parse_env_text(text: str) -> dict[str, str]:
    """Parse dotenv text without touching the environment."""

    parsed: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        match = _LINE.match(line)
        if match is None:
            continue
        key = match.group("key")
        parsed[key] = _unquote(match.group("value"))
    return parsed


def load_dotenv(
    path: str | Path | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
    start: str | Path | None = None,
) -> tuple[str, ...]:
    """Apply ``.env`` values and return the names that were applied.

    Existing environment variables are preserved.  Returns names only, never
    values.
    """

    # An explicitly injected mapping means the caller is testing the loader
    # itself, so the suite guard does not apply.
    explicit = environ is not None
    target = os.environ if environ is None else environ
    if _flag(target.get("NOVACONTROL_SKIP_DOTENV")):
        return ()
    if not explicit and "pytest" in sys.modules:
        return ()

    resolved: Path | None
    if path is not None:
        resolved = Path(path)
        if not resolved.is_file():
            return ()
    else:
        resolved = find_env_file(Path(start) if start is not None else None)
        if resolved is None:
            return ()

    try:
        text = resolved.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ()

    applied: list[str] = []
    for key, value in parse_env_text(text).items():
        if key in target:
            continue
        target[key] = value
        applied.append(key)
    return tuple(applied)


__all__ = ["find_env_file", "load_dotenv", "parse_env_text"]
