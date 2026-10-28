"""NovaForge NF-14 — is this window embeddable, and what is the evidence?

The NF-13 host can answer "did this embedding verify?". This module answers the
question before that one, in a vocabulary a person and a UI can both read:

``SUPPORTED`` · ``SUPPORTED_WITH_LIMITATIONS`` · ``UNSUPPORTED`` · ``UNKNOWN`` ·
``NOT_TESTED``

Design boundaries
-----------------

* **Evidence, never a title.** A classification is derived from measured window
  facts (mode, style, geometry, visible/minimized) and from what the NF-13
  preflight/attempt actually reported. There is no per-game table of guesses.
* **Unknown is an answer.** A property that cannot be read is reported as
  unmeasured (``WindowApiError``, non-Windows, no window *yet*) instead of being
  folded into a pass or a failure.
* **Embedding and capture are separate axes.** A record carries
  ``embedding`` and ``capture`` independently: a hosted window whose surface
  the capture backend cannot render is ``SUPPORTED`` for embedding and
  ``NOT_TESTED``/``UNSUPPORTED`` for capture. Nothing here infers one from the
  other (H03's rule, kept).
* **Persistence cannot fail the work.** The log writes through the application's
  :class:`~novacontrol.persistence.json_store.JsonStateStore`, which already
  drops an unwritable snapshot instead of raising; the log guards anyway.

The module imports NOTHING from :mod:`novacontrol.novaforge.embedding` at run
time — ``embedding`` imports this module, so the dependency points one way and
the helpers duck-type the preflight it is given.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime cycle
    from novacontrol.novaforge.embedding import EmbedPreflight, GameIdentity, WindowInfo

__all__ = [
    "NOT_TESTED",
    "CompatibilityClass",
    "CompatibilityLog",
    "CompatibilityRecord",
    "Evidence",
    "apply_attempt",
    "apply_restore",
    "classify_preflight",
    "note_window_ended_embedding",
]


def _as_int(value: Any) -> int:
    """An int for a stored value, or 0 when it cannot be read."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class CompatibilityClass(StrEnum):
    """The five compatibility answers, kept distinct on purpose."""

    SUPPORTED = "SUPPORTED"
    SUPPORTED_WITH_LIMITATIONS = "SUPPORTED_WITH_LIMITATIONS"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"
    NOT_TESTED = "NOT_TESTED"


#: No record has been made for this game/window yet. A module constant so a
#: caller never writes the string by hand and a typo cannot become a sixth class.
NOT_TESTED = CompatibilityClass.NOT_TESTED

#: Flags of the independent axes: how far the EMBEDDING got, and separately how
#: far CAPTURE got. Lowercase codes, because they are payload values.
EMBEDDING_EMBEDDED = "embedded"
EMBEDDING_REFUSED = "refused"
EMBEDDING_FAILED = "failed"
EMBEDDING_NOT_ATTEMPTED = "not_attempted"

CAPTURE_NOT_TESTED = "not_tested"
CAPTURE_UNAVAILABLE = "unavailable"
CAPTURE_AVAILABLE = "available"

#: Preflight reason codes that end the attempt before any native call. They are
#: UNSUPPORTED (the window cannot be hosted) rather than UNKNOWN: the host looked
#: and the answer is no.
UNSUPPORTED_CODES = frozenset({"fullscreen_presentation_unsupported", "already_child"})

#: Refusals about the CALL at the moment it was made — the host had no surface,
#: something was already embedded, or the window changed owner between the
#: preflight and the first native call. They say nothing about compatibility.
REFUSED_BEFORE_MUTATION = frozenset({
    "host_not_a_window",
    "already_embedded",
    "window_changed_before_embed",
})

#: The window or its process ended while a mutation was running. That is an
#: external event, not a verdict about hosting: the game never demonstrated
#: that it refuses to be embedded, and there may be nothing left to restore.
EXTERNAL_END_CODES = frozenset({"window_destroyed", "process_exited"})

#: Reasons that describe a CONDITION rather than a compatibility fact: the
#: window is missing (yet), minimized, hidden, unreadable, not this PID. The
#: honest answer is UNKNOWN — try again when the condition changes.
UNKNOWN_CODES = frozenset({
    "platform_unsupported",
    "no_process",
    "window_not_found",
    "window_owner_mismatch",
    "process_identity_mismatch",
    "window_minimized",
    "window_hidden",
    "zero_geometry",
})


@dataclass(frozen=True, slots=True)
class Evidence:
    """One measured fact behind a classification."""

    code: str
    detail: str
    measured: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "measured": self.measured}


