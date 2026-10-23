from __future__ import annotations

import errno
import io
import itertools
import os
import ssl
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ateable.downloader import (
    DownloadFailure,
    DownloadSpec,
    DownloadStopped,
    YtDlpDownloader,
    detect_js_runtimes,
    resolve_output_dir,
)
from ateable.validation import ValidationError, normalize_youtube_url


@pytest.mark.parametrize(
    ("source", "video_id"),
    [
        ("youtube.com/watch?v=abcdefghijk", "abcdefghijk"),
        ("https://youtu.be/ZYXWVUTSRQP?t=4", "ZYXWVUTSRQP"),
        ("https://www.youtube.com/shorts/a1b2c3d4e5F", "a1b2c3d4e5F"),
        ("https://www.youtube-nocookie.com/embed/123456789ab", "123456789ab"),
    ],
)
def test_youtube_url_normalization(source: str, video_id: str) -> None:
    canonical, key = normalize_youtube_url(source)
    assert canonical == f"https://www.youtube.com/watch?v={video_id}"
    assert key == video_id


@pytest.mark.parametrize(
    "source",
    [
        "https://notyoutube.com/watch?v=abcdefghijk",
        "file:///tmp/video",
        "https://youtube.com/playlist?list=abc",
        "https://youtube.com/watch?v=../../etc/passwd",
    ],
)
def test_non_video_urls_are_rejected(source: str) -> None:
    with pytest.raises(ValidationError):
        normalize_youtube_url(source)


def test_output_directory_cannot_escape_root(tmp_path: Path) -> None:
    with pytest.raises(DownloadFailure, match="escapes"):
        resolve_output_dir(tmp_path / "downloads", "../outside")


def test_old_deno_falls_back_to_supported_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ateable.downloader.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )

    def fake_run(command, **_kwargs):
        output = "deno 2.2.9\n" if command[0].endswith("deno") else "v22.13.0\n"
        return types.SimpleNamespace(stdout=output)

    monkeypatch.setattr("ateable.downloader.subprocess.run", fake_run)
    assert detect_js_runtimes() == {"node": {"path": "/usr/bin/node"}}


def test_wav_options_are_deterministic_and_portable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ateable.downloader.detect_js_runtimes",
        lambda: {"node": {"path": "/usr/bin/node"}},
    )
    spec = DownloadSpec("job", "https://youtube.test/video", "wav")
    options = YtDlpDownloader(require_ffmpeg=False).build_options(
        spec, tmp_path, lambda _: None
    )
    assert "allowed_extractors" not in options
    assert options["noplaylist"] is True
    assert options["windowsfilenames"] is True
    assert "%(title).180B [%(id)s]" in options["outtmpl"]["default"]
    assert "temp" not in options["outtmpl"]
    assert options["paths"]["home"] == options["paths"]["temp"] == str(tmp_path)
    assert options["postprocessors"][0]["preferredcodec"] == "wav"
    assert options["postprocessor_args"]["ExtractAudio+ffmpeg_o"] == [
        "-c:a",
        "pcm_s16le",
    ]
    assert options["js_runtimes"] == {"node": {"path": "/usr/bin/node"}}
    assert options["retries"] == 3
    assert options["fragment_retries"] == 3
    assert options["sleep_interval"] == 5
    assert options["max_sleep_interval"] == 10
    assert options["concurrent_fragment_downloads"] == 1
    assert options["noprogress"] is True
    assert "proxy" not in options


def test_explicit_proxy_and_cookies_only(tmp_path: Path) -> None:
    spec = DownloadSpec(
        "job",
        "https://youtube.test/video",
        "mp3",
        proxy="socks5h://127.0.0.1:9050",
        cookies_file="/explicit/cookies.txt",
    )
    options = YtDlpDownloader(require_ffmpeg=False).build_options(
        spec, tmp_path, lambda _: None
    )
    assert options["proxy"] == "socks5h://127.0.0.1:9050"
    assert options["cookiefile"] == "/explicit/cookies.txt"


