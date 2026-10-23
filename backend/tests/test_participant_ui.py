from __future__ import annotations

import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ateable import participant_ui
from ateable.participant import ParticipantError, ParticipantIdentity

pytestmark = pytest.mark.skipif(
    not participant_ui.PYSIDE_AVAILABLE, reason="Optional PySide6 is not installed"
)


class Client:
    identity = ParticipantIdentity(
        "https://controller.test", "vm-a", "worker", "NEVER-DISPLAY-TOKEN"
    )

    def __init__(self):
        self.calls = []
        self.sent = []
        self.requested = []
        self.next_cursor = 12

    def workspaces(self):
        return {
            "items": [{"id": "a", "name": "Alpha"}, {"id": "b", "name": "Beta"}],
            "actor": {"id": "vm-a", "type": "worker", "name": "VM A"},
        }

    def members(self, workspace_id):
        self.calls.append(("members", workspace_id))
        return {
            "items": [{"id": "vm-a", "name": "VM A", "status": "online"}],
            "agents": [{"id": "agent-a", "name": "Analyst", "status": "active"}],
        }

    def files(self, workspace_id, *, q="", limit=200, offset=0):
        self.calls.append(("files", workspace_id, q, limit, offset))
        return {
            "items": [
                {
                    "worker_id": "vm-a",
                    "worker_name": "VM A",
                    "path": f"/srv/{workspace_id}/data.zip",
                    "size_bytes": 42,
                    "present": True,
                }
            ],
            "total": 1,
            "total_bytes": 42,
            "present_count": 1,
        }

    def jobs(self, workspace_id, *, limit=200, offset=0):
        self.calls.append(("jobs", workspace_id, limit, offset))
        return {
            "items": [
                {
                    "title": f"{workspace_id}-job",
                    "status": "queued",
                    "progress": 0,
                    "stage": "queued",
                    "target_worker_id": "vm-a",
                }
            ],
            "total": 1,
        }

    def messages(self, workspace_id, *, after_seq=0):
        self.calls.append(("messages", workspace_id, after_seq))
        return {
            "items": [],
            "next_after_seq": max(self.next_cursor, after_seq),
            "has_more": False,
        }

    def send_message(self, workspace_id, text, **options):
        self.sent.append((workspace_id, text, options))
        return {"id": "synthetic-message"}

    def downloads(self, workspace_id, urls, **options):
        self.requested.append((workspace_id, urls, options))
        return {"jobs": [{"id": "synthetic-job"}]}


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    client = Client()
    result = participant_ui.ParticipantWindow(client, auto_refresh=False)
    tasks = []
    result.thread_pool.start = tasks.append
    result._test_tasks = tasks
    yield result
    result.close()
    app.processEvents()


def run_tasks(window, app):
    # Tasks are captured so completion order can be controlled without network.
    while window._test_tasks:
        batch, window._test_tasks[:] = list(window._test_tasks), []
        for task in batch:
            task.run()
        app.processEvents()


def select_workspaces(window, app):
    window._set_workspaces(window.client.workspaces())
    run_tasks(window, app)


def test_narrow_window_has_no_device_admin_controls_or_token_display(window, app):
    select_workspaces(window, app)
    assert [window.tabs.tabText(index) for index in range(window.tabs.count())] == [
        "Workspaces",
        "Files",
        "Downloads",
        "Speaking",
    ]
    assert not hasattr(window, "devices_table")
    assert not hasattr(window, "pairing_key")
    assert not hasattr(window, "admin_key")
    assert "NEVER-DISPLAY-TOKEN" not in window.identity_label.text()
    assert window.files_table.item(0, 1).text() == "/srv/a/data.zip"
    assert window.files_table.item(0, 3).text() == "Present"
    assert window.download_target.findData("vm-a") >= 0
    assert window.recipient.findData("agent-a") >= 0


