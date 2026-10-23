"""yt-dlp integration and download error classification."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import shutil
import ssl
import stat
import subprocess
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any, ClassVar, Protocol
from urllib.parse import unquote, urlparse

from .network import effective_proxy
from .validation import YOUTUBE_HOSTS, ValidationError, normalize_download_url

logger = logging.getLogger(__name__)
_DIRECT_CLOSE_BEFORE_REPLACE = os.name == "nt"


class DownloadStopped(Exception):
    """Raised by a progress callback when a job was paused or cancelled."""


class DownloadFailure(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        failure_kind: str = "generic",
        cooldown_seconds: float | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.failure_kind = failure_kind
        self.cooldown_seconds = cooldown_seconds


@dataclass(frozen=True, slots=True)
class DownloadSpec:
    job_id: str
    url: str
    media_format: str
    output_subdir: str | None = None
    proxy: str | None = None
    cookies_file: str | None = None
    network_mode: str = "direct"


@dataclass(frozen=True, slots=True)
class DownloadResult:
    output_path: str
    title: str | None


ProgressCallback = Callable[[dict[str, Any]], None]


class Downloader(Protocol):
    def download(
        self, spec: DownloadSpec, output_root: Path, progress: ProgressCallback
    ) -> DownloadResult: ...


def _node_is_supported(node_path: str) -> bool:
    try:
        output = subprocess.run(
            [node_path, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        major = int(output.lstrip("v").split(".", 1)[0])
        return major >= 22
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _deno_is_supported(deno_path: str) -> bool:
    try:
        output = subprocess.run(
            [deno_path, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.splitlines()[0]
        version = output.split(maxsplit=1)[1]
        major, minor, *_ = (int(part) for part in version.split("."))
        return (major, minor) >= (2, 3)
    except (IndexError, OSError, ValueError, subprocess.SubprocessError):
        return False


def detect_js_runtimes() -> dict[str, dict[str, str | None]]:
    deno = shutil.which("deno")
    if deno and _deno_is_supported(deno):
        return {"deno": {"path": deno}}
    node = shutil.which("node")
    if node and _node_is_supported(node):
        return {"node": {"path": node}}
    return {}


def resolve_output_dir(output_root: Path, output_subdir: str | None) -> Path:
    root = output_root.expanduser().resolve()
    target = (root / output_subdir).resolve() if output_subdir else root
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise DownloadFailure(
            "Output directory escapes the configured root", retryable=False
        ) from exc
    target.mkdir(parents=True, exist_ok=True)
    return target


def _direct_directory_fd(output_dir: Path) -> int | None:
    """Pin direct output directories where the platform supports no-follow opens.

    Windows keeps the path-based fallback; exclusive file creation still prevents
    an existing link from being followed when a partial file is opened.
    """
    if (
        not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW"))
        or os.open not in os.supports_dir_fd
    ):
        return None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(output_dir.anchor, flags)
    try:
        for component in output_dir.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise DownloadFailure(
            "Output directory is unsafe or unavailable",
            retryable=False,
            failure_kind="filesystem",
        ) from exc


def _direct_existing_file(
    output_dir: Path, filename: str, directory_fd: int | None
) -> os.stat_result | None:
    try:
        info = (
            os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
            if directory_fd is not None
            else (output_dir / filename).lstat()
        )
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise DownloadFailure(
            "Output file must be a regular file without symbolic or hard links",
            retryable=False,
            failure_kind="filesystem",
        )
    return info


def _direct_require_identity(
    output_dir: Path,
    filename: str,
    directory_fd: int | None,
    expected: os.stat_result,
    descriptor: int | None,
) -> None:
    """Check the named regular file still matches the file held by this attempt."""
    named = _direct_existing_file(output_dir, filename, directory_fd)
    pinned = os.fstat(descriptor) if descriptor is not None else expected
    if (
        named is None
        or not stat.S_ISREG(pinned.st_mode)
        or pinned.st_nlink != 1
        or not os.path.samestat(pinned, expected)
        or not os.path.samestat(named, expected)
        or pinned.st_size != expected.st_size
        or named.st_size != expected.st_size
    ):
        raise DownloadFailure(
            "Direct output changed during download; remove it before retrying",
            retryable=False,
            failure_kind="filesystem",
        )


def find_ffmpeg_executable() -> tuple[str | None, str]:
    system = shutil.which("ffmpeg")
    if system:
        return system, "system"
    try:
        import imageio_ffmpeg

        bundled = Path(imageio_ffmpeg.get_ffmpeg_exe())
        if bundled.is_file():
            return str(bundled), "imageio-ffmpeg"
    except (ImportError, OSError, RuntimeError):
        pass
    return None, "missing"


class YtDlpDownloader:
    """yt-dlp adapter. It deliberately uses no automatic proxy rotation."""

    AUDIO_FORMATS: ClassVar[set[str]] = {"wav", "mp3", "m4a", "flac", "opus"}

    def __init__(self, *, require_ffmpeg: bool = True):
        self.require_ffmpeg = require_ffmpeg

    @staticmethod
    def _error_attributes(message: str) -> tuple[bool, str, float | None]:
        lowered = message.lower()
        rate_markers = (
            "http error 429",
            "too many requests",
            "rate limit",
            "rate-limit",
            "unusual traffic",
            "confirm you're not a bot",
            "confirm you’re not a bot",
        )
        if any(marker in lowered for marker in rate_markers):
            return True, "rate_limited", 120.0
        permanent_markers = (
            "private video",
            "video unavailable",
            "has been removed",
            "copyright",
            "members-only",
            "not available in your country",
            "unsupported url",
            "http error 404",
            "http error 410",
        )
        return (
            not any(marker in lowered for marker in permanent_markers),
            "generic",
            None,
        )

    @staticmethod
    def _progress_payload(data: dict[str, Any]) -> dict[str, Any]:
        status = data.get("status")
        info = data.get("info_dict") or {}
        downloaded = data.get("downloaded_bytes")
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        percent: float | None = None
        if (
            isinstance(downloaded, (int, float))
            and isinstance(total, (int, float))
            and total
        ):
            percent = min(99.0, max(0.0, float(downloaded) * 100.0 / float(total)))
        elif status == "finished":
            # The media download is finished, but FFmpeg post-processing is not.
            percent = 99.0
        return {
            "progress": percent,
            "stage": "postprocessing" if status == "finished" else "downloading",
            "title": info.get("title"),
            "downloaded_bytes": downloaded,
            "total_bytes": total,
            "speed": data.get("speed"),
            "eta": data.get("eta"),
        }

    def build_options(
        self,
        spec: DownloadSpec,
        output_dir: Path,
        progress: ProgressCallback,
    ) -> dict[str, Any]:
        last_sent = 0.0
        last_stage: str | None = None

        def hook(data: dict[str, Any]) -> None:
            nonlocal last_sent, last_stage
            payload = self._progress_payload(data)
            now = time.monotonic()
            stage = payload["stage"]
            force = (
                stage != last_stage
                or (payload.get("progress") or 0) >= 99
                or now - last_sent >= 1.0
            )
            if not force:
                return
            progress(payload)
            last_sent = now
            last_stage = stage

        options: dict[str, Any] = {
            "format": (
                "best"
                if spec.media_format == "original"
                else "bestaudio/best"
                if spec.media_format in self.AUDIO_FORMATS
                else "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best"
            ),
            "outtmpl": {
                "default": "%(title).180B [%(id)s].%(ext)s",
            },
            "paths": {"home": str(output_dir), "temp": str(output_dir)},
            "noplaylist": True,
            "windowsfilenames": True,
            "restrictfilenames": False,
            "overwrites": False,
            "continuedl": True,
            "retries": 3,
            "fragment_retries": 3,
            "extractor_retries": 3,
            "file_access_retries": 3,
            "socket_timeout": 30,
            "sleep_interval_requests": 0.75,
            "sleep_interval": 5.0,
            "max_sleep_interval": 10.0,
            "concurrent_fragment_downloads": 1,
            "progress_hooks": [hook],
            "quiet": True,
            "noprogress": True,
            "no_warnings": False,
        }
        runtimes = detect_js_runtimes()
        if runtimes:
            options["js_runtimes"] = runtimes
        else:
            logger.warning(
                "Neither Deno nor Node 22+ was found; full YouTube extraction may fail"
            )
        if spec.media_format in self.AUDIO_FORMATS:
            options["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": spec.media_format,
                    "preferredquality": "0",
                }
            ]
            if spec.media_format == "wav":
                # Explicit, deterministic output for downstream speech/audio tools.
                options["postprocessor_args"] = {
                    "ExtractAudio+ffmpeg_o": ["-c:a", "pcm_s16le"]
                }
        elif spec.media_format != "original":
            options["merge_output_format"] = "mp4"
        if spec.proxy:
            options["proxy"] = spec.proxy
        if spec.cookies_file:
            options["cookiefile"] = spec.cookies_file
        return options

    @staticmethod
    def _has_site_extractor(url: str) -> bool:
        """Avoid probing a known media page a second time before extraction."""
        if (urlparse(url).hostname or "").lower().rstrip(".") in YOUTUBE_HOSTS:
            return True
        from yt_dlp.extractor import gen_extractor_classes

        return any(
            extractor.IE_NAME != "generic" and extractor.suitable(url)
            for extractor in gen_extractor_classes()
        )

    @staticmethod
    def _direct_filename(spec: DownloadSpec, headers: Any) -> str:
        disposition = Message()
        disposition["Content-Disposition"] = headers.get("Content-Disposition", "")
        suggested = (
            disposition.get_filename()
            or unquote(urlparse(spec.url).path).rsplit("/", 1)[-1]
        )
        # Treat both platform separators as separators, regardless of this host OS.
        suggested = suggested.replace("\\", "/").rsplit("/", 1)[-1]
        suggested = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", suggested).strip(" .")
        if not suggested or suggested in {".", ".."}:
            suggested = "download"
        source = Path(suggested)
        suffix = source.suffix[:24]
        stem = source.stem[:120].rstrip(" .") or "download"
        if stem.split(".", 1)[0].upper() in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *{f"COM{i}" for i in range(1, 10)},
            *{f"LPT{i}" for i in range(1, 10)},
        }:
            stem = "_" + stem
        # Filesystem name limits count bytes, not Unicode code points. Leave
        # room for the identity suffix and the temporary ".part" extension.
        stem = stem.encode("utf-8")[:160].decode("utf-8", errors="ignore") or "download"
        suffix = suffix.encode("utf-8")[:40].decode("utf-8", errors="ignore")
        identity = hashlib.sha256(spec.job_id.encode("utf-8")).hexdigest()[:12]
        return f"{stem} [{identity}]{suffix}"

    def _download_direct(
        self, ydl: Any, spec: DownloadSpec, output_dir: Path, progress: ProgressCallback
    ) -> DownloadResult | None:
        """Stream a generic file through yt-dlp's configured network transport.

        HTML pages and adaptive-media manifests return to normal extraction.
        Retries are controlled by the persistent job queue, rather than an
        unbounded network loop here. A failed attempt never publishes a final file.
        """
        from yt_dlp.networking import Request

        response = ydl.urlopen(
            Request(spec.url, headers={"Accept-Encoding": "identity"})
        )
        directory_fd = None
        try:
            try:
                normalize_download_url(response.url)
            except ValidationError as exc:
                raise DownloadFailure(
                    "Download redirected to an invalid URL", retryable=False
                ) from exc
            headers = response.headers
            # This request never sends Range. A slice must not be mistaken for
            # a complete dataset merely because its own Content-Length matches.
            if getattr(response, "status", 200) == 206 or headers.get("Content-Range"):
                raise DownloadFailure(
                    "Direct source returned a partial response without a requested range",
                    retryable=False,
                    failure_kind="source",
                )
            content_type = (
                headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            )
            disposition = headers.get("Content-Disposition", "").lower()
            if "attachment" not in disposition and content_type in {
                "text/html",
                "application/xhtml+xml",
                "application/dash+xml",
                "application/vnd.apple.mpegurl",
                "application/x-mpegurl",
            }:
                return None
            prefix = response.read(512)
            if "attachment" not in disposition and (
                prefix.lstrip()
                .lower()
                .startswith((b"<!doctype html", b"<html", b"#extm3u"))
            ):
                return None
            filename = self._direct_filename(spec, headers)
            final = output_dir / filename
            partial_name = filename + ".part"
            partial = output_dir / partial_name
            length = headers.get("Content-Length")
            total = int(length) if length and length.isdigit() else None
            if headers.get("Content-Encoding", "identity").lower() != "identity":
                total = None  # The transport may decode bytes, changing their length.
            directory_fd = _direct_directory_fd(output_dir)
            existing = _direct_existing_file(output_dir, filename, directory_fd)
            previous_partial = _direct_existing_file(
                output_dir, partial_name, directory_fd
            )
            if existing is not None:
                if total is None or existing.st_size != total:
                    raise DownloadFailure(
                        "Existing direct output cannot be verified against the source size; "
                        "remove it before retrying",
                        retryable=False,
                        failure_kind="filesystem",
                    )
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                descriptor = (
                    os.open(filename, flags, dir_fd=directory_fd)
                    if directory_fd is not None
                    else os.open(final, flags)
                )
                with os.fdopen(descriptor, "rb") as verified:
                    _direct_require_identity(
                        output_dir, filename, directory_fd, existing, verified.fileno()
                    )
                    progress(
                        {
                            "stage": "finishing",
                            "progress": 99.0,
                            "title": filename,
                            "downloaded_bytes": total,
                            "total_bytes": total,
                        }
                    )
                    _direct_require_identity(
                        output_dir, filename, directory_fd, existing, verified.fileno()
                    )
                    return DownloadResult(str(final), filename)
            if previous_partial is not None:
                # Direct downloads restart rather than assuming a server supports
                # ranges. Unlink before exclusive creation; never truncate a link.
                if directory_fd is not None:
                    os.unlink(partial_name, dir_fd=directory_fd)
                else:
                    partial.unlink()
            downloaded = 0
            started = time.monotonic()
            last_update = started
            progress(
                {
                    "stage": "downloading",
                    "progress": 0,
                    "title": filename,
                    "downloaded_bytes": 0,
                    "total_bytes": total,
                }
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = (
                os.open(partial_name, flags, 0o600, dir_fd=directory_fd)
                if directory_fd is not None
                else os.open(partial, flags, 0o600)
            )
            with os.fdopen(descriptor, "wb") as output:
                block = prefix
                while block:
                    output.write(block)
                    downloaded += len(block)
                    now = time.monotonic()
                    if now - last_update >= 1:
                        speed = downloaded / max(now - started, 0.001)
                        progress(
                            {
                                "stage": "downloading",
                                "title": filename,
                                "progress": min(99.0, downloaded * 100 / total)
                                if total
                                else None,
                                "downloaded_bytes": downloaded,
                                "total_bytes": total,
                                "speed": speed,
                                "eta": max(0, (total - downloaded) / speed)
                                if total
                                else None,
                            }
                        )
                        last_update = now
                    block = response.read(256 * 1024)
                output.flush()
                os.fsync(output.fileno())
                if total is not None and downloaded != total:
                    raise DownloadFailure(
                        "Direct file ended before its declared size was received"
                    )
                written = os.fstat(output.fileno())
                if written.st_size != downloaded:
                    raise DownloadFailure(
                        "Direct output changed during download; remove it before retrying",
                        retryable=False,
                        failure_kind="filesystem",
                    )
                progress(
                    {
                        "stage": "finishing",
                        "progress": 99.0,
                        "title": filename,
                        "downloaded_bytes": downloaded,
                        "total_bytes": total,
                    }
                )
                _direct_require_identity(
                    output_dir, partial_name, directory_fd, written, output.fileno()
                )
                if (
                    _direct_existing_file(output_dir, filename, directory_fd)
                    is not None
                ):
                    raise DownloadFailure(
                        "Output file appeared during download; remove it before retrying",
                        retryable=False,
                        failure_kind="filesystem",
                    )
                # POSIX keeps the inode pinned through rename and verification.
                # Windows CRT handles block rename, so close only after the
                # callback and identity check, then verify the captured identity.
                # A local directory owner can still mutate files after return.
                descriptor = output.fileno()
                if _DIRECT_CLOSE_BEFORE_REPLACE:
                    output.close()
                    descriptor = None
                if directory_fd is not None:
                    os.replace(
                        partial_name,
                        filename,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
                else:
                    partial.replace(final)
                try:
                    _direct_require_identity(
                        output_dir, filename, directory_fd, written, descriptor
                    )
                except (DownloadFailure, OSError):
                    # Unlink only the failed published directory entry; never
                    # resolve it or open a substituted symlink/hardlink target.
                    try:
                        if directory_fd is not None:
                            os.unlink(filename, dir_fd=directory_fd)
                        else:
                            final.unlink()
                    except FileNotFoundError:
                        pass
                    raise
                return DownloadResult(str(final), filename)
        except OSError as exc:
            # Disk/permission failures need operator action, not another full
            # multi-gigabyte transfer. SSL errors can reuse small errno values
            # (for example SSL_ERROR_SSL == EPERM), so retain network retries.
            if isinstance(exc, ssl.SSLError) or exc.errno not in {
                errno.EACCES,
                errno.EPERM,
                errno.ENOSPC,
                getattr(errno, "EDQUOT", errno.ENOSPC),
                errno.EROFS,
                errno.ENOTDIR,
                errno.ELOOP,
                errno.ENOENT,
                errno.EEXIST,
                errno.EISDIR,
                errno.ENAMETOOLONG,
                errno.EFBIG,
            }:
                raise
            raise DownloadFailure(
                "Direct output could not be written; check worker disk space and permissions",
                retryable=False,
                failure_kind="filesystem",
            ) from exc
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
            response.close()

    @staticmethod
    def _original_media_file(output_dir: Path, info: dict[str, Any]) -> Path:
        candidates = [info.get("filepath"), info.get("_filename")]
        candidates.extend(
            item.get("filepath")
            for item in info.get("requested_downloads", [])
            if isinstance(item, dict)
        )
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate).resolve()
            if (
                path.is_relative_to(output_dir.resolve())
                and path.is_file()
                and path.suffix != ".part"
            ):
                return path
        extension = info.get("ext")
        if not extension:
            raise DownloadFailure("yt-dlp did not identify the original media file")
        return YtDlpDownloader._find_final_file(
            output_dir, str(info["id"]), str(extension)
        )

    @staticmethod
    def _find_final_file(output_dir: Path, video_id: str, extension: str) -> Path:
        suffix = f"[{video_id}].{extension}".lower()
        candidates = [
            path
            for path in output_dir.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and path.name.lower().endswith(suffix)
        ]
        if not candidates:
            raise DownloadFailure(
                f"yt-dlp returned without producing a final .{extension} file",
                retryable=True,
            )
        return max(candidates, key=lambda path: path.stat().st_mtime)

    @staticmethod
    def _verify_wav(path: Path) -> None:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            try:
                with wave.open(str(path), "rb") as wav:
                    if wav.getsampwidth() != 2 or wav.getcomptype() != "NONE":
                        raise DownloadFailure(
                            "Final WAV is not uncompressed 16-bit PCM",
                            retryable=False,
                        )
                    if wav.getnchannels() < 1 or wav.getframerate() < 1:
                        raise DownloadFailure(
                            "Final WAV has invalid stream metadata", retryable=True
                        )
            except (EOFError, wave.Error) as exc:
                raise DownloadFailure(
                    f"Final WAV failed stdlib verification: {exc}", retryable=True
                ) from exc
            return
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=codec_name",
                    "-of",
                    "default=nokey=1:noprint_wrappers=1",
                    str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DownloadFailure(
                f"Final WAV failed ffprobe verification: {exc}", retryable=True
            ) from exc
        if result.stdout.strip() != "pcm_s16le":
            raise DownloadFailure(
                f"Final WAV codec is {result.stdout.strip() or 'unknown'}, expected pcm_s16le",
                retryable=False,
            )

    def download(
        self, spec: DownloadSpec, output_root: Path, progress: ProgressCallback
    ) -> DownloadResult:
        if spec.network_mode == "tor":
            try:
                effective_proxy("tor", None, spec.proxy or "")
            except ValueError as exc:
                raise DownloadFailure(
                    f"Tor mode is not safely configured: {exc}", retryable=False
                ) from exc
        try:
            normalize_download_url(spec.url)
        except ValidationError as exc:
            raise DownloadFailure(str(exc), retryable=False) from exc
        if urlparse(spec.url).scheme.lower() == "s3":
            # Import lazily: the controller can validate S3 sources without any
            # worker SDK installed, and it never creates an S3 client.
            from .object_storage import S3Source

            return S3Source(network_mode=spec.network_mode, proxy=spec.proxy).download(
                spec, output_root, progress
            )
        ffmpeg, _ = find_ffmpeg_executable()
        if self.require_ffmpeg and spec.media_format != "original" and not ffmpeg:
            raise DownloadFailure(
                "FFmpeg is required but neither a system nor bundled binary was found",
                retryable=False,
            )
        try:
            import yt_dlp
        except ImportError as exc:
            raise DownloadFailure(
                "yt-dlp is not installed; install the backend dependencies",
                retryable=False,
            ) from exc
        output_dir = resolve_output_dir(output_root, spec.output_subdir)
        options = self.build_options(spec, output_dir, progress)
        if ffmpeg:
            options["ffmpeg_location"] = ffmpeg
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                if spec.media_format == "original" and not self._has_site_extractor(
                    spec.url
                ):
                    direct = self._download_direct(ydl, spec, output_dir, progress)
                    if direct is not None:
                        return direct
                # extract_info returns only after every postprocessor has completed.
                info = ydl.extract_info(spec.url, download=True)
        except DownloadStopped:
            raise
        except DownloadFailure:
            raise
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            retryable, failure_kind, cooldown = self._error_attributes(message)
            raise DownloadFailure(
                message,
                retryable=retryable,
                failure_kind=failure_kind,
                cooldown_seconds=cooldown,
            ) from exc
        if not isinstance(info, dict) or not info.get("id"):
            raise DownloadFailure(
                "yt-dlp returned no video metadata after download", retryable=True
            )
        final_file = (
            self._original_media_file(output_dir, info)
            if spec.media_format == "original"
            else self._find_final_file(output_dir, str(info["id"]), spec.media_format)
        )
        if spec.media_format == "wav":
            self._verify_wav(final_file)
        return DownloadResult(
            output_path=str(final_file.resolve()), title=info.get("title")
        )
