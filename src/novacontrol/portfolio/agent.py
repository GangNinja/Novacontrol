"""Advisory portfolio-manager agent over the existing AgentRegistry.

The agent can summarise a portfolio the service actor is allowed to see. It has
no budget, lifecycle, payment or publication authority and cannot promote a
recommendation into a decision.
"""

from __future__ import annotations

from novacontrol.agents.models import AgentResponse, AgentRole, AgentTask, AgentTaskStatus
from novacontrol.portfolio.errors import PortfolioError
from novacontrol.portfolio.service_facade import PortfolioService


class PortfolioManagerAgent:
    name = "portfolio-manager-agent"
    role = AgentRole.BUSINESS
    description = (
        "Synthesises portfolio performance, venture rankings, risks, allocation "
        "recommendations and scenario evidence. Advisory only; no funds move and no "
        "decision is executed."
    )

    def __init__(self, service: PortfolioService) -> None:
        self.service = service

    async def handle_task(self, task: AgentTask) -> AgentResponse:
        portfolio_id = str(task.metadata.get("portfolio_id", "")).strip()
        if not portfolio_id:
            listing = self.service.list_portfolios()
            content = f"{listing['total']} portfolio(s) are visible."
            if listing["portfolios"]:
                content += " Latest: " + ", ".join(
                    f"{row['name']} ({row['ventures']} ventures)"
                    for row in listing["portfolios"][:5]
                )
            content += " Provide portfolio_id metadata to analyse one portfolio."
            return AgentResponse(
                task_id=task.id,
                agent_name=self.name,
                role=self.role,
                status=AgentTaskStatus.COMPLETED,
                content=content,
                metadata={
                    "advisory_only": True,
                    "portfolios": listing["total"],
                    "external_action_executed": False,
                    "authority": "read-only portfolio summary",
                },
            )
        try:
            dashboard = self.service.dashboard(portfolio_id)
        except PortfolioError as exc:
            return AgentResponse(
                task_id=task.id,
                agent_name=self.name,
                role=self.role,
                status=AgentTaskStatus.FAILED,
                content=str(exc),
                metadata={
                    "advisory_only": True,
                    "error": str(exc),
                    "external_action_executed": False,
                },
            )
        overview = dashboard["overview"]
        totals = overview["revenue"]
        observations = [
            f"{dashboard['portfolio']['name']} has {overview['total_ventures']} venture(s), "
            f"{overview['active_ventures']} active and {overview['paused_ventures']} paused.",
            (
                f"Converted revenue is {totals.get('value')} {dashboard['base_currency']} "
                "from posted actuals."
                if totals.get("available")
                else f"Consolidated revenue is unavailable: {totals.get('reason')}."
            ),
            f"{dashboard['alerts']['open']} open alert(s), "
            f"{len(dashboard['risks']['high_or_critical'])} high/critical risk(s).",
        ]
        ranking = dashboard["strategic"].get("ranking", {})
        ranking_rows = ranking.get("ranking", []) if isinstance(ranking, dict) else []
        ranked = [row for row in ranking_rows if row.get("rank") is not None]
        if ranked:
            observations.append(
                "Ranked ventures: "
                + ", ".join(
                    f"{row['venture_name']} #{row['rank']} (coverage {row['coverage']:.0%})"
                    for row in ranked[:3]
                )
                + "."
            )
        observations.append(
            "No budget, transfer, purchase, contract, publication or decision was executed; "
            "consequential actions need human authorization."
        )
        return AgentResponse(
            task_id=task.id,
            agent_name=self.name,
            role=self.role,
            status=AgentTaskStatus.COMPLETED,
            content=" ".join(observations),
            metadata={
                "advisory_only": True,
                "portfolio_id": portfolio_id,
                "portfolio_name": dashboard["portfolio"].get("name"),
                "external_action_executed": False,
                "authority": "read-only portfolio analysis; cannot allocate funds or change lifecycle",
                "forecast_is_actual": False,
                "scenario_is_actual": False,
                "open_alerts": dashboard["alerts"]["open"],
                "visible_ventures": overview["visible_ventures"],
            },
        )


def build_portfolio_agent(service: PortfolioService) -> PortfolioManagerAgent:
    return PortfolioManagerAgent(service)


__all__ = ["PortfolioManagerAgent", "build_portfolio_agent"]
