"""Build-artifact workspace: where generated code lands when the user saves it.

The Build tab can draft code (LLM or deterministic plan) but until now nothing
ever wrote the result to disk — "nothing is getting coded". save_build_artifact
writes drafts into ``build_workspace/`` (gitignored, sibling of data/), with
filename sanitizing here so no crafted name can escape the folder.
"""

from __future__ import annotations

import re

# Extensions we are willing to write, per language. Anything else keeps a
# generic .txt so the workspace never holds executable-looking junk silently.
_CODE_EXT: dict[str, str] = {
    "python": ".py",
    "javascript": ".js",
    "typescript": ".ts",
    "java": ".java",
    "csharp": ".cs",
    "cpp": ".cpp",
    "c": ".c",
    "go": ".go",
    "rust": ".rs",
    "ruby": ".rb",
    "php": ".php",
    "swift": ".swift",
    "kotlin": ".kt",
    "html": ".html",
    "css": ".css",
    "sql": ".sql",
    "bash": ".sh",
    "shell": ".sh",
}


def safe_artifact_name(filename: str, language: str = "python") -> str:
    """Reduce a requested filename to a bare, safe artifact name.

    Strips every directory component (no traversal), keeps only sane filename
    characters, caps the stem length, and falls back to the language's default
    extension when the suffix is unknown or missing. Always returns a bare
    filename — the caller joins it onto the workspace directory itself.
    """
    requested = str(filename or "").strip()
    requested = re.sub(r"[\\/]+", "/", requested).rsplit("/", 1)[-1]
    stem, dot, suffix = requested.rpartition(".")
    if not dot or not re.fullmatch(r"[A-Za-z0-9_]{1,8}", suffix):
        stem, suffix = requested, _CODE_EXT.get(language, ".txt").lstrip(".")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_") or "artifact"
    return f"{stem[:60]}.{suffix}"


__all__ = ["safe_artifact_name", "_CODE_EXT"]
