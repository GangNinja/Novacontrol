from __future__ import annotations

import unittest

from novacontrol.core.retry import RetryPolicy, with_retries


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_until_success(self) -> None:
        attempts = 0

        async def flaky() -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 2:
                raise RuntimeError("not yet")
            return "ok"

        result = await with_retries(flaky, RetryPolicy(max_attempts=2))

        self.assertEqual(result, "ok")
        self.assertEqual(attempts, 2)

    async def test_exhausts_attempts(self) -> None:
        async def failing() -> str:
            raise RuntimeError("still bad")

        with self.assertRaises(RuntimeError):
            await with_retries(failing, RetryPolicy(max_attempts=2))


if __name__ == "__main__":
    unittest.main()