def test_progress_hooks_are_throttled_but_stage_changes_are_immediate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = iter([0.0] * 101 + [0.1])
    monkeypatch.setattr("ateable.downloader.time.monotonic", lambda: next(clock))
    updates = []
    options = YtDlpDownloader(require_ffmpeg=False).build_options(
        DownloadSpec("job", "https://youtube.test/video", "wav"),
        tmp_path,
        updates.append,
    )
    hook = options["progress_hooks"][0]
    for downloaded in range(1, 102):
        hook(
            {
                "status": "downloading",
                "downloaded_bytes": downloaded,
                "total_bytes": 1000,
                "info_dict": {},
            }
        )
    assert len(updates) == 1
    hook(
        {
            "status": "finished",
            "downloaded_bytes": 1000,
            "total_bytes": 1000,
            "info_dict": {},
        }
    )
    assert len(updates) == 2
    assert updates[-1]["stage"] == "postprocessing"


def test_postprocess_failure_after_download_100_is_not_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updates = []

    class FailingYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def extract_info(self, url, download):
            self.options["progress_hooks"][0](
                {
                    "status": "finished",
                    "downloaded_bytes": 100,
                    "total_bytes": 100,
                    "info_dict": {"title": "Long 🎵 title"},
                }
            )
            raise RuntimeError("FFmpeg postprocessing failed")

    monkeypatch.setitem(
        sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FailingYoutubeDL)
    )
    with pytest.raises(DownloadFailure, match="postprocessing failed"):
        YtDlpDownloader(require_ffmpeg=False).download(
            DownloadSpec("job", "https://www.youtube.com/watch?v=abcdefghijk", "wav"),
            tmp_path,
            updates.append,
        )
    assert updates[-1]["progress"] == 99.0
    assert updates[-1]["stage"] == "postprocessing"


def test_return_without_final_wav_is_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class NoOutputYoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def extract_info(self, url, download):
            return {"id": "abcdefghijk", "title": "missing"}

    monkeypatch.setitem(
        sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=NoOutputYoutubeDL)
    )
    with pytest.raises(DownloadFailure, match="without producing"):
        YtDlpDownloader(require_ffmpeg=False).download(
            DownloadSpec("job", "https://www.youtube.com/watch?v=abcdefghijk", "wav"),
            tmp_path,
            lambda _: None,
        )


def test_long_unicode_final_filename_is_found(tmp_path: Path) -> None:
    video_id = "abcdefghijk"
    filename = f"{'音楽' * 35} [{video_id}].wav"
    output = tmp_path / filename
    output.write_bytes(b"wav")
    found = YtDlpDownloader._find_final_file(tmp_path, video_id, "wav")
    assert found == output


def test_rate_limit_errors_request_long_shared_cooldown() -> None:
    retryable, kind, cooldown = YtDlpDownloader._error_attributes(
        "ERROR: HTTP Error 429: Too Many Requests"
    )
    assert retryable is True
    assert kind == "rate_limited"
    assert cooldown is not None and cooldown >= 120

    _, generic_kind, generic_cooldown = YtDlpDownloader._error_attributes(
        "temporary connection reset"
    )
    assert generic_kind == "generic"
    assert generic_cooldown is None


@pytest.fixture
def file_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in {"/missing", "/rate-limited"}:
                self.send_error(404 if self.path == "/missing" else 429)
                return
            if self.path.startswith("/truncated"):
                content, size = b"%PDF-truncated", 5000
            else:
                content = b"%PDF-original binary\x00\xff" * 10000
                size = len(content)
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header(
                "Content-Disposition", 'attachment; filename="../../report.pdf"'
            )
            if not self.path.startswith("/unknown"):
                self.send_header("Content-Length", str(size))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.mark.parametrize("endpoint", ["/file.pdf?signature=a%2Fb", "/unknown"])
