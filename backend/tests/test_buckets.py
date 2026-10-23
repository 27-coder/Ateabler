from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from ateable.api import create_app
from ateable.buckets import BucketStore
from ateable.config import AppConfig
from ateable.database import ConflictError, Database, NotFoundError
from ateable.validation import ValidationError

ADMIN_KEY = "bucket-test-admin-secret"
ADMIN_HEADERS = {"X-Ateable-Key": ADMIN_KEY}


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        database_path=tmp_path / "state" / "controller.db",
        output_dir=tmp_path / "media",
        admin_key=ADMIN_KEY,
        embedded_worker=False,
        worker_timeout_seconds=45,
    )


@pytest.fixture
def store(config: AppConfig) -> BucketStore:
    database = Database(config)
    database.initialize()
    return BucketStore(database)


def pair_worker(client: TestClient, worker_id: str = "vm-a") -> dict:
    pair_key = client.post(
        "/api/pairing-keys",
        headers=ADMIN_HEADERS,
        json={"expires_in_seconds": 300},
    ).json()["pair_key"]
    response = client.post(
        "/api/workers/pair",
        json={
            "pair_key": pair_key,
            "worker_id": worker_id,
            "name": worker_id,
            "max_concurrency": 2,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def links(count: int = 1) -> list[dict]:
    return [
        {"url": f"https://example.com/file-{index}.zip?signature=kept"}
        for index in range(count)
    ]


def test_offline_capture_metadata_search_export_and_restart(
    store: BucketStore, config: AppConfig
):
    assert store.list_buckets()["items"][0]["name"] == "Inbox"
    bucket = store.create_bucket("Research", "Things to fetch on workers")
    captured = store.capture(
        bucket["id"],
        [
            {
                "url": "https://example.com/report.pdf?signature=retained#page=4",
                "title": "Annual Résumé",
                "source_page": "https://example.com/catalog",
            }
        ],
        origin="browser",
        idempotency_key="capture-1",
    )
    assert captured["accepted"] == 1 and captured["duplicates"] == 0
    assert not config.output_dir.exists()
    assert store.database.list_jobs(limit=10, offset=0)[1] == 0
    restarted = BucketStore(store.database)
    item = restarted.list_items(bucket["id"], query="RÉSUMÉ", limit=1)["items"][0]
    assert item["title"] == "Annual Résumé" and item["origin"] == "browser"
    assert item["canonical_url"].endswith("?signature=retained")
    assert item["source_url"].endswith("#page=4")
    assert restarted.export(bucket["id"], format="text") == item["source_url"] + "\n"
    assert restarted.export(bucket["id"], format="json")["items"][0]["id"] == item["id"]
    assert restarted.summary()["total"] == 1
    assert restarted.list_items(bucket["id"], offset=1)["items"] == []


def test_capture_dedupe_is_bucket_scoped_canonical_and_receipts_replay(
    store: BucketStore,
):
    first = store.capture(
        "inbox",
        [
            {"url": "https://youtu.be/abc12345678", "title": "A"},
            {"url": "https://youtube.com/watch?v=abc12345678&feature=share"},
        ],
        idempotency_key="same-request",
    )
    assert (first["accepted"], first["duplicates"]) == (1, 1)
    assert store.capture("inbox", links(), idempotency_key="same-request") == first
    assert store.get_bucket("inbox")["total"] == 1
    assert (
        store.capture("inbox", [{"url": "https://youtu.be/abc12345678"}])["duplicates"]
        == 1
    )
    other = store.create_bucket("Other")
    assert (
        store.capture(other["id"], [{"url": "https://youtu.be/abc12345678"}])[
            "accepted"
        ]
        == 1
    )


def test_invalid_capture_rolls_back_entire_request_and_does_not_echo_url(
    store: BucketStore,
):
    secret_url = (
        "https://user:private-password@example.com/secret?signature=private-query"
    )
    with pytest.raises(ValidationError) as failure:
        store.capture("inbox", [*links(), {"url": secret_url}])
    assert "private-password" not in str(failure.value)
    assert "private-query" not in str(failure.value)
    assert store.get_bucket("inbox")["total"] == 0
    with pytest.raises(ValidationError) as failure:
        store.capture(
            "inbox", [{"url": "https://example.com/" + "secret-query" * 1000}]
        )
    assert "secret-query" not in str(failure.value)
    with pytest.raises(ValidationError):
        store.capture("inbox", [{"url": "https://example.com/a", "title": "x" * 513}])
    assert store.get_bucket("inbox")["total"] == 0


def test_capture_rolls_back_after_mid_transaction_failure(store: BucketStore):
    with store.database.connect() as connection:
        connection.executescript("""
            CREATE TRIGGER reject_second_capture BEFORE INSERT ON bucket_items
            WHEN NEW.source_url LIKE '%file-1.zip%' BEGIN
                SELECT RAISE(ABORT,'simulated disk failure');
            END;
        """)
    with pytest.raises(Exception, match="simulated disk failure"):
        store.capture("inbox", links(2), idempotency_key="rolled-back")
    assert store.get_bucket("inbox")["total"] == 0
    with store.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM bucket_capture_receipts"
            ).fetchone()[0]
            == 0
        )
        connection.execute("DROP TRIGGER reject_second_capture")
    assert (
        store.capture("inbox", links(2), idempotency_key="rolled-back")["accepted"] == 2
    )


