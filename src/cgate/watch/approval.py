"""Decision handlers for approving, executing, and rejecting commands."""

from __future__ import annotations

import getpass
from typing import TYPE_CHECKING

from cgate.db.types import CommandStatus
from cgate.executor.selector import execute_command
from cgate.watch.queue import is_batch_resolved

if TYPE_CHECKING:
    from cgate.connections.store import ConnectionsRepo
    from cgate.db.batches import BatchesRepo
    from cgate.db.commands import CommandsRepo
    from cgate.db.connection import Database
    from cgate.db.types import BatchId, Command, CommandId
    from cgate.executor.base import ExecutionResult


class ConnectionNotFoundError(LookupError):
    """A command references a connection alias absent from local storage."""

    alias: str

    def __init__(self, alias: str) -> None:
        """Store the missing alias for a useful boundary error."""
        self.alias = alias
        message = f"connection '{alias}' not found"
        super().__init__(message)


class CommandDisappearedError(RuntimeError):
    """A command could not be fetched immediately after a status update."""

    command_id: CommandId

    def __init__(self, command_id: CommandId) -> None:
        """Store the missing command ID for a useful boundary error."""
        self.command_id = command_id
        message = f"command {command_id} disappeared after status update"
        super().__init__(message)


def _approve_by() -> str:
    """Return the OS username for the audit column."""
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return "unknown"


def approve_one(  # noqa: PLR0913 - signature follows the required repository DI boundary
    *,
    db: Database,
    commands: CommandsRepo,
    connections: ConnectionsRepo,
    batches: BatchesRepo,
    command_id: CommandId,
    timeout: float = 60.0,
) -> tuple[Command | None, ExecutionResult | None]:
    """Approve and synchronously execute one pending command."""
    del db
    command = commands.get(command_id)
    if command is None or command.status is not CommandStatus.PENDING:
        return command, None
    connection = connections.get(command.server_alias)
    if connection is None:
        raise ConnectionNotFoundError(command.server_alias)

    approver = _approve_by()
    approved = commands.update_status(
        command_id,
        status=CommandStatus.APPROVED,
        approved_by=approver,
        expected_status=CommandStatus.PENDING,
    )
    if not approved:
        # Lost a race with another decision on this command since the
        # PENDING check above -- report its current state, don't execute.
        return commands.get(command_id), None
    result = execute_command(connection, command.command, timeout=timeout)
    status = CommandStatus.EXECUTED if result.ok else CommandStatus.FAILED
    output = result.stdout
    if result.stderr:
        separator = "\n" if output else ""
        output = f"{output}{separator}--- stderr ---\n{result.stderr}"
    _ = commands.update_status(
        command_id,
        status=status,
        approved_by=approver,
        result=output,
    )
    updated = commands.get(command_id)
    if updated is None:
        raise CommandDisappearedError(command_id)
    _maybe_resolve_batch(batches, commands, updated.batch_id)
    return updated, result


def reject_one(
    *,
    commands: CommandsRepo,
    batches: BatchesRepo,
    command_id: CommandId,
) -> Command:
    """Reject one command and resolve its batch when it becomes terminal."""
    _ = commands.update_status(
        command_id,
        status=CommandStatus.REJECTED,
        approved_by=_approve_by(),
        expected_status=CommandStatus.PENDING,
    )
    updated = commands.get(command_id)
    if updated is None:
        raise CommandDisappearedError(command_id)
    _maybe_resolve_batch(batches, commands, updated.batch_id)
    return updated


def approve_remaining(  # noqa: PLR0913 - signature follows the required repository DI boundary
    *,
    db: Database,
    commands: CommandsRepo,
    connections: ConnectionsRepo,
    batches: BatchesRepo,
    remaining: list[Command],
    timeout: float = 60.0,
) -> list[tuple[Command, ExecutionResult | None]]:
    """Approve and execute pending commands in their natural order."""
    results: list[tuple[Command, ExecutionResult | None]] = []
    for command in remaining:
        if command.status is not CommandStatus.PENDING:
            continue
        try:
            updated, result = approve_one(
                db=db,
                commands=commands,
                connections=connections,
                batches=batches,
                command_id=command.id,
                timeout=timeout,
            )
        except Exception as exc:
            _ = commands.update_status(
                command.id,
                status=CommandStatus.FAILED,
                approved_by=_approve_by(),
                result=f"approval/connect failed: {exc}",
            )
            updated = commands.get(command.id)
            result = None
            if updated is None:
                raise CommandDisappearedError(command.id) from exc
            _maybe_resolve_batch(batches, commands, updated.batch_id)
        if updated is not None:
            results.append((updated, result))
    return results


def reject_remaining(
    *,
    commands: CommandsRepo,
    batches: BatchesRepo,
    remaining: list[Command],
) -> list[Command]:
    """Reject each pending command in its natural order."""
    return [
        reject_one(commands=commands, batches=batches, command_id=command.id)
        for command in remaining
        if command.status is CommandStatus.PENDING
    ]


def _maybe_resolve_batch(
    batches: BatchesRepo,
    commands: CommandsRepo,
    batch_id: BatchId,
) -> None:
    """Stamp a batch when every command has reached a terminal status."""
    if is_batch_resolved(commands.list_for_batch(batch_id)):
        batches.mark_resolved(batch_id)