def test_original_direct_file_streams_without_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_server: str, endpoint: str
) -> None:
    monkeypatch.setattr(
        "ateable.downloader.find_ffmpeg_executable", lambda: (None, "missing")
    )
    monkeypatch.setattr("ateable.downloader.detect_js_runtimes", dict)
    updates = []
    result = YtDlpDownloader().download(
        DownloadSpec("direct-file", file_server + endpoint, "original"),
        tmp_path,
        updates.append,
    )
    output = Path(result.output_path)
    assert output.parent == tmp_path.resolve()
    assert output.name.startswith("report [") and output.suffix == ".pdf"
    assert output.read_bytes() == b"%PDF-original binary\x00\xff" * 10000
    assert not list(tmp_path.glob("*.part"))
    assert updates[-1]["progress"] == 99
    assert updates[-1]["downloaded_bytes"] == output.stat().st_size
    if endpoint == "/unknown":
        assert all(item.get("total_bytes") is None for item in updates)


def test_truncated_file_is_not_published(tmp_path: Path, file_server: str) -> None:
    with pytest.raises(DownloadFailure):
        YtDlpDownloader().download(
            DownloadSpec("truncated", file_server + "/truncated", "original"),
            tmp_path,
            lambda _update: None,
        )
    assert not list(tmp_path.glob("*.pdf"))


@pytest.mark.parametrize(
    ("endpoint", "retryable", "kind"),
    [
        ("/missing", False, "generic"),
        ("/rate-limited", True, "rate_limited"),
    ],
)
def test_direct_http_failure_uses_queue_retry_policy(
    tmp_path, file_server, endpoint, retryable, kind
) -> None:
    with pytest.raises(DownloadFailure) as failure:
        YtDlpDownloader().download(
            DownloadSpec("failure", file_server + endpoint, "original"),
            tmp_path,
            lambda _update: None,
        )
    assert failure.value.retryable is retryable
    assert failure.value.failure_kind == kind
    assert not list(tmp_path.iterdir())


def _fake_direct_transport(
    monkeypatch,
    content,
    *,
    content_type="application/octet-stream",
    headers=None,
    status=200,
    read_error=None,
):
    calls = []

    class FakeResponse(io.BytesIO):
        def __init__(self, value):
            super().__init__(value)
            self.headers = {
                "Content-Type": content_type,
                "Content-Length": str(len(content)),
                **(headers or {}),
            }
            self.url = "https://files.example.test/file.zip"
            self.status = status
            self.reads = 0

        def read(self, size=-1):
            self.reads += 1
            if read_error is not None and self.reads > 1:
                raise read_error
            return super().read(size)

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options
            calls.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def urlopen(self, request):
            calls.append(request)
            return FakeResponse(content)

    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(
        YtDlpDownloader, "_has_site_extractor", lambda _self, _url: False
    )
    monkeypatch.setattr("ateable.downloader.detect_js_runtimes", dict)
    return calls, FakeYoutubeDL


def test_direct_stream_uses_configured_tor_transport(
    tmp_path: Path, monkeypatch
) -> None:
    calls, _fake = _fake_direct_transport(monkeypatch, b"ZIP original")
    result = YtDlpDownloader().download(
        DownloadSpec(
            "tor-job",
            "https://files.example.test/file.zip",
            "original",
            proxy="socks5h://127.0.0.1:9050",
            network_mode="tor",
        ),
        tmp_path,
        lambda _update: None,
    )
    assert calls[0]["proxy"] == "socks5h://127.0.0.1:9050"
    assert calls[1].url == "https://files.example.test/file.zip"
    assert Path(result.output_path).read_bytes() == b"ZIP original"


