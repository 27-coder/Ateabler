from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from ateable.api import create_app
from ateable.config import AppConfig
from ateable.database import Database
from ateable.object_storage import S3Source
from ateable.remote_worker import (
    CoordinatorClient,
    CoordinatorError,
    RemoteWorker,
    WorkerConfig,
)

ADMIN = {"X-Ateable-Key": "s3-discovery-tests"}


def pair(master: TestClient, worker_id: str, concurrency: int = 16) -> dict:
    key = master.post("/api/pairing-keys", json={}).json()["pair_key"]
    result = master.post(
        "/api/workers/pair",
        json={
            "pair_key": key,
            "worker_id": worker_id,
            "name": worker_id,
            "max_concurrency": concurrency,
            "output_dir": "/srv/worker-only",
        },
    )
    assert result.status_code == 200, result.text
    return result.json()


@pytest.fixture
def master(tmp_path, monkeypatch):
    def no_bucket_client(*_args, **_kwargs):
        pytest.fail("The controller must never create a bucket client")

    monkeypatch.setattr(S3Source, "_get_client", no_bucket_client)
    config = AppConfig(
        database_path=tmp_path / "master.db",
        output_dir=tmp_path / "never-created",
        admin_key=ADMIN["X-Ateable-Key"],
    )
    with TestClient(create_app(config), headers=ADMIN) as client:
        registration = pair(client, "vm-a")
        client.worker_auth = {"Authorization": f"Bearer {registration['token']}"}
        yield client
        assert not config.output_dir.exists()


def enqueue(master, uri="s3://data-bucket/raw/", **extra) -> dict:
    result = master.post(
        "/api/batches", json={"urls": [uri], "target_worker_id": "vm-a", **extra}
    )
    assert result.status_code == 201, result.text
    return result.json()["jobs"][0]


def claim(master) -> list[dict]:
    response = master.post(
        "/api/workers/vm-a/claim", json={"limit": 16}, headers=master.worker_auth
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


def report(master, job_id, items, continuation_token=None):
    return master.post(
        f"/api/workers/vm-a/jobs/{job_id}/discover",
        headers=master.worker_auth,
        json={"items": items, "continuation_token": continuation_token},
    )


def item(key, size=7, bucket="data-bucket") -> dict:
    from ateable.validation import S3Location

    return {
        "url": S3Location(bucket, key).url,
        "key": key,
        "size_bytes": size,
        "title": key.rsplit("/", 1)[-1],
    }


def test_listing_jobs_expand_only_on_the_assigned_worker_and_have_no_file_ledger(
    master,
) -> None:
    parent = enqueue(master, output_subdir="objects/2026")
    assert parent["kind"] == "discover"
    assert parent["root_job_id"] == parent["id"]
    assert parent["task_options"] == {"limit": 5000}
    active = claim(master)[0]
    assert active["stage"] == "discovering"
    assert active["task_options"] == {"limit": 5000}
    assert (
        master.post(
            f"/api/workers/vm-a/jobs/{parent['id']}/complete",
            headers=master.worker_auth,
            json={"output_path": "/srv/fake-file"},
        ).status_code
        == 409
    )
    result = report(
        master,
        parent["id"],
        [item("raw/A ?#%.PDF"), item("raw/empty.bin", 0)],
        "opaque-next+/=",
    )
    assert result.status_code == 200, result.text
    response = result.json()
    assert response["added_count"] == 2
    assert response["job"]["stage"] == "expanded"
    assert response["job"]["discovery_count"] == 2
    assert response["job"]["status"] == "completed"
    assert response["job"]["output_path"] is None
    children = claim(master)
    downloads = [job for job in children if job["kind"] == "download"]
    continuation = next(job for job in children if job["kind"] == "discover")
    assert len(downloads) == 2
    assert continuation["id"] == response["continuation_job_id"]
    assert continuation["task_options"]["continuation_token"] == "opaque-next+/="
    assert all(
        job["target_worker_id"] == "vm-a" and job["format"] == "original"
        for job in children
    )
    assert all(
        job["source_job_id"] == parent["id"] and job["root_job_id"] == parent["id"]
        for job in children
    )
    assert all(job["output_subdir"] == "objects/2026" for job in children)
    assert "raw/A%20%3F%23%25.PDF" in next(
        job["canonical_url"] for job in downloads if "PDF" in job["canonical_url"]
    )
    storage = master.get("/api/workers/vm-a/storage").json()
    assert storage["completed_files"] == 0


def test_durable_object_dedupe_and_report_replay_survive_completed_queue_cleanup(
    master,
) -> None:
    parent = enqueue(master)
    claim(master)
    first_items = [item("raw/old.zip")]
    first = report(master, parent["id"], first_items, "page-2").json()
    children = claim(master)
    old = next(job for job in children if job["kind"] == "download")
    second_page = next(job for job in children if job["kind"] == "discover")
    assert (
        master.post(
            f"/api/workers/vm-a/jobs/{old['id']}/complete",
            headers=master.worker_auth,
            json={"output_path": "/srv/old.zip", "size_bytes": 7},
        ).status_code
        == 200
    )
    assert master.post("/api/jobs/clear-completed").json()["deleted"] == 2
    assert report(master, parent["id"], first_items, "page-2").json() == first
    assert (
        report(master, parent["id"], [item("raw/changed.zip")], "page-2").status_code
        == 409
    )
    second = report(
        master, second_page["id"], [item("raw/old.zip"), item("raw/new.zip")]
    )
    assert second.status_code == 200, second.text
    assert second.json()["added_count"] == 1
    pending = claim(master)
    assert len(pending) == 1
    assert pending[0]["canonical_url"] == "s3://data-bucket/raw/new.zip"
    assert pending[0]["root_job_id"] == parent["id"]
    assert pending[0]["source_job_id"] == second_page["id"]
    storage = master.get("/api/workers/vm-a/storage").json()
    assert storage["completed_files"] == 1 and storage["total_downloaded_bytes"] == 7
    with master.app.state.database.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM discovered_objects").fetchone()[0]
            == 2
        )