def test_workspace_changes_isolate_late_file_job_message_and_member_callbacks(
    window, app
):
    window._set_workspaces(window.client.workspaces())
    old_tasks = list(window._test_tasks)
    window._test_tasks.clear()
    window.message_input.setPlainText("Draft for Alpha")
    window.workspace_selector.setCurrentIndex(window.workspace_selector.findData("b"))
    assert window.workspace_id == "b"
    assert window.message_input.toPlainText() == ""
    for task in old_tasks:
        task.run()
    app.processEvents()
    assert window.files_table.rowCount() == 0
    assert window.jobs_table.rowCount() == 0
    assert window.members_table.rowCount() == 0
    assert "a" not in window._cursors
    run_tasks(window, app)
    assert window.files_table.item(0, 1).text() == "/srv/b/data.zip"
    assert window.jobs_table.item(0, 0).text() == "b-job"
    assert window._cursors == {"b": 12}
    assert window.client.sent == []
    assert window.client.requested == []


def test_new_file_filter_ignores_an_older_result_in_same_workspace(window, app):
    select_workspaces(window, app)
    window.refresh_files(force=True)
    old = window._test_tasks.pop()
    window.file_query.setText("new search")
    window.filter_timer.stop()
    window.refresh_files(force=True)
    current = window._test_tasks.pop()
    current.run()
    app.processEvents()
    window.files_table.item(0, 1).setText("Current filtered result")
    old.run()
    app.processEvents()
    assert window.files_table.item(0, 1).text() == "Current filtered result"


def test_poll_advances_private_message_cursor_without_sending(window, app):
    select_workspaces(window, app)
    window.client.next_cursor = 31
    window.refresh_messages(force=True)
    run_tasks(window, app)
    window.refresh_messages(force=True)
    run_tasks(window, app)
    calls = [call for call in window.client.calls if call[0] == "messages"]
    assert [call[2] for call in calls] == [0, 12, 31]
    assert window._cursors["a"] == 31
    assert window.speaking_log.toPlainText() == ""
    assert window.client.sent == []


def test_reading_speaking_uses_plain_text_and_never_autosends(window, app):
    select_workspaces(window, app)
    message = {
        "id": "m-1",
        "seq": 32,
        "sender_id": "agent-a",
        "sender_name": "Analyst",
        "text": "<img src='https://files.test/payload'>",
        "created_at": "now",
        "recipient_id": None,
    }
    window._set_messages("a", {"items": [message], "next_after_seq": 32})
    window._set_messages("a", {"items": [message], "next_after_seq": 32})
    assert window.speaking_log.toPlainText().count("<img") == 1
    window.message_input.setPlainText("This draft waits for an explicit send")
    app.processEvents()
    assert window.send_button.isEnabled()
    assert window.client.sent == []
    window.send_button.click()
    assert window.client.sent == []  # Only the explicit click queues a request.
    run_tasks(window, app)
    assert len(window.client.sent) == 1
    assert window.client.sent[0][0] == "a"
    assert window.client.sent[0][2]["idempotency_key"]
    assert window.message_input.toPlainText() == ""


def test_download_form_only_requests_remote_job_on_explicit_click(window, app):
    select_workspaces(window, app)
    source = "https://files.test/data.zip?X-Amz-Signature=unchanged%2F"
    window.url_input.setPlainText(source)
    window.output_subdir.setText("datasets")
    window.download_target.setCurrentIndex(window.download_target.findData("vm-a"))
    app.processEvents()
    assert window.client.requested == []
    window.download_button.click()
    run_tasks(window, app)
    assert len(window.client.requested) == 1
    workspace_id, urls, options = window.client.requested[0]
    assert workspace_id == "a" and urls == [source]
    assert (
        options["target_worker_id"] == "vm-a" and options["output_subdir"] == "datasets"
    )
    assert options["idempotency_key"]
    assert window.url_input.toPlainText() == ""


