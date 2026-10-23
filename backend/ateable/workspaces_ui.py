"""Administrator workspace membership, file logs and Speaking controls."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


class WorkspacesPanel(QWidget):
    def __init__(self, owner: Any) -> None:
        super().__init__()
        self.owner = owner
        self.workspaces: list[dict[str, Any]] = []
        self.members: list[dict[str, Any]] = []
        self.agents: list[dict[str, Any]] = []
        self.workspace_id: str | None = None
        self.revision = self.cursor = self.offset = self.total = 0
        self.loading = self.mutating = False
        self.seen: set[int] = set()
        self._pending_message: tuple[tuple[Any, ...], str] | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        title = QLabel("Share file logs and talk in a workspace")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        explanation = QLabel(
            "A workspace groups download computers and AI clients. Members can see where Ateable files are stored and chat in Speaking. Files stay on their computers."
        )
        explanation.setWordWrap(True)
        explanation.setProperty("muted", True)
        layout.addWidget(explanation)
        picker = QHBoxLayout()
        self.workspace = QComboBox()
        self.workspace.setAccessibleName("Admin workspace")
        self.workspace.currentIndexChanged.connect(self._select)
        picker.addWidget(self.workspace, 1)
        create = QPushButton("New workspace")
        create.clicked.connect(self.create_workspace)
        picker.addWidget(create)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        picker.addWidget(refresh)
        layout.addLayout(picker)
        self.summary = QLabel(
            "Optional: create a workspace, then add connected download computers."
        )
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        columns = QHBoxLayout()
        layout.addLayout(columns, 1)
        sidebar = QVBoxLayout()
        columns.addLayout(sidebar, 1)
        member_group = QGroupBox("Download computers")
        member_group.setMaximumWidth(290)
        member_layout = QVBoxLayout(member_group)
        self.member_list = QListWidget()
        self.member_list.setObjectName("spaceMembers")
        self.member_list.setAccessibleName("Workspace download computers")
        member_layout.addWidget(self.member_list, 1)
        self.worker = QComboBox()
        self.worker.setAccessibleName("Connected computer to add")
        member_layout.addWidget(self.worker)
        self.add_member = QPushButton("Add computer to workspace")
        self.add_member.setToolTip(
            "Share this computer’s Ateable file log with workspace members, including earlier downloads."
        )
        self.add_member.clicked.connect(self.enroll_worker)
        member_layout.addWidget(self.add_member)
        self.remove_member = QPushButton("Remove from workspace")
        self.remove_member.setToolTip(
            "Remove workspace access. The computer stays paired and its files are kept."
        )
        self.remove_member.clicked.connect(self.remove_worker)
        member_layout.addWidget(self.remove_member)
        sidebar.addWidget(member_group, 3)
        agent_group = QGroupBox("AI access")
        agent_group.setMaximumWidth(290)
        agent_layout = QVBoxLayout(agent_group)
        self.agent_list = QListWidget()
        self.agent_list.setObjectName("spaceAgents")
        self.agent_list.setAccessibleName("Workspace AI identities")
        agent_layout.addWidget(self.agent_list, 1)
        self.new_agent = QPushButton("Create AI session…")
        self.new_agent.setToolTip(
            "Create a private session file for one AI client in this workspace."
        )
        self.new_agent.clicked.connect(self.create_agent)
        agent_layout.addWidget(self.new_agent)
        self.revoke_agent = QPushButton("Revoke selected AI")
        self.revoke_agent.clicked.connect(self.revoke_selected_agent)
        agent_layout.addWidget(self.revoke_agent)
        sidebar.addWidget(agent_group, 2)
        self.views = QTabWidget()
        columns.addWidget(self.views, 4)
        file_page = QWidget()
        files_layout = QVBoxLayout(file_page)
        files_help = QLabel(
            "This log shows where Ateable downloads live on member computers. It does not copy files here or browse their other files."
        )
        files_help.setWordWrap(True)
        files_layout.addWidget(files_help)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search downloaded file paths")
        self.search.textChanged.connect(self._search_changed)
        files_layout.addWidget(self.search)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(self.refresh)
        self.files = self._table(
            ["Computer", "File path on that computer", "Size", "Last check"]
        )
        self.files.setAccessibleName("Workspace remote file log")
        files_layout.addWidget(self.files, 1)
        pager = QHBoxLayout()
        self.previous = QPushButton("Previous")
        self.previous.clicked.connect(lambda: self.change_page(-1))
        pager.addWidget(self.previous)
        self.page = QLabel("0 files")
        pager.addWidget(self.page)
        self.next = QPushButton("Next")
        self.next.clicked.connect(lambda: self.change_page(1))
        pager.addWidget(self.next)
        pager.addStretch()
        files_layout.addLayout(pager)
        self.views.addTab(file_page, "Files")
        job_page = QWidget()
        jobs_layout = QVBoxLayout(job_page)
        self.jobs_summary = QLabel("Downloads requested by members of this workspace")
        jobs_layout.addWidget(self.jobs_summary)
        self.jobs = self._table(["Source", "Computer", "Status", "Progress"])
        self.jobs.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.jobs.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.jobs.setAccessibleName("Workspace download requests")
        jobs_layout.addWidget(self.jobs, 1)
        self.views.addTab(job_page, "Downloads")
        speaking_page = QWidget()
        speaking_layout = QVBoxLayout(speaking_page)
        speaking_help = QLabel(
            "Speaking is workspace chat for people and AI clients. Choose everyone or a recipient; messages are saved as text."
        )
        speaking_help.setWordWrap(True)
        speaking_layout.addWidget(speaking_help)
        self.messages = QPlainTextEdit()
        self.messages.setReadOnly(True)
        self.messages.setMaximumBlockCount(2000)
        self.messages.setAccessibleName("Speaking message history")
        speaking_layout.addWidget(self.messages, 1)
        self.recipient = QComboBox()
        self.recipient.addItem("Everyone in this workspace", None)
        self.recipient.setAccessibleName("Speaking recipient")
        speaking_layout.addWidget(self.recipient)
        self.message = QPlainTextEdit()
        self.message.setPlaceholderText("Write a message as admin…")
        self.message.setAccessibleName("Speaking message")
        self.message.setMaximumHeight(86)
        self.message.textChanged.connect(self._controls)
        speaking_layout.addWidget(self.message)
        self.send = QPushButton("Send message")
        self.send.clicked.connect(self.send_message)
        speaking_layout.addWidget(self.send)
        self.views.addTab(speaking_page, "Speaking")
        self.feedback = QLabel(
            "Add computers from Devices first. Sharing a computer includes its earlier Ateable download log."
        )
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        self._controls()

    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        return table

    def reset(self) -> None:
        self.revision += 1
        self.workspace_id = None
        self.workspaces, self.members, self.agents = [], [], []
        self.cursor = self.offset = self.total = 0
        self.loading = self.mutating = False
        self.seen.clear()
        self._pending_message = None
        self.workspace.blockSignals(True)
        self.workspace.clear()
        self.workspace.blockSignals(False)
        self.member_list.clear()
        self.agent_list.clear()
        self.files.setRowCount(0)
        self.jobs.setRowCount(0)
        self.messages.clear()
        self.search_timer.stop()
        self.summary.setText(
            "Optional: create a workspace, then add connected download computers."
        )
        self._controls()

    def _select(self, *_args: Any) -> None:
        selected = self.workspace.currentData()
        if selected == self.workspace_id:
            return
        self.workspace_id = selected
        self.revision += 1
        self.cursor = self.offset = 0
        self.seen.clear()
        self.messages.clear()
        self.members, self.agents = [], []
        self.member_list.clear()
        self.agent_list.clear()
        self.files.setRowCount(0)
        self.jobs.setRowCount(0)
        self.loading = False
        self._controls()
        self.refresh()

    def _search_changed(self, *_args: Any) -> None:
        self.offset = 0
        self.revision += 1
        self.loading = False
        self.search_timer.start()

    def change_page(self, delta: int) -> None:
        self.offset = max(0, self.offset + delta * 200)
        self.revision += 1
        self.loading = False
        self.refresh()

    def update_workers(self) -> None:
        previous = self.worker.currentData()
        self.worker.clear()
        enrolled = {item["worker_id"] for item in self.members}
        for worker in self.owner.workers:
            if (
                worker.get("id")
                and worker["id"] != "local"
                and worker["id"] not in enrolled
            ):
                self.worker.addItem(
                    f"{worker.get('name') or worker['id']} · {worker.get('status', 'offline')}",
                    worker["id"],
                )
        self.worker.setCurrentIndex(max(0, self.worker.findData(previous)))
        self._controls()

    def _controls(self, *_args: Any) -> None:
        active = bool(self.workspace_id) and not self.mutating
        for button in (self.remove_member, self.new_agent, self.revoke_agent):
            button.setEnabled(active)
        self.add_member.setEnabled(active and bool(self.worker.currentData()))
        self.send.setEnabled(
            active and 0 < len(self.message.toPlainText().strip()) <= 10000
        )
        self.previous.setEnabled(self.offset > 0)
        self.next.setEnabled(self.offset + self.files.rowCount() < self.total)

    def refresh(self, *_args: Any) -> None:
        if self.loading:
            return
        self.loading = True
        client, selected, revision = self.owner.client, self.workspace_id, self.revision
        after_seq, offset, query = self.cursor, self.offset, self.search.text().strip()

        def fetch() -> dict[str, Any]:
            workspaces = client.request("GET", "/api/workspaces")["items"]
            chosen = selected or (workspaces[0]["id"] if workspaces else None)
            result: dict[str, Any] = {"workspaces": workspaces, "selected": chosen}
            if chosen:
                prefix = f"/api/workspaces/{chosen}"
                for key in ("members", "agents", "files", "jobs", "messages"):
                    options = {
                        "files": {"q": query or None, "limit": 200, "offset": offset},
                        "jobs": {"limit": 200},
                        "messages": {"after_seq": after_seq, "limit": 100},
                    }
                    result[key] = client.request(
                        "GET", prefix + "/" + key, query=options.get(key)
                    )
            return result

        def loaded(result: dict[str, Any]) -> None:
            if selected != self.workspace_id or revision != self.revision:
                return
            self.workspaces, self.workspace_id = (
                result["workspaces"],
                result["selected"],
            )
            self.workspace.blockSignals(True)
            self.workspace.clear()
            for entry in self.workspaces:
                self.workspace.addItem(entry["name"], entry["id"])
            self.workspace.setCurrentIndex(
                max(0, self.workspace.findData(self.workspace_id))
            )
            self.workspace.blockSignals(False)
            if not self.workspace_id:
                self._controls()
                return
            self.members = [
                {**item, "worker_id": item.get("worker_id") or item["id"]}
                for item in result["members"]["items"]
            ]
            self.agents = [
                {**item, "agent_id": item.get("agent_id") or item["id"]}
                for item in result["agents"]["items"]
            ]
            self._identities()
            self.update_workers()
            from .desktop import format_bytes

            file_info = result["files"]
            self.total = file_info["total"]
            self.summary.setText(
                f"{len(self.members)} computers · {self.total:,} file records · {format_bytes(file_info.get('total_bytes', 0))} recorded output"
            )
            self._rows(
                self.files,
                [
                    (
                        item["worker_name"],
                        item["path"],
                        format_bytes(item.get("size_bytes")),
                        "Present"
                        if item.get("present") is True
                        else "Missing"
                        if item.get("present") is False
                        else "Not checked",
                    )
                    for item in file_info["items"]
                ],
            )
            self.page.setText(
                f"{self.offset + 1 if self.total else 0}–{self.offset + self.files.rowCount()} of {self.total:,}"
            )
            jobs_info = result["jobs"]
            self.jobs_summary.setText(
                f"{jobs_info['total']:,} workspace jobs · showing the latest {len(jobs_info['items']):,}"
            )
            self._rows(
                self.jobs,
                [
                    (
                        item.get("title") or item.get("source_url"),
                        item.get("target_worker_id"),
                        "expanded"
                        if item.get("stage") == "expanded"
                        else item.get("status"),
                        f"{item.get('progress', 0):.0f}%",
                    )
                    for item in jobs_info["items"]
                ],
            )
            self._messages(result["messages"])
            self._controls()

        def finished() -> None:
            if revision == self.revision:
                self.loading = False

        self.owner._run_async(fetch, loaded, on_error=self._error, on_finished=finished)

    @staticmethod
    def _rows(table: QTableWidget, rows: list[tuple[Any, ...]]) -> None:
        table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value if value is not None else "—"))
                cell.setToolTip(cell.text())
                table.setItem(row, column, cell)

    def _identities(self) -> None:
        for widget, entries, key in (
            (self.member_list, self.members, "worker_id"),
            (self.agent_list, self.agents, "agent_id"),
        ):
            selected = (
                widget.currentItem().data(Qt.ItemDataRole.UserRole)
                if widget.currentItem()
                else None
            )
            widget.clear()
            for entry in entries:
                revoked = " · revoked" if entry.get("revoked_at") else ""
                item = QListWidgetItem(str(entry["name"]) + revoked)
                item.setData(Qt.ItemDataRole.UserRole, entry[key])
                item.setToolTip(entry[key])
                widget.addItem(item)
                if entry[key] == selected:
                    widget.setCurrentItem(item)
        previous = self.recipient.currentData()
        self.recipient.clear()
        self.recipient.addItem("Everyone in this workspace", None)
        for entry in self.members:
            self.recipient.addItem("Computer · " + entry["name"], entry["worker_id"])
        for entry in self.agents:
            if not entry.get("revoked_at"):
                self.recipient.addItem("AI · " + entry["name"], entry["agent_id"])
        self.recipient.setCurrentIndex(max(0, self.recipient.findData(previous)))

    def _messages(self, payload: dict[str, Any]) -> None:
        scrolling = self.messages.verticalScrollBar()
        at_bottom = scrolling.value() == scrolling.maximum()
        for item in payload["items"]:
            if item["seq"] in self.seen:
                continue
            self.seen.add(item["seq"])
            recipient = f" → {item['recipient_id']}" if item.get("recipient_id") else ""
            self.messages.appendPlainText(
                f"{item['created_at']} · {item['sender_name']}{recipient}\n{item['text']}\n"
            )
        self.cursor = max(self.cursor, payload["next_after_seq"])
        if len(self.seen) > 2000:
            self.seen = set(sorted(self.seen)[-1000:])
        if at_bottom:
            scrolling.setValue(scrolling.maximum())

    def _error(self, message: str) -> None:
        self.feedback.setText(message)

    def _mutate(self, operation: Any, success: Any = None) -> None:
        if self.mutating:
            return
        self.mutating = True
        self._controls()

        def loaded(result: Any) -> None:
            if success:
                success(result)
            self.refresh()

        def finished() -> None:
            self.mutating = False
            self._controls()

        self.owner._run_async(
            operation, loaded, on_error=self._error, on_finished=finished
        )

    def create_workspace(self) -> None:
        name, accepted = QInputDialog.getText(self, "New workspace", "Workspace name")
        if accepted and name.strip():
            client = self.owner.client

            def created(result: Any) -> None:
                self.workspace_id = result["id"]
                self.revision += 1
                self.cursor = self.offset = 0
                self.seen.clear()
                self.messages.clear()
                self.loading = False

            self._mutate(
                lambda: client.request(
                    "POST", "/api/workspaces", {"name": name.strip()}
                ),
                created,
            )

    def enroll_worker(self) -> None:
        client, workspace_id, worker_id = (
            self.owner.client,
            self.workspace_id,
            self.worker.currentData(),
        )
        if workspace_id and worker_id:
            self._mutate(
                lambda: client.request(
                    "POST",
                    f"/api/workspaces/{workspace_id}/members",
                    {"worker_id": worker_id},
                )
            )

    def remove_worker(self) -> None:
        selected = self.member_list.currentItem()
        if selected:
            client, workspace_id = self.owner.client, self.workspace_id
            worker_id = selected.data(Qt.ItemDataRole.UserRole)
            self._mutate(
                lambda: client.request(
                    "DELETE", f"/api/workspaces/{workspace_id}/members/{worker_id}"
                )
            )

    def create_agent(self) -> None:
        name, accepted = QInputDialog.getText(self, "New AI identity", "AI name")
        if accepted and name.strip():
            client, workspace_id = self.owner.client, self.workspace_id
            self._mutate(
                lambda: client.request(
                    "POST",
                    f"/api/workspaces/{workspace_id}/agents",
                    {"name": name.strip()},
                ),
                lambda result: self._show_session(workspace_id, result),
            )

    def revoke_selected_agent(self) -> None:
        selected = self.agent_list.currentItem()
        if selected:
            client, workspace_id = self.owner.client, self.workspace_id
            agent_id = selected.data(Qt.ItemDataRole.UserRole)
            self._mutate(
                lambda: client.request(
                    "DELETE", f"/api/workspaces/{workspace_id}/agents/{agent_id}"
                )
            )

    def _show_session(self, workspace_id: str, actor: dict[str, Any]) -> None:
        session = {
            "controller_url": self.owner.client.base_url,
            "agent_id": actor["agent_id"],
            "token": actor["token"],
            "workspace_id": workspace_id,
        }
        dialog = QDialog(self)
        dialog.setWindowTitle("AI session for this workspace")
        layout = QVBoxLayout(dialog)
        description = QLabel(
            "Save this private session file for your AI client. It can request downloads, read this workspace’s file log and use Speaking. It cannot manage computers. A client on another computer needs a controller address it can reach."
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        form = QFormLayout()
        identifier = QLineEdit(actor["agent_id"])
        identifier.setReadOnly(True)
        form.addRow("AI identity", identifier)
        token = QLineEdit(actor["token"])
        token.setReadOnly(True)
        token.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Session token", token)
        layout.addLayout(form)
        actions = QHBoxLayout()
        save = QPushButton("Save session JSON…")

        def write_session() -> None:
            filename, _ = QFileDialog.getSaveFileName(
                dialog,
                "Save AI credentials privately",
                "ateable-ai-session.json",
                "JSON session (*.json)",
            )
            if filename:
                from .participant import save_session

                try:
                    save_session(Path(filename), session)
                except (OSError, ValueError, RuntimeError) as exc:
                    self._error(str(exc))
                    return
                self.feedback.setText(
                    "AI session saved. Keep its token private; revoke access here when needed."
                )

        save.clicked.connect(write_session)
        actions.addWidget(save)
        copy = QPushButton("Copy token")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(token.text()))
        actions.addWidget(copy)
        layout.addLayout(actions)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        dialog.resize(640, 230)
        dialog.exec()
        token.clear()

    def send_message(self) -> None:
        if self.mutating:
            return
        text = self.message.toPlainText().strip()
        if not self.workspace_id or not text or len(text) > 10000:
            return
        draft = self.message.toPlainText()
        client, workspace_id = self.owner.client, self.workspace_id
        signature = (workspace_id, text, self.recipient.currentData())
        if not self._pending_message or self._pending_message[0] != signature:
            self._pending_message = (signature, str(uuid4()))
        pending_message = self._pending_message
        body = {
            "text": text,
            "recipient_id": self.recipient.currentData(),
            "idempotency_key": pending_message[1],
        }

        def sent(_result: Any) -> None:
            if self._pending_message == pending_message:
                self._pending_message = None
            if self.message.toPlainText() == draft:
                self.message.clear()
            self.feedback.setText("Message saved in Speaking.")

        self._mutate(
            lambda: client.request(
                "POST", f"/api/workspaces/{workspace_id}/messages", body
            ),
            sent,
        )
