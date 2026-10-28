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
    "GAME_STATE_MODES",
    "GameStateFileVerifier",
    "OBJECTIVE_KINDS",
    "ObjectiveDefinition",
    "ObjectiveEvidence",
    "ObjectiveKind",
    "ObjectiveOutcome",
    "ObjectiveRegistry",
    "ObjectiveVerification",
    "OcrTextVerifier",
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
    #: A game-written JSON state artifact on disk, read at a declared pointer.
    #: The game (not NovaForge) produces the fact; the verifier only reads it.
    GAME_STATE_FILE = "game_state_file"
    #: Text recognised in a fresh frame of the target window. A real game can
    #: announce "YOU WIN" on screen without touching its title, so this kind
    #: exists to read the screen itself -- through the SAME OCR engine the rest
    #: of the product uses, with a confidence floor and a freshness bound.
    OCR_TEXT = "ocr_text"
    #: Declared so an unsupported objective can be recorded as a DEFINITION
    #: rather than as a broken verifier. Nothing implements it.
    UNSUPPORTED = "unsupported"


OBJECTIVE_KINDS = frozenset(kind.value for kind in ObjectiveKind)

#: The three comparisons a title objective may ask for. ``absent`` is what makes
#: a "the error dialog is gone" objective checkable.
TITLE_MODES = frozenset({"contains", "equals", "absent"})

#: Comparisons a game-state artifact may ask for. ``gte``/``lte`` make a score
#: or a turn count checkable; ``absent`` makes "the crash marker is gone"
#: checkable without inventing a sentinel value.
GAME_STATE_MODES = frozenset({"equals", "contains", "absent", "gte", "lte"})

#: Comparisons recognised text may ask for. Ordering is deliberately absent: a
#: number recognised by OCR is a string, and comparing it numerically would be
#: a precision claim OCR cannot support.
OCR_MODES = frozenset({"contains", "equals", "absent"})

