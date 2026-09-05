"""CLI helper functions for NovaControl."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from novacontrol.core.security import ApprovalDecision, ApprovalRequest


def print_json(payload: Mapping[str, Any]) -> None:
    """Print a JSON payload to stdout."""
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


class AllowApprovalGateway:
    """Approval gateway that approves all requests (for demo/testing)."""

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(request.id, approved=True, decided_by="phase-demo")
