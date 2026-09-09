"""Pure helpers for selecting the active batch and counting waiters."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cgate.db.types import CommandStatus

if TYPE_CHECKING:
    from cgate.db.batches import BatchesRepo
    from cgate.db.types import Batch, Command


def active_batch(batches: BatchesRepo) -> Batch | None:
    """Return the next pending batch in FIFO order, or None."""
    pending = batches.list_pending()
    return pending[0] if pending else None


def count_waiting(batches: BatchesRepo) -> int:
    """Count batches queued behind the active one."""
    return max(0, len(batches.list_pending()) - 1)


def pending_commands_in_batch(commands_for_batch: list[Command]) -> list[Command]:
    """Filter pending commands while preserving their repository order."""
    return [
        command
        for command in commands_for_batch
        if command.status is CommandStatus.PENDING
    ]


def is_batch_resolved(commands_for_batch: list[Command]) -> bool:
    """Return whether every command in the batch has a terminal status."""
    terminal = {
        CommandStatus.EXECUTED,
        CommandStatus.REJECTED,
        CommandStatus.FAILED,
    }
    return all(command.status in terminal for command in commands_for_batch)
