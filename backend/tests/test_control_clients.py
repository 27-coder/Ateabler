"""Authenticated control transports must never replay requests through redirects."""

from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from ateable.cli import ApiClient, ApiError
from ateable.desktop import ControllerClient, ControllerError
from ateable.remote_worker import CoordinatorClient, CoordinatorError

SECRET = "private-test-control-token"
CLIENTS = ("worker", "admin-cli", "admin-gui")


def client_request(kind, url, *, opener=None):
    if kind == "worker":
        client = CoordinatorClient(url, opener=opener)

        def request(method, path, body=None):
            return client.request(
                method,
                path,
                body=body,
                headers={"Authorization": "Bearer " + SECRET},
            )

        return request, CoordinatorError
    if kind == "admin-cli":
        client = ApiClient(url, SECRET, opener=opener)
        return client.request, ApiError
    client = ControllerClient(url, controller_key=SECRET, opener=opener)
    return client.request, ControllerError


@pytest.fixture(scope="module")
def control_servers():
    hits = []
    servers = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            parsed = urlsplit(self.path)
            hits.append(
                {
                    "port": self.server.server_port,
                    "path": parsed.path,
                    "method": self.command,
                    "authorization": self.headers.get("Authorization"),
                    "key": self.headers.get("X-Ateable-Key"),
                    "body": json.loads(body) if body else None,
                }
            )
            if parsed.path.startswith("/redirect-"):
                code = int(parse_qs(parsed.query)["status"][0])
                location = "/destination"
                if parsed.path == "/redirect-other":
                    location = f"http://127.0.0.1:{servers[1].server_port}" + location
                self.send_response(code)
                self.send_header("Location", location)
                content = {"detail": "Untrusted redirect response body"}
            elif parsed.path == "/error":
                self.send_response(422)
                content = {"detail": "Invalid test batch"}
            else:
                self.send_response(200)
                content = {"accepted": True}
            data = json.dumps(content).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    threads = []
    for _ in range(2):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        servers.append(server)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)
    try:
        yield f"http://127.0.0.1:{servers[0].server_port}", hits
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


@pytest.mark.parametrize("kind", CLIENTS)
@pytest.mark.parametrize("destination", ["same", "other"])
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_control_redirects_never_forward_secrets_or_repeat_requests(
    control_servers, kind, destination, method, status
):
    url, hits = control_servers
    hits.clear()
    request, error = client_request(kind, url)
    body = {"pair_key": "private-test-pairing-key"} if method == "POST" else None
    with pytest.raises(error, match="redirects are blocked") as failure:
        request(method, f"/redirect-{destination}?status={status}", body)
    assert failure.value.status == status
    assert SECRET not in str(failure.value)
    # Neither a same-origin operation nor a cross-origin destination was reached.
    assert len(hits) == 1
    assert hits[0]["path"] == f"/redirect-{destination}"
    assert hits[0]["method"] == method
    assert hits[0]["body"] == body
    if kind == "worker":
        assert hits[0]["authorization"] == "Bearer " + SECRET
    else:
        assert hits[0]["key"] == SECRET


@pytest.mark.parametrize("kind", CLIENTS)
def test_control_clients_preserve_normal_posts_and_http_errors(control_servers, kind):
    url, hits = control_servers
    hits.clear()
    request, error = client_request(kind, url)
    assert request("POST", "/ok", {"hello": "controller"}) == {"accepted": True}
    assert hits[0]["body"] == {"hello": "controller"}
    assert hits[0]["method"] == "POST"
    with pytest.raises(error, match="Invalid test batch") as failure:
        request("POST", "/error", {})
    assert failure.value.status == 422
    assert len(hits) == 2


@pytest.mark.parametrize("kind", CLIENTS)
def test_control_clients_preserve_callable_transport_injection(kind):
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        return io.BytesIO(b'{"injected":true}')

    request, _error = client_request(kind, "http://unused.invalid", opener=opener)
    assert request("POST", "/operation", {"value": 1}) == {"injected": True}
    assert len(calls) == 1
    assert calls[0][0].full_url == "http://unused.invalid/operation"
    assert json.loads(calls[0][0].data) == {"value": 1}
    assert calls[0][1] > 0


@pytest.mark.parametrize("kind", CLIENTS)
def test_control_clients_preserve_transport_error_reporting(kind):
    def unavailable(_request, timeout):
        raise URLError("Test controller unavailable")

    request, error = client_request(kind, "http://unused.invalid", opener=unavailable)
    with pytest.raises(error) as failure:
        request("POST", "/operation", {})
    assert failure.value.status is None
    assert SECRET not in str(failure.value)
