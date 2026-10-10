"""Small JSON state store for local persistence."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _discard(path: Path) -> None:
    """Remove a staging file, never raising: cleanup must not fail a write."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


class JsonStateStore:
    """Persists small module state snapshots as JSON files.

    This store is the application's own BOOTSTRAP state: it is read while
    ``NovaControlApplication`` is being constructed, so a snapshot that cannot be
    read must not raise, and a snapshot that cannot be replaced must not fail the
    request that recorded it.

    * **Reads are forgiving.** A missing file, an empty one, a truncated or
      hand-edited one, one that holds something other than an object, and one
      whose bytes are not UTF-8 text at all all report the same answer as
      "nothing was stored yet" — and the next write replaces it. A process
      killed mid-write (or a second one mid-replacement) used to leave an empty
      file and fail the whole application with a ``JSONDecodeError`` before it
      could start; the same door is open to bytes no decoder accepts (a tool
      writing UTF-16, or a kill that split a multi-byte character), so the read
      closes both.
    * **Writes stage and replace.** The payload goes to a unique temporary file
      in the store's own directory and is moved into place with ``os.replace``,
      the rule the audit trail, the benchmark store and the evaluation stores
      follow: a reader sees the previous snapshot or the complete next one,
      never the half-written bytes between them.
    * **A refused replace degrades, it does not raise.** Windows refuses the move
      while another process holds either path (a second instance, an indexer, a
      virus scanner). The previous in-place write is then used instead: the
      forgiving read above is what makes that safe, and a raise here would fail
      the work that merely wanted to record that it happened.
    * **An unusable root degrades, it does not raise.** A root that no filesystem
      will create — a read-only or missing parent, or a path that is not a
      directory — leaves a store that reads as "nothing was stored yet" and drops
      its snapshots. The constructor is on that same rule because the application
      builds the store while it is starting.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        # Creating the root is a convenience, never a precondition. This call is
        # where the rule above was broken: on Linux a root like
        # ``/definitely/not/a/directory`` cannot be created and the constructor
        # raised out into its caller, while on Windows that exact path silently
        # became a real directory, so the call looked safe everywhere it was
        # exercised. Reads and writes below were already guarded; this was the one
        # door left open.
        with contextlib.suppress(OSError):
            self.root.mkdir(parents=True, exist_ok=True)

    def read(self, name: str) -> dict[str, Any]:
        path = self.root / f"{name}.json"
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            # Unreadable, or not text this store can decode — a file another
            # tool wrote, or a kill mid-write that split a character. Either
            # way there is no state here, and certainly no crash.
            return {}
        try:
            payload = json.loads(text)
        except ValueError:  # empty, truncated or corrupt: no state, not a crash
            return {}
        return dict(payload) if isinstance(payload, dict) else {}

    def write(self, name: str, payload: dict[str, Any]) -> None:
        path = self.root / f"{name}.json"
        text = json.dumps(payload, indent=2, sort_keys=True, default=str)
        # A unique staging name, so two writers never share one file: a fixed
        # ``name.json.tmp`` was the thing another process could hold open. Staging
        # needs a root that exists and accepts writes, so it is inside the guard
        # too: a store whose root is unusable drops the snapshot instead of raising
        # at the work that merely wanted to record that it happened.
        try:
            handle, staged = tempfile.mkstemp(
                dir=str(self.root), prefix=f"{name}-", suffix=".tmp"
            )
        except OSError:
            return
        temporary = Path(staged)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.replace(temporary, path)
            return
        except OSError:
            _discard(temporary)
        try:
            path.write_text(text, encoding="utf-8")
        except OSError:
            # A state snapshot is auxiliary: losing one must not fail the work
            # that produced it, so the failure ends here rather than travelling
            # back into a request that already succeeded.
            return
