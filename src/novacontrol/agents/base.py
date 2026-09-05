"""Base specialized agents."""

from __future__ import annotations

from collections.abc import Mapping

from novacontrol.agents.models import AgentResponse, AgentRole, AgentTask, AgentTaskStatus


class SpecializedAgent:
    """Deterministic specialized agent used until LLM-backed agents are configured."""

    def __init__(self, name: str, role: AgentRole, description: str) -> None:
        self._name = name
        self.role = role
        self.description = description

    @property
    def name(self) -> str:
        return self._name

    async def handle_task(self, task: AgentTask) -> AgentResponse:
        return AgentResponse(
            task_id=task.id,
            agent_name=self.name,
            role=self.role,
            status=AgentTaskStatus.COMPLETED,
            content=_response_for(self.role, task.goal, task.metadata),
        )


def build_default_agents() -> tuple[SpecializedAgent, ...]:
    return (
        SpecializedAgent("research-agent", AgentRole.RESEARCH, "Searches and summarizes."),
        SpecializedAgent("coding-agent", AgentRole.CODING, "Generates and edits code."),
        SpecializedAgent("debug-agent", AgentRole.DEBUG, "Diagnoses failures."),
        SpecializedAgent("planning-agent", AgentRole.PLANNING, "Breaks goals into plans."),
        SpecializedAgent("documentation-agent", AgentRole.DOCUMENTATION, "Writes documentation."),
        SpecializedAgent("design-agent", AgentRole.DESIGN, "Creates design artifacts."),
        SpecializedAgent("testing-agent", AgentRole.TESTING, "Creates and runs tests."),
        SpecializedAgent("review-agent", AgentRole.REVIEW, "Reviews code and plans."),
        SpecializedAgent("browser-agent", AgentRole.BROWSER, "Controls browser workflows."),
        SpecializedAgent(
            "desktop-automation-agent",
            AgentRole.DESKTOP_AUTOMATION,
            "Coordinates approved desktop workflows.",
        ),
        SpecializedAgent("project-manager-agent", AgentRole.PROJECT_MANAGER, "Tracks projects."),
    )


def _response_for(role: AgentRole, goal: str, metadata: Mapping[str, object]) -> str:
    prefix = {
        AgentRole.RESEARCH: "Research brief prepared",
        AgentRole.CODING: "Implementation path prepared",
        AgentRole.DEBUG: "Debugging path prepared",
        AgentRole.PLANNING: "Plan prepared",
        AgentRole.DOCUMENTATION: "Documentation outline prepared",
        AgentRole.DESIGN: "Design direction prepared",
        AgentRole.TESTING: "Test strategy prepared",
        AgentRole.REVIEW: "Review checklist prepared",
        AgentRole.BROWSER: "Browser workflow prepared",
        AgentRole.DESKTOP_AUTOMATION: "Desktop automation workflow prepared",
        AgentRole.PROJECT_MANAGER: "Project tracking update prepared",
        AgentRole.COORDINATOR: "Delegation prepared",
    }[role]
    detail = f" for: {goal}"
    if metadata:
        detail += f" with metadata keys: {', '.join(sorted(metadata))}"
    return prefix + detail