@pytest.mark.parametrize("operation", ["send", "download"])
def test_lost_committed_reply_retries_same_request_without_duplicate_effect(
    window, app, operation
):
    select_workspaces(window, app)
    committed = {}
    attempts = []

    def lost_reply(workspace_id, payload, **options):
        key = (workspace_id, options["idempotency_key"])
        attempts.append(key)
        if key not in committed:
            committed[key] = (
                {"id": "message-1"}
                if operation == "send"
                else {"jobs": [{"id": "job-1"}]}
            )
            raise ParticipantError("Controller is unavailable")
        return committed[key]

    if operation == "send":
        window.client.send_message = lost_reply
        window.message_input.setPlainText("Retry the same scoped message")
        button = window.send_button
    else:
        window.client.downloads = lost_reply
        window.url_input.setPlainText("https://files.test/data.zip?signature=kept")
        window.output_subdir.setText("datasets")
        button = window.download_button
    button.click()
    run_tasks(window, app)
    assert len(committed) == 1
    assert operation in window._pending_writes
    assert button.isEnabled()
    button.click()
    run_tasks(window, app)
    assert attempts[0] == attempts[1]
    assert len(committed) == 1
    assert operation not in window._pending_writes


@pytest.mark.parametrize("changed", ["text", "recipient", "urls", "target", "subdir"])
def test_retry_key_changes_when_any_request_body_field_changes(window, app, changed):
    select_workspaces(window, app)
    attempts = []

    def reject(workspace_id, payload, **options):
        attempts.append((workspace_id, options["idempotency_key"]))
        raise ParticipantError("Controller is unavailable")

    operation = "send" if changed in {"text", "recipient"} else "download"
    if operation == "send":
        window.client.send_message = reject
        window.message_input.setPlainText("First message")
        button = window.send_button
    else:
        window.client.downloads = reject
        window.url_input.setPlainText("https://files.test/first.zip")
        button = window.download_button
    button.click()
    run_tasks(window, app)
    if changed == "text":
        window.message_input.setPlainText("Changed message")
    elif changed == "recipient":
        window.recipient.setCurrentIndex(window.recipient.findData("agent-a"))
    elif changed == "urls":
        window.url_input.setPlainText("https://files.test/changed.zip")
    elif changed == "target":
        window.download_target.setCurrentIndex(window.download_target.findData("vm-a"))
    else:
        window.output_subdir.setText("changed-directory")
    button.click()
    run_tasks(window, app)
    assert len(attempts) == 2
    assert attempts[0][1] != attempts[1][1]


@pytest.mark.parametrize("operation", ["send", "download"])
def test_workspace_change_uses_new_key_and_stale_success_keeps_current_pending_key(
    window, app, operation
):
    select_workspaces(window, app)
    attempts = []

    def successful_old_request(workspace_id, payload, **options):
        attempts.append((workspace_id, options["idempotency_key"]))
        return {} if operation == "send" else {"jobs": []}

    if operation == "send":
        window.client.send_message = successful_old_request
        window.message_input.setPlainText("Identical body")
        button = window.send_button
    else:
        window.client.downloads = successful_old_request
        window.url_input.setPlainText("https://files.test/same.zip")
        button = window.download_button
    button.click()
    old_task = window._test_tasks.pop()
    old_signature, old_key = window._pending_writes[operation]
    window.workspace_selector.setCurrentIndex(window.workspace_selector.findData("b"))
    run_tasks(window, app)

    def lost_new_request(workspace_id, payload, **options):
        attempts.append((workspace_id, options["idempotency_key"]))
        raise ParticipantError("Controller is unavailable")

    if operation == "send":
        window.client.send_message = lost_new_request
        window.message_input.setPlainText("Identical body")
    else:
        window.client.downloads = lost_new_request
        window.url_input.setPlainText("https://files.test/same.zip")
    button.click()
    run_tasks(window, app)
    current_pending = window._pending_writes[operation]
    assert current_pending[1] != old_key
    old_task.signals.success.emit({"jobs": []})
    old_task.signals.finished.emit()
    app.processEvents()
    assert window._pending_writes[operation] == current_pending
    # Also protect confirmation independently of the asynchronous generation guard.
    window._confirm_submission(operation, old_signature, old_key)
    assert window._pending_writes[operation] == current_pending


