"""Sensitive-value redaction for the operational audit trail.

The audit trail records what a request was, who (which layer) decided what, and
how it ended. What it must NOT record is the material that made the request
work in the first place: API keys, bearer tokens, passwords, private keys and
the credentials embedded in a URL. An audit log is read by more people than a
log line is, and it is kept for longer, so a secret that lands in it is a secret
that has leaked.

Two rules keep this honest:

  * **redaction happens on the way IN, not on the way out.** The logger redacts
    before anything is stored, so there is no field in which a secret briefly
    exists and no reader that has to remember to call a filter.
  * **a redaction is counted, and it is not a deletion of the request.** The
    record still says what happened; only the VALUE is replaced with
    ``[redacted]``, so an operator can see that a credential was handled
    without seeing the credential.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: What a redacted value is replaced with. One spelling, so a reader can grep
#: for it and a test can assert on it.
REDACTED = "[redacted]"

#: Keys whose VALUE is a secret whatever it looks like. A mapping is the one
#: place the field name is known, so it is the one place shape is not needed.
SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "credential",
        "credentials",
        "password",
        "passwd",
        "private_key",
        "pwd",
        "refresh_token",
        "secret",
        "token",
        "access_token",
    }
)


@dataclass(frozen=True, slots=True)
class RedactionRule:
    """One named pattern and how to replace it."""

    kind: str
    pattern: re.Pattern[str]
    #: Replacement template; group references are kept (so a ``key=value`` keeps
    #: its key and loses only the value).
    replacement: str = REDACTED


#: Ordered rules. Specific shapes first, so a private key block is not half
#: replaced by the generic ``key: value`` rule.
DEFAULT_RULES: tuple[RedactionRule, ...] = (
    RedactionRule(
        kind="private_key",
        pattern=re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
            r"(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
            re.S,
        ),
    ),
    RedactionRule(
        kind="authorization",
        pattern=re.compile(
            r"(?i)\b(authorization)\b(\s*[:=]\s*)"
            r"(?:bearer|basic|digest|token)?\s*\S+"
        ),
        replacement=r"\1\2" + REDACTED,
    ),
    RedactionRule(
        kind="bearer_token",
        pattern=re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{8,}"),
        replacement="Bearer " + REDACTED,
    ),
    RedactionRule(
        kind="url_credentials",
        pattern=re.compile(r"(?i)\b(https?://)[^/\s:@]+:[^/\s@]+@"),
        replacement=r"\1" + REDACTED + "@",
    ),
    RedactionRule(
        kind="jwt",
        pattern=re.compile(
            r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\b"
        ),
    ),
    RedactionRule(
        kind="api_key",
        pattern=re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}\b"),
    ),
    RedactionRule(kind="token", pattern=re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    RedactionRule(kind="token", pattern=re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    RedactionRule(kind="api_key", pattern=re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # key = value / key: value, with the value quoted or bare.
    RedactionRule(
        kind="secret_assignment",
        pattern=re.compile(
            r"(?i)\b(api[_-]?key|apikey|secret|client[_-]?secret|password|passwd|pwd|"
            r"access[_-]?token|refresh[_-]?token|token|credential|private[_-]?key)\b"
            r"(\s*[:=]\s*)([\"']?)([^\s\"',;]{3,})"
        ),
        replacement=r"\1\2\3" + REDACTED,
    ),
    # key <space> value, only when the value LOOKS like a credential. This is the
    # narrow half on purpose: "the token expired" is prose, and a redactor that
    # eats prose is a redactor people turn off.
    RedactionRule(
        kind="secret_argument",
        pattern=re.compile(
            r"(?i)\b(api[_-]?key|secret|password|passwd|pwd|token|access[_-]?token|"
            r"refresh[_-]?token)\b(\s+)([\"']?)([A-Za-z0-9_\-+/=.]{12,})"
        ),
        replacement=r"\1\2\3" + REDACTED,
    ),
)


@dataclass(frozen=True, slots=True)
class RedactionOutcome:
    """What one redaction did: the safe text, and what was taken out."""

    text: str
    count: int = 0
    kinds: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.count > 0


@dataclass(frozen=True, slots=True)
class RedactionReport:
    """Totals for one value tree (a record, a payload)."""

    count: int = 0
    kinds: tuple[str, ...] = field(default_factory=tuple)

    def merged(self, other: RedactionReport) -> RedactionReport:
        return RedactionReport(
            count=self.count + other.count,
            kinds=tuple(dict.fromkeys(self.kinds + other.kinds)),
        )


class Redactor:
    """Replaces secrets with a marker, counting what it removed."""

    def __init__(
        self,
        *,
        rules: Sequence[RedactionRule] | None = None,
        enabled: bool = True,
        max_depth: int = 6,
    ) -> None:
        self.rules = tuple(rules) if rules is not None else DEFAULT_RULES
        self.enabled = enabled
        self.max_depth = max(1, max_depth)

    # -- text ------------------------------------------------------------------

    def redact_text(self, text: str) -> RedactionOutcome:
        if not self.enabled or not text:
            return RedactionOutcome(text=str(text or ""))
        result = str(text)
        count = 0
        kinds: list[str] = []
        for rule in self.rules:
            result, hits = rule.pattern.subn(rule.replacement, result)
            if hits:
                count += hits
                kinds.append(rule.kind)
        return RedactionOutcome(result, count, tuple(dict.fromkeys(kinds)))

    # -- structures ------------------------------------------------------------

    def redact_value(self, value: Any, *, depth: int = 0) -> tuple[Any, RedactionReport]:
        """Redact a value tree, returning a JSON-safe copy and a report.

        A mapping's KEYS decide its values when the key names a secret, and this
        is the stronger of the two checks: a password field is a password field
        whatever the password looks like. Structural depth is bounded so a
        self-referential payload cannot spin here.
        """
        if depth > self.max_depth:
            return str(value), RedactionReport()
        if isinstance(value, Mapping):
            report = RedactionReport()
            cleaned: dict[str, Any] = {}
            for key, item in value.items():
                name = str(key)
                if self._is_sensitive_key(name):
                    cleaned[name] = REDACTED
                    report = report.merged(RedactionReport(1, ("sensitive_key",)))
                    continue
                safe, sub = self.redact_value(item, depth=depth + 1)
                cleaned[name] = safe
                report = report.merged(sub)
            return cleaned, report
        if isinstance(value, (list, tuple, set, frozenset)):
            report = RedactionReport()
            items: list[Any] = []
            for item in value:
                safe, sub = self.redact_value(item, depth=depth + 1)
                items.append(safe)
                report = report.merged(sub)
            return items, report
        if isinstance(value, str):
            outcome = self.redact_text(value)
            return outcome.text, RedactionReport(outcome.count, outcome.kinds)
        return value, RedactionReport()

    @staticmethod
    def _is_sensitive_key(name: str) -> bool:
        normalised = name.strip().lower().replace("-", "_").replace(" ", "_")
        return normalised in SENSITIVE_KEYS

    def report(self, value: Any) -> RedactionReport:
        """Just the tally for a value tree, without the copy."""
        return self.redact_value(value)[1]
