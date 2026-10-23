"""Native bucket capture and dispatch controls; all requests carry metadata only."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
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
    QVBoxLayout,
    QWidget,
)

from .capture_pc import install_windows_menu, parse_capture_text, read_capture_file
from .runtime_paths import resource_root


def extension_directory() -> Path:
    return resource_root() / "browser-extension"


class BucketsPanel(QWidget):
    def __init__(self, owner: Any) -> None:
        super().__init__()
        self.owner = owner
        self.bucket_id: str | None = None
        self.buckets: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []
        self.offset, self.total, self.revision = 0, 0, 0
        self.loading = False
        self.mutating = False
        self.active_dispatch: dict[str, Any] | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 16)
        headline = QLabel("Save links now. Download them when you’re ready.")
        headline.setObjectName("sectionTitle")
        outer.addWidget(headline)
        help_text = QLabel(
            "A bucket is a saved list of links, not a storage folder. Save file, media or S3 links here; files stay on the computer that downloads them."
        )
        help_text.setWordWrap(True)
        help_text.setProperty("muted", True)
        outer.addWidget(help_text)
        columns = QHBoxLayout()
        outer.addLayout(columns, 1)
        sidebar = QVBoxLayout()
        self.bucket_list = QListWidget()
        self.bucket_list.setObjectName("bucketList")
        self.bucket_list.setAccessibleName("Saved buckets")
        self.bucket_list.setMaximumWidth(240)
        self.bucket_list.currentItemChanged.connect(self._selection_changed)
        sidebar.addWidget(self.bucket_list, 1)
        self.new_button = QPushButton("New bucket")
        self.new_button.clicked.connect(self.create_bucket)
        sidebar.addWidget(self.new_button)
        self.rename_button = QPushButton("Rename bucket")
        self.rename_button.clicked.connect(self.rename_bucket)
        sidebar.addWidget(self.rename_button)
        columns.addLayout(sidebar)
        content = QVBoxLayout()
        columns.addLayout(content, 1)
        self.summary = QLabel("Loading buckets…")
        self.summary.setWordWrap(True)
        content.addWidget(self.summary)
        capture_row = QHBoxLayout()
        self.draft = QPlainTextEdit()
        self.draft.setAccessibleName("Links to save in bucket")
        self.draft.setPlaceholderText(
            "Paste links here · one per line\nhttps://…/archive.zip   or   s3://bucket/prefix/"
        )
        self.draft.setMaximumHeight(86)
        self.draft.textChanged.connect(self._update_controls)
        capture_row.addWidget(self.draft, 1)
        capture_buttons = QVBoxLayout()
        self.add_button = QPushButton("Save links")
        self.add_button.setToolTip(
            "Save these URLs in this bucket without downloading files."
        )
        self.add_button.clicked.connect(self.capture_draft)
        capture_buttons.addWidget(self.add_button)
        self.import_button = QPushButton("Import URL list…")
        self.import_button.clicked.connect(self.import_list)
        capture_buttons.addWidget(self.import_button)
        capture_row.addLayout(capture_buttons)
        content.addLayout(capture_row)
        tools = QHBoxLayout()
        self.browser_button = QPushButton("Browser capture…")
        self.browser_button.clicked.connect(self.browser_setup)
        tools.addWidget(self.browser_button)
        self.pc_button = QPushButton("PC right-click…")
        self.pc_button.clicked.connect(self.pc_setup)
        self.pc_button.setVisible(os.name == "nt")
        tools.addWidget(self.pc_button)
        self.export_button = QPushButton("Export links…")
        self.export_button.clicked.connect(self.export_links)
        tools.addWidget(self.export_button)
        tools.addStretch()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        tools.addWidget(refresh)
        content.addLayout(tools)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search title or URL in this bucket")
        self.search.setAccessibleName("Search bucket")
        self.search.textChanged.connect(self._search_changed)
        content.addWidget(self.search)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(self.refresh)
        self.table = QTableWidget(0, 4)
        self.table.setAccessibleName("Bucket links and download status")
        self.table.setHorizontalHeaderLabels(
            ["Title / file", "Source URL", "Download status", "Saved from"]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Interactive
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.table.setColumnWidth(0, 170)
        self.table.setColumnWidth(2, 175)
        self.table.setColumnWidth(3, 130)
        content.addWidget(self.table, 1)
        pager = QHBoxLayout()
        self.previous = QPushButton("Previous")
        self.previous.clicked.connect(lambda: self.change_page(-1))
        pager.addWidget(self.previous)
        self.page = QLabel("0 links")
        pager.addWidget(self.page)
        self.next = QPushButton("Next")
        self.next.clicked.connect(lambda: self.change_page(1))
        pager.addWidget(self.next)
        pager.addStretch()
        self.remove_button = QPushButton("Remove saved link")
        self.remove_button.setToolTip(
            "Remove the link from this bucket. Existing jobs and downloaded files are kept."
        )
        self.remove_button.clicked.connect(self.remove_selected)
        pager.addWidget(self.remove_button)
        content.addLayout(pager)
        dispatch = QHBoxLayout()
        dispatch.addWidget(QLabel("Download computer"))
        self.worker = QComboBox()
        self.worker.setAccessibleName("Bucket download computer")
        self.worker.setToolTip("Choose where files will be downloaded and stored.")
        self.worker.addItem("Automatic · online computers", "auto")
        self.worker.currentIndexChanged.connect(self._update_controls)
        dispatch.addWidget(self.worker, 1)
        self.format = QComboBox()
        from .desktop import SUPPORTED_FORMATS

        for media_format in SUPPORTED_FORMATS:
            label = (
                "Keep original file"
                if media_format == "original"
                else media_format.upper()
            )
            self.format.addItem(label, media_format)
            self.format.setItemData(
                self.format.count() - 1,
                "Keep the source file. Required for S3 and recommended for data files."
                if media_format == "original"
                else f"Convert supported media to {media_format.upper()} on the download computer.",
                Qt.ItemDataRole.ToolTipRole,
            )
        self.format.setToolTip(
            "Keep original file for archives, datasets and S3. Other formats convert supported media."
        )
        self.format.currentIndexChanged.connect(self._update_controls)
        dispatch.addWidget(self.format)
        self.download_button = QPushButton("Download saved links")
        self.download_button.clicked.connect(self.dispatch)
        dispatch.addWidget(self.download_button)
        content.addLayout(dispatch)
        self.feedback = QLabel(
            "Saving links does not download files. Connect a computer in Devices, then choose Download saved links."
        )
        self.feedback.setWordWrap(True)
        content.addWidget(self.feedback)
        self._update_controls()

    def reset(self) -> None:
        self.revision += 1
        self.bucket_id = None
        self.buckets, self.items = [], []
        self.offset = self.total = 0
        self.loading = self.mutating = False
        self.active_dispatch = None
        self.search_timer.stop()
        self.bucket_list.clear()
        self.table.setRowCount(0)
        self.summary.setText("Select a bucket on this controller.")
        self._update_controls()

    def _selection_changed(self, current: Any, _previous: Any) -> None:
        chosen = current.data(Qt.ItemDataRole.UserRole) if current else None
        if chosen == self.bucket_id:
            return
        self.bucket_id, self.offset = chosen, 0
        self.active_dispatch = None
        self.revision += 1
        self.loading = False
        self.items = []
        self.table.setRowCount(0)
        self._update_controls()
        if chosen:
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

    def refresh(self, *_args: Any) -> None:
        if self.loading:
            return
        self.loading = True
        revision, selected, client = self.revision, self.bucket_id, self.owner.client
        query = {
            "q": self.search.text().strip() or None,
            "limit": 200,
            "offset": self.offset,
        }

        def fetch() -> dict[str, Any]:
            buckets = client.request("GET", "/api/buckets")
            chosen = selected or next(
                (item["id"] for item in buckets["items"] if item["id"] == "inbox"),
                None,
            )
            page = (
                client.request("GET", f"/api/buckets/{chosen}/items", query=query)
                if chosen
                else {"items": [], "total": 0}
            )
            return {"buckets": buckets["items"], "selected": chosen, "page": page}

        def loaded(result: dict[str, Any]) -> None:
            if revision != self.revision or selected != self.bucket_id:
                return
            self.buckets, self.bucket_id = result["buckets"], result["selected"]
            self.bucket_list.blockSignals(True)
            self.bucket_list.clear()
            for bucket in self.buckets:
                entry = QListWidgetItem(f"{bucket['name']}  ·  {bucket['total']:,}")
                entry.setData(Qt.ItemDataRole.UserRole, bucket["id"])
                self.bucket_list.addItem(entry)
                if bucket["id"] == self.bucket_id:
                    self.bucket_list.setCurrentItem(entry)
            self.bucket_list.blockSignals(False)
            self.items, self.total = result["page"]["items"], result["page"]["total"]
            self.table.setRowCount(len(self.items))
            for row, item in enumerate(self.items):
                status = (
                    item.get("status")
                    or item.get("job_status")
                    or ("dispatching" if item.get("dispatch_run_id") else "pending")
                )
                status_label = {
                    "pending": "Saved · not downloaded",
                    "dispatching": "Preparing download",
                    "queued": "Queued for download",
                    "downloading": "Downloading",
                    "paused": "Paused",
                    "completed": "Downloaded",
                    "failed": "Download failed",
                    "cancelled": "Cancelled",
                    "expanded": "Folder listed · see Queue",
                }.get(status, status)
                for column, value in enumerate(
                    (
                        item.get("title") or item.get("kind", "Link"),
                        item["source_url"],
                        status_label,
                        item.get("origin") or "manual",
                    )
                ):
                    cell = QTableWidgetItem(str(value))
                    cell.setToolTip(str(value))
                    self.table.setItem(row, column, cell)
            bucket = next(
                (entry for entry in self.buckets if entry["id"] == self.bucket_id), {}
            )
            self.active_dispatch = bucket.get("active_dispatch")
            self.summary.setText(
                f"{bucket.get('name', 'Bucket')} · {bucket.get('pending', 0):,} saved, not downloaded · {bucket.get('dispatched', 0):,} sent to Queue"
            )
            if self.active_dispatch and self.active_dispatch.get("error"):
                self.feedback.setText(
                    "A previous download request was interrupted. Retry download request to continue with the same links and computer."
                )
            self.page.setText(
                f"{self.offset + 1 if self.total else 0}–{self.offset + len(self.items)} of {self.total:,}"
            )
            self._update_controls()

        def finished() -> None:
            if revision == self.revision:
                self.loading = False

        self.owner._run_async(
            fetch, loaded, on_error=self._failed, on_finished=finished
        )

    def _failed(self, message: str) -> None:
        self.feedback.setText(message)

    def _mutate(self, operation: Any, success: Any = None) -> None:
        if self.mutating:
            return
        self.mutating = True
        self._update_controls()

        def done(result: Any) -> None:
            if success:
                success(result)
            self.refresh()

        def finished() -> None:
            self.mutating = False
            self._update_controls()

        self.owner._run_async(
            operation, done, on_error=self._failed, on_finished=finished
        )

    def _update_controls(self, *_args: Any) -> None:
        ready = bool(self.bucket_id) and not self.mutating
        self.add_button.setEnabled(ready and bool(self.draft.toPlainText().strip()))
        for button in (
            self.import_button,
            self.rename_button,
            self.browser_button,
            self.pc_button,
            self.export_button,
            self.remove_button,
        ):
            button.setEnabled(ready)
        self.previous.setEnabled(self.offset > 0)
        self.next.setEnabled(self.offset + len(self.items) < self.total)
        target = self.worker.currentData()
        eligible = any(
            worker.get("id") != "local"
            and worker.get("status") == "online"
            and (target == "auto" or worker.get("id") == target)
            and self.format.currentData() in worker.get("capabilities", [])
            for worker in self.owner.workers
        )
        self.download_button.setText(
            "Retry download request" if self.active_dispatch else "Download saved links"
        )
        self.download_button.setToolTip(
            "Continue the interrupted request using its original links and options."
            if self.active_dispatch
            else "Queue saved links on an online download computer. Already queued links are skipped."
            if eligible
            else "Connect a download computer in Devices that supports the selected file format. Your saved links are kept."
        )
        self.download_button.setEnabled(
            ready and (bool(self.active_dispatch) or eligible)
        )

    def update_workers(self) -> None:
        previous = self.worker.currentData()
        self.worker.blockSignals(True)
        self.worker.clear()
        self.worker.addItem("Automatic · online computers", "auto")
        for worker in self.owner.workers:
            if worker.get("id") and worker["id"] != "local":
                self.worker.addItem(
                    f"{worker.get('name') or worker['id']} · {worker.get('status', 'offline')}",
                    worker["id"],
                )
        self.worker.setCurrentIndex(max(0, self.worker.findData(previous)))
        self.worker.blockSignals(False)
        self._update_controls()

    def create_bucket(self) -> None:
        name, accepted = QInputDialog.getText(self, "New bucket", "Bucket name")
        if accepted and name.strip():
            client = self.owner.client

            def created(result: Any) -> None:
                self.bucket_id, self.offset = result["id"], 0
                self.active_dispatch = None
                self.revision += 1
                self.loading = False

            self._mutate(
                lambda: client.request("POST", "/api/buckets", {"name": name.strip()}),
                created,
            )

    def rename_bucket(self) -> None:
        bucket = next(
            (entry for entry in self.buckets if entry["id"] == self.bucket_id), {}
        )
        name, accepted = QInputDialog.getText(
            self, "Rename bucket", "Bucket name", text=bucket.get("name", "")
        )
        if accepted and name.strip():
            client, bucket_id = self.owner.client, self.bucket_id
            self._mutate(
                lambda: client.request(
                    "PATCH", f"/api/buckets/{bucket_id}", {"name": name.strip()}
                )
            )

    def capture_draft(self) -> None:
        try:
            items = parse_capture_text(self.draft.toPlainText())
        except ValueError as exc:
            self._failed(str(exc))
            return
        self._capture(items, clear=True)

    def _capture(self, items: list[dict[str, str]], *, clear: bool = False) -> None:
        client, bucket_id = self.owner.client, self.bucket_id
        draft_snapshot = self.draft.toPlainText()
        payload = {"items": items, "origin": "desktop", "idempotency_key": str(uuid4())}

        def saved(result: Any) -> None:
            if clear and self.draft.toPlainText() == draft_snapshot:
                self.draft.clear()
            self.feedback.setText(
                f"{result['accepted']:,} links saved · {result['duplicates']:,} already in bucket. Download saved links when you’re ready."
            )

        self._mutate(
            lambda: client.request("POST", f"/api/buckets/{bucket_id}/items", payload),
            saved,
        )

    def import_list(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Save links from a file",
            "",
            "URL lists (*.txt *.csv *.url *.m3u *.m3u8)",
        )
        if filename:
            try:
                items = read_capture_file(Path(filename))
            except (ValueError, OSError) as exc:
                self._failed(str(exc))
                return
            self._capture(items)

    def dispatch(self) -> None:
        client, bucket_id = self.owner.client, self.bucket_id
        if self.active_dispatch:
            run_id = self.active_dispatch["run_id"]
            operation = lambda: client.request(
                "POST", f"/api/buckets/{bucket_id}/dispatch-runs/{run_id}/retry", {}
            )
        else:
            payload = {
                "format": self.format.currentData(),
                "target_worker_id": self.worker.currentData(),
                "idempotency_key": str(uuid4()),
            }
            operation = lambda: client.request(
                "POST", f"/api/buckets/{bucket_id}/dispatch", payload
            )

        def dispatched(result: Any) -> None:
            self.feedback.setText(
                f"{result['dispatched']:,} links sent to Queue. Follow progress there; files are stored on the download computer."
            )
            self.owner.refresh_jobs()

        self._mutate(operation, dispatched)

    def remove_selected(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            client, bucket_id, item_id = (
                self.owner.client,
                self.bucket_id,
                self.items[row]["id"],
            )
            self._mutate(
                lambda: client.request(
                    "DELETE", f"/api/buckets/{bucket_id}/items/{item_id}"
                )
            )

    def export_links(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Export bucket metadata",
            "bucket-links.json",
            "JSON metadata (*.json);;URL text (*.txt)",
        )
        if not filename:
            return
        client, bucket_id = self.owner.client, self.bucket_id

        def write(result: Any) -> None:
            text = (
                "".join(item["source_url"] + "\n" for item in result["items"])
                if Path(filename).suffix.lower() == ".txt"
                else json.dumps(result, ensure_ascii=False, indent=2)
            )
            Path(filename).write_text(text, encoding="utf-8")
            self.feedback.setText("Bucket metadata exported.")

        self._mutate(
            lambda: client.request(
                "GET", f"/api/buckets/{bucket_id}/export", query={"format": "json"}
            ),
            write,
        )

    def browser_setup(self) -> None:
        from .desktop import is_loopback_controller

        if not is_loopback_controller(
            self.owner.client.base_url
        ) or not self.owner.client.base_url.startswith("http://"):
            self._failed(
                "Browser capture connects to a local HTTP controller. Choose the local controller in Settings."
            )
            return
        client, bucket_id = self.owner.client, self.bucket_id
        self._mutate(
            lambda: client.request(
                "POST",
                "/api/capture-tokens",
                {"bucket_id": bucket_id, "name": "Browser capture"},
            ),
            self._show_browser_token,
        )

    def _show_browser_token(self, result: Any) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Browser capture for this bucket")
        layout = QVBoxLayout(dialog)
        description = QLabel(
            "Chrome/Edge: load the browser-extension folder. Firefox: load the Firefox ZIP from outputs/browser-extensions using about:debugging. Open the extension's Options, paste this controller URL and capture token, then save. The token can only add links to this bucket."
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        form = QFormLayout()
        url = QLineEdit(self.owner.client.base_url)
        url.setReadOnly(True)
        form.addRow("Controller", url)
        token = QLineEdit(result["token"])
        token.setReadOnly(True)
        token.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Capture token", token)
        layout.addLayout(form)
        actions = QHBoxLayout()
        copy = QPushButton("Copy token")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(token.text()))
        actions.addWidget(copy)
        folder = QPushButton("Open extension folder")
        folder.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(extension_directory()))
            )
        )
        actions.addWidget(folder)
        firefox = QPushButton("Open Firefox packages")
        packages = extension_directory().parent / "outputs" / "browser-extensions"
        firefox.setEnabled(packages.is_dir())
        firefox.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(packages)))
        )
        actions.addWidget(firefox)
        revoke = QPushButton("Revoke this token")

        def revocation() -> None:
            client, token_id = self.owner.client, result["token_id"]
            self.owner._run_async(
                lambda: client.request("DELETE", f"/api/capture-tokens/{token_id}"),
                lambda _result: dialog.accept(),
                on_error=self._failed,
            )

        revoke.clicked.connect(revocation)
        actions.addWidget(revoke)
        layout.addLayout(actions)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(620, 230)
        dialog.exec()
        token.clear()

    def pc_setup(self) -> None:
        from .desktop import should_manage_local_controller

        if not should_manage_local_controller(self.owner.device_config):
            self._failed(
                "PC capture uses this PC’s local controller and metadata database."
            )
            return
        root, bucket_id = resource_root(), self.bucket_id
        from .config import AppConfig

        database_path = AppConfig.from_env().database_path
        self._mutate(
            lambda: install_windows_menu(
                root,
                bucket_id=bucket_id,
                state_dir=database_path.parent,
                database_path=database_path,
            ),
            lambda _result: self.feedback.setText(
                "PC menu installed for Internet shortcuts and URL lists. Captures go to this bucket, even with Ateable closed."
            ),
        )
