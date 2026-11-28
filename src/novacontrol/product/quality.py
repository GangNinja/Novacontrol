"""Quality gates, test evidence and defect lifecycle.

A gate passes only when a recorded test result actually passed. There is no
path from "the application builds" to "production ready": build validation,
test evidence, requirement acceptance and defect status are separate facts.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from novacontrol.product.models import (
    DefectSeverity,
    DefectStatus,
    GateStatus,
    ProductProject,
    RequirementStatus,
    TestStatus,
    new_id,
    utcnow,
)
from novacontrol.product.workspace import redact_secrets

DEFAULT_GATES: tuple[dict[str, Any], ...] = (
    {
        "id": "unit",
        "name": "Unit tests",
        "kinds": ["unit"],
        "required": True,
        "description": "Deterministic unit tests for changed logic.",
    },
    {
        "id": "integration",
        "name": "Integration tests",
        "kinds": ["integration", "api", "e2e", "ui"],
        "required": True,
        "description": "Components and APIs exercised together.",
    },
    {
        "id": "security",
        "name": "Security checks",
        "kinds": ["security", "dependency", "secret_scan"],
        "required": True,
        "description": "Security review, dependency and secret checks.",
    },
    {
        "id": "static_analysis",
        "name": "Static analysis and types",
        "kinds": ["static_analysis", "type_check", "lint"],
        "required": True,
        "description": "Static analysis and type checking.",
    },
    {
        "id": "regression",
        "name": "Regression tests",
        "kinds": ["regression"],
        "required": True,
        "description": "Previously working behaviour still passes.",
    },
    {
        "id": "acceptance",
        "name": "Acceptance tests",
        "kinds": ["acceptance"],
        "required": True,
        "description": "Requirements and acceptance criteria verified.",
    },
)

_TEST_KINDS = {
    "unit",
    "integration",
    "api",
    "e2e",
    "ui",
    "security",
    "dependency",
    "secret_scan",
    "static_analysis",
    "type_check",
    "lint",
    "regression",
    "acceptance",
    "performance",
}
_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def normalise_gates(supplied: Any) -> list[dict[str, Any]]:
    if supplied is None:
        return [dict(row) for row in DEFAULT_GATES]
    if isinstance(supplied, str):
        raw: list[Any] = [supplied]
    elif isinstance(supplied, (list, tuple)):
        raw = list(supplied)
    else:
        raw = []
    if not raw:
        return [dict(row) for row in DEFAULT_GATES]
    gates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            gate_id = _text(item).lower()
            base = next((row for row in DEFAULT_GATES if row["id"] == gate_id), None)
            row = dict(base or {"id": gate_id, "name": gate_id.replace("_", " ").title()})
        elif isinstance(item, Mapping):
            row = dict(item)
            gate_id = _text(row.get("id") or row.get("name")).lower()
        else:
            raise ValueError("Quality gates must be ids or objects.")
        if not gate_id or not _SLUG.match(gate_id):
            raise ValueError(f"Quality gate id is unsafe: {gate_id or '(blank)'}")
        if gate_id in seen:
            raise ValueError(f"Duplicate quality gate: {gate_id}")
        seen.add(gate_id)
        kinds = [str(kind).lower() for kind in row.get("kinds") or [gate_id]]
        unknown = [kind for kind in kinds if kind not in _TEST_KINDS]
        if unknown:
            raise ValueError(
                f"Quality gate {gate_id} names unknown test kind(s): {', '.join(unknown)}"
            )
        gates.append(
            {
                "id": gate_id,
                "name": _text(row.get("name")) or gate_id.replace("_", " ").title(),
                "kinds": kinds,
                "required": bool(row.get("required", True)),
                "description": _text(row.get("description")),
                "status": GateStatus.NOT_RUN.value,
                "last_result_id": "",
                "reason": "No test result has been recorded.",
            }
        )
    return gates


def _normalise_status(value: Any, *, total: int, failed: int, passed: int) -> TestStatus:
    raw = _text(value).lower()
    if raw in {status.value for status in TestStatus}:
        status = TestStatus(raw)
    elif total <= 0:
        status = TestStatus.NOT_RUN
    elif failed or passed < total:
        status = TestStatus.FAILED
    else:
        status = TestStatus.PASSED
    if status is TestStatus.PASSED and (total <= 0 or failed > 0 or passed < total):
        raise ValueError(
            "A test result cannot pass without a positive test count and zero failures."
        )
    if status is TestStatus.PASSED and passed != total:
        raise ValueError("A passing result must report every test as passed.")
    return status


def record_test_result(
    project: ProductProject,
    payload: Mapping[str, Any],
    *,
    actor: str,
    max_tests: int,
) -> dict[str, Any]:
    gate_id = _text(payload.get("gate") or payload.get("gate_id")).lower()
    gate = project.gate(gate_id)
    if gate is None:
        raise ValueError(
            f"Unknown quality gate: {gate_id or '(blank)'}. "
            "Use one of: " + ", ".join(row["id"] for row in project.quality_gates)
        )
    kind = _text(payload.get("kind") or gate["kinds"][0]).lower()
    if kind not in gate["kinds"]:
        raise ValueError(
            f"Test kind {kind} is not part of gate {gate_id}; expected "
            + ", ".join(gate["kinds"])
        )
    raw_counts = (
        payload.get("total", 0),
        payload.get("passed", 0),
        payload.get("failed", 0),
        payload.get("skipped", 0),
    )
    if any(isinstance(value, bool) for value in raw_counts):
        raise ValueError("Test counts must be integers, not booleans.")
    try:
        total = int(raw_counts[0])
        passed = int(raw_counts[1])
        failed = int(raw_counts[2])
        skipped = int(raw_counts[3])
    except (TypeError, ValueError) as exc:
        raise ValueError("Test counts must be integers.") from exc
    if min(total, passed, failed, skipped) < 0:
        raise ValueError("Test counts cannot be negative.")
    if passed + failed + skipped > total:
        raise ValueError("Reported test counts exceed the total.")
    status = _normalise_status(payload.get("status"), total=total, failed=failed, passed=passed)
    coverage = payload.get("coverage")
    if coverage is not None:
        try:
            coverage = float(coverage)
        except (TypeError, ValueError) as exc:
            raise ValueError("Coverage must be a number between 0 and 1.") from exc
        if not 0.0 <= coverage <= 1.0:
            raise ValueError("Coverage must be between 0 and 1.")
    if status is TestStatus.PASSED and total == 0:
        raise ValueError("A passing test result needs at least one test.")
    result = {
        "id": new_id("test"),
        "suite": _text(payload.get("suite")) or gate["name"],
        "kind": kind,
        "gate": gate_id,
        "status": status.value,
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "coverage": coverage,
        "duration_seconds": payload.get("duration_seconds"),
        "command": redact_secrets(_text(payload.get("command")))[:1000],
        "output": redact_secrets(_text(payload.get("output")))[:8000],
        "evidence": redact_secrets(_text(payload.get("evidence")))[:1000],
        "actor": actor,
        "source": _text(payload.get("source")) or "recorded",
        "recorded_at": utcnow().isoformat(),
    }
    project.tests.append(result)
    project.tests = project.tests[-max_tests:]
    recompute_gate(project, gate_id)
    return result


def recompute_gate(project: ProductProject, gate_id: str) -> dict[str, Any]:
    gate = project.gate(gate_id)
    if gate is None:
        raise ValueError(f"Unknown quality gate: {gate_id}")
    results = [row for row in project.tests if row.get("gate") == gate_id]
    if not results:
        gate.update(
            status=GateStatus.NOT_RUN.value,
            last_result_id="",
            reason="No test result has been recorded.",
        )
        return gate
    latest = results[-1]
    status = TestStatus(str(latest.get("status", "not_run")))
    gate["last_result_id"] = latest["id"]
    if status is TestStatus.PASSED:
        gate["status"] = GateStatus.PASSED.value
        gate["reason"] = (
            f"Latest {latest['kind']} result passed "
            f"({latest.get('passed', 0)}/{latest.get('total', 0)})."
        )
    elif status in {TestStatus.FAILED, TestStatus.ERROR}:
        gate["status"] = GateStatus.FAILED.value
        gate["reason"] = (
            f"Latest {latest['kind']} result {status.value}; "
            f"{latest.get('failed', 0)} failure(s)."
        )
    else:
        gate["status"] = GateStatus.NOT_RUN.value
        gate["reason"] = f"Latest result was {status.value}; no passing evidence exists."
    return gate


def failing_gates(project: ProductProject) -> list[str]:
    return [
        str(row["id"])
        for row in project.quality_gates
        if row.get("required", True) and row.get("status") != GateStatus.PASSED.value
    ]


def record_build_result(
    project: ProductProject,
    *,
    command: str,
    exit_code: int,
    output: str,
    validated: bool,
    actor: str,
) -> dict[str, Any]:
    if not command.strip():
        raise ValueError("A build record needs the command that was run.")
    if validated and exit_code != 0:
        raise ValueError("A build with a non-zero exit code cannot be marked validated.")
    project.build = {
        "command": redact_secrets(command)[:1000],
        "exit_code": int(exit_code),
        "output": redact_secrets(output)[:8000],
        "validated": bool(validated),
        "actor": actor,
        "source": "recorded",
        "recorded_at": utcnow().isoformat(),
        "note": (
            "A successful build proves compilation only; it is not test or "
            "acceptance evidence."
        ),
    }
    return project.build


def _unmet_requirements(project: ProductProject) -> list[str]:
    rows = [
        str(row["id"])
        for row in project.requirements()
        if row.get("kind")
        in {"functional", "core_feature", "non_functional", "security", "performance", "accessibility"}
        and row.get("status") not in {RequirementStatus.VERIFIED.value, RequirementStatus.REJECTED.value}
    ]
    return rows


def open_defect(
    project: ProductProject,
    payload: Mapping[str, Any],
    *,
    actor: str,
    max_defects: int,
) -> dict[str, Any]:
    title = _text(payload.get("title"))
    if not title:
        raise ValueError("A defect needs a title.")
    severity = _text(payload.get("severity") or "medium").lower()
    if severity not in {row.value for row in DefectSeverity}:
        raise ValueError("Defect severity must be low, medium, high or critical.")
    requirement_id = _text(payload.get("requirement_id"))
    if requirement_id and project.requirement(requirement_id) is None:
        raise ValueError(f"Unknown requirement for defect: {requirement_id}")
    task_id = _text(payload.get("task_id"))
    if task_id and project.task(task_id) is None:
        raise ValueError(f"Unknown task for defect: {task_id}")
    if payload.get("test_result_id"):
        test_id = _text(payload.get("test_result_id"))
        if not any(row.get("id") == test_id for row in project.tests):
            raise ValueError(f"Unknown test result for defect: {test_id}")
    defect = {
        "id": new_id("defect"),
        "title": title,
        "description": redact_secrets(_text(payload.get("description")))[:4000],
        "severity": severity,
        "status": DefectStatus.OPEN.value,
        "requirement_id": requirement_id,
        "task_id": task_id,
        "test_result_id": _text(payload.get("test_result_id")),
        "detected_by": actor,
        "detected_at": utcnow().isoformat(),
        "fix_task_id": "",
        "test_result_ids": [],
        "resolution": "",
        "resolved_at": "",
        "verified_at": "",
        "history": [
            {
                "at": utcnow().isoformat(),
                "status": DefectStatus.OPEN.value,
                "actor": actor,
                "note": _text(payload.get("note")),
            }
        ],
    }
    if len(project.defects) >= max_defects:
        raise ValueError(
            f"The defect budget ({max_defects}) is exhausted; resolve existing defects "
            "before recording another."
        )
    project.defects.append(defect)
    return defect


def resolve_defect(
    project: ProductProject,
    defect_id: str,
    *,
    actor: str,
    resolution: str,
    fix_task_id: str = "",
    test_result_ids: Sequence[str] = (),
    verified: bool = False,
) -> dict[str, Any]:
    defect = project.defect(defect_id)
    if defect is None:
        raise ValueError(f"Unknown defect: {defect_id}")
    if defect.get("status") == DefectStatus.VERIFIED.value:
        raise ValueError("That defect is already verified.")
    if not resolution.strip():
        raise ValueError("A defect resolution needs a factual note.")
    if fix_task_id:
        task = project.task(fix_task_id)
        if task is None:
            raise ValueError(f"Unknown fix task: {fix_task_id}")
        if task.get("status") != "completed":
            raise ValueError("The corrective task must be completed before a fix is recorded.")
    test_ids = [str(item) for item in test_result_ids if str(item).strip()]
    if not test_ids:
        raise ValueError("A defect fix needs at least one passing test result as evidence.")
    known = {str(row.get("id")): row for row in project.tests}
    missing = [test_id for test_id in test_ids if test_id not in known]
    if missing:
        raise ValueError("Unknown test result(s): " + ", ".join(missing))
    not_passed = [
        test_id
        for test_id in test_ids
        if known[test_id].get("status") != TestStatus.PASSED.value
    ]
    if not_passed:
        raise ValueError(
            "Only passing test results can resolve a defect: " + ", ".join(not_passed)
        )
    now = utcnow().isoformat()
    defect["status"] = DefectStatus.VERIFIED.value if verified else DefectStatus.FIXED.value
    defect["fix_task_id"] = fix_task_id
    defect["test_result_ids"] = test_ids
    defect["resolution"] = redact_secrets(resolution)[:4000]
    defect["resolved_at"] = now
    defect["verified_at"] = now if verified else ""
    defect["history"].append(
        {
            "at": now,
            "status": defect["status"],
            "actor": actor,
            "note": defect["resolution"],
        }
    )
    return defect


def reopen_defect(
    project: ProductProject, defect_id: str, *, actor: str, reason: str
) -> dict[str, Any]:
    defect = project.defect(defect_id)
    if defect is None:
        raise ValueError(f"Unknown defect: {defect_id}")
    if not reason.strip():
        raise ValueError("Reopening a defect needs a reason.")
    now = utcnow().isoformat()
    defect["status"] = DefectStatus.REOPENED.value
    defect["resolution"] = ""
    defect["resolved_at"] = ""
    defect["verified_at"] = ""
    defect["history"].append(
        {"at": now, "status": defect["status"], "actor": actor, "note": reason}
    )
    return defect


def blocking_defects(project: ProductProject) -> list[dict[str, Any]]:
    return [
        row
        for row in project.defects
        if row.get("status") in {DefectStatus.OPEN.value, DefectStatus.REOPENED.value}
        and row.get("severity") in {DefectSeverity.HIGH.value, DefectSeverity.CRITICAL.value}
    ]


def open_defects(project: ProductProject) -> list[dict[str, Any]]:
    return [
        row
        for row in project.defects
        if row.get("status") in {DefectStatus.OPEN.value, DefectStatus.REOPENED.value}
    ]


def quality_snapshot(project: ProductProject) -> dict[str, Any]:
    gates = [
        {
            "id": row["id"],
            "name": row["name"],
            "required": row.get("required", True),
            "status": row.get("status"),
            "reason": row.get("reason"),
            "last_result_id": row.get("last_result_id"),
        }
        for row in project.quality_gates
    ]
    return {
        "gates": gates,
        "failing_required": failing_gates(project),
        "all_required_passed": project.required_gates_passed(),
        "unmet_requirements": _unmet_requirements(project),
        "blocking_defects": [row["id"] for row in blocking_defects(project)],
        "open_defects": [row["id"] for row in open_defects(project)],
        "test_count": len(project.tests),
        "build_validated": bool(project.build.get("validated")),
        "note": "Build success alone is not treated as test, acceptance or launch evidence.",
    }


__all__ = [
    "DEFAULT_GATES",
    "blocking_defects",
    "failing_gates",
    "normalise_gates",
    "open_defect",
    "open_defects",
    "quality_snapshot",
    "record_build_result",
    "record_test_result",
    "recompute_gate",
    "reopen_defect",
    "resolve_defect",
]
