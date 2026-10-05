"""Shared command lifecycle: APPROVED -> EXECUTING -> terminal.

This module owns the CAS-claim-and-execute pattern that issues #36
and #38 rely on. Both the watch TUI's approval action and the MCP
server's AUTO-mode auto-execution path must use the same lifecycle
implementation, otherwise two callers observing ``APPROVED`` at the same
instant can both invoke the remote executor and run the command twice
(see #36 review blocker #1: the MCP AUTO path duplicated the
function and so bypassed the CAS-claim guard).

What lives here:

- ``ConnectionNotFoundError`` / ``CommandDisappearedError``: shared
  boundary errors so the watch app and the MCP server report the same
  thing.
- ``mark_approved``: CAS PENDING -> APPROVED.
- ``execute_and_finalize``: CAS APPROVED -> EXECUTING -> terminal with
  ``expected_status`` so a zombie executor whose process died mid-call
  cannot clobber the heal's FAILED stamp (see #38 follow-up).
- ``reject_one``: CAS PENDING -> REJECTED.

The watch app re-exports these names from ``cgate.watch.approval`` for
backwards compatibility with the rest of the watch app and any
downstream caller; the MCP server imports directly from here to keep
its dependency surface free of the watch UI module.
"""
from __future__ import annotations

import getpass
from typing import TYPE_CHECKING

from cgate.db.commands import all_terminal
from cgate.db.types import CommandStatus
from cgate.executor import selector

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


def mark_approved(
    *,
    commands: CommandsRepo,
    connections: ConnectionsRepo,
    command_id: CommandId,
) -> Command | None:
    """Transition one pending command to APPROVED, without executing it.

    The fast half of the approval two-step split (paired with
    ``execute_and_finalize``): a plain DB write, done in milliseconds.
    The watch TUI calls this first and refreshes before the slow half, so
    a human sees the "approved, running" state (status glyph ◐) instead
    of the queue looking frozen while the executor's timeout runs (up to a
    minute by default).

    Returns the command as-is (still PENDING) if the CAS loses a race
    with another decision on it, or if it's already past PENDING for
    any other reason.
    """
    command = commands.get(command_id)
    if command is None or command.status is not CommandStatus.PENDING:
        return command
    connection = connections.get(command.server_alias)
    if connection is None:
        raise ConnectionNotFoundError(command.server_alias)
    approved = commands.update_status(
        command_id,
        status=CommandStatus.APPROVED,
        approved_by=_approve_by(),
        expected_status=CommandStatus.PENDING,
    )
    return commands.get(command_id) if approved else command


