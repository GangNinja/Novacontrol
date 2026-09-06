"""Task Interpreter: natural-language goal -> structured goal.

Heuristic, deterministic, and dependency-free: it extracts the goal, explicit
constraints, subtask chains ("open X and do Y and then Z"), required tools,
risks, and whether user confirmation is required. When a real LLM provider is
available it is used only to ENRICH the interpretation (never to invent one),
so the interpreter works identically offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

# Verb -> tool family the planner should reach for.
_TOOL_HINTS: tuple[tuple[str, str], ...] = (
    (r"\b(search|google|look up|find (?:info|information|documentation|docs))\b", "research"),
    (r"\b(browse|navigate|open (?:chrome|edge|firefox|a website|the website)|url|website|github)\b", "browser"),
    (r"\b(click|type|press|scroll|screenshot|open \w+ app|notepad|calculator|steam|settings|explorer)\b", "desktop"),
    (r"\b(whatsapp|sms|text (?:mom|someone|my phone)|call |dial|screenshot on my phone)\b", "phone"),
    (r"\b(summarize|analyse|analyze|compare|extract)\b", "reasoning"),
    (r"\b(run|execute|terminal|command line|shell)\b", "shell"),
    (r"\b(file|folder|directory|organize|move files|read file|write file)\b", "filesystem"),
)

_RISK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(delete|remove|format|erase)\b", "high"),
    (r"\b(send|post|publish|submit|pay|purchase|checkout)\b", "high"),
    (r"\b(install|uninstall|update|settings|password|account)\b", "medium"),
)

_CONFIRM_PATTERNS = r"\b(delete|remove|send|post|publish|pay|purchase|submit|install|uninstall)\b"


@dataclass(frozen=True, slots=True)
class InterpretedGoal:
    """Structured output of the Task Interpreter."""

    goal: str
    subtasks: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    required_information: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    expected_outcome: str = ""
    risks: tuple[str, ...] = ()
    confirmation_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "subtasks": list(self.subtasks),
            "constraints": list(self.constraints),
            "required_information": list(self.required_information),
            "required_tools": list(self.required_tools),
            "expected_outcome": self.expected_outcome,
            "risks": list(self.risks),
            "confirmation_required": self.confirmation_required,
        }


class _CompletionProvider(Protocol):
    async def complete(self, messages: Any, **kwargs: Any) -> str:  # pragma: no cover - protocol
        ...


class TaskInterpreter:
    """Converts natural-language requests into structured goals."""

    def __init__(self, completion_provider: object | None = None) -> None:
        self._provider = completion_provider

    async def interpret(self, request: str) -> InterpretedGoal:
        text = request.strip()
        if not text:
            raise ValueError("A task request is required.")

        subtasks = self._split_subtasks(text)
        tools = self._required_tools(text)
        risks = self._risks(text)
        interpreted = InterpretedGoal(
            goal=text,
            subtasks=subtasks,
            constraints=self._constraints(text),
            required_information=self._required_information(text),
            required_tools=tools,
            expected_outcome=self._expected_outcome(text),
            risks=risks,
            confirmation_required=bool(re.search(_CONFIRM_PATTERNS, text, re.IGNORECASE)),
        )
        enriched = await self._enrich(interpreted)
        return enriched

    # -- heuristics ---------------------------------------------------------

    @staticmethod
    def _split_subtasks(text: str) -> tuple[str, ...]:
        # Split on explicit sequencing words; keep fragments that carry verbs,
        # drop pure connective noise.
        parts = re.split(r",?\s+(?:and then|then|after that|afterwards|next|finally)\s+", text, flags=re.IGNORECASE)
        parts = [part.strip(" ,.") for part in parts if part.strip(" ,.")]
        if len(parts) == 1:
            # "open chrome and search for cats" style chains still count as steps.
            chained = re.split(r"\s+and\s+", text, flags=re.IGNORECASE)
            if len(chained) > 1 and all(re.search(r"\b[a-z]+\b", part) for part in chained):
                parts = [part.strip() for part in chained]
        return tuple(parts) or (text,)

    @staticmethod
    def _required_tools(text: str) -> tuple[str, ...]:
        tools: list[str] = []
        for pattern, tool in _TOOL_HINTS:
            if re.search(pattern, text, re.IGNORECASE) and tool not in tools:
                tools.append(tool)
        return tuple(tools) or ("reasoning",)

    @staticmethod
    def _risks(text: str) -> tuple[str, ...]:
        levels: list[str] = []
        for pattern, level in _RISK_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE) and level not in levels:
                levels.append(level)
        return tuple(levels)

    @staticmethod
    def _constraints(text: str) -> tuple[str, ...]:
        constraints: list[str] = []
        for match in re.finditer(r"\b(without|don't|do not|never|only|must|exactly|no more than)\b([^.,;]*)", text, re.IGNORECASE):
            fragment = f"{match.group(1)} {match.group(2)}".strip()
            if fragment:
                constraints.append(fragment)
        return tuple(constraints)

    @staticmethod
    def _required_information(text: str) -> tuple[str, ...]:
        facts: list[str] = []
        for match in re.finditer(r"\b(?:find|check|get|look up|research)\s+(?:my |the |its |their )?([a-z][a-z0-9 _-]{2,60})", text, re.IGNORECASE):
            facts.append(match.group(1).strip())
        return tuple(facts)

    @staticmethod
    def _expected_outcome(text: str) -> str:
        for match in re.finditer(r"\b(?:so that|in order to|to)\s+([A-Z]?[a-z][^.]{3,120})", text):
            return match.group(1).strip()
        return ""

    # -- optional LLM enrichment -------------------------------------------

    async def _enrich(self, interpreted: InterpretedGoal) -> InterpretedGoal:
        complete = getattr(self._provider, "complete", None) if self._provider else None
        if complete is None:
            return interpreted
        # An echo/deterministic provider "answers" by echoing the prompt back —
        # parsing its output would inject the prompt itself as subtasks. Only
        # trust providers that carry a real model name.
        provider_name = str(getattr(self._provider, "name", "") or "").lower()
        if not provider_name or "echo" in provider_name:
            return interpreted
        try:
            prompt = (
                "Break this user request into a short ordered subtask list for a computer-control agent. "
                "Return one subtask per line, no numbering, maximum 9 lines, nothing else.\n\n"
                f"Request: {interpreted.goal}"
            )
            raw = str(await complete([{"role": "user", "content": prompt}]))
            lines = tuple(
                line.strip(" -0123456789.\t")
                for line in raw.splitlines()
                if line.strip(" -0123456789.\t")
            )
            if not lines:
                return interpreted
            # The LLM refines the subtask list; heuristic fields stay authoritative.
            from dataclasses import replace

            return replace(interpreted, subtasks=lines[:9])
        except Exception:
            return interpreted  # never fail interpretation because enrichment failed