def test_concurrent_capture_creates_one_item_and_one_receipt(store: BucketStore):
    barrier = Barrier(8)

    def capture():
        barrier.wait()
        return BucketStore(store.database).capture(
            "inbox", links(), idempotency_key="concurrent"
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: capture(), range(8)))
    assert all(response == responses[0] for response in responses)
    assert responses[0]["accepted"] == 1
    assert store.get_bucket("inbox")["total"] == 1
    with store.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM bucket_capture_receipts"
            ).fetchone()[0]
            == 1
        )


def test_bucket_names_are_casefold_unique_and_items_cannot_cross_buckets(
    store: BucketStore,
):
    other = store.create_bucket("Other")
    with pytest.raises(ConflictError):
        store.create_bucket("oTHER")
    item = store.capture("inbox", links())["items"][0]
    with pytest.raises(NotFoundError):
        store.remove_item(other["id"], item["id"])
    with pytest.raises(NotFoundError):
        store.dispatch(other["id"], item_ids=[item["id"]])
    assert (
        store.update_bucket(other["id"], name="Reading", description="Later")["name"]
        == "Reading"
    )
    with pytest.raises(ValidationError):
        store.update_bucket(other["id"], name="   ")


def test_admin_and_capture_credentials_have_separate_permissions(config: AppConfig):
    with TestClient(create_app(config)) as client:
        assert client.get("/api/buckets").status_code == 401
        bucket = client.post(
            "/api/buckets", headers=ADMIN_HEADERS, json={"name": "Browser"}
        ).json()
        token = client.post(
            "/api/capture-tokens",
            headers=ADMIN_HEADERS,
            json={"bucket_id": bucket["id"]},
        ).json()
        scoped = {"X-Ateable-Capture": token["token"]}
        assert client.get("/api/capture-target", headers=scoped).json() == {
            "bucket_id": bucket["id"],
            "name": "Browser",
        }
        assert (
            client.post(
                "/api/capture",
                headers={"X-Ateable-Capture": ADMIN_KEY},
                json={"items": links()},
            ).status_code
            == 401
        )
        for path in (
            "/api/buckets",
            f"/api/buckets/{bucket['id']}/items",
            "/api/jobs",
            "/api/capture-tokens",
        ):
            assert client.get(path, headers=scoped).status_code == 401
        assert (
            client.post(
                f"/api/buckets/{bucket['id']}/dispatch", headers=scoped, json={}
            ).status_code
            == 401
        )
        response = client.post(
            "/api/capture",
            headers=scoped,
            json={
                "items": links(2),
                "origin": "browser",
                "idempotency_key": "public-1",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json() == {
            "bucket_id": bucket["id"],
            "accepted": 2,
            "duplicates": 0,
        }
        replay = client.post(
            "/api/capture",
            headers=scoped,
            json={
                "items": links(2),
                "origin": "browser",
                "idempotency_key": "public-1",
            },
        )
        assert replay.json() == response.json()
        assert (
            client.get(
                f"/api/buckets/{bucket['id']}/items", headers=ADMIN_HEADERS
            ).json()["total"]
            == 2
        )
        assert (
            client.post(
                "/api/capture",
                headers=scoped,
                json={"items": links(), "bucket_id": "inbox"},
            ).status_code
            == 422
        )
        listing = client.get("/api/capture-tokens", headers=ADMIN_HEADERS).json()
        assert token["token"] not in json.dumps(
            listing
        ) and "token_hash" not in json.dumps(listing)
        with client.app.state.database.connect() as connection:
            persisted = dict(
                connection.execute("SELECT * FROM bucket_capture_tokens").fetchone()
            )
        assert token["token"] not in json.dumps(persisted)
        assert (
            client.delete(
                f"/api/capture-tokens/{token['token_id']}", headers=ADMIN_HEADERS
            ).status_code
            == 200
        )
        assert client.get("/api/capture-target", headers=scoped).status_code == 401
        assert (
            client.post(
                "/api/capture", headers=scoped, json={"items": links()}
            ).status_code
            == 401
        )
        preflight = client.options(
            "/api/capture",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert preflight.headers.get("Access-Control-Allow-Origin") != "*"


def test_capture_receipts_are_separate_for_admin_and_each_token(store: BucketStore):
    token_a = store.create_capture_token("inbox")["token"]
    token_b = store.create_capture_token("inbox")["token"]
    assert (
        store.capture("inbox", links(), idempotency_key="shared-key")["accepted"] == 1
    )
    assert (
        store.capture_with_token(token_a, links(), idempotency_key="shared-key")[
            "duplicates"
        ]
        == 1
    )
    assert (
        store.capture_with_token(token_b, links(2), idempotency_key="shared-key")[
            "accepted"
        ]
        == 1
    )


def test_dispatch_worker_unavailable_reservation_survives_restart(config: AppConfig):
    with TestClient(create_app(config), headers=ADMIN_HEADERS) as client:
        store = client.app.state.bucket_store
        store.capture("inbox", links(3))
        with pytest.raises(ConflictError):
            store.dispatch("inbox")
        assert store.get_bucket("inbox")["counts"] == {"dispatching": 3}
        active = store.get_bucket("inbox")["active_dispatch"]
        assert active["options"]["target_worker_id"] == "auto" and active["error"]
        assert store.summary()["active_dispatches"][0]["run_id"] == active["run_id"]
        assert client.get("/api/jobs").json()["total"] == 0
        restarted = BucketStore(store.database)
        pair_worker(client)
        result = restarted.dispatch("inbox")
        assert result["dispatched"] == 3 and result["replayed"]
        assert all(
            job["target_worker_id"] == "vm-a" and job["format"] == "original"
            for job in result["jobs"]
        )
        assert restarted.dispatch("inbox")["dispatched"] == 0
        assert not config.output_dir.exists()


def test_explicit_retry_route_recovers_only_reserved_items_and_preserves_options(
    config: AppConfig,
):
    with TestClient(create_app(config), headers=ADMIN_HEADERS) as client:
        store = client.app.state.bucket_store
        first = store.capture("inbox", links())["items"][0]
        response = client.post(
            "/api/buckets/inbox/dispatch", json={"output_subdir": "archive"}
        )
        assert response.status_code == 409
        active = client.get("/api/buckets/inbox").json()["active_dispatch"]
        with pytest.raises(ConflictError):
            store.remove_item("inbox", first["id"])
        store.capture(
            "inbox", [{"url": "https://example.com/new-after-reservation.zip"}]
        )
        pair_worker(client)
        route = f"/api/buckets/inbox/dispatch-runs/{active['run_id']}/retry"
        retried = client.post(route, json={})
        assert retried.status_code == 200, retried.text
        assert retried.json()["dispatched"] == 1 and retried.json()["pending"] == 1
        assert retried.json()["options"]["output_subdir"] == "archive"
        assert (
            client.post(route, json={}).json()["jobs"][0]["id"]
            == retried.json()["jobs"][0]["id"]
        )
        assert store.get_bucket("inbox")["active_dispatch"] is None
        other = store.create_bucket("Other")
        assert (
            client.post(
                f"/api/buckets/{other['id']}/dispatch-runs/{active['run_id']}/retry",
                json={},
            ).status_code
            == 404
        )


def test_dispatch_concurrent_process_stores_queue_each_item_once(config: AppConfig):
    with TestClient(create_app(config), headers=ADMIN_HEADERS) as client:
        pair_worker(client)
        store = client.app.state.bucket_store
        store.capture("inbox", links(20))
        stores = [BucketStore(store.database) for _ in range(8)]
        barrier = Barrier(8)

        def dispatch(index):
            barrier.wait()
            return stores[index].dispatch(
                "inbox", idempotency_key="dispatch-concurrent"
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(dispatch, range(8)))
        assert len({result["run_id"] for result in responses}) == 1
        assert all(result["dispatched"] == 20 for result in responses)
        assert client.get("/api/jobs").json()["total"] == 20
        with store.database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1


def test_crash_after_enqueue_and_queue_cleanup_recovers_without_duplicate(
    config: AppConfig, monkeypatch
):
    with TestClient(create_app(config), headers=ADMIN_HEADERS) as client:
        pair_worker(client)
        store = client.app.state.bucket_store
        store.capture("inbox", links(2))
        real_enqueue = store.database.enqueue_batch

        def committed_then_failed(**kwargs):
            real_enqueue(**kwargs)
            raise RuntimeError("simulated process exit after queue commit")

        monkeypatch.setattr(store.database, "enqueue_batch", committed_then_failed)
        with pytest.raises(RuntimeError, match="process exit"):
            store.dispatch("inbox", idempotency_key="crash-run")
        with store.database.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status='completed',stage='completed',output_path='/srv/a.zip'"
            )
        assert store.database.clear_completed() == 2
        monkeypatch.setattr(store.database, "enqueue_batch", real_enqueue)
        restarted = BucketStore(store.database)
        recovered = restarted.dispatch("inbox", idempotency_key="crash-run")
        assert recovered["dispatched"] == 2 and recovered["replayed"]
        assert all(
            job["status"] == "completed" and job["queue_removed"]
            for job in recovered["jobs"]
        )
        assert client.get("/api/jobs").json()["total"] == 0
        items = restarted.list_items("inbox")["items"]
        assert all(
            item["job_status"] == "completed"
            and item["history"][0]["output_path"] == "/srv/a.zip"
            for item in items
        )


def test_failed_enqueue_transaction_rolls_back_and_retry_retains_all_links(
    config: AppConfig, monkeypatch
):
    with TestClient(create_app(config), headers=ADMIN_HEADERS) as client:
        pair_worker(client)
        store = client.app.state.bucket_store
        store.capture("inbox", links(2))
        with store.database.connect() as connection:
            connection.executescript("""
                CREATE TRIGGER reject_second_job BEFORE INSERT ON jobs
                WHEN NEW.source_url LIKE '%file-1.zip%' BEGIN
                    SELECT RAISE(ABORT,'simulated queue failure');
                END;
            """)
        with pytest.raises(Exception, match="queue failure"):
            store.dispatch("inbox")
        with store.database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM bucket_dispatch_items WHERE job_id IS NOT NULL"
                ).fetchone()[0]
                == 0
            )
            connection.execute("DROP TRIGGER reject_second_job")
        assert BucketStore(store.database).dispatch("inbox")["dispatched"] == 2