def test_direct_cancellation_leaves_only_partial_file(
    tmp_path: Path, monkeypatch
) -> None:
    _fake_direct_transport(monkeypatch, b"a" * (300 * 1024))
    clock = itertools.count()
    monkeypatch.setattr("ateable.downloader.time.monotonic", lambda: float(next(clock)))
    updates = []

    def stop(update):
        updates.append(update)
        if update["downloaded_bytes"]:
            raise DownloadStopped("Paused by owner")

    with pytest.raises(DownloadStopped):
        YtDlpDownloader().download(
            DownloadSpec("paused", "https://files.example.test/file.zip", "original"),
            tmp_path,
            stop,
        )
    assert list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob("*.zip"))
    result = YtDlpDownloader().download(
        DownloadSpec("paused", "https://files.example.test/file.zip", "original"),
        tmp_path,
        lambda _update: None,
    )
    assert Path(result.output_path).read_bytes() == b"a" * (300 * 1024)
    assert not list(tmp_path.glob("*.part"))


def test_html_response_uses_site_extraction_instead_of_saving_page(
    tmp_path, monkeypatch
) -> None:
    calls, fake = _fake_direct_transport(
        monkeypatch, b"<html>media page</html>", content_type="text/html"
    )

    def extract(_self, _url, download):
        output = tmp_path / "clip [video-id].webm"
        output.write_bytes(b"original media codec")
        return {
            "id": "video-id",
            "title": "clip",
            "ext": "webm",
            "filepath": str(output),
        }

    monkeypatch.setattr(fake, "extract_info", extract, raising=False)
    result = YtDlpDownloader().download(
        DownloadSpec("page", "https://files.example.test/video", "original"),
        tmp_path,
        lambda _update: None,
    )
    assert Path(result.output_path).suffix == ".webm"
    assert "postprocessors" not in calls[0]
    assert "merge_output_format" not in calls[0]
    assert calls[0]["format"] == "best"


def test_original_media_path_cannot_escape_output_root(tmp_path) -> None:
    outside = tmp_path / "outside.webm"
    outside.write_bytes(b"outside")
    root = tmp_path / "downloads"
    root.mkdir()
    with pytest.raises(DownloadFailure):
        YtDlpDownloader._original_media_file(
            root, {"id": "clip", "ext": "webm", "filepath": str(outside)}
        )


def test_direct_output_rejects_partial_symlink(tmp_path, monkeypatch) -> None:
    _fake_direct_transport(monkeypatch, b"never overwrite this")
    spec = DownloadSpec("symlink", "https://files.example.test/file.zip", "original")
    name = YtDlpDownloader._direct_filename(spec, {})
    victim = tmp_path / "victim"
    victim.write_bytes(b"private")
    (tmp_path / (name + ".part")).symlink_to(victim)
    with pytest.raises(DownloadFailure, match="symbolic"):
        YtDlpDownloader().download(spec, tmp_path, lambda _update: None)
    assert victim.read_bytes() == b"private"


@pytest.mark.parametrize("path_fallback", [False, True])
@pytest.mark.parametrize("extension", ["", ".part"])
def test_direct_output_rejects_hardlinks_without_changing_target(
    tmp_path, monkeypatch, path_fallback, extension
) -> None:
    _fake_direct_transport(monkeypatch, b"downloaded bytes")
    if path_fallback:
        monkeypatch.setattr(
            "ateable.downloader._direct_directory_fd", lambda _path: None
        )
    spec = DownloadSpec("hardlink", "https://files.example.test/file.zip", "original")
    root = tmp_path / "downloads"
    root.mkdir()
    victim = tmp_path / "outside-output-root"
    victim.write_bytes(b"private")
    name = YtDlpDownloader._direct_filename(spec, {})
    try:
        os.link(victim, root / (name + extension))
    except OSError:
        pytest.skip("Filesystem does not support hard links")
    with pytest.raises(DownloadFailure, match="hard links") as failure:
        YtDlpDownloader().download(spec, root, lambda _update: None)
    assert failure.value.retryable is False
    assert victim.read_bytes() == b"private"


