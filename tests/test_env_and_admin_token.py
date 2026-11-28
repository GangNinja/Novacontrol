"""Regression tests for project ``.env`` loading and the local admin opt-in.

Two failures prompted these:

* a ``.env`` written in PowerShell form (``$env:NAME = "value"``) was never
  read, so the SMTP transport kept reporting "SMTP is not configured";
* the retired launcher literal was refused outright, so an owner who pasted it
  as an API token was locked out of their own loopback application.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import pytest

from novacontrol.api.auth import (
    LEGACY_ADMIN_TOKEN,
    SecurityConfigurationError,
    load_security_policy,
)
from novacontrol.envloader import load_dotenv, parse_env_text

_LOOPBACK = ["127.0.0.1"]


class EnvParsingTests(unittest.TestCase):
    def test_parse_dotenv_export_and_powershell_forms(self) -> None:
        text = "\n".join(
            [
                "# comment",
                "",
                "NOVAMARKET_SMTP_HOST=smtp.example.com",
                'export NOVAMARKET_SMTP_USER="user@example.com"',
                '$env:NOVAMARKET_SMTP_PASSWORD = "p@ss#word"',
                '$env:NOVAMARKET_SMTP_FROM = "user@example.com"  # trailing note',
                "MALFORMED LINE WITHOUT EQUALS",
            ]
        )
        parsed = parse_env_text(text)
        self.assertEqual(parsed["NOVAMARKET_SMTP_HOST"], "smtp.example.com")
        self.assertEqual(parsed["NOVAMARKET_SMTP_USER"], "user@example.com")
        # A password containing '#' must survive: only " #" starts a comment.
        self.assertEqual(parsed["NOVAMARKET_SMTP_PASSWORD"], "p@ss#word")
        self.assertEqual(parsed["NOVAMARKET_SMTP_FROM"], "user@example.com")
        self.assertNotIn("MALFORMED LINE WITHOUT EQUALS", parsed)

    def test_load_dotenv_never_overrides_real_environment(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            env_file = Path(raw) / ".env"
            env_file.write_text(
                'EXISTING=from-file\n$env:INJECTED = "new"\n', encoding="utf-8"
            )
            target = {"EXISTING": "from-process"}
            applied = load_dotenv(env_file, environ=target)
        self.assertEqual(target["EXISTING"], "from-process")
        self.assertEqual(target["INJECTED"], "new")
        self.assertEqual(applied, ("INJECTED",))

    def test_missing_file_is_a_no_op(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            target: dict[str, str] = {}
            self.assertEqual(load_dotenv(Path(raw) / "absent.env", environ=target), ())
            self.assertEqual(target, {})


class LegacyAdminTokenTests(unittest.TestCase):
    def test_refused_without_the_explicit_opt_in(self) -> None:
        with pytest.raises(SecurityConfigurationError):
            load_security_policy(
                environ={"NOVACONTROL_ADMIN_TOKEN": LEGACY_ADMIN_TOKEN},
                trusted_hosts=_LOOPBACK,
            )

    def test_allowed_for_development_with_the_opt_in(self) -> None:
        policy = load_security_policy(
            environ={
                "NOVACONTROL_ADMIN_TOKEN": LEGACY_ADMIN_TOKEN,
                "NOVACONTROL_ALLOW_LEGACY_ADMIN_TOKEN": "1",
            },
            trusted_hosts=_LOOPBACK,
        )
        self.assertEqual(policy.legacy_admin_token, LEGACY_ADMIN_TOKEN)
        self.assertFalse(policy.production_like)

    def test_opt_in_is_ignored_in_a_production_like_environment(self) -> None:
        with pytest.raises(SecurityConfigurationError):
            load_security_policy(
                environ={
                    "NOVACONTROL_ENV": "production",
                    "NOVACONTROL_API_TOKEN": "x" * 40,
                    "NOVACONTROL_ADMIN_TOKEN": LEGACY_ADMIN_TOKEN,
                    "NOVACONTROL_ALLOW_LEGACY_ADMIN_TOKEN": "1",
                },
                trusted_hosts=_LOOPBACK,
            )

    def test_opt_in_does_not_endorse_other_weak_secrets(self) -> None:
        with pytest.raises(SecurityConfigurationError):
            load_security_policy(
                environ={
                    "NOVACONTROL_ADMIN_TOKEN": "password",
                    "NOVACONTROL_ALLOW_LEGACY_ADMIN_TOKEN": "1",
                },
                trusted_hosts=_LOOPBACK,
            )
