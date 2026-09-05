"""Coordinator agent and task routing."""

from __future__ import annotations

from novacontrol.agents.models import AgentResponse, AgentRole, AgentTask, AgentTaskStatus
from novacontrol.agents.registry import AgentRegistry


class CoordinatorAgent:
    """Routes tasks to the specialized agent best suited to the goal."""

    role = AgentRole.COORDINATOR

    @property
    def name(self) -> str:
        return "coordinator-agent"

    async def handle_task(self, task: AgentTask) -> AgentResponse:
        return AgentResponse(
            task_id=task.id,
            agent_name=self.name,
            role=self.role,
            status=AgentTaskStatus.COMPLETED,
            content=f"Coordinator received task: {task.goal}",
        )

    async def delegate(self, task: AgentTask, registry: AgentRegistry) -> AgentResponse:
        role = task.role or self.route_task(task.goal)
        agent = registry.first_by_role(role)
        if agent is None:
            return AgentResponse(
                task_id=task.id,
                agent_name=self.name,
                role=self.role,
                status=AgentTaskStatus.FAILED,
                content=f"No agent registered for role {role.value}.",
            )
        routed_task = task.with_role(role)
        return await agent.handle_task(routed_task)

    def route_task(self, goal: str) -> AgentRole:
        text = goal.lower()
        keyword_routes = (
            (AgentRole.DEBUG, ("debug", "bug", "failure", "traceback", "exception")),
            (AgentRole.TESTING, ("test", "pytest", "coverage", "qa")),
            (AgentRole.REVIEW, ("review", "audit", "risk")),
            (AgentRole.CODING, ("code", "implement", "refactor", "build", "fix")),
            (AgentRole.RESEARCH, ("research", "search", "summarize", "compare", "citation")),
            (AgentRole.DOCUMENTATION, ("document", "docs", "readme", "guide")),
            (AgentRole.DESIGN, ("design", "mockup", "wireframe", "palette", "ux", "ui")),
            (AgentRole.BROWSER, ("browser", "web", "download", "form")),
            (AgentRole.DESKTOP_AUTOMATION, ("desktop", "file", "application", "script")),
            (AgentRole.PROJECT_MANAGER, ("milestone", "roadmap", "project", "task")),
        )
        for role, keywords in keyword_routes:
            if any(keyword in text for keyword in keywords):
                return role
        return AgentRole.PLANNING
