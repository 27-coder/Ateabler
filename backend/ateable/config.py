"""Coordinator environment configuration."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from platformdirs import user_data_path


def default_state_dir() -> Path:
    """Keep installed controller metadata outside its read-only application bundle."""
    configured = os.getenv("ATEABLE_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(user_data_path("Ateable", appauthor=False)) / "data"
    return Path("./data").resolve()


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    value = int(os.getenv(name, str(default)))
    return max(minimum, value)


@dataclass(frozen=True, slots=True)
class AppConfig:
    database_path: Path
    output_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    admin_key: str | None = None
    controller_url: str = "http://127.0.0.1:8765"
    pair_ttl_seconds: int = 600
    embedded_worker: bool = False
    local_concurrency: int = 1
    worker_timeout_seconds: int = 45
    lease_seconds: int = 120
    network_mode: str = "direct"
    proxy: str | None = None
    tor_proxy: str = "socks5h://127.0.0.1:9050"

    @classmethod
    def from_env(cls) -> AppConfig:
        state_dir = default_state_dir()
        host = os.getenv("ATEABLE_HOST", "127.0.0.1")
        port = _int_env("ATEABLE_PORT", 8765)
        public_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        network_mode = os.getenv("ATEABLE_NETWORK_MODE", "tor").strip().lower()
        if network_mode not in {"direct", "proxy", "tor"}:
            raise ValueError("ATEABLE_NETWORK_MODE must be direct, proxy, or tor")
        return cls(
            database_path=Path(
                os.getenv("ATEABLE_DATABASE_PATH", str(state_dir / "ateable.db"))
            )
            .expanduser()
            .resolve(),
            output_dir=Path(os.getenv("ATEABLE_OUTPUT_DIR", "./downloads"))
            .expanduser()
            .resolve(),
            host=host,
            port=port,
            admin_key=os.getenv("ATEABLE_ADMIN_KEY") or None,
            controller_url=os.getenv(
                "ATEABLE_CONTROLLER_URL", f"http://{public_host}:{port}"
            ).rstrip("/"),
            pair_ttl_seconds=_int_env("ATEABLE_PAIR_TTL_SECONDS", 600, minimum=30),
            embedded_worker=False,
            local_concurrency=_int_env("ATEABLE_LOCAL_CONCURRENCY", 1),
            worker_timeout_seconds=_int_env("ATEABLE_WORKER_TIMEOUT_SECONDS", 45),
            lease_seconds=_int_env("ATEABLE_LEASE_SECONDS", 120),
            network_mode=network_mode,
            proxy=os.getenv("ATEABLE_PROXY") or None,
            tor_proxy=os.getenv("ATEABLE_TOR_PROXY", "socks5h://127.0.0.1:9050"),
        )

    def with_overrides(self, **values: object) -> AppConfig:
        return replace(self, **values)