@dataclass(slots=True)
class CompatibilityRecord:
    """One game window's classification, with identity and evidence.

    ``embedding`` and ``capture`` are deliberately separate strings. A reader
    that turns one into the other has broken NF-14's Scope E.
    """

    game: str
    classification: CompatibilityClass
    reason_code: str
    reason: str
    checked_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    pid: int = 0
    start_time: str = ""
    handle: int = 0
    window_title: str = ""
    window_class: str = ""
    window_mode: str = ""
    dpi: int = 0
    app_id: str = ""
    launch_id: str = ""
    owned: bool = False
    method: str = "preflight"
    evidence: tuple[Evidence, ...] = ()
    limitations: tuple[str, ...] = ()
    embedding: str = EMBEDDING_NOT_ATTEMPTED
    capture: str = CAPTURE_NOT_TESTED
    restored: bool | None = None

    # -- reporting ---------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "game": self.game,
            "classification": str(self.classification),
            "reason_code": self.reason_code,
            "reason": self.reason,
            "checked_at": self.checked_at,
            "identity": {
                "pid": self.pid,
                "start_time": self.start_time,
                "handle": self.handle,
                "window_title": self.window_title,
                "window_class": self.window_class,
                "app_id": self.app_id,
                "launch_id": self.launch_id,
                "owned": self.owned,
            },
            "window_mode": self.window_mode,
            "dpi": self.dpi,
            "method": self.method,
            "evidence": [item.to_dict() for item in self.evidence],
            "limitations": list(self.limitations),
            "embedding": self.embedding,
            "capture": self.capture,
            "restored": self.restored,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CompatibilityRecord:
        """Tolerant restore: an unknown class, a missing key or a non-numeric
        identity value never raises; the row is loaded with a 0 instead."""
        raw_identity = payload.get("identity")
        identity: Mapping[str, Any] = (
            raw_identity if isinstance(raw_identity, Mapping) else {}
        )
        try:
            classification = CompatibilityClass(str(payload.get("classification")))
        except ValueError:
            classification = CompatibilityClass.UNKNOWN
        raw_evidence = payload.get("evidence") or []
        evidence = tuple(
            Evidence(
                code=str(item.get("code") or ""),
                detail=str(item.get("detail") or ""),
                measured=bool(item.get("measured", True)),
            )
            for item in raw_evidence
            if isinstance(item, Mapping)
        )
        restored = payload.get("restored")
        limitations = payload.get("limitations")
        return cls(
            game=str(payload.get("game") or ""),
            classification=classification,
            reason_code=str(payload.get("reason_code") or ""),
            reason=str(payload.get("reason") or ""),
            checked_at=str(payload.get("checked_at") or ""),
            pid=_as_int(identity.get("pid")),
            start_time=str(identity.get("start_time") or ""),
            handle=_as_int(identity.get("handle")),
            window_title=str(identity.get("window_title") or ""),
            window_class=str(identity.get("window_class") or ""),
            window_mode=str(payload.get("window_mode") or ""),
            dpi=_as_int(payload.get("dpi")),
            app_id=str(identity.get("app_id") or ""),
            launch_id=str(identity.get("launch_id") or ""),
            owned=bool(identity.get("owned")),
            method=str(payload.get("method") or "preflight"),
            evidence=evidence,
            limitations=(
                tuple(str(item) for item in limitations)
                if isinstance(limitations, (list, tuple))
                else ()
            ),
            embedding=str(payload.get("embedding") or EMBEDDING_NOT_ATTEMPTED),
            capture=str(payload.get("capture") or CAPTURE_NOT_TESTED),
            restored=bool(restored) if restored is not None else None,
        )


