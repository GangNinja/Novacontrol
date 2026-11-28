"""Advisory product-pipeline agent over the existing AgentRegistry.

This agent reads the one Phase 36 service and summarises its recorded state. It
cannot approve a requirement, architecture, release or launch, and it cannot
execute an external action. Development execution still goes through the
existing agent runtime and task records.
"""

from __future__ import annotations

from typing import Any

from novacontrol.agents.models import AgentResponse, AgentRole, AgentTask, AgentTaskStatus
from novacontrol.product.errors import ProductNotFound
from novacontrol.product.service import ProductService


class ProductPipelineAgent:
    name = "product-pipeline-agent"
    role = AgentRole.PROJECT_MANAGER
    description = (
        "Summarises product projects, requirements, development progress, quality "
        "gates and launch readiness. Advisory only; all approvals remain human."
    )

    def __init__(self, service: ProductService) -> None:
        self.service = service

    async def handle_task(self, task: AgentTask) -> AgentResponse:
        product_id = str(task.metadata.get("product_id", "")).strip()
        if not product_id:
            listing = self.service.list_projects(limit=20)
            rows = listing["products"]
            content = f"{listing['total']} product project(s) are stored."
            if rows:
                content += " Latest: " + ", ".join(row["name"] for row in rows[:5]) + "."
            content += " Provide product_id metadata to inspect one project."
            return AgentResponse(
                task_id=task.id,
                agent_name=self.name,
                role=self.role,
                status=AgentTaskStatus.COMPLETED,
                content=content,
                metadata={
                    "advisory_only": True,
                    "products": listing["total"],
                    "external_action_executed": False,
                },
            )
        try:
            detail = self.service.detail(product_id)
        except ProductNotFound as exc:
            return AgentResponse(
                task_id=task.id,
                agent_name=self.name,
                role=self.role,
                status=AgentTaskStatus.FAILED,
                content=str(exc),
                metadata={"advisory_only": True, "error": str(exc)},
            )
        summary = detail["summary"]
        quality = detail["quality"]
        readiness = detail.get("readiness") or {}
        content = (
            f"{summary['name']} ({summary['id']}): state {summary['state']}; "
            f"opportunity {summary['validation_state']}; "
            f"{summary['tasks_completed']}/{summary['tasks']} tasks complete; "
            f"{len(quality['failing_required'])} required gate(s) failing; "
            f"readiness {readiness.get('status', 'not evaluated') or 'not evaluated'}"
            f" (score {readiness.get('score', 'not evaluated')}). "
            "This is a read-only summary of recorded state, not an approval or a launch."
        )
        return AgentResponse(
            task_id=task.id,
            agent_name=self.name,
            role=self.role,
            status=AgentTaskStatus.COMPLETED,
            content=content,
            metadata={
                "advisory_only": True,
                "product_id": product_id,
                "state": summary["state"],
                "failing_gates": quality["failing_required"],
                "readiness": readiness.get("status", ""),
                "external_action_executed": False,
                "authority": "read-only project summary; cannot approve or execute",
            },
        )


def build_product_agent(service: ProductService) -> ProductPipelineAgent:
    return ProductPipelineAgent(service)


__all__ = ["ProductPipelineAgent", "build_product_agent"]