def test_repeat_dispatch_only_new_items_and_explicit_retry_reuses_job_ids(
    config: AppConfig,
):
    with TestClient(create_app(config), headers=ADMIN_HEADERS) as client:
        pair_worker(client)
        store = client.app.state.bucket_store
        first_item = store.capture("inbox", links())["items"][0]
        first = store.dispatch("inbox", idempotency_key="initial")
        assert store.dispatch("inbox")["dispatched"] == 0
        with store.database.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status='failed',attempts=3,error='network error'"
            )
        assert store.dispatch("inbox")["dispatched"] == 0
        retried = store.dispatch("inbox", retry_existing=True, idempotency_key="retry")
        assert retried["jobs"][0]["id"] == first["jobs"][0]["id"]
        assert (
            retried["jobs"][0]["attempts"] == 0
            and retried["jobs"][0]["status"] == "queued"
        )
        new_capture = store.capture("inbox", [{"url": "https://example.com/new.zip"}])
        new_run = store.dispatch("inbox")
        assert new_run["dispatched"] == 1 and new_run["jobs"][0]["source_url"].endswith(
            "new.zip"
        )
        assert (
            store.dispatch("inbox", idempotency_key="initial")["jobs"][0]["id"]
            == first["jobs"][0]["id"]
        )
        assert store.remove_item("inbox", first_item["id"])["removed"]
        assert store.database.get_job(first["jobs"][0]["id"])["status"] == "queued"
        assert store.get_bucket("inbox")["total"] == 1
        assert new_capture["accepted"] == 1


