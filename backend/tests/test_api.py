from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from ateable.api import create_app
from ateable.config import AppConfig
from ateable.database import ConflictError
from ateable.downloader import DownloadResult, DownloadSpec
from ateable.pairing import decode_pair_key, encode_pair_key

ADMIN_KEY = "test-admin-key-with-enough-entropy"
ADMIN_HEADERS = {"X-Ateable-Key": ADMIN_KEY}


class FakeDownloader:
    def __init__(self):
        self.calls: list[DownloadSpec] = []

    def download(self, spec: DownloadSpec, output_root: Path, progress):
        self.calls.append(spec)
        # Deliberately omit total/progress first: hooks must tolerate unknown totals.
        progress({"stage": "downloading", "title": "A test", "downloaded_bytes": 12})
        progress(
            {
                "stage": "postprocessing",
                "title": "A test",
                "progress": 99.0,
            }
        )
        target = output_root / (spec.output_subdir or "")
        target.mkdir(parents=True, exist_ok=True)
        output = target / f"A test [{spec.job_id}].{spec.media_format}"
        output.write_bytes(b"fake media")
        return DownloadResult(str(output.resolve()), "A test")


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        database_path=tmp_path / "state" / "ateable.db",
        output_dir=tmp_path / "downloads",
        admin_key=ADMIN_KEY,
        embedded_worker=False,
        local_concurrency=1,
        worker_timeout_seconds=45,
        lease_seconds=120,
    )


@pytest.fixture
def client(config: AppConfig):
    app = create_app(config, downloader=FakeDownloader())
    with TestClient(app, headers=ADMIN_HEADERS) as test_client:
        pair_worker(test_client, "default-test-worker")
        yield test_client


