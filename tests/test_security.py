from __future__ import annotations

import unittest

from novacontrol.core.security import (
    ApprovalRequest,
    DenyByDefaultApprovalGateway,
    PermissionScope,
)


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_gateway_denies_sensitive_actions(self) -> None:
        gateway = DenyByDefaultApprovalGateway()
        request = ApprovalRequest(
            action="delete-file",
            reason="User requested cleanup",
            permissions=(PermissionScope.FILESYSTEM_WRITE,),
        )

        decision = await gateway.request_approval(request)

        self.assertEqual(decision.request_id, request.id)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.decided_by, "policy.deny_by_default")


if __name__ == "__main__":
    unittest.main()
