"""The Research specialized agent (Phase 12.2).

Research is *source-aware* here: the answer is not a paragraph, it is a set of
claims, and every claim says where it came from.

    understand the question   -> TaskInterpreter + parse_query (the query frame)
    determine required sources-> the decision: web research or the local index
    search when authorized    -> the EXISTING ExploreService (no second browser)
    retrieve information      -> the page text Explore already read
    compare sources           -> term overlap and cross-source agreement
    identify conflicts        -> negation / figure / antonym disagreement
    synthesize                -> claims, not prose blobs
    provide citations         -> 1-based citation indexes preserved end to end
    evidence vs inference     -> every claim is labelled, and inference carries
                                 the evidence it was drawn from

The two rules the phase states are structurally enforced:

* **Use the existing web/search tools.** The web path calls the injected
  research provider (the application hands over `ExploreService`), so the
  browser, the page reader, the cache and the synthesis model are the ones the
  rest of the system already uses.
* **Distinguish evidence from inference.** :class:`ClaimKind.EVIDENCE` claims
  quote a source sentence and carry at least one citation index; inference
  claims carry the evidence they rest on. `verify_citations` fails the step when
  a citation index has no matching source, so a fabricated reference cannot
  survive verification.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from novacontrol.agentcore.recovery import RecoveryStrategy
from novacontrol.agentcore.verifier import VerificationResult, VerificationStatus
from novacontrol.agents.models import AgentRole, AgentTask
from novacontrol.agents.pipeline import (
    AgentRun,
    PipelineStep,
    RecoveryPlan,
    SpecialistAgent,
    SpecialistDecision,
    SpecialistPipeline,
    StepOutcome,
    StepStatus,
)
from novacontrol.core.security import PermissionScope, RiskLevel
from novacontrol.explore.evidence import build_evidence
from novacontrol.explore.models import ExploreRequest, ResearchSource
from novacontrol.explore.query import QueryFrame, content_terms, parse_query
from novacontrol.reliability.permissions import PermissionDeclaration

__all__ = [
    "Citation",
    "Claim",
    "ClaimKind",
    "Conflict",
    "RECOVERABLE_RESEARCH_STEPS",
    "ResearchAgent",
    "detect_conflicts",
    "research_declarations",
]

#: Pairs whose presence on either side of a shared topic marks a disagreement.
#: Small on purpose: every entry is a claim two sources can only both hold by
#: contradicting each other.
_ANTONYMS: tuple[tuple[str, str], ...] = (
    ("increase", "decrease"),
    ("increases", "decreases"),
    ("increased", "decreased"),
    ("rises", "falls"),
    ("faster", "slower"),
    ("more", "less"),
    ("always", "never"),
    ("supported", "unsupported"),
    ("deprecated", "recommended"),
    ("safe", "unsafe"),
    ("enabled", "disabled"),
    ("required", "optional"),
    ("improved", "worsened"),
    ("higher", "lower"),
)

_NEGATIONS = ("not ", "n't", "never", "no ", "cannot", "without", "unable", "fails to")

_NUMBER = re.compile(r"\b(\d+(?:\.\d+)?)\s*([a-z%]{0,12})\b")

#: How much of two claims' vocabulary must overlap before a disagreement
#: between them is a disagreement about the same thing. Two measures, because
#: one side is often the more verbose one: the Jaccard overlap for claims of
#: similar length, and containment for a short claim that is fully covered by a
#: longer one — without the second, "HNSW indexing increases query speed" and
#: "HNSW indexing decreases query speed for very large collections" are 0.44
#: apart and their contradiction goes unreported.
_TOPIC_OVERLAP = 0.45
_TOPIC_CONTAINMENT = 0.6

_QUICK_MARKERS = ("quick", "brief", "short", "in a nutshell", "summarize")

#: The research steps a retry can actually change: they fetch something that
#: might be there on a second attempt. Everything else in a research run reads
#: evidence the run already holds.
RECOVERABLE_RESEARCH_STEPS = frozenset({"search_sources", "retrieve_evidence"})

#: Asking for a comparison is a comparison request even when the query frame
#: reads it as one subject: the agent compares how the SOURCES describe
#: something, which needs no second noun in the question. "than" and "difference"
#: are here because "is Rust faster than Go?" is a comparison however the frame
#: labelled its subject, and a comparison that skips the comparison step reports
#: agreement it never measured.
_COMPARE_PATTERN = re.compile(
    r"\b(compare|comparison|versus|vs\.?|contrast|difference|differ|than)\b", re.IGNORECASE
)


class ClaimKind(StrEnum):
    """Where a claim came from: a source, or this agent's own reasoning."""

    EVIDENCE = "evidence"
    INFERENCE = "inference"