@pytest.mark.parametrize("path_fallback", [False, True])
@pytest.mark.parametrize("declared_size", ["16", None])
def test_direct_existing_final_must_match_declared_source_size(
    tmp_path, monkeypatch, path_fallback, declared_size
) -> None:
    _fake_direct_transport(
        monkeypatch, b"downloaded bytes", headers={"Content-Length": declared_size}
    )
    if path_fallback:
        monkeypatch.setattr(
            "ateable.downloader._direct_directory_fd", lambda _path: None
        )
    spec = DownloadSpec("corrupt", "https://files.example.test/file.zip", "original")
    final = tmp_path / YtDlpDownloader._direct_filename(spec, {})
    final.write_bytes(b"x")
    with pytest.raises(DownloadFailure, match="cannot be verified") as failure:
        YtDlpDownloader().download(spec, tmp_path, lambda _update: None)
    assert failure.value.retryable is False
    assert final.read_bytes() == b"x"
    assert not list(tmp_path.glob("*.part"))


def test_direct_existing_verified_size_checks_cancellation(
    tmp_path, monkeypatch
) -> None:
    content = b"complete file"
    _fake_direct_transport(monkeypatch, content)
    spec = DownloadSpec("existing", "https://files.example.test/file.zip", "original")
    final = tmp_path / YtDlpDownloader._direct_filename(spec, {})
    final.write_bytes(content)

    def stop(_update):
        raise DownloadStopped("Cancelled before reporting completion")

    with pytest.raises(DownloadStopped):
        YtDlpDownloader().download(spec, tmp_path, stop)
    assert final.read_bytes() == content


@pytest.mark.parametrize("path_fallback", [False, True])
def test_direct_exclusive_partial_creation_rejects_link_race(
    tmp_path, monkeypatch, path_fallback
) -> None:
    _fake_direct_transport(monkeypatch, b"downloaded bytes")
    if path_fallback:
        monkeypatch.setattr(
            "ateable.downloader._direct_directory_fd", lambda _path: None
        )
    spec = DownloadSpec("link-race", "https://files.example.test/file.zip", "original")
    victim = tmp_path / "outside-file"
    victim.write_bytes(b"private")
    name = YtDlpDownloader._direct_filename(spec, {})

    def insert_link(_update):
        try:
            os.link(victim, tmp_path / (name + ".part"))
        except OSError:
            pytest.skip("Filesystem does not support hard links")

    with pytest.raises(DownloadFailure):
        YtDlpDownloader().download(spec, tmp_path, insert_link)
    assert victim.read_bytes() == b"private"
    assert not (tmp_path / name).exists()


