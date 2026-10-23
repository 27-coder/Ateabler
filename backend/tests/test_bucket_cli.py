from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ateable.api import create_app
from ateable.cli import ApiClient, ApiError, main, normalize_args
from ateable.config import AppConfig
from ateable.device_config import DeviceConfig

ADMIN_KEY = "cli-bucket-test-admin"


@pytest.fixture
def cli_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ATEABLE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ATEABLE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATEABLE_OUTPUT_DIR", str(tmp_path / "media"))
    DeviceConfig.defaults().updated(
        admin_key=ADMIN_KEY, controller_url="http://testcontroller"
    ).save()
    config = AppConfig(
        database_path=tmp_path / "state" / "controller.db",
        output_dir=tmp_path / "media",
        admin_key=ADMIN_KEY,
        embedded_worker=False,
    )
    with TestClient(create_app(config), headers={"X-Ateable-Key": ADMIN_KEY}) as client:

        def request(self, method, path, body=None, *, admin=True):
            response = client.request(
                method,
                path,
                json=body,
                headers={"X-Ateable-Key": self.admin_key or ""} if admin else {},
            )
            if response.status_code >= 400:
                raise ApiError(response.status_code, str(response.json().get("detail")))
            return response.json()

        monkeypatch.setattr(ApiClient, "request", request)
        yield client


def pair_worker(client: TestClient):
    key = client.post("/api/pairing-keys", json={"expires_in_seconds": 300}).json()[
        "pair_key"
    ]
    response = client.post(
        "/api/workers/pair",
        json={
            "pair_key": key,
            "worker_id": "vm-a",
            "name": "VM A",
            "max_concurrency": 2,
        },
    )
    assert response.status_code == 200, response.text


def test_named_bucket_cli_captures_offline_and_searches_encoded_query(
    cli_client, capsys
):
    assert normalize_args(["BUCKET", "LIST", "--json"]) == ["--json", "bucket", "list"]
    assert (
        main(
            ["bucket", "create", "Reading", "--description", "Save for later", "--json"]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["name"] == "Reading" and created["description"] == "Save for later"
    assert (
        main(
            [
                "bucket",
                "add",
                "reading",
                "https://example.com/a.zip?signature=kept",
                "--title",
                "Report & notes+%",
                "--source-page",
                "https://example.com/catalog",
                "--idempotency-key",
                "capture-cli",
                "--json",
            ]
        )
        == 0
    )
    captured = json.loads(capsys.readouterr().out)
    assert captured["accepted"] == 1 and captured["bucket_id"] == created["id"]
    assert cli_client.get("/api/jobs").json()["total"] == 0
    assert not cli_client.app.state.config.output_dir.exists()
    assert main(["bucket", "items", "Reading", "--query", "& notes+%", "--json"]) == 0
    items = json.loads(capsys.readouterr().out)
    assert items["total"] == 1 and items["items"][0]["origin"] == "cli"
    assert main(["bucket", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 2


def test_cli_bucket_add_file_stdin_and_json_text_exports(
    cli_client, tmp_path, monkeypatch, capsys
):
    input_file = tmp_path / "links.txt"
    input_file.write_text(
        "# catalog\nhttps://example.com/report.zip?signature=kept\ns3://bucket/prefix/\n"
    )
    assert main(["bucket", "add", "inbox", "--file", str(input_file), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["accepted"] == 2
    assert main(["bucket", "export", "inbox"]) == 0
    text = capsys.readouterr().out
    assert text.splitlines() == [
        "https://example.com/report.zip?signature=kept",
        "s3://bucket/prefix/",
    ]
    assert main(["bucket", "export", "inbox", "--format", "json", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["items"]) == 2
    export_file = tmp_path / "export.txt"
    assert main(["bucket", "export", "--output", str(export_file), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["items"] == 2
    assert export_file.read_text() == text
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("https://example.com/stdin.zip\n"))
    assert main(["bucket", "add", "inbox", "--file", "-", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["accepted"] == 1


def test_cli_bucket_dispatch_recovers_reservation_and_only_queues_new_items(
    cli_client, capsys
):
    assert main(["bucket", "add", "inbox", "https://example.com/a.zip", "--json"]) == 0
    capsys.readouterr()
    assert (
        main(["bucket", "dispatch", "inbox", "--output-subdir", "saved", "--json"]) == 1
    )
    assert "worker" in json.loads(capsys.readouterr().out)["error"]
    active = cli_client.get("/api/buckets/inbox").json()["active_dispatch"]
    pair_worker(cli_client)
    assert (
        main(["bucket", "dispatch", "inbox", "--retry-run", active["run_id"], "--json"])
        == 0
    )
    dispatched = json.loads(capsys.readouterr().out)
    assert (
        dispatched["dispatched"] == 1
        and dispatched["options"]["output_subdir"] == "saved"
    )
    assert dispatched["jobs"][0]["target_worker_id"] == "vm-a"
    assert main(["bucket", "dispatch", "inbox", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["dispatched"] == 0
    assert (
        main(["bucket", "add", "inbox", "https://example.com/new.zip", "--json"]) == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "bucket",
                "dispatch",
                "inbox",
                "--device",
                "vm-a",
                "--limit",
                "1",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["dispatched"] == 1


def test_cli_rejects_bad_collection_url_and_master_target_without_downloading(
    cli_client, capsys
):
    assert (
        main(["bucket", "add", "missing", "https://example.com/a.zip", "--json"]) == 1
    )
    assert "Bucket was not found" in json.loads(capsys.readouterr().out)["error"]
    assert main(["bucket", "add", "inbox", "file:///secret.txt", "--json"]) == 1
    capsys.readouterr()
    assert cli_client.get("/api/buckets/inbox").json()["total"] == 0
    assert main(["bucket", "add", "inbox", "https://example.com/a.zip", "--json"]) == 0
    capsys.readouterr()
    assert main(["bucket", "dispatch", "inbox", "--device", "local", "--json"]) == 1
    assert "master cannot download" in json.loads(capsys.readouterr().out)["error"]
    assert cli_client.get("/api/jobs").json()["total"] == 0


def test_cli_explicit_retry_existing_keeps_job_identity(cli_client, capsys):
    pair_worker(cli_client)
    assert main(["bucket", "add", "inbox", "https://example.com/a.zip", "--json"]) == 0
    capsys.readouterr()
    assert main(["bucket", "dispatch", "inbox", "--json"]) == 0
    job = json.loads(capsys.readouterr().out)["jobs"][0]
    with cli_client.app.state.database.connect() as connection:
        connection.execute(
            "UPDATE jobs SET status='failed',attempts=3 WHERE id=?", (job["id"],)
        )
    assert main(["bucket", "dispatch", "inbox", "--retry-existing", "--json"]) == 0
    retried = json.loads(capsys.readouterr().out)
    assert retried["jobs"][0]["id"] == job["id"]
    assert (
        retried["jobs"][0]["status"] == "queued" and retried["jobs"][0]["attempts"] == 0
    )
