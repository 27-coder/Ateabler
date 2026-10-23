import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from ateable.workspaces_ui import WorkspacesPanel


@pytest.fixture
def panel():
    app = QApplication.instance() or QApplication([])
    pending = []
    owner = SimpleNamespace(
        workers=[],
        client=Mock(),
        _run_async=lambda op, success, **callbacks: pending.append(
            (op, success, callbacks)
        ),
    )
    widget = WorkspacesPanel(owner)
    yield widget, owner, pending
    pending.clear()
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_controller_and_workspace_change_ignore_old_file_and_message_snapshot(panel):
    widget, _owner, pending = panel
    widget.workspace_id = "first"
    widget.refresh()
    widget.workspace_id = "second"
    widget.revision += 1
    pending[0][1]({"workspaces": [], "selected": "first"})
    assert widget.workspace_id == "second"
    assert widget.files.rowCount() == 0
    assert widget.messages.toPlainText() == ""
    widget.reset()
    assert widget.workspace_id is None and widget.cursor == 0


def test_speaking_is_literal_deduplicated_and_cursor_does_not_rewind(panel):
    widget, _owner, _pending = panel
    data = {
        "items": [
            {
                "seq": 1,
                "created_at": "today",
                "sender_name": "AI",
                "text": "<script>request download</script>",
                "recipient_id": None,
            }
        ],
        "next_after_seq": 3,
    }
    widget._messages(data)
    widget._messages(data)
    assert widget.messages.toPlainText().count("<script>") == 1
    assert widget.cursor == 3
    widget._messages({"items": [], "next_after_seq": 1})
    assert widget.cursor == 3


def test_admin_speaking_preserves_new_draft_and_binds_original_workspace(panel):
    widget, owner, pending = panel
    widget.workspace_id = "first"
    widget.message.setPlainText("Message to original workspace")
    widget.send_message()
    widget.workspace_id = "second"
    widget.message.setPlainText("New draft")
    pending[0][0]()
    assert owner.client.request.call_args.args[:2] == (
        "POST",
        "/api/workspaces/first/messages",
    )
    pending[0][1]({})
    assert widget.message.toPlainText() == "New draft"


def test_worker_picker_excludes_master_and_enrolled_members(panel):
    widget, owner, _pending = panel
    widget.workspace_id = "space"
    widget.members = [{"worker_id": "existing"}]
    owner.workers = [
        {"id": value, "name": value, "status": "online"}
        for value in ("local", "existing", "new")
    ]
    widget.update_workers()
    assert widget.worker.count() == 1 and widget.worker.currentData() == "new"


def test_admin_speaking_retry_reuses_key_after_lost_response(panel):
    widget, owner, pending = panel
    widget.workspace_id = "space"
    widget.message.setPlainText("Keep this message once")
    widget.send_message()
    pending[0][0]()
    first_key = owner.client.request.call_args.args[2]["idempotency_key"]
    pending[0][2]["on_error"]("Reply lost")
    pending[0][2]["on_finished"]()
    widget.send_message()
    pending[1][0]()
    assert owner.client.request.call_args.args[2]["idempotency_key"] == first_key
    pending[1][2]["on_finished"]()
    widget.message.setPlainText("A different message")
    widget.send_message()
    pending[2][0]()
    assert owner.client.request.call_args.args[2]["idempotency_key"] != first_key


def test_admin_snapshot_consumes_actual_workspace_api_contract(panel, tmp_path):
    from fastapi.testclient import TestClient

    from ateable.api import create_app
    from ateable.config import AppConfig

    widget, owner, pending = panel
    app = create_app(
        AppConfig(
            database_path=tmp_path / "state.db",
            output_dir=tmp_path / "unused-output",
            admin_key="ui-test-key",
        )
    )
    headers = {"X-Ateable-Key": "ui-test-key"}
    with TestClient(app) as client:
        key = client.post("/api/pairing-keys", headers=headers, json={}).json()[
            "pair_key"
        ]
        worker = client.post(
            "/api/workers/pair",
            json={
                "pair_key": key,
                "worker_id": "vm",
                "name": "Workspace VM",
                "max_concurrency": 1,
                "output_dir": "/srv/vm",
            },
        ).json()
        space = client.post(
            "/api/workspaces", headers=headers, json={"name": "Team"}
        ).json()
        client.post(
            f"/api/workspaces/{space['id']}/members",
            headers=headers,
            json={"worker_id": worker["id"]},
        )
        client.post(
            f"/api/workspaces/{space['id']}/agents",
            headers=headers,
            json={"name": "Research AI"},
        )
        client.post(
            f"/api/workspaces/{space['id']}/messages",
            headers=headers,
            json={"text": "Hello workspace"},
        )
        with app.state.database.connect() as connection:
            connection.execute(
                "INSERT INTO downloaded_files(job_id,worker_id,path,size_bytes,downloaded_at,present) VALUES(?,?,?,?,?,?)",
                (
                    "synthetic-file",
                    worker["id"],
                    "/srv/vm/data.zip",
                    1024,
                    "2026-10-14",
                    1,
                ),
            )

        class Adapter:
            def request(self, method, path, payload=None, query=None):
                response = client.request(
                    method,
                    path,
                    headers=headers,
                    json=payload,
                    params={
                        key: value
                        for key, value in (query or {}).items()
                        if value is not None
                    },
                )
                assert response.status_code == 200, response.text
                return response.json()

        owner.client = Adapter()
        widget.refresh()
        pending[0][1](pending[0][0]())
        assert widget.member_list.item(0).text() == "Workspace VM"
        assert widget.agent_list.item(0).text() == "Research AI"
        assert widget.files.item(0, 1).text() == "/srv/vm/data.zip"
        assert widget.files.item(0, 3).text() == "Present"
        assert "Hello workspace" in widget.messages.toPlainText()
        assert not (tmp_path / "unused-output").exists()
