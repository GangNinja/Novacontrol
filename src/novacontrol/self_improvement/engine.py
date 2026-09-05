"""Self-coding engine for NovaControl."""

from __future__ import annotations

import ast
from pathlib import Path

from novacontrol.core.security import (
    ApprovalGateway,
    ApprovalRequest,
    DenyByDefaultApprovalGateway,
    PermissionScope,
    RiskLevel,
)
from novacontrol.self_improvement.models import (
    CodeChange,
    CodeChangeResult,
    CodeChangeStatus,
    CodeFinding,
    CodebaseProfile,
    ImprovementAction,
    SelfImprovementPlan,
)


class SelfImprovementEngine:
    """Inspects and safely improves the local NovaControl codebase."""

    ignored_parts = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", "data"}
    test_commands = (
        "python -m unittest discover -s tests",
        "python -m novacontrol health",
    )

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def inspect(self) -> CodebaseProfile:
        source_files = tuple(self._python_files(self.root / "src"))
        test_files = tuple(self._python_files(self.root / "tests"))
        packages = tuple(
            sorted(
                path.name
                for path in (self.root / "src" / "novacontrol").iterdir()
                if path.is_dir() and not path.name.startswith("__")
            )
        ) if (self.root / "src" / "novacontrol").exists() else ()
        line_count = sum(_line_count(path) for path in (*source_files, *test_files))
        return CodebaseProfile(
            root=str(self.root),
            source_files=len(source_files),
            test_files=len(test_files),
            python_lines=line_count,
            packages=packages,
        )

    def plan(self, goal: str) -> SelfImprovementPlan:
        profile = self.inspect()
        findings = self._findings()
        actions = self._actions_for(goal, findings)
        return SelfImprovementPlan(
            goal=goal,
            profile=profile,
            findings=findings,
            actions=actions,
            test_commands=self.test_commands,
            safety={
                "requires_approval_to_write": True,
                "allowed_root": str(self.root),
                "blocked_paths": sorted(self.ignored_parts),
                "verification_required": True,
            },
        )

    async def apply_changes(
        self,
        changes: tuple[CodeChange, ...],
        *,
        approval_gateway: ApprovalGateway | None = None,
    ) -> tuple[CodeChangeResult, ...]:
        gateway = approval_gateway or DenyByDefaultApprovalGateway()
        results: list[CodeChangeResult] = []
        for change in changes:
            target = self._safe_target(change.relative_path)
            approval = await gateway.request_approval(
                ApprovalRequest(
                    action=f"Apply code change to {change.relative_path}",
                    reason=change.description,
                    permissions=(PermissionScope.FILESYSTEM_WRITE,),
                    risk=RiskLevel.HIGH,
                    metadata=change.to_dict(),
                )
            )
            if not approval.approved:
                results.append(
                    CodeChangeResult(
                        change.id,
                        CodeChangeStatus.DENIED,
                        str(target),
                        approval.reason or "Code change was not approved.",
                        approval_id=approval.request_id,
                    )
                )
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(change.content, encoding="utf-8")
                results.append(
                    CodeChangeResult(
                        change.id,
                        CodeChangeStatus.APPLIED,
                        str(target),
                        "Change applied.",
                        approval_id=approval.request_id,
                    )
                )
            except OSError as exc:
                results.append(
                    CodeChangeResult(
                        change.id,
                        CodeChangeStatus.FAILED,
                        str(target),
                        str(exc),
                        approval_id=approval.request_id,
                    )
                )
        return tuple(results)

    def _findings(self) -> tuple[CodeFinding, ...]:
        findings: list[CodeFinding] = []
        source_files = tuple(self._python_files(self.root / "src"))
        test_files = tuple(self._python_files(self.root / "tests"))
        for path in (*source_files, *test_files):
            findings.extend(self._file_findings(path))
        if source_files and len(test_files) / len(source_files) < 0.35:
            findings.append(
                CodeFinding(
                    "warning",
                    "Test file count is low compared with source modules.",
                    "tests",
                )
            )
        if not findings:
            findings.append(CodeFinding("info", "No immediate structural problems found."))
        return tuple(findings)

    def _file_findings(self, path: Path) -> tuple[CodeFinding, ...]:
        relative = self._relative(path)
        findings: list[CodeFinding] = []
        try:
            text = path.read_text(encoding="utf-8")
            ast.parse(text)
        except SyntaxError as exc:
            findings.append(
                CodeFinding(
                    "error",
                    f"Python syntax error: {exc.msg}",
                    relative,
                    exc.lineno,
                )
            )
            text = path.read_text(encoding="utf-8", errors="replace")
        except UnicodeDecodeError as exc:
            return (CodeFinding("warning", f"Could not decode file: {exc}", relative),)

        lines = text.splitlines()
        if len(lines) > 450:
            findings.append(CodeFinding("info", "Large module may need focused refactoring.", relative, len(lines)))
        for index, line in enumerate(lines, start=1):
            stripped = line.strip()
            lowered = stripped.lower()
            if stripped.startswith("#") and ("todo" in lowered or "fixme" in lowered):
                findings.append(CodeFinding("info", "Open TODO/FIXME marker.", relative, index))
        return tuple(findings)

    def _actions_for(
        self,
        goal: str,
        findings: tuple[CodeFinding, ...],
    ) -> tuple[ImprovementAction, ...]:
        lower = goal.lower()
        actions: list[ImprovementAction] = []
        if any(finding.severity == "error" for finding in findings):
            actions.append(
                ImprovementAction(
                    "Fix blocking code errors",
                    "Repair syntax or import failures before adding new behavior.",
                    verification=("python -m unittest discover -s tests",),
                )
            )
        if "test" in lower or "reliable" in lower or "improve" in lower:
            actions.append(
                ImprovementAction(
                    "Add regression tests first",
                    "Create or update tests that prove the requested improvement works.",
                    target_files=("tests",),
                    verification=("python -m unittest discover -s tests",),
                )
            )
        if "code" in lower or "self" in lower or "improve" in lower or "intelligent" in lower:
            actions.append(
                ImprovementAction(
                    "Implement bounded source changes",
                    "Edit the smallest relevant modules, keep approval gates, and run health checks after tests.",
                    target_files=("src/novacontrol",),
                    verification=self.test_commands,
                )
            )
        actions.append(
            ImprovementAction(
                "Record improvement result",
                "Persist task outcome and summarize what changed for the dashboard/API user.",
                verification=("python -m novacontrol status",),
            )
        )
        return tuple(actions)

    def _safe_target(self, relative_path: str) -> Path:
        target = (self.root / relative_path).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"Path escapes project root: {relative_path}") from exc
        if any(part in self.ignored_parts for part in target.parts):
            raise ValueError(f"Path is blocked for self-coding: {relative_path}")
        return target

    def _python_files(self, root: Path) -> tuple[Path, ...]:
        if not root.exists():
            return ()
        return tuple(
            sorted(
                path
                for path in root.rglob("*.py")
                if not any(part in self.ignored_parts for part in path.parts)
            )
        )

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(path)


def _line_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except UnicodeDecodeError:
        return 0
