"""Cross-platform, permission-restricted Ateable device configuration."""

from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

from platformdirs import user_config_path

from .network import DEFAULT_TOR_PROXY, NETWORK_MODES

ROLES = ("controller", "worker", "both")


def default_config_dir() -> Path:
    override = os.getenv("ATEABLE_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(user_config_path("Ateable", appauthor=False))


def default_config_path() -> Path:
    return default_config_dir() / "config.json"


def default_output_dir() -> Path:
    return (Path.home() / "Downloads" / "Ateable").resolve()


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    device_id: str
    name: str
    role: str
    controller_url: str
    output_dir: str
    network_mode: str = "tor"
    proxy: str | None = None
    tor_proxy: str = DEFAULT_TOR_PROXY
    admin_key: str | None = None
    worker_id: str | None = None
    worker_token: str | None = None
    last_pair_key: str | None = None
    schema_version: int = 1

    @classmethod
    def defaults(cls) -> DeviceConfig:
        return cls(
            device_id=str(uuid.uuid4()),
            name=socket.gethostname(),
            role="controller",
            controller_url=os.getenv(
                "ATEABLE_CONTROLLER_URL", "http://127.0.0.1:8765"
            ).rstrip("/"),
            output_dir=str(
                Path(
                    os.getenv("ATEABLE_OUTPUT_DIR", str(default_output_dir()))
                ).expanduser().resolve()
            ),
            network_mode=os.getenv("ATEABLE_NETWORK_MODE", "tor").lower(),
            proxy=os.getenv("ATEABLE_PROXY") or None,
            tor_proxy=os.getenv("ATEABLE_TOR_PROXY", DEFAULT_TOR_PROXY),
            admin_key=os.getenv("ATEABLE_ADMIN_KEY") or None,
        )

    @classmethod
    def load(cls, path: Path | None = None) -> DeviceConfig:
        target = path or default_config_path()
        fallback = cls.defaults()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return fallback
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(f"Could not read device config {target}: {exc}") from exc
        allowed = {field.name for field in fields(cls)}
        values = {key: value for key, value in raw.items() if key in allowed}
        merged = {**asdict(fallback), **values}
        config = cls(**merged)
        config.validate()
        return config

    def validate(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"Device role must be one of {', '.join(ROLES)}")
        if self.network_mode not in NETWORK_MODES:
            raise ValueError(
                f"Network mode must be one of {', '.join(NETWORK_MODES)}"
            )
        try:
            uuid.UUID(self.device_id)
        except ValueError as exc:
            raise ValueError("Device ID is invalid") from exc
        if not self.controller_url.startswith(("http://", "https://")):
            raise ValueError("Controller URL must use HTTP or HTTPS")
        if self.network_mode == "proxy" and not self.proxy:
            raise ValueError("Proxy mode requires a proxy URL")
        if self.network_mode == "tor" and not self.tor_proxy.startswith("socks5h://"):
            raise ValueError("Tor mode requires a socks5h proxy")

    def updated(self, **values: Any) -> DeviceConfig:
        config = replace(self, **values)
        config.validate()
        return config

    def save(self, path: Path | None = None) -> Path:
        target = path or default_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(target)
        try:
            target.chmod(0o600)
        except OSError:
            pass
        return target