def test_dispatch_safe_paths_and_batch_boundaries(config: AppConfig):
    with TestClient(create_app(config), headers=ADMIN_HEADERS) as client:
        pair_worker(client)
        store = client.app.state.bucket_store
        store.capture("inbox", links(7))
        with pytest.raises(ValidationError):
            store.dispatch("inbox", output_subdir="../escape")
        with pytest.raises(ConflictError):
            store.dispatch("inbox", target_worker_id="local")
        with pytest.raises(ValidationError):
            store.dispatch("inbox", limit=5001)
        result = store.dispatch(
            "inbox", limit=3, target_worker_id="vm-a", output_subdir="folder/sub"
        )
        assert result["dispatched"] == 3 and result["pending"] == 4
        assert store.dispatch("inbox", limit=3)["dispatched"] == 3
        assert store.dispatch("inbox")["dispatched"] == 1
        assert all(job["target_worker_id"] != "local" for job in result["jobs"])


def test_expanded_prefix_is_not_counted_as_completed_file(config: AppConfig):
    with TestClient(create_app(config), headers=ADMIN_HEADERS) as client:
        pair_worker(client)
        store = client.app.state.bucket_store
        store.capture("inbox", links())
        store.dispatch("inbox")
        with store.database.connect() as connection:
            connection.execute("UPDATE jobs SET status='completed',stage='expanded'")
        assert store.get_bucket("inbox")["counts"] == {"expanded": 1}
        assert store.list_items("inbox", status="expanded")["total"] == 1
        assert store.list_items("inbox", status="completed")["total"] == 0
        store.database.clear_completed()
        assert store.get_bucket("inbox")["counts"] == {"expanded": 1}


