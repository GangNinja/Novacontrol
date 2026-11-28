"""Business-validation specialist over the existing AgentRegistry.

This is one advisory agent, not eight parallel orchestration frameworks. It
reads the service's recorded evidence and reports what the evidence supports;
it does not write a verdict, approve an experiment or execute an external action.
The eight phase responsibilities remain deterministic modules in the service.
"""

from __future__ import annotations

from typing import Any

from novacontrol.agents.models import AgentResponse, AgentRole, AgentTask, AgentTaskStatus
from novacontrol.business.service import BusinessOpportunityService, OpportunityNotFound


class BusinessValidationAgent:
    """Advisory read-only bridge between agent routing and Phase 35."""

    name = "business-validation-agent"
    role = AgentRole.BUSINESS
    description = (
        "Summarises recorded business evidence, evidence gaps, scores and verdicts. "
        "Advisory only; it cannot validate an opportunity or authorize an external action."
    )

    def __init__(self, service: BusinessOpportunityService) -> None:
        self.service = service

    async def handle_task(self, task: AgentTask) -> AgentResponse:
        metadata = dict(task.metadata)
        opportunity_id = str(metadata.get("opportunity_id", "")).strip()
        ids = metadata.get("opportunity_ids")
        if isinstance(ids, (list, tuple)) and len(ids) >= 2:
            try:
                compared = self.service.compare(tuple(str(item) for item in ids))
            except Exception as exc:  # noqa: BLE001 - an agent reports refusal, it does not raise
                return self._failed(task, str(exc))
            return AgentResponse(
                task_id=task.id,
                agent_name=self.name,
                role=self.role,
                status=AgentTaskStatus.COMPLETED,
                content=(
                    f"Compared {len(compared['opportunities'])} opportunities. "
                    "Each row reports its recorded evidence; missing evidence is not ranked away."
                ),
                metadata={
                    "advisory_only": True,
                    "comparison": compared,
                    "external_action_executed": False,
                },
            )
        if not opportunity_id:
            listing = self.service.list_opportunities(limit=20)
            titles = ", ".join(row["title"] for row in listing["opportunities"][:5])
            return AgentResponse(
                task_id=task.id,
                agent_name=self.name,
                role=self.role,
                status=AgentTaskStatus.COMPLETED,
                content=(
                    f"{listing['total']} opportunities are stored. "
                    + (f"Latest: {titles}. " if titles else "")
                    + "Provide opportunity_id metadata to inspect one record."
                ),
                metadata={
                    "advisory_only": True,
                    "complete": False,
                    "stage_counts": self.service.status()["by_stage"],
                    "external_action_executed": False,
                },
            )
        try:
            detail = self.service.detail(opportunity_id)
        except OpportunityNotFound as exc:
            return self._failed(task, str(exc))
        verdict = detail.get("verdict") or "no verdict yet"
        score = detail.get("score") or {}
        gaps = list((detail.get("report") or {}).get("evidence_gaps") or detail.get("uncertainty") or [])
        content = (
            f"{detail['title']}: {verdict}. "
            f"Stage {detail['stage']}; score {score.get('weighted_score', 'unavailable')}; "
            f"coverage {score.get('evidence_coverage', 'unavailable')}; "
            f"{len(detail.get('evidence', []))} evidence record(s). "
            f"Gaps: {'; '.join(gaps[:4]) if gaps else 'none recorded'}. "
            "This is an advisory summary of recorded evidence, not a new validation decision."
        )
        return AgentResponse(
            task_id=task.id,
            agent_name=self.name,
            role=self.role,
            status=AgentTaskStatus.COMPLETED,
            content=content,
            metadata={
                "advisory_only": True,
                "opportunity_id": detail["id"],
                "stage": detail["stage"],
                "verdict": detail.get("verdict", ""),
                "score": score.get("weighted_score"),
                "evidence_gaps": gaps[:12],
                "external_action_executed": False,
                "authority": "read-only evidence summary; cannot approve or execute external actions",
            },
        )

    def _failed(self, task: AgentTask, reason: str) -> AgentResponse:
        return AgentResponse(
            task_id=task.id,
            agent_name=self.name,
            role=self.role,
            status=AgentTaskStatus.FAILED,
            content=f"Business validation could not read the requested opportunity: {reason}",
            metadata={"advisory_only": True, "error": reason},
        )


def build_business_agent(service: BusinessOpportunityService) -> BusinessValidationAgent:
    return BusinessValidationAgent(service)


__all__ = ["BusinessValidationAgent", "build_business_agent"]
