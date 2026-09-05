"""General automation workflow manager."""

from __future__ import annotations

from dataclasses import replace

from typing import Any

from novacontrol.automation.models import AutomationStep, AutomationWorkflow, AutomationWorkflowStatus


class AutomationManager:
    """Stores reusable automation workflows without executing sensitive actions directly."""

    def __init__(self) -> None:
        self._workflows: dict[str, AutomationWorkflow] = {}

    def create_workflow(self, name: str, steps: tuple[AutomationStep, ...]) -> AutomationWorkflow:
        workflow = AutomationWorkflow(name=name, steps=steps)
        self._workflows[workflow.id] = workflow
        return workflow

    def get_workflow(self, workflow_id: str) -> AutomationWorkflow:
        try:
            return self._workflows[workflow_id]
        except KeyError as exc:
            raise KeyError(f"Automation workflow not found: {workflow_id}") from exc

    def list_workflows(self) -> tuple[AutomationWorkflow, ...]:
        return tuple(self._workflows[key] for key in sorted(self._workflows))

    def mark_status(
        self,
        workflow_id: str,
        status: AutomationWorkflowStatus,
    ) -> AutomationWorkflow:
        workflow = self.get_workflow(workflow_id)
        updated = replace(workflow, status=status)
        self._workflows[workflow_id] = updated
        return updated

    def to_dict(self) -> dict[str, Any]:
        return {"workflows": [workflow.to_dict() for workflow in self._workflows.values()]}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AutomationManager":
        manager = cls()
        for item in payload.get("workflows", []):
            if isinstance(item, dict):
                workflow = AutomationWorkflow.from_dict(item)
                manager._workflows[workflow.id] = workflow
        return manager
