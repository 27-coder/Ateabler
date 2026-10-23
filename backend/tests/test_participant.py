from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from ateable import participant
from ateable.api import create_app
from ateable.config import AppConfig
from ateable.participant import ParticipantClient, ParticipantError, ParticipantIdentity


class Response:
    def __init__(self, url, value):
        self.url = url
        self.data = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.read_limit = None

    def geturl(self):
        return self.url

    def read(self, limit):
        self.read_limit = limit
        return self.data[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Opener:
    def __init__(self, values=()):
        self.values = iter(values)
        self.requests = []
        self.responses = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        value = next(self.values, {"items": [], "total": 0})
        if isinstance(value, Exception):
            raise value
        response = Response(request.full_url, value)
        self.responses.append(response)
        return response


def identity(kind="worker"):
    return ParticipantIdentity(
        "https://controller.test",
        "vm-a" if kind == "worker" else "agent-a",
        kind,
        "DO-NOT-PRINT-BEARER",
        "space-a" if kind == "agent" else None,
    )


@pytest.mark.parametrize(
    "kind,expected",
    [("worker", "x-ateable-worker-id"), ("agent", "x-ateable-agent-id")],
)
def test_scoped_headers_and_routes_never_include_admin_auth(kind, expected):
    opener = Opener()
    client = ParticipantClient(identity(kind), opener=opener)
    client.workspaces()
    client.members("space-a")
    client.files("space-a", q="a & b", limit=10, offset=2)
    client.jobs("space-a")
    for request, timeout in opener.requests:
        headers = {key.lower(): value for key, value in request.header_items()}
        assert headers[expected] == client.identity.actor_id
        assert headers["authorization"] == "Bearer DO-NOT-PRINT-BEARER"
        assert "x-ateable-key" not in headers
        assert (
            len(
                [
                    key
                    for key in headers
                    if key in {"x-ateable-agent-id", "x-ateable-worker-id"}
                ]
            )
            == 1
        )
        assert urlsplit(request.full_url).path.startswith("/api/participant/")
        assert timeout == 20
    assert "q=a+%26+b" in opener.requests[2][0].full_url
    with pytest.raises(ParticipantError):
        client._request("GET", "/api/workers")
    assert len(opener.requests) == 4
    assert "DO-NOT-PRINT-BEARER" not in repr(client.identity)
    assert "token" not in client.identity.safe_info()


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/key",
        "https://user:secret@controller.test",
        "https://controller.test?token=secret",
        "https://controller.test#secret",
        "https://controller.test:wrong",
        "https://controller.test\n",
    ],
)
def test_bad_controller_urls_are_rejected_without_exposing_secrets(url):
    with pytest.raises(ParticipantError) as failure:
        ParticipantIdentity(url, "vm-a", "worker", "token")
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize(
    "error",
    [
        HTTPError(
            "https://controller.test?secret=key",
            403,
            "DO-NOT-PRINT-BEARER",
            None,
            io.BytesIO(b"DO-NOT-PRINT-BEARER"),
        ),
        URLError("DO-NOT-PRINT-BEARER"),
    ],
)
def test_errors_do_not_expose_response_body_url_or_tokens(error):
    with pytest.raises(ParticipantError) as failure:
        ParticipantClient(identity(), opener=Opener([error])).workspaces()
    assert "DO-NOT-PRINT" not in str(failure.value)
    assert "secret" not in str(failure.value)
    assert failure.value.__cause__ is None


def test_redirect_is_rejected_and_credentials_are_never_forwarded():
    forwarded = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/participant/"):
                self.send_response(302)
                self.send_header("Location", "/would-leak-token")
                self.end_headers()
            else:
                forwarded.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

        def log_message(self, *_):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = ParticipantClient(
            ParticipantIdentity(
                f"http://127.0.0.1:{server.server_port}",
                "vm-a",
                "worker",
                "DO-NOT-PRINT-BEARER",
            )
        )
        with pytest.raises(ParticipantError, match="redirects"):
            client.workspaces()
        assert forwarded == []
    finally:
        server.shutdown()
        thread.join(2)
        server.server_close()


def test_response_size_and_json_shape_are_bounded(monkeypatch):
    monkeypatch.setattr(participant, "MAX_RESPONSE_BYTES", 16)
    opener = Opener([b"x" * 17])
    with pytest.raises(ParticipantError, match="size limit"):
        ParticipantClient(identity(), opener=opener).workspaces()
    assert opener.responses[0].read_limit == 17
    for value in [b"invalid JSON DO-NOT-PRINT-BEARER", b"[]"]:
        with pytest.raises(ParticipantError):
            ParticipantClient(identity(), opener=Opener([value])).workspaces()


