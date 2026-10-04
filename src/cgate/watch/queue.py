"""Pure helpers for selecting the active batch, counting waiters, and healing it."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cgate.db.commands import all_terminal
from cgate.db.types import CommandStatus

if TYPE_CHECKING:
    from cgate.db.batches import BatchesRepo
    from cgate.db.commands import CommandsRepo
    from cgate.db.types import Batch, BatchId, Command


def active_batch(batches: BatchesRepo) -> Batch | None:
    """Return the next pending batch in FIFO order, or None."""
    pending = batches.list_pending()
    return pending[0] if pending else None


def select_active_batch(pending: list[Batch], pinned_id: BatchId | None) -> Batch | None:
    """Return the pinned batch if it's still pending, else the FIFO-oldest one.

    Lets a human jump the queue and prioritize a specific batch instead
    of being forced through strict FIFO order. Self-correcting: once the
    pinned batch resolves (or otherwise drops out of `pending`), this
    falls straight back to FIFO without needing the caller to notice and
    clear the pin.
    """
    if pinned_id is not None:
        for batch in pending:
            if batch.id == pinned_id:
                return batch
    return pending[0] if pending else None


def count_waiting(batches: BatchesRepo) -> int:
    """Count batches queued behind the active one."""
    return max(0, len(batches.list_pending()) - 1)


def count_pending_commands(pending_batches: list[Batch], commands: CommandsRepo) -> int:
    """Total PENDING commands across every batch still in the queue."""
    return sum(
        len(pending_commands_in_batch(commands.list_for_batch(batch.id)))
        for batch in pending_batches
    )


def pending_commands_in_batch(commands_for_batch: list[Command]) -> list[Command]:
    """Filter pending commands while preserving their repository order."""
    return [
        command
        for command in commands_for_batch
        if command.status is CommandStatus.PENDING
    ]


def is_batch_resolved(commands_for_batch: list[Command]) -> bool:
    """Return whether every command in the batch has a terminal status."""
    return all_terminal(commands_for_batch)


def resolve_stale_batches(batches: BatchesRepo, commands: CommandsRepo) -> None:
    """Stamp `resolved_at` on any pending batch whose commands are all terminal.

    A command can turn terminal without going through the TUI's own
    approve/reject path -- the MCP AUTO-mode auto-execution path is one
    example. Whenever that happens the batch's `resolved_at` must be
    stamped too, or it sits at the head of the FIFO queue forever with
    nothing left to approve or reject, silently blocking every batch
    behind it. This sweeps up any batch left in that state.
    """
    for batch in batches.list_pending():
        commands_for_batch = commands.list_for_batch(batch.id)
        if commands_for_batch and is_batch_resolved(commands_for_batch):
            batches.mark_resolved(batch.id)


def fail_orphaned_approvals(
    commands: CommandsRepo,
    *,
    stuck_threshold_seconds: int = 120,
) -> None:
    """Mark commands orphaned by a crashed executor.

    Two flavors of orphan, distinguishable by the executor's CAS claim
    (issue #38):

    - ``APPROVED`` with no ``claimed_at``: the process died between
      ``mark_approved`` and the CAS to ``EXECUTING`` -- the executor never
      started. Always an orphan.

    - ``EXECUTING`` with stale ``claimed_at`` (older than
      ``stuck_threshold_seconds``): the process claimed the command, ran
      into the network call, and then died. We recover here too so the
      batch does not sit stuck in EXECUTING forever.

    Live ``EXECUTING`` commands -- fresh ``claimed_at`` -- are left alone.
    Without this distinction, ``cgate watch`` starting on a machine that
    is still running an AUTO-mode executor would mark that executor's
    in-flight commands FAILED and clobber their audit trail (issue #38).
    """
    now = datetime.now(UTC)
    for command in commands.list_by_status(CommandStatus.APPROVED):
        _ = commands.update_status(
            command.id,
            status=CommandStatus.FAILED,
            approved_by=command.approved_by,
            result="interrupted before completion (cgate restarted mid-execution)",
            expected_status=CommandStatus.APPROVED,
        )
    for command in commands.list_by_status(CommandStatus.EXECUTING):
        if command.claimed_at is None:
            # Defensive: should not happen -- the executor CAS stamps
            # claimed_at on the way to EXECUTING. If a manual SQL edit
            # (or a bug in a future change) left this, treat as stuck.
            _ = commands.update_status(
                command.id,
                status=CommandStatus.FAILED,
                approved_by=command.approved_by,
                result=(
                    "interrupted before completion "
                    "(EXECUTING with no claimed_at)"
                ),
                expected_status=CommandStatus.EXECUTING,
            )
            continue
        age = (now - command.claimed_at).total_seconds()
        if age > stuck_threshold_seconds:
            _ = commands.update_status(
                command.id,
                status=CommandStatus.FAILED,
                approved_by=command.approved_by,
                result=(
                    f"interrupted before completion "
                    f"(claimed_at {age:.0f}s old, threshold "
                    f"{stuck_threshold_seconds}s)"
                ),
                expected_status=CommandStatus.EXECUTING,
            )


def heal_queue(batches: BatchesRepo, commands: CommandsRepo) -> None:
    """Repair queue state left inconsistent by a crash or a since-fixed bug.

    Runs once when `cgate watch` starts, before the dashboard is shown.
    """
    fail_orphaned_approvals(commands)
    resolve_stale_batches(batches, commands)
