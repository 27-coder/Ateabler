from __future__ import annotations

import io
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from ateable.downloader import (
    DownloadFailure,
    DownloadSpec,
    DownloadStopped,
    YtDlpDownloader,
)
from ateable.object_storage import S3Source, _safe_filename
from ateable.validation import (
    ValidationError,
    is_s3_listing,
    normalize_download_url,
    parse_s3_url,
)

secure_output = pytest.mark.skipif(
    not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW"))
    or os.open not in os.supports_dir_fd,
    reason="Secure S3 output requires Unix no-follow directory operations",
)


class FakeClient:
    def __init__(
        self, *, pages=None, body: bytes = b"sample data", length: int | None = None
    ):
        self.pages = iter(pages or [])
        self.body = body
        self.length = len(body) if length is None else length
        self.requests: list[dict[str, Any]] = []
        self.streams: list[io.BytesIO] = []

    def list_objects_v2(self, **kwargs):
        self.requests.append(kwargs)
        return next(self.pages)

    def get_object(self, **kwargs):
        self.requests.append(kwargs)
        stream = io.BytesIO(self.body)
        self.streams.append(stream)
        return {"Body": stream, "ContentLength": self.length}


class FakeSDKError(Exception):
    def __init__(self, code: str, status: int):
        super().__init__("credential=DO-NOT-EXPOSE secret=DO-NOT-EXPOSE")
        self.response = {
            "Error": {"Code": code, "Message": str(self)},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


def test_s3_normalization_preserves_key_and_encoding_without_sdk(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "boto3", None)
    canonical, identity = normalize_download_url(
        "S3://data-bucket/Folder/A%20B%3f%23%25.bin"
    )
    assert canonical == "s3://data-bucket/Folder/A%20B%3F%23%25.bin"
    assert parse_s3_url(canonical).key == "Folder/A B?#%.bin"
    assert normalize_download_url(canonical)[1] == identity
    assert (
        identity
        != normalize_download_url("s3://data-bucket/folder/A%20B%3F%23%25.bin")[1]
    )
    assert (
        identity
        != normalize_download_url("s3://other-bucket/Folder/A%20B%3F%23%25.bin")[1]
    )
    assert normalize_download_url("s3://data-bucket/") == normalize_download_url(
        "s3://data-bucket"
    )
    assert is_s3_listing("s3://data-bucket")
    assert is_s3_listing("s3://data-bucket/Folder/")
    assert not is_s3_listing(canonical)
    assert not is_s3_listing("https://files.test/s3/")
    assert parse_s3_url("s3://data-bucket/a//b/../c%2Fd").key == "a//b/../c/d"


@pytest.mark.parametrize(
    "uri",
    [
        "s3://",
        "s3://user:secret@bucket/key",
        "s3://bucket:9000/key",
        "s3://Bucket/key",
        "s3://bucket/key?token=secret",
        "s3://bucket/key#fragment",
        "s3://bucket/key%xx",
        "s3://bucket/%FF",
        "s3://bucket/%00",
        "s3://bucket/a b",
    ],
)
def test_invalid_s3_urls_are_rejected_locally(uri: str) -> None:
    with pytest.raises(ValidationError):
        normalize_download_url(uri)


def test_discovery_is_paginated_bounded_and_resume_token_is_exact() -> None:
    first = {
        "Contents": [
            {"Key": "raw/a?%.pdf", "Size": 4},
            {"Key": "raw/b#B.zip", "Size": 9},
        ],
        "IsTruncated": True,
        "NextContinuationToken": "opaque+/=cursor",
    }
    client = FakeClient(pages=[first])
    result = S3Source(client=client).discover("s3://data-bucket/raw/", limit=2)
    assert client.requests == [
        {"Bucket": "data-bucket", "Prefix": "raw/", "MaxKeys": 2}
    ]
    assert result.continuation_token == "opaque+/=cursor"
    assert result.items == [
        {
            "url": "s3://data-bucket/raw/a%3F%25.pdf",
            "key": "raw/a?%.pdf",
            "size_bytes": 4,
            "title": "a?%.pdf",
        },
        {
            "url": "s3://data-bucket/raw/b%23B.zip",
            "key": "raw/b#B.zip",
            "size_bytes": 9,
            "title": "b#B.zip",
        },
    ]
    resumed = FakeClient(
        pages=[{"Contents": [{"Key": "raw/c.bin", "Size": 0}], "IsTruncated": False}]
    )
    final = S3Source(client=resumed).discover(
        "s3://data-bucket/raw/", limit=2, continuation_token=result.continuation_token
    )
    assert resumed.requests[0]["ContinuationToken"] == "opaque+/=cursor"
    assert final.continuation_token is None
    assert len(final.items) == 1


def test_discovery_never_processes_more_than_5000_raw_keys() -> None:
    pages = [
        {
            "Contents": [
                {"Key": f"raw/{page}-{number}.bin", "Size": number}
                for number in range(1000)
            ],
            "IsTruncated": True,
            "NextContinuationToken": f"page-{page + 1}",
        }
        for page in range(6)
    ]
    client = FakeClient(pages=pages)
    updates = []
    result = S3Source(client=client).discover(
        "s3://data-bucket/raw/", progress=updates.append
    )
    assert len(result.items) == 5000
    assert len(client.requests) == 5
    assert all(request["MaxKeys"] == 1000 for request in client.requests)
    assert result.continuation_token == "page-5"
    assert len(updates) == 5


def test_marker_objects_consume_budget_and_are_not_listing_children() -> None:
    client = FakeClient(
        pages=[
            {
                "Contents": [
                    {"Key": "raw/", "Size": 0},
                    {"Key": "raw/folder/", "Size": 0},
                ],
                "IsTruncated": True,
                "NextContinuationToken": "more",
            }
        ]
    )
    result = S3Source(client=client).discover("s3://data-bucket/raw/", limit=2)
    assert result.items == []
    assert result.continuation_token == "more"
    assert len(client.requests) == 1


@pytest.mark.parametrize("limit", [0, -1, 5001, True, "5"])
def test_invalid_discovery_limits_do_not_call_bucket(limit) -> None:
    client = FakeClient()
    with pytest.raises(DownloadFailure):
        S3Source(client=client).discover("s3://data-bucket", limit=limit)
    assert not client.requests


def test_sdk_omitting_continuation_cannot_silently_truncate_discovery() -> None:
    client = FakeClient(pages=[{"Contents": [], "IsTruncated": True}])
    with pytest.raises(DownloadFailure, match="continuation"):
        S3Source(client=client).discover("s3://data-bucket")


@pytest.mark.parametrize(
    "mode,proxy",
    [
        ("tor", "socks5h://127.0.0.1:9050"),
        ("proxy", "socks5://127.0.0.1:9050"),
        ("direct", "socks5h://127.0.0.1:9050"),
        ("proxy", None),
    ],
)
def test_tor_and_unsupported_proxies_fail_closed(
    mode, proxy, tmp_path, monkeypatch
) -> None:
    monkeypatch.setitem(sys.modules, "boto3", None)
    client = FakeClient()
    source = S3Source(network_mode=mode, proxy=proxy, client=client)
    with pytest.raises(DownloadFailure) as failure:
        source.download(
            DownloadSpec("job", "s3://data-bucket/object.zip", "original"),
            tmp_path,
            lambda _: None,
        )
    assert not failure.value.retryable
    assert failure.value.failure_kind == "network_policy"
    assert not client.requests
    with pytest.raises(DownloadFailure):
        source.discover("s3://data-bucket")
    assert not client.requests


@secure_output
def test_exact_object_download_streams_original_and_never_lists(tmp_path) -> None:
    payload = b"%PDF" + b"B" * (300 * 1024)
    client = FakeClient(body=payload)
    updates = []
    spec = DownloadSpec(
        "object-job",
        "s3://data-bucket/../../Dataset%20A.PDF",
        "original",
        output_subdir="datasets/2026",
    )
    result = S3Source(client=client).download(
        spec, tmp_path / "downloads", updates.append
    )
    path = Path(result.output_path)
    assert path.parent == tmp_path / "downloads/datasets/2026"
    assert path.suffix == ".PDF"
    assert path.read_bytes() == payload
    assert client.requests == [{"Bucket": "data-bucket", "Key": "../../Dataset A.PDF"}]
    assert client.streams[0].closed
    assert updates[0]["total_bytes"] == len(payload)
    assert updates[-1]["downloaded_bytes"] == len(payload)
    assert not list(tmp_path.rglob("*.part"))
    assert result.title == path.name


@secure_output
def test_zero_length_object_is_a_valid_completed_download(tmp_path) -> None:
    updates = []
    result = S3Source(client=FakeClient(body=b"")).download(
        DownloadSpec("empty-job", "s3://data-bucket/empty.txt", "original"),
        tmp_path,
        updates.append,
    )
    assert Path(result.output_path).stat().st_size == 0
    assert updates[-1]["downloaded_bytes"] == updates[-1]["total_bytes"] == 0


@pytest.mark.parametrize("declared,actual", [(10, b"short"), (1, b"too-long")])
@secure_output
def test_incomplete_or_overlong_stream_never_publishes_final_file(
    tmp_path, declared, actual
) -> None:
    client = FakeClient(body=actual, length=declared)
    with pytest.raises(DownloadFailure) as failure:
        S3Source(client=client).download(
            DownloadSpec("job", "s3://data-bucket/model.bin", "original"),
            tmp_path,
            lambda _: None,
        )
    assert failure.value.retryable
    assert list(tmp_path.iterdir()) == []
    assert client.streams[0].closed


@secure_output
def test_cancel_from_progress_removes_partial_and_closes_stream(tmp_path) -> None:
    client = FakeClient()

    def stop(update):
        if update["stage"] == "finishing":
            raise DownloadStopped("cancelled")

    with pytest.raises(DownloadStopped):
        S3Source(client=client).download(
            DownloadSpec("job", "s3://data-bucket/archive.zip", "original"),
            tmp_path,
            stop,
        )
    assert list(tmp_path.iterdir()) == []
    assert client.streams[0].closed


@secure_output
def test_stream_failure_is_retryable_sanitized_and_removes_partial(tmp_path) -> None:
    class BrokenStream(io.BytesIO):
        def read(self, size=-1):
            if self.tell():
                raise FakeSDKError("InternalError", 500)
            return super().read(size)

    stream = BrokenStream(b"partial-data")

    class StreamClient:
        def get_object(self, **kwargs):
            return {"Body": stream, "ContentLength": 50}

    with pytest.raises(DownloadFailure) as failure:
        S3Source(client=StreamClient()).download(
            DownloadSpec("job", "s3://data-bucket/model.bin", "original"),
            tmp_path,
            lambda _: None,
        )
    assert failure.value.retryable
    assert failure.value.failure_kind == "network"
    assert "DO-NOT-EXPOSE" not in str(failure.value)
    assert list(tmp_path.iterdir()) == []
    assert stream.closed


@secure_output
def test_partial_hard_link_cannot_overwrite_a_file_outside_output(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"untouched")
    spec = DownloadSpec("job", "s3://data-bucket/data.bin", "original")
    os.link(outside, root / (_safe_filename(spec, "data.bin") + ".part"))
    client = FakeClient()
    with pytest.raises(DownloadFailure, match="hard links"):
        S3Source(client=client).download(spec, root, lambda _: None)
    assert not client.requests
    assert outside.read_bytes() == b"untouched"


@pytest.mark.parametrize(
    "subdir", ["../outside", "a/../../outside", "/outside", "a/./b"]
)
def test_output_subdir_escape_is_rejected_before_bucket_request(
    tmp_path, subdir
) -> None:
    client = FakeClient()
    with pytest.raises(DownloadFailure):
        S3Source(client=client).download(
            DownloadSpec(
                "job", "s3://data-bucket/a.pdf", "original", output_subdir=subdir
            ),
            tmp_path / "root",
            lambda _: None,
        )
    assert not client.requests
    assert not (tmp_path / "outside").exists()


@secure_output
def test_output_directory_symlink_is_rejected_before_bucket_request(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    client = FakeClient()
    with pytest.raises(DownloadFailure, match="symbolic"):
        S3Source(client=client).download(
            DownloadSpec(
                "job", "s3://data-bucket/archive.zip", "original", output_subdir="link"
            ),
            root,
            lambda _: None,
        )
    assert not client.requests
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("extension", ["", ".part"])
@secure_output
def test_output_file_symlink_is_rejected_and_target_unchanged(
    tmp_path, extension
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"untouched")
    spec = DownloadSpec("job", "s3://data-bucket/archive.zip", "original")
    (root / (_safe_filename(spec, "archive.zip") + extension)).symlink_to(outside)
    client = FakeClient()
    with pytest.raises(DownloadFailure, match="symbolic"):
        S3Source(client=client).download(spec, root, lambda _: None)
    assert not client.requests
    assert outside.read_bytes() == b"untouched"


def test_original_only_and_listing_download_fail_without_network(tmp_path) -> None:
    client = FakeClient()
    for uri, media_format in [
        ("s3://data-bucket", "original"),
        ("s3://data-bucket/raw/", "original"),
        ("s3://data-bucket/model.bin", "wav"),
    ]:
        with pytest.raises(DownloadFailure) as failure:
            S3Source(client=client).download(
                DownloadSpec("job", uri, media_format), tmp_path, lambda _: None
            )
        assert not failure.value.retryable
    assert not client.requests


@pytest.mark.parametrize("missing", ["O_NOFOLLOW", "dir_fd"])
def test_missing_secure_output_operations_fail_closed_before_get(
    tmp_path, monkeypatch, missing
) -> None:
    if missing == "O_NOFOLLOW":
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    else:
        monkeypatch.setattr(os, "supports_dir_fd", set())
    client = FakeClient()
    with pytest.raises(DownloadFailure) as failure:
        S3Source(client=client).download(
            DownloadSpec("job", "s3://data-bucket/data.bin", "original"),
            tmp_path / "downloads",
            lambda _: None,
        )
    assert failure.value.failure_kind == "configuration"
    assert not failure.value.retryable
    assert not client.requests
    assert not (tmp_path / "downloads").exists()


@pytest.mark.parametrize(
    "code,status,retryable,kind",
    [
        ("AccessDenied", 403, False, "access_denied"),
        ("NoSuchKey", 404, False, "not_found"),
        ("NoSuchBucket", 404, False, "not_found"),
        ("InvalidAccessKeyId", 400, False, "credentials"),
        ("SlowDown", 503, True, "rate_limited"),
        ("InternalError", 500, True, "network"),
    ],
)
@secure_output
def test_structured_sdk_failures_are_classified_and_never_expose_secrets(
    tmp_path, code, status, retryable, kind
) -> None:
    class BrokenClient:
        def get_object(self, **kwargs):
            raise FakeSDKError(code, status)

        def list_objects_v2(self, **kwargs):
            raise FakeSDKError(code, status)

    source = S3Source(client=BrokenClient())
    with pytest.raises(DownloadFailure) as failure:
        source.download(
            DownloadSpec("job", "s3://data-bucket/a.zip", "original"),
            tmp_path,
            lambda _: None,
        )
    assert failure.value.retryable is retryable
    assert failure.value.failure_kind == kind
    assert "DO-NOT-EXPOSE" not in str(failure.value)
    assert failure.value.__cause__ is None
    with pytest.raises(DownloadFailure) as discovery:
        source.discover("s3://data-bucket")
    assert discovery.value.failure_kind == kind
    assert "DO-NOT-EXPOSE" not in str(discovery.value)


@pytest.fixture
def fake_sdk(monkeypatch):
    created = []

    class FakeConfig:
        def __init__(self, **kwargs):
            self.options = kwargs

    class FakeSession:
        credentials = object()

        def __init__(self, **kwargs):
            created.append({"session": kwargs})

        def get_credentials(self):
            return self.credentials

        def client(self, service, **kwargs):
            created.append({"service": service, **kwargs})
            return FakeClient()

    monkeypatch.setitem(
        sys.modules, "boto3", types.SimpleNamespace(Session=FakeSession)
    )
    monkeypatch.setitem(
        sys.modules, "botocore", types.SimpleNamespace(UNSIGNED="unsigned")
    )
    monkeypatch.setitem(
        sys.modules, "botocore.config", types.SimpleNamespace(Config=FakeConfig)
    )
    for name in [
        "ATEABLE_S3_ENDPOINT",
        "ATEABLE_S3_PROFILE",
        "ATEABLE_S3_ANONYMOUS",
        "ATEABLE_S3_REGION",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    ]:
        monkeypatch.delenv(name, raising=False)
    return created, FakeSession


def test_worker_sdk_chain_profile_endpoint_proxy_and_bounded_retries(
    fake_sdk, monkeypatch
) -> None:
    created, _ = fake_sdk
    monkeypatch.setenv("ATEABLE_S3_ENDPOINT", "https://objects.worker.test")
    monkeypatch.setenv("ATEABLE_S3_PROFILE", "worker-role")
    source = S3Source(network_mode="proxy", proxy="http://proxy.worker.test:8080")
    source._get_client()
    assert created[0] == {"session": {"profile_name": "worker-role"}}
    assert created[1]["endpoint_url"] == "https://objects.worker.test"
    config = created[1]["config"].options
    assert config["retries"]["total_max_attempts"] == 3
    assert config["proxies"] == {
        "http": "http://proxy.worker.test:8080",
        "https": "http://proxy.worker.test:8080",
    }
    assert config["s3"] == {"addressing_style": "path"}
    assert "signature_version" not in config
    assert "aws_access_key_id" not in created[0]["session"]


@pytest.mark.parametrize(
    "explicit,aws_region,aws_default,expected",
    [
        ("eu-west-1", "us-east-2", "us-west-2", "eu-west-1"),
        ("", "us-east-2", "", "us-east-2"),
        ("", "us-east-2", "us-west-2", None),
        ("", "", "", None),
    ],
)
def test_worker_region_override_and_standard_sdk_defaults(
    fake_sdk, monkeypatch, explicit, aws_region, aws_default, expected
) -> None:
    created, _ = fake_sdk
    for name, value in [
        ("ATEABLE_S3_REGION", explicit),
        ("AWS_REGION", aws_region),
        ("AWS_DEFAULT_REGION", aws_default),
    ]:
        monkeypatch.setenv(name, value)
    S3Source()._get_client()
    assert created[0]["session"].get("region_name") == expected


@pytest.mark.parametrize(
    "setting,credentials,expected_unsigned",
    [
        ("", None, True),
        ("true", object(), True),
        ("false", None, False),
        ("", object(), False),
    ],
)
def test_anonymous_mode_is_explicit_or_missing_worker_chain(
    fake_sdk, monkeypatch, setting, credentials, expected_unsigned
) -> None:
    created, session_class = fake_sdk
    session_class.credentials = credentials
    monkeypatch.setenv("ATEABLE_S3_ANONYMOUS", setting)
    S3Source()._get_client()
    config = created[1]["config"].options
    assert (config.get("signature_version") == "unsigned") is expected_unsigned
    assert config["proxies"] == {}


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:secret@objects.test",
        "https://objects.test?secret=token",
        "ftp://objects.test",
        "https://objects.test:wrong",
    ],
)
def test_credentialed_or_invalid_worker_endpoint_is_rejected_without_sdk(
    fake_sdk, monkeypatch, endpoint
) -> None:
    created, _ = fake_sdk
    monkeypatch.setenv("ATEABLE_S3_ENDPOINT", endpoint)
    with pytest.raises(DownloadFailure) as failure:
        S3Source()._get_client()
    assert not failure.value.retryable
    assert "secret" not in str(failure.value)
    assert not created


def test_s3_dispatch_precedes_ffmpeg_and_ytdlp(tmp_path, monkeypatch) -> None:
    sentinel = object()
    calls = []

    def download(source, spec, root, progress):
        calls.append((source.network_mode, source.proxy, spec, root))
        return sentinel

    monkeypatch.setattr(S3Source, "download", download)
    monkeypatch.setattr(
        "ateable.downloader.find_ffmpeg_executable",
        lambda: pytest.fail("S3 must not require FFmpeg"),
    )
    monkeypatch.setitem(sys.modules, "yt_dlp", None)
    spec = DownloadSpec(
        "job",
        "s3://data-bucket/data.csv",
        "original",
        proxy="https://proxy.worker.test:8080",
    )
    assert YtDlpDownloader().download(spec, tmp_path, lambda _: None) is sentinel
    assert calls == [("direct", "https://proxy.worker.test:8080", spec, tmp_path)]