@dataclass(frozen=True, slots=True)
class Citation:
    """One numbered source, as the answer refers to it."""

    index: int
    title: str
    url: str = ""
    source_type: str = "web"

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "url": self.url,
            "source_type": self.source_type,
        }


@dataclass(frozen=True, slots=True)
class Claim:
    """One statement in the answer, labelled with where it came from."""

    text: str
    kind: ClaimKind
    citations: tuple[int, ...] = ()
    confidence: float = 0.0
    basis: tuple[str, ...] = ()
    id: str = field(default_factory=lambda: uuid4().hex)

    @property
    def marker(self) -> str:
        """The text as it appears in the answer, with its citation markers."""
        if not self.citations:
            return self.text
        return f"{self.text} [{', '.join(str(index) for index in self.citations)}]"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "kind": self.kind.value,
            "citations": list(self.citations),
            "confidence": self.confidence,
            "basis": list(self.basis),
        }


@dataclass(frozen=True, slots=True)
class Conflict:
    """Two sources saying incompatible things about the same topic."""

    kind: str
    topic: str
    left: str
    right: str
    left_source: int = 0
    right_source: int = 0
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "topic": self.topic,
            "left": self.left,
            "right": self.right,
            "left_source": self.left_source,
            "right_source": self.right_source,
            "confidence": self.confidence,
        }


class ResearchProvider(Protocol):
    """The existing research engine, as this agent needs it."""

    async def research(self, request: ExploreRequest) -> Any: ...


class ResearchFn(Protocol):
    """A pre-bound research call (the application's bridge function)."""

    def __call__(self, request: ExploreRequest) -> Awaitable[Any]: ...


def research_declarations() -> Mapping[str, PermissionDeclaration]:
    """How the research actions behave, stated rather than derived.

    Network access is LOW risk because it changes nothing and reads public
    pages — but it is still *declared*, so a build that denies NETWORK_ACCESS
    (offline, air-gapped, or a stricter policy) refuses the search instead of
    silently reaching out.
    """
    read = PermissionDeclaration(
        risk_level=RiskLevel.LOW, required_permission=PermissionScope.FILESYSTEM_READ
    )
    network = PermissionDeclaration(
        risk_level=RiskLevel.LOW,
        required_permission=PermissionScope.NETWORK_ACCESS,
        reversible=True,
        external_side_effect=False,
    )
    return {
        "research.search_web": network,
        "research.retrieve_evidence": read,
        "research.compare_sources": read,
        "research.detect_conflicts": read,
        "research.synthesize": read,
        "research.verify_citations": read,
    }


