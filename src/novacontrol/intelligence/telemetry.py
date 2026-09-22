"""Interpretation telemetry: the self-improvement feed for the GIL.

Tracks what the layer resolved, how, and where it struggled — failed
interpretations, clarifications, unknown intents, failed entity resolution —
so parsers, intent mappings, and prompts improve from evidence instead of
guesswork. Nothing here modifies production behavior automatically; the data
feeds the self-improvement engine's sandboxed planning.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# Kept small: telemetry is a rolling window, not an audit log (the audit log
# already exists for actions). Everything counts, only the last samples are
# kept verbatim.
_MAX_SAMPLES = 50


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile of a small sample (no numpy dependency)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


@dataclass(slots=True)
class _Stats:
    resolved: Counter[str] = field(default_factory=Counter)
    strategies: Counter[str] = field(default_factory=Counter)
    clarifications: int = 0
    unknown: int = 0
    failed_entities: int = 0
    samples: list[dict[str, Any]] = field(default_factory=list)
    # Where requests went once understood (fast / verify / llm / vision), and
    # how often understanding itself needed a model. These two counters are what
    # makes "is the NLU actually keeping work off the LLM?" answerable.
    routes: Counter[str] = field(default_factory=Counter)
    escalations: Counter[str] = field(default_factory=Counter)
    latency_ms: list[float] = field(default_factory=list)


class InterpretationTelemetry:
    """Rolling, in-memory record of how the GIL is interpreting input."""

    def __init__(self, *, max_samples: int = _MAX_SAMPLES) -> None:
        self._stats = _Stats()
        self._max_samples = max_samples

    # -- record ----------------------------------------------------------------

    def record_resolution(
        self,
        *,
        intent: str,
        strategy: str,
        confidence: float,
        normalized: str = "",
        taught: bool = False,
        route: str = "",
        latency_ms: float = 0.0,
    ) -> None:
        self._stats.resolved[intent] += 1
        self._stats.strategies[strategy] += 1
        if route:
            self._stats.routes[route] += 1
        if latency_ms > 0:
            self._stats.latency_ms.append(latency_ms)
            del self._stats.latency_ms[:-_MAX_SAMPLES]
        self._sample({
            "kind": "resolution",
            "intent": intent,
            "strategy": strategy,
            "confidence": round(confidence, 3),
            "normalized": normalized,
            "taught_variation": taught,
            "route": route,
            "latency_ms": round(latency_ms, 3),
        })

    def record_escalation(self, *, reason: str, normalized: str = "", latency_ms: float = 0.0) -> None:
        """Record that understanding had to fall back to a language model."""
        self._stats.escalations[reason or "unspecified"] += 1
        self._sample({
            "kind": "escalation",
            "reason": reason,
            "normalized": normalized,
            "latency_ms": round(latency_ms, 3),
        })

    def record_clarification(self, *, question: str, normalized: str = "") -> None:
        self._stats.clarifications += 1
        self._sample({"kind": "clarification", "question": question, "normalized": normalized})

    def record_unknown(self, *, normalized: str = "") -> None:
        self._stats.unknown += 1
        self._sample({"kind": "unknown_intent", "normalized": normalized})

    def record_failed_entity(self, *, intent: str, entity_kind: str, normalized: str = "") -> None:
        self._stats.failed_entities += 1
        self._sample({
            "kind": "failed_entity",
            "intent": intent,
            "entity_kind": entity_kind,
            "normalized": normalized,
        })

    # -- read surfaces (self-improvement feed) ---------------------------------

    def to_dict(self) -> dict[str, Any]:
        s = self._stats
        latencies = s.latency_ms
        return {
            "resolved_total": sum(s.resolved.values()),
            "resolved_by_intent": dict(s.resolved.most_common()),
            "resolved_by_strategy": dict(s.strategies.most_common()),
            "routes": dict(s.routes.most_common()),
            "escalations": dict(s.escalations.most_common()),
            "escalation_rate": self._rate(sum(s.escalations.values())),
            "latency_ms": {
                "count": len(latencies),
                "avg": round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
                "max": round(max(latencies), 3) if latencies else 0.0,
                "p95": round(_percentile(latencies, 0.95), 3),
            },
            "clarifications": s.clarifications,
            "unknown_intents": s.unknown,
            "failed_entity_resolutions": s.failed_entities,
            "clarification_rate": self._rate(s.clarifications),
            "recent_samples": list(s.samples[-self._max_samples:]),
        }

    def improvement_findings(self) -> list[str]:
        """Human-readable findings the self-improvement engine can plan on."""
        s = self._stats
        findings: list[str] = []
        total = sum(s.resolved.values()) + s.clarifications + s.unknown
        if total == 0:
            return findings
        rate = self._rate(s.clarifications)
        if rate > 0.3:
            findings.append(
                f"High clarification rate ({rate:.0%}): consider adding intent rules or learned variants."
            )
        if s.unknown > 0:
            findings.append(
                f"{s.unknown} input(s) resolved to no known intent — review recent samples for a new rule."
            )
        if s.failed_entities > 0:
            findings.append(
                f"{s.failed_entities} entity resolution(s) failed — improve context tracking or ask narrower questions."
            )
        fuzzy = s.strategies.get("fuzzy", 0)
        if fuzzy > sum(s.resolved.values()) * 0.25:
            findings.append(
                f"Fuzzy matching dominates ({fuzzy} of {sum(s.resolved.values())}) — add canonical phrasings to the registry."
            )
        escalations = sum(s.escalations.values())
        if total and escalations / total > 0.3:
            findings.append(
                f"Understanding escalates to a language model for {escalations / total:.0%} of requests — "
                "add intent rules or exemplars for the frequent phrasings."
            )
        if s.latency_ms:
            avg = sum(s.latency_ms) / len(s.latency_ms)
            p95 = _percentile(s.latency_ms, 0.95)
            if p95 > 50.0:
                findings.append(
                    f"Understanding latency p95 is {p95:.0f} ms (avg {avg:.0f} ms) — check whether a "
                    "model call is happening on the hot path."
                )
        return findings

    # -- internals ---------------------------------------------------------------

    def _rate(self, count: int) -> float:
        total = sum(self._stats.resolved.values()) + self._stats.clarifications + self._stats.unknown
        return count / total if total else 0.0

    def _sample(self, entry: dict[str, Any]) -> None:
        samples = self._stats.samples
        samples.append(entry)
        if len(samples) > self._max_samples:
            del samples[: len(samples) - self._max_samples]
