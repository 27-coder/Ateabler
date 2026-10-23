from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ateable.api import create_app
from ateable.config import AppConfig
from ateable.downloader import DownloadResult
from ateable.remote_worker import (
    CoordinatorClient,
    CoordinatorError,
    RemoteWorker,
    WorkerConfig,
)


class InProcessCoordinator(CoordinatorClient):
    def __init__(self, client: TestClient):
        super().__init__("http://127.0.0.1:8765")
        self.http = client

    def request(self, method, path, *, body=None, headers=None, timeout=30):
        response = self.http.request(method, path, json=body, headers=headers)
        if response.status_code >= 400:
            raise CoordinatorError(response.status_code, response.text)
        return response.json() if response.content else None


def worker_config(tmp_path: Path, pair_key: str) -> WorkerConfig:
    return WorkerConfig(
        coordinator_url="http://127.0.0.1:8765",
        name="VM",
        pair_key=pair_key,
        worker_id=None,
        worker_token=None,
        output_dir=tmp_path / "vm" / "downloads",
        concurrency=2,
        credentials_file=tmp_path / "vm" / "state" / "credentials.json",
        proxy=None,
        network_mode="direct",
        tor_proxy="socks5h://127.0.0.1:9050",
        cookies_file=None,
    )


@pytest.mark.parametrize("inventory_failure", [False, True])
def test_remote_download_completes_on_worker_without_master_output(
    tmp_path: Path, monkeypatch, inventory_failure
):
    config = AppConfig(
        database_path=tmp_path / "master" / "state.db",
        output_dir=tmp_path / "master" / "downloads",
        admin_key="test",
    )

    class FixtureDownloader:
        def download(self, spec, output_root, progress):
            assert output_root == tmp_path / "vm" / "downloads"
            progress({"progress": 80, "stage": "downloading"})
            output = output_root / "fixture.zip"
            output.write_bytes(b"owned test data")
            return DownloadResult(str(output), "Fixture")

    with TestClient(create_app(config), headers={"X-Ateable-Key": "test"}) as master:
        pair_key = master.post("/api/pairing-keys", json={}).json()["pair_key"]
        worker = RemoteWorker(worker_config(tmp_path, pair_key), FixtureDownloader())
        worker.config.output_dir.mkdir(parents=True)
        worker.client = InProcessCoordinator(master)
        worker.connect()
        if inventory_failure:

            def fail_record(*_):
                raise PermissionError("read-only inventory state")

            monkeypatch.setattr(worker.inventory, "record", fail_record)
        created = master.post(
            "/api/batches", json={"urls": ["https://example.com/fixture.zip"]}
        ).json()
        job = worker.client.claim(worker.worker_id, worker.worker_token, 1)[0]
        worker._download(job)
        result = master.get(f"/api/jobs/{created['jobs'][0]['id']}").json()
        assert result["status"] == "completed"
        assert Path(result["output_path"]).read_bytes() == b"owned test data"
        assert not config.output_dir.exists()
        storage = master.get(f"/api/workers/{worker.worker_id}/storage").json()
        assert storage["total_downloaded_bytes"] == 15
        worker._pool.shutdown()
        worker._inventory_pool.shutdown()


def test_reconnect_refreshes_profile_and_recovers_reset_controller(tmp_path: Path):
    config = AppConfig(
        database_path=tmp_path / "master.db",
        output_dir=tmp_path / "never-created",
        admin_key="test",
    )
    with TestClient(create_app(config), headers={"X-Ateable-Key": "test"}) as master:
        pair_key = master.post("/api/pairing-keys", json={}).json()["pair_key"]
        settings = worker_config(tmp_path, pair_key)
        worker = RemoteWorker(settings)
        worker.client = InProcessCoordinator(master)
        worker.connect()
        old_id = worker.worker_id
        with master.app.state.database.connect() as connection:
            connection.execute(
                "UPDATE workers SET capabilities='[\"wav\"]',max_concurrency=1 WHERE id=?",
                (old_id,),
            )
        worker.connect()
        row = next(
            item
            for item in master.get("/api/workers").json()["items"]
            if item["id"] == old_id
        )
        assert "original" in row["capabilities"] and row["max_concurrency"] == 2
        with master.app.state.database.connect() as connection:
            connection.execute(
                "UPDATE workers SET token_hash='revoked' WHERE id=?", (old_id,)
            )
        fresh_key = master.post("/api/pairing-keys", json={}).json()["pair_key"]
        from dataclasses import replace

        worker.config = replace(worker.config, pair_key=fresh_key)
        worker.connect()
        assert worker.worker_id != old_id
        worker._pool.shutdown()
        worker._inventory_pool.shutdown()
