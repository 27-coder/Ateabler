from __future__ import annotations

import io
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from ateable import workspace_cli
from ateable.api import create_app
from ateable.cli import ApiClient, ApiError, build_parser, main
from ateable.config import AppConfig
from ateable.device_config import DeviceConfig
from ateable.participant import ParticipantError

ADMIN_KEY = "workspace-cli-test-admin"


@pytest.fixture
def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ATEABLE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ATEABLE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATEABLE_OUTPUT_DIR", str(tmp_path / "no-media"))
    DeviceConfig.defaults().updated(
        admin_key=ADMIN_KEY, controller_url="http://testcontroller"
    ).save()
    config = AppConfig(
        database_path=tmp_path / "controller.db",
        output_dir=tmp_path / "no-media",
        admin_key=ADMIN_KEY,
        embedded_worker=False,
    )
    recorded = []
    with TestClient(create_app(config), headers={"X-Ateable-Key": ADMIN_KEY}) as client:

        def request(self, method, path, body=None, *, admin=True):
            assert admin is True, "Workspace CLI must use administrator routes"
            recorded.append((method, path, body))
            response = client.request(
                method, path, json=body, headers={"X-Ateable-Key": self.admin_key or ""}
            )
            if response.status_code >= 400:
                raise ApiError(response.status_code, str(response.json().get("detail")))
            return response.json()

        monkeypatch.setattr(ApiClient, "request", request)
        yield client, recorded
    assert not config.output_dir.exists()


def invoke(capsys, arguments):
    result = main(["workspace", *arguments, "--json"])
    captured = capsys.readouterr()
    return result, json.loads(captured.out), captured


def create_space(controller, capsys):
    result, workspace, _ = invoke(
        capsys, ["create", "Team Lab", "--description", "Remote metadata"]
    )
    assert result == 0
    return workspace


