"""Sync business logic for the three MCP tools."""
from __future__ import annotations

import getpass
from typing import TYPE_CHECKING, NotRequired, TypedDict

from cgate.db.mode import AppModeNotSetError, Mode
from cgate.db.types import BatchId, CommandId, CommandStatus
from cgate.executor.lifecycle import execute_and_finalize
from cgate.mcp_server.auto_resolution import resolve_auto_behavior
from cgate.risk import find_risk

if TYPE_CHECKING:
    from cgate.connections.store import ConnectionsRepo
    from cgate.db.batches import BatchesRepo
    from cgate.db.commands import CommandsRepo
    from cgate.db.connection import Database
    from cgate.db.mode import AppModeRepo
    from cgate.db.server_settings import ServerSettingsRepo
    from cgate.db.types import Command, Connection
    from cgate.mcp_server.auto_resolution import BehaviorDecision


class ProposeCommandResult(TypedDict):
    """JSON-compatible result returned after queueing a command."""

    batch_id: str
    command_id: str
    position: int
    status: str
    mode: NotRequired[str | None]
    server_auto_allowed: NotRequired[bool | None]
    effective_reason: NotRequired[str | None]
    result: NotRequired[str | None]
    approved_by: NotRequired[str | None]
    reason: NotRequired[str | None]
    risk_label: NotRequired[str | None]


class ConnectionResult(TypedDict):
    """JSON-compatible saved connection details."""

    alias: str
    hostname: str
    server_type: str
    detection_ssh: bool
    detection_winrm: bool
    auto_allowed: bool


class ModeResult(TypedDict):
    """JSON-compatible global mode and per-server auto-execution opt-ins."""

    mode: str
    auto_allowed_servers: list[str]


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
    reason: str | None
    risk_label: str | None


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


def _auto_approve_by() -> str:
    """Audit identity for auto-approvals.

    Inlined replication of watch/approval.py's ``_approve_by()`` fallback
    pattern (KeyError/OSError -> constant), prefixed so auto-decisions are
    distinguishable from human ones in the audit column.
    """
    try:
        return f"auto:watch:{getpass.getuser()}"
    except (KeyError, OSError):
        return "auto:mcp"


def _execute_auto(  # noqa: PLR0913
    *,
    db: Database,
    batches_repo: BatchesRepo,
    commands_repo: CommandsRepo,
    connections_repo: ConnectionsRepo,
    queued: Command,
    decision: BehaviorDecision,
) -> ProposeCommandResult:
    """Mirror watch/approval.approve_one's transition cycle for an auto-approved command.

    The connection is guaranteed present: ``_require_known_alias`` already
    converted a missing alias into ToolError("unknown_alias") before insert,
    so approve_one's ConnectionNotFoundError path is unreachable here.

    Implementation delegates to the shared ``execute_and_finalize`` from
    ``cgate.executor.lifecycle`` -- the same CAS-claim-and-execute pattern
    the watch TUI uses, so a TUI approval and an MCP AUTO execution
    cannot race past each other to invoke the remote executor twice.
    Without this dedup, the `propose_command` flow would still be open to
    the same race issue #36 fixed for the watch path (issue #36 review
    blocker #1).
    """
    approver = _auto_approve_by()
    approved = commands_repo.update_status(
        queued.id,
        status=CommandStatus.APPROVED,
        approved_by=approver,
        expected_status=CommandStatus.PENDING,
    )
    if not approved:
        # Lost a race with another decision on this command since the insert --
        # report its current state, don't execute (mirrors approve_one).
        current = commands_repo.get(queued.id)
        return {
            "batch_id": queued.batch_id,
            "command_id": queued.id,
            "position": queued.position,
            "status": current.status.value if current is not None else queued.status.value,
            "mode": decision["mode"],
            "server_auto_allowed": decision["server_auto_allowed"],
            "effective_reason": decision["reason"],
            "result": current.result if current is not None else None,
            "approved_by": current.approved_by if current is not None else None,
            "reason": queued.reason,
            "risk_label": queued.risk_label,
        }
    updated, execution = execute_and_finalize(
        db=db,
        commands=commands_repo,
        connections=connections_repo,
        batches=batches_repo,
        command_id=CommandId(queued.id),
    )
    output = execution.stdout if execution is not None else None
    if output and execution and execution.stderr:
        output = f"{output}\n--- stderr ---\n{execution.stderr}"
    status = updated.status if updated is not None else CommandStatus.EXECUTED
    return {
        "batch_id": queued.batch_id,
        "command_id": queued.id,
        "position": queued.position,
        "status": status.value,
        "mode": decision["mode"],
        "server_auto_allowed": decision["server_auto_allowed"],
        "effective_reason": decision["reason"],
        "result": output,
        "approved_by": approver,
        "reason": queued.reason,
        "risk_label": queued.risk_label,
    }