def test_explicit_same_batch_object_is_not_recreated_by_later_listing_after_cleanup(
    master,
) -> None:
    created = master.post(
        "/api/batches",
        json={
            "urls": ["s3://data-bucket/raw/", "s3://data-bucket/raw/explicit.zip"],
            "target_worker_id": "vm-a",
        },
    )
    assert created.status_code == 201, created.text
    jobs = claim(master)
    parent = next(job for job in jobs if job["kind"] == "discover")
    exact = next(job for job in jobs if job["kind"] == "download")
    complete = master.post(
        f"/api/workers/vm-a/jobs/{exact['id']}/complete",
        headers=master.worker_auth,
        json={"output_path": "/srv/explicit.zip", "size_bytes": 11},
    )
    assert complete.status_code == 200
    assert master.post("/api/jobs/clear-completed").json()["deleted"] == 1
    expanded = report(master, parent["id"], [item("raw/explicit.zip", 11)])
    assert expanded.status_code == 200, expanded.text
    assert expanded.json()["added_count"] == 0
    assert expanded.json()["job"]["discovery_count"] == 1
    assert master.get("/api/jobs").json()["total"] == 1
    assert master.get("/api/workers/vm-a/storage").json()["completed_files"] == 1


@pytest.mark.parametrize(
    "bad",
    [
        item("raw/foreign.zip", bucket="other-bucket"),
        item("outside/file.zip"),
        item("raw/prefix/"),
        {**item("raw/key.zip"), "key": "raw/wrong-key.zip"},
        {**item("raw/key.zip"), "url": "https://files.test/raw/key.zip"},
    ],
)
def test_invalid_discovery_reports_roll_back_all_children(master, bad) -> None:
    parent = enqueue(master)
    claim(master)
    result = report(master, parent["id"], [item("raw/valid.zip"), bad], "next")
    assert result.status_code == 422, result.text
    jobs = master.get("/api/jobs").json()["items"]
    assert len(jobs) == 1 and jobs[0]["status"] == "downloading"
    with master.app.state.database.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM discovered_objects").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM discovery_reports").fetchone()[0]
            == 0
        )