def test_atomic_private_session_and_worker_autodetection(tmp_path, monkeypatch):
    session = tmp_path / "agent/session.json"
    participant.save_session(
        session,
        {
            "controller_url": "https://controller.test/",
            "agent_id": "agent-a",
            "token": "DO-NOT-PRINT-BEARER",
            "workspace_id": "space-a",
        },
    )
    loaded = participant.load_identity(session_file=session)
    assert loaded.actor_type == "agent" and loaded.workspace_id == "space-a"
    assert loaded.controller_url == "https://controller.test"
    if os.name != "nt":
        assert session.stat().st_mode & 0o777 == 0o600
    assert not list(session.parent.glob(".node-session-*"))
    worker = tmp_path / "worker-credentials.json"
    worker.write_text(
        json.dumps(
            {
                "coordinator_url": "http://127.0.0.1:8765",
                "worker_id": "vm-a",
                "token": "fake-worker-token",
            }
        )
    )
    monkeypatch.delenv("ATEABLE_WORKER_CREDENTIALS", raising=False)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "runtime"))
    assert participant.default_credentials_file() == worker
    assert participant.load_identity().actor_type == "worker"
    monkeypatch.setenv("ATEABLE_WORKER_CREDENTIALS", str(worker))
    assert participant.load_identity(
        controller_url="http://127.0.0.1:9988"
    ).controller_url.endswith(":9988")
    with pytest.raises(ParticipantError):
        participant.load_identity(session_file=session, credentials_file=worker)


def test_invalid_session_never_overwrites_existing_file(tmp_path):
    path = tmp_path / "session.json"
    path.write_bytes(b"previous")
    with pytest.raises(ParticipantError):
        participant.save_session(
            path,
            {
                "controller_url": "https://user:secret@controller.test",
                "agent_id": "a",
                "token": "fake",
                "workspace_id": "space",
            },
        )
    assert path.read_bytes() == b"previous"
    with pytest.raises(ParticipantError):
        participant.save_session(
            path,
            {
                "controller_url": "https://controller.test",
                "agent_id": "a",
                "token": "fake",
                "workspace_id": "space",
                "admin_key": "not-accepted",
            },
        )
    assert path.read_bytes() == b"previous"


def test_failed_atomic_replace_cleans_private_temporary(tmp_path, monkeypatch):
    path = tmp_path / "session.json"
    path.write_bytes(b"previous")
    monkeypatch.setattr(
        participant.os,
        "replace",
        lambda *_: (_ for _ in ()).throw(PermissionError("secret-bearing OS error")),
    )
    with pytest.raises(ParticipantError, match="could not be saved"):
        participant.save_session(
            path,
            {
                "controller_url": "https://controller.test",
                "agent_id": "a",
                "token": "fake",
                "workspace_id": "space",
            },
        )
    assert path.read_bytes() == b"previous"
    assert not list(tmp_path.glob(".node-session-*"))


def test_download_is_a_remote_request_preserves_signed_query_and_original_bytes_contract():
    opener = Opener()
    client = ParticipantClient(identity(), opener=opener)
    source = "https://files.test/data.zip?X-Amz-Signature=Ab%2Fcd&x=1&x=2"
    client.downloads(
        "space-a",
        [source],
        target_worker_id="vm-a",
        output_subdir="datasets",
        idempotency_key="download-once",
    )
    request = opener.requests[0][0]
    assert (
        request.full_url
        == "https://controller.test/api/participant/workspaces/space-a/downloads"
    )
    assert json.loads(request.data) == {
        "urls": [source],
        "format": "original",
        "target_worker_id": "vm-a",
        "output_subdir": "datasets",
        "idempotency_key": "download-once",
    }
    assert len(opener.requests) == 1


def test_message_cursor_advances_without_visible_messages():
    client = ParticipantClient(
        identity(),
        opener=Opener(
            [{"items": [], "next_after_seq": 8}, {"items": [], "next_after_seq": 7}]
        ),
    )
    assert client.messages("space-a", after_seq=3)["next_after_seq"] == 8
    with pytest.raises(ParticipantError, match="cursor"):
        client.messages("space-a", after_seq=8)