def test_bucket_http_routes_validate_and_export_offline(config: AppConfig):
    with TestClient(create_app(config), headers=ADMIN_HEADERS) as client:
        response = client.post("/api/buckets/inbox/items", json={"items": links(2)})
        assert response.status_code == 201
        assert client.get("/api/buckets/summary").json()["pending"] == 2
        assert client.get("/api/buckets/inbox/export?format=text").text.count("\n") == 2
        assert (
            len(client.get("/api/buckets/inbox/export?format=json").json()["items"])
            == 2
        )
        assert (
            client.get("/api/buckets/inbox/items?limit=1&offset=1").json()["total"] == 2
        )
        invalid = client.post(
            "/api/buckets/inbox/items",
            json={
                "items": [
                    {"url": "https://user:secret@example.com/path?signature=secret"},
                ]
            },
        )
        assert invalid.status_code == 422 and "secret" not in invalid.text
        assert client.get("/api/buckets/missing").status_code == 404


def test_s3_capture_preserves_catalog_kind_and_is_offline(store: BucketStore):
    captured = store.capture(
        "inbox",
        [
            {"url": "s3://research-bucket"},
            {"url": "s3://research-bucket/reports/"},
            {"url": "s3://research-bucket/reports/a%20file.zip"},
        ],
        origin="source-catalog",
    )
    assert captured["accepted"] == 3
    assert [item["kind"] for item in captured["items"]] == [
        "s3_prefix",
        "s3_prefix",
        "s3_object",
    ]
    assert store.database.list_jobs(limit=10, offset=0)[1] == 0


def test_capture_batch_bound_is_atomic_and_counts_every_submitted_url(
    store: BucketStore,
):
    with pytest.raises(ValidationError):
        store.capture("inbox", links() * 5001)
    assert store.get_bucket("inbox")["total"] == 0
    response = store.capture("inbox", links() * 5000)
    assert response["accepted"] == 1 and response["duplicates"] == 4999


def test_invalid_s3_format_does_not_reserve_and_corrected_dispatch_can_succeed(
    config: AppConfig,
):
    with TestClient(create_app(config), headers=ADMIN_HEADERS) as client:
        store = client.app.state.bucket_store
        item = store.capture("inbox", [{"url": "s3://review-bucket/reports/"}])[
            "items"
        ][0]
        invalid = client.post("/api/buckets/inbox/dispatch", json={"format": "wav"})
        assert invalid.status_code == 422
        assert store.get_bucket("inbox")["active_dispatch"] is None
        assert store.get_bucket("inbox")["counts"] == {"pending": 1}
        assert store.remove_item("inbox", item["id"])["removed"]
        store.capture("inbox", [{"url": "s3://review-bucket/reports/"}])
        pair_worker(client)
        corrected = client.post(
            "/api/buckets/inbox/dispatch", json={"format": "original"}
        )
        assert corrected.status_code == 200, corrected.text
        assert corrected.json()["dispatched"] == 1


