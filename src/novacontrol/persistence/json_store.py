"""Small JSON state store for local persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonStateStore:
    """Persists small module state snapshots as JSON files."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def read(self, name: str) -> dict[str, Any]:
        path = self.root / f"{name}.json"
        if not path.exists():
            return {}
        return dict(json.loads(path.read_text(encoding="utf-8")))

    def write(self, name: str, payload: dict[str, Any]) -> None:
        path = self.root / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
