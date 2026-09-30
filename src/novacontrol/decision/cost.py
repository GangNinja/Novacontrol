"""Task cost estimation: what a request is likely to cost, before paying it.

Phase 14.6's examples are the acceptance test for this file:

    "What's my RAM usage?"              -> very low  (a local counter, no model)
    "Analyze a large PDF"               -> medium    (a model, document-sized context)
    "Build and test a full application" -> high/very high (a planner, many tool calls)

The estimator combines two things that already exist rather than inventing a
third reading of a request: the counted signals from
``intelligence/complexity.py`` (actions, dependencies, reasoning, research
markers, ambiguity) and a small task-kind table read from the wording — the
same deterministic phrases the rest of the build already routes on. Ambiguous
wording picks the more expensive reading, because under-estimating a cost is
the direction that hurts.

The result is a HINT, never a gate. :meth:`TaskCostEstimator.route_hint` says
whether a deterministic answer can carry the request, whether a lightweight
model is appropriate, and whether the work needs a capable one — and nothing in
this module can refuse a request, which is the specification's own instruction:
"use this information for routing, not as a rigid blocker." Every figure is an
estimate and says so by being one; nothing here claims a measurement.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from novacontrol.core.security import RiskLevel
from novacontrol.intelligence.complexity import (
    Complexity,
    ComplexityAssessment,
    assess,
    signals_for,
)
from novacontrol.intelligence.registry import ExecutionCategory
from novacontrol.optimization.models import CostEstimate, CostLevel

#: Task kinds, most specific first. Each carries the base score, the estimated
#: resident memory a model would need, tool calls, expected seconds, the risk a
#: step of this kind tends to carry, and the reason a person would recognise.
_TASK_KINDS: tuple[tuple[str, re.Pattern[str], dict[str, Any]], ...] = (
    (
        "status",
        re.compile(
            r"\b(?:ram|memory|cpu|gpu|vram|battery|disk|storage|uptime|temperature|"
            r"telemetry|status|how much .* (?:used|free)|system health)\b",
            re.IGNORECASE,
        ),
        {
            "score": 0.02,
            "ram_mb": None,
            "tool_calls": 0,
            "seconds": 0.2,
            "risk": RiskLevel.LOW,
            "model": False,
            "reason": "a reading from this machine's own counters",
        },
    ),
    (
        "automation",
        re.compile(
            r"\b(?:every|schedul(?:e|ed|ing)|remind(?:er)?|daily|weekly|monthly|"
            r"automation|recurring)\b",
            re.IGNORECASE,
        ),
        {
            "score": 0.2,
            "ram_mb": None,
            "tool_calls": 1,
            "seconds": 0.5,
            "risk": RiskLevel.LOW,
            "model": False,
            "reason": "storing a scheduled request, which runs later",
        },
    ),
    (
        "build",
        re.compile(
            r"\b(?:build|implement|refactor|scaffold|rewrite|migrate|"
            r"create (?:an?|the) (?:app|application|project|service|tool)|"
            r"write (?:the )?tests?|test the|full application|end[- ]to[- ]end)\b",
            re.IGNORECASE,
        ),
        {
            "score": 0.92,
            "ram_mb": 4096,
            "tool_calls": 6,
            "seconds": 120.0,
            "risk": RiskLevel.MEDIUM,
            "model": True,
            "reason": "a plan with many steps and files to change",
        },
    ),
    (
        "vision",
        re.compile(
            r"\b(?:screenshot|screen|image|photo|picture|look at|see my|"
            r"what(?:'s| is) on my (?:screen|display))\b",
            re.IGNORECASE,
        ),
        {
            "score": 0.65,
            "ram_mb": 4096,
            "tool_calls": 1,
            "seconds": 15.0,
            "risk": RiskLevel.MEDIUM,
            "model": True,
            "reason": "an image must be looked at by a vision model",
        },
    ),
    (
        "document",
        re.compile(
            r"\b(?:pdf|document|docx|spreadsheet|large file|contract|invoice|"
            r"analys(?:e|is)|summari(?:se|ze)|translat(?:e|ion)|read the file|"
            r"large .* (?:pdf|document|file))\b",
            re.IGNORECASE,
        ),
        {
            "score": 0.55,
            "ram_mb": 3072,
            "tool_calls": 1,
            "seconds": 30.0,
            "risk": RiskLevel.LOW,
            "model": True,
            "reason": "a long document to read and hold in context",
        },
    ),
    (
        "research",
        re.compile(
            r"\b(?:research|search|look up|find out|latest|news|current|today's|"
            r"compare prices|trending|what(?:'s| is) new)\b",
            re.IGNORECASE,
        ),
        {
            "score": 0.5,
            "ram_mb": 2048,
            "tool_calls": 2,
            "seconds": 20.0,
            "risk": RiskLevel.LOW,
            "model": True,
            "reason": "needs information from outside this machine",
        },
    ),
    (
        "code",
        re.compile(
            r"\b(?:code|coding|function|class|module|script|bug|debug|stack trace|"
            r"failing test|type error|lint)\b",
            re.IGNORECASE,
        ),
        {
            "score": 0.5,
            "ram_mb": 2048,
            "tool_calls": 2,
            "seconds": 20.0,
            "risk": RiskLevel.MEDIUM,
            "model": True,
            "reason": "code to read and change, usually with a run to verify",
        },
    ),
    (
        "simple_action",
        re.compile(
            r"\b(?:open|launch|navigate|click|type|press|close|start|play|pause|"
            r"volume|brightness)\b",
            re.IGNORECASE,
        ),
        {
            "score": 0.15,
            "ram_mb": None,
            "tool_calls": 1,
            "seconds": 1.0,
            "risk": RiskLevel.LOW,
            "model": False,
            "reason": "one action this machine already knows how to perform",
        },
    ),
)

#: Where each band starts, as a score threshold. Ordered high-to-low so the
#: first match wins, and the last band is the floor.
_BANDS: tuple[tuple[float, CostLevel], ...] = (
    (0.80, CostLevel.VERY_HIGH),
    (0.60, CostLevel.HIGH),
    (0.35, CostLevel.MEDIUM),
    (0.15, CostLevel.LOW),
    (0.0, CostLevel.TRIVIAL),
)

#: A chat answer with no task signal at all still costs a model call. This is
#: the floor for "a sentence that needs understanding", and it is deliberately
#: above the deterministic kinds rather than zero.
_CHAT_KIND: dict[str, Any] = {
    "score": 0.3,
    "ram_mb": 2048,
    "tool_calls": 0,
    "seconds": 3.0,
    "risk": RiskLevel.LOW,
    "model": True,
    "reason": "a model answer to a sentence",
}


class TaskCostEstimator:
    """Estimate cost from wording plus the counted complexity signals.

    Stateless and synchronous: it is called on the request path, so it performs
    no I/O and holds no locks. Every call re-reads the complexity assessment it
    is given (or computes one), which keeps the estimate consistent with the
    routing decision made from the SAME signals rather than a parallel reading.
    """

    def estimate(
        self,
        text: str,
        *,
        complexity: ComplexityAssessment | None = None,
        categories: Sequence[ExecutionCategory] = (),
        actions: int = 1,
        requires_vision: bool = False,
        cloud_available: bool = False,
    ) -> CostEstimate:
        """The five-band estimate for ``text``, with the reasons behind it."""
        request = str(text or "").strip()
        if not request:
            return CostEstimate(
                level=CostLevel.TRIVIAL,
                score=0.0,
                reasons=("an empty request costs nothing",),
            )
        assessment = complexity or assess(
            signals_for(request, actions=actions, categories=tuple(categories))
        )

        kinds = [
            (name, table)
            for name, pattern, table in _TASK_KINDS
            if pattern.search(request)
        ]
        if not kinds:
            kinds = [("chat", _CHAT_KIND)]
        # The most expensive reading wins: under-estimating is the direction
        # that strands a request halfway through.
        name, table = max(kinds, key=lambda item: float(item[1]["score"]))
        score = float(table["score"])
        reasons: list[str] = [str(table["reason"])]

        # Extra task kinds add cost, capped so a sentence mentioning five
        # ordinary words does not become a "very high" job on breadth alone.
        extra = len({item[0] for item in kinds}) - 1
        if extra:
            score += min(0.16, 0.08 * extra)
            reasons.append(f"{extra} other task kind(s) named in the same request")

        # The counted complexity signals raise the estimate when they show work
        # the task-kind table cannot see: dependencies, reasoning, ambiguity.
        if assessment.score:
            score += min(0.25, 0.25 * assessment.score)
            reasons.extend(assessment.reasons[:3])

        if requires_vision and name != "vision":
            score += 0.15
            reasons.append("the request carries an image")

        score = max(0.0, min(1.0, score))
        level = next(band for threshold, band in _BANDS if score >= threshold)
        model_required = bool(table["model"]) or assessment.needs_model or requires_vision
        ram_mb = table["ram_mb"]
        if ram_mb is None and model_required:
            ram_mb = 2048
            reasons.append("a model is required, so its working set is counted")
        expected_seconds = float(table["seconds"])
        if assessment.needs_planner:
            expected_seconds *= 1.5
        tool_calls = int(table["tool_calls"])
        if assessment.needs_planner and tool_calls:
            tool_calls += 1
        cloud_required = name == "research" and (
            cloud_available or assessment.level is Complexity.COMPLEX
        )
        if cloud_required:
            reasons.append("research of this shape may need an external provider")

        return CostEstimate(
            level=level,
            score=score,
            reasons=tuple(dict.fromkeys(reasons)),
            model_required=model_required,
            cloud_required=cloud_required,
            gpu_required=name == "vision" or requires_vision,
            estimated_ram_mb=int(ram_mb) if ram_mb is not None else None,
            expected_latency_seconds=round(expected_seconds, 1),
            tool_calls=tool_calls,
            risk=table["risk"],
            complexity=assessment.level.value,
            category=name,
        )

    def route_hint(self, estimate: CostEstimate) -> dict[str, Any]:
        """How the router may use the estimate — as a preference, never a gate.

        Three booleans, each answering a question a routing layer already asks:
        can a deterministic path carry this, is a lightweight model enough, and
        does the work need a capable model. ``external_data`` says the request
        wants information from outside the machine, which is the privacy
        policy's business to allow or refuse, not this method's.
        """
        return {
            "deterministic": not estimate.model_required,
            "prefer_lightweight": estimate.level in (CostLevel.TRIVIAL, CostLevel.LOW),
            "requires_capable_model": estimate.level in (
                CostLevel.HIGH,
                CostLevel.VERY_HIGH,
            ),
            "external_data": estimate.category == "research",
        }


__all__ = ["TaskCostEstimator"]
