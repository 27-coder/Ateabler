"""FastAPI coordinator application."""

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, status

from .buckets import register_bucket_routes
from .config import AppConfig
from .database import ConflictError, Database, GoneError, NotFoundError
from .downloader import Downloader
from .network import probe_network
from .pairing import PairKeyError, decode_pair_key
from .schemas import (
    Batch,
    BatchCreate,
    BatchCreateResponse,
    BulkActionRequest,
    BulkActionResponse,
    ClaimRequest,
    ClaimResponse,
    CompleteReport,
    DiscoveryCompletion,
    DiscoveryReport,
    EventList,
    FailureReport,
    Heartbeat,
    Job,
    JobFilter,
    JobList,
    NetworkProbeRequest,
    NetworkProbeResponse,
    PairingKeyCreate,
    PairingKeyResponse,
    ProgressReport,
    SafeSettings,
    SettingsPatch,
    Stats,
    WorkerInventoryReport,
    WorkerList,
    WorkerPair,
    WorkerProfile,
    WorkerRegistration,
)
from .validation import (
    ValidationError,
    normalize_download_url,
    validate_output_subdir,
    validate_proxy,
)
from .workspaces import register_workspace_routes

logger = logging.getLogger(__name__)


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, GoneError):
        return HTTPException(status_code=410, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Worker bearer token required")
    return authorization.removeprefix("Bearer ").strip()


def create_app(
    config: AppConfig | None = None,
    *,
    downloader: Downloader | None = None,
) -> FastAPI:
    # The master owns control metadata only, even with a legacy local-worker flag.
    config = (config or AppConfig.from_env()).with_overrides(embedded_worker=False)
    database = Database(config)
    database.initialize()
    if config.admin_key is None:
        logger.warning(
            "Generated Ateable admin key ending in …%s; run "
            "`ateable device info --show-secret` locally to reveal it",
            database.admin_key[-4:],
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(
        title="Ateable coordinator",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.database = database
    app.state.admin_key = database.admin_key

    def worker_auth(
        worker_id: str, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        try:
            return database.authenticate_worker(worker_id, _bearer(authorization))
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def admin_auth(
        x_ateable_key: Annotated[str | None, Header(alias="X-Ateable-Key")] = None,
    ) -> None:
        if not x_ateable_key or not hmac.compare_digest(
            x_ateable_key, database.admin_key
        ):
            raise HTTPException(status_code=401, detail="Invalid Ateable admin key")

    control = APIRouter(
        prefix="/api",
        dependencies=[Depends(admin_auth)],
    )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        try:
            with database.connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return {"status": "healthy", "version": "0.1.0"}
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail=f"Database unavailable: {exc}"
            ) from exc

    @control.post(
        "/batches",
        response_model=BatchCreateResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_batch(payload: BatchCreate) -> dict[str, Any]:
        normalized: list[tuple[str, str, str]] = []
        duplicates: list[str] = []
        seen: set[str] = set()
        invalid: list[dict[str, Any]] = []
        for index, source_url in enumerate(payload.urls):
            try:
                canonical, video_id = normalize_download_url(source_url)
            except ValidationError as exc:
                invalid.append({"index": index, "url": source_url, "message": str(exc)})
                continue
            if video_id in seen:
                duplicates.append(source_url)
                continue
            seen.add(video_id)
            normalized.append((source_url, canonical, video_id))
        if invalid:
            raise HTTPException(
                status_code=422,
                detail={"message": "One or more URLs are invalid", "errors": invalid},
            )
        try:
            output_subdir = validate_output_subdir(payload.output_subdir)
            proxy = validate_proxy(payload.proxy)
            batch, jobs, stored_duplicates, _ = database.enqueue_batch(
                urls=normalized,
                media_format=payload.format,
                target_worker_id=payload.target_worker_id,
                output_subdir=output_subdir,
                proxy=proxy,
                cookies_file=payload.cookies_file,
                idempotency_key=payload.idempotency_key,
                max_attempts=payload.max_attempts,
                duplicates=duplicates,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (NotFoundError, ConflictError) as exc:
            raise _http_error(exc) from exc
        return {"batch": batch, "jobs": jobs, "duplicates": stored_duplicates}

    @control.get("/batches/{batch_id}", response_model=Batch)
    def get_batch(batch_id: str) -> dict[str, Any]:
        try:
            return database.get_batch(batch_id)
        except NotFoundError as exc:
            raise _http_error(exc) from exc

    @control.get("/jobs", response_model=JobList)
    def list_jobs(
        job_status: Annotated[JobFilter | None, Query(alias="status")] = None,
        batch_id: str | None = None,
        worker_id: str | None = None,
        query: Annotated[str | None, Query(alias="q", max_length=500)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        items, total = database.list_jobs(
            status=job_status,
            batch_id=batch_id,
            worker_id=worker_id,
            query=query,
            limit=limit,
            offset=offset,
        )
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "filters_applied": True,
        }

    # Static /api/jobs routes must be declared before /api/jobs/{job_id}.
    @control.post("/jobs/actions", response_model=BulkActionResponse)
    def bulk_action(payload: BulkActionRequest) -> dict[str, Any]:
        try:
            updated, items, errors = database.apply_action(
                action=payload.action,
                job_ids=payload.job_ids,
                status=payload.status,
                batch_id=payload.batch_id,
            )
        except ConflictError as exc:
            raise _http_error(exc) from exc
        return {"updated": updated, "items": items, "errors": errors}

    @control.post("/jobs/clear-completed")
    def clear_completed() -> dict[str, int]:
        return {"deleted": database.clear_completed()}

    @control.get("/jobs/{job_id}", response_model=Job)
    def get_job(job_id: str) -> dict[str, Any]:
        try:
            return database.get_job(job_id)
        except NotFoundError as exc:
            raise _http_error(exc) from exc

    def apply_single(job_id: str, action: str) -> dict[str, Any]:
        updated, items, errors = database.apply_action(
            action=action, job_ids=[job_id], status=None, batch_id=None
        )
        if not updated:
            message = errors.get(job_id, "Job was not updated")
            code = 404 if message == "Job was not found" else 409
            raise HTTPException(status_code=code, detail=message)
        return items[0]

    @control.post("/jobs/{job_id}/pause", response_model=Job)
    def pause_job(job_id: str) -> dict[str, Any]:
        return apply_single(job_id, "pause")

    @control.post("/jobs/{job_id}/resume", response_model=Job)
    def resume_job(job_id: str) -> dict[str, Any]:
        return apply_single(job_id, "resume")

    @control.post("/jobs/{job_id}/cancel", response_model=Job)
    def cancel_job(job_id: str) -> dict[str, Any]:
        return apply_single(job_id, "cancel")

    @control.post("/jobs/{job_id}/retry", response_model=Job)
    def retry_job(job_id: str) -> dict[str, Any]:
        return apply_single(job_id, "retry")

    @control.get("/stats", response_model=Stats)
    def stats() -> dict[str, Any]:
        return database.stats()

    @control.get("/workers", response_model=WorkerList)
    def workers() -> dict[str, Any]:
        items = database.list_workers()
        return {"items": items, "total": len(items)}

    @control.get("/workers/{worker_id}/storage")
    def worker_storage(
        worker_id: str, limit: Annotated[int, Query(ge=1, le=1000)] = 100
    ) -> dict[str, Any]:
        try:
            return database.worker_storage(worker_id, limit)
        except NotFoundError as exc:
            raise _http_error(exc) from exc

    @app.post("/api/workers/{worker_id}/inventory", status_code=204)
    def worker_inventory(
        worker_id: str,
        payload: WorkerInventoryReport,
        _: Annotated[dict[str, Any], Depends(worker_auth)],
    ) -> None:
        database.report_inventory(
            worker_id,
            payload.inventory.model_dump(),
            [item.model_dump() for item in payload.files],
        )

    @app.post("/api/workers/{worker_id}/profile", status_code=204)
    def worker_profile(
        worker_id: str,
        payload: WorkerProfile,
        _: Annotated[dict[str, Any], Depends(worker_auth)],
    ) -> None:
        database.update_worker_profile(worker_id, payload.model_dump())

    @app.post("/api/workers/pair", response_model=WorkerRegistration)
    def pair_worker(payload: WorkerPair) -> dict[str, Any]:
        try:
            pair = decode_pair_key(payload.pair_key)
            worker, token = database.consume_pairing_key(
                key_id=pair.key_id,
                secret=pair.secret,
                controller_url=pair.controller_url,
                name=payload.name,
                worker_id=payload.worker_id,
                capabilities=list(payload.capabilities),
                max_concurrency=payload.max_concurrency,
                output_dir=payload.output_dir,
            )
        except PairKeyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except (ConflictError, GoneError) as exc:
            raise _http_error(exc) from exc
        return {**worker, "token": token, "poll_interval_seconds": 1.0}

    @control.post("/pairing-keys", response_model=PairingKeyResponse)
    def create_pairing_key(payload: PairingKeyCreate) -> dict[str, str]:
        return database.create_pairing_key(
            controller_url=config.controller_url,
            expires_in_seconds=payload.expires_in_seconds,
        )

    @control.post("/pairing-keys/regenerate", response_model=PairingKeyResponse)
    def regenerate_pairing_key(payload: PairingKeyCreate) -> dict[str, str]:
        database.revoke_unused_pairing_keys()
        return database.create_pairing_key(
            controller_url=config.controller_url,
            expires_in_seconds=payload.expires_in_seconds,
        )

    @control.post("/network/probe", response_model=NetworkProbeResponse)
    def network_probe(payload: NetworkProbeRequest) -> dict[str, Any]:
        selected_proxy = payload.proxy
        if payload.mode == "tor" and selected_proxy is None:
            selected_proxy = config.tor_proxy
        try:
            return probe_network(
                payload.mode,
                selected_proxy if payload.mode == "proxy" else None,
                selected_proxy or config.tor_proxy,
            ).to_dict()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/workers/{worker_id}/heartbeat", status_code=204)
    def heartbeat(
        worker_id: str,
        payload: Heartbeat,
        _: Annotated[dict[str, Any], Depends(worker_auth)],
    ) -> None:
        database.heartbeat_worker(worker_id, payload.active_job_ids)

    @app.post("/api/workers/{worker_id}/claim", response_model=ClaimResponse)
    def claim(
        worker_id: str,
        payload: ClaimRequest,
        worker: Annotated[dict[str, Any], Depends(worker_auth)],
    ) -> dict[str, Any]:
        import json

        items = database.claim_jobs(
            worker_id=worker_id,
            capabilities=json.loads(worker["capabilities"]),
            limit=min(payload.limit, worker["max_concurrency"]),
            local=False,
        )
        return {"items": items}

    @app.post("/api/workers/{worker_id}/jobs/{job_id}/progress", response_model=Job)
    def worker_progress(
        worker_id: str,
        job_id: str,
        payload: ProgressReport,
        _: Annotated[dict[str, Any], Depends(worker_auth)],
    ) -> dict[str, Any]:
        try:
            return database.report_progress(
                worker_id=worker_id,
                job_id=job_id,
                progress=payload.progress,
                stage=payload.stage,
                title=payload.title,
                metrics={
                    key: value
                    for key, value in {
                        "downloaded_bytes": payload.downloaded_bytes,
                        "total_bytes": payload.total_bytes,
                        "speed": payload.speed,
                        "eta": payload.eta,
                        "discovered_count": payload.discovered_count,
                    }.items()
                    if value is not None
                },
            )
        except (NotFoundError, ConflictError) as exc:
            raise _http_error(exc) from exc

    @app.post(
        "/api/workers/{worker_id}/jobs/{job_id}/discover",
        response_model=DiscoveryCompletion,
    )
    def worker_discover(
        worker_id: str,
        job_id: str,
        payload: DiscoveryReport,
        _: Annotated[dict[str, Any], Depends(worker_auth)],
    ) -> dict[str, Any]:
        try:
            return database.complete_discovery(
                worker_id=worker_id,
                job_id=job_id,
                items=[item.model_dump() for item in payload.items],
                continuation_token=payload.continuation_token,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (NotFoundError, ConflictError) as exc:
            raise _http_error(exc) from exc

    @app.post("/api/workers/{worker_id}/jobs/{job_id}/complete", response_model=Job)
    def worker_complete(
        worker_id: str,
        job_id: str,
        payload: CompleteReport,
        _: Annotated[dict[str, Any], Depends(worker_auth)],
    ) -> dict[str, Any]:
        try:
            return database.complete_job(
                worker_id=worker_id,
                job_id=job_id,
                output_path=payload.output_path,
                title=payload.title,
                size_bytes=payload.size_bytes,
            )
        except (NotFoundError, ConflictError) as exc:
            raise _http_error(exc) from exc

    @app.post("/api/workers/{worker_id}/jobs/{job_id}/fail", response_model=Job)
    def worker_fail(
        worker_id: str,
        job_id: str,
        payload: FailureReport,
        _: Annotated[dict[str, Any], Depends(worker_auth)],
    ) -> dict[str, Any]:
        try:
            return database.fail_job(
                worker_id=worker_id,
                job_id=job_id,
                error=payload.error,
                retryable=payload.retryable,
                failure_kind=payload.failure_kind,
                cooldown_seconds=payload.cooldown_seconds,
            )
        except (NotFoundError, ConflictError) as exc:
            raise _http_error(exc) from exc

    @control.get("/events", response_model=EventList)
    def events(
        after_id: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> dict[str, Any]:
        items, next_after_id = database.events(after_id, limit)
        return {"items": items, "next_after_id": next_after_id}

    @control.get("/settings", response_model=SafeSettings)
    def settings() -> dict[str, Any]:
        return database.get_settings()

    @control.patch("/settings", response_model=SafeSettings)
    def patch_settings(payload: SettingsPatch) -> dict[str, Any]:
        return database.patch_settings(payload.model_dump(exclude_unset=True))

    register_bucket_routes(app, control, database)
    register_workspace_routes(app, control, database)
    app.include_router(control)
    return app


def main() -> None:
    import uvicorn

    config = AppConfig.from_env()
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
