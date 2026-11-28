"""Release preparation for local, desktop, web, API or container products.

Phase 36 packages and documents a release. It never deploys, publishes, sends a
campaign or makes a commercial commitment. The generated archive is a source
bundle plus release documents and a manifest; a compiled artifact is only
included when the project's own build already produced it.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from novacontrol.product.models import ProductProject, RELEASE_TARGETS, new_id, utcnow
from novacontrol.product.quality import failing_gates, open_defects
from novacontrol.product.workspace import ProductWorkspace, redact_secrets

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
_DOC_NAMES = {
    "notes": "RELEASE_NOTES.md",
    "deployment": "DEPLOYMENT.md",
    "rollback": "ROLLBACK.md",
    "security": "SECURITY_CHECKLIST.md",
    "manifest": "manifest.json",
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


def _checks(project: ProductProject, inputs: Mapping[str, Any], scan: Mapping[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    gates = failing_gates(project)
    checks.append(
        {
            "id": "quality_gates",
            "status": "passed" if not gates else "failed",
            "detail": "All required gates passed." if not gates else "Failing: " + ", ".join(gates),
        }
    )
    checks.append(
        {
            "id": "build",
            "status": "passed" if project.build.get("validated") else "failed",
            "detail": (
                f"Build command exited {project.build.get('exit_code')}."
                if project.build
                else "No build result has been recorded."
            ),
        }
    )
    workspace_ok = not scan.get("secret_violations") and bool(scan.get("within_budget", True))
    checks.append(
        {
            "id": "workspace",
            "status": "passed" if workspace_ok else "failed",
            "detail": (
                f"{scan.get('files', 0)} file(s), {scan.get('bytes', 0)} byte(s); no secret "
                "patterns found and the file budget is respected."
                if workspace_ok
                else (
                    "Possible secret(s) in: " + ", ".join(scan.get("secret_violations", []))
                    if scan.get("secret_violations")
                    else "The workspace exceeds its file or byte budget."
                )
            ),
        }
    )
    config = project.environment
    config_problems = []
    if not _text(config.get("language")):
        config_problems.append("language is not set")
    if not _text(config.get("build_command")):
        config_problems.append("build_command is not set")
    if not project.environment.get("validated"):
        config_problems.append("environment has not been validated")
    checks.append(
        {
            "id": "configuration",
            "status": "passed" if not config_problems else "failed",
            "detail": "Environment configuration is complete and validated."
            if not config_problems
            else "; ".join(config_problems),
        }
    )
    migrations = _strings(inputs.get("migrations"))
    if not migrations:
        migration_status = "not_applicable"
        migration_detail = "No database migrations were supplied."
    else:
        missing = [
            item
            for item in migrations
            if not (Path(project.workspace_path) / item).is_file()
        ]
        migration_status = "failed" if missing else "passed"
        migration_detail = (
            "All supplied migration files exist."
            if not missing
            else "Missing migration file(s): " + ", ".join(missing)
        )
    checks.append(
        {"id": "database_migrations", "status": migration_status, "detail": migration_detail}
    )
    operational = inputs.get("operational") if isinstance(inputs.get("operational"), Mapping) else {}
    checks.append(
        {
            "id": "operational_requirements",
            "status": "passed" if operational else "warning",
            "detail": "Operational requirements were supplied."
            if operational
            else "No operational requirements were supplied; record monitoring and recovery before launch.",
        }
    )
    return checks


def _release_notes(project: ProductProject, inputs: Mapping[str, Any]) -> str:
    supplied = _text(inputs.get("notes"))
    if supplied:
        return supplied
    lines = [f"# {project.name} — release notes", ""]
    lines.append("These notes are generated from recorded project state; no unreported feature is claimed.")
    lines.append("")
    lines.append("## Completed tasks")
    completed = [row for row in project.tasks() if row.get("status") == "completed"]
    if completed:
        lines.extend(f"- {row['title']}" for row in completed)
    else:
        lines.append("- No development task has completed.")
    lines.append("")
    lines.append("## Verification")
    for gate in project.quality_gates:
        lines.append(f"- {gate['name']}: {gate.get('status')} — {gate.get('reason', '')}")
    defects = open_defects(project)
    lines.append("")
    lines.append("## Known open defects")
    if defects:
        lines.extend(
            f"- [{row['severity']}] {row['title']} ({row['status']})" for row in defects
        )
    else:
        lines.append("- None recorded.")
    lines.append("")
    lines.append("## Outstanding conditions and risks")
    if project.conditions:
        lines.extend(f"- Opportunity condition: {item}" for item in project.conditions)
    for risk in (project.plan or {}).get("risks", []):
        lines.append(f"- {risk.get('kind')}: {risk.get('description')}")
    if not project.conditions and not (project.plan or {}).get("risks"):
        lines.append("- None recorded.")
    lines.append("")
    lines.append("No external deployment, publication or commercial commitment was performed.")
    return "\n".join(lines)


def _deployment_docs(project: ProductProject) -> str:
    env = project.environment
    return "\n".join(
        [
            f"# {project.name} deployment notes",
            "",
            f"- Release target: {project.release_target}",
            f"- Language/runtime: {env.get('language', 'not recorded')}",
            f"- Build command: `{env.get('build_command', 'not recorded')}`",
            f"- Test command: `{env.get('test_command', 'not recorded')}`",
            f"- Dependencies: {', '.join(_strings(env.get('dependencies'))) or 'none recorded'}",
            f"- Services: {', '.join(_strings(env.get('services'))) or 'none recorded'}",
            "",
            "## Deployment boundary",
            "",
            "This document prepares a release. Phase 36 does not deploy it, publish it, "
            "run a paid campaign or push a commit. A human operator performs deployment "
            "through the deployment method appropriate to the target.",
            "",
            "## Required operator checks",
            "",
            "1. Build from the bundle in a clean environment.",
            "2. Run the test command and inspect the recorded results.",
            "3. Verify secrets/config are supplied outside the source bundle.",
            "4. Confirm rollback steps and monitoring are active.",
        ]
    )


def _rollback_plan(project: ProductProject, inputs: Mapping[str, Any]) -> str:
    supplied = _text(inputs.get("rollback_plan"))
    if supplied:
        return supplied
    previous = project.releases[-1] if project.releases else None
    previous_text = (
        f"restore {previous.get('version')} artifact at {previous.get('artifact', {}).get('path')}"
        if previous
        else "no previous product release exists; retain the source bundle and rebuild the last approved revision"
    )
    return "\n".join(
        [
            f"# {project.name} rollback plan",
            "",
            f"1. Stop the new release using the target's normal service controls.",
            f"2. Restore configuration and data snapshots; {previous_text}.",
            "3. Re-run the acceptance and regression gates.",
            "4. Record the rollback and its evidence in the product history.",
            "",
            "This plan was generated because no operator-supplied rollback plan was recorded.",
        ]
    )


def _security_checklist(project: ProductProject, scan: Mapping[str, Any]) -> list[dict[str, Any]]:
    gate = project.gate("security")
    checklist = [
        {
            "item": "security_quality_gate",
            "status": "passed" if gate and gate.get("status") == "passed" else "failed",
            "detail": (gate or {}).get("reason", "No security gate is configured."),
        },
        {
            "item": "secret_scan",
            "status": "passed" if not scan.get("secret_violations") else "failed",
            "detail": (
                "No credential-shaped strings were found in workspace source."
                if not scan.get("secret_violations")
                else "Review: " + ", ".join(scan.get("secret_violations", []))
            ),
        },
        {
            "item": "authorization_model",
            "status": "reviewed"
            if (project.architecture or {}).get("security")
            else "warning",
            "detail": "Security architecture is recorded."
            if (project.architecture or {}).get("security")
            else "No security architecture was recorded.",
        },
        {
            "item": "dependency_review",
            "status": "recorded"
            if any(row.get("kind") in {"dependency", "security"} and row.get("status") == "passed" for row in project.tests)
            else "warning",
            "detail": "Review the dependency report produced by the product's tooling.",
        },
    ]
    return checklist


def _artifact_manifest(
    project: ProductProject,
    *,
    version: str,
    files: Sequence[str],
    docs: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "product_id": project.id,
        "product": project.name,
        "version": version,
        "release_target": project.release_target,
        "opportunity_id": project.opportunity_id,
        "validation_state": project.validation_state,
        "source_bundle": True,
        "external_deployment_performed": False,
        "created_at": utcnow().isoformat(),
        "files": list(files),
        "documents": dict(docs),
    }


def prepare_release(
    project: ProductProject,
    workspace: ProductWorkspace,
    inputs: Mapping[str, Any],
    *,
    actor: str,
) -> dict[str, Any]:
    if project.archived_at:
        raise ValueError("An archived product cannot prepare a new release.")
    if project.cancelled:
        raise ValueError("A cancelled product cannot prepare a release.")
    gates = failing_gates(project)
    if gates:
        raise ValueError(
            "Release preparation requires every configured quality gate to pass. Failing: "
            + ", ".join(gates)
        )
    if not project.build.get("validated"):
        raise ValueError("Release preparation requires a recorded, successful build.")
    data = dict(inputs or {})
    version = _text(data.get("version")) or "0.1.0"
    if not _VERSION.match(version):
        raise ValueError("Release version must look like 1.2.3 (optional suffix allowed).")
    target = _text(data.get("target") or project.release_target).lower()
    if target not in RELEASE_TARGETS:
        raise ValueError(
            f"Release target must be one of: {', '.join(RELEASE_TARGETS)}"
        )
    project.release_target = target
    scan = workspace.scan(project.id)
    checks = _checks(project, data, scan)
    failed = [row["id"] for row in checks if row["status"] == "failed"]
    if failed:
        raise ValueError("Release checks failed: " + ", ".join(failed))
    files = workspace.list_files(project.id)
    notes = _release_notes(project, data)
    deployment = _deployment_docs(project)
    rollback = _rollback_plan(project, data)
    security = _security_checklist(project, scan)
    docs = {
        "notes": notes,
        "deployment": deployment,
        "rollback": rollback,
        "security": "\n".join(
            [f"# Security checklist — {project.name}", ""]
            + [f"- {row['item']}: {row['status']} — {row['detail']}" for row in security]
        ),
    }
    manifest = _artifact_manifest(project, version=version, files=files, docs=docs)
    release_dir = Path(project.workspace_path) / "releases"
    release_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = release_dir / f"{project.id}-{version}.zip"
    if artifact_path.exists():
        raise ValueError("That release artifact already exists; bump the version.")
    with zipfile.ZipFile(artifact_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in files:
            target_path = Path(project.workspace_path) / relative
            if target_path.is_file():
                archive.write(target_path, relative)
        for key, content in docs.items():
            archive.writestr(_DOC_NAMES[key], content)
        archive.writestr(
            _DOC_NAMES["manifest"],
            json.dumps(manifest, indent=2, sort_keys=True),
        )
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    release = {
        "id": new_id("release"),
        "version": version,
        "status": "draft",
        "target": target,
        "notes": notes,
        "artifact": {
            "path": str(artifact_path),
            "filename": artifact_path.name,
            "sha256": digest,
            "bytes": artifact_path.stat().st_size,
            "kind": "source_bundle",
            "external_deployment_performed": False,
        },
        "checks": checks,
        "security_checklist": security,
        "deployment_docs": deployment,
        "rollback_plan": rollback,
        "migration_checks": next(
            (row for row in checks if row["id"] == "database_migrations"), {}
        ),
        "operational_requirements": dict(data.get("operational") or {}),
        "known_defects": [row["id"] for row in open_defects(project)],
        "created_at": utcnow().isoformat(),
        "created_by": actor,
        "approved_at": "",
        "approved_by": "",
        "approval_note": "",
    }
    project.releases.append(release)
    return release


def approve_release(
    project: ProductProject,
    *,
    release_id: str,
    approved: bool,
    actor: str,
    note: str,
) -> dict[str, Any]:
    release = project.release(release_id)
    if release is None:
        raise ValueError(f"Unknown release: {release_id}")
    if not actor.strip():
        raise ValueError("A release decision needs a named human approver.")
    if not approved and not note.strip():
        raise ValueError("A release rejection needs a note.")
    if approved:
        gates = failing_gates(project)
        if gates:
            raise ValueError(
                "A release cannot be approved while quality gates fail: " + ", ".join(gates)
            )
        if not project.build.get("validated"):
            raise ValueError("A release cannot be approved without a validated build.")
        if not project.environment.get("validated"):
            raise ValueError(
                "A release cannot be approved after its environment validation was lost; "
                "validate the environment and prepare a new release."
            )
        artifact = release.get("artifact") or {}
        artifact_path = Path(str(artifact.get("path", "")))
        expected_digest = str(artifact.get("sha256", ""))
        if not artifact_path.is_file() or not expected_digest:
            raise ValueError("A release cannot be approved without its prepared artifact.")
        actual_digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if actual_digest != expected_digest:
            raise ValueError(
                "A release cannot be approved after its artifact changed; prepare a new release."
            )
    now = utcnow().isoformat()
    release["status"] = "approved" if approved else "rejected"
    release["approved_at"] = now if approved else ""
    release["approved_by"] = actor if approved else ""
    release["approval_note"] = note
    project.release_target = str(release.get("target") or project.release_target)
    return release


__all__ = ["approve_release", "prepare_release"]
