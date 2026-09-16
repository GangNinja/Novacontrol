"""Server-persisted chat transcript shared by every browser and client.

The Chat panel's history used to be per-browser localStorage: each new browser
started with an empty thread even though the server remembered the
conversation context. The transcript now lives server-side in the app's
gitignored data dir (chat_transcript.json via JsonStateStore), so every
client — any tab, any browser, the CLI — renders the SAME thread.

Role values mirror the UI's message vocabulary ("user"/"assistant"); `route`
carries the render route of the turn's answer (chat/explore/brain/…), `at` is
a unix-ms timestamp. The store is append-mostly and bounded: the oldest turns
are dropped beyond ``limit`` so the file cannot grow without bound.
"""

from __future__ import annotations

from typing import Any

from novacontrol.persistence import JsonStateStore

DEFAULT_CHAT_TRANSCRIPT_LIMIT = 200


class ChatTranscriptStore:
    """Bounded, JSON-persisted list of chat turns shared by all clients."""

    def __init__(
        self,
        state_store: JsonStateStore | None,
        *,
        limit: int = DEFAULT_CHAT_TRANSCRIPT_LIMIT,
    ) -> None:
        self._store = state_store
        self._limit = max(10, limit)
        # namespace name inside the shared JsonStateStore
        self._namespace = "chat_transcript"

    def turns(self) -> list[dict[str, Any]]:
        """Every persisted turn, oldest first (defensive copies)."""
        if self._store is None:
            return []
        raw = self._store.read(self._namespace).get("turns", [])
        if not isinstance(raw, list):
            return []
        return [dict(turn) for turn in raw if isinstance(turn, dict)]

    def append(self, role: str, text: str, *, route: str = "", at: int | None = None) -> dict[str, Any] | None:
        """Add one turn (newest last, capped); returns the stored turn."""
        clean = str(text or "").strip()
        if not clean or role not in {"user", "assistant"}:
            return None
        turn: dict[str, Any] = {
            "role": role,
            "text": clean[:2000],
            "route": str(route or ""),
            "at": int(at if at is not None else (time_ms())),
        }
        if self._store is None:
            return turn  # no persistence available: turn lives only in this call
        turns = self.turns()
        turns.append(turn)
        self._store.write(self._namespace, {"turns": turns[-self._limit :]})
        return turn

    def clear(self) -> int:
        """Wipe the transcript; returns how many turns were dropped."""
        if self._store is None:
            return 0
        dropped = len(self.turns())
        self._store.write(self._namespace, {"turns": []})
        return dropped

    def replace_all(self, turns: list[dict[str, Any]]) -> int:
        """Overwrite the transcript (one-time localStorage migration); returns
        how many turns were accepted. Existing history is DISCARDED, so only
        the migration path — which runs exactly once per browser — may call it."""
        accepted: list[dict[str, Any]] = []
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            stored = self.append(
                str(turn.get("role", "")),
                str(turn.get("text", "")),
                route=str(turn.get("route", "")),
                at=turn.get("at") if isinstance(turn.get("at"), int) else None,
            )
            if stored is not None:
                accepted.append(stored)
        if self._store is not None:
            self._store.write(self._namespace, {"turns": accepted[-self._limit :]})
        return len(accepted)


def time_ms() -> int:
    """Unix milliseconds (module-level so tests can pin timestamps)."""
    import time

    return int(time.time() * 1000)