def test_worker_scope_and_cancelled_parent_prevent_expansion(master) -> None:
    parent = enqueue(master)
    claim(master)
    other = pair(master, "vm-b")
    assert (
        master.post(
            f"/api/workers/vm-a/jobs/{parent['id']}/discover",
            headers={"Authorization": f"Bearer {other['token']}"},
            json={"items": []},
        ).status_code
        == 401
    )
    assert (
        master.post(
            f"/api/workers/vm-b/jobs/{parent['id']}/discover",
            headers={"Authorization": f"Bearer {other['token']}"},
            json={"items": []},
        ).status_code
        == 409
    )
    assert master.post(f"/api/jobs/{parent['id']}/cancel").status_code == 200
    assert report(master, parent["id"], [item("raw/late.zip")]).status_code == 409
    assert master.get("/api/jobs").json()["total"] == 1


@pytest.mark.parametrize(
    "uri", ["s3://data-bucket/raw/", "s3://data-bucket/raw/model.bin"]
)
def test_s3_conversion_is_rejected_before_queueing(master, uri) -> None:
    response = master.post(
        "/api/batches",
        json={"urls": [uri], "format": "wav", "target_worker_id": "vm-a"},
    )
    assert response.status_code == 422
    assert master.get("/api/jobs").json()["total"] == 0


def test_exact_object_is_an_ordinary_download_and_cannot_report_discovery(
    master,
) -> None:
    parent = enqueue(master, "s3://data-bucket/raw/exact.bin")
    assert parent["kind"] == "download"
    assert parent["task_options"] == {}
    claim(master)
    assert report(master, parent["id"], []).status_code == 409


def test_discovery_report_limits_and_strict_metadata_are_enforced(master) -> None:
    parent = enqueue(master)
    claim(master)
    assert (
        report(master, parent["id"], [item("raw/file.bin")] * 5001).status_code == 422
    )
    assert (
        report(
            master, parent["id"], [{**item("raw/file.bin"), "size_bytes": True}]
        ).status_code
        == 422
    )
    assert (
        report(
            master,
            parent["id"],
            [{**item("raw/file.bin"), "aws_secret_access_key": "rejected"}],
        ).status_code
        == 422
    )
    assert (
        report(
            master, parent["id"], [{**item("raw/file.bin"), "size_bytes": 2**63}]
        ).status_code
        == 422
    )
    assert master.get("/api/jobs").json()["total"] == 1


def test_concurrent_report_replays_create_children_once(master) -> None:
    parent = enqueue(master)
    claim(master)
    barrier = Barrier(2)

    def submit():
        barrier.wait()
        response = report(
            master, parent["id"], [item("raw/a.bin"), item("raw/a.bin")], "next"
        )
        assert response.status_code == 200, response.text
        return response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: submit(), range(2)))
    assert responses[0] == responses[1]
    assert responses[0]["added_count"] == 1
    assert master.get("/api/jobs").json()["total"] == 3


def test_repeating_a_continuation_token_cannot_loop_or_partially_add_children(
    master,
) -> None:
    first = enqueue(master)
    claim(master)
    report(master, first["id"], [], "token-a")
    second = claim(master)[0]
    report(master, second["id"], [], "token-b")
    third = claim(master)[0]
    assert (
        report(
            master, third["id"], [item("raw/never-added.bin")], "token-a"
        ).status_code
        == 422
    )
    assert master.get("/api/jobs").json()["total"] == 3


def test_job_schema_migration_retains_existing_downloads(tmp_path) -> None:
    config = AppConfig(
        database_path=tmp_path / "legacy.db",
        output_dir=tmp_path / "unused",
        admin_key="migration",
    )
    database = Database(config)
    database.initialize()
    # Simulate a previously initialized database with the pre-discovery schema.
    with database.connect() as connection:
        for column in [
            "kind",
            "task_options",
            "source_job_id",
            "root_job_id",
            "discovery_count",
        ]:
            connection.execute(f"ALTER TABLE jobs DROP COLUMN {column}")
    database.initialize()
    with database.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
    assert {
        "kind",
        "task_options",
        "source_job_id",
        "root_job_id",
        "discovery_count",
    } <= columns
    assert not config.output_dir.exists()