def propose_command(  # noqa: PLR0913 - boundary mirrors the specified MCP tool arguments.
    *,
    db: Database,
    batches_repo: BatchesRepo,
    commands_repo: CommandsRepo,
    connections_repo: ConnectionsRepo,
    mode_repo: AppModeRepo,
    settings_repo: ServerSettingsRepo,
    server_alias: str,
    command: str,
    batch_title: str | None,
    batch_description: str | None = None,
    batch_id: str | None = None,
    reason: str | None = None,
    requested_by_agent: str = "mcp",
) -> ProposeCommandResult:
    """Register a proposed command in the queue.

    Executes it immediately instead when the global mode is AUTO and the
    server alias has explicitly opted in.
    """
    title = _require_batch_title(batch_title)
    connection = _require_known_alias(connections_repo, server_alias)

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

    risk_label = find_risk(command, connection.server_type)
    queued = commands_repo.add(
        batch_id=batch.id,
        server_alias=connection.alias,
        server_type=connection.server_type,
        command=command,
        reason=reason,
        risk_label=risk_label,
    )
    decision = resolve_auto_behavior(
        mode_repo=mode_repo,
        settings_repo=settings_repo,
        server_alias=connection.alias,
        risk_label=risk_label,
    )
    if decision["action"] == "execute":
        return _execute_auto(
            db=db,
            batches_repo=batches_repo,
            commands_repo=commands_repo,
            connections_repo=connections_repo,
            queued=queued,
            decision=decision,
        )
    return {
        "batch_id": batch.id,
        "command_id": queued.id,
        "position": queued.position,
        "status": queued.status.value,
        "mode": decision["mode"],
        "server_auto_allowed": decision["server_auto_allowed"],
        "effective_reason": decision["reason"],
        "result": None,
        "approved_by": None,
        "reason": queued.reason,
        "risk_label": queued.risk_label,
    }


def list_connections(
    *, connections_repo: ConnectionsRepo, settings_repo: ServerSettingsRepo
) -> list[ConnectionResult]:
    """Return saved connections, their server dialect metadata, and auto-execution opt-in."""
    return [
        {
            "alias": connection.alias,
            "hostname": connection.hostname,
            "server_type": connection.server_type.value,
            "detection_ssh": connection.detection_ssh,
            "detection_winrm": connection.detection_winrm,
            "auto_allowed": settings_repo.get_or_default(connection.alias).auto_allowed,
        }
        for connection in connections_repo.list_all()
    ]


def get_mode(*, mode_repo: AppModeRepo, settings_repo: ServerSettingsRepo) -> ModeResult:
    """Report the global mode and which servers are opted in for auto-execution.

    Read-only: an unset global mode is reported as PROPOSE, matching
    ``resolve_auto_behavior``'s safe default.
    """
    try:
        mode = mode_repo.get().mode
    except AppModeNotSetError:
        mode = Mode.PROPOSE
    return {
        "mode": mode.value,
        "auto_allowed_servers": [
            setting.server_alias for setting in settings_repo.list_all() if setting.auto_allowed
        ],
    }


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
                "reason": command.reason,
                "risk_label": command.risk_label,
            }
            for command in commands_repo.list_for_batch(typed_batch_id)
        ],
    }
