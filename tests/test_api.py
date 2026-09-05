from __future__ import annotations

import unittest

from novacontrol.api import ApiSurface, ApiTokenAuthenticator


class ApiTests(unittest.TestCase):
    def test_authenticator_allows_local_when_no_token_configured(self) -> None:
        result = ApiTokenAuthenticator().authenticate(None)

        self.assertTrue(result.authenticated)
        self.assertEqual(result.principal, "local")

    def test_authenticator_requires_bearer_token_when_configured(self) -> None:
        authenticator = ApiTokenAuthenticator("secret")

        self.assertFalse(authenticator.authenticate(None).authenticated)
        self.assertFalse(authenticator.authenticate("Bearer wrong").authenticated)
        self.assertTrue(authenticator.authenticate("Bearer secret").authenticated)

    def test_api_surface_includes_rest_and_websocket_routes(self) -> None:
        surface = ApiSurface.default()
        paths = [route.path for route in surface.routes]

        self.assertIn("/", paths)
        self.assertIn("/health", paths)
        self.assertIn("/ask", paths)
        self.assertIn("/improve", paths)
        self.assertIn("/improve/workflow", paths)
        self.assertIn("/improve/preview", paths)
        self.assertIn("/improve/approve", paths)
        self.assertIn("/learn", paths)
        self.assertIn("/train", paths)
        self.assertIn("/explore", paths)
        self.assertIn("/command/plan", paths)
        self.assertIn("/command/execute", paths)
        self.assertIn("/desktop/plan", paths)
        self.assertIn("/desktop/execute", paths)
        self.assertIn("/phone/status", paths)
        self.assertIn("/phone/plan", paths)
        self.assertIn("/phone/execute", paths)
        self.assertIn("/ws/events", surface.websocket_paths)


if __name__ == "__main__":
    unittest.main()
