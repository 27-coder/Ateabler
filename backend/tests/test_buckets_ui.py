import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from ateable.buckets_ui import BucketsPanel


@pytest.fixture
def panel():
    app = QApplication.instance() or QApplication([])
    pending = []
    owner = SimpleNamespace(
        workers=[],
        client=Mock(),
        _run_async=lambda operation, success, **callbacks: pending.append(
            (operation, success, callbacks)
        ),
        refresh_jobs=Mock(),
    )
    widget = BucketsPanel(owner)
    widget.bucket_id = "inbox"
    widget._update_controls()
    yield widget, owner, pending
    widget.close()
    app.processEvents()


def test_capture_remains_enabled_with_no_worker_and_dispatch_waits(panel):
    widget, owner, pending = panel
    widget.draft.setPlainText(
        "https://example.com/data.zip\ns3://source-bucket/research/"
    )
    assert widget.add_button.isEnabled()
    assert not widget.download_button.isEnabled()
    widget.capture_draft()
    pending[0][0]()
    call = owner.client.request.call_args
    assert call.args[:2] == ("POST", "/api/buckets/inbox/items")
    assert [item["url"] for item in call.args[2]["items"]] == [
        "https://example.com/data.zip",
        "s3://source-bucket/research/",
    ]
    assert "target_worker_id" not in call.args[2]
    widget.draft.setPlainText("https://example.com/new-draft.zip")
    pending[0][1]({"accepted": 2, "duplicates": 0})
    assert widget.draft.toPlainText() == "https://example.com/new-draft.zip"


def test_search_uses_api_query_and_renamed_inbox_is_selected(panel):
    widget, owner, pending = panel
    widget.bucket_id = None
    widget.search.setText("image")
    widget.search_timer.stop()
    owner.client.request.side_effect = [
        {"items": [{"id": "inbox", "name": "Renamed", "total": 0}]},
        {"items": [], "total": 0},
    ]
    widget.refresh()
    result = pending[0][0]()
    assert result["selected"] == "inbox"
    assert owner.client.request.call_args.kwargs["query"]["q"] == "image"


def test_old_bucket_response_cannot_replace_new_selection(panel):
    widget, _owner, pending = panel
    widget.refresh()
    callback = pending[0][1]
    widget.bucket_id = "second"
    widget.revision += 1
    callback(
        {
            "buckets": [{"id": "inbox", "name": "Inbox", "total": 1}],
            "selected": "inbox",
            "page": {"total": 1, "items": [{"source_url": "https://old.example"}]},
        }
    )
    assert widget.bucket_id == "second"
    assert widget.table.rowCount() == 0
    widget.reset()
    assert widget.bucket_id is None


def test_reserved_dispatch_retries_original_run_and_worker_lists_exclude_master(panel):
    widget, owner, pending = panel
    owner.workers = [
        {"id": "local", "status": "online", "capabilities": ["original"]},
        {
            "id": "vm",
            "name": "Worker",
            "status": "online",
            "capabilities": ["original"],
        },
    ]
    widget.update_workers()
    assert widget.worker.findData("local") == -1
    assert widget.download_button.isEnabled()
    widget.active_dispatch = {
        "run_id": "durable-run",
        "options": {"target_worker_id": "original-vm"},
    }
    widget.worker.setCurrentIndex(1)
    widget.dispatch()
    pending[0][0]()
    assert owner.client.request.call_args.args == (
        "POST",
        "/api/buckets/inbox/dispatch-runs/durable-run/retry",
        {},
    )


def test_saved_link_status_is_distinct_from_downloaded_file_and_original_stays_original(
    panel,
):
    widget, owner, pending = panel
    widget.refresh()
    pending[0][1](
        {
            "buckets": [
                {
                    "id": "inbox",
                    "name": "Inbox",
                    "total": 2,
                    "pending": 1,
                    "dispatched": 1,
                }
            ],
            "selected": "inbox",
            "page": {
                "total": 2,
                "items": [
                    {
                        "source_url": "https://example.com/saved.zip",
                        "status": "pending",
                    },
                    {
                        "source_url": "https://example.com/done.zip",
                        "status": "completed",
                    },
                ],
            },
        }
    )
    assert widget.table.item(0, 2).text() == "Saved · not downloaded"
    assert widget.table.item(1, 2).text() == "Downloaded"
    assert "sent to Queue" in widget.summary.text()
    assert widget.format.currentText() == "Keep original file"
    widget.dispatch()
    pending[1][0]()
    assert owner.client.request.call_args.args[2]["format"] == "original"
