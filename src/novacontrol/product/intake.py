"""Phase 35 opportunity intake for Phase 36.

Eligibility is read from the stored Phase 35 validation report, never from an
agent recommendation. A caller cannot ask this module to "accept anyway": it can
only present a human decision for the one state that requires one
(``CONDITIONALLY_VALIDATED``) and the report's own conditions are retained.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from novacontrol.product.errors import OpportunityNotEligible
from novacontrol.product.models import OpportunityValidationState


@dataclass(frozen=True, slots=True)
class IntakeDecision:
    opportunity_id: str
    opportunity_title: str
    state: OpportunityValidationState
    report: dict[str, Any]
    snapshot: dict[str, Any]
    conditions: tuple[str, ...] = ()
    condition_approval: dict[str, Any] = field(default_factory=dict)

    @property
    def conditional(self) -> bool:
        return self.state is OpportunityValidationState.CONDITIONALLY_VALIDATED

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "opportunity_title": self.opportunity_title,
            "validation_state": self.state.value,
            "conditions": list(self.conditions),
            "condition_approval": dict(self.condition_approval),
            "conditional": self.conditional,
        }


def _normalise_state(value: str) -> OpportunityValidationState:
    raw = str(value or "").strip().upper().replace(" ", "_").replace("-", "_")
    aliases = {
        "VALIDATED": "VALIDATED",
        "VALIDATION_COMPLETE": "VALIDATED",
        "CONDITIONALLY_VALIDATED": "CONDITIONALLY_VALIDATED",
        "CONDITIONAL": "CONDITIONALLY_VALIDATED",
        "INSUFFICIENT_EVIDENCE": "INSUFFICIENT_EVIDENCE",
        "NOT_VIABLE": "NOT_VIABLE",
        "REQUIRES_FURTHER_RESEARCH": "REQUIRES_FURTHER_RESEARCH",
    }
    mapped = aliases.get(raw, raw)
    try:
        return OpportunityValidationState(mapped)
    except ValueError:
        return OpportunityValidationState.UNKNOWN


def validation_state(detail: Mapping[str, Any]) -> OpportunityValidationState:
    """Read the report verdict first, then the stored lifecycle stage."""
    report = detail.get("report")
    if isinstance(report, Mapping):
        state = _normalise_state(str(report.get("verdict", "")))
        if state is not OpportunityValidationState.UNKNOWN:
            return state
    state = _normalise_state(str(detail.get("stage", "")))
    if state is not OpportunityValidationState.UNKNOWN:
        return state
    return _normalise_state(str(detail.get("verdict", "")))


def _unique(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    rows: list[str] = []
    for value in values:
        text = " ".join(str(value).split()).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            rows.append(text)
    return tuple(rows)


def unresolved_conditions(detail: Mapping[str, Any]) -> tuple[str, ...]:
    """Collect the report's own gaps/qualifiers without inventing any."""
    report = detail.get("report")
    source = report if isinstance(report, Mapping) else {}
    conditions: list[str] = []
    for key in ("evidence_gaps", "limitations", "next_steps"):
        raw = source.get(key)
        if isinstance(raw, (list, tuple)):
            conditions.extend(str(item) for item in raw)
    score = detail.get("score")
    if isinstance(score, Mapping):
        raw = score.get("missing") or score.get("disqualifiers")
        if isinstance(raw, (list, tuple)):
            conditions.extend(str(item) for item in raw)
    raw = detail.get("uncertainty")
    if isinstance(raw, (list, tuple)):
        conditions.extend(str(item) for item in raw)
    return _unique(conditions)


def _snapshot(detail: Mapping[str, Any]) -> dict[str, Any]:
    """A durable, document-sized trace of the originating opportunity."""
    return {
        "id": str(detail.get("id", "")),
        "title": str(detail.get("title", "")),
        "description": str(detail.get("description", "")),
        "customer_problem": str(detail.get("customer_problem", "")),
        "proposed_solution": str(detail.get("proposed_solution", "")),
        "target_audience": str(detail.get("target_audience", "")),
        "industry": str(detail.get("industry", "")),
        "category": str(detail.get("category", "")),
        "stage": str(detail.get("stage", "")),
        "verdict": str((detail.get("report") or {}).get("verdict", ""))
        if isinstance(detail.get("report"), Mapping)
        else str(detail.get("verdict", "")),
        "score": dict(detail.get("score") or {})
        if isinstance(detail.get("score"), Mapping)
        else {},
        "source_refs": list(detail.get("source_refs") or []),
        "evidence_count": len(detail.get("evidence") or []),
        "tags": list(detail.get("tags") or []),
    }


