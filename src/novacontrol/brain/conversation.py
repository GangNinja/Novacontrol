"""Conversation history management for the Brain.

Provides persistent, turn-based conversation state that the Brain can use
to maintain context across multiple interactions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


@dataclass(slots=True)
class ConversationMessage:
    role: str  # "system" | "user" | "assistant"
    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class ConversationTurn:
    user_message: str
    assistant_response: str
    intent: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)


class ConversationManager:
    """Maintains conversation history for a single session.

    Supports:
    - System prompt injection
    - Turn-based history with configurable limits
    - Summarization-ready truncation
    - Export to LLM message format
    """

    def __init__(
        self,
        *,
        system_prompt: str = "",
        max_turns: int = 50,
        max_context_tokens: int = 4000,
    ) -> None:
        self._system_prompt = system_prompt
        self._max_turns = max_turns
        self._max_context_tokens = max_context_tokens
        self._messages: list[ConversationMessage] = []
        self._turns: list[ConversationTurn] = []
        self._session_id = uuid4().hex

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def turn_count(self) -> int:
        return len(self._turns)

    @property
    def message_count(self) -> int:
        return len(self._messages)

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    def add_user_message(self, content: str, *, metadata: dict[str, Any] | None = None) -> None:
        msg = ConversationMessage(role="user", content=content, metadata=metadata or {})
        self._messages.append(msg)
        self._trim_messages()

    def add_assistant_message(self, content: str, *, metadata: dict[str, Any] | None = None) -> None:
        msg = ConversationMessage(role="assistant", content=content, metadata=metadata or {})
        self._messages.append(msg)
        self._trim_messages()

    def add_turn(
        self,
        user_message: str,
        assistant_response: str,
        *,
        intent: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        turn = ConversationTurn(
            user_message=user_message,
            assistant_response=assistant_response,
            intent=intent,
            metadata=metadata or {},
        )
        self._turns.append(turn)
        if len(self._turns) > self._max_turns:
            self._turns = self._turns[-self._max_turns:]

    def to_messages(self) -> list[dict[str, str]]:
        """Export as LLM-ready message list."""
        messages: list[dict[str, str]] = []
        if self._system_prompt:
            messages.append({"role": "system", "content": self._system_prompt})
        for msg in self._messages:
            messages.append({"role": msg.role, "content": msg.content})
        return messages

    def to_context_string(self) -> str:
        """Export as a plain-text context block for the Brain."""
        parts: list[str] = []
        for turn in self._turns[-10:]:  # Last 10 turns
            parts.append(f"User: {turn.user_message}")
            parts.append(f"Assistant: {turn.assistant_response[:200]}")
            if turn.intent:
                parts.append(f"Intent: {turn.intent}")
        return "\n".join(parts)

    def get_last_user_message(self) -> str:
        for msg in reversed(self._messages):
            if msg.role == "user":
                return msg.content
        return ""

    def get_recent_turns(self, n: int = 5) -> list[ConversationTurn]:
        return list(self._turns[-n:])

    def clear(self) -> None:
        self._messages.clear()
        self._turns.clear()

    def summary(self) -> dict[str, Any]:
        return {
            "session_id": self._session_id,
            "message_count": self.message_count,
            "turn_count": self.turn_count,
            "system_prompt": bool(self._system_prompt),
        }

    def _trim_messages(self) -> None:
        """Trim old messages to stay within context limits."""
        # Rough estimate: ~4 chars per token
        total_chars = sum(len(m.content) for m in self._messages)
        max_chars = self._max_context_tokens * 4

        while self._messages and total_chars > max_chars:
            # Never remove system or the most recent messages
            if len(self._messages) <= 4:
                break
            # Remove the oldest non-system messages
            removed = self._messages.pop(0)
            total_chars -= len(removed.content)
