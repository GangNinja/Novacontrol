"""Where verifiers live, and how the right one is chosen.

The registry is deliberately small: it keeps verifiers by id, refuses two
different implementations of the same id+version (that is tampering, not an
upgrade), keeps a fingerprint of the whole set so a run can prove the verifiers
it was trained against did not change underneath it, and answers discovery
questions by category and task type.

The selector is where "no LLM when a deterministic checker can decide" becomes
code. Given the task, the action, the expectation and the observation, it
derives which categories could decide the question, filters them by policy, and
returns the first deterministic candidate in the priority order the phase
promises:

    domain-specific deterministic verifier → schema → output/rule → AI (only
    when nothing deterministic matched, and only when policy allows it)

A selection that finds nothing returns ``ok=False`` with the reason, never a
guess: an unverifiable action must not be handed a made-up verifier.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novacontrol.evaluation.models import now_iso
from novacontrol.rlvr.config import VerifierPolicyConfig
from novacontrol.rlvr.models import (
    VERIFIER_CATEGORIES,
    VerificationRequest,
    VerifierMetadata,
    _text,
    _texts,
)
from novacontrol.rlvr.verifiers import Verifier, default_verifiers

#: Most specific first. A file expectation is answered by the file verifier
#: before the schema verifier is even considered, and a raw output comparison
#: comes last because it is the least informative check.
CATEGORY_PRIORITY: tuple[str, ...] = (
    "file",
    "process",
    "test",
    "http",
    "database",
    "git",
    "schema",
    "output",
    "custom",
)

#: Which expected keys imply which category. Ordered by the priority above when
#: several match: a test report that also carries an exit code is a test first.
_EXPECTED_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("file", ("path", "exists", "content", "contains", "sha256", "size", "entries")),
    ("process", ("state", "exit_code", "stdout_contains", "stderr_contains", "pid")),
    ("test", ("result", "min_passed", "max_failed", "report", "tests")),
    ("http", ("status", "status_in", "status_code", "max_latency_ms")),
    ("database", ("rows", "changes", "count", "values")),
    ("git", ("branch", "head_commit", "clean", "tracked", "modified", "untracked")),
    ("schema", ("schema", "required")),
    ("output", ("exact", "normalized", "structured")),
)


def _verifier_fingerprint(verifier: Verifier) -> str:
    """A stable digest of what a verifier IS: metadata plus its code.

    The code half matters: re-registering the same id and version with a
    different implementation is exactly the trick the security boundary exists
    to stop, and a fingerprint is how the registry can see it. A plugin's
    callable is hashed by qualname and bytecode when available.
    """
    parts: list[str] = [json.dumps(verifier.metadata().to_dict(), sort_keys=True, default=str)]
    target: Any = type(verifier)
    check = getattr(verifier, "_check", None)
    if callable(check):
        target = check
    try:
        source = inspect.getsource(target)
    except (OSError, TypeError):
        source = repr(target)
    parts.append(source)
    payload = "\n".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class VerifierRecord:
    """One registered verifier: its metadata, its fingerprint, its history."""

    metadata: VerifierMetadata = field(default_factory=VerifierMetadata)
    fingerprint: str = ""
    registered_at: str = field(default_factory=now_iso)
    disabled_reason: str = ""
    history: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.metadata.enabled and not self.disabled_reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "verifier_id": self.metadata.verifier_id,
            "category": self.metadata.category,
            "version": self.metadata.version,
            "deterministic": self.metadata.deterministic,
            "risk_level": self.metadata.risk_level,
            "enabled": self.enabled,
            "disabled_reason": self.disabled_reason,
            "fingerprint": self.fingerprint,
            "registered_at": self.registered_at,
            "history": list(self.history),
        }


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    """Which verifier was chosen for one question, and why — or why not."""

    verifier_id: str = ""
    category: str = ""
    deterministic: bool = False
    reason: str = ""
    candidates: tuple[str, ...] = ()
    fallback_used: bool = False
    ok: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "verifier_id": self.verifier_id,
            "category": self.category,
            "deterministic": self.deterministic,
            "reason": self.reason,
            "candidates": list(self.candidates),
            "fallback_used": self.fallback_used,
            "ok": self.ok,
        }


class VerifierRegistry:
    """The set of verifiers this installation has, and their integrity state."""

    def __init__(
        self,
        verifiers: Sequence[Verifier] = (),
        *,
        policy: VerifierPolicyConfig | None = None,
    ) -> None:
        self.policy = policy if policy is not None else VerifierPolicyConfig()
        self._verifiers: dict[str, Verifier] = {}
        self._records: dict[str, VerifierRecord] = {}
        for verifier in verifiers or default_verifiers():
            self.register(verifier)

    # -- registration ----------------------------------------------------------

    def register(self, verifier: Verifier, *, replace: bool = False) -> VerifierMetadata:
        """Add a verifier, refusing a silent swap of an existing id+version.

        A NEWER version of an existing id is an upgrade and is kept with its
        history. The SAME version with a different implementation is refused:
        the numbers would claim it is the verifier a finished run was trained
        against while the code had changed underneath.
        """
        metadata = verifier.metadata()
        if not metadata.verifier_id:
            raise ValueError("a verifier must have a verifier_id")
        if not metadata.known:
            raise ValueError("a verifier must carry a verifier_id and a version")
        fingerprint = _verifier_fingerprint(verifier)
        existing = self._records.get(metadata.verifier_id)
        if existing is not None:
            same_version = existing.metadata.version == metadata.version
            same_fingerprint = existing.fingerprint == fingerprint
            if same_version and same_fingerprint and not replace:
                return existing.metadata
            if same_version and not same_fingerprint and not replace:
                raise ValueError(
                    f"verifier {metadata.verifier_id!r} version {metadata.version!r} "
                    "is already registered with different code or metadata: a "
                    "verifier cannot be modified in place — publish a new version"
                )
            history = tuple(dict.fromkeys((*existing.history, existing.metadata.version)))
            self._records[metadata.verifier_id] = VerifierRecord(
                metadata=metadata,
                fingerprint=fingerprint,
                history=history,
            )
        else:
            self._records[metadata.verifier_id] = VerifierRecord(
                metadata=metadata, fingerprint=fingerprint
            )
        self._verifiers[metadata.verifier_id] = verifier
        return self._records[metadata.verifier_id].metadata

    def unregister(self, verifier_id: str) -> bool:
        """Remove a verifier entirely (its record is dropped, not hidden)."""
        wanted = _text(verifier_id)
        found = self._verifiers.pop(wanted, None)
        self._records.pop(wanted, None)
        return found is not None

    def disable(self, verifier_id: str, *, reason: str = "") -> bool:
        """Withdraw a verifier without deleting it: history stays readable."""
        wanted = _text(verifier_id)
        record = self._records.get(wanted)
        if record is None:
            return False
        self._records[wanted] = VerifierRecord(
            metadata=record.metadata,
            fingerprint=record.fingerprint,
            registered_at=record.registered_at,
            disabled_reason=_text(reason, "disabled by an operator"),
            history=record.history,
        )
        return True

    def enable(self, verifier_id: str) -> bool:
        wanted = _text(verifier_id)
        record = self._records.get(wanted)
        if record is None:
            return False
        self._records[wanted] = VerifierRecord(
            metadata=record.metadata,
            fingerprint=record.fingerprint,
            registered_at=record.registered_at,
            history=record.history,
        )
        return True

    # -- reading ---------------------------------------------------------------

    def get(self, verifier_id: str) -> Verifier | None:
        record = self._records.get(_text(verifier_id))
        if record is None or not record.enabled:
            return None
        return self._verifiers.get(record.metadata.verifier_id)

    def record(self, verifier_id: str) -> VerifierRecord | None:
        return self._records.get(_text(verifier_id))

    def metadata(self, verifier_id: str) -> VerifierMetadata | None:
        found = self._verifiers.get(_text(verifier_id))
        return found.metadata() if found is not None else None

    def list(
        self,
        *,
        category: str = "",
        task_type: str = "",
        deterministic: bool | None = None,
        enabled_only: bool = True,
    ) -> tuple[VerifierMetadata, ...]:
        wanted_category = _text(category).lower()
        found: list[VerifierMetadata] = []
        for verifier_id in sorted(self._verifiers):
            record = self._records.get(verifier_id)
            if record is None:
                continue
            if enabled_only and not record.enabled:
                continue
            metadata = record.metadata
            if wanted_category and metadata.category.lower() != wanted_category:
                continue
            if not metadata.supports(task_type):
                continue
            if deterministic is not None and metadata.deterministic != deterministic:
                continue
            found.append(metadata)
        return tuple(found)

    def discover(self, **filters: Any) -> tuple[VerifierMetadata, ...]:
        """Alias for :meth:`list`, named for what a caller is doing."""
        return self.list(**filters)

    def validate(self, verifier_id: str) -> tuple[str, ...]:
        """Every reason this verifier is not usable, if any."""
        wanted = _text(verifier_id)
        record = self._records.get(wanted)
        problems: list[str] = []
        if record is None:
            return (f"no verifier {wanted!r} is registered",)
        if not record.enabled:
            problems.append(f"verifier {wanted!r} is disabled: {record.disabled_reason}")
        metadata = record.metadata
        if not metadata.version:
            problems.append("the verifier carries no version")
        if not metadata.deterministic and self.policy.deterministic_only:
            problems.append(
                "the verifier is non-deterministic and this policy allows only "
                "deterministic verifiers"
            )
        if metadata.category not in VERIFIER_CATEGORIES:
            problems.append(f"unknown verifier category {metadata.category!r}")
        if metadata.category == "custom" and not self.policy.allow_custom:
            problems.append("custom verifiers are switched off by policy")
        if not self.policy.allows(metadata.category):
            problems.append(f"category {metadata.category!r} is not allowed by policy")
        return tuple(problems)

    def describe(self) -> dict[str, Any]:
        records = [record.to_dict() for record in self._records.values()]
        by_category: dict[str, int] = {}
        for record in self._records.values():
            key = record.metadata.category
            by_category[key] = by_category.get(key, 0) + 1
        return {
            "verifiers": records,
            "count": len(records),
            "enabled": sum(1 for record in self._records.values() if record.enabled),
            "deterministic": sum(
                1 for record in self._records.values() if record.metadata.deterministic
            ),
            "by_category": by_category,
            "fingerprint": self.fingerprint(),
            "policy": self.policy.to_mapping(),
        }

    # -- integrity ---------------------------------------------------------------

    def snapshot(self) -> dict[str, str]:
        """Id -> fingerprint, for proving the set did not change under a run."""
        return {
            verifier_id: record.fingerprint
            for verifier_id, record in self._records.items()
        }

    def fingerprint(self) -> str:
        payload = json.dumps(self.snapshot(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def assert_unmodified(self, snapshot: Mapping[str, str]) -> tuple[str, ...]:
        """Every difference from a snapshot: added, removed or changed verifiers."""
        current = self.snapshot()
        problems: list[str] = []
        for verifier_id, fingerprint in dict(snapshot).items():
            found = current.get(verifier_id)
            if found is None:
                problems.append(f"verifier {verifier_id!r} was removed after the run started")
            elif found != fingerprint:
                problems.append(
                    f"verifier {verifier_id!r} changed after the run started "
                    f"({fingerprint} -> {found})"
                )
        for verifier_id in current:
            if verifier_id not in snapshot:
                problems.append(f"verifier {verifier_id!r} was added after the run started")
        return tuple(problems)

    # -- selection ---------------------------------------------------------------

    def select(
        self,
        *,
        request: VerificationRequest | None = None,
        action: Mapping[str, Any] | None = None,
        expected: Mapping[str, Any] | None = None,
        observation: Mapping[str, Any] | None = None,
        task_type: str = "",
    ) -> SelectionDecision:
        """Choose the most specific deterministic verifier that can decide."""
        return VerifierSelector(self).select(
            request=request,
            action=action,
            expected=expected,
            observation=observation,
            task_type=task_type,
        )


class VerifierSelector:
    """Picks a verifier from a registry: rules first, opinions last."""

    def __init__(self, registry: VerifierRegistry) -> None:
        self.registry = registry

    def select(
        self,
        *,
        request: VerificationRequest | None = None,
        action: Mapping[str, Any] | None = None,
        expected: Mapping[str, Any] | None = None,
        observation: Mapping[str, Any] | None = None,
        task_type: str = "",
    ) -> SelectionDecision:
        policy = self.registry.policy
        if not policy.enabled:
            return SelectionDecision(reason="verification is switched off by policy")
        if request is not None and request.verifier_id:
            problems = self.registry.validate(request.verifier_id)
            if problems:
                return SelectionDecision(
                    verifier_id=request.verifier_id,
                    reason="; ".join(problems),
                    candidates=(request.verifier_id,),
                )
            metadata = self.registry.metadata(request.verifier_id)
            if metadata is None:
                return SelectionDecision(reason=f"no verifier {request.verifier_id!r}")
            return SelectionDecision(
                verifier_id=metadata.verifier_id,
                category=metadata.category,
                deterministic=metadata.deterministic,
                reason="the request named this verifier",
                candidates=(metadata.verifier_id,),
                ok=True,
            )
        expected = dict(expected or (request.expected if request is not None else {}))
        observation = dict(
            observation or (request.observation if request is not None else {})
        )
        action = dict(action or (request.action if request is not None else {}))
        categories = self._candidates(expected, observation, action)
        candidates: list[str] = []
        for category in categories:
            if not policy.allows(category):
                continue
            if category == "custom" and not policy.allow_custom:
                continue
            for metadata in self.registry.list(category=category):
                if policy.deterministic_only and not metadata.deterministic:
                    continue
                candidates.append(metadata.verifier_id)
        chosen = candidates[0] if candidates else ""
        if chosen:
            metadata = self.registry.metadata(chosen)
            category = metadata.category if metadata is not None else ""
            deterministic = bool(metadata.deterministic) if metadata is not None else False
            return SelectionDecision(
                verifier_id=chosen,
                category=category,
                deterministic=deterministic,
                reason=(
                    f"a deterministic {category} verifier can decide this question"
                    if deterministic
                    else f"a {category} verifier can decide this question"
                ),
                candidates=tuple(candidates[: policy.max_verifiers]),
                ok=True,
            )
        fallback = bool(policy.allow_ai_evaluator)
        return SelectionDecision(
            reason=(
                "no deterministic verifier matches this expectation; an AI "
                "evaluator fallback is permitted by policy but must be asked "
                "for explicitly"
                if fallback
                else "no deterministic verifier matches this expectation and no "
                "fallback is permitted by policy"
            ),
            fallback_used=False,
            ok=False,
        )

    def category_for(
        self,
        expected: Mapping[str, Any],
        observation: Mapping[str, Any] | None = None,
        action: Mapping[str, Any] | None = None,
    ) -> str:
        """The first implied category, or empty when nothing matches."""
        found = self._candidates(dict(expected), dict(observation or {}), dict(action or {}))
        return found[0] if found else ""

    def _candidates(
        self,
        expected: Mapping[str, Any],
        observation: Mapping[str, Any],
        action: Mapping[str, Any],
    ) -> list[str]:
        """Candidate categories, the ones the EXPECTATION names first.

        What the expectation names is the question; what the action and the
        observation look like is only a hint about who can answer it. A step
        that happened to write a file but whose expectation is an output match
        must be decided by the output verifier, not by the file verifier the
        action superficially suggests — so expected-derived categories are
        ranked ahead of action-derived ones, each in the canonical order.
        """
        implied: set[str] = set()
        from_expected: set[str] = set()
        for category, keys in _EXPECTED_HINTS:
            if any(key in expected for key in keys):
                implied.add(category)
                from_expected.add(category)
        for key, category in (
            ("file", "file"),
            ("process", "process"),
            ("tests", "test"),
            ("http", "http"),
            ("database", "database"),
            ("git", "git"),
            ("output", "output"),
            ("value", "schema"),
        ):
            if key in observation and category not in implied:
                if category == "schema" and "schema" not in expected:
                    continue
                implied.add(category)
        for key, category in (
            ("tool", "process"),
            ("command", "process"),
            ("path", "file"),
            ("url", "http"),
        ):
            value = action.get(key)
            if value and category not in implied and category in _allowed_verbs(action):
                implied.add(category)
        ranked = [name for name in CATEGORY_PRIORITY if name in from_expected]
        ranked.extend(
            name
            for name in CATEGORY_PRIORITY
            if name in implied and name not in from_expected
        )
        return ranked


def _allowed_verbs(action: Mapping[str, Any]) -> set[str]:
    """Categories an action's own shape suggests, without over-reading it."""
    kind = _text(action.get("action") or action.get("kind") or action.get("tool")).lower()
    found: set[str] = set()
    if any(word in kind for word in ("write", "file", "save", "build", "artifact")):
        found.add("file")
    if any(word in kind for word in ("run", "command", "process", "execute", "exec")):
        found.add("process")
    if any(word in kind for word in ("test", "pytest", "verify")):
        found.add("test")
    if any(word in kind for word in ("http", "request", "fetch", "api", "url")):
        found.add("http")
    if any(word in kind for word in ("database", "query", "sql", "record")):
        found.add("database")
    if any(word in kind for word in ("git", "commit", "branch", "repo")):
        found.add("git")
    if any(word in kind for word in ("output", "answer", "response", "format")):
        found.add("output")
    if any(word in kind for word in ("schema", "json", "validate")):
        found.add("schema")
    if not found:
        found.update(_texts(action.get("verifier_categories")))
    return found


__all__ = [
    "CATEGORY_PRIORITY",
    "SelectionDecision",
    "VerifierRecord",
    "VerifierRegistry",
    "VerifierSelector",
]
