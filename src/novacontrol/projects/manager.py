"""Project management."""

from __future__ import annotations

from dataclasses import replace

from typing import Any

from novacontrol.projects.models import Milestone, Project, ProjectStatus, ProjectTask, TaskStatus


class ProjectManager:
    def __init__(self) -> None:
        self._projects: dict[str, Project] = {}

    def create_project(self, name: str, description: str = "") -> Project:
        project = Project(name=name, description=description)
        self._projects[project.id] = project
        return project

    def get_project(self, project_id: str) -> Project:
        try:
            return self._projects[project_id]
        except KeyError as exc:
            raise KeyError(f"Project not found: {project_id}") from exc

    def list_projects(self) -> tuple[Project, ...]:
        return tuple(self._projects[key] for key in sorted(self._projects))

    def add_milestone(self, project_id: str, title: str) -> Project:
        project = self.get_project(project_id)
        updated = replace(project, milestones=(*project.milestones, Milestone(title=title)))
        self._projects[project_id] = updated
        return updated

    def add_task(self, project_id: str, title: str, *, milestone_id: str | None = None) -> Project:
        project = self.get_project(project_id)
        updated = replace(
            project,
            tasks=(*project.tasks, ProjectTask(title=title, milestone_id=milestone_id)),
        )
        self._projects[project_id] = updated
        return updated

    def set_task_status(self, project_id: str, task_id: str, status: TaskStatus) -> Project:
        project = self.get_project(project_id)
        tasks = tuple(
            replace(task, status=status) if task.id == task_id else task
            for task in project.tasks
        )
        updated = replace(project, tasks=tasks)
        self._projects[project_id] = updated
        return updated

    def set_project_status(self, project_id: str, status: ProjectStatus) -> Project:
        project = self.get_project(project_id)
        updated = replace(project, status=status)
        self._projects[project_id] = updated
        return updated

    def to_dict(self) -> dict[str, Any]:
        return {"projects": [project.to_dict() for project in self._projects.values()]}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProjectManager":
        manager = cls()
        for item in payload.get("projects", []):
            if isinstance(item, dict):
                project = Project.from_dict(item)
                manager._projects[project.id] = project
        return manager
