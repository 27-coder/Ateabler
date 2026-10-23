from pathlib import Path

import pytest

from ateable.worker_inventory import WorkerInventoryTracker


def test_inventory_tracks_completed_files_and_deletion_without_reading_media(
    tmp_path: Path,
) -> None:
    output = tmp_path / "worker-downloads"
    output.mkdir()
    file = output / "archive.zip"
    file.write_bytes(b"a small worker fixture")
    (output / "unfinished.part").write_bytes(b"partial")
    tracker = WorkerInventoryTracker(output, tmp_path / "state" / "inventory.json")
    assert tracker.record("job-1", str(file)) == 22
    report = tracker.snapshot()
    assert report["inventory"]["downloaded_bytes"] == 22
    assert report["inventory"]["file_count"] == 1
    assert (
        report["inventory"]["disk_free_bytes"]
        <= report["inventory"]["disk_total_bytes"]
    )
    assert report["files"] == [{"job_id": "job-1", "exists": True, "size_bytes": 22}]
    resumed = WorkerInventoryTracker(output, tracker.state_file)
    file.unlink()
    assert resumed.snapshot()["files"] == [
        {"job_id": "job-1", "exists": False, "size_bytes": None}
    ]


def test_worker_inventory_rejects_outside_file_and_ignores_symlinks(
    tmp_path: Path,
) -> None:
    output = tmp_path / "out"
    output.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_bytes(b"outside")
    tracker = WorkerInventoryTracker(output, tmp_path / "state.json")
    with pytest.raises(ValueError, match="outside"):
        tracker.record("job-1", str(outside))
    try:
        (output / "linked.txt").symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks unavailable")
    assert tracker.snapshot()["inventory"]["file_count"] == 0
