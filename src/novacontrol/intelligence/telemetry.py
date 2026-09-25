"""Interpretation telemetry: the self-improvement feed for the GIL.

Tracks what the layer resolved, how, and where it struggled — failed
interpretations, clarifications, unknown intents, failed entity resolution —
so parsers, intent mappings, and prompts improve from evidence instead of
guesswork. Nothing here modifies production behavior automatically; the data
feeds the self-improvement engine's sandboxed planning.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Kept small: telemetry is a rolling window, not an audit log (the audit log
# already exists for actions). Everything counts, only the last samples are
# kept verbatim.
_MAX_SAMPLES = 50

# The timing fields a local model reports about its own work, in the order a
# request experiences them. Only fields the backend actually reported appear in
# a sample, so an unmeasured path records nothing instead of a zero.
_TIMING_FIELDS: tuple[str, ...] = (
    "model_load_ms",
    "first_token_ms",
    "prompt_eval_ms",
    "decode_ms",
    "total_inference_ms",
    "tokens_per_second",
)


def request_id_for(intent_id: str) -> str:
    """The id that ties one request's understanding to its OUTCOME.

    Derived from the intent's own id rather than minted separately, so the id
    the API and the UI already hold is the one the log carries. A correlatable
    id is what makes "understood correctly, then failed" a discoverable fact
    instead of two unrelated records that happen to share a minute.
    """
    return f"req-{intent_id[:12]}"


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
    outcomes: Counter[str] = field(default_factory=Counter)
    decisions: Counter[str] = field(default_factory=Counter)
    decision_providers: Counter[str] = field(default_factory=Counter)
    decision_fallbacks: int = 0
    latency_ms: list[float] = field(default_factory=list)
    # Per-field samples of the provider's own timings (load, prefill/decode,
    # tokens/second). Kept as a list per field so the summary can report an
    # average rather than only the most recent call.
    model_timings: dict[str, list[float]] = field(
        default_factory=lambda: {name: [] for name in _TIMING_FIELDS}
    )


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
        tool: str = "",
        complexity: str = "",
        request_id: str = "",
        used_model: bool = False,
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
            "tool": tool,
            "complexity": complexity,
            "request_id": request_id,
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "nlu_method": strategy,
            "used_model": used_model,
        })

    def record_escalation(
        self,
        *,
        reason: str,
        normalized: str = "",
        latency_ms: float = 0.0,
        timings: Mapping[str, float] | None = None,
    ) -> None:
        """Record that understanding had to fall back to a language model.

        ``latency_ms`` here is the model's own cost, not the request's — the
        difference between "understanding was slow" and "the model was slow"
        is the whole point of measuring both.

        ``timings`` is the backend's own breakdown of that cost (how long the
        weights took to load, how long before the first token, how fast it then
        decoded). It is optional because a cloud provider reports none, and a
        missing measurement must stay visibly missing.
        """
        self._stats.escalations[reason or "unspecified"] += 1
        measured = {
            name: round(float(timings[name]), 3)
            for name in _TIMING_FIELDS
            if timings is not None
            and isinstance(timings.get(name), (int, float))
            and timings[name] > 0
        }
        for name, value in measured.items():
            self._stats.model_timings[name].append(value)
            del self._stats.model_timings[name][:-_MAX_SAMPLES]
        self._sample({
            "kind": "escalation",
            "reason": reason,
            "normalized": normalized,
            "latency_ms": round(latency_ms, 3),
            **measured,
        })

    def record_decision(
        self,
        *,
        route: str,
        decision_type: str = "",
        capability: str = "",
        model: str = "",
        provider: str = "local",
        reason_code: str = "",
        fallback: bool = False,
        requires_planning: bool = False,
        requires_confirmation: bool = False,
        latency_ms: float = 0.0,
        request_id: str = "",
    ) -> None:
        """Record what the decision layer chose, and who chose it.

        Separate from the resolution record because it answers a different
        question: understanding says WHICH intent was meant, the decision says
        what NovaControl then did about it — and how often it had to leave the
        deterministic path, or fall back from a configured provider to the
        local one. Safe metadata only: no reasoning, no prompts, no text.
        """
        self._stats.decisions[route] += 1
        self._stats.decision_providers[provider] += 1
        if fallback:
            self._stats.decision_fallbacks += 1
        self._sample(
            {
                "kind": "decision",
                "route": route,
                "decision_type": decision_type,
                "capability": capability,
                "model": model,
                "provider": provider,
                "reason_code": reason_code,
                "fallback": fallback,
                "requires_planning": requires_planning,
                "requires_confirmation": requires_confirmation,
                "latency_ms": round(latency_ms, 3),
                "request_id": request_id,
                "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )

    def record_outcome(self, *, request_id: str, success: bool, detail: str = "") -> None:
        """Report what happened AFTER understanding: carried out, or not.

        The engine can only record that a request was understood; whether it
        then succeeded is known one layer up, where the handler runs. Recording
        it against the same ``request_id`` is what closes the loop, and it is
        deliberately separate from the resolution sample so a missing outcome
        means "not reported" rather than "succeeded".
        """
        self._stats.outcomes["success" if success else "failure"] += 1
        # Prefer the RESOLUTION sample. The outcome answers "was the understood
        # request carried out", and the decision record beside it answers a
        # different question — what NovaControl chose to do about it. Attaching
        # to whichever sample happens to be the newest would move the fact the
        # day a second record started sharing the same request id.
        target: dict[str, Any] | None = None
        for sample in reversed(self._stats.samples):
            if sample.get("request_id") != request_id or "kind" not in sample:
                continue
            if sample.get("outcome"):
                return  # already reported; the first word is the true one
            if sample.get("kind") == "resolution":
                target = sample
                break
            target = target if target is not None else sample
        if target is not None:
            target["outcome"] = "success" if success else "failure"
            if detail:
                target["outcome_detail"] = detail

    def record_clarification(
        self, *, question: str, normalized: str = "", route: str = ""
    ) -> None:
        """Record the question AND the route that produced it.

        Most clarifications are route ``clarify``, but not all: a request that
        needs an image is flagged and routed to vision even when the words alone
        resolve to nothing. Counting the route here is what keeps the aggregate
        honest about where a request actually went.
        """
        self._stats.clarifications += 1
        if route:
            self._stats.routes[route] += 1
        self._sample(
            {
                "kind": "clarification",
                "question": question,
                "normalized": normalized,
                "route": route,
            }
        )

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
            # What the model cost us, broken down: how long residency took, how
            # long before the first token, how fast it decoded. Empty when no
            # escalated request has reported timings — an unmeasured path says
            # nothing rather than claiming zero.
            "model_timings": {
                name: round(sum(values) / len(values), 3)
                for name, values in s.model_timings.items()
                if values
            },
            "outcomes": dict(s.outcomes.most_common()),
            "decisions": dict(s.decisions.most_common()),
            "decision_providers": dict(s.decision_providers.most_common()),
            "decision_fallbacks": s.decision_fallbacks,
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
        if s.decision_fallbacks:
            findings.append(
                f"The configured decision provider did not answer {s.decision_fallbacks} "
                "time(s); requests were decided locally instead."
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
