"""The knowledge engine as tools the agent can call (Phase 11.1 in the loop).

The three tools here are the engine's public verbs, and each one is a READ
except for the one that is a read of something not yet read:

* ``knowledge_search`` — rank what is already indexed. No permission is asked
  for because the filesystem work was already paid for at ingest time; a search
  is a question about local memory.
* ``knowledge_ingest`` — read a file or directory into the index. This touches
  the disk the agent has not touched yet, so it declares
  ``filesystem:read`` and the central policy decides.
* ``project_context`` — report the active project. Also
  ``filesystem:read``: the same fact, discovered by reading the workspace.

Nothing writes, nothing deletes, and nothing here reads a file the caller did
not name.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from novacontrol.core.security import PermissionScope
from novacontrol.knowledge.manager import KnowledgeManager
from novacontrol.knowledge.models import positive_int
from novacontrol.tools.models import ToolParameter, ToolSchema
from novacontrol.tools.registry import FunctionTool


def knowledge_tools(manager: KnowledgeManager) -> tuple[FunctionTool, ...]:
    """The knowledge engine's tools, bound to one manager."""

    async def search(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        query = str(arguments.get("query") or "").strip()
        limit = positive_int(arguments.get("limit"), default=5, maximum=25)
        project = str(arguments.get("project") or "").strip()
        hits = await manager.search(query, limit=limit, project=project or None)
        return {
            "query": query,
            "count": len(hits),
            "results": [hit.to_dict() for hit in hits],
            "backend": manager.embedding_backend,
            # Stated explicitly so a caller can tell "nothing is indexed" from
            # "nothing matched": the two need different next steps.
            "indexed_sources": len(manager.sources()),
        }

    async def ingest(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        path = str(arguments.get("path") or "").strip()
        if not path:
            return {"status": "failed", "reason": "a path is required"}
        project = str(arguments.get("project") or "").strip()
        force = _truthy(arguments.get("force"))
        report = await manager.ingest_path(path, project=project or None, force=force)
        return report.to_dict()

    async def context(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        path = str(arguments.get("path") or "").strip()
        project = manager.set_project(path) if path else manager.project_context()
        return project.to_dict()

    return (
        FunctionTool(
            "knowledge_search",
            ToolSchema(
                "knowledge_search",
                "Search the local knowledge index and return cited excerpts.",
                parameters=(
                    ToolParameter("query", "string", required=True, description="what to look for"),
                    ToolParameter("limit", "integer", description="how many excerpts (default 5)"),
                    ToolParameter("project", "string", description="restrict to one project"),
                ),
            ),
            search,
        ),
        FunctionTool(
            "knowledge_ingest",
            ToolSchema(
                "knowledge_ingest",
                "Read a file or directory into the local knowledge index.",
                parameters=(
                    ToolParameter("path", "string", required=True, description="file or directory"),
                    ToolParameter("project", "string", description="project to file it under"),
                    ToolParameter("force", "boolean", description="re-read even if unchanged"),
                ),
            ),
            ingest,
            required_permissions=(PermissionScope.FILESYSTEM_READ,),
        ),
        FunctionTool(
            "project_context",
            ToolSchema(
                "project_context",
                "Report the active code project: branch, language, recent files, errors, tests.",
                parameters=(
                    ToolParameter("path", "string", description="directory to inspect"),
                ),
            ),
            context,
            required_permissions=(PermissionScope.FILESYSTEM_READ,),
        ),
    )


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


__all__ = ["knowledge_tools"]