class InProcessCoordinator(CoordinatorClient):
    def __init__(self, http):
        super().__init__("http://127.0.0.1:8765")
        self.http = http

    def request(self, method, path, *, body=None, headers=None, timeout=30):
        response = self.http.request(method, path, json=body, headers=headers)
        if response.status_code >= 400:
            raise CoordinatorError(response.status_code, response.text)
        return response.json() if response.content else None


def test_remote_worker_lists_on_vm_reports_metadata_and_never_records_a_file(
    tmp_path, monkeypatch
) -> None:
    config = AppConfig(
        database_path=tmp_path / "controller.db",
        output_dir=tmp_path / "controller-output",
        admin_key=ADMIN["X-Ateable-Key"],
    )
    calls = []

    class ListingClient:
        def list_objects_v2(self, **kwargs):
            calls.append(kwargs)
            return {
                "Contents": [{"Key": "raw/data.bin", "Size": 25}],
                "IsTruncated": False,
            }

    class NoDownload:
        def download(self, *_):
            pytest.fail("Discovery must not download object bytes")

    monkeypatch.setattr(S3Source, "_get_client", lambda _: ListingClient())
    with TestClient(create_app(config), headers=ADMIN) as master:
        key = master.post("/api/pairing-keys", json={}).json()["pair_key"]
        worker = RemoteWorker(
            WorkerConfig(
                coordinator_url="http://127.0.0.1:8765",
                name="VM",
                pair_key=key,
                worker_id=None,
                worker_token=None,
                output_dir=tmp_path / "vm-output",
                concurrency=1,
                credentials_file=tmp_path / "fake-state/credentials.json",
                proxy=None,
                network_mode="direct",
                tor_proxy="socks5h://127.0.0.1:9050",
                cookies_file=None,
            ),
            NoDownload(),
        )
        worker.client = InProcessCoordinator(master)
        worker.connect()
        monkeypatch.setattr(
            worker.inventory,
            "record",
            lambda *_: pytest.fail("Discovery has no file inventory entry"),
        )
        response = master.post(
            "/api/batches",
            json={
                "urls": ["s3://data-bucket/raw/"],
                "target_worker_id": worker.worker_id,
            },
        )
        assert response.status_code == 201, response.text
        job = worker.client.claim(worker.worker_id, worker.worker_token, 1)[0]
        worker._download(job)
        result = master.get(f"/api/jobs/{job['id']}").json()
        assert result["stage"] == "expanded" and result["discovery_count"] == 1
        assert calls == [{"Bucket": "data-bucket", "Prefix": "raw/", "MaxKeys": 1000}]
        assert not config.output_dir.exists()
        assert not worker.config.output_dir.exists()
        assert (
            master.get(f"/api/workers/{worker.worker_id}/storage").json()[
                "completed_files"
            ]
            == 0
        )
        worker._pool.shutdown()
        worker._inventory_pool.shutdown()


def test_lost_discovery_response_is_replayed_without_duplicate_jobs(
    master, monkeypatch
) -> None:
    parent = enqueue(master)
    claim(master)
    token = master.worker_auth["Authorization"].removeprefix("Bearer ")

    class LostResponse(InProcessCoordinator):
        lost = False

        def request(self, method, path, **kwargs):
            result = super().request(method, path, **kwargs)
            if path.endswith("/discover") and not self.lost:
                self.lost = True
                raise CoordinatorError(None, "Simulated response loss after commit")
            return result

    monkeypatch.setattr("ateable.remote_worker.time.sleep", lambda _: None)
    client = LostResponse(master)
    result = client.discover("vm-a", token, parent["id"], [item("raw/once.bin")])
    assert result["added_count"] == 1
    assert master.get("/api/jobs").json()["total"] == 2
