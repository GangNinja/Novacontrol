"""Self-improvement domain models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class CodeChangeStatus(StrEnum):
    APPLIED = "applied"
    DENIED = "denied"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CodebaseProfile:
    root: str
    source_files: int
    test_files: int
    python_lines: int
    packages: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "source_files": self.source_files,
            "test_files": self.test_files,
            "python_lines": self.python_lines,
            "packages": list(self.packages),
        }


@dataclass(frozen=True, slots=True)
class CodeFinding:
    severity: str
    message: str
    path: str | None = None
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "message": self.message,
            "path": self.path,
            "line": self.line,
        }


@dataclass(frozen=True, slots=True)
class ImprovementAction:
    title: str
    description: str
    target_files: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "target_files": list(self.target_files),
            "verification": list(self.verification),
        }


@dataclass(frozen=True, slots=True)
class CodeChange:
    relative_path: str
    content: str
    description: str
    id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "relative_path": self.relative_path,
            "description": self.description,
            "content_bytes": len(self.content.encode("utf-8")),
        }


@dataclass(frozen=True, slots=True)
class CodeChangeResult:
    change_id: str
    status: CodeChangeStatus
    path: str
    message: str
    approval_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "status": self.status.value,
            "path": self.path,
            "message": self.message,
            "approval_id": self.approval_id,
        }


@dataclass(frozen=True, slots=True)
class SelfImprovementPlan:
    goal: str
    profile: CodebaseProfile
    findings: tuple[CodeFinding, ...]
    actions: tuple[ImprovementAction, ...]
    test_commands: tuple[str, ...]
    safety: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "profile": self.profile.to_dict(),
            "findings": [finding.to_dict() for finding in self.findings],
            "actions": [action.to_dict() for action in self.actions],
            "test_commands": list(self.test_commands),
            "safety": dict(self.safety),
        }
