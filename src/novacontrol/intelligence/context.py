"""Interaction context: recent entities, tasks, and environment for reference
resolution. The intelligence layer consumes this to understand incomplete and
anaphoric input ("do the same thing", "that file", "earthdial")."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class InteractionContext:
    """Rolling context of the conversation, task, and environment."""

    # Most-recent-first list of user utterances (normalized).
    utterances: list[str] = field(default_factory=list)
    # Most-recent-first resolved intents (StructuredIntent.to_dict()).
    recent_intents: list[dict[str, Any]] = field(default_factory=list)
    # Named entities mentioned, most-recent-first: {"repository": ["earthdial", ...], ...}
    entities: dict[str, list[str]] = field(default_factory=dict)
    # Current environment snapshot (active app, window, url, ui state).
    environment: dict[str, Any] = field(default_factory=dict)
    # The intent currently awaiting a missing entity (clarification in flight).
    pending_intent: dict[str, Any] | None = None
    # The last executed workflow ("do the same thing" replays this).
    last_workflow: list[str] = field(default_factory=list)

    _LIMIT = 12

    def remember_utterance(self, text: str) -> None:
        if text.strip():
            self.utterances.insert(0, text.strip())
            del self.utterances[self._LIMIT:]

    def remember_intent(self, intent_dict: dict[str, Any]) -> None:
        self.recent_intents.insert(0, intent_dict)
        del self.recent_intents[self._LIMIT:]
        for kind, value in (intent_dict.get("entities") or {}).items():
            if isinstance(value, str) and value.strip():
                bucket = self.entities.setdefault(kind, [])
                if value not in bucket:
                    bucket.insert(0, value)
                    del bucket[self._LIMIT:]

    def set_environment(self, **entries: Any) -> None:
        self.environment.update(entries)

    # -- resolution ---------------------------------------------------------

    def last_entity(self, kind: str) -> str | None:
        bucket = self.entities.get(kind)
        return bucket[0] if bucket else None

    def last_intent(self) -> dict[str, Any] | None:
        return self.recent_intents[0] if self.recent_intents else None

    def active_application(self) -> str:
        return str(self.environment.get("application", "") or "")

    def candidates_for(self, kind: str) -> list[str]:
        """Known entities of a kind, most-recent-first.

        What was said outranks what is merely true of the machine right now,
        so the environment value is offered last — it is a resolution source of
        a weaker kind ("close it" after the app was opened externally).
        """
        known = list(self.entities.get(kind, ()))
        ambient = str(self.environment.get(kind, "") or "")
        if ambient and ambient not in known:
            known.append(ambient)
        return known

    @property
    def active_project(self) -> str:
        """The project in play: what was said, else what the environment knows."""
        return str(self.environment.get("project", "") or self.last_entity("project") or "")

    def recent_files(self, *, limit: int = 5) -> list[str]:
        """Files this conversation has touched, most-recent-first."""
        return self.candidates_for("file")[:limit]

    @property
    def current_task(self) -> str:
        """What the user is in the middle of, as best the context knows."""
        last = self.last_intent()
        if not last:
            return ""
        return str(last.get("goal", "") or last.get("intent", "") or "")

    def resolve_snapshot(self) -> dict[str, Any]:
        """Everything a reference may be resolved against, in one object.

        The context resolver (and, later, a context-aware planner) needs the
        same six answers; assembling them here keeps "what counts as context"
        a decision this module makes rather than each caller's.
        """
        return {
            "active_application": self.active_application(),
            "active_project": self.active_project,
            "recent_files": self.recent_files(),
            "current_task": self.current_task,
            "pending": dict(self.pending_intent) if self.pending_intent else None,
            "recent_utterances": list(self.utterances[:3]),
        }
