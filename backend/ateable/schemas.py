"""Public API request and response models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MediaFormat = Literal["original", "wav", "mp3", "m4a", "flac", "opus", "mp4"]
JobStatus = Literal[
    "queued", "downloading", "paused", "completed", "failed", "cancelled"
]
JobAction = Literal["pause", "resume", "cancel", "retry"]
JobFilter = JobStatus | Literal["active", "attention"]


class BatchCreate(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=5000)
    format: MediaFormat = "original"
    target_worker_id: str | None = None
    output_subdir: str | None = Field(default=None, max_length=240)
    proxy: str | None = Field(default=None, max_length=2048)
    cookies_file: str | None = Field(default=None, max_length=2048)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)
    max_attempts: int = Field(default=3, ge=1, le=10)

    @field_validator("urls")
    @classmethod
    def strip_urls(cls, urls: list[str]) -> list[str]:
        return [url.strip() for url in urls]


class Job(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    batch_id: str
    source_url: str
    canonical_url: str
    kind: Literal["download", "discover"] = "download"
    task_options: dict[str, Any] = Field(default_factory=dict)
    source_job_id: str | None = None
    root_job_id: str | None = None
    discovery_count: int = 0
    format: MediaFormat
    status: JobStatus
    progress: float
    stage: str | None = None
    title: str | None = None
    target_worker_id: str | None = None
    claimed_by: str | None = None
    attempts: int
    max_attempts: int
    output_path: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str


class Batch(BaseModel):
    id: str
    status: str
    format: MediaFormat
    target_worker_id: str | None = None
    total: int
    queued: int
    downloading: int
    paused: int
    completed: int
    failed: int
    cancelled: int
    created_at: str


class BatchCreateResponse(BaseModel):
    batch: Batch
    jobs: list[Job]
    duplicates: list[str]


class JobList(BaseModel):
    items: list[Job]
    total: int
    limit: int
    offset: int
    filters_applied: bool = False


class Stats(BaseModel):
    total: int
    queued: int
    downloading: int
    paused: int
    completed: int
    failed: int
    cancelled: int
    active_workers: int


class WorkerRegister(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    worker_id: str | None = Field(default=None, max_length=80)
    capabilities: list[MediaFormat] = Field(
        default_factory=lambda: ["original", "wav", "mp3", "m4a", "flac", "opus", "mp4"]
    )
    max_concurrency: int = Field(default=1, ge=1, le=64)
    output_dir: str | None = Field(default=None, max_length=2048)


class WorkerRegistration(BaseModel):
    id: str
    name: str
    token: str
    poll_interval_seconds: float = 1.0


class StorageInventory(BaseModel):
    output_dir: str = Field(max_length=2048)
    disk_total_bytes: int = Field(ge=0)
    disk_free_bytes: int = Field(ge=0)
    downloaded_bytes: int = Field(ge=0)
    file_count: int = Field(ge=0)
    sampled_at: str = Field(max_length=100)
    scan_complete: bool = True


class FilePresence(BaseModel):
    job_id: str = Field(max_length=100)
    exists: bool
    size_bytes: int | None = Field(default=None, ge=0)


class WorkerInventoryReport(BaseModel):
    inventory: StorageInventory
    files: list[FilePresence] = Field(default_factory=list, max_length=200)


class WorkerProfile(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    capabilities: list[MediaFormat] = Field(min_length=1, max_length=7)
    max_concurrency: int = Field(ge=1, le=64)
    output_dir: str = Field(min_length=1, max_length=2048)


class Worker(BaseModel):
    id: str
    name: str
    status: Literal["online", "offline"]
    last_seen_at: str
    capabilities: list[MediaFormat]
    max_concurrency: int
    output_dir: str | None = None
    cooldown_until: str | None = None
    created_at: str
    inventory: StorageInventory | None = None


class WorkerList(BaseModel):
    items: list[Worker]
    total: int


class Heartbeat(BaseModel):
    active_job_ids: list[str] = Field(default_factory=list, max_length=64)


class ClaimRequest(BaseModel):
    limit: int = Field(default=1, ge=1, le=64)


class ClaimedJob(Job):
    output_subdir: str | None = None
    proxy: str | None = None
    cookies_file: str | None = None
    lease_seconds: int


class ClaimResponse(BaseModel):
    items: list[ClaimedJob]


class ProgressReport(BaseModel):
    progress: float | None = Field(default=None, ge=0, le=100)
    stage: str = Field(default="downloading", max_length=80)
    title: str | None = Field(default=None, max_length=1000)
    downloaded_bytes: int | None = Field(default=None, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    speed: float | None = Field(default=None, ge=0)
    eta: float | None = Field(default=None, ge=0)
    discovered_count: int | None = Field(default=None, ge=0, le=5000)


class CompleteReport(BaseModel):
    output_path: str = Field(min_length=1, max_length=4096)
    title: str | None = Field(default=None, max_length=1000)
    size_bytes: int | None = Field(default=None, ge=0)


class DiscoveredObject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=8192)
    key: str = Field(min_length=1, max_length=1024)
    size_bytes: int = Field(ge=0, le=2**63 - 1, strict=True)
    title: str | None = Field(default=None, max_length=1024)


class DiscoveryReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[DiscoveredObject] = Field(default_factory=list, max_length=5000)
    continuation_token: str | None = Field(default=None, min_length=1, max_length=16384)


class DiscoveryCompletion(BaseModel):
    job: Job
    added_count: int
    continuation_job_id: str | None = None


class FailureReport(BaseModel):
    error: str = Field(min_length=1, max_length=8000)
    retryable: bool = True
    failure_kind: Literal[
        "generic",
        "rate_limited",
        "access_denied",
        "not_found",
        "credentials",
        "source",
        "filesystem",
        "network",
        "network_policy",
        "configuration",
    ] = "generic"
    cooldown_seconds: float | None = Field(default=None, ge=0, le=86400)


class PairingKeyCreate(BaseModel):
    expires_in_seconds: int = Field(default=600, ge=30, le=86400)


class PairingKeyResponse(BaseModel):
    id: str
    pair_key: str
    controller_url: str
    expires_at: str


class WorkerPair(WorkerRegister):
    pair_key: str = Field(min_length=40, max_length=4096)


class BulkActionRequest(BaseModel):
    job_ids: list[str] = Field(default_factory=list, max_length=5000)
    action: JobAction
    status: JobStatus | None = None
    batch_id: str | None = None


class BulkActionResponse(BaseModel):
    updated: int
    items: list[Job]
    errors: dict[str, str]


class SafeSettings(BaseModel):
    default_format: MediaFormat
    output_dir: str
    max_concurrent_downloads: int
    worker_timeout_seconds: int


class SettingsPatch(BaseModel):
    default_format: MediaFormat | None = None
    output_dir: str | None = Field(default=None, min_length=1, max_length=2048)
    max_concurrent_downloads: int | None = Field(default=None, ge=1, le=32)
    worker_timeout_seconds: int | None = Field(default=None, ge=5, le=3600)


class Event(BaseModel):
    id: int
    job_id: str | None
    type: str
    payload: dict[str, Any]
    created_at: str


class EventList(BaseModel):
    items: list[Event]
    next_after_id: int


class NetworkProbeRequest(BaseModel):
    mode: Literal["direct", "proxy", "tor"]
    proxy: str | None = Field(default=None, max_length=2048)


class NetworkProbeResponse(BaseModel):
    mode: Literal["direct", "proxy", "tor"]
    proxy: str | None
    reachable: bool
    message: str
