"""Bounded preventive maintenance with previews and protected-file rules.

Maintenance is restricted to the application's own temporary/cache/log
surfaces.  It never walks the project tree, user documents, credentials or the
source tree.  Every mutating item states its impact and requires approval; a
preview is the default and running without approval is refused rather than
silently skipping the destructive half.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from novacontrol.self_management.models import (
    MaintenanceItem,
    MaintenanceKind,
    MaintenanceReport,
    new_id,
    utcnow,
)

#: Names/extensions that must never be removed by maintenance, even if they
#: somehow appear under a temporary directory.  This is a second line after the
#: allowlisted roots: a misconfigured cache path must not turn into a data leak.
_PROTECTED_NAMES = frozenset(
    {
        "credentials",
        "credentials.json",
        "secrets",
        "secrets.json",
        ".env",
        "settings",
        "settings.json",
        "novacontrol.sqlite3",
        "audit.jsonl",
        "tasks.json",
        "self_management.json",
    }
)
_PROTECTED_SUFFIXES = frozenset(
    {".py", ".md", ".toml", ".yaml", ".yml", ".key", ".pem", ".p12", ".pfx", ".db"}
)


def _human_bytes(value: int) -> str:
    size = float(max(0, int(value)))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _protected(path: Path) -> bool:
    name = path.name.lower()
    if name in _PROTECTED_NAMES:
        return True
    if name.startswith(".") or name.startswith("settings.") or name.startswith("credentials"):
        return True
    return path.suffix.lower() in _PROTECTED_SUFFIXES


class MaintenanceService:
    """Preview and execute maintenance inside explicit, allowlisted roots."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        config_report: Callable[[], Mapping[str, Any]] | None = None,
        stale_cache_seconds: float = 7 * 24 * 60 * 60,
        temp_max_age_seconds: float = 24 * 60 * 60,
        log_rotate_bytes: int = 2 * 1024 * 1024,
        resource_leak_mb: int = 2048,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self._config_report = config_report
        self.stale_cache_seconds = max(0.0, float(stale_cache_seconds))
        self.temp_max_age_seconds = max(0.0, float(temp_max_age_seconds))
        self.log_rotate_bytes = max(1024, int(log_rotate_bytes))
        self.resource_leak_mb = max(64, int(resource_leak_mb))
        self._clock = clock or utcnow
        self._last: MaintenanceReport | None = None

    @property
    def last(self) -> MaintenanceReport | None:
        return self._last

    def _roots(self) -> tuple[Path, ...]:
        return (self.data_dir / "tmp", self.data_dir / "cache")

    def _is_inside(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.data_dir)
        except ValueError:
            return False
        roots = self._roots()
        return any(path.resolve() == root or root in path.resolve().parents for root in roots)

    # -- preview -------------------------------------------------------------
    def preview(self) -> MaintenanceReport:
        started = self._clock()
        items: list[MaintenanceItem] = []
        now = started.timestamp()
        for root in self._roots():
            if not root.exists() or not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                age = now - stat.st_mtime
                kind = MaintenanceKind.TEMP_FILES if root.name == "tmp" else MaintenanceKind.STALE_CACHE
                threshold = self.temp_max_age_seconds if kind is MaintenanceKind.TEMP_FILES else self.stale_cache_seconds
                if age < threshold:
                    continue
                protected = _protected(path)
                items.append(
                    MaintenanceItem(
                        id=new_id("maint"),
                        kind=kind,
                        target=str(path),
                        action="delete",
                        detail=(
                            f"{'protected — will not delete' if protected else 'temporary/cache file older than its retention window'}"
                        ),
                        bytes_affected=stat.st_size,
                        files_affected=1,
                        destructive=not protected,
                        requires_approval=not protected,
                        protected=protected,
                        metadata={"age_seconds": round(age, 1)},
                    )
                )
        logs = self.data_dir / "logs"
        if logs.exists() and logs.is_dir():
            for path in sorted(logs.glob("*.log")):
                if not path.is_file() or path.is_symlink() or _protected(path):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_size > self.log_rotate_bytes:
                    items.append(
                        MaintenanceItem(
                            id=new_id("maint"),
                            kind=MaintenanceKind.LOG_ROTATION,
                            target=str(path),
                            action="rotate",
                            detail=f"{_human_bytes(stat.st_size)} exceeds {_human_bytes(self.log_rotate_bytes)}",
                            bytes_affected=stat.st_size,
                            files_affected=1,
                            destructive=False,
                            requires_approval=True,
                            metadata={"max_bytes": self.log_rotate_bytes},
                        )
                    )
        database = self.data_dir / "novacontrol.sqlite3"
        if database.exists() and database.is_file():
            try:
                connection = sqlite3.connect(str(database))
                try:
                    integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
                finally:
                    connection.close()
                items.append(
                    MaintenanceItem(
                        id=new_id("maint"),
                        kind=MaintenanceKind.DATABASE,
                        target=str(database),
                        action="optimize",
                        detail=f"SQLite quick_check: {integrity}",
                        bytes_affected=database.stat().st_size,
                        files_affected=1,
                        destructive=False,
                        requires_approval=False,
                    )
                )
            except sqlite3.DatabaseError as exc:
                items.append(
                    MaintenanceItem(
                        id=new_id("maint"),
                        kind=MaintenanceKind.DATABASE,
                        target=str(database),
                        action="inspect",
                        detail=f"database could not be checked: {type(exc).__name__}: {exc}",
                        destructive=False,
                        requires_approval=False,
                        status="needs_attention",
                    )
                )
        try:
            usage = shutil.disk_usage(self.data_dir)
            if usage.free < 512 * 1024 * 1024:
                items.append(
                    MaintenanceItem(
                        id=new_id("maint"),
                        kind=MaintenanceKind.STORAGE,
                        target=str(self.data_dir),
                        action="report",
                        detail=f"only {_human_bytes(usage.free)} free on the data volume",
                        bytes_affected=usage.free,
                        destructive=False,
                    )
                )
        except OSError:
            pass
        try:
            import psutil

            process = psutil.Process()
            rss = int(process.memory_info().rss)
            if rss > self.resource_leak_mb * 1024 * 1024:
                items.append(
                    MaintenanceItem(
                        id=new_id("maint"),
                        kind=MaintenanceKind.RESOURCE_LEAK,
                        target=f"pid {process.pid}",
                        action="inspect",
                        detail=f"process RSS {_human_bytes(rss)} exceeds {self.resource_leak_mb} MB",
                        bytes_affected=rss,
                        destructive=False,
                        status="needs_attention",
                    )
                )
        except Exception:
            pass
        stale = self._stale_processes()
        items.extend(stale)
        if self._config_report is not None:
            try:
                config_report = dict(self._config_report())
                if not config_report.get("valid", True):
                    items.append(
                        MaintenanceItem(
                            id=new_id("maint"),
                            kind=MaintenanceKind.CONFIGURATION,
                            target="persisted settings",
                            action="restore_known_good",
                            detail="; ".join(
                                str(item) for item in config_report.get("issues", ())
                            ),
                            destructive=False,
                            # The restore itself is approval-gated by the
                            # configuration control; maintenance only reports it.
                            requires_approval=False,
                            status="needs_attention",
                        )
                    )
            except Exception:
                pass
        report = MaintenanceReport(
            mode="preview",
            items=tuple(items),
            started_at=started,
            finished_at=self._clock(),
            skipped=tuple(
                f"{item.target} ({item.detail})" for item in items if item.protected
            ),
        )
        self._last = report
        return report

    def _stale_processes(self) -> list[MaintenanceItem]:
        rows: list[MaintenanceItem] = []
        try:
            import psutil

            current = psutil.Process(os.getpid())
            excluded = {current.pid}
            try:
                excluded.update(parent.pid for parent in current.parents())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            for process in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
                try:
                    pid = int(process.info.get("pid") or 0)
                    if pid in excluded:
                        continue
                    name = str(process.info.get("name") or "").lower()
                    cmdline = [str(item) for item in (process.info.get("cmdline") or ())]
                    lowered = [item.lower() for item in cmdline]
                    runs_module = any(
                        lowered[index] == "-m"
                        and index + 1 < len(lowered)
                        and lowered[index + 1] == "novacontrol"
                        for index in range(len(lowered))
                    )
                    runs_script = bool(cmdline) and Path(cmdline[0]).name.lower() in {
                        "novacontrol",
                        "novacontrol.exe",
                    }
                    # A path merely containing "NovaControl" is the user's
                    # project, not a stale NovaControl runtime. Require the
                    # actual module/entrypoint and a Python (or packaged)
                    # process; parent shells of the live server are excluded too.
                    if "python" not in name and not runs_script:
                        continue
                    if not (runs_module or runs_script):
                        continue
                    command = " ".join(cmdline)
                    rows.append(
                        MaintenanceItem(
                            id=new_id("maint"),
                            kind=MaintenanceKind.STALE_PROCESS,
                            target=f"pid {pid}",
                            action="inspect",
                            detail=f"possible NovaControl process: {command[:180]}",
                            destructive=False,
                            metadata={"pid": pid},
                        )
                    )
                except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
                    continue
        except Exception:
            return []
        return rows

    # -- execution -----------------------------------------------------------
    def run(self, *, approved: bool = False, approved_by: str = "") -> MaintenanceReport:
        preview = self.preview()
        destructive = [item for item in preview.items if item.requires_approval]
        if destructive and not approved:
            raise PermissionError(
                "Maintenance has "
                f"{len(destructive)} approval-required item(s); run preview first, then approve."
            )
        started = self._clock()
        done: list[MaintenanceItem] = []
        removed = 0
        changed = 0
        errors: list[str] = []
        for item in preview.items:
            if item.protected:
                done.append(replace(item, status="skipped_protected"))
                continue
            if item.action in ("report", "inspect"):
                done.append(item)
                continue
            target = Path(item.target)
            try:
                if item.kind in (MaintenanceKind.TEMP_FILES, MaintenanceKind.STALE_CACHE):
                    if not self._is_inside(target) or _protected(target):
                        done.append(replace(item, status="skipped_protected"))
                        continue
                    size = target.stat().st_size
                    target.unlink()
                    removed += size
                    changed += 1
                    done.append(replace(item, status="done"))
                elif item.kind is MaintenanceKind.LOG_ROTATION:
                    if _protected(target) or not target.is_file():
                        continue
                    rotated = target.with_name(f"{target.name}.{int(time.time())}.bak")
                    target.rename(rotated)
                    changed += 1
                    done.append(
                        replace(
                            item,
                            status="done",
                            metadata={**dict(item.metadata), "rotated_to": str(rotated)},
                        )
                    )
                elif item.kind is MaintenanceKind.DATABASE and item.action == "optimize":
                    connection = sqlite3.connect(str(target))
                    try:
                        connection.execute("PRAGMA optimize")
                    finally:
                        connection.close()
                    changed += 1
                    done.append(replace(item, status="done"))
                elif item.kind is MaintenanceKind.CONFIGURATION:
                    # Configuration recovers through its own versioned,
                    # audit-heavy control (including autonomy and pause gates).
                    # Maintenance must not pretend it restored settings here.
                    done.append(
                        replace(
                            item,
                            status="manual_action_required",
                            detail=(
                                f"{item.detail} - use Restore Latest Known-Good "
                                "in the configuration controls"
                            ),
                        )
                    )
                else:
                    done.append(item)
            except Exception as exc:
                errors.append(f"{target}: {type(exc).__name__}: {exc}")
        report = MaintenanceReport(
            mode="run",
            items=tuple(done),
            started_at=started,
            finished_at=self._clock(),
            approved_by=approved_by,
            removed_bytes=removed,
            changed_files=changed,
            skipped=preview.skipped,
            errors=tuple(errors),
        )
        self._last = report
        return report

    def status(self) -> dict[str, Any]:
        return {
            "data_dir": str(self.data_dir),
            "roots": [str(root) for root in self._roots()],
            "stale_cache_seconds": self.stale_cache_seconds,
            "temp_max_age_seconds": self.temp_max_age_seconds,
            "log_rotate_bytes": self.log_rotate_bytes,
            "resource_leak_mb": self.resource_leak_mb,
            "last": self._last.to_dict() if self._last else None,
        }


__all__ = ["MaintenanceService"]
