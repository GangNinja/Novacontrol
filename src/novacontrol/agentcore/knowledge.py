"""Application Knowledge Graph.

Stores how to operate applications: UI paths, element names, alternative
paths, successful and failed actions, with CONFIDENCE and FRESHNESS so old
knowledge is proposed but never blindly trusted (the Verifier still checks
every assumption against the live environment).

Persisted through NovaControl's existing JsonStateStore; no new database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

STALE_DAYS = 14  # after this, confidence decays until the path is re-verified


@dataclass(frozen=True, slots=True)
class ApplicationKnowledgeNode:
    application: str
    area: str                       # e.g. "Settings > Integrations"
    element: str                    # semantic element name on that screen
    selector: str = ""
    children: tuple[str, ...] = ()
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "application": self.application,
            "area": self.area,
            "element": self.element,
            "selector": self.selector,
            "children": list(self.children),
            "notes": self.notes,
        }


@dataclass(slots=True)
class WorkflowKnowledge:
    """One recorded way to accomplish a task in one application."""

    application: str
    task: str
    steps: tuple[str, ...]
    confidence: float = 0.5
    source: str = "observed"          # observed | researched | user_taught
    last_verified: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    failures: int = 0
    successes: int = 0
    alternatives: tuple[str, ...] = ()

    def effective_confidence(self) -> float:
        """Confidence with freshness decay applied."""
        try:
            verified = datetime.fromisoformat(self.last_verified)
            age_days = (datetime.now(UTC) - verified).days
        except (TypeError, ValueError):
            age_days = STALE_DAYS
        decay = max(0.4, 1.0 - 0.05 * max(0, age_days) / STALE_DAYS)
        base = self.confidence + 0.05 * self.successes - 0.1 * self.failures
        return round(max(0.05, min(0.99, base)) * decay, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "application": self.application,
            "task": self.task,
            "steps": list(self.steps),
            "confidence": self.confidence,
            "effective_confidence": self.effective_confidence(),
            "source": self.source,
            "last_verified": self.last_verified,
            "failures": self.failures,
            "successes": self.successes,
            "alternatives": list(self.alternatives),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkflowKnowledge":
        return cls(
            application=str(payload.get("application", "")),
            task=str(payload.get("task", "")),
            steps=tuple(str(s) for s in payload.get("steps", ())),
            confidence=float(payload.get("confidence", 0.5)),
            source=str(payload.get("source", "observed")),
            last_verified=str(payload.get("last_verified", datetime.now(UTC).isoformat())),
            failures=int(payload.get("failures", 0)),
            successes=int(payload.get("successes", 0)),
            alternatives=tuple(str(a) for a in payload.get("alternatives", ())),
        )


class ApplicationKnowledgeGraph:
    """In-memory graph persisted as JSON through JsonStateStore-compatible IO."""

    def __init__(self, snapshot: dict[str, Any] | None = None) -> None:
        self._nodes: dict[str, ApplicationKnowledgeNode] = {}
        self._workflows: dict[str, WorkflowKnowledge] = {}
        if snapshot:
            self._load(snapshot)

    # -- persistence -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self._nodes.values()],
            "workflows": [workflow.to_dict() for workflow in self._workflows.values()],
        }

    def _load(self, snapshot: dict[str, Any]) -> None:
        for item in snapshot.get("nodes", []) or []:
            try:
                node = ApplicationKnowledgeNode(
                    application=str(item.get("application", "")),
                    area=str(item.get("area", "")),
                    element=str(item.get("element", "")),
                    selector=str(item.get("selector", "")),
                    children=tuple(str(c) for c in item.get("children", ())),
                    notes=str(item.get("notes", "")),
                )
                self._nodes[self._key(node.application, node.area, node.element)] = node
            except Exception:
                continue
        for item in snapshot.get("workflows", []) or []:
            try:
                workflow = WorkflowKnowledge.from_dict(dict(item))
                self._workflows[self._wf_key(workflow.application, workflow.task)] = workflow
            except Exception:
                continue

    # -- structure nodes ---------------------------------------------------------

    def add_node(self, node: ApplicationKnowledgeNode) -> ApplicationKnowledgeNode:
        self._nodes[self._key(node.application, node.area, node.element)] = node
        return node

    def children_of(self, application: str, area: str) -> tuple[str, ...]:
        return tuple(
            node.element
            for node in self._nodes.values()
            if node.application == application and node.area == area
        )

    # -- workflows ----------------------------------------------------------------

    def record_workflow(
        self,
        application: str,
        task: str,
        steps: tuple[str, ...],
        *,
        confidence: float = 0.6,
        source: str = "observed",
    ) -> WorkflowKnowledge:
        key = self._wf_key(application, task)
        existing = self._workflows.get(key)
        if existing:
            # Reinforcement: verified again, freshness reset, confidence nudged up.
            updated = WorkflowKnowledge(
                application=existing.application,
                task=existing.task,
                steps=steps or existing.steps,
                confidence=min(0.99, max(existing.confidence, confidence) + 0.05),
                source=source,
                successes=existing.successes + 1,
                failures=existing.failures,
                alternatives=existing.alternatives,
            )
            self._workflows[key] = updated
            return updated
        workflow = WorkflowKnowledge(
            application=application, task=task, steps=steps, confidence=confidence, source=source,
        )
        self._workflows[key] = workflow
        return workflow

    def record_failure(self, application: str, task: str) -> None:
        workflow = self._workflows.get(self._wf_key(application, task))
        if workflow:
            workflow.failures += 1
            workflow.last_verified = datetime.now(UTC).isoformat()

    def record_alternative_path(self, application: str, failed_target: str, alternative: str, *, confidence: float = 0.6) -> None:
        """Attach a working alternative to workflows that used the dead path.
        Matching is semantic: any workflow step whose text overlaps the failed
        target gets the alternative (exact tuple membership would miss
        reworded steps)."""
        failed_words = set(failed_target.lower().replace("text=", "").split())
        for workflow in self._workflows.values():
            if workflow.application != application:
                continue
            used_dead_path = any(
                failed_words & set(step.lower().split())
                for step in workflow.steps
            )
            if used_dead_path:
                if alternative not in workflow.alternatives:
                    workflow.alternatives = (*workflow.alternatives, alternative)
                workflow.confidence = max(0.3, workflow.confidence - 0.1)  # the old path degrades

    def best_path(self, application: str, task_or_target: str) -> str | None:
        """The most confident step sequence start (or alternative) for a task."""
        needle = task_or_target.strip().lower()
        candidates = [
            workflow
            for workflow in self._workflows.values()
            if workflow.application == application
            and (needle in workflow.task.lower() or any(needle in step.lower() for step in workflow.steps))
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda w: w.effective_confidence(), reverse=True)
        best = candidates[0]
        if best.effective_confidence() < 0.3:
            return None  # too stale / too failed to propose
        return best.steps[0] if best.steps else (best.alternatives[0] if best.alternatives else None)

    def confidence_for(self, application: str, step: str) -> float:
        for workflow in self._workflows.values():
            if workflow.application == application and step in workflow.steps:
                return workflow.effective_confidence()
        return 0.4

    def workflows_for(self, application: str) -> tuple[WorkflowKnowledge, ...]:
        return tuple(w for w in self._workflows.values() if w.application == application)

    @staticmethod
    def _key(application: str, area: str, element: str) -> str:
        return f"{application.lower()}|{area.lower()}|{element.lower()}"

    @staticmethod
    def _wf_key(application: str, task: str) -> str:
        return f"{application.lower()}|{task.lower()}"
