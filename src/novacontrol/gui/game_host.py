"""The NovaForge embedded-game tab: a native game surface inside the dashboard.

This module is deliberately thin. :class:`~novacontrol.novaforge.embedding.EmbeddedGameHost`
owns every identity, style, geometry and restoration decision; this panel only

* reads the NF-12 library through ``NovaControlApplication`` (no second catalog),
* plans/executes a launch through the same approval token the web UI uses,
* hands the game panel's native handle to the host and renders its state.

The game surface is a real native child window. It is not a screenshot, a video
of the game, or a captured frame: when the host reports ``embedded``, the game's
own HWND has this widget's HWND as its parent (proven by the host's read-back).
That is also why no browser panel can host it — a page cannot parent an HWND.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from novacontrol.novaforge.compat import CAPTURE_NOT_TESTED, CompatibilityLog
from novacontrol.novaforge.embedding import (
    CLOSED,
    DETACHED,
    EMBEDDED,
    EMBEDDING_UNSUPPORTED,
    RUNNING,
    EmbeddedGameHost,
    GameIdentity,
    Win32WindowApi,
)

_RUNNER = Callable[[Any], Any]


def _page_of(entry: Mapping[str, Any]) -> str:
    status = str(entry.get("status") or "unknown").replace("_", " ").lower()
    tested = " · launch-tested" if entry.get("tested") else ""
    return f"{entry.get('title') or 'Untitled'}  —  {status}{tested}"


class GameSurface(QWidget):
    """The native window the game is reparented under.

    ``WA_NativeWindow`` forces this widget to own a real HWND before ``winId()``
    is read. A plain Qt widget would otherwise be drawn by Qt and have no handle
    a game could become a child of.
    """

    def __init__(self, host: EmbeddedGameHost, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.host = host
        self.setObjectName("gameSurface")
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setMinimumSize(480, 270)
        # A CHILD timer, not QTimer.singleShot: it coalesces a resize/show burst
        # into one fit, and it dies with the widget instead of leaving a queued
        # callback aimed at a deleted C++ object (which raises on close).
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(0)
        self._fit_timer.timeout.connect(self._fit)

    def native_handle(self) -> int:
        return int(self.winId())

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._schedule_fit()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._schedule_fit()

    def _schedule_fit(self) -> None:
        # One deferred fitting per resize/restore burst, never a poll loop. The
        # host reads the host HWND's PHYSICAL client rect itself, so Qt's
        # logical size (and high-DPI scaling) never decides the game geometry.
        self._fit_timer.start()

    def _fit(self) -> None:
        if self.host.state == EMBEDDED:
            self.host.resize()

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#030810"))
        if self.host.state == EMBEDDED:
            painter.end()
            return
        painter.setPen(QColor("#8da4c2"))
        painter.drawText(
            self.rect(),
            Qt.AlignmentFlag.AlignCenter,
            "No game embedded.\nRefresh the library, launch a game, then Embed it here.\n"
            "The game window is reparented, not captured or streamed.",
        )
        painter.end()


class GameHostPanel(QWidget):
    """Library + launch + embed controls for one native game surface."""

    def __init__(
        self,
        nova: Any,
        run: _RUNNER,
        *,
        log: Callable[[str], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.nova = nova
        self._run = run
        self._log = log or (lambda _message: None)
        self.host = EmbeddedGameHost(Win32WindowApi())
        # NF-14: the records outlive one panel (and one process) through the
        # application's state store, which drops an unwritable snapshot instead
        # of raising. No store means the log is simply in memory.
        self._compat_log = CompatibilityLog(getattr(nova, "state_store", None))
        self._library: dict[str, Any] = {}
        self._entries: list[dict[str, Any]] = []
        self._owned: dict[str, Any] | None = None
        self._last_result: dict[str, Any] = {}
        self._last_state = self.host.state

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.addWidget(self._build_library_box())
        layout.addWidget(self._build_controls())
        layout.addWidget(self._build_surface_box(), stretch=1)

        self._timer = QTimer(self)
        self._timer.setInterval(750)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._render_host()

    # -- construction ------------------------------------------------------

    def _build_library_box(self) -> QWidget:
        box = QGroupBox("NovaForge game library (NF-12 catalog)")
        layout = QVBoxLayout(box)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search discovered titles")
        self.search.returnPressed.connect(self.refresh_library)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh_library)
        row.addWidget(self.search, stretch=1)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)

        self.library = QComboBox()
        self.library.currentIndexChanged.connect(self._show_selected)
        layout.addWidget(self.library)

        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(80)
        self.details.setPlaceholderText("Refresh to read the catalog.")
        layout.addWidget(self.details)
        return box

    def _build_controls(self) -> QWidget:
        box = QGroupBox("Launch and host")
        layout = QVBoxLayout(box)
        row = QHBoxLayout()
        self.launch_button = QPushButton("Launch…")
        self.embed_button = QPushButton("Embed Game")
        self.detach_button = QPushButton("Detach / Restore")
        self.focus_button = QPushButton("Focus Game")
        self.close_button = QPushButton("Close Test-Launched Game")
        self.launch_button.clicked.connect(self._launch)
        self.embed_button.clicked.connect(self._embed)
        self.detach_button.clicked.connect(self._detach)
        self.focus_button.clicked.connect(self._focus)
        self.close_button.clicked.connect(self._close)
        for button in (self.launch_button, self.embed_button, self.detach_button,
                       self.focus_button, self.close_button):
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)

        self.embedding_label = QLabel()
        self.window_label = QLabel()
        self.capture_label = QLabel()
        self.compatibility_label = QLabel()
        self.fallback_label = QLabel()
        self.compatibility_label.setObjectName("compatibilityLabel")
        self.fallback_label.setObjectName("fallbackLabel")
        for label in (self.embedding_label, self.window_label, self.capture_label,
                      self.compatibility_label, self.fallback_label):
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(label)
        return box

    def _build_surface_box(self) -> QWidget:
        box = QGroupBox("Embedded game surface (real native window)")
        layout = QVBoxLayout(box)
        self.surface = GameSurface(self.host)
        layout.addWidget(self.surface, stretch=1)
        return box

    # -- library -----------------------------------------------------------

    def ensure_loaded(self) -> None:
        """Read the library once when the tab is first opened."""
        if not self._library:
            self.refresh_library()

    def refresh_library(self) -> None:
        """Read the ONE catalog through the application's NF-12 method."""
        try:
            payload = self._run(self.nova.game_library(self.search.text().strip()))
        except Exception as exc:  # noqa: BLE001 - the panel reports, never crashes
            self.details.setPlainText(f"The library could not be read: {type(exc).__name__}: {exc}")
            return
        self._library = dict(payload or {})
        self._entries = list(self._library.get("entries") or [])
        self._owned = self._library.get("owned")
        previous = str(self._selected_entry().get("title") or "") if self._entries else ""
        self.library.blockSignals(True)
        self.library.clear()
        for entry in self._entries:
            self.library.addItem(_page_of(entry), entry)
        if self._entries:
            index = next(
                (i for i, entry in enumerate(self._entries)
                 if str(entry.get("title") or "") == previous),
                0,
            )
            self.library.setCurrentIndex(index)
        else:
            self.library.addItem("No discovered games", None)
        self.library.blockSignals(False)
        self._show_selected()
        self._render_host()
        self._log(
            f"NovaForge library: {self._library.get('matched', 0)} of "
            f"{self._library.get('total', 0)} titles"
        )

    def _selected_entry(self) -> dict[str, Any]:
        data = self.library.currentData()
        return dict(data) if isinstance(data, Mapping) else {}

    def _show_selected(self) -> None:
        entry = self._selected_entry()
        if not entry:
            self.details.setPlainText(
                "No row selected. Refresh the library; a title is launchable only when "
                "the catalog says INSTALLED_AND_LAUNCHABLE."
            )
            self._render_host()
            return
        lines = [
            f"{entry.get('title')} — {entry.get('status')}",
            str(entry.get("reason") or ""),
        ]
        artifact = entry.get("artifact") or {}
        if artifact:
            lines.append(
                f"Source: {artifact.get('source') or 'unknown'} · target: "
                f"{artifact.get('launch_target') or 'none'}"
            )
            if artifact.get("install_dir"):
                lines.append(f"Install: {artifact.get('install_dir')}")
        running = entry.get("running") or {}
        if running:
            lines.append(
                f"Running: pid {running.get('pid')} · {running.get('name')} · "
                f"window {running.get('window')!r}"
            )
        if entry.get("tested"):
            lines.append("This process has launch-tested this title.")
        lines.append(
            "A catalog row is not a launch test and a launch is not a verified frame."
        )
        self.details.setPlainText("\n".join(lines))
        self._render_host()

    # -- launch ------------------------------------------------------------

    def _launch(self) -> None:
        entry = self._selected_entry()
        if not entry:
            QMessageBox.information(self, "Launch", "Select a library row first.")
            return
        if self.host.state == EMBEDDED:
            QMessageBox.information(
                self, "Launch",
                "A game is embedded. Detach it before launching another title.",
            )
            return
        command = f"launch {entry.get('title')}"
        try:
            plan = self._run(self.nova.plan_game_launch(command))
        except Exception as exc:  # noqa: BLE001 - a refused plan is a result
            QMessageBox.warning(self, "Launch", f"The plan failed: {type(exc).__name__}: {exc}")
            return
        approval = plan.get("approval") or {}
        token = str(approval.get("token") or "")
        if not token:
            self.host.mark_closed("Nothing was launched; the plan carried no token.")
            self._render_host()
            self._render_plan(plan)
            QMessageBox.information(
                self, "Nothing to launch",
                str(plan.get("summary") or "The plan carries no approval token."),
            )
            return
        steps = "\n".join(f"• {step}" for step in plan.get("steps") or [])
        answer = QMessageBox.question(
            self,
            f"Launch {entry.get('title')}?",
            f"{plan.get('summary')}\n\n{steps}\n\nThis starts the verified installation "
            "once. It is a test launch: no input is sent to the game.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.host.mark_closed("The launch was cancelled before anything started.")
            self._render_host()
            self._render_plan(plan)
            return
        self.host.set_launching(str(entry.get("title") or ""))
        self._render_host()
        try:
            result = self._run(
                self.nova.execute_game_launch(command, approval_token=token)
            )
        except Exception as exc:  # noqa: BLE001 - report the refusal, do not crash
            self.host.mark_closed(f"The launch was refused: {type(exc).__name__}: {exc}")
            self._render_host()
            QMessageBox.warning(self, "Launch refused", str(exc))
            return
        self._last_result = dict(result or {})
        game = self._last_result.get("game") or {}
        launch = game.get("launch") or {}
        if launch.get("pid"):
            try:
                self.host.set_running(
                    {
                        "pid": launch.get("pid"),
                        "start_time": launch.get("start_time"),
                        "window": launch.get("window"),
                        "name": launch.get("name"),
                        "owned": True,
                        "launch_id": launch.get("launch_id"),
                    },
                    name=str(launch.get("name") or entry.get("title") or ""),
                )
            except (ValueError, RuntimeError) as exc:
                self.host.mark_closed(str(exc))
        else:
            self.host.mark_closed(str(self._last_result.get("summary") or "No process started."))
        self._render_result(self._last_result)
        self._render_host()
        self.refresh_library()
        self._log(f"Launch: {self._last_result.get('summary', '')}")

    def _render_plan(self, plan: Mapping[str, Any]) -> None:
        stages = plan.get("stages") or {}
        lines = [str(plan.get("summary") or ""), "", "Embedding has not been attempted."]
        memory = plan.get("memory") or {}
        snapshot = memory.get("snapshot") or {}
        if snapshot:
            lines.append(
                f"Resource gate: {snapshot.get('free_gb')} GB free, "
                f"{snapshot.get('used_percent')}% used — {'pass' if memory.get('ok') else 'refused'}"
            )
        lines.append(f"Stages: {stages}")
        self.details.setPlainText("\n".join(lines))

    # -- embed / detach ----------------------------------------------------

    def _identity_for_embed(self) -> GameIdentity | None:
        owned = self._owned or {}
        if owned.get("pid"):
            return GameIdentity(
                pid=int(owned.get("pid") or 0),
                start_time=str(owned.get("start_time") or ""),
                window_title=str(owned.get("window") or ""),
                name=str(owned.get("name") or ""),
                owned=True,
                launch_id=str(owned.get("launch_id") or ""),
            )
        entry = self._selected_entry()
        running = entry.get("running") or {}
        if running.get("pid"):
            return GameIdentity(
                pid=int(running.get("pid") or 0),
                start_time=str(running.get("start_time") or ""),
                window_title=str(running.get("window") or ""),
                name=str(entry.get("title") or running.get("name") or ""),
                owned=False,
            )
        if self.host.identity is not None and self.host.state in {
            RUNNING, DETACHED, EMBEDDING_UNSUPPORTED
        }:
            return self.host.identity
        return None

    def _embed(self) -> None:
        if self.host.state == EMBEDDED:
            QMessageBox.information(self, "Embed", "This game is already embedded.")
            return
        identity = self._identity_for_embed()
        if identity is None:
            QMessageBox.information(
                self, "Embed",
                "No running game to embed. Launch a test game first, or attach to an "
                "instance that is already running (select its row).",
            )
            return
        # Classify BEFORE touching anything (read-only), record it, then let the
        # attempt refine the same record. A preflight refusal is already an
        # answer, and it is stored even when nothing was attempted.
        record = self.host.probe_compatibility(
            identity, app_id=str(identity.launch_id or identity.name)
        )
        self._compat_log.record(record)
        result = self.host.embed(identity, self.surface.native_handle())
        self._remember_compatibility()
        pre = result.get("embedding") or {}
        if not result.get("embedded") and pre.get("reason_code") == "fullscreen_presentation_unsupported":
            self._render_host()
            QMessageBox.warning(self, "Fullscreen window", str(pre.get("reason") or ""))
            return
        self._last_state = str(result.get("state") or "")
        self._render_host()
        if not result.get("embedded"):
            QMessageBox.warning(
                self,
                "Embedding failed",
                str((result.get("embedding") or {}).get("reason") or "The window was not embedded."),
            )
            self._log("Embedding failed: " + str((result.get("embedding") or {}).get("reason_code")))
            return
        self._timer.start()
        self._log("Game window embedded and verified")

    def _remember_compatibility(self) -> None:
        """Persist the host's current record; the log is the durable evidence."""
        record = self.host.compatibility
        if record is not None:
            self._compat_log.record(record)

    def _detach(self) -> None:
        result = self.host.detach()
        self._remember_compatibility()
        self._last_state = self.host.state
        self._render_host()
        if not result.get("detached"):
            QMessageBox.information(self, "Detach", str(result.get("reason") or "Nothing to detach."))
            return
        self._log("Game window detached; original window restored")

    def _focus(self) -> None:
        result = self.host.focus_game()
        self._render_host()
        if not result.get("focused"):
            QMessageBox.information(self, "Focus", str(result.get("reason") or "Focus was refused."))

    def _close(self) -> None:
        identity = self.host.identity
        if identity is None or not self.host.close_allowed:
            QMessageBox.information(
                self, "Close",
                "This game was not started by this process, so it is not ours to close. "
                "Close it from the game itself; the host only detaches.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Close test-launched game?",
            "This asks the game's own window to close (no process kill) and confirms it "
            "exited. Detach first if you want to watch its normal shutdown in the "
            "restored window.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.host.state == EMBEDDED:
            self.host.detach()
        try:
            result = self._run(self.nova.close_game_launch(launch_id=identity.launch_id))
        except Exception as exc:  # noqa: BLE001 - the panel reports the refusal
            self._render_host()
            QMessageBox.warning(self, "Close refused", f"{type(exc).__name__}: {exc}")
            return
        if result.get("closed"):
            self.host.mark_closed(str(result.get("summary") or "The game exited."))
            self._log(f"Game closed: {result.get('summary', '')}")
        else:
            self._log(f"Close not confirmed: {result.get('summary', '')}")
        self._render_host()
        self.refresh_library()

    # -- refresh / reporting ----------------------------------------------

    def _tick(self) -> None:
        payload = self.host.check()
        # NF-14: a launcher can create the real window seconds after the process
        # appeared. The existing timer is the bounded retry — no second process
        # manager and no relaunch. Only while running/launching, never while
        # embedded (the window is already known then).
        if payload.get("state") in {RUNNING, "launching"} and not payload.get("window"):
            refreshed = self.host.refresh_window()
            if refreshed.get("window_found"):
                payload = refreshed
                self._log(
                    "Game window detected after the launch: "
                    + str((refreshed.get("window") or {}).get("title") or "")
                )
                self._render_host()
        state = str(payload.get("state") or "")
        if state != self._last_state:
            self._last_state = state
            self._render_host()
            if state in {CLOSED, DETACHED}:
                self._log(
                    "Host state: " + state + " — "
                    + str((payload.get("embedding") or {}).get("reason") or "")
                )
        if state == CLOSED:
            # The window is gone; there is nothing left to poll. A later launch
            # restarts this timer explicitly instead of leaving a free-running loop.
            self._timer.stop()

    def _observation(self) -> dict[str, Any]:
        return dict(self._library.get("observation") or {})

    def _render_host(self) -> None:
        payload = self.host.status_payload(observation=self._observation())
        embedding = payload.get("embedding") or {}
        window = payload.get("window") or {}
        identity = payload.get("identity") or {}
        self.embedding_label.setText(
            f"Embedding: {payload.get('state_label')}"
            + (f" — {embedding.get('reason')}" if embedding.get("reason") else "")
        )
        if identity:
            self.window_label.setText(
                f"Identity: pid {identity.get('pid')} · start {identity.get('start_time')} · "
                f"hwnd {window.get('handle') or 'not found'} · window "
                f"{window.get('title') or identity.get('window_title')!r} · "
                f"dpi {window.get('dpi') or 'unknown'} · owned by this process: "
                f"{payload.get('owned')}"
            )
        else:
            self.window_label.setText("Identity: no game associated with the host yet.")
        observation = payload.get("observation") or {}
        reason = str(observation.get("reason") or "")
        self.capture_label.setText(
            "Capture: "
            + ("available" if observation.get("available") else "unavailable")
            + (f" — {reason}" if reason else "")
            + " · embedding and capture are separate facts (no frame is inferred from a launch)"
        )

        # NF-14: compatibility is its own line, its own record and its own
        # vocabulary. It is never derived from the capture line above, and the
        # capture axis of the record stays NOT_TESTED however well embedding
        # worked (a verified parent is not a verified frame).
        compatibility = payload.get("compatibility") or {}
        compat_class = str(compatibility.get("classification") or "NOT_TESTED")
        compat_reason = str(compatibility.get("reason") or "")
        checks = len(compatibility.get("evidence") or [])
        self.compatibility_label.setText(
            f"Compatibility: {compat_class}"
            + (f" — {compat_reason}" if compat_reason else "")
            + f" · {checks} evidence row(s)"
            + f" · checked {compatibility.get('checked_at') or 'not yet'}"
            + f" · capture compatibility: {compatibility.get('capture') or CAPTURE_NOT_TESTED}"
            + " (assessed separately, never inferred from embedding)"
            + (
                "\nLimitations: " + " | ".join(compatibility.get("limitations") or [])
                if compatibility.get("limitations")
                else ""
            )
        )
        fallback = payload.get("fallback") or {}
        if payload.get("state") == EMBEDDED or not fallback.get("active"):
            self.fallback_label.setText("")
        else:
            steps = " ".join(str(step) for step in fallback.get("next_steps") or [])
            self.fallback_label.setText(
                "Fallback: " + str(fallback.get("workflow") or "desktop").upper()
                + " — " + str(fallback.get("reason") or "")
                + (f" · {steps}" if steps else "")
            )
        embedded = payload.get("state") == EMBEDDED
        self.launch_button.setEnabled(not embedded)
        self.embed_button.setEnabled(
            not embedded and payload.get("state") in {RUNNING, DETACHED, EMBEDDING_UNSUPPORTED}
        )
        self.detach_button.setEnabled(embedded)
        self.focus_button.setEnabled(embedded)
        self.close_button.setEnabled(
            bool(payload.get("close_allowed")) and payload.get("state") != CLOSED
        )

    def _render_result(self, result: Mapping[str, Any]) -> None:
        stages = result.get("stages") or {}
        lines = [
            str(result.get("summary") or ""),
            "",
            "Launch facts (independent):",
            f"  requested={stages.get('launch_requested')}",
            f"  process_started={stages.get('process_started')}",
            f"  window_detected={stages.get('window_detected')}",
            f"  launch_verified={stages.get('launch_verified')}",
            "",
            "Embedding has not been attempted.",
        ]
        game = result.get("game") or {}
        launch = game.get("launch") or {}
        if launch:
            lines.append(
                f"Process: {launch.get('process_name')} (pid {launch.get('pid')}), "
                f"start {launch.get('start_time')}, window {launch.get('window')!r}"
            )
        self.details.setPlainText("\n".join(lines))

    # -- lifecycle ---------------------------------------------------------

    def shutdown(self) -> None:
        """Detach on dashboard exit; NEVER close a game the operator may own.

        ``cleanup()`` is idempotent, so a shutdown after an already-finished
        detach (or repeated by the dashboard) changes nothing and reports no
        restore it did not perform.
        """
        self._timer.stop()
        if self.host.state != CLOSED:
            # `cleanup()` is the one teardown door: it restores a live embedding
            # AND retries a DETACHED host whose last restore failed. It reports
            # `changed: false` when there was nothing to do.
            result = self.host.cleanup()
            self._remember_compatibility()
            if result.get("changed") or result.get("restored") is False:
                self._log(
                    "Dashboard closing: game window "
                    + ("restored" if result.get("restored") else "could not be restored")
                    + "; the game process was left running."
                )

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self.shutdown()
        super().closeEvent(event)
