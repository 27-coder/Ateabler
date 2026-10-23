"""Worker-only S3-compatible metadata discovery and original object downloads.

The controller only parses S3 URLs. This module constructs its SDK lazily on a
worker, using that worker's normal AWS credential chain or configured profile.
No remote objects or credential values are read at import time.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .downloader import (
    DownloadFailure,
    DownloadResult,
    DownloadSpec,
    DownloadStopped,
    ProgressCallback,
)
from .validation import (
    S3Location,
    ValidationError,
    parse_s3_url,
    validate_output_subdir,
)

MAX_DISCOVERY_ITEMS = 5000
MAX_DISCOVERY_PAGES = 100


@dataclass(frozen=True, slots=True)
class S3DiscoveryResult:
    items: list[dict[str, Any]]
    continuation_token: str | None = None


def _s3_failure(exc: Exception) -> DownloadFailure:
    """Classify structured errors without logging SDK messages or credentials."""
    response = getattr(exc, "response", None)
    response = response if isinstance(response, dict) else {}
    error = response.get("Error") or {}
    metadata = response.get("ResponseMetadata") or {}
    code = error.get("Code", "") if isinstance(error, dict) else ""
    status_code = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    name = type(exc).__name__
    if (
        code in {"SlowDown", "Throttling", "ThrottlingException", "TooManyRequests"}
        or status_code == 429
    ):
        return DownloadFailure(
            "Object storage rate limit reached; retry later",
            failure_kind="rate_limited",
            cooldown_seconds=120.0,
        )
    if code in {"AccessDenied", "Forbidden", "AllAccessDisabled"} or status_code == 403:
        return DownloadFailure(
            "Object storage access denied",
            retryable=False,
            failure_kind="access_denied",
        )
    if code in {"NoSuchKey", "NoSuchBucket", "NotFound", "404"} or status_code == 404:
        return DownloadFailure(
            "Object storage source does not exist",
            retryable=False,
            failure_kind="not_found",
        )
    if code in {
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "ExpiredToken",
        "InvalidToken",
        "TokenRefreshRequired",
    } or name in {
        "NoCredentialsError",
        "PartialCredentialsError",
        "ProfileNotFound",
        "CredentialRetrievalError",
        "TokenRetrievalError",
    }:
        return DownloadFailure(
            "Object storage authentication is unavailable; check worker credentials",
            retryable=False,
            failure_kind="credentials",
        )
    if (
        code
        in {
            "InvalidArgument",
            "InvalidRequest",
            "InvalidBucketName",
            "InvalidURI",
            "PermanentRedirect",
            "AuthorizationHeaderMalformed",
            "InvalidObjectState",
        }
        or status_code in {400, 401, 405, 410}
        or name in {"ParamValidationError", "InvalidRegionError"}
    ):
        return DownloadFailure(
            "Object storage rejected the source or worker configuration",
            retryable=False,
            failure_kind="source",
        )
    if isinstance(exc, OSError) and exc.errno in {
        errno.EACCES,
        errno.EPERM,
        errno.ENOSPC,
        errno.EROFS,
        errno.ENOTDIR,
        errno.ELOOP,
        errno.ENOENT,
        errno.EEXIST,
    }:
        return DownloadFailure(
            "Object storage output could not be written",
            retryable=False,
            failure_kind="filesystem",
        )
    return DownloadFailure(
        "Object storage request or stream failed; retry later",
        retryable=True,
        failure_kind="network",
    )


def _safe_filename(spec: DownloadSpec, key: str) -> str:
    suggested = key.replace("\\", "/").rsplit("/", 1)[-1]
    suggested = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", suggested).strip(" .")
    suggested = suggested or "object"
    source = Path(suggested)
    suffix = source.suffix[:24]
    stem = source.stem[:120].rstrip(" .") or "object"
    if stem.split(".", 1)[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *{f"COM{i}" for i in range(1, 10)},
        *{f"LPT{i}" for i in range(1, 10)},
    }:
        stem = "_" + stem
    # Keep filenames within common 255-byte limits even for multibyte keys.
    stem = stem.encode("utf-8")[:160].decode("utf-8", errors="ignore") or "object"
    suffix = suffix.encode("utf-8")[:40].decode("utf-8", errors="ignore")
    identity = hashlib.sha256(spec.job_id.encode("utf-8")).hexdigest()[:12]
    return f"{stem} [{identity}]{suffix}"


def _open_output_directory(
    output_root: Path, output_subdir: str | None
) -> tuple[Path, int]:
    """Pin each output path component with no-follow directory descriptors."""
    try:
        subdir = validate_output_subdir(output_subdir)
    except ValidationError as exc:
        raise DownloadFailure(
            "Output directory must be a safe relative path",
            retryable=False,
            failure_kind="filesystem",
        ) from exc
    root = Path(os.path.abspath(output_root.expanduser()))
    target = root / subdir if subdir else root
    if (
        not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW"))
        or os.open not in os.supports_dir_fd
    ):
        raise DownloadFailure(
            "S3 output safety requires a worker with no-follow directory operations",
            retryable=False,
            failure_kind="configuration",
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(target.anchor, flags)
    try:
        for component in target.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return target, descriptor
    except OSError as exc:
        os.close(descriptor)
        raise DownloadFailure(
            "Output directory is unsafe or unavailable (symbolic links are not allowed)",
            retryable=False,
            failure_kind="filesystem",
        ) from exc


def _existing_file(directory_fd: int, filename: str) -> os.stat_result | None:
    try:
        info = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise DownloadFailure(
            "Output file must be a regular file without symbolic or hard links",
            retryable=False,
            failure_kind="filesystem",
        )
    return info


class S3Source:
    """Bounded ListObjectsV2 discovery and streaming GetObject on a worker.

    Endpoint/profile/anonymous settings are local worker environment only:
    ATEABLE_S3_ENDPOINT, ATEABLE_S3_PROFILE, ATEABLE_S3_REGION and
    ATEABLE_S3_ANONYMOUS. Standard SDK profile/region defaults remain available.
    Tor/SOCKS modes fail before SDK initialization because botocore does not
    provide this adapter with a supported SOCKS5H transport.
    """

    def __init__(
        self,
        *,
        network_mode: str = "direct",
        proxy: str | None = None,
        client: Any = None,
    ):
        self.network_mode = network_mode
        self.proxy = proxy
        self._client = client

    def _check_network(self) -> dict[str, str]:
        if self.network_mode == "tor":
            raise DownloadFailure(
                "S3 SDK transport does not support Tor SOCKS5H; S3 access is blocked in Tor mode",
                retryable=False,
                failure_kind="network_policy",
            )
        if self.network_mode not in {"direct", "proxy"}:
            raise DownloadFailure(
                "Unsupported object storage network mode",
                retryable=False,
                failure_kind="network_policy",
            )
        if self.network_mode == "proxy" and not self.proxy:
            raise DownloadFailure(
                "Object storage proxy mode requires an explicit HTTP(S) proxy",
                retryable=False,
                failure_kind="network_policy",
            )
        if self.proxy:
            try:
                parsed = urlparse(self.proxy)
                valid = parsed.scheme.lower() in {"http", "https"} and bool(
                    parsed.hostname
                )
                _ = parsed.port
            except ValueError:
                valid = False
            if not valid:
                raise DownloadFailure(
                    "S3 SDK transport requires an explicit HTTP(S) proxy; SOCKS access is blocked",
                    retryable=False,
                    failure_kind="network_policy",
                )
            return {"http": self.proxy, "https": self.proxy}
        # Prevent implicit environment proxy changes from bypassing worker mode.
        return {}

    def _get_client(self) -> Any:
        proxies = self._check_network()
        if self._client is not None:
            return self._client
        endpoint = os.getenv("ATEABLE_S3_ENDPOINT") or None
        if endpoint:
            try:
                parsed = urlparse(endpoint)
                valid = (
                    parsed.scheme.lower() in {"http", "https"}
                    and bool(parsed.hostname)
                    and parsed.username is None
                    and parsed.password is None
                    and not parsed.query
                    and not parsed.fragment
                )
                _ = parsed.port
            except ValueError:
                valid = False
            if not valid:
                raise DownloadFailure(
                    "Worker S3 endpoint must be HTTP(S) without embedded credentials, a query or a fragment",
                    retryable=False,
                    failure_kind="configuration",
                )
        anonymous_setting = os.getenv("ATEABLE_S3_ANONYMOUS", "").strip().lower()
        if anonymous_setting not in {
            "",
            "1",
            "true",
            "yes",
            "on",
            "0",
            "false",
            "no",
            "off",
        }:
            raise DownloadFailure(
                "Worker S3 anonymous setting must be true or false",
                retryable=False,
                failure_kind="configuration",
            )
        try:
            import boto3
            from botocore import UNSIGNED
            from botocore.config import Config
        except ImportError as exc:
            raise DownloadFailure(
                "S3 downloads require the worker dependencies (boto3)",
                retryable=False,
                failure_kind="configuration",
            ) from exc
        try:
            session_options: dict[str, Any] = {
                "profile_name": os.getenv("ATEABLE_S3_PROFILE") or None
            }
            # Boto3 handles AWS_DEFAULT_REGION and profile configuration itself.
            # Also accept the AWS_REGION variable common on managed workers.
            region = os.getenv("ATEABLE_S3_REGION") or (
                os.getenv("AWS_REGION") if not os.getenv("AWS_DEFAULT_REGION") else None
            )
            if region:
                session_options["region_name"] = region
            session = boto3.Session(**session_options)
            anonymous = anonymous_setting in {"1", "true", "yes", "on"}
            if anonymous_setting == "":
                anonymous = session.get_credentials() is None
            options: dict[str, Any] = {
                "connect_timeout": 15,
                "read_timeout": 30,
                "retries": {"mode": "standard", "total_max_attempts": 3},
                "proxies": proxies,
            }
            if endpoint:
                options["s3"] = {"addressing_style": "path"}
            if anonymous:
                options["signature_version"] = UNSIGNED
            self._client = session.client(
                "s3", endpoint_url=endpoint, config=Config(**options)
            )
            return self._client
        # SDK initialization errors can contain credentials or signed URLs.
        except Exception as exc:  # noqa: BLE001
            raise _s3_failure(exc) from None

    def discover(
        self,
        uri: str,
        *,
        limit: int = MAX_DISCOVERY_ITEMS,
        continuation_token: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> S3DiscoveryResult:
        try:
            source = parse_s3_url(uri)
        except ValidationError as exc:
            raise DownloadFailure(
                str(exc), retryable=False, failure_kind="source"
            ) from exc
        if not source.is_listing:
            raise DownloadFailure(
                "S3 discovery requires a bucket or a prefix ending in /",
                retryable=False,
                failure_kind="source",
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > MAX_DISCOVERY_ITEMS
        ):
            raise DownloadFailure(
                "S3 discovery limit must be between 1 and 5000",
                retryable=False,
                failure_kind="source",
            )
        if continuation_token is not None and (
            not isinstance(continuation_token, str)
            or not continuation_token
            or len(continuation_token) > 16384
        ):
            raise DownloadFailure(
                "Invalid S3 continuation token", retryable=False, failure_kind="source"
            )
        client = self._get_client()
        items: list[dict[str, Any]] = []
        token = continuation_token
        pages = 0
        # Limit raw returned keys as well as children, so folder markers or
        # malformed responses cannot cause an unbounded metadata traversal.
        scanned = 0
        while scanned < limit and pages < MAX_DISCOVERY_PAGES:
            kwargs: dict[str, Any] = {
                "Bucket": source.bucket,
                "Prefix": source.key,
                "MaxKeys": min(1000, limit - scanned),
            }
            if token:
                kwargs["ContinuationToken"] = token
            try:
                page = client.list_objects_v2(**kwargs)
            # Sanitize all errors crossing the SDK boundary.
            except Exception as exc:  # noqa: BLE001
                raise _s3_failure(exc) from None
            if not isinstance(page, dict):
                raise DownloadFailure(
                    "Object storage returned an invalid discovery page",
                    retryable=False,
                    failure_kind="source",
                )
            contents = page.get("Contents") or []
            if not isinstance(contents, list) or len(contents) > kwargs["MaxKeys"]:
                raise DownloadFailure(
                    "Object storage returned an invalid discovery page",
                    retryable=False,
                    failure_kind="source",
                )
            pages += 1
            scanned += len(contents)
            for item in contents:
                key = item.get("Key") if isinstance(item, dict) else None
                size = item.get("Size") if isinstance(item, dict) else None
                if (
                    not isinstance(key, str)
                    or not key
                    or not key.startswith(source.key)
                    or not isinstance(size, int)
                    or isinstance(size, bool)
                    or size < 0
                ):
                    raise DownloadFailure(
                        "Object storage returned invalid object metadata",
                        retryable=False,
                        failure_kind="source",
                    )
                # Slash-ending objects represent discovery prefixes in this URI
                # protocol and must not recursively create listing child jobs.
                if key.endswith("/"):
                    continue
                location = S3Location(source.bucket, key)
                try:
                    parse_s3_url(location.url)
                except ValidationError:
                    continue
                items.append(
                    {
                        "url": location.url,
                        "key": key,
                        "size_bytes": size,
                        "title": key.rsplit("/", 1)[-1],
                    }
                )
            next_token = (
                page.get("NextContinuationToken") if page.get("IsTruncated") else None
            )
            if next_token is not None and (
                not isinstance(next_token, str)
                or not next_token
                or next_token == token
                or len(next_token) > 16384
            ):
                raise DownloadFailure(
                    "Object storage returned an invalid continuation token",
                    retryable=False,
                    failure_kind="source",
                )
            if page.get("IsTruncated") and not next_token:
                raise DownloadFailure(
                    "Object storage omitted its continuation token",
                    retryable=False,
                    failure_kind="source",
                )
            token = next_token
            if progress:
                progress(
                    {
                        "stage": "discovering",
                        "title": source.url,
                        "progress": min(99.0, scanned * 100 / limit),
                        "discovered_count": len(items),
                    }
                )
            if not token:
                break
        return S3DiscoveryResult(items, token)

    def download(
        self, spec: DownloadSpec, output_root: Path, progress: ProgressCallback
    ) -> DownloadResult:
        try:
            source = parse_s3_url(spec.url)
        except ValidationError as exc:
            raise DownloadFailure(
                str(exc), retryable=False, failure_kind="source"
            ) from exc
        if source.is_listing:
            raise DownloadFailure(
                "S3 bucket and prefix sources must be discovered on a worker before downloading",
                retryable=False,
                failure_kind="source",
            )
        if spec.media_format != "original":
            raise DownloadFailure(
                "S3 objects preserve their original format; choose original",
                retryable=False,
                failure_kind="source",
            )
        client = self._get_client()
        directory, directory_fd = _open_output_directory(
            output_root, spec.output_subdir
        )
        filename = _safe_filename(spec, source.key)
        partial = filename + ".part"
        body = None
        created_partial = False
        try:
            # Reject hostile output entries before initiating the remote request.
            existing = _existing_file(directory_fd, filename)
            if _existing_file(directory_fd, partial):
                os.unlink(partial, dir_fd=directory_fd)
            response = client.get_object(Bucket=source.bucket, Key=source.key)
            body = response.get("Body")
            total = response.get("ContentLength")
            if (
                body is None
                or not isinstance(total, int)
                or isinstance(total, bool)
                or total < 0
            ):
                raise DownloadFailure(
                    "Object storage returned no stream or valid object size",
                    retryable=True,
                    failure_kind="network",
                )
            if existing is not None:
                if existing.st_size != total:
                    raise DownloadFailure(
                        "Existing object output has a different size; remove it before retrying",
                        retryable=False,
                        failure_kind="filesystem",
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
                return DownloadResult(str(directory / filename), filename)
            downloaded = 0
            started = time.monotonic()
            last_update = started
            progress(
                {
                    "stage": "downloading",
                    "progress": 0.0,
                    "title": filename,
                    "downloaded_bytes": 0,
                    "total_bytes": total,
                }
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            descriptor = os.open(partial, flags, 0o600, dir_fd=directory_fd)
            created_partial = True
            with os.fdopen(descriptor, "wb") as output:
                while True:
                    block = body.read(256 * 1024)
                    if not block:
                        break
                    output.write(block)
                    downloaded += len(block)
                    if downloaded > total:
                        raise DownloadFailure(
                            "Object storage stream exceeded its declared size",
                            retryable=True,
                            failure_kind="network",
                        )
                    now = time.monotonic()
                    if now - last_update >= 1.0:
                        speed = downloaded / max(now - started, 0.001)
                        progress(
                            {
                                "stage": "downloading",
                                "title": filename,
                                "progress": min(99.0, downloaded * 100 / total)
                                if total
                                else 99.0,
                                "downloaded_bytes": downloaded,
                                "total_bytes": total,
                                "speed": speed,
                                "eta": max(0, (total - downloaded) / speed),
                            }
                        )
                        last_update = now
                output.flush()
                os.fsync(output.fileno())
            if downloaded != total:
                raise DownloadFailure(
                    "Object storage stream ended before its declared size was received",
                    retryable=True,
                    failure_kind="network",
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
            _existing_file(directory_fd, filename)
            os.replace(
                partial, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd
            )
            created_partial = False
            return DownloadResult(str(directory / filename), filename)
        except (DownloadFailure, DownloadStopped):
            raise
        # SDK/stream messages are intentionally replaced, never logged.
        except Exception as exc:  # noqa: BLE001
            raise _s3_failure(exc) from None
        finally:
            if body is not None:
                # A failed close must not mask cancellation or disclose SDK text.
                with suppress(Exception):
                    body.close()
            if created_partial:
                try:
                    os.unlink(partial, dir_fd=directory_fd)
                except OSError:
                    pass
            os.close(directory_fd)
