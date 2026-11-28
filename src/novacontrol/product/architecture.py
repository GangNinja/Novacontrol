"""Technical design and architecture decision records.

Design content comes from operator-supplied choices or remains a named missing
input. The generator may group approved requirements into components (a
structural derivation), but it does not select a stack, database or deployment
target on its own. Approval is a human action and approves the recorded ADRs
together with the document.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from novacontrol.product.models import ProductProject, new_id, utcnow


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


def _objects(value: Any, fields: Sequence[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in value or []:
        if isinstance(item, Mapping):
            row = dict(item)
            if "title" not in row and "name" in row:
                row["title"] = row["name"]
            rows.append(row)
        elif _text(item):
            rows.append({fields[0]: _text(item)})
    return rows


def _decisions(project: ProductProject, supplied: Any) -> list[dict[str, Any]]:
    rows = _objects(supplied, ("title",))
    decisions: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        title = _text(row.get("title") or row.get("name"))
        decision = _text(row.get("decision") or row.get("choice"))
        rationale = _text(row.get("rationale") or row.get("reason"))
        if not title or not decision or not rationale:
            raise ValueError(
                "Every architecture decision needs a title, decision and rationale."
            )
        decisions.append(
            {
                "id": _text(row.get("id")) or f"ADR-{index:03d}",
                "title": title,
                "decision": decision,
                "rationale": rationale,
                "alternatives": _strings(row.get("alternatives")),
                "consequences": _strings(row.get("consequences") or row.get("tradeoffs")),
                "major": bool(row.get("major", True)),
                "status": "proposed",
                "approved_by": "",
                "approved_at": "",
                "requirement_ids": _strings(row.get("requirement_ids")),
                "source": "operator",
            }
        )
    return decisions


def build_architecture(
    project: ProductProject, inputs: Mapping[str, Any], *, actor: str
) -> dict[str, Any]:
    prd = project.prd
    if prd is None or prd.get("status") != "approved":
        raise ValueError("A technical design requires an approved PRD.")
    data = dict(inputs or {})
    missing: list[str] = []

    components = _objects(data.get("components"), ("title",))
    if not components:
        requirement_ids = [
            str(row["id"])
            for row in project.requirements()
            if row.get("kind") in {"functional", "core_feature", "non_functional"}
        ]
        components = [
            {
                "id": "COMP-001",
                "title": "Product application",
                "responsibility": "Deliver the approved product requirements.",
                "requirement_ids": requirement_ids,
                "source": "derived_from_approved_prd",
            }
        ]
        missing.append(
            "Component decomposition was derived from requirements; review component "
            "boundaries and responsibilities."
        )
    for index, row in enumerate(components, start=1):
        row.setdefault("id", f"COMP-{index:03d}")
        row.setdefault("title", row.get("name", f"Component {index}"))
        row.setdefault("responsibility", "")
        row["requirement_ids"] = _strings(row.get("requirement_ids"))
        unknown = [
            requirement_id
            for requirement_id in row["requirement_ids"]
            if project.requirement(requirement_id) is None
        ]
        if unknown:
            raise ValueError(
                f"Component {row['id']} references unknown requirement(s): "
                + ", ".join(unknown)
            )

    technology = data.get("technology") or {}
    if isinstance(technology, str):
        technology = {"stack": technology}
    if not isinstance(technology, Mapping):
        technology = {"stack": _strings(technology)}
    if not technology:
        missing.append("Technology selection")

    api_contracts = _objects(data.get("api_contracts"), ("title",))
    database_design = data.get("database_design") or {}
    if isinstance(database_design, str):
        database_design = {"summary": database_design}
    data_flows = _objects(data.get("data_flows"), ("title",))
    integrations = _objects(data.get("integrations"), ("title",))
    deployment = data.get("deployment") or data.get("deployment_architecture") or {}
    if isinstance(deployment, str):
        deployment = {"summary": deployment}
    security = data.get("security") or data.get("security_architecture") or {}
    if isinstance(security, str):
        security = {"summary": security}
    scalability = data.get("scalability") or {}
    if isinstance(scalability, str):
        scalability = {"summary": scalability}
    observability = data.get("observability") or {}
    if isinstance(observability, str):
        observability = {"summary": observability}

    for label, value in (
        ("API contracts", api_contracts),
        ("Database design", database_design),
        ("Data flows", data_flows),
        ("Integrations", integrations),
        ("Deployment architecture", deployment),
        ("Security architecture", security),
        ("Scalability considerations", scalability),
        ("Observability requirements", observability),
    ):
        if not value:
            missing.append(label)

    decisions = _decisions(project, data.get("decisions"))
    for decision in decisions:
        unknown = [
            requirement_id
            for requirement_id in decision["requirement_ids"]
            if project.requirement(requirement_id) is None
        ]
        if unknown:
            raise ValueError(
                f"Architecture decision {decision['id']} references unknown requirement(s): "
                + ", ".join(unknown)
            )
    if decisions and not any(row["major"] for row in decisions):
        missing.append("No architecture decision is marked major; confirm the decisions are complete.")
    now = utcnow().isoformat()
    return {
        "id": new_id("arch"),
        "version": int((project.architecture or {}).get("version", 0)) + 1,
        "status": "draft",
        "summary": _text(data.get("summary")),
        "requirements_version": prd.get("version"),
        "components": components,
        "api_contracts": api_contracts,
        "database_design": dict(database_design),
        "data_flows": data_flows,
        "integrations": integrations,
        "technology": dict(technology),
        "deployment": dict(deployment),
        "security": dict(security),
        "scalability": dict(scalability),
        "observability": dict(observability),
        "decisions": decisions,
        "missing_information": missing,
        "created_at": now,
        "updated_at": now,
        "created_by": actor,
        "approved_at": "",
        "approved_by": "",
        "approval_note": "",
    }


def approve_architecture(
    project: ProductProject, *, approved: bool, actor: str, note: str
) -> dict[str, Any]:
    architecture = project.architecture
    if architecture is None:
        raise ValueError("Generate a technical design before approving it.")
    if not actor.strip():
        raise ValueError("An architecture decision needs a named human approver.")
    if not note.strip():
        raise ValueError("An architecture decision needs a recorded note.")
    if architecture.get("status") == "approved" and not approved:
        raise ValueError("An approved architecture cannot be silently withdrawn; revise it.")
    now = utcnow().isoformat()
    architecture["status"] = "approved" if approved else "rejected"
    architecture["approved_at"] = now if approved else ""
    architecture["approved_by"] = actor if approved else ""
    architecture["approval_note"] = note
    architecture["updated_at"] = now
    for decision in architecture.get("decisions", []):
        decision["status"] = "approved" if approved else "rejected"
        decision["approved_by"] = actor if approved else ""
        decision["approved_at"] = now if approved else ""
    return architecture


__all__ = ["approve_architecture", "build_architecture"]
