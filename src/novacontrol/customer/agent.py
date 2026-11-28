"""Advisory customer-growth agent over the existing AgentRegistry.

The agent reads one workspace and reports recorded acquisition, pipeline and
support state. It cannot send a message, move a deal, approve a discount, issue a
refund or alter customer data.
"""

from __future__ import annotations

from typing import Any

from novacontrol.agents.models import AgentResponse, AgentRole, AgentTask, AgentTaskStatus
from novacontrol.customer.errors import CustomerError
from novacontrol.customer.service import CustomerService


class CustomerGrowthAgent:
    name = "customer-growth-agent"
    role = AgentRole.PROJECT_MANAGER
    description = (
        "Summarises customer segments, leads, sales pipeline, support tickets and "
        "retention indicators. Advisory only; communication, discounts, refunds and "
        "customer changes remain human actions."
    )

    def __init__(self, service: CustomerService) -> None:
        self.service = service

    async def handle_task(self, task: AgentTask) -> AgentResponse:
        product_id = str(task.metadata.get("product_id", "")).strip()
        if not product_id:
            listing = self.service.list_workspaces(limit=20)
            rows = listing["workspaces"]
            content = f"{listing['total']} customer workspace(s) are stored."
            if rows:
                content += " Latest: " + ", ".join(row["product_name"] for row in rows[:5]) + "."
            content += " Provide product_id metadata to inspect one workspace."
            return AgentResponse(
                task_id=task.id,
                agent_name=self.name,
                role=self.role,
                status=AgentTaskStatus.COMPLETED,
                content=content,
                metadata={"advisory_only": True, "workspaces": listing["total"], "external_action_executed": False},
            )
        try:
            detail = self.service.detail(product_id)
        except CustomerError as exc:
            return AgentResponse(
                task_id=task.id,
                agent_name=self.name,
                role=self.role,
                status=AgentTaskStatus.FAILED,
                content=str(exc),
                metadata={"advisory_only": True, "error": str(exc)},
            )
        summary = detail["summary"]
        analytics = detail["analytics"]
        content = (
            f"{summary['product_name']} ({product_id}): {summary['leads']} lead(s), "
            f"{summary['customers']} customer(s), {summary['open_deals']} open deal(s), "
            f"{summary['open_tickets']} open ticket(s). Pipeline value is an estimate: "
            f"{analytics['sales']['pipeline_value'].get('value')} "
            f"{analytics['sales']['pipeline_value'].get('currency', '')}. "
            "This is a read-only summary of recorded state, not revenue, intent or a commitment."
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
                "leads": summary["leads"],
                "customers": summary["customers"],
                "open_tickets": summary["open_tickets"],
                "external_action_executed": False,
                "authority": "read-only customer summary; cannot communicate, discount, refund or mutate",
            },
        )


def build_customer_agent(service: CustomerService) -> CustomerGrowthAgent:
    return CustomerGrowthAgent(service)


__all__ = ["CustomerGrowthAgent", "build_customer_agent"]