class ResearchAgent(SpecialistAgent):
    """Answers a research question with cited, source-aware claims."""

    def __init__(
        self,
        *,
        provider: Any | None = None,
        research_fn: ResearchFn | None = None,
        pipeline: SpecialistPipeline | None = None,
        allow_web: bool = True,
        max_sources: int = 6,
        max_claims: int = 6,
        max_conflicts: int = 5,
    ) -> None:
        super().__init__(
            "research-agent",
            AgentRole.RESEARCH,
            "Researches a question through the existing web/search pipeline and answers "
            "with cited claims, labelling evidence apart from inference.",
            pipeline=pipeline,
        )
        self.provider = provider
        self.research_fn = research_fn or (provider.research if provider is not None else None)
        self.allow_web = allow_web
        self.max_sources = max(1, int(max_sources))
        self.max_claims = max(1, int(max_claims))
        self.max_conflicts = max(0, int(max_conflicts))
        # The gathered sources are kept per run *including* their page text:
        # `ResearchSource.to_dict` deliberately omits the content (six pages of
        # article text have no business in a UI payload), and retrieving
        # evidence from a source whose text was dropped is not retrieval at all.
        self._gathered: dict[str, tuple[ResearchSource, ...]] = {}

    # -- declarations ---------------------------------------------------------

    def declarations(self) -> Mapping[str, PermissionDeclaration]:
        return research_declarations()

    def clone(self, pipeline: SpecialistPipeline) -> ResearchAgent:
        """This agent on another pipeline — the same provider, another scope."""
        return ResearchAgent(
            provider=self.provider,
            research_fn=self.research_fn,
            pipeline=pipeline,
            allow_web=self.allow_web,
            max_sources=self.max_sources,
            max_claims=self.max_claims,
            max_conflicts=self.max_conflicts,
        )

    # -- context --------------------------------------------------------------

    def context_query(self, task: AgentTask, interpretation: Any) -> str:
        return task.goal

    def web_available(self) -> bool:
        """Whether a web search is on the table at all (authorization + wiring)."""
        return bool(self.allow_web and self.research_fn is not None)

    # -- decision -------------------------------------------------------------

    def decide(
        self, task: AgentTask, interpretation: Any, context: Mapping[str, Any]
    ) -> SpecialistDecision:
        frame = parse_query(task.goal)
        action = "research"
        if frame.kind == "comparison" or _COMPARE_PATTERN.search(task.goal):
            action = "compare"
        if not self.web_available() or re.search(
            r"\b(local only|offline|no web)\b", task.goal, re.IGNORECASE
        ):
            action = "local"
        depth = "quick" if any(marker in task.goal.lower() for marker in _QUICK_MARKERS) else "deep"
        sources = "local knowledge" if action == "local" else "web"
        return SpecialistDecision(
            action=action,
            reason=(
                f"question frame '{frame.kind}' on {frame.subject or 'the topic'}; "
                f"sources: {sources}"
            ),
            confidence=0.7 if action != "local" else 0.5,
            requires_approval=False,
            data={
                "frame": frame.kind,
                "subject": frame.subject,
                "depth": depth,
                "sources": sources,
            },
        )

    # -- planning -------------------------------------------------------------

    def plan(
        self,
        task: AgentTask,
        interpretation: Any,
        decision: SpecialistDecision,
        context: Mapping[str, Any],
    ) -> tuple[PipelineStep, ...]:
        web = decision.action in {"research", "compare"}
        steps: list[PipelineStep] = []
        if web:
            steps.append(
                PipelineStep(
                    action="search_sources",
                    description=f"Search for sources on: {task.goal}",
                    tool="research.search_web",
                    target=task.goal,
                    expected="A set of sources with titles, URLs and readable text",
                    permission=PermissionScope.NETWORK_ACCESS,
                    requires_approval=False,
                )
            )
        steps.append(
            PipelineStep(
                action="retrieve_evidence",
                description="Retrieve the evidence sentences the sources actually contain",
                tool="research.retrieve_evidence",
                target=task.goal,
                expected="Ranked sentences, each tied to the source it came from",
            )
        )
        if decision.action == "compare":
            steps.append(
                PipelineStep(
                    action="compare_sources",
                    description="Compare what the sources say about each side of the comparison",
                    tool="research.compare_sources",
                    target=task.goal,
                    expected="Per-source agreement and disagreement",
                )
            )
        steps.append(
            PipelineStep(
                action="detect_conflicts",
                description="Look for conflicting claims between sources",
                tool="research.detect_conflicts",
                target=task.goal,
                expected="Any conflicting claims, named with both sides",
            )
        )
        steps.append(
            PipelineStep(
                action="synthesize",
                description="Synthesize the answer, labelling evidence apart from inference",
                tool="research.synthesize",
                target=task.goal,
                expected="Claims marked as evidence or inference, with citations",
            )
        )
        steps.append(
            PipelineStep(
                action="verify_citations",
                description="Check every citation resolves to a real source",
                tool="research.verify_citations",
                target=task.goal,
                expected="Every citation index maps to a gathered source",
            )
        )
        return tuple(steps)

    # -- execution ------------------------------------------------------------

    async def execute(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        handlers: dict[str, Callable[[PipelineStep, AgentRun], Awaitable[StepOutcome]]] = {
            "search_sources": self._do_search_sources,
            "retrieve_evidence": self._do_retrieve_evidence,
            "compare_sources": self._do_compare_sources,
            "detect_conflicts": self._do_detect_conflicts,
            "synthesize": self._do_synthesize,
            "verify_citations": self._do_verify_citations,
        }
        handler = handlers.get(step.action)
        if handler is None:
            return StepOutcome(
                step_id=step.id,
                action=step.action,
                status=StepStatus.SKIPPED,
                detail=f"{step.action} is not an action this agent performs",
                tool=step.tool,
            )
        return await handler(step, run)

    async def _do_search_sources(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        if not self.web_available() or self.research_fn is None:
            return StepOutcome(
                step_id=step.id,
                action=step.action,
                status=StepStatus.SKIPPED,
                detail="web research is not available in this build; using local knowledge",
                tool=step.tool,
            )
        depth = str(run.decision.get("data", {}).get("depth", "deep"))
        try:
            report = await self.research_fn(
                ExploreRequest(
                    topic=step.target or run.goal,
                    depth=depth,
                    max_sources=self.max_sources,
                    include_videos=False,
                )
            )
        except Exception as exc:
            return StepOutcome(
                step_id=step.id,
                action=step.action,
                status=StepStatus.FAILED,
                detail=f"the research provider failed: {type(exc).__name__}: {exc}",
                tool=step.tool,
            )
        sources = self._sources_of(report)
        self._gathered[run.id] = sources
        while len(self._gathered) > 8:
            self._gathered.pop(next(iter(self._gathered)))
        warnings = [str(entry) for entry in getattr(report, "warnings", ()) or ()]
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED if sources else StepStatus.FAILED,
            detail=f"{len(sources)} source(s) gathered" if sources else "no source was returned",
            output={
                "topic": str(getattr(report, "topic", step.target) or step.target),
                "overview": str(getattr(report, "overview", "") or ""),
                "answer": str(getattr(report, "answer", "") or ""),
                "key_points": [str(point) for point in getattr(report, "key_points", ()) or ()],
                "provider_status": str(getattr(report, "provider_status", "") or ""),
                "warnings": warnings,
                "sources": [source.to_dict() for source in sources],
            },
            tool=step.tool,
        )

    async def _do_retrieve_evidence(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        sources = self._gathered_sources(run)
        if not sources:
            local = [line for line in run.context_text.splitlines() if line.strip()]
            if not local:
                return StepOutcome(
                    step_id=step.id,
                    action=step.action,
                    status=StepStatus.SKIPPED,
                    detail="no sources and no local knowledge matched the question",
                    tool=step.tool,
                )
            return StepOutcome(
                step_id=step.id,
                action=step.action,
                status=StepStatus.COMPLETED,
                detail=f"{len(local)} local knowledge line(s) retrieved",
                output={"local": local[:12], "sources": 0},
                tool=step.tool,
            )
        frame = parse_query(run.goal)
        evidence = build_evidence(run.goal, frame, sources, limit=40)
        sentences = [
            {
                "text": sentence.text,
                "source_index": sentence.source_index,
                "score": round(sentence.score, 4),
                "from_page": sentence.from_page,
            }
            for sentence in evidence.sentences
        ]
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED if sentences else StepStatus.FAILED,
            detail=(
                f"{len(sentences)} evidence sentence(s) from {evidence.pages_read} read page(s)"
                if sentences
                else "the gathered sources contained no usable sentence"
            ),
            output={
                "evidence": sentences,
                "pages_read": evidence.pages_read,
                "sources": len(sources),
            },
            tool=step.tool,
        )

    async def _do_compare_sources(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        evidence = self._evidence(run)
        if not evidence:
            return StepOutcome(
                step_id=step.id,
                action=step.action,
                status=StepStatus.SKIPPED,
                detail="there is no evidence to compare across sources",
                tool=step.tool,
            )
        sources = self._gathered_sources(run)
        frame = parse_query(run.goal)
        per_source: dict[int, list[str]] = {}
        for entry in evidence:
            per_source.setdefault(int(entry["source_index"]), []).append(str(entry["text"]))
        compared: list[dict[str, Any]] = []
        for index in sorted(per_source):
            title = sources[index - 1].title if 0 < index <= len(sources) else f"source {index}"
            overlap = self._topic_overlap(run.goal, frame, per_source[index])
            compared.append(
                {
                    "source_index": index,
                    "title": title,
                    "claims": per_source[index][:3],
                    "relevance": round(overlap, 3),
                }
            )
        agreement = self._agreement(run.goal, frame, list(per_source.values()))
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED,
            detail=f"{len(compared)} source(s) compared; cross-source agreement {agreement:.2f}",
            output={"sources": compared, "agreement": round(agreement, 3)},
            tool=step.tool,
        )

    async def _do_detect_conflicts(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        claims = self._evidence_claims(run)
        if not claims:
            return StepOutcome(
                step_id=step.id,
                action=step.action,
                status=StepStatus.SKIPPED,
                detail="there is no evidence to conflict with",
                tool=step.tool,
            )
        conflicts = detect_conflicts(claims, limit=self.max_conflicts)
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED,
            detail=(
                f"{len(conflicts)} conflicting claim pair(s)"
                if conflicts
                else "no conflicting claims were found between the sources"
            ),
            output={"conflicts": [conflict.to_dict() for conflict in conflicts]},
            tool=step.tool,
        )

    async def _do_synthesize(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        citations = self._citations(run)
        evidence_claims = self._evidence_claims(run)
        local_claims = self._local_claims(run)
        for claim in local_claims:
            passes = all(existing.text != claim.text for existing in evidence_claims)
            if passes:
                evidence_claims.append(claim)
        evidence_claims = evidence_claims[: self.max_claims]
        inference = self._inference_claims(run, evidence_claims)
        choices = [*evidence_claims, *inference]
        if not choices:
            return StepOutcome(
                step_id=step.id,
                action=step.action,
                status=StepStatus.FAILED,
                detail="there was nothing to synthesize an answer from",
                tool=step.tool,
            )
        conflicts = self._conflicts(run)
        answer = self._compose_answer(choices, citations, conflicts)
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED,
            detail=(
                f"{len(evidence_claims)} evidence and {len(inference)} inference claim(s), "
                f"{len(citations)} citation(s)"
            ),
            output={
                "answer": answer,
                "claims": [claim.to_dict() for claim in choices],
                "markers": [claim.marker for claim in choices],
                "citations": [citation.to_dict() for citation in citations],
                "conflicts": [conflict.to_dict() for conflict in conflicts],
                "evidence_count": len(evidence_claims),
                "inference_count": len(inference),
            },
            tool=step.tool,
        )

    async def _do_verify_citations(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        claims = self._claims(run)
        citations = self._citations(run)
        indexes = {citation.index for citation in citations}
        unresolved: list[dict[str, Any]] = []
        for claim in claims:
            if claim.kind is ClaimKind.EVIDENCE and not claim.citations:
                unresolved.append({"claim": claim.id, "reason": "evidence without a citation"})
                continue
            for index in claim.citations:
                if index not in indexes:
                    unresolved.append(
                        {"claim": claim.id, "citation": index, "reason": "no such source"}
                    )
            if claim.kind is ClaimKind.INFERENCE and not claim.basis:
                unresolved.append({"claim": claim.id, "reason": "inference without basis"})
        verified = bool(claims) and not unresolved
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED if verified else StepStatus.FAILED,
            detail=(
                f"all {len(claims)} claim(s) carry resolvable citations"
                if verified
                else f"{len(unresolved)} citation problem(s)"
            ),
            output={"verified": verified, "unresolved": unresolved, "citations": len(citations)},
            tool=step.tool,
        )

    # -- verification ---------------------------------------------------------

    def verify(self, step: PipelineStep, outcome: StepOutcome, run: AgentRun) -> VerificationResult:
        if step.action == "search_sources":
            sources = outcome.output.get("sources") or []
            if not outcome.ok or not sources:
                return VerificationResult(
                    status=VerificationStatus.FAIL,
                    expectation=step.expected or "A set of sources",
                    reason=outcome.detail or "no sources were gathered",
                    evidence={"sources": 0},
                    confidence=0.8,
                )
            return VerificationResult(
                status=VerificationStatus.PASS,
                expectation=step.expected or "A set of sources",
                reason=f"{len(sources)} source(s) gathered",
                evidence={"sources": len(sources)},
                confidence=0.85,
            )
        if step.action == "retrieve_evidence":
            evidence = outcome.output.get("evidence") or []
            local = outcome.output.get("local") or []
            if not evidence and not local:
                return VerificationResult(
                    status=VerificationStatus.FAIL,
                    expectation=step.expected or "Ranked sentences tied to their sources",
                    reason=outcome.detail or "no evidence sentence was retrieved",
                    evidence={},
                    confidence=0.8,
                )
            return VerificationResult(
                status=VerificationStatus.PASS,
                expectation=step.expected or "Ranked sentences tied to their sources",
                reason=f"{len(evidence)} sentence(s) retrieved",
                evidence={"evidence": len(evidence), "local": len(local)},
                confidence=0.8,
            )
        if step.action == "detect_conflicts":
            # "No conflicts" is a finding, not a failure: this step verified the
            # absence as well as the presence, and says which it found.
            return VerificationResult(
                status=VerificationStatus.PASS if outcome.ok else VerificationStatus.FAIL,
                expectation=step.expected or "Any conflicting claims, named with both sides",
                reason=outcome.detail,
                evidence={"conflicts": len(outcome.output.get("conflicts") or [])},
                confidence=0.7 if outcome.ok else 0.5,
            )
        if step.action == "synthesize":
            claims = outcome.output.get("claims") or []
            evidence_count = int(outcome.output.get("evidence_count", 0) or 0)
            if not outcome.ok or not claims:
                return VerificationResult(
                    status=VerificationStatus.FAIL,
                    expectation=step.expected or "Claims with citations",
                    reason=outcome.detail or "no claim was synthesized",
                    evidence={},
                    confidence=0.8,
                )
            return VerificationResult(
                status=VerificationStatus.PASS,
                expectation=step.expected or "Claims with citations",
                reason=f"{len(claims)} claim(s), {evidence_count} of them evidence",
                evidence={"claims": len(claims), "evidence": evidence_count},
                confidence=0.85,
            )
        if step.action == "verify_citations":
            verified = bool(outcome.output.get("verified"))
            return VerificationResult(
                status=VerificationStatus.PASS if verified else VerificationStatus.FAIL,
                expectation=step.expected or "Every citation index maps to a gathered source",
                reason=outcome.detail,
                evidence={"unresolved": outcome.output.get("unresolved") or []},
                confidence=0.95 if verified else 0.9,
            )
        return super().verify(step, outcome, run)

    # -- recovery -------------------------------------------------------------

    def recover(
        self, step: PipelineStep, outcome: StepOutcome, attempt: int, run: AgentRun
    ) -> RecoveryPlan:
        """Retry the step that reaches outside this process, once.

        A second `synthesize` cannot change what it synthesizes from — the
        evidence is already in the run — so retrying it spends time to arrive at
        the same failure. The steps that fetch something are the ones a retry can
        actually change, and even those stop after one attempt.
        """
        if step.action in RECOVERABLE_RESEARCH_STEPS:
            if attempt <= 1:
                return RecoveryPlan(
                    diagnosis=outcome.detail or f"{step.action} failed.",
                    strategy=RecoveryStrategy.RETRY,
                    steps=(step,),
                    text=f"Try {step.action} once more before giving up.",
                    confidence=0.5,
                )
            return RecoveryPlan(
                diagnosis=outcome.detail or f"{step.action} failed repeatedly.",
                strategy=RecoveryStrategy.ABORT,
                text=f"{step.action} was already tried twice; stop and report the failure.",
                confidence=0.9,
            )
        return RecoveryPlan(
            diagnosis=outcome.detail or f"{step.action} failed.",
            strategy=RecoveryStrategy.ABORT,
            text=(
                f"{step.action} works on what was already gathered, so a retry would "
                "repeat the same failure."
            ),
            confidence=0.9,
        )

    # -- report ---------------------------------------------------------------

    def report(self, run: AgentRun) -> dict[str, Any]:
        sources = self._gathered_sources(run)
        claims = self._claims(run)
        conflicts = self._conflicts(run)
        comparisons = self._output_of(run, "compare_sources")
        return {
            "question": run.goal,
            "frame": run.decision.get("data", {}),
            "sources": [citation.to_dict() for citation in self._citations(run)],
            "source_count": len(sources),
            "claims": [claim.to_dict() for claim in claims],
            "evidence_count": sum(1 for claim in claims if claim.kind is ClaimKind.EVIDENCE),
            "inference_count": sum(1 for claim in claims if claim.kind is ClaimKind.INFERENCE),
            "conflicts": [conflict.to_dict() for conflict in conflicts],
            "agreement": comparisons.get("agreement", 0.0),
            "answer": self._output_of(run, "synthesize").get("answer", ""),
            "citations_verified": bool(self._output_of(run, "verify_citations").get("verified")),
            "local_only": not sources,
            "provider_status": str(
                self._output_of(run, "search_sources").get("provider_status", "")
            ),
            "warnings": list(self._output_of(run, "search_sources").get("warnings") or []),
        }

    def summarize(self, run: AgentRun) -> str:
        parts = [super().summarize(run)]
        sources = len(self._gathered_sources(run))
        parts.append(f"Sources: {sources}." if sources else "Answered from local knowledge only.")
        claims = self._claims(run)
        if claims:
            evidence = sum(1 for claim in claims if claim.kind is ClaimKind.EVIDENCE)
            parts.append(f"{evidence} evidence and {len(claims) - evidence} inference claim(s).")
        conflicts = self._conflicts(run)
        if conflicts:
            parts.append(f"{len(conflicts)} conflicting claim(s) between sources.")
        return " ".join(parts)

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _sources_of(report: Any) -> tuple[ResearchSource, ...]:
        """The report's sources as typed objects, whether it gave objects or dicts."""
        raw = getattr(report, "sources", None)
        if raw is None and isinstance(report, Mapping):
            raw = report.get("sources")
        sources: list[ResearchSource] = []
        for entry in raw or ():
            if isinstance(entry, ResearchSource):
                sources.append(entry)
                continue
            if isinstance(entry, Mapping):
                sources.append(
                    ResearchSource(
                        title=str(entry.get("title", "")),
                        url=str(entry.get("url", "")),
                        snippet=str(entry.get("snippet", "")),
                        source_type=str(entry.get("source_type", "web")),
                        content=str(entry.get("content", "")),
                    )
                )
        return tuple(source for source in sources if source.title or source.url)

    @staticmethod
    def _output_of(run: AgentRun, action: str) -> dict[str, Any]:
        for outcome in run.outcomes:
            if outcome.action == action and outcome.output:
                return dict(outcome.output)
        return {}

    def _gathered_sources(self, run: AgentRun) -> tuple[ResearchSource, ...]:
        held = self._gathered.get(run.id)
        if held is not None:
            return held
        # Fallback for a run whose sources did not come from this instance
        # (a resumed run): the JSON-safe projection, which has no page text.
        raw = self._output_of(run, "search_sources").get("sources") or []
        return tuple(
            ResearchSource(
                title=str(entry.get("title", "")),
                url=str(entry.get("url", "")),
                snippet=str(entry.get("snippet", "")),
                source_type=str(entry.get("source_type", "web")),
            )
            for entry in raw
            if isinstance(entry, Mapping)
        )

    def _citations(self, run: AgentRun) -> tuple[Citation, ...]:
        sources = self._gathered_sources(run)
        if not sources:
            local = self._output_of(run, "retrieve_evidence").get("local") or []
            if not local:
                return ()
            return (Citation(index=1, title="Local knowledge index", source_type="local"),)
        return tuple(
            Citation(
                index=index,
                title=source.title or f"source {index}",
                url=source.url,
                source_type=source.source_type,
            )
            for index, source in enumerate(sources, start=1)
        )

    def _evidence(self, run: AgentRun) -> list[dict[str, Any]]:
        raw = self._output_of(run, "retrieve_evidence").get("evidence") or []
        return [dict(entry) for entry in raw if isinstance(entry, Mapping)]

    def _evidence_claims(self, run: AgentRun) -> list[Claim]:
        claims: list[Claim] = []
        for index, entry in enumerate(self._evidence(run), start=1):
            text = str(entry.get("text", "")).strip()
            if not text:
                continue
            claims.append(
                Claim(
                    text=text,
                    kind=ClaimKind.EVIDENCE,
                    citations=(int(entry.get("source_index", 0) or 0),),
                    confidence=min(1.0, float(entry.get("score", 0.0) or 0.0) / 5.0),
                    id=f"evidence-{index}",
                )
            )
        return claims

    def _local_claims(self, run: AgentRun) -> list[Claim]:
        lines = self._output_of(run, "retrieve_evidence").get("local") or []
        claims: list[Claim] = []
        for index, line in enumerate(lines[: self.max_claims], start=1):
            text = str(line).strip()
            if not text:
                continue
            claims.append(
                Claim(
                    text=text,
                    kind=ClaimKind.EVIDENCE,
                    citations=(1,),
                    confidence=0.5,
                    id=f"local-{index}",
                )
            )
        return claims

    def _inference_claims(self, run: AgentRun, evidence: Sequence[Claim]) -> list[Claim]:
        """What the agent concluded, labelled as conclusion rather than quotation."""
        basis = tuple(claim.id for claim in evidence[: self.max_claims])
        search = self._output_of(run, "search_sources")
        candidates: list[str] = []
        overview = str(search.get("overview", "")).strip()
        if overview:
            candidates.append(overview)
        for point in search.get("key_points") or []:
            text = str(point).strip()
            if text:
                candidates.append(text)
        comparisons = self._output_of(run, "compare_sources")
        if comparisons:
            best = max(
                (entry for entry in comparisons.get("sources") or [] if isinstance(entry, Mapping)),
                key=lambda entry: float(entry.get("relevance", 0.0) or 0.0),
                default=None,
            )
            if best is not None and float(best.get("relevance", 0.0) or 0.0) > 0:
                candidates.append(
                    f"{best.get('title', 'The best-matching source')} is the most relevant source "
                    f"for this question (relevance {best.get('relevance')})."
                )
        claims: list[Claim] = []
        for index, text in enumerate(candidates[: max(1, self.max_claims // 2)], start=1):
            claims.append(
                Claim(
                    text=text,
                    kind=ClaimKind.INFERENCE,
                    citations=(),
                    confidence=0.5,
                    basis=basis or ("no-evidence",),
                    id=f"inference-{index}",
                )
            )
        return claims

    def _claims(self, run: AgentRun) -> tuple[Claim, ...]:
        raw = self._output_of(run, "synthesize").get("claims") or []
        claims: list[Claim] = []
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            try:
                kind = ClaimKind(str(entry.get("kind", ClaimKind.EVIDENCE.value)))
            except ValueError:
                kind = ClaimKind.EVIDENCE
            claims.append(
                Claim(
                    text=str(entry.get("text", "")),
                    kind=kind,
                    citations=tuple(int(index) for index in entry.get("citations") or ()),
                    confidence=float(entry.get("confidence", 0.0) or 0.0),
                    basis=tuple(str(item) for item in entry.get("basis") or ()),
                    id=str(entry.get("id") or uuid4().hex),
                )
            )
        return tuple(claims)

    def _conflicts(self, run: AgentRun) -> tuple[Conflict, ...]:
        raw = self._output_of(run, "detect_conflicts").get("conflicts") or []
        conflicts: list[Conflict] = []
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            conflicts.append(
                Conflict(
                    kind=str(entry.get("kind", "")),
                    topic=str(entry.get("topic", "")),
                    left=str(entry.get("left", "")),
                    right=str(entry.get("right", "")),
                    left_source=int(entry.get("left_source", 0) or 0),
                    right_source=int(entry.get("right_source", 0) or 0),
                    confidence=float(entry.get("confidence", 0.0) or 0.0),
                )
            )
        return tuple(conflicts)

    @staticmethod
    def _compose_answer(
        claims: Sequence[Claim], citations: Sequence[Citation], conflicts: Sequence[Conflict]
    ) -> str:
        lines = [claim.marker for claim in claims]
        if conflicts:
            lines.append("")
            lines.append("Conflicting claims:")
            for conflict in conflicts:
                lines.append(f"- {conflict.topic}: {conflict.left} [-] {conflict.right}")
        if citations:
            lines.append("")
            lines.append("Sources:")
            for citation in citations:
                label = citation.url or citation.title
                lines.append(f"[{citation.index}] {citation.title} — {label}")
        return "\n".join(lines)

    @staticmethod
    def _topic_overlap(question: str, frame: QueryFrame, sentences: Sequence[str]) -> float:
        wanted = set(content_terms(f"{question} {' '.join(frame.items)}"))
        if not wanted:
            return 0.0
        seen: set[str] = set()
        for sentence in sentences:
            seen.update(content_terms(sentence))
        return len(wanted & seen) / len(wanted)

    @staticmethod
    def _agreement(question: str, frame: QueryFrame, per_source: Sequence[Sequence[str]]) -> float:
        """How much of the question's vocabulary more than one source supports."""
        wanted = set(content_terms(f"{question} {' '.join(frame.items)}"))
        if not wanted or len(per_source) < 2:
            return 0.0
        agreed = 0
        for term in wanted:
            supporting = sum(
                1
                for sentences in per_source
                if any(term in sentence.lower() for sentence in sentences)
            )
            if supporting >= 2:
                agreed += 1
        return agreed / len(wanted)


def detect_conflicts(claims: Sequence[Claim], *, limit: int = 5) -> tuple[Conflict, ...]:
    """Find pairs of claims that cannot both be right.

    Three kinds are recognised, and only where the two claims are demonstrably
    about the same thing (their vocabulary overlaps): a **negation** on one side
    only, a **figure** that does not match, and an **antonym** pair. Anything
    subtler — a difference of emphasis, a narrower scope — is not called a
    conflict, because a fabricated disagreement is worse than silence.
    """
    conflicts: list[Conflict] = []
    for index, left in enumerate(claims):
        if left.kind is not ClaimKind.EVIDENCE:
            continue
        for right in claims[index + 1 :]:
            if right.kind is not ClaimKind.EVIDENCE:
                continue
            if left.citations and right.citations and left.citations == right.citations:
                continue  # one source cannot contradict itself in this sense
            left_terms, right_terms = _terms(left.text), _terms(right.text)
            if not _same_topic(left_terms, right_terms):
                continue
            conflict = _classify(left, right, left_terms, right_terms)
            if conflict is not None:
                conflicts.append(conflict)
            if len(conflicts) >= limit:
                return tuple(conflicts)
    return tuple(conflicts)


def _classify(
    left: Claim, right: Claim, left_terms: frozenset[str], right_terms: frozenset[str]
) -> Conflict | None:
    topic = " ".join(sorted(left_terms & right_terms))[:80]
    left_negated, right_negated = _negated(left.text), _negated(right.text)
    if left_negated != right_negated:
        return Conflict(
            kind="negation",
            topic=topic,
            left=left.text,
            right=right.text,
            left_source=left.citations[0] if left.citations else 0,
            right_source=right.citations[0] if right.citations else 0,
            confidence=0.6,
        )
    left_numbers, right_numbers = _numbers(left.text), _numbers(right.text)
    if left_numbers and right_numbers and left_numbers != right_numbers:
        return Conflict(
            kind="figure",
            topic=topic,
            left=left.text,
            right=right.text,
            left_source=left.citations[0] if left.citations else 0,
            right_source=right.citations[0] if right.citations else 0,
            confidence=0.55,
        )
    for first, second in _ANTONYMS:
        if (first in left_terms and second in right_terms) or (
            second in left_terms and first in right_terms
        ):
            return Conflict(
                kind="antonym",
                topic=topic,
                left=left.text,
                right=right.text,
                left_source=left.citations[0] if left.citations else 0,
                right_source=right.citations[0] if right.citations else 0,
                confidence=0.5,
            )
    return None


def _terms(text: str) -> frozenset[str]:
    return frozenset(content_terms(text))


def _same_topic(left: frozenset[str], right: frozenset[str]) -> bool:
    if not left or not right:
        return False
    shared = len(left & right)
    union = len(left | right)
    jaccard = shared / union
    containment = shared / min(len(left), len(right))
    return jaccard >= _TOPIC_OVERLAP or containment >= _TOPIC_CONTAINMENT


def _negated(text: str) -> bool:
    lowered = f" {text.lower()} "
    return any(marker in lowered for marker in _NEGATIONS)


def _numbers(text: str) -> frozenset[tuple[str, str]]:
    return frozenset(_NUMBER.findall(text.lower()))
