"""Transparent launch-readiness evaluation and human launch authorization."""

from __future__ import annotations

from typing import Any

from novacontrol.product.models import (
    GateStatus,
    ProductProject,
    ProductState,
    RequirementStatus,
    TaskState,
    TestStatus,
    utcnow,
)
from novacontrol.product.quality import blocking_defects, failing_gates, open_defects


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _check(
    check_id: str,
    passed: bool,
    detail: str,
    *,
    blocking: bool = True,
    warning: bool = False,
) -> dict[str, Any]:
    if passed:
        status = "passed"
    elif warning:
        status = "warning"
    else:
        status = "failed"
    return {
        "id": check_id,
        "status": status,
        "blocking": bool(blocking and not passed and not warning),
        "detail": detail,
    }


def _requirements_check(project: ProductProject) -> dict[str, Any]:
    prd = project.prd
    if prd is None:
        return _check("requirements", False, "No PRD has been generated.", blocking=False)
    if prd.get("status") != "approved":
        return _check("requirements", False, "The PRD has not been approved.", blocking=False)
    requirements = [
        row
        for row in project.requirements()
        if row.get("kind")
        in {
            "functional",
            "core_feature",
            "non_functional",
            "security",
            "performance",
            "accessibility",
        }
    ]
    if not requirements:
        return _check(
            "requirements",
            False,
            "The approved PRD has no implementation requirements.",
            blocking=False,
        )
    unmet = [
        str(row["id"])
        for row in requirements
        if row.get("status") != RequirementStatus.VERIFIED.value
    ]
    return _check(
        "requirements",
        not unmet,
        "All implementation requirements are verified."
        if not unmet
        else "Unverified requirement(s): " + ", ".join(unmet),
        blocking=False,
    )


def _development_check(project: ProductProject) -> dict[str, Any]:
    plan = project.plan
    if plan is None:
        return _check("development", False, "No development plan exists.", blocking=False)
    if plan.get("status") not in {"approved", "draft"}:
        return _check("development", False, "The development plan was rejected.", blocking=False)
    tasks = project.tasks()
    if not tasks:
        return _check("development", False, "The development plan has no tasks.", blocking=False)
    incomplete = [
        str(row["id"])
        for row in tasks
        if row.get("status") != TaskState.COMPLETED.value
    ]
    return _check(
        "development",
        not incomplete,
        "Every plan task is complete."
        if not incomplete
        else f"{len(incomplete)} task(s) are not complete.",
        blocking=False,
    )


def _tests_check(project: ProductProject) -> dict[str, Any]:
    gates = failing_gates(project)
    return _check(
        "quality_gates",
        not gates,
        "Every required quality gate passed."
        if not gates
        else "Failing required gate(s): " + ", ".join(gates),
        blocking=False,
    )


def _release_check(project: ProductProject, *, approval: bool) -> dict[str, Any]:
    release = project.release()
    if release is None:
        return _check(
            "release_approval" if approval else "release_prepared",
            False,
            "No release has been prepared.",
            blocking=False,
        )
    if approval:
        return _check(
            "release_approval",
            release.get("status") == "approved",
            "The release has been approved."
            if release.get("status") == "approved"
            else f"Release {release.get('version')} is {release.get('status')}; approval is required.",
            blocking=False,
        )
    return _check(
        "release_prepared", True, f"Release {release.get('version')} is prepared.", blocking=False
    )


def _security_check(project: ProductProject) -> dict[str, Any]:
    gate = project.gate("security")
    architecture = project.architecture or {}
    if gate is None:
        return _check(
            "security",
            False,
            "No security quality gate is configured.",
            blocking=True,
        )
    if gate.get("status") != GateStatus.PASSED.value:
        return _check(
            "security",
            False,
            gate.get("reason") or "The security gate has not passed.",
        )
    if not architecture.get("security"):
        return _check(
            "security",
            False,
            "Security architecture was not recorded.",
            blocking=False,
            warning=True,
        )
    return _check("security", True, "Security gate passed and security architecture is recorded.")


