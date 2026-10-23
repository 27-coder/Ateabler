"""Administrator CLI for workspace metadata, membership and Speaking.

Uses the existing controller client. It never fetches remote file contents or
captures participant credentials in printed results. Agent tokens go directly
to participant.save_session's private atomic file writer.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from uuid import UUID, uuid4

from .cli import ApiClient, CliError
from .device_config import DeviceConfig
from .participant import ParticipantError, normalize_controller_url, save_session


def _client() -> ApiClient:
    config = DeviceConfig.load()
    return ApiClient(config.controller_url, config.admin_key)


def _safe(value: Any) -> Any:
    """Keep unexpected credential fields out of both human and JSON output."""
    if isinstance(value, dict):
        return {
            key: "[redacted]"
            if key.casefold() in {"token", "token_hash", "worker_token", "admin_key"}
            else _safe(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe(item) for item in value]
    return value


def _resolve_workspace(client: ApiClient, selector: str) -> str:
    selected = selector.strip()
    if not selected:
        raise CliError("Choose a workspace name or ID")
    # IDs accepted by the current controller are UUIDs. Unknown UUIDs go to the
    # scoped route for an ordinary 404; names are resolved by the current list.
    try:
        UUID(selected)
        return selected
    except ValueError:
        pass
    entries = client.request("GET", "/api/workspaces")["items"]
    for workspace in entries:
        if workspace["id"] == selected:
            return workspace["id"]
    for workspace in entries:
        if workspace["name"].casefold() == selected.casefold():
            return workspace["id"]
    raise CliError("Workspace was not found; run `ateable workspace list`")


def _workspace(client: ApiClient, selector: str) -> str:
    return "/api/workspaces/" + quote(_resolve_workspace(client, selector), safe="")


def command_workspace_create(args: argparse.Namespace) -> dict[str, Any]:
    name = args.name.strip()
    if not name or len(name) > 120:
        raise CliError("Workspace name must contain 1–120 characters")
    return _safe(
        _client().request(
            "POST", "/api/workspaces", {"name": name, "description": args.description}
        )
    )


def command_workspace_list(_args: argparse.Namespace) -> dict[str, Any]:
    return _safe(_client().request("GET", "/api/workspaces"))


def command_workspace_info(args: argparse.Namespace) -> dict[str, Any]:
    client = _client()
    return _safe(client.request("GET", _workspace(client, args.workspace)))


def command_workspace_member(args: argparse.Namespace) -> dict[str, Any]:
    client = _client()
    path = _workspace(client, args.workspace) + "/members"
    if args.member_command == "list":
        return _safe(client.request("GET", path))
    if args.member_command == "add":
        return _safe(client.request("POST", path, {"worker_id": args.worker_id}))
    return _safe(client.request("DELETE", path + "/" + quote(args.worker_id, safe="")))


def command_workspace_agent(args: argparse.Namespace) -> dict[str, Any]:
    client = _client()
    path = _workspace(client, args.workspace) + "/agents"
    if args.agent_command == "list":
        return _safe(client.request("GET", path))
    if args.agent_command == "revoke":
        return _safe(
            client.request("DELETE", path + "/" + quote(args.agent_id, safe=""))
        )
    # Check common file mistakes before minting a one-time credential. Existing
    # session files are not replaced silently, preserving their current tokens.
    target = Path(args.session_file).expanduser()
    if target.exists() or target.is_symlink():
        raise CliError(
            "Session destination already exists; choose a new --session-file"
        )
    name = args.name.strip()
    if not name or len(name) > 120:
        raise CliError("Agent name must contain 1–120 characters")
    try:
        controller_url = normalize_controller_url(
            args.session_controller_url or client.controller_url
        )
    except ParticipantError as error:
        raise CliError(str(error)) from None
    agent = client.request("POST", path, {"name": name})
    if not isinstance(agent, dict):
        raise CliError("Controller returned an invalid agent session")
    try:
        save_session(
            target,
            {
                "controller_url": controller_url,
                "agent_id": agent["agent_id"],
                "workspace_id": agent["workspace_id"],
                "token": agent["token"],
            },
        )
    except (ParticipantError, OSError, ValueError, KeyError):
        # If local persistence fails, revoke the credential that could not be
        # delivered. Never include the token or raw response in the error.
        agent_id = agent.get("agent_id")
        if isinstance(agent_id, str) and agent_id:
            try:
                client.request("DELETE", path + "/" + quote(agent_id, safe=""))
            except (CliError, OSError, ValueError):
                raise CliError(
                    f"Agent session could not be saved; revoke agent {agent_id} in this workspace"
                ) from None
            raise CliError(
                "Agent session could not be saved; the new agent was revoked"
            ) from None
        raise CliError("Controller returned an invalid agent session") from None
    return {
        "saved": True,
        "agent_id": agent["agent_id"],
        "workspace_id": agent["workspace_id"],
        "name": agent.get("name", name),
        "session_file": str(target.resolve()),
        "controller_url": controller_url,
    }


def _pagination(args: argparse.Namespace) -> dict[str, Any]:
    if not 1 <= args.limit <= 1000 or args.offset < 0:
        raise CliError("Limit must be 1–1,000 and offset must be nonnegative")
    values: dict[str, Any] = {"limit": args.limit, "offset": args.offset}
    if args.query is not None:
        if len(args.query) > 500:
            raise CliError("Query must contain at most 500 characters")
        values["q"] = args.query
    if getattr(args, "status", None) is not None:
        values["status"] = args.status
    return values


def command_workspace_files(args: argparse.Namespace) -> dict[str, Any]:
    query = _pagination(args)
    client = _client()
    return _safe(
        client.request(
            "GET", _workspace(client, args.workspace) + "/files?" + urlencode(query)
        )
    )


def command_workspace_jobs(args: argparse.Namespace) -> dict[str, Any]:
    query = _pagination(args)
    client = _client()
    return _safe(
        client.request(
            "GET", _workspace(client, args.workspace) + "/jobs?" + urlencode(query)
        )
    )


def command_workspace_message(args: argparse.Namespace) -> dict[str, Any]:
    if args.message_command == "read":
        if args.after_seq < 0 or not 1 <= args.limit <= 1000:
            raise CliError(
                "Message cursor must be nonnegative and limit must be 1–1,000"
            )
        query: dict[str, Any] = {"after_seq": args.after_seq, "limit": args.limit}
        if args.recipient_id is not None:
            query["recipient_id"] = args.recipient_id
        client = _client()
        return _safe(
            client.request(
                "GET",
                _workspace(client, args.workspace) + "/messages?" + urlencode(query),
            )
        )
    inputs = [
        value for value in (args.text, args.named_text, args.file) if value is not None
    ]
    if len(inputs) != 1:
        raise CliError(
            "Provide one message text argument, --text, or --file (use - for stdin)"
        )
    if args.file:
        if args.file == "-":
            text = sys.stdin.read(10001)
        else:
            with Path(args.file).expanduser().open(encoding="utf-8") as source:
                text = source.read(10001)
    else:
        text = args.text if args.text is not None else args.named_text
    if text is None or not text.strip() or len(text) > 10000:
        raise CliError("Message must contain 1–10,000 characters of nonempty text")
    client = _client()
    return _safe(
        client.request(
            "POST",
            _workspace(client, args.workspace) + "/messages",
            {
                "text": text,
                "recipient_id": args.recipient_id,
                "idempotency_key": args.idempotency_key or str(uuid4()),
            },
        )
    )


def register_workspace_commands(commands: Any) -> None:
    workspace = commands.add_parser(
        "workspace", help="administer workspace membership, file logs and Speaking"
    )
    actions = workspace.add_subparsers(dest="workspace_command", required=True)
    create = actions.add_parser("create", help="create a named workspace")
    create.add_argument("name")
    create.add_argument("--description", default="")
    create.set_defaults(handler=command_workspace_create)
    listing = actions.add_parser("list", help="list all workspaces")
    listing.set_defaults(handler=command_workspace_list)
    info = actions.add_parser("info", help="show one workspace")
    info.add_argument("workspace", help="workspace name or ID")
    info.set_defaults(handler=command_workspace_info)

    member = actions.add_parser("member", help="manage enrolled download workers")
    members = member.add_subparsers(dest="member_command", required=True)
    for operation in ("add", "remove", "list"):
        parser = members.add_parser(operation)
        parser.add_argument("workspace", help="workspace name or ID")
        if operation != "list":
            parser.add_argument("worker_id")
        parser.set_defaults(handler=command_workspace_member)

    agent = actions.add_parser(
        "agent", help="create private AI sessions and revoke identities"
    )
    agents = agent.add_subparsers(dest="agent_command", required=True)
    for operation in ("create", "list", "revoke"):
        parser = agents.add_parser(operation)
        parser.add_argument("workspace", help="workspace name or ID")
        if operation == "create":
            parser.add_argument("name")
            parser.add_argument(
                "--session-file",
                required=True,
                help="new private agent session file; token is never printed",
            )
            parser.add_argument(
                "--session-controller-url",
                help="controller address the participant will use (defaults to configured address)",
            )
        elif operation == "revoke":
            parser.add_argument("agent_id")
        parser.set_defaults(handler=command_workspace_agent)

    for operation, handler in (
        ("files", command_workspace_files),
        ("jobs", command_workspace_jobs),
    ):
        parser = actions.add_parser(
            operation, help=f"list workspace {operation} metadata"
        )
        parser.add_argument("workspace", help="workspace name or ID")
        parser.add_argument("--query")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--offset", type=int, default=0)
        if operation == "jobs":
            parser.add_argument("--status")
        parser.set_defaults(handler=handler)

    message = actions.add_parser(
        "message", help="send and read Speaking messages as admin"
    )
    messages = message.add_subparsers(dest="message_command", required=True)
    send = messages.add_parser("send")
    send.add_argument("workspace", help="workspace name or ID")
    send.add_argument("text", nargs="?")
    send.add_argument("--text", dest="named_text")
    send.add_argument("--file", help="UTF-8 message file, or - for stdin")
    send.add_argument(
        "--recipient-id", help="member worker or active agent; omit to broadcast"
    )
    send.add_argument("--idempotency-key")
    send.set_defaults(handler=command_workspace_message)
    read = messages.add_parser("read")
    read.add_argument("workspace", help="workspace name or ID")
    read.add_argument("--after-seq", type=int, default=0)
    read.add_argument("--limit", type=int, default=100)
    read.add_argument("--recipient-id")
    read.set_defaults(handler=command_workspace_message)
