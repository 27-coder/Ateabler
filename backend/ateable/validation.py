"""Input validation and canonicalization helpers."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import parse_qs, quote, unquote, urlparse, urlunparse

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
    "youtu.be",
    "www.youtu.be",
}
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
PROXY_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}


class ValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class S3Location:
    bucket: str
    key: str

    @property
    def is_listing(self) -> bool:
        return not self.key or self.key.endswith("/")

    @property
    def url(self) -> str:
        # Encode literal query/fragment delimiters and percent signs in keys.
        return f"s3://{self.bucket}" + (
            "/" + quote(self.key, safe="/!$&'()*+,-._~:@") if self.key else ""
        )


def parse_s3_url(raw_url: str) -> S3Location:
    """Parse an S3 source locally, without importing an SDK or contacting S3."""
    candidate = raw_url.strip()
    if any(character.isspace() or ord(character) < 32 for character in candidate):
        raise ValidationError("S3 URLs must escape spaces and control characters")
    try:
        parsed = urlparse(candidate)
    except ValueError as exc:
        raise ValidationError("S3 URL has an invalid bucket") from exc
    if parsed.scheme.lower() != "s3":
        raise ValidationError("S3 source must use s3://bucket/key")
    # S3-compatible servers are selected by worker configuration, never by an
    # authority containing a password or port in the submitted source URL.
    bucket = parsed.netloc
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,254}", bucket):
        raise ValidationError("S3 URL must contain a valid bucket name")
    if parsed.query or parsed.fragment or "?" in candidate or "#" in candidate:
        raise ValidationError("Escape literal ? and # in S3 object keys")
    if re.search(r"%(?![0-9a-fA-F]{2})", parsed.path):
        raise ValidationError("S3 object key has invalid percent encoding")
    try:
        key = unquote(parsed.path[1:], encoding="utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValidationError("S3 object key must be valid UTF-8") from exc
    if any(ord(character) < 32 or ord(character) == 127 for character in key):
        raise ValidationError("S3 object keys must not contain control characters")
    if len(key.encode("utf-8")) > 1024:
        raise ValidationError("S3 object key exceeds 1024 bytes")
    return S3Location(bucket, key)


def is_s3_listing(raw_url: str) -> bool:
    """Distinguish worker-side discovery jobs from ordinary object downloads."""
    return (
        raw_url.strip().split(":", 1)[0].lower() == "s3"
        and parse_s3_url(raw_url).is_listing
    )


def normalize_download_url(raw_url: str) -> tuple[str, str]:
    """Validate a download link without fetching it; preserve signed queries."""
    candidate = raw_url.strip()
    if not candidate:
        raise ValidationError("URL is empty")
    if any(character.isspace() or ord(character) < 32 for character in candidate):
        raise ValidationError(
            "URL must not contain unescaped spaces or control characters"
        )
    if candidate.split(":", 1)[0].lower() == "s3":
        canonical = parse_s3_url(candidate).url
        return canonical, "s3:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    bare_host = "://" not in candidate
    if bare_host:
        candidate = "https://" + candidate
    try:
        parsed = urlparse(candidate)
        host = parsed.hostname
        _ = parsed.port  # Reject invalid port syntax before a worker receives it.
    except ValueError as exc:
        raise ValidationError("URL has an invalid host or port") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValidationError("Only HTTP(S) and S3 download URLs are supported")
    if not host or not parsed.netloc:
        raise ValidationError("URL must contain a host")
    if bare_host and "." not in host and host.lower() != "localhost":
        try:
            ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValidationError("Enter an HTTP(S) URL or a valid bare host") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ValidationError(
            "URLs containing usernames or passwords are not supported"
        )
    if "\\" in parsed.netloc:
        raise ValidationError("URL has an invalid host")
    if host.lower().rstrip(".") in YOUTUBE_HOSTS:
        return normalize_youtube_url(candidate)
    # A fragment is never sent in an HTTP request. Avoid treating it as a new job.
    canonical = urlunparse(parsed._replace(fragment=""))
    return canonical, "url:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_youtube_url(raw_url: str) -> tuple[str, str]:
    """Return (canonical URL, stable video id) for a YouTube video URL."""
    candidate = raw_url.strip()
    if not candidate:
        raise ValidationError("URL is empty")
    if "://" not in candidate:
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    if parsed.username is not None or parsed.password is not None:
        raise ValidationError(
            "URLs containing usernames or passwords are not supported"
        )
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValidationError("Only HTTP(S) YouTube URLs are supported")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in YOUTUBE_HOSTS:
        raise ValidationError("URL must point to youtube.com or youtu.be")
    video_id: str | None = None
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/").split("/", 1)[0]
    else:
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.path.rstrip("/") == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
        elif len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
            video_id = parts[1]
    if not video_id or not VIDEO_ID.fullmatch(video_id):
        raise ValidationError("URL does not contain a valid YouTube video id")
    return f"https://www.youtube.com/watch?v={video_id}", video_id


def validate_output_subdir(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    # Normalize Windows separators before applying platform-independent checks.
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    raw_parts = normalized.split("/")
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in raw_parts)
        or ":" in raw_parts[0]
    ):
        raise ValidationError("output_subdir must be a safe relative directory")
    return path.as_posix()


def validate_proxy(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme.lower() not in PROXY_SCHEMES or not parsed.hostname:
        raise ValidationError("Proxy must use http, https, socks4, socks5, or socks5h")
    return value
