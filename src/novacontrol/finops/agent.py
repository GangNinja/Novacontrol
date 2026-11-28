"""Advisory financial-intelligence agent over the existing AgentRegistry.

The agent reads posted actuals, estimates, forecasts and alerts and states what
the evidence supports. It cannot transfer money, make a purchase, execute a
payment, change banking details, approve an invoice, commit spending or alter a
finalized journal entry.
"""

from __future__ import annotations

from typing import Any

from novacontrol.agents.models import AgentResponse, AgentRole, AgentTask, AgentTaskStatus
from novacontrol.finops.errors import FinOpsError
from novacontrol.finops.service import FinOpsService


class FinancialIntelligenceAgent:
    name = "financial-intelligence-agent"
    role = AgentRole.BUSINESS
    description = (
        "Analyses recorded venture revenue, expenses, cash, working capital, budgets, "
        "forecasts and operational performance. Advisory only; it cannot pay, purchase, "
        "approve invoices, change banking details or mutate the ledger."
    )

    def __init__(self, service: FinOpsService) -> None:
        self.service = service

    async def handle_task(self, task: AgentTask) -> AgentResponse:
        venture_id = str(task.metadata.get("venture_id", "")).strip()
        if not venture_id:
            listing = self.service.list_ventures(limit=20)
            rows = listing["ventures"]
            content = f"{listing['total']} venture(s) are recorded."
            if rows:
                content += " Latest: " + ", ".join(
                    f"{row['name']} ({row['reporting_currency']} {row['recognized_revenue']:,.2f} recognized)"
                    for row in rows[:5]
                ) + "."
            content += " Provide venture_id metadata to analyse one venture."
            return AgentResponse(
                task_id=task.id,
                agent_name=self.name,
                role=self.role,
                status=AgentTaskStatus.COMPLETED,
                content=content,
                metadata={
                    "advisory_only": True,
                    "ventures": listing["total"],
                    "external_action_executed": False,
                    "authority": "read-only venture summary",
                },
            )
        try:
            dashboard = self.service.dashboard(venture_id)
        except FinOpsError as exc:
            return AgentResponse(
                task_id=task.id,
                agent_name=self.name,
                role=self.role,
                status=AgentTaskStatus.FAILED,
                content=str(exc),
                metadata={"advisory_only": True, "error": str(exc), "external_action_executed": False},
            )
        venture = dashboard["venture"]
        revenue = dashboard["revenue"]
        expenses = dashboard["expenses"]
        profit = dashboard["profitability"]
        capital = dashboard["working_capital"]
        alerts = dashboard["open_alerts"]
        observations: list[str] = []
        evidence: list[dict[str, Any]] = []
        if revenue.get("available"):
            observations.append(
                f"Recognized revenue is {revenue['currency']} {revenue['recognized_revenue']:,.2f}; "
                f"collected cash is {revenue['currency']} {revenue['collected_cash']:,.2f}."
            )
            evidence.append({"metric": "recognized_revenue", "value": revenue["recognized_revenue"], "source": "posted ledger"})
            if revenue.get("outstanding_invoices", 0) > 0:
                observations.append(
                    f"Outstanding invoices are {revenue['currency']} {revenue['outstanding_invoices']:,.2f}; "
                    f"overdue receivables are {revenue['currency']} {revenue['overdue_receivables']:,.2f}."
                )
        else:
            observations.append("No revenue record exists, so no revenue conclusion is drawn.")
        if expenses.get("actual", 0) > 0:
            observations.append(
                f"Approved actual expenses are {expenses['currency']} {expenses['actual']:,.2f}; "
                f"unapproved actuals are {expenses['currency']} {expenses['unapproved_actual']:,.2f}."
            )
        if profit.get("available"):
            net_profit = profit.get("net_profit")
            observations.append(
                f"Net profit from posted entries is {profit['currency']} {float(net_profit or 0):,.2f} "
                "for the current month to date; this is actual ledger arithmetic, not a forecast."
            )
            evidence.append({"metric": "net_profit", "value": net_profit, "source": "income statement"})
        runway = capital.get("cash_runway_months", {})
        if runway.get("available"):
            observations.append(
                f"Cash runway is estimated at {runway['value']:.1f} month(s) if the observed burn continues."
            )
        else:
            observations.append(f"Cash runway is not calculable: {runway.get('reason')}")
        if alerts:
            observations.append(
                "Open alerts: " + "; ".join(f"{row['metric']} ({row['severity']})" for row in alerts[:5]) + "."
            )
            evidence.extend(
                {"metric": row["metric"], "observed": row["observed_value"], "threshold": row["threshold"], "alert_id": row.get("id")}
                for row in alerts[:5]
            )
        observations.append(
            "No payment, purchase, bank change, invoice approval or ledger mutation was performed; any consequential action needs human authorization."
        )
        return AgentResponse(
            task_id=task.id,
            agent_name=self.name,
            role=self.role,
            status=AgentTaskStatus.COMPLETED,
            content=" ".join(observations),
            metadata={
                "advisory_only": True,
                "venture_id": venture_id,
                "venture_name": venture.get("name"),
                "evidence": evidence,
                "open_alert_count": len(alerts),
                "external_action_executed": False,
                "authority": "read-only analysis; cannot pay, purchase, approve invoices, change banking details or mutate finalized records",
                "forecast_is_actual": False,
            },
        )


def build_finance_agent(service: FinOpsService) -> FinancialIntelligenceAgent:
    return FinancialIntelligenceAgent(service)


__all__ = ["FinancialIntelligenceAgent", "build_finance_agent"]
