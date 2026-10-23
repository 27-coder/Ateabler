from __future__ import annotations

import pytest

from ateable.validation import ValidationError, normalize_download_url


def test_signed_file_query_survives_without_fetching() -> None:
    source = "https://files.example.test/archive.zip?X-Amz-Signature=A%2Fb&x=1&x=2"
    canonical, key = normalize_download_url(source + "#download")
    assert canonical == source
    assert key == normalize_download_url(source)[1]
    assert key != normalize_download_url(source + "&x=3")[1]


def test_generic_site_and_bare_host_are_accepted() -> None:
    assert normalize_download_url("vimeo.com/123456")[0] == "https://vimeo.com/123456"
    assert normalize_download_url("http://127.0.0.1:8080/report.pdf")[0].endswith(
        "/report.pdf"
    )
    assert (
        normalize_download_url("http://intranet/report.pdf")[0]
        == "http://intranet/report.pdf"
    )


def test_generalized_normalization_keeps_youtube_dedupe() -> None:
    assert normalize_download_url("https://youtu.be/abcdefghijk?t=20") == (
        "https://www.youtube.com/watch?v=abcdefghijk",
        "abcdefghijk",
    )


@pytest.mark.parametrize(
    "source",
    [
        "file:///tmp/secret",
        "ftp://files.example.test/archive",
        "javascript:alert(1)",
        "https://user:secret@example.test/a.zip",
        "https://example.test:wrong/file",
        "https:///file",
        "https://example.test/with a space",
        "https://example.test/line\nbreak",
        "",
        "https://youtube.com/playlist?list=abcdef",
        "not-a-url",
        "these are notes",
    ],
)
def test_invalid_generalized_urls_are_rejected(source) -> None:
    with pytest.raises(ValidationError):
        normalize_download_url(source)
