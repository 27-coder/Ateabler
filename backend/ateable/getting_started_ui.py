"""A small, state-driven guide for Ateable's Admin desktop."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NextStep:
    title: str
    detail: str
    action: str
    target: str
    enabled: bool = True


def next_step(
    connection: str,
    *,
    online_computers: int,
    known_computers: int,
    workers_verified: bool,
    jobs_total: int,
) -> NextStep:
    """Choose a useful next action without assuming an old snapshot is online."""
    if connection == "error":
        return NextStep(
            "Controller unavailable",
            "Your saved links are kept. Restore the controller connection before saving new links or starting downloads.",
            "Check connection",
            "settings",
        )
    if connection != "online":
        return NextStep(
            "Connecting to your controller…",
            "Your saved links are kept while Ateable connects.",
            "Connecting…",
            "retry",
            False,
        )
    if not workers_verified:
        return NextStep(
            "Checking download computers…",
            "The controller is ready. Checking which computers can receive downloads.",
            "Refresh computers",
            "retry",
        )
    if not online_computers:
        if known_computers:
            return NextStep(
                "Bring a download computer online",
                "Your paired computers are offline. Reconnect one in Devices, or connect another computer.",
                "Open Devices",
                "devices",
            )
        return NextStep(
            "Connect your first download computer",
            "Connect a Linux computer over SSH using its normal or private-network address. Ateable installs its download worker.",
            "Connect a computer…",
            "connect",
        )
    count = f"{online_computers:,} download computer{'s' if online_computers != 1 else ''} online"
    if jobs_total:
        return NextStep(
            "Your download computers are ready",
            f"{count}. Open the Queue to follow progress or start another download.",
            "View Queue",
            "queue",
        )
    return NextStep(
        "Ready for your first download",
        f"{count}. Paste a link in Queue, or collect several links in a Bucket first.",
        "Add download links",
        "download",
    )


try:
    from PySide6.QtCore import Qt, Signal
    from PySide6.QtWidgets import (
        QFrame,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QScrollArea,
        QVBoxLayout,
        QWidget,
    )
except ImportError:
    pass
else:

    class GettingStartedPanel(QScrollArea):
        navigate = Signal(str)

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setObjectName("gettingStarted")
            self.setWidgetResizable(True)
            self.setFrameShape(QFrame.Shape.NoFrame)
            content = QWidget()
            layout = QVBoxLayout(content)
            layout.setContentsMargins(24, 22, 24, 22)
            layout.setSpacing(15)

            title = QLabel("Bulk data downloads across your computers.")
            title.setObjectName("startTitle")
            title.setWordWrap(True)
            layout.addWidget(title)
            explanation = QLabel(
                "Collect datasets, files, and media on your chosen download computers. "
                "Admin coordinates the jobs; each download computer uses its own disk and internet connection."
            )
            explanation.setObjectName("startIntro")
            explanation.setWordWrap(True)
            layout.addWidget(explanation)

            readiness = QFrame()
            readiness.setObjectName("startReadiness")
            ready_layout = QVBoxLayout(readiness)
            ready_layout.setContentsMargins(18, 15, 18, 15)
            ready_layout.setSpacing(8)
            self.status_title = QLabel()
            self.status_title.setObjectName("sectionTitle")
            self.status_title.setWordWrap(True)
            self.status_detail = QLabel()
            self.status_detail.setWordWrap(True)
            self.status_detail.setProperty("muted", True)
            self.next_button = QPushButton()
            self.next_button.setProperty("primary", True)
            self.next_button.clicked.connect(lambda: self.navigate.emit(self._target))
            ready_layout.addWidget(self.status_title)
            ready_layout.addWidget(self.status_detail)
            ready_layout.addWidget(
                self.next_button, alignment=Qt.AlignmentFlag.AlignLeft
            )
            layout.addWidget(readiness)

            self.connect_button = self._step(
                layout,
                "1",
                "Connect a download computer",
                "Use SSH over your LAN or an existing private network. Devices shows connected computers and their Ateable file logs.",
                "Connect a computer…",
                "connect",
            )
            self.buckets_button = self._step(
                layout,
                "2",
                "Collect data links in a Bucket",
                "A Bucket is a saved list of links. Start with Inbox, paste links, or use the browser's ‘Ate it on bucket’ action. Save now and download later.",
                "Open Buckets",
                "buckets",
            )
            self.queue_button = self._step(
                layout,
                "3",
                "Run batches and monitor downloads",
                "Choose a download computer and start the saved links from Buckets. Queue shows progress, failures, and completed downloads.",
                "Open Queue",
                "queue",
            )
            note = QLabel(
                "Run batches manually here, or use the workspace-scoped Node CLI from scripts and AI tools. "
                "Workspaces share download logs and Speaking messages. Admin manages access."
            )
            note.setWordWrap(True)
            note.setProperty("muted", True)
            layout.addWidget(note)
            spaces = QPushButton("Open Workspaces")
            spaces.setProperty("quiet", True)
            spaces.clicked.connect(lambda: self.navigate.emit("workspaces"))
            layout.addWidget(spaces, alignment=Qt.AlignmentFlag.AlignLeft)
            layout.addStretch(1)
            self.setWidget(content)
            self.update_status("connecting", [], workers_verified=False, jobs_total=0)

        def _step(
            self,
            outer: QVBoxLayout,
            number: str,
            title: str,
            detail: str,
            button_text: str,
            target: str,
        ) -> QPushButton:
            card = QFrame()
            card.setObjectName("startStep")
            row = QHBoxLayout(card)
            row.setContentsMargins(16, 12, 16, 12)
            row.setSpacing(15)
            label = QLabel(number)
            label.setObjectName("startNumber")
            label.setFixedWidth(25)
            row.addWidget(label)
            copy = QVBoxLayout()
            copy.setSpacing(3)
            heading = QLabel(title)
            heading.setObjectName("startStepTitle")
            heading.setWordWrap(True)
            body = QLabel(detail)
            body.setProperty("muted", True)
            body.setWordWrap(True)
            copy.addWidget(heading)
            copy.addWidget(body)
            row.addLayout(copy, 1)
            button = QPushButton(button_text)
            button.clicked.connect(lambda: self.navigate.emit(target))
            row.addWidget(button)
            outer.addWidget(card)
            return button

        def update_status(
            self,
            connection: str,
            workers: list[dict],
            *,
            workers_verified: bool,
            jobs_total: int,
        ) -> None:
            remote = [w for w in workers if w.get("id") not in {None, "local"}]
            online = sum(w.get("status") == "online" for w in remote)
            step = next_step(
                connection,
                online_computers=online,
                known_computers=len(remote),
                workers_verified=workers_verified,
                jobs_total=jobs_total,
            )
            self._target = step.target
            self.status_title.setText(step.title)
            self.status_detail.setText(step.detail)
            self.next_button.setText(step.action)
            self.next_button.setEnabled(step.enabled)
            self.connect_button.setEnabled(connection == "online")
            self.connect_button.setToolTip(
                "Connect a Linux computer using SSH"
                if connection == "online"
                else "The controller must be online before pairing a computer"
            )