def pair_worker(client, worker_id="vm-a"):
    key = client.post("/api/pairing-keys", json={}).json()["pair_key"]
    response = client.post(
        "/api/workers/pair",
        json={
            "pair_key": key,
            "worker_id": worker_id,
            "name": "CLI VM",
            "max_concurrency": 1,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_workspace_create_list_and_info_resolve_current_name_casefold(
    controller, capsys
):
    workspace = create_space(controller, capsys)
    assert workspace["name"] == "Team Lab"
    result, listing, _ = invoke(capsys, ["list"])
    assert result == 0 and listing["items"][0]["id"] == workspace["id"]
    result, by_name, _ = invoke(capsys, ["info", " tEAM lAB "])
    assert result == 0 and by_name["id"] == workspace["id"]
    result, by_id, _ = invoke(capsys, ["info", workspace["id"]])
    assert result == 0 and by_id == by_name
    result, error, _ = invoke(capsys, ["info", "unknown"])
    assert result == 1 and "workspace list" in error["error"]


def test_member_commands_enroll_list_and_remove_only_workspace_workers(
    controller, capsys
):
    client, _ = controller
    workspace = create_space(controller, capsys)
    pair_worker(client)
    result, membership, _ = invoke(capsys, ["member", "add", "team lab", "vm-a"])
    assert result == 0 and membership == {
        "workspace_id": workspace["id"],
        "worker_id": "vm-a",
        "enrolled": True,
    }
    result, members, _ = invoke(capsys, ["member", "list", workspace["id"]])
    assert result == 0 and members["items"][0]["id"] == "vm-a"
    result, removed, _ = invoke(capsys, ["member", "remove", "TEAM LAB", "vm-a"])
    assert result == 0 and removed["removed"] is True
    assert (
        client.get(f"/api/workspaces/{workspace['id']}/members").json()["items"] == []
    )
    result, error, _ = invoke(capsys, ["member", "add", "Team Lab", "local"])
    assert result == 1 and "worker" in error["error"].lower()


def test_agent_create_requires_destination_before_any_network_operation():
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["workspace", "agent", "create", "Team", "Assistant"])
    assert error.value.code == 2


def test_agent_token_is_saved_privately_not_printed_and_can_be_revoked(
    controller, capsys, tmp_path
):
    client, _ = controller
    workspace = create_space(controller, capsys)
    destination = tmp_path / "sessions" / "assistant.json"
    result, saved, captured = invoke(
        capsys,
        [
            "agent",
            "create",
            "TEAM LAB",
            "Assistant",
            "--session-file",
            str(destination),
            "--session-controller-url",
            "http://127.0.0.1:28765",
        ],
    )
    assert result == 0 and saved["saved"] is True
    session = json.loads(destination.read_text())
    assert set(session) == {"controller_url", "agent_id", "workspace_id", "token"}
    assert (
        session["workspace_id"] == workspace["id"]
        and session["agent_id"] == saved["agent_id"]
    )
    assert session["controller_url"] == "http://127.0.0.1:28765"
    assert session["token"] not in captured.out + captured.err
    assert "token" not in saved
    if os.name == "posix":
        assert destination.stat().st_mode & 0o777 == 0o600
    headers = {
        "Authorization": f"Bearer {session['token']}",
        "X-Ateable-Agent-ID": session["agent_id"],
    }
    assert client.get("/api/participant/workspaces", headers=headers).status_code == 200
    result, listing, captured = invoke(capsys, ["agent", "list", "Team Lab"])
    assert result == 0 and listing["items"][0]["agent_id"] == saved["agent_id"]
    assert session["token"] not in captured.out + captured.err
    result, revoked, _ = invoke(
        capsys, ["agent", "revoke", "Team Lab", saved["agent_id"]]
    )
    assert result == 0 and revoked["revoked"] is True
    assert client.get("/api/participant/workspaces", headers=headers).status_code == 401


def test_agent_session_existing_path_is_preserved_without_minting_actor(
    controller, capsys, tmp_path
):
    client, _ = controller
    workspace = create_space(controller, capsys)
    destination = tmp_path / "existing.json"
    destination.write_text("existing private session")
    result, error, _ = invoke(
        capsys,
        [
            "agent",
            "create",
            "Team Lab",
            "Assistant",
            "--session-file",
            str(destination),
        ],
    )
    assert result == 1 and "already exists" in error["error"]
    assert destination.read_text() == "existing private session"
    assert client.get(f"/api/workspaces/{workspace['id']}/agents").json()["total"] == 0


def test_failed_private_session_write_revokes_new_agent_without_echoing_token(
    controller, capsys, tmp_path, monkeypatch
):
    client, _ = controller
    workspace = create_space(controller, capsys)

    def fail_save(_path, session):
        raise ParticipantError("failed secret " + session["token"])

    monkeypatch.setattr(workspace_cli, "save_session", fail_save)
    result, error, captured = invoke(
        capsys,
        [
            "agent",
            "create",
            "Team Lab",
            "Assistant",
            "--session-file",
            str(tmp_path / "new.json"),
        ],
    )
    assert result == 1 and "was revoked" in error["error"]
    assert "awsp_" not in captured.out + captured.err
    agents = client.get(f"/api/workspaces/{workspace['id']}/agents").json()
    assert len(agents["items"]) == 1 and agents["items"][0]["revoked_at"] is not None


def test_file_and_job_metadata_queries_are_encoded_with_pagination(controller, capsys):
    _client, recorded = controller
    create_space(controller, capsys)
    result, files, _ = invoke(
        capsys,
        ["files", "Team Lab", "--query", "a&b +%?", "--limit", "9", "--offset", "2"],
    )
    assert result == 0 and files["items"] == []
    assert parse_qs(urlsplit(recorded[-1][1]).query) == {
        "q": ["a&b +%?"],
        "limit": ["9"],
        "offset": ["2"],
    }
    result, jobs, _ = invoke(
        capsys,
        [
            "jobs",
            "Team Lab",
            "--query",
            "zip&x",
            "--status",
            "completed",
            "--limit",
            "6",
            "--offset",
            "3",
        ],
    )
    assert result == 0 and jobs["items"] == []
    assert parse_qs(urlsplit(recorded[-1][1]).query) == {
        "q": ["zip&x"],
        "status": ["completed"],
        "limit": ["6"],
        "offset": ["3"],
    }


def test_message_send_read_directed_cursor_and_idempotent_replay(controller, capsys):
    client, _ = controller
    workspace = create_space(controller, capsys)
    pair_worker(client)
    invoke(capsys, ["member", "add", "Team Lab", "vm-a"])
    result, broadcast, _ = invoke(
        capsys,
        [
            "message",
            "send",
            "Team Lab",
            "Board note",
            "--idempotency-key",
            "board-note",
        ],
    )
    assert (
        result == 0
        and broadcast["sender_type"] == "admin"
        and broadcast["recipient_id"] is None
    )
    result, replay, _ = invoke(
        capsys,
        [
            "message",
            "send",
            workspace["id"],
            "--text",
            "Board note",
            "--idempotency-key",
            "board-note",
        ],
    )
    assert result == 0 and replay["id"] == broadcast["id"]
    result, direct, _ = invoke(
        capsys,
        ["message", "send", "Team Lab", "Private VM note", "--recipient-id", "vm-a"],
    )
    assert result == 0 and direct["recipient_id"] == "vm-a"
    result, history, _ = invoke(
        capsys,
        [
            "message",
            "read",
            "Team Lab",
            "--after-seq",
            str(broadcast["seq"]),
            "--recipient-id",
            "vm-a",
            "--limit",
            "10",
        ],
    )
    assert result == 0 and history["items"] == [direct]
    assert history["next_after_seq"] == direct["seq"]


def test_message_utf8_file_and_bounded_stdin_are_metadata_only(
    controller, capsys, tmp_path, monkeypatch
):
    create_space(controller, capsys)
    message_file = tmp_path / "note.txt"
    message_file.write_text("Notes\nİstanbul & research", encoding="utf-8")
    result, note, _ = invoke(
        capsys, ["message", "send", "Team Lab", "--file", str(message_file)]
    )
    assert result == 0 and note["text"] == "Notes\nİstanbul & research"
    monkeypatch.setattr("sys.stdin", io.StringIO("From stdin"))
    result, note, _ = invoke(capsys, ["message", "send", "Team Lab", "--file", "-"])
    assert result == 0 and note["text"] == "From stdin"
    monkeypatch.setattr("sys.stdin", io.StringIO("x" * 10001))
    result, error, _ = invoke(capsys, ["message", "send", "Team Lab", "--file", "-"])
    assert result == 1 and "10,000" in error["error"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["files", "Team Lab", "--limit", "0"],
        ["jobs", "Team Lab", "--offset", "-1"],
        ["message", "read", "Team Lab", "--after-seq", "-1"],
        ["message", "send", "Team Lab"],
        ["message", "send", "Team Lab", "a", "--text", "b"],
        ["message", "send", "Team Lab", " "],
    ],
)
def test_invalid_inputs_fail_before_mutating_controller(controller, capsys, arguments):
    _client, recorded = controller
    create_space(controller, capsys)
    before = len(recorded)
    result, error, _ = invoke(capsys, arguments)
    assert result == 1 and error["error"]
    assert len(recorded) == before


def test_unexpected_nested_credential_fields_are_redacted():
    assert workspace_cli._safe(
        {"items": [{"token": "private", "name": "Assistant", "worker_token": "secret"}]}
    ) == {
        "items": [
            {"token": "[redacted]", "name": "Assistant", "worker_token": "[redacted]"}
        ]
    }
