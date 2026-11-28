"""Product requirements engineering.

The generator turns operator-supplied requirements into a traceable PRD. It
never fills an absent requirement with invented prose: absent sections become
``missing_information`` entries that must be answered before approval. The only
defaults come from the originating Phase 35 opportunity (problem, audience,
solution reference), and each carries that source in its trace record.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping, Sequence
from uuid import uuid4

from novacontrol.product.models import ProductProject, RequirementKind, new_id, utcnow

_PREFIX = {
    RequirementKind.FUNCTIONAL: "REQ",
    RequirementKind.NON_FUNCTIONAL: "NFR",
    RequirementKind.CORE_FEATURE: "FEAT",
    RequirementKind.OPTIONAL_FEATURE: "OPT",
    RequirementKind.SECURITY: "SEC",
    RequirementKind.PERFORMANCE: "PERF",
    RequirementKind.ACCESSIBILITY: "A11Y",
    RequirementKind.CONSTRAINT: "CON",
}


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        return [row.strip() for row in value.replace("\r", "").split("\n") if row.strip()]
    return [value]


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _strings(value: Any) -> list[str]:
    return [text for row in _list(value) if (text := _text(row))]


def _priority(value: Any) -> str:
    raw = _text(value).lower()
    if raw in {"critical", "high", "medium", "low"}:
        return raw
    return "medium"


def _requirement(
    *,
    kind: RequirementKind,
    index: int,
    row: Any,
    project: ProductProject,
    default_trace: str,
) -> dict[str, Any]:
    if isinstance(row, Mapping):
        title = _text(row.get("title") or row.get("name") or row.get("feature"))
        description = _text(row.get("description") or row.get("detail") or title)
        acceptance = _strings(row.get("acceptance_criteria") or row.get("acceptance"))
        source = _text(row.get("source")) or default_trace
        notes = _text(row.get("notes"))
        priority = _priority(row.get("priority"))
    else:
        title = _text(row)
        description = title
        acceptance = []
        source = default_trace
        notes = ""
        priority = "medium"
    if not title:
        title = f"Untitled {kind.value.replace('_', ' ')} requirement"
    return {
        "id": f"{_PREFIX[kind]}-{index:03d}",
        "kind": kind.value,
        "title": title,
        "description": description,
        "priority": priority,
        "status": "draft",
        "source": source,
        "trace": {
            "opportunity_id": project.opportunity_id,
            "validation_report_version": project.validation_report.get("version"),
            "validation_state": project.validation_state,
            "source": source,
        },
        "acceptance_criteria": acceptance,
        "notes": notes,
    }


def _requirements_for(
    values: Any,
    kind: RequirementKind,
    project: ProductProject,
    *,
    source: str,
) -> list[dict[str, Any]]:
    rows = _list(values)
    return [
        _requirement(
            kind=kind,
            index=index,
            row=row,
            project=project,
            default_trace=source,
        )
        for index, row in enumerate(rows, start=1)
    ]


def build_prd(
    project: ProductProject,
    inputs: Mapping[str, Any],
    *,
    actor: str,
) -> dict[str, Any]:
    """Build a new PRD revision from explicit inputs and the opportunity trace."""
    data = dict(inputs or {})
    snapshot = project.opportunity_snapshot
    missing: list[str] = []

    vision = _text(data.get("vision"))
    if not vision:
        missing.append("Product vision")

    problem = _text(data.get("problem_statement")) or _text(
        snapshot.get("customer_problem")
    )
    if not problem:
        missing.append("Problem statement")

    target_users = _strings(data.get("target_users"))
    if not target_users:
        audience = _text(snapshot.get("target_audience"))
        if audience:
            target_users = [audience]
    if not target_users:
        missing.append("Target users")

    personas = _strings(data.get("personas"))
    if not personas:
        missing.append("User personas")

    journeys = _strings(data.get("user_journeys"))
    if not journeys:
        missing.append("User journeys")

    requirements: list[dict[str, Any]] = []
    requirements.extend(
        _requirements_for(
            data.get("functional_requirements"),
            RequirementKind.FUNCTIONAL,
            project,
            source=f"operator; opportunity {project.opportunity_id}",
        )
    )
    requirements.extend(
        _requirements_for(
            data.get("non_functional_requirements"),
            RequirementKind.NON_FUNCTIONAL,
            project,
            source="operator",
        )
    )
    requirements.extend(
        _requirements_for(
            data.get("core_features"),
            RequirementKind.CORE_FEATURE,
            project,
            source="operator",
        )
    )
    requirements.extend(
        _requirements_for(
            data.get("optional_features"),
            RequirementKind.OPTIONAL_FEATURE,
            project,
            source="operator",
        )
    )
    requirements.extend(
        _requirements_for(
            data.get("security_requirements"),
            RequirementKind.SECURITY,
            project,
            source="operator",
        )
    )
    requirements.extend(
        _requirements_for(
            data.get("performance_requirements"),
            RequirementKind.PERFORMANCE,
            project,
            source="operator",
        )
    )
    requirements.extend(
        _requirements_for(
            data.get("accessibility_requirements"),
            RequirementKind.ACCESSIBILITY,
            project,
            source="operator",
        )
    )
    if not any(row["kind"] == RequirementKind.FUNCTIONAL.value for row in requirements):
        missing.append("Functional requirements")
    if not any(row["kind"] == RequirementKind.NON_FUNCTIONAL.value for row in requirements):
        missing.append("Non-functional requirements")
    if not any(row["kind"] == RequirementKind.SECURITY.value for row in requirements):
        missing.append("Security requirements")
    if not any(row["kind"] == RequirementKind.PERFORMANCE.value for row in requirements):
        missing.append("Performance requirements")
    if not any(row["kind"] == RequirementKind.ACCESSIBILITY.value for row in requirements):
        missing.append("Accessibility requirements")

    constraints = _strings(data.get("constraints"))
    dependencies = _strings(data.get("dependencies"))
    if not constraints:
        missing.append("Constraints")
    if not dependencies:
        missing.append("Dependencies")

    acceptance_criteria = _strings(data.get("acceptance_criteria"))
    if not acceptance_criteria:
        # Requirement-level criteria are real acceptance criteria too.
        acceptance_criteria = [
            criterion
            for row in requirements
            for criterion in row.get("acceptance_criteria", [])
        ]
    if not acceptance_criteria:
        missing.append("Acceptance criteria")

    success_metrics = _strings(data.get("success_metrics"))
    if not success_metrics:
        missing.append("Success metrics")

    exclusions = _strings(data.get("explicit_exclusions"))
    if not exclusions:
        missing.append("Explicit exclusions")
    if project.conditions:
        missing.append(
            f"{len(project.conditions)} unresolved opportunity condition(s) require "
            "verification before release"
        )

    now = utcnow().isoformat()
    version = int((project.prd or {}).get("version", 0)) + 1
    return {
        "id": new_id("prd"),
        "version": version,
        "status": "draft",
        "vision": vision,
        "problem_statement": problem,
        "target_users": target_users,
        "personas": personas,
        "user_journeys": journeys,
        "requirements": requirements,
        "functional_requirements": [
            row for row in requirements if row["kind"] == "functional"
        ],
        "non_functional_requirements": [
            row for row in requirements if row["kind"] == "non_functional"
        ],
        "core_features": _strings(data.get("core_features")),
        "optional_features": _strings(data.get("optional_features")),
        "constraints": constraints,
        "dependencies": dependencies,
        "security_requirements": _strings(data.get("security_requirements")),
        "performance_requirements": _strings(data.get("performance_requirements")),
        "accessibility_requirements": _strings(data.get("accessibility_requirements")),
        "acceptance_criteria": acceptance_criteria,
        "success_metrics": success_metrics,
        "explicit_exclusions": exclusions,
        "missing_information": missing,
        "traceability": {
            "opportunity_id": project.opportunity_id,
            "opportunity_title": project.opportunity_title,
            "validation_state": project.validation_state,
            "validation_report_version": project.validation_report.get("version"),
            "validation_rationale": project.validation_report.get("rationale", ""),
            "condition_count": len(project.conditions),
            "requirement_ids": [row["id"] for row in requirements],
        },
        "notes": _text(data.get("notes")),
        "created_at": now,
        "updated_at": now,
        "created_by": actor,
        "approved_at": "",
        "approved_by": "",
        "approval_note": "",
    }


def archive_prd_revision(project: ProductProject, note: str) -> None:
    """Keep a bounded, complete snapshot of the revision being replaced."""
    if project.prd:
        project.prd_history.append(
            {
                "version": project.prd.get("version"),
                "status": project.prd.get("status"),
                "archived_at": datetime.now(UTC).isoformat(),
                "note": note,
                "snapshot": project.prd,
            }
        )
        project.prd_history = project.prd_history[-20:]


def approve_prd(
    project: ProductProject,
    *,
    approved: bool,
    actor: str,
    note: str,
    version: int | None = None,
) -> dict[str, Any]:
    prd = project.prd
    if prd is None:
        raise ValueError("Generate a PRD before approving requirements.")
    if version is not None and int(version) != int(prd.get("version", 0)):
        raise ValueError("The approval names a PRD revision that is no longer current.")
    if not actor.strip():
        raise ValueError("A requirements decision needs a named human approver.")
    if not note.strip():
        raise ValueError("A requirements decision needs a recorded note.")
    now = utcnow().isoformat()
    prd["status"] = "approved" if approved else "revision_requested"
    prd["approved_at"] = now if approved else ""
    prd["approved_by"] = actor if approved else ""
    prd["approval_note"] = note
    prd["updated_at"] = now
    for row in prd.get("requirements", []):
        # Re-approving the same PRD must not erase evidence-bound verification.
        if approved and row.get("status") == "verified":
            continue
        row["status"] = "approved" if approved else "draft"
    return {
        "approval": {
            "kind": "requirements",
            "target_id": prd["id"],
            "version": prd.get("version"),
            "approved": approved,
            "actor": actor,
            "note": note,
            "at": now,
            "automated_recommendation": False,
        },
        "prd": prd,
    }


def mark_requirement_verified(
    project: ProductProject,
    requirement_id: str,
    *,
    passed: bool,
    evidence: Sequence[str],
    actor: str,
) -> dict[str, Any]:
    requirement = project.requirement(requirement_id)
    if requirement is None:
        raise ValueError(f"Unknown requirement: {requirement_id}")
    if passed and not [item for item in evidence if str(item).strip()]:
        raise ValueError("A requirement cannot be verified without evidence.")
    requirement["status"] = "verified" if passed else "unmet"
    requirement["verified_at"] = utcnow().isoformat()
    requirement["verified_by"] = actor
    requirement["verification_evidence"] = [str(item) for item in evidence]
    requirement["updated_at"] = requirement["verified_at"]
    return requirement


__all__ = [
    "approve_prd",
    "archive_prd_revision",
    "build_prd",
    "mark_requirement_verified",
]
