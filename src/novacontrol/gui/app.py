"""PySide6 dashboard application."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from novacontrol.application import NovaControlApplication
from novacontrol.core.security import ApprovalDecision, ApprovalRequest
from novacontrol.explore import ExploreRequest
from novacontrol.gui.models import DashboardTab
from novacontrol.gui.viewmodel import DashboardViewModel
from novacontrol.memory import MemoryNamespace
from novacontrol.performance import LoadTester, MetricsRegistry, Profiler, TtlCache
from novacontrol.planning import WorkflowExecutor
from novacontrol.projects import TaskStatus


def create_gui_app() -> Any:
    """Create the PySide6 application and main window."""
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import (
            QApplication,
            QCheckBox,
            QComboBox,
            QFormLayout,
            QGridLayout,
            QGroupBox,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QMainWindow,
            QMessageBox,
            QPushButton,
            QSpinBox,
            QTabWidget,
            QTextEdit,
            QVBoxLayout,
            QWidget,
        )
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError("PySide6 is not installed. Run `pip install -e .` first.") from exc

    app = QApplication.instance() or QApplication([])
    window = _build_dashboard_window(
        Qt=Qt,
        QCheckBox=QCheckBox,
        QComboBox=QComboBox,
        QFont=QFont,
        QFormLayout=QFormLayout,
        QGridLayout=QGridLayout,
        QGroupBox=QGroupBox,
        QHBoxLayout=QHBoxLayout,
        QLabel=QLabel,
        QLineEdit=QLineEdit,
        QMainWindow=QMainWindow,
        QMessageBox=QMessageBox,
        QPushButton=QPushButton,
        QSpinBox=QSpinBox,
        QTabWidget=QTabWidget,
        QTextEdit=QTextEdit,
        QVBoxLayout=QVBoxLayout,
        QWidget=QWidget,
    )
    window.show()
    return app, window


def main() -> None:
    """Launch the dashboard."""
    app, _window = create_gui_app()
    app.exec()


def _build_dashboard_window(**qt: Any) -> Any:
    Qt = qt["Qt"]
    QCheckBox = qt["QCheckBox"]
    QComboBox = qt["QComboBox"]
    QFont = qt["QFont"]
    QFormLayout = qt["QFormLayout"]
    QGridLayout = qt["QGridLayout"]
    QGroupBox = qt["QGroupBox"]
    QHBoxLayout = qt["QHBoxLayout"]
    QLabel = qt["QLabel"]
    QLineEdit = qt["QLineEdit"]
    QMainWindow = qt["QMainWindow"]
    QMessageBox = qt["QMessageBox"]
    QPushButton = qt["QPushButton"]
    QSpinBox = qt["QSpinBox"]
    QTabWidget = qt["QTabWidget"]
    QTextEdit = qt["QTextEdit"]
    QVBoxLayout = qt["QVBoxLayout"]
    QWidget = qt["QWidget"]

    class DashboardWindow(QMainWindow):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__()
            self.view_model = DashboardViewModel.with_default_tabs()
            self.nova = NovaControlApplication(data_dir=Path("data"))
            approval_gateway = _GuiApprovalGateway(self, QMessageBox)
            self.nova.desktop.approval_gateway = approval_gateway
            self.nova.browser.approval_gateway = approval_gateway
            self.logs: list[str] = []
            self.setWindowTitle("NovaControl")
            self.resize(1280, 820)
            self.setMinimumSize(1040, 680)
            _run(self.nova.start())

            root = QWidget()
            layout = QVBoxLayout(root)
            layout.setContentsMargins(14, 14, 14, 14)
            layout.setSpacing(10)
            layout.addWidget(self._build_header())

            self.tabs = QTabWidget()
            self.tabs.addTab(self._build_dashboard_tab(), DashboardTab.DASHBOARD.label)
            self.tabs.addTab(self._build_chat_tab(), DashboardTab.CHAT.label)
            self.tabs.addTab(self._build_intelligence_tab(), DashboardTab.INTELLIGENCE.label)
            self.tabs.addTab(self._build_explore_tab(), DashboardTab.EXPLORE.label)
            self.tabs.addTab(self._build_demos_tab(), DashboardTab.DEMOS.label)
            self.tabs.addTab(self._build_automation_tab(), DashboardTab.AUTOMATION.label)
            self.tabs.addTab(self._build_tasks_tab(), DashboardTab.TASKS.label)
            self.tabs.addTab(self._build_memory_tab(), DashboardTab.MEMORY.label)
            self.tabs.addTab(self._build_projects_tab(), DashboardTab.PROJECTS.label)
            self.tabs.addTab(self._build_plugins_tab(), DashboardTab.PLUGINS.label)
            self.tabs.addTab(self._build_settings_tab(), DashboardTab.SETTINGS.label)
            self.tabs.addTab(self._build_logs_tab(), DashboardTab.LOGS.label)
            self.tabs.addTab(self._build_performance_tab(), DashboardTab.PERFORMANCE.label)
            layout.addWidget(self.tabs, stretch=1)

            self.setCentralWidget(root)
            self.refresh_status()

        def closeEvent(self, event: Any) -> None:
            _run(self.nova.stop())
            event.accept()

        def _build_header(self) -> Any:
            box = QGroupBox("NovaControl Dashboard")
            row = QHBoxLayout(box)
            title = QLabel("Local AI operating system")
            title_font = QFont()
            title_font.setPointSize(14)
            title_font.setBold(True)
            title.setFont(title_font)
            self.header_status = QLabel("Starting...")
            self.header_status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            refresh = QPushButton("Refresh")
            refresh.clicked.connect(self.refresh_status)
            row.addWidget(title)
            row.addWidget(self.header_status, stretch=1)
            row.addWidget(refresh)
            return box

        def _build_dashboard_tab(self) -> Any:
            widget = QWidget()
            layout = QVBoxLayout(widget)
            grid = QGridLayout()
            self.metric_runtime = QLabel()
            self.metric_modules = QLabel()
            self.metric_memory = QLabel()
            self.metric_projects = QLabel()
            self.metric_tasks = QLabel()
            self.metric_workflows = QLabel()
            metrics = (
                ("Runtime", self.metric_runtime),
                ("Modules", self.metric_modules),
                ("Memory", self.metric_memory),
                ("Projects", self.metric_projects),
                ("Tasks", self.metric_tasks),
                ("Workflows", self.metric_workflows),
            )
            for index, (label, value) in enumerate(metrics):
                card = QGroupBox(label)
                card_layout = QVBoxLayout(card)
                value.setTextInteractionFlags(Qt.TextSelectableByMouse)
                value_font = QFont()
                value_font.setPointSize(12)
                value_font.setBold(True)
                value.setFont(value_font)
                card_layout.addWidget(value)
                grid.addWidget(card, index // 3, index % 3)
            layout.addLayout(grid)

            quick = QGroupBox("Quick Start")
            quick_layout = QVBoxLayout(quick)
            self.quick_output = QTextEdit()
            self.quick_output.setReadOnly(True)
            buttons = QHBoxLayout()
            ask_demo = QPushButton("Ask Demo")
            plan_demo = QPushButton("Plan Demo")
            improve_demo = QPushButton("Improve Plan")
            phase_demo = QPushButton("Release Check")
            test_demo = QPushButton("Run Tests")
            ask_demo.clicked.connect(lambda: self._ask_into(self.quick_output, "create tests for the scheduler"))
            plan_demo.clicked.connect(lambda: self._ask_into(self.quick_output, "build a plan for NovaControl next improvements"))
            improve_demo.clicked.connect(lambda: self._ask_into(self.quick_output, "make it intelligent so it can code itself and improve"))
            phase_demo.clicked.connect(self._run_release_check)
            test_demo.clicked.connect(lambda: self._run_cli_command(["-m", "unittest", "discover", "-s", "tests"], self.quick_output))
            buttons.addWidget(ask_demo)
            buttons.addWidget(plan_demo)
            buttons.addWidget(improve_demo)
            buttons.addWidget(phase_demo)
            buttons.addWidget(test_demo)
            buttons.addStretch()
            quick_layout.addLayout(buttons)
            quick_layout.addWidget(self.quick_output)
            layout.addWidget(quick, stretch=1)
            return widget

        def _build_chat_tab(self) -> Any:
            widget = QWidget()
            layout = QVBoxLayout(widget)
            row = QHBoxLayout()
            self.chat_input = QLineEdit()
            self.chat_input.setPlaceholderText("Ask NovaControl")
            ask = QPushButton("Ask")
            ask.clicked.connect(lambda: self._ask_into(self.chat_output, self.chat_input.text()))
            row.addWidget(self.chat_input)
            row.addWidget(ask)
            self.chat_output = QTextEdit()
            self.chat_output.setReadOnly(True)
            layout.addLayout(row)
            layout.addWidget(self.chat_output)
            return widget

        def _build_intelligence_tab(self) -> Any:
            widget = QWidget()
            layout = QVBoxLayout(widget)
            row = QHBoxLayout()
            self.improvement_goal = QLineEdit()
            self.improvement_goal.setPlaceholderText("Improvement goal")
            improve = QPushButton("Plan Improvement")
            improve.clicked.connect(self._run_improvement_plan)
            health = QPushButton("Health Check")
            health.clicked.connect(self._run_health_check)
            row.addWidget(self.improvement_goal)
            row.addWidget(improve)
            row.addWidget(health)
            self.intelligence_output = QTextEdit()
            self.intelligence_output.setReadOnly(True)
            layout.addLayout(row)
            layout.addWidget(self.intelligence_output)
            return widget

        def _build_explore_tab(self) -> Any:
            widget = QWidget()
            layout = QVBoxLayout(widget)
            form = QFormLayout()
            self.explore_topic = QLineEdit()
            self.explore_topic.setPlaceholderText("Topic to research")
            self.explore_depth = QComboBox()
            self.explore_depth.addItems(["deep", "simple"])
            self.explore_sources = QSpinBox()
            self.explore_sources.setRange(1, 10)
            self.explore_sources.setValue(5)
            self.explore_videos = QSpinBox()
            self.explore_videos.setRange(0, 8)
            self.explore_videos.setValue(4)
            self.explore_include_videos = QCheckBox("Include related videos")
            self.explore_include_videos.setChecked(True)
            form.addRow("Topic", self.explore_topic)
            form.addRow("Depth", self.explore_depth)
            form.addRow("Sources", self.explore_sources)
            form.addRow("Videos", self.explore_videos)
            form.addRow("", self.explore_include_videos)
            run = QPushButton("Explore")
            run.clicked.connect(self._run_explore)
            self.explore_output = QTextEdit()
            self.explore_output.setReadOnly(True)
            layout.addLayout(form)
            layout.addWidget(run)
            layout.addWidget(self.explore_output, stretch=1)
            return widget

        def _build_demos_tab(self) -> Any:
            widget = QWidget()
            layout = QVBoxLayout(widget)
            row = QHBoxLayout()
            self.demo_phase = QComboBox()
            self.demo_phase.addItems(
                [
                    "all",
                    "phase1",
                    "phase2",
                    "phase3",
                    "phase4",
                    "phase5",
                    "phase6",
                    "phase7",
                    "phase8",
                    "phase9",
                    "phase10",
                    "phase11",
                    "phase12",
                    "phase13",
                    "phase14",
                    "phase15",
                    "explore",
                ]
            )
            run = QPushButton("Run Demo")
            run.clicked.connect(self._run_selected_demo)
            tests = QPushButton("Run Tests")
            tests.clicked.connect(lambda: self._run_cli_command(["-m", "unittest", "discover", "-s", "tests"], self.demo_output))
            row.addWidget(self.demo_phase)
            row.addWidget(run)
            row.addWidget(tests)
            row.addStretch()
            self.demo_output = QTextEdit()
            self.demo_output.setReadOnly(True)
            layout.addLayout(row)
            layout.addWidget(self.demo_output)
            return widget

        def _build_automation_tab(self) -> Any:
            widget = QWidget()
            layout = QVBoxLayout(widget)

            desktop_box = QGroupBox("Desktop Workflow")
            desktop_layout = QVBoxLayout(desktop_box)
            desktop_row = QHBoxLayout()
            self.desktop_app = QLineEdit()
            self.desktop_app.setPlaceholderText("Application name, for example Code")
            plan_desktop = QPushButton("Plan Open App")
            execute_desktop = QPushButton("Try Execute")
            plan_desktop.clicked.connect(self._plan_desktop)
            execute_desktop.clicked.connect(self._execute_desktop)
            desktop_row.addWidget(self.desktop_app)
            desktop_row.addWidget(plan_desktop)
            desktop_row.addWidget(execute_desktop)
            desktop_layout.addLayout(desktop_row)

            browser_box = QGroupBox("Browser Workflow")
            browser_layout = QVBoxLayout(browser_box)
            browser_row = QHBoxLayout()
            self.browser_url = QLineEdit()
            self.browser_url.setPlaceholderText("https://example.com")
            plan_browser = QPushButton("Plan Navigate")
            execute_browser = QPushButton("Try Execute")
            plan_browser.clicked.connect(self._plan_browser)
            execute_browser.clicked.connect(self._execute_browser)
            browser_row.addWidget(self.browser_url)
            browser_row.addWidget(plan_browser)
            browser_row.addWidget(execute_browser)
            browser_layout.addLayout(browser_row)

            self.automation_output = QTextEdit()
            self.automation_output.setReadOnly(True)
            layout.addWidget(desktop_box)
            layout.addWidget(browser_box)
            layout.addWidget(self.automation_output, stretch=1)
            return widget

        def _build_tasks_tab(self) -> Any:
            widget = QWidget()
            layout = QVBoxLayout(widget)
            row = QHBoxLayout()
            self.plan_input = QLineEdit()
            self.plan_input.setPlaceholderText("Goal to plan")
            create = QPushButton("Create Plan")
            create.clicked.connect(self._create_plan)
            tracked = QPushButton("Show Tracked Tasks")
            tracked.clicked.connect(self._show_tracked_tasks)
            row.addWidget(self.plan_input)
            row.addWidget(create)
            row.addWidget(tracked)
            self.plan_output = QTextEdit()
            self.plan_output.setReadOnly(True)
            layout.addLayout(row)
            layout.addWidget(self.plan_output)
            return widget

        def _build_memory_tab(self) -> Any:
            widget = QWidget()
            layout = QVBoxLayout(widget)
            row = QHBoxLayout()
            self.memory_query = QLineEdit()
            self.memory_query.setPlaceholderText("Search conversation memory")
            search = QPushButton("Search")
            search.clicked.connect(self._search_memory)
            row.addWidget(self.memory_query)
            row.addWidget(search)
            self.memory_output = QTextEdit()
            self.memory_output.setReadOnly(True)
            layout.addLayout(row)
            layout.addWidget(self.memory_output)
            return widget

        def _build_projects_tab(self) -> Any:
            widget = QWidget()
            layout = QVBoxLayout(widget)
            form = QFormLayout()
            self.project_name = QLineEdit()
            self.project_name.setPlaceholderText("Project name")
            self.project_task = QLineEdit()
            self.project_task.setPlaceholderText("First task")
            form.addRow("Project", self.project_name)
            form.addRow("Task", self.project_task)
            create = QPushButton("Create Project")
            create.clicked.connect(self._create_project)
            self.project_output = QTextEdit()
            self.project_output.setReadOnly(True)
            layout.addLayout(form)
            layout.addWidget(create)
            layout.addWidget(self.project_output)
            return widget

        def _build_plugins_tab(self) -> Any:
            widget = QWidget()
            layout = QVBoxLayout(widget)
            output = QTextEdit()
            output.setReadOnly(True)
            output.setPlainText(
                "Plugin marketplace is active.\n\n"
                "Safe plugins can be enabled.\n"
                "Plugins requesting permissions are denied until approval is connected."
            )
            layout.addWidget(output)
            return widget

        def _build_settings_tab(self) -> Any:
            widget = QWidget()
            layout = QVBoxLayout(widget)
            output = QTextEdit()
            output.setReadOnly(True)
            output.setPlainText(
                "Safety defaults:\n"
                "- Desktop execution requires approval\n"
                "- Browser navigation/form/download actions require approval\n"
                "- Sensitive tools require approval\n"
                "- Sensitive plugins require approval\n\n"
                "API token env var: NOVACONTROL_API_TOKEN"
            )
            layout.addWidget(output)
            return widget

        def _build_logs_tab(self) -> Any:
            widget = QWidget()
            layout = QVBoxLayout(widget)
            self.logs_output = QTextEdit()
            self.logs_output.setReadOnly(True)
            layout.addWidget(self.logs_output)
            return widget

        def _build_performance_tab(self) -> Any:
            widget = QWidget()
            layout = QVBoxLayout(widget)
            run = QPushButton("Run Local Performance Check")
            run.clicked.connect(self._run_performance_check)
            self.performance_output = QTextEdit()
            self.performance_output.setReadOnly(True)
            layout.addWidget(run)
            layout.addWidget(self.performance_output)
            return widget

        def refresh_status(self) -> None:
            status = self.nova.status()
            self.header_status.setText("Runtime: running" if status["runtime_started"] else "Runtime: stopped")
            self.metric_runtime.setText("Running" if status["runtime_started"] else "Stopped")
            self.metric_modules.setText(str(len(status["modules"])))
            self.metric_memory.setText("Conversation store ready")
            self.metric_projects.setText(str(status["projects"]))
            self.metric_tasks.setText(f"{status['tracked_tasks']} tracked / {status['scheduled_tasks']} scheduled")
            self.metric_workflows.setText(str(status["automation_workflows"]))
            self._log("Status refreshed")

        def _ask_into(self, output: Any, request: str) -> None:
            request = request.strip()
            if not request:
                return
            output.setPlainText("Working...")
            response = _run(self.nova.handle_request(request))
            output.setPlainText(_format_json(response.to_dict()))
            self.refresh_status()
            self._log(f"Asked: {request}")

        def _run_explore(self) -> None:
            topic = self.explore_topic.text().strip()
            if not topic:
                return
            self.explore_output.setPlainText("Researching...")
            report = _run(
                self.nova.explore.research(
                    ExploreRequest(
                        topic,
                        depth=self.explore_depth.currentText(),
                        include_videos=self.explore_include_videos.isChecked(),
                        max_sources=self.explore_sources.value(),
                        max_videos=self.explore_videos.value(),
                    )
                )
            )
            self.explore_output.setPlainText(_format_report(report.to_dict()))
            self._log(f"Explored: {topic}")

        def _create_plan(self) -> None:
            goal = self.plan_input.text().strip()
            if not goal:
                return
            plan = self.nova.planning.create_plan(goal)
            payload: dict[str, Any] = {"plan": plan.to_dict()}
            if not plan.needs_clarification:
                payload["workflow"] = _run(WorkflowExecutor().execute(plan)).to_dict()
            self.plan_output.setPlainText(_format_json(payload))
            self._log(f"Planned: {goal}")

        def _run_improvement_plan(self) -> None:
            goal = self.improvement_goal.text().strip() or "make NovaControl code itself and improve"
            plan = self.nova.self_improvement.plan(goal)
            self.intelligence_output.setPlainText(_format_json(plan.to_dict()))
            self._log(f"Improvement planned: {goal}")

        def _run_health_check(self) -> None:
            from novacontrol.release import SystemHealthMonitor

            report = SystemHealthMonitor(Path.cwd()).run(self.nova.status())
            self.intelligence_output.setPlainText(_format_json(report.to_dict()))
            self._log("Health check displayed")

        def _show_tracked_tasks(self) -> None:
            self.plan_output.setPlainText(
                _format_json({"tasks": [task.to_dict() for task in self.nova.tasks.list()]})
            )
            self._log("Displayed tracked tasks")

        def _plan_desktop(self) -> None:
            app_name = self.desktop_app.text().strip() or "Code"
            workflow = self.nova.desktop.plan_open_application(app_name)
            self.automation_output.setPlainText(_format_json({"workflow": workflow.to_dict()}))
            self._log(f"Planned desktop workflow: {app_name}")

        def _execute_desktop(self) -> None:
            app_name = self.desktop_app.text().strip() or "Code"
            workflow = self.nova.desktop.plan_open_application(app_name)
            results = _run(self.nova.desktop.execute_workflow(workflow))
            self.automation_output.setPlainText(
                _format_json(
                    {
                        "workflow": workflow.to_dict(),
                        "results": [result.to_dict() for result in results],
                    }
                )
            )
            self._log(f"Tried desktop workflow: {app_name}")

        def _plan_browser(self) -> None:
            url = self.browser_url.text().strip() or "https://example.com"
            workflow = self.nova.browser.plan_navigation(url)
            self.automation_output.setPlainText(_format_json({"workflow": workflow.to_dict()}))
            self._log(f"Planned browser workflow: {url}")

        def _execute_browser(self) -> None:
            url = self.browser_url.text().strip() or "https://example.com"
            workflow = self.nova.browser.plan_navigation(url)
            results = _run(self.nova.browser.execute_workflow(workflow))
            self.automation_output.setPlainText(
                _format_json(
                    {
                        "workflow": workflow.to_dict(),
                        "results": [result.to_dict() for result in results],
                    }
                )
            )
            self._log(f"Tried browser workflow: {url}")

        def _search_memory(self) -> None:
            query = self.memory_query.text().strip()
            results = _run(self.nova.memory.retrieve(MemoryNamespace.CONVERSATION, query, limit=20))
            payload = [result.record.to_dict() | {"score": result.score} for result in results]
            self.memory_output.setPlainText(_format_json({"results": payload}))
            self._log(f"Searched memory: {query}")

        def _create_project(self) -> None:
            name = self.project_name.text().strip()
            if not name:
                return
            project = self.nova.projects.create_project(name)
            task_title = self.project_task.text().strip()
            if task_title:
                project = self.nova.projects.add_task(project.id, task_title)
                project = self.nova.projects.set_task_status(project.id, project.tasks[0].id, TaskStatus.IN_PROGRESS)
            self.project_output.setPlainText(_format_json(project.to_dict()))
            self.refresh_status()
            self._log(f"Created project: {name}")

        def _run_performance_check(self) -> None:
            metrics = MetricsRegistry()
            cache: TtlCache[str] = TtlCache()
            cache.set("dashboard", "ready", ttl_seconds=60)
            metrics.increment("dashboard.checks")
            metrics.gauge("cache.size", cache.size())

            async def operation() -> str:
                return cache.get("dashboard") or "missing"

            profile = _run(Profiler().measure_async("dashboard-cache-read", operation))
            load = _run(LoadTester().run(operation, requests=8, concurrency=2))
            metrics.timing("dashboard.cache.seconds", profile.duration_seconds)
            self.performance_output.setPlainText(
                _format_json(
                    {
                        "profile": profile.to_dict(),
                        "load": load.to_dict(),
                        "metrics": metrics.snapshot(),
                    }
                )
            )
            self._log("Performance check completed")

        def _run_release_check(self) -> None:
            from pathlib import Path

            from novacontrol.release import ReleaseReadinessChecker

            report = ReleaseReadinessChecker(Path.cwd()).check()
            self.quick_output.setPlainText(_format_json(report.to_dict()))
            self._log("Release check completed")

        def _run_selected_demo(self) -> None:
            phase = self.demo_phase.currentText()
            self._run_cli_command(["-m", "novacontrol", "demo", phase], self.demo_output)

        def _run_cli_command(self, args: list[str], output: Any) -> None:
            output.setPlainText("Running...")
            env = dict(os.environ)
            src = os.path.abspath("src")
            env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [sys.executable, *args],
                cwd=os.getcwd(),
                env=env,
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            text = completed.stdout
            if completed.stderr:
                text += "\nSTDERR:\n" + completed.stderr
            text += f"\nExit code: {completed.returncode}"
            output.setPlainText(text)
            self._log("Command completed: " + " ".join(args))

        def _log(self, message: str) -> None:
            self.logs.append(message)
            self.logs_output.setPlainText("\n".join(self.logs[-200:]) if hasattr(self, "logs_output") else "")

    return DashboardWindow()


class _GuiApprovalGateway:
    def __init__(self, parent: Any, message_box_cls: Any) -> None:
        self.parent = parent
        self.message_box_cls = message_box_cls

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        permissions = ", ".join(permission.value for permission in request.permissions)
        result = self.message_box_cls.question(
            self.parent,
            "Approve NovaControl Action",
            f"{request.action}\n\nPermissions: {permissions}\n\nAllow this action?",
            self.message_box_cls.Yes | self.message_box_cls.No,
            self.message_box_cls.No,
        )
        approved = result == self.message_box_cls.Yes
        return ApprovalDecision(
            request_id=request.id,
            approved=approved,
            decided_by="gui.user",
            reason="Approved in dashboard" if approved else "Denied in dashboard",
        )


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def _format_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def _format_report(report: dict[str, Any]) -> str:
    lines = [
        str(report["topic"]),
        "",
        str(report["overview"]),
        "",
        "Key points:",
        *[f"- {point}" for point in report.get("key_points", ())],
        "",
        str(report["detailed_explanation"]),
        "",
        "Sources:",
        *[f"- {source['title']}: {source['url']}" for source in report.get("sources", ())],
        "",
        "Videos:",
        *[f"- {video['title']}: {video['url']}" for video in report.get("videos", ())],
    ]
    return "\n".join(lines)