#: A recognised-text verdict below this confidence is UNKNOWN, never success.
DEFAULT_MIN_OCR_CONFIDENCE = 0.6
#: How old a frame may be and still count as evidence for a screen verdict.
DEFAULT_OCR_MAX_AGE_SECONDS = 5.0


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
    #: ``game_state_file`` only: the artifact's path, relative to one of the
    #: verifier's declared roots, and the dotted pointer into its JSON body.
    #: Kept as plain strings so the definition stays a value a person can read
    #: and a stored record survives a verifier that is not wired on this host.
    path: str = ""
    pointer: str = ""
    #: ``game_state_file`` only: how old the artifact may be and still count as
    #: evidence for THIS verdict. Stale evidence is UNKNOWN, never success.
    max_age_seconds: float | None = None
    #: ``ocr_text`` only: an optional ``x,y,width,height`` region in the frame's
    #: own pixels. Words whose centre falls outside it are not evidence, which
    #: is how "read the scoreboard" stays bounded to the scoreboard.
    region: str = ""

    MAX_PARAMETER = 200
    MAX_DESCRIPTION = 300
    MAX_PATH = 400
    MAX_POINTER = 200
    MAX_AGE_SECONDS = 3600.0

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> "ObjectiveDefinition":
        data = dict(payload or {})
        raw = str(data.get("kind", "") or "").strip().lower()
        if raw not in OBJECTIVE_KINDS:
            raise ValueError(
                f"Unknown objective kind {raw or '(blank)'!r}; expected one of "
                f"{', '.join(sorted(OBJECTIVE_KINDS))}."
            )
        raw_age = data.get("max_age_seconds")
        age: float | None = None
        if raw_age not in (None, ""):
            if isinstance(raw_age, bool):
                raise ValueError("max_age_seconds must be a number of seconds or blank.")
            try:
                age = float(raw_age)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "max_age_seconds must be a number of seconds or blank."
                ) from exc
        return cls(
            kind=ObjectiveKind(raw),
            parameter=str(data.get("parameter", "") or "").strip(),
            mode=str(data.get("mode", "contains") or "contains").strip().lower(),
            description=str(data.get("description", "") or "").strip(),
            path=str(data.get("path", "") or "").strip(),
            pointer=str(data.get("pointer", "") or "").strip(),
            max_age_seconds=age,
            region=str(data.get("region", "") or "").strip(),
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
        elif self.kind is ObjectiveKind.GAME_STATE_FILE:
            problems.extend(self._game_state_problems())
        elif self.kind is ObjectiveKind.OCR_TEXT:
            problems.extend(self._ocr_problems())
        if len(self.description) > self.MAX_DESCRIPTION:
            problems.append(f"The description must be at most {self.MAX_DESCRIPTION} characters.")
        return problems

    def _game_state_problems(self) -> list[str]:
        """Rules that keep a file objective bounded and unambiguous.

        A path is a path into ONE declared root, never an absolute path and
        never a traversal: the alternative is an objective that can name any
        file on the machine, which is a read primitive nobody approved.
        """
        problems: list[str] = []
        if not self.path:
            problems.append("A game_state_file objective needs the artifact's path.")
        else:
            if len(self.path) > self.MAX_PATH:
                problems.append(f"The artifact path must be at most {self.MAX_PATH} characters.")
            normalised = self.path.replace("\\", "/")
            if normalised.startswith("/") or (len(normalised) > 1 and normalised[1] == ":"):
                problems.append(
                    "The artifact path must be relative to a declared root, not an absolute path."
                )
            if any(part == ".." for part in normalised.split("/")):
                problems.append("The artifact path must not contain '..'.")
            if any(char in normalised for char in "*?[]"):
                problems.append("The artifact path must name one file, not a pattern.")
        if self.mode not in GAME_STATE_MODES:
            problems.append(
                f"Unknown comparison {self.mode!r}; expected one of "
                f"{', '.join(sorted(GAME_STATE_MODES))}."
            )
        elif self.mode != "absent" and not self.parameter:
            problems.append(
                f"A game_state_file objective with mode {self.mode!r} needs a value to compare."
            )
        if len(self.parameter) > self.MAX_PARAMETER:
            problems.append(f"The comparison value must be at most {self.MAX_PARAMETER} characters.")
        if len(self.pointer) > self.MAX_POINTER:
            problems.append(f"The artifact pointer must be at most {self.MAX_POINTER} characters.")
        if any(part == "" for part in self.pointer.split(".")) and self.pointer:
            problems.append("The artifact pointer must not contain an empty segment.")
        if self.max_age_seconds is not None and not (
            1.0 <= float(self.max_age_seconds) <= self.MAX_AGE_SECONDS
        ):
            problems.append(
                f"max_age_seconds must be between 1 and {int(self.MAX_AGE_SECONDS)}, or blank."
            )
        return problems

    def _ocr_problems(self) -> list[str]:
        """A screen-text objective needs text and a bounded, optional region."""
        problems: list[str] = []
        if self.mode not in OCR_MODES:
            problems.append(
                f"Unknown comparison {self.mode!r}; expected one of "
                f"{', '.join(sorted(OCR_MODES))}."
            )
        if self.mode != "absent" and not self.parameter:
            problems.append("An ocr_text objective needs the text to look for.")
        if len(self.parameter) > self.MAX_PARAMETER:
            problems.append(f"The text parameter must be at most {self.MAX_PARAMETER} characters.")
        if self.region:
            parts = [part.strip() for part in self.region.split(",")]
            if len(parts) != 4:
                problems.append("The region must be four integers: x,y,width,height.")
            else:
                try:
                    x, y, width, height = (int(part) for part in parts)
                except ValueError:
                    problems.append("The region must be four integers: x,y,width,height.")
                else:
                    if min(x, y) < 0 or width <= 0 or height <= 0:
                        problems.append("The region must have non-negative x/y and a positive size.")
        if self.max_age_seconds is not None and not (
            0.5 <= float(self.max_age_seconds) <= self.MAX_AGE_SECONDS
        ):
            problems.append(
                f"max_age_seconds must be between 0.5 and {int(self.MAX_AGE_SECONDS)}, or blank."
            )
        return problems

    def region_box(self) -> tuple[int, int, int, int] | None:
        """The parsed region, or None when the objective reads the whole frame."""
        if not self.region:
            return None
        parts = [part.strip() for part in self.region.split(",")]
        if len(parts) != 4:
            return None
        try:
            return tuple(int(part) for part in parts)  # type: ignore[return-value]
        except ValueError:
            return None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind.value,
            "parameter": self.parameter,
            "mode": self.mode,
            "description": self.description,
        }
        if self.kind is ObjectiveKind.OCR_TEXT:
            data["region"] = self.region
            data["max_age_seconds"] = self.max_age_seconds
        # Only the file objective carries these, so a window-title record keeps
        # the shape it always had (and older records load unchanged).
        if self.kind is ObjectiveKind.GAME_STATE_FILE:
            data["path"] = self.path
            data["pointer"] = self.pointer
            data["max_age_seconds"] = self.max_age_seconds
        return data

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
    #: ``game_state_file`` only: which artifact was read, how old it was and the
    #: digest of the exact bytes the verdict came from.
    artifact: str = ""
    artifact_mtime: str = ""
    artifact_age_seconds: float | None = None
    artifact_digest: str = ""

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
            "artifact": self.artifact,
            "artifact_mtime": self.artifact_mtime,
            "artifact_age_seconds": self.artifact_age_seconds,
            "artifact_digest": self.artifact_digest,
        }

    def reference(self) -> dict[str, Any]:
        """A compact timeline evidence reference to this observation."""
        reference: dict[str, Any] = {
            "type": "objective_probe",
            "source": self.method,
            "at": self.at,
        }
        if self.window_handle is not None:
            reference["handle"] = self.window_handle
            reference["pid"] = self.window_pid
        if self.artifact:
            reference["artifact"] = self.artifact
            reference["digest"] = self.artifact_digest
        return reference


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
    #: Which verifier produced this verdict, and its version. A verdict without
    #: an identity is not reproducible evidence.
    verifier: str = ""
    verifier_version: int = 0

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
            "verifier": self.verifier,
            "verifier_version": self.verifier_version,
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
            verifier=str(data.get("verifier", "") or ""),
            verifier_version=(
                int(data["verifier_version"])
                if isinstance(data.get("verifier_version"), int)
                and not isinstance(data.get("verifier_version"), bool)
                else 0
            ),
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
    verifier_id: str = "WindowTitleVerifier"
    version: int = 1
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
                evidence=evidence, confidence=confidence,
                verifier=self.verifier_id, verifier_version=self.version, **base,
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
class GameStateFileVerifier:
    """The second supported verifier: a game-written JSON artifact on disk.

    The point of this verifier is that the fact it reads is produced by the
    TARGET, not by NovaForge: a game (or a disposable harness standing in for
    one) writes its own state, and the verdict is a read of that file at a
    declared pointer. An action NovaForge records cannot satisfy it, and a
    session state cannot fake it.

    Every refusal is typed. A file that has not been written yet is
    ``in_progress`` (the objective may still happen); an artifact older than its
    declared age, an unreadable artifact or a changed root is ``unknown``; a
    malformed body, an unreadable pointer or a future timestamp is a
    ``verification_error`` — the artifact exists but cannot be trusted as
    evidence, which is different from evidence of failure.

    ``roots`` are the only directories an objective may name. An empty tuple
    means no artifact root was declared, so the verifier reports itself
    unavailable instead of reading somewhere unapproved.
    """

    roots: tuple[Any, ...] = ()
    default_max_age_seconds: float = 300.0
    verifier_id: str = "GameStateFileVerifier"
    version: int = 1
    supported_kinds: frozenset[str] = frozenset({ObjectiveKind.GAME_STATE_FILE.value})

    # -- path safety ---------------------------------------------------------

    def resolve_artifact(self, path: str) -> tuple[Any | None, str]:
        """Resolve a relative artifact path inside one declared root.

        Returns ``(resolved_path, reason)``. ``reason`` is non-empty exactly
        when the path is refused; the caller turns that into ``unsupported``,
        so a traversal attempt can never become a read.
        """
        from pathlib import Path

        text = str(path or "").strip().replace("\\", "/")
        if not text:
            return None, "The objective names no artifact path."
        if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
            return None, "The artifact path must be relative to a declared root, not absolute."
        if any(part == ".." for part in text.split("/")):
            return None, "The artifact path must not traverse outside its declared root."
        if not self.roots:
            return None, (
                "No artifact root is declared on this host, so a game-state file cannot be "
                "read. Set NOVACONTROL_NOVAFORGE_ARTIFACT_ROOTS to the directory the game writes."
            )
        candidates: list[Any] = []
        root_reasons: list[str] = []
        for raw_root in self.roots:
            root = Path(raw_root)
            try:
                resolved_root = root.resolve(strict=True)
            except (OSError, RuntimeError):
                root_reasons.append(f"{root} is not an accessible directory")
                continue
            if not resolved_root.is_dir():
                root_reasons.append(f"{resolved_root} is not a directory")
                continue
            candidate = (resolved_root / text).resolve()
            try:
                candidate.relative_to(resolved_root)
            except ValueError:
                root_reasons.append(f"the path escapes {resolved_root}")
                continue
            candidates.append(candidate)
        if not candidates:
            return None, (
                "The artifact path is outside every declared root: "
                + ("; ".join(root_reasons) or "no root matched")
                + "."
            )
        # Two roots naming the same relative path is ambiguous evidence, not a
        # preference: refuse rather than pick one and call it the truth.
        distinct = {str(item) for item in candidates if item.exists()}
        if len(distinct) > 1:
            return None, (
                "More than one declared root contains this artifact, so the read would be "
                "ambiguous; declare one root for this objective."
            )
        return candidates[0], ""

    # -- reading -------------------------------------------------------------

    @staticmethod
    def _pointer_value(body: Any, pointer: str) -> tuple[bool, Any]:
        """Follow a dotted pointer into a JSON body. ``(found, value)``."""
        if not pointer:
            return True, body
        current = body
        for raw in pointer.split("."):
            segment = raw.strip()
            if isinstance(current, Mapping):
                if segment not in current:
                    return False, None
                current = current[segment]
            elif isinstance(current, list):
                if not segment.isdigit():
                    return False, None
                index = int(segment)
                if index >= len(current):
                    return False, None
                current = current[index]
            else:
                return False, None
        return True, current

    @staticmethod
    def _compare(mode: str, observed: Any, expected: str) -> tuple[bool, str]:
        """Compare one observed JSON value with the declared parameter."""
        if mode in {"gte", "lte"}:
            if isinstance(observed, bool) or not isinstance(observed, (int, float)):
                raise ValueError(
                    f"The observed value {observed!r} is not a number, so {mode} cannot be judged."
                )
            try:
                target = float(expected)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"The comparison value {expected!r} is not a number, so {mode} cannot be judged."
                ) from exc
            matched = float(observed) >= target if mode == "gte" else float(observed) <= target
            detail = f"the observed value is {'at least' if mode == 'gte' else 'at most'} {target!r}"
            return matched, detail
        if mode == "contains":
            matched = expected.casefold() in str(observed).casefold()
            return matched, f"the observed value contains {expected!r}"
        # equals: strings compare case-insensitively (the game's casing is not
        # evidence about the objective); numbers and booleans compare exactly.
        if isinstance(observed, str):
            matched = observed.strip().casefold() == expected.strip().casefold()
        elif isinstance(observed, bool):
            matched = observed is (expected.strip().casefold() == "true")
        elif isinstance(observed, (int, float)):
            try:
                matched = abs(float(observed) - float(expected)) <= 1e-9
            except (TypeError, ValueError):
                matched = False
        else:
            matched = observed == expected
        return matched, f"the observed value equals {expected!r}"

    @property
    def ready(self) -> bool:
        """Whether this verifier can read anything on THIS host."""
        return bool(self.roots)

    @property
    def unavailable_reason(self) -> str:
        return (
            ""
            if self.ready
            else (
                "The game-state verifier is installed but no artifact root is declared on this "
                "host, so no artifact can be read. Set NOVACONTROL_NOVAFORGE_ARTIFACT_ROOTS."
            )
        )

    def supports(self, definition: ObjectiveDefinition) -> bool:
        return (
            definition.kind.value in self.supported_kinds
            and not definition.errors()
            and self.ready
        )

    async def verify(
        self,
        definition: ObjectiveDefinition,
        *,
        handle: int = 0,
        pid: int | None = None,
        session_id: str = "",
        action_id: str = "",
    ) -> ObjectiveVerification:
        import asyncio
        import hashlib
        import json
        from datetime import UTC, datetime

        at = utc_stamp()
        base: dict[str, Any] = {
            "session_id": session_id,
            "action_id": action_id,
            "at": at,
            "definition": definition.to_dict(),
            "method": "game_state_file",
            "verifier": self.verifier_id,
            "verifier_version": self.version,
        }

        def result(
            outcome: ObjectiveOutcome,
            *,
            reason: str,
            verified: bool | None = None,
            evidence: tuple[ObjectiveEvidence, ...] = (),
            confidence: float | None = None,
        ) -> ObjectiveVerification:
            return ObjectiveVerification(
                outcome=outcome,
                verified=verified,
                reason=reason,
                evidence=evidence,
                confidence=confidence,
                **base,
            )

        problems = definition.errors()
        if problems:
            return result(
                ObjectiveOutcome.UNSUPPORTED,
                reason="The objective definition is not checkable: " + "; ".join(problems),
            )
        if not self.roots:
            return result(
                ObjectiveOutcome.UNSUPPORTED,
                reason=(
                    "No artifact root is declared on this host, so a game-state file objective "
                    "cannot be read. Set NOVACONTROL_NOVAFORGE_ARTIFACT_ROOTS to the directory "
                    "the target writes its state into."
                ),
            )

        path, refusal = self.resolve_artifact(definition.path)
        if path is None:
            return result(ObjectiveOutcome.UNSUPPORTED, reason=refusal)

        max_age = float(
            definition.max_age_seconds
            if definition.max_age_seconds is not None
            else self.default_max_age_seconds
        )

        def evidence(
            *,
            observed: str,
            expected: str,
            note: str = "",
            digest: str = "",
            mtime: str = "",
            age: float | None = None,
        ) -> tuple[ObjectiveEvidence, ...]:
            return (
                ObjectiveEvidence(
                    method="game_state_file",
                    observed=observed,
                    expected=expected,
                    at=at,
                    note=note,
                    artifact=str(definition.path),
                    artifact_mtime=mtime,
                    artifact_age_seconds=None if age is None else round(age, 3),
                    artifact_digest=digest,
                ),
            )

        expected_text = (
            "the artifact must be absent"
            if definition.mode == "absent"
            else f"{definition.mode} {definition.parameter!r}"
            + (f" at {definition.pointer!r}" if definition.pointer else "")
        )

        try:
            stat = await asyncio.to_thread(path.stat)
        except FileNotFoundError:
            if definition.mode == "absent":
                return result(
                    ObjectiveOutcome.VERIFIED_SUCCESS,
                    verified=True,
                    confidence=1.0,
                    evidence=evidence(
                        observed="(no artifact)",
                        expected=expected_text,
                        note="The declared artifact does not exist, which is the objective.",
                    ),
                    reason=(
                        f"The artifact {definition.path!r} is absent, which is the declared "
                        "success condition."
                    ),
                )
            return result(
                ObjectiveOutcome.IN_PROGRESS,
                evidence=evidence(
                    observed="(no artifact)",
                    expected=expected_text,
                    note="The target has not written its state artifact yet.",
                ),
                reason=(
                    f"The artifact {definition.path!r} has not been written yet, so the "
                    "objective is IN PROGRESS — a missing artifact is not success."
                ),
            )
        except OSError as exc:
            return result(
                ObjectiveOutcome.UNKNOWN,
                evidence=evidence(observed="(unreadable)", expected=expected_text),
                reason=f"The artifact could not be inspected: {type(exc).__name__}: {exc}",
            )

        if not stat.st_size:
            return result(
                ObjectiveOutcome.IN_PROGRESS,
                evidence=evidence(observed="(empty file)", expected=expected_text),
                reason=(
                    f"The artifact {definition.path!r} exists but is empty; a target that is "
                    "still writing is not a decided objective."
                ),
            )

        mtime = datetime.fromtimestamp(stat.st_mtime, UTC)
        age = max(0.0, (datetime.now(UTC) - mtime).total_seconds())
        if definition.mode == "absent":
            return result(
                ObjectiveOutcome.VERIFIED_FAILURE,
                verified=True,
                confidence=1.0,
                evidence=evidence(
                    observed=f"artifact present ({stat.st_size} bytes)",
                    expected=expected_text,
                    mtime=mtime.isoformat(),
                    age=age,
                    note="The objective is that the artifact is gone.",
                ),
                reason=(
                    f"The artifact {definition.path!r} is present, so the declared "
                    "'absent' condition is not met."
                ),
            )
        # A timestamp from the future cannot be used to prove freshness: it is
        # evidence the clock or the writer is untrustworthy, not a fresh read.
        if stat.st_mtime > (datetime.now(UTC).timestamp() + 5.0):
            return result(
                ObjectiveOutcome.VERIFICATION_ERROR,
                evidence=evidence(
                    observed="(future timestamp)",
                    expected=expected_text,
                    mtime=mtime.isoformat(),
                    age=0.0,
                ),
                reason=(
                    f"The artifact's modification time ({mtime.isoformat()}) is in the future, "
                    "so its freshness cannot be established and it is not evidence."
                ),
            )
        if age > max_age:
            return result(
                ObjectiveOutcome.UNKNOWN,
                evidence=evidence(
                    observed="(stale artifact)",
                    expected=expected_text,
                    mtime=mtime.isoformat(),
                    age=age,
                ),
                reason=(
                    f"The artifact is {age:.1f}s old, older than the {max_age:.0f}s freshness "
                    "bound; a stale read is not a verdict about now."
                ),
            )

        try:
            raw = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            return result(
                ObjectiveOutcome.VERIFICATION_ERROR,
                evidence=evidence(
                    observed="(unreadable)",
                    expected=expected_text,
                    mtime=mtime.isoformat(),
                    age=age,
                ),
                reason=f"The artifact exists but could not be read: {type(exc).__name__}: {exc}",
            )
        digest = hashlib.sha256(raw).hexdigest()[:16]
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return result(
                ObjectiveOutcome.VERIFICATION_ERROR,
                evidence=evidence(
                    observed="(malformed JSON)",
                    expected=expected_text,
                    mtime=mtime.isoformat(),
                    age=age,
                    digest=digest,
                ),
                reason=(
                    f"The artifact is not valid UTF-8 JSON ({type(exc).__name__}: {exc}); a "
                    "malformed artifact cannot decide an objective."
                ),
            )
        found, observed = self._pointer_value(body, definition.pointer)
        if not found:
            return result(
                ObjectiveOutcome.VERIFICATION_ERROR,
                evidence=evidence(
                    observed="(pointer not found)",
                    expected=expected_text,
                    mtime=mtime.isoformat(),
                    age=age,
                    digest=digest,
                    note=f"Present keys: {', '.join(sorted(map(str, body))) if isinstance(body, Mapping) else type(body).__name__}",
                ),
                reason=(
                    f"The artifact has no value at {definition.pointer!r}; the artifact does "
                    "not have the shape the objective declared, so it is not evidence of "
                    "either outcome."
                ),
            )
        try:
            matched, detail = self._compare(definition.mode, observed, definition.parameter)
        except ValueError as exc:
            return result(
                ObjectiveOutcome.VERIFICATION_ERROR,
                evidence=evidence(
                    observed=json.dumps(observed, sort_keys=True)[:200],
                    expected=expected_text,
                    mtime=mtime.isoformat(),
                    age=age,
                    digest=digest,
                ),
                reason=str(exc),
            )
        return result(
            ObjectiveOutcome.VERIFIED_SUCCESS if matched else ObjectiveOutcome.VERIFIED_FAILURE,
            verified=True,
            confidence=1.0,
            evidence=evidence(
                observed=json.dumps(observed, sort_keys=True)[:200],
                expected=expected_text,
                mtime=mtime.isoformat(),
                age=age,
                digest=digest,
                note=f"Read {definition.path!r} ({stat.st_size} bytes).",
            ),
            reason=(
                f"Read {definition.path!r} (modified {mtime.isoformat()}, sha256:{digest}) and "
                f"{'satisfied' if matched else 'did not satisfy'} the objective because {detail}."
            ),
        )


