"""Sync business logic for the three MCP tools."""
from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from cgate.db.types import BatchId

if TYPE_CHECKING:
    from cgate.connections.store import ConnectionsRepo
    from cgate.db.batches import BatchesRepo
    from cgate.db.commands import CommandsRepo
    from cgate.db.types import Connection


class ProposeCommandResult(TypedDict):
    """JSON-compatible result returned after queueing a command."""

    batch_id: str
    command_id: str
    position: int
    status: str


class ConnectionResult(TypedDict):
    """JSON-compatible saved connection details."""

    alias: str
    hostname: str
    server_type: str
    detection_ssh: bool
    detection_winrm: bool


class CommandStatusResult(TypedDict):
    """JSON-compatible status and audit details for one command."""

    id: str
    position: int
    server_alias: str
    server_type: str
    command: str
    status: str
    result: str | None
    approved_by: str | None
    created_at: str
    resolved_at: str | None


class BatchStatusResult(TypedDict):
    """JSON-compatible batch metadata and ordered command statuses."""

    batch_id: str
    title: str
    description: str | None
    created_at: str
    resolved_at: str | None
    requested_by_agent: str | None
    commands: list[CommandStatusResult]


class ToolError(Exception):
    """Domain error converted into MCP error content by the async boundary."""

    code: str
    message: str

    def __init__(self, code: str, message: str) -> None:
        """Create an error with a stable machine code and human-readable message."""
        super().__init__(message)
        self.code = code
        self.message = message


def _require_batch_title(batch_title: str | None) -> str:
    if not batch_title or not batch_title.strip():
        code = "missing_batch_title"
        message = "batch_title is required"
        raise ToolError(code, message)
    return batch_title.strip()


def _require_known_alias(connections_repo: ConnectionsRepo, alias: str) -> Connection:
    connection = connections_repo.get(alias)
    if connection is None:
        code = "unknown_alias"
        message = f"unknown alias '{alias}'"
        raise ToolError(code, message)
    return connection


def propose_command(  # noqa: PLR0913 - boundary mirrors the specified MCP tool arguments.
    *,
    batches_repo: BatchesRepo,
    commands_repo: CommandsRepo,
    connections_repo: ConnectionsRepo,
    server_alias: str,
    command: str,
    batch_title: str | None,
    batch_description: str | None = None,
    batch_id: str | None = None,
    reason: str | None = None,
    requested_by_agent: str = "mcp",
) -> ProposeCommandResult:
    """Register a proposed command in the queue without ever executing it."""
    title = _require_batch_title(batch_title)
    connection = _require_known_alias(connections_repo, server_alias)
    _ = reason  # Phase 1 accepts the justification but has no persistence column for it.

    if batch_id is None:
        batch = batches_repo.create(
            title=title,
            description=batch_description,
            requested_by_agent=requested_by_agent,
        )
    else:
        batch = batches_repo.get(BatchId(batch_id))
        if batch is None:
            # An unknown optional ID is a stale hint, so create a fresh UUID-backed batch.
            batch = batches_repo.create(
                title=title,
                description=batch_description,
                requested_by_agent=requested_by_agent,
            )

    queued = commands_repo.add(
        batch_id=batch.id,
        server_alias=connection.alias,
        server_type=connection.server_type,
        command=command,
    )
    return {
        "batch_id": batch.id,
        "command_id": queued.id,
        "position": queued.position,
        "status": queued.status.value,
    }


def list_connections(*, connections_repo: ConnectionsRepo) -> list[ConnectionResult]:
    """Return saved connections and their server dialect metadata."""
    return [
        {
            "alias": connection.alias,
            "hostname": connection.hostname,
            "server_type": connection.server_type.value,
            "detection_ssh": connection.detection_ssh,
            "detection_winrm": connection.detection_winrm,
        }
        for connection in connections_repo.list_all()
    ]


def check_status(
    *,
    batches_repo: BatchesRepo,
    commands_repo: CommandsRepo,
    batch_id: str,
) -> BatchStatusResult:
    """Return batch metadata and ordered per-command status and audit fields."""
    typed_batch_id = BatchId(batch_id)
    batch = batches_repo.get(typed_batch_id)
    if batch is None:
        code = "unknown_batch"
        message = f"unknown batch '{batch_id}'"
        raise ToolError(code, message)

    return {
        "batch_id": batch.id,
        "title": batch.title,
        "description": batch.description,
        "created_at": batch.created_at.isoformat(),
        "resolved_at": batch.resolved_at.isoformat() if batch.resolved_at else None,
        "requested_by_agent": batch.requested_by_agent,
        "commands": [
            {
                "id": command.id,
                "position": command.position,
                "server_alias": command.server_alias,
                "server_type": command.server_type.value,
                "command": command.command,
                "status": command.status.value,
                "result": command.result,
                "approved_by": command.approved_by,
                "created_at": command.created_at.isoformat(),
                "resolved_at": command.resolved_at.isoformat() if command.resolved_at else None,
            }
            for command in commands_repo.list_for_batch(typed_batch_id)
        ],
    }