@pytest.mark.parametrize("path_fallback", [False, True])
@pytest.mark.parametrize("close_before_replace", [False, True])
@pytest.mark.parametrize("replacement", ["symlink", "hardlink", "regular"])
@pytest.mark.parametrize("when", ["finishing", "replace"])
def test_direct_partial_substitution_is_never_reported_as_complete(
    tmp_path, monkeypatch, path_fallback, close_before_replace, replacement, when
) -> None:
    if os.name == "nt" and not close_before_replace:
        pytest.skip("Windows CRT handles require closing before rename")
    content = b"downloaded bytes"
    _fake_direct_transport(monkeypatch, content)
    monkeypatch.setattr(
        "ateable.downloader._DIRECT_CLOSE_BEFORE_REPLACE", close_before_replace
    )
    if path_fallback:
        monkeypatch.setattr(
            "ateable.downloader._direct_directory_fd", lambda _path: None
        )
    spec = DownloadSpec(
        "substitution", "https://files.example.test/file.zip", "original"
    )
    root = tmp_path / "downloads"
    root.mkdir()
    victim = tmp_path / "outside-file"
    victim.write_bytes(b"private bytes must stay unchanged")
    name = YtDlpDownloader._direct_filename(spec, {})
    partial = root / (name + ".part")
    swapped = []

    def substitute():
        # Windows may refuse deletion while the download descriptor is open;
        # that is also a safe permanent failure before publication.
        partial.unlink()
        if replacement == "symlink":
            try:
                partial.symlink_to(victim)
            except OSError:
                pytest.skip("Host cannot create symbolic links")
        elif replacement == "hardlink":
            try:
                os.link(victim, partial)
            except OSError:
                pytest.skip("Filesystem cannot create hard links")
        else:
            partial.write_bytes(b"untrusted replacement with a different size")
        swapped.append(True)

    original_replace = os.replace

    def replace(source, destination, **kwargs):
        if when == "replace":
            substitute()
        return original_replace(source, destination, **kwargs)

    monkeypatch.setattr("ateable.downloader.os.replace", replace)

    def progress(update):
        if when == "finishing" and update["stage"] == "finishing":
            substitute()

    with pytest.raises(DownloadFailure) as failure:
        YtDlpDownloader().download(spec, root, progress)
    assert failure.value.retryable is False
    assert failure.value.failure_kind == "filesystem"
    assert not (root / name).exists()
    assert not (root / name).is_symlink()
    assert victim.read_bytes() == b"private bytes must stay unchanged"
    if when == "replace" and swapped:
        assert not partial.exists() and not partial.is_symlink()
        assert victim.stat().st_nlink == 1


@pytest.mark.parametrize("close_before_replace", [False, True])
def test_direct_descriptor_stays_open_until_publication_check(
    tmp_path, monkeypatch, close_before_replace
) -> None:
    if os.name == "nt" and not close_before_replace:
        pytest.skip("Windows CRT handles require closing before rename")
    _fake_direct_transport(monkeypatch, b"correct bytes")
    monkeypatch.setattr(
        "ateable.downloader._DIRECT_CLOSE_BEFORE_REPLACE", close_before_replace
    )
    outputs = []
    original_fdopen = os.fdopen

    def fdopen(descriptor, mode):
        output = original_fdopen(descriptor, mode)
        if mode == "wb":
            outputs.append(output)
        return output

    monkeypatch.setattr("ateable.downloader.os.fdopen", fdopen)
    original_replace = os.replace

    def replace(source, destination, **kwargs):
        assert outputs[-1].closed is close_before_replace
        return original_replace(source, destination, **kwargs)

    monkeypatch.setattr("ateable.downloader.os.replace", replace)

    def progress(update):
        if update["stage"] == "finishing":
            assert outputs and not outputs[-1].closed

    result = YtDlpDownloader().download(
        DownloadSpec("descriptor", "https://files.example.test/file.zip", "original"),
        tmp_path,
        progress,
    )
    assert Path(result.output_path).read_bytes() == b"correct bytes"
    assert all(output.closed for output in outputs)


def test_direct_pause_at_finishing_keeps_partial_and_retry_succeeds(
    tmp_path, monkeypatch
) -> None:
    content = b"download bytes retained for a paused attempt"
    _fake_direct_transport(monkeypatch, content)
    spec = DownloadSpec(
        "finishing-pause", "https://files.example.test/file.zip", "original"
    )

    def pause(update):
        if update["stage"] == "finishing":
            raise DownloadStopped("Paused before publication")

    with pytest.raises(DownloadStopped):
        YtDlpDownloader().download(spec, tmp_path, pause)
    partials = list(tmp_path.glob("*.part"))
    assert len(partials) == 1 and partials[0].read_bytes() == content
    assert not list(tmp_path.glob("*.zip"))
    result = YtDlpDownloader().download(spec, tmp_path, lambda _update: None)
    assert Path(result.output_path).read_bytes() == content
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("replacement", ["symlink", "hardlink"])
def test_direct_existing_final_is_rechecked_after_finishing_callback(
    tmp_path, monkeypatch, replacement
) -> None:
    content = b"previous verified-size output"
    _fake_direct_transport(monkeypatch, content)
    spec = DownloadSpec(
        "reuse-substitution", "https://files.example.test/file.zip", "original"
    )
    root = tmp_path / "downloads"
    root.mkdir()
    final = root / YtDlpDownloader._direct_filename(spec, {})
    final.write_bytes(content)
    victim = tmp_path / "outside-file"
    victim.write_bytes(b"private bytes")

    def substitute(update):
        if update["stage"] != "finishing":
            return
        final.unlink()
        try:
            if replacement == "symlink":
                final.symlink_to(victim)
            else:
                os.link(victim, final)
        except OSError:
            pytest.skip("Filesystem cannot create requested link")

    with pytest.raises(DownloadFailure) as failure:
        YtDlpDownloader().download(spec, root, substitute)
    assert failure.value.retryable is False
    assert failure.value.failure_kind == "filesystem"
    assert victim.read_bytes() == b"private bytes"