#: The probe may capture and read a frame itself, so it is given a generous
#: ceiling. Exceeding it is UNKNOWN, never a failure.
OCR_PROBE_TIMEOUT_SECONDS = 20.0


@dataclass(slots=True)
class OcrTextVerifier:
    """The third supported verifier: text read off the target window's screen.

    The verifier never captures anything itself. The manager injects a probe
    that captures through the SAME desktop worker as the session's frames and
    returns that frame's OCR reading, which keeps the independent-evidence
    property this path rests on: the input side cannot write into a probe.

    The probe's mapping carries ``available``, ``fresh``, ``captured_at``,
    ``age_seconds``, ``path``, ``text``, ``words``, ``confidence``, ``engine``
    and ``reason``. A missing field is an unusable reading, never a default.

    Honesty rules, in order: an unavailable engine is ``unsupported``; a
    reading that is stale, empty or below ``min_confidence`` is ``unknown``
    (never a decided failure — the screen was not read well enough to say
    anything about it); and only a fresh, trusted reading can FAIL one. An
    engine that reports no confidence at all (Windows OCR does not) may still
    decide from recognised words, and may never prove ABSENCE from none; the
    verdict then carries ``confidence: null`` rather than an invented number.
    """

    probe: Callable[[int], Awaitable[Any | None]]
    min_confidence: float = DEFAULT_MIN_OCR_CONFIDENCE
    default_max_age_seconds: float = DEFAULT_OCR_MAX_AGE_SECONDS
    verifier_id: str = "OcrTextVerifier"
    version: int = 1
    supported_kinds: frozenset[str] = frozenset({ObjectiveKind.OCR_TEXT.value})

    @property
    def ready(self) -> bool:
        """Whether screen text can be read on THIS host."""
        return self.probe is not None

    @property
    def unavailable_reason(self) -> str:
        return (
            ""
            if self.ready
            else (
                "The screen-text verifier is installed but no OCR frame probe is wired, so "
                "nothing can be read from the screen on this host."
            )
        )

    def supports(self, definition: ObjectiveDefinition) -> bool:
        return (
            definition.kind.value in self.supported_kinds
            and not definition.errors()
            and self.ready
        )

    async def verify(
        self,
        definition: ObjectiveDefinition,
        *,
        handle: int = 0,
        pid: int | None = None,
        session_id: str = "",
        action_id: str = "",
    ) -> ObjectiveVerification:
        import asyncio

        at = utc_stamp()
        base: dict[str, Any] = {
            "session_id": session_id,
            "action_id": action_id,
            "at": at,
            "definition": definition.to_dict(),
            "method": "ocr_text",
            "verifier": self.verifier_id,
            "verifier_version": self.version,
        }

        def result(
            outcome: ObjectiveOutcome,
            *,
            reason: str,
            verified: bool | None = None,
            evidence: tuple[ObjectiveEvidence, ...] = (),
            confidence: float | None = None,
        ) -> ObjectiveVerification:
            return ObjectiveVerification(
                outcome=outcome,
                verified=verified,
                reason=reason,
                evidence=evidence,
                confidence=confidence,
                **base,
            )

        def undecided(reason: str) -> ObjectiveVerification:
            return result(
                ObjectiveOutcome.UNKNOWN,
                verified=None,
                reason=(
                    f"Could not decide whether {definition.description or definition.parameter!r} "
                    f"was achieved: {reason}"
                ),
            )

        problems = definition.errors()
        if problems:
            return result(
                ObjectiveOutcome.UNSUPPORTED,
                verified=None,
                reason="The objective definition is not checkable: " + "; ".join(problems),
            )
        if not self.ready:
            return result(
                ObjectiveOutcome.UNSUPPORTED,
                verified=None,
                reason=self.unavailable_reason,
            )
        if handle <= 0:
            return undecided("there is no window to read.")

        try:
            reading = await asyncio.wait_for(
                self.probe(handle), timeout=OCR_PROBE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            return undecided(
                f"reading the screen took longer than {OCR_PROBE_TIMEOUT_SECONDS:.0f}s."
            )
        except Exception as exc:  # noqa: BLE001 - a broken probe is UNKNOWN, not a failure
            return undecided(f"reading the screen failed: {type(exc).__name__}: {exc}.")
        if not isinstance(reading, Mapping):
            return undecided("the screen reader returned no reading for this window.")
        if not reading.get("available", False):
            reason = str(reading.get("reason") or "the screen reader is unavailable")
            return result(
                ObjectiveOutcome.UNSUPPORTED,
                verified=None,
                reason=f"Screen text could not be read: {reason}.",
            )
        if not reading.get("fresh", False):
            return undecided("the screen reading was not a fresh capture of this window.")
        age = _as_float(reading.get("age_seconds"))
        if age is None or age < 0:
            return undecided("the screen reading did not say how old it was.")
        max_age = (
            float(definition.max_age_seconds)
            if definition.max_age_seconds is not None
            else self.default_max_age_seconds
        )
        if age > max_age:
            return undecided(
                f"the screen reading is {age:.1f}s old, older than the {max_age:.0f}s "
                "this objective allows."
            )

        region = definition.region_box()
        words = _ocr_words(reading.get("words"), region)
        observed = " ".join(str(word.get("text") or "") for word in words)
        if not words:
            observed = " ".join(str(reading.get("text") or "").split())
        confidence = _as_float(reading.get("confidence"))
        if words and confidence is None:
            # A word list carries its own confidences; the weakest one decides,
            # so a single guessed word cannot promote itself to a clean read.
            scores = [
                score
                for score in (_as_float(word.get("confidence")) for word in words)
                if score is not None
            ]
            confidence = min(scores) if scores else None
        # Windows' OCR engine reports words but no per-word confidence. That is
        # a missing MEASUREMENT, not a zero: a reading the engine actually
        # recognised can still be compared (the words are the measurement),
        # while an EMPTY reading cannot prove absence, because "nothing came
        # back" and "the read failed" look identical without a confidence.
        if confidence is None and reading.get("confidence_reported") is not False:
            return undecided("the screen reader reported no confidence for this reading.")
        if confidence is not None and confidence < self.min_confidence:
            return undecided(
                f"the screen reader was only {confidence:.2f} confident, below the "
                f"{self.min_confidence:.2f} needed to decide."
            )
        if not words and confidence is None:
            return undecided(
                "the engine recognised no text and reports no confidence, so an absent "
                "marker cannot be told apart from a failed read."
            )

        expected = definition.parameter
        haystack = _normalise(observed)
        needle = _normalise(expected)
        if definition.mode == "absent":
            matched = needle not in haystack
            detail = (
                f"the screen did not show {expected!r}"
                if matched
                else f"the screen still showed {expected!r}"
            )
        elif definition.mode == "equals":
            matched = haystack == needle
            detail = f"the screen showed exactly {expected!r}"
        else:
            matched = needle in haystack
            detail = f"the screen showed {expected!r}"
        if not matched and definition.mode != "absent":
            detail += f"; it read {observed[:160]!r}"
        where = "" if not definition.region else f" in region {definition.region}"
        confidence_text = (
            f"at {confidence:.2f} confidence"
            if confidence is not None
            else "with no confidence reported by the engine"
        )
        note = (
            f"Read {len(words)} word(s) {confidence_text} from "
            f"{reading.get('path') or 'the captured frame'} "
            f"({reading.get('engine') or 'the screen reader'}, {age:.1f}s old)."
        )
        evidence = (
            ObjectiveEvidence(
                method="ocr_text",
                observed=observed[:200],
                expected=f"{definition.mode} {expected!r}",
                at=at,
                window_handle=handle,
                window_pid=pid,
                note=note,
            ),
        )
        return result(
            ObjectiveOutcome.VERIFIED_SUCCESS if matched else ObjectiveOutcome.VERIFIED_FAILURE,
            verified=True,
            confidence=confidence,
            evidence=evidence,
            reason=(
                f"Read window {handle}{where}: {'satisfied' if matched else 'not satisfied'} "
                f"because {detail}."
            ),
        )


def _normalise(value: str) -> str:
    return " ".join(value.split()).casefold()


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ocr_words(
    value: Any, region: tuple[int, int, int, int] | None
) -> list[Mapping[str, Any]]:
    """The words a region admits, dropping any that cannot be placed in it."""
    words: list[Mapping[str, Any]] = []
    for word in value if isinstance(value, (list, tuple)) else ():
        if not isinstance(word, Mapping) or not str(word.get("text") or "").strip():
            continue
        if region is not None:
            x = _as_float(word.get("x"))
            y = _as_float(word.get("y"))
            if x is None or y is None or not _in_region(x, y, region):
                continue
        words.append(word)
    return words


def _in_region(x: float, y: float, region: tuple[int, int, int, int]) -> bool:
    left, top, width, height = region
    return left <= x <= left + width and top <= y <= top + height


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

    def available_kinds(self) -> list[str]:
        """Kinds a READY verifier can check on this host right now."""
        kinds: set[str] = set()
        for verifier in self.verifiers:
            if not bool(getattr(verifier, "ready", True)):
                continue
            kinds.update(str(kind) for kind in getattr(verifier, "supported_kinds", ()))
        return sorted(kinds)

    def supported_kinds(self) -> list[str]:
        kinds: set[str] = set()
        for verifier in self.verifiers:
            kinds.update(str(kind) for kind in getattr(verifier, "supported_kinds", ()))
        return sorted(kinds)

    def unavailable(self, definition: ObjectiveDefinition) -> ObjectiveVerification:
        """The honest answer when nothing can check this objective."""
        kinds = self.supported_kinds()
        return ObjectiveVerification(
            outcome=ObjectiveOutcome.UNSUPPORTED,
            verified=None,
            method="none",
            reason=(
                f"No available verifier can check the {definition.kind.value!r} objective. "
                f"This build verifies: {', '.join(kinds) or 'nothing'}; anything else is "
                "reported UNSUPPORTED rather than guessed."
            ),
            at=utc_stamp(),
            definition=definition.to_dict(),
        )

    def availability(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for kind in sorted(OBJECTIVE_KINDS):
            declared = [
                verifier
                for verifier in self.verifiers
                if kind in set(getattr(verifier, "supported_kinds", ()))
            ]
            owners = [item for item in declared if bool(getattr(item, "ready", True))]
            supported = bool(owners)
            identity = ""
            if supported:
                owner = owners[0]
                identity = (
                    f"{getattr(owner, 'verifier_id', type(owner).__name__)} "
                    f"v{getattr(owner, 'version', 1)}"
                )
            elif declared:
                identity = (
                    f"{getattr(declared[0], 'verifier_id', type(declared[0]).__name__)} "
                    f"v{getattr(declared[0], 'version', 1)} (not ready)"
                )
            rows.append(
                {
                    "kind": kind,
                    "available": supported,
                    "verifier": identity,
                    "reason": (
                        "A verifier reads the target window's title from the OS."
                        if supported and kind == ObjectiveKind.WINDOW_TITLE.value
                        else "A verifier reads a game-written JSON artifact at a declared pointer."
                        if supported and kind == ObjectiveKind.GAME_STATE_FILE.value
                        else "A verifier reads text off a fresh frame of the target window."
                        if supported and kind == ObjectiveKind.OCR_TEXT.value
                        else str(getattr(declared[0], "unavailable_reason", ""))
                        if declared
                        else "No verifier implements this objective kind; it is reported unsupported."
                    ),
                }
            )
        return rows