def execute_and_finalize(  # noqa: PLR0913 - signature follows the required repository DI boundary
    *,
    db: Database,
    commands: CommandsRepo,
    connections: ConnectionsRepo,
    batches: BatchesRepo,
    command_id: CommandId,
    timeout: float = 60.0,
) -> tuple[Command | None, ExecutionResult | None]:
    """Execute an already-APPROVED command and stamp EXECUTED/FAILED.

    Issue #36: a plain ``status == APPROVED`` check is racy -- two
    callers (a ``cgate watch`` instance and the MCP AUTO-mode
    auto-execution path, or two ``cgate watch`` instances on the same
    DB) could both pass the check and both invoke ``execute_command``
    against the remote server, running a destructive command twice.

    The fix is the two-step CAS state machine:

      APPROVED --[CAS APPROVED->EXECUTING, stamps claimed_at]--> EXECUTING
                                                                 --[remote done]--> EXECUTED | FAILED

    ``execute_and_finalize`` does the ``APPROVED -> EXECUTING`` transition
    through ``update_status(expected_status=APPROVED)``. If the CAS loses
    (another caller already won), the loser returns immediately without
    invoking the executor. The winner stamps ``claimed_at = now`` (the
    heal's liveness marker -- issue #38).

    The terminal write also uses ``expected_status=EXECUTING``. Without
    it, a zombie executor whose process died mid-call could clobber the
    heal's FAILED stamp when its network call eventually returned. This
    is verified end-to-end by
    ``test_executor_terminal_write_is_a_noop_when_heal_already_marked_failed``.

    If the network call itself raises (SSH transport failure, WinRM HTTP
    error, connection refused, ...), the row would otherwise sit in
    EXECUTING until the next heal -- and the batch would block the FIFO
    queue while ``resolve_stale_batches`` waits for every command to
    terminate. Mark FAILED here with the EXECUTING CAS guard so the row
    terminates promptly and the audit trail records what happened. We do
    not re-raise -- the caller gets a clean ``(Command(FAILED), None)``
    return, so they don't need a second try/except layer to handle the
    failure path.
    """
    del db
    command = commands.get(command_id)
    if command is None or command.status is not CommandStatus.APPROVED:
        return command, None
    connection = connections.get(command.server_alias)
    if connection is None:
        raise ConnectionNotFoundError(command.server_alias)
    approver = command.approved_by or _approve_by()

    claimed = commands.update_status(
        command_id,
        status=CommandStatus.EXECUTING,
        approved_by=approver,
        expected_status=CommandStatus.APPROVED,
    )
    if not claimed:
        refreshed = commands.get(command_id)
        return refreshed if refreshed is not None else command, None

    try:
        result = selector.execute_command(connection, command.command, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - the executor surfaces generic Exception subclasses; we want every transport failure here
        _ = commands.update_status(
            command_id,
            status=CommandStatus.FAILED,
            approved_by=approver,
            result=f"executor raised: {type(exc).__name__}: {exc}",
            expected_status=CommandStatus.EXECUTING,
        )
        updated = commands.get(command_id)
        if updated is None:
            raise CommandDisappearedError(command_id) from exc
        _maybe_resolve_batch(batches, commands, updated.batch_id)
        return updated, None

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
        expected_status=CommandStatus.EXECUTING,
    )
    updated = commands.get(command_id)
    if updated is None:
        raise CommandDisappearedError(command_id)
    _maybe_resolve_batch(batches, commands, updated.batch_id)
    return updated, result


def _maybe_resolve_batch(
    batches: BatchesRepo,
    commands: CommandsRepo,
    batch_id: BatchId,
) -> None:
    """Stamp a batch when every command has reached a terminal status."""
    if all_terminal(commands.list_for_batch(batch_id)):
        batches.mark_resolved(batch_id)


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


def approve_one(  # noqa: PLR0913 - signature follows the required repository DI boundary
    *,
    db: Database,
    commands: CommandsRepo,
    connections: ConnectionsRepo,
    batches: BatchesRepo,
    command_id: CommandId,
    timeout: float = 60.0,
) -> tuple[Command | None, ExecutionResult | None]:
    """Approve and synchronously execute one pending command.

    Composes ``mark_approved`` + ``execute_and_finalize`` in one call, for
    callers that don't need the mid-flight refresh those two are split
    for: ``approve_remaining`` and the MCP AUTO-mode auto-execution path.
    """
    _ = mark_approved(commands=commands, connections=connections, command_id=command_id)
    return execute_and_finalize(
        db=db,
        commands=commands,
        connections=connections,
        batches=batches,
        command_id=command_id,
        timeout=timeout,
    )


def approve_remaining(  # noqa: PLR0913 - signature follows the required repository DI boundary
    *,
    db: Database,
    commands: CommandsRepo,
    connections: ConnectionsRepo,
    batches: BatchesRepo,
    remaining: list[Command],
    timeout: float = 60.0,
) -> list[tuple[Command, ExecutionResult | None]]:
    """Approve and execute pending commands in their natural order.

    The exception fallback that marks FAILED uses a CAS guard
    (``expected_status``) instead of an unguarded write. Without the
    guard, an exception caught here could clobber the FAILED stamp the
    startup heal wrote from a dead-executor case (#38 follow-up).
    """
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
    """Reject each pending command in their natural order."""
    return [
        reject_one(commands=commands, batches=batches, command_id=command.id)
        for command in remaining
        if command.status is CommandStatus.PENDING
    ]


__all__ = [
    "CommandDisappearedError",
    "ConnectionNotFoundError",
    "approve_one",
    "approve_remaining",
    "execute_and_finalize",
    "mark_approved",
    "reject_one",
    "reject_remaining",
]