def test_direct_unicode_filename_fits_filesystem_byte_limit(
    tmp_path, monkeypatch
) -> None:
    content = b"data"
    _fake_direct_transport(
        monkeypatch,
        content,
        headers={
            "Content-Disposition": 'attachment; filename="' + "文" * 120 + '.bin"'
        },
    )
    result = YtDlpDownloader().download(
        DownloadSpec("unicode", "https://files.example.test/file.zip", "original"),
        tmp_path,
        lambda _update: None,
    )
    final = Path(result.output_path)
    assert final.suffix == ".bin"
    assert len((final.name + ".part").encode("utf-8")) <= 255
    assert final.read_bytes() == content


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (206, {"Content-Range": "bytes 100-199/1000"}),
        (206, {}),
        (200, {"Content-Range": "bytes 100-199/1000"}),
    ],
)
def test_direct_unsolicited_partial_response_is_never_published(
    tmp_path, monkeypatch, status, headers
) -> None:
    # The slice's Content-Length is correct, but it is not the requested file.
    _fake_direct_transport(monkeypatch, b"x" * 100, status=status, headers=headers)
    with pytest.raises(DownloadFailure, match="partial response") as failure:
        YtDlpDownloader().download(
            DownloadSpec("partial", "https://files.example.test/file.zip", "original"),
            tmp_path,
            lambda _update: None,
        )
    assert failure.value.retryable is False
    assert failure.value.failure_kind == "source"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("error_number", [errno.ENOSPC, errno.EACCES, errno.EROFS])
def test_direct_output_failure_does_not_retry_entire_download(
    tmp_path, monkeypatch, error_number
) -> None:
    _fake_direct_transport(monkeypatch, b"all the downloaded bytes")

    def cannot_flush(_descriptor):
        raise OSError(error_number, "Worker output is unavailable")

    monkeypatch.setattr("ateable.downloader.os.fsync", cannot_flush)
    with pytest.raises(DownloadFailure) as failure:
        YtDlpDownloader().download(
            DownloadSpec(
                "disk-full", "https://files.example.test/file.zip", "original"
            ),
            tmp_path,
            lambda _update: None,
        )
    assert failure.value.retryable is False
    assert failure.value.failure_kind == "filesystem"
    assert not list(tmp_path.glob("*.zip"))


@pytest.mark.parametrize(
    "error",
    [
        OSError(errno.ECONNRESET, "Connection lost"),
        ssl.SSLError(1, "TLS stream failed"),
    ],
)
def test_direct_network_errors_still_use_retry_policy(tmp_path, monkeypatch, error):
    _fake_direct_transport(monkeypatch, b"bytes received", read_error=error)
    with pytest.raises(DownloadFailure) as failure:
        YtDlpDownloader().download(
            DownloadSpec("network", "https://files.example.test/file.zip", "original"),
            tmp_path,
            lambda _update: None,
        )
    assert failure.value.retryable is True
    assert failure.value.failure_kind == "generic"
    assert not list(tmp_path.glob("*.zip"))