def youtube(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def pair_worker(client: TestClient, worker_id: str, *, concurrency: int = 1) -> dict:
    pair_key = client.post(
        "/api/pairing-keys", json={"expires_in_seconds": 300}
    ).json()["pair_key"]
    response = client.post(
        "/api/workers/pair",
        json={
            "pair_key": pair_key,
            "name": worker_id,
            "worker_id": worker_id,
            "max_concurrency": concurrency,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_health_and_safe_settings(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert client.get("/api/settings", headers={"X-Ateable-Key": ""}).status_code == 401

    settings = client.get("/api/settings").json()
    assert settings["default_format"] == "original"
    assert "admin_key" not in settings
    changed = client.patch(
        "/api/settings",
        json={"default_format": "flac", "max_concurrent_downloads": 3},
    )
    assert changed.status_code == 200
    assert changed.json()["default_format"] == "flac"


def test_database_handles_close_after_requests_and_errors(client: TestClient) -> None:
    database = client.app.state.database
    with database.connect() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    with pytest.raises(RuntimeError), database.connect() as failed:
        failed.execute("BEGIN IMMEDIATE")
        raise RuntimeError("failed operation")
    with pytest.raises(sqlite3.ProgrammingError):
        failed.execute("SELECT 1")
    # A failed transaction must not leave a lock that blocks the next write.
    assert client.patch("/api/settings", json={"default_format": "flac"}).status_code == 200


def test_auto_distributes_general_links_by_remote_capacity(config: AppConfig) -> None:
    with TestClient(create_app(config), headers=ADMIN_HEADERS) as master:
        pair_worker(master, "vm-a", concurrency=2)
        pair_worker(master, "vm-b", concurrency=1)
        created = master.post(
            "/api/batches",
            json={
                "urls": [
                    f"https://example.com/data-{i}.zip?signature=kept" for i in range(6)
                ],
            },
        )
        assert created.status_code == 201, created.text
        jobs = created.json()["jobs"]
        assert [job["target_worker_id"] for job in jobs].count("vm-a") == 4
        assert [job["target_worker_id"] for job in jobs].count("vm-b") == 2
        assert all(job["format"] == "original" for job in jobs)
        assert all("signature=kept" in job["canonical_url"] for job in jobs)
        assert not config.output_dir.exists()


def test_storage_ledger_survives_queue_cleanup_and_requires_scoped_auth(
    client: TestClient,
) -> None:
    registration = pair_worker(client, "storage-vm")
    auth = {"Authorization": f"Bearer {registration['token']}"}
    created = client.post(
        "/api/batches",
        json={
            "urls": ["https://example.com/data.zip"],
            "target_worker_id": "storage-vm",
        },
    ).json()
    job_id = created["jobs"][0]["id"]
    client.post("/api/workers/storage-vm/claim", json={"limit": 1}, headers=auth)
    complete = client.post(
        f"/api/workers/storage-vm/jobs/{job_id}/complete",
        headers=auth,
        json={"output_path": "/srv/files/data.zip", "size_bytes": 42},
    )
    assert complete.status_code == 200
    inventory = {
        "output_dir": "/srv/files",
        "disk_total_bytes": 1000,
        "disk_free_bytes": 900,
        "downloaded_bytes": 42,
        "file_count": 1,
        "sampled_at": "2026-10-13T12:00:00+00:00",
        "scan_complete": True,
    }
    report = {
        "inventory": inventory,
        "files": [{"job_id": job_id, "exists": True, "size_bytes": 42}],
    }
    assert (
        client.post(
            "/api/workers/storage-vm/inventory", json=report, headers=auth
        ).status_code
        == 204
    )
    assert (
        client.post(
            "/api/workers/default-test-worker/inventory", json=report, headers=auth
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/workers/storage-vm/storage", headers={"X-Ateable-Key": ""}
        ).status_code
        == 401
    )
    assert client.post("/api/jobs/clear-completed").json()["deleted"] == 1
    stored = client.get("/api/workers/storage-vm/storage").json()
    assert stored["completed_files"] == 1 and stored["total_downloaded_bytes"] == 42
    assert stored["files"][0]["path"] == "/srv/files/data.zip"
    assert stored["inventory"] == inventory
    report["files"][0]["exists"] = False
    report["files"][0]["size_bytes"] = None
    client.post("/api/workers/storage-vm/inventory", json=report, headers=auth)
    stored = client.get("/api/workers/storage-vm/storage").json()
    assert stored["files"][0]["exists"] is False
    assert stored["total_downloaded_bytes"] == 42


def test_old_unassigned_queue_moves_to_remote_worker(client: TestClient) -> None:
    job = client.post(
        "/api/batches", json={"urls": ["https://example.com/old-data.zip"]}
    ).json()["jobs"][0]
    with client.app.state.database.connect() as connection:
        connection.execute(
            "UPDATE jobs SET target_worker_id=NULL WHERE id=?", (job["id"],)
        )
    registration = pair_worker(client, "upgraded-worker")
    claim = client.post(
        "/api/workers/upgraded-worker/claim",
        headers={"Authorization": f"Bearer {registration['token']}"},
        json={"limit": 1},
    )
    # Assignment remains deterministic across all online remote workers.
    assigned = client.get(f"/api/jobs/{job['id']}").json()
    assert assigned["target_worker_id"] in {"default-test-worker", "upgraded-worker"}
    assert assigned["target_worker_id"] != "local"
    assert claim.status_code == 200


def test_worker_profile_update_refreshes_capabilities_and_is_scoped(
    client: TestClient,
) -> None:
    worker = pair_worker(client, "upgraded")
    auth = {"Authorization": f"Bearer {worker['token']}"}
    with client.app.state.database.connect() as connection:
        connection.execute(
            "UPDATE workers SET capabilities='[\"wav\"]' WHERE id='upgraded'"
        )
    profile = {
        "name": "Updated",
        "capabilities": ["original", "wav"],
        "max_concurrency": 3,
        "output_dir": "/srv/new",
    }
    assert (
        client.post(
            "/api/workers/upgraded/profile", headers=auth, json=profile
        ).status_code
        == 204
    )
    assert (
        client.post(
            "/api/workers/default-test-worker/profile", headers=auth, json=profile
        ).status_code
        == 401
    )
    item = next(
        row
        for row in client.get("/api/workers").json()["items"]
        if row["id"] == "upgraded"
    )
    assert item["capabilities"] == ["original", "wav"] and item["max_concurrency"] == 3


def test_atomic_batch_dedupe_and_idempotency(client: TestClient) -> None:
    payload = {
        "urls": [
            youtube("abcdefghijk"),
            "https://youtu.be/abcdefghijk?t=2",
            "https://youtube.com/shorts/ZYXWVUTSRQP",
        ],
        "idempotency_key": "import-001",
    }
    first = client.post("/api/batches", json=payload)
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["batch"]["format"] == "original"
    assert body["batch"]["total"] == 2
    assert body["batch"]["queued"] == 2
    assert body["duplicates"] == ["https://youtu.be/abcdefghijk?t=2"]
    assert {job["format"] for job in body["jobs"]} == {"original"}

    replay = client.post(
        "/api/batches",
        json={**payload, "urls": [youtube("11111111111")], "format": "mp3"},
    )
    assert replay.status_code == 201
    assert replay.json()["batch"]["id"] == body["batch"]["id"]
    assert replay.json()["batch"]["format"] == "original"
    assert client.get("/api/stats").json()["total"] == 2


def test_batch_with_invalid_url_is_fully_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/batches",
        json={"urls": [youtube("abcdefghijk"), "file:///not-supported.zip"]},
    )
    assert response.status_code == 422
    assert client.get("/api/stats").json()["total"] == 0


@pytest.mark.parametrize(
    "subdir",
    ["../escape", "/absolute", r"..\escape", "C:/windows", ".", "a//b"],
)
def test_output_path_traversal_is_rejected(client: TestClient, subdir: str) -> None:
    response = client.post(
        "/api/batches",
        json={"urls": [youtube("abcdefghijk")], "output_subdir": subdir},
    )
    assert response.status_code == 422
    assert client.get("/api/stats").json()["total"] == 0


def test_bulk_actions_scale_without_loaded_job_ids(client: TestClient) -> None:
    urls = [youtube(f"x{i:010d}") for i in range(120)]
    created = client.post("/api/batches", json={"urls": urls}).json()
    batch_id = created["batch"]["id"]

    paused = client.post(
        "/api/jobs/actions", json={"action": "pause", "status": "queued"}
    )
    assert paused.status_code == 200
    assert paused.json()["updated"] == 120
    assert len(paused.json()["items"]) == 100
    assert client.get("/api/stats").json()["paused"] == 120

    resumed = client.post(
        "/api/jobs/actions",
        json={"action": "resume", "status": "paused", "batch_id": batch_id},
    )
    assert resumed.json()["updated"] == 120
    job_id = created["jobs"][0]["id"]
    assert client.post(f"/api/jobs/{job_id}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/api/jobs/{job_id}/retry").json()["status"] == "queued"


def test_5000_urls_are_inserted_in_one_batch(client: TestClient) -> None:
    urls = [youtube(f"v{i:010d}") for i in range(5000)]
    response = client.post(
        "/api/batches",
        json={"urls": urls, "format": "m4a", "idempotency_key": "five-k"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["batch"]["total"] == 5000
    assert len(body["jobs"]) == 5000
    assert client.get("/api/stats").json()["queued"] == 5000


def test_remote_worker_auth_claim_progress_complete_and_clear(
    client: TestClient,
) -> None:
    assert client.post("/api/workers/register", json={"name": "box"}).status_code == 404
    registration = pair_worker(client, "worker-1", concurrency=2)
    token = registration["token"]
    auth = {"Authorization": f"Bearer {token}"}

    created = client.post(
        "/api/batches",
        json={
            "urls": [youtube("abcdefghijk")],
            "target_worker_id": "worker-1",
            "format": "flac",
        },
    )
    assert created.status_code == 201
    job_id = created.json()["jobs"][0]["id"]
    assert (
        client.post(
            "/api/workers/worker-1/claim",
            headers={"Authorization": "Bearer nope"},
            json={"limit": 1},
        ).status_code
        == 401
    )

    claim = client.post("/api/workers/worker-1/claim", headers=auth, json={"limit": 2})
    assert claim.status_code == 200
    assert claim.json()["items"][0]["id"] == job_id
    assert claim.json()["items"][0]["lease_seconds"] == 120

    # Unknown total is valid and does not invent a percentage.
    progress = client.post(
        f"/api/workers/worker-1/jobs/{job_id}/progress",
        headers=auth,
        json={"stage": "downloading", "downloaded_bytes": 400},
    )
    assert progress.status_code == 200
    assert progress.json()["progress"] == 0

    completed = client.post(
        f"/api/workers/worker-1/jobs/{job_id}/complete",
        headers=auth,
        json={"output_path": "/remote/downloads/example.flac", "title": "Remote"},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert client.get("/api/stats").json()["completed"] == 1
    assert client.post("/api/jobs/clear-completed").json() == {"deleted": 1}
    assert client.get("/api/stats").json()["total"] == 0


@pytest.mark.parametrize("release", ["complete", "fail", "cancel", "pause", "expire"])
def test_worker_claims_respect_remaining_capacity_and_recover_slots(
    client: TestClient,
    release: str,
) -> None:
    registration = pair_worker(client, "bounded-worker", concurrency=2)
    auth = {"Authorization": f"Bearer {registration['token']}"}
    prefix = "/api/workers/bounded-worker"
    assert (
        client.post(
            "/api/batches",
            json={
                "urls": [youtube(f"capacity{i:03d}") for i in range(8)],
                "target_worker_id": "bounded-worker",
            },
        ).status_code
        == 201
    )

    first = client.post(f"{prefix}/claim", headers=auth, json={"limit": 1}).json()[
        "items"
    ]
    assert len(first) == 1
    second = client.post(f"{prefix}/claim", headers=auth, json={"limit": 64}).json()[
        "items"
    ]
    assert len(second) == 1
    assert (
        client.post(f"{prefix}/claim", headers=auth, json={"limit": 2}).json()["items"]
        == []
    )

    job_id = first[0]["id"]
    if release == "expire":
        with client.app.state.database.connect() as connection:
            connection.execute(
                "UPDATE jobs SET lease_expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00.000+00:00", job_id),
            )
    elif release in {"cancel", "pause"}:
        assert client.post(f"/api/jobs/{job_id}/{release}").status_code == 200
    else:
        body = (
            {"output_path": "/remote/example.wav"}
            if release == "complete"
            else {"error": "test failure", "retryable": False}
        )
        assert (
            client.post(
                f"{prefix}/jobs/{job_id}/{release}", headers=auth, json=body
            ).status_code
            == 200
        )

    replacement = client.post(
        f"{prefix}/claim", headers=auth, json={"limit": 64}
    ).json()["items"]
    assert len(replacement) == 1
    assert client.get("/api/stats").json()["downloading"] == 2


def test_simultaneous_worker_claims_share_one_capacity_limit(
    client: TestClient,
) -> None:
    registration = pair_worker(client, "concurrent-worker", concurrency=2)
    auth = {"Authorization": f"Bearer {registration['token']}"}
    assert (
        client.post(
            "/api/batches",
            json={
                "urls": [youtube(f"parallel{i:03d}") for i in range(12)],
                "target_worker_id": "concurrent-worker",
            },
        ).status_code
        == 201
    )
    barrier = Barrier(4)

    def claim(_index: int) -> list[dict]:
        barrier.wait(timeout=10)
        response = client.post(
            "/api/workers/concurrent-worker/claim", headers=auth, json={"limit": 2}
        )
        assert response.status_code == 200
        return response.json()["items"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = [job for jobs in pool.map(claim, range(4)) for job in jobs]
    assert len(claimed) == len({job["id"] for job in claimed}) == 2
    assert client.get("/api/stats").json()["downloading"] == 2


def test_master_cannot_claim_or_receive_download_jobs(client: TestClient) -> None:
    database = client.app.state.database
    with pytest.raises(ConflictError, match="master"):
        database.claim_jobs(
            worker_id="local", capabilities=["original"], limit=1, local=True
        )
    response = client.post(
        "/api/batches",
        json={
            "urls": ["https://example.com/archive.zip"],
            "target_worker_id": "local",
        },
    )
    assert response.status_code == 409
    assert "master" in response.json()["detail"]
    assert not database.config.output_dir.exists()


def test_queue_search_reaches_older_jobs_and_treats_search_text_literally(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/batches", json={"urls": [youtube(f"search{i:05d}") for i in range(305)]}
    ).json()["jobs"]
    target = created[0]["id"]
    with client.app.state.database.connect() as connection:
        connection.execute(
            "UPDATE jobs SET title = ?, created_at = ? WHERE id = ?",
            ("Straße 100%_done", "2000-01-01T00:00:00.000+00:00", target),
        )
    assert target not in {
        job["id"] for job in client.get("/api/jobs?limit=250").json()["items"]
    }
    for query in ("STRASSE", "100%_", "%", "_done"):
        result = client.get("/api/jobs", params={"q": query}).json()
        assert result["filters_applied"] is True
        assert result["total"] == 1
        assert result["items"][0]["id"] == target
    assert client.get("/api/jobs", params={"q": "' OR 1=1 --"}).json()["total"] == 0
    assert client.get("/api/jobs", params={"q": "x" * 501}).status_code == 422


def test_queue_pagination_is_stable_when_timestamps_are_equal(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/batches", json={"urls": [youtube(f"paging{i:05d}") for i in range(305)]}
    ).json()["jobs"]
    with client.app.state.database.connect() as connection:
        connection.execute(
            "UPDATE jobs SET created_at = ?, title = ?", ("2026-01-01", "Shared title")
        )
    first = client.get("/api/jobs", params={"q": "shared", "limit": 250}).json()
    second = client.get(
        "/api/jobs", params={"q": "shared", "limit": 250, "offset": 250}
    ).json()
    ids = [job["id"] for job in first["items"] + second["items"]]
    assert first["total"] == second["total"] == 305
    assert len(first["items"]) == 250
    assert len(second["items"]) == 55
    assert len(ids) == len(set(ids)) == 305
    assert set(ids) == {job["id"] for job in created}


@pytest.mark.parametrize(
    ("status", "expected"),
    [("active", {"queued", "downloading"}), ("attention", {"paused", "failed"})],
)
def test_queue_group_filters_combine_with_search(
    client: TestClient, status: str, expected: set[str]
) -> None:
    created = client.post(
        "/api/batches", json={"urls": [youtube(f"status{i:05d}") for i in range(6)]}
    ).json()["jobs"]
    with client.app.state.database.connect() as connection:
        for job, state in zip(
            created,
            ("queued", "downloading", "paused", "failed", "completed", "cancelled"),
        ):
            connection.execute(
                "UPDATE jobs SET status = ?, title = ? WHERE id = ?",
                (state, "Target group", job["id"]),
            )
    result = client.get("/api/jobs", params={"status": status, "q": "target"}).json()
    assert result["total"] == 2
    assert {job["status"] for job in result["items"]} == expected


def test_legacy_embedded_flag_never_downloads_on_master(tmp_path: Path) -> None:
    fake = FakeDownloader()
    config = AppConfig(
        database_path=tmp_path / "state.db",
        output_dir=tmp_path / "downloads",
        admin_key=ADMIN_KEY,
        embedded_worker=True,
    )
    app = create_app(config, downloader=fake)
    with TestClient(app, headers=ADMIN_HEADERS) as controller:
        rejected = controller.post(
            "/api/batches", json={"urls": ["https://example.com/data.zip"]}
        )
        assert rejected.status_code == 409
        registration = pair_worker(controller, "vm")
        created = controller.post(
            "/api/batches", json={"urls": ["https://example.com/data.zip"]}
        )
        assert created.status_code == 201
        job = created.json()["jobs"][0]
        assert job["target_worker_id"] == registration["id"]
        assert job["status"] == "queued"
        assert fake.calls == []
        assert not config.output_dir.exists()
        assert not app.state.config.embedded_worker


def test_events_are_incrementally_pollable(client: TestClient) -> None:
    client.post("/api/batches", json={"urls": [youtube("abcdefghijk")]})
    first = client.get("/api/events?after_id=0").json()
    assert first["items"][-1]["type"] == "batch.enqueued"
    cursor = first["next_after_id"]
    second = client.get(f"/api/events?after_id={cursor}").json()
    assert second == {"items": [], "next_after_id": cursor}


def test_pairing_key_is_single_use_hashed_and_expiring(
    client: TestClient,
) -> None:
    created = client.post("/api/pairing-keys", json={"expires_in_seconds": 60})
    assert created.status_code == 200, created.text
    pair_body = created.json()
    decoded = decode_pair_key(pair_body["pair_key"])
    assert decoded.controller_url == "http://127.0.0.1:8765"

    database = client.app.state.database
    with database.connect() as connection:
        stored = connection.execute(
            "SELECT secret_hash FROM pairing_keys WHERE id = ?", (decoded.key_id,)
        ).fetchone()["secret_hash"]
    assert decoded.secret not in stored

    tampered = encode_pair_key(
        "https://attacker.invalid", decoded.key_id, decoded.secret
    )
    assert (
        client.post(
            "/api/workers/pair",
            json={"pair_key": tampered, "name": "tampered", "worker_id": "bad"},
        ).status_code
        == 401
    )

    paired = client.post(
        "/api/workers/pair",
        headers={"X-Ateable-Key": ""},
        json={
            "pair_key": pair_body["pair_key"],
            "name": "paired box",
            "worker_id": "paired-1",
        },
    )
    assert paired.status_code == 200, paired.text
    assert paired.json()["id"] == "paired-1"
    assert paired.json()["token"]

    replay = client.post(
        "/api/workers/pair",
        json={
            "pair_key": pair_body["pair_key"],
            "name": "replay",
            "worker_id": "paired-2",
        },
    )
    assert replay.status_code == 409

    expiring = client.post("/api/pairing-keys", json={"expires_in_seconds": 30}).json()
    expiring_id = decode_pair_key(expiring["pair_key"]).key_id
    with database.connect() as connection:
        connection.execute(
            "UPDATE pairing_keys SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", expiring_id),
        )
    expired = client.post(
        "/api/workers/pair",
        json={
            "pair_key": expiring["pair_key"],
            "name": "late",
            "worker_id": "paired-3",
        },
    )
    assert expired.status_code == 410


def test_pairing_regenerate_revokes_previous_key(client: TestClient) -> None:
    first = client.post("/api/pairing-keys", json={"expires_in_seconds": 300}).json()
    second = client.post(
        "/api/pairing-keys/regenerate", json={"expires_in_seconds": 300}
    )
    assert second.status_code == 200
    replay = client.post(
        "/api/workers/pair",
        json={"pair_key": first["pair_key"], "name": "old"},
    )
    assert replay.status_code == 409


def test_rate_limit_failure_cools_entire_worker(client: TestClient) -> None:
    registration = pair_worker(client, "cooldown-worker")
    auth = {"Authorization": f"Bearer {registration['token']}"}
    client.post(
        "/api/batches",
        json={
            "urls": [youtube("aaa11111111"), youtube("bbb22222222")],
            "target_worker_id": "cooldown-worker",
        },
    )
    claimed = client.post(
        "/api/workers/cooldown-worker/claim", headers=auth, json={"limit": 1}
    ).json()["items"]
    failed = client.post(
        f"/api/workers/cooldown-worker/jobs/{claimed[0]['id']}/fail",
        headers=auth,
        json={
            "error": "HTTP Error 429: Too Many Requests",
            "retryable": True,
            "failure_kind": "rate_limited",
            "cooldown_seconds": 120,
        },
    )
    assert failed.status_code == 200
    assert (
        client.post(
            "/api/workers/cooldown-worker/claim", headers=auth, json={"limit": 2}
        ).json()["items"]
        == []
    )
    worker = next(
        item
        for item in client.get("/api/workers").json()["items"]
        if item["id"] == "cooldown-worker"
    )
    assert worker["cooldown_until"] is not None


def test_network_probe_uses_json_body_and_admin_auth(
    client: TestClient,
) -> None:
    direct = client.post("/api/network/probe", json={"mode": "direct", "proxy": None})
    assert direct.status_code == 200
    assert direct.json()["reachable"] is True
    assert (
        client.post(
            "/api/network/probe",
            headers={"X-Ateable-Key": ""},
            json={"mode": "direct"},
        ).status_code
        == 401
    )
