from __future__ import annotations

import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from ateable.desktop import (
    BACKEND_AVAILABLE,
    ControllerClient,
    ControllerError,
    InProcessController,
    Routes,
    controller_is_listening,
    filter_jobs,
    initialize_local_device,
    is_loopback_controller,
    job_status_counts,
    normalize_base_url,
    pairing_key_from_payload,
    parse_urls,
    progress_percent,
    proxy_for_mode,
    read_url_list,
    should_manage_local_controller,
)

if BACKEND_AVAILABLE:
    from ateable.device_config import DeviceConfig


class FakeResponse:
    def __init__(self, payload, status: int = 200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        if self.payload is None:
            return b""
        return json.dumps(self.payload).encode()

    def close(self):
        return None


class ParsingTests(unittest.TestCase):
    def test_normalizes_controller_url(self):
        self.assertEqual(
            normalize_base_url("controller.local:8765/"), "http://controller.local:8765"
        )
        self.assertEqual(
            normalize_base_url("https://example.test/api/"), "https://example.test/api"
        )
        with self.assertRaises(ValueError):
            normalize_base_url("ftp://example.test")

    def test_parses_general_links_and_canonicalizes_duplicate_youtube_links(self):
        parsed = parse_urls(
            "https://youtu.be/alpha123\n"
            "youtube.com/watch?v=alpha123\n"
            "https://example.com/file.zip?signature=a,b%2Bc)\n"
            "ftp://example.com/file.zip\n"
        )
        self.assertEqual(
            parsed.urls,
            (
                "https://www.youtube.com/watch?v=alpha123",
                "https://example.com/file.zip?signature=a,b%2Bc)",
            ),
        )
        self.assertEqual(
            parsed.duplicates, ("https://www.youtube.com/watch?v=alpha123",)
        )
        self.assertEqual(parsed.invalid, ("ftp://example.com/file.zip",))

    def test_limits_input_to_five_thousand(self):
        text = "\n".join(f"https://youtu.be/video-{index}" for index in range(5002))
        parsed = parse_urls(text)
        self.assertEqual(len(parsed.urls), 5000)
        self.assertEqual(len(parsed.invalid), 2)

    def test_import_reads_unicode_lists_and_ignores_comment_lines(self):
        contents = "# My list\r\nhttps://youtu.be/example1\r\n  # https://youtu.be/ignored1\r\nhttps://youtu.be/example2\r\n"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "links.txt"
            for encoding in ("utf-8", "utf-8-sig", "utf-16"):
                with self.subTest(encoding=encoding):
                    path.write_bytes(contents.encode(encoding))
                    parsed = parse_urls(read_url_list(path))
                    self.assertEqual(len(parsed.urls), 2)
                    self.assertEqual(parsed.invalid, ())
                    self.assertNotIn("https://youtu.be/ignored1", parsed.urls)

    def test_import_rejects_large_binary_and_invalid_text_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "links.txt"
            for data, expected in (
                (b"x" * 33, "too large"),
                (b"binary\x00data", "binary"),
                (b"\x80\x81", "UTF-8"),
            ):
                with self.subTest(expected=expected):
                    path.write_bytes(data)
                    with self.assertRaisesRegex(ValueError, expected):
                        read_url_list(path, max_bytes=32)
            with self.assertRaisesRegex(ValueError, "text file"):
                read_url_list(Path(temporary))

    def test_proxy_modes(self):
        self.assertIsNone(proxy_for_mode("direct"))
        self.assertEqual(proxy_for_mode("tor"), "socks5h://127.0.0.1:9050")
        accepted = (
            "http://proxy.local:8080",
            "https://proxy.local:8443",
            "socks5://proxy.local:1080",
            "socks5h://proxy.local:1080",
        )
        for proxy_url in accepted:
            with self.subTest(proxy_url=proxy_url):
                self.assertEqual(proxy_for_mode("proxy", proxy_url), proxy_url)
        self.assertEqual(proxy_for_mode("custom", accepted[-1]), accepted[-1])
        for proxy_url in ("ftp://proxy.local:21", "ssh://proxy.local:22"):
            with self.subTest(proxy_url=proxy_url), self.assertRaises(ValueError):
                proxy_for_mode("proxy", proxy_url)

    def test_job_status_counts_includes_stable_zeroes_and_unknown_in_total(self):
        counts = job_status_counts(
            [
                {"status": "queued"},
                {"status": "QUEUED"},
                {"status": "downloading"},
                {"status": "paused"},
                {"status": "completed"},
                {"status": "failed"},
                {"status": "cancelled"},
                {"status": "future-status"},
            ]
        )

        self.assertEqual(
            counts,
            {
                "total": 8,
                "queued": 2,
                "downloading": 1,
                "paused": 1,
                "completed": 1,
                "failed": 1,
                "cancelled": 1,
            },
        )

    def test_filter_jobs_supports_summary_groups_and_exact_statuses(self):
        jobs = [
            {"id": "queued", "status": "queued"},
            {"id": "downloading", "status": "downloading"},
            {"id": "paused", "status": "paused"},
            {"id": "failed", "status": "failed"},
            {"id": "completed", "status": "completed"},
        ]

        self.assertEqual(
            [job["id"] for job in filter_jobs(jobs, status="active")],
            ["queued", "downloading"],
        )
        self.assertEqual(
            [job["id"] for job in filter_jobs(jobs, status="attention")],
            ["paused", "failed"],
        )
        self.assertEqual(
            [job["id"] for job in filter_jobs(jobs, status=" COMPLETED ")],
            ["completed"],
        )

    def test_filter_jobs_searches_user_visible_job_fields_case_insensitively(self):
        jobs = [
            {
                "id": "lecture",
                "status": "queued",
                "title": "Alpha Lecture",
                "target_worker_id": "render-box",
            },
            {
                "id": "download",
                "status": "downloading",
                "stage": "Extracting audio",
                "claimed_by": "WORKER-TWO",
            },
            {
                "id": "failure",
                "status": "failed",
                "source_url": "https://youtu.be/failure",
                "error": "HTTP timeout",
            },
        ]

        self.assertEqual(
            [job["id"] for job in filter_jobs(jobs, query="alpha lecture")],
            ["lecture"],
        )
        self.assertEqual(
            [job["id"] for job in filter_jobs(jobs, query="worker-two")],
            ["download"],
        )
        self.assertEqual(
            [job["id"] for job in filter_jobs(jobs, status="failed", query="TIMEOUT")],
            ["failure"],
        )

    def test_progress_accepts_fraction_or_percent(self):
        self.assertEqual(progress_percent(0.42), 42)
        self.assertEqual(progress_percent(42), 42)
        self.assertEqual(progress_percent(200), 100)
        self.assertEqual(progress_percent(None), 0)
        self.assertEqual(progress_percent(0, "completed"), 100)

    def test_pairing_key_shape_is_tolerant(self):
        self.assertEqual(pairing_key_from_payload({"pair_key": "PAIR"}), "PAIR")
        self.assertEqual(pairing_key_from_payload({"pairing_key": "ABCD"}), "ABCD")
        self.assertEqual(
            pairing_key_from_payload({"items": [{"code": "1234"}]}), "1234"
        )
        self.assertEqual(pairing_key_from_payload({}), "")

    def test_loopback_controller_detection_is_strict(self):
        self.assertTrue(is_loopback_controller("http://127.0.0.1:8765"))
        self.assertTrue(is_loopback_controller("http://[::1]:8765"))
        self.assertFalse(is_loopback_controller("https://127.0.0.1:8765"))
        self.assertFalse(is_loopback_controller("http://controller.example:8765"))
        self.assertFalse(is_loopback_controller("http://127.0.0.1:8765/prefix"))


class ClientTests(unittest.TestCase):
    def test_batch_request_uses_central_route_and_expected_shape(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["method"] = request.method
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data)
            captured["key"] = request.get_header("X-ateable-key")
            return FakeResponse({"batch": {"total": 1}})

        client = ControllerClient(
            "http://controller:8765", controller_key="control-secret", opener=opener
        )
        result = client.create_batch(
            ["https://youtu.be/alpha"],
            output_format="wav",
            target_worker_id="worker-1",
            output_subdir="speech",
        )

        self.assertEqual(captured["url"], f"http://controller:8765{Routes.BATCHES}")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["payload"]["format"], "wav")
        self.assertEqual(captured["key"], "control-secret")
        self.assertEqual(captured["payload"]["target_worker_id"], "worker-1")
        self.assertEqual(captured["payload"]["output_subdir"], "speech")
        self.assertEqual(result["batch"]["total"], 1)

    def test_job_query_encodes_filters(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            return FakeResponse({"items": [], "total": 0})

        client = ControllerClient("http://controller:8765", opener=opener)
        client.list_jobs(limit=25, offset=50, status="downloading")
        self.assertIn("/api/jobs?", captured["url"])
        self.assertIn("limit=25", captured["url"])
        self.assertIn("offset=50", captured["url"])
        self.assertIn("status=downloading", captured["url"])

    def test_stats_uses_server_wide_summary_route(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            return FakeResponse({"total": 600, "queued": 500, "completed": 100})

        client = ControllerClient("http://controller:8765", opener=opener)
        result = client.stats()

        self.assertEqual(captured["url"], f"http://controller:8765{Routes.STATS}")
        self.assertEqual(result["total"], 600)

    def test_network_probe_posts_proxy_without_query_string(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["method"] = request.method
            captured["payload"] = json.loads(request.data)
            return FakeResponse(
                {"mode": "proxy", "proxy": None, "reachable": True, "message": "OK"}
            )

        client = ControllerClient("http://controller:8765", opener=opener)
        client.probe_network("proxy", "socks5h://user:secret@proxy.local:1080")

        self.assertEqual(captured["url"], "http://controller:8765/api/network/probe")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["payload"]["mode"], "proxy")
        self.assertEqual(
            captured["payload"]["proxy"],
            "socks5h://user:secret@proxy.local:1080",
        )

    def test_http_error_becomes_readable_controller_error(self):
        def opener(request, timeout):
            raise HTTPError(
                request.full_url,
                422,
                "Unprocessable Entity",
                {},
                FakeResponse({"detail": "Bad batch"}),
            )

        client = ControllerClient("http://controller:8765", opener=opener)
        with self.assertRaisesRegex(ControllerError, "Bad batch") as raised:
            client.health()
        self.assertEqual(raised.exception.status, 422)


@unittest.skipUnless(BACKEND_AVAILABLE, "ATEABLE backend is not installed")
class StandaloneControllerTests(unittest.TestCase):
    def _free_port(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def test_remote_device_remains_client_only(self):
        remote = DeviceConfig.defaults().updated(
            controller_url="https://controller.example:8765",
            role="controller",
        )
        self.assertFalse(should_manage_local_controller(remote))

    def test_initialization_creates_admin_and_pair_key_without_changing_tor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            port = self._free_port()
            environment = {
                "ATEABLE_STATE_DIR": str(root / "state"),
                "ATEABLE_DATABASE_PATH": str(root / "state" / "ateable.db"),
            }
            with patch.dict(os.environ, environment):
                device = DeviceConfig.defaults().updated(
                    controller_url=f"http://127.0.0.1:{port}",
                    output_dir=str(root / "downloads"),
                    network_mode="tor",
                    admin_key=None,
                    last_pair_key=None,
                )
                initialized, app_config = initialize_local_device(
                    device,
                    save_path=root / "config.json",
                )

            self.assertTrue(initialized.admin_key)
            self.assertTrue(initialized.last_pair_key)
            self.assertEqual(initialized.network_mode, "tor")
            self.assertEqual(app_config.network_mode, "tor")
            self.assertEqual(
                json.loads((root / "config.json").read_text())["admin_key"],
                initialized.admin_key,
            )

    def test_in_process_controller_starts_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            port = self._free_port()
            environment = {
                "ATEABLE_STATE_DIR": str(root / "state"),
                "ATEABLE_DATABASE_PATH": str(root / "state" / "ateable.db"),
            }
            with patch.dict(os.environ, environment):
                device = DeviceConfig.defaults().updated(
                    controller_url=f"http://127.0.0.1:{port}",
                    output_dir=str(root / "downloads"),
                    network_mode="tor",
                    admin_key=None,
                    last_pair_key=None,
                )
                _initialized, app_config = initialize_local_device(
                    device,
                    save_path=root / "config.json",
                )
                runtime = InProcessController(
                    app_config,
                    tor_ensurer=lambda _proxy: None,
                )
                self.assertTrue(runtime.start())
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not controller_is_listening(
                    device.controller_url
                ):
                    time.sleep(0.05)
                self.assertTrue(controller_is_listening(device.controller_url))
                runtime.stop()

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and controller_is_listening(
                device.controller_url
            ):
                time.sleep(0.05)
            self.assertFalse(controller_is_listening(device.controller_url))
            self.assertIsNone(runtime.error)

    def test_controller_does_not_bootstrap_tor_or_create_media_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            port = self._free_port()
            with patch.dict(
                os.environ,
                {
                    "ATEABLE_STATE_DIR": str(root / "state"),
                    "ATEABLE_DATABASE_PATH": str(root / "state" / "ateable.db"),
                },
            ):
                device = DeviceConfig.defaults().updated(
                    controller_url=f"http://127.0.0.1:{port}",
                    output_dir=str(root / "media"),
                    network_mode="tor",
                    admin_key=None,
                    last_pair_key=None,
                )
                _, app_config = initialize_local_device(
                    device, save_path=root / "config.json"
                )
                self.assertFalse(app_config.embedded_worker)
                with patch("ateable.desktop.ensure_tor") as ensure:
                    runtime = InProcessController(app_config)
                    try:
                        self.assertTrue(runtime.start())
                        deadline = time.monotonic() + 5
                        while (
                            time.monotonic() < deadline
                            and not controller_is_listening(device.controller_url)
                        ):
                            time.sleep(0.025)
                        self.assertTrue(controller_is_listening(device.controller_url))
                        ensure.assert_not_called()
                        self.assertFalse((root / "media").exists())
                    finally:
                        runtime.stop()


if __name__ == "__main__":
    unittest.main()
