"""Optional native participant viewer; imported only by ``ateable-node ui``."""

from __future__ import annotations

import argparse
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from .branding import app_icon, logo_pixmap
from .participant import (
    ParticipantClient,
    ParticipantError,
    _read_session,
    default_credentials_file,
    default_session_file,
    load_identity,
)

try:
    from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
    from PySide6.QtWidgets import (
        QApplication,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFileDialog,
        QFormLayout,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QTableWidget,
        QTableWidgetItem,
        QTabWidget,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )

    PYSIDE_AVAILABLE = True
except ImportError:
    PYSIDE_AVAILABLE = False


if PYSIDE_AVAILABLE:

    class _Signals(QObject):
        success = Signal(object)
        failure = Signal(str)
        finished = Signal()

    class _Task(QRunnable):
        def __init__(self, operation):
            super().__init__()
            self.operation = operation
            self.signals = _Signals()

        def run(self):
            try:
                result = self.operation()
                with suppress(RuntimeError):
                    self.signals.success.emit(result)
            except ParticipantError as exc:
                with suppress(RuntimeError):
                    self.signals.failure.emit(str(exc))
            except Exception:  # noqa: BLE001 - never expose HTTP/session data from unexpected failures
                with suppress(RuntimeError):
                    self.signals.failure.emit("Participant request failed")
            finally:
                with suppress(RuntimeError):
                    self.signals.finished.emit()

    class ParticipantWindow(QMainWindow):
        def __init__(self, client: ParticipantClient, *, auto_refresh: bool = True):
            super().__init__()
            self.client = client
            self.workspace_id: str | None = None
            self._generation = 0
            self._latest: dict[str, int] = {}
            self._busy: dict[str, int] = {}
            self._tasks: dict[str, _Task] = {}
            self._cursors: dict[str, int] = {}
            self._messages: dict[str, list[dict[str, Any]]] = {}
            self._pending_writes: dict[str, tuple[tuple[Any, ...], str]] = {}
            self._closed = False
            self._connection_checked = False
            self._connected = False
            self._members_loaded = False
            self._worker_count = 0
            self._online_worker_count = 0
            self._participant_names: dict[str, str] = {}
            self._file_offset = 0
            self._job_offset = 0
            self.page_size = 200
            self.destroyed.connect(self._mark_closed)
            self.thread_pool = QThreadPool(self)
            self.thread_pool.setMaxThreadCount(4)
            self.setWindowTitle("Ateable Node")
            self.setWindowIcon(app_icon())
            self.resize(1140, 760)
            self.setMinimumSize(800, 540)
            self.setStyleSheet("""
                QMainWindow, QWidget { background:#000000; color:#f1f1f1; font-size:13px; }
                QLineEdit, QTextEdit, QTableWidget, QComboBox { background:#000000; border:1px solid #424242; border-radius:5px; padding:6px; }
                QPushButton { background:#000000; border:1px solid #646464; border-radius:5px; padding:7px 12px; }
                QPushButton:hover { background:#383838; }
                QPushButton:disabled { color:#777; border-color:#333; }
                QHeaderView::section { background:#000000; padding:7px; border:0; }
                QTabBar::tab { background:#000000; padding:10px 20px; }
                QTabBar::tab:selected { background:#000000; border-bottom:2px solid white; }
                QTabBar::tab:hover:!selected { background:#282828; }
                QTabWidget::pane { background:#000000; border:1px solid #424242; }
                QAbstractItemView { background:#000000; alternate-background-color:#000000;
                    selection-background-color:#383838; selection-color:white; }
                QTableCornerButton::section { background:#000000; border:0; }
            """)
            body = QWidget()
            layout = QVBoxLayout(body)
            header = QHBoxLayout()
            logo = QLabel()
            logo.setPixmap(logo_pixmap(64))
            logo.setFixedSize(64, 64)
            logo.setAccessibleName("Ateable logo")
            header.addWidget(logo)
            title = QLabel("Ateable Node")
            title.setStyleSheet("font-size:24px;font-weight:600;")
            title_group = QVBoxLayout()
            title_group.addWidget(title)
            subtitle = QLabel(
                "Workspace data collection: files, batch requests, and chat"
            )
            subtitle.setStyleSheet("color:#bdbdbd;")
            title_group.addWidget(subtitle)
            header.addLayout(title_group)
            header.addStretch()
            self.workspace_selector = QComboBox()
            self.workspace_selector.setAccessibleName("Assigned workspace")
            self.workspace_selector.setMinimumWidth(250)
            self.workspace_selector.currentIndexChanged.connect(self._workspace_changed)
            header.addWidget(self.workspace_selector)
            refresh = QPushButton("Refresh")
            refresh.clicked.connect(self.refresh_all)
            header.addWidget(refresh)
            layout.addLayout(header)
            self.identity_label = QLabel(
                "Access file loaded · connection not checked yet"
            )
            self.identity_label.setTextFormat(Qt.TextFormat.PlainText)
            self.identity_label.setToolTip(
                f"Participant ID: {client.identity.actor_id}"
            )
            layout.addWidget(self.identity_label)
            self.next_step_label = QLabel(
                "Start here: select Refresh to connect and load your assigned workspaces."
            )
            self.next_step_label.setWordWrap(True)
            self.next_step_label.setStyleSheet(
                "background:#000000; border:1px solid #4c4c4c; border-radius:6px; padding:10px;"
            )
            layout.addWidget(self.next_step_label)
            self.tabs = QTabWidget()
            layout.addWidget(self.tabs)
            self._build_workspaces()
            self._build_files()
            self._build_jobs()
            self._build_speaking()
            self.status_label = QLabel("Ready to connect")
            self.status_label.setTextFormat(Qt.TextFormat.PlainText)
            self.status_label.setWordWrap(True)
            layout.addWidget(self.status_label)
            self.setCentralWidget(body)
            self.poll_timer = QTimer(self)
            self.poll_timer.setInterval(2500)
            self.poll_timer.timeout.connect(self.poll)
            self.filter_timer = QTimer(self)
            self.filter_timer.setSingleShot(True)
            self.filter_timer.setInterval(300)
            self.filter_timer.timeout.connect(lambda: self.refresh_files(force=True))
            self._render_messages()
            if auto_refresh:
                self.poll_timer.start()
                self.refresh_workspaces()

        @staticmethod
        def _table(headers: list[str]) -> QTableWidget:
            table = QTableWidget(0, len(headers))
            table.setHorizontalHeaderLabels(headers)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            table.horizontalHeader().setSectionResizeMode(
                QHeaderView.ResizeMode.ResizeToContents
            )
            table.horizontalHeader().setStretchLastSection(True)
            table.verticalHeader().hide()
            return table

        def _build_workspaces(self):
            page = QWidget()
            layout = QVBoxLayout(page)
            self.workspace_hint = QLabel(
                "Choose a workspace above or select a row below. Only assigned workspaces appear here."
            )
            self.workspace_hint.setWordWrap(True)
            layout.addWidget(self.workspace_hint)
            self.workspaces_table = self._table(["Workspace", "ID"])
            self.workspaces_table.setColumnHidden(1, True)
            self.workspaces_table.cellClicked.connect(self._workspace_row_clicked)
            layout.addWidget(self.workspaces_table)
            self.roster_hint = QLabel(
                "Choose a workspace to see its people, AI participants, and download workers."
            )
            self.roster_hint.setWordWrap(True)
            layout.addWidget(self.roster_hint)
            self.members_table = self._table(["Member", "Role", "Status", "ID"])
            self.members_table.setColumnHidden(3, True)
            layout.addWidget(self.members_table)
            self.tabs.addTab(page, "Workspaces")

        def _build_files(self):
            page = QWidget()
            layout = QVBoxLayout(page)
            self.file_query = QLineEdit()
            self.file_query.setPlaceholderText("Search the workspace file catalog")
            self.file_query.setAccessibleName("Workspace file search")
            self.file_query.textChanged.connect(self._file_filter_changed)
            layout.addWidget(self.file_query)
            layout.addWidget(
                QLabel(
                    "Files stay on the download workers. Copy a path to locate a file on the named worker."
                )
            )
            self.files_summary = QLabel("Choose a workspace to view its files")
            layout.addWidget(self.files_summary)
            self.files_hint = QLabel(
                "Completed downloads from workspace workers appear here automatically."
            )
            self.files_hint.setWordWrap(True)
            layout.addWidget(self.files_hint)
            self.files_table = self._table(
                ["Worker", "Path on worker", "Size", "Availability", "Downloaded"]
            )
            layout.addWidget(self.files_table)
            pager = QHBoxLayout()
            self.files_previous = QPushButton("Previous")
            self.files_next = QPushButton("Next")
            self.files_previous.setEnabled(False)
            self.files_next.setEnabled(False)
            self.files_previous.clicked.connect(lambda: self._move_file_page(-1))
            self.files_next.clicked.connect(lambda: self._move_file_page(1))
            pager.addWidget(self.files_previous)
            pager.addWidget(self.files_next)
            pager.addStretch()
            layout.addLayout(pager)
            copy = QPushButton("Copy selected path")
            copy.clicked.connect(self.copy_file_path)
            layout.addWidget(copy)
            self.tabs.addTab(page, "Files")

        def _build_jobs(self):
            page = QWidget()
            layout = QVBoxLayout(page)
            heading = QLabel("Request a download")
            heading.setStyleSheet("font-size:18px;font-weight:600;")
            layout.addWidget(heading)
            instructions = QLabel(
                "Paste file links, supported media pages, or S3 object / prefix links, one per line. "
                "Your chosen workspace worker downloads and stores the files."
            )
            instructions.setWordWrap(True)
            layout.addWidget(instructions)
            self.url_input = QTextEdit()
            self.url_input.setAccessibleName("Remote download URLs")
            self.url_input.setPlaceholderText(
                "https://example.org/dataset.zip\ns3://my-bucket/folder/"
            )
            self.url_input.setMaximumHeight(105)
            self.url_input.textChanged.connect(self._update_send_controls)
            layout.addWidget(self.url_input)
            form = QFormLayout()
            self.download_target = QComboBox()
            self.download_target.addItem("Choose automatically", "auto")
            form.addRow("Download on", self.download_target)
            self.output_subdir = QLineEdit()
            self.output_subdir.setPlaceholderText(
                "Optional, for example datasets/october"
            )
            form.addRow("Folder on worker", self.output_subdir)
            layout.addLayout(form)
            self.download_button = QPushButton("Request a download")
            self.download_button.setEnabled(False)
            self.download_button.clicked.connect(self.request_download)
            layout.addWidget(self.download_button)
            self.jobs_hint = QLabel(
                "Your workspace download history appears below after a request."
            )
            self.jobs_hint.setWordWrap(True)
            layout.addWidget(self.jobs_hint)
            self.jobs_table = self._table(
                ["Title or source", "Status", "Stage", "Progress", "Worker"]
            )
            layout.addWidget(self.jobs_table)
            pager = QHBoxLayout()
            self.jobs_previous = QPushButton("Previous")
            self.jobs_next = QPushButton("Next")
            self.jobs_previous.setEnabled(False)
            self.jobs_next.setEnabled(False)
            self.jobs_previous.clicked.connect(lambda: self._move_job_page(-1))
            self.jobs_next.clicked.connect(lambda: self._move_job_page(1))
            self.jobs_summary = QLabel("Choose a workspace to view download history")
            pager.addWidget(self.jobs_previous)
            pager.addWidget(self.jobs_next)
            pager.addWidget(self.jobs_summary)
            pager.addStretch()
            layout.addLayout(pager)
            self.tabs.addTab(page, "Downloads")

        def _build_speaking(self):
            page = QWidget()
            layout = QVBoxLayout(page)
            heading = QLabel("Workspace chat · Speaking")
            heading.setStyleSheet("font-size:18px;font-weight:600;")
            layout.addWidget(heading)
            self.speaking_hint = QLabel(
                "Send to the workspace or one participant. Directed messages are visible to the sender, recipient, and Admin."
            )
            self.speaking_hint.setWordWrap(True)
            layout.addWidget(self.speaking_hint)
            self.speaking_log = QTextEdit()
            self.speaking_log.setReadOnly(True)
            self.speaking_log.setAccessibleName("Workspace speaking history")
            layout.addWidget(self.speaking_log)
            self.recipient = QComboBox()
            self.recipient.addItem("Everyone in this workspace", None)
            recipient_form = QFormLayout()
            recipient_form.addRow("Send to", self.recipient)
            layout.addLayout(recipient_form)
            self.message_input = QTextEdit()
            self.message_input.setMaximumHeight(95)
            self.message_input.setPlaceholderText("Write a message to this workspace")
            self.message_input.setAccessibleName("Workspace message")
            self.message_input.textChanged.connect(self._update_send_controls)
            layout.addWidget(self.message_input)
            self.send_button = QPushButton("Send message")
            self.send_button.setEnabled(False)
            self.send_button.clicked.connect(self.send_message)
            layout.addWidget(self.send_button)
            self.tabs.addTab(page, "Speaking")

        def _run_async(self, operation, success, *, section: str, scoped: bool = True):
            generation = self._generation
            revision = self._latest.get(section, 0) + 1
            self._latest[section] = revision
            self._busy[section] = revision
            task_id = str(uuid4())
            task = _Task(operation)
            self._tasks[task_id] = task

            def current() -> bool:
                return (
                    not self._closed
                    and (not scoped or generation == self._generation)
                    and self._latest.get(section) == revision
                )

            def done(value):
                if current():
                    success(value)

            def failed(message):
                if current():
                    self.status_label.setText(message)
                    if section in {
                        "workspaces",
                        "members",
                        "files",
                        "jobs",
                        "messages",
                    }:
                        self._connection_checked = True
                        self._connected = False
                        self._update_guidance()
                    self._update_send_controls()

            def finished():
                self._tasks.pop(task_id, None)
                if self._busy.get(section) == revision:
                    self._busy.pop(section, None)
                if not self._closed:
                    self._update_send_controls()

            task.signals.success.connect(done)
            task.signals.failure.connect(failed)
            task.signals.finished.connect(finished)
            self.thread_pool.start(task)

        @staticmethod
        def _fill(table, rows):
            table.setRowCount(len(rows))
            for index, row in enumerate(rows):
                for column, value in enumerate(row):
                    table.setItem(
                        index,
                        column,
                        QTableWidgetItem("" if value is None else str(value)),
                    )

        def refresh_workspaces(self):
            if "workspaces" in self._busy:
                return
            if not self._connection_checked:
                self.next_step_label.setText(
                    "Connecting to your workspace admin… Loading your assigned workspaces."
                )
                self.status_label.setText("Connecting…")
            self._run_async(
                self.client.workspaces,
                self._set_workspaces,
                section="workspaces",
                scoped=False,
            )

        def _set_workspaces(self, response):
            self._connection_checked = True
            self._connected = True
            items = response.get("items", [])
            previous = self.workspace_id or self.client.identity.workspace_id
            self.workspace_selector.blockSignals(True)
            self.workspace_selector.clear()
            for workspace in items:
                self.workspace_selector.addItem(
                    workspace.get("name") or workspace["id"], workspace["id"]
                )
            index = self.workspace_selector.findData(previous)
            self.workspace_selector.setCurrentIndex(
                index if index >= 0 else (0 if items else -1)
            )
            self.workspace_selector.blockSignals(False)
            self._fill(
                self.workspaces_table,
                [[item.get("name"), item["id"]] for item in items],
            )
            self._workspace_changed()
            actor = response.get("actor", {})
            role = (
                "Download worker"
                if actor.get("type", self.client.identity.actor_type) == "worker"
                else "AI participant"
            )
            self.identity_label.setText(f"{actor.get('name') or role} · {role}")
            self.identity_label.setToolTip(
                f"Participant ID: {actor.get('id') or self.client.identity.actor_id}"
            )
            for row, workspace in enumerate(items):
                self.workspaces_table.item(row, 0).setToolTip(
                    f"Workspace ID: {workspace['id']}"
                )
            self.workspace_hint.setText(
                "Choose a workspace above or select a row below. Only assigned workspaces appear here."
                if items
                else "No workspaces are assigned yet. Ask your workspace admin to add this worker or issue an AI access file."
            )
            self.status_label.setText(f"Connected · {len(items)} assigned workspace(s)")
            self._update_guidance()

        def _workspace_row_clicked(self, row, _column):
            item = self.workspaces_table.item(row, 1)
            if item:
                self.workspace_selector.setCurrentIndex(
                    self.workspace_selector.findData(item.text())
                )

        def _update_guidance(self):
            if not self._connected:
                if self._connection_checked:
                    text = (
                        "Could not refresh your workspace. Check the controller connection or access file, "
                        "then select Refresh. Your drafts are kept."
                    )
                else:
                    text = "Start here: select Refresh to connect and load your assigned workspaces."
            elif not self.workspace_id:
                text = (
                    "Next step: ask your workspace admin to assign a workspace. "
                    "This app will show it here; select Refresh after the admin adds you."
                )
            elif not self._members_loaded:
                text = "Loading this workspace… Its files, downloads, and chat will appear in the tabs below."
            elif not self._worker_count:
                text = (
                    "This workspace is ready for chat. Ask its admin to add a download worker "
                    "before requesting files."
                )
            elif not self._online_worker_count:
                text = (
                    "Workspace chat is available. No download workers are online. "
                    "Ask your admin to reconnect a worker before requesting a download."
                )
            else:
                text = (
                    "Ready. Open Downloads to paste links, Files to locate completed downloads, "
                    "or Speaking to chat with this workspace."
                )
            self.next_step_label.setText(text)

        def _workspace_changed(self, *_args):
            workspace_id = self.workspace_selector.currentData()
            if workspace_id == self.workspace_id:
                self._update_guidance()
                return
            self._generation += 1
            self.workspace_id = workspace_id
            self._file_offset = 0
            self._job_offset = 0
            self._busy.clear()
            self._members_loaded = False
            self._worker_count = 0
            self._online_worker_count = 0
            self._participant_names = {}
            self.message_input.clear()
            self.url_input.clear()
            self.output_subdir.clear()
            self.files_table.setRowCount(0)
            self.jobs_table.setRowCount(0)
            self.members_table.setRowCount(0)
            self.files_previous.setEnabled(False)
            self.files_next.setEnabled(False)
            self.jobs_previous.setEnabled(False)
            self.jobs_next.setEnabled(False)
            self.files_summary.setText(
                "Loading files…"
                if workspace_id
                else "Choose a workspace to view its files"
            )
            self.jobs_summary.setText(
                "Loading download history…"
                if workspace_id
                else "Choose a workspace to view download history"
            )
            self.files_hint.setText(
                "Loading the shared file list…"
                if workspace_id
                else "Completed downloads from workspace workers appear here automatically."
            )
            self.jobs_hint.setText(
                "Loading download history…"
                if workspace_id
                else "Choose an assigned workspace before requesting a download."
            )
            self.roster_hint.setText(
                "Loading workspace participants…"
                if workspace_id
                else "Ask your workspace admin to assign a workspace."
            )
            self.download_target.clear()
            self.download_target.addItem("Choose automatically", "auto")
            self.recipient.clear()
            self.recipient.addItem("Everyone in this workspace", None)
            self._render_messages()
            self._update_send_controls()
            self._update_guidance()
            if workspace_id:
                self.refresh_selected(force=True)

        def refresh_all(self):
            self.refresh_workspaces()
            self.refresh_selected(force=True)

        def refresh_selected(self, *, force: bool = False):
            if not self.workspace_id:
                return
            self.refresh_members(force=force)
            self.refresh_files(force=force)
            self.refresh_jobs(force=force)
            self.refresh_messages(force=force)

        def refresh_members(self, *, force: bool = False):
            if self.workspace_id and (force or "members" not in self._busy):
                workspace_id = self.workspace_id
                self._run_async(
                    lambda: self.client.members(workspace_id),
                    self._set_members,
                    section="members",
                )

        def _set_members(self, response):
            workers = response.get("items", [])
            agents = response.get("agents", [])
            self._members_loaded = True
            self._worker_count = len(workers)
            self._online_worker_count = sum(
                worker.get("status") == "online" for worker in workers
            )
            self._participant_names = {
                member["id"]: member.get("name") or member["id"]
                for member in [*workers, *agents]
            }
            self.roster_hint.setText(
                f"{len(workers)} download worker(s), {self._online_worker_count} online · {len(agents)} AI participant(s) in this workspace"
            )
            self._update_guidance()
            selected_target, selected_recipient = (
                self.download_target.currentData(),
                self.recipient.currentData(),
            )
            self.download_target.clear()
            self.download_target.addItem("Choose automatically", "auto")
            self.recipient.clear()
            self.recipient.addItem("Everyone in this workspace", None)
            for member in workers:
                member_id = member.get("worker_id") or member["id"]
                self.download_target.addItem(
                    f"{member.get('name', member_id)} · {member.get('status', '')}",
                    member_id,
                )
            for member in [*workers, *agents]:
                if member["id"] != self.client.identity.actor_id:
                    self.recipient.addItem(
                        member.get("name") or member["id"], member["id"]
                    )
            self.download_target.setCurrentIndex(
                max(0, self.download_target.findData(selected_target))
            )
            self.recipient.setCurrentIndex(
                max(0, self.recipient.findData(selected_recipient))
            )
            self._fill(
                self.members_table,
                [
                    [member.get("name"), kind, member.get("status"), member["id"]]
                    for kind, roster in (
                        ("Download worker", workers),
                        ("AI participant", agents),
                    )
                    for member in roster
                ],
            )
            for row, member in enumerate([*workers, *agents]):
                self.members_table.item(row, 0).setToolTip(
                    f"Participant ID: {member['id']}"
                )
            self._render_messages()
            self._update_send_controls()

        def refresh_files(self, *, force: bool = False):
            if self.workspace_id and (force or "files" not in self._busy):
                workspace_id, query, offset = (
                    self.workspace_id,
                    self.file_query.text(),
                    self._file_offset,
                )
                self._run_async(
                    lambda: self.client.files(
                        workspace_id, q=query, limit=self.page_size, offset=offset
                    ),
                    self._set_files,
                    section="files",
                )

        def _set_files(self, response):
            items = response.get("items", [])
            self._fill(
                self.files_table,
                [
                    [
                        item.get("worker_name") or item.get("worker_id"),
                        item.get("path"),
                        self._format_bytes(item.get("size_bytes")),
                        "Present"
                        if item.get("present") is True
                        else "Missing"
                        if item.get("present") is False
                        else "Not checked",
                        item.get("downloaded_at"),
                    ]
                    for item in items
                ],
            )
            total = response.get("total", 0)
            self.files_hint.setText(
                "No files match this search. Try a different name or clear the search."
                if not items and self.file_query.text().strip()
                else "No completed downloads yet. Open Downloads and request a file; completed files will appear here."
                if not total
                else "Copy a selected path to find the file on its worker. Availability is the worker's last reported check."
            )
            start, end = (
                (self._file_offset + 1 if items else 0),
                self._file_offset + len(items),
            )
            self.files_summary.setText(
                f"Showing {start}–{end} of {total} files · {self._format_bytes(response.get('total_bytes', 0))} · {response.get('present_count', 0)} confirmed present"
            )
            self.files_previous.setEnabled(self._file_offset > 0)
            self.files_next.setEnabled(self._file_offset + len(items) < total)

        @staticmethod
        def _format_bytes(value):
            if value is None:
                return "Unknown"
            amount = max(0, value)
            for unit in ("B", "KB", "MB", "GB", "TB"):
                if amount < 1024 or unit == "TB":
                    return (
                        f"{amount:,.0f} {unit}"
                        if unit == "B"
                        else f"{amount:,.1f} {unit}"
                    )
                amount /= 1024

        def _file_filter_changed(self, *_args):
            self._file_offset = 0
            self._latest["files"] = self._latest.get("files", 0) + 1
            self.filter_timer.start()

        def _move_file_page(self, direction):
            self._file_offset = max(0, self._file_offset + direction * self.page_size)
            self.refresh_files(force=True)

        def _move_job_page(self, direction):
            self._job_offset = max(0, self._job_offset + direction * self.page_size)
            self.refresh_jobs(force=True)

        def refresh_jobs(self, *, force: bool = False):
            if self.workspace_id and (force or "jobs" not in self._busy):
                workspace_id, offset = self.workspace_id, self._job_offset
                self._run_async(
                    lambda: self.client.jobs(
                        workspace_id, limit=self.page_size, offset=offset
                    ),
                    self._set_jobs,
                    section="jobs",
                )

        def _set_jobs(self, response):
            items = response.get("items", [])
            self._fill(
                self.jobs_table,
                [
                    [
                        item.get("title") or item.get("canonical_url"),
                        item.get("status"),
                        item.get("stage"),
                        f"{item.get('progress', 0):.0f}%",
                        self._participant_names.get(
                            item.get("claimed_by") or item.get("target_worker_id"),
                            item.get("claimed_by")
                            or item.get("target_worker_id")
                            or "Choose automatically",
                        ),
                    ]
                    for item in items
                ],
            )
            total = response.get("total", 0)
            start, end = (
                (self._job_offset + 1 if items else 0),
                self._job_offset + len(items),
            )
            self.jobs_summary.setText(f"Showing {start}–{end} of {total} downloads")
            self.jobs_hint.setText(
                "No downloads requested yet. Paste links above, choose a worker, and select Request a download."
                if not total
                else "Requests are sent to workspace workers. Completed files appear in Files."
            )
            self.jobs_previous.setEnabled(self._job_offset > 0)
            self.jobs_next.setEnabled(self._job_offset + len(items) < total)

        def refresh_messages(self, *, force: bool = False):
            if self.workspace_id and (force or "messages" not in self._busy):
                workspace_id = self.workspace_id
                after_seq = self._cursors.get(workspace_id, 0)
                self._run_async(
                    lambda: self.client.messages(workspace_id, after_seq=after_seq),
                    lambda response: self._set_messages(workspace_id, response),
                    section="messages",
                )

        def _set_messages(self, workspace_id, response):
            if workspace_id != self.workspace_id:
                return
            cursor = response.get("next_after_seq", self._cursors.get(workspace_id, 0))
            self._cursors[workspace_id] = max(
                self._cursors.get(workspace_id, 0), cursor
            )
            existing = self._messages.setdefault(workspace_id, [])
            seen = {message["id"] for message in existing}
            existing.extend(
                message
                for message in response.get("items", [])
                if message["id"] not in seen
            )
            # Keep the native viewer's in-memory transcript bounded.
            self._messages[workspace_id] = existing[-2000:]
            self._render_messages()

        def _render_messages(self):
            lines = []
            for message in self._messages.get(self.workspace_id, []):
                directed = (
                    f" → {self._participant_names.get(message['recipient_id'], message['recipient_id'])} (directed)"
                    if message.get("recipient_id")
                    else ""
                )
                lines.append(
                    f"{message.get('created_at', '')}  {message.get('sender_name') or message.get('sender_id')}{directed}\n{message.get('text', '')}"
                )
            self.speaking_log.setPlainText("\n\n".join(lines))
            self.speaking_log.setPlaceholderText(
                "No messages yet. Start a conversation below. Messages are shared only with the selected workspace or recipient."
                if self.workspace_id
                else "Choose an assigned workspace to join its chat."
            )

        def _update_send_controls(self):
            if not hasattr(self, "send_button"):
                return
            text = self.message_input.toPlainText()
            self.send_button.setEnabled(
                bool(
                    self.workspace_id
                    and text.strip()
                    and len(text) <= 10000
                    and "send" not in self._busy
                )
            )
            urls = self.url_input.toPlainText().splitlines()
            self.download_button.setEnabled(
                bool(
                    self.workspace_id
                    and self._online_worker_count
                    and any(line.strip() for line in urls)
                    and len(urls) <= 5000
                    and "download" not in self._busy
                )
            )

        def _submission_key(self, operation: str, signature: tuple[Any, ...]) -> str:
            pending = self._pending_writes.get(operation)
            if pending is None or pending[0] != signature:
                pending = (signature, str(uuid4()))
                self._pending_writes[operation] = pending
            return pending[1]

        def _confirm_submission(
            self, operation: str, signature: tuple[Any, ...], key: str
        ):
            # A reply for a superseded body/workspace must not discard the key
            # retained for a newer request whose response may still be pending.
            if self._pending_writes.get(operation) == (signature, key):
                self._pending_writes.pop(operation)

        def _message_signature(self) -> tuple[Any, ...]:
            return (
                self.workspace_id,
                self.message_input.toPlainText(),
                self.recipient.currentData(),
            )

        def _download_signature(self) -> tuple[Any, ...]:
            urls = tuple(
                line.strip()
                for line in self.url_input.toPlainText().splitlines()
                if line.strip()
            )
            return (
                self.workspace_id,
                urls,
                "original",
                self.download_target.currentData() or "auto",
                self.output_subdir.text().strip() or None,
            )

        def send_message(self):
            if not self.send_button.isEnabled() or not self.workspace_id:
                return
            workspace_id, text = self.workspace_id, self.message_input.toPlainText()
            recipient = self.recipient.currentData()
            signature = self._message_signature()
            key = self._submission_key("send", signature)
            self.send_button.setEnabled(False)

            def sent(_response):
                self._confirm_submission("send", signature, key)
                if self._message_signature() == signature:
                    self.message_input.clear()
                self.status_label.setText("Message sent")
                self.refresh_messages(force=True)

            self._run_async(
                lambda: self.client.send_message(
                    workspace_id, text, recipient_id=recipient, idempotency_key=key
                ),
                sent,
                section="send",
            )

        def request_download(self):
            if not self.download_button.isEnabled() or not self.workspace_id:
                return
            workspace_id = self.workspace_id
            raw = self.url_input.toPlainText()
            urls = [line.strip() for line in raw.splitlines() if line.strip()]
            target, subdir = (
                self.download_target.currentData() or "auto",
                self.output_subdir.text().strip() or None,
            )
            signature = self._download_signature()
            key = self._submission_key("download", signature)
            self.download_button.setEnabled(False)

            def submitted(response):
                self._confirm_submission("download", signature, key)
                if self._download_signature() == signature:
                    self.url_input.clear()
                self.status_label.setText(
                    f"Download request accepted · {response.get('dispatched', len(response.get('jobs', [])))} job(s)"
                )
                self.refresh_jobs(force=True)

            self._run_async(
                lambda: self.client.downloads(
                    workspace_id,
                    urls,
                    target_worker_id=target,
                    output_subdir=subdir,
                    idempotency_key=key,
                ),
                submitted,
                section="download",
            )

        def copy_file_path(self):
            row = self.files_table.currentRow()
            item = self.files_table.item(row, 1) if row >= 0 else None
            if item:
                QApplication.clipboard().setText(item.text())

        def poll(self):
            if not self._connected or not self.workspace_id:
                self.refresh_workspaces()
            self.refresh_selected()

        def closeEvent(self, event):
            self._mark_closed()
            self.poll_timer.stop()
            self.filter_timer.stop()
            self.thread_pool.clear()
            super().closeEvent(event)

        def _mark_closed(self, *_args):
            self._closed = True
            self._generation += 1

    def _choose_access_file() -> Path | None:
        """Explain the Node role before choosing a private participant access file."""
        dialog = QDialog()
        dialog.setWindowTitle("Welcome to Ateable Node")
        dialog.setWindowIcon(app_icon())
        dialog.setMinimumWidth(500)
        dialog.setStyleSheet("""
            QDialog, QLabel { background:#000000; color:#f1f1f1; font-size:13px; }
            QPushButton { background:#000000; color:white; border:1px solid #646464;
                border-radius:5px; padding:8px 12px; }
            QPushButton:hover { background:#383838; }
        """)
        layout = QVBoxLayout(dialog)
        heading = QLabel("Join your workspace")
        heading.setStyleSheet("font-size:21px;font-weight:600;")
        layout.addWidget(heading)
        explanation = QLabel(
            "Ateable Node lets you view shared file lists, request downloads, and chat "
            "with people and AI participants in assigned workspaces.\n\n"
            "Ask your Ateable admin for a private access file. On an automatically paired "
            "worker, Ateable normally finds this file for you.\n\n"
            "Choose your AI session or worker access file to connect. Keep it private: "
            "it grants access to your workspace."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        selected: list[Path] = []

        def choose():
            filename, _ = QFileDialog.getOpenFileName(
                dialog,
                "Choose your Ateable access file",
                str(default_session_file().parent),
                "Ateable access files (*.json);;All files (*)",
            )
            if filename:
                selected.append(Path(filename))
                dialog.accept()

        choose_button = QPushButton("Choose access file…")
        choose_button.clicked.connect(choose)
        layout.addWidget(choose_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
        return selected[0] if selected else None


def launch(client: ParticipantClient) -> int:
    if not PYSIDE_AVAILABLE:
        raise ParticipantError(
            "Native Ateable Node viewer requires the optional desktop dependencies (PySide6)"
        )
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Ateable Node")
    app.setOrganizationName("Ateable")
    app.setWindowIcon(app_icon())
    window = ParticipantWindow(client)
    window.show()
    return app.exec()


class _UiParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ParticipantError("Invalid Node viewer arguments; use --help")


def main(argv: list[str] | None = None) -> int:
    """Windowed entry point with a local credential picker and readable errors."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = _UiParser(
        prog="ateable-node-ui",
        description="Join assigned workspaces to view files, request downloads, and chat using a private access file.",
    )
    identity_group = parser.add_mutually_exclusive_group()
    identity_group.add_argument("--session-file", type=Path)
    identity_group.add_argument("--credentials-file", type=Path)
    parser.add_argument("--controller-url")
    if "--help" in arguments or "-h" in arguments:
        if sys.stdout is not None:
            parser.print_help()
        return 0
    if not PYSIDE_AVAILABLE:
        if sys.stderr is not None:
            print("Ateable Node viewer requires PySide6", file=sys.stderr)
        return 2
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Ateable Node")
    app.setOrganizationName("Ateable")
    try:
        args = parser.parse_args(arguments)
        if (
            args.session_file is None
            and args.credentials_file is None
            and not default_session_file().is_file()
            and not default_credentials_file().is_file()
        ):
            access_file = _choose_access_file()
            if access_file is None:
                return 0
            # Identify a selected local credential file using the same bounded,
            # symlink-rejecting reader as the CLI. This never accepts admin auth.
            if "agent_id" in _read_session(access_file):
                args.session_file = access_file
            else:
                args.credentials_file = access_file
        identity = load_identity(
            session_file=args.session_file,
            credentials_file=args.credentials_file,
            controller_url=args.controller_url,
        )
        return launch(ParticipantClient(identity))
    except ParticipantError as exc:
        QMessageBox.critical(None, "Ateable Node", str(exc))
        return 1