def accept_opportunity(
    detail: Mapping[str, Any],
    *,
    approved: bool = False,
    actor: str = "",
    note: str = "",
    conditions_acknowledged: tuple[str, ...] = (),
) -> IntakeDecision:
    """Decide whether one Phase 35 record may create a product project."""
    opportunity_id = str(detail.get("id", "")).strip()
    if not opportunity_id:
        raise OpportunityNotEligible("The opportunity record has no id.")
    state = validation_state(detail)
    report = dict(detail.get("report") or {}) if isinstance(detail.get("report"), Mapping) else {}
    conditions = unresolved_conditions(detail)
    title = str(detail.get("title", "")).strip() or opportunity_id

    if state is OpportunityValidationState.VALIDATED:
        if not report:
            raise OpportunityNotEligible(
                "The opportunity is marked VALIDATED but has no stored validation report.",
                state=state.value,
                opportunity_id=opportunity_id,
            )
        return IntakeDecision(
            opportunity_id=opportunity_id,
            opportunity_title=title,
            state=state,
            report=report,
            snapshot=_snapshot(detail),
            conditions=conditions,
        )

    if state is OpportunityValidationState.CONDITIONALLY_VALIDATED:
        if not report:
            raise OpportunityNotEligible(
                "The conditional validation has no stored report to qualify it.",
                state=state.value,
                opportunity_id=opportunity_id,
                conditions=conditions,
            )
        if not conditions:
            raise OpportunityNotEligible(
                "A conditionally validated opportunity must retain at least one "
                "report condition or evidence gap; none was present.",
                state=state.value,
                opportunity_id=opportunity_id,
            )
        if not approved:
            raise OpportunityNotEligible(
                "Conditionally validated opportunities require an explicit operator "
                "approval before product development.",
                state=state.value,
                opportunity_id=opportunity_id,
                conditions=conditions,
            )
        if not actor.strip():
            raise OpportunityNotEligible(
                "A conditional acceptance needs a named human approver.",
                state=state.value,
                opportunity_id=opportunity_id,
                conditions=conditions,
            )
        if not note.strip():
            raise OpportunityNotEligible(
                "A conditional acceptance needs an approval note explaining how the "
                "unresolved conditions will be handled.",
                state=state.value,
                opportunity_id=opportunity_id,
                conditions=conditions,
            )
        approved_conditions = _unique([str(item) for item in conditions_acknowledged])
        if approved_conditions and not set(conditions).issubset(set(approved_conditions)):
            missing = sorted(set(conditions) - set(approved_conditions))
            raise OpportunityNotEligible(
                "The acceptance does not acknowledge every recorded condition: "
                + "; ".join(missing[:4]),
                state=state.value,
                opportunity_id=opportunity_id,
                conditions=conditions,
            )
        return IntakeDecision(
            opportunity_id=opportunity_id,
            opportunity_title=title,
            state=state,
            report=report,
            snapshot=_snapshot(detail),
            conditions=conditions,
            condition_approval={
                "approved": True,
                "actor": actor,
                "note": note,
                "conditions_acknowledged": list(approved_conditions),
                "at": "",
            },
        )

    labels = {
        OpportunityValidationState.INSUFFICIENT_EVIDENCE: "insufficient evidence",
        OpportunityValidationState.NOT_VIABLE: "not viable",
        OpportunityValidationState.REQUIRES_FURTHER_RESEARCH: "requires further research",
        OpportunityValidationState.UNKNOWN: "no recognised validation verdict",
    }
    raise OpportunityNotEligible(
        f"The opportunity cannot enter product development: {labels[state]}.",
        state=state.value,
        opportunity_id=opportunity_id,
        conditions=conditions,
    )


__all__ = [
    "IntakeDecision",
    "accept_opportunity",
    "unresolved_conditions",
    "validation_state",
]