def _performance_check(project: ProductProject) -> dict[str, Any]:
    performance_requirements = [
        row
        for row in project.requirements()
        if row.get("kind") == "performance"
    ]
    if not performance_requirements:
        return _check(
            "performance",
            True,
            "No performance requirement is recorded; nothing was measured.",
            blocking=False,
        )
    results = [
        row for row in project.tests if row.get("kind") == "performance"
    ]
    if not results:
        return _check(
            "performance",
            False,
            "Performance requirements exist but no performance result was recorded.",
            blocking=False,
            warning=True,
        )
    latest = results[-1]
    passed = latest.get("status") == TestStatus.PASSED.value
    return _check(
        "performance",
        passed,
        f"Latest performance result: {latest.get('status')}.",
        blocking=not passed,
    )


def _checklist(project: ProductProject) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    state = project.state
    checks.append(
        _check(
            "opportunity",
            project.validation_state
            in {"VALIDATED", "CONDITIONALLY_VALIDATED"},
            f"Originating opportunity state: {project.validation_state}.",
        )
    )
    if project.conditions:
        resolved = {
            str(row.get("condition")) for row in project.condition_resolutions if row.get("resolved")
        }
        unresolved = [item for item in project.conditions if item not in resolved]
        checks.append(
            _check(
                "opportunity_conditions",
                not unresolved,
                "All recorded opportunity conditions are resolved."
                if not unresolved
                else f"{len(unresolved)} unresolved opportunity condition(s) remain; "
                "they are retained as launch risk.",
                blocking=False,
                warning=True,
            )
        )
    else:
        checks.append(_check("opportunity_conditions", True, "No unresolved conditions were recorded."))
    checks.append(_requirements_check(project))
    architecture = project.architecture
    checks.append(
        _check(
            "architecture",
            bool(architecture and architecture.get("status") == "approved"),
            "The architecture is approved."
            if architecture and architecture.get("status") == "approved"
            else "The architecture has not been approved.",
            blocking=False,
        )
    )
    checks.append(_development_check(project))
    build = project.build
    checks.append(
        _check(
            "build",
            bool(build.get("validated")),
            f"Build command exited {build.get('exit_code')}; build validation is "
            + ("recorded." if build.get("validated") else "not recorded."),
            blocking=False,
        )
    )
    checks.append(_tests_check(project))
    blocking = blocking_defects(project)
    open_rows = open_defects(project)
    checks.append(
        _check(
            "defects",
            not blocking,
            "No high/critical open defect is recorded."
            if not blocking
            else "Blocking defect(s): "
            + ", ".join(f"{row['id']} ({row['severity']})" for row in blocking),
        )
    )
    if open_rows and not blocking:
        checks.append(
            _check(
                "open_defects",
                False,
                f"{len(open_rows)} lower-severity open defect(s) remain known.",
                blocking=False,
                warning=True,
            )
        )
    else:
        checks.append(_check("open_defects", True, "No open lower-severity defects are recorded.", blocking=False))
    checks.append(_release_check(project, approval=False))
    checks.append(_release_check(project, approval=True))
    release = project.release()
    docs_ready = bool(
        release
        and release.get("notes")
        and release.get("deployment_docs")
        and release.get("rollback_plan")
    )
    checks.append(
        _check(
            "documentation",
            docs_ready,
            "Release notes, deployment documentation and rollback plan are recorded."
            if docs_ready
            else "Release documentation is incomplete.",
            blocking=False,
        )
    )
    operational = bool(release and release.get("operational_requirements"))
    checks.append(
        _check(
            "operational_requirements",
            operational,
            "Operational requirements are recorded."
            if operational
            else "Monitoring and recovery requirements are not recorded.",
            blocking=False,
            warning=True,
        )
    )
    checks.append(_security_check(project))
    checks.append(_performance_check(project))
    if project.conditions or (project.plan or {}).get("risks"):
        checks.append(
            _check(
                "known_risks",
                True,
                f"{len(project.conditions)} opportunity condition(s) and "
                f"{len((project.plan or {}).get('risks', []))} plan risk(s) are visible in the report.",
                blocking=False,
            )
        )
    if state == ProductState.ARCHIVED.value:
        checks.append(_check("lifecycle", False, "The product is archived.", blocking=True))
    elif project.cancelled:
        checks.append(
            _check(
                "lifecycle",
                False,
                "The product was cancelled: " + (project.cancellation_reason or "no reason recorded"),
                blocking=True,
            )
        )
    else:
        checks.append(_check("lifecycle", True, "The product is active.", blocking=False))
    return checks


