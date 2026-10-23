"""Ateable Node: a scoped participant client and headless command line.

This module has no Qt, controller, downloader, or payload-network imports. It
uses only worker or workspace-agent credentials and participant API routes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_SESSION_BYTES = 64 * 1024


class ParticipantError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


def normalize_controller_url(value: str) -> str:
    try:
        parsed = urlparse(value.strip())
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
        _ = parsed.port
    except (ValueError, AttributeError) as exc:
        raise ParticipantError("Controller URL is invalid") from exc
    if (
        not valid
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(character.isspace() or ord(character) < 32 for character in value)
        or "\\" in parsed.netloc
    ):
        raise ParticipantError(
            "Controller URL must be HTTP(S) without credentials, a query or a fragment"
        )
    return urlunparse(parsed._replace(path=parsed.path.rstrip("/")))


def _identifier(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 160
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ParticipantError(f"{label} is invalid")
    return value


def _token(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 8192
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ParticipantError("Participant token is invalid")
    return value


@dataclass(frozen=True, slots=True)
class ParticipantIdentity:
    controller_url: str
    actor_id: str
    actor_type: str
    token: str = field(repr=False)
    workspace_id: str | None = None

    def __post_init__(self):
        object.__setattr__(
            self, "controller_url", normalize_controller_url(self.controller_url)
        )
        _identifier(self.actor_id, "Participant identity")
        _token(self.token)
        if self.actor_type not in {"worker", "agent"}:
            raise ParticipantError(
                "Participant identity must be a worker or a workspace agent"
            )
        if self.workspace_id is not None:
            _identifier(self.workspace_id, "Workspace identity")

    def headers(self) -> dict[str, str]:
        header = (
            "X-Ateable-Worker-ID"
            if self.actor_type == "worker"
            else "X-Ateable-Agent-ID"
        )
        return {"Authorization": f"Bearer {self.token}", header: self.actor_id}

    def safe_info(self) -> dict[str, Any]:
        return {
            "id": self.actor_id,
            "type": self.actor_type,
            "controller_url": self.controller_url,
            "workspace_id": self.workspace_id,
        }


def default_session_file() -> Path:
    configured = os.getenv("ATEABLE_PARTICIPANT_SESSION")
    if configured:
        return Path(configured).expanduser()
    return _native_config_dir("ateable-node") / "session.json"


def _native_config_dir(app_name: str) -> Path:
    """Native config locations without importing controller dependencies."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    if sys.platform == "win32":
        return (
            Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
            / app_name
        )
    return Path(os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / app_name


def default_credentials_file() -> Path:
    configured = os.getenv("ATEABLE_WORKER_CREDENTIALS")
    return (
        Path(configured).expanduser()
        if configured
        else (
            _native_config_dir("Ateable") / "worker-credentials.json"
            if getattr(sys, "frozen", False)
            else Path(sys.prefix).parent / "worker-credentials.json"
        )
    )


def _read_session(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink():
            raise ParticipantError(
                "Participant credential files must not be symbolic links"
            )
        with path.open("rb") as stream:
            data = stream.read(MAX_SESSION_BYTES + 1)
        if len(data) > MAX_SESSION_BYTES:
            raise ParticipantError("Participant credential file is too large")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ParticipantError("Participant credential file is invalid")
        return value
    except ParticipantError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise ParticipantError(
            "Participant credentials could not be read; select a worker credential or agent session file"
        ) from None


def load_identity(
    *,
    credentials_file: Path | str | None = None,
    session_file: Path | str | None = None,
    controller_url: str | None = None,
) -> ParticipantIdentity:
    if credentials_file is not None and session_file is not None:
        raise ParticipantError("Choose either worker credentials or an agent session")
    if session_file is not None:
        value = _read_session(Path(session_file).expanduser())
        kind = "agent"
    elif credentials_file is not None:
        value = _read_session(Path(credentials_file).expanduser())
        kind = "worker"
    else:
        worker_path = default_credentials_file()
        session_path = default_session_file()
        if worker_path.is_file():
            value, kind = _read_session(worker_path), "worker"
        elif session_path.is_file():
            value, kind = _read_session(session_path), "agent"
        else:
            raise ParticipantError(
                "No participant credentials found; use --credentials-file or login with an agent token"
            )
    if kind == "agent":
        return ParticipantIdentity(
            controller_url or value.get("controller_url", ""),
            _identifier(value.get("agent_id"), "Agent identity"),
            kind,
            _token(value.get("token")),
            _identifier(value.get("workspace_id"), "Workspace identity"),
        )
    return ParticipantIdentity(
        controller_url or value.get("coordinator_url", ""),
        _identifier(value.get("worker_id"), "Worker identity"),
        kind,
        _token(value.get("token")),
    )


def save_session(path: Path | str, session: dict[str, Any]) -> None:
    """Validate and atomically save an agent session, never accepting admin keys."""
    if not isinstance(session, dict) or set(session) != {
        "controller_url",
        "agent_id",
        "token",
        "workspace_id",
    }:
        raise ParticipantError(
            "Agent session must contain only controller_url, agent_id, token and workspace_id"
        )
    identity = ParticipantIdentity(
        session["controller_url"],
        _identifier(session["agent_id"], "Agent identity"),
        "agent",
        _token(session["token"]),
        _identifier(session["workspace_id"], "Workspace identity"),
    )
    target = Path(path).expanduser()
    temporary = None
    try:
        if target.is_symlink():
            raise ParticipantError(
                "Agent session destination must not be a symbolic link"
            )
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(prefix=".node-session-", dir=target.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600) if hasattr(os, "fchmod") else None
            json.dump(
                {
                    "controller_url": identity.controller_url,
                    "agent_id": identity.actor_id,
                    "token": identity.token,
                    "workspace_id": identity.workspace_id,
                },
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        temporary = None
    except ParticipantError:
        raise
    except OSError:
        raise ParticipantError("Agent session could not be saved") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ParticipantClient:
    def __init__(
        self, identity: ParticipantIdentity, *, opener: Any = None, timeout: float = 20
    ):
        self.identity = identity
        self.timeout = timeout
        self._opener = opener or build_opener(ProxyHandler({}), _NoRedirect())

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not path.startswith("/api/participant/"):
            raise ParticipantError("Ateable Node only supports participant operations")
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            self.identity.controller_url + path,
            data=encoded,
            method=method,
            headers={
                **self.identity.headers(),
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if encoded is not None else {}),
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.geturl() != request.full_url:
                    raise ParticipantError("Controller redirects are blocked")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ParticipantError(
                        "Controller response exceeded the participant size limit"
                    )
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ParticipantError(
                        "Controller returned an invalid participant response"
                    )
                return value
        except ParticipantError:
            raise
        except HTTPError as exc:
            exc.close()
            messages = {
                401: "Participant credentials were rejected",
                403: "Participant access was denied",
                404: "Workspace or participant resource was not found",
                409: "Participant request conflicts with current workspace state",
                422: "Participant request was invalid",
            }
            message = (
                "Controller redirects are blocked"
                if 300 <= exc.code < 400
                else messages.get(exc.code, "Controller request failed")
            )
            raise ParticipantError(message, status=exc.code) from None
        except (URLError, OSError, TimeoutError):
            raise ParticipantError("Controller is unavailable") from None
        except (ValueError, UnicodeError, RecursionError):
            raise ParticipantError(
                "Controller returned an invalid participant response"
            ) from None

    @staticmethod
    def _workspace(workspace_id: str) -> str:
        return "/api/participant/workspaces/" + quote(
            _identifier(workspace_id, "Workspace identity"), safe=""
        )

    def workspaces(self) -> dict[str, Any]:
        return self._request("GET", "/api/participant/workspaces")

    def members(self, workspace_id: str) -> dict[str, Any]:
        return self._request("GET", self._workspace(workspace_id) + "/members")

    def files(
        self, workspace_id: str, *, q: str = "", limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            self._workspace(workspace_id)
            + "/files?"
            + urlencode({"q": q, "limit": limit, "offset": offset}),
        )

    def jobs(
        self, workspace_id: str, *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            self._workspace(workspace_id)
            + "/jobs?"
            + urlencode({"limit": limit, "offset": offset}),
        )

    def messages(
        self, workspace_id: str, *, after_seq: int = 0, limit: int = 200
    ) -> dict[str, Any]:
        result = self._request(
            "GET",
            self._workspace(workspace_id)
            + "/messages?"
            + urlencode({"after_seq": after_seq, "limit": limit}),
        )
        cursor = result.get("next_after_seq")
        items = result.get("items")
        if (
            not isinstance(cursor, int)
            or isinstance(cursor, bool)
            or cursor < after_seq
            or not isinstance(items, list)
        ):
            raise ParticipantError("Controller returned an invalid message cursor")
        return result

    def send_message(
        self,
        workspace_id: str,
        text: str,
        *,
        recipient_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not text.strip() or len(text) > 10000:
            raise ParticipantError(
                "Message must contain between 1 and 10000 characters"
            )
        return self._request(
            "POST",
            self._workspace(workspace_id) + "/messages",
            {
                "text": text,
                "recipient_id": recipient_id,
                "idempotency_key": idempotency_key or str(uuid4()),
            },
        )

    def downloads(
        self,
        workspace_id: str,
        urls: list[str],
        *,
        target_worker_id: str = "auto",
        output_subdir: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not urls or len(urls) > 5000:
            raise ParticipantError(
                "Download request must contain between 1 and 5000 URLs"
            )
        return self._request(
            "POST",
            self._workspace(workspace_id) + "/downloads",
            {
                "urls": urls,
                "format": "original",
                "target_worker_id": target_worker_id,
                "output_subdir": output_subdir,
                "idempotency_key": idempotency_key or str(uuid4()),
            },
        )


def _emit(value: Any, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False))
        return
    if isinstance(value, dict) and "items" in value:
        for item in value["items"]:
            print(json.dumps(item, ensure_ascii=False))
        if "next_after_seq" in value:
            print(f"Next cursor: {value['next_after_seq']}")
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


class _SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        # Argparse's default error includes unknown argument values; those may
        # be accidental tokens or signed source URLs supplied on the command.
        self.print_usage(sys.stderr)
        self.exit(2, "ateable-node: Invalid command or arguments; use --help\n")


def parser() -> argparse.ArgumentParser:
    result = _SafeParser(
        prog="ateable-node",
        description="Use assigned workspaces, remote downloads, VM file catalogs and speaking as a participant.",
    )
    result.add_argument("--credentials-file", type=Path)
    result.add_argument("--session-file", type=Path)
    result.add_argument("--controller-url")
    result.add_argument("--json", action="store_true", dest="json_output")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "identity", help="Show this participant's identity without its token"
    )
    commands.add_parser("workspaces", help="List assigned workspaces")
    login = commands.add_parser(
        "login", help="Save an agent session; reads its token from standard input"
    )
    login.add_argument("--agent-id", required=True)
    login.add_argument("--workspace-id", required=True)
    for name in ("members", "files", "jobs", "download"):
        command = commands.add_parser(name)
        command.add_argument("workspace_id", nargs="?")
        if name in {"files", "jobs"}:
            command.add_argument(
                "--limit",
                type=int,
                default=100,
                choices=range(1, 1001),
                metavar="1..1000",
            )
            command.add_argument("--offset", type=int, default=0)
        if name == "files":
            command.add_argument("--query", default="")
        if name == "download":
            command.add_argument("--url", action="append", required=True, dest="urls")
            command.add_argument("--target-worker-id", default="auto")
            command.add_argument("--output-subdir")
            command.add_argument("--idempotency-key")
    message = commands.add_parser(
        "message", help="Send, read or watch workspace speaking"
    )
    actions = message.add_subparsers(dest="message_action", required=True)
    for name in ("send", "read", "watch"):
        command = actions.add_parser(name)
        command.add_argument("workspace_id", nargs="?")
        if name == "send":
            command.add_argument("--text")
            command.add_argument("--recipient-id")
            command.add_argument("--idempotency-key")
        else:
            command.add_argument("--after-seq", type=int, default=0)
            command.add_argument(
                "--limit",
                type=int,
                default=200,
                choices=range(1, 1001),
                metavar="1..1000",
            )
        if name == "watch":
            command.add_argument("--interval", type=float, default=2.0)
            command.add_argument(
                "--count",
                type=int,
                default=0,
                help="Stop after this many polls; 0 watches until interrupted",
            )
    commands.add_parser("ui", help="Open the optional native participant viewer")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "login":
            if not args.controller_url:
                raise ParticipantError("Agent login requires --controller-url")
            token = sys.stdin.read(8193).strip()
            save_session(
                args.session_file or default_session_file(),
                {
                    "controller_url": args.controller_url,
                    "agent_id": args.agent_id,
                    "workspace_id": args.workspace_id,
                    "token": token,
                },
            )
            _emit(
                {
                    "saved": True,
                    "agent_id": args.agent_id,
                    "workspace_id": args.workspace_id,
                },
                args.json_output,
            )
            return 0
        identity = load_identity(
            credentials_file=args.credentials_file,
            session_file=args.session_file,
            controller_url=args.controller_url,
        )
        if args.command == "identity":
            _emit(identity.safe_info(), args.json_output)
            return 0
        client = ParticipantClient(identity)
        if args.command == "ui":
            from .participant_ui import launch

            return launch(client)
        if args.command == "workspaces":
            _emit(client.workspaces(), args.json_output)
            return 0
        workspace_id = args.workspace_id or identity.workspace_id
        if not workspace_id:
            available = client.workspaces().get("items", [])
            if len(available) != 1:
                raise ParticipantError(
                    "Choose a workspace ID from the workspaces command"
                )
            workspace_id = available[0]["id"]
        if args.command == "members":
            value = client.members(workspace_id)
        elif args.command == "files":
            value = client.files(
                workspace_id, q=args.query, limit=args.limit, offset=max(0, args.offset)
            )
        elif args.command == "jobs":
            value = client.jobs(
                workspace_id, limit=args.limit, offset=max(0, args.offset)
            )
        elif args.command == "download":
            value = client.downloads(
                workspace_id,
                args.urls,
                target_worker_id=args.target_worker_id,
                output_subdir=args.output_subdir,
                idempotency_key=args.idempotency_key,
            )
        elif args.command == "message" and args.message_action == "send":
            value = client.send_message(
                workspace_id,
                args.text if args.text is not None else sys.stdin.read(10001),
                recipient_id=args.recipient_id,
                idempotency_key=args.idempotency_key,
            )
        elif args.command == "message" and args.message_action == "read":
            value = client.messages(
                workspace_id, after_seq=max(0, args.after_seq), limit=args.limit
            )
        else:
            if not 0.1 <= args.interval <= 60 or args.count < 0 or args.after_seq < 0:
                raise ParticipantError(
                    "Watch interval must be 0.1..60 seconds, count and cursor must be nonnegative"
                )
            cursor = args.after_seq
            polls = 0
            while True:
                value = client.messages(
                    workspace_id, after_seq=cursor, limit=args.limit
                )
                cursor = value["next_after_seq"]
                if value.get("items") or args.json_output:
                    _emit(value, args.json_output)
                polls += 1
                if args.count and polls >= args.count:
                    break
                if not value.get("has_more"):
                    time.sleep(args.interval)
            return 0
        _emit(value, args.json_output)
        return 0
    except ParticipantError as exc:
        if args.json_output:
            print(
                json.dumps({"error": str(exc), "status": exc.status}), file=sys.stderr
            )
        else:
            print(f"Ateable Node: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
