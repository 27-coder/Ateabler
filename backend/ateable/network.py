"""Worker network modes and non-invasive connectivity probes."""

from __future__ import annotations

import json
import socket
import ssl
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, build_opener

NETWORK_MODES = ("direct", "proxy", "tor")
DEFAULT_TOR_PROXY = "socks5h://127.0.0.1:9050"


class _NoControlRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Control requests carry admin keys, worker tokens or pairing secrets.
        # Even a same-origin redirect could replay a state-changing operation.
        return None


def control_request_opener() -> Any:
    """Create a private control transport without changing default proxy policy."""
    return build_opener(_NoControlRedirects()).open


@dataclass(frozen=True, slots=True)
class NetworkStatus:
    mode: str
    proxy: str | None
    reachable: bool
    message: str

    def to_dict(self) -> dict[str, str | bool | None]:
        return asdict(self)


def effective_proxy(
    mode: str, proxy: str | None, tor_proxy: str = DEFAULT_TOR_PROXY
) -> str | None:
    if mode == "direct":
        return None
    if mode == "proxy":
        if not proxy:
            raise ValueError("Proxy network mode requires an explicit proxy URL")
        return proxy
    if mode == "tor":
        parsed = urlparse(tor_proxy)
        if parsed.scheme.lower() != "socks5h" or not parsed.hostname or not parsed.port:
            raise ValueError("Tor proxy must be an explicit socks5h://host:port URL")
        return tor_proxy
    raise ValueError(f"Unsupported network mode {mode!r}")


def redact_proxy(value: str | None) -> str | None:
    if not value:
        return value
    parsed = urlparse(value)
    if parsed.username is None and parsed.password is None:
        return value
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://***@{host}{port}"


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        chunk = sock.recv(count - len(chunks))
        if not chunk:
            raise OSError("SOCKS proxy closed the connection")
        chunks.extend(chunk)
    return bytes(chunks)


def _tor_api_probe(proxy: str, timeout: float) -> bool:
    parsed = urlparse(proxy)
    if parsed.scheme != "socks5h" or not parsed.hostname or not parsed.port:
        raise ValueError("Tor proxy must be an explicit socks5h://host:port URL")
    target_host = "check.torproject.org"
    with socket.create_connection(
        (parsed.hostname, parsed.port), timeout=timeout
    ) as raw:
        raw.settimeout(timeout)
        if parsed.username is not None or parsed.password is not None:
            raw.sendall(b"\x05\x01\x02")
            if _recv_exact(raw, 2) != b"\x05\x02":
                raise OSError("SOCKS proxy rejected username/password authentication")
            username = (parsed.username or "").encode()
            password = (parsed.password or "").encode()
            if len(username) > 255 or len(password) > 255:
                raise ValueError("SOCKS credentials are too long")
            raw.sendall(
                b"\x01"
                + bytes([len(username)])
                + username
                + bytes([len(password)])
                + password
            )
            if _recv_exact(raw, 2) != b"\x01\x00":
                raise OSError("SOCKS authentication failed")
        else:
            raw.sendall(b"\x05\x01\x00")
            if _recv_exact(raw, 2) != b"\x05\x00":
                raise OSError("SOCKS proxy rejected no-authentication mode")
        encoded_host = target_host.encode("ascii")
        raw.sendall(
            b"\x05\x01\x00\x03"
            + bytes([len(encoded_host)])
            + encoded_host
            + (443).to_bytes(2, "big")
        )
        response = _recv_exact(raw, 4)
        if response[:2] != b"\x05\x00":
            raise OSError(f"SOCKS CONNECT failed with code {response[1]}")
        address_type = response[3]
        if address_type == 1:
            _recv_exact(raw, 4)
        elif address_type == 3:
            _recv_exact(raw, _recv_exact(raw, 1)[0])
        elif address_type == 4:
            _recv_exact(raw, 16)
        else:
            raise OSError("SOCKS proxy returned an unknown address type")
        _recv_exact(raw, 2)
        context = ssl.create_default_context()
        with context.wrap_socket(raw, server_hostname=target_host) as secure:
            secure.settimeout(timeout)
            secure.sendall(
                (
                    "GET /api/ip HTTP/1.1\r\n"
                    f"Host: {target_host}\r\n"
                    "User-Agent: Ateable/0.1\r\n"
                    "Accept: application/json\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
            )
            response_bytes = bytearray()
            while len(response_bytes) < 65536:
                chunk = secure.recv(8192)
                if not chunk:
                    break
                response_bytes.extend(chunk)
    header, separator, body = bytes(response_bytes).partition(b"\r\n\r\n")
    if not separator or b" 200 " not in header.splitlines()[0]:
        raise OSError("Tor verification endpoint returned an invalid response")
    # The small response is normally plain JSON. Handle chunk framing by locating
    # the JSON object without ever exposing the returned public IP.
    start, end = body.find(b"{"), body.rfind(b"}")
    if start < 0 or end < start:
        raise OSError("Tor verification endpoint returned no JSON")
    payload = json.loads(body[start : end + 1])
    return payload.get("IsTor") is True


def probe_network(
    mode: str,
    proxy: str | None = None,
    tor_proxy: str = DEFAULT_TOR_PROXY,
    *,
    timeout: float = 10.0,
) -> NetworkStatus:
    selected = effective_proxy(mode, proxy, tor_proxy)
    if selected is None:
        return NetworkStatus(
            mode="direct",
            proxy=None,
            reachable=True,
            message="Direct networking selected; no proxy probe required",
        )
    parsed = urlparse(selected)
    host, port = parsed.hostname, parsed.port
    if not host or not port:
        return NetworkStatus(mode, selected, False, "Proxy URL has no host or port")
    display_proxy = redact_proxy(selected)
    try:
        if mode == "tor":
            verified = _tor_api_probe(selected, timeout)
            if not verified:
                return NetworkStatus(
                    mode,
                    display_proxy,
                    False,
                    "SOCKS endpoint is reachable but is not using Tor",
                )
        else:
            with socket.create_connection((host, port), timeout=timeout):
                pass
    except (OSError, ValueError, ssl.SSLError, json.JSONDecodeError) as exc:
        return NetworkStatus(
            mode,
            display_proxy,
            False,
            f"Proxy endpoint is unreachable: {exc}",
        )
    label = "Tor SOCKS endpoint" if mode == "tor" else "Proxy endpoint"
    message = (
        "Tor circuit verified by check.torproject.org"
        if mode == "tor"
        else f"{label} is reachable"
    )
    return NetworkStatus(mode, display_proxy, True, message)