def _score(checks: list[dict[str, Any]]) -> float:
    relevant = [row for row in checks if row["status"] != "not_applicable"]
    if not relevant:
        return 0.0
    earned = sum(
        1.0 if row["status"] == "passed" else 0.5 if row["status"] == "warning" else 0.0
        for row in relevant
    )
    return round(earned / len(relevant), 4)


def _base_status(project: ProductProject, checks: list[dict[str, Any]]) -> str:
    if project.state == ProductState.ARCHIVED.value:
        return ProductState.ARCHIVED.value
    if project.cancelled:
        return ProductState.BLOCKED.value
    if any(row["blocking"] for row in checks):
        return ProductState.BLOCKED.value
    tasks = project.tasks()
    completed = bool(tasks) and all(
        row.get("status") == TaskState.COMPLETED.value for row in tasks
    )
    if not completed:
        return ProductState.DEVELOPMENT.value
    if not project.tests:
        return ProductState.INTERNAL_TESTING.value
    if failing_gates(project):
        return ProductState.QA_IN_PROGRESS.value
    if project.release() is None:
        return ProductState.QA_IN_PROGRESS.value
    return ProductState.READY_FOR_RELEASE_REVIEW.value


def _approval_is_valid(project: ProductProject, checks: list[dict[str, Any]]) -> bool:
    approval = project.launch_approval
    if not approval.get("approved"):
        return False
    if any(row["blocking"] for row in checks):
        return False
    checks_by_id = {str(row["id"]): row for row in checks}
    for required in ("requirements", "architecture", "development", "build"):
        if (checks_by_id.get(required) or {}).get("status") != "passed":
            return False
    prd = project.prd or {}
    architecture = project.architecture or {}
    plan = project.plan or {}
    if prd.get("status") != "approved":
        return False
    if architecture.get("status") != "approved":
        return False
    if architecture.get("requirements_version") != prd.get("version"):
        return False
    if plan.get("requirements_version") != prd.get("version"):
        return False
    if failing_gates(project):
        return False
    release = project.release()
    if release is None or release.get("status") != "approved":
        return False
    return True


def evaluate_launch(project: ProductProject, *, actor: str = "system") -> dict[str, Any]:
    checks = _checklist(project)
    blockers = [row for row in checks if row["blocking"]]
    warnings = [row for row in checks if row["status"] == "warning"]
    status = _base_status(project, checks)
    approval_valid = _approval_is_valid(project, checks)
    release = project.release()
    if approval_valid:
        status = ProductState.APPROVED_FOR_LAUNCH.value
    elif status == ProductState.ARCHIVED.value:
        pass
    elif status == ProductState.READY_FOR_RELEASE_REVIEW.value and not (
        release is not None and release.get("status") == "approved"
    ):
        # Still a release review: the release itself needs a human decision.
        status = ProductState.READY_FOR_RELEASE_REVIEW.value
    if (
        project.launch_approval.get("approved")
        and not approval_valid
        and status != ProductState.ARCHIVED.value
    ):
        status = ProductState.BLOCKED.value
        warnings.append(
            {
                "id": "launch_approval_invalidated",
                "status": "warning",
                "blocking": False,
                "detail": "A previous launch approval no longer satisfies the current gates "
                "and is not treated as approval.",
            }
        )
    readiness = {
        "id": f"readiness-{project.id}",
        "evaluated_at": utcnow().isoformat(),
        "evaluated_by": actor,
        "status": status,
        "score": _score(checks),
        "checks": checks,
        "blockers": [row["id"] for row in blockers],
        "warnings": [row["id"] for row in warnings],
        "warnings_detail": warnings,
        "requirements_completion": _requirements_completion(project),
        "open_defects": [row["id"] for row in open_defects(project)],
        "known_risks": list(project.conditions)
        + [str(row.get("description", "")) for row in (project.plan or {}).get("risks", [])],
        "launch_approval": dict(project.launch_approval),
        "approval_valid": approval_valid,
        "release_id": release.get("id", "") if release is not None else "",
        "release_approved": bool(release is not None and release.get("status") == "approved"),
        "external_launch_executed": False,
        "note": (
            "This is a readiness assessment, not an external launch. APPROVED_FOR_LAUNCH "
            "records human authorization only; no deployment, campaign or commercial "
            "commitment is performed."
        ),
    }
    project.readiness = readiness
    project.state = status
    project.updated_at = utcnow()
    return readiness


