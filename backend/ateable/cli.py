"""The `ateable` command-line interface."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from .config import AppConfig
from .database import Database
from .device_config import (
    ROLES,
    DeviceConfig,
    default_config_dir,
    default_config_path,
)
from .downloader import detect_js_runtimes, find_ffmpeg_executable
from .network import (
    NETWORK_MODES,
    control_request_opener,
    effective_proxy,
    probe_network,
)
from .pairing import decode_pair_key
from .remote_worker import RemoteWorker, WorkerConfig
from .tor_manager import ensure_tor, tor_status
from .validation import normalize_download_url

FORMATS = ("original", "wav", "mp3", "m4a", "flac", "opus", "mp4")


class CliError(RuntimeError):
    pass


class ApiError(CliError):
    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


class ApiClient:
    def __init__(
        self,
        controller_url: str,
        admin_key: str | None = None,
        *,
        opener: Callable[..., Any] | None = None,
    ):
        self.controller_url = controller_url.rstrip("/")
        self.admin_key = admin_key
        self._opener = opener if opener is not None else control_request_opener()

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        admin: bool = True,
    ) -> Any:
        encoded = None if body is None else json.dumps(body).encode()
        headers = {"Accept": "application/json"}
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        if admin:
            if not self.admin_key:
                raise CliError(
                    "No admin key is configured; run `ateable setup` or set "
                    "ATEABLE_ADMIN_KEY"
                )
            headers["X-Ateable-Key"] = self.admin_key
        request = urllib.request.Request(
            self.controller_url + path,
            data=encoded,
            method=method,
            headers=headers,
        )
        try:
            with self._opener(request, timeout=30) as response:
                data = response.read()
                return json.loads(data) if data else None
        except urllib.error.HTTPError as exc:
            try:
                if 300 <= exc.code < 400:
                    raise ApiError(
                        exc.code,
                        "Controller redirects are blocked; configure its final URL",
                    ) from None
                try:
                    detail = json.loads(exc.read()).get("detail", exc.reason)
                except (ValueError, AttributeError):
                    detail = exc.reason
            finally:
                exc.close()
            raise ApiError(exc.code, str(detail)) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ApiError(None, f"Controller unavailable: {exc}") from exc


def allocate_urls(
    urls: list[str], workers: list[dict[str, Any]], media_format: str
) -> dict[str, list[str]]:
    """Deterministic capacity-weighted allocation across online devices."""
    eligible = sorted(
        (
            worker
            for worker in workers
            if worker.get("status") == "online"
            and worker.get("id") != "local"
            and media_format in worker.get("capabilities", [])
        ),
        key=lambda worker: worker["id"],
    )
    slots: list[str] = []
    for worker in eligible:
        weight = max(1, min(64, int(worker.get("max_concurrency") or 1)))
        slots.extend([worker["id"]] * weight)
    if not slots:
        raise CliError(f"No online device supports {media_format}")
    allocation: dict[str, list[str]] = defaultdict(list)
    for index, url in enumerate(urls):
        allocation[slots[index % len(slots)]].append(url)
    return dict(allocation)


def _url_inputs(positional: list[str], file_path: str | None) -> list[str]:
    values = list(positional)
    if file_path:
        if file_path == "-":
            text = sys.stdin.read()
        else:
            text = Path(file_path).read_text(encoding="utf-8")
        values.extend(
            token
            for line in text.splitlines()
            if not line.lstrip().startswith("#")
            for token in line.split()
        )
    return [value.strip() for value in values if value.strip()]


def _read_urls(positional: list[str], file_path: str | None) -> list[str]:
    values = _url_inputs(positional, file_path)
    urls = list(
        dict.fromkeys(
            normalize_download_url(value)[0] for value in values if value.strip()
        )
    )
    if not urls:
        raise CliError("Provide at least one URL or --file")
    if len(urls) > 5000:
        raise CliError("A single add command accepts at most 5,000 URLs")
    return urls


def _read_capture_urls(positional: list[str], file_path: str | None) -> list[str]:
    values = _url_inputs(positional, file_path)
    if not values:
        raise CliError("Provide at least one URL or --file")
    if len(values) > 5000:
        raise CliError("A single capture command accepts at most 5,000 URLs")
    for value in values:
        normalize_download_url(value)
    # The collection retains the original URL; canonical dedupe occurs in its
    # transaction, so accepted+duplicate counts reflect every submitted link.
    return values


def _print(value: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                rendered = json.dumps(item, ensure_ascii=False)
            elif item is None:
                rendered = "—"
            else:
                rendered = str(item)
            print(f"{key.replace('_', ' ').title()}: {rendered}")
    elif isinstance(value, list):
        for item in value:
            print(json.dumps(item, ensure_ascii=False, default=str))
    else:
        print(value)


def _controller_config(device: DeviceConfig, args: argparse.Namespace) -> AppConfig:
    base = AppConfig.from_env()
    port = getattr(args, "port", None) or base.port
    host = getattr(args, "host", None) or base.host
    controller_url = (
        getattr(args, "controller_url", None)
        or device.controller_url
        or base.controller_url
    )
    return base.with_overrides(
        host=host,
        port=port,
        controller_url=controller_url.rstrip("/"),
        output_dir=Path(device.output_dir).expanduser().resolve(),
        admin_key=device.admin_key or base.admin_key,
        embedded_worker=False,
        network_mode=device.network_mode,
        proxy=device.proxy,
        tor_proxy=device.tor_proxy,
    )


def command_setup(args: argparse.Namespace) -> dict[str, Any]:
    config = DeviceConfig.load()
    role = args.role or config.role
    name = args.name or config.name
    controller_url = (args.controller or config.controller_url).rstrip("/")
    output_dir = str(Path(args.output_dir or config.output_dir).expanduser().resolve())
    network_mode = args.network_mode or config.network_mode
    proxy = args.proxy if args.proxy is not None else config.proxy
    tor_proxy = args.tor_proxy or config.tor_proxy
    effective_proxy(network_mode, proxy, tor_proxy)
    config = config.updated(
        name=name,
        role=role,
        controller_url=controller_url,
        output_dir=output_dir,
        network_mode=network_mode,
        proxy=proxy,
        tor_proxy=tor_proxy,
    )
    tor = (
        tor_status(tor_proxy, timeout=0.5, verify_circuit=False).to_dict()
        if role == "worker" and network_mode == "tor"
        else None
    )
    if role in {"controller", "both"}:
        app_config = _controller_config(config, args)
        database = Database(app_config)
        database.initialize()
        pair = database.create_pairing_key(
            controller_url=controller_url,
            expires_in_seconds=app_config.pair_ttl_seconds,
        )
        config = config.updated(
            admin_key=database.admin_key,
            last_pair_key=pair["pair_key"],
        )
    path = config.save()
    if args.automation:
        return {
            "configured": True,
            "privacy": "redacted",
            "role": config.role,
            "network_mode": config.network_mode,
            "admin_auth_ready": bool(config.admin_key),
            "pair_key_created": bool(config.last_pair_key),
        }
    return {
        "configured": True,
        "config_path": str(path),
        "device_id": config.device_id,
        "role": config.role,
        "controller_url": config.controller_url,
        "output_dir": config.output_dir,
        "network_mode": config.network_mode,
        "tor": tor,
    }


def command_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .api import create_app

    device = DeviceConfig.load()
    if device.role not in {"controller", "both"}:
        raise CliError("This device is not configured as a controller")
    config = _controller_config(device, args)
    if config.embedded_worker and config.network_mode == "tor":
        ensure_tor(config.tor_proxy)
    uvicorn.run(create_app(config), host=config.host, port=config.port, reload=False)


def command_worker(args: argparse.Namespace) -> None:
    if getattr(sys, "frozen", False):
        raise CliError(
            "Native Admin/Node packages cannot run a download worker. "
            "Install the source pack with --role worker on your download device."
        )
    device = DeviceConfig.load()
    pair_key = args.pair_key or os.getenv("ATEABLE_PAIR_KEY")
    controller = args.coordinator or device.controller_url
    if pair_key:
        controller = decode_pair_key(pair_key).controller_url
    network_mode = args.network_mode or device.network_mode
    proxy = args.proxy if args.proxy is not None else device.proxy
    tor_proxy = args.tor_proxy or device.tor_proxy
    effective_proxy(network_mode, proxy, tor_proxy)
    if network_mode == "tor":
        ensure_tor(tor_proxy)
    config = WorkerConfig(
        coordinator_url=controller,
        name=args.name or device.name,
        pair_key=pair_key,
        worker_id=device.worker_id or device.device_id,
        worker_token=device.worker_token,
        output_dir=Path(args.output_dir or device.output_dir).expanduser().resolve(),
        concurrency=args.concurrency,
        credentials_file=default_config_dir() / "worker-credentials.json",
        proxy=proxy,
        network_mode=network_mode,
        tor_proxy=tor_proxy,
        cookies_file=args.cookies_file or os.getenv("ATEABLE_COOKIES_FILE"),
        poll_interval=args.poll_interval,
    )
    RemoteWorker(config).run()


def _readiness() -> dict[str, Any]:
    ffmpeg, source = find_ffmpeg_executable()
    try:
        import yt_dlp

        yt_dlp_version = yt_dlp.version.__version__
    except (ImportError, AttributeError):
        yt_dlp_version = None
    runtimes = detect_js_runtimes()
    return {
        "ffmpeg": {
            "ready": bool(ffmpeg),
            "path": ffmpeg,
            "source": source,
            "ffprobe": bool(shutil.which("ffprobe")),
        },
        "yt_dlp": {"ready": bool(yt_dlp_version), "version": yt_dlp_version},
        "javascript_runtime": {
            "ready": bool(runtimes),
            "runtimes": list(runtimes),
        },
    }


def command_doctor(args: argparse.Namespace) -> dict[str, Any]:
    """Return automation-safe readiness without host identity or secret material."""
    configured = default_config_path().is_file()
    config = DeviceConfig.load()
    readiness = _readiness() if config.role == "worker" else {}
    controller_auth_ready = config.role not in {"controller", "both"} or bool(
        config.admin_key
    )
    worker_auth_ready = config.role != "worker" or bool(config.worker_token)
    checks = {
        "configured": configured,
        "controller_auth": controller_auth_ready,
        "worker_auth": worker_auth_ready,
        **(
            {
                "ffmpeg": readiness["ffmpeg"]["ready"],
                "yt_dlp": readiness["yt_dlp"]["ready"],
                "javascript_runtime": readiness["javascript_runtime"]["ready"],
            }
            if readiness
            else {}
        ),
    }
    next_actions = []
    if not configured:
        next_actions.append(
            "run setup with explicit role, output directory, and network mode"
        )
    if configured and not controller_auth_ready:
        next_actions.append("run controller setup to generate admin authentication")
    if configured and not worker_auth_ready:
        next_actions.append("pair this worker using a single-use pairing key")
    if checks.get("ffmpeg") is False:
        next_actions.append("install FFmpeg")
    if checks.get("yt_dlp") is False:
        next_actions.append("install Ateable dependencies")
    if checks.get("javascript_runtime") is False:
        next_actions.append("install Deno 2.3+ or Node.js 22+")
    return {
        "ready": all(checks.values()),
        "privacy": "redacted",
        "role": config.role if configured else None,
        "network_mode": config.network_mode if configured else None,
        "checks": checks,
        "next_actions": next_actions,
    }


def command_device_info(args: argparse.Namespace) -> dict[str, Any]:
    config = DeviceConfig.load()
    tor = (
        tor_status(config.tor_proxy)
        if config.role == "worker" and config.network_mode == "tor"
        else None
    )
    network = (
        probe_network(config.network_mode, config.proxy, config.tor_proxy, timeout=1)
        if tor is None and config.role == "worker"
        else None
    )
    admin_display: str | None
    if args.show_secret:
        admin_display = config.admin_key
    elif config.admin_key:
        admin_display = f"configured (…{config.admin_key[-4:]})"
    else:
        admin_display = None
    return {
        "role": config.role,
        "device_id": config.device_id,
        "name": config.name,
        # DeviceInfo is the intentional local handoff surface for a worker.
        # The admin key remains redacted unless --show-secret is explicit.
        "pair_key": config.last_pair_key,
        "controller_url": config.controller_url,
        "output_folder": config.output_dir,
        "network_mode": config.network_mode,
        "proxy": (config.tor_proxy if config.network_mode == "tor" else config.proxy),
        "tor_reachable": (tor.bootstrapped if tor is not None else None),
        "tor": tor.to_dict() if tor is not None else None,
        "network_message": tor.message
        if tor is not None
        else network.message
        if network is not None
        else "Download networking is configured per remote worker",
        "admin_auth": {
            "ready": bool(config.admin_key),
            "key": admin_display,
        },
        **(
            _readiness()
            if config.role == "worker"
            else {"download_execution": "remote workers only"}
        ),
    }


def command_device_key(args: argparse.Namespace) -> dict[str, Any]:
    config = DeviceConfig.load()
    client = ApiClient(config.controller_url, config.admin_key)
    route = "/api/pairing-keys/regenerate" if args.regenerate else "/api/pairing-keys"
    result = client.request("POST", route, {"expires_in_seconds": args.expires})
    config.updated(last_pair_key=result["pair_key"]).save()
    return result


def command_device_pair(args: argparse.Namespace) -> dict[str, Any]:
    pair = decode_pair_key(args.pair_key)
    config = DeviceConfig.load()
    client = ApiClient(pair.controller_url)
    result = client.request(
        "POST",
        "/api/workers/pair",
        {
            "pair_key": args.pair_key,
            "name": args.name or config.name,
            "worker_id": config.device_id,
            "max_concurrency": args.concurrency,
            "output_dir": config.output_dir,
            "capabilities": list(FORMATS),
        },
        admin=False,
    )
    role = "both" if config.role == "controller" else "worker"
    config.updated(
        role=role,
        controller_url=pair.controller_url,
        worker_id=result["id"],
        worker_token=result["token"],
        last_pair_key=None,
    ).save()
    return {
        "paired": True,
        "device_id": result["id"],
        "controller_url": pair.controller_url,
    }


def command_device_list(args: argparse.Namespace) -> dict[str, Any]:
    config = DeviceConfig.load()
    return ApiClient(config.controller_url, config.admin_key).request(
        "GET", "/api/workers"
    )


def command_device_ssh(args: argparse.Namespace) -> None:
    """Keep a remote worker and its reverse SSH tunnel alive in this command."""
    from .ssh_devices import SshDeviceSpec, SshDeviceStore, SshProvisioner

    config = DeviceConfig.load()
    store = SshDeviceStore(default_config_dir() / "ssh-devices.json")
    saved = store.load().get(args.host, SshDeviceSpec(args.host))
    values = asdict(saved)
    for name in (
        "user",
        "port",
        "identity_file",
        "remote_dir",
        "output_dir",
        "concurrency",
        "network_mode",
        "proxy",
        "tunnel_port",
        "workspace_id",
        "s3_endpoint",
        "s3_profile",
    ):
        value = getattr(args, name)
        if value is not None:
            values[name] = value
    spec = SshDeviceSpec(**values)
    provisioner = SshProvisioner()
    try:
        api = ApiClient(config.controller_url, config.admin_key)
        if spec.workspace_id:
            api.request("GET", f"/api/workspaces/{spec.workspace_id}")
        if not args.connect_only:
            provisioner.provision(spec)
        pair_key = None
        if not args.connect_only:
            pair = ApiClient(config.controller_url, config.admin_key).request(
                "POST", "/api/pairing-keys", {"expires_in_seconds": 600}
            )
            pair_key = pair["pair_key"]
        handle = provisioner.connect(spec, config.controller_url, pair_key=pair_key)
        if spec.workspace_id:
            api.request(
                "POST",
                f"/api/workspaces/{spec.workspace_id}/members",
                {"worker_id": handle.worker_id},
            )
        try:
            store.save(spec.host, spec)
        except (OSError, ValueError, RuntimeError):
            logging.getLogger(__name__).warning(
                "Worker connected, but its SSH settings could not be saved"
            )
        _print(
            {
                "connected": True,
                "device_id": handle.worker_id,
                "workspace_id": spec.workspace_id,
                "node_cli": spec.remote_dir.rstrip("/") + "/bin/ateable-node",
                "connection": "SSH tunnel; keep this command running; Ctrl+C disconnects",
            },
            as_json=args.json,
        )
        while handle.poll() is None:
            time.sleep(1)
        exit_code = handle.poll()
        if exit_code:
            raise CliError(f"SSH worker connection exited ({exit_code})")
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        raise CliError(str(exc)) from exc
    finally:
        provisioner.stop_all()


def command_add(args: argparse.Namespace) -> dict[str, Any]:
    urls = _read_urls(args.urls, args.file)
    config = DeviceConfig.load()
    client = ApiClient(config.controller_url, config.admin_key)
    if args.device == "auto":
        workers = client.request("GET", "/api/workers")["items"]
        allocations = allocate_urls(urls, workers, args.format)
    else:
        target = args.device
        if not target or target == "local":
            raise CliError(
                "The controller cannot download files. Select auto or an online remote device."
            )
        allocations = {target: urls}
    batches = []
    for device_id, assigned in allocations.items():
        result = client.request(
            "POST",
            "/api/batches",
            {
                "urls": assigned,
                "format": args.format,
                "target_worker_id": device_id,
                "idempotency_key": str(uuid.uuid4()),
            },
        )
        batches.append(
            {
                "device_id": device_id,
                "batch_id": result["batch"]["id"],
                "jobs": result["batch"]["total"],
                "duplicates": len(result["duplicates"]),
            }
        )
    return {
        "submitted": sum(batch["jobs"] for batch in batches),
        "format": args.format,
        "batches": batches,
    }


def command_jobs(args: argparse.Namespace) -> dict[str, Any]:
    config = DeviceConfig.load()
    query = f"?limit={args.limit}&offset={args.offset}"
    if args.status:
        query += f"&status={args.status}"
    return ApiClient(config.controller_url, config.admin_key).request(
        "GET", "/api/jobs" + query
    )


def _bucket_client() -> ApiClient:
    config = DeviceConfig.load()
    return ApiClient(config.controller_url, config.admin_key)


def _resolve_bucket(client: ApiClient, selector: str) -> str:
    """Accept the stable Inbox ID, generated IDs, or a named collection."""
    if selector.casefold() == "inbox":
        return "inbox"
    try:
        uuid.UUID(selector)
        return selector
    except ValueError:
        pass
    for bucket in client.request("GET", "/api/buckets")["items"]:
        if bucket["name"].casefold() == selector.strip().casefold():
            return bucket["id"]
    raise CliError("Bucket was not found; run `ateable bucket list` to see collections")


def command_bucket_list(args: argparse.Namespace) -> dict[str, Any]:
    return _bucket_client().request("GET", "/api/buckets")


def command_bucket_create(args: argparse.Namespace) -> dict[str, Any]:
    return _bucket_client().request(
        "POST",
        "/api/buckets",
        {
            "name": args.name,
            "description": args.description,
        },
    )


def command_bucket_items(args: argparse.Namespace) -> dict[str, Any]:
    client = _bucket_client()
    bucket_id = _resolve_bucket(client, args.bucket)
    query = {"limit": args.limit, "offset": args.offset}
    if args.query is not None:
        query["q"] = args.query
    if args.status is not None:
        query["status"] = args.status
    return client.request(
        "GET", f"/api/buckets/{quote(bucket_id, safe='')}/items?{urlencode(query)}"
    )


def command_bucket_add(args: argparse.Namespace) -> dict[str, Any]:
    # Validate locally, but never ask workers or fetch metadata when capturing.
    urls = _read_capture_urls(args.urls, args.file)
    client = _bucket_client()
    bucket_id = _resolve_bucket(client, args.bucket)
    return client.request(
        "POST",
        f"/api/buckets/{quote(bucket_id, safe='')}/items",
        {
            "items": [
                {"url": url, "title": args.title, "source_page": args.source_page}
                for url in urls
            ],
            "origin": "cli",
            "idempotency_key": args.idempotency_key or str(uuid.uuid4()),
        },
    )


def command_bucket_dispatch(args: argparse.Namespace) -> dict[str, Any]:
    client = _bucket_client()
    bucket_id = _resolve_bucket(client, args.bucket)
    path = f"/api/buckets/{quote(bucket_id, safe='')}/"
    if args.retry_run:
        return client.request(
            "POST", path + f"dispatch-runs/{quote(args.retry_run, safe='')}/retry", {}
        )
    return client.request(
        "POST",
        path + "dispatch",
        {
            "format": args.format,
            "target_worker_id": args.device,
            "output_subdir": args.output_subdir,
            "limit": args.limit,
            "item_ids": args.item_ids,
            "retry_existing": args.retry_existing,
            "max_attempts": args.max_attempts,
            "idempotency_key": args.idempotency_key or str(uuid.uuid4()),
        },
    )


def command_bucket_export(args: argparse.Namespace) -> dict[str, Any] | str:
    client = _bucket_client()
    bucket_id = _resolve_bucket(client, args.bucket)
    # Fetch JSON through the existing authenticated client; text is generated
    # from the same durable source metadata rather than downloading any URL.
    exported = client.request(
        "GET", f"/api/buckets/{quote(bucket_id, safe='')}/export?format=json"
    )
    if args.format == "text":
        content = "".join(item["source_url"] + "\n" for item in exported["items"])
    else:
        content = json.dumps(exported, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        destination = Path(args.output).expanduser()
        destination.write_text(content, encoding="utf-8")
        return {
            "output": str(destination),
            "format": args.format,
            "items": len(exported["items"]),
        }
    return exported if args.format == "json" else content.rstrip("\n")


def command_job_action(args: argparse.Namespace) -> dict[str, Any]:
    config = DeviceConfig.load()
    return ApiClient(config.controller_url, config.admin_key).request(
        "POST", f"/api/jobs/{args.job_id}/{args.action}", {}
    )


def command_network_status(args: argparse.Namespace) -> dict[str, Any]:
    config = DeviceConfig.load()
    mode = args.mode or config.network_mode
    proxy = args.proxy
    if proxy is None:
        proxy = config.tor_proxy if mode == "tor" else config.proxy
    if config.admin_key:
        return ApiClient(config.controller_url, config.admin_key).request(
            "POST", "/api/network/probe", {"mode": mode, "proxy": proxy}
        )
    return probe_network(
        mode,
        proxy if mode == "proxy" else None,
        proxy or config.tor_proxy,
    ).to_dict()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ateable",
        description="Pair devices and manage reliable bulk media downloads.",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the controller")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--controller-url")
    serve.set_defaults(handler=command_serve)

    worker = commands.add_parser("worker", help="run a paired download worker")
    worker.add_argument("--pair-key")
    worker.add_argument("--coordinator")
    worker.add_argument("--name")
    worker.add_argument("--output-dir")
    worker.add_argument("--concurrency", type=int, default=1)
    worker.add_argument(
        "--network", "--network-mode", dest="network_mode", choices=NETWORK_MODES
    )
    worker.add_argument("--proxy")
    worker.add_argument("--tor-proxy")
    worker.add_argument("--cookies-file")
    worker.add_argument("--poll-interval", type=float, default=1)
    worker.set_defaults(handler=command_worker)

    setup = commands.add_parser("setup", help="configure this device")
    setup.add_argument("--role", choices=ROLES)
    setup.add_argument("--name")
    setup.add_argument("--controller")
    setup.add_argument("--output-dir")
    setup.add_argument(
        "--network", "--network-mode", dest="network_mode", choices=NETWORK_MODES
    )
    setup.add_argument("--proxy")
    setup.add_argument("--tor-proxy")
    setup.add_argument(
        "--automation",
        action="store_true",
        help="omit host paths, device identifiers, and secret-adjacent values",
    )
    setup.set_defaults(handler=command_setup)

    doctor = commands.add_parser(
        "doctor", help="print privacy-safe readiness for automation"
    )
    doctor.set_defaults(handler=command_doctor)

    device = commands.add_parser("device", help="inspect and pair devices")
    device_commands = device.add_subparsers(dest="device_command", required=True)
    info = device_commands.add_parser("info", help="show device readiness")
    info.add_argument("--show-secret", action="store_true")
    info.set_defaults(handler=command_device_info)
    key = device_commands.add_parser("key", help="create a single-use pair key")
    key.add_argument("--expires", type=int, default=600)
    key.add_argument("--regenerate", action="store_true")
    key.set_defaults(handler=command_device_key)
    pair = device_commands.add_parser("pair", help="pair with a controller")
    pair.add_argument("pair_key")
    pair.add_argument("--name")
    pair.add_argument("--concurrency", type=int, default=1)
    pair.set_defaults(handler=command_device_pair)
    listing = device_commands.add_parser("list", help="list paired devices")
    listing.set_defaults(handler=command_device_list)
    ssh = device_commands.add_parser(
        "ssh",
        help="install/pair a Linux worker over SSH; keep running to maintain its tunnel",
    )
    ssh.add_argument("host", help="SSH host or configured alias")
    ssh.add_argument("--user")
    ssh.add_argument("--port", type=int, help="override the port from SSH config")
    ssh.add_argument("--identity-file")
    ssh.add_argument("--remote-dir", help="remote worker installation path")
    ssh.add_argument("--output-dir", help="download directory on the remote worker")
    ssh.add_argument("--concurrency", type=int)
    ssh.add_argument("--network", dest="network_mode", choices=NETWORK_MODES)
    ssh.add_argument("--proxy")
    ssh.add_argument("--tunnel-port", type=int)
    ssh.add_argument(
        "--workspace-id", help="enroll the paired node in this admin workspace"
    )
    ssh.add_argument("--s3-endpoint", help="nonsecret S3 API endpoint on the worker")
    ssh.add_argument(
        "--s3-profile", help="AWS profile configured privately on the worker"
    )
    ssh.add_argument(
        "--connect-only",
        action="store_true",
        help="reconnect an installed worker using its saved credentials",
    )
    ssh.set_defaults(handler=command_device_ssh)

    add = commands.add_parser("add", help="add up to 5,000 URLs")
    add.add_argument("urls", nargs="*")
    add.add_argument("--file")
    add.add_argument("--format", choices=FORMATS, default="original")
    add.add_argument(
        "--device",
        default="auto",
        help="auto or an online remote device ID; the controller never downloads",
    )
    add.set_defaults(handler=command_add)

    bucket = commands.add_parser(
        "bucket", help="collect links and dispatch them to remote workers"
    )
    bucket_commands = bucket.add_subparsers(dest="bucket_command", required=True)
    bucket_list = bucket_commands.add_parser(
        "list", help="list named collections and counts"
    )
    bucket_list.set_defaults(handler=command_bucket_list)
    bucket_create = bucket_commands.add_parser(
        "create", help="create a named collection"
    )
    bucket_create.add_argument("name")
    bucket_create.add_argument("--description", default="")
    bucket_create.set_defaults(handler=command_bucket_create)
    bucket_items = bucket_commands.add_parser(
        "items", help="list captured links and job history"
    )
    bucket_items.add_argument(
        "bucket", nargs="?", default="inbox", help="bucket name or ID"
    )
    bucket_items.add_argument("--query", "--q", dest="query")
    bucket_items.add_argument("--status")
    bucket_items.add_argument("--limit", type=int, default=100)
    bucket_items.add_argument("--offset", type=int, default=0)
    bucket_items.set_defaults(handler=command_bucket_items)
    bucket_add = bucket_commands.add_parser(
        "add", help="capture links without starting downloads"
    )
    bucket_add.add_argument(
        "bucket", help="bucket name or ID; use inbox for the default collection"
    )
    bucket_add.add_argument("urls", nargs="*")
    bucket_add.add_argument("--file", help="URL text file or - for stdin")
    bucket_add.add_argument("--title")
    bucket_add.add_argument("--source-page")
    bucket_add.add_argument("--idempotency-key")
    bucket_add.set_defaults(handler=command_bucket_add)
    bucket_dispatch = bucket_commands.add_parser(
        "dispatch", help="queue pending links on paired workers"
    )
    bucket_dispatch.add_argument(
        "bucket", nargs="?", default="inbox", help="bucket name or ID"
    )
    bucket_dispatch.add_argument("--format", choices=FORMATS, default="original")
    bucket_dispatch.add_argument(
        "--device", default="auto", help="auto or a remote worker ID"
    )
    bucket_dispatch.add_argument("--output-subdir")
    bucket_dispatch.add_argument("--limit", type=int, default=5000)
    bucket_dispatch.add_argument("--item", dest="item_ids", action="append")
    bucket_dispatch.add_argument("--idempotency-key")
    bucket_dispatch.add_argument(
        "--retry-existing",
        action="store_true",
        help="retry failed/cancelled jobs with the same IDs",
    )
    bucket_dispatch.add_argument(
        "--retry-run", help="resume exactly this reserved run using its stored options"
    )
    bucket_dispatch.add_argument("--max-attempts", type=int, default=3)
    bucket_dispatch.set_defaults(handler=command_bucket_dispatch)
    bucket_export = bucket_commands.add_parser(
        "export", help="export captured URLs or full metadata"
    )
    bucket_export.add_argument(
        "bucket", nargs="?", default="inbox", help="bucket name or ID"
    )
    bucket_export.add_argument("--format", choices=("text", "json"), default="text")
    bucket_export.add_argument(
        "--output", help="save export to this file instead of printing it"
    )
    bucket_export.set_defaults(handler=command_bucket_export)

    from .workspace_cli import register_workspace_commands

    register_workspace_commands(commands)

    jobs = commands.add_parser("jobs", help="list jobs")
    jobs.add_argument("--status")
    jobs.add_argument("--limit", type=int, default=100)
    jobs.add_argument("--offset", type=int, default=0)
    jobs.set_defaults(handler=command_jobs)

    job = commands.add_parser("job", help="control one job")
    job_commands = job.add_subparsers(dest="action", required=True)
    for action in ("pause", "resume", "cancel", "retry"):
        action_parser = job_commands.add_parser(action)
        action_parser.add_argument("job_id")
        action_parser.set_defaults(handler=command_job_action)

    network = commands.add_parser("network", help="inspect network policy")
    network_commands = network.add_subparsers(dest="network_command", required=True)
    network_status = network_commands.add_parser("status")
    network_status.add_argument("--mode", choices=NETWORK_MODES)
    network_status.add_argument("--proxy")
    network_status.set_defaults(handler=command_network_status)
    return parser


def normalize_args(argv: list[str]) -> list[str]:
    values = list(argv)
    wants_json = "--json" in values
    if wants_json:
        values.remove("--json")
    if values and values[0].lower() == "ateable":
        values.pop(0)
    if not values:
        return ["--json"] if wants_json else values
    aliases = {
        "deviceinfo": ["device", "info"],
        "device-info": ["device", "info"],
        "devicekey": ["device", "key"],
        "devicelist": ["device", "list"],
        "networkstatus": ["network", "status"],
    }
    compact = values[0].lower()
    if compact in aliases:
        values[:1] = aliases[compact]
    else:
        values[0] = compact
        if len(values) > 1 and values[0] in {"device", "job", "network", "bucket"}:
            values[1] = values[1].lower()
    return (["--json"] if wants_json else []) + values


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.getenv("ATEABLE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(normalize_args(list(argv or sys.argv[1:])))
    try:
        result = args.handler(args)
        if result is not None:
            _print(result, as_json=args.json)
        return 0
    except (CliError, ValueError, OSError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"ateable: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