def test_invalid_explicit_worker_and_format_do_not_lock_bucket(config: AppConfig):
    with TestClient(create_app(config), headers=ADMIN_HEADERS) as client:
        store = client.app.state.bucket_store
        store.capture("inbox", links())
        invalid = client.post(
            "/api/buckets/inbox/dispatch", json={"target_worker_id": "missing"}
        )
        assert invalid.status_code == 404
        assert store.get_bucket("inbox")["active_dispatch"] is None
        pair_key = client.post("/api/pairing-keys", json={}).json()["pair_key"]
        paired = client.post(
            "/api/workers/pair",
            json={
                "pair_key": pair_key,
                "worker_id": "original-only",
                "name": "Original VM",
                "capabilities": ["original"],
            },
        )
        assert paired.status_code == 200
        unsupported = client.post(
            "/api/buckets/inbox/dispatch",
            json={
                "format": "wav",
                "target_worker_id": "original-only",
            },
        )
        assert unsupported.status_code == 409
        assert store.get_bucket("inbox")["active_dispatch"] is None
        corrected = client.post(
            "/api/buckets/inbox/dispatch", json={"target_worker_id": "original-only"}
        )
        assert corrected.status_code == 200 and corrected.json()["dispatched"] == 1


def test_dispatch_conversion_validation_applies_only_to_selected_pending_items(
    config: AppConfig,
):
    with TestClient(create_app(config), headers=ADMIN_HEADERS) as client:
        pair_worker(client)
        store = client.app.state.bucket_store
        items = store.capture(
            "inbox",
            [
                {"url": "s3://review-bucket/a.bin"},
                {"url": "https://example.com/video.webm"},
            ],
        )["items"]
        first = store.dispatch("inbox", media_format="wav", item_ids=[items[1]["id"]])
        assert first["dispatched"] == 1 and first["jobs"][0]["format"] == "wav"
        assert store.get_bucket("inbox")["pending"] == 1
        assert store.dispatch("inbox", media_format="original")["dispatched"] == 1


def test_source_child_insert_cannot_overwrite_cleaned_capture_history(
    config: AppConfig,
):
    with TestClient(create_app(config), headers=ADMIN_HEADERS) as client:
        pair_worker(client)
        store = client.app.state.bucket_store
        captures = store.capture(
            "inbox",
            [
                {"url": "s3://review-bucket/raw/"},
                {"url": "s3://review-bucket/raw/a.zip"},
            ],
        )["items"]
        jobs = store.dispatch("inbox")["jobs"]
        exact = next(job for job in jobs if job["canonical_url"].endswith("a.zip"))
        prefix = next(job for job in jobs if job["canonical_url"].endswith("raw/"))
        with store.database.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status='completed',stage='completed',output_path='/srv/a.zip' WHERE id=?",
                (exact["id"],),
            )
        assert store.database.clear_completed() == 1
        # Migrate an existing store too; changing CREATE IF NOT EXISTS alone
        # would leave a previous trigger able to overwrite retained history.
        with store.database.connect() as connection:
            trigger = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='bucket_job_insert'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER bucket_job_insert")
            connection.execute(trigger.replace("job_id IS NULL AND ", ""))
        BucketStore(store.database)
        with store.database.connect() as connection:
            connection.execute(
                "INSERT INTO jobs(id,batch_id,source_url,canonical_url,dedupe_key,format,status,stage,"
                "target_worker_id,next_attempt_at,created_at,updated_at,kind,source_job_id,root_job_id) "
                "SELECT ?,batch_id,source_url,canonical_url,dedupe_key,'original','queued','queued',"
                "job_target_worker_id,job_created_at,job_created_at,job_created_at,'download',?,? "
                "FROM bucket_dispatch_items WHERE item_id=?",
                ("new-source-child", prefix["id"], prefix["id"], captures[1]["id"]),
            )
        retained = store.list_items("inbox")["items"][1]
        assert (
            retained["job_id"] == exact["id"] and retained["job_status"] == "completed"
        )
        assert retained["history"][0]["output_path"] == "/srv/a.zip"
        assert len(retained["history"]) == 1
