"""Checkpoint management: write one, check it, resume from the last GOOD one.

A checkpoint is only worth having if it can be loaded. Every checkpoint this
manager writes goes through a temporary file and an atomic replace — the same
pattern the audit trail and the evaluation stores use — and every checkpoint it
is asked to resume from is RE-VALIDATED against the filesystem first: a missing,
empty or unparsable file is reported as ``incomplete`` or ``corrupt`` and is
never offered as a resume point. A run that resumes from a corrupt checkpoint
would waste the operator's time and say nothing about why.

Retention is explicit: ``apply_retention`` keeps the newest ``max_checkpoints``
checkpoints plus the best one, and deletes the FILES it drops (they are the
large artifacts). Nothing is ever overwritten in place — a new checkpoint is a
new file, so an older one stays readable until retention removes it.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from novacontrol.evaluation.models import now_iso
from novacontrol.training.models import CheckpointRecord

CHECKPOINT_COMPLETE = "complete"
CHECKPOINT_INCOMPLETE = "incomplete"
CHECKPOINT_CORRUPT = "corrupt"

_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


class CheckpointRepositoryLike(Protocol):
    """The slice of the checkpoint repository this manager uses."""

    def save(self, record: CheckpointRecord) -> CheckpointRecord: ...

    def list(self, *, run_id: str = "", limit: int = 0) -> Sequence[CheckpointRecord]: ...

    def remove(self, checkpoint_id: str) -> int: ...


def _slug(value: str) -> str:
    cleaned = _SLUG.sub("_", str(value or "").strip())
    cleaned = cleaned.strip("._")
    return cleaned or "run"


class CheckpointManager:
    """Writes, validates, lists and prunes one run's checkpoints."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_checkpoints: int = 3,
        repository: CheckpointRepositoryLike | None = None,
    ) -> None:
        self.root = Path(root)
        self.max_checkpoints = max(1, int(max_checkpoints))
        self.repository = repository
        self._records: dict[str, list[CheckpointRecord]] = {}

    # -- locations -------------------------------------------------------------

    def directory_for(self, run_id: str) -> Path:
        """A directory under the root; a run id can never escape it."""
        resolved_root = self.root.resolve()
        candidate = resolved_root / _slug(run_id)
        try:
            candidate.resolve().relative_to(resolved_root)
        except ValueError:  # pragma: no cover - _slug already strips traversal
            candidate = resolved_root / "run"
        return candidate

    # -- writing ---------------------------------------------------------------

    def create(
        self,
        run_id: str,
        *,
        epoch: int,
        step: int,
        kind: str = "periodic",
        metrics: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> CheckpointRecord:
        """Write one checkpoint and return its record.

        A write that fails does NOT raise: the record comes back ``incomplete``
        with the reason, because losing a checkpoint must not lose the run that
        was making it.
        """
        directory = self.directory_for(run_id)
        filename = f"{_slug(kind)}-epoch{int(epoch)}-step{int(step)}.json"
        target = directory / filename
        record = CheckpointRecord(
            run_id=str(run_id),
            path=str(target),
            kind=_slug(kind) or "periodic",
            epoch=max(0, int(epoch)),
            step=max(0, int(step)),
            metrics=dict(metrics or {}),
        )
        body: dict[str, Any] = {
            "format": "novacontrol.training.checkpoint",
            "schema_version": record.schema_version,
            "checkpoint_id": record.checkpoint_id,
            "run_id": record.run_id,
            "epoch": record.epoch,
            "step": record.step,
            "kind": record.kind,
            "metrics": dict(metrics or {}),
            "preprocessing_version": "phase16.1",
            "written_at": now_iso(),
        }
        if payload:
            body["payload"] = dict(payload)
        temporary = target.with_suffix(target.suffix + ".tmp")
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(body, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, target)
        except OSError as exc:
            record = replace(
                record,
                status=CHECKPOINT_INCOMPLETE,
                reason=f"the checkpoint could not be written: {type(exc).__name__}: {exc}",
            )
            self._remember(record)
            return record
        size = target.stat().st_size if target.exists() else 0
        record = replace(record, size_bytes=size)
        self._remember(record)
        return record

    def _remember(self, record: CheckpointRecord) -> None:
        entries = self._records.setdefault(record.run_id, [])
        entries.append(record)
        if self.repository is not None:
            # A record store is not the checkpoint: failing to mirror the record
            # must not lose the file that was just written.
            with contextlib.suppress(Exception):
                self.repository.save(record)

    # -- reading ---------------------------------------------------------------

    def validate(self, record: CheckpointRecord) -> CheckpointRecord:
        """Check the file behind a record. Never raises; returns the verdict."""
        path = Path(record.path)
        if not record.path:
            return replace(record, status=CHECKPOINT_INCOMPLETE, reason="no path recorded")
        if not path.exists():
            return replace(
                record, status=CHECKPOINT_INCOMPLETE, reason="the checkpoint file is missing"
            )
        try:
            if path.stat().st_size <= 0:
                return replace(
                    record, status=CHECKPOINT_INCOMPLETE, reason="the checkpoint file is empty"
                )
            with path.open("r", encoding="utf-8") as handle:
                parsed = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            return replace(
                record,
                status=CHECKPOINT_CORRUPT,
                reason=f"the checkpoint could not be read: {type(exc).__name__}",
            )
        if not isinstance(parsed, Mapping) or parsed.get("format") != (
            "novacontrol.training.checkpoint"
        ):
            return replace(
                record,
                status=CHECKPOINT_CORRUPT,
                reason="the checkpoint is not a NovaControl training checkpoint",
            )
        if str(parsed.get("checkpoint_id", "")) != record.checkpoint_id:
            return replace(
                record,
                status=CHECKPOINT_CORRUPT,
                reason="the checkpoint's own id does not match its record",
            )
        size = path.stat().st_size
        return replace(record, status=CHECKPOINT_COMPLETE, reason="", size_bytes=size)

    def load(self, record: CheckpointRecord) -> dict[str, Any] | None:
        """The checkpoint's payload, or ``None`` when it cannot be trusted."""
        checked = self.validate(record)
        if checked.status != CHECKPOINT_COMPLETE:
            return None
        try:
            with Path(checked.path).open("r", encoding="utf-8") as handle:
                parsed = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, Mapping):
            return None
        payload = parsed.get("payload")
        return dict(payload) if isinstance(payload, Mapping) else dict(parsed)

    def list(self, run_id: str) -> tuple[CheckpointRecord, ...]:
        """Every checkpoint this manager knows for a run, newest last."""
        rows: list[CheckpointRecord] = list(self._records.get(str(run_id), []))
        if self.repository is not None:
            with contextlib.suppress(Exception):
                rows.extend(self.repository.list(run_id=str(run_id)))
        seen: set[str] = set()
        unique: list[CheckpointRecord] = []
        for record in rows:
            if record.checkpoint_id in seen:
                continue
            seen.add(record.checkpoint_id)
            unique.append(record)
        unique.sort(key=lambda item: (item.epoch, item.step, item.created_at))
        return tuple(unique)

    def best(self, run_id: str) -> CheckpointRecord | None:
        for record in reversed(self.list(run_id)):
            if record.kind == "best":
                return record
        return None

    def resume_point(self, run_id: str) -> tuple[CheckpointRecord | None, str]:
        """The newest checkpoint that actually loads, with a reason when none does.

        Validation is against the filesystem NOW, not against what was true when
        the record was written: a file deleted after the fact is discovered here
        rather than during a resume.
        """
        candidates = list(reversed(self.list(run_id)))
        if not candidates:
            return (None, f"no checkpoint is recorded for run {run_id!r}")
        failures: list[str] = []
        for record in candidates:
            checked = self.validate(record)
            if checked.status == CHECKPOINT_COMPLETE:
                return (checked, "")
            # The validation result carries the reason; the record still carries
            # the status it had when it was written, which is exactly the stale
            # answer that must not be reported here.
            failures.append(checked.reason or checked.status or CHECKPOINT_INCOMPLETE)
        return (
            None,
            "no checkpoint could be loaded: " + "; ".join(dict.fromkeys(failures)),
        )

    # -- retention -------------------------------------------------------------

    def apply_retention(
        self, run_id: str, *, max_checkpoints: int | None = None
    ) -> tuple[str, ...]:
        """Keep the newest N plus the best; delete the files of what it drops.

        Returns the ids removed. The best checkpoint and the newest one are
        never removed, so a resume point always exists after a prune.
        """
        wanted = self.max_checkpoints if max_checkpoints is None else max_checkpoints
        limit = max(1, int(wanted))
        records = list(self.list(run_id))
        if len(records) <= limit:
            return ()
        protected = {records[-1].checkpoint_id}
        best = self.best(run_id)
        if best is not None:
            protected.add(best.checkpoint_id)
        removable = [
            record for record in records[:-limit] if record.checkpoint_id not in protected
        ]
        removed: list[str] = []
        for record in removable:
            path = Path(record.path)
            try:
                if record.path and path.exists() and path.is_file():
                    path.unlink()
            except OSError:
                continue
            removed.append(record.checkpoint_id)
            entries = self._records.get(str(run_id))
            if entries:
                self._records[str(run_id)] = [
                    item for item in entries if item.checkpoint_id != record.checkpoint_id
                ]
            if self.repository is not None:
                # The record goes too: a repository row for a deleted file would
                # put the path back into every later listing.
                remove = getattr(self.repository, "remove", None)
                if callable(remove):
                    with contextlib.suppress(Exception):
                        remove(record.checkpoint_id)
        return tuple(removed)

    def clear(self) -> int:
        """Forget every record of every run; the files stay on disk.

        The repository rows go too. Leaving them would make ``clear()`` a
        promise the manager cannot keep: a later ``list()`` merges the
        repository, so the "cleared" checkpoints would come straight back.
        """
        known = {
            row.checkpoint_id
            for rows in self._records.values()
            for row in rows
        }
        self._records.clear()
        if self.repository is not None:
            remove = getattr(self.repository, "remove", None)
            if callable(remove):
                for row in self.repository.list():
                    if row.checkpoint_id in known:
                        # A row this manager wrote: already counted, so it is
                        # removed rather than counted twice.
                        with contextlib.suppress(Exception):
                            remove(row.checkpoint_id)
                        continue
                    with contextlib.suppress(Exception):
                        if remove(row.checkpoint_id):
                            known.add(row.checkpoint_id)
        return len(known)


__all__ = [
    "CHECKPOINT_COMPLETE",
    "CHECKPOINT_CORRUPT",
    "CHECKPOINT_INCOMPLETE",
    "CheckpointManager",
    "CheckpointRepositoryLike",
]