def classify_preflight(
    identity: GameIdentity,
    preflight: EmbedPreflight,
    *,
    app_id: str = "",
    method: str = "preflight",
) -> CompatibilityRecord:
    """Turn the NF-13 preflight's measured answer into a compatibility record.

    The mapping is explicit, not inferred from prose:

    | preflight | class |
    | --- | --- |
    | supported, windowed | SUPPORTED |
    | supported, borderless (not covering the monitor) | SUPPORTED_WITH_LIMITATIONS |
    | fullscreen presentation / already a child | UNSUPPORTED |
    | missing/minimized/hidden/unreadable window, wrong identity, other platform | UNKNOWN |
    """
    window: WindowInfo | None = getattr(preflight, "window", None)
    code = str(getattr(preflight, "reason_code", "") or "")
    supported = bool(getattr(preflight, "supported", False))
    mode = str(getattr(preflight, "window_mode", "") or "")
    evidence: list[Evidence] = [
        Evidence("preflight", str(getattr(preflight, "reason", "") or code), measured=window is not None)
    ]
    limitations: list[str] = []
    restored: bool | None = None

    if supported:
        classification = CompatibilityClass.SUPPORTED
        if mode == "borderless":
            classification = CompatibilityClass.SUPPORTED_WITH_LIMITATIONS
            limitations.append(
                "Borderless window: it has no caption or frame to restore, and the "
                "embedding preserves its aspect by resizing the whole window."
            )
        if mode == "windowed":
            evidence.append(Evidence("window_mode", "windowed presentation", True))
    elif code in UNSUPPORTED_CODES:
        classification = CompatibilityClass.UNSUPPORTED
    elif code in UNKNOWN_CODES:
        classification = CompatibilityClass.UNKNOWN
    else:
        # An unrecognised refusal is not a pass and not a proven failure.
        classification = CompatibilityClass.UNKNOWN
        evidence.append(
            Evidence("unclassified_preflight", f"Unrecognised reason code {code!r}", False)
        )

    if window is not None:
        evidence.append(
            Evidence(
                "window",
                f"class {window.class_name!r}; style 0x{window.style:08X}; "
                f"parent {window.parent}; owner {window.owner}",
                True,
            )
        )
        evidence.append(
            Evidence(
                "geometry",
                f"rect {window.rect.width}x{window.rect.height} at "
                f"({window.rect.x}, {window.rect.y}); client "
                f"{window.client.width}x{window.client.height}; dpi {window.dpi}",
                True,
            )
        )
        if window.minimized:
            evidence.append(Evidence("window_minimized", "IsIconic is true", True))
        if not window.visible:
            evidence.append(Evidence("window_hidden", "IsWindowVisible is false", True))
    else:
        evidence.append(
            Evidence("window_unmeasured", "No window facts could be read for this attempt", False)
        )
    if code == "platform_unsupported":
        evidence.append(
            Evidence(
                "platform",
                "Native embedding is Windows-only; this host cannot measure it.",
                False,
            )
        )

    return CompatibilityRecord(
        game=str(identity.name or identity.window_title or ""),
        classification=classification,
        reason_code=code or ("ok" if supported else "unknown"),
        reason=str(getattr(preflight, "reason", "") or ""),
        pid=int(identity.pid),
        start_time=str(identity.start_time or ""),
        handle=int(getattr(window, "handle", 0) or 0),
        window_title=str(getattr(window, "title", "") or identity.window_title or ""),
        window_class=str(getattr(window, "class_name", "") or ""),
        window_mode=mode,
        dpi=int(getattr(window, "dpi", 0) or 0),
        app_id=str(app_id or ""),
        launch_id=str(identity.launch_id or ""),
        owned=bool(identity.owned),
        method=method,
        evidence=tuple(evidence),
        limitations=tuple(limitations),
        restored=restored,
    )


def _add_limitation(record: CompatibilityRecord, text: str) -> None:
    """Append a limitation once: recovery can run more than once."""
    if text not in record.limitations:
        record.limitations = record.limitations + (text,)


def apply_attempt(
    record: CompatibilityRecord, result: Mapping[str, Any]
) -> CompatibilityRecord:
    """Fold one ``EmbeddedGameHost.embed()`` result into the record.

    Three facts decide the outcome, and none of them is the API call's return
    value:

    * an embedded result is SUPPORTED (or keeps a more specific class);
    * a refusal that made **no native write** says nothing about compatibility —
      the window was fullscreen (UNSUPPORTED by policy) or the condition was
      transient (UNKNOWN: no window yet, minimized, hidden, wrong host);
    * only an attempt that actually mutated the window and then failed is a
      proven UNSUPPORTED, and it is the only case that can carry a failed
      restoration.
    """
    embedding = result.get("embedding") or {}
    recovery = embedding.get("recovery") or {}
    code = str(embedding.get("reason_code") or record.reason_code)
    embedded = bool(result.get("embedded"))
    record.method = "embed_attempt"

    if embedded:
        record.embedding = EMBEDDING_EMBEDDED
        record.reason_code = code or "embedded"
        record.reason = str(embedding.get("reason") or record.reason)
        if record.classification is CompatibilityClass.UNKNOWN:
            record.classification = CompatibilityClass.SUPPORTED
        record.restored = None
        return record

    steps = embedding.get("applied")
    applied = list(steps) if isinstance(steps, (list, tuple)) else list(recovery.get("applied") or ())
    mutated = bool(applied)
    record.embedding = (
        EMBEDDING_FAILED
        if mutated and code not in UNSUPPORTED_CODES
        else EMBEDDING_REFUSED
    )
    record.reason_code = code
    record.reason = str(embedding.get("reason") or record.reason)
    if code in UNSUPPORTED_CODES:
        record.classification = CompatibilityClass.UNSUPPORTED
    elif code in REFUSED_BEFORE_MUTATION or code in UNKNOWN_CODES or code in EXTERNAL_END_CODES:
        # A condition, not a verdict: the host looked, and the window was not
        # there (yet), not readable, not this process, or gone mid-attempt.
        record.classification = CompatibilityClass.UNKNOWN
    elif mutated:
        record.classification = CompatibilityClass.UNSUPPORTED
        restored = recovery.get("restored")
        record.restored = bool(restored) if restored is not None else None
        if restored is False:
            _add_limitation(
                record,
                "The original window could not be confirmed restored after the "
                "failed attempt; the game may be left in the attempted state.",
            )
    else:
        # An unrecognised refusal with no measured mutation is not proof of
        # incompatibility: keep the class the preflight reached.
        record.classification = CompatibilityClass.UNKNOWN
    return record