def test_bounded_download_preview_displays_full_dispatch_count(window, app):
    select_workspaces(window, app)
    window.client.downloads = lambda *_args, **_options: {
        "jobs": [{"id": "preview"}],
        "dispatched": 5000,
        "jobs_total": 5000,
        "jobs_truncated": True,
    }
    window.url_input.setPlainText("https://files.test/data.zip")
    window.download_button.click()
    run_tasks(window, app)
    assert "5000 job(s)" in window.status_label.text()


def test_closed_window_ignores_pending_callbacks(window, app):
    window._set_workspaces(window.client.workspaces())
    tasks = list(window._test_tasks)
    window.close()
    for task in tasks:
        task.run()
    app.processEvents()
    assert window._cursors == {}
    assert window.files_table.rowCount() == 0
    assert not window.poll_timer.isActive()


def test_file_and_job_pagers_are_bounded_and_search_resets_offset(window, app):
    select_workspaces(window, app)
    calls = []

    def files(workspace_id, *, q, limit, offset):
        calls.append(("files", workspace_id, q, limit, offset))
        total = 1 if q else 450
        return {
            "items": [
                {"path": f"/srv/file-{number}", "size_bytes": 1, "worker_id": "vm-a"}
                for number in range(offset, min(offset + limit, total))
            ],
            "total": total,
            "total_bytes": total,
            "present_count": 0,
        }

    def jobs(workspace_id, *, limit, offset):
        calls.append(("jobs", workspace_id, limit, offset))
        return {
            "items": [
                {"title": f"job-{number}", "progress": 0}
                for number in range(offset, min(offset + limit, 450))
            ],
            "total": 450,
        }

    window.client.files = files
    window.client.jobs = jobs
    window.refresh_files(force=True)
    window.refresh_jobs(force=True)
    run_tasks(window, app)
    assert window.files_table.rowCount() == window.jobs_table.rowCount() == 200
    assert "1–200 of 450" in window.files_summary.text()
    assert window.files_next.isEnabled() and window.jobs_next.isEnabled()
    window.files_next.click()
    window.jobs_next.click()
    run_tasks(window, app)
    assert window._file_offset == window._job_offset == 200
    assert "201–400 of 450" in window.jobs_summary.text()
    window.jobs_next.click()
    run_tasks(window, app)
    assert window.jobs_table.rowCount() == 50
    assert not window.jobs_next.isEnabled()
    assert window.jobs_previous.isEnabled()
    window.file_query.setText("a full catalog search")
    window.filter_timer.stop()
    window.refresh_files(force=True)
    run_tasks(window, app)
    assert window._file_offset == 0
    assert window.files_table.rowCount() == 1
    assert not window.files_previous.isEnabled() and not window.files_next.isEnabled()
    assert calls[-1] == ("files", "a", "a full catalog search", 200, 0)
    window.workspace_selector.setCurrentIndex(window.workspace_selector.findData("b"))
    assert window._file_offset == window._job_offset == 0


def test_deleted_qt_signal_object_does_not_raise_when_task_finishes():
    import shiboken6

    calls = []
    task = participant_ui._Task(lambda: calls.append("requested") or {})
    shiboken6.delete(task.signals)
    task.run()
    assert calls == ["requested"]


def test_requests_run_in_background_and_update_on_gui_thread(app):
    client = Client()
    window = participant_ui.ParticipantWindow(client, auto_refresh=False)
    request_threads = []
    callback_threads = []

    def operation():
        request_threads.append(threading.get_ident())
        return "ready"

    window._run_async(
        operation,
        lambda _: callback_threads.append(threading.get_ident()),
        section="background",
        scoped=False,
    )
    deadline = time.monotonic() + 2
    while not callback_threads and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert request_threads and request_threads[0] != threading.get_ident()
    assert callback_threads == [threading.get_ident()]
    window.thread_pool.waitForDone(2000)
    window.close()


