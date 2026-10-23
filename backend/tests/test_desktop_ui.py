from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ateable import desktop


class MemorySettings:
    def __init__(self, *_args, **_kwargs):
        self.values: dict[str, object] = {}

    def value(self, key, default=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value

    def remove(self, key):
        self.values.pop(key, None)

    def sync(self):
        return None


@unittest.skipUnless(desktop.PYSIDE_AVAILABLE, "PySide6 is not installed")
class DesktopWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")

    def setUp(self):
        self.config_scratch = tempfile.TemporaryDirectory()
        device = desktop.DeviceConfig.defaults().updated(
            controller_url="https://controller.example.test",
            admin_key="test-key",
            network_mode="direct",
        )
        self.patches = (
            patch.dict(os.environ, {"ATEABLE_CONFIG_DIR": self.config_scratch.name}),
            patch.object(desktop, "QSettings", MemorySettings),
            patch.object(desktop.DeviceConfig, "load", return_value=device),
            patch.object(desktop.DeviceConfig, "save", return_value=None),
            patch.object(desktop.AteableWindow, "refresh_all", autospec=True),
        )
        for item in self.patches:
            item.start()
        self.window = desktop.AteableWindow()
        self.window.workers = [
            {
                "id": "remote",
                "name": "Remote",
                "status": "online",
                "capabilities": list(desktop.SUPPORTED_FORMATS),
                "max_concurrency": 1,
            }
        ]
        self.window.poll_timer.stop()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.config_scratch.cleanup()

    def test_semantic_controls_and_shortcuts_are_present(self):
        self.assertEqual(self.window.tabs.count(), 6)
        self.assertEqual(self.window.url_input.accessibleName(), "Download URLs")
        self.assertIsNotNone(self.window.url_input)
        self.assertFalse(self.window.enqueue_button.isEnabled())
        self.assertEqual(self.window.jobs_table.columnCount(), 7)
        self.assertEqual(self.window.devices_table.columnCount(), 5)
        self.assertEqual(self.window.pairing_key.echoMode().name, "Password")
        self.assertEqual(len(self.window._shortcuts), 10)

    def test_fresh_install_starts_with_guide_and_plain_language_choices(self):
        self.assertIs(self.window.tabs.currentWidget(), self.window.start_tab)
        self.assertEqual(self.window.format_combo.currentText(), "Keep original file")
        self.assertEqual(self.window.format_combo.currentData(), "original")
        self.assertEqual(self.window.enqueue_button.text(), "&Start downloads")
        self.assertFalse(self.window.manual_pairing_panel.isVisible())
        self.window.manual_pairing_toggle.setChecked(True)
        self.assertFalse(self.window.manual_pairing_panel.isHidden())

    def test_saved_tab_migration_preserves_existing_destinations(self):
        settings = self.window.local_settings
        names = ("queue", "buckets", "workspaces", "devices", "settings")
        for index, name in enumerate(names):
            settings.setValue("active_tab", index)
            self.assertIs(self.window._saved_tab(), self.window._tab_names[name])
        settings.setValue("active_tab_name", "start")
        self.assertIs(self.window._saved_tab(), self.window.start_tab)
        settings.setValue("active_tab_name", "missing")
        settings.setValue("active_tab", "bad")
        self.assertIs(self.window._saved_tab(), self.window.start_tab)

    def test_offline_guide_blocks_pairing_and_keeps_buckets_accessible(self):
        self.window._set_connection_state("error", "Connection refused")
        self.assertEqual(
            self.window.start_tab.status_title.text(), "Controller unavailable"
        )
        self.assertFalse(self.window.start_tab.connect_button.isEnabled())
        self.assertFalse(self.window.ssh_pair_button.isEnabled())
        self.assertTrue(self.window.start_tab.buckets_button.isEnabled())
        with patch.object(self.window, "show_ssh_pair_dialog") as pair:
            self.window._start_action("connect")
        pair.assert_not_called()
        with (
            patch.object(self.window.buckets_tab, "refresh"),
            patch.object(self.window, "refresh_devices"),
        ):
            self.window.start_tab.buckets_button.click()
        self.assertIs(self.window.tabs.currentWidget(), self.window.buckets_tab)

    def test_guide_requires_fresh_worker_snapshot_after_connection_change(self):
        self.window._set_connection_state("online")
        self.assertIn("Checking", self.window.start_tab.status_title.text())
        self.window._populate_devices({"workers": {"items": []}})
        self.assertIn(
            "first download computer", self.window.start_tab.status_title.text()
        )
        with patch.object(self.window, "show_ssh_pair_dialog") as pair:
            self.window.start_tab.next_button.click()
        pair.assert_called_once()
        worker = {"id": "vm", "status": "online", "name": "VM"}
        self.window._populate_devices({"workers": {"items": [worker]}})
        self.assertEqual(
            self.window.start_tab.status_title.text(), "Ready for your first download"
        )
        self.window._invalidate_connection_tasks()
        self.assertFalse(self.window._workers_verified)
        self.window._set_connection_state("online")
        self.assertIn("Checking", self.window.start_tab.status_title.text())

    def test_tab_polling_uses_widgets_and_start_refreshes_devices(self):
        with (
            patch.object(self.window, "refresh_devices") as devices,
            patch.object(self.window, "refresh_jobs") as jobs,
        ):
            self.window._poll_current_tab()
            devices.assert_called_once()
            jobs.assert_not_called()
            self.window.tabs.setCurrentWidget(self.window.queue_tab)
            jobs.reset_mock()
            self.window._poll_current_tab()
            jobs.assert_called_once()

    def test_ssh_dialog_collapses_advanced_options_and_keeps_saved_values(self):
        from ateable.ssh_devices import SshDeviceSpec, SshDeviceStore

        self.window._set_connection_state("online")
        saved = SshDeviceSpec(
            "saved-vm",
            user="download",
            port=2222,
            remote_dir="/opt/ateable",
            output_dir="/data/downloads",
            s3_endpoint="https://objects.example.test",
            s3_profile="archive",
            concurrency=3,
        )

        def inspect_dialog(dialog):
            panel = dialog.findChild(desktop.QWidget, "sshAdvancedOptions")
            self.assertTrue(panel.isHidden())
            preset = dialog.findChild(desktop.QComboBox, "sshPreset")
            preset.setCurrentIndex(1)
            self.assertTrue(panel.isHidden())
            self.assertEqual(
                dialog.findChild(desktop.QLineEdit, "ssh_host").text(), "saved-vm"
            )
            toggle = dialog.findChild(desktop.QToolButton, "sshAdvancedToggle")
            toggle.setChecked(True)
            self.assertFalse(panel.isHidden())
            toggle.setChecked(False)
            return desktop.QDialog.DialogCode.Accepted

        with (
            patch.object(SshDeviceStore, "load", return_value={"Saved": saved}),
            patch.object(desktop.QDialog, "exec", inspect_dialog),
            patch.object(self.window, "connect_ssh_worker") as connect,
        ):
            self.window.show_ssh_pair_dialog()
        spec = connect.call_args.args[0]
        self.assertEqual(spec.port, 2222)
        self.assertEqual(spec.remote_dir, "/opt/ateable")
        self.assertEqual(spec.s3_endpoint, "https://objects.example.test")
        self.assertEqual(spec.s3_profile, "archive")
        self.assertEqual(spec.concurrency, 3)
        self.assertFalse(connect.call_args.kwargs["install"])

    def test_named_tab_is_saved_when_window_closes(self):
        with patch.object(self.window, "refresh_devices"):
            self.window.tabs.setCurrentWidget(self.window.devices_tab)
        self.window.close()
        self.assertEqual(self.window.local_settings.value("active_tab_name"), "devices")

    def test_url_feedback_controls_enqueue_enablement(self):
        self.window.url_input.setPlainText("not-a-url")
        self.app.processEvents()
        self.assertFalse(self.window.enqueue_button.isEnabled())
        self.assertIn("invalid", self.window.url_count.text())

        self.window.url_input.setPlainText("https://youtu.be/allowed")
        self.app.processEvents()
        self.assertTrue(self.window.enqueue_button.isEnabled())
        self.assertEqual(self.window.url_count.text(), "1 valid URLs")

    def test_enqueue_requires_remote_worker_and_never_inherits_master_proxy(self):
        self.window.url_input.setPlainText("https://example.com/archive.zip")
        self.window.workers = [
            {"id": "local", "status": "online", "capabilities": ["original"]}
        ]
        with (
            patch.object(desktop.QMessageBox, "warning") as warning,
            patch.object(self.window, "refresh_devices"),
            patch.object(self.window, "_run_async") as submit,
        ):
            self.window.enqueue_urls()
        warning.assert_called_once()
        submit.assert_not_called()
        self.assertIs(self.window.tabs.currentWidget(), self.window.devices_tab)

        self.window.workers = [
            {"id": "remote", "status": "online", "capabilities": ["original"]}
        ]
        self.window.active_network_mode = "tor"
        submitted = []
        self.window._run_async = lambda operation, success, **callbacks: (
            submitted.append(operation)
        )
        with patch.object(
            self.window.client, "create_batch", return_value={}
        ) as create:
            self.window.enqueue_urls()
            submitted[0]()
        self.assertEqual(create.call_args.kwargs["output_format"], "original")
        self.assertIsNone(create.call_args.kwargs["proxy"])
        self.assertIsNone(create.call_args.kwargs["target_worker_id"])

    def test_ssh_pair_uses_fresh_key_after_install_and_closes_tunnel(self):
        from ateable.ssh_devices import SshDeviceSpec

        self.window._set_connection_state("online")
        events = []
        spec = SshDeviceSpec("worker-alias")
        handle = type("Handle", (), {"worker_id": "remote"})()

        class Provisioner:
            def provision(self, value):
                self.spec = value
                events.append("install")

            def connect(self, value, controller_url, pair_key=None):
                self.key = pair_key
                events.append("connect")
                return handle

            def stop_all(self):
                events.append("stop")

        provisioner = Provisioner()
        self.window._ssh_provisioner = provisioner
        self.window._run_async = lambda operation, success, **callbacks: (
            success(operation()),
            callbacks["on_finished"](),
        )

        def pair():
            events.append("pair")
            return {"pair_key": "one-use-invitation"}

        with (
            patch.object(self.window.client, "create_pairing_key", side_effect=pair),
            patch.object(self.window, "refresh_devices"),
        ):
            self.window.connect_ssh_worker(spec)
        self.assertEqual(events, ["install", "pair", "connect"])
        self.assertIs(provisioner.spec, spec)
        self.assertEqual(provisioner.key, "one-use-invitation")
        self.assertTrue(self.window.ssh_pair_button.isEnabled())
        self.window.shutdown_controller()
        self.window.shutdown_controller()
        self.assertEqual(events.count("stop"), 1)

    def test_storage_response_from_previous_selection_is_ignored(self):
        workers = [
            {"id": name, "name": name, "status": "online"}
            for name in ("first", "second")
        ]
        self.window._populate_devices({"workers": {"items": workers}})
        pending = []
        self.window._run_async = lambda operation, success, **callbacks: pending.append(
            (success, callbacks)
        )
        self.window.devices_table.selectRow(0)
        self.window.devices_table.selectRow(1)
        payload = {
            "inventory": {"output_dir": "/remote/downloads", "disk_free_bytes": 1024},
            "total_downloaded_bytes": 512,
            "completed_files": 1,
            "files": [
                {
                    "path": "/remote/downloads/file.zip",
                    "size_bytes": 512,
                    "exists": True,
                }
            ],
        }
        pending[0][0](payload)
        self.assertEqual(self.window.worker_files.rowCount(), 0)
        pending[1][0](payload)
        self.assertEqual(self.window.worker_files.rowCount(), 1)
        self.assertIn("1.0 KiB free", self.window.worker_storage_summary.text())
        self.assertEqual(
            self.window.worker_files.item(0, 0).text(), "/remote/downloads/file.zip"
        )

    def test_queue_filter_and_selection_survive_refresh(self):
        self.window.jobs = [
            {
                "id": "job-a",
                "title": "Alpha lecture",
                "source_url": "https://youtu.be/a",
                "format": "wav",
                "status": "downloading",
                "progress": 42,
                "updated_at": "2026-08-24T00:00:00+00:00",
            },
            {
                "id": "job-b",
                "title": "Beta lecture",
                "source_url": "https://youtu.be/b",
                "format": "mp3",
                "status": "failed",
                "progress": 10,
                "updated_at": "2026-08-24T00:00:00+00:00",
            },
        ]
        self.window._jobs_total = 2
        self.window._update_job_stats()
        self.window._apply_job_filters()
        self.window.jobs_table.selectRow(0)
        self.assertEqual(self.window._selected_job_ids(), ["job-a"])

        self.window.jobs[0]["progress"] = 55
        self.window._apply_job_filters()
        self.assertEqual(self.window._selected_job_ids(), ["job-a"])

        failed_index = self.window.job_status_filter.findData("failed")
        self.window.job_status_filter.setCurrentIndex(failed_index)
        self.assertEqual(self.window.jobs_table.rowCount(), 1)
        self.assertEqual(
            self.window.jobs_table.item(0, 0).data(desktop.Qt.ItemDataRole.UserRole),
            "job-b",
        )

    def test_enqueue_is_single_flight(self):
        submitted = []

        def hold_task(operation, _on_success, **_kwargs):
            submitted.append(operation)

        self.window._run_async = hold_task
        self.window.url_input.setPlainText("https://youtu.be/allowed")
        self.window.enqueue_urls()
        self.window.enqueue_urls()

        self.assertEqual(len(submitted), 1)
        self.assertTrue(self.window._enqueue_in_flight)
        self.assertFalse(self.window.enqueue_button.isEnabled())

        self.window._enqueue_finished()
        self.assertFalse(self.window._enqueue_in_flight)
        self.assertTrue(self.window.enqueue_button.isEnabled())

    def test_import_appends_to_draft_without_enqueuing_and_is_undoable(self):
        original = "https://youtu.be/original1"
        self.window.url_input.setPlainText(original)
        self.window.output_subdir.setText("keep-this-folder")
        self.window.format_combo.setCurrentIndex(
            self.window.format_combo.findData("flac")
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "links.txt"
            path.write_text(
                "# Imported list\nhttps://youtu.be/imported1\nhttps://youtu.be/original1\n",
                encoding="utf-8",
            )
            with (
                patch.object(
                    desktop.QFileDialog, "getOpenFileName", return_value=(str(path), "")
                ),
                patch.object(self.window, "refresh_jobs"),
                patch.object(self.window, "_run_async") as submit,
            ):
                self.window.import_url_list()
                submit.assert_not_called()
        self.assertEqual(
            len(desktop.parse_urls(self.window.url_input.toPlainText()).urls), 2
        )
        self.assertIn("1 duplicates", self.window.url_count.text())
        self.assertEqual(self.window.output_subdir.text(), "keep-this-folder")
        self.assertEqual(self.window.format_combo.currentData(), "flac")
        self.assertFalse(self.window.batch_group.isHidden())
        self.window.url_input.undo()
        self.assertEqual(self.window.url_input.toPlainText(), original)

    def test_failed_import_preserves_existing_draft(self):
        original = "https://youtu.be/original1"
        self.window.url_input.setPlainText(original)
        with (
            patch.object(
                desktop.QFileDialog, "getOpenFileName", return_value=("missing.txt", "")
            ),
            patch.object(desktop.QMessageBox, "warning") as warning,
        ):
            self.window.import_url_list()
        warning.assert_called_once()
        self.assertEqual(self.window.url_input.toPlainText(), original)

    def test_import_refuses_combined_draft_over_batch_limit(self):
        original = "\n".join(f"https://youtu.be/video-{i}" for i in range(5000))
        self.window.url_input.setPlainText(original)
        with (
            patch.object(
                desktop.QFileDialog, "getOpenFileName", return_value=("links.txt", "")
            ),
            patch.object(
                desktop, "read_url_list", return_value="https://youtu.be/extra001"
            ),
            patch.object(desktop.QMessageBox, "warning") as warning,
        ):
            self.window.import_url_list()
        warning.assert_called_once()
        self.assertIn("5,000", warning.call_args.args[2])
        self.assertEqual(self.window.url_input.toPlainText(), original)

    def test_late_enqueue_success_does_not_erase_new_draft(self):
        pending = []
        self.window._run_async = lambda operation, success, **callbacks: pending.append(
            (success, callbacks)
        )
        self.window.url_input.setPlainText("https://youtu.be/original1")
        self.window.enqueue_urls()
        self.window.url_input.setPlainText("https://youtu.be/nextdraft")
        success, callbacks = pending[0]
        with patch.object(self.window, "refresh_jobs"):
            success({"batch": {"total": 1}})
        callbacks["on_finished"]()
        self.assertEqual(
            self.window.url_input.toPlainText(), "https://youtu.be/nextdraft"
        )
        self.assertIn("draft was kept", self.window.statusBar().currentMessage())

    def test_dirty_settings_are_not_overwritten_by_polling(self):
        self.window._settings_loaded_once = True
        self.window._mark_settings_dirty("downloads")
        with patch.object(self.window, "_run_async") as run_async:
            self.window.refresh_settings()
            run_async.assert_not_called()

            self.window.refresh_settings(force=True)
            run_async.assert_called_once()

    def test_settings_response_started_before_edit_is_ignored(self):
        original = self.window.output_folder.text()
        request_revision = self.window._settings_revisions["downloads"]
        self.window.output_folder.setText("/new/local/edit")
        self.window._mark_settings_dirty("downloads")

        applied = self.window._settings_loaded(
            {
                "output_dir": "/stale/controller/value",
                "default_format": "mp3",
                "max_concurrent_downloads": 8,
                "worker_timeout_seconds": 90,
            },
            expected_revision=request_revision,
        )

        self.assertFalse(applied)
        self.assertNotEqual(self.window.output_folder.text(), original)
        self.assertEqual(self.window.output_folder.text(), "/new/local/edit")
        self.assertIn("downloads", self.window._dirty_sections)

    def test_server_stats_drive_global_queue_cards_and_actions(self):
        class SnapshotClient:
            base_url = "https://controller.example.test"

            def list_jobs(self, **_kwargs):
                return {
                    "items": [
                        {
                            "id": "newest",
                            "status": "completed",
                            "source_url": "https://youtu.be/newest",
                        }
                    ],
                    "total": 1000,
                }

            def stats(self):
                return {
                    "total": 1000,
                    "queued": 700,
                    "downloading": 100,
                    "paused": 50,
                    "completed": 100,
                    "failed": 40,
                    "cancelled": 10,
                }

        def immediate(operation, on_success, **kwargs):
            on_success(operation())
            finished = kwargs.get("on_finished")
            if finished:
                finished()

        self.window.client = SnapshotClient()
        self.window._run_async = immediate
        self.window.refresh_jobs()

        self.assertEqual(self.window.total_jobs_value.text(), "1,000")
        self.assertEqual(self.window.active_jobs_value.text(), "800")
        self.assertEqual(self.window.completed_jobs_value.text(), "100")
        self.assertEqual(self.window.attention_jobs_value.text(), "90")
        self.assertTrue(self.window.pause_active_action.isEnabled())
        self.assertTrue(self.window.resume_paused_action.isEnabled())
        self.assertTrue(self.window.clear_completed_action.isEnabled())

    def test_old_controller_callbacks_are_ignored_after_switch(self):
        queued = []

        class HoldingPool:
            def start(self, task):
                queued.append(task)

        succeeded = []
        finished = []
        self.window.thread_pool = HoldingPool()
        self.window._run_async(
            lambda: "old controller",
            succeeded.append,
            on_finished=lambda: finished.append(True),
        )
        self.window._connection_generation += 1
        queued[0].run()
        self.app.processEvents()

        self.assertEqual(succeeded, [])
        self.assertEqual(finished, [])

    def _use_paged_queue(self, jobs):
        class PagedClient:
            base_url = "https://controller.example.test"

            def __init__(self):
                self.calls = []

            def list_jobs(self, *, limit=250, offset=0, status=None, query=None):
                self.calls.append((offset, status, query))
                matches = desktop.filter_jobs(
                    jobs, status=status or "all", query=query or ""
                )
                return {
                    "items": matches[offset : offset + limit],
                    "total": len(matches),
                    "filters_applied": True,
                }

            def stats(self):
                return desktop.job_status_counts(jobs)

        def immediate(operation, on_success, **kwargs):
            on_success(operation())
            kwargs["on_finished"]()

        client = PagedClient()
        self.window.client = client
        self.window._run_async = immediate
        return client

    def test_queue_pages_and_search_cover_jobs_beyond_first_page(self):
        jobs = [
            {
                "id": f"job-{i}",
                "title": "Older target" if i == 504 else f"Video {i}",
                "status": "failed" if i == 504 else "queued",
                "format": "wav",
            }
            for i in range(505)
        ]
        client = self._use_paged_queue(jobs)
        self.window.refresh_jobs()
        self.assertEqual(self.window.jobs_table.rowCount(), 250)
        self.assertEqual(self.window.queue_page_label.text(), "Page 1 of 3")
        self.assertFalse(self.window.previous_jobs_button.isEnabled())
        self.window.next_jobs_button.click()
        self.assertEqual(self.window._jobs_offset, 250)
        self.assertEqual(self.window.jobs[0]["id"], "job-250")
        self.assertTrue(self.window.previous_jobs_button.isEnabled())
        self.window.job_search.setText("Older target")
        self.window.refresh_jobs()
        self.assertEqual(client.calls[-1], (0, None, "Older target"))
        self.assertEqual([job["id"] for job in self.window.jobs], ["job-504"])
        self.assertEqual(self.window.queue_summary.text(), "1–1 of 1 matches")
        self.assertEqual(self.window.total_jobs_value.text(), "505")
        self.assertFalse(self.window.next_jobs_button.isEnabled())

    def test_empty_search_keeps_filters_visible_and_clearable(self):
        self._use_paged_queue([{"id": "one", "title": "First", "status": "queued"}])
        self.window.job_search.setText("not present")
        self.window.refresh_jobs()
        self.assertEqual(self.window.jobs_table.rowCount(), 0)
        self.assertFalse(self.window.queue_filter_bar.isHidden())
        self.assertEqual(self.window.jobs_empty_title.text(), "No matching jobs")
        self.window.job_search.clear()
        self.window.refresh_jobs()
        self.assertEqual(self.window.jobs_table.rowCount(), 1)

    def test_queue_shrinking_moves_back_to_valid_page(self):
        jobs = [{"id": f"job-{i}", "status": "completed"} for i in range(251)]
        self._use_paged_queue(jobs)
        self.window.refresh_jobs()
        self.window.next_jobs_button.click()
        self.assertEqual(self.window._jobs_offset, 250)
        jobs[:] = jobs[:1]
        self.window.refresh_jobs()
        self.assertEqual(self.window._jobs_offset, 0)
        self.assertEqual(self.window.jobs_table.rowCount(), 1)
        self.assertFalse(self.window._jobs_loading)

    def test_old_search_response_cannot_replace_newer_filter(self):
        jobs = [{"id": "new", "title": "New target", "status": "queued"}]
        client = self._use_paged_queue(jobs)
        pending = []
        self.window._run_async = lambda operation, success, **callbacks: pending.append(
            (operation, success, callbacks)
        )
        self.window.refresh_jobs()
        self.window.job_search.setText("New target")
        self.window.job_search_timer.stop()
        operation, success, callbacks = pending.pop(0)
        success(operation())
        self.assertEqual(self.window.jobs, [])
        callbacks["on_error"]("old search failed")
        self.assertEqual(self.window.queue_summary.text(), "Searching queue…")
        callbacks["on_finished"]()
        self.assertEqual(len(pending), 1)
        operation, success, callbacks = pending.pop()
        success(operation())
        callbacks["on_finished"]()
        self.assertEqual(client.calls[-1], (0, None, "New target"))
        self.assertEqual(self.window.jobs[0]["id"], "new")

    def test_old_controller_group_filter_uses_labelled_page_fallback(self):
        class OldClient:
            base_url = "https://controller.example.test"

            def list_jobs(self, **kwargs):
                if kwargs.get("status") == "active":
                    raise desktop.ControllerError("Unknown status", status=422)
                return {"items": [{"id": "one", "status": "queued"}], "total": 500}

            def stats(self):
                return {"total": 500, "queued": 500}

        self._use_paged_queue([])
        self.window.client = OldClient()
        self.window.job_status_filter.setCurrentIndex(
            self.window.job_status_filter.findData("active")
        )
        self.window.refresh_jobs()
        self.assertEqual(self.window.jobs_table.rowCount(), 1)
        self.assertIn("older controller", self.window.queue_summary.text())
        self.assertIn("whole queue", self.window.queue_summary.toolTip())
        self.assertTrue(self.window.next_jobs_button.isEnabled())

    def test_same_url_key_change_restarts_owned_controller(self):
        local_url = "http://127.0.0.1:8765"
        self.window.device_config = self.window.device_config.updated(
            controller_url=local_url,
            admin_key="old-key",
            network_mode="direct",
        )
        self.window.client = desktop.ControllerClient(
            local_url,
            controller_key="old-key",
        )
        self.window.controller_url.setText(local_url)
        self.window.controller_key.setText("new-key")

        class OldRuntime:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True
                return True

        class NewRuntime:
            def __init__(self):
                self.started = False

            def start(self):
                self.started = True
                return True

            def stop(self):
                return True

        old_runtime = OldRuntime()
        new_runtime = NewRuntime()
        self.window.controller_runtime = old_runtime

        with (
            patch.object(desktop, "controller_is_listening", return_value=False),
            patch.object(desktop, "InProcessController", return_value=new_runtime),
        ):
            self.window.apply_controller_url()

        self.assertTrue(old_runtime.stopped)
        self.assertTrue(new_runtime.started)
        self.assertIs(self.window.controller_runtime, new_runtime)
        self.assertEqual(self.window.client.controller_key, "new-key")

    def test_owned_controller_rejects_an_empty_key(self):
        local_url = "http://127.0.0.1:8765"
        self.window.device_config = self.window.device_config.updated(
            controller_url=local_url,
            admin_key="existing-key",
            network_mode="direct",
        )
        self.window.client = desktop.ControllerClient(
            local_url,
            controller_key="existing-key",
        )
        self.window.controller_url.setText(local_url)
        self.window.controller_key.clear()

        class Runtime:
            stopped = False

            def stop(self):
                self.stopped = True
                return True

        runtime = Runtime()
        self.window.controller_runtime = runtime
        with patch.object(desktop.QMessageBox, "warning") as warning:
            self.window.apply_controller_url()

        warning.assert_called_once()
        self.assertFalse(runtime.stopped)
        self.assertEqual(self.window.controller_key.text(), "existing-key")
        self.assertEqual(self.window.client.controller_key, "existing-key")

    def test_local_network_policy_stays_active_until_restart(self):
        self.window.device_config = self.window.device_config.updated(
            controller_url="http://127.0.0.1:8765",
            network_mode="direct",
        )

        class Runtime:
            def stop(self):
                return True

        self.window.controller_runtime = Runtime()
        self.window.active_network_mode = "direct"
        self.window.network_mode.setCurrentIndex(
            self.window.network_mode.findData("tor")
        )
        self.window.tor_proxy_input.setText("socks5h://127.0.0.1:9050")

        self.window.save_network_settings()

        self.assertEqual(self.window.device_config.network_mode, "tor")
        self.assertEqual(self.window.active_network_mode, "direct")
        self.assertTrue(self.window._restart_required)
        self.assertIsNone(self.window.local_settings.value("custom_proxy"))

    def test_unsaved_connection_test_keeps_active_controller_state(self):
        self.window._set_connection_state("error", "Actual controller is offline")
        self.window.controller_url.setText("https://candidate.example.test")
        self.window.controller_key.setText("candidate-key")
        self.window._run_async = lambda operation, success, **callbacks: success(
            {"status": "healthy"}
        )
        self.window.test_controller()
        self.assertEqual(self.window._connection_state, "error")
        self.assertFalse(self.window._workers_verified)
        self.assertIn("Apply these settings", self.window.statusBar().currentMessage())

    def test_unsaved_failed_connection_test_keeps_online_controller(self):
        self.window._set_connection_state("online")
        self.window.controller_url.setText("https://candidate.example.test")
        self.window._run_async = lambda operation, success, **callbacks: callbacks[
            "on_error"
        ]("Candidate unavailable")
        self.window.test_controller()
        self.assertEqual(self.window._connection_state, "online")
        self.assertIn(
            "active connection was kept", self.window.statusBar().currentMessage()
        )

    def test_connection_failure_invalidates_cached_worker_readiness(self):
        self.window._workers_verified = True
        self.window._set_connection_state("error", "Connection lost")
        self.window._set_connection_state("online")
        self.assertFalse(self.window._workers_verified)
        self.assertIn(
            "Checking download computers", self.window.start_tab.status_title.text()
        )


if __name__ == "__main__":
    unittest.main()
