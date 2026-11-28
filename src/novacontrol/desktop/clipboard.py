"""Clipboard staging with an honest restore decision (DF-6 clipboard residue).

The Windows type path stages text on the clipboard and pastes it. That means
NovaControl *modifies the user's clipboard*, and the DF-6 investigation found
the staged text still sitting there after a failed paste — a privacy residue,
not an input leak, but a real one.

Restoring is only safe when the clipboard is still the one this operation
wrote. Windows exposes ``GetClipboardSequenceNumber`` for exactly that: it
increments whenever the clipboard's owner or contents change, so a different
sequence than the one read after our write means another application has since
taken the clipboard, and restoring would destroy their copy. In that case the
only honest outcome is to record that the residue was left alone.

This module is the pure decision; the controller owns the OS transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "ClipboardSnapshot",
    "ClipboardStatus",
    "RestorePlan",
    "plan_restore",
]


class ClipboardStatus(StrEnum):
    """What happened to the clipboard after a paste attempt."""

    #: Nothing was written, so there is nothing to undo.
    NOT_MODIFIED = "not_modified"
    #: The previous text was put back.
    RESTORED = "restored"
    #: The clipboard was empty before, and has been cleared again.
    CLEARED = "cleared"
    #: Another application took the clipboard; its value was left untouched.
    SKIPPED_CONCURRENT_CHANGE = "skipped_concurrent_change"
    #: The clipboard held something other than text, which cannot be restored.
    SKIPPED_NON_TEXT = "skipped_non_text"
    #: Windows refused access (another process holds the clipboard open).
    DENIED = "denied"
    #: The clipboard state could not be read at all.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ClipboardSnapshot:
    """The clipboard as it was before this operation wrote to it.

    ``sequence`` is ``GetClipboardSequenceNumber``. ``has_other`` records that
    the clipboard held a non-text format (an image, a file drop); text is the
    only format this path preserves, so an overwritten non-text clipboard is
    reported rather than silently counted as restored.
    """

    available: bool
    sequence: int = 0
    text: str | None = None
    has_text: bool = False
    has_other: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "sequence": self.sequence,
            "has_text": self.has_text,
            "has_other": self.has_other,
            "chars": len(self.text or ""),
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class RestorePlan:
    """The restore decision and the text to write back (if any)."""

    status: ClipboardStatus
    write_text: str | None = None
    clear: bool = False
    reason: str = ""

    @property
    def modifies_clipboard(self) -> bool:
        return self.write_text is not None or self.clear

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "clear": self.clear,
            "reason": self.reason,
            "modifies_clipboard": self.modifies_clipboard,
        }


def plan_restore(
    snapshot: ClipboardSnapshot,
    *,
    sequence_after_write: int,
    sequence_now: int,
    delivered: bool,
) -> RestorePlan:
    """Decide whether the previous clipboard contents may be put back.

    Order matters: a value another application wrote must never be overwritten,
    so the concurrency check comes before every other reason to restore.
    ``delivered`` does not change *whether* the clipboard is restored (a failed
    paste is exactly the case where the residue matters most); it is recorded
    by the caller so a failure is never mistaken for "nothing was staged".
    """
    del delivered  # recorded by the caller; the decision does not depend on it
    if not snapshot.available:
        return RestorePlan(
            ClipboardStatus.UNAVAILABLE,
            reason=(
                "the clipboard could not be read before staging, so its "
                "contents cannot be preserved"
            ),
        )
    if sequence_after_write <= 0:
        return RestorePlan(
            ClipboardStatus.SKIPPED_CONCURRENT_CHANGE,
            reason=(
                "the clipboard sequence number could not be read after staging, "
                "so a concurrent change cannot be ruled out"
            ),
        )
    if sequence_now != sequence_after_write:
        return RestorePlan(
            ClipboardStatus.SKIPPED_CONCURRENT_CHANGE,
            reason=(
                "another application changed the clipboard during the paste; "
                "its value was left in place"
            ),
        )
    if snapshot.has_other:
        return RestorePlan(
            ClipboardStatus.SKIPPED_NON_TEXT,
            reason=(
                "the clipboard held a non-text format before staging; only text "
                "can be restored, so the previous value is lost"
            ),
        )
    if snapshot.has_text and snapshot.text is not None:
        return RestorePlan(
            ClipboardStatus.RESTORED,
            write_text=snapshot.text,
            reason="the previous clipboard text was put back",
        )
    return RestorePlan(
        ClipboardStatus.CLEARED,
        clear=True,
        reason="the clipboard was empty before staging and has been cleared again",
    )
