"""Deterministic product planning over the approved requirements.

No model is asked to invent schedules or effort. A task's estimate is either an
operator-supplied number with its basis and confidence, or explicitly absent.
The critical path is a dependency calculation; it is not a promised delivery
date, and every response says so.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any, Mapping, Sequence

from novacontrol.agents.models import AgentRole
from novacontrol.product.models import ProductProject, new_id, utcnow

_ROLE_BY_KIND = {
    "functional": AgentRole.CODING.value,
    "core_feature": AgentRole.CODING.value,
    "optional_feature": AgentRole.CODING.value,
    "non_functional": AgentRole.CODING.value,
    "security": AgentRole.REVIEW.value,
    "performance": AgentRole.TESTING.value,
    "accessibility": AgentRole.CODING.value,
    "constraint": AgentRole.PLANNING.value,
}
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_IMPLEMENTATION_KINDS = {
    "functional",
    "core_feature",
    "optional_feature",
    "non_functional",
    "security",
    "performance",
    "accessibility",
}


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [row.strip() for row in value.replace("\r", "").split("\n") if row.strip()]
    if isinstance(value, (list, tuple)):
        return [_text(row) for row in value if _text(row)]
    return [_text(value)] if _text(value) else []


def _priority(value: Any) -> str:
    raw = _text(value).lower()
    return raw if raw in {"critical", "high", "medium", "low"} else "medium"


def _estimate(value: Any) -> tuple[float | None, str]:
    if value is None or value == "":
        return None, "not estimated"
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None, "not estimated"
    if hours <= 0:
        return None, "not estimated"
    return round(hours, 2), "operator_supplied"


def _normalise_task(
    row: Mapping[str, Any],
    *,
    project: ProductProject,
    index: int,
    roles: set[str],
    agent_by_role: Mapping[str, str],
) -> dict[str, Any]:
    task_id = _text(row.get("id")) or f"TASK-{index:03d}"
    if not _TASK_ID.match(task_id):
        raise ValueError(f"Task id is not safe: {task_id}")
    title = _text(row.get("title") or row.get("name"))
    if not title:
        raise ValueError(f"Task {task_id} needs a title.")
    description = _text(row.get("description")) or title
    requirement_ids = _strings(row.get("requirement_ids") or row.get("requirements"))
    for requirement_id in requirement_ids:
        if project.requirement(requirement_id) is None:
            raise ValueError(
                f"Task {task_id} references an unknown requirement: {requirement_id}"
            )
    role = _text(row.get("role")).lower()
    if role not in roles:
        raise ValueError(f"Task {task_id} names an unknown agent role: {role or '(blank)'}")
    estimate, basis = _estimate(row.get("estimate_hours"))
    confidence = _text(row.get("estimate_confidence")) or (
        "operator_assumption" if estimate is not None else "unknown"
    )
    dependencies = _strings(row.get("dependencies"))
    return {
        "id": task_id,
        "title": title,
        "description": description,
        "role": role,
        "priority": _priority(row.get("priority")),
        "estimate_hours": estimate,
        "estimate_basis": basis,
        "estimate_confidence": confidence,
        "dependencies": dependencies,
        "milestone_id": _text(row.get("milestone_id")),
        "parallel_safe": bool(row.get("parallel_safe", False)),
        "requirement_ids": requirement_ids,
        "assigned_agent": _text(row.get("assigned_agent")) or agent_by_role.get(role, ""),
        "status": "pending",
        "attempts": 0,
        "result": "",
        "evidence": [],
        "files_written": [],
        "last_error": "",
        "created_at": utcnow().isoformat(),
        "updated_at": utcnow().isoformat(),
        "corrective_for": _text(row.get("corrective_for")),
    }


def _tasks_from_requirements(
    project: ProductProject,
    *,
    roles: set[str],
    agent_by_role: Mapping[str, str],
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    task_by_requirement: dict[str, str] = {}
    for requirement in project.requirements():
        kind = str(requirement.get("kind", ""))
        if kind not in _IMPLEMENTATION_KINDS:
            continue
        index = len(tasks) + 1
        task_id = f"TASK-{index:03d}"
        task_by_requirement[str(requirement["id"])] = task_id
        role = _ROLE_BY_KIND.get(kind, AgentRole.CODING.value)
        if role not in roles:
            role = AgentRole.CODING.value
        title_prefix = {
            "security": "Implement security requirement",
            "performance": "Implement performance requirement",
            "accessibility": "Implement accessibility requirement",
            "non_functional": "Implement quality requirement",
        }.get(kind, "Implement requirement")
        tasks.append(
            {
                "id": task_id,
                "title": f"{title_prefix}: {requirement['title']}",
                "description": str(requirement.get("description") or requirement["title"]),
                "role": role,
                "priority": _priority(requirement.get("priority")),
                "estimate_hours": None,
                "estimate_basis": "not estimated",
                "estimate_confidence": "unknown",
                "dependencies": [],
                "milestone_id": "MS-1",
                "parallel_safe": False,
                "requirement_ids": [str(requirement["id"])],
                "assigned_agent": agent_by_role.get(role, ""),
                "status": "pending",
                "attempts": 0,
                "result": "",
                "evidence": [],
                "files_written": [],
                "last_error": "",
                "created_at": utcnow().isoformat(),
                "updated_at": utcnow().isoformat(),
                "corrective_for": "",
            }
        )
    # A verification task exists for every implementation task and depends on it.
    implementation_ids = [str(row["id"]) for row in tasks]
    for requirement_id, implementation_id in task_by_requirement.items():
        index = len(tasks) + 1
        role = AgentRole.TESTING.value if AgentRole.TESTING.value in roles else AgentRole.VERIFICATION.value
        tasks.append(
            {
                "id": f"TASK-{index:03d}",
                "title": f"Verify acceptance for {requirement_id}",
                "description": (
                    f"Run the relevant tests and record evidence for {requirement_id}; "
                    "a passing build alone is not acceptance."
                ),
                "role": role,
                "priority": "high",
                "estimate_hours": None,
                "estimate_basis": "not estimated",
                "estimate_confidence": "unknown",
                "dependencies": [implementation_id],
                "milestone_id": "MS-2",
                "parallel_safe": True,
                "requirement_ids": [requirement_id],
                "assigned_agent": agent_by_role.get(role, ""),
                "status": "pending",
                "attempts": 0,
                "result": "",
                "evidence": [],
                "files_written": [],
                "last_error": "",
                "created_at": utcnow().isoformat(),
                "updated_at": utcnow().isoformat(),
                "corrective_for": "",
            }
        )
    del implementation_ids
    return tasks


def _milestones(
    tasks: Sequence[Mapping[str, Any]], supplied: Any
) -> list[dict[str, Any]]:
    raw = supplied if isinstance(supplied, (list, tuple)) else []
    rows: list[dict[str, Any]] = []
    if raw:
        for index, item in enumerate(raw, start=1):
            if not isinstance(item, Mapping):
                continue
            milestone_id = _text(item.get("id")) or f"MS-{index}"
            rows.append(
                {
                    "id": milestone_id,
                    "title": _text(item.get("title")) or f"Milestone {index}",
                    "description": _text(item.get("description")),
                    "target_order": index,
                    "task_ids": _strings(item.get("task_ids")),
                    "dependencies": _strings(item.get("dependencies")),
                    "status": "pending",
                }
            )
    else:
        by_milestone: dict[str, list[str]] = defaultdict(list)
        for task in tasks:
            by_milestone[str(task.get("milestone_id") or "MS-1")].append(str(task["id"]))
        labels = {
            "MS-1": "Implementation",
            "MS-2": "Quality verification",
            "MS-3": "Release preparation",
        }
        for index, milestone_id in enumerate(sorted(by_milestone), start=1):
            rows.append(
                {
                    "id": milestone_id,
                    "title": labels.get(milestone_id, milestone_id),
                    "description": "",
                    "target_order": index,
                    "task_ids": by_milestone[milestone_id],
                    "dependencies": [],
                    "status": "pending",
                }
            )
    known = {str(row["id"]) for row in tasks}
    milestone_ids = [str(row["id"]) for row in rows]
    if len(milestone_ids) != len(set(milestone_ids)):
        raise ValueError("Milestone ids must be unique.")
    known_milestones = set(milestone_ids)
    for milestone in rows:
        unknown = [task_id for task_id in milestone["task_ids"] if task_id not in known]
        if unknown:
            raise ValueError(
                f"Milestone {milestone['id']} references unknown task(s): {', '.join(unknown)}"
            )
        unknown_dependencies = [
            item for item in milestone["dependencies"] if item not in known_milestones
        ]
        if unknown_dependencies:
            raise ValueError(
                f"Milestone {milestone['id']} depends on unknown milestone(s): "
                + ", ".join(unknown_dependencies)
            )
        if milestone["id"] in milestone["dependencies"]:
            raise ValueError(f"Milestone {milestone['id']} cannot depend on itself.")
    return rows


def _critical_path(tasks: Sequence[Mapping[str, Any]]) -> list[str]:
    by_id = {str(row["id"]): row for row in tasks}
    indegree = {task_id: 0 for task_id in by_id}
    outgoing: dict[str, list[str]] = defaultdict(list)
    for task in tasks:
        for dependency in task.get("dependencies", []):
            if dependency in by_id:
                outgoing[dependency].append(str(task["id"]))
                indegree[str(task["id"])] += 1
    queue = deque(sorted(task_id for task_id, degree in indegree.items() if degree == 0))
    weights = {
        task_id: float(row["estimate_hours"])
        if row.get("estimate_hours") is not None
        else 0.0
        for task_id, row in by_id.items()
    }
    # A node's duration belongs to every path that ends at it. Initialising with
    # its own weight (and a depth tie-breaker) keeps an unestimated dependency
    # chain visible instead of collapsing to a single zero-length task.
    distance = dict(weights)
    depth = {task_id: 1 for task_id in by_id}
    previous: dict[str, str] = {}
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for child in outgoing[current]:
            candidate = distance[current] + weights[child]
            candidate_depth = depth[current] + 1
            if candidate > distance[child] or (
                candidate == distance[child] and candidate_depth > depth[child]
            ):
                distance[child] = candidate
                depth[child] = candidate_depth
                previous[child] = current
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(by_id):
        raise ValueError("The task dependency graph contains a cycle.")
    if not by_id:
        return []
    end = max(sorted(by_id), key=lambda task_id: (distance[task_id], depth[task_id]))
    path: list[str] = []
    cursor = end
    while cursor:
        path.append(cursor)
        cursor = previous.get(cursor, "")
    return list(reversed(path))


def build_plan(
    project: ProductProject,
    inputs: Mapping[str, Any],
    *,
    actor: str,
    resource_snapshot: Mapping[str, Any] | None = None,
    agent_by_role: Mapping[str, str] | None = None,
    max_parallel: int = 4,
) -> dict[str, Any]:
    prd = project.prd
    if prd is None or prd.get("status") != "approved":
        raise ValueError("A development plan requires an approved PRD.")
    roles = {role.value for role in AgentRole}
    agents = dict(agent_by_role or {})
    data = dict(inputs or {})
    supplied_tasks = data.get("tasks")
    if isinstance(supplied_tasks, (list, tuple)) and supplied_tasks:
        tasks = [
            _normalise_task(
                row,
                project=project,
                index=index,
                roles=roles,
                agent_by_role=agents,
            )
            for index, row in enumerate(supplied_tasks, start=1)
            if isinstance(row, Mapping)
        ]
        if len(tasks) != len(supplied_tasks):
            raise ValueError("Every supplied task must be an object.")
    else:
        tasks = _tasks_from_requirements(project, roles=roles, agent_by_role=agents)
    if not tasks:
        raise ValueError(
            "No implementable requirements were found, so no development plan can be built."
        )
    known = {str(row["id"]) for row in tasks}
    if len(known) != len(tasks):
        raise ValueError("Task ids must be unique.")
    for task in tasks:
        unknown = [item for item in task["dependencies"] if item not in known]
        if unknown:
            raise ValueError(
                f"Task {task['id']} depends on unknown task(s): {', '.join(unknown)}"
            )
        if task["id"] in task["dependencies"]:
            raise ValueError(f"Task {task['id']} cannot depend on itself.")
    critical_path = _critical_path(tasks)
    milestones = _milestones(tasks, data.get("milestones"))
    for task in tasks:
        dependencies_done = not task["dependencies"]
        task["status"] = "ready" if dependencies_done else "pending"
    total_estimate = sum(
        float(row["estimate_hours"])
        for row in tasks
        if row.get("estimate_hours") is not None
    )
    unestimated = sum(1 for row in tasks if row.get("estimate_hours") is None)
    risks: list[dict[str, Any]] = []
    for condition in project.conditions:
        risks.append(
            {
                "kind": "unresolved_opportunity_condition",
                "description": condition,
                "source": f"Phase 35 opportunity {project.opportunity_id}",
            }
        )
    if unestimated:
        risks.append(
            {
                "kind": "estimation_gap",
                "description": f"{unestimated} task(s) have no effort estimate.",
                "source": "planning",
            }
        )
    for raw_risk in data.get("risks") or []:
        text = _text(raw_risk)
        if text:
            risks.append({"kind": "operator_supplied", "description": text, "source": actor})
    if data.get("risks") is None:
        risks.append(
            {
                "kind": "resource_assumption",
                "description": (
                    "Parallelism is bounded by the declared limit; available compute and "
                    "agents are measured, not reserved."
                ),
                "source": "planning",
            }
        )
    return {
        "id": new_id("plan"),
        "version": int((project.plan or {}).get("version", 0)) + 1,
        "requirements_version": prd.get("version"),
        "status": "draft",
        "tasks": tasks,
        "milestones": milestones,
        "critical_path": critical_path,
        "critical_path_basis": (
            "Longest dependency path using supplied effort estimates; unknown "
            "estimates count as zero and are not promises."
        ),
        "resource_assumptions": {
            "snapshot": dict(resource_snapshot or {}),
            "max_parallel": max(1, int(max_parallel)),
            "available_agent_roles": sorted({str(row["role"]) for row in tasks}),
            "agent_by_role": agents,
            "note": "Agents and compute are shared platform resources, not dedicated reserves.",
        },
        "delivery_plan": {
            "milestones_in_order": [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "target_order": row["target_order"],
                }
                for row in milestones
            ],
            "estimated_hours_total": round(total_estimate, 2),
            "unestimated_tasks": unestimated,
            "calendar_dates_committed": False,
            "note": (
                "Milestones are ordered, not dated. Effort figures are estimates, not "
                "guaranteed completion dates."
            ),
        },
        "risks": risks,
        "estimate_disclaimer": (
            "All effort and duration figures are estimates with recorded confidence; "
            "this plan does not guarantee a completion date."
        ),
        "created_at": utcnow().isoformat(),
        "updated_at": utcnow().isoformat(),
        "created_by": actor,
        "approved_at": "",
        "approved_by": "",
        "approval_note": "",
    }


def approve_plan(
    project: ProductProject, *, approved: bool, actor: str, note: str
) -> dict[str, Any]:
    plan = project.plan
    if plan is None:
        raise ValueError("Create a development plan before approving it.")
    if not actor.strip():
        raise ValueError("A plan decision needs a named human approver.")
    if not approved and not note.strip():
        raise ValueError("A plan rejection needs a note.")
    now = utcnow().isoformat()
    plan["status"] = "approved" if approved else "rejected"
    plan["approved_at"] = now if approved else ""
    plan["approved_by"] = actor if approved else ""
    plan["approval_note"] = note
    plan["updated_at"] = now
    return plan


__all__ = ["approve_plan", "build_plan"]
