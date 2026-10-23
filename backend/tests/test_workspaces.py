from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from ateable.api import create_app
from ateable.config import AppConfig
from ateable.object_storage import S3Source
from ateable.workspaces import AccessDenied, WorkspaceStore

ADMIN = {"X-Ateable-Key": "workspace-test-admin"}


def pair(client, worker_id, concurrency=1, capabilities=None):
    key = client.post("/api/pairing-keys", headers=ADMIN, json={}).json()["pair_key"]
    payload = {
        "pair_key": key,
        "worker_id": worker_id,
        "name": worker_id,
        "max_concurrency": concurrency,
        "output_dir": f"/srv/{worker_id}",
    }
    if capabilities is not None:
        payload["capabilities"] = capabilities
    response = client.post("/api/workers/pair", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def worker_headers(registration):
    return {
        "Authorization": f"Bearer {registration['token']}",
        "X-Ateable-Worker-ID": registration["id"],
    }


def agent_headers(agent):
    return {
        "Authorization": f"Bearer {agent['token']}",
        "X-Ateable-Agent-ID": agent["agent_id"],
    }


def base(space):
    return f"/api/participant/workspaces/{space}"


@pytest.fixture
def env(tmp_path, monkeypatch):
    def forbidden_fetch(*_args, **_kwargs):
        pytest.fail("Controller must not fetch object metadata or payloads")

    monkeypatch.setattr(S3Source, "_get_client", forbidden_fetch)
    config = AppConfig(
        database_path=tmp_path / "master.db",
        output_dir=tmp_path / "no-media",
        admin_key=ADMIN["X-Ateable-Key"],
        embedded_worker=True,
    )
    app = create_app(config)
    with TestClient(app) as client:
        workers = {
            name: pair(client, name, concurrency, capabilities)
            for name, concurrency, capabilities in (
                ("vm-a", 2, ["original", "wav"]),
                ("vm-b", 1, ["original"]),
                ("outside", 16, ["original", "wav"]),
            )
        }
        spaces = {
            name: client.post(
                "/api/workspaces", headers=ADMIN, json={"name": name}
            ).json()["id"]
            for name in ("Team", "Other")
        }
        for worker in ("vm-a", "vm-b"):
            assert (
                client.post(
                    f"/api/workspaces/{spaces['Team']}/members",
                    headers=ADMIN,
                    json={"worker_id": worker},
                ).status_code
                == 201
            )
        client.post(
            f"/api/workspaces/{spaces['Other']}/members",
            headers=ADMIN,
            json={"worker_id": "outside"},
        )
        agent = client.post(
            f"/api/workspaces/{spaces['Team']}/agents",
            headers=ADMIN,
            json={"name": "Research agent"},
        ).json()
        yield client, app.state.workspace_store, workers, spaces, agent
        assert not config.output_dir.exists()


def urls(count=1):
    return [
        f"https://example.com/file-{index}.zip?signature=kept" for index in range(count)
    ]


def test_admin_workspace_members_agents_and_durable_name_metadata(env):
    client, store, _workers, spaces, agent = env
    listing = client.get("/api/workspaces", headers=ADMIN).json()
    assert listing["total"] == 2 and listing["items"][0]["member_count"] == 2
    assert (
        client.patch(
            f"/api/workspaces/{spaces['Team']}",
            headers=ADMIN,
            json={"name": "Shared Lab", "description": "Metadata collaboration"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/workspaces", headers=ADMIN, json={"name": "shared lab"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/workspaces/{spaces['Team']}/members",
            headers=ADMIN,
            json={"worker_id": "local"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/workspaces/{spaces['Team']}/members",
            headers=ADMIN,
            json={"worker_id": "missing"},
        ).status_code
        == 404
    )
    persisted = WorkspaceStore(store.database).get_workspace(spaces["Team"])
    assert persisted["name"] == "Shared Lab" and persisted["agent_count"] == 1
    agents = client.get(
        f"/api/workspaces/{spaces['Team']}/agents", headers=ADMIN
    ).json()
    assert agent["token"] not in json.dumps(agents) and "token_hash" not in json.dumps(
        agents
    )
    with store.database.connect() as connection:
        row = dict(connection.execute("SELECT * FROM workspace_agents").fetchone())
    assert agent["token"] not in json.dumps(row) and len(row["token_hash"]) == 64


def test_participant_auth_identity_scope_and_admin_separation(env):
    client, _store, workers, spaces, agent = env
    worker = worker_headers(workers["vm-a"])
    scoped_agent = agent_headers(agent)
    assert client.get("/api/participant/workspaces").status_code == 401
    assert client.get("/api/participant/workspaces", headers=ADMIN).status_code == 401
    assert (
        client.get(
            "/api/participant/workspaces",
            headers={"Authorization": worker["Authorization"]},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/participant/workspaces",
            headers=worker | {"X-Ateable-Agent-ID": agent["agent_id"]},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/participant/workspaces",
            headers={
                "Authorization": worker["Authorization"],
                "X-Ateable-Agent-ID": agent["agent_id"],
            },
        ).status_code
        == 401
    )
    result = client.get("/api/participant/workspaces", headers=worker).json()
    assert [space["id"] for space in result["items"]] == [spaces["Team"]]
    assert result["actor"] == {"id": "vm-a", "type": "worker", "name": "vm-a"}
    assert (
        client.get(base(spaces["Other"]) + "/members", headers=worker).status_code
        == 403
    )
    for headers in (worker, scoped_agent):
        for path in (
            "/api/workspaces",
            "/api/workers",
            "/api/settings",
            "/api/buckets",
            "/api/pairing-keys",
        ):
            assert client.get(path, headers=headers).status_code in {401, 405}
        assert (
            client.post(
                "/api/workspaces", headers=headers, json={"name": "Forbidden"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                f"/api/workspaces/{spaces['Team']}/members",
                headers=headers,
                json={"worker_id": "outside"},
            ).status_code
            == 401
        )
        assert (
            client.get(base(spaces["Other"]) + "/files", headers=headers).status_code
            == 403
        )
    members = client.get(base(spaces["Team"]) + "/members", headers=worker).json()
    assert all(set(member) == {"id", "name", "status"} for member in members["items"])
    assert members["agents"][0]["id"] == agent["agent_id"]
    assert "token" not in json.dumps(members) and "proxy" not in json.dumps(members)


def test_removed_members_and_revoked_agents_lose_all_workspace_access(env):
    client, store, workers, spaces, agent = env
    stale_actor = store.authenticate(workers["vm-a"]["token"], worker_id="vm-a")
    client.delete(f"/api/workspaces/{spaces['Team']}/members/vm-a", headers=ADMIN)
    worker = worker_headers(workers["vm-a"])
    assert (
        client.get("/api/participant/workspaces", headers=worker).json()["items"] == []
    )
    for suffix in ("/members", "/files", "/jobs", "/messages"):
        assert (
            client.get(base(spaces["Team"]) + suffix, headers=worker).status_code == 403
        )
    assert (
        client.post(
            base(spaces["Team"]) + "/downloads", headers=worker, json={"urls": urls()}
        ).status_code
        == 403
    )
    assert (
        client.post(
            base(spaces["Team"]) + "/messages", headers=worker, json={"text": "Denied"}
        ).status_code
        == 403
    )
    with pytest.raises(AccessDenied):
        store.list_files(spaces["Team"], stale_actor)
    revoked = client.delete(
        f"/api/workspaces/{spaces['Team']}/agents/{agent['agent_id']}", headers=ADMIN
    )
    assert revoked.status_code == 200
    for suffix in ("/members", "/files", "/jobs", "/messages"):
        assert (
            client.get(
                base(spaces["Team"]) + suffix, headers=agent_headers(agent)
            ).status_code
            == 401
        )
    assert (
        client.get(
            "/api/participant/workspaces", headers=agent_headers(agent)
        ).status_code
        == 401
    )


def test_workspace_auto_is_weighted_scoped_and_format_compatible(env):
    client, _store, workers, spaces, agent = env
    response = client.post(
        base(spaces["Team"]) + "/downloads",
        headers=agent_headers(agent),
        json={"urls": urls(6)},
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["dispatched"] == 6
    assert [job["target_worker_id"] for job in result["jobs"]].count("vm-a") == 4
    assert [job["target_worker_id"] for job in result["jobs"]].count("vm-b") == 2
    assert all(
        job["format"] == "original" and "signature=kept" in job["canonical_url"]
        for job in result["jobs"]
    )
    wav = client.post(
        base(spaces["Team"]) + "/downloads",
        headers=agent_headers(agent),
        json={"urls": ["https://example.com/audio.webm"], "format": "wav"},
    )
    assert (
        wav.status_code == 201 and wav.json()["jobs"][0]["target_worker_id"] == "vm-a"
    )
    assert (
        client.get(
            base(spaces["Other"]) + "/jobs", headers=worker_headers(workers["outside"])
        ).json()["total"]
        == 0
    )
    assert (
        client.get(f"/api/workspaces/{spaces['Team']}/jobs", headers=ADMIN).json()[
            "total"
        ]
        == 7
    )


def test_invalid_requests_are_atomic_and_explicit_offline_members_can_queue(env):
    client, store, _workers, spaces, agent = env
    route, headers = base(spaces["Team"]) + "/downloads", agent_headers(agent)
    for body, code in (
        ({"urls": urls(), "target_worker_id": "outside"}, 403),
        ({"urls": urls(), "target_worker_id": "local"}, 422),
        ({"urls": urls(), "target_worker_id": "vm-b", "format": "wav"}, 409),
        ({"urls": urls(), "output_subdir": "../escape"}, 422),
        (
            {
                "urls": [
                    *urls(),
                    "https://user:secret@example.com/path?signature=private",
                ]
            },
            422,
        ),
        ({"urls": ["s3://data-bucket/raw/"], "format": "wav"}, 422),
        ({"urls": urls(), "proxy": "http://unexpected"}, 422),
    ):
        response = client.post(route, headers=headers, json=body)
        assert response.status_code == code, response.text
        assert (
            "secret" not in response.text and "signature=private" not in response.text
        )
    assert store.list_jobs(spaces["Team"])["total"] == 0
    with store.database.connect() as connection:
        connection.execute(
            "UPDATE workers SET last_seen_at='2000-01-01T00:00:00+00:00'"
        )
    unavailable = client.post(route, headers=headers, json={"urls": urls()})
    assert (
        unavailable.status_code == 409 and store.list_jobs(spaces["Team"])["total"] == 0
    )
    explicit = client.post(
        route,
        headers=headers,
        json={
            "urls": urls(),
            "target_worker_id": "vm-b",
            "output_subdir": "saved/2026",
        },
    )
    assert explicit.status_code == 201, explicit.text
    assert explicit.json()["jobs"][0]["target_worker_id"] == "vm-b"


def test_download_idempotency_is_actor_workspace_scoped_and_survives_cleanup(env):
    client, store, workers, spaces, agent = env
    route, headers = base(spaces["Team"]) + "/downloads", agent_headers(agent)
    body = {"urls": urls(), "idempotency_key": "same-key"}
    original = client.post(route, headers=headers, json=body).json()
    replay = client.post(route, headers=headers, json=body).json()
    assert replay["request_id"] == original["request_id"] and replay["replayed"]
    assert replay["jobs"][0]["id"] == original["jobs"][0]["id"]
    assert (
        client.post(route, headers=headers, json={**body, "urls": urls(2)}).status_code
        == 409
    )
    other_actor = client.post(
        route, headers=worker_headers(workers["vm-a"]), json=body
    ).json()
    assert other_actor["request_id"] != original["request_id"]
    with store.database.connect() as connection:
        connection.execute(
            "UPDATE jobs SET status='completed',stage='completed',output_path='/srv/fake.zip'"
        )
    store.database.clear_completed()
    restarted = WorkspaceStore(store.database)
    actor = restarted.authenticate(agent["token"], agent_id=agent["agent_id"])
    after_cleanup = restarted.downloads(spaces["Team"], actor, body)
    assert after_cleanup["replayed"] and after_cleanup["jobs"][0]["queue_removed"]
    assert after_cleanup["jobs"][0]["output_path"] == "/srv/fake.zip"
    assert restarted.list_jobs(spaces["Team"], actor)["total"] == 2
    assert store.database.list_jobs(limit=10, offset=0)[1] == 0


def test_concurrent_download_replays_create_one_atomic_request_and_batch(env):
    client, store, _workers, spaces, agent = env
    barrier = Barrier(4)

    def submit(_):
        barrier.wait()
        return client.post(
            base(spaces["Team"]) + "/downloads",
            headers=agent_headers(agent),
            json={"urls": urls(3), "idempotency_key": "concurrent"},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(submit, range(4)))
    assert all(response.status_code == 201 for response in responses)
    assert len({response.json()["request_id"] for response in responses}) == 1
    assert store.list_jobs(spaces["Team"])["total"] == 3
    with store.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM workspace_download_requests"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1


def test_queue_failure_rolls_back_scope_request_jobs_and_history(env):
    _client, store, _workers, spaces, agent = env
    actor = store.authenticate(agent["token"], agent_id=agent["agent_id"])
    with store.database.connect() as connection:
        connection.executescript("""CREATE TRIGGER reject_second_workspace_job BEFORE INSERT ON jobs
            WHEN NEW.source_url LIKE '%file-1.zip%' BEGIN SELECT RAISE(ABORT,'disk failure'); END;""")
    body = {"urls": urls(2), "idempotency_key": "retry-failure"}
    with pytest.raises(sqlite3.IntegrityError, match="disk failure"):
        store.downloads(spaces["Team"], actor, body)
    with store.database.connect() as connection:
        for table in (
            "workspace_download_requests",
            "workspace_jobs",
            "jobs",
            "batches",
        ):
            assert (
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            )
        connection.execute("DROP TRIGGER reject_second_workspace_job")
    assert store.downloads(spaces["Team"], actor, body)["dispatched"] == 2


def test_lost_reply_after_commit_can_replay_without_duplicate_jobs(env, monkeypatch):
    _client, store, _workers, spaces, agent = env
    actor = store.authenticate(agent["token"], agent_id=agent["agent_id"])
    real_connect = store.database.connect

    @contextmanager
    def lost_reply():
        with real_connect() as connection:
            yield connection
        raise RuntimeError("reply lost after commit")

    body = {"urls": urls(2), "idempotency_key": "lost-reply"}
    monkeypatch.setattr(store.database, "connect", lost_reply)
    with pytest.raises(RuntimeError, match="reply lost"):
        store.downloads(spaces["Team"], actor, body)
    monkeypatch.setattr(store.database, "connect", real_connect)
    recovered = WorkspaceStore(store.database).downloads(spaces["Team"], actor, body)
    assert recovered["replayed"] and recovered["dispatched"] == 2
    assert store.list_jobs(spaces["Team"])["total"] == 2


def complete_fake(client, workers, worker_id, path, size):
    job = client.post(
        "/api/batches",
        headers=ADMIN,
        json={
            "urls": [f"https://example.com/{worker_id}.zip"],
            "target_worker_id": worker_id,
        },
    ).json()["jobs"][0]
    auth = {"Authorization": f"Bearer {workers[worker_id]['token']}"}
    client.post(f"/api/workers/{worker_id}/claim", headers=auth, json={"limit": 1})
    response = client.post(
        f"/api/workers/{worker_id}/jobs/{job['id']}/complete",
        headers=auth,
        json={"output_path": path, "size_bytes": size},
    )
    assert response.status_code == 200, response.text
    return job["id"]


def test_files_are_current_member_ledger_metadata_with_search_totals_and_cleanup(env):
    client, _store, workers, spaces, agent = env
    a = complete_fake(client, workers, "vm-a", "/srv/Research A.zip", 10)
    b = complete_fake(client, workers, "vm-b", "/srv/B.zip", 20)
    outside = complete_fake(client, workers, "outside", "/srv/Hidden.zip", 99)
    client.post("/api/jobs/clear-completed", headers=ADMIN)
    route, headers = base(spaces["Team"]) + "/files", agent_headers(agent)
    catalog = client.get(route, headers=headers, params={"limit": 1}).json()
    assert (
        catalog["total"] == 2
        and catalog["total_bytes"] == 30
        and catalog["present_count"] == 2
    )
    assert len(catalog["items"]) == 1
    research = client.get(route, headers=headers, params={"q": "RESEARCH"}).json()
    assert research["items"][0]["job_id"] == a and research["total_bytes"] == 10
    assert client.get(route + f"/{outside}", headers=headers).status_code == 404
    assert (
        client.get(route + f"/{a}", headers=headers).json()["path"]
        == "/srv/Research A.zip"
    )
    assert (
        client.get(f"/api/workspaces/{spaces['Team']}/files", headers=ADMIN).json()[
            "total"
        ]
        == 2
    )
    client.delete(f"/api/workspaces/{spaces['Team']}/members/vm-b", headers=ADMIN)
    assert client.get(route, headers=headers).json()["total"] == 1
    assert client.get(route + f"/{b}", headers=headers).status_code == 404


def test_discovery_descendants_remain_workspace_scoped_after_parent_cleanup(env):
    client, _store, workers, spaces, agent = env
    result = client.post(
        base(spaces["Team"]) + "/downloads",
        headers=agent_headers(agent),
        json={"urls": ["s3://data-bucket/raw/"], "target_worker_id": "vm-a"},
    ).json()
    parent = result["jobs"][0]
    auth = {"Authorization": f"Bearer {workers['vm-a']['token']}"}
    client.post("/api/workers/vm-a/claim", headers=auth, json={"limit": 1})
    first = client.post(
        f"/api/workers/vm-a/jobs/{parent['id']}/discover",
        headers=auth,
        json={
            "items": [
                {
                    "url": "s3://data-bucket/raw/a.bin",
                    "key": "raw/a.bin",
                    "size_bytes": 5,
                }
            ],
            "continuation_token": "next-page",
        },
    )
    assert first.status_code == 200, first.text
    client.post("/api/jobs/clear-completed", headers=ADMIN)
    client.post("/api/workers/vm-a/claim", headers=auth, json={"limit": 2})
    next_id = first.json()["continuation_job_id"]
    second = client.post(
        f"/api/workers/vm-a/jobs/{next_id}/discover",
        headers=auth,
        json={
            "items": [
                {
                    "url": "s3://data-bucket/raw/b.bin",
                    "key": "raw/b.bin",
                    "size_bytes": 6,
                }
            ],
        },
    )
    assert second.status_code == 200, second.text
    jobs = client.get(
        base(spaces["Team"]) + "/jobs", headers=agent_headers(agent)
    ).json()
    assert jobs["total"] == 4 and all(
        job["request_id"] == result["request_id"] for job in jobs["items"]
    )
    assert any(
        job["id"] == parent["id"]
        and job["queue_removed"]
        and job["stage"] == "expanded"
        for job in jobs["items"]
    )
    assert (
        client.get(
            base(spaces["Other"]) + "/jobs", headers=worker_headers(workers["outside"])
        ).json()["total"]
        == 0
    )


def test_mixed_s3_prefix_exact_dedupe_survives_workspace_queue_cleanup(env):
    client, store, workers, spaces, agent = env
    request = client.post(
        base(spaces["Team"]) + "/downloads",
        headers=agent_headers(agent),
        json={
            "urls": ["s3://data-bucket/raw/", "s3://data-bucket/raw/a.bin"],
            "target_worker_id": "vm-a",
        },
    ).json()
    parent = next(job for job in request["jobs"] if job["kind"] == "discover")
    exact = next(job for job in request["jobs"] if job["kind"] == "download")
    auth = {"Authorization": f"Bearer {workers['vm-a']['token']}"}
    client.post("/api/workers/vm-a/claim", headers=auth, json={"limit": 2})
    client.post(
        f"/api/workers/vm-a/jobs/{exact['id']}/complete",
        headers=auth,
        json={"output_path": "/srv/a.bin", "size_bytes": 3},
    )
    client.post("/api/jobs/clear-completed", headers=ADMIN)
    expanded = client.post(
        f"/api/workers/vm-a/jobs/{parent['id']}/discover",
        headers=auth,
        json={
            "items": [
                {
                    "url": "s3://data-bucket/raw/a.bin",
                    "key": "raw/a.bin",
                    "size_bytes": 3,
                }
            ],
        },
    )
    assert expanded.status_code == 200 and expanded.json()["added_count"] == 0
    assert store.list_jobs(spaces["Team"])["total"] == 2


def test_message_board_trusted_sender_private_visibility_and_cursor_progress(env):
    client, _store, workers, spaces, agent = env
    route = base(spaces["Team"]) + "/messages"
    a, b, observer = (
        worker_headers(workers["vm-a"]),
        worker_headers(workers["vm-b"]),
        agent_headers(agent),
    )
    private = client.post(
        route, headers=a, json={"text": "Private coordination", "recipient_id": "vm-b"}
    ).json()
    assert private["sender_id"] == "vm-a" and private["sender_type"] == "worker"
    hidden = client.get(route, headers=observer, params={"limit": 1}).json()
    assert hidden["items"] == [] and hidden["next_after_seq"] == private["seq"]
    public = client.post(
        route, headers=b, json={"text": "<script>plain text, never execute</script>"}
    ).json()
    visible = client.get(
        route, headers=observer, params={"after_seq": hidden["next_after_seq"]}
    ).json()
    assert visible["items"] == [public]
    assert len(client.get(route, headers=a).json()["items"]) == 2
    assert len(client.get(route, headers=b).json()["items"]) == 2
    assert (
        len(
            client.get(
                f"/api/workspaces/{spaces['Team']}/messages", headers=ADMIN
            ).json()["items"]
        )
        == 2
    )
    assert (
        client.post(
            route, headers=a, json={"text": "Spoof", "sender_id": "outside"}
        ).status_code
        == 422
    )
    assert (
        client.get(route, headers=worker_headers(workers["outside"])).status_code == 403
    )
    assert (
        client.post(
            route, headers=a, json={"text": "Wrong scope", "recipient_id": "outside"}
        ).status_code
        == 404
    )
    assert client.post(route, headers=a, json={"text": "   "}).status_code == 422
    assert client.post(route, headers=a, json={"text": "x" * 10001}).status_code == 422
    to_agent = client.post(
        route, headers=a, json={"text": "Agent note", "recipient_id": agent["agent_id"]}
    ).json()
    assert to_agent["recipient_type"] == "agent"
    assert client.get(route, headers=observer).json()["items"] == [public, to_agent]


def test_message_idempotency_concurrency_filters_and_restart(env):
    client, store, workers, spaces, agent = env
    route, headers = base(spaces["Team"]) + "/messages", agent_headers(agent)
    body, barrier = (
        {"text": "One durable message", "idempotency_key": "message-1"},
        Barrier(4),
    )

    def submit(_):
        barrier.wait()
        return client.post(route, headers=headers, json=body).json()

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(submit, range(4)))
    assert all(message == responses[0] for message in responses)
    assert (
        client.post(
            route, headers=headers, json={**body, "text": "Different"}
        ).status_code
        == 409
    )
    another = client.post(
        route, headers=worker_headers(workers["vm-a"]), json=body
    ).json()
    assert another["id"] != responses[0]["id"]
    restarted = WorkspaceStore(store.database)
    actor = restarted.authenticate(agent["token"], agent_id=agent["agent_id"])
    assert len(restarted.list_messages(spaces["Team"], actor)["items"]) == 2
    filtered = client.get(
        route, headers=headers, params={"recipient_id": "absent", "limit": 1}
    ).json()
    assert (
        filtered["items"] == []
        and filtered["next_after_seq"] == another["seq"]
        and not filtered["has_more"]
    )
    admin = client.post(
        f"/api/workspaces/{spaces['Team']}/messages",
        headers=ADMIN,
        json={"text": "Admin board note"},
    ).json()
    assert admin["sender_type"] == "admin" and admin["sender_id"] == "admin"


def test_download_bounds_and_canonical_duplicates_count_every_submitted_url(env):
    client, store, _workers, spaces, agent = env
    route, headers = base(spaces["Team"]) + "/downloads", agent_headers(agent)
    assert (
        client.post(route, headers=headers, json={"urls": urls() * 5001}).status_code
        == 422
    )
    assert store.list_jobs(spaces["Team"])["total"] == 0
    response = client.post(
        route,
        headers=headers,
        json={
            "urls": [
                "https://youtu.be/abc12345678",
                "https://youtube.com/watch?v=abc12345678&source=share",
            ]
        },
    )
    assert response.status_code == 201 and response.json()["dispatched"] == 1
    assert len(response.json()["duplicates"]) == 1
    assert response.json()["jobs"][0]["source_url"] == "https://youtu.be/abc12345678"


def test_visible_message_limit_skips_hidden_rows_without_losing_later_broadcasts(env):
    client, _store, workers, spaces, agent = env
    route = base(spaces["Team"]) + "/messages"
    a, observer = worker_headers(workers["vm-a"]), agent_headers(agent)
    for index in range(4):
        client.post(
            route, headers=a, json={"text": f"Private {index}", "recipient_id": "vm-b"}
        )
    first = client.post(route, headers=a, json={"text": "First broadcast"}).json()
    client.post(
        route, headers=a, json={"text": "Private between", "recipient_id": "vm-b"}
    )
    second = client.post(route, headers=a, json={"text": "Second broadcast"}).json()
    hidden_tail = client.post(
        route, headers=a, json={"text": "Private tail", "recipient_id": "vm-b"}
    ).json()
    page = client.get(route, headers=observer, params={"limit": 1}).json()
    assert (
        page["items"] == [first]
        and page["next_after_seq"] == first["seq"]
        and page["has_more"]
    )
    final_page = client.get(
        route,
        headers=observer,
        params={"limit": 1, "after_seq": page["next_after_seq"]},
    ).json()
    assert final_page["items"] == [second] and not final_page["has_more"]
    assert final_page["next_after_seq"] == hidden_tail["seq"]
    assert (
        client.get(
            route, headers=observer, params={"after_seq": hidden_tail["seq"]}
        ).json()["items"]
        == []
    )


@pytest.mark.parametrize("identity", ["worker", "agent"])
def test_revocation_between_authentication_and_queue_transaction_cannot_create_jobs(
    env, monkeypatch, identity
):
    _client, store, workers, spaces, agent = env
    import ateable.workspaces as module

    if identity == "worker":
        actor = store.authenticate(workers["vm-a"]["token"], worker_id="vm-a")
        revoke = lambda: store.remove_member(spaces["Team"], "vm-a")
    else:
        actor = store.authenticate(agent["token"], agent_id=agent["agent_id"])
        revoke = lambda: store.revoke_agent(spaces["Team"], agent["agent_id"])
    normalize = module.normalize_download_url

    def revoke_before_write(url):
        result = normalize(url)
        revoke()
        return result

    monkeypatch.setattr(module, "normalize_download_url", revoke_before_write)
    with pytest.raises(AccessDenied):
        store.downloads(
            spaces["Team"],
            actor,
            {"urls": urls(), "idempotency_key": "revocation-race"},
        )
    assert store.list_jobs(spaces["Team"])["total"] == 0
    with store.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM workspace_download_requests"
            ).fetchone()[0]
            == 0
        )


def test_missing_file_is_404_and_presence_is_a_boolean(env):
    client, store, workers, spaces, agent = env
    route, headers = base(spaces["Team"]) + "/files", agent_headers(agent)
    assert client.get(route + "/missing-job-id", headers=headers).status_code == 404
    job_id = complete_fake(client, workers, "vm-a", "/srv/present.zip", 4)
    metadata = client.get(route + "/" + job_id, headers=headers).json()
    assert metadata["present"] is True
    with store.database.connect() as connection:
        connection.execute(
            "UPDATE downloaded_files SET present=0 WHERE job_id=?", (job_id,)
        )
    assert client.get(route + "/" + job_id, headers=headers).json()["present"] is False


def test_allowed_5000_signed_urls_return_bounded_receipt_and_complete_paginated_history(
    env,
):
    client, store, _workers, spaces, agent = env
    route, headers = base(spaces["Team"]) + "/downloads", agent_headers(agent)
    signature = "A" * 2000
    submitted_urls = [
        f"https://files.example/data-{index}.zip?signature={signature}"
        for index in range(5000)
    ]
    body = {"urls": submitted_urls, "idempotency_key": "large-signed-request"}
    created = client.post(route, headers=headers, json=body)
    assert created.status_code == 201, created.text[:1000]
    assert len(created.content) < 16 * 1024 * 1024
    receipt = created.json()
    assert receipt["dispatched"] == receipt["jobs_total"] == 5000
    assert len(receipt["jobs"]) == 50 and receipt["jobs_truncated"]
    assert receipt["duplicate_count"] == 0 and receipt["duplicates"] == []
    assert not receipt["duplicates_truncated"]
    assert receipt["jobs"][0]["source_url"] == submitted_urls[0]
    assert all(job["request_id"] == receipt["request_id"] for job in receipt["jobs"])
    replay = client.post(route, headers=headers, json=body)
    assert replay.status_code == 201 and len(replay.content) < 16 * 1024 * 1024
    replayed = replay.json()
    assert replayed["replayed"] and replayed["request_id"] == receipt["request_id"]
    assert [job["id"] for job in replayed["jobs"]] == [
        job["id"] for job in receipt["jobs"]
    ]
    page = client.get(
        base(spaces["Team"]) + "/jobs",
        headers=headers,
        params={"limit": 1000, "offset": 4000},
    ).json()
    assert page["total"] == 5000 and len(page["items"]) == 1000
    with store.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 5000
        assert (
            connection.execute("SELECT COUNT(*) FROM workspace_jobs").fetchone()[0]
            == 5000
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM workspace_download_requests"
            ).fetchone()[0]
            == 1
        )


def test_large_duplicate_receipt_retains_full_counts_and_bounded_preview(env):
    client, store, _workers, spaces, agent = env
    source = "https://files.example/a.zip?signature=" + "B" * 2000
    body = {"urls": [source] * 5000, "idempotency_key": "large-duplicate-request"}
    response = client.post(
        base(spaces["Team"]) + "/downloads", headers=agent_headers(agent), json=body
    )
    assert response.status_code == 201 and len(response.content) < 16 * 1024 * 1024
    receipt = response.json()
    assert (
        receipt["dispatched"] == receipt["jobs_total"] == 1
        and not receipt["jobs_truncated"]
    )
    assert receipt["duplicate_count"] == 4999 and len(receipt["duplicates"]) == 50
    assert receipt["duplicates_truncated"] and all(
        url == source for url in receipt["duplicates"]
    )
    actor = store.authenticate(agent["token"], agent_id=agent["agent_id"])
    replayed = WorkspaceStore(store.database).downloads(spaces["Team"], actor, body)
    assert replayed["replayed"] and replayed["request_id"] == receipt["request_id"]
    assert replayed["duplicate_count"] == 4999 and replayed["dispatched"] == 1


def test_per_url_model_bound_rejects_large_input_without_creating_request(env):
    client, store, _workers, spaces, agent = env
    source = "https://files.example/a.zip?signature=" + "private-signature" * 1000
    invalid = client.post(
        base(spaces["Team"]) + "/downloads",
        headers=agent_headers(agent),
        json={"urls": [source]},
    )
    assert invalid.status_code == 422 and "private-signature" not in invalid.text
    assert store.list_jobs(spaces["Team"])["total"] == 0
    with store.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM workspace_download_requests"
            ).fetchone()[0]
            == 0
        )
