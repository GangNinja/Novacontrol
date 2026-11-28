"""Phase 36 product-development and launch service.

One service owns the lifecycle. It reads Phase 35's stored validation report,
persists one project record per opportunity through NovaControl's state store,
executes tasks through the existing AgentRegistry (or an injected executor),
and records events/audit entries through the same application seams every other
phase uses.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from novacontrol.agents.models import AgentRole, AgentTask, AgentTaskStatus
from novacontrol.product.architecture import approve_architecture, build_architecture
from novacontrol.product.errors import (
    ApprovalRequired,
    BudgetExceeded,
    OpportunityNotEligible,
    ProductConflict,
    ProductNotFound,
    ProductValidationError,
    QualityGateError,
    WorkspaceError,
)
from novacontrol.product.intake import accept_opportunity
from novacontrol.product.launch import approve_launch, evaluate_launch
from novacontrol.product.models import (
    ApprovalKind,
    DefectSeverity,
    DefectStatus,
    GateStatus,
    OpportunityValidationState,
    ProductProject,
    ProductState,
    ProductTask,
    RELEASE_TARGETS,
    RequirementStatus,
    TaskOutcome,
    TaskState,
    TestStatus,
    new_id,
    utcnow,
)
from novacontrol.product.planning import approve_plan, build_plan
from novacontrol.product.prd import (
    approve_prd,
    archive_prd_revision,
    build_prd,
    mark_requirement_verified,
)
from novacontrol.product.quality import (
    blocking_defects,
    failing_gates,
    normalise_gates,
    open_defect,
    quality_snapshot,
    recompute_gate,
    record_build_result,
    record_test_result,
    reopen_defect,
    resolve_defect,
)
from novacontrol.product.release import approve_release, prepare_release
from novacontrol.product.workspace import (
    ProductWorkspace,
    contains_secret,
    redact_secrets,
)

SCHEMA_VERSION = 1
NAMESPACE = "product_projects"

OpportunityLoader = Callable[[str], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]
TaskExecutor = Callable[[ProductTask], TaskOutcome | Awaitable[TaskOutcome]]
TestRunner = Callable[..., Sequence[Mapping[str, Any]] | Mapping[str, Any] | Awaitable[Any]]
BuildRunner = Callable[..., Mapping[str, Any] | Awaitable[Mapping[str, Any]]]
EnvironmentProbe = Callable[[Mapping[str, Any]], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


def _default_workspace_root() -> Path:
    return Path.cwd() / "build_workspace" / "products"


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _safe_detail(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _safe_detail(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_detail(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value


def _json_for_secret_scan(value: Any) -> str:
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class ProductService:
    """The persisted Phase 36 lifecycle service."""

    def __init__(
        self,
        *,
        store: Any | None = None,
        opportunities: OpportunityLoader | None = None,
        workspace_root: Path | str | None = None,
        agent_registry: Any | None = None,
        task_executor: TaskExecutor | None = None,
        test_runner: TestRunner | None = None,
        build_runner: BuildRunner | None = None,
        environment_probe: EnvironmentProbe | None = None,
        resource_snapshot: Callable[[], Mapping[str, Any]] | None = None,
        publish: Callable[[str, Mapping[str, Any]], Any] | None = None,
        audit: Callable[..., Any] | None = None,
        memory: Callable[..., Any] | None = None,
        knowledge: Callable[..., Any] | None = None,
        actor: str = "operator",
        max_products: int = 50,
        max_tasks: int = 200,
        max_tests: int = 500,
        max_defects: int = 100,
        max_events: int = 500,
        max_parallel: int = 4,
        max_task_attempts: int = 2,
        max_iterations: int = 3,
        max_workspace_files: int = 500,
        max_file_bytes: int = 1_000_000,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._opportunities = opportunities
        self._workspace = ProductWorkspace(
            Path(workspace_root) if workspace_root is not None else _default_workspace_root(),
            max_files=max_workspace_files,
            max_file_bytes=max_file_bytes,
        )
        self._registry = agent_registry
        self._task_executor = task_executor
        self._test_runner = test_runner
        self._build_runner = build_runner
        self._environment_probe = environment_probe
        self._resource_snapshot = resource_snapshot
        self._publish_callback = publish
        self._audit_callback = audit
        self._memory_callback = memory
        self._knowledge_callback = knowledge
        self.actor = actor
        self.max_products = max(1, int(max_products))
        self.max_tasks = max(1, int(max_tasks))
        self.max_tests = max(1, int(max_tests))
        self.max_defects = max(1, int(max_defects))
        self.max_events = max(20, int(max_events))
        self.max_parallel = max(1, min(12, int(max_parallel)))
        self.max_task_attempts = max(1, min(5, int(max_task_attempts)))
        self.max_iterations = max(1, min(5, int(max_iterations)))
        self._clock = clock or utcnow
        self._projects: dict[str, ProductProject] = {}
        self._loaded = False
        self._load_errors: list[str] = []
        self._write_lock = threading.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._task_locks: dict[str, asyncio.Lock] = {}

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._store is None:
            return
        payload = self._store.read(NAMESPACE)
        rows = payload.get("projects") if isinstance(payload, Mapping) else None
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            return
        recovered = False
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            try:
                project = ProductProject.from_dict(row)
            except Exception as exc:  # noqa: BLE001 - one corrupt record must not hide the rest
                self._load_errors.append(str(exc))
                continue
            for task in project.tasks():
                if task.get("status") == TaskState.RUNNING.value:
                    task["status"] = TaskState.PENDING.value
                    task["last_error"] = "Execution was interrupted by a restart; retry is required."
                    recovered = True
            self._projects[project.id] = project
        if recovered:
            self._persist()

    def _persist(self) -> None:
        if self._store is None:
            return
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": self._clock().isoformat(),
            "projects": [project.to_dict() for project in self._projects.values()],
        }
        with self._write_lock:
            self._store.write(NAMESPACE, payload)

    def _lock(self, product_id: str) -> asyncio.Lock:
        return self._locks.setdefault(product_id, asyncio.Lock())

    def _task_lock(self, product_id: str, task_id: str) -> asyncio.Lock:
        return self._task_locks.setdefault(f"{product_id}:{task_id}", asyncio.Lock())

    def _get(self, product_id: str) -> ProductProject:
        self._load()
        key = str(product_id or "").strip()
        project = self._projects.get(key)
        if project is None:
            raise ProductNotFound(key)
        return project

    def _require_active(self, project: ProductProject) -> None:
        if project.state == ProductState.ARCHIVED.value or project.archived_at:
            raise ProductConflict("The product is archived.")
        if project.cancelled:
            raise ProductConflict("The product was cancelled; resume it before continuing.")

    def _require_not_archived(self, project: ProductProject) -> None:
        if project.state == ProductState.ARCHIVED.value or project.archived_at:
            raise ProductConflict("The product is archived.")

    # -- recording -----------------------------------------------------------

    def _schedule(self, result: Any) -> None:
        """Run an optional async integration without delaying the state transition."""
        if not inspect.isawaitable(result):
            return

        async def _consume(awaitable: Any) -> None:
            try:
                await awaitable
            except Exception:  # noqa: BLE001 - memory/knowledge must not roll back a product
                pass

        try:
            asyncio.get_running_loop().create_task(_consume(result))
        except RuntimeError:
            pass

    def _remember(self, project: ProductProject, event_type: str) -> None:
        """Persist a small project-memory snapshot through the existing memory manager."""
        if self._memory_callback is None:
            return
        value = {
            "product_id": project.id,
            "name": project.name,
            "opportunity_id": project.opportunity_id,
            "validation_state": project.validation_state,
            "state": project.state,
            "tasks": len(project.tasks()),
            "completed_tasks": sum(
                1 for row in project.tasks() if row.get("status") == TaskState.COMPLETED.value
            ),
            "failing_gates": failing_gates(project),
            "readiness": (project.readiness or {}).get("status", ""),
            "last_event": event_type,
        }
        try:
            result = self._memory_callback(
                key=f"product:{project.id}:latest",
                value=value,
                text=f"Product {project.name}: {event_type}; state {project.state}.",
                importance=0.7,
            )
            self._schedule(result)
        except Exception:  # noqa: BLE001 - optional integration
            pass
        if self._knowledge_callback is not None and event_type == "requirements_approved":
            prd = project.prd or {}
            titles = "; ".join(
                str(row.get("title", "")) for row in prd.get("requirements", [])[:20]
            )
            try:
                result = self._knowledge_callback(
                    (
                        f"Approved product requirements for {project.name} "
                        f"(PRD v{prd.get('version')}), originating opportunity "
                        f"{project.opportunity_id}: {titles}"
                    ),
                    title=f"Product PRD — {project.name}",
                )
                self._schedule(result)
            except Exception:  # noqa: BLE001 - optional integration
                pass

    def _event(
        self, project: ProductProject, event_type: str, *, actor: str, detail: Mapping[str, Any]
    ) -> dict[str, Any]:
        event = {
            "id": new_id("evt"),
            "type": event_type,
            "product_id": project.id,
            "at": self._clock().isoformat(),
            "actor": actor,
            "detail": _safe_detail(dict(detail)),
        }
        project.events.append(event)
        project.events = project.events[-self.max_events :]
        project.updated_at = self._clock()
        payload = {
            "product_id": project.id,
            "event_id": event["id"],
            "state": project.state,
        }
        if self._publish_callback is not None:
            try:
                result = self._publish_callback(f"product.{event_type}", payload)
                self._schedule(result)
            except Exception:  # noqa: BLE001 - notification must not roll back a transition
                pass
        if event_type in {
            "project_created",
            "requirements_approved",
            "architecture_approved",
            "development_started",
            "task_completed",
            "release_prepared",
            "launch_readiness_evaluated",
            "launch_approved",
            "launch_rejected",
            "product_cancelled",
        }:
            self._remember(project, event_type)
        if self._audit_callback is not None and event_type in {
            "project_created",
            "opportunity_accepted",
            "requirements_approved",
            "architecture_approved",
            "development_started",
            "task_completed",
            "task_failed",
            "defect_opened",
            "defect_resolved",
            "release_prepared",
            "launch_readiness_evaluated",
            "launch_approved",
            "launch_rejected",
        }:
            try:
                self._audit_callback(
                    task_id=project.id,
                    user_request=f"product:{project.id}:{event_type}",
                    outcome=event_type,
                    source="product",
                    actions=[event_type],
                    failures=[],
                )
            except Exception:  # noqa: BLE001 - audit failure is surfaced by the audit layer
                pass
        return event

    def _approval(
        self,
        project: ProductProject,
        kind: ApprovalKind | str,
        *,
        target_id: str,
        approved: bool,
        actor: str,
        note: str,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = {
            "kind": str(kind),
            "target_id": target_id,
            "approved": bool(approved),
            "actor": actor,
            "note": note,
            "at": self._clock().isoformat(),
            "automated_recommendation": False,
        }
        if extra:
            row.update(dict(extra))
        project.approvals.append(row)
        project.approvals = project.approvals[-200:]
        return row

    def _assert_no_secrets(self, payload: Any, label: str) -> None:
        if contains_secret(_json_for_secret_scan(payload)):
            raise ProductValidationError(
                f"{label} contains a credential-shaped value. Keep secrets in configuration, "
                "not in product documents or generated files."
            )

    def _workspace_scan(self, project: ProductProject) -> dict[str, Any]:
        try:
            return self._workspace.scan(project.id)
        except Exception as exc:  # noqa: BLE001 - a missing workspace is a reported condition
            return {
                "files": 0,
                "bytes": 0,
                "secret_violations": [],
                "within_budget": False,
                "error": str(exc),
            }

    def _agent_names(self) -> list[str]:
        if self._registry is None:
            return []
        try:
            return [str(agent.name) for agent in self._registry.list()]
        except Exception:  # noqa: BLE001 - optional integration
            return []

    def _agent_by_role(self) -> dict[str, str]:
        if self._registry is None:
            return {}
        rows: dict[str, str] = {}
        for role in AgentRole:
            try:
                agent = self._registry.first_by_role(role)
            except Exception:  # noqa: BLE001
                agent = None
            if agent is not None:
                rows[role.value] = str(agent.name)
        return rows

    def _resource_read(self) -> dict[str, Any]:
        if self._resource_snapshot is None:
            return {}
        try:
            return dict(self._resource_snapshot() or {})
        except Exception as exc:  # noqa: BLE001 - resource probing is optional
            return {"error": str(exc)}

    # -- project creation and intake ------------------------------------------

    async def create_project(
        self,
        *,
        opportunity_id: str,
        name: str = "",
        approved: bool = False,
        approval_note: str = "",
        conditions_acknowledged: Sequence[str] = (),
        actor: str = "",
        release_target: str = "local",
        repository: str = "",
        environment: Mapping[str, Any] | None = None,
        quality_gates: Any = None,
    ) -> dict[str, Any]:
        self._load()
        if len(self._projects) >= self.max_products:
            raise BudgetExceeded(f"The product budget ({self.max_products}) is exhausted.")
        opportunity_key = str(opportunity_id or "").strip()
        if not opportunity_key:
            raise ProductValidationError("Choose a validated opportunity first.")
        existing = next(
            (
                project
                for project in self._projects.values()
                if project.opportunity_id == opportunity_key
            ),
            None,
        )
        if existing is not None:
            raise ProductConflict(
                f"Opportunity {opportunity_key} already has product project {existing.id}."
            )
        if self._opportunities is None:
            raise ProductConflict("No Phase 35 opportunity service is configured.")
        try:
            detail = await _maybe_await(self._opportunities(opportunity_key))
        except Exception as exc:  # noqa: BLE001 - the Phase 35 refusal is the useful error
            raise ProductConflict(f"Could not read the Phase 35 opportunity: {exc}") from exc
        if not isinstance(detail, Mapping):
            raise ProductConflict("The Phase 35 opportunity loader returned no record.")
        actor_name = _text(actor) or self.actor
        decision = accept_opportunity(
            detail,
            approved=approved,
            actor=actor_name,
            note=approval_note,
            conditions_acknowledged=tuple(str(item) for item in conditions_acknowledged),
        )
        project_name = _text(name) or decision.opportunity_title
        if len(project_name) > 120:
            raise ProductValidationError("A product name is limited to 120 characters.")
        target = _text(release_target).lower() or "local"
        if target not in RELEASE_TARGETS:
            raise ProductValidationError(
                f"Release target must be one of: {', '.join(RELEASE_TARGETS)}"
            )
        env = self._build_environment(environment or {})
        gates = normalise_gates(quality_gates)
        now = self._clock()
        project = ProductProject(
            id=f"product-{now.strftime('%Y%m%d%H%M%S')}-{len(self._projects) + 1}",
            name=project_name,
            opportunity_id=decision.opportunity_id,
            opportunity_title=decision.opportunity_title,
            validation_state=decision.state.value,
            validation_report=decision.report,
            opportunity_snapshot=decision.snapshot,
            conditions=list(decision.conditions),
            condition_approval=dict(decision.condition_approval),
            state=ProductState.DEVELOPMENT.value,
            release_target=target,
            repository=_text(repository),
            environment=env,
            quality_gates=gates,
            agents=self._agent_names(),
            created_at=now,
            updated_at=now,
        )
        if decision.conditional:
            project.condition_approval["at"] = now.isoformat()
            self._approval(
                project,
                ApprovalKind.OPPORTUNITY_CONDITIONS,
                target_id=decision.opportunity_id,
                approved=True,
                actor=actor_name,
                note=approval_note,
                extra={"conditions": list(decision.conditions)},
            )
        project.workspace_path = str(self._workspace.create(project.id))
        self._projects[project.id] = project
        self._event(
            project,
            "project_created",
            actor=actor_name,
            detail={
                "opportunity_id": project.opportunity_id,
                "validation_state": project.validation_state,
                "workspace": project.workspace_path,
                "release_target": project.release_target,
            },
        )
        self._event(
            project,
            "opportunity_accepted",
            actor=actor_name,
            detail={
                "conditions": len(project.conditions),
                "conditional_approval": bool(project.condition_approval),
            },
        )
        self._persist()
        return self.detail(project.id)

    async def import_opportunity(self, **kwargs: Any) -> dict[str, Any]:
        return await self.create_project(**kwargs)

    def list_projects(
        self, *, query: str = "", state: str = "", limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        self._load()
        rows = sorted(self._projects.values(), key=lambda row: row.updated_at, reverse=True)
        if state:
            rows = [row for row in rows if row.state == state]
        if query:
            needle = query.strip().lower()
            rows = [
                row
                for row in rows
                if needle
                in " ".join(
                    (
                        row.name,
                        row.opportunity_title,
                        row.opportunity_id,
                        row.id,
                        row.validation_state,
                    )
                ).lower()
            ]
        total = len(rows)
        page = rows[max(0, offset) : max(0, offset) + max(1, min(200, limit))]
        return {
            "products": [row.summary() for row in page],
            "total": total,
            "limit": max(1, min(200, limit)),
            "offset": max(0, offset),
        }

    def detail(self, product_id: str) -> dict[str, Any]:
        project = self._get(product_id)
        plan = project.plan or {}
        tasks = project.tasks()
        completed = sum(1 for task in tasks if task.get("status") == TaskState.COMPLETED.value)
        scan = self._workspace_scan(project)
        return {
            **project.to_dict(),
            "summary": project.summary(),
            "quality": quality_snapshot(project),
            "workspace": scan,
            "progress": {
                "tasks": len(tasks),
                "completed": completed,
                "ratio": round(completed / len(tasks), 4) if tasks else 0.0,
                "milestones": plan.get("milestones", []),
                "critical_path": plan.get("critical_path", []),
            },
            "available_actions": self._available_actions(project),
            "decision_boundary": {
                "automated_recommendations": [
                    "PRD draft and requirement decomposition",
                    "architecture draft and component grouping",
                    "plan, dependency graph and critical path",
                    "test-gate evaluation",
                    "launch-readiness score",
                ],
                "human_approvals_required": [
                    "conditional opportunity acceptance",
                    "PRD approval",
                    "architecture approval",
                    "release approval",
                    "launch approval",
                ],
                "external_launch_executed": False,
                "commits_pushed": False,
                "external_deployment_performed": False,
            },
        }

    def _available_actions(self, project: ProductProject) -> dict[str, bool]:
        prd = project.prd or {}
        architecture = project.architecture or {}
        plan = project.plan or {}
        release = project.release() or {}
        readiness = project.readiness or {}
        return {
            "generate_prd": not project.cancelled and project.state != "ARCHIVED",
            "approve_prd": bool(prd) and prd.get("status") in {"draft", "revision_requested"},
            "generate_architecture": prd.get("status") == "approved",
            "approve_architecture": bool(architecture)
            and architecture.get("status") == "draft",
            "create_plan": prd.get("status") == "approved",
            "start_development": bool(plan)
            and prd.get("status") == "approved"
            and architecture.get("status") == "approved",
            "run_tasks": bool(project.development_started_at)
            and not project.cancelled,
            "prepare_release": project.required_gates_passed()
            and bool(project.build.get("validated"))
            and not project.cancelled,
            "approve_release": bool(release) and release.get("status") == "draft",
            "evaluate_launch": not project.cancelled,
            "approve_launch": readiness.get("status")
            == ProductState.READY_FOR_RELEASE_REVIEW.value
            and (readiness.get("requirements_completion") or {}).get("requirements", 0) > 0
            and (readiness.get("requirements_completion") or {}).get("verified", 0)
            == (readiness.get("requirements_completion") or {}).get("requirements", 0)
            and (readiness.get("release_approved") is True),
            "archive": project.state != "ARCHIVED",
        }

    # -- documents ------------------------------------------------------------

    def generate_prd(
        self, product_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        self._assert_no_secrets(inputs, "PRD input")
        actor_name = _text(actor) or self.actor
        archive_prd_revision(project, "Superseded by a new PRD revision.")
        project.prd = build_prd(project, inputs, actor=actor_name)
        self._event(
            project,
            "prd_generated",
            actor=actor_name,
            detail={
                "prd_id": project.prd["id"],
                "version": project.prd["version"],
                "requirements": len(project.prd["requirements"]),
                "missing_information": len(project.prd["missing_information"]),
                "automated_recommendation": True,
            },
        )
        self._persist()
        return project.prd

    def approve_requirements(
        self,
        product_id: str,
        *,
        approved: bool,
        actor: str,
        note: str,
        version: int | None = None,
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        actor_name = _text(actor)
        try:
            result = approve_prd(
                project,
                approved=approved,
                actor=actor_name,
                note=note,
                version=version,
            )
        except ValueError as exc:
            raise ProductValidationError(str(exc)) from exc
        self._approval(
            project,
            ApprovalKind.REQUIREMENTS,
            target_id=result["prd"]["id"],
            approved=approved,
            actor=actor_name,
            note=note,
            extra={"version": result["prd"].get("version")},
        )
        self._event(
            project,
            "requirements_approved" if approved else "requirements_revision_requested",
            actor=actor_name,
            detail={
                "prd_id": result["prd"]["id"],
                "version": result["prd"].get("version"),
                "automated_recommendation": False,
            },
        )
        self._persist()
        return dict(result["prd"])

    def generate_architecture(
        self, product_id: str, inputs: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        self._assert_no_secrets(inputs, "Architecture input")
        actor_name = _text(actor) or self.actor
        if project.architecture:
            project.architecture_history.append(
                {
                    "version": project.architecture.get("version"),
                    "status": project.architecture.get("status"),
                    "archived_at": self._clock().isoformat(),
                    "snapshot": project.architecture,
                }
            )
            project.architecture_history = project.architecture_history[-20:]
        try:
            project.architecture = build_architecture(project, inputs, actor=actor_name)
        except ValueError as exc:
            raise ProductValidationError(str(exc)) from exc
        self._event(
            project,
            "architecture_generated",
            actor=actor_name,
            detail={
                "architecture_id": project.architecture["id"],
                "version": project.architecture["version"],
                "decisions": len(project.architecture.get("decisions", [])),
                "missing_information": len(project.architecture.get("missing_information", [])),
                "automated_recommendation": True,
            },
        )
        self._persist()
        return project.architecture

    def approve_architecture(
        self, product_id: str, *, approved: bool, actor: str, note: str
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        actor_name = _text(actor)
        try:
            architecture = approve_architecture(
                project, approved=approved, actor=actor_name, note=note
            )
        except ValueError as exc:
            raise ProductValidationError(str(exc)) from exc
        self._approval(
            project,
            ApprovalKind.ARCHITECTURE,
            target_id=architecture["id"],
            approved=approved,
            actor=actor_name,
            note=note,
            extra={"version": architecture.get("version")},
        )
        self._event(
            project,
            "architecture_approved" if approved else "architecture_rejected",
            actor=actor_name,
            detail={"architecture_id": architecture["id"], "automated_recommendation": False},
        )
        self._persist()
        return architecture

    def create_plan(
        self, product_id: str, inputs: Mapping[str, Any] | None = None, *, actor: str = ""
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        self._assert_no_secrets(inputs or {}, "Plan input")
        actor_name = _text(actor) or self.actor
        try:
            plan = build_plan(
                project,
                inputs or {},
                actor=actor_name,
                resource_snapshot=self._resource_read(),
                agent_by_role=self._agent_by_role(),
                max_parallel=self.max_parallel,
            )
        except ValueError as exc:
            raise ProductValidationError(str(exc)) from exc
        if len(plan["tasks"]) > self.max_tasks:
            raise BudgetExceeded(f"The task budget ({self.max_tasks}) is exhausted.")
        project.plan = plan
        self._event(
            project,
            "plan_created",
            actor=actor_name,
            detail={
                "plan_id": plan["id"],
                "tasks": len(plan["tasks"]),
                "milestones": len(plan["milestones"]),
                "critical_path_length": len(plan["critical_path"]),
                "automated_recommendation": True,
            },
        )
        self._persist()
        return plan

    def approve_plan_record(
        self, product_id: str, *, approved: bool, actor: str, note: str
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        actor_name = _text(actor)
        try:
            plan = approve_plan(project, approved=approved, actor=actor_name, note=note)
        except ValueError as exc:
            raise ProductValidationError(str(exc)) from exc
        self._approval(
            project,
            ApprovalKind.PLAN,
            target_id=plan["id"],
            approved=approved,
            actor=actor_name,
            note=note,
        )
        self._event(
            project,
            "plan_approved" if approved else "plan_rejected",
            actor=actor_name,
            detail={"plan_id": plan["id"], "automated_recommendation": False},
        )
        self._persist()
        return plan

    def start_development(self, product_id: str, *, actor: str = "") -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        if (project.prd or {}).get("status") != "approved":
            raise ApprovalRequired(
                "Development requires an approved PRD.", approval_kind="requirements"
            )
        if (project.architecture or {}).get("status") != "approved":
            raise ApprovalRequired(
                "Development requires an approved architecture.", approval_kind="architecture"
            )
        if (project.architecture or {}).get("requirements_version") != (
            project.prd or {}
        ).get("version"):
            raise ApprovalRequired(
                "The architecture was approved against a different PRD revision; "
                "regenerate and reapprove it.",
                approval_kind="architecture",
            )
        if not project.tasks():
            raise ProductConflict("Create and approve a development plan before starting.")
        if (project.plan or {}).get("requirements_version") != (project.prd or {}).get("version"):
            raise ApprovalRequired(
                "The development plan was built against a different PRD revision; "
                "rebuild the plan.",
                approval_kind="plan",
            )
        if project.development_started_at:
            return {"started_at": project.development_started_at, "already_started": True}
        actor_name = _text(actor) or self.actor
        project.development_started_at = self._clock().isoformat()
        for task in project.tasks():
            if task.get("status") == TaskState.PENDING.value and not task.get("dependencies"):
                task["status"] = TaskState.READY.value
        self._event(
            project,
            "development_started",
            actor=actor_name,
            detail={
                "tasks": len(project.tasks()),
                "workspace": project.workspace_path,
                "automated_recommendation": False,
            },
        )
        self._persist()
        return {"started_at": project.development_started_at, "already_started": False}

    # -- task execution -------------------------------------------------------

    async def _execute_task(self, project: ProductProject, task: dict[str, Any]) -> TaskOutcome:
        if self._task_executor is not None:
            result = self._task_executor(
                ProductTask(
                    id=str(task["id"]),
                    product_id=project.id,
                    title=str(task["title"]),
                    description=str(task.get("description", "")),
                    role=str(task.get("role", AgentRole.CODING.value)),
                    requirement_ids=tuple(str(item) for item in task.get("requirement_ids", [])),
                    dependencies=tuple(str(item) for item in task.get("dependencies", [])),
                    priority=str(task.get("priority", "medium")),
                )
            )
            outcome = await _maybe_await(result)
            if not isinstance(outcome, TaskOutcome):
                return TaskOutcome(
                    verified=False,
                    summary="The configured task executor returned no TaskOutcome.",
                )
            return outcome
        if self._registry is None:
            return TaskOutcome(
                verified=False,
                summary="No agent registry or task executor is configured.",
            )
        role_value = str(task.get("role", AgentRole.CODING.value))
        try:
            role = AgentRole(role_value)
        except ValueError:
            role = AgentRole.CODING
        agent = self._registry.first_by_role(role)
        if agent is None:
            return TaskOutcome(
                verified=False,
                summary=f"No registered agent fills the {role.value} responsibility.",
            )
        response = await agent.handle_task(
            AgentTask(
                goal=f"{task['title']}\n\n{task.get('description', '')}",
                role=role,
                metadata=ProductTask(
                    id=str(task["id"]),
                    product_id=project.id,
                    title=str(task["title"]),
                    description=str(task.get("description", "")),
                    role=role.value,
                    requirement_ids=tuple(str(item) for item in task.get("requirement_ids", [])),
                    dependencies=tuple(str(item) for item in task.get("dependencies", [])),
                    priority=str(task.get("priority", "medium")),
                ).to_agent_metadata(),
            )
        )
        verified = response.status is AgentTaskStatus.COMPLETED and bool(
            response.metadata.get("verified")
        )
        evidence = response.metadata.get("evidence")
        return TaskOutcome(
            verified=verified,
            summary=response.content[:4000],
            evidence=tuple(str(item) for item in evidence)
            if isinstance(evidence, (list, tuple))
            else (() if not verified else ("agent response",)),
            agent=str(response.agent_name),
        )

    def _write_task_files(self, project: ProductProject, files: Mapping[str, str]) -> list[str]:
        written: list[str] = []
        for relative_path, content in files.items():
            existing = relative_path in project.generated_files
            expected = project.generated_files.get(relative_path, "")
            result = self._workspace.write(
                project.id,
                relative_path,
                str(content),
                expected_sha256=expected if existing else "",
                overwrite=existing,
            )
            project.generated_files[relative_path] = result.sha256
            written.append(result.path)
        return written

    def _refresh_task_readiness(self, project: ProductProject) -> None:
        tasks = project.tasks()
        by_id = {str(task["id"]): task for task in tasks}
        for task in tasks:
            if task.get("status") not in {TaskState.PENDING.value, TaskState.BLOCKED.value}:
                continue
            dependencies = [by_id[item] for item in task.get("dependencies", []) if item in by_id]
            if any(
                row.get("status") in {TaskState.FAILED.value, TaskState.CANCELLED.value}
                for row in dependencies
            ):
                task["status"] = TaskState.BLOCKED.value
            elif all(row.get("status") == TaskState.COMPLETED.value for row in dependencies):
                task["status"] = TaskState.READY.value

    def _apply_task_outcome(
        self,
        project: ProductProject,
        task: dict[str, Any],
        outcome: TaskOutcome,
        *,
        actor: str,
    ) -> None:
        now = self._clock().isoformat()
        try:
            files_written = self._write_task_files(project, outcome.files)
        except WorkspaceError as exc:
            task["status"] = TaskState.FAILED.value
            task["last_error"] = str(exc)
            task["result"] = "Workspace refused the generated files."
            task["updated_at"] = now
            self._event(
                project,
                "task_failed",
                actor=actor,
                detail={"task_id": task["id"], "error": str(exc)},
            )
            self._refresh_task_readiness(project)
            return
        task["files_written"] = files_written
        if outcome.verified:
            task["status"] = TaskState.COMPLETED.value
            task["result"] = outcome.summary
            task["evidence"] = [str(item) for item in outcome.evidence]
            task["last_error"] = ""
            event_type = "task_completed"
        else:
            task["status"] = TaskState.NEEDS_REVIEW.value
            task["result"] = outcome.summary or "The executor did not verify this task."
            task["evidence"] = [str(item) for item in outcome.evidence]
            task["last_error"] = (
                "Execution did not provide verified evidence; a human must review it."
            )
            event_type = "task_needs_review"
        task["updated_at"] = now
        if outcome.agent:
            task["assigned_agent"] = outcome.agent
        self._event(
            project,
            event_type,
            actor=actor,
            detail={
                "task_id": task["id"],
                "status": task["status"],
                "evidence": len(task.get("evidence", [])),
                "files": len(files_written),
            },
        )
        self._refresh_task_readiness(project)
        for milestone in (project.plan or {}).get("milestones", []):
            task_ids = [str(item) for item in milestone.get("task_ids", [])]
            if not task_ids:
                continue
            rows = [project.task(item) for item in task_ids]
            if rows and all(row and row.get("status") == TaskState.COMPLETED.value for row in rows):
                milestone["status"] = "completed"
            elif any(row and row.get("status") in {TaskState.FAILED.value, TaskState.BLOCKED.value} for row in rows):
                milestone["status"] = "blocked"

    async def run_task(self, product_id: str, task_id: str, *, actor: str = "") -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        if not project.development_started_at:
            raise ProductConflict("Start development before executing tasks.")
        async with self._task_lock(project.id, task_id):
            task = project.task(task_id)
            if task is None:
                raise ProductConflict(f"Unknown task: {task_id}")
            if task.get("status") == TaskState.COMPLETED.value:
                return task
            if task.get("status") == TaskState.RUNNING.value:
                raise ProductConflict("That task is already running.")
            if int(task.get("attempts", 0)) >= self.max_task_attempts:
                raise BudgetExceeded(
                    f"Task {task_id} reached its attempt budget ({self.max_task_attempts})."
                )
            self._refresh_task_readiness(project)
            if task.get("status") == TaskState.BLOCKED.value:
                raise ProductConflict("A dependency failed or was cancelled; retry the dependency first.")
            if task.get("status") != TaskState.READY.value:
                raise ProductConflict(
                    "That task is waiting for its dependencies; run the dependency first."
                )
            actor_name = _text(actor) or self.actor
            task["status"] = TaskState.RUNNING.value
            task["attempts"] = int(task.get("attempts", 0)) + 1
            task["last_error"] = ""
            task["updated_at"] = self._clock().isoformat()
            self._event(
                project,
                "task_started",
                actor=actor_name,
                detail={
                    "task_id": task_id,
                    "attempt": task["attempts"],
                    "role": task.get("role"),
                },
            )
            self._persist()
            if project.cancelled:
                task["status"] = TaskState.CANCELLED.value
                self._persist()
                return task
            try:
                outcome = await self._execute_task(project, task)
            except Exception as exc:  # noqa: BLE001 - a failing agent must not kill the project
                task["status"] = TaskState.FAILED.value
                task["result"] = "Task execution failed."
                task["last_error"] = redact_secrets(str(exc))[:2000]
                task["updated_at"] = self._clock().isoformat()
                project.last_error = task["last_error"]
                self._event(
                    project,
                    "task_failed",
                    actor=actor_name,
                    detail={"task_id": task_id, "error": task["last_error"]},
                )
                self._refresh_task_readiness(project)
                self._persist()
                return task
            if project.cancelled:
                task["status"] = TaskState.CANCELLED.value
                task["result"] = "Cancelled while the task was executing."
                task["updated_at"] = self._clock().isoformat()
                self._persist()
                return task
            if not isinstance(outcome, TaskOutcome):
                task["status"] = TaskState.NEEDS_REVIEW.value
                task["result"] = "The executor returned an unrecognised result."
                task["last_error"] = "Expected TaskOutcome."
                self._event(
                    project,
                    "task_needs_review",
                    actor=actor_name,
                    detail={"task_id": task_id, "error": task["last_error"]},
                )
            else:
                self._apply_task_outcome(project, task, outcome, actor=actor_name)
            self._persist()
            return task

    async def run_ready_tasks(
        self, product_id: str, *, limit: int = 4, actor: str = ""
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        if not project.development_started_at:
            raise ProductConflict("Start development before executing tasks.")
        bound = max(1, min(self.max_parallel, int(limit or self.max_parallel)))
        results: list[dict[str, Any]] = []
        passes = 0
        while passes < max(1, len(project.tasks()) + 2):
            passes += 1
            self._refresh_task_readiness(project)
            ready = [
                task
                for task in project.tasks()
                if task.get("status") == TaskState.READY.value and not project.cancelled
            ]
            if not ready:
                break
            priority = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            ready.sort(key=lambda row: (priority.get(str(row.get("priority")), 2), str(row["id"])))
            parallel = [row for row in ready if row.get("parallel_safe")][:bound]
            if len(parallel) >= 2:
                batch = parallel
            else:
                batch = [ready[0]]
            completed = await asyncio.gather(
                *(self.run_task(project.id, str(row["id"]), actor=actor) for row in batch)
            )
            results.extend(completed)
            if not any(
                row.get("status") in {"completed", "needs_review", "failed"}
                for row in completed
            ):
                break
        return {
            "tasks": results,
            "completed": sum(1 for row in results if row.get("status") == "completed"),
            "needs_review": sum(1 for row in results if row.get("status") == "needs_review"),
            "failed": sum(1 for row in results if row.get("status") == "failed"),
            "remaining": sum(
                1
                for row in project.tasks()
                if row.get("status") not in {"completed", "cancelled"}
            ),
            "cancelled": project.cancelled,
            "bounded": True,
        }

    def record_task_result(
        self,
        product_id: str,
        task_id: str,
        *,
        verified: bool,
        summary: str,
        evidence: Sequence[str],
        files: Mapping[str, str] | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        if not project.development_started_at:
            raise ProductConflict("Start development before recording a task result.")
        task = project.task(task_id)
        if task is None:
            raise ProductConflict(f"Unknown task: {task_id}")
        if task.get("status") in {TaskState.COMPLETED.value, TaskState.CANCELLED.value}:
            raise ProductConflict("A completed or cancelled task cannot be overwritten.")
        if not verified:
            raise ProductValidationError(
                "A recorded task result must carry verified evidence; otherwise use retry."
            )
        if not [item for item in evidence if str(item).strip()]:
            raise ProductValidationError("A verified task result needs evidence.")
        if len(project.tests) > self.max_tests:
            raise BudgetExceeded("The test evidence budget is exhausted.")
        actor_name = _text(actor) or self.actor
        outcome = TaskOutcome(
            verified=True,
            summary=summary,
            evidence=tuple(str(item) for item in evidence),
            files=dict(files or {}),
        )
        task["status"] = TaskState.RUNNING.value
        task["attempts"] = int(task.get("attempts", 0)) + 1
        task["updated_at"] = self._clock().isoformat()
        self._apply_task_outcome(project, task, outcome, actor=actor_name)
        self._event(
            project,
            "task_result_recorded",
            actor=actor_name,
            detail={"task_id": task_id, "status": task["status"]},
        )
        self._persist()
        return task

    def retry_task(self, product_id: str, task_id: str, *, actor: str = "") -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        task = project.task(task_id)
        if task is None:
            raise ProductConflict(f"Unknown task: {task_id}")
        if task.get("status") not in {
            TaskState.FAILED.value,
            TaskState.NEEDS_REVIEW.value,
            TaskState.BLOCKED.value,
            TaskState.CANCELLED.value,
        }:
            raise ProductConflict("Only a failed, blocked, cancelled or review task can be retried.")
        if int(task.get("attempts", 0)) >= self.max_task_attempts:
            raise BudgetExceeded(
                f"Task {task_id} reached its attempt budget ({self.max_task_attempts})."
            )
        task["status"] = TaskState.PENDING.value
        task["last_error"] = ""
        task["updated_at"] = self._clock().isoformat()
        self._refresh_task_readiness(project)
        self._event(
            project,
            "task_retried",
            actor=_text(actor) or self.actor,
            detail={"task_id": task_id, "attempts": task.get("attempts")},
        )
        self._persist()
        return task

    # -- environment, workspace and build ------------------------------------

    def _build_environment(self, supplied: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(supplied or {})
        self._assert_no_secrets(data, "Environment configuration")
        env = {
            "language": _text(data.get("language")) or "python",
            "runtime": _text(data.get("runtime")),
            "dependencies": [str(item) for item in data.get("dependencies") or []]
            if isinstance(data.get("dependencies"), (list, tuple))
            else [],
            "build_command": _text(data.get("build_command")),
            "test_command": _text(data.get("test_command")),
            "run_command": _text(data.get("run_command")),
            "services": [str(item) for item in data.get("services") or []]
            if isinstance(data.get("services"), (list, tuple))
            else [],
            "configuration": dict(data.get("configuration") or {})
            if isinstance(data.get("configuration"), Mapping)
            else {},
            "notes": _text(data.get("notes")),
            "validated": False,
            "validation_scope": "",
            "validation_output": "",
            "validated_at": "",
            "secrets_policy": "environment variables only; never in source or reports",
        }
        return env

    async def validate_environment(self, product_id: str, *, actor: str = "") -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        if self._environment_probe is None:
            project.environment["validated"] = False
            project.environment["validation_scope"] = "unavailable"
            project.environment["validation_output"] = (
                "No environment probe is configured; the environment is not validated."
            )
            self._event(
                project,
                "environment_validation_failed",
                actor=_text(actor) or self.actor,
                detail={"validated": False},
            )
            self._persist()
            return dict(project.environment)
        try:
            probe_input = dict(project.environment)
            probe_input["workspace_path"] = project.workspace_path
            result = await _maybe_await(self._environment_probe(probe_input))
        except Exception as exc:  # noqa: BLE001 - a failed probe is reported, not hidden
            result = {"validated": False, "scope": "probe_error", "output": str(exc)}
        project.environment["validated"] = bool(result.get("validated"))
        project.environment["validation_scope"] = _text(result.get("scope"))
        project.environment["validation_output"] = redact_secrets(
            _text(result.get("output"))
        )[:4000]
        project.environment["validated_at"] = self._clock().isoformat()
        self._event(
            project,
            "environment_validated" if project.environment["validated"] else "environment_validation_failed",
            actor=_text(actor) or self.actor,
            detail={
                "validated": project.environment["validated"],
                "scope": project.environment["validation_scope"],
            },
        )
        self._persist()
        return dict(project.environment)

    def write_generated_file(
        self,
        product_id: str,
        *,
        path: str,
        content: str,
        expected_sha256: str = "",
        overwrite: bool = False,
        actor: str = "",
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        try:
            written = self._workspace.write(
                project.id,
                path,
                content,
                expected_sha256=expected_sha256,
                overwrite=overwrite,
            )
        except WorkspaceError as exc:
            raise ProductValidationError(str(exc)) from exc
        project.generated_files[written.path] = written.sha256
        self._event(
            project,
            "code_written",
            actor=_text(actor) or self.actor,
            detail={
                "path": written.path,
                "bytes": written.bytes,
                "overwritten": written.overwritten,
            },
        )
        self._persist()
        return written.to_dict()

    def cleanup_workspace(
        self, product_id: str, *, approved: bool, actor: str = ""
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_not_archived(project)
        if not approved:
            raise ApprovalRequired("Workspace cleanup requires explicit approval.")
        result = self._workspace.cleanup(project.id, approved=True)
        project.generated_files = {}
        self._event(
            project,
            "workspace_cleaned",
            actor=_text(actor) or self.actor,
            detail={"removed": result.get("removed")},
        )
        self._persist()
        return result

    def record_build(
        self,
        product_id: str,
        *,
        command: str,
        exit_code: int,
        output: str,
        validated: bool,
        actor: str = "",
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        try:
            record = record_build_result(
                project,
                command=command,
                exit_code=exit_code,
                output=output,
                validated=validated,
                actor=_text(actor) or self.actor,
            )
        except ValueError as exc:
            raise ProductValidationError(str(exc)) from exc
        self._event(
            project,
            "build_completed" if validated else "build_failed",
            actor=_text(actor) or self.actor,
            detail={"exit_code": record["exit_code"], "validated": validated},
        )
        self._persist()
        return record

    async def run_build(
        self, product_id: str, *, actor: str = "", timeout_seconds: float = 180.0
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        if self._build_runner is None:
            raise ProductConflict("No build runner is configured; record a real build result instead.")
        command = _text(project.environment.get("build_command"))
        if not command:
            raise ProductValidationError("Set a build command in the environment first.")
        result = await _maybe_await(
            self._build_runner(
                product_id=project.id,
                workspace_path=project.workspace_path,
                command=command,
                timeout_seconds=timeout_seconds,
            )
        )
        if not isinstance(result, Mapping):
            raise ProductConflict("The build runner returned no structured result.")
        return self.record_build(
            product_id,
            command=str(result.get("command") or command),
            exit_code=int(result.get("exit_code", 1)),
            output=str(result.get("output", "")),
            validated=bool(result.get("validated", False)),
            actor=actor,
        )

    # -- quality --------------------------------------------------------------

    def record_test(
        self, product_id: str, payload: Mapping[str, Any], *, actor: str = ""
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        try:
            result = record_test_result(
                project,
                payload,
                actor=_text(actor) or self.actor,
                max_tests=self.max_tests,
            )
        except ValueError as exc:
            raise ProductValidationError(str(exc)) from exc
        self._event(
            project,
            "test_passed" if result["status"] == TestStatus.PASSED.value else "test_failed",
            actor=_text(actor) or self.actor,
            detail={
                "gate": result["gate"],
                "kind": result["kind"],
                "status": result["status"],
                "total": result["total"],
                "failed": result["failed"],
            },
        )
        self._persist()
        return result

    async def run_quality_checks(
        self,
        product_id: str,
        *,
        gates: Sequence[str] | None = None,
        actor: str = "",
        timeout_seconds: float = 180.0,
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        if self._test_runner is None:
            raise ProductConflict(
                "No test runner is configured. Record actual test evidence or configure a runner."
            )
        wanted = [str(item).strip().lower() for item in gates or [] if str(item).strip()]
        selected = [
            gate
            for gate in project.quality_gates
            if not wanted or str(gate["id"]) in wanted
        ]
        if wanted and len(selected) != len(set(wanted)):
            raise ProductValidationError("One or more requested quality gates do not exist.")
        for gate in selected:
            try:
                raw = await _maybe_await(
                    self._test_runner(
                        product_id=project.id,
                        workspace_path=project.workspace_path,
                        gate=dict(gate),
                        test_command=str(project.environment.get("test_command", "")),
                        timeout_seconds=timeout_seconds,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - report a runner failure as test error
                self.record_test(
                    product_id,
                    {
                        "gate": gate["id"],
                        "kind": gate["kinds"][0],
                        "suite": gate["name"],
                        "status": "error",
                        "total": 0,
                        "passed": 0,
                        "failed": 0,
                        "output": f"Test runner failed: {exc}",
                        "source": "test_runner",
                    },
                    actor=actor,
                )
                continue
            rows: list[Mapping[str, Any]]
            if isinstance(raw, Mapping) and isinstance(raw.get("results"), list):
                rows = [row for row in raw["results"] if isinstance(row, Mapping)]
            elif isinstance(raw, Mapping):
                rows = [raw]
            elif isinstance(raw, (list, tuple)):
                rows = [row for row in raw if isinstance(row, Mapping)]
            else:
                rows = []
            if not rows:
                self.record_test(
                    product_id,
                    {
                        "gate": gate["id"],
                        "kind": gate["kinds"][0],
                        "suite": gate["name"],
                        "status": "not_run",
                        "total": 0,
                        "passed": 0,
                        "failed": 0,
                        "output": "The test runner returned no result.",
                        "source": "test_runner",
                    },
                    actor=actor,
                )
                continue
            for row in rows:
                payload = dict(row)
                payload.setdefault("gate", gate["id"])
                payload.setdefault("kind", gate["kinds"][0])
                payload.setdefault("source", "test_runner")
                try:
                    self.record_test(product_id, payload, actor=actor)
                except ProductValidationError as exc:
                    self.record_test(
                        product_id,
                        {
                            "gate": gate["id"],
                            "kind": gate["kinds"][0],
                            "suite": gate["name"],
                            "status": "error",
                            "total": 0,
                            "passed": 0,
                            "failed": 0,
                            "output": f"Malformed runner result: {exc}",
                            "source": "test_runner",
                        },
                        actor=actor,
                    )
        return {
            **quality_snapshot(project),
            "tests": [row for row in project.tests if row.get("gate") in {gate["id"] for gate in selected}],
            "executed_at": self._clock().isoformat(),
            "test_runner_configured": True,
        }

    def open_defect(self, product_id: str, payload: Mapping[str, Any], *, actor: str = "") -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        try:
            defect = open_defect(
                project,
                payload,
                actor=_text(actor) or self.actor,
                max_defects=self.max_defects,
            )
        except ValueError as exc:
            raise ProductValidationError(str(exc)) from exc
        self._event(
            project,
            "defect_opened",
            actor=_text(actor) or self.actor,
            detail={
                "defect_id": defect["id"],
                "severity": defect["severity"],
                "requirement_id": defect.get("requirement_id", ""),
            },
        )
        self._persist()
        return defect

    def resolve_defect(
        self,
        product_id: str,
        defect_id: str,
        *,
        resolution: str,
        fix_task_id: str = "",
        test_result_ids: Sequence[str] = (),
        verified: bool = False,
        actor: str = "",
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        try:
            defect = resolve_defect(
                project,
                defect_id,
                actor=_text(actor) or self.actor,
                resolution=resolution,
                fix_task_id=fix_task_id,
                test_result_ids=test_result_ids,
                verified=verified,
            )
        except ValueError as exc:
            raise ProductValidationError(str(exc)) from exc
        self._event(
            project,
            "defect_resolved",
            actor=_text(actor) or self.actor,
            detail={
                "defect_id": defect_id,
                "status": defect["status"],
                "verified": verified,
            },
        )
        self._persist()
        return defect

    def reopen_defect(
        self, product_id: str, defect_id: str, *, reason: str, actor: str = ""
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        try:
            defect = reopen_defect(project, defect_id, actor=_text(actor) or self.actor, reason=reason)
        except ValueError as exc:
            raise ProductValidationError(str(exc)) from exc
        self._event(
            project,
            "defect_reopened",
            actor=_text(actor) or self.actor,
            detail={"defect_id": defect_id, "reason": reason},
        )
        self._persist()
        return defect

    def mark_requirement(
        self,
        product_id: str,
        requirement_id: str,
        *,
        passed: bool,
        evidence: Sequence[str],
        actor: str = "",
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        try:
            requirement = mark_requirement_verified(
                project,
                requirement_id,
                passed=passed,
                evidence=evidence,
                actor=_text(actor) or self.actor,
            )
        except ValueError as exc:
            raise ProductValidationError(str(exc)) from exc
        self._event(
            project,
            "requirement_verified" if passed else "requirement_unmet",
            actor=_text(actor) or self.actor,
            detail={"requirement_id": requirement_id, "evidence": len(evidence)},
        )
        self._persist()
        return requirement

    def create_corrective_tasks(
        self, product_id: str, *, actor: str = ""
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        plan = project.plan
        if plan is None:
            raise ProductConflict("Create a development plan before creating corrective tasks.")
        if project.iterations >= self.max_iterations:
            raise BudgetExceeded(
                f"The product iteration budget ({self.max_iterations}) is exhausted."
            )
        existing = {
            str(task.get("corrective_for"))
            for task in project.tasks()
            if task.get("status") not in {"completed", "cancelled"}
        }
        targets: list[tuple[str, str, str]] = []
        for gate in failing_gates(project):
            key = f"gate:{gate}"
            if key not in existing:
                targets.append((key, f"Repair failing quality gate: {gate}", AgentRole.CODING.value))
        for defect in blocking_defects(project):
            key = f"defect:{defect['id']}"
            if key not in existing:
                targets.append((key, f"Fix defect: {defect['title']}", AgentRole.DEBUG.value))
        for requirement in project.requirements():
            if requirement.get("status") == RequirementStatus.UNMET.value:
                key = f"requirement:{requirement['id']}"
                if key not in existing:
                    targets.append(
                        (
                            key,
                            f"Satisfy unmet requirement: {requirement['title']}",
                            AgentRole.CODING.value,
                        )
                    )
        if not targets:
            return {"created": [], "iterations": project.iterations, "reason": "No failing gate, blocking defect or unmet requirement."}
        if len(project.tasks()) + len(targets) > self.max_tasks:
            raise BudgetExceeded(f"The task budget ({self.max_tasks}) is exhausted.")
        assigned = self._agent_by_role()
        now = self._clock().isoformat()
        created: list[dict[str, Any]] = []
        existing_ids = {str(row["id"]) for row in project.tasks()}
        for key, title, role in targets:
            sequence = len(project.tasks()) + 1
            task_id = f"TASK-{sequence:03d}"
            while task_id in existing_ids:
                sequence += 1
                task_id = f"TASK-{sequence:03d}"
            existing_ids.add(task_id)
            task = {
                "id": task_id,
                "title": title,
                "description": (
                    f"Corrective work for {key}. Re-run the relevant quality gate and "
                    "record evidence before resolving."
                ),
                "role": role,
                "priority": "high" if key.startswith(("defect:", "gate:security")) else "medium",
                "estimate_hours": None,
                "estimate_basis": "not estimated",
                "estimate_confidence": "unknown",
                "dependencies": [],
                "milestone_id": "MS-2",
                "parallel_safe": False,
                "requirement_ids": [key.split(":", 1)[1]]
                if key.startswith("requirement:")
                else [],
                "assigned_agent": assigned.get(role, ""),
                "status": "ready",
                "attempts": 0,
                "result": "",
                "evidence": [],
                "files_written": [],
                "last_error": "",
                "created_at": now,
                "updated_at": now,
                "corrective_for": key,
            }
            plan["tasks"].append(task)
            created.append(task)
        project.iterations += 1
        if project.plan is not None:
            project.plan["status"] = "draft"
        self._event(
            project,
            "corrective_tasks_created",
            actor=_text(actor) or self.actor,
            detail={"created": len(created), "iteration": project.iterations},
        )
        self._persist()
        return {"created": created, "iterations": project.iterations}

    # -- release and launch ---------------------------------------------------

    def prepare_release(
        self, product_id: str, inputs: Mapping[str, Any] | None = None, *, actor: str = ""
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        self._assert_no_secrets(inputs or {}, "Release input")
        try:
            release = prepare_release(
                project,
                self._workspace,
                inputs or {},
                actor=_text(actor) or self.actor,
            )
        except ValueError as exc:
            raise QualityGateError(str(exc)) from exc
        self._event(
            project,
            "release_prepared",
            actor=_text(actor) or self.actor,
            detail={
                "release_id": release["id"],
                "version": release["version"],
                "target": release["target"],
                "artifact_bytes": release["artifact"]["bytes"],
                "external_deployment_performed": False,
            },
        )
        self._persist()
        return release

    def approve_release(
        self,
        product_id: str,
        *,
        release_id: str,
        approved: bool,
        actor: str,
        note: str,
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        actor_name = _text(actor)
        try:
            release = approve_release(
                project,
                release_id=release_id,
                approved=approved,
                actor=actor_name,
                note=note,
            )
        except ValueError as exc:
            raise QualityGateError(str(exc)) from exc
        self._approval(
            project,
            ApprovalKind.RELEASE,
            target_id=release["id"],
            approved=approved,
            actor=actor_name,
            note=note,
            extra={"version": release.get("version")},
        )
        self._event(
            project,
            "release_approved" if approved else "release_rejected",
            actor=actor_name,
            detail={"release_id": release["id"], "version": release.get("version")},
        )
        self._persist()
        return release

    def evaluate_launch(self, product_id: str, *, actor: str = "") -> dict[str, Any]:
        project = self._get(product_id)
        readiness = evaluate_launch(project, actor=_text(actor) or self.actor)
        self._event(
            project,
            "launch_readiness_evaluated",
            actor=_text(actor) or self.actor,
            detail={
                "status": readiness["status"],
                "score": readiness["score"],
                "blockers": len(readiness["blockers"]),
                "external_launch_executed": False,
            },
        )
        self._persist()
        return readiness

    def approve_launch(
        self, product_id: str, *, approved: bool, actor: str, note: str
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_not_archived(project)
        actor_name = _text(actor)
        try:
            readiness = approve_launch(
                project, approved=approved, actor=actor_name, note=note
            )
        except ValueError as exc:
            raise ApprovalRequired(str(exc), approval_kind="launch") from exc
        self._approval(
            project,
            ApprovalKind.LAUNCH,
            target_id=readiness.get("release_id", ""),
            approved=approved,
            actor=actor_name,
            note=note,
        )
        self._event(
            project,
            "launch_approved" if approved else "launch_rejected",
            actor=actor_name,
            detail={
                "status": readiness["status"],
                "automated_recommendation": False,
                "external_launch_executed": False,
            },
        )
        self._persist()
        return readiness

    def resolve_condition(
        self,
        product_id: str,
        *,
        condition: str,
        evidence: Sequence[str],
        actor: str = "",
    ) -> dict[str, Any]:
        project = self._get(product_id)
        self._require_active(project)
        text = _text(condition)
        if text not in project.conditions:
            raise ProductValidationError("That condition is not recorded on this opportunity.")
        if not [item for item in evidence if str(item).strip()]:
            raise ProductValidationError("Resolving an opportunity condition needs evidence.")
        row = {
            "condition": text,
            "evidence": [str(item) for item in evidence],
            "actor": _text(actor) or self.actor,
            "resolved": True,
            "at": self._clock().isoformat(),
        }
        project.condition_resolutions.append(row)
        self._event(
            project,
            "opportunity_condition_resolved",
            actor=_text(actor) or self.actor,
            detail={"condition": text, "evidence": len(evidence)},
        )
        self._persist()
        return row

    # -- lifecycle ------------------------------------------------------------

    def cancel(self, product_id: str, *, reason: str, actor: str = "") -> dict[str, Any]:
        project = self._get(product_id)
        if project.archived_at:
            raise ProductConflict("An archived product cannot be cancelled.")
        if project.cancelled:
            return project.summary()
        project.cancelled_at = self._clock()
        project.cancellation_reason = _text(reason) or "Cancelled by operator."
        for task in project.tasks():
            if task.get("status") in {
                TaskState.PENDING.value,
                TaskState.READY.value,
                TaskState.RUNNING.value,
                TaskState.BLOCKED.value,
            }:
                task["status"] = TaskState.CANCELLED.value
                task["updated_at"] = self._clock().isoformat()
        project.state = ProductState.BLOCKED.value
        self._event(
            project,
            "product_cancelled",
            actor=_text(actor) or self.actor,
            detail={"reason": project.cancellation_reason},
        )
        self._persist()
        return project.summary()

    def resume(self, product_id: str, *, actor: str = "") -> dict[str, Any]:
        project = self._get(product_id)
        if project.archived_at:
            raise ProductConflict("An archived product cannot be resumed.")
        if not project.cancelled:
            return project.summary()
        project.cancelled_at = None
        project.cancellation_reason = ""
        project.state = ProductState.DEVELOPMENT.value
        self._refresh_task_readiness(project)
        self._event(
            project,
            "product_resumed",
            actor=_text(actor) or self.actor,
            detail={"note": "Cancelled tasks remain cancelled until individually retried."},
        )
        self._persist()
        return project.summary()

    def archive(self, product_id: str, *, reason: str = "", actor: str = "") -> dict[str, Any]:
        project = self._get(product_id)
        if project.archived_at:
            return project.summary()
        project.archived_at = self._clock()
        project.state = ProductState.ARCHIVED.value
        self._event(
            project,
            "product_archived",
            actor=_text(actor) or self.actor,
            detail={"reason": _text(reason)},
        )
        self._persist()
        return project.summary()

    def history(self, product_id: str) -> dict[str, Any]:
        project = self._get(product_id)
        return {
            "product_id": project.id,
            "events": list(project.events),
            "approvals": list(project.approvals),
            "prd_history": list(project.prd_history),
            "architecture_history": list(project.architecture_history),
            "condition_resolutions": list(project.condition_resolutions),
        }

    def status(self) -> dict[str, Any]:
        self._load()
        states: dict[str, int] = {}
        for project in self._projects.values():
            states[project.state] = states.get(project.state, 0) + 1
        return {
            "service": "product_pipeline",
            "namespace": NAMESPACE,
            "schema_version": SCHEMA_VERSION,
            "products": len(self._projects),
            "by_state": states,
            "configured": {
                "opportunity_loader": self._opportunities is not None,
                "agent_registry": self._registry is not None,
                "task_executor": self._task_executor is not None,
                "test_runner": self._test_runner is not None,
                "build_runner": self._build_runner is not None,
                "environment_probe": self._environment_probe is not None,
            },
            "limits": {
                "max_products": self.max_products,
                "max_tasks_per_product": self.max_tasks,
                "max_tests_per_product": self.max_tests,
                "max_defects_per_product": self.max_defects,
                "max_events_per_product": self.max_events,
                "max_parallel_tasks": self.max_parallel,
                "max_task_attempts": self.max_task_attempts,
                "max_iterations": self.max_iterations,
                "max_workspace_files": self._workspace.max_files,
                "max_file_bytes": self._workspace.max_file_bytes,
            },
            "workspace_root": str(self._workspace.root),
            "resource_snapshot": self._resource_read(),
            "boundaries": {
                "external_launch_executed": False,
                "paid_campaign_executed": False,
                "commit_or_push_executed": False,
                "external_deployment_executed": False,
                "human_approval_required_for_launch": True,
            },
            "responsibilities": (
                "validated_opportunity_intake",
                "product_requirements",
                "product_planning",
                "architecture_and_adr",
                "development_orchestration",
                "environment_validation",
                "quality_assurance",
                "defect_management",
                "release_preparation",
                "launch_readiness",
            ),
            "load_errors": list(self._load_errors),
        }


__all__ = ["NAMESPACE", "SCHEMA_VERSION", "ProductService"]