def _requirements_completion(project: ProductProject) -> dict[str, Any]:
    rows = [
        row
        for row in project.requirements()
        if row.get("kind")
        in {"functional", "core_feature", "non_functional", "security", "performance", "accessibility"}
    ]
    verified = [row for row in rows if row.get("status") == RequirementStatus.VERIFIED.value]
    return {
        "requirements": len(rows),
        "verified": len(verified),
        "ratio": round(len(verified) / len(rows), 4) if rows else 0.0,
    }


def approve_launch(
    project: ProductProject,
    *,
    approved: bool,
    actor: str,
    note: str,
) -> dict[str, Any]:
    if not actor.strip():
        raise ValueError("A launch decision needs a named human approver.")
    if not note.strip():
        raise ValueError("A launch decision needs a recorded note.")
    if project.state == ProductState.ARCHIVED.value:
        raise ValueError("An archived product cannot be approved for launch.")
    readiness = evaluate_launch(project, actor=actor)
    if not approved:
        now = utcnow().isoformat()
        project.launch_approval = {
            "approved": False,
            "actor": actor,
            "note": note,
            "at": now,
            "automated_recommendation": False,
        }
        project.state = ProductState.BLOCKED.value
        readiness["status"] = ProductState.BLOCKED.value
        readiness["launch_approval"] = dict(project.launch_approval)
        readiness["detail"] = "Launch approval was rejected by a human operator."
        project.readiness = readiness
        project.updated_at = utcnow()
        return readiness
    if readiness["status"] != ProductState.READY_FOR_RELEASE_REVIEW.value:
        raise ValueError(
            "Launch approval requires a READY_FOR_RELEASE_REVIEW product; current status is "
            f"{readiness['status']}."
        )
    if (project.prd or {}).get("status") != "approved":
        raise ValueError("Launch approval requires an approved PRD.")
    if (project.architecture or {}).get("status") != "approved":
        raise ValueError("Launch approval requires an approved architecture.")
    if (project.architecture or {}).get("requirements_version") != (
        project.prd or {}
    ).get("version"):
        raise ValueError(
            "The approved architecture does not match the current PRD revision; "
            "regenerate and reapprove it."
        )
    if (project.plan or {}).get("requirements_version") != (project.prd or {}).get("version"):
        raise ValueError(
            "The development plan does not match the current PRD revision; rebuild the plan."
        )
    completion = readiness.get("requirements_completion") or {}
    total_requirements = int(completion.get("requirements", 0) or 0)
    verified_requirements = int(completion.get("verified", 0) or 0)
    if total_requirements == 0:
        raise ValueError("Launch approval requires at least one implementation requirement.")
    if verified_requirements != total_requirements:
        raise ValueError(
            "Launch approval requires every implementation requirement to be verified; "
            f"{total_requirements - verified_requirements} remain unverified."
        )
    release = project.release()
    if release is None:
        raise ValueError("Launch approval requires a prepared release.")
    if release.get("status") != "approved":
        raise ValueError("Launch approval requires the prepared release to be approved first.")
    if failing_gates(project):
        raise ValueError(
            "Launch approval cannot bypass failing quality gates: "
            + ", ".join(failing_gates(project))
        )
    if readiness["blockers"]:
        raise ValueError(
            "Launch approval cannot bypass blockers: " + ", ".join(readiness["blockers"])
        )
    now = utcnow().isoformat()
    project.launch_approval = {
        "approved": True,
        "actor": actor,
        "note": note,
        "at": now,
        "automated_recommendation": False,
        "release_id": release["id"],
    }
    return evaluate_launch(project, actor=actor)


__all__ = ["approve_launch", "evaluate_launch"]
