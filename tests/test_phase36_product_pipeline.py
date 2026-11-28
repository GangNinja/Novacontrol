"""Phase 36 — product development and launch pipeline tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from novacontrol.product import (
    ApprovalRequired,
    OpportunityNotEligible,
    ProductConflict,
    ProductService,
    ProductState,
    ProductValidationError,
    QualityGateError,
    TaskOutcome,
)
from novacontrol.product.planning import _critical_path


class MemoryStore:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def read(self, namespace: str) -> dict[str, Any]:
        return copy.deepcopy(self.data.get(namespace, {}))

    def write(self, namespace: str, payload: dict[str, Any]) -> None:
        self.data[namespace] = copy.deepcopy(payload)


def opportunity_detail(
    *,
    verdict: str = "validated",
    stage: str = "validated",
    conditions: list[str] | None = None,
    opportunity_id: str = "opp-1",
) -> dict[str, Any]:
    report = {
        "verdict": verdict,
        "rationale": "Recorded customer interviews and sourced market evidence.",
        "evidence_gaps": list(conditions or []),
        "limitations": [],
        "next_steps": [],
        "sections": [],
        "source_count": 4,
        "primary_count": 2,
        "stale_count": 0,
        "version": 3,
    }
    return {
        "id": opportunity_id,
        "title": "Invoice follow-up assistant",
        "description": "A local assistant that drafts invoice reminders.",
        "customer_problem": "Small agencies forget to follow up overdue invoices.",
        "proposed_solution": "A local assistant that drafts and schedules reminders.",
        "target_audience": "Small design agencies",
        "industry": "Professional services",
        "category": "automation",
        "stage": stage,
        "report": report,
        "score": {"weighted_score": 0.78, "evidence_coverage": 0.7, "missing": []},
        "evidence": [{"id": "ev-1"}],
        "source_refs": [{"title": "Interview notes"}],
        "uncertainty": [],
        "tags": [],
    }


async def passing_runner(**kwargs: Any) -> dict[str, Any]:
    gate = kwargs["gate"]
    return {
        "suite": gate["name"],
        "kind": gate["kinds"][0],
        "status": "passed",
        "total": 3,
        "passed": 3,
        "failed": 0,
        "coverage": 0.82,
        "command": "python -m pytest",
        "output": "3 passed",
    }


async def failing_runner(**kwargs: Any) -> dict[str, Any]:
    gate = kwargs["gate"]
    return {
        "suite": gate["name"],
        "kind": gate["kinds"][0],
        "status": "failed",
        "total": 3,
        "passed": 2,
        "failed": 1,
        "command": "python -m pytest",
        "output": "1 failed",
    }


async def verified_executor(task: Any) -> TaskOutcome:
    files = {"src/app.py": "def run():\n    return 'ok'\n"} if task.id == "TASK-001" else {}
    return TaskOutcome(
        verified=True,
        summary=f"Implemented {task.title}",
        evidence=("unit output: 3 passed",),
        files=files,
        agent="test-agent",
    )


def prd_inputs() -> dict[str, Any]:
    return {
        "vision": "Make invoice follow-up reliable for small agencies.",
        "problem_statement": "Agencies lose cash because reminders are manual.",
        "target_users": ["Small design agencies"],
        "personas": ["Owner-operator who invoices monthly"],
        "user_journeys": ["Review overdue invoices, approve a reminder, see it scheduled."],
        "functional_requirements": [
            {
                "title": "Draft a reminder",
                "description": "Draft a reminder for an overdue invoice.",
                "priority": "high",
                "acceptance_criteria": ["A draft is shown before anything is sent."],
            }
        ],
        "non_functional_requirements": ["Reminder drafts are produced locally."],
        "core_features": ["Invoice queue"],
        "optional_features": ["Tone presets"],
        "constraints": ["No autonomous email sending"],
        "dependencies": ["Local invoice data"],
        "security_requirements": ["Do not store provider credentials in source."],
        "performance_requirements": ["Draft generation under two seconds locally."],
        "accessibility_requirements": ["Keyboard-only review workflow."],
        "acceptance_criteria": ["A user can review and reject a draft."],
        "success_metrics": ["At least 80% of overdue invoices reviewed weekly."],
        "explicit_exclusions": ["No payment processing."],
    }


def architecture_inputs() -> dict[str, Any]:
    return {
        "summary": "A local application with a small API and SQLite store.",
        "components": [
            {
                "id": "COMP-001",
                "title": "Reminder service",
                "responsibility": "Draft reminders from invoice records.",
                "requirement_ids": ["REQ-001"],
            }
        ],
        "api_contracts": [{"title": "POST /draft", "request": "invoice_id", "response": "draft"}],
        "database_design": {"engine": "sqlite", "tables": ["invoices", "drafts"]},
        "data_flows": [{"title": "invoice to draft", "steps": ["read invoice", "render draft"]}],
        "integrations": [{"title": "Local invoice store"}],
        "technology": {"language": "python", "ui": "local web"},
        "deployment": {"target": "local", "process": "single process"},
        "security": {"auth": "local only", "secrets": "environment"},
        "scalability": {"limit": "single agency workspace"},
        "observability": {"logs": "structured local logs"},
        "decisions": [
            {
                "id": "ADR-001",
                "title": "Use a local SQLite store",
                "decision": "Use SQLite for the first release.",
                "rationale": "The product is local-first and single-user.",
                "alternatives": ["Hosted Postgres"],
                "major": True,
            }
        ],
    }


def plan_inputs() -> dict[str, Any]:
    return {
        "tasks": [
            {
                "id": "TASK-001",
                "title": "Implement reminder draft endpoint",
                "role": "coding",
                "requirement_ids": ["REQ-001"],
                "parallel_safe": True,
                "estimate_hours": 4,
            },
            {
                "id": "TASK-002",
                "title": "Implement local invoice queue",
                "role": "coding",
                "requirement_ids": ["REQ-001"],
                "parallel_safe": True,
                "estimate_hours": 3,
            },
            {
                "id": "TASK-003",
                "title": "Verify reminder acceptance",
                "role": "testing",
                "requirement_ids": ["REQ-001"],
                "dependencies": ["TASK-001", "TASK-002"],
                "parallel_safe": True,
                "estimate_hours": 2,
            },
        ],
        "milestones": [
            {"id": "MS-1", "title": "Implementation", "task_ids": ["TASK-001", "TASK-002"]},
            {"id": "MS-2", "title": "Quality", "task_ids": ["TASK-003"]},
        ],
    }


def make_service(
    tmp_path: Path,
    *,
    detail: dict[str, Any] | None = None,
    runner: Any = passing_runner,
    executor: Any = verified_executor,
    store: MemoryStore | None = None,
) -> tuple[ProductService, MemoryStore]:
    state = store or MemoryStore()
    opportunity = detail or opportunity_detail()
    service = ProductService(
        store=state,
        opportunities=lambda opportunity_id: opportunity,
        workspace_root=tmp_path / "products",
        task_executor=executor,
        test_runner=runner,
        environment_probe=lambda env: {
            "validated": True,
            "scope": "runtime_and_workspace",
            "output": "Python runtime and isolated workspace are available.",
        },
        resource_snapshot=lambda: {"available_ram_gb": 8.0, "cpu_percent": 20},
        actor="operator",
    )
    return service, state


async def ready_project(service: ProductService) -> dict[str, Any]:
    created = await service.create_project(
        opportunity_id="opp-1",
        name="Invoice Assistant",
        environment={
            "language": "python",
            "build_command": "python -m compileall src",
            "test_command": "python -m pytest",
        },
    )
    product_id = created["id"]
    service.generate_prd(product_id, prd_inputs(), actor="operator")
    service.approve_requirements(
        product_id, approved=True, actor="operator", note="Requirements reviewed."
    )
    service.generate_architecture(product_id, architecture_inputs(), actor="operator")
    service.approve_architecture(
        product_id, approved=True, actor="operator", note="Architecture reviewed."
    )
    service.create_plan(product_id, plan_inputs(), actor="operator")
    service.start_development(product_id, actor="operator")
    await service.run_ready_tasks(product_id, actor="operator")
    await service.validate_environment(product_id, actor="operator")
    service.record_build(
        product_id,
        command="python -m compileall src",
        exit_code=0,
        output="compiled",
        validated=True,
        actor="operator",
    )
    await service.run_quality_checks(product_id, actor="operator")
    detail = service.detail(product_id)
    for requirement in detail["prd"]["requirements"]:
        if requirement["kind"] in {
            "functional",
            "core_feature",
            "non_functional",
            "security",
            "performance",
            "accessibility",
        }:
            service.mark_requirement(
                product_id,
                requirement["id"],
                passed=True,
                evidence=["acceptance test evidence"],
                actor="operator",
            )
    return service.detail(product_id)


@pytest.mark.asyncio
async def test_opportunity_intake_accepts_validated_and_retains_reference(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    product = await service.create_project(opportunity_id="opp-1", name="Invoice Assistant")
    assert product["opportunity_id"] == "opp-1"
    assert product["validation_state"] == "VALIDATED"
    assert product["validation_report"]["version"] == 3
    assert product["workspace_path"].endswith(product["id"])
    assert product["state"] == ProductState.DEVELOPMENT.value


@pytest.mark.asyncio
async def test_opportunity_intake_rejects_unvalidated_states(tmp_path: Path) -> None:
    for verdict in ("insufficient_evidence", "not_viable", "requires_further_research"):
        service, _ = make_service(
            tmp_path / verdict,
            detail=opportunity_detail(verdict=verdict, stage=verdict),
        )
        with pytest.raises(OpportunityNotEligible):
            await service.create_project(opportunity_id="opp-1")


@pytest.mark.asyncio
async def test_conditionally_validated_requires_named_approval_and_conditions(tmp_path: Path) -> None:
    detail = opportunity_detail(
        verdict="conditionally_validated",
        stage="conditionally_validated",
        conditions=["Two more customer interviews are required."],
    )
    service, _ = make_service(tmp_path, detail=detail)
    with pytest.raises(OpportunityNotEligible):
        await service.create_project(opportunity_id="opp-1")
    with pytest.raises(OpportunityNotEligible):
        await service.create_project(opportunity_id="opp-1", approved=True, actor="")
    with pytest.raises(OpportunityNotEligible):
        await service.create_project(opportunity_id="opp-1", approved=True, actor="owner")
    product = await service.create_project(
        opportunity_id="opp-1",
        approved=True,
        actor="owner",
        approval_note="Proceed while the two interviews are scheduled.",
    )
    assert product["conditions"] == ["Two more customer interviews are required."]
    assert product["condition_approval"]["approved"] is True
    assert product["condition_approval"]["actor"] == "owner"


@pytest.mark.asyncio
async def test_duplicate_product_and_unknown_product_are_conflicts(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    await service.create_project(opportunity_id="opp-1")
    with pytest.raises(ProductConflict):
        await service.create_project(opportunity_id="opp-1")
    with pytest.raises(Exception):
        service.detail("missing-product")


@pytest.mark.asyncio
async def test_prd_tracks_missing_information_and_revision_history(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    product = await service.create_project(opportunity_id="opp-1")
    prd = service.generate_prd(
        product["id"],
        {"vision": "Make reminders reliable", "functional_requirements": ["Draft a reminder"]},
        actor="operator",
    )
    assert prd["missing_information"]
    assert prd["traceability"]["opportunity_id"] == "opp-1"
    assert prd["requirements"][0]["source"].startswith("operator")
    service.generate_prd(product["id"], prd_inputs(), actor="operator")
    detail = service.detail(product["id"])
    assert detail["prd"]["version"] == 2
    assert len(detail["prd_history"]) == 1


@pytest.mark.asyncio
async def test_requirements_approval_is_human_and_required_for_architecture(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    product = await service.create_project(opportunity_id="opp-1")
    with pytest.raises(ProductValidationError):
        service.generate_architecture(product["id"], architecture_inputs())
    service.generate_prd(product["id"], prd_inputs(), actor="operator")
    with pytest.raises(ProductValidationError):
        service.approve_requirements(product["id"], approved=True, actor="", note="")
    with pytest.raises(ProductValidationError):
        service.approve_requirements(product["id"], approved=True, actor="owner", note="")
    approved = service.approve_requirements(
        product["id"], approved=True, actor="owner", note="Reviewed and approved."
    )
    assert approved["status"] == "approved"


@pytest.mark.asyncio
async def test_architecture_requires_approval_before_development(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    product = await service.create_project(opportunity_id="opp-1")
    service.generate_prd(product["id"], prd_inputs(), actor="operator")
    service.approve_requirements(
        product["id"], approved=True, actor="owner", note="Reviewed."
    )
    architecture = service.generate_architecture(
        product["id"], architecture_inputs(), actor="operator"
    )
    assert architecture["status"] == "draft"
    with pytest.raises(ApprovalRequired):
        service.start_development(product["id"])
    service.create_plan(product["id"], plan_inputs(), actor="operator")
    with pytest.raises(ApprovalRequired):
        service.start_development(product["id"])
    service.approve_architecture(
        product["id"], approved=True, actor="owner", note="Design approved."
    )
    service.start_development(product["id"], actor="owner")
    assert service.detail(product["id"])["development_started_at"]


@pytest.mark.asyncio
async def test_plan_has_dependencies_critical_path_and_explicit_estimates(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    product = await service.create_project(opportunity_id="opp-1")
    service.generate_prd(product["id"], prd_inputs(), actor="operator")
    service.approve_requirements(
        product["id"], approved=True, actor="owner", note="Reviewed."
    )
    plan = service.create_plan(product["id"], plan_inputs(), actor="owner")
    assert plan["critical_path"]
    assert plan["tasks"][2]["dependencies"] == ["TASK-001", "TASK-002"]
    assert plan["tasks"][0]["estimate_basis"] == "operator_supplied"
    assert plan["delivery_plan"]["calendar_dates_committed"] is False
    assert "estimate" in plan["estimate_disclaimer"].lower()


def test_critical_path_includes_terminal_duration_and_zero_estimate_depth() -> None:
    tasks = [
        {"id": "A", "estimate_hours": 1, "dependencies": []},
        {"id": "B", "estimate_hours": 1, "dependencies": ["A"]},
        {"id": "C", "estimate_hours": 100, "dependencies": []},
    ]
    assert _critical_path(tasks) == ["C"]
    unestimated = [
        {"id": "A", "estimate_hours": None, "dependencies": []},
        {"id": "B", "estimate_hours": None, "dependencies": ["A"]},
        {"id": "C", "estimate_hours": None, "dependencies": ["B"]},
    ]
    assert _critical_path(unestimated) == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_parallel_ready_tasks_execute_and_write_only_in_workspace(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    product = await service.create_project(opportunity_id="opp-1")
    service.generate_prd(product["id"], prd_inputs(), actor="operator")
    service.approve_requirements(
        product["id"], approved=True, actor="owner", note="Reviewed."
    )
    service.generate_architecture(product["id"], architecture_inputs(), actor="operator")
    service.approve_architecture(
        product["id"], approved=True, actor="owner", note="Reviewed."
    )
    service.create_plan(product["id"], plan_inputs(), actor="owner")
    service.start_development(product["id"], actor="owner")
    result = await service.run_ready_tasks(product["id"], actor="owner")
    assert result["completed"] == 3
    assert result["remaining"] == 0
    detail = service.detail(product["id"])
    assert all(task["status"] == "completed" for task in detail["plan"]["tasks"])
    assert (Path(detail["workspace_path"]) / "src" / "app.py").is_file()
    assert detail["summary"]["tasks_completed"] == 3


@pytest.mark.asyncio
async def test_workspace_refuses_traversal_and_unreviewed_overwrite(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    product = await service.create_project(opportunity_id="opp-1")
    with pytest.raises(ProductValidationError):
        service.write_generated_file(product["id"], path="../escape.py", content="x = 1")
    first = service.write_generated_file(
        product["id"], path="src/app.py", content="x = 1\n"
    )
    assert first["bytes"] > 0
    with pytest.raises(ProductValidationError):
        service.write_generated_file(
            product["id"], path="src/app.py", content="x = 2\n"
        )
    second = service.write_generated_file(
        product["id"],
        path="src/app.py",
        content="x = 2\n",
        expected_sha256=first["sha256"],
        overwrite=True,
    )
    assert second["overwritten"] is True
    with pytest.raises(ApprovalRequired):
        service.cleanup_workspace(product["id"], approved=False)


@pytest.mark.asyncio
async def test_quality_gate_failure_blocks_release_and_launch(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, runner=failing_runner)
    product = await service.create_project(
        opportunity_id="opp-1",
        environment={"language": "python", "build_command": "python -m compileall src"},
    )
    service.generate_prd(product["id"], prd_inputs(), actor="operator")
    service.approve_requirements(
        product["id"], approved=True, actor="owner", note="Reviewed."
    )
    service.generate_architecture(product["id"], architecture_inputs(), actor="operator")
    service.approve_architecture(
        product["id"], approved=True, actor="owner", note="Reviewed."
    )
    service.create_plan(product["id"], plan_inputs(), actor="owner")
    service.start_development(product["id"], actor="owner")
    await service.run_ready_tasks(product["id"], actor="owner")
    await service.validate_environment(product["id"], actor="owner")
    service.record_build(
        product["id"],
        command="python -m compileall src",
        exit_code=0,
        output="compiled",
        validated=True,
        actor="owner",
    )
    await service.run_quality_checks(product["id"], actor="owner")
    detail = service.detail(product["id"])
    assert detail["quality"]["failing_required"]
    with pytest.raises(QualityGateError):
        service.prepare_release(product["id"], {"version": "1.0.0"}, actor="owner")
    readiness = service.evaluate_launch(product["id"], actor="owner")
    assert readiness["status"] != ProductState.APPROVED_FOR_LAUNCH.value
    assert readiness["blockers"] or readiness["warnings"]
    with pytest.raises(ApprovalRequired):
        service.approve_launch(
            product["id"], approved=True, actor="owner", note="Ship it."
        )


@pytest.mark.asyncio
async def test_defect_cannot_be_resolved_without_a_passing_test(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    product = await service.create_project(opportunity_id="opp-1")
    service.generate_prd(product["id"], prd_inputs(), actor="operator")
    service.approve_requirements(
        product["id"], approved=True, actor="owner", note="Reviewed."
    )
    service.generate_architecture(product["id"], architecture_inputs(), actor="operator")
    service.approve_architecture(
        product["id"], approved=True, actor="owner", note="Reviewed."
    )
    service.create_plan(product["id"], plan_inputs(), actor="owner")
    defect = service.open_defect(
        product["id"],
        {
            "title": "Draft is not keyboard reachable",
            "severity": "high",
            "requirement_id": "A11Y-001",
        },
        actor="owner",
    )
    service.record_test(
        product["id"],
        {
            "gate": "acceptance",
            "kind": "acceptance",
            "status": "failed",
            "total": 1,
            "passed": 0,
            "failed": 1,
        },
        actor="owner",
    )
    failed_test = service.detail(product["id"])["tests"][-1]
    with pytest.raises(ProductValidationError):
        service.resolve_defect(
            product["id"],
            defect["id"],
            resolution="Fixed focus order.",
            test_result_ids=[failed_test["id"]],
            actor="owner",
        )
    service.record_test(
        product["id"],
        {
            "gate": "acceptance",
            "kind": "acceptance",
            "status": "passed",
            "total": 1,
            "passed": 1,
            "failed": 0,
        },
        actor="owner",
    )
    passing = service.detail(product["id"])["tests"][-1]
    resolved = service.resolve_defect(
        product["id"],
        defect["id"],
        resolution="Fixed focus order and verified with acceptance test.",
        test_result_ids=[passing["id"]],
        verified=True,
        actor="owner",
    )
    assert resolved["status"] == "verified"


@pytest.mark.asyncio
async def test_release_artifact_tampering_blocks_approval(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    product = await ready_project(service)
    release = service.prepare_release(
        product["id"],
        {"version": "1.0.0", "operational": {"monitoring": "logs", "recovery": "bundle"}},
        actor="owner",
    )
    Path(release["artifact"]["path"]).write_bytes(b"tampered artifact")
    with pytest.raises(QualityGateError):
        service.approve_release(
            product["id"],
            release_id=release["id"],
            approved=True,
            actor="owner",
            note="Should not approve a changed artifact.",
        )


@pytest.mark.asyncio
async def test_release_and_launch_happy_path_requires_human_approval(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    product = await ready_project(service)
    assert product["quality"]["all_required_passed"] is True
    release = service.prepare_release(
        product["id"],
        {
            "version": "1.0.0",
            "target": "local",
            "operational": {"monitoring": "local logs", "recovery": "restore previous bundle"},
        },
        actor="owner",
    )
    assert release["artifact"]["external_deployment_performed"] is False
    readiness = service.evaluate_launch(product["id"], actor="owner")
    assert readiness["status"] == ProductState.READY_FOR_RELEASE_REVIEW.value
    with pytest.raises(ApprovalRequired):
        service.approve_launch(
            product["id"], approved=True, actor="owner", note="Ship it."
        )
    approved_release = service.approve_release(
        product["id"],
        release_id=release["id"],
        approved=True,
        actor="owner",
        note="Release reviewed.",
    )
    assert approved_release["status"] == "approved"
    readiness = service.evaluate_launch(product["id"], actor="owner")
    assert readiness["status"] == ProductState.READY_FOR_RELEASE_REVIEW.value
    launched = service.approve_launch(
        product["id"], approved=True, actor="owner", note="Launch approved by owner."
    )
    assert launched["status"] == ProductState.APPROVED_FOR_LAUNCH.value
    assert launched["external_launch_executed"] is False
    detail = service.detail(product["id"])
    assert detail["state"] == ProductState.APPROVED_FOR_LAUNCH.value
    assert detail["launch_approval"]["automated_recommendation"] is False
    assert detail["approvals"][-1]["kind"] == "launch"
    requirement = next(
        row
        for row in detail["prd"]["requirements"]
        if row["kind"] in {"functional", "core_feature", "non_functional"}
    )
    service.mark_requirement(
        product["id"], requirement["id"], passed=False, evidence=[], actor="owner"
    )
    invalidated = service.evaluate_launch(product["id"], actor="owner")
    assert invalidated["approval_valid"] is False
    assert invalidated["status"] == ProductState.BLOCKED.value


@pytest.mark.asyncio
async def test_launch_cannot_bypass_unverified_requirements(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    product = await ready_project(service)
    release = service.prepare_release(
        product["id"],
        {"version": "1.0.0", "operational": {"monitoring": "logs", "recovery": "bundle"}},
        actor="owner",
    )
    service.approve_release(
        product["id"], release_id=release["id"], approved=True, actor="owner", note="Approved."
    )
    detail = service.detail(product["id"])
    requirement = next(
        row
        for row in detail["prd"]["requirements"]
        if row["kind"] in {"functional", "core_feature", "non_functional"}
    )
    service.mark_requirement(
        product["id"], requirement["id"], passed=False, evidence=[], actor="owner"
    )
    readiness = service.evaluate_launch(product["id"], actor="owner")
    assert readiness["status"] == ProductState.READY_FOR_RELEASE_REVIEW.value
    with pytest.raises(ApprovalRequired):
        service.approve_launch(
            product["id"], approved=True, actor="owner", note="Bypass attempt."
        )


@pytest.mark.asyncio
async def test_launch_rejection_is_recorded_and_blocks_status(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    product = await ready_project(service)
    release = service.prepare_release(
        product["id"],
        {"version": "1.0.0", "operational": {"monitoring": "logs", "recovery": "bundle"}},
        actor="owner",
    )
    service.approve_release(
        product["id"], release_id=release["id"], approved=True, actor="owner", note="Approved."
    )
    readiness = service.approve_launch(
        product["id"], approved=False, actor="owner", note="Customer support is not ready."
    )
    assert readiness["status"] == ProductState.BLOCKED.value
    assert readiness["launch_approval"]["approved"] is False


@pytest.mark.asyncio
async def test_prd_revision_invalidates_stale_architecture_and_plan(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    product = await service.create_project(opportunity_id="opp-1")
    service.generate_prd(product["id"], prd_inputs(), actor="operator")
    service.approve_requirements(product["id"], approved=True, actor="owner", note="Reviewed.")
    service.generate_architecture(product["id"], architecture_inputs(), actor="operator")
    service.approve_architecture(product["id"], approved=True, actor="owner", note="Reviewed.")
    service.create_plan(product["id"], plan_inputs(), actor="owner")
    service.generate_prd(product["id"], prd_inputs(), actor="operator")
    service.approve_requirements(product["id"], approved=True, actor="owner", note="Revision approved.")
    with pytest.raises(ApprovalRequired):
        service.start_development(product["id"], actor="owner")


@pytest.mark.asyncio
async def test_task_result_requires_development_start(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    product = await service.create_project(opportunity_id="opp-1")
    service.generate_prd(product["id"], prd_inputs(), actor="operator")
    service.approve_requirements(product["id"], approved=True, actor="owner", note="Reviewed.")
    plan = service.create_plan(product["id"], plan_inputs(), actor="owner")
    with pytest.raises(ProductConflict):
        service.record_task_result(
            product["id"],
            plan["tasks"][0]["id"],
            verified=True,
            summary="Premature result",
            evidence=["fabricated"],
            actor="owner",
        )


@pytest.mark.asyncio
async def test_reapproving_requirements_preserves_verified_evidence(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    product = await service.create_project(opportunity_id="opp-1")
    service.generate_prd(product["id"], prd_inputs(), actor="operator")
    service.approve_requirements(
        product["id"], approved=True, actor="owner", note="Reviewed."
    )
    requirement = service.detail(product["id"])["prd"]["requirements"][0]
    service.mark_requirement(
        product["id"],
        requirement["id"],
        passed=True,
        evidence=["acceptance output"],
        actor="owner",
    )
    service.approve_requirements(
        product["id"], approved=True, actor="owner", note="Re-reviewed."
    )
    verified = service.detail(product["id"])["prd"]["requirements"][0]
    assert verified["status"] == "verified"
    assert verified["verification_evidence"] == ["acceptance output"]


@pytest.mark.asyncio
async def test_defect_budget_is_enforced_instead_of_silently_evicting(tmp_path: Path) -> None:
    service = ProductService(
        store=MemoryStore(),
        opportunities=lambda opportunity_id: opportunity_detail(),
        workspace_root=tmp_path / "products",
        max_defects=1,
    )
    product = await service.create_project(opportunity_id="opp-1")
    service.open_defect(product["id"], {"title": "First"}, actor="owner")
    with pytest.raises(ProductValidationError):
        service.open_defect(product["id"], {"title": "Second"}, actor="owner")
    assert len(service.detail(product["id"])["defects"]) == 1


@pytest.mark.asyncio
async def test_architecture_rejects_unknown_requirement_trace(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    product = await service.create_project(opportunity_id="opp-1")
    service.generate_prd(product["id"], prd_inputs(), actor="operator")
    service.approve_requirements(
        product["id"], approved=True, actor="owner", note="Reviewed."
    )
    inputs = copy.deepcopy(architecture_inputs())
    inputs["components"][0]["requirement_ids"] = ["REQ-999"]
    with pytest.raises(ProductValidationError):
        service.generate_architecture(product["id"], inputs, actor="owner")


@pytest.mark.asyncio
async def test_task_failure_recovery_is_bounded_and_no_false_completion(tmp_path: Path) -> None:
    calls = {"count": 0}

    async def flaky(task: Any) -> TaskOutcome:
        calls["count"] += 1
        return TaskOutcome(verified=False, summary="Agent did not verify the change.")

    service, _ = make_service(tmp_path, executor=flaky)
    product = await service.create_project(opportunity_id="opp-1")
    service.generate_prd(product["id"], prd_inputs(), actor="operator")
    service.approve_requirements(
        product["id"], approved=True, actor="owner", note="Reviewed."
    )
    service.generate_architecture(product["id"], architecture_inputs(), actor="operator")
    service.approve_architecture(
        product["id"], approved=True, actor="owner", note="Reviewed."
    )
    service.create_plan(product["id"], plan_inputs(), actor="owner")
    service.start_development(product["id"], actor="owner")
    result = await service.run_ready_tasks(product["id"], actor="owner")
    assert result["completed"] == 0
    assert result["needs_review"] >= 1
    task = service.detail(product["id"])["plan"]["tasks"][0]
    assert task["status"] == "needs_review"
    service.retry_task(product["id"], task["id"], actor="owner")
    await service.run_ready_tasks(product["id"], actor="owner")
    task = service.detail(product["id"])["plan"]["tasks"][0]
    assert task["status"] == "needs_review"
    with pytest.raises(Exception):
        service.retry_task(product["id"], task["id"], actor="owner")


@pytest.mark.asyncio
async def test_cancellation_stops_further_task_execution(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    product = await service.create_project(opportunity_id="opp-1")
    service.generate_prd(product["id"], prd_inputs(), actor="operator")
    service.approve_requirements(
        product["id"], approved=True, actor="owner", note="Reviewed."
    )
    service.generate_architecture(product["id"], architecture_inputs(), actor="operator")
    service.approve_architecture(
        product["id"], approved=True, actor="owner", note="Reviewed."
    )
    service.create_plan(product["id"], plan_inputs(), actor="owner")
    service.start_development(product["id"], actor="owner")
    cancelled = service.cancel(product["id"], reason="Scope changed.", actor="owner")
    assert cancelled["cancelled"] is True
    with pytest.raises(ProductConflict):
        await service.run_ready_tasks(product["id"], actor="owner")
    resumed = service.resume(product["id"], actor="owner")
    assert resumed["cancelled"] is False
    readiness = service.evaluate_launch(product["id"], actor="owner")
    assert readiness["status"] in {ProductState.DEVELOPMENT.value, ProductState.BLOCKED.value}
    development = next(row for row in readiness["checks"] if row["id"] == "development")
    assert development["status"] == "failed"


@pytest.mark.asyncio
async def test_persistence_restores_projects_and_revision_history(tmp_path: Path) -> None:
    store = MemoryStore()
    service, _ = make_service(tmp_path, store=store)
    product = await service.create_project(
        opportunity_id="opp-1", name="Invoice Assistant"
    )
    service.generate_prd(product["id"], prd_inputs(), actor="operator")
    second = ProductService(
        store=store,
        opportunities=lambda opportunity_id: opportunity_detail(),
        workspace_root=tmp_path / "products",
        actor="operator",
    )
    detail = second.detail(product["id"])
    assert detail["name"] == "Invoice Assistant"
    assert detail["prd"]["version"] == 1
    assert store.data["product_projects"]["schema_version"] == 1


@pytest.mark.asyncio
async def test_secret_and_workspace_governance(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    product = await service.create_project(opportunity_id="opp-1")
    with pytest.raises(ProductValidationError):
        service.generate_prd(
            product["id"],
            {"vision": "x", "constraints": ["api_key=sk-abcdefghijklmnop"]},
            actor="operator",
        )
    with pytest.raises(ProductValidationError):
        service.write_generated_file(
            product["id"],
            path="src/app.py",
            content="API_KEY = 'sk-abcdefghijklmnopqrstuv'\n",
        )


@pytest.mark.asyncio
async def test_history_and_audit_bounded_events(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    service, _ = make_service(tmp_path)
    service._publish_callback = lambda event_type, payload: events.append((event_type, dict(payload)))
    product = await service.create_project(opportunity_id="opp-1")
    history = service.history(product["id"])
    assert history["events"]
    assert all(row["product_id"] == product["id"] for row in history["events"])
    assert any(event_type == "product.project_created" for event_type, _ in events)
    assert service.status()["products"] == 1


@pytest.mark.asyncio
async def test_project_memory_and_knowledge_callbacks_receive_snapshots(tmp_path: Path) -> None:
    memories: list[dict[str, Any]] = []
    knowledge: list[tuple[str, str]] = []
    service, _ = make_service(tmp_path)
    service._memory_callback = lambda **kwargs: memories.append(kwargs)
    service._knowledge_callback = lambda text, title: knowledge.append((text, title))
    product = await service.create_project(opportunity_id="opp-1")
    service.generate_prd(product["id"], prd_inputs(), actor="operator")
    service.approve_requirements(
        product["id"], approved=True, actor="owner", note="Reviewed."
    )
    assert memories
    assert memories[-1]["value"]["product_id"] == product["id"]
    assert knowledge and "Approved product requirements" in knowledge[0][0]


@pytest.mark.asyncio
async def test_condition_resolution_requires_evidence(tmp_path: Path) -> None:
    detail = opportunity_detail(
        verdict="conditionally_validated",
        stage="conditionally_validated",
        conditions=["Two more customer interviews are required."],
    )
    service, _ = make_service(tmp_path, detail=detail)
    product = await service.create_project(
        opportunity_id="opp-1",
        approved=True,
        actor="owner",
        approval_note="Proceed while interviews are scheduled.",
    )
    with pytest.raises(ProductValidationError):
        service.resolve_condition(
            product["id"],
            condition="Two more customer interviews are required.",
            evidence=[],
        )
    resolved = service.resolve_condition(
        product["id"],
        condition="Two more customer interviews are required.",
        evidence=["interview-1", "interview-2"],
        actor="owner",
    )
    assert resolved["resolved"] is True