def test_cli_login_reads_token_only_from_stdin_and_outputs_safe_identity(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "session.json"
    monkeypatch.setattr(sys, "stdin", io.StringIO("DO-NOT-PRINT-BEARER\n"))
    assert (
        participant.main(
            [
                "--json",
                "--session-file",
                str(path),
                "--controller-url",
                "https://controller.test",
                "login",
                "--agent-id",
                "agent-a",
                "--workspace-id",
                "space-a",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    assert "DO-NOT-PRINT" not in output.out + output.err
    assert participant.main(["--json", "--session-file", str(path), "identity"]) == 0
    safe = json.loads(capsys.readouterr().out)
    assert safe["type"] == "agent" and "token" not in safe
    with pytest.raises(SystemExit):
        participant.main(
            [
                "login",
                "--agent-id",
                "agent-a",
                "--workspace-id",
                "space-a",
                "--token",
                "DO-NOT-PRINT-BEARER",
            ]
        )
    assert "DO-NOT-PRINT" not in capsys.readouterr().err


def test_cli_watch_tracks_scanned_cursor_and_never_sends(monkeypatch, capsys):
    calls = []

    class Client:
        def __init__(self, _):
            pass

        def messages(self, workspace, *, after_seq, limit):
            calls.append((workspace, after_seq, limit))
            return {"items": [], "next_after_seq": after_seq + 5, "has_more": False}

    monkeypatch.setattr(participant, "load_identity", lambda **_: identity("agent"))
    monkeypatch.setattr(participant, "ParticipantClient", Client)
    monkeypatch.setattr(participant.time, "sleep", lambda _: None)
    assert participant.main(["--json", "message", "watch", "--count", "3"]) == 0
    assert [call[1] for call in calls] == [0, 5, 10]
    assert [
        json.loads(line)["next_after_seq"]
        for line in capsys.readouterr().out.splitlines()
    ] == [5, 10, 15]


def test_headless_import_has_no_qt_controller_or_downloader_imports():
    code = "import sys; import ateable.participant; print(any(name.startswith(('PySide6', 'ateable.desktop', 'ateable.api', 'ateable.downloader')) for name in sys.modules))"
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stdout.strip() == "False"


class InProcessOpener:
    def __init__(self, http):
        self.http = http
        self.paths = []

    def open(self, request, timeout):
        parsed = urlsplit(request.full_url)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        self.paths.append(path)
        response = self.http.request(
            request.method,
            path,
            content=request.data,
            headers=dict(request.header_items()),
        )
        if response.status_code >= 400:
            raise HTTPError(
                request.full_url,
                response.status_code,
                "error",
                None,
                io.BytesIO(response.content),
            )
        return Response(request.full_url, response.content)


def test_participant_client_integrates_scoped_workspace_catalog_messages_and_downloads(
    tmp_path,
):
    admin = {"X-Ateable-Key": "participant-integration"}
    config = AppConfig(
        database_path=tmp_path / "master.db",
        output_dir=tmp_path / "never-download-here",
        admin_key=admin["X-Ateable-Key"],
    )
    with TestClient(create_app(config)) as http:
        key = http.post("/api/pairing-keys", json={}, headers=admin).json()["pair_key"]
        worker = http.post(
            "/api/workers/pair",
            json={"pair_key": key, "worker_id": "vm-a", "name": "VM A"},
        ).json()
        workspace = http.post(
            "/api/workspaces", json={"name": "Research"}, headers=admin
        ).json()
        workspace_id = workspace["id"]
        assert (
            http.post(
                f"/api/workspaces/{workspace_id}/members",
                json={"worker_id": worker["id"]},
                headers=admin,
            ).status_code
            == 201
        )
        agent = http.post(
            f"/api/workspaces/{workspace_id}/agents",
            json={"name": "Analyst"},
            headers=admin,
        ).json()
        opener = InProcessOpener(http)
        client = ParticipantClient(
            ParticipantIdentity(
                "http://controller.test",
                agent["agent_id"],
                "agent",
                agent["token"],
                workspace_id,
            ),
            opener=opener,
        )
        assert client.workspaces()["actor"]["type"] == "agent"
        assert client.members(workspace_id)["items"][0]["id"] == "vm-a"
        assert client.files(workspace_id)["items"] == []
        request = client.downloads(
            workspace_id,
            ["https://files.test/data.zip"],
            idempotency_key="request-once",
        )
        assert request["dispatched"] == 1
        assert client.downloads(
            workspace_id,
            ["https://files.test/data.zip"],
            idempotency_key="request-once",
        )["replayed"]
        assert client.jobs(workspace_id)["total"] == 1
        message = client.send_message(
            workspace_id, "Synthetic scoped test", idempotency_key="message-once"
        )
        page = client.messages(workspace_id)
        assert page["items"][0]["id"] == message["id"]
        assert all(path.startswith("/api/participant/") for path in opener.paths)
        assert not config.output_dir.exists()
        assert (
            http.delete(
                f"/api/workspaces/{workspace_id}/agents/{agent['agent_id']}",
                headers=admin,
            ).status_code
            == 200
        )
        with pytest.raises(ParticipantError, match="rejected"):
            client.workspaces()
