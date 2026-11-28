"""Isolated, non-destructive product workspaces.

Every product gets its own directory under a configured root. Paths are
relative, traversal is refused, symlinks are refused, and an existing file is
never overwritten unless the caller presents the hash of what is already there.
The default root is the existing gitignored ``build_workspace/products`` path,
so product work cannot write into NovaControl's own source tree by accident.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novacontrol.product.errors import WorkspaceError

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_SKIP_PARTS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "releases",
}

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret|password|passwd|access[_-]?token)\b\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{8,}"
    ),
)


def safe_product_id(product_id: str) -> str:
    value = product_id.strip()
    if not _SAFE_ID.match(value):
        raise WorkspaceError("Product id contains characters that are unsafe in a path.")
    return value


def safe_relative_path(relative_path: str) -> str:
    raw = relative_path.replace("\\", "/").strip()
    if not raw or raw.startswith("/") or raw.startswith("../"):
        raise WorkspaceError("A workspace file path must be relative and non-empty.")
    if re.match(r"^[A-Za-z]:", raw):
        raise WorkspaceError("A workspace file path must not be an absolute drive path.")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise WorkspaceError("A workspace file path must stay inside the product workspace.")
    if any(part.lower() in _SKIP_PARTS for part in parts):
        raise WorkspaceError("That path is reserved by the workspace policy.")
    return "/".join(parts)


def redact_secrets(text: str) -> str:
    redacted = str(text)
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def contains_secret(text: str) -> bool:
    return any(pattern.search(str(text)) for pattern in _SECRET_PATTERNS)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True, slots=True)
class WrittenFile:
    path: str
    bytes: int
    sha256: str
    overwritten: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "overwritten": self.overwritten,
        }


class ProductWorkspace:
    """Owns one product's directory and the file-write policy for it."""

    def __init__(
        self,
        root: Path,
        *,
        max_files: int = 500,
        max_file_bytes: int = 1_000_000,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.max_files = max(1, int(max_files))
        self.max_file_bytes = max(1024, int(max_file_bytes))

    def path_for(self, product_id: str) -> Path:
        return self.root / safe_product_id(product_id)

    def create(self, product_id: str) -> Path:
        path = self.path_for(product_id)
        path.mkdir(parents=True, exist_ok=True)
        resolved = path.resolve()
        if not self._inside_root(resolved):
            raise WorkspaceError("The product workspace resolves outside the configured root.")
        return resolved

    def _inside_root(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
            return True
        except ValueError:
            return False

    def _resolve_file(self, product_id: str, relative_path: str) -> tuple[Path, str]:
        workspace = self.path_for(product_id).resolve()
        if not workspace.is_dir() or not self._inside_root(workspace):
            raise WorkspaceError("The product workspace has not been created yet.")
        safe_rel = safe_relative_path(relative_path)
        target = (workspace / safe_rel).resolve()
        if not self._inside_root(target):
            raise WorkspaceError("A workspace file path escapes the product workspace.")
        # Refuse writes through a symlinked parent or a symlinked target.
        probe = workspace
        for part in safe_rel.split("/"):
            probe = probe / part
            if probe.is_symlink():
                raise WorkspaceError("Workspace writes through symlinks are not allowed.")
        return target, safe_rel

    def write(
        self,
        product_id: str,
        relative_path: str,
        content: str,
        *,
        expected_sha256: str = "",
        overwrite: bool = False,
    ) -> WrittenFile:
        target, safe_rel = self._resolve_file(product_id, relative_path)
        encoded = str(content).encode("utf-8")
        if len(encoded) > self.max_file_bytes:
            raise WorkspaceError(
                f"A workspace file is limited to {self.max_file_bytes} bytes."
            )
        if contains_secret(content):
            raise WorkspaceError(
                "The file looks like it contains a credential or private key; "
                "store secrets in configuration, never in generated source."
            )
        existing_hash = ""
        if target.exists():
            if not target.is_file():
                raise WorkspaceError("The target exists and is not a regular file.")
            existing_hash = sha256_bytes(target.read_bytes())
            if not overwrite:
                raise WorkspaceError(
                    "Refusing to overwrite an existing workspace file. "
                    "Provide expected_sha256 and overwrite=true only for a reviewed change."
                )
            if not expected_sha256 or expected_sha256 != existing_hash:
                raise WorkspaceError(
                    "Overwrite refused: expected_sha256 does not match the existing file."
                )
        files = self.list_files(product_id)
        if len(files) >= self.max_files and not target.exists():
            raise WorkspaceError(f"The workspace file budget ({self.max_files}) is exhausted.")
        target.parent.mkdir(parents=True, exist_ok=True)
        # A final containment check after mkdir, before the bytes land.
        if not self._inside_root(target.resolve()):
            raise WorkspaceError("A workspace file path escapes the product workspace.")
        target.write_bytes(encoded)
        return WrittenFile(
            path=safe_rel,
            bytes=len(encoded),
            sha256=sha256_bytes(encoded),
            overwritten=bool(existing_hash),
        )

    def read(self, product_id: str, relative_path: str) -> str:
        target, _ = self._resolve_file(product_id, relative_path)
        if not target.is_file():
            raise WorkspaceError("That file is not in the product workspace.")
        return target.read_text(encoding="utf-8", errors="replace")

    def list_files(self, product_id: str) -> list[str]:
        workspace = self.path_for(product_id)
        if not workspace.is_dir():
            return []
        rows: list[str] = []
        for base, dirs, files in os.walk(workspace):
            dirs[:] = [name for name in dirs if name.lower() not in _SKIP_PARTS]
            for name in files:
                path = Path(base) / name
                if path.is_symlink():
                    continue
                rows.append(path.relative_to(workspace).as_posix())
        return sorted(rows)

    def scan(self, product_id: str) -> dict[str, Any]:
        files = self.list_files(product_id)
        violations: list[str] = []
        total_bytes = 0
        for relative in files:
            try:
                text = self.read(product_id, relative)
            except WorkspaceError:
                continue
            total_bytes += len(text.encode("utf-8"))
            if contains_secret(text):
                violations.append(relative)
        size = sum(
            (self.path_for(product_id) / item).stat().st_size
            for item in files
            if (self.path_for(product_id) / item).is_file()
        )
        return {
            "files": len(files),
            "bytes": size,
            "secret_violations": violations,
            "within_budget": len(files) <= self.max_files
            and total_bytes <= self.max_files * self.max_file_bytes,
        }

    def cleanup(self, product_id: str, *, approved: bool) -> dict[str, Any]:
        """Delete only this product's isolated workspace, after explicit approval."""
        if not approved:
            raise WorkspaceError("Workspace cleanup requires explicit approval.")
        import shutil

        path = self.path_for(product_id)
        if not path.is_dir():
            return {"removed": False, "path": str(path), "reason": "workspace does not exist"}
        if not self._inside_root(path.resolve()) or path.resolve() == self.root:
            raise WorkspaceError("Cleanup refused outside the configured product root.")
        shutil.rmtree(path)
        return {"removed": True, "path": str(path)}


__all__ = [
    "ProductWorkspace",
    "WrittenFile",
    "contains_secret",
    "redact_secrets",
    "safe_product_id",
    "safe_relative_path",
    "sha256_bytes",
]
