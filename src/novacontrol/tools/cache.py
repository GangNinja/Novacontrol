"""Caching tool results — and refusing to, wherever that would be a lie.

Some tool work is expensive and its answer does not change: the installed
applications, the size of the disk, what OS this is, which capabilities exist.
Asking the machine again for the same fact on every request is pure latency. The
other half of that sentence is what usually goes wrong, so this module's rules
are stated in one place and enforced there:

  * **Only a tool may make its results cacheable.** ``ToolMetadata.cache_ttl_s``
    is the tool saying "mine may be reused, for this long". A tool that says
    nothing (0) is never cached, and the executor never assumes.
  * **A volatile operation is never cached, however read-only it is.** Live CPU
    load, current memory use, battery level and network throughput change between
    two calls; a cached one is stale the moment it is reused, and *nobody can tell
    from the value*. ``volatile_values`` names those operations, and
    ``cache_refusal`` explains the skip when it happens.
  * **Only successful results are stored.** A failure, a denial or an empty
    result is a statement about a moment, not a fact about the machine.
  * **The cache is bounded.** ``max_entries`` with least-recently-used eviction,
    so a long-running process cannot grow one dictionary without limit.

Time is injected (``clock``), which is what lets the tests prove expiry without
sleeping — a cache tested by ``sleep(600)`` is a cache nobody tests.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from novacontrol.tools.metadata import ToolMetadata


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One stored result, with the moment it stops being usable."""

    output: Mapping[str, Any]
    stored_at: float
    expires_at: float

    def expired(self, now: float) -> bool:
        return now >= self.expires_at

    @property
    def lifetime_s(self) -> float:
        return max(0.0, self.expires_at - self.stored_at)


class ToolResultCache:
    """A bounded, TTL-honouring store for cacheable tool results."""

    def __init__(
        self,
        *,
        max_entries: int = 256,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1.")
        self.max_entries = max_entries
        self._clock = clock or time.monotonic
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.stores = 0
        self.refusals: dict[str, int] = {}
        self.evictions = 0

    # -- lookups ----------------------------------------------------------------

    @staticmethod
    def key(tool_name: str, arguments: Mapping[str, Any] | None = None) -> str:
        """A stable key for one tool call.

        Arguments are canonicalized (sorted keys, no whitespace) so that the same
        call written two ways is one cache entry — and so that a caller cannot
        accidentally read another call's result by reordering a dictionary.
        """
        payload = json.dumps(
            dict(arguments or {}), sort_keys=True, default=str, separators=(",", ":")
        )
        return f"{tool_name}:{payload}"

    def get(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        metadata: ToolMetadata | None = None,
    ) -> Mapping[str, Any] | None:
        """The stored result for this call, or ``None`` — never a stale one."""
        refusal = _refusal(metadata, arguments or {})
        if refusal:
            self._record_refusal(refusal)
            return None
        key = self.key(tool_name, arguments)
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        if entry.expired(self._clock()):
            # Expiry is deletion, not a soft miss: keeping it would leave a stale
            # value one lookup away from being served.
            del self._entries[key]
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return entry.output

    def set(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
        output: Mapping[str, Any],
        *,
        metadata: ToolMetadata | None = None,
    ) -> bool:
        """Store a result when the tool's own contract allows it.

        There is no caller-supplied TTL: how long a tool's answer stays true is
        the tool's claim to make, and a caller that could extend it would be the
        second opinion this cache exists to avoid. Returns whether it stored, so
        the caller can report the decision instead of guessing at it.
        """
        refusal = _refusal(metadata, arguments or {})
        if refusal:
            self._record_refusal(refusal)
            return False
        if not output:
            # An empty result is usually "nothing matched right now", which is
            # exactly the kind of answer that must not be remembered.
            self._record_refusal(f"{tool_name} returned nothing to reuse.")
            return False
        lifetime = metadata.cache_ttl_s if metadata is not None else 0.0
        if lifetime <= 0:
            return False
        now = self._clock()
        key = self.key(tool_name, arguments)
        self._entries[key] = CacheEntry(
            output=dict(output), stored_at=now, expires_at=now + lifetime
        )
        self._entries.move_to_end(key)
        self.stores += 1
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
            self.evictions += 1
        return True

    # -- housekeeping -----------------------------------------------------------

    def invalidate(self, tool_name: str = "") -> int:
        """Drop one tool's entries (or every entry), returning how many went."""
        if not tool_name:
            removed = len(self._entries)
            self._entries.clear()
            return removed
        prefix = f"{tool_name}:"
        keys = [key for key in self._entries if key.startswith(prefix)]
        for key in keys:
            del self._entries[key]
        return len(keys)

    def prune(self) -> int:
        """Remove everything already expired; returns how many were dropped."""
        now = self._clock()
        expired = [key for key, entry in self._entries.items() if entry.expired(now)]
        for key in expired:
            del self._entries[key]
        return len(expired)

    @property
    def size(self) -> int:
        return len(self._entries)

    def _record_refusal(self, reason: str) -> None:
        self.refusals[reason] = self.refusals.get(reason, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        """What the cache did, including what it declined to do and why."""
        return {
            "entries": len(self._entries),
            "max_entries": self.max_entries,
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "evictions": self.evictions,
            "refusals": dict(sorted(self.refusals.items())),
        }


def _refusal(metadata: ToolMetadata | None, arguments: Mapping[str, Any]) -> str:
    """Why this call may not be cached at all (empty when it may be)."""
    if metadata is None:
        return "This tool has no metadata, so nothing is known about caching it."
    return metadata.cache_refusal(arguments)