def test_first_connection_never_claims_access_file_is_connected(window, app):
    assert "Connected" not in window.status_label.text()
    assert "not checked" in window.identity_label.text()
    assert "vm-a" not in window.identity_label.text()
    assert "vm-a" in window.identity_label.toolTip()
    assert "Start here" in window.next_step_label.text()
    window.refresh_workspaces()
    assert "Connecting" in window.status_label.text()
    assert "Loading" in window.next_step_label.text()
    run_tasks(window, app)
    assert window.identity_label.text() == "VM A · Download worker"
    assert window.status_label.text().startswith("Connected")
    assert "Ready." in window.next_step_label.text()


def test_empty_workspace_assignment_explains_admin_step_and_can_refresh(window, app):
    window.client.workspaces = lambda: {"items": [], "actor": {"name": "VM A"}}
    window.refresh_workspaces()
    run_tasks(window, app)
    assert window.workspace_id is None
    assert "ask your workspace admin" in window.next_step_label.text()
    assert "No workspaces are assigned" in window.workspace_hint.text()
    assert not window.send_button.isEnabled()
    assert not window.download_button.isEnabled()
    assert "Choose an assigned workspace" in window.speaking_log.placeholderText()
    window.client.workspaces = Client().workspaces
    window.poll()
    run_tasks(window, app)
    assert window.workspace_id == "a"
    assert window.identity_label.text() == "VM A · Download worker"


def test_refresh_failure_explains_recovery_and_preserves_draft(window, app):
    select_workspaces(window, app)
    window.message_input.setPlainText("Unsent workspace draft")
    window.url_input.setPlainText("https://example.org/pending.zip")

    def unavailable():
        raise ParticipantError("Controller is unavailable")

    window.client.workspaces = unavailable
    window.refresh_workspaces()
    run_tasks(window, app)
    assert "Controller is unavailable" in window.status_label.text()
    assert (
        "Check the controller connection or access file"
        in window.next_step_label.text()
    )
    assert "Your drafts are kept" in window.next_step_label.text()
    assert window.message_input.toPlainText() == "Unsent workspace draft"
    assert window.url_input.toPlainText() == "https://example.org/pending.zip"
    assert not window._connected
    window.client.workspaces = Client().workspaces
    window.poll()
    run_tasks(window, app)
    assert window._connected
    assert window.message_input.toPlainText() == "Unsent workspace draft"


def test_selected_workspace_read_failure_triggers_connection_recovery(window, app):
    select_workspaces(window, app)
    window.client.files = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        ParticipantError("Controller is unavailable")
    )
    window.refresh_files(force=True)
    run_tasks(window, app)
    assert not window._connected
    assert "Refresh" in window.next_step_label.text()


def test_empty_data_explains_where_downloads_and_messages_will_appear(window, app):
    select_workspaces(window, app)
    window._set_files({"items": [], "total": 0})
    window._set_jobs({"items": [], "total": 0})
    assert "No completed downloads yet" in window.files_hint.text()
    assert "Open Downloads" in window.files_hint.text()
    assert "Paste links above" in window.jobs_hint.text()
    assert "No messages yet" in window.speaking_log.placeholderText()
    assert window.speaking_log.toPlainText() == ""
    window.file_query.setText("not-found")
    window.filter_timer.stop()
    window._set_files({"items": [], "total": 0})
    assert "No files match this search" in window.files_hint.text()
    assert "clear the search" in window.files_hint.text()