def apply_restore(
    record: CompatibilityRecord, result: Mapping[str, Any]
) -> CompatibilityRecord:
    """Fold a detach/cleanup result into the record without touching capture."""
    if result.get("restored") is not None:
        record.restored = bool(result.get("restored"))
        if record.restored is False:
            _add_limitation(record, "Detach could not confirm the original window state.")
    return record


def note_window_ended_embedding(
    record: CompatibilityRecord, code: str, detail: str
) -> CompatibilityRecord:
    """Record that the WINDOW itself undid a verified embedding.

    A game that minimizes itself on focus loss, hides, re-parents or recreates
    its window is still embeddable, but only with that limitation — and the
    record has to say so, otherwise it reads `SUPPORTED` while the host had to
    give the window back.
    """
    if record.classification is CompatibilityClass.SUPPORTED:
        record.classification = CompatibilityClass.SUPPORTED_WITH_LIMITATIONS
    _add_limitation(record, detail)
    record.evidence = record.evidence + (Evidence(code, detail, True),)
    return record


class CompatibilityLog:
    """Newest-first compatibility records, optionally persisted by the app.

    The log is the phase's "record the evidence" requirement: game name,
    process identity, window, mode, evidence, limitations and the timestamp of
    the check. Two hundred rows is the cap — compatibility history is a
    diagnostic, not an audit trail (which already exists elsewhere).
    """

    NAMESPACE = "novaforge_embedding_compat"
    MAX_RECORDS = 200

    def __init__(self, store: Any = None) -> None:
        self._store = store
        self._records: list[CompatibilityRecord] = []
        self._load()

    # -- reads -------------------------------------------------------------

    def all(self) -> list[CompatibilityRecord]:
        return list(self._records)

    def latest(self, game: str = "") -> CompatibilityRecord | None:
        wanted = str(game or "").strip().lower()
        if not wanted:
            return self._records[0] if self._records else None
        for record in self._records:
            if record.game.lower() == wanted:
                return record
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.NAMESPACE,
            "records": [record.to_dict() for record in self._records],
        }

    # -- writes ------------------------------------------------------------

    def record(self, record: CompatibilityRecord) -> CompatibilityRecord:
        """Store a SNAPSHOT of one record (newest first) and persist.

        The caller keeps refining the live record (a preflight, then the attempt
        that follows it), so storing the object itself would rewrite history:
        every row would show the last state and two rows would share one object.
        ``to_dict``/``from_dict`` is the boundary the store already uses, so the
        snapshot survives a round-trip unchanged.
        """
        snapshot = CompatibilityRecord.from_dict(record.to_dict())
        self._records.insert(0, snapshot)
        del self._records[self.MAX_RECORDS:]
        self._persist()
        return snapshot

    def _load(self) -> None:
        if self._store is None:
            return
        try:
            payload = self._store.read(self.NAMESPACE)
        except Exception:  # noqa: BLE001 - an unreadable history is not a failure
            return
        if not isinstance(payload, Mapping):
            return
        for raw in payload.get("records") or ():
            if not isinstance(raw, Mapping):
                continue
            # ONE unreadable row must not lose the whole history (or crash the
            # panel's constructor): skip it and keep the readable ones.
            try:
                self._records.append(CompatibilityRecord.from_dict(raw))
            except Exception:  # noqa: BLE001 - a corrupt row is skipped
                continue
        del self._records[self.MAX_RECORDS:]

    def _persist(self) -> None:
        if self._store is None:
            return
        try:
            self._store.write(self.NAMESPACE, self.to_dict())
        except Exception:  # noqa: BLE001 - persistence must not fail the check
            pass
