"""NovaForge objective verification (readiness remediation R2).

The audit's second blocker: a session that stopped, a benchmark that completed
and an evaluation that ran all said "completed", and none of them meant the
objective was met. There was no verifier at all, so ``objective.verified`` was
``false`` everywhere and ``achieved`` was ``null``.

This module adds the missing layer with three rules that make it honest:

* **The verifier is independent of the thing it verifies.** A game verifier may
  not read the action record it is checking, or the session's own claim. The
  one verifier this build ships reads the TARGET WINDOW's title from the OS
  through the same geometry probe capture uses — a fact the input path cannot
  write to. That is what makes "the harness window showed ``tick 1``" evidence
  rather than a restatement of "we pressed Enter".
* **Unknown is a real answer.** A probe that cannot read the window, an
  unsupported objective kind and an ambiguous observation all return
  ``unknown``/``unsupported`` with a reason. Nothing guesses success.
* **Lifecycle and outcome are separate.** ``SessionState`` says whether the
  session is running; :class:`ObjectiveVerification` says whether the objective
  was met. A stopped session is not a success and a verified success is not a
  session state.

The supported kinds are intentionally few and checkable. ``window_title`` is the
disposable-harness objective: a small test window retitles itself when input
arrives, so the whole chain (action → title change → independent read) can be
demonstrated end to end without a game.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "OBJECTIVE_KINDS",
    "ObjectiveDefinition",
    "ObjectiveEvidence",
    "ObjectiveKind",
    "ObjectiveOutcome",
    "ObjectiveRegistry",
    "ObjectiveVerification",
    "WindowProbe",
    "WindowTitleVerifier",
    "utc_stamp",
]


def utc_stamp() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


class ObjectiveOutcome(StrEnum):
    """What verification can say. Only two of these are a game outcome."""

    VERIFIED_SUCCESS = "verified_success"
    VERIFIED_FAILURE = "verified_failure"
    IN_PROGRESS = "in_progress"
    UNKNOWN = "unknown"
    VERIFICATION_ERROR = "verification_error"
    UNSUPPORTED = "unsupported"

    @property
    def decided(self) -> bool:
        """Whether this is a final, evidence-backed game outcome."""
        return self in {ObjectiveOutcome.VERIFIED_SUCCESS, ObjectiveOutcome.VERIFIED_FAILURE}


class ObjectiveKind(StrEnum):
    """Objective shapes this build can actually check."""

    #: The target window's title currently contains / equals / lacks a value.
    WINDOW_TITLE = "window_title"
    #: Declared so an unsupported objective can be recorded as a DEFINITION
    #: rather than as a broken verifier. Nothing implements it.
    UNSUPPORTED = "unsupported"


OBJECTIVE_KINDS = frozenset(kind.value for kind in ObjectiveKind)

#: The three comparisons a title objective may ask for. ``absent`` is what makes
#: a "the error dialog is gone" objective checkable.
TITLE_MODES = frozenset({"contains", "equals", "absent"})


@dataclass(frozen=True, slots=True)
class ObjectiveDefinition:
    """What "success" means for this session, in checkable terms.

    ``parameter`` is the value the verifier compares against (a title token, for
    the one supported kind). ``kind="unsupported"`` is a legal DEFINITION: it
    records that the operator asked for something this build cannot check, so
    every read says ``unsupported`` with that reason instead of pretending the
    objective set is empty.
    """

    kind: ObjectiveKind
    parameter: str = ""
    mode: str = "contains"
    description: str = ""

    MAX_PARAMETER = 200
    MAX_DESCRIPTION = 300

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "ObjectiveDefinition":
        data = dict(payload or {})
        raw = str(data.get("kind", "") or "").strip().lower()
        if raw not in OBJECTIVE_KINDS:
            raise ValueError(
                f"Unknown objective kind {raw or '(blank)'!r}; expected one of "
                f"{', '.join(sorted(OBJECTIVE_KINDS))}."
            )
        return cls(
            kind=ObjectiveKind(raw),
            parameter=str(data.get("parameter", "") or "").strip(),
            mode=str(data.get("mode", "contains") or "contains").strip().lower(),
            description=str(data.get("description", "") or "").strip(),
        )

    def errors(self) -> list[str]:
        problems: list[str] = []
        if self.kind is ObjectiveKind.WINDOW_TITLE:
            if not self.parameter:
                problems.append("A window_title objective needs the title text to compare.")
            elif len(self.parameter) > self.MAX_PARAMETER:
                problems.append(f"The title parameter must be at most {self.MAX_PARAMETER} characters.")
            if self.mode not in TITLE_MODES:
                problems.append(
                    f"Unknown comparison {self.mode!r}; expected one of {', '.join(sorted(TITLE_MODES))}."
                )
        if len(self.description) > self.MAX_DESCRIPTION:
            problems.append(f"The description must be at most {self.MAX_DESCRIPTION} characters.")
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "parameter": self.parameter,
            "mode": self.mode,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "ObjectiveDefinition | None":
        try:
            return cls.from_payload(payload)
        except ValueError:
            return None

    def digest(self) -> str:
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ObjectiveEvidence:
    """One fact the verifier read, and where it read it from."""

    method: str
    observed: str
    expected: str
    at: str
    window_handle: int | None = None
    window_pid: int | None = None
    window_title: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "observed": self.observed,
            "expected": self.expected,
            "at": self.at,
            "window_handle": self.window_handle,
            "window_pid": self.window_pid,
            "window_title": self.window_title,
            "note": self.note,
        }

    def reference(self) -> dict[str, Any]:
        """A compact timeline evidence reference to this observation."""
        return {
            "type": "objective_probe",
            "source": self.method,
            "handle": self.window_handle,
            "pid": self.window_pid,
            "at": self.at,
        }


@dataclass(frozen=True, slots=True)
class ObjectiveVerification:
    """The verdict, its evidence and the reason a human needs to trust it."""

    outcome: ObjectiveOutcome
    verified: bool | None
    method: str
    reason: str
    at: str
    definition: dict[str, Any]
    evidence: tuple[ObjectiveEvidence, ...] = ()
    confidence: float | None = None
    session_id: str = ""
    action_id: str = ""

    @property
    def achieved(self) -> bool | None:
        """Whether the objective is MET. None when verification did not decide."""
        return True if self.outcome is ObjectiveOutcome.VERIFIED_SUCCESS else (
            False if self.outcome is ObjectiveOutcome.VERIFIED_FAILURE else None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "verified": self.verified,
            "achieved": self.achieved,
            "decided": self.outcome.decided,
            "method": self.method,
            "reason": self.reason,
            "at": self.at,
            "definition": dict(self.definition),
            "evidence": [item.to_dict() for item in self.evidence],
            "confidence": self.confidence,
            "session_id": self.session_id,
            "action_id": self.action_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "ObjectiveVerification | None":
        data = dict(payload or {})
        try:
            outcome = ObjectiveOutcome(str(data.get("outcome", "")))
        except ValueError:
            return None
        evidence: list[ObjectiveEvidence] = []
        for row in data.get("evidence") or []:
            if not isinstance(row, Mapping):
                continue
            evidence.append(
                ObjectiveEvidence(
                    method=str(row.get("method", "") or ""),
                    observed=str(row.get("observed", "") or ""),
                    expected=str(row.get("expected", "") or ""),
                    at=str(row.get("at", "") or ""),
                    window_handle=row.get("window_handle"),
                    window_pid=row.get("window_pid"),
                    window_title=str(row.get("window_title", "") or ""),
                    note=str(row.get("note", "") or ""),
                )
            )
        verified = data.get("verified")
        return cls(
            outcome=outcome,
            verified=verified if isinstance(verified, bool) else None,
            method=str(data.get("method", "") or ""),
            reason=str(data.get("reason", "") or ""),
            at=str(data.get("at", "") or ""),
            definition=dict(data.get("definition") or {}),
            evidence=tuple(evidence),
            confidence=data.get("confidence") if isinstance(data.get("confidence"), (int, float)) else None,
            session_id=str(data.get("session_id", "") or ""),
            action_id=str(data.get("action_id", "") or ""),
        )


#: A read of one window's current geometry (the same shape capture produces).
WindowProbe = Callable[[int], Awaitable[Any | None]]


@dataclass(slots=True)
class WindowTitleVerifier:
    """The one supported verifier: the target window's title, read from the OS.

    ``probe`` takes a window handle and returns a geometry-like object with
    ``title``/``handle``/``pid`` — the application wires it to the capture
    worker's ``describe <handle>``. Because the probe reads the WINDOW and not
    the session, an action that never reached the window cannot make this pass,
    and an action that did reach it cannot be hidden by the session's own state.
    """

    probe: WindowProbe
    supported_kinds: frozenset[str] = frozenset({ObjectiveKind.WINDOW_TITLE.value})

    def supports(self, definition: ObjectiveDefinition) -> bool:
        return definition.kind.value in self.supported_kinds and not definition.errors()

    async def verify(
        self,
        definition: ObjectiveDefinition,
        *,
        handle: int,
        pid: int | None = None,
        session_id: str = "",
        action_id: str = "",
    ) -> ObjectiveVerification:
        at = utc_stamp()
        base: dict[str, Any] = {
            "session_id": session_id,
            "action_id": action_id,
            "at": at,
            "definition": definition.to_dict(),
        }

        def result(outcome: ObjectiveOutcome, *, verified: bool | None, reason: str,
                   evidence: tuple[ObjectiveEvidence, ...] = (), confidence: float | None = None,
                   method: str = "window_title") -> ObjectiveVerification:
            return ObjectiveVerification(
                outcome=outcome, verified=verified, method=method, reason=reason,
                evidence=evidence, confidence=confidence, **base,
            )

        problems = definition.errors()
        if problems:
            return result(
                ObjectiveOutcome.UNSUPPORTED, verified=None,
                reason="The objective definition is not checkable: " + "; ".join(problems),
            )

        try:
            geometry = await self.probe(handle)
        except Exception as exc:  # noqa: BLE001 - a probe failure is UNKNOWN, not a pass
            return result(
                ObjectiveOutcome.VERIFICATION_ERROR, verified=None,
                reason=f"The window probe failed: {type(exc).__name__}: {exc}",
            )
        if geometry is None:
            return result(
                ObjectiveOutcome.UNKNOWN, verified=None,
                reason=(
                    "The target window could not be read, so the objective is UNKNOWN — "
                    "a window that is gone is not a satisfied objective."
                ),
            )

        observed_handle = int(getattr(geometry, "handle", 0) or 0)
        observed_pid = int(getattr(geometry, "pid", 0) or 0)
        title = str(getattr(geometry, "title", "") or "")
        evidence = (
            ObjectiveEvidence(
                method="window_title",
                observed=title,
                expected=f"{definition.mode} {definition.parameter!r}",
                at=at,
                window_handle=observed_handle,
                window_pid=observed_pid,
                window_title=title,
            ),
        )
        if observed_handle != handle:
            return result(
                ObjectiveOutcome.UNKNOWN, verified=None,
                evidence=evidence,
                reason=(
                    f"The probe read window {observed_handle} instead of the pinned handle "
                    f"{handle}; the objective cannot be attributed to this session's target."
                ),
            )
        if pid is not None and observed_pid != pid:
            return result(
                ObjectiveOutcome.UNKNOWN, verified=None, evidence=evidence,
                reason=(
                    f"The window's process changed ({observed_pid} != {pid}); another window "
                    "now owns the handle, so the observation is not this session's target."
                ),
            )

        needle = definition.parameter
        if definition.mode == "equals":
            # Case-insensitive: a title's casing is the OS's choice, not evidence
            # about the objective, and a case-sensitive comparison would turn a
            # satisfied objective into a failure for a reason nobody asked for.
            matched = title.strip().casefold() == needle.casefold()
            detail = f"the title is exactly {needle!r}"
        elif definition.mode == "absent":
            matched = needle.lower() not in title.lower()
            detail = f"the title does not contain {needle!r}"
        else:
            matched = needle.lower() in title.lower()
            detail = f"the title contains {needle!r}"

        return result(
            ObjectiveOutcome.VERIFIED_SUCCESS if matched else ObjectiveOutcome.VERIFIED_FAILURE,
            verified=True,
            confidence=1.0,
            evidence=evidence,
            reason=(
                f"Read {title!r} from window {observed_handle} (pid {observed_pid}): "
                f"{'satisfied' if matched else 'not satisfied'} because {detail}."
            ),
        )


@dataclass(slots=True)
class ObjectiveRegistry:
    """Which objective kinds this build can check, and why the rest cannot."""

    verifiers: tuple[Any, ...] = ()

    def verifier_for(self, definition: ObjectiveDefinition) -> Any | None:
        for verifier in self.verifiers:
            try:
                if verifier.supports(definition):
                    return verifier
            except Exception:  # noqa: BLE001 - a broken verifier claim is not support
                continue
        return None

    def unavailable(self, definition: ObjectiveDefinition) -> ObjectiveVerification:
        """The honest answer when nothing can check this objective."""
        return ObjectiveVerification(
            outcome=ObjectiveOutcome.UNSUPPORTED,
            verified=None,
            method="none",
            reason=(
                f"No verifier is available for the {definition.kind.value!r} objective. "
                "This build can verify a target window's title; anything else is reported "
                "UNSUPPORTED rather than guessed."
            ),
            at=utc_stamp(),
            definition=definition.to_dict(),
        )

    def availability(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for kind in sorted(OBJECTIVE_KINDS):
            supported = any(
                kind in set(getattr(verifier, "supported_kinds", ())) for verifier in self.verifiers
            )
            rows.append(
                {
                    "kind": kind,
                    "available": supported,
                    "reason": (
                        "A verifier reads the target window's title from the OS."
                        if supported
                        else "No verifier implements this objective kind; it is reported unsupported."
                    ),
                }
            )
        return rows