def test_workspace_without_workers_guides_admin_assignment_and_keeps_chat(window, app):
    select_workspaces(window, app)
    window._set_members({"items": [], "agents": [{"id": "agent-a", "name": "Analyst"}]})
    assert "ready for chat" in window.next_step_label.text()
    assert "admin to add a download worker" in window.next_step_label.text()
    assert "0 download worker(s)" in window.roster_hint.text()
    window.message_input.setPlainText("Chat remains available")
    assert window.send_button.isEnabled()


def test_friendly_names_hide_ids_and_workspace_rows_switch_selection(window, app):
    select_workspaces(window, app)
    assert window.workspaces_table.isColumnHidden(1)
    assert window.members_table.isColumnHidden(3)
    assert "vm-a" in window.members_table.item(0, 0).toolTip()
    assert window.jobs_table.item(0, 4).text() == "VM A"
    assert (
        window.download_target.itemText(window.download_target.findData("vm-a"))
        == "VM A · online"
    )
    assert window.recipient.itemText(0) == "Everyone in this workspace"
    assert window.recipient.itemData(0) is None
    window.workspaces_table.cellClicked.emit(1, 0)
    assert window.workspace_id == "b"
    run_tasks(window, app)
    assert window.files_table.item(0, 1).text() == "/srv/b/data.zip"


def test_private_chat_uses_friendly_recipient_name_without_rendering_markup(
    window, app
):
    select_workspaces(window, app)
    window._set_messages(
        "a",
        {
            "items": [
                {
                    "id": "private-1",
                    "recipient_id": "agent-a",
                    "sender_name": "VM A",
                    "text": "<b>Plain message</b>",
                }
            ],
            "next_after_seq": 1,
        },
    )
    assert "→ Analyst (directed)" in window.speaking_log.toPlainText()
    assert "<b>Plain message</b>" in window.speaking_log.toPlainText()


def test_first_launch_access_file_cancel_exits_without_loading_identity(
    app, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        participant_ui,
        "default_session_file",
        lambda: tmp_path / "missing-session.json",
    )
    monkeypatch.setattr(
        participant_ui,
        "default_credentials_file",
        lambda: tmp_path / "missing-worker.json",
    )
    monkeypatch.setattr(participant_ui, "_choose_access_file", lambda: None)
    monkeypatch.setattr(
        participant_ui,
        "load_identity",
        lambda **_kwargs: pytest.fail("Cancel must not load credentials"),
    )
    assert participant_ui.main([]) == 0


def test_access_file_welcome_explains_narrow_role_and_cancel(app, monkeypatch):
    from PySide6.QtWidgets import QDialog, QLabel

    seen = []

    def cancel(dialog):
        seen.append(
            (
                dialog.windowTitle(),
                " ".join(label.text() for label in dialog.findChildren(QLabel)),
            )
        )
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QDialog, "exec", cancel)
    assert participant_ui._choose_access_file() is None
    assert seen[0][0] == "Welcome to Ateable Node"
    assert "Join your workspace" in seen[0][1]
    assert "Ask your Ateable admin" in seen[0][1]
    assert "Keep it private" in seen[0][1]


def test_all_offline_workers_keep_chat_available_and_download_draft_until_online(
    window, app
):
    select_workspaces(window, app)
    window.url_input.setPlainText("https://example.org/dataset.zip")
    window.message_input.setPlainText("Can someone reconnect the worker?")
    window._set_members(
        {"items": [{"id": "vm-a", "name": "VM A", "status": "offline"}], "agents": []}
    )
    assert "No download workers are online" in window.next_step_label.text()
    assert "reconnect a worker" in window.next_step_label.text()
    assert "0 online" in window.roster_hint.text()
    assert "Ready." not in window.next_step_label.text()
    assert not window.download_button.isEnabled()
    assert window.send_button.isEnabled()
    assert window.url_input.toPlainText() == "https://example.org/dataset.zip"
    window._set_members(window.client.members("a"))
    assert "Ready." in window.next_step_label.text()
    assert window.download_button.isEnabled()
    assert window.message_input.toPlainText() == "Can someone reconnect the worker?"
    assert window.client.requested == []
